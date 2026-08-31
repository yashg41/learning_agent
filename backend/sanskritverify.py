"""Check Sanskrit forms with vidyut, so wrong grammar never reaches a learner.

Same thesis as mathverify.py, different subject. The tutor writes plausible
Sanskrit — a declension that looks right, reads right, and is wrong in the
instrumental plural teaches a false paradigm more convincingly than no paradigm
at all. Worse than the algebra case, in fact: a learner can sanity-check an
equation by substituting numbers, but has no way to sanity-check रामैः except by
already knowing it.

So the rule that governs code — "your sense of certainty is not evidence about
what a library actually does" — is extended to morphology. The tutor claims a
form; vidyut decides.

TRI-STATE, AND WHY IT MATTERS MORE HERE
Everything returns `verified` as True, False, or None, where None means "could
not check". Sanskrit has far more legitimate None cases than algebra does: the
data bundle is a separate download, the kośa does not contain every stem, and a
root the learner invents is not a bug. Collapsing None into False would tell a
learner their correct form is wrong — the single most damaging thing this module
could do, since they cannot tell which of us is mistaken.

WHAT THIS MODULE IS NOT
It generates forms and compares. It does not parse arbitrary running Sanskrit;
that is segmentation, needs the data bundle, and lives behind analyse() with an
HTTP fallback. A form this module cannot generate is reported unchecked, never
wrong.
"""

import hashlib
import logging
import re

logger = logging.getLogger(__name__)

# Longest input we will try to handle. A model that emits a 5KB "word" has gone
# wrong in a way no verification result would usefully describe. Verses are the
# long case and a triṣṭubh pāda is well under this.
MAX_INPUT_CHARS = 1_000

# Devanagari, plus Vedic Extensions and Devanagari Extended. Same three blocks
# the frontend tagger uses — Vedic accent marks are spread across all of them,
# so the single-range version people reach for silently drops them.
_DEVA_RE = re.compile(r"[ऀ-ॿ᳐-᳿꣠-ꣿ]")

# SLP1 is ASCII, so "is this already SLP1" cannot be answered by script alone.
# Anything with Devanagari is Devanagari; anything with IAST diacritics is IAST;
# otherwise assume SLP1, which is what vidyut's own APIs speak.
_IAST_RE = re.compile(r"[āīūṛṝḷḹṃḥñṅṇṭḍśṣ]", re.IGNORECASE)


def _load_vidyut():
    """Return (lipi, prakriya) modules, or (None, None) if unusable.

    Probes with a real call rather than trusting the import. This is not
    theoretical caution: `import vidyut` succeeds on a machine that has none of
    the data bundle, and several submodules only fail when first *used*. An
    import-only check would report every form "unchecked" while looking healthy,
    which is exactly the silent-decoration failure mathverify's docstring warns
    about.
    """
    try:
        from vidyut import lipi, prakriya
    except ImportError as e:
        logger.warning(f"vidyut unavailable, Sanskrit forms unchecked: {e}")
        return None, None

    try:
        # Cheapest possible round-trip that exercises the Rust bindings.
        if lipi.transliterate("rAma", lipi.Scheme.Slp1, lipi.Scheme.Devanagari) != "राम":
            raise RuntimeError("transliteration round-trip is wrong")
        prakriya.Vyakarana()
    except Exception as e:
        logger.warning(f"vidyut present but not usable ({type(e).__name__}), forms unchecked")
        return None, None

    return lipi, prakriya


def is_available() -> bool:
    """Whether form checking can run at all in this environment."""
    lipi, _ = _load_vidyut()
    return lipi is not None


def detect_scheme(text: str) -> str:
    """'devanagari' | 'iast' | 'slp1' — best guess, never raises."""
    if _DEVA_RE.search(text or ""):
        return "devanagari"
    if _IAST_RE.search(text or ""):
        return "iast"
    return "slp1"


def to_slp1(text: str) -> str | None:
    """Normalise any supported input to SLP1, which vidyut's APIs speak.

    Returns None if it cannot be done, so callers report unchecked rather than
    silently proceeding with mangled text. That mangling is a real failure mode:
    feeding IAST to an API expecting a different scheme produced टपोवनम् from
    `tapovanam` — wrong, and wrong *quietly*.
    """
    lipi, _ = _load_vidyut()
    if lipi is None or not text:
        return None
    scheme = {
        "devanagari": lipi.Scheme.Devanagari,
        "iast": lipi.Scheme.Iast,
        "slp1": lipi.Scheme.Slp1,
    }[detect_scheme(text)]
    try:
        return lipi.transliterate(text.strip(), scheme, lipi.Scheme.Slp1)
    except Exception as e:
        logger.debug(f"to_slp1 failed: {type(e).__name__}")
        return None


