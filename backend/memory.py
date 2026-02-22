"""Conversation turn persistence — saves raw conversation data per user.

Supports multiple sessions per user. Session-specific data (conversation,
metadata, summary, turns) lives in data/users/<email>/sessions/<session_id>/.
Shared data (knowledge, quizzes) stays at the user level.
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
    """Extract a session name from the first user message in conversation.json."""
    conv_path = os.path.join(session_dir, "conversation.json")
    if not os.path.isfile(conv_path):
        return "Untitled Session"
    try:
        with open(conv_path) as f:
            data = json.load(f)
        for turn in data.get("turns", []):
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


def save_user_session(email: str, session_id: str, name: str = ""):
    """Add a session to the user's sessions list and set it as active.

    If the session already exists, updates its last_active timestamp.
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
    else:
        sessions.append({
            "session_id": session_id,
            "name": name or "New Session",
            "created_at": now,
            "last_active": now,
        })

    data["sessions"] = sessions
    data["active_session"] = session_id

    with open(user_path, "w") as f:
        json.dump(data, f, indent=2)


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
        self.turns_dir = os.path.join(self.session_dir, "turns")
        self.turn_counter = 0
        self.turns: list[dict] = []

        os.makedirs(self.turns_dir, exist_ok=True)

        # Load existing state if resuming
        conv_path = os.path.join(self.session_dir, "conversation.json")
        if os.path.exists(conv_path):
            with open(conv_path) as f:
                data = json.load(f)
                self.turns = data.get("turns", [])
                self.turn_counter = len(self.turns)

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

        filename = self._turn_filename(turn)
        with open(os.path.join(self.turns_dir, filename), "w") as f:
            json.dump(turn, f, indent=2, default=str)

        self._write_conversation()
        self._update_metadata()

    def _turn_filename(self, turn: dict) -> str:
        idx = str(turn["index"]).zfill(3)
        t = turn.get("type", "unknown")
        suffix = ""
        if t == "tool_call" and "tool_name" in turn:
            safe_name = re.sub(r"[^\w]", "_", turn["tool_name"])
            suffix = f"_{safe_name}"
        return f"{idx}_{t}{suffix}.json"

    def _write_conversation(self):
        path = os.path.join(self.session_dir, "conversation.json")
        with open(path, "w") as f:
            json.dump({
                "email": self.email,
                "session_id": self.session_id,
                "turns": self.turns,
            }, f, indent=2, default=str)

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
