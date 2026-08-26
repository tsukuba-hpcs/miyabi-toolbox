---
name: miyabi-development
description: >-
  Use when developing with Codex for Miyabi-targeted projects from either a
  local machine or a Miyabi remote shell. Routes by hostname: hosts that do not
  match miyabi-g* or interact-g* use the local Codex workflow with GitHub
  feature-branch synchronization; Miyabi remote hosts use cautious remote
  development rules, login-node control-plane limits, and PBS interactive/debug
  compute nodes for runtime validation.
---

# Miyabi Development

## Scope

Use this skill for Miyabi-targeted development, testing, runtime validation, PBS job scripts, distributed execution, CUDA/NCCL work, MPI, torchrun, Hugging Face Accelerate, vLLM, training, inference, or runtime debugging.

This skill covers two operating modes:

- Local Codex workflow: development starts on a non-Miyabi host and changes must be synchronized to Miyabi for static checks or runtime validation.
- Remote Miyabi workflow: Codex is already running on a Miyabi remote host and must obey the cluster safety rules directly.

Do not assume project-specific paths. Discover them from the repository, active shell, user notes, or local docs such as `for_codex/`, `README*`, `shells/miyabi/`, or existing qsub scripts.

## Host Routing

Before choosing the workflow, run:

```bash
hostname
```

Route by hostname:

- If `hostname` does not match `miyabi-g*` or `interact-g*`, use the Local Codex Workflow.
- If `hostname` matches `miyabi-g*` or `interact-g*`, use the Remote Miyabi Workflow.

The routing decision selects the development workflow. Once the remote workflow has obtained a PBS interactive allocation, the shell may report a compute hostname such as `mg<number>`. Treat that as a Miyabi compute/debug node under the Remote Miyabi Workflow, not as the local workflow.

If the host is unknown, inspect `hostname`, `$PBS_JOBID`, and `$PBS_NODEFILE`. If still uncertain, choose the safer interpretation: keep runtime work off the host until a proper Miyabi interactive/debug allocation is confirmed.

## Reference Routing

Keep this file loaded for the workflow. Load reference files only when the task needs their details:

- Python environment setup or dependency installation: read `references/python-env.md`.
- PBS batch job script creation, review, or submission with `qsub`: read `references/pbs-submission.md`.
- PyTorch distributed, `torchrun`, or code expecting `RANK`, `WORLD_SIZE`, `LOCAL_RANK`, `MASTER_ADDR`, or `MASTER_PORT`: read `references/torchrun-pbs.md`.
- Hugging Face Accelerate under MPI/PBS: read `references/accelerate-pbs.md`.
- vLLM inference, LoRA evaluation, CUDA/Triton/FlashInfer runtime errors, or PBS jobs launching vLLM: read `references/vllm-miyabi.md`.

If a task mixes these areas, read the smallest relevant combination.

## Miyabi Constants

- Login nodes: `miyabi-g1`, `miyabi-g2`, `miyabi-g3`.
- Host-routing aliases or remote entries may also look like `miyabi-g*` or `interact-g*`.
- Interactive GPU queue: `interact-g`.
- Regular GPU queue commonly used by examples: `regular-g`.
- 1-node interactive debug: start with `walltime=00:30:00`; maximum is `walltime=01:00:00`.
- 2-node interactive debug: maximum is `walltime=00:10:00`.
- PBS group: do not hardcode a user or project group in reusable guidance. Derive it from the current account with `groups`, then override only when project docs or the user specify a different group.

Before a `qsub` command that needs `group_list`, set:

```bash
GROUP_ID="${GROUP_ID:-$(groups | tr ' ' '\n' | awk '/^xg/ {print; exit}')}"
GROUP_ID="${GROUP_ID:-$(groups | awk '{print $1}')}"
echo "GROUP_ID=$GROUP_ID"
```

## Shared Defaults

- Prefer the smallest validation that exercises the changed behavior.
- Build or update focused test code when implementing or fixing behavior.
- Treat Miyabi login nodes as a control plane only.
- Use Miyabi interactive/debug or batch compute nodes for runtime tests, heavy imports, training, inference, distributed launchers, and GPU work.
- Do not treat local results as a substitute for Miyabi validation when behavior depends on PBS, GPU, MPI, CUDA, NCCL, cluster paths, or Miyabi modules.
- Use direct SSH copy only for scratch artifacts, logs, or explicitly temporary files, not as the primary path for tracked source code.

## Local Codex Workflow

