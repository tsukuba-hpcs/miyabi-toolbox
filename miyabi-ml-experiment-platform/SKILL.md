---
name: miyabi-ml-experiment-platform
description: Design, build or audit the platform that runs ML experiments (train → evaluate → statistics) on Miyabi-G PBS — late-binding pilot jobs, a single-writer controller, walltime from measured smoke runs, cost gates, in-place evaluation, artifact retention under byte and file-count quotas, streaming statistics, science versions — and plan experiments on it. Use when setting up or redesigning an experiment system or pipeline on Miyabi; planning a training/evaluation/HPO study there (stages, seeds, smoke, target-scale trials or pilot studies, stop rules, cost estimates); choosing job shapes, queues, walltime or job packing; deciding what to save, clean or tabulate; or diagnosing slow, idle or wasteful Miyabi experiments, even if only one of these is named. Also 在Miyabi上搭建/改造实验框架、实验平台、实验流程、冒烟、成本估算、walltime、作业打包、评测、清理、统计. Not for raw qsub/qstat syntax (miyabi-pbs-cmd), one job script (miyabi-pbs-shell-template) or waiting on a job (miyabi-pbs-goal-wakeup).
---

# Miyabi ML Experiment Platform

Build and run the system behind train → evaluate → statistics experiments on
Miyabi-G without wasting queue time, node-hours, quota or conclusions. The
rules come from building and running such a platform for multi-model
training and evaluation studies on Miyabi-G in October 2026 (about 4,850
node·h). Each rule quotes the cost that taught it, so you can judge whether it
applies to your case; "one study" means one of those experiments. Most
measurements come from 11-node jobs (one coordinator and ten workers) and
1-node jobs.

The skill stands alone. When the sibling skills are installed they supply the
mechanics: `miyabi-pbs-cmd` (qstat/qsub dialect, the compute-node guard),
`miyabi-pbs-shell-template` (job scripts, modules, `mpirun`) and
`miyabi-pbs-goal-wakeup` (sleeping through long jobs). Without them, use the
commands given here and in the references.

No implementation ships with the skill: the operator commands named below
(`apply`, `release`, `loop`, `status`, `estimate`, `hold`, …) are the ones
[architecture.md](references/architecture.md) §8 specifies for you to build.
Placeholders: `<R>` an experiment root, `<exp>` an experiment name, `<pkg>`
your Python package, `<group>` your project group, `EXP_` the prefix of the
platform's environment variables. Set `MIYABI_ML_PLATFORM_ROOT` to the
absolute directory containing this SKILL.md; the commands below use it.

Where a mechanism's status matters it is labeled **verified** (worked on
Miyabi-G in an earlier implementation) or **unverified** (designed, or
unit-tested only).

## What drives the design

- **Resources are queue positions, not compute.** In one study's first stage
  (855 jobs, walltime guessed), 11-node jobs ran a median 16 min and waited a
  median 3.6 h (p90 10.4 h, max 24.5 h), about 13× the run. Every serial stage
  adds a wait; every resubmission loses a position.
- **Requested walltime decides backfill.** Those jobs used a median 0.17 of the
  walltime they requested (4.8× over in node·h), and the 11-node ones could not
  backfill. Resizing 96 queued jobs to 1.5× measured cut their wait from a
  PBS-predicted median 35.8 h to an actual 5.8 h.
- **The project limit counts jobs.** 16 running and 128 accepted jobs per
  project (`qstat --limit`), shared by all its experiments. One-node jobs can
  use at most 16 GPUs: pack single-GPU work into multi-node allocations when
  more of it should run at once; for a few waves of long tasks, weigh one
  packed wave against 1-node waves ([architecture.md](references/architecture.md) §9).
- **One GH200 per node** (aarch64, ~95 GiB device memory): node·h = GPU·h.
- **/work limits file counts as well as bytes.** One study swept 3.37 M
  compile-cache files (240 GB); one search run left 216 files. `/local` (NVMe,
  ~900 GB per node) is free job scratch.
