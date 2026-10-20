# Architecture and contracts

The control plane, stated as contracts. Each item is **verified** (it worked on
Miyabi-G in an earlier implementation) unless marked **unverified** (designed,
or unit-tested only). `<R>` is an experiment root: one per experiment, holding
`control/`, `runs/`, `snapshots/`, `stats/`, `caches/` and the experiment's
data. `<pkg>` is your Python package. How to build it:
[implementation-guide.md](implementation-guide.md).

## Contents

1. Components
2. Spec
3. Tasks
4. State store
5. Pilot protocol
6. Task executor
7. Releases, science versions and canaries
8. Operator commands
9. Parallel shapes

## 1 Components

| Component | Responsibility | Runs on |
| --- | --- | --- |
| spec | declares the experiment: models, data, numeric settings, speed settings, stage budgets, seeds, search spaces, selection rule, smoke matrix, initial cost estimates | YAML in the repository |
| controller | expands the spec, imports results, classifies attempts, selects, writes queue files, submits pilots, appends statistics, deletes by consumers | login node, one process per root |
| state store | tasks, attempts, pilots, selections, artifacts, results, cost estimates, releases, events | `<R>/control/state.sqlite` |
| pilot | bad-node gate, claim, execute on the release snapshot, evaluate in place, exit | PBS job ([`assets/pilot.pbs`](../assets/pilot.pbs)) |
| task executor | turns one task config into the declared outputs | `python -m <pkg>.run <config.json>`, started by the pilot |
| engines | the science: training, evaluation, statistics | called by the executor |
| method registry | per method: name, science version, hyperparameters, extra inputs | one module |
| science versions and canaries | tell numerically different code apart; catch numeric changes without a version bump | a versions module, canary tests |
| cost model | expected seconds, walltime basis, cost gate, stage wall-clock bounds | inside the controller |

Adding a method should mean one registry entry, one spec entry and one test.
Without a registry, adding one baseline touched 15–20 places, and one
experiment's methods changed 79 files.

## 2 Spec

One YAML per experiment. Keep **numeric settings** (they change results) apart
from **speed settings** (they do not). Mixed together, nobody can tell whether
a change needs a science-version bump or whether old results still count.

| Group | Contents | Changeable mid-experiment? |
| --- | --- | --- |
| `models` | model IDs with pinned revisions | no; that is a new experiment |
| `data` | per-stage data roots, relative to `<R>` | no |
| `training` | numeric: micro-batch, compile settings (`pad_multiple`), the batch of a method's own generation step (e.g. rewriting training data once) | fixed before the first development result is used; afterwards only with a science-version bump |
| `speed` | not numeric: evaluation batch, lanes per GPU, threads | any time; the next queue file carries it |
| `evaluation` | benchmarks; which checkpoints each protocol evaluates | no (it is the comparison protocol) |
| `budgets` | per stage (smoke, screen, confirm, trial, formal): data size, steps, warmup, evaluation fraction | until that stage's results are read |
| `seeds` | per stage; development and formal seeds disjoint | no |
| `selection`, `search` | selection rule, promotion counts, exhaustive limit, TPE settings; search spaces | until that cell selects |
| cells | model × protocol × method (a protocol is a training procedure several methods share, e.g. single-round vs multi-round; one `default` when there is only one); `carried` selections from earlier work; `warm_start` points | add any time; removing one cancels its unstarted tasks |
| `formal` | the formal method set | add any time |
| `smoke` | smoke variants; speed probes such as `eval_batch_sizes` | any time |
| `pilot_walltime_s`, `max_walltime_s` | per stage: fixed walltime used when it holds the work; cap | any time |
| `max_pilots`, `debug_pilots` | this experiment's share of the project's job limit; slots kept for debug-g | any time |
| `estimates`, `expected_seconds` | initial per-part estimates; per-cost-class overrides | any time |
| `debug`, `retain_*` | keep logs and samples; keep outputs beyond their consumers | any time |
| `after` | per stage, the smoke variants that must succeed first | any time |
| `not_yet_in_<version>` | planned steps the platform cannot run yet, listed explicitly | — |

