"""Watches and waiters: the background command an agent runs, detached waiters, timeouts, failures."""

import json
import subprocess
import time
import unittest

from base import ROOT, FakePbsCase

LAUNCHER = ROOT / "bin" / "miyabi-mcp"


class WatchTests(FakePbsCase):
    def waiter(self, watch_id: str) -> subprocess.Popen:
        return subprocess.Popen([str(LAUNCHER), "wait", "--watch", watch_id, "--project-dir", str(self.project)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_background_waiter_prints_the_wake_up_and_exits_zero(self):
        first = self.submit(name="a")["job_id"]
        second = self.submit(name="b")["job_id"]
        watch = self.ok_call("watch_create", {"job_ids": [first, second], "when": "all_ended",
                                              "message": "read results/metrics.json", "poll_seconds": 1})
        self.assertIsNone(watch["waiter"])
        self.assertIn("wait --watch", watch["command_local"])
        self.assertTrue(watch["command_via_ssh"].startswith("ssh -o BatchMode=yes miyabi-g "))
        process = self.waiter(watch["watch_id"])
        time.sleep(2.5)
        self.finish(first, exit_status=0)
        time.sleep(2.5)
        self.assertIsNone(process.poll(), "one of two jobs ended: the all_ended watch must keep waiting")
        self.finish(second, exit_status=1, used="00:00:30")
        out, err = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, err)
        lines = out.strip().splitlines()
        self.assertTrue(json.loads(lines[0])["armed"])
        message = "\n".join(lines[1:])
        self.assertIn("fired: all 2 jobs ended", message)
        self.assertIn("F Exit_status=1 (failed)", message)
        self.assertIn("Your note: read results/metrics.json", message)
        token = message.split('ack_token="')[1].split('"')[0]
        listed = self.ok_call("watch_list")["watches"][0]
        self.assertEqual(listed["status"], "delivered")
        wrong = self.call("watch_ack", {"watch_id": watch["watch_id"], "ack_token": "nope"})
        self.assertFalse(wrong["ok"])
        acked = self.ok_call("watch_ack", {"watch_id": watch["watch_id"], "ack_token": token})
        self.assertEqual(acked["status"], "acknowledged")

    def test_cancel_stops_the_waiter_with_exit_3(self):
        job_id = self.submit()["job_id"]
        watch = self.ok_call("watch_create", {"job_ids": [job_id], "poll_seconds": 1})
        process = self.waiter(watch["watch_id"])
        time.sleep(2)
        self.ok_call("watch_cancel", {"watch_id": watch["watch_id"], "reason": "plan changed"})
        out, _ = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 3)
        self.assertIn("cancelled", out)

    def test_timeout_fires_even_if_jobs_keep_running(self):
        job_id = self.submit()["job_id"]
        watch = self.ok_call("watch_create", {"job_ids": [job_id], "timeout": 2, "poll_seconds": 1})
        process = self.waiter(watch["watch_id"])
        out, err = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, err)
        self.assertIn("fired: timeout", out)

    def test_job_wait_is_bounded(self):
        job_id = self.submit()["job_id"]
        waited = self.ok_call("job_wait", {"job_ids": [job_id], "timeout_s": 1})
        self.assertFalse(waited["met"])
        self.finish(job_id)
        waited = self.ok_call("job_wait", {"job_ids": [job_id], "timeout_s": 5})
        self.assertTrue(waited["met"])
        self.assertEqual(waited["jobs"][0]["raw_state"], "F Exit_status=0")

    def test_queue_condition(self):
        watch = self.ok_call("watch_create", {"queue": {"queue": "debug-g", "state": "started"}, "poll_seconds": 1})
        process = self.waiter(watch["watch_id"])
        out, err = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, err)
        self.assertIn("queue debug-g is enabled and started", out)

    def test_detached_waiter_delivers_to_a_file(self):
        job_id = self.submit()["job_id"]
        inbox = self.project / "inbox.jsonl"
        watch = self.ok_call("watch_create", {"job_ids": [job_id], "poll_seconds": 1,
                                              "deliver": {"method": "file", "path": str(inbox)}})
        self.assertIsNotNone(watch["waiter"]["pid"])
        self.finish(job_id)
        deadline = time.time() + 30
        while time.time() < deadline and not inbox.exists():
            time.sleep(0.5)
        self.assertTrue(inbox.exists(), (self.project / ".miyabi-mcp" / "watches").glob("*.log"))
        line = json.loads(inbox.read_text().splitlines()[0])
        self.assertEqual(line["watch_id"], watch["watch_id"])
        self.assertIn("fired", line["message"])

    def test_outside_file_paths_are_refused(self):
        job_id = self.submit()["job_id"]
        result = self.call("watch_create", {"job_ids": [job_id], "deliver": {"method": "file", "path": "/tmp/x"}})
        self.assertEqual(result["error"]["type"], "invalid_arguments")

    def test_persistent_query_failure_exits_75(self):
        job_id = self.submit()["job_id"]
        watch = self.ok_call("watch_create", {"job_ids": [job_id], "poll_seconds": 1})
        (self.bin / "qstat").unlink()
        (self.bin / "qstat").write_text("#!/bin/sh\necho broken >&2\nexit 1\n")
        (self.bin / "qstat").chmod(0o755)
        config = self.user_root / ".miyabi-mcp" / "config.json"
        config.write_text(json.dumps({**self.user_config, "waiter_max_errors": 2}))
        process = self.waiter(watch["watch_id"])
        out, err = process.communicate(timeout=120)
        self.assertEqual(process.returncode, 75, err)
        self.assertIn("could not continue", out)
        self.assertIn("Resume later with", out)


if __name__ == "__main__":
    unittest.main()
