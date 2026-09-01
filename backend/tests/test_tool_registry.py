"""Per-app tool families.

One backend serves more than one tutor, and they do not want the same tools. A
Sanskrit tutor handed run_code would "verify" a declension by writing a Python
script instead of calling the analyser that actually knows the answer — which
is the exact failure the correctness tools exist to prevent.

The invariant worth the most here is the last one: the allowlist the model is
shown and the tools actually registered come from the same family selection, so
the tutor can never be told about a tool that does not exist. That drift is
silent — the model calls the tool, gets an error it did not expect, and burns a
turn on it.
"""

import pytest

from backend.agent import (
    DEMO_BUILDER_TOOL_NAMES,
    DERIVATION_BUILDER_TOOL_NAMES,
    MCP_TOOL_NAMES,
    _TUTOR_TOOLS_BY_FAMILY,
    tutor_tool_names,
)
from backend.tools import APP_TOOL_FAMILIES, app_tool_families


def _bare(name: str) -> str:
    """'mcp__learning-tools__run_code' -> 'run_code'."""
    return name.rsplit("__", 1)[-1]


def test_core_is_implicit_for_every_app():
    """A tutor that cannot record what it taught is not a tutor."""
    for app in APP_TOOL_FAMILIES:
        assert "core" in app_tool_families(app)


def test_unknown_app_fails_loudly():
    with pytest.raises(ValueError, match="Unknown app"):
        app_tool_families("nonexistent")


def test_pymentor_keeps_everything_it_had():
    families = app_tool_families("pymentor")
    assert set(families) == {"core", "code", "demos", "derivations"}


def test_sanskrit_has_no_code_execution():
    """The point of the split.

    run_code executes model-authored Python as a subprocess. A Sanskrit tutor
    has no Python to teach, and offering it invites the model to check grammar
    by writing a script rather than calling the analyser.
    """
    assert "code" not in app_tool_families("sanskrit")
    names = tutor_tool_names("sanskrit")
    assert not any("run_code" in n for n in names)
    assert not any("get_code_result" in n for n in names)


def test_sanskrit_keeps_the_shared_tutoring_machinery():
    """Memory, knowledge graph, quizzes and feedback are subject-neutral."""
    bare = {_bare(n) for n in tutor_tool_names("sanskrit")}
    for essential in (
        "update_concept", "suggest_next_topics", "get_domain_map",
        "search_past_conversations", "save_conversation_summary",
        "generate_quiz_question", "record_quiz_result", "get_feedback",
    ):
        assert essential in bare, f"sanskrit lost {essential}"


def test_every_app_can_still_look_things_up():
    for app in APP_TOOL_FAMILIES:
        names = tutor_tool_names(app)
        assert "WebSearch" in names and "WebFetch" in names


def test_back_compat_name_matches_pymentor():
    assert MCP_TOOL_NAMES == tutor_tool_names("pymentor")


def test_builder_only_writers_stay_off_the_tutor_allowlist():
    """The tutor requests; the builder writes.

    Keeping save_demo and derivation_write off this list is what stops the
    tutor emitting 30KB of HTML inside a chat turn, or assembling a derivation
    inline and stalling the composer.
    """
    for app in APP_TOOL_FAMILIES:
        bare = {_bare(n) for n in tutor_tool_names(app)}
        for writer in (
            "save_demo", "update_demo", "get_demo_html",
            "derivation_open", "derivation_write", "derivation_verify",
            "derivation_read", "derivation_plot",
        ):
            assert writer not in bare, f"{app} tutor can call {writer} directly"


def test_no_duplicate_tool_names_per_app():
    for app in APP_TOOL_FAMILIES:
        names = tutor_tool_names(app)
        assert len(names) == len(set(names)), f"{app} has duplicate tool names"


def test_families_referenced_by_apps_all_exist():
    """A typo in APP_TOOL_FAMILIES must not silently yield fewer tools."""
    known = set(_TUTOR_TOOLS_BY_FAMILY)
    for app, families in APP_TOOL_FAMILIES.items():
        for family in families:
            assert family in known, f"app '{app}' names unknown family '{family}'"


