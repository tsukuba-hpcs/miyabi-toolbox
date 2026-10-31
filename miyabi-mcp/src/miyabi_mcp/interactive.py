"""Policy-checked interactive allocations; the caller's PTY owns their lifetime.

MCP prepares a plan. The dedicated CLI submits in a terminal, records the job as
soon as qsub prints its ID, then relays that same terminal until qsub exits.
There is no login shell behind qsub in the relay and no detached terminal daemon.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import re
import select
import shlex
import signal
import sys
import termios
import time
import tty
from pathlib import Path

from . import context, parse, submit
from .config import LAUNCHER, Settings
from .ledger import Store, job_num, submit_lock
from .pbs import Completed, Pbs
from .util import MiyabiError, compact, hms_to_seconds, ok, read_json, within


def check_command(settings: Settings, system: str, job_id: str | None = None) -> str:
    args = {"target_system": system}
    if job_id:
        args["expected_job_id"] = job_id
    group = ["--group", settings.group] if settings.group else []
    return shlex.join([str(LAUNCHER), *group, "--project-dir", str(settings.project_dir),
                       "call", "allocation_check", compact(args)])


def plan(settings: Settings, args: dict) -> dict:
    if not isinstance(args.get("request_key"), str) or not args["request_key"].strip():
        raise MiyabiError("invalid_arguments", "request_key is required; reuse it for retries of the same allocation")
    result = submit.submit(settings, {**args, "interactive": True, "dry_run": True})
    result["next"] = "fix the failed checks"
    if result["valid"]:
        group = ["--group", settings.group] if settings.group else []
        launch = "exec " + shlex.join([str(LAUNCHER), *group, "--project-dir", str(settings.project_dir),
                                       "interactive-start", result["plan_digest"]])
        result.update(
            launch_command=launch,
            terminal_open={"host": settings.config.get("remote_alias", "miyabi-g"),
                           "cwd": result["plan"]["workdir"], "command": launch, "timeout": 10},
            allocation_check_command=check_command(settings, result["plan"]["system"]),
            next="Run launch_command in pi-env-mcp terminal_open (adjust host to your configured login alias). "
                 "Keep terminal_id. Read until the compute shell is ready, then run allocation_check_command "
                 "in that terminal and require ok=true before workloads. Use job_get/job_wait for scheduler "
                 "state; terminal_send/terminal_read for this shell. exit releases the allocation and closes "
                 "this terminal. For cancellation use job_cancel dry-run/commit, then terminal_close. "
                 "A timeout from a terminal tool does not cancel the allocation; do not relaunch.")
    return result


def allocation_check(settings: Settings, args: dict) -> dict:
    """Check this process's shell, not the shell of an MCP client on another host."""
    system = args.get("target_system")
    if system not in context.ARCHITECTURES:
        raise MiyabiError("invalid_arguments", "target_system must be Miyabi-G or Miyabi-C")
    evidence = dict(context.inspect(target_system=system))
    reasons = list(evidence["reasons"])
    job_id = evidence["pbs_job_id"]
    expected = args.get("expected_job_id")
    if expected and (not job_id or job_num(job_id) != job_num(expected)):
        reasons.append("PBS_JOBID does not match expected_job_id")
    fields = None
    remaining = None
    if evidence["runtime_execution_allowed"] and not reasons:
        details = Pbs(settings).detail([job_id])  # fresh, active-only evidence
        fields = next((v for k, v in details.items() if job_num(k) == job_num(job_id)), None)
        if not fields or fields.get("job_state") != "R":
            reasons.append("The allocation is not currently RUNNING in PBS")
        else:
            if fields.get("Job_Owner", "").split("@", 1)[0] != settings.user:
                reasons.append("The allocation belongs to another user")
            hosts = {part.split("/", 1)[0].split(".", 1)[0]
                     for part in fields.get("exec_host", "").split("+")}
            if evidence["hostname"].split(".", 1)[0] not in hosts:
                reasons.append("Current host is absent from PBS exec_host")
            variables = parse.variable_list(fields.get("Variable_List"))
            if variables.get("MIYABI_PROJECT_DIR") not in {None, str(settings.project_dir)}:
                reasons.append("The allocation belongs to a different project")
            limit = hms_to_seconds(fields.get("Resource_List.walltime"))
            used = hms_to_seconds(fields.get("resources_used.walltime"))
            remaining = max(0, limit - used) if limit is not None and used is not None else None
            if remaining == 0:
                reasons.append("The allocation has no remaining walltime")
    evidence.update(runtime_execution_allowed=not reasons, reasons=reasons)
    result = ok(context=evidence, job_state=(fields or {}).get("job_state"), remaining_walltime_s=remaining)
    if reasons:
        result.update(ok=False, error={"type": "allocation_unconfirmed", "message": "; ".join(reasons)})
    return result


