# Torchrun Under PBS

For code expecting PyTorch's `RANK`, `WORLD_SIZE`, `LOCAL_RANK`, `MASTER_ADDR`
and `MASTER_PORT`, adapt [torchrun.pbs](../assets/pbs/torchrun.pbs) using the
[template setup](templates.md) and [Open MPI rules](mpi.md).

The G/Open MPI topology is one MPI supervisor per node (`mpiprocs=1`), with
`torchrun` spawning `NPROC_PER_NODE` local workers. Choose that count from the
allocated GPUs and workload. The template invokes
`"$PYTHON_BIN" -m torch.distributed.run` so torchrun uses the selected environment.
It uses a c10d rendezvous endpoint on the first allocated host, with a job-derived
ID/port that can be overridden for concurrent groups.

The entrypoint must select its CUDA device from `LOCAL_RANK` before initializing
a GPU process group and use an appropriate backend (`nccl` for this G template).
Use torchrun's worker ranks for application logic; elastic rendezvous does not
promise they equal MPI supervisor ranks. A startup check should exercise the
actual collective communication path before scaling a changed launcher.

Use the project's actual entrypoint and argv. Check flags against the pinned
PyTorch version and [torchrun documentation](https://docs.pytorch.org/docs/stable/elastic/run.html).
