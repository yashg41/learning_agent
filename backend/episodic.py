"""Tier 2: Episodic Memory — ChromaDB-backed conversation store.

Stores conversation summaries as vector embeddings for semantic search.
Uses ChromaDB's default all-MiniLM-L6-v2 embeddings (runs locally, no API calls).

Image handling: Images are saved to data/uploads/ as files. Only the AI-generated
caption and file path are stored in ChromaDB — never the base64 data.
"""

import base64
import logging
import os
import uuid
from datetime import datetime, timezone

import chromadb

from backend.config import settings

logger = logging.getLogger(__name__)


class EpisodicMemory:
    """Persistent vector store for past conversation summaries."""

    def __init__(self, chroma_dir: str | None = None):
        path = chroma_dir or settings.CHROMA_DIR
        self._client = chromadb.PersistentClient(path=path)
        self._conversations = self._client.get_or_create_collection(
            name="conversations",
            metadata={"hnsw:space": "cosine"},
        )

    def save_episode(
        self,
        email: str,
        summary: str,
        topics: list[str] | None = None,
        session_id: str | None = None,
    ) -> str:
        """Store a conversation summary with metadata. Returns the episode ID."""
        now = datetime.now(timezone.utc)
        episode_id = f"ep_{email}_{now.strftime('%Y%m%d_%H%M%S_%f')}"

        metadata = {
            "user_email": email,
            "timestamp": now.isoformat(),
            "topics": ",".join(topics) if topics else "",
        }
        if session_id:
            metadata["session_id"] = session_id

        self._conversations.upsert(
            ids=[episode_id],
            documents=[summary],
            metadatas=[metadata],
        )

        logger.info(f"Saved episode {episode_id} for {email}: {summary[:80]}...")
        return episode_id

    def search_episodes(
        self,
        email: str,
        query: str,
        n_results: int = 5,
    ) -> list[dict]:
        """Semantic search over past conversation summaries for a user.

        Returns list of {summary, topics, timestamp, similarity_score}.
        """
        count = self._conversations.count()
        if count == 0:
            return []

        # ChromaDB requires n_results <= total docs matching the filter.
        # Query with a safe limit.
        try:
            results = self._conversations.query(
                query_texts=[query],
                n_results=min(n_results, count),
                where={"user_email": email},
            )
        except Exception as e:
            # If no documents match the where filter, ChromaDB may raise
            logger.warning(f"Episode search failed for {email}: {e}")
            return []

        episodes = []
        if results["documents"] and results["documents"][0]:
            for i, doc in enumerate(results["documents"][0]):
                meta = results["metadatas"][0][i]
                distance = results["distances"][0][i]
                episodes.append({
                    "summary": doc,
                    "topics": meta.get("topics", "").split(",") if meta.get("topics") else [],
                    "timestamp": meta.get("timestamp", ""),
                    "session_id": meta.get("session_id", ""),
                    "similarity_score": round(1 - distance, 3),
                })

        return episodes

    def get_recent_episodes(self, email: str, n: int = 10) -> list[dict]:
        """Get the most recent N conversation summaries for a user.

        Returns list of {summary, topics, timestamp} sorted by timestamp descending.
        """
        count = self._conversations.count()
        if count == 0:
            return []

        try:
            # Get all episodes for this user (ChromaDB doesn't support ORDER BY)
            results = self._conversations.get(
                where={"user_email": email},
                include=["documents", "metadatas"],
            )
        except Exception as e:
            logger.warning(f"Failed to get recent episodes for {email}: {e}")
            return []

        if not results["documents"]:
            return []

        episodes = []
        for i, doc in enumerate(results["documents"]):
            meta = results["metadatas"][i]
            episodes.append({
                "summary": doc,
                "topics": meta.get("topics", "").split(",") if meta.get("topics") else [],
                "timestamp": meta.get("timestamp", ""),
                "session_id": meta.get("session_id", ""),
            })

        # Sort by timestamp descending, take top N
        episodes.sort(key=lambda x: x["timestamp"], reverse=True)
        return episodes[:n]

    # =================================================================
    # Image Handling
    # =================================================================

    def save_image(
        self,
        email: str,
        image_data: str,
        caption: str,
        filename: str | None = None,
        content_type: str = "image/png",
    ) -> dict:
        """Save an uploaded image to disk and store its caption in ChromaDB.

        Instead of storing the base64 data in the vector DB (which wastes space
        and slows searches), we:
        1. Save the actual image file to data/uploads/<email>/
        2. Generate a text caption (done by the caller/LLM)
        3. Store only the caption + file path in ChromaDB

        Args:
            email: User's email
            image_data: Base64-encoded image data
            caption: AI-generated description of the image
            filename: Optional filename (auto-generated if not provided)
            content_type: MIME type (default: image/png)

        Returns:
            dict with {file_path, caption, episode_id}
        """
        # Determine file extension from content type
        ext_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }
        ext = ext_map.get(content_type, ".png")

        # Create uploads directory for this user
        uploads_dir = os.path.join(settings.DATA_DIR, "uploads", email)
        os.makedirs(uploads_dir, exist_ok=True)

        # Generate filename if not provided
        if not filename:
            now = datetime.now(timezone.utc)
            filename = f"img_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"

        file_path = os.path.join(uploads_dir, filename)

        # Decode and save the image file
        try:
            image_bytes = base64.b64decode(image_data)
            with open(file_path, "wb") as f:
                f.write(image_bytes)
        except Exception as e:
            logger.error(f"Failed to save image for {email}: {e}")
            raise

        # Store the caption (NOT the base64) in ChromaDB
        summary = f"User uploaded an image (Path: {file_path}). Agent noted: \"{caption}\""
        episode_id = self.save_episode(
            email=email,
            summary=summary,
            topics=["image_upload"],
        )

        logger.info(f"Image saved: {file_path} ({len(image_bytes)} bytes) → caption stored as {episode_id}")

        return {
            "file_path": file_path,
            "caption": caption,
            "episode_id": episode_id,
        }

    def format_for_system_prompt(self, email: str, n: int = 5) -> str:
        """Format recent episodes as a condensed string for system prompt injection."""
        episodes = self.get_recent_episodes(email, n=n)
        if not episodes:
            return "No past conversations recorded yet."

        lines = ["== RECENT CONVERSATION HISTORY =="]
        for ep in episodes:
            ts = ep["timestamp"][:10] if ep["timestamp"] else "unknown"
            topics_str = ", ".join(ep["topics"]) if ep["topics"] else "general"
            lines.append(f"- [{ts}] ({topics_str}): {ep['summary']}")

        return "\n".join(lines)
