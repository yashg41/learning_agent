"""Tier 3: Semantic Memory — structured knowledge graph per user.

Tracks Python concepts, mastery levels, prerequisites, and quiz history.
Stored as JSON files in data/users/<email>/.
"""

import asyncio
import os
import json
import logging
import tempfile
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# One lock per knowledge file, keyed on its path rather than held on the store.
# A KnowledgeStore is constructed per request in tools.py, agent.py and several
# routes, so a per-instance lock would guard nothing — two handlers touching the
# same user hold two different instances of the same file. Single uvicorn
# process, so an asyncio.Lock is sufficient; this only orders coroutines on one
# event loop, which is exactly where the interleaving happens. Mirrors
# sandbox._lock_for, which solves the same hazard for venvs.
_knowledge_locks: dict[str, asyncio.Lock] = {}


def knowledge_lock(path: str) -> asyncio.Lock:
    """The lock guarding read-modify-write of one knowledge file."""
    key = os.path.abspath(path)
    # No await between the check and the insert, so this stays atomic under
    # asyncio without a second lock around the dict.
    if key not in _knowledge_locks:
        _knowledge_locks[key] = asyncio.Lock()
    return _knowledge_locks[key]

# Mastery levels in progression order
MASTERY_LEVELS = ["introduced", "practiced", "mastered"]


def _mastery_rank(level: str | None) -> int:
    """Position of a mastery level, tolerant of values not in MASTERY_LEVELS.

    A bare MASTERY_LEVELS.index() raises ValueError on anything else, and this
    is called on the mastery already stored on disk — a hand-edited file, or
    one written before a level was renamed, is enough to make it throw. The
    raise happened while comparing old against new, i.e. after the concept was
    already mutated in memory but before the save, so it lost the write it was
    in the middle of AND took down every sibling call in the same batch.
    Unknown values sort lowest, so an odd level reads as a promotion rather
    than silently looking like a demotion.
    """
    try:
        return MASTERY_LEVELS.index(level)
    except ValueError:
        return -1


