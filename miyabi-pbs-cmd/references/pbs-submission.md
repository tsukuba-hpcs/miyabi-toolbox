# PBS Job Script Review and Submission

Use this reference after the main skill determines that a batch job is
appropriate and before submitting any new or changed PBS job script with
`qsub`.

## Verify Site Defaults

Treat these values as Miyabi starting points, not timeless facts. Compare them
with current site or project documentation and existing working scripts before
submission:

- Login hosts: hostnames matching `miyabi-g*` or `miyabi-c*`.
- Interactive GPU queue: `interact-g`.
- Common regular GPU queue in examples: `regular-g`.
- 1-node interactive debug: start at `00:30:00`; use at most `01:00:00` when
  the next full attempt requires it.
- 2-node interactive debug: use at most `00:10:00` and only after 1-node
  validation passes.

Derive the PBS group from the current account unless the project specifies one:

```bash
GROUP_ID="${GROUP_ID:-$(groups | tr ' ' '\n' | awk '/^xg/ {print; exit}')}"
GROUP_ID="${GROUP_ID:-$(groups | awk '{print $1}')}"
: "${GROUP_ID:?Could not determine GROUP_ID; set it explicitly}"
printf 'GROUP_ID=%s\n' "$GROUP_ID"
```

Use a literal group value in `#PBS -W group_list=...`; PBS directives do not
expand shell variables.

## Submission Gate

1. Run `bash -n` on every PBS script in the submission scope in a safe static
   validation environment.
2. Replace every `#PBS -W group_list=<group_id>` placeholder with a valid,
   literal group ID.
3. Estimate runtime from the workload and prior evidence, then request the
   shortest practical `walltime` that still has enough safety margin for
   startup variance, runtime variance, and orderly teardown. Request at least
   10 minutes. If the script's default is materially longer than this
   evidence-based estimate, override it explicitly with
   `qsub -l walltime=...`. Do not reduce the margin so far that the job is
   likely to time out.
4. Do not submit the script until these checks are complete.

## Script Review

- Confirm the PBS queue, group, node count, process count, walltime, and GPU
  assumptions with existing project scripts or user notes.
- Confirm `cd "$PROJECT_ROOT"` points to the intended checkout.
- Confirm Python, `torchrun`, `accelerate`, `module load`, cache directories,
  and dataset paths are project-specific placeholders or valid for the user.
- Put every required non-default `module load` in the executable body after the
  PBS directives and before project commands. Do not rely on module state
  inherited from the submission shell.
- Disable module pagers and capture `module list` in the job log before the
  launcher starts.
- Require `$PBS_NODEFILE` when the launcher depends on it.
- Print `NNODES`, `WORLD_SIZE`, `MASTER_ADDR`, `MASTER_PORT`, ranks, hostnames,
  and log paths before launching.
- Check Open MPI environment propagation and CPU binding explicitly. Prefer
  `/usr/bin/env KEY=value ... bash -lc ...`; never mix `mpirun -x` with
  `OMPI_MCA_mca_base_env_list`.
- Use `set -eEuo pipefail` and a concise `ERR` trap.
- Write logs to a timestamped directory and preserve PBS output.
