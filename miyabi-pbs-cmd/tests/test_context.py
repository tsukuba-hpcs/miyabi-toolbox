"""Offline regressions for architecture routing and stale allocation evidence."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import context


class ContextTests(unittest.TestCase):
    """Exercise admission decisions without accessing a scheduler or project."""

    def inspect(self, hostname="mg0021", architecture="aarch64", target="Miyabi-G",
                nodes="mg0021\nmg0022\nmg0021\n", job_id="1234.opbs"):
        """Supply nodefile evidence independently of the machine running tests."""
        with tempfile.TemporaryDirectory() as directory:
            nodefile = Path(directory) / "nodes"
            nodefile.write_text(nodes)
            return context.inspect_context(hostname, architecture,
                                           {"PBS_JOBID": job_id, "PBS_NODEFILE": str(nodefile)}, target)

    def test_target_architecture_and_node_membership(self):
        """Known allocated G and C hosts accept only their matching target."""
        result = self.inspect()
        self.assertTrue(result["runtime_execution_allowed"])
        self.assertEqual(result["allocated_hosts"], ["mg0021", "mg0022"])
        result = self.inspect("mc001.site", "x86_64", "Miyabi-C", "mc001\n")
        self.assertTrue(result["runtime_execution_allowed"])
        self.assertEqual(result["target_architecture"], "x86_64")
        self.assertFalse(self.inspect(target="Miyabi-C")["runtime_execution_allowed"])
        self.assertFalse(self.inspect(architecture="x86_64")["runtime_execution_allowed"])

    def test_login_with_inherited_pbs_variables_stays_control_plane(self):
        """Copied PBS variables must not turn a login host into a compute host."""
        for hostname, architecture in (("miyabi-g1", "aarch64"), ("miyabi-c2", "x86_64"),
                                       ("interact-g1", "aarch64")):
            with self.subTest(hostname=hostname):
                result = self.inspect(hostname, architecture, nodes=hostname)
                self.assertEqual(result["node_role"], "login")
                self.assertFalse(result["allocation_confirmed"])
                self.assertFalse(result["runtime_execution_allowed"])

    def test_unknown_host_or_incomplete_evidence_refuses_execution(self):
        """A hostname or a single PBS variable is insufficient allocation evidence."""
        for kwargs in ({"hostname": "workstation"}, {"nodes": ""}, {"nodes": "mg0099\n"},
                       {"job_id": ""}, {"job_id": "stale"}, {"target": None}):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(self.inspect(**kwargs)["runtime_execution_allowed"])
        for env in ({}, {"PBS_JOBID": "1234.opbs"}, {"PBS_NODEFILE": "/missing/nodefile"}):
            result = context.inspect_context("mg0021", "aarch64", env, "Miyabi-G")
            self.assertFalse(result["runtime_execution_allowed"])

    def test_guard_has_json_failure_and_nonzero_exit(self):
        """A refused check must stop a shell using the helper as a guard."""
        host = type("Host", (), {"nodename": "miyabi-c2", "machine": "x86_64"})()
        stream = io.StringIO()
        with patch.object(context.os, "uname", return_value=host), patch.dict(context.os.environ, {}, clear=True):
            with contextlib.redirect_stdout(stream):
                code = context.main(["--target-system", "Miyabi-G", "--require-compute"])
        result = json.loads(stream.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["architecture"], "x86_64")
        self.assertEqual(result["error"]["type"], "execution_context")


if __name__ == "__main__":
    unittest.main()
