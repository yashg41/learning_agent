"""Aspect-scoped feedback files — global defaults plus per-user overrides.

The agent reads these on demand via the `get_feedback` MCP tool so rules
are only pulled into context when actually relevant to the task at hand.

Layout:
    feedback/global/<aspect>.md        — applies to every learner
    feedback/users/<safe_email>/<aspect>.md — per-user override

`load_feedback` concatenates global first, then user-specific, so user
rules can extend or override the baseline.
"""

import os
import logging
from pathlib import Path

from backend.memory import _safe_email

logger = logging.getLogger(__name__)

ASPECTS = ("quiz", "suggest_topic", "explain", "general")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEEDBACK_DIR = os.path.join(_REPO_ROOT, "feedback")
GLOBAL_DIR = os.path.join(FEEDBACK_DIR, "global")
USERS_DIR = os.path.join(FEEDBACK_DIR, "users")


_ASPECT_HEADERS = {
    "quiz": "Guidance when generating quiz questions",
    "suggest_topic": "Guidance when suggesting next topics",
    "explain": "Guidance when explaining concepts",
    "general": "Guidance that applies to every interaction",
}


def _default_template(aspect: str) -> str:
    return (
        f"# {_ASPECT_HEADERS[aspect]}\n\n"
        "Add short, imperative rules here. The agent reads this file\n"
        "before performing a related action.\n\n"
        "- Example rule one\n"
        "- Example rule two\n"
    )


def _ensure_dirs() -> None:
    os.makedirs(GLOBAL_DIR, exist_ok=True)
    os.makedirs(USERS_DIR, exist_ok=True)
    for aspect in ASPECTS:
        path = os.path.join(GLOBAL_DIR, f"{aspect}.md")
        if not os.path.isfile(path):
            Path(path).write_text(_default_template(aspect))


def _global_path(aspect: str) -> str:
    return os.path.join(GLOBAL_DIR, f"{aspect}.md")


def _user_path(email: str, aspect: str) -> str:
    return os.path.join(USERS_DIR, _safe_email(email), f"{aspect}.md")


def load_feedback(aspect: str, email: str | None = None) -> str:
    """Return concatenated feedback for an aspect — global first, then user."""
    if aspect not in ASPECTS:
        return ""
    _ensure_dirs()

    parts: list[str] = []

    global_text = Path(_global_path(aspect)).read_text().strip()
    if global_text:
        parts.append(f"## Global guidance\n{global_text}")

    if email:
        path = _user_path(email, aspect)
        if os.path.isfile(path):
            user_text = Path(path).read_text().strip()
            if user_text:
                parts.append(f"## Learner-specific guidance\n{user_text}")

    return "\n\n".join(parts)


def load_global_feedback(aspect: str) -> str:
    if aspect not in ASPECTS:
        return ""
    _ensure_dirs()
    return Path(_global_path(aspect)).read_text()


def load_user_feedback(email: str, aspect: str) -> str:
    if aspect not in ASPECTS:
        return ""
    _ensure_dirs()
    path = _user_path(email, aspect)
    if not os.path.isfile(path):
        return ""
    return Path(path).read_text()


def save_user_feedback(email: str, aspect: str, content: str) -> None:
    if aspect not in ASPECTS:
        raise ValueError(f"Unknown aspect: {aspect}. Must be one of {ASPECTS}")
    _ensure_dirs()
    user_dir = os.path.join(USERS_DIR, _safe_email(email))
    os.makedirs(user_dir, exist_ok=True)
    Path(os.path.join(user_dir, f"{aspect}.md")).write_text(content)
    logger.info(f"Saved feedback: {email} / {aspect} ({len(content)} chars)")