def _atomic_write_json(path: str, data: dict) -> None:
    """Write JSON so a concurrent reader never sees a half-written file.

    open(path, "w") truncates on open and only then writes, which leaves a
    window where the file on disk is empty or partial. These stores are
    read-modify-written by async tool handlers that interleave on one event
    loop, so a reader landing in that window got a JSONDecodeError. Writing to
    a temp file in the SAME directory (so it is on the same filesystem) and
    then os.replace makes the swap atomic: a reader sees either the old file
    or the new one, never a torn one.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
            # The rename is atomic, but only orders against data already
            # handed to the OS; flush so the bytes exist before the swap.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Never leave a .tmp-*.json behind for the next reader to trip over.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# The curriculum itself lives in backend/curriculum/*.yaml, one file per track,
# and is loaded by backend.curriculum. It used to be ~285 lines of dict literals
# right here — which meant the subject matter and the graph algorithms that walk
# it could only be edited in the same file. Everything below this line works the
# same whatever the subject is; everything about *which* subject is in the YAML.
#
# Re-exported under the original names because tools.py, routes.py and the tests
# import them from this module.
from backend.curriculum import (  # noqa: E402
    APP_TRACKS,
    CURRICULUM_GRAPH,
    CATEGORY_PRIORITY,
    CATEGORY_LABELS,
    TRACK_HUE,
    TRACK_NAMES,
    TRACK_ORDER,
    app_tracks,
)

# CATEGORY_PRIORITY (imported above) sorts candidates *within* one track's
# bucket in get_next_topic_suggestions; cross-track ordering is the
# round-robin interleave, not this. Categories absent from it fall through to
# 99, so a category the tutor invents still sorts predictably.

# Visual palette for the frontend graph view. Single source of truth —
# routes.py imports from here so colors stay co-located with the curriculum.
#
# Each track lives in its own hue family so the force-directed view shows
# clear color clusters: Python = blues, ML = oranges, DL = cyans/teals,
# LLM = purples/violets, Ops = greens, Quantum = hot magentas/pinks.
CATEGORY_COLORS = {
    # Python — blues
    "fundamentals": "#7aa2f7",
    "control_flow": "#5d8ee5",
    "data_structures": "#3d7be5",
    "functions": "#89b4fa",
    "oop": "#6a9af0",
    "error_handling": "#4f8de8",
    "file_io": "#85a9f0",
    "modules": "#4488d8",
    "testing": "#74a0f4",
    "data_processing": "#4f7ed8",
    "advanced": "#a8c1f9",
    # ML — oranges/ambers
    "ml_core": "#f5a623",
    "ml_supervised": "#ffb454",
    "ml_ensembles": "#ffc978",
    "ml_unsupervised": "#e08e2a",
    "ml_eval": "#d97706",
    # DL — cyans/teals
    "dl_basics": "#56b6c2",
    "dl_architectures": "#22c1d4",
    # LLM — purples/violets
    "llm_core": "#a277ff",
    "llm_rag": "#c678dd",
    "llm_tuning": "#8b5cf6",
    # Ops — greens
    "ops_serving": "#98c379",
    "ops_monitoring": "#5fb37c",
    # Quantum — hot magentas/pinks
    "qc_foundations": "#ff4dca",
    "qc_qiskit": "#ff1f8f",
    "qc_algorithms": "#ff66a8",
    "qc_advanced": "#d61c87",
    # System design — warm reds/corals (distinct from ML ambers and QC pinks)
    "sd_foundations": "#ff8a7a",
    "sd_principles": "#f7768e",
    "sd_components": "#e5484d",
    "sd_data": "#c94f4f",
    "sd_distributed": "#ff6b6b",
    "sd_architecture": "#b3364a",
}

# Representative color per track. Used by the graph view to paint pie-ring
# slices on "bridge" concepts (concepts whose dependents span multiple
# tracks). Pick the most saturated category color from each track family
# so slices read clearly even when small.
TRACK_COLORS = {
    "python":    "#7aa2f7",
    "ml":        "#f5a623",
    "dl":        "#56b6c2",
    "llm":       "#c678dd",
    "ops":       "#98c379",
    "quantum":   "#ff1f8f",
    "sysdesign": "#e5484d",
}

# TRACK_NAMES (imported above) is how each track is named in prose. The tutor's
# identity line is built from it rather than restated in the prompt, so a track
# added to the curriculum cannot leave the prompt naming a different subject.


def taught_domains(app: str = "pymentor") -> str:
    """The subjects ONE tutor covers, as an English list.

    Derived from the curriculum so the identity prompt and the knowledge graph
    can never disagree — which is the point, and was also the bug: before this
    took an `app`, it walked every loaded track, so adding the Sanskrit track
    made PyMentor introduce itself as teaching "... system design, and
    Sanskrit". A tutor must only ever name its own tracks.

    PyMentor's phrasing leads with Python and presents the rest as what builds
    on it, matching how those tracks are actually sequenced. A single-track app
    just names its subject; stringing "and the topics that build on it" onto
    one track would read as though something were missing.
    """
    tracks = app_tracks(app)
    names = [TRACK_NAMES.get(t, t) for t in tracks]

    if len(names) == 1:
        return names[0]

    lead, rest = names[0], names[1:]
    tail = f"{', '.join(rest[:-1])}, and {rest[-1]}" if len(rest) > 1 else rest[0]
    return f"{lead} and the topics that build on it: {tail}"

# --- Derived palette (OKLCH) ------------------------------------------------
# 31 independently-chosen category hues are indistinguishable on screen. Since
# every category belongs to exactly one track, colour is *derived* instead:
# one hue per track, and each category is a lightness/chroma step within that
# hue. The System Design cluster then reads as one family of red rather than
# six unrelated colours, and adding a category can never invent a new hue.
#
# OKLCH is used because its lightness is perceptually uniform — equal steps
# look equally different, which plain HSL does not give you.

# TRACK_HUE (imported above) is one hue angle per track, spaced around the wheel
# so tracks stay mutually distinguishable. Chroma and lightness are set per
# theme below, not in the curriculum — a hue that glows on black is thin and
# washed out on white.

# Per-theme rendering targets.
#
# Dark: high lightness, high chroma — nodes read as light on a near-black
# canvas. Light: lower lightness so the same nodes read as ink on paper.
#
# The category ramp runs in OPPOSITE directions per theme. Foundational
# categories are the lighter end in dark mode; inverting the whole scheme
# would make them white-on-white in light mode, so light mode ramps the
# other way (foundational = lighter but still ink-dark enough to see).
THEME_RAMP = {
    "dark":  {"l_start": 80.0, "l_end": 58.0, "chroma": 0.155},
    "light": {"l_start": 62.0, "l_end": 42.0, "chroma": 0.145},
}


def _category_track_map() -> dict[str, str]:
    """Map each category id to its (single) owning track."""
    mapping: dict[str, str] = {}
    for info in CURRICULUM_GRAPH.values():
        mapping.setdefault(info["category"], info.get("track", "python"))
    return mapping


def build_oklch_palette(theme: str = "dark") -> dict:
    """Derive per-track and per-category OKLCH colours for one theme.

    Categories are ordered by CATEGORY_PRIORITY within their track, so the
    lightness ramp follows the actual learning progression rather than
    alphabetical accident.

    Returns {"tracks": {track: css}, "categories": {category: css}}.
    """
    ramp = THEME_RAMP.get(theme, THEME_RAMP["dark"])
    l_start, l_end, chroma = ramp["l_start"], ramp["l_end"], ramp["chroma"]

    cat_track = _category_track_map()
    by_track: dict[str, list[str]] = {}
    for cat, track in cat_track.items():
        by_track.setdefault(track, []).append(cat)

    # Track swatches sit mid-ramp so they represent the family as a whole.
    mid = (l_start + l_end) / 2
    tracks_css = {
        track: f"oklch({mid:.1f}% {chroma:.3f} {hue})"
        for track, hue in TRACK_HUE.items()
    }

    categories_css = {}
    for track, cats in by_track.items():
        hue = TRACK_HUE.get(track, TRACK_HUE["python"])
        cats.sort(key=lambda c: (CATEGORY_PRIORITY.get(c, 99), c))
        n = len(cats)
        for i, cat in enumerate(cats):
            # Single-category tracks sit mid-ramp rather than at an extreme.
            t = 0.5 if n == 1 else i / (n - 1)
            lightness = l_start + (l_end - l_start) * t
            # Deeper shades carry slightly more chroma so they don't read as
            # muddy versions of the light end.
            c = chroma * (0.85 + 0.3 * t)
            categories_css[cat] = f"oklch({lightness:.1f}% {c:.3f} {hue})"

    return {"tracks": tracks_css, "categories": categories_css}

# CATEGORY_LABELS (imported above) carries the legend labels the curriculum
# spells out. Naive `cat.replace("_", " ").title()` mangles the abbreviated
# prefixes ("Ml Core", "Qc Qiskit", "Sd Data"), so a track's YAML names those
# explicitly. A category with no label falls back to title-casing below, which
# is what a category the tutor invents gets.


def category_label(category: str) -> str:
    """Display name for a category id, falling back to title-casing."""
    return CATEGORY_LABELS.get(category, category.replace("_", " ").title())


# Palette for categories the tutor invents. The built-in tracks each own a hue
# family (see CATEGORY_COLORS); these are deliberately outside those families
# so a learner-invented category never looks like it belongs to Python or ML.
CUSTOM_CATEGORY_COLORS = [
    "#e0af68", "#d4a5c8", "#9ece6a", "#f7768e", "#7dcfff",
    "#bb9af7", "#e69875", "#73daca", "#c0caf5", "#ff9e64",
]


def _auto_label(category: str) -> str:
    """Human-readable label for a category id the curriculum never defined.

    Mirrors the "Track · Topic" shape of CATEGORY_LABELS where the id carries a
    known track prefix, so an invented `ml_ensembles` reads as "ML · Ensembles"
    beside the built-in ML entries rather than as "Ml Ensembles".
    """
    prefixes = {
        "ml": "ML", "dl": "DL", "llm": "LLM", "ops": "MLOps",
        "qc": "Quantum", "sd": "System Design",
    }
    head, _, tail = category.partition("_")
    if tail and head in prefixes:
        return f"{prefixes[head]} · {tail.replace('_', ' ').title()}"
    return category.replace("_", " ").title()


def _auto_color(category: str) -> str:
    """Stable color for an invented category.

    Hashed rather than assigned in arrival order so the same category keeps its
    color across learners and across a knowledge file being rebuilt — the graph
    view would otherwise reshuffle colors whenever concepts were re-added.
    """
    idx = sum(ord(c) for c in category) % len(CUSTOM_CATEGORY_COLORS)
    return CUSTOM_CATEGORY_COLORS[idx]


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

    def __init__(self, user_data_dir: str, app: str = "pymentor"):
        """
        app: which tutor this store serves. Bounds every curriculum walk to
            that tutor's tracks, so the Python tutor cannot suggest a Sanskrit
            concept and vice versa. The stored knowledge.json is shared — one
            learner, one progress file — but what gets *offered* is scoped.
        """
        self.user_data_dir = user_data_dir
        self.app = app
        self.tracks = frozenset(app_tracks(app))
        self.knowledge_path = os.path.join(user_data_dir, "knowledge.json")
        self.quiz_path = os.path.join(user_data_dir, "quiz_history.json")
        os.makedirs(user_data_dir, exist_ok=True)

    def _in_scope(self, concept_id: str) -> bool:
        """Is this concept one this tutor teaches?

        A concept the tutor invented is not in CURRICULUM_GRAPH at all; those
        stay in scope, because the tutor that created one is the tutor that
        should keep seeing it.
        """
        info = CURRICULUM_GRAPH.get(concept_id)
        return info is None or info.get("track") in self.tracks

    def load_knowledge(self) -> dict:
        if not os.path.isfile(self.knowledge_path):
            return _default_knowledge()
        with open(self.knowledge_path) as f:
            return json.load(f)

    def save_knowledge(self, data: dict):
        _atomic_write_json(self.knowledge_path, data)

    def load_quiz_history(self) -> dict:
        if not os.path.isfile(self.quiz_path):
            return _default_quiz_history()
        with open(self.quiz_path) as f:
            return json.load(f)

    def save_quiz_history(self, data: dict):
        _atomic_write_json(self.quiz_path, data)

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

        # Register a category the curriculum never defined. The tutor teaches
        # past the fixed curriculum — a session on gradient boosting produces
        # concepts no CURRICULUM_* dict anticipated — and a category with no
        # label and no color renders as an unnamed grey node. Recording it here
        # means the graph view can name and colour it like any built-in.
        self._register_category(data, category)

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
            action = "promoted" if _mastery_rank(mastery) > _mastery_rank(old_mastery) else "reviewed"
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

    @staticmethod
    def _register_category(data: dict, category: str) -> None:
        """Record a category that is not part of the built-in curriculum.

        Mutates `data` in place; the caller saves. Built-in categories are
        skipped, so this only ever grows with what the tutor actually invents.
        Uses setdefault throughout: knowledge files written before this existed
        have no custom_categories key, and re-registering must not overwrite a
        label the learner may later have edited.
        """
        if not category or category in CATEGORY_LABELS or category in CATEGORY_COLORS:
            return
        customs = data.setdefault("custom_categories", {})
        if category not in customs:
            customs[category] = {
                "label": _auto_label(category),
                "color": _auto_color(category),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            logger.info(f"Registered new category '{category}'")

    def categories(self) -> dict:
        """Every category this learner has, built-in and invented.

        Returns {id: {"label", "color"}} — what the graph legend needs, without
        the caller having to know which categories came from the curriculum.
        """
        out = {
            cat: {"label": category_label(cat), "color": color}
            for cat, color in CATEGORY_COLORS.items()
        }
        for cat, meta in self.load_knowledge().get("custom_categories", {}).items():
            out[cat] = {
                "label": meta.get("label") or _auto_label(cat),
                "color": meta.get("color") or _auto_color(cat),
            }
        return out

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

    def get_next_topic_suggestions(
        self, count: int = 3, track: str | None = None
    ) -> list[dict]:
        """Suggest next topics, mixing tracks so cross-domain topics surface early.

        Finds concepts whose prerequisites are all met, buckets them by track
        (python/ml/dl/llm/ops), then interleaves: primary track first, then
        one candidate from each other track whose bucket is non-empty, then
        back to primary — repeating until `count` candidates are returned.

        `track` restricts the walk to a single track. Without it, a learner who
        asks about one domain gets a cross-track interleave that mostly answers
        a question they did not ask. Note that a track filter makes an empty
        result much more likely: the eligible set is already small, and one
        track's slice of it is often empty even when the unfiltered call
        returns something. That is what get_domain_map exists to explain —
        an empty list here means "nothing startable", not "nothing to learn".
        """
        data = self.load_knowledge()
        known_concepts = set(data["concepts"].keys())

        # Bucket candidates by track, within this tutor's tracks only.
        buckets: dict[str, list[dict]] = {}
        for concept_id, info in CURRICULUM_GRAPH.items():
            if info.get("track", "python") not in self.tracks:
                continue
            if track and info.get("track", "python") != track:
                continue
            if concept_id in known_concepts:
                continue
            prereqs = info["prereqs"]
            if not all(p in known_concepts for p in prereqs):
                continue
            track = info.get("track", "python")
            buckets.setdefault(track, []).append({
                "concept_id": concept_id,
                "name": info["name"],
                "category": info["category"],
                "track": track,
                "prerequisites_met": prereqs,
                "reason": f"Prerequisites met: {', '.join(prereqs)}" if prereqs else "No prerequisites needed",
            })

        # Sort within each bucket by intra-track category priority, then staleness
        for track_list in buckets.values():
            track_list.sort(key=lambda s: (
                CATEGORY_PRIORITY.get(s["category"], 99),
                s["concept_id"],
            ))

        # Pick the learner's primary track — whichever of THIS tutor's tracks
        # has the most concepts at practiced/mastered. Ties break toward the
        # tutor's first track, which is the one its curriculum leads with.
        app_track_order = [t for t in TRACK_ORDER if t in self.tracks]
        default_track = app_track_order[0]

        track_scores: dict[str, int] = {}
        for cid, concept in data["concepts"].items():
            if concept.get("mastery") not in ("practiced", "mastered"):
                continue
            concept_track = CURRICULUM_GRAPH.get(cid, {}).get("track")
            if concept_track not in self.tracks:
                continue  # another tutor's progress, or an invented concept
            track_scores[concept_track] = track_scores.get(concept_track, 0) + 1
        if track_scores:
            primary_track = max(
                track_scores.items(),
                key=lambda kv: (kv[1], kv[0] == default_track),
            )[0]
        else:
            primary_track = default_track

        # Build an interleaved walk order: primary first, then the rest in a
        # stable order. Each round, take one candidate from each track in
        # walk order, until we hit `count` or exhaust all buckets.
        track_order = [primary_track] + [
            t for t in app_track_order if t != primary_track
        ]
        result: list[dict] = []
        while len(result) < count:
            made_progress = False
            for track in track_order:
                bucket = buckets.get(track)
                if bucket:
                    result.append(bucket.pop(0))
                    made_progress = True
                    if len(result) >= count:
                        break
            if not made_progress:
                break
        return result

    def get_domain_map(self, track: str) -> dict:
        """The whole shape of one track: what is in it, in what order, how far off.

        get_next_topic_suggestions answers "what can you start right now", which
        for a track gated behind another track is nothing at all. Asking about
        MLOps returned a single Python fundamentals concept, because all 15 ops
        concepts sit behind ML prerequisites that are not recorded yet — the
        tutor could not tell "this domain is empty" from "this domain is not
        open yet", and fell back to asking the learner what MLOps contains.

        So this returns every concept in the track, each marked with its status
        and, when blocked, the specific prerequisites that are missing. Nothing
        is filtered out; the caller gets the map and can describe the route.

        `gateway_concepts` are the missing prerequisites that lie OUTSIDE this
        track, ordered by how many of the track's concepts they unblock. That
        is the actionable part of the answer: for a fully-gated track, the next
        step is never in the track itself.
        """
        data = self.load_knowledge()
        known = set(data["concepts"].keys())

        concepts: list[dict] = []

        for concept_id, info in CURRICULUM_GRAPH.items():
            if info.get("track", "python") != track:
                continue

            prereqs = info["prereqs"]
            missing = [p for p in prereqs if p not in known]

            if concept_id in known:
                status = data["concepts"][concept_id].get("mastery", "introduced")
            elif missing:
                status = "blocked"
            else:
                status = "available"

            concepts.append({
                "concept_id": concept_id,
                "name": info["name"],
                "category": info["category"],
                "status": status,
                "prerequisites": prereqs,
                "missing_prerequisites": missing,
            })

        # Curriculum order within the track, so the list reads as a route
        # rather than a set: available first, then by category priority.
        status_rank = {"available": 0, "blocked": 1}
        concepts.sort(key=lambda c: (
            status_rank.get(c["status"], 2),
            CATEGORY_PRIORITY.get(c["category"], 99),
            c["concept_id"],
        ))

        # Gateways are counted TRANSITIVELY. Counting only direct dependents
        # ranks every prerequisite equally — for a fully-gated track each one
        # blocks exactly one concept, and the list says nothing about where to
        # start. Walking the full unmet chain instead shows that
        # sklearn_pipelines gates the entire serving line, while
        # llm_evaluation gates one leaf.
        def _unmet_chain(cid: str, seen: set[str]) -> set[str]:
            """Every unmet prerequisite behind `cid`, transitively."""
            out: set[str] = set()
            for p in CURRICULUM_GRAPH.get(cid, {}).get("prereqs", []):
                if p in known or p in seen:
                    continue
                seen.add(p)
                out.add(p)
                out |= _unmet_chain(p, seen)
            return out

        gateway_counts: dict[str, int] = {}
        for c in concepts:
            if c["status"] != "blocked":
                continue
            for p in _unmet_chain(c["concept_id"], set()):
                # Only prerequisites outside this track are gateways. A missing
                # in-track prereq resolves by learning the track in order and
                # is already visible as that concept's own row.
                if CURRICULUM_GRAPH.get(p, {}).get("track", "python") != track:
                    gateway_counts[p] = gateway_counts.get(p, 0) + 1

        # Entry points first: a gateway that is itself blocked cannot be
        # started today, so a startable one outranks it at equal reach.
        gateways = [
            {
                "concept_id": cid,
                "name": CURRICULUM_GRAPH.get(cid, {}).get("name", cid),
                "track": CURRICULUM_GRAPH.get(cid, {}).get("track", "python"),
                "unblocks": n,
                "startable": all(
                    p in known
                    for p in CURRICULUM_GRAPH.get(cid, {}).get("prereqs", [])
                ),
            }
            for cid, n in gateway_counts.items()
        ]
        gateways.sort(key=lambda g: (not g["startable"], -g["unblocks"], g["concept_id"]))

        return {
            "track": track,
            "track_name": TRACK_NAMES.get(track, track),
            "total": len(concepts),
            "available": sum(1 for c in concepts if c["status"] == "available"),
            "blocked": sum(1 for c in concepts if c["status"] == "blocked"),
            "known": sum(
                1 for c in concepts
                if c["status"] not in ("available", "blocked")
            ),
            "concepts": concepts,
            "gateway_concepts": gateways,
        }

    def clear_struggle_area(self, concept_id: str, *, passed: bool) -> bool:
        """Remove a concept from struggle_areas after a passed re-test.

        struggle_areas used to be a one-way ratchet: record_quiz appended on a
        wrong answer and nothing ever removed the entry, so a single miss was
        reported in every system prompt forever. The live symptom was a concept
        sitting at mastery="mastered" while the same prompt said the learner
        struggled with it.

        Clearing is deliberately not automatic. A flag is only lifted when the
        learner asks to be re-tested and passes, so "no longer struggling" is
        backed by evidence the learner chose to produce.

        Returns True when the flag was actually removed.
        """
        if not passed:
            return False

        data = self.load_knowledge()
        struggles = data["profile"].get("struggle_areas", [])
        if concept_id not in struggles:
            return False

        struggles.remove(concept_id)
        data["profile"]["struggle_areas"] = struggles
        # Recorded rather than silently dropped: the learning path is the audit
        # trail for every other mastery change, and a recovery is one too.
        data["learning_path"].append({
            "concept": concept_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "struggle_cleared",
            "mastery": data["concepts"].get(concept_id, {}).get("mastery", ""),
        })
        self.save_knowledge(data)
        logger.info(f"Struggle area '{concept_id}' cleared by passed re-test")
        return True

    def record_quiz(self, quiz_data: dict, retest_concept: str | None = None) -> dict:
        """Save quiz results and promote mastery for correct answers.

        quiz_data: {quiz_id, topics, questions: [{question, user_answer, correct, concept}], score, total}

        retest_concept: set when the learner explicitly asked to be re-tested on
        a flagged concept. Every question on that concept must be correct for
        the struggle flag to clear; anything less leaves it in place.

        Returns dict with mastery_changes and struggle_cleared.
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
                # Note the struggle area. A failed re-test is exempt: the
                # concept is already flagged, and re-appending would be a no-op
                # that reads like a second, independent failure.
                if concept_id != retest_concept and concept_id not in data["profile"].get("struggle_areas", []):
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

        # Evaluate the re-test after the quiz itself is saved, so clearing the
        # flag reads the state this quiz just wrote (mastery promotions
        # included) rather than a stale copy.
        struggle_cleared = False
        if retest_concept:
            answers = [
                q for q in quiz_data.get("questions", [])
                if q.get("concept") == retest_concept
            ]
            # A re-test with no questions on the concept proves nothing.
            passed = bool(answers) and all(q.get("correct") for q in answers)
            struggle_cleared = self.clear_struggle_area(retest_concept, passed=passed)

        return {
            "mastery_changes": mastery_changes,
            "quiz_entry": quiz_entry,
            "struggle_cleared": struggle_cleared,
        }

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
