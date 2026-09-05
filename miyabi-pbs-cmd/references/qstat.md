# qstat Tips

Use this reference to discover current PBS resources and limits, inspect jobs,
or diagnose scheduler history. Prefer live output over copied queue tables.

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

`-f` is supported only for a running job ID. Do not use `-u` to filter jobs or
`-x`/`-xf` to query job history on Miyabi.

## Inspect Completed Or Failed Jobs

```bash
qstat -H
qstat -H --hday <day>
qstat -H --hnum <num>
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

`qdel`, `qhold`, and `qrls` change scheduler state. Confirm the exact job ID and
user authorization immediately before running them.
