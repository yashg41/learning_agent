"""A demo is moved by path, never carried through a tool call.

Editing one used to send the whole document across the CLI transport four
times — get_demo_html returned it, Write sent it, Read returned it, update_demo
sent it — for a change that touched 41 lines. One of those 24KB calls failed
with "Stream closed" and took every later call on that session with it,
including one whose argument was two bytes. The builder had finished the work
and simply could not save it.

The document now stays on disk. The server copies it into the builder's scratch
directory and hands back a path; the builder patches that file with Edit and
passes the path back. These tests pin that the payloads stay small and that a
path from a model cannot reach outside scratch.
"""

import json
import os

import pytest

from backend import demos
from backend.sandbox import SCRATCH_ROOT


HTML = (
    "<!doctype html><html><body><div id='out'></div>"
    "<script>function go(){document.getElementById('out').textContent='x';}"
    "go();</script></body></html>"
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(demos, "USERS_DIR", str(tmp_path))
    return "u"


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """A scratch root inside tmp_path, so the confinement check has a real root."""
    root = tmp_path / "scratch"
    root.mkdir()
    monkeypatch.setattr("backend.sandbox.SCRATCH_ROOT", root)
    return root


# --- Paths in, paths out ---------------------------------------------------


def test_saving_by_path_matches_saving_inline(store, scratch):
    """The two routes must store byte-identical documents."""
    draft = scratch / "demo.html"
    draft.write_text(HTML)

    by_path = demos.save_demo(store, "path", html_path=str(draft))
    inline = demos.save_demo(store, "inline", html=HTML)

    assert (
        demos.get_demo(store, by_path["id"])["html"]
        == demos.get_demo(store, inline["id"])["html"]
    )


def test_checkout_patch_publish_round_trip(store, scratch):
    """The whole edit flow, without the document ever being an argument."""
    meta = demos.save_demo(store, "demo", html=HTML)

    path = demos.copy_demo_to_scratch(store, meta["id"], str(scratch))
    assert os.path.isfile(path)

    # What Edit would do to the checked-out file.
    patched = open(path).read().replace("'x'", "'patched'")
    open(path, "w").write(patched)

    updated = demos.update_demo(store, meta["id"], html_path=path)

    assert updated["version"] == 2
    assert "'patched'" in demos.get_demo(store, meta["id"])["html"]


def test_checkout_copies_rather_than_exposing_the_store(store, scratch):
    """A half-finished edit must not touch what the learner is looking at."""
    meta = demos.save_demo(store, "demo", html=HTML)
    path = demos.copy_demo_to_scratch(store, meta["id"], str(scratch))

    open(path, "w").write("<!doctype html><html><body>WIP</body></html>")

    # The live demo is untouched until update_demo publishes.
    assert "WIP" not in demos.get_demo(store, meta["id"])["html"]


# --- The path comes from a model, so it is not trusted ----------------------


def test_a_path_outside_scratch_is_refused(store, scratch, tmp_path):
    """Otherwise any file the server can read could be published as a demo.

    The path is chosen by a model acting on a learner's request, so this is the
    boundary that keeps "build me a demo" from becoming "show me /etc/passwd".
    """
    outside = tmp_path / "secret.html"
    outside.write_text(HTML)

    for bad in (
        str(outside),
        "/etc/hosts",
        str(scratch / ".." / "secret.html"),
    ):
        with pytest.raises(ValueError, match="scratch"):
            demos.read_builder_html(bad)


def test_a_symlink_out_of_scratch_is_refused(store, scratch, tmp_path):
    """realpath is what makes the check hold — a name inside, a target outside."""
    outside = tmp_path / "secret.html"
    outside.write_text(HTML)
    link = scratch / "looks_local.html"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="scratch"):
        demos.read_builder_html(str(link))


def test_missing_and_oversized_drafts_are_refused(store, scratch, monkeypatch):
    with pytest.raises(ValueError, match="No file"):
        demos.read_builder_html(str(scratch / "nope.html"))

    monkeypatch.setattr(demos, "MAX_DEMO_BYTES", 100)
    big = scratch / "big.html"
    big.write_text("x" * 200)
    with pytest.raises(ValueError, match="over the"):
        demos.read_builder_html(str(big))


def test_neither_html_nor_path_is_refused(store):
    with pytest.raises(ValueError, match="html_path"):
        demos.save_demo(store, "empty")


# --- The payload guard -----------------------------------------------------


def test_get_demo_html_returns_a_path_not_the_document(store, scratch):
    """The regression guard.

    If this tool ever returns the document again, an edit silently goes back to
    carrying five copies of it through the transport. The demo here is
    deliberately large so a stray full return cannot pass on size.
    """
    import mcp.types as types

    from backend.tools import create_learning_tools

    udir = os.path.join(demos.USERS_DIR, store)
    os.makedirs(udir, exist_ok=True)

    big = HTML + "<!--" + "pad " * 4000 + "-->"
    meta = demos.save_demo(store, "big", html=big)
    assert meta["bytes"] > 15_000, "the fixture must be big enough to matter"

    ctx = {
        "chat_session_id": "",
        "builder_session_id": "",
        "demo_id": None,
        "scratch_dir": str(scratch),
    }
    server = create_learning_tools(udir, None, None, ctx, "pymentor")
    caller = server["instance"].request_handlers[types.CallToolRequest]

    async def go():
        return await caller(
            types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(
                    name="get_demo_html", arguments={"demo_id": meta["id"]}
                ),
            )
        )

    import asyncio

    text = asyncio.run(go()).root.content[0].text

    assert len(text) < 1_000, f"returned {len(text)} bytes — the document is back"
    assert os.path.isfile(json.loads(text)["path"])


def test_the_builder_can_slice_a_demo_it_is_editing(store, scratch):
    """get_demo_source was tutor-only, which locked out the agent that edits.

    Its guard was `session_ctx is None`, set on builder runs to stop a recursive
    request_demo — so the exclusion was collateral, and the error message sent
    the builder to the whole-document reader instead.
    """
    import asyncio

    import mcp.types as types

    from backend.tools import create_learning_tools

    udir = os.path.join(demos.USERS_DIR, store)
    os.makedirs(udir, exist_ok=True)
    meta = demos.save_demo(store, "demo", html=HTML)

    ctx = {"chat_session_id": "", "builder_session_id": "", "demo_id": None}
    server = create_learning_tools(udir, None, None, ctx, "pymentor")
    caller = server["instance"].request_handlers[types.CallToolRequest]

    async def go():
        return await caller(
            types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(
                    name="get_demo_source",
                    arguments={"demo_id": meta["id"], "query": "getElementById"},
                ),
            )
        )

    text = asyncio.run(go()).root.content[0].text
    assert "not available here" not in text
    assert "getElementById" in text


def test_the_builder_can_patch_files(store):
    """Edit is what makes a small change small. Without it the only write verb
    is Write, which replaces the whole file — and Glob/Grep become useless,
    since the builder could find the line and not change it."""
    from backend.agent import DEMO_BUILDER_DENIED_TOOL_NAMES, DENIED_TOOL_NAMES

    assert "Edit" not in DEMO_BUILDER_DENIED_TOOL_NAMES
    # Still denied to the tutor, whose job never involves writing files.
    assert "Edit" in DENIED_TOOL_NAMES
    # And the dangerous ones stay denied to both.
    for name in ("Bash", "Task", "BashOutput", "KillShell"):
        assert name in DEMO_BUILDER_DENIED_TOOL_NAMES
