// PyMentor Frontend — SSE streaming + knowledge dashboard + multi-session

const API_BASE = "";
let currentEmail = "";
let currentSessionId = null;
let isStreaming = false;


// =====================================================================
// Theme
//
// The initial theme is set inline in index.html <head> so there is no
// flash before first paint; this only handles switching afterwards.
// =====================================================================

/**
 * @param {string} t  "light" | "dark"
 * @param {boolean} persist  false when merely syncing the button to the theme
 *   index.html already applied — otherwise following the OS preference would
 *   silently harden into an explicit saved choice.
 */
function applyTheme(t, persist = true) {
    document.documentElement.setAttribute("data-theme", t);
    const btn = document.getElementById("theme-btn");
    if (btn) {
        btn.textContent = t === "light" ? "◑ Dark" : "◐ Light";
        btn.title = t === "light" ? "Switch to dark" : "Switch to light";
    }
    if (persist) {
        try { localStorage.setItem("pymentor:theme", t); } catch (e) {}
    }
    // Rendered diagrams are baked SVG — they hold the old theme's colours
    // until redrawn, which leaves them unreadable on the opposite theme.
    if (typeof rerenderMermaidForTheme === "function") rerenderMermaidForTheme();
}

function toggleTheme() {
    const cur = document.documentElement.getAttribute("data-theme") || "dark";
    applyTheme(cur === "dark" ? "light" : "dark");
}

// =====================================================================
// Rails
//
// Either side panel can collapse so the conversation gets the whole
// screen. The choice persists — someone who works focused stays focused.
// =====================================================================

const RAIL_SELECTOR = { left: ".left-panel", right: ".right-panel" };

function toggleRail(side, force) {
    const rail = document.querySelector(RAIL_SELECTOR[side]);
    if (!rail) return;

    const willCollapse = force !== undefined
        ? force
        : !rail.classList.contains("collapsed");

    rail.classList.toggle("collapsed", willCollapse);

    const btn = document.getElementById("toggle-" + side);
    if (btn) {
        btn.classList.toggle("is-collapsed", willCollapse);
        btn.textContent = willCollapse ? "⊞" : "⊟";
        btn.title = (willCollapse ? "Show " : "Hide ")
            + (side === "left" ? "sidebar (⌘\\)" : "panel (⌘])");
    }
    try { localStorage.setItem("pymentor:rail-" + side, willCollapse ? "1" : "0"); } catch (e) {}
    syncFocusBtn();
}

/** Both rails at once — one keystroke to a distraction-free screen. */
function toggleFocus() {
    const anyOpen = ["left", "right"].some(s => {
        const el = document.querySelector(RAIL_SELECTOR[s]);
        return el && !el.classList.contains("collapsed");
    });
    toggleRail("left", anyOpen);
    toggleRail("right", anyOpen);
}

function syncFocusBtn() {
    const bothHidden = ["left", "right"].every(s => {
        const el = document.querySelector(RAIL_SELECTOR[s]);
        return el && el.classList.contains("collapsed");
    });
    const btn = document.getElementById("focus-btn");
    if (btn) btn.textContent = bothHidden ? "⛶ Exit focus" : "⛶ Focus";
    document.querySelector(".container").classList.toggle("focused", bothHidden);
}

function initRails() {
    ["left", "right"].forEach(side => {
        let v = null;
        try { v = localStorage.getItem("pymentor:rail-" + side); } catch (e) {}
        if (v === "1") toggleRail(side, true);
    });
    syncFocusBtn();
}

// =====================================================================
// Image attachments
// =====================================================================

// Kept in step with backend/api/routes.py (MAX_ATTACHMENT_BYTES,
// MAX_ATTACHMENTS_PER_TURN, EpisodicMemory.IMAGE_EXT_MAP). The server
// enforces all three independently — these exist so the user gets an
// immediate, readable error instead of a 400 after the upload.
const ATTACH_TYPES = ["image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"];
const MAX_ATTACH_BYTES = 5 * 1024 * 1024;
const MAX_ATTACHMENTS = 4;

function readFileAsDataURL(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
    });
}

/**
 * Wire paste, drag-drop and the file picker for one chat surface.
 *
 * `dropZone` is the whole chat area rather than just the textarea — dropping
 * a screenshot anywhere over the conversation is the natural gesture.
 */
function initAttachments(chatFor, { inputEl, dropZone, attachBtnEl, attachInputEl }) {
    if (inputEl) {
        inputEl.addEventListener("paste", (e) => {
            const files = Array.from(e.clipboardData?.items || [])
                .filter(item => item.kind === "file" && ATTACH_TYPES.includes(item.type))
                .map(item => item.getAsFile())
                .filter(Boolean);
            if (!files.length) return;   // plain text paste — leave it alone
            e.preventDefault();
            chatFor().addAttachments(files);
        });
    }

    if (attachBtnEl && attachInputEl) {
        attachBtnEl.onclick = (e) => { e.preventDefault(); attachInputEl.click(); };
        attachInputEl.addEventListener("change", () => {
            chatFor().addAttachments(attachInputEl.files);
            // Allow re-picking the same file, which otherwise fires no change.
            attachInputEl.value = "";
        });
    }

    if (dropZone) {
        // dragenter/dragleave fire for every child element crossed, so a
        // depth counter is needed or the highlight flickers constantly.
        let depth = 0;
        const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");

        dropZone.addEventListener("dragenter", (e) => {
            if (!hasFiles(e)) return;
            e.preventDefault();
            depth += 1;
            dropZone.classList.add("drop-active");
        });
        // Without preventDefault on dragover the drop event never fires.
        dropZone.addEventListener("dragover", (e) => {
            if (hasFiles(e)) e.preventDefault();
        });
        dropZone.addEventListener("dragleave", () => {
            depth = Math.max(0, depth - 1);
            if (!depth) dropZone.classList.remove("drop-active");
        });
        dropZone.addEventListener("drop", (e) => {
            if (!hasFiles(e)) return;
            e.preventDefault();
            depth = 0;
            dropZone.classList.remove("drop-active");
            chatFor().addAttachments(e.dataTransfer.files);
        });
    }
}

// =====================================================================
// Markdown Renderer (delegates to marked.js loaded in index.html)
// =====================================================================

// A $$…$$ span, non-greedy so consecutive formulas do not merge into one.
const MATH_SPAN_RE = /\$\$[\s\S]+?\$\$/g;

/**
 * Render markdown, holding $$…$$ spans out of the parser.
 *
 * LaTeX and markdown fight over the same punctuation, and markdown wins
 * because it runs first:
 *
 *   `_` — a formula with two subscripts (`\text{IG}_{\text{node}}` … `\sum_{`)
 *         reads as an emphasis pair, so marked emits <em> INSIDE the span.
 *         That splits the formula across elements and KaTeX's text-node walk
 *         never sees a whole $$…$$, leaving the raw source on screen.
 *   `\%`, `\_` — escapable punctuation, so marked strips the backslash and
 *         KaTeX then fails on the bare character.
 *
 * Both are fixed by never letting the parser see the math. Spans come out
 * first, a placeholder holds their position, and they go back verbatim
 * afterwards. Verified against marked 12.
 */
