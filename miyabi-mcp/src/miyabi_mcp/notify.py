"""Notifications: job groups, sealing, conditions and an outbox the agent-side daemon delivers (DESIGN.md 9).

The login node decides (shared qstat snapshot + ledger); a daemon on the agent's machine polls with
`miyabi-mcp notify poll --notifier <name>`, delivers locally and reports back with `notify report`.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

from . import deliver as delivery, observe
from .config import LAUNCHER, STATE_NAME, Settings
from .ledger import Store, host
from .util import MiyabiError, compact, now, now_iso, ok, parse_duration, read_json, seconds_to_hms, short_id, write_json

WHEN = {"all_ended", "any_ended", "started"}
SETTLED = {"ended", "unknown"}
OPEN = {"open", "fired"}
MAX_ATTEMPTS = 5


# -- storage ----------------------------------------------------------------------------------------------------

def _path(state: Path, notification_id: str) -> Path:
    return state / "notifications" / f"{notification_id}.json"


def _load(store: Store, key: str) -> dict:
    """A notification by id, or by name among this project's open ones."""
    found = read_json(_path(store.state, key)) if key.startswith("N-") else None
    if found is None:
        for item in _all(store.state):
            if item.get("name") == key and item["status"] in OPEN:
                found = item
                break
    if found is None:
        raise MiyabiError("not_found", f"no notification {key} in this project")
    return found


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


# -- the agent-side daemon: names, commands, heartbeat ------------------------------------------------------------

def default_notifier(settings: Settings) -> str:
    return settings.config.get("notifier") or host()


def export_command(settings: Settings, project_dir: Path | str) -> str:
    """A command for the agent's machine that prints this project's smooth-research JSONL (data never passes
    through the model)."""
    local = f"{LAUNCHER} --project-dir {project_dir} ledger --format smooth-research --jsonl"
    notifier = settings.config.get("notifier")
    if not notifier or notifier == host():
        return local
    return f"ssh -T -o BatchMode=yes {settings.config.get('remote_alias', 'miyabi-g')} {local}"


def daemon_info(settings: Settings, notifier: str, notification_id: str | None = None) -> dict:
    beat = read_json(settings.user_dir / "notifiers" / f"{notifier}.json") or {}
    poll = int(settings.config.get("notify_poll_seconds", 300))
    last = beat.get("last_poll")
    alive = bool(last) and (now() - datetime.fromisoformat(last)).total_seconds() <= 2 * poll + 60
    local = notifier == host()
    launcher = str(LAUNCHER) if local else settings.config.get("local_launcher", "miyabi-mcp")
    remote = "" if local else (f" --remote {settings.config.get('remote_alias', 'miyabi-g')}"
                               f" --remote-launcher {LAUNCHER}")
    info = {"notifier": notifier, "last_poll": last, "daemon_running": alive, "poll_seconds": poll,
            "start_command": f"{launcher} notifyd ensure --notifier {notifier}{remote}"}
    if notification_id:
        info["listen_command"] = f"{launcher} notify listen --id {notification_id} --notifier {notifier}{remote}"
    if not alive:
        info["hint"] = "no daemon has polled for this notifier recently: run start_command on the agent's machine"
    return info


# -- create, join, update -----------------------------------------------------------------------------------------

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
        "early_failure": bool(args.get("early_failure", True)), "early_sent": [],
        "deliver": delivery.validate_shape(args.get("deliver")),
        "notifier": args.get("notifier") or default_notifier(settings),
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
    if args.get("name"):
        try:
            existing = _load(store, args["name"])
        except MiyabiError:
            existing = None
        if existing:
            raise MiyabiError("exists", f"notification {args['name']} is already open ({existing['id']}); use "
                              "notify_update to add jobs")
    with store.lock("notify-names"):
        jobs = _ledger_ids(store, list(args.get("job_ids") or []))
        # An explicit job list is complete unless `expected` says more jobs will join.
        item = _new(settings, store, args, jobs, seal_now=bool(jobs) and args.get("expected") is None)
    _note_on_jobs(store, item, jobs)
    return ok(notification=_view(item), daemon=daemon_info(settings, item["notifier"], item["id"]))


