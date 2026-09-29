# Environment Modules

Query the target shell's loaded and available modules as JSON (supported on
`miyabi-g1`, verified 2026-09-05):

```bash
export PAGER=cat MODULES_PAGER=cat LMOD_PAGER=cat
module --json list 2>&1
module --json avail <software-name> 2>&1
```

Module output commonly uses stderr. Check exit status and preserve diagnostics
if the result is not JSON. Use a targeted `avail` query unless the software name
is unknown. See the [native JSON option](https://modules.readthedocs.io/en/latest/module.html#cmdoption-json).

For module instructions or environment changes, use `module help <module/version>`
and `module show <module/version>` with the pager variables above. `show_module`
adds Miyabi's `NodeGroup` and `BaseCompiler/MPI` information; `ModuleName` is the
loadable name. Use `show_module -a` for cross-system discovery. Availability on
G login does not establish compatibility with a C job.

If JSON is unsupported, inspect `module --version` and use targeted text
`list`/`avail`. If a pager is already open, quit it with `q` and wait for the
shell prompt before rerunning the command.

## Establish The Job's Stack

```bash
module load <module/version>
module --json list 2>&1
command -v <expected-command>
```

Choose exact versions from the project and target catalog. G normally uses
NVIDIA HPC SDK with `nv-hpcx` (Open MPI); C uses Intel oneAPI with `impi`.
Inspect current defaults rather than assuming versions or loading both stacks.
Read conflicts before unloading/switching; purge only when intentionally
replacing the complete stack.

Load required modules in the current job shell. Loading in a separate subprocess
does not update its caller, and login-shell state is not a job setup recipe.
The [PBS assets](templates.md) contain the module setup block; an empty
`REQUIRED_MODULES=()` means the compute defaults are intentional. Retain a
pager-free loaded-module list in job logs.

MPI children started with `bash -lc` can reapply site defaults. Apply diagnosed
runtime overrides after child-shell initialization; see [mpi.md](mpi.md).
