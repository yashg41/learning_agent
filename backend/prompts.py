"""Prompt loading for the Python Learning Agent.

The prompts themselves live as markdown under backend/prompts/, one file per
topic, not as string literals in this module. They are English documents, not
code: keeping them as .md means they render with headings and wrapping, diff a
line at a time instead of as one enormous changed string, and can be read end
to end before being edited. The tutor's code-execution rules had grown to 12
bullets buried inside a list about teaching style, and that was invisible while
they were a Python literal.

Escaping is the other reason. In a literal, LaTeX backslashes had to be doubled
(`\\\\theta`) to survive Python's escape processing, and `{` had to be doubled to
survive .format(). In a file, the raw characters are what reach the model.

Assembly order is the numeric filename prefix (00_, 10_, 20_ ...), so inserting
a section is a new file rather than an edit in the middle of an existing one.
`demo_builder.md` is deliberately outside that sequence: it is a different
agent's entire prompt, not a section of the tutor's.

Layout: `_common/` holds the sections that are genuinely subject-neutral, and
`<app>/` holds the rest. One backend serves more than one tutor, and the split
is the point — a fix to the memory or quiz protocol lands in `_common/` once and
both tutors get it, with no porting step. What is emphatically NOT shared is
anything that names a subject: teaching style talks about Qiskit snippets and
15-line code examples, output formatting is built around LaTeX and mermaid, and
the learner-state rules hardcode Python track ids. Those live under the app.

Sections from both directories merge into one numbered sequence, so `_common`
and app files interleave by prefix rather than one block following the other.
An app file wins over a `_common` file with the same name, which is the
override hatch: copy the file into `<app>/` and edit it there.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Named prompt_files, not prompts: a `prompts/` directory sitting beside
# prompts.py would shadow this module on import.
PROMPTS_DIR = Path(__file__).parent / "prompt_files"

# Sections every tutor gets, whatever it teaches.
COMMON_DIR_NAME = "_common"

# Which app's prompt to assemble when the caller does not say. Keeps every
# existing call site working unchanged.
DEFAULT_APP = "pymentor"

# Read once per (directory, filename). These are small files that never change
# while the process is alive, and the alternative is a disk read on every turn.
# Keyed by full path, not bare name, so two apps' 00_identity.md cannot collide
# — which they would have under the old name-only key.
_CACHE: dict[Path, str] = {}


def _read(path: Path) -> str:
    """One prompt file's contents, cached."""
    if path not in _CACHE:
        _CACHE[path] = path.read_text(encoding="utf-8")
    return _CACHE[path]


def _tutor_sections(app: str = DEFAULT_APP) -> list[Path]:
    """Numbered tutor sections for one app, in assembly order.

    Merges `_common/` with `<app>/` and sorts by filename, so a common 40_memory
    and an app-specific 15_mathematics land in numeric order rather than in
    directory order. A file in the app directory shadows a `_common` file of the
    same name.

    Excludes the builder prompts (separate agents) and anything not numbered, so
    a scratch file dropped in either directory cannot silently join the prompt.
    """
    app_dir = PROMPTS_DIR / app
    if not app_dir.is_dir():
        raise FileNotFoundError(
            f"No prompt directory for app '{app}': {app_dir}. "
            f"Available: {sorted(p.name for p in PROMPTS_DIR.iterdir() if p.is_dir())}"
        )

    by_name: dict[str, Path] = {}
    for directory in (PROMPTS_DIR / COMMON_DIR_NAME, app_dir):
        for path in directory.glob("*.md"):
            if path.name[:2].isdigit():
                by_name[path.name] = path  # app dir is second, so it wins
    return [by_name[name] for name in sorted(by_name)]


def build_system_prompt(
    knowledge_state: str,
    recent_episodes: str,
    session_demos: str = "",
    app: str = DEFAULT_APP,
) -> str:
    """Assemble the tutor's system prompt with the live state filled in.

    session_demos lists only the demos built in the CURRENT session, not all of
    them. A learner can accumulate up to MAX_DEMOS_PER_USER (100); injecting
    every title would cost hundreds of tokens on every turn — including turns
    with no demo intent — and would dangle a list of demo ideas in front of a
    model that is explicitly told not to volunteer demos. Older demos are
    reachable through the `search_demos` tool instead.
    """
    text = "\n".join(_read(p).rstrip("\n") for p in _tutor_sections(app))

    # Plain replace, deliberately, for both of the obvious alternatives:
    #
    #   .format() chokes on the literal braces the prompts are full of (JSON
    #   examples, f-string samples in the code rules) and raises KeyError.
    #
    #   string.Template eats `$$`, which is its escape for a literal dollar.
    #   The formatting rules tell the model that `$$...$$` is the ONLY math
    #   delimiter that renders and that single `$...$` does not — Template
    #   silently rewrote that line into the broken form it warns against, and
    #   every equation would have stopped rendering.
    #
    # Only these three names are substituted; every other character in the
    # markdown reaches the model untouched, which is the point of the files.
    # Imported here, not at module scope: knowledge imports nothing from this
    # module today, but prompts is imported by tools and agent both, and a
    # top-level cycle would only show up as an ImportError at startup.
    from backend.knowledge import taught_domains

    for key, value in (
        ("$knowledge_state", knowledge_state),
        ("$recent_episodes", recent_episodes),
        ("$session_demos", session_demos or "(none yet in this session)"),
        ("$domains", taught_domains(app)),
    ):
        text = text.replace(key, value)
    return text


def _builder_prompt(filename: str, app: str) -> Path:
    """Locate a builder prompt, letting an app override the shared one."""
    override = PROMPTS_DIR / app / filename
    return override if override.is_file() else PROMPTS_DIR / COMMON_DIR_NAME / filename


def demo_builder_prompt(app: str = DEFAULT_APP) -> str:
    """The demo builder's entire system prompt.

    A different agent from the tutor. It inherits the tutor's conversation by
    forking the session, but NOT the tutor's system prompt — no quiz protocol,
    no knowledge-graph bookkeeping, no feedback rules competing for attention
    with the one job it has.

    Shared by default: "build one demo, emit no prose" is the same instruction
    whatever the subject. An app that needs different rules drops its own
    demo_builder.md into its directory.
    """
    return _read(_builder_prompt("demo_builder.md", app))


def demo_part_builder_prompt(app: str = DEFAULT_APP) -> str:
    """System prompt for an agent writing ONE panel of a split demo.

    Separate from demo_builder.md because the job is genuinely different: this
    agent does not design the demo, choose its structure, or decide when it is
    finished. It receives a contract — class names, data, what this panel must
    show — and fills in one slot. Handing it the orchestrator's prompt would
    invite it to open demos of its own and reason about the whole document.
    """
    return _read(_builder_prompt("demo_part_builder.md", app))


def derivation_builder_prompt(app: str = DEFAULT_APP) -> str:
    """The derivation builder's entire system prompt.

    Like the demo builder: forks the tutor's conversation for context but not
    its system prompt, so the one job it has is not competing with the quiz
    protocol and the memory rules.
    """
    return _read(_builder_prompt("derivation_builder.md", app))


# Deliberately NOT evaluated at import time any more. As module constants these
# froze one app's builder prompt into the process at first import, which made
# "which app is this?" unanswerable at the point of use. Call the functions
# instead — they are cached, so this costs a dict lookup, not a disk read.
