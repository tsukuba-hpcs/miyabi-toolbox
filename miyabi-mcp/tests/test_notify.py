"""Notifications: groups, sealing, early failures, the agent-side daemon and its deliveries."""

import json
import os
import socket
import subprocess
import time
from datetime import timedelta
from unittest import mock

from base import ROOT, FakePbsCase
from miyabi_mcp import deliver as delivery, notify, notifyd
from miyabi_mcp.util import Lock, MiyabiError, now, read_json, write_json

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
        daemons = self.ok_call("notify_list")["daemons"]
        self.assertEqual([d["notifier"] for d in daemons], ["testbox"])
        self.assertTrue(daemons[0]["last_poll"])

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
        # Acknowledging an early message would close the group before its final message: no token in it.
        self.assertNotIn("notify_ack(", early["message"])
        self.assertIn("needs no acknowledgment", early["message"])
        self.assertEqual(self.poll()["messages"], [])
        self.finish(jobs[1])
        [final] = self.deliver_all(self.poll())
        self.assertEqual(final["kind"], "final")
        self.assertIn("F Exit_status=1 (failed)", final["message"])
        self.assertIn("notify_ack(", final["message"])

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
            if not status["running"] and status["state"]["status"] != "exiting":
                break
            time.sleep(0.5)
        self.assertEqual(status["state"]["status"], "exited_idle")
        self.assertEqual(status["state"]["started_by"], "listen")
        closed = read_json(self.user_root / ".miyabi-mcp" / "notifyd" / f"closed-{HOST}.json")
        self.assertEqual(closed[notification]["status"], "delivered")
        [daemon] = self.ok_call("notify_list")["daemons"]
        self.assertEqual((daemon["state"], daemon["exited"]["reason"]), ("idle_exited", "idle"))
        self.assertNotIn("hint", daemon)  # nothing is open: resting is normal

    def test_remote_mode_through_ssh(self):
        fake_ssh = self.bin / "fake-ssh"
        # Like ssh: drop options and the host, then let a shell parse the one remote command line.
        fake_ssh.write_text('#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do [ "$1" = "-o" ] && shift; shift; done\n'
                            'shift\nexec sh -c "$*"\n')
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
        self.assertIn("ssh -T -o BatchMode=yes miyabi-g ", line["message"])  # export_command for laptop
        self.assertEqual(self.ok_call("notify_list")["notifications"][0]["status"], "delivered")
        state = json.loads(self.run_cli("notifyd", "status", "--notifier", "laptop").stdout)["state"]
        self.assertEqual(state["failures"], 0)
        # The interval and the idle exit reached the server through ssh.
        daemon = notify.daemon_info(self.settings(), "laptop")
        self.assertEqual((daemon["state"], daemon["daemon_poll_seconds"]), ("idle_exited", 1))

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


