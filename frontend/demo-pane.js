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

function demoPaneEl() { return document.getElementById("demo-pane"); }
function demoFrameEl() { return document.getElementById("demo-frame"); }

function demoUiKey() {
    const email = typeof getEmail === "function" ? getEmail() : "";
    return `pymentor:demo-ui:${email || "_anon"}`;
}

function saveDemoUi() {
    try {
        localStorage.setItem(demoUiKey(),
            JSON.stringify({ open: demoOpen, demoId: activeDemoId }));
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

    if (!activeDemo) {
        if (empty) {
            empty.style.display = "flex";
            empty.textContent = (typeof getEmail === "function" && getEmail())
                ? "Ask for a demo — e.g. \"show me how consistent hashing works\"."
                : "Set your email to use demos.";
        }
        if (title) title.textContent = "";
        if (summary) summary.textContent = "";
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
        if (!d || d.type !== "demo-error") return;
        const where = d.line ? ` (line ${d.line})` : "";
        demoToast(`Demo error: ${String(d.message).slice(0, 140)}${where}`, true);
    });
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
// Init
// =====================================================================

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
    if (ui.open) toggleDemo(true);
}
