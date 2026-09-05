# Miyabi Development

`miyabi-development` provides agent-facing JSON helpers, PBS templates and
operational guidance for Miyabi. Start with the helpers for host/allocation
context and job queries; use native commands for uncovered queries or diagnostics.
It also covers Environment Modules, Python environments, storage and distributed
launch patterns.

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

`SKILL.md` contains routing and safety invariants. References describe the
interfaces and Miyabi-specific behavior for each operational area.
`scripts/qstat_json.py` turns current-job and history tables into JSON using
only Python's standard library. See [references/qstat.md](references/qstat.md)
for invocation, fields, error handling, and native fallback coverage.
`scripts/context.py` uses host Python 3.9 to report role, architecture and PBS
evidence, with an optional compute guard. Compatible project environments may
serve lightweight login tasks; tests and application runtime use the target
allocation, respecting stricter project rules. The PBS assets contain the
maintained templates; their validation limits are documented in
[references/templates.md](references/templates.md).

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

## Validation

Run the small standard-library offline regressions from this skill directory:

```bash
/usr/bin/python3 -m unittest discover -s tests -v
```

The job helper was checked against live current-job, completed-job and no-match
queries on `miyabi-g1`. Its history fixture preserves observed table spacing with
anonymized identifiers, names and projects; queued/array/MIG and malformed-output
cases are synthetic regressions. Column definitions were checked against the
local `/usr/local/share/man/man1/qstat.1`. These checks do not validate every
scheduler state or a distributed workload. Context and template validation
limits are documented in [references/templates.md](references/templates.md#validation-scope).
