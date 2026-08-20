"""API endpoints for the Python Learning Agent."""

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from urllib.parse import quote
import asyncio
import json
import logging
import os
import numpy as np

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# How long the SSE generator waits for an event before emitting a heartbeat.
KEEPALIVE_SECONDS = 15

# Strong references to in-flight background tasks. asyncio only holds a weak
# reference to a running task, so a bare create_task() can be garbage-collected
# mid-flight — which tears down the SDK transport under a live agent turn.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_tracked(coro) -> asyncio.Task:
    """create_task, but the task is kept alive until it finishes."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


# Ceiling on a single decoded attachment. The browser enforces its own limit
# for a fast error, but that's a UX affordance — this is the actual control.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENTS_PER_TURN = 4


class Attachment(BaseModel):
    """An image the learner attached to a chat turn, as base64."""
    data: str
    content_type: str
    filename: str | None = None


class ChatRequest(BaseModel):
    message: str
    email: str
    session_id: str | None = None
    # Branch off another session, inheriting its context. Set by side-chats
    # and edit-branches on their first turn only.
    fork_from: str | None = None
    # Side-chats must not steal the user's active-session pointer.
    set_active: bool = True
    # Images attached to THIS turn only. Sent to the model as content blocks
    # and written to disk; the transcript keeps paths, never base64.
    attachments: list[Attachment] | None = None
    # A demo the learner is asking about, staged from the Demo pane:
    # {"demo_id": "d_xxx", "selection": {"text", "tag", "ids"}}. The server
    # turns this into a structural digest — the client never decides what the
    # model sees, and the html is never sent.
    demo_ref: dict | None = None


def _validate_attachments(attachments: list[Attachment] | None) -> list[Attachment]:
    """Reject anything that isn't a supported image within the size cap.

    Raises HTTPException(400) rather than letting a bad payload reach the SDK,
    where it would surface as an opaque streaming failure.
    """
    import base64
    import binascii

    from backend.episodic import EpisodicMemory

    if not attachments:
        return []

    if len(attachments) > MAX_ATTACHMENTS_PER_TURN:
        raise HTTPException(
            status_code=400,
            detail=f"at most {MAX_ATTACHMENTS_PER_TURN} images per message",
        )

    for att in attachments:
        if att.content_type not in EpisodicMemory.IMAGE_EXT_MAP:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported image type: {att.content_type}",
            )
        try:
            raw = base64.b64decode(att.data, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail="attachment is not valid base64")
        if not raw:
            raise HTTPException(status_code=400, detail="attachment is empty")
        if len(raw) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"image exceeds {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB limit",
            )

    return attachments


# --- Chat Endpoints ---


@router.post("/chat/stream")
async def chat_stream(body: ChatRequest):
    """Send a message and stream back events as SSE."""
    event_queue: asyncio.Queue = asyncio.Queue()

    # Validate before the task starts: a 400 here is a real HTTP error the
    # client can show, whereas a failure inside the stream is just an SSE
    # error frame buried in the transcript.
    attachments = _validate_attachments(body.attachments)

    async def on_event(event: dict):
        await event_queue.put(event)

    async def run_and_signal_done():
        from backend.agent import run_agent

        try:
            await run_agent(
                prompt=body.message,
                email=body.email,
                on_event=on_event,
                session_id=body.session_id,
                fork_from=body.fork_from,
                set_active=body.set_active,
                attachments=[a.model_dump() for a in attachments],
                demo_ref=body.demo_ref,
            )
        except Exception as e:
            await event_queue.put({"type": "error", "content": str(e)})
        finally:
            await event_queue.put({"type": "done"})

    _spawn_tracked(run_and_signal_done())

    async def event_generator():
        while True:
            # Bounded wait so a slow turn can't leave the socket silent. A
            # quiet stream is indistinguishable from a dead one to both the
            # browser and any intermediate proxy, so emit an SSE comment
            # frame as a heartbeat. The frontend parser only accepts lines
            # starting with "data: ", so comments are ignored client-side.
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue

            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "done":
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable proxy buffering (nginx and friends), which would
            # otherwise hold deltas back and defeat token-level streaming.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat")
async def chat(body: ChatRequest):
    """Non-streaming version — waits for the full response."""
    from backend.agent import run_agent

    events = []

    async def collect_event(event: dict):
        events.append(event)

    session_id = await run_agent(
        prompt=body.message,
        email=body.email,
        on_event=collect_event,
        session_id=body.session_id,
    )

    response_text = ""
    for event in events:
        if event["type"] == "assistant_message":
            response_text += event.get("content", "")

    return {
        "response": response_text,
        "session_id": session_id,
        "email": body.email,
        "events": events,
    }


# --- Code Runner Endpoints ---


class RunCodeRequest(BaseModel):
    email: str
    code: str
    timeout: float | None = None
    # Python only today. Present so a second runtime is a registry entry
    # rather than a signature change through the whole stack.
    language: str = "python"


class ResetVenvRequest(BaseModel):
    email: str


class InstallRequest(BaseModel):
    email: str
    packages: list[str]


def _clamp_timeout(value: float | None) -> float:
    from backend.sandbox import DEFAULT_TIMEOUT_SEC

    return value if (value and 0 < value <= 300) else DEFAULT_TIMEOUT_SEC


def _sse_response(event_generator):
    return StreamingResponse(
        event_generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable proxy buffering, which would otherwise hold chunks back
            # and defeat the point of streaming cell output.
            "X-Accel-Buffering": "no",
        },
    )


async def _pump_sse(queue: asyncio.Queue, request: Request, cancel: asyncio.Event):
    """Yield queued events as SSE frames until a terminal event arrives.

    Also watches for client disconnect: a browser-side AbortController drops
    the connection, and setting `cancel` is what turns that into a killpg of
    the cell's process group instead of an orphan running to completion.
    """
    # Poll for disconnect on a short tick rather than only when the queue goes
    # quiet. A cell that streams output keeps the queue busy, and a cell that
    # streams nothing blocks the generator entirely — in both cases a
    # keepalive-only check never fires, and the abandoned process runs to
    # completion. This is the Stop button's actual kill path.
    disconnect_poll = 1.0
    idle = 0.0
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=disconnect_poll)
            except asyncio.TimeoutError:
                if await request.is_disconnected():
                    break
                idle += disconnect_poll
                if idle >= KEEPALIVE_SECONDS:
                    idle = 0.0
                    yield ": ping\n\n"
                continue

            idle = 0.0
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") in ("done", "error"):
                return
    finally:
        # Starlette cancels this generator when the client goes away, so the
        # loop above may never get to observe the disconnect itself — the
        # CancelledError lands on whichever await is in flight. Signalling
        # from `finally` covers both routes out, and is what actually kills
        # the cell's process group when the learner hits Stop.
        cancel.set()


@router.post("/code/run")
async def code_run(body: RunCodeRequest):
    """Run a code cell and return the whole result at once.

    Kept alongside /code/run_stream: same core, useful for tests and any
    non-browser caller that doesn't want to parse SSE.
    """
    from backend.sandbox import run_code, VenvError

    try:
        return await run_code(body.email, body.code,
                              timeout=_clamp_timeout(body.timeout),
                              language=body.language)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except VenvError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Python environment isn't ready — try 'Reset venv'. ({e})",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"runner failed: {e}")


@router.post("/code/run_stream")
async def code_run_stream(body: RunCodeRequest, request: Request):
    """Run a code cell, streaming stdout/stderr as it is produced."""
    from backend.sandbox import run_code, VenvError

    queue: asyncio.Queue = asyncio.Queue()
    cancel = asyncio.Event()

    async def on_event(event: dict):
        await queue.put(event)

    async def run_and_signal_done():
        try:
            await run_code(body.email, body.code,
                           timeout=_clamp_timeout(body.timeout),
                           language=body.language,
                           on_event=on_event, cancel=cancel)
        except VenvError as e:
            await queue.put({
                "type": "error",
                "message": f"Python environment isn't ready — try 'Reset venv'. ({e})",
            })
        except Exception as e:
            logger.exception("code run failed")
            await queue.put({"type": "error", "message": str(e)})

    _spawn_tracked(run_and_signal_done())
    return _sse_response(_pump_sse(queue, request, cancel))


@router.post("/code/install")
async def code_install(body: InstallRequest, request: Request):
    """pip install into the user's venv, streaming pip's output."""
    from backend.sandbox import install_packages, VenvError

    queue: asyncio.Queue = asyncio.Queue()
    cancel = asyncio.Event()

    async def on_line(text: str):
        await queue.put({"type": "install", "packages": body.packages, "line": text})

    async def run_and_signal_done():
        try:
            result = await install_packages(body.email, body.packages, on_line=on_line)
            await queue.put({"type": "done", **result})
        except VenvError as e:
            await queue.put({
                "type": "error",
                "message": f"Python environment isn't ready — try 'Reset venv'. ({e})",
            })
        except Exception as e:
            logger.exception("install failed")
            await queue.put({"type": "error", "message": str(e)})

    _spawn_tracked(run_and_signal_done())
    return _sse_response(_pump_sse(queue, request, cancel))


@router.get("/code/artifacts/{email}/{run_id}/{filename}")
async def code_artifact(email: str, run_id: str, filename: str):
    """Serve a plot a cell produced.

    Same containment discipline as get_upload below: everything is resolved
    and then checked, so a traversal attempt lands outside the user's run
    directory and is refused.
    """
    from fastapi.responses import FileResponse

    from backend.sandbox import run_dir_for

    base = os.path.realpath(run_dir_for(email, run_id))
    target = os.path.realpath(os.path.join(base, filename))

    # commonpath, not startswith: the latter says /a/b-evil is inside /a/b.
    if target != base and os.path.commonpath([base, target]) != base:
        raise HTTPException(status_code=404, detail="not found")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="not found")

    return FileResponse(target)


@router.get("/code/verified/{email}/{run_id}")
async def code_verified_source(email: str, run_id: str):
    """The exact source the tutor executed for `run_id`.

    This is what closes the verification gap. The tutor used to run a snippet
    and then RETYPE it into chat, and a real session showed the retyped copy
    drifting from what actually ran — it grew an `export_text` call that had
    never been executed, and that call was the one that crashed for the
    learner. Serving cell.py from the run directory means the code the learner
    sees never passes back through the model: sandbox -> disk -> browser.

    Reads the durable archive under the user's data dir, NOT the run
    directory: scratch lives in /tmp and is swept hourly, while a saved
    conversation can cite a run weeks later.

    Same containment discipline as code_artifact above: resolve, then check.
    A run_id reaching here came from a chat message, so it is attacker-shaped
    input even in a single-user app.
    """
    from backend.codejobs import _verified_dir, verified_path

    base = os.path.realpath(_verified_dir(email))
    target = os.path.realpath(verified_path(email, run_id))

    # commonpath, not startswith: the latter says /a/b-evil is inside /a/b.
    if os.path.commonpath([base, target]) != base:
        raise HTTPException(status_code=404, detail="not found")
    if not os.path.isfile(target):
        # Never archived, or a run that failed. The frontend falls back to
        # rendering the placeholder as inert text.
        raise HTTPException(status_code=404, detail="not found")

    try:
        with open(target, encoding="utf-8") as f:
            return {"run_id": run_id, "code": f.read()}
    except OSError:
        raise HTTPException(status_code=404, detail="not found")


@router.post("/code/reset_venv")
async def code_reset_venv(body: ResetVenvRequest):
    """Wipe and recreate the user's venv. Useful when an install gets corrupted."""
    from backend.sandbox import reset_venv

    try:
        await reset_venv(body.email)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"reset failed: {e}")


