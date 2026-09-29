---
name: miyabi-development
description: >-
  Operate Miyabi login and PBS compute shells: inspect host/allocation context,
  query and submit jobs, select modules and Python environments, and configure
  Miyabi storage or GPU/distributed workloads.
---

# Miyabi Operations

Use project instructions, existing PBS scripts, environment pins and current
site output to choose paths, accounting group, resources and software versions.
This skill supplies Miyabi execution guidance; project acceptance criteria stay
with the project and user.

## Establish The Execution Context

Set `SKILL_ROOT` to this installed skill's absolute directory. The helpers need
only Python 3.9+ and its standard library; use host Python before invoking a
project environment.

```bash
SKILL_ROOT="/absolute/path/to/miyabi-development"
# Select the target from the workload, independently of the current login host.
/usr/bin/python3 -I "$SKILL_ROOT/scripts/context.py" --target-system Miyabi-G
```

Keep host role, observed architecture and project target separate: Miyabi-G is
`aarch64`, Miyabi-C is `x86_64`. Editing a G project on C login does not change
its target. Before application execution, run the helper with the chosen target
and `--require-compute`; require exit 0. Unknown hosts, login hosts and inherited
PBS variables alone do not pass this guard. Its evidence is local, so also check
the job's live state when entering or reusing an allocation.

## Execution Boundaries

Use these conservative defaults for agent-run work on shared login nodes;
the [site guidance](https://www.cc.u-tokyo.ac.jp/en/guide/notice/) describes the
underlying load restrictions.

| Work | Where to run |
| --- | --- |
| File/repository edits, scheduler/module inspection, authorized submission | Login/control plane |
| Small host-native utilities, `bash -n`, existing compatible static tools such as Ruff; this skill's stdlib offline tests | Login, when project rules permit |
| Project test suites, runtime validators, application/ML imports, environment synchronization, dependency builds, preprocessing, training, inference, MPI/CUDA operations | Matching PBS compute allocation |

A compatible `.venv` does not change a command's workload class. Route an
incompatible binary to its target architecture instead of recreating the shared
environment on login. Honor stricter project execution rules.

Use a persistent interactive PTY for iterative compute checks and batch jobs
for unattended or sustained work. In each allocation, establish modules in that
shell, select the project cwd/interpreter, and check remaining walltime before
starting work. Preserve required scratch outputs and stop owned background
processes before exiting. Recheck context after the session ends.

Submission and job control change scheduler state. Verify the exact target and
resources against the user's authorized scope immediately before acting; an
existing authorization remains valid within that scope.

## Query And Load Details On Demand

Use [qstat_json.py](scripts/qstat_json.py) for current jobs and history:

```bash
/usr/bin/python3 -I "$SKILL_ROOT/scripts/qstat_json.py"
# Add -H for history; --fields or --summary limits the returned output.
```

Check JSON `ok` and exit status. Use the documented native fallbacks for fields
or queries the helper does not cover, or to diagnose a failure.

| Need | Reference |
| --- | --- |
| Job JSON contract, resources, limits, history and exit status | [qstat.md](references/qstat.md) |
| Interactive sessions and batch submission | [qsub.md](references/qsub.md) |
| PBS assets, placeholders and validation limits | [templates.md](references/templates.md) |
| Module discovery, conflicts and shell initialization | [module.md](references/module.md) |
| Python architecture, interpreter identity and environment setup | [python-env.md](references/python-env.md) |
| Storage, scratch, quotas, network and containers | [filesystem-network.md](references/filesystem-network.md) |
| Open MPI placement and rank environments | [mpi.md](references/mpi.md) |
| `torchrun` or Accelerate launch contracts | [torchrun-pbs.md](references/torchrun-pbs.md), [accelerate-pbs.md](references/accelerate-pbs.md) |
| vLLM device visibility, compilation and LoRA evaluation | [vllm-miyabi.md](references/vllm-miyabi.md) |

Retain the job ID, execution context, modules, interpreter, command and exit
result in task logs. PBS completion, application exit and validated outputs are
separate evidence; report only what was checked.
