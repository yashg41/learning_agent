"""Builder jobs read their outcome off disk, never off a tool's build_ctx.

The incident: a PCA derivation was written — 31 blocks on disk — and its job
was reported "failed: the builder finished without writing a derivation".

The tools run behind the HTTP MCP transport, where build_ctx reaches a tool as
a per-request copy rebuilt from a header (mcp_http._ctx_from_headers). The tool
recorded doc_id / wrote_blocks on that copy; the copy died with the request;
the job runner read its own dict, found nothing, and failed a finished build.
Demo jobs read demo_id, declared_parts and verified the same way, so they had
the same bug waiting.

Every fake builder below therefore hands the tools a JSON ROUND-TRIPPED COPY of
build_ctx, exactly as the transport does. A test that passed a live dict would
pass against the broken code too, which is how this shipped.

The session-id tests cover the other half of the same flaw: the header is
built before the SDK's init message, so any id learned only at init never
reached the tools — a new chat had none, a fork had its parent's.
"""

import asyncio
import json
import os

import pytest

import backend.agent as agent
from backend import demojobs, demos, derivations, mathjobs


def _as_transport(build_ctx: dict) -> dict:
    """What a tool actually receives: a copy of the inbound keys."""
    from backend.mcp_http import H_BUILD_CTX, mcp_headers

    headers = mcp_headers("u", "pymentor", "builder", build_ctx=build_ctx)
    return json.loads(headers[H_BUILD_CTX])


# --- Derivations -----------------------------------------------------------


@pytest.fixture
def deriv_store(tmp_path, monkeypatch):
    monkeypatch.setattr(derivations, "USERS_DIR", str(tmp_path))
    monkeypatch.setattr(mathjobs, "JOBS", {})
    monkeypatch.setattr(mathjobs, "TASKS", {})
    monkeypatch.setattr(mathjobs, "CANCELS", {})
    return "learner@example.com"


def _deriv_builder(monkeypatch, write=None, raise_exc=None):
    """A derivation builder that writes (or not) to the doc it was named."""
    seen = {}

    async def build(email, request, chat_session_id=None,
                    resume_builder_session=None, on_event=None, build_ctx=None):
        seen["request"] = request
        seen["ctx"] = copy = _as_transport(build_ctx)
        if write:
            derivations.append_blocks(email, copy["doc_id"], write)
        # What the old tools did — onto the copy, where it is lost.
        copy["wrote_blocks"] = True
        if raise_exc:
            raise raise_exc
        return "builder-session-1"

    monkeypatch.setattr(agent, "run_derivation_builder", build)
    return seen


def test_blocks_written_to_disk_make_the_job_ready(deriv_store, monkeypatch):
    """The regression: the incident's exact shape must now report ready."""
    _deriv_builder(monkeypatch, write=[{"kind": "text", "content": "why"}])
    record = mathjobs.new_job(deriv_store, "chat-1", "PCA")

    asyncio.run(mathjobs._run_build(record))

    job = mathjobs.JOBS[record["job_id"]]
    assert job["state"] == mathjobs.READY, job.get("error")
    doc = derivations.get_doc(deriv_store, job["doc_id"])
    assert len(doc["blocks"]) == 1
    # Was '' when the header dropped chat_session_id.
    assert doc["session_id"] == "chat-1"
    assert doc["builder_session_id"] == "builder-session-1"


def test_the_builder_is_named_the_doc_and_told_to_write_files(
        deriv_store, monkeypatch):
    seen = _deriv_builder(monkeypatch, write=[{"kind": "text", "content": "x"}])
    record = mathjobs.new_job(deriv_store, "chat-1", "PCA")

    asyncio.run(mathjobs._run_build(record))

    doc_id = mathjobs.JOBS[record["job_id"]]["doc_id"]
    assert doc_id in seen["request"]
    assert "blocks_path" in seen["request"]
    assert "derivation_open" not in seen["request"]
    assert seen["ctx"]["job_id"] == record["job_id"]


