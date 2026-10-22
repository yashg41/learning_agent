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
import shutil
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

# Where the shell drops its parts. A demo written in one piece has no parts
# directory at all, which is what keeps every demo saved before this existed
# loading unchanged — see get_demo.
PARTS_MARKER = "<!--{{PARTS}}-->"
_SHELL_NAME = "00_shell.html"
# NN_<name>.html. The numeric prefix is what orders the document, so parts
# written concurrently and finishing out of order still assemble correctly.
_PART_NAME_RE = re.compile(r"^[0-9]{2,3}_[a-z0-9_]{1,40}$")
# A part is markup for one panel, not a document. Generous next to the ~13KB a
# real panel measures, while still bounding a runaway generation.
MAX_PART_BYTES = 60_000
MAX_PARTS = 40

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


def _parts_dir(email: str, demo_id: str) -> str:
    return os.path.join(demos_dir(email), "parts", _safe_demo_id(demo_id))


def has_parts(email: str, demo_id: str) -> bool:
    """Whether this demo is assembled from parts rather than stored whole."""
    try:
        return os.path.isfile(os.path.join(_parts_dir(email, demo_id), _SHELL_NAME))
    except ValueError:
        return False


def save_demo_shell(email: str, demo_id: str, shell_html: str) -> None:
    """Write the shell: doctype, <style>, <script>, and the parts marker.

    Validated differently from a finished demo. The shell is deliberately NOT a
    complete document yet — it has no panel markup — so validate_demo_html's
    <html>/<body> and handler checks cannot run against it. They run once, on
    the assembled document, in assemble_demo. What must hold here is that the
    marker exists, because without it the parts have nowhere to land and the
    demo would silently render as an empty frame.
    """
    if not isinstance(shell_html, str) or not shell_html.strip():
        raise ValueError("shell html is empty")
    if PARTS_MARKER not in shell_html:
        raise ValueError(
            f"shell must contain {PARTS_MARKER} exactly once — that is where "
            f"the parts are inserted. Without it the demo renders empty."
        )
    if shell_html.count(PARTS_MARKER) > 1:
        raise ValueError(f"shell contains {PARTS_MARKER} more than once")

    directory = _parts_dir(email, demo_id)
    os.makedirs(directory, exist_ok=True)
    _write_atomic(os.path.join(directory, _SHELL_NAME), shell_html)


def save_demo_part(email: str, demo_id: str, index: int, name: str, html: str) -> str:
    """Write one part. Returns the filename it was stored under.

    One file per part, deliberately: parts are written by builders running
    concurrently, and independent files need no lock. Accumulating them into a
    shared list or a single file would be a read-modify-write from several
    coroutines at once — the race that silently dropped a whole batch of
    concept writes elsewhere in this codebase.
    """
    if not isinstance(html, str) or not html.strip():
        raise ValueError("part html is empty")

    size = len(html.encode("utf-8"))
    if size > MAX_PART_BYTES:
        raise ValueError(
            f"Part is {size} bytes, over the {MAX_PART_BYTES} byte limit. "
            f"A part is the markup for ONE panel — split it further."
        )

    try:
        idx = int(index)
    except (TypeError, ValueError):
        raise ValueError(f"index must be a number, got {index!r}")
    if not 1 <= idx <= MAX_PARTS:
        raise ValueError(f"index must be between 1 and {MAX_PARTS}, got {idx}")

    slug = re.sub(r"[^a-z0-9_]+", "_", (name or "part").strip().lower()).strip("_")
    filename = f"{idx:02d}_{slug or 'part'}.html"
    if not _PART_NAME_RE.match(filename[:-5]):
        raise ValueError(f"could not derive a safe filename from name={name!r}")

    directory = _parts_dir(email, demo_id)
    os.makedirs(directory, exist_ok=True)
    _write_atomic(os.path.join(directory, filename), html)
    return filename


def list_demo_parts(email: str, demo_id: str) -> list[str]:
    """Part filenames in document order — the shell is not one of them."""
    try:
        directory = _parts_dir(email, demo_id)
    except ValueError:
        return []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    # Sorted by the numeric prefix in the filename, NOT by write order or mtime:
    # parts are written concurrently and finish in an arbitrary order, and the
    # document's order is the orchestrator's, not the schedule's.
    return sorted(n for n in names if n.endswith(".html") and n != _SHELL_NAME)


