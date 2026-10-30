"""Submit, observe, cancel, adopt and export through the tool layer against the fake PBS."""

import json
import unittest

from base import FakePbsCase
from miyabi_mcp import export
from miyabi_mcp.util import canonical


class ContextTests(FakePbsCase):
    def test_context_and_queues(self):
        context = self.ok_call("pbs_context")
        self.assertEqual(context["settings"]["project_dir"], str(self.project))
        self.assertEqual(context["pbs_projects"], ["gtest"])
        self.assertTrue(context["scheduled_stop"].startswith("2026-11-25T09:00:00"))
        self.assertTrue((self.project / ".miyabi-mcp" / ".gitignore").exists() or True)
        queues = self.ok_call("pbs_queues", {"system": "Miyabi-G"})
        self.assertIn("debug-g", {q["name"] for q in queues["queues"]})
        self.assertTrue(all(row["system"] == "Miyabi-G" for row in queues["limits"]))

    def test_no_project_blocks_writes_but_not_reads(self):
        settings_args = {"project_dir": str(self.user_root / "missing")}
        self.assertTrue(self.call("job_list", **settings_args)["ok"])
        result = self.call("job_submit", {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00"},
                           **settings_args)
        self.assertEqual(result["error"]["type"], "no_project")


class SubmitTests(FakePbsCase):
    def test_plan_then_commit_records_the_job(self):
        args = {"script": str(self.script), "queue": "debug-g", "walltime": "00:20:00", "name": "job-a",
                "client_ref": "ATT-0001-1", "labels": {"agent": "w1", "code_commit": "abc"}, "env": {"X": "a,b"}}
        plan = self.ok_call("job_submit", args)
        self.assertTrue(plan["valid"])
        self.assertTrue(plan["plan_digest"].startswith("blake2b:"))
        self.assertIn("code.mutable", {c["id"] for c in plan["checks"]})
        missing = self.call("job_submit", {**args, "dry_run": False})
        self.assertEqual(missing["error"]["type"], "plan_required")
        changed = self.call("job_submit", {**args, "walltime": "00:25:00", "dry_run": False,
                                           "plan_digest": plan["plan_digest"]})
        self.assertEqual(changed["error"]["type"], "plan_mismatch")
        done = self.ok_call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})
        job_id = done["job_id"]
        self.assertTrue(job_id.endswith(".opbs"))
        fake = self.job(job_id)
        self.assertEqual(fake["variables"]["MIYABI_CLIENT_REF"], "ATT-0001-1")
        self.assertEqual(fake["variables"]["X"], "a,b")
        self.assertEqual(fake["variables"]["MIYABI_PROJECT_DIR"], str(self.project))
        self.assertTrue(fake["output"].startswith(str(self.project / ".miyabi-mcp" / "logs")))
        self.assertEqual(fake["cwd"], str(self.project))
        [event] = done["events"]
        self.assertEqual((event["kind"], event["execution_state"], event["raw_state"], event["source"]),
                         ("submitted", "queued", "Q", "qsub"))
        self.assertIn("pbs_time", event)
        record = self.ok_call("ledger_query")["records"][0]
        self.assertEqual(record["labels"]["code_commit"], "abc")
        self.assertEqual(record["submit"]["unit"], "node_hours")

    def test_request_key_makes_retries_idempotent(self):
        first = self.submit(request_key="sr:ATT-1")
        again = self.submit(request_key="sr:ATT-1")
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["job_id"], first["job_id"])
        self.assertEqual(len(self.read_state()["jobs"]), 1)

    def test_rejected_plans(self):
        cases = [({"queue": "small-g"}, "queue.name"), ({"walltime": "01:00:00"}, "queue.walltime"),
                 ({"script_text": "#!/bin/bash\necho <entrypoint.py>\n", "script": None}, "script.placeholders"),
                 ({"script_text": "#!/bin/bash\nif then\n", "script": None}, "script.syntax"),
                 ({"output": "/tmp/x.log"}, "output")]
        for override, check_id in cases:
            args = {"script": str(self.script), "queue": "debug-g", "walltime": "00:20:00", **override}
            args = {k: v for k, v in args.items() if v is not None}
            plan = self.ok_call("job_submit", args)
            self.assertFalse(plan["valid"], override)
            self.assertIn(check_id, {c["id"] for c in plan["checks"] if c["status"] == "fail"}, override)

    def test_qsub_failure_is_reported_and_the_key_can_retry(self):
        state = self.read_state()
        state["qsub_mode"] = "fail"
        self.write_state(state)
        args = {"script": str(self.script), "queue": "debug-g", "walltime": "00:20:00", "request_key": "k1"}
        plan = self.ok_call("job_submit", args)
        failed = self.call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})
        self.assertEqual(failed["error"]["type"], "qsub_failed")
        state = self.read_state()
        state["qsub_mode"] = "ok"
        self.write_state(state)
        self.assertTrue(self.call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})["ok"])

    def test_unparsable_qsub_output_is_reconciled_by_submit_id(self):
        state = self.read_state()
        state["qsub_mode"] = "garbage"
        self.write_state(state)
        result = self.submit(request_key="k2")
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["job_id"], "3800000.opbs")
        again = self.submit(request_key="k2")
        self.assertTrue(again["idempotent"])


