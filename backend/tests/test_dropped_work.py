"""Work survives when the tool channel dies.

`Stream closed` means the CLI could not deliver the call to this process, so
the handler never ran. The agent keeps working and finishes its task; only the
*saving* fails. Three consequences, all seen in one week:

- Four demos were rescued by hand out of the builder's scratch directory. Of
  the drafts on disk at the time, 13 of 13 passed validation — a failed build
  almost always means a finished demo that could not be published.
- `columnar_olap_databases` should have moved to `practiced` with detailed
  notes; the store still read `introduced`, 1 review. Eight such writes were
  recoverable from a single transcript, each still carrying its arguments.
- The UI reported all of it, plus two unrelated outcomes, as
  "no change was made — describe what to change about the demo", which blamed
  the learner for a transport fault and for the builder correctly declining.

Nothing here prevents the channel dying. It makes the failure survivable.
"""

import asyncio
import json
import os

import pytest

import backend.agent as agent
from backend import demojobs, demos


HTML = (
    "<!doctype html><html><body><div id='out'></div>"
    "<script>function go(){document.getElementById('out').textContent='x';}"
    "go();</script></body></html>"
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(demos, "USERS_DIR", str(tmp_path))
    # A draft is only publishable from inside the scratch root — see
    # demos.read_builder_html. Point that root at tmp_path so the fixture's
    # drafts are legitimate rather than working around the check.
    monkeypatch.setattr("backend.sandbox.SCRATCH_ROOT", tmp_path)
    monkeypatch.setattr(demojobs, "JOBS", {})
    monkeypatch.setattr(demojobs, "TASKS", {})
    monkeypatch.setattr(demojobs, "CANCELS", {})
    return "u"


def _job(email: str) -> dict:
    record = demojobs.new_job(email, None, "demo", app="pymentor")
    demojobs.JOBS[record["job_id"]] = record
    return record


# --- Rescuing the draft a dead channel left behind -------------------------


def test_a_finished_draft_is_published_instead_of_lost(store, tmp_path):
    """The four manual rescues, automated.

    The builder had done the work; only the call carrying it failed. The
    document was sitting in scratch the whole time.
    """
    meta = demos.save_demo(store, "demo", html=HTML)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "draft.html").write_text(HTML.replace("'x'", "'RESCUED'"))

    record = _job(store)
    rescued = demojobs._rescue_draft(
        record, {"scratch_dir": str(scratch)}, meta["id"], "b1"
    )

    assert rescued is True
    job = demojobs.JOBS[record["job_id"]]
    assert job["state"] == demojobs.READY
    assert "connection" in job["note"], "the learner should be told why"
    assert "'RESCUED'" in demos.get_demo(store, meta["id"])["html"]


def test_a_broken_draft_never_overwrites_a_working_demo(store, tmp_path):
    """The safety property. Publish only what save_demo would have accepted."""
    meta = demos.save_demo(store, "demo", html=HTML)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "draft.html").write_text("<div>not a document</div>")

    record = _job(store)
    rescued = demojobs._rescue_draft(
        record, {"scratch_dir": str(scratch)}, meta["id"], "b1"
    )

    assert rescued is False
    # The demo the learner already had is untouched.
    assert "'x'" in demos.get_demo(store, meta["id"])["html"]


def test_no_draft_means_no_rescue(store, tmp_path):
    meta = demos.save_demo(store, "demo", html=HTML)
    empty = tmp_path / "empty"
    empty.mkdir()

    assert demojobs._rescue_draft(_job(store), {"scratch_dir": str(empty)}, meta["id"], "") is False
    assert demojobs._rescue_draft(_job(store), {}, meta["id"], "") is False
    assert demojobs._rescue_draft(_job(store), {"scratch_dir": str(empty)}, None, "") is False


def test_the_newest_draft_wins(store, tmp_path):
    """A build may leave several drafts; the last one is the finished one."""
    import time

    meta = demos.save_demo(store, "demo", html=HTML)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "a.html").write_text(HTML.replace("'x'", "'OLD'"))
    time.sleep(0.01)
    (scratch / "b.html").write_text(HTML.replace("'x'", "'NEW'"))

    demojobs._rescue_draft(_job(store), {"scratch_dir": str(scratch)}, meta["id"], "")
    assert "'NEW'" in demos.get_demo(store, meta["id"])["html"]


# --- Replaying knowledge writes the transport dropped ----------------------


