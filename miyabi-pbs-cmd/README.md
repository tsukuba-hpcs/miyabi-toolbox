# Miyabi Development

`miyabi-development` is a Codex skill for developing, testing, and debugging projects that target the Miyabi supercomputer environment.

It gives Codex a concrete workflow for moving between a local development machine, Miyabi login nodes, PBS interactive/debug nodes, and batch jobs. The skill is especially useful for GPU and distributed ML work where local checks are not enough and login-node safety matters.

## What This Skill Does

- Routes Codex behavior by hostname:
  - local machines use a local-development workflow with GitHub branch synchronization;
  - `miyabi-g*`, `miyabi-c*`, and `interact-g*` hosts use Miyabi remote-development rules;
  - PBS compute/debug nodes such as `mg<number>` are treated as runtime-capable only when the allocation is confirmed.
- Keeps Miyabi login nodes as a control plane only.
- Pushes runtime validation to PBS interactive/debug or batch compute nodes.
- Reuses one persistent 1-node interactive allocation for edit-test-debug
  cycles, unit tests, and short single-node GPU checks instead of submitting
  many extremely short jobs.
- Keeps the primary agent attached to that interactive session; a subagent is
  not needed merely to retain the allocation.
- Uses pager-safe `module avail`/`module list` discovery and reloads required
  modules inside PBS job shells.
- Provides validation ladders for local checks, login-node static checks, 1-node runtime checks, and 2-node distributed checks.
- Includes reusable PBS patterns for:
  - Python environment setup with `uv`;
  - PyTorch `torchrun`;
  - Hugging Face Accelerate;
  - vLLM inference and LoRA evaluation on Miyabi.
- Reminds Codex to avoid unsafe shortcuts such as running `pytest`, model imports, `torchrun`, `mpirun`, training, or inference directly on login nodes.

## Repository Layout

```text
miyabi-development/
├── agents/
│   └── openai.yaml
├── README.md
├── SKILL.md
└── references/
    ├── accelerate-pbs.md
    ├── git-sync.md
    ├── miyabi-operations.md
    ├── pbs-submission.md
    ├── python-env.md
    ├── torchrun-pbs.md
    └── vllm-miyabi.md
```

`SKILL.md` is the main Codex skill file, `agents/openai.yaml` provides UI metadata, and the files under `references/` are loaded only when a task needs those details.

## Installation

Install the skill by placing this directory under your Codex skills folder:

```bash
mkdir -p ~/.codex/skills
git clone <repo-url> ~/.codex/skills/miyabi-development
```

If you downloaded or copied the files manually, make sure the final layout is:

```text
~/.codex/skills/miyabi-development/SKILL.md
~/.codex/skills/miyabi-development/references/
```

Then restart Codex or start a new Codex session so the skill can be discovered.

## Basic Usage

Open Codex in a Miyabi-targeted project and ask for work that involves Miyabi, PBS, distributed execution, GPU validation, or cluster runtime debugging. For example:

```text
Use the miyabi-development skill to review this qsub script.
```

```text
Help me add a 1-node Miyabi smoke test for this training script.
```

```text
Debug this vLLM inference failure on Miyabi. Start with safe login-node checks.
```

## When Not To Use This Skill

Do not invoke this skill merely because a repository eventually runs on
Miyabi. If the user explicitly limits the task to analyzing or editing
documents or source files and excludes testing or runtime validation, work
directly in the current checkout without hostname classification, Miyabi
synchronization, or a PBS allocation.

Invoke the skill if the task later expands to tests, runtime validation,
Environment Modules, PBS, GPU/distributed execution, training, or inference.

When the skill is active, Codex should first check:

```bash
hostname
```

It then chooses the correct workflow:

- non-Miyabi host: edit locally, run safe local checks, and sync tracked source through a feature branch when Miyabi validation is needed;
- Miyabi login host: inspect, edit, run static checks, submit or inspect PBS jobs, but do not run heavy runtime commands;
- Miyabi compute/debug host: run focused tests, smoke commands, capped
  training/inference checks, or brief distributed launcher validation only
  after a valid PBS allocation is confirmed; use batch jobs for full or
  long-running workloads.

## Typical Workflows

### Local Machine To Miyabi

Use this when Codex starts on your laptop or workstation.

1. Edit code, tests, configs, or qsub scripts locally.
2. Run local checks that are safe for the local machine.
3. When Miyabi synchronization is needed, push a `codex/<task-name>` feature branch.
4. Fetch the branch on Miyabi.
5. Run static checks on the login node.
6. Request a 1-node interactive allocation for runtime validation.
7. Use a 2-node interactive allocation only after the 1-node path passes and distributed behavior matters.
8. Report results and keep the branch ready for the user to merge.

### Already On Miyabi

