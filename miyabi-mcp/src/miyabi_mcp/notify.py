"""Notifications: job groups, sealing, conditions and an outbox the agent-side daemon delivers (DESIGN.md 9).

The login node decides (shared qstat snapshot + ledger); a daemon on the agent's machine polls with
`miyabi-mcp notify poll --notifier <name>`, delivers locally and reports back with `notify report`.
Each notifier has a heartbeat in the user directory (notifiers/<name>.json): when its last poll started,
the last poll without errors, the daemon's actual interval, that poll's errors and the exit the daemon
reported. daemon_info derives the daemon's state from it.
"""

from __future__ import annotations

import shlex
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from . import deliver as delivery, observe
from .config import LAUNCHER, NOTIFY_POLL_SECONDS, STATE_NAME, Settings
from .ledger import Store, host
from .util import (Lock, MiyabiError, check_notifier, compact, now, now_iso, ok, parse_duration, read_json,
                   seconds_to_hms, short_id, write_json)

WHEN = {"all_ended", "any_ended", "started"}
# Not closed yet. Only `open` ones collect jobs: a fired notification is never evaluated again, so a job
# added to it would never be reported, and its name is free for a new group.
OPEN = {"open", "fired"}
CLOSED = {"delivered", "acknowledged", "cancelled", "dismissed", "failed"}
# One-off messages sent while the group keeps waiting, and the notification field that records them.
NOTICES = {"early": "early_sent", "unknown": "unknown_sent"}
MAX_ATTEMPTS = 5
CLOSED_WINDOW_S = 30 * 86400  # how long a poll keeps reporting a closed notification back to the daemon
STATE_TEXT = {"never": "has never polled", "idle_exited": "exited after polling with nothing left to do",
              "stopped": "was stopped", "stale": "has not polled for more than two intervals (killed, or its "
                                                  "connection is down)"}


# -- storage ----------------------------------------------------------------------------------------------------

def _path(state: Path, notification_id: str) -> Path:
    return state / "notifications" / f"{notification_id}.json"


def _load(store: Store, key: str) -> dict:
    """A notification by id, or by name: the open one of that name, else the latest fired one."""
    found = read_json(_path(store.state, key)) if key.startswith("N-") else None
    if found is None:
        found = _named(store.state, key, "open") or _named(store.state, key, "fired")
    if found is None:
        raise MiyabiError("not_found", f"no notification {key} in this project")
    return found


def _named(state: Path, name: str, status: str) -> dict | None:
    """The latest notification called `name` with this status (ids sort by creation time)."""
    found = [n for n in _all(state) if name and n.get("name") == name and n["status"] == status]
    return found[-1] if found else None


def _all(state: Path) -> list[dict]:
    return [n for n in (read_json(p) for p in sorted((state / "notifications").glob("N-*.json"))) if n]


def _update(store: Store, notification_id: str, change) -> dict:
    with store.lock(f"notify-{notification_id}"):
        item = read_json(_path(store.state, notification_id))
        if item is None:
            raise MiyabiError("not_found", f"no notification {notification_id}")
        change(item)
        write_json(_path(store.state, notification_id), item)
        return item


def _transition(item: dict, status: str, **fields) -> None:
    item.update(status=status, history=item["history"] + [{"at": now_iso(), "status": status}], **fields)


def _closed_at(item: dict) -> str:
    return item["history"][-1]["at"] if item.get("history") else item.get("created_at")


def _reached(item: dict, status: str) -> str | None:
    """When the notification last entered `status`, from its history."""
    return next((h["at"] for h in reversed(item.get("history") or []) if h.get("status") == status), None)


# -- the agent-side daemon: names, commands, heartbeat ------------------------------------------------------------

def default_notifier(settings: Settings) -> str:
    return settings.config.get("notifier") or host()


def poll_seconds(settings: Settings) -> int:
    """The configured poll interval (notify_poll_seconds): the returned commands pass it to the daemon."""
    return int(settings.config.get("notify_poll_seconds", NOTIFY_POLL_SECONDS))


