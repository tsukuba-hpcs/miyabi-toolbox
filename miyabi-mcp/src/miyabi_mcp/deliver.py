"""Wake-up delivery adapters (DESIGN.md 9.3).

claude_background prints the message: the waiter runs as a Claude Code background command, whose exit
wakes the session. codex_queue and codex_goal are ported from miyabi-pbs-goal-wakeup's job_wakeup.py
(verified with Codex 0.158). herdr and tmux type into an agent's terminal (unverified). command runs a
command named in config.json; file appends to a file inside the project.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Settings
from .util import MiyabiError, append_line, compact, now_iso

METHODS = {"claude_background", "codex_queue", "codex_goal", "herdr", "tmux", "command", "file"}


def validate(deliver: dict | None, settings: Settings, project_dir: Path) -> dict:
    deliver = dict(deliver or {"method": "claude_background"})
    method = deliver.get("method")
    if method not in METHODS:
        raise MiyabiError("invalid_arguments", f"deliver.method must be one of {sorted(METHODS)}")
    if method in {"codex_queue", "codex_goal"}:
        thread = deliver.get("thread") or os.environ.get("CODEX_THREAD_ID")
        try:
            uuid.UUID(str(thread))
        except ValueError:
            raise MiyabiError("invalid_arguments", "codex delivery needs deliver.thread (an exact thread UUID)") from None
        remote = deliver.get("remote") or ""
        if not remote.startswith("unix:///"):
            raise MiyabiError("invalid_arguments", "codex delivery needs deliver.remote = unix:///<daemon socket>; "
                              "see `codex app-server daemon version`")
        executable = shutil.which(deliver.get("codex") or "codex")
        if not executable:
            raise MiyabiError("invalid_arguments", "codex executable not found on this host")
        deliver.update(thread=str(thread), codex=executable)
    elif method == "herdr":
        if not deliver.get("agent"):
            raise MiyabiError("invalid_arguments", "herdr delivery needs deliver.agent")
    elif method == "tmux":
        if not deliver.get("target"):
            raise MiyabiError("invalid_arguments", "tmux delivery needs deliver.target (session:window.pane)")
    elif method == "command":
        if deliver.get("name") not in (settings.config.get("deliver_commands") or {}):
            raise MiyabiError("invalid_arguments", "command delivery needs deliver.name defined in config.json "
                              "deliver_commands")
    elif method == "file":
        path = Path(deliver.get("path") or "")
        if not path.is_absolute() or not path.resolve().is_relative_to(project_dir.resolve()):
            raise MiyabiError("invalid_arguments", "file delivery needs an absolute deliver.path inside the project")
    return deliver


def send(settings: Settings, watch: dict, message: str) -> dict[str, Any]:
    """Deliver once. Status: delivered, uncertain (may have arrived), retry (try later) or failed."""
    deliver = watch["deliver"]
    method = deliver["method"]
    try:
        if method == "claude_background":
            print(message, flush=True)
            return {"status": "delivered"}
        if method == "file":
            append_line(Path(deliver["path"]), compact({"watch_id": watch["watch_id"], "at": now_iso(),
                                                        "message": message}))
            return {"status": "delivered"}
        if method == "command":
            argv = settings.config["deliver_commands"][deliver["name"]]
            result = subprocess.run(argv, input=compact({"watch": watch, "message": message}), text=True,
                                    capture_output=True, timeout=60, check=False)
            return {"status": "delivered" if result.returncode == 0 else "failed",
                    "returncode": result.returncode, "stderr": result.stderr[-1000:]}
        if method == "herdr":
            argv = ["herdr"] + (["--machine", deliver["machine"]] if deliver.get("machine") else [])
            result = subprocess.run(argv + ["agent", "prompt", deliver["agent"], message], text=True,
                                    capture_output=True, timeout=120, check=False)
            if result.returncode == 0:
                return {"status": "delivered"}
            text = result.stdout + result.stderr
            if "agent_blocked" in text:
                return {"status": "retry", "detail": "agent is blocked at a dialog"}
            if "timeout" in text or "stalled" in text:
                return {"status": "uncertain", "detail": text[-1000:]}
            return {"status": "failed", "detail": text[-1000:]}
        if method == "tmux":
            first = subprocess.run(["tmux", "send-keys", "-t", deliver["target"], "-l", message],
                                   capture_output=True, text=True, timeout=30, check=False)
            if first.returncode != 0:
                return {"status": "failed", "detail": first.stderr[-1000:]}
            second = subprocess.run(["tmux", "send-keys", "-t", deliver["target"], "Enter"],
                                    capture_output=True, text=True, timeout=30, check=False)
            return {"status": "delivered" if second.returncode == 0 else "uncertain", "detail": second.stderr[-500:]}
        if method == "codex_queue":
            result = subprocess.run([deliver["codex"], "queue", "--thread", deliver["thread"], "--message", message,
                                     "--remote", deliver["remote"]], capture_output=True, text=True, timeout=60,
                                    check=False)
            return {"status": "delivered" if result.returncode == 0 else "uncertain",
                    "returncode": result.returncode, "stdout": result.stdout[-2000:], "stderr": result.stderr[-2000:]}
        if method == "codex_goal":
            return goal_resume(watch, message)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "uncertain", "detail": str(exc)}
    raise MiyabiError("invalid_arguments", f"unknown delivery method {method}")


# -- Codex Goal restoration (ported from job_wakeup.py) ----------------------------------------------------------

class Rpc:
    """JSON-RPC over the Codex daemon's WebSocket Unix socket (no new app server)."""

    def __init__(self, remote: str, timeout: float = 20):
        if not remote.startswith("unix:///"):
            raise ValueError("Goal mode requires an explicit unix:///absolute/socket endpoint")
        self.timeout = timeout
        self.sequence = 0
        self.buffer = b""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(remote[len("unix://"):])
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                               "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                               f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
            while b"\r\n\r\n" not in self.buffer:
                data = self.sock.recv(4096)
                if not data or len(self.buffer) > 16384:
                    raise RuntimeError("invalid WebSocket handshake")
                self.buffer += data
            header, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
            lines = header.decode().split("\r\n")
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            accept = next((line.split(":", 1)[1].strip() for line in lines[1:]
                           if line.lower().startswith("sec-websocket-accept:")), "")
            upgrade = next((line.split(":", 1)[1].strip() for line in lines[1:]
                            if line.lower().startswith("upgrade:")), "")
            if lines[0].split()[1] != "101" or accept != expected or upgrade.lower() != "websocket":
                raise RuntimeError("WebSocket handshake rejected")
            self.call("initialize", {"clientInfo": {"name": "miyabi-mcp", "version": "0.1"},
                                     "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def frame(self, payload: bytes, opcode: int = 1) -> None:
        size = len(payload)
        head = bytes([0x80 | opcode])
        if size < 126:
            head += bytes([0x80 | size])
        elif size < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", size)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(payload)))

    def send(self, value: dict) -> None:
        self.frame(json.dumps(value).encode())

    def receive_bytes(self, size: int, deadline: float) -> bytes:
        while len(self.buffer) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RPC deadline exceeded; result may be uncertain")
            self.sock.settimeout(remaining)
            try:
                data = self.sock.recv(max(4096, size - len(self.buffer)))
            except socket.timeout as exc:
                raise TimeoutError("RPC deadline exceeded; result may be uncertain") from exc
            if not data:
                raise RuntimeError("WebSocket closed")
            self.buffer += data
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result

    def receive(self, deadline: float) -> dict:
        payload = bytearray()
        started = False
        while True:
            first, second = self.receive_bytes(2, deadline)
            opcode, final = first & 15, bool(first & 128)
            if first & 0x70 or second & 128:
                raise RuntimeError("unsupported WebSocket flags")
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self.receive_bytes(2, deadline))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.receive_bytes(8, deadline))[0]
            if size + len(payload) > 16 * 1024 * 1024:
                raise RuntimeError("WebSocket message exceeds 16 MiB")
            body = self.receive_bytes(size, deadline)
            if opcode == 8:
                raise RuntimeError("WebSocket peer closed connection")
            if opcode in (9, 10):
                if not final or size > 125:
                    raise RuntimeError("invalid WebSocket control frame")
                if opcode == 9:
                    self.frame(body, 10)
                continue
            if (not started and opcode != 1) or (started and opcode != 0):
                raise RuntimeError("unexpected WebSocket frame")
            started = True
            payload.extend(body)
            if final:
                return json.loads(payload)

    def call(self, method: str, params: dict) -> dict:
        self.sequence += 1
        request_id = self.sequence
        self.sock.settimeout(self.timeout)
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            message = self.receive(deadline)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError(f"RPC {method}: {message['error']}")
                return message["result"]
            if "method" in message and "id" in message:
                self.send({"id": message["id"], "error": {"code": -32601,
                           "message": "miyabi-mcp does not handle agent requests"}})

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "Rpc":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def goal_identity(goal: dict | None) -> dict | None:
    if not goal:
        return None
    return {key: goal.get(key) for key in ("threadId", "createdAt", "objective", "tokenBudget")}