def assemble_demo(email: str, demo_id: str) -> str | None:
    """Join shell and parts into one document. None if there is no shell.

    This is what makes a multi-part demo indistinguishable from a single-file
    one everywhere else: the frame is handed a whole document via srcdoc, and
    the sandbox (allow-scripts without allow-same-origin) means parts could
    never have fetched each other in the browser anyway.
    """
    directory = _parts_dir(email, demo_id)
    shell_path = os.path.join(directory, _SHELL_NAME)
    if not os.path.isfile(shell_path):
        return None

    try:
        with open(shell_path, encoding="utf-8") as f:
            shell = f.read()
    except OSError as e:
        logger.warning(f"Unreadable demo shell {shell_path}: {e}")
        return None

    chunks = []
    for filename in list_demo_parts(email, demo_id):
        try:
            with open(os.path.join(directory, filename), encoding="utf-8") as f:
                chunks.append(f.read())
        except OSError as e:
            # Skip rather than fail the whole demo: nine panels that render
            # beat ten that do not.
            logger.warning(f"Unreadable demo part {filename}: {e}")

    return shell.replace(PARTS_MARKER, "\n".join(chunks))


def _write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read_builder_html(path: str) -> str:
    """Load a demo the builder drafted, by path rather than by value.

    A demo is 15-30KB. Passing it as a tool argument sends it through the CLI's
    stdio transport, and a payload that size has been observed to kill the MCP
    channel outright: the call fails with "Stream closed", and so does every
    later call on that session, including unrelated ones. Reproduced in
    isolation — an MCP call, a 20KB write, then an MCP call that dies, where
    the same sequence with a small write is fine.

    The builder already drafts into its scratch directory, so the file exists
    before the tool is called. Sending the path keeps the bytes out of the
    transport entirely: the server reads them straight off disk.

    Confined to the scratch root on purpose. The path arrives from a model
    acting on a learner's request, so without this it would read any file the
    server can reach and publish it into the demo pane.
    """
    return read_scratch_file(path, MAX_DEMO_BYTES, "html_path")


def read_scratch_file(path: str, max_bytes: int, label: str) -> str:
    """Read a file a builder drafted in its scratch directory.

    Shared by every builder that hands the server a path instead of a payload
    (demos here, derivation blocks in derivations.read_builder_blocks), so the
    confinement check exists once. `label` is the argument name the model used,
    so an error points it at the thing it actually passed.
    """
    from backend.sandbox import SCRATCH_ROOT

    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"{label} is empty")

    resolved = os.path.realpath(path.strip())
    root = os.path.realpath(str(SCRATCH_ROOT))
    if not (resolved == root or resolved.startswith(root + os.sep)):
        raise ValueError(
            f"{label} must be inside the scratch directory ({root}). "
            f"Write your draft there first, then pass that path."
        )

    if not os.path.isfile(resolved):
        raise ValueError(
            f"No file at {resolved}. Write the draft before saving it."
        )

    size = os.path.getsize(resolved)
    if size > max_bytes:
        raise ValueError(
            f"Draft is {size} bytes, over the {max_bytes} byte limit."
        )

    try:
        with open(resolved, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"Could not read {resolved}: {e}") from e


def shell_contract_digest(shell_html: str, limit: int = 4000) -> str:
    """What a panel author needs from the shell: the names, not the markup.

    The shell used to be interpolated whole into every part-builder's prompt.
    With ten panels that is the same 8-20KB sent ten times, and each individual
    prompt already exceeds what the transport reliably carries — the failure
    that loses a build.

    A panel author needs to know which classes to use and which helpers already
    exist, so that is what this extracts. Same idea as demo_digest, which keeps
    a demo out of the tutor's prompt for the same reason.
    """
    classes = sorted(set(re.findall(r'class\s*=\s*["\']([^"\']+)["\']', shell_html)))
    names: set[str] = set()
    for group in classes:
        names.update(group.split())

    css_classes = sorted(set(re.findall(r"\.([a-zA-Z][\w-]*)\s*[{,]", shell_html)))
    functions = sorted(set(_FN_DECL.findall(shell_html)))
    ids = sorted(set(_ID_ATTR.findall(shell_html)))

    parts = []
    if css_classes:
        parts.append("CSS classes defined in the shell: " + ", ".join(css_classes))
    if names:
        parts.append("Classes used in the shell's own markup: " + ", ".join(sorted(names)))
    if functions:
        parts.append("Shared functions already defined: " + ", ".join(functions))
    if ids:
        parts.append("Ids already taken: " + ", ".join(ids))

    out = "\n".join(parts)
    return out[:limit] + ("\n… [truncated]" if len(out) > limit else "")


