---
title: How the Sanskrit tutor works
description: Architecture, guardrails, and the curriculum graph — as built, not as planned.
date: 2026-08-31
---

# How the Sanskrit tutor works

Two tutors, one backend. **PyMentor** teaches Python and the seven technical
tracks that build on it; **SanskritMentor** teaches Sanskrit. They share the
agent loop, memory, sessions, the knowledge graph and the quiz machinery. They
share nothing about *what* they teach — prompts, curriculum, tools and UI are
separate.

That split is the whole architecture. Everything below is a consequence of it.

```
                    browser
        /                              /sanskrit
        │                                   │
        │  app: "pymentor"                  │  app: "sanskrit"
        └───────────────┬───────────────────┘
                        ▼
              FastAPI  ·  SSE stream
                        │
                        ▼
              run_agent(app=…)
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   prompt files    tool families    curriculum
   _common + app   core + app       app's tracks
        └───────────────┬───────────────┘
                        ▼
              Claude Agent SDK  →  claude-sonnet-5
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   working memory   episodic        semantic
   (SDK context)    (ChromaDB)      (JSON graph)
```

---

## 1. Size and shape

| Layer | Lines | What it is |
|---|---:|---|
| `backend/` | ~11,600 | shared by both tutors |
| `frontend/sanskrit/` | ~11,500 | Sanskrit's own UI |
| `frontend/` (PyMentor) | ~16,500 | unchanged |
| tests | 227 passing | the only safety net — there is no CI |

Dependencies are deliberately few:

```
claude-agent-sdk==0.1.34   the agent loop (pinned)
chromadb                   episodic memory (vector search)
fastapi + uvicorn          HTTP and SSE
pyyaml                     reads the curriculum
sympy + lark               PyMentor's derivation checker
PyMuPDF                    PDF import (lazy — app runs without it)
```

No frontend build step. No bundler, no `package.json`, no framework. The UI is
vanilla JS loaded as plain `<script>` tags, which is why a change is visible on
reload.

---

## 2. The agent loop

`backend/agent.py` wraps the **Claude Agent SDK**, which spawns the bundled
Claude Code CLI as a subprocess. The CLI owns session transcripts and `--resume`;
we own everything the learner sees.

One `async for message in query(...)` loop with five branches:

| SDK message | What we do |
|---|---|
| `init` | capture the session id, populate `session_ctx` so tools know which chat called them |
| `StreamEvent` | token deltas → `assistant_delta` events (the text appearing as it types) |
| `AssistantMessage` | settled text, and tool-call cards |
| `UserMessage` | unwrap `ToolResultBlock` → tool results |
| `ResultMessage` | turn finished |

Model is **`claude-sonnet-5`** for both tutors ([`config.py`](../backend/config.py)).
The tutor's job is explanation and diagnosis — noticing what a learner actually
got wrong and why — which is where a stronger model earns its cost.

### The `app` parameter is load-bearing

`app` travels from the browser (`{message, email, app: "sanskrit"}`) through
`ChatRequest` → `run_agent` → the prompt builder, the tool registry and the
knowledge store. One value decides which tutor you are talking to.

> **This broke once, and instructively.** `run_agent` accepted `app` but forgot to
> forward it to `run_agent_internal`, so the tools were *always* built as
> PyMentor. The Sanskrit tutor called `get_domain_map` on its own curriculum, was
> told `Unknown track 'sanskrit'`, and then **apologised to the learner for a
> limitation that did not exist**. Nothing crashed; it was only visible by
> reading a tool result. Two tests now assert the forwarding, because a silent
> wrong-answer bug is worse than a loud one.

---

## 3. The knowledge graph

### How it is created

The curriculum is **data, not code**: one YAML file per track under
`backend/curriculum/`. It used to be ~285 lines of Python dict literals inside
`knowledge.py`, which meant subject matter and graph algorithms could only be
edited in the same file.

```yaml
# backend/curriculum/sanskrit.yaml
track: sanskrit
name: Sanskrit
hue: 95

categories:
- id: san_nonfinite
  label: Sanskrit · Participles & Absolutives
  priority: 5
  concepts:
  - id: ppp
    name: Past passive participle (kta) — the workhorse of narrative
    prereqs: [dhatu, a_stems]
```

At import, `backend/curriculum/__init__.py` reads every file named in
`TRACK_ORDER` and derives six lookup tables in a single pass, so they cannot
disagree with each other. **282 concepts total; 67 of them Sanskrit.**

Categories carry their concepts, rather than every concept naming its category
and track — that removes two identical keys from all 282 nodes, which is exactly
the repetition a typo hides in.