class StateTests(NotifyCase):
    def test_commands_carry_the_configured_interval(self):
        job_id = self.submit()["job_id"]
        daemon = self.ok_call("notify_create", {"job_ids": [job_id], "notifier": "laptop"})["daemon"]
        self.assertEqual(daemon["poll_seconds"], 180)
        self.assertIn("--poll-seconds 180", daemon["start_command"])
        self.assertIn("--poll-seconds 180", daemon["listen_command"])
        config = self.user_root / ".miyabi-mcp" / "config.json"
        config.write_text(json.dumps({**self.user_config, "notify_poll_seconds": 120}))
        self.assertIn("--poll-seconds 120", self.ok_call("notify_list")["daemons"][0]["start_command"])
        notify.poll(self.settings(), "laptop", "test", interval=120, instance="a")
        info = self.ok_call("notify_list")["daemons"][0]
        self.assertEqual((info["state"], info["daemon_poll_seconds"]), ("running", 120))
        self.assertNotIn("note", info)
        notify.poll(self.settings(), "laptop", "test", interval=300, instance="a")
        self.assertIn("polls every 300 s", self.ok_call("notify_list")["daemons"][0]["note"])

    def test_idle_exit_is_reported_and_hinted_only_with_open_work(self):
        notify.poll(self.settings(), "laptop", "test", interval=60, instance="A")
        notify.poll(self.settings(), "laptop", "test", interval=60, instance="A", exiting="idle")
        resting = notify.daemon_info(self.settings(), "laptop")
        self.assertEqual((resting["state"], resting["daemon_running"]), ("idle_exited", False))
        self.assertNotIn("hint", resting)
        job_id = self.submit()["job_id"]
        made = self.ok_call("notify_create", {"job_ids": [job_id], "notifier": "laptop"})
        self.assertEqual(made["daemon"]["state"], "idle_exited")
        self.assertIn("exited after polling", made["daemon"]["hint"])
        self.assertIn("listen_command", made["next"])
        self.assertEqual(self.ok_call("notify_list")["summary"]["daemons"], {"laptop": "idle_exited"})
        notify.poll(self.settings(), "laptop", "test", interval=60, instance="B")
        # A late exit report of the old daemon does not hide its successor.
        notify.poll(self.settings(), "laptop", "test", interval=60, instance="A", exiting="stopped")
        running = notify.daemon_info(self.settings(), "laptop")
        self.assertEqual(running["state"], "running")
        self.assertNotIn("hint", running)

    def test_exit_report_sees_a_notification_registered_meanwhile(self):
        job_id = self.submit()["job_id"]
        self.ok_call("notify_create", {"job_ids": [job_id], "notifier": "laptop"})
        report = notify.poll(self.settings(), "laptop", "test", interval=60, instance="A", exiting="idle")
        self.assertEqual(report["open_count"], 1)  # the daemon resumes instead of leaving it stranded

    def test_project_errors_still_count_open_notifications(self):
        job_id = self.submit(notify={"notifier": "laptop"})["job_id"]
        (self.project / ".miyabi-mcp" / "jobs" / job_id.split(".")[0]).unlink()
        result = self.poll("laptop")
        self.assertEqual(result["open_count"], 1)
        self.assertEqual(len(result["targets"]), 1)
        self.assertEqual(result["errors"][0]["type"], "not_in_ledger")
        daemon = notify.daemon_info(self.settings(), "laptop")
        self.assertIsNone(daemon["last_ok"])
        self.assertEqual(daemon["errors"][0]["type"], "not_in_ledger")

    def test_unknown_member_does_not_end_the_group(self):
        jobs = self.group(2)
        self.finish(jobs[0])
        state = self.read_state()
        del state["jobs"][jobs[1].split(".")[0]]
        self.write_state(state)
        [notice] = self.deliver_all(self.poll())
        self.assertEqual(notice["kind"], "unknown")
        self.assertIn(f"job {jobs[1]} is neither active nor in PBS history", notice["message"])
        self.assertNotIn("notify_ack(", notice["message"])
        self.assertEqual(self.poll()["messages"], [])
        self.assertEqual(self.ok_call("notify_list")["notifications"][0]["status"], "open")
        waited = self.ok_call("job_wait", {"job_ids": jobs, "timeout_s": 1})
        self.assertFalse(waited["met"])
        self.assertIn(f"state unknown for {jobs[1]}", waited["next"])
        self.assertIn("state-unknown message was already sent", waited["notifications"][0]["next"])

    def test_delivered_notification_can_be_dismissed_without_token(self):
        [job_id] = self.group(1)
        self.finish(job_id)
        [final] = self.deliver_all(self.poll())
        token = final["message"].split('ack_token="')[1].split('"')[0]
        item = self.ok_call("notify_list")["notifications"][0]
        self.assertEqual(self.ok_call("notify_list")["summary"]["delivered_unacknowledged"], 1)
        waited = self.ok_call("job_wait", {"job_ids": [job_id], "timeout_s": 1})
        self.assertTrue(waited["met"])
        [entry] = waited["notifications"]
        self.assertEqual(entry["status"], "delivered")
        self.assertIn("notify_cancel", entry["next"])
        self.assertNotIn(token, json.dumps(waited))
        dismissed = self.ok_call("notify_cancel", {"id": item["id"], "reason": "outputs already read"})
        self.assertEqual(dismissed["notification"]["status"], "dismissed")
        stored = read_json(self.project / ".miyabi-mcp" / "notifications" / f"{item['id']}.json")
        self.assertNotIn("acknowledged_at", stored)
        self.assertEqual(stored["dismissal_reason"], "outputs already read")
        listed = self.ok_call("notify_list")
        self.assertEqual(listed["summary"]["delivered_unacknowledged"], 0)
        self.assertEqual(listed["notifications"][0]["closed_at"], stored["history"][-1]["at"])
        self.assertEqual(self.call("notify_cancel", {"id": item["id"]})["error"]["type"], "invalid_state")