def copy_demo_to_scratch(email: str, demo_id: str, dest_dir: str) -> str:
    """Put a stored demo where the builder can edit it. Returns the path.

    The other half of read_builder_html: instead of returning 20KB of HTML
    through a tool result, the server copies the file and returns only where it
    landed. The builder then reads and patches it with its own file tools,
    which never touch the MCP channel.

    Copies rather than handing back the store path, so a half-finished edit
    cannot corrupt the live demo — the learner keeps seeing the current version
    until update_demo publishes.

    Writes get_demo's html — what the learner actually sees — not the flat
    <demo_id>.html. For a multi-panel demo those differ: the page is assembled
    from parts/, and the flat file is either absent or a stale earlier edit.
    """
    demo = get_demo(email, demo_id)
    if demo is None:
        raise ValueError(f"No demo with id {demo_id!r}")

    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{_safe_demo_id(demo_id)}.html")
    _write_atomic(dest, demo["html"])
    return dest


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
    since `key` is only known at runtime. Nothing covers that shape: the skill
    tells the builder to derive ids from one source, and the frame's error relay
    reports the throw. This covers the simpler literal mismatch only.

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
        if not meta or not meta.get("id"):
            continue
        # A part-built demo writes its meta at open time, before any panel
        # exists, so that the parts have an id to attach to. Until finalize
        # runs there is nothing renderable, and listing it would put a demo in
        # the learner's pane whose frame is empty. finalize_demo sets
        # part_count, so its absence is what marks one as still being built.
        if meta.get("parts") and not meta.get("part_count"):
            continue
        entries.append(_index_entry(meta))
    entries.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    _write_atomic(_index_path(email), json.dumps({"demos": entries}, indent=2))
    return entries


def list_demos(email: str) -> list[dict]:
    """This learner's demos, or [] if they have none.

    Deliberately creates nothing. Listing is a read, and the email reaching it
    is whatever is currently in the UI's box — which, while someone types an
    address, is every prefix of it. Creating a directory here made a user out
    of each keystroke: "yashg41@", "yashg41@s", … all appeared on disk beside
    real learners, each holding a single empty _index.json.
    """
    if not os.path.isdir(demos_dir(email)):
        return []
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


def _resolve_html(html: str = "", html_path: str = "") -> str:
    """The document to store, from either a path or an inline string.

    Path is the preferred form and the reason this exists: a demo is 15-30KB,
    and sending that as a tool argument puts it through the CLI transport. The
    inline form stays because a single part (demo_write_part) is small enough
    not to care, and the tests use it.
    """
    if html_path:
        return read_builder_html(html_path)
    if html:
        return html
    raise ValueError("pass html_path (preferred) or html")


def save_demo(
    email: str,
    title: str,
    html: str = "",
    concept_id: str = "",
    summary: str = "",
    builder_session_id: str = "",
    chat_session_id: str = "",
    html_path: str = "",
    job_id: str = "",
) -> dict:
    """Validate and store a demo. Returns its metadata (never the html).

    Takes html_path (a file the builder drafted) or html inline; see
    _resolve_html for why the path form is preferred.

    builder_session_id links the demo to the SDK session that produced it, so
    a later "add a reset button" resumes that session — which still holds both
    the conversation it was forked from and the HTML it wrote — instead of
    regenerating the whole document from scratch. See update_demo.

    chat_session_id records which learning session asked for it, so the tutor's
    prompt can list this session's demos (see format_session_demos).

    job_id records which build wrote it. The job runner finds its result by
    this stamp on disk (find_demo_by_job) — a tool cannot hand a value back to
    the runner, because over the HTTP transport its build_ctx is a copy.
    """
    html = validate_demo_html(_resolve_html(html, html_path))
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
        "job_id": (job_id or "").strip(),
        # New content, so nothing has checked it yet. demo_verify sets this.
        "verified": None,
    }

    _write_atomic(_html_path(email, demo_id), html)
    _write_atomic(_meta_path(email, demo_id), json.dumps(meta, indent=2))
    _touch_index(email, meta)
    logger.info(f"Demo saved: {demo_id} ({meta['bytes']} bytes) for {email}")
    return meta


