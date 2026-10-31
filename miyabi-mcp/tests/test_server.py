"""The MCP server over stdio, driven by the official SDK client through bin/miyabi-mcp."""

import os
import unittest

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from base import ROOT, FakePbsCase


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


if __name__ == "__main__":
    unittest.main()
