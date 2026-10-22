---
name: interactive-demo
description: >-
  How to construct a self-contained interactive HTML demo. This is the DEMO
  BUILDER's skill: it applies when you have been asked to produce or change a
  demo and have save_demo/update_demo available. If you are the tutor talking
  to a learner, you do not build demos yourself — call request_demo and carry
  on teaching.
---

# Interactive demo builder

Build a small, self-contained HTML page that makes one concept tangible, then
store it. It opens in a pane beside the learner's chat.

## Who this is for

You are the builder agent, running in your own session. A background job
brought you here because the learner asked to see something.

**If you are the tutor** — i.e. you are mid-conversation with a learner —
stop. Call `request_demo(title, concept_id, request)` and say one sentence
about what is coming. Writing the HTML inline would freeze the learner's
composer for the ~60s it takes, which is the whole reason builds moved to the
background. You do not have `save_demo`.

A demo is worth it when a concept has **moving parts the learner is failing to
connect**: what happens to keys when a shard is added, how a window slides, how
a rotation changes a probability. If the concept is a definition or a list of
tradeoffs, prose is better and faster.

## The four rules

**1. Self-contained.** All CSS and JS inline, in one document. No CDN scripts,
no web fonts, no remote images. This is not a style preference — the demo runs
in a sandboxed frame with no network access, so any external URL renders a
blank page. Draw with inline SVG, canvas, or styled divs. `save_demo` rejects
external URLs and tells you which one, so fix and retry if that happens.

**2. Interactive, not a picture.** The learner must be able to *do* something
and see the result change. Every demo needs at least one control — a button, a
slider, a stepper. If the concept has a parameter (shard count, window size,
angle), that parameter must be adjustable. A static diagram is not a demo; if
that is all the concept needs, use an ASCII sketch in chat instead.

**Derive ids, never write them twice.** The most common way a demo breaks is an
id written one way in the markup and another way in the lookup — `id="btn-s1"`
handled by `getElementById("btn-" + "scenario1")`. It throws on the first
click, and because the frame is sandboxed the page just looks inert. Two ways
to make it impossible:

```js
// Best: no ids at all — mark up with data attributes and delegate.
document.querySelector(".controls").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-scenario]");
  if (!btn) return;
  setScenario(btn.dataset.scenario);          // the value IS the key
});

// If you do use getElementById, build the markup from the same source.
const KEYS = ["normal", "readHeavy", "failover"];
controls.innerHTML = KEYS.map(k =>
  `<button id="btn-${k}" onclick="setScenario('${k}')">${label(k)}</button>`
).join("");
```

Whichever you choose, guard the lookup so one typo cannot kill the demo:

```js
const el = document.getElementById("btn-" + key);
if (el) el.classList.add("active");
```

**3. Show the mechanism, not just the outcome.** This is the whole reason they
asked. Expose the intermediate state: the hash value *and* the angle it maps
to *and* which shard that lands in. When something changes, show what moved and
by how much ("12 of 100 keys moved"). A demo that only shows a tidy final
answer teaches nothing that a sentence could not.

**4. Theme-aware.** The pane sends a `postMessage` on load:
`{type: "theme", theme: "dark" | "light"}`. Listen for it and set a class on
`<html>`. Default to dark. Keep colours legible in both.

## Required shape

```html
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  :root { --bg:#09090b; --fg:#fafafa; --muted:#a1a1aa; --accent:#8b5cf6;
          --panel:#18181b; --border:#27272a; }
  html.light { --bg:#fbfbfc; --fg:#09090b; --muted:#63636b; --accent:#7c3aed;
               --panel:#ffffff; --border:#d4d4d8; }
  body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
         font:14px/1.5 system-ui, sans-serif; }
  button { background:var(--accent); color:#fff; border:0; border-radius:6px;
           padding:7px 12px; font:inherit; cursor:pointer; }
  .readout { background:var(--panel); border:1px solid var(--border);
             border-radius:8px; padding:10px; font-family:ui-monospace,monospace; }
</style>
</head>
<body>
  <h2>Title</h2>
  <p class="lede">One line: what this shows and what to try.</p>

  <div class="controls"><!-- buttons / sliders --></div>
  <div class="stage"><!-- the visual: inline SVG or canvas --></div>
  <div class="readout"><!-- the mechanism, in numbers --></div>

<script>
  window.addEventListener("message", (e) => {
    if (e.data && e.data.type === "theme") {
      document.documentElement.className = e.data.theme === "light" ? "light" : "";
    }
  });
  // state -> render() -> wire controls
</script>
</body>
</html>
```

