---
name: miyabi-pbs-shell-template
description: Write or adapt Miyabi PBS job shell scripts from single-node, torchrun and MPI-worker examples, with guidance for modules, Python environments, storage and distributed launch. Use when creating or fixing a qsub/.pbs script, or when torchrun, Accelerate, Open MPI or vLLM must run inside a Miyabi job.
---

# Miyabi PBS Shell Templates

Use an existing project script or adapt one of these examples to the project's
resources, modules, paths and workload arguments.

| Example | Use |
| --- | --- |
| [single-node.pbs](assets/pbs/single-node.pbs) | One process on Miyabi-G or Miyabi-C |
| [torchrun.pbs](assets/pbs/torchrun.pbs) | Miyabi-G Open MPI, one torchrun supervisor per node |
| [mpi-workers.pbs](assets/pbs/mpi-workers.pbs) | Miyabi-G Open MPI, one Python worker per rank |

Read [template setup and validation](references/templates.md) before adapting an
asset. The examples run the context guard `miyabi-pbs-cmd/scripts/context.py`
from the miyabi-toolbox checkout (the miyabi-pbs-cmd skill is deprecated; its
guard is not); that reference explains the required paths, placeholders and
checks.

Query queues, submit, monitor and wait with [miyabi-mcp](../miyabi-mcp/README.md):
`pbs_queues`, a `job_submit` dry run and then its commit, `job_get`, and `notify`
for long jobs. Tests, imports and other workloads run only inside a compute
allocation.

## Workload References

Read only the details relevant to the shell being written:

- [Modules](references/module.md): discovery and shell initialization.
- [Python environments](references/python-env.md): architecture, interpreter
  identity, and environment setup on the target system's login node.
- [Storage and network](references/filesystem-network.md): scratch, quotas,
  network and containers.
- [Open MPI](references/mpi.md): placement and per-rank environment.
- [torchrun](references/torchrun-pbs.md) and
  [Accelerate](references/accelerate-pbs.md): distributed launch contracts.
- [vLLM](references/vllm-miyabi.md): device visibility and runtime configuration.
