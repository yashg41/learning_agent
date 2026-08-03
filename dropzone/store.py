"""In-memory item buffer with SSE fan-out.

Deliberately ephemeral: nothing is written to disk and nothing survives a
restart. The buffer holds whatever was pasted (possibly credentials or exam
material), so persisting it would be a liability, not a feature.
"""

import asyncio
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from dropzone.config import settings


@dataclass
class DropItem:
    id: str
    seq: int
    kind: Literal["text", "image", "file"]
    created_at: float
    mime: str
    size: int
    text: str | None = None
    data: bytes | None = field(default=None, repr=False)
    filename: str | None = None
    source: str = "device"

    def public(self) -> dict:
        """Client-facing shape. Never includes raw bytes — those are fetched
        separately from /api/item/{id}/raw so the JSON stays small."""
        d = {
            "id": self.id,
            "seq": self.seq,
            "kind": self.kind,
            "createdAt": self.created_at,
            "mime": self.mime,
            "size": self.size,
            "source": self.source,
            "filename": self.filename,
        }
        if self.kind == "text":
            # Cap the inline preview; full text comes from the raw endpoint.
            d["text"] = self.text[:2000] if self.text else ""
            d["truncated"] = bool(self.text and len(self.text) > 2000)
        else:
            d["url"] = f"/api/item/{self.id}/raw"
        return d


class Store:
    def __init__(self):
        self._items: deque[DropItem] = deque()
        self._subscribers: set[asyncio.Queue] = set()
        self._seq = 0
        self._lock = asyncio.Lock()

    # --- reads ---

    def _sweep(self):
        """Drop expired items. Called on access rather than on a timer —
        an idle Dropzone doesn't need a background task."""
        cutoff = time.time() - settings.ITEM_TTL_SECONDS
        while self._items and self._items[0].created_at < cutoff:
            self._items.popleft()

    def list_since(self, since: int) -> list[dict]:
        self._sweep()
        return [i.public() for i in self._items if i.seq > since]

    def get(self, item_id: str) -> DropItem | None:
        self._sweep()
        return next((i for i in self._items if i.id == item_id), None)

    @property
    def current_seq(self) -> int:
        return self._seq

    # --- writes ---

    async def add(
        self,
        kind: str,
        mime: str,
        size: int,
        text: str | None = None,
        data: bytes | None = None,
        filename: str | None = None,
        source: str = "device",
    ) -> DropItem:
        async with self._lock:
            self._seq += 1
            item = DropItem(
                id=secrets.token_urlsafe(8),
                seq=self._seq,
                kind=kind,
                created_at=time.time(),
                mime=mime,
                size=size,
                text=text,
                data=data,
                filename=filename,
                source=source,
            )
            self._items.append(item)
            self._evict()

        await self.broadcast({"type": "new", "seq": item.seq, "id": item.id})
        return item

    def _evict(self):
        """Bound the buffer by count and by total bytes, oldest first.
        Caller holds the lock."""
        while len(self._items) > settings.MAX_ITEMS:
            self._items.popleft()

        total = sum(i.size for i in self._items)
        while self._items and total > settings.MAX_TOTAL_BYTES:
            total -= self._items.popleft().size

    async def delete(self, item_id: str) -> bool:
        async with self._lock:
            before = len(self._items)
            self._items = deque(i for i in self._items if i.id != item_id)
            if len(self._items) == before:
                return False
        await self.broadcast({"type": "delete", "id": item_id})
        return True

    async def clear(self) -> int:
        async with self._lock:
            n = len(self._items)
            self._items.clear()
        await self.broadcast({"type": "clear"})
        return n

    # --- SSE fan-out ---

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)

    async def broadcast(self, event: dict):
        # Iterate a copy: a full queue is discarded mid-loop. Dropping the
        # oldest event keeps one suspended tablet from growing memory without
        # bound — the client's polling fallback recovers anything missed.
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    self._subscribers.discard(q)


store = Store()
