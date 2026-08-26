# PBS Job Script Review and Submission

Use this reference before submitting any new or changed PBS job script with `qsub`.

## Submission Gate

1. Run `bash -n` on every PBS script in the submission scope in a safe static-validation environment.
2. Replace every `#PBS -W group_list=<group_id>` placeholder with a valid, literal group ID.
3. Estimate runtime from the workload and prior evidence, then request the shortest practical `walltime` that still has enough safety margin for startup variance, runtime variance, and orderly teardown. Request at least 10 minutes. If the script's default is materially longer than this evidence-based estimate, override it explicitly with `qsub -l walltime=...`. Do not reduce the margin so far that the job is likely to time out.
4. Do not submit the script until these checks are complete.

## Script Review

- Confirm the PBS queue, group, node count, process count, walltime, and GPU assumptions with existing project scripts or user notes.
- Confirm `cd "$PROJECT_ROOT"` points to the intended checkout.
- Confirm Python, `torchrun`, `accelerate`, `module load`, cache directories, and dataset paths are project-specific placeholders or valid for the user.
- Print `NNODES`, `WORLD_SIZE`, `MASTER_ADDR`, `MASTER_PORT`, ranks, hostnames, and log paths before launching.
- Check Open MPI environment propagation: prefer `/usr/bin/env KEY=value ... bash -lc ...`; never mix `mpirun -x` with `OMPI_MCA_mca_base_env_list`.
- Use `set -eEuo pipefail` and a simple error trap.
- Write logs to a timestamped directory and preserve PBS output.