function renderMarkdown(text) {
    if (!text) return "";
    if (typeof marked === "undefined") return escapeHtml(text);

    const spans = [];
    // The placeholder must survive markdown untouched and never occur in real
    // text: letters and digits only, no punctuation for the parser to act on.
    const held = text.replace(MATH_SPAN_RE, (m) => {
        spans.push(m);
        return `x0mathspan${spans.length - 1}endx0`;
    });

    let html = marked.parse(held, { breaks: true, gfm: true });
    if (spans.length) {
        html = html.replace(/x0mathspan(\d+)endx0/g, (_, i) => spans[Number(i)]);
    }
    return html;
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

// =====================================================================
// Rich rendering — math, diagrams, syntax highlighting
//
// renderMarkdown returns a STRING, but typesetting and diagram rendering
// need a live element. So this is a second pass: call enrichMessage() on
// the element right after any `innerHTML = renderMarkdown(...)` write.
//
// Deltas are deliberately rendered as plain textContent while streaming
// (see the assistant_delta case), so everything here only ever sees a
// COMPLETE block. That is what makes it affordable — no reparsing on
// every token, and no half-open $$ or ``` to trip over.
//
// Every step is optional at runtime and individually guarded. These are
// three CDN libraries on the critical path of the only surface the app
// has; a failed load or a throw on odd model output must cost the
// typesetting, never the message. The finally-block that calls this can
// soft-lock the composer permanently if it throws.
// =====================================================================

/**
 * Enrich a rendered assistant message in place. Idempotent and safe to
 * call on any element — each step no-ops if its library is absent.
 */
function enrichMessage(el) {
    if (!el) return;
    renderMathIn(el);
    renderMermaidIn(el);
    expandVerifiedIn(el);
    highlightCodeIn(el);
    addRunButtonsIn(el);
}

// $$…$$ is the ONLY math delimiter, and that is a deliberate, tested
// choice rather than a default:
//
//   \(…\) and \[…\] cannot work here at all. marked treats the backslash
//   as escaped punctuation and strips it, so KaTeX receives a bare "(x^2)"
//   and never sees a delimiter. Verified against marked 12.
//
//   Single $…$ works, but cannot be told apart from currency. "$5,000" is
//   safe, yet "from $5 to $10" renders as one math span, and — worse — a
//   price earlier in a sentence swallows the delimiter of real math later
//   in it. Heuristics to separate the two (require a closing $, reject
//   money-shaped bodies) each broke a legitimate case: "$2x + 1 = 5$" or
//   "$0.001$", both ordinary in this app's subject matter.
//
// $$ is unambiguous — no one writes a price with two dollar signs — and it
// costs nothing, because the display/inline distinction is recovered below
// from where the model put the math rather than from the delimiter.
const MATH_BASE = {
    // Load-bearing: without this, a `$` in a shell snippet or an f-string
    // opens a math span and eats the rest of the block.
    ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
    // Bad LaTeX shows as red source instead of aborting the pass — one
    // malformed formula must not cost the rest of the message.
    throwOnError: false,
};

const MATH_SOLO_RE = /^\$\$[\s\S]+\$\$$/;

/**
 * Typeset $$…$$ spans.
 *
 * A block whose entire text is one formula is rendered as centred display
 * math; anything mixed into a sentence stays inline on the prose baseline.
 * KaTeX picks that per call, not per delimiter, so this runs in two passes:
 * the solo blocks first, then everything still unrendered.
 */
// Second line of defence, for when the MODEL writes a bare character rather
// than an escaped one. renderMarkdown now holds math spans out of the parser,
// so marked no longer strips backslashes — but nothing stops the model from
// writing `100%` or `\text{a_b}` in the first place, and both break KaTeX:
//   %  opens a LaTeX comment, swallowing the rest of the line including the
//      closing brace -> "Unexpected end of input, expected '}'".
//   _  bare inside \text{} is a parse error -> "Expected 'EOF', got '_'".
// Idempotent, so correctly-escaped input passes through unchanged.
// `%` is a comment anywhere in math, so it is always escaped. `_` is NOT:
// outside \text{} it is legitimate subscript syntax and `x_1` must survive
// untouched — only a bare `_` INSIDE a \text{} group is an error, which is
// exactly where snake_case feature names land.
const MATH_PERCENT_RE = /(?<!\\)%/g;
const MATH_TEXT_GROUP_RE = /\\text\{([^{}]*)\}/g;

/**
 * Re-escape LaTeX syntax characters inside $$…$$ spans.
 *
 * Percentages and snake_case feature names are both completely ordinary in
 * this app's subject matter ("62%", "sociability_score"), so this fires on
 * routine content rather than exotic input.
 *
 * Only rewrites between $$ pairs: a bare % or _ in prose is not math and must
 * be left exactly as written. Already-escaped characters are skipped, so
 * running this pass twice cannot produce `\\%`.
 */
function escapeMathSyntax(text) {
    if (text.indexOf("$$") === -1) return text;
    const parts = text.split("$$");
    // Odd indices are inside a span. An even part count means the last $$ was
    // unclosed — a streaming fragment — so leave that trailing part alone.
    const lastInside = parts.length % 2 === 0 ? parts.length - 2 : parts.length - 1;
    for (let i = 1; i <= lastInside; i += 2) {
        parts[i] = parts[i]
            .replace(MATH_PERCENT_RE, "\\%")
            .replace(MATH_TEXT_GROUP_RE, (m, inner) =>
                `\\text{${inner.replace(/(?<!\\)_/g, "\\_")}}`);
    }
    return parts.join("$$");
}

function renderMathIn(el) {
    if (typeof renderMathInElement === "undefined") return;
    try {
        // Fix up the source before KaTeX sees it. Walks text nodes only, and
        // skips the same tags MATH_BASE ignores, so a % in a code block stays
        // literal.
        el.querySelectorAll("p, li, td, th").forEach(block => {
            block.childNodes.forEach(node => {
                if (node.nodeType !== 3) return;  // text nodes only
                const fixed = escapeMathSyntax(node.nodeValue);
                if (fixed !== node.nodeValue) node.nodeValue = fixed;
            });
        });
        el.querySelectorAll("p, li, td, th").forEach(block => {
            const t = block.textContent.trim();
            // The second test rejects "$$a$$ and $$b$$", which matches the
            // regex but is two inline formulas, not one display block.
            if (!MATH_SOLO_RE.test(t) || t.indexOf("$$", 2) !== t.length - 2) return;
            renderMathInElement(block, {
                ...MATH_BASE,
                delimiters: [{ left: "$$", right: "$$", display: true }],
            });
        });
        renderMathInElement(el, {
            ...MATH_BASE,
            delimiters: [{ left: "$$", right: "$$", display: false }],
        });
    } catch (e) {
        console.warn("math render failed", e);
    }
}

/**
 * Syntax-highlight fenced code.
 *
 * Skips mermaid (that fence is a diagram, not code) and anything already
 * processed — enrichMessage runs again on the same element when a turn
 * ends on the finally pass.
 */
function highlightCodeIn(el) {
    if (typeof hljs === "undefined") return;
    el.querySelectorAll("pre code").forEach(block => {
        if (block.classList.contains("language-mermaid")) return;
        if (block.dataset.highlighted) return;
        try {
            hljs.highlightElement(block);
        } catch (e) {
            block.dataset.highlighted = "failed";
        }
    });
}

/**
 * Replace ```verified:<run_id> placeholders with the code that actually ran.
 *
 * Why the indirection: the tutor used to verify a snippet and then RETYPE it
 * into its reply, and a real session caught the retyped copy drifting from
 * what ran — it gained an export_text call that had never been executed, and
 * that call was the one that crashed for the learner. Now the model only
 * names a run; the source travels sandbox -> disk -> here, never back through
 * the model, so it cannot drift.
 *
 * Async, so the fetched block misses this turn's highlight/button pass — both
 * are re-run on arrival. Both are idempotent and flag-guarded, so the blocks
 * already on screen are untouched.
 */
function expandVerifiedIn(el) {
    const email = getEmail();
    promoteBareVerifiedRefs(el);
    el.querySelectorAll("pre code").forEach(block => {
        const cls = Array.from(block.classList)
            .find(c => c.startsWith("language-verified:"));
        if (!cls) return;
        const pre = block.parentElement;
        if (!pre || pre.dataset.verifiedState) return;

        const runId = cls.slice("language-verified:".length).trim();
        // Ids come from a chat message, so treat them as untrusted: this is
        // the shape _new_run_dir produces, and anything else never reaches
        // the network.
        if (!/^run_\d+_[a-f0-9]+$/.test(runId) || !email) {
            markVerifiedUnavailable(pre, block);
            return;
        }

        pre.dataset.verifiedState = "loading";
        block.textContent = "Loading verified code…";

        fetch(`${API_BASE}/api/code/verified/${encodeURIComponent(email)}/${encodeURIComponent(runId)}`)
            .then(res => res.ok ? res.json() : Promise.reject(res.status))
            .then(data => {
                pre.dataset.verifiedState = "ready";
                // textContent, never innerHTML: this is source, and the block
                // must show it literally rather than parse it as markup.
                block.textContent = data.code || "";
                block.className = "language-python";
                pre.classList.add("is-verified");
                // Both helpers select "pre code" — a <code> with a <pre>
                // ANCESTOR — so they must be handed the pre's PARENT. Passing
                // `pre` itself matches nothing (it would need a <pre> nested
                // inside a <pre>), which left verified blocks unhighlighted
                // and without a run button.
                //
                // The blocks that existed when enrichMessage ran are already
                // done; their dataset guards make this re-run a no-op.
                const scope = pre.parentElement || pre;
                highlightCodeIn(scope);
                addRunButtonsIn(scope);
            })
            .catch(() => markVerifiedUnavailable(pre, block));
    });
}

/**
 * Rescue a placeholder the model wrote as prose instead of a fenced block.
 *
 * Observed in the wild: the tutor emitted `verified:run_...` on its own line,
 * so marked produced a <p> (and GFM autolinked it, since "verified:" parses as
 * a URL scheme) — it reached the learner as a dead blue link with no code.
 * The run itself was fine; only the citation was malformed.
 *
 * Rewriting it here rather than only tightening the prompt: the instruction is
 * one line in a long system prompt, and a missed fence should degrade to the
 * right output instead of a broken link.
 */
function promoteBareVerifiedRefs(el) {
    const RE = /^\s*(?:```)?\s*verified:(run_\d+_[a-f0-9]+)\s*(?:```)?\s*$/;
    el.querySelectorAll("p").forEach(p => {
        const m = (p.textContent || "").match(RE);
        if (!m) return;
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        code.className = `language-verified:${m[1]}`;
        pre.appendChild(code);
        p.replaceWith(pre);
    });
}

/**
 * A placeholder we could not resolve — expired, never archived, or malformed.
 * Says so plainly rather than leaving "Loading…" forever or, worse, showing
 * nothing where the learner expected code.
 */
function markVerifiedUnavailable(pre, block) {
    pre.dataset.verifiedState = "missing";
    pre.classList.add("is-verified-missing");
    block.className = "";
    block.textContent =
        "This code sample is no longer available. Ask and I'll run it again.";
}

/**
 * Put a "Run in code runner" button on every Python block the tutor wrote.
 *
 * The tutor has already executed this code before showing it (see run_code in
 * backend/tools.py), so the button hands the learner something known to work
 * rather than a snippet that merely looks right. Both sides share one venv
 * per user, so a package installed during that verification is already there.
 *
 * Same contract as highlightCodeIn: enrichMessage re-runs on the finally pass
 * of a streamed turn, so the dataset flag is what stops a second button being
 * appended to a block that already has one.
 */
function addRunButtonsIn(el) {
    el.querySelectorAll("pre code").forEach(block => {
        if (block.classList.contains("language-mermaid")) return;
        const pre = block.parentElement;
        if (!pre || pre.dataset.runButton) return;

        // Only Python. hljs may add its own classes, so read the language off
        // the fence's class list rather than trusting a single attribute.
        const isPython = Array.from(block.classList).some(c =>
            c === "language-python" || c === "language-py");
        if (!isPython) return;

        pre.dataset.runButton = "1";
        pre.classList.add("has-run-btn");

        const btn = document.createElement("button");
        btn.className = "header-btn code-run-btn";
        btn.type = "button";
        btn.textContent = "Run in code runner";
        btn.title = "Copy this into a new notebook cell";
        btn.onclick = () => {
            // textContent, not innerHTML: hljs has wrapped the source in spans
            // by now, and innerHTML would carry that markup into the cell.
            sendCodeToRunner(block.textContent || "");
        };
        pre.appendChild(btn);
    });
}

/**
 * Seed a notebook cell with code from chat and reveal it.
 *
 * The mirror of sendCellToChat: that carries a learner's cell into the
 * conversation, this carries the tutor's snippet back out into the notebook.
 */
function sendCodeToRunner(code) {
    if (!code.trim()) return;
    // Open BEFORE adding: addCell sizes the textarea from scrollHeight, which
    // a hidden pane reports as 0, so a cell added first opens up collapsed.
    if (!notebookOpen) toggleNotebook();
    const cell = addCell(code.replace(/\s+$/, ""));
    saveCells();
    if (cell) cell.scrollIntoView({ behavior: "smooth", block: "center" });
}

// --- Mermaid ---------------------------------------------------------
// Diagrams are baked SVG, so they cannot follow a CSS variable when the
// theme flips — applyTheme re-runs the whole pass instead. That only
// works if the source survives rendering, hence data-mermaid-src on the
// figure. See rerenderMermaidForTheme below.

let mermaidReady = false;
let mermaidSeq = 0;

window.addEventListener("mermaid-ready", () => {
    mermaidReady = true;
    // A message may have rendered before the module finished loading;
    // those left a placeholder figure behind, so sweep them now.
    document.querySelectorAll("[data-mermaid-src]").forEach(fig => {
        if (!fig.querySelector("svg")) drawMermaid(fig);
    });
});

/**
 * Mermaid's own theme variables, read from the live design tokens so a
 * diagram matches the app in both themes rather than shipping its own
 * palette. Re-read on every init — the values change with the theme.
 */
function mermaidThemeVars() {
    const css = getComputedStyle(document.documentElement);
    const v = n => css.getPropertyValue(n).trim();
    return {
        background: v("--bg-secondary"),
        primaryColor: v("--bg-tertiary"),
        primaryTextColor: v("--text-bright"),
        primaryBorderColor: v("--border-strong"),
        secondaryColor: v("--bg-secondary"),
        tertiaryColor: v("--bg-secondary"),
        lineColor: v("--text-muted"),
        textColor: v("--text-dim"),
        fontFamily: v("--font-sans") || "inherit",
        fontSize: "14px",
    };
}

function initMermaid() {
    window.mermaid.initialize({
        startOnLoad: false,
        // The chat path feeds model output straight to marked, which has
        // had no sanitizer since v8 (notes.js escapes first precisely
        // because it is a stored surface; this one is not). Mermaid puts
        // generated SVG into the main document, so strict mode — which
        // strips scripts and click-bound JS from diagram source — is doing
        // real work here, not ceremony.
        securityLevel: "strict",
        theme: "base",
        themeVariables: mermaidThemeVars(),
    });
}

/**
 * Replace ```mermaid fences with rendered diagrams.
 *
 * The <pre> is swapped for a <figure data-mermaid-src> immediately, even
 * if mermaid has not loaded yet — that keeps the source out of view and
 * gives the ready-handler something to find.
 */
function renderMermaidIn(el) {
    el.querySelectorAll("pre > code.language-mermaid").forEach(code => {
        const pre = code.parentElement;
        const fig = document.createElement("figure");
        fig.className = "mermaid-figure";
        fig.dataset.mermaidSrc = code.textContent;
        pre.replaceWith(fig);
        if (mermaidReady) drawMermaid(fig);
    });
}

async function drawMermaid(fig) {
    const src = fig.dataset.mermaidSrc || "";
    try {
        initMermaid();
        const { svg } = await window.mermaid.render(`mmd-${++mermaidSeq}`, src);
        fig.innerHTML = svg;
        attachMermaidNodeClicks(fig);
    } catch (e) {
        // Invalid diagram syntax is a model mistake, not a page fault. Show
        // the source back rather than an empty box, so the learner still
        // sees what was meant — and mermaid's own error SVG never leaks in.
        fig.classList.add("mermaid-failed");
        const pre = document.createElement("pre");
        pre.textContent = src;
        fig.replaceChildren(pre);
    }
}

/**
 * Make diagram boxes clickable: fill the composer with a question about
 * that node and focus it.
 *
 * Prefill only, never auto-send — a stray click on a diagram must not
 * spend a turn.
 */
function attachMermaidNodeClicks(fig) {
    fig.querySelectorAll(".node").forEach(node => {
        node.classList.add("mermaid-node-clickable");
        node.addEventListener("click", () => {
            const label = (node.textContent || "").trim();
            if (!label) return;
            const input = document.getElementById("message-input");
            if (!input) return;
            input.value = `Tell me more about "${label}" in that diagram.`;
            autoResizeInput(input);
            input.focus();
        });
    });
}

/**
 * Redraw every diagram on the page against the new theme's tokens.
 * Called from applyTheme — SVG cannot inherit the variable change.
 */
function rerenderMermaidForTheme() {
    if (!mermaidReady) return;
    document.querySelectorAll("[data-mermaid-src]").forEach(drawMermaid);
}

// =====================================================================
// Tab Management
// =====================================================================

function switchTab(tabName) {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
    document.querySelector(`.tab[data-tab="${tabName}"]`).classList.add("active");
    document.getElementById(`tab-${tabName}`).classList.add("active");

    if (tabName === "preferences") {
        initPreferences();
    }
}

// =====================================================================
// Session Management
// =====================================================================

async function onEmailChange() {
    const email = getEmail();
    if (!email) {
        document.getElementById("session-section").style.display = "none";
        updateLearnerChip("");
        // Clearing the box is also a change of identity — the previous
        // learner's demo must not stay on screen.
        if (typeof demoOnEmailChange === "function") demoOnEmailChange();
        getMainChat().setDemoRef(null);
        return;
    }
    currentEmail = email;
    // Persist identity: without this every refresh drops it and blanks every
    // panel, even though the notebook already used localStorage for cells.
    try { localStorage.setItem("pymentor:email", email); } catch (e) {}
    updateLearnerChip(email);

    // Reset per-learner UI state BEFORE loading the new user's sessions, so
    // nothing from the previous identity survives into the replay. Demos are
    // isolated on disk but this module's state is not — see demoOnEmailChange.
    if (typeof demoOnEmailChange === "function") demoOnEmailChange();
    // A demo staged for the next message belongs to the previous learner, and
    // its id means nothing under the new one.
    getMainChat().setDemoRef(null);

    // Clear out sessions that were created by "+" and never used. Done here
    // rather than in loadSessions, which also runs after every turn.
    await pruneEmptySessions(email);
    await loadSessions(email);
}

/** Reflect the current learner in the top bar. */
function updateLearnerChip(email) {
    const avatar = document.getElementById("learner-avatar");
    const label = document.getElementById("learner-email");
    if (!avatar || !label) return;
    avatar.textContent = email ? email[0] : "?";
    label.textContent = email || "Set your email";
}

/** Clicking the chip jumps to the email field so it can be changed. */
function focusEmail() {
    const el = document.getElementById("email-input");
    if (!el) return;
    // Reveal the rail first if the user has it collapsed.
    const rail = document.querySelector(".left-panel");
    if (rail && rail.classList.contains("collapsed")) toggleRail("left", false);
    el.focus();
    el.select();
}

/** Send one of the empty-state starter prompts. */
function useStarter(btn) {
    const input = document.getElementById("message-input");
    input.value = btn.textContent.trim();
    autoResizeInput(input);
    if (!getEmail()) {
        // No identity yet — the message would fail, so ask for it first.
        focusEmail();
        return;
    }
    sendMessage();
}

async function loadSessions(email) {
    try {
        const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(email)}`);
        if (!res.ok) {
            // No sessions yet — show section with empty state
            document.getElementById("session-section").style.display = "block";
            renderSessionList([], null);
            return;
        }
        const data = await res.json();
        const sessions = data.sessions || [];
        const activeSession = data.active_session || null;

        document.getElementById("session-section").style.display = "block";

        // Decide which session is current BEFORE rendering: the list marks
        // the active row and sets the header from it, so rendering first
        // would highlight one session while the header named another.
        if (activeSession && sessions.some(s => s.session_id === activeSession)) {
            currentSessionId = activeSession;
        } else if (sessions.length > 0) {
            currentSessionId = sessions[sessions.length - 1].session_id;
        } else {
            currentSessionId = null;
        }

        renderSessionList(sessions, activeSession);

        // Load chat history for the selected session
        if (currentSessionId) {
            await loadSessionHistory(email, currentSessionId);
            refreshKnowledge(email);
            refreshEpisodes(email);
            refreshQuizzes(email);
        }
    } catch (e) {
        console.error("Failed to load sessions:", e);
        document.getElementById("session-section").style.display = "block";
        renderSessionList([], null);
    }
}

function renderSessionList(sessions, activeSession) {
    const container = document.getElementById("session-list");

    if (sessions.length === 0) {
        container.innerHTML = '<div class="session-empty">No sessions yet. Send a message to start!</div>';
        return;
    }

    // Sort by last_active descending
    const sorted = [...sessions].sort((a, b) =>
        (b.last_active || b.created_at || "").localeCompare(a.last_active || a.created_at || "")
    );

    let html = "";
    for (const s of sorted) {
        // Highlights the session actually loaded in the chat — loadSessions
        // resolves currentSessionId before calling this, so the lit row and
        // the header always name the same conversation.
        const isActive = s.session_id === (currentSessionId || activeSession);
        const date = s.last_active
            ? s.last_active.substring(0, 10)
            : s.created_at ? s.created_at.substring(0, 10) : "";
        const name = s.name || "Untitled Session";
        if (isActive) setChatTitle(name);

        // Branches nest under their parent so a forked conversation reads as
        // a variant rather than an unrelated sibling.
        const kind = s.kind || "main";
        const isBranch = kind === "branch" || kind === "side_chat";

        html += `
            <div class="session-item${isActive ? " active" : ""}${isBranch ? " is-branch" : ""}"
                 onclick="switchSession('${s.session_id}')"
                 data-session-id="${s.session_id}">
                <div class="session-row">
                    <div class="session-name" title="${escapeHtml(name)}">${escapeHtml(name)}</div>
                    <button class="session-delete" title="Delete this session"
                            onclick="deleteSession('${s.session_id}', this.closest('.session-item').querySelector('.session-name').textContent, event)">×</button>
                </div>
                <div class="session-date">${date}${isBranch ? ` · ${kind === "branch" ? "edited" : "side"}` : ""}</div>
            </div>`;
    }

    container.innerHTML = html;
}

/** Name the conversation you're actually in, in the chat header. */
function setChatTitle(name) {
    const el = document.getElementById("chat-title");
    if (!el) return;
    const label = (name || "").trim() || "New session";
    el.textContent = label;
    el.title = label;
}

// After this many user turns the session gets a title based on what was
// actually discussed, rather than whatever the opening message happened to be.
const RETITLE_AFTER_TURNS = 3;

/** Ask the server for a subject-based title. Best-effort. */
async function retitleSession(email, sessionId) {
    try {
        const res = await fetch(
            `${API_BASE}/api/sessions/${encodeURIComponent(email)}/${sessionId}/retitle`,
            { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
        );
        if (!res.ok) return;
        const data = await res.json();
        if (!data.name) return;

        if (sessionId === currentSessionId) setChatTitle(data.name);

        // Patch just this row's label rather than calling loadSessions().
        // A side chat can retitle itself now, and a full list reload from
        // there would rebuild the main chat's message list mid-conversation.
        const row = document.querySelector(
            `.session-item[data-session-id="${sessionId}"] .session-name`);
        if (row) {
            row.textContent = data.name;
            row.title = data.name;
        }
    } catch (e) {
        // Non-critical — the existing name stays.
    }
}

// Sessions already retitled (or judged not to need it) in this page load, so
// switching back and forth between two chats cannot re-fire the LLM call.
const backfilledTitles = new Set();

/**
 * Name a session that never got past the "New Session" default.
 *
 * Sessions created before side chats could retitle themselves are stuck with
 * the server default; there is no migration, so they heal when opened. Guarded
 * hard, because each call costs an LLM round-trip: only the default name, only
 * once the conversation has a real subject, only once per page load.
 */
function backfillTitleIfDefault(email, sessionId, userTurnCount) {
    if (!email || !sessionId) return;
    if (backfilledTitles.has(sessionId)) return;
    if (userTurnCount < RETITLE_AFTER_TURNS) return;

    const row = document.querySelector(
        `.session-item[data-session-id="${sessionId}"] .session-name`);
    // No row yet (list still loading) means we cannot confirm the name is the
    // default — skip rather than risk renaming a session the user named.
    if (!row) return;
    const name = (row.textContent || "").trim();
    if (name && name !== "New Session" && name !== "Untitled Session") return;

    backfilledTitles.add(sessionId);
    retitleSession(email, sessionId);
}

/** Delete a session and its transcript, after confirming. */
async function deleteSession(sessionId, name, ev) {
    if (ev) ev.stopPropagation();   // don't also switch to the row being deleted
    if (isStreaming) return;
    const email = getEmail();
    if (!email) return;
    if (!confirm(`Delete "${name}"?\n\nThe conversation and its transcript are removed permanently.`)) return;

    try {
        const res = await fetch(
            `${API_BASE}/api/sessions/${encodeURIComponent(email)}/${sessionId}`,
            { method: "DELETE" }
        );
        if (!res.ok) return;
        if (sessionId === currentSessionId) {
            // We just deleted what was on screen — fall back to whatever the
            // server promoted to active.
            currentSessionId = null;
            clearChat();
            setChatTitle("New session");
        }
        await loadSessions(email);
    } catch (e) {
        console.error("Failed to delete session:", e);
    }
}

/** Drop sessions that were created but never used. */
async function pruneEmptySessions(email) {
    try {
        const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(email)}/prune`,
            { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        if (!res.ok) return 0;
        return (await res.json()).count || 0;
    } catch (e) {
        return 0;
    }
}

async function switchSession(sessionId) {
    if (sessionId === currentSessionId) return;
    if (isStreaming) return;

    currentSessionId = sessionId;

    // Activate on server
    const email = getEmail();
    if (email) {
        try {
            await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(email)}/${sessionId}/activate`, {
                method: "POST",
            });
        } catch (e) {
            console.error("Failed to activate session:", e);
        }
    }

    // Update UI. The header has to be set here, not just in
    // renderSessionList: switching only re-flags the rows, and clearChat()
    // below resets the title to "New session".
    let switchedName = "New session";
    document.querySelectorAll(".session-item").forEach(el => {
        const isTarget = el.dataset.sessionId === sessionId;
        el.classList.toggle("active", isTarget);
        if (isTarget) {
            const label = el.querySelector(".session-name");
            if (label) switchedName = label.textContent;
        }
    });

    // Clear chat and load history
    clearChat(false);
    setChatTitle(switchedName);
    await loadSessionHistory(email, sessionId);

    // Refresh panels
    if (currentEmail) {
        refreshKnowledge(currentEmail);
        refreshEpisodes(currentEmail);
        refreshQuizzes(currentEmail);
    }
}

async function createNewSession() {
    const email = getEmail();
    if (!email) {
        addMessage("Please enter your email first.", "error");
        return;
    }
    if (isStreaming) return;

    try {
        const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(email)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: "New Session" }),
        });
        const data = await res.json();
        currentSessionId = data.session_id;

        // Reload sessions list
        await loadSessions(email);

        // Clear chat for the new session
        clearChat();
    } catch (e) {
        console.error("Failed to create session:", e);
        addMessage("Failed to create new session.", "error");
    }
}

/**
 * Empty the transcript.
 *
 * @param {boolean} resetTitle  true when starting a genuinely new session.
 *   Switching to an existing one passes false and sets the real name itself,
 *   otherwise the header would flash back to "New session".
 */
function clearChat(resetTitle = true) {
    const container = document.getElementById("chat-messages");
    container.innerHTML = `
        <div class="empty-state">
            <div class="emoji">&#128218;</div>
            Ask anything to get started.
        </div>`;

    if (resetTitle) setChatTitle("New session");

    // Clear events log
    document.getElementById("events-log").innerHTML = "";
}

async function loadSessionHistory(email, sessionId) {
    if (!email || !sessionId) return;

    try {
        const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(email)}/${sessionId}/history`);
        if (!res.ok) return;
        const data = await res.json();
        const turns = data.turns || [];

        // Remember how far along this conversation is, so the auto-name and
        // retitle rules key off real history rather than DOM state.
        loadedUserTurnCount = turns.filter(t => t.type === "user").length;
        if (mainChat) mainChat.turnCount = loadedUserTurnCount;

        backfillTitleIfDefault(email, sessionId, loadedUserTurnCount);

        if (turns.length === 0) return;

        // Clear the empty state
        const container = document.getElementById("chat-messages");
        container.innerHTML = "";

        // Accumulate assistant_message chunks and flush before other turn types.
        // This handles cases where the agent sends text, then makes tool calls,
        // and the final "result" event has empty content.
        let pendingAssistantText = "";

        function flushAssistant() {
            if (pendingAssistantText) {
                addMessage(pendingAssistantText, "assistant");
                pendingAssistantText = "";
            }
        }

        for (const turn of turns) {
            switch (turn.type) {
                case "user": {
                    flushAssistant();
                    // Attachments come back as filenames only; the serving
                    // route rebuilds the path from email + filename.
                    const atts = (turn.attachments || []).map(a => ({
                        url: `${API_BASE}/api/uploads/${encodeURIComponent(email)}/${encodeURIComponent(a.filename)}`,
                        filename: a.filename,
                    }));
                    const div = addMessage(turn.content, "user", null, atts);
                    // The stored index is the handle "edit this message" needs;
                    // without it the client can't tell the server what to
                    // rewrite. Live-streamed messages get theirs on reload.
                    if (turn.index != null) {
                        div.dataset.turnIndex = turn.index;
                        attachEditControl(div);
                    }
                    break;
                }
                case "assistant_message":
                    if (turn.content) {
                        pendingAssistantText += turn.content;
                    }
                    break;
                case "result":
                    // If result has content, prefer it (it's the final aggregated text).
                    // Otherwise the accumulated assistant_message chunks already cover it.
                    if (turn.content) {
                        // Drop pending text — the result supersedes it
                        pendingAssistantText = "";
                        addMessage(turn.content, "assistant");
                    } else {
                        flushAssistant();
                    }
                    break;
                case "tool_call":
                    flushAssistant();
                    addToolCard(
                        turn.tool_name || "",
                        turn.tool_input || {},
                        turn.tool_use_id || `hist-${turn.index}`
                    );
                    break;
                case "tool_result":
                    // Replayed so a tool that failed still looks failed after a
                    // refresh. The card was rendered by the tool_call above and
                    // is matched by tool_use_id.
                    if (turn.is_error) {
                        markToolCardFailed(
                            turn.tool_use_id || `hist-${turn.index}`,
                            turn.content,
                            container,
                        );
                    } else {
                        // Restores the chip's job id; resumeDemoJobs then
                        // re-attaches any build still running.
                        attachDemoJob(
                            turn.tool_use_id || `hist-${turn.index}`,
                            turn.content,
                            container,
                        );
                    }
                    break;
                case "error":
                    flushAssistant();
                    addMessage(turn.content, "error");
                    break;
                case "side_chat_summary":
                    // A finished tangent, folded back in as one card.
                    flushAssistant();
                    renderSideSummaryCard(turn, container);
                    break;
                case "demo_pin":
                    // A demo the learner added to the conversation.
                    flushAssistant();
                    renderDemoPinCard(turn, container);
                    break;
                case "side_chat_start":
                    // Boundary marker — the summary card carries the payload,
                    // so nothing to render here.
                    break;
            }
        }
        // Flush any trailing assistant text
        flushAssistant();

        // Chips are back in the DOM but nothing is watching them yet; a build
        // outlives the request that started it, so re-attach to any still
        // running. Must follow the replay — it matches on data-job-id.
        if (typeof resumeDemoJobs === "function") resumeDemoJobs();

        // Scroll to bottom
        container.scrollTop = container.scrollHeight;
    } catch (e) {
        console.error("Failed to load session history:", e);
    }
}

