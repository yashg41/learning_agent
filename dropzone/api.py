"""Dropzone API routes."""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from dropzone.auth import require_auth
from dropzone.config import settings
from dropzone.store import store

logger = logging.getLogger("dropzone")

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

# Matches the heartbeat cadence used by the learning app's SSE endpoint.
KEEPALIVE_SECONDS = 15


def device_label(request: Request) -> str:
    ua = (request.headers.get("user-agent") or "").lower()
    for needle, label in (
        ("ipad", "iPad"),
        ("iphone", "iPhone"),
        ("android", "Android"),
        ("macintosh", "Mac"),
        ("windows", "Windows"),
    ):
        if needle in ua:
            return label
    return "device"


# Set by run.py once cloudflared reports its address. Held in memory rather
# than a file so it can never go stale across runs.
_public_url = {"url": ""}


def _lan_url() -> str:
    """This machine's address on the local network, for the no-tunnel case."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return f"http://{s.getsockname()[0]}:{settings.DROPZONE_PORT}"
    except OSError:
        return ""
    finally:
        s.close()


@router.get("/public-url")
async def get_public_url():
    """Always hand the page a scannable address, with the QR already rendered.

    The tunnel is preferred because https is what unlocks clipboard access on
    the tablet, but a LAN address is still worth a QR: scanning beats typing
    an IP and a 22-character token by hand.

    The QR travels inline as a data URI rather than as a separate /qr.svg
    request. An <img> fetch does not reliably carry the SameSite=Strict auth
    cookie, so a second request can 401 even though this one succeeded —
    leaving a broken image on an otherwise working page.
    """
    import base64

    from dropzone import qr as qrgen

    tunnel = _public_url["url"]
    base = tunnel or _lan_url()
    target = f"{base}/t?t={settings.DROPZONE_TOKEN}" if base else ""

    qr_data_uri = ""
    if target:
        try:
            svg = qrgen.svg(target)
            encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
            qr_data_uri = f"data:image/svg+xml;base64,{encoded}"
        except ValueError:
            # Payload too long to encode; the page falls back to the text URL.
            qr_data_uri = ""

    return {
        "url": base,
        "target": target,
        "qr": qr_data_uri,
        "token": settings.DROPZONE_TOKEN,
        "secure": bool(tunnel),
    }


@router.post("/public-url")
async def set_public_url(request: Request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    if url and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="tunnel URL must be https")
    _public_url["url"] = url
    logger.info(f"  Tablet:  {url}/t" if url else "  Tunnel URL cleared")
    return {"ok": True}


@router.get("/items")
async def list_items(since: int = 0):
    return {"items": store.list_since(since), "seq": store.current_seq}


@router.post("/text")
async def post_text(request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")

    size = len(text.encode("utf-8"))
    if size > settings.MAX_ITEM_BYTES:
        raise HTTPException(status_code=413, detail="text too large")

    item = await store.add(
        kind="text",
        mime="text/plain",
        size=size,
        text=text,
        source=device_label(request),
    )
    return item.public()


@router.post("/file")
async def post_file(
    request: Request,
    file: UploadFile = File(...),
    filename: str = Form(default=""),
):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > settings.MAX_ITEM_BYTES:
        limit_mb = settings.MAX_ITEM_BYTES // 1024 // 1024
        raise HTTPException(
            status_code=413,
            detail=f"file is {len(data) // 1024 // 1024}MB, limit is {limit_mb}MB",
        )

    mime = (file.content_type or "application/octet-stream").split(";")[0].strip()
    item = await store.add(
        kind="image" if mime.startswith("image/") else "file",
        mime=mime,
        size=len(data),
        data=data,
        filename=filename or file.filename or "pasted",
        source=device_label(request),
    )
    return item.public()


@router.get("/item/{item_id}/raw")
async def get_raw(item_id: str):
    item = store.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="not found")

    if item.kind == "text":
        # Always text/plain. Serving stored content as text/html would be
        # stored XSS against this very origin, which holds the auth cookie.
        return Response(
            content=(item.text or "").encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    return Response(
        content=item.data or b"",
        media_type=item.mime,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{item.filename or item.id}"',
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.delete("/item/{item_id}")
async def delete_item(item_id: str):
    if not await store.delete(item_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@router.delete("/items")
async def clear_items():
    return {"ok": True, "cleared": await store.clear()}


@router.get("/events")
async def events(request: Request):
    """SSE broadcast. Carries only {type, seq, id} — never payload bytes."""
    queue = store.subscribe()

    async def generator():
        try:
            yield "retry: 2000\n\n"
            yield f"data: {json.dumps({'type': 'hello', 'seq': store.current_seq})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    # A silent stream is indistinguishable from a dead one to
                    # both the browser and any intermediate proxy.
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            store.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Discourage proxy buffering, which would otherwise hold events
            # back and defeat realtime delivery.
            "X-Accel-Buffering": "no",
        },
    )
