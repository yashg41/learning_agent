"""Panels of one demo are built concurrently, then the demo is finalized.

The orchestrator writes the shell and declares the panels; the job runner
builds them. They run inside ONE job on purpose — spawning them as separate
jobs would meet MAX_CONCURRENT_DEMO_JOBS, which is a per-user gate, and
serialise the fan-out two at a time.
"""

import asyncio

import pytest

from backend import demojobs, demos


SHELL = (
    "<!doctype html><html><head><style>.p{}</style></head><body>\n"
    + demos.PARTS_MARKER
    + "\n<script>function show(i){}\nshow(0);</script></body></html>"
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(demos, "USERS_DIR", str(tmp_path))
    monkeypatch.setattr(demojobs, "JOBS", {})
    monkeypatch.setattr(demojobs, "TASKS", {})
    monkeypatch.setattr(demojobs, "CANCELS", {})
    return "learner@example.com"


def _job(email: str) -> dict:
    record = demojobs.new_job(email, None, "Shapes", app="pymentor")
    demojobs.JOBS[record["job_id"]] = record
    return record


def _ctx(demo_id: str) -> dict:
    return {
        "chat_session_id": "",
        "builder_session_id": "",
        "demo_id": demo_id,
        "shell_contract": SHELL,
    }


def _declare(n: int) -> list[dict]:
    return [{"name": f"panel{i}", "spec": f"show item {i}"} for i in range(1, n + 1)]


def _fake_builder(email_store, demo_id, monkeypatch, *, fail_on=()):
    """Stand in for a part-builder agent: write the panel it was asked for."""
    import backend.agent as agent

    seen = {"peak": 0, "live": 0}

    async def build(email, request, build_ctx=None, app="pymentor"):
        fields = dict(
            line.split(":", 1) for line in request.splitlines() if ":" in line
        )
        index = int(fields["index"].strip())
        name = fields["name"].strip()
        seen["live"] += 1
        seen["peak"] = max(seen["peak"], seen["live"])
        try:
            await asyncio.sleep(0.01)
            if index in fail_on:
                raise RuntimeError("panel failed")
            demos.save_demo_part(
                email, demo_id, index, name, f'<div class="p">P{index}</div>'
            )
        finally:
            seen["live"] -= 1

    monkeypatch.setattr(agent, "run_demo_part_builder", build)
    return seen


def test_every_declared_panel_is_written_and_ordered(store, monkeypatch):
    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]
    _fake_builder(store, demo_id, monkeypatch)
    record = _job(store)

    failed = asyncio.run(demojobs._build_parts(record, _ctx(demo_id), _declare(10)))

    assert failed == []
    html = demos.get_demo(store, demo_id)["html"]
    positions = [html.index(f"P{i}<") for i in range(1, 11)]
    assert positions == sorted(positions)


def test_panels_run_concurrently_but_bounded(store, monkeypatch):
    """Several at once — that is the point — but never unbounded."""
    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]
    seen = _fake_builder(store, demo_id, monkeypatch)
    record = _job(store)

    asyncio.run(demojobs._build_parts(record, _ctx(demo_id), _declare(10)))

    assert seen["peak"] > 1, "panels ran one at a time; the fan-out did nothing"
    assert seen["peak"] <= demojobs.MAX_CONCURRENT_PARTS


def test_a_failed_panel_does_not_lose_the_others(store, monkeypatch):
    """Nine panels that render beat ten that do not."""
    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]
    _fake_builder(store, demo_id, monkeypatch, fail_on=(2, 4))
    record = _job(store)

    failed = asyncio.run(demojobs._build_parts(record, _ctx(demo_id), _declare(5)))

    assert sorted(failed) == ["panel2", "panel4"]
    assert len(demos.list_demo_parts(store, demo_id)) == 3
    # Still a real, renderable demo.
    assert demos.get_demo(store, demo_id) is not None
    assert demojobs.JOBS[record["job_id"]]["state"] != demojobs.FAILED


