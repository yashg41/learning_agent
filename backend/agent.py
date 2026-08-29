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
# .types directly. UserMessage/ToolResultBlock are how tool results actually
# arrive — see the UserMessage branch in run_agent_internal.
from claude_agent_sdk.types import StreamEvent, ToolResultBlock, UserMessage

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
from backend.demos import demo_digest, format_session_demos
from backend.prompts import (
    DEMO_BUILDER_PROMPT,
    DERIVATION_BUILDER_PROMPT,
    build_system_prompt,
)
from backend.replay import (
    session_exists_in_claude,
    session_exists_in_memory,
    replay_from_memory,
    check_and_save_compact,
)
from backend.tools import create_learning_tools
from backend.transcripts import archive_session

logger = logging.getLogger(__name__)

# Project root — pinned so the bundled CLI always derives the same
# projects/<slug>/ regardless of where run.py was launched from.
PROJECT_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(__file__)))

# Root of the bundled CLI's own state — session transcripts, todos, debug logs.
#
# This is ~/.claude and CANNOT currently be moved into the project. Setting the
# CLI's CLAUDE_CONFIG_DIR does relocate the transcripts, but it also makes the
# CLI treat the install as brand new and unauthenticated: subscription
# credentials live in the macOS Keychain, and the CLI stops finding them.
# Verified directly — same binary, same keychain, the env var alone is the
# difference:
#
#   claude -p "say ok"                        -> ok
#   CLAUDE_CONFIG_DIR=... claude -p "say ok"  -> Not logged in · Please run /login
#
# Copying ~/.claude.json (oauthAccount, userID) into the new dir does not fix
# it. Revisit if the app moves to a real ANTHROPIC_API_KEY, where keychain
# lookup is not involved — the env var may well work there.
#
# Consequence to keep in mind: app state is split. The UI renders from
# data/users/<email>/sessions/, while `--resume` reads the CLI's copy here.
# Deleting one leaves the chat looking intact while the model has lost every
# prior turn. replay.py's _get_claude_project_dir must agree with this value.
CLAUDE_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".claude")


def _cli_env() -> dict:
    """Environment for the CLI subprocess.

    Shared by both option branches so the two cannot drift.
    """
    return {"ANTHROPIC_API_KEY": settings.ANTHROPIC_API_KEY}

# Tool results are persisted to the transcript and replayed on every session
# load, so a verbose one (or a whole demo payload echoed back) would bloat the
# file permanently. Enough to carry an error message and a small JSON result.
TOOL_RESULT_MAX_CHARS = 2000

# MCP tool names (must match mcp__<server-name>__<tool-name> convention)
MCP_TOOL_NAMES = [
    "mcp__learning-tools__search_past_conversations",
    "mcp__learning-tools__save_conversation_summary",
    "mcp__learning-tools__save_image_memory",
    # Lets the tutor see what it has already recorded. Without it the model
    # cannot tell a covered topic from an uncovered one, and summaries end up
    # far behind the teaching they are meant to record.
    "mcp__learning-tools__get_memory_status",
    "mcp__learning-tools__get_learning_progress",
    "mcp__learning-tools__update_concept",
    "mcp__learning-tools__suggest_next_topics",
    "mcp__learning-tools__get_domain_map",
    "mcp__learning-tools__get_quiz_topics",
    "mcp__learning-tools__record_quiz_result",
    "mcp__learning-tools__update_learner_profile",
    "mcp__learning-tools__generate_quiz_question",
    "mcp__learning-tools__get_feedback",
    # Runs Python in the learner's own venv. Unlike every other tool here this
    # one executes model-authored code as a subprocess — acceptable only
    # because the notebook already gives the same venv the same reach, and this
    # app is single-user by design (see the security note in sandbox.py).
    "mcp__learning-tools__run_code",
    "mcp__learning-tools__get_code_result",
    # The tutor requests a demo; it does not write one. save_demo/update_demo
    # belong to the builder (DEMO_BUILDER_TOOL_NAMES) — keeping them off this
    # list stops the tutor emitting 30KB of HTML inside a chat turn, which is
    # what used to block the composer for a minute.
    "mcp__learning-tools__request_demo",
    "mcp__learning-tools__search_demos",
    # Reads slices of a demo's source when the learner asks about one. Capped
    # and query-required — the tutor never gets the whole document.
    "mcp__learning-tools__get_demo_source",
    # Same split as demos: the tutor asks for a derivation, the builder writes
    # it. Keeping derivation_write off this list is what stops the tutor
    # assembling one inline and stalling the turn.
    "mcp__learning-tools__request_derivation",
    # Built-ins, not MCP: the tutor's only route to anything newer than the
    # model's cutoff. Listed here for accuracy even though the allowlist is
    # advisory under bypassPermissions — what actually admits them is their
    # absence from DENIED_TOOL_NAMES.
    "WebSearch",
    "WebFetch",
]
# Note: skills do NOT need a "Skill" entry here. Verified against the bundled
# CLI — a skill loads and runs with an MCP-only allowlist and no permission
# denials. Skill bodies are injected as context, not invoked as a tool.

