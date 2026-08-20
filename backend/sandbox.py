"""Per-user Python venv + subprocess code runner for the in-browser notebook.

Execution model: each cell is written to its own run directory and executed as
a fresh `python -u cell.py`. There is no kernel and no shared namespace —
variables do not carry between cells. That is a deliberate choice (see the
README); the UI says so explicitly so a NameError across cells is expected
rather than baffling.

Security note: code submitted here runs as a subprocess on this server. There
is no jail/seccomp/container — the user accepted this trade-off in exchange
for full package support (qiskit, numpy, matplotlib, etc.). Do not expose
this endpoint to untrusted users.
"""

import asyncio
import codecs
import json
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from backend.memory import _safe_email, USERS_DIR

logger = logging.getLogger(__name__)

DEFAULT_PACKAGES = ["qiskit", "qiskit-aer", "numpy", "matplotlib"]
DEFAULT_TIMEOUT_SEC = 15
MAX_OUTPUT_BYTES = 50_000

# A venv is only trusted once this marker lands, and only for this schema.
# Bump VENV_SCHEMA when DEFAULT_PACKAGES changes to force a rebuild.
VENV_MARKER = ".pymentor_ready"
VENV_SCHEMA = 1

# Run directories older than this are swept on the next run.
RUN_DIR_TTL_SEC = 3600

# Ephemeral run files live outside the project tree — see _scratch_dir. Kept as
# a module constant so tests can point it somewhere else.
SCRATCH_ROOT = Path(tempfile.gettempdir()) / "pymentor-scratch"

# Artifacts (plots) a cell may hand back.
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"}
MAX_ARTIFACTS = 20
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024

# Import name -> PyPI distribution name, for the cases where a learner would
# guess wrong. `sklearn` is the one that prompted this: it exists on PyPI as a
# deprecated placeholder that fails to build, so guessing it is worse than not
# guessing at all. Not exhaustive by design — it covers what a
# ModuleNotFoundError is likely to name in this app's subject areas.
IMPORT_TO_PACKAGE = {
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "serial": "pyserial",
    "dateutil": "python-dateutil",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "fitz": "pymupdf",
    "attr": "attrs",
    "Crypto": "pycryptodome",
    "OpenSSL": "pyopenssl",
    "skimage": "scikit-image",
    "torch": "torch",
}

# PyPI names that resolve but are deprecated shims which fail to build. Pip's
# error for these is a wall of setup.py output that buries the one useful line.
DEPRECATED_SHIMS = {"sklearn": "scikit-learn"}

# CSI escape sequences. Nothing here renders colour, so strip them rather than
# letting "\x1b[0;31m" reach the learner as literal garbage.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_MODULE_ERR_RE = re.compile(r"ModuleNotFoundError: No module named '([\w.]+)'")

# One lock per user. Two cells dispatched back-to-back on first use would
# otherwise race two `python -m venv` into the same directory. Single uvicorn
# process, so an asyncio.Lock is sufficient — a file lock would be overkill.
_venv_locks: dict[str, asyncio.Lock] = {}


def _lock_for(email: str) -> asyncio.Lock:
    key = _safe_email(email)
    if key not in _venv_locks:
        _venv_locks[key] = asyncio.Lock()
    return _venv_locks[key]


# --- Paths ---


def _venv_dir(email: str) -> Path:
    return Path(USERS_DIR) / _safe_email(email) / "venv"


def _scratch_dir(email: str) -> Path:
    """Ephemeral per-run files, deliberately OUTSIDE the project tree.

    These directories hold cell.py and sitecustomize.py, written on every
    execution. While they lived under data/ they were inside uvicorn's reload
    scope and matched its default "*.py" filter, so running a cell restarted
    the server and killed any in-flight agent turn (see run.py). run.py now
    also narrows reload_dirs; keeping scratch out of the tree means no future
    widening of that watch can reintroduce the failure.

    Unlike the venv (which stays under data/ — it is expensive to rebuild and
    users expect it to persist), scratch is throwaway: _sweep_run_dirs drops
    anything older than RUN_DIR_TTL_SEC, so a temp location loses nothing.
    """
    p = SCRATCH_ROOT / _safe_email(email)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _venv_ready(venv: Path) -> bool:
    """True only once the install *finished*.

    Checking for the python binary alone (the original behaviour) marks a venv
    ready the instant `python -m venv` returns — i.e. before pip has installed
    anything. A pip failure then left a permanently "ready" but empty venv
    that every later run silently trusted.
    """
    marker = venv / VENV_MARKER
    if not marker.exists() or not _venv_python(venv).exists():
        return False
    try:
        return json.loads(marker.read_text()).get("schema") == VENV_SCHEMA
    except (OSError, ValueError):
        return False


