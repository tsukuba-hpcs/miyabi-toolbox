# Python Environment Tips

Use the project's existing environment tool and version pins. Do not replace a
working environment merely to conform to this reference.

Before creating or changing an environment, inspect:

```text
.venv/
pyproject.toml
uv.lock
.python-version
requirements*.txt
environment.yml
```

Prefer a project-local `.venv` when the project has no established alternative.
If the project uses `uv`, common commands are:

```bash
uv venv --python <compatible-python-version> .venv
uv sync
uv pip install -r requirements.txt
uv run python <script.py>
```

On a login node, creating an empty virtual environment is acceptable when it
does not execute project code. Resolve, build, or install substantial GPU and
ML dependency stacks in an interactive compute allocation or batch job. Select
Python from project pins and package compatibility; do not impose a global
version or perform unbounded dependency upgrades.