// =====================================================================
// Chat
// =====================================================================

function getEmail() {
    const el = document.getElementById("email-input");
    return el.value.trim();
}

function cleanToolName(name) {
    // Strip "mcp__learning-tools__" prefix for display
    return (name || "").replace(/^mcp__[^_]+__/, "");
}

// The render helpers take an optional container so a second chat surface (the
// side-chat window) can reuse them. Omitting it targets the main chat, which
// keeps every existing call site working unchanged.
function mainMessagesEl() {
    return document.getElementById("chat-messages");
}

/**
 * @param attachments Images to show on the bubble. Either staged uploads
 *   ({dataUrl}) or replayed history ({url}) — both render the same.
 */
function addMessage(content, type, container, attachments = null) {
    container = container || mainMessagesEl();
    // Remove empty state
    const empty = container.querySelector(".empty-state");
    if (empty) empty.remove();

    const div = document.createElement("div");
    div.className = `message ${type}`;
    if (type === "assistant") {
        div.innerHTML = renderMarkdown(content);
        enrichMessage(div);
    } else {
        div.textContent = content;
    }
    if (attachments && attachments.length) {
        // Appended as DOM, not concatenated into the string above: user
        // messages render via textContent, which would show raw markup.
        const strip = document.createElement("div");
        strip.className = "message-attachments";
        attachments.forEach(att => {
            const img = document.createElement("img");
            img.src = att.dataUrl || att.url;
            img.alt = att.name || att.filename || "attached image";
            strip.appendChild(img);
        });
        div.appendChild(strip);
    }
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;

    // Only main-chat assistant messages offer a tangent launcher — a side
    // chat spawning its own side chat is out of scope.
    if (type === "assistant" && container === mainMessagesEl()) {
        attachSideChatLauncher(div);
    }
    return div;
}

function addToolCard(toolName, toolInput, toolUseId, container) {
    container = container || mainMessagesEl();
    const div = document.createElement("div");
    div.className = "tool-card";
    // Element ids live in a global namespace, so scope them per surface —
    // otherwise a side-chat rendering the same tool_use_id collides.
    const scope = container.id || "chat-messages";
    div.id = `tool-${scope}-${toolUseId}`;

    const displayName = cleanToolName(toolName);

    // Build a friendly summary of the tool input
    let summary = "";
    if (displayName === "update_concept") {
        summary = `${toolInput.name || toolInput.concept_id} → ${toolInput.mastery}`;
    } else if (displayName === "save_conversation_summary") {
        summary = (toolInput.summary || "").substring(0, 80) + "...";
    } else if (displayName === "search_past_conversations") {
        summary = `"${toolInput.query}"`;
    } else if (displayName === "suggest_next_topics") {
        summary = `count: ${toolInput.count || 3}`;
    } else if (displayName === "get_quiz_topics") {
        summary = toolInput.category ? `category: ${toolInput.category}` : "all topics";
    } else if (displayName === "record_quiz_result") {
        summary = `${toolInput.score}/${toolInput.total}`;
    } else if (displayName === "update_learner_profile") {
        summary = toolInput.level || "profile update";
    } else if (displayName === "save_demo" || displayName === "request_demo") {
        summary = toolInput.title || "interactive demo";
    }

    // A demo requested from the tutor is built in the background, so the chip
    // starts in a building state and is driven to ready/failed by the job
    // poller. It is the only completion signal — see demo-pane.js.
    if (displayName === "request_demo") {
        div.classList.add("is-demo", "is-building");
        div.innerHTML = `
            <div class="tool-header">
                <span class="tool-icon">▶</span>
                <span class="tool-name">demo</span>
                <span class="tool-summary">${escapeHtml(summary)}</span>
                <span class="demo-chip-status">building…</span>
                <button class="demo-open-btn" type="button" style="display:none">Open</button>
            </div>
        `;
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
        return div;
    }

    // A demo's tool_input holds the entire HTML document. Dumping that into
    // the expandable JSON view would bury the conversation in markup, so show
    // a title and a way back to the pane instead.
    if (displayName === "save_demo") {
        div.classList.add("is-demo");
        div.innerHTML = `
            <div class="tool-header">
                <span class="tool-icon">▶</span>
                <span class="tool-name">demo</span>
                <span class="tool-summary">${escapeHtml(summary)}</span>
                <button class="demo-open-btn" type="button">Open</button>
            </div>
        `;
        const btn = div.querySelector(".demo-open-btn");
        if (btn) {
            const cardTitle = toolInput.title || "";
            const cardConcept = toolInput.concept_id || "";
            btn.onclick = (e) => {
                e.stopPropagation();
                // Open THIS card's demo, resolved server-side from the args
                // the call was made with. Previously this only opened the
                // pane, so it showed whatever was last loaded — clicking an
                // older card gave the wrong demo.
                if (typeof openDemoByTitle === "function") {
                    openDemoByTitle(cardTitle, cardConcept);
                } else if (typeof toggleDemo === "function") {
                    toggleDemo(true);
                }
            };
        }
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
        return div;
    }

    div.innerHTML = `
        <div class="tool-header">
            <span class="tool-icon">⚡</span>
            <span class="tool-name">${displayName}</span>
            ${summary ? `<span class="tool-summary">${escapeHtml(summary)}</span>` : ""}
        </div>
        <div class="tool-detail">${escapeHtml(JSON.stringify(toolInput, null, 2))}</div>
    `;
    div.onclick = () => div.classList.toggle("expanded");
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return div;
}

