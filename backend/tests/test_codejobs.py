"""Tests for background code-run job records.

The sweep is the part worth pinning: a run orphaned by a restart used to leave
an on-disk record saying "building" forever, and get_code_result would keep
telling the model "still running, check again later" with nothing behind it.
"""

import json
import os

import pytest

from backend.demojobs import BUILDING, FAILED, READY


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    """Point the job store at a temp dir and clear in-memory state."""
    import backend.codejobs as cj

    monkeypatch.setattr(cj, "USERS_DIR", str(tmp_path))
    monkeypatch.setattr(cj, "JOBS", {})
    # TASKS is demojobs' registry, shared through spawn(). Patch it there so
    # the name codejobs imported resolves to the same empty dict.
    monkeypatch.setattr(cj, "TASKS", {})
    return tmp_path


def test_orphaned_running_job_is_resolved_to_failed(jobs_dir):
    """The actual bug: JOBS is empty after a restart, so only disk knows."""
    import backend.codejobs as cj

    email = "orphan@example.com"
    record = cj.new_job(email, "print(1)")
    cj.update_job(record["job_id"], state=BUILDING)

    # Simulate a restart: in-memory registry gone, disk record remains.
    cj.JOBS.clear()

    assert cj.sweep_stale() == 1
    after = cj.get_job(email, record["job_id"])
    assert after["state"] == FAILED
    assert "restart" in (after["error"] or "")


def test_sweep_leaves_a_live_job_alone(jobs_dir):
    """A job with a live task is genuinely running, not an orphan."""
    import backend.codejobs as cj

    email = "live@example.com"
    record = cj.new_job(email, "print(1)")
    cj.update_job(record["job_id"], state=BUILDING)
    cj.TASKS[record["job_id"]] = object()  # stand-in for a running asyncio task

    assert cj.sweep_stale() == 0
    assert cj.get_job(email, record["job_id"])["state"] == BUILDING


def test_recent_terminal_job_is_kept(jobs_dir):
    """Inside the TTL a finished job must still be readable after a refresh."""
    import backend.codejobs as cj

    email = "recent@example.com"
    record = cj.new_job(email, "print(1)")
    cj.update_job(record["job_id"], state=READY, stdout="1\n", exit_code=0)

    assert cj.sweep_stale() == 0
    assert cj.get_job(email, record["job_id"])["state"] == READY


def test_expired_terminal_job_is_removed(jobs_dir):
    import backend.codejobs as cj

    email = "old@example.com"
    record = cj.new_job(email, "print(1)")
    cj.update_job(record["job_id"], state=READY)
    # Age it past the TTL on disk and drop it from memory.
    path = cj.verified_path  # noqa: F841 - keep import surface obvious
    job_path = cj._job_path(email, record["job_id"])
    stored = json.load(open(job_path))
    stored["updated_at"] = "2000-01-01T00:00:00+00:00"
    open(job_path, "w").write(json.dumps(stored))
    cj.JOBS.clear()

    assert cj.sweep_stale() == 1
    assert not os.path.exists(job_path)


def test_sweep_is_scoped_when_given_an_email(jobs_dir):
    import backend.codejobs as cj

    a = cj.new_job("a@example.com", "print(1)")
    b = cj.new_job("b@example.com", "print(2)")
    cj.update_job(a["job_id"], state=BUILDING)
    cj.update_job(b["job_id"], state=BUILDING)
    cj.JOBS.clear()

    assert cj.sweep_stale("a@example.com") == 1
    assert cj.get_job("a@example.com", a["job_id"])["state"] == FAILED
    assert cj.get_job("b@example.com", b["job_id"])["state"] == BUILDING


def test_sweep_on_an_empty_store_is_harmless(jobs_dir):
    import backend.codejobs as cj

    assert cj.sweep_stale() == 0
