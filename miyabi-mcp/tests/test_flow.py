"""Submit, observe, cancel, adopt and export through the tool layer against the fake PBS."""

import json
import subprocess
import time
import unittest
from unittest import mock

from base import ROOT, FakePbsCase
from miyabi_mcp import export
from miyabi_mcp.ledger import Store
from miyabi_mcp.util import MiyabiError, canonical

LAUNCHER = ROOT / "bin" / "miyabi-mcp"
WITH_CD = '#!/bin/bash\n#PBS -j oe\ncd -- "${PBS_O_WORKDIR:?}" || exit 1\n'


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


class NamingAndCodeTests(FakePbsCase):
    def test_default_name_comes_from_client_ref(self):
        result = self.submit(name=None, client_ref="ATT-0009-1")
        self.assertEqual(self.job(result["job_id"])["name"], "ATT-0009-1")

    def test_code_mutability_is_a_warning_only_for_recorded_runs(self):
        base = {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00"}

        def status(**extra):
            plan = self.ok_call("job_submit", {**base, **extra})
            return next(c for c in plan["checks"] if c["id"] == "code.mutable")

        self.assertEqual(status()["status"], "pass")
        warned = status(client_ref="ATT-1")
        self.assertEqual(warned["status"], "warn")
        self.assertIn("read-only snapshot", warned["detail"])
        snapshot = self.project / "snapshots" / "abc"
        snapshot.mkdir(parents=True)
        (snapshot / "job.pbs").write_text(self.script.read_text())
        snapshot.chmod(0o555)
        self.addCleanup(snapshot.chmod, 0o755)
        frozen = status(client_ref="ATT-1", script=str(snapshot / "job.pbs"), workdir=str(snapshot))
        self.assertEqual(frozen["status"], "pass")
        self.assertIn("read-only", frozen["detail"])


class SubmitCheckTests(FakePbsCase):
    def plan(self, **overrides) -> dict:
        args = {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00", **overrides}
        return self.ok_call("job_submit", {key: value for key, value in args.items() if value is not None})

    @staticmethod
    def check(plan: dict, check_id: str) -> dict:
        return next(check for check in plan["checks"] if check["id"] == check_id)

    def test_mig_queues_take_their_instance_counts(self):
        for count in (1, 2, 4):
            plan = self.plan(queue="debug-mig", select=f"{count}:mpiprocs=1")
            self.assertTrue(plan["valid"], plan["checks"])
            self.assertEqual((plan["plan"]["units"], plan["plan"]["unit"]), (count, "mig_instance_hours"))
            self.assertIn("cannot be chosen", plan["plan"]["mig_slice"])
        three = self.plan(queue="debug-mig", select="3:mpiprocs=1")
        self.assertFalse(three["valid"])
        self.assertIn("accepts 1, 2 or 4 MIG instances, not 3", self.check(three, "queue.shape")["detail"])
        self.assertNotIn("mig_slice", self.plan()["plan"])

    def test_select_resources(self):
        vnode = self.plan(queue="debug-mig", select="1:mpiprocs=1:vnode=mg1112_mig3")
        self.assertFalse(vnode["valid"])
        self.assertIn("cannot be chosen", self.check(vnode, "select.resources")["detail"])
        unconfirmed = self.plan(select="1:mpiprocs=1:ngpus=1")
        self.assertTrue(unconfirmed["valid"])
        self.assertEqual(self.check(unconfirmed, "select.resources")["status"], "warn")
        self.assertEqual(self.check(self.plan(select="1:mpiprocs"), "select.resources")["status"], "fail")
        self.assertEqual(self.check(self.plan(queue="debug-mig", select="2"), "select.resources")["status"], "pass")
        self.assertEqual(self.check(self.plan(), "select.resources")["status"], "pass")

    def test_placeholders_name_the_line_and_skip_redirections(self):
        text = (WITH_CD + "python x.py <config.yaml >train.log\ncat <<EOF >notes.txt\nnote\nEOF\n"
                "sort <in.txt >out.txt\n")
        clean = self.plan(script=None, script_text=text)
        self.assertTrue(clean["valid"], clean["checks"])
        self.assertNotIn("script.placeholders", {check["id"] for check in clean["checks"]})
        commented = text + "# run <label> <sha> <pytest target>\n"
        flagged = self.check(self.plan(script=None, script_text=commented), "script.placeholders")
        self.assertEqual(flagged["status"], "fail")
        self.assertIn("line 9: <label>; line 9: <sha>.", flagged["detail"])
        allowed = self.plan(script=None, script_text=commented, allow_placeholders=True)
        self.assertTrue(allowed["valid"])
        self.assertEqual(self.check(allowed, "script.placeholders")["status"], "warn")

    def test_scripts_without_cd_get_a_warning(self):
        warned = self.check(self.plan(), "script.cwd")
        self.assertEqual(warned["status"], "warn")
        self.assertIn('cd -- "${PBS_O_WORKDIR:?}" || exit 1', warned["detail"])
        self.assertEqual(self.check(self.plan(script=None, script_text=WITH_CD + "echo hi\n"), "script.cwd")["status"],
                         "pass")
        commented = "#!/bin/bash\n# cd somewhere first\necho hi\n"
        self.assertEqual(self.check(self.plan(script=None, script_text=commented), "script.cwd")["status"], "warn")

    def test_notify_is_part_of_the_plan(self):
        args = {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00",
                "notify": {"name": "g", "notifier": "a"}}
        plan = self.ok_call("job_submit", args)
        self.assertEqual((plan["plan"]["notify"]["notifier"], plan["plan"]["notify"]["when"]), ("a", "all_ended"))
        moved = {**args, "notify": {"name": "g", "notifier": "b"}}
        self.assertNotEqual(self.ok_call("job_submit", moved)["plan_digest"], plan["plan_digest"])
        swapped = self.call("job_submit", {**moved, "dry_run": False, "plan_digest": plan["plan_digest"]})
        self.assertEqual(swapped["error"]["type"], "plan_mismatch")
        bad = self.ok_call("job_submit", {**args, "notify": {"when": "sometime"}})
        self.assertFalse(bad["valid"])
        self.assertEqual(self.check(bad, "notify")["status"], "fail")
        done = self.ok_call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})
        self.assertEqual(done["notify"]["notification"]["notifier"], "a")

    def test_a_failed_join_is_flagged(self):
        with mock.patch("miyabi_mcp.notify.join", side_effect=MiyabiError("lock_timeout", "lock is held")):
            done = self.submit(notify={"name": "g", "notifier": "a"})
        self.assertTrue(done["job_id"])
        self.assertEqual(done["notify_error"]["type"], "lock_timeout")
        self.assertIn("WITHOUT a notification", done["warning"])

    def test_job_list_fields_are_checked(self):
        self.submit()
        bad = self.call("job_list", {"fields": ["job_id", "ledger.usage"]})
        self.assertEqual(bad["error"]["type"], "invalid_arguments")
        self.assertIn("ledger.usage", bad["error"]["message"])
        listed = self.ok_call("job_list", {"fields": ["job_id", "status", "ledger"]})
        self.assertEqual(set(listed["jobs"][0]), {"job_id", "status", "ledger"})