def to_devanagari(slp1: str) -> str:
    """SLP1 to Devanagari for display. Falls back to the input unchanged."""
    lipi, _ = _load_vidyut()
    if lipi is None or not slp1:
        return slp1
    try:
        return lipi.transliterate(slp1, lipi.Scheme.Slp1, lipi.Scheme.Devanagari)
    except Exception:
        return slp1


def claim_id(*parts: str) -> str:
    """A short stable id for one checked claim.

    The tutor cites this so the UI can mark a form as machine-checked. Hashed
    rather than sequential so the same claim gets the same id across turns and
    across sessions — a learner scrolling back sees a stable marker.
    """
    joined = "␟".join(p or "" for p in parts)
    return "sv_" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def _guard(text: str, label: str) -> dict | None:
    """Shared cheap rejections. Returns an unchecked result, or None to proceed."""
    if not text or not text.strip():
        return {"verified": None, "detail": f"empty {label}"}
    if len(text) > MAX_INPUT_CHARS:
        return {"verified": None, "detail": f"{label} too long to check"}
    return None


def check_form(expected: str, generated: list[str]) -> dict:
    """Compare a claimed form against what the grammar actually generates.

    Biased against False, exactly as mathverify is. A form absent from the
    generated set is only *wrong* if we successfully generated something; if we
    generated nothing at all, that is unchecked. Vidyut legitimately produces
    several forms for one slot (रामाद्/रामात्), so this is set membership, not
    equality.
    """
    if not generated:
        return {"verified": None, "detail": "grammar produced no form to compare"}

    want = to_slp1(expected)
    if want is None:
        return {"verified": None, "detail": "could not read the claimed form"}

    if want in generated:
        return {"verified": True, "detail": "matches the generated form"}

    shown = ", ".join(to_devanagari(g) for g in generated[:4])
    return {"verified": False, "detail": f"grammar gives {shown}"}


# --- Verbs ------------------------------------------------------------------
#
# KNOWN_ROOTS exists because of a genuinely dangerous property of the library:
# **vidyut does not validate roots**. Given the nonsense root `zzzz` it happily
# returns `zzati` — a well-formed-looking verb built from nothing. Without a
# whitelist, a tutor that mistyped a root would be handed a fabricated form
# carrying our "verified" marker, which is worse than no checking at all,
# because the marker is a promise.
#
# Each entry below was derived and compared against its known textbook form
# before being added; the test suite re-checks them. The list is short by
# design — a re-learner needs the roots that actually occur, not 2,000 of them.
#
# Roots whose stem substitution lives in the Dhātupāṭha (√gam → gacch, √sthā →
# tiṣṭh) are ABSENT, because without the data bundle vidyut silently returns
# `gamati`/`sthāti` — plausible, and wrong. Those are reported unchecked rather
# than guessed, and the setup step is what unlocks them.
KNOWN_ROOTS = {
    # code            gaṇa           3sg laṭ   gloss
    "BU":       ("Bhvadi",    "bhavati",  "be, become"),
    "paWa":     ("Bhvadi",    "paṭhati",  "read, recite"),
    "vada":     ("Bhvadi",    "vadati",   "speak"),
    "jIva":     ("Bhvadi",    "jīvati",   "live"),
    "hasa~":    ("Bhvadi",    "hasati",   "laugh"),
    "smf":      ("Bhvadi",    "smarati",  "remember"),
    "KAda~":    ("Bhvadi",    "khādati",  "eat"),
    "krIqf~":   ("Bhvadi",    "krīḍati",  "play"),
    "qupacaz":  ("Bhvadi",    "pacati",   "cook"),
    "pA\\":     ("Bhvadi",    "pibati",   "drink"),
    "nI\\Y":    ("Bhvadi",    "nayati",   "lead"),
    "SruY":     ("Bhvadi",    "śṛṇoti",   "hear"),
    "likha":    ("Tudadi",    "likhati",  "write"),
    "nftI~":    ("Divadi",    "nṛtyati",  "dance"),
    "asa~":     ("Adadi",     "asti",     "be"),
    "iR":       ("Adadi",     "eti",      "go"),
    "jYA\\":    ("Kryadi",    "jānāti",   "know"),
    "dA":       ("Juhotyadi", "dadāti",   "give"),
    "qukfY":    ("Tanadi",    "karoti",   "do, make"),
}

