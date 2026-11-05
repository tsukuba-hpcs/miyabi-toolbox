"""The notification daemon on the agent's machine, and the listener for Claude Code (DESIGN.md 9.3, 9.4).

An agent starts the daemon after submitting (`notifyd ensure`): it polls miyabi-mcp every --poll-seconds
(the server's notify_poll_seconds, written into the commands it returns), delivers on this machine,
reports back, and exits after two polls with nothing left to deliver, telling the server why it exited.
On a Miyabi login node it calls miyabi-mcp directly; elsewhere it runs `ssh <alias> <launcher> notify poll`.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from . import config, deliver as delivery
from .config import LAUNCHER
from .context import identify_host
from .util import Lock, MiyabiError, compact, now, now_iso, ok, read_json, write_json

DEFAULT_POLL_SECONDS = config.NOTIFY_POLL_SECONDS
INBOX_KEEP_S = 7 * 86400   # an inbox is removed this long after its notification was acknowledged or closed
CLEAN_EVERY_S = 3600
TRANSIENT = (MiyabiError, OSError, subprocess.TimeoutExpired)


@dataclass
class Options:
    notifier: str
    remote: str | None = None            # ssh alias of the login node; None when running on it
    remote_launcher: str | None = None   # bin/miyabi-mcp on the login node
    poll_seconds: int = DEFAULT_POLL_SECONDS
    idle_polls: int = 2
    fail_notify_after: int = 3
    started_by: str = "run"              # ensure, listen or run: recorded when the daemon starts

    def argv(self) -> list[str]:
        argv = ["--notifier", self.notifier, "--poll-seconds", str(self.poll_seconds),
                "--idle-polls", str(self.idle_polls), "--fail-notify-after", str(self.fail_notify_after),
                "--started-by", self.started_by]
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


def poll(opts: Options, instance: str | None = None, exiting: str | None = None) -> dict:
    """One poll; `exiting` turns it into this daemon's exit report (idle, stopped or error)."""
    if opts.remote:
        extra = ["--interval", str(opts.poll_seconds)] + (["--instance", instance] if instance else []) \
            + (["--exiting", exiting] if exiting else [])
        return _remote(opts, ["notify", "poll", "--notifier", opts.notifier, "--caller", short_host(), *extra])
    from . import notify

    return notify.poll(config.load(), opts.notifier, short_host(), interval=opts.poll_seconds, instance=instance,
                       exiting=exiting)


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
            "closed": directory / f"closed-{opts.notifier}.json", "stop": directory / f"{opts.notifier}.stop"}


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


def _running(opts: Options, paths: dict, state: dict) -> dict:
    result = ok(running=True, started=False, pid=state["pid"], notifier=opts.notifier,
                poll_seconds=state.get("poll_seconds"), log=str(paths["log"]))
    if state.get("poll_seconds") != opts.poll_seconds:
        # A running daemon keeps its interval: say so instead of pretending the request was applied.
        result["requested_poll_seconds"] = opts.poll_seconds
        result["note"] = (f"the running daemon polls every {state.get('poll_seconds')} s and keeps that until it "
                          f"exits; to poll every {opts.poll_seconds} s run `notifyd stop`, then this command again")
    return result


def ensure(opts: Options, by: str = "ensure") -> dict:
    """Start the daemon unless one already runs here; return its status.

    A daemon that exits at once without taking over found the lock held: by a predecessor that is
    finishing its exit (or resumed at its exit report). Look again and, if none runs, start another.
    """
    paths = _paths(opts)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    for _attempt in range(3):
        state = read_json(paths["state"])
        if _alive(state):
            return _running(opts, paths, state)
        paths["stop"].unlink(missing_ok=True)
        with open(paths["log"], "ab", buffering=0) as log:
            process = subprocess.Popen([str(LAUNCHER), "notifyd", "run", *replace(opts, started_by=by).argv()],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                                       close_fds=True)
        deadline = time.monotonic() + 15
        while True:
            state = read_json(paths["state"]) or {}
            if state.get("pid") == process.pid and state.get("status") in {"running", "exiting", "exited_idle"}:
                return ok(running=True, started=True, pid=process.pid, notifier=opts.notifier,
                          poll_seconds=opts.poll_seconds, log=str(paths["log"]))
            if process.poll() is not None:
                break
            if time.monotonic() > deadline:
                raise MiyabiError("daemon_failed", f"the notification daemon did not start within 15 s; see {paths['log']}")
            time.sleep(0.2)
        if process.returncode != 0:
            raise MiyabiError("daemon_failed", f"the notification daemon exited ({process.returncode}); see {paths['log']}")
        time.sleep(0.5)
    state = read_json(paths["state"])
    if _alive(state):
        return _running(opts, paths, state)
    holder = Lock(paths["lock"]).holder()
    raise MiyabiError("daemon_failed", f"no notification daemon runs, and a new one cannot take the lock {paths['lock']} "
                      f"(held by {holder!r}); a lock its holder stopped refreshing frees itself after "
                      f"{3 * opts.poll_seconds + 120} s. See {paths['log']}")


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


