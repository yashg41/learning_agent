"""Dropzone app: a personal laptop -> tablet clipboard bridge.

Deliberately separate from the learning-app backend. That app binds loopback
and exposes a code-execution endpoint; serving a tablet means binding a
routable interface, and the two must not share a process.
"""

import logging
import os
import socket
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from dropzone import qr
from dropzone.api import router
from dropzone.auth import clear_cookie, is_valid, require_auth, set_cookie, token_from
from dropzone.config import settings

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("dropzone")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Assets change whenever Dropzone is edited, and a browser holding a stale copy
# produces confusing, hard-to-diagnose UI bugs. This is a local tool serving a
# handful of KB, so caching buys nothing worth that cost.
_NO_CACHE = {"Cache-Control": "no-store, must-revalidate"}


def local_ip() -> str | None:
    """Best-effort LAN address. The UDP socket is never actually sent on."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    url = f"http://localhost:{settings.DROPZONE_PORT}/?t={settings.DROPZONE_TOKEN}"
    logger.info("")
    logger.info("  Dropzone ready")
    logger.info(f"  Laptop:  {url}")
    if settings.DROPZONE_HOST == "0.0.0.0" and (ip := local_ip()):
        logger.info(
            f"  LAN:     http://{ip}:{settings.DROPZONE_PORT}/?t={settings.DROPZONE_TOKEN}"
        )
        logger.info("           (plain http: clipboard API unavailable on the tablet)")
    logger.info("")
    yield
    await __import__("dropzone.store", fromlist=["store"]).store.clear()


app = FastAPI(title="Dropzone", lifespan=lifespan)

# No CORS middleware. This is a same-origin app, and the learning-app's
# allow_origins=["*"] would be actively dangerous on a public tunnel.

app.include_router(router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # Referrer-Policy matters specifically here: the bootstrap URL carries the
    # token, and a leaked Referer would hand it to any host the page links to.
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob: data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'",
        )
    return response


def _page(name: str, request: Request) -> Response:
    """Serve a page, exchanging a valid ?t= token for a cookie."""
    if not is_valid(token_from(request)):
        response = HTMLResponse(
            "<h1>Dropzone</h1>"
            "<p>Invalid or missing token.</p>"
            "<p>Open the link printed in your laptop's terminal, or scan the "
            "QR code on the Dropzone page. The token changes each time the "
            "server restarts.</p>",
            status_code=401,
        )
        # Drop any cookie from an earlier run so a reload of a correct link
        # cannot be shadowed by a dead credential.
        clear_cookie(response)
        return response

    response = FileResponse(
        os.path.join(STATIC_DIR, name), media_type="text/html", headers=_NO_CACHE
    )
    set_cookie(response, request)
    return response


@app.get("/")
async def laptop_page(request: Request):
    return _page("index.html", request)


@app.get("/t")
async def tablet_page(request: Request):
    return _page("tablet.html", request)


@app.get("/app.js", dependencies=[Depends(require_auth)])
async def appjs():
    return FileResponse(
        os.path.join(STATIC_DIR, "app.js"),
        media_type="text/javascript",
        headers=_NO_CACHE,
    )


@app.get("/styles.css", dependencies=[Depends(require_auth)])
async def styles():
    return FileResponse(
        os.path.join(STATIC_DIR, "styles.css"),
        media_type="text/css",
        headers=_NO_CACHE,
    )


@app.get("/qr.svg", dependencies=[Depends(require_auth)])
async def qr_svg(url: str):
    """QR for an arbitrary URL — the laptop page passes the tunnel address."""
    try:
        svg = qr.svg(url)
    except ValueError:
        return Response("URL too long to encode", status_code=400)
    return Response(svg, media_type="image/svg+xml")


@app.get("/health")
async def health():
    return {"ok": True}
