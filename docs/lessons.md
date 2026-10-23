---
title: Lessons from the roadblocks
description: The edge cases, false premises and misleading errors this project actually hit — what looked like the cause, what the cause was, and the rule we kept.
date: 2026-09-03
---

# Lessons from the roadblocks

Every entry below is something that actually broke, or an approach that was
actually rejected. They are written cause-first rather than fix-first, because
in almost every case the fix was easy once the cause was named and the real
cost was the hours spent on the *plausible* cause instead.

The shape each one takes:

> **Symptom** — what was observed.
> **The tempting explanation** — what it looked like, and why that reading was
> reasonable.
> **Actual cause** — what it was.
> **Fix** — what we did.
> **Rule** — the part worth carrying to the next project.

A note on how to read this. The tempting explanation is not a strawman. In
nearly every case it was the *first* diagnosis, it was consistent with the
evidence available at the time, and acting on it would have produced a change
that looked like a fix. That is precisely what made these expensive.

---

## Contents

- [1. "Stream closed": one message, five causes, a recovery, and the number that settled it](#1-stream-closed-one-message-five-causes-a-recovery-and-the-number-that-settled-it)
- [2. The plausible-output family](#2-the-plausible-output-family)
- [3. Limits you only meet by losing work](#3-limits-you-only-meet-by-losing-work)
- [4. Feedback loops and runaway state](#4-feedback-loops-and-runaway-state)
- [5. Identity, isolation and the shared-store leak](#5-identity-isolation-and-the-shared-store-leak)
- [6. Concurrency on one JSON file](#6-concurrency-on-one-json-file)
- [7. Readiness, restarts and interrupted runs](#7-readiness-restarts-and-interrupted-runs)
- [8. Errors that never reach the error channel](#8-errors-that-never-reach-the-error-channel)
- [9. Prompts, tools, and where a rule belongs](#9-prompts-tools-and-where-a-rule-belongs)
- [10. Boundaries that were not boundaries](#10-boundaries-that-were-not-boundaries)
- [11. Someone else's state, in someone else's format](#11-someone-elses-state-in-someone-elses-format)
- [12. Untrusted content crossing a boundary](#12-untrusted-content-crossing-a-boundary)
- [13. Corrections that changed the design](#13-corrections-that-changed-the-design)
- [14. Small edge cases worth keeping](#14-small-edge-cases-worth-keeping)
- [15. The recurring shapes](#15-the-recurring-shapes)

---

## 1. "Stream closed": one message, five causes, a recovery, and the number that settled it

This is the single most expensive string in the project's history. It is the
error the Claude Agent SDK surfaces when a tool call fails on the CLI's stdio
transport, and it says nothing whatsoever about why. Four separate
investigations chased it, and each found a different cause in a different
layer — before a fifth finally read the CLI's own stack trace and found what
the string actually means (§1e).

Worse: the SDK substitutes the literal text `"Check stderr output for
details"` as the error message — so the one place that would have explained it
says nothing, and stderr was not being captured at all
([`backend/agent.py:106`](../backend/agent.py)).

### 1a. The auto-reloader was killing the server

**Symptom.** Tool calls died with `Stream closed` mid-turn, most often ~57s
in — during a demo build, the only tool that runs that long.

**The tempting explanation.** The SDK's MCP channel is flaky under load. Add
retries. This is very believable: it correlates with long-running calls, which
is exactly what a timeout or a flaky socket would do.

**Actual cause.** The code runner writes `cell.py` and `sitecustomize.py` into
`data/users/<email>/scratch/` on *every* Run Code. uvicorn's reloader watches
the whole working directory and matches `*.py`. So **every code execution
restarted the server**, killing the `claude` CLI subprocess that carries the
MCP channel, and any in-flight tool call came back as `Stream closed`.

The correlation with long calls was real but misleading — long calls simply had
a wider window in which to catch a restart.

**The trap inside the trap.** `reload_excludes` does *not* fix this.
uvicorn's `WatchFilesReload` calls `watch(*reload_dirs)`, so paths outside
`reload_dirs` never reach the exclusion filter to be excluded. The filter
would have returned the right answer; it was never asked.

**Fix.** Three layers, deliberately redundant
([`run.py:47`](../run.py), [`backend/sandbox.py:114`](../backend/sandbox.py)):
`RELOAD` now defaults to `0`, because the server should run the code it started
with while a session is live; when reload *is* on, `reload_dirs` is pinned to
`backend/`; and scratch moved to `SCRATCH_ROOT` outside the project tree
entirely, so no future widening of the watch can reintroduce it.

**Rule.** An application's write path must never intersect a file watcher's
read path. And when you exclude paths from a watcher, confirm the exclusion
mechanism actually *sees* those paths — a filter that is never consulted looks
identical to a filter that passes.

### 1b. Oversized payloads killed the MCP channel

**Symptom.** A build finished its work and could not save it. The call
returned `Stream closed` — **and so did every later MCP call, including ones
with two-byte arguments, while the CLI's own built-in tools kept working.**
Four demos had to be rescued by hand.

**The tempting explanation.** The last call had a bug. It did not; the channel
had died several calls earlier and everything after it was collateral. This is
the general hazard of transport-level failures: they are attributed to whatever
call happens to be in flight when they surface.

**Actual cause.** A payload past roughly 14KB on the stdio transport kills the
channel. Editing one demo sent the whole document across it **four times** —
`get_demo_html` returned it, `Write` sent it, `Read` returned it, `update_demo`
sent it — for a change that touched 41 lines
([`backend/tests/test_demo_paths.py`](../backend/tests/test_demo_paths.py)).

**The diagnostic tell**, and the most useful line in this document: *later MCP
calls with tiny arguments also fail, while the CLI's own built-in tools still
work.* That asymmetry distinguishes a dead MCP channel from a dead process.
Nothing else does.

**Fix, and the half that got missed.** Making the demo tools path-based fixed
the MCP half — the server copies the document into the builder's scratch dir
and hands back a *path*, which the builder patches with `Edit`. Then the large
payloads simply **moved**: a 26KB `Read` result and a 14KB `Edit` argument, on
tools we do not define and cannot change.

The obvious next move was a `PreToolUse` hook: the only place a size rule can
be enforced once, because it sees every tool, built-in and MCP alike. **It was
built, and it had to be removed** — see §1e, which is the reason.

What survives is an in-process check called by the tool handlers themselves
(`check_payload_size`, [`backend/agent.py`](../backend/agent.py)). Its details
each encode their own lesson:

- **Deny, never silently rewrite.** The model is told which field was too big
  and what to do instead, so a wrong guess stays visible rather than being
  quietly altered underneath it.
- **Path fields are exempt** (`_PATH_FIELDS`) — the whole point of the rule is
  to move content *by* path, so without this the guard would block its own
  remedy.
- **Nested payloads are measured serialised.** `blocks=[...]`, `questions=[...]`
  carry content one level down; a naive `isinstance(value, str)` check misses
  them entirely.

The threshold (`MAX_TOOL_PAYLOAD_BYTES = 8_000`) was chosen conservatively and
the reasoning is recorded rather than asserted: a 14,264-byte Edit preceded a
failure, a run whose largest payload was 1,050 bytes was clean, and six of the
seven Edit calls in the failing build were already under 2KB. A `PostToolUse`
observer logs sizes so it can be tuned on evidence — *"rather than to discover
them by losing another build."* That observer is what caught the 43,868-byte
`Edit` result that prompted the next round.

**The unenforceable half, stated plainly.** The in-process check reaches only
*our* tools. The CLI's own `Read` and `Edit` move as much content as anything
we define, and no in-process check can see them. That half rests on the skill
and the builder prompt — guidance, not enforcement. Writing this down matters
more than papering over it: a reader who believes the rule is enforced
everywhere will misdiagnose the next occurrence.

**Rule.** File contents cross a process boundary as paths, never as arguments.
And when you cap the tools you own, the pressure relocates to the tools you
don't — so know which half you actually control.

### 1c. A lost-update race, wearing the same error

**Symptom.** A batch of six `update_concept` calls: two returned `Stream
closed`, four returned `Sibling tool call errored`. The whole lesson vanished
from the knowledge tracker while the reply above it looked completely fine.

**The tempting explanation.** The transport again. **Nothing had restarted.**

**Actual cause.** See [§6](#6-concurrency-on-one-json-file) — an unlocked
read-modify-write, plus a `ValueError` raised *after* mutation and before save,
which took down every sibling call in the same parallel batch.

**Rule.** A tool-transport error message is not evidence about the transport.

### 1d. The MCP server was built but never reached

**Symptom.** A builder's MCP calls failed with `Stream closed` while its
`Write`/`Read` kept working.

**Actual cause.** The CLI is handed only `{"type": "sdk", "name": ...}` — the
server instance is stripped — and must call back over the control protocol to
reach it. When those callbacks fail, the result says only `Stream closed`,
which **cannot distinguish "the server was never built" from "it was built but
never reached"** ([`backend/tools.py:3107`](../backend/tools.py)).

**Fix.** Log the server's instance id at build time and again at call time.
Comparing them tells the two apart. Also log what each run was launched with
(`mcp_servers`, tool count, resume/fork, event-loop id, task name) — because
after the fact the transcript records the failed call and nothing about the
conditions.

**Rule.** When one error string has several causes, write down the
*distinguishing observation* for each, not just the fix. Then add the logging
that makes the distinction visible next time — the error message is the least
informative part of the incident.

### 1e. What the string actually means — and why the fix ate itself

Four investigations reasoned *about* this error. The fifth read it. The CLI
printed its own stack trace to stderr, and one frame settles what the previous
sections could only infer:

```js
createCanUseTool → await this.sendRequest({subtype:"can_use_tool", …})
async sendRequest(…) { if (this.inputClosed) throw Error("Stream closed") }
```

**`Stream closed` is the CLI refusing to send a control request, because the
pipe back to our process is shut.** Not a timeout, not a crash, not a bug in
the call that reported it. That single fact explains every earlier observation
at once: built-in tools survive because they execute inside the CLI, while
anything needing a round trip to us — MCP tools, permission checks — fails
regardless of size. It is why a two-byte `get_learning_progress` failed
alongside a 24KB `update_demo`.

**Then it invalidated the fix from §1b.** A `PreToolUse` hook is *itself* a
control request on that same pipe. So the guard:

- **could not run when it was needed** — the log showed `Error in hook callback
  hook_0` interleaved with the very failures it existed to prevent; and
- **cost a round trip on every tool call when it was not.**

A guard that depends on the thing it guards is the wrong shape. It was removed
the same day it was written, and the check moved into the tool handlers, where
it runs in our process on arguments that have already arrived and cannot itself
fail. `PostToolUse` stayed, because it only observes.

**The cost of not reading it sooner.** Between §1b and here, three separate
theories were tested and falsified by experiment — forking a live session,
concurrent tutor and builder, payload size as the direct cause. Each was
plausible, each was disproven, and none of it was necessary: the stderr the SDK
was already able to pipe (`options.stderr`, unset until §8) contained the
answer the whole time.

**Rule.** Before theorising about a transport, capture what the transport says.
An hour of hypotheses lost to a stack trace nobody had asked for is the most
expensive hour in this document — and the second most expensive is building a
guard that runs on the channel it is meant to protect.

### 1f. Giving up on prevention, and making the failure survivable instead

Five sections of this chapter try to stop the channel dying. It kept dying. The
change that finally helped accepted that and asked a different question: *when
the call does not arrive, what is actually lost?*

Almost nothing, as it turns out. `Stream closed` means the call never reached
this process, so **the agent had already done the work** — it only failed to
hand it over. Three consequences, each with its own recovery:

- **Demo builds.** The finished document is sitting in the builder's scratch
  directory. Of the drafts on disk when this was written, **13 of 13 passed
  `validate_demo_html`**. Four demos had been rescued by hand, one at a time,
  before anyone noticed that the rescue was pure mechanism:
  `demojobs._rescue_draft` now publishes the draft when a build fails, gated on
  the same validation `save_demo` applies. It can turn a failure into a success
  but never into a bad demo.
- **Knowledge writes.** Nothing server-side can retry them, because nothing
  server-side ever saw them. But the *transcript* records each failed call with
  its arguments — **eight recoverable calls in one session file** — so
  `agent.replay_dropped_writes` re-applies them after the turn ends. Run against
  the real store it recovered `columnar_olap_databases`, stuck at `introduced`
  while the tutor's reply described it as practiced.
- **The message.** All of the above, plus two unrelated outcomes, were reported
  as *"no change was made — describe what to change about the demo"* — blaming
  the learner for a transport fault, and for the builder correctly declining a
  request the demo already satisfied. Three outcomes now say three different
  things, and "already does this" is a **success**, not a red chip.

**Replay only what is idempotent.** `update_concept` is an upsert keyed on
`concept_id`, so re-applying it is a no-op. `request_demo` would start a second
build; `run_code` would re-run the code. That property is the criterion, not
convenience — and it is why the allowlist is one tool rather than fifteen.

**The honest limit, measured.** Of ten dropped calls in one session, this
recovers four. Two `save_conversation_summary` calls and four `run_code` calls
stay lost: the first appends an episode (a replay would duplicate it unless
keyed on `tool_use_id`), the second has real side effects and must never be
replayed. Writing the gap down beats implying the problem is closed.

**Rule.** When a failure cannot be prevented, ask what it destroys — often the
answer is "nothing that is not still on disk". Recovery is usually cheaper than
prevention, and always cheaper than a fifth theory about the transport.

### 1g. The recovery that did not run, and a sixth wrong theory

**Symptom.** A live session: `thinking…` for 201 seconds, then five red tool
chips — one `Stream closed`, four `Sibling tool call errored` — and afterwards
**every** call failed at 0s, `update_concept` and `run_code` alike, for minutes.
Three concepts stayed at their old mastery while the reply described them as
practiced.

**Not a new cause.** This is §1a again: the reloader restarting the server
mid-turn and orphaning the CLI's control channel. `RELOAD` defaulting to `0`
prevents it; a dev box with reload on does not have that protection. The five
chips are §1b's collateral pattern — one call hit the dead channel, the API
cancelled its four batch-mates.

**Three wrong theories, in order, all mine.** Worth recording because each was
built on real evidence and each was still wrong:

1. *"Compaction killed the MCP pipe."* Built from two adjacent log lines —
   `SessionStart query=compact`, then MCP failures — plus an invented mechanism
   about child processes and stdio. But this project's MCP server is
   `create_sdk_mcp_server`: in-process functions reached by callback, **no
   subprocess and no pipe**. The mechanism did not exist.
2. *"A network fault."* The `node:_http_client` `emitError` lines are real, but
   they started at 07:43:41 — *three minutes before* the compact boundary — so
   they bracket compaction rather than follow it. Correct observation,
   wrong conclusion drawn from ordering.
3. *"Compaction restarts the session."* It does not. Same `sessionId`, same
   JSONL, appended not replaced: row 353 is `subtype: compact_boundary` with
   `parentUuid: None`, which severs the replay chain so the summary becomes the
   new head. The user's own model of compaction was right and the explanation
   offered to them was wrong.

The tell that settled it was mechanical, not narrative: **a failed call takes
0–4ms**. `update_concept` does a locked read-modify-write of a JSON file; it
cannot start, let alone finish, in 4ms. That number proves the call never
reached this process — no theory about *why* is needed to establish *that*.

**What was actually new.** Not the cause — the recovery's placement.
`replay_dropped_writes` sits at the end of the happy path
([`backend/agent.py:891`](../backend/agent.py)), after `archive_session`. A turn
that dies mid-flight never reaches it, so §1f's recovery is absent in exactly
the failure it was written for. Run by hand afterwards it recovered all three
concepts immediately, `columnar_olap_databases` among them — the same concept
§1f names. The mechanism was right; it just was not reachable.

**Fix.** The UI half is done: the CLI's `compact_boundary` system message now
reaches the browser as a `compacting` event and relabels the indicator
("condensing this long session…"), because 201 seconds of `thinking…` is
indistinguishable from a crash — that ambiguity *is* what turned a working
compaction into a bug report. Read from the message stream, **not** a
`PreCompact` hook: hooks are control requests on the same pipe as the MCP
calls, so a hook cannot report trouble at the moment the pipe is in trouble
(the reason `payload_hooks` is `None`). The replay's placement is still open.

**Rule.** A recovery path that only runs on success is not a recovery path.
Put it where failure goes — a `finally`, or the next turn's startup.

**Rule.** Adjacent log lines are correlation. Before explaining *why* a call
failed, establish *where* it died: a duration too short for the work to have
started proves the call never arrived, and that single number outranks any
amount of plausible narrative built from event ordering.

### 1h. Counting it, finally

Three more incidents, three more confident explanations — compaction, a server
restart, a network fault — each abandoned when the next one arrived. §1e's rule
had been read and still not followed: every diagnosis came from the *ordering*
of two log lines in one window.

So this round parsed every `~/.claude/debug/*.txt` instead of reading one.

**Within a single run** — no restart, no resume, no compact boundary between:

```
07:22:50  run_code        ok 25ms       <- channel healthy
07:22:51  [ERROR] emitError (node:_http_client)
07:23:02  update_concept  FAIL 0s
07:23:07  update_concept  FAIL 0ms      <- not even a millisecond
```

**Across all sessions:**

| | count |
|---|---|
| `_http_client` errors | 274 |
| `Stream closed` failures | 333 |
| http errors followed by a failure within 60s | **118 / 274** |

Every `Stream closed` had a socket error shortly before it — but **156 socket
errors were harmless**, one followed nine seconds later by a perfectly good
`get_feedback`. So the socket error is *necessary but not sufficient*, and the
tempting sentence "the network killed the channel" is measurably wrong as
stated: something about *which* request fails decides whether the control
channel dies with it. Nothing logs what sets `inputClosed`, so that last step
remains inference — and this section exists to stop the next reader promoting
it to a cause.

**What the counting ruled out, by evidence rather than argument:** not the fork
(one session active, and §1e had already falsified forking by experiment), not
compaction (`tokens=118586` against `threshold=167000`), not a reload (zero
process-lifecycle events), not payload size, not §1c's race (which fails
*inside* this process, after the handler runs — these never arrive).

**The gap that made all of it necessary.** §1d prescribed logging the MCP
instance id "at build time **and again at call time**". Only the build half was
ever written, so a failed call could never be placed, and every incident ended
in inference from a suspiciously short duration. The missing half is now a
`_entered()` line at the top of each write tool
([`backend/tools.py`](../backend/tools.py)), which reads directly:

| Log shows | Meaning |
|---|---|
| no entry line | the call never arrived; the pipe was shut |
| entry, no completion | it arrived and our handler died |
| entry, id ≠ build id | it reached a **stale** server |

**Rule.** When the same error recurs and each explanation is abandoned by the
next incident, stop explaining and start counting. A ratio across every log
(118 of 274) refutes a story that any single window would have supported — and
the cases that *didn't* fail are the informative half, which one-window
debugging never sees.

**Rule.** A prescribed diagnostic that was only half-implemented is worse than
none: it reads as covered in review, while the distinction it promised to make
visible stays invisible. Finish it, or write down that it is unfinished.

---

## 2. The plausible-output family

The through-line of this project. Every serious bug here is an output that
*looks right*. None of them look like errors, and the learner — who is by
definition learning the subject — has no way to catch them.

### 2a. The tutor asserted computed values it had never run

**Symptom.** The learner ran code the tutor handed them and got a different
answer than the tutor had claimed. Twice in two days:

```python
tree = DecisionTreeRegressor(max_depth=2); tree.fit(X, y)
tree.predict([[1800]])   # comment claimed 300000
```
Actual: `[350000.]`. And the next day, with the expected probabilities written
into the comments: claimed `[[0.4, 0.6]]`, actual `[[1. 0.]]`.

**The tempting explanation.** The model reasoned about the tree wrong; explain
the split logic better. That is a *content* fix for a *process* failure —
reasoning harder would not have helped, because the tutor was confident and
wrong, and the confidence was the entire problem.

**Actual cause.** Predicted outputs were being written into code comments from
the model's own reasoning, never executed. The user named the fix himself:

> *"instead of writing the code and value, you can run and test it before"*
> — and a minute later — *"you run the code and tell me what you are getting?"*

**Fix.** The `run_code` sandbox, plus a rule now generalised well past Python
([`backend/mathverify.py`](../backend/mathverify.py),
[`backend/sanskritverify.py`](../backend/sanskritverify.py)):

> *your sense of certainty is not evidence about what a library actually does.*

**Rule.** A model's confidence about a library's output is not evidence about
that library's output. If you are citing a computed value, compute it.

### 2b. Verified code that still crashed — verification attaches to bytes

**Symptom.** A snippet presented as verified crashed for the learner anyway.
The user caught it directly:

> *"how did the agent give a verified code which had an error??"*

**The tempting explanation.** The sandbox environment differs from the
learner's. It does not — both run the learner's own venv, visible in the
traceback paths.

**Actual cause.** Verification and presentation were two separate acts. The
tutor ran code, got a clean result, and then **retyped the snippet from memory
into its reply**, adding one extra `export_text` call along the way. That call
had never run. It was the line that crashed.

**Fix.** The user proposed it: *"why don't we temp save the verified code and
then agent just insert it in place? why does it have to generate it again?"* A
clean run is saved under a `run_id`, and the tool result hands back a
`verified_id` plus the literal placeholder to emit. The frontend expands the
fence from the saved source. One run, one placeholder, no edits in between.
Only a **zero-exit** run gets an id — pointing at a crashed snippet would
present the crash as a worked example.

**Rule.** Verification attaches to *bytes*, not to intent. If you retype
verified output, you have unverified it. Give the model a handle to the
artifact, never let it re-serialise the artifact from memory.

### 2c. The same bug returned — because of dict key order

**Symptom.** The next day: *"again instead of inserting the code, it wrote it
again — check if I am wrong?"* The `verified:` mechanism from 2b was already
live.

**The tempting explanation.** The prompt wording was not emphatic enough. Add
another instruction. This would have failed, repeatedly and invisibly.

**Actual cause.** Tool results are truncated to `TOOL_RESULT_MAX_CHARS` (2000)
before being written into the transcript, and the transcript is what the model
re-reads when composing. With `verified_id` placed **last** in the JSON
payload, a run with sizable stdout pushed the id past the truncation boundary.
The model saw no id to cite and fell back to retyping. Observed twice in one
session at exactly 2000 chars.

**Fix.** Reorder the payload: anything the model must *act on* goes first,
bulk output last. Then budget `stdout`/`stderr` against what the fixed fields
leave, with head-and-tail clipping — the tail is kept because a traceback's
useful line is at the end. Note the budget is taken **once across both
streams**: sizing them independently let a cell with large stdout *and* stderr
reach ~3400 chars against a 2000 budget, so `verified_id` survived only by
being first in the dict rather than because the cap worked.

**Rule.** Under truncation, dict key order is load-bearing API design. A
truncation boundary is an invisible feature flag.

### 2d. The near-miss root: a real root, a real form, the wrong answer

The best-documented false premise in the codebase, and a trap that had to be
rejected twice.

**Symptom.** `√pā` ("drink") derived as **पतति** — which is the correct present
tense of `√pat` ("fall"). Returned with `verified: true`.

**The tempting explanation.** The Dhātupāṭha spells roots in *aupadeśika* form
with anubandha markers a learner never types (`√sthā` is listed `zWA`, `√dṛś`
is `dfSi~r`, `√gam` is `gamL`). Each is one character off the obvious spelling.
So: add a one-character fuzzy fallback. The source now carries a standing note
against exactly this ([`backend/sanskritverify.py:344`](../backend/sanskritverify.py)):

> *do NOT try `slp+"a"` / `slp+"i"` as general fallbacks. **It looks harmless
> and is not**: `pA` then matches `pata` (√pat, 'fall') and returns पतति for
> √pā 'drink' — a real root, a real form, and the wrong answer, delivered with
> a verified marker. A near-miss must refuse rather than land on a neighbour.*

**Fix.** Structured refusal, layered: one *narrow, rule-based* variant (dental
`s` → retroflex `z`, a genuine phonological rule, not a guess); a written-out
per-root map for the rest (`{"sTA": "zWA", "dfS": "dfSi"}`) — enumerated by
hand rather than inferred; and otherwise refuse, naming the accepted spelling.
Roots are validated *before* deriving, because the library will build a form
from any string it can parse — that whitelist is the only thing between a typo
and a fabricated form wearing a verified marker.

Two precedence rules in the same function, each a bug in waiting: the 19
hand-checked roots win over a Dhātupāṭha lookup (a lookup by stripped spelling
is a good guess but only a guess); and the curated gaṇa wins over the caller's
default (a right root in the wrong gaṇa derives silently wrong output — √kṛ in
bhvādi is not *karoti*).

**Rule.** Fuzzy matching in a dense namespace does not fail by returning
nothing — it fails by returning a valid neighbour. Where a wrong answer is
indistinguishable from a right one to the consumer, refuse on a near-miss and
enumerate the exceptions by hand.

### 2e. "Unknown" is not "false"

**Symptom.** Two `verified: null` results that look identical and are not: one
root needs a data bundle that is not installed; the other is a typo the library
would happily invent a form for.

**The tempting simplification.** Collapse the tri-state into a boolean.
Rejected, with the sharpest reasoning in the codebase:

> *Collapsing None into False would tell a learner their correct form is wrong
> — the single most damaging thing this module could do, since they cannot tell
> which of us is mistaken.*

Sanskrit has far more legitimate `None` cases than algebra: the data bundle is
a separate download, the kośa does not contain every stem, and a root the
learner invents is not a bug.

**Rule.** In any verification system whose consumer cannot independently check
the answer, conflating "unknown" with "false" converts a silent gap into
confident misinformation.

### 2f. Don't silently correct a reference implementation

**Symptom.** For ī/ū-stem feminines, the grammar engine emits **नदीः** in the
nominative singular where every textbook prints **नदी**.

**The tempting fix.** Strip the visarga. One line, and the output then matches
every textbook.

**Why it was rejected** ([`backend/sanskritverify.py:624`](../backend/sanskritverify.py)):

> *quietly "correcting" a grammar engine is how you end up shipping a second,
> unverified grammar of your own.*

**Fix.** A `caveat` field travels alongside `verified: true`, naming the
divergence and telling the learner to trust the textbook for that one cell.

**Rule.** Every patch on top of a reference implementation is a rule in a
second, untested implementation you now maintain. Annotate the divergence
instead.

### 2g. A successful import is not a working dependency

**Symptom.** The status check reported the grammar package healthy — four of
six probes passed, and `√bhū → भवति` was genuinely correct — while the data
bundle was entirely absent.

**Actual cause.** `import vidyut` succeeds on a machine with none of the data,
and several submodules only fail when first *used*. An import-only check would
report every form "unchecked" while looking healthy.

**Fix.** Probe with a real call, not an import. Report capability
*per-feature*, not per-package, and degrade to a status table plus a manual
install recipe — which is what let the user route around a TLS failure by
fetching the bundle on another device.

**Rule.** A successful import is not a working dependency. Probe optional
backends with a real call.

### 2h. The model discussing a page it was never shown

**Symptom.** None observed — this one was reasoned about before it could
happen, and the reasoning is worth keeping.

**The hazard.** Page images are not re-sent every turn (~2,900 vision tokens
per page, replayed on every later turn, is ~116k tokens over a 20-turn
conversation about one spread). So `needs_resend` keys on
`(session_id, page, window)`. The session id is **not incidental**: a new
session has an empty transcript, so an unchanged position must still re-send.

> *Without that, the model discusses — fluently, and with total confidence — a
> page it has never seen. That is the worst failure this feature can produce,
> and it is invisible to the learner, who has the page in front of them.*

Two placement details follow from it: the check runs *after* session
resolution, because keyed on the unresolved argument it would compare against
`None` and silently never re-send; and `session_id is None` returns `True`,
because a brand-new session must not inherit the previous one's marker.
Separately, when the model's view is narrower than the learner's, the digest
says so explicitly — otherwise it discusses a page it was never shown.

**Rule.** When you cache "already sent", the cache key must include everything
that resets the context. Getting that key wrong fails silently and confidently.

---

## 3. Limits you only meet by losing work

Four different size limits, each invisible until it silently discarded work
rather than raising.

### 3a. The output-token cap discards the whole reply

**Symptom.** A ten-panel demo was never saved. The job reported only *"finished
without saving a demo."* No error, no exception, no partial demo.

**Actual cause.** The builder tried to emit the whole document in one reply,
twice, and both times the API discarded the reply for exceeding the
output-token maximum. **Going over the cap discards the reply rather than
truncating it**, so a too-large demo produces *nothing*.

And the failure arrives as assistant **text** — `API Error: Claude's response
exceeded the 32000 output token maximum` — not as an exception and not as an
error result, so nothing in the pipeline noticed
([`backend/agent.py:1031`](../backend/agent.py)).

**Fix.** Two parts. Pattern-match the API's own phrasing
(`_OUTPUT_OVERFLOW_MARKERS`) so the real reason survives into the job record.
And, structurally: stop asking for the document in one reply. The shell
(doctype, shared CSS, shared JS) is written once with a `PARTS_MARKER`, each
panel is written separately, and the pieces are joined at read time. Roughly
two thirds of a demo is shared scaffolding, so **the panel count stops driving
the size of any single write**.

The user corrected his own first proposal here, and the correction is the
design:

> *"no but it will be one html, but files can be loaded like python functions,
> so instead of one html being massive, we will have the file split into
> separate small files and on loading they all combine together"*

**Rule.** The output cap applies to *one reply*, so the fix is structural, not
a retry. And a cap that discards rather than truncates presents as success.

### 3b. Parallelism silently serialised by a pre-existing limit

**The near-miss.** Panels of one demo are built concurrently **inside one
job**, on purpose. Spawning them as separate jobs would meet
`MAX_CONCURRENT_DEMO_JOBS` — a *per-user* gate — and serialise the fan-out two
at a time, undoing the entire point.

So there are two independent limits: `MAX_CONCURRENT_DEMO_JOBS = 2` (whole
builds) and `MAX_CONCURRENT_PARTS = 4` (panels within one build), with the
reasoning recorded at both.

**Rule.** When you add parallelism, check whether an existing rate limit will
quietly serialise it. This failure mode has no error — just the old latency.

### 3c. Truncation cap ≠ transport cap

Easy to conflate, and they protect nothing of each other's
([`backend/agent.py:138`](../backend/agent.py)):

> *NOTE this is a TRANSCRIPT cap, applied where the event is recorded — **the
> full result has already crossed the transport by then.** It protects replay
> cost, not the channel.*

`TOOL_RESULT_MAX_CHARS` (2000) exists because tool results are replayed on
every session load, so a verbose one bloats the file permanently. It gives
**zero** protection against [§1b](#1b-oversized-payloads-killed-the-mcp-channel).

**And it has its own sharp edge.** A Sanskrit verification result landed at
~1900 chars against that 2000 cap, so a slightly longer stem truncated the JSON
mid-structure — and the tutor read the wreckage as *"the tool is broken."*
Fixed by returning Devanagari only in the visible rows and treating SLP1 as an
internal transport.

**Rule.** A value sitting just under a truncation cap fails as *malformed*, not
as *shortened*, and will be misdiagnosed as a broken tool.

### 3d. Amortised cost: anything in the prompt is re-sent every turn

The same arithmetic drives several decisions. One demo's HTML in the prompt
would cost ~63k tokens over a 20-turn conversation; the digest (ids, handlers,
function names, warnings) is ~200 tokens, with `get_demo_source` as the
on-demand escape hatch. Session demos list only the *current* session's, not
all 100 a learner may accumulate — and doing otherwise would dangle a list of
demo ideas in front of a model explicitly told not to volunteer them. Web
lookups are allowed but the prompt tells the model to summarise rather than
quote at length, because fetched pages land in the transcript too.

**Rule.** Every tool whose result persists in the transcript has a *recurring*
cost, not a one-time one. Budget it per-conversation, not per-call.

---

## 4. Feedback loops and runaway state

### The zoom that would not stop — and was not a zoom bug

**Symptom.** *"when i click on '+' , after 100 percent if i click zoom, it
doesn't stop it keep on increasing."*

**The tempting explanation.** A missing clamp on the zoom value. This is what
everyone assumes, it is how the bug was first reported, and **it would have been
the wrong fix.**

**How it was actually diagnosed.** The user refused a speculative patch and
asked for instrumentation instead: *"can you give me a console log statement so
it can record what is happening."* The log settles it in one glance:

```
[notes.zoom] setZoom {requested: 1.2, clampedTo: 1.2, wasClamped: false, callCount: 1}
[notes.zoom] applied {zoom: '120%', canvasWidthPx: 812,  modelWidth: 812}
[notes.zoom] applied {zoom: '120%', canvasWidthPx: 974,  modelWidth: 974}
[notes.zoom] applied {zoom: '120%', canvasWidthPx: 1169, modelWidth: 1169}
...                                 canvasWidthPx: 5028
```

`zoom` stayed pinned at 1.2. `setZoom` was called exactly once. **The clamp
worked perfectly.** What ran away was the canvas width, growing ~1.2× per
frame.

**Actual cause** ([`frontend/notes.js:1596`](../frontend/notes.js)): the canvas
was `width:100%` of a wrapper whose width was computed *from the canvas*. Each
zoom multiplied the measurement by the zoom factor, the `ResizeObserver` read
that as a pane resize, `rescaleForWidth()` multiplied every block's
coordinates, and it ran away — 677px → 15014px in a dozen frames, **corrupting
the note on every pass**.

So clamping the zoom harder would have hidden the symptom while user data kept
being destroyed.

**Fix.** Break the cycle by giving the measurement an independent source of
truth: measure the *scroll container*, which is never transformed and never
resized by us. The canvas width is set *from* that measurement and never feeds
back into it. The separation is called "load-bearing" in the source. A
secondary detail: `offsetWidth`, not `clientWidth`, because zooming in adds a
horizontal scrollbar — and the scrollbar's appearance would itself be a width
change that re-enters the loop.

**Rule.** When a value runs away, log the value *and everything downstream of
it* before patching. A quantity that is correctly clamped while its dependents
explode means a feedback loop — and clamping harder hides data corruption
rather than stopping it.

---

## 5. Identity, isolation and the shared-store leak

### 5a. One learner, two identities, a third of their memory missing

**Symptom.** None visible. That is the point.

**Actual cause.** `_safe_email` lowercased every filesystem path, but
`episodic.py` stored the *raw* email in ChromaDB metadata and in record IDs,
then read it back with an exact-match filter. Chroma does not case-fold. A
learner who arrived as `YASHG41` could not see rows written as `yashg41` — on
this install that hid **127 of 373 records, roughly a third of the learner's
episodic memory**, from the tutor.

**The trap in the obvious fix.** Reuse `_safe_email` as the identity key.
Rejected: it substitutes anything outside `[\w@.\-]` with `_`, which is right
for a directory name and wrong for an identity — it maps
`yash+ml@gmail.com` and `yash_ml@gmail.com` onto the same key and would
**silently merge two learners**.

**Fix.** A separate `_normalize_email` (case-folding only) as the canonical
identity key, applied at the five `EpisodicMemory` methods every read and write
passes through, the two admin filters that build a where-clause directly, the
`user.json` writer, and both job queues — the last a latent bug, where a
mismatch hides a job from its owner and lets the concurrency cap be exceeded.
The migration reuses the stored 384-dim vector verbatim so nothing is
re-embedded and ranking does not shift; dry-run by default, idempotent, backs
up the sqlite file first.

**Rule.** A key for a *path* and a key for an *identity* have different
correctness conditions. Sanitising is right for one and lossy for the other,
and the lossy direction merges records silently.

### 5b. Two tutors, one vector store

**Symptom.** None visible — again the point.

**Actual cause.** `app` was a purely behavioural switch: it chose prompts, tool
families and curriculum tracks, and never touched a filesystem path. Every
store was keyed on the learner's email alone. `CHROMA_DIR` was hardcoded to
`<repo>/data/chroma`, so overriding `DATA_DIR` alone left both tutors sharing
one vector store.

> *That is the leak least likely to be noticed, because episodic memory is read
> into every system prompt — the model just quietly knows the other tutor's
> conversations.*

**Fix.** Every root derives from one `APP` setting, with tests
([`backend/tests/test_app_isolation.py`](../backend/tests/test_app_isolation.py))
pinning that the roots are disjoint and that **neither is a parent of the
other** — or one app's sweep would walk into the other's users and fail its
jobs.

**Rule.** A configuration switch that selects *behaviour* and a switch that
selects *storage* must be the same switch, or they will drift apart in the
direction that shares data.

### 5c. Fork the content, share the mechanism

The user set this constraint before the second tutor existed, which is why it
worked:

> *"i dont want to diverge so that any fixes or upgrade i do in architecture i
> want to travel to both, only that part i want to remain same, like tools,
> learning and stuff. but prompts and UI will be completely separate"*

And, on scope — an explicit instruction to *remove* capability rather than
carry it: *"code runner and derivation can be removed but rest will remain."*

**Rule.** When cloning a product, decide the shared/forked boundary *before*
the copy, and thread the discriminator as a parameter rather than branching on
it. Otherwise every later architecture fix must be applied twice, and
eventually is applied once.

**A tool-scoping corollary.** Tools are scoped per app in families, and the
reason is sharper than tidiness: *a Sanskrit tutor handed `run_code` would
"verify" a declension by writing a Python script instead of calling the
analyser that actually knows the answer* — which is the exact failure the
correctness tools exist to prevent. The invariant the tests protect is that
**the allowlist the model is shown and the tools actually registered come from
the same family selection**, because that drift is silent: the model calls a
tool, gets an unexpected error, and burns a turn.

---

## 6. Concurrency on one JSON file

**Symptom.** Six `update_concept` calls in one parallel batch. Two returned
`Stream closed`, four returned `Sibling tool call errored`. The lesson vanished
from the tracker; the reply above it looked fine.

**The tempting explanation.** The SDK transport. Nothing had restarted.

**Actual cause — three distinct bugs in one incident**
([`backend/knowledge.py`](../backend/knowledge.py)):

1. **Lost update.** The handlers are `async` wrappers around a *synchronous*
   read-modify-write of one JSON file. The tutor emits them in a single
   parallel batch — one per concept just taught, and sometimes twice for the
   same concept. They interleave on the event loop: each reads the same state,
   and the last save wins.

2. **A per-instance lock would have guarded nothing.** A `KnowledgeStore` is
   constructed per request in `tools.py`, `agent.py` and several routes — two
   handlers touching the same user hold two *different instances of the same
   file*. So the lock is keyed on the file's absolute path, in a module-level
   dict, mirroring `sandbox._lock_for`.

3. **A `ValueError` that took the batch down with it.** `MASTERY_LEVELS.index()`
   raises on any value not in the list, and it is called on the mastery
   *already stored on disk* — a hand-edited file, or one written before a level
   was renamed, is enough. The raise happened while comparing old against new,
   i.e. **after the concept was mutated in memory but before the save**, so it
   lost the write it was in the middle of *and* took down every sibling call in
   the batch. `_mastery_rank` now sorts unknown values lowest, so an odd level
   reads as a promotion rather than a silent demotion.

**Plus a torn-read hazard.** `open(path, "w")` truncates on open and only then
writes, leaving a window where the file is empty or partial — a reader landing
there got a `JSONDecodeError`. `_atomic_write_json` writes to a temp file *in
the same directory* (so it is on the same filesystem), fsyncs, then
`os.replace`. On any exception it unlinks the temp file, so no `.tmp-*.json` is
left for the next reader to trip over.

**The test pins the exact incident** — the real six-call batch, duplicate
included — and yields (`await asyncio.sleep(0)`) *inside* the critical section,
precisely where an unlocked version hands control to a sibling and loses the
write.

**Rule.** `async def` around synchronous file I/O is a lost-update bug waiting
for a parallel tool batch. Lock the whole read-modify-write, not just the
write; key the lock on the resource, not the object; and never leave a mutation
in memory ahead of a call that can raise.

---

## 7. Readiness, restarts and interrupted runs

### 7a. A venv that was "ready" before pip installed anything

**The tempting readiness check.** Does the python binary exist? That marks a
venv ready the instant `python -m venv` returns — *before pip has installed
anything*. A pip failure then left a permanently "ready" but empty venv that
every later run silently trusted, surfacing forever after as unrelated
`ImportError`s.

**Fix.** A `.pymentor_ready` marker written **only after the install
completes**, carrying a `VENV_SCHEMA` version so changing the default package
set forces a rebuild. Readiness = marker exists AND python exists AND schema
matches. A venv directory that exists but is not marked is a failed or
interrupted build — start clean rather than pip-ing into the wreckage. Creation
takes a per-email `asyncio.Lock`, with a re-check inside it, or two cells
dispatched back-to-back would race two `python -m venv` into one directory.

**Rule.** Readiness is the *completion* of setup, never the *existence* of its
first artifact. Write the marker last, version it, and lock the creation.

### 7b. Jobs orphaned by a restart span forever

**Symptom.** *"it still says building, from the past i think."*

**Actual cause.** A job marked `building` whose owning process is gone. Nothing
will ever move it to a terminal state, so the UI polls it forever.

**Fix.** `sweep_stale`, called from the app's lifespan hook: anything
queued/building at startup is marked failed with *"server restarted while
building"*. This is also why job state lives in two places on purpose — `JOBS`
in memory is authoritative while the process lives, and one `.json` per job on
disk so a job survives a refresh *and* an orphan can be reported rather than
spinning.

**The precondition that a later feature invalidated.** The sweep needs no age
threshold because at startup nothing can be building. That is a *temporal*
precondition, not a property of the function — and once the builder became a
full session working for minutes rather than one turn, calling it from a
request handler would start failing live builds. The constraint and its
remediation (gate on age plus a heartbeat) are recorded at the call site.

**Rule.** "Nothing can be running right now" is a claim about *when* a function
is called. Write it at the call site, because a later feature will lengthen the
very window it depends on.

### 7c. Fire-and-forget tasks get garbage collected

`TASKS` holds a strong reference to every running build. **asyncio only keeps a
weak one, so a bare `create_task()` can be collected mid-build.** Classic, and
worth restating because the failure looks like the work simply never happened.

### 7d. Millisecond-keyed run directories collided

Run files were `cell_<epoch_ms>.py` written straight into scratch, which
collided when two cells were dispatched inside the same millisecond — one run
deleted the other's artifacts. Fixed with a fresh directory per execution.

**Rule.** A timestamp is not a unique id.

### 7e. "condensing this long session…" appeared only once the wait was over

**Symptom.** A long session sat on "thinking…" for minutes, and the server log
ended on a `CallToolRequest`. It looked stuck. Then "condensing this long
session…" flashed up for a moment, right before the reply started.

**The tempting explanation.** The tool call had hung, or the label fired late
because of buffering somewhere between the server and the browser.

**Actual cause.** The label was keyed to `compact_boundary`, and the CLI writes
that message *after* compaction. Its sequence is status `"compacting"` →
summarise (the minutes) → status `null` → `compact_boundary`. The tool call had
returned in 0.5s; what came after it was compaction, and nothing said so.

**Fix.** `compaction_event` in `agent.py` treats the `system/status` message
with `status: "compacting"` as the start and keeps the boundary as the end.
Both ends are logged, so the terminal shows the wait as well.

**Rule.** A message named "boundary" or "done" marks when something finished,
not when it started. To show progress, find the message the CLI sends when the
work begins, and check the order in the CLI's source.

---

## 8. Errors that never reach the error channel

A recurring shape, worth grouping: several failures here arrive shaped like
success.

- **Tool results were being dropped entirely.** They arrive as
  `ToolResultBlock`s inside a `UserMessage` — *not* as a top-level message with
  `.type == "tool_result"`. `UserMessage` is a dataclass with no `.type` at
  all, so a `hasattr(message, "type")` check never matched and **every tool
  result was silently discarded**. That blindness is why three failing
  `save_demo` calls produced no log line, no SSE event and no transcript
  record — while the UI showed a successful-looking demo chip for a demo that
  was never written.

- **`is_error` under-reports.** It is set by the CLI for transport failures and
  by our own `_error_result`, but *not* when a tool handler raises and the SDK
  converts the exception to text. So our own error prefixes are treated as
  failures too, and the UI and the log agree.

- **A transport failure arrives as a tool result, not an exception**, so the
  handler at the bottom of the function never sees it.

- **Output-token overflow arrives as assistant text** ([§3a](#3a-the-output-token-cap-discards-the-whole-reply)).

- **Context overflow needs two signals**: the CLI usually reports *"Prompt is
  too long"*, but it can also die with nothing but `exit code 1 … Check stderr
  output for details`. When the message says nothing useful, the transcript
  file's own size is consulted instead.

- **A sandboxed iframe is opaque from outside.** If a demo throws on load its
  buttons silently do nothing and the pane looks fine. A fixed,
  non-interpolated bootstrap script is injected *ahead of* the demo's own
  scripts, forwarding `window.onerror` and `unhandledrejection` to the parent.

- **The trust-dialog warning silently dropped the permission allowlist**, with
  one INFO line in a wall of access logs — and its primary remedy ("run
  interactively once and accept") is *unreachable by construction* when the SDK
  is launched headlessly from a server. The second, non-interactive remedy had
  to be used instead.

**Rule.** Errors do not always arrive on the error channel. If a failure mode
can present as ordinary success-shaped output, pattern-match the output — and
have a second, independent signal for when the first says nothing. Any
execution context you cannot see into needs an explicit error channel out of
it, or every bug in it presents as "nothing happened".

---

## 9. Prompts, tools, and where a rule belongs

The consistent finding: **rules the model must obey every single turn belong in
code; prompts are for judgement calls.**

### 9a. Enforce in code, instruct in prose

- **`run_code` firing for no reason.** After the verify-before-showing rule was
  tightened, the tutor over-applied it and began running cells whose entire
  content was prose it had already written — verifying nothing and permanently
  occupying transcript space. Fixed with a **server-side refusal**, not a
  prompt line: a prose-ratio check and a `computes_nothing` check, both run
  before execution. Refusing the whole cell keeps the lecture out of the
  transcript.

- **Both refusals state what to do instead**, deliberately — *"this costs one
  corrected turn rather than a retry loop."* A refusal without a corrective
  action just gets retried.

- **The threshold was measured, not chosen.** `MAX_PROSE_RATIO = 0.6` is set
  *"from real data rather than taste: in one session the legitimate calls
  measured 11-34% and the abusive ones 71-99%, so 0.6 sits in an empty gap."*
  A threshold with a recorded gap either side is one the next person can move
  with evidence rather than argue about.

- **`TodoWrite` was denied but kept being called.** The bundled CLI refuses it,
  but only *after* the model has spent a turn calling it — and the memory
  prompt tells the tutor to track progress, so it kept reaching for it. Fixed
  by adding it to the deny-list so it never enters the tool list at all.
  **Rule:** a tool that will always be refused should not be advertised; a
  per-turn refusal is a per-turn tax paid forever.

### 9b. Don't enumerate a rule's instances

The user rejected a prompt padded with concrete examples:

> *"no dont put example or cases, it is overfitting in a sense / let agent have
> the freedom to do what it thinks is correct"*

and, separately, on a clause dressed up as a factual premise:

> *"remove this, this is not a condition."*

**Rule.** Enumerated examples in a system prompt act as training data — the
model pattern-matches the listed cases and misses the unlisted ones. State the
rule, don't enumerate its instances. And don't dress a *policy* as a *premise*
the model can argue itself out of.

### 9c. Never restate in a prompt what a data structure knows

The identity prompt hard-coded the list of domains taught; the knowledge graph
also knew it. Adding a domain to one left the other stale — and once the graph
could grow at runtime ([§9d](#9d-a-tool-that-writes-a-taxonomy-must-extend-it)),
the hard-coded list would drift *by design*.

> *"system prompt should be derived from the knowledge graph as to what the
> domains knowledge graph has"*

Now `$domains` is substituted from `taught_domains(app)`.

**Rule.** Templating a prompt from live state costs one substitution; a
duplicated list costs a silent drift every time either side changes.

### 9d. A tool that writes a taxonomy must extend it

**Symptom.** `Input validation error: 'category' is a required property`,
recurring across sessions.

**Actual cause.** `update_concept` required a `category` for a concept that did
not yet exist in the graph. The model had no way to supply a category for a
topic the graph had never heard of — so every call for a *new* topic failed,
which is exactly the case the tool exists to handle.

**Rule.** Required fields whose valid values come from prior state are a
first-run trap. A tool that writes into a taxonomy must be able to extend it.

### 9e. Empty results must say which kind of empty

An empty suggestion list used to claim the curriculum was finished. That is one
of two very different situations, and the wrong one sends the tutor off to
invent advanced material. It now asks the domain map which it is.

**Rule.** "No results" and "no more results" are different answers.

### 9f. The templating engine that would have broken its own rule

The system prompt is assembled with plain `str.replace`, and both obvious
alternatives are rejected in a comment
([`backend/prompts.py:105`](../backend/prompts.py)):

- `.format()` chokes on the literal braces the prompts are full of (JSON
  examples, f-string samples) and raises `KeyError`.
- `string.Template` eats `$$` — its escape for a literal dollar. The formatting
  rules tell the model that `$$...$$` is the **only** math delimiter that
  renders. Template would have silently rewritten that line into the broken
  form it warns against, and every equation would have stopped rendering.

**Rule.** A templating engine has opinions about your content. When the content
*is* a specification of syntax, those opinions are bugs.

### 9g. Deny-lists need per-tool justification

Web search and fetch were denied on the general theory that a tutor should not
browse. The user overruled it:

> *"these two tools should be allowed, how can a teacher teach if they cant
> fetch latest information"*

The tutor teaches libraries that move faster than the model's cutoff, and its
only other route to ground truth is running code — which cannot answer "what is
current" for an API it has never seen. The cost is named rather than ignored
(fetched pages land in the transcript and are re-sent every later turn), so the
prompt says to summarise rather than quote.

**Rule.** Justify deny-lists per tool, not by category.

### 9h. Prose in a string literal is prose you cannot review

The system prompt was one Python string literal. Two separate problems, both
recorded in [`backend/prompts.py:1`](../backend/prompts.py):

- **Structure was invisible.** *"The tutor's code-execution rules had grown to
  12 bullets buried inside a list about teaching style, and that was invisible
  while they were a Python literal."*
- **The escaping was lying.** LaTeX backslashes had to be **quadrupled**
  (`\\\\theta`) to survive Python's escape processing, and `{` doubled to
  survive `.format()`. In a file, the raw characters are what reach the model —
  which matters rather a lot for a maths tutor.

**Fix.** One `.md` per topic, assembled by numeric filename prefix so inserting
a section is a *new file* rather than an edit in the middle of an existing one.
`_common/` and `<app>/` merge into one numbered sequence and interleave by
prefix, with an app file overriding a `_common` file of the same name.

**Two naming near-misses worth keeping.** The directory is `prompt_files/`, not
`prompts/` — *a `prompts/` directory beside `prompts.py` would shadow the
module on import*. And the read cache is keyed by **full path, not bare
filename**, or two apps' `00_identity.md` would collide.

**Rule.** Move prose to files, order by filename, key any cache by path.

### 9i. Zero-argument tools have no natural size bound

Found while auditing for [§1b](#1b-oversized-payloads-killed-the-mcp-channel),
not from a user report. `get_learning_progress` returned every field of every
concept — **54,842 bytes measured against a real learner** — on a
zero-argument tool the tutor can call at any time. Notes alone were ~22KB
across 84 concepts. `get_domain_map` returned ~15KB in one result, *which then
rides in the transcript for the rest of the session*.

**Fix.** Default to a summary (counts and mastery buckets, no notes) with
drill-down by `concept_id` or `category`; `get_domain_map` defaults to what can
be started now, while keeping the `total/available/blocked/known` counts so the
shape of the track survives the filter.

**Rule.** Audit zero-argument tools first. The model calls them speculatively,
they have no argument to bound their output, and the result rides in the
transcript forever.

### 9j. A repair pass in the renderer beats a rule in the prompt

The `verified:` placeholder was sometimes emitted on a bare line with no fence.
`marked` produced a `<p>`, and GFM autolinked it — because `verified:` parses as
a **URL scheme**. The learner saw a dead blue link where code should be.

Fixed by rewriting a bare-line reference into a proper `<pre><code>` before
expansion, with the reasoning stated
([`frontend/app.js:535`](../frontend/app.js)):

> *Rewriting it here rather than only tightening the prompt: the instruction is
> one line in a long system prompt, and a missed fence should degrade to the
> right output instead of a broken link.*

**Rule.** When a model's output format is load-bearing, put a repair pass in the
renderer. A prompt rule is a probability; a parser is a guarantee.

### 9k. A check that cannot decide the real bug should warn, not reject

**Symptom.** A demo's buttons did nothing. The console — which the user had to
paste, because the frame is sandboxed and opaque from outside — showed:

```
about:srcdoc:361 Uncaught TypeError: Cannot read properties of null (reading 'classList')
    at setScenario (about:srcdoc:361:47)
    at HTMLButtonElement.onclick (about:srcdoc:191:60)
```

**Actual cause.** `id="btn-s1"` in the markup, `getElementById("btn-" + key)`
in the lookup. It throws on the *first* click, and the sandbox swallows it.

**The tempting fix, and why it cannot work.** Write a static check that catches
concatenated-id lookups. `demos.py` records why that specific check is
impossible ([`backend/demos.py:487`](../backend/demos.py)):

> *Static analysis cannot resolve it — the suffix only exists at runtime, and
> the literal prefix `btn-` does match the real ids, so no regex separates the
> broken case from a correct one.*

**Fix — three layers, each covering what the others cannot.** *Prevention* in
the skill (derive ids from one source; guard every lookup). *Detection* of only
the decidable subset — the literal mismatch — surfaced as a **warning, not a
rejection**, because *"ids can legitimately be created at runtime, and refusing
a working demo is worse than flagging a suspect one."* And a *runtime relay*: a
fixed bootstrap script injected ahead of the demo's own scripts forwards
`window.onerror` to the parent, so a broken demo says so instead of sitting
inert.

The handler check similarly does **not model scope**, for the same reason — *a
regex that guessed at scope would reject working demos.* The scope rule lives
in the skill; the regex catches the plain typo.

**Rule.** Know which failures your static check *cannot* see, write that down
next to the check, and route those failures to a runtime channel instead. Never
let a heuristic reject correct work to catch a bug it can only guess at.

### 9l. Model output is untrusted input

Adopted pre-emptively rather than after an incident, and worth recording as
such. Expressions reach `mathverify` as LaTeX *from a model*, and sympy's
parser will happily evaluate arbitrary Python if handed the wrong entry point.
So: only `parse_latex`/`sympify` with `evaluate=False` semantics; refuse
anything unparseable rather than guessing; cap at `MAX_EXPR_CHARS = 2_000`
because *"a model that emits a 20KB 'step' has gone wrong in a way no
verification result would usefully describe."*

The same shape recurs everywhere a model-authored value crosses a boundary: run
and demo ids are validated against `^run_\d+_[a-f0-9]+$` and `^d_[0-9a-f]{6,32}$`
before any network call or path join; demo paths are confined to the scratch
root; and fetched verified source is set with **`textContent`, never
`innerHTML`** — it is source, and must render literally rather than parse as
markup.

**Rule.** Validate the *shape* of model output before it reaches a parser, a
filesystem, or the network — and prefer a parser that refuses over one that
guesses.

---

## 10. Boundaries that were not boundaries

### 10a. An allowlist consulted by a check you disabled

**Symptom.** The tutor wrote an HTML file to `/tmp` and started
`python3 -m http.server 8000` while "restricted" to MCP tools.

**The false premise.** That `allowed_tools` is a sandbox. It is not — the
allowlist is consulted *by the permission check*, and `bypassPermissions`
**skips that check** ([`backend/agent.py:1220`](../backend/agent.py)).

**Fix.** `disallowed_tools` is enforced regardless of permission mode. The deny
list is what actually holds the line.

**Rule.** An allowlist consulted by a check you've disabled is decoration.
Verify which list survives your permission mode.

### 10b. `cwd` and `HOME` are conveniences, not confinement

An audit commissioned the moment the tutor gained `run_code` — not a bug
report. Its value is the list of premises it killed:

- **`cwd=run_dir` and `HOME=scratch` are boundaries.** No — they set the
  resolution base for *relative* paths only. Nothing intercepts
  `open("/absolute/path")`, and redirecting `HOME` does not protect `~/.ssh`;
  the literal path still resolves.
- **The `sitecustomize` import hook is a restriction.** No — it wraps
  `builtins.__import__` but unconditionally returns the real import. It never
  denies anything.
- **A freshly-built `env` dict scrubs the environment.** Partially.
  `ANTHROPIC_API_KEY` genuinely is not inherited — a real accidental win — but
  `PATH` is, which hands the child `git`, `curl`, `ssh` and every homebrew
  binary. Combined with `subprocess`, the Python-level scrubbing is decorative.

The sharpest observation: the venv lives *inside* the data dir, so a cell
already holds a legitimate write handle adjacent to user records — and can
rewrite `prompts.py`, `tools.py` or `sandbox.py` itself. That is persistent
compromise of the agent, not merely data theft.

**The real lesson is about the docstring.** `sandbox.py` already said *"There is
no jail/seccomp/container — the user accepted this trade-off in exchange for
full package support. Do not expose this endpoint to untrusted users."* That
premise — *only the trusted human triggers it* — is exactly what adding a
tutor-callable tool broke, **without a line of the sandbox changing**.

**Rule.** A documented security trade-off is scoped to a threat model. Adding a
new caller invalidates the scope silently. Re-read every "we accepted this"
comment whenever a new path reaches the code.

### 10c. Two environments, and the tests were not running the server's code

**Symptom.** Two test failures in `test_sanskritverify.py` were written off as
"pre-existing, missing `vidyut` module" for an entire session, and repeatedly
reported as an unchanged baseline.

**Actual cause.** The project had **two** virtualenvs. `setup.sh` creates and
reuses `.venv`; the README told you to create `venv`. Both existed, both had
the dependencies, so nothing ever failed loudly:

- `.venv/bin/python` → `/opt/anaconda3/bin/python` — what `python run.py` used
- `venv/bin/python3.12` → a real 3.12 — what the tests used

Only `.venv` had the `vidyut` Sanskrit bundle. Removing the stray `venv/` took
the suite from **361 passed / 2 failed to 405 passed / 0 failed** — the
"pre-existing failures" were an artefact of running the tests under a different
interpreter than the server.

**The tell that was ignored.** The server log said
`Using bundled Claude Code CLI: /opt/anaconda3/...` on every start, while every
verification ran against the other tree. It was read a dozen times before
anyone asked why the paths disagreed.

**Rule.** One interpreter per project, named once, in the setup script and the
README both. And when a test failure is written off as environmental, confirm
the environment is the one the application actually runs in — otherwise
"pre-existing" is doing the work of "unexamined".

---

## 11. Someone else's state, in someone else's format

**Symptom.** The SDK keeps its session transcripts under `~/.claude`, outside
the project — so `data/` was not portable, and the raw transcripts were hard to
find.

**The obvious fix, and why it fails.** Set `CLAUDE_CONFIG_DIR` to relocate them
into the project. **It does relocate them — and it breaks authentication.** The
experiment is recorded rather than asserted
([`backend/agent.py:64`](../backend/agent.py)):

```
claude -p "say ok"                        -> ok
CLAUDE_CONFIG_DIR=... claude -p "say ok"  -> Not logged in · Please run /login
```

Subscription credentials live in the macOS Keychain, and the CLI stops finding
them when the config dir moves. Copying `~/.claude.json` across does not fix
it. The note is correctly scoped: *revisit if the app moves to a real
`ANTHROPIC_API_KEY`, where keychain lookup is not involved.*

**Fix — the user's proposal.** *"keep that as it is but also maintain a copy
here inside sessions"*: mirror, don't move. Copy after each turn; on startup,
restore anything present in the project but missing from `~/.claude`. And copy
the file **byte-for-byte** — *"It is an internal format we do not control, so
parsing or rewriting it would risk producing something the CLI refuses to
resume."*

**The consequence, documented rather than discovered later.** App state is now
split: the UI renders from `data/users/<email>/sessions/`, while `--resume`
reads the CLI's copy. **Deleting one leaves the chat looking intact while the
model has lost every prior turn.**

**A related constraint.** `cwd` is *not* parameterised for builder agents,
deliberately: the CLI derives its `projects/<slug>/` transcript directory from
`cwd`, and `replay._get_claude_project_dir` recomputes that slug from
`PROJECT_ROOT`. Pointing a builder at its scratch dir would file its transcript
elsewhere, `session_exists_in_claude` would answer `False`, and refining a demo
could never resume the session that wrote it.

**Rule.** When a dependency's state lives somewhere inconvenient, mirroring
beats relocating — relocation moves the data and leaves the credentials behind.
And copy an opaque format verbatim; parsing it is how you produce something its
owner refuses to read.

---

## 12. Untrusted content crossing a boundary

Three boundaries where content authored by a model — or fetched at a model's
direction — reaches something that will act on it.

### 12a. Two parsers claiming the same punctuation

**Symptom.** A page of raw `$\hat{y} = \beta_0 + \mathbf{x}^T\boldsymbol{\beta}$`
instead of rendered mathematics.

**Actual cause.** Markdown runs before KaTeX. An `_` in a formula with two
subscripts reads as an **emphasis pair**, so `marked` emits an `<em>` *inside*
the span — splitting the formula so KaTeX's text-node walk never sees a whole
`$$…$$`. Separately, `\%` and `\_` are escapable punctuation, so marked strips
the backslash and KaTeX then fails on the bare character.

**The tempting fix.** Tune the math-detection heuristic. Every variant tried
broke a legitimate case.

**Fix.** Don't tune the heuristic — **remove one parser's input.** Math spans
are extracted, replaced with a placeholder that markdown cannot act on
(letters and digits only, no punctuation), parsed, then reinserted verbatim
([`frontend/app.js:290`](../frontend/app.js)).

**Why `$$` only, and not single `$`.** A single `$` cannot be distinguished
from currency: *"from $5 to $10"* renders as one math span, and a price earlier
in a sentence swallows the delimiter of real math later in it. `ignoredTags` is
called load-bearing — without it *"a `$` in a shell snippet or an f-string opens
a math span and eats the rest of the block."* And because that guard runs on
the DOM long after parsing, prose and code have to be split *before* it too.

**Rule.** When two parsers claim the same punctuation, don't arbitrate between
them — hold one's input out of the other entirely.

### 12b. The right sanitization depends on whether content is stored

Two markdown paths, deliberately **not** shared. The chat transcript feeds
model output straight to `marked`. Notes and derivation blocks **escape
first**, and the reason is precise
([`frontend/derivation-pane.js:13`](../frontend/derivation-pane.js)):

> *block content is stored on the server and re-rendered on every load, which
> makes it a stored-XSS surface the chat transcript is not.*

(marked dropped its own sanitizer in v8, so this is not inherited.) Fenced code
still renders because escaping runs before the parser sees the backticks.
Mermaid runs at `securityLevel: "strict"` because it injects generated SVG into
the main document.

**Rule.** Sanitization requirements follow from whether content is *stored and
re-served*, not from where it came from.

### 12c. The sandbox attribute pair that cancels itself

`allow-scripts` **without** `allow-same-origin`. The two together *"would
defeat the sandbox entirely, because the frame could then reach into this
document and remove its own sandbox attribute"*
([`frontend/demo-pane.js:183`](../frontend/demo-pane.js)).

`allow-scripts` alone puts the demo in an opaque origin: its JS runs but cannot
read `localStorage`, cookies, or call any `/api/` route. That matters more here
than usual — *"this app has no auth; the email in every request path is the
whole identity model"* — so a demo reaching the API would be reaching the
learner's data.

The design works *with* the boundary rather than weakening it: errors are
relayed **out** by `postMessage`, theme is delivered **in** by `postMessage`
(never string-injected into the HTML, so there is no concatenation path for
markup injection), and screenshots are captured by the frame itself, because
its document is same-origin *to itself* while the parent cannot read in —
"which is exactly the sandbox working as intended."

**One more.** Reassigning `srcdoc` does not stop the previous demo. The iframe
**element** is replaced every render, or a leftover `setInterval` keeps running
and competes for the main thread.

**Rule.** Work with a sandbox's restrictions — a channel out and a channel in —
rather than removing the attribute that makes it a sandbox.

### 12d. A server-side request with a model-chosen destination

`open_book` lets the **tutor** pass a URL it found by searching. Every rule in
[`backend/bookresolve.py:1`](../backend/bookresolve.py) is described as
*"load-bearing rather than defensive habit"*, and each closes a specific hole:

- **https only.** Not http, not `file://`, not `ftp://`, not `data:`.
- **The hostname is resolved first and every returned address checked** — a
  name can point anywhere. This is what blocks `169.254.169.254` (the cloud
  metadata endpoint), localhost, and the private ranges.
- **Redirects are followed by hand, re-checking each hop.** httpx's
  `follow_redirects=True` *"would check the first host and then follow a 302
  into 127.0.0.1 without looking."*
- **The body is capped while streaming** — *"a Content-Length header is a
  claim, not a fact."*
- **An allowlist, on by default**, because the model picks the URL and starting
  closed is the right direction.

And the content check: bytes are verified as a PDF **by content, never by
Content-Type**, because *"both archive.org and Google answer a bad request with
an HTML page at HTTP 200, and a captcha saved as .pdf is exactly the failure to
catch here rather than three steps later inside the parser."*

**Rule.** A URL chosen by a model is a server-side request forgery primitive.
Resolve then check, re-check every redirect hop, cap while streaming, and
verify content rather than trusting a header. A 200 is not a success.

---

## 13. Corrections that changed the design

Cases where the user rejected the approach outright, and the rejection was
right.

### 13a. "Treat this subagent spin as a new session and make sure it is capable"

**Symptom.** Two builds of the same ten-panel demo produced nothing.

**Actual cause.** The builder was equipped as a *single tool call*: the prompt
told it to emit the whole document at once, it had nowhere to draft, and no way
to check its own work.

**The correction**, which reframed the whole subsystem:

> *"when the main agent asks for this tool, it hands over everything to that
> tool, now this tool is a chat on its own, so it has the freedom to build
> whatever it wants however it wants and then hand over whatever it created to
> main chat, **so treat this subagent spin as a new session and make sure it is
> capable**"*

**Fix.** The builder became a full agent session with file tools and a scratch
directory — but explicitly **no shell**. The reasoning is pinned in a test: the
request text comes from a learner and `permission_mode` is bypassed, so a shell
would turn "build me a demo" into arbitrary code execution as the server's
user. A companion test pins that **widening the builder must not widen the
tutor**.

**Rule.** If you want a model to produce something it must iterate on, give it
a workspace and a session — not one tool call. Then scope its tools to that
workspace, because untrusted text now reaches an agent running with permissions
bypassed.

### 13b. "Every file transfer needs to happen using file path"

The user generalised [§1b](#1b-oversized-payloads-killed-the-mcp-channel) into a
project-wide invariant and asked for a sweep:

> *"as a global setting, check if any tool anywhere is sending these files as
> payloads be it mcp or between tools, every file transfer needs to happen
> using file path... do a global sweep of it"*

**Rule.** When you find a bug class, sweep for the class — not the instance.

### 13c. "Why is frontend even polling and for what?"

When log noise from a 2s poll was obscuring a bug, the proposed remedy was
log filtering. Rejected:

> *"no but why cant we start the polling when build starts and stop when
> failed"* … *"okay so if that is the case then now what do you want to do for
> debugging?"*

**Rule.** When noise obscures a bug, fix the *lifecycle of the noise source*.
Don't filter the logs, and don't let the noise become the investigation.

### 13d. "I will test it locally. You don't run."

A standing instruction from the first UI session. Worth recording that it did
**not** slow diagnosis down — it produced *better* evidence than automation
would have. Every hard UI bug here was solved from something the user pasted:
the zoom log that disproved the clamp hypothesis
([§4](#4-feedback-loops-and-runaway-state)), the `about:srcdoc` traceback, the
`/resolve` 404s, the token-accounting table that found the payload bug.

**Rule.** The person who owns the running app owns the verification loop. Your
job is to hand them the instrumentation that makes their observation decisive.

### 13e. "I can't see what it is thinking, or it is not asking me questions"

After four rounds of feedback on one demo:

> *"for demo i keep on telling, but i am not seeing the improvement, agent is
> not able to understand what i am saying or atleast i cant see what it is
> thinking, or it is not asking me questions"*

Two failures in one complaint: the demo's convergence logic was genuinely
wrong, *and* the feedback loop was opaque — version numbers incremented with no
visible reasoning and no clarifying question, so a misunderstanding could
repeat indefinitely at ~60s per round.

**Rule.** A background agent that emits only "done, v4" gives the user no way
to correct a misunderstanding, and the iteration loop becomes a random walk.
Surface the reasoning, or let it ask.

### 13f. "Okay, verify it"

Two pushbacks worth recording as a method rather than a fix:

> *"no so i am not satisfied with what you are saying"* … *"okay verify it"*

Both were demands to check a claim about what the model actually sees against
the **raw transcript JSONL**, rather than accept reasoning about it. The claim
in question — what is in the context window on a given turn — is exactly the
kind that is easy to reason about confidently and get wrong.

The same session produced a self-caught wrong mental model, worth keeping
because it is the ordinary case: *"i was getting confused with this skill.md, i
thought this is also being read."* It is not — the CLI globs `<name>/SKILL.md`,
so a `README.md` at that level is never read by the app and costs nothing at
runtime. That discovery rule is now written down at the point it matters.

**Rule.** A claim about what is in a context window is checkable. Check it.

---

## 14. Small edge cases worth keeping

Individually minor, collectively the texture of the thing.

- **Two hints overwrote each other.** A timeout exits `-15`, which is non-zero,
  so a killed cell that had *already* printed a `ModuleNotFoundError` set both
  a timeout hint and a package hint — and the package hint used to overwrite
  the timeout advice, sending the model back to the inline path to be killed
  again instead of to `background=true`. Fixed by setting the package hint only
  when nothing else set one.

- **`sklearn` is a trap.** It exists on PyPI as a broken placeholder; the real
  distribution is `scikit-learn`. A missing-import failure returns the correct
  PyPI name rather than letting the model guess (`cv2` → `opencv-python`, and
  so on).

- **The first matplotlib plot in a fresh venv times out** — building the font
  cache alone can exceed the inline budget. It succeeds on retry, so the same
  "resubmit as background" advice covers both this and genuinely slow work.

- **A timestamp is not a unique id.** Run files were `cell_<epoch_ms>.py`
  written straight into scratch and collided when two cells were dispatched in
  the same millisecond — one run deleted the other's artifacts.

- **A descendant selector needs the ancestor's *parent* as its scope.** Both
  `highlightCodeIn` and `addRunButtonsIn` select `"pre code"`, so passing the
  `pre` itself matched nothing — it would need a `<pre>` nested in a `<pre>`.
  Compounding it: verified source arrives async and misses that turn's
  enrichment pass, so both helpers are re-run on arrival — safe only because
  they are idempotent and flag-guarded.

- **Strict session-scoping hid demos the learner was looking at.** A strict
  `chat_session_id` match silently hides anything built before that field
  existed, *and* an edit-branch fork or a restore-from-memory mints a new
  session id that no existing demo carries. Hence the fallback to recent demos.

- **A literal path segment collides with a path parameter.**
  `/demos/{email}/resolve` must be declared *before* `/demos/{email}/{demo_id}`,
  or `resolve` is captured as an id — FastAPI matches in declaration order.

- **A tool handler cannot see its own `tool_use_id`.** So a tool card can record
  only the *arguments* of the `save_demo` call that created a demo, never the
  id, which exists only after storage. Resolution is therefore
  content-addressed on title then `concept_id`, over a **newest-first** index —
  a deliberate tie-break, since "open the demo from this message" should mean
  the most recent build of it.

- **A regex missed single-parameter arrow functions.** `const reset = e => {}`
  written without parentheses was reported as an undefined handler.

- **Demos run in a sandboxed frame with no network**, so any external URL
  renders blank. `save_demo` rejects external URLs and names the offending one.
  This is a functional constraint, not a style preference. (`xmlns=` is fine —
  a namespace, not a fetch.)

- **`setting_sources=["project"]` is required or skills are silently ignored** —
  the SDK default is `None`, which loads nothing from disk. `"user"` is
  deliberately excluded: it would pull in settings belonging to whoever runs the
  server, which vary by machine and have nothing to do with the learner.

- **A cap looser than its guard denies a call nothing can make.** A test asserts
  `MAX_TOOL_PAYLOAD_BYTES <= 14_000` so the two limits cannot drift into that
  state.

- **Granting a strictly weaker verb shrank payloads at zero added privilege.**
  The demo builder had been denied `Edit`, so its only write verb was `Write` —
  which replaces a whole file, and made `Grep`/`Glob` near-useless (it could
  *find* the line and had no way to change it). `Edit` cannot create a file or
  reach a path that does not exist, so it adds no reach. The comment is honest
  about what remains: `Write` with an absolute path can still escape scratch —
  *"cwd plus the prompt make scratch the default, not a jail — denying Bash is
  what actually bounds the blast radius."*

- **A guard must not invent failures.** In the payload hook an unreadable path
  is allowed through — *"let the tool report its own."*

- **A per-task prompt that contradicts the system prompt yields a half-built
  artifact.** The per-build prompt ended *"Call save_demo once with the complete
  HTML"* while the system prompt taught the split path — and the builder split
  the difference, writing a shell and declaring no panels. Per-task prompts now
  state **what** to produce, not which tool to call; tests assert the old
  phrases appear in neither prompt.

- **Fan-out multiplies prompt size by N.** Each panel author was being sent the
  whole 8–20KB shell. It now receives a *digest* — the class names and helper
  names, which is all a panel author needs. Send the interface, not the
  implementation.

- **Partial success beats all-or-nothing** — *"nine panels that render beat ten
  that do not"* — but an all-failed build fails loudly and never publishes,
  because *"an empty demo must not reach the learner's list."*

- **Identity-by-address is unsound across object lifetimes.** A concurrency test
  holds the context dict *objects*, not their `id()`s: a freed dict's address is
  reused by the next one, so comparing by `id()` would report a false clash.

- **`read()`, not `readline()`.** A progress bar that rewrites with `\r` and
  never emits a newline stalls until it finishes. And the decode must be
  incremental — a naive `chunk.decode()` splits multi-byte characters across
  read boundaries and yields mojibake.

- **`X-Accel-Buffering: no`** — without it, nginx-style proxy buffering holds
  SSE deltas back and defeats token-level streaming.

- **Teardown belongs in `finally`.** `marked.parse` on arbitrary model output
  can throw; without it the composer stays disabled and the UI soft-locks
  permanently.

- **A cross-device `shutil.move` would silently produce a broken venv** — it is
  ~469MB and bakes absolute paths into its scripts. Check `st_dev` and refuse:
  *"better to stop and say so than to 'helpfully' do the slow wrong thing."*

- **Colour is derived, not hand-picked.** 31 hand-chosen hues are
  indistinguishable, so one hue per track with a lightness/chroma step per
  category, in OKLCH for perceptually uniform lightness. The ramp runs in
  **opposite directions per theme**, or foundational categories go white-on-white
  in light mode.

- **Devanāgarī CSS is functional, not stylistic.** `letter-spacing: normal
  !important`, because any non-zero tracking **splits conjuncts** (क्ष → क् ष)
  and the shared heading styles do set tracking; `line-height: 1.85`, because
  matras collide at Latin 1.5; `font-synthesis: none`, because a faked bold
  smears conjunct shapes.

- **Curriculum integrity checks are fatal at import** — dangling prerequisite,
  cycle, self-loop, a category spanning two tracks, a track in no app or in two.
  *"The app refuses to start rather than teach around a hole."*

---

## 15. The recurring shapes

Six patterns that appear across otherwise unrelated bugs.

### "Plausible" is the enemy word

It appears independently in the run-code rule, the demo id-checker, the
Sanskrit root resolver (*"a plausible but WRONG form"*), the near-miss note, and
`mathverify`'s docstring (*"a derivation that looks right, reads right, and is
wrong in step three teaches a false mechanism more convincingly than no
derivation at all"*). **Every serious bug in this project is an output that
looks right.** None of them look like errors.

### The right answer is usually "refuse and say why", not "guess better"

Near-miss roots refuse. Oversized payloads are denied with per-tool advice.
Missing data yields `None`, not `False`. Divergences from a reference are
flagged, not patched. Cells that compute nothing are refused with the reason.

The asymmetry: a loud refusal is cheap; a confident wrong answer to someone who
cannot check it is not.

**With one deliberate inversion.** Where the *checker* is the thing that might
be wrong — the demo id and handler analysers, which cannot decide the case that
motivated them ([§9k](#9k-a-check-that-cannot-decide-the-real-bug-should-warn-not-reject))
— it warns instead of rejecting, because refusing a working demo is worse than
flagging a suspect one. Refuse when *you* are certain and the consumer cannot
check; warn when *you* are guessing.

### Symptom-level fixes were repeatedly the dangerous ones

Worth listing together, because in each case the obvious fix is *worse than the
bug*:

| The obvious fix | What it would have done |
|---|---|
| Clamp the zoom harder | Hidden ongoing note corruption |
| Strip the visarga | Forked the grammar into a second, untested one |
| Collapse `None` into `False` | Told learners their correct answers were wrong |
| Add retries for `Stream closed` | Retried into a server that was restarting itself |
| Strengthen the prompt wording | Fought a truncation boundary with prose |
| One-char fuzzy root fallback | Returned a different real root, marked verified |
| Set `CLAUDE_CONFIG_DIR` | Moved the data, left the credentials behind |
| Trust `allowed_tools` | Decoration under the permission mode actually in use |
| `reload_excludes` for scratch | A filter that is never consulted |
| Let the tutor stop running code | Moved the work to the learner, fixed nothing |

### Instrumentation before patching

Twice at the user's insistence: the zoom console log that disproved the clamp
hypothesis, and the `PostToolUse` size logger added specifically so the payload
cap can be tuned *"on evidence instead of guesswork"* rather than by losing
another build. The stderr ring buffer exists for the same reason — it is read
only when something has already gone wrong.

### The person watching it run was usually right first

Worth recording honestly, because it says where diagnostic authority actually
sat. In four of the largest incidents the shipped fix was proposed by the user
*before* the assistant reached it:

| The user's line | What it turned out to be |
|---|---|
| *"are we sending the entire code as payload?? i mean you test it and see"* | The 14KB transport cap ([§1b](#1b-oversized-payloads-killed-the-mcp-channel)) |
| *"why dont we temp save the verified code and then agent just insert it in place?"* | The `verified:` placeholder ([§2b](#2b-verified-code-that-still-crashed-verification-attaches-to-bytes)) |
| *"we will have the file split into seperate small files and on loading they all combine"* | Shell-plus-parts ([§3a](#3a-the-output-token-cap-discards-the-whole-reply)) |
| *"keep that as it is but also maintain a copy here inside sessions"* | Mirror, don't move ([§11](#11-someone-elses-state-in-someone-elses-format)) |

The pattern is not that the user knew the codebase better. It is that he was
**watching the thing run** and kept asking what the evidence actually showed —
*"can you give me a console log statement"*, *"okay verify it"*, *"i mean you
test it and see"* — while the assistant reasoned from the error message it had
been handed. Every entry in the table above came from someone refusing to
accept the first plausible story.

### Handles, not payloads

The same idea at four scales: verified code cited by `run_id`; demos moved by
filesystem path; tool results ordered so the handle survives truncation; page
images sent once and referenced by digest thereafter. The user's own
formulation is the cleanest — *"every file transfer needs to happen using file
path."*

---

## Appendix: the practice worth copying

The highest-signal documentation habit in this repo is not this file. It is
that **the test files are written as post-mortems**. Each opens with a
docstring naming the incident, the wrong theory, and the real cause — with the
actual numbers:

- [`test_payload_guard.py`](../backend/tests/test_payload_guard.py) — *"four
  demos had to be rescued by hand"*, the CLI stack frame that explains the
  error string, and why the hook that looked like the right home for this rule
  could not be.
- [`test_knowledge_concurrency.py`](../backend/tests/test_knowledge_concurrency.py)
  — the exact six-call batch from the incident, duplicate included, and *"the
  message pointed at the SDK transport, but nothing had restarted."*
- [`test_demo_paths.py`](../backend/tests/test_demo_paths.py) — the four
  transfers of one document for a 41-line change.
- [`test_demo_parts.py`](../backend/tests/test_demo_parts.py) — why the cap on
  one reply makes the fix structural.
- [`test_app_isolation.py`](../backend/tests/test_app_isolation.py) — *"the leak
  least likely to be noticed."*

The same habit runs through the source comments: they record what was tried and
did not work (`reload_excludes`), the measurement behind a constant (the
14,264-byte Edit), and the fix that was rejected and why (`slp+"a"` fallbacks,
`string.Template`, `_safe_email` as an identity key).

A comment that records a *rejected* approach is worth more than one that
describes the current one — the current one is visible in the code; the
rejected one is what stops the next person re-introducing the bug.

### The test that passed while the feature was switched off

Every fix in this document was mutation-tested — the change reverted, the suite
re-run, the specific failure confirmed. Twice that caught something no amount
of reading would have:

- **A guard nothing called.** `check_payload_size` had six tests and all of
  them called the function *directly*. Disabling it at the five call sites in
  `tools.py` left the entire suite green: the check existed, was correct, and
  was wired to nothing. The fix was one test that goes through the real MCP
  handler.
- **An identity check that could not fail.** A test asserted four concurrent
  agents got distinct `build_ctx` dicts by comparing `id()` values — but CPython
  reuses the address of a freed object, so short-lived dicts collide and the
  test reported a false clash. It had to hold references to keep them alive.
- **A guard that was redundant, and one that only looked redundant.** Widening
  `_REPLAYABLE_TOOLS` to include `request_demo` and `run_code` changed nothing
  observable, because the replay loop keys on `concept_id` and skips them
  anyway — the code was safe by a *second* path, so the test could not see the
  first one being removed. The fix was to assert the allowlist itself. The
  reverse case appeared in the same pass: dropping the explicit
  `validate_demo_html` from `_rescue_draft` also changed nothing, but there the
  redundancy is real — `update_demo` validates internally — so it was left
  alone. Two identical-looking results, opposite conclusions.

**Rule.** A test that has never been seen to fail is a hypothesis, not a test.
Revert the fix and watch it go red — especially for a guard, where "the code
exists" and "the code runs" are different claims and only one of them is
usually verified. And when a mutation *doesn't* fail, find out whether the
safety survived by accident or by design before deciding which one you have.

### The corollary, stated by the codebase about itself

[`docs/sanskrit-tutor.md`](sanskrit-tutor.md) §9 puts it plainly:

> *Trust the code over `README.md`, which is stale. The inline comments are the
> opposite — most non-obvious decisions carry the failure that prompted them.*

That is measurably true as of this writing. The README describes **9 MCP tools
and 35 concepts**; the code registers **42 tool definitions** and
`CURRICULUM_GRAPH` holds **282 concepts** across 8 tracks. Meanwhile nearly
every lesson in this document came from a comment or a test docstring written
at the moment the bug was fixed.

The asymmetry is not an accident, and it is worth understanding rather than
just fixing. A README describes the system from outside and has to be
*remembered* at the moment something changes. A comment sits at the line that
would break and is *read* by whoever is about to change it. Documentation that
lives where the decision lives survives; documentation that lives elsewhere
decays at exactly the rate the code moves.

Which is the honest caveat on this file too: it will drift the same way. The
entries that will stay true are the **rules**, not the line numbers.
