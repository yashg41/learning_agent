"""Turn a URL into book PDF bytes.

Two jobs, and the second is the reason this module is careful:

1. RESOLVE. An archive.org details page is not a PDF. Neither is a Google Books
   volume page. Each needs its own lookup to find the actual file — or to
   establish that there isn't one and say so usefully.

2. FETCH SAFELY. The URL does not only come from the learner: `open_book` lets
   the TUTOR pass a URL it found by searching. That is a server-side request
   with a model-chosen destination, so every rule below is load-bearing rather
   than defensive habit:

     - https only. Not http, not file://, not ftp://, not data:.
     - The hostname is resolved FIRST and every address it returns is checked.
       This is what blocks 169.254.169.254 (the cloud metadata endpoint),
       localhost, and the private ranges — a name can point anywhere.
     - Redirects are followed BY HAND, re-checking each hop. httpx's
       follow_redirects=True would check the first host and then follow a
       302 into 127.0.0.1 without looking.
     - The body is capped WHILE streaming. A Content-Length header is a claim,
       not a fact.
     - An optional host allowlist, on by default, because the model picks the
       URL and starting closed is the right direction.

The bytes are verified as a PDF by content (pdfimport.trim_to_pdf_header), never
by Content-Type. Both archive.org and Google answer a bad request with an HTML
page at HTTP 200, and a captcha saved as .pdf is exactly the failure to catch
here rather than three steps later inside the parser.
"""

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

from backend.config import settings

logger = logging.getLogger(__name__)

# Generous enough for a scanned grammar, bounded so one fetch cannot fill the
# disk. Shared with the upload path via pdfimport.MAX_PDF_BYTES.
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
TOTAL_TIMEOUT = 300.0

# TLS verification. A corporate proxy (Netskope, Zscaler and friends) re-signs
# every certificate with a private root that lives in the OS keychain — which
# curl and the browser consult and Python does not, so httpx fails with
# "self-signed certificate in certificate chain" on a machine where the same
# URL opens fine in a browser.
#
# Resolved in the same order as backend/sanskrit_setup.py's downloader, which
# hit this first: try the normal path, then fall back to the OS trust store.
# Both are real verification — this never disables certificate checking, which
# would trade a nuisance for a genuine hole on a path that fetches
# model-chosen URLs.
_SSL_CONTEXT = None
_SSL_RESOLVED = False


def _ssl_context():
    """An SSL context that also trusts the OS store, when one is needed.

    Returns True (httpx's default verification) unless the `truststore` package
    is available, in which case the OS store is used — that is what carries a
    corporate proxy's root.
    """
    global _SSL_CONTEXT, _SSL_RESOLVED
    if _SSL_RESOLVED:
        return _SSL_CONTEXT
    _SSL_RESOLVED = True
    try:
        import ssl

        import truststore

        _SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        logger.info("Using the OS trust store for book fetches")
    except ImportError:
        _SSL_CONTEXT = True
    return _SSL_CONTEXT


def _client_kwargs() -> dict:
    return {"verify": _ssl_context()}


ARCHIVE_METADATA = "https://archive.org/metadata/{}"
ARCHIVE_DOWNLOAD = "https://archive.org/download/{}/{}"
GOOGLE_VOLUMES = "https://www.googleapis.com/books/v1/volumes/{}"

_ARCHIVE_PATH = re.compile(r"^/(?:details|download|stream|embed)/([^/?#]+)")

# In preference order. A scanned book is frequently <id>_bw.pdf and <id>.pdf may
# not exist at all, which is why the metadata API is consulted rather than the
# download URL being guessed.
_PDF_FORMATS = ("Text PDF", "Image Container PDF", "Additional Text PDF")


class ResolveError(ValueError):
    """The URL cannot become a book, with a message meant for the tutor.

    The text matters: it is what `open_book` returns, and the tutor has
    WebSearch, so "no PDF here, try archive.org" is an instruction it can act
    on. A dead end that names the next step turns into a retry that converges.
    """


@dataclass
class ResolvedSource:
    pdf_url: str
    title: str = ""
    kind: str = "url"
    identifier: str = ""
    filename: str = ""
    meta: dict = field(default_factory=dict)


