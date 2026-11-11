"""Book storage, lazy page rendering, and what reaches the model.

The tests worth the most here are the needs_resend truth table and the
window-cap check. Both guard failures that are invisible to the learner: one
lets the model discuss a page it has never seen, the other silently narrows its
view while the learner believes they share a screen.

Nothing here calls the model. Rendering is real PyMuPDF work on a PDF built in
memory, because the whole point of the module is what comes out of that.
"""

import asyncio
import os
import shutil

import pytest

from backend.books import (
    MAX_WINDOW_PAGES_TO_MODEL,
    _safe_book_id,
    book_digest,
    books_dir,
    create_book,
    delete_book,
    ensure_pages,
    get_book,
    list_books,
    mark_sent,
    needs_resend,
    page_image_path,
    page_images_for_model,
    rebuild_index,
    set_position,
    source_pdf_path,
    window_pages,
)
from backend.memory import _safe_email

pytest.importorskip("fitz", reason="PyMuPDF is needed to render book pages")


def _pdf(pages: int = 12) -> bytes:
    """A real multi-page PDF, so rendering is exercised rather than mocked."""
    import fitz

    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=420, height=595)
        page.insert_text((60, 100), f"page {i + 1}", fontsize=24)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def email(tmp_path_factory):
    who = f"__test_{tmp_path_factory.mktemp('b').name}__"
    yield who
    shutil.rmtree(books_dir(who), ignore_errors=True)


@pytest.fixture
def book(email):
    return create_book(email, pdf_bytes=_pdf(12), title="Test Grammar")


# --- Ids -------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "b_", "../etc/passwd", "b_../../x", "d_abc123", "b_ABC123",
     "b_" + "f" * 33, "b_xyz", None],
)
def test_safe_book_id_rejects_anything_not_ours(bad):
    """Ids reach this module from MODEL output and become path segments."""
    with pytest.raises(ValueError):
        _safe_book_id(bad)


def test_safe_book_id_accepts_a_generated_id(book):
    assert _safe_book_id(book["book_id"]) == book["book_id"]


def test_safe_email_is_idempotent():
    """books.py passes _email_from_dir output (already sanitised) back through
    _safe_email. That is only safe while the function is idempotent, so a
    future change to it must break here rather than silently break every
    book lookup."""
    for raw in ["Yash+ml@Gmail.com", "a b/../c@x.io", "plain@example.com"]:
        assert _safe_email(_safe_email(raw)) == _safe_email(raw)


# --- Create / read / delete ------------------------------------------------


def test_create_reads_page_count_from_the_document(email):
    b = create_book(email, pdf_bytes=_pdf(7), title="Seven")
    assert b["page_count"] == 7
    assert b["title"] == "Seven"
    assert os.path.isfile(source_pdf_path(email, b["book_id"]))


def test_create_rejects_a_non_pdf(email):
    with pytest.raises(ValueError, match="not a PDF"):
        create_book(email, pdf_bytes=b"<html>nope</html>")


def test_create_renders_only_the_opening_pages(book, email):
    """Lazy rendering is the point: a 400-page book must not cost 400 renders."""
    assert book["rendered"], "the opening window should be rendered eagerly"
    assert max(book["rendered"]) < 12, "the whole book must not be rendered upfront"


def test_round_trip_and_index(book, email):
    assert get_book(email, book["book_id"])["title"] == "Test Grammar"
    assert [e["book_id"] for e in list_books(email)] == [book["book_id"]]


def test_index_is_a_derived_cache(book, email):
    """The record files are the truth; the index can always be regenerated."""
    os.remove(os.path.join(books_dir(email), "_index.json"))
    assert [e["book_id"] for e in rebuild_index(email)] == [book["book_id"]]


def test_a_stray_tmp_file_is_not_a_book(book, email):
    with open(os.path.join(books_dir(email), "b_deadbeef99.json.tmp"), "w") as f:
        f.write('{"book_id": "b_deadbeef99"}')
    assert [e["book_id"] for e in rebuild_index(email)] == [book["book_id"]]


def test_get_book_treats_a_bad_id_as_missing(email):
    """An id from the model must 404, not raise: the route must not 500."""
    assert get_book(email, "../../etc/passwd") is None


