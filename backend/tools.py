"""MCP tool definitions for the Python Learning Agent.

Tools across memory, curriculum, quiz, feedback, code execution and demos.
The demo tools are documented at their definitions below; the rest:

Episodic (ChromaDB):
  - search_past_conversations: Semantic search over past exchanges + summaries
  - save_conversation_summary: Store a conversation summary with metadata

Semantic (JSON knowledge graph):
  - get_learning_progress: Read full knowledge state
  - update_concept: Add/update concept mastery
  - suggest_next_topics: Prerequisite-aware topic suggestions
  - get_quiz_topics: Get best concepts to quiz on
  - record_quiz_result: Save quiz results + promote mastery
  - update_learner_profile: Update learner level/preferences/notes

Quiz + feedback:
  - generate_quiz_question: Validate + shuffle options server-side, return
    to agent so chat-text A/B/C/D positions are uniformly randomized.
  - get_feedback: Read aspect-scoped guidance (global + user-specific)
    before performing quiz / suggest / explain actions.

Code execution:
  - run_code: Run Python in the learner's venv and return its real output, so
    the tutor verifies a snippet instead of predicting what it prints. Slow
    work (installs, long fits) goes to a background job instead of holding
    the chat turn — see codejobs.py.
  - get_code_result: Collect a background run's output on a later turn.
"""

import ast
import json
import logging
import os
import random
import re
from datetime import datetime, timezone

from claude_agent_sdk import tool, create_sdk_mcp_server

from backend.episodic import EpisodicMemory
from backend.feedback import ASPECTS as FEEDBACK_ASPECTS, load_feedback
from backend.knowledge import CURRICULUM_GRAPH, KnowledgeStore, knowledge_lock

logger = logging.getLogger(__name__)

# Ceiling on one get_demo_source result. Everything a tool returns stays in the
# SDK transcript and is re-sent on every later turn of the session, so this is
# a permanent per-call cost, not a one-off. ~4000 chars ≈ 1000 tokens.
MAX_DEMO_SOURCE_CHARS = 4000
DEMO_SOURCE_CONTEXT_LINES = 12

# Same reasoning for run_code: stdout lands in the transcript permanently. The
# sandbox's own 50KB cap is sized for a human reading a notebook; this one is
# sized for something re-sent on every subsequent turn.
MAX_RUN_OUTPUT_CHARS = 3000

# Mirror of agent.TOOL_RESULT_MAX_CHARS, the ceiling that module applies when
# writing a tool result into the transcript. Duplicated rather than imported:
# agent.py imports this module, so importing back would be a cycle. Keep the
# two in step — run_code sizes its output against this so that verified_id
# always survives the truncation (it did not, and the model retyped code
# instead of citing it).
TRANSCRIPT_RESULT_BUDGET = 2000

# Shorter than the notebook's 15s. The learner is watching a reply compose, and
# a verification snippet that needs longer than this is the wrong snippet.
RUN_CODE_TIMEOUT_SEC = 10

# Above this fraction of English (string literals + comments), a cell is a
# lecture wearing a program's clothes. Set from real data rather than taste:
# in one session the legitimate calls measured 11-34% and the abusive ones
# 71-99%, so 0.6 sits in an empty gap. See prose_ratio().
MAX_PROSE_RATIO = 0.6

# A Pāṇinian derivation runs to ~20 steps, most of which restate the same
# intermediate form while metadata rules fire. The whole chain would be re-sent
# on every later turn for no teaching value, so derive_form keeps only the steps
# that actually changed the form, capped here.
MAX_SUTRA_STEPS = 12

# Which tool families each app gets.
#
# One backend serves more than one tutor, and they do not want the same tools:
# a Sanskrit tutor has no use for run_code, and PyMentor has no use for a
# sandhi splitter. Families rather than individual names, because the families
# are the real units — the demo pair (request + search + read source) is
# meaningless if you take only one of them, and a tutor with record_quiz_result
# but not generate_quiz_question cannot run the quiz protocol at all.
#
# `core` is the tutoring machinery every app needs: memory, the knowledge
# graph, quizzes, feedback. It is not listed per-app because omitting it never
# makes sense — a tutor that cannot record what it taught is not a tutor.
APP_TOOL_FAMILIES = {
    "pymentor": ("code", "demos", "derivations"),
    # Sanskrit gets its own families in Phase 2 ("sanskrit"). Demos stay:
    # a declension table or a sandhi animation is exactly the demo surface.
    # Code goes: there is no Python to run, and leaving run_code in would
    # invite the tutor to "verify" morphology by writing a script instead of
    # calling the analyser that actually knows.
    # "reader" is Sanskrit-only for now: it exists for the learner who wants to
    # read an actual edition of an actual text, which is this tutor's whole
    # goal. PyMentor teaches from runnable code, not from scans.
    "sanskrit": ("demos", "sanskrit", "reader"),
}

DEFAULT_APP = "pymentor"


def app_tool_families(app: str) -> tuple[str, ...]:
    """Families for one app, always including core. Fails loudly on a typo."""
    if app not in APP_TOOL_FAMILIES:
        raise ValueError(
            f"Unknown app '{app}'. Known apps: {sorted(APP_TOOL_FAMILIES)}."
        )
    return ("core",) + APP_TOOL_FAMILIES[app]


