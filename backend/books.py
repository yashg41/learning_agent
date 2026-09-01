"""Books the learner reads page-by-page with the tutor.

Layout:
    data/users/<safe_email>/books/_index.json    — list for the picker
    data/users/<safe_email>/books/<book_id>.json — the record (the truth)
    data/users/<safe_email>/books/<book_id>/
        source.pdf          — retained, so pages can be rendered on demand
        p_0037.png          — display copy, RENDER_DPI_DISPLAY
        p_0037.model.jpg    — model copy, RENDER_DPI_MODEL, sent to Claude

A per-book directory rather than the flat data/uploads/ tree that notes use:
delete_book becomes one rmtree, and "which pages belong to this book" is a fact
rather than a filename convention. The consequence is that
GET /api/uploads/{email}/{filename} cannot serve these — it resolves against
EpisodicMemory.uploads_dir_for(email), and a books/ path correctly fails its
containment check. The reader has its own route with its own anchor; the
uploads anchor is load-bearing and must not be widened to accommodate this.

PAGES ARE RENDERED ON DEMAND, not upfront. A 412-page grammar at 144 DPI is
~200MB and several minutes of CPU, for pages the learner will mostly never
open. Keeping source.pdf and rendering lazily makes opening a book ~0.5s
instead, and keeps the background-job machinery off the common path.

TWO COPIES OF EVERY PAGE, and the reason is token cost. A 144 DPI page is
~500KB and ~2,900 vision tokens; the same page at 100 DPI JPEG is ~90KB and
~1,400 tokens. Both are rendered from a single opened document, so the second
costs almost nothing to produce and halves what every page turn costs to send.

WHAT GOES TO THE MODEL, AND WHEN: see needs_resend(). Page images are sent only
when the position changed since the last turn of the same session — they are
already in the transcript from the turn that sent them, and re-sending would
cost ~116k tokens over a 20-turn conversation about one page spread. Same
reasoning that keeps demo HTML out of the prompt (see demos.demo_digest).

Writes go through a temp file + os.replace, as demos.py and memory.py do:
uvicorn may run with reload=True, and a restart mid-write must not leave a
truncated record.
"""

import base64
import json
import logging
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone

from backend.config import settings
from backend.memory import _safe_email

logger = logging.getLogger(__name__)

USERS_DIR = os.path.join(settings.DATA_DIR, "users")

# Serialises read-modify-write on _index.json. Threading rather than asyncio:
# these are plain sync functions called from async handlers (via to_thread),
# and a threading lock is correct in both contexts.
_INDEX_LOCK = threading.Lock()

# Display copy. Matches pdfimport.RENDER_DPI so a book page looks exactly like
# a note page — 2x the PDF's native 72 DPI, readable scaled to a ~700px pane.
RENDER_DPI_DISPLAY = 144

# What a deep zoom asks for. At 144 DPI a page is ~1240x1750, which runs out of
# real detail somewhere past 200% — beyond that the browser is interpolating
# and a damaged conjunct stops being legible exactly when the learner is
# leaning in to read it. Rendered on demand and NOT stored: one page at 300 DPI
# is several MB, and keeping a third copy of all 554 pages of a book to serve
# the handful anyone zooms into would be the worst of both.
RENDER_DPI_ZOOM = 300
MAX_RENDER_DPI = 400

# Model copy. At 100 DPI an A5 page is ~860x1215, which is comfortably legible
# for Devanagari conjuncts and well inside Anthropic's 1568px long-edge
# guidance — above that the image is downscaled server-side anyway, so paying
# to send more pixels buys nothing at all.
RENDER_DPI_MODEL = 100
MODEL_JPEG_QUALITY = 72

# A real ceiling, but far above pdfimport.MAX_PDF_PAGES (200, right for a note
# background). Books are long; a grammar is 400+ pages and the Mahabharata is
# not a reasonable thing to refuse on page count alone. What actually contains
# the cost is MAX_RENDERED_PAGES_PER_BOOK below, since pages render lazily.
MAX_BOOK_PAGES = 2000

# Render this many pages past the window, so clicking Next is instant.
READ_AHEAD = 2

# Rendered pages are a cache, not data: source.pdf can always regenerate them
# in ~0.5s each. Evict the ones furthest from where the learner is reading.
# Without a ceiling a cover-to-cover read of several books fills the disk, and
# there is no user-facing quota anywhere in this app to catch it.
MAX_RENDERED_PAGES_PER_BOOK = 600