/** Mark a tool card as failed, using the id addToolCard assigned it.
 *
 * Tool results were invisible until the backend started emitting them (the
 * SDK delivers them inside a UserMessage, which agent.py used to drop), so a
 * tool that failed still rendered as a successful-looking card. A save_demo
 * that never wrote anything left an "Open" button that 404'd.
 */
function markToolCardFailed(toolUseId, reason, container) {
    container = container || mainMessagesEl();
    const scope = container.id || "chat-messages";
    const card = document.getElementById(`tool-${scope}-${toolUseId}`);
    if (!card || card.classList.contains("is-failed")) return;

    card.classList.add("is-failed");

    // An Open button on a demo that was never saved is a trap — remove it.
    const openBtn = card.querySelector(".demo-open-btn");
    if (openBtn) openBtn.remove();

    const header = card.querySelector(".tool-header");
    if (!header) return;
    const note = document.createElement("span");
    note.className = "tool-error";
    note.textContent = (reason || "failed").split("\n")[0].substring(0, 120);
    header.appendChild(note);
}

/** Bind a request_demo chip to its background build.
 *
 * The job id exists only in the tool RESULT — the call cannot know it — so the
 * chip is matched back by tool_use_id and then handed to the job poller.
 */
function attachDemoJob(toolUseId, resultContent, container) {
    if (!toolUseId || !resultContent) return;
    container = container || mainMessagesEl();
    const scope = container.id || "chat-messages";
    const card = document.getElementById(`tool-${scope}-${toolUseId}`);
    if (!card || !card.classList.contains("is-demo")) return;

    let jobId = null;
    try {
        jobId = JSON.parse(resultContent).job_id || null;
    } catch {
        return;  // not a request_demo result
    }
    if (!jobId) return;

    card.dataset.jobId = jobId;
    if (typeof trackDemoJob === "function") trackDemoJob(jobId, card);
}

function addEventLog(event) {
    const log = document.getElementById("events-log");
    const div = document.createElement("div");
    div.className = `event-item ${event.type}`;

    let text = `[${event.type}]`;
    if (event.type === "tool_call") {
        text = `[tool] ${cleanToolName(event.tool_name)}`;
    } else if (event.type === "assistant_message") {
        text = `[msg] ${(event.content || "").substring(0, 80)}...`;
    } else if (event.type === "result") {
        text = `[done] stream complete`;
    } else if (event.type === "error") {
        text = `[error] ${event.content}`;
    } else if (event.type === "session_init") {
        text = `[session] ${event.session_id?.substring(0, 8)}...`;
    }

    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
}

function scrollChatToBottom(container) {
    container = container || mainMessagesEl();
    container.scrollTop = container.scrollHeight;
    if (container === mainMessagesEl()) hideScrollToBottomBtn();
}

// How close to the bottom still counts as "following along" (px).
const SCROLL_STICK_THRESHOLD = 80;

function isChatAtBottom(container) {
    const c = container || mainMessagesEl();
    return c.scrollHeight - c.scrollTop - c.clientHeight <= SCROLL_STICK_THRESHOLD;
}

// Scroll only if the reader is already parked at the bottom. During streaming
// this fires on every token, so unconditionally scrolling would rip the view
// away from someone reading further up. If they've scrolled up, leave the
// viewport alone and offer a button instead.
function scrollChatIfPinned(container) {
    container = container || mainMessagesEl();
    const isMain = container === mainMessagesEl();
    if (isChatAtBottom(container)) {
        container.scrollTop = container.scrollHeight;
        if (isMain) hideScrollToBottomBtn();
    } else if (isMain) {
        // The jump-to-latest button only exists for the main chat.
        showScrollToBottomBtn();
    }
}

function showScrollToBottomBtn() {
    const btn = document.getElementById("scroll-bottom-btn");
    if (btn) btn.classList.add("visible");
}

function hideScrollToBottomBtn() {
    const btn = document.getElementById("scroll-bottom-btn");
    if (btn) btn.classList.remove("visible");
}

function autoResizeInput(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 150) + "px";
}

// --- Thinking indicator ---------------------------------------------------
// Lives inside the message list (not the sidebar) so it appears where the user
// is actually looking. The elapsed counter is the part that distinguishes
// "still working" from "hung".
//
// State hangs off the element rather than a module global, so two chats
// streaming at once each get their own indicator and timer.

function showThinking(container) {
    container = container || mainMessagesEl();
    if (container.querySelector(".thinking-indicator")) return;

    const empty = container.querySelector(".empty-state");
    if (empty) empty.remove();

    const el = document.createElement("div");
    el.className = "thinking-indicator";
    el.innerHTML =
        '<span class="thinking-dots"><span></span><span></span><span></span></span>' +
        '<span class="thinking-label">thinking…</span>' +
        '<span class="thinking-elapsed">0s</span>';
    container.appendChild(el);
    scrollChatIfPinned(container);

    const startedAt = Date.now();
    el._timer = setInterval(() => {
        const elapsed = el.querySelector(".thinking-elapsed");
        if (elapsed) {
            elapsed.textContent = Math.round((Date.now() - startedAt) / 1000) + "s";
        }
    }, 1000);
}

function hideThinking(container) {
    container = container || mainMessagesEl();
    const el = container.querySelector(".thinking-indicator");
    if (!el) return;
    if (el._timer) clearInterval(el._timer);
    el.remove();
}

// =====================================================================
// ChatController — one streaming conversation bound to one set of DOM
// elements and one session id.
//
// The main chat is instance #1. The side-chat window is instance #2, running
// concurrently against a *different* session. Two rules keep them from
// corrupting each other:
//   - all DOM access goes through this.* elements, never getElementById
//   - only the main instance touches globals (currentSessionId, isStreaming)
//     or refreshes the session list, which rebuilds the main chat's DOM
// =====================================================================

class ChatController {
    constructor({ messagesEl, inputEl, sendBtnEl, sessionId = null, isMain = false, onSessionId = null,
                  attachStripEl = null, attachBtnEl = null, attachInputEl = null }) {
        this.messagesEl = messagesEl;
        this.inputEl = inputEl;
        this.sendBtnEl = sendBtnEl;
        this.sessionId = sessionId;
        this.isMain = isMain;
        this.streaming = false;
        // Images staged for the NEXT turn, as {name, contentType, data}
        // where data is bare base64 (no data: prefix). Cleared on send.
        this.pendingAttachments = [];
        // A demo the next message is about, staged from the Demo pane as
        // {demo_id, title, selection}. Only the id and selection go to the
        // server, which builds the digest — see run_agent's demo_ref.
        this.pendingDemoRef = null;
        this.attachStripEl = attachStripEl;
        this.attachBtnEl = attachBtnEl;
        this.attachInputEl = attachInputEl;
        // How many user turns this controller has sent into the current
        // session. Drives auto-naming (turn 1) and the retitle check (turn 3).
        this.turnCount = 0;
        // Notified when the SDK reports the real session id (it differs from
        // any placeholder we sent).
        this.onSessionId = onSessionId;
    }

    /**
     * Stage image files for the next turn. Accepts anything File-like — a
     * FileList from the picker or drop, or files pulled off clipboard items.
     *
     * Limits mirror the server's (see MAX_ATTACHMENT_BYTES in routes.py).
     * The server is the real gate; these just fail fast with a clearer
     * message than a 400 mid-send.
     */
    async addAttachments(files) {
        const list = Array.from(files || []).filter(f => ATTACH_TYPES.includes(f.type));
        if (!list.length) return;

        for (const file of list) {
            if (this.pendingAttachments.length >= MAX_ATTACHMENTS) {
                addMessage(`You can attach at most ${MAX_ATTACHMENTS} images per message.`,
                           "error", this.messagesEl);
                break;
            }
            if (file.size > MAX_ATTACH_BYTES) {
                addMessage(`"${file.name || "image"}" is larger than ${MAX_ATTACH_BYTES / (1024 * 1024)}MB.`,
                           "error", this.messagesEl);
                continue;
            }
            try {
                const dataUrl = await readFileAsDataURL(file);
                this.pendingAttachments.push({
                    name: file.name || "pasted-image",
                    contentType: file.type,
                    // Strip the "data:<mime>;base64," prefix — the API wants
                    // bare base64, but the thumbnail needs the full URL.
                    data: dataUrl.slice(dataUrl.indexOf(",") + 1),
                    dataUrl,
                });
            } catch (e) {
                addMessage(`Could not read "${file.name || "image"}".`, "error", this.messagesEl);
            }
        }
        this.renderAttachments();
    }

    /** Repaint the staged-thumbnail strip from pendingAttachments. */
    renderAttachments() {
        const strip = this.attachStripEl;
        if (!strip) return;
        strip.innerHTML = "";
        this.pendingAttachments.forEach((att, i) => {
            const wrap = document.createElement("div");
            wrap.className = "attach-thumb";

            const img = document.createElement("img");
            img.src = att.dataUrl;
            img.alt = att.name;
            wrap.appendChild(img);

            const rm = document.createElement("button");
            rm.className = "attach-thumb-remove";
            rm.textContent = "×";
            rm.title = "Remove";
            rm.onclick = (e) => {
                e.stopPropagation();
                this.pendingAttachments.splice(i, 1);
                this.renderAttachments();
            };
            wrap.appendChild(rm);

            strip.appendChild(wrap);
        });

        // A staged demo reference rides in the same strip as attachments, so
        // it inherits the "you can see it before you send, and remove it"
        // behaviour rather than being an invisible mode.
        if (this.pendingDemoRef) {
            const chip = document.createElement("div");
            chip.className = "attach-demo-chip";

            const label = document.createElement("span");
            const sel = this.pendingDemoRef.selection;
            label.textContent = sel && sel.text
                ? `▶ ${sel.text.slice(0, 28)}`
                : `▶ ${this.pendingDemoRef.title || "demo"}`;
            label.title = this.pendingDemoRef.title || "";
            chip.appendChild(label);

            // Its own class, not .attach-thumb-remove: that one is absolutely
            // positioned for the corner of a 56px image thumbnail, so on an
            // inline chip it lands on top of the label.
            const rm = document.createElement("button");
            rm.className = "attach-demo-remove";
            rm.textContent = "×";
            rm.title = "Don't ask about this demo";
            rm.onclick = (e) => {
                e.stopPropagation();
                this.setDemoRef(null);
            };
            chip.appendChild(rm);

            strip.appendChild(chip);
        }
    }

    /** Stage (or clear) a demo the next message is asking about. */
    setDemoRef(ref) {
        this.pendingDemoRef = ref;
        this.renderAttachments();
    }

    clearAttachments() {
        this.pendingAttachments = [];
        this.pendingDemoRef = null;
        this.renderAttachments();
        // Reset the picker too, or re-choosing the same file fires no change.
        if (this.attachInputEl) this.attachInputEl.value = "";
    }