def create_learning_tools(
    user_data_dir: str,
    episodic: EpisodicMemory,
    session_ctx: dict | None = None,
    build_ctx: dict | None = None,
    app: str = DEFAULT_APP,
    _return_tools: bool = False,
):
    """Create in-process MCP tools for the learning agent.

    Args:
        user_data_dir: Path to the user's data directory (data/users/<email>/)
        episodic: Shared EpisodicMemory instance
        app: Which tutor this is. Selects the tool families (see
            APP_TOOL_FAMILIES) so a Sanskrit tutor is not handed run_code.
            Every tool is still *defined* below whatever the app — only the
            list handed to the MCP server is filtered, because the definitions
            are closures over this factory's arguments and splitting them
            across modules would mean threading five parameters through each.
        session_ctx: Caller-owned dict that run_agent_internal fills with the
            live session id. request_demo needs it to know which conversation
            to fork, and the id does not exist when this factory is called.
            None for the demo builder, which disables request_demo — a builder
            must not be able to request another build.
        build_ctx: Caller-owned dict for a builder run (demo or derivation):
            carries the ids to stamp onto what is saved, and receives back the
            demo_id / doc_id that was written so the job runner can tell whether
            the build actually produced anything. Its presence is also the real
            boundary that keeps save_demo and derivation_write away from the
            tutor — allowed_tools is advisory under bypassPermissions.

    Returns:
        McpSdkServerConfig for use in ClaudeAgentOptions.mcp_servers
    """
    # Imported here, not at module scope: agent imports tools.
    from backend.agent import check_payload_size

    knowledge = KnowledgeStore(user_data_dir, app=app)

    # Filled in at the bottom of this factory, once create_sdk_mcp_server has
    # produced the instance whose id identifies it. A one-element list, not a
    # plain name, because the handlers below are closures defined BEFORE the
    # server exists — they capture this cell and read it at call time.
    #
    # This is the missing half of the diagnostic in lessons.md §1d: the id was
    # logged at build time only, so a failed call could never be placed. With
    # an entry line naming the same id, the log distinguishes three cases that
    # all present as "Stream closed":
    #   no entry line          -> the call never arrived; the pipe was shut
    #   entry, no completion   -> it arrived and our handler died
    #   entry, id != build id  -> it reached a STALE server
    # Only the first is a transport fault, and until now nothing could tell it
    # from the others except inferring from a suspiciously short duration.
    mcp_instance_id = [0]

    def _entered(tool_name: str):
        """Record that a tool call actually reached this process."""
        logger.info(
            "Tool entered [%s]: %s instance=%x",
            app, tool_name, mcp_instance_id[0],
        )

    # =====================================================================
    # TIER 2: Episodic Memory Tools (ChromaDB)
    # =====================================================================

    @tool(
        "search_past_conversations",
        "Search past conversations by semantic similarity. "
        "Returns DETAILED content from past sessions including specific code examples, "
        "quiz questions, explanations, and full user+assistant exchanges. "
        "Use this to recall what was discussed in previous sessions — "
        "e.g., 'what quiz questions did we cover?', "
        "'what code examples did we write for list comprehensions?', "
        "'when did we discuss error handling?'",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query describing what you're looking for",
                },
                "n_results": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5, max: 20)",
                },
            },
            "required": ["query"],
        },
    )
    async def search_past_conversations_tool(args):
        try:
            query = args["query"]
            n = min(args.get("n_results", 5), 20)

            email = _email_from_dir(user_data_dir)

            # Search both collections: detailed exchanges + high-level summaries
            exchange_results = episodic.search_exchanges(email, query, n_results=n)
            summary_results = episodic.search_episodes(email, query, n_results=min(n, 3))

            if not exchange_results and not summary_results:
                return _text_result(json.dumps({
                    "detailed_exchanges": [],
                    "summaries": [],
                    "message": "No past conversations found matching your query.",
                }))

            return _text_result(json.dumps({
                "detailed_exchanges": exchange_results,
                "summaries": summary_results,
                "exchange_count": len(exchange_results),
                "summary_count": len(summary_results),
            }, default=str))
        except Exception as e:
            logger.error(f"search_past_conversations error: {e}", exc_info=True)
            return _error_result(f"Error searching conversations: {e}")

    @tool(
        "save_conversation_summary",
        "Save a summary of what was discussed in this conversation. "
        "Call this AFTER teaching a concept or having a significant discussion "
        "so you can recall it in future sessions. Include key topics and what was covered.",
        {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Concise summary of what was discussed (2-4 sentences)",
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key topic tags (e.g., ['lists', 'indexing', 'slicing'])",
                },
            },
            "required": ["summary", "topics"],
        },
    )
    async def save_conversation_summary_tool(args):
        _entered("save_conversation_summary")
        try:
            email = _email_from_dir(user_data_dir)
            episode_id = episodic.save_episode(
                email=email,
                summary=args["summary"],
                topics=args.get("topics", []),
            )
            return _text_result(json.dumps({
                "status": "saved",
                "episode_id": episode_id,
                "message": "Conversation summary saved to long-term memory.",
            }))
        except Exception as e:
            logger.error(f"save_conversation_summary error: {e}", exc_info=True)
            return _error_result(f"Error saving summary: {e}")

    @tool(
        "save_image_memory",
        "Save an image the user shared along with your description of it. "
        "Images are stored as files — only the caption and path go into memory. "
        "Call this when the user shares a screenshot, diagram, or code image.",
        {
            "type": "object",
            "properties": {
                "image_base64": {
                    "type": "string",
                    "description": "Base64-encoded image data",
                },
                "caption": {
                    "type": "string",
                    "description": "Your description of the image content and its relevance to the learning session",
                },
                "content_type": {
                    "type": "string",
                    "description": "MIME type (default: image/png)",
                },
            },
            "required": ["image_base64", "caption"],
        },
    )
    async def save_image_memory_tool(args):
        try:
            email = _email_from_dir(user_data_dir)
            result = episodic.save_image(
                email=email,
                image_data=args["image_base64"],
                caption=args["caption"],
                content_type=args.get("content_type", "image/png"),
            )
            return _text_result(json.dumps({
                "status": "saved",
                "file_path": result["file_path"],
                "caption": result["caption"],
                "message": "Image saved to disk; caption stored in long-term memory.",
            }))
        except Exception as e:
            logger.error(f"save_image_memory error: {e}", exc_info=True)
            return _error_result(f"Error saving image: {e}")

    @tool(
        "get_memory_status",
        "See what you have already written into long-term memory: how many conversation "
        "summaries exist, when the last one was saved, and which concepts you have taught "
        "since then. Call this before saving a summary to check whether a topic is already "
        "covered, or whenever you want to know if your notes are behind the conversation. "
        "Cheap — returns counts and short strings, not full transcripts.",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    )
    async def get_memory_status_tool(args):
        try:
            email = _email_from_dir(user_data_dir)

            # Recency-ordered already; see EpisodicMemory.get_recent_episodes.
            recent = episodic.get_recent_episodes(email, n=5)
            last_at = recent[0]["timestamp"] if recent else None

            summaries = [
                {
                    "timestamp": ep["timestamp"][:19],
                    "topics": ep["topics"],
                    # First line only: this is a status check, not a retrieval.
                    # search_past_conversations is where full text belongs.
                    "preview": (ep["summary"] or "").split("\n")[0][:160],
                }
                for ep in recent
            ]

            data = knowledge.load_knowledge()
            path = data.get("learning_path", [])

            # Concepts taught since the last summary — the gap, computed rather
            # than guessed. Without this the model cannot tell a covered topic
            # from an uncovered one, which is how 72 concepts ended up with 30
            # summaries between them.
            since_last = []
            if last_at:
                since_last = [
                    e["concept"] for e in path
                    if e.get("action") == "introduced" and e.get("timestamp", "") > last_at
                ]
            else:
                since_last = [
                    e["concept"] for e in path if e.get("action") == "introduced"
                ]

            hours_since = None
            if last_at:
                try:
                    delta = datetime.now(timezone.utc) - datetime.fromisoformat(last_at)
                    hours_since = round(delta.total_seconds() / 3600, 1)
                except ValueError:
                    pass

            return _text_result(json.dumps({
                "summary_count": episodic.count_episodes(email),
                "last_summary_at": last_at[:19] if last_at else None,
                "hours_since_last_summary": hours_since,
                "recent_summaries": summaries,
                "concepts_since_last_summary": since_last,
                "recent_concept_activity": [
                    {
                        "concept": e.get("concept"),
                        "action": e.get("action"),
                        "at": (e.get("timestamp") or "")[:19],
                    }
                    for e in path[-10:]
                ],
            }, default=str))
        except Exception as e:
            logger.error(f"get_memory_status error: {e}", exc_info=True)
            return _error_result(f"Error reading memory status: {e}")

    # =====================================================================
    # TIER 3: Semantic Memory Tools (JSON Knowledge Graph)
    # =====================================================================

    @tool(
        "get_learning_progress",
        "The learner's progress: how many concepts at each mastery level, quiz "
        "stats, and what to study next. Returns concept NAMES grouped by "
        "mastery, not the full record of each — pass a category or a concept_id "
        "to see the detail (notes, prerequisites, review history) for just that "
        "part.",
        {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": (
                        "Optional. Full detail for one category only, e.g. "
                        "'data_structures'."
                    ),
                },
                "concept_id": {
                    "type": "string",
                    "description": "Optional. Full detail for one concept.",
                },
            },
            "required": [],
        },
    )
    async def get_learning_progress_tool(args):
        """Summarise by default; expand only what was asked for.

        This used to return data["concepts"] verbatim — every field of every
        concept. Against a real learner's file that is 54KB in one tool result,
        most of it `notes` prose, on a tool the tutor can call at any moment
        with no arguments. Everything a tool returns also stays in the
        transcript and is re-sent on every later turn, so the cost repeated for
        the rest of the session.

        The summary answers what the tutor actually asks — where is this learner
        strong, what is shaky, what next — and the two optional arguments fetch
        the detail behind any single answer.
        """
        try:
            data = knowledge.load_knowledge()
            quiz_history = knowledge.load_quiz_history()
            suggestions = knowledge.get_next_topic_suggestions(5)
            concepts = data.get("concepts", {})

            result = {
                "profile": data.get("profile", {}),
                "total_concepts": len(concepts),
                "learning_path_length": len(data.get("learning_path", [])),
                "quiz_stats": quiz_history.get("stats", {}),
                "suggested_next_topics": suggestions,
            }

            concept_id = (args.get("concept_id") or "").strip()
            category = (args.get("category") or "").strip()

            if concept_id:
                one = concepts.get(concept_id)
                result["concept"] = one or f"no concept {concept_id!r}"
            elif category:
                result["concepts"] = {
                    cid: c
                    for cid, c in concepts.items()
                    if c.get("category") == category
                }
                if not result["concepts"]:
                    result["note"] = (
                        f"No concepts in {category!r}. Categories in use: "
                        + ", ".join(sorted(
                            {c.get("category", "") for c in concepts.values()} - {""}
                        ))
                    )
            else:
                # Names grouped by mastery: enough to see the shape of what the
                # learner knows, without the notes that make up most of the bytes.
                by_mastery: dict[str, list[str]] = {}
                for cid, c in concepts.items():
                    by_mastery.setdefault(c.get("mastery", "unknown"), []).append(cid)
                result["by_mastery"] = {
                    level: sorted(ids) for level, ids in sorted(by_mastery.items())
                }
                result["how_to_see_detail"] = (
                    "Pass category= or concept_id= for the full record, "
                    "including notes and prerequisites."
                )

            return _text_result(json.dumps(result, default=str))
        except Exception as e:
            logger.error(f"get_learning_progress error: {e}", exc_info=True)
            return _error_result(f"Error reading progress: {e}")

    @tool(
        "update_concept",
        "Record or update a Python concept in the learner's knowledge tracker. "
        "Call this AFTER teaching a new concept or when the learner demonstrates improved understanding.\n"
        "Mastery levels:\n"
        "- 'introduced': First exposure — concept was explained\n"
        "- 'practiced': Learner worked with it (wrote code, answered questions)\n"
        "- 'mastered': Learner demonstrated deep understanding or passed a quiz",
        {
            "type": "object",
            "properties": {
                "concept_id": {
                    "type": "string",
                    "description": "Snake_case unique ID (e.g., 'list_comprehensions', 'for_loops')",
                },
                "name": {
                    "type": "string",
                    "description": "Human-readable name (e.g., 'List Comprehensions')",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "REQUIRED. Snake_case category id. Prefer an existing one: "
                        "Python: fundamentals, control_flow, data_structures, functions, oop, "
                        "error_handling, file_io, modules, testing, data_processing, advanced. "
                        "ML: ml_core, ml_supervised, ml_unsupervised, ml_eval. "
                        "Deep learning: dl_basics, dl_architectures. "
                        "LLM / RAG: llm_core, llm_rag. "
                        "MLOps: ops_serving, ops_monitoring. "
                        "Quantum: qc_foundations, qc_qiskit, qc_algorithms, qc_advanced. "
                        "System design: sd_foundations, sd_principles, sd_components, "
                        "sd_data, sd_distributed, sd_architecture. "
                        "If none fits, invent one — keep the track prefix so it "
                        "groups correctly (e.g. ml_ensembles) and it is registered "
                        "automatically. Never omit this field."
                    ),
                },
                "mastery": {
                    "type": "string",
                    "enum": ["introduced", "practiced", "mastered"],
                    "description": "Current mastery level",
                },
                "prerequisites": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concept IDs that should be learned first",
                },
                "related": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Related concept IDs",
                },
                "notes": {
                    "type": "string",
                    "description": "Brief notes on what was covered or demonstrated",
                },
            },
            "required": ["concept_id", "name", "category", "mastery"],
        },
    )
    async def update_concept_tool(args):
        # Before the try: this must record arrival even if everything below
        # throws. This is the tool that keeps failing with "Stream closed", so
        # it is the one whose absence from the log is the evidence.
        _entered("update_concept")
        try:
            # upsert_concept is a synchronous read-modify-write of one JSON
            # file. The model routinely emits several update_concept calls in a
            # single parallel batch — sometimes twice for the SAME concept —
            # and those handlers interleave on one event loop, so without this
            # lock they read the same state and overwrite each other. Observed
            # as a batch of six where two calls reported "Stream closed" and
            # the other four "Sibling tool call errored", losing every concept
            # in the batch. The lock is keyed on the file, not this store,
            # because a KnowledgeStore is constructed per request.
            async with knowledge_lock(knowledge.knowledge_path):
                concept = knowledge.upsert_concept(
                    concept_id=args["concept_id"],
                    name=args["name"],
                    category=args["category"],
                    mastery=args["mastery"],
                    prerequisites=args.get("prerequisites"),
                    related=args.get("related"),
                    notes=args.get("notes", ""),
                )
            return _text_result(json.dumps({
                "status": "updated",
                "concept": concept,
                "message": f"Concept '{args['name']}' recorded at mastery level '{args['mastery']}'.",
            }, default=str))
        except Exception as e:
            logger.error(f"update_concept error: {e}", exc_info=True)
            return _error_result(f"Error updating concept: {e}")

    @tool(
        "suggest_next_topics",
        "Suggest what the learner should study next, based on prerequisite chains and "
        "current mastery. Returns only concepts they can START NOW. "
        "Pass `track` to scope to one domain (python, ml, dl, llm, ops, quantum, sysdesign). "
        "If the learner asked about a specific domain and this returns nothing, that domain "
        "is gated behind unmet prerequisites — call get_domain_map(track) to see its shape "
        "and what unlocks it. Do NOT ask the learner what the domain contains; the "
        "curriculum knows.",
        {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of suggestions to return (default: 3)",
                },
                "track": {
                    "type": "string",
                    "description": (
                        "Restrict to one track: python, ml, dl, llm, ops "
                        "(MLOps), quantum, sysdesign. Omit for a cross-track mix."
                    ),
                },
            },
            "required": [],
        },
    )
    async def suggest_next_topics_tool(args):
        try:
            count = args.get("count", 3)
            track = args.get("track")
            if track is not None:
                track = str(track).strip().lower()
                if track not in knowledge.tracks:
                    return _error_result(
                        f"Unknown track '{track}'. Valid tracks: "
                        f"{', '.join(sorted(knowledge.tracks))}."
                    )

            suggestions = knowledge.get_next_topic_suggestions(count, track=track)

            if not suggestions:
                # An empty result used to claim the curriculum was finished.
                # That is one of two very different situations, and the wrong
                # one sends the tutor off to invent advanced material. Ask the
                # domain map which it is.
                if track:
                    dmap = knowledge.get_domain_map(track)
                    return _text_result(json.dumps({
                        "suggestions": [],
                        "track": track,
                        "message": (
                            f"Nothing in {dmap['track_name']} can be started yet: "
                            f"{dmap['blocked']} of {dmap['total']} concepts are "
                            f"blocked by unmet prerequisites. Call "
                            f"get_domain_map('{track}') for the full structure "
                            f"and the concepts that unlock it."
                        ),
                        "gateway_concepts": dmap["gateway_concepts"][:5],
                    }, default=str))

                remaining = [
                    c for c in CURRICULUM_GRAPH
                    if c not in knowledge.load_knowledge()["concepts"]
                ]
                if remaining:
                    return _text_result(json.dumps({
                        "suggestions": [],
                        "message": (
                            f"No concept is currently startable, but "
                            f"{len(remaining)} remain uncovered — they are all "
                            f"behind unmet prerequisites. Use get_domain_map "
                            f"on a track to see what unlocks it."
                        ),
                    }))
                return _text_result(json.dumps({
                    "suggestions": [],
                    "message": "All curriculum concepts have been covered! Consider diving deeper into advanced topics.",
                }))

            return _text_result(json.dumps({
                "suggestions": suggestions,
                "count": len(suggestions),
            }, default=str))
        except Exception as e:
            logger.error(f"suggest_next_topics error: {e}", exc_info=True)
            return _error_result(f"Error suggesting topics: {e}")

    @tool(
        "get_domain_map",
        "Get the full structure of one domain: every concept in it, in learning order, "
        "each marked available / blocked / already-known, plus the specific prerequisites "
        "that are missing and the 'gateway' concepts that unlock the most. "
        "Use this whenever the learner asks what a domain contains, asks for a structured "
        "path through it, or when suggest_next_topics returns nothing for that track. "
        "This is how you answer 'what's in MLOps?' — never ask the learner to tell you.",
        {
            "type": "object",
            "properties": {
                "track": {
                    "type": "string",
                    "description": (
                        "Which domain: python, ml, dl, llm, ops (MLOps), "
                        "quantum, sysdesign."
                    ),
                },
                "status": {
                    "type": "string",
                    "enum": ["available", "blocked", "known", "all"],
                    "description": (
                        "Which concepts to list. Defaults to 'available' — "
                        "what the learner can start now, which is what most "
                        "questions are really asking. 'all' returns the whole "
                        "track and is large."
                    ),
                },
            },
            "required": ["track"],
        },
    )
    async def get_domain_map_tool(args):
        """The map, filtered to what was asked for.

        A whole track is ~50 concepts with their prerequisite lists — 15KB in
        one result, which then rides in the transcript for the rest of the
        session. The counts (total / available / blocked / known) are always
        included, so the shape of the track survives the filter; only the
        per-concept rows are narrowed.
        """
        try:
            track = str(args.get("track", "")).strip().lower()
            if track not in knowledge.tracks:
                return _error_result(
                    f"Unknown track '{track}'. Valid tracks: "
                    f"{', '.join(sorted(knowledge.tracks))}."
                )

            data = knowledge.get_domain_map(track)
            status = (args.get("status") or "available").strip().lower()

            if status != "all":
                rows = [c for c in data.get("concepts", []) if c.get("status") == status]
                data["concepts"] = rows
                data["showing"] = status
                data["how_to_see_more"] = (
                    "Pass status='blocked', 'known' or 'all' for the rest."
                )
            return _text_result(json.dumps(data, default=str))
        except Exception as e:
            logger.error(f"get_domain_map error: {e}", exc_info=True)
            return _error_result(f"Error building domain map: {e}")

    @tool(
        "get_quiz_topics",
        "Get a list of Python concepts suitable for a quiz. Prioritizes concepts at 'introduced' "
        "or 'practiced' mastery level, and those not recently reviewed. "
        "Call this when the user asks to be quizzed.",
        {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of topics to return (default: 5)",
                },
                "category": {
                    "type": "string",
                    "description": "Optional: filter by category (e.g., 'data_structures')",
                },
            },
            "required": [],
        },
    )
    async def get_quiz_topics_tool(args):
        try:
            count = args.get("count", 5)
            category = args.get("category")
            candidates = knowledge.get_quiz_candidates(count, category)

            if not candidates:
                return _text_result(json.dumps({
                    "topics": [],
                    "message": "No concepts tracked yet. The learner needs to study some topics first before taking a quiz.",
                }))

            return _text_result(json.dumps({
                "topics": candidates,
                "count": len(candidates),
            }, default=str))
        except Exception as e:
            logger.error(f"get_quiz_topics error: {e}", exc_info=True)
            return _error_result(f"Error getting quiz topics: {e}")

    @tool(
        "record_quiz_result",
        "Record the results of a completed quiz. Updates mastery levels based on performance: "
        "correct answers on 'introduced' concepts promote to 'practiced', "
        "correct answers on 'practiced' concepts promote to 'mastered'. "
        "Call this AFTER the user completes a quiz.",
        {
            "type": "object",
            "properties": {
                "quiz_id": {
                    "type": "string",
                    "description": "Unique quiz ID (e.g., 'q_20260222_143000')",
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concept IDs that were tested",
                },
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "user_answer": {"type": "string"},
                            "correct": {"type": "boolean"},
                            "concept": {"type": "string"},
                        },
                        "required": ["question", "user_answer", "correct", "concept"],
                    },
                    "description": "List of quiz questions with results",
                },
                "score": {"type": "integer", "description": "Number of correct answers"},
                "total": {"type": "integer", "description": "Total number of questions"},
                "retest_concept": {
                    "type": "string",
                    "description": (
                        "Set ONLY when the learner explicitly asked to be re-tested on a "
                        "concept listed under 'Struggles with'. Every question on that "
                        "concept must be correct for the struggle flag to clear. Never set "
                        "this for an ordinary quiz, and never set it unless the learner "
                        "asked for the re-test themselves."
                    ),
                },
            },
            "required": ["quiz_id", "topics", "questions", "score", "total"],
        },
    )
    async def record_quiz_result_tool(args):
        _entered("record_quiz_result")
        try:
            retest_concept = args.get("retest_concept") or None
            result = knowledge.record_quiz(args, retest_concept=retest_concept)
            payload = {
                "status": "recorded",
                "mastery_changes": result["mastery_changes"],
                "quiz_summary": {
                    "score": args["score"],
                    "total": args["total"],
                    "percentage": result["quiz_entry"]["percentage"],
                },
                "message": "Quiz results saved and mastery levels updated.",
            }
            if retest_concept:
                payload["struggle_cleared"] = result["struggle_cleared"]
                payload["message"] += (
                    f" Re-test passed — '{retest_concept}' is no longer flagged as a struggle area."
                    if result["struggle_cleared"]
                    else f" Re-test not passed — '{retest_concept}' stays flagged."
                )
            return _text_result(json.dumps(payload, default=str))
        except Exception as e:
            logger.error(f"record_quiz_result error: {e}", exc_info=True)
            return _error_result(f"Error recording quiz: {e}")

    @tool(
        "update_learner_profile",
        "Update the learner's profile with observations about their level, strengths, "
        "struggle areas, or learning preferences. Call this when you notice patterns "
        "in the learner's understanding.",
        {
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "description": "Overall level: beginner, intermediate, advanced",
                },
                "strengths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concept IDs or areas the learner is strong in",
                },
                "struggle_areas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concept IDs or areas the learner struggles with",
                },
                "preferences": {
                    "type": "string",
                    "description": "Notes about learning style, preferences, or goals",
                },
            },
            "required": [],
        },
    )
    async def update_learner_profile_tool(args):
        _entered("update_learner_profile")
        try:
            data = knowledge.load_knowledge()
            profile = data.get("profile", {})

            if "level" in args:
                profile["level"] = args["level"]
            if "strengths" in args:
                profile["strengths"] = args["strengths"]
            if "struggle_areas" in args:
                profile["struggle_areas"] = args["struggle_areas"]
            if "preferences" in args:
                profile["preferences"] = args["preferences"]

            profile["last_active"] = datetime.now(timezone.utc).isoformat()
            data["profile"] = profile
            knowledge.save_knowledge(data)

            return _text_result(json.dumps({
                "status": "updated",
                "profile": profile,
                "message": "Learner profile updated.",
            }, default=str))
        except Exception as e:
            logger.error(f"update_learner_profile error: {e}", exc_info=True)
            return _error_result(f"Error updating profile: {e}")

    # =====================================================================
    # Quiz + Feedback Tools
    # =====================================================================

    @tool(
        "generate_quiz_question",
        "Generate ONE quiz question with options shuffled server-side so the "
        "correct answer lands at a uniformly random position (not always A). "
        "Call this for EVERY quiz question — one call per question. "
        "Validation: exactly 4 options, all non-empty, all distinct, "
        "correct_index in 0..3. The tool returns shuffled_options and the "
        "new correct_letter — you MUST render the question in chat using "
        "shuffled_options in order as A/B/C/D and remember correct_letter "
        "to grade the learner's answer.",
        {
            "type": "object",
            "properties": {
                "concept_id": {
                    "type": "string",
                    "description": "Concept ID this question tests (e.g., 'list_comprehensions')",
                },
                "question": {
                    "type": "string",
                    "description": "The question text",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exactly 4 answer options, all distinct",
                },
                "correct_index": {
                    "type": "integer",
                    "description": "Index (0..3) of the correct option in the input list",
                },
                "explanation": {
                    "type": "string",
                    "description": "Short explanation shown after the learner answers",
                },
            },
            "required": ["concept_id", "question", "options", "correct_index"],
        },
    )
    async def generate_quiz_question_tool(args):
        try:
            options = args.get("options") or []
            correct_index = args.get("correct_index")

            if len(options) != 4:
                return _error_result(
                    f"options must contain exactly 4 entries, got {len(options)}"
                )
            cleaned = [(o or "").strip() for o in options]
            if any(not o for o in cleaned):
                return _error_result("all options must be non-empty after trimming")
            if len(set(cleaned)) != 4:
                return _error_result("options must be distinct (no duplicates)")
            if not isinstance(correct_index, int) or correct_index not in range(4):
                return _error_result(
                    f"correct_index must be an integer in 0..3, got {correct_index!r}"
                )

            correct_text = cleaned[correct_index]
            shuffled = cleaned[:]
            random.shuffle(shuffled)
            new_correct_index = shuffled.index(correct_text)
            correct_letter = "ABCD"[new_correct_index]

            return _text_result(json.dumps({
                "concept_id": args["concept_id"],
                "question": args["question"],
                "shuffled_options": shuffled,
                "correct_index": new_correct_index,
                "correct_letter": correct_letter,
                "explanation": args.get("explanation", ""),
            }, default=str))
        except Exception as e:
            logger.error(f"generate_quiz_question error: {e}", exc_info=True)
            return _error_result(f"Error generating quiz question: {e}")

    @tool(
        "get_feedback",
        "Read aspect-scoped guidance set by the admin and the learner. "
        "Call this BEFORE performing a relevant action: "
        "'quiz' before each quiz question, 'suggest_topic' before suggesting "
        "next topics, 'explain' before explaining a new concept, 'general' "
        "at the start of a new conversation. Returns concatenated global + "
        "learner-specific guidance as markdown.",
        {
            "type": "object",
            "properties": {
                "aspect": {
                    "type": "string",
                    "enum": list(FEEDBACK_ASPECTS),
                    "description": "Which aspect of behavior to load guidance for",
                },
            },
            "required": ["aspect"],
        },
    )
    async def get_feedback_tool(args):
        try:
            aspect = args.get("aspect", "")
            if aspect not in FEEDBACK_ASPECTS:
                return _error_result(
                    f"unknown aspect {aspect!r}; must be one of {list(FEEDBACK_ASPECTS)}"
                )
            email = _email_from_dir(user_data_dir)
            text = load_feedback(aspect, email=email)
            return _text_result(json.dumps({
                "aspect": aspect,
                "guidance": text,
                "has_content": bool(text.strip()),
            }, default=str))
        except Exception as e:
            logger.error(f"get_feedback error: {e}", exc_info=True)
            return _error_result(f"Error loading feedback: {e}")

    @tool(
        "run_code",
        "Execute Python in the learner's virtualenv and see its real output. "
        "Use this to CHECK YOURSELF before showing the learner a snippet whose "
        "result you would otherwise be predicting.\n\n"
        "Do NOT call this to format prose. If the code is only print() "
        "statements containing text you wrote, you already know the output and "
        "running it proves nothing — write it in your reply instead.\n\n"
        "Each call is a FRESH process with no shared state, so every snippet "
        "must be self-contained. A clean run returns a verified_id, which is "
        "how you show the code to the learner WITHOUT retyping it.\n\n"
        "Full mechanics — the verified:<id> placeholder syntax, %pip install, "
        "background runs, reading the result — are in the `code-execution` "
        "skill. Load it before your first run of a conversation.",
        {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Complete, self-contained Python program. Include every "
                        "import; nothing persists from earlier calls."
                    ),
                },
                "purpose": {
                    "type": "string",
                    "description": (
                        "One line on what you are checking, e.g. 'confirm the "
                        "split threshold before explaining it'. Recorded with "
                        "the run for debugging; the learner does not see it, "
                        "so say anything they need to hear in your reply."
                    ),
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Run without waiting. Returns a job id immediately "
                        "instead of the output; collect it later with "
                        "get_code_result. Use for anything slow — a '%pip "
                        "install' line, a real training loop, the first plot "
                        "in a new environment — so the learner is not left "
                        "watching a frozen composer. Default false."
                    ),
                },
            },
            "required": ["code"],
        },
    )
    async def run_code_tool(args):
        """Run a snippet in the learner's venv and hand back its real output.

        Imported lazily: sandbox pulls in the venv machinery, and the demo
        builder loads this module too without ever running code.

        The result is deliberately trimmed. Everything a tool returns stays in
        the transcript and is re-sent on every later turn, so a runaway loop
        printing 50KB would become a permanent per-turn cost. The sandbox caps
        output at MAX_OUTPUT_BYTES; we cap tighter, keeping the head and tail
        because a traceback's useful line is at the end.
        """
        _entered("run_code")
        from backend.sandbox import run_code, VenvError

        try:
            code = (args.get("code") or "").strip()
            if not code:
                return _error_result("code is required")

            ratio = prose_ratio(code)
            if ratio > MAX_PROSE_RATIO and not computes_nothing(code):
                # Real computation is in here, but it is buried in an essay.
                # Refusing the whole cell rather than running it keeps the
                # lecture out of the transcript; the model can resubmit just
                # the part that computes.
                return _error_result(
                    f"Not run: {ratio:.0%} of this cell is English text in "
                    f"strings and comments, not code. The explanation belongs "
                    f"in your reply as prose — send only the lines that "
                    f"actually compute something, and write the teaching "
                    f"around them in chat."
                )

            if computes_nothing(code):
                # Refuse rather than run: the output would be the model's own
                # text handed back to it, and it would then live in the
                # transcript forever. Says what to do instead, so this costs
                # one corrected turn rather than a retry loop.
                return _error_result(
                    "Not run: this code computes nothing — it only prints text "
                    "you wrote, so its output is already known to you and "
                    "verifies nothing. Put explanations, summaries, "
                    "corrections and comparisons directly in your reply as "
                    "prose. Use run_code only when real computation decides "
                    "the answer (fitting a model, checking a threshold, "
                    "comparing actual values)."
                )

            email = _email_from_dir(user_data_dir)

            if args.get("background"):
                from backend.codejobs import start_code_job

                record = start_code_job(email, code, args.get("purpose", ""))
                return _text_result(json.dumps({
                    "job_id": record["job_id"],
                    "state": record["state"],
                    "note": (
                        "Running in the background — the chat is not blocked. "
                        "Keep teaching, then call get_code_result with this "
                        "job_id to collect the output. Do not describe what "
                        "the code prints until you have it."
                    ),
                }, default=str))

            result = await run_code(email, code, timeout=RUN_CODE_TIMEOUT_SEC)

            # Key order is load-bearing. agent.py truncates a tool result to
            # TOOL_RESULT_MAX_CHARS before writing it to the transcript, and
            # the transcript is what the model re-reads when it composes the
            # reply. With verified_id last, a run with sizable stdout pushed it
            # past the cut: the model saw no id to cite and fell back to
            # retyping the code — the exact drift the placeholder exists to
            # prevent. Observed twice in one session at exactly 2000 chars.
            # Anything the model must ACT on goes first; bulk output last.
            payload = {}
            if result["exit_code"] == 0:
                # The handle the model shows the learner instead of retyping the
                # code. Only for a clean run: pointing at a snippet that crashed
                # would present the crash as a worked example. See the tool
                # description for the placeholder syntax.
                from backend.codejobs import save_verified

                save_verified(email, result["run_id"], code)
                payload["verified_id"] = result["run_id"]
                # Spelled out in the result, not just the tool description:
                # the description is read once at the top of the turn, while
                # this sits right where the model decides what to write next.
                # It ignored the placeholder twice with the id available.
                payload["show_this_code_by_writing"] = (
                    f"```verified:{result['run_id']}\n```"
                )
                payload["do_not"] = (
                    "Do not paste the code into your reply. The line above IS "
                    "the code block the learner sees."
                )
            payload["exit_code"] = result["exit_code"]
            payload["duration_ms"] = result["duration_ms"]
            if result["timed_out"]:
                payload["timed_out"] = True
                # A dead end otherwise: the model sees only that it was killed.
                # Name the escape hatch so a misjudged fast-path call costs one
                # retry rather than the verification being abandoned. First use
                # of matplotlib in a fresh venv hits this too — the font cache
                # build alone can exceed the budget — and it succeeds on the
                # retry, so the advice holds for both causes.
                payload["hint"] = (
                    f"Killed at {RUN_CODE_TIMEOUT_SEC}s. If the work is "
                    f"genuinely slow (installs, big fits, first plot in a new "
                    f"environment), resubmit with background=true. Otherwise "
                    f"shrink it — fewer rows, fewer iterations."
                )
            if result["images"]:
                # Names only. The bytes are served over HTTP to the learner;
                # the model cannot see them and does not need to.
                payload["images"] = [img["name"] for img in result["images"]]
            if result["suggestion"] and "hint" not in payload:
                # The run died on a missing import. Tell the model the exact
                # magic line to retry with, so it fixes this in one more call
                # instead of guessing the PyPI name (sklearn is the trap).
                #
                # Only when nothing else set a hint. A timeout exits -15, which
                # is non-zero, so a killed cell that had already printed a
                # ModuleNotFoundError sets BOTH — and this used to overwrite
                # the timeout advice, sending the model back to the inline path
                # to be killed again instead of to background=true.
                pkg = result["suggestion"]["package"]
                payload["hint"] = (
                    f"Missing module. Retry with '%pip install {pkg}' as the "
                    f"first line of the cell."
                )
            # Last, and budgeted: whatever room the fixed-size fields above
            # leave is what output may occupy, so a chatty cell can never push
            # verified_id out of the stored transcript again.
            #
            # Budgeted ONCE across both streams, not once each. Sizing them
            # independently let a cell with large stdout AND stderr reach ~3400
            # chars against a 2000 budget, so verified_id survived only by
            # being first in the dict rather than because the cap worked.
            # stderr is filled first: when a run fails, the traceback is what
            # the model needs, and stdout is usually the less useful half.
            room = max(200, TRANSCRIPT_RESULT_BUDGET - len(json.dumps(payload)) - 200)
            stderr = _clip_output(result["stderr"], room)
            payload["stdout"] = _clip_output(
                result["stdout"], max(200, room - len(stderr))
            )
            payload["stderr"] = stderr

            # Then check the REAL serialized length: JSON escaping (\n, quotes)
            # inflates output past any character-count estimate. Trim from the
            # already-clipped strings by slicing, NOT by re-clipping — a second
            # _clip_output pass re-inserts its "[N chars trimmed]" marker and
            # can leave the string the same length, which made an earlier
            # version of this loop spin forever inside the request handler.
            for _ in range(3):
                over = len(json.dumps(payload, default=str)) - TRANSCRIPT_RESULT_BUDGET
                if over <= 0:
                    break
                longest = max(("stdout", "stderr"), key=lambda k: len(payload[k]))
                keep = max(100, len(payload[longest]) - over - 32)
                payload[longest] = payload[longest][:keep] + "\n... [trimmed]"
            return _text_result(json.dumps(payload, default=str))
        except VenvError as e:
            return _error_result(f"Environment is not usable: {e}")
        except Exception as e:
            logger.error(f"run_code error: {e}", exc_info=True)
            return _error_result(f"Error running code: {e}")

    @tool(
        "get_code_result",
        "Collect the output of a background run started by run_code. If the "
        "state is still 'queued' or 'building' it is not finished — carry on "
        "teaching and check again on a later turn rather than waiting, and do "
        "not describe what the code prints until you have the real output.",
        {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job_id run_code returned.",
                },
            },
            "required": ["job_id"],
        },
    )
    async def get_code_result_tool(args):
        """Read a background run's record.

        Returns the same output shape as the inline path once the job reaches
        a terminal state, so the model does not have to learn two formats.
        """
        from backend.codejobs import get_job
        from backend.demojobs import TERMINAL

        try:
            job_id = (args.get("job_id") or "").strip()
            if not job_id:
                return _error_result("job_id is required")

            email = _email_from_dir(user_data_dir)
            record = get_job(email, job_id)
            if record is None:
                return _error_result(f"No such job: {job_id}")

            payload = {"job_id": job_id, "state": record["state"]}
            if record["state"] not in TERMINAL:
                payload["note"] = (
                    "Still running. Keep teaching and check again later."
                )
            else:
                payload.update({
                    "stdout": record.get("stdout", ""),
                    "stderr": record.get("stderr", ""),
                    "exit_code": record.get("exit_code"),
                    "duration_ms": record.get("duration_ms"),
                })
                if record.get("run_id"):
                    payload["verified_id"] = record["run_id"]
                if record.get("timed_out"):
                    payload["timed_out"] = True
                if record.get("images"):
                    payload["images"] = record["images"]
                if record.get("error"):
                    payload["error"] = record["error"]
            return _text_result(json.dumps(payload, default=str))
        except Exception as e:
            logger.error(f"get_code_result error: {e}", exc_info=True)
            return _error_result(f"Error reading job: {e}")

    @tool(
        "save_demo",
        "Save a self-contained interactive HTML demo so the learner can open "
        "and play with it. Call this ONLY when the learner has explicitly "
        "asked to see, visualize, or interact with something — never "
        "volunteer a demo. The HTML must be a complete document with all CSS "
        "and JS inline: it runs in a sandboxed frame with no network access, "
        "so any external URL makes it render blank.",
        {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short name for the demo, e.g. 'Consistent hashing ring'",
                },
                "html_path": {
                    "type": "string",
                    "description": (
                        "PREFERRED. Path to the finished document in your "
                        "scratch directory. The server reads it from disk, so "
                        "the demo never goes through a tool call."
                    ),
                },
                "html": {
                    "type": "string",
                    "description": (
                        "Inline document — only for a small demo. Draft to a "
                        "file and pass html_path instead. Self-contained: "
                        "inline all CSS and JS, no CDN links or remote images."
                    ),
                },
                "concept_id": {
                    "type": "string",
                    "description": "Concept this demonstrates, e.g. 'consistent_hashing'",
                },
                "summary": {
                    "type": "string",
                    "description": "One line telling the learner what to try first",
                },
            },
            "required": ["title"],
        },
    )
    async def save_demo_tool(args):
        """Store a demo and return a reference to it.

        Deliberately returns metadata only, never the HTML: the tool result
        lands in the conversation transcript, and echoing a few hundred lines
        back would be re-sent to the model on every later turn and slow replay.
        The frontend fetches the HTML separately and renders it inside a
        sandboxed iframe — it is never injected into the chat DOM.
        """
        # Refuse a payload big enough to take the channel down. See
        # agent.check_payload_size for why this is here and not in a hook.
        _too_big = check_payload_size("mcp__learning-tools__save_demo", args)
        if _too_big:
            return _error_result(_too_big)

        from backend.demos import save_demo as store_demo

        # Only the builder may write a demo. allowed_tools is advisory under
        # bypassPermissions (that mode skips the check it feeds), and the model
        # was observed reaching save_demo anyway via the skill — which put 18KB
        # of HTML back inside the chat turn, the exact freeze this design
        # removes. build_ctx is present only for builder runs, so it is the
        # real boundary.
        if build_ctx is None:
            return _error_result(
                "save_demo is not available here. Call request_demo(title, "
                "concept_id, request) instead — a background builder writes "
                "the demo so the learner is not blocked. Do not write HTML."
            )

        try:
            email = _email_from_dir(user_data_dir)
            build = build_ctx or {}
            meta = store_demo(
                email=email,
                title=args.get("title", ""),
                html=args.get("html", ""),
                html_path=args.get("html_path", ""),
                concept_id=args.get("concept_id", ""),
                summary=args.get("summary", ""),
                # Stamped by the job runner, which knows both ids. This is what
                # makes the demo refinable later.
                builder_session_id=build.get("builder_session_id", ""),
                chat_session_id=build.get("chat_session_id", ""),
                # How the job runner finds this demo afterwards. build_ctx
                # cannot carry the id back: over HTTP it is a request copy.
                job_id=build.get("job_id", ""),
            )
            from backend.demos import missing_element_ids

            payload = {
                "demo_id": meta["id"],
                "title": meta["title"],
                "status": "saved",
                "note": "The demo is now open in the learner's Demo pane. "
                        "Tell them briefly what to try.",
            }

            # Saved, but likely broken on first click. A warning rather than a
            # rejection: an id can legitimately be created at runtime, so
            # refusing a working demo would be worse than flagging a suspect
            # one. If it is a real mismatch, fix it and call save_demo again.
            #
            # Read back what was stored rather than the html argument, which is
            # empty on the html_path route — checking the argument would make
            # this warning silently stop firing for every path-based save.
            from backend.demos import get_demo

            stored = get_demo(email, meta["id"])
            missing = missing_element_ids(stored["html"] if stored else "")
            if missing:
                payload["warning"] = (
                    "getElementById looks up "
                    + ", ".join(sorted(missing))
                    + " but no element has that id. If these are not created at "
                      "runtime the demo will throw on first click — fix the ids "
                      "and save again."
                )

            return _text_result(json.dumps(payload))
        except ValueError as e:
            # Actionable on purpose — the model can fix the HTML and retry.
            return _error_result(f"Demo rejected: {e}")
        except Exception as e:
            logger.error(f"save_demo error: {e}", exc_info=True)
            return _error_result(f"Error saving demo: {e}")

    @tool(
        "demo_open",
        "Start a demo that is too big to write in one go, by saving its SHELL: "
        "the doctype, all the shared CSS, all the shared JS, and the marker "
        "<!--{{PARTS}}--> where the panels will be inserted. Use this instead "
        "of save_demo when the demo needs more than about four panels — one "
        "reply cannot hold a large document, and going over discards the whole "
        "reply and saves nothing. Returns the demo_id the parts attach to. "
        "The shell defines the contract every part must follow: decide the "
        "class names, the data model and the panel order HERE, then state them "
        "in each part's spec. Builder only.",
        {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short name, e.g. 'Database shapes'",
                },
                "shell_html": {
                    "type": "string",
                    "description": (
                        "The document WITHOUT the panels: doctype, <html>, "
                        "<head> with every shared style, <body> with any fixed "
                        "chrome, the literal marker <!--{{PARTS}}--> on its own "
                        "line, and <script> with every shared function. It must "
                        "contain the marker exactly once. Inline everything — "
                        "the sandbox blocks all network access."
                    ),
                },
                "concept_id": {
                    "type": "string",
                    "description": "Concept this demonstrates, e.g. 'databases'",
                },
                "summary": {
                    "type": "string",
                    "description": "One line telling the learner what to try first",
                },
                "parts": {
                    "type": "array",
                    "description": (
                        "OPTIONAL. Declare the panels here and they are built "
                        "FOR you, several at once, and finalized automatically "
                        "— you write only the shell and stop. Prefer this for "
                        "anything past a few panels: it is much faster than "
                        "writing them yourself one call at a time. Each entry "
                        "needs 'name' (slug) and 'spec' (exactly what this "
                        "panel must show, which shared class names to use, and "
                        "the data to render — the panel's author sees your "
                        "spec and the shell, nothing else). Order here is the "
                        "order on screen. Omit this to write the panels "
                        "yourself with demo_write_part."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "spec": {"type": "string"},
                        },
                        "required": ["name", "spec"],
                    },
                },
            },
            "required": ["title", "shell_html"],
        },
    )
    async def demo_open_tool(args):
        """Mint the demo id and store its shell. Nothing renders yet.

        The demo stays invisible to the learner until demo_finalize runs, so a
        build that dies half way leaves no empty frame in their pane.
        """
        # Refuse a payload big enough to take the channel down. See
        # agent.check_payload_size for why this is here and not in a hook.
        _too_big = check_payload_size("mcp__learning-tools__demo_open", args)
        if _too_big:
            return _error_result(_too_big)

        from backend.demos import open_demo

        if build_ctx is None:
            return _error_result(
                "demo_open is not available here. If you are the tutor, call "
                "request_demo instead."
            )

        try:
            email = _email_from_dir(user_data_dir)
            build = build_ctx or {}
            # Declared panels are built by the job runner, concurrently, once
            # this agent's turn ends — see _run_build. Stored on the demo's
            # meta rather than acted on here because a tool cannot spawn the
            # agents that would write them, and the runner reads them from
            # disk. The shell they must match is already on disk as well.
            declared = [
                {"name": str(p.get("name") or ""), "spec": str(p.get("spec") or "")}
                for p in (args.get("parts") or [])
                if isinstance(p, dict) and p.get("name") and p.get("spec")
            ]
            meta = open_demo(
                email=email,
                title=args.get("title", ""),
                shell_html=args.get("shell_html", ""),
                concept_id=args.get("concept_id", ""),
                summary=args.get("summary", ""),
                chat_session_id=build.get("chat_session_id", ""),
                job_id=build.get("job_id", ""),
                declared_parts=declared,
            )
            if declared:
                return _text_result(json.dumps({
                    "demo_id": meta["id"],
                    "status": "open",
                    "parts_declared": len(declared),
                    "next": (
                        f"Stop now. The {len(declared)} panels you declared are "
                        f"being written in parallel and the demo will be "
                        f"finalized for you. Do NOT write them yourself and do "
                        f"NOT call demo_finalize."
                    ),
                }))

            return _text_result(json.dumps({
                "demo_id": meta["id"],
                "status": "open",
                "next": (
                    "Now write the panels with demo_write_part — index 1, 2, 3 "
                    "… in the order they should appear. Give every part the "
                    "same class names the shell's CSS defines. Then call "
                    "demo_finalize once."
                ),
            }))
        except ValueError as e:
            return _error_result(f"Shell rejected: {e}")
        except Exception as e:
            logger.error(f"demo_open error: {e}", exc_info=True)
            return _error_result(f"Error opening demo: {e}")

    @tool(
        "demo_write_part",
        "Write ONE panel of a demo opened with demo_open. Send only this "
        "panel's markup — not the doctype, not <html>, not the shared CSS or "
        "JS, all of which already live in the shell. Parts are inserted in "
        "index order, so index decides where this panel appears. Keeping each "
        "call to a single panel is the whole point: it is what stops a large "
        "demo from exceeding one reply's output limit. Builder only.",
        {
            "type": "object",
            "properties": {
                "demo_id": {"type": "string", "description": "From demo_open"},
                "index": {
                    "type": "integer",
                    "description": "1-based position of this panel in the demo",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Short slug for this panel, e.g. 'relational' or "
                        "'keyvalue'. Used for the filename only."
                    ),
                },
                "html": {
                    "type": "string",
                    "description": (
                        "This panel's markup only — a fragment, not a "
                        "document. Use the class names the shell defines. Any "
                        "JS specific to this panel goes in its own inline "
                        "<script> here."
                    ),
                },
            },
            "required": ["demo_id", "index", "name", "html"],
        },
    )
    async def demo_write_part_tool(args):
        """Store one panel.

        Each part is its own file, so several of these running at once never
        contend: there is no shared document to read-modify-write.
        """
        # Refuse a payload big enough to take the channel down. See
        # agent.check_payload_size for why this is here and not in a hook.
        _too_big = check_payload_size("mcp__learning-tools__demo_write_part", args)
        if _too_big:
            return _error_result(_too_big)

        from backend.demos import save_demo_part

        if build_ctx is None:
            return _error_result(
                "demo_write_part is not available here. If you are the tutor, "
                "call request_demo instead."
            )

        try:
            email = _email_from_dir(user_data_dir)
            filename = save_demo_part(
                email=email,
                demo_id=args.get("demo_id", ""),
                index=args.get("index", 0),
                name=args.get("name", ""),
                html=args.get("html", ""),
            )
            # Counted on disk: a counter on build_ctx would reset on every
            # request over the HTTP transport.
            from backend.demos import list_demo_parts

            return _text_result(json.dumps({
                "stored": filename,
                "parts_written": len(list_demo_parts(email, args.get("demo_id", ""))),
                "next": (
                    "Write the next panel, or call demo_finalize when every "
                    "panel is in."
                ),
            }))
        except ValueError as e:
            return _error_result(f"Part rejected: {e}")
        except Exception as e:
            logger.error(f"demo_write_part error: {e}", exc_info=True)
            return _error_result(f"Error writing part: {e}")

    @tool(
        "demo_finalize",
        "Assemble the shell and every part into the finished demo and show it "
        "to the learner. Call this ONCE, after the last demo_write_part. This "
        "is where the whole document is checked, so an error here is about the "
        "assembled result — most often a function an onclick names but no part "
        "defines. Builder only.",
        {
            "type": "object",
            "properties": {
                "demo_id": {"type": "string", "description": "From demo_open"},
            },
            "required": ["demo_id"],
        },
    )
    async def demo_finalize_tool(args):
        """Validate the assembled document and publish it."""
        from backend.demos import finalize_demo, missing_element_ids, get_demo

        if build_ctx is None:
            return _error_result(
                "demo_finalize is not available here. If you are the tutor, "
                "call request_demo instead."
            )

        try:
            email = _email_from_dir(user_data_dir)
            build = build_ctx or {}
            demo_id = args.get("demo_id", "")
            meta = finalize_demo(
                email=email,
                demo_id=demo_id,
                builder_session_id=build.get("builder_session_id", ""),
                job_id=build.get("job_id", ""),
            )
            if meta is None:
                return _error_result(f"No demo with id {demo_id!r}")

            payload = {
                "demo_id": meta["id"],
                "parts": meta.get("part_count"),
                "bytes": meta.get("bytes"),
                "status": "saved",
                "note": "The demo is now open in the learner's Demo pane. "
                        "Tell them briefly what to try.",
            }

            # Same warning save_demo gives, but it can only be computed now:
            # a handler defined in one part and called from another is only
            # visible once the parts are joined.
            demo = get_demo(email, meta["id"])
            missing = missing_element_ids(demo["html"]) if demo else set()
            if missing:
                payload["warning"] = (
                    "getElementById looks up "
                    + ", ".join(sorted(missing))
                    + " but no element has that id. If these are not created "
                      "at runtime the demo will throw on first click."
                )
            return _text_result(json.dumps(payload))
        except ValueError as e:
            return _error_result(f"Demo rejected: {e}")
        except Exception as e:
            logger.error(f"demo_finalize error: {e}", exc_info=True)
            return _error_result(f"Error finalizing demo: {e}")

    @tool(
        "demo_verify",
        "Check a saved demo for the faults that make one throw on first click: "
        "an onclick naming a function nothing defines, a getElementById for an "
        "id no element has, an external URL the sandbox will block. Call this "
        "after saving and BEFORE telling the tutor you are done — the frame is "
        "sandboxed, so a broken demo looks exactly like a working one until the "
        "learner clicks. Fix whatever it reports and call it again. Builder "
        "only.",
        {
            "type": "object",
            "properties": {
                "demo_id": {
                    "type": "string",
                    "description": "The demo to check.",
                },
            },
            "required": ["demo_id"],
        },
    )
    async def demo_verify_tool(args):
        """Report what is wrong with an assembled demo, as work to be done.

        Reads from disk rather than taking the HTML as an argument: the demo is
        already stored, and passing it back through a tool call would put the
        whole document in the transcript a second time.

        The checks themselves already existed — they were just advisory. The
        same faults were reported as a `warning` field tacked onto a successful
        save, which the model could and did ignore. Here they are the entire
        result, with an instruction attached.
        """
        from backend.demos import (
            get_demo,
            missing_element_ids,
            _missing_handlers,
            set_demo_verified,
            validate_demo_html,
        )

        if build_ctx is None:
            return _error_result(
                "demo_verify is not available here. If you are the tutor, the "
                "builder checks its own work."
            )

        try:
            email = _email_from_dir(user_data_dir)
            demo_id = (args.get("demo_id") or "").strip()
            demo = get_demo(email, demo_id)
            if demo is None:
                return _error_result(f"No demo with id {demo_id!r}")

            html = demo["html"]
            broken = []

            # The hard rules — these already block a save, so a failure here
            # means the demo on disk predates them or was assembled from parts.
            try:
                validate_demo_html(html)
            except ValueError as e:
                broken.append(str(e))

            handlers = _missing_handlers(html)
            if handlers:
                broken.append(
                    "These inline handlers call functions nothing defines at "
                    "top level: " + ", ".join(sorted(handlers)) + ". A function "
                    "inside DOMContentLoaded or a type=module script is "
                    "invisible to an onclick."
                )

            ids = missing_element_ids(html)
            if ids:
                broken.append(
                    "getElementById looks up ids no element has: "
                    + ", ".join(sorted(ids))
                    + ". If these are built at runtime this is fine; otherwise "
                    "the demo throws on first click."
                )

            payload = {"demo_id": demo_id, "broken": broken}
            if broken:
                payload["action"] = (
                    "Fix these and call demo_verify again. Do not report the "
                    "demo as finished while anything is listed here."
                )
            else:
                payload["note"] = (
                    "Nothing found. Static checks cannot see ids built by "
                    "concatenation or a runtime throw, so give the logic one "
                    "read before you stop."
                )
            # Recorded on the meta so the job runner can tell a checked build
            # from an unchecked one; see _run_build.
            set_demo_verified(email, demo_id, not broken)
            return _text_result(json.dumps(payload))
        except Exception as e:
            logger.error(f"demo_verify error: {e}", exc_info=True)
            return _error_result(f"Error verifying demo: {e}")

    @tool(
        "get_demo_html",
        "Check out a demo you are about to change. Copies it into your scratch "
        "directory and returns the PATH — not the document. Read and patch that "
        "file with your own file tools (Read, Grep, Edit), then publish it with "
        "update_demo(demo_id, html_path=...). Builder only.",
        {
            "type": "object",
            "properties": {
                "demo_id": {"type": "string", "description": "The demo to check out"},
            },
            "required": ["demo_id"],
        },
    )
    async def get_demo_html_tool(args):
        """Copy a demo into scratch and return where it landed.

        Returns a path, not the HTML. A demo runs 15-30KB, and everything a
        tool returns stays in the session transcript and is re-sent on every
        later turn — the same reason this was always kept away from the tutor.
        The builder is a resumed, multi-turn session too, so it paid that cost
        as well: one edit used to carry five copies of the document, between
        the original save, this read, a Write, a Read back, and the update.

        Handing back a path costs ~80 bytes and lets the builder patch the file
        with Edit, which never touches this channel.
        """
        from backend.demos import copy_demo_to_scratch

        if build_ctx is None:
            return _error_result("get_demo_html is only available to the demo builder.")

        try:
            email = _email_from_dir(user_data_dir)
            demo_id = args.get("demo_id", "")
            dest = build_ctx.get("scratch_dir")
            if not dest:
                # No scratch this run (mkdir failed upstream). Say so plainly
                # rather than falling back to returning the document, which is
                # the behaviour this replaced.
                return _error_result(
                    "No scratch directory for this build, so the demo cannot "
                    "be checked out. Rebuild it instead of editing."
                )

            path = copy_demo_to_scratch(email, demo_id, dest)
            return _text_result(json.dumps({
                "path": path,
                "next": (
                    "Read or Grep this file to find what to change, patch it "
                    "with Edit, then call update_demo(demo_id, html_path) with "
                    "this same path. Do not paste the document into a tool "
                    "call."
                ),
            }))
        except ValueError as e:
            return _error_result(str(e))
        except Exception as e:
            logger.error(f"get_demo_html error: {e}", exc_info=True)
            return _error_result(f"Error reading demo: {e}")

    @tool(
        "update_demo",
        "Replace the HTML of an existing demo, keeping its id and its place "
        "in the learner's list. Use this for every change to a demo that "
        "already exists — never save_demo, which would create a duplicate.",
        {
            "type": "object",
            "properties": {
                "demo_id": {"type": "string", "description": "The demo to update"},
                "html_path": {
                    "type": "string",
                    "description": (
                        "PREFERRED. Path to the patched file — the one "
                        "get_demo_html checked out. The server reads it from "
                        "disk, so the document never goes through a tool call."
                    ),
                },
                "html": {
                    "type": "string",
                    "description": (
                        "Inline replacement document. Avoid for anything "
                        "sizeable: use html_path instead."
                    ),
                },
                "title": {"type": "string", "description": "Optional new title"},
                "summary": {
                    "type": "string",
                    "description": "One line on what changed and what to try",
                },
            },
            "required": ["demo_id"],
        },
    )
    async def update_demo_tool(args):
        """Edit a demo in place so refinement does not churn the demo list."""
        # Refuse a payload big enough to take the channel down. See
        # agent.check_payload_size for why this is here and not in a hook.
        _too_big = check_payload_size("mcp__learning-tools__update_demo", args)
        if _too_big:
            return _error_result(_too_big)

        from backend.demos import update_demo as store_update

        if build_ctx is None:
            return _error_result(
                "update_demo is not available here. Call request_demo with "
                "base_demo_id set to change an existing demo."
            )

        try:
            email = _email_from_dir(user_data_dir)
            build = build_ctx or {}
            meta = store_update(
                email=email,
                demo_id=args.get("demo_id", ""),
                html=args.get("html", ""),
                html_path=args.get("html_path", ""),
                title=args.get("title", ""),
                summary=args.get("summary", ""),
                builder_session_id=build.get("builder_session_id", ""),
                job_id=build.get("job_id", ""),
            )
            if meta is None:
                return _error_result(f"No demo with id {args.get('demo_id')!r}")
            return _text_result(json.dumps({
                "demo_id": meta["id"],
                "version": meta["version"],
                "status": "updated",
            }))
        except ValueError as e:
            return _error_result(f"Demo rejected: {e}")
        except Exception as e:
            logger.error(f"update_demo error: {e}", exc_info=True)
            return _error_result(f"Error updating demo: {e}")

    @tool(
        "get_demo_source",
        "Read the parts of a demo's code that mention a name — a function, an "
        "element id, or a label the learner pointed at. Use it when the "
        "learner asks why something in a demo behaves as it does and the "
        "structure you were given is not enough. Returns matching excerpts "
        "only, never the whole file: pass a specific query like 'resetBtn'.",
        {
            "type": "object",
            "properties": {
                "demo_id": {"type": "string", "description": "The demo to read"},
                "query": {
                    "type": "string",
                    "description": (
                        "A function name, element id, or literal string to "
                        "find. Required — this never returns the whole file."
                    ),
                },
            },
            "required": ["demo_id", "query"],
        },
    )
    async def get_demo_source_tool(args):
        """Slice a demo's source around a query. Both agents.

        Everything a tool returns stays in the SDK transcript and is re-sent on
        every later turn, so a 17KB demo would cost ~4200 tokens per turn for
        the rest of the session. The char cap below is the real protection — it
        is enforced here and no prompt can talk its way past it.

        The builder gets this too, and used not to: the guard was
        `session_ctx is None`, which is set on builder runs for an unrelated
        reason (stopping a recursive request_demo), so it locked out the one
        agent that edits demos and pointed it at the whole-document reader
        instead. Finding the three lines to change is exactly what this is for.
        """
        from backend.demos import _read_json, _meta_path, _ID_ATTR, _FN_DECL, get_demo

        if session_ctx is None and build_ctx is None:
            return _error_result("get_demo_source is not available here.")

        demo_id = (args.get("demo_id") or "").strip()
        query = (args.get("query") or "").strip()
        if not query:
            return _error_result(
                "query is required — name a function, element id, or label to "
                "look for. This tool never returns the whole demo."
            )

        try:
            email = _email_from_dir(user_data_dir)
            if _read_json(_meta_path(email, demo_id)) is None:
                return _error_result(f"No demo with id {demo_id!r}")
            # What the learner sees. The flat file is absent or stale for a
            # multi-panel demo, whose page is assembled from its parts.
            demo = get_demo(email, demo_id)
            if demo is None:
                return _error_result(f"No source on disk for {demo_id!r}")
            html = demo["html"]

            lines = html.splitlines()
            needle = query.lower()
            hits = [i for i, line in enumerate(lines) if needle in line.lower()]

            if not hits:
                # The name not existing is often itself the answer, so say what
                # does exist rather than just reporting nothing.
                ids = sorted(set(_ID_ATTR.findall(html)))[:20]
                fns = sorted(set(_FN_DECL.findall(html)))[:20]
                return _error_result(
                    f"Nothing in the demo mentions {query!r}. "
                    f"Element ids present: {', '.join(ids) or 'none'}. "
                    f"Functions defined: {', '.join(fns) or 'none'}."
                )

            # Merge overlapping context windows so a dense match doesn't repeat
            # the same lines several times over.
            windows: list[list[int]] = []
            for i in hits:
                lo = max(0, i - DEMO_SOURCE_CONTEXT_LINES)
                hi = min(len(lines), i + DEMO_SOURCE_CONTEXT_LINES + 1)
                if windows and lo <= windows[-1][1]:
                    windows[-1][1] = max(windows[-1][1], hi)
                else:
                    windows.append([lo, hi])

            chunks = []
            for lo, hi in windows:
                body = "\n".join(
                    f"{n + 1:4} | {lines[n]}" for n in range(lo, hi)
                )
                chunks.append(f"--- lines {lo + 1}-{hi} ---\n{body}")

            out = "\n\n".join(chunks)
            truncated = len(out) > MAX_DEMO_SOURCE_CHARS
            if truncated:
                out = out[:MAX_DEMO_SOURCE_CHARS] + "\n…[truncated — narrow your query]"

            return _text_result(
                f"{len(hits)} line(s) match {query!r} in {demo_id}:\n\n{out}"
            )
        except Exception as e:
            logger.error(f"get_demo_source error: {e}", exc_info=True)
            return _error_result(f"Error reading demo source: {e}")

    @tool(
        "search_demos",
        "Find demos this learner built earlier, by title or concept. Use it "
        "when they refer to a demo that is not listed in 'Demos in This "
        "Session' — e.g. 'that hashing demo from last week'.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Words from the title or concept. Omit to list recent demos.",
                },
            },
            "required": [],
        },
    )
    async def search_demos_tool(args):
        """Titles and ids only — deliberately never the HTML."""
        from backend.demos import list_demos

        try:
            email = _email_from_dir(user_data_dir)
            query = (args.get("query") or "").strip().lower()
            entries = list_demos(email)
            if query:
                terms = query.split()
                entries = [
                    e for e in entries
                    if any(
                        t in f"{e.get('title','')} {e.get('concept_id','')}".lower()
                        for t in terms
                    )
                ]
            return _text_result(json.dumps({
                "demos": [
                    {
                        "demo_id": e["id"],
                        "title": e["title"],
                        "concept_id": e.get("concept_id"),
                        "version": e.get("version", 1),
                    }
                    for e in entries[:20]
                ]
            }))
        except Exception as e:
            logger.error(f"search_demos error: {e}", exc_info=True)
            return _error_result(f"Error searching demos: {e}")

    @tool(
        "request_demo",
        "Ask the demo builder to construct an interactive demo in the "
        "background. Returns immediately — do NOT write any HTML yourself. "
        "Call this ONLY when the learner explicitly asked to see, visualise "
        "or interact with something. To change a demo that already exists, "
        "pass its base_demo_id.",
        {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short name, e.g. 'Gradient descent steps'",
                },
                "request": {
                    "type": "string",
                    "description": (
                        "What to build or change, in detail: what the learner "
                        "is confused about, which values matter, what they "
                        "should be able to manipulate."
                    ),
                },
                "concept_id": {
                    "type": "string",
                    "description": "Concept this demonstrates, e.g. 'gradient_descent'",
                },
                "base_demo_id": {
                    "type": "string",
                    "description": "Set ONLY when changing an existing demo.",
                },
            },
            "required": ["title", "request"],
        },
    )
    async def request_demo_tool(args):
        """Queue a background build and return a job id straight away.

        The whole point is that this returns in microseconds. Generation used
        to happen inline, which meant the learner's composer stayed disabled
        for the ~60s it took the model to write the HTML.
        """
        from backend.demojobs import start_demo_job

        if session_ctx is None:
            return _error_result(
                "request_demo is not available here. If you are the demo "
                "builder, call save_demo or update_demo directly."
            )

        try:
            email = _email_from_dir(user_data_dir)
            job = start_demo_job(
                email=email,
                chat_session_id=session_ctx.get("session_id"),
                title=args.get("title", ""),
                concept_id=args.get("concept_id", ""),
                request=args.get("request", ""),
                base_demo_id=(args.get("base_demo_id") or "").strip() or None,
                app=app,
            )
            return _text_result(json.dumps({
                "job_id": job["job_id"],
                "status": "building",
                "note": (
                    "Building in the background. Say ONE sentence about what "
                    "it will show, then carry on teaching — do not wait for "
                    "it and do not write any HTML. If the learner asks whether "
                    "it is done, call get_demo_status with this job_id: "
                    "search_demos cannot tell a failed build from a slow one."
                ),
            }))
        except Exception as e:
            logger.error(f"request_demo error: {e}", exc_info=True)
            return _error_result(f"Error requesting demo: {e}")

    @tool(
        "get_demo_status",
        "Check whether a demo requested with request_demo has finished. Pass "
        "the job_id request_demo returned. Use this — NOT search_demos — to "
        "answer 'is it done yet?': search_demos lists demos that already "
        "exist, so a build that FAILED looks exactly like one still running, "
        "and reporting 'still building' for a dead build leaves the learner "
        "waiting for something that is never coming.",
        {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job_id request_demo returned.",
                },
            },
            "required": ["job_id"],
        },
    )
    async def get_demo_status_tool(args):
        """Report one build's state, and say what to do about it.

        This exists because the tutor had no way to see a failed build. It
        polled search_demos, saw an empty list, and told the learner "still
        building" — three times, for a job that had already failed. An empty
        list genuinely cannot distinguish the two states; only the job record
        can, which is what this reads.
        """
        from backend.demojobs import get_job, BUILDING, QUEUED, READY, FAILED, CANCELLED

        try:
            email = _email_from_dir(user_data_dir)
            job_id = (args.get("job_id") or "").strip()
            if not job_id:
                return _error_result("job_id is required")

            # Scoped by email inside get_job, so one learner cannot read
            # another's build (including the prompt text they typed).
            record = get_job(email, job_id)
            if record is None:
                return _error_result(
                    f"No demo build found with job_id '{job_id}'. It may belong "
                    f"to a different learner, or the id may be wrong — check "
                    f"what request_demo returned."
                )

            state = record.get("state", "")
            payload = {"job_id": job_id, "state": state}

            if state in (QUEUED, BUILDING):
                payload["note"] = (
                    "Still building. Do NOT wait or poll in a loop — carry on "
                    "teaching and check again on a later turn."
                )
            elif state == READY:
                payload["demo_id"] = record.get("demo_id")
                payload["note"] = (
                    "Ready. Tell the learner it has landed and what to try "
                    "first."
                )
            elif state == FAILED:
                # The error is the whole point of this branch: without it the
                # tutor can see "failed" but not say anything useful about it.
                payload["error"] = record.get("error") or "unknown error"
                payload["note"] = (
                    "This build FAILED — it is not coming. Tell the learner "
                    "plainly rather than saying it is still building, and ask "
                    "whether to retry. If the error mentions an output limit, "
                    "request a SMALLER demo: one concept, not ten panels."
                )
            elif state == CANCELLED:
                payload["note"] = "This build was cancelled."

            return _text_result(json.dumps(payload, default=str))
        except Exception as e:
            logger.error(f"get_demo_status error: {e}", exc_info=True)
            return _error_result(f"Error checking demo status: {e}")

    # =====================================================================
    # Derivations
    # =====================================================================

    @tool(
        "request_derivation",
        "Ask the derivation builder to work out a piece of mathematics in the "
        "background: a multi-step derivation, a matrix being reduced, or "
        "anything that needs its steps shown and checked. Returns immediately "
        "— do NOT write the derivation yourself. Use this when a result is "
        "worth more than one or two lines of algebra; keep short formulas "
        "inline in chat. To revise one that exists, pass its base_doc_id.",
        {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short name, e.g. 'Gradient of logistic loss'",
                },
                "request": {
                    "type": "string",
                    "description": (
                        "What to derive, in detail: the result to reach, where "
                        "the learner is stuck, which step matters most, and "
                        "whether a plot would help."
                    ),
                },
                "concept_id": {
                    "type": "string",
                    "description": "Concept this belongs to, e.g. 'logistic_regression'",
                },
                "base_doc_id": {
                    "type": "string",
                    "description": "Set ONLY when revising an existing derivation.",
                },
            },
            "required": ["title", "request"],
        },
    )
    async def request_derivation_tool(args):
        """Queue a background build and return a job id straight away.

        Returns in microseconds, like request_demo and for the same reason: a
        derivation with per-step verification takes long enough that building
        it inline would freeze the learner's composer.
        """
        from backend.mathjobs import start_derivation_job

        if session_ctx is None:
            return _error_result(
                "request_derivation is not available here. If you are the "
                "derivation builder, call derivation_write on the doc_id your "
                "instructions name."
            )

        try:
            email = _email_from_dir(user_data_dir)
            job = start_derivation_job(
                email=email,
                chat_session_id=session_ctx.get("session_id"),
                title=args.get("title", ""),
                concept_id=args.get("concept_id", ""),
                request=args.get("request", ""),
                base_doc_id=(args.get("base_doc_id") or "").strip() or None,
            )
            return _text_result(json.dumps({
                "job_id": job["job_id"],
                "status": "building",
                "note": (
                    "Building in the background. Say ONE sentence about what "
                    "it will show, then carry on teaching — do not wait for it "
                    "and do not write the derivation yourself."
                ),
            }))
        except Exception as e:
            logger.error(f"request_derivation error: {e}", exc_info=True)
            return _error_result(f"Error requesting derivation: {e}")

    @tool(
        "derivation_write",
        "Append blocks to a derivation. Blocks ADD to what is already there, "
        "so call this several times as you work — intuition first, then the "
        "steps, then any table or matrix — and the learner watches it fill in. "
        "Write each wave as a JSON array to a file in your scratch directory "
        "and pass blocks_path. Builder only.",
        {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "The doc_id your instructions name",
                },
                "blocks_path": {
                    "type": "string",
                    "description": (
                        "PREFERRED. Path to a JSON file in your scratch "
                        "directory holding an array of blocks. The server "
                        "reads it from disk, so the wave never goes through "
                        "a tool call."
                    ),
                },
                "blocks": {
                    "type": "array",
                    "description": (
                        "Inline blocks — only for a small wave; use "
                        "blocks_path otherwise. Each has a 'kind': "
                        "'text' {content}, 'latex' {content}, "
                        "'derivation' {title, steps:[{expr, reason}]}, "
                        "'matrix' {label, rows, ops}, "
                        "'table' {headers, rows, caption}. "
                        "Every derivation step needs a reason naming WHY that "
                        "step follows. Brace function arguments: \\log{(x)} not "
                        "\\log(x), or the step cannot be verified."
                    ),
                    "items": {"type": "object"},
                },
            },
            "required": ["doc_id"],
        },
    )
    async def derivation_write_tool(args):
        # Refuse a payload big enough to take the channel down. See
        # agent.check_payload_size for why this is here and not in a hook.
        _too_big = check_payload_size("mcp__learning-tools__derivation_write", args)
        if _too_big:
            return _error_result(_too_big)

        from backend.derivations import append_blocks, from_builder, read_builder_blocks

        if build_ctx is None:
            return _error_result(
                "derivation_write is not available here. Call "
                "request_derivation(title, concept_id, request) instead."
            )
        try:
            email = _email_from_dir(user_data_dir)
            blocks_path = (args.get("blocks_path") or "").strip()
            blocks = (
                read_builder_blocks(blocks_path) if blocks_path
                else args.get("blocks") or []
            )
            if not blocks:
                return _error_result(
                    "No blocks to write. Put the wave in a JSON file in your "
                    "scratch directory and pass blocks_path."
                )
            # Nothing is reported back through build_ctx: over the HTTP
            # transport it is a per-request copy. The job runner reads the doc
            # itself once the builder exits — see mathjobs._run_build.
            doc = append_blocks(email, args.get("doc_id", ""), from_builder(blocks))
            if doc is None:
                return _error_result(f"No such derivation: {args.get('doc_id')}")

            unverified = [
                b["id"] for b in doc["blocks"]
                if b["kind"] == "derivation"
                and any(s.get("verified") is None and s.get("check") != "premise"
                        for s in b.get("steps") or [])
            ]
            payload = {
                "doc_id": doc["id"],
                "blocks": len(doc["blocks"]),
                "status": "written",
            }
            if unverified:
                payload["next"] = (
                    "Call derivation_verify on: " + ", ".join(unverified)
                )
            from backend.derivations import bare_math_warnings
            bare = bare_math_warnings(doc["blocks"])
            if bare:
                payload["fix_formatting"] = (
                    "Maths outside $$…$$ renders as raw text in the pane. Wrap each symbol or formula in these places in $$…$$ (e.g. $$w_1^2$$, $$\\|w\\|^2$$) and update the block: " + " | ".join(bare)
                )
            return _text_result(json.dumps(payload))
        except ValueError as e:
            return _error_result(f"Derivation rejected: {e}")
        except Exception as e:
            logger.error(f"derivation_write error: {e}", exc_info=True)
            return _error_result(f"Error writing derivation: {e}")

    @tool(
        "derivation_verify",
        "Check a derivation block's steps with sympy and stamp each one "
        "verified. Call this on every derivation block you write. A step that "
        "comes back false is WRONG — fix it, do not leave it on screen. "
        "Builder only.",
        {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "block_id": {
                    "type": "string",
                    "description": "The derivation block to check",
                },
            },
            "required": ["doc_id", "block_id"],
        },
    )
    async def derivation_verify_tool(args):
        from backend.derivations import get_doc, update_block
        from backend.mathverify import verify_steps

        if build_ctx is None:
            return _error_result("derivation_verify is not available here.")
        try:
            email = _email_from_dir(user_data_dir)
            doc = get_doc(email, args.get("doc_id", ""))
            if doc is None:
                return _error_result(f"No such derivation: {args.get('doc_id')}")

            block_id = args.get("block_id", "")
            block = next((b for b in doc["blocks"] if b["id"] == block_id), None)
            if block is None:
                return _error_result(f"No such block: {block_id}")
            if block["kind"] != "derivation":
                return _error_result(
                    f"Block {block_id} is a {block['kind']}, not a derivation"
                )

            checked = verify_steps(block.get("steps") or [])
            update_block(email, doc["id"], block_id, steps=checked)

            wrong = [
                {"step": i + 1, "expr": s["expr"][:120], "why": s["check"][:160]}
                for i, s in enumerate(checked) if s["verified"] is False
            ]
            # Each unchecked step with its own reason. A blanket "usually
            # unbraced functions" note used to go here; it was wrong for
            # equations, definitions and matrix notation alike, and a builder
            # spent four rewrites of a correct derivation chasing braces.
            unchecked = [
                {"step": i + 1, "why": s["check"][:120]}
                for i, s in enumerate(checked)
                if s["verified"] is None and i > 0
            ]
            payload = {
                "verified": sum(1 for s in checked if s["verified"] is True),
                "wrong": wrong,
                "unchecked": unchecked,
            }
            if wrong:
                payload["action"] = (
                    "These steps do not follow. Fix them with "
                    "get_derivation_json + Edit + derivation_update, then call "
                    "derivation_verify again."
                )
            elif unchecked:
                payload["note"] = (
                    "Unchecked is not wrong. Rewrite a step only when its why "
                    "says ambiguous notation — brace function arguments, "
                    "\\log{(x)} not \\log(x). Leave the rest."
                )
            return _text_result(json.dumps(payload))
        except Exception as e:
            logger.error(f"derivation_verify error: {e}", exc_info=True)
            return _error_result(f"Error verifying derivation: {e}")

    @tool(
        "derivation_read",
        "Read a derivation's blocks back, before revising it. Builder only.",
        {
            "type": "object",
            "properties": {"doc_id": {"type": "string"}},
            "required": ["doc_id"],
        },
    )
    async def derivation_read_tool(args):
        from backend.derivations import get_doc

        if build_ctx is None:
            return _error_result("derivation_read is not available here.")
        try:
            email = _email_from_dir(user_data_dir)
            doc = get_doc(email, args.get("doc_id", ""))
            if doc is None:
                return _error_result(f"No such derivation: {args.get('doc_id')}")
            return _text_result(json.dumps({
                "doc_id": doc["id"],
                "title": doc.get("title"),
                "blocks": doc.get("blocks") or [],
            }))
        except Exception as e:
            logger.error(f"derivation_read error: {e}", exc_info=True)
            return _error_result(f"Error reading derivation: {e}")

    @tool(
        "get_derivation_json",
        "Check out a derivation you are about to correct. Copies its blocks "
        "into your scratch directory as JSON and returns the PATH — not the "
        "blocks. Find what to change with Grep/Read, patch it with Edit, then "
        "publish with derivation_update(doc_id, blocks_path=...). Builder only.",
        {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "The derivation to check out"},
            },
            "required": ["doc_id"],
        },
    )
    async def get_derivation_json_tool(args):
        """Copy a derivation into scratch and return where it landed.

        The derivation twin of get_demo_html. Appending was the only write a
        builder had, so a wrong block could only be followed by a corrected
        copy — a live build left the same derivation on the page four times.
        """
        from backend.derivations import copy_doc_to_scratch

        if build_ctx is None:
            return _error_result("get_derivation_json is not available here.")
        try:
            email = _email_from_dir(user_data_dir)
            dest = build_ctx.get("scratch_dir")
            if not dest:
                return _error_result(
                    "No scratch directory for this build, so the derivation "
                    "cannot be checked out."
                )
            path = copy_doc_to_scratch(email, args.get("doc_id", ""), dest)
            return _text_result(json.dumps({
                "path": path,
                "next": (
                    "Grep or Read this file to find the block, patch it with "
                    "Edit, then call derivation_update(doc_id, blocks_path) "
                    "with this same path. Delete a block by removing it from "
                    "the array. Keep each block's \"id\"."
                ),
            }))
        except ValueError as e:
            return _error_result(str(e))
        except Exception as e:
            logger.error(f"get_derivation_json error: {e}", exc_info=True)
            return _error_result(f"Error checking out derivation: {e}")

    @tool(
        "derivation_update",
        "Replace a derivation's blocks with the file you checked out and "
        "edited. The file is the WHOLE document, in order: a block you removed "
        "is deleted, a block you changed is replaced where it stands. Changed "
        "derivation blocks lose their verification — call derivation_verify "
        "on the ids this returns. Builder only.",
        {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "blocks_path": {
                    "type": "string",
                    "description": "The file get_derivation_json returned, after your edits",
                },
            },
            "required": ["doc_id", "blocks_path"],
        },
    )
    async def derivation_update_tool(args):
        from backend.derivations import read_builder_blocks, replace_blocks

        if build_ctx is None:
            return _error_result("derivation_update is not available here.")
        try:
            email = _email_from_dir(user_data_dir)
            blocks = read_builder_blocks(args.get("blocks_path", ""))
            if not blocks:
                return _error_result(
                    "The file holds no blocks. A derivation cannot be emptied; "
                    "remove only the blocks that should go."
                )
            doc, needs_verify = replace_blocks(email, args.get("doc_id", ""), blocks)
            payload = {
                "doc_id": doc["id"],
                "blocks": len(doc["blocks"]),
                "status": "updated",
            }
            if needs_verify:
                payload["next"] = "Call derivation_verify on: " + ", ".join(needs_verify)
            from backend.derivations import bare_math_warnings
            bare = bare_math_warnings(doc["blocks"])
            if bare:
                payload["fix_formatting"] = (
                    "Maths outside $$…$$ renders as raw text in the pane. Wrap each symbol or formula in these places in $$…$$ (e.g. $$w_1^2$$, $$\\|w\\|^2$$) and update the block: " + " | ".join(bare)
                )
            return _text_result(json.dumps(payload))
        except ValueError as e:
            return _error_result(f"Derivation rejected: {e}")
        except Exception as e:
            logger.error(f"derivation_update error: {e}", exc_info=True)
            return _error_result(f"Error updating derivation: {e}")

    @tool(
        "derivation_plot",
        "Add a plot to a derivation from a figure a run_code cell just saved. "
        "Run the matplotlib code first with run_code (save as .svg), then pass "
        "the run_id and the filename here. Builder only.",
        {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "run_id": {
                    "type": "string",
                    "description": "The run_id whose figure to attach",
                },
                "filename": {
                    "type": "string",
                    "description": "Figure filename from that run, e.g. '_plot_001.svg'",
                },
                "caption": {"type": "string", "description": "One line: what it shows"},
            },
            "required": ["doc_id", "run_id", "filename"],
        },
    )
    async def derivation_plot_tool(args):
        """Copy a figure out of scratch and into the document.

        The copy is the point: run artifacts live under a swept scratch dir with
        an hour TTL and do not survive a reboot, so a doc that merely linked one
        would render a hole by tomorrow.
        """
        from backend.derivations import add_plot
        from backend.sandbox import run_dir_for

        if build_ctx is None:
            return _error_result("derivation_plot is not available here.")
        try:
            email = _email_from_dir(user_data_dir)
            run_id = (args.get("run_id") or "").strip()
            # Shape _new_run_dir produces. The id reaches here from the model,
            # so a traversal attempt must never become a path.
            if not re.match(r"^run_\d+_[a-f0-9]+$", run_id):
                return _error_result(f"Invalid run_id: {run_id!r}")
            filename = os.path.basename(args.get("filename", ""))
            source = os.path.join(str(run_dir_for(email, run_id)), filename)
            if not os.path.isfile(source):
                return _error_result(
                    f"No figure {filename!r} in run {args.get('run_id')!r}. "
                    "Check the run saved it, and that the run has not expired."
                )
            doc = add_plot(email, args.get("doc_id", ""), source,
                           caption=args.get("caption", ""))
            return _text_result(json.dumps({
                "doc_id": doc["id"],
                "blocks": len(doc["blocks"]),
                "status": "plot added",
            }))
        except ValueError as e:
            return _error_result(f"Plot rejected: {e}")
        except Exception as e:
            logger.error(f"derivation_plot error: {e}", exc_info=True)
            return _error_result(f"Error adding plot: {e}")

    # =====================================================================
    # Create MCP Server
    # =====================================================================

    # =====================================================================
    # Sanskrit correctness tools
    #
    # These exist for one reason: the tutor writes plausible Sanskrit, and a
    # declension that is wrong in the instrumental plural teaches a false
    # paradigm more convincingly than no paradigm at all. Worse than the
    # algebra case — a learner can check an equation by substituting numbers,
    # but cannot check रामैः except by already knowing it.
    #
    # Every one returns a tri-state `verified`. None means "could not check"
    # and must never be presented as agreement.
    # =====================================================================

    @tool(
        "derive_form",
        "Derive a Sanskrit verb form from its root using Pāṇinian rules, and "
        "return the sūtra chain that produced it. Use this BEFORE stating any "
        "conjugated form. Also the way to answer 'why is it X and not Y' — the "
        "steps are the explanation. Roots may be given as IAST (bhū, paṭh), "
        "Devanagari, or Dhātupāṭha code. If the root is not recognised this "
        "says so rather than guessing: the library will happily invent a form "
        "from a typo.",
        {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Verb root, e.g. 'bhū', 'paṭh', 'kṛ'"},
                "lakara": {
                    "type": "string",
                    "description": (
                        "Tense/mood: Lat (present), Lrt (future), Lan (imperfect), "
                        "Lot (imperative), VidhiLin (optative), Lit (perfect), Lun (aorist)"
                    ),
                },
                "purusha": {
                    "type": "string",
                    "description": "Prathama (3rd), Madhyama (2nd), Uttama (1st)",
                },
                "vacana": {"type": "string", "description": "Eka, Dvi, or Bahu"},
                "prayoga": {
                    "type": "string",
                    "description": "Kartari (active), Karmani (passive), Bhave",
                },
            },
            "required": ["root"],
        },
    )
    async def derive_form_tool(args):
        try:
            from backend.sanskritverify import derive_verb

            res = derive_verb(
                root=args.get("root", ""),
                lakara=args.get("lakara") or "Lat",
                purusha=args.get("purusha") or "Prathama",
                vacana=args.get("vacana") or "Eka",
                prayoga=args.get("prayoga") or "Kartari",
            )
            # Sūtra chains run to ~20 steps and every tool result is re-sent on
            # every later turn, so the chain is trimmed to the steps that
            # actually changed the form. The full one is rarely the lesson.
            steps = res.get("steps") or []
            if len(steps) > MAX_SUTRA_STEPS:
                kept, seen = [], None
                for st in steps:
                    if st["result"] != seen:
                        kept.append(st)
                        seen = st["result"]
                steps = kept[-MAX_SUTRA_STEPS:]
                res["steps_note"] = "showing the steps that changed the form"
            res["steps"] = steps
            return _text_result(json.dumps(res, ensure_ascii=False, default=str))
        except Exception as e:
            logger.error(f"derive_form error: {e}", exc_info=True)
            return _error_result(f"Error deriving form: {e}")

    @tool(
        "decline",
        "Generate the full 8x3 declension table for a Sanskrit nominal stem, "
        "in the case order Indian textbooks print. Use this BEFORE stating any "
        "case form. Give the BARE stem (rāma, not rāmaḥ; nadī, not nadīm).",
        {
            "type": "object",
            "properties": {
                "stem": {"type": "string", "description": "Bare stem, e.g. 'rāma', 'nadī', 'phala'"},
                "gender": {
                    "type": "string",
                    "description": "m (masculine), f (feminine), or n (neuter)",
                },
            },
            "required": ["stem", "gender"],
        },
    )
    async def decline_tool(args):
        try:
            from backend.sanskritverify import decline

            res = decline(args.get("stem", ""), args.get("gender", "m"))
            return _text_result(json.dumps(res, ensure_ascii=False, default=str))
        except Exception as e:
            logger.error(f"decline error: {e}", exc_info=True)
            return _error_result(f"Error declining stem: {e}")

    @tool(
        "identify_metre",
        "Scan a Sanskrit verse: split it into syllables and mark each one "
        "laghu (L) or guru (G), naming the metre where it can. Use this BEFORE "
        "saying anything about metre or rhythm, and when the learner is about "
        "to recite — the weights are what tell them where the beat falls.",
        {
            "type": "object",
            "properties": {
                "verse": {"type": "string", "description": "The verse, in Devanagari or IAST"},
            },
            "required": ["verse"],
        },
    )
    async def identify_metre_tool(args):
        try:
            from backend.sanskritverify import scan_metre

            return _text_result(json.dumps(
                scan_metre(args.get("verse", "")), ensure_ascii=False, default=str))
        except Exception as e:
            logger.error(f"identify_metre error: {e}", exc_info=True)
            return _error_result(f"Error scanning verse: {e}")

    @tool(
        "transliterate_sanskrit",
        "Convert Sanskrit between Devanagari, IAST and SLP1. Use it to show a "
        "learner the same word in both scripts, or to read something they "
        "typed in Latin letters.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The text to convert"},
                "to": {
                    "type": "string",
                    "enum": ["devanagari", "iast", "slp1"],
                    "description": "Target scheme. The source is detected.",
                },
            },
            "required": ["text", "to"],
        },
    )
    async def transliterate_sanskrit_tool(args):
        try:
            from backend.sanskritverify import detect_scheme, to_devanagari, to_slp1

            text = args.get("text", "")
            target = (args.get("to") or "devanagari").lower()
            source = detect_scheme(text)

            slp = to_slp1(text)
            if slp is None:
                return _error_result(
                    "Could not read that text. Is vidyut installed? "
                    "Run `python -m backend.sanskrit_setup --check`."
                )

            if target == "slp1":
                out = slp
            elif target == "devanagari":
                out = to_devanagari(slp)
            else:
                from vidyut.lipi import Scheme, transliterate as _tr
                out = _tr(slp, Scheme.Slp1, Scheme.Iast)

            return _text_result(json.dumps(
                {"source": source, "target": target, "text": out},
                ensure_ascii=False))
        except Exception as e:
            logger.error(f"transliterate_sanskrit error: {e}", exc_info=True)
            return _error_result(f"Error transliterating: {e}")

    @tool(
        "analyse_sanskrit",
        "Split continuous Sanskrit into words and identify each one — the "
        "sandhi-resolution step that is the hardest part of reading. Use this "
        "BEFORE claiming where word boundaries fall in a phrase the learner is "
        "working through. Needs either the vidyut data bundle or the network; "
        "it says which when it cannot run.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Continuous Sanskrit, Devanagari or IAST"},
            },
            "required": ["text"],
        },
    )
    async def analyse_sanskrit_tool(args):
        try:
            from backend.sanskritsegment import segment

            res = segment(args.get("text", ""))
            return _text_result(json.dumps(res, ensure_ascii=False, default=str))
        except Exception as e:
            logger.error(f"analyse_sanskrit error: {e}", exc_info=True)
            return _error_result(f"Error analysing text: {e}")

    @tool(
        "lookup_word",
        "Look up a Sanskrit word: its stem, morphology and meaning. Use this "
        "BEFORE glossing a word the learner asks about. Reports what it could "
        "not find rather than inventing a plausible gloss.",
        {
            "type": "object",
            "properties": {
                "word": {"type": "string", "description": "The word, Devanagari or IAST"},
            },
            "required": ["word"],
        },
    )
    async def lookup_word_tool(args):
        try:
            from backend.sanskritsegment import lookup

            res = lookup(args.get("word", ""))
            return _text_result(json.dumps(res, ensure_ascii=False, default=str))
        except Exception as e:
            logger.error(f"lookup_word error: {e}", exc_info=True)
            return _error_result(f"Error looking up word: {e}")

    # =================================================================
    # Reader — turning the pages of a book the learner has open
    #
    # The tutor SEES the pages: they ride into the turn as image blocks
    # whenever the position has moved (see backend/books.py needs_resend).
    # These tools move the position; the pane follows, because the position
    # lives on the server rather than in the client.
    #
    # None of them return page content. A tool result is re-sent on every
    # later turn, so answering "here is page 38" in text would put the page
    # into the transcript permanently AND duplicate what the images already
    # carry. The result says where the reader now is; the images arrive next
    # turn.
    # =================================================================

    def _resolve_book(args) -> tuple[str, dict | None]:
        """(email, record) for the book a tool call refers to.

        A missing book_id means "the one they have open", which is what the
        tutor will usually intend: there is normally one book, and making the
        model carry an id it has to remember is a needless failure mode.
        """
        from backend.books import active_book, get_book

        email = _email_from_dir(user_data_dir)
        book_id = (args.get("book_id") or "").strip()
        return email, (get_book(email, book_id) if book_id else active_book(email))

    def _position_result(email: str, record: dict, moved_to: int) -> dict:
        from backend.books import MAX_WINDOW_PAGES_TO_MODEL

        pos = record["position"]
        shown = min(pos["window"], MAX_WINDOW_PAGES_TO_MODEL)
        last = min(pos["page"] + shown - 1, record["page_count"])
        span = f"page {pos['page']}" if last == pos["page"] else f"pages {pos['page']}–{last}"
        return {
            "ok": True,
            "book_id": record["book_id"],
            "title": record["title"],
            "page": pos["page"],
            "window": pos["window"],
            "page_count": record["page_count"],
            # Without this the model describes the page it has just turned to,
            # because turning felt like looking. It has not seen it yet.
            "note": (
                f"The reader pane has moved to {span}. You will SEE these pages "
                "in the learner's next message — do not describe them yet."
            ),
        }

    @tool(
        "go_to_page",
        "Turn the reader pane to a specific page of the book the learner has "
        "open. The pane on their screen moves to match, and you are shown the "
        "new pages with their next message. Use this when they ask to go "
        "somewhere particular ('back a bit', 'chapter 3'). Page numbers are the "
        "PDF's own sequence starting at 1, which in a scanned book is usually "
        "offset from the printed page number.",
        {
            "type": "object",
            "properties": {
                "page": {"type": "integer", "description": "PDF page number, 1-based"},
                "book_id": {
                    "type": "string",
                    "description": "Omit to use the book they already have open",
                },
                "window": {
                    "type": "integer",
                    "description": "How many pages to show at once (1-4). Omit to keep the current setting.",
                },
                "reason": {
                    "type": "string",
                    "description": "One short phrase for the learner, e.g. 'on to the sandhi rules'",
                },
            },
            "required": ["page"],
        },
    )
    async def go_to_page_tool(args):
        try:
            from backend.books import set_position

            email, record = _resolve_book(args)
            if record is None:
                return _error_result(
                    "No book is open. Ask the learner to add one, or call "
                    "open_book with a URL if they named a text."
                )

            page = int(args.get("page") or 1)
            count = record["page_count"]
            if page < 1 or page > count:
                # Name the valid range: a bare "invalid page" makes the model
                # retry blind.
                return _error_result(
                    f"Page {page} is outside \"{record['title']}\", which has "
                    f"{count} pages. Pick a page between 1 and {count}."
                )

            window = args.get("window")
            updated = set_position(
                email, record["book_id"], page,
                max(1, min(int(window), 4)) if window else None,
            )
            if updated is None:
                return _error_result("That book could not be opened.")
            return _text_result(json.dumps(
                _position_result(email, updated, page), ensure_ascii=False))
        except Exception as e:
            logger.error(f"go_to_page error: {e}", exc_info=True)
            return _error_result(f"Error turning the page: {e}")

    @tool(
        "advance_pages",
        "Move forward through the book by whole pages — the ordinary rhythm of "
        "reading, 'that's this page done, let's go on'. Pass a negative number "
        "to go back. Omit `by` to advance by exactly what is on screen. Stops "
        "at the start and end of the book rather than failing.",
        {
            "type": "object",
            "properties": {
                "by": {
                    "type": "integer",
                    "description": "Pages to move; negative goes back. Omit for one screenful.",
                },
                "book_id": {"type": "string", "description": "Omit to use the open book"},
                "reason": {
                    "type": "string",
                    "description": "One short phrase for the learner, e.g. 'on to the next verse'",
                },
            },
            "required": [],
        },
    )
    async def advance_pages_tool(args):
        try:
            from backend.books import set_position

            email, record = _resolve_book(args)
            if record is None:
                return _error_result(
                    "No book is open. Ask the learner to add one, or call "
                    "open_book with a URL if they named a text."
                )

            pos = record["position"]
            by = args.get("by")
            step = int(by) if by not in (None, "") else pos.get("window", 1)
            target = pos["page"] + step

            # Clamped by set_position rather than refused: running off either
            # end of a book is a normal event in a reading session.
            at_end = target > record["page_count"]
            updated = set_position(email, record["book_id"], target)
            if updated is None:
                return _error_result("That book could not be opened.")

            result = _position_result(email, updated, target)
            if at_end:
                result["note"] = (
                    f"That is the end of \"{record['title']}\" — the pane is at "
                    f"the last page ({record['page_count']}). Tell the learner "
                    "they have finished it."
                )
            return _text_result(json.dumps(result, ensure_ascii=False))
        except Exception as e:
            logger.error(f"advance_pages error: {e}", exc_info=True)
            return _error_result(f"Error moving through the book: {e}")

    @tool(
        "open_book",
        "Open a book for reading from a URL you found. Give a direct PDF link "
        "or an archive.org page — archive.org is by far the best source for "
        "out-of-copyright Sanskrit texts and grammars, and its books resolve "
        "reliably. Google Books usually does NOT offer a downloadable PDF and "
        "will be refused; when that happens, search archive.org for the same "
        "title instead. The download runs in the background: say one sentence "
        "about what you are opening and carry on teaching — do not wait for it, "
        "and do not promise to read from it until it is ready.",
        {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "https:// URL of a PDF, or an archive.org details page",
                },
                "title": {
                    "type": "string",
                    "description": "What to call it in the learner's library. Omit to use the source's own title.",
                },
            },
            "required": ["url"],
        },
    )
    async def open_book_tool(args):
        try:
            from backend.bookjobs import start_book_job

            url = (args.get("url") or "").strip()
            if not url:
                return _error_result("open_book needs a url.")

            email = _email_from_dir(user_data_dir)
            record = start_book_job(email, url, args.get("title") or "")
            return _text_result(json.dumps({
                "job_id": record["job_id"],
                "status": record["state"],
                "note": (
                    "The download is running in the background. Tell the learner "
                    "in one sentence what you are opening, then carry on with "
                    "what you were teaching. It will appear in their reader when "
                    "it is ready; you will not see the pages until they write "
                    "again after that."
                ),
            }, ensure_ascii=False))
        except Exception as e:
            logger.error(f"open_book error: {e}", exc_info=True)
            return _error_result(f"Error opening that book: {e}")

    @tool(
        "list_books",
        "The books this learner has, and which one is open. Check this before "
        "offering to fetch a text — they may already have it.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def list_books_tool(args):
        try:
            from backend.books import active_book, list_books

            email = _email_from_dir(user_data_dir)
            books = list_books(email)
            open_book = active_book(email)
            return _text_result(json.dumps({
                "books": [
                    {
                        "book_id": b["book_id"],
                        "title": b["title"],
                        "page_count": b["page_count"],
                        "position": b.get("position"),
                    }
                    for b in books
                ],
                "open": open_book["book_id"] if open_book else None,
            }, ensure_ascii=False))
        except Exception as e:
            logger.error(f"list_books error: {e}", exc_info=True)
            return _error_result(f"Error listing books: {e}")

    # Grouped by family so an app can take or leave a whole capability. The
    # groups are also the documentation: what "demos" means is this list, not a
    # comment that can drift from it.
    families = {
        "core": [
            # Episodic memory (ChromaDB)
            search_past_conversations_tool,
            save_conversation_summary_tool,
            save_image_memory_tool,
            get_memory_status_tool,
            # Semantic memory / knowledge graph
            get_learning_progress_tool,
            update_concept_tool,
            suggest_next_topics_tool,
            get_domain_map_tool,
            # Quizzing and the learner profile
            get_quiz_topics_tool,
            record_quiz_result_tool,
            update_learner_profile_tool,
            generate_quiz_question_tool,
            get_feedback_tool,
        ],
        "code": [
            run_code_tool,
            get_code_result_tool,
        ],
        "demos": [
            request_demo_tool,      # tutor
            get_demo_status_tool,   # tutor
            search_demos_tool,      # tutor
            get_demo_source_tool,   # tutor
            save_demo_tool,         # builder only
            demo_open_tool,         # builder only — split path
            demo_write_part_tool,   # builder only — split path
            demo_finalize_tool,     # builder only — split path
            demo_verify_tool,       # builder only — self-check
            update_demo_tool,       # builder only
            get_demo_html_tool,     # builder only
        ],
        "sanskrit": [
            derive_form_tool,
            decline_tool,
            identify_metre_tool,
            transliterate_sanskrit_tool,
            analyse_sanskrit_tool,
            lookup_word_tool,
        ],
        # All tutor-facing: turning a page is instant and belongs inside the
        # turn that decides to turn it, so there is no builder half here.
        "reader": [
            go_to_page_tool,
            advance_pages_tool,
            open_book_tool,
            list_books_tool,
        ],
        "derivations": [
            request_derivation_tool,  # tutor
            derivation_write_tool,    # builder only
            derivation_verify_tool,   # builder only
            derivation_read_tool,     # builder only
            get_derivation_json_tool, # builder only
            derivation_update_tool,   # builder only
            derivation_plot_tool,     # builder only
        ],
    }

    selected = app_tool_families(app)
    unknown = set(selected) - set(families)
    if unknown:
        raise ValueError(
            f"App '{app}' asks for tool families that do not exist: "
            f"{sorted(unknown)}. Known: {sorted(families)}."
        )

    enabled = [t for name in selected for t in families[name]]
    logger.debug(
        "Tools for app '%s': %d across families %s",
        app, len(enabled), list(selected),
    )

    # The HTTP transport registers each tool itself and never needs the SDK
    # server object; returning early also avoids logging a build it will not use.
    if _return_tools:
        return enabled

    server = create_sdk_mcp_server(
        name="learning-tools",
        version="1.0.0",
        tools=enabled,
    )
    # Identity of the server this run will use. The CLI is handed only
    # {"type": "sdk", "name": ...} — the instance is stripped — and must call
    # back over the control protocol to reach it. When those callbacks fail the
    # tool result says only "Stream closed", which cannot distinguish "the
    # server was never built" from "it was built but never reached". Logging
    # the instance id here, against the same id at call time, tells them apart.
    # Hand the id to the handlers' closure cell so an entry line can name the
    # same server this line names. Comparing the two is what tells "never
    # reached" from "reached a stale one".
    mcp_instance_id[0] = id(server.get("instance"))
    logger.info(
        "MCP server built [%s]: %d tools, instance=%x, role=%s",
        app, len(enabled), mcp_instance_id[0],
        "builder" if build_ctx is not None else "tutor",
    )
    return server


def build_tool_list(
    user_data_dir: str,
    episodic: EpisodicMemory,
    session_ctx: dict | None = None,
    build_ctx: dict | None = None,
    app: str = DEFAULT_APP,
) -> list:
    """The same tools create_learning_tools assembles, as a plain list.

    For the HTTP transport (backend/mcp_http.py), which registers each tool
    itself. create_sdk_mcp_server returns only {type, name, instance} and hides
    the tools inside the Server object; rather than reach into its internals —
    or bolt an extra key onto a TypedDict the SDK serialises toward the CLI —
    ask the factory for the list. Each SdkMcpTool already carries the name,
    description, input_schema and handler a transport needs.

    Shares create_learning_tools rather than duplicating the family filtering
    and the builder gate: those decide which tools exist for an app and whether
    build_ctx unlocks the builder-only ones, and two copies would drift.
    """
    return create_learning_tools(
        user_data_dir, episodic, session_ctx, build_ctx, app=app,
        _return_tools=True,
    )


# =====================================================================
# Helpers
# =====================================================================

def _email_from_dir(user_data_dir: str) -> str:
    """Extract email from user data directory path (last segment)."""
    return os.path.basename(user_data_dir)


def prose_ratio(code: str) -> float:
    """Fraction of the cell that is English rather than program.

    Counts string-literal contents and comment text against total length.

    Why a ratio and not just computes_nothing(): once pure-prose cells were
    refused, the model started appending a real computation to the bottom of
    the same essay. A 250-line cell whose last 40 lines fit a RandomForest is
    not a verification — it is a lecture with a fig leaf, and the whole thing
    lands in the transcript forever.

    Measured across a real session: legitimate calls run 11-34% prose, the
    abusive ones 71-99%. There is no overlap, which is what makes a threshold
    honest here rather than arbitrary.

    Comments are counted because ast discards them, so text moved from a
    docstring into `#` lines would otherwise slip through untouched — the
    obvious next place for prose to hide.
    """
    if not code.strip():
        return 0.0
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Let the sandbox report it; a broken cell is a real result.
        return 0.0

    prose = sum(
        len(n.value) for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            prose += len(stripped)
    return prose / len(code)


def computes_nothing(code: str) -> bool:
    """True if this cell can only echo text the model already wrote.

    Guards a real misuse: asked to check a learner's understanding of entropy,
    the tutor sent 22 print() calls containing a hand-written essay, ran it,
    and read its own words back. Nothing was verified — a subprocess used as a
    text formatter — and the output then sat in the transcript being re-sent
    on every later turn. Four such calls in one session cost ~25KB.

    "Only print statements" is too narrow: the essay had its text parked in
    variables first. The real property is that NOTHING is computed — no
    imports, no control flow, no calls but print, no attribute or subscript
    access. Under those limits the output is a pure function of literals the
    model typed, so running it cannot teach the model anything.

    Deliberately conservative. A cell doing real work trips one of these
    checks immediately, so a false positive would need code that computes
    nothing at all — which is exactly the case being refused. Verified against
    a full session: 4/18 calls flagged, every genuine computation untouched.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Let the sandbox report the syntax error; that is a real result.
        return False
    if not tree.body:
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.For, ast.AsyncFor, ast.While, ast.If, ast.Try,
                             ast.With, ast.AsyncWith, ast.comprehension)):
            return False
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                continue
            return False
        if isinstance(node, (ast.Subscript, ast.Attribute)):
            return False
        # An f-string placeholder evaluates something: f"{2+2}" is a result
        # the model has not seen.
        if isinstance(node, ast.FormattedValue):
            return False
        # Numeric arithmetic is a result the model has not seen; string
        # building is not. `"\n" + "=" * 80` is banner punctuation and stays
        # allowed, while `6 * 7` makes the cell a genuine question.
        if isinstance(node, ast.BinOp) and not _is_string_building(node):
            return False
    return True


def _is_string_building(node: ast.AST) -> bool:
    """True for expressions that only concatenate or repeat string literals.

    Recursive because banners nest: `"\\n" + "=" * 80` is an Add whose right
    side is a Mult. Anything numeric fails here and is treated as computation.
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.BinOp):
        left, right = node.left, node.right
        if isinstance(node.op, ast.Add):
            return _is_string_building(left) and _is_string_building(right)
        if isinstance(node.op, ast.Mult):
            # "=" * 80 or 80 * "=", but never 6 * 7.
            str_side = _is_string_building(left) or _is_string_building(right)
            int_side = (isinstance(left, ast.Constant)
                        and isinstance(left.value, int)) or (
                        isinstance(right, ast.Constant)
                        and isinstance(right.value, int))
            return str_side and int_side
    return False


def _clip_output(text: str, limit: int = MAX_RUN_OUTPUT_CHARS) -> str:
    """Trim run output, keeping both ends.

    A traceback puts its one useful line last, so a head-only truncation would
    reliably discard the reason the run failed.

    `limit` lets the caller shrink this further to fit a budget — run_code
    sizes it against what is left of the transcript allowance, so bulk output
    can never displace the fields the model has to act on.
    """
    if not text or len(text) <= limit:
        return text
    half = limit // 2
    dropped = len(text) - (half * 2)
    return f"{text[:half]}\n... [{dropped} chars trimmed] ...\n{text[-half:]}"


def _text_result(text: str) -> dict:
    """Format a success result for MCP tool response."""
    return {"content": [{"type": "text", "text": text}]}


def _error_result(text: str) -> dict:
    """Format an error result for MCP tool response."""
    return {"content": [{"type": "text", "text": text}], "is_error": True}