# --- Uploaded Images ---


@router.get("/uploads/{email}/{filename}")
async def get_upload(email: str, filename: str):
    """Serve an image the learner attached to a chat turn.

    Deliberately a route rather than a StaticFiles mount over data/uploads:
    that tree is user data keyed by email, and the filename reaches us as a
    URL segment. Everything is resolved and then checked for containment, so
    a traversal attempt lands outside the user's directory and is refused.
    """
    from fastapi.responses import FileResponse

    from backend.episodic import EpisodicMemory

    uploads_dir = os.path.realpath(EpisodicMemory.uploads_dir_for(email))
    target = os.path.realpath(os.path.join(uploads_dir, filename))

    # os.path.commonpath, not startswith: the latter says /a/b-evil is inside
    # /a/b. Compare resolved paths so symlinks can't sidestep the check.
    if target != uploads_dir and os.path.commonpath([uploads_dir, target]) != uploads_dir:
        raise HTTPException(status_code=404, detail="not found")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="not found")

    return FileResponse(target)


# --- Knowledge & Quiz Endpoints ---


@router.get("/knowledge/{email}")
async def get_knowledge(email: str):
    """Get a user's knowledge.json (semantic memory)."""
    from backend.agent import _get_user_data_dir
    from backend.knowledge import KnowledgeStore

    user_dir = _get_user_data_dir(email)
    store = KnowledgeStore(user_dir)
    return store.load_knowledge()


@router.get("/quiz/{email}")
async def get_quiz_history(email: str):
    """Get a user's quiz_history.json."""
    from backend.agent import _get_user_data_dir
    from backend.knowledge import KnowledgeStore

    user_dir = _get_user_data_dir(email)
    store = KnowledgeStore(user_dir)
    return store.load_quiz_history()


