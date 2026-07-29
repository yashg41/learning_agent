"""Per-user Python venv + subprocess code runner for the in-browser notebook.

Security note: code submitted here runs as a subprocess on this server. There
is no jail/seccomp/container — the user accepted this trade-off in exchange
for full package support (qiskit, numpy, matplotlib, etc.). Do not expose
this endpoint to untrusted users.
"""

import asyncio
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from backend.memory import _safe_email, USERS_DIR

logger = logging.getLogger(__name__)

DEFAULT_PACKAGES = ["qiskit", "qiskit-aer", "numpy", "matplotlib"]
DEFAULT_TIMEOUT_SEC = 15
MAX_OUTPUT_BYTES = 50_000


def _venv_dir(email: str) -> Path:
    return Path(USERS_DIR) / _safe_email(email) / "venv"


def _scratch_dir(email: str) -> Path:
    p = Path(USERS_DIR) / _safe_email(email) / "scratch"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _venv_ready(venv: Path) -> bool:
    return _venv_python(venv).exists()


async def _run_subprocess(cmd: list[str], cwd: Path | None = None,
                          env: dict | None = None,
                          timeout: float | None = None) -> tuple[int, bytes, bytes, bool]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2)
        except asyncio.TimeoutError:
            stdout, stderr = b"", b""
    return proc.returncode if proc.returncode is not None else -1, stdout, stderr, timed_out


async def ensure_venv(email: str) -> Path:
    """Create the user's venv (and install default packages) if missing.

    First call for a user takes ~30-60s while pip installs qiskit. Subsequent
    calls are no-ops because the marker file exists.
    """
    venv = _venv_dir(email)
    if _venv_ready(venv):
        return venv

    logger.info("Creating venv for %s at %s", email, venv)
    venv.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err, _ = await _run_subprocess([sys.executable, "-m", "venv", str(venv)])
    if rc != 0:
        raise RuntimeError(f"venv creation failed: {err.decode(errors='replace')}")

    py = _venv_python(venv)
    rc, _, err, _ = await _run_subprocess(
        [str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        timeout=120,
    )
    if rc != 0:
        logger.warning("pip upgrade failed: %s", err.decode(errors="replace")[:500])

    rc, _, err, _ = await _run_subprocess(
        [str(py), "-m", "pip", "install", "--quiet", *DEFAULT_PACKAGES],
        timeout=600,
    )
    if rc != 0:
        raise RuntimeError(f"pip install failed: {err.decode(errors='replace')[:1000]}")

    return venv


async def reset_venv(email: str) -> None:
    venv = _venv_dir(email)
    if venv.exists():
        shutil.rmtree(venv, ignore_errors=True)
    await ensure_venv(email)


def _truncate(b: bytes) -> str:
    if len(b) <= MAX_OUTPUT_BYTES:
        return b.decode(errors="replace")
    head = b[:MAX_OUTPUT_BYTES].decode(errors="replace")
    return head + f"\n... [truncated, total {len(b)} bytes]"


async def run_code(email: str, code: str, timeout: float = DEFAULT_TIMEOUT_SEC) -> dict:
    """Run a code cell in the user's venv. Returns stdout/stderr/exit/timing."""
    venv = await ensure_venv(email)
    py = _venv_python(venv)
    scratch = _scratch_dir(email)

    cell_path = scratch / f"cell_{int(time.time() * 1000)}.py"
    cell_path.write_text(code, encoding="utf-8")

    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(scratch), "MPLBACKEND": "Agg"}

    started = time.time()
    rc, stdout, stderr, timed_out = await _run_subprocess(
        [str(py), str(cell_path)],
        cwd=scratch,
        env=env,
        timeout=timeout,
    )
    duration_ms = int((time.time() - started) * 1000)

    try:
        cell_path.unlink()
    except OSError:
        pass

    extra_err = ""
    if timed_out:
        extra_err = f"\n[killed after {timeout}s timeout]"

    return {
        "stdout": _truncate(stdout),
        "stderr": _truncate(stderr) + extra_err,
        "exit_code": rc,
        "duration_ms": duration_ms,
        "timed_out": timed_out,
    }
