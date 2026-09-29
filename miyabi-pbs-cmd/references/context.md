# Host And Allocation Context

[context.py](../scripts/context.py) uses host Python 3.9+ and the standard
library. It reads local host/PBS evidence without importing project code or
querying the scheduler.

```bash
SKILL_ROOT="/absolute/path/to/miyabi-pbs-cmd"
/usr/bin/python3 -I "$SKILL_ROOT/scripts/context.py" --target-system Miyabi-G
/usr/bin/python3 -I "$SKILL_ROOT/scripts/context.py" \
  --target-system Miyabi-G --require-compute
```

Use the actual workload target: `Miyabi-G` (`aarch64`) or `Miyabi-C` (`x86_64`).
Both commands emit JSON containing host role, observed/target architecture,
PBS job ID, nodefile hosts, `allocation_confirmed`, `runtime_execution_allowed`
and refusal `reasons`. `--pretty` adds indentation.

- Inspection exits 0 with `ok:true` even when runtime execution is disallowed.
- `--require-compute` requires `--target-system`. It exits 1 with `ok:false`
  unless a recognized compute hostname, matching architecture, valid job ID,
  nodefile membership and workload target establish local allocation evidence.
- Invalid arguments exit 2 with argparse text. Inherited PBS variables alone
  cannot make a login or unknown host pass the guard.

The guard cannot establish that the allocation is still running. Check the job
ID with [job queries](qstat.md) when entering or reusing an allocation.
Unfamiliar host families need site evidence before extending the guard.

`-I` isolates Python from project paths and user site packages; it does not
establish workload cost or project-interpreter compatibility. Follow the
[execution boundaries](../SKILL.md#execution-boundaries).
