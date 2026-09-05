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