class FiredGroupTests(NotifyCase):
    """A fired notification is never evaluated again: it takes no more jobs and its name is free."""

    def fire_by_timeout(self) -> dict:
        made = self.ok_call("notify_create", {"name": "grp", "timeout": 1, "notifier": "testbox"})
        time.sleep(1.2)
        self.poll()
        self.assertEqual(self.ok_call("notify_list")["notifications"][0]["status"], "fired")
        return made["notification"]

    def test_a_late_job_starts_a_new_group_and_is_reported(self):
        old = self.fire_by_timeout()
        plan = self.ok_call("job_submit", {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00",
                                           "notify": {"name": "grp", "notifier": "testbox"}})
        self.assertIn("creates notification grp", next(c for c in plan["checks"] if c["id"] == "notify")["detail"])
        late = self.submit(name="late", notify={"name": "grp", "notifier": "testbox", "expected": 1})
        self.assertTrue(late["notify"]["created"])
        self.assertNotEqual(late["notify"]["notification"]["id"], old["id"])
        self.finish(late["job_id"])
        kinds = {(m["notification_id"], m["kind"]) for m in self.poll()["messages"]}
        self.assertIn((late["notify"]["notification"]["id"], "final"), kinds)

    def test_a_fired_group_refuses_updates_and_frees_its_name(self):
        old = self.fire_by_timeout()
        job_id = self.submit(name="x")["job_id"]
        refused = self.call("notify_update", {"id": old["id"], "add_job_ids": [job_id]})
        self.assertEqual(refused["error"]["type"], "invalid_state")
        made = self.ok_call("notify_create", {"name": "grp", "job_ids": [job_id], "notifier": "testbox"})
        self.assertNotEqual(made["notification"]["id"], old["id"])
        updated = self.ok_call("notify_update", {"name": "grp", "message": "by name: the open one"})
        self.assertEqual(updated["notification"]["id"], made["notification"]["id"])
        duplicate = self.call("notify_create", {"name": "grp", "notifier": "testbox", "timeout": 60})
        self.assertEqual(duplicate["error"]["type"], "exists")


class JoinRaceTests(NotifyCase):
    def test_a_job_does_not_join_a_group_sealed_after_the_lookup(self):
        first = self.submit(name="a", notify={"name": "g", "notifier": "testbox", "expected": 2})
        settings = self.settings()
        store = notify.Store(settings)
        looked_up = notify._named(store.state, "g", "open")  # what join saw before another call sealed it
        self.ok_call("notify_update", {"name": "g", "seal": True})
        second = self.submit(name="b")["job_id"]
        with mock.patch.object(notify, "_named", return_value=looked_up), self.assertRaises(MiyabiError) as caught:
            notify.join(settings, store, {"name": "g", "notifier": "testbox"}, second)
        self.assertEqual(caught.exception.kind, "sealed")
        self.assertEqual(notify._named(store.state, "g", "open")["jobs"], [first["job_id"]])

    def test_joining_again_adds_the_job_once(self):
        job_id = self.submit(name="a", notify={"name": "g", "notifier": "testbox", "expected": 1})["job_id"]
        settings = self.settings()
        again = notify.join(settings, notify.Store(settings), {"name": "g", "notifier": "testbox"}, job_id)
        self.assertEqual(again["notification"]["jobs"], [job_id])


