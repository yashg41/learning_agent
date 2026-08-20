// PyMentor Demo pane — runs agent-built interactive demos beside the chat.
//
// The demo's HTML is injected into a sandboxed iframe via srcdoc, never into
// the page's own DOM. The frame gets `allow-scripts` WITHOUT
// `allow-same-origin`, which puts it in an opaque origin: demo JS can run, but
// cannot read localStorage, cookies, or call any /api/ route. That matters
// because this app has no auth — the email in every request path is the whole
// identity model — so a demo reaching the API would be reaching the learner's
// data.
//
// Loaded after app.js and notes.js; depends on escapeHtml, currentEmail,
// getEmail, and the pane-divider helpers from notes.js.

let demoOpen = false;
let demoIndex = [];
let activeDemoId = null;
let activeDemo = null;
// What the learner has highlighted inside the demo frame, if anything. Set by
// the frame's relay (see DEMO_ERROR_RELAY) — this document cannot read a
// selection inside a sandboxed, opaque-origin iframe.
let demoSelection = null;

// Frame zoom. The demo is sandboxed and opaque-origin, so we cannot set a zoom
// inside it — this scales the frame element from out here (see .demo-frame).
let demoZoom = 1;
const DEMO_ZOOM_MIN = 0.4;
const DEMO_ZOOM_MAX = 2.5;
// Multiply rather than add: +0.25 is a huge jump at 50% and a nudge at 250%.
const DEMO_ZOOM_RATIO = 1.2;

function demoPaneEl() { return document.getElementById("demo-pane"); }
function demoFrameEl() { return document.getElementById("demo-frame"); }

function demoUiKey() {
    const email = typeof getEmail === "function" ? getEmail() : "";
    return `pymentor:demo-ui:${email || "_anon"}`;
}

function saveDemoUi() {
    try {
        localStorage.setItem(demoUiKey(),
            JSON.stringify({ open: demoOpen, demoId: activeDemoId, zoom: demoZoom }));
    } catch (e) {}
}

function loadDemoUi() {
    try { return JSON.parse(localStorage.getItem(demoUiKey()) || "{}"); }
    catch (e) { return {}; }
}

function demoToast(msg, isError) {
    const host = document.getElementById("demo-body");
    if (!host) return;
    const prev = host.querySelector(".demo-toast");
    if (prev) prev.remove();
    const el = document.createElement("div");
    el.className = "demo-toast" + (isError ? " is-error" : "");
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => el.remove(), 3600);
}

// =====================================================================
// Data
// =====================================================================

async function loadDemoIndex() {
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email) { demoIndex = []; renderDemoPicker(); return; }
    try {
        const res = await fetch(`${API_BASE}/api/demos/${encodeURIComponent(email)}`);
        if (!res.ok) throw new Error(res.status);
        demoIndex = (await res.json()).demos || [];
    } catch (e) {
        demoIndex = [];
    }
    renderDemoPicker();
}

function renderDemoPicker() {
    const sel = document.getElementById("demo-picker");
    if (!sel) return;
    if (!demoIndex.length) {
        sel.innerHTML = `<option value="">No demos yet</option>`;
        return;
    }
    sel.innerHTML = demoIndex.map(d =>
        `<option value="${escapeHtml(d.id)}"${d.id === activeDemoId ? " selected" : ""}>` +
        `${escapeHtml(d.title)}</option>`).join("");
}

async function openDemo(demoId) {
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email || !demoId) return;
    try {
        const res = await fetch(
            `${API_BASE}/api/demos/${encodeURIComponent(email)}/${demoId}`);
        if (!res.ok) throw new Error(res.status);
        activeDemo = await res.json();
        activeDemoId = demoId;
    } catch (e) {
        demoToast("Could not open that demo", true);
        return;
    }
    renderDemo();
    renderDemoPicker();
    saveDemoUi();
}

async function onDemoPicked() {
    const sel = document.getElementById("demo-picker");
    if (sel && sel.value) await openDemo(sel.value);
}

async function deleteDemo() {
    if (!activeDemo) return;
    if (!confirm(`Delete "${activeDemo.title}"?`)) return;
    const email = getEmail();
    try {
        const res = await fetch(
            `${API_BASE}/api/demos/${encodeURIComponent(email)}/${activeDemoId}`,
            { method: "DELETE" });
        if (!res.ok) throw new Error(res.status);
    } catch (e) {
        demoToast("Delete failed", true);
        return;
    }
    activeDemo = null;
    activeDemoId = null;
    await loadDemoIndex();
    if (demoIndex.length) await openDemo(demoIndex[0].id);
    else renderDemo();
}

// =====================================================================
// Rendering
// =====================================================================

/**
 * Rebuild the iframe from scratch every time.
 *
 * Replacing the element rather than reassigning srcdoc guarantees the previous
 * demo's timers, animation frames and listeners are gone — a leftover
 * setInterval from the last demo would otherwise keep running and compete for
 * the main thread.
 */