# The 5- and 10-page windows are a DISPLAY affordance for skimming layout. Ten
# page images is ~29,000 tokens in a single turn, so the model sees at most
# this many — and book_digest says so explicitly, because a model that thinks
# it shares the learner's screen will confidently discuss a page it never saw.
MAX_WINDOW_PAGES_TO_MODEL = 4

# Exactly what create_book generates: "b_" + uuid4().hex[:10].
_BOOK_ID_RE = re.compile(r"^b_[0-9a-f]{6,32}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def books_dir(email: str) -> str:
    return os.path.join(USERS_DIR, _safe_email(email), "books")


def _ensure_dirs(email: str) -> str:
    d = books_dir(email)
    os.makedirs(d, exist_ok=True)
    return d


def _safe_book_id(book_id: str) -> str:
    """Reject anything that is not a generated book id.

    Ids are interpolated straight into a path and they do NOT all arrive from
    our own UI: go_to_page and open_book pass ids the MODEL produced, where a
    `..` segment would read outside the learner's books directory. Same
    reasoning and same shape as demos._safe_demo_id.
    """
    if not _BOOK_ID_RE.match(book_id or ""):
        raise ValueError(f"invalid book id: {book_id!r}")
    return book_id


def book_dir(email: str, book_id: str) -> str:
    """Directory holding one book's source PDF and rendered pages."""
    return os.path.join(books_dir(email), _safe_book_id(book_id))


def _record_path(email: str, book_id: str) -> str:
    return os.path.join(books_dir(email), f"{_safe_book_id(book_id)}.json")


def _index_path(email: str) -> str:
    return os.path.join(books_dir(email), "_index.json")


def source_pdf_path(email: str, book_id: str) -> str:
    return os.path.join(book_dir(email, book_id), "source.pdf")


def page_image_path(email: str, book_id: str, page: int, for_model: bool = False) -> str:
    """Path of one rendered page. `page` is 1-based, as the learner counts."""
    name = f"p_{page:04d}.model.jpg" if for_model else f"p_{page:04d}.png"
    return os.path.join(book_dir(email, book_id), name)


def _write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_json(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Unreadable book file {path}: {e}")
        return None


# --- Index ---


def _index_entry(record: dict) -> dict:
    # Deliberately no `rendered` list — the index is fetched on every picker
    # render and a 600-entry array per book would dominate it.
    return {
        "book_id": record["book_id"],
        "title": record.get("title") or "Untitled book",
        "page_count": record.get("page_count") or 0,
        "position": record.get("position") or {"page": 1, "window": 2},
        "source_kind": (record.get("source") or {}).get("kind", "upload"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at") or record.get("created_at"),
    }


def rebuild_index(email: str) -> list[dict]:
    """Regenerate _index.json by scanning the directory.

    The index is a derived cache; the per-book .json files are the truth.
    """
    d = _ensure_dirs(email)
    entries = []
    for name in os.listdir(d):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        record = _read_json(os.path.join(d, name))
        if record and record.get("book_id"):
            entries.append(_index_entry(record))
    entries.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
    _write_atomic(_index_path(email), json.dumps({"books": entries}, indent=2))
    return entries


def list_books(email: str) -> list[dict]:
    _ensure_dirs(email)
    data = _read_json(_index_path(email))
    if data is None:
        return rebuild_index(email)
    return data.get("books", [])


def _touch_index(email: str, record: dict) -> None:
    # Read-modify-write on a shared file. A background fetch job can finish at
    # the same moment the learner uploads, so two concurrent writes could
    # otherwise each produce an index missing the other's entry.
    with _INDEX_LOCK:
        entries = [e for e in list_books(email) if e.get("book_id") != record["book_id"]]
        entries.insert(0, _index_entry(record))
        _write_atomic(_index_path(email), json.dumps({"books": entries}, indent=2))


def _save_record(email: str, record: dict) -> dict:
    record["updated_at"] = _now()
    _write_atomic(_record_path(email, record["book_id"]), json.dumps(record, indent=2))
    _touch_index(email, record)
    return record


# --- Create / read / delete ---


def create_book(
    email: str,
    *,
    pdf_bytes: bytes,
    title: str = "",
    source: dict | None = None,
    window: int = 2,
) -> dict:
    """Store a PDF as a book and render its opening pages.

    Raises ValueError for anything the caller reports as a 400 (not a PDF,
    encrypted, empty, too long) and PdfImportUnavailable when PyMuPDF is
    missing. The page count is read here, from the document itself, rather than
    trusted from any caller.
    """
    from backend.pdfimport import open_document

    doc = open_document(pdf_bytes)
    try:
        page_count = doc.page_count
        if page_count > MAX_BOOK_PAGES:
            raise ValueError(
                f"That PDF has {page_count} pages (max {MAX_BOOK_PAGES})"
            )
    finally:
        doc.close()

    _ensure_dirs(email)
    book_id = f"b_{uuid.uuid4().hex[:10]}"
    os.makedirs(book_dir(email, book_id), exist_ok=True)

    # Write the source before the record: a record pointing at a missing PDF
    # would render nothing forever, whereas an orphaned PDF is just bytes.
    with open(source_pdf_path(email, book_id), "wb") as f:
        f.write(pdf_bytes)

    record = {
        "book_id": book_id,
        "title": (title or "").strip()[:200] or "Untitled book",
        "source": source or {"kind": "upload", "url": "", "filename": "", "identifier": ""},
        "page_count": page_count,
        # Explicit list rather than a bool, so lazy rendering is inspectable:
        # ensure_pages diffs against this instead of stat-ing the filesystem
        # on every turn.
        "rendered": [],
        # Position lives HERE, server-side, not in the client. This is the one
        # decision that makes tool-driven page turns work: the tutor's
        # go_to_page writes it, the next chat turn reads it, and the pane
        # reconciles against it. In localStorage the tutor could not move it.
        "position": {"page": 1, "window": max(1, min(int(window or 2), 10))},
        # Which (session, position) last had its images sent — see needs_resend.
        "last_sent": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _save_record(email, record)

    pos = record["position"]
    ensure_pages(email, book_id, window_pages(record, pos["page"], pos["window"], READ_AHEAD))
    logger.info(f"Created book {book_id} ({page_count} pages) for {email}")
    return get_book(email, book_id) or record


def get_book(email: str, book_id: str) -> dict | None:
    try:
        return _read_json(_record_path(email, book_id))
    except ValueError:
        # An invalid id is "no such book", not a crash — the id may have come
        # from the model, and a 404 is the honest answer.
        return None


def delete_book(email: str, book_id: str) -> bool:
    """Remove a book, its record, its source PDF and every rendered page."""
    try:
        book_id = _safe_book_id(book_id)
    except ValueError:
        return False
    record_path = _record_path(email, book_id)
    if not os.path.isfile(record_path):
        return False

    shutil.rmtree(book_dir(email, book_id), ignore_errors=True)
    try:
        os.remove(record_path)
    except OSError as e:
        logger.warning(f"Could not remove book record {book_id}: {e}")
    with _INDEX_LOCK:
        entries = [e for e in list_books(email) if e.get("book_id") != book_id]
        _write_atomic(_index_path(email), json.dumps({"books": entries}, indent=2))
    logger.info(f"Deleted book {book_id} for {email}")
    return True


# --- Position ---


def window_pages(record: dict, page: int, window: int, read_ahead: int = 0) -> list[int]:
    """The 1-based page numbers visible at `page`, optionally plus read-ahead.

    Clamped to the book, so the end of a book yields a short list rather than
    page numbers that do not exist.
    """
    count = record.get("page_count") or 0
    last = min(page + max(1, window) - 1 + read_ahead, count)
    return list(range(max(1, page), last + 1))


def set_position(
    email: str, book_id: str, page: int, window: int | None = None
) -> dict | None:
    """Move the reading position, clamped to the book. Returns the record.

    Clamping rather than erroring: reaching either end of a book is a normal
    event in a reading session, not a mistake to report.
    """
    record = get_book(email, book_id)
    if record is None:
        return None

    count = record.get("page_count") or 1
    if window is None:
        window = record["position"].get("window", 2)
    window = max(1, min(int(window), 10))
    page = max(1, min(int(page), count))

    record["position"] = {"page": page, "window": window}
    _save_record(email, record)
    return record


def active_book(email: str) -> dict | None:
    """The book the learner is most likely reading — the most recently touched.

    Used when a tool omits book_id, which the tutor will usually do: there is
    normally one book open and making it name an id it has to remember is a
    needless failure mode.
    """
    entries = list_books(email)
    if not entries:
        return None
    return get_book(email, entries[0]["book_id"])


# --- Rendering ---


def ensure_pages(email: str, book_id: str, pages: list[int]) -> list[int]:
    """Render any of `pages` not already on disk. Idempotent; safe to call often.

    Returns the pages that are now available. Synchronous and CPU-bound —
    callers in async context MUST use asyncio.to_thread, because chat streams
    SSE from the same event loop and a multi-page render would stall live
    deltas mid-sentence.

    This is the whole reason source.pdf is retained: rasterizing 412 pages
    upfront costs ~200MB and several minutes for a book the learner will read
    thirty pages of.
    """
    from backend.pdfimport import open_document, render_page

    record = get_book(email, book_id)
    if record is None:
        return []

    count = record.get("page_count") or 0
    wanted = sorted({p for p in pages if 1 <= p <= count})
    missing = [
        p for p in wanted
        if not os.path.isfile(page_image_path(email, book_id, p))
        or not os.path.isfile(page_image_path(email, book_id, p, for_model=True))
    ]
    if not missing:
        return wanted

    src = source_pdf_path(email, book_id)
    if not os.path.isfile(src):
        logger.warning(f"Book {book_id} has no source.pdf; cannot render {missing}")
        return [p for p in wanted if p not in missing]

    with open(src, "rb") as f:
        data = f.read()

    doc = open_document(data)
    rendered = []
    try:
        for p in missing:
            try:
                # Both copies from one opened document: the display PNG and the
                # smaller JPEG the model sees. The second get_pixmap is the
                # cheap part once the page is parsed.
                png = render_page(doc, p - 1, RENDER_DPI_DISPLAY, "png")
                jpg = render_page(
                    doc, p - 1, RENDER_DPI_MODEL, "jpg", MODEL_JPEG_QUALITY
                )
            except ValueError as e:
                logger.warning(f"Book {book_id} page {p}: {e}")
                continue
            with open(page_image_path(email, book_id, p), "wb") as f:
                f.write(png)
            with open(page_image_path(email, book_id, p, for_model=True), "wb") as f:
                f.write(jpg)
            rendered.append(p)
    finally:
        # An open document holds its whole page cache in memory.
        doc.close()

    if rendered:
        record = get_book(email, book_id) or record
        record["rendered"] = sorted(set(record.get("rendered") or []) | set(rendered))
        _save_record(email, record)
        logger.info(f"Rendered {len(rendered)} page(s) of book {book_id}")
        _evict_distant_pages(email, book_id)

    return sorted(set(wanted) - set(missing) | set(rendered))


def _evict_distant_pages(email: str, book_id: str) -> int:
    """Drop rendered pages furthest from the current position, past the cap.

    Pages are a cache — source.pdf regenerates any of them in ~0.5s — so the
    only cost of evicting wrongly is one re-render. Never touches source.pdf.
    """
    record = get_book(email, book_id)
    if record is None:
        return 0
    rendered = record.get("rendered") or []
    if len(rendered) <= MAX_RENDERED_PAGES_PER_BOOK:
        return 0

    here = (record.get("position") or {}).get("page", 1)
    doomed = sorted(rendered, key=lambda p: abs(p - here))[MAX_RENDERED_PAGES_PER_BOOK:]
    for p in doomed:
        for model in (False, True):
            try:
                os.remove(page_image_path(email, book_id, p, for_model=model))
            except OSError:
                pass
    record["rendered"] = sorted(set(rendered) - set(doomed))
    _save_record(email, record)
    logger.info(f"Evicted {len(doomed)} cached page(s) of book {book_id}")
    return len(doomed)


# --- What the model sees ---


def book_digest(email: str, book_id: str) -> str | None:
    """One line naming where the learner is. Cheap enough for every turn.

    Deliberately tiny (~60 tokens). The page IMAGES are the expensive part and
    are sent conditionally (see needs_resend); this text is what keeps the
    model oriented on the turns where they are not.
    """
    record = get_book(email, book_id)
    if record is None:
        return None

    pos = record.get("position") or {"page": 1, "window": 1}
    count = record.get("page_count") or 0
    shown = window_pages(record, pos["page"], pos.get("window", 1))
    if not shown:
        return None

    lo, hi = shown[0], shown[-1]
    span = f"page {lo}" if lo == hi else f"pages {lo}–{hi}"
    digest = (
        f'Book "{record.get("title") or "Untitled book"}" (book_id: {book_id}), '
        f"{count} pages. The learner is looking at {span}."
    )

    # The model must know when its view is narrower than the learner's, or it
    # will discuss a page it was never shown with complete confidence.
    if len(shown) > MAX_WINDOW_PAGES_TO_MODEL:
        sent = shown[:MAX_WINDOW_PAGES_TO_MODEL]
        digest += (
            f" You are shown only {sent[0]}–{sent[-1]} of those — if they ask "
            f"about a later page in the spread, move to it first."
        )
    return digest


def needs_resend(email: str, book_id: str, session_id: str | None) -> bool:
    """Whether this turn must carry the page images again.

    The key is (session_id, page, window), and the session id is NOT
    incidental: a new session has an empty transcript, so the images are not in
    its context and an unchanged position must still re-send. Without that, the
    model discusses — fluently, and with total confidence — a page it has never
    seen. That is the worst failure this feature can produce, and it is
    invisible to the learner, who has the page in front of them.

    Within one session the images stay in the SDK transcript, so re-sending on
    every turn is pure duplication: ~2,900 vision tokens per page, replayed on
    every later turn, is ~116k tokens over a 20-turn conversation about one
    spread. book_digest is what makes not-re-sending safe.
    """
    record = get_book(email, book_id)
    if record is None:
        return False
    last = record.get("last_sent")
    if not last:
        return True
    # No session id yet means the SDK has not minted one, which happens on the
    # FIRST turn of a new session — an empty transcript, so nothing has been
    # shown into it whatever the stored id says. Treating None as "might match"
    # would let a brand-new session inherit the previous one's marker and skip
    # the images entirely, which is the worst outcome this module can produce:
    # the model discusses a page it has never seen and the learner, looking
    # right at that page, has no reason to doubt it.
    if session_id is None:
        return True
    pos = record.get("position") or {}
    return (
        last.get("session_id") != session_id
        or last.get("page") != pos.get("page")
        or last.get("window") != pos.get("window")
    )


def mark_sent(email: str, book_id: str, session_id: str | None) -> None:
    """Record that this session has now seen the current position's images."""
    record = get_book(email, book_id)
    if record is None:
        return
    pos = record.get("position") or {}
    record["last_sent"] = {
        "session_id": session_id,
        "page": pos.get("page"),
        "window": pos.get("window"),
    }
    _save_record(email, record)


def page_images_for_model(email: str, book_id: str) -> list[dict]:
    """Base64 JPEG page images for the current position, as attachment dicts.

    Shaped to match what agent._prompt_as_stream expects of `attachments`:
    {data, content_type, filename}. Renders anything missing first, so a
    position set by a tool that has not been displayed yet still arrives.

    Synchronous and CPU/IO-bound — call through asyncio.to_thread.
    """
    record = get_book(email, book_id)
    if record is None:
        return []

    pos = record.get("position") or {"page": 1, "window": 1}
    shown = window_pages(record, pos["page"], pos.get("window", 1))
    wanted = shown[:MAX_WINDOW_PAGES_TO_MODEL]
    if not wanted:
        return []

    ensure_pages(email, book_id, wanted)

    images = []
    for p in wanted:
        path = page_image_path(email, book_id, p, for_model=True)
        try:
            with open(path, "rb") as f:
                images.append({
                    "data": base64.b64encode(f.read()).decode("ascii"),
                    "content_type": "image/jpeg",
                    "filename": f"page_{p:04d}.jpg",
                })
        except OSError as e:
            # A page that will not render must not sink the turn; the digest
            # still names the position and the tutor can say what it cannot see.
            logger.warning(f"Book {book_id} page {p} unreadable: {e}")
    return images


def render_page_at_dpi(email: str, book_id: str, page: int, dpi: int) -> bytes | None:
    """One page rendered fresh at `dpi`, returned as PNG bytes. Not cached.

    Serves a deep zoom, where the 144 DPI display copy has run out of real
    detail. Deliberately not written to disk: a 300 DPI page is several MB and
    only a handful of pages in any book are ever zoomed into, so a third stored
    copy of every page would cost far more than re-rendering the few that are.

    Synchronous and CPU-bound — call through asyncio.to_thread.
    """
    from backend.pdfimport import open_document, render_page

    record = get_book(email, book_id)
    if record is None or not (1 <= page <= (record.get("page_count") or 0)):
        return None

    src = source_pdf_path(email, book_id)
    if not os.path.isfile(src):
        return None

    with open(src, "rb") as f:
        data = f.read()

    doc = open_document(data)
    try:
        return render_page(doc, page - 1, max(72, min(int(dpi), MAX_RENDER_DPI)), "png")
    except ValueError:
        return None
    finally:
        doc.close()
