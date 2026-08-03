"""Note documents — a free-positioning canvas per note, stored as JSON.

Layout:
    data/users/<safe_email>/notes/_index.json   — denormalized list for the picker
    data/users/<safe_email>/notes/<note_id>.json — one note

A note is a list of absolutely-positioned blocks (text, sticky, code, image,
shape, arrow) over an optional background document. Blocks carry their own
x/y/w/h, so the canvas has no layout engine — which is the point: the user
places things wherever they like and nothing reflows underneath them.

Images and rasterized PDF pages are NOT stored here. They go through
EpisodicMemory.save_attachment into data/uploads/<safe_email>/ and are served
by GET /api/uploads/{email}/{filename}, the same path chat attachments use.
Notes keep only the URL, so a document stays small enough to rewrite on every
autosave.

Writes go through a temp file + os.replace (as memory.py does): uvicorn runs
with reload=True, and a restart mid-write must never leave a truncated note.
This module holds no mutable state for the same reason — a reload would
discard it.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

from backend.config import settings
from backend.memory import _safe_email

logger = logging.getLogger(__name__)

USERS_DIR = os.path.join(settings.DATA_DIR, "users")

BLOCK_TYPES = ("text", "sticky", "code", "image", "shape", "arrow")

# A single block's content. Generous for prose, but bounded: note documents are
# rewritten whole on every autosave, so an unbounded field would let one block
# make every save slow.
MAX_BLOCK_CONTENT = 100_000
MAX_BLOCKS = 2_000

DEFAULT_CANVAS_HEIGHT = 1200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def notes_dir(email: str) -> str:
    return os.path.join(USERS_DIR, _safe_email(email), "notes")


def _note_path(email: str, note_id: str) -> str:
    return os.path.join(notes_dir(email), f"{note_id}.json")


def _index_path(email: str) -> str:
    return os.path.join(notes_dir(email), "_index.json")


def _ensure_dirs(email: str) -> str:
    d = notes_dir(email)
    os.makedirs(d, exist_ok=True)
    return d


def _write_json(path: str, data: dict) -> None:
    """Atomic write — temp file then rename, so a crash can't truncate."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _read_json(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Unreadable note file {path}: {e}")
        return None


# --- Validation ---


def validate_blocks(blocks: list) -> list:
    """Reject structurally invalid blocks.

    Raises ValueError; the route turns that into a 400. Coordinates are
    coerced to numbers rather than rejected, because a block whose x arrives
    as "12" is still perfectly placeable and failing the whole save over it
    would lose the user's other edits.
    """
    if not isinstance(blocks, list):
        raise ValueError("blocks must be a list")
    if len(blocks) > MAX_BLOCKS:
        raise ValueError(f"too many blocks (max {MAX_BLOCKS})")

    out = []
    for b in blocks:
        if not isinstance(b, dict):
            raise ValueError("each block must be an object")
        btype = b.get("type")
        if btype not in BLOCK_TYPES:
            raise ValueError(f"unknown block type: {btype!r}")

        content = b.get("content") or ""
        if not isinstance(content, str):
            raise ValueError("block content must be a string")
        if len(content) > MAX_BLOCK_CONTENT:
            raise ValueError(f"block content exceeds {MAX_BLOCK_CONTENT} chars")

        clean = {
            "id": str(b.get("id") or f"b_{uuid.uuid4().hex[:8]}"),
            "type": btype,
            "x": float(b.get("x") or 0),
            "y": float(b.get("y") or 0),
            "w": float(b.get("w") or 200),
            "h": float(b.get("h") or 80),
            "z": int(b.get("z") or 0),
            "groupId": b.get("groupId") or None,
            "content": content,
            "style": b.get("style") if isinstance(b.get("style"), dict) else {},
        }
        out.append(clean)
    return out


# --- Index ---


def _index_entry(note: dict) -> dict:
    return {
        "id": note["id"],
        "title": note.get("title") or "Untitled note",
        "updated_at": note.get("updated_at"),
        "pinned_session_id": note.get("pinned_session_id"),
        "has_doc": bool(note.get("doc")),
    }


def rebuild_index(email: str) -> list[dict]:
    """Regenerate _index.json by scanning the notes directory.

    The index is a derived cache; the note files are the truth. Used when the
    index is missing or unreadable rather than failing the list call.
    """
    d = _ensure_dirs(email)
    entries = []
    for name in os.listdir(d):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        note = _read_json(os.path.join(d, name))
        if note and note.get("id"):
            entries.append(_index_entry(note))
    entries.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
    _write_json(_index_path(email), {"notes": entries})
    return entries


def list_notes(email: str) -> list[dict]:
    _ensure_dirs(email)
    data = _read_json(_index_path(email))
    if data is None:
        return rebuild_index(email)
    return data.get("notes", [])