function renderDemo() {
    const body = document.getElementById("demo-body");
    const empty = document.getElementById("demo-empty");
    const title = document.getElementById("demo-title");
    const summary = document.getElementById("demo-summary");
    if (!body) return;

    const old = demoFrameEl();
    if (old) old.remove();

    // The frame is being replaced, so any selection it reported belongs to a
    // document that no longer exists. Leaving it would let a highlight from
    // one demo ride along on a question about another.
    demoSelection = null;
    renderSelectionAsk();

    if (!activeDemo) {
        if (empty) {
            empty.style.display = "flex";
            empty.textContent = (typeof getEmail === "function" && getEmail())
                ? "Ask for a demo — e.g. \"show me how consistent hashing works\"."
                : "Set your email to use demos.";
        }
        if (title) title.textContent = "";
        if (summary) summary.textContent = "";
        syncDemoChat();
        return;
    }

    if (empty) empty.style.display = "none";
    if (title) title.textContent = activeDemo.title || "Demo";
    if (summary) summary.textContent = activeDemo.summary || "";

    const frame = document.createElement("iframe");
    frame.id = "demo-frame";
    frame.className = "demo-frame";
    // allow-scripts WITHOUT allow-same-origin: the two together would defeat
    // the sandbox entirely, because the frame could then reach into this
    // document and remove its own sandbox attribute.
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.srcdoc = withErrorRelay(activeDemo.html);
    frame.addEventListener("load", () => postThemeToDemo(frame));
    body.appendChild(frame);
    // The frame element is new on every render, so the scale has to be
    // re-asserted — it lives on .demo-body, but the readout and the
    // limit-disabled buttons still need refreshing.
    applyDemoZoom();
    syncDemoChat();
}

// Injected ahead of the demo's own scripts so it catches errors they throw at
// parse or run time. Fixed text with no interpolation — nothing from the demo
// or the app is concatenated in.
const DEMO_ERROR_RELAY = `<script>
(function () {
  function send(message, line) {
    try { parent.postMessage({ type: "demo-error", message: message, line: line }, "*"); }
    catch (e) {}
  }
  window.addEventListener("error", function (e) {
    send((e && e.message) || "script error", e && e.lineno);
  });
  window.addEventListener("unhandledrejection", function (e) {
    send("unhandled promise rejection: " + ((e.reason && e.reason.message) || e.reason), 0);
  });

  // Report what the learner highlighted, so they can ask the tutor about
  // "this bit". Only anchors travel out — the text is capped and the ids are
  // there so the tutor can locate the selection in the source. Drag-and-drop
  // could not do this job: a drag starting in here never escapes the frame's
  // opaque origin, and never fires from touch at all.
  function reportSelection() {
    try {
      var sel = document.getSelection();
      if (!sel || sel.isCollapsed) {
        parent.postMessage({ type: "demo-selection", text: "" }, "*");
        return;
      }
      var text = sel.toString().trim();
      if (!text) return;

      var ids = [];
      var tag = "";
      var node = sel.anchorNode;
      node = (node && node.nodeType === 1) ? node : (node && node.parentElement);
      if (node) tag = (node.tagName || "").toLowerCase();
      var hops = 0;
      while (node && hops < 6) {
        if (node.id) ids.push(node.id);
        node = node.parentElement;
        hops++;
      }
      parent.postMessage({
        type: "demo-selection",
        text: text.slice(0, 2000),
        tag: tag,
        ids: ids.slice(0, 5)
      }, "*");
    } catch (e) {}
  }
  // Screenshot on request. Text selection cannot reach a <canvas>, an <svg>
  // or a layout question, so the picture is the only way to show the tutor
  // what the learner is actually looking at. The frame captures itself
  // because its document is same-origin to itself — the PARENT cannot read
  // in, which is exactly the sandbox working as intended.
  window.addEventListener("message", function (e) {
    var d = e.data;
    if (!d || d.type !== "demo-capture") return;
    try {
      var canvases = document.querySelectorAll("canvas");
      if (canvases.length === 1) {
        var source = canvases[0];
        var out = source;

        // A crop rectangle, in CSS pixels relative to the canvas, sent when
        // the learner dragged a box instead of asking for the whole thing.
        var crop = d.crop;
        if (crop && crop.w > 4 && crop.h > 4) {
          var box = source.getBoundingClientRect();
          // crop arrives relative to the FRAME's viewport, but the canvas may
          // sit lower down the page, so subtract its offset first.
          var localX = crop.x - box.left;
          var localY = crop.y - box.top;
          // The backing store is often a different size from the displayed
          // element, so map CSS pixels onto device pixels before cropping.
          var sx = source.width / (box.width || 1);
          var sy = source.height / (box.height || 1);
          var cx = Math.max(0, Math.round(localX * sx));
          var cy = Math.max(0, Math.round(localY * sy));
          var cw = Math.min(source.width - cx, Math.round(crop.w * sx));
          var ch = Math.min(source.height - cy, Math.round(crop.h * sy));
          if (cw > 0 && ch > 0) {
            var cut = document.createElement("canvas");
            cut.width = cw;
            cut.height = ch;
            cut.getContext("2d").drawImage(source, cx, cy, cw, ch, 0, 0, cw, ch);
            out = cut;
          }
        }

        out.toBlob(function (blob) {
          if (!blob) { sendCaptureFailed("canvas produced no image"); return; }
          var reader = new FileReader();
          reader.onload = function () {
            parent.postMessage(
              { type: "demo-capture-result", dataUrl: reader.result }, "*");
          };
          reader.onerror = function () { sendCaptureFailed("could not read image"); };
          reader.readAsDataURL(blob);
        }, "image/png");
        return;
      }
      // No canvas (or several): nothing reliable to rasterise from in here
      // without a DOM-to-image library, which would be an external script the
      // sandbox blocks. Let the parent fall back.
      sendCaptureFailed(canvases.length ? "multiple canvases" : "no canvas");
    } catch (err) {
      sendCaptureFailed((err && err.message) || "capture failed");
    }
  });

  function sendCaptureFailed(why) {
    try {
      parent.postMessage({ type: "demo-capture-result", error: why }, "*");
    } catch (e) {}
  }

  document.addEventListener("mouseup", reportSelection);
  document.addEventListener("touchend", reportSelection);
  document.addEventListener("selectionchange", function () {
    var sel = document.getSelection();
    if (sel && sel.isCollapsed) {
      try { parent.postMessage({ type: "demo-selection", text: "" }, "*"); } catch (e) {}
    }
  });
})();
<\/script>`;

