"""Claude Agent SDK wrapper — wires together all three memory tiers.

Tier 1 (Working Memory): SDK context window with session resume
Tier 2 (Episodic Memory): ChromaDB conversation store (episodic.py)
Tier 3 (Semantic Memory): JSON knowledge graph (knowledge.py)
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from typing import Callable

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
)
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
    build_system_prompt,
    demo_builder_prompt,
    demo_part_builder_prompt,
    derivation_builder_prompt,
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


# How many of the CLI's most recent stderr lines to keep per run. The CLI is
# chatty and its stderr is not ours to parse, so this is a ring buffer read only
# when something has already gone wrong.
_STDERR_KEEP_LINES = 40


def _make_stderr_sink(tag: str) -> tuple[Callable[[str], None], deque]:
    """A callback the SDK pipes the CLI's stderr into, plus the buffer.

    Without this the SDK leaves stderr attached to the server's own, where it
    lands in the console unprefixed and uncorrelated with any build. That is
    why two demo builds died reporting only "Stream closed": the CLI's own
    account of why its MCP channel closed was never captured. Worse, the SDK
    substitutes the literal string "Check stderr output for details" as the
    error text, so the one place that would have said what happened says
    nothing.

    Kept as a bounded deque rather than logged line-by-line: a healthy run
    produces plenty of stderr that is of no interest, and only a failing run
    needs the tail. See _log_stderr_tail.
    """
    buf: deque = deque(maxlen=_STDERR_KEEP_LINES)

    def sink(line: str) -> None:
        text = (line or "").rstrip()
        if text:
            buf.append(text)

    return sink, buf


def _log_stderr_tail(buf: deque, tag: str) -> None:
    """Emit whatever the CLI said before it died. No-op when it said nothing."""
    if not buf:
        return
    logger.error(
        "CLI stderr for %s (last %d lines):\n%s", tag, len(buf), "\n".join(buf)
    )

# Tool results are persisted to the transcript and replayed on every session
# load, so a verbose one (or a whole demo payload echoed back) would bloat the
# file permanently. Enough to carry an error message and a small JSON result.
#
# NOTE this is a TRANSCRIPT cap, applied where the event is recorded — the full
# result has already crossed the transport by then. It protects replay cost, not
# the channel. MAX_TOOL_PAYLOAD_BYTES below is the one that guards the wire.
TOOL_RESULT_MAX_CHARS = 2000


# The largest single value allowed to cross a tool boundary, in either
# direction. A payload past roughly this size has repeatedly killed the CLI's
# MCP channel mid-session: the call returns "Stream closed" and so does every
# later MCP call, including ones with two-byte arguments, while the CLI's own
# built-in tools keep working. The agent finishes its work and cannot save it.
#
# Chosen conservatively rather than measured: a 14,264-byte Edit preceded a
# failure, a run whose largest payload was 1,050 bytes was clean, and six of the
# seven Edit calls in the failing build were already under 2KB. So this bounds
# the outlier without touching normal work. The PostToolUse logger below is what
# will let us tune it on evidence instead of guesswork.
MAX_TOOL_PAYLOAD_BYTES = 8_000

# Fields naming a file rather than carrying its contents. Exempt because the
# whole point of the rule is to move content by path.
_PATH_FIELDS = ("file_path", "path", "notebook_path", "html_path", "shell_path",
                "image_path", "blocks_path", "source_path")


def _oversized_field(tool_input: dict) -> tuple[str, int] | None:
    """The first argument carrying more than MAX_TOOL_PAYLOAD_BYTES, if any."""
    for key, value in (tool_input or {}).items():
        if key in _PATH_FIELDS or key.endswith("_path"):
            continue
        if isinstance(value, str):
            size = len(value.encode("utf-8"))
        elif isinstance(value, (list, dict)):
            # blocks=[...], questions=[...] and friends carry their content
            # nested, so measure what would actually be serialised.
            try:
                size = len(json.dumps(value).encode("utf-8"))
            except (TypeError, ValueError):
                continue
        else:
            continue
        if size > MAX_TOOL_PAYLOAD_BYTES:
            return key, size
    return None


# What to do instead, per tool. Generic advice would leave the model guessing.
_PAYLOAD_ADVICE = {
    "Edit": (
        "Split it into several smaller Edit calls — one region at a time — "
        "rather than replacing a large block in one call."
    ),
    "Write": (
        "Build the file in pieces: Write a small version, then extend it with "
        "Edit calls."
    ),
    "mcp__learning-tools__save_demo": (
        "Write the document to your scratch directory and pass html_path "
        "instead of html."
    ),
    "mcp__learning-tools__update_demo": (
        "Pass html_path — the file you patched — instead of html."
    ),
    "mcp__learning-tools__demo_open": (
        "Keep the shell small: shared CSS and helpers only, no panel markup."
    ),
    "mcp__learning-tools__demo_write_part": (
        "A part is one panel. If it is this large, the panel is doing too much "
        "— split it, or move shared styles into the shell."
    ),
    "mcp__learning-tools__derivation_write": (
        "Write the wave to a JSON file in your scratch directory and pass "
        "blocks_path instead of blocks."
    ),
}


def check_payload_size(tool_name: str, tool_input: dict) -> str | None:
    """The refusal message for an oversized tool argument, or None if it fits.

    Called by the MCP tools themselves rather than from a hook. A PreToolUse
    hook was the obvious home for this and does not work: the CLI's own trace
    shows a hook is a control request on the same pipe the failure has already
    closed —

        createCanUseTool -> await this.sendRequest({subtype:"can_use_tool"…})
        async sendRequest(…) { if (this.inputClosed) throw Error("Stream closed") }

    — so it cannot run when it is most needed, and costs a round trip per tool
    call when it is not. Enforcing inside the handler runs in this process, on
    arguments that have already arrived, and cannot itself fail.

    The limit therefore only reaches OUR tools. The CLI's built-in Read and Edit
    move as much content as anything we define and no in-process check can see
    them; the skill and the builder prompt carry that half.
    """
    hit = _oversized_field(tool_input)
    if hit is None:
        return None

    field, size = hit
    advice = _PAYLOAD_ADVICE.get(
        tool_name, "Write the content to a file and pass its path instead."
    )
    logger.warning(
        "Oversized %s: %r is %d bytes (limit %d)",
        tool_name, field, size, MAX_TOOL_PAYLOAD_BYTES,
    )
    return (
        f"'{field}' is {size:,} bytes, over the {MAX_TOOL_PAYLOAD_BYTES:,}-byte "
        f"limit for a single tool argument. A payload this size has killed the "
        f"tool connection mid-build, losing the work. {advice}"
    )


# The tutor's tool names, per family (must match the mcp__<server>__<tool>
# convention). Mirrors the families in tools.py, but lists only what the TUTOR
# may call: the builder-only writers are deliberately absent, which is what
# stops the tutor emitting 30KB of HTML or assembling a derivation inline and
# stalling the turn.
_TUTOR_TOOLS_BY_FAMILY = {
    "core": [
        "mcp__learning-tools__search_past_conversations",
        "mcp__learning-tools__save_conversation_summary",
        "mcp__learning-tools__save_image_memory",
        # Lets the tutor see what it has already recorded. Without it the model
        # cannot tell a covered topic from an uncovered one, and summaries end
        # up far behind the teaching they are meant to record.
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
    ],
    "code": [
        # Runs Python in the learner's own venv. Unlike every other tool here
        # this one executes model-authored code as a subprocess — acceptable
        # only because the notebook already gives the same venv the same reach,
        # and this app is single-user by design (see sandbox.py's security
        # note). A tutor with no Python to teach must not be handed this.
        "mcp__learning-tools__run_code",
        "mcp__learning-tools__get_code_result",
    ],
    "demos": [
        # The tutor requests a demo; it does not write one. save_demo and
        # update_demo belong to the builder (DEMO_BUILDER_TOOL_NAMES).
        "mcp__learning-tools__request_demo",
        # Answers "is it done yet?". Without it the tutor polled search_demos,
        # which cannot tell a failed build from a slow one.
        "mcp__learning-tools__get_demo_status",
        "mcp__learning-tools__search_demos",
        # Reads slices of a demo's source when the learner asks about one.
        # Capped and query-required — the tutor never gets the whole document.
        "mcp__learning-tools__get_demo_source",
    ],
    "derivations": [
        # Same split as demos: the tutor asks, the builder writes.
        "mcp__learning-tools__request_derivation",
    ],
    # The correctness layer. All six are tutor-facing — unlike demos and
    # derivations there is no builder here, because checking a form is fast
    # and synchronous and belongs inside the turn that makes the claim.
    "sanskrit": [
        "mcp__learning-tools__derive_form",
        "mcp__learning-tools__decline",
        "mcp__learning-tools__identify_metre",
        "mcp__learning-tools__transliterate_sanskrit",
        "mcp__learning-tools__analyse_sanskrit",
        "mcp__learning-tools__lookup_word",
    ],
    # Turning the pages of a book the learner has open. Also all tutor-facing:
    # the position is a single write and the pane follows it, so there is
    # nothing here to hand to a builder.
    "reader": [
        "mcp__learning-tools__go_to_page",
        "mcp__learning-tools__advance_pages",
        "mcp__learning-tools__open_book",
        "mcp__learning-tools__list_books",
    ],
}

# Built-ins, not MCP: the tutor's only route to anything newer than the model's
# cutoff. Every app gets them. Listed for accuracy even though the allowlist is
# advisory under bypassPermissions — what actually admits them is their absence
# from DENIED_TOOL_NAMES.
_BUILTIN_TOOL_NAMES = ["WebSearch", "WebFetch"]


def tutor_tool_names(app: str = "pymentor") -> list[str]:
    """The tutor's allowlist for one app.

    Derived from the same family selection tools.py uses to build the server,
    so the two cannot drift into the state where the tutor is told about a tool
    that was never registered.
    """
    from backend.tools import app_tool_families

    names = [
        name
        for family in app_tool_families(app)
        for name in _TUTOR_TOOLS_BY_FAMILY.get(family, [])
    ]
    return names + _BUILTIN_TOOL_NAMES


# Back-compat for callers that predate the per-app split.
MCP_TOOL_NAMES = tutor_tool_names("pymentor")
# Note: skills do NOT need a "Skill" entry here. Verified against the bundled
# CLI — a skill loads and runs with an MCP-only allowlist and no permission
# denials. Skill bodies are injected as context, not invoked as a tool.

# The builder is a single-purpose agent: read the demo it is editing, write it
# back, and nothing else. Narrower than MCP_TOOL_NAMES so a build cannot wander
# into quiz or profile tools. Advisory under bypassPermissions (see below), so
# the builder prompt says the same thing in words.
DEMO_BUILDER_TOOL_NAMES = [
    "mcp__learning-tools__save_demo",
    # Checking its own work is part of the job: a demo that throws on the first
    # click is indistinguishable from one that works, because the frame is
    # sandboxed and nothing surfaces. demo_verify reads the saved demo from
    # disk, so it costs nothing to call and is available to every app.
    "mcp__learning-tools__demo_verify",
    # The split path, for a demo too large to emit in one reply. One reply
    # cannot exceed the model's output limit, and going over discards the whole
    # reply rather than truncating it — so a many-panel demo has to be written
    # as a shell plus one part per panel.
    "mcp__learning-tools__demo_open",
    "mcp__learning-tools__demo_write_part",
    "mcp__learning-tools__demo_finalize",
    "mcp__learning-tools__update_demo",
    "mcp__learning-tools__get_demo_html",
    # Finds the lines to change without pulling the document in. The pair that
    # makes a cheap edit possible: this locates the site, Edit changes it.
    "mcp__learning-tools__get_demo_source",
]


def demo_builder_tool_names(app: str = "pymentor") -> list[str]:
    """The demo builder's allowlist for one app.

    run_code is added only where it is registered. It rides in the "code"
    family, which PyMentor has and Sanskrit does not — a Sanskrit tutor has no
    Python to teach — so naming it unconditionally would advertise a tool that
    does not exist there. Derived from the same family selection tools.py uses
    to build the server, so the two cannot drift.
    """
    from backend.tools import app_tool_families

    names = list(DEMO_BUILDER_TOOL_NAMES)
    if "code" in app_tool_families(app):
        # Same reason the derivation builder has it: an open-ended way to check
        # its own work before the learner sees it.
        names.append("mcp__learning-tools__run_code")
    return names

# What a part-builder may touch: one panel of an already-open demo, nothing
# else. Narrower than DEMO_BUILDER_TOOL_NAMES on purpose — a part-builder must
# not open a second demo, finalize one before its siblings have landed, or
# rewrite the shell its siblings are sharing.
DEMO_PART_BUILDER_TOOL_NAMES = [
    "mcp__learning-tools__demo_write_part",
]

# The derivation builder writes blocks and verifies them. run_code is included
# because a derivation may need a plot, and verification is the whole point of
# the surface — but nothing else, so a build cannot wander into quiz or profile
# tools. There is no derivation_open: the job runner creates the doc and names
# it in the prompt, then reads the result off disk (mathjobs._run_build).
DERIVATION_BUILDER_TOOL_NAMES = [
    "mcp__learning-tools__derivation_write",
    # Correcting a block without appending a copy of it: check the doc out to
    # scratch, patch it with Edit, publish the file. The demo builder's
    # get_demo_html / update_demo pair, for derivations.
    "mcp__learning-tools__get_derivation_json",
    "mcp__learning-tools__derivation_update",
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

# The builders' deny list — the demo builder's and the derivation builder's,
# which both draft into a scratch directory. Every justification above is
# written about the
# TUTOR — "a tutoring agent has no business running commands or touching the
# filesystem" — and the builder is a different job: it drafts a document, checks
# it, and iterates. Doing that with no scratch space means holding the whole
# demo in one reply, which is the failure this exists to end.
#
# So it gets the file tools and keeps every dangerous one. Bash stays denied
# above all: the request text comes from a learner, permission_mode is
# bypassPermissions, and a shell would turn "build me a demo" into arbitrary
# code execution as the server's user. Task stays denied because a builder that
# spawns its own agents bypasses the fan-out the job runner controls.
#
# Edit is here for a specific reason: without it the only write verb is Write,
# which replaces a whole file. Adding a reset button to a demo changed 41 lines
# — about 2KB — but cost a 24KB Write and a 26KB Read back, because there was no
# way to patch. It also made Glob and Grep close to useless: the builder could
# find the line to change and then had no way to change it. Edit is strictly
# weaker than Write — it cannot create a file or reach a path that does not
# already exist — so granting it adds no reach.
#
# Note honestly what this is NOT: Write with an absolute path can still reach
# outside the scratch directory. cwd plus the prompt make scratch the default,
# not a jail — denying Bash is what actually bounds the blast radius.
BUILDER_DENIED_TOOL_NAMES = [
    name
    for name in DENIED_TOOL_NAMES
    if name not in ("Write", "Read", "Edit", "Glob", "Grep", "TodoWrite")
]
# The name every existing caller and test uses.
DEMO_BUILDER_DENIED_TOOL_NAMES = BUILDER_DENIED_TOOL_NAMES

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


# How many dropped writes to re-apply per turn. A handful covers a batch the
# transport ate; more than that means the channel is broken badly enough that
# replaying is not the answer.
_MAX_REPLAYED_WRITES = 10

# Only writes that are safe to apply twice. update_concept is an upsert keyed on
# concept_id, so re-applying it is a no-op when it already landed. Deliberately
# NOT here: request_demo (would start another build), run_code (would re-run
# it), record_quiz_result (appends history). Idempotence is the criterion, not
# convenience.
_REPLAYABLE_TOOLS = {"update_concept"}


def replay_dropped_writes(email: str, session_id: str, app: str = "pymentor") -> int:
    """Re-apply knowledge writes whose tool call never reached this process.

    "Stream closed" means the CLI could not deliver the call, so the handler
    never ran and the write is simply gone: a concept stayed at `introduced`
    while the tutor's reply described it as practiced. Nothing server-side can
    retry it, because nothing server-side ever saw it.

    What survives is the transcript — it records the failed call *with its
    arguments*. Eight such calls were recoverable from a single session file,
    which is what makes this worth doing rather than asking the tutor to notice
    and re-issue.

    Best-effort throughout: this runs after the learner's turn is already
    finished, so a failure here must never surface to them.
    """
    from backend.transcripts import _claude_path

    try:
        path = _claude_path(session_id)
        if not os.path.isfile(path):
            return 0

        calls: dict[str, tuple[str, dict]] = {}
        dropped: list[tuple[str, dict]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = (entry.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        calls[block.get("id", "")] = (
                            str(block.get("name", "")).split("__")[-1],
                            block.get("input") or {},
                        )
                    elif block.get("type") == "tool_result" and block.get("is_error"):
                        text = block.get("content")
                        if isinstance(text, list):
                            text = "".join(
                                b.get("text", "") for b in text if isinstance(b, dict)
                            )
                        if "stream closed" not in str(text).lower():
                            continue
                        name, args = calls.get(block.get("tool_use_id", ""), ("", {}))
                        if name in _REPLAYABLE_TOOLS and args:
                            dropped.append((name, args))
    except OSError as e:
        logger.warning(f"Could not scan {session_id} for dropped writes: {e}")
        return 0

    if not dropped:
        return 0

    # The same concept can be dropped several times in one turn — the model
    # retries against a dead channel. Keep the last write per concept.
    latest: dict[str, dict] = {}
    for _, args in dropped:
        cid = str(args.get("concept_id") or "")
        if cid:
            latest[cid] = args

    store = KnowledgeStore(_get_user_data_dir(email), app=app)
    applied = 0
    for cid, args in list(latest.items())[:_MAX_REPLAYED_WRITES]:
        try:
            store.upsert_concept(
                concept_id=cid,
                name=args.get("name", cid),
                category=args.get("category", ""),
                mastery=args.get("mastery", "introduced"),
                prerequisites=args.get("prerequisites"),
                related=args.get("related"),
                notes=args.get("notes", ""),
            )
            applied += 1
        except Exception as e:
            logger.warning(f"Could not replay update_concept({cid}): {e}")

    if applied:
        logger.info(
            f"Replayed {applied} knowledge write(s) the transport dropped "
            f"in {session_id}"
        )
    return applied


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
    book_ref: dict | None = None,
    app: str = "pymentor",
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
        book_ref: {"book_id": str} when the learner is reading a book. The id
            only — the server owns the reading position, so a page named by the
            client could disagree with the record the moment the tutor turned
            one. A digest is prepended on every turn; the page images ride
            along only when the position moved (see books.needs_resend).

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
    knowledge_store = KnowledgeStore(user_data_dir, app=app)
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
        app=app,
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

    # A book the learner is reading with the tutor. Two mechanisms, and they
    # are deliberately different:
    #
    #   - the DIGEST is text, ~60 tokens, and goes in on EVERY turn. It is what
    #     keeps the model oriented on the turns where no image is sent.
    #   - the IMAGES are ~1,400 tokens EACH and go in only when the position
    #     changed since the last turn of this session.
    #
    # Re-sending the images every turn would cost tens of thousands of tokens
    # over a conversation about one page spread — and they land in the SDK
    # transcript, so they would be replayed on every later turn as well. Same
    # reasoning that keeps demo html out of the prompt (see demo_digest).
    #
    # Placed here, AFTER the session resolution above, because needs_resend
    # keys on the session id: a new session has an empty transcript, so an
    # unchanged position must still re-send. Keyed on the unresolved argument
    # this would compare against None and silently never re-send.
    book_images: list[dict] = []
    if book_ref and book_ref.get("book_id"):
        from backend.books import (
            book_digest,
            needs_resend,
            page_images_for_model,
        )

        book_id = book_ref["book_id"]
        digest = book_digest(email, book_id)
        if digest:
            if needs_resend(email, book_id, session_id):
                # Rendering is CPU-bound and this event loop is streaming SSE.
                book_images = await asyncio.to_thread(
                    page_images_for_model, email, book_id
                )

            if book_images:
                frame = (
                    f"[The learner is reading with you. {digest}\n\n"
                    "The page images for this position are attached to THIS "
                    "message, above — read them directly. Do not look for a "
                    "file on disk and do not ask them to paste the text.\n\n"
                    "To move, call go_to_page or advance_pages; the reader "
                    "pane follows you. Do not ask them to turn the page.]\n\n"
                )
                # NB: mark_sent happens after the run, against the session id
                # the SDK actually returns — see below.
            else:
                frame = (
                    f"[The learner is reading with you. {digest}\n\n"
                    "You were shown these page images earlier in this "
                    "conversation — they are still the current position. Work "
                    "from what you already read; do not claim you cannot see "
                    "the page, and do not ask them to re-send it.\n\n"
                    "To move, call go_to_page or advance_pages.]\n\n"
                )
            prompt = frame + prompt
        else:
            logger.warning(f"book_ref {book_id} not found for {email}")

    # Run the SDK query.
    #
    # Wrapped so the dropped-write replay below runs whether this returns or
    # raises. It used to sit after the call as a plain statement, which meant
    # the recovery was skipped in exactly the case it exists for: a turn that
    # dies mid-flight is the one most likely to have lost a write to a closed
    # transport. Recovered by hand four times before this was noticed.
    try:
        result_session_id = await run_agent_internal(
            prompt=prompt,
            session_id=session_id,
            system_prompt=system_prompt,
            user_data_dir=user_data_dir,
            on_event=on_event,
            email=email,
            fork_session=do_fork,
            # Book pages are merged in HERE, not into `attachments` above: that
            # list is what saved_attachments was built from, so folding them in
            # earlier would write a duplicate PNG into data/uploads/ and record it
            # on the transcript on every single page turn.
            #
            # They are also outside MAX_ATTACHMENTS_PER_TURN by construction, and
            # that is deliberate: the cap is validated in the route against what a
            # CLIENT posted, to stop 40MB of base64 arriving over the wire. These
            # are server-generated and bounded by MAX_WINDOW_PAGES_TO_MODEL.
            attachments=(attachments or []) + book_images,
            saved_attachments=saved_attachments,
            session_ctx=session_ctx,
            # Without this the tools are built for the default app, so the
            # Sanskrit tutor was handed PyMentor's tracks and get_domain_map
            # answered "Unknown track 'sanskrit'" for its own curriculum.
            app=app,
        )

        # Record which session has now seen this book position, against the id
        # the SDK actually returned.
        #
        # NOT the session_id computed before the run: on the first turn of a
        # session that is None, and a placeholder or a restore rewrites it too.
        # The images would then be marked as sent under None, and the NEXT new
        # session — also None at this point — would look like the same one and
        # skip them. The model would then discuss, fluently and with total
        # confidence, a page it had never been shown. Only the returned id is
        # stable enough to key on.
        if book_images and result_session_id and book_ref:
            from backend.books import mark_sent

            mark_sent(email, book_ref["book_id"], result_session_id)

        # Save session mapping
        if result_session_id:
            if placeholder_session_id:
                # Replace the placeholder entry with the real SDK session ID
                replace_session_id(email, placeholder_session_id, result_session_id)
                logger.info(f"Replaced placeholder {placeholder_session_id} → {result_session_id}")
            save_user_session(email, result_session_id, set_active=set_active)
            check_and_save_compact(email, result_session_id)
            # Mirror the CLI's transcript into the project so data/ stays
            # portable. Best-effort by design — see transcripts.archive_session.
            archive_session(email, result_session_id)

        return result_session_id
    finally:
        # Re-apply knowledge writes the transport dropped — on EVERY path.
        #
        # "Stream closed" means the call never reached this process, so the
        # model did the thinking and only the saving was lost; the transcript
        # still records the call with its arguments. This used to sit after the
        # bookkeeping above, outside any try, so a turn that raised skipped its
        # own recovery — precisely the turn most likely to need it.
        #
        # session_ctx, not result_session_id: that name is unbound if
        # run_agent_internal raised, while session_ctx is created before the run
        # and filled with the live id during it, so it survives the exception.
        sid = session_ctx.get("session_id")
        if sid:
            try:
                replay_dropped_writes(email, sid, app=app)
            except Exception as e:
                # Recovery is best-effort and must never replace the original
                # failure with its own — the caller needs to see what actually
                # went wrong in the turn.
                logger.warning(f"Dropped-write replay failed for {sid}: {e}")


async def run_demo_builder(
    email: str,
    request: str,
    chat_session_id: str | None = None,
    resume_builder_session: str | None = None,
    on_event=None,
    build_ctx: dict | None = None,
    app: str = "pymentor",
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
    # Somewhere to draft and check before publishing. Reuses the code sandbox's
    # scratch root, which already sits outside the project tree (so a file
    # written here can never trip uvicorn's reloader) and is already swept by
    # TTL. Named per build so two concurrent builders do not read each other's
    # drafts. Announced in the prompt rather than set as cwd — see the note on
    # cwd in run_agent_internal.
    from backend.sandbox import SCRATCH_ROOT

    scratch = SCRATCH_ROOT / "demo-builds" / f"b_{uuid.uuid4().hex[:10]}"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        # Tools need it too, not just the prompt: get_demo_html copies the
        # demo being edited into this directory and returns the path, rather
        # than returning the document itself.
        if build_ctx is not None:
            build_ctx["scratch_dir"] = str(scratch)
        request = (
            f"{request}\n\n"
            f"Scratch space for this build: {scratch}/ — a NEW, EMPTY "
            f"directory. Write drafts into it (e.g. {scratch}/demo.html) and "
            f"read them back; write nothing outside it. There is nothing in "
            f"there yet, so do not try to read it before you have written "
            f"something."
        )
    except OSError as e:
        # A missing scratch dir costs the builder its drafting space, not the
        # build — it can still write the demo directly.
        logger.warning(f"Could not create builder scratch dir: {e}")

    return await run_agent_internal(
        prompt=request,
        session_id=resume,
        fork_session=fork,
        system_prompt=demo_builder_prompt(app),
        user_data_dir=user_data_dir,
        on_event=on_event,
        email=None,
        allowed_tools=demo_builder_tool_names(app),
        # Wider than the tutor's: this agent drafts files and reads them back.
        denied_tools=DEMO_BUILDER_DENIED_TOOL_NAMES,
        app=app,
        # None so a builder cannot recursively request another demo.
        session_ctx=None,
        build_ctx=build_ctx,
    )


async def run_demo_part_builder(
    email: str,
    request: str,
    build_ctx: dict | None = None,
    app: str = "pymentor",
) -> str | None:
    """Write ONE panel of an already-open demo. Returns its session id.

    Several of these run at once, one per panel, which is what turns a demo too
    large for a single reply into many small ones. Each gets its own session
    and its own build_ctx — sharing either would have them racing on the same
    dict while they run interleaved on one event loop.

    Deliberately NOT forked from the chat session. A part-builder needs the
    shell's contract, not the conversation: the orchestrator states the class
    names, the data and the panel's job in the request, and a forked
    twenty-turn transcript per panel would multiply the input cost by the panel
    count for context none of them use.
    """
    user_data_dir = _get_user_data_dir(email)

    return await run_agent_internal(
        prompt=request,
        session_id=None,
        fork_session=False,
        system_prompt=demo_part_builder_prompt(app),
        user_data_dir=user_data_dir,
        email=None,
        allowed_tools=DEMO_PART_BUILDER_TOOL_NAMES,
        app=app,
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

    # Where the builder drafts each wave before derivation_write reads it off
    # disk — the demo builder's route, for the same reason: a wave of blocks
    # passed as a tool argument goes through the transport, a path does not.
    # Same root and naming as run_demo_builder, so it is swept by the same TTL
    # and sits outside the reloader's watch.
    from backend.sandbox import SCRATCH_ROOT

    scratch = SCRATCH_ROOT / "derivation-builds" / f"b_{uuid.uuid4().hex[:10]}"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        if build_ctx is not None:
            build_ctx["scratch_dir"] = str(scratch)
        request = (
            f"{request}\n\n"
            f"Scratch space for this build: {scratch}/ — a NEW, EMPTY "
            f"directory. Write each wave there as its own file (e.g. "
            f"{scratch}/wave1.json) and pass that path as blocks_path; write "
            f"nothing outside it."
        )
    except OSError as e:
        # Costs the builder its drafting space, not the build: small waves
        # can still go inline as blocks=[...].
        logger.warning(f"Could not create derivation scratch dir: {e}")

    return await run_agent_internal(
        prompt=request,
        session_id=resume,
        fork_session=fork,
        system_prompt=derivation_builder_prompt(),
        user_data_dir=user_data_dir,
        on_event=on_event,
        email=None,
        allowed_tools=DERIVATION_BUILDER_TOOL_NAMES,
        # Drafting waves to files needs Write/Read; Bash and Task stay denied.
        denied_tools=BUILDER_DENIED_TOOL_NAMES,
        # None so a builder cannot recursively request another derivation.
        session_ctx=None,
        build_ctx=build_ctx,
    )


# What the CLI says when a resumed session no longer fits the context window.
# It exits non-zero and the SDK re-raises the text, so this is matched on the
# message rather than on a type. Kept broad on purpose: the exact wording is
# the CLI's, not ours, and mistaking an overflow for a generic failure costs
# the learner their whole session.
_CONTEXT_OVERFLOW_MARKERS = (
    "prompt is too long",
    "context length",
    "context_length_exceeded",
    "too many tokens",
    "exceeds the maximum",
)


# The OUTPUT side, which is a different failure with a different fix. These
# arrive as assistant TEXT — "API Error: Claude's response exceeded the 32000
# output token maximum" — not as an exception and not as an error result, so
# nothing in the pipeline noticed them: the run returned a session id like any
# other, and the job could only report the generic "finished without saving a
# demo". The real cause was a demo too large to emit in one reply, and the fix
# is to split it, so the reason has to survive to the job record.
_OUTPUT_OVERFLOW_MARKERS = (
    "output token maximum",
    "exceeded the maximum output",
    "max_tokens",
    "response exceeded",
)


def _is_output_overflow(text: str) -> bool:
    """Whether this text is the API refusing a reply for being too long."""
    low = (text or "").lower()
    if "api error" not in low and "error" not in low:
        return False
    return any(marker in low for marker in _OUTPUT_OVERFLOW_MARKERS)


# Roughly where a resumed transcript stops fitting. The file is JSON, so it is
# perhaps 4 chars per token before overhead; 2M chars is around 500k tokens,
# which is already past every current model. Deliberately generous — this only
# decides whether to attempt a rescue that is harmless when unnecessary.
_OVERSIZED_TRANSCRIPT_CHARS = 2_000_000


def _is_context_overflow(error: BaseException, session_id: str | None = None) -> bool:
    """Whether this failure means the conversation outgrew the model.

    Two signals, because neither is sufficient on its own. The CLI usually
    reports the real reason ("Prompt is too long"), but it can also die with
    nothing but `exit code 1 … Check stderr output for details` — which is
    exactly what a genuinely oversized session produced. So when the message
    says nothing useful, the transcript's own size is consulted instead.
    """
    text = str(error).lower()
    if any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS):
        return True

    if not session_id:
        return False
    try:
        from backend.transcripts import _claude_path

        path = _claude_path(session_id)
        return os.path.getsize(path) > _OVERSIZED_TRANSCRIPT_CHARS
    except OSError:
        return False


def _unwrap_exception(exc: BaseException) -> BaseException:
    """Unwrap ExceptionGroup/TaskGroup to get the first real exception."""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            unwrapped = _unwrap_exception(sub)
            if unwrapped is not sub or not isinstance(sub, BaseExceptionGroup):
                return unwrapped
        return exc.exceptions[0] if exc.exceptions else exc
    return exc


def compaction_event(message) -> dict | None:
    """The UI event for a compaction signal in the CLI's stream, or None.

    The CLI compacts a long session on its own, and that takes minutes with no
    tokens coming out. The UI's only signal was the thinking indicator ticking
    upward, which reads as a hang — one such wait ran to 201s and was reported
    as a bug.

    The first fix listened for `compact_boundary` and the label still appeared
    only at the very end, a moment before the first token removed it. The
    boundary is written AFTER the summary: the CLI's order is
    status "compacting" -> summarise (the minutes) -> status null -> boundary.
    So the start is the `status` message; the boundary stays as a fallback for
    a CLI that does not send it. The UI handler is idempotent, so getting both
    costs nothing.

    Read from the message stream, NOT a PreCompact hook: every hook is a
    control request on the same pipe as the MCP calls (see the note at
    payload_hooks), so a hook cannot report trouble at exactly the moment the
    pipe is in trouble. These arrive as ordinary messages.
    """
    subtype = getattr(message, "subtype", None)
    data = getattr(message, "data", None) or {}
    if subtype == "status":
        # status null (compaction over) and permission-mode updates share this
        # subtype; only the start of compaction is ours.
        if data.get("status") == "compacting":
            return {"type": "compacting", "phase": "start",
                    "trigger": None, "pre_tokens": None}
        return None
    if subtype == "compact_boundary":
        meta = data.get("compactMetadata") or {}
        return {
            "type": "compacting",
            "phase": "end",
            # "auto" when the CLI hit the context limit by itself,
            # "manual" when something asked for it.
            "trigger": meta.get("trigger", "auto"),
            "pre_tokens": meta.get("preTokens"),
        }
    return None


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
    denied_tools: list[str] | None = None,
    session_ctx: dict | None = None,
    build_ctx: dict | None = None,
    app: str = "pymentor",
    _retrying: bool = False,
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

    app: which tutor this is. Selects both the tools that get registered and
    the allowlist the model is shown, from one family list, so the two cannot
    disagree about what exists.

    session_ctx: a caller-owned dict this run writes its session id into, as
    soon as the SDK reports it. Tools close over the same dict, which is the
    only way a tool handler can learn which chat session invoked it — the id
    does not exist yet when the options are built.
    """
    episodic = _get_episodic()

    # Capture the CLI's stderr for both branches. Setting this callback is what
    # makes the SDK pipe stderr at all; left unset it stays attached to the
    # server's own and the reason for a failed run is lost.
    stderr_sink, stderr_buf = _make_stderr_sink(session_id or "new session")

    # No hooks. Two were tried here and both removed, for the reason the CLI's
    # own stack trace spells out:
    #
    #   createCanUseTool -> await this.sendRequest({subtype:"can_use_tool"…})
    #   async sendRequest(…) { if (this.inputClosed) throw Error("Stream closed") }
    #
    # "Stream closed" is the CLI refusing to send a control request because the
    # pipe back to this process is shut. EVERY hook is another control request
    # on that same pipe, so a hook cannot run precisely when something has gone
    # wrong — and when it fails the CLI logs its own minified source as the
    # error, tens of KB of it, burying the real failure.
    #
    # The PreToolUse guard went first: it needed the channel it was guarding.
    # The PostToolUse observer went too — it earned its keep by finding a
    # 43,868-byte Edit result, but on a broken channel it turned one failure
    # into a screenful of noise, which is worse than the blind spot it filled.
    #
    # Size enforcement now lives in the tool handlers (check_payload_size),
    # which run in this process and cannot fail this way.
    payload_hooks = None

    # Choose this run's session id before anything is built from it.
    #
    # Over the HTTP transport the tools learn who called them from headers
    # fixed when the options are built — which is before the SDK's init message
    # reports the id. Left to init, a new chat sent no session at all (so
    # request_demo/request_derivation refused on its first turn), a fork sent
    # its PARENT's id (so a demo requested there would fork the wrong
    # conversation), and a builder's tools never learned its own session.
    #
    # A plain resume keeps its id, so only new and forked runs need one. The
    # CLI's own rule, checked in the bundled binary: "--session-id can only be
    # used with --continue or --resume if --fork-session is also specified" —
    # which is exactly this condition.
    #
    # A plain resume is written in too: the context-overflow retry below
    # resumes a rebuilt session under a NEW id while session_ctx still holds
    # the old one.
    preassigned_session_id = None
    if not session_id or fork_session:
        preassigned_session_id = str(uuid.uuid4())
    run_session_id = preassigned_session_id or session_id
    if session_ctx is not None:
        session_ctx["session_id"] = run_session_id
    if build_ctx is not None:
        build_ctx["builder_session_id"] = run_session_id

    # Create MCP tools
    if user_data_dir:
        # Two transports for the same 32 tools; see backend/mcp_http.py.
        #
        # An "sdk" server has no channel of its own — the CLI calls back down
        # the stdin pipe the prompt went out on, and the SDK closes that pipe
        # 60 seconds into a turn, after which every call fails with "Stream
        # closed". Over http the CLI opens its own connection and keeps it.
        if settings.MCP_TRANSPORT == "http":
            from backend.mcp_http import mcp_headers, mcp_url
            from backend.tools import _email_from_dir

            # Identity travels per request: one endpoint serves every run, and
            # a demo build overlaps a live chat turn by design.
            mcp_config = {
                "type": "http",
                "url": mcp_url(),
                "headers": mcp_headers(
                    email=_email_from_dir(user_data_dir),
                    app=app,
                    role="builder" if build_ctx is not None else "tutor",
                    session_id=(session_ctx or {}).get("session_id"),
                    build_ctx=build_ctx,
                ),
            }
        else:
            mcp_config = create_learning_tools(
                user_data_dir, episodic, session_ctx, build_ctx, app=app
            )

        options = ClaudeAgentOptions(
            mcp_servers={"learning-tools": mcp_config},
            allowed_tools=allowed_tools or tutor_tool_names(app),
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
            disallowed_tools=denied_tools or DENIED_TOOL_NAMES,
            permission_mode="bypassPermissions",
            model=settings.MODEL_NAME,
            env=_cli_env(),
            # NOT parameterised, deliberately. The CLI derives its
            # projects/<slug>/ transcript directory from cwd, and
            # replay._get_claude_project_dir recomputes that same slug from
            # PROJECT_ROOT. Pointing a builder at its scratch directory would
            # file its transcript somewhere else, so session_exists_in_claude
            # would answer False and refining a demo could never resume the
            # session that wrote it. A builder is told its scratch path in the
            # prompt instead.
            cwd=PROJECT_ROOT,
            include_partial_messages=True,
            stderr=stderr_sink,
            hooks=payload_hooks,
        )
    else:
        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model=settings.MODEL_NAME,
            env=_cli_env(),
            cwd=PROJECT_ROOT,
            include_partial_messages=True,
            stderr=stderr_sink,
            hooks=payload_hooks,
        )

    if system_prompt:
        options.system_prompt = system_prompt
    if session_id:
        options.resume = session_id
        # Only meaningful alongside resume: inherit the parent's context but
        # write to a new session id.
        options.fork_session = fork_session
    if preassigned_session_id:
        options.extra_args = {
            **(options.extra_args or {}),
            "session-id": preassigned_session_id,
        }

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

    # What this run was launched with. A builder's MCP calls have failed with
    # "Stream closed" while its Write/Read kept working, which means the CLI
    # was alive but never reached the in-process MCP server. That is invisible
    # after the fact: the transcript records the failed call but nothing about
    # the conditions. Logged once per run, at the moment it starts.
    _role = "builder" if build_ctx is not None else "tutor"
    try:
        _loop_id = id(asyncio.get_running_loop())
        _task = asyncio.current_task()
        _task_name = _task.get_name() if _task else "?"
    except RuntimeError:
        _loop_id, _task_name = 0, "?"
    logger.info(
        "Agent run [%s]: mcp_servers=%s tools=%d resume=%s fork=%s "
        "loop=%x task=%s",
        _role,
        list(getattr(options, "mcp_servers", {}) or {}),
        len(getattr(options, "allowed_tools", []) or []),
        (session_id or "-")[:8],
        fork_session,
        _loop_id,
        _task_name,
    )

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
                if (preassigned_session_id and captured_session_id
                        and captured_session_id != preassigned_session_id):
                    # The tools were told the pre-assigned id. If the CLI ever
                    # stops honouring --session-id, they are now naming a
                    # session that does not exist — make that visible.
                    logger.warning(
                        f"CLI ignored --session-id {preassigned_session_id}; "
                        f"it reported {captured_session_id}"
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

            # Compaction: say so when it STARTS — see compaction_event for why
            # compact_boundary alone arrived too late to be useful.
            elif (event := compaction_event(message)) is not None:
                if event["phase"] == "start":
                    logger.info(
                        f"Compaction started [{_role}] "
                        f"session={(captured_session_id or '-')[:8]}"
                    )
                else:
                    logger.info(
                        f"Compaction finished [{_role}] "
                        f"session={(captured_session_id or '-')[:8]} "
                        f"trigger={event['trigger']} "
                        f"pre_tokens={event['pre_tokens']}"
                    )
                if on_event:
                    await on_event(event)

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
                            # An over-long reply is reported as ordinary
                            # assistant text, so this is the only place it can
                            # be seen. Recorded rather than raised: the agent
                            # usually retries and may still succeed, and it is
                            # the job runner that needs the reason if it does
                            # not.
                            if _is_output_overflow(block.text):
                                logger.error(
                                    f"Output overflow from the model: "
                                    f"{block.text[:200]}"
                                )
                                if build_ctx is not None:
                                    build_ctx["output_overflow"] = block.text[:300]

                            event_dict = {"type": "assistant_message", "content": block.text}
                            if on_event:
                                await on_event(event_dict)
                            if memory:
                                memory.record_event(event_dict)
                        elif hasattr(block, "name"):
                            tool_input = getattr(block, "input", {})
                            # Did this run reach for a tool at all? An edit
                            # request that produced no write is ambiguous
                            # without this: a builder that looked and found
                            # nothing to do is a success, while one that never
                            # tried usually means the input was a question
                            # rather than an instruction.
                            if build_ctx is not None:
                                build_ctx["ran_tools"] = True
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
                        # A transport failure arrives HERE, as a tool result,
                        # not as an exception — so the handler at the bottom of
                        # this function never sees it. Two demo builds died
                        # this way with "Stream closed" and no reason; this is
                        # where the CLI's own account of it surfaces.
                        if "stream closed" in text.lower():
                            _log_stderr_tail(
                                stderr_buf, session_id or "new session"
                            )
                            # Record it, not just log it. "Stream closed" means
                            # the call never reached this process, so the agent
                            # did the work and only the saving failed. Without
                            # this flag the job runner cannot tell that from a
                            # builder that simply chose not to write, and both
                            # were reported as "no change was made".
                            if build_ctx is not None:
                                build_ctx["transport_failed"] = True

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
        # The exception text is often useless here — the SDK substitutes
        # "Check stderr output for details" when the CLI exits non-zero. This
        # is that detail.
        _log_stderr_tail(stderr_buf, session_id or "new session")

        # A session can outgrow the context window. The CLI compacts on its
        # own, but a long enough conversation reaches the limit on the very
        # turn that would have triggered it, and then every later turn fails
        # the same way — the session is permanently unusable, with the whole
        # history still sitting on disk and no way for the learner to get at
        # it. That is what "Prompt is too long" means here.
        #
        # replay_from_memory already knows how to rebuild a session from the
        # saved summary plus the turns after it, which is exactly the smaller
        # context this needs. It was only wired to the case where the SDK
        # transcript had gone missing; an oversized one needs the same rescue.
        if (not _retrying and email and session_id
                and _is_context_overflow(real_error, session_id)):
            logger.warning(
                f"Session {session_id} is over the context limit — "
                "rebuilding it from memory and retrying once."
            )
            if on_event:
                await on_event({
                    "type": "assistant_message",
                    "content": "That session got long — condensing it and carrying on…",
                })
            try:
                fresh = await replay_from_memory(
                    email, session_id, system_prompt=system_prompt
                )
            except Exception as replay_error:
                logger.error(f"Could not rebuild {session_id}: {replay_error}")
                raise RuntimeError(
                    "This conversation has grown past what the model can hold, "
                    "and it could not be condensed automatically. Start a new "
                    "session — everything you have covered is still in memory "
                    "and the tutor will pick it up."
                ) from real_error

            save_user_session(email, fresh)
            return await run_agent_internal(
                prompt=prompt,
                session_id=fresh,
                system_prompt=system_prompt,
                user_data_dir=user_data_dir,
                on_event=on_event,
                email=email,
                attachments=attachments,
                saved_attachments=saved_attachments,
                session_ctx=session_ctx,
                build_ctx=build_ctx,
                allowed_tools=allowed_tools,
                app=app,
                # One rescue only. If the rebuilt session is still too large
                # the problem is not staleness, and retrying would loop.
                _retrying=True,
            )

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