# --- Safety ---------------------------------------------------------------


def _allowed_hosts() -> set[str]:
    raw = (getattr(settings, "BOOK_FETCH_ALLOWED_HOSTS", "") or "").strip()
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _host_allowed(host: str) -> bool:
    """Whether `host` is permitted, honouring a leading-dot suffix rule.

    An empty allowlist means "any public address", which is still subject to
    every other check below.
    """
    allowed = _allowed_hosts()
    if not allowed:
        return True
    host = host.lower().rstrip(".")
    for entry in allowed:
        if host == entry or host.endswith("." + entry.lstrip(".")):
            return True
    return False


def assert_fetchable(url: str) -> str:
    """Raise ResolveError unless this URL is safe for the server to fetch.

    Called on the original URL AND on every redirect target. Returns the URL
    so it can be used inline.
    """
    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise ResolveError(
            f"Only https:// links can be opened (this one is "
            f"'{parsed.scheme or 'relative'}'). Give a direct https link to a "
            "PDF, or an archive.org page."
        )
    host = parsed.hostname
    if not host:
        raise ResolveError("That URL has no host.")

    if not _host_allowed(host):
        raise ResolveError(
            f"Books cannot be fetched from {host}. archive.org is the reliable "
            "source for out-of-copyright Sanskrit texts — search there instead."
        )

    # Resolve the NAME, then judge the addresses. A hostname that looks public
    # can resolve to a private address, which is the whole trick.
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ResolveError(f"Could not look up {host}: {e}") from e

    for family, _, _, _, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise ResolveError(f"{host} resolves to an address type we do not fetch.")
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            raise ResolveError(f"{host} resolved to something that is not an IP address.")
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local        # blocks 169.254.169.254, cloud metadata
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ResolveError(
                f"{host} points at a non-public address ({ip}), which this "
                "server will not fetch."
            )
    return url


# --- Resolvers ------------------------------------------------------------


