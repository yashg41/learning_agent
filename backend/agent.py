"""Claude Agent SDK wrapper — wires together all three memory tiers.

Tier 1 (Working Memory): SDK context window with session resume
Tier 2 (Episodic Memory): ChromaDB conversation store (episodic.py)
Tier 3 (Semantic Memory): JSON knowledge graph (knowledge.py)
"""

import logging
import os
import re

from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage

from backend.config import settings
from backend.episodic import EpisodicMemory
from backend.knowledge import KnowledgeStore
from backend.memory import (
    MemoryStore,
    get_active_session,
    save_user_session,
    get_session_dir,
    _safe_email,
)
from backend.prompts import build_system_prompt
from backend.replay import (
    session_exists_in_claude,
    session_exists_in_memory,
    replay_from_memory,
    check_and_save_compact,
)
from backend.tools import create_learning_tools

logger = logging.getLogger(__name__)

# MCP tool names (must match mcp__<server-name>__<tool-name> convention)
MCP_TOOL_NAMES = [
    "mcp__learning-tools__search_past_conversations",
    "mcp__learning-tools__save_conversation_summary",
    "mcp__learning-tools__save_image_memory",
    "mcp__learning-tools__get_learning_progress",
    "mcp__learning-tools__update_concept",
    "mcp__learning-tools__suggest_next_topics",
    "mcp__learning-tools__get_quiz_topics",
    "mcp__learning-tools__record_quiz_result",
    "mcp__learning-tools__update_learner_profile",
]

# Shared episodic memory instance (initialized once)
_episodic: EpisodicMemory | None = None


def _get_episodic() -> EpisodicMemory:
    """Get or create the shared EpisodicMemory instance."""
    global _episodic
    if _episodic is None:
        _episodic = EpisodicMemory(settings.CHROMA_DIR)
    return _episodic


def _get_user_data_dir(email: str) -> str:
    """Get the data directory for a user, creating it if needed."""
    user_dir = os.path.join(settings.DATA_DIR, "users", _safe_email(email))
    os.makedirs(user_dir, exist_ok=True)
    return user_dir


async def run_agent(
    prompt: str,
    email: str,
    on_event=None,
    session_id: str | None = None,
) -> str | None:
    """Run the learning agent for a user.

    - Loads knowledge state (Tier 3) and recent episodes (Tier 2)
    - Injects both into the system prompt
    - Creates MCP tools for the agent to read/write memory
    - Resumes or creates an SDK session (Tier 1)
    - Streams events back via on_event callback

    Args:
        prompt: User message
        email: User's email
        on_event: Callback for SSE events
        session_id: Optional session to resume. If None, uses active session or creates new.

    Returns the session_id.
    """
    user_data_dir = _get_user_data_dir(email)
    episodic = _get_episodic()

    # Load Tier 3: Semantic memory → inject into system prompt
    knowledge_store = KnowledgeStore(user_data_dir)
    knowledge_state = knowledge_store.format_for_system_prompt()

    # Load Tier 2: Recent episodic memory → inject into system prompt
    recent_episodes = episodic.format_for_system_prompt(email, n=5)

    # Build system prompt with injected memory state
    system_prompt = build_system_prompt(knowledge_state, recent_episodes)

    # Look up existing session (Tier 1)
    if not session_id:
        session_id = get_active_session(email)

    if session_id:
        logger.info(f"Returning user {email} — session {session_id}")
        # Auto-replay if SDK session data is missing but local memory exists
        if not session_exists_in_claude(session_id) and session_exists_in_memory(email, session_id):
            logger.info(f"Session {session_id} not in .claude/ — restoring from memory...")
            if on_event:
                await on_event({"type": "assistant_message", "content": "Restoring your session from memory..."})
            session_id = await replay_from_memory(email, session_id, system_prompt=system_prompt)
            save_user_session(email, session_id)
            logger.info(f"Restore complete. New session: {session_id}")
    else:
        logger.info(f"New user {email} — creating fresh session")

    # Run the SDK query
    result_session_id = await run_agent_internal(
        prompt=prompt,
        session_id=session_id,
        system_prompt=system_prompt,
        user_data_dir=user_data_dir,
        on_event=on_event,
        email=email,
    )

    # Save session mapping
    if result_session_id:
        save_user_session(email, result_session_id)
        check_and_save_compact(email, result_session_id)

    return result_session_id


