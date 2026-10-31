"""Exercise the PTY lifecycle with fake PBS, plus allocation evidence failure paths."""

import errno
import json
import os
import pty
import select
import signal
import subprocess
import time
from unittest import mock

from base import ROOT, FakePbsCase
from miyabi_mcp import context, interactive
from miyabi_mcp.ledger import Store
from miyabi_mcp.util import MiyabiError


class InteractiveTests(FakePbsCase):
    def plan(self, **changes):
        return self.ok_call("interactive_plan", {"queue": "interact-g", "walltime": "00:02:00",
                                                "request_key": "interactive-test", **changes})

    def launch(self, digest):
        master, slave = pty.openpty()
        # Only the CLI's login-plane precondition is mocked. qsub and its shell
        # use real nested PTYs, the real ledger, policy and fake PBS commands.
        code = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                "from miyabi_mcp import interactive; from miyabi_mcp.cli import main; "
                "interactive.context.inspect=lambda **kw: {'node_role':'login'}; "
                "raise SystemExit(main())")
        process = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-c", code, str(ROOT / "src"),
                                    "--project-dir", str(self.project), "interactive-start", digest],
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        self.addCleanup(os.close, master)
        def cleanup():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        self.addCleanup(cleanup)
        return process, master

    def read_until(self, master, expected, timeout=10):
        output = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not data:
                    break
                output += data
                if expected.encode() in output:
                    return output.decode(errors="replace")
        self.fail(f"Did not see {expected!r}: {output.decode(errors='replace')}")

    def test_plan_is_checked_and_does_not_submit(self):
        result = self.plan()
        self.assertTrue(result["valid"])
        self.assertEqual(self.read_state()["jobs"], {})
        self.assertTrue(result["launch_command"].startswith("exec "))
        self.assertEqual(result["terminal_open"]["command"], result["launch_command"])
        self.assertEqual(result["plan"]["route"], "interact-g_n1")
        self.assertIn("-I", result["qsub_preview"])
        self.assertNotIn("-o", result["qsub_preview"])
        for changes in ({"nodes": 9}, {"walltime": "03:00:00"}, {"queue": "debug-g"}):
            self.assertFalse(self.plan(**changes)["valid"])
        self.assertEqual(self.plan(queue="interact-c")["plan"]["system"], "Miyabi-C")
        self.assertEqual(self.plan(queue="interact-mig")["plan"]["unit"], "mig_instance_hours")

    def test_non_terminal_and_batch_only_arguments_fail_before_submission(self):
        result = self.plan()
        with mock.patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(MiyabiError) as caught:
                interactive.start(self.settings(), result["plan_digest"])
        self.assertEqual(caught.exception.kind, "terminal_required")
        bad = self.call("interactive_plan", {"queue": "interact-g", "walltime": "00:02:00",
                                              "request_key": "test", "script": str(self.script)})
        self.assertEqual(bad["error"]["type"], "invalid_arguments")
        self.assertEqual(self.read_state()["jobs"], {})

    def test_persistent_shell_exit_and_idempotent_retry(self):
        result = self.plan(env={"TEST_VALUE": "initial"})
        process, master = self.launch(result["plan_digest"])
        output = self.read_until(master, '"allocation_check_command"')
        self.assertIn('"job_id":"3800000.opbs"', output)
        os.write(master, b"export TEST_VALUE=retained; printf 'pre%spared\\n' ''\n")
        self.read_until(master, "prepared")
        os.write(master, b"printf 'value=%s\\n' \"$TEST_VALUE\"\n")
        self.read_until(master, "value=retained")
        os.write(master, b"exit 7\n")
        self.assertEqual(process.wait(timeout=10), 7)
        job = self.job("3800000.opbs")
        self.assertEqual((job["state"], job["exit_status"]), ("F", 7))
        record = Store(self.settings()).find("3800000.opbs")
        self.assertTrue(record["submit"]["interactive"])
        self.assertFalse((Store(self.settings()).submission_dir(record["submit_id"]) / "script.sh").exists())
        with open(record["submit"]["output"]) as transcript:
            self.assertIn("value=retained", transcript.read())
        detail = self.ok_call("job_get", {"job_ids": ["3800000.opbs"], "tail_lines": 20})["jobs"][0]
        self.assertEqual(detail["output_source"], "terminal_transcript")
        self.assertIn("value=retained", detail["output_tail"])
        retry, retry_master = self.launch(result["plan_digest"])
        self.read_until(retry_master, "original terminal_id")
        self.assertEqual(retry.wait(timeout=10), 0)
        self.assertEqual(len(self.read_state()["jobs"]), 1)

    def test_launch_rechecks_policy_and_expiration(self):
        result = self.plan()
        store = Store(self.settings())
        (store.state / "policy.json").write_text(json.dumps({"queues": ["debug-g"]}))
        process, master = self.launch(result["plan_digest"])
        self.read_until(master, "plan_rejected")
        self.assertEqual(process.wait(timeout=10), 1)
        self.assertEqual(self.read_state()["jobs"], {})
        saved_path = store.plan_path(result["plan_digest"])
        saved = json.loads(saved_path.read_text())
        saved["created_at"] = "2000-01-01T00:00:00+00:00"
        saved_path.write_text(json.dumps(saved))
        process, master = self.launch(result["plan_digest"])
        self.read_until(master, "plan_expired")
        self.assertEqual(process.wait(timeout=10), 1)

    def test_queue_wait_does_not_hold_submit_lock_and_can_cancel(self):
        state = self.read_state()
        state["qsub_mode"] = "interactive_queue"
        self.write_state(state)
        result = self.plan()
        process, master = self.launch(result["plan_digest"])
        self.read_until(master, '"allocation_check_command"')
        self.assertIsNone(process.poll())
        # An unrelated submission can acquire the same lock while qsub -I waits.
        self.submit()
        args = {"job_ids": ["3800000.opbs"]}
        cancel = self.ok_call("job_cancel", args)
        self.ok_call("job_cancel", {**args, "dry_run": False, "targets_digest": cancel["targets_digest"],
                                   "reason": "interactive test completed"})
        self.assertEqual(process.wait(timeout=10), 1)
        self.assertEqual(self.job("3800000.opbs")["state"], "F")

    def test_disconnect_releases_only_its_own_job(self):
        other = self.submit()["job_id"]
        result = self.plan()
        process, master = self.launch(result["plan_digest"])
        self.read_until(master, '"allocation_check_command"')
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 1)
        self.assertEqual(self.job("3800001.opbs")["state"], "F")
        self.assertEqual(self.job(other)["state"], "Q")

    def test_disconnect_before_qsub_reports_id_reconciles_and_cancels(self):
        state = self.read_state()
        state["qsub_mode"] = "interactive_silent"
        self.write_state(state)
        result = self.plan()
        process, master = self.launch(result["plan_digest"])
        deadline = time.monotonic() + 10
        while not self.read_state()["jobs"] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.read_state()["jobs"])
        process.send_signal(signal.SIGTERM)
        self.read_until(master, "terminal_disconnected")
        self.assertEqual(process.wait(timeout=10), 1)
        self.assertEqual(self.job("3800000.opbs")["state"], "F")
        self.assertTrue(Store(self.settings()).find("3800000.opbs")["cancel_requested"])