class DeliveryErrorTests(NotifyCase):
    def send(self, argv: list[str]) -> dict:
        return delivery.send({"method": "command", "name": "x"}, "hi", state_dir=self.project, notification_id="N-1",
                             msg_id="N-1:1", commands={"x": argv})

    def test_a_missing_program_fails_instead_of_counting_as_delivered(self):
        self.assertEqual(self.send([str(self.project / "no-such-program")])["status"], "failed")

    def test_a_timeout_may_have_delivered_and_a_full_disk_is_retried(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 60)):
            self.assertEqual(self.send(["true"])["status"], "uncertain")
        with mock.patch("subprocess.run", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(self.send(["true"])["status"], "retry")


class NotifierNameTests(NotifyCase):
    def test_notifier_names_are_checked_and_commands_quoted(self):
        bad = self.call("notify_create", {"name": "x", "timeout": 60, "notifier": "box; rm -rf ~"})
        self.assertEqual(bad["error"]["type"], "invalid_arguments")
        plan = self.ok_call("job_submit", {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00",
                                           "notify": {"notifier": "../x"}})
        self.assertEqual(next(c for c in plan["checks"] if c["id"] == "notify")["status"], "fail")
        with self.assertRaises(MiyabiError):
            notify.poll(self.settings(), "../../etc/x", "test")
        spaced = self.user_root / "my project"
        spaced.mkdir()
        command = notify.export_command(self.settings(), spaced, notifier="laptop")
        self.assertIn(f"--project-dir '{spaced}'", command)
        with self.assertRaises(MiyabiError):
            notifyd.Options(notifier="a b")


class DaemonLifecycleTests(NotifyCase):
    def setUp(self):
        super().setUp()
        os.environ["MIYABI_MCP_NOTIFYD_DIR"] = str(self.user_root / "notifyd-local")
        self.addCleanup(os.environ.pop, "MIYABI_MCP_NOTIFYD_DIR", None)
        self.opts = notifyd.Options(notifier="box", poll_seconds=1)
        self.paths = notifyd._paths(self.opts)
        self.paths["dir"].mkdir(parents=True)

    def hand_over(self, result=None, error=None, successor=False):
        lock = Lock(self.paths["lock"], lease=60, timeout=0).acquire()
        state = {"status": "running", "pid": os.getpid(), "host": notifyd.short_host()}

        def report(opts, instance=None, exiting=None):
            self.assertEqual(exiting, "idle")
            self.assertEqual(read_json(self.paths["state"])["status"], "exiting")
            self.assertFalse(self.paths["lock"].exists())  # free before the server hears of the exit
            if successor:
                Lock(self.paths["lock"], lease=60, timeout=0).acquire()
            if error:
                raise error
            return result

        with mock.patch.object(notifyd, "poll", report):
            leave = notifyd._hand_over(self.opts, self.paths, lock, state, "A")
        return leave, lock, read_json(self.paths["state"])

    def test_idle_daemon_leaves_holding_the_lock_for_its_last_state(self):
        leave, lock, _state = self.hand_over({"open_count": 0, "messages": [], "errors": []})
        self.assertTrue(leave)
        self.assertIsNotNone(lock.token)

    def test_work_at_the_exit_report_resumes(self):
        for result in ({"open_count": 1, "messages": [], "errors": []},
                       {"open_count": 0, "messages": [], "errors": [{"project": "p"}]}):
            leave, lock, state = self.hand_over(result)
            self.assertFalse(leave)
            self.assertEqual(state["status"], "running")
            lock.release()
        leave, lock, _state = self.hand_over(error=MiyabiError("transport", "ssh failed"))
        self.assertFalse(leave)
        lock.release()

    def test_successor_keeps_its_lock_and_state(self):
        leave, lock, state = self.hand_over({"open_count": 1, "messages": [], "errors": []}, successor=True)
        self.assertTrue(leave)
        self.assertIsNone(lock.token)
        self.assertEqual(state["status"], "exiting")  # untouched: the successor writes its own

    def test_listener_exit_names_the_reason(self):
        write_json(self.paths["closed"], {
            "N-c": {"status": "cancelled", "at": "2026-11-05T10:00:00+09:00", "delivered_at": None},
            "N-d": {"status": "acknowledged", "at": "2026-11-05T11:00:00+09:00",
                    "delivered_at": "2026-11-05T10:30:00+09:00"}})
        box = delivery.inbox(self.paths["dir"], "N-d")
        box.mkdir(parents=True)
        (box / "N-d_1.shown").write_text("{}")
        cancelled = notifyd._gone("N-c", self.opts, self.paths, delivery.inbox(self.paths["dir"], "N-c"))
        self.assertIn("cancelled at 2026-11-05T10:00:00+09:00", cancelled)
        taken = notifyd._gone("N-d", self.opts, self.paths, box)
        self.assertIn("delivered at 2026-11-05T10:30:00+09:00", taken)
        self.assertIn("does not mean an agent read it", taken)
        self.assertIn("then acknowledged at 2026-11-05T11:00:00+09:00", taken)
        self.assertIn("a listener took its last message at", taken)
        stranger = notifyd._gone("N-x", self.opts, self.paths, delivery.inbox(self.paths["dir"], "N-x"))
        self.assertIn("notify_list shows each notification's notifier", stranger)

    def listen_once(self, last_ok_started_at: str) -> tuple[int, str]:
        """Run the listener with a daemon state whose last good poll ended after the listener started; a
        message arrives in its inbox after a few rounds unless the listener gives up first."""
        write_json(self.paths["state"], {"status": "running", "last_ok_at": (now() + timedelta(seconds=5)).isoformat(),
                                         "last_ok_started_at": last_ok_started_at})
        write_json(self.paths["known"], {})
        box = delivery.inbox(self.paths["dir"], "N-new")
        clock = iter(range(0, 10 ** 6, 61))  # every look at the clock is 61 s later: each round checks
        rounds = []

        def sleep(_seconds):
            rounds.append(1)
            if len(rounds) == 3:
                box.mkdir(parents=True, exist_ok=True)
                (box / "N-new_1.json").write_text(json.dumps({"message": "the job ended"}))

        printed = []

        def capture(*args, **_kwargs):
            printed.extend(args)

        with mock.patch.object(notifyd, "ensure"), mock.patch.object(notifyd.time, "monotonic", lambda: next(clock)), \
                mock.patch.object(notifyd.time, "sleep", sleep), mock.patch("builtins.print", capture):
            code = notifyd.listen("N-new", self.opts)
        return code, " ".join(map(str, printed))

    def test_listener_ignores_a_poll_that_began_before_it(self):
        # The poll began before the notification existed and ended after the listener started.
        code, text = self.listen_once((now() - timedelta(seconds=30)).isoformat())
        self.assertEqual((code, text), (0, "the job ended"))

    def test_listener_gives_up_after_a_poll_that_began_after_it(self):
        code, text = self.listen_once((now() + timedelta(seconds=5)).isoformat())
        self.assertEqual(code, 3)
        self.assertIn("no longer open for notifier box", text)

    def test_old_closed_inboxes_are_removed(self):
        old, recent = (now() - timedelta(days=8)).isoformat(), now().isoformat()
        closed = {"N-a": {"status": "acknowledged", "at": old}, "N-b": {"status": "delivered", "at": old},
                  "N-c": {"status": "dismissed", "at": recent}, "N-d": {"status": "cancelled", "at": old}}
        for key in closed:
            delivery.inbox(self.paths["dir"], key).mkdir(parents=True)
        notifyd._clean_inboxes(self.paths, closed)
        self.assertEqual(sorted(p.name for p in (self.paths["dir"] / "inbox").iterdir()), ["N-b", "N-c"])


class DaemonErrorTests(NotifyCase):
    def run_cli(self, *args, timeout=60) -> subprocess.CompletedProcess:
        return subprocess.run([str(LAUNCHER), *args], capture_output=True, text=True, timeout=timeout)

    def test_project_error_never_counts_as_idle(self):
        job_id = self.submit(notify={"notifier": HOST})["job_id"]
        (self.project / ".miyabi-mcp" / "jobs" / job_id.split(".")[0]).unlink()  # this project's refresh fails
        daemon = subprocess.Popen([str(LAUNCHER), "notifyd", "run", "--notifier", HOST, "--poll-seconds", "1",
                                   "--idle-polls", "1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(5)
        self.assertIsNone(daemon.poll())
        self.run_cli("notifyd", "stop", "--notifier", HOST)
        _out, err = daemon.communicate(timeout=30)
        self.assertEqual(daemon.returncode, 0, err)
        log = self.user_root / ".miyabi-mcp" / "notifyd" / f"{HOST}.log"
        events = [json.loads(line) for line in log.read_text().splitlines() if line.startswith("{")]
        names = [event["event"] for event in events]
        self.assertEqual(names[0], "started")
        self.assertIn("poll_errors", names)
        self.assertIn("broken_notice", names)  # three rounds with errors announce a broken channel
        self.assertEqual((events[-1]["event"], events[-1]["reason"]), ("exited", "stopped"))
        [info] = self.ok_call("notify_list")["daemons"]
        self.assertEqual((info["state"], info["exited"]["reason"]), ("stopped", "stopped"))
        self.assertIn("hint", info)  # its notification is still open


if __name__ == "__main__":
    import unittest

    unittest.main()
