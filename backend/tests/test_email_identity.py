"""Email is one identity, whatever casing it arrives in.

The bug these guard against: `_safe_email` lowercased every filesystem path,
but ChromaDB metadata kept the raw string and was filtered with exact match, so
one learner typing "YASHG41" and "yashg41" became two half-histories. The tutor
reads episodic memory into every system prompt, so the visible symptom was
silent amnesia about a third of the learner's past.
"""

from backend.memory import _normalize_email, _safe_email


# --- Normalization ---


def test_casing_and_whitespace_fold_to_one_key():
    for variant in ("YASHG41", "yashg41", "YashG41", "  yashg41  ", "\tYASHG41\n"):
        assert _normalize_email(variant) == "yashg41"


def test_normalize_does_not_substitute_characters():
    """The distinction from _safe_email, and the reason it exists separately.

    _safe_email maps anything outside [\\w@.\\-] to "_", which is right for a
    directory name but wrong for an identity: it collapses "yash+ml@gmail.com"
    and "yash_ml@gmail.com" onto one key, silently merging two learners'
    memories. As a Chroma metadata value there is no path to sanitize.
    """
    assert _normalize_email("yash+ml@gmail.com") == "yash+ml@gmail.com"
    assert _safe_email("yash+ml@gmail.com") == _safe_email("yash_ml@gmail.com")


def test_normalize_is_idempotent():
    once = _normalize_email("  YASHG41 ")
    assert _normalize_email(once) == once


# --- The path helper still behaves exactly as before ---


def test_safe_email_still_lowercases_and_sanitizes():
    assert _safe_email("YashG41") == "yashg41"
    assert _safe_email("  ABC@Gmail.com ") == "abc@gmail.com"


def test_safe_email_strips_path_separators():
    """Containment rests on this — see EpisodicMemory.uploads_dir_for.

    A bare ".." survives ("." is legal in an address, so the character class
    keeps it) but cannot traverse without a separator, and every caller joins
    the result as a single path segment.
    """
    assert _safe_email("../../etc/passwd") == ".._.._etc_passwd"
    assert "/" not in _safe_email("../../etc/passwd")
    assert "/" not in _safe_email("a/b")


def test_safe_email_composes_on_normalize():
    """Path derivation must not diverge from identity on casing."""
    for variant in ("YASHG41", "yashg41", " YashG41 "):
        assert _safe_email(variant) == _safe_email(_normalize_email(variant))
