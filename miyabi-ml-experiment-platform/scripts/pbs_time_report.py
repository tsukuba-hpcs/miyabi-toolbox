#!/usr/bin/env python3
"""Where the time went: queue wait, run time, walltime use and GPU use of finished Miyabi PBS jobs.

    pbs_time_report.py [--hday N] [--hnum M] [--name PREFIX] [--text] [--jobs]
    pbs_time_report.py [options] JOB_ID [JOB_ID ...]
    pbs_time_report.py [options] --from-file SAVED_QSTAT_H_F_OUTPUT

Without job IDs it lists the history window (``qstat -H -ll --hday N --hnum M``)
and reads each job's full record (``qstat -H -f``, 40 jobs per call, about
10 s per call), so narrow it with ``--name`` and ``--hday``. It only queries,
so it may run on a login node with the host Python 3.9+ standard library.
History reaches back as far as the site keeps it (31 days at most).

Jobs are grouped by the queue they were submitted to (``-q`` in their submit
arguments; the site routes regular-g to small-g and so on) and their node
count. Per group and overall:

- jobs, never started (deleted or expired while queued), non-zero exits;
- queue wait (start - queue time) median, p90 and max, in hours;
- run time median, p90 and max, in hours;
- used / requested walltime p10, median and p90;
- node-hours used and requested by the jobs that started;
- GPU utilization: the per-node averages PBS records, weighted by node time.
  PBS seems to average utilization and power over only about the last hour
  of a job (inferred: in an 11-node job, eight worker GPUs that trained for
  the first 29 min of 112, at 31 GB, record a maximum utilization of 0;
  memory peaks do cover the whole job). For longer jobs it shows how the job
  ended, not its average: count idle nodes from task timelines instead;
- hours that never-started jobs spent queued before they were removed
  (last modification - queue time): queue positions thrown away.

Output is one JSON document with ``ok``; ``--text`` prints a table instead
and ``--jobs`` adds one row per job. Exit 0 on success, 1 when a query or
parse fails, 2 for invalid arguments.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import subprocess
import sys
from datetime import datetime

BATCH = 40
LISTED_JOB = re.compile(r"^(\d+)(?:\.\S+)?\s")
TIME_FORMAT = "%a %b %d %H:%M:%S %Y"
GIB = 2 ** 30


class QueryError(RuntimeError):
    pass


def query(argv: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        reply = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QueryError(f"{' '.join(argv[:4])}: {error}") from error
    return reply.returncode, reply.stdout, reply.stderr


def history_ids(hday: int, hnum: int, name: str | None, timeout: int) -> list[str]:
    """Job numbers in the history window whose name starts with ``name``; array jobs are skipped.

    The name filter runs on the wide listing (``-ll``, names up to 64 characters)
    because each full record costs about a quarter of a second to fetch."""
    code, out, err = query(["qstat", "-H", "-ll", "--hday", str(hday), "--hnum", str(hnum)], timeout)
    if code != 0:
        raise QueryError(f"qstat -H exited {code}: {err.strip()[-500:]}")
    found = []
    for line in out.splitlines():
        match = LISTED_JOB.match(line)
        words = line.split()
        if match and (not name or (len(words) > 1 and words[1].startswith(name))):
            found.append(match.group(1))
    return found


def parse_records(text: str) -> dict[str, dict[str, str]]:
    """``qstat -f`` blocks keyed by job number. A value wrapped onto lines that
    start with a tab is joined back first."""
    text = text.replace("\n\t", "")
    records = {}
    for block in re.split(r"^Job Id:\s*", text, flags=re.MULTILINE)[1:]:
        job_id = block.split(None, 1)[0]
        fields = dict(re.findall(r"^    ([A-Za-z_][\w.]*) = (.*)$", block, re.MULTILINE))
        records[job_id.split(".", 1)[0]] = fields
    return records


def fetch_records(job_ids: list[str], timeout: int) -> dict[str, dict[str, str]]:
    records = {}
    for start in range(0, len(job_ids), BATCH):
        batch = job_ids[start:start + BATCH]
        code, out, err = query(["qstat", "-H", "-f", *batch], timeout)
        found = parse_records(out)
        # One unknown ID can make qstat exit non-zero; keep what it did print.
        if code != 0 and not found:
            raise QueryError(f"qstat -H -f exited {code}: {err.strip()[-500:]}")
        records.update(found)
    return records


def seconds(value: str | None) -> int | None:
    if not value or not re.fullmatch(r"\d+:\d{2}:\d{2}", value.strip()):
        return None
    hours, minutes, secs = (int(part) for part in value.strip().split(":"))
    return hours * 3600 + minutes * 60 + secs


def moment(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), TIME_FORMAT) if value else None
    except ValueError:
        return None


def per_node(value: str | None) -> dict[str, float]:
    """A PBS per-node map such as ``'{"mg0297": "20.0", ...}'``."""
    if not value:
        return {}
    try:
        return {host: float(number) for host, number in json.loads(value.strip().strip("'")).items()}
    except (ValueError, TypeError, AttributeError):
        return {}


def number(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "", "-") else None
    except ValueError:
        return None


def submitted_queue(fields: dict) -> str | None:
    arguments = fields.get("Submit_arguments", "")
    try:
        words = shlex.split(arguments)
    except ValueError:
        words = arguments.split()
    for flag, value in zip(words, words[1:]):
        if flag == "-q":
            return value
    return fields.get("queue")


def job_row(job: str, fields: dict) -> dict:
    nodes = int(number(fields.get("Resource_List.nodect")) or 1)
    queued, started = moment(fields.get("qtime")), moment(fields.get("stime"))
    modified = moment(fields.get("mtime"))
    run_s = seconds(fields.get("resources_used.walltime")) if started else None
    request_s = seconds(fields.get("Resource_List.walltime"))
    utilization = per_node(fields.get("resources_used.gpu_avg_util_per_nodes"))
    memory = per_node(fields.get("resources_used.gpu_max_memory_per_nodes"))
    exit_status = number(fields.get("Exit_status"))
    return {
        "job_id": job, "name": fields.get("Job_Name"), "state": fields.get("job_state"),
        "queue": submitted_queue(fields), "ran_in": fields.get("queue"), "nodes": nodes,
        "queued_at": queued.isoformat() if queued else None,
        "started_at": started.isoformat() if started else None,
        "wait_s": (started - queued).total_seconds() if started and queued else None,
        "abandoned_wait_s": (modified - queued).total_seconds() if not started and modified and queued else None,
        "run_s": run_s, "request_s": request_s,
        "used_ratio": round(run_s / request_s, 3) if run_s is not None and request_s else None,
        "gpu_util_mean": round(sum(utilization.values()) / len(utilization), 1) if utilization else None,
        "gpu_util_min": min(utilization.values()) if utilization else None,
        "gpu_mem_max_gib": round(max(memory.values()) / GIB, 1) if memory else None,
        "exit_status": int(exit_status) if exit_status is not None else None,
        "token": number(fields.get("TOKEN")),
    }


def percentile(values: list[float], fraction: float) -> float | None:
    """Linear interpolation between the closest ranks."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def hours(value: float | None) -> float | None:
    return round(value / 3600, 2) if value is not None else None


