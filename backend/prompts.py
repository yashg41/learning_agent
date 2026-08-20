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
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Named prompt_files, not prompts: a `prompts/` directory sitting beside
# prompts.py would shadow this module on import.
PROMPTS_DIR = Path(__file__).parent / "prompt_files"

# Read once at import. These are a handful of small files that never change
# while the process is alive, and the alternative is a disk read on every turn.
_CACHE: dict[str, str] = {}


def _read(name: str) -> str:
    """One prompt file's contents, cached."""
    if name not in _CACHE:
        path = PROMPTS_DIR / name
        _CACHE[name] = path.read_text(encoding="utf-8")
    return _CACHE[name]


def _tutor_sections() -> list[Path]:
    """Numbered tutor sections, in filename order.

    Excludes demo_builder.md (a separate agent) and anything not numbered, so
    a scratch file dropped in this directory cannot silently join the prompt.
    """
    return sorted(
        p for p in PROMPTS_DIR.glob("*.md")
        if p.name[:2].isdigit()
    )


def build_system_prompt(
    knowledge_state: str,
    recent_episodes: str,
    session_demos: str = "",
) -> str:
    """Assemble the tutor's system prompt with the live state filled in.

    session_demos lists only the demos built in the CURRENT session, not all of
    them. A learner can accumulate up to MAX_DEMOS_PER_USER (100); injecting
    every title would cost hundreds of tokens on every turn — including turns
    with no demo intent — and would dangle a list of demo ideas in front of a
    model that is explicitly told not to volunteer demos. Older demos are
    reachable through the `search_demos` tool instead.
    """
    text = "\n".join(_read(p.name).rstrip("\n") for p in _tutor_sections())

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
        ("$domains", taught_domains()),
    ):
        text = text.replace(key, value)
    return text


def demo_builder_prompt() -> str:
    """The demo builder's entire system prompt.

    A different agent from the tutor. It inherits the tutor's conversation by
    forking the session, but NOT the tutor's system prompt — no quiz protocol,
    no knowledge-graph bookkeeping, no feedback rules competing for attention
    with the one job it has.
    """
    return _read("demo_builder.md")


# Back-compat: agent.py imports this name directly.
DEMO_BUILDER_PROMPT = demo_builder_prompt()
