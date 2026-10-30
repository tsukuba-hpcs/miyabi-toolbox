"""The notification daemon on the agent's machine, and the listener for Claude Code (DESIGN.md 9.3, 9.4).

An agent starts the daemon after submitting (`notifyd ensure`): it polls miyabi-mcp every 5 minutes,
delivers on this machine, reports back, and exits after two polls with nothing left to deliver. On a
Miyabi login node it calls miyabi-mcp directly; elsewhere it runs `ssh <alias> <launcher> notify poll`.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config, deliver as delivery
from .config import LAUNCHER
from .context import identify_host
from .util import Lock, MiyabiError, compact, now_iso, ok, read_json, write_json

DEFAULT_POLL_SECONDS = 300


@dataclass
class Options:
    notifier: str
    remote: str | None = None            # ssh alias of the login node; None when running on it
    remote_launcher: str | None = None   # bin/miyabi-mcp on the login node
    poll_seconds: int = DEFAULT_POLL_SECONDS
    idle_polls: int = 2
    fail_notify_after: int = 3

    def argv(self) -> list[str]:
        argv = ["--notifier", self.notifier, "--poll-seconds", str(self.poll_seconds),
                "--idle-polls", str(self.idle_polls), "--fail-notify-after", str(self.fail_notify_after)]
        if self.remote:
            argv += ["--remote", self.remote, "--remote-launcher", self.remote_launcher or "miyabi-mcp"]
        return argv


def short_host() -> str:
    return socket.gethostname().split(".", 1)[0]


def on_miyabi() -> bool:
    return identify_host(socket.gethostname())[0] != "unknown" or bool(os.environ.get("MIYABI_MCP_WORK"))


def state_dir() -> Path:
    """On Miyabi under /work/<group>/<user>/.miyabi-mcp/notifyd (never $HOME); elsewhere the XDG state dir."""
    if os.environ.get("MIYABI_MCP_NOTIFYD_DIR"):
        return Path(os.environ["MIYABI_MCP_NOTIFYD_DIR"])
    if on_miyabi():
        return config.load().user_dir / "notifyd"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "miyabi-mcp" / "notifyd"


def local_commands() -> dict:
    """Named delivery commands of this machine (never taken from a notification)."""
    if on_miyabi():
        path = config.load().user_dir / "config.json"
    else:
        path = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "miyabi-mcp" / "config.json"
    return (read_json(path, {}) or {}).get("deliver_commands", {})


# -- talking to miyabi-mcp ----------------------------------------------------------------------------------------

def _remote(opts: Options, args: list[str], timeout: float = 120) -> dict:
    ssh = os.environ.get("MIYABI_MCP_SSH", "ssh")
    # ssh hands the remote shell one command line: quote every argument (JSON outcomes contain quotes).
    command = shlex.join([opts.remote_launcher or "miyabi-mcp", *args])
    argv = [ssh, "-T", "-o", "BatchMode=yes", opts.remote, command]
    try:
        process = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                                 stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MiyabiError("transport", f"ssh to {opts.remote} failed: {exc}") from None
    try:
        data = json.loads(process.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise MiyabiError("transport", f"ssh to {opts.remote} failed (exit {process.returncode}): "
                          f"{(process.stderr or process.stdout).strip()[-500:]}") from None
    if not data.get("ok"):
        error = data.get("error") or {}
        raise MiyabiError(error.get("type", "remote_error"), error.get("message", "miyabi-mcp failed"))
    return data


def poll(opts: Options) -> dict:
    if opts.remote:
        return _remote(opts, ["notify", "poll", "--notifier", opts.notifier, "--caller", short_host()])
    from . import notify

    return notify.poll(config.load(), opts.notifier, short_host())


def report(opts: Options, project_dir: str, msg_id: str, outcome: dict) -> None:
    if opts.remote:
        _remote(opts, ["--project-dir", project_dir, "notify", "report", "--msg", msg_id, "--outcome", compact(outcome)])
        return
    from . import notify

    notify.report(config.load(project_dir=project_dir), msg_id, outcome)


# -- the daemon -----------------------------------------------------------------------------------------------------

def _paths(opts: Options) -> dict[str, Path]:
    directory = state_dir()
    return {"dir": directory, "state": directory / f"{opts.notifier}.json", "log": directory / f"{opts.notifier}.log",
            "lock": directory / "locks" / opts.notifier, "known": directory / f"known-{opts.notifier}.json",
            "stop": directory / f"{opts.notifier}.stop"}


def _alive(state: dict | None) -> bool:
    if not state or state.get("status") != "running" or state.get("host") != short_host():
        return False
    try:
        os.kill(int(state["pid"]), 0)
    except (OSError, ValueError, KeyError):
        return False
    return True


def status(opts: Options) -> dict:
    paths = _paths(opts)
    state = read_json(paths["state"])
    return ok(running=_alive(state), state=state, state_dir=str(paths["dir"]), log=str(paths["log"]))


def ensure(opts: Options) -> dict:
    """Start the daemon unless it already runs; return its status."""
    paths = _paths(opts)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    state = read_json(paths["state"])
    if _alive(state):
        return ok(running=True, started=False, pid=state["pid"], notifier=opts.notifier, log=str(paths["log"]))
    paths["stop"].unlink(missing_ok=True)
    with open(paths["log"], "ab", buffering=0) as log:
        process = subprocess.Popen([str(LAUNCHER), "notifyd", "run", *opts.argv()], stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = read_json(paths["state"])
        if state and state.get("pid") == process.pid and state.get("status") in {"running", "exited_idle"}:
            return ok(running=True, started=True, pid=process.pid, notifier=opts.notifier, log=str(paths["log"]))
        if process.poll() is not None:
            state = read_json(paths["state"])
            if _alive(state):  # another daemon won the lock
                return ok(running=True, started=False, pid=state["pid"], notifier=opts.notifier, log=str(paths["log"]))
            raise MiyabiError("daemon_failed", f"the notification daemon exited ({process.returncode}); see {paths['log']}")
        time.sleep(0.2)
    raise MiyabiError("daemon_failed", f"the notification daemon did not start within 15 s; see {paths['log']}")


def stop(opts: Options) -> dict:
    paths = _paths(opts)
    paths["stop"].parent.mkdir(parents=True, exist_ok=True)
    paths["stop"].write_text(now_iso())
    state = read_json(paths["state"])
    if _alive(state):
        os.kill(int(state["pid"]), signal.SIGTERM)
    return ok(stopping=_alive(state))


def _log(paths: dict, **fields) -> None:
    with open(paths["log"], "a", encoding="utf-8") as stream:
        stream.write(compact({"at": now_iso(), **fields}) + "\n")


def _broken(opts: Options, paths: dict, known: dict, error: Exception, commands: dict) -> None:
    """Tell every known target that notifications are not getting through."""
    text = (f"[miyabi-mcp] notification channel broken on {opts.notifier}: {error}. Job notifications are not "
            f"being delivered; the daemon keeps retrying every {opts.poll_seconds} s. "
            + (f"If ssh to {opts.remote} fails, reopen its master connection (ssh -fN {opts.remote}). " if opts.remote else "")
            + "Check jobs directly with job_list meanwhile.")
    stamp = int(time.time())
    for notification_id, target in known.items():
        outcome = delivery.send(target, text, state_dir=paths["dir"], notification_id=notification_id,
                                msg_id=f"{notification_id}:broken-{stamp}", commands=commands)
        _log(paths, event="broken_notice", notification=notification_id, outcome=outcome)


def run(opts: Options) -> int:
    paths = _paths(opts)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    lock = Lock(paths["lock"], lease=3 * opts.poll_seconds + 120, timeout=0)
    try:
        lock.acquire()
    except MiyabiError:
        print(f"notifyd for {opts.notifier} is already running: {lock.holder()}", flush=True)
        return 0
    stopping = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stopping.update(flag=True))
    state = {"notifier": opts.notifier, "pid": os.getpid(), "host": short_host(), "started_at": now_iso(),
             "remote": opts.remote, "poll_seconds": opts.poll_seconds, "status": "running", "failures": 0}
    write_json(paths["state"], state)
    commands = local_commands()
    known = read_json(paths["known"], {}) or {}
    idle, failures, broken_sent = 0, 0, False
    try:
        while not stopping["flag"] and not paths["stop"].exists():
            try:
                result = poll(opts)
                failures, broken_sent = 0, False
                for message in result.get("messages", []):
                    outcome = delivery.send(message["deliver"], message["message"], state_dir=paths["dir"],
                                            notification_id=message["notification_id"], msg_id=message["msg_id"],
                                            commands=commands)
                    report(opts, message["project_dir"], message["msg_id"], outcome)
                    _log(paths, event="delivered", msg_id=message["msg_id"], outcome=outcome)
                known = result.get("targets") or {}
                write_json(paths["known"], known)
                idle = idle + 1 if not result.get("open_count") else 0
                state.update(last_poll_at=now_iso(), last_ok_at=now_iso(), failures=0, last_error=None,
                             open=result.get("open_count"), polls=state.get("polls", 0) + 1)
                if idle >= opts.idle_polls:
                    state["status"] = "exited_idle"
                    break
            except (MiyabiError, OSError, subprocess.TimeoutExpired) as exc:
                failures += 1
                state.update(last_poll_at=now_iso(), failures=failures, last_error=str(exc),
                             polls=state.get("polls", 0) + 1)
                _log(paths, event="poll_failed", failures=failures, error=str(exc))
                if failures >= opts.fail_notify_after and not broken_sent:
                    _broken(opts, paths, known, exc, commands)
                    broken_sent = True
            write_json(paths["state"], state)
            end = time.monotonic() + opts.poll_seconds * 2 ** min(failures, 3)
            touched = time.monotonic()
            while time.monotonic() < end and not stopping["flag"] and not paths["stop"].exists():
                time.sleep(min(1.0, max(0.0, end - time.monotonic())))
                if time.monotonic() - touched > 30:
                    os.utime(lock.path)
                    touched = time.monotonic()
    finally:
        if state["status"] == "running":
            state["status"] = "stopped"
        state["stopped_at"] = now_iso()
        write_json(paths["state"], state)
        paths["stop"].unlink(missing_ok=True)
        lock.release()
    return 0


# -- the listener (Claude Code) --------------------------------------------------------------------------------------

def listen(notification_id: str, opts: Options) -> int:
    """Wait for a message of this notification in the local inbox; print it and exit 0.

    Exit 3 when the notification is no longer open on the server (cancelled or already delivered),
    75 when the daemon cannot be kept running.
    """
    try:
        ensure(opts)
    except MiyabiError as exc:
        print(f"[miyabi-mcp] cannot start the notification daemon: {exc.message}", flush=True)
        return 75
    paths = _paths(opts)
    box = delivery.inbox(paths["dir"], notification_id)
    started = time.time()
    last_check = time.monotonic()
    while True:
        for path in sorted(box.glob("*.json")):
            try:
                os.rename(path, path.with_suffix(".shown"))
            except FileNotFoundError:
                continue  # another listener took it
            print(json.loads(path.with_suffix(".shown").read_text())["message"], flush=True)
            return 0
        if time.monotonic() - last_check > 60:
            last_check = time.monotonic()
            state = read_json(paths["state"]) or {}
            polled_after = bool(state.get("last_ok_at")) and \
                datetime.fromisoformat(state["last_ok_at"]).timestamp() > started
            known = read_json(paths["known"], {}) or {}
            if polled_after and notification_id not in known and not any(box.glob("*.json")):
                print(f"[miyabi-mcp] notification {notification_id} is no longer open (cancelled, already "
                      "delivered or unknown to this notifier); check notify_list", flush=True)
                return 3
            try:
                ensure(opts)
            except MiyabiError as exc:
                print(f"[miyabi-mcp] the notification daemon stopped and cannot restart: {exc.message}", flush=True)
                return 75
        time.sleep(2)
