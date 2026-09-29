# Watcher Options And Recovery

Use `WAKE_SCRIPT`, `CODEX_ENDPOINT` and the task directory established in
[SKILL.md](../SKILL.md#register-and-pause). The script supports Goal restoration
with `--resume-goal`, or message delivery without it.

## Options

| Option | Meaning |
| --- | --- |
| `--job` | Required for `pbs`; one exact job or subjob ID, not an array aggregate; must be visible to `qstat` at registration |
| `--seconds` | Required for `timer`; positive finite delay from registration |
| `--message` | Required; the intended continuation and output checks |
| `--remote` | Required existing daemon endpoint; Goal mode requires `unix:///absolute/socket` |
| `--thread` | Exact thread UUID; defaults to `CODEX_THREAD_ID` |
| `--resume-goal` | Restore an existing active Goal after its authorized pause |
| `--poll-seconds` | `pbs` only; polling interval in positive finite seconds, default 300 |
| `--state-root` | Event storage; defaults to `.job-wakeup/` under the task cwd |
| `--helper` | `pbs` only; override the bundled `qstat_json.py` |
| `--codex` | Codex executable; defaults to `codex` on PATH |

Each PBS cycle queries active jobs, then up to 31 days of history if the exact
job is absent. Only observed `FINISH`/`EXPIRED` states trigger delivery, including
failed jobs and jobs deleted before starting. Detail output and `Exit_status` are retained when available; a
failed detail query does not suppress an observed terminal state.

Missing jobs remain `UNKNOWN`. Normal polling waits the configured interval
without an upper limit in seconds. Consecutive query errors use 2, 4, 8, then
16 times that interval; a successful query restores the configured interval.
Query duration adds to the wait, so 300 seconds is not an exact detection
deadline. Each registered event polls separately, and waits remain cancellable.

## Message mode

Without `--resume-goal`, the worker uses `codex queue` to send an authorized
continuation to the specified thread. It neither requires nor changes a Goal:

```bash
/usr/bin/python3 -I "$WAKE_SCRIPT" pbs --job "$JOB_ID" \
  --remote "$CODEX_ENDPOINT" --message "$CONTINUATION"
```

Retain the returned event directory/PID and end the turn once preparation is
complete. Do not call `update_goal` for this mode. On continuation, inspect the
PBS observation and acknowledge as described in SKILL.md; Goal snapshots are
absent. Registration does not itself grant authorization to resume a thread.

For an explicitly requested timer test, replace `pbs --job "$JOB_ID"` with
`timer --seconds 20`. Add `--resume-goal` only to test an existing Goal's
pause/resume lifecycle; follow the registration and pause steps in SKILL.md.
Do not register another event solely because a wake-up arrived.

## State and recovery

Each event directory contains atomic `state.json`, lock files and `worker.log`.
The states are `waiting`, `ready`, `sending`, `delivered`, `acknowledged`,
`uncertain` and `cancelled`. `delivered` means API acceptance, so check the
registered thread before concluding it resumed.

```bash
/usr/bin/python3 -I "$WAKE_SCRIPT" status "$EVENT_DIRECTORY"
/usr/bin/python3 -I "$WAKE_SCRIPT" cancel "$EVENT_DIRECTORY"
```

Cancellation is allowed only before delivery starts and cannot retract an
in-flight request. For a stopped worker, confirm that it has exited and inspect
the event state before restarting the same event. `run` runs in the foreground;
use a detached process when returning to idle:

```bash
nohup /usr/bin/python3 -I "$WAKE_SCRIPT" run "$EVENT_DIRECTORY" \
  >>"$EVENT_DIRECTORY/worker.log" 2>&1 </dev/null &
```

A per-event lock prevents concurrent workers. Delivered/acknowledged events
are not resent. A crash during sending, queue timeout or nonzero queue exit
requires reconciling the event with the live thread/Goal; the request may have
been accepted. Do not register a replacement or reset an uncertain event blindly.
Persisted state survives worker exit, but no supervisor restarts it automatically.

## Goal restoration limits

Goal mode uses WebSocket over the existing daemon's Unix socket. On completion,
it verifies the Goal and idle thread, injects the PBS evidence without starting
a turn, then sets the same Goal to `active`. The Goal continuation mechanism
starts the agent; this mode does not call `codex queue` or `turn/start`.

Goal replacement, changed budget, exhausted token budget, or lifecycle/revision
changes after the paused/idle checkpoint cancel automatic restoration. Cancel
the event when authorization is revoked. The API has no atomic compare-and-swap
for Goal updates, so avoid another controller editing that Goal during dispatch.
There is no exactly-once delivery guarantee; ambiguous responses require inspection.

See [validation.md](validation.md) for offline tests and recorded live behavior.
