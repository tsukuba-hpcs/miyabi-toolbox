---
name: miyabi-pbs-cmd
description: Query, submit and control Miyabi PBS jobs using the site's qstat, qsub, qdel and tracejob dialect, and decide whether work may run on a login node or needs a compute allocation. Use for job status/history, queue limits, interactive or batch submission and cancellation. Prefer bundled scripts for JSON job/history and host/allocation information.
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

Miyabi-G is `aarch64`; Miyabi-C is `x86_64`. Choose the workload target
independently of the login host. Before application execution, require exit 0
from `context.py --target-system <target> --require-compute` and check the job's
live state. See the [context helper contract](references/context.md) for JSON
fields and guard behavior.

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
Codex agent that meets its prerequisites. Plan at submission and register after
obtaining the exact job ID. That skill defines estimation, authorization and
Goal prerequisites, with a default 5-minute polling interval. Reassess when a
queued job's expected wait grows.

Otherwise, report the job ID, its current state and the `qstat_json.py` query
for checking it later. Do not keep a turn open in a sleep or polling loop for a
long job.
