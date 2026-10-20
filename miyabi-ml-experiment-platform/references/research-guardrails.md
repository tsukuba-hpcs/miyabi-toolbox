# Research guardrails

A fast platform can still spend thousands of node·h on a conclusion that a
small check would have given early. fedcl's research result was negative: two
methods judged GO on a 0.6B proxy did not reproduce at 3–4B, after at least
~4,850 node·h and ~4.4 billion input tokens. These checks are the ones whose
absence cost the most (final retrospective §4–§7).

## Contents

1. Before scaling up
2. Before freezing a design
3. While it runs
4. Reporting cost and handling requests
5. Agent operations
6. What skipping each check cost

## 1 Before scaling up

- **Two-level GO.** A GO on a proxy setting (small model, short budget, tuned
  knobs) is a *candidate* GO. A project GO needs a pilot on the target model
  and protocol: one backbone, ~3 seeds, the strongest baseline and the
  hypothesis only, with a written mid-way stop rule for the full design.
  fedcl went straight from a 0.6B GO to 9 methods × 3 backbones × 2 protocols ×
  5 seeds (G1-1, ~4,350 node·h, 5 days to a verdict). Neither hypothesis
  showed a gain on the first backbone. A pilot of ~160 node·h (one protocol)
  to ~320 node·h (two) would have decided in about two days.
- **Gain-dependency list.** When a hypothesis passes, list the settings whose
  change removes its gain, from its ablations and candidate records. The next
  design states for each item whether it is kept or changed, and why. H-0005's
  stage-1 gain needed client updates scaled c = 4–6 at merge; at c = 1 the
  same records had it 2.2–2.7 points *below* FedProx. G1-1's plan fixed c = 1
  ("other grids and ablations are not carried over") and nobody flagged it
  until the formal results were in.
- **Hypotheses early, not after the baselines.** At hour 70 of G1-1 the user
  asked where the H-0004/H-0005 results were: their searches had not started
  and the baseline selection still waited on 16 jobs. G2-1 ran 6 baselines'
  searches and never ported the hypotheses. Schedule the first
  hypothesis-vs-strongest-baseline comparison as early as possible; baselines
  beyond the strongest one can follow.

## 2 Before freezing a design

- **Evaluation noise floor.** Evaluate the same weights twice, on one node and
  across nodes (< 1 node·h). Store it as a project-level number; set gates no
  lower than it and read single-seed differences against it. fedcl measured it
  only at the end: identical weights differed by +0.48 points [+0.17, +0.81]
  with 44 GSM8K answers flipped, while its P1 gate was +1.0 and a λ check read
  +0.91 and +0.54. Earlier hints were there: two runs of one G1-1 model agreed
  on only 68 of 96 greedy answers, and re-evaluation moved S by about ±0.2.
- **The development metric must predict the test metric.** On 3–5 existing
  checkpoints, correlate the development metric with a test-format metric
  before freezing it; if the correlation is weak, do not freeze it. G1-1
  selected on answer NLL, which correlated r = 0.14 with the benchmarks:
  selection was near random. H-0005b passed P1 at +1.23 and lost P2 at −1.03
  because P1 judged three domains by NLL only (~68 node·h).
- **Symmetric hyperparameters.** A method's hyperparameters treat every domain
  or benchmark alike; any asymmetry needs a reason written in advance. H-0004c
  gave finance its own weight and a finance-like development set (+2.78); the
  user asked whether this was tuning for finance, and the symmetric version
  gave +1.82.
- **Account for test data.** Count how often each test set has been looked at;
  results on looked-at data are exploratory. Set aside an unseen confirmation
  set at the start. fedcl's follow-ups were designed after reading G1-1's test
  results and evaluated on the same seeds and benchmarks, so they could only be
  exploratory.
- **Stop-loss per research line**, written when the line opens, e.g. "stop
  after two consecutive hypotheses with no significant difference from the
  best baseline". fedcl ran five follow-ups on one line without one.
- **Each stage names the decision it changes.** FedSDR (~600 node·h, no
  result) and an HPO "full" stage that changed no selection (~167 node·h) were
  18% of G1-1.

## 3 While it runs

