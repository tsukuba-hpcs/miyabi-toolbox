# Python Environments

Follow the [execution boundaries](../../miyabi-pbs-cmd/SKILL.md#execution-boundaries)
and [context guard](../../miyabi-pbs-cmd/references/context.md) before running
project Python. Shared storage does not make a virtualenv portable between G
(`aarch64`) and C (`x86_64`). An architecture error calls for changing execution
location, not replacing a working environment. Use the project's environment
tool and version pins; maintain separate environments only when both targets
are needed. Host Python 3.9 cannot validate syntax introduced by newer Python.

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
