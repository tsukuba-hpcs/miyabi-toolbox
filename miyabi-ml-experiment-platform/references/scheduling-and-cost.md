# Scheduling, walltime and cost

## Contents

1. Walltime
2. Routing, admission and shares
3. Which pilots to submit, in which order
4. Claiming work inside a pilot
5. Dependencies and attempt classification
6. Cost model
7. Cost gate and wall-clock bound
8. Known gaps

## 1 Walltime

- Request **expected seconds × 1.5, rounded up to 10 minutes**, at most 16 h
  (implemented). A fixed ladder of classes (0.5/1/2/4/8/16 h) asks for up to
  twice too much and cannot respect a cap such as 3 h.
- **Expected seconds**, the first available of: the longest measured run of the
  task's cost class; the spec's `expected_seconds` for that class; the part
  model fitted from the smoke; the spec's initial part estimates.
- A pilot that runs in-place work after its training asks for training +
  waves × the longest in-place chain, with waves = ⌈in-place tasks ÷
  (nodes × lanes per GPU)⌉.
- `pilot_walltime_s` per stage: a fixed walltime, used when it holds the work
  (verified; e.g. 1800 s keeps 10% smokes in `debug-g`). Scope it narrowly:
  H-0004d set `formal: 7800` to rerun one merged-model GSM8K, and afterwards
  every formal pilot asked for 130 min; four 1-node formal pilots used 49–66
  min of it (state store `pilots`, 2026-10-20).
- `max_walltime_s` per stage: a **cap**. Walltime = min(estimate-based, cap);
  in-place work that no longer fits is left for later pilots (implemented).
  In exp-v2 a stage's fixed `pilot_walltime_s` is returned before the cap is
  applied, and nothing caps the node count: enforce both at submission when
  you port it (`controller.pilot_walltime`).
  A user's time limit is a cap, never the requested walltime and never a
  reason to time components at full size: fedcl was corrected three times in
  one day for submitting at a 3 h cap and for full-size timing probes.
- **Never submit what cannot fit.** When expected × 1.2 exceeds anything a
  pilot could hold, record one `does_not_fit` event instead of submitting
  pilots that start and leave empty forever (implemented).
- Do not trim the 1.5× margin: generation-heavy evaluations vary several-fold
  between models of one cost class (§6).

## 2 Routing, admission and shares

- **Route**: walltime ≤ 30 min and ≤ 16 nodes → `debug-g`; everything else →
  `regular-g` (the site routes it to small/medium/large by node count). Never
  `short-g`.
- **Admission** each cycle from `qstat --limit`: room = accepted limit −
  accepted − 4 (kept for bad-node resubmissions and release tests). A `qsub`
  reply that cannot be parsed is recorded and not retried.
- `--max-queued N` per queue (debug-g and regular-g counted apart): how many of
  this experiment's pilots may wait at once.
- `max_pilots`: this experiment's queued + running pilots when it shares the
  project; `debug_pilots` (default 1) of them are kept for debug-g so long
  tasks never starve short ones. Verified: with `regular-g` 99% busy, four
  1-node pilots waiting behind a reservation (PBS-predicted start 7 h later)
  had been counted as covering two 30-minute tasks; after the change those ran
  in debug-g pilots that started 3–14 min after submission.
- Release-test pilots ignore `--max-queued`; otherwise queued smokes block the
  test, then start on the old code.

## 3 Which pilots to submit, in which order

- One pilot per runnable queue entry not covered by an open, unclaimed pilot of
  the same node count **and queue**. A short entry (its own pilot would fit
  debug-g) counts only debug-g pilots as cover, so it never waits behind
  regular-g pilots queued for long tasks; a long pilot that starts first still
  claims it.
- Order: release tests; other short entries; then by stage (test, data, smoke,
  screen, confirm, pilot, formal), multi-node first within a stage. Ordering by
  shape alone let queued 11-node formal pilots take every regular-g slot while
  ready 1-node pilot-stage tasks got none (verified fix, H-0004d).
- Submit while a release is under test; the pilot binds whatever is released
  when it starts. Hold tasks that need the new code until it is live (verified).