@router.get("/graph/{email}")
async def get_knowledge_graph(email: str):
    """Get the full curriculum graph with user mastery overlaid.

    Returns nodes (concepts) and edges (prerequisites) for graph visualization.
    """
    from backend.agent import _get_user_data_dir
    from backend.knowledge import (
        KnowledgeStore, CURRICULUM_GRAPH, CATEGORY_COLORS, TRACK_COLORS,
    )

    user_dir = _get_user_data_dir(email)
    store = KnowledgeStore(user_dir)
    data = store.load_knowledge()
    user_concepts = data.get("concepts", {})

    # Build a reverse map: for each concept, which tracks rely on it as a prereq?
    # Bridge concepts (e.g. numpy_basics) feed multiple downstream tracks and get
    # a multi-color pie ring in the frontend.
    feeds: dict[str, set[str]] = {cid: set() for cid in CURRICULUM_GRAPH}
    for cid, info in CURRICULUM_GRAPH.items():
        own_track = info.get("track", "python")
        for prereq in info["prereqs"]:
            if prereq in feeds:
                feeds[prereq].add(own_track)

    nodes = []
    edges = []

    for concept_id, info in CURRICULUM_GRAPH.items():
        user_data = user_concepts.get(concept_id, {})
        mastery = user_data.get("mastery", "not_started")
        own_track = info.get("track", "python")
        feeds_tracks = sorted(feeds[concept_id] | {own_track})
        nodes.append({
            "id": concept_id,
            "name": info["name"],
            "category": info["category"],
            "track": own_track,
            "color": CATEGORY_COLORS.get(info["category"], "#565f89"),
            "mastery": mastery,
            "review_count": user_data.get("review_count", 0),
            "last_reviewed": user_data.get("last_reviewed", ""),
            "feeds_tracks": feeds_tracks,
            "is_bridge": len(feeds_tracks) >= 2,
        })
        for prereq in info["prereqs"]:
            edges.append({"source": prereq, "target": concept_id})

    # Concepts the tutor taught that the fixed curriculum never listed. Without
    # this the graph shows only CURRICULUM_GRAPH, so anything learned past the
    # built-in syllabus is recorded in the knowledge file but invisible here.
    categories = store.categories()
    for concept_id, user_data in user_concepts.items():
        if concept_id in CURRICULUM_GRAPH:
            continue
        category = user_data.get("category", "")
        nodes.append({
            "id": concept_id,
            "name": user_data.get("name") or concept_id.replace("_", " ").title(),
            "category": category,
            "track": user_data.get("track", "custom"),
            "color": categories.get(category, {}).get("color", "#565f89"),
            "mastery": user_data.get("mastery", "not_started"),
            "review_count": user_data.get("review_count", 0),
            "last_reviewed": user_data.get("last_reviewed", ""),
            "feeds_tracks": [],
            "is_bridge": False,
            "is_custom": True,
        })
        # Only to prereqs that exist as nodes — a dangling edge would leave the
        # force layout referencing a node id it never received.
        known = set(CURRICULUM_GRAPH) | set(user_concepts)
        for prereq in user_data.get("prerequisites", []):
            if prereq in known:
                edges.append({"source": prereq, "target": concept_id})

    return {
        "nodes": nodes,
        "edges": edges,
        "categories": [
            {"id": cat, "name": meta["label"], "color": meta["color"]}
            for cat, meta in categories.items()
        ],
        "track_colors": TRACK_COLORS,
        "profile": data.get("profile", {}),
    }


@router.get("/episodes/{email}")
async def get_episodes(email: str):
    """Get recent episodic memories for a user."""
    from backend.agent import _get_episodic

    episodic = _get_episodic()
    episodes = episodic.get_recent_episodes(email, n=20)
    return {"episodes": episodes, "count": len(episodes)}


# --- Info ---


@router.get("/tools")
async def list_tools():
    """List the MCP tools available to the agent."""
    from backend.agent import MCP_TOOL_NAMES

    return {"tools": MCP_TOOL_NAMES}


@router.get("/memory/users")
async def list_memory_users():
    """List all users with their session info."""
    from backend.memory import list_users

    return {"users": list_users()}


@router.get("/memory/users/{email}")
async def get_user_memory(email: str):
    """Get the user's sessions and info."""
    from backend.memory import get_user_sessions, _safe_email, USERS_DIR

    sessions = get_user_sessions(email)
    if not sessions:
        raise HTTPException(status_code=404, detail="User not found")
    return {"email": email, "sessions": sessions}


# --- Session Management Endpoints ---


class SessionCreateRequest(BaseModel):
    name: str = ""
    # A derived session names its origin so the sidebar can nest it instead of
    # listing it flat. "" keeps the plain-main default.
    kind: str = ""
    parent_session_id: str = ""
    # Register an id the agent already minted (a fork) rather than inventing
    # one. Empty means "create a fresh session", the "+" button's behaviour.
    session_id: str = ""


class SessionRenameRequest(BaseModel):
    name: str


class EndSideChatRequest(BaseModel):
    """Close a side chat and fold what was learned back into the parent."""
    parent_session_id: str
    topic: str = ""


class BranchRequest(BaseModel):
    """Rewrite history from a turn onward with an edited message."""
    turn_index: int
    message: str


@router.get("/palette.css")
async def palette_css():
    """Serve the derived track/category palette as CSS custom properties.

    Curriculum colour is owned by knowledge.py — this endpoint stops the
    frontend from keeping its own drifting copy. Linked as a stylesheet, so
    the browser caches it like any other CSS.
    """
    from backend.knowledge import build_oklch_palette, CATEGORY_LABELS, TRACK_ORDER

    lines = [
        "/* Generated from backend/knowledge.py — do not edit by hand.",
        "   Curriculum colour is derived per theme: a hue that glows on black",
        "   is thin and washed out on white, so both ramps are emitted and the",
        "   data-theme attribute selects between them. */",
    ]

    for theme, selector in (("dark", ":root"), ("light", '[data-theme="light"]')):
        palette = build_oklch_palette(theme)
        lines.append(f"{selector} {{")
        for track in TRACK_ORDER:
            if track in palette["tracks"]:
                lines.append(f"  --track-{track}: {palette['tracks'][track]};")
        lines.append("")
        for cat, css in sorted(palette["categories"].items()):
            label = CATEGORY_LABELS.get(cat, cat.replace("_", " ").title())
            lines.append(f"  --cat-{cat.replace('_', '-')}: {css};  /* {label} */")
        lines.append("}")
        lines.append("")
    # Must be served as text/css — browsers refuse to apply a stylesheet with
    # any other MIME type, and the custom properties silently resolve to "".
    return Response(content="\n".join(lines), media_type="text/css")


