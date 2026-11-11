"""The curriculum loads from YAML and keeps the shape the rest of the app expects.

These guard the seam created when ~285 lines of CURRICULUM_* dict literals moved
out of knowledge.py into backend/curriculum/*.yaml. Everything that reads the
curriculum — tools.py, routes.py, the graph view, the other test modules —
imports the same six names it always did, so the tests here are about the
contract, not the file format.

The dangling-prereq check is deliberately a hard failure at import time rather
than a warning: a prereq pointing at a concept that does not exist makes its
dependent permanently unreachable, because the prerequisite walk can never mark
it met. That is a silently unteachable concept, which is worse than a crash.
"""

import pytest
import yaml

from backend.curriculum import APP_TRACKS, CURRICULUM_DIR, _load_all
from backend.knowledge import (
    CATEGORY_LABELS,
    CATEGORY_PRIORITY,
    CURRICULUM_GRAPH,
    TRACK_HUE,
    TRACK_NAMES,
    TRACK_ORDER,
    build_oklch_palette,
    category_label,
    taught_domains,
)


def test_every_track_has_a_file_and_declares_itself():
    """TRACK_ORDER and the filenames agree, and each file names its own track."""
    for track in TRACK_ORDER:
        path = CURRICULUM_DIR / f"{track}.yaml"
        assert path.is_file(), f"{track} is in TRACK_ORDER but {path.name} is missing"
        assert yaml.safe_load(path.read_text())["track"] == track


def test_no_dangling_prereqs():
    """Every prereq resolves. Import would already have raised; this names it."""
    dangling = {
        f"{cid} -> {pr}"
        for cid, info in CURRICULUM_GRAPH.items()
        for pr in info["prereqs"]
        if pr not in CURRICULUM_GRAPH
    }
    assert not dangling, f"dangling prereqs: {sorted(dangling)}"


def test_no_concept_is_its_own_prerequisite():
    """A self-loop is unreachable the same way a dangling prereq is."""
    for cid, info in CURRICULUM_GRAPH.items():
        assert cid not in info["prereqs"], f"{cid} lists itself as a prereq"


