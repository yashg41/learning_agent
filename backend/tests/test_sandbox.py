"""Unit tests for the code runner's pure helpers.

These cover the logic that defeated a learner who ran
`subprocess.run(["pip", "install", "sklearn"])` in a cell: the deprecated-shim
rewrite, the import-name/package-name mismatch, and the magic-line stripping
that gives them a working `%pip install` instead.

No subprocess here — the process plumbing is exercised end-to-end by hand.
"""

import json

import pytest

from backend.sandbox import (
    VENV_MARKER,
    VENV_SCHEMA,
    _venv_ready,
    missing_module_from_stderr,
    normalize_packages,
    packages_from_magic,
    split_magics,
    suggest_package,
)


# --- split_magics ---


def test_split_magics_extracts_pip_install():
    magics, rest = split_magics("%pip install scikit-learn\nprint(1)")
    assert magics == [["install", "scikit-learn"]]
    assert rest.strip() == "print(1)"


def test_split_magics_preserves_line_numbers():
    """A stripped magic becomes a blank line so tracebacks stay honest."""
    code = "import numpy\n%pip install foo\nraise ValueError()"
    _, rest = split_magics(code)
    assert rest.split("\n") == ["import numpy", "", "raise ValueError()"]


def test_split_magics_accepts_bang_and_cell_magic():
    assert split_magics("!pip install foo")[0] == [["install", "foo"]]
    assert split_magics("%%pip install foo")[0] == [["install", "foo"]]
    assert split_magics("!pip3 install foo")[0] == [["install", "foo"]]


def test_split_magics_recognised_mid_cell_and_indented():
    magics, _ = split_magics("x = 1\n  %pip install foo\ny = 2")
    assert magics == [["install", "foo"]]


def test_split_magics_translates_conda():
    magics, _ = split_magics("!conda install -y numpy")
    assert magics == [["install", "numpy"]]


def test_split_magics_leaves_unknown_magics_alone():
    """Silently dropping a line we don't understand is worse than a SyntaxError."""
    code = "%timeit foo()"
    magics, rest = split_magics(code)
    assert magics == []
    assert rest == code


def test_split_magics_ignores_plain_code():
    code = "x = 5 % 2\nprint(x != 1)"
    magics, rest = split_magics(code)
    assert magics == []
    assert rest == code


def test_split_magics_survives_unbalanced_quotes():
    magics, _ = split_magics("%pip install 'foo")
    assert magics == []


# --- packages_from_magic ---


def test_packages_from_magic_drops_flags():
    assert packages_from_magic(["install", "-q", "numpy", "--upgrade"]) == ["numpy"]


def test_packages_from_magic_ignores_other_subcommands():
    assert packages_from_magic(["list"]) == []
    assert packages_from_magic([]) == []


# --- normalize_packages ---


def test_normalize_rewrites_deprecated_sklearn_shim():
    pkgs, notes = normalize_packages(["sklearn"])
    assert pkgs == ["scikit-learn"]
    assert len(notes) == 1 and "scikit-learn" in notes[0]


def test_normalize_leaves_explicit_specifiers_alone():
    """A version pin means the learner was deliberate; don't second-guess."""
    pkgs, notes = normalize_packages(["scikit-learn==1.3.0"])
    assert pkgs == ["scikit-learn==1.3.0"]
    assert notes == []


def test_normalize_passes_through_ordinary_packages():
    pkgs, notes = normalize_packages(["numpy", "pandas"])
    assert pkgs == ["numpy", "pandas"]
    assert notes == []


# --- missing_module_from_stderr ---


def test_missing_module_from_traceback():
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "cell.py", line 2, in <module>\n'
        "    from sklearn.preprocessing import PolynomialFeatures\n"
        "ModuleNotFoundError: No module named 'sklearn'\n"
    )
    assert missing_module_from_stderr(stderr) == "sklearn"


def test_missing_module_reduces_submodule_to_top_level():
    """Installing 'sklearn.ensemble' would fail; we want the distribution."""
    stderr = "ModuleNotFoundError: No module named 'sklearn.ensemble'"
    assert missing_module_from_stderr(stderr) == "sklearn"


def test_missing_module_absent_for_other_errors():
    assert missing_module_from_stderr("ValueError: nope") is None
    assert missing_module_from_stderr("") is None
    assert missing_module_from_stderr(None) is None


# --- suggest_package ---


@pytest.mark.parametrize("module,expected", [
    ("sklearn", "scikit-learn"),
    ("cv2", "opencv-python"),
    ("PIL", "pillow"),
    ("bs4", "beautifulsoup4"),
    ("requests", "requests"),   # unknown -> assume import name == package name
])
def test_suggest_package(module, expected):
    assert suggest_package(module) == expected


# --- _venv_ready ---


def _make_venv(tmp_path, marker: dict | None):
    """A venv skeleton: python binary always, marker only if given."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    if marker is not None:
        (venv / VENV_MARKER).write_text(json.dumps(marker))
    return venv


def test_venv_not_ready_without_marker(tmp_path):
    """The bug this marker fixes: a python binary exists the instant
    `python -m venv` returns, i.e. before pip has installed anything."""
    assert _venv_ready(_make_venv(tmp_path, None)) is False


def test_venv_ready_with_current_schema(tmp_path):
    assert _venv_ready(_make_venv(tmp_path, {"schema": VENV_SCHEMA})) is True


def test_venv_not_ready_on_stale_schema(tmp_path):
    assert _venv_ready(_make_venv(tmp_path, {"schema": VENV_SCHEMA + 1})) is False


def test_venv_not_ready_on_corrupt_marker(tmp_path):
    venv = _make_venv(tmp_path, None)
    (venv / VENV_MARKER).write_text("{not json")
    assert _venv_ready(venv) is False


def test_venv_not_ready_when_python_missing(tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / VENV_MARKER).write_text(json.dumps({"schema": VENV_SCHEMA}))
    assert _venv_ready(venv) is False
