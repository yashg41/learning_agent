# Demo patterns

Four shapes that cover most teachable concepts. Pick the one whose *shape*
matches the confusion, not the topic — "where does my key land" is a ring
question whether it is about hashing or clock arithmetic.

Each pattern lists the readout that exposes the mechanism, which is the part
that actually teaches. Without it you have drawn a picture.

---

## 1. Ring / wheel — "where does this land, and what moves"

**Fits:** consistent hashing, modular arithmetic, round-robin, clock/vector
clocks, Bloch sphere angles, circular buffers.

**Stage:** an inline SVG circle. Nodes as coloured arcs or dots on the
circumference; items as small markers. Position = `(value / space) * 360°`.

**Controls:** add/remove a node, add an item, reset.

**Readout — the critical part.** Show the whole derivation for one item:

```
user_42  →  hash 0x8f3a2b  →  2398934 mod 360 = 201°
         →  walks clockwise → lands on Shard B
```

And on every change, what it cost:

```
Added Shard D:  12 of 100 keys moved  (12%)
Naive mod-N would have moved:  74 keys  (74%)
```

That comparison is usually the entire lesson. Show it.

**Trap:** do not animate the walk so slowly that they lose the thread. A
highlighted arc plus the numbers beats a two-second animation.

---

## 2. Step-through — "what happens in what order"

**Fits:** TCP handshake, TLS negotiation, Raft elections, two-phase commit,
garbage collection phases, request lifecycles.

**Stage:** two or more columns (client / server), messages as arrows between
them. Only draw arrows up to the current step.

**Controls:** Next / Back / Reset. Let them go **backwards** — that is when the
"wait, why did that happen" moment gets resolved.

**Readout:** current state of each participant, not just the message in flight:

```
Step 2 of 4:  SYN-ACK
  Client:  SYN_SENT      seq=1000  ack=—
  Server:  SYN_RECEIVED  seq=5000  ack=1001
```

**Trap:** an auto-playing animation. Learners need to sit on the step that
confused them. Manual stepping only.

---

## 3. Before / after — "why is this one better"

**Fits:** index vs table scan, cache hit vs miss, N+1 queries, normalised vs
denormalised, sync vs async.

**Stage:** two panes side by side running the *same* input, so the difference
is attributable to the approach and nothing else.

**Controls:** one shared input (row count, cache size) affecting both.

**Readout:** the same metrics for both, plus the ratio:

```
             without index    with index
rows read         100,000            14
comparisons       100,000            17
                              → 7000x fewer
```

**Trap:** rigging it. If the naive approach wins at small N, show that — "below
~50 rows the scan is faster" is a genuinely useful thing to learn, and hiding
it makes the demo a lie.

---

## 4. Parameter sweep — "how sensitive is this"

**Fits:** hash collisions vs table size, Bloom filter false positives, cache
hit rate vs capacity, thread pool sizing, quantum rotation angles.

**Stage:** a slider plus a plot (SVG polyline is enough) with a marker at the
current value.

**Controls:** the slider. Label both ends with what they mean, not just numbers.

**Readout:** current value, the outcome, and the interesting boundary:

```
Filter size:  1024 bits,  3 hashes,  200 items
False positive rate:  2.1%
Doubling to 2048 bits →  0.4%
Sweet spot for <1%:    ~1400 bits
```

**Trap:** a slider with no plot. The shape of the curve is the insight — where
it is flat and where it falls off a cliff.

---

## Things that break demos here

- **Any external URL.** No CDN, no Google Fonts, no remote images. The sandbox
  has no network; `save_demo` rejects these with the offending URL quoted.
- **`localStorage`, cookies, `fetch`.** The frame runs in an opaque origin, so
  these throw. Keep all state in JS variables.
- **`alert()` / `prompt()`.** Blocked in a sandboxed frame. Render messages
  into the page instead.
- **Runaway loops.** The frame shares the page's main thread, so an infinite
  loop freezes the whole app. Bound every loop; prefer
  `requestAnimationFrame` over `while` for anything continuous.
- **Assuming a fixed width.** The pane is resizable and can be narrow. Use
  `viewBox` on SVG and percentage widths; never hardcode `width: 900px`.
