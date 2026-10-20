# miyabi-toolbox

Agent skills for working on the Miyabi supercomputer from Claude Code, Codex
and pi: its PBS scheduler, and the ML experiment platforms built on it. Each
skill is Markdown guidance plus standard-library Python and Bash; nothing needs
installing beyond linking the skill directories where the agent looks for
skills.

| Skill | Use it to | Ships |
| --- | --- | --- |
| [miyabi-pbs-cmd](miyabi-pbs-cmd/SKILL.md) | Query, submit and control jobs with Miyabi's `qstat`/`qsub`/`qdel`/`tracejob` dialect, and decide whether work may run on a login node or needs a compute allocation | `context.py` (host and allocation guard), `qstat_json.py` (jobs and history as JSON), qstat/qsub references |
| [miyabi-pbs-shell-template](miyabi-pbs-shell-template/SKILL.md) | Write or fix `.pbs` scripts: single node, `torchrun`, Open MPI workers; modules, Python environments, storage, Accelerate, vLLM | Three templates in `assets/pbs/` and workload references |
| [miyabi-pbs-goal-wakeup](miyabi-pbs-goal-wakeup/SKILL.md) | Let an agent sleep through a job whose queue wait plus runtime exceeds an hour and be woken when it ends | `job_wakeup.py`, a PBS poller that wakes a Codex Goal or a Claude Code session |
| [miyabi-ml-experiment-platform](miyabi-ml-experiment-platform/SKILL.md) | Design, build or audit the platform behind train → evaluate → statistics experiments (late-binding pilot jobs, walltime from measured smoke runs, cost gates, in-place evaluation, retention, streaming statistics) and plan experiments on it | `pbs_time_report.py` (queue wait, run time, used walltime and GPU use from `qstat -H -f`), a pilot job template, references with the measured costs behind each rule |

## How the skills fit together

1. **Write the job script** from a template ([shell-template](miyabi-pbs-shell-template/SKILL.md)).
   The templates call cmd's context guard first, so login nodes and mismatched
   architectures are refused before any project code runs.
2. **Choose resources and submit** ([cmd](miyabi-pbs-cmd/references/qsub.md)).
   Submission and cancellation change scheduler state, so they follow the
   scope the user authorized.
3. **Wait** ([goal-wakeup](miyabi-pbs-goal-wakeup/SKILL.md)). For long jobs the
   agent registers a watcher and ends its turn instead of polling in a loop.
4. **Interpret the result** with cmd's [job queries](miyabi-pbs-cmd/references/qstat.md):
   `FINISH` alone never establishes success.

For a whole experiment rather than one job,
[platform](miyabi-ml-experiment-platform/SKILL.md) designs the system that
submits and tracks the jobs, and uses the three skills above for the mechanics.

The skills reference each other by name or relative path, so install them side
by side.

## Requirements

- A Miyabi shell with `qstat` on `PATH` for real job queries. The offline tests
  need neither PBS nor a compute node.
- Host Python 3.9+ (`/usr/bin/python3`); the scripts use only the standard library.
- For goal-wakeup, either a Codex thread with an active Goal and a running
  app-server daemon (tested with Codex 0.158.0), or a Claude Code session whose
  Bash tool supports `run_in_background` (tested with Claude Code 2.1.284).

## Install

Clone the repository anywhere and link every skill directory into each agent's
skills directory (`ln -sfn` also repoints existing links):

```bash
TOOLBOX=/absolute/path/to/miyabi-toolbox
mkdir -p ~/.claude/skills ~/.codex/skills ~/.pi/agent/skills
for dir in "$TOOLBOX"/*/; do
  [ -f "$dir/SKILL.md" ] || continue
  skill=$(basename "$dir")
  ln -sfn "$TOOLBOX/$skill" ~/.claude/skills/"$skill"     # Claude Code
  ln -sfn "$TOOLBOX/$skill" ~/.codex/skills/"$skill"      # Codex
  ln -sfn "$TOOLBOX/$skill" ~/.pi/agent/skills/"$skill"   # pi
done
```

In the maintainer's setup the repository is `/work/xg24i002/x10041/miyabi-toolbox`;
self-made skills are kept here and linked into all three agents. pi loads every
skill, but goal-wakeup's wake-up flows exist only for Codex and Claude Code.

## Use

Agents load a skill from its description. To name one explicitly, use
`/miyabi-pbs-cmd` in Claude Code, `$miyabi-pbs-cmd` in Codex or
`/skill:miyabi-pbs-cmd` in pi. The helpers also
run by hand; set `SKILL_ROOT` to the installed skill directory:

```bash
SKILL_ROOT=~/.claude/skills/miyabi-pbs-cmd
# Is this shell a compute allocation for Miyabi-G? (inspection always exits 0)
/usr/bin/python3 -I "$SKILL_ROOT/scripts/context.py" --target-system Miyabi-G
# Status counts for the last 10 finished jobs
/usr/bin/python3 -I "$SKILL_ROOT/scripts/qstat_json.py" -H --hday 3 --hnum 10 --summary
```

### Long jobs

When estimated queue wait plus runtime exceeds one hour, goal-wakeup arms a
poller (every 5 minutes by default) and the agent sleeps until the job ends:

- **Claude Code**: the poller runs as a background command; its exit, carrying
  the wake-up message, resumes the idle session.
- **Codex**: a detached poller restores the paused Goal through the running
  app-server daemon.

The Claude poller is tied to its session and stops with it; the Codex poller runs
detached. Neither has a supervisor, so a stalled event is checked with `status`
and restarted with `run`; see [usage](miyabi-pbs-goal-wakeup/references/usage.md)
for options, recovery and limits.

## Tests

Each skill has an offline suite that submits no PBS jobs. The cmd skill's
[execution boundaries](miyabi-pbs-cmd/SKILL.md#execution-boundaries) allow its
stdlib offline tests on a login node when project rules permit. From a skill's
directory:

```bash
/usr/bin/python3 -I -B -m unittest discover -s tests -v
```

## Maintenance notes

- `miyabi-pbs-goal-wakeup/scripts/qstat_json.py` is a byte-identical copy of
  `miyabi-pbs-cmd/scripts/qstat_json.py`. After changing either, update both and
  check with `cmp` (see [validation](miyabi-pbs-goal-wakeup/references/validation.md)).
- The templates need `MIYABI_PBS_CMD_ROOT` to point at the installed
  `miyabi-pbs-cmd` directory; see [templates](miyabi-pbs-shell-template/references/templates.md).
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