### Integrity checks, all fatal at import

The app refuses to start rather than teach around a hole:

- **Dangling prereq** — a concept whose prerequisite doesn't exist is permanently
  unreachable, because the prerequisite walk can never mark it met.
- **Prereq cycle** — every concept in the cycle is permanently blocked.
- **Self-loop** — same failure, different shape.
- **Category spanning two tracks** — its colour would be undefined.
- **Track belonging to no app** — it loads, but no tutor ever teaches it.
- **Track belonging to two apps** — the identity line would name it twice.

### The Sanskrit graph

Twelve categories, in teaching order:

| # | Category | Concepts |
|---:|---|---:|
| 0 | `san_script` — varṇamālā, conjuncts, IAST | 6 |
| 1 | `san_phonology` — guṇa/vṛddhi, syllable weight | 2 |
| 2 | `san_sandhi` | 4 |
| 3 | `san_nominal` — cases and stems | 8 |
| 4 | `san_verbal` | 10 |
| 5 | `san_nonfinite` — participles, absolutives | 6 |
| 6 | `san_compound` — the six samāsa types | 7 |
| 7 | `san_syntax` — kāraka, anvaya | 5 |
| 8 | `san_lexicon` — roots, upasarga, kṛt/taddhita | 5 |
| 9 | `san_metre` — scansion, anuṣṭubh | 4 |
| 10 | `san_reading` — the graded ladder | 6 |
| 11 | `san_spoken` — pronunciation, recitation, chanting | 4 |

**Two orderings deliberately contradict the obvious arrangement.** The YAML says
so at the top so a later edit does not "fix" them back:

1. **Sandhi is split in two.** *Generating* sandhi (joining words) is easy and
   early. *Resolving* it while reading — finding word boundaries that are not
   written — is the single most common place learners quit, so it sits late and
   continuous, practised against real text.
2. **Participles come before serious reading.** Classical prose leans on
   participles and absolutives more than finite verbs. Gating reading behind the
   full verb system means the learner never starts.

The result is a shallow graph — reading is reachable at **depth 4**:

```
varnamala             0
ppp                   3     ← the workhorse form
reading_graded        4     ← first continuous Sanskrit
sandhi_resolve        4
reading_hitopadesha   5
anvaya                5
chanting              5
```

The perfect (liṭ) and aorist (luṅ) are marked **recognition, not production** —
the perfect is the default narrative tense of the epics but follows one of seven
unpredictable stem patterns, and the aorist is rare in classical Sanskrit. Both
are read, never written.

### How the graph is used at runtime

`KnowledgeStore` (`backend/knowledge.py`) is constructed with an `app` and bounds
every walk to that tutor's tracks.

- **`suggest_next_topics`** — concepts whose prerequisites are all met, bucketed
  by track and interleaved round-robin.
- **`get_domain_map`** — the whole shape of one track, each concept marked
  `available` / `blocked` / its mastery level. It distinguishes *"this domain is
  empty"* from *"this domain is gated"*, and computes `gateway_concepts`
  **transitively**, ranked by how many concepts each unblocks. For a fully-gated
  track the useful answer is never inside the track itself.
- **Mastery** is `introduced → practiced → mastered`, written by `update_concept`.

### Struggle areas

A concept lands in `profile.struggle_areas` when the learner gets it wrong in a
quiz, and appears in the system prompt every turn thereafter.

It is removed in **exactly one way**: the learner asks to be re-tested and passes
every question on it. Not by the tutor deciding they seem fine now; not via
`update_learner_profile`.

> It used to be a one-way ratchet, which produced prompts saying "struggles with
> X" while X sat at `mastered`. The fix is evidence-backed and learner-initiated,
> and it is tested.

---

## 4. Prompts

Assembled from numbered markdown files, merged from two directories:

```
prompt_files/
├── _common/     25_web_lookup · 40_memory · 50_quiz · 70_feedback
├── pymentor/    00_identity · 10_teaching_style · 15_mathematics
│                20_code_execution · 30_output_formatting · 60_demos
│                80_learner_state
└── sanskrit/    00_identity · 10_teaching_style · 30_output_formatting
                 60_demos · 80_learner_state
```

Both directories merge into **one numeric sequence**, so common and app-specific
sections interleave by prefix rather than one block following the other:

```
sanskrit  00_identity.md
sanskrit  10_teaching_style.md
_common   25_web_lookup.md      ← shared verbatim
sanskrit  30_output_formatting.md
_common   40_memory.md          ← shared verbatim
_common   50_quiz.md            ← shared verbatim
sanskrit  60_demos.md
_common   70_feedback.md        ← shared verbatim
sanskrit  80_learner_state.md
```

