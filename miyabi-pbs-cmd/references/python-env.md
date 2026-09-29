# Python Environments

Follow the [execution boundaries](../SKILL.md#execution-boundaries). Shared
storage does not make a virtualenv portable between G (`aarch64`) and C
(`x86_64`). Use the project's existing environment tool and version pins.

## Context Helper Contract

[context.py](../scripts/context.py) reports host role, architecture, target,
PBS identifiers, nodefile hosts, `allocation_confirmed`,
`runtime_execution_allowed`, and refusal reasons without running project Python.

- Inspection exits 0 even when runtime execution is disallowed.
- `--target-system <Miyabi-G|Miyabi-C> --require-compute` exits 1 with JSON
  `ok:false` unless the compute hostname, architecture, job ID and nodefile
  membership match the target. Invalid CLI arguments exit 2 with argparse text.
- `allocation_confirmed` describes local evidence only. The helper makes no
  scheduler call and cannot establish that a job is still running; check the
  PBS job ID with [job queries](qstat.md). Unfamiliar host families need fresh
  site evidence before extending the guard.

Use `/usr/bin/python3 -I` for the helpers and small independent utilities.
`-I` isolates Python from project paths and user site packages. It does not
check workload cost or interpreter compatibility. Python 3.9 cannot validate
syntax introduced by a newer project Python.

An existing compatible `.venv` Python/Ruff may serve lightweight login tasks
when project rules permit. Invoke it directly without synchronizing packages.
An architecture error calls for changing execution location, not replacing a
working environment. Maintain separate environments only when the project
actually needs both architectures.

## Preserve Interpreter Identity

Inspect `.venv`, `pyproject.toml`, lockfiles, `.python-version` and the project's
launcher before changing dependencies. In the target allocation:

```bash
PROJECT_ROOT="/absolute/project/or/snapshot/root"
export PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
"$PYTHON_BIN" -c 'import sys; print(sys.executable, sys.version, sys.prefix)'
```

Keep the absolute **virtualenv** interpreter path without resolving symlinks.
Use `os.path.abspath`/`Path.absolute`, not `realpath`/`Path.resolve`: invoking the
base interpreter can lose the virtualenv's packages. Export the same
`PYTHON_BIN` through wrappers and MPI children; verify the actual child command
and `sys.prefix` before treating `ModuleNotFoundError` as a missing dependency.
A source snapshot may use an existing external environment when the project
explicitly selects it. See [Python venv](https://docs.python.org/3/library/venv.html).

## Create Or Synchronize On The Target

Choose the needed operation for the project's dependency format; these are
alternatives, not a sequence:

```bash
uv venv --python <project-python-version> .venv
uv sync --locked                    # uv-managed project with an existing lock
uv pip install --python "$PYTHON_BIN" -r requirements.txt
```

`uv run` can synchronize the environment before executing its command. Account
for that work when choosing where to run it; `--no-sync` does not repair an
architecture mismatch. Preserve the project's lockfile policy rather than
upgrading dependencies to solve a login-side execution error.
See [uv project execution](https://docs.astral.sh/uv/concepts/projects/run/).