- **Login nodes** edit files, query metadata and operate PBS; anything that
  imports torch, runs tests, compiles or scans large trees needs an allocation.
- **Plans change mid-run.** One study changed course eight times in three days
  (search strategy, bad-node guard, walltime policy, pausing an experiment,
  dropping a slow baseline, …). With code, task and walltime bound at `qsub`,
  each change meant qdel and resubmit: 389 of 393 archived attempts never started.

Numbers drift. Re-check them with the commands in
[platform-facts.md](references/platform-facts.md) before a decision rests on one,
and measure your own history with `scripts/pbs_time_report.py` (below).

## How much platform to build

| Workload | Build |
| --- | --- |
| A probe, a benchmark run, a handful of jobs | One job script (`miyabi-pbs-shell-template`, or the module and compiler lines of [`assets/pilot.pbs`](assets/pilot.pbs)) with the cache and thread variables of [hf-pytorch-practice.md](references/hf-pytorch-practice.md) §3. Still: walltime 1.5× measured, caches on `/local`, only needed outputs on /work, results appended to one table |
| A fixed matrix of tens of long single-GPU runs, no search, few changes expected | Job scripts driven by a task manifest, packed so the job-count limit is not exhausted, keeping the contracts that matter: code bound at job start from a tested release, claims by `os.link`, `.started` and failure classes, walltime 1.5× measured, `/local` caches, results ingested as they land. Move to the control plane once searches, hundreds of tasks or frequent plan changes appear |
| A study: methods × models × seeds in stages (smoke → search → formal → evaluation → statistics), tens to thousands of jobs, changes likely mid-run | The control plane below, built to the contracts in [architecture.md](references/architecture.md) in the order of [implementation-guide.md](references/implementation-guide.md); a small study can leave out TPE, reuse, sharding and compile-cache archives. An earlier control plane was reworked twice before this design settled |

## Principles

1. **Submit early, bind late.** A PBS job declares only a resource shape. When
   it starts, it decides which task to run, with which released code and which
   speed settings. Changing code, configuration or the method set never
   touches the queue.
2. **Fix only the comparison and the record.** Data, budgets, seeds, selection
   rules and the evaluation protocol are fixed in a spec before the results
   that use them are read; each run records the commit and science version it
   used. Code snapshots, resources and walltime are not frozen at submission.
   (Freezing by content hash cost one study 120 renames and 5,580 rewritten
   records, and rejected harmless edits.)
3. **What is never written never needs cleaning.** Only outputs a downstream
   consumer declared go to /work; caches and intermediate state go to `/local`
   and die with the task.
4. **Smoke before search.** Every method × model runs the real path end to end
   at 10% of the real budget first. The smoke tunes system settings, measures
   cost and gates expensive methods before any search spends on them.
5. **One state store, one writer.** One SQLite database per experiment, written
   only by a controller on the login node; compute nodes read queue files and
   write claim and result files.
6. **A small operating surface.** About ten commands, a status of at most 40
   lines; facts go in the database, the decision log records decisions only.
   (One execution log grew 1,600 lines in three days, and every new agent had
   to read it.)
7. **Rerunnable tasks, uncontaminated conclusions.** A task definition is data,
   so a rerun is comparable; numeric code changes get a science version and
   never mix in one comparison; selection never reads test scores.
8. **Every stage names the decision it changes.** A stage that changes no
   decision is not run. (An unpriced baseline, ~600 node·h with no result, and
   a full-budget search stage that never changed a selection, ~167 node·h,
   were 18% of one study.)

## Architecture

