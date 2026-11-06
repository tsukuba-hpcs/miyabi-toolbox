"""Submit, observe, cancel, adopt and export through the tool layer against the fake PBS."""

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from base import ROOT, FakePbsCase
from miyabi_mcp import export, jobs
from miyabi_mcp.ledger import Store
from miyabi_mcp.pbs import Pbs
from miyabi_mcp.util import Lock, MiyabiError, canonical

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

    def test_array_jobs_are_refused(self):
        self.assertEqual(self.check(self.plan(array="1-100"), "array")["status"], "fail")
        self.script.write_text("#!/bin/bash\n#PBS -J 1-100\n#PBS -r y\necho $PBS_ARRAY_INDEX\n")
        plan = self.plan()
        self.assertFalse(plan["valid"])
        self.assertEqual(self.check(plan, "array")["status"], "fail")
        self.assertNotIn("script.directives", {c["id"] for c in plan["checks"]})  # nothing overrides -J or -r

    def test_only_what_the_command_line_changes_is_reported_as_overridden(self):
        self.script.write_text("#!/bin/bash\n#PBS -l walltime=01:00:00\n#PBS -m abe\n#PBS -W group_list=gtest\n"
                               "#PBS -j oe\necho hi\n")
        detail = self.check(self.plan(), "script.directives")["detail"]
        self.assertIn("-l walltime walltime=01:00:00 (it gives walltime=00:10:00)", detail)
        self.assertNotIn("-m", detail)
        self.assertNotIn("group_list", detail)  # the same value (11-06: every plan warned about it)
        self.assertNotIn("-j", detail)
        self.script.write_text("#!/bin/bash\n#PBS -W group_list=gtest\n#PBS -q debug-g\necho hi\n")
        self.assertNotIn("script.directives", {c["id"] for c in self.plan()["checks"]})
        self.script.write_text("#!/bin/bash\n#PBS -W group_list=gother\necho hi\n")
        detail = self.check(self.plan(), "script.directives")["detail"]
        self.assertIn("-W group_list group_list=gother (it gives group_list=gtest)", detail)

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

    def test_a_policy_that_is_not_an_object_is_refused(self):
        state = self.project / ".miyabi-mcp"
        state.mkdir(exist_ok=True)
        args = {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00"}
        for text in ("null", "[]", "0", '""'):
            (state / "policy.json").write_text(text)
            self.assertEqual(self.call("job_submit", args)["error"]["type"], "invalid_policy", text)
        (state / "policy.json").unlink()
        self.assertTrue(self.ok_call("job_submit", args)["valid"])

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

    def test_two_projects_cannot_both_adopt_a_job(self):
        state = self.read_state()
        state["jobs"]["3700000"] = {"name": "ext", "state": "Q", "queue": "debug-g", "route": "debug-g",
                                    "project": "gtest", "select": "1:mpiprocs=1", "units": 1, "mig": False,
                                    "walltime": "00:10:00", "qtime": 1793300000, "stime": None, "obittime": None,
                                    "exit_status": None, "used": None, "output": "/work/x.log", "variables": {},
                                    "submit_args": "-q debug-g ext.pbs"}
        self.write_state(state)
        other = self.user_root / "other"
        other.mkdir()
        real, race = jobs.index_owner, {}

        def owner_then_race(settings, key):  # the other project adopts while this one decides
            if not race:
                race["thread"] = threading.Thread(target=lambda: race.update(other=self.call(
                    "job_annotate", {"job_id": "3700000", "adopt": True}, project_dir=other)))
                race["thread"].start()
                time.sleep(0.5)
            return real(settings, key)

        with mock.patch.object(jobs, "index_owner", owner_then_race):
            mine = self.ok_call("job_annotate", {"job_id": "3700000", "adopt": True})
        race["thread"].join(60)
        self.assertTrue(mine["adopted"])
        self.assertEqual(race["other"]["error"]["type"], "owned_elsewhere")
        self.assertEqual(self.ok_call("ledger_query", project_dir=other)["records"], [])

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
                                              "raw_state": "reconciled"}, "miyabi-mcp", observed_ts=None,
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


