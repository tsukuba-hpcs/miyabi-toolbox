# miyabi-toolbox

Agent skills for working on the Miyabi supercomputer from Claude Code, Codex
and pi: its PBS scheduler, and the ML experiment platforms built on it. Each
skill is Markdown guidance plus standard-library Python and Bash; nothing needs
installing beyond linking the skill directories where the agent looks for
skills.

[miyabi-mcp](miyabi-mcp/README.md) is the standard way for agents to operate PBS:
an MCP server and CLI that queries queues and jobs, checks every submission
against the project's policy and budget, cancels, keeps a per-project job ledger
and notifies the agent when groups of jobs end. It is installed and registered
separately (see its README). The skills cover what it does not: job scripts and
experiment platforms.

| Skill | Use it to | Ships |
| --- | --- | --- |
| [miyabi-pbs-cmd](miyabi-pbs-cmd/SKILL.md) | **Deprecated (2026-11-05): use miyabi-mcp.** Kept as background on the `qstat`/`qsub` dialect, and for `context.py`, the guard the templates run inside jobs | `context.py` (host and allocation guard), `qstat_json.py` (jobs and history as JSON), qstat/qsub references |
| [miyabi-pbs-shell-template](miyabi-pbs-shell-template/SKILL.md) | Write or fix `.pbs` scripts: single node, `torchrun`, Open MPI workers; modules, Python environments, storage, Accelerate, vLLM | Three templates in `assets/pbs/` and workload references |
| [miyabi-pbs-goal-wakeup](miyabi-pbs-goal-wakeup/SKILL.md) | **Deprecated (2026-11-05): use miyabi-mcp notifications.** The earlier per-job poller that woke a Codex Goal or a Claude Code session | `job_wakeup.py` |
| [miyabi-ml-experiment-platform](miyabi-ml-experiment-platform/SKILL.md) | Design, build or audit the platform behind train → evaluate → statistics experiments (late-binding pilot jobs, walltime from measured smoke runs, cost gates, in-place evaluation, retention, streaming statistics) and plan experiments on it | `pbs_time_report.py` (queue wait, run time, used walltime and GPU use from `qstat -H -f`), a pilot job template, references with the measured costs behind each rule |

## How the skills fit together

1. **Write the job script** from a template ([shell-template](miyabi-pbs-shell-template/SKILL.md)).
   The templates run the context guard (`miyabi-pbs-cmd/scripts/context.py`)
   first, so login nodes and mismatched architectures are refused before any
   project code runs.
2. **Choose resources and submit** with miyabi-mcp: `pbs_queues`, then
   `job_submit` (the dry run returns the checked plan; commit with its
   `plan_digest`). Submission and cancellation change scheduler state, so they
   follow the scope the user authorized.
3. **Wait**: `job_wait` for jobs of a few minutes. For longer ones submit with
   `notify`, run the command the result names and end the turn instead of
   polling in a loop.
4. **Interpret the result** with `job_get`: `FINISH` alone never establishes
   success.

For a whole experiment rather than one job,
[platform](miyabi-ml-experiment-platform/SKILL.md) designs the system that
submits and tracks the jobs, and relies on miyabi-mcp and the templates for the
mechanics.

The skills reference each other by name or relative path, so install them side
by side.

## Requirements

- A Miyabi shell with `qstat` on `PATH` for real job queries. The offline tests
  need neither PBS nor a compute node.
- Host Python 3.9+ (`/usr/bin/python3`); the scripts use only the standard library.
- miyabi-mcp has its own requirements (a Python 3.11 venv on /work); see its
  [install notes](miyabi-mcp/README.md#install).

## Install

Clone the repository anywhere and link each skill still in use into each agent's
skills directory (`ln -sfn` also repoints existing links). The deprecated
`miyabi-pbs-cmd` and `miyabi-pbs-goal-wakeup` are not linked; the templates reach
`miyabi-pbs-cmd/scripts/context.py` through the checkout path.

```bash
TOOLBOX=/absolute/path/to/miyabi-toolbox
mkdir -p ~/.claude/skills ~/.codex/skills ~/.pi/agent/skills
for skill in miyabi-pbs-shell-template miyabi-ml-experiment-platform; do
  ln -sfn "$TOOLBOX/$skill" ~/.claude/skills/"$skill"     # Claude Code
  ln -sfn "$TOOLBOX/$skill" ~/.codex/skills/"$skill"      # Codex
  ln -sfn "$TOOLBOX/$skill" ~/.pi/agent/skills/"$skill"   # pi
done
```

Remove links to the deprecated skills left by an earlier install, for example
`rm ~/.codex/skills/miyabi-pbs-cmd ~/.codex/skills/miyabi-pbs-goal-wakeup`.

In the maintainer's setup the repository is `/work/xg24i002/x10041/miyabi-toolbox`;
self-made skills are kept here and linked into the agents.

## Use

Agents load a skill from its description. To name one explicitly, use
`/miyabi-pbs-shell-template` in Claude Code, `$miyabi-pbs-shell-template` in Codex
or `/skill:miyabi-pbs-shell-template` in pi. The operating plane, queues and jobs
come from miyabi-mcp (`pbs_context`, `pbs_queues`, `job_list`, `job_get`), as MCP
tools or through its CLI:

```bash
"$TOOLBOX"/miyabi-mcp/bin/miyabi-mcp --project-dir <project> call job_list '{}'
```

### Long jobs

Submit with a miyabi-mcp notification and end the turn: a daemon on the agent's
machine delivers the message when the jobs end (Claude Code runs the returned
`listen_command` as a background command). See
[Notifications](miyabi-mcp/README.md#notifications). The goal-wakeup pollers are
deprecated; resuming a paused Codex Goal has no miyabi-mcp equivalent yet.

## Tests

Each skill has an offline suite that submits no PBS jobs. From a skill's
directory:

```bash
/usr/bin/python3 -I -B -m unittest discover -s tests -v
```

## Maintenance notes

- `miyabi-pbs-goal-wakeup/scripts/qstat_json.py` is a byte-identical copy of
  `miyabi-pbs-cmd/scripts/qstat_json.py`. After changing either, update both and
  check with `cmp` (see [validation](miyabi-pbs-goal-wakeup/references/validation.md)).
- The templates need `MIYABI_PBS_CMD_ROOT` to point at the `miyabi-pbs-cmd`
  directory of this checkout, not at an agent's skills link: the skill is
  deprecated, but its `context.py` is still the job-side guard; see
  [templates](miyabi-pbs-shell-template/references/templates.md).
- Do not create a `.git` inside a skill directory: this repository would silently
  stop tracking that skill.

## Scope and validation

These skills target Miyabi's own table and history formats; they are not a
general OpenPBS/Torque adapter. The job helpers were checked against live `qstat`
output on a Miyabi-G login node. The templates' tests simulate the launchers; a
login-node probe validated only their refusal, and no live Miyabi-C allocation or
distributed run has validated them
([template validation scope](miyabi-pbs-shell-template/references/templates.md#validation-scope)).
The wake-up flows have recorded live results only for the cases listed in
[validation](miyabi-pbs-goal-wakeup/references/validation.md). The platform
skill condenses experiments run on Miyabi-G in October 2026: its numbers are
dated, its status labels say what ran on Miyabi, and `pbs_time_report.py` was
checked against live `qstat -H -f` output.
