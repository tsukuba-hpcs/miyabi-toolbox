# Python Environment

Prefer `uv` for Python environment and dependency management when the project
does not already standardize on another tool.

- Prefer a project-local virtual environment at `<project_root>/.venv`.
- Before creating a new environment, inspect the project for existing `.venv/`, active `$VIRTUAL_ENV`, `pyproject.toml`, `uv.lock`, `.python-version`, `requirements*.txt`, `environment.yml`, or local docs.
- Select Python from project pins and the compatibility range of required ML
  packages. Do not impose a global version on an existing project.
- If the project is unpinned, identify a mutually supported version before
  installing large dependency stacks and record the choice.

Create a default environment only when appropriate for the node:

```bash
uv venv --python <compatible-python-version> .venv
```

Common install/run commands:

```bash
uv sync
uv pip install -r requirements.txt
uv run python <script.py>
```

On login nodes, creating a bare `.venv` is acceptable when it does not execute
project code. Avoid resolving, building, or installing heavy GPU/ML dependency
stacks on a login node; do that in an interactive node or a PBS job when it is
substantial. Do not rebuild a working environment merely to conform to this
reference.