/**
 * Put the relay first in <head>, or at the very start if the demo has no head.
 *
 * Position matters: a script placed after the demo's own would miss anything
 * that throws while those parse.
 */
function withErrorRelay(html) {
    if (!html) return html;
    const head = html.match(/<head[^>]*>/i);
    if (head) {
        const at = head.index + head[0].length;
        return html.slice(0, at) + DEMO_ERROR_RELAY + html.slice(at);
    }
    const body = html.match(/<body[^>]*>/i);
    if (body) {
        const at = body.index + body[0].length;
        return html.slice(0, at) + DEMO_ERROR_RELAY + html.slice(at);
    }
    return DEMO_ERROR_RELAY + html;
}

/**
 * Tell the demo which theme to use.
 *
 * postMessage rather than string-injecting a class into the HTML: nothing from
 * the parent gets concatenated into the demo source, so there is no path for
 * markup injection, and the demo stays a static document we merely talk to.
 * targetOrigin is "*" because a sandboxed frame has an opaque origin that
 * cannot be named — safe here since the payload is one enum value.
 */
function postThemeToDemo(frame) {
    frame = frame || demoFrameEl();
    if (!frame || !frame.contentWindow) return;
    const theme = document.documentElement.getAttribute("data-theme") || "dark";
    try {
        frame.contentWindow.postMessage({ type: "theme", theme }, "*");
    } catch (e) {}
}

/** Re-mount the current demo — the escape hatch when one hangs or misbehaves. */
function reloadDemo() {
    if (activeDemo) renderDemo();
}

// =====================================================================
// Zoom
// =====================================================================

/**
 * Push the current zoom onto the frame and refresh the readout.
 *
 * The scale lives on .demo-body rather than the frame so it survives
 * renderDemo() replacing the frame element — otherwise switching demos or
 * hitting reload would silently snap back to 100%.
 */
function applyDemoZoom() {
    const body = document.getElementById("demo-body");
    if (body) body.style.setProperty("--demo-zoom", String(demoZoom));

    const label = document.getElementById("demo-zoom-level");
    if (label) label.textContent = Math.round(demoZoom * 100) + "%";

    // Grey out at the limits, so a button that has stopped responding looks
    // like it has stopped rather than like it's broken.
    const zin = document.getElementById("demo-zoom-in");
    const zout = document.getElementById("demo-zoom-out");
    if (zin) zin.disabled = demoZoom >= DEMO_ZOOM_MAX - 0.001;
    if (zout) zout.disabled = demoZoom <= DEMO_ZOOM_MIN + 0.001;
}

function setDemoZoom(next) {
    demoZoom = Math.max(DEMO_ZOOM_MIN, Math.min(DEMO_ZOOM_MAX, next));
    applyDemoZoom();
    saveDemoUi();
}

function demoZoomBy(dir) {
    const next = dir > 0 ? demoZoom * DEMO_ZOOM_RATIO : demoZoom / DEMO_ZOOM_RATIO;
    // Snap to exactly 1 when passing near it, so "back to 100%" is reachable
    // by button without overshooting to 99.6%.
    setDemoZoom(Math.abs(next - 1) < 0.06 ? 1 : next);
}

function demoZoomReset() { setDemoZoom(1); }

/**
 * Surface script errors from inside the demo.
 *
 * A sandboxed frame is opaque from out here: if the demo throws on load, its
 * buttons silently do nothing and the pane looks fine. The bootstrap injected
 * in renderDemo() forwards window.onerror to us, so a broken demo says so
 * instead of just being inert — and the message is specific enough for the
 * tutor to fix when asked.
 */
