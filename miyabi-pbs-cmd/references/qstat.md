# qstat Tips

Use this reference to discover current PBS resources and limits, inspect jobs,
or diagnose scheduler history. Prefer live output over copied queue tables.

## JSON Job Queries

Prefer [../scripts/qstat_json.py](../scripts/qstat_json.py) for current and
historical job lists. Run it in an established Miyabi shell with `qstat` on
`PATH`; it does not connect over SSH. Python 3.9+ and the standard library are
sufficient; no project environment or module loading is needed. Paths below
are relative to this skill directory; use its absolute path from another cwd.

```bash
# Current user's active jobs
/usr/bin/python3 scripts/qstat_json.py

# A bounded history query with only the needed fields
/usr/bin/python3 scripts/qstat_json.py -H --hday 3 --hnum 10 \
  --fields job_id,job_name,status,elapsed_seconds

# Status counts within the queried history window
/usr/bin/python3 scripts/qstat_json.py -H --hday 7 --hnum 100 --summary

# Filter active jobs; repeat --status to include multiple states
/usr/bin/python3 scripts/qstat_json.py --status running --status queued

# Specific completed job, or array subjobs (quote array IDs)
/usr/bin/python3 scripts/qstat_json.py -H 1234567
/usr/bin/python3 scripts/qstat_json.py -t '1234567[]'
```

The helper makes one read-only `qstat -ll` call, adding the requested `-H`,
`--hday`, `--hnum`, `-t`, and job IDs. It does not poll or retry. The native
history defaults remain 3 days and 100 jobs; `--hday` accepts 1–31. Filters
apply **after** qstat's history window and row cap, so a filtered empty result
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

This helper supports job tables only. Continue using native commands below
for queue limits, resource occupancy, and full job attributes.

## Discover Queues And Limits

```bash
qstat
qstat --rsc
qstat --limit
qstat --rscuse
```

- `qstat` lists the current user's queued and running jobs.
- `qstat --rsc` includes current node, walltime, memory, and project limits.
- `qstat --limit` shows current project submission and execution limits.
- `qstat --rscuse` shows current used and total nodes for each resource pool.

Inspect these values before choosing a queue or resource shape for `qsub`.
Use `qstat --rscuse` only as a current occupancy signal; it does not override
queue node ranges, walltime limits, scheduler priority, or project limits.

## Inspect A Job

Use the narrowest command that answers the question:

```bash
qstat
qstat <job_id>
qstat -f <job_id>
```

For the current interactive allocation:

```bash
qstat "$PBS_JOBID"
qstat -f "$PBS_JOBID" | grep -E \
  'Job Id|job_state|queue|exec_host|resources_used.walltime|Resource_List.walltime|Exit_status|comment'
```

Inspect requested resources separately from `resources_used`. A job ID,
hostname, or elapsed walltime in an old log does not describe the current
allocation.

`-f` requires a job ID; use `-H -f <job_id>` for retained completed-job details.
Do not use `-u` to filter jobs, `-Q` for queues, or `-x`/`-xf` for job history
on Miyabi. Use `--rsc -x` for queue limits; that scoped `-x` is supported.

## Inspect Completed Or Failed Jobs

```bash
qstat -H
qstat -H --hday <day>
qstat -H --hnum <num>
qstat -H -f <job_id>
tracejob <job_id>
```

Use `qstat -H` to display completed jobs. By default, up to 100 jobs completed
in the last 3 days are displayed. Use `qstat -H --hday <day>` to specify the
display period, or `qstat -H --hnum <num>` to specify the maximum number of
items. Use `tracejob` for scheduler, server, and accounting events. Preserve
the job ID and relevant PBS output alongside the application's own log when
diagnosing a failure. For a comparable completed job, use
`resources_used.walltime` and its log to inform a new walltime request while
retaining margin for runtime variation and cleanup.

Do not poll `qstat` in a tight loop. Poll only as often as the operation needs,
and stop when the terminal job state is observed or the user no longer needs
monitoring.

Treat a missing job or missing history as unknown until reconciled with the
available job logs and retained details. Submission time, scheduler start,
application-ready time and elapsed runtime are different events. A queued
child job has not yet tested the application; a RUNNING parent allocation is
not proof that every MPI rank/actor is healthy. Even `Exit_status=0` needs the
workload's expected outputs/readback before claiming application success.

`qdel`, `qhold`, and `qrls` change scheduler state. Confirm the exact job ID and
user authorization immediately before running them.

## Local Command Documentation And Validation

Miyabi supplies a customized qstat interface. Inspect `type -a qstat`,
`qstat --help`, and `man qstat` before assuming generic PBS flags apply. On
`miyabi-g1`, the local man source is `/usr/local/share/man/man1/qstat.1`;
read it directly if a pager or formatter is unavailable. Its column definitions
and live output were used for this parser. The site's
[Miyabi FAQ](https://www.cc.u-tokyo.ac.jp/en/faq/miyabi.php) also documents the
queue/resource queries and points to the full guide in the User Support Portal.

The helper was checked against live current-job, completed-job, and no-match
queries on `miyabi-g1`. Run the offline regressions with:

```bash
/usr/bin/python3 -m unittest discover -s tests -v
```

The history fixture preserves observed table spacing with anonymized job IDs,
names, and project values. Queued/array/MIG and malformed-output cases are
synthetic regression inputs, not evidence of live allocations.
