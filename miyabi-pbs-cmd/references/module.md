# Environment Modules Tips

Use this reference for `module`, `show_module`, compiler/runtime selection, and
module setup inside PBS shells. Live output from the relevant Miyabi host takes
precedence over historical module names or versions.

## Query Loaded And Available Modules As JSON

On the inspected Miyabi-G login shell, Environment Modules 5.3.0 supports native
JSON for discovery and loaded state (verified 2026-09-05):

```bash
export PAGER=cat MODULES_PAGER=cat LMOD_PAGER=cat
module --json list 2>&1
module --json avail <software-name> 2>&1
```

Use these as the default discovery interface. Output commonly goes to stderr,
hence `2>&1`; check the exit status and keep diagnostics if a result is not JSON.
Use unfiltered JSON `avail` only when the software name is unknown. Prefer an
exact module already used by the project, matching the target node group and
compiler/MPI stack. See the
[Modules JSON option](https://modules.readthedocs.io/en/latest/module.html#cmdoption-json).

## Text Details And Fallback

Not every subcommand supports JSON. For a selected module's instructions or
environment changes, use pager-safe text:

```bash
module help <module/version> 2>&1 | cat
module show <module/version> 2>&1 | cat
```

Use `show_module` when the Miyabi catalog's node-group/compiler information is
needed, and `show_module -a` only for cross-system results. Relevant fields are:

- `ModuleName`: value accepted by `module load`;
- `NodeGroup`: login or compute-node family where the module is available;
- `BaseCompiler/MPI`: compiler and MPI stack expected by the module.

If the target shell rejects JSON, check `module --version`/help and use targeted
text `list`/`avail` with `2>&1 | cat`. If `(END)` or `--More--` appears, send `q`,
set the pager variables above and rerun; wait for the prompt before sending
another command. Text typed into a pager does not execute in the shell.

## Load And Inspect The Result

```bash
module load <module/version>
module --json list 2>&1
command -v <expected-command>
<expected-command> --version
```

Environment Modules changes `PATH`, `LD_LIBRARY_PATH`, and related variables in
the current shell. Read conflict messages before using `module unload` or
`module switch`. Do not use `module purge` reflexively; purge only when the
workload intentionally replaces the complete default stack.

The Miyabi User's Guide v1.8 documents NVIDIA HPC SDK with `nv-hpcx` as the
Miyabi-G default and Intel oneAPI with `impi` as the Miyabi-C default. Inspect
the loaded-module JSON rather than assuming versions or reloading defaults.
Module availability, architecture and compiler/MPI selection are target-shell
properties; a module path visible from G login is not a C environment recipe.

## Load Modules In Every PBS Shell

Module state is shell-local. Establish the intended stack explicitly in every
allocation; do not assume login state was inherited correctly. Running `module
load` inside a separate Python/subprocess shell does not change the caller's
environment. A login shell started by an MPI launcher can reapply site defaults
after environment variables were passed to it; set intentional workload
overrides after that initialization, immediately before Python (see [mpi.md](mpi.md)).

For a batch script, place module setup after PBS directives and before project
commands:

```bash
#!/bin/bash
#PBS ...

set -eEuo pipefail
trap 'echo "[ERROR] Failed at line $LINENO" >&2' ERR
export PAGER=cat MODULES_PAGER=cat LMOD_PAGER=cat

REQUIRED_MODULES=(
  "<required-module/version>"
)
for module_name in "${REQUIRED_MODULES[@]}"; do
  module load "$module_name"
done
module list 2>&1 | cat

cd "${PBS_O_WORKDIR:?PBS_O_WORKDIR is not set}"
```

Replace every placeholder before submission. `REQUIRED_MODULES=()` is valid
when the workload intentionally uses compute-node defaults. The text `module
list` above is a durable job log; agent queries should use JSON where supported.

For an interactive allocation, repeat discovery and loading after `qsub -I`
returns the compute-node prompt. If a module configures Open MPI, also read
[mpi.md](mpi.md) before constructing `mpirun` environment arguments.