Use this workflow when the initial `hostname` does not match `miyabi-g*` or `interact-g*`.

### Local Roles

- Local Codex: edit source, update tests, run formatters, run syntax checks, run dependency-complete unit tests that are safe locally, and prepare commits.
- GitHub: transfer tracked source changes between local and Miyabi with a visible commit graph.
- Miyabi login node: inspect files, review diffs, fetch and switch branches, run static shell checks, inspect logs, and submit or inspect PBS jobs.
- Miyabi compute/debug node: run project Python tests, import heavy libraries, run data preprocessing, model initialization, training, inference, CUDA/NCCL, MPI, torchrun, Accelerate, vLLM, or other runtime validation.

Before switching environments, record the branch and commit so local, GitHub, and Miyabi do not drift silently:

```bash
git status --short --branch
git rev-parse --short HEAD
```

### Branch And Merge Authority

Codex may create and push a `codex/<short-task-name>` feature branch without asking when any of these apply:

- Source changes need Miyabi synchronization or runtime validation.
- The change affects tracked source, PBS scripts, configs, tests, or docs that should be versioned.
- The change spans multiple files or may need multiple WIP iterations.
- The work should keep `main` clean while Miyabi validation is pending.
- Local and Miyabi checkouts need an exact shared code state.

Codex should avoid creating a branch only for read-only inspection, a tiny explicitly requested edit on the current branch, disposable scratch files, or when the user explicitly says to stay on the current branch.

Codex must not merge into `main` by default. Codex may prepare the branch, clean WIP commits, summarize validation, and say `ready to merge`; the user decides whether and how to merge into `main`.

Only merge without another confirmation when the user has explicitly pre-authorized that exact class of low-risk change and all stated checks have passed. Miyabi runtime, PBS, data-download, training, inference, distributed, or GPU-related changes always require explicit user approval before merge.

### GitHub Synchronization

Create and push a feature branch from the local checkout:

```bash
git switch -c codex/<short-task-name>
git add <changed-files>
git commit -m "<short WIP or focused message>"
git push -u origin codex/<short-task-name>
```

Fetch it on Miyabi:

```bash
git fetch origin
git switch codex/<short-task-name>
git pull --ff-only
```

WIP commits are acceptable on the feature branch during debugging. Before a user-confirmed merge, clean noisy branch history when needed:

```bash
git fetch origin
git reset --soft origin/main
git commit -m "<clean final message>"
git push --force-with-lease
```

Use direct SSH copy only for files intentionally outside the tracked source workflow. Prefer a dry run and do not casually overwrite tracked source files:

```bash
rsync -azn <local-path> miyabi-g:<remote-path>
```

If tracked source must be copied directly because GitHub is unavailable, immediately reconcile it back into Git on both sides and report that the normal branch workflow was bypassed.

### Local Recommended Sequence

1. In the local checkout, implement the change and add the smallest useful test, smoke script, or minimal config.
2. Run local lightweight checks that are safe for the local machine.
3. Create or reuse a `codex/<short-task-name>` feature branch, commit, and push it to GitHub.
4. On Miyabi, fetch the branch and run login-node static checks only.
5. Request a 1-node interactive allocation and run focused runtime tests or smoke commands inside it.
6. After each runtime attempt, check `qstat "$PBS_JOBID"` and decide whether enough walltime remains for another complete attempt plus cleanup.
7. If distributed behavior matters, run the 2-node interactive path only after the 1-node path passes.
8. Push or pull follow-up fixes through the same feature branch.
9. When validation passes, clean noisy WIP history if needed, then report `ready to merge` and wait for user approval before merging to `main`.

If interactive allocation cannot be obtained, leave the test code in place, report the skipped runtime validation, and provide the exact interactive command needed to continue.

## Remote Miyabi Workflow

Use this workflow when the initial `hostname` matches `miyabi-g*` or `interact-g*`, or when an already-started remote workflow has entered a PBS compute/debug node such as `mg<number>`.

### Remote First Check

Before any command that may execute project code or import heavy runtime libraries, run:

```bash
hostname
```

Classify the node:

- `miyabi-g1`, `miyabi-g2`, `miyabi-g3`, or `miyabi-g*`: login node or login-style remote entry. Treat as a control plane only.
- `mg<number>`: compute/debug node. Runtime validation is allowed when obtained through PBS.
- `interact-g*`: remote interactive entry. Confirm the actual shell hostname and PBS environment before runtime work.
- Unknown host: inspect `hostname`, `$PBS_JOBID`, and `$PBS_NODEFILE`; if uncertain, treat it like a login node.

