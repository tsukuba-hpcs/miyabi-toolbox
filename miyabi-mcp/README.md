# miyabi-mcp

An MCP server and CLI that lets coding agents (Claude Code, Codex, pi, any MCP client) use Miyabi's PBS:
query queues and jobs, submit and cancel jobs within a project policy, keep a per-project job ledger,
and get notified when jobs (or groups of jobs) end. Design and rationale: [DESIGN.md](DESIGN.md) (Chinese).

The server always runs on a Miyabi login node, next to PBS. An agent on a login node starts it directly;
an agent elsewhere starts the same server over `ssh`. It never runs workloads: those go to PBS jobs.
Notifications are delivered by a small daemon on the agent's own machine, which the agent starts.

## Install

On a Miyabi login node (the server):

```bash
cd /work/<group>/<user>/miyabi-toolbox/miyabi-mcp
uv sync --frozen --python /usr/bin/python3.11     # .venv on /work; no interpreter download, nothing in $HOME
bin/miyabi-mcp --project-dir /work/<group>/<user>/<project> context --pretty
```

On the agent's machine, if it is not a Miyabi node (for the notification daemon): clone this repository and
run `uv sync` in `miyabi-mcp/`. `bin/miyabi-mcp` runs from `.venv` (Python ≥ 3.11). A venv is tied to the
CPU architecture: on Miyabi-C login nodes build another one with
`UV_PROJECT_ENVIRONMENT=.venv-$(uname -m) uv sync --frozen --python /usr/bin/python3.11`.

## Register with an agent

Register per project; `--project <name>` means `/work/<group>/<user>/<name>`, `--project-dir` takes any path.
For an agent on another machine, also tell the server that machine's name (`--notifier`), where
`bin/miyabi-mcp` lives there (`--local-launcher`) and the ssh alias of the login node (`--remote-alias`), so the
commands it returns run as they are.

```bash
# Claude Code on a workstation (uses your ssh ControlMaster for miyabi-g; open it with `ssh -fN miyabi-g`)
claude mcp add --scope local miyabi-pbs -- \
  ssh -T -o BatchMode=yes miyabi-g /work/<group>/<user>/miyabi-toolbox/miyabi-mcp/bin/miyabi-mcp serve \
  --project <project> --notifier $(hostname -s) --local-launcher <clone>/miyabi-mcp/bin/miyabi-mcp --remote-alias miyabi-g

# Claude Code on a Miyabi login node
claude mcp add --scope local miyabi-pbs -- \
  /work/<group>/<user>/miyabi-toolbox/miyabi-mcp/bin/miyabi-mcp serve --project-dir /work/<group>/<user>/<project>
```

```toml
# Codex (~/.codex/config.toml); pass `project` in tool calls for other projects
[mcp_servers.miyabi-pbs]
command = "ssh"
args = ["-T", "-o", "BatchMode=yes", "miyabi-g", "<path>/bin/miyabi-mcp", "serve", "--project", "<project>",
        "--notifier", "<this machine>", "--local-launcher", "<clone>/miyabi-mcp/bin/miyabi-mcp"]
tool_timeout_sec = 120
```

