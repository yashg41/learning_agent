"""The Sanskrit correctness layer.

The property worth the most here is the tri-state: `None` means "could not
check" and must never be confused with either agreement or disagreement. A
learner cannot verify रामैः for themselves — that is what they came to learn —
so a wrong form carrying a verified marker is the worst thing this system can
produce, and an unchecked one presented as checked is the same failure wearing
a different hat.

The golden set below is small and hand-checked against textbook paradigms. It
is not trying to test vidyut; it is testing that OUR wiring of vidyut still
produces the forms a learner would recognise.
"""

import pytest

from backend.sanskritverify import (
    KNOWN_ROOTS,
    MAX_INPUT_CHARS,
    check_form,
    claim_id,
    decline,
    derive_verb,
    detect_scheme,
    is_available,
    resolve_root,
    scan_metre,
    to_devanagari,
    to_slp1,
)

# vidyut is optional: the app must run without it. Where a test needs real
# grammar, skip rather than fail, and let the absence tests carry the load.
needs_vidyut = pytest.mark.skipif(
    not is_available(), reason="vidyut is not installed"
)


# --- Script handling --------------------------------------------------------

def test_detect_scheme():
    assert detect_scheme("रामः") == "devanagari"
    assert detect_scheme("rāmaḥ") == "iast"
    assert detect_scheme("rAmaH") == "slp1"


@needs_vidyut
def test_round_trip_through_slp1():
    """Devanagari must survive the trip out and back.

    Not pedantry: every tool normalises to SLP1 internally, so a lossy
    conversion would corrupt input before any checking happened — and would do
    it silently, which is the part that matters.
    """
    for word in ("रामः", "गच्छति", "संस्कृतम्", "कृष्णः"):
        assert to_devanagari(to_slp1(word)) == word


@needs_vidyut
def test_iast_is_accepted_as_input():
    """A learner types Latin; the tools must read it."""
    assert to_slp1("rāmaḥ") == to_slp1("रामः")


def test_claim_id_is_stable_and_distinct():
    """The UI marks checked forms by this id, so it must not drift per call."""
    assert claim_id("a", "b") == claim_id("a", "b")
    assert claim_id("a", "b") != claim_id("a", "c")
    assert claim_id("verb", "BU").startswith("sv_")


# --- The tri-state ----------------------------------------------------------

def test_empty_input_is_unchecked_not_wrong():
    for result in (derive_verb(""), decline(""), scan_metre("")):
        assert result["verified"] is None


def test_oversized_input_is_unchecked_not_wrong():
    huge = "अ" * (MAX_INPUT_CHARS + 10)
    assert scan_metre(huge)["verified"] is None
    assert decline(huge)["verified"] is None


def test_unknown_gender_is_unchecked():
    r = decline("rAma", "q")
    assert r["verified"] is None
    assert "gender" in r["detail"]


@needs_vidyut
def test_check_form_distinguishes_all_three_states():
    """The distinction the whole module exists for."""
    assert check_form("रामः", ["rAmaH"])["verified"] is True
    assert check_form("रामम्", ["rAmaH"])["verified"] is False
    # Nothing generated is NOT disagreement.
    assert check_form("रामः", [])["verified"] is None


# --- Root validation --------------------------------------------------------

def test_invented_root_is_refused():
    """The bug this guard exists for.

    vidyut does not validate roots: given `zzzz` it returns `zzati`, a
    well-formed-looking verb built from nothing. Without the whitelist a typo
    would produce a fabricated form carrying our verified marker.
    """
    code, why = resolve_root("zzzz")
    assert code is None
    assert "not in the verified root list" in why

    result = derive_verb("zzzz")
    assert result["verified"] is None
    assert not result.get("forms")