### Login Node Policy

On `miyabi-g{1-3}` or any login-style `miyabi-g*` host, keep work to lightweight control-plane tasks:

- Read/search/edit files.
- Inspect configs, scripts, logs, and git status/diffs.
- Fetch or switch branches.
- Submit or inspect PBS jobs with `qsub` and `qstat`.
- Run static checks such as `bash -n <script>` when they do not execute project code.

Do not run these on a login node:

- `pytest`, test scripts, or ad hoc Python smoke tests.
- Python commands that import heavy project modules or runtime libraries such as `torch`, `tensorflow`, `jax`, `transformers`, `datasets`, `accelerate`, `deepspeed`, CUDA/NCCL code, model code, or dataset loaders.
- Training, evaluation, inference, data preprocessing, or checkpoint conversion unless the user has explicitly confirmed it is trivial and safe for a login node.
- `torchrun`, `accelerate launch`, `mpirun`, distributed launchers, GPU profiling, CUDA/NCCL jobs, or multi-process debug runs.

If a validation command is more than static inspection, switch to an interactive node first. If interactive allocation is unavailable, do not silently fall back to executing on the login node; report that runtime validation was not run and keep remaining checks static.

### Remote Recommended Sequence

When implementing or fixing code, prefer to build or update focused test code as part of the change. This may be a unit test, a smoke test script, a tiny config, a parser check, or a reduced training/inference command that directly exercises the changed behavior.

Recommended sequence:

1. Write or update the implementation and the smallest useful test/smoke harness.
2. On the current node, run only static checks that do not import heavy runtime libraries or execute project code, such as script inspection, `bash -n`, config review, or Python bytecode compilation for files that do not trigger heavy imports.
3. After static checks pass, switch to a 1-node interactive compute node for actual runtime testing. Do this first for both training and inference work, even when the final target is distributed.
4. Inside the interactive allocation, confirm `hostname`, `cd <project_root>`, then run the focused runtime tests or smoke commands.
5. After each runtime attempt finishes, call `qstat "$PBS_JOBID"` and inspect the elapsed or used time. If the remaining walltime is not enough for the next full attempt plus cleanup, save the log path, exit the interactive shell, and request a fresh allocation instead of squeezing in another run.
6. After the 1-node path passes, expand to a 2-node interactive allocation only when the task needs distributed launch validation or multi-node behavior. Keep the 2-node run small because its maximum walltime is `00:10:00`.
7. Exit the interactive shell when validation is complete and report both static and runtime results.

If the interactive allocation cannot be obtained, leave the test code in place, report that runtime validation was intentionally skipped, and provide the exact interactive command needed to continue.

## Interactive Runtime

Start with a 1-node interactive allocation:

```bash
GROUP_ID="${GROUP_ID:-$(groups | tr ' ' '\n' | awk '/^xg/ {print; exit}')}"
GROUP_ID="${GROUP_ID:-$(groups | awk '{print $1}')}"
qsub -I -l select=1 -W group_list="$GROUP_ID" -q interact-g -l walltime=00:30:00
```

Use `walltime=01:00:00` only when the next run clearly needs more time for compilation, downloads, installation, model initialization, or a complete real-project check.

After the shell starts:

```bash
hostname
cd <project_root>
```

Continue runtime validation only if `hostname` matches `mg<number>` or the PBS environment clearly identifies a compute/debug allocation. If it still reports a login host such as `miyabi-g{1-3}`, do not run tests; exit and retry or report the allocation problem.

After every runtime attempt:

```bash
qstat "$PBS_JOBID"
qstat -f "$PBS_JOBID" | egrep 'Job Id|job_state|resources_used.walltime|Resource_List.walltime'
```

Use the `qstat` elapsed or used-time view to decide whether the remaining walltime can cover the next full attempt plus cleanup. If not, exit and request a fresh interactive allocation. Do not start another compile, model download, training run, inference run, or multi-node launch when the remaining time is tight.

For multi-node launcher debugging, always run the 1-node path first, then use the user's requested shape or this 2-node maximum:

```bash
GROUP_ID="${GROUP_ID:-$(groups | tr ' ' '\n' | awk '/^xg/ {print; exit}')}"
GROUP_ID="${GROUP_ID:-$(groups | awk '{print $1}')}"
qsub -I -l select=2:mpiprocs=1 -W group_list="$GROUP_ID" -q interact-g -l walltime=00:10:00
```