class TerminalDisconnected(Exception):
    """Must escape submit's uncertain-command recovery so a hangup stops the relay."""


class TerminalQsub:
    """A single qsub process with a controlling PTY and a durable raw transcript."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.pid = None
        self.fd = None
        self.log = None
        self.job_id = None
        self.returncode = None

    def resize(self, *_):
        if self.fd is not None and sys.stdin.isatty():
            size = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, size)

    def read(self) -> bytes:
        try:
            data = os.read(self.fd, 65536)
        except OSError as exc:
            if exc.errno != errno.EIO:  # PTY EOF on Linux
                raise
            data = b""
        if data:
            self.log.write(data)
            self.log.flush()
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        return data

    def __call__(self, argv: list[str], *, cwd: Path, output: Path) -> Completed:
        self.log = output.open("ab")
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.chdir(cwd)
                command = self.settings.command("qsub")
                os.execve(command, [command, *argv], dict(os.environ, LC_ALL="C"))
            except OSError as exc:
                os.write(2, f"qsub: {exc}\n".encode())
                os._exit(127)
        self.resize()
        started = time.monotonic()
        output_text = ""
        while time.monotonic() - started < 30:
            readable, _, _ = select.select([self.fd], [], [], 0.2)
            if not readable:
                continue
            data = self.read()
            if not data:
                _, status = os.waitpid(self.pid, 0)
                self.returncode = os.waitstatus_to_exitcode(status)
                return Completed(["qsub", *argv], self.returncode, output_text, output_text,
                                 time.monotonic() - started)
            output_text = (output_text + data.decode("utf-8", errors="replace"))[-65536:]
            match = re.search(r"qsub: (?:waiting for job|job) (\d+(?:\[\])?\.[A-Za-z0-9_.-]+)(?: to start| ready)",
                              output_text)
            self.job_id = match.group(1) if match else submit.parse_job_id(output_text)
            if self.job_id:
                return Completed(["qsub", *argv], 0, self.job_id + "\n", "", time.monotonic() - started)
        raise MiyabiError("timeout", "qsub has not reported a job ID after 30 s; reconcile before retrying")

    def relay(self) -> int:
        previous = termios.tcgetattr(sys.stdin.fileno())
        old_resize = signal.signal(signal.SIGWINCH, self.resize)
        try:
            # Do not flush commands typed as soon as the accepted-job result appears.
            tty.setraw(sys.stdin.fileno(), termios.TCSANOW)
            while True:
                readable, _, _ = select.select([self.fd, sys.stdin.fileno()], [], [])
                if self.fd in readable and not self.read():
                    break
                if sys.stdin.fileno() in readable:
                    data = os.read(sys.stdin.fileno(), 65536)
                    if not data:
                        raise TerminalDisconnected("the owning terminal disconnected")
                    while data:
                        data = data[os.write(self.fd, data):]
            _, status = os.waitpid(self.pid, 0)
            self.returncode = os.waitstatus_to_exitcode(status)
            return self.returncode
        finally:
            signal.signal(signal.SIGWINCH, old_resize)
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, previous)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.pid and self.returncode is None:
            # Closing the master hangs up qsub. Bound cleanup even if it ignores HUP.
            for sig in (signal.SIGHUP, signal.SIGKILL):
                try:
                    os.kill(self.pid, sig)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    found, status = os.waitpid(self.pid, os.WNOHANG)
                    if found:
                        self.returncode = os.waitstatus_to_exitcode(status)
                        break
                    time.sleep(0.05)
                if self.returncode is not None:
                    break
        if self.log:
            self.log.close()


def start(settings: Settings, plan_digest: str) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise MiyabiError("terminal_required", "interactive-start requires a persistent PTY, e.g. pi-env-mcp terminal_open")
    if context.inspect()["node_role"] != "login":
        raise MiyabiError("wrong_plane", "start the interactive allocation on a Miyabi login node")
    if not re.fullmatch(r"blake2b:[a-f0-9]+", plan_digest):
        raise MiyabiError("invalid_arguments", "invalid plan_digest")
    store = Store(settings)
    saved = read_json(store.plan_path(plan_digest))
    if not saved or not saved["plan"].get("interactive") or not within(saved.get("created_at"), submit.PLAN_TTL_S):
        raise MiyabiError("plan_expired", "unknown or expired interactive plan; call interactive_plan again")
    terminal = TerminalQsub(settings)
    completed = False
    result = {}

    def disconnected(signum, _frame):
        raise TerminalDisconnected(f"terminal runner received signal {signum}")

    handlers = {sig: signal.signal(sig, disconnected) for sig in (signal.SIGHUP, signal.SIGTERM)}
    try:
        result = submit.submit(settings, {**saved["arguments"], "dry_run": False, "plan_digest": plan_digest},
                               interactive_qsub=terminal)
        result["allocation_check_command"] = check_command(settings, saved["plan"]["system"], result["job_id"])
        print("\n" + compact(result), flush=True)
        if terminal.pid is None:  # idempotent retry: never opens a second allocation
            print("Use the original terminal_id; a lost terminal cannot be reattached. Check job_get before retrying.",
                  flush=True)
            return 0
        status = terminal.returncode if terminal.returncode is not None else terminal.relay()
        completed = True
        return status if status >= 0 else 128 - status
    except (TerminalDisconnected, KeyboardInterrupt) as exc:
        raise MiyabiError("terminal_disconnected", str(exc) or "terminal runner interrupted") from None
    finally:
        terminal.close()
        job_id = result.get("job_id") or terminal.job_id
        if not completed and terminal.pid is not None:
            # On disconnect, clean up exactly this allocation. Never cancel unrelated jobs.
            try:
                if not job_id:
                    with submit_lock(settings):
                        request = read_json(store.request_path(saved["arguments"]["request_key"]))
                        record = store.read_submission(request["submit_id"]) if request else None
                        job_id = (record.get("job_id") or submit.reconcile(settings, record)) if record else None
                        if job_id and not record.get("job_id"):
                            submit._bind(settings, store, record, job_id)
                            submit._first_event(settings, store, job_id)
                    if not job_id and record:
                        print(compact({"cleanup_error": "submission not yet found; reconcile this request_key",
                                       "submit_id": record["submit_id"]}), file=sys.stderr)
                if job_id and Pbs(settings).detail([job_id]):
                    if store.find(job_id):
                        store.note(job_id, "cancel_requested", "qdel requested", note="interactive terminal disconnected")
                    cancellation = Pbs(settings).qdel([job_id])
                    if cancellation.returncode:
                        print(compact({"cleanup_error": cancellation.stderr, "job_id": job_id}), file=sys.stderr)
            except MiyabiError as exc:
                print(compact({"cleanup_error": str(exc), "job_id": job_id}), file=sys.stderr)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