class LiveLimitTests(FakePbsCase):
    """The live checks of a commit see the jobs accepted just before it (a cached table would not)."""

    def plans(self, *names: str) -> list[tuple[dict, dict]]:
        result = []
        for name in names:
            args = {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00", "name": name}
            plan = self.ok_call("job_submit", args)
            self.assertTrue(plan["valid"], plan["checks"])
            result.append((args, plan))
        return result

    def commit(self, args: dict, plan: dict) -> dict:
        return self.call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})

    def active(self) -> int:
        return sum(1 for job in self.read_state()["jobs"].values() if job["state"] != "F")

    def test_back_to_back_commits_respect_max_active_jobs(self):
        (self.project / ".miyabi-mcp").mkdir(exist_ok=True)
        (self.project / ".miyabi-mcp" / "policy.json").write_text(json.dumps(
            {"max_active_jobs": 1, "label_limits": {"agent": {"max_active_jobs": 1}}}))
        (first, first_plan), (second, second_plan) = self.plans("a", "b")
        self.assertTrue(self.commit(first, first_plan)["ok"])
        refused = self.commit(second, second_plan)  # within 15 s: the cached table predates the first job
        self.assertEqual(refused["error"]["type"], "plan_rejected")
        self.assertIn("policy.max_active_jobs", {c["id"] for c in refused["error"]["checks"] if c["status"] == "fail"})
        self.assertEqual(self.active(), 1)
        again = self.ok_call("job_submit", second)  # a dry run now sees the job as well
        self.assertFalse(again["valid"])

    def test_user_cap_counts_jobs_of_other_projects(self):
        (self.user_root / ".miyabi-mcp" / "policy.json").write_text(json.dumps({"user": {"max_active_jobs": 1}}))
        other = self.user_root / "other"
        other.mkdir()
        (args, plan), = self.plans("a")
        other_args = {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00", "name": "o"}
        other_plan = self.ok_call("job_submit", other_args, project_dir=other)
        self.assertTrue(other_plan["valid"])
        self.assertTrue(self.commit(args, plan)["ok"])
        refused = self.call("job_submit", {**other_args, "dry_run": False, "plan_digest": other_plan["plan_digest"]},
                            project_dir=other)
        self.assertIn("user.max_active_jobs", {c["id"] for c in refused["error"]["checks"] if c["status"] == "fail"})
        self.assertEqual(self.active(), 1)


class RequestKeyTests(FakePbsCase):
    def args(self, **extra) -> dict:
        return {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00", "name": "k", **extra}

    def expire_plans(self) -> None:
        for path in (self.project / ".miyabi-mcp" / "plans").glob("*.json"):
            data = json.loads(path.read_text())
            data["created_at"] = "2000-01-01T00:00:00+09:00"
            path.write_text(json.dumps(data))

    def test_a_retry_after_the_plan_expired_returns_the_job(self):
        args = self.args(request_key="key-1")
        plan = self.ok_call("job_submit", args)
        first = self.ok_call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})
        self.expire_plans()
        again = self.ok_call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["job_id"], first["job_id"])
        self.assertEqual(len(self.read_state()["jobs"]), 1)

    def test_a_dry_run_names_the_job_of_a_used_key(self):
        (self.project / ".miyabi-mcp").mkdir(exist_ok=True)
        (self.project / ".miyabi-mcp" / "policy.json").write_text(json.dumps({"max_active_jobs": 1}))
        job_id = self.submit(**self.args(request_key="key-2"))["job_id"]
        dry = self.ok_call("job_submit", self.args(request_key="key-2"))
        check = next(c for c in dry["checks"] if c["id"] == "request_key")
        self.assertEqual(check["status"], "warn")
        self.assertIn(job_id, check["detail"])
        self.assertTrue(dry["valid"])  # live limits are not judged for a commit that submits nothing
        fresh = self.ok_call("job_submit", self.args(request_key="key-3"))
        self.assertEqual(next(c for c in fresh["checks"] if c["id"] == "request_key")["status"], "pass")
        self.assertFalse(fresh["valid"])  # a new key would submit a second job: max_active_jobs applies

    def test_a_reconciled_submission_joins_its_notification(self):
        state = self.read_state()
        state["qsub_mode"] = "garbage"
        self.write_state(state)
        result = self.submit(**self.args(notify={"name": "grp", "notifier": "box"}))
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["notify"]["notification"]["jobs"], [result["job_id"]])
        self.assertIn("export_command", result)

    def test_a_retry_joins_the_notification_the_first_call_missed(self):
        state = self.read_state()
        state["qsub_mode"] = "garbage"
        self.write_state(state)
        args = self.args(request_key="key-4", notify={"name": "grp", "notifier": "box"})
        plan = self.ok_call("job_submit", args)
        with mock.patch("miyabi_mcp.submit.reconcile", return_value=None):  # the job is not visible yet
            uncertain = self.call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})
        self.assertEqual(uncertain["error"]["type"], "submit_uncertain")
        retry = self.ok_call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})
        self.assertTrue(retry["reconciled"])
        self.assertEqual(retry["notify"]["notification"]["jobs"], [retry["job_id"]])
        again = self.ok_call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})
        self.assertNotIn("notify", again)  # joined once
        self.assertEqual(len(self.ok_call("notify_list")["notifications"]), 1)

    def test_adopting_binds_a_keyless_uncertain_submission(self):
        state = self.read_state()
        state["qsub_mode"] = "garbage"
        self.write_state(state)
        args = self.args()
        plan = self.ok_call("job_submit", args)
        with mock.patch("miyabi_mcp.submit.reconcile", return_value=None):
            uncertain = self.call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})
        self.assertIn("adopt=true", uncertain["error"]["message"])
        adopted = self.ok_call("job_annotate", {"job_id": "3800000", "adopt": True})
        record = adopted["record"]
        self.assertEqual((record["origin"], record["status"]), ("submitted", "submitted"))
        self.assertEqual(record["submit_id"], uncertain["error"]["submit_id"])
        self.assertEqual(len(self.ok_call("ledger_query")["records"]), 1)

    def test_a_retry_completes_the_user_index(self):
        policy = self.user_root / ".miyabi-mcp" / "policy.json"
        policy.write_text(json.dumps({"user": {"budget_windows": [{"window": "24h", "max_jobs": 1}]}}))
        args = self.args(request_key="key-ix")
        plan = self.ok_call("job_submit", args)
        with mock.patch("miyabi_mcp.submit.index_append", side_effect=OSError("disk full")):
            broken = self.call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})
        self.assertFalse(broken["ok"])
        self.assertEqual(len(self.read_state()["jobs"]), 1)  # PBS took the job; the user index missed it
        retry = self.ok_call("job_submit", {**args, "dry_run": False, "plan_digest": plan["plan_digest"]})
        self.assertTrue(retry["idempotent"])
        self.assertNotIn("reconciled", retry)  # the record knew its job
        self.assertEqual(len(self.read_state()["jobs"]), 1)
        second = self.ok_call("job_submit", self.args(request_key="key-ix2", name="k2"))
        self.assertFalse(second["valid"])  # the window counts the first job
        self.assertIn("1 jobs already submitted", json.dumps(second["checks"]))

    def test_a_used_key_whose_job_sealed_its_notification_can_be_retried(self):
        args = self.args(request_key="key-n", notify={"name": "solo", "notifier": "box", "expected": 1})
        first = self.submit(**args)
        dry = self.ok_call("job_submit", args)
        self.assertTrue(dry["valid"], dry["checks"])
        self.assertEqual(next(c for c in dry["checks"] if c["id"] == "notify")["status"], "warn")
        again = self.ok_call("job_submit", {**args, "dry_run": False, "plan_digest": dry["plan_digest"]})
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["job_id"], first["job_id"])
        self.assertEqual(len(self.read_state()["jobs"]), 1)

    def test_expired_plans_are_pruned(self):
        plans = self.project / ".miyabi-mcp" / "plans"
        self.ok_call("job_submit", self.args(name="old"))
        [old] = plans.glob("*.json")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        self.ok_call("job_submit", self.args(name="new"))
        self.assertFalse(old.exists())
        self.assertEqual(len(list(plans.glob("*.json"))), 1)


