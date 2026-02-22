"""API endpoints for the Python Learning Agent."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio
import json
import os
import numpy as np

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    message: str
    email: str
    session_id: str | None = None


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
            )
        except Exception as e:
            await event_queue.put({"type": "error", "content": str(e)})
        finally:
            await event_queue.put({"type": "done"})

    asyncio.create_task(run_and_signal_done())

    async def event_generator():
        while True:
            event = await event_queue.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "done":
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
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
    from backend.knowledge import KnowledgeStore, CURRICULUM_GRAPH

    user_dir = _get_user_data_dir(email)
    store = KnowledgeStore(user_dir)
    data = store.load_knowledge()
    user_concepts = data.get("concepts", {})

    # Category colors for grouping
    category_colors = {
        "fundamentals": "#7aa2f7",
        "control_flow": "#e0af68",
        "data_structures": "#9ece6a",
        "functions": "#bb9af7",
        "oop": "#f7768e",
        "error_handling": "#ff9e64",
        "file_io": "#73daca",
        "modules": "#2ac3de",
        "advanced": "#c0caf5",
    }

    nodes = []
    edges = []

    for concept_id, info in CURRICULUM_GRAPH.items():
        user_data = user_concepts.get(concept_id, {})
        mastery = user_data.get("mastery", "not_started")
        nodes.append({
            "id": concept_id,
            "name": info["name"],
            "category": info["category"],
            "color": category_colors.get(info["category"], "#565f89"),
            "mastery": mastery,
            "review_count": user_data.get("review_count", 0),
            "last_reviewed": user_data.get("last_reviewed", ""),
        })
        for prereq in info["prereqs"]:
            edges.append({"source": prereq, "target": concept_id})

    return {
        "nodes": nodes,
        "edges": edges,
        "categories": [
            {"id": cat, "name": cat.replace("_", " ").title(), "color": color}
            for cat, color in category_colors.items()
        ],
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
    col = episodic._conversations
    count = col.count()
    metadata = col.metadata or {}

    return {
        "collection_name": col.name,
        "total_documents": count,
        "distance_metric": metadata.get("hnsw:space", "unknown"),
        "embedding_model": "all-MiniLM-L6-v2",
        "embedding_dimensions": 384,
    }


@router.get("/chroma/documents")
async def chroma_documents(email: str | None = None, limit: int = 100):
    """Get all documents from ChromaDB with their embeddings projected to 2D."""
    from backend.agent import _get_episodic

    episodic = _get_episodic()
    col = episodic._conversations
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
        docs.append({
            "id": results["ids"][i],
            "document": results["documents"][i] or "",
            "email": meta.get("user_email", ""),
            "timestamp": meta.get("timestamp", ""),
            "topics": [t for t in topics if t],
            "session_id": meta.get("session_id", ""),
        })
        if results["embeddings"] is not None and len(results["embeddings"]) > i:
            emb = results["embeddings"][i]
            if emb is not None and len(emb) > 0:
                embeddings.append(list(emb) if not isinstance(emb, list) else emb)

    # Project to 2D
    points_2d = _pca_2d(embeddings) if embeddings else []

    return {"documents": docs, "points_2d": points_2d, "count": len(docs)}


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
