#!/usr/bin/env python3
"""Durable, stdlib-only timer/PBS notifications for an existing Codex thread or Claude Code session."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import base64
import hashlib
import socket
import struct
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


DEFAULT_HELPER = Path(__file__).resolve().with_name("qstat_json.py")
TERMINAL = {"FINISH", "EXPIRED"}
# One job or subjob; an array aggregate such as 123[] is not a supported wake-up target.
JOB_ID = re.compile(r"[0-9]+(?:\[[0-9]+\])?(?:\.[A-Za-z0-9_.-]+)?")


class Rpc:
    """JSON-RPC over the daemon's WebSocket Unix socket (no new app server)."""

    def __init__(self, codex, remote, timeout=20):
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
            headers = dict(line.lower().split(":", 1) for line in lines[1:] if ":" in line)
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            # Header values are case-sensitive; parse the accept value separately.
            accept = next((l.split(":", 1)[1].strip() for l in lines[1:]
                           if l.lower().startswith("sec-websocket-accept:")), "")
            if lines[0].split()[1] != "101" or accept != expected:
                raise RuntimeError("WebSocket handshake rejected")
            if headers.get("upgrade", "").strip() != "websocket":
                raise RuntimeError("server did not upgrade to WebSocket")
            self.call("initialize", {"clientInfo": {"name": "job-wakeup", "version": "0.2"},
                                     "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def frame(self, payload, opcode=1):
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

    def send(self, value):
        self.frame(json.dumps(value).encode())

    def receive_bytes(self, size, deadline):
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

    def receive(self, deadline):
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

    def call(self, method, params):
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
                           "message": "job-wakeup does not handle agent requests"}})

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def goal_identity(goal):
    if not goal:
        return None
    return {key: goal.get(key) for key in ("threadId", "createdAt", "objective", "tokenBudget")}


def goal_view(rpc, thread_id):
    goal = rpc.call("thread/goal/get", {"threadId": thread_id}).get("goal")
    thread = rpc.call("thread/read", {"threadId": thread_id})["thread"]
    return goal, thread["status"]["type"]


def check_goal(state, goal, thread_status):
    """Fail closed on goal replacement, manual lifecycle changes, or exceeded budget."""
    if goal_identity(goal) != goal_identity(state["goal_registered"]):
        return "cancel", "Goal identity or budget changed"
    if not state.get("goal_parked"):
        if goal["status"] not in {"active", "paused"}:
            return "cancel", f"Goal became {goal['status']}"
        if time.time() - state["created_at"] > 300:
            return "cancel", "Goal did not become paused and idle within five minutes"
        return ("park", None) if goal["status"] == "paused" and thread_status == "idle" else ("wait", None)
    parked = state["goal_parked"]
    if goal["status"] != "paused" or goal["updatedAt"] != parked["updatedAt"]:
        return "cancel", "Goal changed after parking; automatic resume revoked"
    if goal.get("tokenBudget") is not None and goal["tokensUsed"] >= goal["tokenBudget"]:
        return "cancel", "Goal token budget exhausted"
    return ("ready", None) if thread_status == "idle" else ("wait", None)


def guard_goal(directory):
    state = read_state(directory)
    with Rpc(state["codex"], state["remote"]) as rpc:
        goal, thread_status = goal_view(rpc, state["thread_id"])
    action, reason = check_goal(state, goal, thread_status)

    def record(current):
        if current["status"] not in {"waiting", "ready"}:
            return
        current.update(goal_last_checked_at=time.time(), goal_last_observed=goal,
                       thread_last_status=thread_status)
        if action == "cancel":
            current.update(status="cancelled", cancellation_reason=reason)
        elif action == "park":
            current.update(goal_parked=goal, goal_parked_at=time.time())
    update(directory, record)
    return action in {"park", "ready"}


