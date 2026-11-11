"""The demo builder is a working session, not a one-shot tool call.

It is spun up as its own agent, handed a request, and expected to return a
finished demo — so it needs room to draft, check what it wrote, and fix it. Two
builds of the same ten-panel request produced nothing while it was equipped as a
single tool call: the per-build prompt told it to emit the whole document at
once, it had nowhere to draft, and no way to check its own work.

These tests pin the parts of that which are decidable without running an agent:
what it is allowed to touch, what it is told, and whether a failure explains
itself.
"""

import asyncio

import pytest

import backend.agent as agent
from backend import demojobs, demos
from backend.prompts import demo_builder_prompt


# --- What the builder may touch -------------------------------------------


def test_builder_can_draft_but_never_shells_out():
    """File tools yes; a shell absolutely not.

    The request text comes from a learner and permission_mode is
    bypassPermissions, so a shell would turn "build me a demo" into arbitrary
    code execution as the server's user.
    """
    denied = agent.DEMO_BUILDER_DENIED_TOOL_NAMES

    for gained in ("Write", "Read", "Glob", "Grep"):
        assert gained not in denied, f"builder cannot draft without {gained}"

    for forbidden in ("Bash", "BashOutput", "KillShell", "Task"):
        assert forbidden in denied, f"{forbidden} must stay denied"


def test_the_tutor_gains_nothing_from_this():
    """Widening the builder must not widen the tutor.

    Every justification on DENIED_TOOL_NAMES is written about the tutor — "a
    tutoring agent has no business running commands or touching the
    filesystem" — and that still holds.
    """
    for name in ("Bash", "Write", "Read", "Glob", "Grep", "Task"):
        assert name in agent.DENIED_TOOL_NAMES


def test_run_code_is_offered_only_where_it_exists():
    """It rides in the "code" family, which Sanskrit does not have.

    Naming a tool the server never registered advertises something that cannot
    be called.
    """
    pymentor = agent.demo_builder_tool_names("pymentor")
    sanskrit = agent.demo_builder_tool_names("sanskrit")

    assert "mcp__learning-tools__run_code" in pymentor
    assert "mcp__learning-tools__run_code" not in sanskrit
    # demo_verify reads from disk, so every app gets it.
    for names in (pymentor, sanskrit):
        assert "mcp__learning-tools__demo_verify" in names


# --- What the builder is told ---------------------------------------------


def test_the_per_build_prompt_does_not_fight_the_system_prompt():
    """The contradiction that produced a shell with no panels.

    _build_prompt used to end "Call save_demo once with the complete HTML"
    while the system prompt taught demo_open and a declared parts array. The
    builder split the difference: it wrote the shell, declared nothing, and the
    ten-panel request produced an empty build.
    """
    record = {"title": "Database Types", "request": "ten engines", "concept_id": "db"}
    fresh = demojobs._build_prompt(record, None)
    edit = demojobs._build_prompt(record, "d_abc1234567")
    system = demo_builder_prompt("pymentor")

    assert "demo_open" in system, "system prompt no longer teaches the split path"
    for mandate in ("save_demo once", "save_demo exactly once", "full replacement"):
        assert mandate not in fresh
        assert mandate not in edit


def test_the_prompt_asks_for_a_session_not_one_reply():
    """It may take as many turns as it needs, and must check its own work."""
    system = demo_builder_prompt("pymentor")

    assert "Do not call any other tool" not in system
    assert "Your entire output is the tool call" not in system
    assert "demo_verify" in system or "check" in system.lower()


# --- Failures explain themselves ------------------------------------------


def test_cli_stderr_is_captured_and_bounded():
    """Two builds died reporting only "Stream closed" and no reason.

    The SDK pipes the CLI's stderr only when this callback is set; unset, it
    stays attached to the server's own where it is unprefixed and uncorrelated
    with any build. Worse, the SDK substitutes the literal string "Check stderr
    output for details" as the error text — so the one place that would explain
    the failure says nothing.
    """
    sink, buf = agent._make_stderr_sink("sess-1")

    for i in range(agent._STDERR_KEEP_LINES * 3):
        sink(f"line {i}")

    assert len(buf) == agent._STDERR_KEEP_LINES, "unbounded: a chatty run would grow"
    # The tail is what explains a death; the head is startup noise.
    assert buf[-1] == f"line {agent._STDERR_KEEP_LINES * 3 - 1}"

    sink("")
    sink("   ")
    assert len(buf) == agent._STDERR_KEEP_LINES, "blank lines should not evict"


