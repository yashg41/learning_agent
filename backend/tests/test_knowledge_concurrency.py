"""Concurrent writes to one learner's knowledge file must not lose concepts.

The tutor emits update_concept several times in a SINGLE parallel batch — one
per concept it just taught, and sometimes twice for the same concept. Those
handlers are async wrappers around a synchronous read-modify-write of one JSON
file, so they interleave on the event loop: without a lock each reads the same
state and the last save wins, silently discarding the rest.

Observed in the wild as a batch of six update_concept calls where two returned
"Stream closed" and the other four "Sibling tool call errored" — the whole
lesson vanished from the tracker while the reply above it looked fine. The
message pointed at the SDK transport, but nothing had restarted; the failure
was here.
"""

import asyncio
import json
import threading

from backend.knowledge import KnowledgeStore, _atomic_write_json, knowledge_lock


# The exact batch from the incident, duplicate included — the two calls that
# reported "Stream closed" were both this same concept.
BATCH = [
    ("vector_databases", "Vector Databases"),
    ("search_engines", "Search Engines / Elasticsearch"),
    ("graph_databases", "Graph Databases"),
    ("columnar_olap", "Columnar / OLAP Databases"),
    ("time_series_databases", "Time-Series Databases"),
    ("vector_databases", "Vector Databases"),
]

UNIQUE = {cid for cid, _ in BATCH}


def test_parallel_batch_keeps_every_concept(tmp_path):
    """All six calls land, including the duplicated one."""
    store = KnowledgeStore(str(tmp_path), app="pymentor")

    async def call(concept_id: str, name: str) -> None:
        # Mirrors update_concept_tool: the lock spans the whole
        # read-modify-write, not just the save.
        async with knowledge_lock(store.knowledge_path):
            # Yield inside the critical section — this is precisely where an
            # unlocked version hands control to a sibling and loses the write.
            await asyncio.sleep(0)
            store.upsert_concept(concept_id, name, "sd_data", "introduced")

    async def batch() -> None:
        await asyncio.gather(*[call(cid, name) for cid, name in BATCH])

    asyncio.run(batch())

    saved = json.loads((tmp_path / "knowledge.json").read_text())
    assert set(saved["concepts"]) == UNIQUE

    # The duplicate is an update, not a second row: seen once, then reviewed.
    assert saved["concepts"]["vector_databases"]["review_count"] == 2


def test_lock_is_shared_across_store_instances(tmp_path):
    """Two stores over one file must contend on the same lock.

    A KnowledgeStore is constructed per request in tools.py, agent.py and
    several routes, so a lock held as instance state would guard nothing —
    each concurrent handler would hold its own. Keying on the path is what
    makes the guard real.
    """
    a = KnowledgeStore(str(tmp_path), app="pymentor")
    b = KnowledgeStore(str(tmp_path), app="pymentor")
    other = KnowledgeStore(str(tmp_path / "other"), app="pymentor")

    async def check() -> None:
        # Inside a loop: asyncio.Lock binds to the running one.
        assert a is not b
        assert knowledge_lock(a.knowledge_path) is knowledge_lock(b.knowledge_path)
        # A different learner must NOT contend.
        assert knowledge_lock(other.knowledge_path) is not knowledge_lock(
            a.knowledge_path
        )

    asyncio.run(check())


def test_reader_never_sees_a_half_written_file(tmp_path):
    """A save must be atomic, so a concurrent read is never torn.

    open(path, "w") truncates on open and writes afterwards, leaving a window
    where the file is empty or partial; a reader landing there got a
    JSONDecodeError. The payload here is large enough that the write is not a
    single syscall, which is what opens the window.
    """
    path = tmp_path / "knowledge.json"
    payload = {"concepts": {f"c{i}": {"notes": "x" * 400} for i in range(600)}}
    _atomic_write_json(str(path), payload)

    torn, reads = [], []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                json.loads(path.read_text())
                reads.append(1)
            except FileNotFoundError:
                pass
            except json.JSONDecodeError:
                torn.append(1)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for _ in range(200):
            _atomic_write_json(str(path), payload)
    finally:
        stop.set()
        for t in threads:
            t.join()

    assert reads, "readers never observed the file; the test proved nothing"
    assert not torn

    # The swap must not litter the directory with its temp files.
    assert [p.name for p in tmp_path.iterdir()] == ["knowledge.json"]


def test_unknown_stored_mastery_does_not_raise(tmp_path):
    """A mastery level outside MASTERY_LEVELS must not kill the write.

    upsert_concept compared old against new with MASTERY_LEVELS.index(), which
    raises ValueError on anything else. It ran after the concept was already
    mutated in memory but before the save, so a single odd value on disk lost
    that write and took every sibling call in the batch down with it.
    """
    store = KnowledgeStore(str(tmp_path), app="pymentor")
    store.upsert_concept("hnsw", "HNSW", "sd_data", "introduced")

    data = store.load_knowledge()
    data["concepts"]["hnsw"]["mastery"] = "learning"  # not a real level
    store.save_knowledge(data)

    concept = store.upsert_concept("hnsw", "HNSW", "sd_data", "practiced")
    assert concept["mastery"] == "practiced"