@router.get("/sessions/{email}")
async def list_sessions(email: str):
    """List all sessions for a user."""
    from backend.memory import get_user_sessions, get_active_session

    sessions = get_user_sessions(email)
    active = get_active_session(email)
    return {"sessions": sessions, "active_session": active}


@router.post("/sessions/{email}")
async def create_session(email: str, body: SessionCreateRequest):
    """Create a new empty session, or register one the agent already minted.

    The second case is what a side chat needs. Its session id only exists once
    the SDK forks, and the agent's own save_user_session runs after the turn
    completes — so the turn-1 rename PATCH would hit a row that is not there
    yet and 404. Registering the id here as soon as session_init reports it
    closes that gap; save_user_session updates in place, so the agent's later
    save is harmless.

    A derived session never becomes active: it is a background conversation,
    and stealing the pointer would change which chat the user returns to.
    """
    import uuid
    from backend.memory import save_user_session

    session_id = body.session_id or str(uuid.uuid4())
    name = body.name or "New Session"
    save_user_session(
        email,
        session_id,
        name=name,
        kind=body.kind,
        parent_session_id=body.parent_session_id,
        set_active=not body.kind,
    )
    return {"session_id": session_id, "name": name}


@router.patch("/sessions/{email}/{session_id}")
async def update_session(email: str, session_id: str, body: SessionRenameRequest):
    """Rename a session."""
    from backend.memory import rename_session

    if not rename_session(email, session_id, body.name):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, "name": body.name}


@router.post("/sessions/{email}/{session_id}/activate")
async def activate_session(email: str, session_id: str):
    """Switch the active session for a user."""
    from backend.memory import set_active_session

    if not set_active_session(email, session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"active_session": session_id}


@router.get("/sessions/{email}/{session_id}/history")
async def get_session_history(email: str, session_id: str):
    """Get the conversation history for a session."""
    from backend.memory import read_turns

    # read_turns is the single source of truth: it migrates legacy sessions and
    # strips superseded turns, so edited-away content never reaches the client.
    return {"turns": read_turns(email, session_id), "session_id": session_id}


@router.delete("/sessions/{email}/{session_id}")
async def remove_session(email: str, session_id: str):
    """Delete a session and its transcript."""
    from backend.memory import delete_session

    if not delete_session(email, session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "deleted", "session_id": session_id}


@router.post("/sessions/{email}/prune")
async def prune_sessions(email: str):
    """Drop sessions that were created but never used."""
    from backend.memory import prune_empty_sessions

    removed = prune_empty_sessions(email)
    return {"status": "ok", "removed": removed, "count": len(removed)}


def _clean_title(raw: str) -> str:
    """Reduce a model reply to a bare title.

    Small models sometimes ignore "reply with the title only" and prefix
    something like "Based on the transcript, the subject covered is: X".
    Strip that lead-in rather than storing it, and drop a trailing clause so
    a truncated sentence never becomes the name.
    """
    import re as _re

    # Take the first non-empty line: a model that ignores the instruction
    # tends to answer with a list or a paragraph, and the rest is noise.
    first = next((ln for ln in raw.strip().splitlines() if ln.strip()), "")
    text = " ".join(first.split())
    # Drop list markers ("1.", "-", "*") and markdown emphasis.
    text = _re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    # A dash-separated gloss ("Dictionary creation - Creating a dict") keeps
    # only the head.
    text = _re.split(r"\s+[-–—]\s+", text)[0]
    # Prefer the part after a lead-in colon ("… the subject covered is: X").
    if ":" in text:
        head, _, tail = text.partition(":")
        if len(head.split()) > 3 and tail.strip():
            text = tail.strip()
    text = _re.sub(
        r"^(?:the\s+)?(?:title|subject|topic)(?:\s+\w+){0,3}\s+(?:is|would be)\s+",
        "", text, flags=_re.I,
    )
    text = text.strip().strip('"').strip("'").rstrip(".")

    # If it still opens like a sentence about the task, the model ignored the
    # instruction and no amount of trimming yields a real title — reject it
    # so the caller can keep the existing name rather than store prose.
    if _re.match(r"^(looking at|based on|here|this (?:conversation|session)|"
                 r"the (?:conversation|session|learner|student))\b", text, _re.I):
        return ""

    words = text.split()
    if len(words) > 7:
        text = " ".join(words[:7])
    return text[:48].strip()


@router.post("/sessions/{email}/{session_id}/retitle")
async def retitle_session(email: str, session_id: str):
    """Give a session a short title based on what it actually covered.

    Called once a conversation has enough turns to have a real subject —
    the first message alone is often a greeting, or the topic drifts. Reuses
    the same one-shot summarizer pattern as the side-chat fold: no memory
    tools, so it cannot write anything on its own.
    """
    from backend.agent import run_agent_internal
    from backend.memory import read_turns, rename_session

    turns = read_turns(email, session_id)
    exchanges = [
        t for t in turns
        if t.get("type") in ("user", "assistant_message") and t.get("content")
    ]
    if not exchanges:
        raise HTTPException(status_code=404, detail="Session has no conversation")

    transcript = "\n\n".join(
        f"{'User' if t['type'] == 'user' else 'Tutor'}: {str(t['content'])[:600]}"
        for t in exchanges[:12]
    )

    async def ask(prompt: str) -> str:
        chunks: list[str] = []

        async def collect(event: dict):
            if event.get("type") == "assistant_message" and event.get("content"):
                chunks.append(event["content"])

        await run_agent_internal(
            prompt=prompt,
            system_prompt="You reply with a short title and nothing else.",
            on_event=collect,
        )
        return _clean_title("".join(chunks))

    title = await ask(
        "Give this tutoring conversation a title of at most 5 words naming "
        "the specific subject covered. No quotes, no punctuation at the end, "
        "no preamble — reply with the title only.\n\n"
        f"--- transcript ---\n{transcript}"
    )

    # A long or rambling transcript sometimes draws an explanation instead of
    # a title. One firmer retry on a shorter excerpt is usually enough.
    if not title:
        title = await ask(
            "TITLE ONLY. Three to five words. No sentence, no explanation, "
            "no list, no colon.\n\nExample good replies:\n"
            "Python Dictionary Basics\nCAP Theorem And Tradeoffs\n\n"
            f"--- transcript ---\n{transcript[:1500]}"
        )

    if not title:
        raise HTTPException(status_code=502, detail="Could not generate a title")

    rename_session(email, session_id, title)
    return {"session_id": session_id, "name": title}


