---
name: miyabi-development
description: >-
  Develop, test, and debug Miyabi-targeted projects from a local workstation, a
  Miyabi login shell, or a PBS compute allocation. Use when work requires
  Miyabi PBS/qsub, Environment Modules, GPU or distributed runtime validation,
  CUDA/NCCL, MPI, torchrun, Hugging Face Accelerate, vLLM, training, inference,
  environment setup, or cluster runtime diagnosis. Route work by hostname plus
  PBS context, keep login nodes control-plane only, and choose interactive or
  batch compute nodes safely. Do not invoke for requests explicitly limited to
  analyzing or editing documents or source files with no testing or runtime
  validation, unless the user explicitly requests this skill.
---

# Miyabi Development

## Skip File-Only Work

If the user explicitly limits the task to analyzing or editing documents or
source files and excludes testing or runtime validation, skip the rest of this
skill. Work directly in the current checkout; do not run `hostname`, load the
Miyabi references, synchronize to Miyabi, or request a PBS allocation solely
because the project targets Miyabi.

If the scope later expands to tests, heavy imports, runtime validation, PBS,
modules, GPU/distributed execution, training, or inference, resume this skill
and classify the host before running those commands.

## Start By Classifying The Host

Run before choosing a workflow:

```bash
hostname
printf 'PBS_JOBID=%s\nPBS_NODEFILE=%s\n' "${PBS_JOBID:-}" "${PBS_NODEFILE:-}"
```

Classify the current shell:

- **Local**: the hostname does not match `miyabi-g*`, `miyabi-c*`, or
  `interact-g*`, and the shell is not an allocated `mg<number>` node. Use the
  Local Workflow.
- **Miyabi control plane**: the hostname matches `miyabi-g*` or `miyabi-c*`, or
  an `interact-g*` entry has no confirmed PBS allocation. Use the Remote
  Workflow but keep all work static.
- **Miyabi compute/debug**: the hostname matches `mg<number>` and PBS evidence
  such as `$PBS_JOBID` or `$PBS_NODEFILE` confirms an allocation. Use the
  Remote Workflow and allow runtime validation.

Treat an `mg<number>` shell without PBS evidence as untrusted until the
allocation is confirmed. For any unknown host, inspect the active shell,
`$PBS_JOBID`, `$PBS_NODEFILE`, and project documentation; keep runtime work off
the host while classification remains uncertain.

## Discover Project Rules

Inspect the repository before proposing commands. Prefer current project
evidence over generic defaults:

- `AGENTS.md`, `README*`, `for_codex/`, and environment files;
- `shells/miyabi/`, existing qsub scripts, site user guides, and recent PBS
  logs;
- `pyproject.toml`, `uv.lock`, `.python-version`, `.venv/`, and module setup;
- current git branch, worktree state, remotes, and user notes.

Do not hardcode project paths, accounts, groups, modules, models, datasets, or
cache directories unless the repository or user provides them.

## Load Only Relevant References

- Local-to-Miyabi tracked-source synchronization: read
  `references/git-sync.md`.
- Module discovery/loading, job-shell environments, storage, queue discovery,
  or job diagnostics: read `references/miyabi-operations.md`.
- PBS batch job script creation, review, or submission with `qsub`: read
  `references/pbs-submission.md`.
- Python environment creation or dependency installation: read
  `references/python-env.md`.
- PyTorch distributed or `torchrun`: read `references/torchrun-pbs.md`.
- Direct `Accelerator()` use or `accelerate launch` under PBS/MPI: read
  `references/accelerate-pbs.md`.
- vLLM, LoRA evaluation, CUDA device visibility, Triton, or FlashInfer: read
  `references/vllm-miyabi.md`.

Load the smallest relevant combination. Keep framework-specific commands in
their reference instead of copying them into this file.

## Enforce Safety Invariants

- Treat Miyabi login nodes as a control plane only.
- Run heavy imports, project tests, preprocessing, model loading, training,
  inference, distributed launchers, CUDA/NCCL, and GPU profiling only in a
  confirmed PBS compute allocation.
- Allow login nodes to inspect and edit files, manage git, review configs and
  logs, run static shell checks, and submit or inspect PBS jobs.
- Do not treat local success as Miyabi runtime validation when behavior depends
  on PBS, modules, cluster paths, MPI, CUDA, NCCL, or GPUs.