def _broken(opts: Options, paths: dict, known: dict, error: str, commands: dict) -> None:
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


def _clean_inboxes(paths: dict, closed: dict) -> None:
    """Remove the inbox of a notification acknowledged, cancelled or dismissed at least a week ago.
    A delivered one that nobody acknowledged keeps its inbox: its message may be unread."""
    cutoff = now() - timedelta(seconds=INBOX_KEEP_S)
    for notification_id, info in closed.items():
        if info.get("status") not in {"acknowledged", "cancelled", "dismissed"}:
            continue
        if not info.get("at") or datetime.fromisoformat(info["at"]) > cutoff:
            continue
        box = delivery.inbox(paths["dir"], notification_id)
        if box.is_dir():
            shutil.rmtree(box, ignore_errors=True)
            _log(paths, event="inbox_removed", notification=notification_id, status=info["status"])


def _hand_over(opts: Options, paths: dict, lock: Lock, state: dict, instance: str) -> bool:
    """Leave after idle polls without stranding a notification registered meanwhile; True to leave.

    The local state says `exiting` and the lock is free before the server hears of the exit, so an agent
    that registers a notification now starts a new daemon (ensure does not count an exiting one). If the
    exit report finds open notifications, or cannot be sent, this daemon carries on, unless a new one
    already holds the lock. Every write of the state file happens under the lock.
    """
    state["status"] = "exiting"
    write_json(paths["state"], state)
    lock.release()
    try:
        result = poll(opts, instance, exiting="idle")
        busy = bool(result.get("open_count") or result.get("messages") or result.get("errors"))
    except TRANSIENT as exc:
        _log(paths, event="exit_report_failed", error=str(exc))
        busy = True
    try:
        lock.acquire()
    except MiyabiError:
        _log(paths, event="handed_over", holder=lock.holder())
        return True  # a newer daemon took over: leave its state file alone
    if not busy:
        return True
    state["status"] = "running"
    write_json(paths["state"], state)
    _log(paths, event="resumed", reason="work found at the exit report")
    return False


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
    instance = f"{short_host()}:{os.getpid()}:{int(time.time())}"
    state = {"notifier": opts.notifier, "pid": os.getpid(), "host": short_host(), "instance": instance,
             "started_at": now_iso(), "started_by": opts.started_by, "remote": opts.remote,
             "poll_seconds": opts.poll_seconds, "status": "running", "failures": 0}
    write_json(paths["state"], state)
    _log(paths, event="started", pid=os.getpid(), host=short_host(), poll_seconds=opts.poll_seconds,
         started_by=opts.started_by, remote=opts.remote)
    commands = local_commands()
    known = read_json(paths["known"], {}) or {}
    closed = read_json(paths["closed"], {}) or {}
    idle, failures, troubled, broken_sent = 0, 0, 0, False
    reason, cleaned = "error", None
    try:
        while True:
            if stopping["flag"] or paths["stop"].exists():
                reason = "stopped"
                break
            try:
                result = poll(opts, instance)
                failures = 0
                for message in result.get("messages", []):
                    outcome = delivery.send(message["deliver"], message["message"], state_dir=paths["dir"],
                                            notification_id=message["notification_id"], msg_id=message["msg_id"],
                                            commands=commands)
                    report(opts, message["project_dir"], message["msg_id"], outcome)
                    _log(paths, event="delivered", msg_id=message["msg_id"], outcome=outcome)
            except TRANSIENT as exc:
                failures += 1
                troubled += 1
                idle = 0
                state.update(last_poll_at=now_iso(), failures=failures, last_error=str(exc),
                             polls=state.get("polls", 0) + 1)
                _log(paths, event="poll_failed", failures=failures, error=str(exc))
                if troubled >= opts.fail_notify_after and not broken_sent:
                    _broken(opts, paths, known, str(exc), commands)
                    broken_sent = True
            else:
                errors = result.get("errors") or []
                if errors:
                    # Projects that could not be read may still have open notifications: never idle, and keep
                    # what was known about them.
                    troubled += 1
                    idle = 0
                    closed = {**closed, **(result.get("closed") or {})}
                    known = {key: value for key, value in {**known, **(result.get("targets") or {})}.items()
                             if key not in (result.get("closed") or {})}
                    _log(paths, event="poll_errors", errors=errors)
                    if troubled >= opts.fail_notify_after and not broken_sent:
                        first = errors[0]
                        _broken(opts, paths, known, f"{len(errors)} project(s) could not be polled, e.g. "
                                f"{first.get('project')}: {first.get('message')}", commands)
                        broken_sent = True
                else:
                    troubled, broken_sent = 0, False
                    known = result.get("targets") or {}
                    closed = result.get("closed") or {}
                    idle = idle + 1 if not result.get("open_count") else 0
                # closed first: a listener that misses its id in `known` then finds why in `closed`
                write_json(paths["closed"], closed)
                write_json(paths["known"], known)
                if cleaned is None or time.monotonic() - cleaned > CLEAN_EVERY_S:
                    _clean_inboxes(paths, closed)
                    cleaned = time.monotonic()
                state.update(last_poll_at=now_iso(), failures=0, last_error=None, open=result.get("open_count"),
                             errors=len(errors), polls=state.get("polls", 0) + 1)
                if not errors:
                    state["last_ok_at"] = now_iso()
                if idle >= opts.idle_polls:
                    if _hand_over(opts, paths, lock, state, instance):
                        reason = "idle"
                        break
                    idle = 0
                    continue  # resumed: poll again at once, which also tells the server it runs
            write_json(paths["state"], state)
            end = time.monotonic() + opts.poll_seconds * 2 ** min(failures, 3)
            touched = time.monotonic()
            while time.monotonic() < end and not stopping["flag"] and not paths["stop"].exists():
                time.sleep(min(1.0, max(0.0, end - time.monotonic())))
                if time.monotonic() - touched > 30:
                    os.utime(lock.path)
                    touched = time.monotonic()
    except KeyboardInterrupt:
        reason = "stopped"
    finally:
        holding = lock.token is not None
        if holding:
            state["status"] = {"idle": "exited_idle", "stopped": "stopped"}.get(reason, "failed")
            state["stopped_at"] = now_iso()
            write_json(paths["state"], state)
            paths["stop"].unlink(missing_ok=True)
            lock.release()
        _log(paths, event="exited", reason=reason, last_ok_at=state.get("last_ok_at"), handed_over=not holding)
        if reason != "idle":  # an idle exit was reported by _hand_over
            try:
                poll(opts, instance, exiting=reason)
            except TRANSIENT as exc:
                _log(paths, event="exit_report_failed", error=str(exc))
    return 0


