"""Tests for prompt assembly from backend/prompt_files/.

The prompts are markdown on disk rather than string literals, so these guard
the seam between the two: that every section is picked up, that the three
placeholders are filled, and — the one that actually bit — that nothing else in
the text is mangled on the way through.
"""

import pytest

from backend.prompts import (
    PROMPTS_DIR,
    build_system_prompt,
    demo_builder_prompt,
)


def test_all_sections_are_assembled():
    prompt = build_system_prompt("KSTATE", "REPISODES", "SDEMOS")
    for heading in (
        "## Your Teaching Style",
        "## Running Code",
        "## Output Formatting",
        "## Your Memory System",
        "## Concept Tracking Rules",
        "## Quiz Behavior",
        "## Interactive Demos",
        "## Aspect-Specific Feedback",
        "## Learner's Knowledge State",
    ):
        assert heading in prompt, f"missing section: {heading}"


def test_sections_are_assembled_in_filename_order():
    """Identity first, learner state last — the numeric prefixes decide."""
    prompt = build_system_prompt("K", "R", "D")
    assert prompt.startswith("You are PyMentor")
    assert prompt.index("## Your Teaching Style") < prompt.index("## Quiz Behavior")
    assert prompt.index("## Quiz Behavior") < prompt.index("## Learner's Knowledge State")


def test_placeholders_are_filled():
    prompt = build_system_prompt("KSTATE", "REPISODES", "SDEMOS")
    assert "KSTATE" in prompt
    assert "REPISODES" in prompt
    assert "SDEMOS" in prompt
    assert "$knowledge_state" not in prompt
    assert "$recent_episodes" not in prompt
    assert "$session_demos" not in prompt


def test_empty_session_demos_gets_a_placeholder_line():
    prompt = build_system_prompt("K", "R", "")
    assert "(none yet in this session)" in prompt


def test_double_dollar_math_delimiters_survive():
    """The regression this file exists for.

    string.Template treats `$$` as an escaped literal `$`, so assembling with
    it rewrote `$$...$$` into `$...$` — the single-dollar form the formatting
    rules explicitly say does NOT render. Every equation would have broken,
    silently, with the prompt still looking correct in the file.
    """
    prompt = build_system_prompt("K", "R", "D")
    assert "`$$...$$`" in prompt
    assert r"$$\theta$$" in prompt
    # The rule warns about single-dollar; it must still read as a warning.
    assert "single `$...$` does not" in prompt


def test_latex_backslashes_are_not_doubled():
    """In a .py literal these needed \\\\; from a file the raw char is correct."""
    prompt = build_system_prompt("K", "R", "D")
    assert r"\(...\)" in prompt
    assert r"\\(" not in prompt


def test_braces_in_a_prompt_would_not_break_assembly(tmp_path, monkeypatch):
    """Assembly must not interpret braces.

    Today's prompts happen to contain none, so this writes one that does: a
    future edit adding a JSON example or an f-string sample must not turn into
    a KeyError at startup. (.format() would raise on exactly this input.)
    """
    import backend.prompts as prompts

    (tmp_path / "_common").mkdir()
    app_dir = tmp_path / "pymentor"
    app_dir.mkdir()
    (app_dir / "00_x.md").write_text(
        'Example: {"concept_id": "trees"} and f"{value:.2f}"\n$knowledge_state',
        encoding="utf-8",
    )
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(prompts, "_CACHE", {})

    out = prompts.build_system_prompt("KSTATE", "R", "D")
    assert '{"concept_id": "trees"}' in out
    assert 'f"{value:.2f}"' in out
    assert "KSTATE" in out


def test_demo_builder_is_separate_from_the_tutor_prompt():
    builder = demo_builder_prompt()
    tutor = build_system_prompt("K", "R", "D")
    # Matched on the job rather than an exact sentence: the wording changed
    # when the builder became a multi-turn session, and pinning the phrasing
    # made an intended edit look like a regression.
    assert "interactive HTML demo" in builder
    # The builder must not inherit the tutor's protocols.
    assert "Quiz Behavior" not in builder
    assert "Concept Tracking" not in builder
    assert builder not in tutor


def test_unnumbered_files_are_not_pulled_into_the_tutor_prompt():
    """demo_builder.md lives in the same directory but is a different agent."""
    prompt = build_system_prompt("K", "R", "D")
    # Compared against the file's actual first line, so this cannot quietly
    # start passing because the sentence it looked for was reworded.
    assert demo_builder_prompt().splitlines()[0] not in prompt


