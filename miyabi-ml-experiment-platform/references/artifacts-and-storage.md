# Artifacts, storage and statistics

The rule: **write to /work only what a declared consumer reads; everything else
lives on `/local` and dies with the task.** /work is limited in files as well
as bytes, and the group quota is shared with other projects. Writing
everything "in case" and cleaning up afterwards cost one project 216 files per
run, 3.37 M compile-cache files and cleanup code changed 11 times.

## Contents

1. /work or /local
2. Artifact contract
3. Retention and deletion
4. Streaming statistics
5. Compile caches
6. Quotas

## 1 /work or /local

- What a later task, the statistics or a human review reads goes straight to
  /work. What is used up inside the task (weights of unconsumed rounds,
  per-round state, launch records, request and compile caches, exported
  temporaries) goes to `/local/$USER/<pkg>/<job>/<task>/`. The executor
  deletes that directory on exit, exceptions included, after logging
  `[local-cache] bytes=… files=…`; the pilot removes `/local/$USER/<pkg>/<job>`
  on every host when it exits.
- Point the Trainer's `output_dir` at the run directory on /work, but with
  `save_strategy="no"`, `logging_strategy="no"` and `report_to="none"`, so the
  Trainer writes nothing during training; code saves the declared checkpoints
  to explicit paths ([hf-pytorch-practice.md](hf-pytorch-practice.md) §1).
- **No resume across jobs.** A failed task reruns from scratch with the same
  seed. That is what lets per-round state live on `/local`. If a single task
  grows beyond a few hours, revisit this: the next attempt would then be a
  declared consumer of a /work checkpoint.
- Models come from a shared `HF_HOME` on /work, filled from the login node with
  pinned revisions; jobs run with `HF_HUB_OFFLINE=1`. Evaluation data is built
  in memory from frozen local snapshots, never written to a datasets cache.

Why: one search run left 216 files (per-round server state, 10 worker
endpoints, 100 per-round worker-state files, 40 launch records). Its 39 MB
per-round worker states were read only by the same worker's next round inside
the same run; the launch logs had no reader at all. One evaluation directory
held 129 files, including request and output caches, task YAML copies, a
`uv.lock` copy, the exported adapter and a 36.6 MB `result.json` with the raw
harness output embedded.

## 2 Artifact contract

Write the table before the code; count the files a smoke leaves against it.

| Task kind | Written to /work | Only on `/local` |
| --- | --- | --- |
| search-stage training | `result.json`, `train.log` | weights, per-round state, launch records, compile caches |
| training with downstream consumers | the above plus the declared checkpoints, `adapter-<ckpt>/` from `save_pretrained` (only `adapter_model.safetensors` and `adapter_config.json`) | rounds and checkpoints nobody consumes |
| evaluation (checkpoint × benchmark) | `eval-<ckpt>-<bench>.json` (metrics, per-question correctness, metadata including the batch actually used and the time); `samples-<ckpt>-<bench>.jsonl.gz` (per-question outputs, for audit) | request cache, exported model, task YAML copies |
| failed task | additionally the last 300 lines of each failed rank's or role's log (`*.err`); deleted after a successful rerun | — |
| saved intermediates for a declared later task (e.g. per-worker endpoints to re-derive from) | one file per task with its recipe, base loss and cost in the metadata (one Qwen set ≈ 0.4 GB); deleted once no declared task can still use it | other per-round state |
| compile caches | one archive per purpose × model × package set under `<R>/caches/` (§5) | the unpacked cache files |
| pilot | PBS output in `control/pbs/`, deleted after 72 h unless an attempt failed | — |

Verified on an 11-node smoke: after its 4 evaluations the adapters were
deleted and the formal-style run directory held 10 files — `result.json`,
`train.log` and 2 files per evaluation — with 0 compile-cache files on /work
and 0.38–0.48 GB / 4,286–5,985 files of cache per worker node on `/local`,
gone at task end.

## 3 Retention and deletion

- Register every kept artifact in the state store's `artifacts` table: file
  count, bytes, consumers, retention, deletion time.
  - `consumers_done`: delete when every declared consumer has finished.
  - `experiment_closed`: keep until the experiment closes. Use it for the
    anchor baselines' formal checkpoints: methods added later may need them
    (a cleaned-up baseline checkpoint once had to be retrained for 1.3 node·h).
- A post-processing task that re-checks its parent's checkpoint (for example a
  bitwise check that re-deriving from saved intermediates reproduces the saved
  adapter) registers as a consumer of that checkpoint. Otherwise the checkpoint
  can go when the parent's evaluations end, and a rerun of the check fails.
