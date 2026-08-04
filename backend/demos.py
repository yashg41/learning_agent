"""Interactive HTML demos the tutor builds to explain a concept visually.

Layout:
    data/users/<safe_email>/demos/_index.json   — list for the picker
    data/users/<safe_email>/demos/<demo_id>.html — the demo itself
    data/users/<safe_email>/demos/<demo_id>.json — its metadata

The HTML is kept out of the conversation transcript on purpose: a good demo is
several hundred lines, and storing it inline would re-send the whole thing to
the model on every subsequent turn and slow session replay. The transcript gets
{demo_id, title}; this module holds the rest. Same reasoning as attachments,
where base64 is never persisted (see episodic.save_attachment).

Demos are rendered in a sandboxed iframe with `allow-scripts` but NOT
`allow-same-origin`, so demo JS runs in an opaque origin and cannot reach the
app's storage or API. `validate_demo_html` is therefore about catching mistakes
that would silently break a demo — chiefly external subresources, which the
sandbox blocks — rather than being the security boundary itself.

Writes go through a temp file + os.replace (as memory.py does): uvicorn runs
with reload=True, and a restart mid-write must not leave a truncated demo.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

from backend.config import settings
from backend.memory import _safe_email

logger = logging.getLogger(__name__)

USERS_DIR = os.path.join(settings.DATA_DIR, "users")

# A self-contained interactive demo runs 150-400 lines. The cap is generous
# enough for an elaborate one while stopping a runaway generation from filling
# the disk.
MAX_DEMO_BYTES = 400_000
MAX_DEMOS_PER_USER = 100

# Anything the sandbox would refuse to load. Matching on the attribute keeps
# this from firing on a URL that merely appears in visible text or a comment.
_EXTERNAL_REF = re.compile(
    r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//""",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def demos_dir(email: str) -> str:
    return os.path.join(USERS_DIR, _safe_email(email), "demos")


def _ensure_dirs(email: str) -> str:
    d = demos_dir(email)
    os.makedirs(d, exist_ok=True)
    return d


def _html_path(email: str, demo_id: str) -> str:
    return os.path.join(demos_dir(email), f"{demo_id}.html")


def _meta_path(email: str, demo_id: str) -> str:
    return os.path.join(demos_dir(email), f"{demo_id}.json")


def _index_path(email: str) -> str:
    return os.path.join(demos_dir(email), "_index.json")


