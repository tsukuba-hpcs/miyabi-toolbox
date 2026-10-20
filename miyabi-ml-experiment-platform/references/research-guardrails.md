# Research guardrails

A fast platform can still spend thousands of node·h on a conclusion that a
small check would have given early. These checks are the ones whose absence
cost the most in one project (~4,850 node·h over several experiments), whose
main study spent ~4,350 node·h to find that two methods judged GO on a 0.6B
proxy did not reproduce at 3–4B.

## Contents

1. Before scaling up
2. Before freezing a design
3. While it runs
4. Reporting cost and handling requests
5. Agent operations
6. What skipping each check cost

## 1 Before scaling up

- **Two-level GO.** A GO on a proxy setting (small model, short budget, tuned
  knobs) is a *candidate* GO. A project GO needs a trial (a pilot study) on
  the target model and protocol: one backbone, ~3 seeds, the strongest
  baseline and the hypothesis only, with a written mid-way stop rule for the
  full design. Going straight from a 0.6B GO to 9 methods × 3 backbones × 2
  training protocols × 5 seeds took ~4,350 node·h and 5 days to a verdict,
  and neither hypothesis showed a gain on the first backbone. A trial of
  ~160 node·h (one protocol) to ~320 node·h (two) would have decided in
  about two days.
- **Gain-dependency list.** When a hypothesis passes, list the settings whose
  change removes its gain, from its ablations and candidate records. The next
  design states for each item whether it is kept or changed, and why. One
  method's gain needed its updates scaled by c = 4–6 at merge; at c = 1 the
  same records had it 2.2–2.7 points *below* the strongest baseline. The
  next plan fixed c = 1 ("other grids and ablations are not carried over"),
  and nobody flagged it until the formal results were in.
- **Hypotheses early, not after the baselines.** Seventy hours into one study
  the user asked where the hypothesis results were: their searches had not
  started and the baseline selection still waited on 16 jobs. Another
  experiment ran six baselines' searches and never ported the hypotheses.
  Schedule the first hypothesis-vs-strongest-baseline comparison as early as
  possible; baselines beyond the strongest one can follow.

## 2 Before freezing a design

- **Evaluation noise floor.** Evaluate the same weights twice, on one node and
  across nodes (< 1 node·h). Store it as a project-level number; set gates no
  lower than it and read single-seed differences against it. Measured only at
  the end of one project: identical weights differed by +0.48 points [+0.17,
  +0.81] with 44 GSM8K answers flipped, while its gate was +1.0 and a later
  check read +0.91 and +0.54. Earlier hints were there: two runs of one model
  agreed on only 68 of 96 greedy answers, and re-evaluation moved the score by
  about ±0.2.
- **The development metric must predict the test metric.** On 3–5 existing
  checkpoints, correlate the development metric with a test-format metric
  before freezing it; if the correlation is weak, do not freeze it. Selection
  on answer NLL, which correlated r = 0.14 with the benchmarks, was near
  random. A screen that judged three domains by NLL only passed at +1.23, and
  the confirmation then lost at −1.03 (~68 node·h).
- **Symmetric hyperparameters.** A method's hyperparameters treat every domain
  or benchmark alike; any asymmetry needs a reason written in advance. One
  method gave one domain its own weight and a development set resembling it
  (+2.78); the user asked whether this was tuning for that domain, and the
  symmetric version gave +1.82.
- **Account for test data.** Count how often each test set has been looked at;
  results on looked-at data are exploratory. Set aside an unseen confirmation
  set at the start. Follow-ups designed after reading a study's test results,
  and evaluated on the same seeds and benchmarks, could only be exploratory.
- **Stop-loss per research line**, written when the line opens, e.g. "stop
  after two consecutive hypotheses with no significant difference from the
  best baseline". One line ran five follow-ups without one.
- **Each stage names the decision it changes.** An unpriced baseline (~600
  node·h, no result) and a full-budget search stage that changed no selection
  (~167 node·h) were 18% of one study.

## 3 While it runs

