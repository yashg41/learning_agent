"""No large payload crosses a tool boundary.

A payload past roughly 14KB on the CLI's stdio transport kills the MCP channel
mid-session: the call returns "Stream closed", and so does every later MCP call,
including ones with two-byte arguments, while the CLI's own built-in tools keep
working. The agent finishes its work and cannot save it — four demos have had to
be rescued by hand from scratch.

The CLI's own stack trace shows the mechanism:

    createCanUseTool -> await this.sendRequest({subtype:"can_use_tool"…})
    async sendRequest(…) { if (this.inputClosed) throw Error("Stream closed") }

"Stream closed" is the CLI refusing to send a control request because the pipe
back to this process is shut. That also rules out the obvious fix: a PreToolUse
hook is another control request on that same pipe, so it cannot run once the
channel is broken and costs a round trip per call while it is healthy. The check
therefore lives inside the tool handlers, in this process, on arguments that
have already arrived.

The limit only reaches OUR tools. The CLI's built-in Read and Edit move as much
content as anything we define and no in-process check can see them; the skill
and the builder prompt carry that half.
"""

import asyncio
import json
import os

import pytest

import backend.agent as agent
from backend import demos


def refusal(tool_name: str, tool_input: dict) -> str | None:
    """The message a tool would return, or None if the payload fits."""
    return agent.check_payload_size(tool_name, tool_input)


# --- Arguments -------------------------------------------------------------


def test_the_edit_sized_payload_is_refused():
    """13,476 bytes was the outlier that took the channel down. The same size
    arriving on one of our own tools must be refused before it is stored."""
    msg = refusal(
        "mcp__learning-tools__update_demo",
        {"demo_id": "d_1234567890", "html": "y" * 13_476},
    )
    assert msg is not None
    assert "html" in msg and "13,476" in msg
    assert "html_path" in msg, "must name the alternative"


def test_ordinary_payloads_are_untouched():
    for size in (72, 561, 1_787, 7_999):
        assert refusal(
            "mcp__learning-tools__update_demo",
            {"demo_id": "d_1234567890", "html": "y" * size},
        ) is None, f"{size} bytes should pass"


def test_a_path_argument_is_never_counted_as_content():
    """The whole point of the rule is to move content by path."""
    assert refusal(
        "mcp__learning-tools__update_demo",
        {"demo_id": "d_1234567890", "html_path": "/tmp/" + "p" * 20_000},
    ) is None


def test_content_nested_in_a_list_is_measured():
    """derivation_write takes blocks=[...]; the bytes are inside the list."""
    msg = refusal(
        "mcp__learning-tools__derivation_write",
        {"doc_id": "x", "blocks": [{"kind": "text", "content": "z" * 9_000}]},
    )
    assert msg is not None and "blocks" in msg


def test_non_string_arguments_do_not_break_the_check():
    assert refusal(
        "mcp__learning-tools__demo_write_part",
        {"demo_id": "d", "index": 3, "name": "kv", "html": "small"},
    ) is None


def test_the_advice_is_specific_to_the_tool():
    """Generic advice would leave the model guessing."""
    shell = refusal(
        "mcp__learning-tools__demo_open",
        {"title": "t", "shell_html": "s" * 20_000},
    )
    assert "shell" in shell.lower()


# --- The tools we ship -----------------------------------------------------


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A user with enough history that an uncapped tool would blow the limit."""
    monkeypatch.setattr(demos, "USERS_DIR", str(tmp_path))

    from backend.knowledge import KnowledgeStore

    udir = tmp_path / "u"
    udir.mkdir()
    store = KnowledgeStore(str(udir), app="pymentor")
    for i in range(90):
        store.upsert_concept(
            concept_id=f"concept_{i}",
            name=f"Concept number {i}",
            category="sd_data" if i % 2 else "data_structures",
            mastery=("introduced", "practiced", "mastered")[i % 3],
            # Notes are what made the real payload 54KB: ~22KB of prose across
            # 84 concepts.
            notes="The learner worked through this in detail. " * 8,
        )
    return str(udir)


def _call(udir, name, **args):
    import mcp.types as types

    from backend.tools import create_learning_tools

    server = create_learning_tools(udir, None, {"session_id": "s"}, None, "pymentor")
    caller = server["instance"].request_handlers[types.CallToolRequest]

    async def go():
        return await caller(
            types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(name=name, arguments=args),
            )
        )

    return asyncio.run(go()).root.content[0].text


def test_learning_progress_summarises_instead_of_dumping(seeded):
    """It used to return every field of every concept — 54,842 bytes measured
    against a real learner, on a zero-argument tool the tutor can call at any
    time."""
    out = _call(seeded, "get_learning_progress")

    assert len(out) < agent.MAX_TOOL_PAYLOAD_BYTES, f"{len(out)} bytes"

    data = json.loads(out)
    # The shape of what the learner knows survives the trim.
    assert data["total_concepts"] == 90
    assert sum(len(v) for v in data["by_mastery"].values()) == 90
    # Notes are the bulk of the bytes and must not be in the summary.
    assert "The learner worked through this" not in out


def test_learning_progress_can_still_reach_the_detail(seeded):
    one = json.loads(_call(seeded, "get_learning_progress", concept_id="concept_1"))
    assert one["concept"]["concept_id"] == "concept_1"
    assert "notes" in one["concept"]

    grouped = json.loads(
        _call(seeded, "get_learning_progress", category="data_structures")
    )
    assert grouped["concepts"], "a real category should return rows"
    assert all(
        c["category"] == "data_structures" for c in grouped["concepts"].values()
    )


def test_domain_map_defaults_to_what_can_be_started_now(seeded):
    """A whole track is ~50 concepts with prerequisite lists — 15KB in one
    result, which then rides in the transcript for the rest of the session."""
    out = _call(seeded, "get_domain_map", track="sysdesign")

    assert len(out) < agent.MAX_TOOL_PAYLOAD_BYTES, f"{len(out)} bytes"
    data = json.loads(out)
    assert data["showing"] == "available"
    assert all(c["status"] == "available" for c in data["concepts"])
    # The counts are kept, so the shape of the track survives the filter.
    assert {"total", "available", "blocked", "known"} <= set(data)


def test_the_guard_and_the_caps_agree():
    """A cap that is looser than the guard would deny a call nothing can make."""
    assert agent.MAX_TOOL_PAYLOAD_BYTES <= 14_000, (
        "14,264 bytes is the smallest payload observed before a failure"
    )


def test_the_tools_actually_apply_the_check(seeded):
    """The check existing is not the same as the tools calling it.

    Disabling the call sites while keeping check_payload_size intact passed the
    whole suite once — every other test here exercises the function directly.
    This one goes through the real MCP handler.
    """
    out = _call(
        seeded,
        "update_demo",
        demo_id="d_1234567890",
        html="<!doctype html><html><body>" + "x" * 20_000 + "</body></html>",
    )
    assert "8,000-byte limit" in out, out[:200]
    assert "html_path" in out