function initDemoErrorRelay() {
    window.addEventListener("message", (e) => {
        const d = e.data;
        if (!d) return;

        if (d.type === "demo-error") {
            const where = d.line ? ` (line ${d.line})` : "";
            demoToast(`Demo error: ${String(d.message).slice(0, 140)}${where}`, true);
            return;
        }

        if (d.type === "demo-selection") {
            // The frame is the only thing that can see its own selection —
            // it is a separate, opaque-origin document.
            demoSelection = d.text
                ? {
                    text: String(d.text).slice(0, 2000),
                    tag: String(d.tag || "").slice(0, 20),
                    ids: (Array.isArray(d.ids) ? d.ids : []).slice(0, 5).map(String),
                }
                : null;
            renderSelectionAsk();
        }
    });
}

/** Offer "ask about this" while something in the demo is highlighted. */
function renderSelectionAsk() {
    const bar = document.getElementById("demo-selection-bar");
    if (!bar) return;
    if (!demoSelection || !activeDemo) {
        bar.style.display = "none";
        return;
    }
    const label = document.getElementById("demo-selection-text");
    if (label) label.textContent = demoSelection.text.slice(0, 80);
    bar.style.display = "";
}

// =====================================================================
// Pane open/close
// =====================================================================

async function toggleDemo(force) {
    const pane = demoPaneEl();
    if (!pane) return;
    demoOpen = force !== undefined ? force : !demoOpen;
    pane.style.display = demoOpen ? "flex" : "none";

    const btn = document.getElementById("demo-btn");
    if (btn) btn.classList.toggle("is-on", demoOpen);

    if (!demoOpen) {
        // Destroy the frame on close: a running demo would otherwise keep
        // burning CPU in a pane nobody is looking at.
        const frame = demoFrameEl();
        if (frame) frame.remove();
        if (typeof syncPaneSplit === "function") syncPaneSplit();
        saveDemoUi();
        return;
    }

    // The three right-hand panes compete for the same column.
    if (typeof notesOpen !== "undefined" && notesOpen
        && typeof toggleNotes === "function") {
        await toggleNotes(false);
    }
    if (typeof notebookOpen !== "undefined" && notebookOpen
        && typeof toggleNotebook === "function") {
        toggleNotebook();
    }
    if (typeof syncPaneSplit === "function") syncPaneSplit();

    if (!demoIndex.length) await loadDemoIndex();

    if (!activeDemoId) {
        const ui = loadDemoUi();
        const target = (ui.demoId && demoIndex.some(d => d.id === ui.demoId))
            ? ui.demoId
            : (demoIndex[0] && demoIndex[0].id);
        if (target) await openDemo(target);
        else renderDemo();
    } else {
        renderDemo();
    }
    saveDemoUi();
}

/**
 * Called from the chat's tool_result stream when save_demo succeeds.
 *
 * The tool returns only {demo_id, title} — the HTML never travels through the
 * transcript — so the pane fetches the document separately.
 */
/**
 * Open the demo a chat tool-card refers to.
 *
 * Resolution happens server-side via /api/demos/{email}/resolve, which owns
 * the index and returns the document in one request. Doing it here would mean
 * fetching the whole index and re-implementing the match in the client, and
 * the two could then disagree about which demo a title refers to.
 */
async function openDemoByTitle(title, conceptId) {
    await toggleDemo(true);
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email || (!title && !conceptId)) return;

    const q = new URLSearchParams();
    if (title) q.set("title", title);
    if (conceptId) q.set("concept_id", conceptId);

    try {
        const res = await fetch(
            `${API_BASE}/api/demos/${encodeURIComponent(email)}/resolve?${q}`);
        if (res.status === 404) {
            demoToast("That demo is no longer saved", true);
            return;
        }
        if (!res.ok) throw new Error(res.status);
        activeDemo = await res.json();
        activeDemoId = activeDemo.id;
    } catch (e) {
        demoToast("Could not open that demo", true);
        return;
    }
    renderDemo();
    await loadDemoIndex();
    renderDemoPicker();
    saveDemoUi();
}

/**
 * Open a freshly-built demo.
 *
 * Called from the save_demo *tool call*, which the SDK emits as the tool
 * starts — there is no tool_result event for MCP tools to wait on. So the
 * demo is usually not on disk yet, and the index has to be polled briefly.
 *
 * Matching is by title when one is known: the index is newest-first, so
 * "first entry whose title matches" is the demo just built, even if an older
 * one shares the name.
 */
async function onDemoSaved(demoId, title, conceptId) {
    const before = activeDemoId;
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email) return;

    for (let attempt = 0; attempt < 12; attempt++) {
        await loadDemoIndex();

        let hit = null;
        if (demoId) {
            hit = demoIndex.find(d => d.id === demoId);
        } else if (title) {
            hit = demoIndex.find(d => d.title === title);
        } else if (demoIndex.length && demoIndex[0].id !== before) {
            // Nothing to match on: take the newest, but only once it is
            // actually new — otherwise we reopen what is already showing.
            hit = demoIndex[0];
        }

        if (hit && hit.id !== before) {
            await toggleDemo(true);
            await openDemo(hit.id);
            return;
        }
        // Backs off from ~150ms to ~1s. The demo is written in one atomic
        // replace, so it appears as soon as the tool returns.
        await new Promise(r => setTimeout(r, 150 + attempt * 80));
    }

    // Never appeared — most likely the tool rejected the HTML (external URL,
    // or over the size cap) and the model will explain in the chat.
    if (demoIndex.length) await toggleDemo(true);
}