def deliver_goal(directory):
    # Serialize local cancel/ack with dispatch. An interrupted RPC remains uncertain.
    with locked(directory / "state.lock"):
        state = read_state(directory)
        if state["status"] != "ready":
            return
        with Rpc(state["codex"], state["remote"]) as rpc:
            goal, thread_status = goal_view(rpc, state["thread_id"])
            action, reason = check_goal(state, goal, thread_status)
            if action != "ready":
                if action == "cancel":
                    state.update(status="cancelled", cancellation_reason=reason)
                    write_state(directory, state)
                return
            state.update(status="sending", send_started_at=time.time(), goal_before_resume=goal)
            write_state(directory, state)
            try:
                # Persist evidence without starting a turn. Goal activation starts continuation.
                rpc.call("thread/inject_items", {"threadId": state["thread_id"], "items": [{
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": wake_message(state, directory)}],
                }]})
                state["evidence_injected_at"] = time.time()
                write_state(directory, state)
                # Recheck after injection; a user may have changed the Goal while it ran.
                goal, thread_status = goal_view(rpc, state["thread_id"])
                action, reason = check_goal(state, goal, thread_status)
                if action != "ready":
                    state.update(status="cancelled", cancellation_reason=reason or "thread became busy")
                    write_state(directory, state)
                    return
                result = rpc.call("thread/goal/set", {"threadId": state["thread_id"], "status": "active"})
                resumed = result["goal"]
                if goal_identity(resumed) != goal_identity(goal) or resumed["status"] != "active":
                    raise RuntimeError("Goal resume response did not preserve the expected Goal")
                state.update(status="delivered", delivery_status="accepted", goal_after_resume=resumed,
                             goal_resumed_at=time.time(), send_finished_at=time.time())
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                state.update(status="uncertain", delivery_status="uncertain", delivery_error=str(exc),
                             send_finished_at=time.time())
            write_state(directory, state)


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return number