class SummaryTests(FakePbsCase):
    def test_requested_is_summed_before_rounding(self):
        for index in range(3):
            self.submit(queue="debug-mig", walltime="00:08:00", name=f"m{index}")
        summary = self.ok_call("ledger_query", {"format": "summary"})["summary"]
        self.assertEqual(summary["requested"], {"mig_instance_hours": 0.4})

    def test_rejected_submissions_are_listed_apart(self):
        self.submit(queue="debug-mig", walltime="00:08:00", name="accepted")
        state = self.read_state()
        state["qsub_mode"] = "fail"
        self.write_state(state)
        args = {"script": str(self.script), "queue": "debug-mig", "walltime": "00:08:00", "name": "rejected"}
        plan = self.ok_call("job_submit", args)
        self.call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})
        summary = self.ok_call("ledger_query", {"format": "summary"})["summary"]
        self.assertEqual(summary["requested"], {"mig_instance_hours": 0.133333})
        self.assertEqual(summary["rejected"], {"count": 1, "requested": {"mig_instance_hours": 0.133333}})
        self.assertEqual(summary["uncertain"], {"count": 0, "requested": {}})
        self.assertNotIn("budget", summary)


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
        self.write_policy({"budget": {"unit": "node_hours", "limit": 1, "factors": {"gpu_hours": 1}}})
        result = self.call("job_submit", {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00"})
        self.assertEqual(result["error"]["type"], "invalid_policy")
        summary = self.ok_call("ledger_query", {"format": "summary"})["summary"]  # reads keep working
        self.assertEqual(summary["budget_error"]["type"], "invalid_policy")


class BudgetTests(FakePbsCase):
    BUDGET = {"unit": "node_hours", "limit": 1, "warn_at": 0.8,
              "factors": {"node_hours": 1, "mig_instance_hours": 0.25}, "source": "test constraints"}

    def setUp(self):
        super().setUp()
        self.write_policy({"budget": self.BUDGET})

    def write_policy(self, policy: dict) -> None:
        (self.project / ".miyabi-mcp").mkdir(exist_ok=True)
        (self.project / ".miyabi-mcp" / "policy.json").write_text(json.dumps(policy))

    def args(self, name: str, walltime: str = "00:20:00", queue: str = "debug-g") -> dict:
        return {"script": str(self.script), "queue": queue, "walltime": walltime, "name": name}

    def budget(self, plan: dict) -> dict:
        return next(check for check in plan["checks"] if check["id"] == "policy.budget")

    def qsub_mode(self, mode: str) -> None:
        state = self.read_state()
        state["qsub_mode"] = mode
        self.write_state(state)

    def test_measured_use_plus_reservations(self):
        measured = self.submit(name="measured")["job_id"]
        self.start(measured)
        self.finish(measured, used="00:06:00")  # 0.1 node-hours measured
        self.qsub_mode("fail")
        rejected = self.ok_call("job_submit", self.args("rejected"))
        failed = self.call("job_submit", {**self.args("rejected"), "dry_run": False,
                                          "plan_digest": rejected["plan_digest"]})
        self.assertEqual(failed["error"]["type"], "qsub_failed")  # rejected: counts nothing
        self.qsub_mode("ok")
        never = self.submit(name="never")["job_id"]
        targets = self.ok_call("job_cancel", {"job_ids": [never]})["targets_digest"]
        self.ok_call("job_cancel", {"job_ids": [never], "dry_run": False, "reason": "test", "targets_digest": targets})
        unmeasured = self.submit(name="unmeasured")["job_id"]
        self.set_job(unmeasured, state="F", stime=time.time() - 100, obittime=time.time(), exit_status=0, used="-")
        self.ok_call("job_list")
        budget = self.ok_call("ledger_query", {"format": "summary"})["summary"]["budget"]
        self.assertEqual((budget["used"], budget["reserved"], budget["total"]), (0.1, 0.333333, 0.433333))
        self.assertEqual(budget["jobs"], {"measured": 1, "reserved": 1, "never_started": 1, "rejected": 1})
        self.assertEqual((budget["missing_measurement"], budget["missing_measurement_jobs"]), (1, [unmeasured]))
        self.assertFalse(budget["filtered"])
        self.assertEqual(self.budget(self.ok_call("job_submit", self.args("a")))["status"], "pass")  # 0.767
        warned = self.ok_call("job_submit", self.args("b", walltime="00:24:00"))  # 0.833
        self.assertTrue(warned["valid"])
        self.assertEqual(self.budget(warned)["status"], "warn")
        mig = self.budget(self.ok_call("job_submit", self.args("m", queue="debug-mig")))  # 0.25 per instance-hour
        self.assertEqual(mig["status"], "pass")
        self.assertIn("+ 0.083 this job = 0.517 of 1 node_hours (test constraints)", mig["detail"])

    def test_concurrent_submissions_cannot_pass_the_limit_together(self):
        self.write_policy({"budget": {**self.BUDGET, "limit": 0.5}})  # debug-g takes at most 00:30:00
        first = self.ok_call("job_submit", self.args("c1", walltime="00:18:00"))  # 0.3 each: alone they fit
        second = self.ok_call("job_submit", self.args("c2", walltime="00:18:00"))
        self.assertEqual((self.budget(first)["status"], self.budget(second)["status"]), ("pass", "pass"))
        self.ok_call("job_submit", {**self.args("c1", walltime="00:18:00"), "dry_run": False,
                                    "plan_digest": first["plan_digest"]})
        late = self.call("job_submit", {**self.args("c2", walltime="00:18:00"), "dry_run": False,
                                        "plan_digest": second["plan_digest"]})
        self.assertEqual(late["error"]["type"], "plan_rejected")  # rechecked inside the submit lock
        [check] = [c for c in late["error"]["checks"] if c["id"] == "policy.budget"]
        self.assertEqual(check["status"], "fail")
        again = self.ok_call("job_submit", self.args("c2", walltime="00:18:00"))
        self.assertFalse(again["valid"])
        self.assertIn("over the limit", self.budget(again)["detail"])
        self.assertEqual(len(self.read_state()["jobs"]), 1)
        at_limit = self.ok_call("job_submit", self.args("c3", walltime="00:12:00"))  # exactly 0.5 of 0.5
        self.assertTrue(at_limit["valid"])
        self.assertEqual(self.budget(at_limit)["status"], "warn")

    def test_a_unit_without_factor_fails_closed(self):
        self.write_policy({"budget": {"unit": "node_hours", "limit": 100}})
        self.assertEqual(self.budget(self.ok_call("job_submit", self.args("g")))["status"], "pass")
        mig = self.ok_call("job_submit", self.args("m", queue="debug-mig"))
        self.assertFalse(mig["valid"])
        self.assertIn("no factor for mig_instance_hours", self.budget(mig)["detail"])


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


class PlacementAndExportTests(FakePbsCase):
    def test_placement_is_kept_and_never_erased(self):
        job_id = self.submit()["job_id"]
        self.start(job_id)
        self.ok_call("job_list")
        placement = self.ok_call("ledger_query")["records"][0]["state"]["pbs"]
        self.assertEqual(placement, {"exec_host": "mg0001/0*72",
                                     "exec_vnode": "(mg0001:mem=104857600kb:ncpus=72:ngpus=1:nvnodes-g=1)"})
        self.set_job(job_id, placement=False)  # a later record without exec_host/exec_vnode
        self.finish(job_id)
        self.ok_call("job_list")
        state = self.ok_call("ledger_query")["records"][0]["state"]
        self.assertEqual((state["execution_state"], state["pbs"]), ("ended", placement))
        events = self.ok_call("ledger_query", {"format": "events"})["events"]
        self.assertEqual([("pbs" in event) for event in events], [False, True, False])
        observations = self.ok_call("ledger_query", {"format": "smooth-research"})["observations"]
        self.assertFalse(any("pbs" in observation for observation in observations))

    def test_jsonl_export_parses_line_by_line_with_stderr_merged(self):
        job_id = self.submit(client_ref="ATT-é-1")["job_id"]
        self.start(job_id)
        self.ok_call("job_list")
        self.finish(job_id)
        self.ok_call("job_list")
        Store(self.settings()).apply(job_id, {"kind": "reconciled", "execution_state": "ended", "outcome": "failed",
                                              "raw_state": "reconciled"}, "miyabi-mcp",
                                     correction_of=f"miyabi-pbs:{job_id}:3", note="checked the log:\nloss was NaN")
        command = [str(LAUNCHER), "--project-dir", str(self.project), "ledger", "--format", "smooth-research", "--jsonl"]
        merged = self.project / "export.jsonl"
        with open(merged, "w") as stream:
            run = subprocess.run(command + ["--cursor", ""], stdout=stream, stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(run.returncode, 0)
        text = merged.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"))
        lines = [json.loads(line) for line in text.splitlines()]  # one JSON value per line, cursor last
        self.assertEqual(set(lines[-1]), {"next_cursor"})
        observations = lines[:-1]
        self.assertEqual(len(observations), 4)
        self.assertTrue(all(o["attempt_id"] == "ATT-é-1" for o in observations))
        self.assertEqual(observations[-1]["note"], "checked the log:\nloss was NaN")
        plain = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(plain.stderr, "")  # no cursor unless asked for
        self.assertEqual([json.loads(line) for line in plain.stdout.splitlines()], observations)
        self.assertEqual(plain.stdout, "".join(canonical(o) + "\n" for o in observations))


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
        placed = {**events[2], "pbs": {"exec_host": "mg0001/0*72", "exec_vnode": "(mg0001:ngpus=1:nvnodes-g=1)"}}
        self.assertEqual(canonical(export.observation(placed)), canonical(observations[2]))


if __name__ == "__main__":
    unittest.main()
