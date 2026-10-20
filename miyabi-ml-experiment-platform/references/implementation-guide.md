# Implementation guide

How to build the control plane of [architecture.md](architecture.md): which
modules, in which order, what the first implementation got wrong, and how to
accept it on Miyabi-G. Sizes and results come from an earlier implementation
that ran several studies on Miyabi-G in October 2026. `<pkg>` is your Python
package, `<R>` an experiment root, and `EXP_` the prefix of the environment
variables the platform passes; choose your own names.

## Contents

1. Modules and build order
2. Pilot job script
3. Requirements the first implementation missed
4. Conventions
5. Tests and acceptance

## 1 Modules and build order

| Module | Responsibility | Size in the earlier implementation |
| --- | --- | --- |
| `<pkg>/control/db.py` | state store schema and transactions | ~170 lines |
| `<pkg>/control/spec.py` | load and expand the spec into tasks; deterministic task IDs; the `apply` diff | ~520 |
| `<pkg>/control/pbs.py` | `qsub`, job table, `qstat --limit`, routing, walltime rounding | ~130 |
| `<pkg>/control/pilot.py` + `scripts/pilot.pbs` | bad-node gate, claims, execution, in-place evaluation, lanes, shards, `/local` cleanup | ~360 + ~30 |
| `<pkg>/control/controller.py` + `__main__.py` | the cycle, releases, `stale`, holds, `withdraw`, loop restart, commands | ~960 + ~90 |
| `<pkg>/control/estimate.py` | cost classes, fitted parts, gate, stage bounds | ~200 |
| `<pkg>/control/select.py` | selection, promotion, TPE | ~260 |
| `<pkg>/control/reuse.py` | binding identical trainings (`alias`, `rederive`) | ~270 |
| `<pkg>/control/ledger.py` | statistics tables, run-directory cleanup | ~140 |
| `<pkg>/run.py` | task executor and its kind table | ~360 |
| `<pkg>/compile_cache.py` | compile-cache archives | ~110 |
| `<pkg>/methods.py`, `<pkg>/versions.py` | method registry, science versions | ~50 each |
| `tests/control/`, `tests/canary/` | unit tests, numeric canaries | ~100 test functions |

The control plane came to ~3,100 lines; the one it replaced, which bound work at
`qsub`, had 8,130. The engines (training, evaluation, statistics) come on top.
For scale: with the engines already written, it went from its first commit to
a running 11-node smoke in about 70 minutes of agent work, after a design
document, and took 37 more commits over the next five days while experiments
ran on it.

| Generic: write once | Experiment-specific: write per study family |
| --- | --- |
| state store, PBS access, pilot protocol | spec expansion: cells, protocols, smoke variants, carried selections (keep its contract: deterministic task IDs, held stages, `origin`) |
| controller cycle, release and `stale` logic, holds, `withdraw`, loop restart | executor kinds and their outputs; launch scripts of multi-role protocols |
| cost-model mechanism (classes, longest run, `expected_seconds`, gate, bounds) | part formulas (e.g. per-round costs) and cost-class names |
| compile-cache archives, ledger mechanism | ledger columns; method registry entries, science-version paths, canary model; reuse rules (only if methods share identical work) |

**Build order.** Each step ends with something that runs on a compute node:

