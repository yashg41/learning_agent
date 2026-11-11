"""Background book fetches.

Mirrors test_codejobs.py. The ownership check and the restart sweep are the
two that matter: one is a cross-tenant read, the other is a chip that spins
forever in the reader.
"""

import asyncio
import json
import os
import shutil

import pytest

from backend import bookjobs
from backend.books import books_dir


@pytest.fixture
def email(tmp_path_factory):
    who = f"__test_{tmp_path_factory.mktemp('bj').name}__"
    yield who
    shutil.rmtree(books_dir(who), ignore_errors=True)
    for job_id in list(bookjobs.JOBS):
        bookjobs.JOBS.pop(job_id, None)


def test_new_job_starts_queued(email):
    record = bookjobs.new_job(email, "https://archive.org/details/x", "Kāle")
    assert record["state"] == bookjobs.QUEUED
    assert record["book_id"] is None
    assert record["job_id"].startswith("bj_")
    assert record["progress"] == {"received": 0, "total": None}


def test_job_is_persisted_and_listed(email):
    record = bookjobs.new_job(email, "https://archive.org/details/x")
    assert os.path.isfile(bookjobs._job_path(email, record["job_id"]))
    assert [j["job_id"] for j in bookjobs.list_jobs(email)] == [record["job_id"]]


def test_get_job_refuses_another_users_id(email):
    """JOBS is keyed by job_id alone. Without the ownership check one learner's
    id reads another's record — including the URL they asked for — and
    request_cancel routes through here."""
    mine = bookjobs.new_job(email, "https://archive.org/details/mine")
    assert bookjobs.get_job(email, mine["job_id"]) is not None
    assert bookjobs.get_job("someone-else@example.com", mine["job_id"]) is None


def test_cancel_is_false_for_a_finished_job(email):
    record = bookjobs.new_job(email, "https://archive.org/details/x")
    bookjobs.update_job(record["job_id"], state=bookjobs.READY)
    assert bookjobs.request_cancel(email, record["job_id"]) is False


def test_cancel_is_false_for_an_unknown_job(email):
    assert bookjobs.request_cancel(email, "bj_doesnotexist") is False


def test_sweep_resolves_a_fetch_orphaned_by_a_restart(email):
    """A record still marked fetching on startup cannot be running — the
    process that owned it is gone."""
    record = bookjobs.new_job(email, "https://archive.org/details/x")
    bookjobs.update_job(record["job_id"], state=bookjobs.FETCHING)
    path = bookjobs._job_path(email, record["job_id"])
    # Drop the in-memory half, as a restart would.
    bookjobs.JOBS.pop(record["job_id"], None)

    assert bookjobs.sweep_stale(email) >= 1
    with open(path) as f:
        swept = json.load(f)
    assert swept["state"] == bookjobs.FAILED
    assert "restarted" in swept["error"]


def test_sweep_leaves_a_finished_job_alone(email):
    record = bookjobs.new_job(email, "https://archive.org/details/x")
    bookjobs.update_job(record["job_id"], state=bookjobs.READY, book_id="b_0123456789")
    bookjobs.JOBS.pop(record["job_id"], None)
    bookjobs.sweep_stale(email)
    with open(bookjobs._job_path(email, record["job_id"])) as f:
        assert json.load(f)["state"] == bookjobs.READY


def test_a_bad_url_fails_the_job_rather_than_raising(email):
    """The failure has to land on the record: the tool returned long ago and
    the only thing still watching is the chip in the reader."""
    record = bookjobs.new_job(email, "http://127.0.0.1/secret.pdf")   # not https
    asyncio.run(bookjobs._run_fetch(email, record["job_id"]))
    finished = bookjobs.get_job(email, record["job_id"])
    assert finished["state"] == bookjobs.FAILED
    assert finished["error"]


def test_failure_message_is_phrased_for_the_tutor(email):
    """open_book's error is what the tutor reads, and it has WebSearch — so a
    dead end that names the next step turns into a retry that converges."""
    record = bookjobs.new_job(email, "https://evil.example.com/book.pdf")
    asyncio.run(bookjobs._run_fetch(email, record["job_id"]))
    finished = bookjobs.get_job(email, record["job_id"])
    assert finished["state"] == bookjobs.FAILED
    assert "archive.org" in finished["error"]
