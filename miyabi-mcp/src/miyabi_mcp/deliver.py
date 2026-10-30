"""Notification delivery on the agent's machine (DESIGN.md 9.4).

The server only checks the shape of a delivery target (it cannot see the agent's machine); the daemon
runs the delivery locally. listener writes the message into the local inbox that `notify listen` (a
Claude Code background command) waits on; herdr and tmux type into an agent's terminal; codex_queue uses
the local Codex app-server; command runs a command named in the local config; file appends to a file.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from .util import MiyabiError, append_line, compact, now_iso, write_json

METHODS = {"listener", "herdr", "tmux", "codex_queue", "command", "file"}
REQUIRED = {"herdr": ("agent",), "tmux": ("target",), "codex_queue": ("thread", "remote"), "command": ("name",),
            "file": ("path",)}


def validate_shape(deliver: dict | None) -> dict:
    deliver = dict(deliver or {"method": "listener"})
    method = deliver.get("method")
    if method not in METHODS:
        raise MiyabiError("invalid_arguments", f"deliver.method must be one of {sorted(METHODS)}")
    missing = [key for key in REQUIRED.get(method, ()) if not deliver.get(key)]
    if missing:
        raise MiyabiError("invalid_arguments", f"{method} delivery needs deliver.{', deliver.'.join(missing)}")
    if method == "codex_queue" and not str(deliver["remote"]).startswith("unix:///"):
        raise MiyabiError("invalid_arguments", "codex_queue needs deliver.remote = unix:///<app-server socket>; "
                          "see `codex app-server daemon version`")
    if method == "file" and not str(deliver["path"]).startswith("/"):
        raise MiyabiError("invalid_arguments", "file delivery needs an absolute deliver.path")
    return deliver


def inbox(state_dir: Path, notification_id: str) -> Path:
    return state_dir / "inbox" / notification_id


def send(deliver: dict, message: str, *, state_dir: Path, notification_id: str, msg_id: str,
         commands: dict | None = None) -> dict[str, Any]:
    """Deliver once on this machine. Status: delivered, uncertain (may have arrived), retry or failed."""
    method = deliver["method"]
    try:
        if method == "listener":
            write_json(inbox(state_dir, notification_id) / (msg_id.replace(":", "_") + ".json"),
                       {"msg_id": msg_id, "at": now_iso(), "message": message})
            return {"status": "delivered"}
        if method == "file":
            append_line(Path(deliver["path"]), compact({"notification": notification_id, "msg_id": msg_id,
                                                        "at": now_iso(), "message": message}))
            return {"status": "delivered"}
        if method == "command":
            argv = (commands or {}).get(deliver["name"])
            if not argv:
                return {"status": "failed", "detail": f"no command {deliver['name']} in this machine's config"}
            result = subprocess.run(argv, input=compact({"notification": notification_id, "msg_id": msg_id,
                                                         "message": message}),
                                    text=True, capture_output=True, timeout=60, check=False)
            return {"status": "delivered" if result.returncode == 0 else "failed",
                    "returncode": result.returncode, "stderr": result.stderr[-1000:]}
        if method == "herdr":
            argv = ["herdr"] + (["--machine", deliver["machine"]] if deliver.get("machine") else [])
            result = subprocess.run(argv + ["agent", "prompt", deliver["agent"], message], text=True,
                                    capture_output=True, timeout=120, check=False)
            text = result.stdout + result.stderr
            if result.returncode == 0:
                return {"status": "delivered"}
            if "agent_blocked" in text:
                return {"status": "retry", "detail": "the agent is waiting at a dialog"}
            if "timeout" in text or "stalled" in text:
                return {"status": "uncertain", "detail": text[-1000:]}
            return {"status": "retry", "detail": text[-1000:]}
        if method == "tmux":
            first = subprocess.run(["tmux", "send-keys", "-t", deliver["target"], "-l", message],
                                   capture_output=True, text=True, timeout=30, check=False)
            if first.returncode != 0:
                return {"status": "retry", "detail": first.stderr[-1000:]}
            second = subprocess.run(["tmux", "send-keys", "-t", deliver["target"], "Enter"],
                                    capture_output=True, text=True, timeout=30, check=False)
            return {"status": "delivered" if second.returncode == 0 else "uncertain", "detail": second.stderr[-500:]}
        if method == "codex_queue":
            codex = shutil.which(deliver.get("codex") or "codex")
            if not codex:
                return {"status": "failed", "detail": "codex is not installed on this machine"}
            result = subprocess.run([codex, "queue", "--thread", deliver["thread"], "--message", message,
                                     "--remote", deliver["remote"]], capture_output=True, text=True, timeout=60,
                                    check=False)
            return {"status": "delivered" if result.returncode == 0 else "uncertain",
                    "returncode": result.returncode, "stderr": result.stderr[-2000:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "uncertain", "detail": str(exc)}
    raise MiyabiError("invalid_arguments", f"unknown delivery method {method}")
