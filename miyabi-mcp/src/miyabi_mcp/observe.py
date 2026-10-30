"""The shared `qstat -ll` snapshot and turning PBS changes into ledger events (DESIGN.md 7.3, 9.1).

The snapshot is per user (qstat -ll lists all of the user's jobs) and refreshed by whoever finds it
too old, under a lock, so concurrent agents share one query. A change seen in the table is confirmed
with `qstat -f`; a job gone from the table is read back with `qstat -H -f`.
"""

from __future__ import annotations

import time
from datetime import datetime

from . import states
from .config import Settings
from .ledger import Store, host, job_num
from .pbs import Pbs
from .util import Lock, MiyabiError, now_iso, read_json, within, write_json

QUEUE_CACHE_S = 60


def _age(snapshot: dict) -> float:
    return round(max(0.0, time.time() - float(snapshot.get("observed_ts", 0))), 1)


def snapshot(settings: Settings, max_age: float, force: bool = False) -> dict:
    """The user's unfinished jobs, at most `max_age` seconds old (one shared `qstat -ll`)."""
    path = settings.user_dir / "cache" / "active.json"
    cached = read_json(path)
    if cached and not force and max_age > 0 and time.time() - float(cached.get("observed_ts", 0)) <= max_age:
        return {**cached, "cache_age_s": _age(cached)}
    lock = Lock(settings.user_dir / "locks" / "snapshot", lease=90, timeout=0)
    try:
        lock.acquire()
    except MiyabiError:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            time.sleep(0.5)
            fresh = read_json(path)
            if fresh and (not cached or fresh.get("observed_ts") != cached.get("observed_ts")):
                return {**fresh, "cache_age_s": _age(fresh)}
        lock = Lock(settings.user_dir / "locks" / "snapshot", lease=90, timeout=30).acquire()
    try:
        started, started_iso = time.time(), now_iso()  # the table reflects PBS at some time after this
        data = Pbs(settings).active()
        result = {"schema_version": 1, "observed_at": started_iso, "observed_ts": started, "observer": host(),
                  "jobs": data["jobs"], "scheduled_stop": data["scheduled_stop"]}
        write_json(path, result)
        return {**result, "cache_age_s": 0.0}
    finally:
        lock.release()


def queues(settings: Settings, max_age: float = QUEUE_CACHE_S) -> dict:
    """`qstat --rsc -x`, cached per user for `max_age` seconds."""
    path = settings.user_dir / "cache" / "queues.json"
    cached = read_json(path)
    if cached and _age(cached) <= max_age:
        return cached
    data = Pbs(settings).queues()
    result = {"observed_at": now_iso(), "observed_ts": time.time(), **data}
    write_json(path, result)
    return result


def _match(details: dict[str, dict], job_id: str) -> dict | None:
    if job_id in details:
        return details[job_id]
    number = job_num(job_id)
    for key, fields in details.items():
        if job_num(key) == number:
            return fields
    return None


def refresh(store: Store, records: list[dict], snap: dict) -> list[dict]:
    """Bring the given ledger records up to date with PBS; returns the new events."""
    settings = store.settings
    pbs = Pbs(settings)
    mig_hardware = settings.config.get("mig_hardware")
    grace = int(settings.config.get("submit_grace_s", 120))
    active = {job_num(row["job_id"]): row for row in snap["jobs"]}
    detail_ids: list[str] = []
    history_ids: list[str] = []
    for record in records:
        job_id = record.get("job_id")
        state = record.get("state") or {}
        if not job_id or state.get("execution_state") == "ended":
            continue
        row = active.get(job_num(job_id))
        if row is not None:
            seen = states.TABLE_CLASS.get(row["status"])
            if seen is None or seen != states.state_class(state):
                detail_ids.append(job_id)
        else:
            submit = record.get("submit") or {}
            # Only a table read that started after qsub returned is evidence that the job left it.
            if submit.get("accepted_ts") and float(snap.get("observed_ts", 0)) <= submit["accepted_ts"]:
                continue
            if grace and within(submit.get("submitted_at"), grace):
                continue
            history_ids.append(job_id)
    events: list[dict] = []
    if detail_ids:
        details = pbs.detail(detail_ids)
        for job_id in detail_ids:
            fields = _match(details, job_id)
            if fields is None:
                history_ids.append(job_id)
                continue
            observation = states.from_detail(fields, history=False, cancel_requested=False,
                                             mig_hardware=mig_hardware)
            event = store.apply(job_id, observation, "qstat -f")
            if event:
                events.append(event)
    if history_ids:
        details = pbs.detail(history_ids, history=True)
        for job_id in history_ids:
            fields = _match(details, job_id)
            record = store.find(job_id) or {}
            if fields is not None:
                observation = states.from_detail(fields, history=True,
                                                 cancel_requested=bool(record.get("cancel_requested")),
                                                 mig_hardware=mig_hardware)
            else:
                missing = _missing_since(store, job_id)
                if (datetime.now().astimezone() - missing).total_seconds() < float(settings.config.get("missing_s", 300)):
                    continue
                observation = {"kind": "unknown", "execution_state": "unknown", "outcome": "unknown",
                               "raw_state": "not in active table or history", "pbs_time": None}
            event = store.apply(job_id, observation, "qstat -H -f")
            if event:
                events.append(event)
    return events


def _missing_since(store: Store, job_id: str) -> datetime:
    """When the job was first found missing from both the table and history (recorded on first miss)."""
    holder: dict = {}

    def change(record: dict) -> None:
        record.setdefault("missing_since", now_iso())
        holder["at"] = record["missing_since"]

    store.update(job_id, change)
    return datetime.fromisoformat(holder["at"])


def refresh_jobs(store: Store, job_ids: list[str], max_age: float) -> tuple[list[dict], dict]:
    """Refresh the named ledger jobs; returns (records, snapshot)."""
    records = []
    for job_id in job_ids:
        record = store.find(job_id)
        if record is None:
            raise MiyabiError("not_in_ledger", f"job {job_id} is not in this project's ledger; adopt it with "
                              "job_annotate(adopt=true) first")
        records.append(record)
    snap = snapshot(store.settings, max_age)
    refresh(store, records, snap)
    return [store.find(job_id) or {} for job_id in job_ids], snap