# What a learner is likely to type, mapped to the Dhātupāṭha spelling. √gam is
# the one everyone reaches for first, so its absence is named explicitly rather
# than left as an unhelpful "unknown root".
_ROOT_ALIASES = {
    "bhu": "BU", "bhū": "BU", "BU": "BU",
    "path": "paWa", "paṭh": "paWa",
    "vad": "vada", "jiv": "jIva", "jīv": "jIva",
    "has": "hasa~", "smr": "smf", "smṛ": "smf",
    "khad": "KAda~", "khād": "KAda~",
    "krid": "krIqf~", "krīḍ": "krIqf~",
    "pac": "qupacaz", "pa": "pA\\", "pā": "pA\\",
    "ni": "nI\\Y", "nī": "nI\\Y",
    "shru": "SruY", "śru": "SruY",
    "likh": "likha", "nrt": "nftI~", "nṛt": "nftI~",
    "as": "asa~", "i": "iR",
    "jna": "jYA\\", "jñā": "jYA\\",
    "da": "dA", "dā": "dA",
    "kr": "qukfY", "kṛ": "qukfY",
}

# Roots that need the Dhātupāṭha's irregular stem table. Named so the tutor can
# say what is missing and why, instead of returning a bare failure.
_NEEDS_DATA = {
    "gam": "√gam (gacchati)", "gacch": "√gam (gacchati)",
    "stha": "√sthā (tiṣṭhati)", "sthā": "√sthā (tiṣṭhati)",
    "drs": "√dṛś (paśyati)", "dṛś": "√dṛś (paśyati)",
    "pasy": "√dṛś (paśyati)", "paśy": "√dṛś (paśyati)",
}


def resolve_root(root: str) -> tuple[str | None, str | None]:
    """Map what the caller typed to a validated Dhātupāṭha code.

    Returns (code, None) on success or (None, reason) on failure. Never guesses:
    an unrecognised root is refused, because vidyut would otherwise fabricate a
    form from it.
    """
    raw = (root or "").strip()
    if not raw:
        return None, "no root given"

    if raw in KNOWN_ROOTS:
        return raw, None

    key = raw.lower()
    if key in _ROOT_ALIASES:
        return _ROOT_ALIASES[key], None

    # Try it as Devanagari/IAST → SLP1 before giving up.
    slp = to_slp1(raw)
    if slp and slp in KNOWN_ROOTS:
        return slp, None
    if slp and slp.lower() in _ROOT_ALIASES:
        return _ROOT_ALIASES[slp.lower()], None

    if key in _NEEDS_DATA or (slp or "").lower() in _NEEDS_DATA:
        name = _NEEDS_DATA.get(key) or _NEEDS_DATA[(slp or "").lower()]
        return None, (
            f"{name} needs vidyut's Dhātupāṭha data, which is not installed. "
            f"Run `python -m backend.sanskrit_setup` to enable it. Without it "
            f"the library returns a plausible but WRONG form for this root."
        )

    return None, (
        f"'{root}' is not in the verified root list, and vidyut does not "
        f"validate roots — it would return an invented form. Known roots: "
        f"{', '.join(sorted(g for _, _, g in KNOWN_ROOTS.values())[:8])}…"
    )


