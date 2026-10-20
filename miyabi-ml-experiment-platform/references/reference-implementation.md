# Reference implementation: fedcl exp-v2

Repository `/work/xg24i002/x10041/fedcl-v2`, branch `exp-v2` (83b2276 when this
was written, 2026-10-20). Read the code there; the framework document §15–§17
tracks its status.

## Contents

1. File map
2. What to keep and what to replace
3. Porting checklist
4. Tests and acceptance
5. Open problems and backlog

## 1 File map

| Component | File | Lines at 83b2276 |
| --- | --- | --- |
| state store | `src/fedcl/control/db.py` | 169 |
| spec expansion | `src/fedcl/control/spec.py` | 523 |
| controller cycle and commands | `src/fedcl/control/controller.py`, `__main__.py` | 957 + 88 |
| PBS access (qsub, job table, limits, routing, walltime) | `src/fedcl/control/pbs.py` | 129 |
| pilot | `src/fedcl/control/pilot.py`, `scripts/pilot.pbs` | 364 + 29 |
| cost model, gate, stage bounds | `src/fedcl/control/estimate.py` | 201 |
| selection and TPE | `src/fedcl/control/select.py` | 256 |
| reuse of identical client training | `src/fedcl/control/reuse.py` | 274 |
| statistics tables and run-file removal | `src/fedcl/control/ledger.py` | 138 |
| task executor | `src/fedcl/run.py` | 355 |
| compile-cache archives | `src/fedcl/compile_cache.py` | 114 |
| method registry, science versions | `src/fedcl/methods.py`, `src/fedcl/versions.py` | 53 + 52 |
| federation launch | `scripts/federation.sh`, `scripts/federation_worker.{sh,py}` | — |
| kernel wheel build and numeric check | `scripts/build_causal_conv1d.pbs`, `scripts/check_causal_conv1d.{pbs,py}` | — |
| tests | `tests/control/` (9 files), `tests/canary/` | 103 test functions |
| example specs | `configs/g21/experiment.yaml`, `configs/h0004d/experiment.yaml` | — |

The control plane (`src/fedcl/control/`) is ~3,100 lines; the control plane it
replaced had 8,130. The controller runs from the working tree on the login
node (`PYTHONPATH=src`); pilots and tasks run from release snapshots.

The repository also holds ~7,618 lines of earlier modules that v2's entry
points no longer reach (from an import analysis only) and `tests/stage2` (41
files, 10,262 lines) that release tests do not run, plus scripts such as
`scripts/make_snapshot.sh` that point at a deleted worktree. Do not port them
or read them as the current design: start from `fedcl.control`,
`fedcl.control.pilot` and `fedcl.run` and follow the imports.

## 2 What to keep and what to replace

| Keep (Miyabi- and protocol-level) | Replace (fedcl science) |
| --- | --- |
| `db.py` schema and transactions | `spec.py`: cells, protocols, smoke variants, carried selections (keep its contract: deterministic task IDs, held stages, `origin`) |
| `pbs.py`: routing, admission from `qstat --limit`, liveness from the job table | `run.py` kinds and their outputs |
| `pilot.py` protocol: bad-node gate, claims by `os.link`, `.started`/`.failed`, in-place evaluation, lanes, shards, `/local` cleanup | `methods.py` entries, `versions.py` paths, canary model |
| controller cycle, release/stale logic, holds, `withdraw`, loop restart on code change | `reuse.py` (only if your methods share identical work) |
| `estimate.py` mechanism (cost classes, longest measured run, `expected_seconds`, gate, bounds) | the part formulas (federation rounds) and cost-class names |
| `compile_cache.py`, `ledger.py` mechanism | `ledger.py` columns; `scripts/federation*` |

## 3 Porting checklist

- Rename the environment prefix (`FEDCL_ROOT`, `FEDCL_WALLTIME_S`,
  `FEDCL_QUEUE`, `FEDCL_PILOT_SCRIPT`, `FEDCL_SNAPSHOT`, `FEDCL_RESUBMIT_OF`,
  `FEDCL_LOCAL_ROOT`, `FEDCL_LOCAL_DIR`, `FEDCL_COMPILE_CACHE`,
  `FEDCL_EVAL_SHARD`, `FEDCL_DEPLOYMENT_INDEX`, `FEDCL_REPO`) and the package in
  `python -m fedcl.control.pilot` / `fedcl.run`.
- `scripts/pilot.pbs`: `#PBS -W group_list=` (fedcl: `xg24i002`); the
  operating-plane guard (`context.py --require-compute` of `miyabi-pbs-cmd`;
  exp-v2 calls it through the Codex link
  `/work/xg24i002/x10041/.codex/skills/miyabi-pbs-cmd`, whose target is
  `/work/xg24i002/x10041/miyabi-toolbox/miyabi-pbs-cmd`);
  `PYTHON_BIN` (your experiment's venv); `HF_HOME` (fedcl:
  `/work/xg24i002/x10041/hf_home`). Keep the module and GCC toolset lines.
