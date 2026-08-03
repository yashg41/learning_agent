"""PDF → page images, so an uploaded document can be annotated on the canvas.

Isolated in its own module because PyMuPDF is the only heavy binary dependency
in this repo. `import fitz` happens inside the functions, never at module
scope: a missing wheel must degrade to "PDF import unavailable" rather than
break API startup for everyone.

Pages are written into the user's existing uploads directory and served by
GET /api/uploads/{email}/{filename} — the same route chat attachments use, so
there is no second asset tree and no second path-traversal check to get right.
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

    # Checked by content, not by the filename: a .txt renamed to .pdf should
    # fail here rather than deep inside the parser.
    #
    # The header is not always at byte 0. Real files turn up with a UTF-8 BOM,
    # or wrapped by an exporter — Java's serialization header (\xac\xed) in
    # front of the document is common in enterprise systems. Scan a bounded
    # prefix and trim, rather than demanding a pristine offset 0.
    start = data.find(PDF_MAGIC, 0, PDF_HEADER_SEARCH)
    if start == -1:
        raise ValueError("That file is not a PDF")
    if start > 0:
        logger.info(f"Trimming {start} bytes of wrapper before the PDF header")
        data = data[start:]

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        raise ValueError(f"Could not read that PDF: {e}") from e

    try:
        if doc.needs_pass:
            raise ValueError("Password-protected PDFs are not supported")
        if doc.page_count == 0:
            raise ValueError("That PDF has no pages")
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