```
 configs/<exp>/experiment.yaml                 git commit
       │ apply                                   │ release <commit>
       ▼                                         ▼
 ┌─ controller (login node; one cycle ≤ 60 s; the only DB writer) ───────────┐
 │ <R>/control/state.sqlite                                                   │
 │ refresh pilots → import gate receipts, claims, results → classify attempts │
 │ → release dependants, record cost → publish a tested release → select and  │
 │ propose → write queue files → submit pilots → delete by consumers →        │
 │ append statistics → print a ≤ 40-line summary                              │
 └───┬───────────────────────────────▲──────────────────────────────┬────────┘
     │ control/queue/n<N>.json       │ control/claims/<task>        │ qsub
     ▼ (read-only)                   │   [.started | .failed]       ▼
 ┌─ pilot (PBS job on compute nodes) ────────────────────────────────────────┐
 │ bad-node gate → read queue → claim with os.link → run the task from the    │
 │ release snapshot → write runs/<task>/ → evaluate in place on idle GPUs →   │
 │ claim the next task that fits, or exit                                     │
 └────────────────────────────────────────────────────────────────────────────┘
     ▼
 statistics tables, appended as results land → paired statistics and figures
 once the comparison is locked
```

Data flows one way: the controller writes queues, pilots write claims and
results, the controller imports them. Compute nodes never write SQLite
(rollback journal, because WAL needs shared memory a network file system lacks).
Contracts for the spec, tasks, state tables, pilot protocol, executor and
releases: [architecture.md](references/architecture.md).

## Building the platform

1. **Diagnose before building.** Measure where time goes on the current setup:
   queue wait vs run vs requested walltime per queue and node count, GPU use of
   multi-node jobs, queue time thrown away by jobs that never started.

   ```bash
   MIYABI_ML_PLATFORM_ROOT=/absolute/path/to/miyabi-ml-experiment-platform
   /usr/bin/python3 -I "$MIYABI_ML_PLATFORM_ROOT/scripts/pbs_time_report.py" --hday 7 --name <job-name-prefix> --text
   ```

   Then state what the change will save and which experiment waits for it.
   One control plane was generalized and then refactored (52 files) before an
   18-minute diagnosis, on data already on disk, showed where the time went.
   With no job history yet, go on to the contracts and measure from the smoke.
2. **Write the contracts before the code**: spec field groups with numeric
   settings apart from speed settings; task definition, states and outcomes;
   the artifact table (what each task kind writes to /work); retention by
   declared consumers; release and science-version rules. Four parallel
   research branches of one project each lost 4–6 h to data-access rules that
   arrived after their code.
3. **Build in order** ([implementation-guide.md](references/implementation-guide.md)):
   state store and spec, PBS access, pilot ([`assets/pilot.pbs`](assets/pilot.pbs))
   and executor, releases, the controller cycle, engines, artifacts and
   statistics, cost model, selection. Build the requirements below in from the
   start.
4. **Accept it on compute nodes, in this order:**
   - control-plane and canary tests as a release test inside a pilot;
   - a two-pilot integration test: two 1-node pilots, three sleep tasks plus
     one too long to fit — each short task runs exactly once, the long one is
     never claimed, an idle pilot exits within 60 s;
   - a bad-node drill (fake node file, stub `qsub`);
   - one controller cycle ≤ 60 s over 2,000 synthetic tasks;
   - dry runs of what an earlier implementation only found live: a pilot stuck
     behind a reservation, admission slots full, a loop whose code changed
     under it, a walltime kill;
   - the 10% smoke of every method × model with its file contract checked.

## Starting an experiment

Decide first (evidence and procedures: [research-guardrails.md](references/research-guardrails.md)):

1. **Name the decision each stage changes**; drop stages that change none.
   Estimate each stage from measured comparable runs, and give the user the
   node·h and wall-clock before any stage or method above ~100 node·h or 2×
   the base method. One baseline's 46 h rewriting estimate existed on day one;
   the user learned the number three days and ~600 node·h later.
2. **Run a trial at the target scale before a full factorial** (a pilot
   study; called a trial here because a pilot is a PBS job): one model, one
   training protocol, ~3 seeds, the strongest baseline and the hypothesis,
   plus a written stop rule for the full design. One study spent ~4,350
   node·h learning that a 0.6B result did not transfer to 3–4B; a 160–320
   node·h trial would have shown it in about two days.
