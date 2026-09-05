---
name: miyabi-development
description: >-
  Operate Miyabi safely from local, login, and PBS compute shells. Use for
  hostname and allocation classification, Environment Modules, qsub/qstat,
  storage, job diagnostics, Python environments, and Miyabi-specific GPU or
  distributed runtime setup. Keep login nodes control-plane only and load only
  the reference needed for the current operation.
---

# Miyabi Operations For Agents

Use this skill as an operating manual for Miyabi. It defines where commands may
run, how to work with PBS and Environment Modules, and which cluster-specific
reference to load. Project development policy belongs to the project
instructions, not this skill.

## Establish The Execution Context

Before issuing Miyabi-specific or runtime commands, run:

```bash
hostname
printf 'PBS_JOBID=%s\nPBS_NODEFILE=%s\n' "${PBS_JOBID:-}" "${PBS_NODEFILE:-}"
```

Classify the shell from both hostname and PBS evidence:

- **Local**: the hostname does not match a known Miyabi host. Work locally or
  use the user's established remote-access path; do not interpret local command
  output as Miyabi state.
- **Miyabi login/control plane**: the hostname matches `miyabi-g*` or
  `miyabi-c*`. Treat `interact-g*` as control-plane unless a compute allocation
  is confirmed.
- **Miyabi compute node**: the hostname matches `mg<number>` and `$PBS_JOBID`
  or `$PBS_NODEFILE` confirms an allocation.

Treat an `mg<number>` shell without PBS evidence, or any unknown hostname, as
untrusted. Inspect the active shell and project documentation before running
project code.

## Inspect Local And Project Rules

Prefer current evidence over generic examples:

- `AGENTS.md`, `README*`, `for_codex/`, environment files, and user notes;
- existing PBS scripts, recent job logs, module setup, and site documentation;
- `pyproject.toml`, `uv.lock`, `.python-version`, `.venv/`, and launcher
  configuration;
- live `module` and `qstat` output.

Do not hardcode project paths, groups, queues, modules, models, datasets, cache
directories, or resource limits unless the project, user, or current Miyabi
state provides them.

## Load Only The Relevant Reference

- Environment Modules discovery, loading, conflicts, or pager behavior: read
  [references/module.md](references/module.md).
- Queue/resource discovery, job status, history, or diagnostics: read
  [references/qstat.md](references/qstat.md).
- Interactive allocation, batch script construction, or job submission: read
  [references/qsub.md](references/qsub.md).
- Storage, quotas, job-local scratch, network, or containers: read
  [references/filesystem-network.md](references/filesystem-network.md).
- Python environment creation or dependency installation: read
  [references/python-env.md](references/python-env.md).
- Open MPI environment propagation and process placement: read
  [references/mpi.md](references/mpi.md).
- PyTorch distributed or `torchrun`: read
  [references/torchrun-pbs.md](references/torchrun-pbs.md).
- Direct `Accelerator()` use or `accelerate launch`: read
  [references/accelerate-pbs.md](references/accelerate-pbs.md).
- vLLM, LoRA inference, CUDA device visibility, Triton, or FlashInfer: read
  [references/vllm-miyabi.md](references/vllm-miyabi.md).

Load the smallest relevant combination. Do not copy framework-specific
procedures back into this entrypoint.

## Enforce Miyabi Safety Boundaries

On login/control-plane nodes, limit work to:

- reading and editing files;
- repository and configuration inspection;
- pager-safe module discovery;
- `qstat`, authorized `qsub`, and job-log inspection;
- shell syntax checks and other commands that do not execute the project
  runtime.

Run project code, heavy imports, dependency builds, preprocessing, model
loading, training, inference, distributed launchers, CUDA/NCCL operations, and
GPU profiling only in a confirmed PBS compute allocation.

Treat module state as shell-local. Load required modules again inside every
interactive or batch job shell and record `module list` there.

Treat job submission, cancellation, suspension, and release as external state
changes. Perform them only within the user's authorized scope, and confirm the
target job and requested resources immediately before the command.

Preserve existing user files and shared work. Do not overwrite tracked source,
delete jobs or artifacts, alter shell startup files, or change shared
infrastructure without explicit authorization.

## Choose Interactive Or Batch Execution

Use an interactive allocation when the agent needs a shell for short,
iterative commands or direct inspection of compute-node state. Keep the same
PTY session and allocation until that work is finished; do not request a new
allocation for each command.

Use a batch job for unattended or durable work, sustained resource use, or
multi-node execution that should continue without an attached terminal.
Request only the nodes, processes, memory, and walltime justified by the
workload and current queue limits.

After entering any allocation:

1. Confirm `hostname`, `$PBS_JOBID`, `$PBS_NODEFILE`, and the working directory.
2. Inspect and load modules in that shell.
3. Confirm the assigned resources and remaining walltime with `qstat`.
4. Run only the user-authorized workload.
5. Copy required outputs from job-local scratch to durable storage.
6. Stop background processes and exit cleanly when finished.

## Report Operations Precisely

Report the initial hostname and selected node class, commands that changed PBS
state, job IDs, requested resources, modules loaded in the job shell, relevant
logs or artifacts, exit status, and unresolved operational risks. Distinguish
observed command results from recommendations or work that was not run.
