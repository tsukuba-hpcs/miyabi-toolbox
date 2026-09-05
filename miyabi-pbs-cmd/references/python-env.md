# Python Environment Tips

Use the project's existing environment tool and version pins. Do not replace a
working environment merely to conform to this reference.

## Route By Role And Architecture

Both `miyabi-g*` (aarch64) and `miyabi-c*` (x86_64) login nodes provide system
Python 3.9. `/usr/bin/python3 -I` is the baseline for this skill's helpers and
small standard-library tasks: JSON parsing, bounded log summaries, file/path
inspection, and text transformations. Keep those tasks small; large data scans,
preprocessing, application/ML imports, dependency builds and runtime checks belong in
compute allocations. `-I` isolates these utilities from project Python paths
and user site packages; it does not make an arbitrary workload suitable for a
login node.

Choose the project's target system before touching its virtual environment:

```bash
# Read-only context, usable from either login family (path relative to skill).
/usr/bin/python3 -I scripts/context.py --target-system Miyabi-G

# Guard a project check after entering the target allocation.
/usr/bin/python3 -I scripts/context.py --target-system Miyabi-G --require-compute
```

The helper emits host role, observed and target architectures, PBS identifiers,
nodefile hosts, `allocation_confirmed`, `runtime_execution_allowed`, and reasons.
This permission field covers application/runtime execution, not lightweight
login utilities or static checks. Inspection exits 0 even when runtime execution
is disallowed. The explicit
guard exits 1 with JSON `ok:false` on refusal; invalid CLI arguments exit 2.
It reads only OS identity and PBS environment/nodefile evidence, makes no
scheduler calls, and never executes the project interpreter. This is a local
execution guard, not scheduler authentication or proof that dependencies work;
confirm live allocation state with qstat when entering/reusing a session.
The G/C host families are supported; unfamiliar site hosts need fresh evidence.

| Check | Execution context |
| --- | --- |
| JSON/text utilities; `bash -n` | Login, using host-native tools |
| Lightweight utility scripts and Ruff/format checks, including compatible project `.venv` tools | Login when project rules permit and architecture/version/config match |
| pytest, runtime validators, application/ML imports | Target compute allocation, normally the persistent interactive session |
| CUDA, MPI, training, inference, substantial builds | Target compute allocation |

An ARM `.venv/bin/ruff` or Python binary cannot run on an x86_64 login node.
Route G-project validation to `interact-g` even when editing on `miyabi-c*`;
route C-project validation to a supported C allocation. On a G login host, an
existing compatible G environment is convenient for lightweight utilities and
static checks: call its Python/Ruff directly, without synchronizing packages.
The same rule applies to a compatible C environment on a C login host. Honor
project rules that explicitly place even Ruff in an interactive allocation.
Do not retry on another login host merely to execute project runtime checks. An independent native
[Ruff installation](https://docs.astral.sh/ruff/installation/) is an optional
fast path when already available, not a reason to build a second ML environment.
Use `ruff --version` to inspect Ruff, not `ruff.__version__`.

The baseline Python's parser only knows Python 3.9 syntax. Do not treat a 3.9
`ast.parse`/`py_compile` rejection of newer project syntax as a product defect
or substitute a 3.9 parse for the required project-version validation.

## Preserve Environment Identity

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
Shared filesystem visibility does not make its binaries or native extensions
portable across architectures. If the project actually runs on both systems,
maintain separate environments and explicit interpreter paths; do not rename
or recreate an existing working environment simply for login-side linting.

Keep the **absolute virtualenv interpreter path without resolving symlinks**:

```bash
PROJECT_ROOT="/absolute/project/root"  # Select the intended checkout.
export PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
```

In Python use `os.path.abspath(...)` or `Path(...).absolute()` for that path,
not `Path.resolve()`/`realpath`. Resolving `.venv/bin/python` can invoke the base
interpreter directly and lose the virtualenv's site-packages. Diagnose a child
`ModuleNotFoundError` by comparing the actual interpreter and `sys.prefix`
before installing anything. In the confirmed target allocation, use the exact
launcher interpreter for a small dependency check:

```bash
"$PYTHON_BIN" -c 'import sys; print(sys.executable, sys.version, sys.prefix)'
# Then import only the dependencies used by the affected workload.
```

Export `PYTHON_BIN` through every wrapper that uses it, including npm tasks and
MPI children. A shell-local variable or absolute `npm` path does not bind nested
interpreters: scripts using `#!/usr/bin/env node` also need the selected Node
toolchain's bin directory on the job-local `PATH`. Use existing project controls
and test the actual nested command in the allocation. Never alter global shell
startup files to accomplish this. See [mpi.md](mpi.md) for rank-shell overrides.

## Create Or Synchronize In The Target Context

If the project uses `uv`, common commands inside its target allocation are:

```bash
uv venv --python <compatible-python-version> .venv
uv sync
uv pip install -r requirements.txt
uv run python <script.py>
```

`uv run` normally synchronizes the project environment before executing even a
seemingly static command such as `ruff`. Route it before invocation; neither a
shared lockfile nor `--no-sync` makes an incompatible environment executable.
Do not run `uv sync`, package installation, or environment recreation as an
automatic response to an architecture error on a login host.

Prefer a compatible existing environment or system Python for lightweight
tasks. An empty host-native utility environment may be created on a login node
when needed for an authorized lightweight task. Resolve/build/install substantial
GPU and ML stacks in compute. Select project Python from version pins and
compatibility, and retain the project lockfile discipline. Python 3.9 is the
utility baseline, not a prescribed version for training.

Sources: [uv project execution](https://docs.astral.sh/uv/concepts/projects/run/),
[Python virtual environments](https://docs.python.org/3/library/venv.html), and
the architecture/interpreter incidents indexed in [failure-lessons.md](failure-lessons.md).