def spread(values: list[float], scale=hours) -> dict:
    return {"median": scale(percentile(values, 0.5)), "p90": scale(percentile(values, 0.9)),
            "max": scale(max(values)) if values else None}


def summarize(rows: list[dict]) -> dict:
    started = [row for row in rows if row["started_at"]]
    waits = [row["wait_s"] for row in started if row["wait_s"] is not None]
    runs = [row["run_s"] for row in started if row["run_s"] is not None]
    ratios = [row["used_ratio"] for row in started if row["used_ratio"] is not None]
    timed = [row for row in started if row["gpu_util_mean"] is not None and row["run_s"]]
    weight = sum(row["nodes"] * row["run_s"] for row in timed)
    ratio = lambda value: round(value, 2) if value is not None else None  # noqa: E731
    return {
        "jobs": len(rows), "never_started": len(rows) - len(started),
        "nonzero_exit": sum(1 for row in started if row["exit_status"] not in (0, None)),
        "wait_h": spread(waits), "run_h": spread(runs),
        "used_ratio": {"p10": ratio(percentile(ratios, 0.1)), "median": ratio(percentile(ratios, 0.5)),
                       "p90": ratio(percentile(ratios, 0.9))},
        "node_h_used": round(sum(row["nodes"] * row["run_s"] for row in started if row["run_s"]) / 3600, 1),
        "node_h_requested": round(sum(row["nodes"] * row["request_s"] for row in started
                                      if row["request_s"]) / 3600, 1),
        "gpu_util_pct": round(sum(row["gpu_util_mean"] * row["nodes"] * row["run_s"] for row in timed) / weight, 1)
        if weight else None,
        "abandoned_wait_h": round(sum(row["abandoned_wait_s"] or 0 for row in rows) / 3600, 1),
    }