def test_delete_removes_pages_and_source(book, email):
    d = os.path.join(books_dir(email), book["book_id"])
    assert os.path.isdir(d)
    assert delete_book(email, book["book_id"]) is True
    assert not os.path.exists(d)
    assert get_book(email, book["book_id"]) is None
    assert list_books(email) == []


def test_delete_is_false_for_an_unknown_book(email):
    assert delete_book(email, "b_0123456789") is False


# --- Position --------------------------------------------------------------


def test_window_pages_clamps_at_the_end_of_the_book(book):
    """Reaching the end is normal; it must yield a short list, not bad pages."""
    assert window_pages(book, 11, 5) == [11, 12]
    assert window_pages(book, 1, 2) == [1, 2]


def test_set_position_clamps_rather_than_erroring(book, email):
    bid = book["book_id"]
    assert set_position(email, bid, 999)["position"]["page"] == 12
    assert set_position(email, bid, -4)["position"]["page"] == 1


def test_set_position_keeps_the_window_when_not_given(book, email):
    bid = book["book_id"]
    set_position(email, bid, 3, window=5)
    assert set_position(email, bid, 4)["position"]["window"] == 5


def test_set_position_is_none_for_an_unknown_book(email):
    assert set_position(email, "b_0123456789", 2) is None


# --- Rendering -------------------------------------------------------------


def test_ensure_pages_renders_both_copies(book, email):
    bid = book["book_id"]
    ensure_pages(email, bid, [9])
    assert os.path.isfile(page_image_path(email, bid, 9))
    assert os.path.isfile(page_image_path(email, bid, 9, for_model=True))


def test_the_model_copy_is_smaller_than_the_display_copy(book, email):
    """The whole reason for two copies: halved token cost per page turn."""
    bid = book["book_id"]
    ensure_pages(email, bid, [5])
    display = os.path.getsize(page_image_path(email, bid, 5))
    model = os.path.getsize(page_image_path(email, bid, 5, for_model=True))
    assert model < display


def test_ensure_pages_ignores_pages_outside_the_book(book, email):
    assert ensure_pages(email, book["book_id"], [0, 5, 99]) == [5]


def test_ensure_pages_is_idempotent(book, email):
    bid = book["book_id"]
    ensure_pages(email, bid, [6])
    stamp = os.path.getmtime(page_image_path(email, bid, 6))
    assert ensure_pages(email, bid, [6]) == [6]
    assert os.path.getmtime(page_image_path(email, bid, 6)) == stamp


# --- What the model sees ---------------------------------------------------


def test_digest_names_a_single_page_and_a_span(book, email):
    bid = book["book_id"]
    set_position(email, bid, 4, window=1)
    assert "page 4" in book_digest(email, bid)
    set_position(email, bid, 4, window=2)
    assert "pages 4–5" in book_digest(email, bid)


def test_digest_is_none_for_a_missing_book(email):
    """The route must not 500 on an id the model invented."""
    assert book_digest(email, "b_0123456789") is None


def test_digest_admits_when_the_model_sees_less_than_the_learner(book, email):
    """Silently narrowing the model's view is how it discusses a page it was
    never shown."""
    bid = book["book_id"]
    set_position(email, bid, 1, window=10)
    assert "only" in book_digest(email, bid)


def test_model_never_gets_more_than_the_window_cap(book, email):
    """Ten page images is ~29k tokens in one turn."""
    bid = book["book_id"]
    set_position(email, bid, 1, window=10)
    images = page_images_for_model(email, bid)
    assert len(images) == MAX_WINDOW_PAGES_TO_MODEL


def test_model_images_are_attachment_shaped(book, email):
    """Must match what agent._prompt_as_stream expects of `attachments`."""
    images = page_images_for_model(email, book["book_id"])
    assert images
    for img in images:
        assert set(img) == {"data", "content_type", "filename"}
        assert img["content_type"] == "image/jpeg"
        assert isinstance(img["data"], str) and img["data"]


def test_model_images_render_a_position_never_displayed(book, email):
    """A tool can set a position the pane has not drawn yet; the turn that
    follows still has to carry real pages."""
    bid = book["book_id"]
    set_position(email, bid, 12, window=1)
    assert len(page_images_for_model(email, bid)) == 1


# --- needs_resend: the truth table -----------------------------------------


