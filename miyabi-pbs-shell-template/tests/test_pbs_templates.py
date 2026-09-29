"""Simulate shell/launcher boundaries without PBS, MPI, project imports or GPUs."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = tuple(sorted((ROOT / "assets/pbs").glob("*.pbs")))
ACTOR = '''import json, os, sys
record = {"argv0": sys.argv[0], "args": sys.argv[1:], "cwd": os.getcwd(),
          "python_bin": os.environ.get("PYTHON_BIN"), "cc": os.environ.get("CC"),
          "cxx": os.environ.get("CXX"), "rank": os.environ.get("OMPI_COMM_WORLD_RANK")}
with open(os.environ["TEST_OBSERVATIONS"], "a") as stream:
    stream.write(json.dumps(record) + "\\n")
sys.exit(0 if sys.argv[1:2] == ["-c"] else int(os.environ["TEST_ACTOR_EXIT"]))
'''
MPI = '''import os, subprocess, sys
args = sys.argv[1:]
count = int(args[args.index("-np") + 1])
child = args[args.index("/usr/bin/env") + 1:]
env = dict(os.environ)
while child and "=" in child[0]:
    name, value = child.pop(0).split("=", 1)
    env[name] = value
assert child[:2] == ["bash", "-lc"], child
for rank in range(count):
    env.update(OMPI_COMM_WORLD_RANK=str(rank), OMPI_COMM_WORLD_SIZE=str(count),
               OMPI_COMM_WORLD_LOCAL_RANK="0", OMPI_COMM_WORLD_LOCAL_SIZE="1")
    # Emulate site initialization replacing compiler settings before rank code.
    command = "export CC=site-nvc CXX=site-nvcxx;\\n" + child[2]
    result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", command] + child[3:], env=env)
    if result.returncode:
        sys.exit(result.returncode)
'''


class TemplateTests(unittest.TestCase):
    """Check executable boundaries and failure propagation with fake workers."""

    def run_template(self, template, *, actor_exit=0, real_guard=False):
        """Render into temporary storage and substitute only external runtime tools."""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            project = workspace / "project tree"
            bindir = workspace / "bin"
            skill = workspace / "skill"
            for path in (project / ".venv/bin", bindir, skill / "scripts"):
                path.mkdir(parents=True)
            for name, source in (("actor-python", ACTOR), ("mpirun", MPI)):
                path = bindir / name
                path.write_text("#!" + sys.executable + "\n" + source)
                path.chmod(0o755)
            native_uname = bindir / "uname"
            native_uname.write_text("#!/bin/sh\nprintf 'aarch64\\n'\n")
            native_uname.chmod(0o755)
            interpreter = project / ".venv/bin/python"
            interpreter.symlink_to(bindir / "actor-python")
            if real_guard:
                # Deterministic login evidence, using the real guard implementation.
                (skill / "scripts/context.py").write_text(
                    "import os, runpy\n"
                    "os.uname = lambda: type('Host', (), {'nodename': 'miyabi-c2', 'machine': 'x86_64'})()\n"
                    "runpy.run_path(" + repr(str(ROOT.parent / "miyabi-pbs-cmd/scripts/context.py")) + ", run_name='__main__')\n"
                )
            else:
                (skill / "scripts/context.py").write_text(
                    "import sys\nassert sys.argv[1:] == ['--target-system', 'Miyabi-G', '--require-compute']\n"
                )
            values = {
                "eligible_g_batch_queue": "debug-g", "eligible_batch_queue": "debug-g",
                "literal_project_group": "example", "num_nodes": "2", "processes_per_node": "1",
                "HH:MM:SS": "00:10:00", "absolute_miyabi-pbs-cmd_directory": str(skill),
                "Miyabi-G_or_Miyabi-C": "Miyabi-G", "absolute_project_or_snapshot_root": str(project),
                "local_workers_per_node": "1", "target_module/version": "example/1",
                "compiler-or-runtime-module/version": "example/1", "mpi-module/version": "example/1",
                "entrypoint.py": "worker entrypoint.py", "argument": "argument with spaces",
                "python_module": "example.worker",
            }
            content = re.sub(r"<([A-Za-z_][A-Za-z0-9_./:-]*)>", lambda match: values[match[1]], template.read_text())
            script = workspace / "job.pbs"
            script.write_text(content)
            nodefile = workspace / "nodes"
            nodefile.write_text("mg0021\nmg0022\n" if template.name != "single-node.pbs" else "mg0021\n")
            observations = workspace / "observations.jsonl"
            # Ambient project overrides must not hide the template's filled entrypoint.
            env = {key: value for key, value in os.environ.items()
                   if key not in {"ENTRYPOINT", "PYTHON_MODULE"}}
            env.update(PATH=str(bindir) + ":/usr/bin:/bin", PBS_JOBID="1234.opbs",
                       PBS_NODEFILE=str(nodefile), PYTHON_BIN=str(interpreter),
                       RUNTIME_CC="selected-gcc", RUNTIME_CXX="selected-g++",
                       TEST_OBSERVATIONS=str(observations), TEST_ACTOR_EXIT=str(actor_exit),
                       LOG_ROOT=str(project / "logs/attempt"))
            env["BASH_FUNC_module%%"] = "() { :; }"
            result = subprocess.run(["/bin/bash", str(script)], env=env, capture_output=True, text=True, timeout=10)
            records = [json.loads(line) for line in observations.read_text().splitlines()] if observations.exists() else []
            for record in records:
                self.assertEqual(record["argv0"], str(interpreter))
                self.assertEqual(record["python_bin"], str(interpreter))
                self.assertEqual(record["cwd"], str(project))
            return result, records

    def test_templates_have_valid_shell_syntax(self):
        """All shipped assets must be valid Bash before project substitutions."""
        self.assertEqual(len(TEMPLATES), 3)
        for template in TEMPLATES:
            with self.subTest(template=template.name):
                result = subprocess.run(["bash", "-n", str(template)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_interpreter_and_overrides_reach_actual_workers(self):
        """Catch symlink resolution, missing exports and login-shell override loss."""
        for template in TEMPLATES:
            with self.subTest(template=template.name):
                result, records = self.run_template(template)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(records), 2)
                for record in records:
                    self.assertEqual(record["cc"], "selected-gcc")
                    self.assertEqual(record["cxx"], "selected-g++")
                if template.name == "torchrun.pbs":
                    self.assertEqual(records[0]["args"][:2], ["-m", "torch.distributed.run"])
                    self.assertEqual(records[0]["args"][-2:], [
                        str(Path(records[0]["cwd"]) / "worker entrypoint.py"), "argument with spaces",
                    ])
                elif template.name == "mpi-workers.pbs":
                    self.assertEqual(records[0]["args"], ["-m", "example.worker", "argument with spaces"])
                else:
                    self.assertEqual(records[-1]["args"], ["worker entrypoint.py", "argument with spaces"])

    def test_worker_failure_survives_tee(self):
        """A successful log sink must not turn an application failure into success."""
        for template in TEMPLATES:
            with self.subTest(template=template.name):
                result, records = self.run_template(template, actor_exit=7)
                self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
                self.assertTrue(records)

    def test_login_guard_prevents_any_project_interpreter_call(self):
        """Reject inherited PBS variables on login before touching foreign binaries."""
        for template in TEMPLATES:
            with self.subTest(template=template.name):
                result, records = self.run_template(template, real_guard=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(records, [])
                self.assertEqual(json.loads(result.stdout)["error"]["type"], "execution_context")


if __name__ == "__main__":
    unittest.main()