def test_allowlist_and_registered_tools_cannot_drift(tmp_path):
    """The invariant that keeps the two lists honest.

    Builds the real MCP server for each app and checks that every name on the
    tutor's allowlist is actually registered. If these drift, the model is told
    about a tool that does not exist and wastes a turn discovering that.
    """
    import asyncio

    import mcp.types as types

    from backend.episodic import EpisodicMemory
    from backend.tools import create_learning_tools

    episodic = EpisodicMemory(chroma_dir=str(tmp_path / "chroma"))

    def registered_names(server) -> set[str]:
        """Ask the server what it actually registered.

        Via the MCP list_tools handler rather than any attribute of ours, so
        this checks what the model will really be offered.
        """
        handler = server["instance"].request_handlers[types.ListToolsRequest]
        result = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
        return {t.name for t in result.root.tools}

    for app in APP_TOOL_FAMILIES:
        server = create_learning_tools(
            str(tmp_path / "user"), episodic, session_ctx={}, app=app
        )
        registered = registered_names(server)

        for name in tutor_tool_names(app):
            if name in ("WebSearch", "WebFetch"):
                continue  # built-ins, not MCP
            assert _bare(name) in registered, (
                f"{app}: allowlist names '{_bare(name)}' but it is not registered"
            )

        # The builders' tools must exist too wherever their family is enabled.
        families = app_tool_families(app)
        if "demos" in families:
            for n in DEMO_BUILDER_TOOL_NAMES:
                assert _bare(n) in registered, f"{app}: demo builder needs {_bare(n)}"
        if "derivations" in families:
            for n in DERIVATION_BUILDER_TOOL_NAMES:
                if _bare(n) == "run_code" and "code" not in families:
                    continue  # derivation plots need run_code; see the note below
                assert _bare(n) in registered, (
                    f"{app}: derivation builder needs {_bare(n)}"
                )


def test_run_agent_forwards_app_to_the_tools(monkeypatch):
    """The regression the learner hit in the running app.

    run_agent accepted `app` but did not pass it to run_agent_internal, so the
    MCP tools were always built for the default app. The Sanskrit tutor asked
    get_domain_map about its own curriculum and was told "Unknown track
    'sanskrit'" — then apologised to the learner for a limitation that did not
    exist. Silent, and only visible by reading a tool result.

    Asserts the wiring by signature rather than by running a turn: the call
    needs a live SDK subprocess, which a unit test should not require.
    """
    import inspect

    from backend import agent

    # Parse rather than slice on the first ")": a comment inside the call
    # containing a bracket broke the naive version, which is a brittle test
    # failing on a working system — the worst kind.
    import ast

    tree = ast.parse(inspect.getsource(agent.run_agent).lstrip())
    forwarded = any(
        isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "run_agent_internal"
        and any(kw.arg == "app" for kw in node.keywords)
        for node in ast.walk(tree)
    )
    assert forwarded, (
        "run_agent must forward `app` to run_agent_internal, or every tutor "
        "gets the default app's tools and curriculum"
    )


def test_every_agent_entrypoint_takes_an_app():
    """A new entrypoint that forgets `app` silently becomes PyMentor."""
    import inspect

    from backend import agent

    for name in ("run_agent", "run_agent_internal", "run_demo_builder"):
        fn = getattr(agent, name)
        assert "app" in inspect.signature(fn).parameters, (
            f"{name}() has no `app` parameter"
        )


def test_book_pages_are_not_persisted_as_attachments():
    """Book page images must never reach episodic.save_attachment.

    saved_attachments is built by looping over `attachments`. If the book's
    page images were merged into that list before the loop, every page turn
    would write a duplicate PNG into data/uploads/ and record it on the
    transcript — for images the server generated and can regenerate for free.

    Asserted by source order because exercising it needs a live SDK
    subprocess. The merge belongs at the run_agent_internal call site.
    """
    import inspect

    from backend import agent

    source = inspect.getsource(agent.run_agent)
    assert "book_images" in source, "run_agent no longer handles book_ref"
    assert source.index("saved_attachments = []") < source.index("book_images"), (
        "merge book images into `attachments` only AFTER saved_attachments is "
        "built, or every page turn duplicates a PNG into data/uploads/"
    )


def test_book_frame_is_built_after_the_session_is_resolved():
    """needs_resend keys on the session id, so it needs the RESOLVED one.

    session_id is reassigned several times above (active-session fallback,
    fork, restore-from-memory). Building the frame before that would compare
    against the caller's argument — usually None — so a new session would
    never re-send the page images, and the model would discuss a page it has
    never seen with complete confidence.
    """
    import inspect

    from backend import agent

    source = inspect.getsource(agent.run_agent)
    assert source.index("session_ctx: dict") < source.index("needs_resend("), (
        "build the book_ref frame after the session id is resolved"
    )


def test_sanskrit_can_read_a_book():
    """The reader tools are what let the tutor turn the page itself."""
    bare = {_bare(n) for n in tutor_tool_names("sanskrit")}
    for name in ("go_to_page", "advance_pages", "list_books"):
        assert name in bare, f"the Sanskrit tutor cannot {name}"


def test_pymentor_has_no_reader():
    """PyMentor teaches from runnable code, not from scans of books."""
    assert "reader" not in app_tool_families("pymentor")
    bare = {_bare(n) for n in tutor_tool_names("pymentor")}
    assert "go_to_page" not in bare