class CancelFailureTests(FakePbsCase):
    def refuse(self, *job_ids: str) -> None:
        state = self.read_state()
        state["qdel_refuse"] = [job_id.split(".")[0] for job_id in job_ids]
        self.write_state(state)

    def cancel(self, job_ids: list[str]) -> dict:
        plan = self.ok_call("job_cancel", {"job_ids": job_ids})
        return self.call("job_cancel", {"job_ids": job_ids, "dry_run": False, "reason": "test",
                                        "targets_digest": plan["targets_digest"]})

    def record(self, job_id: str) -> dict:
        return next(r for r in self.ok_call("ledger_query")["records"] if r["job_id"] == job_id)

    def test_a_refused_qdel_is_an_error_and_not_a_cancellation(self):
        job_id = self.submit(name="x")["job_id"]
        self.start(job_id)
        self.refuse(job_id)
        result = self.cancel([job_id])
        self.assertEqual(result["error"]["type"], "qdel_failed")
        self.assertEqual(result["error"]["targets"][0]["result"], "qdel_failed")
        self.assertFalse(self.record(job_id)["cancel_requested"])
        self.finish(job_id, exit_status=-29)  # later killed at its walltime: that is a failure, not our cancel
        self.ok_call("job_list")
        state = self.record(job_id)["state"]
        self.assertEqual((state["raw_state"], state["outcome"]), ("F Exit_status=-29", "failed"))
        kinds = [e["kind"] for e in self.ok_call("ledger_query", {"format": "events"})["events"]]
        self.assertEqual(kinds.index("cancel_failed"), kinds.index("cancel_requested") + 1)
        self.assertEqual(kinds[-1], "ended")

    def test_a_partial_failure_names_both_sides(self):
        done, kept = self.submit(name="a")["job_id"], self.submit(name="b")["job_id"]
        self.start(done)
        self.start(kept)
        self.refuse(kept)
        result = self.cancel([done, kept])
        self.assertTrue(result["ok"])
        self.assertEqual((result["cancelled"], result["failed"]), ([done], [kept]))
        self.assertIn(kept, result["warning"])
        self.assertEqual(self.record(done)["state"]["outcome"], "cancelled")
        self.assertFalse(self.record(kept)["cancel_requested"])

    def test_a_whole_qdel_failure_without_job_ids_counts_for_every_target(self):
        job_id = self.submit(name="x")["job_id"]
        qdel = self.bin / "qdel"
        qdel.unlink()
        qdel.write_text("#!/bin/sh\necho 'qdel: cannot connect to server opbs (errno=111)' >&2\nexit 2\n")
        qdel.chmod(0o755)
        result = self.cancel([job_id])
        self.assertEqual(result["error"]["type"], "qdel_failed")
        self.assertFalse(self.record(job_id)["cancel_requested"])