// =====================================================================
// Build jobs
// =====================================================================
//
// A demo is built by a background agent, so the chat turn that requested it
// finishes in seconds and the composer never locks. The chip in the transcript
// is the only progress signal — deliberately not a toast or a tab badge: the
// right column is exclusive (Notes/Notebook/Demo), so any "View it" affordance
// would destroy whatever the learner is currently reading.

const trackedJobs = new Map();  // job_id -> chip element
let jobPollTimer = null;

const JOB_POLL_MS = 2000;
const JOB_TERMINAL = ["ready", "failed", "cancelled"];
// ~1 minute of consecutive failures before giving up on a build we can no
// longer reach. Generous, because a build legitimately outlives a backend
// restart and we would rather re-attach than abandon it.
const JOB_POLL_MAX_FAILURES = 30;
let pollFailures = 0;

/** Watch a job until it finishes, updating its chip. */
function trackDemoJob(jobId, chipEl) {
    if (!jobId || !chipEl) return;
    trackedJobs.set(jobId, chipEl);
    if (!jobPollTimer) {
        // Fresh watch — an earlier build that gave up must not count against
        // this one.
        pollFailures = 0;
        jobPollTimer = setInterval(pollDemoJobs, JOB_POLL_MS);
    }
}

function stopJobPolling() {
    if (jobPollTimer) {
        clearInterval(jobPollTimer);
        jobPollTimer = null;
    }
}

async function pollDemoJobs() {
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email || trackedJobs.size === 0) {
        stopJobPolling();
        return;
    }

    let jobs;
    try {
        const res = await fetch(`${API_BASE}/api/demo-jobs/${encodeURIComponent(email)}`);
        if (!res.ok) throw new Error(res.status);
        jobs = (await res.json()).jobs || [];
        pollFailures = 0;
    } catch {
        // Transient failures are normal (a backend restart mid-build), so retry
        // — but give up eventually. Returning unconditionally here meant a
        // backend that stayed down kept this interval alive for the life of the
        // tab. resumeDemoJobs() re-attaches on the next reload or session load.
        if (++pollFailures >= JOB_POLL_MAX_FAILURES) {
            trackedJobs.clear();
            stopJobPolling();
        }
        return;
    }

    let indexStale = false;
    const byId = new Map(jobs.map(j => [j.job_id, j]));

    // Reconcile against what we are tracking, NOT against what came back. A job
    // can stop appearing in the response entirely — the server reaps terminal
    // records after JOB_TTL_SECONDS, and a backend restart empties the
    // in-memory table — and iterating the response alone would never drop it.
    // That left trackedJobs permanently non-empty and this 2s interval running
    // for the life of the tab.
    for (const [jobId, chip] of [...trackedJobs]) {
        const job = byId.get(jobId);

        // Gone from the server: reaped or lost on restart. Either way the build
        // is over and there is nothing left to watch. Whatever the chip last
        // painted is the final word — a build that really did finish was
        // already rendered on an earlier tick.
        if (!job) {
            trackedJobs.delete(jobId);
            continue;
        }

        // Switching sessions replaces the transcript wholesale, so a tracked
        // chip can be a detached node. Painting it would be invisible work,
        // and holding it would keep the element alive. resumeDemoJobs
        // re-attaches when the session is loaded again.
        if (!chip.isConnected) {
            trackedJobs.delete(jobId);
            continue;
        }

        renderJobChip(chip, job);

        if (JOB_TERMINAL.includes(job.state)) {
            trackedJobs.delete(jobId);
            if (job.state === "ready") indexStale = true;
        }
    }

    // One refresh for the batch, so the picker lists whatever just landed.
    if (indexStale) await loadDemoIndex();
    if (trackedJobs.size === 0) stopJobPolling();
}

/** Paint a chip for the job's current state. */
function renderJobChip(chip, job) {
    if (!chip || chip.dataset.jobState === job.state) return;
    chip.dataset.jobState = job.state;
    chip.classList.remove("is-building", "is-ready", "is-failed");

    const summary = chip.querySelector(".tool-summary");
    const status = chip.querySelector(".demo-chip-status");
    const openBtn = chip.querySelector(".demo-open-btn");

    if (job.state === "queued" || job.state === "building") {
        chip.classList.add("is-building");
        if (status) status.textContent = job.state === "queued" ? "queued…" : "building…";
        if (openBtn) openBtn.style.display = "none";
        return;
    }

    if (job.state === "ready") {
        chip.classList.add("is-ready");
        if (status) status.textContent = "";
        if (summary && job.title) summary.textContent = job.title;
        if (openBtn && job.demo_id) {
            openBtn.style.display = "";
            openBtn.onclick = (e) => {
                e.stopPropagation();
                openDemoById(job.demo_id);
            };
        }
        // One-time pulse: loud enough to catch the eye in a static transcript,
        // quiet enough not to interrupt reading. The pane is NOT auto-opened —
        // the demo can land several messages later, and stealing the column
        // mid-sentence is the disruption this design exists to avoid.
        chip.classList.add("just-finished");
        setTimeout(() => chip.classList.remove("just-finished"), 2400);
        return;
    }

    // failed / cancelled
    chip.classList.add("is-failed");
    if (openBtn) openBtn.style.display = "none";
    if (status) {
        status.textContent = job.state === "cancelled"
            ? "cancelled"
            : (job.error || "failed").split("\n")[0].substring(0, 80);
    }
}

