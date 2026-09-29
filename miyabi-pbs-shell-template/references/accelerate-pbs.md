# Accelerate Under PBS

For direct `Accelerator()` use, adapt
[mpi-workers.pbs](../assets/pbs/mpi-workers.pbs) using the
[template setup](templates.md) and [Open MPI rules](mpi.md). Each MPI process
runs one Python worker and exports:

| Worker variable | Open MPI source |
| --- | --- |
| `RANK` | `OMPI_COMM_WORLD_RANK` |
| `WORLD_SIZE` | `OMPI_COMM_WORLD_SIZE` |
| `LOCAL_RANK` | `OMPI_COMM_WORLD_LOCAL_RANK` |
| `LOCAL_WORLD_SIZE` | `OMPI_COMM_WORLD_LOCAL_SIZE` |

The template also supplies a shared `MASTER_ADDR`/`MASTER_PORT`. Accelerate
uses this distributed environment; an `accelerate launch` configuration file
is not consumed by this direct Python invocation.

For `accelerate launch --multi_gpu`, run one launcher per machine and let it
spawn local workers, adapting the supervisor topology in
[torchrun-pbs.md](torchrun-pbs.md). Other backends, such as DeepSpeed, need their
own launch contract. Keep `--num_processes` (total workers),
`--num_machines`, `--machine_rank`, `--main_process_ip`, `--main_process_port`
and the config's distributed mode consistent with the allocation. Do not run
a full spawning launcher inside every MPI worker.

Check the pinned version's [Accelerate CLI](https://huggingface.co/docs/accelerate/package_reference/cli)
for supported flags, including configuration and mixed-precision settings.
