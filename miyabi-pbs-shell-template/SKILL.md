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
asset. The examples depend on the installed `miyabi-pbs-cmd` context helper;
that reference explains the required paths, placeholders and checks.

Use [miyabi-pbs-cmd](../miyabi-pbs-cmd/SKILL.md) for resource queries, submission
and monitoring. Follow its execution boundaries and
[long-job wake-up rule](../miyabi-pbs-cmd/SKILL.md#long-jobs) when submitting.

## Workload References

Read only the details relevant to the shell being written:

- [Modules](references/module.md): discovery and shell initialization.
- [Python environments](references/python-env.md): architecture, interpreter
  identity, and environment setup inside the target allocation.
- [Storage and network](references/filesystem-network.md): scratch, quotas,
  network and containers.
- [Open MPI](references/mpi.md): placement and per-rank environment.
- [torchrun](references/torchrun-pbs.md) and
  [Accelerate](references/accelerate-pbs.md): distributed launch contracts.
- [vLLM](references/vllm-miyabi.md): device visibility and runtime configuration.