def _new_run_dir(email: str) -> tuple[str, Path]:
    """A fresh directory per execution, so artifacts are scoped and diffable.

    The old scheme (`cell_<epoch_ms>.py` straight into scratch/) collided when
    two cells were dispatched inside the same millisecond — one run deleted
    the other's source mid-execution.
    """
    run_id = f"run_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    d = _scratch_dir(email) / run_id
    d.mkdir(parents=True, exist_ok=True)
    return run_id, d


def run_dir_for(email: str, run_id: str) -> Path:
    return _scratch_dir(email) / run_id


def _sweep_run_dirs(email: str) -> None:
    """Drop run directories older than RUN_DIR_TTL_SEC. Best-effort."""
    cutoff = time.time() - RUN_DIR_TTL_SEC
    try:
        for child in _scratch_dir(email).iterdir():
            if not child.is_dir() or not child.name.startswith("run_"):
                continue
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


# --- Package-name handling ---


def suggest_package(module: str) -> str:
    """Best guess at the PyPI name that provides `module`."""
    return IMPORT_TO_PACKAGE.get(module, module)


def missing_module_from_stderr(stderr: str) -> str | None:
    """Top-level package name from a ModuleNotFoundError traceback, if any.

    `sklearn.ensemble` reports as `sklearn` so the suggestion maps through
    IMPORT_TO_PACKAGE rather than trying to install a submodule.
    """
    m = _MODULE_ERR_RE.search(stderr or "")
    if not m:
        return None
    return m.group(1).split(".")[0]


def normalize_packages(packages: list[str]) -> tuple[list[str], list[str]]:
    """Rewrite deprecated shims to the real distribution.

    Returns (packages, notes) where notes are lines to show the learner.
    Only a bare name is rewritten — if there's a version specifier or an
    extras bracket the learner was explicit, so leave it alone.
    """
    out: list[str] = []
    notes: list[str] = []
    for pkg in packages:
        bare = pkg.strip()
        if bare in DEPRECATED_SHIMS:
            real = DEPRECATED_SHIMS[bare]
            notes.append(
                f"note: installing '{real}' instead of '{bare}' — "
                f"'{bare}' is a deprecated placeholder package on PyPI that fails to build."
            )
            out.append(real)
        else:
            out.append(bare)
    return out, notes


def split_magics(code: str) -> tuple[list[list[str]], str]:
    """Strip `%pip ...` / `!pip ...` lines out of a cell.

    Returns (pip_arg_lists, remaining_code). Recognised at the start of any
    line, not just the first — Jupyter allows them mid-cell.

    Stripped lines are replaced with blanks rather than removed, so the line
    numbers in a traceback still match what the learner is looking at.
    """
    magics: list[list[str]] = []
    lines = code.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped[0] not in "%!":
            continue
        body = stripped[1:].strip()
        # `%%pip` (cell magic) too — same intent, and a learner won't
        # distinguish. Anything that isn't pip/conda is left for Python to
        # complain about, since silently dropping it would be worse.
        if body.startswith("%"):
            body = body[1:].strip()
        try:
            parts = shlex.split(body)
        except ValueError:
            continue
        if not parts:
            continue
        if parts[0] in ("pip", "pip3"):
            magics.append(parts[1:])
        elif parts[0] == "conda" and len(parts) > 1 and parts[1] == "install":
            # No conda in the venv; translate to pip, dropping conda-only flags.
            magics.append(["install"] + [p for p in parts[2:] if not p.startswith("-")])
        else:
            continue
        lines[i] = ""
    return magics, "\n".join(lines)


def packages_from_magic(args: list[str]) -> list[str]:
    """Package names out of a `pip install` arg list (drop flags/subcommand)."""
    if not args or args[0] != "install":
        return []
    return [a for a in args[1:] if not a.startswith("-")]


# --- Process plumbing ---


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


async def _kill_tree(proc) -> None:
    """SIGTERM the child's whole process group, escalating to SIGKILL.

    proc.kill() alone reaps only the direct child, so a cell that spawned a
    subprocess (a pip install, a simulator worker) left an orphan running
    after a timeout or a Stop. start_new_session=True at spawn time is what
    makes the group addressable here.
    """
    if proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, OSError):
        return

    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
        return
    except asyncio.TimeoutError:
        pass

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except asyncio.TimeoutError:
        logger.warning("process %s survived SIGKILL", proc.pid)