def _write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_json(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Unreadable demo file {path}: {e}")
        return None


# --- Validation ---


def validate_demo_html(html: str) -> str:
    """Reject HTML that would not work as a sandboxed demo.

    Raises ValueError with a message aimed at the model, since it is the
    caller — "you referenced a CDN" is actionable, "invalid input" is not.
    Returns the html unchanged when it passes.
    """
    if not isinstance(html, str) or not html.strip():
        raise ValueError("html is empty")

    size = len(html.encode("utf-8"))
    if size > MAX_DEMO_BYTES:
        # Report both in bytes when the KB figures would round to the same
        # number — "390KB is over the 390KB limit" reads as a bug.
        raise ValueError(
            f"Demo is {size} bytes, over the {MAX_DEMO_BYTES} byte limit. "
            "Simplify it to a single concept."
        )

    low = html.lower()
    if "<html" not in low or "<body" not in low:
        raise ValueError(
            "html must be a complete document with <html> and <body> tags."
        )

    m = _EXTERNAL_REF.search(html)
    if m:
        raise ValueError(
            "Demo references an external URL "
            f"({html[m.start():m.start() + 60].strip()!r}). The sandbox blocks "
            "network requests, so it would render blank. Inline all CSS and JS, "
            "and draw with inline SVG or canvas instead of loading images."
        )

    missing = _missing_handlers(html)
    if missing:
        raise ValueError(
            f"onclick calls {', '.join(sorted(missing))} but no such function is "
            "defined at top level. Inline handlers only see globals — a function "
            "inside DOMContentLoaded or a type=\"module\" script is invisible to "
            "them, and the button throws on first click."
        )

    return html


# Literal-id lookups: getElementById("x") — the id must exist in the markup.
_GET_BY_ID = re.compile(r"""getElementById\(\s*["']([\w-]+)["']\s*\)""")
_ID_ATTR = re.compile(r"""\bid\s*=\s*["']([\w-]+)["']""")
_ONCLICK_FN = re.compile(r"""\bon\w+\s*=\s*["']\s*(\w+)\s*\(""")
_FN_DECL = re.compile(r"""\bfunction\s+(\w+)\s*\(""")
_FN_ASSIGN = re.compile(r"""\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:function\b|\()""")


def _missing_handlers(html: str) -> set[str]:
    """Inline-handler names with no function of that name anywhere.

    Catches a misspelled or entirely absent handler. It does NOT model scope,
    so a function nested inside DOMContentLoaded still counts as defined —
    detecting that needs a parser, and a regex that guessed at scope would
    reject working demos. The skill covers the scope rule; this catches the
    plain typo.
    """
    called = set(_ONCLICK_FN.findall(html))
    if not called:
        return set()
    defined = set(_FN_DECL.findall(html)) | set(_FN_ASSIGN.findall(html))
    # Browser built-ins that are legitimately called from a handler.
    builtins = {"alert", "confirm", "print", "open", "close", "history"}
    return {c for c in called - defined - builtins}


def missing_element_ids(html: str) -> set[str]:
    """Literal getElementById ids that never appear as an id attribute.

    Only sees literals. The failure actually observed in the wild was an id
    built by concatenation — `getElementById('btn-' + key)` against markup
    written as `id="btn-s1"` — which no static check of this kind can resolve,
    since `key` is only known at runtime. `suspicious_id_prefixes` covers that
    shape; this covers the simpler literal mismatch.

    A warning rather than a rejection either way: ids can legitimately be
    created at runtime, and refusing a working demo is worse than flagging a
    suspect one.
    """
    wanted = set(_GET_BY_ID.findall(html))
    if not wanted:
        return set()
    return wanted - set(_ID_ATTR.findall(html))


# Note on what is NOT checked here: the failure seen in practice was
# `getElementById('btn-' + scenario)` against markup written `id="btn-s1"`.
# Static analysis cannot resolve it — the suffix only exists at runtime, and
# the literal prefix `btn-` does match the real ids, so no regex separates the
# broken case from a correct one. That class of bug is addressed where it can
# be: the skill tells the model to derive ids from one source and to guard the
# lookup, and the frame's error relay reports the throw to the learner
# immediately rather than leaving the demo silently inert.


# --- Index ---


def _index_entry(meta: dict) -> dict:
    return {
        "id": meta["id"],
        "title": meta.get("title") or "Untitled demo",
        "concept_id": meta.get("concept_id"),
        "summary": meta.get("summary", ""),
        "created_at": meta.get("created_at"),
    }


def rebuild_index(email: str) -> list[dict]:
    """Regenerate _index.json by scanning the directory.

    The index is a derived cache; the per-demo .json files are the truth.
    """
    d = _ensure_dirs(email)
    entries = []
    for name in os.listdir(d):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        meta = _read_json(os.path.join(d, name))
        if meta and meta.get("id"):
            entries.append(_index_entry(meta))
    entries.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    _write_atomic(_index_path(email), json.dumps({"demos": entries}, indent=2))
    return entries


def list_demos(email: str) -> list[dict]:
    _ensure_dirs(email)
    data = _read_json(_index_path(email))
    if data is None:
        return rebuild_index(email)
    return data.get("demos", [])


def _touch_index(email: str, meta: dict) -> None:
    entries = [e for e in list_demos(email) if e.get("id") != meta["id"]]
    entries.insert(0, _index_entry(meta))
    # Oldest demos fall off rather than accumulating without bound.
    dropped = entries[MAX_DEMOS_PER_USER:]
    entries = entries[:MAX_DEMOS_PER_USER]
    for old in dropped:
        _delete_files(email, old["id"])
    _write_atomic(_index_path(email), json.dumps({"demos": entries}, indent=2))


# --- CRUD ---


def save_demo(
    email: str,
    title: str,
    html: str,
    concept_id: str = "",
    summary: str = "",
) -> dict:
    """Validate and store a demo. Returns its metadata (never the html)."""
    html = validate_demo_html(html)
    _ensure_dirs(email)

    demo_id = f"d_{uuid.uuid4().hex[:10]}"
    meta = {
        "id": demo_id,
        "title": (title or "Untitled demo").strip()[:120],
        "concept_id": (concept_id or "").strip(),
        "summary": (summary or "").strip()[:300],
        "created_at": _now(),
        "bytes": len(html.encode("utf-8")),
    }

    _write_atomic(_html_path(email, demo_id), html)
    _write_atomic(_meta_path(email, demo_id), json.dumps(meta, indent=2))
    _touch_index(email, meta)
    logger.info(f"Demo saved: {demo_id} ({meta['bytes']} bytes) for {email}")
    return meta


def resolve_demo(email: str, title: str = "", concept_id: str = "") -> dict | None:
    """Look up a saved demo from what a chat tool-card knows about it.

    A tool-card records the arguments of the save_demo call — title and
    concept_id — but not the id, which only exists once the demo is stored.
    The SDK gives the tool handler no way to see its own tool_use_id, so
    those arguments are the only link back.

    The index is newest-first, so a repeated title resolves to the most
    recent build of it, which is what "open the demo from this message"
    should mean.
    """
    entries = list_demos(email)
    if title:
        for e in entries:
            if e.get("title") == title:
                return e
    if concept_id:
        for e in entries:
            if e.get("concept_id") == concept_id:
                return e
    return None


def get_demo(email: str, demo_id: str) -> dict | None:
    """Metadata plus html, or None if missing."""
    meta = _read_json(_meta_path(email, demo_id))
    if meta is None:
        return None
    path = _html_path(email, demo_id)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        html = f.read()
    return {**meta, "html": html}


def _delete_files(email: str, demo_id: str) -> bool:
    found = False
    for path in (_html_path(email, demo_id), _meta_path(email, demo_id)):
        if os.path.isfile(path):
            os.remove(path)
            found = True
    return found


def delete_demo(email: str, demo_id: str) -> bool:
    if not _delete_files(email, demo_id):
        return False
    rebuild_index(email)
    return True
