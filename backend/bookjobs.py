"""Background book fetches, tracked as jobs.

Downloading a book is slow and unpredictable — a 40MB scan over a bad link is
minutes — so it cannot happen inside a chat turn. `open_book` returns a job id
immediately and the tutor carries on teaching while the fetch runs.

Modelled closely on demojobs.py, which has already absorbed the lessons that
matter: JOBS in memory is authoritative while the process lives, one .json per
job on disk survives a refresh, TASKS holds strong references (asyncio keeps
only weak ones, so a bare create_task can be collected mid-run), and
sweep_stale() resolves anything a restart orphaned so a chip cannot spin
forever.

Two deliberate differences from demo builds:

  - TWO working states, FETCHING and RENDERING, not one. The failure modes read
    completely differently to the learner — "the download died" versus "the PDF
    is corrupt" — and a single "building" cannot say which.
  - A `progress` field. Unlike a demo build, a download has a real numerator,
    and a silent four-minute chip is what makes people reload the page.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from backend.books import _read_json, _write_atomic, books_dir
from backend.memory import _normalize_email

logger = logging.getLogger(__name__)

JOB_TTL_SECONDS = 30 * 60

# One at a time. Books are large downloads and the bottleneck is bandwidth, not
# CPU; running several only makes each slower and the progress harder to read.
MAX_CONCURRENT_BOOK_JOBS = 1

QUEUED = "queued"
FETCHING = "fetching"
RENDERING = "rendering"
READY = "ready"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = (READY, FAILED, CANCELLED)

JOBS: dict[str, dict] = {}
TASKS: dict[str, asyncio.Task] = {}
CANCELS: dict[str, asyncio.Event] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jobs_dir(email: str) -> str:
    d = os.path.join(books_dir(email), "jobs")
    os.makedirs(d, exist_ok=True)
    return d


def _job_path(email: str, job_id: str) -> str:
    return os.path.join(_jobs_dir(email), f"{job_id}.json")


def _persist(record: dict) -> None:
    try:
        _write_atomic(
            _job_path(record["email"], record["job_id"]),
            json.dumps(record, indent=2),
        )
    except OSError as e:
        # A job that cannot be persisted still runs; it just won't survive a
        # refresh. Not worth failing the fetch over.
        logger.warning(f"Could not persist book job {record.get('job_id')}: {e}")


def new_job(email: str, url: str, title: str = "") -> dict:
    email = _normalize_email(email)
    _jobs_dir(email)
    record = {
        "job_id": f"bj_{uuid.uuid4().hex[:10]}",
        "email": email,
        "url": (url or "").strip()[:2000],
        "title": (title or "").strip()[:200],
        "state": QUEUED,
        "book_id": None,
        "error": None,
        "progress": {"received": 0, "total": None},
        "created_at": _now(),
        "updated_at": _now(),
    }
    JOBS[record["job_id"]] = record
    CANCELS[record["job_id"]] = asyncio.Event()
    _persist(record)
    return record


def update_job(job_id: str, **fields) -> dict | None:
    record = JOBS.get(job_id)
    if record is None:
        return None
    record.update(fields)
    record["updated_at"] = _now()
    _persist(record)
    return record


def get_job(email: str, job_id: str) -> dict | None:
    """One job, or None if it is missing or belongs to someone else.

    The ownership check is load-bearing, exactly as in demojobs: JOBS is keyed
    by job_id alone, so without it one learner's id reads another's record —
    including the URL they asked for — and request_cancel routes through here.
    """
    email = _normalize_email(email)
    record = JOBS.get(job_id)
    if record is not None:
        return record if _normalize_email(record.get("email") or "") == email else None
    return _read_json(_job_path(email, job_id))


def list_jobs(email: str) -> list[dict]:
    """All known jobs for a user, newest first.

    Disk first so a refresh still sees them, then the in-memory record on top,
    which is fresher for a running fetch.
    """
    email = _normalize_email(email)
    out: dict[str, dict] = {}
    try:
        for name in os.listdir(_jobs_dir(email)):
            if not name.endswith(".json"):
                continue
            record = _read_json(os.path.join(_jobs_dir(email), name))
            if record and record.get("job_id"):
                out[record["job_id"]] = record
    except OSError:
        pass

    for job_id, record in JOBS.items():
        if _normalize_email(record.get("email") or "") == email:
            out[job_id] = record

    jobs = list(out.values())
    jobs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return jobs


def active_count(email: str) -> int:
    email = _normalize_email(email)
    return sum(
        1
        for r in JOBS.values()
        if _normalize_email(r.get("email") or "") == email
        and r.get("state") in (QUEUED, FETCHING, RENDERING)
    )


def request_cancel(email: str, job_id: str) -> bool:
    """Stop a running fetch. Explicit action only — closing the tab must not."""
    record = get_job(email, job_id)
    if record is None or record.get("state") in TERMINAL:
        return False
    event = CANCELS.get(job_id)
    if event is not None:
        event.set()
    task = TASKS.get(job_id)
    if task is not None:
        task.cancel()
    update_job(job_id, state=CANCELLED, error="cancelled")
    return True


def is_cancelled(job_id: str) -> bool:
    event = CANCELS.get(job_id)
    return bool(event and event.is_set())


def spawn(record: dict, coro_factory) -> dict:
    """Run a fetch in the background, keeping a strong reference to the task."""
    job_id = record["job_id"]

    async def runner():
        try:
            await coro_factory()
        except asyncio.CancelledError:
            update_job(job_id, state=CANCELLED, error="cancelled")
            raise
        except Exception as e:
            logger.error(f"Book job {job_id} failed: {e}", exc_info=True)
            update_job(job_id, state=FAILED, error=str(e)[:500])

    task = asyncio.create_task(runner())
    TASKS[job_id] = task

    def _done(t: asyncio.Task):
        TASKS.pop(job_id, None)
        CANCELS.pop(job_id, None)
        # A task that died without a terminal state leaves the chip spinning.
        current = JOBS.get(job_id)
        if current and current.get("state") not in TERMINAL:
            update_job(
                job_id,
                state=CANCELLED if t.cancelled() else FAILED,
                error="cancelled" if t.cancelled() else "the fetch ended unexpectedly",
            )

    task.add_done_callback(_done)
    return record


def start_book_job(email: str, url: str, title: str = "") -> dict:
    """Create a job and fetch in the background. Returns immediately.

    Called from the open_book tool, so it must not await the download — the
    whole point is that the tutor's turn stays short.
    """
    record = new_job(email, url, title)
    if active_count(email) > MAX_CONCURRENT_BOOK_JOBS:
        logger.info(f"Book job {record['job_id']} queued behind an active fetch")
    return spawn(record, lambda: _run_fetch(email, record["job_id"]))


async def _run_fetch(email: str, job_id: str) -> None:
    from backend.books import create_book
    from backend.bookresolve import ResolveError, fetch_pdf, resolve_to_pdf
    from backend.pdfimport import MAX_PDF_BYTES, PdfImportUnavailable

    record = JOBS.get(job_id)
    if record is None:
        return

    # Wait for a slot rather than refusing: a queued job is a chip that will
    # start, which is a better answer than "try again later".
    while active_count(email) > MAX_CONCURRENT_BOOK_JOBS:
        if is_cancelled(job_id):
            update_job(job_id, state=CANCELLED, error="cancelled")
            return
        await asyncio.sleep(0.5)

    url = record["url"]
    try:
        update_job(job_id, state=FETCHING)
        source = await resolve_to_pdf(url)

        def on_progress(received, total):
            # Best-effort: a progress write must never interrupt the download.
            update_job(job_id, progress={"received": received, "total": total})

        data = await asyncio.wait_for(
            fetch_pdf(source.pdf_url, MAX_PDF_BYTES, on_progress=on_progress),
            timeout=300,
        )
        if is_cancelled(job_id):
            update_job(job_id, state=CANCELLED, error="cancelled")
            return

        update_job(job_id, state=RENDERING)
        # create_book verifies the bytes really are a PDF and renders the
        # opening pages. On a worker thread: this process streams SSE.
        book = await asyncio.to_thread(
            create_book,
            email,
            pdf_bytes=data,
            title=record["title"] or source.title,
            source={
                "kind": source.kind,
                "url": source.pdf_url,
                "filename": source.filename,
                "identifier": source.identifier,
            },
        )
        update_job(job_id, state=READY, book_id=book["book_id"], error=None)
        logger.info(f"Book job {job_id} ready: {book['book_id']}")

    except asyncio.TimeoutError:
        update_job(job_id, state=FAILED, error="the download took too long and was stopped")
    except ResolveError as e:
        # Already phrased for the learner and for the tutor to act on.
        update_job(job_id, state=FAILED, error=str(e)[:500])
    except PdfImportUnavailable:
        update_job(job_id, state=FAILED, error="PyMuPDF is not installed on the server")
    except ValueError as e:
        update_job(job_id, state=FAILED, error=f"That file is not a readable PDF: {e}"[:500])


def sweep_stale(email: str | None = None) -> int:
    """Resolve jobs orphaned by a restart, and drop expired ones.

    A record still marked fetching on startup cannot be running — the process
    that owned it is gone. Without this the UI polls a job that will never
    finish. Called from the app's lifespan hook.
    """
    from backend.books import USERS_DIR

    job_dirs: list[str] = []
    if email:
        job_dirs.append(_jobs_dir(email))
    else:
        try:
            for name in os.listdir(USERS_DIR):
                candidate = os.path.join(USERS_DIR, name, "books", "jobs")
                if os.path.isdir(candidate):
                    job_dirs.append(candidate)
        except OSError:
            return 0

    swept = 0
    now = datetime.now(timezone.utc)
    for job_dir in job_dirs:
        try:
            names = os.listdir(job_dir)
        except OSError:
            continue

        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(job_dir, name)
            record = _read_json(path)
            if not record:
                continue

            try:
                age = (now - datetime.fromisoformat(record["updated_at"])).total_seconds()
            except (KeyError, ValueError, TypeError):
                age = 0
            if record.get("state") in TERMINAL and age > JOB_TTL_SECONDS:
                try:
                    os.remove(path)
                    swept += 1
                except OSError:
                    pass
                continue

            if record.get("state") in (QUEUED, FETCHING, RENDERING):
                record["state"] = FAILED
                record["error"] = "the server restarted while fetching"
                record["updated_at"] = _now()
                try:
                    _write_atomic(path, json.dumps(record, indent=2))
                    swept += 1
                except OSError:
                    pass

    if swept:
        logger.info(f"Swept {swept} stale book job(s)")
    return swept