- Treat module state as shell-local. Discover and inspect modules on the login
  node, then load the required modules again inside every batch or interactive
  compute-node shell before running project code.
- Prefer the smallest check that proves the changed behavior. Do not allocate
  more nodes or walltime than the next complete attempt needs.
- Prefer one persistent 1-node interactive allocation for iterative
  development, `pytest` or other unit tests, and short single-node CPU/GPU
  checks. Reuse that allocation across edit-test-debug cycles instead of
  submitting many extremely short jobs.
- Do not create a subagent merely to obtain or retain an interactive
  allocation. Keep the primary agent attached to one persistent terminal
  session; delegate only genuinely independent work, and designate exactly one
  agent as allocation owner if delegation is otherwise justified.
- Preserve existing user changes. Do not merge to `main`, rewrite history,
  force-push, or overwrite tracked source by direct copy without explicit user
  authorization.

## Choose Interactive Or Batch Runtime

Use a persistent 1-node interactive allocation for focused tests, iterative
debugging, short single-node GPU checks, and capped real-path smoke tests that
fit within the interactive walltime. Keep the allocation open after each test,
edit the shared project files as needed, and rerun validation in the same
compute-node shell.

Submit a batch job only when the workload needs unattended or durable
execution, a complete training/inference/evaluation run, sustained GPU use that
cannot fit comfortably within the interactive limit, or multi-node execution
beyond the brief 2-node interactive debug path. A tiny real-model or
real-dataset smoke test does not require a batch job merely because it uses the
real runtime path.

## Local Workflow

Use this sequence from a local workstation:

1. Inspect the repository, worktree, branch, and local instructions.
2. Implement the change and add the smallest focused test, smoke harness, or
   config that exercises it.
3. Run safe local format, syntax, parser, and dependency-complete unit checks.
4. If cluster-dependent validation is needed, read `references/git-sync.md`,
   synchronize tracked source through a feature branch, and record the exact
   branch and commit.
5. On a Miyabi login node, fetch with fast-forward-only behavior and run only
   static checks.
6. Request a 1-node allocation and run the focused runtime check inside it.
7. Expand to 2 nodes only when distributed behavior matters and 1 node passes.
8. Push follow-up commits through the same branch. Leave the branch ready for
   the user to merge unless merge authorization was explicit.

Skip branch creation for read-only inspection, disposable scratch work, or a
small edit that does not require synchronization. Use direct SSH copy only for
logs, scratch artifacts, or files intentionally outside tracked source.

## Remote Workflow

Before any command that may execute project code or import runtime libraries,
re-run `hostname` and confirm the node class.

On a login/control-plane host, limit work to:

- reading, searching, and editing files;
- `git status`, diffs, fetches, and branch switching;
- qsub/qstat operations and log inspection;
- pager-safe `module avail`, `module list`, `module help`, and `show_module`
  inspection;
- `bash -n`, config review, and other checks that do not import or execute the
  project runtime.

Do not run `pytest`, ad hoc project Python, `torch`, `transformers`, `datasets`,
`accelerate`, `vllm`, model/data loaders, `torchrun`, `mpirun`, training,
evaluation, inference, or preprocessing on a login node. If an allocation is
unavailable, stop after static validation and report the exact unrun check.

On a confirmed compute/debug node:

1. Confirm `hostname`, `$PBS_JOBID`, and the project root.
2. Load the exact required modules in this compute-node shell and capture
   `module list` without a pager.
3. Run the focused test before the full application path.
4. Keep this same interactive shell alive while editing, rerunning `pytest`,
   and iterating on short single-node GPU checks. Project files are on shared
   storage, so normal Codex file-editing tools and the retained compute shell
   can participate in the same edit-test loop.
5. Check remaining walltime after every attempt.
6. Preserve logs and artifacts before leaving the allocation.
7. Exit to the Miyabi login node only when runtime validation is complete or
   the remaining walltime cannot cover another full attempt plus cleanup.

## Request Interactive Runtime

Start with one node in a persistent terminal/PTY session:

```bash
GROUP_ID="${GROUP_ID:-$(groups | tr ' ' '\n' | awk '/^xg/ {print; exit}')}"
GROUP_ID="${GROUP_ID:-$(groups | awk '{print $1}')}"
: "${GROUP_ID:?Set GROUP_ID explicitly}"
qsub -I -l select=1 -W group_list="$GROUP_ID" -q interact-g -l walltime=00:30:00
```

