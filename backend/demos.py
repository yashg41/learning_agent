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
import threading
import uuid
from datetime import datetime, timezone

from backend.config import settings
from backend.memory import _safe_email

logger = logging.getLogger(__name__)

USERS_DIR = os.path.join(settings.DATA_DIR, "users")

# Serialises read-modify-write on _index.json. Threading rather than asyncio:
# these helpers are plain sync functions called from async handlers, and a
# threading lock is correct in both contexts.
_INDEX_LOCK = threading.Lock()

# A self-contained interactive demo runs 150-400 lines. The cap is generous
# enough for an elaborate one while stopping a runaway generation from filling
# the disk.
MAX_DEMO_BYTES = 400_000
MAX_DEMOS_PER_USER = 100

# Caps for what the tutor is told about a demo. A demo's html is ~4200 tokens
# and would be re-sent on every later turn of the session, so the tutor gets a
# digest instead (demo_digest) and reads real source only through
# get_demo_source, which slices.
MAX_DIGEST_NAMES = 40
# Shown when no demo matches the current session — see format_session_demos.
DEMO_FALLBACK_COUNT = 5

# Exactly what save_demo generates: "d_" + uuid4().hex[:10]. Anything else is
# not one of ours — see _safe_demo_id.
_DEMO_ID_RE = re.compile(r"^d_[0-9a-f]{6,32}$")

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


def _safe_demo_id(demo_id: str) -> str:
    """Reject anything that is not a generated demo id.

    Ids are interpolated straight into a path, and they do NOT all arrive from
    our own UI: the MCP tools pass ids the model produced, where a `..` segment
    would read a file outside the learner's demos directory. Matches the
    generator in save_demo (`d_` + uuid4 hex).
    """
    if not _DEMO_ID_RE.match(demo_id or ""):
        raise ValueError(f"invalid demo id: {demo_id!r}")
    return demo_id


def _html_path(email: str, demo_id: str) -> str:
    return os.path.join(demos_dir(email), f"{_safe_demo_id(demo_id)}.html")


def _meta_path(email: str, demo_id: str) -> str:
    return os.path.join(demos_dir(email), f"{_safe_demo_id(demo_id)}.json")


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
# Object-literal shorthand and class methods: `reset() {` / `reset: function`.
_METHOD_DEF = re.compile(r"""(?:^|[{,;\s])(\w+)\s*(?:\([^)]*\)\s*\{|:\s*(?:async\s*)?(?:function\b|\())""")
# Also matches single-parameter arrows written without parentheses
# (`const reset = e => {}`), which the older `function|\(` form missed and
# therefore reported as an undefined handler.
_FN_ASSIGN = re.compile(
    r"""\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:function\b|\(|\w+\s*=>)"""
)

