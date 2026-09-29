# Validation

The skill is self-contained. Python scripts use only the standard library.
Set `SKILL_ROOT` to this installed skill directory. From any cwd, run:

```bash
/usr/bin/python3 -I "$SKILL_ROOT/tests/test_job_wakeup.py"
/usr/bin/python3 -I "$SKILL_ROOT/scripts/job_wakeup.py" pbs --help
/usr/bin/python3 -I "$SKILL_ROOT/scripts/qstat_json.py" --help
```

Tests use fake PBS responses, a fake Codex command and socket pairs; they neither
submit PBS jobs nor wake a live agent. The detached timer test uses the packaged
script, so moving the skill must not break its subprocess path. The Claude tests
run the attached watcher as a subprocess (timer, failing PBS job with a fake
`qstat`, cancel while attached) and cover agent selection, refusal of options for
the other agent, session-bound `ack` and unchanged handling of legacy Codex events.

For a live test, follow [SKILL.md](../SKILL.md) with an explicitly authorized job or timer and
Goal pause. A successful API return alone is not enough: the original Goal must
become active, the agent must execute a new turn, and that turn must acknowledge
the event. Compare goal identity, budget and usage before and after restoration.
Retain evidence in the task's event directory; never include event state, runtime
logs, credentials or user configuration in a distributed skill archive.

## Recorded live behavior

The implementation was tested on Miyabi with Codex 0.158.0 on 2026-09-29:

- A 20-second timer triggered after 20.003 seconds; the original thread
  acknowledged execution after 27.035 seconds.
- PBS job 3445269 was observed RUNNING, then FINISH with Exit_status=0.
  PBS recorded completion at 16:30:19 JST; the watcher restored the paused Goal
  at 16:31:19 JST; the agent acknowledged at 16:31:40 JST.
- The original objective, creation timestamp, token budget and accumulated usage
  were retained. The worker exited afterward. Polling had been changed to 180
  seconds during the wait.

These historical runs preceded the skill split/rename and the change to a
300-second default. They do not validate a fresh installation or its live Goal
lifecycle. Offline tests alone do not establish successful live restoration.

The Claude Code watcher was tested on Miyabi (`miyabi-g1`) with Claude Code
2.1.284 on 2026-09-29, from the installed `~/.claude/skills` path, each launched
as a background Bash task in an active session:

- A 20-second timer was delivered 0.003 seconds after its due time; the task
  completion notice arrived with the full wake-up text and `ack` from the same
  session set `acknowledged`.
- A real `qstat` query of an already finished job (3447971, Exit_status=0)
  produced the wake-up with the observation and `qstat -H -f` detail on the
  first poll.

Not observed: a wake-up of a session whose turn had already ended (the notice
reached a turn still in progress), multi-hour background retention, an
in-flight job moving through QUEUED/RUNNING, and session loss with recovery
through `run`. These rest on Claude Code's documented background-task behavior
and the offline tests.

## Bundled PBS helper

`scripts/qstat_json.py` is a byte-identical copy of
`miyabi-pbs-cmd/scripts/qstat_json.py`, last synchronized on 2026-09-29. It is
included in this package, not imported from another skill; its parser tests live
in `miyabi-pbs-cmd`. It uses Miyabi's table and history flags; it is not a
general OpenPBS/Torque adapter. After changing either copy, update both and
check them from the shared skills directory:

```bash
cmp miyabi-pbs-cmd/scripts/qstat_json.py miyabi-pbs-goal-wakeup/scripts/qstat_json.py
```