def goal_view(rpc: Rpc, thread_id: str) -> tuple[dict | None, str]:
    goal = rpc.call("thread/goal/get", {"threadId": thread_id}).get("goal")
    thread = rpc.call("thread/read", {"threadId": thread_id})["thread"]
    return goal, thread["status"]["type"]


def check_goal(watch: dict, goal: dict | None, thread_status: str) -> tuple[str, str | None]:
    """Fail closed on goal replacement, manual lifecycle changes, or an exhausted budget."""
    if goal_identity(goal) != goal_identity(watch.get("goal_registered")):
        return "cancel", "Goal identity or budget changed"
    assert goal is not None
    parked = watch.get("goal_parked")
    if not parked:
        if goal["status"] not in {"active", "paused"}:
            return "cancel", f"Goal became {goal['status']}"
        created = time.time() - watch.get("created_ts", time.time())
        if created > 300:
            return "cancel", "Goal did not become paused and idle within five minutes"
        return ("park", None) if goal["status"] == "paused" and thread_status == "idle" else ("wait", None)
    if goal["status"] != "paused" or goal.get("updatedAt") != parked.get("updatedAt"):
        return "cancel", "Goal changed after parking; automatic resume revoked"
    if goal.get("tokenBudget") is not None and goal.get("tokensUsed", 0) >= goal["tokenBudget"]:
        return "cancel", "Goal token budget exhausted"
    return ("ready", None) if thread_status == "idle" else ("wait", None)


