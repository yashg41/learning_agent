"""Claude Agent SDK wrapper — wires together all three memory tiers.

Tier 1 (Working Memory): SDK context window with session resume
Tier 2 (Episodic Memory): ChromaDB conversation store (episodic.py)
Tier 3 (Semantic Memory): JSON knowledge graph (knowledge.py)
"""

import logging
import os
import re
import time

from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage
# StreamEvent carries partial-message deltas. It is part of the SDK's Message
# union but is NOT re-exported from the top-level package, so import it from
# .types directly.
from claude_agent_sdk.types import StreamEvent

from backend.config import settings
from backend.episodic import EpisodicMemory
from backend.knowledge import KnowledgeStore
from backend.memory import (
    MemoryStore,
    get_active_session,
    save_user_session,
    replace_session_id,
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

# Project root — pinned so the bundled CLI always derives the same
# ~/.claude/projects/<slug>/ regardless of where run.py was launched from.
PROJECT_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(__file__)))

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
    "mcp__learning-tools__generate_quiz_question",
    "mcp__learning-tools__get_feedback",
    "mcp__learning-tools__save_demo",
]
# Note: skills do NOT need a "Skill" entry here. Verified against the bundled
# CLI — a skill loads and runs with an MCP-only allowlist and no permission
# denials. Skill bodies are injected as context, not invoked as a tool.

