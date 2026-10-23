"""The learning tools, served over local HTTP instead of the SDK's stdin pipe.

WHY THIS EXISTS

An SDK MCP server (`type: "sdk"`) has no transport of its own: the tool
functions live in this process, and the CLI reaches them by calling *back* down
the same stdin pipe the prompt was written to. The SDK holds that pipe open
only until the turn's first `result` message, capped at 60 seconds
(`CLAUDE_CODE_STREAM_CLOSE_TIMEOUT`), then closes it regardless:

    if self.sdk_mcp_servers or has_hooks:          # true ONLY for type "sdk"
        with anyio.move_on_after(self._stream_close_timeout):   # 60s
            await self._first_result_event.wait()
    await self.transport.end_input()               # closes stdin

After that every callback throws `Stream closed`. Measured across this
project's debug logs: max age of any successful call 59.3s, min age of any
failure 60.2s — 560 calls, 29 tools, zero overlap. And 314 in-turn retries with
0 successes, because the close is one-way.

`sdk_mcp_servers` is populated only for `type == "sdk"`, so an HTTP server never
enters it, the branch above is skipped, and stdin closing costs nothing — the
CLI opens its own connection and keeps it as long as it needs. That is the whole
reason this module exists; it is not a performance change.

WHAT IS AND IS NOT DIFFERENT

The 32 tools are unchanged. `create_learning_tools` still builds them, still
filters by app, and still gates builder-only tools on `build_ctx`. Only the
transport in front of them moves.

What changes is *when* that factory runs. As an SDK server it was built once per
agent run, closing over the session and the build context. One HTTP endpoint is
shared by every concurrent run — the tutor and a demo builder overlap by
design — so that identity has to arrive with each request instead. It travels
as headers, and `_ctx_from_headers` turns them back into the factory arguments.
"""

import json
import logging
import os

import mcp.types as mt
from mcp.server.fastmcp import FastMCP

from backend.config import settings
from backend.tools import build_tool_list

logger = logging.getLogger(__name__)

# Header names the agent sets when it launches a run. Prefixed so they cannot
# collide with anything the MCP client sends for its own purposes.
H_EMAIL = "x-pymentor-email"
H_APP = "x-pymentor-app"
H_ROLE = "x-pymentor-role"          # "tutor" | "builder"
H_SESSION = "x-pymentor-session"
H_BUILD_CTX = "x-pymentor-build-ctx"   # JSON, builder only


mcp = FastMCP("learning-tools")
_server = mcp._mcp_server


def _headers() -> dict:
    """Headers of the request being served, lowercased by Starlette."""
    try:
        request = mcp.get_context().request_context.request
    except Exception:
        return {}
    return dict(getattr(request, "headers", {}) or {})


def _ctx_from_headers():
    """Rebuild this request's tool set from the headers the agent sent.

    Returns (tools_by_name, role). The factory is what enforces the tutor /
    builder split: builder-only tools check `build_ctx is None` and refuse, so
    passing build_ctx only for a builder keeps that boundary exactly where it
    already was. `allowed_tools` is advisory under bypassPermissions, so this
    must not be relaxed into a naming convention.
    """
    h = _headers()
    email = h.get(H_EMAIL, "")
    app = h.get(H_APP) or settings.APP
    role = h.get(H_ROLE, "tutor")
    session_id = h.get(H_SESSION) or None

    build_ctx = None
    if role == "builder":
        raw = h.get(H_BUILD_CTX)
        # A builder with no context still gets a dict: its presence is the
        # signal that unlocks save_demo, and scratch_dir may legitimately be
        # absent when the mkdir upstream failed (the tool reports that itself).
        try:
            build_ctx = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            logger.warning("Unparseable build-ctx header; treating as empty")
            build_ctx = {}

    from backend.agent import _get_episodic, _get_user_data_dir

    user_data_dir = _get_user_data_dir(email) if email else settings.DATA_DIR
    session_ctx = {"session_id": session_id} if session_id else None

    tools = {
        t.name: t
        for t in build_tool_list(
            user_data_dir,
            _get_episodic(),
            session_ctx=session_ctx,
            build_ctx=build_ctx,
            app=app,
        )
    }
    return tools, role


@_server.list_tools()
async def _list_tools() -> list[mt.Tool]:
    tools, role = _ctx_from_headers()
    # inputSchema is handed over verbatim. These schemas are hand-written with
    # descriptions the model actually reads; letting FastMCP re-derive them
    # from type hints would quietly discard that wording.
    return [
        mt.Tool(name=t.name, description=t.description, inputSchema=t.input_schema)
        for t in tools.values()
    ]


@_server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[mt.TextContent]:
    tools, role = _ctx_from_headers()
    tool = tools.get(name)
    if tool is None:
        # Not in this request's set: either the app does not enable it, or it
        # is builder-only and this is the tutor. Same answer either way.
        raise ValueError(f"{name} is not available to this run")

    result = await tool.handler(arguments)

    blocks = result.get("content") or []
    out = [
        mt.TextContent(type="text", text=b.get("text", ""))
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    if result.get("is_error"):
        # Surface the tool's own error as an error, so a real failure is never
        # mistaken for the transport dropping the call.
        raise ValueError(out[0].text if out else f"{name} failed")
    return out


def mcp_url() -> str:
    """Where the CLI should reach these tools.

    Loopback by construction: these tools read and write the learner's data with
    no auth of their own, so binding to 127.0.0.1 is the boundary. Never
    advertise this on a routable interface.
    """
    return f"http://127.0.0.1:{settings.API_PORT}/mcp-tools/mcp"


def mcp_headers(email: str, app: str, role: str,
                session_id: str | None = None,
                build_ctx: dict | None = None) -> dict:
    """Headers that carry one run's identity to the shared endpoint."""
    h = {H_EMAIL: email or "", H_APP: app, H_ROLE: role}
    if session_id:
        h[H_SESSION] = session_id
    if role == "builder":
        # Only the keys the tools read, and only INBOUND. Whatever a tool
        # writes into its copy dies with the request, so no tool reports back
        # through build_ctx: builds stamp job_id onto what they write and the
        # job runners read the result off disk (demojobs / mathjobs
        # _run_build). builder_session_id is known here because agent.py
        # pre-assigns it before building these headers.
        keep = {k: v for k, v in (build_ctx or {}).items()
                if k in ("scratch_dir", "demo_id", "doc_id", "builder_session_id",
                         "job_id", "chat_session_id")}
        h[H_BUILD_CTX] = json.dumps(keep)
    return h