def open_demo(
    email: str,
    title: str,
    shell_html: str,
    concept_id: str = "",
    summary: str = "",
    chat_session_id: str = "",
    job_id: str = "",
    declared_parts: list | None = None,
) -> dict:
    """Mint a demo id and write its shell. Returns metadata; nothing renders yet.

    The split path exists because one response cannot emit a large document:
    the cap is on what the model writes in a single reply, and an elaborate
    multi-panel demo exceeds it, discarding the whole response rather than
    truncating it. Writing the shell once and each panel separately keeps every
    individual write far under that ceiling, whatever the panel count.

    Deliberately does NOT touch the index: an opened-but-unfinished demo must
    not appear in the learner's list. finalize_demo does that.

    declared_parts is stored on the meta because the job runner, not this
    agent, builds the panels — and it reads them from here after the agent
    exits. See save_demo on job_id for why disk is the channel.
    """
    _ensure_dirs(email)
    demo_id = f"d_{uuid.uuid4().hex[:10]}"

    # Before the id is committed anywhere, so a bad shell fails here rather
    # than leaving an empty directory behind.
    save_demo_shell(email, demo_id, shell_html)

    meta = {
        "id": demo_id,
        "title": (title or "Untitled demo").strip()[:120],
        "concept_id": (concept_id or "").strip(),
        "summary": (summary or "").strip()[:300],
        "created_at": _now(),
        "updated_at": _now(),
        "version": 1,
        "builder_session_id": "",
        "chat_session_id": (chat_session_id or "").strip(),
        "bytes": 0,
        # What finalize_demo checks against what actually landed.
        "parts": True,
        "job_id": (job_id or "").strip(),
        "declared_parts": list(declared_parts or []),
        "verified": None,
    }
    _write_atomic(_meta_path(email, demo_id), json.dumps(meta, indent=2))
    logger.info(f"Demo opened: {demo_id} for {email}")
    return meta


def finalize_demo(
    email: str, demo_id: str, builder_session_id: str = "", job_id: str = ""
) -> dict | None:
    """Assemble the parts, validate the whole document, publish it.

    Validation runs HERE and only here. <html>/<body> presence and the
    inline-handler check are properties of a finished document — a single part
    is a fragment and would fail every one of them — so they cannot run per
    part.
    """
    meta = get_demo_meta(email, demo_id)
    if meta is None:
        return None

    html = assemble_demo(email, demo_id)
    if html is None:
        raise ValueError(f"demo {demo_id} has no shell — call demo_open first")

    parts = list_demo_parts(email, demo_id)
    if not parts:
        # A shell with no panels is an empty frame. Refuse rather than publish
        # it: the job runner treats this as a failed build.
        raise ValueError(
            f"demo {demo_id} has a shell but no parts — write at least one "
            f"part before finalizing"
        )

    html = validate_demo_html(html)

    meta = {
        **meta,
        "updated_at": _now(),
        "bytes": len(html.encode("utf-8")),
        "part_count": len(parts),
        # The assembled document is new; any earlier check was of a shell.
        "verified": None,
    }
    if builder_session_id:
        meta["builder_session_id"] = builder_session_id.strip()
    if job_id:
        meta["job_id"] = job_id.strip()

    _write_atomic(_meta_path(email, demo_id), json.dumps(meta, indent=2))
    # Only now does it become visible to the learner.
    _touch_index(email, meta)
    logger.info(
        f"Demo finalized: {demo_id} ({len(parts)} parts, {meta['bytes']} bytes) "
        f"for {email}"
    )
    return meta


