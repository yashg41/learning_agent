"""Tests for the portable SDK-transcript archive.

The CLI's own transcript — not our conversation.jsonl — is what `--resume`
reads, and it lives under ~/.claude rather than in the project. These cover the
mirror that keeps a copy in data/ so the directory can move between machines.
"""

import os

import pytest

from backend import transcripts


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate both roots: the fake CLI dir and the app's users dir."""
    claude = tmp_path / "claude_project"
    users = tmp_path / "users"
    claude.mkdir()
    (users / "learner@x.com" / "sessions" / "s1").mkdir(parents=True)

    monkeypatch.setattr(transcripts, "USERS_DIR", str(users))
    monkeypatch.setattr(
        "backend.replay._get_claude_project_dir", lambda: str(claude)
    )
    monkeypatch.setattr(
        transcripts, "get_session_dir",
        lambda email, sid: str(users / email / "sessions" / sid),
    )
    return claude, users


def test_archive_copies_the_cli_transcript(env):
    claude, users = env
    (claude / "s1.jsonl").write_text('{"type":"user"}\n')

    assert transcripts.archive_session("learner@x.com", "s1") is True
    archived = users / "learner@x.com" / "sessions" / "s1" / "sdk_transcript.jsonl"
    assert archived.read_text() == '{"type":"user"}\n'


def test_archive_is_byte_identical(env):
    """The format is the CLI's, not ours — a rewrite risks an unresumable file."""
    claude, users = env
    raw = '{"parentUuid":null,"type":"user","message":{"role":"user"}}\n' * 5
    (claude / "s1.jsonl").write_text(raw)

    transcripts.archive_session("learner@x.com", "s1")
    archived = users / "learner@x.com" / "sessions" / "s1" / "sdk_transcript.jsonl"
    assert archived.read_text() == raw


def test_archive_skips_when_unchanged(env):
    """A long session must not rewrite megabytes on every turn."""
    claude, _ = env
    (claude / "s1.jsonl").write_text("x\n")

    assert transcripts.archive_session("learner@x.com", "s1") is True
    assert transcripts.archive_session("learner@x.com", "s1") is False


def test_archive_recopies_when_the_transcript_grows(env):
    claude, users = env
    src = claude / "s1.jsonl"
    src.write_text("one\n")
    transcripts.archive_session("learner@x.com", "s1")

    src.write_text("one\ntwo\n")
    assert transcripts.archive_session("learner@x.com", "s1") is True
    archived = users / "learner@x.com" / "sessions" / "s1" / "sdk_transcript.jsonl"
    assert archived.read_text() == "one\ntwo\n"


def test_archive_is_a_noop_without_a_cli_transcript(env):
    assert transcripts.archive_session("learner@x.com", "s1") is False


def test_restore_puts_it_back_when_the_cli_has_none(env):
    claude, users = env
    archived = users / "learner@x.com" / "sessions" / "s1" / "sdk_transcript.jsonl"
    archived.write_text("archived\n")

    assert transcripts.restore_session("learner@x.com", "s1") is True
    assert (claude / "s1.jsonl").read_text() == "archived\n"


def test_restore_never_overwrites_a_live_transcript(env):
    """The CLI's copy may hold turns the archive has not seen yet."""
    claude, users = env
    (claude / "s1.jsonl").write_text("live and newer\n")
    (users / "learner@x.com" / "sessions" / "s1" / "sdk_transcript.jsonl").write_text("stale\n")

    assert transcripts.restore_session("learner@x.com", "s1") is False
    assert (claude / "s1.jsonl").read_text() == "live and newer\n"


def test_restore_all_walks_every_user(env):
    """The migration case: a fresh machine where the CLI has nothing."""
    claude, users = env
    (users / "other@y.com" / "sessions" / "s2").mkdir(parents=True)
    (users / "learner@x.com" / "sessions" / "s1" / "sdk_transcript.jsonl").write_text("a\n")
    (users / "other@y.com" / "sessions" / "s2" / "sdk_transcript.jsonl").write_text("b\n")

    assert transcripts.restore_all() == 2
    assert (claude / "s1.jsonl").read_text() == "a\n"
    assert (claude / "s2.jsonl").read_text() == "b\n"


def test_restore_all_skips_sessions_with_no_archive(env):
    claude, users = env
    (users / "learner@x.com" / "sessions" / "empty").mkdir(parents=True)
    assert transcripts.restore_all() == 0


def test_builder_transcripts_are_archived_outside_sessions(env):
    """A builder session is not a conversation.

    Archiving it under sessions/ would create a directory the UI then lists as
    an empty chat, so it goes in its own folder.
    """
    claude, users = env
    (claude / "b1.jsonl").write_text("builder\n")

    assert transcripts.archive_builder_session("learner@x.com", "b1") is True
    archived = users / "learner@x.com" / "builder_transcripts" / "b1.jsonl"
    assert archived.read_text() == "builder\n"
    assert not (users / "learner@x.com" / "sessions" / "b1").exists()


def test_restore_all_restores_builder_transcripts(env):
    """Refining a migrated demo must resume the builder, not rebuild it."""
    claude, users = env
    bt = users / "learner@x.com" / "builder_transcripts"
    bt.mkdir(parents=True)
    (bt / "b1.jsonl").write_text("builder\n")

    assert transcripts.restore_all() == 1
    assert (claude / "b1.jsonl").read_text() == "builder\n"


def test_archive_failure_does_not_raise(env, monkeypatch):
    """Archiving must never fail a turn the learner already received."""
    claude, _ = env
    (claude / "s1.jsonl").write_text("x\n")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(transcripts.shutil, "copy2", boom)
    assert transcripts.archive_session("learner@x.com", "s1") is False
