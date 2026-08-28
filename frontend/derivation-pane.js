// =====================================================================
// Derivation pane — read-only mathematics beside the chat.
//
// A derivation is an ordered list of typed blocks rendered top to bottom.
// Deliberately NOT a canvas: there is no x/y, no drag, no z-order, because a
// derivation is a sequence and giving it free positioning would only create
// layout work for something that reads in one direction anyway.
//
// Not an iframe either, unlike demos. A demo is model-written HTML and has to
// be sandboxed; this renders a structured document whose renderer lives here,
// so it can be a normal same-origin pane and reuse the page's KaTeX.
//
// The security tradeoff that buys is real: block content is stored on the
// server and re-rendered on every load, which makes it a stored-XSS surface
// the chat transcript is not. So every string is escaped before it reaches the
// DOM, and math spans are held out and typeset afterwards — the same two-pass
// trick renderMarkdown uses in app.js.
// =====================================================================

let derivIndex = [];
let activeDerivId = null;
let activeDeriv = null;
let derivOpen = false;
let derivJobTimer = null;

const DERIV_JOB_POLL_MS = 1500;
const DERIV_UI_KEY = "pymentor:derivation-ui";

function derivPaneEl() { return document.getElementById("derivation-pane"); }
function derivBodyEl() { return document.getElementById("derivation-body"); }

function derivEmail() {
    return typeof getEmail === "function" ? getEmail() : "";
}

function saveDerivUi() {
    try {
        localStorage.setItem(DERIV_UI_KEY, JSON.stringify({
            open: derivOpen, docId: activeDerivId,
        }));
    } catch (e) {}
}

function loadDerivUi() {
    try { return JSON.parse(localStorage.getItem(DERIV_UI_KEY) || "{}"); }
    catch (e) { return {}; }
}

// --- Rendering -------------------------------------------------------