def _touch_index(email: str, note: dict) -> None:
    """Upsert one note's entry, keeping newest-first order."""
    entries = [e for e in list_notes(email) if e.get("id") != note["id"]]
    entries.insert(0, _index_entry(note))
    _write_json(_index_path(email), {"notes": entries})


# --- CRUD ---


def create_note(email: str, title: str = "") -> dict:
    _ensure_dirs(email)
    now = _now()
    note = {
        "id": f"n_{uuid.uuid4().hex[:10]}",
        "title": title or "Untitled note",
        "created_at": now,
        "updated_at": now,
        "pinned_session_id": None,
        "canvas_height": DEFAULT_CANVAS_HEIGHT,
        "next_z": 1,
        "doc": None,
        "blocks": [],
        "groups": {},
    }
    _write_json(_note_path(email, note["id"]), note)
    _touch_index(email, note)
    return note


def get_note(email: str, note_id: str) -> dict | None:
    return _read_json(_note_path(email, note_id))


def save_note(email: str, note_id: str, **fields) -> dict | None:
    """Partial update. Only keys present (and not None) are applied.

    Returns the saved note, or None if it doesn't exist so the route can 404.
    Partial rather than whole-document so a rename doesn't have to ship the
    block array, and an autosave doesn't have to know the title.
    """
    note = get_note(email, note_id)
    if note is None:
        return None

    for key in ("title", "canvas_height", "next_z", "blocks", "groups", "doc"):
        if fields.get(key) is not None:
            # Normalize here, not only in the route: every block needs an id
            # and numeric geometry, and a caller reaching this function
            # directly must not be able to persist a malformed block.
            note[key] = validate_blocks(fields[key]) if key == "blocks" else fields[key]

    note["updated_at"] = _now()
    _write_json(_note_path(email, note_id), note)
    _touch_index(email, note)
    return note


def delete_note(email: str, note_id: str) -> bool:
    """Delete the note document.

    Deliberately does NOT unlink referenced images: they live in the shared
    data/uploads/<email>/ tree and the same file may be referenced by a chat
    turn. Deleting a note must never break a conversation's history.
    """
    path = _note_path(email, note_id)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    rebuild_index(email)
    return True


# --- Pinning ---


def pin_note(email: str, note_id: str, session_id: str | None) -> bool:
    """Pin a note to a chat session, or unpin when session_id is None.

    Clears the same session from any other note, so the "one note per session"
    invariant holds even if two tabs pin concurrently — enforcing it here
    rather than client-side is what makes that safe.
    """
    note = get_note(email, note_id)
    if note is None:
        return False

    if session_id:
        for entry in list_notes(email):
            if entry["id"] == note_id:
                continue
            if entry.get("pinned_session_id") == session_id:
                other = get_note(email, entry["id"])
                if other:
                    other["pinned_session_id"] = None
                    _write_json(_note_path(email, other["id"]), other)
                    _touch_index(email, other)

    note["pinned_session_id"] = session_id
    note["updated_at"] = _now()
    _write_json(_note_path(email, note_id), note)
    _touch_index(email, note)
    return True


def get_pinned(email: str, session_id: str) -> str | None:
    """Find the note pinned to a session, following branch ancestry.

    Editing a message forks a new session (routes.py branch endpoint), so an
    exact match would lose the pin the moment a user edits anything. The
    parent chain is walked so a branched conversation keeps showing the notes
    that were taken on its parent — that is what "the same conversation"
    means to the person who wrote them.
    """
    from backend.memory import get_user_sessions

    entries = list_notes(email)

    def find(sid: str) -> str | None:
        for e in entries:
            if e.get("pinned_session_id") == sid:
                return e["id"]
        return None

    hit = find(session_id)
    if hit:
        return hit

    sessions = {s["session_id"]: s for s in get_user_sessions(email)}
    seen = {session_id}
    cur = sessions.get(session_id)
    while cur:
        parent = cur.get("parent_session_id")
        # Guard against a cycle in the recorded ancestry: a malformed
        # user.json must not spin here forever.
        if not parent or parent in seen:
            return None
        hit = find(parent)
        if hit:
            return hit
        seen.add(parent)
        cur = sessions.get(parent)
    return None


# --- Documents (PDF background) ---


def set_document(email: str, note_id: str, doc: dict | None) -> dict | None:
    """Attach or clear a note's background document.

    Clearing keeps the blocks: a wrong-file import should be recoverable
    without throwing away annotations already written on top of it.
    """
    note = get_note(email, note_id)
    if note is None:
        return None
    note["doc"] = doc
    note["updated_at"] = _now()
    _write_json(_note_path(email, note_id), note)
    _touch_index(email, note)
    return note