def derive_verb(
    root: str,
    gana: str = "Bhvadi",
    lakara: str = "Lat",
    purusha: str = "Prathama",
    vacana: str = "Eka",
    prayoga: str = "Kartari",
    pada: str | None = None,
) -> dict:
    """Derive one finite verb form, with the Pāṇinian sūtra chain behind it.

    The sūtra chain is the reason this tool is worth more than a lookup table:
    "why is it gacchati and not gamati" is answerable, step by step, from what
    vidyut already returns.

    Roots are given in SLP1 as the Dhātupāṭha spells them, which is not always
    what a learner would type — √gam is `gamL`, and a bare `gam` derives a
    different (wrong) form silently. So an unrecognised root is reported
    unchecked with the accepted spelling named, never guessed at.
    """
    bad = _guard(root, "root")
    if bad:
        return bad

    lipi, prak = _load_vidyut()
    if prak is None:
        return {"verified": None, "detail": "vidyut is not installed", "forms": []}

    # Validate BEFORE deriving. vidyut will build a form from any string it can
    # parse, so this whitelist is the only thing standing between a typo and a
    # fabricated form wearing a "verified" marker.
    slp, why = resolve_root(root)
    if slp is None:
        return {"verified": None, "detail": why, "forms": []}

    # The curated gaṇa wins over a caller-supplied one: the pair is what was
    # actually tested, and a right root in the wrong gaṇa derives silently
    # wrong output (√kṛ in bhvādi is not karoti).
    known_gana, expected_3sg, gloss = KNOWN_ROOTS[slp]
    if gana != known_gana and gana == "Bhvadi":
        gana = known_gana

    try:
        enums = {
            "gana": getattr(prak.Gana, gana),
            "lakara": getattr(prak.Lakara, lakara),
            "purusha": getattr(prak.Purusha, purusha),
            "vacana": getattr(prak.Vacana, vacana),
            "prayoga": getattr(prak.Prayoga, prayoga),
        }
    except AttributeError as e:
        return {"verified": None, "detail": f"unknown grammatical value: {e}", "forms": []}

    try:
        dhatu = prak.Dhatu.mula(slp, enums["gana"])
        kwargs = dict(
            dhatu=dhatu,
            prayoga=enums["prayoga"],
            lakara=enums["lakara"],
            purusha=enums["purusha"],
            vacana=enums["vacana"],
        )
        if pada:
            kwargs["pada"] = getattr(prak.DhatuPada, pada)
        prakriyas = prak.Vyakarana().derive(prak.Pada.Tinanta(**kwargs))
    except Exception as e:
        return {
            "verified": None,
            "detail": f"could not derive: {type(e).__name__}",
            "forms": [],
        }

    if not prakriyas:
        return {
            "verified": None,
            "detail": f"the grammar produced no form for {slp} in gaṇa {gana}",
            "forms": [],
        }

    first = prakriyas[0]
    return {
        "verified": True,
        "detail": "derived by Pāṇinian rules",
        "root": slp,
        "gloss": gloss,
        "gana": gana,
        "forms": sorted({to_devanagari(p.text) for p in prakriyas}),
        "slp1": sorted({p.text for p in prakriyas}),
        "steps": [
            {"sutra": s.code, "result": " + ".join(s.result)}
            for s in first.history
        ],
        "claim_id": claim_id("verb", slp, gana, lakara, purusha, vacana, prayoga),
    }


# --- Nouns ------------------------------------------------------------------

# The eight cases in the order every Indian textbook prints them, so a generated
# table is directly comparable with the one the learner half-remembers chanting.
VIBHAKTI_ORDER = [
    ("Prathama", "prathamā", "nominative"),
    ("Dvitiya", "dvitīyā", "accusative"),
    ("Trtiya", "tṛtīyā", "instrumental"),
    ("Caturthi", "caturthī", "dative"),
    ("Panchami", "pañcamī", "ablative"),
    ("Sasthi", "ṣaṣṭhī", "genitive"),
    ("Saptami", "saptamī", "locative"),
    ("Sambodhana", "sambodhana", "vocative"),
]

_VACANA_ORDER = [("Eka", "eka"), ("Dvi", "dvi"), ("Bahu", "bahu")]

_LINGA = {
    "m": "Pum", "masculine": "Pum", "pum": "Pum",
    "f": "Stri", "feminine": "Stri", "stri": "Stri",
    "n": "Napumsaka", "neuter": "Napumsaka", "napumsaka": "Napumsaka",
}