def goal_register(deliver: dict) -> dict:
    with Rpc(deliver["remote"]) as rpc:
        goal, thread_status = goal_view(rpc, deliver["thread"])
    if not goal or goal.get("status") != "active":
        raise MiyabiError("invalid_arguments", "codex_goal requires an active Goal; arm the watch before pausing it")
    return {"goal_registered": goal, "thread_registered_status": thread_status}


def goal_guard(watch: dict) -> tuple[str, str | None, dict | None]:
    deliver = watch["deliver"]
    with Rpc(deliver["remote"]) as rpc:
        goal, thread_status = goal_view(rpc, deliver["thread"])
    action, reason = check_goal(watch, goal, thread_status)
    return action, reason, goal


def goal_resume(watch: dict, message: str) -> dict[str, Any]:
    deliver = watch["deliver"]
    try:
        with Rpc(deliver["remote"]) as rpc:
            goal, thread_status = goal_view(rpc, deliver["thread"])
            action, reason = check_goal(watch, goal, thread_status)
            if action == "cancel":
                return {"status": "cancelled", "detail": reason}
            if action != "ready":
                return {"status": "retry", "detail": "thread is busy"}
            # Persist evidence without starting a turn; activating the Goal starts the continuation.
            rpc.call("thread/inject_items", {"threadId": deliver["thread"], "items": [{
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": message}]}]})
            goal, thread_status = goal_view(rpc, deliver["thread"])
            action, reason = check_goal(watch, goal, thread_status)
            if action != "ready":
                return {"status": "cancelled", "detail": reason or "thread became busy after injection",
                        "evidence_injected": True}
            resumed = rpc.call("thread/goal/set", {"threadId": deliver["thread"], "status": "active"})["goal"]
            if goal_identity(resumed) != goal_identity(goal) or resumed.get("status") != "active":
                raise RuntimeError("Goal resume response did not preserve the expected Goal")
            return {"status": "delivered", "goal_before_resume": goal, "goal_after_resume": resumed}
    except (OSError, ValueError, KeyError, RuntimeError, TimeoutError) as exc:
        return {"status": "uncertain", "detail": str(exc)}
