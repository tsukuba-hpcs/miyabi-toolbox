"""Parsers against anonymized live output captured on miyabi-g1 (2026-10-30)."""

import unittest

from base import FIXTURES  # noqa: F401  (puts src/ on sys.path)
from miyabi_mcp import parse, states
from miyabi_mcp.util import MiyabiError


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class TableTests(unittest.TestCase):
    def test_history_rows_and_banner(self):
        text = fixture("history.txt")
        jobs = parse.parse_table(text)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[0]["status"], "FINISH")
        self.assertEqual(jobs[0]["project"], "gtest")
        self.assertIsNotNone(jobs[0]["elapsed_seconds"])
        self.assertEqual(parse.scheduled_stop(text)[:19], "2026-11-25T09:00:00")

    def test_empty_message_is_an_empty_list(self):
        self.assertEqual(parse.parse_table(fixture("active_empty.txt")), [])

    def test_unknown_output_fails(self):
        with self.assertRaises(MiyabiError) as caught:
            parse.parse_table("something else entirely\n")
        self.assertEqual(caught.exception.kind, "parse_error")


class DetailTests(unittest.TestCase):
    def test_continuations_are_joined(self):
        records = parse.parse_detail(fixture("detail_g_finished.txt"))
        fields = records["3731052.opbs"]
        self.assertEqual(fields["Exit_status"], "0")
        self.assertEqual(fields["resources_used.walltime"], "00:10:17")
        self.assertTrue(fields["Output_Path"].endswith(".log.pbs"))
        self.assertIn("SPEC", parse.variable_list(fields["Variable_List"]))
        self.assertIn("-W group_list=gtest", fields["Submit_arguments"])

    def test_finished_g_job_maps_to_success_with_rounded_node_hours(self):
        fields = parse.parse_detail(fixture("detail_g_finished.txt"))["3731052.opbs"]
        observed = states.from_detail(fields, history=True, cancel_requested=False)
        self.assertEqual((observed["execution_state"], observed["outcome"], observed["raw_state"]),
                         ("ended", "success", "F Exit_status=0"))
        self.assertEqual(observed["usage"], {
            "quantity": 0.171389, "unit": "node_hours", "resource_type": "gpu_node", "hardware": "GH200",
            "basis": "measured", "source_ref": "resources_used.walltime=00:10:17 x nvnodes-g=1"})
        self.assertTrue(observed["pbs_time"].startswith("2026-10-30T05:52:49"))
        self.assertEqual(observed["token"], states.token(fields))

    def test_finished_mig_job_counts_instance_hours(self):
        fields = parse.parse_detail(fixture("detail_mig_finished.txt"))["3499506.opbs"]
        usage = states.usage(fields, mig_hardware="GH200 MIG test")
        self.assertEqual(usage["unit"], "mig_instance_hours")
        self.assertEqual(usage["resource_type"], "gpu_mig_instance")
        self.assertEqual(usage["hardware"], "GH200 MIG test")
        self.assertEqual(usage["source_ref"], "resources_used.walltime=00:04:39 x nvnodes-mig=1")
        self.assertEqual(usage["quantity"], round(279 / 3600, 6))


class SiteTests(unittest.TestCase):
    def test_queues_routing_mig_and_remaining_time(self):
        queues = parse.parse_rsc(fixture("rsc_x.txt"))
        by_name = {q["name"]: q for q in queues}
        self.assertEqual(by_name["regular-g"]["routes"], ["small-g", "medium-g", "large-g", "x-large-g"])
        self.assertEqual(by_name["small-g"]["parent"], "regular-g")
        self.assertEqual(by_name["debug-g"]["max_elapse_s"], 1800)
        self.assertTrue(by_name["debug-g"]["started"])
        self.assertEqual(by_name["debug-mig"]["mig_sizes"], [1, 2, 4])
        self.assertTrue(by_name["debug-mig"]["mig"])
        self.assertEqual(by_name["debug-mig"]["mem_gib"], 25.0)
        self.assertEqual(by_name["prepost"]["system"], "Prepost")
        self.assertEqual(by_name["debug-g"]["projects"], ["gtest"])

    def test_limits(self):
        rows = parse.parse_limit(fixture("limit.txt"))
        g = next(r for r in rows if r["system"] == "Miyabi-G")
        self.assertEqual(g["accept"]["limit"], 128)
        self.assertEqual(g["run"]["limit"], 16)
        self.assertEqual(g["mig"]["limit"], 24)
        c = next(r for r in rows if r["system"] == "Miyabi-C")
        self.assertNotIn("mig", c)

    def test_occupancy_and_tokens(self):
        rows = parse.parse_rscuse(fixture("rscuse.txt"))
        self.assertTrue(any(r["unit"] == "mig" for r in rows))
        self.assertTrue(all(0 <= r["used"] <= r["total"] for r in rows))
        tokens = parse.parse_show_token(fixture("show_token.txt"))
        group = next(r for r in tokens if r["user"] is None)
        self.assertEqual(group["project"], "gtest")
        self.assertGreater(group["limit_hours"], 0)
        self.assertTrue(any(r["user"] == "utest" for r in tokens))


class StateTests(unittest.TestCase):
    base = {"stime": "Fri Oct 30 05:42:26 2026", "obittime": "Fri Oct 30 05:52:49 2026",
            "resources_used.walltime": "00:10:17", "exec_vnode": "(mg0001:ncpus=72:nvnodes-g=1)"}

    def outcome(self, history=True, cancel=False, **fields):
        return states.from_detail({**self.base, "job_state": "F", **fields}, history=history,
                                  cancel_requested=cancel)

    def test_exit_codes(self):
        self.assertEqual(self.outcome(Exit_status="1")["outcome"], "failed")
        self.assertEqual(self.outcome(Exit_status="271")["outcome"], "failed")
        self.assertEqual(self.outcome(Exit_status="271", cancel=True)["outcome"], "cancelled")
        self.assertEqual(self.outcome(Exit_status="-29")["raw_state"], "F Exit_status=-29")
        self.assertEqual(self.outcome(Exit_status="-29")["outcome"], "failed")

    def test_never_started(self):
        observed = states.from_detail({"job_state": "F", "obittime": self.base["obittime"]}, history=True,
                                      cancel_requested=False)
        self.assertEqual((observed["outcome"], observed["raw_state"], observed["usage"]),
                         ("cancelled", "FINISH no-start", None))

    def test_active_states(self):
        self.assertEqual(states.from_detail({"job_state": "H"}, history=False, cancel_requested=False)["kind"], "held")
        running = states.from_detail({"job_state": "R", **self.base}, history=False, cancel_requested=False)
        self.assertEqual((running["execution_state"], running["raw_state"]), ("running", "R"))
        self.assertTrue(running["pbs_time"].startswith("2026-10-30T05:42:26"))


if __name__ == "__main__":
    unittest.main()