@router.post("/sessions/{email}/{session_id}/branch")
async def branch_session(email: str, session_id: str, body: BranchRequest):
    """Rewrite a conversation from `turn_index` onward with an edited message.

    The model cannot un-remember, so a new session is built that never saw the
    original wording: replay the surviving prefix into a fresh session, then
    send the edited message. The old session is left intact and switchable.

    Returns the new session id; the caller then streams into it as normal.
    """
    from backend.agent import _get_episodic, _get_user_data_dir, run_agent_internal
    from backend.knowledge import KnowledgeStore
    from backend.memory import (
        get_user_sessions,
        read_turns,
        save_user_session,
        supersede_from,
    )
    from backend.prompts import build_system_prompt

    turns = read_turns(email, session_id)
    if not turns:
        raise HTTPException(status_code=404, detail="Session not found")

    prefix = [t for t in turns if t.get("index", 0) < body.turn_index]
    if not any(t.get("type") == "user" for t in prefix):
        # Editing the very first message: nothing to carry over, so the branch
        # is simply a fresh conversation.
        prefix = []

    # Rebuild the surviving context as a single bootstrap prompt. This is the
    # same prompt-stuffing strategy replay_from_memory uses.
    user_data_dir = _get_user_data_dir(email)
    knowledge_state = KnowledgeStore(user_data_dir).format_for_system_prompt()
    recent = _get_episodic().format_for_system_prompt(email, n=5)
    system_prompt = build_system_prompt(knowledge_state, recent)

    new_session_id = None
    if prefix:
        lines = []
        for t in prefix:
            if t.get("type") == "user" and t.get("content"):
                lines.append(f"User: {t['content']}")
            elif t.get("type") == "assistant_message" and t.get("content"):
                lines.append(f"Tutor: {t['content']}")
        transcript = "\n\n".join(lines[-40:])  # cap: only recent context matters
        bootstrap = (
            "Here is our conversation so far. Acknowledge it in one short line "
            "and wait for the learner's next message — do not re-teach it.\n\n"
            f"{transcript}"
        )
        new_session_id = await run_agent_internal(
            prompt=bootstrap,
            system_prompt=system_prompt,
            user_data_dir=user_data_dir,
            email=email,
        )

    # Retract the edited turn and everything it led to. Applied to the ORIGINAL
    # session only if we are rewriting in place; here the original is preserved
    # as its own branch, and the tombstone goes on the new session's copy.
    name = next(
        (s.get("name") for s in get_user_sessions(email) if s["session_id"] == session_id),
        "Session",
    )
    if new_session_id:
        save_user_session(
            email,
            new_session_id,
            name=f"{name} (edited)",
            set_active=True,
            kind="branch",
            parent_session_id=session_id,
        )
        # The branch's own transcript starts after the bootstrap, so hide the
        # scaffolding turn from the UI.
        supersede_from(email, new_session_id, from_index=1, reason="branch_bootstrap")
    else:
        # Editing the first message leaves no prefix to replay, so there is no
        # bootstrap turn and hence no session yet. The caller streams with
        # session_id=None, which creates a fresh session on its first turn —
        # exactly the desired "start over with different wording".
        logger.info(f"Branch from turn {body.turn_index} has no prefix — caller starts fresh")

    return {
        "session_id": new_session_id,
        "parent_session_id": session_id,
        "turn_index": body.turn_index,
        "message": body.message,
    }


@router.post("/sessions/{email}/{session_id}/end-side")
async def end_side_chat(email: str, session_id: str, body: EndSideChatRequest):
    """Summarize a side chat, fold it into its parent, and record the learning.

    Three effects, all reusing existing machinery:
      1. a summary turn appended to the parent's transcript
      2. an episode saved to ChromaDB (episodic memory is keyed by email, so a
         side chat's learning is searchable from any session)
      3. concepts promoted in the knowledge graph
    """
    import json as _json

    from backend.agent import _get_episodic, _get_user_data_dir, run_agent_internal
    from backend.knowledge import CURRICULUM_GRAPH, KnowledgeStore
    from backend.memory import append_turn_to_session, read_turns

    turns = read_turns(email, session_id)
    exchanges = [
        t for t in turns if t.get("type") in ("user", "assistant_message") and t.get("content")
    ]
    if not exchanges:
        raise HTTPException(status_code=404, detail="Side chat has no conversation to summarize")

    transcript = "\n\n".join(
        f"{'User' if t['type'] == 'user' else 'Tutor'}: {t['content']}" for t in exchanges
    )

    known = ", ".join(sorted(CURRICULUM_GRAPH.keys()))
    ask = (
        "Summarize this tutoring side-conversation for the learner's record.\n"
        "Reply with ONLY a JSON object, no prose or code fences:\n"
        '{"summary": "2-3 sentences, what was asked and understood",\n'
        ' "topics": ["short", "tags"],\n'
        ' "concepts": [{"concept_id": "<id from the list below, or a new snake_case id>",\n'
        '               "name": "Human Readable", "mastery": "introduced|practiced|mastered"}]}\n'
        "Only include concepts genuinely covered. Prefer existing ids:\n"
        f"{known}\n\n"
        f"--- transcript ---\n{transcript}"
    )

    # No user_data_dir: this is a one-shot summarizer, so it gets no memory
    # tools and cannot write anything on its own.
    #
    # run_agent_internal returns the session id, not the reply — the text only
    # arrives through on_event, so collect it there.
    chunks: list[str] = []

    async def collect(event: dict):
        if event.get("type") == "assistant_message" and event.get("content"):
            chunks.append(event["content"])

    await run_agent_internal(
        prompt=ask,
        system_prompt="You output only valid JSON.",
        on_event=collect,
    )
    raw = "".join(chunks)

    parsed = {}
    if raw:
        text = raw.strip()
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = _json.loads(text[start:end + 1])
            except Exception:
                logger.warning("Side-chat summary was not valid JSON; falling back")

    summary = parsed.get("summary") or f"Side discussion about {body.topic or 'a related topic'}."
    topics = parsed.get("topics") or ([body.topic] if body.topic else [])
    concepts = parsed.get("concepts") or []

    # 1. Fold a summary card into the parent conversation.
    append_turn_to_session(email, body.parent_session_id, {
        "type": "side_chat_summary",
        "content": summary,
        "side_session_id": session_id,
        "topic": body.topic,
        "topics": topics,
    })

    # 2. Episodic memory — searchable from any session.
    episode_id = None
    try:
        episode_id = _get_episodic().save_episode(
            email=email, summary=summary, topics=topics, session_id=session_id,
        )
    except Exception as e:
        logger.error(f"Side-chat episode save failed: {e}", exc_info=True)

    # 3. Knowledge graph — the learning counts even though it happened aside.
    store = KnowledgeStore(_get_user_data_dir(email))
    updated = []
    for c in concepts:
        cid = (c.get("concept_id") or "").strip()
        if not cid:
            continue
        known_concept = CURRICULUM_GRAPH.get(cid, {})
        try:
            store.upsert_concept(
                concept_id=cid,
                name=c.get("name") or known_concept.get("name", cid),
                category=known_concept.get("category", "side_topics"),
                mastery=c.get("mastery", "introduced"),
                prerequisites=known_concept.get("prereqs", []),
                notes=f"Learned in a side chat about {body.topic}" if body.topic else "Learned in a side chat",
            )
            updated.append(cid)
        except Exception as e:
            logger.error(f"upsert_concept failed for {cid}: {e}", exc_info=True)

    return {
        "status": "ok",
        "summary": summary,
        "topics": topics,
        "concepts_updated": updated,
        "episode_id": episode_id,
        "side_session_id": session_id,
        "parent_session_id": body.parent_session_id,
    }