@contextmanager
def locked(path, nonblocking=False):
    with open(path, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield


def read_state(directory):
    return json.loads((directory / "state.json").read_text())


def write_state(directory, state):
    # Same-directory rename keeps readers from seeing partial JSON.
    fd, name = tempfile.mkstemp(prefix=".state-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, directory / "state.json")
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def update(directory, fn):
    with locked(directory / "state.lock"):
        state = read_state(directory)
        fn(state)
        write_state(directory, state)
        return state


def query_jobs(helper, job_id, history=False):
    command = [sys.executable, "-I", helper]
    if history:
        command += ["-H", "--hday", "31"]
    command += [job_id]
    result = subprocess.run(command, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError(f"qstat helper failed: {result.stdout[-2000:]} {result.stderr[-1000:]}")
    data = json.loads(result.stdout)
    if data.get("ok") is not True or not isinstance(data.get("jobs"), list):
        raise RuntimeError(f"invalid qstat helper response: {result.stdout[-2000:]}")
    return data["jobs"]


def same_job(requested, observed):
    # Miyabi's table may omit a server suffix. Explicit mismatched servers fail closed.
    return requested == observed or (
        ("." not in requested or "." not in observed)
        and requested.split(".", 1)[0] == observed.split(".", 1)[0]
    )


def pbs_observation(state):
    job_id = state["job_id"]
    jobs = query_jobs(state["helper"], job_id)
    matches = [job for job in jobs if same_job(job_id, job["job_id"])]
    if not matches:
        jobs = query_jobs(state["helper"], job_id, history=True)
        matches = [job for job in jobs if same_job(job_id, job["job_id"])]
    if not matches:
        return False, {"status": "UNKNOWN", "reason": "absent from active and retained history"}
    if len(matches) != 1:
        raise RuntimeError("ambiguous PBS job identity")
    job = matches[0]
    if job["status"] not in TERMINAL:
        return False, job
    # Detail failure must not hide an observed terminal state. Do not infer application success.
    try:
        detail = subprocess.run(
            ["qstat", "-H", "-f", job_id], capture_output=True, text=True, timeout=30
        )
        match = re.search(r"^\s*Exit_status\s*=\s*(-?\d+)\s*$", detail.stdout, re.M)
        job["exit_status"] = int(match[1]) if match and detail.returncode == 0 else None
        job["detail_returncode"] = detail.returncode
        job["detail"] = detail.stdout[-16000:]
        job["detail_stderr"] = detail.stderr[-2000:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        job["exit_status"] = None
        job["detail_error"] = str(exc)
    return True, job


def wake_message(state, directory):
    return (
        f"[job-wakeup event {state['event_id']}]\n"
        "This is the wake-up you previously authorized. The wait for this event has ended; "
        "continue the original task within its existing permissions.\n"
        f"Event state: {directory / 'state.json'}\n"
        f"Observation: {json.dumps(state['observation'], ensure_ascii=False)}\n"
        f"Next action: {state['message']}\n"
        "First inspect the event state, then acknowledge this event using argv: "
        f"{json.dumps([sys.executable, str(Path(__file__).resolve()), 'ack', str(directory)])}. "
        "Acknowledgement means the agent resumed, not that the PBS workload succeeded. "
        "Do not register another wake-up solely because this one arrived."
    )


def send_codex(state, directory):
    command = [state["codex"], "queue", "--thread", state["thread_id"],
               "--message", wake_message(state, directory), "--remote", state["remote"]]
    try:
        result = subprocess.run(command, cwd=state["cwd"], capture_output=True,
                                text=True, timeout=60)
        return {"queue_returncode": result.returncode,
                "queue_stdout": result.stdout[-4000:], "queue_stderr": result.stderr[-4000:],
                "send_finished_at": time.time(),
                "delivery_status": "accepted" if result.returncode == 0 else "uncertain"}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"delivery_status": "uncertain", "delivery_error": str(exc),
                "send_finished_at": time.time()}


def send_claude(state, directory):
    """Print the wake-up. The watcher runs attached as a Claude Code background command,
    and the harness resumes the idle session when that command exits."""
    try:
        print(wake_message(state, directory), flush=True)
    except OSError as exc:
        return {"delivery_status": "uncertain", "delivery_error": str(exc),
                "send_finished_at": time.time()}
    return {"delivery_status": "accepted", "send_finished_at": time.time()}


SENDERS = {"codex": send_codex, "claude": send_claude}


def event_agent(state):
    # Events registered before Claude support have no agent key and belong to Codex.
    return state.get("agent", "codex")


def deliver(directory):
    def claim(state):
        if state["status"] != "ready":
            raise RuntimeError(f"cannot deliver from {state['status']}")
        state.update(status="sending", send_started_at=time.time())

    state = update(directory, claim)
    outcome = SENDERS[event_agent(state)](state, directory)

    def finish(current):
        current.update(outcome)
        # The resumed agent can acknowledge before the send call returns.
        if current["status"] != "acknowledged":
            current["status"] = "delivered" if outcome["delivery_status"] == "accepted" else "uncertain"
    update(directory, finish)


def worker(directory):
    with locked(directory / "worker.lock", nonblocking=True):
        state = read_state(directory)
        if state["status"] == "sending":
            # A crash after sending but before recording its result is ambiguous.
            update(directory, lambda s: s.update(status="uncertain"))
            return
        errors = 0
        while state["status"] in {"waiting", "ready"}:
            if state.get("resume_goal"):
                try:
                    allowed = guard_goal(directory)
                except (OSError, ValueError, KeyError, RuntimeError) as exc:
                    update(directory, lambda s: s.update(goal_check_error=str(exc),
                                                        goal_check_error_at=time.time()))
                    time.sleep(min(state["poll_seconds"], 30))
                    state = read_state(directory)
                    continue
                state = read_state(directory)
                if state["status"] not in {"waiting", "ready"}:
                    return
                if not allowed:
                    time.sleep(2)
                    continue
            if state["status"] == "ready":
                if state.get("resume_goal"):
                    try:
                        deliver_goal(directory)
                    except (OSError, ValueError, KeyError, RuntimeError) as exc:
                        update(directory, lambda s: s.update(goal_check_error=str(exc),
                                                            goal_check_error_at=time.time()))
                    state = read_state(directory)
                    if state["status"] == "ready":
                        time.sleep(2)
                        continue
                else:
                    deliver(directory)
                return
            if state["kind"] == "timer":
                remaining = state["due_at"] - time.time()
                if remaining > 0:
                    time.sleep(min(remaining, 1))
                    state = read_state(directory)
                    continue
                ready, observation = True, {"status": "TIMER_ELAPSED", "due_at": state["due_at"]}
            else:
                try:
                    ready, observation = pbs_observation(state)
                    errors = 0
                except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    errors += 1
                    ready, observation = False, {"status": "QUERY_ERROR", "error": str(exc)}

            def observed(current):
                if current["status"] == "waiting":
                    previous = current.get("observation", {}).get("status")
                    if observation.get("status") != previous:
                        transitions = current.setdefault("observations", [])
                        transitions.append({"at": time.time(), "status": observation.get("status")})
                        current["observations"] = transitions[-32:]
                    current.update(observation=observation, last_checked_at=time.time())
                    if ready:
                        current.update(status="ready", triggered_at=time.time())
            state = update(directory, observed)
            if state["status"] == "waiting":
                delay = state["poll_seconds"] * 2 ** min(errors, 4)
                end = time.monotonic() + delay
                while time.monotonic() < end and read_state(directory)["status"] == "waiting":
                    time.sleep(min(1, max(0, end - time.monotonic())))
                state = read_state(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for kind in ("timer", "pbs"):
        arm = sub.add_parser(kind)
        arm.add_argument("--agent", choices=("codex", "claude"), help=(
            "agent to wake; default: claude inside Claude Code unless a Codex option "
            "or CODEX_THREAD_ID is present, otherwise codex"))
        arm.add_argument("--message", required=True)
        arm.add_argument("--state-root", type=Path, default=Path(".job-wakeup"))
        arm.add_argument("--session", help="Claude only: exact session UUID; defaults to CLAUDE_CODE_SESSION_ID")
        arm.add_argument("--thread", help="Codex only: exact thread UUID; defaults to CODEX_THREAD_ID")
        arm.add_argument("--remote", help="Codex only, required there: existing Codex app-server endpoint")
        arm.add_argument("--codex", help="Codex only: Codex executable (default: codex)")
        arm.add_argument("--resume-goal", action="store_true", help=(
            "Codex only: arm restoration of this Goal after the user-authorized pause; "
            "caller must pause it and end the turn within five minutes"))
        if kind == "timer":
            arm.add_argument("--seconds", type=positive, required=True)
        else:
            arm.add_argument("--job", required=True, help="exact job or subjob ID; not an array aggregate")
            arm.add_argument("--helper", type=Path, default=DEFAULT_HELPER)
            arm.add_argument("--poll-seconds", type=positive, default=300, help="PBS polling interval in seconds (default: 300 / 5 minutes)")
    for name in ("run", "status", "ack", "cancel"):
        sub.add_parser(name).add_argument("event", type=Path)
    args = parser.parse_args(argv)
    if args.command in ("timer", "pbs"):
        codex_options = [flag for flag, value in (
            ("--thread", args.thread), ("--remote", args.remote),
            ("--codex", args.codex), ("--resume-goal", args.resume_goal)) if value]
        in_claude = os.environ.get("CLAUDE_CODE_SESSION_ID") and not os.environ.get("CODEX_THREAD_ID")
        agent = args.agent or ("claude" if in_claude and not codex_options else "codex")
        if agent == "claude":
            if codex_options:
                parser.error(", ".join(codex_options) + " apply only to --agent codex")
            target = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID")
            if not target:
                parser.error("--session is required outside a Claude Code session")
            try:
                uuid.UUID(target)
            except ValueError:
                parser.error("--session must be an exact session UUID")
            executable = None
        else:
            if args.session:
                parser.error("--session applies only to --agent claude")
            if not args.remote:
                parser.error("--remote is required for --agent codex")
            target = args.thread or os.environ.get("CODEX_THREAD_ID")
            if not target:
                parser.error("--thread is required outside a Codex thread")
            try:
                uuid.UUID(target)
            except ValueError:
                parser.error("--thread must be an exact thread UUID")
            executable = shutil.which(args.codex or "codex")
            if not executable:
                parser.error("codex executable not found")
        if args.command == "pbs":
            if not JOB_ID.fullmatch(args.job) or not args.helper.is_file():
                parser.error("invalid job ID or missing qstat helper")
            # A mistyped or expired ID would otherwise leave the agent waiting indefinitely.
            if pbs_observation({"job_id": args.job, "helper": str(args.helper.resolve())})[1]["status"] == "UNKNOWN":
                parser.error("job is absent from active jobs and 31-day history")
        event_id = str(uuid.uuid4())
        directory = args.state_root.resolve() / event_id
        now = time.time()
        state = dict(schema_version=1, event_id=event_id, kind=args.command, agent=agent,
                     status="waiting", created_at=now, cwd=os.getcwd(), message=args.message,
                     poll_seconds=getattr(args, "poll_seconds", 1))
        if agent == "claude":
            state.update(session_id=target)
        else:
            state.update(thread_id=target, remote=args.remote, codex=executable)
        if args.resume_goal:
            with Rpc(executable, args.remote) as rpc:
                goal, thread_status = goal_view(rpc, args.thread)
            if not goal or goal["status"] != "active":
                parser.error("--resume-goal requires an active Goal; arm before pausing")
            state.update(resume_goal=True, goal_registered=goal, thread_registered_status=thread_status)
        if args.command == "timer":
            state["due_at"] = now + args.seconds
        else:
            state.update(job_id=args.job, helper=str(args.helper.resolve()))
        # Create the event only after every registration check has passed.
        directory.mkdir(parents=True, mode=0o700)
        write_state(directory, state)
        if agent == "claude":
            # Stay attached: the caller runs this as a Claude Code background command, and
            # its exit (carrying the wake-up on stdout) is what resumes the idle session.
            update(directory, lambda s: s.update(worker_pid=os.getpid()))
            print(json.dumps({"event": str(directory), "worker_pid": os.getpid(),
                              "due_at": state.get("due_at")}), flush=True)
            worker(directory)
            final = read_state(directory)
            if final["status"] == "cancelled":
                print(f"job-wakeup: event {event_id} cancelled"
                      + (f": {final['cancellation_reason']}" if final.get("cancellation_reason") else ""))
            return
        with open(directory / "worker.log", "ab", buffering=0) as log:
            process = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()),
                                        "run", str(directory)], stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True, close_fds=True)
        update(directory, lambda s: s.update(worker_pid=process.pid))
        print(json.dumps({"event": str(directory), "worker_pid": process.pid, "due_at": state.get("due_at")}))
    elif args.command == "run":
        worker(args.event.resolve())
    elif args.command == "status":
        print(json.dumps(read_state(args.event), ensure_ascii=False, indent=2))
    else:
        def change(state):
            if args.command == "ack":
                if event_agent(state) == "claude":
                    if os.environ.get("CLAUDE_CODE_SESSION_ID") != state["session_id"]:
                        raise RuntimeError("ack must run inside the registered Claude Code session")
                elif os.environ.get("CODEX_THREAD_ID") != state["thread_id"]:
                    raise RuntimeError("ack must run inside the registered Codex thread")
                if state["status"] not in {"sending", "delivered", "uncertain", "acknowledged"}:
                    raise RuntimeError("event has not been sent")
                state.setdefault("acknowledged_at", time.time())
                state["status"] = "acknowledged"
            else:
                if state["status"] not in {"waiting", "ready", "cancelled"}:
                    raise RuntimeError("delivery already started; cannot retract message")
                state.update(status="cancelled", cancelled_at=time.time())
        print(json.dumps(update(args.event, change), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"job-wakeup: {exc}", file=sys.stderr)
        sys.exit(1)
