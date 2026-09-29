# Open MPI Under PBS

These patterns target Miyabi-G/Open MPI. Miyabi-C's Intel MPI requires its own
launcher options and rank environment; do not copy the G recipe unchanged.

## Environment Propagation

Miyabi modules may set `OMPI_MCA_mca_base_env_list`; combining that mechanism
with `mpirun -x` can fail in the site stack. Keep module-provided MCA settings
and pass workload values as quoted arguments to `/usr/bin/env`:

```bash
MPI_ENV_ARGS=("PROJECT_ROOT=$PROJECT_ROOT" "PYTHON_BIN=$PYTHON_BIN")
# Add only the workload variables needed by every rank.
mpirun <placement-options> -np "$WORLD_SIZE" \
  /usr/bin/env "${MPI_ENV_ARGS[@]}" bash -lc '<rank-command>'
```

Do not add `-x` or construct a competing MCA environment list. Keep
scheduler-provided device visibility node-local: exporting rank 0's GPU UUIDs
to every host can select devices absent from other nodes. Inspect visibility in
each rank; if the launcher overwrites it, use a verified site launch mechanism.

## Placement And Rank Shells

- One MPI process per Python worker: match the slots in `$PBS_NODEFILE` and
  requested `mpiprocs`; [mpi-workers.pbs](../assets/pbs/mpi-workers.pbs) requires
  a homogeneous slot count per host.
- MPI → torchrun: request `mpiprocs=1` and one MPI supervisor per node;
  [torchrun.pbs](../assets/pbs/torchrun.pbs) creates the local workers itself.
- Use `--bind-to none` for supervisors that spawn workers unless the project
  defines CPU affinity. Choose a rendezvous host from the allocated nodes and
  distinct ports/IDs for concurrent groups. Job-derived defaults reduce
  collisions but do not guarantee a free port.

Inside each rank shell, set the project cwd and use the exported absolute
virtualenv `PYTHON_BIN` without resolving its symlink; see
[python-env.md](python-env.md#preserve-interpreter-identity). Record hostname,
ranks, effective interpreter/compiler and launcher exit status. A RUNNING PBS
job alone does not establish rank health.

`bash -lc` can overwrite parent `CC`/`CXX` through site initialization. For a
reproduced NVHPC incompatibility, pass the selected compiler as `RUNTIME_CC` or
`RUNTIME_CXX`, then apply it immediately before Python in the child:

```bash
[[ -z "${RUNTIME_CC:-}" ]] || export CC="$RUNTIME_CC"
[[ -z "${RUNTIME_CXX:-}" ]] || export CXX="$RUNTIME_CXX"
```

This is a conditional workaround. Verify the affected compile path through the
actual launcher, using a fresh task-local cache when a warm cache could hide the
failure. Do not delete shared caches or disable compilation as a default fix.
