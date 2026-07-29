"""Conversation turn persistence — saves raw conversation data per user.

Supports multiple sessions per user. Session-specific data (conversation,
metadata, summary) lives in data/users/<email>/sessions/<session_id>/.
Shared data (knowledge, quizzes) stays at the user level.

Turns are stored in `conversation.jsonl` — one JSON object per line, appended.
The previous format re-serialized the entire history on every turn (plus a
numbered file per turn under turns/), which cost O(history) per write and lost
turns when two writers raced. Appending is constant-time and crash-safe.

Legacy sessions are converted on first read; see `_ensure_jsonl`.

Editing is modelled as a tombstone rather than a rewrite: a `supersede` line
retracts an earlier turn, and `read_turns` rebuilds the surviving history.
Every reader must go through `read_turns`, or retracted turns leak back out.
"""

import os
import json
import re
import shutil
from datetime import datetime, timezone

from backend.config import settings

USERS_DIR = os.path.join(settings.DATA_DIR, "users")


def _safe_email(email: str) -> str:
    """Sanitize email for use as directory name."""
    return re.sub(r"[^\w@.\-]", "_", email.lower().strip())


def _conversation_paths(session_dir: str) -> tuple[str, str]:
    """Return (jsonl_path, legacy_json_path) for a session directory."""
    return (
        os.path.join(session_dir, "conversation.jsonl"),
        os.path.join(session_dir, "conversation.json"),
    )


def _ensure_jsonl(session_dir: str) -> str:
    """Convert a legacy conversation.json to conversation.jsonl, once.

    Returns the jsonl path. The old file is kept as conversation.json.bak so a
    bad conversion is recoverable. No-op when the jsonl already exists or the
    session is brand new.
    """
    jsonl_path, legacy_path = _conversation_paths(session_dir)
    if os.path.exists(jsonl_path) or not os.path.isfile(legacy_path):
        return jsonl_path

    with open(legacy_path) as f:
        data = json.load(f)

    # Write to a temp file and rename, so an interrupted conversion can't leave
    # a half-written jsonl that would later be mistaken for complete.
    tmp_path = jsonl_path + ".tmp"
    with open(tmp_path, "w") as f:
        for turn in data.get("turns", []):
            f.write(json.dumps(turn, default=str) + "\n")
    os.replace(tmp_path, jsonl_path)
    os.replace(legacy_path, legacy_path + ".bak")
    return jsonl_path


def _read_raw_lines(jsonl_path: str) -> list[dict]:
    """Read every line, skipping blanks and unparseable ones.

    A truncated final line (power loss mid-append) shouldn't make the whole
    session unreadable, so bad lines are dropped rather than raised.
    """
    if not os.path.isfile(jsonl_path):
        return []
    records = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _apply_supersedes(records: list[dict]) -> list[dict]:
    """Resolve tombstones into the surviving history.

    A {"type": "supersede", "from_index": N} line retracts turn N and every
    turn after it — the edit point and everything it led to. Turns appended
    after the marker are the replacement.
    """
    turns: list[dict] = []
    for rec in records:
        if rec.get("type") == "supersede":
            cutoff = rec.get("from_index")
            if cutoff is not None:
                turns = [t for t in turns if t.get("index", 0) < cutoff]
            continue
        turns.append(rec)
    return turns


def read_turns(email: str, session_id: str) -> list[dict]:
    """The single source of truth for a session's visible history.

    Applies legacy migration and supersede resolution. Every caller — the
    history API, replay, backfill — must use this, or superseded turns leak
    back into the UI.
    """
    session_dir = get_session_dir(email, session_id)
    jsonl_path = _ensure_jsonl(session_dir)
    return _apply_supersedes(_read_raw_lines(jsonl_path))


