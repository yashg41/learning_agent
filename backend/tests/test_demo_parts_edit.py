"""Editing a multi-panel demo must change what the learner sees.

The incident: a PCA demo built as a shell plus eight panels was edited ("show
the raw data also"). The job reported ready, the demo's meta said v2, and the
learner saw no change. update_demo wrote the edited page to <demo_id>.html,
but get_demo serves any demo that has parts/ by assembling the parts — so the
edit was saved and never served. demo_verify reads through get_demo too, so it
checked v1 and passed it; every signal said "done".
"""

import asyncio
import os

import pytest

from backend import demojobs, demos

SHELL = (
    "<!doctype html><html><head><style>.p{}</style></head><body>\n"
    + demos.PARTS_MARKER
    + "\n<script>function show(i){}\nshow(0);</script></body></html>"
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    import backend.sandbox as sandbox

    monkeypatch.setattr(demos, "USERS_DIR", str(tmp_path))
    # Drafts are only read from under the scratch root; the tests draft in
    # tmp_path/s, so that is the root.
    monkeypatch.setattr(sandbox, "SCRATCH_ROOT", tmp_path)
    monkeypatch.setattr(demojobs, "JOBS", {})
    monkeypatch.setattr(demojobs, "TASKS", {})
    monkeypatch.setattr(demojobs, "CANCELS", {})
    return "learner@example.com"


def _panel_demo(email):
    meta = demos.open_demo(email, "PCA", SHELL, declared_parts=[
        {"name": "raw", "spec": "raw data"}, {"name": "scaled", "spec": "scaled"},
    ])
    demos.save_demo_part(email, meta["id"], 1, "raw", '<div class="p">RAW-V1</div>')
    demos.save_demo_part(email, meta["id"], 2, "scaled", '<div class="p">SCALED</div>')
    demos.finalize_demo(email, meta["id"])
    return meta["id"]


def _edited(email, demo_id, scratch):
    """What a builder does: check out, patch, publish by path."""
    path = demos.copy_demo_to_scratch(email, demo_id, str(scratch))
    with open(path, encoding="utf-8") as f:
        html = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(html.replace("RAW-V1", "RAW-V2 with the first 10 rows"))
    return path


def test_an_edit_to_a_panel_demo_is_what_gets_served(store, tmp_path):
    demo_id = _panel_demo(store)

    demos.update_demo(store, demo_id, html_path=_edited(store, demo_id, tmp_path / "s"))

    served = demos.get_demo(store, demo_id)
    assert "RAW-V2 with the first 10 rows" in served["html"]
    assert "RAW-V1" not in served["html"]
    assert served["version"] == 2


def test_the_edit_keeps_every_other_panel(store, tmp_path):
    """The checkout is the assembled page, so the edit carries all panels."""
    demo_id = _panel_demo(store)

    demos.update_demo(store, demo_id, html_path=_edited(store, demo_id, tmp_path / "s"))

    assert "SCALED" in demos.get_demo(store, demo_id)["html"]


def test_checkout_of_a_panel_demo_is_the_assembled_page(store, tmp_path):
    """It used to copy the flat file, which a panel demo does not have."""
    demo_id = _panel_demo(store)

    path = demos.copy_demo_to_scratch(store, demo_id, str(tmp_path / "s"))

    with open(path, encoding="utf-8") as f:
        assert f.read() == demos.get_demo(store, demo_id)["html"]


def test_the_panels_are_retired_not_lost(store, tmp_path):
    demo_id = _panel_demo(store)

    demos.update_demo(store, demo_id, html_path=_edited(store, demo_id, tmp_path / "s"))

    assert not demos.has_parts(store, demo_id)
    retired = demos._retired_parts_dir(store, demo_id)
    assert os.path.isfile(os.path.join(retired, "01_raw.html"))
    meta = demos.get_demo_meta(store, demo_id)
    for key in ("parts", "part_count", "declared_parts"):
        assert key not in meta, f"{key} would make the runner treat it as split"
    assert any(e["id"] == demo_id for e in demos.list_demos(store))


def test_verify_checks_the_version_that_is_served(store, tmp_path):
    """demo_verify reads get_demo; after the edit that must be the edit."""
    demo_id = _panel_demo(store)
    path = _edited(store, demo_id, tmp_path / "s")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(html.replace("</body>", "<script>go();</script></body>"))

    demos.update_demo(store, demo_id, html_path=path)

    assert "go();" in demos.get_demo(store, demo_id)["html"]


def test_the_tutors_digest_describes_the_served_page(store):
    demo_id = _panel_demo(store)

    assert demos.demo_digest(store, demo_id) is not None


def test_deleting_an_edited_panel_demo_removes_the_retired_panels(store, tmp_path):
    demo_id = _panel_demo(store)
    demos.update_demo(store, demo_id, html_path=_edited(store, demo_id, tmp_path / "s"))

    demos.delete_demo(store, demo_id)

    assert not os.path.exists(demos._retired_parts_dir(store, demo_id))


def test_an_edit_job_on_a_panel_demo_is_ready_and_does_not_rebuild_panels(
        store, tmp_path, monkeypatch):
    """The runner must not see the old declared_parts and fan out again."""
    import backend.agent as agent

    demo_id = _panel_demo(store)
    rebuilt = []

    async def builder(email, request, chat_session_id=None,
                      resume_builder_session=None, on_event=None,
                      build_ctx=None, app="pymentor"):
        demos.update_demo(email, demo_id,
                          html_path=_edited(email, demo_id, tmp_path / "s"),
                          job_id=build_ctx["job_id"])
        return "b1"

    async def part_builder(*a, **k):
        rebuilt.append(k)

    monkeypatch.setattr(agent, "run_demo_builder", builder)
    monkeypatch.setattr(agent, "run_demo_part_builder", part_builder)
    record = demojobs.new_job(store, "chat-1", "PCA", request="show raw data",
                              base_demo_id=demo_id, app="pymentor")

    asyncio.run(demojobs._run_build(record))

    job = demojobs.JOBS[record["job_id"]]
    assert job["state"] == demojobs.READY, job.get("error")
    assert rebuilt == []
    assert "RAW-V2" in demos.get_demo(store, demo_id)["html"]
