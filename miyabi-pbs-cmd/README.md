# Miyabi Development

`miyabi-development` is an agent-facing operating manual for the Miyabi
supercomputer. It covers host classification, login-node safety, Environment
Modules, PBS job submission and inspection, storage, and cluster-specific GPU
or distributed launch patterns.

The skill determines where a command may run and how to use the cluster safely.
Project development and acceptance policy remains with the project and user.

## Layout

```text
miyabi-development/
├── agents/openai.yaml
├── SKILL.md
├── scripts/
│   ├── context.py
│   └── qstat_json.py
├── assets/pbs/
│   ├── single-node.pbs
│   ├── torchrun.pbs
│   └── mpi-workers.pbs
├── tests/
│   ├── fixtures/qstat_history.txt
│   ├── test_context.py
│   ├── test_pbs_templates.py
│   └── test_qstat_json.py
└── references/
    ├── accelerate-pbs.md
    ├── filesystem-network.md
    ├── failure-lessons.md
    ├── module.md
    ├── mpi.md
    ├── python-env.md
    ├── qstat.md
    ├── qsub.md
    ├── torchrun-pbs.md
    ├── templates.md
    ├── validation.md
    └── vllm-miyabi.md
```

`SKILL.md` contains routing and safety invariants. Each reference contains
agent-relevant commands and Miyabi-specific behavior for one operational area.
`scripts/qstat_json.py` turns current-job and history tables into JSON using
only Python's standard library. See [references/qstat.md](references/qstat.md)
for invocation, fields, error handling, and offline validation.
`scripts/context.py` uses host Python 3.9 to report role, architecture and PBS
evidence, with an optional compute guard. Compatible project environments may
serve lightweight login tasks; tests and application runtime use the target
allocation, respecting stricter project rules. The PBS assets contain the
maintained templates; their validation limits are documented in
[references/templates.md](references/templates.md). Incorporated incident
provenance is indexed in [references/failure-lessons.md](references/failure-lessons.md).

## Usage

Examples:

```text
Use $miyabi-development to inspect why this PBS job is queued.
```

```text
Use $miyabi-development to prepare and submit this qsub script.
```

```text
Use $miyabi-development to run this workload in an interactive GPU allocation.
```

Live Miyabi output and current site documentation take precedence over copied
queue limits, module versions, and historical examples.