# -- the listener (Claude Code) --------------------------------------------------------------------------------------

def _gone(notification_id: str, opts: Options, paths: dict, box: Path) -> str:
    """Why a notification is no longer open for this notifier, from the closed list the daemon keeps."""
    head = f"[miyabi-mcp] notification {notification_id} is no longer open for notifier {opts.notifier}"
    info = (read_json(paths["closed"], {}) or {}).get(notification_id)
    if info is None:
        return (head + ": it is neither open nor recently closed there. It may belong to another notifier, or the "
                "id may be wrong; notify_list shows each notification's notifier.")
    status, at = info.get("status"), info.get("at")
    if status == "cancelled":
        return head + f": it was cancelled at {at}; nothing will be delivered for it."
    if status == "failed":
        return head + f": its delivery failed at {at}; notify_list shows why."
    delivered = info.get("delivered_at")
    if delivered:
        text = head + (f": it was delivered at {delivered} (written to this machine's inbox; that alone does not "
                       "mean an agent read it)")
        if status in {"acknowledged", "dismissed"}:
            text += f", then {status} at {at}"
    else:
        text = head + f": it was {status} at {at}"
    shown = sorted(box.glob("*.shown"), key=lambda path: path.stat().st_mtime)
    if shown:
        taken = datetime.fromtimestamp(shown[-1].stat().st_mtime).astimezone().replace(microsecond=0).isoformat()
        text += f". The inbox here shows a listener took its last message at {taken}."
    else:
        text += ". Its message is not in this machine's inbox."
    return text + " Check notify_list."


def listen(notification_id: str, opts: Options) -> int:
    """Wait for a message of this notification in the local inbox; print it and exit 0.

    Exit 3 when the notification is no longer open for this notifier (the printed reason says whether it
    was cancelled, delivered or closed earlier, failed, or is not this notifier's), 75 when the daemon
    cannot be kept running.
    """
    try:
        ensure(opts, by="listen")
    except MiyabiError as exc:
        print(f"[miyabi-mcp] cannot start the notification daemon: {exc.message}", flush=True)
        return 75
    paths = _paths(opts)
    box = delivery.inbox(paths["dir"], notification_id)
    started = time.time()
    last_check = time.monotonic()
    while True:
        for path in sorted(box.glob("*.json")):
            shown = path.with_suffix(".shown")
            try:
                os.rename(path, shown)
            except FileNotFoundError:
                continue  # another listener took it
            os.utime(shown)  # its mtime now says when a listener took the message
            print(json.loads(shown.read_text())["message"], flush=True)
            return 0
        if time.monotonic() - last_check > 60:
            last_check = time.monotonic()
            state = read_json(paths["state"]) or {}
            polled_after = bool(state.get("last_ok_at")) and \
                datetime.fromisoformat(state["last_ok_at"]).timestamp() > started
            known = read_json(paths["known"], {}) or {}
            if polled_after and notification_id not in known and not any(box.glob("*.json")):
                print(_gone(notification_id, opts, paths, box), flush=True)
                return 3
            try:
                ensure(opts, by="listen")
            except MiyabiError as exc:
                print(f"[miyabi-mcp] the notification daemon stopped and cannot restart: {exc.message}", flush=True)
                return 75
        time.sleep(2)