async def _prompt_as_stream(prompt: str):
    """Wrap a string prompt into an AsyncIterable for the SDK.

    Required when mcp_servers or can_use_tool is set — the SDK raises
    ValueError for plain strings in these modes.
    """
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
    }


async def run_agent_internal(
    prompt: str,
    session_id: str | None = None,
    system_prompt: str | None = None,
    user_data_dir: str | None = None,
    on_event=None,
    email: str | None = None,
) -> str | None:
    """Internal SDK call — runs query() and streams events.

    Used by both run_agent() and replay_from_memory().
    """
    episodic = _get_episodic()

    # Create MCP tools
    if user_data_dir:
        mcp_server = create_learning_tools(user_data_dir, episodic)
        options = ClaudeAgentOptions(
            mcp_servers={"learning-tools": mcp_server},
            allowed_tools=MCP_TOOL_NAMES,
            permission_mode="bypassPermissions",
            model=settings.MODEL_NAME,
            env={"ANTHROPIC_API_KEY": settings.ANTHROPIC_API_KEY},
        )
    else:
        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model=settings.MODEL_NAME,
            env={"ANTHROPIC_API_KEY": settings.ANTHROPIC_API_KEY},
        )

    if system_prompt:
        options.system_prompt = system_prompt
    if session_id:
        options.resume = session_id

    captured_session_id = None
    memory: MemoryStore | None = None

    # SDK requires AsyncIterable prompt when mcp_servers is set
    prompt_input = _prompt_as_stream(prompt) if options.mcp_servers else prompt

    async for message in query(
        prompt=prompt_input,
        options=options,
    ):
        # Capture session ID from the init event
        if hasattr(message, "subtype") and message.subtype == "init":
            captured_session_id = getattr(message, "session_id", None)
            if not captured_session_id and hasattr(message, "data"):
                captured_session_id = message.data.get("session_id")
            logger.info(f"Session ID: {captured_session_id}")

            # Initialize conversation memory (writes to session dir)
            if captured_session_id and email:
                memory = MemoryStore(
                    email, captured_session_id,
                    model=settings.MODEL_NAME,
                    system_prompt=system_prompt,
                )
                memory.record_user_message(prompt)

            if on_event:
                await on_event({
                    "type": "session_init",
                    "session_id": captured_session_id,
                })

        # Stream assistant messages (text and tool calls)
        elif isinstance(message, AssistantMessage):
            if hasattr(message, "content"):
                for block in message.content:
                    if hasattr(block, "text") and block.text:
                        event_dict = {"type": "assistant_message", "content": block.text}
                        if on_event:
                            await on_event(event_dict)
                        if memory:
                            memory.record_event(event_dict)
                    elif hasattr(block, "name"):
                        tool_input = getattr(block, "input", {})
                        event_dict = {
                            "type": "tool_call",
                            "tool_name": block.name,
                            "tool_input": tool_input,
                            "tool_use_id": getattr(block, "id", ""),
                        }
                        if on_event:
                            await on_event(event_dict)
                        if memory:
                            memory.record_event(event_dict)

        # Final result
        elif isinstance(message, ResultMessage):
            result_text = getattr(message, "result", "")
            event_dict = {
                "type": "result",
                "subtype": getattr(message, "subtype", ""),
                "content": result_text,
            }
            if on_event:
                await on_event(event_dict)
            if memory:
                memory.record_event(event_dict)

        # Tool results
        elif hasattr(message, "type") and message.type == "tool_result":
            event_dict = {
                "type": "tool_result",
                "tool_use_id": getattr(message, "tool_use_id", ""),
                "content": str(getattr(message, "content", "")),
            }
            if on_event:
                await on_event(event_dict)
            if memory:
                memory.record_event(event_dict)

    return captured_session_id