def join(settings: Settings, store: Store, spec: dict, job_id: str) -> dict:
    """Add a just-submitted job to the named notification, creating it on first use (job_submit notify=)."""
    if not isinstance(spec, dict):
        raise MiyabiError("invalid_arguments", "notify must be an object")
    with store.lock("notify-names"):
        item = None
        if spec.get("name"):
            item = next((n for n in _all(store.state) if n.get("name") == spec["name"] and n["status"] in OPEN), None)
        if item is None:
            # Without a name no other job can join: a notification for this job alone, sealed at once.
            item = _new(settings, store, {**spec, "job_ids": None}, [job_id],
                        seal_now=not spec.get("name") and spec.get("expected") is None)
        else:
            if item["sealed"]:
                raise MiyabiError("sealed", f"notification {spec['name']} is sealed ({len(item['jobs'])} jobs); "
                                  "use a new name")

            def add(current: dict) -> None:
                current["jobs"].append(job_id)
                _seal_if_complete(current)

            item = _update(store, item["id"], add)
    _note_on_jobs(store, item, [job_id])
    return {"notification": _view(item), "daemon": daemon_info(settings, item["notifier"], item["id"])}


def update(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    item = _load(store, args.get("id") or args.get("name") or "")
    added = _ledger_ids(store, list(args.get("add_job_ids") or []))

    def change(current: dict) -> None:
        if current["status"] not in OPEN:
            raise MiyabiError("invalid_state", f"notification is {current['status']}")
        if added and current["sealed"]:
            raise MiyabiError("sealed", "the notification is sealed")
        current["jobs"].extend(j for j in added if j not in current["jobs"])
        _seal_if_complete(current, explicit=bool(args.get("seal")))
        if args.get("message") is not None:
            current["message"] = args["message"]

    item = _update(store, item["id"], change)
    _note_on_jobs(store, item, added)
    return ok(notification=_view(item), daemon=daemon_info(settings, item["notifier"], item["id"]))


def _view(item: dict) -> dict:
    return {k: item.get(k) for k in ("id", "name", "status", "sealed", "jobs", "expected", "when", "queue",
                                     "deadline", "early_failure", "notifier", "deliver", "message", "created_at")}


def list_(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    wanted = set(args.get("status") or [])
    items = [n for n in _all(store.state) if not wanted or n["status"] in wanted]
    notifiers = {n["notifier"] for n in items if n["status"] in OPEN}
    return ok(count=len(items), notifications=[{**_view(n), "pending_messages": sum(
        1 for m in n["outbox"] if m["status"] == "pending"), "fired_reason": (n.get("fired") or {}).get("reason")}
        for n in items], daemons=[daemon_info(settings, name) for name in sorted(notifiers)])


def cancel(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    item = _load(store, args.get("id") or "")

    def change(current: dict) -> None:
        if current["status"] not in OPEN:
            raise MiyabiError("invalid_state", f"notification is {current['status']}; a delivery cannot be retracted")
        current.update(status="cancelled", cancellation_reason=args.get("reason"),
                       history=current["history"] + [{"at": now_iso(), "status": "cancelled"}])

    return ok(notification=_view(_update(store, item["id"], change)))


def ack(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    item = _load(store, args.get("id") or "")

    def change(current: dict) -> None:
        if args.get("ack_token") != current["ack_token"]:
            raise MiyabiError("invalid_arguments", "ack_token does not match the one in the notification")
        if current["status"] not in {"fired", "delivered", "acknowledged"}:
            raise MiyabiError("invalid_state", f"notification is {current['status']}; nothing was sent yet")
        current.setdefault("acknowledged_at", now_iso())
        current["status"] = "acknowledged"
        current["history"] = current["history"] + [{"at": now_iso(), "status": "acknowledged"}]

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


def _line(item: dict) -> str:
    usage = item.get("usage") or {}
    used = usage.get("source_ref", "").split("=", 1)[-1].split(" x ")[0] if usage else "-"
    requested = seconds_to_hms(item["requested_walltime_s"]) if item.get("requested_walltime_s") else "-"
    amount = f"{usage['quantity']} {usage['unit']}" if usage else "no usage recorded"
    return (f"- {item['job_id']} {item.get('name')}  {item.get('raw_state')} ({item.get('outcome')})  "
            f"walltime {used}/{requested}  {amount}  log {item.get('output')}")


def render(item: dict, reason: str, summary: list[dict], kind: str, export: str | None = None) -> str:
    label = f"{item['id']} ({item['name']})" if item.get("name") else item["id"]
    head = "early failure in" if kind == "early" else "notification"
    lines = [f"[miyabi-mcp] {head} {label}: {reason} ({now().strftime('%H:%M %Z')})"]
    lines += [_line(s) if "job_id" in s else f"- queue {s['queue']}: {s['status']}" for s in summary]
    if item.get("jobs"):
        lines.append("PBS exit status is not application success: check the outputs before continuing.")
        lines.append(f"Details: job_get(job_ids={compact(item['jobs'])}, tail_lines=50).")
    if kind == "early":
        lines.append("The group notification is still pending; to keep listening run its listen_command again.")
    elif item.get("message"):
        lines.append(f"Your note: {item['message']}")
    lines.append(f'Acknowledge: notify_ack(id="{item["id"]}", ack_token="{item["ack_token"]}")')
    if export and kind == "final":
        lines.append(f"Ledger export (smooth-research JSONL): {export}")
    return "\n".join(lines)


def _evaluate(settings: Settings, store: Store, item: dict, records: dict) -> list[dict]:
    """New outbox messages for one notification (early failures and the final one)."""
    messages = []
    deadline_hit = bool(item.get("deadline")) and now() >= datetime.fromisoformat(item["deadline"])
    if item.get("queue"):
        name = item["queue"]["queue"]
        data = observe.queues(settings, max_age=int(settings.config.get("poll_max_age_s", 60)))
        leaves = [q for q in data["queues"] if (q["name"] == name or q.get("parent") == name) and not q.get("routing")]
        summary = [{"queue": q["name"], "status": q.get("status")} for q in leaves]
        if leaves and all(q.get("enabled") and q.get("started") for q in leaves):
            messages.append(("final", f"queue {name} is enabled and started", summary))
        elif deadline_hit:
            messages.append(("final", "timeout", summary))
        return messages
    summary = [_summary(records[j]) for j in item["jobs"] if j in records]
    states = [s["execution_state"] for s in summary]
    if item["early_failure"]:
        for s in summary:
            if s["outcome"] == "failed" and s["job_id"] not in item["early_sent"] and not all(x in SETTLED for x in states):
                messages.append(("early", f"job {s['job_id']} failed", [s]))
    reason = None
    if item["sealed"] and summary:
        when = item["when"]
        if when == "all_ended" and all(x in SETTLED for x in states):
            reason = f"all {len(states)} jobs ended"
        elif when == "any_ended" and any(x in SETTLED for x in states):
            reason = f"{sum(x in SETTLED for x in states)} of {len(states)} jobs ended"
        elif when == "started" and all(x in SETTLED | {"running"} for x in states):
            reason = "all jobs started"
    if reason is None and deadline_hit:
        reason = "timeout" + ("" if item["sealed"] else " (the group was never sealed)")
    if reason:
        messages.append(("final", reason, summary))
    return messages


def _projects(settings: Settings) -> list[Path]:
    registry = read_json(settings.user_dir / "projects.json", {}) or {}
    paths = {Path(p) for p in (registry.get("projects") or {})}
    if settings.project_dir:
        paths.add(settings.project_dir)
    return sorted(p for p in paths if (p / STATE_NAME / "notifications").is_dir())


def poll(settings: Settings, notifier: str, caller: str | None = None) -> dict:
    """Bring this notifier's notifications up to date; return the messages it should deliver."""
    write_json(settings.user_dir / "notifiers" / f"{notifier}.json",
               {"notifier": notifier, "last_poll": now_iso(), "caller": caller, "observer": host()})
    max_age = int(settings.config.get("poll_max_age_s", 60))
    messages, open_count, targets, errors = [], 0, {}, []
    for project in _projects(settings):
        try:
            store = Store(settings.with_project(str(project)))
            items = [n for n in _all(store.state) if n.get("notifier") == notifier and n["status"] in OPEN]
            if not items:
                continue
            members = sorted({j for n in items for j in n["jobs"]})
            records = {}
            if members:
                found, _snap = observe.refresh_jobs(store, members, max_age)
                records = {r["job_id"]: r for r in found if r}
            for item in items:
                if item["status"] == "open":
                    for kind, reason, summary in _evaluate(settings, store, item, records):
                        _queue_message(store, item["id"], kind, reason, summary)
                current = read_json(_path(store.state, item["id"]))
                if current["status"] in OPEN:
                    open_count += 1
                    targets[current["id"]] = current["deliver"]
                for message in current["outbox"]:
                    if message["status"] == "pending":
                        messages.append({"msg_id": message["msg_id"], "notification_id": current["id"],
                                         "project_dir": str(project), "kind": message["kind"],
                                         "message": message["message"], "deliver": current["deliver"]})
        except MiyabiError as exc:
            errors.append({"project": str(project), **exc.to_dict()})
    return ok(notifier=notifier, observed_at=now_iso(), open_count=open_count, messages=messages, targets=targets,
              poll_seconds=int(settings.config.get("notify_poll_seconds", 300)), errors=errors)


def _queue_message(store: Store, notification_id: str, kind: str, reason: str, summary: list[dict]) -> None:
    def change(item: dict) -> None:
        if kind == "early":
            job_id = summary[0]["job_id"]
            if job_id in item["early_sent"]:
                return
            item["early_sent"].append(job_id)
        elif item["status"] != "open":
            return
        else:
            item.update(status="fired", fired={"at": now_iso(), "reason": reason, "summary": summary},
                        history=item["history"] + [{"at": now_iso(), "status": "fired"}])
        number = len(item["outbox"]) + 1
        item["outbox"].append({"msg_id": f"{item['id']}:{number}", "kind": kind, "created_at": now_iso(),
                               "message": render(item, reason, summary, kind,
                                                 export_command(store.settings, store.project_dir)),
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
            item.update(status="delivered", history=item["history"] + [{"at": now_iso(), "status": "delivered"}])
        elif final and final[-1]["status"] == "failed" and item["status"] == "fired":
            item.update(status="failed", history=item["history"] + [{"at": now_iso(), "status": "failed"}])

    item = _update(store, notification_id, change)
    return ok(id=item["id"], status=item["status"])


def check_spec(store: Store, spec: dict) -> tuple[str, str]:
    """A dry-run check of job_submit's notify argument: (status, detail)."""
    if not isinstance(spec, dict):
        return "fail", "notify must be an object"
    try:
        delivery.validate_shape(spec.get("deliver"))
        if spec.get("when", "all_ended") not in WHEN:
            raise MiyabiError("invalid_arguments", f"notify.when must be one of {sorted(WHEN)}")
    except MiyabiError as exc:
        return "fail", exc.message
    if spec.get("name"):
        current = next((n for n in _all(store.state) if n.get("name") == spec["name"] and n["status"] in OPEN), None)
        if current and current["sealed"]:
            return "fail", f"notification {spec['name']} is sealed; use a new name"
        if current:
            return "pass", f"joins {current['id']} ({len(current['jobs'])} jobs so far, expected {current['expected']})"
        return "pass", f"creates notification {spec['name']}" + (
            f" sealed after {spec['expected']} jobs" if spec.get("expected") else " (seal it with notify_update)")
    return "pass", "a notification for this job alone"


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
        met = {"all_ended": all(s in SETTLED for s in states), "any_ended": any(s in SETTLED for s in states),
               "started": all(s in SETTLED | {"running"} for s in states)}[when]
        elapsed = time.monotonic() - start
        if met or elapsed >= timeout or cancelled():
            return ok(met=met, waited_s=round(elapsed, 1), jobs=summary,
                      next=None if met else "not yet: for a long wait submit with notify and run its command")
        if progress:
            progress(elapsed, timeout)
        time.sleep(min(10.0, max(0.5, timeout - elapsed)))
