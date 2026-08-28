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
from backend.knowledge import CURRICULUM_GRAPH, TRACK_ORDER, KnowledgeStore

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


def create_learning_tools(
    user_data_dir: str,
    episodic: EpisodicMemory,
    session_ctx: dict | None = None,
    build_ctx: dict | None = None,
):
    """Create in-process MCP tools for the learning agent.

    Args:
        user_data_dir: Path to the user's data directory (data/users/<email>/)
        episodic: Shared EpisodicMemory instance
        session_ctx: Caller-owned dict that run_agent_internal fills with the
            live session id. request_demo needs it to know which conversation
            to fork, and the id does not exist when this factory is called.
            None for the demo builder, which disables request_demo — a builder
            must not be able to request another build.
        build_ctx: Caller-owned dict for a demo build: carries the ids to stamp
            onto saved demos, and receives back the demo_id that was written so
            the job runner can tell whether the build actually produced one.

    Returns:
        McpSdkServerConfig for use in ClaudeAgentOptions.mcp_servers
    """
    knowledge = KnowledgeStore(user_data_dir)

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
        "Get the learner's complete Python learning progress: all concepts covered, "
        "mastery levels, learning path, quiz stats, and suggested next topics. "
        "Use this for a detailed view beyond what's in the system prompt.",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    )
    async def get_learning_progress_tool(args):
        try:
            data = knowledge.load_knowledge()
            quiz_history = knowledge.load_quiz_history()
            suggestions = knowledge.get_next_topic_suggestions(5)

            result = {
                "profile": data.get("profile", {}),
                "concepts": data.get("concepts", {}),
                "total_concepts": len(data.get("concepts", {})),
                "learning_path_length": len(data.get("learning_path", [])),
                "quiz_stats": quiz_history.get("stats", {}),
                "suggested_next_topics": suggestions,
            }
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
        try:
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
                if track not in TRACK_ORDER:
                    return _error_result(
                        f"Unknown track '{track}'. Valid tracks: "
                        f"{', '.join(TRACK_ORDER)}."
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
            },
            "required": ["track"],
        },
    )
    async def get_domain_map_tool(args):
        try:
            track = str(args.get("track", "")).strip().lower()
            if track not in TRACK_ORDER:
                return _error_result(
                    f"Unknown track '{track}'. Valid tracks: "
                    f"{', '.join(TRACK_ORDER)}."
                )
            return _text_result(json.dumps(
                knowledge.get_domain_map(track), default=str
            ))
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
                "html": {
                    "type": "string",
                    "description": (
                        "Complete self-contained HTML document. Inline all CSS "
                        "and JS. No CDN links, external fonts, or remote images."
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
            "required": ["title", "html"],
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
                concept_id=args.get("concept_id", ""),
                summary=args.get("summary", ""),
                # Stamped by the job runner, which knows both ids. This is what
                # makes the demo refinable later.
                builder_session_id=build.get("builder_session_id", ""),
                chat_session_id=build.get("chat_session_id", ""),
            )
            build["demo_id"] = meta["id"]
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
            missing = missing_element_ids(args.get("html", ""))
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
        "get_demo_html",
        "Read the current HTML of a demo you are about to change. Call this "
        "before update_demo so you edit the existing demo rather than "
        "rewriting it from memory.",
        {
            "type": "object",
            "properties": {
                "demo_id": {"type": "string", "description": "The demo to read"},
            },
            "required": ["demo_id"],
        },
    )
    async def get_demo_html_tool(args):
        """Return a demo's HTML. Builder-only — never in the tutor's allowlist.

        The tutor must not call this: a demo can be up to MAX_DEMO_BYTES, and
        pulling that into the chat transcript would re-send it to the model on
        every subsequent turn.
        """
        from backend.demos import get_demo

        # Builder-only: a demo can be up to MAX_DEMO_BYTES, and pulling that
        # into the tutor's transcript would re-send it on every later turn.
        if build_ctx is None:
            return _error_result("get_demo_html is only available to the demo builder.")

        try:
            email = _email_from_dir(user_data_dir)
            demo = get_demo(email, args.get("demo_id", ""))
            if demo is None:
                return _error_result(f"No demo with id {args.get('demo_id')!r}")
            return _text_result(demo["html"])
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
                "html": {
                    "type": "string",
                    "description": (
                        "Complete replacement HTML document, self-contained. "
                        "Keep the parts that already worked."
                    ),
                },
                "title": {"type": "string", "description": "Optional new title"},
                "summary": {
                    "type": "string",
                    "description": "One line on what changed and what to try",
                },
            },
            "required": ["demo_id", "html"],
        },
    )
    async def update_demo_tool(args):
        """Edit a demo in place so refinement does not churn the demo list."""
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
                title=args.get("title", ""),
                summary=args.get("summary", ""),
                builder_session_id=build.get("builder_session_id", ""),
            )
            if meta is None:
                return _error_result(f"No demo with id {args.get('demo_id')!r}")
            build["demo_id"] = meta["id"]
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
        """Slice a demo's source around a query. Tutor-only.

        The builder has get_demo_html for the whole document; the tutor must
        never pull that in, because everything in a tool result stays in the
        SDK transcript and is re-sent on every later turn of the session. A
        17KB demo would cost ~4200 tokens per turn for the rest of the
        conversation. The char cap below is the real protection — it is
        enforced here and no prompt can talk its way past it.
        """
        from backend.demos import _read_json, _html_path, _meta_path, _ID_ATTR, _FN_DECL

        if session_ctx is None:
            return _error_result(
                "get_demo_source is not available here. If you are the demo "
                "builder, call get_demo_html for the full document."
            )

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
            path = _html_path(email, demo_id)
            if not os.path.isfile(path):
                return _error_result(f"No source on disk for {demo_id!r}")
            with open(path, encoding="utf-8") as f:
                html = f.read()

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
            )
            return _text_result(json.dumps({
                "job_id": job["job_id"],
                "status": "building",
                "note": (
                    "Building in the background. Say ONE sentence about what "
                    "it will show, then carry on teaching — do not wait for "
                    "it and do not write any HTML."
                ),
            }))
        except Exception as e:
            logger.error(f"request_demo error: {e}", exc_info=True)
            return _error_result(f"Error requesting demo: {e}")

    # =====================================================================
    # Create MCP Server
    # =====================================================================

    return create_sdk_mcp_server(
        name="learning-tools",
        version="1.0.0",
        tools=[
            search_past_conversations_tool,
            save_conversation_summary_tool,
            save_image_memory_tool,
            get_memory_status_tool,
            get_learning_progress_tool,
            update_concept_tool,
            suggest_next_topics_tool,
            get_domain_map_tool,
            get_quiz_topics_tool,
            record_quiz_result_tool,
            update_learner_profile_tool,
            generate_quiz_question_tool,
            get_feedback_tool,
            run_code_tool,
            get_code_result_tool,
            save_demo_tool,
            update_demo_tool,
            get_demo_html_tool,
            search_demos_tool,
            get_demo_source_tool,
            request_demo_tool,
        ],
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