After the shell starts:

```bash
hostname
printf 'PBS_JOBID=%s\nPBS_NODEFILE=%s\n' "${PBS_JOBID:-}" "${PBS_NODEFILE:-}"
cd <project_root>
```

Continue only when the shell is a confirmed PBS compute allocation. Before any
runtime command, follow `references/miyabi-operations.md`: disable module
pagers, inspect the compute-node defaults, load the required modules in this
shell, and verify the result with `module list`.

Keep using this same terminal session after each command. If the terminal tool
returns a session identifier, retain and resume that identifier; do not start a
new `qsub -I` for every test. Do not exit merely because one test or edit-test
cycle finished.

After each attempt, inspect used and requested walltime:

```bash
qstat "$PBS_JOBID"
qstat -f "$PBS_JOBID" | grep -E 'Job Id|job_state|resources_used.walltime|Resource_List.walltime'
```

Request two nodes only after the 1-node path passes and the task requires it:

```bash
GROUP_ID="${GROUP_ID:-$(groups | tr ' ' '\n' | awk '/^xg/ {print; exit}')}"
GROUP_ID="${GROUP_ID:-$(groups | awk '{print $1}')}"
: "${GROUP_ID:?Set GROUP_ID explicitly}"
qsub -I -l select=2:mpiprocs=1 -W group_list="$GROUP_ID" -q interact-g -l walltime=00:10:00
```

Do not start another compile, download, model load, training run, inference run,
or multi-node launch unless the remaining walltime covers the full attempt plus
artifact preservation and cleanup.

When walltime is close to exhaustion, save logs and required artifacts, stop
any background processes, and `exit` cleanly to the Miyabi login node. Continue
static editing there; request a fresh interactive allocation only if more
runtime validation remains.

## Climb The Validation Ladder Deliberately

Stop at the smallest level that proves the change; continue when the changed
behavior depends on the next level:

1. Focused test, smoke script, parser check, or minimal config.
2. Safe local syntax, formatting, and unit checks.
3. Miyabi login-node static inspection and shell/config checks.
4. 1-node targeted runtime test in an allocation.
5. 1-node real training/inference path with a tiny workload.
6. 2-node real distributed path when multi-node behavior changed.
7. Full or long-running training/inference/evaluation, sustained GPU use, or
   multi-node execution beyond brief debug validation in a batch job.

For a real training check, use the actual model/data path when available and
cap work at about 10 optimizer steps or the closest project equivalent. For
inference/evaluation, use a tiny real sample and short generation length.
Verify finite loss, non-empty outputs, expected artifacts, the intended
backend, and participating ranks/hosts. Treat NaN loss, empty output, missing
artifacts, or absent ranks as failures.

## Preserve MPI Launch Correctness

Do not mix Open MPI `-x VAR` exports with
`OMPI_MCA_mca_base_env_list` or `--mca mca_base_env_list ...`; Open MPI can
abort when both mechanisms are active. Prefer `/usr/bin/env`:

```bash
MPI_ENV_ARGS=(
  "MASTER_ADDR=$MASTER_ADDR"
  "MASTER_PORT=$MASTER_PORT"
  "PROJECT_ROOT=$PROJECT_ROOT"
)

mpirun \
  --mca mpi_abort_print_stack 1 \
  --report-bindings \
  --bind-to none \
  -np "$WORLD_SIZE" \
  /usr/bin/env "${MPI_ENV_ARGS[@]}" \
  bash -lc '...'
```

Do not pin an MPI supervisor to one core when it will spawn multiple local
workers unless the project explicitly designs and verifies that affinity.
Follow existing site launcher conventions when they are stricter.

## Report The Result Precisely

Include:

- initial hostname and selected workflow;
- branch and commit used for local-to-Miyabi synchronization, if any;
- GitHub feature-branch sync or the reason for exceptional direct copy;
- whether the branch is merely prepared or explicitly merged;
- local checks, login-node static checks, and compute-node runtime checks as
  separate categories;
- compute node/allocation shape used, or the exact runtime check intentionally
  left unrun;
- relevant command names, logs, artifacts, and remaining risks.
- modules loaded in the compute-node job shell when runtime behavior depends on
  the software stack.

Say `ready to merge` instead of merging to `main` when authorization is absent.
