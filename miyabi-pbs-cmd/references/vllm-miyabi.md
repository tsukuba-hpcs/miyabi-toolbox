# vLLM On Miyabi

Use this reference for vLLM inference, LoRA adapter evaluation, offline
`LLM.generate(...)`, or PBS/MPI jobs that launch vLLM on Miyabi. Follow the
main skill first: do not import vLLM, install a GPU stack, load a model, or run
inference on a login node.

This file records both durable launch rules and version-scoped workarounds.
Apply a workaround that changes compiler, sampler, or device visibility only
when the corresponding failure is observed with the project's pinned versions.

## Contents

- [Version And Installation Discipline](#version-and-installation-discipline)
- [Known Miyabi Failure Modes](#known-miyabi-failure-modes)
- [Safe CUDA Device Handling](#safe-cuda-device-handling)
- [PBS And MPI Pattern](#pbs-and-mpi-pattern)
- [LoRA Evaluation Pattern](#lora-evaluation-pattern)

## Version And Installation Discipline

Inspect the current environment on a compute node before changing it:

```bash
.venv/bin/python -c "import torch, vllm; print('torch', torch.__version__); print('vllm', vllm.__version__)"
uv pip freeze | grep -E '^(torch|vllm|triton|flashinfer)='
```

Prefer the project's lockfile or an exact version compatible with its PyTorch and
CUDA stack. Avoid an unbounded install such as `vllm>=...`, which can replace
PyTorch or transitive GPU packages unexpectedly.

If a pinned vLLM build compiles `fastsafetensors` and fails under NVHPC with an
error such as `nvc++-Error-Unknown switch`, retry on a compute node with GNU
compilers without changing the requested vLLM version:

```bash
CC="$(command -v gcc)" CXX="$(command -v g++)" \
  uv pip install "vllm==<project-version>"
```

Capture `uv pip freeze` before and after installation and re-check both
`torch.__version__` and `vllm.__version__`.

## Known Miyabi Failure Modes

The following workarounds came from a vLLM 0.22 GRPO LoRA evaluation. Treat
them as symptom-driven and version-scoped:

- NVHPC `CC=nvc` or `CXX=nvc++` can break extension or Triton helper builds.
  Switch to GNU compilers only when the error points to unsupported NVHPC
  flags.
- EngineCore, Triton, FlashInfer, and LoRA kernels may spawn subprocesses.
  Required non-scheduler environment values must exist before importing vLLM
  and must be propagated through MPI. Keep device visibility node-local.
- FlashInfer top-k/top-p sampling may fail during JIT or linking. For a version
  that recognizes the setting, isolate the diagnosis with:

  ```bash
  export VLLM_USE_FLASHINFER_SAMPLER=0
  ```

- Compilation or CUDAGraph failures can be isolated with eager mode:

  ```python
  llm = LLM(..., enforce_eager=True)
  ```

Remove diagnostic workarounds after the underlying environment issue is fixed;
do not turn every workaround into a permanent performance default.

## Safe CUDA Device Handling

Preserve the scheduler-provided `CUDA_VISIBLE_DEVICES` by default. Current
vLLM releases have explicit logical-to-physical device mapping. Preserve the
scheduler value unless the pinned version reports a parsing failure.

Some older versions failed while parsing PBS-provided GPU UUIDs such as
`GPU-...`. If that exact failure is reproduced, translate every UUID to its
actual physical index with `nvidia-smi` before importing vLLM:

```python
import os
import subprocess


def translate_gpu_uuids_for_legacy_vllm() -> None:
    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "")
    entries = [item.strip() for item in visible.split(",") if item.strip()]
    if not entries or not any(item.startswith("GPU-") for item in entries):
        return
    if not all(item.startswith("GPU-") for item in entries):
        raise RuntimeError(f"Mixed CUDA_VISIBLE_DEVICES format: {visible}")

    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    uuid_to_index: dict[str, str] = {}
    for line in result.stdout.splitlines():
        index, uuid = (part.strip() for part in line.split(",", maxsplit=1))
        uuid_to_index[uuid] = index

    missing = [uuid for uuid in entries if uuid not in uuid_to_index]
    if missing:
        raise RuntimeError(f"Allocated GPU UUIDs not found by nvidia-smi: {missing}")

    os.environ.setdefault("ORIGINAL_CUDA_VISIBLE_DEVICES", visible)
    os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(
        uuid_to_index[uuid] for uuid in entries
    )


translate_gpu_uuids_for_legacy_vllm()

from vllm import LLM, SamplingParams
```

Never replace UUIDs with `0,1,...` merely by list position. That can select
physical GPUs that PBS did not allocate. Fail closed if any UUID cannot be
resolved, and log both original and translated values.

## PBS And MPI Pattern

Use this pattern for independent rank-sharded evaluation with one Python/vLLM
process per node. Do not use it as a substitute for vLLM's documented
tensor-parallel or data-parallel launcher when the model itself spans workers.

Before this snippet runs, discover the required compiler/MPI/CUDA modules and
load them in the PBS job shell as described in [module.md](module.md). Record a
pager-free `module list` in the job log; do not rely on login-shell module
state.

Initialize required paths before building the MPI environment array:

```bash
: "${PROJECT_ROOT:?Set PROJECT_ROOT}"
: "${PYTHON_BIN:?Set PYTHON_BIN}"
: "${ENTRYPOINT:?Set ENTRYPOINT}"
: "${MODEL_DIR:?Set MODEL_DIR}"
: "${WORLD_SIZE:?Set WORLD_SIZE}"
: "${LOG_ROOT:?Set LOG_ROOT}"
declare -a EVAL_ARGS

HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}"
HF_DATASETS_CACHE="${HF_DATASETS_CACHE:-$HF_HOME/datasets}"
TOKENIZERS_PARALLELISM="${TOKENIZERS_PARALLELISM:-false}"
NCCL_DEBUG="${NCCL_DEBUG:-WARN}"
OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
FLASHINFER_WORKSPACE_BASE="${FLASHINFER_WORKSPACE_BASE:-${TMPDIR:-$PROJECT_ROOT/.cache}/flashinfer-${PBS_JOBID:-manual}}"
mkdir -p "$FLASHINFER_WORKSPACE_BASE" "$LOG_ROOT"

MPI_ENV_ARGS=(
  "PROJECT_ROOT=$PROJECT_ROOT"
  "PYTHON_BIN=$PYTHON_BIN"
  "ENTRYPOINT=$ENTRYPOINT"
  "MODEL_DIR=$MODEL_DIR"
  "HF_HOME=$HF_HOME"
  "HF_DATASETS_CACHE=$HF_DATASETS_CACHE"
  "TOKENIZERS_PARALLELISM=$TOKENIZERS_PARALLELISM"
  "NCCL_DEBUG=$NCCL_DEBUG"
  "OMP_NUM_THREADS=$OMP_NUM_THREADS"
  "FLASHINFER_WORKSPACE_BASE=$FLASHINFER_WORKSPACE_BASE"
)

[[ -n "${VLLM_USE_FLASHINFER_SAMPLER:-}" ]] && \
  MPI_ENV_ARGS+=("VLLM_USE_FLASHINFER_SAMPLER=$VLLM_USE_FLASHINFER_SAMPLER")
[[ -n "${CC:-}" ]] && MPI_ENV_ARGS+=("CC=$CC")
[[ -n "${CXX:-}" ]] && MPI_ENV_ARGS+=("CXX=$CXX")

mpirun \
  --mca mpi_abort_print_stack 1 \
  --report-bindings \
  --bind-to none \
  -np "$WORLD_SIZE" \
  /usr/bin/env "${MPI_ENV_ARGS[@]}" \
  bash -lc '
    set -euo pipefail
    echo "vllm rank=${OMPI_COMM_WORLD_RANK:?}/${OMPI_COMM_WORLD_SIZE:?} host=$(hostname)"
    exec "$PYTHON_BIN" "$ENTRYPOINT" "$@"
  ' bash "${EVAL_ARGS[@]}" 2>&1 | tee "$LOG_ROOT/eval.log"
```

Do not combine this `/usr/bin/env` pattern with `mpirun -x` or MCA environment
lists. Keep the MPI parent unbound unless an explicit CPU-affinity plan exists.
Do not copy a rank-0 GPU UUID list to every node through `MPI_ENV_ARGS`; preserve
PBS/site node-local device visibility and print it from every rank. If the
launcher replaces node-local visibility with rank-0 values, stop and adapt a
known-working site launcher instead of guessing GPU ids.

## LoRA Evaluation Pattern

Infer the base model and rank from `adapter_config.json`. Use tokenizer files
from the adapter directory only when they actually exist:

```python
import json
from pathlib import Path

from vllm import LLM, SamplingParams
from vllm.lora.request import LoRARequest


model_dir = Path("outputs/<adapter_output>")
adapter_config = json.loads((model_dir / "adapter_config.json").read_text())
base_model = adapter_config["base_model_name_or_path"]
lora_rank = int(adapter_config.get("r", 16))
tokenizer = model_dir if (model_dir / "tokenizer_config.json").exists() else base_model

llm = LLM(
    model=base_model,
    tokenizer=str(tokenizer),
    dtype="auto",
    tensor_parallel_size=1,
    gpu_memory_utilization=0.80,
    enforce_eager=True,  # diagnostic only; remove after resolving the failure
    enable_lora=True,
    max_lora_rank=lora_rank,
)

outputs = llm.generate(
    prompts,
    SamplingParams(max_tokens=64, temperature=0.0, top_p=1.0),
    lora_request=LoRARequest("adapter", 1, str(model_dir)),
)
```

For rank-sharded evaluation, write rank-local CSV/JSON files and a done marker.
Let rank 0 merge only after every expected marker exists. Avoid concurrent
writes to one output file.

Re-check device and LoRA behavior against the current
[vLLM platform](https://docs.vllm.ai/en/stable/api/vllm/platforms/index.html)
and [LoRA](https://docs.vllm.ai/en/stable/features/lora/) documentation before
carrying a version-specific workaround into a new environment.