# --- ChromaDB Visualization Endpoints ---


def _pca_2d(embeddings: list[list[float]]) -> list[dict]:
    """Project high-dimensional embeddings to 2D using PCA (no sklearn needed)."""
    if not embeddings or len(embeddings) < 1:
        return []

    X = np.array(embeddings, dtype=np.float64)

    # Center the data
    mean = X.mean(axis=0)
    Xc = X - mean

    if X.shape[0] == 1:
        return [{"x": 0.0, "y": 0.0}]

    # Covariance matrix and eigendecomposition
    cov = np.cov(Xc, rowvar=False)
    eigenvalues, eigenvectors = np.linalg.eigh(cov)

    # Take top 2 components (eigh returns ascending order)
    top2 = eigenvectors[:, -2:][:, ::-1]
    projected = Xc @ top2

    # Normalize to [-1, 1] for display
    for dim in range(2):
        col = projected[:, dim]
        rng = col.max() - col.min()
        if rng > 0:
            projected[:, dim] = 2 * (col - col.min()) / rng - 1

    return [{"x": round(float(p[0]), 4), "y": round(float(p[1]), 4)} for p in projected]


@router.get("/chroma/stats")
async def chroma_stats():
    """Get ChromaDB collection statistics."""
    from backend.agent import _get_episodic

    episodic = _get_episodic()

    summaries_col = episodic._conversations
    exchanges_col = episodic._exchanges

    return {
        "collections": {
            "conversations": {
                "total_documents": summaries_col.count(),
                "description": "High-level session summaries (agent-saved)",
            },
            "conversation_exchanges": {
                "total_documents": exchanges_col.count(),
                "description": "Detailed user+assistant exchanges (auto-saved)",
            },
        },
        "total_documents": summaries_col.count() + exchanges_col.count(),
        "distance_metric": "cosine",
        "embedding_model": "all-MiniLM-L6-v2",
        "embedding_dimensions": 384,
    }


@router.get("/chroma/documents")
async def chroma_documents(email: str | None = None, limit: int = 100, collection: str = "conversations"):
    """Get all documents from ChromaDB with their embeddings projected to 2D.

    Args:
        collection: 'conversations' (summaries) or 'exchanges' (detailed exchanges)
    """
    from backend.agent import _get_episodic

    episodic = _get_episodic()
    col = episodic._exchanges if collection == "exchanges" else episodic._conversations
    count = col.count()

    if count == 0:
        return {"documents": [], "points_2d": [], "count": 0}

    kwargs = {"include": ["documents", "metadatas", "embeddings"], "limit": min(limit, count)}
    if email:
        kwargs["where"] = {"user_email": email}

    try:
        results = col.get(**kwargs)
    except Exception:
        return {"documents": [], "points_2d": [], "count": 0}

    docs = []
    embeddings = []
    for i in range(len(results["ids"])):
        meta = results["metadatas"][i] or {}
        topics = meta.get("topics", "").split(",") if meta.get("topics") else []
        doc_entry = {
            "id": results["ids"][i],
            "document": results["documents"][i] or "",
            "email": meta.get("user_email", ""),
            "timestamp": meta.get("timestamp", ""),
            "topics": [t for t in topics if t],
            "session_id": meta.get("session_id", ""),
        }
        # Include exchange-specific fields when viewing exchanges
        if collection == "exchanges":
            doc_entry["user_message"] = meta.get("user_message", "")
            doc_entry["raw_exchange"] = meta.get("raw_exchange", "")
            doc_entry["exchange_index"] = meta.get("exchange_index", 0)
        docs.append(doc_entry)
        if results["embeddings"] is not None and len(results["embeddings"]) > i:
            emb = results["embeddings"][i]
            if emb is not None and len(emb) > 0:
                embeddings.append(list(emb) if not isinstance(emb, list) else emb)

    # Project to 2D
    points_2d = _pca_2d(embeddings) if embeddings else []

    return {"documents": docs, "points_2d": points_2d, "count": len(docs), "collection": collection}


class SearchRequest(BaseModel):
    query: str
    email: str | None = None
    n_results: int = 10


@router.post("/chroma/search")
async def chroma_search(body: SearchRequest):
    """Semantic search across ChromaDB and return results with similarity scores."""
    from backend.agent import _get_episodic

    episodic = _get_episodic()
    col = episodic._conversations
    count = col.count()

    if count == 0:
        return {"results": [], "query": body.query}

    kwargs = {
        "query_texts": [body.query],
        "n_results": min(body.n_results, count),
        "include": ["documents", "metadatas", "distances"],
    }
    if body.email:
        kwargs["where"] = {"user_email": body.email}

    try:
        results = col.query(**kwargs)
    except Exception:
        return {"results": [], "query": body.query}

    items = []
    if results["documents"] and results["documents"][0]:
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i] or {}
            distance = results["distances"][0][i]
            topics = meta.get("topics", "").split(",") if meta.get("topics") else []
            items.append({
                "document": doc,
                "email": meta.get("user_email", ""),
                "timestamp": meta.get("timestamp", ""),
                "topics": [t for t in topics if t],
                "similarity": round(1 - distance, 4),
            })

    return {"results": items, "query": body.query}


