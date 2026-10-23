"""Background demo builds, tracked as jobs.

Why this exists: building a demo means the model writes 5-30KB of HTML, which
takes ~60s. While that happened inside the chat turn, the composer stayed
disabled for the whole minute — the learner could not ask anything else, could
not switch sessions, and had no signal that a demo was even being built.

So generation moves out of the chat turn entirely. The tutor calls
`request_demo`, which returns a job id immediately; a background task then runs
a *separate* agent (see agent.run_demo_builder) that forks the chat session and
produces the demo. The tutor's turn stays short and the chat never blocks.

State lives in two places on purpose:
  - JOBS, in memory, is authoritative while the process is alive.
  - one .json per job on disk, so a job survives a page refresh AND so a job
    orphaned by a server restart can be reported as failed rather than
    spinning forever in the UI (see sweep_stale).

TASKS holds a strong reference to every running task. asyncio only keeps a weak
one, so a bare create_task() can be garbage-collected mid-build.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from backend.demos import _ensure_dirs, _read_json, _write_atomic, demos_dir
from backend.memory import _normalize_email

logger = logging.getLogger(__name__)

# Terminal states are kept this long so a refresh can still explain what
# happened to a job the user watched start.
JOB_TTL_SECONDS = 30 * 60

# Each build is a full agent run with its own CLI subprocess and forked
# context. Two at once is plenty for one learner; more would mostly compete
# for the same API rate limit.
MAX_CONCURRENT_DEMO_JOBS = 2

# How many panels of ONE demo are written at the same time. Higher than
# MAX_CONCURRENT_DEMO_JOBS because a part-builder is much cheaper than a full
# build: it is not forked from the chat session, it holds one panel's contract
# rather than a whole conversation, and it makes exactly one tool call. This
# bounds the fan-out inside a single job — it deliberately does NOT go through
# active_count, which counts jobs and would serialise the panels two at a time
# and undo the point of building them in parallel.
MAX_CONCURRENT_PARTS = 4

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
    d = os.path.join(demos_dir(email), "jobs")
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
        # refresh. Not worth failing the build over.
        logger.warning(f"Could not persist demo job {record.get('job_id')}: {e}")


def new_job(
    email: str,
    chat_session_id: str | None,
    title: str,
    concept_id: str = "",
    request: str = "",
    base_demo_id: str | None = None,
    app: str = "pymentor",
) -> dict:
    """Create a job record in the queued state and persist it."""
    _ensure_dirs(email)
    # The directory is lowercased by _ensure_dirs but this field is compared
    # with == in get_job/list_jobs/active_count, so it has to be normalized
    # too — otherwise a casing difference hides the job from its own owner.
    email = _normalize_email(email)
    record = {
        "job_id": f"j_{uuid.uuid4().hex[:10]}",
        "email": email,
        "chat_session_id": chat_session_id,
        # Filled in once the builder's own session id is known; this is what
        # makes a demo refinable later.
        "builder_session_id": None,
        "title": (title or "Interactive demo").strip()[:120],
        "concept_id": (concept_id or "").strip(),
        "request": (request or "").strip()[:2000],
        "base_demo_id": base_demo_id,
        # Which tutor asked. Persisted rather than passed through, so a build
        # resumed after a restart still uses the right prompt and tools.
        "app": app,
        "state": QUEUED,
        "demo_id": base_demo_id,
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
    """One job, or None if it is missing or belongs to someone else.

    The ownership check is load-bearing. JOBS is keyed by job_id alone, so
    without it a job id from one learner read another learner's record —
    including the prompt text they typed — and request_cancel, which routes
    through here, would let them kill that build. The on-disk branch was
    already scoped by path; the in-memory one was not.
    """
    email = _normalize_email(email)
    record = JOBS.get(job_id)
    if record is not None:
        return record if _normalize_email(record.get("email") or "") == email else None
    return _read_json(_job_path(email, job_id))


def list_jobs(email: str) -> list[dict]:
    """All known jobs for a user, newest first.

    Reads from disk so a page refresh (or a restart) still sees them, then
    overlays the in-memory record, which is fresher for a running job.
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
        and r.get("state") in (QUEUED, BUILDING)
    )


