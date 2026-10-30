"""The tool registry shared by the MCP server and the CLI: schemas, annotations and handlers (DESIGN.md 8.1)."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Any, Callable

from . import jobs, submit as submissions, watch
from .config import Settings
from .ledger import register_project
from .util import MiyabiError, failure

STR = {"type": "string"}
STRS = {"type": "array", "items": {"type": "string"}}
LABELS = {"type": "object", "additionalProperties": {"type": "string"}}
PROJECT = {"type": "string", "description": "Project name under /work/<group>/<user>/ or an absolute project "
                                            "directory; defaults to the project the server was started for."}
OUTPUT_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def schema(properties: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": {**properties, "project": PROJECT}, "required": list(required),
            "additionalProperties": False}


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    handler: Callable[..., dict]
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = False


DELIVER = {"type": "object", "description": "How to wake the agent. method: claude_background (default; run the "
           "returned command in the background), codex_queue / codex_goal (thread, remote), herdr (agent, machine), "
           "tmux (target), command (name from config.json), file (path inside the project).",
           "properties": {"method": {"enum": ["claude_background", "codex_queue", "codex_goal", "herdr", "tmux",
                                              "command", "file"]},
                          "thread": STR, "remote": STR, "codex": STR, "agent": STR, "machine": STR, "target": STR,
                          "name": STR, "path": STR},
           "required": ["method"], "additionalProperties": False}
WATCH_PROPERTIES = {
    "job_ids": {**STRS, "description": "Jobs in this project's ledger."},
    "when": {"enum": ["all_ended", "any_ended", "started"], "description": "Default all_ended."},
    "queue": {"type": "object", "description": 'Wait for a queue instead, e.g. {"queue": "debug-g", "state": "started"}.',
              "properties": {"queue": STR, "state": {"enum": ["started"]}}, "required": ["queue"],
              "additionalProperties": False},
    "timeout": {"type": ["string", "number"], "description": "Fire anyway after this long: seconds, HH:MM:SS or e.g. 12h."},
    "deliver": DELIVER,
    "message": {**STR, "description": "Your continuation: what to do next and which outputs to check."},
    "labels": LABELS,
    "poll_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
    "start_waiter": {"type": "boolean", "description": "Start a detached waiter on the login node (default for "
                                                      "every method except claude_background)."},
}

TOOLS = [
    Tool("pbs_context", "Miyabi context",
         "Where the server runs (login or compute plane, system, host), the resolved project and its state "
         "directory, the PBS projects you may use, the scheduled stop and the effective policy. Call this first.",
         schema({"target_system": {"enum": ["Miyabi-G", "Miyabi-C"]}, "include_pbs": {"type": "boolean"}}),
         jobs.pbs_context),
    Tool("pbs_queues", "Queues, limits and budget",
         "Queue shapes and status, including REMAIN_ELAPSE before the scheduled stop; project limits and usage "
         "(qstat --limit); pool occupancy (not a start-time prediction); the group's token budget.",
         schema({"system": STR, "include": {"type": "array", "items": {"enum": ["rsc", "limit", "rscuse", "token"]}}}),
         jobs.pbs_queues),
    Tool("job_list", "List jobs",
         "The user's unfinished jobs from one shared qstat snapshot (at most ~15 s old), merged with this project's "
         "ledger; optional bounded history. Also brings the ledger up to date. FINISH never means success.",
         schema({"status": STRS, "name_prefix": STR, "labels": LABELS, "project_only": {"type": "boolean"},
                 "history": {"type": "object", "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 7},
                                                               "limit": {"type": "integer", "minimum": 1,
                                                                         "maximum": 500}},
                             "additionalProperties": False},
                 "fields": STRS, "summary": {"type": "boolean"}, "refresh_ledger": {"type": "boolean"}}),
         jobs.job_list),
    Tool("job_get", "Job details",
         "Details of 1–40 jobs from qstat -f (active) or qstat -H -f (finished): times, resources, exit status, "
         "paths, ledger record, MIYABI_* variables; tail_lines reads the end of the job's own output file.",
         schema({"job_ids": {**STRS, "minItems": 1, "maxItems": 40},
                 "tail_lines": {"type": "integer", "minimum": 0, "maximum": 200}}, ("job_ids",)),
         jobs.job_get),
    Tool("job_submit", "Submit a batch job",
         "Two steps. dry_run=true (default) checks the script (bash -n, placeholders, #PBS overrides), queue shape "
         "and walltime, the scheduled stop, group, limits and the project policy, and returns the plan with a "
         "plan_digest. Call again with the same arguments, dry_run=false and that plan_digest to run qsub. "
         "request_key makes retries safe; client_ref is exported as the attempt id. Jobs get MIYABI_SUBMIT_ID, "
         "MIYABI_CLIENT_REF, MIYABI_PROJECT_DIR and MIYABI_JOB_RECORD; output defaults to <project>/.miyabi-mcp/logs/.",
         schema({"script": {**STR, "description": "Absolute path of the job script."},
                 "script_text": {**STR, "description": "The job script itself (stored as the snapshot)."},
                 "queue": {**STR, "description": "Top-level queue, e.g. debug-g, regular-g, debug-mig."},
                 "walltime": {**STR, "description": "HH:MM:SS; size it from measurements (×1.5), never the cap."},
                 "select": {**STR, "description": "PBS select, e.g. 1:mpiprocs=1 (MIG queues count instances)."},
                 "nodes": {"type": "integer", "minimum": 1}, "group": STR, "name": STR,
                 "workdir": {**STR, "description": "qsub working directory; default the project directory."},
                 "output": {**STR, "description": "Absolute PBS output path on /work."}, "join": STR,
                 "env": {"type": "object", "additionalProperties": {"type": "string"}},
                 "depend": STR, "array": STR, "labels": LABELS, "client_ref": STR, "request_key": STR,
                 "expected_seconds": {"type": "number"}, "note": STR,
                 "dry_run": {"type": "boolean", "default": True}, "plan_digest": STR,
                 "resubmit": {"type": "boolean"}, "allow_placeholders": {"type": "boolean"},
                 "watch": {"type": "object", "properties": {k: v for k, v in WATCH_PROPERTIES.items()
                                                             if k != "job_ids"}, "additionalProperties": False}},
                ("queue", "walltime")),
         submissions.submit, read_only=False),
    Tool("job_cancel", "Cancel jobs",
         "dry_run=true (default) lists the targets and a targets_digest; dry_run=false with a reason and that "
         "digest runs qdel. Only jobs in this project's ledger unless the policy allows cancel_scope=any.",
         schema({"job_ids": STRS, "label_selector": LABELS, "reason": STR, "dry_run": {"type": "boolean"},
                 "targets_digest": STR, "allow_external": {"type": "boolean"}}),
         jobs.job_cancel, read_only=False, destructive=True),
    Tool("job_wait", "Wait briefly",
         "Block up to ~45 s for jobs of a few minutes (all_ended, any_ended or started). For anything longer use "
         "watch_create and end your turn instead of polling.",
         schema({"job_ids": {**STRS, "minItems": 1}, "when": {"enum": ["all_ended", "any_ended", "started"]},
                 "timeout_s": {"type": "number", "minimum": 1}}, ("job_ids",)),
         watch.job_wait),
    Tool("watch_create", "Arm a wake-up",
         "Wake an agent when jobs end or start, when a queue reopens, or at a timeout. For Claude Code, run the "
         "returned command with run_in_background=true and end your turn; its exit carries the wake-up message. "
         "Other methods start a detached waiter on the login node.",
         schema(WATCH_PROPERTIES), watch.create, read_only=False),
    Tool("watch_list", "List wake-ups",
         "Watches of this project with their status, waiter heartbeat (waiter_alive) and, for a lost waiter, the "
         "command that resumes it.", schema({"status": STRS}), watch.watch_list),
    Tool("watch_cancel", "Cancel a wake-up", "Cancel an armed or fired watch (a delivery cannot be retracted).",
         schema({"watch_id": STR, "reason": STR}, ("watch_id",)), watch.watch_cancel, read_only=False),
    Tool("watch_ack", "Acknowledge a wake-up",
         "Acknowledge a delivered wake-up with the ack_token from its message: it records that you resumed.",
         schema({"watch_id": STR, "ack_token": STR}, ("watch_id", "ack_token")), watch.watch_ack,
         read_only=False, idempotent=True),
    Tool("ledger_query", "Query the job ledger",
         "This project's ledger: records, a summary (counts, requested and used node·h or MIG instance·h), raw "
         "events, or smooth-research observations (format=smooth-research; byte-stable, resumable with cursor).",
         schema({"format": {"enum": ["records", "summary", "events", "smooth-research"]}, "labels": LABELS,
                 "client_ref": STR, "job_ids": STRS, "state": STRS, "since": STR, "until": STR, "cursor": STR,
                 "limit": {"type": "integer", "minimum": 1}, "include_token": {"type": "boolean"},
                 "refresh": {"type": "boolean"}}),
         jobs.ledger_query),
    Tool("job_annotate", "Annotate or adopt a job",
         "Add labels, a note or an application-level verdict to a ledger job; adopt=true brings a job submitted "
         "outside miyabi-mcp into this project's ledger (refused if another project owns it).",
         schema({"job_id": STR, "labels": LABELS, "note": STR, "adopt": {"type": "boolean"}, "client_ref": STR,
                 "verdict": {"type": "object", "properties": {
                     "status": {"enum": ["unreviewed", "verified_success", "verified_failure"]}, "evidence": STR},
                     "required": ["status"], "additionalProperties": False}}, ("job_id",)),
         jobs.job_annotate, read_only=False),
]
BY_NAME = {tool.name: tool for tool in TOOLS}


def run(name: str, settings: Settings, arguments: dict[str, Any], **hooks: Any) -> dict:
    """Run a tool; expected failures become {"ok": false, "error": ...}, never exceptions."""
    tool = BY_NAME.get(name)
    if tool is None:
        return failure(MiyabiError("unknown_tool", f"no tool {name}"))
    args = dict(arguments or {})
    try:
        local = settings.with_project(args.pop("project", None))
        if not tool.read_only and local.project_dir is not None and local.project_dir.is_dir():
            register_project(local)
        if name == "job_wait":
            return tool.handler(local, args, **hooks)
        return tool.handler(local, args)
    except MiyabiError as exc:
        return failure(exc)
    except Exception as exc:  # report, do not crash the server
        return failure(MiyabiError("internal_error", f"{type(exc).__name__}: {exc}",
                                   traceback=traceback.format_exc()[-3000:]))