    async send() {
        const email = getEmail();
        if (!email) {
            addMessage("Please enter your email first.", "error", this.messagesEl);
            return;
        }

        const message = this.inputEl.value.trim();
        const attachments = this.pendingAttachments.slice();
        const demoRef = this.pendingDemoRef;
        // An image on its own is a complete question ("what's wrong here?"),
        // so only bail when there's neither text nor an image.
        if (!message && !attachments.length) return;
        if (this.streaming) return;   // one turn at a time per surface

        this.inputEl.value = "";
        this.inputEl.style.height = "auto";
        this.clearAttachments();
        if (this.isMain) currentEmail = email;

        addMessage(message, "user", this.messagesEl, attachments);

        this.sendBtnEl.disabled = true;
        this.inputEl.disabled = true;
        this.streaming = true;
        if (this.isMain) {
            isStreaming = true;
            setStatus("Thinking...", false);
        }

        // Whether this turn is the one that names the session.
        //
        // NOT `!this.sessionId`: the "+" button pre-creates a session with a
        // placeholder id, so sessionId is already set by the time the first
        // message is sent — which is why every session created that way
        // stayed called "New Session". Count turns instead.
        this.turnCount += 1;
        const isFirstMessage = this.turnCount === 1;

        let assistantDiv = null;
        let assistantText = "";
        // Text accumulated from live deltas for the *current* bubble. The
        // backend sends deltas AND a final assistant_message with the same
        // full text, so this tracks what we've already rendered — the final
        // message replaces it rather than appending, otherwise every reply
        // renders twice.
        let deltaText = "";

        showThinking(this.messagesEl);

        try {
            const requestBody = { message, email };
            // Always send an explicit session id when we have one: the backend
            // otherwise falls back to the user's "active session" pointer,
            // which two concurrent chats would fight over.
            if (this.sessionId) requestBody.session_id = this.sessionId;
            if (this.forkFrom) requestBody.fork_from = this.forkFrom;
            if (attachments.length) {
                requestBody.attachments = attachments.map(a => ({
                    data: a.data,
                    content_type: a.contentType,
                    filename: a.name,
                }));
            }
            // Just the id and what was highlighted — the server builds the
            // digest. Sending html from here would put ~4000 tokens into the
            // transcript for the rest of the session.
            if (demoRef && demoRef.demo_id) {
                requestBody.demo_ref = {
                    demo_id: demoRef.demo_id,
                    selection: demoRef.selection || null,
                };
            }

            const res = await fetch(`${API_BASE}/api/chat/stream`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(requestBody),
            });

            // Without this, a 500 falls into the reader loop, yields zero
            // events, and the turn ends silently with an empty bubble.
            if (!res.ok) {
                throw new Error(`Server returned ${res.status} ${res.statusText}`);
            }

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split("\n");
                buffer = lines.pop(); // Keep incomplete line in buffer

                for (const line of lines) {
                    if (!line.startsWith("data: ")) continue;

                    let event;
                    try {
                        event = JSON.parse(line.slice(6));
                    } catch {
                        continue;
                    }

                    if (this.isMain) addEventLog(event);

                    switch (event.type) {
                        case "session_init":
                            if (event.session_id) {
                                this.sessionId = event.session_id;
                                // The fork only happens on the first turn;
                                // afterwards we resume our own session.
                                this.forkFrom = null;
                                if (this.isMain) currentSessionId = event.session_id;
                                if (this.onSessionId) this.onSessionId(event.session_id);
                                // Let the notes pane follow a branch: editing a
                                // message forks a new id, and a note pinned to
                                // the old one would otherwise disappear.
                                if (this.isMain && typeof notesOnSessionChange === "function") {
                                    notesOnSessionChange(event.session_id);
                                }
                                // Auto-name the session with first message.
                                // Side chats opt out: onSessionId has already
                                // named this row after the tangent's topic, and
                                // their opener is a seeded 'About "x" — ' prefix
                                // that would make a worse name than the topic.
                                if (isFirstMessage && this.isMain) {
                                    // An image-only opening turn has no text
                                    // to name from — don't PATCH a blank name.
                                    const sessionName = message.substring(0, 40)
                                        || (attachments.length ? "Image question" : "");
                                    setChatTitle(sessionName);
                                    try {
                                        await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(email)}/${event.session_id}`, {
                                            method: "PATCH",
                                            headers: { "Content-Type": "application/json" },
                                            body: JSON.stringify({ name: sessionName }),
                                        });
                                    } catch (e) {
                                        // Non-critical
                                    }
                                }
                            }
                            // Don't clobber the thinking indicator's status
                            // while the turn is still running — the session
                            // label is set once the turn completes.
                            break;

                        case "assistant_delta":
                            hideThinking(this.messagesEl);
                            if (!assistantDiv) {
                                assistantDiv = addMessage("", "assistant", this.messagesEl);
                            }
                            deltaText += event.content || "";
                            assistantText += event.content || "";
                            // Render as PLAIN TEXT while streaming. Deltas
                            // split mid-token, so the buffer is usually a
                            // half-finished markdown document (unclosed ```
                            // fence, heading with no trailing newline).
                            // Parsing that collapses the structure and makes
                            // ## and ** show up literally. Markdown is applied
                            // once the full block lands below.
                            assistantDiv.classList.add("streaming");
                            assistantDiv.textContent = assistantText;
                            scrollChatIfPinned(this.messagesEl);
                            break;

                        case "assistant_message":
                            hideThinking(this.messagesEl);
                            if (!assistantDiv) {
                                assistantDiv = addMessage("", "assistant", this.messagesEl);
                            }
                            // Reconcile with what deltas already rendered. The
                            // final message repeats the same text, so subtract
                            // the streamed portion instead of appending again.
                            if (deltaText) {
                                assistantText = assistantText.slice(0, assistantText.length - deltaText.length);
                                deltaText = "";
                            }
                            assistantText += event.content || "";
                            // Complete block — safe to parse markdown now.
                            assistantDiv.classList.remove("streaming");
                            assistantDiv.innerHTML = renderMarkdown(assistantText);
                            enrichMessage(assistantDiv);
                            scrollChatIfPinned(this.messagesEl);
                            break;

                        case "tool_call":
                            hideThinking(this.messagesEl);
                            // Finalize current assistant markdown before card
                            if (assistantDiv && assistantText) {
                                assistantDiv.classList.remove("streaming");
                                assistantDiv.innerHTML = renderMarkdown(assistantText);
                                enrichMessage(assistantDiv);
                            }
                            addToolCard(event.tool_name, event.tool_input, event.tool_use_id, this.messagesEl);
                            // A demo requested here is built in the background;
                            // the chip is driven by the job poller once the
                            // tool_result hands us a job_id. Legacy save_demo
                            // cards (built inline, before background builds)
                            // still resolve by title.
                            if (this.isMain && cleanToolName(event.tool_name) === "save_demo"
                                && typeof onDemoSaved === "function") {
                                onDemoSaved(null, (event.tool_input || {}).title);
                            }
                            // Reset assistant div for post-tool text
                            assistantDiv = null;
                            assistantText = "";
                            deltaText = "";
                            // Tool calls can run long — show the indicator
                            // again so the wait isn't silent.
                            showThinking(this.messagesEl);
                            break;

                        case "tool_result":
                            // These DO arrive for MCP tools; the backend used
                            // to drop them because the SDK wraps them in a
                            // UserMessage (no `.type`), so this looked dead.
                            // Without it a failed tool renders as a success.
                            if (event.is_error) {
                                markToolCardFailed(
                                    event.tool_use_id,
                                    event.content,
                                    this.messagesEl,
                                );
                            } else if (this.isMain) {
                                // request_demo returns the job id here — the
                                // call itself has no way to know it. This is
                                // what binds the chip to the background build.
                                attachDemoJob(
                                    event.tool_use_id,
                                    event.content,
                                    this.messagesEl,
                                );
                            }
                            break;

                        case "result":
                            // Skip duplicate result content — already captured
                            // via assistant_message
                            break;

                        case "error":
                            hideThinking(this.messagesEl);
                            addMessage(event.content, "error", this.messagesEl);
                            break;

                        case "done":
                            break;
                    }
                }
            }
        } catch (err) {
            addMessage(`Connection error: ${err.message}`, "error", this.messagesEl);
        } finally {
            hideThinking(this.messagesEl);

            // Teardown must run even if the render below throws (marked.parse
            // on arbitrary model output can), otherwise the input stays
            // disabled and the UI soft-locks permanently.
            try {
                // Final markdown pass — also converts the plain-text streaming
                // buffer if the turn ended on deltas with no final message.
                if (assistantDiv && assistantText) {
                    assistantDiv.classList.remove("streaming");
                    assistantDiv.innerHTML = renderMarkdown(assistantText);
                    enrichMessage(assistantDiv);
                    // Re-attach: every innerHTML write above destroys child
                    // nodes, so the launcher can only be added once the text
                    // has settled. Must follow enrichMessage for the same
                    // reason — mermaid replaces nodes inside this subtree.
                    if (this.isMain) attachSideChatLauncher(assistantDiv);
                }

                // No assistant text means the message never got through — put
                // it back in the box so it isn't lost.
                if (!assistantText.trim()) {
                    if (!this.inputEl.value.trim()) {
                        this.inputEl.value = message;
                        autoResizeInput(this.inputEl);
                    }
                    // Restore the images too, or a failed turn silently eats
                    // them and the retry sends text with no picture.
                    if (attachments.length && !this.pendingAttachments.length) {
                        this.pendingAttachments = attachments;
                        this.renderAttachments();
                    }
                    // Same for the demo reference — otherwise the retry asks
                    // "why doesn't this work" about nothing in particular.
                    if (demoRef && !this.pendingDemoRef) {
                        this.setDemoRef(demoRef);
                    }
                    addMessage("Your message was not sent — it's back in the box, press Send to retry.", "error", this.messagesEl);
                }
            } finally {
                this.sendBtnEl.disabled = false;
                this.inputEl.disabled = false;
                this.streaming = false;
                this.inputEl.focus();

                // Once a conversation has a real subject, replace the
                // first-message title with one drawn from what was actually
                // covered — an opener is often just "hi", and topics drift.
                // Fire-and-forget: a failed retitle just leaves the old name.
                //
                // Deliberately OUTSIDE the isMain guard below. This is a POST
                // that touches no DOM, so a side chat can rename itself without
                // disturbing the main conversation — and it matters more here,
                // since a tangent is otherwise stuck with its seeded topic.
                if (currentEmail && this.turnCount === RETITLE_AFTER_TURNS && this.sessionId) {
                    retitleSession(currentEmail, this.sessionId);
                }

                if (this.isMain) {
                    isStreaming = false;
                    setStatus(this.sessionId ? `Session: ${this.sessionId.substring(0, 8)}...` : "Ready", true);
                    // These four are main-chat only: loadSessions calls
                    // loadSessionHistory, which wipes and rebuilds the main
                    // message list. Doing that from a side-chat would destroy
                    // the main conversation's DOM mid-stream.
                    if (currentEmail) {
                        loadSessions(currentEmail);
                        refreshKnowledge(currentEmail);
                        refreshEpisodes(currentEmail);
                        refreshQuizzes(currentEmail);
                    }
                }
            }
        }
    }
}

// The main chat. Created lazily so the DOM is guaranteed to exist.
let mainChat = null;
// User-turn count of the session currently loaded into the main chat.
let loadedUserTurnCount = 0;

function getMainChat() {
    if (!mainChat) {
        mainChat = new ChatController({
            messagesEl: document.getElementById("chat-messages"),
            inputEl: document.getElementById("message-input"),
            sendBtnEl: document.getElementById("send-btn"),
            isMain: true,
            attachStripEl: document.getElementById("attach-strip"),
            attachBtnEl: document.getElementById("attach-btn"),
            attachInputEl: document.getElementById("attach-input"),
        });
    }
    // Keep in sync with session switching, which mutates the global. Changing
    // session resets the turn counter, so an existing conversation is never
    // re-named by the naming rule meant for a brand-new one.
    if (mainChat.sessionId !== currentSessionId) {
        mainChat.sessionId = currentSessionId;
        // Seeded from the loaded history (see loadSessionHistory) so an
        // existing conversation is never re-named by the first-turn rule.
        mainChat.turnCount = currentSessionId ? (loadedUserTurnCount || 99) : 0;
    }
    return mainChat;
}

// Preserved as a global for the inline onclick/onkeydown handlers in index.html.
async function sendMessage() {
    return getMainChat().send();
}

// =====================================================================
// Side chat — a second ChatController in a floating window.
//
// Runs against its own forked session, so the main chat stays clean and
// both can stream at the same time.
// =====================================================================

let sideChat = null;          // ChatController instance
let sideChatParentId = null;  // session it was forked from
let sideChatTopic = "";       // what the tangent is about

function sideChatEl() { return document.getElementById("side-chat"); }
function sideMessagesEl() { return document.getElementById("side-chat-messages"); }

function getSideChat() {
    if (!sideChat) {
        sideChat = new ChatController({
            messagesEl: sideMessagesEl(),
            inputEl: document.getElementById("side-chat-input"),
            sendBtnEl: document.getElementById("side-chat-send"),
            isMain: false,
            onSessionId: registerSideChatSession,
            attachStripEl: document.getElementById("side-attach-strip"),
            attachBtnEl: document.getElementById("side-attach-btn"),
            attachInputEl: document.getElementById("side-attach-input"),
        });
    }
    return sideChat;
}

/**
 * Give the forked side-chat session a row of its own, as soon as the SDK
 * reports its id.
 *
 * Without this the tangent has no entry in user.json until the agent finishes
 * the turn, so the generic first-message rename 404s and the session lands
 * with the server default "New Session". Registering here also supplies the
 * `kind` and `parent_session_id` the sidebar already knows how to nest — the
 * topic label is the one openSideChat computed for the window header.
 *
 * Best-effort: a tangent that fails to register still works, it just reads as
 * an ordinary session.
 */
async function registerSideChatSession(sessionId) {
    if (!currentEmail || !sessionId) return;
    try {
        await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(currentEmail)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                session_id: sessionId,
                name: sideChatTopic
                    ? `Side: ${sideChatTopic.substring(0, 40)}`
                    : "Side chat",
                kind: "side_chat",
                parent_session_id: sideChatParentId || "",
            }),
        });
    } catch (e) {
        // Non-critical — the tangent still runs, just unlabelled.
    }
}

async function sendSideMessage() {
    return getSideChat().send();
}

/**
 * Open the side chat, branching from the main conversation.
 *
 * `topic` seeds the window title and the eventual summary. The fork itself
 * happens lazily on the first message, so opening the window costs nothing.
 */
function openSideChat(topic = "") {
    const el = sideChatEl();
    const chat = getSideChat();

    // A fresh tangent starts a fresh session; an already-running one is just
    // re-revealed so its history survives being minimised.
    if (!chat.sessionId) {
        sideChatParentId = currentSessionId;
        chat.forkFrom = currentSessionId;   // inherit main-chat context
        sideChatTopic = topic || "";
        if (topic) {
            sideMessagesEl().innerHTML = "";
            addMessage(`Tangent: ${topic}`, "user", sideMessagesEl());
            chat.inputEl.value = `About "${topic}" — `;
        }
    }

    document.getElementById("side-chat-title").textContent =
        sideChatTopic ? `Side chat — ${sideChatTopic.substring(0, 40)}` : "Side chat";

    el.classList.add("open");
    chat.inputEl.focus();
    // Put the caret at the end of the seeded prompt.
    const len = chat.inputEl.value.length;
    chat.inputEl.setSelectionRange(len, len);
}

/** Hide the window without discarding the conversation. */
function closeSideChat() {
    sideChatEl().classList.remove("open");
}

/**
 * Finish the tangent: summarize it into the main chat, record the learning,
 * and reset the window for the next one. The transcript itself is kept — the
 * side chat remains a real session, reachable from the sidebar.
 */
async function endSideChat() {
    const chat = getSideChat();
    const email = getEmail();

    if (!chat.sessionId) {
        // Nothing was ever asked; just close.
        closeSideChat();
        return;
    }
    if (chat.streaming) {
        addMessage("Still answering — wait for it to finish before ending.", "error", sideMessagesEl());
        return;
    }

    const endBtn = document.getElementById("side-chat-end");
    endBtn.disabled = true;
    endBtn.textContent = "Summarizing…";
    showThinking(sideMessagesEl());

    try {
        const res = await fetch(
            `${API_BASE}/api/sessions/${encodeURIComponent(email)}/${chat.sessionId}/end-side`,
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    parent_session_id: sideChatParentId || currentSessionId,
                    topic: sideChatTopic || "",
                }),
            }
        );
        if (!res.ok) throw new Error(`Server returned ${res.status} ${res.statusText}`);
        const data = await res.json();

        // Fold the card into the main conversation.
        renderSideSummaryCard({
            content: data.summary,
            topic: data.topic || sideChatTopic,
            side_session_id: data.side_session_id,
        });

        // Reset so the next tangent starts a fresh session.
        chat.sessionId = null;
        chat.forkFrom = null;
        sideChatTopic = "";
        sideChatParentId = null;
        sideMessagesEl().innerHTML =
            '<div class="empty-state side-chat-hint">Ask about a tangent here — the main chat stays clean.</div>';
        closeSideChat();

        // The knowledge graph likely changed.
        if (currentEmail) refreshKnowledge(currentEmail);
    } catch (err) {
        addMessage(`Could not summarize: ${err.message}`, "error", sideMessagesEl());
    } finally {
        hideThinking(sideMessagesEl());
        endBtn.disabled = false;
        endBtn.textContent = "End & summarize";
    }
}

// =====================================================================
// Edit → branch
//
// The model can't un-remember, so editing forks a new session that never saw
// the original wording. The old branch stays in the sidebar, switchable.
// =====================================================================

/** Add a ✎ affordance to a stored user message. */
function attachEditControl(messageDiv) {
    if (!messageDiv || messageDiv.querySelector(".msg-edit-btn")) return;
    const btn = document.createElement("button");
    btn.className = "msg-edit-btn";
    btn.textContent = "✎";
    btn.title = "Edit this message and re-ask from here";
    btn.onclick = (e) => {
        e.stopPropagation();
        beginEditMessage(messageDiv);
    };
    messageDiv.appendChild(btn);
}

