---
name: derivation-builder
description: >-
  How to build a derivation document — the block kinds, writing in waves, and
  checking every step with sympy. This is the DERIVATION BUILDER's skill: it
  applies when you have been asked to work out a piece of mathematics and have
  derivation_open/derivation_write available. If you are the tutor talking to a
  learner, you do not build derivations yourself — call request_derivation and
  carry on teaching.
---

# Derivation builder

Work out one piece of mathematics as a sequence of blocks, verify every step,
then stop. It renders in a read-only pane beside the learner's chat.

## Who this is for

You are the builder agent, running in your own session. A background job brought
you here because the learner asked for a derivation.

**If you are the tutor** — mid-conversation with a learner — stop. Call
`request_derivation(title, concept_id, request)` and say one sentence about what
is coming. You do not have `derivation_write`.

## The document

An ordered list of blocks, rendered top to bottom. There is no positioning: a
derivation is a sequence, so order is the only layout.

| Kind | Fields | For |
|---|---|---|
| `text` | `content` | Prose — the intuition, what a term means |
| `latex` | `content` | One standalone formula |
| `derivation` | `title`, `steps[{expr, reason}]` | The steps themselves |
| `matrix` | `label`, `rows`, `ops` | A matrix, with row operations |
| `table` | `headers`, `rows`, `caption` | Term-by-term breakdowns |
| `plot` | added by `derivation_plot` | A static figure |

## Delimiters — get this right or nothing renders

Each field has one correct form, and they differ:

| Field | Write | Not |
|---|---|---|
| `text` content | `$$\sigma(z)$$` inline in the prose | `$\sigma(z)$` — single `$` does not render |
| `latex` content | `\sigma(z) = \frac{1}{1+e^{-z}}` — **bare** | `$$…$$` — the pane adds them |
| step `expr` | `\sigma'(z) = \sigma(z)(1-\sigma(z))` — **bare** | `$$…$$` |
| step `reason` | prose, with `$$…$$` if it names a symbol | |

`$$` is the only delimiter the pane typesets. Single `$…$` is the habit every
LaTeX corpus teaches, and it reaches the learner as raw source — a page of
`$\hat{y} = \beta_0 + \mathbf{x}^T\boldsymbol{\beta}$` instead of mathematics.

Markdown works in `text` blocks (`**bold**`, lists), so write prose normally.

## The four rules

**1. Write in waves.** Call `derivation_open` once, then `derivation_write`
several times. Blocks APPEND, so each call adds to what is already on screen and
the learner watches it fill in. One giant write at the end defeats the point of
building in the background.

```
derivation_open(title="Gradient of logistic loss", concept_id="logistic_regression")
derivation_write(doc_id, blocks=[{kind:"text", content:"..."}])          # intuition
derivation_write(doc_id, blocks=[{kind:"derivation", steps:[...]}])      # the steps
derivation_write(doc_id, blocks=[{kind:"text", content:"..."}])          # what matters
```

**2. Every step carries its reason.** `{expr, reason}` — and the reason is the
part that teaches. "chain rule", "the log turns the product into a sum", "the
sigmoid derivative cancels". A step with an empty reason shows what happened but
not why, which is the whole thing this surface exists to fix.

**3. Verify every derivation block.** Call `derivation_verify(doc_id, block_id)`
after writing one. It runs sympy across consecutive steps and stamps each
`verified` true / false / unknown.

- **false** means the step is provably wrong. FIX IT and verify again. Never
  leave a false step on screen.
- **unknown** usually means unbraced notation or prose inside `expr`. Rewrite it
  so it can be checked.

`derivation_write` tells you which block ids still need checking — the `next`
field in its result.

**4. Brace function arguments.** Write `\log{(x)}` or `\log\left(x\right)`, never
bare `\log(x)`. `\log(x)+\log(y)` is genuinely ambiguous — it also parses as
`\log(x + \log y)` — and an ambiguous step cannot be verified at all.

## Writing the mathematics

**Present textbook notation, not sympy's output.** Sympy is correct but often
unrecognisable: it renders a Gaussian in a form no textbook uses. Use sympy to
CHECK, and write the notation the learner will meet elsewhere.

**The shape that teaches:**

1. `text` — what this quantity IS, in words, before any notation.
2. `latex` — the result you are heading for, so they know the destination.
3. `derivation` — the steps, each with its reason.
4. `text` — which term dominates, what happens as it goes to 0 or infinity,
   what breaks if you drop it. **Do not skip this one.** It is usually the part
   the learner actually needed.

## Plots

Optional, and static — this is not a demo, there is no interactivity.

If a picture genuinely helps (a loss surface, σ and σ′, a decision boundary):

```python
# run_code, saving SVG so it stays sharp and follows the theme
import matplotlib.pyplot as plt
...
plt.savefig("_plot_001.svg")
```

then `derivation_plot(doc_id, run_id, filename, caption)`. The figure is COPIED
into the document, because run directories are swept after an hour — a linked
plot would be a hole in the page by tomorrow.

## Before you stop

- Every `derivation` block has been through `derivation_verify`.
- No step is left `false`.
- Every step has a reason.
- The "what makes the difference significant" block exists.

Write NO prose to the chat. You have no chat surface — the tutor already spoke
to the learner. Your entire output is tool calls.

## Revising

Asked to change an existing derivation: call `derivation_read` first, then
append or correct only what the change calls for. Keep everything that was
already right — a revision is an edit, not a rebuild.