def export_command(settings: Settings, project_dir: Path | str, notifier: str | None = None) -> str:
    """A command for the agent's machine that prints this project's smooth-research JSONL (data never passes
    through the model). `notifier` names the agent's machine; on another machine the command goes over ssh."""
    local = shlex.join([str(LAUNCHER), "--project-dir", str(project_dir), "ledger", "--format", "smooth-research",
                        "--jsonl"])
    notifier = notifier or settings.config.get("notifier")
    if not notifier or notifier == host():
        return local
    # ssh joins its arguments into one remote command line: `local` is quoted for that shell.
    ssh = ["ssh", "-T", "-o", "BatchMode=yes", settings.config.get("remote_alias", "miyabi-g")]
    return shlex.join(ssh) + " " + local


def _command(settings: Settings, notifier: str, action: list[str]) -> str:
    local = notifier == host()
    launcher = str(LAUNCHER) if local else settings.config.get("local_launcher", "miyabi-mcp")
    remote = [] if local else ["--remote", settings.config.get("remote_alias", "miyabi-g"),
                               "--remote-launcher", str(LAUNCHER)]
    return shlex.join([launcher, *action, "--notifier", notifier, "--poll-seconds", str(poll_seconds(settings)),
                       *remote])


def listen_command(settings: Settings, notifier: str, notification_id: str) -> str:
    return _command(settings, notifier, ["notify", "listen", "--id", notification_id])


def _beat_path(settings: Settings, notifier: str) -> Path:
    return settings.user_dir / "notifiers" / f"{notifier}.json"


def _beat_update(settings: Settings, notifier: str, change) -> dict:
    """Read-modify-write a heartbeat: a daemon's exit report and its successor's first poll may race."""
    path = _beat_path(settings, notifier)
    with Lock(settings.user_dir / "locks" / f"notifier-{notifier}", lease=60, timeout=30):
        beat = read_json(path) or {}
        change(beat)
        write_json(path, beat)
        return beat


def daemon_state(beat: dict, configured_s: int) -> str:
    """running, idle_exited, stopped, stale (no poll for two intervals + 60 s) or never.

    An exit counts only when the daemon that reported it is also the one that polled last: a successor
    that polled in between is the one running.
    """
    if not beat.get("last_poll"):
        return "never"
    exited = beat.get("exited")
    if exited and exited.get("instance") == beat.get("instance"):
        return "idle_exited" if exited.get("reason") == "idle" else "stopped"
    interval = int(beat.get("interval_s") or configured_s)
    age = (now() - datetime.fromisoformat(beat["last_poll"])).total_seconds()
    return "running" if age <= 2 * interval + 60 else "stale"


def daemon_info(settings: Settings, notifier: str, notification_id: str | None = None, *, open_count: int = 0) -> dict:
    """The notifier's state as the server sees it, and the commands that start its daemon.

    `daemon_running` (kept for older callers) is true exactly when `state` is running: a poll started within
    two intervals plus 60 s and no exit was reported since. A hint appears only when open notifications wait
    for a daemon that is not running; idle_exited with nothing open is the normal resting state.
    """
    beat = read_json(_beat_path(settings, notifier)) or {}
    configured = poll_seconds(settings)
    state = daemon_state(beat, configured)
    info = {"notifier": notifier, "state": state, "daemon_running": state == "running",
            "last_poll": beat.get("last_poll"), "last_ok": beat.get("last_ok"), "poll_seconds": configured,
            "daemon_poll_seconds": beat.get("interval_s"),
            "start_command": _command(settings, notifier, ["notifyd", "ensure"])}
    if state in {"idle_exited", "stopped"}:
        info["exited"] = {k: beat["exited"].get(k) for k in ("at", "reason")}
    if beat.get("errors"):
        info["errors"] = beat["errors"]
    if notification_id:
        info["listen_command"] = listen_command(settings, notifier, notification_id)
    actual = beat.get("interval_s")
    if state == "running" and actual and int(actual) != configured:
        info["note"] = (f"the running daemon polls every {actual} s, not the configured {configured} s; it keeps "
                        "its interval until it exits (or `notifyd stop`, then start_command)")
    if open_count and state != "running":
        info["hint"] = (f"{open_count} open notification(s) wait for notifier {notifier}, whose daemon "
                        f"{STATE_TEXT[state]}: on that machine run listen_command (deliver=listener) or "
                        "start_command now")
    return info


def _open_count(store: Store, notifier: str) -> int:
    return sum(1 for n in _all(store.state) if n.get("notifier") == notifier and n["status"] in OPEN)


