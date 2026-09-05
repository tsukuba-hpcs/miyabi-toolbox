---
name: miyabi-development
description: >-
  Operate Miyabi safely from local, login, and PBS compute shells. Use for
  hostname, architecture and allocation classification, Environment Modules,
  qsub/qstat, storage, job diagnostics, Python environments, and Miyabi-specific GPU or
  distributed runtime setup. Keep login nodes control-plane only and load only
  the reference needed for the current operation.
---

# Miyabi Operations For Agents

Use this skill as an operating manual for Miyabi. It defines where commands may
run, how to work with PBS and Environment Modules, and which cluster-specific
reference to load. Project development policy belongs to the project
instructions, not this skill.

## Establish The Execution Context

Identify the host before selecting an interpreter, environment, or allocation:

```bash
hostname
uname -m
printf 'PBS_JOBID=%s\nPBS_NODEFILE=%s\n' "${PBS_JOBID:-}" "${PBS_NODEFILE:-}"
```

Keep **node role**, **host architecture**, and **project target** separate:

- **Miyabi login/control plane**: the hostname matches `miyabi-g*` or
  `miyabi-c*`; their architectures are `aarch64` and `x86_64`, respectively.
  Treat `interact-g*`/`interact-c*` hostnames conservatively as control-plane.
- **Miyabi compute node**: `mg<number>` is Miyabi-G (`aarch64`), `mc<number>`
  is Miyabi-C (`x86_64`). Require a PBS job ID and readable nodefile containing
  the current host before executing project code.
- **Local/unknown**: use the established remote-access path or inspect site
  evidence. An unknown hostname or inherited PBS variables alone do not grant
  compute execution.

Both login families provide system Python 3.9. Use `/usr/bin/python3 -I` for
initial probes and small standard-library utilities. For lightweight scripts
or static tools, prefer an existing compatible project environment when useful
and project rules permit; on a G login host this commonly means the G `.venv`.
For structured evidence, run [scripts/context.py](scripts/context.py) with that
interpreter. Before application/runtime checks, add `--target-system Miyabi-G` (or
`Miyabi-C`) and `--require-compute`; a refusal exits nonzero. Paths are relative
to this skill, so use the absolute skill path from a project directory.

Select the target from the project/user's workload, not the login hostname.
A G project edited on a C login node still needs a G allocation for its project
environment. Architecture agreement does not permit runtime work on a login
node. Details and helper semantics: [references/python-env.md](references/python-env.md).

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
  [references/qstat.md](references/qstat.md). For current and historical job
  lists, prefer [scripts/qstat_json.py](scripts/qstat_json.py): it emits JSON
  with optional field selection, status filtering, and counts.
- Interactive allocation, batch script construction, or job submission: read
  [references/qsub.md](references/qsub.md).
- Storage, quotas, job-local scratch, network, or containers: read
  [references/filesystem-network.md](references/filesystem-network.md).
- Python architecture mismatch, interpreter selection, environment creation,
  or dependency installation: read
  [references/python-env.md](references/python-env.md).
- Code → validation → experiments, allocation reuse, or validation failures:
  read [references/validation.md](references/validation.md).
- Open MPI environment propagation and process placement: read
  [references/mpi.md](references/mpi.md).
- PyTorch distributed or `torchrun`: read
  [references/torchrun-pbs.md](references/torchrun-pbs.md).
- Direct `Accelerator()` use or `accelerate launch`: read
  [references/accelerate-pbs.md](references/accelerate-pbs.md).
- vLLM, LoRA inference, CUDA device visibility, Triton, or FlashInfer: read
  [references/vllm-miyabi.md](references/vllm-miyabi.md).

Load the smallest relevant combination. Do not copy framework-specific
procedures back into this entrypoint. The provenance and scope of incorporated
operational incidents are indexed in
[references/failure-lessons.md](references/failure-lessons.md); read it when
investigating a matching symptom or revising these rules.

## Enforce Miyabi Safety Boundaries

On login/control-plane nodes, limit work to:

- reading and editing files;
- repository and configuration inspection;
- pager-safe module discovery;
- `qstat`, authorized `qsub`, and job-log inspection;
- small Python 3.9 standard-library tasks such as JSON conversion, bounded
  log/file processing, and this skill's helpers and offline tests;
- shell syntax checks and lightweight static checks using an already available
  compatible tool (including project `.venv` Ruff), with the required version
  and configuration, when project rules allow it.

Run application code, tests, heavy imports, dependency builds, preprocessing, model
loading, training, inference, distributed launchers, CUDA/NCCL operations, and
GPU profiling only in a confirmed PBS compute allocation.
Route pytest, runtime validators, and commands that synchronize/build the
project environment to the target allocation. A compatible `.venv` does not
change a command's workload class. An architecture mismatch calls for changing execution
location, not replacing the shared environment on the login host. Honor
stricter project rules that require all project checks on compute nodes.

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

1. Confirm hostname, architecture, target system, PBS evidence, and cwd.
2. Inspect and load modules in that shell.
3. Confirm the assigned resources and remaining walltime with `qstat`.
4. Run only the user-authorized workload.
5. Copy required outputs from job-local scratch to durable storage.
6. Stop background processes and exit cleanly when finished.

## Report Operations Precisely

Retain hostname/architecture, target system, PBS job ID, resource request,
interpreter path, modules, commands and exit results in the relevant logs or
project records. Report the result and material limitations concisely. Treat
PBS completion, application exit, and validated outputs as separate evidence;
do not claim unrun or interrupted checks passed.
