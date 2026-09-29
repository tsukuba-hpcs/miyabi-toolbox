"""Offline regressions for the Miyabi table and CLI; no scheduler access."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import qstat_json as qstat


HISTORY = (ROOT / "tests/fixtures/qstat_history.txt").read_text()
HEADER = HISTORY.splitlines()[2]
FIRST_ROW = HISTORY.splitlines()[3]


class ParserTests(unittest.TestCase):
    def test_anonymized_live_history(self):
        jobs, notices = qstat.parse_qstat(HISTORY)
        self.assertEqual(jobs, [
            {"job_id": "1234001", "job_name": "STDIN", "status": "FINISH", "project": "example1",
             "queue": "interact-g_n1", "start_date": "09/04 04:26:27", "elapsed_seconds": 115,
             "token": None, "token_estimate": None, "nodes": 1, "mig": None},
            {"job_id": "1234002", "job_name": "example_full_protocol", "status": "FINISH", "project": "example1",
             "queue": "debug-g", "start_date": "09/04 04:30:53", "elapsed_seconds": 644,
             "token": 1.6, "token_estimate": None, "nodes": 9, "mig": None},
        ])
        self.assertEqual(notices, [HISTORY.splitlines()[0]])

    def test_narrow_table(self):
        # Observed default (10-character names), distinct from the -ll fixture.
        output = (
            "JOB_ID            JOB_NAME   STATUS    PROJECT    QUEUE           START_DATE       ELAPSE        TOKEN NODE MIG\n"
            "1234002           example_fu FINISH    example1   debug-g         09/04 04:30:53   00:10:44        1.6    9   -\n"
        )
        jobs, _ = qstat.parse_qstat(output)
        self.assertEqual(jobs[0]["job_name"], "example_fu")
        self.assertEqual(jobs[0]["elapsed_seconds"], 644)

    def test_array_estimate_missing_usage_and_name_with_spaces(self):
        row = (
            "1234003[7]        array job with spaces                                            QUEUED    example1   regular-g       (09/05 12:30:00) --:--:--          -    2   -"
        )
        jobs, _ = qstat.parse_qstat(HEADER + "\n" + row)
        self.assertEqual(jobs[0]["job_id"], "1234003[7]")
        self.assertEqual(jobs[0]["job_name"], "array job with spaces")
        self.assertEqual(jobs[0]["start_date"], "(09/05 12:30:00)")
        self.assertIsNone(jobs[0]["elapsed_seconds"])
        self.assertIsNone(jobs[0]["token"])

    def test_long_duration_grouped_token_and_overflowing_status(self):
        row = (
            "1234004           long_job                                                         TRANSITING example1  regular-g       09/01 00:00:00   120:01:02    1,234.5   16   0"
        )
        jobs, _ = qstat.parse_qstat(HEADER + "\n" + row)
        self.assertEqual(jobs[0]["status"], "TRANSITING")
        self.assertEqual(jobs[0]["elapsed_seconds"], 432062)
        self.assertEqual(jobs[0]["token"], 1234.5)
        self.assertEqual(jobs[0]["nodes"], 16)
        self.assertEqual(jobs[0]["mig"], 0)

    def test_missing_start_and_mig_job(self):
        row = (
            "1234005           mig_job                                                          HELD      example1   regular-mig     --/-- --:--:--  --:--:--          -    -   1"
        )
        jobs, _ = qstat.parse_qstat(HEADER + "\n" + row)
        self.assertIsNone(jobs[0]["start_date"])
        self.assertIsNone(jobs[0]["nodes"])
        self.assertEqual(jobs[0]["mig"], 1)

    def test_observed_queued_estimate_and_never_started_finish(self):
        # Anonymized shapes observed on miyabi-g1 on 2026-09-29.
        queued = (
            "1234006           queued_job                                                       QUEUED    example1   short-g         (09/30 05:58:36) --:--:--      (2.0)    1   -"
        )
        deleted = (
            "1234007           deleted_before_start                                             FINISH    example1   small-g         --:--:--         --:--:--          -    0   -"
        )
        jobs, _ = qstat.parse_qstat(HEADER + "\n" + queued + "\n" + deleted)
        self.assertIsNone(jobs[0]["token"])
        self.assertEqual(jobs[0]["token_estimate"], 2.0)
        self.assertEqual(jobs[0]["start_date"], "(09/30 05:58:36)")
        self.assertEqual(jobs[1]["status"], "FINISH")
        self.assertIsNone(jobs[1]["start_date"])
        self.assertIsNone(jobs[1]["elapsed_seconds"])
        self.assertEqual(jobs[1]["nodes"], 0)

    def test_explicit_empty_results(self):
        for message in ("No unfinished job found. ", "No matching job found. "):
            with self.subTest(message=message):
                jobs, notices = qstat.parse_qstat(HISTORY.splitlines()[0] + "\n\n" + message)
                self.assertEqual(jobs, [])
                self.assertEqual(len(notices), 1)

    def test_unrecognized_or_partial_output_is_not_success(self):
        for output in (
            "", HISTORY.splitlines()[0], "qstat: server unavailable",
            "JOB_ID JOB_NAME STATUS PROJECT QUEUE NEW_COLUMN",
            HISTORY + "unrecognized footer\n",
            HISTORY.replace("00:10:44", "00:99:44"),
            HISTORY.replace("1.6", "NaN"),
            HISTORY + "No matching job found.\n",
            "No matching job found.\n" + HISTORY,
        ):
            with self.subTest(output=output[-80:]):
                with self.assertRaises(qstat.ParseError):
                    qstat.parse_qstat(output)


class QueryTests(unittest.TestCase):
    def query(self, args, stdout=HISTORY, stderr="", returncode=0):
        process = subprocess.CompletedProcess([], returncode, stdout, stderr)
        with patch.object(qstat.subprocess, "run", return_value=process):
            return qstat.query(qstat.parse_args(args))

    def test_filter_projection_and_summary(self):
        result = self.query(["-H", "--hnum", "2", "--status", "finish", "--fields", "job_id,status"])
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["jobs"][0], {"job_id": "1234001", "status": "FINISH"})
        result = self.query(["-H", "--status", "running", "--summary"])
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["status_counts"], {})
        self.assertNotIn("jobs", result)
        result = self.query(["-H", "--status", "running", "--status", "finish", "--summary"])
        self.assertEqual(result["status_counts"], {"FINISH": 2})

    def test_nonzero_exit_never_returns_partial_jobs(self):
        result = self.query(["-H"], stderr="qstat: cannot query one server", returncode=7)
        self.assertFalse(result["ok"])
        self.assertNotIn("jobs", result)
        self.assertEqual(result["error"]["returncode"], 7)
        self.assertIn("cannot query", result["error"]["stderr"])

    def test_success_keeps_stderr_and_parse_errors_keep_diagnostics(self):
        result = self.query(["-H"], stderr="a site warning")
        self.assertTrue(result["ok"])
        self.assertEqual(result["stderr"], "a site warning")
        result = self.query(["-H"], stdout=HISTORY + "broken row\n")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "parse_error")
        self.assertIn("broken row", result["error"]["stdout"])

    def test_expected_execution_failures_are_structured(self):
        for error, kind in (
            (FileNotFoundError(), "command_not_found"),
            (PermissionError("denied"), "command_error"),
            (subprocess.TimeoutExpired(["qstat"], 30, output=b"partial", stderr=b"warning"), "timeout"),
        ):
            with self.subTest(kind=kind):
                with patch.object(qstat.subprocess, "run", side_effect=error):
                    result = qstat.query(qstat.parse_args([]))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["type"], kind)

    def test_invalid_arguments_are_json_and_do_not_execute(self):
        for args in (
            ["--hnum", "1"], ["-H", "--hday", "32"], ["-H", "--hnum", "0"],
            ["--timeout", "nan"], ["--timeout", "inf"], ["--timeout", "0"],
            ["--fields", "job_id,unknown"], ["--fields", "job_id,job_id"],
            ["--summary", "--fields", "status"], ["--status", ""],
            ["--", "-f"], ["123;touch bad"], ["--rsc"],
        ):
            with self.subTest(args=args):
                stream = io.StringIO()
                with patch.object(qstat.subprocess, "run") as run, contextlib.redirect_stdout(stream):
                    code = qstat.main(args)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(stream.getvalue())["error"]["type"], "invalid_arguments")
                run.assert_not_called()


class CLITests(unittest.TestCase):
    def test_cli_invokes_only_requested_read_only_query(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "qstat"
            fake.write_text(
                "#!" + sys.executable + "\nimport os, sys\n"
                "assert sys.argv[1:] == ['-ll', '-H', '--hday', '7', '--hnum', '2', '-t', '1234001[]']\n"
                "assert os.environ['LC_ALL'] == 'C'\n"
                "sys.stdout.write(" + repr(HISTORY) + ")\n"
            )
            fake.chmod(0o755)
            env = dict(os.environ, PATH=directory)
            process = subprocess.run(
                [sys.executable, str(ROOT / "scripts/qstat_json.py"), "-H", "--hday", "7", "--hnum", "2",
                 "-t", "1234001[]", "--summary"],
                env=env, capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(process.stderr, "")
        result = json.loads(process.stdout)
        self.assertEqual(result["status_counts"], {"FINISH": 2})
        self.assertEqual(result["command"][-1], "1234001[]")

    def test_missing_qstat_has_nonzero_exit_and_json(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run(
                [sys.executable, str(ROOT / "scripts/qstat_json.py")],
                env=dict(os.environ, PATH=directory), capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stderr, "")
        self.assertEqual(json.loads(process.stdout)["error"]["type"], "command_not_found")


if __name__ == "__main__":
    unittest.main()