# Built-in tools the tutor must never reach. This is the real restriction:
# `permission_mode="bypassPermissions"` skips the check that consults
# allowed_tools, so the allowlist above is advisory in practice, while
# disallowed_tools is enforced in every permission mode.
#
# Without this the tutor happily shells out — it was seen writing a demo to
# /tmp and leaving `python3 -m http.server 8000` running, which exposed that
# directory on the network. A tutoring agent has no business running commands
# or touching files; everything it legitimately needs is an MCP tool.
DENIED_TOOL_NAMES = [
    "Bash",
    "BashOutput",
    "KillShell",
    "Write",
    "Edit",
    "NotebookEdit",
    "Read",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
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
    fork_from: str | None = None,
    set_active: bool = True,
    attachments: list[dict] | None = None,
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
        fork_from: Branch off this session — the new session inherits its full
            context but writes to its own transcript, leaving the parent
            untouched. Used by side-chats and edit-branches. Applies only on
            the first turn; afterwards the child resumes normally.
        set_active: Whether this run should become the user's active session.
            Side-chats pass False so opening one doesn't hijack the main chat.
        attachments: Images for THIS turn, as [{data, content_type, filename}]
            with base64 `data`. Already validated by the route.

    Returns the session_id.
    """
    user_data_dir = _get_user_data_dir(email)
    episodic = _get_episodic()

    # Write attachments to disk before the SDK call so the transcript can
    # record paths. The base64 goes to the model but is never persisted.
    saved_attachments = []
    for att in (attachments or []):
        try:
            saved_attachments.append(
                episodic.save_attachment(
                    email,
                    att["data"],
                    content_type=att.get("content_type", "image/png"),
                )
            )
        except Exception as e:
            # A failed write must not sink the turn — the model can still see
            # the image, it just won't survive reload.
            logger.warning(f"Could not persist attachment for {email}: {e}")

    # Load Tier 3: Semantic memory → inject into system prompt
    knowledge_store = KnowledgeStore(user_data_dir)
    knowledge_state = knowledge_store.format_for_system_prompt()

    # Load Tier 2: Recent episodic memory → inject into system prompt
    recent_episodes = episodic.format_for_system_prompt(email, n=5)

    # Build system prompt with injected memory state
    system_prompt = build_system_prompt(knowledge_state, recent_episodes)

    # Look up existing session (Tier 1). A fork explicitly opts out of the
    # active-session fallback: it must branch from the session it was given,
    # never from whatever happens to be active.
    if not session_id and not fork_from:
        session_id = get_active_session(email)

    placeholder_session_id = None  # Track pre-created sessions that need ID replacement
    do_fork = False

    if fork_from and not session_id:
        # First turn of a branch: resume the parent but tell the SDK to split
        # off a new session id, so the parent's transcript is never appended to.
        if session_exists_in_claude(fork_from):
            session_id = fork_from
            do_fork = True
            logger.info(f"Forking new session from {fork_from} for {email}")
        else:
            # Parent has no SDK transcript to fork (e.g. it was restored from
            # memory). Fall back to a fresh session rather than failing.
            logger.warning(f"Cannot fork {fork_from} — no SDK session file; starting fresh")

    if session_id and not do_fork:
        sdk_exists = session_exists_in_claude(session_id)
        mem_exists = session_exists_in_memory(email, session_id)

        if sdk_exists:
            # SDK session file exists — resume it
            logger.info(f"Resuming SDK session {session_id} for {email}")
        elif mem_exists:
            # SDK session gone but local memory exists — restore from memory
            logger.info(f"Session {session_id} not in .claude/ — restoring from memory...")
            if on_event:
                await on_event({"type": "assistant_message", "content": "Restoring your session from memory..."})
            session_id = await replay_from_memory(email, session_id, system_prompt=system_prompt)
            save_user_session(email, session_id)
            logger.info(f"Restore complete. New session: {session_id}")
        else:
            # Session exists in user.json but has no SDK or memory data — start fresh
            # (e.g., user clicked "New Session" button but hasn't sent a message yet)
            logger.info(f"Session {session_id} has no SDK or memory data — starting fresh for {email}")
            placeholder_session_id = session_id
            session_id = None
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
        fork_session=do_fork,
        attachments=attachments,
        saved_attachments=saved_attachments,
    )

    # Save session mapping
    if result_session_id:
        if placeholder_session_id:
            # Replace the placeholder session entry with the real SDK session ID
            replace_session_id(email, placeholder_session_id, result_session_id)
            logger.info(f"Replaced placeholder {placeholder_session_id} → {result_session_id}")
        save_user_session(email, result_session_id, set_active=set_active)
        check_and_save_compact(email, result_session_id)

    return result_session_id


def _unwrap_exception(exc: BaseException) -> BaseException:
    """Unwrap ExceptionGroup/TaskGroup to get the first real exception."""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            unwrapped = _unwrap_exception(sub)
            if unwrapped is not sub or not isinstance(sub, BaseExceptionGroup):
                return unwrapped
        return exc.exceptions[0] if exc.exceptions else exc
    return exc


async def _prompt_as_stream(prompt: str, attachments: list[dict] | None = None):
    """Wrap a prompt into an AsyncIterable for the SDK.

    Required when mcp_servers or can_use_tool is set — the SDK raises
    ValueError for plain strings in these modes.

    With attachments, `content` becomes a list of content blocks instead of a
    string. The SDK does not inspect `content` — stream_input json.dumps-es
    the dict straight to the CLI — so multimodal blocks pass through to the
    Messages API unchanged.
    """
    content: str | list[dict] = prompt

    if attachments:
        # Images before text: when the question refers to an image, the model
        # does better having seen it first.
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": att.get("content_type", "image/png"),
                    "data": att["data"],
                },
            }
            for att in attachments
        ]
        # An image-only turn still needs a text block — a bare image gives the
        # model nothing to act on.
        #
        # The wording matters: anything resembling "take a look at this image"
        # reads as a pointer to a file on disk, and the model goes hunting for
        # a path instead of reading the image already in its context. Say
        # explicitly that the image is attached above.
        content.append({
            "type": "text",
            "text": prompt or (
                "The learner attached this image with no accompanying text. "
                "The image is included directly in this message — do not look "
                "for a file on disk. Describe what you see and help them with it."
            ),
        })

    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


async def run_agent_internal(
    prompt: str,
    session_id: str | None = None,
    system_prompt: str | None = None,
    user_data_dir: str | None = None,
    on_event=None,
    email: str | None = None,
    fork_session: bool = False,
    attachments: list[dict] | None = None,
    saved_attachments: list[dict] | None = None,
) -> str | None:
    """Internal SDK call — runs query() and streams events.

    Used by both run_agent() and replay_from_memory().

    fork_session: when resuming, split off a new session id instead of
    continuing the resumed one. The parent's transcript stays untouched.

    attachments: base64 images for this turn, sent to the model as content
    blocks. saved_attachments: the same images' on-disk metadata, recorded to
    the transcript. Split because base64 must never be persisted.
    """
    episodic = _get_episodic()

    # Create MCP tools
    if user_data_dir:
        mcp_server = create_learning_tools(user_data_dir, episodic)
        options = ClaudeAgentOptions(
            mcp_servers={"learning-tools": mcp_server},
            allowed_tools=MCP_TOOL_NAMES,
            # Loads .claude/ from PROJECT_ROOT — specifically skills/, which is
            # how the tutor learns to build interactive demos. The SDK default
            # is None, which makes the CLI load nothing from disk, so without
            # this a skill file is silently ignored.
            #
            # Only "project": "user" would pull in ~/.claude settings belonging
            # to whoever runs the server, which has nothing to do with the
            # learner and would vary by machine.
            setting_sources=["project"],
            # allowed_tools alone does NOT restrict anything under
            # bypassPermissions — that mode skips the permission check the
            # allowlist is consulted by. Observed in practice: the tutor wrote
            # an HTML file to /tmp and started `python3 -m http.server 8000`
            # while "restricted" to MCP tools. disallowed_tools is enforced
            # regardless of permission mode, so the deny list is what actually
            # holds the line.
            disallowed_tools=DENIED_TOOL_NAMES,
            permission_mode="bypassPermissions",
            model=settings.MODEL_NAME,
            env={"ANTHROPIC_API_KEY": settings.ANTHROPIC_API_KEY},
            cwd=PROJECT_ROOT,
            include_partial_messages=True,
        )
    else:
        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model=settings.MODEL_NAME,
            env={"ANTHROPIC_API_KEY": settings.ANTHROPIC_API_KEY},
            cwd=PROJECT_ROOT,
            include_partial_messages=True,
        )

    if system_prompt:
        options.system_prompt = system_prompt
    if session_id:
        options.resume = session_id
        # Only meaningful alongside resume: inherit the parent's context but
        # write to a new session id.
        options.fork_session = fork_session

    captured_session_id = None
    memory: MemoryStore | None = None

    # SDK requires AsyncIterable prompt when mcp_servers is set. Attachments
    # force the streaming path regardless: a plain string can't carry images.
    prompt_input = (
        _prompt_as_stream(prompt, attachments)
        if (options.mcp_servers or attachments)
        else prompt
    )

    # Timing around the SDK handshake. The initialize control request has a
    # 60s ceiling inside the SDK; when it blows, the traceback says nothing
    # about how long things actually took. Log it so a recurrence of
    # "Control request timeout: initialize" has evidence attached.
    query_started = time.monotonic()
    first_delta_logged = False

    try:
        async for message in query(
            prompt=prompt_input,
            options=options,
        ):
            # Capture session ID from the init event
            if hasattr(message, "subtype") and message.subtype == "init":
                captured_session_id = getattr(message, "session_id", None)
                if not captured_session_id and hasattr(message, "data"):
                    captured_session_id = message.data.get("session_id")
                logger.info(
                    f"Session ID: {captured_session_id} "
                    f"(SDK handshake took {time.monotonic() - query_started:.2f}s)"
                )

                # Initialize conversation memory (writes to session dir)
                if captured_session_id and email:
                    memory = MemoryStore(
                        email, captured_session_id,
                        model=settings.MODEL_NAME,
                        system_prompt=system_prompt,
                    )
                    memory.record_user_message(prompt, attachments=saved_attachments)

                if on_event:
                    await on_event({
                        "type": "session_init",
                        "session_id": captured_session_id,
                    })

            # Partial text deltas — emitted as the model generates, so the UI
            # can render live instead of waiting for the whole block.
            #
            # `.event` is the raw Anthropic stream event, not pre-parsed, and
            # its shape varies (thinking_delta / input_json_delta carry no
            # "text"), so every hop is a guarded .get().
            #
            # Deliberately NOT recorded to memory: the terminal AssistantMessage
            # below persists the same text, and recording both would duplicate
            # it on replay.
            elif isinstance(message, StreamEvent):
                raw = message.event or {}
                if raw.get("type") == "content_block_delta":
                    delta_text = (raw.get("delta") or {}).get("text") or ""
                    if delta_text and on_event:
                        if not first_delta_logged:
                            first_delta_logged = True
                            logger.info(
                                f"First token at {time.monotonic() - query_started:.2f}s"
                            )
                        await on_event({
                            "type": "assistant_delta",
                            "content": delta_text,
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

    except BaseException as exc:
        # Unwrap ExceptionGroup/TaskGroup to get the real error
        real_error = _unwrap_exception(exc)
        logger.error(f"SDK query failed: {real_error}", exc_info=True)
        raise RuntimeError(str(real_error)) from real_error

    # Auto-save the latest exchange to ChromaDB for rich episodic search
    if memory and captured_session_id and email:
        try:
            _extract_and_save_exchange(memory, episodic, email, captured_session_id)
        except Exception as e:
            logger.warning(f"Auto-save exchange failed: {e}")

    return captured_session_id


def _extract_and_save_exchange(
    memory: MemoryStore,
    episodic: EpisodicMemory,
    email: str,
    session_id: str,
):
    """Extract the latest user+assistant exchange from memory and save to ChromaDB.

    Called automatically after each agent response completes.
    Walks backwards through turns to find the most recent user message
    and collects the corresponding assistant response + tool calls.
    """
    turns = memory.turns
    if not turns:
        return

    # Walk backwards to find the most recent user message
    user_msg_idx = None
    for i in range(len(turns) - 1, -1, -1):
        if turns[i].get("type") == "user":
            user_msg_idx = i
            break

    if user_msg_idx is None:
        return

    user_message = turns[user_msg_idx].get("content", "")

    # Collect all turns after this user message (assistant responses + tool calls)
    assistant_parts = []
    tool_calls_collected = []

    for turn in turns[user_msg_idx + 1:]:
        t = turn.get("type", "")
        if t == "user":
            break  # Next user message, stop
        elif t == "assistant_message":
            content = turn.get("content", "")
            if content:
                assistant_parts.append(content)
        elif t == "tool_call":
            tool_calls_collected.append({
                "tool_name": turn.get("tool_name", ""),
                "tool_input": turn.get("tool_input", {}),
            })
        elif t == "tool_result":
            if tool_calls_collected:
                tool_calls_collected[-1]["result"] = turn.get("content", "")[:500]

    assistant_response = "\n".join(assistant_parts)

    # Skip exchanges with very short assistant responses (trivial auto-acks)
    if len(assistant_response.strip()) < 50:
        return

    # Count exchanges in this session to determine exchange_index
    exchange_count = sum(1 for t in turns[:user_msg_idx + 1] if t.get("type") == "user")

    # Extract topics from any update_concept tool calls
    topics = []
    for tc in tool_calls_collected:
        if "update_concept" in tc.get("tool_name", ""):
            concept = tc.get("tool_input", {}).get("concept_id", "")
            if concept:
                topics.append(concept)

    episodic.save_exchange(
        email=email,
        session_id=session_id,
        exchange_index=exchange_count,
        user_message=user_message,
        assistant_response=assistant_response,
        tool_calls=tool_calls_collected if tool_calls_collected else None,
        topics=topics if topics else None,
    )