# Names that resolve at runtime but are not declared in the document. Rejecting
# these would refuse working demos — requestAnimationFrame in particular is
# actively recommended in references/patterns.md.
_HANDLER_BUILTINS = {
    "alert", "confirm", "prompt", "print", "open", "close", "history",
    "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "requestAnimationFrame", "cancelAnimationFrame",
    "parseInt", "parseFloat", "Number", "String", "Boolean", "Math", "JSON",
    "console", "window", "document", "event", "this",
}


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
    # Object methods and class fields: `onclick="app.reset()"` or
    # `onclick="obj.fn()"`. The name is a property, not a top-level function,
    # so absence from `defined` proves nothing.
    defined |= set(_METHOD_DEF.findall(html))
    return {c for c in called - defined - _HANDLER_BUILTINS}


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
    # Deliberately no html — the index is fetched for every picker render and
    # is also the cheapest thing to show the tutor, so it must stay small.
    return {
        "id": meta["id"],
        "title": meta.get("title") or "Untitled demo",
        "concept_id": meta.get("concept_id"),
        "summary": meta.get("summary", ""),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at") or meta.get("created_at"),
        "version": meta.get("version") or 1,
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
    # Read-modify-write on a shared file. Demos are now built by background
    # jobs that can finish at the same moment, so two concurrent saves could
    # otherwise each write an index missing the other's entry.
    with _INDEX_LOCK:
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
    builder_session_id: str = "",
    chat_session_id: str = "",
) -> dict:
    """Validate and store a demo. Returns its metadata (never the html).

    builder_session_id links the demo to the SDK session that produced it, so
    a later "add a reset button" resumes that session — which still holds both
    the conversation it was forked from and the HTML it wrote — instead of
    regenerating the whole document from scratch. See update_demo.

    chat_session_id records which learning session asked for it, so the tutor's
    prompt can list this session's demos (see format_session_demos).
    """
    html = validate_demo_html(html)
    _ensure_dirs(email)

    demo_id = f"d_{uuid.uuid4().hex[:10]}"
    meta = {
        "id": demo_id,
        "title": (title or "Untitled demo").strip()[:120],
        "concept_id": (concept_id or "").strip(),
        "summary": (summary or "").strip()[:300],
        "created_at": _now(),
        "updated_at": _now(),
        "version": 1,
        "builder_session_id": (builder_session_id or "").strip(),
        "chat_session_id": (chat_session_id or "").strip(),
        "bytes": len(html.encode("utf-8")),
    }

    _write_atomic(_html_path(email, demo_id), html)
    _write_atomic(_meta_path(email, demo_id), json.dumps(meta, indent=2))
    _touch_index(email, meta)
    logger.info(f"Demo saved: {demo_id} ({meta['bytes']} bytes) for {email}")
    return meta


def update_demo(
    email: str,
    demo_id: str,
    html: str,
    title: str = "",
    summary: str = "",
    builder_session_id: str = "",
) -> dict | None:
    """Replace a demo's html in place, keeping its id. None if it's gone.

    Refinement must not mint a new id. save_demo always generates one, so
    without this every "make the slider go to 0.5" would leave a duplicate
    entry behind and churn the MAX_DEMOS_PER_USER eviction — the user would
    watch their older demos silently fall off the list while editing one.
    Keeping the id stable also keeps every existing chat card's Open button
    pointing at the demo it was created for.
    """
    existing = get_demo_meta(email, demo_id)
    if existing is None:
        return None

    html = validate_demo_html(html)

    meta = {
        **existing,
        "title": (title or existing.get("title") or "Untitled demo").strip()[:120],
        "summary": (summary or existing.get("summary", "")).strip()[:300],
        "updated_at": _now(),
        "version": int(existing.get("version") or 1) + 1,
        "bytes": len(html.encode("utf-8")),
    }
    if builder_session_id:
        meta["builder_session_id"] = builder_session_id.strip()

    _write_atomic(_html_path(email, demo_id), html)
    _write_atomic(_meta_path(email, demo_id), json.dumps(meta, indent=2))
    _touch_index(email, meta)
    logger.info(
        f"Demo updated: {demo_id} v{meta['version']} "
        f"({meta['bytes']} bytes) for {email}"
    )
    return meta


def get_demo_meta(email: str, demo_id: str) -> dict | None:
    """Metadata only, without reading the html off disk.

    get_demo() loads the whole document — up to MAX_DEMO_BYTES — which is
    wasted work for callers that only want a title or version.

    A malformed id reads as "no such demo" rather than raising: callers treat
    None as a 404, and a bad id is not a server error.
    """
    try:
        return _read_json(_meta_path(email, demo_id))
    except ValueError:
        return None


def _demo_line(entry: dict, meta: dict) -> str:
    version = meta.get("version") or 1
    suffix = f" (v{version})" if version > 1 else ""
    return f"- {entry['title']}{suffix} — demo_id: {entry['id']}"