def _next_step(item: dict, created: bool) -> str:
    if item["deliver"]["method"] != "listener":
        return ("Run daemon.start_command on the machine named by daemon.notifier now, even when a daemon seems "
                "to be running: it is idempotent and starts one only when none runs there.")
    if created:
        return ("Run daemon.listen_command on the machine named by daemon.notifier now (Claude Code: Bash with "
                "run_in_background=true), even when a daemon already runs. With deliver=listener the message is "
                "only written to that machine's inbox and only the listener shows it: start_command alone never "
                "does. One receiver per notification: do not run a second listener for it.")
    return ("This job joined an existing notification. If its listener already runs, run daemon.start_command now "
            "(idempotent) instead of a second listener: one receiver per notification. Otherwise run "
            "daemon.listen_command (Claude Code: run_in_background=true).")


# -- create, join, update -----------------------------------------------------------------------------------------

def normalize_spec(settings: Settings, spec) -> dict:
    """job_submit's notify argument with its defaults filled in; the plan digest covers exactly this."""
    if not isinstance(spec, dict):
        raise MiyabiError("invalid_arguments", "notify must be an object")
    when = spec.get("when", "all_ended")
    if when not in WHEN:
        raise MiyabiError("invalid_arguments", f"notify.when must be one of {sorted(WHEN)}")
    expected = spec.get("expected")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected < 1):
        raise MiyabiError("invalid_arguments", "notify.expected must be a positive integer")
    labels = spec.get("labels") or {}
    if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
        raise MiyabiError("invalid_arguments", "notify.labels must map names to string values")
    return {"name": spec.get("name") or None, "expected": expected, "when": when,
            "early_failure": bool(spec.get("early_failure", True)),
            "timeout": parse_duration(spec["timeout"]) if spec.get("timeout") else None,
            "deliver": delivery.validate_shape(spec.get("deliver")), "message": spec.get("message") or "",
            "notifier": check_notifier(spec.get("notifier") or default_notifier(settings)),
            "labels": dict(sorted(labels.items()))}


def _new(settings: Settings, store: Store, args: dict, jobs: list[str], seal_now: bool = False) -> dict:
    when = args.get("when", "all_ended")
    if when not in WHEN:
        raise MiyabiError("invalid_arguments", f"when must be one of {sorted(WHEN)}")
    queue = args.get("queue")
    if queue and (not isinstance(queue, dict) or not queue.get("queue")):
        raise MiyabiError("invalid_arguments", 'queue must look like {"queue": "debug-g", "state": "started"}')
    expected = args.get("expected")
    if expected is not None and (not isinstance(expected, int) or expected < 1):
        raise MiyabiError("invalid_arguments", "expected must be a positive integer")
    deadline = None
    if args.get("timeout"):
        deadline = (now() + timedelta(seconds=parse_duration(args["timeout"]))).isoformat()
    item = {
        "schema_version": 1, "id": f"N-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{short_id(2)}",
        "name": args.get("name"), "project_dir": str(store.project_dir), "created_at": now_iso(),
        "jobs": jobs, "expected": expected, "sealed": False, "when": when, "queue": queue, "deadline": deadline,
        "early_failure": bool(args.get("early_failure", True)), "early_sent": [], "unknown_sent": [],
        "deliver": delivery.validate_shape(args.get("deliver")),
        "notifier": check_notifier(args.get("notifier") or default_notifier(settings)),
        "message": args.get("message") or "", "labels": args.get("labels") or {}, "status": "open",
        "ack_token": short_id(3), "outbox": [], "fired": None, "history": [{"at": now_iso(), "status": "open"}],
    }
    _seal_if_complete(item, explicit=seal_now or bool(queue))
    write_json(_path(store.state, item["id"]), item)
    return item


def _seal_if_complete(item: dict, explicit: bool = False) -> None:
    if explicit or (item.get("expected") and len(item["jobs"]) >= item["expected"]):
        item["sealed"] = True


def _ledger_ids(store: Store, job_ids: list[str]) -> list[str]:
    full = []
    for job_id in job_ids:
        record = store.find(job_id)
        if record is None:
            raise MiyabiError("not_in_ledger", f"job {job_id} is not in this project's ledger; adopt it first")
        full.append(record["job_id"])
    return full


