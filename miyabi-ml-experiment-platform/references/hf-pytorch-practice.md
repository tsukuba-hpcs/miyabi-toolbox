# HF / PyTorch practice on Miyabi-G

Settings fedcl converged on for Transformers, PEFT and lm-evaluation-harness
on GH200 (aarch64), and how to adopt a faster kernel without contaminating a
comparison. The reference code is in the fedcl repository
([reference-implementation.md](reference-implementation.md)): `src/fedcl/training.py`,
`src/fedcl/evaluation.py`, `src/fedcl/versions.py` and `scripts/pilot.pbs`.

## Contents

1. Training
2. Evaluation
3. Environment and venvs
4. Adopting a faster kernel

## 1 Training

- **Trainer arguments**: `save_strategy="no"`, `logging_strategy="no"`,
  `eval_strategy="no"`, `report_to="none"`, `disable_tqdm=True`, and an explicit
  `output_dir` (the run directory), never the default. The Trainer then writes
  nothing; `trainer.save_model()` without a path would write into `output_dir`.
- **Checkpoints**: save only declared ones, with
  `model.save_pretrained(path, selected_adapters=[name], safe_serialization=True)`,
  and delete the generated `README.md`.
- **Started marker**: a `TrainerCallback` that touches `.started` in
  `on_step_end` when `state.global_step == 1`. A failure before it is
  infrastructure (retried automatically); after it, science (kept for review).
- **Batch**: the effective batch is fixed; micro-batch × gradient accumulation
  fills it. The micro-batch changes the numbers, so it is chosen in the smoke
  and then fixed ([experiment-lifecycle.md](experiment-lifecycle.md) §1).
- **Precision**: bf16 base with fp32 trainable LoRA → AMP off (`bf16=False`,
  `fp16=False`).
- **Seeds**: derive every seed from (experiment seed, purpose label), e.g.
  `derive_seed(seed, "lora")`; no ad-hoc constants.
- **Timing**: record each part (load, compile, training, validation,
  generation, gates) in `result.json` under `timing`. The cost model fits
  these; totals alone cannot be scaled.
- **Logs**: write progress lines from code straight into `train.log`; a
  framework may swallow stdout (a Flower ServerApp's output only reaches
  `flwr log`).

## 2 Evaluation

- Pin lm-evaluation-harness to a commit. Package the task YAMLs and build each
  `Dataset` in memory from a frozen local snapshot; write no datasets cache.
  When every selected task is packaged, construct
  `TaskManager(include_path=<package tasks>, include_defaults=False)`: indexing
  the harness's own task YAMLs costs seconds per process.
- Request cache on `/local`; keep no output cache.
- Summary file: metrics, per-question correctness, and metadata (batch
  actually used, question count, load and scoring time, peak memory).
  Per-question outputs go to gzip JSONL.
- Smoke evaluations use `limit` (the first N questions; 10%, at least 32).
- The evaluation batch is a speed knob, not a protocol item: switch to a faster
  one any time and record it in every result. Bigger is not always faster
  (Qwen: 16 beat 32–64).
- **LoRA merged or not.** fedcl evaluated unmerged: bf16 base, fp32 LoRA, and
  the evaluation code refuses other dtypes. `merge_and_unload` is faster but
  rounds W + BA to bf16, so scores are not bitwise identical and greedy
  generation differs on some questions. Evaluate every model of one
  comparison the same way and never switch mid-experiment. Cost of staying
  unmerged: rank-160 adapters took 1.2–3.4× their rank-16 parents' GSM8K time
  (HumanEval and IFEval ~1×; cause not found).