def test_all_panels_failing_fails_the_job_with_a_reason(store, monkeypatch):
    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]
    _fake_builder(store, demo_id, monkeypatch, fail_on=(1, 2, 3))
    record = _job(store)

    result = asyncio.run(demojobs._build_parts(record, _ctx(demo_id), _declare(3)))

    assert result is None, "caller must stop; the job is already terminal"
    job = demojobs.JOBS[record["job_id"]]
    assert job["state"] == demojobs.FAILED
    assert "panel1" in job["error"]
    # Never published: an empty demo must not reach the learner's list.
    assert demo_id not in [d["id"] for d in demos.list_demos(store)]


def test_each_panel_gets_its_own_build_ctx(store, monkeypatch):
    """Sharing one dict across concurrent panels would race on demo_id."""
    import backend.agent as agent

    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]
    # Hold the dicts themselves, not their id()s: a freed dict's address is
    # reused by the next one, so identity by id() would report a false clash.
    contexts = []

    async def build(email, request, build_ctx=None, app="pymentor"):
        contexts.append(build_ctx)
        index = int(
            [l for l in request.splitlines() if l.startswith("index:")][0].split(":")[1]
        )
        # Write through the ctx the way the real tool does, so a shared dict
        # would show up as panels overwriting each other's marker.
        build_ctx["panel"] = index
        demos.save_demo_part(email, demo_id, index, f"p{index}", f"<div>P{index}</div>")

    monkeypatch.setattr(agent, "run_demo_part_builder", build)
    asyncio.run(demojobs._build_parts(_job(store), _ctx(demo_id), _declare(4)))

    assert len(contexts) == 4
    # Four distinct objects, each still holding its own panel's value.
    assert len({id(c) for c in contexts}) == 4, "panels shared a build_ctx"
    assert sorted(c["panel"] for c in contexts) == [1, 2, 3, 4]


def test_part_builder_is_told_the_shell_contract(store, monkeypatch):
    """Its author sees the spec and the shell's NAMES, never the conversation.

    The names, not the shell itself: sending the whole shell put the same
    8-20KB into each of N prompts, and each prompt then exceeded what the
    transport reliably carries. A panel author needs to know which classes and
    helpers exist, which is what the digest carries.
    """
    import backend.agent as agent

    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]
    prompts = []

    async def build(email, request, build_ctx=None, app="pymentor"):
        prompts.append(request)
        demos.save_demo_part(email, demo_id, 1, "p1", "<div>P1</div>")

    monkeypatch.setattr(agent, "run_demo_part_builder", build)
    asyncio.run(demojobs._build_parts(_job(store), _ctx(demo_id), _declare(1)))

    assert demo_id in prompts[0]
    assert "show item 1" in prompts[0]
    # The shell's class and function names reach the author...
    assert "p" in prompts[0] and "show" in prompts[0]
    # ...but not the shell's raw markup.
    assert "<!doctype" not in prompts[0].lower()
    assert demos.PARTS_MARKER not in prompts[0]


def test_output_overflow_is_recognised_and_reported(store):
    """An over-long reply must not be reported as "finished without saving".

    The API discards a reply that exceeds the output-token maximum and says so
    as ordinary assistant text, so the build looks like it simply stopped. That
    is what hid the original failure: the job said the builder "finished
    without saving a demo", and the tutor, having no way to see a failed build,
    told the learner it was still building.
    """
    from backend.agent import _is_context_overflow, _is_output_overflow

    real = (
        "API Error: Claude's response exceeded the 32000 output token maximum. "
        "To configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS "
        "environment variable."
    )
    assert _is_output_overflow(real)
    # Distinct from the INPUT-side overflow, which has a different fix
    # (replay from memory, not split the demo).
    assert not _is_context_overflow(Exception(real))

    assert not _is_output_overflow("Prompt is too long")
    assert not _is_output_overflow("Here is your demo.")
    assert not _is_output_overflow("")