def _transcript(path, calls):
    """A CLI transcript where each call failed with Stream closed."""
    lines = []
    for i, (name, args) in enumerate(calls):
        lines.append(json.dumps({
            "message": {"content": [
                {"type": "tool_use", "id": f"t{i}",
                 "name": f"mcp__learning-tools__{name}", "input": args}
            ]}
        }))
        lines.append(json.dumps({
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"t{i}",
                 "is_error": True, "content": "Stream closed"}
            ]}
        }))
    path.write_text("\n".join(lines))


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A transcript on disk plus a knowledge store to replay into."""
    monkeypatch.setattr(agent, "_get_user_data_dir", lambda email: str(tmp_path / "u"))
    (tmp_path / "u").mkdir()

    path = tmp_path / "sess.jsonl"
    monkeypatch.setattr("backend.transcripts._claude_path", lambda sid: str(path))
    return path


def test_a_dropped_concept_write_is_re_applied(session, tmp_path):
    """The write that was actually lost: introduced -> practiced, with notes."""
    from backend.knowledge import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "u"), app="pymentor")
    store.upsert_concept("columnar_olap", "Columnar", "sd_data", "introduced")

    _transcript(session, [(
        "update_concept",
        {"concept_id": "columnar_olap", "name": "Columnar",
         "category": "sd_data", "mastery": "practiced",
         "notes": "Resolved with verified code."},
    )])

    assert agent.replay_dropped_writes("u@example.com", "sess") == 1

    concept = KnowledgeStore(str(tmp_path / "u"), app="pymentor").load_knowledge()
    assert concept["concepts"]["columnar_olap"]["mastery"] == "practiced"
    assert "verified code" in concept["concepts"]["columnar_olap"]["notes"]


def test_only_idempotent_writes_are_replayed(session, tmp_path):
    """Replaying everything would start builds and re-run code.

    update_concept is an upsert keyed on concept_id, so applying it twice is a
    no-op. request_demo and run_code are not, and must be left alone however
    tempting it is to "recover" them too.
    """
    _transcript(session, [
        ("request_demo", {"title": "x", "request": "build something"}),
        ("run_code", {"code": "print('side effect')"}),
        ("update_concept", {"concept_id": "safe_one", "name": "Safe",
                            "category": "sd_data", "mastery": "introduced"}),
    ])

    assert agent.replay_dropped_writes("u@example.com", "sess") == 1

    from backend.knowledge import KnowledgeStore

    concepts = KnowledgeStore(str(tmp_path / "u"), app="pymentor").load_knowledge()["concepts"]
    assert "safe_one" in concepts

    # Assert the allowlist itself, not just today's behaviour. The replay loop
    # happens to be safe by construction as well (it keys on concept_id), so
    # widening the allowlist alone changes nothing observable — which means
    # without this the guard could be removed and no test would notice.
    assert agent._REPLAYABLE_TOOLS == {"update_concept"}, (
        "only idempotent writes may be replayed: request_demo would start "
        "another build, run_code would re-run it"
    )


def test_a_retried_concept_is_applied_once(session, tmp_path):
    """The model retries against a dead channel, so the same write appears
    several times. The last one wins; it must not be applied N times."""
    calls = [(
        "update_concept",
        {"concept_id": "vec", "name": "Vectors", "category": "sd_data",
         "mastery": "practiced", "notes": f"attempt {i}"},
    ) for i in range(4)]
    _transcript(session, calls)

    assert agent.replay_dropped_writes("u@example.com", "sess") == 1

    from backend.knowledge import KnowledgeStore

    concept = KnowledgeStore(str(tmp_path / "u"), app="pymentor") \
        .load_knowledge()["concepts"]["vec"]
    assert concept["notes"] == "attempt 3", "the last write should win"
    assert concept["review_count"] == 1


def test_successful_calls_are_not_replayed(session, tmp_path):
    """Only calls that failed with Stream closed. A write that landed must not
    be applied a second time."""
    session.write_text("\n".join([
        json.dumps({"message": {"content": [
            {"type": "tool_use", "id": "t0",
             "name": "mcp__learning-tools__update_concept",
             "input": {"concept_id": "fine", "name": "Fine",
                       "category": "sd_data", "mastery": "mastered"}}
        ]}}),
        json.dumps({"message": {"content": [
            {"type": "tool_result", "tool_use_id": "t0", "content": "ok"}
        ]}}),
    ]))

    assert agent.replay_dropped_writes("u@example.com", "sess") == 0


def test_a_missing_transcript_is_not_an_error(session):
    """This runs after the learner's turn has finished — it must never raise."""
    assert not session.exists(), "the fixture should not have created it"
    assert agent.replay_dropped_writes("u@example.com", "sess") == 0


