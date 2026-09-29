# PBS Submission And Interactive Sessions

## Select Resources From Current Site State

Use [qstat's resource, limit and occupancy queries](qstat.md#native-fallback)
before selecting a queue or resource shape. Check node/MIG counts, memory,
maximum and remaining walltime, and project limits. Size walltime from comparable
job usage and application logs, including startup and output preservation.
Occupancy is useful context, not a prediction of start time.

Miyabi uses parent routing queues: submit to `regular-g`/`regular-c` or
`interact-g`/`interact-c`, not their indented destinations such as `small-g` or
`interact-g_n1`. `debug-*` and `short-*` are options for eligible short batch
work. Available queues and limits vary by project; use live output instead of a
copied capacity table. Include supervisor jobs when checking concurrency limits.

Use the project/user's PBS group. If unspecified, compare `groups` with the
eligible projects in `qstat --limit`/`--rsc -x`; infer it only when exactly one
choice fits. Resolve ambiguous accounting projects before submission.

The installed `/usr/local/share/man/man1/qsub.1` documents required `-q`,
`-W group_list=...` and `-l select=N`, with chunk resources such as `mpiprocs`,
`ompthreads` and `mem`. Do not transplant generic `ngpus` flags: Miyabi selects
GPU/MIG resources through the site queue and select shape. Use `type -a qsub`
and `qsub --version` for command diagnosis.

## Interactive Work

Use a PTY and keep its session ID so later commands reach the same allocation.
For a one-node G session, after choosing the accounting group and walltime:

```bash
qsub -I -q interact-g -W group_list="${GROUP_ID:?Set the PBS group}" \
  -l select=1 -l walltime=00:30:00
```

For C, select `interact-c` and the C environment. The 2026-09-05 live view
permits one node for up to two hours on either system; multi-node interactive
limits are shorter (G: 2–8 nodes, C: 2 nodes, up to ten minutes). Recheck before
requesting a different shape; use `select=N:mpiprocs=P` when placement needs it.

After the compute prompt appears, follow [context and module setup](../SKILL.md).
Set `SKILL_ROOT` in that shell and check the actual allocation:

```bash
/usr/bin/python3 -I "$SKILL_ROOT/scripts/context.py" \
  --target-system Miyabi-G --require-compute
/usr/bin/python3 -I "$SKILL_ROOT/scripts/qstat_json.py" "${PBS_JOBID:?}"
```

Use the actual target in the guard and require success before project execution.
Inspect remaining walltime with [job details](qstat.md#native-fallback).
To keep a failed check from closing the persistent shell, run checks in a child:

```bash
# Set CHECK_SCRIPT and a durable LOG_ROOT for this attempt.
export CHECK_SCRIPT LOG_ROOT
mkdir -p "$LOG_ROOT"
bash -o pipefail -c 'bash "$CHECK_SCRIPT" 2>&1 | tee "$LOG_ROOT/validation.log"'
```

The check script should stop on required failures; inspect the returned status.
When time is insufficient, preserve outputs and continue in another allocation
only within existing resource/count authorization. After exit or disconnection,
recheck host context; a lost allocation does not permit login-side runtime.

## Batch Work

Use an existing project script or fill a [PBS template](templates.md). Bind
queued experiments to the intended stable code/configuration snapshot when the
project requires reproducibility. A submitted script is copied by PBS; referenced
source and environment files still come from their paths when the job runs.

Before `qsub <script>`, run `bash -n <script>`, inspect the final placeholders,
queue, group, resources, paths, module setup and outputs. PBS directives require
literal values; shell variables are not expanded. Keep dependency-complete
preflight in compute; a submission wrapper should only inspect parameters/files
and perform the authorized submission.

Retain qsub's exit status, stdout/stderr and returned job ID. Monitor that ID with
the JSON helper. If submission has an uncertain result, reconcile current/history
jobs and logs before retrying to avoid duplicates. Correct a rejected request
before retrying; changing or cancelling jobs remains subject to the authorized
scope. Interpret completion with [job results](qstat.md#interpret-results).