- Design, not built: also count "soon runnable" tasks (dependencies running and
  expected to finish within the shape's median queue wait), so the next stage
  queues while the current one runs; halve that window if more than 20% of
  pilots start empty.

## 4 Claiming work inside a pilot

- Claim the first queue entry whose expected × 1.2 ≤ remaining − 90 s, by
  `os.link`.
- After a training, claim all of its in-place evaluations that fit at once,
  not lane by lane: an unclaimed waiting evaluation looks runnable to the
  controller, which then submits another pilot for it (three extra debug-g
  pilots in one smoke).
- A near-deadline pilot still claims a parent training that fits; long
  in-place children that no longer fit become 1-node tasks. This saves a whole
  11-node queue wait at the cost of one 1-node wait (verified, H-0004d P2: two
  pilots with ~45 min left trained two parents).

## 5 Dependencies and attempt classification

- No PBS `afterok`. A child becomes `ready` only after the controller has
  validated its parent's result. A post-aggregation task waits for its source's
  endpoint file, not for its success.
- Smoke gates as dependencies: a later stage lists the smoke *variants* it
  needs (`after: [SC]`), not stage names, so adding a smoke variant leaves
  finished tasks' dependencies (and their `stale` status) untouched.
- Liveness only from the job table (`qstat -f`, `qstat -H -f`), never from the
  exit status of `qstat <id>`. Report pilots missing from the table for an hour.

| Observed | Outcome | Handling |
| --- | --- | --- |
| pilot ended, nothing claimed | not an attempt | update the pilot row only |
| claim, no `.started`; pilot ended or `.failed` written | `failed_infra` | back to `ready` automatically, at most twice |
| `.started`, no `.failed`, walltime used up | `lost` | `failed`; human review, then `rerun` |
| `.started` and `.failed`, or the pilot ended early | `failed_science` | `failed`; logs kept; no automatic retry |
| valid result | `done` | record cost, release dependants; delete claim files only after the queue files stop listing the task |

The last rule matters: deleting claims before rewriting the queue files let a
stale queue file offer a finished task to another pilot.

## 6 Cost model

- **Cost classes**: training `<kind>/<model>/<protocol>/<method>/<total
  steps>`; evaluation `evaluate/<model>/<benchmark>/<fraction>`;
  post-aggregation `postaggregate/<model>`. Evaluations of models produced by
  server-side post-processing get their own class (`…/<method>`): merged models
  took up to 3.5× their parents' GSM8K time, and sizing them from the parents'
  class timed them out.
- Keep the **longest** measured run per class as the walltime basis. Classes do
  not separate environments, so after a faster kernel the old maximum stays
  (conservative); split by environment only when that changes the walltime
  materially.
- **Fit part parameters from the smoke**, from per-part timings in the results:
  - training: startup + rounds × per-round fixed cost + one validation + total
    steps × step time (round r's validation overlaps round r + 1, so only the
    last one is on the critical path); a per-method step factor (e.g. 1.4×)
    until that method's own smoke replaces it;
  - evaluation: load + items × per-item time, **per benchmark**. Generation
    costs orders of magnitude more per item than scoring a choice (smoke: FPB
    0.018 s, MMLU 0.05–0.09 s, GSM8K 0.87 s, HumanEval 6.2 s, IFEval 8.8 s per
    question). One shared value made every estimate depend on whichever smoke
    evaluation finished last: IFEval's 8.8 s would have sized FPB's 2,264
    questions at 19,900 s instead of ~40 s.
- `expected_seconds` in the spec overrides the model for one class until that
  class has a measurement. Use it when the smoke ran a different class (a smoke
  variant) and you scaled it by hand. Estimates of already stored tasks do not
  change on `apply`; change them here.
- Generation time also depends on the model: ten rank-160 merges of one class
  took 1,576–4,216 s on GSM8K. Keep the job-level 1.5× margin.
- Cannot be fitted: per-round parts from a single-round protocol (needs ≥ 2
  rounds). Biased: a short smoke of a method with variable-length inputs
  (replay) overestimates the step cost because its first hundreds of steps are
  cold compiles (smoke 2.25× FedProx vs 1.37× at full length). Before scaling a
  smoke linearly, look for a full-length measurement of the same recipe.
- `estimate` prints the measured classes, the fitted parts, each method's
  projected full-budget node·h and ratio to the base method (the gate), and
  each stage's node·h and wall-clock lower bound.

## 7 Cost gate and wall-clock bound

- **Gate.** After tuning, project each method's full-budget node·h (including
  generation, rewriting and every other extra) and divide by the base method's
  (e.g. FedAvg) for the same model and protocol. Defer baselines and auxiliary
  references above 2× unless the spec states why they stay; for the hypotheses
  under test, only report the ratio. Without measurements, report, don't
  exclude. FedSDR measured 3.1–9.4× and spent ~600 node·h before it was deferred
  by hand.
- **Wall-clock lower bound per stage:**

  ```
  T ≥ Σ(nodes × expected seconds) ÷ min(256, 16 × nodes per task) + median queue wait of that shape
  ```

  It assumes the project's full width; a busy site, or a share of the project,
  makes it longer. Report it with the node·h whenever you propose a stage.
  G1-1 formal: 360 tasks × 0.59 h × 11 nodes ≈ 2,340 node·h ÷ 176 nodes ≈ 13.3 h,
  plus 3.6 h of queue ≈ 17 h.

## 8 Known gaps

From the H-0004d runs (framework document §17) and the final retrospective §6.2:

1. **Claim margin.** Claims use 1.2× while submission uses 1.5×, and a pilot
   claims the first entry that fits, not the task it was sized for. A merged
   model's GSM8K (actual 4,216 s; class maximum 2,697 s × 1.2 = 3,236 s fit a
   3,600 s pilot) was killed at 849 of 1,319 questions and rerun in a 7,800 s
   pilot. Claim generation evaluations at 1.5× (or max(measured × 1.5,
   declared × 1.2)), or let a pilot claim the task that triggered it first.
2. **Walltime kills are misrecorded.** At walltime PBS sends SIGTERM; the pilot
   kills its task, which exits non-zero and is recorded `failed_science`, not
   `lost`. Mark tasks that ended after the stop signal in `.failed`, record them
   `lost`, and rerun them automatically with more walltime.
3. **Idle nodes after training.** In the h0004d root's formal 11-node jobs,
   64.6 of 185.4 billed node·h (~35%) waited on single-node steps after
   training (selection, export, evaluations), measured from task timelines
   (PBS's GPU figures cover only about a job's last hour). Let large
   allocations train and run tails that use their width (shards, lanes); send
   serial single-node tails to 1-node pilots (a 1-node wait averaged 26 min).
   Expected saving: ~65 node·h, 26% of that root. Candidate, unverified: PBS
   Pro's `pbs_release_nodes` (installed on the login nodes) releases a running
   job's sister nodes; test on a 2-node debug-g job whether Miyabi permits it
   and whether the released nodes stop being billed.
4. **Defects found only live.** The stale-rules loop, the "already covered"
   pilot count and the submission order were all found in production. Add
   dry-run scenarios (a pilot stuck behind a reservation; slots full) to the
   release tests, and check the code hash when a loop starts.
5. **A pilot that starts before any release exits**, wasting its queue wait.
   For multi-node pilots, wait a few minutes when a release is under test.
