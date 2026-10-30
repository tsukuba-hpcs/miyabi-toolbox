"""The per-project job ledger in <project>/.miyabi-mcp/ and the user-level index (DESIGN.md 7.1–7.7).

Records are replaced atomically; events are append-only and never edited; every write that must not
interleave across login nodes holds a Lock. Ended jobs are final: only a `reconciled` event follows.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .util import (Lock, MiyabiError, append_line, compact, digest_bytes, now_iso, read_json, read_lines,
                   short_id, write_json)

STATE_KEYS = ("kind", "execution_state", "outcome", "raw_state", "pbs_time", "exit_status", "observed_at",
              "source", "seq")
EXPORTED_KINDS = {"submitted", "queued", "held", "running", "ended", "unknown", "reconciled"}


def host() -> str:
    return socket.gethostname().split(".", 1)[0]


def job_num(job_id: str) -> str:
    return job_id.split(".", 1)[0]


def month(iso: str) -> str:
    return iso[:7]


class Store:
    """One project's ledger."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = settings.require_project()
        self.project_dir = settings.project_dir

    # -- paths and locks --------------------------------------------------------------------------------------
    def submission_dir(self, submit_id: str) -> Path:
        return self.state / "submissions" / submit_id

    def job_dir(self, job_id: str) -> Path:
        return self.state / "jobs" / job_num(job_id)

    def locator(self, job_id: str) -> str:
        return str(self.job_dir(job_id) / "record.json")

    def lock(self, name: str, lease: float = 120, timeout: float = 30) -> Lock:
        return Lock(self.state / "locks" / name, lease=lease, timeout=timeout)

    # -- records ----------------------------------------------------------------------------------------------
    def new_submission(self, record: dict) -> Path:
        submit_id = record["submit_id"]
        directory = self.submission_dir(submit_id)
        directory.mkdir(mode=0o700, parents=True)
        write_json(directory / "record.json", record)
        return directory

    def write(self, record: dict) -> None:
        write_json(self.submission_dir(record["submit_id"]) / "record.json", record)

    def read_submission(self, submit_id: str) -> dict | None:
        return read_json(self.submission_dir(submit_id) / "record.json")

    def bind(self, submit_id: str, job_id: str) -> None:
        link = self.job_dir(job_id)
        link.parent.mkdir(parents=True, exist_ok=True)
        target = Path("..") / "submissions" / submit_id
        try:
            os.symlink(target, link)
        except FileExistsError:
            if os.readlink(link) != str(target):
                raise MiyabiError("ledger_conflict", f"job {job_id} is already bound to {os.readlink(link)}")

    def find(self, job_id: str) -> dict | None:
        return read_json(self.job_dir(job_id) / "record.json")

    def records(self) -> Iterator[dict]:
        for path in sorted((self.state / "submissions").glob("*/record.json")):
            record = read_json(path)
            if record:
                yield record

    def update(self, job_id: str, change) -> dict:
        """Read-modify-write one bound record under its job lock."""
        with self.lock(f"job-{job_num(job_id)}"):
            record = self.find(job_id)
            if record is None:
                raise MiyabiError("not_in_ledger", f"job {job_id} is not in this project's ledger")
            change(record)
            self.write(record)
            return record

    # -- events -----------------------------------------------------------------------------------------------
    def _append_event(self, event: dict) -> None:
        with self.lock("events", lease=60):
            append_line(self.state / "events" / f"{month(event['observed_at'])}.jsonl", compact(event))

    def apply(self, job_id: str, observation: dict, source: str, *, correction_of: str | None = None,
              note: str | None = None) -> dict | None:
        """Record an observation as the next event when it changes the job's state. Returns the event."""
        kind = observation["kind"]
        if kind == "reconciled" and not (correction_of and note):
            raise MiyabiError("invalid_arguments", "a reconciled event needs correction_of and note (the evidence)")
        with self.lock(f"job-{job_num(job_id)}"):
            record = self.find(job_id)
            if record is None:
                raise MiyabiError("not_in_ledger", f"job {job_id} is not in this project's ledger")
            last = record.get("state") or {}
            if kind != "reconciled":
                if last.get("execution_state") == "ended":
                    return None
                if (observation["execution_state"], observation["raw_state"]) == (
                        last.get("execution_state"), last.get("raw_state")):
                    return None
            seq = int(record.get("seq") or 0) + 1
            event = {
                "event_id": f"miyabi-pbs:{job_id}:{seq}", "job_id": job_id, "seq": seq, "kind": kind,
                "observed_at": now_iso(), "observer": host(), "source": source,
                "execution_state": observation["execution_state"], "outcome": observation["outcome"],
                "raw_state": observation["raw_state"], "locator": self.locator(job_id),
            }
            for key in ("pbs_time", "exit_status", "usage", "token"):
                if observation.get(key) is not None:
                    event[key] = observation[key]
            if record.get("client_ref"):
                event["client_ref"] = record["client_ref"]
            if correction_of:
                event["correction_of"] = correction_of
                event["note"] = note
            self._append_event(event)
            record["seq"] = seq
            record["state"] = {key: event.get(key) for key in STATE_KEYS}
            if event.get("usage"):
                record["usage"] = event["usage"]
            if event.get("token") is not None:
                record["token_reported"] = event["token"]
            self.write(record)
        if event["execution_state"] == "ended":
            index_append(self.settings, {"type": "ended", "job_id": job_id, "project_dir": str(self.project_dir),
                                         "outcome": event["outcome"], "usage": event.get("usage"),
                                         "at": event["observed_at"]})
        return event

    def note(self, job_id: str, kind: str, raw_state: str, **extra: Any) -> dict:
        """A non-state event (cancel_requested, annotated): it consumes a seq but changes no state."""
        with self.lock(f"job-{job_num(job_id)}"):
            record = self.find(job_id)
            if record is None:
                raise MiyabiError("not_in_ledger", f"job {job_id} is not in this project's ledger")
            last = record.get("state") or {}
            seq = int(record.get("seq") or 0) + 1
            event = {"event_id": f"miyabi-pbs:{job_id}:{seq}", "job_id": job_id, "seq": seq, "kind": kind,
                     "observed_at": now_iso(), "observer": host(), "source": "miyabi-mcp",
                     "execution_state": last.get("execution_state", "unknown"),
                     "outcome": last.get("outcome", "unknown"), "raw_state": raw_state,
                     "locator": self.locator(job_id), **extra}
            self._append_event(event)
            record["seq"] = seq
            if kind == "cancel_requested":
                record["cancel_requested"] = True
            self.write(record)
            return event

    def events(self, cursor: str | None = None, limit: int | None = None) -> tuple[list[dict], str | None]:
        """Events in append order from `cursor` ("YYYY-MM:<lines consumed>")."""
        start_month, start_line = (cursor.split(":", 1) if cursor else ("", "0"))
        start_line_n = int(start_line)
        result: list[dict] = []
        next_cursor = cursor
        for path in sorted((self.state / "events").glob("*.jsonl")):
            name = path.stem
            if name < start_month:
                continue
            skip = start_line_n if name == start_month else 0
            for index, line in enumerate(read_lines(path)):
                if index < skip:
                    continue
                if limit is not None and len(result) >= limit:
                    return result, next_cursor
                result.append(json.loads(line))
                next_cursor = f"{name}:{index + 1}"
        return result, next_cursor

    # -- idempotency keys and plans ---------------------------------------------------------------------------
    def request_path(self, key: str) -> Path:
        return self.state / "requests" / (digest_bytes(key.encode("utf-8")).split(":", 1)[1][:32] + ".json")

    def plan_path(self, plan_digest: str) -> Path:
        return self.state / "plans" / (plan_digest.split(":", 1)[-1][:32] + ".json")


