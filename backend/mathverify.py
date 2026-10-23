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
        a, a_derivs = _parse_with_derivatives(sp, parse_latex, _strip_wrappers(lhs))
        b, b_derivs = _parse_with_derivatives(sp, parse_latex, _strip_wrappers(rhs))
    except Exception as e:
        return {"verified": None, "detail": f"could not parse: {type(e).__name__}"}

    # The lark backend returns an ambiguity Tree instead of an Expr when the
    # source genuinely reads two ways — "\log(x)+\log(y)" can bind as
    # log(x + log y). That is ambiguous notation, not a wrong step, so report
    # it as uncheckable and let the prompt push the model toward braces.
    #
    # Anything else that is not an Expr — an equation, a boolean — is NOT
    # ambiguity, and saying "add braces" about it sent a builder chasing
    # braces through four rewrites of a correct derivation. Name what it was.
    for side in (a, b):
        if not isinstance(side, sp.Expr):
            if type(side).__name__ == "Tree":
                return {"verified": None, "detail": "ambiguous notation — add braces"}
            return {
                "verified": None,
                "detail": f"not a single expression (read as {type(side).__name__})",
            }

    # Derivatives were parsed as placeholder symbols so the parser could read
    # the rest of the side; swap the evaluated derivatives back in.
    a, b = a.subs(a_derivs), b.subs(b_derivs)

    result = _compare(sp, a, b)

    # The parser reads `e` as a variable, not Euler's number, so d/dz of
    # (1+e^{-z})^{-1} picks up a log(e) factor and a CORRECT step comes back
    # false — observed live on the sigmoid derivative. In a derivation `e` is
    # almost always Euler's number, but not always (an error term), so it is
    # retried rather than substituted: either reading holding means the step
    # is not wrong.
    e = sp.Symbol("e")
    if result["verified"] is not True and e in (a.free_symbols | b.free_symbols):
        as_euler = _compare(sp, a.subs(e, sp.E), b.subs(e, sp.E))
        if as_euler["verified"] is True:
            return {"verified": True,
                    "detail": f"{as_euler['detail']} (reading e as Euler's number)"}
    return result


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


# \frac{d}{dx}, \frac{d}{d w_1}, \frac{\partial}{\partial w_{1}}, and second
# derivatives written \frac{d^2}{dx^2}. The operator must be applied to a
# bracketed group that follows it; \frac{dL}{dw} — the derivative of a NAME —
# carries no expression to differentiate and is left for the parser to refuse.
_DERIV_OP = re.compile(
    r"\\frac\s*\{\s*(d|\\partial)\s*(?:\^\s*\{?\s*(\d)\s*\}?)?\s*\}"
    r"\s*\{\s*(?:d|\\partial)\s*((?:[A-Za-z]|\\[A-Za-z]+)(?:\s*_\s*(?:\{[^{}]*\}|[A-Za-z0-9]))?)"
    r"\s*(?:\^\s*\{?\s*\d\s*\}?)?\s*\}"
)


def _group_end(e: str, i: int) -> int:
    """Index just past the bracketed group starting at e[i], or -1.

    Handles \\left( … \\right) (and [ ]), plain ( … ) and { … }, counting nested
    brackets so the group ends where it really closes.
    """
    if e.startswith(r"\left", i):
        depth, j = 0, i
        while j < len(e):
            if e.startswith(r"\left", j):
                depth += 1
                j += len(r"\left")
            elif e.startswith(r"\right", j):
                depth -= 1
                j += len(r"\right") + 1      # the closing ) or ] itself
                if depth == 0:
                    return j
            else:
                j += 1
        return -1
    pairs = {"(": ")", "{": "}", "[": "]"}
    if i >= len(e) or e[i] not in pairs:
        return -1
    open_, close, depth = e[i], pairs[e[i]], 0
    for j in range(i, len(e)):
        if e[j] == open_:
            depth += 1
        elif e[j] == close:
            depth -= 1
            if depth == 0:
                return j + 1
    return -1