def test_nothing_written_fails_and_leaves_no_empty_doc(deriv_store, monkeypatch):
    _deriv_builder(monkeypatch, write=None)
    record = mathjobs.new_job(deriv_store, "chat-1", "PCA")

    asyncio.run(mathjobs._run_build(record))

    job = mathjobs.JOBS[record["job_id"]]
    assert job["state"] == mathjobs.FAILED
    assert "without writing" in job["error"]
    assert job["doc_id"] is None
    assert derivations.list_docs(deriv_store) == []


def test_a_crashed_builder_leaves_no_empty_doc(deriv_store, monkeypatch):
    _deriv_builder(monkeypatch, write=None, raise_exc=RuntimeError("boom"))
    record = mathjobs.new_job(deriv_store, "chat-1", "PCA")

    with pytest.raises(RuntimeError):
        asyncio.run(mathjobs._run_build(record))

    assert derivations.list_docs(deriv_store) == []


def test_a_crashed_builder_keeps_what_it_wrote(deriv_store, monkeypatch):
    _deriv_builder(monkeypatch, write=[{"kind": "text", "content": "kept"}],
                   raise_exc=RuntimeError("boom"))
    record = mathjobs.new_job(deriv_store, "chat-1", "PCA")

    with pytest.raises(RuntimeError):
        asyncio.run(mathjobs._run_build(record))

    assert len(derivations.list_docs(deriv_store)) == 1


def test_a_revision_that_changed_nothing_says_so(deriv_store, monkeypatch):
    base = derivations.create_doc(deriv_store, "PCA")
    derivations.append_blocks(base_email := deriv_store, base["id"],
                              [{"kind": "text", "content": "v1"}])
    _deriv_builder(monkeypatch, write=None)
    record = mathjobs.new_job(base_email, "chat-1", "PCA", base_doc_id=base["id"])

    asyncio.run(mathjobs._run_build(record))

    job = mathjobs.JOBS[record["job_id"]]
    assert job["state"] == mathjobs.FAILED
    assert "no change" in job["error"]
    # The existing doc is never deleted for a declined revision.
    assert derivations.get_doc(deriv_store, base["id"]) is not None


def test_a_revision_that_appended_is_ready(deriv_store, monkeypatch):
    base = derivations.create_doc(deriv_store, "PCA")
    derivations.append_blocks(deriv_store, base["id"],
                              [{"kind": "text", "content": "v1"}])
    _deriv_builder(monkeypatch, write=[{"kind": "text", "content": "v2"}])
    record = mathjobs.new_job(deriv_store, "chat-1", "PCA", base_doc_id=base["id"])

    asyncio.run(mathjobs._run_build(record))

    job = mathjobs.JOBS[record["job_id"]]
    assert job["state"] == mathjobs.READY
    assert job["doc_id"] == base["id"]


# --- derivation_write reads a file -----------------------------------------


def _call_tool(udir, build_ctx, name, arguments, session_ctx=None):
    """Through the real MCP request path, as test_demo_builder_session does."""
    import mcp.types as types
    from backend.tools import create_learning_tools

    os.makedirs(udir, exist_ok=True)
    server = create_learning_tools(udir, None, session_ctx, build_ctx, "pymentor")
    caller = server["instance"].request_handlers[types.CallToolRequest]

    async def go():
        return await caller(types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name=name, arguments=arguments),
        ))

    result = asyncio.run(go()).root
    return result.content[0].text, bool(result.isError)


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    import backend.sandbox as sandbox

    root = tmp_path / "scratch"
    root.mkdir()
    monkeypatch.setattr(sandbox, "SCRATCH_ROOT", root)
    return root