# -- user level ---------------------------------------------------------------------------------------------------

def _user_lock(settings: Settings, name: str, lease: float = 60, timeout: float = 30) -> Lock:
    return Lock(settings.user_dir / "locks" / name, lease=lease, timeout=timeout)


def submit_lock(settings: Settings) -> Lock:
    return _user_lock(settings, "submit", lease=180, timeout=120)


def register_project(settings: Settings) -> None:
    if settings.project_dir is None:
        return
    path = settings.user_dir / "projects.json"
    with _user_lock(settings, "projects"):
        data = read_json(path, {"schema_version": 1, "projects": {}})
        entry = data["projects"].setdefault(str(settings.project_dir), {"first_used": now_iso()})
        entry["last_used"] = now_iso()
        write_json(path, data)


def index_append(settings: Settings, entry: dict) -> None:
    entry = {"at": now_iso(), **entry}
    with _user_lock(settings, "index"):
        append_line(settings.user_dir / "index" / f"{month(entry['at'])}.jsonl", compact(entry))


def index_entries(settings: Settings) -> Iterator[dict]:
    for path in sorted((settings.user_dir / "index").glob("*.jsonl"), reverse=True):
        for line in reversed(list(read_lines(path))):
            yield json.loads(line)


def index_owner(settings: Settings, job_id: str) -> str | None:
    """The project directory that submitted (or adopted) `job_id`, from the user index."""
    number = job_num(job_id)
    for entry in index_entries(settings):
        if entry.get("type") in {"submitted", "adopted"} and job_num(entry.get("job_id", "")) == number:
            return entry.get("project_dir")
    return None


def new_submit_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + short_id(3)
