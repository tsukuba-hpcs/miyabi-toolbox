"""The MCP server over stdio, driven by the official SDK client through bin/miyabi-mcp."""

import json
import os
import subprocess
import time
import unittest

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from base import ROOT, FakePbsCase
from miyabi_mcp import server, version


class ServerTests(FakePbsCase):
    def run_session(self, body):
        params = StdioServerParameters(command=str(ROOT / "bin" / "miyabi-mcp"),
                                       args=["serve", "--project-dir", str(self.project)], env=dict(os.environ))

        async def main():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    return await body(session, init)

        return anyio.run(main)

    def test_tools_results_and_errors(self):
        async def body(session, init):
            listed = await session.list_tools()
            context = await session.call_tool("pbs_context", {})
            plan = await session.call_tool("job_submit", {"script": str(self.script), "queue": "debug-g",
                                                          "walltime": "00:10:00"})
            bad = await session.call_tool("job_get", {"job_ids": ["1"], "bogus": True})
            missing = await session.call_tool("notify_ack", {"id": "N-x", "ack_token": "t"})
            return init, listed, context, plan, bad, missing

        init, listed, context, plan, bad, missing = self.run_session(body)
        self.assertEqual(init.serverInfo.name, "miyabi-mcp")
        self.assertEqual(init.serverInfo.version, version.label())  # 0.1.0+<commit of the checkout>
        self.assertEqual(context.structuredContent["server"]["commit"], version.running())
        self.assertNotIn("server_note", context.structuredContent)  # the checkout has not moved on
        self.assertIn("job_submit (dry run)", init.instructions)
        names = {tool.name for tool in listed.tools}
        self.assertEqual(names, {"pbs_context", "pbs_queues", "job_list", "job_get", "job_submit", "job_cancel",
                                 "job_wait", "notify_create", "notify_update", "notify_list", "notify_cancel", "notify_ack",
                                 "ledger_query", "job_annotate", "interactive_plan", "allocation_check"})
        cancel = next(tool for tool in listed.tools if tool.name == "job_cancel")
        self.assertTrue(cancel.annotations.destructiveHint)
        self.assertTrue(next(t for t in listed.tools if t.name == "job_list").annotations.readOnlyHint)
        self.assertFalse(context.isError)
        self.assertEqual(context.structuredContent["settings"]["project_dir"], str(self.project))
        self.assertTrue(plan.structuredContent["valid"])
        self.assertTrue(bad.isError)  # schema rejects unknown arguments
        self.assertTrue(missing.isError)
        self.assertEqual(missing.structuredContent["error"]["type"], "not_found")

    def test_interactive_tools_over_mcp(self):
        async def body(session, init):
            plan = await session.call_tool("interactive_plan", {"queue": "interact-g", "walltime": "00:02:00",
                                                                "request_key": "mcp-interactive"})
            bad = await session.call_tool("interactive_plan", {"queue": "interact-g", "walltime": "00:02:00",
                                                               "request_key": "mcp-interactive", "script": "x.pbs"})
            guard = await session.call_tool("allocation_check", {"target_system": "Miyabi-G"})
            return init, plan, bad, guard

        init, plan, bad, guard = self.run_session(body)
        self.assertIn("interactive_plan", init.instructions)
        self.assertFalse(plan.isError)
        self.assertTrue(plan.structuredContent["valid"])
        self.assertIn("interactive-start", plan.structuredContent["launch_command"])
        self.assertEqual(self.read_state()["jobs"], {})
        self.assertTrue(bad.isError)
        self.assertTrue(guard.isError)
        self.assertEqual(guard.structuredContent["error"]["type"], "allocation_unconfirmed")

    def test_job_wait_reports_progress(self):
        job_id = self.submit()["job_id"]
        seen = []

        async def body(session, _init):
            async def progress(value, total, message):
                seen.append((value, total, message))

            return await session.call_tool("job_wait", {"job_ids": [job_id], "timeout_s": 12},
                                           progress_callback=progress)

        result = self.run_session(body)
        self.assertFalse(result.structuredContent["met"])
        self.assertTrue(seen)

    def test_a_closed_stdin_ends_a_running_job_wait_at_once(self):
        """pi-env-mcp closes a hosted server's stdin and kills it 2 s later; until 11-06 job_wait slept on
        for up to 10 s."""
        job_id = self.submit()["job_id"]  # stays queued: the wait would run to its timeout
        process = subprocess.Popen([str(ROOT / "bin" / "miyabi-mcp"), "serve", "--project-dir", str(self.project)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())

        def send(message: dict) -> None:
            process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
            process.stdin.flush()

        send({"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                          "clientInfo": {"name": "test", "version": "0"}}})
        self.assertIn("result", json.loads(process.stdout.readline()))
        send({"method": "notifications/initialized"})
        send({"id": 2, "method": "tools/call", "params": {"name": "job_wait",
                                                          "arguments": {"job_ids": [job_id], "timeout_s": 40}}})
        time.sleep(1.5)  # inside its first pause
        closed = time.monotonic()
        process.stdin.close()
        process.wait(timeout=30)
        self.assertLess(time.monotonic() - closed, 2.0)


class InstructionsTests(unittest.TestCase):
    def test_instructions_fit_what_pi_env_mcp_shows(self):
        self.assertLessEqual(len(server.INSTRUCTIONS), 2000)  # pi-env-mcp cuts a hosted server's instructions there


if __name__ == "__main__":
    unittest.main()


class VersionTests(unittest.TestCase):
    def test_a_server_behind_its_checkout_says_so(self):
        current = version.checkout_commit()
        self.assertIsNotNone(current)  # the tests run from a git checkout
        saved = dict(version._cache)
        self.addCleanup(lambda: (version._cache.clear(), version._cache.update(saved)))
        version._cache.clear()
        version._cache["running"] = "0000000"  # as if the server started before the latest commit
        note = version.stale_note(max_age=0)
        self.assertIn(f"runs 0000000, but its checkout is now at {current}", note)
        described = version.describe()
        self.assertEqual((described["commit"], described["checkout"]), ("0000000", current))
        self.assertIn("Restart the MCP server", described["restart_needed"])
        version._cache["running"] = current
        self.assertIsNone(version.stale_note(max_age=0))