class AllocationTests(FakePbsCase):
    def test_guard_requires_host_nodefile_target_and_live_job(self):
        job_id = self.submit()["job_id"]
        nodefile = self.project / "nodes"
        nodefile.write_text("mg0001\n")
        evidence = context.inspect(hostname="mg0001", architecture="aarch64", target_system="Miyabi-G",
                                   environ={"PBS_JOBID": job_id, "PBS_NODEFILE": str(nodefile)})
        args = {"target_system": "Miyabi-G", "expected_job_id": job_id}
        self.assertFalse(self.call("allocation_check", args)["ok"])  # actual local host
        with mock.patch.object(interactive.context, "inspect", return_value=evidence):
            self.assertFalse(self.call("allocation_check", args)["ok"])  # still queued
            self.start(job_id)
            result = self.ok_call("allocation_check", args)
            self.assertEqual(result["remaining_walltime_s"], 1190)
            self.assertFalse(self.call("allocation_check", {**args, "expected_job_id": "1.opbs"})["ok"])
            self.finish(job_id)
            self.assertFalse(self.call("allocation_check", args)["ok"])

    def test_login_never_passes_with_inherited_pbs_variables(self):
        nodefile = self.project / "nodes"
        nodefile.write_text("miyabi-g1\n")
        evidence = context.inspect(hostname="miyabi-g1", architecture="aarch64", target_system="Miyabi-G",
                                   environ={"PBS_JOBID": "123.opbs", "PBS_NODEFILE": str(nodefile)})
        with mock.patch.object(interactive.context, "inspect", return_value=evidence):
            result = self.call("allocation_check", {"target_system": "Miyabi-G"})
        self.assertFalse(result["ok"])
        self.assertFalse(result["context"]["runtime_execution_allowed"])
