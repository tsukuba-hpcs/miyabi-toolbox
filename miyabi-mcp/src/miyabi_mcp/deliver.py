"""Notification delivery on the agent's machine (DESIGN.md 9.4).

The server only checks the shape of a delivery target (it cannot see the agent's machine); the daemon
runs the delivery locally. listener writes the message into the local inbox that `notify listen` (a
Claude Code background command) waits on; herdr and tmux type into an agent's terminal; codex_queue uses
the local Codex app-server; command runs a command named in the local config; file appends to a file.
"""

from __future__ import annotations

import json
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


def _herdr(deliver: dict) -> list[str]:
    return ["herdr"] + (["--machine", deliver["machine"]] if deliver.get("machine") else [])


def _herdr_error(text: str) -> tuple[str | None, str]:
    """herdr's error code and message: it prints errors as one JSON object on stderr."""
    for line in reversed(text.strip().splitlines()):
        try:
            error = json.loads(line).get("error")
        except (ValueError, AttributeError):
            continue
        if isinstance(error, dict):
            return error.get("code"), str(error.get("message") or line)
    return None, text.strip()[-1000:]


def _herdr_agents(deliver: dict) -> list[str]:
    """The live agents herdr knows (names, or pane ids of unnamed ones), to show next to a wrong target."""
    try:
        result = subprocess.run(_herdr(deliver) + ["agent", "list"], capture_output=True, text=True, timeout=30,
                                check=False)
        agents = json.loads(result.stdout)["result"]["agents"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return []
    return [f"{a['name']} ({a.get('pane_id')})" if a.get("name") else f"{a.get('pane_id')} ({a.get('agent')}, unnamed)"
            for a in agents][:12]


def check(deliver: dict, commands: dict | None = None) -> str | None:
    """Why `deliver` cannot reach its target from this machine now; None when it can, or when this cannot tell.

    Run where the daemon delivers, right after a notification is registered: otherwise a wrong target shows
    only once the jobs have ended, as failed deliveries that nobody reads (11-06 joint test: a herdr agent
    name that did not exist left a project waiting for eight hours).
    """
    method = deliver.get("method")
    try:
        if method == "herdr":
            result = subprocess.run(_herdr(deliver) + ["agent", "get", deliver["agent"]], capture_output=True,
                                    text=True, timeout=30, check=False)
            if result.returncode == 0:
                return None
            code, text = _herdr_error(result.stderr + "\n" + result.stdout)
            if code != "agent_not_found":
                return f"herdr cannot look up agent {deliver['agent']!r}: {text}"
            live = _herdr_agents(deliver)
            return (f"herdr has no agent {deliver['agent']!r}" + (f"; live agents: {', '.join(live)}" if live else "")
                    + ". A target is a unique agent name or the pane id of a pane that hosts an agent")
        if method == "tmux":
            result = subprocess.run(["tmux", "display-message", "-p", "-t", deliver["target"], "#{pane_id}"],
                                    capture_output=True, text=True, timeout=30, check=False)
            if result.returncode != 0:
                return f"tmux has no target {deliver['target']!r}: {result.stderr.strip()[-300:]}"
            return None
        if method == "command" and not (commands or {}).get(deliver["name"]):
            return f"no command {deliver['name']!r} in this machine's config"
        if method == "codex_queue" and not shutil.which(deliver.get("codex") or "codex"):
            return "codex is not installed on this machine"
    except (FileNotFoundError, PermissionError) as exc:
        return f"{method} cannot run on this machine: {exc}"
    except (OSError, subprocess.TimeoutExpired):
        return None  # cannot tell now; a delivery attempt will
    return None


def alert(deliver: dict, title: str, body: str) -> dict[str, Any]:
    """The last resort for a message given up: a herdr notification for the person at this machine."""
    if not shutil.which("herdr"):
        return {"status": "skipped", "detail": "herdr is not installed on this machine"}
    machine = {"machine": deliver["machine"]} if deliver.get("method") == "herdr" and deliver.get("machine") else {}
    try:
        result = subprocess.run(_herdr(machine) + ["notification", "show", title, "--body", body[:1500],
                                                   "--sound", "request"],
                                capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "detail": str(exc)}
    return {"status": "delivered" if result.returncode == 0 else "failed",
            "detail": (result.stderr or result.stdout).strip()[-300:]}


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
            result = subprocess.run(_herdr(deliver) + ["agent", "prompt", deliver["agent"], message], text=True,
                                    capture_output=True, timeout=120, check=False)
            if result.returncode == 0:
                return {"status": "delivered"}
            code, text = _herdr_error(result.stderr + "\n" + result.stdout)
            if code == "agent_blocked":
                return {"status": "retry", "code": code, "detail": "the agent is waiting at a dialog"}
            if code in {"timeout", "agent_prompt_stalled"} or "timeout" in text or "stalled" in text:
                return {"status": "uncertain", "code": code, "detail": text}
            # agent_not_found: a wrong target (notifyd ensure checks for that) or an agent being restarted
            return {"status": "retry", "code": code, "detail": text}
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
    except (FileNotFoundError, PermissionError) as exc:
        # The program, file or directory is missing or not allowed here: nothing was sent, and retrying does
        # not help. "uncertain" would count as delivered and stop the retries.
        return {"status": "failed", "detail": str(exc)}
    except subprocess.TimeoutExpired as exc:  # the program ran and may have delivered
        return {"status": "uncertain", "detail": str(exc)}
    except OSError as exc:  # nothing was started or written (e.g. a full disk): try again
        return {"status": "retry", "detail": str(exc)}
    raise MiyabiError("invalid_arguments", f"unknown delivery method {method}")