def archive_identifier(url: str) -> str | None:
    """The archive.org item id in a details/download/stream/embed URL."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host == "archive.org" or host.endswith(".archive.org")):
        return None
    match = _ARCHIVE_PATH.match(parsed.path or "")
    return match.group(1) if match else None


def pick_archive_pdf(metadata: dict) -> tuple[str, str] | None:
    """(filename, format) of the best PDF in an archive.org metadata payload.

    Prefers a real text PDF over a scanned image container, and takes any .pdf
    over nothing. Returns None when the item has no PDF at all — some are audio
    or plain text, and guessing a filename would 404 later instead of now.
    """
    files = metadata.get("files") or []
    for wanted in _PDF_FORMATS:
        for f in files:
            if f.get("format") == wanted and (f.get("name") or "").lower().endswith(".pdf"):
                return f["name"], wanted
    for f in files:
        if (f.get("name") or "").lower().endswith(".pdf"):
            return f["name"], f.get("format", "PDF")
    return None


def google_volume_id(url: str) -> str | None:
    """The volume id in a Google Books URL, from the path or the ?id= query."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if "books.google" not in host and host != "books.googleusercontent.com":
        return None
    match = re.search(r"[?&]id=([A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"/books/edition/[^/]+/([A-Za-z0-9_-]+)", parsed.path or "")
    return match.group(1) if match else None


async def _get_json(client, url: str) -> dict:
    assert_fetchable(url)
    response = await client.get(url, headers={"Accept": "application/json"})
    response.raise_for_status()
    return response.json()


async def resolve_to_pdf(url: str) -> ResolvedSource:
    """Work out what PDF a URL means. Network, but no download yet."""
    import httpx

    url = (url or "").strip()
    if not url:
        raise ResolveError("No URL given.")
    assert_fetchable(url)

    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=CONNECT_TIMEOUT, pool=CONNECT_TIMEOUT
    )
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, **_client_kwargs()
    ) as client:
        item = archive_identifier(url)
        if item:
            try:
                metadata = await _get_json(client, ARCHIVE_METADATA.format(quote(item)))
            except Exception as e:
                raise ResolveError(f"Could not read archive.org metadata for '{item}': {e}") from e
            if not metadata or not metadata.get("files"):
                raise ResolveError(
                    f"archive.org has no item called '{item}'. Check the link, or "
                    "search archive.org for the title."
                )
            picked = pick_archive_pdf(metadata)
            if not picked:
                raise ResolveError(
                    f"The archive.org item '{item}' has no PDF (it may be audio "
                    "or plain text). Look for a different scan of the same text."
                )
            name, fmt = picked
            meta = metadata.get("metadata") or {}
            return ResolvedSource(
                pdf_url=ARCHIVE_DOWNLOAD.format(quote(item), quote(name)),
                title=str(meta.get("title") or item),
                kind="archive_org",
                identifier=item,
                filename=name,
                meta={"format": fmt, "creator": meta.get("creator", "")},
            )

        volume = google_volume_id(url)
        if volume:
            try:
                data = await _get_json(client, GOOGLE_VOLUMES.format(quote(volume)))
            except Exception as e:
                raise ResolveError(f"Could not read Google Books data for '{volume}': {e}") from e
            access = data.get("accessInfo") or {}
            info = data.get("volumeInfo") or {}
            title = str(info.get("title") or volume)
            pdf_available = (access.get("pdf") or {}).get("isAvailable")
            # Refuse rather than download: for a preview book the "download"
            # is a captcha page, and finding that out inside the PDF parser is
            # three steps too late to say anything useful.
            if access.get("viewability") != "ALL_VIEW" or not pdf_available:
                raise ResolveError(
                    f"Google Books does not offer “{title}” as a downloadable "
                    "PDF — it is preview-only, which is true of most books "
                    "there. If it is out of copyright it is very likely on "
                    "archive.org; search there instead, or ask the learner to "
                    "upload a PDF they already have."
                )
            link = (access.get("pdf") or {}).get("downloadLink")
            if not link:
                raise ResolveError(
                    f"Google Books lists “{title}” as downloadable but gave no "
                    "link. Try archive.org for the same title."
                )
            return ResolvedSource(
                pdf_url=link, title=title, kind="google_books", identifier=volume,
            )

    # Anything else is taken at face value and verified by its bytes.
    return ResolvedSource(
        pdf_url=url,
        kind="url",
        filename=(urlparse(url).path or "").rsplit("/", 1)[-1],
    )


# --- Fetch ----------------------------------------------------------------


async def fetch_pdf(url: str, max_bytes: int, on_progress=None) -> bytes:
    """Download a PDF, following redirects by hand and capping as it streams.

    `on_progress(received, total)` is called every few hundred KB. A book is a
    large download and a silent progress chip is what makes people reload.
    """
    import httpx

    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=CONNECT_TIMEOUT, pool=CONNECT_TIMEOUT
    )
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, **_client_kwargs()
    ) as client:
        current = assert_fetchable(url)

        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ResolveError("That link redirected to nowhere.")
                    # Re-check EVERY hop. A first host that passes says nothing
                    # about where it sends you.
                    current = assert_fetchable(str(response.next_request.url) if response.next_request
                                               else location)
                    continue

                if response.status_code >= 400:
                    raise ResolveError(
                        f"That link returned HTTP {response.status_code}. "
                        "The file may have moved, or the item may be restricted."
                    )

                total = None
                try:
                    total = int(response.headers.get("content-length") or 0) or None
                except ValueError:
                    total = None
                if total and total > max_bytes:
                    raise ResolveError(
                        f"That PDF is {total // (1024 * 1024)}MB, over the "
                        f"{max_bytes // (1024 * 1024)}MB limit."
                    )

                chunks, received, last_ping = [], 0, 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    received += len(chunk)
                    # Enforced against what actually arrives, not the header.
                    if received > max_bytes:
                        raise ResolveError(
                            f"That download went over the "
                            f"{max_bytes // (1024 * 1024)}MB limit."
                        )
                    if on_progress and received - last_ping >= 512 * 1024:
                        last_ping = received
                        on_progress(received, total)
                if on_progress:
                    on_progress(received, total or received)
                logger.info(f"Fetched {received:,} bytes from {current}")
                return b"".join(chunks)

        raise ResolveError("That link redirected too many times.")