An app file **shadows** a `_common` file of the same name — that is the override
hatch. Fix the memory protocol once in `_common/` and both tutors get it.

**What is *not* shared, and why it matters.** Teaching style, output formatting
and learner-state rules look generic but are not: PyMentor's mention Qiskit
snippets, LaTeX and mermaid, and hardcode the Python track ids. Sharing them
would push Python assumptions into every future tutor. A test asserts they stay
out of `_common/`.

Four `$`-tokens are substituted: `$knowledge_state`, `$recent_episodes`,
`$session_demos`, `$domains`. Substitution is a plain `str.replace` —
deliberately not `.format()` (chokes on the braces in JSON examples) and not
`string.Template` (eats `$$`, which is the math delimiter, silently rewriting
every equation into the broken form the rules warn against).

### The Sanskrit teaching prompt, in essence

- **Never open by asking what syllabus they studied.** Which board someone sat is
  a proxy for what they can do; put a sentence in front of them and watch where
  it breaks.
- **They are not a beginner.** Devanāgarī is readable if rusty; the metalanguage
  (*sandhi, samāsa, kāraka, lakāra*) survives long after the content. Say "you
  already know what a bahuvrīhi is" and remind them.
- **Reach for Hindi cognates constantly** — a Sanskrit word that survives in
  Hindi is a word they already half-know.
- **Teach sandhi as resolution, not production**, and require reading aloud: the
  rules only make sense in sound.
- **Pāṇini is the explanation, not the syllabus.** Show the sūtra when asked
  *why*; never make the metalanguage a prerequisite.

---

## 5. Tools and guardrails

Tools are grouped into **families**, and each app declares which it wants:

| App | Families | Registered | Tutor allowlist |
|---|---|---:|---:|
| `pymentor` | core + code + demos + derivations | 27 | 21 |
| `sanskrit` | core + demos | 19 | 18 |

`core` — memory, knowledge graph, quiz, feedback — is implicit for every app: a
tutor that cannot record what it taught is not a tutor.

**Sanskrit deliberately has no `run_code`.** A Sanskrit tutor handed a Python
sandbox will "verify" a declension by writing a script instead of consulting
something that actually knows — the exact failure the correctness tools exist to
prevent.

### Guardrail 1 — deny-list, not allow-list

`permission_mode="bypassPermissions"` means `allowed_tools` is **advisory**. The
enforced boundary is `disallowed_tools` (11 entries): `Bash`, `Write`, `Edit`,
`Read`, `Glob`, `Grep`, `Task`, `TodoWrite` and friends.

> Without this the tutor shells out. It was once seen writing a demo to `/tmp`
> and leaving `python3 -m http.server 8000` running, exposing that directory on
> the network. A tutoring agent has no business running commands.

`WebSearch` / `WebFetch` are deliberately *not* denied — the tutor teaches things
that move faster than the model's cutoff.

### Guardrail 2 — tutor requests, builder writes

The tutor can call `request_demo`. It **cannot** call `save_demo`. Those belong
to a separate builder agent.

The real enforcement is not the allowlist but the **capability context**: a
builder runs with `session_ctx=None`, which makes `request_demo` refuse outright,
so a build cannot recursively request another build.

The builder gets its context by **forking the tutor's session** — inheriting the
actual conversation rather than a summary, because summarising twenty turns
produces a generic artifact. Refinement resumes the *builder's own* session,
which still holds what it wrote, so "add the dual forms" is an edit rather than a
rebuild.

### Guardrail 3 — transcript budget

Every tool result is persisted and **re-sent on every later turn**, so a verbose
one is a permanent cost, not a one-off:

```
TOOL_RESULT_MAX_CHARS   2000
MAX_DEMO_SOURCE_CHARS   4000   (and query-required — never the whole document)
MAX_RUN_OUTPUT_CHARS    3000   (PyMentor only)
```

### Guardrail 4 — app isolation

`KnowledgeStore(user_data_dir, app=...)` bounds every curriculum walk. The two
tutors share one `knowledge.json` — one learner, one progress file — but what
gets *offered* is scoped. `taught_domains(app)` builds the identity line from the
app's tracks only.

> Also learned the hard way: `taught_domains()` originally walked every loaded
> track, so adding Sanskrit made **PyMentor introduce itself as teaching
> "…system design, and Sanskrit."** Deriving identity from the curriculum is the
> right idea; deriving it from *every* curriculum is the bug.

### Guardrail 5 — deterministic verification (the design thesis)