def append_turn_to_session(email: str, session_id: str, turn: dict) -> dict:
    """Append one turn to a session that isn't currently being streamed into.

    Used to fold a side-chat summary into its parent. Assigns the next index
    without disturbing the session's metadata (model, system_prompt), which a
    fresh MemoryStore would otherwise blank.
    """
    session_dir = get_session_dir(email, session_id)
    jsonl_path = _ensure_jsonl(session_dir)

    existing = _apply_supersedes(_read_raw_lines(jsonl_path))
    record = dict(turn)
    record["index"] = max((t.get("index", 0) for t in existing), default=0) + 1
    record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

    with open(jsonl_path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    return record


def supersede_from(email: str, session_id: str, from_index: int, reason: str = "edit") -> None:
    """Retract turn `from_index` and everything after it."""
    session_dir = get_session_dir(email, session_id)
    jsonl_path = _ensure_jsonl(session_dir)
    marker = {
        "type": "supersede",
        "from_index": from_index,
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with open(jsonl_path, "a") as f:
        f.write(json.dumps(marker) + "\n")


def _read_user_json(email: str) -> dict:
    """Read user.json, auto-migrating from old format if needed."""
    safe = _safe_email(email)
    user_path = os.path.join(USERS_DIR, safe, "user.json")
    if not os.path.isfile(user_path):
        return {}
    with open(user_path) as f:
        data = json.load(f)

    # Auto-migrate old format (flat session_id) → new format (sessions list)
    if "session_id" in data and "sessions" not in data:
        data = _migrate_user_json(safe, data)

    return data


def _migrate_user_json(safe_email: str, old_data: dict) -> dict:
    """Migrate old user.json format to multi-session format.

    Moves conversation.json, metadata.json, summary.json, turns/ into
    sessions/<session_id>/ and rewrites user.json.
    """
    user_dir = os.path.join(USERS_DIR, safe_email)
    session_id = old_data.get("session_id", "")
    if not session_id:
        return old_data

    session_dir = os.path.join(user_dir, "sessions", session_id)
    os.makedirs(session_dir, exist_ok=True)

    # Move session-specific files
    for filename in ("conversation.json", "metadata.json", "summary.json"):
        src = os.path.join(user_dir, filename)
        dst = os.path.join(session_dir, filename)
        if os.path.isfile(src) and not os.path.isfile(dst):
            shutil.move(src, dst)

    # Move turns directory
    src_turns = os.path.join(user_dir, "turns")
    dst_turns = os.path.join(session_dir, "turns")
    if os.path.isdir(src_turns) and not os.path.isdir(dst_turns):
        shutil.move(src_turns, dst_turns)

    # Build session name from first user message
    name = _session_name_from_conversation(session_dir)

    now = datetime.now(timezone.utc).isoformat()
    new_data = {
        "email": old_data.get("email", safe_email),
        "active_session": session_id,
        "sessions": [
            {
                "session_id": session_id,
                "name": name,
                "created_at": old_data.get("created_at", now),
                "last_active": old_data.get("updated_at", now),
            }
        ],
        "created_at": old_data.get("created_at", now),
    }

    # Write migrated user.json
    user_path = os.path.join(user_dir, "user.json")
    with open(user_path, "w") as f:
        json.dump(new_data, f, indent=2)

    return new_data


def _session_name_from_conversation(session_dir: str) -> str:
    """Extract a session name from the first user message in the transcript."""
    try:
        turns = _apply_supersedes(_read_raw_lines(_ensure_jsonl(session_dir)))
        for turn in turns:
            if turn.get("type") == "user" and turn.get("content"):
                return turn["content"][:40].strip()
    except Exception:
        pass
    return "Untitled Session"


# =====================================================================
# Session Management
# =====================================================================


def get_user_sessions(email: str) -> list[dict]:
    """Get all sessions for a user. Returns list of session dicts."""
    data = _read_user_json(email)
    return data.get("sessions", [])


def get_active_session(email: str) -> str | None:
    """Get the active session_id for a user. Returns None if no sessions."""
    data = _read_user_json(email)
    return data.get("active_session")


def save_user_session(
    email: str,
    session_id: str,
    name: str = "",
    set_active: bool = True,
    kind: str = "",
    parent_session_id: str = "",
):
    """Add a session to the user's sessions list.

    If the session already exists, updates its last_active timestamp.

    set_active: whether this becomes the user's active session. Side-chats and
        branches pass False — a background conversation must not steal the
        pointer that determines which chat the user returns to.
    kind: "main" (default), "side_chat", or "branch" — lets the sidebar nest
        or filter derived sessions instead of listing them flat.
    parent_session_id: the session this one was forked from, if any.
    """
    safe = _safe_email(email)
    user_dir = os.path.join(USERS_DIR, safe)
    os.makedirs(user_dir, exist_ok=True)
    user_path = os.path.join(user_dir, "user.json")

    data = _read_user_json(email)
    now = datetime.now(timezone.utc).isoformat()

    if not data:
        # Brand new user
        data = {
            "email": email,
            "active_session": session_id,
            "sessions": [],
            "created_at": now,
        }

    # Check if session already exists
    sessions = data.get("sessions", [])
    existing = next((s for s in sessions if s["session_id"] == session_id), None)

    if existing:
        existing["last_active"] = now
        if name:
            existing["name"] = name
        if kind:
            existing["kind"] = kind
        if parent_session_id:
            existing["parent_session_id"] = parent_session_id
    else:
        entry = {
            "session_id": session_id,
            "name": name or "New Session",
            "created_at": now,
            "last_active": now,
        }
        if kind:
            entry["kind"] = kind
        if parent_session_id:
            entry["parent_session_id"] = parent_session_id
        sessions.append(entry)

    data["sessions"] = sessions
    if set_active:
        data["active_session"] = session_id

    with open(user_path, "w") as f:
        json.dump(data, f, indent=2)


def replace_session_id(email: str, old_session_id: str, new_session_id: str):
    """Replace a session's ID in user.json (e.g., when SDK assigns a new ID).

    Preserves the session's name and created_at but updates its ID and last_active.
    Also moves the session directory if it exists.
    """
    safe = _safe_email(email)
    user_dir = os.path.join(USERS_DIR, safe)
    user_path = os.path.join(user_dir, "user.json")
    data = _read_user_json(email)
    if not data:
        return

    sessions = data.get("sessions", [])
    for s in sessions:
        if s["session_id"] == old_session_id:
            s["session_id"] = new_session_id
            s["last_active"] = datetime.now(timezone.utc).isoformat()
            break

    if data.get("active_session") == old_session_id:
        data["active_session"] = new_session_id

    data["sessions"] = sessions
    with open(user_path, "w") as f:
        json.dump(data, f, indent=2)

    # Move session data directory if it exists
    old_dir = os.path.join(user_dir, "sessions", old_session_id)
    new_dir = os.path.join(user_dir, "sessions", new_session_id)
    if os.path.isdir(old_dir) and not os.path.isdir(new_dir):
        shutil.move(old_dir, new_dir)


def set_active_session(email: str, session_id: str) -> bool:
    """Switch the active session for a user. Returns True if found."""
    data = _read_user_json(email)
    sessions = data.get("sessions", [])
    if not any(s["session_id"] == session_id for s in sessions):
        return False

    safe = _safe_email(email)
    user_path = os.path.join(USERS_DIR, safe, "user.json")
    data["active_session"] = session_id
    with open(user_path, "w") as f:
        json.dump(data, f, indent=2)
    return True


def rename_session(email: str, session_id: str, name: str) -> bool:
    """Rename a session. Returns True if found."""
    data = _read_user_json(email)
    sessions = data.get("sessions", [])
    for s in sessions:
        if s["session_id"] == session_id:
            s["name"] = name
            safe = _safe_email(email)
            user_path = os.path.join(USERS_DIR, safe, "user.json")
            with open(user_path, "w") as f:
                json.dump(data, f, indent=2)
            return True
    return False


def get_session_dir(email: str, session_id: str) -> str:
    """Get the session-specific data directory, creating it if needed."""
    safe = _safe_email(email)
    session_dir = os.path.join(USERS_DIR, safe, "sessions", session_id)
    os.makedirs(session_dir, exist_ok=True)
    return session_dir


# =====================================================================
# Summary (per-session)
# =====================================================================


def save_summary(email: str, session_id: str, summary_text: str, compacted_at: str):
    """Save the compacted summary to sessions/<session_id>/summary.json."""
    session_dir = get_session_dir(email, session_id)
    path = os.path.join(session_dir, "summary.json")
    with open(path, "w") as f:
        json.dump({
            "session_id": session_id,
            "compacted_at": compacted_at,
            "summary": summary_text,
        }, f, indent=2)


def get_summary(email: str, session_id: str | None = None) -> dict | None:
    """Read summary.json for a session. Returns dict or None."""
    if not session_id:
        session_id = get_active_session(email)
    if not session_id:
        return None

    session_dir = get_session_dir(email, session_id)
    path = os.path.join(session_dir, "summary.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


# =====================================================================
# User listing
# =====================================================================


def list_users() -> list[dict]:
    """List all users with their metadata."""
    if not os.path.exists(USERS_DIR):
        return []
    users = []
    for name in sorted(os.listdir(USERS_DIR)):
        user_path = os.path.join(USERS_DIR, name, "user.json")
        if os.path.isfile(user_path):
            with open(user_path) as f:
                data = json.load(f)
            # Auto-migrate if old format
            if "session_id" in data and "sessions" not in data:
                data = _migrate_user_json(name, data)
            users.append(data)
    return users


# =====================================================================
# MemoryStore — conversation turn persistence (per-session)
# =====================================================================


class MemoryStore:
    """Writes conversation data to local JSON files per session."""

    def __init__(self, email: str, session_id: str, model: str = "", system_prompt: str | None = None):
        safe = _safe_email(email)
        self.email = email
        self.session_id = session_id
        self.user_dir = os.path.join(USERS_DIR, safe)
        self.session_dir = os.path.join(self.user_dir, "sessions", session_id)
        self.turn_counter = 0
        self.turns: list[dict] = []

        os.makedirs(self.session_dir, exist_ok=True)

        # Load existing state if resuming (migrating the legacy format if this
        # session predates jsonl). turn_counter continues from the highest
        # surviving index rather than len(), so indices stay unique after an
        # edit has retracted a tail.
        self.jsonl_path = _ensure_jsonl(self.session_dir)
        self.turns = _apply_supersedes(_read_raw_lines(self.jsonl_path))
        self.turn_counter = max((t.get("index", 0) for t in self.turns), default=0)

        self._write_metadata(model, system_prompt)

    def record_user_message(self, content: str):
        self._append_turn({"type": "user", "content": content})

    def record_event(self, event: dict):
        event_type = event.get("type")
        if event_type in ("session_init", "done"):
            return
        self._append_turn(dict(event))

    def _append_turn(self, turn: dict):
        self.turn_counter += 1
        turn["index"] = self.turn_counter
        turn["timestamp"] = datetime.now(timezone.utc).isoformat()
        self.turns.append(turn)

        # One append — no full-history rewrite, no per-turn file.
        with open(self.jsonl_path, "a") as f:
            f.write(json.dumps(turn, default=str) + "\n")

        self._update_metadata()

    def _write_metadata(self, model: str = "", system_prompt: str | None = None):
        path = os.path.join(self.session_dir, "metadata.json")
        existing = {}
        if os.path.exists(path):
            with open(path) as f:
                existing = json.load(f)

        now = datetime.now(timezone.utc).isoformat()
        metadata = {
            "email": self.email,
            "session_id": self.session_id,
            "created_at": existing.get("created_at", now),
            "updated_at": now,
            "model": model or existing.get("model", ""),
            "system_prompt": system_prompt or existing.get("system_prompt"),
            "turn_count": self.turn_counter,
        }
        with open(path, "w") as f:
            json.dump(metadata, f, indent=2)

    def _update_metadata(self):
        path = os.path.join(self.session_dir, "metadata.json")
        if not os.path.exists(path):
            return
        with open(path) as f:
            metadata = json.load(f)
        metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        metadata["turn_count"] = self.turn_counter
        with open(path, "w") as f:
            json.dump(metadata, f, indent=2)
