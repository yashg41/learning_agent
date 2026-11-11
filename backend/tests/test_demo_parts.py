"""Demos assembled from a shell plus one file per panel.

A demo the learner asked for — the same dataset shown as ten different database
engines — was never saved. The builder tried to emit the whole document in one
reply twice, and both times the API discarded the reply for exceeding the
output-token maximum, so nothing reached save_demo at all.

The cap is on what a model writes in ONE reply, so the fix is to stop asking it
to write the demo in one reply: the shell (doctype, shared CSS, shared JS) is
written once, each panel is written separately, and the pieces are joined when
the demo is read. Roughly two thirds of a demo is that shared scaffolding, so
the panel count stops driving the size of any single write.
"""

import asyncio
import os

import pytest

from backend import demos


SHELL = (
    "<!doctype html><html><head><style>.p{display:none}.p.on{display:block}"
    "</style></head><body><div id='tabs'></div>\n"
    + demos.PARTS_MARKER
    + "\n<script>function show(i){}\nshow(0);</script></body></html>"
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the demo store at a temp dir."""
    monkeypatch.setattr(demos, "USERS_DIR", str(tmp_path))
    return "learner@example.com"


def test_parts_assemble_in_index_order_not_write_order(store):
    """Document order is the orchestrator's, not the schedule's.

    Panels are written by builders running concurrently, so they finish in an
    arbitrary order. Ordering on the numeric filename prefix is what makes the
    result deterministic.
    """
    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]

    for index, name in [(3, "graph"), (1, "relational"), (2, "keyvalue")]:
        demos.save_demo_part(store, demo_id, index, name, f'<div class="p">{name}</div>')

    demos.finalize_demo(store, demo_id)
    html = demos.get_demo(store, demo_id)["html"]

    positions = [html.index(n) for n in ("relational", "keyvalue", "graph")]
    assert positions == sorted(positions)
    assert demos.PARTS_MARKER not in html


def test_assembled_document_passes_validation(store):
    """The whole-document checks run on the join, because they cannot run per part.

    A single part is a fragment: it has no <html>, no <body>, and may call a
    handler another part defines. Validating one in isolation would fail every
    time.
    """
    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]
    demos.save_demo_part(store, demo_id, 1, "relational", '<div class="p">A</div>')

    meta = demos.finalize_demo(store, demo_id)
    assert meta["part_count"] == 1
    # Would raise if the assembled document did not validate.
    demos.validate_demo_html(demos.get_demo(store, demo_id)["html"])


def test_single_file_demo_still_loads(store):
    """Demos written before parts existed must be untouched.

    get_demo falls back to the flat <demo_id>.html when there is no shell, so
    nothing already on disk needs migrating.
    """
    meta = demos.save_demo(
        store, "old", "<!doctype html><html><body><p>hi</p></body></html>"
    )
    assert not demos.has_parts(store, meta["id"])
    assert "hi" in demos.get_demo(store, meta["id"])["html"]


def test_unfinished_demo_is_not_listed(store):
    """An opened demo stays hidden until it has panels.

    Its metadata is written at open time so the parts have an id to attach to,
    but listing it then would put a demo in the learner's pane whose frame is
    empty.
    """
    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]
    assert demo_id not in [d["id"] for d in demos.list_demos(store)]

    demos.save_demo_part(store, demo_id, 1, "relational", '<div class="p">A</div>')
    assert demo_id not in [d["id"] for d in demos.list_demos(store)]

    demos.finalize_demo(store, demo_id)
    assert demo_id in [d["id"] for d in demos.list_demos(store)]


def test_finalize_without_parts_is_refused(store):
    """A shell with no panels is an empty frame, not a demo."""
    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]
    with pytest.raises(ValueError, match="no parts"):
        demos.finalize_demo(store, demo_id)


def test_shell_must_carry_the_parts_marker(store):
    """Without the marker the panels have nowhere to land."""
    with pytest.raises(ValueError, match="PARTS"):
        demos.open_demo(store, "Shapes", "<!doctype html><html><body></body></html>")

    with pytest.raises(ValueError, match="more than once"):
        demos.open_demo(
            store,
            "Shapes",
            "<html><body>" + demos.PARTS_MARKER + demos.PARTS_MARKER + "</body></html>",
        )


def test_concurrent_part_writes_all_survive(store):
    """Ten panels written at once must all land.

    One file per part, so there is no shared document to read-modify-write —
    the failure mode that silently dropped a whole batch of concept writes
    elsewhere in this codebase.
    """
    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]

    async def write(index: int) -> None:
        await asyncio.sleep(0)  # force interleaving
        demos.save_demo_part(
            store, demo_id, index, f"panel{index}", f'<div class="p">P{index}</div>'
        )

    async def main() -> None:
        await asyncio.gather(*[write(i) for i in range(1, 11)])

    asyncio.run(main())

    assert len(demos.list_demo_parts(store, demo_id)) == 10
    demos.finalize_demo(store, demo_id)
    html = demos.get_demo(store, demo_id)["html"]
    positions = [html.index(f"P{i}<") for i in range(1, 11)]
    assert positions == sorted(positions)


def test_delete_removes_the_parts_directory(store):
    """Eviction must not leave panels behind.

    MAX_DEMOS_PER_USER eviction reclaims the index row and the .html; without
    this the parts would stay on disk forever.
    """
    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]
    demos.save_demo_part(store, demo_id, 1, "relational", '<div class="p">A</div>')
    demos.finalize_demo(store, demo_id)

    parts_dir = demos._parts_dir(store, demo_id)
    assert os.path.isdir(parts_dir)

    assert demos.delete_demo(store, demo_id) is True
    assert not os.path.isdir(parts_dir)


def test_oversized_part_is_refused(store):
    """A part is one panel's markup, not a whole document."""
    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]
    with pytest.raises(ValueError, match="over the"):
        demos.save_demo_part(
            store, demo_id, 1, "big", "x" * (demos.MAX_PART_BYTES + 1)
        )


def test_part_filenames_are_sanitised(store):
    """A part name reaches the filesystem, so it must not escape the directory."""
    demo_id = demos.open_demo(store, "Shapes", SHELL)["id"]
    name = demos.save_demo_part(store, demo_id, 1, "../../etc/passwd", "<div>x</div>")
    assert "/" not in name and ".." not in name
    assert name.startswith("01_")
