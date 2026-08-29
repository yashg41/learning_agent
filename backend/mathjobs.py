"""Background derivation builds, tracked as jobs.

Deliberately a COPY of demojobs.py rather than a shared abstraction. The two
job systems look alike today, but a demo produces one opaque HTML document while
a derivation produces an ordered list of typed blocks that arrive in waves and
get verified individually. Merging them would couple two things that are about
to diverge, and the demo path is working — so this file duplicates ~80 lines of
bookkeeping on purpose and leaves demos untouched.

Why background at all: a derivation with per-step sympy verification and a plot
takes long enough that building it inside the chat turn would freeze the
composer, which is the exact problem that moved demo builds out of the turn.
The tutor calls `request_derivation`, gets a job id back immediately, and keeps
teaching while the pane fills in.

State lives in two places, as in demojobs:
  - JOBS, in memory, authoritative while the process is alive.
  - one .json per job on disk, so a job survives a refresh and an orphan left by
    a restart can be reported failed rather than spinning forever.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from backend.derivations import _ensure_dirs, _read_json, _write_json, derivations_dir
from backend.memory import _normalize_email

logger = logging.getLogger(__name__)

JOB_TTL_SECONDS = 30 * 60

# Each build is a full agent run with its own subprocess and forked context.
MAX_CONCURRENT_MATH_JOBS = 2

QUEUED = "queued"
BUILDING = "building"
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
    d = os.path.join(derivations_dir(email), "jobs")
    os.makedirs(d, exist_ok=True)
    return d


def _job_path(email: str, job_id: str) -> str:
    return os.path.join(_jobs_dir(email), f"{job_id}.json")


def _persist(record: dict) -> None:
    try:
        _write_json(_job_path(record["email"], record["job_id"]), record)
    except OSError as e:
        # A job that cannot be persisted still runs; it just will not survive a
        # refresh. Not worth failing the build over.
        logger.warning(f"Could not persist derivation job {record.get('job_id')}: {e}")


def new_job(
    email: str,
    chat_session_id: str | None,
    title: str,
    concept_id: str = "",
    request: str = "",
    base_doc_id: str | None = None,
) -> dict:
    _ensure_dirs(email)
    # Normalized because this field is compared with == on the read side; see
    # the note in demojobs.new_job.
    email = _normalize_email(email)
    record = {
        "job_id": f"j_{uuid.uuid4().hex[:10]}",
        "email": email,
        "chat_session_id": chat_session_id,
        "builder_session_id": None,
        "title": (title or "Derivation").strip()[:120],
        "concept_id": (concept_id or "").strip(),
        "request": (request or "").strip()[:2000],
        "base_doc_id": base_doc_id,
        "state": QUEUED,
        "doc_id": base_doc_id,
        "error": None,
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
    """One job, or None if missing or owned by someone else.

    The ownership check matters: JOBS is keyed by job_id alone, so without it a
    job id from one learner would read another learner's record — including the
    request text they typed.
    """
    email = _normalize_email(email)
    record = JOBS.get(job_id)
    if record is not None:
        return record if _normalize_email(record.get("email") or "") == email else None
    return _read_json(_job_path(email, job_id))


def list_jobs(email: str) -> list[dict]:
    """All known jobs for a user, newest first — disk, overlaid with memory."""
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
        and r.get("state") in (QUEUED, BUILDING)
    )


def request_cancel(email: str, job_id: str) -> bool:
    """Explicit stop only — never wired to client disconnect.

    Closing the tab must leave the build running: the whole point is that the
    learner can walk away and find the derivation waiting.
    """
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


def sweep_stale(email: str) -> None:
    """Mark jobs orphaned by a restart as failed.

    A record on disk in a running state with nothing in JOBS means the process
    that owned it died. Without this the UI chip spins forever.
    """
    for record in list_jobs(email):
        job_id = record.get("job_id")
        if not job_id or job_id in JOBS:
            continue
        if record.get("state") in (QUEUED, BUILDING):
            record["state"] = FAILED
            record["error"] = "server restarted during the build"
            record["updated_at"] = _now()
            _persist(record)


def spawn(record: dict, coro_factory) -> dict:
    """Run a build in the background, keeping a strong reference to the task.

    asyncio holds only a weak reference, so a bare create_task can be collected
    mid-build.
    """
    job_id = record["job_id"]

    async def runner():
        try:
            await coro_factory()
        except asyncio.CancelledError:
            update_job(job_id, state=CANCELLED, error="cancelled")
            raise
        except Exception as e:
            logger.error(f"Derivation job {job_id} failed: {e}", exc_info=True)
            update_job(job_id, state=FAILED, error=str(e)[:500])

    task = asyncio.create_task(runner())
    TASKS[job_id] = task

    def _done(t: asyncio.Task):
        TASKS.pop(job_id, None)
        CANCELS.pop(job_id, None)
        current = JOBS.get(job_id)
        if current and current.get("state") not in TERMINAL:
            update_job(
                job_id,
                state=CANCELLED if t.cancelled() else FAILED,
                error="cancelled" if t.cancelled() else "build ended unexpectedly",
            )

    task.add_done_callback(_done)
    return record


def start_derivation_job(
    email: str,
    chat_session_id: str | None,
    title: str,
    concept_id: str = "",
    request: str = "",
    base_doc_id: str | None = None,
) -> dict:
    """Create a job and start building. Returns immediately.

    Called from the request_derivation tool, so it must not await the build —
    the tutor's turn is waiting on this return value.
    """
    record = new_job(
        email=email,
        chat_session_id=chat_session_id,
        title=title,
        concept_id=concept_id,
        request=request,
        base_doc_id=base_doc_id,
    )

    async def build():
        await _run_build(record)

    spawn(record, build)
    return record


async def _run_build(record: dict) -> None:
    """Run one derivation build to completion, recording what happened."""
    from backend.agent import run_derivation_builder
    from backend.derivations import get_doc, save_meta

    job_id = record["job_id"]
    email = record["email"]

    while active_count(email) > MAX_CONCURRENT_MATH_JOBS:
        if is_cancelled(job_id):
            update_job(job_id, state=CANCELLED, error="cancelled")
            return
        await asyncio.sleep(0.5)

    update_job(job_id, state=BUILDING)

    base_doc_id = record.get("base_doc_id")
    resume_builder = None
    if base_doc_id:
        existing = get_doc(email, base_doc_id)
        if existing:
            # Resume the session that wrote this derivation: it still holds the
            # conversation and the blocks, so a revision is incremental.
            resume_builder = existing.get("builder_session_id") or None
        else:
            logger.warning(
                f"Derivation job {job_id}: base doc {base_doc_id} is gone; building fresh"
            )
            base_doc_id = None

    prompt = _build_prompt(record, base_doc_id)

    build_ctx: dict = {
        "chat_session_id": record.get("chat_session_id") or "",
        "builder_session_id": "",
        "doc_id": base_doc_id,
        "title": record.get("title") or "",
        "concept_id": record.get("concept_id") or "",
    }

    builder_session_id = await run_derivation_builder(
        email=email,
        request=prompt,
        chat_session_id=record.get("chat_session_id"),
        resume_builder_session=resume_builder,
        build_ctx=build_ctx,
    )

    # Mirror the builder's transcript before any early return: a revision
    # resumes this same session, so without the copy a migrated data/ directory
    # would rebuild from scratch instead of editing in place.
    if builder_session_id:
        try:
            from backend.transcripts import archive_builder_session

            archive_builder_session(email, builder_session_id)
        except Exception as e:
            logger.warning(f"Could not archive builder session: {e}")

    if is_cancelled(job_id):
        update_job(job_id, state=CANCELLED, error="cancelled")
        return

    doc_id = build_ctx.get("doc_id")
    wrote = bool(build_ctx.get("wrote_blocks"))

    if base_doc_id and not wrote:
        # On a revision the builder may legitimately decline — "hi" is not a
        # change request. Reporting ready would claim an edit nobody made.
        update_job(
            job_id,
            state=FAILED,
            doc_id=base_doc_id,
            builder_session_id=builder_session_id,
            error="no change was made — say what to change about the derivation",
        )
        return

    if not doc_id or not wrote:
        update_job(
            job_id,
            state=FAILED,
            error="the builder finished without writing a derivation",
            builder_session_id=builder_session_id,
        )
        return

    if builder_session_id:
        try:
            save_meta(email, doc_id, builder_session_id=builder_session_id)
        except Exception as e:
            logger.warning(f"Could not stamp builder session on {doc_id}: {e}")

    update_job(
        job_id,
        state=READY,
        doc_id=doc_id,
        builder_session_id=builder_session_id,
        error=None,
    )
    logger.info(f"Derivation job {job_id} ready: {doc_id}")


def _build_prompt(record: dict, base_doc_id: str | None) -> str:
    """The instruction handed to the builder agent."""
    title = record.get("title") or "Derivation"
    request = record.get("request") or ""
    concept_id = record.get("concept_id") or ""

    if base_doc_id:
        return (
            f"Revise the existing derivation {base_doc_id}.\n\n"
            f"Requested change: {request}\n\n"
            "Read it with derivation_read first, then append or correct only "
            "what the change calls for. Keep everything that was already right."
        )

    lines = [f"Build a derivation titled: {title}", ""]
    if request:
        lines.append(f"What to derive: {request}")
    if concept_id:
        lines.append(f"Concept id: {concept_id}")
    lines += [
        "",
        "Call derivation_open once, then derivation_write in waves so the "
        "learner sees it fill in: the intuition first, then the steps, then "
        "any table or matrix. Verify every step with derivation_verify.",
    ]
    return "\n".join(lines).strip()