- **Next experiment** (the user's decision for fedcl): first probe the same
  adapter merged vs unmerged (time and per-question agreement) together with
  repeated evaluations (same node, across nodes, 1 vs 2 lanes); then evaluate
  merged from the first evaluation. Comparisons with older unmerged results
  then mix modes — say so wherever they appear.
- **Log-softmax precision.** Comparing summed label log-likelihoods in bf16
  rounds close labels together (0.0625 apart near −10) and breaks ties toward
  the first label; fedcl forces fp32 for its own multiple-choice tasks.
  Whether the harness's upstream tasks (MMLU, FPB) compute it in bf16 is
  unverified and is one lead for the evaluation noise
  ([experiment-lifecycle.md](experiment-lifecycle.md) §4). Check it before
  freezing the protocol.

## 3 Environment and venvs

| Variable / setting | Value | Why |
| --- | --- | --- |
| `PYTHONPATH` | `<snapshot>/src` | run the released code, never the working tree |
| project install | none: `uv sync --locked --no-install-project` | an editable install points at one worktree; a process that forgot `PYTHONPATH` (e.g. an `mpirun` remote rank) silently imports that worktree instead of failing |
| venv | one `.venv` per experiment worktree, synced on the target system's login node (G2-1: 24 s, packages hard-linked from the uv cache); never re-synced while an experiment uses it; a dependency change means a new venv | queued jobs must not see dependencies change; another experiment's venv is not yours to touch |
| `HF_HOME`, `HF_HUB_OFFLINE=1` | shared directory on /work; pinned revisions | compute nodes do not download |
| `TORCHINDUCTOR_CACHE_DIR`, `TRITON_CACHE_DIR` | `/local/$USER/<framework>/<job>/<task>/…` | one 11-node deployment wrote ~26,000 cache files |
| `CC`, `CXX`, `COMPILER_PATH`, `LD_LIBRARY_PATH`, `LIBRARY_PATH` | GCC toolset 14 (`/opt/rh/gcc-toolset-14/root/usr/{bin,lib64,lib}`) | Inductor's SVE2 kernels on aarch64 |
| `module load` | `nvidia/25.9 nv-hpcx/25.9 cuda/13.2` | matches torch 2.14 + cu132 |
| `CUDA_VISIBLE_DEVICES` | one GPU per process | |
| `OMP_NUM_THREADS=8`, `OPENBLAS_NUM_THREADS=1`, `TOKENIZERS_PARALLELISM=false` | | lanes sharing a node must not fight over cores |
| `ARROW_DEFAULT_MEMORY_POOL=system` | | jemalloc on many-core aarch64 burns system time even on small parquet reads |
| CUDA extensions without an aarch64 wheel | build the wheel in a compute job for `sm_90` only, keep it in a /work wheel directory with its SHA-256, point `pyproject.toml` at it (path source; `uv.lock` records the hash) | login nodes do not compile; a swapped wheel is detected |
| environment provenance | every result records the numeric packages' versions (torch, transformers, peft, trl, kernel packages) | results from before and after a kernel or upgrade stay distinguishable |
| optional kernels | import the kernel package explicitly at model load and fail the task if it is missing | transformers silently falls back to the Torch reference with one warning; a broken node then mixes implementations inside one deployment |

`mpirun` does not forward the caller's environment: pass every variable a
remote process needs on its command line (`mpirun … /usr/bin/env K=V … python …`),
`PYTHONPATH` included, and set `umask` there if the group must read its files.
The job-script side (modules, guard, `mpirun` forms) is in the
`miyabi-pbs-shell-template` skill; fedcl's `scripts/pilot.pbs` shows the full set.

**Decide the environment in the smoke.** Search the smoke logs for "falling
back to the reference implementation"-style warnings and settle kernels before
the first development result. G2-1 adopted causal-conv1d mid-search: imported
Qwen screens ran on the Torch reference, later ones on the kernel, so those
cells' screens mix two environments (each result's `environment` tells them
apart).

## 4 Adopting a faster kernel

Install it, then decide with one single-GPU check job:

1. **Operator**: at the model's real shapes, in both memory layouts (the model
   may pass a non-contiguous tensor), eager and `torch.compile`: compare outputs
   and input gradients at the kernel's own test tolerances.
2. **One real micro-batch**: trainable-parameter gradients against the eager
   reference, together with *compiled reference vs eager reference*, which is
   the size of rounding itself.
3. **Short training** (tens of updates) three ways: the reference, a
   **rounding-only variant of the reference** (e.g. fp32 compute rounded once,
   the way the kernel rounds) and the kernel; at a stable learning rate and at
   the largest learning rate in the search space. If kernel-vs-reference is no
   larger than rounding-only-vs-reference, the kernel is correct: record it as
   an environment fact, with no science-version bump.

Do not calibrate with "rerun the reference": training that is bitwise
reproducible gives a distance of 0, and nothing can be judged against it
(fedcl's first check did this and had to be redone). At a learning rate where
trajectories are chaotic under rounding (every pairwise endpoint distance close
to the update itself), short training cannot decide anything; it only shows
that results at that rate carry rounding-level noise.

Example, causal-conv1d 1.7.0 on Qwen3.5 (2026-10-16): operator errors within
upstream tolerances (fp32 2.4e-7, bf16 7.8e-3); one LoRA gradient batch 4.4%
from eager (compiled reference vs eager: 3.7%); 64 updates at lr 1e-4: endpoint
distance / update 0.037 for the kernel vs 0.095 for the rounding-only variant
(V difference 0.005 vs 0.012); at lr 1e-3 all pairs 0.77–0.96 (chaotic).
Result: 0.43 → 0.37 s per update (−14%), peak memory 75.3 → 72.1 GB. The first
build failed at link time on `libctf.so` until GCC toolset 14's `lib64` was on
`LD_LIBRARY_PATH`; then it built in 238 s.