- **Look at where the time goes yourself, regularly** (`scripts/pbs_time_report.py`,
  `status`, `estimate`). In G1-1 the user found at least seven process problems
  first: a self-imposed 15-job cap while the site allowed 128 accepted and 16
  running; evaluations waiting ~3 h for all 390 formal tasks to be registered;
  guessed walltime, twice; FedSDR's cost; the HPO full stage; a control loop
  idle ~58 min on a missing key. In the follow-ups the user corrected the
  walltime and smoke practice three times in one day.
- **A user's limit is a submission check.** Write it into the spec
  (`max_walltime_s`, `max_pilots`) the moment it is stated; it is a cap, never
  the request. fedcl's "each job within 11 nodes × 3 h" lived only in session
  memory, was lost after a context compaction, and jobs went in at a
  hard-coded 6–12 h two days later.
- **Diagnose before reworking infrastructure**: measure queue, run and
  resubmission shares, state the expected saving and which experiment waits
  for it. During G2-1 the control plane was generalized, refactored (52 files)
  and rewritten: 3 days 21 h with no formal result; the diagnosis that settled
  the design took 18 minutes on data already on disk.
- **Mid-way additions** ("also remove SHA-256 while you are at it", "full
  refactor"): state their cost and their effect on hypothesis testing before
  starting.
- **Cleanup** only from the registry of every session's retention needs
  ([artifacts-and-storage.md](artifacts-and-storage.md) §3).

## 4 Reporting cost and handling requests

- **Numbers, not adjectives.** Before any stage or method above ~100 node·h or
  2× the base method, give the user node·h and wall-clock. FedSDR's estimate
  (46 h to rewrite 8,000 items; no single 48 h allocation could hold it)
  existed on 10-13; the user was told only that "rewriting may make a single
  job time out", asked on 10-16 after seeing the queued jobs' estimates, and
  deferred it 12 minutes later — after ~600 node·h.
- **An expensive, ambiguous request gets 2–3 priced options, the cheapest as
  the default.** A λ check asked for "the full pipeline" without saying whether
  one arm (MRc) was included; the agent logged the ambiguity but took the
  widest reading (67 node·h), and finished that arm's evaluations (31.8 node·h)
  after its outcome was predictable. (Those repeats did reveal the evaluation
  noise.)
- Apply a preference the user states (walltime, smoke rules) the same day,
  everywhere it applies.

## 5 Agent operations

- **Tokens ≈ calls × context.** Each call re-reads the context (~200k tokens
  per call in fedcl's long sessions; 91% of G2-1's calls were Bash). Save by
  making fewer calls (one script for a multi-step check; fewer wakeups) and
  keeping contexts short (one session per unit of work). The amount of code
  written matters little.
- **Wait on changes, not timers**: one resident monitor that wakes the session
  when state changes (`miyabi-pbs-goal-wakeup`). About 11% of the follow-up
  experiments' tokens went to wakeups where nothing had changed.
- **Hand over a state card**, at most ~2 KB, plus the list of constraints in
  force; not the execution log. G1-1's takeover read a 22.9 KB handover and an
  84 KB execution log and compacted 46 minutes later.
- **Decision log**: one entry per debatable choice, ≤ 5 lines: question,
  options, choice, reason, where the evidence is, whether it can be reversed.
  Facts are queries of the store, not log lines.
- **Subagents for bounded tasks.** Six resident coding subagents used 2.1× the
  main session's tokens in G1-1's coding phase; about 60% of their later
  deliveries were read-backs and re-verification (inferred).

## 6 What skipping each check cost

| Check | Its cost | Cost of skipping it in fedcl |
| --- | --- | --- |
| target-scale pilot with a stop rule | 160–320 node·h, ~2 days | ~4,350 node·h to a negative result |
| gain-dependency list | reading the earlier stage's records | a hypothesis tested at a setting its own data had shown to lose |
| noise floor | < 1 node·h | gates and single-seed differences inside the noise |
| development-metric check | a few evaluations of existing checkpoints | near-random selection; ~68 node·h on a false P1 pass |
| stage decision + priced estimate | minutes | ~770 node·h (18% of G1-1) |
| stop-loss per line | one sentence | five follow-ups on one line; up to one more P2 round (~140 node·h) |
| retention registry before cleanup | minutes | 10.5 node·h retrained; numbers no longer recomputable |
| diagnosis before rework | 18 minutes | ~4 days without a formal result |
| user limits as checks | one spec field | three corrections in a day; jobs past the cap |
