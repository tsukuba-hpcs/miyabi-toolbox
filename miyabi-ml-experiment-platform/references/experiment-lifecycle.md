# Experiment lifecycle

Smoke → development search → formal runs with in-place evaluation →
statistics → closing. Research-level checks that come before all of this
(target-scale trial, noise floor, metric validity, stop rules) are in
[research-guardrails.md](research-guardrails.md).

## Contents

1. Smoke, system tuning and the cost gate
2. Development search
3. Formal runs and in-place evaluation
4. Statistics and closing
5. What is fixed when

## 1 Smoke, system tuning and the cost gate

Run every method × model end to end on the real PBS path at **10% of the real
budget**, structure unchanged: the same rounds and checkpoints, 10% of the
steps, warmup scaled alike, 10% of each benchmark's questions (at least 32),
10% of the items a method generates itself (e.g. rewritten training data; all
of them when there are ≤ 64), and the statistics step at the end.

Why 10%: enough steady-state steps that compile and warm-up do not distort the
step time; every cross-round path and checkpoint runs; a 10%-trained adapter
already answers in the trained format, so generation lengths are realistic.
Timing evaluation on the base model underestimated it up to 4.5×: trained
models often generate until the token limit.

**One ratio, every part timed.** Shrink every count by the same ratio (steps,
development and validation rows, generated items, grid steps), record each cost
part separately in the result (load, compile, training, scoring or validation,
generation, method-specific steps), then scale the variable parts back up;
load and compile do not scale. A rough estimate is enough. Do not run
components at full size just to time them. One smoke shrank its parts by
unrelated ratios (3%, 3–17%, 1–2%) and recorded only totals: the formal time
could not be derived and an extra timing task was needed.

Keep each smoke task inside `debug-g` (30 min): it starts in minutes instead of
hours. Aim for ≤ ~20 min of expected work, which the 1.5× walltime rule keeps
within 30 min (a fixed `pilot_walltime_s` of 1800 s admits up to ~23 min under
the 1.2× claim margin). A method whose smoke does not fit is itself a cost
warning. For tasks that are long by design (a 6 h single-GPU training gives a
36 min 10% smoke), lower the ratio for every count alike, as long as
steady-state steps still dominate the timing and the adapter already answers
in the trained format; or run the 10% smoke in `regular-g` when 1-node waits
are short. Either way, compare generation lengths with the first full-size
run before trusting the evaluation estimates.

**Tune the system settings in the smoke:**

| Setting | Changes numbers? | Fixed when | How to measure |
| --- | --- | --- | --- |
| training micro-batch (effective batch fixed through gradient accumulation) | yes | before the first development result is used | 1-node probe at full sequence length: peak memory (stay under ~80–90% of device memory) and step time |
| compile settings (`pad_multiple`, …) | treat as yes | same | compile time + step time |
| batch of a method's own generation step | yes: greedy output depends on the batch | before the first development result is used | seconds per item |
| evaluation batch | slightly; not a protocol item | never: switch whenever a faster one is found, and record the batch in every result | run the smoke's evaluations at different batches (`eval_batch_sizes`); bigger is not always faster (Qwen: 16 beat 32–64; 64 ran out of memory at 4 lanes) |
| evaluation lanes per GPU | no | any time | throughput and memory (Qwen: 2 lanes, 83 q/s at 39 GB) |
| compile-cache reuse | no | any time | worth it once compile time exceeds ~20% of a short task |

**Smoke results are system evidence only.** Read times, memory, generation
lengths, empty-output and parse-failure rates. Never read scores; never feed a
smoke into a selection. Prefer a dedicated smoke seed; if only development
partitions exist, reuse one but keep its results out of every selection.

**Smoke acceptance.** Each smoke training and its evaluations reach the
statistics tables and their run directory is gone (`debug: false`); /work holds
no compile-cache files outside the `caches/` archives; `/local` cache sizes are
in the logs; every written file is in the artifact contract.

**Cost gate**: [scheduling-and-cost.md](scheduling-and-cost.md) §7. Gate after
tuning, so a slow method gets the chance to be optimized first.

## 2 Development search

