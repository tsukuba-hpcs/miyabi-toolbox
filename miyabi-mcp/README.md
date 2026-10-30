# miyabi-mcp

An MCP server and CLI that lets coding agents (Claude Code, Codex, pi, any MCP client) use Miyabi's PBS:
query queues and jobs, submit and cancel jobs within a project policy, keep a per-project job ledger,
and wake the agent when jobs end. Design and rationale: [DESIGN.md](DESIGN.md) (Chinese).

The server always runs on a Miyabi login node, next to PBS. An agent on a login node starts it directly;
an agent elsewhere starts the same server over `ssh`. It never runs workloads: those go to PBS jobs.

## Install (on a Miyabi login node)

```bash
cd /work/<group>/<user>/miyabi-toolbox/miyabi-mcp
uv sync --frozen --python /usr/bin/python3.11     # .venv on /work; no interpreter download, nothing in $HOME
bin/miyabi-mcp --project-dir /work/<group>/<user>/<project> context --pretty
```

`bin/miyabi-mcp` runs the program from `.venv` (Python ≥ 3.11, the official MCP SDK). A venv is tied to
the CPU architecture: on Miyabi-C login nodes build a second one with
`UV_PROJECT_ENVIRONMENT=.venv-$(uname -m) uv sync --frozen --python /usr/bin/python3.11`.

## Register with an agent

Register per project; `--project <name>` means `/work/<group>/<user>/<name>`, `--project-dir` takes any path.

```bash
# Claude Code on a workstation (uses your ssh ControlMaster for miyabi-g; open it with `ssh -fN miyabi-g`)
claude mcp add --scope local miyabi-pbs -- \
  ssh -T -o BatchMode=yes miyabi-g /work/<group>/<user>/miyabi-toolbox/miyabi-mcp/bin/miyabi-mcp serve --project <project>

# Claude Code on a Miyabi login node
claude mcp add --scope local miyabi-pbs -- \
  /work/<group>/<user>/miyabi-toolbox/miyabi-mcp/bin/miyabi-mcp serve --project-dir /work/<group>/<user>/<project>

# pi (project config .pi/mcp.json)
pi mcp add -l miyabi-pbs -- ssh -T -o BatchMode=yes miyabi-g <path>/bin/miyabi-mcp serve --project <project>
```

```toml
# Codex (~/.codex/config.toml); pass `project` in tool calls for other projects
[mcp_servers.miyabi-pbs]
command = "ssh"
args = ["-T", "-o", "BatchMode=yes", "miyabi-g", "<path>/bin/miyabi-mcp", "serve", "--project", "<project>"]
tool_timeout_sec = 120
```

Agents that only have a shell (for example through pi-env-mcp's `bash`) use the CLI:
`bin/miyabi-mcp --project-dir <dir> call <tool> '<json arguments>'` prints the same JSON as the tool.

## Where things are written

Nothing goes to `$HOME`. Everything a project needs to keep is under `<project>/.miyabi-mcp/` (mode 700,
self-ignored by git): job records and script snapshots (`submissions/<submit_id>/`, linked from
`jobs/<job number>`), append-only events (`events/`), default PBS output (`logs/`), watches, plans and the
project `policy.json`. User-level files are in `/work/<group>/<user>/.miyabi-mcp/`: `config.json`, the
default `policy.json`, the shared `qstat` snapshot, the job index across projects and the submit lock.

## Policy

`<project>/.miyabi-mcp/policy.json` limits what agents can submit in that project (unknown keys or wrong
types stop all writes). Create the directory with `bin/miyabi-mcp --project-dir <dir> init --policy file.json`.

```json
{
  "queues": ["debug-mig"],
  "per_job": {"max_mig_instances": 1, "max_walltime": "00:10:00"},
  "budget_windows": [{"window": "24h", "max_requested_mig_instance_hours": 1.0, "max_jobs": 6}],
  "max_active_jobs": 2,
  "require_plan": true
}
```

Keys: `groups`, `queues`, `per_job` (`max_nodes`, `max_mig_instances`, `max_walltime`, `max_node_hours`,
`max_mig_instance_hours`), `max_active_jobs`, `label_limits` (`{"agent": {"max_active_jobs": 6}}`),
`budget_windows` (`window`, `max_jobs`, `max_requested_node_hours`, `max_requested_mig_instance_hours`),
`reserve_accept_slots` (default 4), `require_plan` (default true), `auto_commit_below_unit_hours`,
`maintenance` (`refuse_if_crossing` or `warn`), `cancel_scope` (`project` or `any`). The user-level file may
also set `user: {max_active_jobs, budget_windows}` across all projects. The policy is enforced by the
server; an agent with shell access to the project could still edit the file, so keep it out of the agent's
instructions to change.

## Tools

| Tool | Does |
| --- | --- |
| `pbs_context` | plane, host, project and state directory, PBS projects, scheduled stop, effective policy |
| `pbs_queues` | queue shapes and status (REMAIN_ELAPSE), project limits, occupancy, token budget |
| `job_list`, `job_get` | jobs from one shared `qstat -ll` snapshot; details from `qstat [-H] -f`; output tails |
| `job_submit` | dry run → checked plan + `plan_digest`; same arguments + `dry_run=false` + digest → qsub |
| `job_cancel` | dry run → targets + digest; commit with a reason → qdel |
| `job_wait` | bounded wait (≤ 45 s) for short jobs |
| `watch_create`, `watch_list`, `watch_cancel`, `watch_ack` | wake-ups when jobs end or start, a queue reopens, or a timeout passes |
| `ledger_query` | records, summary, events, or smooth-research observations (`format=smooth-research`) |
| `job_annotate` | labels, notes, application-level verdicts; `adopt` jobs submitted elsewhere |

### Waking up after long jobs

`watch_create` returns `command_local` and `command_via_ssh`. In Claude Code, run the one that fits with the
Bash tool's `run_in_background: true` and end the turn: the command exits when the condition holds and its
output is the wake-up message (job states, exit statuses, usage, log paths, your note, the ack token).
Exit codes: 0 delivered, 3 cancelled, 75 the watch could not continue (it still wakes you, with the reason).
Codex (`codex_queue`, `codex_goal`), `herdr`, `tmux`, `command` and `file` deliveries start a detached
waiter on the login node instead.

## Tests

```bash
uv sync && .venv/bin/python -m unittest discover -s tests -v     # fake PBS; submits nothing
```

The fake PBS replays anonymized output captured on miyabi-g1 (2026-10-30).

## Status (v0.1)

Verified offline (35 tests) and against live read-only queries on Miyabi-G. Not yet verified live:
submission, cancellation and wake-ups on real jobs; Exit_status values for walltime kills and qdel of a
running job; codex_goal and codex_queue with Codex 0.163; herdr and tmux delivery; Miyabi-C. The MIG
`hardware` string is taken from `mig_hardware` in the user `config.json` and stays null until set.
