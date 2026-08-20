"""Unit tests for the learning tools' pure helpers.

Same split as test_sandbox.py: the pure functions are tested here, while the
subprocess and MCP plumbing is exercised by driving the registered handler by
hand.

_clip_output is what stands between a runaway cell and the transcript. Anything
a tool returns is replayed on every later turn of the session, so a cell that
prints a megabyte would otherwise become a permanent per-turn cost.
"""

import pytest

from backend.codejobs import load_verified, save_verified, verified_path
from backend.tools import (
    MAX_PROSE_RATIO,
    MAX_RUN_OUTPUT_CHARS,
    _clip_output,
    computes_nothing,
    prose_ratio,
)


# --- _clip_output ---


def test_clip_passes_short_text_through():
    assert _clip_output("4\n") == "4\n"


def test_clip_passes_empty_through():
    assert _clip_output("") == ""


def test_clip_handles_none_like_empty():
    """run_code hands us whatever the sandbox produced; guard the falsy case."""
    assert _clip_output(None) is None


def test_clip_leaves_text_at_the_limit_untouched():
    text = "x" * MAX_RUN_OUTPUT_CHARS
    assert _clip_output(text) == text


def test_clip_trims_over_limit():
    text = "x" * (MAX_RUN_OUTPUT_CHARS * 3)
    out = _clip_output(text)
    assert len(out) < len(text)
    assert "chars trimmed" in out


def test_clip_keeps_both_ends():
    """A traceback's useful line is last, so head-only truncation loses it."""
    head = "H" * 100
    tail = "ValueError: boom"
    text = head + ("m" * MAX_RUN_OUTPUT_CHARS * 2) + tail
    out = _clip_output(text)
    assert out.startswith("HHH")
    assert out.endswith(tail)


def test_clip_reports_the_dropped_count_accurately():
    total = MAX_RUN_OUTPUT_CHARS * 2
    out = _clip_output("x" * total)
    half = MAX_RUN_OUTPUT_CHARS // 2
    expected = total - (half * 2)
    assert f"[{expected} chars trimmed]" in out


@pytest.mark.parametrize("size", [
    MAX_RUN_OUTPUT_CHARS - 1,
    MAX_RUN_OUTPUT_CHARS,
    MAX_RUN_OUTPUT_CHARS + 1,
])
def test_clip_boundary(size):
    """Off-by-one around the threshold: only over-limit input is trimmed."""
    out = _clip_output("x" * size)
    if size <= MAX_RUN_OUTPUT_CHARS:
        assert out == "x" * size
    else:
        assert "chars trimmed" in out


# --- computes_nothing ---
#
# From a real session: asked to check a learner's understanding of entropy,
# the tutor sent an essay wrapped in print() calls, ran it, and read its own
# words back. Four such calls cost ~25KB of permanent transcript. The samples
# below are shortened versions of the actual code that was sent.


def test_rejects_bare_print_essay():
    code = '''
print("="*80)
print("CHECKING YOUR UNDERSTANDING")
print("="*80)
print("""
1. "We have a training set"
   Correct - decision trees learn from training data
""")
'''
    assert computes_nothing(code) is True


def test_rejects_prose_parked_in_variables_first():
    """The real call assigned its text to names before printing it.

    An "only print statements" check misses this, which is why the rule is
    "nothing is computed" rather than "nothing but print".
    """
    code = '''
corrected = """
CLASSIFIER:
1. Start with training set
"""
table = "| aspect | value |"
print(corrected)
print(table)
'''
    assert computes_nothing(code) is True


def test_allows_string_multiplication_in_banners():
    """`"="*80` is still not computation the model needs to observe."""
    assert computes_nothing('print("=" * 80)\nprint("TITLE")') is True


def test_allows_real_computation():
    code = '''
from sklearn.tree import DecisionTreeRegressor
import numpy as np
X = np.array([[1], [2], [3]])
y = np.array([10, 20, 30])
tree = DecisionTreeRegressor(max_depth=1).fit(X, y)
print(tree.predict([[1.5]]))
'''
    assert computes_nothing(code) is False


def test_arithmetic_counts_as_computation():
    """Trivial, but the model genuinely cannot be certain without running it."""
    assert computes_nothing("x = 6 * 7\nprint(x)") is False