/** Open a demo by id, bringing the pane up. */
async function openDemoById(demoId) {
    await toggleDemo(true);
    await loadDemoIndex();
    await openDemo(demoId);
}

/** Re-attach to jobs still running after a page refresh.
 *
 * A build outlives the request that started it, so after a reload the chips
 * are replayed from the transcript but nothing is watching them. The job list
 * is the source of truth for what is still in flight.
 */
async function resumeDemoJobs() {
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email) return;

    let jobs;
    try {
        const res = await fetch(`${API_BASE}/api/demo-jobs/${encodeURIComponent(email)}`);
        if (!res.ok) return;
        jobs = (await res.json()).jobs || [];
    } catch {
        return;
    }

    for (const job of jobs) {
        const chip = document.querySelector(`[data-job-id="${job.job_id}"]`);
        if (!chip) continue;
        renderJobChip(chip, job);
        if (!JOB_TERMINAL.includes(job.state)) trackDemoJob(job.job_id, chip);
    }
}

// =====================================================================
// Talking to the builder directly
// =====================================================================
//
// Two routes reach the same builder: asking the tutor ("add residuals to that
// demo"), or typing here. Both create a demo job against the demo's own
// builder session, so an edit is incremental either way.

function demoChatEl() { return document.getElementById("demo-chat"); }
function demoChatLogEl() { return document.getElementById("demo-chat-log"); }

function demoChatNote(text, kind) {
    const log = demoChatLogEl();
    if (!log) return;
    const line = document.createElement("div");
    line.className = `demo-chat-line${kind ? " is-" + kind : ""}`;
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

// Greetings and similar chatter, which reach here by habit rather than intent.
// Deliberately a short, exact-match list: anything longer or unrecognised is
// treated as a real instruction, because wrongly refusing an edit is far worse
// than occasionally letting a wasted build through.
const NOT_AN_EDIT = new Set([
    "hi", "hi?", "hii", "hey", "hello", "yo", "sup", "test", "testing",
    "ok", "okay", "k", "thanks", "thank you", "ty", "cool", "nice",
    "?", "??", "help", "what", "huh",
]);

function isNotAnEditInstruction(text) {
    const normalised = text.toLowerCase().replace(/[!.]+$/, "").trim();
    return NOT_AN_EDIT.has(normalised);
}

/** Show or hide the composer depending on whether a demo is loaded. */
function syncDemoChat() {
    const box = demoChatEl();
    if (!box) return;
    box.style.display = activeDemo ? "" : "none";
}

async function sendDemoEdit() {
    const input = document.getElementById("demo-chat-text");
    const sendBtn = document.getElementById("demo-chat-send");
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!input || !activeDemo || !email) return;

    const request = input.value.trim();
    if (!request) return;

    // This box edits the demo; it is not a conversation. A greeting here would
    // otherwise spend a full agent run (~30s and an API call) on a build that
    // correctly changes nothing, and the tutor in the main chat is the right
    // place to actually talk.
    if (isNotAnEditInstruction(request)) {
        demoChatNote(request, "user");
        demoChatNote(
            "This box changes the demo — try “add a reset button” or " +
            "“make the slider go to 0.5”. To chat, use the main conversation.",
            "status",
        );
        input.value = "";
        return;
    }

    input.value = "";
    input.disabled = true;
    if (sendBtn) sendBtn.disabled = true;
    demoChatNote(request, "user");
    demoChatNote("building…", "status");

    try {
        const res = await fetch(
            `${API_BASE}/api/demos/${encodeURIComponent(email)}/${activeDemo.id}/edit`,
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ request }),
            },
        );
        if (!res.ok) throw new Error(`server returned ${res.status}`);
        const job = await res.json();
        await watchDemoEditJob(email, job.job_id);
    } catch (e) {
        demoChatNote(`Could not start the edit: ${e.message}`, "error");
    } finally {
        input.disabled = false;
        if (sendBtn) sendBtn.disabled = false;
        input.focus();
    }
}

/** Poll one edit job and reload the demo in place when it lands. */
async function watchDemoEditJob(email, jobId) {
    for (let attempt = 0; attempt < 240; attempt++) {
        await new Promise(r => setTimeout(r, 2000));

        let job;
        try {
            const res = await fetch(
                `${API_BASE}/api/demo-jobs/${encodeURIComponent(email)}/${jobId}`);
            if (!res.ok) continue;
            job = await res.json();
        } catch {
            continue;
        }

        if (job.state === "ready") {
            const keepId = job.demo_id || (activeDemo && activeDemo.id);
            const priorVersion = activeDemo && activeDemo.version;
            await loadDemoIndex();
            await openDemo(keepId);   // rebuilds the frame with the new html
            // Report what actually changed rather than assuming. The builder
            // can finish without editing anything, and claiming "Updated" for
            // an untouched demo is worse than saying nothing happened.
            const now = activeDemo && activeDemo.version;
            if (priorVersion && now && now > priorVersion) {
                demoChatNote(`Updated — now v${now}.`, "ok");
            } else {
                demoChatNote("Finished, but the demo was not changed.", "status");
            }
            return;
        }
        if (job.state === "failed" || job.state === "cancelled") {
            demoChatNote(job.error || job.state, "error");
            return;
        }
    }
    demoChatNote("Still building — check back shortly.", "status");
}