def request_cancel(email: str, job_id: str) -> bool:
    """Ask a running build to stop. Explicit action only.

    Deliberately NOT wired to client disconnect: closing the tab must leave the
    build running, since the whole point is that the learner can walk away and
    find the demo waiting. (The code runner does the opposite — there,
    disconnect means the Stop button was pressed.)
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


def spawn(record: dict, coro_factory) -> dict:
    """Run a build in the background, keeping a strong reference to the task."""
    job_id = record["job_id"]

    async def runner():
        try:
            await coro_factory()
        except asyncio.CancelledError:
            update_job(job_id, state=CANCELLED, error="cancelled")
            raise
        except Exception as e:
            logger.error(f"Demo job {job_id} failed: {e}", exc_info=True)
            update_job(job_id, state=FAILED, error=str(e)[:500])

    task = asyncio.create_task(runner())
    TASKS[job_id] = task

    def _done(t: asyncio.Task):
        TASKS.pop(job_id, None)
        CANCELS.pop(job_id, None)
        # A task that died without reaching a terminal state would otherwise
        # leave the UI chip spinning forever.
        current = JOBS.get(job_id)
        if current and current.get("state") not in TERMINAL:
            reason = "build ended unexpectedly"
            if t.cancelled():
                reason = "cancelled"
            update_job(
                job_id,
                state=CANCELLED if t.cancelled() else FAILED,
                error=reason,
            )

    task.add_done_callback(_done)
    return record


def start_demo_job(
    email: str,
    chat_session_id: str | None,
    title: str,
    concept_id: str = "",
    request: str = "",
    base_demo_id: str | None = None,
    app: str = "pymentor",
) -> dict:
    """Create a job and start building in the background. Returns immediately.

    Called from the request_demo tool, so it must not await the build — the
    tutor's turn is waiting on this return value.
    """
    record = new_job(
        email=email,
        chat_session_id=chat_session_id,
        title=title,
        concept_id=concept_id,
        request=request,
        base_demo_id=base_demo_id,
        app=app,
    )

    if active_count(email) > MAX_CONCURRENT_DEMO_JOBS:
        # Left queued; the runner below waits for a slot rather than refusing.
        logger.info(
            f"Demo job {record['job_id']} queued behind "
            f"{active_count(email) - 1} active build(s)"
        )

    async def build():
        await _run_build(record)

    spawn(record, build)
    return record


async def _build_parts(record: dict, build_ctx: dict, declared: list) -> list | None:
    """Write every declared panel concurrently, then finalize the demo.

    Returns the names that failed (empty when all succeeded), or None when the
    job has already been marked FAILED and the caller should stop.

    Each panel gets its OWN build_ctx. They run interleaved on one event loop,
    so a shared dict would have them overwriting each other's demo_id — the
    same read-modify-write race that silently dropped writes elsewhere in this
    codebase. Nothing here is shared but the demo id, and each panel writes its
    own file, so there is nothing to lock.
    """
    from backend.agent import run_demo_part_builder
    from backend.demos import finalize_demo, list_demo_parts

    job_id = record["job_id"]
    email = record["email"]
    app = record.get("app", "pymentor")
    demo_id = build_ctx["demo_id"]
    # The names a panel must match, not the shell itself. Sending the whole
    # shell put the same 8-20KB into every one of N part-builder prompts, and
    # each prompt on its own already exceeded what the transport carries
    # reliably — which is how a ten-panel build loses its work.
    from backend.demos import shell_contract_digest

    shell = shell_contract_digest(build_ctx.get("shell_contract") or "")

    sem = asyncio.Semaphore(MAX_CONCURRENT_PARTS)
    failed: list[str] = []

    async def one(index: int, part: dict) -> None:
        name = part["name"]
        async with sem:
            if is_cancelled(job_id):
                return
            # Its own dict, per the note above.
            ctx = {
                "chat_session_id": build_ctx.get("chat_session_id", ""),
                "builder_session_id": "",
                "demo_id": demo_id,
            }
            prompt = (
                f"demo_id: {demo_id}\n"
                f"index: {index}\n"
                f"name: {name}\n\n"
                f"What this panel must show:\n{part['spec']}\n\n"
                f"The shell every panel shares already defines these — use "
                f"them, do not restate them, and prefix anything of your own "
                f"with '{name}_' so it cannot collide with another "
                f"panel:\n{shell}\n\n"
                f"Write this one panel now with demo_write_part."
            )
            try:
                await run_demo_part_builder(
                    email=email, request=prompt, build_ctx=ctx, app=app
                )
            except Exception as e:
                # One panel failing must not take the others down: nine panels
                # that render beat ten that do not.
                logger.error(f"Demo part {name!r} failed: {e}", exc_info=True)
                failed.append(name)

    await asyncio.gather(
        *[one(i, p) for i, p in enumerate(declared, start=1)],
        return_exceptions=True,
    )

    if is_cancelled(job_id):
        return []

    # What actually landed on disk, which is the only thing that decides
    # whether this build produced anything.
    written = list_demo_parts(email, demo_id)
    if not written:
        update_job(
            job_id,
            state=FAILED,
            error=(
                f"none of the {len(declared)} panels could be written "
                f"({', '.join(failed) or 'no panel reported a reason'})"
            ),
        )
        return None

    try:
        finalize_demo(email, demo_id, build_ctx.get("builder_session_id", ""))
    except ValueError as e:
        update_job(job_id, state=FAILED, demo_id=demo_id, error=str(e)[:500])
        return None

    if failed:
        logger.warning(
            f"Demo {demo_id} finalized with {len(written)}/{len(declared)} "
            f"panels; failed: {failed}"
        )
    return failed


def _rescue_draft(
    record: dict,
    build_ctx: dict,
    demo_id: str | None,
    builder_session_id: str,
) -> bool:
    """Publish the draft a failed build left behind. True if it was rescued.

    "Stream closed" means the call never reached this process, so the builder
    finished its work and only the saving failed — the finished document is
    sitting in its scratch directory. Every one of the four builds rescued by
    hand this week was exactly that, and of the drafts on disk at the time,
    13 of 13 passed validation. Doing it by hand was pure mechanism.

    The validation gate is what makes this safe: publish only what save_demo
    would have accepted anyway. A half-written or broken draft falls through to
    the normal failure path, so this can turn a failure into a success but
    never into a bad demo.
    """
    if not demo_id:
        return False

    scratch = build_ctx.get("scratch_dir")
    if not scratch or not os.path.isdir(scratch):
        return False

    try:
        drafts = [
            os.path.join(scratch, name)
            for name in os.listdir(scratch)
            if name.endswith(".html")
        ]
    except OSError:
        return False
    if not drafts:
        return False

    newest = max(drafts, key=os.path.getmtime)

    from backend.demos import update_demo, validate_demo_html

    try:
        # Read and validate before touching the store, so a broken draft is
        # never published over a working demo.
        with open(newest, encoding="utf-8") as f:
            validate_demo_html(f.read())
        meta = update_demo(
            email=record["email"],
            demo_id=demo_id,
            html_path=newest,
            builder_session_id=builder_session_id,
        )
    except (ValueError, OSError) as e:
        logger.warning(f"Draft at {newest} is not publishable: {e}")
        return False

    if meta is None:
        return False

    logger.info(
        f"Demo job {record['job_id']}: recovered v{meta['version']} from "
        f"{newest} after the tool connection dropped"
    )
    update_job(
        record["job_id"],
        state=READY,
        demo_id=demo_id,
        builder_session_id=builder_session_id,
        error=None,
        note="recovered after the tool connection dropped",
    )
    return True


async def _run_build(record: dict) -> None:
    """Run one demo build to completion, recording what happened."""
    from backend.agent import run_demo_builder
    from backend.demos import (
        find_demo_by_job,
        get_demo,
        get_demo_meta,
        read_demo_shell,
    )

    job_id = record["job_id"]
    email = record["email"]

    # Wait for a slot. Each build is a full agent run with its own subprocess,
    # so letting an unbounded number start would mostly buy rate-limit errors.
    while active_count(email) > MAX_CONCURRENT_DEMO_JOBS:
        if is_cancelled(job_id):
            update_job(job_id, state=CANCELLED, error="cancelled")
            return
        await asyncio.sleep(0.5)

    update_job(job_id, state=BUILDING)

    base_demo_id = record.get("base_demo_id")
    resume_builder = None
    if base_demo_id:
        existing = get_demo(email, base_demo_id)
        if existing:
            # Resume the session that wrote this demo: it still has both the
            # conversation and the HTML, so the edit is incremental.
            resume_builder = existing.get("builder_session_id") or None
        else:
            logger.warning(
                f"Demo job {job_id}: base demo {base_demo_id} is gone; building fresh"
            )
            base_demo_id = None

    prompt = _build_prompt(record, base_demo_id)

    # Inbound only: what the tools need to know. Nothing comes back through
    # it from a tool — over the HTTP MCP transport each request rebuilds it
    # from a header, so a tool's write lands on a copy that is then discarded.
    # Tools stamp job_id onto the demo's meta instead, and the result is read
    # from disk below. agent.py's own flags (scratch_dir, builder_session_id,
    # transport_failed, ran_tools, output_overflow) are written in-process to
    # this live dict and are safe to read.
    build_ctx: dict = {
        "chat_session_id": record.get("chat_session_id") or "",
        "builder_session_id": "",
        "job_id": job_id,
    }

    builder_session_id = await run_demo_builder(
        email=email,
        request=prompt,
        chat_session_id=record.get("chat_session_id"),
        resume_builder_session=resume_builder,
        build_ctx=build_ctx,
        # Persisted on the record, so a build resumed after a restart still
        # builds with the right tutor's prompt and tools.
        app=record.get("app", "pymentor"),
    )

    # Mirror the builder's transcript before any early return below. Refining a
    # demo resumes this same session, so without the copy a migrated data/
    # directory would rebuild from scratch instead of editing in place.
    if builder_session_id:
        from backend.transcripts import archive_builder_session

        archive_builder_session(email, builder_session_id)

    if is_cancelled(job_id):
        update_job(job_id, state=CANCELLED, error="cancelled")
        return

    # What this build wrote, read from the stamp its tools left on disk. On an
    # edit the builder may legitimately decline — "hi" is not a change request
    # — so the base demo counts as written only if THIS job stamped it.
    # Falling back to base_demo_id would report success for a demo nobody
    # touched.
    if base_demo_id:
        base_meta = get_demo_meta(email, base_demo_id) or {}
        wrote = base_meta.get("job_id") == job_id
        written_meta = base_meta if wrote else None
    else:
        written_meta = find_demo_by_job(email, job_id)
        wrote = written_meta is not None
    if wrote:
        build_ctx["demo_id"] = written_meta["id"]
    demo_id = written_meta["id"] if wrote else base_demo_id

    # The orchestrator wrote a shell and declared its panels. Build them here,
    # several at a time, then finalize — the whole reason the split path exists
    # is that these cannot all fit in the orchestrator's own reply.
    declared = (written_meta or {}).get("declared_parts") or []
    if declared and not (written_meta or {}).get("part_count"):
        # The panels' authors never see this conversation, so the shell on
        # disk is their only source for the class names and helper functions
        # they must match.
        build_ctx["shell_contract"] = read_demo_shell(email, demo_id)
        failed = await _build_parts(record, build_ctx, declared)
        if is_cancelled(job_id):
            update_job(job_id, state=CANCELLED, error="cancelled")
            return
        if failed is None:
            return  # already marked FAILED with a reason

    if base_demo_id and not wrote:
        # Three different things end up here, and they used to share one
        # message — "no change was made — describe what to change about the
        # demo" — which blamed the learner for all of them.
        if _rescue_draft(record, build_ctx, base_demo_id, builder_session_id):
            return

        if build_ctx.get("transport_failed"):
            # The builder did the work; the call carrying it never arrived.
            update_job(
                job_id,
                state=FAILED,
                demo_id=base_demo_id,
                builder_session_id=builder_session_id,
                error=(
                    "the tool connection dropped before the change could be "
                    "saved — ask again and it will retry"
                ),
            )
        elif build_ctx.get("ran_tools"):
            # It looked, and decided there was nothing to do — most often
            # because the demo already does what was asked. Not a failure, and
            # painting it red taught the learner to distrust a correct answer.
            update_job(
                job_id,
                state=READY,
                demo_id=base_demo_id,
                builder_session_id=builder_session_id,
                error=None,
                note="no change needed — the demo already does this",
            )
        else:
            # It never reached for a tool at all, which is what a question
            # rather than an instruction produces.
            update_job(
                job_id,
                state=FAILED,
                demo_id=base_demo_id,
                builder_session_id=builder_session_id,
                error=(
                    "that box takes an instruction, not a question — say what "
                    "to change, e.g. 'add a reset button'. To ask about the "
                    "demo, use Ask tutor or the main chat."
                ),
            )
        return

    if not demo_id:
        # The builder finished without writing anything. Treat as a failure:
        # otherwise the chip would sit at "ready" pointing at no demo, which is
        # exactly the state that produced 404s on Open.
        #
        # Say WHY when the reason is known. An over-long reply is discarded by
        # the API, so the build looks like it simply stopped — reporting that
        # as "finished without saving a demo" hid the real cause and left the
        # tutor with nothing useful to tell the learner.
        overflow = build_ctx.get("output_overflow")
        update_job(
            job_id,
            state=FAILED,
            error=(
                "the demo was too large to write in one reply — it needs to be "
                "split into a shell plus one part per panel"
                if overflow
                else "the builder finished without saving a demo"
            ),
            builder_session_id=builder_session_id,
        )
        return

    # Backfill the builder session onto the demo. save_demo runs before the
    # session id is known on a first build, so it is stamped here instead.
    if builder_session_id:
        try:
            from backend.demos import _meta_path, _read_json, _write_atomic

            meta = _read_json(_meta_path(email, demo_id))
            if meta is not None and not meta.get("builder_session_id"):
                meta["builder_session_id"] = builder_session_id
                _write_atomic(_meta_path(email, demo_id), json.dumps(meta, indent=2))
        except OSError as e:
            logger.warning(f"Could not stamp builder session on {demo_id}: {e}")

    # A demo still ships when the builder skipped its own check or left a fault
    # in — the static checks cannot see a runtime throw either way, so refusing
    # to publish would trade a possibly-broken demo for certainly none. But the
    # state is recorded rather than assumed, so an unchecked build is visible
    # here instead of looking identical to a verified one.
    verified = (get_demo_meta(email, demo_id) or {}).get("verified")
    if verified is None:
        logger.warning(f"Demo job {job_id}: builder never ran demo_verify")
    elif verified is False:
        logger.warning(
            f"Demo job {job_id}: published with demo_verify faults outstanding"
        )

    update_job(
        job_id,
        state=READY,
        demo_id=demo_id,
        builder_session_id=builder_session_id,
        verified=verified,
        error=None,
    )
    logger.info(f"Demo job {job_id} ready: {demo_id} (verified={verified})")


def _build_prompt(record: dict, base_demo_id: str | None) -> str:
    """The goal handed to the builder agent.

    States WHAT to produce, not which tool to call. Choosing between writing a
    demo whole and splitting it into a shell plus parts depends on how big it
    turns out to be, which only the builder knows — and the system prompt is
    where that decision is taught.

    It used to end with "Call save_demo once with the complete HTML". That
    contradicted the system prompt's split path, and the builder split the
    difference: it wrote a shell and then declared no panels, so a ten-panel
    request produced nothing at all.
    """
    title = record.get("title") or "Interactive demo"
    request = record.get("request") or ""

    if base_demo_id:
        return (
            f"Update the existing demo (demo_id: {base_demo_id}).\n\n"
            f"Requested change: {request}\n\n"
            "Read the current version first, and keep everything that already "
            "worked — this is an edit, not a rebuild."
        )

    return (
        f"Build a new interactive demo titled {title!r}.\n\n"
        f"What it needs to show: {request}\n\n"
        f"concept_id: {record.get('concept_id') or 'none'}\n\n"
        "Work however the demo needs — draft, check what you wrote, fix it, "
        "and publish only once it holds together."
    )


def sweep_stale(email: str | None = None) -> int:
    """Resolve jobs orphaned by a restart, and drop expired ones.

    A record still marked building on startup cannot be running — the process
    that owned it is gone. Without this the UI would poll a job that will never
    finish. Called from the app's lifespan hook.
    """
    from backend.demos import USERS_DIR

    # Job directories, resolved once. When no email is given, walk every user
    # that has one — the startup sweep does not know who was mid-build.
    job_dirs: list[str] = []
    if email:
        job_dirs.append(_jobs_dir(email))
    else:
        try:
            for name in os.listdir(USERS_DIR):
                candidate = os.path.join(USERS_DIR, name, "demos", "jobs")
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

            # Expire old terminal records.
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

            # Anything still in flight was orphaned by a restart.
            #
            # Safe only because this runs at startup and nowhere else (see
            # main.py's lifespan hook) — at that moment nothing can be building,
            # so no age threshold is needed. A builder now works for minutes
            # rather than one turn, so calling this from a request handler
            # would start failing live builds. If it ever needs to run outside
            # startup, gate this branch on age and heartbeat updated_at from
            # _run_build.
            if record.get("state") in (QUEUED, BUILDING):
                record["state"] = FAILED
                record["error"] = "server restarted while building"
                record["updated_at"] = _now()
                try:
                    _write_atomic(path, json.dumps(record, indent=2))
                    swept += 1
                except OSError:
                    pass

    if swept:
        logger.info(f"Swept {swept} stale demo job(s)")
    return swept
