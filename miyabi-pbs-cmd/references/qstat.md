# Structured PBS Queries

Use [qstat_json.py](../scripts/qstat_json.py) as the default interface for job
status and history. Use native commands only for queries the helper does not
cover or to diagnose a helper failure; see [Native Fallback](#native-fallback).

## JSON Job Queries

Run the helper in an established Miyabi shell with `qstat` on
`PATH`; it does not connect over SSH. Python 3.9+ and the standard library are
sufficient; no project environment or module loading is needed. Paths below
are relative to this skill directory; use its absolute path from another cwd.

```bash
# Current user's active jobs
/usr/bin/python3 -I scripts/qstat_json.py

# One active job (inside an allocation, use "$PBS_JOBID")
/usr/bin/python3 -I scripts/qstat_json.py 1234567

# A bounded history query with only the needed fields
/usr/bin/python3 -I scripts/qstat_json.py -H --hday 3 --hnum 10 \
  --fields job_id,job_name,status,elapsed_seconds

# Status counts within the queried history window
/usr/bin/python3 -I scripts/qstat_json.py -H --hday 7 --hnum 100 --summary

# Filter active jobs; repeat --status to include multiple states
/usr/bin/python3 -I scripts/qstat_json.py --status running --status queued

# Specific completed job, or array subjobs (quote array IDs)
/usr/bin/python3 -I scripts/qstat_json.py -H 1234567
/usr/bin/python3 -I scripts/qstat_json.py -t '1234567[]'
```

Each invocation makes one read-only query, with no polling or retries. History
defaults to 3 days and 100 jobs; `--hday` accepts 1–31. Filters
apply **after** the history window and row cap, so a filtered empty result
or a summary is not evidence about all historical jobs. Results retain the
source order. Choose a small `--hnum` for inspection, or an appropriate larger
cap when counting. `--summary` and `--fields` are mutually exclusive.

Stdout is one compact JSON document (`--pretty` adds indentation):

```json
{"schema_version":1,"ok":true,"command":["qstat","-ll"],"source_count":0,"count":0,"notices":[],"jobs":[]}
```

- `command` records the actual query. `source_count` is the number of parsed
  rows before local status filtering; `count` is the number after filtering.
- `jobs` contains records with the following fields; `--fields` projects each
  record onto a comma-separated subset. `--summary` replaces `jobs` with
  `status_counts`, a map from source status to count after filtering.
- `notices` preserves the scheduled-stop banner. Nonempty qstat stderr is
  retained as `stderr`, including on success.

| Field | JSON type | Meaning |
| --- | --- | --- |
| `job_id` | string | PBS identifier, including array syntax when present |
| `job_name` | string | Displayed name; `-ll` expands its width to 64 characters, but longer names can still be truncated |
| `status` | string | Source state such as `QUEUED`, `RUNNING`, `FINISH`, or `EXPIRED`; **FINISH does not establish success** |
| `project`, `queue` | string | Source project and queue |
| `start_date` | string or null | Source `MM/DD HH:MM:SS`; parentheses mean a predicted start. No year or timezone is inferred |
| `elapsed_seconds` | integer or null | Displayed ELAPSE converted to seconds, allowing hours above 24 |
| `token` | number or null | Displayed, rounded token usage; grouping commas removed |
| `nodes`, `mig` | integer or null | Requested node/MIG counts, not observed utilization |

Missing values such as `-` and `--:--:--` become `null`, not zero. The job table
does not supply an exit code. Use detailed job output and application logs to
decide whether a finished job succeeded.

Exit status is 0 on success, 1 for execution/parse failures, and 2 for invalid
arguments. Failures emit `ok:false` and `error.type`/`error.message`; command
and parse failures include available diagnostics, capped at 4096 characters
per stream. No partial job list is returned on failure. Unknown output fails
explicitly instead of being treated as zero jobs. The default command timeout
is 30 seconds, configurable with `--timeout`. Only `--help` emits plain text.

Keep `ok:false` and its diagnostics visible; never replace a failed query with
an empty list or silently treat fallback text as helper JSON. For a parse
failure, inspect the recorded `command` and diagnostic output before a targeted
native query. For `command_not_found`, enter the established Miyabi shell or
repair its command environment first. Do not retry timeouts in a tight loop.

## Interpret Results

Query only as often as the operation needs, stopping when the terminal job
state is observed or monitoring is no longer needed. For remaining walltime,
requested resources, scheduler comments or exit status, the job-list schema
is insufficient: use the detail fallback below. Distinguish requested limits
from measured usage, and compare a completed job's walltime with its own logs
when sizing a similar workload.

Treat a missing job or missing history as unknown until reconciled with the
available job logs and retained details. Submission time, scheduler start,
application-ready time and elapsed runtime are different events. A queued
child job has not yet tested the application; a RUNNING parent allocation is
not proof that every MPI rank/actor is healthy. Even `Exit_status=0` needs the
workload's expected outputs/readback before claiming application success.

## Native Fallback

These queries are **not implemented by the JSON helper**. Select only the
query needed; do not repeat routine job-list/history queries in raw form.

| Need | Native query |
| --- | --- |
| Queue/resource shapes and limits before submission | `qstat --rsc -x` |
| Project submission/execution limits | `qstat --limit` |
| Current resource-pool occupancy; not a start-time prediction | `qstat --rscuse` |
| Active job attributes, resource limits/usage, scheduler comment | `qstat -f <job_id>` |
| Retained completed-job attributes, including exit status | `qstat -H -f <job_id>` |
| Scheduler/server/accounting events | `tracejob <job_id>` |

For dialect or parser diagnosis, inspect `qstat --help` and the local manual
(observed on `miyabi-g1`: `/usr/local/share/man/man1/qstat.1`). Generic PBS
`-u`, `-Q`, and history `-x`/`-xf` are unsupported here; `--rsc -x` is supported.
Live site output takes precedence over copied limits. Parser validation and
fixture scope are recorded in [README.md](../README.md#validation).