/** Drag a box over the demo, then send only that region.
 *
 * The overlay sits in THIS document, above the frame — the frame never sees
 * the drag, so a demo that handles its own mouse events (dragging graph
 * nodes, for instance) is not disturbed while picking a region.
 */
function startRegionCapture() {
    const body = document.getElementById("demo-body");
    const frame = demoFrameEl();
    if (!body || !frame || !activeDemo) return;

    const existing = document.getElementById("demo-crop-layer");
    if (existing) existing.remove();

    const layer = document.createElement("div");
    layer.className = "demo-crop-layer";
    layer.id = "demo-crop-layer";
    layer.innerHTML =
        '<div class="demo-crop-hint">Drag a box around what you want to ask about — Esc to cancel</div>' +
        '<div class="demo-crop-box" style="display:none"></div>';
    body.appendChild(layer);

    const box = layer.querySelector(".demo-crop-box");
    let startX = 0;
    let startY = 0;
    let dragging = false;

    const cleanup = () => {
        layer.remove();
        document.removeEventListener("keydown", onKey);
    };
    const onKey = (e) => {
        if (e.key === "Escape") cleanup();
    };
    document.addEventListener("keydown", onKey);

    // Pointer events so a tablet works the same as a mouse — the reason
    // HTML5 drag-and-drop was not used anywhere in this feature.
    layer.addEventListener("pointerdown", (e) => {
        const rect = layer.getBoundingClientRect();
        startX = e.clientX - rect.left;
        startY = e.clientY - rect.top;
        dragging = true;
        layer.setPointerCapture(e.pointerId);
        box.style.display = "";
        box.style.left = `${startX}px`;
        box.style.top = `${startY}px`;
        box.style.width = "0px";
        box.style.height = "0px";
    });

    layer.addEventListener("pointermove", (e) => {
        if (!dragging) return;
        const rect = layer.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        box.style.left = `${Math.min(startX, x)}px`;
        box.style.top = `${Math.min(startY, y)}px`;
        box.style.width = `${Math.abs(x - startX)}px`;
        box.style.height = `${Math.abs(y - startY)}px`;
    });

    layer.addEventListener("pointerup", async (e) => {
        if (!dragging) return;
        dragging = false;
        const rect = layer.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        cleanup();

        // Check the drag on screen, before converting: the threshold is about
        // "did they actually drag a box", which is a question about the
        // pointer, not about the demo's internal coordinate space.
        if (Math.abs(x - startX) < 5 || Math.abs(y - startY) < 5) {
            demoToast("Region too small — drag a larger box");
            return;
        }

        // The box is dragged in on-screen pane pixels, but it is consumed
        // inside the frame, whose viewport is 1/zoom larger before the scale is
        // applied. Divide, or a crop taken while zoomed lands on the wrong part
        // of the demo.
        const z = demoZoom || 1;
        const crop = {
            x: Math.min(startX, x) / z,
            y: Math.min(startY, y) / z,
            w: Math.abs(x - startX) / z,
            h: Math.abs(y - startY) / z,
        };
        await screenshotDemoForChat(crop);
    });
}

/** Attach a picture of the demo to the next tutor message.
 *
 * Selection only reaches text, so a canvas demo (a graph, a chart, anything
 * drawn) cannot be pointed at any other way. The frame screenshots itself and
 * posts the PNG out — the parent cannot read into a sandboxed frame, which is
 * the sandbox doing its job.
 *
 * The result rides the ordinary image-attachment path, so it inherits the
 * size cap, the thumbnail, and the model's existing ability to read images.
 */
async function screenshotDemoForChat(crop) {
    const frame = demoFrameEl();
    const chat = typeof getMainChat === "function" ? getMainChat() : null;
    if (!frame || !frame.contentWindow || !activeDemo || !chat) return;

    const dataUrl = await new Promise((resolve) => {
        let settled = false;
        const done = (value) => {
            if (settled) return;
            settled = true;
            window.removeEventListener("message", onMessage);
            resolve(value);
        };
        const onMessage = (e) => {
            const d = e.data;
            if (!d || d.type !== "demo-capture-result") return;
            done(d.error ? null : d.dataUrl);
        };
        window.addEventListener("message", onMessage);
        frame.contentWindow.postMessage(
            { type: "demo-capture", crop: crop || null }, "*");
        // A demo that never answers must not hang the button forever.
        setTimeout(() => done(null), 3000);
    });

    if (!dataUrl) {
        // No canvas to rasterise. Rendering arbitrary DOM to an image needs a
        // library the sandbox would block as an external script — but a
        // DOM demo is exactly the case where selection works, and the digest
        // already describes its structure. So stage the reference and say so
        // rather than leaving the learner with nothing.
        askTutorAboutDemo();
        demoToast("This demo has no drawing to capture — its structure is attached instead");
        return;
    }

    const blob = await (await fetch(dataUrl)).blob();
    const name = `${(activeDemo.title || "demo").replace(/[^\w-]+/g, "-").slice(0, 40)}.png`;
    await chat.addAttachments([new File([blob], name, { type: "image/png" })]);

    // The picture shows what it looks like; the reference explains how it is
    // built. Together they answer "why does this look wrong".
    chat.setDemoRef({
        demo_id: activeDemo.id,
        title: activeDemo.title || "demo",
        selection: demoSelection || null,
    });

    demoToast("Screenshot attached — type your question in the chat");
    const input = document.getElementById("message-input");
    if (input) input.focus();
}