The best idea in the codebase is in `mathverify.py`: the tutor writes
plausible-looking work, so a **deterministic checker gates it before the learner
sees it**. Two hard rules — refuse what you cannot parse, and report unparseable
as *unverified, never true*.

PyMentor has this for algebra (sympy). **Sanskrit does not have it yet.** See
§8.

---

## 6. Memory

Three tiers:

| Tier | Store | Holds |
|---|---|---|
| Working | SDK context window | the current conversation; auto-compacts |
| Episodic | **ChromaDB** (`all-MiniLM-L6-v2`, cosine) | searchable summaries + every exchange |
| Semantic | JSON | the knowledge graph, mastery, quiz history |

On disk:

```
data/users/<email>/
  knowledge.json        concepts, mastery, struggle_areas
  quiz_history.json
  sessions/<id>/        conversation.jsonl, sdk_transcript.jsonl
  demos/  notes/
```

The Sanskrit tutor uses a **separate identity** (`you@sanskrit`), so the two
tutors' progress and memory are naturally distinct.

Session state is split by an OS constraint worth knowing: the SDK CLI keeps its
transcripts in `~/.claude` and **cannot be relocated** (moving it breaks Keychain
auth). The UI renders from `data/`, while `--resume` reads the CLI's copy;
`transcripts.py` mirrors between them. Delete one and the chat looks intact while
the model has lost every prior turn.

---

## 7. The UI

A copy of PyMentor's, so chat behaves identically — streaming, sessions, forking,
side-chats, notes, demos, knowledge dashboard. Removed: the code runner and
derivation pane (~700 lines), because neither applies.

Served under paths that name the tutor: `/sanskrit`, `/sanskrit/graph`,
`/sanskrit/memory`. The graph is a **shared renderer**, not a fork — it reads its
app from the path, and gets its track list from the API rather than a hardcoded
copy (that copy had already drifted; Sanskrit rendered unlaned and colourless
because the page had never heard of the track).

### Devanāgarī rendering

Three CSS rules are load-bearing, not stylistic:

| Rule | Why |
|---|---|
| `letter-spacing: normal !important` | any non-zero tracking **splits conjuncts** — क्ष renders as क् ष — and the shared heading styles do set tracking |
| `line-height: 1.85` | matras stack above and below the headline; at the Latin 1.5 they collide |
| `font-synthesis: none` | a faked bold smears conjunct shapes |

Font is **Noto Serif Devanagari**, not Tiro — Tiro looks like the scholarly
choice but its conjunct-substitution rules are effectively empty in the Google
Fonts build.

Theme is **amber**, where PyMentor is violet. One detail that is not cosmetic:
`--accent-contrast` is **ink** on the dark theme. White on that amber measures
2.13:1 and is unreadable; ink is 8.61:1. Light mode drops to a bronze that carries
white at 4.61:1. All pass WCAG AA — measured, not eyeballed.

### Audio

`app.js` already wraps every Devanāgarī run in `<span lang="sa">`. Audio hangs off
that with **one delegated click listener on `document`** — click to hear,
shift-click or long-press for slow.

Why delegation rather than a button per phrase: `enrichMessage` runs from five
places and every one does `innerHTML = …` first, destroying child nodes; the
tagger sets `span.textContent`, wiping any child; and a button inside the span
would inherit the Devanāgarī font and the forced `letter-spacing`. Delegation
costs one listener and survives every re-render. The affordance is a
hover-revealed `::after` glyph, so nothing enters the DOM at all.

**The voice is Hindi**, because no browser ships a Sanskrit one. Measured on this
machine rather than assumed: the macOS `hi-IN` voice keeps the visarga
(रामः 0.51s vs राम 0.37s) and full syllable counts (गच्छति 0.61s) — better than
the research predicted. It is still a Hindi voice, so a dismissible note says so
after the first playback. Letting the learner assume it is authoritative would be
the one genuinely harmful thing this feature could do.

---

## 8. The correctness layer

This is what makes the tutor trustworthy rather than merely fluent, and it is the
newest part of the system.

**The problem it solves.** Every form the tutor states was, until recently, the
model recalling — so it could hand you a declension table with total confidence
and be wrong. That is worse here than in most subjects: you can sanity-check an
equation by substituting numbers, but you cannot check रामैः except by already
knowing it, which is the thing you came to learn.

Six tools now generate Sanskrit by Pāṇinian rule, via **`vidyut`** (MIT, Ambuda's
Rust toolkit):

