"""Derivation documents — an ordered list of typed blocks, stored as JSON.

Layout:
    data/users/<safe_email>/derivations/_index.json    — list for the picker
    data/users/<safe_email>/derivations/<doc_id>.json  — one derivation
    data/users/<safe_email>/derivations/<doc_id>/      — plots copied per doc

A derivation is a SEQUENCE, not a canvas: blocks render top to bottom in array
order and carry no x/y/w/h. That is the whole difference from notes.py, and it
is deliberate — dropping the geometry is what makes this a document the tutor
can append to without deciding where anything goes.

Blocks are APPENDED, the way notebook cells are. The builder emits the intuition
first, then the steps, then a plot once its run finishes, so the pane fills in
while the learner watches instead of appearing all at once at the end.

Plot images are COPIED in here rather than linked. Run artifacts live in swept
scratch (RUN_DIR_TTL_SEC, and gone on reboot), which codejobs.py already calls
"correct for plots ... wrong for something a saved conversation still points at
next week". A doc that outlived its run directory would otherwise render holes.

Writes go through a temp file + os.replace, as notes.py and memory.py do:
uvicorn runs with reload=True and a restart mid-write must never leave a
truncated document. This module holds no mutable state for the same reason.
"""

import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone

from backend.config import settings
from backend.memory import _safe_email

logger = logging.getLogger(__name__)

USERS_DIR = os.path.join(settings.DATA_DIR, "users")

BLOCK_KINDS = ("text", "latex", "derivation", "matrix", "table", "plot")

# Mirrors notes.py. A document is rewritten whole on every append, so an
# unbounded field would make every later append slow.
MAX_BLOCK_CONTENT = 100_000
MAX_BLOCKS = 500
MAX_STEPS = 60

# Plots are copied in, so a runaway doc could otherwise grow without bound.
MAX_DOC_ARTIFACT_BYTES = 5 * 1024 * 1024

# Exactly what create_doc generates: "d_" + uuid4().hex[:10]. Anything else is
# not one of ours — see _safe_doc_id.
_DOC_ID_RE = re.compile(r"^d_[0-9a-f]{6,32}$")

# A plot filename inside a doc's own asset dir. Never a URL, never a path.
_PLOT_SRC_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def derivations_dir(email: str) -> str:
    return os.path.join(USERS_DIR, _safe_email(email), "derivations")


def _doc_path(email: str, doc_id: str) -> str:
    return os.path.join(derivations_dir(email), f"{_safe_doc_id(doc_id)}.json")


def doc_assets_dir(email: str, doc_id: str) -> str:
    return os.path.join(derivations_dir(email), _safe_doc_id(doc_id))


def _index_path(email: str) -> str:
    return os.path.join(derivations_dir(email), "_index.json")


def _ensure_dirs(email: str) -> str:
    d = derivations_dir(email)
    os.makedirs(d, exist_ok=True)
    return d


def _safe_doc_id(doc_id: str) -> str:
    """Reject anything that is not a doc id we minted.

    Ids reach this module from chat text (a ```derivation:<id> fence) and from
    query params, so a traversal attempt must never become a path.
    """
    doc_id = (doc_id or "").strip()
    if not _DOC_ID_RE.match(doc_id):
        raise ValueError(f"invalid derivation id: {doc_id!r}")
    return doc_id