async def stream_process(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict | None = None,
    timeout: float | None = None,
    on_chunk: Callable[[str, str], Awaitable[None]] | None = None,
    cancel: asyncio.Event | None = None,
) -> dict:
    """Run `cmd`, invoking on_chunk(stream_name, text) as bytes arrive.

    Replaces the old communicate()-based helper, which buffered to EOF and so
    made live output impossible. Returns
    {exit_code, timed_out, cancelled, stdout, stderr, truncated}.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
        # Own process group, so a timeout or Stop can kill the whole tree.
        start_new_session=True,
    )

    collected: dict[str, list[str]] = {"stdout": [], "stderr": []}
    total = 0
    truncated = False

    async def pump(name: str, stream) -> None:
        nonlocal total, truncated
        # Incremental decoder: a naive chunk.decode() splits multi-byte
        # characters across read boundaries and yields mojibake mid-stream.
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            # read(), not readline(): a progress bar that rewrites with \r and
            # never emits a newline would otherwise stall until it finished.
            chunk = await stream.read(4096)
            if not chunk:
                break
            if truncated:
                continue
            total += len(chunk)
            text = _strip_ansi(dec.decode(chunk))
            if total > MAX_OUTPUT_BYTES:
                truncated = True
                text += f"\n... [truncated at {MAX_OUTPUT_BYTES} bytes]"
            if not text:
                continue
            collected[name].append(text)
            if on_chunk:
                await on_chunk(name, text)

    pumps = asyncio.gather(
        pump("stdout", proc.stdout),
        pump("stderr", proc.stderr),
    )

    async def wait_cancel() -> None:
        if cancel is not None:
            await cancel.wait()

    timed_out = False
    cancelled = False
    waiters = [asyncio.ensure_future(pumps)]
    cancel_task = asyncio.ensure_future(wait_cancel()) if cancel is not None else None
    if cancel_task:
        waiters.append(cancel_task)

    try:
        done, pending = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            timed_out = True
        elif cancel_task is not None and cancel_task in done:
            cancelled = True
    finally:
        if timed_out or cancelled:
            await _kill_tree(proc)
        for w in waiters:
            if not w.done():
                w.cancel()
        # Let the pumps drain whatever the dying process already wrote.
        try:
            await asyncio.wait_for(asyncio.gather(*waiters, return_exceptions=True), timeout=3)
        except asyncio.TimeoutError:
            pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        await _kill_tree(proc)

    return {
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "stdout": "".join(collected["stdout"]),
        "stderr": "".join(collected["stderr"]),
        "truncated": truncated,
    }


async def _run_subprocess(cmd: list[str], cwd: Path | None = None,
                          env: dict | None = None,
                          timeout: float | None = None) -> tuple[int, bytes, bytes, bool]:
    """Buffered wrapper kept for the venv/pip paths that don't stream."""
    r = await stream_process(cmd, cwd=cwd, env=env, timeout=timeout)
    return (
        r["exit_code"],
        r["stdout"].encode(),
        r["stderr"].encode(),
        r["timed_out"],
    )


# --- Venv lifecycle ---


class VenvError(RuntimeError):
    """Environment setup failed — distinct from the learner's code failing."""