def report(records: dict[str, dict[str, str]], name: str | None, missing: list[str]) -> dict:
    rows = [job_row(job, fields) for job, fields in records.items()
            if fields.get("job_state", "F") == "F" and (not name or (fields.get("Job_Name") or "").startswith(name))]
    groups = {}
    for row in rows:
        groups.setdefault((row["queue"], row["nodes"]), []).append(row)
    ordered = sorted(groups.items(), key=lambda item: (-item[0][1], str(item[0][0])))
    overall = summarize(rows)
    wait, run = overall["wait_h"]["median"], overall["run_h"]["median"]
    overall["median_wait_over_run"] = round(wait / run, 1) if wait is not None and run else None
    return {"ok": True, "jobs_without_record": missing, "overall": overall,
            "groups": [dict(queue=queue, nodes=nodes, **summarize(members)) for (queue, nodes), members in ordered],
            "rows": sorted(rows, key=lambda row: row["queued_at"] or "")}


def text_table(document: dict) -> str:
    def cell(value, width):
        return f"{'-' if value is None else value:>{width}}"

    header = (f"{'queue':<10} {'nodes':>5} {'jobs':>5} {'never':>5} {'wait_med':>8} {'wait_p90':>8} {'wait_max':>8} "
              f"{'run_med':>7} {'used/req':>8} {'node_h':>8} {'req_h':>8} {'gpu%':>5}")
    lines = [header]
    for group in [*document["groups"], dict(queue="ALL", nodes=None, **document["overall"])]:
        lines.append(" ".join([f"{group['queue'] or '?':<10}", cell(group["nodes"], 5), cell(group["jobs"], 5),
                               cell(group["never_started"], 5), cell(group["wait_h"]["median"], 8),
                               cell(group["wait_h"]["p90"], 8), cell(group["wait_h"]["max"], 8),
                               cell(group["run_h"]["median"], 7), cell(group["used_ratio"]["median"], 8),
                               cell(group["node_h_used"], 8), cell(group["node_h_requested"], 8),
                               cell(group["gpu_util_pct"], 5)]))
    overall = document["overall"]
    lines.append(f"hours in h; median wait / median run = {overall['median_wait_over_run']}; "
                 f"never-started jobs sat queued {overall['abandoned_wait_h']} h in total; "
                 f"non-zero exits {overall['nonzero_exit']}")
    lines.append("gpu%: PBS's per-node average, which seems to cover only about the last hour of each job")
    if document["jobs_without_record"]:
        lines.append(f"no history record for {len(document['jobs_without_record'])} job(s)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job_ids", nargs="*", help="job numbers; default: the history window")
    parser.add_argument("--hday", type=int, default=7, choices=range(1, 32), metavar="1-31")
    parser.add_argument("--hnum", type=int, default=500)
    parser.add_argument("--name", help="keep jobs whose name (as listed, at most 64 characters) starts with this")
    parser.add_argument("--from-file", help="parse saved `qstat -H -f` output instead of querying")
    parser.add_argument("--jobs", action="store_true", help="include one row per job")
    parser.add_argument("--text", action="store_true", help="print a table instead of JSON")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    try:
        if args.from_file:
            with open(args.from_file) as stream:
                records, missing = parse_records(stream.read()), []
        else:
            wanted = ([job.split(".", 1)[0] for job in args.job_ids]
                      or history_ids(args.hday, args.hnum, args.name, args.timeout))
            records = fetch_records(wanted, args.timeout)
            missing = [job for job in wanted if job not in records]
        document = report(records, args.name, missing)
    except (QueryError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    if not args.jobs:
        document.pop("rows")
    print(text_table(document) if args.text else json.dumps(document, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