def _write_json(path: str, data: dict) -> None:
    """Atomic write — temp file then rename, so a crash cannot truncate."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _read_json(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Unreadable derivation file {path}: {e}")
        return None


# One wave of blocks as the builder drafts it. Well above a real wave (the PCA
# derivation was ~20KB for all 31 blocks) while bounding a runaway file.
MAX_BLOCKS_FILE_BYTES = 400_000


def read_builder_blocks(path: str) -> list:
    """Load a wave of blocks the builder wrote to its scratch directory.

    Same route as a demo's html_path, and for the same reason: the blocks never
    travel as a tool argument, so a large wave cannot take the transport down.
    Shape only here — validate_blocks, via append_blocks, still owns content.
    """
    from backend.demos import read_scratch_file

    raw = read_scratch_file(path, MAX_BLOCKS_FILE_BYTES, "blocks_path")
    try:
        blocks = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"blocks_path is not valid JSON: {e}") from e
    if not isinstance(blocks, list):
        raise ValueError(
            "blocks_path must hold a JSON array of blocks, "
            f"got {type(blocks).__name__}"
        )
    return blocks


# --- Validation ---


def validate_blocks(blocks: list) -> list:
    """Normalize and reject structurally invalid blocks.

    Raises ValueError; callers turn that into a 400. Every block gets an id
    here rather than at the route, so a caller reaching this directly cannot
    persist a block the frontend then cannot address.
    """
    if not isinstance(blocks, list):
        raise ValueError("blocks must be a list")
    if len(blocks) > MAX_BLOCKS:
        raise ValueError(f"too many blocks (max {MAX_BLOCKS})")

    out = []
    for b in blocks:
        if not isinstance(b, dict):
            raise ValueError("each block must be an object")
        kind = b.get("kind")
        if kind not in BLOCK_KINDS:
            raise ValueError(f"unknown block kind: {kind!r}")

        clean = {
            "id": str(b.get("id") or f"b_{uuid.uuid4().hex[:8]}"),
            "kind": kind,
        }

        if kind == "derivation":
            clean["steps"] = _validate_steps(b.get("steps"))
            clean["title"] = str(b.get("title") or "")[:200]
        elif kind == "matrix":
            clean["rows"] = _validate_rows(b.get("rows"))
            clean["label"] = str(b.get("label") or "")[:200]
            # Free text like "R2 <- R2 - 1/2 R1", shown beside the matrix.
            ops = b.get("ops")
            clean["ops"] = [str(o)[:200] for o in ops][:MAX_STEPS] if isinstance(ops, list) else []
        elif kind == "table":
            clean["headers"] = [str(h)[:200] for h in (b.get("headers") or [])][:20]
            clean["rows"] = _validate_rows(b.get("rows"), stringify=True)
            clean["caption"] = str(b.get("caption") or "")[:300]
        elif kind == "plot":
            # Filename inside the doc's own asset dir — never a URL, never a
            # path. add_plot() is what puts a file there.
            src = str(b.get("src") or "")
            if not _PLOT_SRC_RE.match(src):
                raise ValueError(f"invalid plot src: {src!r}")
            clean["src"] = src
            clean["caption"] = str(b.get("caption") or "")[:300]
        else:  # text, latex
            content = b.get("content") or ""
            if not isinstance(content, str):
                raise ValueError("block content must be a string")
            if len(content) > MAX_BLOCK_CONTENT:
                raise ValueError(f"block content exceeds {MAX_BLOCK_CONTENT} chars")
            clean["content"] = content

        out.append(clean)
    return out


def _validate_steps(steps) -> list:
    """A derivation step is {expr, reason, verified}.

    `reason` is not optional by convention — a step without one shows the
    learner what happened but not why, which is the thing this whole surface
    exists to fix. It is allowed to be empty here so a partial write is not
    rejected, but the prompt asks for it every time.
    """
    if not isinstance(steps, list):
        raise ValueError("derivation steps must be a list")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"too many steps (max {MAX_STEPS})")

    out = []
    for s in steps:
        if not isinstance(s, dict):
            raise ValueError("each step must be an object")
        expr = s.get("expr")
        if not isinstance(expr, str) or not expr.strip():
            raise ValueError("each step needs an expr")
        if len(expr) > MAX_BLOCK_CONTENT:
            raise ValueError(f"step expr exceeds {MAX_BLOCK_CONTENT} chars")
        out.append({
            "expr": expr,
            "reason": str(s.get("reason") or "")[:300],
            # None = never checked, True/False = sympy said so. Three states,
            # because "unchecked" and "checked and wrong" must not look alike.
            "verified": s.get("verified") if s.get("verified") in (True, False) else None,
            "check": str(s.get("check") or "")[:500],
        })
    return out


def _validate_rows(rows, stringify: bool = False) -> list:
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    if len(rows) > 100:
        raise ValueError("too many rows (max 100)")
    out = []
    for r in rows:
        if not isinstance(r, list):
            raise ValueError("each row must be a list")
        if len(r) > 20:
            raise ValueError("too many columns (max 20)")
        out.append([str(c)[:500] for c in r] if stringify else [str(c)[:500] for c in r])
    return out


# --- Index ---


def _index_entry(doc: dict) -> dict:
    return {
        "id": doc["id"],
        "title": doc.get("title") or "Untitled derivation",
        "concept_id": doc.get("concept_id") or "",
        "updated_at": doc.get("updated_at"),
        "blocks": len(doc.get("blocks") or []),
    }


def list_docs(email: str) -> list[dict]:
    return _read_json(_index_path(email)) or []


def _touch_index(email: str, doc: dict) -> None:
    entries = [e for e in list_docs(email) if e.get("id") != doc["id"]]
    entries.insert(0, _index_entry(doc))
    _write_json(_index_path(email), entries[:200])


# --- CRUD ---


def create_doc(email: str, title: str = "", concept_id: str = "",
               session_id: str | None = None) -> dict:
    _ensure_dirs(email)
    now = _now()
    doc = {
        "id": f"d_{uuid.uuid4().hex[:10]}",
        "title": title or "Untitled derivation",
        "concept_id": (concept_id or "").strip(),
        "created_at": now,
        "updated_at": now,
        "session_id": session_id,
        # Set once the builder's session is known; this is what lets a
        # follow-up edit resume that session rather than regenerate.
        "builder_session_id": None,
        "blocks": [],
    }
    _write_json(_doc_path(email, doc["id"]), doc)
    _touch_index(email, doc)
    return doc


def get_doc(email: str, doc_id: str) -> dict | None:
    try:
        return _read_json(_doc_path(email, doc_id))
    except ValueError:
        return None


def append_blocks(email: str, doc_id: str, blocks: list) -> dict | None:
    """Add blocks to the end. Returns the saved doc, or None if it is missing.

    Append rather than replace: the builder writes the intuition, then the
    steps, then a plot as its run finishes, and each call must not discard what
    the previous one wrote.
    """
    doc = get_doc(email, doc_id)
    if doc is None:
        return None

    clean = validate_blocks(blocks)
    existing = doc.get("blocks") or []
    if len(existing) + len(clean) > MAX_BLOCKS:
        raise ValueError(f"document would exceed {MAX_BLOCKS} blocks")

    doc["blocks"] = existing + clean
    doc["updated_at"] = _now()
    _write_json(_doc_path(email, doc_id), doc)
    _touch_index(email, doc)
    return doc


def from_builder(blocks: list) -> list:
    """Drop what a builder must not decide: block ids and verification stamps.

    A builder rewriting a wave copied an existing block's "id" into the new
    one, and the store kept it — the doc ended up with the same id three
    times, so derivation_verify could only ever reach the first. And a step
    arriving with "verified": true would show the learner a green tick sympy
    never gave. Both are the server's to assign.
    """
    out = []
    for b in blocks:
        if not isinstance(b, dict):
            out.append(b)                      # validate_blocks rejects it
            continue
        clean = {k: v for k, v in b.items() if k != "id"}
        if isinstance(clean.get("steps"), list):
            clean["steps"] = [
                {k: v for k, v in s.items() if k not in ("verified", "check")}
                if isinstance(s, dict) else s
                for s in clean["steps"]
            ]
        out.append(clean)
    return out


def copy_doc_to_scratch(email: str, doc_id: str, dest_dir: str) -> str:
    """Write a doc's blocks where the builder can edit them. Returns the path.

    The derivation counterpart of demos.copy_demo_to_scratch, for the same
    reason: the builder finds the block with Grep/Read, patches it with Edit,
    and publishes with derivation_update — the document never travels through
    a tool call. Without this the only way to correct a block was to append a
    corrected copy below the wrong one; a live build appended the same
    derivation four times.

    One key per line (indent=2), so Grep lands on the line to change and Edit
    can match it exactly.
    """
    doc = get_doc(email, doc_id)
    if doc is None:
        raise ValueError(f"No such derivation: {doc_id!r}")

    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{_safe_doc_id(doc_id)}.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(doc.get("blocks") or [], f, indent=2, ensure_ascii=False)
    return dest


def replace_blocks(email: str, doc_id: str, blocks: list) -> tuple[dict, list]:
    """Replace a doc's whole block list with an edited copy.

    Returns (doc, block ids whose steps need derivation_verify).

    The file comes from a builder, so the server decides three things itself:
      - ids: a block keeps its id only if that id exists in the doc and is
        not already taken earlier in this list. Anything else gets a new one.
      - verification: a derivation block keeps the STORED stamps only when its
        steps are unchanged. Edited steps go back to unchecked; stamps in the
        file are never trusted.
      - plots: only a plot already in the doc may appear, since add_plot is
        what puts the image file in place.
    """
    doc = get_doc(email, doc_id)
    if doc is None:
        raise ValueError(f"No such derivation: {doc_id!r}")
    if not isinstance(blocks, list):
        raise ValueError("blocks must be a list")

    stored = {b.get("id"): b for b in (doc.get("blocks") or [])}
    stored_plots = {b.get("src") for b in stored.values() if b.get("kind") == "plot"}

    wanted_ids = [b.get("id") if isinstance(b, dict) else None for b in blocks]
    clean = validate_blocks(from_builder(blocks))

    used: set = set()
    needs_verify = []
    for b, wanted in zip(clean, wanted_ids):
        if wanted in stored and wanted not in used:
            b["id"] = wanted
        used.add(b["id"])

        if b["kind"] == "plot" and b.get("src") not in stored_plots:
            raise ValueError(
                f"plot {b.get('src')!r} is not in this derivation — add plots "
                f"with derivation_plot"
            )

        if b["kind"] == "derivation":
            old = stored.get(b["id"])
            old_exprs = [s.get("expr") for s in (old or {}).get("steps") or []]
            new_exprs = [s.get("expr") for s in b["steps"]]
            if old is not None and old.get("kind") == "derivation" and old_exprs == new_exprs:
                b["steps"] = [
                    {**s, "verified": o.get("verified"), "check": o.get("check", "")}
                    for s, o in zip(b["steps"], old["steps"])
                ]
            else:
                needs_verify.append(b["id"])

    doc["blocks"] = clean
    doc["updated_at"] = _now()
    _write_json(_doc_path(email, doc_id), doc)
    _touch_index(email, doc)
    return doc, needs_verify


def update_block(email: str, doc_id: str, block_id: str, **fields) -> dict | None:
    """Replace fields on one block, found by id.

    Used to stamp verification onto steps after sympy has run, without
    rewriting blocks the builder already got right.
    """
    doc = get_doc(email, doc_id)
    if doc is None:
        return None

    for b in doc.get("blocks") or []:
        if b.get("id") == block_id:
            merged = {**b, **{k: v for k, v in fields.items() if v is not None}}
            b.clear()
            b.update(validate_blocks([merged])[0])
            b["id"] = block_id          # validate_blocks would mint a new one
            break
    else:
        return None

    doc["updated_at"] = _now()
    _write_json(_doc_path(email, doc_id), doc)
    return doc


def save_meta(email: str, doc_id: str, **fields) -> dict | None:
    """Partial update of document-level fields only."""
    doc = get_doc(email, doc_id)
    if doc is None:
        return None
    for key in ("title", "concept_id", "builder_session_id", "session_id"):
        if fields.get(key) is not None:
            doc[key] = fields[key]
    doc["updated_at"] = _now()
    _write_json(_doc_path(email, doc_id), doc)
    _touch_index(email, doc)
    return doc


def delete_doc(email: str, doc_id: str) -> bool:
    try:
        path = _doc_path(email, doc_id)
    except ValueError:
        return False
    if not os.path.isfile(path):
        return False
    os.remove(path)
    shutil.rmtree(doc_assets_dir(email, doc_id), ignore_errors=True)
    entries = [e for e in list_docs(email) if e.get("id") != doc_id]
    _write_json(_index_path(email), entries)
    return True


# --- Plots ---


def add_plot(email: str, doc_id: str, source_path: str, caption: str = "") -> dict:
    """Copy a plot out of scratch and into the doc, then append a plot block.

    The copy is the point. Run artifacts live under SCRATCH_ROOT, which
    _sweep_run_dirs clears past RUN_DIR_TTL_SEC and which is gone on reboot, so
    a doc that merely linked /api/code/artifacts/... would render a hole within
    the hour. This is the same move codejobs.py makes for verified source.
    """
    doc = get_doc(email, doc_id)
    if doc is None:
        raise ValueError(f"no such derivation: {doc_id}")

    if not os.path.isfile(source_path):
        raise ValueError(f"no such plot file: {source_path}")

    ext = os.path.splitext(source_path)[1].lower()
    if ext not in (".svg", ".png", ".jpg", ".jpeg", ".webp"):
        raise ValueError(f"unsupported plot type: {ext}")

    size = os.path.getsize(source_path)
    assets = doc_assets_dir(email, doc_id)
    os.makedirs(assets, exist_ok=True)

    used = 0
    for name in os.listdir(assets):
        try:
            used += os.path.getsize(os.path.join(assets, name))
        except OSError:
            pass
    if used + size > MAX_DOC_ARTIFACT_BYTES:
        raise ValueError(
            f"derivation artifacts would exceed {MAX_DOC_ARTIFACT_BYTES} bytes"
        )

    n = len([f for f in os.listdir(assets)]) + 1
    name = f"plot_{n:03d}{ext}"
    shutil.copy2(source_path, os.path.join(assets, name))

    return append_blocks(email, doc_id, [
        {"kind": "plot", "src": name, "caption": caption}
    ])


# --- Digest for the tutor's prompt ---


MAX_DIGEST_STEPS = 24


def derivation_digest(email: str, doc_id: str,
                      selection: dict | None = None) -> str | None:
    """A compact text form of a derivation, for the tutor's prompt.

    Simpler than demo_digest: a derivation IS structured, so this is the real
    content rather than a summary of markup. Still bounded — anything placed in
    prompt text lands in the SDK transcript and is re-sent on every later turn,
    so a 60-step document must not become a permanent per-turn cost.

    Verification state is included deliberately. "why is step 4 marked
    unverified" is a question the learner will actually ask, and the tutor
    cannot answer it without seeing the flags.
    """
    doc = get_doc(email, doc_id)
    if doc is None:
        return None

    lines = [f"Derivation: {doc.get('title') or 'Untitled'}"]
    if doc.get("concept_id"):
        lines.append(f"Concept: {doc['concept_id']}")

    steps_shown = 0
    for b in doc.get("blocks") or []:
        kind = b.get("kind")
        if kind == "text":
            lines.append(f"\n{b.get('content', '')[:600]}")
        elif kind == "latex":
            lines.append(f"\nFormula: {b.get('content', '')[:300]}")
        elif kind == "derivation":
            lines.append(f"\nSteps ({b.get('title') or 'derivation'}):")
            for i, s in enumerate(b.get("steps") or [], 1):
                if steps_shown >= MAX_DIGEST_STEPS:
                    lines.append("  … (more steps not listed)")
                    break
                mark = {True: "verified", False: "WRONG", None: "unchecked"}[
                    s.get("verified") if s.get("verified") in (True, False) else None
                ]
                reason = s.get("reason") or ""
                lines.append(f"  {i}. {s.get('expr', '')[:200]}"
                             f"{'  — ' + reason[:120] if reason else ''}  [{mark}]")
                steps_shown += 1
        elif kind == "matrix":
            lines.append(f"\nMatrix {b.get('label') or ''}: "
                         f"{len(b.get('rows') or [])} rows"
                         + (f", ops: {'; '.join(b.get('ops') or [])[:200]}"
                            if b.get("ops") else ""))
        elif kind == "table":
            lines.append(f"\nTable: {', '.join(b.get('headers') or [])[:200]}")
        elif kind == "plot":
            lines.append(f"\nPlot: {b.get('caption') or b.get('src')}")

    if selection and selection.get("text"):
        lines.append(f"\nThe learner highlighted: \"{selection['text'][:400]}\"")

    return "\n".join(lines)


# ── bare-maths lint ───────────────────────────────────────────────────────
#
# The pane typesets only $$…$$. A builder that writes "(1/2)||w||^2" or
# "x_11 = 2" in a step's reason ships it as raw text — seen on a live SVM
# derivation where every reason read like source code. The skill says to wrap
# symbols; this catches the misses while the builder can still fix them,
# instead of after the learner has the page.
#
# Heuristic and deliberately loud rather than clever: it is a warning returned
# to the builder, never a rejection, so a false positive costs one glance.
_MATH_SEGMENT = re.compile(r"\$\$.*?\$\$|`[^`]*`", re.S)
_BARE_MATH = re.compile(
    r"[A-Za-z]_(?:\{|[A-Za-z0-9])"        # w_1, x_{11}, a_i
    r"|[A-Za-z0-9)}\]]\^"                  # w^2, (…)^2
    r"|\|\|"                               # ||w||
    r"|\\(?:frac|sum|cdot|alpha|beta|gamma|lambda|sigma|mu|theta|partial|nabla|ge|le|geq|leq)\b"
)


def _bare_math_hits(text: str) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    outside = _MATH_SEGMENT.sub(" ", text)
    hits = []
    for m in _BARE_MATH.finditer(outside):
        a, b = max(0, m.start() - 12), min(len(outside), m.end() + 12)
        hits.append(" ".join(outside[a:b].split()))
    return hits


def bare_math_warnings(blocks: list, limit: int = 12) -> list[str]:
    """Places where maths sits outside $$…$$ and will render as raw text.

    Checks prose-bearing fields only: text content, step reasons, captions and
    table cells. Step exprs and latex blocks are typeset whole and never need
    delimiters, so they are skipped.
    """
    out = []

    def add(where, text):
        for hit in _bare_math_hits(text)[:2]:
            out.append(f"{where}: …{hit}…")

    for b in blocks or []:
        bid, kind = b.get("id", "?"), b.get("kind")
        if kind == "text":
            add(f"{bid} text", b.get("content"))
        elif kind == "derivation":
            for n, s in enumerate(b.get("steps") or [], 1):
                add(f"{bid} step {n} reason", s.get("reason"))
        if kind in ("table", "matrix"):
            for h in b.get("headers") or []:
                add(f"{bid} header", h)
            for r in b.get("rows") or []:
                for c in r if isinstance(r, list) else []:
                    add(f"{bid} cell", c)
        if b.get("caption"):
            add(f"{bid} caption", b.get("caption"))
        if len(out) >= limit:
            break
    return out[:limit]
