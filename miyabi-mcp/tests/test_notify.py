"""Notifications: groups, sealing, early failures, the agent-side daemon and its deliveries."""

import json
import os
import socket
import subprocess
import time

from base import ROOT, FakePbsCase
from miyabi_mcp import notify

LAUNCHER = ROOT / "bin" / "miyabi-mcp"
HOST = socket.gethostname().split(".", 1)[0]


class NotifyCase(FakePbsCase):
    def poll(self, notifier: str = "testbox") -> dict:
        result = notify.poll(self.settings(), notifier, "test")
        self.assertTrue(result["ok"], result)
        return result

    def deliver_all(self, result: dict) -> list[dict]:
        for message in result["messages"]:
            notify.report(self.settings(project_dir=message["project_dir"]), message["msg_id"], {"status": "delivered"})
        return result["messages"]

    def group(self, count: int, **spec) -> list[str]:
        base = {"name": "seeds", "expected": count, "notifier": "testbox", "message": "aggregate the seeds"}
        return [self.submit(name=f"seed-{i}", notify={**base, **spec})["job_id"] for i in range(count)]


class GroupTests(NotifyCase):
    def test_group_fires_once_when_sealed_and_all_ended(self):
        first = self.submit(name="seed-0", notify={"name": "seeds", "expected": 3, "notifier": "testbox",
                                                   "message": "aggregate the seeds"})
        daemon = first["notify"]["daemon"]
        self.assertIn("notifyd ensure --notifier testbox", daemon["start_command"])
        self.assertIn("notify listen --id N-", daemon["listen_command"])
        self.assertFalse(first["notify"]["notification"]["sealed"])
        jobs = [first["job_id"]] + [self.submit(name=f"seed-{i}", notify={"name": "seeds"})["job_id"] for i in (1, 2)]
        listed = self.ok_call("notify_list")["notifications"][0]
        self.assertTrue(listed["sealed"])
        self.assertEqual(listed["jobs"], jobs)
        self.assertEqual(self.poll()["messages"], [])
        self.finish(jobs[0])
        self.finish(jobs[1])
        self.assertEqual(self.poll()["messages"], [])
        self.finish(jobs[2])
        [message] = self.deliver_all(self.poll())
        self.assertEqual(message["kind"], "final")
        self.assertIn("all 3 jobs ended", message["message"])
        self.assertIn("Your note: aggregate the seeds", message["message"])
        self.assertEqual(sum(line.startswith("- ") for line in message["message"].splitlines()), 3)
        self.assertEqual(self.poll()["messages"], [])
        state = self.ok_call("notify_list")["notifications"][0]
        self.assertEqual(state["status"], "delivered")
        token = message["message"].split('ack_token="')[1].split('"')[0]
        self.assertEqual(self.ok_call("notify_ack", {"id": state["id"], "ack_token": token})["status"], "acknowledged")

    def test_unsealed_group_waits_for_seal(self):
        jobs = [self.submit(name=f"g{i}", notify={"name": "open-group", "notifier": "testbox"})["job_id"]
                for i in range(2)]
        for job_id in jobs:
            self.finish(job_id)
        self.assertEqual(self.poll()["messages"], [])
        self.ok_call("notify_update", {"name": "open-group", "seal": True})
        [message] = self.poll()["messages"]
        self.assertIn("all 2 jobs ended", message["message"])

    def test_early_failure_then_final(self):
        jobs = self.group(2)
        self.finish(jobs[0], exit_status=1, used="00:00:30")
        [early] = self.deliver_all(self.poll())
        self.assertEqual(early["kind"], "early")
        self.assertIn(f"job {jobs[0]} failed", early["message"])
        self.assertEqual(self.poll()["messages"], [])
        self.finish(jobs[1])
        [final] = self.deliver_all(self.poll())
        self.assertEqual(final["kind"], "final")
        self.assertIn("F Exit_status=1 (failed)", final["message"])

    def test_sealed_group_refuses_more_jobs(self):
        self.group(1)
        plan = self.ok_call("job_submit", {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00",
                                           "notify": {"name": "seeds"}})
        self.assertFalse(plan["valid"])
        self.assertIn("notify", {c["id"] for c in plan["checks"] if c["status"] == "fail"})


class CreateTests(NotifyCase):
    def test_existing_jobs_cancel_queue_and_timeout(self):
        job_id = self.submit()["job_id"]
        created = self.ok_call("notify_create", {"job_ids": [job_id], "notifier": "testbox"})["notification"]
        self.assertTrue(created["sealed"])
        self.ok_call("notify_cancel", {"id": created["id"], "reason": "plan changed"})
        self.finish(job_id)
        self.assertEqual(self.poll()["messages"], [])
        queue = self.ok_call("notify_create", {"queue": {"queue": "debug-g"}, "notifier": "testbox"})
        [message] = self.poll()["messages"]
        self.assertIn("queue debug-g is enabled and started", message["message"])
        self.assertEqual(message["notification_id"], queue["notification"]["id"])
        running = self.submit(name="long")["job_id"]
        self.start(running)
        self.ok_call("notify_create", {"job_ids": [running], "timeout": 1, "notifier": "other"})
        time.sleep(1.5)
        [late] = self.poll("other")["messages"]
        self.assertIn("timeout", late["message"])

    def test_notifier_heartbeat_is_reported(self):
        job_id = self.submit()["job_id"]
        made = self.ok_call("notify_create", {"job_ids": [job_id], "notifier": "laptop"})
        self.assertFalse(made["daemon"]["daemon_running"])
        self.assertIn("hint", made["daemon"])
        self.poll("laptop")
        self.assertTrue(self.ok_call("notify_list")["daemons"][0]["daemon_running"])