class LedgerEdgeTests(FakePbsCase):
    def test_mig_usage_is_labelled_without_configuration(self):
        job_id = self.submit(queue="debug-mig", name="m")["job_id"]
        self.start(job_id)
        self.finish(job_id)
        self.ok_call("job_list")
        record = self.ok_call("ledger_query")["records"][0]
        self.assertEqual(record["usage"]["hardware"], "GH200 MIG")

    def test_a_reappearing_job_gets_a_new_grace_period(self):
        job_id = self.submit(name="x")["job_id"]
        state = self.read_state()
        hidden = state["jobs"].pop(job_id.split(".")[0])
        self.write_state(state)
        self.ok_call("job_list")  # missing_s=0: unknown at once
        record = self.ok_call("ledger_query", {"refresh": False})["records"][0]
        self.assertEqual(record["state"]["execution_state"], "unknown")
        self.assertIn("missing_since", record)
        state = self.read_state()
        state["jobs"][job_id.split(".")[0]] = hidden
        self.write_state(state)
        self.ok_call("job_list")
        record = self.ok_call("ledger_query", {"refresh": False})["records"][0]
        self.assertEqual(record["state"]["execution_state"], "queued")
        self.assertNotIn("missing_since", record)

    def test_events_never_go_back_to_an_older_month_file(self):
        events = self.project / ".miyabi-mcp" / "events"
        events.mkdir(parents=True)
        (events / "2999-01.jsonl").write_text("")  # a newer month already exists (another node's clock)
        self.submit(name="x")
        self.assertEqual([p.name for p in events.glob("*.jsonl")], ["2999-01.jsonl"])
        first = self.ok_call("ledger_query", {"format": "events"})
        self.assertTrue(first["next_cursor"].startswith("2999-01:"))

    def event_ids(self) -> list[str]:
        return [e["event_id"] for e in self.ok_call("ledger_query", {"format": "events"})["events"]]

    def test_a_failed_record_write_never_repeats_an_event_id(self):
        job_id = self.submit(name="x")["job_id"]
        self.start(job_id)
        real, failed = Store.write, []

        def full_quota(store, record):  # the record write fails once; a small append would still go through
            if (record.get("state") or {}).get("execution_state") == "running" and not failed:
                failed.append(1)
                raise OSError(122, "Disk quota exceeded")
            return real(store, record)

        with mock.patch.object(Store, "write", full_quota):
            self.assertFalse(self.call("job_list")["ok"])
        self.ok_call("job_list")
        self.finish(job_id)
        self.ok_call("job_list")
        ids = self.event_ids()
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertEqual(ids[-1], f"miyabi-pbs:{job_id}:3")

    def test_a_failed_append_puts_the_record_back(self):
        job_id = self.submit(name="x")["job_id"]
        self.start(job_id)
        with mock.patch.object(Store, "_append_event", side_effect=OSError(5, "Input/output error")):
            self.assertFalse(self.call("job_list")["ok"])
        record = self.ok_call("ledger_query", {"refresh": False})["records"][0]
        self.assertEqual((record["seq"], record["state"]["execution_state"]), (1, "queued"))
        self.ok_call("job_list")
        self.assertEqual(self.event_ids(), [f"miyabi-pbs:{job_id}:1", f"miyabi-pbs:{job_id}:2"])

    def test_a_retry_never_rebinds_over_newer_events(self):
        args = {"script": str(self.script), "queue": "debug-g", "walltime": "00:10:00", "name": "x",
                "request_key": "key-r"}
        job_id = self.submit(**args)["job_id"]
        self.start(job_id)
        self.ok_call("job_list")  # running: seq 2
        store = Store(self.settings())
        request = json.loads(store.request_path("key-r").read_text())
        request.pop("job_id")
        request["status"] = "uncertain"  # as if the call stopped between writing the record and the key
        store.request_path("key-r").write_text(json.dumps(request))
        again = self.call("job_submit", {**args, "dry_run": False})
        self.assertTrue(again["idempotent"], again)
        self.assertEqual(again["events"], [])
        self.finish(job_id)
        self.ok_call("job_list")
        ids = self.event_ids()
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertEqual(json.loads(store.request_path("key-r").read_text())["job_id"], job_id)

    def test_an_invalid_verdict_adopts_nothing(self):
        state = self.read_state()
        state["jobs"]["3700000"] = {"name": "ext", "state": "Q", "queue": "debug-g", "route": "debug-g",
                                    "project": "gtest", "select": "1:mpiprocs=1", "units": 1, "mig": False,
                                    "walltime": "00:10:00", "qtime": 1793300000, "stime": None, "obittime": None,
                                    "exit_status": None, "used": None, "output": "/work/x.log", "variables": {},
                                    "submit_args": "-q debug-g ext.pbs"}
        self.write_state(state)
        result = self.call("job_annotate", {"job_id": "3700000", "adopt": True, "verdict": {"status": "great"}})
        self.assertEqual(result["error"]["type"], "invalid_arguments")
        self.assertEqual(self.ok_call("ledger_query")["records"], [])