def test_replay_is_bounded(session, tmp_path):
    """A badly broken channel must not turn into an unbounded write loop."""
    _transcript(session, [(
        "update_concept",
        {"concept_id": f"c{i}", "name": f"C{i}", "category": "sd_data",
         "mastery": "introduced"},
    ) for i in range(40)])

    assert agent.replay_dropped_writes("u@example.com", "sess") <= agent._MAX_REPLAYED_WRITES


# --- Saying which of three things happened ---------------------------------


def _classify(store, monkeypatch, build_ctx_extra, base_demo_id):
    """Run _run_build's tail with a stubbed builder, return the job record."""
    import asyncio

    import backend.agent as agent_mod

    meta = demos.save_demo(store, "demo", html=HTML)
    record = demojobs.new_job(store, None, "demo", base_demo_id=meta["id"])
    demojobs.JOBS[record["job_id"]] = record

    async def fake_builder(email, request, chat_session_id=None,
                           resume_builder_session=None, on_event=None,
                           build_ctx=None, app="pymentor"):
        # The builder ran and wrote nothing; the caller decides what that meant.
        build_ctx.update(build_ctx_extra)
        return "sess-1"

    monkeypatch.setattr(agent_mod, "run_demo_builder", fake_builder)
    asyncio.run(demojobs._run_build(record))
    return demojobs.JOBS[record["job_id"]], meta["id"]


def test_a_dropped_connection_is_not_blamed_on_the_learner(store, monkeypatch):
    """It used to say "no change was made — describe what to change"."""
    job, _ = _classify(store, monkeypatch, {"transport_failed": True}, True)

    assert job["state"] == demojobs.FAILED
    assert "connection" in job["error"]
    assert "describe what to change" not in job["error"]


def test_nothing_to_do_is_not_a_failure(store, monkeypatch):
    """The builder looked, found the demo already did it, and declined.

    Reporting that as a red failure taught the learner to distrust a correct
    answer — it happened on a real edit where the requested legend already
    existed.
    """
    job, _ = _classify(store, monkeypatch, {"ran_tools": True}, True)

    assert job["state"] == demojobs.READY
    assert job.get("error") is None
    assert "already" in job["note"]


def test_a_question_gets_told_where_questions_go(store, monkeypatch):
    """No tool was reached for at all, which is what a question produces."""
    job, _ = _classify(store, monkeypatch, {}, True)

    assert job["state"] == demojobs.FAILED
    assert "instruction, not a question" in job["error"]
    assert "Ask tutor" in job["error"]


def test_replay_runs_even_when_the_turn_raises(session, monkeypatch):
    """The recovery must not depend on the turn finishing.

    It used to sit after the bookkeeping at the end of run_agent(), outside any
    try, so an exception anywhere in the turn skipped it — and a turn that dies
    mid-flight is precisely the one whose writes went missing. Four concepts
    had to be replayed by hand before anyone noticed the recovery was on the
    happy path only.
    """
    session.write_text(json.dumps({"message": {"content": [
        {"type": "tool_use", "id": "t0",
         "name": "mcp__learning-tools__update_concept",
         "input": {"concept_id": "lost_in_a_failed_turn", "name": "Lost",
                   "category": "sd_data", "mastery": "practiced"}},
    ]}}) + "\n" + json.dumps({"message": {"content": [
        {"type": "tool_result", "tool_use_id": "t0",
         "is_error": True, "content": "Stream closed"},
    ]}}))

    replayed = []
    monkeypatch.setattr(
        agent, "replay_dropped_writes",
        lambda email, sid, app="pymentor": replayed.append(sid),
    )

    async def explode(**kwargs):
        # The session id reaches session_ctx before the failure, exactly as it
        # does in a real run — the SDK reports it on the init event, long
        # before whatever later kills the turn.
        kwargs["session_ctx"]["session_id"] = "sess"
        raise RuntimeError("turn died mid-flight")

    class _Stub:
        def format_for_system_prompt(self, *a, **k):
            return ""

    monkeypatch.setattr(agent, "run_agent_internal", explode)
    monkeypatch.setattr(agent, "_get_episodic", _Stub)
    monkeypatch.setattr(agent, "KnowledgeStore", lambda *a, **k: _Stub())

    with pytest.raises(RuntimeError, match="turn died mid-flight"):
        asyncio.run(agent.run_agent("hi", "u@example.com"))

    assert replayed == ["sess"], "recovery must run on the failure path too"
