"""Shared helpers: errors, times, JSON, blake2b digests, atomic files and cross-node locks (stdlib only)."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import socket
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
HMS = re.compile(r"(\d+):([0-5]\d):([0-5]\d)")
DIAGNOSTIC_LIMIT = 4096


class MiyabiError(Exception):
    """An expected failure, reported to the caller as {"ok": false, "error": {...}}."""

    def __init__(self, kind: str, message: str, **details: Any):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"type": self.kind, "message": self.message, **self.details}


def ok(**fields: Any) -> dict:
    return {"schema_version": SCHEMA_VERSION, "ok": True, **fields}


def failure(error: MiyabiError) -> dict:
    return {"schema_version": SCHEMA_VERSION, "ok": False, "error": error.to_dict()}


def diagnostic(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = value or ""
    return value if len(value) <= DIAGNOSTIC_LIMIT else value[:DIAGNOSTIC_LIMIT] + "\n[truncated]"


# -- time -----------------------------------------------------------------------------------------------------

def now() -> datetime:
    """The current time in the host's zone (JST on Miyabi), whole seconds."""
    return datetime.now().astimezone().replace(microsecond=0)


def now_iso() -> str:
    return now().isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def pbs_time(value: str | None) -> str | None:
    """'Fri Oct 30 05:22:05 2026' (site local time, as qstat -f prints it) -> ISO 8601 with offset."""
    if not value:
        return None
    try:
        moment = datetime.strptime(value.strip(), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return moment.astimezone().isoformat()


def hms_to_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    match = HMS.fullmatch(value.strip())
    if not match:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def seconds_to_hms(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_duration(value: Any) -> int:
    """Seconds from an int, 'HH:MM:SS', or '<n>s|m|h|d'."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if HMS.fullmatch(text):
            return hms_to_seconds(text) or 0
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd])", text)
        if match:
            return int(float(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)])
    raise MiyabiError("invalid_arguments", f"invalid duration {value!r}: use seconds, HH:MM:SS or e.g. 12h")


def within(moment_iso: str | None, window_s: int, reference: datetime | None = None) -> bool:
    if not moment_iso:
        return False
    reference = reference or now()
    return parse_iso(moment_iso) >= reference - timedelta(seconds=window_s)


# -- JSON and digests -----------------------------------------------------------------------------------------

def canonical(value: Any) -> str:
    """The byte-stable serialization used for exports and digests."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def digest_bytes(data: bytes) -> str:
    return "blake2b:" + hashlib.blake2b(data, digest_size=32).hexdigest()


def digest_json(value: Any) -> str:
    return digest_bytes(canonical(value).encode("utf-8"))


def digest_file(path: Path) -> str:
    digest = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return "blake2b:" + digest.hexdigest()


def short_id(nbytes: int = 4) -> str:
    return secrets.token_hex(nbytes)


# -- files ----------------------------------------------------------------------------------------------------

def write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    """Replace `path` atomically: readers on any node see the old or the new file, never a partial one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=1, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def append_line(path: Path, line: str) -> None:
    """Append one line. Callers hold a Lock: appends from two login nodes are not atomic on their own."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_lines(path: Path) -> Iterator[str]:
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if line:
                    yield line
    except FileNotFoundError:
        return


def tail_text(path: Path, lines: int, max_bytes: int = 65536) -> str:
    """The last `lines` lines of a file, reading at most `max_bytes` from its end."""
    with open(path, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - max_bytes))
        data = stream.read()
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


# -- locks ----------------------------------------------------------------------------------------------------

class Lock:
    """Mutual exclusion across processes and login nodes by `os.link` create-if-absent.

    `os.link` fails when the target exists, also on Lustre across nodes, so it works where flock may
    not. A lock older than `lease` seconds is taken to be abandoned and may be broken.
    """

    def __init__(self, path: Path, lease: float = 120.0, timeout: float = 30.0):
        self.path = path
        self.lease = lease
        self.timeout = timeout
        self.token: str | None = None

    def acquire(self) -> "Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = f"{socket.gethostname()} {os.getpid()} {time.time():.3f} {secrets.token_hex(6)}"
        fd, temporary = tempfile.mkstemp(prefix=".lock-", dir=self.path.parent)
        with os.fdopen(fd, "w") as stream:
            stream.write(token)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    os.link(temporary, self.path)
                    self.token = token
                    return self
                except FileExistsError:
                    self._break_if_stale()
                    if time.monotonic() >= deadline:
                        raise MiyabiError("lock_timeout", f"lock {self.path} is held", holder=self.holder())
                    time.sleep(0.05 + random.random() * 0.15)
        finally:
            os.unlink(temporary)

    def holder(self) -> str | None:
        try:
            return self.path.read_text()
        except OSError:
            return None

    def _break_if_stale(self) -> None:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if age <= self.lease:
            return
        moved = self.path.with_name(f"{self.path.name}.stale-{secrets.token_hex(4)}")
        try:
            os.rename(self.path, moved)
        except FileNotFoundError:
            return
        try:
            fresh = time.time() - moved.stat().st_mtime <= self.lease
            if fresh:
                # Someone re-acquired between our stat and rename: give the lock back.
                try:
                    os.link(moved, self.path)
                except FileExistsError:
                    pass
        finally:
            moved.unlink(missing_ok=True)

    def release(self) -> None:
        if self.token is None:
            return
        try:
            if self.path.read_text() == self.token:
                self.path.unlink()
        except FileNotFoundError:
            pass
        self.token = None

    def __enter__(self) -> "Lock":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()
