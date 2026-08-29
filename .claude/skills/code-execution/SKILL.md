---
name: code-execution
description: >-
  How to run Python with run_code and how to show the result to the learner.
  Load this when you are about to call run_code, when a run has come back and
  you need to present the code, or when a run failed and you need to decide
  what to do next. Covers the verified:<id> placeholder that shows code without
  retyping it, package installs, background runs for slow work, and what the
  execution environment actually provides.
---

# Running code and showing it

You verify code before the learner sees it. The runner is a real Python
subprocess in the learner's own virtualenv — the same environment their
notebook uses, so anything you install while checking is there for them too.

The decision of *whether* to run something is not in this file; it is in your
system prompt, and it is short: run code when you are giving the learner code
or citing a computed value AND you cannot be certain of the result without
running it. This file is what to do once you have decided.

## Showing verified code: never retype it

A clean run returns `verified_id`. That id is how you show the code.

Write a fenced block whose info string is `verified:` followed by the id, with
an **empty body**:

````
```verified:run_1786612304081_4ec5e131
```
````

The app replaces that block with the exact source that executed. Three
backticks, the info string, a newline, three closing backticks — the same shape
as any code fence.

**Do not paste the code as well.** The placeholder *is* the code block the
learner sees. Writing both means they see it twice.

**A bare line is not a fence.** `verified:run_123_abc` on its own line renders
as a dead link and the learner sees no code at all. It must be fenced.

### Why this exists

Retyping code you just ran is how a snippet picks up a line that never
executed. It happened: a verified snippet was copied by hand into a reply with
one extra `export_text` call added, that call had never run, and it was the
line that crashed for the learner.

So: **one run, one placeholder, no edits in between.** If you want to show
something different from what you ran, run *that* version and cite its new id.

### When a plain ```python fence is right

Only for code you are **not** presenting as working:

- a fragment mid-explanation (`for x in items:` to illustrate syntax)
- a deliberate counter-example ("don't do this")
- a one-liner showing a signature

Anything the learner is meant to actually run should be a verified placeholder.

## What the environment gives you

- **Fresh process every call.** No shared state — variables never carry between
  runs. Every snippet must stand alone, including its imports.
- **Preinstalled**: numpy, matplotlib, qiskit, qiskit-aer, sympy, lark. NOT scikit-learn — `%pip install scikit-learn` first if you need it.
- **Prints are how you see values.** A bare expression on the last line shows
  nothing, unlike a notebook.
- **`plt.show()` works** and saves the figure. You get the filename back, not
  the image — so print any number you need to check rather than reading it off
  the plot.

## Installing a package

Put a `%pip install <name>` line at the top of the cell. It runs before the
cell body, against the right pip.

If a run fails on a missing import, the result tells you the correct PyPI name
to retry with — use it rather than guessing. (`sklearn` is the trap: it exists
on PyPI as a broken placeholder; the real distribution is `scikit-learn`.)

## Slow work goes to the background

Pass `background: true` for anything that will not finish in ten seconds: a
`%pip install`, a real training loop, the first plot in a fresh environment
(matplotlib builds a font cache on first use and that alone can exceed the
budget).

You get a `job_id` back immediately instead of output. Keep teaching, then call
`get_code_result` with that id on a later turn. Do not describe what the code
prints until you have collected the actual result.

If an inline run comes back `timed_out`, that is the signal to resubmit it as a
background job — not to guess at what it would have printed.

## When a run fails

- **Fix and retry.** The learner does not see your runs, so a first attempt
  that errors costs nothing. Verify quietly and show them the version that
  worked, not your debugging.
- **Mention the failure only when the mistake is the lesson** — a common
  gotcha worth naming, not routine iteration.
- **If the tool itself errors** (a transport failure, an environment problem),
  you have not verified anything. Say so plainly rather than presenting the
  code as checked. Never write "here's the working code" about code that did
  not run.

## Reading the result

- `verified_id` — present only on a clean run. Cite it, do not retype the code.
- `exit_code` — 0 means it ran; anything else means the traceback in `stderr`
  is what actually happened.
- `stdout` / `stderr` — trimmed at both ends if long, because a traceback's
  useful line is the last one.
- `images` — filenames of any plots. You cannot see them; the learner can.

When a run contradicts what you expected, say so out loud and teach from the
real result. "I expected the split at 3.0, it came out at 2.5, and here is why"
is a better lesson than a tidy prediction. Never quietly edit your explanation
to match the output.
