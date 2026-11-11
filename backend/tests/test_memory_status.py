"""Episode counting is per-learner, and folds casing like every other lookup.

`get_memory_status` reports how many summaries a learner has. The obvious
implementation — `collection.count()` — returns the size of the whole store
across every user, which is the wrong number to show a tutor asking what it
remembers about one person.
"""

import tempfile

import pytest

from backend.episodic import EpisodicMemory


@pytest.fixture
def store():
    return EpisodicMemory(chroma_dir=tempfile.mkdtemp())


def test_counts_only_this_learner(store):
    store.save_episode("alice@example.com", "Alice covered lists", ["lists"])
    store.save_episode("alice@example.com", "Alice covered loops", ["loops"])
    store.save_episode("bob@example.com", "Bob covered sets", ["sets"])

    assert store.count_episodes("alice@example.com") == 2
    assert store.count_episodes("bob@example.com") == 1


def test_count_folds_casing(store):
    """Same identity rule as every other episodic method — see _normalize_email."""
    store.save_episode("Learner@Example.com", "covered lists", ["lists"])
    for variant in ("learner@example.com", "LEARNER@EXAMPLE.COM", "  Learner@Example.com "):
        assert store.count_episodes(variant) == 1


def test_unknown_learner_counts_zero(store):
    assert store.count_episodes("nobody@example.com") == 0
