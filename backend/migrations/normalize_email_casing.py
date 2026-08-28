"""Fold non-canonical email casings in ChromaDB onto their lowercase form.

Why this exists
---------------
`_safe_email` has lowercased every filesystem path since the first commit, but
`episodic.py` used to store the *raw* email in ChromaDB metadata and in the
record IDs, then read it back with an exact-match `where={"user_email": ...}`.
Chroma does not case-fold, so a learner who arrived as "YASHG41" could not see
the rows written as "yashg41" and vice versa — on this install that hid roughly
a third of one learner's episodic memory from the tutor.

The code fix (episodic.py normalizes on the way in and out) stops new splits.
This script repairs the rows already on disk.

What it does
------------
For every record whose `user_email` is not already canonical, it rewrites both
the metadata value *and* the record ID, because the ID embeds the email:

    ex_YASHG41_<session>_<idx>  ->  ex_yashg41_<session>_<idx>

Rewriting the ID is the point, not a flourish. `save_exchange` derives the ID
deterministically so a re-save upserts rather than duplicates, and
`POST /api/chroma/backfill/{email}` advertises that it is safe to run
repeatedly. Leaving a row under an uppercase ID while the code computes a
lowercase one turns the next backfill into a duplicate-generator.

The existing embedding vector is read and written back verbatim, so nothing is
re-embedded and similarity ranking is unchanged. `merged_from` / `original_id`
(left by an earlier identity consolidation) are preserved untouched.

Usage
-----
    python -m backend.migrations.normalize_email_casing            # dry run
    python -m backend.migrations.normalize_email_casing --apply

Safe to re-run: after a successful pass no record has a non-canonical casing,
so the selector matches nothing and the script is a no-op.
"""

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone

import chromadb

from backend.config import settings
from backend.memory import _normalize_email

COLLECTIONS = ("conversations", "conversation_exchanges")

# Record IDs are built as "<prefix><email><rest>" — see episodic.save_episode
# and episodic.save_exchange.
ID_PREFIXES = ("ex_", "ep_")


def _rewrite_id(old_id: str, raw_email: str, canonical: str) -> str | None:
    """Return the ID with its email segment folded, or None if it doesn't match.

    Anchored on the known prefix + the exact email rather than splitting on
    "_", because an email may itself contain underscores: naively splitting
    "ex_a_b@x.com_<sid>_3" would corrupt the address.
    """
    for prefix in ID_PREFIXES:
        head = prefix + raw_email
        if old_id.startswith(head):
            return prefix + canonical + old_id[len(head):]
    return None


def _variants(col) -> dict[str, int]:
    """Distinct non-canonical user_email values in a collection, with counts."""
    got = col.get(include=["metadatas"])
    counts: dict[str, int] = {}
    for meta in got.get("metadatas") or []:
        value = (meta or {}).get("user_email")
        if not isinstance(value, str) or not value:
            continue
        if value != _normalize_email(value):
            counts[value] = counts.get(value, 0) + 1
    return counts


def _backup_db() -> str | None:
    """Copy chroma.sqlite3 next to itself, stamped, before any write."""
    src = os.path.join(settings.CHROMA_DIR, "chroma.sqlite3")
    if not os.path.isfile(src):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = f"{src}.bak_{stamp}"
    shutil.copy2(src, dst)
    return dst


def _plan_collection(col, raw_email: str) -> tuple[list, list, list, list, list]:
    """Read every row for one casing and compute its rewritten form."""
    canonical = _normalize_email(raw_email)
    got = col.get(
        where={"user_email": raw_email},
        include=["documents", "metadatas", "embeddings"],
    )

    old_ids = got["ids"]
    docs = got.get("documents") or []
    metas = got.get("metadatas") or []
    embs = got.get("embeddings")

    new_ids, new_metas, unmatched = [], [], []
    for i, old_id in enumerate(old_ids):
        new_id = _rewrite_id(old_id, raw_email, canonical)
        if new_id is None:
            # The ID doesn't carry the email in the expected shape. Keep it as
            # it is and fix only the metadata — never guess at an ID.
            unmatched.append(old_id)
            new_id = old_id
        new_ids.append(new_id)

        meta = dict(metas[i] or {})
        meta["user_email"] = canonical
        new_metas.append(meta)

    return old_ids, new_ids, docs, new_metas, (embs, unmatched)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without this the script only reports.",
    )
    args = parser.parse_args()

    client = chromadb.PersistentClient(path=settings.CHROMA_DIR)

    # ---- Plan ----------------------------------------------------------
    work = []
    for name in COLLECTIONS:
        try:
            col = client.get_collection(name)
        except Exception:
            print(f"  {name}: not present, skipping")
            continue
        variants = _variants(col)
        if variants:
            work.append((name, col, variants))
        print(f"  {name}: {col.count()} records, "
              f"{len(variants)} non-canonical casing(s) {variants or ''}")

    if not work:
        print("\nNothing to do — every user_email is already canonical.")
        return 0

    total_before = {n: client.get_collection(n).count() for n in COLLECTIONS}

    # ---- Pre-flight: no computed ID may already exist -------------------
    print()
    blocked = False
    for name, col, variants in work:
        existing = set(col.get(include=[])["ids"])
        for raw_email in variants:
            old_ids, new_ids, _, _, (_, unmatched) = _plan_collection(col, raw_email)
            moving = {o: n for o, n in zip(old_ids, new_ids) if o != n}
            collisions = [n for n in moving.values() if n in existing]
            print(f"  {name} / {raw_email!r} -> {_normalize_email(raw_email)!r}: "
                  f"{len(old_ids)} rows, {len(moving)} id rewrites, "
                  f"{len(collisions)} collisions, {len(unmatched)} id(s) left as-is")
            for old, new in list(moving.items())[:2]:
                print(f"      {old}\n        -> {new}")
            if collisions:
                blocked = True
                print(f"      !! would overwrite: {collisions[:5]}")

    if blocked:
        print("\nAborting: a rewritten ID already exists. Resolve by hand.")
        return 1

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to commit.")
        return 0

    # ---- Apply ---------------------------------------------------------
    backup = _backup_db()
    print(f"\nBacked up: {backup}" if backup else "\nNo sqlite file to back up.")

    for name, col, variants in work:
        for raw_email in variants:
            old_ids, new_ids, docs, metas, (embs, _) = _plan_collection(col, raw_email)
            if not old_ids:
                continue

            # Add before delete: interrupted here, the worst case is duplicate
            # rows that a re-run cleans up — not lost history.
            col.add(
                ids=new_ids,
                documents=list(docs),
                metadatas=metas,
                embeddings=embs,
            )
            stale = [o for o, n in zip(old_ids, new_ids) if o != n]
            if stale:
                col.delete(ids=stale)
            print(f"  {name}: folded {len(old_ids)} rows "
                  f"{raw_email!r} -> {_normalize_email(raw_email)!r}")

    # ---- Verify --------------------------------------------------------
    print()
    ok = True
    for name in COLLECTIONS:
        try:
            col = client.get_collection(name)
        except Exception:
            continue
        after = col.count()
        before = total_before.get(name)
        leftover = _variants(col)
        print(f"  {name}: {before} -> {after} records, "
              f"{len(leftover)} non-canonical casing(s) remaining")
        if before != after or leftover:
            ok = False

    if not ok:
        print("\nVerification FAILED — inspect before trusting this run.")
        return 1

    print("\nDone. Counts preserved, all casings canonical.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