class ObservationOrderTests(FakePbsCase):
    """Two refreshes at once (an agent's call and notifyd): the one that queried PBS first may record last."""

    def later_refresh_during_the_query(self, then):
        """Pbs.detail whose first call takes PBS's answer, then lets a later refresh run to the end after `then`
        moved the job on, and only then hands the earlier answer back."""
        real, calls = Pbs.detail, []

        def detail(pbs, ids, history=False):
            answer = real(pbs, ids, history=history)
            if not calls:
                calls.append(list(ids))
                for job_id in ids:
                    then(job_id)
                self.ok_call("job_list")  # queries after this one started, and records first
            return answer

        return mock.patch.object(Pbs, "detail", detail)

    def kinds(self) -> list[str]:
        return [event["kind"] for event in self.ok_call("ledger_query", {"format": "events"})["events"]]

    def test_an_earlier_query_recording_last_does_not_take_the_state_back(self):
        job_id = self.submit(name="x")["job_id"]
        self.set_job(job_id, state="H")
        with self.later_refresh_during_the_query(self.start):
            self.ok_call("job_list")  # sees HELD and asks qstat -f (H); the later refresh records running first
        record = self.ok_call("ledger_query", {"refresh": False})["records"][0]
        self.assertEqual(record["state"]["execution_state"], "running")
        self.assertEqual(self.kinds(), ["submitted", "running"])
        self.finish(job_id)
        self.ok_call("job_list")  # later queries still go through
        self.assertEqual(self.kinds(), ["submitted", "running", "ended"])

    def test_submitted_does_not_follow_a_later_observation(self):
        with self.later_refresh_during_the_query(self.start):
            job_id = self.submit(name="x")["job_id"]  # first_events asks qstat -f (Q); meanwhile running is recorded
        record = self.ok_call("ledger_query", {"refresh": False})["records"][0]
        self.assertEqual((record["job_id"], record["state"]["execution_state"]), (job_id, "running"))
        self.assertEqual(self.kinds(), ["running"])