def test_loop_counts_as_computation():
    assert computes_nothing('for i in range(3):\n    print(i)') is False


def test_method_call_counts_as_computation():
    assert computes_nothing('print("a,b".split(","))') is False


def test_fstring_with_expression_counts_as_computation():
    assert computes_nothing('print(f"{2 + 2}")') is False


def test_syntax_error_is_not_refused():
    """A broken cell should reach the sandbox — the traceback is a real result."""
    assert computes_nothing("print('unterminated") is False


def test_empty_is_not_refused():
    """Empty input is handled by the earlier `code is required` check."""
    assert computes_nothing("") is False


# --- prose_ratio ---
#
# computes_nothing() refuses a cell with NO computation. The model adapted by
# appending a real computation to the bottom of the same essay — a 250-line
# lecture ending in a RandomForest fit, which passed. This is the follow-up
# guard, and the threshold comes from a real session: legitimate calls measured
# 11-34% prose, abusive ones 71-99%.


def test_real_code_is_mostly_not_prose():
    code = '''
from sklearn.tree import DecisionTreeRegressor
import numpy as np
X = np.array([[1], [2], [3], [4]])
y = np.array([10, 20, 30, 40])
tree = DecisionTreeRegressor(max_depth=1).fit(X, y)
print(tree.predict([[1.5]]))
print(tree.score(X, y))
'''
    assert prose_ratio(code) < MAX_PROSE_RATIO


def test_lecture_with_a_computation_appended_is_caught():
    """The exact shape that defeated computes_nothing()."""
    essay = '"""\n' + ("Feature importance tells you which features matter. " * 40) + '\n"""'
    code = f'''
import numpy as np
print({essay})
x = np.array([1, 2, 3])
print(x.mean())
'''
    assert not computes_nothing(code), "has real computation, so the old guard passes it"
    assert prose_ratio(code) > MAX_PROSE_RATIO, "but the ratio guard should catch it"


def test_prose_hidden_in_comments_is_counted():
    """ast drops comments, so text moved there would otherwise slip through."""
    comments = "\n".join(
        f"# Random forests average many trees to reduce variance, point {i}"
        for i in range(40)
    )
    code = f"{comments}\nx = 1 + 1\nprint(x)"
    assert prose_ratio(code) > MAX_PROSE_RATIO


def test_docstrings_count_as_prose():
    code = '"""' + ("An explanation of entropy and information gain. " * 30) + '"""\nprint(2 + 2)'
    assert prose_ratio(code) > MAX_PROSE_RATIO


def test_short_labels_do_not_trip_the_guard():
    """Ordinary print labels are prose but nowhere near the threshold."""
    code = '''
import numpy as np
data = np.array([1, 2, 3, 4, 5])
print("mean:", data.mean())
print("std:", data.std())
print("variance:", data.var())
'''
    assert prose_ratio(code) < MAX_PROSE_RATIO


def test_empty_and_broken_code_do_not_trip_the_guard():
    assert prose_ratio("") == 0.0
    assert prose_ratio("print('unterminated") == 0.0


# --- transcript budget ---
#
# A real regression: verified_id was the LAST key in the run_code payload, and
# agent.py truncates a tool result to TOOL_RESULT_MAX_CHARS before writing it
# to the transcript. Two runs with sizable stdout hit exactly 2000 chars, the
# id was cut off, and the model — seeing no id to cite — retyped the code by
# hand. That is precisely the drift the placeholder was built to prevent.


def test_transcript_budget_matches_the_agent_cap():
    """These live in two modules to avoid an import cycle; keep them equal."""
    from backend.agent import TOOL_RESULT_MAX_CHARS
    from backend.tools import TRANSCRIPT_RESULT_BUDGET

    assert TRANSCRIPT_RESULT_BUDGET == TOOL_RESULT_MAX_CHARS


def test_clip_output_honours_an_explicit_limit():
    out = _clip_output("x" * 5000, limit=500)
    assert len(out) < 700
    assert "chars trimmed" in out


