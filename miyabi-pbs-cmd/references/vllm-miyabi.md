# vLLM on Miyabi

Use this reference when implementing or debugging vLLM inference, LoRA adapter evaluation, offline `LLM.generate(...)`, or PBS/MPI jobs that launch vLLM on Miyabi.

Follow the main skill first: never import vLLM, load models, install heavy GPU stacks, or run inference on login nodes. Start real vLLM debugging with a 1-node interactive allocation, then move to 2 nodes only after the 1-node path works.

## Common Miyabi Issues

These are known failure modes from a working GRPO LoRA evaluation on Miyabi with vLLM 0.22:

- vLLM install may build `fastsafetensors`; with NVHPC defaults it can fail with `nvc++-Error-Unknown switch`. Install on an interactive node and force GNU compilers:

```bash
CC="$(command -v gcc)" CXX="$(command -v g++)" uv pip install 'vllm>=0.8.0'
```

- Installing vLLM can adjust the local PyTorch version. After installation, inspect the environment on a compute node:

```bash
.venv/bin/python -c "import torch, vllm; print('torch', torch.__version__); print('vllm', vllm.__version__)"
```

- PBS may set `CUDA_VISIBLE_DEVICES` to GPU UUIDs such as `GPU-...`. vLLM can fail while parsing those as integer device ids. Normalize the visible device list to local numeric ids before importing vLLM.
- vLLM EngineCore, Triton, FlashInfer, and LoRA kernels may spawn subprocesses. Environment fixes must be present before `from vllm import LLM` and should also be passed through MPI.
- FlashInfer top-k/top-p sampler may trigger JIT or link errors on Miyabi. Disable the vLLM FlashInfer sampler for debug/evaluation runs:

```bash
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"
```

- Prefer eager mode while debugging vLLM on Miyabi. This reduces `torch.compile` and CUDAGraph-related JIT paths:

```python
llm = LLM(..., enforce_eager=True)
```

## Runtime Environment Guard

Put this guard near the start of a vLLM entrypoint and call it before importing `vllm`, `torch`, or model code:

```python
import os
import shutil
from pathlib import Path


def normalize_cuda_visible_devices_for_vllm() -> None:
    visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
    if not visible_devices:
        return
    entries = [entry.strip() for entry in visible_devices.split(",") if entry.strip()]
    if not entries or not any(entry.startswith("GPU-") for entry in entries):
        return
    os.environ.setdefault("ORIGINAL_CUDA_VISIBLE_DEVICES", visible_devices)
    os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(str(index) for index, _ in enumerate(entries))


def normalize_compilers_for_runtime() -> None:
    for env_name, bad_basename, replacement_name in (
        ("CC", "nvc", "gcc"),
        ("CXX", "nvc++", "g++"),
    ):
        current = os.environ.get(env_name, "")
        if current and Path(current).name != bad_basename:
            continue
        replacement = shutil.which(replacement_name)
        if replacement:
            os.environ[env_name] = replacement


def configure_vllm_miyabi_runtime() -> None:
    os.environ.setdefault("VLLM_USE_FLASHINFER_SAMPLER", "0")
    normalize_compilers_for_runtime()
    normalize_cuda_visible_devices_for_vllm()


configure_vllm_miyabi_runtime()

from vllm import LLM, SamplingParams
```

Why this is needed:

- `CUDA_VISIBLE_DEVICES=GPU-...` can cause `ValueError: invalid literal for int() with base 10: 'GPU-...'` inside vLLM device capability checks.
- `CC=nvc` or `CXX=nvc++` can break Triton helper builds with flags such as `-Wno-psabi` or Python extension build flags.
- `VLLM_USE_FLASHINFER_SAMPLER=0` keeps top-k/top-p sampling on the PyTorch-native path. FlashAttention may still be used separately.

## PBS/MPI Snippet

For vLLM inference under MPI, keep one Python process per node unless the project explicitly needs tensor parallel or multiple local workers. Pass environment with `/usr/bin/env`, not `mpirun -x`.

```bash
export TOKENIZERS_PARALLELISM="${TOKENIZERS_PARALLELISM:-false}"
export NCCL_DEBUG="${NCCL_DEBUG:-WARN}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

if [[ -z "${CC:-}" || "$(basename "$CC")" == "nvc" ]]; then
  export CC="$(command -v gcc)"
fi
if [[ -z "${CXX:-}" || "$(basename "$CXX")" == "nvc++" ]]; then
  export CXX="$(command -v g++)"
fi

# Isolate FlashInfer cache from stale user-level builds while debugging.
export FLASHINFER_WORKSPACE_BASE="${FLASHINFER_WORKSPACE_BASE:-$PROJECT_ROOT}"

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
  "VLLM_USE_FLASHINFER_SAMPLER=$VLLM_USE_FLASHINFER_SAMPLER"
  "CC=$CC"
  "CXX=$CXX"
  "FLASHINFER_WORKSPACE_BASE=$FLASHINFER_WORKSPACE_BASE"
)

mpirun \
  --mca mpi_abort_print_stack 1 \
  --report-bindings \
  --bind-to core \
  -np "$WORLD_SIZE" \
  /usr/bin/env "${MPI_ENV_ARGS[@]}" \
  bash -lc '
    set -euo pipefail
    echo "vllm rank=${OMPI_COMM_WORLD_RANK:?}/${OMPI_COMM_WORLD_SIZE:?} host=$(hostname)"
    exec "$PYTHON_BIN" "$ENTRYPOINT" "$@"
  ' bash "${EVAL_ARGS[@]}" 2>&1 | tee "$LOG_ROOT/eval.log"
```

## LoRA Evaluation Pattern

For PEFT/LoRA adapter outputs, infer the base model from `adapter_config.json`, load the base model in vLLM, and pass the adapter with `LoRARequest`.

```python
import json
from pathlib import Path

from vllm import LLM, SamplingParams
from vllm.lora.request import LoRARequest


model_dir = Path("outputs/<adapter_output>")
adapter_config = json.loads((model_dir / "adapter_config.json").read_text())
base_model = adapter_config["base_model_name_or_path"]
lora_rank = int(adapter_config.get("r", 16))

llm = LLM(
    model=base_model,
    tokenizer=str(model_dir),
    dtype="bfloat16",
    tensor_parallel_size=1,
    gpu_memory_utilization=0.80,
    enforce_eager=True,
    enable_lora=True,
    max_lora_rank=lora_rank,
)

outputs = llm.generate(
    prompts,
    SamplingParams(max_tokens=64, temperature=0.0, top_p=1.0),
    lora_request=LoRARequest("adapter", 1, str(model_dir)),
)
```

For distributed evaluation jobs, shard input rows by MPI rank and write rank-local CSV/JSON files first. Let rank 0 merge only after all ranks have written a done marker. This keeps failures diagnosable and avoids concurrent writes to one CSV.

## Debug Workflow

1. On the login node, only edit files and run static checks such as `bash -n` and bytecode compilation of files that do not import vLLM.
2. Request 1-node interactive first with `walltime=00:30:00`. Use `01:00:00` only when compile/download/model-init time clearly needs it.
3. On the 1-node allocation, confirm `hostname`, install or fix dependencies, import vLLM, load the real model/backend, and run a tiny inference job.
4. After each attempt, run `qstat "$PBS_JOBID"` and decide whether enough walltime remains for another full attempt. If not, exit and request a fresh allocation.
5. After 1-node passes, request the 2-node allocation with `walltime=00:10:00` and run the smallest real multi-node evaluation that proves both ranks participate and output artifacts are written.
