# Python Environment

Default to `uv` for Python environment and dependency management.

- Default Python version: `python==3.13`.
- Prefer a project-local virtual environment at `<project_root>/.venv`.
- Before creating a new environment, inspect the project for existing `.venv/`, active `$VIRTUAL_ENV`, `pyproject.toml`, `uv.lock`, `.python-version`, `requirements*.txt`, `environment.yml`, or local docs.
- Follow project pins when they conflict with these defaults, and mention the deviation.

Create a default environment only when appropriate for the node:

```bash
uv venv --python 3.13 .venv
```

Common install/run commands:

```bash
uv sync
uv pip install -r requirements.txt
uv run python <script.py>
```

On login nodes, creating a bare `.venv` is acceptable when it does not execute project code. Avoid resolving, building, or installing heavy GPU/ML dependency stacks on a login node; do that in an interactive node or a PBS job when it is substantial.