def test_code_execution_prompt_points_at_the_skill():
    """The prompt keeps the decision rules; the skill holds the mechanics.

    If the skill is renamed or removed, the prompt's pointer becomes a dangling
    reference to a file the model will never find — and the mechanics vanish
    with no error anywhere.
    """
    from pathlib import Path

    prompt = build_system_prompt("K", "R", "D")
    assert "code-execution" in prompt

    skill = (Path(__file__).resolve().parents[2]
             / ".claude" / "skills" / "code-execution" / "SKILL.md")
    assert skill.is_file(), "prompt references a skill that does not exist"


def test_mechanics_are_not_duplicated_in_the_always_on_prompt():
    """Mechanics belong in the skill only.

    They were previously stated in BOTH the prompt and the run_code tool
    description, and the two drifted apart across successive edits.
    """
    prompt = build_system_prompt("K", "R", "D")
    # The fence syntax is the clearest marker of mechanics.
    assert "```verified:" not in prompt
    assert "%pip install" not in prompt


def test_every_section_file_is_non_empty():
    for path in sorted(PROMPTS_DIR.glob("*/*.md")):
        assert path.read_text(encoding="utf-8").strip(), f"empty prompt: {path.name}"


# --- The _common / <app> split -------------------------------------------
#
# One backend serves more than one tutor. These guard the property that makes
# that worth doing: a fix to a shared rule reaches every app with no porting.


def test_common_sections_reach_the_assembled_prompt():
    """A rule in _common/ is in the prompt without the app copying it."""
    prompt = build_system_prompt("K", "R", "D")
    assert "## Your Memory System" in prompt      # _common/40_memory.md
    assert "## Quiz Behavior" in prompt           # _common/50_quiz.md
    assert "## Looking Things Up" in prompt       # _common/25_web_lookup.md


def test_common_and_app_sections_interleave_by_number():
    """Merged into ONE numeric sequence, not one directory after the other.

    app 20_code_execution must precede common 25_web_lookup, which must precede
    app 30_output_formatting. Concatenating directories would break this.
    """
    prompt = build_system_prompt("K", "R", "D")
    assert (
        prompt.index("## Running Code")            # pymentor/20_
        < prompt.index("## Looking Things Up")     # _common/25_
        < prompt.index("## Output Formatting")     # pymentor/30_
    )


@pytest.mark.sanskrit
def test_an_app_file_overrides_a_common_file_of_the_same_name(tmp_path, monkeypatch):
    """The override hatch: copy the file into <app>/ and edit it there."""
    import backend.prompts as prompts

    common = tmp_path / "_common"
    common.mkdir()
    (common / "10_rule.md").write_text("SHARED VERSION", encoding="utf-8")
    (common / "40_keep.md").write_text("STILL SHARED", encoding="utf-8")

    app = tmp_path / "sanskrit"
    app.mkdir()
    (app / "10_rule.md").write_text("APP VERSION", encoding="utf-8")

    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(prompts, "_CACHE", {})

    out = prompts.build_system_prompt("K", "R", "D", app="sanskrit")
    assert "APP VERSION" in out
    assert "SHARED VERSION" not in out
    # Overriding one file must not detach the app from the rest of _common.
    assert "STILL SHARED" in out


def test_subject_specific_sections_are_not_in_common():
    """_common must stay genuinely subject-neutral.

    These three name Qiskit, LaTeX/mermaid, and the Python track ids. Sharing
    them would push Python assumptions into every future tutor — the exact
    failure the split exists to prevent.
    """
    common = PROMPTS_DIR / "_common"
    for leaked in ("10_teaching_style.md", "30_output_formatting.md",
                   "80_learner_state.md", "00_identity.md"):
        assert not (common / leaked).is_file(), (
            f"{leaked} is subject-specific and must live under an app directory"
        )


def test_unknown_app_fails_loudly():
    with pytest.raises(FileNotFoundError, match="No prompt directory"):
        build_system_prompt("K", "R", "D", app="nonexistent")


def test_builder_prompts_default_to_the_shared_copy():
    """'Build one demo, emit no prose' is subject-neutral, so it is shared."""
    assert (PROMPTS_DIR / "_common" / "demo_builder.md").is_file()
    assert (PROMPTS_DIR / "_common" / "derivation_builder.md").is_file()
    # Every app with demos resolves to the same file, which is the property
    # this test is about — not any particular sentence in it.
    assert demo_builder_prompt("pymentor") == demo_builder_prompt("sanskrit")
    assert "interactive HTML demo" in demo_builder_prompt()


@pytest.mark.sanskrit
def test_reading_section_sits_between_correctness_and_web_lookup():
    """The order carries meaning: the correctness rules are established before
    reading (a printed form is not a verified one), and web lookup follows
    immediately, which is where open_book belongs."""
    from backend.prompts import _tutor_sections

    names = [p.name for p in _tutor_sections("sanskrit")]
    assert names.index("15_correctness.md") < names.index("20_reading.md")
    assert names.index("20_reading.md") < names.index("25_web_lookup.md")
