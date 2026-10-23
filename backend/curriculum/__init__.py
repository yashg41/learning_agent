"""The curriculum, loaded from YAML rather than written as Python literals.

Why this is data and not code. The curriculum is the one part of this app that
is *about a subject* rather than about tutoring: everything else in knowledge.py
— mastery, prerequisite walks, gateway ranking, the colour ramp — works the same
whether the subject is Python or Sanskrit. Keeping ~285 lines of subject matter
as module constants meant a second subject could only be added by editing the
same file the graph algorithms live in.

One file per track. A track is a self-contained document: its concepts, the
categories they group into, the order those categories are taught in, and the
hue the graph view paints them. Adding a track is adding a file; it is not an
edit in the middle of an existing one.

Categories carry their concepts here, rather than each concept naming its
category as CURRICULUM_* did. That removes a `category` and a `track` key from
every single node — they were identical across whole blocks and were exactly the
kind of repetition a typo hides in.

The public names (CURRICULUM_GRAPH, CATEGORY_PRIORITY, TRACK_ORDER, ...) are
rebuilt to the shape knowledge.py and the tests already expect, so this swap is
invisible above this module.
"""

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CURRICULUM_DIR = Path(__file__).parent

# Track order is load order, and load order is this list — not a directory
# glob. The order decides how cross-track suggestions interleave, so it is a
# curriculum decision that has to be written down, not an accident of how the
# filesystem happens to sort. A file not named here is not loaded.
TRACK_ORDER = (
    "python", "ml", "dl", "llm", "ops", "quantum", "sysdesign",
)

# Which tracks belong to which tutor.
#
# TRACK_ORDER is every track this backend can load; it is NOT what any one
# tutor teaches. Adding the Sanskrit track without this split immediately made
# PyMentor introduce itself as teaching "... system design, and Sanskrit",
# because the identity line is derived from the curriculum — which is normally
# the point, and here was the bug. A tutor must only ever see its own tracks.
APP_TRACKS = {
    "pymentor": ("python", "ml", "dl", "llm", "ops", "quantum", "sysdesign"),
}

DEFAULT_APP = "pymentor"


def app_tracks(app: str = DEFAULT_APP) -> tuple[str, ...]:
    """The tracks one tutor teaches, in curriculum order."""
    if app not in APP_TRACKS:
        raise ValueError(f"Unknown app '{app}'. Known apps: {sorted(APP_TRACKS)}.")
    return APP_TRACKS[app]


def _load_track(track: str) -> dict:
    """One track's YAML document, validated enough to fail usefully."""
    path = CURRICULUM_DIR / f"{track}.yaml"
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Curriculum file missing for track '{track}': {path}. "
            f"Either add it or remove '{track}' from TRACK_ORDER."
        ) from None

    if doc.get("track") != track:
        raise ValueError(
            f"{path.name} declares track '{doc.get('track')}' but is loaded as "
            f"'{track}'. The filename and the track field must agree."
        )
    return doc


def _load_all() -> tuple[dict, dict, dict, dict, dict, dict]:
    """Read every track once and derive the six lookup tables from it.

    Built in a single pass so the tables cannot disagree with each other: a
    category's priority, label, owning track and concepts all come from the same
    parse of the same file.
    """
    graph: dict[str, dict] = {}
    priority: dict[str, int] = {}
    labels: dict[str, str] = {}
    hues: dict[str, int] = {}
    names: dict[str, str] = {}
    cat_track: dict[str, str] = {}

    for track in TRACK_ORDER:
        doc = _load_track(track)
        names[track] = doc["name"]
        hues[track] = doc["hue"]

        for cat in doc.get("categories", []):
            cid = cat["id"]
            if cid in cat_track and cat_track[cid] != track:
                raise ValueError(
                    f"Category '{cid}' appears in both '{cat_track[cid]}' and "
                    f"'{track}'. A category belongs to exactly one track — the "
                    f"colour ramp derives its hue from that."
                )
            cat_track[cid] = track
            priority[cid] = cat.get("priority", 99)
            # Only record a label when the file gives one. Absent means "let
            # category_label() title-case it", which is what an invented
            # category gets; writing None here would defeat that fallback.
            if cat.get("label"):
                labels[cid] = cat["label"]

            for concept in cat.get("concepts", []):
                key = concept["id"]
                if key in graph:
                    raise ValueError(
                        f"Concept '{key}' is defined twice "
                        f"({graph[key]['track']} and {track}). Ids are global "
                        f"because prereqs reference them across tracks."
                    )
                graph[key] = {
                    "name": concept["name"],
                    "prereqs": concept.get("prereqs") or [],
                    "category": cid,
                    "track": track,
                }

    return graph, priority, labels, hues, names, cat_track


(
    CURRICULUM_GRAPH,
    CATEGORY_PRIORITY,
    CATEGORY_LABELS,
    TRACK_HUE,
    TRACK_NAMES,
    CATEGORY_TRACK,
) = _load_all()


# Integrity check, kept at import time and kept fatal. A dangling prereq is not
# a degraded graph, it is a silently unreachable concept: the prerequisite walk
# would treat it as permanently unmet and the concept would never be suggested
# to anyone. Better to refuse to start than to teach around a hole.
_missing = {
    f"{cid} -> {pr}"
    for cid, info in CURRICULUM_GRAPH.items()
    for pr in info["prereqs"]
    if pr not in CURRICULUM_GRAPH
}
if _missing:
    raise AssertionError(f"Curriculum has dangling prereqs: {sorted(_missing)}")
del _missing

# Every track belongs to exactly one app. An unassigned track loads but no tutor
# ever teaches it — invisible, and the kind of thing found weeks later.
_assigned = [t for tracks in APP_TRACKS.values() for t in tracks]
_orphans = set(TRACK_ORDER) - set(_assigned)
if _orphans:
    raise AssertionError(
        f"Tracks belong to no app: {sorted(_orphans)}. Add them to APP_TRACKS."
    )
_unknown = set(_assigned) - set(TRACK_ORDER)
if _unknown:
    raise AssertionError(f"APP_TRACKS names unknown tracks: {sorted(_unknown)}.")
_duplicated = {t for t in _assigned if _assigned.count(t) > 1}
if _duplicated:
    raise AssertionError(
        f"Tracks assigned to more than one app: {sorted(_duplicated)}. "
        f"Cross-app sharing is not supported — the identity line would name it twice."
    )
del _assigned, _orphans, _unknown, _duplicated

logger.debug(
    "Curriculum loaded: %d concepts across %d tracks",
    len(CURRICULUM_GRAPH), len(TRACK_ORDER),
)