- Pilot job names (`fedcl-n<N>`): choose a prefix that `pbs_time_report.py
  --name` can isolate.
- Known bad nodes: `BAD_NODES` in `pilot.py` (fedcl: `mg0882`, still to be
  reported to the site). Keep it as data.
- Release test command: `TEST_ARGV` in `controller.py` (pytest over
  `tests/control` and `tests/canary`).
- New venv per experiment worktree ([hf-pytorch-practice.md](hf-pytorch-practice.md) §3).
- Tests that read the real spec check only invariant properties ("every smoke
  variant is in a cell"), never counts or positions; build a minimal spec from
  the real one when a fixed shape is needed. A release (945d7ad) was refused
  because six tests assumed exactly one smoke variant.

## 4 Tests and acceptance

| Level | Content | Where | Status |
| --- | --- | --- | --- |
| control-plane unit tests | spec expansion and ID stability, claim exclusivity, bad-node gate (stub `qsub`), routing, task lifecycle (stub PBS), failure classification, holds, `stale`, selection order, TPE reproducibility, a 2,000-task cycle, each cell's space vs the frozen grid, `withdraw` | release-test task on a compute node | pass on every release (e.g. 49 tests in 44 s, 2026-10-18) |
| numeric canaries | one tiny federated round per method vs `golden.json` | same | golden files generated on compute nodes |
| I1 integration | two 1-node pilots, three short tasks, one that cannot fit | debug-g | verified |
| bad-node drill | fake nodefile, stub `qsub` | unit test | verified |
| smoke | every method × model at 10%, file contract, cost fit | debug-g, 11 nodes | verified for the G2-1 matrix (23 trainings) and H-0004b/c/d |
| I4 mini-experiment | one model, two methods, two seeds, spec to CSV and figures, four mid-run changes | regular-g | design, not run |

Verified on Miyabi beyond these: in-place evaluation on remote nodes, sharded
evaluation, lanes per GPU, compile-cache archives, reuse by alias and by
re-aggregation, in-place server-side post-processing with bitwise parent
checks, short tasks routed to `debug-g`, walltime from 1.5× estimates, a
220-task formal stage (H-0004d P2) and a variant run on a subset of seeds.

Not validated: DDP/FSDP shapes, MIG and `prepost` queues, early submission of
soon-runnable tasks, an automatic cost gate (it only reports), the v2
statistics and report step, a deliberate concurrent `os.link` race across
nodes, and whether confirm-fidelity rankings match full-budget rankings.

## 5 Open problems and backlog

In rough order of value for the next experiment:

1. **Claim margin.** Claim generation evaluations at 1.5× (or
   max(measured × 1.5, declared × 1.2)), or let a pilot claim the task that
   triggered it first ([scheduling-and-cost.md](scheduling-and-cost.md) §8).
2. **Walltime kills** recorded as `lost` (mark tasks that end after the stop
   signal) and rerun automatically with more walltime.
   **Caps enforced everywhere**: `max_walltime_s` also over a stage's fixed
   `pilot_walltime_s`, and a node-count cap for limits such as "11 nodes × 3 h".
3. **Multi-node jobs train only**; serial single-node tails go to 1-node
   pilots or spread over the allocation. Expected saving ~65 node·h, 26% of the
   h0004d root.
4. **Dry-run scenarios** in the release tests (a pilot stuck behind a
   reservation; admission slots full) and a code-hash check when a loop starts.
5. **Evaluation protocol**: noise probe, merged-LoRA evaluation from the first
   evaluation, the bf16 log-softmax lead ([hf-pytorch-practice.md](hf-pytorch-practice.md) §2).
6. **Canaries** for evaluation parsing, data preparation and generation.
7. **A pilot that starts before any release exits** and wastes its queue wait;
   multi-node pilots should wait a few minutes while a release is under test.
8. **Concurrent claim drill**: several pilots starting together, each looping
   over the same empty tasks.
9. **In-place evaluation and queue wait**: compare waits by (nodes, walltime
   class); if longer pilots wait more than a separate evaluation pilot would,
   split evaluation back out.
10. **Early submission** of tasks whose dependencies will finish within the
    shape's median queue wait.
11. **Automatic cost gate** once every method has its own smoke measurement.
12. **Statistics and report** in v2 (`statistics.py`, `plotting.py` must read
    the new evaluation files).
13. **Old code**: delete the unreachable modules and `tests/stage2`, or move
    them out of the release gate and code search; then run I4.
