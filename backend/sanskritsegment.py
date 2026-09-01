"""Split continuous Sanskrit into words, and look words up.

Separate from sanskritverify.py because these two need data that module does
not: vidyut's kośa and segmentation model are a ~32 MB download, and until it
has been fetched the only route is an HTTP API. Keeping the network-touching
code out of the verifier means the verifier stays synchronous, offline and
fast, which is what lets it sit in front of every form the tutor states.

Sandhi resolution is the thing this is really for. Applying sandhi forward is
the easy half and the half schools drill; undoing it in running text, where the
word boundaries simply are not written, is where learners stall. A tutor that
can split reliably is doing the one job the textbook cannot.

FALLBACK ORDER, AND THE RULE ABOUT IT
  1. vidyut.cheda — local, exact, needs the bundle
  2. Samsaadhanii (Univ. of Hyderabad) — remote, good, no SLA
  3. report unchecked
Never a guess. The remote call is enrichment behind a short timeout and a
cache: a university CGI being slow must degrade the lesson, never stall it. If
both are unavailable the tutor is told to say so, which is a better lesson than
a confident wrong split.
"""

import hashlib
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

from backend.config import settings
from backend.sanskritverify import (
    MAX_INPUT_CHARS,
    claim_id,
    detect_scheme,
    to_devanagari,
    to_slp1,
)

logger = logging.getLogger(__name__)

SAMSAADHANII_BASE = "https://sanskrit.uohyd.ac.in/cgi-bin/scl"

# Short on purpose. This is a nice-to-have on someone else's university server;
# the learner is watching a reply compose and would rather have "I could not
# split this" quickly than a correct answer in thirty seconds.
HTTP_TIMEOUT_SEC = 8

# A split never changes for the same input, so the cache is permanent and the
# key is a hash — the input is Devanagari and would not survive being a
# filename. `schema` lets a format change invalidate old entries rather than
# have them silently misread (the trick sandbox._venv_ready uses).
CACHE_SCHEMA = 1

# The API marks a token it could not parse with a leading '?'. Returning that
# to the tutor as a word would be the exact failure this module exists to
# prevent, so it is detected and reported as unparsed.
_FAILED_TOKEN_RE = re.compile(r"^\s*\?")


def _cache_dir() -> Path:
    # Beside the vidyut bundle rather than in a per-app root: this is a
    # derived cache of the same upstream data, and a sandhi split does not
    # belong to a learner or to a tutor.
    d = Path(settings.SHARED_DATA_DIR) / "cache" / "sanskrit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_get(kind: str, key: str) -> dict | None:
    path = _cache_dir() / f"{kind}_{hashlib.sha256(key.encode()).hexdigest()[:16]}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"unreadable cache entry {path.name}: {e}")
        return None
    if data.get("schema") != CACHE_SCHEMA:
        return None
    return data.get("value")


def _cache_put(kind: str, key: str, value: dict) -> None:
    path = _cache_dir() / f"{kind}_{hashlib.sha256(key.encode()).hexdigest()[:16]}.json"
    tmp = str(path) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"schema": CACHE_SCHEMA, "key": key, "value": value}, f,
                      ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"could not cache {kind}: {e}")


def _vidyut_data_dir() -> Path:
    if settings.VIDYUT_DATA_DIR:
        return Path(settings.VIDYUT_DATA_DIR)
    # Shared across apps — see sanskrit_setup.data_dir().
    return Path(settings.SHARED_DATA_DIR) / "vidyut"


def _local_available() -> bool:
    return (_vidyut_data_dir() / ".vidyut_ready").is_file()


def _get_json(url: str) -> object | None:
    """GET and parse JSON, or None. Never raises — this is the optional path."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
            body = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.info(f"Samsaadhanii unreachable: {type(e).__name__}")
        return None

    # The splitter returns the bare string 'No Output Found' rather than JSON
    # when it cannot parse. Documented here because it looks like a 200.
    stripped = body.strip()
    if not stripped or not stripped[0] in "[{":
        logger.info(f"Samsaadhanii non-JSON reply: {stripped[:60]!r}")
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _segment_remote(slp: str) -> dict | None:
    """Sandhi-split via Samsaadhanii.

    Sends WX, because that is what the endpoint expects; sending IAST while
    declaring WX silently produced टपोवनम् from `tapovanam` — wrong, and wrong
    quietly, which is the worst combination.
    """
    from vidyut.lipi import Scheme, transliterate

    try:
        wx = transliterate(slp, Scheme.Slp1, Scheme.Wx)
    except Exception:
        return None

    url = (
        f"{SAMSAADHANII_BASE}/MT/prog/sandhi_splitter/sandhi_splitter.cgi?"
        + urllib.parse.urlencode({
            "word": wx, "encoding": "WX",
            # outencoding=D, not IAST: with IAST this endpoint answers with the
            # bare string 'No Output Found' instead of JSON.
            "outencoding": "D", "mode": "word", "disp_mode": "json",
        })
    )
    data = _get_json(url)
    if not isinstance(data, dict):
        return None

    segs = data.get("segmentation") or []
    if not segs:
        return None

    parts = [p for p in str(segs[0]).split("-") if p.strip()]
    if not parts or any(_FAILED_TOKEN_RE.match(p) for p in parts):
        return None

    return {
        "verified": True,
        "detail": "split by the Samsaadhanii analyser (University of Hyderabad)",
        "source": "samsaadhanii",
        "words": [{"devanagari": p.strip()} for p in parts],
    }


def _segment_local(slp: str) -> dict | None:
    """Sandhi-split with vidyut's own segmenter. Needs the data bundle."""
    if not _local_available():
        return None
    try:
        from vidyut.cheda import Chedaka

        tokens = Chedaka(str(_vidyut_data_dir() / "cheda")).run(slp)
    except Exception as e:
        logger.info(f"vidyut segmentation unavailable: {type(e).__name__}")
        return None

    words = []
    for t in tokens:
        text = getattr(t, "text", None) or str(t)
        entry = {"devanagari": to_devanagari(text), "slp1": text}
        info = getattr(t, "info", None)
        if info is not None:
            entry["morphology"] = str(info)
        words.append(entry)

    if not words:
        return None
    return {
        "verified": True,
        "detail": "split by vidyut",
        "source": "vidyut",
        "words": words,
    }


