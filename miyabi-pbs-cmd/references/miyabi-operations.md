# Miyabi Environment And Operations

Use this reference for Environment Modules, job-shell setup, storage choices,
queue discovery, and PBS diagnostics. It distills the agent-relevant parts of
the Miyabi User's Guide v1.8. Treat live command output and current project
scripts as authoritative when versions or queue limits differ from the guide.

## Contents

- [Use Modules Without Losing Terminal Commands](#use-modules-without-losing-terminal-commands)
- [Discover And Select Modules](#discover-and-select-modules)
- [Load Modules Inside The Job Shell](#load-modules-inside-the-job-shell)
- [Respect Default Compiler Stacks](#respect-default-compiler-stacks)
- [Choose Storage Deliberately](#choose-storage-deliberately)
- [Discover Queues And Diagnose Jobs](#discover-queues-and-diagnose-jobs)
- [Network And Container Notes](#network-and-container-notes)

## Use Modules Without Losing Terminal Commands

`module avail` and `module list` can invoke a terminal pager. In an automated
PTY, the pager may consume characters intended as the next shell command.
Disable common module pagers before inspection and capture stderr, where module
commands commonly write their output:

```bash
export PAGER=cat
export MODULES_PAGER=cat
export LMOD_PAGER=cat

module avail 2>&1 | cat
module list 2>&1 | cat
```

When driving an interactive terminal:

1. Send `module avail` or `module list` as a command by itself.
2. Wait for completion and the shell prompt before sending `module load` or any
   later command.
3. If `(END)`, `--More--`, or another pager prompt appears, send `q`, wait for
   the shell prompt, set the pager variables above, and rerun the inspection.
4. Never assume commands typed after a paged command were executed; verify the
   resulting state explicitly.

Apply the same one-command-at-a-time rule to `show_module` when its output is
large.

## Discover And Select Modules

Use this sequence on the relevant Miyabi login environment:

```bash
export PAGER=cat MODULES_PAGER=cat LMOD_PAGER=cat
module list 2>&1 | cat
module avail <software-name> 2>&1 | cat
show_module
```

Prefer a targeted `module avail <software-name>` query to avoid unnecessary
output. Use the unfiltered `module avail 2>&1 | cat` only when the software name
is unknown.

Use `show_module -a` only when cross-system results are needed. Its useful
fields are:

- `ModuleName`: the name accepted by `module load` and shown by `module avail`;
- `NodeGroup`: whether the software is available on Login-G/Login-C or
  Miyabi-G/Miyabi-C;
- `BaseCompiler/MPI`: the compiler and MPI stack the module expects.

Before loading a candidate, inspect its build and usage information:

```bash
module help <module/version> 2>&1 | cat
```

Prefer an exact module/version already used by project scripts. Otherwise,
choose from current `module avail`/`show_module` output and verify that its node
group and compiler/MPI dependencies match the target job.

After loading, always verify:

```bash
module load <module/version>
module list 2>&1 | cat
command -v <expected-command>
<expected-command> --version
```

Environment Modules changes `PATH`, `LD_LIBRARY_PATH`, and related variables in
the current shell. Multiple versions or incompatible stacks may conflict. Use
`module unload` or `module switch` after reading the conflict message. Do not
use `module purge` reflexively; purge only when intentionally replacing the
complete default stack.

## Load Modules Inside The Job Shell

Module state is shell-local. Loading a module on a login node does not replace
loading it in the batch job or interactive compute-node shell.

For a PBS script, put module setup after the PBS directives and before project
paths, Python, compilers, or launchers:

```bash
#!/bin/bash
#PBS ...

set -eEuo pipefail
trap 'echo "[ERROR] Failed at line $LINENO" >&2' ERR

export PAGER=cat MODULES_PAGER=cat LMOD_PAGER=cat

# Purge only when the project intentionally replaces Miyabi's default stack.
# module purge

REQUIRED_MODULES=(
  "<replace-with-required-module/version>"
)
for module_name in "${REQUIRED_MODULES[@]}"; do
  module load "$module_name"
done
module list 2>&1 | cat

cd "${PBS_O_WORKDIR:?PBS_O_WORKDIR is not set}"
```

Replace or remove every placeholder before submission. An empty
`REQUIRED_MODULES=()` is acceptable only when the job intentionally relies on
the compute-node default stack; still record `module list` in the job log.

For an interactive allocation, repeat the same inspection and loading after
`qsub -I` returns the compute-node prompt. Do not rely on what was loaded in the
login shell before `qsub`.

When Environment Modules configures Open MPI, it may populate
`OMPI_MCA_mca_base_env_list` with `PATH` and `LD_LIBRARY_PATH`. Do not combine
that mechanism with `mpirun -x`; follow the main skill's `/usr/bin/env` pattern
or a verified project launcher.

## Respect Default Compiler Stacks

The v1.8 guide documents these defaults:

- Miyabi-G login and compute nodes: NVIDIA HPC SDK `nvidia` and `nv-hpcx`.
- Miyabi-C login and compute nodes: Intel oneAPI `intel` and `impi`.

Inspect `module list` instead of assuming versions. Do not reload a default
stack unnecessarily. To switch Miyabi-G to GCC/OpenMPI, the guide's pattern is
to purge intentionally, then load compatible `gcc` and `ompi` modules in the
job shell. To use the standalone CUDA Toolkit, intentionally switch to the
available `cuda` module. Derive exact versions from live output.

## Choose Storage Deliberately

- Use `/work/<group>/...` for shared durable project data and outputs.
- Keep `/home` light; the v1.8 guide documents a 50 GB user quota. Check the
  current state with `show_quota` rather than assuming remaining capacity.
- On compute nodes, use the automatically assigned `$TMPDIR` and, on Miyabi-G,
  `$LOCALDIR` for job-local scratch when the workload benefits from local I/O.
  Copy required artifacts back to durable storage before the job or
  interactive allocation ends.
- Do not assume `/common` paths are visible from compute nodes; the guide marks
  that storage as login-node-only.
- Diagnose `No space left on device` as either byte quota or directory/file
  count exhaustion; inspect both before retrying.

Do not run `chhome`, move dotfiles, or alter shell startup files without an
explicit user request. When editing shell initialization, avoid globally
prepending custom paths ahead of Miyabi system paths.

## Discover Queues And Diagnose Jobs

Prefer live PBS data over copied queue limits:

```bash
qstat --rsc
qstat --rsc -x
qstat --limit
```

Use the smallest diagnostic that answers the question:

```bash
qstat
qstat -v
qstat -H
tracejob <job_id>
```

- `qstat --rsc`: available queues and whether they accept/start jobs.
- `qstat --rsc -x`: current node, walltime, memory, and project limits.
- `qstat --limit`: current project submission/execution limits.
- `qstat -v`: detailed active-job status.
- `qstat -H`: recently ended jobs.
- `tracejob`: scheduler/server/accounting history for a job.

Treat `qdel`, `qhold`, and `qrls` as state-changing commands. Use them only
when the user requested that action or it is clearly required by the authorized
workflow.

## Network And Container Notes

The v1.8 guide documents outbound HTTP/HTTPS access from Miyabi-G compute
nodes through NAT, but no inbound access to compute nodes. Verify current
connectivity inside the allocation before depending on downloads.

For containers, discover the current module name/version first, then load it in
the job shell before running the container command. The guide's example uses a
Singularity module, but do not hardcode its historical version.