# The builder is a single-purpose agent: read the demo it is editing, write it
# back, and nothing else. Narrower than MCP_TOOL_NAMES so a build cannot wander
# into quiz or profile tools. Advisory under bypassPermissions (see below), so
# the builder prompt says the same thing in words.
DEMO_BUILDER_TOOL_NAMES = [
    "mcp__learning-tools__save_demo",
    "mcp__learning-tools__update_demo",
    "mcp__learning-tools__get_demo_html",
]

# The derivation builder writes blocks and verifies them. run_code is included
# because a derivation may need a plot, and verification is the whole point of
# the surface — but nothing else, so a build cannot wander into quiz or profile
# tools.
DERIVATION_BUILDER_TOOL_NAMES = [
    "mcp__learning-tools__derivation_open",
    "mcp__learning-tools__derivation_write",
    "mcp__learning-tools__derivation_verify",
    "mcp__learning-tools__derivation_read",
    "mcp__learning-tools__derivation_plot",
    "mcp__learning-tools__run_code",
]

# Built-in tools the tutor must never reach. This is the real restriction:
# `permission_mode="bypassPermissions"` skips the check that consults
# allowed_tools, so the allowlist above is advisory in practice, while
# disallowed_tools is enforced in every permission mode.
#
# Without this the tutor happily shells out — it was seen writing a demo to
# /tmp and leaving `python3 -m http.server 8000` running, which exposed that
# directory on the network. A tutoring agent has no business running commands
# or touching the filesystem.
#
# WebSearch/WebFetch are deliberately NOT denied. The tutor teaches libraries
# that move faster than the model's cutoff, and its only other route to ground
# truth is running code — which cannot answer "what is current" for an API it
# has never seen. The cost is that fetched pages land in the transcript and are
# re-sent every later turn, so the prompt tells it to look up and summarise
# rather than quote at length.
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
    "Task",
    # The bundled CLI already refuses this one, but only after the model has
    # spent a turn calling it. The tutor reaches for it because the memory
    # prompt tells it to track progress; denying it up front keeps it out of
    # the tool list entirely, so the model records progress via the semantic
    # memory tools instead of rediscovering the refusal each curriculum.
    "TodoWrite",
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
    demo_ref: dict | None = None,
    derivation_ref: dict | None = None,
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
        demo_ref: {"demo_id": str, "selection": {...}} when the learner asked
            about a demo. A structural digest is prepended to the prompt; the
            model never sees the demo's html unless it calls get_demo_source.

    Returns the session_id.
    """
    user_data_dir = _get_user_data_dir(email)
    episodic = _get_episodic()

    # Put the referenced demo in front of the model. This goes into the PROMPT,
    # not the transcript file — conversation.jsonl is never read back into the
    # model's context, so a reference recorded there alone would be invisible.
    if demo_ref and demo_ref.get("demo_id"):
        digest = demo_digest(
            email, demo_ref["demo_id"], demo_ref.get("selection")
        )
        if digest:
            prompt = (
                "[The learner is asking about this demo, open in their Demo "
                "pane. You cannot see it running — this is its structure.\n\n"
                f"{digest}\n\n"
                "If you need the actual code to answer, call "
                "get_demo_source(demo_id, query) with a specific name. Do not "
                "paste the demo's code back at them.]\n\n"
                f"{prompt}"
            )
        else:
            logger.warning(f"demo_ref {demo_ref['demo_id']} not found for {email}")

    # Same idea for a derivation the learner is pointing at. Cheaper than the
    # demo case — a derivation is already structured, so the digest IS the
    # content rather than a summary of markup.
    if derivation_ref and derivation_ref.get("doc_id"):
        from backend.derivations import derivation_digest

        digest = derivation_digest(
            email, derivation_ref["doc_id"], derivation_ref.get("selection")
        )
        if digest:
            prompt = (
                "[The learner is asking about this derivation, open in their "
                "Maths pane. Steps marked WRONG failed a sympy check; steps "
                "marked unchecked could not be verified, usually because of "
                "ambiguous notation.\n\n"
                f"{digest}\n\n"
                "Answer their question about it. To revise the derivation "
                "itself, call request_derivation with base_doc_id.]\n\n"
                f"{prompt}"
            )
        else:
            logger.warning(
                f"derivation_ref {derivation_ref['doc_id']} not found for {email}"
            )

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

    # Look up existing session (Tier 1). A fork explicitly opts out of the
    # active-session fallback: it must branch from the session it was given,
    # never from whatever happens to be active.
    if not session_id and not fork_from:
        session_id = get_active_session(email)

    # Built here rather than earlier because the demo list is scoped to the
    # resolved session id, which the lookup above is what determines.
    system_prompt = build_system_prompt(
        knowledge_state,
        recent_episodes,
        format_session_demos(email, session_id),
    )

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

    # Tools read this to learn which conversation invoked them. request_demo
    # needs it to know which session the builder should fork; its presence is
    # also what marks this run as "the tutor" rather than "the builder".
    session_ctx: dict = {"session_id": session_id}

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
        session_ctx=session_ctx,
    )

    # Save session mapping
    if result_session_id:
        if placeholder_session_id:
            # Replace the placeholder session entry with the real SDK session ID
            replace_session_id(email, placeholder_session_id, result_session_id)
            logger.info(f"Replaced placeholder {placeholder_session_id} → {result_session_id}")
        save_user_session(email, result_session_id, set_active=set_active)
        check_and_save_compact(email, result_session_id)
        # Mirror the CLI's transcript into the project so data/ stays portable.
        # Best-effort by design — see transcripts.archive_session.
        archive_session(email, result_session_id)

    return result_session_id


async def run_demo_builder(
    email: str,
    request: str,
    chat_session_id: str | None = None,
    resume_builder_session: str | None = None,
    on_event=None,
    build_ctx: dict | None = None,
) -> str | None:
    """Build (or refine) a demo in a session of its own. Returns its session id.

    This is a second agent, not the tutor. It exists because writing 5-30KB of
    HTML takes ~60s, and doing that inside the chat turn froze the composer for
    the whole minute.

    Context comes from *forking* the chat session rather than from a prose
    brief: a summary of a twenty-turn conversation produces a generic demo,
    whereas a fork inherits the actual discussion. The fork writes to a new
    session id, so the tutor's transcript is never appended to — which is also
    what makes running this concurrently with a live chat turn safe.

    On a refinement, we resume the builder's own previous session instead,
    which still holds both that conversation and the HTML it wrote. That makes
    "add a reset button" an incremental edit rather than a full regeneration.

    email is deliberately NOT passed to run_agent_internal: that is what gates
    MemoryStore construction and _extract_and_save_exchange, so the builder
    gets tools without writing into the learner's conversation transcript or
    polluting episodic memory with half-written HTML.
    """
    from backend.replay import session_exists_in_claude

    user_data_dir = _get_user_data_dir(email)

    if resume_builder_session and session_exists_in_claude(resume_builder_session):
        resume, fork = resume_builder_session, False
    elif chat_session_id and session_exists_in_claude(chat_session_id):
        resume, fork = chat_session_id, True
    else:
        # No usable context — the builder still works, it just has only the
        # request text to go on.
        resume, fork = None, False
        logger.info("Demo builder starting with no session context")

    # The builder's own session id is only known once the SDK reports init,
    # which is before any tool runs — so routing it through build_ctx lets
    # save_demo/update_demo stamp it onto the demo as they write it.
    return await run_agent_internal(
        prompt=request,
        session_id=resume,
        fork_session=fork,
        system_prompt=DEMO_BUILDER_PROMPT,
        user_data_dir=user_data_dir,
        on_event=on_event,
        email=None,
        allowed_tools=DEMO_BUILDER_TOOL_NAMES,
        # None so a builder cannot recursively request another demo.
        session_ctx=None,
        build_ctx=build_ctx,
    )


async def run_derivation_builder(
    email: str,
    request: str,
    chat_session_id: str | None = None,
    resume_builder_session: str | None = None,
    on_event=None,
    build_ctx: dict | None = None,
) -> str | None:
    """Build (or revise) a derivation in a session of its own. Returns its id.

    Same shape as run_demo_builder and for the same reasons: a derivation with
    per-step verification takes long enough that doing it inside the chat turn
    would freeze the composer.

    Context comes from *forking* the chat session rather than a prose brief — a
    summary of twenty turns produces a generic derivation, whereas a fork
    inherits what the learner actually got stuck on. The fork writes to a new
    session id, so the tutor's transcript is never appended to.

    email is deliberately NOT passed to run_agent_internal: that is what gates
    MemoryStore construction and _extract_and_save_exchange, so the builder gets
    tools without writing half-finished derivations into episodic memory.
    """
    from backend.replay import session_exists_in_claude

    user_data_dir = _get_user_data_dir(email)

    if resume_builder_session and session_exists_in_claude(resume_builder_session):
        resume, fork = resume_builder_session, False
    elif chat_session_id and session_exists_in_claude(chat_session_id):
        resume, fork = chat_session_id, True
    else:
        resume, fork = None, False
        logger.info("Derivation builder starting with no session context")

    return await run_agent_internal(
        prompt=request,
        session_id=resume,
        fork_session=fork,
        system_prompt=DERIVATION_BUILDER_PROMPT,
        user_data_dir=user_data_dir,
        on_event=on_event,
        email=None,
        allowed_tools=DERIVATION_BUILDER_TOOL_NAMES,
        # None so a builder cannot recursively request another derivation.
        session_ctx=None,
        build_ctx=build_ctx,
    )


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
    allowed_tools: list[str] | None = None,
    session_ctx: dict | None = None,
    build_ctx: dict | None = None,
) -> str | None:
    """Internal SDK call — runs query() and streams events.

    Used by both run_agent() and replay_from_memory().

    fork_session: when resuming, split off a new session id instead of
    continuing the resumed one. The parent's transcript stays untouched.

    attachments: base64 images for this turn, sent to the model as content
    blocks. saved_attachments: the same images' on-disk metadata, recorded to
    the transcript. Split because base64 must never be persisted.

    allowed_tools: narrow the MCP allowlist for this run. The demo builder
    uses it so a build cannot wander into quiz or profile tools.

    session_ctx: a caller-owned dict this run writes its session id into, as
    soon as the SDK reports it. Tools close over the same dict, which is the
    only way a tool handler can learn which chat session invoked it — the id
    does not exist yet when the options are built.
    """
    episodic = _get_episodic()

    # Create MCP tools
    if user_data_dir:
        mcp_server = create_learning_tools(
            user_data_dir, episodic, session_ctx, build_ctx
        )
        options = ClaudeAgentOptions(
            mcp_servers={"learning-tools": mcp_server},
            allowed_tools=allowed_tools or MCP_TOOL_NAMES,
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
            env=_cli_env(),
            cwd=PROJECT_ROOT,
            include_partial_messages=True,
        )
    else:
        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model=settings.MODEL_NAME,
            env=_cli_env(),
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

                # Tools close over these dicts, so writing them here is what
                # lets a tool handler know which session called it. build_ctx
                # must be set before any tool runs, which init precedes.
                if session_ctx is not None and captured_session_id:
                    session_ctx["session_id"] = captured_session_id
                if build_ctx is not None and captured_session_id:
                    build_ctx["builder_session_id"] = captured_session_id

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

            # Tool results.
            #
            # These arrive as ToolResultBlocks inside a UserMessage — NOT as a
            # top-level message with .type == "tool_result". UserMessage is a
            # dataclass with fields (content, uuid, parent_tool_use_id,
            # tool_use_result) and no `.type` at all, so the previous
            # hasattr(message, "type") check never matched and every tool
            # result was silently dropped.
            #
            # That blindness is why three save_demo calls failing with
            # "Stream closed" produced no log line, no SSE event and no
            # transcript record — the UI showed a successful-looking demo chip
            # for a demo that was never written.
            elif isinstance(message, UserMessage):
                blocks = message.content if isinstance(message.content, list) else []
                for block in blocks:
                    if not isinstance(block, ToolResultBlock):
                        continue

                    raw = block.content
                    if isinstance(raw, list):
                        # Content blocks: [{"type": "text", "text": ...}, ...]
                        text = "".join(
                            b.get("text", "") for b in raw if isinstance(b, dict)
                        )
                    else:
                        text = str(raw or "")

                    # is_error is set by the CLI for transport failures (e.g.
                    # "Stream closed") and by our own _error_result helper in
                    # tools.py. It is NOT set when a tool handler raises and
                    # the SDK converts the exception to text, so a bare
                    # bool(block.is_error) under-reports. Treat our own error
                    # prefixes as failures too, so the UI and the log agree.
                    is_error = bool(block.is_error) or text.startswith(
                        ("Demo rejected:", "Error ")
                    )
                    if is_error:
                        logger.error(
                            f"Tool result error (tool_use_id={block.tool_use_id}): "
                            f"{text[:500]}"
                        )

                    event_dict = {
                        "type": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "is_error": is_error,
                        # Capped because this now lands in conversation.jsonl:
                        # a tool like save_demo can return a large payload, and
                        # the transcript is replayed on every session load.
                        "content": text[:TOOL_RESULT_MAX_CHARS],
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