def segment(text: str) -> dict:
    """Split continuous Sanskrit into words. Tri-state, like the verifier."""
    raw = (text or "").strip()
    if not raw:
        return {"verified": None, "detail": "no text given", "words": []}
    if len(raw) > MAX_INPUT_CHARS:
        return {"verified": None, "detail": "text too long to split", "words": []}

    slp = to_slp1(raw)
    if slp is None:
        return {
            "verified": None,
            "detail": "could not read that text — is vidyut installed?",
            "words": [],
        }

    cached = _cache_get("seg", slp)
    if cached is not None:
        return cached

    for attempt in (_segment_local, _segment_remote):
        result = attempt(slp)
        if result:
            result["input"] = to_devanagari(slp)
            result["claim_id"] = claim_id("segment", slp)
            _cache_put("seg", slp, result)
            return result

    return {
        "verified": None,
        "detail": (
            "could not split this — vidyut's data bundle is not installed "
            "(run `python -m backend.sanskrit_setup`) and the Samsaadhanii "
            "analyser did not answer. Say you could not check it rather than "
            "guessing at the word boundaries."
        ),
        "input": to_devanagari(slp),
        "words": [],
    }


def _lookup_remote(slp: str) -> dict | None:
    """Morphological analysis via Samsaadhanii."""
    from vidyut.lipi import Scheme, transliterate

    try:
        wx = transliterate(slp, Scheme.Slp1, Scheme.Wx)
    except Exception:
        return None

    url = (
        f"{SAMSAADHANII_BASE}/MT/prog/morph/morph.cgi?"
        + urllib.parse.urlencode({
            "morfword": wx, "encoding": "WX",
            "outencoding": "IAST", "mode": "json",
        })
    )
    data = _get_json(url)
    if not isinstance(data, list) or not data:
        return None

    readings = []
    for item in data[:6]:          # several readings are normal; six is plenty
        if not isinstance(item, dict):
            continue
        readings.append({
            "stem": item.get("RT") or item.get("rt") or "",
            "analysis": item.get("ANS", ""),
            "kind": item.get("APP", ""),
        })
    if not readings:
        return None

    return {
        "verified": True,
        "detail": "analysed by Samsaadhanii (University of Hyderabad)",
        "source": "samsaadhanii",
        # More than one reading is information, not noise: gacchati is both a
        # finite verb and a locative participle, and that ambiguity is exactly
        # what a learner needs shown.
        "note": "several readings are possible" if len(readings) > 1 else None,
        "readings": readings,
    }


def _lookup_local(slp: str) -> dict | None:
    """Dictionary lookup in vidyut's kośa. Needs the data bundle."""
    if not _local_available():
        return None
    try:
        from vidyut.kosha import Kosha

        entries = Kosha(str(_vidyut_data_dir() / "kosha")).get_all(slp)
    except Exception as e:
        logger.info(f"vidyut kosha unavailable: {type(e).__name__}")
        return None

    readings = [{"analysis": str(e)} for e in entries[:6]]
    if not readings:
        return None
    return {
        "verified": True,
        "detail": "found in vidyut's kośa",
        "source": "vidyut",
        "readings": readings,
    }


def lookup(word: str) -> dict:
    """Analyse one word. Tri-state; absence is reported, never filled in."""
    raw = (word or "").strip()
    if not raw:
        return {"verified": None, "detail": "no word given", "readings": []}
    if len(raw) > 200:
        return {"verified": None, "detail": "that is a phrase, not a word — try analyse_sanskrit", "readings": []}

    slp = to_slp1(raw)
    if slp is None:
        return {
            "verified": None,
            "detail": "could not read that word — is vidyut installed?",
            "readings": [],
        }

    cached = _cache_get("look", slp)
    if cached is not None:
        return cached

    for attempt in (_lookup_local, _lookup_remote):
        result = attempt(slp)
        if result:
            result["word"] = to_devanagari(slp)
            result["claim_id"] = claim_id("lookup", slp)
            _cache_put("look", slp, result)
            return result

    return {
        "verified": None,
        "detail": (
            "could not find this word — vidyut's data bundle is not installed "
            "(run `python -m backend.sanskrit_setup`) and the Samsaadhanii "
            "analyser did not answer. Say the lookup failed rather than "
            "offering a plausible gloss."
        ),
        "word": to_devanagari(slp),
        "readings": [],
    }