/** Stage this demo (and any highlighted part) for the next tutor message.
 *
 * The reference reaches the model as prompt text, assembled server-side from
 * the demo_id — the client never decides what the model sees, and the demo's
 * html is never sent. See run_agent's demo_ref.
 */
function askTutorAboutDemo() {
    if (!activeDemo) return;
    const chat = typeof getMainChat === "function" ? getMainChat() : null;
    if (!chat) return;

    chat.setDemoRef({
        demo_id: activeDemo.id,
        title: activeDemo.title || "demo",
        selection: demoSelection || null,
    });

    const what = demoSelection
        ? `“${demoSelection.text.slice(0, 40)}”`
        : activeDemo.title || "this demo";
    demoToast(`Asking about ${what} — type your question in the chat`);

    const input = document.getElementById("message-input");
    if (input) {
        input.focus();
        input.scrollIntoView({ block: "nearest" });
    }
}

/** Leave a visible bookmark card for this demo in the transcript.
 *
 * For the LEARNER, not the model: the turn goes to conversation.jsonl, which
 * the model never reads. To put a demo in front of the tutor use
 * askTutorAboutDemo, which sends a digest in the prompt.
 *
 * Non-terminal, unlike the side-chat's "End & summarize": the demo stays open
 * and keeps its builder session.
 */
async function pinDemoToChat() {
    const email = typeof getEmail === "function" ? getEmail() : "";
    const sessionId = typeof currentSessionId !== "undefined" ? currentSessionId : null;
    if (!activeDemo || !email || !sessionId) {
        demoToast("Open a chat session first", true);
        return;
    }

    try {
        const res = await fetch(
            `${API_BASE}/api/sessions/${encodeURIComponent(email)}/${sessionId}/pin-demo`,
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ demo_id: activeDemo.id }),
            },
        );
        if (!res.ok) throw new Error(`server returned ${res.status}`);
        const turn = await res.json();
        if (typeof renderDemoPinCard === "function") renderDemoPinCard(turn);
        demoToast("Added to the conversation");
    } catch (e) {
        demoToast(`Could not add it: ${e.message}`, true);
    }
}

// =====================================================================
// Init
// =====================================================================

/** Drop every trace of the previous learner's demos.
 *
 * Demos are per-user on disk, but this module's state is not: the pane, the
 * picker, the selection and the job poller all belong to one identity. Without
 * this, changing the email left the previous learner's demo rendered in the
 * frame, their titles in the picker, and — worst — the poller running against
 * their job ids while fetching the NEW user's job list, so those chips stuck
 * at "building…" forever and the interval never stopped.
 *
 * Mirrors the notesOnSessionChange convention in notes.js.
 */
function demoOnEmailChange() {
    stopJobPolling();
    trackedJobs.clear();

    activeDemo = null;
    activeDemoId = null;
    demoSelection = null;
    demoIndex = [];

    // Zoom is a per-user preference; initDemoPane restores it from the
    // email-keyed localStorage entry, so reset to the default here rather
    // than carrying the previous learner's setting across.
    demoZoom = 1;
    applyDemoZoom();

    // A half-finished region capture belongs to the demo that is going away.
    const cropLayer = document.getElementById("demo-crop-layer");
    if (cropLayer) cropLayer.remove();

    renderSelectionAsk();
    renderDemo();          // destroys the iframe
    renderDemoPicker();

    // Repopulate for whoever is here now, then re-attach to their live builds.
    loadDemoIndex().then(() => resumeDemoJobs());
}

function initDemoPane() {
    const pane = demoPaneEl();
    if (!pane) return;
    if (pane.dataset.demoInit === "1") return;
    pane.dataset.demoInit = "1";

    // Follow the app's theme toggle while a demo is open.
    new MutationObserver(() => {
        if (demoOpen && activeDemo) postThemeToDemo();
    }).observe(document.documentElement, {
        attributes: true, attributeFilter: ["data-theme"],
    });

    initDemoErrorRelay();

    const ui = loadDemoUi();
    // Restore before opening, so the pane never renders at 100% and then jumps.
    if (Number.isFinite(ui.zoom)) {
        demoZoom = Math.max(DEMO_ZOOM_MIN, Math.min(DEMO_ZOOM_MAX, ui.zoom));
    }
    applyDemoZoom();
    if (ui.open) toggleDemo(true);

    // Pick up builds that were still running when the page was last closed.
    resumeDemoJobs();
}