- **Look at where the time goes yourself, regularly** (`scripts/pbs_time_report.py`,
  `status`, `estimate`). In one study the user found at least seven process
  problems before the agent did: a self-imposed 15-job cap while the site
  allowed 128 accepted and 16 running; evaluations waiting ~3 h for all 390
  formal tasks to be registered; guessed walltime, twice; an unpriced
  baseline; a full-budget search stage; a control loop idle ~58 min on a
  missing key. Later the user corrected the walltime and smoke practice three
  times in one day.
- **A user's limit is a submission check.** Write it into the spec
  (`max_walltime_s`, `max_pilots`) the moment it is stated; it is a cap, never
  the request. A limit of "each job within 11 nodes × 3 h" lived only in
  session memory, was lost after a context compaction, and jobs went in at a
  hard-coded 6–12 h two days later.
- **Diagnose before reworking infrastructure**: measure queue, run and
  resubmission shares, state the expected saving and which experiment waits
  for it. One control plane was generalized, refactored (52 files) and
  rewritten: 3 days 21 h with no formal result; the diagnosis that settled the
  design took 18 minutes on data already on disk.
- **Mid-way additions** ("also remove content hashing while you are at it",
  "full refactor"): state their cost and their effect on hypothesis testing
  before starting.
- **Cleanup** only from the registry of every session's retention needs
  ([artifacts-and-storage.md](artifacts-and-storage.md) §3).

## 4 Reporting cost and handling requests

- **Numbers, not adjectives.** Before any stage or method above ~100 node·h or
  2× the base method, give the user node·h and wall-clock. One baseline's
  estimate (46 h to rewrite 8,000 items; no single 48 h allocation could hold
  it) existed on day one; the user was told only that "rewriting may make a
  single job time out", asked three days later after seeing the queued jobs'
  estimates, and deferred it 12 minutes later — after ~600 node·h.
- **An expensive, ambiguous request gets 2–3 priced options, the cheapest as
  the default.** A check asked for "the full pipeline" without saying whether
  one arm was included; the agent logged the ambiguity but took the widest
  reading (67 node·h), and finished that arm's evaluations (31.8 node·h) after
  its outcome was predictable. (Those repeats did reveal the evaluation noise.)
- Apply a preference the user states (walltime, smoke rules) the same day,
  everywhere it applies.

## 5 Agent operations

- **Tokens ≈ calls × context.** Each call re-reads the context (~200k tokens
  per call in long sessions; 91% of one experiment's calls were Bash). Save by
  making fewer calls (one script for a multi-step check; fewer wakeups) and
  keeping contexts short (one session per unit of work). The amount of code
  written matters little.
- **Wait on changes, not timers**: one resident watcher that wakes the session
  when the controller's `status` changes (in Claude Code, a background command
  that exits on a change); `miyabi-pbs-goal-wakeup` for one long job; long
  timers only as a fallback. About 11% of one series of experiments' tokens
  went to wakeups where nothing had changed.
- **Hand over a state card**, at most ~2 KB, plus the list of constraints in
  force; not the execution log. One takeover read a 22.9 KB handover and an
  84 KB execution log and compacted 46 minutes later.
- **Decision log**: one entry per debatable choice, ≤ 5 lines: question,
  options, choice, reason, where the evidence is, whether it can be reversed.
  Facts are queries of the store, not log lines.
- **Subagents for bounded tasks.** Six resident coding subagents used 2.1× the
  main session's tokens; about 60% of their later deliveries were read-backs
  and re-verification (inferred).

## 6 What skipping each check cost

| Check | Its cost | Cost of skipping it |
| --- | --- | --- |
| target-scale trial with a stop rule | 160–320 node·h, ~2 days | ~4,350 node·h to a negative result |
| gain-dependency list | reading the earlier stage's records | a hypothesis tested at a setting its own data had shown to lose |
| noise floor | < 1 node·h | gates and single-seed differences inside the noise |
| development-metric check | a few evaluations of existing checkpoints | near-random selection; ~68 node·h on a false screen pass |
| stage decision + priced estimate | minutes | ~770 node·h (18% of a study) |
| stop-loss per line | one sentence | five follow-ups on one line; up to one more confirmation round (~140 node·h) |
| retention registry before cleanup | minutes | 10.5 node·h retrained; numbers no longer recomputable |
| diagnosis before rework | 18 minutes | ~4 days without a formal result |
| user limits as checks | one spec field | three corrections in a day; jobs past the cap |
