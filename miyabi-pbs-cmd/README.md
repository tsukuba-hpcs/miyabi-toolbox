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
└── references/
    ├── accelerate-pbs.md
    ├── filesystem-network.md
    ├── module.md
    ├── mpi.md
    ├── python-env.md
    ├── qstat.md
    ├── qsub.md
    ├── torchrun-pbs.md
    └── vllm-miyabi.md
```

`SKILL.md` contains routing and safety invariants. Each reference contains
agent-relevant commands and Miyabi-specific behavior for one operational area.

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