/** Swap the bubble for a textarea seeded with the original text. */
function beginEditMessage(messageDiv) {
    if (messageDiv.querySelector("textarea")) return;   // already editing
    const original = messageDiv.dataset.originalText || messageDiv.textContent.replace(/✎$/, "").trim();
    messageDiv.dataset.originalText = original;

    messageDiv.innerHTML = "";
    messageDiv.classList.add("editing");

    const ta = document.createElement("textarea");
    ta.className = "msg-edit-input";
    ta.value = original;
    ta.rows = 1;

    const actions = document.createElement("div");
    actions.className = "msg-edit-actions";
    const save = document.createElement("button");
    save.textContent = "Save & re-ask";
    save.onclick = () => submitEdit(messageDiv, ta.value.trim());
    const cancel = document.createElement("button");
    cancel.textContent = "Cancel";
    cancel.onclick = () => cancelEdit(messageDiv);
    actions.append(save, cancel);

    messageDiv.append(ta, actions);
    ta.focus();
    autoResizeInput(ta);
    ta.addEventListener("input", () => autoResizeInput(ta));
    ta.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save.click(); }
        if (e.key === "Escape") cancelEdit(messageDiv);
    });
}

function cancelEdit(messageDiv) {
    messageDiv.classList.remove("editing");
    messageDiv.textContent = messageDiv.dataset.originalText || "";
    attachEditControl(messageDiv);
}

/**
 * Branch the conversation at this turn, then stream the edited message into
 * the new session. Everything from the edit point on is replaced.
 */
async function submitEdit(messageDiv, newText) {
    const email = getEmail();
    const turnIndex = parseInt(messageDiv.dataset.turnIndex, 10);
    if (!newText || !email || !turnIndex) return cancelEdit(messageDiv);
    if (isStreaming) {
        addMessage("Wait for the current reply to finish before editing.", "error");
        return;
    }

    messageDiv.classList.remove("editing");
    messageDiv.textContent = newText;

    // Drop everything after the edited message — it belongs to the old branch.
    const container = mainMessagesEl();
    let node = messageDiv.nextSibling;
    while (node) { const next = node.nextSibling; node.remove(); node = next; }

    setStatus("Branching…", false);
    showThinking(container);
    try {
        const res = await fetch(
            `${API_BASE}/api/sessions/${encodeURIComponent(email)}/${currentSessionId}/branch`,
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ turn_index: turnIndex, message: newText }),
            }
        );
        if (!res.ok) throw new Error(`Server returned ${res.status} ${res.statusText}`);
        const data = await res.json();

        hideThinking(container);

        // Point the main chat at the branch, then send the edited message.
        if (data.session_id) currentSessionId = data.session_id;
        const chat = getMainChat();
        chat.sessionId = data.session_id || null;
        chat.inputEl.value = newText;
        await chat.send();
        // The message we just re-sent is already shown above.
        const dupes = container.querySelectorAll(".message.user");
        if (dupes.length >= 2) {
            const last = dupes[dupes.length - 1];
            if (last !== messageDiv && last.textContent.trim() === newText) last.remove();
        }
    } catch (err) {
        hideThinking(container);
        addMessage(`Could not branch: ${err.message}`, "error");
        setStatus("Ready", true);
    }
}

/** Render the collapsible card that represents a finished side chat. */
function renderSideSummaryCard(turn, container) {
    container = container || mainMessagesEl();
    const card = document.createElement("div");
    card.className = "side-summary-card";

    const topic = turn.topic || (turn.topics && turn.topics[0]) || "a tangent";
    card.innerHTML = `
        <div class="side-summary-head">
            <span>⤴ side chat</span>
            <span class="side-summary-topic">${escapeHtml(topic)}</span>
        </div>
        <div class="side-summary-body">${renderMarkdown(turn.content || "")}</div>
    `;
    container.appendChild(card);
    // Interpolated into a template above rather than assigned to an
    // element, so this can only be enriched once the card exists.
    enrichMessage(card.querySelector(".side-summary-body"));
    scrollChatIfPinned(container);
    return card;
}

/** A demo the learner pinned into the conversation.
 *
 * Rendered like the demo chip so it reads as the same kind of object, and
 * replayed from the transcript so it survives a refresh.
 */
function renderDemoPinCard(turn, container) {
    container = container || mainMessagesEl();
    const card = document.createElement("div");
    card.className = "tool-card is-demo is-ready";
    const version = (turn.version || 1) > 1 ? ` v${turn.version}` : "";
    card.innerHTML = `
        <div class="tool-header">
            <span class="tool-icon">📌</span>
            <span class="tool-name">demo</span>
            <span class="tool-summary">${escapeHtml((turn.title || "demo") + version)}</span>
            <button class="demo-open-btn" type="button">Open</button>
        </div>
    `;
    const btn = card.querySelector(".demo-open-btn");
    if (btn) {
        btn.onclick = (e) => {
            e.stopPropagation();
            if (typeof openDemoById === "function") openDemoById(turn.demo_id);
        };
    }
    container.appendChild(card);
    scrollChatIfPinned(container);
    return card;
}

/** Launch a side chat about a specific assistant message. */
function startSideChatFrom(button) {
    const msg = button.closest(".message");
    const text = (msg ? msg.textContent : "").replace(/^⤴.*?tangent/i, "").trim();
    openSideChat(text.substring(0, 60));
}

/**
 * Attach a "⤴ tangent" launcher to an assistant message. Hidden until the
 * message is hovered, so it doesn't clutter the transcript.
 */
function attachSideChatLauncher(messageDiv) {
    if (!messageDiv || messageDiv.querySelector(".side-chat-launch")) return;
    const btn = document.createElement("button");
    btn.className = "side-chat-launch";
    btn.textContent = "⤴ tangent";
    btn.title = "Ask about something in this message without derailing the lesson";
    btn.onclick = (e) => {
        e.stopPropagation();
        startSideChatFrom(btn);
    };
    messageDiv.appendChild(btn);
}

// --- Dragging and resizing -------------------------------------------
// Pointer events (not mouse) so it works with trackpad and touch alike;
// setPointerCapture keeps the drag alive if the cursor outruns the header.

function initSideChatWindow() {
    const win = sideChatEl();
    const header = document.getElementById("side-chat-header");
    const handles = win ? win.querySelectorAll(".side-chat-resize") : [];
    if (!win || !header) return;

    let mode = null;         // "drag" | "resize"
    let dir = "";            // for resize: any of n/s/e/w, combined at corners
    let startX = 0, startY = 0, startLeft = 0, startTop = 0, startW = 0, startH = 0;

    /**
     * Switch the window from its CSS right/bottom berth to absolute left/top.
     *
     * This is load-bearing for BOTH gestures. While the window is still
     * right/bottom-anchored, growing width/height expands it leftward and
     * upward — so a resize appears to run away from the pointer. Pinning
     * left/top first makes the geometry unambiguous. Idempotent.
     */
    function pinToLeftTop() {
        const r = win.getBoundingClientRect();
        win.style.left = r.left + "px";
        win.style.top = r.top + "px";
        win.style.right = "auto";
        win.style.bottom = "auto";
    }

    /**
     * Read the size bounds from CSS rather than duplicating them here.
     * Resolved per gesture so viewport-relative values (50vw / 100vh) are
     * correct even if the browser was resized since load.
     */
    function bounds() {
        const cs = getComputedStyle(win);
        const px = (v, fallback) => {
            const n = parseFloat(v);
            return Number.isFinite(n) && n > 0 ? n : fallback;
        };
        return {
            minW: px(cs.minWidth, 300),
            minH: px(cs.minHeight, 240),
            maxW: px(cs.maxWidth, window.innerWidth),
            maxH: px(cs.maxHeight, window.innerHeight),
        };
    }

    function beginDrag(e) {
        // Ignore clicks on the header buttons.
        if (e.target.closest("button")) return;
        pinToLeftTop();
        const r = win.getBoundingClientRect();
        mode = "drag";
        startX = e.clientX; startY = e.clientY;
        startLeft = r.left; startTop = r.top;
        header.setPointerCapture(e.pointerId);
    }

    function beginResize(e) {
        pinToLeftTop();
        const r = win.getBoundingClientRect();
        mode = "resize";
        dir = e.currentTarget.dataset.dir || "";
        startX = e.clientX; startY = e.clientY;
        startLeft = r.left; startTop = r.top;
        startW = r.width; startH = r.height;
        e.currentTarget.setPointerCapture(e.pointerId);
        e.stopPropagation();
    }

    function onMove(e) {
        if (!mode) return;
        const dx = e.clientX - startX, dy = e.clientY - startY;

        if (mode === "drag") {
            // Clamp so the window can never be dragged fully off-screen and
            // become unreachable.
            const maxL = window.innerWidth - win.offsetWidth;
            const maxT = window.innerHeight - win.offsetHeight;
            win.style.left = Math.max(0, Math.min(startLeft + dx, maxL)) + "px";
            win.style.top = Math.max(0, Math.min(startTop + dy, maxT)) + "px";
            return;
        }

        const b = bounds();
        let w = startW, h = startH, left = startLeft, top = startTop;

        // Size first, position second. Deriving left/top from the *clamped*
        // size is what makes a west/north drag stop dead at the minimum
        // instead of sliding the whole window across the screen.
        if (dir.includes("e")) {
            w = startW + dx;
        } else if (dir.includes("w")) {
            w = startW - dx;
        }
        if (dir.includes("s")) {
            h = startH + dy;
        } else if (dir.includes("n")) {
            h = startH - dy;
        }

        // Keep the window inside the viewport: a west/north edge can't cross
        // the screen edge, and an east/south edge can't run past it.
        if (dir.includes("w")) w = Math.min(w, startLeft + startW);
        if (dir.includes("n")) h = Math.min(h, startTop + startH);
        if (dir.includes("e")) w = Math.min(w, window.innerWidth - startLeft);
        if (dir.includes("s")) h = Math.min(h, window.innerHeight - startTop);

        w = Math.max(b.minW, Math.min(w, b.maxW));
        h = Math.max(b.minH, Math.min(h, b.maxH));

        if (dir.includes("w")) left = startLeft + startW - w;
        if (dir.includes("n")) top = startTop + startH - h;

        win.style.width = w + "px";
        win.style.height = h + "px";
        win.style.left = Math.max(0, left) + "px";
        win.style.top = Math.max(0, top) + "px";
    }

    function endDrag() { mode = null; dir = ""; }

    header.addEventListener("pointerdown", beginDrag);
    header.addEventListener("pointermove", onMove);
    header.addEventListener("pointerup", endDrag);
    // A cancelled gesture (OS/browser interruption) must clear `mode` too,
    // or the window keeps following the cursor with no button held.
    header.addEventListener("pointercancel", endDrag);

    handles.forEach(handle => {
        handle.addEventListener("pointerdown", beginResize);
        handle.addEventListener("pointermove", onMove);
        handle.addEventListener("pointerup", endDrag);
        handle.addEventListener("pointercancel", endDrag);
    });

    // Shrinking the browser must not strand a manually-placed window
    // off-screen. Size caps are handled by CSS max-width/max-height.
    window.addEventListener("resize", () => {
        if (win.style.left === "" || !win.classList.contains("open")) return;
        const r = win.getBoundingClientRect();
        win.style.left = Math.max(0, Math.min(r.left, window.innerWidth - r.width)) + "px";
        win.style.top = Math.max(0, Math.min(r.top, window.innerHeight - r.height)) + "px";
    });
}

function setStatus(text, connected) {
    const el = document.getElementById("status");
    el.textContent = text;
    el.className = `status-indicator${connected ? " connected" : ""}`;
}

// =====================================================================
// Knowledge Dashboard
// =====================================================================

async function refreshKnowledge(email) {
    try {
        const res = await fetch(`${API_BASE}/api/knowledge/${encodeURIComponent(email)}`);
        if (!res.ok) return;
        const data = await res.json();
        renderProgress(data);
    } catch (e) {
        console.error("Failed to refresh knowledge:", e);
    }
}

// =====================================================================
// Progress groups — collapse state
//
// Held outside the DOM on purpose. renderProgress() replaces the panel's
// innerHTML, and it runs after every chat turn (refreshKnowledge is called
// from the streaming completion path among others), so state stored in the
// markup would silently reset mid-lesson each time the tutor updated a
// concept.
// =====================================================================

function collapsedGroupsKey() {
    const email = getEmail();
    return email ? `pymentor:progress-collapsed:${email}` : "pymentor:progress-collapsed:_anon";
}

function loadCollapsedGroups() {
    try {
        const raw = localStorage.getItem(collapsedGroupsKey());
        return new Set(raw ? JSON.parse(raw) : []);
    } catch (e) {
        return new Set();
    }
}

function saveCollapsedGroups(set) {
    try {
        localStorage.setItem(collapsedGroupsKey(), JSON.stringify([...set]));
    } catch (e) {}
}

/**
 * Collapse or expand one mastery group.
 *
 * Toggles the class on the existing element rather than re-rendering: a full
 * re-render would rebuild the whole panel and lose the scroll position.
 */
function toggleConceptGroup(level) {
    if (!level) return;
    const collapsed = loadCollapsedGroups();
    const nowCollapsed = !collapsed.has(level);
    if (nowCollapsed) collapsed.add(level);
    else collapsed.delete(level);
    saveCollapsedGroups(collapsed);

    const group = document.querySelector(`.concept-group.${level}`);
    if (group) group.classList.toggle("is-collapsed", nowCollapsed);
    const btn = group && group.querySelector(".group-toggle");
    if (btn) {
        btn.setAttribute("aria-expanded", String(!nowCollapsed));
        const caret = btn.querySelector(".group-caret");
        if (caret) caret.textContent = nowCollapsed ? "▸" : "▾";
    }
}

function initProgressGroups() {
    const container = document.getElementById("progress-content");
    if (!container || container.dataset.groupsInit === "1") return;
    container.dataset.groupsInit = "1";
    // Delegated: the panel's innerHTML is replaced on every refresh, so a
    // listener bound to the buttons themselves would be destroyed with them.
    container.addEventListener("click", (e) => {
        const btn = e.target.closest(".group-toggle");
        if (btn) toggleConceptGroup(btn.dataset.group);
    });
}