def test_prereq_graph_is_acyclic():
    """A cycle makes every concept in it permanently blocked.

    get_next_topic_suggestions only ever offers a concept whose prereqs are all
    known, so a cycle is not a slow path — it is a set of concepts no learner
    can ever be offered.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(CURRICULUM_GRAPH, WHITE)

    def walk(node, path):
        colour[node] = GREY
        for nxt in CURRICULUM_GRAPH[node]["prereqs"]:
            if colour[nxt] == GREY:
                cycle = path[path.index(nxt):] + [nxt] if nxt in path else [nxt]
                pytest.fail(f"prereq cycle: {' -> '.join(cycle)}")
            if colour[nxt] == WHITE:
                walk(nxt, path + [nxt])
        colour[node] = BLACK

    for cid in CURRICULUM_GRAPH:
        if colour[cid] == WHITE:
            walk(cid, [cid])


def test_every_concept_has_a_name_and_a_known_track():
    for cid, info in CURRICULUM_GRAPH.items():
        assert info["name"].strip(), f"{cid} has an empty name"
        assert info["track"] in TRACK_ORDER, f"{cid} has unknown track {info['track']}"
        assert info["category"], f"{cid} has no category"


def test_each_category_belongs_to_exactly_one_track():
    """The colour ramp derives a category's hue from its track, so a category
    spanning two tracks would have no well-defined colour."""
    owner: dict[str, str] = {}
    for cid, info in CURRICULUM_GRAPH.items():
        cat, track = info["category"], info["track"]
        assert owner.setdefault(cat, track) == track, (
            f"category '{cat}' spans {owner[cat]} and {track} (at {cid})"
        )


def test_every_category_has_a_priority():
    """Ordering within a track comes from CATEGORY_PRIORITY; a missing entry
    silently falls through to 99 and sorts last."""
    for cid, info in CURRICULUM_GRAPH.items():
        assert info["category"] in CATEGORY_PRIORITY, (
            f"category '{info['category']}' (from {cid}) has no priority"
        )


def test_track_metadata_is_complete():
    for track in TRACK_ORDER:
        assert track in TRACK_NAMES and TRACK_NAMES[track].strip()
        assert isinstance(TRACK_HUE[track], int) and 0 <= TRACK_HUE[track] < 360


def test_track_hues_are_distinct():
    """Two tracks sharing a hue makes them indistinguishable in the graph view."""
    seen: dict[int, str] = {}
    for track in TRACK_ORDER:
        hue = TRACK_HUE[track]
        assert hue not in seen, f"{track} and {seen[hue]} share hue {hue}"
        seen[hue] = track


def test_labels_survive_the_round_trip():
    """A spelled-out label is used; an absent one falls back to title-casing."""
    for cat, label in CATEGORY_LABELS.items():
        assert category_label(cat) == label
    assert category_label("san_invented_thing") == "San Invented Thing"


def test_palette_covers_every_category_in_both_themes():
    for theme in ("dark", "light"):
        palette = build_oklch_palette(theme)
        for cid, info in CURRICULUM_GRAPH.items():
            assert info["category"] in palette["categories"], (
                f"{info['category']} (from {cid}) has no {theme} colour"
            )
        for track in TRACK_ORDER:
            assert track in palette["tracks"]


def test_identity_line_names_every_track_the_app_teaches():
    """taught_domains() builds a tutor's identity from the curriculum, so a
    track added to an app cannot leave that prompt naming a different subject."""
    for app, tracks in APP_TRACKS.items():
        domains = taught_domains(app)
        for track in tracks:
            assert TRACK_NAMES[track] in domains, (
                f"{track} missing from {app}'s identity line"
            )


def test_identity_line_names_no_other_app_tracks():
    """The regression this pair exists for.

    taught_domains() used to walk every loaded track, so adding the Sanskrit
    curriculum immediately made PyMentor introduce itself as teaching
    "... system design, and Sanskrit". Deriving identity from the curriculum is
    the right idea; deriving it from *every* curriculum is the bug.
    """
    for app, tracks in APP_TRACKS.items():
        domains = taught_domains(app)
        for other in set(TRACK_ORDER) - set(tracks):
            assert TRACK_NAMES[other] not in domains, (
                f"{app}'s identity line leaks the '{other}' track"
            )


@pytest.mark.sanskrit
def test_single_track_app_reads_naturally():
    """One track should just be named, not dressed up as a lead plus a tail."""
    assert taught_domains("sanskrit") == "Sanskrit"


def test_every_track_belongs_to_exactly_one_app():
    """An unassigned track loads but no tutor ever teaches it — invisible."""
    assigned = [t for tracks in APP_TRACKS.values() for t in tracks]
    assert set(assigned) == set(TRACK_ORDER)
    assert len(assigned) == len(set(assigned)), "a track is assigned twice"


def test_suggestions_never_cross_app_boundaries(tmp_path):
    """A tutor must not offer a concept belonging to the other tutor."""
    from backend.knowledge import KnowledgeStore

    for app, tracks in APP_TRACKS.items():
        store = KnowledgeStore(str(tmp_path / app), app=app)
        for s in store.get_next_topic_suggestions(count=25):
            assert s["track"] in tracks, (
                f"{app} suggested '{s['concept_id']}' from track '{s['track']}'"
            )


def test_loader_is_deterministic():
    """Reloading yields the same graph — nothing depends on dict iteration luck."""
    graph, priority, labels, hues, names, _ = _load_all()
    assert graph == CURRICULUM_GRAPH
    assert priority == CATEGORY_PRIORITY
    assert labels == CATEGORY_LABELS
    assert hues == TRACK_HUE
    assert names == TRACK_NAMES
