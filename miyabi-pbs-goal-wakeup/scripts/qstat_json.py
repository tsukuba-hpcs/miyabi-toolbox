#!/usr/bin/env python3
"""Read Miyabi's qstat job tables and emit a single JSON document (stdlib only)."""

import argparse
from collections import Counter
import json
import math
import os
import re
import subprocess
import sys


SCHEMA_VERSION = 1
HEADER = (
    "JOB_ID", "JOB_NAME", "STATUS", "PROJECT", "QUEUE", "START_DATE",
    "ELAPSE", "TOKEN", "NODE", "MIG",
)
FIELDS = (
    "job_id", "job_name", "status", "project", "queue", "start_date",
    "elapsed_seconds", "token", "token_estimate", "nodes", "mig",
)
JOB_ID = re.compile(r"[0-9]+(?:\[(?:[0-9]+(?:-[0-9]+)?)?\])?(?:\.[A-Za-z0-9_.-]+)?")
EMPTY_MESSAGES = {"No unfinished job found.", "No finished job found.", "No matching job found."}


class ParseError(ValueError):
    pass


class UsageError(ValueError):
    pass


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def timeout_seconds(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive finite number") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def job_id(value):
    if not JOB_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid PBS job ID")
    return value


def field_list(value):
    fields = value.split(",")
    if any(field not in FIELDS for field in fields):
        raise argparse.ArgumentTypeError("fields must be comma-separated names from: " + ",".join(FIELDS))
    if len(fields) != len(set(fields)):
        raise argparse.ArgumentTypeError("fields must not repeat")
    return fields


def parse_args(argv):
    parser = ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("job_ids", nargs="*", type=job_id, help="optional job IDs; quote array IDs")
    parser.add_argument("-H", action="store_true", dest="history", help="query finished jobs")
    parser.add_argument("--hday", type=positive_int, help="history period, 1–31 days (qstat default: 3)")
    parser.add_argument("--hnum", type=positive_int, help="history row cap (qstat default: 100)")
    parser.add_argument("-t", action="store_true", dest="subjobs", help="include array subjobs")
    parser.add_argument("--status", action="append", help="keep this status, case-insensitive; repeat for OR")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--fields", type=field_list, help="emit only these comma-separated job fields")
    output.add_argument("--summary", action="store_true", help="emit status counts instead of job rows")
    parser.add_argument("--timeout", type=timeout_seconds, default=30, help="qstat timeout in seconds (default: 30)")
    parser.add_argument("--pretty", action="store_true", help="indent JSON for human inspection")
    args = parser.parse_args(argv)
    if (args.hday is not None or args.hnum is not None) and not args.history:
        parser.error("--hday and --hnum require -H")
    if args.hday is not None and args.hday > 31:
        parser.error("--hday must not exceed 31")
    if args.status and any(not re.fullmatch(r"[A-Za-z_]+", status) for status in args.status):
        parser.error("--status must be a status name such as RUNNING or FINISH")
    return args


def integer_cell(value):
    if value == "-":
        return None
    if not re.fullmatch(r"[0-9]+", value):
        raise ParseError("invalid node or MIG count")
    return int(value)


def token_cell(value):
    """Return (usage, estimate); parentheses mark a not-yet-used amount, as on queued jobs."""
    if value == "-":
        return None, None
    estimate = value.startswith("(") and value.endswith(")")
    number = value[1:-1] if estimate else value
    if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", number):
        raise ParseError("invalid token value")
    result = float(number.replace(",", ""))
    if not math.isfinite(result):
        raise ParseError("non-finite token value")
    return (None, result) if estimate else (result, None)


def elapsed_cell(value):
    if value in {"-", "--:--:--"}:
        return None
    if not re.fullmatch(r"[0-9]+:[0-5][0-9]:[0-5][0-9]", value):
        raise ParseError("invalid elapsed time")
    hours, minutes, seconds = map(int, value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def start_date_cell(value):
    # "--:--:--" is observed for finished jobs that never started, e.g. deleted while queued.
    if value in {"-", "--:--:--", "--/-- --:--:--"}:
        return None
    date = value
    if value.startswith("(") and value.endswith(")"):
        date = value[1:-1]
    if not re.fullmatch(r"[0-9]{2}/[0-9]{2} [0-9]{2}:[0-5][0-9]:[0-5][0-9]", date):
        raise ParseError("invalid start date")
    # No year or timezone is supplied. Parentheses mark a scheduler estimate.
    return value


def parse_row(line, name_start, status_start):
    identifier = line[:name_start].strip()
    name = line[name_start:status_start].strip()
    if not JOB_ID.fullmatch(identifier) or not name:
        raise ParseError("invalid job ID or missing job name")
    # Only the name is sliced by header positions. Right-aligned numeric cells
    # and a status wider than its advertised width must not shift other fields.
    cells = line[status_start:].split()
    if len(cells) not in (8, 9) or not re.fullmatch(r"[A-Z_]+", cells[0]):
        raise ParseError("unexpected job columns")
    status, project, queue = cells[:3]
    date = " ".join(cells[3:-4])
    elapsed, token, nodes, mig = cells[-4:]
    return dict(zip(FIELDS, (
        identifier, name, status, project, queue, start_date_cell(date),
        elapsed_cell(elapsed), *token_cell(token), integer_cell(nodes), integer_cell(mig),
    )))


def parse_qstat(output):
    """Parse the default/-ll table; reject unexpected output instead of dropping rows."""
    jobs, notices = [], []
    layout = None
    empty = False
    for number, line in enumerate(output.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Miyabi scheduled stop time:"):
            notices.append(stripped)
            continue
        if stripped in EMPTY_MESSAGES:
            if layout is not None or empty:
                raise ParseError("line {}: contradictory empty-job message".format(number))
            empty = True
            continue
        if tuple(stripped.split()) == HEADER:
            if empty:
                raise ParseError("line {}: job table after empty-job message".format(number))
            layout = (line.index("JOB_NAME"), line.index("STATUS"))
            continue
        if layout is None:
            raise ParseError("line {}: expected Miyabi job table or explicit empty-job message".format(number))
        try:
            jobs.append(parse_row(line, *layout))
        except ParseError as exc:
            raise ParseError("line {}: {}".format(number, exc)) from exc
    if layout is None and not empty:
        raise ParseError("no job table or explicit empty-job message found")
    return jobs, notices


def build_command(args):
    command = ["qstat", "-ll"]
    if args.history:
        command.append("-H")
    if args.hday is not None:
        command.extend(["--hday", str(args.hday)])
    if args.hnum is not None:
        command.extend(["--hnum", str(args.hnum)])
    if args.subjobs:
        command.append("-t")
    command.extend(args.job_ids)
    return command


def diagnostic(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = value or ""
    return value if len(value) <= 4096 else value[:4096] + "\n[truncated]"


def error_result(kind, message, command=None, **details):
    result = {"schema_version": SCHEMA_VERSION, "ok": False}
    if command is not None:
        result["command"] = command
    result["error"] = {"type": kind, "message": message, **details}
    return result


def query(args):
    command = build_command(args)
    env = dict(os.environ, LC_ALL="C")
    try:
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                 timeout=args.timeout, env=env, check=False)
    except subprocess.TimeoutExpired as exc:
        return error_result("timeout", "qstat exceeded {} seconds".format(args.timeout), command,
                            stdout=diagnostic(exc.stdout), stderr=diagnostic(exc.stderr))
    except FileNotFoundError:
        return error_result("command_not_found", "qstat is not on PATH; run in a Miyabi shell", command)
    except (OSError, UnicodeError) as exc:
        return error_result("command_error", str(exc), command)
    if process.returncode != 0:
        return error_result("command_error", "qstat failed", command, returncode=process.returncode,
                            stdout=diagnostic(process.stdout), stderr=diagnostic(process.stderr))
    try:
        jobs, notices = parse_qstat(process.stdout)
    except ParseError as exc:
        return error_result("parse_error", str(exc), command,
                            stdout=diagnostic(process.stdout), stderr=diagnostic(process.stderr))
    source_count = len(jobs)
    if args.status:
        statuses = {status.upper() for status in args.status}
        jobs = [job for job in jobs if job["status"] in statuses]
    result = {"schema_version": SCHEMA_VERSION, "ok": True, "command": command,
              "source_count": source_count, "count": len(jobs), "notices": notices}
    if args.summary:
        result["status_counts"] = dict(sorted(Counter(job["status"] for job in jobs).items()))
    else:
        result["jobs"] = [{field: job[field] for field in args.fields} for job in jobs] if args.fields else jobs
    if process.stderr.strip():
        result["stderr"] = diagnostic(process.stderr)
    return result


def main(argv=None):
    try:
        args = parse_args(argv)
    except UsageError as exc:
        result = error_result("invalid_arguments", str(exc))
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 2
    result = query(args)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False,
                     indent=2 if args.pretty else None,
                     separators=None if args.pretty else (",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