def _as_parens(operand: str) -> str:
    """Rewrite the operand's outer group as round brackets.

    The parser reads (x) and \\left(x\\right) but rejects a bare {x} group and
    \\left[x\\right], both of which models write after d/dx. Only the outer
    group changes; a trailing exponent is kept.
    """
    end = _group_end(operand, 0)
    if end < 0:
        return operand
    group, rest = operand[:end], operand[end:]
    if group.startswith(r"\left"):
        body = group[len(r"\left") + 1: -(len(r"\right") + 1)]
        return rf"\left({body}\right)" + rest
    return f"({group[1:-1]})" + rest


# A power followed directly by another factor: "x^2 b", "w_1^{2} a_1", "x^2(".
# The lark parser rejects exactly this juxtaposition while accepting "b x^2"
# and "x^2 \cdot b", which is why builders took to writing \cdot between every
# factor — textbook notation was costing them the check. Making the product
# explicit here lets the displayed step stay textbook. Commands that are not
# factors (\right, \cdot, relations, spacing) never get a product inserted.
_POWER_THEN_FACTOR = re.compile(
    r"(\^\s*(?:\{[^{}]*\}|[A-Za-z0-9]))\s*"
    r"(?=[A-Za-z(]|\\(?!(?:right|cdot|times|div|pm|mp|leq?|geq?|neq?|lt|gt"
    r"|quad|qquad|cdots|ldots|dots|end)(?![A-Za-z]))[A-Za-z]+)"
)


def _explicit_products(e: str) -> str:
    return _POWER_THEN_FACTOR.sub(r"\1 \\cdot ", e)


def _parse_with_derivatives(sp, parse_latex, src: str):
    """Parse src, evaluating derivative operators the LaTeX parser cannot read.

    sympy's lark parser has no derivative rule at all — even \\frac{d}{dx}(x^2)
    fails — so every differentiation step used to come back "could not parse",
    and those are exactly the steps a derivation most needs checked.

    Each \\frac{d}{dv}(group) is cut out, its group parsed (recursively, so
    nested derivatives work) and differentiated with sympy, and a placeholder
    symbol left in its place so the surrounding expression still parses.
    Returns (expr, {placeholder: derivative}); the caller substitutes.
    """
    src = _explicit_products(src)
    derivs = {}
    out, pos = [], 0
    for m in _DERIV_OP.finditer(src):
        if m.start() < pos:
            continue                         # inside a group already consumed
        start = m.end()
        while start < len(src) and src[start] == " ":
            start += 1
        end = _group_end(src, start)
        if end < 0:
            continue                         # no group: leave it to the parser
        # A power on the group belongs to the operand: \frac{d}{dz}(1+e^{-z})^{-1}
        # differentiates the reciprocal, not 1+e^{-z} — the live sigmoid step.
        k = end
        while k < len(src) and src[k] == " ":
            k += 1
        if k < len(src) and src[k] == "^":
            k += 1
            while k < len(src) and src[k] == " ":
                k += 1
            exp_end = _group_end(src, k)
            end = exp_end if exp_end > 0 else k + 1
        inner_expr, inner_derivs = _parse_with_derivatives(sp, parse_latex, _as_parens(src[start:end]))
        if not isinstance(inner_expr, sp.Expr):
            raise ValueError("derivative of a non-expression")
        inner_expr = inner_expr.subs(inner_derivs)
        var = parse_latex(m.group(3))
        order = int(m.group(2) or 1)
        name = "zqd" + "".join(chr(ord("a") + int(c)) for c in str(len(derivs)))
        derivs[sp.Symbol(name)] = sp.diff(inner_expr, var, order)
        out.append(src[pos:m.start()] + rf" \mathit{{{name}}} ")
        pos = end
    out.append(src[pos:])
    return parse_latex("".join(out)), derivs


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
    """Check a derivation, step by step.

    A step is either ONE expression, or an equation "A = B (= C …)".

      - Step 1 is the premise and stays unchecked, equation or not.
      - An expression step claims it equals the step above — more exactly, the
        LAST side of the step above, since "A = B" leaves the derivation at B.
      - An equation step is checked on its own: every adjacent pair of sides
        must be equal. It is NOT also chained to the step above.

    Equation steps used to go through check_equal whole. The parser turns
    "a + b = b + a" into True rather than an expression, so every one came
    back "ambiguous notation — add braces" — a live build spent four rewrites
    chasing braces on a correct derivation, and a PCA derivation shipped with
    26 of 33 steps unchecked, because the skill's own example writes steps as
    equations.
    """
    out = []
    prev_last = ""
    for i, s in enumerate(steps):
        step = dict(s)
        expr = s.get("expr", "") or ""
        sides, relation = split_sides(expr)

        if i == 0:
            step["verified"] = None
            step["check"] = "premise"
        elif relation:
            step["verified"] = None
            step["check"] = f"{relation} — only equalities are checked"
        elif len(sides) > 1:
            res = _check_equation(sides)
            step["verified"] = res["verified"]
            step["check"] = res["detail"]
        else:
            res = check_equal(prev_last, sides[0])
            step["verified"] = res["verified"]
            step["check"] = res["detail"]

        prev_last = sides[-1] if sides else expr
        out.append(step)
    return out


