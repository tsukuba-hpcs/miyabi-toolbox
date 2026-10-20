---
name: miyabi-pbs-cmd
description: Operate Miyabi PBS safely. First establish whether the shell is a login/control node, a confirmed PBS compute allocation or unknown, and keep heavy CPU/memory work off login nodes. Query, submit and control jobs using the site's qstat, qsub, qdel and tracejob dialect. Use at the start of any Miyabi session, before running tests, builds, training or inference, and for job status/history, queue limits, interactive or batch submission and cancellation. Prefer bundled scripts for JSON job/history and host/allocation information.
---

# Miyabi PBS Commands

Use project instructions and current site output to select the target system,
accounting group, resources and paths. Prefer the bundled scripts over parsing
raw command output yourself. They use host Python 3.9+ and the standard library.
Set `SKILL_ROOT` to the absolute directory containing this SKILL.md.

```bash
SKILL_ROOT="/absolute/path/to/miyabi-pbs-cmd"
/usr/bin/python3 -I "$SKILL_ROOT/scripts/context.py" --target-system Miyabi-G
/usr/bin/python3 -I "$SKILL_ROOT/scripts/qstat_json.py"
/usr/bin/python3 -I "$SKILL_ROOT/scripts/qstat_json.py" -H --hday 3 --hnum 10 --summary
```

Check exit status and JSON `ok`. Use `--fields` or `--summary` to limit output.
The helpers inspect local context and query jobs; they do not submit jobs or
implement resource/limit queries. Use native commands for those operations.
Do not hide helper failures as empty job lists.

## Establish The Operating Plane

Before running anything beyond reading and editing files, and again whenever the
shell changes (new SSH session, entering or leaving an allocation, a restarted
PTY), run `context.py` for the project's target system and state the plane in one
line, for example `Plane: login/control (miyabi-g1, aarch64); target Miyabi-G; no
allocation`. Do not infer the plane from the prompt, the working directory or
earlier turns.

| Plane | Evidence from `context.py` | Allowed work |
| --- | --- | --- |
| Login/control | `node_role: login` (`miyabi-g*`, `miyabi-c*`, `interact-g*`, `interact-c*`) | Control-class work only; see [Execution Boundaries](#execution-boundaries) |
| Confirmed compute | `runtime_execution_allowed: true`: a compute host (`mg<N>`, `mc<N>`), a valid PBS job ID, a nodefile containing this host, and a matching target | The user-authorized workload |
| Not confirmed | Anything else: an unknown host (local machine, unfamiliar hostname), a compute host without valid job/nodefile evidence, or an allocation of the wrong system for the target | Treat as control-plane at most; read `reasons`, then reach or obtain the right shell |

Keep three things separate: the node role (where this shell runs), the host
architecture, and the project's target system, which comes from the workload
rather than the login host. Miyabi-G is `aarch64`; Miyabi-C is `x86_64`. A G
project edited on a C login node still needs a G allocation, and inherited PBS
variables, a familiar hostname or a matching architecture never turn a login or
unknown shell into compute. Before application execution, require exit 0 from
`context.py --target-system <target> --require-compute` and check the job's live
state. See the [context helper contract](references/context.md) for JSON fields
and guard behavior. When reporting results, name the plane and, for compute work,
the host and job ID.

## Execution Boundaries

Use these conservative defaults for agent-run work on shared login nodes;
the [site guidance](https://www.cc.u-tokyo.ac.jp/en/guide/notice/) describes the
underlying load restrictions.

| Work | Where to run |
| --- | --- |
| File/repository edits, scheduler/module inspection, authorized submission | Login/control plane |
| Small host-native utilities, `bash -n`, existing compatible static tools such as Ruff; this skill's stdlib offline tests | Login, when project rules permit |
| Environment setup from prebuilt wheels: creating or synchronizing virtualenvs, installing dependencies, downloading interpreters (`uv venv`, `uv init`, `uv add`, `uv sync`, `uv pip install`) | Login node of the target system (`miyabi-g*` for G, `miyabi-c*` for C), with `timeout` |
| Installing a package that compiles from source (no wheel for the target, or a build from a source tree) | Interactive compute allocation of the target system (see [qsub.md](references/qsub.md#interactive-work)) |
| Project test suites, runtime validators, application/ML imports, preprocessing, training, inference, MPI/CUDA operations | Matching PBS compute allocation |

A compatible `.venv` does not change a command's workload class. Route an
incompatible binary to its target architecture rather than replacing a working
environment; build each system's environment on that system's login node. Honor
stricter project execution rules.

Classify each login-side command by what it can consume, not by how it is
invoked. Work that may use sustained CPU, many threads or processes, large
memory, heavy I/O or a GPU is workload-class, and so is anything whose cost you
cannot bound. Beyond the table's examples, this includes parallel builds
(`make -j`, a package compiling from source during an install), compressing or extracting large
archives, recursive scans of large trees (`find`, `du`, `grep -r` over data or
scratch), loading a large file or log whole into memory, and heavy imports such
as `python -c 'import torch'`. Smaller inputs or `nice` do not reclassify a
command when imports, model loading or compilation dominate.

Keep login-side processing bounded: check sizes first (`ls -l`, `wc -c`), read a
slice or stream (`head`, `tail -n`, `grep -m`), and cap runtime with `timeout`.
If a login-side command runs longer or heavier than expected, stop your own
process and move the work to compute.

When a command is workload-class and the plane is not a confirmed matching
allocation, do not run it. Use an interactive allocation for short iterative
checks or a batch job for unattended work (see [qsub.md](references/qsub.md)). If
allocating or submitting is not authorized, prepare the command or script, report
what must run where, and stop instead of running a reduced version on login.

Use a persistent interactive PTY for iterative compute checks and batch jobs
for unattended or sustained work. In each allocation, establish modules in that
shell, select the project cwd/interpreter, and check remaining walltime before
starting work. Preserve required scratch outputs and stop owned background
processes before exiting. Recheck context after the session ends.

Submission and job control change scheduler state. Verify the exact target and
resources against the user's authorized scope immediately before acting; an
existing authorization remains valid within that scope.

## Command References

- [qstat.md](references/qstat.md): JSON schema, Miyabi history flags, resource
  and limit queries, native fallbacks and job-result interpretation.
- [qsub.md](references/qsub.md): routing queues, accounting group, interactive
  sessions, submission validation and uncertain-result reconciliation.
- Cancel an authorized exact job with `qdel <job_id>`; reconcile its state with
  the JSON helper. For other control options, inspect the installed command's
  help/manual before using generic PBS syntax.
- For writing a submission shell, use
  [miyabi-pbs-shell-template](../miyabi-pbs-shell-template/SKILL.md), including its
  module, environment and distributed-launch references.

## Long Jobs

When estimated queue wait plus runtime exceeds 1 hour, invoke
[miyabi-pbs-goal-wakeup](../miyabi-pbs-goal-wakeup/SKILL.md) if you are a
Codex or Claude Code agent that meets its prerequisites. Plan at submission and
register after obtaining the exact job ID. That skill defines estimation,
authorization and per-agent prerequisites, with a default 5-minute polling
interval. Reassess when a queued job's expected wait grows.

Otherwise, report the job ID, its current state and the `qstat_json.py` query
for checking it later. Do not keep a turn open in a sleep or polling loop for a
long job.
