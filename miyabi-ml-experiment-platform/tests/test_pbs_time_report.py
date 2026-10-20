"""Offline tests of scripts/pbs_time_report.py on saved `qstat -H -f` output.

    /usr/bin/python3 -I -m unittest discover -s tests

The fixture holds three real Miyabi-G records (Variable_List removed): an
11-node regular-g pilot routed to small-g (3615444), a one-node debug-g pilot
(3584931) and an 11-node job deleted while still queued (3549989).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "qstat_H_f.txt"
SPEC = importlib.util.spec_from_file_location("pbs_time_report", HERE.parent / "scripts" / "pbs_time_report.py")
report_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report_module)


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = report_module.main(list(argv))
    return code, out.getvalue()


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.records = report_module.parse_records(FIXTURE.read_text())

    def test_wrapped_values_are_joined(self):
        fields = self.records["3615444"]
        self.assertEqual(len(report_module.per_node(fields["resources_used.gpu_avg_util_per_nodes"])), 11)
        self.assertTrue(fields["Error_Path"].endswith("/h0004d/control/pbs/fedcl-n11.e3615444"))

    def test_started_job(self):
        row = report_module.job_row("3615444", self.records["3615444"])
        self.assertEqual((row["queue"], row["ran_in"], row["nodes"]), ("regular-g", "small-g", 11))
        self.assertEqual(row["wait_s"], 49)            # 10:22:24 -> 10:23:13
        self.assertEqual(row["run_s"], 5059)           # 01:24:19
        self.assertEqual(row["request_s"], 7800)       # 02:10:00
        self.assertEqual(row["used_ratio"], 0.649)
        self.assertEqual(row["gpu_util_mean"], 20.5)   # 226 / 11
        self.assertEqual(row["gpu_util_min"], 12.0)
        self.assertEqual(row["gpu_mem_max_gib"], 74.0)
        self.assertEqual((row["exit_status"], row["token"]), (0, 15.5))

    def test_never_started_job(self):
        row = report_module.job_row("3549989", self.records["3549989"])
        self.assertIsNone(row["started_at"])
        self.assertIsNone(row["run_s"])
        self.assertIsNone(row["token"])                # TOKEN = -
        self.assertEqual(row["abandoned_wait_s"], 18754)  # 11:48:48 -> 17:01:22


class ReportTest(unittest.TestCase):
    def test_groups_and_overall(self):
        code, out = run("--from-file", str(FIXTURE))
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertTrue(document["ok"])
        self.assertNotIn("rows", document)
        groups = {(group["queue"], group["nodes"]): group for group in document["groups"]}
        self.assertEqual(set(groups), {("regular-g", 11), ("debug-g", 1)})
        eleven = groups[("regular-g", 11)]
        self.assertEqual((eleven["jobs"], eleven["never_started"]), (2, 1))
        self.assertEqual(eleven["node_h_used"], 15.5)       # 11 x 5059 s
        self.assertEqual(eleven["node_h_requested"], 23.8)  # only jobs that started count
        self.assertEqual(eleven["abandoned_wait_h"], 5.2)
        overall = document["overall"]
        self.assertEqual((overall["jobs"], overall["never_started"], overall["nonzero_exit"]), (3, 1, 0))
        # Node-time weighted, from the rounded per-job means: (20.5 x 11 x 5059 + 29 x 796) / (11 x 5059 + 796)
        self.assertEqual(overall["gpu_util_pct"], 20.6)

    def test_name_filter_and_rows(self):
        _, out = run("--from-file", str(FIXTURE), "--name", "fedcl-n", "--jobs")
        document = json.loads(out)
        self.assertEqual([row["job_id"] for row in document["rows"]], ["3584931", "3615444"])

    def test_text_table(self):
        code, out = run("--from-file", str(FIXTURE), "--text")
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[0].startswith("queue"))
        self.assertIn("ALL", out)

    def test_percentile(self):
        self.assertEqual(report_module.percentile([4, 1, 3, 2], 0.5), 2.5)
        self.assertIsNone(report_module.percentile([], 0.9))


if __name__ == "__main__":
    unittest.main()
