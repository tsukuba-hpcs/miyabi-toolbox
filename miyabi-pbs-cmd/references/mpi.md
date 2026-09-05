# Open MPI Tips For PBS Jobs

Use this reference when a Miyabi PBS job launches work with `mpirun`.

## Propagate Environment Variables Once

Environment Modules may populate `OMPI_MCA_mca_base_env_list`. Open MPI can
abort when that mechanism is combined with `mpirun -x VAR`. Prefer one explicit
`/usr/bin/env` argument list:

```bash
MPI_ENV_ARGS=(
  "MASTER_ADDR=$MASTER_ADDR"
  "MASTER_PORT=$MASTER_PORT"
  "PROJECT_ROOT=$PROJECT_ROOT"
)

mpirun \
  --mca mpi_abort_print_stack 1 \
  --report-bindings \
  --bind-to none \
  -np "$WORLD_SIZE" \
  /usr/bin/env "${MPI_ENV_ARGS[@]}" \
  bash -lc '<launcher-command>'
```

Do not combine this pattern with `mpirun -x` or another MCA environment list.
Include only values every MPI child should receive. Preserve scheduler-provided
node-local values such as GPU visibility unless the launcher has an explicit,
correct mapping.

## Match Process Placement To The Launcher

- For one MPI process per worker, derive `WORLD_SIZE` from `$PBS_NODEFILE` and
  match it to the requested `mpiprocs` shape.
- For `mpirun -> torchrun`, use one MPI supervisor per node; `torchrun` creates
  the local workers.
- Do not bind an MPI supervisor to one core when it spawns multiple local
  workers unless the workload deliberately defines that affinity.
- Derive the rendezvous host from the allocated node list and use a
  job-specific rendezvous ID or port when concurrent jobs could collide.

Print hostnames, ranks, world size, rendezvous address, and binding information
before the workload starts. Follow an existing Miyabi project launcher when it
imposes stricter placement or environment rules.

## Preserve The Runtime In The Actual Rank Shell

Pass an absolute **virtualenv** `PYTHON_BIN` without resolving the interpreter
symlink. Set `cd "$PROJECT_ROOT"` inside the rank shell and export the same
interpreter for any nested tools. Do not infer child environment correctness
from a successful parent-side import; check the actual launcher path.

`bash -lc` can reload modules/site defaults and replace `CC`, even when the MPI
parent already exported a different compiler. If an observed Triton/Inductor
build fails under the module-provided `nvc`, diagnose it in a small target
allocation. If the pinned stack succeeds with GNU, pass the selected path under
a separate name such as `RUNTIME_CC`, then export `CC="$RUNTIME_CC"` **inside**
the login child shell immediately before Python. Apply `RUNTIME_CXX` similarly
when the failure involves C++. Inspect the effective values per rank.

This is a symptom-dependent compiler override, not a universal switch away
from the site compiler. A warm compiler cache can hide the failure; verify the
affected compile path with a fresh task-local cache and the same shell/launcher
before resubmission. Do not delete shared caches or disable compilation across
all workloads as a default workaround.

Create shared log/publication parents before MPI starts. A co-allocated PBS
job can remain RUNNING after one actor has failed, so check launcher exit and
rank/process results independently of the parent job state. For several
independent actor jobs, account for scheduler startup and project limits before
deciding that a missing application-ready signal means runtime failure.

Sources and scope: [failure-lessons.md](failure-lessons.md).
