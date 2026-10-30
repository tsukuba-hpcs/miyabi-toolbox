"""Fixed site commands run as argv lists (never through a shell), with timeouts and kept diagnostics."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import parse
from .config import Settings
from .util import MiyabiError, diagnostic

DETAIL_BATCH = 40


@dataclass
class Completed:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    seconds: float

    def failure(self, message: str) -> MiyabiError:
        return MiyabiError("command_error", message, command=self.argv, returncode=self.returncode,
                           stdout=diagnostic(self.stdout), stderr=diagnostic(self.stderr))


def run(argv: list[str], timeout: float = 30, cwd: Path | None = None) -> Completed:
    env = dict(os.environ, LC_ALL="C")
    started = time.monotonic()
    try:
        process = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=timeout, cwd=cwd, env=env, stdin=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        raise MiyabiError("command_not_found", f"{argv[0]} not found; run on a Miyabi login node",
                          command=argv) from None
    except subprocess.TimeoutExpired as exc:
        raise MiyabiError("timeout", f"{Path(argv[0]).name} exceeded {timeout:g} s", command=argv,
                          stdout=diagnostic(exc.stdout), stderr=diagnostic(exc.stderr)) from None
    except OSError as exc:
        raise MiyabiError("command_error", str(exc), command=argv) from None
    return Completed(argv, process.returncode, process.stdout, process.stderr, time.monotonic() - started)


def _empty(text: str) -> bool:
    return any(line.strip() in parse.EMPTY_MESSAGES for line in text.splitlines())


class Pbs:
    def __init__(self, settings: Settings):
        self.settings = settings

    def qstat(self, args: list[str], timeout: float = 30) -> Completed:
        return run([self.settings.command("qstat"), *args], timeout)

    def active(self) -> dict:
        """The user's unfinished jobs (`qstat -ll`) with the scheduled-stop banner."""
        result = self.qstat(["-ll"])
        if result.returncode != 0:
            raise result.failure("qstat -ll failed")
        return {"jobs": parse.parse_table(result.stdout), "scheduled_stop": parse.scheduled_stop(result.stdout),
                "command": result.argv}

    def history(self, days: int, limit: int, ids: Iterable[str] = ()) -> list[dict]:
        result = self.qstat(["-H", "-ll", "--hday", str(days), "--hnum", str(limit), *ids], timeout=60)
        if result.returncode != 0:
            raise result.failure("qstat -H -ll failed")
        return parse.parse_table(result.stdout)

    def detail(self, ids: list[str], history: bool = False) -> dict[str, dict[str, str]]:
        """`qstat [-H] -f` records keyed by full job ID. Jobs not printed are simply absent."""
        records: dict[str, dict[str, str]] = {}
        for start in range(0, len(ids), DETAIL_BATCH):
            batch = ids[start:start + DETAIL_BATCH]
            result = self.qstat((["-H"] if history else []) + ["-f", *batch], timeout=60)
            found = parse.parse_detail(result.stdout)
            if result.returncode != 0 and not found and not _empty(result.stdout + result.stderr):
                raise result.failure(f"qstat {'-H ' if history else ''}-f failed")
            records.update(found)
        return records

    def limits(self) -> dict:
        result = self.qstat(["--limit"])
        if result.returncode != 0:
            raise result.failure("qstat --limit failed")
        return {"limits": parse.parse_limit(result.stdout), "scheduled_stop": parse.scheduled_stop(result.stdout)}

    def queues(self) -> dict:
        result = self.qstat(["--rsc", "-x"])
        if result.returncode != 0:
            raise result.failure("qstat --rsc -x failed")
        return {"queues": parse.parse_rsc(result.stdout), "scheduled_stop": parse.scheduled_stop(result.stdout)}

    def occupancy(self) -> list[dict]:
        result = self.qstat(["--rscuse"])
        if result.returncode != 0:
            raise result.failure("qstat --rscuse failed")
        return parse.parse_rscuse(result.stdout)

    def tokens(self) -> list[dict]:
        result = run([self.settings.command("show_token")])
        if result.returncode != 0:
            raise result.failure("show_token failed")
        return parse.parse_show_token(result.stdout)

    def qsub(self, args: list[str], cwd: Path, timeout: float = 60) -> Completed:
        return run([self.settings.command("qsub"), *args], timeout, cwd=cwd)

    def qdel(self, ids: list[str], timeout: float = 60) -> Completed:
        return run([self.settings.command("qdel"), *ids], timeout)
