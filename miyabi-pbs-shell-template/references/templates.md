# PBS Templates

Choose an example from the [skill's template table](../SKILL.md).

## Fill And Check

- Select queue, group and resources using
  [qsub.md](../../miyabi-pbs-cmd/references/qsub.md). PBS directives require
  literal values; shell variables are not expanded there.
- Set `MIYABI_PBS_CMD_ROOT` to the absolute installed `miyabi-pbs-cmd`
  directory (not this skill's directory); it supplies `scripts/context.py`.
  Set the project root, target modules and actual entrypoint/argv separately.
  Replace or remove every `<...>` placeholder, preserving quoted argument
  arrays. `grep -n '<[A-Za-z]' <script>` lists any placeholders that remain.
- Use an absolute virtualenv `PYTHON_BIN`, preserving its symlink. The selected
  source, environment and context helper must remain readable from compute
  nodes until the queued job finishes; do not derive paths from PBS's spooled
  script location.
- Select worker counts from the allocation. Distributed assets use G/Open MPI;
  they are not C/Intel MPI recipes. See [mpi.md](mpi.md) for placement and
  optional compiler overrides.
- Choose durable log/output paths. The templates create log parents before
  `tee`; create any other required shared parents before launching ranks.
  Add the project's checkpointing and scratch-output preservation as needed.
- Run `bash -n` on the filled script. When the launch boundary changes, exercise
  a small real startup/communication check in an authorized target allocation
  before scaling.

Templates guard local compute context before using project Python, set the cwd,
export the interpreter to nested tools, and preserve worker failure through
`pipefail`. MPI compiler overrides are applied after child login initialization.

## Validation Scope

From this skill directory, run:

```bash
/usr/bin/python3 -I -B -m unittest discover -s tests -v
```

These offline regressions check shell syntax, context refusal and simulated
launcher boundaries. They require the sibling `miyabi-pbs-cmd` installation.
They do not test PBS scheduling, module availability, Python ABI, CUDA/NCCL or
distributed communication. The
G login probe validates refusal only; no live C allocation or distributed run
has validated these revised templates. Record the target, modules, framework
versions and outcome when performing the relevant real workload check.