pi: `pi mcp add -l miyabi-pbs -- ssh -T -o BatchMode=yes miyabi-g <path>/bin/miyabi-mcp serve --project <project> …`.
Agents that only have a shell (for example through pi-env-mcp's `bash`) use the CLI:
`bin/miyabi-mcp --project-dir <dir> call <tool> '<json arguments>'` prints the same JSON as the tool.

## Where things are written

Nothing goes to `$HOME` on Miyabi. Everything a project keeps is under `<project>/.miyabi-mcp/` (mode 700,
self-ignored by git): job records and script snapshots (`submissions/<submit_id>/`, linked from
`jobs/<job number>`), append-only events (`events/`), default PBS output (`logs/`), notifications, plans and
the project `policy.json`. User-level files are in `/work/<group>/<user>/.miyabi-mcp/`: `config.json`
(e.g. `mig_hardware`), the default `policy.json`, the shared `qstat` snapshot, the job index across projects,
notifier heartbeats and the submit lock. On a workstation the daemon keeps its state in
`~/.local/state/miyabi-mcp/notifyd/`.

## Policy

`<project>/.miyabi-mcp/policy.json` limits what agents can submit in that project (unknown keys or wrong
types stop all writes). Install one with `bin/miyabi-mcp --project-dir <dir> init --policy file.json`.

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
also set `user: {max_active_jobs, budget_windows}` across all projects. The server enforces the policy; an
agent with shell access to the project could still edit the file, so do not ask agents to change it.

## Tools

| Tool | Does |
| --- | --- |
| `pbs_context` | plane, host, project and state directory, PBS projects, scheduled stop, effective policy |
| `pbs_queues` | queue shapes and status (REMAIN_ELAPSE), project limits, occupancy, token budget |
| `job_list`, `job_get` | jobs from one shared `qstat -ll` snapshot; details from `qstat [-H] -f`; output tails |
| `job_submit` | dry run → checked plan + `plan_digest`; same arguments + `dry_run=false` + digest → qsub; `notify` adds the job to a notification group |
| `job_cancel` | dry run → targets + digest; commit with a reason → qdel |
| `job_wait` | bounded wait (≤ 45 s) for short jobs |
| `notify_create`, `notify_update`, `notify_list`, `notify_cancel`, `notify_ack` | notifications for job groups, a queue reopening, or a timeout |
| `ledger_query` | records, summary, events, or smooth-research observations; `export_command` prints them as JSONL |
| `job_annotate` | labels, notes, application-level verdicts; `adopt` jobs submitted elsewhere |

## Notifications

A notification tells an agent that jobs ended, whether it is busy or idle. One notification covers a group:

```json
{"queue": "regular-g", "walltime": "01:10:00", "script": "/work/…/train.pbs", "dry_run": false, "plan_digest": "…",
 "notify": {"name": "E12-seeds", "expected": 3, "message": "all seeds done: run stats/aggregate.py"}}
```

Jobs submitted with the same `name` join the group; it is sealed when `expected` jobs have joined (or with
`notify_update(seal=true)`), and fires once when all of them have ended. With `early_failure` (default on)
a failing member is reported at once. The result carries `notify.daemon`:

- Claude Code: run `listen_command` with the Bash tool's `run_in_background: true`. It starts the daemon on
  your machine if needed and exits when the notification arrives; the exit notice is the message.
- Other agents: run `start_command`; choose `deliver` = `herdr` (`agent`), `tmux` (`target`), `codex_queue`
  (`thread`, `remote`), `command` (a name in the machine's config) or `file` (`path`).

The daemon polls the login node every 5 minutes (`notify poll` over ssh, or directly on a login node),
delivers locally, reports back, and exits after two polls with nothing left. If polling keeps failing (for
example the ssh master closed) it sends a "notification channel broken" message to every known target.
`notify_list` shows whether a daemon has polled recently. Manage it with
`bin/miyabi-mcp notifyd ensure|status|stop --notifier <name> [--remote <alias> --remote-launcher <path>]`.

## Tests

```bash
uv sync && .venv/bin/python -m unittest discover -s tests -v     # fake PBS and fake ssh; submits nothing
```

The fake PBS replays anonymized output captured on miyabi-g1 (2026-10-30).

## Status (v0.1)

Verified on Miyabi-G (2026-10-30): read-only queries against live PBS; the MCP server over `ssh -T`;
submission with a project policy (debug-mig); cancelling held and running jobs; Exit_status of qdel on a
running job (271) and of a walltime kill (−29); a background-command notification over ssh from a
workstation; the MIG profile of debug-mig (`GH200 MIG 2g.24gb`); the notification daemon on a workstation
over real ssh; and, in smooth-research's end-to-end test, a group of three jobs notified once through the
listener, acknowledged and exported into `rl run record`. herdr, tmux and codex_queue delivery and Miyabi-C
have not been run live.
