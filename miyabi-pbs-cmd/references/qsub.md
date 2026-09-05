# qsub Tips

Use this reference for interactive allocations, PBS batch scripts, and job
submission. Submission consumes shared resources and changes scheduler state;
run `qsub` only within the user's authorized scope.

## Inspect Current Site State

Before choosing a queue, node count, or walltime, read [qstat.md](qstat.md) and
run:

```bash
qstat --rsc -x
qstat --limit
qstat --rscuse
```

The following Miyabi-G reference was checked with live `qstat --rsc -x` on
2026-09-05. Read current limits before choosing resources:

| Submit with | Nodes | Maximum walltime | Scheduler destination |
| --- | ---: | ---: | --- |
| `-q interact-g` | 1 | `02:00:00` | `interact-g_n1` |
| `-q interact-g` | 2–8 | `00:10:00` | `interact-g_n8` |
| `-q debug-g` | 1–16 | `00:30:00` | `debug-g` |
| `-q short-g` | 1–8 | `08:00:00` | `short-g` |
| `-q regular-g` | 1–16 | `48:00:00` | `small-g` |
| `-q regular-g` | 17–64 | `48:00:00` | `medium-g` |
| `-q regular-g` | 65–128 | `48:00:00` | `large-g` |
| `-q regular-g` | 129–256 | `24:00:00` | `x-large-g` |

Submit only to the parent queues shown in the first column. PBS assigns
`small-g`, `medium-g`, `large-g`, `x-large-g`, `interact-g_n1`, or
`interact-g_n8`; never pass those destination names to `qsub -q`.

Choose the queue and walltime in this order:

1. Determine the required node count and estimate elapsed time from the actual
   workload and comparable completed jobs. Inspect `resources_used.walltime`
   and the corresponding application logs rather than copying an unrelated
   request.
2. Include margin for initialization, runtime variation, output preservation,
   and orderly teardown. Ten minutes is a useful initial estimate for some
   ML checks, not a mandatory minimum; use measured task costs and queue limits.
3. Exclude every queue whose node range or maximum walltime cannot satisfy the
   request.
4. Use `qstat --rscuse` to compare current `Used/Total(Node)` values among the
   remaining queues. Prefer an eligible queue with enough free capacity; do
   not treat a recorded utilization snapshot as current state.
5. Prefer an eligible debug/short queue for a genuinely short batch workload.
   Occupancy alone does not predict start time: inspect project job/node limits
   and scheduler comments too. Account for both supervisors and their child jobs
   before running several experiments concurrently.

Re-run the live commands when the displayed limits differ from this table;
current Miyabi output is authoritative.

Use the project/user's PBS group. If none is specified, compare `groups` with
the projects shown by `qstat --rsc -x`/`--limit`; infer it only when there is one
eligible choice. Multiple valid groups require resolving the intended allocation
or accounting project, not choosing the first account group.

```bash
: "${GROUP_ID:?Set the project/user-selected PBS group}"
printf 'GROUP_ID=%s\n' "$GROUP_ID"
```

Use a literal group in `#PBS -W group_list=...`; PBS directives do not expand
shell variables.

## Request An Interactive Allocation

Select the target from the workload, not the login architecture. A typical
one-node Miyabi-G allocation is:

```bash
qsub -I \
  -q interact-g \
  -W group_list="$GROUP_ID" \
  -l select=1 \
  -l walltime=00:30:00
```

After the prompt changes, confirm that the shell is an allocated compute node:

```bash
hostname
uname -m
printf 'PBS_JOBID=%s\nPBS_NODEFILE=%s\n' "${PBS_JOBID:-}" "${PBS_NODEFILE:-}"
cd <project_root>
```

Keep the same terminal session while the allocation is needed. Load modules in
this shell, inspect remaining walltime with `qstat`, preserve required files,
stop background processes, and `exit` cleanly when finished.
Follow [validation.md](validation.md) to keep failed check commands from closing
the persistent shell and to renew the allocation within the authorized scope.

Use `interact-g` for G interactive requests; a recorded `qsub -I -q regular-g`
request was rejected. For a C target, the live 2026-09-05 resource view lists
`interact-c` (one node up to two hours, two nodes up to ten minutes); recheck it
and choose the matching x86_64 environment. Do not copy G module/MPI settings
into a C job without checking the target stack.

Miyabi's established `interact-g` request uses `select=1` and explicit
`-W group_list=...`. Recorded attempts with `select=1:ngpus=1`, a separate
`-l ngpus=1`, or a missing group were rejected before allocation. The queue
supplies its GPU resource; do not transplant generic PBS GPU flags. Consult
the local qsub manual and live queue definition for other resource shapes.

For a multi-node interactive allocation, request between 2 and 8 nodes only
when the operation requires them. Its walltime cannot exceed 10 minutes:

```bash
qsub -I \
  -q interact-g \
  -W group_list="$GROUP_ID" \
  -l select=2:mpiprocs=1 \
  -l walltime=00:10:00
```

## Prepare A Batch Script

Use project scripts as the primary template, or adapt
[single-node.pbs](../assets/pbs/single-node.pbs),
[torchrun.pbs](../assets/pbs/torchrun.pbs), or
[mpi-workers.pbs](../assets/pbs/mpi-workers.pbs). Their setup and validation
status are described in [templates.md](templates.md).

Before submission:

1. Run `bash -n <script>` without executing the workload.
2. Replace every placeholder with a project- or site-supported value.
3. Confirm the queue, group, node/process count, memory, walltime, paths, and
   expected output location.
4. Confirm required modules are loaded in the job body, not only in the login
   shell.
5. Check target architecture, PBS nodefile, and the absolute virtualenv Python
   path. Preserve interpreter symlinks; propagate it to nested launchers.
6. Confirm the exact script and resource request immediately before `qsub`.

Submit and record the returned job ID:

```bash
qsub <script>
```

Use command-line resource overrides only when their precedence and effect are
intentional. Do not request a shorter walltime than startup, execution, output
preservation, and orderly teardown can fit.

Keep dependency-complete preflight in compute; submit wrappers should only do
control-plane-compatible preparation and the authorized qsub. Retain qsub's
exit status, stdout and stderr. After an uncertain submission response, reconcile
the job before retrying, to avoid duplicate jobs. A rejected resource request
requires a corrected request, not an unchanged retry. Do not assume users may
move a queued job with `qalter -q`; inspect the rejection and change scheduling
only within the authorized scope.

Use `type -a qsub`, `qsub --version`, and the local manual
`/usr/local/share/man/man1/qsub.1` to identify the command and dialect.
Unlike `qstat --help`, `qsub --help` was rejected on the inspected login host
(2026-09-05); its generic usage output is not a list of site-approved resources.
Incident provenance: [failure-lessons.md](failure-lessons.md).

For MPI-based jobs, read [mpi.md](mpi.md) and the relevant framework reference
before constructing the launcher.
