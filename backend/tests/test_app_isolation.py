"""Each tutor gets its own process, its own port and its own data root.

The `app` parameter used to be a purely behavioural switch — it chose prompts,
tool families and curriculum tracks, and never touched a filesystem path. Every
store was keyed on the learner's email alone, so the same email in both tutors
meant one knowledge.json, one session list, and one ChromaDB whose episodes are
injected into every system prompt.

These tests pin the structural half: that the derivation from APP actually
produces disjoint roots, and that nothing quietly re-shares one.

Settings is constructed directly rather than mutated globally, so nothing here
depends on which app the test process happens to be running as.
"""

import os

import pytest

from backend.config import _APP_PORTS, Settings
from backend.curriculum import APP_TRACKS


def _settings(app: str) -> Settings:
    """A Settings for one app, ignoring any ambient env that would skew it."""
    return Settings(
        APP=app, DATA_DIR="", CHROMA_DIR="", API_PORT=0, _env_file=None,
    )


@pytest.mark.parametrize("app", sorted(APP_TRACKS))
def test_data_root_is_named_for_the_app(app):
    assert _settings(app).DATA_DIR.endswith(os.path.join("data", app))


@pytest.mark.parametrize("app", sorted(APP_TRACKS))
def test_chroma_lives_inside_the_app_data_root(app):
    """The regression guard for the bug this refactor fixed.

    CHROMA_DIR used to be hardcoded to <repo>/data/chroma, so overriding
    DATA_DIR alone left both tutors sharing one vector store. That is the
    leak least likely to be noticed, because episodic memory is read into
    every system prompt — the model just quietly knows the other tutor's
    conversations.
    """
    s = _settings(app)
    assert os.path.commonpath([s.DATA_DIR, s.CHROMA_DIR]) == s.DATA_DIR


def test_the_two_apps_never_share_a_root():
    roots = [_settings(a).DATA_DIR for a in sorted(APP_TRACKS)]
    assert len(set(roots)) == len(roots), "two apps resolved to the same root"
    for a in roots:
        for b in roots:
            if a is not b:
                # Neither may be a parent of the other, or one app's sweep
                # would walk into the other's users and fail its jobs.
                assert os.path.commonpath([a, b]) != a


def test_an_unknown_app_fails_at_startup():
    """Better than serving a live process against data/sanskirt/."""
    with pytest.raises(ValueError, match="Unknown app"):
        _settings("sanskirt")


def test_an_explicit_data_dir_still_wins():
    """The sentinel-default must not become a hardcoded value.

    Someone moving the data onto a bigger disk sets DATA_DIR and expects
    CHROMA_DIR to follow it, not to stay behind in the repo.
    """
    s = Settings(APP="sanskrit", DATA_DIR="/tmp/elsewhere", CHROMA_DIR="",
                 API_PORT=0, _env_file=None)
    assert s.DATA_DIR == "/tmp/elsewhere"
    assert s.CHROMA_DIR == os.path.join("/tmp/elsewhere", "chroma")


def test_every_app_has_a_port_and_none_collide():
    assert set(_APP_PORTS) == set(APP_TRACKS), "an app has no port assigned"
    assert len(set(_APP_PORTS.values())) == len(_APP_PORTS)


def test_no_app_takes_dropzones_port():
    """Dropzone binds one fixed port with one token. If a tutor took it, each
    app's startup would evict the other's Dropzone in a loop."""
    from dropzone.config import settings as dz

    assert dz.DROPZONE_PORT not in _APP_PORTS.values()


@pytest.mark.parametrize("app", sorted(APP_TRACKS))
def test_shared_data_is_not_app_scoped(app):
    """vidyut's bundle is ~77MB of read-only linguistic data that PyMentor
    never touches. Scoping it per app would duplicate it and, worse, would
    make the Sanskrit tools silently report "could not check" after a split,
    because the bundle would no longer be where they look."""
    s = _settings(app)
    assert not s.SHARED_DATA_DIR.endswith(os.path.join("data", app))
    assert os.path.commonpath([s.SHARED_DATA_DIR, s.DATA_DIR]) == s.SHARED_DATA_DIR


def test_email_is_sanitised_identically_in_both_apps():
    """The migration's whole premise: the same email is the same directory
    NAME in both roots, and it is the root that makes them different people."""
    from backend.memory import _safe_email

    assert _safe_email("yashg41") == "yashg41"
    assert _safe_email("Yashg41 ") == _safe_email("yashg41")
