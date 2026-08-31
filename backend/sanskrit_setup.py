"""Download vidyut's linguistic data, and report what that unlocks.

    python -m backend.sanskrit_setup          # download + verify
    python -m backend.sanskrit_setup --check  # verify only, no download

WHY THIS IS A SEPARATE STEP
The `vidyut` wheel ships the grammar engine but not the Dhātupāṭha, the kośa or
the segmentation model — those are a ~32 MB bundle on GitHub releases. Without
them four tools work and two do not, and one specific failure is nasty: for a
root whose stem substitution lives in the Dhātupāṭha (√gam → gacch), vidyut
returns `gamati` rather than an error. Plausible, and wrong. sanskritverify's
KNOWN_ROOTS whitelist exists to refuse those until this has been run.

WHY NOT vidyut.download_data()
It exists, and on a normal network it is what you would call. It fails behind a
TLS-inspecting proxy — verified, `CERTIFICATE_VERIFY_FAILED` on a self-signed
chain — and its error says nothing about what to do next. This tries urllib,
then certifi, then curl, and if all three fail prints the manual instructions
rather than leaving you to guess.
"""

import argparse
import logging
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from backend.config import settings

logger = logging.getLogger(__name__)

VIDYUT_VERSION = "0.4.0"
DATA_URL = (
    f"https://github.com/ambuda-org/vidyut/releases/download/"
    f"py-{VIDYUT_VERSION}/data-{VIDYUT_VERSION}.zip"
)

# A partial extract is worse than none: the tools would half-work and report
# nonsense. The marker is written last, so its presence means "complete".
MARKER = ".vidyut_ready"


def data_dir() -> Path:
    """Where the bundle lives. Configurable so it can be baked into an image."""
    if settings.VIDYUT_DATA_DIR:
        return Path(settings.VIDYUT_DATA_DIR)
    return Path(settings.DATA_DIR) / "vidyut"


def is_installed() -> bool:
    d = data_dir()
    return (d / MARKER).is_file()


def _fetch(url: str, dest: Path) -> None:
    """Download, trying three transports before giving up.

    Ordered by how little they assume: the stdlib first, then certifi's CA
    bundle (fixes a machine whose system store has been replaced), then curl
    (which often has its own store and succeeds where Python does not).
    """
    errors = []

    try:
        with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return
    except Exception as e:
        errors.append(f"urllib: {type(e).__name__}")

    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(url, timeout=300, context=ctx) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return
    except Exception as e:
        errors.append(f"certifi: {type(e).__name__}")

    if shutil.which("curl"):
        try:
            subprocess.run(
                ["curl", "-fsSL", "--max-time", "600", "-o", str(dest), url],
                check=True, capture_output=True,
            )
            # curl -f still writes a body on some proxy errors; a real zip is
            # tens of MB, so anything tiny is an error page, not data.
            if dest.stat().st_size > 1_000_000:
                return
            errors.append(f"curl: got {dest.stat().st_size} bytes (proxy error page?)")
        except subprocess.CalledProcessError as e:
            errors.append(f"curl: exit {e.returncode}")
    else:
        errors.append("curl: not installed")

    raise RuntimeError(
        "Could not download the data bundle. Tried — " + "; ".join(errors) + ".\n\n"
        f"Download it by hand instead:\n"
        f"  1. {DATA_URL}\n"
        f"  2. unzip it so that <dir>/kosha and <dir>/dhatupatha.tsv exist\n"
        f"  3. put it at {data_dir()}, or set VIDYUT_DATA_DIR in .env\n"
        f"  4. re-run: python -m backend.sanskrit_setup --check"
    )


def install() -> Path:
    """Download and extract the bundle. Returns the directory."""
    target = data_dir()
    if is_installed():
        print(f"Already installed at {target}")
        return target

    target.mkdir(parents=True, exist_ok=True)
    print(f"Downloading vidyut data ({VIDYUT_VERSION}, ~32 MB)…")

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "data.zip"
        _fetch(DATA_URL, archive)
        size = archive.stat().st_size
        print(f"  got {size / 1e6:.1f} MB, extracting…")

        with zipfile.ZipFile(archive) as z:
            # Refuse absolute paths and traversal before writing anything: this
            # archive is trusted, but a zip extract that can escape its target
            # is a bad habit to leave in a codebase.
            for name in z.namelist():
                p = Path(name)
                if p.is_absolute() or ".." in p.parts:
                    raise RuntimeError(f"refusing unsafe path in archive: {name}")
            z.extractall(target)

    # The archive nests everything under one directory; flatten it so the paths
    # below are stable regardless of how it was packed.
    entries = [p for p in target.iterdir() if p.name != MARKER]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for child in inner.iterdir():
            shutil.move(str(child), str(target / child.name))
        inner.rmdir()

    (target / MARKER).write_text(VIDYUT_VERSION, encoding="utf-8")
    print(f"Installed to {target}")
    return target


def check() -> bool:
    """Report what works right now. Returns True if everything does."""
    ok = True

    try:
        import vidyut  # noqa: F401
        print(f"  vidyut package        OK ({vidyut.__version__})")
    except ImportError:
        print("  vidyut package        MISSING — pip install vidyut")
        return False

    from backend import sanskritverify as sv

    print(f"  transliteration       {'OK' if sv.is_available() else 'FAILED'}")

    v = sv.derive_verb("BU")
    print(f"  verb derivation       {'OK' if v['verified'] else 'FAILED'}"
          f"  (√bhū → {', '.join(v.get('forms', [])) or '—'})")
    ok &= bool(v["verified"])

    d = sv.decline("rAma", "m")
    first = d["rows"][0]["eka"]["devanagari"] if d.get("rows") else "—"
    print(f"  declension            {'OK' if d['verified'] else 'FAILED'}  (rāma nom. sg. → {first})")
    ok &= bool(d["verified"])

    m = sv.scan_metre("धर्मक्षेत्रे कुरुक्षेत्रे")
    print(f"  metre scansion        {'OK' if m['verified'] else 'FAILED'}"
          f"  ({m['padas'][0]['weights'] if m.get('padas') else '—'})")
    ok &= bool(m["verified"])

    target = data_dir()
    if is_installed():
        print(f"  data bundle           OK ({target})")
        print(f"  irregular roots       OK (√gam, √sthā, √dṛś available)")
    else:
        ok = False
        print(f"  data bundle           NOT INSTALLED ({target})")
        print( "  irregular roots       unavailable — √gam, √sthā, √dṛś refused")
        print( "  dictionary lookup     unavailable")
        print( "  segmentation          falls back to the Samsaadhanii API")

    return ok


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="verify only, do not download")
    args = ap.parse_args()

    print("Sanskrit tooling")
    print("=" * 52)

    if not args.check:
        try:
            install()
        except Exception as e:
            print(f"\n{e}\n")
            print("Continuing with a status check of what does work:\n")
        print()

    complete = check()
    print("=" * 52)
    if complete:
        print("Everything is available.")
        return 0
    print("Some features are unavailable — see above. The tutor will say so")
    print("rather than guessing, so it is safe to use meanwhile.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
