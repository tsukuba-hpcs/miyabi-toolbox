# Hugging Face Accelerate PBS Pattern

Use this for code that creates `Accelerator()` directly and lets Accelerate read rank information from environment variables. The local Miyabi pattern is `mpirun -np WORLD_SIZE` and, for each MPI process, export `RANK`, `WORLD_SIZE`, `LOCAL_RANK`, and `LOCAL_WORLD_SIZE` from Open MPI variables before running the Python module.

Before writing the PBS directive, derive the project group with `groups` and fill the literal group value into `#PBS -W group_list=...`; PBS directives do not expand shell variables.

```bash
#!/bin/bash
#PBS -q regular-g
#PBS -W group_list=<group_id_from_groups>
#PBS -l select=<num_nodes>:mpiprocs=<processes_per_node>
#PBS -l walltime=01:00:00
#PBS -j oe
#PBS -m ae

set -eEuo pipefail
trap 'echo "[ERROR] Failed at line $LINENO" >&2' ERR

# module load cuda/<version>  # uncomment and set the version if required

PROJECT_ROOT="${PROJECT_ROOT:-${PBS_O_WORKDIR:-$PWD}}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
PYTHON_MODULE="${PYTHON_MODULE:-src.cli}"
ACCELERATE_CONFIG="${ACCELERATE_CONFIG:-accelerate_config/accelerate_config.yaml}"
MASTER_PORT="${MASTER_PORT:-29500}"

: "${PBS_NODEFILE:?PBS_NODEFILE is not set}"
cd "$PROJECT_ROOT"

NNODES=$(sort -u "$PBS_NODEFILE" | wc -l)
WORLD_SIZE=$(wc -l < "$PBS_NODEFILE")
NPROC_PER_NODE="${NPROC_PER_NODE:-$((WORLD_SIZE / NNODES))}"
MASTER_ADDR="${MASTER_ADDR:-$(head -n 1 "$PBS_NODEFILE")}"

timestamp=$(date "+%Y%m%d%H%M%S")
LOG_ROOT="${LOG_ROOT:-$PROJECT_ROOT/logs/qsub_${timestamp}}"
mkdir -p "$LOG_ROOT"

MPI_ENV_ARGS=(
  "MASTER_ADDR=$MASTER_ADDR"
  "MASTER_PORT=$MASTER_PORT"
  "ACCELERATE_CONFIG_FILE=$ACCELERATE_CONFIG"
  "PROJECT_ROOT=$PROJECT_ROOT"
  "PYTHON_BIN=$PYTHON_BIN"
  "PYTHON_MODULE=$PYTHON_MODULE"
)

echo "NNODES=$NNODES NPROC_PER_NODE=$NPROC_PER_NODE WORLD_SIZE=$WORLD_SIZE"
echo "MASTER_ADDR=$MASTER_ADDR MASTER_PORT=$MASTER_PORT"
echo "ACCELERATE_CONFIG_FILE=$ACCELERATE_CONFIG"

TRAIN_ARGS=(
  train
  --config "<config_or_args>"
)

mpirun \
  --mca mpi_abort_print_stack 1 \
  --report-bindings \
  --bind-to core \
  -np "$WORLD_SIZE" \
  /usr/bin/env "${MPI_ENV_ARGS[@]}" \
  bash -lc '
    set -euo pipefail
    export RANK="${OMPI_COMM_WORLD_RANK:?OMPI_COMM_WORLD_RANK is not set}"
    export WORLD_SIZE="${OMPI_COMM_WORLD_SIZE:?OMPI_COMM_WORLD_SIZE is not set}"
    export LOCAL_RANK="${OMPI_COMM_WORLD_LOCAL_RANK:?OMPI_COMM_WORLD_LOCAL_RANK is not set}"
    export LOCAL_WORLD_SIZE="${OMPI_COMM_WORLD_LOCAL_SIZE:?OMPI_COMM_WORLD_LOCAL_SIZE is not set}"
    echo "accelerate rank=${RANK}/${WORLD_SIZE} local_rank=${LOCAL_RANK}/${LOCAL_WORLD_SIZE} host=$(hostname)"
    exec "$PYTHON_BIN" -m "$PYTHON_MODULE" "$@"
  ' bash "${TRAIN_ARGS[@]}" 2>&1 | tee "$LOG_ROOT/train.log"
```

Python entrypoint expectations:

```python
from accelerate import Accelerator

def train(*args, **kwargs):
    accelerator = Accelerator()
    world_size = accelerator.num_processes

    if accelerator.is_main_process:
        print(f"world_size={world_size}")

    # Build model, optimizer, dataloaders, then use accelerator.prepare(...)
    # or a Trainer/SFTTrainer stack that integrates with Accelerate.

    accelerator.wait_for_everyone()
    accelerator.end_training()
```

Notes:

- This template is for direct `Accelerator()` use under `mpirun`.
- If the project standardizes on `accelerate launch`, use official launcher flags (`--config_file`, `--num_processes`, `--num_machines`, `--machine_rank`, `--main_process_ip`, `--main_process_port`) and still run only from compute nodes or PBS jobs.
- Keep the Accelerate config consistent with the job shape: `distributed_type`, `mixed_precision`, `num_machines`, and `num_processes` should match the allocation or be intentionally overridden.
- Use `accelerate test --config_file <config>` only on an interactive/compute node, never on a login node.