def test_derivation_write_reads_the_wave_from_a_file(deriv_store, scratch):
    doc = derivations.create_doc(deriv_store, "PCA")
    wave = scratch / "wave1.json"
    wave.write_text(json.dumps([
        {"kind": "latex", "content": "\\frac{a}{b}"},
        {"kind": "text", "content": "why"},
    ]))
    ctx = {"doc_id": doc["id"]}

    out, is_error = _call_tool(
        os.path.join(derivations.USERS_DIR, deriv_store), ctx,
        "derivation_write", {"doc_id": doc["id"], "blocks_path": str(wave)},
    )

    assert '"status": "written"' in out, out
    blocks = derivations.get_doc(deriv_store, doc["id"])["blocks"]
    assert [b["kind"] for b in blocks] == ["latex", "text"]
    assert blocks[0]["content"] == "\\frac{a}{b}"
    # Reports nothing back through build_ctx — the runner reads the doc.
    assert ctx == {"doc_id": doc["id"]}


def test_derivation_write_refuses_a_path_outside_scratch(
        deriv_store, scratch, tmp_path):
    doc = derivations.create_doc(deriv_store, "PCA")
    outside = tmp_path / "elsewhere.json"
    outside.write_text("[]")

    out, is_error = _call_tool(
        os.path.join(derivations.USERS_DIR, deriv_store), {},
        "derivation_write", {"doc_id": doc["id"], "blocks_path": str(outside)},
    )

    # The in-process SDK server does not carry is_error into isError (see
    # agent.py's prefix check), so the refusal is asserted on its text.
    assert out.startswith("Derivation rejected:")
    assert "scratch directory" in out


def test_derivation_write_refuses_json_that_is_not_a_list(deriv_store, scratch):
    doc = derivations.create_doc(deriv_store, "PCA")
    wave = scratch / "wave1.json"
    wave.write_text(json.dumps({"kind": "text", "content": "x"}))

    out, is_error = _call_tool(
        os.path.join(derivations.USERS_DIR, deriv_store), {},
        "derivation_write", {"doc_id": doc["id"], "blocks_path": str(wave)},
    )

    assert out.startswith("Derivation rejected:")
    assert "JSON array" in out
    assert derivations.get_doc(deriv_store, doc["id"])["blocks"] == []


def test_derivation_open_is_gone():
    """The runner owns the doc; a builder must not be able to open a second."""
    assert not any("derivation_open" in n for n in agent.DERIVATION_BUILDER_TOOL_NAMES)


def test_derivation_builder_may_draft_files_but_never_shell_out():
    assert agent.BUILDER_DENIED_TOOL_NAMES is agent.DEMO_BUILDER_DENIED_TOOL_NAMES
    for gained in ("Write", "Read"):
        assert gained not in agent.BUILDER_DENIED_TOOL_NAMES
    for forbidden in ("Bash", "Task"):
        assert forbidden in agent.BUILDER_DENIED_TOOL_NAMES


# --- Demos -----------------------------------------------------------------

HTML = (
    "<!doctype html><html><body><div id='out'></div>"
    "<script>function go(){document.getElementById('out').textContent='x';}"
    "go();</script></body></html>"
)
SHELL = (
    "<!doctype html><html><head><style>.panel-card{}</style></head><body>\n"
    + demos.PARTS_MARKER
    + "\n<script>function show(i){}\nshow(0);</script></body></html>"
)


@pytest.fixture
def demo_store(tmp_path, monkeypatch):
    monkeypatch.setattr(demos, "USERS_DIR", str(tmp_path))
    monkeypatch.setattr(demojobs, "JOBS", {})
    monkeypatch.setattr(demojobs, "TASKS", {})
    monkeypatch.setattr(demojobs, "CANCELS", {})
    return "learner@example.com"


def _demo_builder(monkeypatch, act):
    """A demo builder whose tools only ever see a transport copy."""

    async def build(email, request, chat_session_id=None,
                    resume_builder_session=None, on_event=None,
                    build_ctx=None, app="pymentor"):
        copy = _as_transport(build_ctx)
        act(email, copy)
        return "builder-session-1"

    monkeypatch.setattr(agent, "run_demo_builder", build)