1. Contracts on paper: spec field groups, task definition and states, artifact
   table, retention, release and science-version rules (SKILL.md, "Building
   the platform").
2. State store and spec expansion, with unit tests for ID stability, the
   `apply` diff and holds.
3. PBS access with stub commands in the tests: parse the job ID from `qsub`
   (record an unparsable reply, never retry it blindly), liveness from the job
   table, admission from `qstat --limit`, routing, walltime rounding.
4. Pilot, job script and an executor with only the `command` kind. Run the
   two-pilot integration test and the bad-node drill (§5).
5. Releases: `git archive` snapshots, the release-test task, `release.json`.
6. Controller cycle and `loop`: import, classify, release dependants, write
   queue files, submit, `status`; the 2,000-task cycle test.
7. Engines as executor kinds (`train`, `evaluate`, `postprocess`), the method
   registry, science versions and canaries; in-place evaluation in the pilot.
   Run the smoke of one method × model.
8. Artifact registry, ledger and deletion by consumers; check the smoke's file
   contract.
9. Cost model from the smoke's per-part timings, `estimate` and the gate.
10. Selection and search; then, when the workload needs them, sharded
    evaluation, lanes per GPU, compile-cache archives and reuse.

The controller runs from the working tree on the login node
(`PYTHONPATH=src` with the experiment's venv); pilots and tasks run only from
release snapshots.

## 2 Pilot job script

Copy [`assets/pilot.pbs`](../assets/pilot.pbs) to `scripts/pilot.pbs` and fill
every `<...>`:

- `#PBS -W group_list=`: the literal project group (PBS does not expand
  variables in `#PBS` lines).
- `MIYABI_PBS_CMD_ROOT`: the absolute directory of the `miyabi-pbs-cmd` skill,
  whose `context.py --require-compute` refuses login nodes and foreign
  allocations. Without that skill, use the inline check the template shows.
- `PYTHON_BIN` (the experiment's venv), `HF_HOME` (a shared directory on /work),
  `PILOT_MODULE`, and a `module load` line that matches your torch build.

The controller submits from `<R>/control/pbs/`, so the `-j oe` output lands on
/work, with `-N <exp>-n<nodes>` (a prefix that `scripts/pbs_time_report.py
--name` can isolate) and `-v EXP_ROOT=<R>,EXP_WALLTIME_S=…,EXP_QUEUE=…,EXP_PILOT_SCRIPT=…`.
`EXP_PILOT_SCRIPT` is the script inside the release snapshot, so a
resubmission runs the same file. A release test adds `EXP_SNAPSHOT`; a bad-node
resubmission adds `EXP_RESUBMIT_OF=<job>`, which stops it from resubmitting
again.

The pilot adds each task's variables: `PYTHONPATH=<snapshot>/src`,
`CUDA_VISIBLE_DEVICES`, the threads and caches of
[hf-pytorch-practice.md](hf-pytorch-practice.md) §3 (caches under the task's
`/local` directory), `EXP_LOCAL_DIR` (that directory) and
`EXP_DEPLOYMENT_INDEX` (a counter of multi-role runs in this pilot, so each
picks its own ports). The executor passes `EXP_COMPILE_CACHE` (the archive to
unpack) and `EXP_EVAL_SHARD` (a shard's index) to the processes it starts.
`mpirun` forwards none of the caller's environment, so keep one list of the
variables a remote task needs (paths, compilers, `HF_*`, threads, caches,
`PBS_JOBID`, `PYTHONPATH`, these `EXP_*`) and put them on the command line:
`mpirun … /usr/bin/env K=V … python -m <pkg>.run <config>`.

## 3 Requirements the first implementation missed

Build these in from the start; each was found live.

1. **Claim margin.** Claim generation-heavy evaluations at
   max(measured × 1.5, declared × 1.2), or let a pilot claim the task that
   triggered its submission first: the GSM8K evaluation of a merge step's
   output was killed at 849 of 1,319 questions
   ([scheduling-and-cost.md](scheduling-and-cost.md) §4).
2. **Walltime kills are `lost`.** Write in `.failed` whether the task ended
   after PBS's stop signal, classify those `lost`, and rerun once with more
   walltime ([scheduling-and-cost.md](scheduling-and-cost.md) §5).
3. **Caps everywhere.** Apply `max_walltime_s` after every walltime source,
   the fixed `pilot_walltime_s` included, and cap node counts for limits such as
   "11 nodes × 3 h" ([scheduling-and-cost.md](scheduling-and-cost.md) §1).
4. **Multi-node allocations run work that uses their width.** Serial
   single-node tails go to 1-node pilots
   ([scheduling-and-cost.md](scheduling-and-cost.md) §8).
5. **Dry-run scenarios in the release tests**, with a stub PBS: pilots that a
   reservation keeps from starting, counted as cover for short tasks;
   admission slots full; a loop whose code changed under it (compare the code
   hash at loop start and every cycle); submission order across stages; a
   walltime kill; a task that fits no pilot.
6. **Pilots that start before a release.** A pilot that finds no published
   release (none yet, or the first one still under test) exits; for a
   multi-node pilot that throws away its queue wait. Let multi-node pilots
   wait a few minutes for it ([`assets/pilot.pbs`](../assets/pilot.pbs) waits
   up to 10 minutes; unverified).
7. **Canaries beyond training**: evaluation parsing, data preparation and
   generation, each on fixed samples against golden outputs.
8. **Concurrent claim drill**: several pilots start together and loop over the
   same tasks; every task runs exactly once.
9. **The statistics and report step** reads the files the executor writes.
   Test it on the smoke's outputs, not after the formal stage.
10. **Retire what the new platform replaces.** The earlier repository kept
    ~7,600 lines of modules no entry point reached and an old 41-file test
    suite; later readers took them for the current design. Delete them, or
    keep them out of the release gate and code search.

Unverified designs worth evaluating: early submission of soon-runnable tasks
([scheduling-and-cost.md](scheduling-and-cost.md) §3); an automatic cost gate
once every method has its own smoke measurement (the gate only reported);
comparing queue waits by (nodes, walltime class), to see whether the longer
walltime of in-place evaluation costs more wait than a separate evaluation
pilot would.

## 4 Conventions

- One environment-variable prefix for everything the platform passes.
- **Bad nodes are data**: a constant list in the pilot (2026-10: `mg0882`, CUDA
  illegal memory access five times), reported to the site. Drill the gate with
  a fake node file and a stub `qsub`.
- **Release test**: `python -m pytest -q -p no:cacheprovider tests/control
  tests/canary` from the snapshot, on a compute node (`no:cacheprovider` keeps
  pytest from writing into the snapshot).
- One venv per experiment worktree, no editable install of the project
  ([hf-pytorch-practice.md](hf-pytorch-practice.md) §3).
- **Tests check invariants, not counts.** Tests that read the real spec assert
  properties ("every smoke variant belongs to a cell"), never counts or
  positions; when a test needs a fixed shape, build a minimal spec from the
  real one. A release was refused because six tests assumed exactly one smoke
  variant.
- `status` at most 40 lines. Decision log entries at most 5 lines; facts are
  queries of the store.

## 5 Tests and acceptance

| Level | Content | Where | Status |
| --- | --- | --- | --- |
| control-plane unit tests | spec expansion and ID stability, claim exclusivity, bad-node gate, routing, task lifecycle (stub PBS), failure classification, holds, `stale`, selection order, TPE reproducibility, a 2,000-task cycle ≤ 60 s, each cell's search space vs a frozen old grid (when migrating), `withdraw` | release-test task on a compute node | verified: passed on every release (e.g. 49 tests in 44 s, 2026-10-18) |
| numeric canaries | per method, three optimizer steps of a tiny model vs `golden.json` | same | verified; golden files generated on compute nodes |
| two-pilot integration | two 1-node pilots, three sleep tasks and one too long to fit: each short task runs exactly once, the long one is never claimed, an idle pilot exits within 60 s | `debug-g` | verified |
| bad-node drill | fake node file, stub `qsub` | unit test | verified |
| smoke | every method × model at 10%, file contract, cost fit | `debug-g`, up to 11 nodes | verified on a 23-training matrix and three later experiments |
| mini-experiment | one model, two methods, two seeds, spec to CSV and figures, four mid-run changes | `regular-g` | unverified: designed, never run |

Also verified on Miyabi-G: in-place evaluation on remote nodes, sharded
evaluation, lanes per GPU, compile-cache archives, reuse by alias and by
re-derivation, in-place post-processing with bitwise checks of the parent,
short tasks routed to `debug-g`, walltime from 1.5× estimates, a 220-task
formal stage, and a variant run on a subset of seeds.

Not validated: DDP/FSDP shapes, MIG and `prepost` queues, early submission, an
automatic cost gate, the statistics and report step on the new evaluation
files, a deliberate concurrent `os.link` race across nodes, and whether
confirm-fidelity rankings match full-budget rankings.
