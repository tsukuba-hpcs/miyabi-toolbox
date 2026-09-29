"""Lightweight offline regression tests; no PBS jobs or model calls."""

import json
import os
from pathlib import Path
import subprocess
import socket
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import job_wakeup as wake


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.state = dict(event_id="test", thread_id="thread", status="ready", kind="timer",
                          codex="codex", cwd=str(self.directory), remote="unix:///test.sock",
                          message="Inspect results", observation={"status": "TIMER_ELAPSED"})
        wake.write_state(self.directory, self.state)

    def test_acceptance_is_not_acknowledgement_and_restart_does_not_resend(self):
        with patch.object(wake.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "queued", "")) as run:
            wake.deliver(self.directory)
            self.assertEqual(wake.read_state(self.directory)["status"], "delivered")
            wake.worker(self.directory)
            self.assertEqual(run.call_count, 1)

    def test_ambiguous_timeout_is_not_retried(self):
        with patch.object(wake.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 60)) as run:
            wake.deliver(self.directory)
            wake.worker(self.directory)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(wake.read_state(self.directory)["status"], "uncertain")

    def test_crash_during_send_requires_reconciliation(self):
        wake.update(self.directory, lambda s: s.update(status="sending"))
        with patch.object(wake.subprocess, "run") as run:
            wake.worker(self.directory)
            run.assert_not_called()
        self.assertEqual(wake.read_state(self.directory)["status"], "uncertain")

    def test_ack_racing_sender_is_preserved(self):
        def send(*args, **kwargs):
            wake.update(self.directory, lambda s: s.update(status="acknowledged", acknowledged_at=123))
            return subprocess.CompletedProcess([], 0, "queued", "")
        with patch.object(wake.subprocess, "run", side_effect=send):
            wake.deliver(self.directory)
        self.assertEqual(wake.read_state(self.directory)["status"], "acknowledged")

    def test_cancelled_timer_does_not_send(self):
        wake.update(self.directory, lambda s: s.update(status="cancelled"))
        with patch.object(wake.subprocess, "run") as run:
            wake.worker(self.directory)
            run.assert_not_called()

    def test_missing_pbs_job_is_unknown(self):
        with patch.object(wake, "query_jobs", return_value=[]):
            ready, observation = wake.pbs_observation({"job_id": "123", "helper": "helper"})
        self.assertFalse(ready)
        self.assertEqual(observation["status"], "UNKNOWN")

    def test_query_error_does_not_become_completion(self):
        with patch.object(wake, "query_jobs", side_effect=RuntimeError("scheduler unavailable")):
            with self.assertRaisesRegex(RuntimeError, "scheduler unavailable"):
                wake.pbs_observation({"job_id": "123", "helper": "helper"})

    def test_exit_failure_wakes_and_retains_exit_code(self):
        with patch.object(wake, "query_jobs", side_effect=[[], [{"job_id": "123", "status": "FINISH"}]]), \
             patch.object(wake.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "    Exit_status = 42\n", "")):
            ready, observation = wake.pbs_observation({"job_id": "123", "helper": "helper"})
        self.assertTrue(ready)
        self.assertEqual(observation["exit_status"], 42)

    def test_exiting_is_not_terminal(self):
        with patch.object(wake, "query_jobs", return_value=[{"job_id": "123", "status": "EXITING"}]):
            self.assertFalse(wake.pbs_observation({"job_id": "123", "helper": "helper"})[0])

    def test_server_and_array_identity(self):
        self.assertFalse(wake.same_job("123.server-a", "123.server-b"))
        self.assertFalse(wake.same_job("123[1]", "123[2]"))
        self.assertTrue(wake.same_job("123.server-a", "123"))
        self.assertTrue(wake.JOB_ID.fullmatch("123[4].server"))
        self.assertFalse(wake.JOB_ID.fullmatch("123[]"))

    def test_polling_respects_long_intervals_and_recovers_from_query_errors(self):
        scenarios = (
            (300, ["RUNNING", "FINISH"], [0, 300]),
            (1800, ["RUNNING", "FINISH"], [0, 1800]),
            (1800, ["ERROR", "RUNNING", "FINISH"], [0, 3600, 5400]),
            (300, ["ERROR", "ERROR", "RUNNING", "FINISH"], [0, 600, 1800, 2100]),
        )
        for interval, outcomes, expected_times in scenarios:
            with self.subTest(interval=interval, outcomes=outcomes):
                wake.write_state(self.directory, dict(self.state, kind="pbs", status="waiting",
                                                     poll_seconds=interval))
                now = 0
                queried_at = []

                def observe(state):
                    outcome = outcomes[len(queried_at)]
                    queried_at.append(now)
                    if outcome == "ERROR":
                        raise RuntimeError("scheduler unavailable")
                    return outcome == "FINISH", {"status": outcome}

                def sleep(seconds):
                    nonlocal now
                    # Simulate scheduling in minute steps without real sleeps.
                    now += max(seconds, 60)

                with patch.object(wake.time, "monotonic", side_effect=lambda: now), \
                     patch.object(wake.time, "sleep", side_effect=sleep), \
                     patch.object(wake, "pbs_observation", side_effect=observe), \
                     patch.object(wake, "deliver") as deliver:
                    wake.worker(self.directory)
                self.assertEqual(queried_at, expected_times)
                deliver.assert_called_once_with(self.directory)

    def test_long_poll_wait_can_be_cancelled_before_next_query(self):
        wake.write_state(self.directory, dict(self.state, kind="pbs", status="waiting",
                                             poll_seconds=3600))

        def cancel(seconds):
            self.assertLessEqual(seconds, 1)
            wake.update(self.directory, lambda s: s.update(status="cancelled"))

        with patch.object(wake, "pbs_observation", return_value=(False, {"status": "RUNNING"})) as query, \
             patch.object(wake.time, "sleep", side_effect=cancel), \
             patch.object(wake, "deliver") as deliver:
            wake.worker(self.directory)
        query.assert_called_once()
        deliver.assert_not_called()
        self.assertEqual(wake.read_state(self.directory)["status"], "cancelled")

    def test_registration_refuses_unseen_job_without_creating_event(self):
        helper = self.directory / "fake-helper.py"
        helper.write_text('print(\'{"ok": true, "jobs": []}\')\n')
        fake = self.directory / "fake-codex"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o700)
        state_root = self.directory / "events"
        result = subprocess.run(
            [sys.executable, "-I", str(Path(wake.__file__)), "pbs", "--job", "1234567",
             "--helper", str(helper), "--thread", "00000000-0000-0000-0000-000000000001",
             "--message", "Inspect results", "--remote", "unix:///test.sock",
             "--codex", str(fake), "--state-root", str(state_root)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("absent from active jobs", result.stderr)
        self.assertFalse(state_root.exists())

    def test_detached_timer_uses_exact_thread_and_waits_until_due(self):
        fake = self.directory / "fake-codex"
        receipt = self.directory / "receipt.json"
        fake.write_text("#!/usr/bin/python3\nimport json,sys,time\n"
                        f"open({str(receipt)!r}, 'w').write(json.dumps({{'argv':sys.argv[1:], 'at':time.time()}}))\n")
        fake.chmod(0o700)
        result = subprocess.run(
            [sys.executable, "-I", str(Path(wake.__file__)), "timer", "--seconds", "0.2",
             "--thread", "00000000-0000-0000-0000-000000000001", "--message", "literal $(false)",
             "--remote", "unix:///test.sock", "--codex", str(fake), "--state-root", str(self.directory)],
            capture_output=True, text=True, check=True)
        event = Path(json.loads(result.stdout)["event"])
        deadline = time.monotonic() + 5
        while wake.read_state(event)["status"] != "delivered" and time.monotonic() < deadline:
            time.sleep(0.02)
        state = wake.read_state(event)
        self.assertEqual(state["status"], "delivered", (event / "worker.log").read_text())
        record = json.loads(receipt.read_text())
        self.assertGreaterEqual(record["at"], state["due_at"])
        self.assertEqual(record["argv"][0:3], ["queue", "--thread", state["thread_id"]])
        self.assertIn("literal $(false)", record["argv"][4])


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.goal = dict(threadId="thread", createdAt=1, updatedAt=2, objective="Finish task",
                         tokenBudget=1000, tokensUsed=100, timeUsedSeconds=3, status="paused")
        self.state = dict(event_id="event", thread_id="thread", status="ready", resume_goal=True,
                          created_at=time.time(), goal_registered=dict(self.goal, status="active"),
                          goal_parked=self.goal.copy(), codex="codex", remote="unix:///test.sock",
                          message="Inspect results", observation={"status": "FINISH"})
        wake.write_state(self.directory, self.state)

    def test_waits_for_pause_and_turn_end(self):
        self.state.pop("goal_parked")
        self.assertEqual(wake.check_goal(self.state, dict(self.goal, status="active"), "idle")[0], "wait")
        self.assertEqual(wake.check_goal(self.state, self.goal, "active")[0], "wait")
        self.assertEqual(wake.check_goal(self.state, self.goal, "idle")[0], "park")

    def test_manual_changes_and_budget_exhaustion_revoke_resume(self):
        for change in ({"objective": "New task"}, {"status": "active"}, {"updatedAt": 3},
                       {"tokenBudget": 900}, {"tokensUsed": 1000}, {"createdAt": 9}):
            with self.subTest(change=change):
                self.assertEqual(wake.check_goal(self.state, dict(self.goal, **change), "idle")[0], "cancel")
        self.assertEqual(wake.check_goal(self.state, None, "idle")[0], "cancel")

    def test_busy_thread_defers_resume(self):
        self.assertEqual(wake.check_goal(self.state, self.goal, "active")[0], "wait")

    def mock_rpc(self, mutate=None, fail=None):
        goal = self.goal.copy()
        calls = []
        def call(method, params):
            calls.append((method, params))
            if method == "thread/goal/get":
                return {"goal": goal.copy()}
            if method == "thread/read":
                return {"thread": {"status": {"type": "idle"}}}
            if method == "thread/inject_items":
                if mutate:
                    goal.update(mutate)
                return {}
            if method == "thread/goal/set":
                if fail:
                    raise fail
                goal.update(params)
                return {"goal": goal.copy()}
            raise AssertionError(method)
        return call, calls

    def test_resume_injects_evidence_first_and_preserves_budget_and_usage(self):
        call, calls = self.mock_rpc()
        with patch.object(wake, "Rpc") as cls:
            cls.return_value.__enter__.return_value.call.side_effect = call
            wake.deliver_goal(self.directory)
        state = wake.read_state(self.directory)
        self.assertEqual(state["status"], "delivered")
        self.assertEqual(state["goal_before_resume"]["status"], "paused")
        self.assertEqual(state["goal_after_resume"]["status"], "active")
        self.assertEqual(state["goal_after_resume"]["tokensUsed"], 100)
        self.assertEqual(state["goal_after_resume"]["tokenBudget"], 1000)
        methods = [method for method, params in calls]
        self.assertLess(methods.index("thread/inject_items"), methods.index("thread/goal/set"))
        self.assertEqual(calls[-1], ("thread/goal/set", {"threadId": "thread", "status": "active"}))

    def test_goal_changed_during_injection_is_not_resumed(self):
        call, calls = self.mock_rpc(mutate={"objective": "Changed by user"})
        with patch.object(wake, "Rpc") as cls:
            cls.return_value.__enter__.return_value.call.side_effect = call
            wake.deliver_goal(self.directory)
        self.assertNotIn("thread/goal/set", [method for method, params in calls])
        self.assertEqual(wake.read_state(self.directory)["status"], "cancelled")

    def test_uncertain_goal_resume_is_not_repeated(self):
        call, calls = self.mock_rpc(fail=TimeoutError("connection lost"))
        with patch.object(wake, "Rpc") as cls:
            cls.return_value.__enter__.return_value.call.side_effect = call
            wake.deliver_goal(self.directory)
            wake.worker(self.directory)
        self.assertEqual(wake.read_state(self.directory)["status"], "uncertain")
        self.assertEqual([method for method, params in calls].count("thread/goal/set"), 1)


class TransportTests(unittest.TestCase):
    def client(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        rpc = wake.Rpc.__new__(wake.Rpc)
        rpc.sock, rpc.buffer = client, b""
        return rpc, server

    def test_fragmented_response_with_ping(self):
        rpc, server = self.client()
        first, last = b'{"value":', b'42}'
        server.sendall(bytes([1, len(first)]) + first + b'\x89\x01x'
                       + bytes([128, len(last)]) + last)
        self.assertEqual(rpc.receive(time.monotonic() + 1), {"value": 42})
        pong = server.recv(100)
        self.assertEqual(pong[0], 0x8A)
        self.assertEqual(pong[1], 0x81)
        self.assertEqual(pong[6] ^ pong[2], ord('x'))

    def test_extended_frame_and_masking(self):
        rpc, server = self.client()
        body = json.dumps({"text": "x" * 300}).encode()
        server.sendall(b'\x81\x7e' + struct.pack('!H', len(body)) + body)
        self.assertEqual(rpc.receive(time.monotonic() + 1), json.loads(body))
        rpc.frame(body)
        received = server.recv(4096)
        self.assertEqual(received[:2], b'\x81\xfe')
        mask, payload = received[4:8], received[8:]
        self.assertEqual(bytes(v ^ mask[i % 4] for i, v in enumerate(payload)), body)

    def test_unresponsive_socket_times_out(self):
        rpc, server = self.client()
        with self.assertRaises(TimeoutError):
            rpc.receive(time.monotonic() + 0.02)


if __name__ == "__main__":
    unittest.main()