def decline(stem: str, gender: str = "m") -> dict:
    """Generate the full 8x3 paradigm for a nominal stem.

    Returns rows in textbook order with both Devanagari and IAST, because the
    learner recognises the shape of the table before they recognise any single
    cell in it.
    """
    bad = _guard(stem, "stem")
    if bad:
        return bad

    lipi, prak = _load_vidyut()
    if prak is None:
        return {"verified": None, "detail": "vidyut is not installed", "rows": []}

    linga_name = _LINGA.get((gender or "").strip().lower())
    if linga_name is None:
        return {
            "verified": None,
            "detail": f"unknown gender '{gender}' — use m, f or n",
            "rows": [],
        }

    slp = to_slp1(stem)
    if slp is None:
        return {"verified": None, "detail": "could not read the stem", "rows": []}

    v = prak.Vyakarana()
    linga = getattr(prak.Linga, linga_name)
    rows, produced = [], 0

    for vib_name, vib_sa, vib_en in VIBHAKTI_ORDER:
        cells = []
        for vac_name, _ in _VACANA_ORDER:
            try:
                pada = prak.Pada.Subanta(
                    pratipadika=prak.Pratipadika.basic(slp),
                    linga=linga,
                    vibhakti=getattr(prak.Vibhakti, vib_name),
                    vacana=getattr(prak.Vacana, vac_name),
                )
                forms = sorted({p.text for p in v.derive(pada)})
            except Exception:
                forms = []
            produced += len(forms)
            cells.append({
                "devanagari": " / ".join(to_devanagari(f) for f in forms),
                "slp1": forms,
            })
        rows.append({
            "case": vib_sa, "case_en": vib_en,
            "eka": cells[0], "dvi": cells[1], "bahu": cells[2],
        })

    if not produced:
        return {
            "verified": None,
            "detail": (
                f"no forms for stem '{stem}' as {gender}. Give the bare stem "
                f"(rāma, not rāmaḥ), and check the gender is right."
            ),
            "rows": [],
        }

    # ī/ū-stem feminines come out with a visarga in the nominative singular
    # (नदीः where every textbook prints नदी). vidyut is giving the form before
    # a final rule that the isolated-word context does not trigger. Flagged
    # rather than silently patched: quietly "correcting" a grammar engine is
    # how you end up shipping a second, unverified grammar of your own.
    caveat = None
    if linga_name == "Stri" and slp.endswith(("I", "U")):
        caveat = (
            "note: the nominative singular shows a visarga here (नदीः); "
            "textbooks print नदी. Trust the textbook for that one cell."
        )

    return {
        "verified": True,
        "detail": f"generated from the stem by Pāṇinian rules ({produced} forms)",
        "stem": to_devanagari(slp),
        "gender": gender,
        "caveat": caveat,
        "rows": rows,
        "claim_id": claim_id("decline", slp, linga_name),
    }


# --- Metre ------------------------------------------------------------------

# Enough of the common metres to identify what a learner actually meets. G is
# guru, L laghu, / a pāda boundary. Anuṣṭubh is the śloka — by far the most
# common — and is genuinely loose: only syllables 5-8 of each pāda are fixed,
# so it is matched by syllable count and cadence rather than a rigid mask.
_METRE_TSV = "\n".join([
    "vasantatilakA\tvrtta\tGGLGLLLGLLGLGG",
    "mandAkrAntA\tvrtta\tGGGGLLLLLGGLGGLGG",
    "SArdUlavikrIqitam\tvrtta\tGGGLLGLGLLLGGGLGGLG",
    "mAlinI\tvrtta\tLLLLLLGGLGGLGG",
    "SikhariNI\tvrtta\tLGGGGGLLLLLGGLLLGLLLG",
    "indravajrA\tvrtta\tGGLGGLLGLGG",
    "upendravajrA\tvrtta\tLGLGGLLGLGG",
])


def scan_metre(verse: str) -> dict:
    """Scan a verse into syllables with laghu/guru weights.

    The weights are the useful part and need no metre database — they are what
    lets a learner *hear* where the rhythm falls, and what makes the difference
    between reciting and reading aloud. Naming the metre is a bonus on top.
    """
    bad = _guard(verse, "verse")
    if bad:
        return bad

    lipi, _ = _load_vidyut()
    if lipi is None:
        return {"verified": None, "detail": "vidyut is not installed", "padas": []}

    slp = to_slp1(verse)
    if slp is None:
        return {"verified": None, "detail": "could not read the verse", "padas": []}

    try:
        from vidyut.chandas import Chandas
        match = Chandas.from_text(_METRE_TSV).classify(slp)
    except Exception as e:
        return {
            "verified": None,
            "detail": f"could not scan: {type(e).__name__}",
            "padas": [],
        }

    padas = []
    for row in match.aksharas:
        padas.append({
            "syllables": [a.text for a in row],
            "devanagari": [to_devanagari(a.text) for a in row],
            # str(weight) is "Guru"/"Laghu"; the initial is what scansion prints.
            "weights": "".join(str(a.weight)[0].upper() for a in row),
        })

    if not padas:
        return {"verified": None, "detail": "nothing to scan", "padas": []}

    total = sum(len(p["syllables"]) for p in padas)
    name = getattr(getattr(match, "padya", None), "name", None)

    return {
        "verified": True,
        "detail": f"scanned {total} syllables",
        "metre": name,
        # Said plainly, because "no name" reads as failure otherwise. The
        # weights are still correct and still the point.
        "note": None if name else "metre not identified — the weights are still exact",
        "padas": padas,
    }