A layout example (illustrative values; name the fields as your code expects).
Comment every fixed value with the reason it was chosen:

```yaml
models:
  qwen7b: {id: Qwen/Qwen2.5-7B-Instruct, revision: <commit>}
data: {train: data/train, eval: data/eval}          # relative to <R>
training: {micro_batch: 4, pad_multiple: 64}        # numeric: chosen in the smoke, then fixed
speed: {eval_batch: 16, eval_lanes_per_gpu: 2}      # not numeric: change any time
evaluation: {benchmarks: [gsm8k, mmlu, ifeval], checkpoints: [final]}
budgets:
  smoke: {fraction: 0.1}
  screen: {steps: 400, eval_fraction: 0.25}
  formal: {steps: 4000}
seeds: {development: [101, 102], formal: [201, 202, 203]}
selection: {rule: mean_development_score, promote: 2}
search: {exhaustive_max: 8, tpe: {proposals: 8, pending: 4}}
cells:
  - {model: qwen7b, method: base}
  - {model: qwen7b, method: candidate, space: {lr: [1.0e-4, 2.0e-4, 4.0e-4]}}
formal: [base, candidate]
smoke: {variants: [default], eval_batch_sizes: [8, 16, 32]}
after: {screen: [default], formal: [default]}
pilot_walltime_s: {smoke: 1800}
max_walltime_s: {formal: 36000}                     # the user's limit: a check, never the request
max_pilots: 12
debug_pilots: 1
```

## 3 Tasks

A task definition is plain data that contains everything needed to run it.
Only the code release and the speed settings bind when a pilot claims it.

