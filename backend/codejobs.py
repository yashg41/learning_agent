"""Background code runs, tracked as jobs.

Why this exists: the tutor verifies a snippet before showing it to the learner,
and that verification happens inside the chat turn. A fast snippet is fine —
run_code's inline path caps at RUN_CODE_TIMEOUT_SEC and returns in
milliseconds for ordinary code. But a cell that installs a package pays pip's
budget (up to 600s — see the timeout default on sandbox.install_packages), and
holding the composer that long is exactly the failure demo builds already
taught us not to repeat (see demojobs.py). That install budget is NOT bounded
by CODE_JOB_TIMEOUT_SEC, which covers the cell body only; see the note there.

So a slow verification moves out of the chat turn: the tool returns a job id
immediately, the run continues in the background, and the tutor either keeps
teaching or picks the result up on a later turn.

Deliberately a separate module from demojobs rather than a generalisation of
it. The two share their *lifecycle* — queued/running/terminal, cancellation,
disk persistence, a strong task reference — and that shared part is imported
from demojobs.spawn rather than copied. What they do not share is the record
shape (a demo job carries titles and concept ids; a code job carries code and
output) or the storage root (demos_dir vs. the user dir), and forcing one
record to cover both would make every field optional and the meaning unclear.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from backend.demojobs import (
    BUILDING,
    FAILED,
    QUEUED,
    READY,
    TASKS,
    TERMINAL,
    spawn,
)
from backend.demos import _write_atomic
from backend.memory import _safe_email, USERS_DIR

logger = logging.getLogger(__name__)

# A code job is a subprocess, not a forked agent, so it is far cheaper than a
# demo build (MAX_CONCURRENT_DEMO_JOBS = 2). The ceiling that matters here is
# pip and the venv lock, not the API rate limit.
MAX_CONCURRENT_CODE_JOBS = 4

# Generous next to the inline path's 10s: this exists precisely for the runs
# that cannot finish quickly.
#
# Bounds the CELL BODY only. A `%pip install` line runs before the body, via
# sandbox.install_packages, under pip's own 600s budget which run_code does not
# thread this value into — so a job that installs can legitimately take longer
# than this number suggests. Left that way deliberately: the install timeout is
# shared with the learner's notebook, and a package that needs eight minutes to
# build should not be killed differently depending on who asked for it.
CODE_JOB_TIMEOUT_SEC = 300

# Matches demojobs.JOB_TTL_SECONDS: long enough that a learner who wandered off
# can still come back and see what happened.
JOB_TTL_SECONDS = 30 * 60

# Same reasoning as tools.MAX_RUN_OUTPUT_CHARS — whatever lands here is read
# back into the transcript when the tutor collects the result.
MAX_JOB_OUTPUT_CHARS = 3000

# In-memory registry, authoritative while the process is alive. Mirrors the
# demojobs pattern, including the on-disk copy so a refresh still sees a job.
JOBS: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Verified source ---------------------------------------------------
#
# The tutor cites a run in chat as ```verified:<run_id> and the frontend
# fetches the real source rather than trusting the model to retype it. That
# means the source has to outlive the run.
#
# It cannot live in the run directory: scratch is under /tmp, swept hourly by
# _sweep_run_dirs, and gone on reboot. Correct for plots and cell.py, wrong
# for something a saved conversation still points at next week. So a clean run
# gets a copy here, under the user's data dir, keyed by run_id.


def _verified_dir(email: str) -> str:
    d = os.path.join(USERS_DIR, _safe_email(email), "verified")
    os.makedirs(d, exist_ok=True)
    return d


def verified_path(email: str, run_id: str) -> str:
    return os.path.join(_verified_dir(email), f"{run_id}.py")


def save_verified(email: str, run_id: str, code: str) -> None:
    """Keep the exact source of a clean run, for the chat to cite later.

    Best-effort: failing to archive must not fail the run the learner is
    waiting on. The frontend degrades to inert placeholder text on a miss.
    """
    try:
        _write_atomic(verified_path(email, run_id), code)
    except OSError as e:
        logger.warning(f"Could not archive verified run {run_id}: {e}")


def load_verified(email: str, run_id: str) -> str | None:
    """The archived source, or None if it was never saved."""
    try:
        with open(verified_path(email, run_id), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _jobs_dir(email: str) -> str:
    d = os.path.join(USERS_DIR, _safe_email(email), "code_jobs")
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
        # A job that cannot be persisted still runs; it just will not survive a
        # refresh. Not worth failing the run over.
        logger.warning(f"Could not persist code job {record.get('job_id')}: {e}")


def new_job(email: str, code: str, purpose: str = "") -> dict:
    """Create a queued code job and persist it."""
    record = {
        "job_id": f"c_{uuid.uuid4().hex[:10]}",
        "email": email,
        "code": code,
        "purpose": (purpose or "").strip()[:500],
        "state": QUEUED,
        "stdout": "",
        "stderr": "",
        "exit_code": None,
        # Set once the run finishes cleanly; this is what the tutor cites in a
        # ```verified:<id> placeholder so the learner sees the exact source.
        "run_id": None,
        "images": [],
        "duration_ms": None,
        "timed_out": False,
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    JOBS[record["job_id"]] = record
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

    The ownership check is load-bearing for the same reason it is in
    demojobs.get_job: JOBS is keyed by job_id alone, so without it one
    learner's id would read another learner's record — here, the code and its
    output.
    """
    record = JOBS.get(job_id)
    if record is not None:
        return record if record.get("email") == email else None
    try:
        with open(_job_path(email, job_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_jobs(email: str) -> list[dict]:
    """All known code jobs for a user, newest first.

    Disk first so a refresh still sees them, then the in-memory record on top,
    which is fresher for a running job.
    """
    out: dict[str, dict] = {}
    try:
        for name in os.listdir(_jobs_dir(email)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(_jobs_dir(email), name), encoding="utf-8") as f:
                    record = json.load(f)
            except (OSError, ValueError):
                continue
            if record.get("job_id"):
                out[record["job_id"]] = record
    except OSError:
        pass

    for job_id, record in JOBS.items():
        if record.get("email") == email:
            out[job_id] = record

    jobs = list(out.values())
    jobs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return jobs


def active_count(email: str) -> int:
    return sum(
        1
        for r in JOBS.values()
        if r.get("email") == email and r.get("state") in (QUEUED, BUILDING)
    )


def _clip(text: str) -> str:
    """Trim output, keeping both ends — a traceback's useful line is last."""
    if not text or len(text) <= MAX_JOB_OUTPUT_CHARS:
        return text
    half = MAX_JOB_OUTPUT_CHARS // 2
    dropped = len(text) - (half * 2)
    return f"{text[:half]}\n... [{dropped} chars trimmed] ...\n{text[-half:]}"


def start_code_job(email: str, code: str, purpose: str = "") -> dict:
    """Create a job and start running in the background. Returns immediately.

    Called from the run_code tool, so it must not await the run — the tutor's
    turn is waiting on this return value.
    """
    record = new_job(email=email, code=code, purpose=purpose)

    async def run():
        # Wait for a slot. This mirrors demojobs._run_build: the check has to
        # be INSIDE the background task, because start_code_job must return to
        # the tutor immediately and cannot block. An earlier version only
        # logged here and started anyway, which made the limit decorative.
        #
        # active_count includes this job (new_job registered it as QUEUED
        # above), so `>` is correct and yields exactly MAX concurrent runs.
        while active_count(email) > MAX_CONCURRENT_CODE_JOBS:
            await asyncio.sleep(0.5)
        await _run_job(record)

    spawn(record, run)
    return record


async def _run_job(record: dict) -> None:
    """Run one code job to completion, recording what happened."""
    from backend.sandbox import run_code

    job_id = record["job_id"]
    update_job(job_id, state=BUILDING)
    try:
        result = await run_code(
            record["email"], record["code"], timeout=CODE_JOB_TIMEOUT_SEC
        )
    except asyncio.CancelledError:
        # spawn's runner records the CANCELLED state; just let it through.
        raise
    except Exception as e:
        logger.error(f"Code job {job_id} failed: {e}", exc_info=True)
        update_job(job_id, state=FAILED, error=str(e)[:500])
        return

    update_job(
        job_id,
        state=READY,
        stdout=_clip(result["stdout"]),
        stderr=_clip(result["stderr"]),
        exit_code=result["exit_code"],
        images=[img["name"] for img in result["images"]],
        duration_ms=result["duration_ms"],
        timed_out=result["timed_out"],
        # Only on a clean run — see the equivalent guard in the inline path.
        run_id=result["run_id"] if result["exit_code"] == 0 else None,
    )
    if result["exit_code"] == 0:
        save_verified(record["email"], result["run_id"], record["code"])


def sweep_stale(email: str | None = None) -> int:
    """Resolve orphaned runs and drop expired terminal ones. Best-effort.

    Two jobs in one, both needed at startup:

    1. A run that was in flight when the process died cannot resume. JOBS is
       empty after a restart, so the only trace is the on-disk record still
       saying "building" — and get_code_result would tell the model "still
       running, check again later" forever. Those are marked failed.
    2. Terminal records older than the TTL are removed so they do not
       accumulate on disk run after run.

    Scans disk rather than JOBS for exactly the reason above: at startup the
    in-memory registry has nothing in it.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - JOB_TTL_SECONDS
    touched = 0

    emails = [email] if email else _known_emails()
    for owner in emails:
        for record in list_jobs(owner):
            job_id = record.get("job_id")
            if not job_id:
                continue
            state = record.get("state")

            if state not in TERMINAL:
                # Only orphans reach here at startup; a genuinely running job
                # is in TASKS, which is empty in a fresh process. TASKS is
                # shared with demo jobs via spawn(), which is safe because ids
                # are prefixed (c_ here, j_ there) and cannot collide.
                if job_id in TASKS:
                    continue
                record.update(
                    state=FAILED,
                    error="server restarted while this run was in flight",
                    updated_at=_now(),
                )
                JOBS.pop(job_id, None)
                _persist(record)
                touched += 1
                continue

            try:
                ts = datetime.fromisoformat(record["updated_at"]).timestamp()
            except (KeyError, ValueError):
                continue
            if ts < cutoff:
                JOBS.pop(job_id, None)
                try:
                    os.remove(_job_path(owner, job_id))
                except OSError:
                    pass
                touched += 1
    return touched


def _known_emails() -> list[str]:
    """Users with a code_jobs directory on disk."""
    try:
        return [
            name for name in os.listdir(USERS_DIR)
            if os.path.isdir(os.path.join(USERS_DIR, name, "code_jobs"))
        ]
    except OSError:
        return []
