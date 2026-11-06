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


def accepted_after(records) -> float:
    """The latest qsub acceptance time among `records` (0 when none is known)."""
    return max([float((r.get("submit") or {}).get("accepted_ts") or 0) for r in records] or [0.0])


def _accepted_path(settings: Settings):
    return settings.user_dir / "cache" / "accepted.json"


def last_accepted(settings: Settings) -> float:
    """When qsub last accepted a job of this user through miyabi-mcp, in any project (0 when unknown)."""
    return float((read_json(_accepted_path(settings)) or {}).get("accepted_ts") or 0)


def mark_accepted(settings: Settings, accepted_ts: float) -> None:
    """Record a qsub acceptance (under the user's submit lock): later live checks need a newer table."""
    write_json(_accepted_path(settings), {"accepted_ts": max(accepted_ts, last_accepted(settings)),
                                          "at": now_iso()})


def snapshot(settings: Settings, max_age: float, force: bool = False, newer_than: float = 0.0) -> dict:
    """The user's unfinished jobs, at most `max_age` seconds old (one shared `qstat -ll`).

    A table read that started before `newer_than` (a qsub acceptance time) cannot show that job, so it
    is refreshed instead of being taken as evidence that the job is gone.
    """
    path = settings.user_dir / "cache" / "active.json"
    cached = read_json(path)
    fresh_enough = cached is not None and float(cached.get("observed_ts", 0)) > newer_than
    if cached and fresh_enough and not force and max_age > 0 \
            and time.time() - float(cached.get("observed_ts", 0)) <= max_age:
        return {**cached, "cache_age_s": _age(cached)}
    lock = Lock(settings.user_dir / "locks" / "snapshot", timeout=0)
    try:
        lock.acquire()
    except MiyabiError:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            time.sleep(0.5)
            fresh = read_json(path)
            if fresh and (not cached or fresh.get("observed_ts") != cached.get("observed_ts")) \
                    and float(fresh.get("observed_ts", 0)) > newer_than:
                return {**fresh, "cache_age_s": _age(fresh)}
        lock = Lock(settings.user_dir / "locks" / "snapshot", timeout=30).acquire()
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
    young: set[str] = set()  # submitted within the grace period: maybe not listed yet, maybe already over
    for record in records:
        job_id = record.get("job_id")
        state = record.get("state") or {}
        if not job_id or state.get("execution_state") == "ended":
            continue
        row = active.get(job_num(job_id))
        if row is not None:
            if record.get("missing_since"):  # seen again: a later disappearance starts a new grace period
                store.update(job_id, lambda current: current.pop("missing_since", None))
            seen = states.TABLE_CLASS.get(row["status"])
            if seen is None or seen != states.state_class(state):
                detail_ids.append(job_id)
        else:
            submit = record.get("submit") or {}
            # Only a table read that started after qsub returned is evidence that the job left it.
            if submit.get("accepted_ts") and float(snap.get("observed_ts", 0)) <= submit["accepted_ts"]:
                continue
            # History is read for a job just submitted too: a short job is over within seconds, and skipping it
            # left job_wait showing it queued until the grace ran out (11-06 joint test). In the grace only a
            # finished record counts, and not finding the job does not start its missing clock.
            if grace and within(submit.get("submitted_at"), grace):
                young.add(job_id)
            history_ids.append(job_id)
    events: list[dict] = []
    # Each observation is dated by the start of its query, so that a concurrent refresh that queried later and
    # finished first is not undone by this one (Store.apply).
    if detail_ids:
        queried = time.time()
        details = pbs.detail(detail_ids)
        for job_id in detail_ids:
            fields = _match(details, job_id)
            if fields is None:
                history_ids.append(job_id)
                continue
            observation = states.from_detail(fields, history=False, cancel_requested=False,
                                             mig_hardware=mig_hardware)
            event = store.apply(job_id, observation, "qstat -f", observed_ts=queried)
            if event:
                events.append(event)
    if history_ids:
        queried = time.time()
        details = pbs.detail(history_ids, history=True)
        # `unknown` also rests on earlier queries (the table read, perhaps qstat -f): date it by the earliest.
        absent_since = min(float(snap.get("observed_ts") or queried), queried)
        for job_id in history_ids:
            fields = _match(details, job_id)
            if job_id in young and (fields is None or not states.finished(fields)):
                continue
            record = store.find(job_id) or {}
            if fields is not None:
                observation = states.from_detail(fields, history=True,
                                                 cancel_requested=bool(record.get("cancel_requested")),
                                                 mig_hardware=mig_hardware)
                observed_ts = queried
            else:
                missing = _missing_since(store, job_id)
                if (datetime.now().astimezone() - missing).total_seconds() < float(settings.config.get("missing_s", 300)):
                    continue
                observation = {"kind": "unknown", "execution_state": "unknown", "outcome": "unknown",
                               "raw_state": "not in active table or history", "pbs_time": None}
                observed_ts = absent_since
            event = store.apply(job_id, observation, "qstat -H -f", observed_ts=observed_ts)
            if event:
                events.append(event)
    return events


def refreshed(store: Store, records: list[dict], snap: dict) -> list[dict]:
    """`records` after bringing them up to date with PBS: the ones that changed are read again."""
    changed = {event["job_id"] for event in refresh(store, records, snap)}
    return [(store.find(r["job_id"]) or r) if r.get("job_id") in changed else r for r in records]


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
    snap = snapshot(store.settings, max_age, newer_than=accepted_after(records))
    refresh(store, records, snap)
    return [store.find(job_id) or {} for job_id in job_ids], snap
