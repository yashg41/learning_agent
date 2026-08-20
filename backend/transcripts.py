"""Portable copies of the CLI's session transcripts.

The bundled CLI keeps its own transcript per session under ~/.claude, and that
file — not our conversation.jsonl — is what `--resume` reads. So the app's data
is split across two roots: everything the learner sees lives in the project,
while the thing that gives the model its memory lives in the home directory.

Moving the CLI's root into the project does not work: setting CLAUDE_CONFIG_DIR
relocates the transcripts but makes the CLI treat the install as
unauthenticated, because subscription credentials come from the macOS Keychain
(see the note on agent.CLAUDE_CONFIG_DIR).

So instead of moving it, mirror it. After each turn the session's raw transcript
is copied next to the app's own copy, and on startup anything present here but
missing from ~/.claude is restored. The project directory becomes the portable
record: copy data/ to another machine, start the app, and sessions resume there.

The copy is deliberately the CLI's file verbatim. It is an internal format we do
not control, so parsing or rewriting it would risk producing something the CLI
refuses to resume. Byte-for-byte is the only version that is safe.
"""

import logging
import os
import shutil

from backend.memory import USERS_DIR, _safe_email, get_session_dir

logger = logging.getLogger(__name__)

# Name inside the session directory, alongside conversation.jsonl and
# metadata.json. Prefixed so it is obvious this one is not ours to edit.
ARCHIVE_NAME = "sdk_transcript.jsonl"


def _claude_path(session_id: str) -> str:
    """Where the CLI keeps this session's transcript."""
    from backend.replay import _get_claude_project_dir

    return os.path.join(_get_claude_project_dir(), f"{session_id}.jsonl")


def archive_path(email: str, session_id: str) -> str:
    """Where we keep the portable copy."""
    return os.path.join(get_session_dir(email, session_id), ARCHIVE_NAME)


def archive_session(email: str, session_id: str) -> bool:
    """Copy this session's CLI transcript into the project. Best-effort.

    Called after a turn completes. Returns True if a copy was made.

    Never raises: failing to archive must not fail a turn the learner has
    already received. A missed copy costs portability, not the conversation.
    """
    src = _claude_path(session_id)
    if not os.path.isfile(src):
        return False
    try:
        dst = archive_path(email, session_id)
        # Only copy when the source actually changed. A turn appends to the
        # transcript, so size differing is enough — and it keeps a long session
        # from rewriting a multi-megabyte file on every turn.
        if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
            return False
        tmp = f"{dst}.tmp"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return True
    except OSError as e:
        logger.warning(f"Could not archive transcript for {session_id}: {e}")
        return False


def restore_session(email: str, session_id: str) -> bool:
    """Put an archived transcript back where the CLI expects it.

    Returns True if a restore happened. Does NOT overwrite an existing CLI
    transcript: that one is live and may be newer than the archive.
    """
    dst = _claude_path(session_id)
    if os.path.isfile(dst):
        return False
    src = archive_path(email, session_id)
    if not os.path.isfile(src):
        return False
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = f"{dst}.tmp"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        logger.info(f"Restored SDK transcript for session {session_id}")
        return True
    except OSError as e:
        logger.warning(f"Could not restore transcript for {session_id}: {e}")
        return False


def _builder_dir(email: str) -> str:
    """Where builder-session transcripts are archived.

    Deliberately NOT under sessions/: a builder session is not a conversation,
    and get_session_dir would create a sessions/<id>/ directory that the UI
    would then list as an empty chat.
    """
    d = os.path.join(USERS_DIR, _safe_email(email), "builder_transcripts")
    os.makedirs(d, exist_ok=True)
    return d


def archive_builder_session(email: str, session_id: str) -> bool:
    """Mirror a demo builder's transcript.

    Refining a demo resumes the builder's own earlier session (see
    demojobs._run_build), so without this a migrated data/ directory would
    rebuild demos from scratch instead of editing them in place.
    """
    src = _claude_path(session_id)
    if not os.path.isfile(src):
        return False
    try:
        dst = os.path.join(_builder_dir(email), f"{session_id}.jsonl")
        if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
            return False
        tmp = f"{dst}.tmp"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return True
    except OSError as e:
        logger.warning(f"Could not archive builder transcript {session_id}: {e}")
        return False


def restore_all() -> int:
    """Restore every archived transcript the CLI is missing. Startup hook.

    This is what makes data/ portable: on a fresh machine the CLI has no
    transcripts at all, and each archived session is copied into place so it
    resumes instead of silently falling back to replay.
    """
    restored = 0
    try:
        users = os.listdir(USERS_DIR)
    except OSError:
        return 0

    for user in users:
        sessions_dir = os.path.join(USERS_DIR, user, "sessions")
        if not os.path.isdir(sessions_dir):
            continue
        try:
            session_ids = os.listdir(sessions_dir)
        except OSError:
            continue
        for session_id in session_ids:
            # `user` is already the on-disk (safe) form; restore_session
            # re-derives the same directory, so passing it through is correct.
            if restore_session(user, session_id):
                restored += 1

        # Builder transcripts, so a migrated demo can still be edited in place
        # rather than rebuilt.
        builder_dir = os.path.join(USERS_DIR, user, "builder_transcripts")
        if not os.path.isdir(builder_dir):
            continue
        try:
            names = os.listdir(builder_dir)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            dst = _claude_path(name[: -len(".jsonl")])
            if os.path.isfile(dst):
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                src = os.path.join(builder_dir, name)
                tmp = f"{dst}.tmp"
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
                restored += 1
            except OSError as e:
                logger.warning(f"Could not restore builder transcript {name}: {e}")
    return restored