@router.post("/chroma/backfill/{email}")
async def backfill_exchanges(email: str):
    """Backfill all existing session transcripts into the exchanges collection.

    Scans all sessions for a user and indexes any exchanges not yet in ChromaDB.
    Safe to run multiple times — uses deterministic IDs for deduplication.
    """
    from backend.agent import _get_episodic
    from backend.memory import get_user_sessions, read_turns

    episodic = _get_episodic()
    sessions = get_user_sessions(email)
    total_saved = 0

    for session_info in sessions:
        sid = session_info.get("session_id", "")
        if not sid:
            continue

        # Superseded turns are excluded, so edited-away content stays out of
        # episodic memory too.
        turns = read_turns(email, sid)
        if not turns:
            continue

        saved = episodic.backfill_exchanges(email, sid, turns)
        total_saved += saved

    return {
        "status": "ok",
        "sessions_scanned": len(sessions),
        "exchanges_saved": total_saved,
    }


# --- Feedback Endpoints ---


class FeedbackBody(BaseModel):
    content: str


@router.get("/feedback/aspects")
async def list_feedback_aspects():
    """List the fixed set of aspect names the Preferences UI can edit."""
    from backend.feedback import ASPECTS
    return {"aspects": list(ASPECTS)}


@router.get("/feedback/{email}/{aspect}")
async def get_feedback_files(email: str, aspect: str):
    """Return both layers for an aspect: global (read-only) + user (editable)."""
    from backend.feedback import ASPECTS, load_global_feedback, load_user_feedback
    if aspect not in ASPECTS:
        raise HTTPException(status_code=400, detail=f"Unknown aspect {aspect!r}")
    return {
        "aspect": aspect,
        "global": load_global_feedback(aspect),
        "user": load_user_feedback(email, aspect),
    }


@router.put("/feedback/{email}/{aspect}")
async def save_feedback_file(email: str, aspect: str, body: FeedbackBody):
    """Persist the learner's per-aspect feedback file."""
    from backend.feedback import ASPECTS, save_user_feedback
    if aspect not in ASPECTS:
        raise HTTPException(status_code=400, detail=f"Unknown aspect {aspect!r}")
    save_user_feedback(email, aspect, body.content)
    return {"ok": True}


# --- Notes Endpoints ---
#
# A note is a canvas of absolutely-positioned blocks, optionally over a
# rasterized PDF. Images and pages are NOT stored in the note document — they
# go through the same save_attachment/uploads path as chat attachments, so
# there is exactly one asset tree and one traversal check in this codebase.


class NoteCreateBody(BaseModel):
    title: str = ""


class NoteSaveBody(BaseModel):
    """Partial update — only fields that are sent get written."""
    title: str | None = None
    canvas_height: int | None = None
    next_z: int | None = None
    blocks: list[dict] | None = None
    groups: dict | None = None
    doc: dict | None = None


class NotePinBody(BaseModel):
    session_id: str | None = None   # null unpins


class NoteImageBody(BaseModel):
    data: str                       # base64, no data: prefix
    content_type: str = "image/png"


@router.get("/notes/{email}")
async def list_notes_route(email: str):
    """List a user's notes for the picker (denormalized index, one read)."""
    from backend.notes import list_notes
    return {"notes": list_notes(email)}


@router.post("/notes/{email}")
async def create_note_route(email: str, body: NoteCreateBody):
    """Create an empty note."""
    from backend.notes import create_note
    return create_note(email, body.title)


@router.get("/notes/{email}/pinned/{session_id}")
async def get_pinned_note(email: str, session_id: str):
    """Which note is pinned to this chat, following branch ancestry.

    Declared before /notes/{email}/{note_id} so "pinned" isn't captured as a
    note id — FastAPI matches routes in declaration order.
    """
    from backend.notes import get_pinned
    return {"note_id": get_pinned(email, session_id)}


@router.get("/notes/{email}/{note_id}")
async def get_note_route(email: str, note_id: str):
    """Full note document, including every block."""
    from backend.notes import get_note
    note = get_note(email, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@router.put("/notes/{email}/{note_id}")
async def save_note_route(email: str, note_id: str, body: NoteSaveBody):
    """Persist canvas state. Called by autosave, so it must stay cheap."""
    from backend.notes import save_note, validate_blocks

    blocks = body.blocks
    if blocks is not None:
        try:
            blocks = validate_blocks(blocks)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    note = save_note(
        email,
        note_id,
        title=body.title,
        canvas_height=body.canvas_height,
        next_z=body.next_z,
        blocks=blocks,
        groups=body.groups,
        doc=body.doc,
    )
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"ok": True, "updated_at": note["updated_at"]}


@router.delete("/notes/{email}/{note_id}")
async def delete_note_route(email: str, note_id: str):
    """Delete a note. Referenced images survive — chat turns may use them."""
    from backend.notes import delete_note
    if not delete_note(email, note_id):
        raise HTTPException(status_code=404, detail="Note not found")
    return {"ok": True}


@router.put("/notes/{email}/{note_id}/pin")
async def pin_note_route(email: str, note_id: str, body: NotePinBody):
    """Pin this note to a chat session, or unpin with a null session_id."""
    from backend.notes import pin_note
    if not pin_note(email, note_id, body.session_id):
        raise HTTPException(status_code=404, detail="Note not found")
    return {"ok": True}


@router.post("/notes/{email}/{note_id}/images")
async def add_note_image(email: str, note_id: str, body: NoteImageBody):
    """Store a pasted screenshot and return its URL plus true dimensions.

    Reuses the chat-attachment validation and storage wholesale: same type
    whitelist, same size cap, same uploads directory, same serving route. The
    only addition is width/height, which the canvas needs to size the block at
    the image's real aspect ratio.
    """
    import base64
    import binascii

    from backend.episodic import EpisodicMemory
    from backend.notes import get_note

    if get_note(email, note_id) is None:
        raise HTTPException(status_code=404, detail="Note not found")

    if body.content_type not in EpisodicMemory.IMAGE_EXT_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported image type: {body.content_type}",
        )
    try:
        raw = base64.b64decode(body.data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="image is not valid base64")
    if not raw:
        raise HTTPException(status_code=400, detail="image is empty")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"image exceeds {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB limit",
        )

    saved = EpisodicMemory.save_attachment(email, body.data, body.content_type)

    from backend.pdfimport import image_size
    dims = image_size(raw)

    return {
        "ok": True,
        "src": f"/api/uploads/{quote(email)}/{saved['filename']}",
        "w": dims[0] if dims else None,
        "h": dims[1] if dims else None,
    }


