# PBS Templates

Prefer a working project script; otherwise copy and fill one of these assets:

| Asset | Target and topology |
| --- | --- |
| [single-node.pbs](../assets/pbs/single-node.pbs) | G or C; one process on one node |
| [torchrun.pbs](../assets/pbs/torchrun.pbs) | G/Open MPI; one supervisor per node, torchrun spawns local workers |
| [mpi-workers.pbs](../assets/pbs/mpi-workers.pbs) | G/Open MPI; one MPI process per Python worker, including direct Accelerate |

## Fill And Check

- Select literal PBS queue, group and resources using [qsub.md](qsub.md).
- Set `SKILL_ROOT`, project root, target modules and the actual entrypoint/argv.
  All `<...>` placeholders need replacement or removal. These scripts assume
  no project-specific CLI layout.
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

[Offline regressions](../README.md#validation) check shell syntax, context
refusal and simulated launcher boundaries. They do not test PBS scheduling,
module availability, Python ABI, CUDA/NCCL or distributed communication. The
G login probe validates refusal only; no live C allocation or distributed run
has validated these revised templates. Record the target, modules, framework
versions and outcome when performing the relevant real workload check.