- Delete only registered paths; no hashing, no sweeps. The deletion record is
  the receipt. At most 500 deletions per controller cycle; more become a
  `command` cleanup task on a compute node.
- **Before any cleanup, every session's retention needs are in the store.**
  Once a deletion script was blocked by the agent's permission classifier, the
  user was asked to run a cleanup and removed the whole experiment root,
  including the state store, analyses, evaluation summaries and outputs another
  session had just asked to keep: 10.5 node·h retrained, and that experiment's
  numbers could afterwards only be cited, not recomputed.
- When the user asks for a deletion: check the consumers, delete path by path,
  and record what went in the `events` table and the experiment's plan.
- Closing an experiment deletes `experiment_closed` artifacts and the
  per-question samples, and keeps the statistics CSVs, figures,
  `state.sqlite`, each task's `result.json` and the evaluation summaries.

## 4 Streaming statistics

Tabulate each result as it lands, then delete what nothing reads. The number
of files on /work then grows with what is running, not with what has run.

- When a training and all its evaluations are done, the controller appends one
  row to `stats/trainings.csv` (validation score, wall and startup time,
  per-round wall for multi-round protocols, training and validation time, GPU
  seconds, peak memory per role, number of evaluations, job ID, commit) and
  one row per evaluation to `stats/evaluations.csv` (accuracy, macro-F1,
  worst-class recall, all metrics as JSON, wall, load and scoring time, GPU
  scoring seconds, shards, batch, peak memory, compile-cache hit), joined on
  `source_task_id` (empty for base-model evaluations).
- Each result's compact summary also stays in the `results` table, which
  selection reads. Selection never opens run directories.
- Then the run directory loses what nobody reads: outside the formal stage all
  of it (checkpoints follow §3); in the formal stage only the logs, since the
  statistics read `result.json` and the per-question results, and the samples
  stay for audit until closing. Finished `command` tasks (release tests,
  imports) lose their logs and exit records. Failed tasks keep everything for
  review. `debug: true` in the spec keeps logs and samples.
- A table whose header differs from the current one is refused, not appended;
  move it away first.
- Join runtime, queue and GPU use from PBS history into the same tables when
  you need them (`scripts/pbs_time_report.py --jobs` prints per-job rows).

Smoke acceptance: every smoke training and its evaluations are in the tables
and their run directories are gone (`debug: false`).

## 5 Compile caches

- Triton and Inductor caches are per process and large: ~26,000 files for one
  11-node run; one study swept 3,374,653 files (239.5 GB) left by 352 runs.
  They belong on `/local` (`TORCHINDUCTOR_CACHE_DIR`, `TRITON_CACHE_DIR`).
- Share compiled kernels through archives on /work:
  `<R>/caches/compile-<eval|train>-<model>-<package-hash>.tar`, one per
  purpose × model × package set, holding `triton/` and `inductor/`. Each
  process (evaluation shard, each rank of a multi-node run, single-GPU
  training) unpacks it into its `/local` cache at start and, at the end, adds
  the files the archive lacks; writers take turns through a lock file and
  replace the archive by rename, and skip the update when the lock is busy.
  Entries are keyed by the compilers themselves, so a stale entry is merely
  unused.
- Measured (2026-10-17): a cold Qwen evaluation process compiled ~35 s; a
  worker of a multi-round training ~110 s in round 1 and ~40 s again in any
  round that met a new padded length, while the other workers waited. With the
  training archive a multi-round 11-node SmolLM training went 821 → 529 s (round 1
  209 → 82 s, later rounds 115 → 75 s). A Qwen evaluation sharded over 5 nodes
  with 2 lanes per GPU took 51 s cold and 16 s warm (94 s unsharded on one
  node).
- Worth building once compilation exceeds ~20% of a short task. Smoke
  acceptance: no compile-cache files on /work outside `caches/`.

## 6 Quotas

```bash
lfs quota -g <group> /work      # bytes and files, shared by the group's projects
lfs quota -u $USER /work
```

One group's quota on 2026-10-16: 27 TB and 56.62 M files, of which 14.6 TB and
12.9 M files were in use across its projects. Give each task kind a file
budget in the contract and check it in the smoke: a smoke that counts the
files it leaves exposes a cache leak on day one. Compile caches of ~26,000
files per run were noticed on day two of one study, and the exit hook that
removed them landed 30 hours later.
