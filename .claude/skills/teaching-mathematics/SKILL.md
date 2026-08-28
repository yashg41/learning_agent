---
name: teaching-mathematics
description: >-
  How to explain mathematics so it lands: the intuition-then-derivation shape,
  what to say about each term, per-domain guidance for ML, linear algebra,
  quantum and system design, notation discipline, and checking your algebra with
  sympy before the learner reads it. Load this when you are about to explain a
  formula, derive a result, or answer a "why does this work" question in a
  mathematical track.
---

# Teaching mathematics

A formula the learner cannot derive is a formula they will misremember. Lead
with meaning, then show the machinery.

## The default shape

1. **What it does, in words.** One or two sentences, no symbols.
2. **The formulation, and why THIS form.** Give the equation, then say what each
   term contributes — and which one carries the behaviour.
3. **The derivation.** Step by step, naming the REASON for each step ("chain
   rule", "the log turns the product into a sum", "the sigmoid derivative
   cancels"). A step without its reason teaches nothing.
4. **What makes the difference significant.** The term that dominates; what
   happens as it goes to zero or infinity; what breaks if you drop it. This is
   usually the part the learner actually needed.
5. **A worked number.** One concrete substitution, run with `run_code`.

Skip steps only when the learner already has them. Never skip 3 and 4 together —
that leaves an asserted formula, which is what a search engine already gives
them.

## Rigor

- Default to intuition first, then the derivation.
- Go fully formal — assumptions, definitions, edge cases — when they say
  "prove", "derive from first principles", or challenge a step.
- Stay at intuition for "what is X", or when they are new to the track.

## Verify your algebra — you are not exempt

An unchecked derivation step is a guess with an equals sign in front of it.
Before asserting that one expression equals another — a simplification, a
cancellation, a closed form, a limit — check it with `run_code` and sympy:

```python
import sympy as sp
z = sp.symbols('z')
sig = 1/(1 + sp.exp(-z))
sp.simplify(sp.diff(sig, z) - sig*(1 - sig))   # 0, so the identity holds
```

`sp.simplify(lhs - rhs) == 0` is the general pattern.

This is CHECKING A CLAIM, not formatting prose — the "never run code to format
prose" rule does not apply. That rule forbids printing an explanation; this
tests whether your algebra is true before the learner reads it.

**Verify with sympy, present your own LaTeX.** Sympy's output is correct but
frequently unrecognisable — it renders the Gaussian in a form no textbook uses.
Write the notation the learner will meet elsewhere, and use sympy only to
confirm the two are equal. Never paste sympy's raw output as the explanation.

If a check fails, say so and fix the derivation. Never show a step you could not
verify without saying it is unverified.

## By domain

- **ML and statistics.** Gradients, losses, backprop, bias-variance,
  regularization. Show WHY a gradient comes out clean — the sigmoid derivative
  cancelling in logistic loss is the canonical case, and it is what makes the
  whole formulation make sense. Pair a loss derivation with a plot of the
  surface.
- **Linear algebra.** Lead with the geometric picture — what the transformation
  DOES to space — then the algebra. A worked 2x2 or 3x3 teaches more than an
  abstract n x n. Say what an eigenvector means before computing one.
- **Quantum.** State the space and the basis before the operator. Show the
  amplitude algebra AND the probability it produces; a learner who sees only the
  final probability has not seen the mechanism. Qiskit code comes after the
  maths, never instead of it.
- **System design.** The back-of-envelope IS the mathematics here. Show the
  arithmetic — Little's Law, queueing, latency budgets, hash distribution — and
  name the assumption behind every factor you pick.

## Notation

- Define every symbol the first time it appears.
- One symbol means one thing for the whole reply.
- Reuse the notation the learner's track already uses.
- Write it as LaTeX in `$$...$$` — see the output formatting rules.
- Brace function arguments: `\log\left(x\right)` or `\log{(x)}`, not `\log(x)`.
  Bare `\log(x)+\log(y)` is ambiguous — it also parses as `\log(x + \log y)` —
  and an ambiguous step cannot be verified.

## Detail for `request_derivation`

Put real substance in `request`: which result to derive, where the learner is
stuck, which step matters most, and whether a plot would help. The builder forks
this conversation, so it sees what was discussed — but the request is what tells
it which part to work out.

To revise one, pass `base_doc_id` so it is edited rather than rebuilt.