Use this when Codex is running in a remote Miyabi shell.

1. Check the hostname.
2. If on a login node, only inspect, edit, run static checks, and manage PBS jobs.
3. If runtime validation is needed, request one 1-node interactive allocation
   in a persistent terminal session.
4. Inside the allocation, confirm the compute hostname and run the smallest real check that exercises the changed behavior.
5. Keep the same allocation open while editing files and rerunning unit tests or
   short single-node GPU checks.
6. Check remaining walltime after each attempt. Exit to the login node when
   validation is complete or there is not enough time for another full attempt
   plus cleanup.
7. Use a batch job for complete or long-running training/inference/evaluation,
   sustained GPU use beyond the interactive limit, or multi-node work beyond a
   brief 2-node debug allocation.

## Included References

Use these files as extension points and implementation examples:

- `references/python-env.md`: project-first Python environment policy using `uv` and a project-local `.venv` when appropriate.
- `references/git-sync.md`: safe feature-branch synchronization without automatic merges or history rewriting.
- `references/miyabi-operations.md`: pager-safe module discovery, job-shell module loading, storage selection, live queue discovery, and job diagnostics distilled from the Miyabi User's Guide.
- `references/pbs-submission.md`: PBS job-script review, site-default verification, syntax checks, walltime selection, and the final `qsub` submission gate.
- `references/torchrun-pbs.md`: PBS template for `mpirun -> torchrun`, including rank setup and `MASTER_ADDR` handling.
- `references/accelerate-pbs.md`: PBS/Open MPI pattern for code that uses `Accelerator()` and reads distributed variables from the environment.
- `references/vllm-miyabi.md`: version-aware vLLM guidance, including safe CUDA UUID handling, compiler and FlashInfer diagnostics, and LoRA evaluation patterns.

## Adapting This Skill To Your Needs

This skill is written for Miyabi, but you can adapt it for a different cluster or a different Miyabi project.

### Change Host Routing

Edit the hostname patterns in `SKILL.md` if your cluster uses different login or compute node names. The important rule is to distinguish:

- local development hosts;
- login/control-plane hosts;
- interactive or compute nodes where runtime work is allowed.

### Change PBS Queues And Limits

Update the constants in `SKILL.md` if your site uses different queue names, walltime limits, group rules, or node shapes.

For example, adapt:

- interactive queue name;
- regular GPU queue name;
- 1-node and 2-node debug walltime;
- `group_list` discovery;
- `select=...:mpiprocs=...` shapes.

### Add Project-Specific Runtime Rules

If your project has fixed paths, modules, cache directories, datasets, or launcher commands, add them in a new reference file instead of hardcoding them into the main routing logic.

Example:

```text
references/my-project-runtime.md
```

Then add a short routing note in `SKILL.md` telling Codex when to read that file.

### Add New Framework References

For another framework, create a focused reference file such as:

```text
references/deepspeed-pbs.md
references/ray-pbs.md
references/jax-pbs.md
```

A good reference file should include:

- when to use it;
- safe login-node checks;
- required PBS environment variables;
- a minimal interactive/debug command;
- a batch job template;
- expected success signals;
- common failure modes.

### Keep The Main Skill Small

Use `SKILL.md` for routing, safety rules, and final-response discipline. Put framework-specific commands, large scripts, and detailed examples in `references/`.

This keeps the skill easier to maintain and prevents Codex from loading irrelevant details for every task.

## Safety Model

The main safety rule is simple: login nodes are for control-plane work, not runtime work.

Allowed on login nodes:

- file inspection and edits;
- git operations;
- qsub/qstat and log inspection;
- static checks such as `bash -n`;
- config review and dry-run command construction.

Run on PBS interactive/debug or batch compute nodes instead:

- Python tests that import project runtime code;
- heavy imports such as `torch`, `transformers`, `datasets`, `accelerate`, or `vllm`;
- model loading, training, inference, evaluation, and preprocessing;
- `torchrun`, `accelerate launch`, `mpirun`, CUDA, NCCL, and GPU profiling.

Prefer a persistent 1-node interactive allocation for iterative tests and
short GPU validation. Keep that terminal session alive across edits and test
runs. Do not create a subagent only to hold the allocation. Reserve batch jobs
for long, unattended, complete, or multi-node workloads that do not fit the
interactive debug limits.

## Contributing

When extending this skill:

- keep the machine-facing skill name as `miyabi-development`;
- keep long YAML descriptions in folded block style with `description: >-`;
- avoid user-specific absolute paths in shared instructions;
- prefer placeholders such as `<project_root>`, `<group_id>`, and `<config.yaml>`;
- keep examples small enough for debug allocations;
- add a reference file for new frameworks instead of expanding `SKILL.md` indefinitely.