function derivEscape(s) {
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// A $$…$$ span, for holding math out of the markdown parser.
const DV_MATH_SPAN_RE = /\$\$[\s\S]+?\$\$/g;

/**
 * Drop delimiters from content the pane is about to wrap itself.
 *
 * `latex` blocks and derivation `expr` fields hold a bare formula by contract,
 * but a model that has just written `$$x$$` in a text block will sometimes
 * carry the habit across. Wrapping that again renders a literal $$.
 */
function stripMathDelimiters(expr) {
    let e = String(expr == null ? "" : expr).trim();
    for (const pair of [["$$", "$$"], ["\\[", "\\]"], ["\\(", "\\)"], ["$", "$"]]) {
        if (e.startsWith(pair[0]) && e.endsWith(pair[1]) && e.length > pair[0].length) {
            e = e.slice(pair[0].length, -pair[1].length).trim();
            break;
        }
    }
    return e;
}

/**
 * Rewrite single-$ math to $$, which is the only delimiter that renders.
 *
 * The builder is told to use $$, but it runs on a small model and `$…$` is the
 * LaTeX habit every corpus teaches — when it slips, the formula reaches the
 * learner as raw source, which is exactly what this pane exists to avoid. So
 * the rule is enforced here as well as asked for in the prompt.
 *
 * Pairs are matched left to right and a lone trailing $ is left alone, so a
 * price ("$5") or an unbalanced fragment cannot swallow the rest of the text.
 */
function normalizeMathDelimiters(text) {
    if (!text || text.indexOf("$") === -1) return text || "";
    // Split on $$ first so existing spans pass through untouched; only the
    // parts BETWEEN them can contain single-$ math.
    return text.split(/(\$\$[\s\S]+?\$\$)/g).map(part => {
        if (part.startsWith("$$")) return part;
        return part.replace(/\$([^$\n]+?)\$/g, (_, body) => `$$${body}$$`);
    }).join("");
}

/**
 * Markdown for a text block, with math held out of the parser.
 *
 * Escape-then-parse, as notes.js does and for the same reason: block content is
 * stored on the server and re-rendered on every load, which makes it a
 * stored-XSS surface the chat transcript is not.
 *
 * Math comes out before the escape and goes back after the parse — otherwise
 * markdown reads `\beta_0 … \beta_1` as an emphasis pair and splits the formula
 * across <em> tags, which is the bug app.js documents at renderMarkdown.
 */
function renderDerivText(content) {
    const normalized = normalizeMathDelimiters(content);

    const spans = [];
    const held = normalized.replace(DV_MATH_SPAN_RE, (m) => {
        spans.push(m);
        return `x0dvmath${spans.length - 1}endx0`;
    });

    let html = derivEscape(held);
    if (typeof marked !== "undefined") {
        try {
            html = marked.parse(html, { breaks: true, gfm: true });
        } catch (e) {
            html = html.replace(/\n/g, "<br>");
        }
    } else {
        html = html.replace(/\n/g, "<br>");
    }

    if (spans.length) {
        // Reinserted raw: KaTeX needs the real delimiters, and the span was
        // never user-controlled markup — it is math the pane is about to
        // typeset, and typesetDeriv replaces it wholesale.
        html = html.replace(/x0dvmath(\d+)endx0/g, (_, i) =>
            derivEscape(spans[Number(i)]));
    }
    return html;
}

/**
 * Render one block to HTML.
 *
 * Everything is escaped here. Math is left as literal $$...$$ inside the
 * escaped text and typeset by typesetDeriv() after insertion, so a formula
 * never has to arrive as trusted markup.
 */
function renderDerivBlock(b, docId, email) {
    docId = docId || activeDerivId;
    email = email || derivEmail();
    switch (b.kind) {
        case "text":
            return `<div class="dv-text">${renderDerivText(b.content)}</div>`;

        case "latex":
            // The content IS the formula, so the pane supplies the delimiters.
            // Stripped first in case the builder wrapped it anyway — doubling
            // them would leave a literal $$ on screen.
            return `<div class="dv-latex">$$${derivEscape(stripMathDelimiters(b.content))}$$</div>`;

        case "derivation":
            return renderDerivSteps(b);

        case "matrix":
            return renderDerivMatrix(b);

        case "table":
            return renderDerivTable(b);

        case "plot":
            return renderDerivPlot(b, docId, email);

        default:
            return "";
    }
}

function renderDerivSteps(b) {
    const steps = (b.steps || []).map((s, i) => {
        // Three states, and they must look different: a step nobody could
        // check is not the same claim as a step sympy said is wrong.
        let badge = `<span class="dv-badge dv-unchecked" title="${derivEscape(s.check || "not checked")}">?</span>`;
        if (s.verified === true) {
            badge = `<span class="dv-badge dv-ok" title="${derivEscape(s.check || "verified")}">✓</span>`;
        } else if (s.verified === false) {
            badge = `<span class="dv-badge dv-bad" title="${derivEscape(s.check || "does not follow")}">✕</span>`;
        } else if (s.check === "premise") {
            badge = `<span class="dv-badge dv-premise" title="starting point">•</span>`;
        }
        // A reason often names a symbol ("the $$\sigma'$$ term cancels"), so it
        // goes through the same normalize-and-typeset path as prose.
        const reason = s.reason
            ? `<div class="dv-reason">${renderDerivText(s.reason)}</div>` : "";
        return `<li class="dv-step">
            ${badge}
            <div class="dv-step-body">
              <div class="dv-expr">$$${derivEscape(stripMathDelimiters(s.expr))}$$</div>
              ${reason}
            </div>
        </li>`;
    }).join("");

    const title = b.title
        ? `<div class="dv-block-title">${derivEscape(b.title)}</div>` : "";
    return `<div class="dv-derivation">${title}<ol class="dv-steps">${steps}</ol></div>`;
}

function renderDerivMatrix(b) {
    const rows = (b.rows || []).map(r =>
        `<tr>${r.map(c => `<td>${derivEscape(c)}</td>`).join("")}</tr>`
    ).join("");
    const label = b.label
        ? `<div class="dv-block-title">${derivEscape(b.label)}</div>` : "";
    const ops = (b.ops || []).length
        ? `<ul class="dv-ops">${b.ops.map(o =>
            `<li>${derivEscape(o)}</li>`).join("")}</ul>`
        : "";
    return `<div class="dv-matrix">${label}
        <div class="dv-matrix-wrap"><table class="dv-matrix-table">${rows}</table></div>
        ${ops}</div>`;
}

function renderDerivTable(b) {
    const head = (b.headers || []).length
        ? `<thead><tr>${b.headers.map(h =>
            `<th>${derivEscape(h)}</th>`).join("")}</tr></thead>` : "";
    const body = (b.rows || []).map(r =>
        `<tr>${r.map(c => `<td>${derivEscape(c)}</td>`).join("")}</tr>`
    ).join("");
    const cap = b.caption
        ? `<div class="dv-caption">${derivEscape(b.caption)}</div>` : "";
    // Wide tables scroll inside their own box rather than widening the pane.
    return `<div class="dv-table"><div class="dv-table-wrap">
        <table>${head}<tbody>${body}</tbody></table></div>${cap}</div>`;
}

// docId is threaded explicitly rather than read off activeDerivId, so the same
// renderer serves both the pane and a ```derivation: fence in the chat.
function renderDerivPlot(b, docId, email) {
    const src = `/api/derivations/${encodeURIComponent(email)}`
              + `/${encodeURIComponent(docId)}`
              + `/assets/${encodeURIComponent(b.src)}`;
    const cap = b.caption
        ? `<div class="dv-caption">${derivEscape(b.caption)}</div>` : "";
    return `<figure class="dv-plot">
        <img src="${derivEscape(src)}" alt="${derivEscape(b.caption || "plot")}" loading="lazy">
        ${cap}</figure>`;
}

/**
 * Render a whole derivation as HTML, for a ```derivation: fence in the chat.
 *
 * Shares every block renderer with the pane so a derivation looks the same
 * wherever it appears — and so there is one place where escaping happens.
 */
function renderDerivationInline(doc, email) {
    const blocks = doc.blocks || [];
    if (!blocks.length) return `<div class="dv-empty">Empty derivation.</div>`;
    const title = doc.title
        ? `<div class="dv-inline-title">${derivEscape(doc.title)}</div>` : "";
    return title + blocks.map(b => renderDerivBlock(b, doc.id, email)).join("");
}

/**
 * Typeset the pane's math.
 *
 * Reuses the page's KaTeX and the same $$-only delimiter choice app.js
 * settled on, so a formula renders identically whether it is in the chat or
 * in here. Failure is contained: bad LaTeX must cost the typesetting, never
 * the document.
 */
function typesetDeriv(root) {
    if (typeof renderMathInElement === "undefined" || !root) return;
    try {
        root.querySelectorAll(".dv-expr, .dv-latex").forEach(el => {
            renderMathInElement(el, {
                ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
                throwOnError: false,
                delimiters: [{ left: "$$", right: "$$", display: true }],
            });
        });
        root.querySelectorAll(".dv-text, .dv-reason, .dv-caption").forEach(el => {
            renderMathInElement(el, {
                ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
                throwOnError: false,
                delimiters: [{ left: "$$", right: "$$", display: false }],
            });
        });
    } catch (e) {
        console.warn("derivation math render failed", e);
    }
}

function renderDerivation() {
    const body = derivBodyEl();
    if (!body) return;

    if (!activeDeriv) {
        body.innerHTML = `<div class="dv-empty">
            No derivation open. Ask for one — "derive the gradient of logistic
            loss" — and it will build here while you keep chatting.</div>`;
        return;
    }

    const blocks = activeDeriv.blocks || [];
    if (!blocks.length) {
        body.innerHTML = `<div class="dv-empty">Building…</div>`;
        return;
    }

    body.innerHTML = blocks
        .map(b => renderDerivBlock(b, activeDerivId, derivEmail()))
        .join("");
    typesetDeriv(body);
}

// --- Data ------------------------------------------------------------

async function loadDerivIndex() {
    const email = derivEmail();
    if (!email) return;
    try {
        const res = await fetch(`/api/derivations/${encodeURIComponent(email)}`);
        if (!res.ok) throw new Error(res.status);
        derivIndex = (await res.json()).derivations || [];
        renderDerivPicker();
    } catch (e) {
        console.warn("could not load derivations", e);
    }
}

function renderDerivPicker() {
    const sel = document.getElementById("derivation-picker");
    if (!sel) return;
    sel.innerHTML = derivIndex.map(d =>
        `<option value="${derivEscape(d.id)}"${d.id === activeDerivId ? " selected" : ""}>`
        + `${derivEscape(d.title)}</option>`
    ).join("") || `<option value="">No derivations yet</option>`;
}

async function openDerivation(docId) {
    const email = derivEmail();
    if (!email || !docId) return;
    try {
        const res = await fetch(
            `/api/derivations/${encodeURIComponent(email)}/${encodeURIComponent(docId)}`);
        if (!res.ok) throw new Error(res.status);
        activeDeriv = await res.json();
        activeDerivId = activeDeriv.id;
        renderDerivPicker();
        renderDerivation();
        saveDerivUi();
    } catch (e) {
        console.warn("could not open derivation", e);
    }
}

function onDerivationPicked() {
    const sel = document.getElementById("derivation-picker");
    if (sel && sel.value) openDerivation(sel.value);
}

// --- Jobs ------------------------------------------------------------
//
// A build outlives the request that started it, so the chip state is rebuilt
// by polling rather than pushed. Same approach as demo jobs.

const DERIV_JOB_TERMINAL = ["ready", "failed", "cancelled"];

function startDerivJobPolling() {
    if (derivJobTimer) return;
    derivJobTimer = setInterval(pollDerivJobs, DERIV_JOB_POLL_MS);
}

function stopDerivJobPolling() {
    if (!derivJobTimer) return;
    clearInterval(derivJobTimer);
    derivJobTimer = null;
}

async function pollDerivJobs() {
    const email = derivEmail();
    if (!email) return;
    let jobs = [];
    try {
        const res = await fetch(`/api/derivation-jobs/${encodeURIComponent(email)}`);
        if (!res.ok) throw new Error(res.status);
        jobs = (await res.json()).jobs || [];
    } catch (e) {
        return;                       // transient; the next tick retries
    }

    let anyActive = false;
    for (const job of jobs) {
        if (!DERIV_JOB_TERMINAL.includes(job.state)) anyActive = true;
        applyDerivJobToChip(job);
    }
    if (!anyActive) stopDerivJobPolling();
}

function applyDerivJobToChip(job) {
    const chip = document.querySelector(
        `.tool-call.is-derivation[data-job-id="${CSS.escape(job.job_id)}"]`);
    if (!chip) return;
    if (chip.dataset.state === job.state) return;
    chip.dataset.state = job.state;

    chip.classList.remove("is-building", "is-ready", "is-failed");
    const status = chip.querySelector(".derivation-chip-status");
    const btn = chip.querySelector(".derivation-open-btn");

    if (job.state === "queued" || job.state === "building") {
        chip.classList.add("is-building");
        if (status) status.textContent = "building…";
        if (btn) btn.style.display = "none";
        return;
    }
    if (job.state === "ready") {
        chip.classList.add("is-ready");
        if (status) status.textContent = "ready";
        if (btn) {
            btn.style.display = "";
            btn.onclick = async () => {
                await toggleDerivation(true);
                await loadDerivIndex();
                await openDerivation(job.doc_id);
            };
        }
        // The pane may already be showing this doc mid-build; refresh it.
        if (derivOpen && activeDerivId === job.doc_id) openDerivation(job.doc_id);
        loadDerivIndex();
        return;
    }
    chip.classList.add("is-failed");
    if (status) status.textContent = job.error ? `failed — ${job.error}` : "failed";
    if (btn) btn.style.display = "none";
}

// --- Toggle ----------------------------------------------------------

async function toggleDerivation(force) {
    const pane = derivPaneEl();
    if (!pane) return;
    derivOpen = force !== undefined ? force : !derivOpen;
    pane.style.display = derivOpen ? "flex" : "none";

    const btn = document.getElementById("derivation-btn");
    if (btn) btn.classList.toggle("is-on", derivOpen);

    if (!derivOpen) {
        if (typeof syncPaneSplit === "function") syncPaneSplit();
        saveDerivUi();
        return;
    }

    // The right-hand panes compete for the same column.
    if (typeof notesOpen !== "undefined" && notesOpen
        && typeof toggleNotes === "function") {
        await toggleNotes(false);
    }
    if (typeof demoOpen !== "undefined" && demoOpen
        && typeof toggleDemo === "function") {
        await toggleDemo(false);
    }
    if (typeof notebookOpen !== "undefined" && notebookOpen
        && typeof toggleNotebook === "function") {
        toggleNotebook();
    }
    if (typeof syncPaneSplit === "function") syncPaneSplit();

    if (!derivIndex.length) await loadDerivIndex();
    if (!activeDerivId) {
        const ui = loadDerivUi();
        const target = (ui.docId && derivIndex.some(d => d.id === ui.docId))
            ? ui.docId
            : (derivIndex[0] && derivIndex[0].id);
        if (target) await openDerivation(target);
        else renderDerivation();
    } else {
        // Re-fetch on open: a background build may have appended blocks.
        await openDerivation(activeDerivId);
    }
    saveDerivUi();
}

// Resume polling after a refresh — a build started before the reload is
// probably still running.
document.addEventListener("DOMContentLoaded", () => {
    if (derivEmail()) startDerivJobPolling();
});

// --- Asking the tutor about a derivation -----------------------------
//
// Simpler than the demo equivalent: that one relays selections out of a
// sandboxed, opaque-origin iframe over postMessage. This pane renders in the
// main document, so the selection is readable directly.

let derivSelection = null;

function derivSelectionBarEl() {
    return document.getElementById("derivation-selection-bar");
}

/** Track what the learner highlighted inside the pane. */
function onDerivSelectionChange() {
    const bar = derivSelectionBarEl();
    const body = derivBodyEl();
    if (!bar || !body) return;

    const sel = window.getSelection();
    const text = sel ? sel.toString().trim() : "";

    // Only selections that START inside the pane count — otherwise dragging
    // across the whole page would claim the chat's text as derivation context.
    const anchor = sel && sel.anchorNode;
    const inside = anchor && body.contains(
        anchor.nodeType === 1 ? anchor : anchor.parentElement);

    if (!text || !inside) {
        derivSelection = null;
        bar.style.display = "none";
        return;
    }

    derivSelection = { text: text.slice(0, 2000) };
    const label = document.getElementById("derivation-selection-text");
    if (label) label.textContent = text.length > 60 ? text.slice(0, 60) + "…" : text;
    bar.style.display = "flex";
}

/**
 * Stage this derivation as context for the learner's next chat message.
 *
 * Rides the same path as the demo reference: the client sends only the id and
 * what was highlighted, and the SERVER builds the digest. Sending the blocks
 * from here would put the whole document into the transcript, where it is
 * re-sent on every later turn of the session.
 */
function askTutorAboutDerivation() {
    if (!activeDeriv) return;
    const chat = typeof getMainChat === "function" ? getMainChat() : null;
    if (!chat || typeof chat.setDerivationRef !== "function") return;

    chat.setDerivationRef({
        doc_id: activeDeriv.id,
        title: activeDeriv.title || "derivation",
        selection: derivSelection || null,
    });

    const what = derivSelection
        ? `“${derivSelection.text.slice(0, 40)}”`
        : activeDeriv.title || "this derivation";
    derivToast(`Asking about ${what} — type your question in the chat`);

    const input = document.getElementById("message-input");
    if (input) input.focus();
}

function derivToast(msg) {
    const body = derivBodyEl();
    if (!body) return;
    const el = document.createElement("div");
    el.className = "dv-toast";
    el.textContent = msg;
    body.appendChild(el);
    setTimeout(() => el.remove(), 3200);
}

// --- Revising ---------------------------------------------------------

function derivChatLogEl() { return document.getElementById("derivation-chat-log"); }

function toggleDerivationChat() {
    const box = document.getElementById("derivation-chat");
    if (!box) return;
    const open = box.style.display === "none" || !box.style.display;
    box.style.display = open ? "flex" : "none";
    const btn = document.getElementById("derivation-revise-btn");
    if (btn) btn.classList.toggle("is-on", open);
    if (open) {
        const input = document.getElementById("derivation-chat-text");
        if (input) input.focus();
    }
}

function derivChatNote(text, kind) {
    const log = derivChatLogEl();
    if (!log) return;
    const line = document.createElement("div");
    line.className = `derivation-chat-line${kind ? " is-" + kind : ""}`;
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

// Greetings and similar chatter, which reach here by habit rather than intent.
// A short exact-match list on purpose: wrongly refusing a real edit is far
// worse than occasionally spending a build on one, so anything unrecognised is
// treated as an instruction.
const DERIV_NOT_AN_EDIT = new Set([
    "hi", "hey", "hello", "yo", "thanks", "thank you", "ok", "okay", "cool",
    "nice", "great", "test", "?", "help",
]);

function isNotADerivationEdit(text) {
    return DERIV_NOT_AN_EDIT.has(text.toLowerCase().replace(/[!.]+$/, "").trim());
}

async function sendDerivationEdit() {
    const input = document.getElementById("derivation-chat-text");
    const sendBtn = document.getElementById("derivation-chat-send");
    const email = derivEmail();
    if (!input || !activeDeriv || !email) return;

    const request = input.value.trim();
    if (!request) return;

    // This box revises the derivation; it is not a conversation. A greeting
    // here would otherwise spend a full agent run on a build that correctly
    // changes nothing, and the tutor in the main chat is where to actually talk.
    if (isNotADerivationEdit(request)) {
        derivChatNote(request, "user");
        derivChatNote(
            "This box changes the derivation — try “expand step 3” or “redo "
            + "it with L1”. To ask a question, use the main conversation.",
            "status",
        );
        input.value = "";
        return;
    }

    input.value = "";
    input.disabled = true;
    if (sendBtn) sendBtn.disabled = true;
    derivChatNote(request, "user");
    derivChatNote("building…", "status");

    try {
        const res = await fetch(
            `${API_BASE}/api/derivations/${encodeURIComponent(email)}/${activeDeriv.id}/edit`,
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ request }),
            },
        );
        if (!res.ok) throw new Error(`server returned ${res.status}`);
        const job = await res.json();
        startDerivJobPolling();
        await watchDerivEditJob(email, job.job_id);
    } catch (e) {
        derivChatNote(`Could not start the revision: ${e.message}`, "error");
    } finally {
        input.disabled = false;
        if (sendBtn) sendBtn.disabled = false;
        input.focus();
    }
}

/** Poll one edit job and reload the derivation in place when it lands. */
async function watchDerivEditJob(email, jobId) {
    for (let i = 0; i < 240; i++) {                 // ~6 min ceiling
        await new Promise(r => setTimeout(r, DERIV_JOB_POLL_MS));
        let job;
        try {
            const res = await fetch(
                `${API_BASE}/api/derivation-jobs/${encodeURIComponent(email)}/${jobId}`);
            if (!res.ok) continue;
            job = await res.json();
        } catch (e) {
            continue;                              // transient; keep waiting
        }
        if (!DERIV_JOB_TERMINAL.includes(job.state)) continue;

        if (job.state === "ready") {
            derivChatNote("done", "status");
            await openDerivation(job.doc_id || activeDerivId);
        } else {
            derivChatNote(job.error || `revision ${job.state}`, "error");
        }
        return;
    }
    derivChatNote("still building — check back shortly", "status");
}

document.addEventListener("selectionchange", onDerivSelectionChange);
