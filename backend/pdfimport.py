"""PDF → page images, so an uploaded document can be annotated on the canvas.

Isolated in its own module because PyMuPDF is the only heavy binary dependency
in this repo. `import fitz` happens inside the functions, never at module
scope: a missing wheel must degrade to "PDF import unavailable" rather than
break API startup for everyone. This module is the ONLY place that imports
fitz — backend/books.py renders through the primitives below rather than
reaching for the library itself, so that invariant survives a second caller.

Pages are written into the user's existing uploads directory and served by
GET /api/uploads/{email}/{filename} — the same route chat attachments use, so
there is no second asset tree and no second path-traversal check to get right.

Two callers with different needs:
  - rasterize_pdf (notes) renders EVERY page at once. A note background is a
    scrollable column of the whole document, so there is nothing to defer.
  - backend/books.py renders single pages on demand via open_document +
    render_page. A 400-page book is ~200MB and several minutes if rendered
    upfront, for pages the learner will mostly never open.
Deliberately not one function with a mode flag: the caps differ (200 pages vs
2000), the output differs (files on disk vs bytes), and a single function
serving both would have to be read twice to understand either.
"""

import logging
import os
import uuid
from urllib.parse import quote

logger = logging.getLogger(__name__)

# Rasterizing is linear in pages and each page is a few hundred KB of PNG, so
# both dimensions need a ceiling. 200 pages at 144 DPI is already ~100 MB.
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 200

# 2x the PDF's native 72 DPI. Readable when a page is scaled to a ~700px pane
# without generating enormous files.
RENDER_DPI = 144

PDF_MAGIC = b"%PDF-"

# How far in to look for the header before giving up. Generous enough for a
# BOM or a serialization wrapper, small enough that a non-PDF containing the
# string somewhere in its body isn't mistaken for one.
PDF_HEADER_SEARCH = 4096


class PdfImportUnavailable(RuntimeError):
    """PyMuPDF isn't installed."""


def is_available() -> bool:
    """Whether PDF import can run in this environment."""
    try:
        import fitz  # noqa: F401
    except ImportError:
        return False
    return True


def trim_to_pdf_header(data: bytes) -> bytes:
    """Verify the bytes are a PDF and trim any wrapper before the header.

    Checked by content, not by the filename or a Content-Type header: a .txt
    renamed to .pdf, or an HTML error page served from a .pdf URL, should fail
    here rather than deep inside the parser. That second case is the common one
    for backend/books.py, where archive.org and Google Books both answer a bad
    request with an HTML page at HTTP 200.

    The header is not always at byte 0. Real files turn up with a UTF-8 BOM, or
    wrapped by an exporter — Java's serialization header (\\xac\\xed) in front of
    the document is common in enterprise systems. Scan a bounded prefix and
    trim, rather than demanding a pristine offset 0.

    Raises ValueError if there is no PDF header in the first PDF_HEADER_SEARCH
    bytes.
    """
    start = data.find(PDF_MAGIC, 0, PDF_HEADER_SEARCH)
    if start == -1:
        raise ValueError("That file is not a PDF")
    if start > 0:
        logger.info(f"Trimming {start} bytes of wrapper before the PDF header")
        return data[start:]
    return data


def open_document(data: bytes):
    """Open PDF bytes as a fitz.Document, with the usual rejections applied.

    The caller owns the returned document and MUST close it — an open document
    holds its whole page cache in memory. Use try/finally; with uvicorn
    reload=True these otherwise pile up across restarts.

    Raises ValueError for anything the caller should report as a 400, and
    PdfImportUnavailable when the dependency is missing. Page-count limits are
    deliberately NOT enforced here: the two callers have different ceilings
    (MAX_PDF_PAGES for notes, MAX_BOOK_PAGES for books), so each checks its own
    against `doc.page_count`.
    """
    try:
        import fitz
    except ImportError as e:
        raise PdfImportUnavailable("PyMuPDF is not installed") from e

    data = trim_to_pdf_header(data)
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        raise ValueError(f"Could not read that PDF: {e}") from e

    # Close before raising: these paths would otherwise leak the document that
    # was successfully opened a line ago.
    try:
        if doc.needs_pass:
            raise ValueError("Password-protected PDFs are not supported")
        if doc.page_count == 0:
            raise ValueError("That PDF has no pages")
    except Exception:
        doc.close()
        raise
    return doc


def render_page(doc, index: int, dpi: int, fmt: str = "png", quality: int = 72) -> bytes:
    """Render one 0-based page of an open document to encoded image bytes.

    Returns bytes rather than writing a file so the caller decides where it
    lands — books.py writes two copies of every page (a PNG to display and a
    smaller JPEG for the model) from a single get_pixmap.

    `alpha=False` because a scanned page has no transparency to preserve and an
    alpha channel would inflate every file for nothing.
    """
    try:
        import fitz
    except ImportError as e:
        raise PdfImportUnavailable("PyMuPDF is not installed") from e

    if index < 0 or index >= doc.page_count:
        raise ValueError(f"page {index + 1} is outside this document (1-{doc.page_count})")

    zoom = dpi / 72
    pix = doc[index].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    if fmt == "jpg" or fmt == "jpeg":
        return pix.tobytes("jpg", jpg_quality=quality)
    return pix.tobytes("png")


def rasterize_pdf(data: bytes, out_dir: str, email: str) -> list[dict]:
    """Render every page to a PNG and return page geometry.

    Returns [{src, w, h}] in page order. Raises ValueError for anything the
    caller should report as a 400 (corrupt, encrypted, too many pages), and
    PdfImportUnavailable when the dependency is missing.
    """
    try:
        import fitz
    except ImportError as e:
        raise PdfImportUnavailable("PyMuPDF is not installed") from e

    # Content check, wrapper trim, password and empty-document rejection all
    # live in open_document — shared with backend/books.py so the lessons
    # encoded there (BOM, Java serialization wrapper, HTML error pages) are
    # applied identically by both callers.
    doc = open_document(data)

    try:
        if doc.page_count > MAX_PDF_PAGES:
            raise ValueError(
                f"That PDF has {doc.page_count} pages (max {MAX_PDF_PAGES})"
            )

        os.makedirs(out_dir, exist_ok=True)
        stem = uuid.uuid4().hex[:8]
        zoom = RENDER_DPI / 72
        matrix = fitz.Matrix(zoom, zoom)

        pages = []
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            name = f"pg_{stem}_{i + 1:03d}.png"
            pix.save(os.path.join(out_dir, name))
            pages.append({
                "src": f"/api/uploads/{quote(email)}/{name}",
                "w": pix.width,
                "h": pix.height,
            })

        logger.info(f"Rasterized {len(pages)} PDF pages into {out_dir}")
        return pages
    finally:
        # An open fitz document holds its whole page cache in memory. With
        # uvicorn reload=True these would otherwise pile up across restarts.
        doc.close()


def image_size(data: bytes) -> tuple[int, int] | None:
    """Pixel dimensions of an encoded image, or None if they can't be read.

    Used to size a pasted screenshot at its true aspect ratio. PyMuPDF is
    already a dependency for PDFs, so it does double duty here rather than
    hand-rolling PNG/JPEG header parsing; callers fall back to a default
    when this returns None.
    """
    try:
        import fitz
    except ImportError:
        return None
    try:
        pix = fitz.Pixmap(data)
        return pix.width, pix.height
    except Exception:
        return None
