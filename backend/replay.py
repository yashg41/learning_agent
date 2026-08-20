"""Session recovery — rebuilds an SDK session from local memory files.

Supports two restore strategies:
1. Summary-based (fast): uses the compacted summary + only post-summary user messages
2. Transcript-based (fallback): sends full conversation transcript in a single API call
"""

import json
import os
import re
import logging

from backend.config import settings
from backend.memory import USERS_DIR, _safe_email, save_summary, get_summary, get_session_dir

logger = logging.getLogger(__name__)


def _get_claude_project_dir() -> str:
    """Get the .claude project directory path for this app.

    Must match the slug the bundled CLI derives from `options.cwd`
    (see backend.agent.PROJECT_ROOT). The CLI replaces every non-alnum
    char — including underscore — with a dash.

    Rooted at agent.CLAUDE_CONFIG_DIR, which is what the CLI is given as
    CLAUDE_CONFIG_DIR. These two must agree: if this looked in ~/.claude while
    the CLI wrote elsewhere, session_exists_in_claude would answer False for
    every session and each one would silently fall back to a full replay.
    """
    from backend.agent import CLAUDE_CONFIG_DIR, PROJECT_ROOT
    project_slug = re.sub(r"[^a-zA-Z0-9\-]", "-", PROJECT_ROOT)
    if project_slug.startswith("-"):
        project_slug = project_slug[1:]
    return os.path.join(CLAUDE_CONFIG_DIR, "projects", f"-{project_slug}")


def session_exists_in_claude(session_id: str) -> bool:
    """Check if a session .jsonl file exists in ~/.claude/."""
    claude_dir = _get_claude_project_dir()
    session_file = os.path.join(claude_dir, f"{session_id}.jsonl")
    return os.path.isfile(session_file)


def session_exists_in_memory(email: str, session_id: str) -> bool:
    """Check if a session has conversation data in our local data/ folder.

    Probes the path directly rather than via get_session_dir, which creates
    the directory as a side effect — asking whether a session exists should
    not bring it into existence.
    """
    from backend.memory import USERS_DIR, _safe_email

    session_dir = os.path.join(USERS_DIR, _safe_email(email), "sessions", session_id)
    return os.path.isfile(os.path.join(session_dir, "conversation.jsonl")) or os.path.isfile(
        os.path.join(session_dir, "conversation.json")
    )


def read_compact_summary(session_id: str) -> tuple[str, str] | None:
    """Read the session JSONL from .claude/ and find the latest compacted summary.

    Returns (summary_text, timestamp) or None if no compacting has happened.
    """
    claude_dir = _get_claude_project_dir()
    jsonl_path = os.path.join(claude_dir, f"{session_id}.jsonl")

    if not os.path.isfile(jsonl_path):
        return None

    summary_text = None
    summary_timestamp = None

    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("isCompactSummary"):
                message = entry.get("message", {})
                content = message.get("content", "")
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block["text"])
                        elif isinstance(block, str):
                            parts.append(block)
                    content = "\n".join(parts)
                summary_text = content
                summary_timestamp = entry.get("timestamp", "")

    if summary_text and summary_timestamp:
        return (summary_text, summary_timestamp)
    return None


def check_and_save_compact(email: str, session_id: str):
    """Check if the session's JSONL has a compacted summary and save it."""
    result = read_compact_summary(session_id)
    if result:
        summary_text, compacted_at = result
        save_summary(email, session_id, summary_text, compacted_at)
        logger.info(f"Saved compacted summary for {email} (compacted at {compacted_at})")


async def replay_from_memory(email: str, session_id: str, system_prompt: str | None = None) -> str:
    """Restore a user's session from local memory.

    Strategy:
    1. If summary.json exists: send the summary as context (1 API call),
       then replay only post-summary user messages.
    2. If no summary: send the full conversation transcript in a single API call.

    Returns the new session_id from the SDK.
    """
    from backend.agent import run_agent_internal

    from backend.memory import read_turns

    safe = _safe_email(email)

    if not session_exists_in_memory(email, session_id):
        raise FileNotFoundError(f"No local memory found for user {email} session {session_id}")

    # read_turns applies supersede markers, so an edited-away message is never
    # replayed back into the model's context.
    all_turns = read_turns(email, session_id)
    user_messages = [t for t in all_turns if t.get("type") == "user"]

    if not user_messages:
        raise ValueError(f"No user messages found for {email} session {session_id}")

    # Check if we have a compacted summary
    summary_data = get_summary(email, session_id)

    user_data_dir = os.path.join(USERS_DIR, safe)

    if summary_data:
        # Strategy 1: Summary-based restore
        compacted_at = summary_data["compacted_at"]
        summary_text = summary_data["summary"]
        logger.info(f"Restoring {email} from summary (compacted at {compacted_at})")

        post_summary_messages = [
            t["content"] for t in user_messages
            if t.get("timestamp", "") > compacted_at
        ]

        context_prompt = (
            "This session is being restored from a saved summary. "
            "Here is the context from the previous conversation:\n\n"
            f"{summary_text}\n\n"
            "The conversation continues from here. Please acknowledge briefly that you have the context."
        )

        new_session_id = await run_agent_internal(
            prompt=context_prompt,
            session_id=None,
            system_prompt=system_prompt,
            user_data_dir=user_data_dir,
            email=email,
        )

        for i, msg in enumerate(post_summary_messages):
            logger.info(f"Replaying post-summary message {i + 1}/{len(post_summary_messages)}")
            new_session_id = await run_agent_internal(
                prompt=msg,
                session_id=new_session_id,
                user_data_dir=user_data_dir,
                email=email,
            )

        logger.info(f"Summary-based restore complete for {email}. New session: {new_session_id}")
        return new_session_id

    else:
        # Strategy 2: Transcript-based restore
        logger.info(f"No summary for {email} — restoring from transcript ({len(all_turns)} turns)")

        transcript_parts = []
        for turn in all_turns:
            turn_type = turn.get("type", "")
            content = turn.get("content", "")
            if not content:
                continue
            if turn_type == "user":
                transcript_parts.append(f"User: {content}")
            elif turn_type == "result":
                transcript_parts.append(f"Agent: {content}")

        transcript = "\n\n".join(transcript_parts)

        context_prompt = (
            "This session is being restored from a saved conversation transcript. "
            "Below is the previous conversation:\n\n"
            f"{transcript}\n\n"
            "The conversation continues from here. Please acknowledge briefly that you have the context."
        )

        new_session_id = await run_agent_internal(
            prompt=context_prompt,
            session_id=None,
            system_prompt=system_prompt,
            user_data_dir=user_data_dir,
            email=email,
        )

        logger.info(f"Transcript-based restore complete for {email}. New session: {new_session_id}")
        return new_session_id