# Relations other than "=" that may sit at the top level of a step. \left and
# \neg start with the same letters, hence the lookahead.
_RELATION = re.compile(r"\\(?:leq?|geq?|neq?|lt|gt)(?![A-Za-z])")


def split_sides(expr: str) -> tuple[list[str], str]:
    """Split "A = B = C" at its top-level "=" signs.

    Returns (sides, relation). relation is "" for an equality or a plain
    expression, and names the relation when the step is an inequality, which
    is reported unchecked rather than guessed at.

    Only "=" outside every bracket counts: "f(x=1)" and "\\sum_{i=1}^n" hold
    one inside a group, and splitting there would compare nonsense.
    """
    e = _strip_wrappers(expr)
    sides, depth, start = [], 0, 0
    for i, ch in enumerate(e):
        if ch in "{([":
            depth += 1
        elif ch in "})]":
            depth = max(0, depth - 1)
        elif depth == 0 and ch in "<>":
            return [e], "inequality"
        elif depth == 0 and ch == "=":
            if e[i - 1: i] in ("!", "<", ">", "=") or e[i + 1: i + 2] == "=":
                return [e], "inequality"
            sides.append(e[start:i].strip())
            start = i + 1
    sides.append(e[start:].strip())

    top = "".join(_depth0(e))
    if _RELATION.search(top):
        return [e], "inequality"
    if any(not part for part in sides):
        return [e], ""
    return sides, ""


def _depth0(e: str):
    """The characters of e that sit outside every bracket."""
    depth = 0
    for ch in e:
        if ch in "{([":
            depth += 1
        elif ch in "})]":
            depth = max(0, depth - 1)
        elif depth == 0:
            yield ch


# A bare name: x, \Sigma, y_i, L_{w}, f(x), L(w, \lambda), \hat{y}. An equation
# whose left side is one of these is usually DEFINING it, and a definition is
# not a claim sympy can refute: "y = mx + b" is not an identity, and calling it
# wrong would tell a learner a correct definition is broken.
#
# One letter or one \command only: "mx" is m times x, and a command followed by
# brace groups is \frac{a}{b}, not a name — only the accent commands below may
# take an argument.
_BARE_NAME = re.compile(
    r"^(?:\\(?:hat|bar|tilde|vec|dot|ddot|widehat|overline|mathbf|boldsymbol"
    r"|mathcal|mathbb)\{[^{}]*\}|\\[A-Za-z]+|[A-Za-z])"          # x, \Sigma, \hat{y}
    r"(?:\s*[_^]\s*(?:\{[^{}]*\}|[A-Za-z0-9*']))*"                # y_i, L_{w}, x^*
    r"(?:\s*(?:\\left)?\([^()]*(?:\\right)?\))?$"                  # f(x), L(w, \lambda)
)


def _check_equation(sides: list[str]) -> dict:
    """Every adjacent pair of sides must be equal."""
    results = [check_equal(a, b) for a, b in zip(sides, sides[1:])]

    for n, r in enumerate(results):
        if r["verified"] is False:
            if n == 0 and _BARE_NAME.match(sides[0].strip()):
                return {"verified": None,
                        "detail": "definition — the left side names the right"}
            return {"verified": False,
                    "detail": f"side {n + 1} ≠ side {n + 2}: {r['detail']}"}
    for r in results:
        if r["verified"] is None:
            return {"verified": None, "detail": r["detail"]}
    return {"verified": True, "detail": results[-1]["detail"]}