function renderProgress(data) {
    const concepts = data.concepts || {};
    const conceptList = Object.values(concepts);

    if (conceptList.length === 0) return;

    document.getElementById("progress-empty").style.display = "none";
    const container = document.getElementById("progress-content");
    container.style.display = "block";

    // Group by mastery
    const groups = { mastered: [], practiced: [], introduced: [] };
    for (const c of conceptList) {
        const m = c.mastery || "introduced";
        if (groups[m]) groups[m].push(c);
    }

    const profile = data.profile || {};
    let html = `<div class="progress-header">
        <div class="progress-level">
            <span class="level-label">Level</span>
            <span class="level-value">${profile.level || "beginner"}</span>
        </div>
        <div class="progress-count">
            <span class="count-number">${conceptList.length}</span>
            <span class="count-label">concepts</span>
        </div>
    </div>`;

    const collapsed = loadCollapsedGroups();
    for (const [level, items] of Object.entries(groups)) {
        if (items.length === 0) continue;
        const isOpen = !collapsed.has(level);
        html += `<div class="concept-group ${level}${isOpen ? "" : " is-collapsed"}">`;
        // A real button, not a clickable heading: focusable and operable with
        // Enter/Space without any extra key handling. The count stays outside
        // the collapsible body so a closed group still says how much is in it.
        html += `<button type="button" class="group-toggle" data-group="${level}"
                         aria-expanded="${isOpen}">
                    <span class="group-caret">${isOpen ? "▾" : "▸"}</span>
                    ${level} <span class="count">(${items.length})</span>
                 </button>`;
        html += `<div class="concept-group-body">`;
        for (const c of items) {
            html += `
                <div class="concept-item">
                    <div class="concept-dot ${level}"></div>
                    <span class="concept-name">${escapeHtml(c.name || c.concept_id)}</span>
                    <span class="concept-category">${escapeHtml(c.category || "")}</span>
                </div>`;
        }
        html += `</div></div>`;
    }

    if (profile.strengths && profile.strengths.length > 0) {
        html += `<div class="profile-section">
            <span class="profile-label">Strengths:</span>
            ${profile.strengths.map(s => `<span class="topic-tag strength">${escapeHtml(s)}</span>`).join("")}
        </div>`;
    }
    if (profile.struggle_areas && profile.struggle_areas.length > 0) {
        html += `<div class="profile-section">
            <span class="profile-label">Needs work:</span>
            ${profile.struggle_areas.map(s => `<span class="topic-tag struggle">${escapeHtml(s)}</span>`).join("")}
        </div>`;
    }

    container.innerHTML = html;
}

async function refreshEpisodes(email) {
    try {
        const res = await fetch(`${API_BASE}/api/episodes/${encodeURIComponent(email)}`);
        if (!res.ok) return;
        const data = await res.json();
        renderEpisodes(data.episodes || []);
    } catch (e) {
        console.error("Failed to refresh episodes:", e);
    }
}

function renderEpisodes(episodes) {
    if (episodes.length === 0) return;

    document.getElementById("history-empty").style.display = "none";
    const container = document.getElementById("history-content");
    container.style.display = "block";

    let html = "";
    for (const ep of episodes) {
        const date = ep.timestamp ? ep.timestamp.substring(0, 16).replace("T", " ") : "unknown";
        const topics = (ep.topics || []).filter(t => t);
        html += `
            <div class="episode-item">
                <div class="episode-date">${escapeHtml(date)}</div>
                <div class="episode-summary">${escapeHtml(ep.summary)}</div>
                ${topics.length ? `<div class="episode-topics">${topics.map(t => `<span class="topic-tag">${escapeHtml(t)}</span>`).join("")}</div>` : ""}
            </div>`;
    }

    container.innerHTML = html;
}

async function refreshQuizzes(email) {
    try {
        const res = await fetch(`${API_BASE}/api/quiz/${encodeURIComponent(email)}`);
        if (!res.ok) return;
        const data = await res.json();
        renderQuizzes(data);
    } catch (e) {
        console.error("Failed to refresh quizzes:", e);
    }
}

function renderQuizzes(data) {
    const quizzes = data.quizzes || [];
    const stats = data.stats || {};

    if (quizzes.length === 0 && !stats.total_quizzes) return;

    document.getElementById("quizzes-empty").style.display = "none";
    const container = document.getElementById("quizzes-content");
    container.style.display = "block";

    let html = "";

    // Overall stats
    if (stats.total_quizzes > 0) {
        const accClass = stats.overall_accuracy >= 80 ? "good" : stats.overall_accuracy >= 50 ? "ok" : "poor";
        html += `
            <div class="quiz-stats">
                <h3>Overall Stats</h3>
                <div class="stat-row"><span class="stat-label">Total Quizzes</span><span class="stat-value">${stats.total_quizzes}</span></div>
                <div class="stat-row"><span class="stat-label">Questions</span><span class="stat-value">${stats.total_correct}/${stats.total_questions}</span></div>
                <div class="stat-row"><span class="stat-label">Accuracy</span><span class="stat-value ${accClass}">${stats.overall_accuracy}%</span></div>
            </div>`;
    }

    // Individual quizzes (most recent first). Each is click-to-expand:
    // the per-question detail (prompt, the learner's own answer, whether it
    // was right, and the concept tested) has always been stored by
    // record_quiz_result — it simply had no surface until now. Wrong answers
    // are the most useful thing here, so they are called out.
    const sorted = [...quizzes].reverse();
    for (const q of sorted.slice(0, 10)) {
        const pct = q.percentage || 0;
        const cls = pct >= 80 ? "good" : pct >= 50 ? "ok" : "poor";
        const date = q.timestamp ? q.timestamp.substring(0, 16).replace("T", " ") : "";
        const questions = q.questions || [];
        const missed = questions.filter(x => !x.correct).length;

        html += `
            <div class="quiz-item${questions.length ? " expandable" : ""}"
                 ${questions.length ? 'onclick="this.classList.toggle(\'expanded\')"' : ""}>
                <div class="quiz-item-head">
                    <span class="quiz-score ${cls}">${q.score}/${q.total} (${pct}%)</span>
                    ${questions.length
                        ? `<span class="quiz-expand-hint">${questions.length} question${questions.length === 1 ? "" : "s"}${missed ? ` · ${missed} missed` : ""}</span>`
                        : ""}
                </div>
                <div class="quiz-date">${escapeHtml(date)}</div>
                <div class="episode-topics" style="margin-top:4px">${(q.topics || []).map(t => `<span class="topic-tag">${escapeHtml(t)}</span>`).join("")}</div>
                ${questions.length ? `
                <div class="quiz-questions">
                    ${questions.map((x, i) => `
                        <div class="quiz-q ${x.correct ? "right" : "wrong"}">
                            <div class="quiz-q-head">
                                <span class="quiz-q-mark">${x.correct ? "✓" : "✗"}</span>
                                <span class="quiz-q-num">Q${i + 1}</span>
                                ${x.concept ? `<span class="topic-tag">${escapeHtml(x.concept)}</span>` : ""}
                            </div>
                            <div class="quiz-q-text">${escapeHtml(x.question || "")}</div>
                            ${x.user_answer ? `
                                <div class="quiz-q-answer">
                                    <span class="quiz-q-label">Your answer</span>
                                    ${escapeHtml(x.user_answer)}
                                </div>` : ""}
                        </div>`).join("")}
                </div>` : ""}
            </div>`;
    }

    container.innerHTML = html;
}

// =====================================================================
// Preferences Tab
// =====================================================================

let prefsInitialized = false;
let prefsAspects = [];

async function initPreferences() {
    const email = getEmail();
    if (!email) {
        document.getElementById("preferences-empty").style.display = "block";
        document.getElementById("preferences-content").style.display = "none";
        return;
    }
    document.getElementById("preferences-empty").style.display = "none";
    document.getElementById("preferences-content").style.display = "block";

    if (!prefsInitialized) {
        try {
            const res = await fetch(`${API_BASE}/api/feedback/aspects`);
            const data = await res.json();
            prefsAspects = data.aspects || [];
            const select = document.getElementById("prefs-aspect");
            select.innerHTML = prefsAspects
                .map(a => `<option value="${a}">${a.replace("_", " ")}</option>`)
                .join("");
            prefsInitialized = true;
        } catch (e) {
            console.error("Failed to load aspects:", e);
            return;
        }
    }
    loadPreferences();
}

async function loadPreferences() {
    const email = getEmail();
    if (!email) return;
    const aspect = document.getElementById("prefs-aspect").value;
    if (!aspect) return;

    document.getElementById("prefs-status").textContent = "";
    document.getElementById("prefs-global").textContent = "Loading...";
    document.getElementById("prefs-user").value = "";

    try {
        const res = await fetch(`${API_BASE}/api/feedback/${encodeURIComponent(email)}/${aspect}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        document.getElementById("prefs-global").textContent = data.global || "(empty)";
        document.getElementById("prefs-user").value = data.user || "";
    } catch (e) {
        console.error("Failed to load feedback:", e);
        document.getElementById("prefs-global").textContent = "(failed to load)";
        setPrefsStatus("Failed to load: " + e.message, true);
    }
}

async function savePreferences() {
    const email = getEmail();
    if (!email) return;
    const aspect = document.getElementById("prefs-aspect").value;
    const content = document.getElementById("prefs-user").value;

    const btn = document.getElementById("prefs-save-btn");
    btn.disabled = true;
    setPrefsStatus("Saving...", false);

    try {
        const res = await fetch(`${API_BASE}/api/feedback/${encodeURIComponent(email)}/${aspect}`, {
            method: "PUT",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({content}),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        setPrefsStatus("Saved", false);
    } catch (e) {
        console.error("Failed to save feedback:", e);
        setPrefsStatus("Failed: " + e.message, true);
    } finally {
        btn.disabled = false;
    }
}

function setPrefsStatus(text, isError) {
    const el = document.getElementById("prefs-status");
    el.textContent = text;
    // Tokens, not literals — hardcoding here would keep the dark-theme reds
    // and greens in light mode.
    el.style.color = isError ? "var(--error)" : "var(--success)";
}

// =====================================================================
// Notebook (in-browser code runner)
// =====================================================================

let cellCounter = 0;
let notebookOpen = false;

/**
 * POST `body` to `url` and invoke `onEvent` for each SSE `data:` frame.
 *
 * Pass an AbortSignal to cancel: dropping the connection is what the server
 * watches for to kill the cell's process group, so this is also the Stop
 * mechanism, not just a client-side tidy-up.
 *
 * The chat controller has its own copy of this loop. It isn't shared because
 * that one's event handling is bound up with per-turn controller state; this
 * helper serves the two code-runner call sites.
 */
async function streamSSE(url, body, onEvent, signal) {
    const res = await fetch(`${API_BASE}${url}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
    });

    if (!res.ok) {
        // Prefer FastAPI's {"detail": "..."} over dumping the raw JSON blob at
        // the learner, which is what the old runner did.
        let detail = `${res.status} ${res.statusText}`;
        try {
            const parsed = JSON.parse(await res.text());
            if (parsed && parsed.detail) detail = parsed.detail;
        } catch {}
        throw new Error(detail);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();   // keep the incomplete line

        for (const line of lines) {
            if (!line.startsWith("data: ")) continue;   // skips ": ping"
            let event;
            try {
                event = JSON.parse(line.slice(6));
            } catch {
                continue;
            }
            onEvent(event);
        }
    }
}

/**
 * Buffers streamed text and flushes on an animation frame.
 *
 * A cell in a tight print loop emits thousands of chunks; appending to the
 * DOM per chunk locks the tab up. Batching per frame keeps it responsive
 * while still looking live.
 */
function makeOutputSink(pre) {
    let pending = [];
    let scheduled = false;

    const flush = () => {
        scheduled = false;
        if (!pending.length) return;
        const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
        for (const [stream, text] of pending) {
            if (stream === "stderr") {
                // A span, not a class on the whole block: qiskit and
                // matplotlib write warnings to stderr routinely, and
                // reddening all the output because of a warning is wrong.
                const span = document.createElement("span");
                span.className = "stream-err";
                span.textContent = text;
                pre.appendChild(span);
            } else {
                pre.appendChild(document.createTextNode(text));
            }
        }
        pending = [];
        if (atBottom) pre.scrollTop = pre.scrollHeight;
    };

    return {
        write(stream, text) {
            if (!text) return;
            pending.push([stream, text]);
            if (!scheduled) {
                scheduled = true;
                requestAnimationFrame(flush);
            }
        },
        flush,
    };
}

function notebookKey() {
    const email = getEmail();
    return email ? `pymentor:cells:${email}` : "pymentor:cells:_anon";
}

// Cap on output persisted per cell. localStorage tops out around 5MB per
// origin and setItem throws on overflow, which would silently lose the whole
// notebook — so keep the tail of a chatty cell rather than all of it.
const MAX_SAVED_OUTPUT = 20_000;

let saveCellsTimer = null;

function saveCellsNow() {
    saveCellsTimer = null;
    const cells = [...document.querySelectorAll(".cell")].map(el => {
        const out = el.querySelector(".cell-output");
        let text = out.textContent;
        if (text.length > MAX_SAVED_OUTPUT) {
            text = "... [earlier output trimmed]\n" + text.slice(-MAX_SAVED_OUTPUT);
        }
        return {
            code: el.querySelector(".cell-textarea").value,
            output: text,
            status: el.dataset.status || "",
            isError: out.classList.contains("error"),
            images: [...el.querySelectorAll(".cell-image")].map(img => img.getAttribute("src")),
        };
    });
    try { localStorage.setItem(notebookKey(), JSON.stringify(cells)); } catch {}
}

/** Debounced: fires on every keystroke and, while streaming, every frame. */
function saveCells() {
    if (saveCellsTimer) return;
    saveCellsTimer = setTimeout(saveCellsNow, 300);
}

function loadCells() {
    const container = document.getElementById("cells");
    container.innerHTML = "";
    let stored = [];
    try { stored = JSON.parse(localStorage.getItem(notebookKey()) || "[]"); } catch {}
    if (stored.length === 0) {
        addCell("# Try me — Qiskit is installed\nfrom qiskit import QuantumCircuit\nqc = QuantumCircuit(2)\nqc.h(0)\nqc.cx(0, 1)\nprint(qc)");
        return;
    }
    for (const c of stored) {
        const el = addCell(c.code || "");
        if (c.output) {
            const out = el.querySelector(".cell-output");
            out.textContent = c.output;
            out.classList.remove("empty");
            if (c.isError) out.classList.add("error");
        }
        for (const src of c.images || []) {
            // Only the URL was persisted, never a data URI. The run directory
            // is swept after an hour, so an old image 404s — that's fine, it
            // just renders as a broken thumbnail on a stale cell.
            appendCellImage(el, src);
        }
        if (c.status) {
            el.dataset.status = c.status;
            const s = el.querySelector(".cell-status");
            s.textContent = c.status;
        }
    }
}

function appendCellImage(el, src) {
    const img = document.createElement("img");
    img.className = "cell-image";
    img.src = src;
    img.loading = "lazy";
    el.appendChild(img);
}

