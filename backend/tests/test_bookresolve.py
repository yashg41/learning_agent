"""URL → book PDF: resolving, and refusing to fetch what it shouldn't.

The SSRF tests are the point of this file. `open_book` lets the TUTOR hand the
server a URL it found by searching, so these are server-side requests with a
model-chosen destination. Nothing here touches the network: DNS is monkeypatched
so a passing suite proves the checks fire before a socket is opened, not that
the internet happened to refuse.
"""

import socket

import pytest

from backend.bookresolve import (
    ResolveError,
    archive_identifier,
    assert_fetchable,
    google_volume_id,
    pick_archive_pdf,
)


@pytest.fixture
def resolves_to(monkeypatch):
    """Point every hostname at an address of our choosing."""
    def _set(ip: str, family=socket.AF_INET):
        def fake_getaddrinfo(host, port, *a, **kw):
            return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 443))]
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return _set


@pytest.fixture
def open_allowlist(monkeypatch):
    """Empty the host allowlist, to test the address checks in isolation."""
    from backend.config import settings

    monkeypatch.setattr(settings, "BOOK_FETCH_ALLOWED_HOSTS", "", raising=False)


# --- Scheme ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://archive.org/download/x/x.pdf",   # plaintext
        "file:///etc/passwd",
        "ftp://archive.org/x.pdf",
        "gopher://archive.org/x",
        "data:application/pdf;base64,JVBERi0=",
        "/etc/passwd",                            # no scheme at all
    ],
)
def test_only_https_is_fetchable(url, open_allowlist):
    with pytest.raises(ResolveError):
        assert_fetchable(url)


# --- Addresses -------------------------------------------------------------


@pytest.mark.parametrize(
    "ip,what",
    [
        ("127.0.0.1", "loopback"),
        ("0.0.0.0", "unspecified"),
        ("10.0.0.1", "private class A"),
        ("172.16.5.4", "private class B"),
        ("192.168.1.1", "private class C"),
        ("169.254.169.254", "cloud metadata endpoint"),
        ("224.0.0.1", "multicast"),
        ("240.0.0.1", "reserved"),
    ],
)
def test_non_public_addresses_are_refused(ip, what, resolves_to, open_allowlist):
    """A public-looking hostname can resolve anywhere — that is the trick."""
    resolves_to(ip)
    with pytest.raises(ResolveError, match="non-public"):
        assert_fetchable("https://totally-innocent.example.com/book.pdf")


@pytest.mark.parametrize("ip", ["::1", "fe80::1", "fc00::1"])
def test_non_public_ipv6_is_refused(ip, resolves_to, open_allowlist):
    resolves_to(ip, family=socket.AF_INET6)
    with pytest.raises(ResolveError, match="non-public"):
        assert_fetchable("https://example.com/book.pdf")


def test_a_public_address_passes(resolves_to, open_allowlist):
    resolves_to("207.241.224.2")          # archive.org, as it happens
    assert assert_fetchable("https://archive.org/download/x/x.pdf")


def test_dns_failure_is_reported_not_swallowed(monkeypatch, open_allowlist):
    def boom(*a, **kw):
        raise socket.gaierror("nope")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(ResolveError, match="Could not look up"):
        assert_fetchable("https://nosuchhost.example/x.pdf")


# --- Allowlist -------------------------------------------------------------


def test_allowlist_blocks_an_unlisted_host(resolves_to, monkeypatch):
    from backend.config import settings

    monkeypatch.setattr(settings, "BOOK_FETCH_ALLOWED_HOSTS", "archive.org", raising=False)
    resolves_to("93.184.216.34")
    with pytest.raises(ResolveError, match="archive.org"):
        assert_fetchable("https://evil.example.com/book.pdf")


def test_allowlist_covers_subdomains(resolves_to, monkeypatch):
    from backend.config import settings

    monkeypatch.setattr(settings, "BOOK_FETCH_ALLOWED_HOSTS", "archive.org", raising=False)
    resolves_to("207.241.224.2")
    assert assert_fetchable("https://ia600504.us.archive.org/x/x.pdf")


def test_a_host_merely_ending_in_the_name_is_not_allowed(resolves_to, monkeypatch):
    """archive.org.evil.com must not pass as archive.org."""
    from backend.config import settings

    monkeypatch.setattr(settings, "BOOK_FETCH_ALLOWED_HOSTS", "archive.org", raising=False)
    resolves_to("93.184.216.34")
    with pytest.raises(ResolveError):
        assert_fetchable("https://archive.org.evil.com/book.pdf")


# --- archive.org -----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://archive.org/details/higersanskritgra00kale",
        "https://archive.org/download/higersanskritgra00kale/x.pdf",
        "https://archive.org/stream/higersanskritgra00kale",
        "https://archive.org/embed/higersanskritgra00kale",
        "https://archive.org/details/higersanskritgra00kale?ref=ol",
    ],
)
def test_archive_identifier_from_every_url_shape(url):
    assert archive_identifier(url) == "higersanskritgra00kale"


def test_archive_identifier_ignores_other_hosts():
    assert archive_identifier("https://example.com/details/x") is None


def test_pick_prefers_a_text_pdf_over_a_scan():
    picked = pick_archive_pdf({"files": [
        {"name": "book_bw.pdf", "format": "Image Container PDF"},
        {"name": "book.pdf", "format": "Text PDF"},
    ]})
    assert picked == ("book.pdf", "Text PDF")


def test_pick_falls_back_to_a_scan():
    picked = pick_archive_pdf({"files": [
        {"name": "book_jp2.zip", "format": "Single Page Processed JP2 ZIP"},
        {"name": "book_bw.pdf", "format": "Image Container PDF"},
    ]})
    assert picked == ("book_bw.pdf", "Image Container PDF")


def test_pick_takes_any_pdf_over_nothing():
    picked = pick_archive_pdf({"files": [{"name": "odd.pdf", "format": "Unknown"}]})
    assert picked == ("odd.pdf", "Unknown")


def test_pick_returns_none_when_there_is_no_pdf():
    """Some items are audio or plain text. Guessing a filename would 404
    later instead of failing usefully now."""
    assert pick_archive_pdf({"files": [{"name": "a.mp3", "format": "VBR MP3"}]}) is None


# --- Google Books ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://books.google.com/books?id=AbC123_x-Y",
        "https://books.google.co.in/books?id=AbC123_x-Y&printsec=frontcover",
        "https://books.google.com/books/edition/Some_Title/AbC123_x-Y",
    ],
)
def test_google_volume_id(url):
    assert google_volume_id(url) == "AbC123_x-Y"


def test_google_volume_id_ignores_other_hosts():
    assert google_volume_id("https://archive.org/details/x?id=y") is None