def update_demo(
    email: str,
    demo_id: str,
    html: str = "",
    title: str = "",
    summary: str = "",
    builder_session_id: str = "",
    html_path: str = "",
    job_id: str = "",
) -> dict | None:
    """Replace a demo's html in place, keeping its id. None if it's gone.

    Takes html_path (the draft the builder just patched) or html inline. The
    path form is what keeps an edit off the transport: the server reads the
    file itself instead of the document arriving as a tool argument.

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

    html = validate_demo_html(_resolve_html(html, html_path))

    meta = {
        **existing,
        "title": (title or existing.get("title") or "Untitled demo").strip()[:120],
        "summary": (summary or existing.get("summary", "")).strip()[:300],
        "updated_at": _now(),
        "version": int(existing.get("version") or 1) + 1,
        "bytes": len(html.encode("utf-8")),
        "verified": None,
    }
    if builder_session_id:
        meta["builder_session_id"] = builder_session_id.strip()
    if job_id:
        # The runner compares this to its own id to tell "this build edited
        # the demo" from "the builder looked and changed nothing".
        meta["job_id"] = job_id.strip()

    # A multi-panel demo becomes a single-file one here. get_demo serves a
    # demo that has parts/ by assembling the parts, so the edited page written
    # below would otherwise be saved and never shown — observed: an edit
    # reported ready at v2, the server said v2, and served v1's panels, with
    # demo_verify (which reads through get_demo) checking v1 and passing it.
    # The edit was made to the whole assembled page (copy_demo_to_scratch), so
    # it supersedes every part; they are kept aside, not deleted.
    retiring = has_parts(email, demo_id)
    if retiring:
        for key in ("parts", "part_count", "declared_parts"):
            meta.pop(key, None)

    _write_atomic(_html_path(email, demo_id), html)
    if retiring:
        _retire_parts(email, demo_id)
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


def find_demo_by_job(email: str, job_id: str) -> dict | None:
    """The demo a build wrote, found by the job id stamped on its meta.

    Scans the meta files rather than the index: a demo that was opened for the
    split path is deliberately absent from the index until finalize, and the
    runner still has to find it to build its panels. Newest wins if a builder
    somehow wrote twice.
    """
    if not job_id:
        return None
    try:
        names = os.listdir(demos_dir(email))
    except OSError:
        return None

    found = []
    for name in names:
        if not name.endswith(".json") or name.startswith("_"):
            continue
        meta = _read_json(os.path.join(demos_dir(email), name))
        if meta and meta.get("job_id") == job_id and meta.get("id"):
            found.append(meta)
    if not found:
        return None
    return max(found, key=lambda m: m.get("updated_at") or "")


def set_demo_verified(email: str, demo_id: str, verified: bool) -> None:
    """Record demo_verify's verdict on the meta, where the job runner reads it."""
    meta = get_demo_meta(email, demo_id)
    if meta is None:
        return
    meta["verified"] = bool(verified)
    _write_atomic(_meta_path(email, demo_id), json.dumps(meta, indent=2))


def read_demo_shell(email: str, demo_id: str) -> str:
    """The stored shell of a split demo, or "" if there is none."""
    try:
        path = os.path.join(_parts_dir(email, demo_id), _SHELL_NAME)
    except ValueError:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


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
    # Through get_demo, so a multi-panel demo is described as assembled —
    # the flat file is absent or stale for those.
    demo = get_demo(email, demo_id)
    if demo is None:
        return None
    meta, html = demo, demo["html"]

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
    # The single chokepoint. Every caller of a demo's html — both API routes,
    # the iframe, and the refinement path — comes through here, so assembling
    # at this point is what lets a multi-part demo need no change anywhere
    # else, the frontend included.
    #
    # Falls back to the flat <demo_id>.html when there is no shell, which is
    # what keeps every demo written before parts existed loading unchanged.
    assembled = assemble_demo(email, demo_id)
    if assembled is not None:
        return {**meta, "html": assembled}

    path = _html_path(email, demo_id)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        html = f.read()
    return {**meta, "html": html}


def _retired_parts_dir(email: str, demo_id: str) -> str:
    return _parts_dir(email, demo_id) + ".retired"


def _retire_parts(email: str, demo_id: str) -> None:
    """Move parts/ aside so get_demo serves the flat file from now on.

    Renamed rather than deleted, so an edit that went wrong can be recovered by
    hand. Only the latest retired set is kept.
    """
    live = _parts_dir(email, demo_id)
    retired = _retired_parts_dir(email, demo_id)
    shutil.rmtree(retired, ignore_errors=True)
    os.replace(live, retired)
    logger.info(f"Demo {demo_id}: edited as a whole page; panels retired to {retired}")


def _delete_files(email: str, demo_id: str) -> bool:
    try:
        paths = (_html_path(email, demo_id), _meta_path(email, demo_id))
        parts = _parts_dir(email, demo_id)
    except ValueError:
        return False   # malformed id — nothing of ours to delete
    found = False
    for path in paths:
        if os.path.isfile(path):
            os.remove(path)
            found = True
    # Also the parts directory, or MAX_DEMOS_PER_USER eviction would reclaim
    # the index row and the .html while leaving the parts on disk forever.
    if os.path.isdir(parts):
        shutil.rmtree(parts, ignore_errors=True)
        found = True
    shutil.rmtree(_retired_parts_dir(email, demo_id), ignore_errors=True)
    return found


def delete_demo(email: str, demo_id: str) -> bool:
    if not _delete_files(email, demo_id):
        return False
    rebuild_index(email)
    return True