3. **List what a carried gain depends on** (from the earlier stage's ablations)
   and check each item against the new design. One method's gain needed its
   updates scaled by c = 4–6; the next design fixed c = 1 and re-tested a
   setting its own records had shown to lose.
4. **Measure the evaluation noise floor**: evaluate the same weights twice, on
   one node and across nodes (< 1 node·h). Identical weights differed by +0.48
   points with 44 GSM8K answers flipped; gates and single-seed differences of
   that size are noise.
5. **Check that the development metric predicts the test metric** before it
   drives a selection: on 3–5 checkpoints (from earlier work, or the first
   screen runs), correlate it with a test-format metric computed on
   development data (an answer-NLL metric correlated r = 0.14 with the
   benchmarks; selection on it was near random).
6. **Write a stop-loss for the research line, keep an unseen confirmation set,
   and count how often each test set has been looked at.**

Then run it ([experiment-lifecycle.md](references/experiment-lifecycle.md)):

7. **Spec**: pinned model revisions; numeric vs speed settings; stage budgets;
   disjoint development and formal seeds; evaluation protocol; selection rule;
   smoke matrix; the user's limits as caps (`max_walltime_s`, `max_pilots`).
   A cap is checked at submission, never requested as the walltime and never
   left to session memory.
8. `apply` with every stage but the smoke held; `release` the tested commit.
9. **Smoke at one uniform ratio (10%)** of every count (steps, rows, generated
   items, ≥ 32 questions per benchmark), with each cost part timed separately
   so the formal time scales up. Tune speed settings in the smoke; fix numeric
   settings before the first development result is used. Read times, memory,
   lengths and failure rates — never scores.
10. `estimate`: cost gate (defer baselines above 2× the base method) and the
    wall-clock lower bound per stage ([scheduling-and-cost.md](references/scheduling-and-cost.md)); report both.
11. Resume the next stage under one `loop`; follow `status` and `estimate`
    only. Wake on state changes, not on short timers: a watcher that wakes the
    session when `status` changes (in Claude Code, a background command that
    exits on a change), `miyabi-pbs-goal-wakeup` for one long job, and a long
    timer only as a fallback.
12. Formal runs evaluate in place what uses the allocation (short, sharded or
    laned work) and send long serial tails to 1-node pilots; statistics read
    scores only after every method of a comparison has locked; finish with the
    closing audit; clean up from the artifact registry
    ([artifacts-and-storage.md](references/artifacts-and-storage.md)), never by
    guessing.

## Changing things mid-run

| Change | Do | Effect on queued work |
| --- | --- | --- |
| Fix controller logic (scheduling, admission, cleanup) | commit → `release` (the release test runs the control-plane tests) | none; the loop restarts on the new code as soon as the package changes |
| Fix pilot or task code (executor, logging) | commit → `release` | none; pilots that start after the release use it |
| Faster speed setting (eval batch, lanes, threads) | edit `speed` → `apply` | none; the next queue file carries it |
| Fix a score-changing evaluation bug | bump `EVAL` → `release` | finished evaluations become `stale` → `rerun` |
| Fix one method's training bug | bump that method's version → `release` | only its finished tasks become `stale` |
| Add a baseline | registry entry + spec entry → `apply` | its tasks start from the smoke |
| Defer or drop a baseline | `hold --method`, or remove it from the spec → `apply` | unstarted tasks leave the queue; running ones finish |
| Change a search space | spec → `apply`; `withdraw` unrun proposals drawn from the wrong space | affected cells reopen; finished results stay, out-of-space ones stop ranking |
| New dependency or kernel | new venv from the new `uv.lock` (never re-sync the one in use) → numeric check job → point `pilot.pbs` at it → `release` | pilots queued earlier keep the old venv (PBS copied their script at `qsub`): `hold` tasks that need the new package until those pilots are gone, or `qdel` them; results record `environment`; a numeric change needs a science version |
| Lend resources to another experiment | `hold --all` (or by stage); `qdel` queued pilots if needed | nothing to archive; `resume` resubmits |
| Task needs code not yet released | `hold` it before `apply`, `resume` once the release is live | a pilot binds the release current at its start |

`hold` conditions are OR-ed: `--stage trial --method x` holds every trial-stage
task and every x task. Hold by the field that separates what you mean.
Rehearse risky `apply`s (space revisions, seed changes) on a copy of
`state.sqlite` first; that caught a search-space bug before it went live.

## When experiments are slow or wasteful

Run `scripts/pbs_time_report.py`, then look, in this order, for:

- **used/requested walltime** well below ~0.6 → size from measurements;
- **queue wait ≫ run** on large shapes → fewer serial stages, in-place work,
  earlier submission, walltime that backfills;
- **idle nodes in multi-node jobs** → serial single-node work after training
  (in one formal stage, ~35% of the 11-node node·h waited on selection,
  export and evaluation) → send serial tails to 1-node pilots, or spread them
  over the allocation (shards, lanes). PBS's GPU figures seem to cover only
  about a job's last hour, so measure idle time from the task timeline
  (claim → end of each attempt, placed on its nodes);
- **time from `ready` to claim** beyond the queue wait → the experiment's own
  share (`max_pilots`, `--max-queued`) or the submission order holds work
  back; raise the share while the project has room;
- **queued hours of never-started jobs** → binding at `qsub`, or pilots
  submitted for tasks that cannot fit;
- **file counts on /work** (`lfs quota`) → caches or per-round state outside
  `/local`.

## Defaults

| Item | Value |
| --- | --- |
| Walltime | expected seconds × 1.5, rounded up to 10 min, ≤ 16 h; stage cap `max_walltime_s` |
| Expected seconds | longest measured run of the cost class → spec `expected_seconds` → part model fitted from the smoke → initial spec estimates |
| Queue | walltime ≤ 30 min and ≤ 16 nodes → `debug-g`; else `regular-g`; not `short-g` (≤ 8 nodes; its 1-node waits were no shorter than small-g's, and one day its 1-node jobs sat > 7 h) |
| Claim | expected × 1.2 ≤ remaining walltime − 90 s; generation-heavy evaluations at max(measured × 1.5, declared × 1.2) |
| Admission | accepted limit − accepted − 4 reserved; `--max-queued` per queue; `max_pilots` with `debug_pilots` (1) kept for short tasks |
| Retries | `failed_infra` (before the first optimizer step) retried twice automatically; `lost` (walltime) once with more walltime; other failures wait for review |
| Smoke | 10% of every count; evaluations ≥ 32 questions; same rounds and checkpoints; ≤ ~20 min expected per task so it fits `debug-g` (for long tasks, a lower ratio applied to every count) |
| Cost gate | defer baselines whose projected node·h exceeds 2× the base method; report the ratio for hypotheses |
| Search | exhaustive if ≤ 8 distinct trainings, else TPE (8 proposals, 4 pending); screen → top 2 confirm on 1 seed → winner to formal; no full-budget development stage |
| Cleanup | ≤ 500 deletions per cycle, more become a cleanup task; PBS output kept 72 h unless an attempt failed |
| Controller | one loop per experiment root, restarted automatically on code change; cycle ≤ 60 s at 2,000 tasks; status ≤ 40 lines |

## Costly mistakes

| Mistake | What it cost | Instead |
| --- | --- | --- |
| Binding code, task and walltime at `qsub` | 389 of 393 archived attempts never started | late-binding pilots |
| Guessed walltime; the user's cap used as the request | median use 0.17 of request, no backfill; three corrections in one day | 1.5 × smoke-scaled estimate; the cap as a check |
| Writing everything "in case", then building a cleanup system | 216 files per run, 3.37 M cache files, cleanup code changed 11 times | artifact contract, caches on `/local` |
| Freezing inputs by content hash | 120 renames, 5,580 rewritten records, harmless edits rejected | science versions + canaries |
| Timing evaluation on the base model | 4.5× underestimate (trained models generate to the token limit) | time a 10%-trained adapter's evaluation |
| No cost gate, cost not reported | one baseline: ~600 node·h, no result | smoke, ratio gate, numbers to the user |
| Evaluation queued as separate jobs | one more queue wait per trajectory (median 1.1–2.1 h) | in-place evaluation for work that uses the allocation |
| Long serial tails kept in multi-node jobs | ~35% of one formal stage's 11-node node·h idle; chained parents added a second 11-node queue wait | 1-node pilots for long tails and two-parent steps |
| Several control loops; tasks running the working tree | lock contention; a loop kept old rules and broke a 3 h cap and a 6-job share | one controller loop that restarts when its code changes; pilots and tasks from release snapshots only |
| `afterok` chains | broken chains | the controller releases dependants after validating results |
| Facts in an execution log | 1,600 lines in three days | facts in the store, decisions in the log |
| Smoke parts shrunk by unrelated ratios, total time only | formal time could not be derived | one ratio, per-part timing |
| Scaling a proxy result without a target-scale trial | ~4,350 node·h to a negative result | a trial with a stop rule |
| Cleanup without a registry of every session's needs | a whole experiment root deleted; 10.5 node·h retrained | consumers in the store; delete listed paths only |
| Infrastructure rework before diagnosis | control plane generalized, refactored, then rewritten; ~4 days without formal results | measure first, state the saving |

## Requirements to build in

An earlier implementation lacked these; each was found live
([implementation-guide.md](references/implementation-guide.md) §3 has the full list):

1. Claim generation-heavy evaluations at the submission margin, not 1.2×: the
   GSM8K evaluation of a model produced by a merge step was killed at 849 of
   1,319 questions.
2. Record walltime kills as `lost` and rerun them with more walltime: the
   pilot notes in `.failed` that the task ended after PBS's stop signal; a
   bare `.failed` (SIGTERM → non-zero exit) made them `failed_science`.
3. Apply caps after every walltime source, and cap node counts too.
4. Keep serial single-node tails out of multi-node allocations.
5. Settle the evaluation protocol before the first formal evaluation: noise
   probe, LoRA evaluated merged (`merge_and_unload`) from the first
   evaluation, multiple-choice log-likelihoods in fp32
   ([hf-pytorch-practice.md](references/hf-pytorch-practice.md) §2).
6. Canaries for evaluation parsing, data preparation and generation, not only
   for training.

## Keeping this current

When an implementation or an experiment confirms, refutes or refines a rule,
update the rule where it lives (status label, number with its date, the
evidence in one clause) and its summary here. Platform numbers carry dates;
re-measure rather than copy them. Describe evidence by what happened ("one
study", "a 2026-10 window"), not by project, so the skill stays usable for
any project.

## Reference files

| File | Read when |
| --- | --- |
| [platform-facts.md](references/platform-facts.md) | sizing anything; before trusting a number; Miyabi pitfalls |
| [architecture.md](references/architecture.md) | designing the spec, task model, state store, pilot, executor, releases or commands |
| [implementation-guide.md](references/implementation-guide.md) | building the platform: modules and build order, the pilot job script, requirements an earlier implementation missed, tests and acceptance |
| [scheduling-and-cost.md](references/scheduling-and-cost.md) | walltime, routing, admission, claiming, failure classification, the cost model and gate, idle nodes |
| [experiment-lifecycle.md](references/experiment-lifecycle.md) | smoke and tuning, development search, formal runs, in-place evaluation, statistics, what is fixed when |
| [artifacts-and-storage.md](references/artifacts-and-storage.md) | what goes to /work or `/local`, retention and deletion, streaming statistics, compile caches |
| [hf-pytorch-practice.md](references/hf-pytorch-practice.md) | Trainer, PEFT and lm-eval settings; environment variables and venvs; adopting a faster kernel |
| [research-guardrails.md](references/research-guardrails.md) | planning a study; deciding whether to scale up, stop, or report a cost |
| [`assets/pilot.pbs`](assets/pilot.pbs) | the pilot job script template; fill every `<...>` |
| `scripts/pbs_time_report.py` | queue wait, run time, walltime use and GPU use from PBS history (`--help`; offline tests in `tests/`) |
