# PyTorch Torchrun PBS Pattern

Use this for code that calls `torch.distributed.init_process_group(...)` and expects `torchrun` environment variables such as `RANK`, `WORLD_SIZE`, `LOCAL_RANK`, `MASTER_ADDR`, and `MASTER_PORT`.

PBS provides `$PBS_NODEFILE`; compute `MASTER_ADDR` from its first host. Run
one MPI supervisor per node; each supervisor starts `torchrun`, which spawns
`NPROC_PER_NODE` local workers. Pass variables to MPI children with
`/usr/bin/env "${MPI_ENV_ARGS[@]}"`, not `mpirun -x`, and derive `node_rank`
inside the MPI child shell from `OMPI_COMM_WORLD_RANK`.

## Contents

- [PBS Template](#pbs-template)
- [Python Entrypoint Expectations](#python-entrypoint-expectations)
- [Notes](#notes)

## PBS Template

Select the project/user PBS group as described in [qsub.md](qsub.md); fill it
as a literal because PBS directives do not expand shell variables.

Copy [../assets/pbs/torchrun.pbs](../assets/pbs/torchrun.pbs)
and follow [templates.md](templates.md) to set the target, paths, modules and
resource shape. That file is the maintained script; this reference explains
the launcher contract.


## Python Entrypoint Expectations

```python
import datetime
import os
import torch
import torch.distributed as dist

local_rank = int(os.environ.get("LOCAL_RANK", "0"))
backend = os.environ.get("DIST_BACKEND", "nccl")
if torch.cuda.is_available():
    torch.cuda.set_device(local_rank)

dist.init_process_group(
    backend=backend,
    timeout=datetime.timedelta(seconds=300),
)
print(
    f"world={dist.get_world_size()} rank={dist.get_rank()} "
    f"local_rank={local_rank} host={os.uname().nodename}",
    flush=True,
)

dist.destroy_process_group()
```

## Notes

- Discover exact modules with `module avail`, `show_module`, and `module help`
  as described in [module.md](module.md). Replace or remove every module
  placeholder before submission. Keep `REQUIRED_MODULES=()` only when the job
  intentionally uses the compute-node defaults, and still log `module list`.
- Keep `#PBS -l select=<num_nodes>:mpiprocs=1` for the `mpirun -> torchrun` pattern. `torchrun`, not MPI, creates local worker processes.
- Set `NPROC_PER_NODE` to the number of local GPU workers each `torchrun` should spawn.
- Keep the MPI supervisor unbound unless the project defines an explicit CPU
  affinity plan; child workers can inherit an overly narrow supervisor
  affinity.
- Give every concurrent worker group a distinct `RDZV_ID` and non-conflicting
  rendezvous port. `$PBS_JOBID` is a suitable default rendezvous id and input
  for a job-specific port; override `MASTER_PORT` if site policy requires it.
- Re-check rendezvous flags against the current
  [PyTorch torchrun documentation](https://docs.pytorch.org/docs/stable/elastic/run.html).
