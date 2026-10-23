---
name: derivation-builder
description: >-
  How to build a derivation document — the block kinds, writing in waves, and
  checking every step with sympy. This is the DERIVATION BUILDER's skill: it
  applies when you have been asked to work out a piece of mathematics and have
  derivation_write available. If you are the tutor talking to a
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
| step `reason` | `power rule on $$w_1^2$$, so the $$\frac{1}{2}$$ cancels` | `power rule on w_1^2` — raw text |
| table `headers` / `rows`, any `caption` | `$$a_1$$`, `$$y_i(w\cdot x_i+b)\ge1$$` | `a_1`, `x_11 = 2`, `(1/2)\|\|w\|\|^2` |

`$$` is the only delimiter the pane typesets. Single `$…$` is the habit every
LaTeX corpus teaches, and it reaches the learner as raw source — a page of
`$\hat{y} = \beta_0 + \mathbf{x}^T\boldsymbol{\beta}$` instead of mathematics.

**Every symbol in prose goes in `$$…$$` — reasons, captions and table cells
included, not just `text` blocks.** A reason is prose about maths, so it names
symbols constantly: `w_1`, `a_i`, `||w||^2`, `x_11 = 2`, `sum a_i y_i` all
reach the learner as raw text unless wrapped. ASCII maths (`^`, `_`, `||`,
`(1/2)`) never belongs outside `$$`. `derivation_write` and `derivation_update`
return a `fix_formatting` field listing every place they found it — when it
appears, wrap those symbols and update the block before moving on.

**Write products the textbook way: `2a_1w_1`, `\frac{1}{2}w_1^2 b`, not
`2\cdot a_1\cdot w_1`.** The checker reads juxtaposition, including a power
followed by a factor. Keep `\cdot` for where a reader needs it — between two
numbers (`2\cdot 3`) or a dot product (`w\cdot x`). Derivatives check too:
`\frac{d}{dw_1}\left(\frac{1}{2}w_1^2 - 2a_1w_1\right) = w_1 - 2a_1` gets a ✓.

Markdown works in `text` blocks (`**bold**`, lists), so write prose normally.

## The four rules

**1. Write in waves, through files.** The document already exists — its
`doc_id` is in your instructions; there is nothing to open. Write each wave as a
JSON array of blocks to its own file in your scratch directory, then hand the
server the PATH with `derivation_write(doc_id, blocks_path=...)`. It reads the
file from disk, so the blocks never travel as a tool argument — a large one can
take the tool connection down. Blocks APPEND, so each call adds to what is
already on screen and the learner watches it fill in. One giant write at the end
defeats the point of building in the background.

```
Write  <scratch>/wave1.json   [{"kind":"text","content":"..."}]               # intuition
derivation_write(doc_id, blocks_path="<scratch>/wave1.json")
Write  <scratch>/wave2.json   [{"kind":"derivation","title":"...","steps":[...]}]  # the steps
derivation_write(doc_id, blocks_path="<scratch>/wave2.json")
Write  <scratch>/wave3.json   [{"kind":"text","content":"..."}]               # what matters
derivation_write(doc_id, blocks_path="<scratch>/wave3.json")
```

A new file per wave — never rewrite an earlier one, it has already been
appended. The file is JSON, so escape backslashes: `"\\frac{a}{b}"` in the file
is `\frac{a}{b}` on screen. Inline `blocks=[...]` still works for a tiny wave.
Never write an `"id"`, `"verified"` or `"check"` into a block: the server
assigns ids and sympy sets the stamps.

**Correcting a block you already wrote.** Never append a fixed copy below the
wrong one — the learner sees both. Check the document out, patch it, publish it:

```
get_derivation_json(doc_id)                       -> <scratch>/<doc_id>.json
Grep / Read that file to find the block
Edit the block in place (remove it from the array to delete it)
derivation_update(doc_id, blocks_path="<scratch>/<doc_id>.json")
```

The file is the whole document in order, and each block keeps its `"id"`.
`derivation_update` tells you which blocks changed and need verifying again.

**2. Every step carries its reason.** `{expr, reason}` — and the reason is the
part that teaches. "chain rule", "the log turns the product into a sum", "the
sigmoid derivative cancels". A step with an empty reason shows what happened but
not why, which is the whole thing this surface exists to fix.

**3. Verify every derivation block.** Call `derivation_verify(doc_id, block_id)`
after writing one. It stamps each step `verified` true / false / unknown:

- A step written as ONE expression is checked against the step above — against
  its last side, if the step above was an equation.
- A step written as an equation, `A = B` (or `A = B = C`), is checked on its
  own: every side must equal the next. When the left side is just a name —
  `y`, `f(x)`, `L(w, \lambda)` — it is read as a definition and left unknown.
- The first step is the premise and is never checked.

What to do with the result:

- **false** means the step is provably wrong. FIX IT (see *Correcting a block*
  above) and verify again. Never leave a false step on screen.
- **unknown** is not wrong. Matrix notation, expectations and definitions are
  beyond sympy and stay unknown. Rewrite only when the `check` says
  *ambiguous notation* — then brace function arguments. Do not rewrite a
  correct step to chase a tick.

`derivation_write` tells you which block ids still need checking — the `next`
field in its result.

**4. Brace function arguments.** Write `\log{(x)}` or `\log\left(x\right)`, never
bare `\log(x)`. `\log(x)+\log(y)` is genuinely ambiguous — it also parses as
`\log(x + \log y)` — and an ambiguous step cannot be verified at all.

## Writing the mathematics

**Make every symbol decodable.** Near the top of the document, before the
first derivation, put a `table` block with headers
`["symbol", "what it is", "here"]`, one row per symbol: what it stands for, what
its indices run over, and its value in the worked example —
`["$$x_i$$", "the $$i$$-th data point, a vector in $$\mathbb{R}^2$$", "$$x_1=(2,2)$$"]`,
`["$$x_{ij}$$", "coordinate $$j$$ of point $$i$$", "$$x_{12}=2$$"]`. A symbol
introduced later gets a "where …" sentence in the `text` block before its first
use. Use the conversation's symbols exactly — if the tutor said $$a_i$$, the
document says $$a_i$$, never $$\alpha_i$$; a renamed symbol reads as a new
quantity. The `teaching-mathematics` skill's *Notation* section has the full
rules.

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
to the learner. Your entire output is tool calls. Write nothing outside your
scratch directory.

## Revising

Asked to change an existing derivation: call `derivation_read` first, then
append or correct only what the change calls for, through a wave file as above. Keep everything that was
already right — a revision is an edit, not a rebuild.
