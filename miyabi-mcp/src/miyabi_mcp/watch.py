"""Watches: conditions over ledger jobs or a queue, a waiter process, delivery and acknowledgement.

DESIGN.md 8.6, 9.2–9.5. Exit codes of `miyabi-mcp wait --watch`: 0 delivered (condition met or timed
out), 2 bad arguments, 3 cancelled, 75 the watch could not continue (PBS queries kept failing, or the
delivery failed) — every exit wakes a Claude Code session, so a broken watch is never silent.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import deliver as delivery, observe
from .config import LAUNCHER, Settings
from .ledger import Store, host
from .util import Lock, MiyabiError, compact, now, now_iso, ok, parse_duration, read_json, seconds_to_hms, short_id, write_json

WHEN = {"all_ended", "any_ended", "started"}
SETTLED = {"ended", "unknown"}
OPEN = {"armed", "fired"}
MAX_ERRORS = 6


def _path(store: Store, watch_id: str) -> Path:
    return store.state / "watches" / f"{watch_id}.json"


def _read(store: Store, watch_id: str) -> dict:
    watch = read_json(_path(store, watch_id))
    if watch is None:
        raise MiyabiError("not_found", f"no watch {watch_id} in this project")
    return watch


def _update(store: Store, watch_id: str, change) -> dict:
    with store.lock(f"watch-{watch_id}"):
        watch = _read(store, watch_id)
        change(watch)
        write_json(_path(store, watch_id), watch)
        return watch


def _commands(settings: Settings, watch_id: str, project_dir: Path) -> dict:
    local = f"{LAUNCHER} wait --watch {watch_id} --project-dir {project_dir}"
    alias = settings.config.get("remote_alias", "miyabi-g")
    return {"command_local": local, "command_via_ssh": f"ssh -o BatchMode=yes {alias} {local}"}


def create(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    job_ids = list(args.get("job_ids") or [])
    queue = args.get("queue")
    if bool(job_ids) == bool(queue):
        raise MiyabiError("invalid_arguments", "give exactly one of job_ids or queue")
    when = args.get("when", "all_ended")
    if job_ids and when not in WHEN:
        raise MiyabiError("invalid_arguments", f"when must be one of {sorted(WHEN)}")
    full_ids = []
    for job_id in job_ids:
        record = store.find(job_id)
        if record is None:
            raise MiyabiError("not_in_ledger", f"job {job_id} is not in this project's ledger; adopt it first")
        full_ids.append(record["job_id"])
    if queue and (not isinstance(queue, dict) or not queue.get("queue")):
        raise MiyabiError("invalid_arguments", 'queue must look like {"queue": "debug-g", "state": "started"}')
    target = delivery.validate(args.get("deliver"), settings, store.project_dir)
    deadline = None
    if args.get("timeout"):
        deadline = (now() + timedelta(seconds=parse_duration(args["timeout"]))).isoformat()
    watch_id = f"W-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{short_id(2)}"
    watch = {
        "schema_version": 1, "watch_id": watch_id, "project_dir": str(store.project_dir),
        "created_at": now_iso(), "created_ts": time.time(), "jobs": full_ids, "when": when if job_ids else None,
        "queue": queue, "deadline": deadline, "deliver": target, "message": args.get("message") or "",
        "labels": args.get("labels") or {}, "status": "armed", "ack_token": short_id(3),
        "poll_seconds": int(args.get("poll_seconds") or settings.config.get("poll_seconds", 60)),
        "waiter": None, "fired": None, "delivery": None, "history": [{"at": now_iso(), "status": "armed"}],
    }
    if target["method"] == "codex_goal":
        watch.update(delivery.goal_register(target))
    write_json(_path(store, watch_id), watch)
    for job_id in full_ids:
        store.update(job_id, lambda record: record.setdefault("watches", []).append(watch_id))
    commands = _commands(settings, watch_id, store.project_dir)
    started = None
    if args.get("start_waiter", target["method"] != "claude_background"):
        started = spawn_waiter(store, watch_id)
    how = ("Run command_local (agent on a Miyabi login node) or command_via_ssh (agent elsewhere) with the Bash "
           "tool's run_in_background=true, then end your turn; its exit wakes you with the result."
           if target["method"] == "claude_background" and not started else
           "A detached waiter was started on the login node; end your turn.")
    return ok(watch_id=watch_id, status="armed", deliver=target["method"], jobs=full_ids, queue=queue,
              deadline=deadline, waiter=started, how=how, **commands)


def spawn_waiter(store: Store, watch_id: str) -> dict:
    log = store.state / "watches" / f"{watch_id}.log"
    with open(log, "ab", buffering=0) as stream:
        process = subprocess.Popen([str(LAUNCHER), "wait", "--watch", watch_id, "--project-dir", str(store.project_dir)],
                                   stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, start_new_session=True,
                                   close_fds=True)
    return {"host": host(), "pid": process.pid, "log": str(log)}


def _job_summary(record: dict) -> dict:
    state = record.get("state") or {}
    submit = record.get("submit") or {}
    usage = record.get("usage") or {}
    return {"job_id": record.get("job_id"), "name": record.get("name"), "execution_state": state.get("execution_state"),
            "outcome": state.get("outcome"), "raw_state": state.get("raw_state"), "exit_status": state.get("exit_status"),
            "requested_walltime_s": submit.get("walltime_s"), "usage": usage or None, "output": submit.get("output")}


def evaluate(settings: Settings, store: Store, watch: dict, max_age: float) -> tuple[bool, str | None, list[dict]]:
    if watch.get("deadline") and now() >= datetime.fromisoformat(watch["deadline"]):
        reason = "timeout"
    else:
        reason = None
    if watch.get("jobs"):
        records, _snap = observe.refresh_jobs(store, watch["jobs"], max_age)
        summary = [_job_summary(record) for record in records]
        states = [item["execution_state"] for item in summary]
        when = watch["when"]
        if when == "all_ended" and all(state in SETTLED for state in states):
            reason = reason or f"all {len(states)} jobs ended"
        elif when == "any_ended" and any(state in SETTLED for state in states):
            reason = reason or f"{sum(state in SETTLED for state in states)} of {len(states)} jobs ended"
        elif when == "started" and all(state in SETTLED | {"running"} for state in states):
            reason = reason or "all jobs started"
        return reason is not None, reason, summary
    name = watch["queue"]["queue"]
    data = observe.queues(settings, max_age=max_age)
    entries = [q for q in data["queues"] if q["name"] == name or q.get("parent") == name]
    leaves = [q for q in entries if not q.get("routing")]
    summary = [{"queue": q["name"], "status": q.get("status")} for q in leaves]
    if leaves and all(q.get("enabled") and q.get("started") for q in leaves):
        reason = reason or f"queue {name} is enabled and started"
    return reason is not None, reason, summary


def render(watch: dict, reason: str, summary: list[dict]) -> str:
    stamp = now().strftime("%H:%M %Z")
    lines = [f"[miyabi-mcp] watch {watch['watch_id']} fired: {reason} ({stamp})"]
    for item in summary:
        if "job_id" in item:
            usage = item.get("usage") or {}
            used = usage.get("source_ref", "").split("=", 1)[-1].split(" x ")[0] if usage else "-"
            requested = seconds_to_hms(item["requested_walltime_s"]) if item.get("requested_walltime_s") else "-"
            amount = f"{usage['quantity']} {usage['unit']}" if usage else "no usage recorded"
            lines.append(f"- {item['job_id']} {item.get('name')}  {item.get('raw_state')} ({item.get('outcome')})  "
                         f"walltime {used}/{requested}  {amount}  log {item.get('output')}")
        else:
            lines.append(f"- queue {item['queue']}: {item['status']}")
    if watch.get("jobs"):
        lines.append("PBS exit status is not application success: check the outputs before continuing.")
        ids = compact(watch["jobs"])
        lines.append(f"Details: job_get(job_ids={ids}, tail_lines=50).")
    if watch.get("message"):
        lines.append(f"Your note: {watch['message']}")
    lines.append(f'Acknowledge: watch_ack(watch_id="{watch["watch_id"]}", ack_token="{watch["ack_token"]}")')
    return "\n".join(lines)


def run_waiter(settings: Settings, watch_id: str) -> int:
    store = Store(settings)
    try:
        watch = _read(store, watch_id)
    except MiyabiError as exc:
        print(f"[miyabi-mcp] {exc.message}", flush=True)
        return 2
    poll = int(watch.get("poll_seconds") or 60)
    lock = Lock(store.state / "locks" / f"waiter-{watch_id}", lease=3 * poll + 60, timeout=0)
    try:
        lock.acquire()
    except MiyabiError:
        print(f"[miyabi-mcp] another waiter is running for watch {watch_id}: {lock.holder()}", flush=True)
        return 2
    try:
        return _wait(settings, store, watch_id, poll, lock)
    finally:
        lock.release()


def _heartbeat(store: Store, watch_id: str, lock: Lock, error: str | None = None) -> dict:
    os.utime(lock.path)

    def change(watch: dict) -> None:
        waiter = watch.get("waiter") or {}
        if waiter.get("pid") != os.getpid() or waiter.get("host") != host():
            waiter = {"host": host(), "pid": os.getpid(), "started_at": now_iso()}
        waiter["beat_at"] = now_iso()
        watch["waiter"] = waiter
        if error is not None:
            watch["last_error"] = {"at": now_iso(), "error": error}

    return _update(store, watch_id, change)


def _wait(settings: Settings, store: Store, watch_id: str, poll: int, lock: Lock) -> int:
    watch = _heartbeat(store, watch_id, lock)
    print(compact({"watch_id": watch_id, "armed": True, "jobs": watch.get("jobs"), "queue": watch.get("queue"),
                   "poll_seconds": poll, "waiter": watch["waiter"]}), flush=True)
    errors = 0
    max_errors = int(settings.config.get("waiter_max_errors", MAX_ERRORS))
    goal_mode = watch["deliver"]["method"] == "codex_goal"
    while True:
        watch = _read(store, watch_id)
        if watch["status"] == "cancelled":
            print(f"[miyabi-mcp] watch {watch_id} cancelled", flush=True)
            return 3
        if watch["status"] not in OPEN:
            print(f"[miyabi-mcp] watch {watch_id} is already {watch['status']}", flush=True)
            return 0
        try:
            if goal_mode and not watch.get("goal_parked"):
                action, reason, goal = delivery.goal_guard(watch)
                if action == "cancel":
                    _update(store, watch_id, lambda w: w.update(status="cancelled", cancellation_reason=reason))
                    continue
                if action == "park":
                    _update(store, watch_id, lambda w: w.update(goal_parked=goal, goal_parked_at=now_iso()))
                else:
                    time.sleep(2)
                    continue
            if watch["status"] == "armed":
                fired, reason, summary = evaluate(settings, store, watch, max_age=poll)
                if fired:
                    message = render(watch, reason or "fired", summary)
                    watch = _update(store, watch_id, lambda w: w.update(
                        status="fired", fired={"at": now_iso(), "reason": reason, "summary": summary,
                                               "message": message}))
            if watch["status"] == "fired":
                outcome = delivery.send(settings, watch, watch["fired"]["message"])
                status = {"delivered": "delivered", "uncertain": "uncertain", "failed": "failed",
                          "cancelled": "cancelled"}.get(outcome["status"], "fired")
                _update(store, watch_id, lambda w: w.update(
                    status=status, delivery={"at": now_iso(), **outcome},
                    history=w["history"] + [{"at": now_iso(), "status": status}]))
                if status == "delivered":
                    return 0
                if status != "fired":
                    print(f"[miyabi-mcp] watch {watch_id} delivery {status}: {outcome}", flush=True)
                    return 75
            errors = 0
            _heartbeat(store, watch_id, lock)
        except MiyabiError as exc:
            errors += 1
            _heartbeat(store, watch_id, lock, error=f"{exc.kind}: {exc.message}")
            if errors >= max_errors:
                print(f"[miyabi-mcp] watch {watch_id} could not continue after {errors} failed queries: "
                      f"{exc.kind}: {exc.message}\nResume later with: {_commands(settings, watch_id, store.project_dir)['command_local']}",
                      flush=True)
                return 75
        delay = poll * 2 ** min(errors, 4)
        end = time.monotonic() + delay
        while time.monotonic() < end:
            time.sleep(min(2.0, max(0.0, end - time.monotonic())))
            if _read(store, watch_id)["status"] == "cancelled":
                break
            os.utime(lock.path)


def job_wait(settings: Settings, args: dict, cancelled=lambda: False, progress=None) -> dict:
    """Bounded wait inside one tool call (for jobs of a few minutes)."""
    store = Store(settings)
    job_ids = list(args.get("job_ids") or [])
    when = args.get("when", "all_ended")
    if not job_ids or when not in WHEN:
        raise MiyabiError("invalid_arguments", f"give job_ids and when in {sorted(WHEN)}")
    limit = float(settings.config.get("job_wait_max_s", 45))
    timeout = min(float(args.get("timeout_s") or limit), limit)
    probe = {"jobs": job_ids, "when": when, "deadline": None, "queue": None}
    start = time.monotonic()
    while True:
        met, reason, summary = evaluate(settings, store, probe, max_age=5)
        elapsed = time.monotonic() - start
        if met or elapsed >= timeout or cancelled():
            return ok(met=met, reason=reason, waited_s=round(elapsed, 1), jobs=summary,
                      next=None if met else "not yet: for a long wait use watch_create and end your turn")
        if progress:
            progress(elapsed, timeout)
        time.sleep(min(10.0, max(0.5, timeout - elapsed)))


def watch_list(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    wanted = set(args.get("status") or [])
    watches = []
    for path in sorted((store.state / "watches").glob("W-*.json")):
        watch = read_json(path)
        if not watch or (wanted and watch["status"] not in wanted):
            continue
        waiter = watch.get("waiter") or {}
        beat = waiter.get("beat_at")
        limit = 3 * int(watch.get("poll_seconds") or 60) + 60
        alive = bool(beat) and (now() - datetime.fromisoformat(beat)).total_seconds() <= limit
        item = {k: watch.get(k) for k in ("watch_id", "status", "jobs", "when", "queue", "deadline", "created_at",
                                          "last_error")}
        item.update(deliver=watch["deliver"]["method"], waiter=waiter or None,
                    waiter_alive=alive if watch["status"] in OPEN else None,
                    fired_reason=(watch.get("fired") or {}).get("reason"),
                    delivered_at=(watch.get("delivery") or {}).get("at"))
        if watch["status"] in OPEN and not alive:
            item.update(_commands(settings, watch["watch_id"], store.project_dir))
        watches.append(item)
    return ok(count=len(watches), watches=watches)


def watch_cancel(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    watch_id = args.get("watch_id")

    def change(watch: dict) -> None:
        if watch["status"] not in OPEN:
            raise MiyabiError("invalid_state", f"watch is {watch['status']}; only armed or fired watches can be "
                              "cancelled (a delivery cannot be retracted)")
        watch.update(status="cancelled", cancellation_reason=args.get("reason"),
                     history=watch["history"] + [{"at": now_iso(), "status": "cancelled"}])

    return ok(watch=_update(store, watch_id, change))


def watch_ack(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    watch_id = args.get("watch_id")

    def change(watch: dict) -> None:
        if args.get("ack_token") != watch["ack_token"]:
            raise MiyabiError("invalid_arguments", "ack_token does not match the one in the wake-up message")
        if watch["status"] not in {"delivered", "uncertain", "acknowledged", "fired"}:
            raise MiyabiError("invalid_state", f"watch is {watch['status']}; nothing was delivered yet")
        watch.setdefault("acknowledged_at", now_iso())
        watch["status"] = "acknowledged"
        watch["history"] = watch["history"] + [{"at": now_iso(), "status": "acknowledged"}]

    watch = _update(store, watch_id, change)
    return ok(watch_id=watch_id, status=watch["status"], acknowledged_at=watch["acknowledged_at"])