## Size

Aim for 150-400 lines. One concept per demo. If it is growing past that, the
demo is usually trying to teach too much — cut it down to the single step that
was confusing.

The exception is a request that genuinely needs many panels ("show all ten
database types side by side"), where the comparison *is* the lesson. Do not
shrink that to three panels, and do not try to write it in one go: one reply
caps at about 32000 output tokens, and going over discards the whole reply
rather than truncating it, saving nothing.

Build it in pieces instead:

```
demo_open(title, shell_html, parts=[{name, spec}, ...])
    shell_html = doctype + shared <style> + <!--{{PARTS}}--> + shared <script>
    -> the panels are written in parallel and finalized for you
```

The shell carries every shared style and helper — roughly two thirds of a
demo's bytes — so each panel only costs its own markup, and the panel count
stops mattering. Each `spec` is the whole brief for that panel: its author sees
the spec and the shell, nothing else.

To write the panels yourself instead, omit `parts`, call `demo_write_part` once
per panel in display order, then `demo_finalize` once.

**Naming across panels.** Everything is concatenated into one document, so
prefix each panel's ids and functions with its slug (`relational_render`, not
`render`). Shared helpers belong in the shell.

## Before you stop

A demo that throws on the first click looks identical to one that works until
the learner clicks — the frame is sandboxed, so nothing surfaces on the page.
Checking is part of the job.

**Call `demo_verify(demo_id)` after saving.** It reports the faults a function
can decide: an `onclick` naming a function nothing defines, a `getElementById`
for an id no element has, an external URL the sandbox will block. Fix what it
lists and call it again. Do not report the demo as finished while anything is
outstanding.

Three things `demo_verify` cannot see. Read for these yourself:

1. **Ids built by concatenation.** `getElementById('btn-' + key)` against markup
   written `id="btn-s1"` cannot be checked statically — `key` only exists at
   runtime. Derive both from one source rather than writing the id twice.
2. **Scope.** A function inside `DOMContentLoaded`, inside another function, or
   in a `<script type="module">` is invisible to an inline `onclick`, and the
   check does not model scope. Define handlers at top level.
3. **Initial state renders without a click.** Call your `render()` once at the
   end of the script so the demo is not blank on open.

You also have a scratch directory and `run_code`. For anything a script could
settle faster than reading — parsing the markup, listing every id, diffing them
against the lookups — write the check and run it.

## Saving

For a NEW demo, call `save_demo` with:

- `title` — short, e.g. "Consistent hashing ring"
- `html_path` — the file you drafted in scratch (preferred)
- `concept_id` — e.g. `consistent_hashing`, matching the concept being tracked
- `summary` — one line telling them what to try first

**Never paste a document into a tool call.** Write it to your scratch directory
and pass the path. The server reads the file off disk, so the demo never goes
through the tool channel — which is what a 20KB argument was doing when it took
the connection down mid-build and lost the work.

### Changing an existing demo

Patch it. Do not rewrite it.

```
get_demo_html(demo_id)          -> copies it to scratch, returns the PATH
get_demo_source(demo_id, query) -> the lines mentioning a name, if you need
                                   to find the spot without reading it all
Edit  old_string -> new_string   on that file, as many times as needed
update_demo(demo_id, html_path=<that path>)
```

Adding a reset button changes perhaps 40 lines. `Edit` sends those lines;
rewriting the file sends all 400. Use `Grep` and `Read` with an offset on the
draft rather than reading it whole.

**Every tool call has a size limit of roughly 8KB per argument**, and an
oversized one is refused with an explanation rather than sent. Reading a large
file whole is refused too — pass `offset` and `limit`. This is not a style
preference: a large payload has repeatedly killed the tool connection
mid-build, after which every later call fails and the work is lost. Small,
targeted calls are how a big demo gets built at all.

`Edit` needs `old_string` to match exactly once, so include a little
surrounding context when a line is not unique.

`update_demo` keeps the demo's id and its place in the learner's list, so never
use `save_demo` for an edit; that would leave a duplicate behind.

Then stop. Write no prose: you have no chat surface, and the tutor has already
spoken to the learner. The tool call is your entire output.

## Worked patterns

See [references/patterns.md](references/patterns.md) for four shapes that cover
most concepts — ring/wheel, step-through, before-and-after, and parameter
sweep — with the mechanism-exposing readout each one needs.