def test_roots_needing_the_data_bundle_say_so():
    """√gam is the first root anyone reaches for, and it is data-dependent.

    WITH the Dhātupāṭha it must derive गच्छति. WITHOUT it, vidyut returns
    `gamati` rather than an error — plausible and wrong — so it must be refused
    with the fix named, never guessed at.
    """
    from backend.sanskritverify import _dhatupatha_dir

    if _dhatupatha_dir() is not None:
        result = derive_verb("gam")
        assert result["verified"] is True, result["detail"]
        assert "गच्छति" in result["forms"], result["forms"]
    else:
        code, why = resolve_root("gam")
        assert code is None
        assert "gacchati" in why
        assert "sanskrit_setup" in why


@needs_vidyut
@pytest.mark.parametrize("typed,expected", [
    ("gam", "गच्छति"),      # needs the Dhātupāṭha's irregular stem
    ("sthā", "तिष्ठति"),     # listed under a retroflex spelling (zWA)
    ("dṛś", "पश्यति"),       # listed as dfSi~r, and suppletive
    ("pā", "पिबति"),         # suppletive, and a near-miss for √pat
    ("kṛ", "करोति"),         # listed as qukfY
])
def test_irregular_roots_resolve_to_the_right_stem(typed, expected):
    """The roots where a near-miss returns a real-but-wrong form.

    √pā is the cautionary one: a general `slp + "a"` fallback matched √pat
    ("fall") and returned पतति for "drink" — a real root, a real form, and the
    wrong answer wearing a verified marker. Near-misses must refuse, not land
    on a neighbour.
    """
    from backend.sanskritverify import _dhatupatha_dir

    if _dhatupatha_dir() is None:
        pytest.skip("needs the vidyut data bundle")

    result = derive_verb(typed)
    assert result["verified"] is True, result["detail"]
    assert expected in result["forms"], f"{typed} gave {result['forms']}"


@needs_vidyut
def test_near_misses_are_refused_not_redirected():
    """A typo must not resolve to a neighbouring real root."""
    for typo in ("gaam", "zzzz", "xyzzy", "qqq"):
        assert derive_verb(typo)["verified"] is None, typo


def test_common_spellings_resolve():
    for typed in ("bhū", "bhu", "BU"):
        code, why = resolve_root(typed)
        assert code == "BU", f"{typed} did not resolve ({why})"


# --- Golden set: forms a learner would recognise ----------------------------

@needs_vidyut
@pytest.mark.parametrize("code,expected", [
    ("BU", "भवति"), ("paWa", "पठति"), ("vada", "वदति"),
    ("asa~", "अस्ति"), ("qukfY", "करोति"), ("pA\\", "पिबति"),
    ("jYA\\", "जानाति"), ("dA", "ददाति"), ("SruY", "शृणोति"),
])
def test_verb_golden_set(code, expected):
    """laṭ 3sg for each curated root, against the textbook form."""
    result = derive_verb(code)
    assert result["verified"] is True, result["detail"]
    assert expected in result["forms"], f"got {result['forms']}"


@needs_vidyut
def test_every_curated_root_derives():
    """KNOWN_ROOTS is a promise: each entry was checked when added."""
    for code, (gana, expected_iast, _) in KNOWN_ROOTS.items():
        result = derive_verb(code, gana)
        assert result["verified"] is True, f"{code}: {result['detail']}"
        assert result.get("forms"), f"{code} produced nothing"


@needs_vidyut
def test_derivation_returns_the_sutra_chain():
    """The sūtra chain is why this beats a lookup table."""
    result = derive_verb("BU")
    steps = result["steps"]
    assert steps, "no derivation steps"
    assert all("." in s["sutra"] for s in steps), "sutra codes look wrong"


@needs_vidyut
def test_rama_paradigm_matches_the_textbook():
    """The table every Indian schoolchild chants."""
    result = decline("rAma", "m")
    assert result["verified"] is True

    by_case = {r["case"]: r for r in result["rows"]}
    assert by_case["prathamā"]["sg"] == "रामः"
    assert by_case["prathamā"]["pl"] == "रामाः"
    assert by_case["dvitīyā"]["sg"] == "रामम्"
    assert by_case["tṛtīyā"]["sg"] == "रामेण"
    assert by_case["tṛtīyā"]["pl"] == "रामैः"
    assert by_case["ṣaṣṭhī"]["sg"] == "रामस्य"


