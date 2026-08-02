"""API endpoints for the Python Learning Agent."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
import asyncio
import json
import logging
import os
import numpy as np

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# How long the SSE generator waits for an event before emitting a heartbeat.
KEEPALIVE_SECONDS = 15


class ChatRequest(BaseModel):
    message: str
    email: str
    session_id: str | None = None
    # Branch off another session, inheriting its context. Set by side-chats
    # and edit-branches on their first turn only.
    fork_from: str | None = None
    # Side-chats must not steal the user's active-session pointer.
    set_active: bool = True


# --- Chat Endpoints ---


@router.post("/chat/stream")
async def chat_stream(body: ChatRequest):
    """Send a message and stream back events as SSE."""
    event_queue: asyncio.Queue = asyncio.Queue()

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
            )
        except Exception as e:
            await event_queue.put({"type": "error", "content": str(e)})
        finally:
            await event_queue.put({"type": "done"})

    asyncio.create_task(run_and_signal_done())

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


class ResetVenvRequest(BaseModel):
    email: str


@router.post("/code/run")
async def code_run(body: RunCodeRequest):
    """Run a code cell in the user's per-user venv. Returns stdout/stderr."""
    from backend.sandbox import run_code, DEFAULT_TIMEOUT_SEC

    timeout = body.timeout if (body.timeout and 0 < body.timeout <= 60) else DEFAULT_TIMEOUT_SEC
    try:
        return await run_code(body.email, body.code, timeout=timeout)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"runner failed: {e}")


@router.post("/code/reset_venv")
async def code_reset_venv(body: ResetVenvRequest):
    """Wipe and recreate the user's venv. Useful when an install gets corrupted."""
    from backend.sandbox import reset_venv

    try:
        await reset_venv(body.email)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"reset failed: {e}")


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
        KnowledgeStore, CURRICULUM_GRAPH, CATEGORY_COLORS, TRACK_COLORS, category_label,
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

    return {
        "nodes": nodes,
        "edges": edges,
        "categories": [
            {"id": cat, "name": category_label(cat), "color": color}
            for cat, color in CATEGORY_COLORS.items()
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
    """Create a new empty session for a user."""
    import uuid
    from backend.memory import save_user_session

    session_id = str(uuid.uuid4())
    name = body.name or "New Session"
    save_user_session(email, session_id, name=name)
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
