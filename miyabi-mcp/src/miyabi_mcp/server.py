"""The MCP stdio server, built on the official SDK's low-level Server (DESIGN.md 6.1).

Tool work is synchronous (PBS commands, files) and runs in worker threads, so one slow call (job_wait)
never blocks the others. Results carry structuredContent and the same JSON as compact text.
"""

from __future__ import annotations

import threading

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import tools, version
from .config import Settings
from .util import compact

INSTRUCTIONS = """\
miyabi-mcp operates Miyabi PBS for this project: queries, submission, cancellation, a job ledger in
<project>/.miyabi-mcp/ and wake-ups. Rules:
- Call pbs_context first. The server runs on a Miyabi login node: it never runs workloads; tests,
  training and anything heavy run inside PBS jobs.
- Submitting is two calls: job_submit (dry run) shows the checked plan; show it to the user when the
  job is not clearly within their authorization, then call again with dry_run=false and the plan_digest.
  Size walltime from measurements (about 1.5x); a user's limit is a cap, never the request.
- Interactive work: call interactive_plan with an interact-g/interact-c/interact-mig queue, walltime and
  a unique request_key. It only prepares a checked plan. Run its launch_command in pi-env-mcp terminal_open
  on the login node within 10 minutes; keep terminal_id and use terminal_read/send for that same shell.
  The runner rechecks policy, submits qsub -I, records the job ID, and closes the terminal when qsub ends.
  Wait for the compute prompt, then run allocation_check_command IN THAT TERMINAL and require ok=true.
  pbs_context/allocation_check on this login-side MCP server cannot verify the remote compute shell.
  Set modules, cd to the project and select its interpreter there; inspect remaining walltime before work.
  Run fallible checks in a child shell so failure does not close the allocation. Preserve outputs under
  /work, stop owned background processes, then exit; use job_get to confirm completion. For cancellation
  use job_cancel dry-run/commit for the exact ID, then terminal_close. Terminal timeouts do not cancel;
  lost terminals cannot be reattached. Check the existing job before retrying with the same request_key.
- FINISH and Exit_status=0 do not mean the application succeeded: check the outputs. A FINISH without
  a start time never ran.
- Do not poll in a loop. For jobs of a few minutes use job_wait. For longer ones submit with
  notify={"name": ..., "expected": <jobs in the group>, "message": <what to do next>}: one notification
  covers a group (e.g. all seeds). After every registration or join, run the command the result's
  `next` names on your own machine, even when the daemon seems to be running (it is idempotent). With
  deliver=listener (the default) only daemon.listen_command shows the message (Claude Code: Bash with
  run_in_background=true); daemon.start_command alone never does. Other delivery methods need
  daemon.start_command. One receiver per notification: the agent that submitted listens; when two
  agents must wake, create two notifications. Keep working on things that do not depend on the jobs.
  When notified, read the jobs with job_get, then notify_ack with the token of the final message.
- Cancel only jobs this project submitted (job_cancel dry run first).
"""


def build(settings: Settings) -> Server:
    # version.label() pins the commit this server runs (a long-lived server keeps it while the checkout moves on).
    server: Server = Server("miyabi-mcp", version=version.label(), instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(name=tool.name, title=tool.title, description=tool.description,
                           inputSchema=tool.input_schema, outputSchema=tools.OUTPUT_SCHEMA,
                           annotations=types.ToolAnnotations(title=tool.title, readOnlyHint=tool.read_only,
                                                             destructiveHint=tool.destructive,
                                                             idempotentHint=tool.idempotent, openWorldHint=True))
                for tool in tools.TOOLS]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        context = server.request_context
        token = context.meta.progressToken if context.meta else None
        session = context.session
        stop = threading.Event()
        hooks = {}
        if name == "job_wait":
            def progress(elapsed: float, total: float) -> None:
                if token is not None:
                    anyio.from_thread.run(session.send_progress_notification, token, elapsed, total,
                                          "waiting for jobs")
            hooks = {"cancelled": stop.is_set, "progress": progress}
        actor = {"client": getattr(getattr(session, "client_params", None), "clientInfo", None)}
        if name == "job_submit" and actor["client"] is not None:
            arguments = {**(arguments or {}), "_actor": {"client": f"{actor['client'].name} {actor['client'].version}"}}
        try:
            result = await anyio.to_thread.run_sync(lambda: tools.run(name, settings, arguments, **hooks),
                                                    abandon_on_cancel=True)
        except anyio.get_cancelled_exc_class():
            stop.set()
            raise
        note = version.stale_note()
        if note:
            result = {**result, "server_note": note}
        return types.CallToolResult(content=[types.TextContent(type="text", text=compact(result))],
                                    structuredContent=result, isError=not result.get("ok", False))

    return server


async def _main(settings: Settings) -> None:
    server = build(settings)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def serve(settings: Settings) -> int:
    anyio.run(_main, settings)
    return 0
