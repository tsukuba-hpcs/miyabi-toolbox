# PBS Template Use And Validation

Use a known-working project script first. Otherwise copy one of these assets
and replace all `<...>` placeholders before submission:

| Asset | Target and launch topology |
| --- | --- |
| [single-node.pbs](../assets/pbs/single-node.pbs) | G or C; one process on one node, target/module/environment selected explicitly |
| [torchrun.pbs](../assets/pbs/torchrun.pbs) | G/Open MPI; one MPI supervisor per node, torchrun creates local workers |
| [mpi-workers.pbs](../assets/pbs/mpi-workers.pbs) | G/Open MPI; one MPI process per Python worker, including direct `Accelerator()` use |

The distributed templates deliberately target the existing G/Open MPI pattern.
They are not an Intel MPI recipe for C. Select processes per node from the
actual allocation and workload; do not import another cluster's GPU count.

## Fill And Check

1. Read [qsub.md](qsub.md) and current queue/project limits. Fill literal queue,
   group, select and walltime directives; shell variables do not expand in them.
2. Set `SKILL_ROOT` to this installed skill's absolute directory, readable from
   compute nodes. Its Python 3.9 [context helper](../scripts/context.py) gates
   the job before project execution. Keep that helper available for queued jobs.
3. Set the intended project/snapshot root and absolute virtualenv `PYTHON_BIN`.
   Preserve its symlink; set the correct environment for the target architecture.
   Bind project-specific entrypoints/arguments rather than assuming `train.py`
   or `src.cli` exists. An existing environment outside a source snapshot can be
   selected explicitly when it is the project's established setup.
4. Select modules for the target shell. `REQUIRED_MODULES=()` is valid only when
   compute defaults are intentional. Optional `RUNTIME_CC`/`RUNTIME_CXX` are for
   diagnosed compiler issues; see [mpi.md](mpi.md). Keep scheduler GPU visibility
   node-local.
5. Choose durable output paths. Log parents are created before `tee`/MPI; any
   application-specific shared publication parents must also exist before ranks
   start. Add the project's checkpoint/restore/output-preservation handling
   where required; a generic shell cannot infer those contracts.
6. Run `bash -n` on the filled script, inspect remaining placeholders, and verify
   the resource request. Then run the affected small workload in an authorized
   target allocation before using a new launch pattern at scale.

The templates preserve failure status through `pipefail`, set an explicit cwd,
and export the interpreter to nested tools. Torchrun is invoked through
`"$PYTHON_BIN" -m torch.distributed.run` so its Python is tied to the selected
environment. MPI compiler overrides are applied inside the child login shell,
after site initialization. The helper validates local allocation evidence;
it does not validate Python ABI, installed packages, MPI/CUDA compatibility,
remaining time or a successful application result.

The source layout is self-contained apart from the referenced installed helper
and chosen project environment. PBS may copy a submitted script into its spool,
so these paths must not depend on the copied script's current directory.

## Validation Scope

This revision is checked with Bash syntax validation, Python 3.9 offline tests,
and simulated launcher-boundary regressions. The context helper is also probed
on the G login host for correct refusal of runtime execution. Host families were
checked against local `/etc/hosts`; C queue availability was observed through
`qstat --rsc -x`. No C allocation or live distributed run validates these revised
templates yet. Before adoption, record the target, modules, Python/framework
versions and outcome of the relevant allocation test. Syntax or simulated
success alone is not evidence of distributed runtime correctness.
