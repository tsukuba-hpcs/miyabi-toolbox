"""Command line: the same tools as the MCP server, plus serve, wait and init (DESIGN.md 6.3).

Every tool runs through `miyabi-mcp call <tool> '<json arguments>'` and prints one JSON document;
exit 0 when ok, 1 on a reported failure, 2 on bad arguments.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, config, tools
from .util import MiyabiError, canonical, compact, failure, ok


def _settings(args: argparse.Namespace) -> config.Settings:
    return config.load(project_dir=args.project_dir, project=args.project, group=args.group)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="miyabi-mcp", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"miyabi-mcp {__version__}")
    for target, default in ((parser, None), (common := argparse.ArgumentParser(add_help=False), argparse.SUPPRESS)):
        target.add_argument("--project", default=default, help="project name under /work/<group>/<user>/")
        target.add_argument("--project-dir", default=default, help="absolute project directory")
        target.add_argument("--group", default=default, help="PBS group (default: your only /work/<group>/<user>)")
        target.add_argument("--pretty", action="store_true", default=False if default is None else default,
                            help="indent JSON output")
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser = sub.add_parser
    sub.add_parser = lambda *a, **k: add_parser(*a, parents=[common], **k)  # options before or after the command
    sub.add_parser("serve", help="run the MCP server on stdio")
    wait = sub.add_parser("wait", help="run a watch's waiter until it delivers (see watch_create)")
    wait.add_argument("--watch", required=True)
    init = sub.add_parser("init", help="create <project>/.miyabi-mcp/, optionally installing a policy file")
    init.add_argument("--policy", help="JSON policy file to validate and install as the project policy")
    sub.add_parser("tools", help="list the tools and their input schemas")
    call = sub.add_parser("call", help="run one tool")
    call.add_argument("tool", choices=sorted(tools.BY_NAME))
    call.add_argument("arguments", nargs="?", help="JSON object, @file or - for stdin")
    sub.add_parser("context", help="= call pbs_context")
    sub.add_parser("queues", help="= call pbs_queues")
    sub.add_parser("jobs", help="= call job_list")
    job = sub.add_parser("job", help="= call job_get")
    job.add_argument("job_ids", nargs="+")
    job.add_argument("--tail", type=int, default=0)
    ledger = sub.add_parser("ledger", help="= call ledger_query")
    ledger.add_argument("--format", default="records", choices=["records", "summary", "events", "smooth-research"])
    ledger.add_argument("--cursor")
    ledger.add_argument("--jsonl", action="store_true", help="smooth-research: one canonical observation per line")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        settings = _settings(args)
        if args.command == "serve":
            from .server import serve

            return serve(settings)
        if args.command == "wait":
            from .watch import run_waiter

            return run_waiter(settings, args.watch)
        if args.command == "init":
            return _emit(_init(settings, args.policy), args.pretty)
        if args.command == "tools":
            return _emit(ok(tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema,
                                    "read_only": t.read_only, "destructive": t.destructive} for t in tools.TOOLS]),
                         args.pretty)
        if args.command == "call":
            return _emit(tools.run(args.tool, settings, _arguments(args.arguments)), args.pretty)
        if args.command == "context":
            return _emit(tools.run("pbs_context", settings, {}), args.pretty)
        if args.command == "queues":
            return _emit(tools.run("pbs_queues", settings, {}), args.pretty)
        if args.command == "jobs":
            return _emit(tools.run("job_list", settings, {}), args.pretty)
        if args.command == "job":
            return _emit(tools.run("job_get", settings, {"job_ids": args.job_ids, "tail_lines": args.tail}),
                         args.pretty)
        if args.command == "ledger":
            query = {"format": args.format}
            if args.cursor:
                query["cursor"] = args.cursor
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
        return _emit(failure(MiyabiError("invalid_arguments", f"arguments are not JSON: {exc}")), args.pretty) and 2
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
