# Development And Validation On Miyabi

Use this for the edit → test → experiment cycle. Project instructions determine
which checks and acceptance criteria apply; this reference determines where
and how to execute them. Follow [python-env.md](python-env.md) first when host
and project architectures differ.

## Keep A Continuous Validation Session

Edit and inspect code on the control plane. Use host system Python 3.9 for
small independent utilities and native shell/static tools where permitted.
Compatible project `.venv` tools may serve those lightweight tasks when project
rules permit. For tests, runtime checks, incompatible tools, or checks covered
by a stricter project rule, request the target system's interactive queue
using [qsub.md](qsub.md), then retain its PTY across the continuous test window.
In that shell, require the [context helper's](../scripts/context.py)
`--target-system <target> --require-compute` guard, inspect/load modules, and
bind the actual project Python before Ruff, pytest, imports, generators or
prepare-only checks.

Use [JSON job queries](qstat.md#json-job-queries) to check the allocation's live
state. Inspect walltime limits/usage with the
[detail fallback](qstat.md#native-fallback) before the next group of checks. Preserve
logs and finish processes before exiting. If time is insufficient, continue in
a replacement target allocation within the existing authorization; an explicit
user limit on allocation count still applies. When a session ends, confirm
where subsequent commands will run before issuing another project command.
An expired allocation does not justify a login-side runtime fallback. Avoid
creating a separate batch job for each individual check.

Run a check sequence in a child shell or script so `exit`/`set -e` stops that
attempt without unintentionally closing the persistent interactive shell:

```bash
# Already inside the confirmed allocation; paths selected for this attempt.
mkdir -p "$LOG_ROOT"
bash -o pipefail -c '
  bash "$CHECK_SCRIPT" 2>&1 | tee "$LOG_ROOT/validation.log"
'
```

Export `LOG_ROOT`, `CHECK_SCRIPT` and `PYTHON_BIN` before this command. The check
script should stop at its first required failed gate. Inspect the returned exit
status before continuing; a successful `tee` is not a successful validation.
Choose a new attempt path for a retry when previous evidence must be retained.

## Exercise The Real Launch Boundary

Resolve the actual files, cwd and CLI flags from the chosen checkout. When
deriving changed Python paths, exclude deleted paths and account for new files.
Use the repository's test discovery conventions; a wrong path or missing
`conftest.py` context is an invocation problem, not proof of a product failure.

Separate control-plane submission from dependency-complete preparation:

- A submit wrapper checks literal PBS parameters, files and authorization,
  then invokes qsub once. It must not invoke an incompatible project Python
  on a login host.
- A prepare-only/validation path runs in compute, exercises the actual resolved
  config and initialization boundary, and must not reach qsub or start training.
  Inspect its documented flags before using it; a filename such as `validate`
  does not establish absence of side effects.
- Use the intended checkout's absolute entrypoint and cwd, and pass the exact
  virtualenv interpreter without resolving its symlink. Preparation from the
  control tree is not validation of an isolated experiment tree.

Where launch behavior changed, progress from the affected checks to a small
real startup/communication probe before scaling. Import-only or syntax checks
do not exercise rank environment, model compilation, shared-directory setup,
or actual collective communication. Test the changed boundary without adopting
project-specific experiment counts or time/loss gates as cluster-wide rules.

## Preserve A Useful Result

For durable experiments, bind submission to a stable code/configuration snapshot
using the project's established isolation workflow, including intended uncommitted
changes when applicable. Record the actual source and interpreter used at job
start. Changes to the control worktree while a job queues must not silently
change the experiment's code. Keep large temporary models/cache outputs outside
small validation-report packages; preserve only the needed durable artifacts.

Create log and shared publication parents before opening pipelines or launching
MPI ranks. A compute node's `/tmp` path is not the login host's `/tmp`; write
retained logs/results to a shared durable path or copy them there before exit.

Distinguish queue waiting, process startup, application readiness, and runtime.
In supervisor/child-job workflows, size concurrency against project job/node
limits, including the supervisor itself. Queue starvation is not application
failure. Use bounded waits for the actual readiness event; avoid increasing
timeouts or resubmitting unchanged runs without diagnosing the observed failure.

Reconcile live/history JSON job results, application exits, and expected output
readback; use retained job details when exit status is needed. A running parent
allocation does not prove every child/rank is alive;
use rank/process evidence for failures inside one co-allocation. Preserve failed,
interrupted, and unknown outcomes as such. Rerun the affected behavior after a
correction; a metadata/source-ID change alone does not require all experiments.