When using terminal automation, keep the interactive session open and run validation commands inside that session. End it with:

```bash
exit
```

## Runtime Validation Rules

After unit tests and focused smoke tests pass, validate the real training or inference path before submitting long jobs:

- Use the real dataset/model path when available; do not replace it with mocks for this step.
- Let the run download or read the real dataset/model as the production job would. Use existing cache locations when available.
- Cap training to 10 optimizer steps or the closest project-specific equivalent, such as `--max_steps 10`, `MAX_STEPS=10`, or a small debug config.
- For inference/evaluation, cap to a tiny real sample count and short generation length that still loads the real model/backend and writes the intended artifact.
- Inspect logs or W&B output for finite training losses and a sane trend. For inference/evaluation, inspect generated outputs and artifacts such as CSV/JSON files, and confirm the intended backend actually ran.
- If loss is NaN, exploding, missing, outputs are empty, or artifacts are absent, keep debugging rather than treating validation as passed.
- For 2-node validation, confirm both nodes participate, rank/host logs are printed, the run completes, and outputs remain sane.

Only use a lighter launcher smoke test instead of this real-project workflow when the user explicitly asks for launcher-only validation or the real dataset/model is unavailable; say so clearly.

## Validation Ladder

Prefer the smallest validation that exercises the changed behavior:

1. Test construction: create or update a focused test, smoke script, or minimal config.
2. Local workflow only: run safe local syntax, formatting, config, parser, or dependency-complete unit tests when appropriate.
3. Local workflow only: sync tracked source through a GitHub feature branch, then fetch/switch/pull on Miyabi with `--ff-only`.
4. Miyabi static checks: script inspection, `bash -n`, config/log review, or dry-run argument generation that does not import heavy modules.
5. 1-node interactive targeted checks: one focused test, a short parser command, or a minimal CPU/GPU smoke run after obtaining a PBS allocation.
6. 1-node real project check: run the real dataset/model training path for 10 steps or the real inference/evaluation path on a tiny sample in a 1-node interactive allocation. Verify loss, generated output, and artifacts are sane.
7. 2-node real distributed check: only after the 1-node path passes, run the real project distributed path in a `select=2:mpiprocs=1` interactive allocation and verify both nodes participate and loss/output/artifacts remain sane.
8. Full GPU or multi-node training/inference: submit a PBS job script instead of running directly.

Useful Miyabi commands:

```bash
qsub <job_script>
qstat
qstat -f <job_id>
qstat "$PBS_JOBID"
```

Runtime logs are written under the working directory or configured log directories. PBS stdout/stderr files commonly follow:

```text
<script_name>.o<job_id>
```

## Open MPI Environment Passing

On Miyabi, do not mix Open MPI `-x VAR` exports with the MCA environment-list mechanism (`OMPI_MCA_mca_base_env_list` or `--mca mca_base_env_list ...`). Open MPI aborts when both methods are used in the same launch.

Preferred pattern for new qsub scripts: build a shell array of `KEY=value` pairs and pass it through `/usr/bin/env` immediately before `bash -lc ...` in the `mpirun` command. This keeps the launch compatible with environments that already set `OMPI_MCA_mca_base_env_list`.

```bash
MPI_ENV_ARGS=(
  "MASTER_ADDR=$MASTER_ADDR"
  "MASTER_PORT=$MASTER_PORT"
  "PROJECT_ROOT=$PROJECT_ROOT"
)

mpirun \
  --mca mpi_abort_print_stack 1 \
  --report-bindings \
  --bind-to core \
  -np "$WORLD_SIZE" \
  /usr/bin/env "${MPI_ENV_ARGS[@]}" \
  bash -lc '...'
```

If adapting an existing project script that uses `OMPI_MCA_mca_base_env_list`, keep that mechanism or `/usr/bin/env`, but do not add `mpirun -x`.

## Final Response Discipline

When finishing a task that involved this workflow:

- State the initial hostname and which workflow was selected.
- State the branch and commit used for synchronization when local-to-Miyabi sync was used.
- State whether GitHub feature branch sync or an exceptional direct SSH copy was used.
- State whether the branch is only prepared or explicitly merged.
- If not explicitly authorized to merge, say `ready to merge` rather than merging to `main`.
- Distinguish local checks, Miyabi login-node static checks, and Miyabi compute-node runtime tests.
- State the Miyabi node type used for validation.
- If on a login node and no interactive node was obtained, say that runtime tests were intentionally skipped.
- Include the exact command names run, but avoid implying login-node runtime tests are acceptable.