- **Count distinct trainings, not points.** Search exhaustively, all at once,
  when the space needs ≤ 8 trainings; otherwise TPE with at most 8 proposals
  and 4 pending. Candidates that differ only in a cheap post-hoc parameter and
  can be re-derived from one training count once (6 learning rates × 9
  post-hoc settings were searched exhaustively as 6 trainings).
- **Stages**: screen (small data, short budget) → top 2 confirm on one
  development seed → the winner goes straight to formal. Each stage retrains
  from the same initialization. **No full-budget development stage**: one took
  39% of a study's development GPU·h and never changed a selection. If you
  doubt that the confirm budget ranks candidates as the full budget would,
  check it on one cell (top 2 at full budget, one seed) rather than adding a
  stage for every cell (unverified).
- **Tiers pay only when the low tier is cheap.** Compute each tier's fixed share
  (startup, per-round cost, validation). If a low-tier run costs more than half
  a high-tier run (screen 16.9 min vs confirm 20.5 min, with training steps
  only 7% of a screen), screen at confirm fidelity or cut fixed costs first.
- **Extra development seeds** help only when seed noise is comparable to the
  gaps between candidates. Check existing records first (one confirm seed and
  two-seed means once picked the same candidate in 36 of 36 cells; median seed
  difference 0.0017).
- **No pruning on intermediate validation of a scheduled run** (warmup +
  decay to the end): intermediate checkpoints are biased proxies (short-horizon
  bias, Wu et al., ICLR 2018). Successive-halving and Hyperband pruners fit only
  when an intermediate value is itself a valid low-fidelity result; with budget
  tiers, retrain each tier and promote in the controller.
- **Selection rule** in spec and code: mean development validation score over
  the stage's seeds, then extra GPU seconds, then extra steps, then parameters
  (smaller first). A candidate with a failed seed is not promoted. Decisions
  that need full-budget development information (e.g. the strongest baseline)
  use the formal seeds' validation scores, frozen before any benchmark score is
  read.