class PolicyTests(FakePbsCase):
    def write_policy(self, policy: dict) -> None:
        state = self.project / ".miyabi-mcp"
        state.mkdir(exist_ok=True)
        (state / "policy.json").write_text(json.dumps(policy))

    def test_mig_budget_window(self):
        self.write_policy({"queues": ["debug-mig"], "per_job": {"max_mig_instances": 1, "max_walltime": "00:10:00"},
                           "budget_windows": [{"window": "24h", "max_requested_mig_instance_hours": 1.0}],
                           "require_plan": True})
        bad_queue = self.ok_call("job_submit", {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00"})
        self.assertIn("policy.queue", {c["id"] for c in bad_queue["checks"] if c["status"] == "fail"})
        too_long = self.ok_call("job_submit", {"script": str(self.script), "queue": "debug-mig",
                                               "walltime": "00:15:00"})
        self.assertIn("policy.per_job", {c["id"] for c in too_long["checks"] if c["status"] == "fail"})
        for index in range(6):
            result = self.submit(queue="debug-mig", walltime="00:10:00", name=f"mig{index}")
            self.assertEqual(self.job(result["job_id"])["mig"], True)
        seventh = self.ok_call("job_submit", {"script": str(self.script), "queue": "debug-mig", "walltime": "00:10:00"})
        self.assertFalse(seventh["valid"])
        self.assertIn("project.budget[24h]", {c["id"] for c in seventh["checks"] if c["status"] == "fail"})

    def test_bad_policy_fails_closed(self):
        self.write_policy({"queuez": ["debug-g"]})
        result = self.call("job_submit", {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00"})
        self.assertEqual(result["error"]["type"], "invalid_policy")


class LifecycleTests(FakePbsCase):
    def test_events_and_export_follow_the_job(self):
        job_id = self.submit(client_ref="ATT-0001-1")["job_id"]
        self.start(job_id)
        self.ok_call("job_list")
        self.finish(job_id, exit_status=0, used="00:10:17")
        listed = self.ok_call("job_list")
        self.assertEqual(listed["count"], 0)
        record = self.ok_call("ledger_query")["records"][0]
        self.assertEqual(record["state"]["raw_state"], "F Exit_status=0")
        self.assertEqual(record["usage"]["quantity"], 0.171389)
        exported = self.ok_call("ledger_query", {"format": "smooth-research", "include_token": True})
        observations = exported["observations"]
        self.assertEqual([o["execution_state"] for o in observations], ["queued", "running", "ended"])
        self.assertEqual([o["source_sequence"] for o in observations], [1, 2, 3])
        self.assertEqual(observations[0]["source_ref"].split(" @")[0], f"qsub {job_id}")
        self.assertEqual(observations[1]["source_ref"].split(" @")[0], f"qstat -f {job_id}")
        self.assertEqual(observations[2]["source_ref"].split(" @")[0], f"qstat -H -f {job_id}")
        self.assertEqual(observations[2]["usage"]["accounting_ref"], f"miyabi-pbs:{job_id}")
        self.assertEqual(observations[2]["usage"]["source_ref"], "resources_used.walltime=00:10:17 x nvnodes-g=1")
        self.assertTrue(all(o["attempt_id"] == "ATT-0001-1" for o in observations))
        self.assertEqual(exported["token_usages"][0]["accounting_ref"], f"miyabi-pbs:{job_id}:token")
        again = self.ok_call("ledger_query", {"format": "smooth-research"})
        self.assertEqual([canonical(o) for o in again["observations"]], [canonical(o) for o in observations])
        resumed = self.ok_call("ledger_query", {"format": "smooth-research", "cursor": exported["next_cursor"]})
        self.assertEqual(resumed["observations"], [])

    def test_cancel_needs_a_dry_run_and_marks_the_outcome(self):
        queued = self.submit(name="q1")["job_id"]
        running = self.submit(name="r1")["job_id"]
        self.start(running)
        plan = self.ok_call("job_cancel", {"job_ids": [queued, running]})
        self.assertEqual({t["action"] for t in plan["targets"]}, {"qdel"})
        no_reason = self.call("job_cancel", {"job_ids": [queued, running], "dry_run": False,
                                             "targets_digest": plan["targets_digest"]})
        self.assertFalse(no_reason["ok"])
        done = self.ok_call("job_cancel", {"job_ids": [queued, running], "dry_run": False, "reason": "test",
                                           "targets_digest": plan["targets_digest"]})
        self.assertEqual(sorted(done["cancelled"]), sorted([queued, running]))
        records = {r["job_id"]: r for r in self.ok_call("ledger_query")["records"]}
        self.assertEqual(records[queued]["state"]["raw_state"], "FINISH no-start")
        self.assertEqual(records[queued]["state"]["outcome"], "cancelled")
        self.assertEqual(records[running]["state"]["raw_state"], "F Exit_status=271")
        self.assertEqual(records[running]["state"]["outcome"], "cancelled")
        kinds = [e["kind"] for e in self.ok_call("ledger_query", {"format": "events"})["events"]]
        self.assertIn("cancel_requested", kinds)
        exported = self.ok_call("ledger_query", {"format": "smooth-research"})["observations"]
        self.assertNotIn("cancel_requested", {o["raw_state"] for o in exported})

    def test_external_jobs_are_refused_then_adopted(self):
        state = self.read_state()
        state["jobs"]["3700000"] = {"name": "ext", "state": "Q", "queue": "debug-g", "route": "debug-g",
                                    "project": "gtest", "select": "1:mpiprocs=1", "units": 1, "mig": False,
                                    "walltime": "00:10:00", "qtime": 1793300000, "stime": None, "obittime": None,
                                    "exit_status": None, "used": None, "output": "/work/x.log", "variables": {},
                                    "submit_args": "-q debug-g ext.pbs"}
        self.write_state(state)
        plan = self.ok_call("job_cancel", {"job_ids": ["3700000"]})
        self.assertEqual(plan["targets"][0]["action"], "refused")
        adopted = self.ok_call("job_annotate", {"job_id": "3700000", "adopt": True, "labels": {"agent": "w2"},
                                                 "note": "found running outside miyabi-mcp"})
        self.assertTrue(adopted["adopted"])
        self.assertEqual(adopted["record"]["origin"], "adopted")
        plan = self.ok_call("job_cancel", {"job_ids": ["3700000"]})
        self.assertEqual(plan["targets"][0]["action"], "qdel")

    def test_other_projects_find_the_owner(self):
        job_id = self.submit()["job_id"]
        other = self.user_root / "other"
        other.mkdir()
        details = self.ok_call("job_get", {"job_ids": [job_id]}, project_dir=other)["jobs"][0]
        self.assertEqual(details["project"], str(self.project))
        self.assertEqual(details["ledger"]["submit_id"], self.ok_call("ledger_query")["records"][0]["submit_id"])
        refused = self.call("job_annotate", {"job_id": job_id, "adopt": True}, project_dir=other)
        self.assertEqual(refused["error"]["type"], "owned_elsewhere")

    def test_job_get_tail(self):
        job_id = self.submit()["job_id"]
        output = self.job(job_id)["output"]
        with open(output, "w") as stream:
            stream.write("".join(f"line {i}\n" for i in range(10)))
        details = self.ok_call("job_get", {"job_ids": [job_id], "tail_lines": 3})["jobs"][0]
        self.assertEqual(details["output_tail"], "line 7\nline 8\nline 9")
        self.assertIn("MIYABI_SUBMIT_ID", details["variables"])


class ExportShapeTests(unittest.TestCase):
    def test_matches_the_smooth_research_contract_fixture(self):
        """The three events of the shared fixture (smooth-research 9398c63) export to the same observations."""
        locator = "<state>/jobs/3731052/record.json"
        base = {"job_id": "3731052.opbs", "client_ref": "ATT-0001-1", "locator": locator, "outcome": "unknown"}
        events = [
            {**base, "event_id": "miyabi-pbs:3731052.opbs:1", "seq": 1, "kind": "submitted", "source": "qsub",
             "observer": "miyabi-g2", "observed_at": "2026-10-30T05:22:06+09:00", "execution_state": "queued",
             "raw_state": "Q", "pbs_time": "2026-10-30T05:22:05+09:00"},
            {**base, "event_id": "miyabi-pbs:3731052.opbs:2", "seq": 2, "kind": "running", "source": "qstat -f",
             "observer": "miyabi-g1", "observed_at": "2026-10-30T05:43:10+09:00", "execution_state": "running",
             "raw_state": "R", "pbs_time": "2026-10-30T05:42:26+09:00"},
            {**base, "event_id": "miyabi-pbs:3731052.opbs:3", "seq": 3, "kind": "ended", "source": "qstat -H -f",
             "observer": "miyabi-g1", "observed_at": "2026-10-30T05:53:30+09:00", "execution_state": "ended",
             "outcome": "success", "raw_state": "F Exit_status=0", "pbs_time": "2026-10-30T05:52:49+09:00",
             "exit_status": 0, "usage": {"quantity": 0.171389, "unit": "node_hours", "resource_type": "gpu_node",
                                         "hardware": "GH200", "basis": "measured",
                                         "source_ref": "resources_used.walltime=00:10:17 x nvnodes-g=1"}},
        ]
        observations = [export.observation(e) for e in events]
        self.assertEqual(observations[0]["source_ref"], "qsub 3731052.opbs @miyabi-g2")
        self.assertEqual(observations[1]["source_ref"], "qstat -f 3731052.opbs @miyabi-g1")
        self.assertEqual(observations[2]["usage"], {
            "accounting_ref": "miyabi-pbs:3731052.opbs", "quantity": 0.171389, "unit": "node_hours",
            "resource_type": "gpu_node", "hardware": "GH200", "basis": "measured",
            "source_ref": "resources_used.walltime=00:10:17 x nvnodes-g=1"})
        self.assertEqual(set(observations[0]), {"record_id", "attempt_id", "source", "observed_at", "occurred_at",
                                                "source_sequence", "execution_state", "outcome", "raw_state",
                                                "source_ref", "execution_ref"})
        self.assertIsNone(export.observation({**events[0], "kind": "cancel_requested"}))


if __name__ == "__main__":
    unittest.main()
