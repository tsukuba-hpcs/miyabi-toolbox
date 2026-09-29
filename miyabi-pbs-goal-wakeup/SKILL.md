---
name: miyabi-pbs-goal-wakeup
description: Resume a Codex Goal after a Miyabi PBS job ends. Codex only. Invoke when estimated queue wait plus runtime exceeds 1 hour, or when the user requests pause-and-resume. Poll every 5 minutes by default.
---

# Miyabi PBS Goal Wake-up

## When to invoke

Invoke when estimated queue wait plus runtime exceeds 1 hour. The user may also
request this workflow for shorter jobs. Plan at submission, register after
obtaining the exact job ID, and reassess if queue wait grows. Base runtime
estimates on comparable work rather than requested walltime alone; keep unknown
wait times explicit.

This workflow requires a Codex thread (`CODEX_THREAD_ID`), the Goal tools
`get_goal`/`update_goal` and a running Codex app-server daemon. Use an existing
active Goal and explicit authorization to pause it now and resume when the job
ends. Reuse standing authorization covering this job; ask only if it is missing,
after preparing the job ID and continuation. Do not create a Goal implicitly.
Without an active Goal, use the optional
[message mode](references/usage.md#message-mode) only when continuation is
authorized. If neither mode applies, register nothing; report the job ID, its
current state and how to check it.

## Register and pause

The bundled [job_wakeup.py](scripts/job_wakeup.py) uses host Python 3.9+ and the
standard library. It needs Miyabi `qstat` on PATH and an existing Codex daemon
with Goal APIs (live-tested with 0.158.0). It queries PBS using its bundled
helper; no other skill is required, and it does not submit or modify PBS jobs.

1. Set `WAKE_SCRIPT` to the absolute path of `scripts/job_wakeup.py` in the
   directory containing this SKILL.md. Run from the task directory so
   `.job-wakeup/` belongs to that task. Inspect existing event states to avoid
   registering another watcher for the same Goal/job.
2. Confirm the current Goal is `active` with `get_goal`. Use the current
   `CODEX_THREAD_ID`; supply `--thread` only for an explicitly selected thread.
   Derive the endpoint from the existing daemon; do not start or restart a
   daemon for this workflow:

   ```bash
   CODEX_ENDPOINT="$(codex app-server daemon version | /usr/bin/python3 -I -c 'import json, sys; info = json.load(sys.stdin); assert info.get("status") == "running", info; print("unix://" + info["socketPath"])')"
   ```

3. Set `JOB_ID` to the exact job/subjob ID and `CONTINUATION` to the next action,
   log/output paths and acceptance checks. Register one event:

   ```bash
   /usr/bin/python3 -I "$WAKE_SCRIPT" pbs \
     --job "$JOB_ID" --resume-goal \
     --remote "$CODEX_ENDPOINT" --message "$CONTINUATION"
   ```

   Registration fails if `qstat` cannot see the job among active jobs or 31
   days of history. PBS polling defaults to **300 seconds (5 minutes)**. Use
   `--poll-seconds` for a requested override, with no upper limit in seconds;
   see [polling and error backoff](references/usage.md#options).

4. Retain the returned event directory and PID. Only after successful
   registration, call `update_goal(status="paused")`, report that status and
   end the turn immediately. The worker must observe the Goal paused and the
   thread idle within five minutes of registration. Do not wait by keeping an
   agent turn in a sleep or polling loop.

If prerequisites or registration fail, report the specific limitation without
claiming a watcher is armed or pausing the Goal for an unregistered event.

## After wake-up

Shell variables from the registration turn are gone. The wake-up message gives
the event's `state.json` path and the exact `ack` argv. Read that `state.json`
and the sibling `worker.log`. Verify the job ID and observed terminal state. For
Goal mode, also verify `goal_before_resume.status=paused`,
`goal_after_resume.status=active`, preserved Goal identity/budget, and the live
Goal via `get_goal`. Acknowledge from the registered thread with the argv from
the message, which has this form:

```bash
/usr/bin/python3 -I "$WAKE_SCRIPT" ack "$EVENT_DIRECTORY"
```

`delivered` records API acceptance; `acknowledged` records that the agent resumed.
Carry out the continuation and validate expected outputs: `FINISH` or
`Exit_status=0` alone does not establish application success, and a `FINISH`
job with no start time never ran. Complete the Goal only when its objective is
satisfied.

## Recovery

Use `status EVENT_DIRECTORY` to inspect an event and `cancel EVENT_DIRECTORY`
to revoke a waiting/ready event. A stopped worker has no automatic supervisor;
`run EVENT_DIRECTORY` resumes it in the foreground. For detached recovery,
uncertain delivery, timer tests and Goal concurrency limits, read
[usage.md](references/usage.md). Never blindly resend an `uncertain` event.
Validation commands and historical live results are in
[validation.md](references/validation.md).
