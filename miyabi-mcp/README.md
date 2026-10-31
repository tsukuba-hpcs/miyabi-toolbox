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
| `interactive_plan` | checked interactive allocation plan → `launch_command` and pi-env-mcp `terminal_open` arguments; submission happens when that command runs in a PTY |
| `allocation_check` | verify the calling shell's compute host, target architecture, nodefile and live PBS allocation; fails outside a confirmed running allocation |
| `job_cancel` | dry run → targets + digest; commit with a reason → qdel |
| `job_wait` | bounded wait (≤ 45 s) for short jobs |
| `notify_create`, `notify_update`, `notify_list`, `notify_cancel`, `notify_ack` | notifications for job groups, a queue reopening, or a timeout |
| `ledger_query` | records, summary, events, or smooth-research observations; `export_command` prints them as JSONL |
| `job_annotate` | labels, notes, application-level verdicts; `adopt` jobs submitted elsewhere |

## Interactive compute allocations

Use an interactive allocation for short iterative tests, builds and debugging. The workflow follows
[miyabi-pbs-cmd's interactive guidance](../miyabi-pbs-cmd/references/qsub.md#interactive-work).
`miyabi-mcp` checks resources and policy and keeps the job ledger; pi-env-mcp's persistent terminal carries
the shell. Both servers must be configured, and the Miyabi installation of `bin/miyabi-mcp` and its Python
environment must be usable from the selected compute system. G is aarch64; C needs its own x86_64 venv.

1. Call `pbs_context`, then `pbs_queues` to inspect current queues, accounting groups, limits and remaining
   walltime before maintenance. Select a top-level `interact-g`, `interact-c` or `interact-mig` queue;
   indented destinations such as `interact-g_n1` are not submission targets. `select` counts nodes for
   G/C and MIG instances for MIG. Omit `group` only when there is exactly one eligible choice.
2. Call `interactive_plan`, for example:

   ```json
   {"queue":"interact-g","nodes":1,"walltime":"00:05:00",
    "name":"interactive-check","request_key":"project-check-001"}
   ```

   Optional arguments: `select` (e.g. `1:mpiprocs=1`), `group`, `name`, `workdir`, `env`, `labels`,
   `client_ref`, `note`, and the usual `project`. `queue`, `walltime` and `request_key` are required.
   This checks the same queue, group, per-job, concurrency and budget policies as batch submissions.
   It returns `valid`, `checks`, `plan`, `plan_digest`, `qsub_preview` and, on success,
   `launch_command`, `terminal_open` and `allocation_check_command`. It submits **no PBS job**.
   Choose a fresh request key per intended allocation, and reuse that key for retries of that allocation.
3. Within 10 minutes, pass the returned `terminal_open` object to **pi-env-mcp `terminal_open`**. Adjust
   its `host` to the configured pi-env-mcp login alias if necessary (normally `miyabi-g`). The command
   uses `exec ... interactive-start <plan_digest>` so completion closes the terminal. Keep the returned
   `terminal_id`; all subsequent input goes to that same terminal. The runner rechecks policy at launch,
   runs `qsub -I`, records its exact job ID and prints a JSON submission result with the transcript path
   and a check command tied to that job. The user-level submit lock is released once PBS accepts the job,
   so queue waiting does not block other submissions.
4. Read output with pi-env-mcp `terminal_read` until the compute shell appears. An accepted job ID or
   quiet output is not evidence of a ready shell. `job_get` and `job_wait` with `when="started"` provide
   scheduler state; `job_wait` waits at most 45 seconds. For longer waits, create a notification with
   `when="started"` for the recorded job and keep the terminal connection alive. A terminal tool timeout
   leaves the allocation queued or running; do not start another terminal to repeat the submission.
5. Send the returned `allocation_check_command` followed by `\n` using pi-env-mcp `terminal_send`:

   ```json
   {"terminal_id":"t1","input":"<allocation_check_command>\n","timeout":10}
   ```

   Replace the placeholder with the returned command. Require exit 0 and `ok=true` before executing
   workloads. The check combines the current hostname, architecture, `PBS_JOBID`, `PBS_NODEFILE`,
   live PBS RUNNING state, owner and execution hosts. It also reports `remaining_walltime_s` (`null`
   means unknown). Calling `allocation_check` on the login-side MCP server checks that server's shell;
   it cannot confirm the compute shell in your terminal. Recheck after any shell/host change.
6. In the confirmed compute shell, `cd "$MIYABI_PROJECT_DIR"` (or the requested `workdir`), establish
   [modules](../miyabi-pbs-shell-template/references/module.md) and select the project's interpreter.
   Exports, loaded modules, cwd and REPL state persist across `terminal_send` calls. Run fallible tests
   in a child shell, inspect their exit code, and save logs under `/work`, for example:

   ```bash
   # CHECK_SCRIPT must already point to the intended validation script.
   export CHECK_SCRIPT
   export LOG_ROOT="$MIYABI_PROJECT_DIR/validation/$PBS_JOBID"
   mkdir -p "$LOG_ROOT"
   bash -o pipefail -c 'bash "$CHECK_SCRIPT" 2>&1 | tee "$LOG_ROOT/validation.log"'
   printf 'validation exit=%s\n' "$?"
   ```

   Check remaining walltime before starting each substantial step. Tests, application imports, builds
   and GPU work belong in this confirmed allocation, not on the login host. `C-c` through
   `terminal_send(keys=["C-c"])` interrupts foreground work; inspect the shell before sending more input.
7. Preserve required scratch outputs and stop your own background processes. Send `exit\n` to release
   the allocation; the terminal closes when qsub ends, without falling back to a login shell. Use
   `job_get` to confirm completion and inspect application outputs; FINISH alone is not success.
   To cancel a queued or running allocation, use `job_cancel` dry-run and commit with the exact job ID,
   digest and reason, then `terminal_close`. Normal terminal exit does not require a separate cancel.

The CLI equivalent of step 2 is
`bin/miyabi-mcp --project-dir <dir> call interactive_plan '<JSON above>'`; run its returned command in a
persistent PTY, never pi-env-mcp `bash` or a pipe. Interactive jobs use the ordinary ledger, labels,
budget accounting, `job_list`, `job_get`, `job_cancel` and notifications. Their `submit.interactive` is
true; `output` points to a raw terminal transcript under `.miyabi-mcp/logs/`, rather than a PBS output
file. `job_get(tail_lines=…)` reads this transcript and reports `output_source="terminal_transcript"`.
Store important application logs separately. There is no batch script snapshot for these jobs.

Closing the terminal, restarting pi-env-mcp or losing its connection ends the terminal session. The
runner attempts to cancel its exact allocation on a caught disconnect; abrupt process/node failure
cannot guarantee cleanup. Check `job_get` and cancel any remaining job before starting a replacement.
Re-running a launch with the same `request_key` returns the existing job and never reattaches a shell or
creates another allocation. An uncertain submission is reconciled by `MIYABI_SUBMIT_ID`; do not bypass
that result with a new key. Re-plan after plan expiry; use a new key only for an intentional new allocation.

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

Interactive verification (2026-10-31 UTC / 2026-11-01 JST), through pi-env-mcp on `miyabi-g`: a one-node
`interact-g` allocation on `mg0004` (job `3752062.opbs`) passed both allocation guards, retained shell
variables/cwd across calls, survived a failing child check and exited normally (PBS Exit_status 0).
A second allocation on `mg0032` (`3752112.opbs`) ran all 52 tests successfully, then was cancelled through
`job_cancel` (PBS Exit_status 265, ledger outcome `cancelled`). Both were confirmed finished, with no
active jobs left in the test project. Transcript tails, idempotent launch retries and rejection of login-side
allocation checks were also verified. C and MIG interactive allocations, and real connection-loss cleanup, remain untested live;
fake-PBS tests cover C/MIG planning and disconnect cleanup before and after receipt of the job ID.
