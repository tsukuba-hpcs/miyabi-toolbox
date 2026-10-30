"""Command line: the same tools as the MCP server, plus serve, init, notify and notifyd (DESIGN.md 6.3, 9).

Every tool runs through `miyabi-mcp call <tool> '<json arguments>'` and prints one JSON document;
exit 0 when ok, 1 on a reported failure, 2 on bad arguments. `notifyd` and `notify listen` run on the
agent's machine, which need not be a Miyabi node.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__, config, tools
from .util import MiyabiError, canonical, compact, failure, ok


def _emit(result: dict, pretty: bool) -> int:
    print(json.dumps(result, ensure_ascii=False, indent=2) if pretty else compact(result), flush=True)
    return 0 if result.get("ok") else 1


def _arguments(text: str | None) -> dict:
    if text is None or text == "":
        return {}
    if text == "-":
        text = sys.stdin.read()
    elif text.startswith("@"):
        text = Path(text[1:]).read_text()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise MiyabiError("invalid_arguments", "tool arguments must be a JSON object")
    return value


def _daemon_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--notifier", help="daemon name (default: this machine's short hostname)")
    parser.add_argument("--remote", help="ssh alias of the Miyabi login node; omit when running on it")
    parser.add_argument("--remote-launcher", help="bin/miyabi-mcp on the login node")
    parser.add_argument("--poll-seconds", type=int, default=300)
    parser.add_argument("--idle-polls", type=int, default=2)
    parser.add_argument("--fail-notify-after", type=int, default=3)


def _options(args: argparse.Namespace):
    from .notifyd import Options, short_host

    return Options(notifier=args.notifier or short_host(), remote=args.remote, remote_launcher=args.remote_launcher,
                   poll_seconds=args.poll_seconds, idle_polls=args.idle_polls, fail_notify_after=args.fail_notify_after)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="miyabi-mcp", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"miyabi-mcp {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    for target, default in ((parser, None), (common, argparse.SUPPRESS)):
        target.add_argument("--project", default=default, help="project name under /work/<group>/<user>/")
        target.add_argument("--project-dir", default=default, help="absolute project directory")
        target.add_argument("--group", default=default, help="PBS group (default: your only /work/<group>/<user>)")
        target.add_argument("--pretty", action="store_true", default=False if default is None else default,
                            help="indent JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, **kwargs) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], **kwargs)  # options before or after the command

    serve = add("serve", help="run the MCP server on stdio (on a Miyabi login node)")
    serve.add_argument("--notifier", help="default daemon name for notifications (the agent's machine)")
    serve.add_argument("--local-launcher", help="bin/miyabi-mcp on the agent's machine, for returned commands")
    serve.add_argument("--remote-alias", help="ssh alias of this login node as seen from the agent's machine")
    init = add("init", help="create <project>/.miyabi-mcp/, optionally installing a policy file")
    init.add_argument("--policy", help="JSON policy file to validate and install as the project policy")
    add("tools", help="list the tools and their input schemas")
    call = add("call", help="run one tool")
    call.add_argument("tool", choices=sorted(tools.BY_NAME))
    call.add_argument("arguments", nargs="?", help="JSON object, @file or - for stdin")
    add("context", help="= call pbs_context")
    add("queues", help="= call pbs_queues")
    add("jobs", help="= call job_list")
    job = add("job", help="= call job_get")
    job.add_argument("job_ids", nargs="+")
    job.add_argument("--tail", type=int, default=0)
    ledger = add("ledger", help="= call ledger_query")
    ledger.add_argument("--format", default="records", choices=["records", "summary", "events", "smooth-research"])
    ledger.add_argument("--cursor")
    ledger.add_argument("--jsonl", action="store_true", help="smooth-research: one canonical observation per line")
    notify = add("notify", help="notification plumbing: poll and report (login node), listen (agent's machine)")
    notify_sub = notify.add_subparsers(dest="action", required=True)
    poll = notify_sub.add_parser("poll", parents=[common], help="messages due for a notifier (used by notifyd)")
    poll.add_argument("--notifier", required=True)
    poll.add_argument("--caller")
    report = notify_sub.add_parser("report", parents=[common], help="record a delivery attempt (used by notifyd)")
    report.add_argument("--msg", required=True)
    report.add_argument("--outcome", required=True, help="JSON outcome, e.g. {\"status\": \"delivered\"}")
    listen = notify_sub.add_parser("listen", parents=[common],
                                   help="wait for one notification on this machine (Claude Code background command)")
    listen.add_argument("--id", required=True)
    _daemon_options(listen)
    notifyd = add("notifyd", help="the notification daemon on the agent's machine")
    notifyd.add_argument("action", choices=["ensure", "run", "status", "stop"])
    _daemon_options(notifyd)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        # Commands that run on the agent's machine: no Miyabi settings needed.
        if args.command == "notifyd":
            from . import notifyd

            options = _options(args)
            if args.action == "run":
                return notifyd.run(options)
            return _emit({"ensure": notifyd.ensure, "status": notifyd.status, "stop": notifyd.stop}[args.action](options),
                         args.pretty)
        if args.command == "notify" and args.action == "listen":
            from .notifyd import listen

            return listen(args.id, _options(args))
        settings = config.load(project_dir=args.project_dir, project=args.project, group=args.group)
        if args.command == "serve":
            from .server import serve

            overrides = {key: value for key, value in (("notifier", args.notifier), ("local_launcher", args.local_launcher),
                                                      ("remote_alias", args.remote_alias)) if value}
            return serve(replace(settings, config={**settings.config, **overrides}))
        if args.command == "notify":
            from . import notify

            if args.action == "poll":
                return _emit(notify.poll(settings, args.notifier, args.caller), args.pretty)
            return _emit(notify.report(settings, args.msg, json.loads(args.outcome)), args.pretty)
        if args.command == "init":
            return _emit(_init(settings, args.policy), args.pretty)
        if args.command == "tools":
            return _emit(ok(tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema,
                                    "read_only": t.read_only, "destructive": t.destructive} for t in tools.TOOLS]),
                         args.pretty)
        if args.command == "call":
            return _emit(tools.run(args.tool, settings, _arguments(args.arguments)), args.pretty)
        shortcuts = {"context": ("pbs_context", {}), "queues": ("pbs_queues", {}), "jobs": ("job_list", {})}
        if args.command in shortcuts:
            name, arguments = shortcuts[args.command]
            return _emit(tools.run(name, settings, arguments), args.pretty)
        if args.command == "job":
            return _emit(tools.run("job_get", settings, {"job_ids": args.job_ids, "tail_lines": args.tail}), args.pretty)
        if args.command == "ledger":
            query = {"format": args.format, **({"cursor": args.cursor} if args.cursor else {})}
            result = tools.run("ledger_query", settings, query)
            if args.jsonl and result.get("ok") and args.format == "smooth-research":
                for item in result["observations"]:
                    print(canonical(item))
                print(compact({"next_cursor": result.get("next_cursor")}), file=sys.stderr)
                return 0
            return _emit(result, args.pretty)
    except MiyabiError as exc:
        return _emit(failure(exc), args.pretty)
    except json.JSONDecodeError as exc:
        _emit(failure(MiyabiError("invalid_arguments", f"arguments are not JSON: {exc}")), args.pretty)
        return 2
    return 2


def _init(settings: config.Settings, policy_file: str | None) -> dict:
    from . import policy as policies
    from .util import write_json

    state = settings.require_project()
    if policy_file:
        data = json.loads(Path(policy_file).read_text())
        policies.validate(data, "project policy")
        if "user" in data:
            raise MiyabiError("invalid_policy", "project policy cannot set user-wide caps (`user`)")
        write_json(state / "policy.json", data)
    return ok(project_dir=str(settings.project_dir), state_dir=str(state), policy=policies.load(settings))
