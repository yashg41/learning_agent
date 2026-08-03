"""Start everything: the Python Learning Agent, Dropzone, and the tunnel.

One command. Run setup.sh once first to install dependencies.

Three processes, because they have genuinely different lifecycles:
  - the learning agent reloads on file changes (uvicorn --reload)
  - Dropzone must NOT reload, or a file save would wipe its in-memory buffer
    mid-study-session
  - cloudflared publishes the https address the tablet needs

Whether you connect a tablet is your choice — the URL and QR are always there.

Escape hatches, none needed for normal use:
  DROPZONE=0   learning agent only
  TUNNEL=0     no tunnel (laptop + same-Wi-Fi LAN only, no clipboard on tablet)
"""

import atexit
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import uvicorn

from backend.config import settings

ROOT = os.path.dirname(os.path.abspath(__file__))

START_DROPZONE = os.environ.get("DROPZONE", "1") not in ("0", "false", "no")
START_TUNNEL = os.environ.get("TUNNEL", "1") not in ("0", "false", "no")

_children: list[subprocess.Popen] = []


def _shutdown():
    for proc in _children:
        if proc.poll() is None:
            proc.terminate()
    for proc in _children:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


atexit.register(_shutdown)


def _wait_for_health(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=1
            ) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    return False


def start_dropzone(dz) -> subprocess.Popen | None:
    # The child would otherwise generate its own random token, so the URLs
    # printed here would not match the ones it accepts.
    env = {**os.environ, "DROPZONE_TOKEN": dz.DROPZONE_TOKEN}
    # Bind all interfaces so a tablet on the same Wi-Fi can reach it even
    # without the tunnel. Loopback-only would make TUNNEL=0 useless.
    env.setdefault("DROPZONE_HOST", "0.0.0.0")

    # Keep the child's output: the common failure here is "address already in
    # use" from a leftover Dropzone, and discarding stderr turns that into a
    # silent no-start with a printed URL that never works.
    log_path = os.path.join(ROOT, ".dropzone.log")
    log = open(log_path, "w+")
    proc = subprocess.Popen(
        [sys.executable, "run_dropzone.py"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    _children.append(proc)
    return proc


def port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def reclaim_port(port: int) -> bool:
    """Free `port` if a previous Dropzone is still holding it.

    A leftover from an earlier run (crashed terminal, forgotten background
    process) would otherwise make the new child exit instantly while the
    banner still advertises a token that nothing is listening for. Only
    processes whose command line is this project's own runner are touched.
    """
    if not port_in_use(port):
        return True

    try:
        pids = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return False

    for pid in pids:
        try:
            cmdline = subprocess.run(
                ["ps", "-p", pid, "-o", "command="],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        # Never kill an unrelated process that happens to hold this port.
        if "run_dropzone.py" not in cmdline and "dropzone.main" not in cmdline:
            print(f"  Port {port} is held by another program — Dropzone skipped.")
            return False
        print(f"  Clearing a leftover Dropzone on port {port} (pid {pid}).")
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass

    for _ in range(20):
        if not port_in_use(port):
            return True
        time.sleep(0.25)
    return not port_in_use(port)


def start_tunnel(port: int) -> tuple[subprocess.Popen, str] | None:
    """Launch cloudflared and wait for it to report its public URL."""
    if not shutil.which("cloudflared"):
        return None

    log_path = os.path.join(ROOT, ".dropzone-tunnel.log")
    log = open(log_path, "w+")
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    _children.append(proc)

    url_pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    # cloudflared prints the URL immediately, but the hostname 502s until an
    # edge connection is actually registered. Some networks block outbound
    # 7844 entirely, in which case the URL is printed and never works — so
    # wait for a registered connection, not just for the URL to appear.
    ready_pattern = re.compile(r"Registered tunnel connection|Connection [0-9a-f-]+ registered")

    url = None
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        log.flush()
        with open(log_path) as f:
            content = f.read()
        if url is None:
            match = url_pattern.search(content)
            if match:
                url = match.group(0)
        if url and ready_pattern.search(content):
            return proc, url
        time.sleep(0.4)

    # Timed out without a registered connection — the tunnel is not usable.
    proc.terminate()
    if proc in _children:
        _children.remove(proc)
    if url:
        print()
        print("  Tunnel could not connect (your network may block outbound")
        print(f"  port 7844). Details: {log_path}")
        print("  Falling back to local Wi-Fi — see the address below.")
    return None


def publish_url(port: int, token: str, url: str):
    """Hand the tunnel address to the running server so its page can show a QR."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/public-url",
        data=f'{{"url":"{url}"}}'.encode(),
        headers={"Content-Type": "application/json", "X-Dropzone-Token": token},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5).close()
    except (urllib.error.URLError, OSError) as e:
        print(f"  (could not register tunnel URL: {e})")


def local_ip() -> str | None:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def banner(dz, tunnel_url: str | None):
    port, token = dz.DROPZONE_PORT, dz.DROPZONE_TOKEN
    print()
    print("  " + "─" * 62)
    print(f"  Learning app   http://{settings.API_HOST}:{settings.API_PORT}")
    print()
    print(f"  Dropzone       http://localhost:{port}/?t={token}")
    if tunnel_url:
        print(f"  On your tablet {tunnel_url}/t?t={token}")
        print("                 (or just scan the QR on the Dropzone page)")
    else:
        ip = local_ip()
        if ip:
            print(f"  On your tablet http://{ip}:{port}/t?t={token}")
            print("                 same Wi-Fi only; no clipboard copy over http")
        if not shutil.which("cloudflared"):
            print("                 run ./setup.sh to enable the https tunnel")
    print("  " + "─" * 62)
    print()


if __name__ == "__main__":
    tunnel_url = None
    dz = None

    # uvicorn's reloader re-executes this module in a child process on every
    # file save. Without this guard each reload would spawn another Dropzone
    # and another tunnel. The reloader sets this variable in its children.
    if os.environ.get("_DROPZONE_SUPERVISOR") == "1":
        START_DROPZONE = False
    os.environ["_DROPZONE_SUPERVISOR"] = "1"

    if START_DROPZONE:
        try:
            from dropzone.config import settings as dz
        except ImportError:
            print("  Dropzone unavailable (import failed) — continuing without it.")
            dz = None

    if dz and not reclaim_port(dz.DROPZONE_PORT):
        dz = None

    if dz:
        start_dropzone(dz)
        if _wait_for_health(dz.DROPZONE_PORT):
            if START_TUNNEL:
                result = start_tunnel(dz.DROPZONE_PORT)
                if result:
                    _, tunnel_url = result
                    publish_url(dz.DROPZONE_PORT, dz.DROPZONE_TOKEN, tunnel_url)
        else:
            # Surface the child's own error rather than a generic timeout —
            # it names the actual cause (port clash, import error, ...).
            print("\n  Dropzone failed to start. Last output:")
            try:
                with open(os.path.join(ROOT, ".dropzone.log")) as f:
                    for line in f.read().strip().splitlines()[-6:]:
                        print("    " + line)
            except OSError:
                print("    (no log available)")
            print()
            dz = None

    if dz:
        banner(dz, tunnel_url)

    try:
        uvicorn.run(
            "backend.main:app",
            host=settings.API_HOST,
            port=settings.API_PORT,
            reload=settings.DEBUG,
        )
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown()