class DaemonTests(NotifyCase):
    def run_cli(self, *args, timeout=60) -> subprocess.CompletedProcess:
        return subprocess.run([str(LAUNCHER), *args], capture_output=True, text=True, timeout=timeout)

    def test_local_listener_end_to_end(self):
        job_id = self.submit(notify={"notifier": HOST, "message": "check the output"})["job_id"]
        notification = self.ok_call("notify_list")["notifications"][0]["id"]
        listener = subprocess.Popen([str(LAUNCHER), "notify", "listen", "--id", notification, "--notifier", HOST,
                                     "--poll-seconds", "1", "--idle-polls", "2"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(3)
        self.assertIsNone(listener.poll())
        self.finish(job_id)
        out, err = listener.communicate(timeout=60)
        self.assertEqual(listener.returncode, 0, err)
        self.assertIn("all 1 jobs ended", out)
        self.assertEqual(self.ok_call("notify_list")["notifications"][0]["status"], "delivered")
        deadline = time.time() + 20
        while time.time() < deadline:
            status = json.loads(self.run_cli("notifyd", "status", "--notifier", HOST).stdout)
            if not status["running"]:
                break
            time.sleep(0.5)
        self.assertEqual(status["state"]["status"], "exited_idle")

    def test_remote_mode_through_ssh(self):
        fake_ssh = self.bin / "fake-ssh"
        fake_ssh.write_text('#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do [ "$1" = "-o" ] && shift; shift; done\n'
                            'shift\nexec "$@"\n')
        fake_ssh.chmod(0o755)
        os.environ["MIYABI_MCP_SSH"] = str(fake_ssh)
        self.addCleanup(os.environ.pop, "MIYABI_MCP_SSH", None)
        inbox = self.project / "notes.jsonl"
        job_id = self.submit(notify={"notifier": "laptop", "deliver": {"method": "file", "path": str(inbox)}})["job_id"]
        self.finish(job_id)
        daemon = self.run_cli("notifyd", "run", "--notifier", "laptop", "--remote", "miyabi-g", "--remote-launcher",
                              str(LAUNCHER), "--poll-seconds", "1", "--idle-polls", "1")
        self.assertEqual(daemon.returncode, 0, daemon.stderr)
        line = json.loads(inbox.read_text().splitlines()[0])
        self.assertIn("all 1 jobs ended", line["message"])
        self.assertEqual(self.ok_call("notify_list")["notifications"][0]["status"], "delivered")

    def test_broken_channel_is_announced(self):
        broken = self.bin / "broken-ssh"
        broken.write_text("#!/bin/sh\necho 'Connection closed by remote host' >&2\nexit 255\n")
        broken.chmod(0o755)
        os.environ["MIYABI_MCP_SSH"] = str(broken)
        self.addCleanup(os.environ.pop, "MIYABI_MCP_SSH", None)
        inbox = self.project / "broken.jsonl"
        state_dir = self.user_root / ".miyabi-mcp" / "notifyd"
        state_dir.mkdir(parents=True)
        (state_dir / "known-laptop.json").write_text(json.dumps({"N-1": {"method": "file", "path": str(inbox)}}))
        daemon = subprocess.Popen([str(LAUNCHER), "notifyd", "run", "--notifier", "laptop", "--remote", "x",
                                   "--remote-launcher", "y", "--poll-seconds", "1", "--fail-notify-after", "2",
                                   "--idle-polls", "99"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while time.time() < deadline and not inbox.exists():
            time.sleep(0.5)
        self.run_cli("notifyd", "stop", "--notifier", "laptop")
        daemon.communicate(timeout=30)
        self.assertIn("notification channel broken", json.loads(inbox.read_text().splitlines()[0])["message"])

    def test_herdr_delivery(self):
        record = self.project / "herdr-args.txt"
        herdr = self.bin / "herdr"
        herdr.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\n')
        herdr.chmod(0o755)
        os.environ["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        job_id = self.submit(notify={"notifier": HOST, "deliver": {"method": "herdr", "agent": "worker-1"}})["job_id"]
        self.finish(job_id)
        daemon = self.run_cli("notifyd", "run", "--notifier", HOST, "--poll-seconds", "1", "--idle-polls", "1")
        self.assertEqual(daemon.returncode, 0, daemon.stderr)
        args = record.read_text().splitlines()
        self.assertEqual(args[:3], ["agent", "prompt", "worker-1"])
        self.assertIn("all 1 jobs ended", "\n".join(args[3:]))


if __name__ == "__main__":
    import unittest

    unittest.main()
