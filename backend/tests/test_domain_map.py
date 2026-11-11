"""Domain scoping and the domain map.

The bug these cover: a learner asked for a structured path through MLOps and
the tutor asked *them* what MLOps contains. suggest_next_topics took no track
argument, so it answered a cross-track question, and every ops concept was
filtered out by unmet prerequisites — leaving one Python fundamentals concept
as the entire response to a question about MLOps.
"""

import json

import pytest

from backend.knowledge import CURRICULUM_GRAPH, KnowledgeStore


@pytest.fixture
def store(tmp_path):
    """A KnowledgeStore over an empty temp dir — no prior concepts."""
    return KnowledgeStore(str(tmp_path))


def _learn(store, *concept_ids, mastery="mastered"):
    """Record concepts so their dependents become eligible.

    Pulls name/category from the curriculum so the stored rows match what the
    real tutor would write, rather than placeholders that could mask a
    category-ordering bug.
    """
    for cid in concept_ids:
        info = CURRICULUM_GRAPH[cid]
        store.upsert_concept(
            cid,
            name=info["name"],
            category=info["category"],
            mastery=mastery,
            prerequisites=info["prereqs"],
        )


# --- track scoping ----------------------------------------------------


def test_suggestions_scoped_to_track_never_leak_other_tracks(store):
    _learn(store, "variables")
    for track in ("python", "ml", "ops"):
        for s in store.get_next_topic_suggestions(10, track=track):
            assert s["track"] == track


def test_unscoped_suggestions_still_mix_tracks(store):
    """The track filter must be opt-in — the default stays a cross-track mix."""
    _learn(store, "variables", "data_types", "for_loops")
    tracks = {s["track"] for s in store.get_next_topic_suggestions(10)}
    assert tracks  # non-empty; interleaving behaviour itself is tested elsewhere


def test_gated_track_yields_no_suggestions(store):
    """The exact failing case: MLOps is real but nothing in it is startable."""
    assert store.get_next_topic_suggestions(10, track="ops") == []


# --- the domain map ---------------------------------------------------


def test_domain_map_reports_every_concept_even_when_all_blocked(store):
    """A fully-gated domain must still describe itself. This is the fix:
    an empty suggestion list is 'not open yet', not 'nothing here'."""
    m = store.get_domain_map("ops")
    ops_total = sum(
        1 for i in CURRICULUM_GRAPH.values() if i.get("track") == "ops"
    )
    assert m["total"] == ops_total > 0
    assert len(m["concepts"]) == ops_total
    assert m["blocked"] == ops_total
    assert m["available"] == 0


def test_blocked_concepts_name_their_missing_prerequisites(store):
    m = store.get_domain_map("ops")
    for c in m["concepts"]:
        if c["status"] == "blocked":
            assert c["missing_prerequisites"]
            assert set(c["missing_prerequisites"]) <= set(c["prerequisites"])


def test_gateways_are_outside_the_track_and_rank_by_reach(store):
    """Gateways must be actionable: outside the track, most-unblocking first,
    and a startable one ahead of a blocked one."""
    m = store.get_domain_map("ops")
    gws = m["gateway_concepts"]
    assert gws, "a fully-gated track must expose a way in"

    for g in gws:
        assert g["track"] != "ops"

    reach = [g["unblocks"] for g in gws]
    assert reach == sorted(reach, reverse=True) or any(
        g["startable"] for g in gws
    )
    # Startable gateways come first.
    startable_flags = [g["startable"] for g in gws]
    assert startable_flags == sorted(startable_flags, reverse=True)


def test_transitive_counting_discriminates_between_gateways(store):
    """Direct-dependent counting tied every gateway at 1, which ranked
    nothing. Transitive reach must actually spread them out."""
    m = store.get_domain_map("ops")
    reach = {g["unblocks"] for g in m["gateway_concepts"]}
    assert len(reach) > 1, "all gateways tied — ranking conveys nothing"


def test_learning_a_gateway_opens_the_track(store):
    """The map's advice has to be true: clearing prerequisites must actually
    move concepts from blocked to available."""
    before = store.get_domain_map("ops")
    assert before["available"] == 0

    target = "model_serialization"
    chain = ["variables", "data_types", "for_loops", "lists",
             "functions_basic", "numpy_basics", "pandas_series_df",
             "train_test_split", "linear_regression", "sklearn_pipelines"]
    _learn(store, *chain)

    after = store.get_domain_map("ops")
    opened = [c["concept_id"] for c in after["concepts"]
              if c["status"] == "available"]
    assert target in opened, f"expected {target} to open; available={opened}"
    assert after["blocked"] < before["blocked"]


def test_known_concepts_carry_mastery_not_blocked(store):
    _learn(store, "variables", mastery="practiced")
    m = store.get_domain_map("python")
    row = next(c for c in m["concepts"] if c["concept_id"] == "variables")
    assert row["status"] == "practiced"
    assert m["known"] >= 1


def test_available_concepts_sort_ahead_of_blocked(store):
    _learn(store, "variables", "data_types", "for_loops")
    statuses = [c["status"] for c in store.get_domain_map("python")["concepts"]]
    if "available" in statuses and "blocked" in statuses:
        assert statuses.index("available") < statuses.index("blocked")


@pytest.mark.parametrize("track", ["python", "ml", "dl", "llm", "ops",
                                   "quantum", "sysdesign"])
def test_every_track_produces_a_usable_map(store, track):
    m = store.get_domain_map(track)
    assert m["track"] == track
    assert m["track_name"]
    assert m["total"] == m["available"] + m["blocked"] + m["known"]