function toggleNotebook() {
    notebookOpen = !notebookOpen;
    document.getElementById("notebook-pane").style.display = notebookOpen ? "flex" : "none";
    const runBtn = document.getElementById("run-code-btn");
    if (runBtn) runBtn.classList.toggle("is-on", notebookOpen);
    // The divider serves whichever pane is open, so it has to re-evaluate
    // here too. Guarded: notes.js may not be loaded.
    if (typeof syncPaneSplit === "function") syncPaneSplit();
    // The notebook and the notes canvas both want half the centre column;
    // three panes at flex:1 leaves none of them usable.
    if (notebookOpen && typeof notesOpen !== "undefined" && notesOpen
        && typeof toggleNotes === "function") {
        toggleNotes(false);
    }
    if (notebookOpen && typeof demoOpen !== "undefined" && demoOpen
        && typeof toggleDemo === "function") {
        toggleDemo(false);
    }
    if (notebookOpen && document.querySelectorAll(".cell").length === 0) {
        loadCells();
    }
}

function addCell(initialCode = "") {
    cellCounter += 1;
    const id = `cell-${cellCounter}`;
    const container = document.getElementById("cells");
    const div = document.createElement("div");
    div.className = "cell";
    div.id = id;
    div.innerHTML = `
        <div class="cell-toolbar">
            <span class="cell-status"></span>
            <div class="cell-buttons">
                <button class="header-btn" data-action="run">Run</button>
                <button class="header-btn" data-action="send">Send to chat</button>
                <button class="header-btn" data-action="delete">Delete</button>
            </div>
        </div>
        <textarea class="cell-textarea" spellcheck="false" placeholder="# Python — a fresh process each run, so variables don't carry between cells.&#10;# Need a package? Put %pip install <name> at the top."></textarea>
        <pre class="cell-output empty"></pre>
    `;
    container.appendChild(div);
    const ta = div.querySelector(".cell-textarea");
    ta.value = initialCode;
    ta.addEventListener("input", () => {
        autoGrowCell(ta);
        saveCells();
    });
    ta.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            runCell(id);
            return;
        }
        handleEditorKeys(e, ta);
    });
    autoGrowCell(ta);
    div.querySelector('[data-action="run"]').onclick = () => runCell(id);
    div.querySelector('[data-action="send"]').onclick = () => sendCellToChat(id);
    div.querySelector('[data-action="delete"]').onclick = () => deleteCell(id);
    return div;
}

function deleteCell(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
    saveCells();
}

/** Cell timeout in seconds, from the notebook header control. */
function cellTimeout() {
    const sel = document.getElementById("cell-timeout");
    const v = sel ? parseFloat(sel.value) : NaN;
    return Number.isFinite(v) ? v : 15;
}

function setCellStatus(el, cls, text) {
    const status = el.querySelector(".cell-status");
    status.className = `cell-status ${cls}`;
    status.textContent = text;
    el.dataset.status = text;
}

/** Clear everything a previous run left behind on this cell. */
function resetCellOutput(el) {
    const out = el.querySelector(".cell-output");
    out.textContent = "";
    out.classList.remove("error", "empty");
    el.querySelectorAll(".cell-image, .install-suggestion").forEach(n => n.remove());
    return out;
}

async function runCell(id) {
    const email = getEmail();
    if (!email) {
        alert("Enter your email first — the runner uses a per-user venv.");
        return;
    }
    const el = document.getElementById(id);
    if (!el) return;

    // Second click on a running cell is Stop. Aborting drops the HTTP
    // connection; the server notices and kills the cell's process group.
    if (el._abort) {
        el._abort.abort();
        return;
    }

    const code = el.querySelector(".cell-textarea").value;
    const out = resetCellOutput(el);
    const sink = makeOutputSink(out);
    const runBtn = el.querySelector('[data-action="run"]');

    const controller = new AbortController();
    el._abort = controller;
    runBtn.textContent = "Stop";
    runBtn.classList.add("is-stop");
    setCellStatus(el, "running", "Running...");

    let sawOutput = false;
    let finished = null;

    const finish = () => {
        el._abort = null;
        runBtn.textContent = "Run";
        runBtn.classList.remove("is-stop");
        sink.flush();
    };

    try {
        await streamSSE("/api/code/run_stream",
            { email, code, timeout: cellTimeout() },
            (event) => {
                switch (event.type) {
                    case "start":
                        if (!event.venv_ready) {
                            setCellStatus(el, "running", "Preparing environment...");
                        }
                        break;
                    case "venv":
                    case "install":
                        if (event.line) { sink.write("stdout", event.line); sawOutput = true; }
                        break;
                    case "stdout":
                    case "stderr":
                        sink.write(event.type, event.text);
                        sawOutput = true;
                        break;
                    case "image":
                        appendCellImage(el, event.src);
                        break;
                    case "error":
                        sink.write("stderr", event.message);
                        sawOutput = true;
                        setCellStatus(el, "fail", "runner error");
                        out.classList.add("error");
                        break;
                    case "done":
                        finished = event;
                        break;
                }
            },
            controller.signal);

        finish();

        if (finished) {
            const failed = finished.exit_code !== 0 || finished.timed_out;
            if (failed) out.classList.add("error");
            if (!sawOutput) out.textContent = "(no output)";
            let label;
            if (finished.cancelled) label = "stopped";
            else if (finished.timed_out) label = "timeout";
            else if (failed) label = `exit ${finished.exit_code}`;
            else label = "ok";
            setCellStatus(el, failed ? "fail" : "ok",
                          `${label} · ${finished.duration_ms}ms`);

            // The reason this whole feature exists: turn a dead end into a
            // click. A missing import becomes an install button.
            if (finished.suggestion) {
                renderInstallSuggestion(el, finished.suggestion);
            }
        }
        saveCellsNow();
    } catch (err) {
        finish();
        if (err.name === "AbortError") {
            // The server saw the disconnect and killed the process; it can't
            // tell us so over the socket it just lost.
            sink.write("stderr", "\n[stopped]");
            sink.flush();
            setCellStatus(el, "fail", "stopped");
        } else {
            sink.write("stderr", `\n${err.message}`);
            sink.flush();
            out.classList.add("error");
            setCellStatus(el, "fail", "error");
        }
        saveCellsNow();
    }
}

/** The one-click fix for a ModuleNotFoundError. */
function renderInstallSuggestion(el, suggestion) {
    const box = document.createElement("div");
    box.className = "install-suggestion";

    const label = document.createElement("span");
    label.textContent = suggestion.module === suggestion.package
        ? `Missing module '${suggestion.module}'.`
        : `Missing module '${suggestion.module}' — provided by '${suggestion.package}'.`;

    const btn = document.createElement("button");
    btn.className = "header-btn";
    btn.textContent = `Install ${suggestion.package}`;
    btn.onclick = () => installForCell(el, suggestion.package);

    box.appendChild(label);
    box.appendChild(btn);
    el.appendChild(box);
}

async function installForCell(el, pkg) {
    const email = getEmail();
    if (!email) return;

    const out = resetCellOutput(el);
    const sink = makeOutputSink(out);
    setCellStatus(el, "running", `Installing ${pkg}...`);

    let ok = false;
    try {
        await streamSSE("/api/code/install", { email, packages: [pkg] },
            (event) => {
                if (event.type === "install" && event.line) {
                    sink.write("stdout", event.line);
                } else if (event.type === "error") {
                    sink.write("stderr", event.message);
                } else if (event.type === "done") {
                    ok = !!event.ok;
                }
            });
        sink.flush();
    } catch (err) {
        sink.write("stderr", `\n${err.message}`);
        sink.flush();
    }

    if (ok) {
        setCellStatus(el, "ok", "installed · re-running");
        await runCell(el.id);
    } else {
        out.classList.add("error");
        setCellStatus(el, "fail", "install failed");
        saveCellsNow();
    }
}

// =====================================================================
// Cell editor ergonomics
//
// Deliberately a plain textarea with a keydown handler rather than
// CodeMirror/Ace: those need a bundler or a CDN, and this app has neither a
// build step nor a guarantee of network access.
// =====================================================================

const INDENT = "    ";

/** Grow the textarea to fit its content so a long cell isn't a scrollbox. */
function autoGrowCell(ta) {
    ta.style.height = "auto";
    ta.style.height = Math.min(Math.max(ta.scrollHeight, 80), 600) + "px";
}

/**
 * Insert text at the cursor via execCommand where available.
 *
 * Assigning to .value directly wipes the browser's native undo stack, which
 * is far more annoying than not having Tab support at all.
 */
function insertText(ta, text) {
    if (document.execCommand) {
        if (document.execCommand("insertText", false, text)) return;
    }
    const { selectionStart: s, selectionEnd: e } = ta;
    ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
    ta.selectionStart = ta.selectionEnd = s + text.length;
}

function handleEditorKeys(e, ta) {
    const { selectionStart: start, selectionEnd: end, value } = ta;

    if (e.key === "Tab") {
        e.preventDefault();
        const multiline = value.slice(start, end).includes("\n");
        if (!multiline && !e.shiftKey) {
            insertText(ta, INDENT);
        } else {
            // Indent/dedent every touched line, keeping the selection over
            // the same lines afterwards.
            const from = value.lastIndexOf("\n", start - 1) + 1;
            const toRaw = value.indexOf("\n", end);
            const to = toRaw === -1 ? value.length : toRaw;
            const block = value.slice(from, to);
            const shifted = block.split("\n").map(line => {
                if (e.shiftKey) {
                    if (line.startsWith(INDENT)) return line.slice(INDENT.length);
                    return line.replace(/^[ \t]{1,4}/, "");
                }
                return INDENT + line;
            }).join("\n");
            ta.setSelectionRange(from, to);
            insertText(ta, shifted);
            ta.setSelectionRange(from, from + shifted.length);
        }
    } else if (e.key === "Enter" && !e.shiftKey) {
        const lineStart = value.lastIndexOf("\n", start - 1) + 1;
        const line = value.slice(lineStart, start);
        const indent = (line.match(/^[ \t]*/) || [""])[0];
        // A trailing colon opens a block, so the next line goes one deeper.
        const deeper = /:\s*$/.test(line) ? INDENT : "";
        if (!indent && !deeper) return;
        e.preventDefault();
        insertText(ta, "\n" + indent + deeper);
    } else if (e.key === "Backspace" && start === end) {
        // Delete a whole indent level when sitting on one.
        const lineStart = value.lastIndexOf("\n", start - 1) + 1;
        const before = value.slice(lineStart, start);
        if (before.length >= INDENT.length && /^[ ]+$/.test(before)
            && before.length % INDENT.length === 0) {
            e.preventDefault();
            ta.setSelectionRange(start - INDENT.length, start);
            insertText(ta, "");
        }
    }

    autoGrowCell(ta);
}

function sendCellToChat(id) {
    const el = document.getElementById(id);
    if (!el) return;
    const code = el.querySelector(".cell-textarea").value;
    const output = el.querySelector(".cell-output").textContent;
    const isError = el.querySelector(".cell-output").classList.contains("error");

    let payload = `Here's a code cell I ran:\n\n\`\`\`python\n${code}\n\`\`\``;
    if (output && output !== "(no output)") {
        payload += `\n\n${isError ? "Error/output" : "Output"}:\n\`\`\`\n${output}\n\`\`\``;
    }
    const input = document.getElementById("message-input");
    input.value = input.value ? input.value + "\n\n" + payload : payload;
    input.focus();
    input.dispatchEvent(new Event("input"));
}

async function resetVenv() {
    const email = getEmail();
    if (!email) {
        alert("Enter your email first.");
        return;
    }
    if (!confirm("Wipe and recreate your venv? This re-installs qiskit and may take a minute.")) return;
    try {
        const res = await fetch(`${API_BASE}/api/code/reset_venv`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email }),
        });
        if (!res.ok) {
            const detail = await res.text();
            alert(`Reset failed: ${detail}`);
            return;
        }
        alert("Venv reset. Try running a cell.");
    } catch (err) {
        alert(`Reset failed: ${err.message}`);
    }
}

// =====================================================================
// Init
// =====================================================================

document.addEventListener("DOMContentLoaded", () => {
    const textarea = document.getElementById("message-input");
    textarea.focus();

    // Auto-resize textarea as user types
    textarea.addEventListener("input", () => autoResizeInput(textarea));

    // Image attachments: paste into the composer, drop anywhere over the
    // conversation, or use the paperclip.
    initAttachments(getMainChat, {
        inputEl: textarea,
        dropZone: document.querySelector(".chat-scroll-wrap") || document.getElementById("chat-messages"),
        attachBtnEl: document.getElementById("attach-btn"),
        attachInputEl: document.getElementById("attach-input"),
    });

    // Sync the button label only — don't persist what may be an OS default.
    applyTheme(document.documentElement.getAttribute("data-theme") || "dark", false);

    // Restore collapsed rails from last session.
    initRails();

    // Restore identity so a refresh doesn't blank every panel. ?email= and
    // ?session= let the Memory explorer link straight into a conversation.
    const params = new URLSearchParams(location.search);
    let savedEmail = params.get("email");
    if (!savedEmail) {
        try { savedEmail = localStorage.getItem("pymentor:email"); } catch (e) {}
    }
    if (savedEmail) {
        document.getElementById("email-input").value = savedEmail;
        const wanted = params.get("session");
        onEmailChange().then(() => {
            if (wanted) {
                switchSession(wanted);
                // Drop the params so a refresh doesn't keep re-opening it.
                history.replaceState(null, "", location.pathname);
            }
        });
    } else {
        updateLearnerChip("");
    }

    document.addEventListener("keydown", (e) => {
        if (!(e.metaKey || e.ctrlKey)) return;
        if (e.key === "\\") { e.preventDefault(); toggleRail("left"); }
        else if (e.key === "]") { e.preventDefault(); toggleRail("right"); }
        else if (e.key === ".") { e.preventDefault(); toggleFocus(); }
        else if (e.key.toLowerCase() === "e" && typeof toggleNotes === "function") {
            e.preventDefault(); toggleNotes();
        }
    });

    // Notes canvas. Guarded so app.js keeps working if notes.js fails to load.
    initProgressGroups();
    if (typeof initNotes === "function") initNotes();
    if (typeof initDemoPane === "function") initDemoPane();

    // Side-chat window: drag/resize handlers and its own auto-resizing input.
    initSideChatWindow();
    const sideInput = document.getElementById("side-chat-input");
    if (sideInput) sideInput.addEventListener("input", () => autoResizeInput(sideInput));

    initAttachments(getSideChat, {
        inputEl: sideInput,
        dropZone: sideChatEl(),
        attachBtnEl: document.getElementById("side-attach-btn"),
        attachInputEl: document.getElementById("side-attach-input"),
    });

    // Show the jump-to-latest button whenever the reader scrolls away from
    // the bottom, and retire it once they're back.
    const chat = document.getElementById("chat-messages");
    chat.addEventListener("scroll", () => {
        if (isChatAtBottom()) {
            hideScrollToBottomBtn();
        } else if (isStreaming) {
            showScrollToBottomBtn();
        }
    });
});