class SubmitGraceTests(FakePbsCase):
    """The first submit_grace_s after qsub: a job not in the table may be over already, or not listed yet."""

    user_config = {**FakePbsCase.user_config, "submit_grace_s": 120}

    def state(self) -> dict:
        record = self.ok_call("ledger_query", {"refresh": False})["records"][0]
        return {"execution_state": record["state"]["execution_state"], "missing": "missing_since" in record}

    def test_job_wait_sees_a_short_job_end_at_once(self):
        job_id = self.submit(name="short")["job_id"]
        self.start(job_id)
        self.finish(job_id, exit_status=1)
        result = self.ok_call("job_wait", {"job_ids": [job_id], "timeout_s": 5})  # until 11-06: queued, met false
        self.assertTrue(result["met"], result)
        self.assertEqual((result["jobs"][0]["execution_state"], result["jobs"][0]["outcome"]), ("ended", "failed"))
        self.assertLess(result["waited_s"], 5)

    def test_a_job_found_nowhere_is_not_counted_missing_yet(self):
        job_id = self.submit(name="x")["job_id"]
        state = self.read_state()
        state["jobs"].pop(job_id.split(".")[0])
        self.write_state(state)
        self.ok_call("job_list")  # missing_s=0: past the grace this would be unknown at once
        self.assertEqual(self.state(), {"execution_state": "queued", "missing": False})

    def test_only_a_finished_history_record_ends_a_job(self):
        job_id = self.submit(name="x")["job_id"]
        state = self.read_state()
        job = state["jobs"].pop(job_id.split(".")[0])
        self.write_state(state)
        real = Pbs.detail

        def detail(pbs, ids, history=False):  # a history record that does not say the job is over
            if history:
                return {job_id: {"job_state": "R", "stime": "Fri Nov  6 14:30:21 2026", "queue": job["queue"]}}
            return real(pbs, ids, history=history)

        with mock.patch.object(Pbs, "detail", detail):
            self.ok_call("job_list")
        self.assertEqual(self.state(), {"execution_state": "queued", "missing": False})


class LockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "lock"

    def test_a_held_lock_turns_others_away_and_names_its_holder(self):
        held = Lock(self.path, timeout=0).acquire()
        with self.assertRaises(MiyabiError) as caught:
            Lock(self.path, timeout=0.3).acquire()  # the same process through another open file: excluded too
        self.assertEqual((caught.exception.kind, caught.exception.details["holder"]), ("lock_timeout", held.token))
        held.release()
        self.assertFalse(self.path.exists())
        Lock(self.path, timeout=0).acquire().release()

    def test_threads_take_turns(self):
        inside, overlaps = [], []

        def work() -> None:
            for _ in range(20):
                with Lock(self.path, timeout=30):
                    overlaps.extend(inside)
                    inside.append(threading.get_ident())
                    time.sleep(0.002)
                    inside.pop()

        threads = [threading.Thread(target=work) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(overlaps, [])

    def test_a_dead_holder_frees_the_lock_at_once(self):
        code = ("import pathlib, sys, time; sys.path.insert(0, sys.argv[1]); from miyabi_mcp.util import Lock; "
                "Lock(pathlib.Path(sys.argv[2]), timeout=0).acquire(); print('held', flush=True); time.sleep(60)")
        child = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "src"), str(self.path)],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "held")
        with self.assertRaises(MiyabiError):
            Lock(self.path, timeout=0).acquire()
        child.kill()
        child.wait()
        child.stdout.close()
        Lock(self.path, timeout=1).acquire().release()  # no lease to wait out

    def test_a_waiter_does_not_keep_a_deleted_lock_file(self):
        holder = Lock(self.path, timeout=0).acquire()
        real, others = fcntl.flock, []

        def flock(fd: int, operation: int) -> None:
            if operation & fcntl.LOCK_EX and not others:
                # The waiter has opened the lock file. Before it locks it, the holder releases (deleting the
                # file) and another caller takes the lock on a new file.
                others.append(None)
                holder.release()
                others[0] = Lock(self.path, timeout=0).acquire()
            real(fd, operation)

        with mock.patch.object(fcntl, "flock", flock), self.assertRaises(MiyabiError):
            Lock(self.path, timeout=0.5).acquire()  # it locked the deleted file, saw that, and found the lock held
        self.assertEqual(self.path.read_text(), others[0].token)
        others[0].release()


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
