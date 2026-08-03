"""Regression tests for the hand-rolled QR encoder.

The encoder is small but unforgiving: a single misplaced module makes a code
that silently fails to scan. These tests pin it against the known-good
`qrcode` package. That package is NOT a runtime dependency — install it only
to run these tests:

    pip install qrcode
    pytest dropzone/test_qr.py

Every bug caught during development was length-dependent (padding parity at
odd data lengths, version-info blocks at v>=7), so coverage sweeps many
payload lengths rather than checking a couple of examples.
"""

import pytest

from dropzone import qr

qrcode = pytest.importorskip("qrcode", reason="reference encoder not installed")

from qrcode.constants import ERROR_CORRECT_L  # noqa: E402
from qrcode.util import MODE_8BIT_BYTE, QRData  # noqa: E402


def our_matrix(payload: str):
    data = payload.encode("utf-8")
    version = qr._pick_version(len(data))
    interleaved = qr._interleave(qr._build_codewords(data, version), version)
    bits = []
    for cw in interleaved:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    return qr._place(version, bits), version


def reference_matrix(payload: str, version: int):
    # Force single-segment byte mode and mask 0 to match our encoder. Left to
    # itself the reference splits input into mixed alphanumeric/byte segments,
    # which is a different but equally valid encoding.
    q = qrcode.QRCode(
        version=version,
        error_correction=ERROR_CORRECT_L,
        box_size=1,
        border=0,
        mask_pattern=0,
    )
    q.add_data(QRData(payload.encode("utf-8"), mode=MODE_8BIT_BYTE))
    q.make(fit=False)
    return [[1 if c else 0 for c in row] for row in q.get_matrix()]


def payload_of_length(n: int) -> str:
    if n <= 10:
        return "a" * n
    return ("https://x-" + ("ab1-" * 90) + ".trycloudflare.com")[:n]


@pytest.mark.parametrize("length", range(1, 230))
def test_matches_reference_encoder(length):
    payload = payload_of_length(length)
    ours, version = our_matrix(payload)
    ref = reference_matrix(payload, version)
    assert ours == ref, f"module mismatch at length {length} (v{version})"


def test_version_information_bits_match_spec():
    """Values from ISO/IEC 18004 Annex D."""
    for version, expected in {
        7: 0x07C94,
        8: 0x085BC,
        9: 0x09A99,
        10: 0x0A4D3,
    }.items():
        assert qr._version_bits(version) == expected


def test_padding_starts_with_ec_then_alternates():
    # Odd data length is what previously flipped the pad order.
    codewords = qr._build_codewords(b"x", 1)
    assert codewords[:5] == [64, 23, 128, 0xEC, 0x11]


def test_svg_is_self_contained_and_well_formed():
    svg = qr.svg("https://example.trycloudflare.com/?t=abc123")
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "http://www.w3.org/2000/svg" in svg
    # No external references — the page is served under a strict CSP.
    assert "<image" not in svg and "href" not in svg


def test_rejects_oversized_payload():
    with pytest.raises(ValueError):
        qr.svg("x" * 400)
