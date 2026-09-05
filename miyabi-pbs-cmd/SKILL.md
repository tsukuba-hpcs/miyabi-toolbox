---
name: miyabi-development
description: >-
  Operate Miyabi safely from local, login, and PBS compute shells. Use for
  hostname, architecture and allocation classification, Environment Modules,
  qsub/qstat, storage, job diagnostics, Python environments, and Miyabi-specific GPU or
  distributed runtime setup. Prefer bundled JSON helpers for context and job
  queries; keep login nodes control-plane only and load references on demand.
---

# Miyabi Operations For Agents

Use the bundled helpers to inspect Miyabi and the PBS assets to prepare jobs.
This skill defines execution boundaries and site-specific operations; project
development policy belongs to the project instructions.

## Start With Structured Queries

Set `SKILL_ROOT` to this installed skill's absolute directory so these commands
work from the project checkout. Both login families provide system Python 3.9;
the helpers use only its standard library and need no project environment.

```bash
SKILL_ROOT="/absolute/path/to/miyabi-development"
# Select the target from the workload: Miyabi-G or Miyabi-C.
/usr/bin/python3 -I "$SKILL_ROOT/scripts/context.py" --target-system Miyabi-G
# Current jobs; use -H for history and --fields or --summary to limit output.
/usr/bin/python3 -I "$SKILL_ROOT/scripts/qstat_json.py"
```

Use [context.py](scripts/context.py) before selecting a project interpreter or
entering/reusing an allocation. Keep its `node_role`, `architecture` and
`target_system` separate: G is `aarch64`, C is `x86_64`; a G project edited on
C login still targets G. Before application/runtime execution, add
`--require-compute` and require success. Unknown hosts, inherited PBS variables
alone, or matching architecture on a login host do not grant compute execution.
Guard semantics and compatible login-side `.venv` use:
[references/python-env.md](references/python-env.md).

Use [qstat_json.py](scripts/qstat_json.py) as the default for current jobs,
history, status filtering and counts. Read JSON and check `ok` and exit status;
do not recreate its parsing with shell pipelines. Direct `qstat` is a fallback
for uncovered fields/queries or helper diagnostics, as described in
[references/qstat.md](references/qstat.md). For module discovery and loaded
state, use native JSON as described in [references/module.md](references/module.md);
module loading still runs in the current shell.

## Inspect Local And Project Rules

Prefer current evidence over generic examples:

- `AGENTS.md`, `README*`, `for_codex/`, environment files, and user notes;
- existing PBS scripts, recent job logs, module setup, and site documentation;
- `pyproject.toml`, `uv.lock`, `.python-version`, `.venv/`, and launcher
  configuration;
- live structured context, job and module results, with native diagnostics
  when needed.

Do not hardcode project paths, groups, queues, modules, models, datasets, cache
directories, or resource limits unless the project, user, or current Miyabi
state provides them.

## Load Only The Relevant Reference

- Environment Modules discovery, loading, conflicts, or pager behavior: read
  [references/module.md](references/module.md).
- Queue/resource discovery, job status, history, or diagnostics: read
  [references/qstat.md](references/qstat.md) for the JSON contract and limited
  native fallbacks.
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
procedures back into this entrypoint.

## Enforce Miyabi Safety Boundaries

On login/control-plane nodes, limit work to:

- reading and editing files;
- repository and configuration inspection;
- module and scheduler inspection through the interfaces above, authorized
  `qsub`, and job-log inspection;
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
interactive or batch job shell and record the loaded modules there.

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

1. Require a successful `context.py --target-system <target> --require-compute`
   guard and set the intended project cwd.
2. Inspect and load modules in that shell.
3. Query the allocation ID with `qstat_json.py`. For resource details and
   walltime budget, use the detail fallback in [references/qstat.md](references/qstat.md).
4. Run only the user-authorized workload.
5. Copy required outputs from job-local scratch to durable storage.
6. Stop background processes and exit cleanly when finished.

## Report Operations Precisely

Retain hostname/architecture, target system, PBS job ID, resource request,
interpreter path, modules, commands and exit results in the relevant logs or
project records. Report the result and material limitations concisely. Treat
PBS completion, application exit, and validated outputs as separate evidence;
do not claim unrun or interrupted checks passed.