def format_session_demos(email: str, session_id: str | None) -> str:
    """Demos the tutor should know about without being asked.

    Prefers demos built in THIS session, then falls back to the most recent
    few. The fallback matters because a strict session match silently hides
    demos the learner is looking at right now: anything built before
    chat_session_id existed has no such field, and an edit-branch fork or a
    restore-from-memory mints a new session id that no existing demo carries.

    Titles and ids only. Summaries are deliberately excluded — they are capped
    at 300 chars and this string is in the system prompt, so it would be paid
    on every turn including the many with no demo intent. The one demo the
    learner actually references gets its summary via demo_digest instead.
    """
    scoped: list[str] = []
    recent: list[str] = []

    # list_demos is newest-first (rebuild_index sorts by created_at reverse).
    for entry in list_demos(email):
        meta = _read_json(_meta_path(email, entry["id"]))
        if not meta:
            continue
        line = _demo_line(entry, meta)
        if session_id and meta.get("chat_session_id") == session_id:
            scoped.append(line)
        else:
            recent.append(line)

    if scoped:
        return "\n".join(scoped)
    if not recent:
        return ""
    return "\n".join(
        ["(none built in this session; most recent overall:)"]
        + recent[:DEMO_FALLBACK_COUNT]
    )


def demo_digest(email: str, demo_id: str, selection: dict | None = None) -> str | None:
    """A compact structural description of a demo, for the tutor's prompt.

    Deliberately NOT the html. A real demo runs ~17KB (~4200 tokens), and
    anything placed in prompt text lands in the SDK transcript and is re-sent
    on every later turn of the session — one demo would cost ~63k tokens over
    a 20-turn conversation. This is ~200 tokens and carries the part that
    actually answers "why doesn't the reset button work", because that answer
    is nearly always an id or handler mismatch the analysers below detect.

    When the tutor needs the real code it calls get_demo_source, which slices
    and caps rather than dumping.
    """
    meta = get_demo_meta(email, demo_id)
    if meta is None:
        return None
    path = _html_path(email, demo_id)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        html = f.read()

    version = meta.get("version") or 1
    lines = [
        f'Demo "{meta.get("title", "")}" (demo_id: {demo_id}, v{version})'
    ]
    if meta.get("summary"):
        lines.append(f'Summary: {meta["summary"]}')

    # What the learner highlighted, if anything — the "this bit here" pointer.
    if selection:
        text = (selection.get("text") or "").strip()
        if text:
            lines.append(f'They highlighted: "{text[:300]}"')
        anchor = []
        if selection.get("tag"):
            anchor.append(f'<{selection["tag"]}>')
        for demo_element_id in (selection.get("ids") or [])[:5]:
            anchor.append(f'id="{demo_element_id}"')
        if anchor:
            lines.append("Selection is inside: " + " ".join(anchor))

    ids = sorted(set(_ID_ATTR.findall(html)))[:MAX_DIGEST_NAMES]
    handlers = sorted(set(_ONCLICK_FN.findall(html)))[:MAX_DIGEST_NAMES]
    functions = sorted(
        set(_FN_DECL.findall(html)) | set(_FN_ASSIGN.findall(html))
    )[:MAX_DIGEST_NAMES]

    if ids:
        lines.append("Element ids: " + ", ".join(ids))
    if handlers:
        lines.append("Inline handlers call: " + ", ".join(handlers))
    if functions:
        lines.append("Functions defined: " + ", ".join(functions))

    missing_ids = missing_element_ids(html)
    if missing_ids:
        lines.append(
            "WARNING getElementById looks up ids that no element has: "
            + ", ".join(sorted(missing_ids))
        )
    missing_fns = _missing_handlers(html)
    if missing_fns:
        lines.append(
            "WARNING inline handlers call undefined functions: "
            + ", ".join(sorted(missing_fns))
        )

    return "\n".join(lines)


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
    """Metadata plus html, or None if missing (or the id is malformed)."""
    meta = get_demo_meta(email, demo_id)
    if meta is None:
        return None
    path = _html_path(email, demo_id)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        html = f.read()
    return {**meta, "html": html}


def _delete_files(email: str, demo_id: str) -> bool:
    try:
        paths = (_html_path(email, demo_id), _meta_path(email, demo_id))
    except ValueError:
        return False   # malformed id — nothing of ours to delete
    found = False
    for path in paths:
        if os.path.isfile(path):
            os.remove(path)
            found = True
    return found


def delete_demo(email: str, demo_id: str) -> bool:
    if not _delete_files(email, demo_id):
        return False
    rebuild_index(email)
    return True
