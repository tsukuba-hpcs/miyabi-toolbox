# Hugging Face Accelerate PBS Pattern

Use this for code that creates `Accelerator()` directly and lets Accelerate
read rank information from environment variables. The Miyabi pattern is
`mpirun -np WORLD_SIZE`; each MPI process exports `RANK`, `WORLD_SIZE`,
`LOCAL_RANK`, and `LOCAL_WORLD_SIZE` from Open MPI variables before running the
Python module.

## Contents

- [PBS Template](#pbs-template)
- [Python Entrypoint Expectations](#python-entrypoint-expectations)
- [Notes](#notes)

## PBS Template

Select the project/user PBS group as described in [qsub.md](qsub.md); fill it
as a literal because PBS directives do not expand shell variables.

Copy [../assets/pbs/mpi-workers.pbs](../assets/pbs/mpi-workers.pbs)
and follow [templates.md](templates.md) to set the target, paths, modules and
resource shape. That file is the maintained script; this reference explains
the launcher contract.


## Python Entrypoint Expectations

```python
from accelerate import Accelerator

def train(*args, **kwargs):
    accelerator = Accelerator()
    world_size = accelerator.num_processes

    if accelerator.is_main_process:
        print(f"world_size={world_size}")

    # Build model, optimizer, dataloaders, then use accelerator.prepare(...)
    # or a Trainer/SFTTrainer stack that integrates with Accelerate.

    accelerator.wait_for_everyone()
    accelerator.end_training()
```

## Notes

- Discover exact modules through the JSON queries in [module.md](module.md),
  consulting catalog/help text when needed. Replace or remove every module
  placeholder before submission. Keep `REQUIRED_MODULES=()` only when the job
  intentionally uses the compute-node defaults, and still log `module list`.
- This template is for direct `Accelerator()` use under `mpirun`; rank discovery
  comes from the exported environment, not an Accelerate launcher config.
- If the project standardizes on `accelerate launch`, use official launcher
  flags (`--config_file`, `--num_processes`, `--num_machines`,
  `--machine_rank`, `--main_process_ip`, `--main_process_port`) and still run
  only from compute nodes or PBS jobs.
- Keep any Accelerate launcher config consistent with the allocation:
  `distributed_type`, `mixed_precision`, `num_machines`, and `num_processes`
  should match or be intentionally overridden.
- Derive a job-specific default port from `$PBS_JOBID`, but allow
  `MASTER_PORT` to override it when project policy reserves a port range.
- Re-check launcher flags against the current
  [Accelerate CLI documentation](https://huggingface.co/docs/accelerate/package_reference/cli).
