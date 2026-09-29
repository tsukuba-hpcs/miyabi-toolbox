# vLLM On Miyabi

Use the target allocation and selected virtualenv from
[python-env.md](python-env.md). Inspect the pinned vLLM, PyTorch, CUDA, Triton
and FlashInfer versions before diagnosing or changing the GPU stack. Keep the
project's lock/version policy; an unbounded vLLM install may replace PyTorch.

## Device Visibility

Preserve scheduler-provided `CUDA_VISIBLE_DEVICES` in every rank before importing
vLLM. Do not unset it, replace it with `0,1,...` by list position, or broadcast
rank 0's GPU UUIDs to other nodes. Inspect the effective value in the actual
launcher, since MPI propagation can differ from the parent shell.

UUID support depends on the pinned vLLM implementation. If a device-mapping
traceback rejects `GPU-...` or `MIG-...`, inspect that version's mapping code
before selecting a compatible fix. Do not apply a generic UUID-to-index shim:
it must preserve the exact allocated devices, their order and any MIG instance
identity. CUDA logical indices refer to the visible-device ordering, not an
unconditional physical GPU index. See
[CUDA device enumeration](https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/environment-variables.html#cuda-visible-devices)
and the pinned version of [vLLM's platform code](https://docs.vllm.ai/en/stable/api/vllm/platforms/interface/).

## Conditional Build And Runtime Diagnostics

- If extension/Triton compilation fails on NVHPC-specific unsupported flags,
  test GNU `CC`/`CXX` with the same package versions. For MPI children, apply
  `RUNTIME_CC`/`RUNTIME_CXX` after login-shell initialization as in [mpi.md](mpi.md).
- If FlashInfer sampling fails during JIT/linking, test
  `VLLM_USE_FLASHINFER_SAMPLER=0` only if the pinned version supports it.
- Use `enforce_eager=True` temporarily to isolate compilation/CUDAGraph failures;
  keep it out of normal evaluation defaults unless the project requires it.

Set supported runtime variables before vLLM imports or spawns EngineCore/kernel
workers. Verify the failing path with a fresh task-local cache when necessary,
and remove diagnostic overrides after resolving the cause. Check the pinned
version's [environment-variable documentation](https://docs.vllm.ai/en/stable/configuration/env_vars/).

## PBS/MPI Evaluation

For independent rank-sharded inference, adapt
[mpi-workers.pbs](../assets/pbs/mpi-workers.pbs) with one Python/vLLM process per
node and explicit project entrypoint/arguments. This topology does not provide
vLLM tensor/data parallelism for a model spanning workers; use the project's
supported vLLM launcher for that case.

Pass shared paths and required workload settings through `MPI_ENV_ARGS`. Choose
reusable Hugging Face caches on project storage, respecting existing settings.
For node-local compiler/FlashInfer caches, derive and create the directory in
each rank shell; do not export a scratch path prepared only on rank 0.
Preserve pipeline failure status and use shared durable logs as in the template.

For LoRA evaluation, read the base model and actual adapter rank requirements
from `adapter_config.json`, including any per-layer rank overrides. Match
`max_lora_rank` to supported values in the pinned vLLM release. Use adapter
tokenizer files only when the required files are present; otherwise use the
base tokenizer. Follow [vLLM LoRA support](https://docs.vllm.ai/en/stable/features/lora/)
for the API instead of assuming a fixed rank, memory fraction or eager mode.

Write rank-local outputs in a fresh run directory. Merge only after every
expected rank has succeeded and its outputs are complete; if using done markers,
create them after successful output writes and reject stale markers.