def _note_on_jobs(store: Store, item: dict, job_ids: list[str]) -> None:
    for job_id in job_ids:
        store.update(job_id, lambda record: record.setdefault("notifications", []).append(item["id"]))


def create(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    if not (args.get("job_ids") or args.get("queue") or args.get("name") or args.get("timeout")):
        raise MiyabiError("invalid_arguments", "give job_ids, a queue, a timeout, or a name to fill later")
    with store.lock("notify-names"):  # the name check and the creation together
        existing = _named(store.state, args.get("name"), "open")
        if existing:
            raise MiyabiError("exists", f"notification {args['name']} is already open ({existing['id']}); use "
                              "notify_update to add jobs")
        jobs = _ledger_ids(store, list(args.get("job_ids") or []))
        # An explicit job list is complete unless `expected` says more jobs will join.
        item = _new(settings, store, args, jobs, seal_now=bool(jobs) and args.get("expected") is None)
    _note_on_jobs(store, item, jobs)
    return ok(notification=_view(item), next=_next_step(item, created=True),
              daemon=daemon_info(settings, item["notifier"], item["id"],
                                 open_count=_open_count(store, item["notifier"])))


def join(settings: Settings, store: Store, spec: dict, job_id: str) -> dict:
    """Add a just-submitted job to the named notification, creating it on first use (job_submit notify=)."""
    if not isinstance(spec, dict):
        raise MiyabiError("invalid_arguments", "notify must be an object")
    with store.lock("notify-names"):
        item = _named(store.state, spec.get("name"), "open")
        created = item is None
        if created:
            # Without a name no other job can join: a notification for this job alone, sealed at once.
            item = _new(settings, store, {**spec, "job_ids": None}, [job_id],
                        seal_now=not spec.get("name") and spec.get("expected") is None)
        else:
            if item["sealed"] and job_id not in item["jobs"]:
                raise MiyabiError("sealed", f"notification {spec['name']} is sealed ({len(item['jobs'])} jobs); "
                                  "use a new name")

            def add(current: dict) -> None:  # under the notification's lock: it may have changed meanwhile
                if job_id in current["jobs"]:
                    return  # joined before: a retry
                if current["status"] != "open" or current["sealed"]:
                    raise MiyabiError("sealed", f"notification {spec['name']} was sealed or fired meanwhile "
                                      f"({len(current['jobs'])} jobs); use a new name")
                current["jobs"].append(job_id)
                _seal_if_complete(current)

            item = _update(store, item["id"], add)
    _note_on_jobs(store, item, [job_id])
    return {"notification": _view(item), "created": created, "next": _next_step(item, created),
            "daemon": daemon_info(settings, item["notifier"], item["id"],
                                  open_count=_open_count(store, item["notifier"]))}


def update(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    item = _load(store, args.get("id") or args.get("name") or "")
    added = _ledger_ids(store, list(args.get("add_job_ids") or []))

    def change(current: dict) -> None:
        if current["status"] != "open":
            raise MiyabiError("invalid_state", f"notification is {current['status']}: only an open one takes jobs "
                              "or changes; create a new one")
        if added and current["sealed"]:
            raise MiyabiError("sealed", "the notification is sealed")
        current["jobs"].extend(j for j in added if j not in current["jobs"])
        _seal_if_complete(current, explicit=bool(args.get("seal")))
        if args.get("message") is not None:
            current["message"] = args["message"]

    item = _update(store, item["id"], change)
    _note_on_jobs(store, item, added)
    return ok(notification=_view(item), daemon=daemon_info(settings, item["notifier"], item["id"],
                                                           open_count=_open_count(store, item["notifier"])))


def _view(item: dict) -> dict:
    return {k: item.get(k) for k in ("id", "name", "status", "sealed", "jobs", "expected", "when", "queue",
                                     "deadline", "early_failure", "notifier", "deliver", "message", "created_at")}


def list_(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    everything = _all(store.state)
    wanted = set(args.get("status") or [])
    items = [n for n in everything if not wanted or n["status"] in wanted]
    open_by_notifier = Counter(n["notifier"] for n in everything if n["status"] in OPEN)
    notifiers = sorted({n["notifier"] for n in items} | set(open_by_notifier))
    daemons = [daemon_info(settings, name, open_count=open_by_notifier.get(name, 0)) for name in notifiers]
    views = []
    for n in items:
        view = {**_view(n), "pending_messages": sum(1 for m in n["outbox"] if m["status"] == "pending"),
                "fired_reason": (n.get("fired") or {}).get("reason")}
        if n["status"] in OPEN and n["deliver"]["method"] == "listener":
            view["listen_command"] = listen_command(settings, n["notifier"], n["id"])
        if n["status"] in CLOSED:
            view["closed_at"] = _closed_at(n)
        views.append(view)
    summary = {"open": sum(open_by_notifier.values()),
               "delivered_unacknowledged": sum(1 for n in everything if n["status"] == "delivered"),
               "daemons": {d["notifier"]: d["state"] for d in daemons}}
    return ok(count=len(items), summary=summary, notifications=views, daemons=daemons)


def cancel(settings: Settings, args: dict) -> dict:
    """Cancel an open notification, or dismiss a delivered one that was never acknowledged (no token needed;
    dismissal is not an acknowledgment)."""
    store = Store(settings)
    item = _load(store, args.get("id") or "")

    def change(current: dict) -> None:
        if current["status"] in OPEN:
            _transition(current, "cancelled", cancellation_reason=args.get("reason"))
        elif current["status"] == "delivered":
            _transition(current, "dismissed", dismissal_reason=args.get("reason"))
        else:
            raise MiyabiError("invalid_state", f"notification is {current['status']}; only open, fired or delivered "
                              "notifications can be cancelled or dismissed")

    return ok(notification=_view(_update(store, item["id"], change)))


def ack(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    item = _load(store, args.get("id") or "")

    def change(current: dict) -> None:
        if args.get("ack_token") != current["ack_token"]:
            raise MiyabiError("invalid_arguments", "ack_token does not match the one in the notification")
        if current["status"] not in {"fired", "delivered", "acknowledged", "dismissed"}:
            raise MiyabiError("invalid_state", f"notification is {current['status']}; nothing was sent yet")
        if current["status"] != "acknowledged":
            _transition(current, "acknowledged", acknowledged_at=now_iso())

    item = _update(store, item["id"], change)
    return ok(id=item["id"], status=item["status"], acknowledged_at=item["acknowledged_at"])


# -- evaluation ---------------------------------------------------------------------------------------------------

def _summary(record: dict) -> dict:
    state = record.get("state") or {}
    submit = record.get("submit") or {}
    return {"job_id": record.get("job_id"), "name": record.get("name"), "execution_state": state.get("execution_state"),
            "outcome": state.get("outcome"), "raw_state": state.get("raw_state"),
            "requested_walltime_s": submit.get("walltime_s"), "usage": record.get("usage"),
            "output": submit.get("output")}


def _met(when: str, states: list[str]) -> bool:
    """Only `ended` ends a job: a job PBS has lost track of (unknown) is neither ended nor started."""
    if when == "all_ended":
        return all(s == "ended" for s in states)
    if when == "any_ended":
        return any(s == "ended" for s in states)
    return all(s in {"running", "ended"} for s in states)


def _line(item: dict) -> str:
    usage = item.get("usage") or {}
    used = usage.get("source_ref", "").split("=", 1)[-1].split(" x ")[0] if usage else "-"
    requested = seconds_to_hms(item["requested_walltime_s"]) if item.get("requested_walltime_s") else "-"
    amount = f"{usage['quantity']} {usage['unit']}" if usage else "no usage recorded"
    return (f"- {item['job_id']} {item.get('name')}  {item.get('raw_state')} ({item.get('outcome')})  "
            f"walltime {used}/{requested}  {amount}  log {item.get('output')}")


def render(item: dict, reason: str, summary: list[dict], kind: str, export: str | None = None) -> str:
    """The message text. Only the final message carries the ack token: acknowledging an early-failure or
    state-unknown message would close the group before its final message is delivered."""
    label = f"{item['id']} ({item['name']})" if item.get("name") else item["id"]
    head = {"early": "early failure in", "unknown": "job state unknown in"}.get(kind, "notification")
    lines = [f"[miyabi-mcp] {head} {label}: {reason} ({now().strftime('%H:%M %Z')})"]
    lines += [_line(s) if "job_id" in s else f"- queue {s['queue']}: {s['status']}" for s in summary]
    if item.get("jobs"):
        ids = item["jobs"] if kind == "final" else [s["job_id"] for s in summary]
        lines.append("PBS exit status is not application success: check the outputs before continuing.")
        lines.append(f"Details: job_get(job_ids={compact(ids)}, tail_lines=50).")
    if kind == "unknown":
        lines.append("PBS lists this job neither as active nor in its history: check it with job_get. The ledger "
                     "keeps watching it and the group counts it as ended only once PBS reports it ended.")
    if kind in NOTICES:
        lines.append("The group notification is still waiting; this message needs no acknowledgment."
                     + (" To keep listening run its listen_command again." if item["deliver"]["method"] == "listener"
                        else ""))
    else:
        if item.get("message"):
            lines.append(f"Your note: {item['message']}")
        lines.append(f'Acknowledge: notify_ack(id="{item["id"]}", ack_token="{item["ack_token"]}")')
    if export and kind == "final":
        lines.append(f"Ledger export (smooth-research JSONL): {export}")
    return "\n".join(lines)


def _evaluate(settings: Settings, item: dict, records: dict) -> list[tuple]:
    """New outbox messages for one open notification: the final one, or else one-off notices."""
    deadline_hit = bool(item.get("deadline")) and now() >= datetime.fromisoformat(item["deadline"])
    if item.get("queue"):
        name = item["queue"]["queue"]
        data = observe.queues(settings, max_age=int(settings.config.get("poll_max_age_s", 60)))
        leaves = [q for q in data["queues"] if (q["name"] == name or q.get("parent") == name) and not q.get("routing")]
        summary = [{"queue": q["name"], "status": q.get("status")} for q in leaves]
        if leaves and all(q.get("enabled") and q.get("started") for q in leaves):
            return [("final", f"queue {name} is enabled and started", summary)]
        return [("final", "timeout", summary)] if deadline_hit else []
    summary = [_summary(records[j]) for j in item["jobs"] if j in records]
    states = [s["execution_state"] for s in summary]
    reason = None
    if item["sealed"] and summary and _met(item["when"], states):
        ended = sum(s == "ended" for s in states)
        reason = {"all_ended": f"all {len(states)} jobs ended",
                  "any_ended": f"{ended} of {len(states)} jobs ended",
                  "started": "all jobs started" if not ended else f"all {len(states)} jobs started or ended"}[item["when"]]
    if reason is None and deadline_hit:
        reason = "timeout" + ("" if item["sealed"] else " (the group was never sealed)")
    if reason:
        return [("final", reason, summary)]  # it lists every member: no separate notices
    messages = []
    for s in summary:
        if s["execution_state"] == "unknown" and s["job_id"] not in item.get("unknown_sent", []):
            messages.append(("unknown", f"job {s['job_id']} is neither active nor in PBS history", [s]))
        elif item["early_failure"] and s["outcome"] == "failed" and s["job_id"] not in item["early_sent"]:
            messages.append(("early", f"job {s['job_id']} failed", [s]))
    return messages


def _projects(settings: Settings) -> list[Path]:
    registry = read_json(settings.user_dir / "projects.json", {}) or {}
    paths = {Path(p) for p in (registry.get("projects") or {})}
    if settings.project_dir:
        paths.add(settings.project_dir)
    return sorted(p for p in paths if (p / STATE_NAME / "notifications").is_dir())


def poll(settings: Settings, notifier: str, caller: str | None = None, *, interval: int | None = None,
         instance: str | None = None, exiting: str | None = None) -> dict:
    """Bring this notifier's notifications up to date; return the messages it should deliver.

    Open notifications are counted before their jobs are refreshed, so a project whose refresh fails still
    counts as having work, and its error is returned. `exiting` is a daemon's exit report: the exit is
    recorded first, and for an idle exit the usual evaluation follows, so a notification registered during
    the daemon's last poll is seen by it (it resumes) or by the daemon the agent starts next.
    """
    check_notifier(notifier)
    started = now_iso()
    if exiting:
        _beat_update(settings, notifier, lambda beat: beat.update(
            exited={"at": started, "reason": exiting, "instance": instance}))
        if exiting != "idle":
            return ok(notifier=notifier, observed_at=started, exited=exiting)
    max_age = int(settings.config.get("poll_max_age_s", 60))
    horizon = now() - timedelta(seconds=CLOSED_WINDOW_S)
    messages, open_count, targets, closed, errors = [], 0, {}, {}, []
    for project in _projects(settings):
        try:
            store = Store(settings.with_project(str(project)))
            mine = [n for n in _all(store.state) if n.get("notifier") == notifier]
        except MiyabiError as exc:
            errors.append({"project": str(project), **exc.to_dict()})
            continue
        for item in mine:
            if item["status"] in CLOSED and datetime.fromisoformat(_closed_at(item)) >= horizon:
                closed[item["id"]] = {"status": item["status"], "at": _closed_at(item),
                                      "delivered_at": _reached(item, "delivered")}
        items = [n for n in mine if n["status"] in OPEN]
        for item in items:
            open_count += 1
            targets[item["id"]] = item["deliver"]
        if not items:
            continue
        try:
            records = {}
            members = sorted({j for n in items for j in n["jobs"]})
            if members:
                found, _snap = observe.refresh_jobs(store, members, max_age)
                records = {r["job_id"]: r for r in found if r}
            for item in items:
                if item["status"] == "open":
                    for kind, reason, summary in _evaluate(settings, item, records):
                        _queue_message(store, item["id"], kind, reason, summary)
        except MiyabiError as exc:
            errors.append({"project": str(project), **exc.to_dict()})
        for item in items:
            current = read_json(_path(store.state, item["id"])) or item
            if current["status"] not in OPEN:
                continue
            for message in current["outbox"]:
                if message["status"] == "pending":
                    messages.append({"msg_id": message["msg_id"], "notification_id": current["id"],
                                     "project_dir": str(project), "kind": message["kind"],
                                     "message": message["message"], "deliver": current["deliver"]})
    if not exiting:
        brief = [{"project": e["project"], "type": e.get("type"), "message": str(e.get("message"))[:300]}
                 for e in errors]

        def record(beat: dict) -> None:
            beat.update(notifier=notifier, caller=caller, observer=host(), instance=instance, last_poll=started,
                        interval_s=interval, errors=brief)
            if not errors:
                beat["last_ok"] = now_iso()
            beat.pop("exited", None)

        _beat_update(settings, notifier, record)
    return ok(notifier=notifier, observed_at=now_iso(), open_count=open_count, messages=messages, targets=targets,
              closed=closed, poll_seconds=poll_seconds(settings), errors=errors)


def _queue_message(store: Store, notification_id: str, kind: str, reason: str, summary: list[dict]) -> None:
    def change(item: dict) -> None:
        if item["status"] != "open":
            return
        if kind in NOTICES:
            sent = item.setdefault(NOTICES[kind], [])
            if summary[0]["job_id"] in sent:
                return
            sent.append(summary[0]["job_id"])
        else:
            _transition(item, "fired", fired={"at": now_iso(), "reason": reason, "summary": summary})
        number = len(item["outbox"]) + 1
        item["outbox"].append({"msg_id": f"{item['id']}:{number}", "kind": kind, "created_at": now_iso(),
                               "message": render(item, reason, summary, kind,
                                                 export_command(store.settings, store.project_dir,
                                                                item.get("notifier"))),
                               "status": "pending", "attempts": 0})

    _update(store, notification_id, change)


def report(settings: Settings, msg_id: str, outcome: dict) -> dict:
    """Record a delivery attempt from the daemon: delivered, retry, uncertain or failed."""
    notification_id = msg_id.rsplit(":", 1)[0]
    store = Store(settings)
    status = outcome.get("status")

    def change(item: dict) -> None:
        message = next((m for m in item["outbox"] if m["msg_id"] == msg_id), None)
        if message is None:
            raise MiyabiError("not_found", f"no message {msg_id}")
        message["attempts"] += 1
        message["last_outcome"] = {"at": now_iso(), **outcome}
        if status in {"delivered", "uncertain"}:
            message["status"] = status
        elif status == "failed" or message["attempts"] >= MAX_ATTEMPTS:
            message["status"] = "failed"
        final = [m for m in item["outbox"] if m["kind"] == "final"]
        if final and final[-1]["status"] in {"delivered", "uncertain"} and item["status"] == "fired":
            _transition(item, "delivered")
        elif final and final[-1]["status"] == "failed" and item["status"] == "fired":
            _transition(item, "failed")

    item = _update(store, notification_id, change)
    return ok(id=item["id"], status=item["status"])


def check_spec(store: Store, spec: dict) -> tuple[str, str]:
    """A dry-run check of a normalized notify argument against the group's current state: (status, detail)."""
    if spec.get("name"):
        current = _named(store.state, spec["name"], "open")
        if current and current["sealed"]:
            return "fail", f"notification {spec['name']} is sealed; use a new name"
        if current:
            return "pass", f"joins {current['id']} ({len(current['jobs'])} jobs so far, expected {current['expected']})"
        return "pass", f"creates notification {spec['name']}" + (
            f" sealed after {spec['expected']} jobs" if spec.get("expected") else " (seal it with notify_update)")
    return "pass", "a notification for this job alone"


# -- job_wait -----------------------------------------------------------------------------------------------------

def _closing_hint(item: dict) -> str:
    status = item["status"]
    if status == "open":
        when = item["when"].replace("_", " ")
        if item["sealed"]:
            text = f"waiting until {when} for its {len(item['jobs'])} jobs; it fires by itself at a daemon poll"
        else:
            expected = item.get("expected") or "?"
            text = (f"unsealed ({len(item['jobs'])} of {expected} jobs): it fires on {when} only once sealed "
                    "(notify_update seal=true) or when `expected` jobs have joined")
        if item.get("deadline"):
            text += f"; it fires anyway at its timeout {item['deadline']}"
        for key, what in (("early_sent", "an early-failure message"), ("unknown_sent", "a state-unknown message")):
            if item.get(key):
                text += f"; {what} was already sent for {', '.join(item[key])}"
        return text + f". Not needed any more? notify_cancel(id=\"{item['id']}\")"
    if status == "fired":
        return (f"fired; the daemon of {item['notifier']} delivers it at its next poll. Already handled? "
                f"notify_cancel(id=\"{item['id']}\")")
    if status == "delivered":
        return (f"delivered to {item['notifier']} but not acknowledged: acknowledge it with the ack_token in its "
                f"message, or close it without the token with notify_cancel(id=\"{item['id']}\") (recorded as "
                "dismissed, not as an acknowledgment)")
    if status == "failed":
        return "its delivery failed; see notify_list for the reason"
    return "closed; nothing to do"


def _wait_notifications(store: Store, records: list[dict]) -> list[dict]:
    ids = list(dict.fromkeys(n for record in records for n in (record.get("notifications") or [])))
    result = []
    for notification_id in ids:
        item = read_json(_path(store.state, notification_id))
        if item:
            result.append({"id": item["id"], "name": item.get("name"), "status": item["status"],
                           "jobs": item["jobs"], "sealed": item["sealed"], "next": _closing_hint(item)})
    return result


def job_wait(settings: Settings, args: dict, cancelled=lambda: False, progress=None) -> dict:
    """Bounded wait inside one tool call, for jobs of a few minutes."""
    store = Store(settings)
    job_ids = list(args.get("job_ids") or [])
    when = args.get("when", "all_ended")
    if not job_ids or when not in WHEN:
        raise MiyabiError("invalid_arguments", f"give job_ids and when in {sorted(WHEN)}")
    limit = float(settings.config.get("job_wait_max_s", 45))
    timeout = min(float(args.get("timeout_s") or limit), limit)
    start = time.monotonic()
    while True:
        records, _snap = observe.refresh_jobs(store, job_ids, max_age=5)
        summary = [_summary(record) for record in records]
        states = [item["execution_state"] for item in summary]
        met = _met(when, states)
        elapsed = time.monotonic() - start
        if met or elapsed >= timeout or cancelled():
            unknown = [s["job_id"] for s in summary if s["execution_state"] == "unknown"]
            steps = []
            if unknown:
                steps.append(f"state unknown for {', '.join(unknown)}: PBS lists them neither as active nor in its "
                             "history; check them with job_get (they do not count as ended)")
            if not met:
                steps.append("not yet: for a long wait submit with notify and run its command")
            result = ok(met=met, waited_s=round(elapsed, 1), jobs=summary, next="; ".join(steps) or None)
            notifications = _wait_notifications(store, records)
            if notifications:
                result["notifications"] = notifications
            return result
        if progress:
            progress(elapsed, timeout)
        time.sleep(min(10.0, max(0.5, timeout - elapsed)))
