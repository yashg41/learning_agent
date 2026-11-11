"""Struggle areas must be escapable.

The bug: `record_quiz` appended a concept to `profile.struggle_areas` on a wrong
answer and nothing anywhere removed it. The live symptom was a concept sitting
at mastery="mastered" while every system prompt still said the learner
struggled with it — the tutor told both things at once.

Clearing is deliberately narrow: only a re-test the learner asked for, and only
when every question on that concept is correct. These tests pin both the
clearing and the fact that ordinary quizzes still flag.
"""

import tempfile

import pytest

from backend.knowledge import KnowledgeStore


@pytest.fixture
def store():
    return KnowledgeStore(tempfile.mkdtemp())


def _quiz(concept, *results):
    """A quiz where each bool in `results` is one question on `concept`."""
    return {
        "quiz_id": "q",
        "topics": [concept],
        "questions": [
            {"question": "q", "user_answer": "a", "correct": ok, "concept": concept}
            for ok in results
        ],
        "score": sum(results),
        "total": len(results),
    }


def _flagged(store):
    return store.load_knowledge()["profile"].get("struggle_areas", [])


@pytest.fixture
def flagged_store(store):
    """A store where 'caching' is already flagged by an ordinary wrong answer."""
    store.upsert_concept("caching", "Caching", "sysdesign", "introduced")
    store.record_quiz(_quiz("caching", False))
    assert _flagged(store) == ["caching"]
    return store


# --- The flag still gets set ---


def test_ordinary_wrong_answer_still_flags(store):
    """The existing behaviour must not regress — this is how a struggle is found."""
    store.upsert_concept("caching", "Caching", "sysdesign", "introduced")
    store.record_quiz(_quiz("caching", False))
    assert "caching" in _flagged(store)


def test_flag_is_not_duplicated(flagged_store):
    flagged_store.record_quiz(_quiz("caching", False))
    assert _flagged(flagged_store) == ["caching"]


# --- Clearing it ---


def test_passed_retest_clears_the_flag(flagged_store):
    result = flagged_store.record_quiz(
        _quiz("caching", True, True), retest_concept="caching"
    )
    assert result["struggle_cleared"] is True
    assert _flagged(flagged_store) == []


def test_failed_retest_leaves_the_flag(flagged_store):
    result = flagged_store.record_quiz(
        _quiz("caching", False), retest_concept="caching"
    )
    assert result["struggle_cleared"] is False
    assert _flagged(flagged_store) == ["caching"]


def test_partial_retest_leaves_the_flag(flagged_store):
    """One right and one wrong is not a pass — every question must be correct."""
    result = flagged_store.record_quiz(
        _quiz("caching", True, False), retest_concept="caching"
    )
    assert result["struggle_cleared"] is False
    assert _flagged(flagged_store) == ["caching"]


def test_retest_with_no_questions_on_the_concept_proves_nothing(flagged_store):
    """An empty re-test must not clear by vacuous truth (all([]) is True)."""
    result = flagged_store.record_quiz(
        _quiz("something_else", True), retest_concept="caching"
    )
    assert result["struggle_cleared"] is False
    assert _flagged(flagged_store) == ["caching"]


def test_failed_retest_does_not_double_flag(flagged_store):
    """A failed re-test is not a second, independent failure."""
    flagged_store.record_quiz(_quiz("caching", False), retest_concept="caching")
    assert _flagged(flagged_store) == ["caching"]


# --- The audit trail ---


def test_clearing_is_recorded_in_the_learning_path(flagged_store):
    """Every other mastery change is logged; a recovery is one too."""
    flagged_store.record_quiz(_quiz("caching", True), retest_concept="caching")
    events = [
        e for e in flagged_store.load_knowledge()["learning_path"]
        if e.get("action") == "struggle_cleared"
    ]
    assert len(events) == 1
    assert events[0]["concept"] == "caching"


def test_clearing_an_unflagged_concept_is_a_no_op(store):
    store.upsert_concept("lists", "Lists", "data_structures", "introduced")
    assert store.clear_struggle_area("lists", passed=True) is False
    assert not [
        e for e in store.load_knowledge()["learning_path"]
        if e.get("action") == "struggle_cleared"
    ]


def test_clear_requires_passing(flagged_store):
    assert flagged_store.clear_struggle_area("caching", passed=False) is False
    assert _flagged(flagged_store) == ["caching"]


# --- Normal quiz behaviour is untouched ---


def test_correct_answer_still_promotes_mastery(store):
    store.upsert_concept("lists", "Lists", "data_structures", "introduced")
    result = store.record_quiz(_quiz("lists", True))
    assert result["mastery_changes"]
    assert store.load_knowledge()["concepts"]["lists"]["mastery"] == "practiced"
