"""Tier 3: Semantic Memory — structured knowledge graph per user.

Tracks Python concepts, mastery levels, prerequisites, and quiz history.
Stored as JSON files in data/users/<email>/.
"""

import os
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Mastery levels in progression order
MASTERY_LEVELS = ["introduced", "practiced", "mastered"]

# Canonical Python curriculum with prerequisite chains
CURRICULUM_GRAPH = {
    "variables": {"name": "Variables", "prereqs": [], "category": "fundamentals"},
    "data_types": {"name": "Data Types", "prereqs": ["variables"], "category": "fundamentals"},
    "operators": {"name": "Operators", "prereqs": ["variables", "data_types"], "category": "fundamentals"},
    "string_formatting": {"name": "String Formatting", "prereqs": ["variables", "data_types"], "category": "fundamentals"},
    "input_output": {"name": "Input/Output", "prereqs": ["variables"], "category": "fundamentals"},
    "if_else": {"name": "If/Else Conditionals", "prereqs": ["operators"], "category": "control_flow"},
    "for_loops": {"name": "For Loops", "prereqs": ["if_else"], "category": "control_flow"},
    "while_loops": {"name": "While Loops", "prereqs": ["if_else"], "category": "control_flow"},
    "match_statement": {"name": "Match Statement", "prereqs": ["if_else"], "category": "control_flow"},
    "lists": {"name": "Lists", "prereqs": ["variables", "for_loops"], "category": "data_structures"},
    "tuples": {"name": "Tuples", "prereqs": ["lists"], "category": "data_structures"},
    "dictionaries": {"name": "Dictionaries", "prereqs": ["lists"], "category": "data_structures"},
    "sets": {"name": "Sets", "prereqs": ["lists"], "category": "data_structures"},
    "list_comprehensions": {"name": "List Comprehensions", "prereqs": ["lists", "for_loops"], "category": "data_structures"},
    "functions_basic": {"name": "Functions (Basic)", "prereqs": ["variables", "if_else"], "category": "functions"},
    "functions_args": {"name": "Function Arguments", "prereqs": ["functions_basic"], "category": "functions"},
    "scope": {"name": "Variable Scope", "prereqs": ["functions_basic"], "category": "functions"},
    "lambda_functions": {"name": "Lambda Functions", "prereqs": ["functions_basic"], "category": "functions"},
    "closures": {"name": "Closures", "prereqs": ["scope", "functions_args"], "category": "functions"},
    "decorators": {"name": "Decorators", "prereqs": ["closures"], "category": "functions"},
    "classes_basic": {"name": "Classes (Basic)", "prereqs": ["functions_basic", "dictionaries"], "category": "oop"},
    "inheritance": {"name": "Inheritance", "prereqs": ["classes_basic"], "category": "oop"},
    "dunder_methods": {"name": "Dunder Methods", "prereqs": ["classes_basic"], "category": "oop"},
    "error_handling": {"name": "Error Handling", "prereqs": ["functions_basic"], "category": "error_handling"},
    "custom_exceptions": {"name": "Custom Exceptions", "prereqs": ["error_handling", "classes_basic"], "category": "error_handling"},
    "file_reading": {"name": "File Reading", "prereqs": ["error_handling"], "category": "file_io"},
    "file_writing": {"name": "File Writing", "prereqs": ["file_reading"], "category": "file_io"},
    "context_managers": {"name": "Context Managers", "prereqs": ["file_reading", "classes_basic"], "category": "file_io"},
    "modules_imports": {"name": "Modules & Imports", "prereqs": ["functions_basic"], "category": "modules"},
    "packages": {"name": "Packages", "prereqs": ["modules_imports"], "category": "modules"},
    "virtual_environments": {"name": "Virtual Environments", "prereqs": ["packages"], "category": "modules"},
    "generators": {"name": "Generators", "prereqs": ["for_loops", "functions_args"], "category": "advanced"},
    "iterators": {"name": "Iterators", "prereqs": ["classes_basic", "for_loops"], "category": "advanced"},
    "async_await": {"name": "Async/Await", "prereqs": ["generators", "error_handling"], "category": "advanced"},
    "type_hints": {"name": "Type Hints", "prereqs": ["functions_args", "classes_basic"], "category": "advanced"},
}


def _default_knowledge() -> dict:
    """Return empty knowledge structure."""
    return {
        "profile": {
            "level": "beginner",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "last_active": datetime.now(timezone.utc).isoformat(),
            "total_concepts": 0,
            "struggle_areas": [],
            "strengths": [],
            "preferences": "",
        },
        "concepts": {},
        "learning_path": [],
    }