def test_a_saved_demo_is_found_by_its_job_stamp(demo_store, monkeypatch):
    def act(email, ctx):
        meta = demos.save_demo(email, "Ring", HTML, job_id=ctx["job_id"],
                               chat_session_id=ctx.get("chat_session_id", ""))
        demos.set_demo_verified(email, meta["id"], True)
        ctx["demo_id"] = meta["id"]          # lost, as over HTTP

    _demo_builder(monkeypatch, act)
    record = demojobs.new_job(demo_store, "chat-1", "Ring", app="pymentor")

    asyncio.run(demojobs._run_build(record))

    job = demojobs.JOBS[record["job_id"]]
    assert job["state"] == demojobs.READY, job.get("error")
    meta = demos.get_demo_meta(demo_store, job["demo_id"])
    assert meta["job_id"] == record["job_id"]
    assert meta["chat_session_id"] == "chat-1"
    assert job["verified"] is True


def test_a_demo_from_another_job_is_not_claimed(demo_store, monkeypatch):
    demos.save_demo(demo_store, "Old", HTML, job_id="j_someoneelse")
    _demo_builder(monkeypatch, lambda email, ctx: None)
    record = demojobs.new_job(demo_store, "chat-1", "Ring", app="pymentor")

    asyncio.run(demojobs._run_build(record))

    job = demojobs.JOBS[record["job_id"]]
    assert job["state"] == demojobs.FAILED
    assert "without saving" in job["error"]


def test_an_edit_stamped_by_this_job_is_ready(demo_store, monkeypatch):
    base = demos.save_demo(demo_store, "Ring", HTML)

    def act(email, ctx):
        demos.update_demo(email, base["id"], html=HTML.replace("x", "y"),
                          job_id=ctx["job_id"])

    _demo_builder(monkeypatch, act)
    record = demojobs.new_job(demo_store, "chat-1", "Ring",
                              base_demo_id=base["id"], app="pymentor")

    asyncio.run(demojobs._run_build(record))

    job = demojobs.JOBS[record["job_id"]]
    assert job["state"] == demojobs.READY, job.get("error")
    assert job["demo_id"] == base["id"]
    assert demos.get_demo_meta(demo_store, base["id"])["version"] == 2


def test_an_edit_nobody_made_is_not_ready(demo_store, monkeypatch):
    base = demos.save_demo(demo_store, "Ring", HTML)
    _demo_builder(monkeypatch, lambda email, ctx: None)
    record = demojobs.new_job(demo_store, "chat-1", "Ring",
                              base_demo_id=base["id"], app="pymentor")

    asyncio.run(demojobs._run_build(record))

    job = demojobs.JOBS[record["job_id"]]
    assert job["state"] == demojobs.FAILED
    assert demos.get_demo_meta(demo_store, base["id"])["version"] == 1


def test_declared_panels_on_disk_drive_the_fan_out(demo_store, monkeypatch):
    """declared_parts used to ride build_ctx, so over HTTP no panel was built."""
    declared = [{"name": f"panel{i}", "spec": f"item {i}"} for i in (1, 2)]

    def act(email, ctx):
        demos.open_demo(email, "Shapes", SHELL, job_id=ctx["job_id"],
                        declared_parts=declared)

    prompts = []

    async def part_builder(email, request, build_ctx=None, app="pymentor"):
        prompts.append(request)
        fields = dict(l.split(":", 1) for l in request.splitlines() if ":" in l)
        demos.save_demo_part(email, fields["demo_id"].strip(),
                             int(fields["index"]), fields["name"].strip(),
                             '<div class="panel-card">P</div>')

    _demo_builder(monkeypatch, act)
    monkeypatch.setattr(agent, "run_demo_part_builder", part_builder)
    record = demojobs.new_job(demo_store, "chat-1", "Shapes", app="pymentor")

    asyncio.run(demojobs._run_build(record))

    job = demojobs.JOBS[record["job_id"]]
    assert job["state"] == demojobs.READY, job.get("error")
    assert demos.get_demo_meta(demo_store, job["demo_id"])["part_count"] == 2
    # The shell contract reaches the panels from disk, not build_ctx.
    assert all("panel-card" in p for p in prompts)