def test_resend_when_never_sent(book, email):
    assert needs_resend(email, book["book_id"], "s-1") is True


def test_no_resend_on_the_same_position_in_the_same_session(book, email):
    """~2,900 vision tokens per page, replayed on every later turn."""
    bid = book["book_id"]
    mark_sent(email, bid, "s-1")
    assert needs_resend(email, bid, "s-1") is False


def test_resend_when_the_position_changed(book, email):
    bid = book["book_id"]
    mark_sent(email, bid, "s-1")
    set_position(email, bid, 6)
    assert needs_resend(email, bid, "s-1") is True


def test_resend_when_only_the_window_changed(book, email):
    """Widening the window brings pages the model has not seen."""
    bid = book["book_id"]
    set_position(email, bid, 3, window=1)
    mark_sent(email, bid, "s-1")
    set_position(email, bid, 3, window=3)
    assert needs_resend(email, bid, "s-1") is True


def test_resend_in_a_new_session_at_the_same_position(book, email):
    """The one that matters most. A new session has an empty transcript, so
    the images are not in its context — without this the model discusses,
    with complete confidence, a page it has never seen."""
    bid = book["book_id"]
    mark_sent(email, bid, "s-1")
    assert needs_resend(email, bid, "s-2") is True


def test_no_resend_for_a_missing_book(email):
    assert needs_resend(email, "b_0123456789", "s-1") is False


# --- The whole lifecycle, through run_agent ---------------------------------


@pytest.mark.sanskrit
def test_page_images_are_sent_once_per_position_per_session(email, monkeypatch):
    """The end-to-end contract, exercised through run_agent.

    Unit-testing needs_resend cannot catch this: the session id it is keyed on
    is resolved inside run_agent through the active-session fallback, a fork,
    or a restore, and is written back only after the SDK returns one. An
    earlier version marked the images as sent against the pre-run id — None on
    a session's first turn — so a genuinely new session inherited the previous
    one's marker and silently skipped the images.

    The SDK itself is stubbed: a real turn needs a live subprocess.
    """
    from backend import agent
    from backend.memory import save_user_session

    captured = []
    minted = [0]

    async def fake_internal(**kwargs):
        captured.append(kwargs)
        sid = kwargs.get("session_id")
        if not sid:
            minted[0] += 1
            sid = f"sdk-{minted[0]}"
        return sid

    monkeypatch.setattr(agent, "run_agent_internal", fake_internal)
    monkeypatch.setattr(
        agent, "session_exists_in_claude",
        lambda sid: bool(sid) and str(sid).startswith("sdk-"),
    )

    book = create_book(email, pdf_bytes=_pdf(6), title="Lifecycle")
    bid = book["book_id"]
    set_position(email, bid, 2, window=1)

    def turn():
        captured.clear()
        asyncio.run(agent.run_agent(
            prompt="q", email=email, book_ref={"book_id": bid}, app="sanskrit",
        ))
        return len(captured[0]["attachments"])

    assert turn() == 1, "first turn of a session must carry the page"
    assert turn() == 0, "same page, same session — already in the transcript"

    set_position(email, bid, 4)
    assert turn() == 1, "the position moved, so the new page must be sent"
    assert turn() == 0

    # A new session starts with an empty transcript: whatever was sent before
    # is not in its context, so an unchanged position must still re-send.
    save_user_session(email, "", set_active=True)
    assert turn() == 1, "a NEW session has never seen this page"
    assert turn() == 0


@pytest.mark.sanskrit
def test_book_pages_are_never_written_to_uploads(email, monkeypatch):
    """saved_attachments must stay empty for a turn whose only images are
    book pages: they are server-generated, already on disk, and regenerable."""
    from backend import agent

    captured = []

    async def fake_internal(**kwargs):
        captured.append(kwargs)
        return "sdk-1"

    monkeypatch.setattr(agent, "run_agent_internal", fake_internal)

    book = create_book(email, pdf_bytes=_pdf(4), title="No dupes")
    asyncio.run(agent.run_agent(
        prompt="q", email=email, book_ref={"book_id": book["book_id"]}, app="sanskrit",
    ))
    assert captured[0]["attachments"], "the page images should reach the model"
    assert captured[0]["saved_attachments"] == [], (
        "book pages must not be persisted as chat attachments"
    )