@pytest.mark.parametrize("stdout,stderr", [
    ("O" * 20000, "E" * 20000),   # both huge — the case that broke the budget
    ("O" * 20000, ""),
    ("", "E" * 20000),
    ("x\n" * 8000, "y\n" * 8000),  # newlines: JSON escaping doubles their cost
])
def test_result_payload_fits_the_transcript_budget(stdout, stderr):
    """Budget the two output streams together, not once each.

    Sizing them independently let a run with large stdout AND stderr reach
    ~3400 chars against a 2000 budget; verified_id then survived only by being
    first in the dict rather than because the cap worked.
    """
    import json

    from backend.tools import TRANSCRIPT_RESULT_BUDGET

    payload = {
        "verified_id": "run_1786698351741_3acf86e8",
        "show_this_code_by_writing": "```verified:run_1786698351741_3acf86e8\n```",
        "do_not": "Do not paste the code into your reply. The line above IS "
                  "the code block the learner sees.",
        "exit_code": 0,
        "duration_ms": 25,
    }
    room = max(200, TRANSCRIPT_RESULT_BUDGET - len(json.dumps(payload)) - 200)
    clipped_err = _clip_output(stderr, room)
    payload["stdout"] = _clip_output(stdout, max(200, room - len(clipped_err)))
    payload["stderr"] = clipped_err
    for _ in range(3):
        over = len(json.dumps(payload)) - TRANSCRIPT_RESULT_BUDGET
        if over <= 0:
            break
        longest = max(("stdout", "stderr"), key=lambda k: len(payload[k]))
        keep = max(100, len(payload[longest]) - over - 32)
        payload[longest] = payload[longest][:keep] + "\n... [trimmed]"

    serialized = json.dumps(payload)
    assert len(serialized) <= TRANSCRIPT_RESULT_BUDGET
    assert "verified_id" in serialized[:TRANSCRIPT_RESULT_BUDGET]


def test_reclipping_an_already_clipped_string_terminates():
    """_clip_output re-inserts its marker, so a naive shrink loop can spin.

    An earlier fix looped `while over > 0: text = _clip_output(text, smaller)`
    and never terminated, hanging the request handler — worse than the
    over-budget payload it was fixing.
    """
    text = _clip_output("x" * 5000, 500)
    for _ in range(5):
        shorter = _clip_output(text, max(100, len(text) - 50))
        assert len(shorter) <= len(text) + 40  # marker may re-add a little
        text = shorter


def test_clip_output_limit_keeps_both_ends():
    text = "HEAD" + ("m" * 5000) + "TAIL"
    out = _clip_output(text, limit=400)
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")


# --- verified source archive ---
#
# This exists because of a real failure: the tutor verified a snippet, then
# retyped it into chat with an extra export_text call that had never run, and
# that call was the one that crashed for the learner. The archive is what lets
# the frontend show the executed source instead of the model's retyping, so
# "byte-identical" is the property under test, not "roughly the same".


@pytest.fixture
def archive_email(tmp_path, monkeypatch):
    """Point the archive at a temp dir so tests never touch real user data."""
    monkeypatch.setattr("backend.codejobs.USERS_DIR", str(tmp_path))
    return "archive-test@example.com"


def test_verified_roundtrip_is_byte_identical(archive_email):
    code = 'x = 6 * 7\nprint("answer", x)\n'
    save_verified(archive_email, "run_1_abc123", code)
    assert load_verified(archive_email, "run_1_abc123") == code


def test_verified_preserves_exact_whitespace(archive_email):
    """Indentation is semantic in Python — a normalising write would corrupt it."""
    code = "def f():\n    if True:\n        return 1\n\n\n# trailing comment"
    save_verified(archive_email, "run_2_def456", code)
    assert load_verified(archive_email, "run_2_def456") == code


def test_verified_survives_unicode(archive_email):
    code = 'print("θ = 0.5 → café")\n'
    save_verified(archive_email, "run_3_aaa111", code)
    assert load_verified(archive_email, "run_3_aaa111") == code


def test_load_verified_missing_returns_none(archive_email):
    assert load_verified(archive_email, "run_nope_000000") is None


def test_verified_path_is_scoped_to_the_user(archive_email):
    """Two learners citing the same run id must not read each other's code."""
    save_verified(archive_email, "run_4_bbb222", "mine")
    other = "someone-else@example.com"
    assert load_verified(other, "run_4_bbb222") is None
    assert verified_path(archive_email, "run_4_bbb222") != verified_path(
        other, "run_4_bbb222")
