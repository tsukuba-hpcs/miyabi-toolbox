"""Which code is running: the package version and the commit of the checkout it came from (DESIGN.md 6.1).

An MCP server lives as long as its client's session and keeps the code it started with, while the checkout,
its README and DESIGN move on. In the 11-06 joint test agents read docs newer than the server they talked to
and took the differences for bugs. The server pins the commit when it starts and says so when the checkout
has moved on since.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from . import __version__

CHECKOUT = Path(__file__).resolve().parents[2]
_cache: dict = {}


def checkout_commit() -> str | None:
    """The commit the checkout is at now (short hash); None outside a git checkout."""
    try:
        result = subprocess.run(["git", "-C", str(CHECKOUT), "rev-parse", "--short=7", "HEAD"], capture_output=True,
                                text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def running() -> str | None:
    """The commit this process started from: the first call pins it (the server makes it at start)."""
    if "running" not in _cache:
        _cache["running"] = checkout_commit()
    return _cache["running"]


def label() -> str:
    commit = running()
    return f"{__version__}+{commit}" if commit else __version__


def stale_note(max_age: float = 60) -> str | None:
    """A note when the checkout has moved on since this process started (it is read at most every max_age s)."""
    started = running()
    now = time.monotonic()
    if "checked" not in _cache or now - _cache["checked"] >= max_age:
        _cache.update(checked=now, checkout=checkout_commit())
    current = _cache.get("checkout")
    if started and current and current != started:
        return (f"this miyabi-mcp server runs {started}, but its checkout is now at {current}: its tools and "
                "schemas may differ from the README and DESIGN there. Restart the MCP server (the agent's MCP "
                "client) to use the newer code")
    return None


def describe() -> dict:
    """For pbs_context: the version, the running commit and, when it has moved on, the checkout's."""
    info = {"version": __version__, "commit": running()}
    note = stale_note(max_age=0)
    if note:
        info.update(checkout=_cache.get("checkout"), restart_needed=note)
    return info
