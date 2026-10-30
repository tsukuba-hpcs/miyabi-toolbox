"""Shared fixture: a temporary /work tree, the fake PBS on PATH and helpers to move jobs along."""

from __future__ import annotations

import getpass
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures"

from miyabi_mcp import config, tools  # noqa: E402


class FakePbsCase(unittest.TestCase):
    user_config: dict = {"submit_grace_s": 0, "missing_s": 0, "poll_seconds": 1, "interactive_max_age_s": 0}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.user = os.environ.get("USER") or getpass.getuser()
        self.work = root / "work"
        self.user_root = self.work / "gtest" / self.user
        self.project = self.user_root / "proj"
        self.project.mkdir(parents=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        for name in ("qstat", "qsub", "qdel", "show_token"):
            (self.bin / name).symlink_to(TESTS / "fakepbs.py")
        self.state_file = root / "fake.json"
        self.write_state({"next_id": 3800000, "server": "opbs", "qsub_mode": "ok", "jobs": {}})
        self.env = {"MIYABI_MCP_WORK": str(self.work), "MIYABI_MCP_GROUP": "gtest", "MIYABI_MCP_PBS_BIN": str(self.bin),
                    "FAKE_PBS_STATE": str(self.state_file), "TZ": "Asia/Tokyo", "USER": self.user}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        time.tzset()
        user_dir = self.user_root / ".miyabi-mcp"
        user_dir.mkdir(parents=True)
        (user_dir / "config.json").write_text(json.dumps(self.user_config))
        self.script = self.project / "job.pbs"
        self.script.write_text("#!/bin/bash\n#PBS -j oe\nset -euo pipefail\necho hello\n")
        self.addCleanup(self.tmp.cleanup)

    # -- helpers ------------------------------------------------------------------------------------------------
    def settings(self, **kwargs) -> config.Settings:
        return config.load(project_dir=str(kwargs.pop("project_dir", self.project)), **kwargs)

    def call(self, name: str, args: dict | None = None, **kwargs) -> dict:
        return tools.run(name, self.settings(**kwargs), args or {})

    def ok_call(self, name: str, args: dict | None = None, **kwargs) -> dict:
        result = self.call(name, args, **kwargs)
        self.assertTrue(result["ok"], json.dumps(result, indent=1, ensure_ascii=False)[:3000])
        return result

    def read_state(self) -> dict:
        return json.loads(self.state_file.read_text())

    def write_state(self, state: dict) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.state_file)

    def job(self, job_id: str) -> dict:
        return self.read_state()["jobs"][job_id.split(".")[0]]

    def set_job(self, job_id: str, **fields) -> None:
        state = self.read_state()
        state["jobs"][job_id.split(".")[0]].update(fields)
        self.write_state(state)

    def start(self, job_id: str) -> None:
        self.set_job(job_id, state="R", stime=time.time() - 60)

    def finish(self, job_id: str, exit_status: int = 0, used: str = "00:10:17") -> None:
        job = self.job(job_id)
        self.set_job(job_id, state="F", stime=job.get("stime") or time.time() - 700, obittime=time.time(),
                     exit_status=exit_status, used=used)

    def submit(self, **overrides) -> dict:
        args = {"script": str(self.script), "queue": "debug-g", "walltime": "00:20:00", "name": "job-a"}
        args.update(overrides)
        plan = self.ok_call("job_submit", {**args, "dry_run": True})
        self.assertTrue(plan["valid"], plan["checks"])
        return self.ok_call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})