def test_demo_tools_write_nothing_into_build_ctx(demo_store, scratch):
    ctx ={"job_id": "j_test", "chat_session_id": "chat-1"}
    draft = scratch / "demo.html"
    draft.write_text(HTML)
    udir = os.path.join(demos.USERS_DIR, demo_store)

    out, is_error = _call_tool(udir, ctx, "save_demo",
                               {"title": "Ring", "html_path": str(draft)})
    assert not is_error, out
    demo_id = json.loads(out)["demo_id"]

    out, is_error = _call_tool(udir, ctx, "demo_verify", {"demo_id": demo_id})
    assert not is_error, out

    assert ctx == {"job_id": "j_test", "chat_session_id": "chat-1"}
    meta = demos.get_demo_meta(demo_store, demo_id)
    assert meta["job_id"] == "j_test"
    assert meta["chat_session_id"] == "chat-1"
    assert meta["verified"] is True


# --- The session id is known before the headers are built -------------------


def _capture_run(monkeypatch, **kwargs):
    """Run run_agent_internal with a query that captures its options."""
    from backend.config import settings

    monkeypatch.setattr(settings, "MCP_TRANSPORT", "http")
    captured = {}

    async def fake_query(prompt, options):
        captured["options"] = options
        return
        yield  # an async generator that yields nothing

    monkeypatch.setattr(agent, "query", fake_query)
    asyncio.run(agent.run_agent_internal(prompt="hi", user_data_dir="/tmp/u", **kwargs))
    opts = captured["options"]
    headers = opts.mcp_servers["learning-tools"]["headers"]
    return opts, headers


def test_a_new_chat_tells_its_tools_the_session_it_will_have(monkeypatch):
    from backend.mcp_http import H_SESSION

    session_ctx = {"session_id": None}
    opts, headers = _capture_run(monkeypatch, session_ctx=session_ctx)

    sid = opts.extra_args["session-id"]
    assert sid
    assert headers[H_SESSION] == sid == session_ctx["session_id"]


def test_a_fork_does_not_carry_its_parents_id(monkeypatch):
    from backend.mcp_http import H_SESSION

    session_ctx = {"session_id": "parent-session"}
    opts, headers = _capture_run(
        monkeypatch, session_id="parent-session", fork_session=True,
        session_ctx=session_ctx,
    )

    sid = opts.extra_args["session-id"]
    assert sid != "parent-session"
    assert headers[H_SESSION] == sid
    assert opts.resume == "parent-session"


def test_a_plain_resume_keeps_its_id_and_passes_no_flag(monkeypatch):
    from backend.mcp_http import H_SESSION

    session_ctx = {"session_id": "stale-id"}
    opts, headers = _capture_run(
        monkeypatch, session_id="live-session", session_ctx=session_ctx,
    )

    assert "session-id" not in (opts.extra_args or {})
    # The resumed id, even when session_ctx held another (the overflow retry).
    assert headers[H_SESSION] == "live-session"


def test_a_builders_tools_know_its_session(monkeypatch):
    from backend.mcp_http import H_BUILD_CTX

    build_ctx = {"job_id": "j_1", "chat_session_id": "chat-1"}
    opts, headers = _capture_run(
        monkeypatch, session_id="chat-1", fork_session=True, build_ctx=build_ctx,
    )

    sent = json.loads(headers[H_BUILD_CTX])
    assert sent["builder_session_id"] == opts.extra_args["session-id"]
    assert sent["job_id"] == "j_1"
    assert sent["chat_session_id"] == "chat-1"
