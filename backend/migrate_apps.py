"""One-time migration: split the shared data root into one root per app.

    data/users/…      ->  data/pymentor/users/…   and  data/sanskrit/users/…
    data/uploads/…    ->  data/<app>/uploads/…
    data/chroma/      ->  data/<app>/chroma/      (rows partitioned by email)

Before this, both tutors shared one `data/` tree keyed on the learner's email
alone. The only thing separating them in practice was that the same person had
typed two different emails — `yashg41` in PyMentor and `yashg41@sanskrit` in
the Sanskrit UI. This moves each into its own root and renames the Sanskrit one
back to the plain email, so one address means two separate identities
distinguished by which root they live in.

Usage:
    python -m backend.migrate_apps            # dry run — prints the plan
    python -m backend.migrate_apps --apply

Deliberately does NOT read settings.DATA_DIR: with APP set that already points
at data/pymentor, and this script has to reason about the layout that existed
before the split. The legacy paths are computed from the repo root directly.

Every step is `if source exists and target does not`, so a re-run after a
completed migration is a no-op, and a run interrupted halfway can be finished
by running it again.
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LEGACY_ROOT = os.path.join(_REPO_ROOT, "data")
LEGACY_USERS = os.path.join(LEGACY_ROOT, "users")
LEGACY_UPLOADS = os.path.join(LEGACY_ROOT, "uploads")
LEGACY_CHROMA = os.path.join(LEGACY_ROOT, "chroma")

MANIFEST = os.path.join(LEGACY_ROOT, "migration-manifest.json")

# Which legacy email belongs to which app, and what it should be called after.
# The Sanskrit identity loses its suffix: the root now carries that meaning.
USER_MOVES = {
    "yashg41": ("pymentor", "yashg41"),
    "yashg41@sanskrit": ("sanskrit", "yashg41"),
}

# Left over from testing and from typing an email into a live input box, one
# character at a time. Listed explicitly rather than pattern-matched, so a real
# learner can never be caught by a wildcard.
JUNK_USERS = [
    "yashg41@", "yashg41@s",
    "other@example.com",
    "probe@test.com", "probe2@test.com", "probe3@test.com", "probe4@test.com",
]

# pytest fixture directories. Anchored on both ends so a real user named
# "__init__" or "__me" is never matched.
JUNK_RE = re.compile(r"^__.*__$")

COLLECTIONS = ("conversations", "conversation_exchanges")
CHROMA_BATCH = 500


class Migration:
    def __init__(self, apply: bool):
        self.apply = apply
        self.actions: list[dict] = []
        self.problems: list[str] = []

    # --- reporting ---

    def act(self, kind: str, src: str, dst: str = "", note: str = ""):
        self.actions.append({"kind": kind, "src": src, "dst": dst, "note": note})
        arrow = f" -> {self._rel(dst)}" if dst else ""
        extra = f"   ({note})" if note else ""
        print(f"  {kind:8} {self._rel(src)}{arrow}{extra}")

    def _rel(self, path: str) -> str:
        try:
            return os.path.relpath(path, _REPO_ROOT)
        except ValueError:
            return path

    def problem(self, msg: str):
        self.problems.append(msg)
        print(f"  !! {msg}")

    # --- helpers ---

    def _same_device(self, src: str, dst_parent: str) -> bool:
        """Both on one filesystem, so os.rename is atomic and instant.

        Matters because data/users/yashg41/venv is ~469MB. A cross-device
        shutil.move would copy for minutes AND produce a broken venv, since a
        virtualenv bakes absolute paths into its scripts. Better to stop and
        say so than to "helpfully" do the slow wrong thing.
        """
        return os.stat(src).st_dev == os.stat(dst_parent).st_dev

    def move(self, src: str, dst: str, note: str = ""):
        if not os.path.exists(src):
            return False
        if os.path.exists(dst):
            self.problem(f"target already exists, skipping: {self._rel(dst)}")
            return False
        self.act("move", src, dst, note)
        if not self.apply:
            return True

        parent = os.path.dirname(dst)
        os.makedirs(parent, exist_ok=True)
        if not self._same_device(src, parent):
            raise RuntimeError(
                f"{self._rel(src)} and {self._rel(parent)} are on different "
                "filesystems; refusing to copy (a copied venv is broken anyway)."
            )
        os.rename(src, dst)
        return True

    def remove(self, path: str, note: str = ""):
        if not os.path.exists(path):
            return
        self.act("delete", path, note=note or _size(path))
        if self.apply:
            shutil.rmtree(path, ignore_errors=True)

    # --- steps ---

    def preflight(self):
        print("Preflight")
        if not os.path.isdir(LEGACY_USERS):
            print("  data/users/ is gone — nothing to migrate.")
            return False

        for app in ("pymentor", "sanskrit"):
            root = os.path.join(LEGACY_ROOT, app)
            if os.path.isdir(root) and os.listdir(root):
                self.problem(
                    f"data/{app}/ already exists and is not empty, while "
                    "data/users/ is still here — that looks like a half-finished "
                    "run. Inspect it before continuing."
                )
        # A live server would keep writing into the old root mid-move.
        for app, port in (("pymentor", 8001), ("sanskrit", 8003)):
            if _port_in_use(port):
                self.problem(f"something is listening on {port} ({app}) — stop it first")

        if self.problems:
            return False
        print("  ok")
        return True

    def backup_chroma(self):
        print("\nBackup")
        src = os.path.join(LEGACY_CHROMA, "chroma.sqlite3")
        if not os.path.isfile(src):
            print("  no chroma.sqlite3 — nothing to back up")
            return
        dst = os.path.join(LEGACY_ROOT, "chroma.sqlite3.pre-split.bak")
        self.act("copy", src, dst, "20MB safety net")
        if self.apply and not os.path.exists(dst):
            shutil.copy2(src, dst)

    def make_roots(self):
        print("\nRoots")
        for app in ("pymentor", "sanskrit"):
            for sub in ("users", "uploads", "chroma"):
                d = os.path.join(LEGACY_ROOT, app, sub)
                if not os.path.isdir(d):
                    self.act("mkdir", d)
                    if self.apply:
                        os.makedirs(d, exist_ok=True)

    def move_users(self):
        print("\nUsers")
        for legacy_email, (app, new_email) in USER_MOVES.items():
            src = os.path.join(LEGACY_USERS, legacy_email)
            dst = os.path.join(LEGACY_ROOT, app, "users", new_email)
            note = f"{app}" + (f", renamed from {legacy_email}" if new_email != legacy_email else "")
            if self.move(src, dst, note) and new_email != legacy_email:
                self.rewrite_identity(dst, legacy_email, new_email)

    def rewrite_identity(self, user_dir: str, old: str, new: str):
        """Point user.json at the new email, and report anything else holding it.

        Rewriting known keys is safe; blanket search-and-replace across a
        learner's whole tree is not. So the rest is reported for a human to
        look at rather than silently changed.
        """
        path = os.path.join(user_dir, "user.json")
        if os.path.isfile(path):
            self.act("rewrite", path, note=f"{old} -> {new}")
            if self.apply:
                with open(path) as f:
                    data = json.load(f)
                if data.get("email") == old:
                    data["email"] = new
                for entry in data.get("sessions", []):
                    if isinstance(entry, dict) and entry.get("email") == old:
                        entry["email"] = new
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, path)

        # Job records compare their stored `email` for ownership (see
        # demojobs.get_job and friends), so one left holding the old address
        # would silently belong to nobody: invisible in the UI, and unkillable.
        # Session metadata carries it too. Both are our own generated files
        # with a known shape, so rewriting the one key is safe.
        for pattern in (
            os.path.join(user_dir, "*", "jobs", "*.json"),
            os.path.join(user_dir, "code_jobs", "*.json"),
            os.path.join(user_dir, "sessions", "*", "metadata.json"),
        ):
            import glob

            for path in sorted(glob.glob(pattern)):
                try:
                    with open(path) as f:
                        data = json.load(f)
                except (OSError, json.JSONDecodeError):
                    continue
                if data.get("email") != old:
                    continue
                self.act("rewrite", path, note=f"email {old} -> {new}")
                if self.apply:
                    data["email"] = new
                    tmp = path + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(data, f, indent=2)
                    os.replace(tmp, path)

        if self.apply:
            hits = _grep_tree(user_dir, old)
            if hits:
                print(f"     note: {old!r} still appears in {len(hits)} file(s):")
                for h in hits[:10]:
                    print(f"       {self._rel(h)}")
                if len(hits) > 10:
                    print(f"       … and {len(hits) - 10} more")
                print("     (session ids and Chroma keys are opaque; usually fine)")

    def move_uploads(self):
        print("\nUploads")
        if not os.path.isdir(LEGACY_UPLOADS):
            print("  none")
            return
        for name in sorted(os.listdir(LEGACY_UPLOADS)):
            target = USER_MOVES.get(name)
            if not target:
                self.problem(f"uploads/{name} belongs to no known user — left in place")
                continue
            app, new_email = target
            self.move(
                os.path.join(LEGACY_UPLOADS, name),
                os.path.join(LEGACY_ROOT, app, "uploads", new_email),
                app,
            )

    def split_chroma(self):
        print("\nChroma")
        if not os.path.isdir(LEGACY_CHROMA):
            print("  none")
            return
        for legacy_email, (app, new_email) in USER_MOVES.items():
            dst_path = os.path.join(LEGACY_ROOT, app, "chroma")
            self.act("chroma", f"{legacy_email}", dst_path, f"{app}, as {new_email}")
            if self.apply:
                moved = _copy_chroma_rows(LEGACY_CHROMA, dst_path, legacy_email, new_email)
                for name, n in moved.items():
                    print(f"     {name}: {n} rows")

    def cleanup(self):
        print("\nCleanup")
        if not os.path.isdir(LEGACY_USERS):
            return
        for name in sorted(os.listdir(LEGACY_USERS)):
            path = os.path.join(LEGACY_USERS, name)
            if JUNK_RE.match(name):
                self.remove(path, "pytest fixture")
            elif name in JUNK_USERS:
                self.remove(path, "test / typo artifact")

        # On a dry run nothing was actually removed, so subtract what WOULD
        # have gone rather than reporting the whole directory as leftover.
        planned = {os.path.basename(a["src"]) for a in self.actions if a["kind"] == "delete"}
        planned |= {os.path.basename(a["src"]) for a in self.actions if a["kind"] == "move"}
        leftover = sorted(
            n for n in (os.listdir(LEGACY_USERS) if os.path.isdir(LEGACY_USERS) else [])
            if n not in planned
        )
        if not leftover:
            self.act("rmdir", LEGACY_USERS)
            if self.apply:
                os.rmdir(LEGACY_USERS)
        else:
            # Never remove a directory this script does not understand.
            print(f"  data/users/ still holds {len(leftover)} entr(y/ies), left alone:")
            for name in leftover[:10]:
                print(f"    {name}")

    def verify(self):
        print("\nVerify")
        ok = True
        for app in ("pymentor", "sanskrit"):
            users = os.path.join(LEGACY_ROOT, app, "users")
            names = sorted(os.listdir(users)) if os.path.isdir(users) else []
            print(f"  {app}: users={names}")
            for name in names:
                kpath = os.path.join(users, name, "knowledge.json")
                if os.path.isfile(kpath):
                    try:
                        with open(kpath) as f:
                            n = len(json.load(f).get("concepts", {}))
                        print(f"    knowledge.json parses, {n} concepts")
                    except Exception as e:
                        print(f"    !! knowledge.json unreadable: {e}")
                        ok = False
            counts = _chroma_counts(os.path.join(LEGACY_ROOT, app, "chroma"))
            for name, (n, space) in counts.items():
                flag = "" if space == "cosine" else f"  !! hnsw:space={space}"
                print(f"    chroma {name}: {n} rows{flag}")
                if space != "cosine":
                    ok = False
        return ok

    def write_manifest(self):
        if not self.apply:
            return
        payload = {
            "when": datetime.now(timezone.utc).isoformat(),
            "actions": self.actions,
            "problems": self.problems,
        }
        with open(MANIFEST, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nManifest written to {self._rel(MANIFEST)}")

    def run(self) -> int:
        mode = "APPLYING" if self.apply else "DRY RUN — nothing will change"
        print(f"=== Split data/ into per-app roots — {mode} ===\n")

        if not self.preflight():
            print("\nRefusing to continue.")
            return 1

        self.backup_chroma()
        self.make_roots()
        self.move_users()
        self.move_uploads()
        self.split_chroma()
        self.cleanup()

        if self.apply:
            ok = self.verify()
            self.write_manifest()
            print("\nLeft in place as a backup: data/chroma/ and "
                  "data/chroma.sqlite3.pre-split.bak")
            print("Delete them once both apps have run a real session.")
            return 0 if ok else 1

        print(f"\n{len(self.actions)} action(s) planned. "
              "Re-run with --apply to perform them.")
        return 0


# --- helpers that touch the outside world ---


def _size(path: str) -> str:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024:
            return f"{total:.0f}{unit}"
        total /= 1024
    return f"{total:.0f}TB"


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _grep_tree(root: str, needle: str) -> list[str]:
    """Files under root whose text contains needle. Skips venv and binaries."""
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("venv", "__pycache__")]
        for name in filenames:
            if not name.endswith((".json", ".jsonl", ".txt", ".md")):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    if needle in f.read():
                        hits.append(path)
            except OSError:
                pass
    return hits


def _copy_chroma_rows(src_path: str, dst_path: str, legacy_email: str,
                      new_email: str) -> dict:
    """Move one learner's episodes into an app's own Chroma, rewriting the email.

    Embeddings are carried across verbatim. `get()` omits them by default, so
    without include=["embeddings"] the upsert would silently re-embed every
    document — slow, and a source of drift if the bundled model ever changes.
    """
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    # Telemetry off, as everywhere we build a client — see episodic.py.
    quiet = ChromaSettings(anonymized_telemetry=False)
    src = chromadb.PersistentClient(path=src_path, settings=quiet)
    dst = chromadb.PersistentClient(path=dst_path, settings=quiet)
    moved = {}

    for name in COLLECTIONS:
        try:
            s = src.get_collection(name)
        except Exception:
            continue
        # The space must be repeated on the target: the default is L2, and a
        # collection created without this would silently change every
        # similarity score the tutor's memory search depends on.
        d = dst.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})

        got = s.get(
            where={"user_email": legacy_email},
            include=["documents", "metadatas", "embeddings"],
        )
        ids = got.get("ids") or []
        if not ids:
            moved[name] = 0
            continue

        for i in range(0, len(ids), CHROMA_BATCH):
            chunk = slice(i, i + CHROMA_BATCH)
            metas = [{**m, "user_email": new_email} for m in got["metadatas"][chunk]]
            d.upsert(
                ids=ids[chunk],
                documents=got["documents"][chunk],
                metadatas=metas,
                embeddings=got["embeddings"][chunk],
            )
        moved[name] = len(ids)
    return moved


def _chroma_counts(path: str) -> dict:
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    out = {}
    if not os.path.isdir(path):
        return out
    try:
        client = chromadb.PersistentClient(
            path=path,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
    except Exception:
        return out
    for name in COLLECTIONS:
        try:
            col = client.get_collection(name)
        except Exception:
            continue
        space = (col.metadata or {}).get("hnsw:space", "default(l2)")
        out[name] = (col.count(), space)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="actually move things (default is a dry run)")
    args = parser.parse_args()
    return Migration(apply=args.apply).run()


if __name__ == "__main__":
    sys.exit(main())
