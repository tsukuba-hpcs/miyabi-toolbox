# Environment Modules Tips

Use this reference for `module`, `show_module`, compiler/runtime selection, and
module setup inside PBS shells. Live output from the relevant Miyabi host takes
precedence over historical module names or versions.

## Prevent Pager Interference

`module avail` and `module list` commonly write to stderr and may open a pager.
In an automated terminal, disable the pager before inspection:

```bash
export PAGER=cat MODULES_PAGER=cat LMOD_PAGER=cat
module list 2>&1 | cat
module avail <software-name> 2>&1 | cat
```

Send a paged command by itself and wait for the shell prompt before sending the
next command. If `(END)` or `--More--` appears, send `q`, set the pager variables,
and rerun the command. Do not assume text typed into a pager reached the shell.

## Discover A Module

Start with the current shell state and a targeted search:

```bash
module list 2>&1 | cat
module avail <software-name> 2>&1 | cat
module help <module/version> 2>&1 | cat
```

Use an unfiltered `module avail 2>&1 | cat` only when the software name is
unknown. Use `show_module` for the Miyabi catalog and `show_module -a` only when
cross-system results are needed. Relevant fields include:

- `ModuleName`: value accepted by `module load`;
- `NodeGroup`: login or compute-node family where the module is available;
- `BaseCompiler/MPI`: compiler and MPI stack expected by the module.

Prefer an exact module already used by the project. Otherwise select from live
output and match the target node group and compiler/MPI stack.

## Load And Inspect The Result

```bash
module load <module/version>
module list 2>&1 | cat
command -v <expected-command>
<expected-command> --version
```

Environment Modules changes `PATH`, `LD_LIBRARY_PATH`, and related variables in
the current shell. Read conflict messages before using `module unload` or
`module switch`. Do not use `module purge` reflexively; purge only when the
workload intentionally replaces the complete default stack.

The Miyabi User's Guide v1.8 documents NVIDIA HPC SDK with `nv-hpcx` as the
Miyabi-G default and Intel oneAPI with `impi` as the Miyabi-C default. Inspect
`module list` rather than assuming the current versions or reloading defaults.

## Load Modules In Every PBS Shell

Module state is shell-local. Loading a module on a login node does not load it
inside an interactive allocation or batch job.

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
when the workload intentionally uses compute-node defaults; still capture
`module list` in the job log.

For an interactive allocation, repeat discovery and loading after `qsub -I`
returns the compute-node prompt. If a module configures Open MPI, also read
[mpi.md](mpi.md) before constructing `mpirun` environment arguments.
