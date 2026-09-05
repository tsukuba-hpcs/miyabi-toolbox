# PyTorch Torchrun PBS Pattern

Use this for code that calls `torch.distributed.init_process_group(...)` and expects `torchrun` environment variables such as `RANK`, `WORLD_SIZE`, `LOCAL_RANK`, `MASTER_ADDR`, and `MASTER_PORT`.

PBS provides `$PBS_NODEFILE`; compute `MASTER_ADDR` from its first host. Run
one MPI supervisor per node; each supervisor starts `torchrun`, which spawns
`NPROC_PER_NODE` local workers. Pass variables to MPI children with
`/usr/bin/env "${MPI_ENV_ARGS[@]}"`, not `mpirun -x`, and derive `node_rank`
inside the MPI child shell from `OMPI_COMM_WORLD_RANK`.

## Contents

- [PBS Template](#pbs-template)
- [Python Entrypoint Expectations](#python-entrypoint-expectations)
- [Notes](#notes)

## PBS Template

Before writing the PBS directive, derive the project group with `groups` and fill the literal group value into `#PBS -W group_list=...`; PBS directives do not expand shell variables.

```bash
#!/bin/bash
#PBS -q regular-g
#PBS -W group_list=<group_id_from_groups>
#PBS -l select=<num_nodes>:mpiprocs=1
#PBS -l walltime=01:00:00
#PBS -j oe
#PBS -m ae

set -eEuo pipefail
trap 'echo "[ERROR] Failed at line $LINENO" >&2' ERR

export PAGER=cat MODULES_PAGER=cat LMOD_PAGER=cat
REQUIRED_MODULES=(
  "<compiler-or-runtime-module/version>"
  "<mpi-module/version>"
)
for module_name in "${REQUIRED_MODULES[@]}"; do
  module load "$module_name"
done
module list 2>&1 | cat

PROJECT_ROOT="${PROJECT_ROOT:-${PBS_O_WORKDIR:-$PWD}}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
TORCHRUN_BIN="${TORCHRUN_BIN:-$(dirname "$PYTHON_BIN")/torchrun}"
ENTRYPOINT="${ENTRYPOINT:-$PROJECT_ROOT/train.py}"
NPROC_PER_NODE="${NPROC_PER_NODE:-1}"
RDZV_ID="${RDZV_ID:-${PBS_JOBID:-manual}}"
DIST_BACKEND="${DIST_BACKEND:-nccl}"

if [[ -z "${MASTER_PORT:-}" ]]; then
  job_id="${PBS_JOBID:-}"
  job_number="${job_id%%.*}"
  if [[ "$job_number" =~ ^[0-9]+$ ]]; then
    MASTER_PORT=$((20000 + job_number % 20000))
  else
    MASTER_PORT=29500
  fi
fi

: "${PBS_NODEFILE:?PBS_NODEFILE is not set}"
cd "$PROJECT_ROOT"

mapfile -t HOSTS < <(awk '!seen[$0]++' "$PBS_NODEFILE")
NNODES="${#HOSTS[@]}"
if (( NNODES == 0 )); then
  echo "PBS_NODEFILE is empty" >&2
  exit 1
fi
WORLD_SIZE=$((NNODES * NPROC_PER_NODE))
MASTER_ADDR="${MASTER_ADDR:-${HOSTS[0]}}"
export PROJECT_ROOT TORCHRUN_BIN ENTRYPOINT NPROC_PER_NODE MASTER_ADDR MASTER_PORT RDZV_ID NNODES DIST_BACKEND

timestamp=$(date "+%Y%m%d%H%M%S")
LOG_ROOT="${LOG_ROOT:-$PROJECT_ROOT/logs/qsub_${timestamp}}"
mkdir -p "$LOG_ROOT"

MPI_ENV_ARGS=(
  "PROJECT_ROOT=$PROJECT_ROOT"
  "TORCHRUN_BIN=$TORCHRUN_BIN"
  "ENTRYPOINT=$ENTRYPOINT"
  "MASTER_ADDR=$MASTER_ADDR"
  "MASTER_PORT=$MASTER_PORT"
  "RDZV_ID=$RDZV_ID"
  "NNODES=$NNODES"
  "NPROC_PER_NODE=$NPROC_PER_NODE"
  "DIST_BACKEND=$DIST_BACKEND"
)

echo "NNODES=$NNODES NPROC_PER_NODE=$NPROC_PER_NODE WORLD_SIZE=$WORLD_SIZE"
echo "MASTER_ADDR=$MASTER_ADDR MASTER_PORT=$MASTER_PORT"
echo "RDZV_ID=$RDZV_ID"
echo "PROJECT_ROOT=$PROJECT_ROOT"
echo "LOG_ROOT=$LOG_ROOT"

TRAIN_ARGS=(
  --config "<config.yaml>"
  --experiment-name "<experiment_name>"
)

mpirun \
  --mca mpi_abort_print_stack 1 \
  --report-bindings \
  --map-by ppr:1:node \
  --bind-to none \
  -np "$NNODES" \
  /usr/bin/env "${MPI_ENV_ARGS[@]}" \
  bash -lc '
    set -euo pipefail
    node_rank="${OMPI_COMM_WORLD_RANK:?OMPI_COMM_WORLD_RANK is not set}"
    echo "torchrun supervisor host=$(hostname) node_rank=${node_rank}/${NNODES:?NNODES is not set}"
    exec "$TORCHRUN_BIN" \
      --rdzv-backend=c10d \
      --rdzv-endpoint="${MASTER_ADDR}:${MASTER_PORT}" \
      --rdzv-id="$RDZV_ID" \
      --nnodes="$NNODES" \
      --nproc-per-node="$NPROC_PER_NODE" \
      --node-rank="$node_rank" \
      --max-restarts=0 \
      "$ENTRYPOINT" "$@"
  ' bash "${TRAIN_ARGS[@]}" 2>&1 | tee "$LOG_ROOT/train.log"
```

## Python Entrypoint Expectations

```python
import datetime
import os
import torch
import torch.distributed as dist

local_rank = int(os.environ.get("LOCAL_RANK", "0"))
backend = os.environ.get("DIST_BACKEND", "nccl")
if torch.cuda.is_available():
    torch.cuda.set_device(local_rank)

dist.init_process_group(
    backend=backend,
    timeout=datetime.timedelta(seconds=300),
)
print(
    f"world={dist.get_world_size()} rank={dist.get_rank()} "
    f"local_rank={local_rank} host={os.uname().nodename}",
    flush=True,
)

dist.destroy_process_group()
```

## Notes

- Discover exact modules with `module avail`, `show_module`, and `module help`
  as described in [module.md](module.md). Replace or remove every module
  placeholder before submission. Keep `REQUIRED_MODULES=()` only when the job
  intentionally uses the compute-node defaults, and still log `module list`.
- Keep `#PBS -l select=<num_nodes>:mpiprocs=1` for the `mpirun -> torchrun` pattern. `torchrun`, not MPI, creates local worker processes.
- Set `NPROC_PER_NODE` to the number of local GPU workers each `torchrun` should spawn.
- Keep the MPI supervisor unbound unless the project defines an explicit CPU
  affinity plan; child workers can inherit an overly narrow supervisor
  affinity.
- Give every concurrent worker group a distinct `RDZV_ID` and non-conflicting
  rendezvous port. `$PBS_JOBID` is a suitable default rendezvous id and input
  for a job-specific port; override `MASTER_PORT` if site policy requires it.
- Re-check rendezvous flags against the current
  [PyTorch torchrun documentation](https://docs.pytorch.org/docs/stable/elastic/run.html).
