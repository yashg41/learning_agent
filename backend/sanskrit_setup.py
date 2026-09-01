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
    # SHARED_DATA_DIR, not DATA_DIR: the bundle is ~77MB of read-only
    # linguistic data and is identical for every app, so it lives beside
    # the per-app roots rather than inside one of them.
    return Path(settings.SHARED_DATA_DIR) / "vidyut"


def is_installed() -> bool:
    d = data_dir()
    return (d / MARKER).is_file()


def adopt(source: Path | None = None) -> bool:
    """Accept a bundle that was placed (or unzipped) by hand.

    Downloading is blocked on some machines by a TLS-inspecting proxy that no
    amount of client code can talk past, so copying the file across from
    somewhere else has to be a first-class path rather than a workaround.

    Validates the shape before writing the marker: a half-extracted bundle is
    worse than none, because the tools would half-work and report nonsense
    rather than saying they are unavailable.

    `source` may be the zip itself or a directory holding the extracted files.
    Defaults to looking inside the target directory, which is where someone
    following the printed instructions will have put things.
    """
    target = data_dir()
    target.mkdir(parents=True, exist_ok=True)

    if source and source.is_file() and source.suffix == ".zip":
        print(f"Extracting {source.name}…")
        with zipfile.ZipFile(source) as z:
            for name in z.namelist():
                pth = Path(name)
                if pth.is_absolute() or ".." in pth.parts:
                    raise RuntimeError(f"refusing unsafe path in archive: {name}")
            z.extractall(target)
    elif source and source.is_dir() and source.resolve() != target.resolve():
        print(f"Copying from {source}…")
        for child in source.iterdir():
            dest = target / child.name
            if dest.exists():
                continue
            if child.is_dir():
                shutil.copytree(child, dest)
            else:
                shutil.copy2(child, dest)

    # The archive nests everything under one directory; flatten so the paths
    # the tools use are the same however the bundle arrived.
    entries = [p for p in target.iterdir() if p.name != MARKER]
    if len(entries) == 1 and entries[0].is_dir() and entries[0].name != "kosha":
        inner = entries[0]
        for child in inner.iterdir():
            shutil.move(str(child), str(target / child.name))
        inner.rmdir()

    # The bundle is one directory per component. `kosha` and `prakriya` are the
    # two the tools actually load, so their presence is what makes a bundle
    # usable; the rest are checked only to report a partial extract clearly.
    # (Checked against the real 0.4.0 archive, which contains chandas, cheda,
    # kosha, prakriya and sandhi — an earlier guess at `dhatupatha.tsv` at the
    # top level was wrong and would have rejected a perfectly good bundle.)
    required = ("kosha", "prakriya")
    missing = [n for n in required if not (target / n).exists()]
    if missing:
        found = ", ".join(sorted(p.name for p in target.iterdir())) or "(nothing)"
        print(f"\nThat does not look like the vidyut bundle — missing: {', '.join(missing)}")
        print(f"Found in {target}: {found}")
        print("\nUnzip data-0.4.0.zip so that <dir>/kosha and <dir>/prakriya exist.")
        return False

    (target / MARKER).write_text(VIDYUT_VERSION, encoding="utf-8")
    print(f"Adopted the bundle at {target}")
    return True


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
        + _diagnose()
        + f"\nTo install it by hand:\n"
        f"  1. download {DATA_URL}\n"
        f"  2. unzip it so that <dir>/kosha and <dir>/dhatupatha.tsv exist\n"
        f"  3. put it at {data_dir()}, or set VIDYUT_DATA_DIR in .env\n"
        f"  4. re-run: python -m backend.sanskrit_setup --check"
    )


def _diagnose() -> str:
    """Work out WHY the download failed, so the fix is obvious.

    Worth the extra requests: "it failed" sends you hunting through Python's
    TLS stack, when the actual cause is usually a corporate proxy that allows
    github.com and blocks the host release downloads actually come from. That
    is not a bug anyone can fix in this file, and knowing it saves an hour.
    """
    import socket

    def reachable(host: str) -> bool:
        try:
            with socket.create_connection((host, 443), timeout=6):
                return True
        except OSError:
            return False

    github_ok = reachable("github.com")
    assets_ok = reachable("release-assets.githubusercontent.com")

    if not github_ok:
        return "  You appear to be offline — github.com is unreachable.\n"

    if github_ok and not assets_ok:
        return (
            "  DIAGNOSIS: github.com is reachable but\n"
            "  release-assets.githubusercontent.com is NOT. That host is where\n"
            "  GitHub release downloads actually come from, and a corporate\n"
            "  proxy or VPN is blocking it.\n\n"
            "  Nothing in this script can work around that. Either:\n"
            "    · run this again on a different network (home wifi is usually\n"
            "      enough), or\n"
            "    · download the file on another machine and copy it across.\n"
        )

    # Both hosts resolve, so this is TLS interception rather than a block: the
    # proxy re-signs certificates with a root Python's bundled CA store has
    # never heard of, even though the system keychain trusts it.
    return (
        "  DIAGNOSIS: the hosts are reachable, so this is most likely TLS\n"
        "  interception — a proxy re-signing certificates with a root that\n"
        "  Python does not trust, even where the system does. Check with:\n"
        "    echo | openssl s_client -connect release-assets.githubusercontent.com:443 \\\n"
        "      2>/dev/null | grep issuer=\n"
        "  If the issuer is not a public CA, that is your proxy.\n"
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
    first = d["rows"][0]["sg"] if d.get("rows") else "—"
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
    ap.add_argument(
        "--adopt", metavar="PATH", nargs="?", const="",
        help="use a bundle downloaded by hand: pass the .zip, a directory, or "
             "nothing if you already unzipped it into the data directory",
    )
    args = ap.parse_args()

    print("Sanskrit tooling")
    print("=" * 52)

    if args.adopt is not None:
        src = Path(args.adopt).expanduser() if args.adopt else None
        if not adopt(src):
            return 1
        print()
    elif not args.check:
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
