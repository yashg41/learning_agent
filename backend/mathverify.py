"""Check derivation steps with sympy, so wrong algebra never reaches a learner.

The tutor runs on a small model. It writes plausible algebra, which is exactly
the failure this guards: a derivation that looks right, reads right, and is
wrong in step three teaches a false mechanism more convincingly than no
derivation at all.

So the rule from 20_code_execution.md — "your sense of certainty is not evidence
about what a library actually does" — is extended from output to algebra. A step
claims lhs == rhs; sympy decides.

Parsing is deliberately narrow. Expressions arrive as LaTeX from a model, and
sympy's parser will happily evaluate arbitrary Python if handed the wrong
entry point, so this module only ever uses parse_latex/sympify with
evaluate=False semantics and refuses anything it cannot parse rather than
guessing. A step that cannot be checked is reported unverified, never assumed
true.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Longest expression we will try to parse. A model that emits a 20KB "step" has
# gone wrong in a way no verification result would usefully describe.
MAX_EXPR_CHARS = 2_000

# Constructs that are notation for a reader but not algebra sympy can compare.
# A step containing one is reported as unchecked rather than false — claiming a
# correct step is wrong is worse than admitting we could not check it.
_UNCHECKABLE = re.compile(
    r"\\(?:text|mathrm|operatorname|approx|propto|sim|ldots|cdots|dots"
    r"|underbrace|overbrace|xrightarrow|implies|iff|therefore)\b"
)


def _load_sympy():
    """Import sympy and a LaTeX parser lazily; report absence, never crash.

    Two parser backends exist and they have very different install costs:

      parse_latex_lark  needs `lark`, pure Python, small.
      parse_latex       needs the antlr4 runtime, which is NOT installed here
                        and which fails at CALL time with ImportError rather
                        than at import time.

    That last detail is why this probes with a real parse instead of trusting
    the import. The antlr path imports cleanly and then throws on every call,
    which would silently report every step "unchecked" and quietly turn
    verification into decoration — the exact failure this module exists to
    prevent.
    """
    try:
        import sympy as sp
    except ImportError as e:
        logger.warning(f"sympy unavailable, derivation steps unchecked: {e}")
        return None, None

    for name in ("parse_latex_lark", "parse_latex"):
        try:
            parser = getattr(__import__(
                "sympy.parsing.latex", fromlist=[name]), name)
            parser("x")                      # prove the backend actually runs
            return sp, parser
        except Exception:
            continue

    logger.warning(
        "No working LaTeX parser (install `lark`), derivation steps unchecked"
    )
    return None, None


def check_equal(lhs: str, rhs: str) -> dict:
    """Is lhs == rhs as algebra? Returns {verified, detail}.

    verified is True, False, or None. None means "could not check" — an
    unparseable expression, prose notation, or sympy missing — and must render
    differently from False, because "unchecked" and "checked and wrong" are
    very different things to show a learner.
    """
    if not lhs or not rhs:
        return {"verified": None, "detail": "empty expression"}
    if len(lhs) > MAX_EXPR_CHARS or len(rhs) > MAX_EXPR_CHARS:
        return {"verified": None, "detail": "expression too long to check"}
    if _UNCHECKABLE.search(lhs) or _UNCHECKABLE.search(rhs):
        return {"verified": None, "detail": "contains prose or approximation"}

    sp, parse_latex = _load_sympy()
    if sp is None:
        return {"verified": None, "detail": "sympy not installed"}

    try:
        a = parse_latex(_strip_wrappers(lhs))
        b = parse_latex(_strip_wrappers(rhs))
    except Exception as e:
        return {"verified": None, "detail": f"could not parse: {type(e).__name__}"}

    # The lark backend returns an ambiguity Tree instead of an Expr when the
    # source genuinely reads two ways — "\log(x)+\log(y)" can bind as
    # log(x + log y). That is ambiguous notation, not a wrong step, so report
    # it as uncheckable and let the prompt push the model toward braces.
    if not (isinstance(a, sp.Expr) and isinstance(b, sp.Expr)):
        return {"verified": None, "detail": "ambiguous notation — add braces"}

    return _compare(sp, a, b)


def _compare(sp, a, b) -> dict:
    """Decide whether two parsed expressions are equal.

    Ordered cheapest-first, and biased against calling a step WRONG. A false
    "wrong" is the damaging verdict here: it tells a learner a correct
    derivation is broken, which is worse than admitting we could not check.
    So every escalation below runs before False is returned, and anything that
    throws lands on None.
    """
    try:
        diff = sp.simplify(a - b)
        if diff == 0:
            return {"verified": True, "detail": "simplify(lhs - rhs) == 0"}

        # simplify() is incomplete by design — it gives up rather than run
        # forever — so a non-zero result is not yet a counterexample.
        try:
            if sp.simplify(sp.expand(sp.powsimp(diff, force=True))) == 0:
                return {"verified": True, "detail": "equal after expand/powsimp"}
        except Exception:
            pass

        # Log and power identities (log(x^n) = n log x, sqrt(ab) = sqrt a sqrt b)
        # hold only for positive reals, and sympy will not assume that of a bare
        # Symbol. Retry with every free symbol declared positive: that is the
        # domain a tutoring derivation is working in, and without this the
        # single most common log step gets reported as an error.
        try:
            pos = {s: sp.Symbol(s.name, positive=True) for s in (a.free_symbols | b.free_symbols)}
            ap, bp = a.subs(pos), b.subs(pos)
            if sp.simplify(sp.expand_log(ap - bp, force=True)) == 0:
                return {"verified": True, "detail": "equal for positive reals"}
            if sp.simplify(sp.powsimp(ap - bp, force=True)) == 0:
                return {"verified": True, "detail": "equal for positive reals"}
        except Exception:
            pass

        # Last resort: a numeric probe. Symbolic simplification failing does
        # not prove inequality, but disagreeing at random points does.
        probe = _numeric_disagrees(sp, a, b)
        if probe is False:
            return {"verified": True, "detail": "agrees numerically"}
        if probe is None:
            return {"verified": None, "detail": "could not decide"}

        return {"verified": False, "detail": f"difference does not vanish: {diff}"}
    except Exception as e:
        # A timeout, a recursion limit, an unsupported domain. Unchecked, not
        # wrong.
        return {"verified": None, "detail": f"check failed: {type(e).__name__}"}


def _numeric_disagrees(sp, a, b):
    """True if the two disagree numerically, False if they agree, None if unknown.

    Substitutes a few unremarkable positive values. Points where either side is
    undefined are skipped rather than counted as disagreement.
    """
    syms = sorted(a.free_symbols | b.free_symbols, key=lambda s: s.name)
    if len(syms) > 4:
        return None
    checked = 0
    for trial in ((sp.Rational(3, 2), sp.Rational(7, 5), sp.Rational(9, 4), sp.Rational(5, 3)),
                  (sp.Rational(2), sp.Rational(3), sp.Rational(5), sp.Rational(7)),
                  (sp.Rational(1, 3), sp.Rational(4, 7), sp.Rational(8, 5), sp.Rational(6, 11))):
        subs = dict(zip(syms, trial))
        try:
            va = complex(sp.N(a.subs(subs)))
            vb = complex(sp.N(b.subs(subs)))
        except Exception:
            continue
        if any(map(lambda v: v != v or abs(v) == float("inf"), (va, vb))):
            continue
        checked += 1
        if abs(va - vb) > 1e-9 * max(1.0, abs(va), abs(vb)):
            return True
    return False if checked else None


def _strip_wrappers(expr: str) -> str:
    """Drop delimiters the model wraps around a formula but sympy cannot read."""
    e = expr.strip()
    for pat in ("$$", "$", r"\[", r"\]", r"\(", r"\)"):
        if e.startswith(pat):
            e = e[len(pat):]
        if e.endswith(pat):
            e = e[: -len(pat)]
    e = e.strip()
    # "x &= y" alignment markers and trailing punctuation from prose context.
    e = e.replace("&", "").rstrip(",.;").strip()
    return e


def verify_steps(steps: list) -> list:
    """Check each consecutive pair in a derivation.

    Step 1 has nothing before it, so it is the premise and stays unchecked.
    Every later step claims to follow from the one above, which is exactly the
    claim `check_equal` tests.
    """
    out = []
    for i, s in enumerate(steps):
        step = dict(s)
        if i == 0:
            step["verified"] = None
            step["check"] = "premise"
        else:
            res = check_equal(steps[i - 1].get("expr", ""), s.get("expr", ""))
            step["verified"] = res["verified"]
            step["check"] = res["detail"]
        out.append(step)
    return out