@needs_vidyut
def test_neuter_paradigm():
    result = decline("Pala", "n")
    by_case = {r["case"]: r for r in result["rows"]}
    # The neuter's defining feature: nominative and accusative are identical.
    assert by_case["prathamā"]["sg"] == "फलम्"
    assert by_case["dvitīyā"]["sg"] == "फलम्"
    assert by_case["prathamā"]["pl"] == "फलानि"


@needs_vidyut
def test_feminine_i_stem_carries_its_caveat():
    """vidyut gives नदीः where textbooks give नदी.

    Flagged rather than silently patched — quietly "correcting" a grammar
    engine means shipping a second, unverified grammar of your own.
    """
    result = decline("nadI", "f")
    assert result["verified"] is True
    assert result["caveat"] and "नदी" in result["caveat"]


@needs_vidyut
def test_declension_covers_all_eight_cases_and_three_numbers():
    result = decline("rAma", "m")
    assert len(result["rows"]) == 8
    for row in result["rows"]:
        for number in ("sg", "du", "pl"):
            assert row[number], f"{row['case']} {number} is empty"


# --- Metre ------------------------------------------------------------------

@needs_vidyut
def test_scansion_weights_are_correct():
    """The opening of the Gītā, scanned by hand and checked here.

    dhar-ma-kṣe-tre: the first syllable is closed, hence guru; the weights are
    what let a learner hear where the beat falls.
    """
    result = scan_metre("धर्मक्षेत्रे कुरुक्षेत्रे समवेता युयुत्सवः")
    assert result["verified"] is True
    weights = "".join(p["weights"] for p in result["padas"])
    assert weights == "GGGGLGGGLLGGLGLG", weights
    assert set(weights) <= {"G", "L"}


@needs_vidyut
def test_scansion_syllabifies():
    result = scan_metre("रामः")
    syllables = result["padas"][0]["devanagari"]
    assert syllables == ["रा", "मः"], syllables


@needs_vidyut
def test_unnamed_metre_still_reports_weights():
    """A metre with no name is not a failure — the weights are the point."""
    result = scan_metre("रामः गच्छति")
    assert result["verified"] is True
    assert result["padas"]
    if not result.get("metre"):
        assert result["note"]


# --- Degrading without vidyut -----------------------------------------------

def test_everything_degrades_when_vidyut_is_missing(monkeypatch):
    """The app must run without vidyut, reporting unchecked rather than crashing.

    Same posture as a missing PyMuPDF disabling only PDF import.
    """
    import backend.sanskritverify as sv

    monkeypatch.setattr(sv, "_load_vidyut", lambda: (None, None))

    for result in (
        sv.derive_verb("BU"),
        sv.decline("rAma", "m"),
        sv.scan_metre("रामः"),
    ):
        assert result["verified"] is None
        assert "vidyut" in result["detail"]

    assert sv.to_slp1("रामः") is None
    assert sv.is_available() is False


@needs_vidyut
@pytest.mark.parametrize("stem,gender", [
    ("rAjan", "m"), ("nadI", "f"), ("rAma", "m"), ("Pala", "n"),
])
def test_paradigm_fits_the_transcript_budget(stem, gender):
    """A truncated payload is read as a broken tool, not as a long one.

    Every tool result is written into the SDK transcript, which agent.py cuts
    at TOOL_RESULT_MAX_CHARS. The first version of this payload carried SLP1
    alongside Devanagari for all 24 cells and landed at ~1900 chars — close
    enough that a longer stem truncated the JSON mid-structure. The tutor read
    the wreckage as "vidyut isn't installed" and told the learner so, which is
    the worst possible failure: a working tool reported as broken.
    """
    import json

    from backend.agent import TOOL_RESULT_MAX_CHARS

    payload = json.dumps(decline(stem, gender), ensure_ascii=False)
    assert len(payload) < TOOL_RESULT_MAX_CHARS * 0.75, (
        f"{stem} paradigm is {len(payload)} chars against a "
        f"{TOOL_RESULT_MAX_CHARS} cap — too close to truncation"
    )