| Tool | Backed by | Needs the data bundle? |
|---|---|---|
| `derive_form` | `vidyut.prakriya` — the form **with its sūtra chain** | no |
| `decline` | `vidyut.prakriya` — full 8×3 table | no |
| `identify_metre` | `vidyut.chandas` — syllables + laghu/guru | no |
| `transliterate_sanskrit` | `vidyut.lipi` | no |
| `analyse_sanskrit` | `vidyut.cheda`, falling back to Samsaadhanii | either |
| `lookup_word` | `vidyut.kosha`, falling back to Samsaadhanii | either |

### The tri-state, and why it is the whole design

Every tool returns `verified` as **`true`**, **`false`**, or **`null`**, and the
third is not a rounding error:

- `true` — the grammar generated this
- `false` — the grammar generated something else, named in `detail`
- `null` — **could not check**, which is *not* agreement

Collapsing `null` into `true` would put a verified marker on a guess, and
collapsing it into `false` would tell a learner their correct form is wrong. Both
are worse than saying nothing. `sanskritverify.py` never raises; every path
returns one of the three.

### The trap that shaped the design

**vidyut does not validate roots.** Given the nonsense root `zzzz` it returns
`zzati` — a well-formed-looking verb built from nothing. So a typo would have
produced a fabricated form wearing our verified marker, which is worse than no
checking at all, because the marker is a promise.

Hence `KNOWN_ROOTS`: a curated whitelist, each entry derived and compared against
its textbook form before being added, and re-checked by the test suite. Roots
whose stem substitution lives in the Dhātupāṭha (√gam → gacch, √sthā → tiṣṭh) are
deliberately **absent**, because without the data bundle vidyut silently returns
`gamati` — plausible, and wrong. Those are refused with the reason and the fix.

### Two tiers

Four tools work with **no download at all**. Two need vidyut's ~32 MB data bundle:

```
python -m backend.sanskrit_setup          # download + verify
python -m backend.sanskrit_setup --check  # verify only
```

Until then, `analyse_sanskrit` falls back to the **Samsaadhanii** API (University
of Hyderabad) — which works: तपोवनम् correctly splits to तपः + वनम् today. It sits
behind a permanent cache and an 8-second timeout, because a university CGI being
slow must degrade the lesson, never stall it. If both routes fail, the tool says
so and the tutor is instructed to pass that on rather than guess.

### Visible verification

A checked form is cited as `[sv_…]`, which the frontend replaces with a small ✓
and a tooltip. **Positive evidence only** — unmarked text is not an accusation,
since prose and quoted verse are legitimately unmarked. A mark means "this came
from Pāṇini"; its absence means nothing in particular. Flagging absence would
fire constantly and quickly stop being read.

### What it looks like in practice

Asked to conjugate √gam — the first root anyone reaches for, and one that needs
the bundle — the tutor replied:

> *"I just tried to verify it and couldn't… rather than hand you a form wearing a
> false checkmark, let me tell you what I can and can't say."*

That is the design working. A tutor that says "I could not check this" is more
useful than one that is confidently wrong.

### Known caveat

vidyut gives nadī nominative singular as **नदीः** where textbooks print **नदी** —
it is returning the form before a rule that the isolated-word context does not
trigger. This is **flagged in the result**, not silently patched: quietly
"correcting" a grammar engine means shipping a second, unverified grammar of your
own.

---

## 8b. What is still not built

- **The reading environment** — the graded ladder (Amarahāsa → Hitopadeśa) and the
  click-a-word interlinear reader.
- **The placement diagnostic** — see §3 on where a CBSE-schooled adult actually
  stands.
- **A real dictionary.** `lookup_word` gives morphology, not glosses. Cologne's
  Monier-Williams needs parsing into local SQLite; `pycdsl` is broken and cannot
  be used.
- **Pronunciation scoring** — deferred deliberately; see the plan.

---

## 9. Reading the code

Start here, in this order:

| File | Why |
|---|---|
| `backend/curriculum/sanskrit.yaml` | the curriculum, and the reasoning for its orderings |
| `backend/prompt_files/sanskrit/10_teaching_style.md` | how the tutor is told to teach |
| `backend/agent.py` | the loop, the tool lists, the deny-list |
| `backend/knowledge.py` | mastery, prerequisite walks, `get_domain_map` |
| `backend/mathverify.py` | the verification thesis, in its docstring |
| `frontend/sanskrit/audio.js` | the delegation argument, written out |

**Trust the code over `README.md`**, which is stale. The *inline comments* are the
opposite — most non-obvious decisions carry the failure that prompted them. Read
those before changing anything; several encode constraints that cost real
debugging (the `CLAUDE_CONFIG_DIR`/Keychain issue, the `$$`-only math delimiter,
the conjunct-splitting `letter-spacing`).
