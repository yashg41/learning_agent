"""Keep the test suite out of the real data directory.

Tests used to write into the live `data/users/` — a run left ~55 `__test_*__`
directories behind, beside real learners' sessions and knowledge files.

The reason is subtle enough to be worth stating, because it will catch the next
person too: `USERS_DIR` is computed at IMPORT time from `settings.DATA_DIR`

    USERS_DIR = os.path.join(settings.DATA_DIR, "users")   # memory.py

so `monkeypatch.setattr(settings, "DATA_DIR", tmp_path)` inside a test does
nothing at all — the module constant was bound long before the test ran, and
the patch silently has no effect. The store has to be redirected by patching
the MODULE CONSTANTS, which is what this fixture does.

It is session-scoped and autouse: no test has to remember to ask for it, and a
new test file cannot reintroduce the leak by forgetting. Tests that want their
own isolated root (most of them, via tmp_path) still work — they patch the same
attributes again, more narrowly.
"""

import os
import shutil

import pytest

# The live directory this suite must never touch. Resolved from this file so it
# is right regardless of where pytest was invoked.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LIVE_USERS = os.path.join(_REPO_ROOT, "data", "users")


@pytest.fixture(scope="session", autouse=True)
def _isolate_data_root(tmp_path_factory):
    """Point every user-data store at a throwaway root for the whole session."""
    from backend import books, config, demos, derivations, memory, notes

    root = tmp_path_factory.mktemp("data_root")
    users = root / "users"
    users.mkdir()

    config.settings.DATA_DIR = str(root)
    config.settings.CHROMA_DIR = str(root / "chroma")

    # Every module that computes its own USERS_DIR at import. bookjobs and
    # demojobs import theirs inside the function that uses it, so they pick up
    # books.USERS_DIR / demos.USERS_DIR from here without needing their own
    # entry — an accidental demonstration that the function-local import is the
    # more robust form.
    for module in (memory, demos, derivations, notes, books):
        module.USERS_DIR = str(users)

    yield


@pytest.fixture(scope="session", autouse=True)
def _fail_if_tests_touched_live_data():
    """Fail loudly if the suite wrote into the real data/users/.

    A guard rather than a cleanup: silently deleting what a test created would
    hide the leak instead of fixing it, and the whole point is that this is a
    bug in the test, not a mess to tidy. Compares the directory listing before
    and after — the ~55 stray dirs are exactly what this would have caught.
    """
    before = set(os.listdir(_LIVE_USERS)) if os.path.isdir(_LIVE_USERS) else set()
    yield
    after = set(os.listdir(_LIVE_USERS)) if os.path.isdir(_LIVE_USERS) else set()

    leaked = sorted(after - before)
    if leaked:
        for name in leaked:
            shutil.rmtree(os.path.join(_LIVE_USERS, name), ignore_errors=True)
        pytest.fail(
            "tests wrote into the live data directory: "
            + ", ".join(leaked)
            + f"\n(cleaned up, but fix the test — {_LIVE_USERS} is real user data.)"
            + "\nPatch the module constant, not settings.DATA_DIR: USERS_DIR is"
            + " bound at import time. See backend/tests/conftest.py."
        )