async def ensure_venv(
    email: str,
    on_line: Callable[[str, str], Awaitable[None]] | None = None,
) -> Path:
    """Create the user's venv and install DEFAULT_PACKAGES if not already done.

    First call for a user takes ~30-60s while pip installs qiskit; pass
    on_line(stage, text) to surface that progress instead of blocking opaquely.
    Subsequent calls are no-ops because the marker file exists.
    """
    venv = _venv_dir(email)
    if _venv_ready(venv):
        return venv

    async with _lock_for(email):
        # Re-check: another request may have built it while we waited.
        if _venv_ready(venv):
            return venv

        async def emit(stage: str, text: str) -> None:
            if on_line:
                await on_line(stage, text)

        # A venv dir that exists but isn't marked ready is a failed or
        # interrupted build. Start clean rather than pip-ing into the wreckage.
        if venv.exists():
            logger.info("Discarding incomplete venv for %s", email)
            await emit("create", "Discarding incomplete environment...\n")
            shutil.rmtree(venv, ignore_errors=True)

        logger.info("Creating venv for %s at %s", email, venv)
        await emit("create", "Creating your Python environment...\n")
        venv.parent.mkdir(parents=True, exist_ok=True)
        rc, _, err, _ = await _run_subprocess(
            [sys.executable, "-m", "venv", str(venv)], timeout=180
        )
        if rc != 0:
            shutil.rmtree(venv, ignore_errors=True)
            raise VenvError(f"venv creation failed: {err.decode(errors='replace')[:1000]}")

        py = _venv_python(venv)
        rc, _, err, _ = await _run_subprocess(
            [str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
            timeout=120,
        )
        if rc != 0:
            logger.warning("pip upgrade failed: %s", err.decode(errors="replace")[:500])

        await emit("install", f"Installing {', '.join(DEFAULT_PACKAGES)} (this takes a minute)...\n")

        async def on_pip(_stream: str, text: str) -> None:
            await emit("install", text)

        result = await stream_process(
            [str(py), "-m", "pip", "install", "--no-input",
             "--disable-pip-version-check", *DEFAULT_PACKAGES],
            timeout=600,
            on_chunk=on_pip,
        )
        if result["exit_code"] != 0:
            # Leave nothing half-built behind: without this the next run would
            # find a python binary, no marker, and rebuild from scratch anyway
            # — but an operator poking around would see a plausible-looking venv.
            shutil.rmtree(venv, ignore_errors=True)
            tail = (result["stderr"] or result["stdout"])[-1000:]
            raise VenvError(f"pip install failed: {tail}")

        # Marker last: it is the signal that everything above succeeded.
        (venv / VENV_MARKER).write_text(
            json.dumps({
                "schema": VENV_SCHEMA,
                "packages": DEFAULT_PACKAGES,
                "created": time.time(),
            }),
            encoding="utf-8",
        )
        await emit("install", "Environment ready.\n")
        return venv


async def reset_venv(email: str) -> None:
    venv = _venv_dir(email)
    if venv.exists():
        shutil.rmtree(venv, ignore_errors=True)
    await ensure_venv(email)


async def install_packages(
    email: str,
    packages: list[str],
    on_line: Callable[[str], Awaitable[None]] | None = None,
    timeout: float = 600,
) -> dict:
    """pip install into the *user's* venv, streaming pip's output.

    Always `venv/bin/python -m pip` — never bare `pip`, which resolves via
    PATH to the server's own interpreter and installs into the wrong place.
    That mismatch is exactly what defeated the learner who ran
    `subprocess.run(["pip", "install", "sklearn"])` inside a cell.
    """
    packages = [p for p in (p.strip() for p in packages) if p]
    if not packages:
        return {"ok": False, "packages": [], "notes": ["nothing to install"]}

    resolved, notes = normalize_packages(packages)

    venv = await ensure_venv(email)
    py = _venv_python(venv)

    for note in notes:
        if on_line:
            await on_line(note + "\n")

    if on_line:
        await on_line(f"$ pip install {' '.join(resolved)}\n")

    async def on_pip(_stream: str, text: str) -> None:
        if on_line:
            await on_line(text)

    result = await stream_process(
        [str(py), "-m", "pip", "install", "--no-input",
         "--disable-pip-version-check", *resolved],
        timeout=timeout,
        on_chunk=on_pip,
    )
    ok = result["exit_code"] == 0
    if on_line:
        await on_line(
            f"\n{'Installed ' + ', '.join(resolved) if ok else 'Install failed'}.\n"
        )
    return {"ok": ok, "packages": resolved, "notes": notes,
            "exit_code": result["exit_code"]}


# --- Runtime registry (python today; the shape other languages would slot into) ---


@dataclass(frozen=True)
class Runtime:
    name: str
    source_suffix: str
    build_argv: Callable[[Path, Path], list[str]]


PYTHON_RUNTIME = Runtime(
    name="python",
    source_suffix=".py",
    # -u as well as PYTHONUNBUFFERED: belt and braces, since without unbuffered
    # output "streaming" would still deliver everything at process exit.
    build_argv=lambda py, src: [str(py), "-u", str(src)],
)

RUNTIMES: dict[str, Runtime] = {"python": PYTHON_RUNTIME}


# --- Artifacts (matplotlib plots) ---

# Under MPLBACKEND=Agg, plt.show() is a silent no-op — the learner writes
# textbook matplotlib and sees nothing. This shim makes show() save instead.
# It lives in sitecustomize.py (auto-imported, on PYTHONPATH) rather than
# being prepended to the cell, so the learner's traceback line numbers stay
# honest.
_SITECUSTOMIZE = '''\
"""Injected by the PyMentor code runner. Makes plt.show() emit a PNG."""
import builtins

_counter = {"n": 0}


def _patch_pyplot(plt):
    if getattr(plt, "_pymentor_patched", False):
        return
    plt._pymentor_patched = True
    _orig_show = plt.show

    def show(*args, **kwargs):
        fignums = plt.get_fignums()
        if not fignums:
            return
        for num in fignums:
            _counter["n"] += 1
            fig = plt.figure(num)
            try:
                fig.savefig("_plot_%03d.png" % _counter["n"],
                            dpi=110, bbox_inches="tight")
            except Exception:
                pass
        plt.close("all")

    plt.show = show


_real_import = builtins.__import__


def _hook(name, *args, **kwargs):
    mod = _real_import(name, *args, **kwargs)
    if name == "matplotlib.pyplot" or name.startswith("matplotlib.pyplot"):
        try:
            import sys
            _patch_pyplot(sys.modules["matplotlib.pyplot"])
        except Exception:
            pass
    return mod


builtins.__import__ = _hook
'''


def _collect_artifacts(run_dir: Path, before: set[str]) -> list[dict]:
    """Image files the cell produced, newest last, capped."""
    out: list[dict] = []
    try:
        entries = [p for p in run_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
                   and p.name not in before]
    except OSError:
        return out
    entries.sort(key=lambda p: p.stat().st_mtime)
    budget = MAX_ARTIFACT_BYTES
    for p in entries[:MAX_ARTIFACTS]:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > budget:
            break
        budget -= size
        out.append({"name": p.name, "size": size})
    return out


# --- Running a cell ---


async def run_code(
    email: str,
    code: str,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    language: str = "python",
    on_event: Callable[[dict], Awaitable[None]] | None = None,
    cancel: asyncio.Event | None = None,
) -> dict:
    """Run one cell as a fresh process in the user's venv.

    on_event receives the same event dicts the SSE endpoint forwards, so the
    streaming and non-streaming paths share this one implementation.
    """
    runtime = RUNTIMES.get(language)
    if runtime is None:
        raise ValueError(f"unsupported language: {language}")

    async def emit(event: dict) -> None:
        if on_event:
            await on_event(event)

    venv = _venv_dir(email)
    run_id, run_dir = _new_run_dir(email)
    await emit({"type": "start", "run_id": run_id, "language": language,
                "venv_ready": _venv_ready(venv)})

    async def on_venv_line(stage: str, text: str) -> None:
        await emit({"type": "venv", "stage": stage, "line": text})

    venv_path = await ensure_venv(email, on_line=on_venv_line)
    py = _venv_python(venv_path)

    # %pip / !pip lines run before the cell body, against the right pip.
    magics, code_body = split_magics(code)
    for args in magics:
        pkgs = packages_from_magic(args)
        if not pkgs:
            continue
        await emit({"type": "install", "packages": pkgs, "line": ""})

        async def on_install_line(text: str, _pkgs=pkgs) -> None:
            await emit({"type": "install", "packages": _pkgs, "line": text})

        await install_packages(email, pkgs, on_line=on_install_line)

    src = run_dir / f"cell{runtime.source_suffix}"
    src.write_text(code_body, encoding="utf-8")
    (run_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    before = {p.name for p in run_dir.iterdir()}

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(_scratch_dir(email)),
        "MPLBACKEND": "Agg",
        # Without this the pipe block-buffers and "streaming" is a lie.
        "PYTHONUNBUFFERED": "1",
        # Picks up the sitecustomize shim above.
        "PYTHONPATH": str(run_dir),
    }

    async def on_chunk(stream: str, text: str) -> None:
        await emit({"type": stream, "text": text})

    started = time.time()
    result = await stream_process(
        runtime.build_argv(py, src),
        cwd=run_dir,
        env=env,
        timeout=timeout,
        on_chunk=on_chunk,
        cancel=cancel,
    )
    duration_ms = int((time.time() - started) * 1000)

    stderr = result["stderr"]
    if result["timed_out"]:
        stderr += f"\n[killed after {timeout}s timeout]"
    elif result["cancelled"]:
        stderr += "\n[stopped]"

    images = _collect_artifacts(run_dir, before)
    for img in images:
        await emit({
            "type": "image",
            "src": f"/api/code/artifacts/{_safe_email(email)}/{run_id}/{img['name']}",
            "name": img["name"],
        })

    # Only suggest an install when the run actually failed on a missing import.
    suggestion = None
    if result["exit_code"] != 0:
        module = missing_module_from_stderr(stderr)
        if module:
            suggestion = {"module": module, "package": suggest_package(module)}

    _sweep_run_dirs(email)

    done = {
        "type": "done",
        "run_id": run_id,
        "stdout": result["stdout"],
        "stderr": stderr,
        "exit_code": result["exit_code"],
        "duration_ms": duration_ms,
        "timed_out": result["timed_out"],
        "cancelled": result["cancelled"],
        "images": images,
        "suggestion": suggestion,
    }
    await emit(done)
    return done