def test_stderr_tail_logs_only_when_there_is_something_to_say(caplog):
    sink, buf = agent._make_stderr_sink("sess-2")

    agent._log_stderr_tail(buf, "sess-2")
    assert not caplog.records, "an empty buffer must stay silent"

    sink("Error: MCP server 'learning-tools' disconnected")
    with caplog.at_level("ERROR"):
        agent._log_stderr_tail(buf, "sess-2")
    assert any("learning-tools" in r.getMessage() for r in caplog.records)


def test_sdk_accepts_the_stderr_option():
    """Guards the wiring: a renamed option would silently stop capturing."""
    from claude_agent_sdk import ClaudeAgentOptions

    options = ClaudeAgentOptions(stderr=lambda line: None)
    assert options.stderr is not None


# --- Verifying its own work -----------------------------------------------


SHELL = (
    "<!doctype html><html><head><style>.p{}</style></head><body>\n"
    + demos.PARTS_MARKER
    + "\n<script>function show(i){}\nshow(0);</script></body></html>"
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(demos, "USERS_DIR", str(tmp_path))
    return "u"


def _verify(store_email, demo_id, build_ctx):
    """Call demo_verify through the real MCP request path."""
    import mcp.types as types
    from backend.tools import create_learning_tools

    import os

    udir = os.path.join(demos.USERS_DIR, store_email)
    os.makedirs(udir, exist_ok=True)
    server = create_learning_tools(udir, None, None, build_ctx, "pymentor")
    caller = server["instance"].request_handlers[types.CallToolRequest]

    async def go():
        return await caller(
            types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(
                    name="demo_verify", arguments={"demo_id": demo_id}
                ),
            )
        )

    return asyncio.run(go()).root.content[0].text


def test_demo_verify_reports_a_lookup_no_element_answers(store):
    """The fault that actually ships: a demo that throws on first click."""
    html = (
        "<!doctype html><html><body><div id='out'></div>"
        "<script>function go(){document.getElementById('nope').textContent='x';}"
        "go();</script></body></html>"
    )
    meta = demos.save_demo(store, "broken", html)
    ctx = {"chat_session_id": "", "builder_session_id": "", "demo_id": None}

    out = _verify(store, meta["id"], ctx)

    assert "nope" in out
    # An instruction, not a warning it can ignore — the whole point.
    assert "again" in out
    # On the meta, not build_ctx: over the HTTP transport the tool's build_ctx
    # is a per-request copy, so the job runner reads the verdict off disk.
    assert demos.get_demo_meta(store, meta["id"])["verified"] is False
    assert "verified" not in ctx


def test_demo_verify_passes_a_sound_demo(store):
    html = (
        "<!doctype html><html><body><div id='out'></div>"
        "<script>function go(){document.getElementById('out').textContent='x';}"
        "go();</script></body></html>"
    )
    meta = demos.save_demo(store, "clean", html)
    ctx = {"chat_session_id": "", "builder_session_id": "", "demo_id": None}

    out = _verify(store, meta["id"], ctx)

    assert '"broken": []' in out
    assert demos.get_demo_meta(store, meta["id"])["verified"] is True


def test_demo_verify_is_builder_only(store):
    """The tutor must not reach it — build_ctx is the real boundary."""
    import os

    import mcp.types as types
    from backend.tools import create_learning_tools

    udir = os.path.join(demos.USERS_DIR, store)
    os.makedirs(udir, exist_ok=True)
    server = create_learning_tools(udir, None, {"session_id": "s"}, None, "pymentor")
    caller = server["instance"].request_handlers[types.CallToolRequest]

    async def go():
        return await caller(
            types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(
                    name="demo_verify", arguments={"demo_id": "d_1234567890"}
                ),
            )
        )

    assert "not available here" in asyncio.run(go()).root.content[0].text