- **Train identical work once** (verified). Tasks whose expensive training is
  determined by the same data, seed, initialization and objective share one
  training (e.g. single-round methods that differ only in how the coordinator
  combines the workers' updates). Same post-processing → reuse its
  result; a different post-processing → re-derive it from the saved
  intermediates on one GPU (58 s instead of a ~5 min 11-node run). Multi-round
  protocols diverge after round 1 and train separately. Reuse only identical
  recipes: two independent runs of one recipe differed by up to 0.06 in the
  validation score.
- **TPE details.** Treat parameter tuples that are not a full product (e.g. an
  adaptive optimizer's (learning rate, ε) pairs) as one categorical dimension,
  or TPE proposes points outside the space. Rebuild the study every cycle from
  the store (done, failed and pending proposals) with sampler seeds derived
  from (experiment seed, cell, n): the controller can restart at will and
  proposals reproduce. Warm-start points are proposed first and count toward
  the cap.
- **Revising a space mid-search.** Read development validation scores only.
  Apply one rule to every cell, e.g.: each optimizer's grid contains the point
  equivalent to the plain baseline, with values on both sides; an edge optimum
  whose drop to the next point exceeds seed noise extends the grid one step; a
  value indistinguishable from another is dropped. Compute equivalences from
  the update formula as implemented: for an adaptive optimizer without bias
  correction whose second-moment state starts at v₀ = τ² (τ its ε-like
  constant), 9 of 12 original (learning rate, τ) pairs either barely moved or
  diverged. Record it as a design change. Afterwards rank only inside the
  current space; out-of-space results stay stored but unranked.
- **Migrating a space** from an older system: add a test comparing each cell's
  new space with the frozen old grid (a migration once silently lost one
  baseline setting). Importing earlier screen results saves far more than
  carrying only their promotions (109 screens, ~114 node·h): check data
  manifest hashes, budgets, candidate IDs and space for each record, mark them
  `imported`, and confirm the new ranking reproduces the old promotions.

## 3 Formal runs and in-place evaluation

- A method's formal tasks become runnable as soon as its own selection locks;
  they do not wait for other methods, and evaluation does not wait for the
  whole formal plan (a study once idled ~3 h waiting for all 390 formal tasks).
- **Evaluate in place what uses the allocation.** Short evaluations, and
  evaluations sharded over the idle nodes or run as lanes, run in the
  training's allocation right after training: no extra queue wait, and the
  adapter can go once evaluated. A long serial tail on a multi-node
  allocation (IFEval ~50 min on one GPU, a 30-min selection) idles the other
  nodes: ~35% of the billed node·h of one formal stage's 11-node jobs. Send
  such tails to 1-node pilots unless 1-node waits are long: the trade is one
  1-node queue wait against idle nodes × tail length. 1-node waits were short
  on 2026-10-18 → 20 (debug-g median < 1 min, regular-g 0.48 h); separately
  queued evaluation jobs had waited a median 1.1–2.1 h in an earlier, busier
  window.
  Measure before choosing (`scripts/pbs_time_report.py`).
- A failed evaluation fails only itself; the training result stays; re-evaluate
  later from the kept adapter.
- In-place children attach to their source's queue entry only if all their
  other dependencies are already done when the queue is written; otherwise
  they queue on their own after the source. Do not chain parents just to keep
  such a child in place: one stage made each of two parents depend on the
  other so that the selection (which needs both) could run inside the second
  job. 4 of 5 selections ran in place, but the stage paid two serial 11-node
  queue waits (3.1–3.4 h, then 1.4–1.8 h) and idle nodes during each 25–35 min
  selection. Run a child with two parents as a 1-node task after both.
- A model produced by post-processing (merge, export) is evaluated in the same
  allocation too: the parent's evaluations and the export start together, the
  export's evaluations follow; tabulate the export only after all its
  evaluations finish (they read its `result.json`); delete the exported
  adapter once evaluated.
- A task that reuses another trajectory reuses its evaluations too (copied
  when the source's evaluation completes).
- Serial single-node tails idle the rest of a multi-node allocation:
  [scheduling-and-cost.md](scheduling-and-cost.md) §8.

## 4 Statistics and closing

- Tabulate continuously ([artifacts-and-storage.md](artifacts-and-storage.md)
  §4); read **scores** only after every method of a comparison unit has locked
  its formal configuration.
- Paired statistics on per-question correctness; CSV first, figures from the CSV.
- **Evaluation is itself noisy.** The same weights evaluated in two jobs gave
  different per-question results (identical commit, prompts and batch 16; only
  node and job differed): GSM8K answer texts differed on 33–34% of items, 44
  and 53 items flipped correctness; in multiple-choice benchmarks 14–48% of
  items had a different log-likelihood (up to 0.36 nat); the aggregate score
  moved +0.48 and +0.09 points, and a question-level bootstrap interval for
  the first, [+0.17, +0.81], excluded 0. A bootstrap over questions does not
  include this. For single-seed comparisons evaluate the same weights several
  times, or state that the interval is too narrow; multi-seed bootstraps
  resample seeds and absorb it. Cause unknown (ruled out: weights, items,
  batch, commit; open: kernel choice per node or job, two lanes on one GPU,
  bf16 log-softmax).
- Derived fields come from direct evidence in the record, not a proxy (an
  "accepted" flag computed from step counts counted rollbacks as accepted).
- **Closing audit**: per comparison unit, the commits used and their diff on the
  scientific paths, and the evaluation batch sizes used.
- **Closing**: delete `experiment_closed` artifacts and per-question samples;
  keep the statistics CSVs, figures, `state.sqlite`, each task's `result.json`
  and the evaluation summaries.

## 5 What is fixed when

| Item | Fixed |
| --- | --- |
| data sources, deduplication, splits, validation sets, seed derivation | before the first development result is used for selection |
| training budget; evaluation protocol (tasks, shots, `max_new_tokens`, context length) | same; the evaluation protocol at the latest before the first formal evaluation |
| numeric training and generation settings (micro-batch, compile settings, generation batch) | chosen in the smoke; fixed before the first development result is used |
| candidate set and selection rule | before that cell selects; candidates may be added but selection waits for all of them; never changed because of test scores |
| formal configuration | when its selection locks |
| code, evaluation batch, lanes, packing, threads, compile caches, walltime, queues, file layout | never frozen; every run records what it used |