@router.post("/notes/{email}/{note_id}/document")
async def import_note_document(email: str, note_id: str, file: UploadFile = File(...)):
    """Rasterize an uploaded PDF into the note's background page column.

    Multipart rather than base64 here: a 30MB lecture PDF would become ~40MB
    of JSON string in browser memory. Rendering runs on a worker thread so a
    200-page import doesn't stall the event loop — chat streams over SSE from
    this same process.
    """
    import asyncio

    from backend.episodic import EpisodicMemory
    from backend.notes import get_note, set_document
    from backend.pdfimport import (
        MAX_PDF_BYTES,
        PdfImportUnavailable,
        rasterize_pdf,
    )

    if get_note(email, note_id) is None:
        raise HTTPException(status_code=404, detail="Note not found")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"PDF exceeds {MAX_PDF_BYTES // (1024 * 1024)}MB limit",
        )

    out_dir = EpisodicMemory.uploads_dir_for(email)
    try:
        pages = await asyncio.to_thread(rasterize_pdf, data, out_dir, email)
    except PdfImportUnavailable:
        raise HTTPException(
            status_code=501,
            detail="PDF import needs PyMuPDF on the server (pip install PyMuPDF)",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    doc = {
        "kind": "pdf",
        "filename": file.filename or "document.pdf",
        "page_gap": 24,
        "layout_width": None,   # set by the client once it lays the pages out
        "pages": pages,
    }
    if set_document(email, note_id, doc) is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"ok": True, "doc": doc}


@router.delete("/notes/{email}/{note_id}/document")
async def clear_note_document(email: str, note_id: str):
    """Drop the background pages but keep every annotation written on them."""
    from backend.notes import set_document
    if set_document(email, note_id, None) is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"ok": True}


@router.get("/notes-capabilities")
async def notes_capabilities():
    """What the notes UI can offer — currently just whether PDF import works.

    Lets the client disable the import button with a real explanation instead
    of letting the user discover a 501 by trying.
    """
    from backend.pdfimport import is_available
    return {"pdf_import": is_available()}


# --- Demo Endpoints ---
#
# Interactive HTML demos the tutor builds via the save_demo MCP tool. There is
# deliberately no route that serves a demo as text/html: the HTML comes back
# inside a JSON field and the client injects it into a sandboxed iframe via
# srcdoc. Serving it directly would give demo JS a same-origin URL on this app.


@router.get("/demos/{email}")
async def list_demos_route(email: str):
    """List a learner's saved demos for the picker."""
    from backend.demos import list_demos
    return {"demos": list_demos(email)}


@router.get("/demos/{email}/resolve")
async def resolve_demo_route(email: str, title: str = "", concept_id: str = ""):
    """Find the demo a chat tool-card refers to, and return it ready to render.

    Declared before /{demo_id} so "resolve" isn't captured as an id — FastAPI
    matches routes in declaration order.

    Returns the full document rather than just an id, so opening a demo from a
    message is one request instead of a resolve-then-fetch round trip.
    """
    from backend.demos import get_demo, resolve_demo

    entry = resolve_demo(email, title=title, concept_id=concept_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Demo not found")
    demo = get_demo(email, entry["id"])
    if demo is None:
        raise HTTPException(status_code=404, detail="Demo not found")
    return demo


@router.get("/demos/{email}/{demo_id}")
async def get_demo_route(email: str, demo_id: str):
    """Full demo including its HTML, for rendering into the iframe."""
    from backend.demos import get_demo

    demo = get_demo(email, demo_id)
    if demo is None:
        raise HTTPException(status_code=404, detail="Demo not found")
    return demo


@router.delete("/demos/{email}/{demo_id}")
async def delete_demo_route(email: str, demo_id: str):
    """Delete a demo."""
    from backend.demos import delete_demo

    if not delete_demo(email, demo_id):
        raise HTTPException(status_code=404, detail="Demo not found")
    return {"ok": True}


class EditDemoRequest(BaseModel):
    """A change asked for by talking to the demo directly, not via the tutor."""
    request: str


@router.post("/demos/{email}/{demo_id}/edit")
async def edit_demo_route(email: str, demo_id: str, body: EditDemoRequest):
    """Ask the builder to change this demo. Returns a job immediately.

    Same pipeline as a tutor-delegated change: both resume the demo's own
    builder session, so an edit is incremental either way.
    """
    from backend.demojobs import start_demo_job
    from backend.demos import get_demo

    demo = get_demo(email, demo_id)
    if demo is None:
        raise HTTPException(status_code=404, detail="Demo not found")

    request = (body.request or "").strip()
    if not request:
        raise HTTPException(status_code=400, detail="request is empty")

    return start_demo_job(
        email=email,
        chat_session_id=demo.get("chat_session_id") or None,
        title=demo.get("title", ""),
        concept_id=demo.get("concept_id", ""),
        request=request,
        base_demo_id=demo_id,
    )


class PinDemoRequest(BaseModel):
    demo_id: str


@router.post("/sessions/{email}/{session_id}/pin-demo")
async def pin_demo_route(email: str, session_id: str, body: PinDemoRequest):
    """Leave a visible bookmark card for a demo in the transcript.

    This is for the LEARNER, not the model. Turns written here go to
    conversation.jsonl, which the model never reads — its context comes from
    the SDK session alone. To put a demo in front of the tutor, stage it with
    the "Ask tutor" button, which sends a digest in the prompt (see
    run_agent's demo_ref).

    Non-terminal, unlike the side-chat fold: the demo stays open and keeps its
    builder session.
    """
    from backend.demos import get_demo_meta
    from backend.memory import append_turn_to_session

    # Metadata only — get_demo would read the whole html off disk just to
    # throw it away.
    demo = get_demo_meta(email, body.demo_id)
    if demo is None:
        raise HTTPException(status_code=404, detail="Demo not found")

    turn = {
        "type": "demo_pin",
        "demo_id": demo["id"],
        "title": demo.get("title", ""),
        "summary": demo.get("summary", ""),
        "version": demo.get("version", 1),
    }
    append_turn_to_session(email, session_id, turn)
    return turn


# --- Demo build jobs ---
#
# A demo is built by a background agent, so the chat turn that asked for it
# finishes immediately. The frontend polls these to drive the chip from
# "building" to "ready" — and to rebuild that state after a page refresh,
# since a build outlives the request that started it.


@router.get("/demo-jobs/{email}")
async def list_demo_jobs_route(email: str):
    from backend.demojobs import list_jobs

    return {"jobs": list_jobs(email)}


@router.get("/demo-jobs/{email}/{job_id}")
async def get_demo_job_route(email: str, job_id: str):
    from backend.demojobs import get_job

    record = get_job(email, job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return record


@router.post("/demo-jobs/{email}/{job_id}/cancel")
async def cancel_demo_job_route(email: str, job_id: str):
    """Stop a running build.

    Cancellation is explicit only. Closing the tab deliberately does NOT
    cancel: the learner should be able to walk away and find the demo waiting.
    (The code runner does the opposite, where disconnect means Stop.)
    """
    from backend.demojobs import request_cancel

    if not request_cancel(email, job_id):
        raise HTTPException(status_code=404, detail="Job not found or already finished")
    return {"ok": True}