| Field | Meaning |
| --- | --- |
| `task_id` | stage, model, protocol, candidate ID (a deterministic hash of the parameters), seed; no `/`. The same definition always gets the same ID |
| `kind` | `train`, `evaluate`, `postprocess` (single-GPU processing of a source training's outputs: merge, export, re-derivation from saved intermediates), `command` (tests, imports, data builds, cleanup; `{root}` stands for `<R>`). New kinds go into the executor's kind table |
| `stage`, `cell`, `method`, `seed`, `params`, `budget`, `training`, `model` | scientific content |
| `nodes` | resource shape |
| `deps` | tasks that must be `done` before this one becomes `ready` |
| `outputs` | outputs to keep, e.g. `adapters: [final]`; nothing undeclared is written |
| `inplace` | evaluations or post-processing that run in the source task's allocation |
| `save_intermediate` | keep named intermediate outputs (e.g. per-worker endpoints) for a declared later task |
| `reuse` | set by the controller: bound to another task's identical training, as `alias` (copy its result) or `rederive` (rerun only the cheap post-processing from its saved intermediates on one GPU); ignored when `apply` compares definitions |
| `speed` | per-task speed override, for probes only |
| `origin` | `spec`, `selection`, `release` or `manual`; decides whether `apply` may cancel it |

**Task states:** `planned` (waiting on dependencies) → `ready` → `running` →
`done`; also `failed`, `cancelled`, and `stale` (its definition or science
version changed; resolve with `rerun`, or `accept` with evidence). A hold is not
a state: it lives in `meta` and only keeps tasks out of the queue files.

**Attempt outcomes:** `running`, `done`, `failed_infra` (ended before the first
optimizer step), `failed_science` (failed after it), `lost` (walltime used up).

## 4 State store

SQLite in rollback-journal mode (`PRAGMA journal_mode=DELETE`), not WAL: WAL
needs shared memory, which a network file system does not provide. Only the
controller writes, holding `control/controller.lock` (`fcntl`; a cycle that
cannot take it is skipped). Every step is an idempotent transaction, so the loop
can be killed and restarted at any point.

| Table | Contents |
| --- | --- |
| `meta` | holds, published science versions, fitted cost parts, last-cycle summary |
| `spec_revisions` | every applied spec with its git commit |
| `tasks` | the task matrix |
| `attempts` | one row per claim: job, host, claim, start and end times, commit, science key, outcome, cost |
| `pilots` | PBS view: job, shape, queue, walltime, state, used seconds, resubmission source, claims |
| `selections` | each selection's ranked inputs and result |
| `artifacts` | files kept on /work: file count, bytes, consumers, retention, deletion time |
| `results` | per finished task a compact summary (training without per-worker detail; evaluation: metrics, time, memory). Selection reads this, never run directories |
| `estimates` | longest measured seconds and run count per cost class |
| `releases` | requested commits, test task, state, science versions |
| `events` | append-only audit trail |

Pilots writing SQLite directly was rejected: it relies on correct locking over
a network file system, and a corrupted store stops the whole experiment.

## 5 Pilot protocol

**Submission.** The controller submits one pilot per runnable queue entry that
no open, still-unclaimed pilot of the same shape and queue covers
([scheduling-and-cost.md](scheduling-and-cost.md) §3). `qsub -v` passes the
root, the walltime in seconds, the queue and the pilot script path; a release
test also passes its snapshot. The job script runs the compute-node guard
(`miyabi-pbs-cmd` `context.py --require-compute`), loads modules, sets the
compiler, venv and `HF_*` variables, `cd`s into the snapshot and `exec`s the
pilot module with `PYTHONPATH=<snapshot>/src`
([`assets/pilot.pbs`](../assets/pilot.pbs)). The pilot gives each task the
rest: threads, `CUDA_VISIBLE_DEVICES`, compile caches in the task's `/local`
directory ([hf-pytorch-practice.md](hf-pytorch-practice.md) §3).

**On start:**

1. **Bad-node gate** (drilled with a fake node file and a stub `qsub`; never hit
   live). If the node file contains a known bad node: resubmit the same script
   and resources once, marked as a resubmission so it can never loop; write
   `control/gate/<job>.json` (original, successor, nodes); exit 79. Nothing was
   claimed, so no record changes and nothing counts as a failure.
2. **Read the queue** `control/queue/n<nodes>.json`: runnable tasks in priority
   order, each with its full definition, expected seconds and speed settings.
   The header names the current release; a release-test entry carries its own
   candidate snapshot. A pilot that starts before any release exists exits; a
   multi-node one first waits up to 10 minutes for it (unverified; the
   template does this).
3. **Claim** the first task whose expected seconds × 1.2 fit the remaining
   walltime − 90 s (generation-heavy evaluations need more:
   [scheduling-and-cost.md](scheduling-and-cost.md) §4), by creating
   `control/claims/<task_id>` with `os.link` (content: job, host, time). If the
   link exists, try the next.
4. **Run** `python -m <pkg>.run <config>` from `snapshots/<commit>/`. Create
   `claims/<task_id>.started` when the first optimizer step (for an evaluation,
   the first scored batch) completes. On a non-zero exit write
   `claims/<task_id>.failed` with the exit code and whether the pilot had
   already received PBS's stop signal (the controller classifies on it), and
   keep the last 300 lines of each failed rank's log beside the task's
   outputs.
5. **Evaluate in place.** When a training succeeds, immediately claim its
   declared evaluations that fit the remaining time and run them on free GPUs —
   locally, or with `mpirun -np 1 --host <h>` on another node. Shardable
   benchmarks split the idle nodes evenly, one contiguous slice of questions
   per node, merged into the same files an unsharded run writes. Evaluations
   that do not fit stay unclaimed for later 1-node pilots. Size the pilot for
   training + waves × the longest in-place chain. Keep only work that uses the
   allocation's width in place; a long serial tail idles the other nodes
   ([experiment-lifecycle.md](experiment-lifecycle.md) §3).
6. **Continue or exit.** Exit as soon as nothing runs and nothing queued fits.
   On exit remove `/local/$USER/<pkg>/<job>` on every host, and the job's
   /work scratch directory unless a task failed.

Pilots are interchangeable: an extra pilot costs at most one empty start, never
a duplicate execution. Deleting a queued pilot needs no bookkeeping: an attempt
exists only once something was claimed.

## 6 Task executor

`python -m <pkg>.run <config.json>`. The pilot writes the config:

| Field | Contents |
| --- | --- |
| `task` | the task definition |
| `root`, `output_dir`, `data_root` | experiment root; run directory (for an evaluation, the source training's); data root |
| `commit`, `snapshot`, `science_key` | code identity |
| `speed` | speed settings |
| `started_marker` | path of the `.started` file |
| `local_root`, `local_dir`, `launch_root` | the job's and the task's directories on `/local`; coordination directory on /work for multi-node launches |
| `deadline_s` | seconds this task may use |

The executor:

- refuses to run outside a compute allocation (host name and `PBS_JOBID`);
- dispatches on `kind`;
- on exit, exceptions included, logs `[local-cache] bytes=… files=…` and
  deletes `local_dir`;
- writes `runs/<task>/result.json` with at least `task_id`, `status`, `kind`,
  `stage`, `science_key`, `code_commit`, `job_id`, `wall_seconds`, `speed`,
  `environment` and per-part `timing`; an evaluation's summary file is its
  result;
- exits 0 on success and non-zero on failure.

Write progress lines from code straight into `train.log`. Processes whose
stdout a framework swallows leave no trace otherwise (some frameworks send a
server process's output only to their own log command).

The controller reads result metadata (status, time, science key); it never
reads evaluation scores.

## 7 Releases, science versions and canaries

- **Release.** `release <commit>` exports `snapshots/<commit>/` with `git
  archive` (src, scripts, configs, tests, `pyproject.toml`, `uv.lock`, a
  `COMMIT` file) and queues a `command` task that runs the control-plane and
  canary tests on a compute node. When it passes, `control/release.json` points
  at the snapshot. Tasks that start afterwards use it; running tasks keep
  theirs. Working-tree edits never take effect by themselves. A newer release
  request supersedes one still under test, so a late test cannot swap old code
  back in. Release-test pilots bypass `--max-queued`.
- **Science versions** (unit-tested). `DATA`, `TRAIN`, `EVAL` and one integer
  per method. Bump only for changes that alter numbers. Every result records
  its `science_key` (for example `D1.T1.candidate2`, `E1`). Publishing a new key
  marks finished tasks of the old key `stale`; selection and statistics refuse
  stale results and refuse to mix keys within one comparison.
- **Canaries** (golden files generated on compute nodes; run in every release
  test). Per method, a tiny random model on CPU, one thread, fixed seed: three
  optimizer steps, plus any post-processing step the method has, compared with
  `golden.json` at relative tolerance 1e-6. Output changed without a version
  bump → the release test fails. Regenerate golden files only through an
  explicit switch after a bump.
- **Blind spots.** Evaluation parsing, data preparation and generation steps
  need their own fixed-sample canaries (unverified).
- **Environment.** Every result records the versions of the numeric packages
  (torch, transformers, peft, trl, kernel packages). A kernel that passed the
  numeric check ([hf-pytorch-practice.md](hf-pytorch-practice.md) §4) is an
  environment fact, not a science version.
- **Closing audit.** Per comparison unit, list the commits used and their diff
  on the scientific paths, and the evaluation batch sizes used; a human checks
  it once.

## 8 Operator commands

The commands to build (the earlier implementation's set). On the login node,
with `PYTHONPATH=src` and the experiment's venv:

```bash
python -m <pkg>.control --root <R> apply <spec.yaml> [--yes]   # show the diff, then write
python -m <pkg>.control --root <R> release <commit>
python -m <pkg>.control --root <R> cycle [--no-submit] [--max-queued N]
python -m <pkg>.control --root <R> loop --interval 120 --hours 24 --max-queued 2
python -m <pkg>.control --root <R> status [--stage S]          # at most 40 lines
python -m <pkg>.control --root <R> estimate                    # cost model, gate, stage bounds
python -m <pkg>.control --root <R> hold|resume [--all] [--stage S] [--method M] [--cell C]
python -m <pkg>.control --root <R> rerun|accept <task_id>... --reason TEXT
python -m <pkg>.control --root <R> withdraw --stage S --cell C --reason TEXT
```

Run one `loop` per root as a background job of the session. It restarts itself
with the new code when the package changes: a loop that kept the rules it
started with once requested 4 h jobs past a 3 h cap and ran 7 jobs against a
share of 6. Wait with a watcher that wakes the session when `status` changes
(or `miyabi-pbs-goal-wakeup` for one long job), not with a polling loop in the
conversation.

Record decisions, not facts: one short entry per debatable choice (question,
options, choice, reason, evidence, reversibility). Facts are queries of the store.

## 9 Parallel shapes

| Shape | Use | Launch | Status |
| --- | --- | --- | --- |
| `n1` | single-GPU tasks: tests, imports, single-GPU training, separate evaluations | directly on the pilot's host | verified |
| `nK` lanes | K nodes each running single-GPU tasks, in-place evaluations included | `mpirun -np 1 --host <h> /usr/bin/env K=V … python -m <pkg>.run` | verified |
| sharded evaluation | one benchmark's questions split over K nodes, merged afterwards | coordinator runs `mpirun -np K --host h1,…,hK …`; the MPI rank is the shard | verified on 2 nodes: per-question results identical, wall −28% on 9,815 items |
| one role per node, `n(N+1)` | multi-role protocols: a coordinator and N workers (e.g. federated learning) | `mpirun --map-by ppr:1:node`, one port per run | verified |
| lanes per GPU | several evaluations or shards on one GPU | `eval_lanes_per_gpu` | verified on one GPU (Qwen, batch 16): 1 lane 59 q/s, 2 lanes 83 q/s at 39 GB, 4 lanes 93 q/s at 76 GB |
| DDP / FSDP | one task over several nodes | `mpirun` one process per node starting `torchrun`, or `torch.distributed` from MPI variables | unverified |
| MIG | tiny tests | `debug-mig` and friends | unverified |

Only shard benchmarks whose metrics can be recomputed from per-question
predictions; check each shard against the harness, compute the class-level
metrics on the merged set.

Pack or run in waves. `nK` lanes pay when more single-GPU tasks should run at
once than the experiment's job share allows. For k waves of tasks of length T,
compare one packed wave (wait of the K-node shape + T) with 1-node waves
(1-node wait + k·T), using waits measured at the walltime you will request:
in a 2026-10-18 → 20 window, 11-node regular-g pilots waited a median 0.85 h
and 1-node ones 0.48 h, for median runs of 1.4 h and 0.9 h
([platform-facts.md](platform-facts.md) §4). Put tasks of one cost class in a
packed pilot so its nodes finish together, and keep long serial tails out of
it ([experiment-lifecycle.md](experiment-lifecycle.md) §3). 1-node waves keep
a decision point between waves, which a staged stop rule may need.

Look at per-node GPU use by role in training-only jobs (where PBS's
last-hour window covers the training): a coordinator used 1–2% of its GPU
while its workers used 48–72%, so one node in eleven idled. Co-locating the
coordinator with a worker is a candidate (unverified: needs a canary check and
a round-time measurement).

Measure an engine's fixed per-round cost apart from its per-step cost. An
engine that started a worker process per message spent ~45–56 s fixed per
round, ~27 s of it on imports and model loading. Overlap work that leaves GPUs
idle: validating round r while the workers train round r + 1 cut rounds from
128–134 s to 81–92 s without changing any number.
