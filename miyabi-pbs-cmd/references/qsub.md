# qsub Tips

Use this reference for interactive allocations, PBS batch scripts, and job
submission. Submission consumes shared resources and changes scheduler state;
run `qsub` only within the user's authorized scope.

## Inspect Current Site State

Before choosing a queue, node count, or walltime, read [qstat.md](qstat.md) and
run:

```bash
qstat --rsc -x
qstat --rscuse
```

The current Miyabi-G queue constraints are:

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
2. Request at least `00:10:00` so normal initialization has time to complete.
   Include enough margin for startup, runtime variation, output preservation,
   and orderly teardown.
3. Exclude every queue whose node range or maximum walltime cannot satisfy the
   request.
4. Use `qstat --rscuse` to compare current `Used/Total(Node)` values among the
   remaining queues. Prefer an eligible queue with enough free capacity; do
   not treat a recorded utilization snapshot as current state.
5. When eligible queues are otherwise comparable, Miyabi-G batch priority is
   usually `debug-g`, then `short-g`, then `regular-g`. Live scheduler state
   and project limits still determine when a job starts.

Re-run the live commands when the displayed limits differ from this table;
current Miyabi output is authoritative.

Derive the PBS group from the current account unless the project specifies one:

```bash
GROUP_ID="${GROUP_ID:-$(groups | tr ' ' '\n' | awk '/^xg/ {print; exit}')}"
GROUP_ID="${GROUP_ID:-$(groups | awk '{print $1}')}"
: "${GROUP_ID:?Set GROUP_ID explicitly}"
printf 'GROUP_ID=%s\n' "$GROUP_ID"
```

Use a literal group in `#PBS -W group_list=...`; PBS directives do not expand
shell variables.

## Request An Interactive Allocation

Start with the resource shape required by the workload. A typical one-node GPU
allocation is:

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
printf 'PBS_JOBID=%s\nPBS_NODEFILE=%s\n' "${PBS_JOBID:-}" "${PBS_NODEFILE:-}"
cd <project_root>
```

Keep the same terminal session while the allocation is needed. Load modules in
this shell, inspect remaining walltime with `qstat`, preserve required files,
stop background processes, and `exit` cleanly when finished.

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

Use project scripts as the primary template. A minimal PBS shape is:

```bash
#!/bin/bash
#PBS -q <queue>
#PBS -W group_list=<literal_group_id>
#PBS -l select=<nodes>:mpiprocs=<processes_per_node>
#PBS -l walltime=<HH:MM:SS>
#PBS -j oe

set -eEuo pipefail
trap 'echo "[ERROR] Failed at line $LINENO" >&2' ERR
export PAGER=cat MODULES_PAGER=cat LMOD_PAGER=cat

module load <required-module/version>
module list 2>&1 | cat

cd "${PBS_O_WORKDIR:?PBS_O_WORKDIR is not set}"
exec <workload-command>
```

Before submission:

1. Run `bash -n <script>` without executing the workload.
2. Replace every placeholder with a project- or site-supported value.
3. Confirm the queue, group, node/process count, memory, walltime, paths, and
   expected output location.
4. Confirm required modules are loaded in the job body, not only in the login
   shell.
5. Require `$PBS_NODEFILE` when the launcher depends on it.
6. Confirm the exact script and resource request immediately before `qsub`.

Submit and record the returned job ID:

```bash
qsub <script>
```

Use command-line resource overrides only when their precedence and effect are
intentional. Do not request a shorter walltime than startup, execution, output
preservation, and orderly teardown can fit.

For MPI-based jobs, read [mpi.md](mpi.md) and the relevant framework reference
before constructing the launcher.
