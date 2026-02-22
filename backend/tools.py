"""MCP tool definitions for the Python Learning Agent.

8 tools across Tier 2 (episodic) and Tier 3 (semantic) memory:

Episodic (ChromaDB):
  - search_past_conversations: Semantic search over past discussion summaries
  - save_conversation_summary: Store a conversation summary with metadata

Semantic (JSON knowledge graph):
  - get_learning_progress: Read full knowledge state
  - update_concept: Add/update concept mastery
  - suggest_next_topics: Prerequisite-aware topic suggestions
  - get_quiz_topics: Get best concepts to quiz on
  - record_quiz_result: Save quiz results + promote mastery
  - update_learner_profile: Update learner level/preferences/notes
"""

import json
import logging
import os
from datetime import datetime, timezone

from claude_agent_sdk import tool, create_sdk_mcp_server

from backend.episodic import EpisodicMemory
from backend.knowledge import KnowledgeStore

logger = logging.getLogger(__name__)


def create_learning_tools(user_data_dir: str, episodic: EpisodicMemory):
    """Create in-process MCP tools for the learning agent.

    Args:
        user_data_dir: Path to the user's data directory (data/users/<email>/)
        episodic: Shared EpisodicMemory instance

    Returns:
        McpSdkServerConfig for use in ClaudeAgentOptions.mcp_servers
    """
    knowledge = KnowledgeStore(user_data_dir)

    # =====================================================================
    # TIER 2: Episodic Memory Tools (ChromaDB)
    # =====================================================================

    @tool(
        "search_past_conversations",
        "Search past conversation summaries by semantic similarity. "
        "Use this to recall what was discussed in previous sessions — "
        "e.g., 'what did we cover about list comprehensions?' or "
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

            # Extract email from the user_data_dir path
            email = _email_from_dir(user_data_dir)
            results = episodic.search_episodes(email, query, n_results=n)

            if not results:
                return _text_result(json.dumps({
                    "results": [],
                    "message": "No past conversations found matching your query.",
                }))

            return _text_result(json.dumps({
                "results": results,
                "count": len(results),
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
                    "description": "Category: fundamentals, control_flow, data_structures, functions, oop, error_handling, file_io, modules, advanced",
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
        "Analyze the learner's knowledge graph and suggest what Python concepts to learn next. "
        "Considers prerequisite chains, current mastery levels, and natural learning progression.",
        {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of suggestions to return (default: 3)",
                },
            },
            "required": [],
        },
    )
    async def suggest_next_topics_tool(args):
        try:
            count = args.get("count", 3)
            suggestions = knowledge.get_next_topic_suggestions(count)

            if not suggestions:
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
            },
            "required": ["quiz_id", "topics", "questions", "score", "total"],
        },
    )
    async def record_quiz_result_tool(args):
        try:
            result = knowledge.record_quiz(args)
            return _text_result(json.dumps({
                "status": "recorded",
                "mastery_changes": result["mastery_changes"],
                "quiz_summary": {
                    "score": args["score"],
                    "total": args["total"],
                    "percentage": result["quiz_entry"]["percentage"],
                },
                "message": "Quiz results saved and mastery levels updated.",
            }, default=str))
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
    # Create MCP Server
    # =====================================================================

    return create_sdk_mcp_server(
        name="learning-tools",
        version="1.0.0",
        tools=[
            search_past_conversations_tool,
            save_conversation_summary_tool,
            save_image_memory_tool,
            get_learning_progress_tool,
            update_concept_tool,
            suggest_next_topics_tool,
            get_quiz_topics_tool,
            record_quiz_result_tool,
            update_learner_profile_tool,
        ],
    )


# =====================================================================
# Helpers
# =====================================================================

def _email_from_dir(user_data_dir: str) -> str:
    """Extract email from user data directory path (last segment)."""
    return os.path.basename(user_data_dir)


def _text_result(text: str) -> dict:
    """Format a success result for MCP tool response."""
    return {"content": [{"type": "text", "text": text}]}


def _error_result(text: str) -> dict:
    """Format an error result for MCP tool response."""
    return {"content": [{"type": "text", "text": text}], "is_error": True}