def _default_quiz_history() -> dict:
    """Return empty quiz history structure."""
    return {
        "quizzes": [],
        "stats": {
            "total_quizzes": 0,
            "total_questions": 0,
            "total_correct": 0,
            "overall_accuracy": 0.0,
        },
    }


class KnowledgeStore:
    """Read/write access to a user's knowledge.json and quiz_history.json."""

    def __init__(self, user_data_dir: str):
        self.user_data_dir = user_data_dir
        self.knowledge_path = os.path.join(user_data_dir, "knowledge.json")
        self.quiz_path = os.path.join(user_data_dir, "quiz_history.json")
        os.makedirs(user_data_dir, exist_ok=True)

    def load_knowledge(self) -> dict:
        if not os.path.isfile(self.knowledge_path):
            return _default_knowledge()
        with open(self.knowledge_path) as f:
            return json.load(f)

    def save_knowledge(self, data: dict):
        with open(self.knowledge_path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def load_quiz_history(self) -> dict:
        if not os.path.isfile(self.quiz_path):
            return _default_quiz_history()
        with open(self.quiz_path) as f:
            return json.load(f)

    def save_quiz_history(self, data: dict):
        with open(self.quiz_path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def upsert_concept(
        self,
        concept_id: str,
        name: str,
        category: str,
        mastery: str,
        prerequisites: list[str] | None = None,
        related: list[str] | None = None,
        notes: str = "",
    ) -> dict:
        """Add or update a concept. Returns the updated concept dict."""
        data = self.load_knowledge()
        now = datetime.now(timezone.utc).isoformat()

        existing = data["concepts"].get(concept_id)
        if existing:
            # Update existing concept
            old_mastery = existing.get("mastery", "introduced")
            existing["mastery"] = mastery
            existing["last_reviewed"] = now
            existing["review_count"] = existing.get("review_count", 0) + 1
            if notes:
                existing["notes"] = notes
            if prerequisites is not None:
                existing["prerequisites"] = prerequisites
            if related is not None:
                existing["related"] = related
            concept = existing
            action = "promoted" if MASTERY_LEVELS.index(mastery) > MASTERY_LEVELS.index(old_mastery) else "reviewed"
        else:
            # New concept
            concept = {
                "concept_id": concept_id,
                "name": name,
                "category": category,
                "mastery": mastery,
                "first_seen": now,
                "last_reviewed": now,
                "review_count": 1,
                "prerequisites": prerequisites or [],
                "related": related or [],
                "notes": notes,
            }
            data["concepts"][concept_id] = concept
            action = "introduced"

        # Update learning path
        data["learning_path"].append({
            "concept": concept_id,
            "timestamp": now,
            "action": action,
            "mastery": mastery,
        })

        # Update profile
        data["profile"]["last_active"] = now
        data["profile"]["total_concepts"] = len(data["concepts"])

        self.save_knowledge(data)
        logger.info(f"Concept '{concept_id}' {action} at mastery={mastery}")
        return concept

    def get_quiz_candidates(self, count: int = 5, category: str | None = None) -> list[dict]:
        """Get concepts ranked by quiz priority.

        Prioritizes: introduced > practiced > mastered, then by staleness (oldest first).
        """
        data = self.load_knowledge()
        concepts = list(data["concepts"].values())

        if category:
            concepts = [c for c in concepts if c.get("category") == category]

        # Sort by mastery level (lower = higher priority) then by last_reviewed (older first)
        mastery_priority = {"introduced": 0, "practiced": 1, "mastered": 2}
        concepts.sort(key=lambda c: (
            mastery_priority.get(c.get("mastery", "introduced"), 0),
            c.get("last_reviewed", ""),
        ))

        return concepts[:count]

    def get_next_topic_suggestions(self, count: int = 3) -> list[dict]:
        """Suggest next topics based on the curriculum graph.

        Finds concepts whose prerequisites are all met (at least 'introduced')
        but which haven't been covered yet.
        """
        data = self.load_knowledge()
        known_concepts = set(data["concepts"].keys())

        suggestions = []
        for concept_id, info in CURRICULUM_GRAPH.items():
            if concept_id in known_concepts:
                continue
            prereqs = info["prereqs"]
            if all(p in known_concepts for p in prereqs):
                suggestions.append({
                    "concept_id": concept_id,
                    "name": info["name"],
                    "category": info["category"],
                    "prerequisites_met": prereqs,
                    "reason": f"Prerequisites met: {', '.join(prereqs)}" if prereqs else "No prerequisites needed",
                })

        # Prioritize fundamentals first, then by category depth
        category_priority = {
            "fundamentals": 0, "control_flow": 1, "data_structures": 2,
            "functions": 3, "oop": 4, "error_handling": 5,
            "file_io": 6, "modules": 7, "advanced": 8,
        }
        suggestions.sort(key=lambda s: category_priority.get(s["category"], 99))
        return suggestions[:count]

    def record_quiz(self, quiz_data: dict) -> dict:
        """Save quiz results and promote mastery for correct answers.

        quiz_data: {quiz_id, topics, questions: [{question, user_answer, correct, concept}], score, total}

        Returns dict with mastery_changes.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Save quiz to history
        history = self.load_quiz_history()
        quiz_entry = {
            "quiz_id": quiz_data.get("quiz_id", f"q_{now}"),
            "timestamp": now,
            "topics": quiz_data.get("topics", []),
            "questions": quiz_data.get("questions", []),
            "score": quiz_data.get("score", 0),
            "total": quiz_data.get("total", 0),
            "percentage": round(quiz_data["score"] / quiz_data["total"] * 100, 1) if quiz_data.get("total") else 0,
        }
        history["quizzes"].append(quiz_entry)

        # Update stats
        stats = history["stats"]
        stats["total_quizzes"] += 1
        stats["total_questions"] += quiz_data.get("total", 0)
        stats["total_correct"] += quiz_data.get("score", 0)
        stats["overall_accuracy"] = round(
            stats["total_correct"] / stats["total_questions"] * 100, 1
        ) if stats["total_questions"] else 0.0
        self.save_quiz_history(history)

        # Promote mastery for correct answers
        data = self.load_knowledge()
        mastery_changes = []

        for q in quiz_data.get("questions", []):
            concept_id = q.get("concept")
            if not concept_id or concept_id not in data["concepts"]:
                continue

            concept = data["concepts"][concept_id]
            old_mastery = concept.get("mastery", "introduced")

            if q.get("correct"):
                # Promote mastery
                old_idx = MASTERY_LEVELS.index(old_mastery) if old_mastery in MASTERY_LEVELS else 0
                new_idx = min(old_idx + 1, len(MASTERY_LEVELS) - 1)
                new_mastery = MASTERY_LEVELS[new_idx]
                if new_mastery != old_mastery:
                    concept["mastery"] = new_mastery
                    mastery_changes.append({
                        "concept": concept_id,
                        "from": old_mastery,
                        "to": new_mastery,
                    })
            else:
                # Note the struggle area
                if concept_id not in data["profile"].get("struggle_areas", []):
                    data["profile"].setdefault("struggle_areas", []).append(concept_id)

            concept["last_reviewed"] = now

        # Update learning path for quiz
        data["learning_path"].append({
            "concept": "quiz",
            "timestamp": now,
            "action": "quiz_completed",
            "mastery": f"score: {quiz_data.get('score', 0)}/{quiz_data.get('total', 0)}",
        })

        self.save_knowledge(data)
        return {"mastery_changes": mastery_changes, "quiz_entry": quiz_entry}

    def format_for_system_prompt(self) -> str:
        """Format the current knowledge state as a condensed string for system prompt injection."""
        data = self.load_knowledge()
        profile = data.get("profile", {})
        concepts = data.get("concepts", {})

        if not concepts:
            return (
                "== LEARNER'S KNOWLEDGE ==\n"
                f"Level: {profile.get('level', 'beginner')}\n"
                "No concepts tracked yet — this is a new learner."
            )

        # Group by mastery level
        grouped = {"mastered": [], "practiced": [], "introduced": []}
        for cid, c in concepts.items():
            mastery = c.get("mastery", "introduced")
            grouped.setdefault(mastery, []).append(c)

        lines = [
            "== LEARNER'S KNOWLEDGE ==",
            f"Level: {profile.get('level', 'beginner')} | Concepts: {len(concepts)}",
        ]

        if profile.get("struggle_areas"):
            lines.append(f"Struggles with: {', '.join(profile['struggle_areas'])}")
        if profile.get("strengths"):
            lines.append(f"Strong in: {', '.join(profile['strengths'])}")

        for level in ["mastered", "practiced", "introduced"]:
            items = grouped.get(level, [])
            if items:
                lines.append(f"\n{level.upper()} ({len(items)}):")
                for c in items:
                    ts = c.get("last_reviewed", "")[:10]
                    lines.append(f"  - {c['name']} ({c.get('category', '')}) — reviewed {ts}")
                    if c.get("notes"):
                        lines.append(f"    Notes: {c['notes']}")

        # Suggest next topics
        suggestions = self.get_next_topic_suggestions(3)
        if suggestions:
            lines.append(f"\nSUGGESTED NEXT: {', '.join(s['name'] for s in suggestions)}")

        return "\n".join(lines)
