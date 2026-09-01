/**
 * Reader pane — read a book page by page, with the tutor.
 *
 * Loaded after app.js, notes.js and demo-pane.js; depends on getEmail,
 * escapeHtml and the pane-divider helpers from notes.js.
 *
 * WHY THIS FILE WRAPS RATHER THAN EDITS
 *
 * openRightPane(), toggleNotes() and toggleDemo() live in notes.js and
 * demo-pane.js, both of which are byte-identical forks of frontend/notes.js
 * and frontend/demo-pane.js. Teaching them about a fourth pane would mean
 * editing two copies of each file forever — and PyMentor, which owns the other
 * copy, has no reader. So this file wraps those three functions instead,
 * keeping every divergence inside the one file that is already Sanskrit-only.
 * They are plain top-level function declarations in classic scripts, so the
 * bindings are mutable and a wrapper composes cleanly.
 *
 * WHY POSITION IS NOT HELD HERE
 *
 * The server owns the reading position (see backend/books.py). Both sides turn
 * pages — the learner clicks, the tutor calls go_to_page — so the client
 * cannot be the source of truth without the two disagreeing the moment the
 * tutor moves while the learner is reading. Every navigation writes to
 * /position and re-renders from the response; nothing moves optimistically.
 */

let readerOpen = false;
let activeBookId = null;
let activeBook = null;
let readerBooks = [];
let readerZoom = 1;

// Weighted upward, and it goes a long way up. A 144 DPI scan of Devanagari
// often needs 150-250% before a subscript vowel is comfortably readable, and
// a damaged or very small conjunct can need 400%+ to settle an argument about
// what the letter actually is. The display copy has the pixels for it — a page
// renders at ~1240x1750, so 400% is still showing real detail rather than
// interpolated mush. Below 100% is only for seeing a page's shape, so one step
// down is enough.
const READER_ZOOM_STEPS = [0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4, 5, 6];
// Must match books.MAX_WINDOW_PAGES_TO_MODEL. When the learner opens a wider
// window than this, the caption says so — silently showing the tutor less than
// the learner sees is how it ends up discussing a page it was never sent.
const READER_MODEL_WINDOW = 4;

function readerUiKey() {
    const email = typeof getEmail === "function" ? getEmail() : "";
    return `sanskrit:reader:${email || "_anon"}`;
}

function saveReaderUi() {
    try {
        localStorage.setItem(readerUiKey(), JSON.stringify({
            bookId: activeBookId,
            zoom: readerZoom,
        }));
    } catch (e) { /* private mode costs the preference, not the feature */ }
}

function loadReaderUi() {
    try {
        return JSON.parse(localStorage.getItem(readerUiKey()) || "{}") || {};
    } catch (e) { return {}; }
}

// --- Data ------------------------------------------------------------------

async function loadReaderIndex() {
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email) return [];
    try {
        const res = await fetch(`/api/books/${encodeURIComponent(email)}`);
        if (!res.ok) throw new Error(`Server returned ${res.status}`);
        readerBooks = (await res.json()).books || [];
    } catch (e) {
        console.warn("Could not load the book list:", e);
        readerBooks = [];
    }
    return readerBooks;
}

async function fetchBook(bookId) {
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email || !bookId) return null;
    try {
        const res = await fetch(
            `/api/books/${encodeURIComponent(email)}/${encodeURIComponent(bookId)}`);
        if (!res.ok) return null;
        return await res.json();
    } catch (e) { return null; }
}

/**
 * Move to a page. The server clamps, renders and answers with the truth.
 *
 * Never moves the pane first and reconciles after: the tutor may be turning
 * the page in the same moment, and the server's answer is what both sides use.
 */
async function readerGoTo(page, window_) {
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email || !activeBookId) return;
    const body = { page: Math.round(page) };
    if (window_ != null) body.window = Number(window_);

    try {
        const res = await fetch(
            `/api/books/${encodeURIComponent(email)}/${encodeURIComponent(activeBookId)}/position`,
            {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
        if (!res.ok) throw new Error(`Server returned ${res.status}`);
        activeBook = await res.json();
        renderReader();
    } catch (e) {
        console.warn("Could not move to that page:", e);
    }
}

function readerPos() {
    return (activeBook && activeBook.position) || { page: 1, window: 2 };
}

function readerNext() {
    if (!activeBook) return;
    const p = readerPos();
    readerGoTo(p.page + (p.window || 1));
}

function readerPrev() {
    if (!activeBook) return;
    const p = readerPos();
    readerGoTo(p.page - (p.window || 1));
}

function readerJump(value) {
    const n = parseInt(String(value).trim(), 10);
    if (Number.isFinite(n)) readerGoTo(n);
    else renderReader();          // put the input back to the real page
}

function setReaderWindow(value) {
    if (!activeBook) return;
    readerGoTo(readerPos().page, Number(value));
}

// --- Rendering -------------------------------------------------------------

function renderReader() {
    const body = document.getElementById("reader-body");
    const titleEl = document.getElementById("reader-title");
    const countEl = document.getElementById("reader-page-count");
    const inputEl = document.getElementById("reader-page-input");
    const windowEl = document.getElementById("reader-window");
    const noteEl = document.getElementById("reader-model-note");
    if (!body) return;

    if (!activeBook) {
        body.innerHTML = `<div class="reader-empty">
            <p>No book open.</p>
            <p class="reader-empty-hint">Add a PDF to read it page by page — the
            tutor sees the same pages you do. Or just ask: “find me the
            Hitopadeśa and open it”.</p>
            <div class="reader-empty-actions">
                <button class="reader-add-btn" onclick="pickReaderPdf()">Upload a PDF</button>
                <button class="reader-add-btn is-secondary" onclick="addBookFromUrl()">Paste a link</button>
            </div>
        </div>`;
        if (titleEl) titleEl.textContent = "";
        if (countEl) countEl.textContent = "";
        if (inputEl) inputEl.value = "";
        if (noteEl) noteEl.textContent = "";
        return;
    }

    const email = typeof getEmail === "function" ? getEmail() : "";
    const pos = readerPos();
    const count = activeBook.page_count || 0;
    const last = Math.min(pos.page + (pos.window || 1) - 1, count);
    const pages = [];
    for (let p = pos.page; p <= last; p++) pages.push(p);

    if (titleEl) titleEl.textContent = activeBook.title || "Untitled book";
    if (countEl) countEl.textContent = `/ ${count}`;
    if (inputEl) inputEl.value = String(pos.page);
    if (windowEl) windowEl.value = String(pos.window || 2);

    // Be honest about the asymmetry rather than letting the learner assume the
    // tutor is looking at everything on screen.
    if (noteEl) {
        noteEl.textContent = pages.length > READER_MODEL_WINDOW
            ? `tutor sees the first ${READER_MODEL_WINDOW}`
            : "";
    }

    const base = `/api/books/${encodeURIComponent(email)}/${encodeURIComponent(activeBookId)}/page`;

    // One page fills the pane; two sit side by side; more wrap. Expressed as a
    // flex-basis rather than a fixed width so the pane divider still governs.
    const perRow = pages.length === 1 ? 1 : (pages.length <= 4 ? 2 : 3);
    const basis = perRow === 1 ? "100%" : `calc(${(100 / perRow).toFixed(2)}% - var(--space-3))`;

    // Zoom widens the page column and lets the body scroll, rather than the
    // CSS `zoom` property — that scales the layout box too, so the pane's own
    // scrollbars stopped matching what was on screen at anything but 100%.
    // Past ~200% the stored 144 DPI copy runs out of real detail and the
    // browser starts interpolating — exactly when the learner is leaning in to
    // settle what a conjunct actually is. Ask the server to render those
    // fresh at a higher DPI (it does not cache them; see the page route).
    const dpi = readerZoom > 2 ? "?dpi=300" : (readerZoom > 1.5 ? "?dpi=216" : "");

    body.innerHTML =
        `<div class="reader-pages" style="--reader-page-basis:${basis};` +
        `width:${Math.round(readerZoom * 100)}%">` +
        pages.map(p => `
            <figure class="reader-page">
                <img src="${base}/${p}.png${dpi}" alt="Page ${p}" loading="lazy">
                <figcaption>${p}</figcaption>
            </figure>`).join("") +
        `</div>`;

    const prev = document.getElementById("reader-prev");
    const next = document.getElementById("reader-next");
    if (prev) prev.disabled = pos.page <= 1;
    if (next) next.disabled = last >= count;
}

function readerZoomBy(step) {
    const i = READER_ZOOM_STEPS.indexOf(readerZoom);
    const at = i === -1 ? 3 : i;
    readerZoom = READER_ZOOM_STEPS[
        Math.max(0, Math.min(READER_ZOOM_STEPS.length - 1, at + step))];
    const label = document.getElementById("reader-zoom-level");
    if (label) label.textContent = `${Math.round(readerZoom * 100)}%`;
    renderReader();
    saveReaderUi();
}

function readerZoomReset() {
    readerZoom = 1;
    const label = document.getElementById("reader-zoom-level");
    if (label) label.textContent = "100%";
    renderReader();
    saveReaderUi();
}

// --- Opening / switching ---------------------------------------------------

async function openBook(bookId) {
    activeBookId = bookId;
    activeBook = await fetchBook(bookId);
    if (!activeBook) {
        activeBookId = null;
        renderReader();
        return;
    }
    renderReaderPicker();
    renderReader();
    stageBookRef();
    saveReaderUi();
}

function onBookPicked() {
    const sel = document.getElementById("reader-picker");
    if (sel && sel.value) openBook(sel.value);
}

function renderReaderPicker() {
    const sel = document.getElementById("reader-picker");
    if (!sel) return;
    sel.innerHTML = readerBooks.map(b =>
        `<option value="${b.book_id}"${b.book_id === activeBookId ? " selected" : ""}>` +
        `${typeof escapeHtml === "function" ? escapeHtml(b.title) : b.title}</option>`
    ).join("");
    sel.style.display = readerBooks.length ? "" : "none";
}

/**
 * Attach the open book to the next message.
 *
 * Auto-staged, unlike a demo: someone reading and asking "what does this line
 * mean?" obviously means this page. The chip in the composer is the escape
 * hatch for asking something unrelated.
 */
function stageBookRef() {
    const chat = typeof getMainChat === "function" ? getMainChat() : null;
    if (!chat || typeof chat.setBookRef !== "function") return;
    if (readerOpen && activeBook) {
        chat.setBookRef({ book_id: activeBookId, title: activeBook.title });
    } else {
        chat.setBookRef(null);
    }
}

function askTutorAboutPage() {
    stageBookRef();
    const input = document.getElementById("message-input");
    if (input) input.focus();
}

// --- Upload ----------------------------------------------------------------

function pickReaderPdf() {
    const input = document.getElementById("reader-pdf-input");
    if (input) input.click();
}

/**
 * One "add a book" entry point, rather than two glyph buttons in a row that
 * already has a zoom "+" in it.
 */
function addBookMenu() {
    const wantsLink = confirm(
        "Add a book\n\n" +
        "OK — paste a link (archive.org, or a direct PDF)\n" +
        "Cancel — choose a PDF from this computer"
    );
    if (wantsLink) addBookFromUrl();
    else pickReaderPdf();
}

/**
 * Paste a link instead of a file.
 *
 * The same background job the tutor's open_book starts, so archive.org pages
 * resolve and the SSRF rules apply identically — there is one fetch path, not
 * a laxer one for the learner.
 */
async function addBookFromUrl() {
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email) return;
    const url = (prompt(
        "Paste a link to a PDF, or an archive.org page:\n\n" +
        "e.g. https://archive.org/details/higersanskritgra00kale"
    ) || "").trim();
    if (!url) return;

    const body = document.getElementById("reader-body");
    if (body) {
        body.innerHTML = `<div class="reader-empty"><p>Fetching…</p>
            <p class="reader-empty-hint">This runs in the background — you can
            keep talking to the tutor while it downloads.</p></div>`;
    }
    try {
        const res = await fetch(`/api/books/${encodeURIComponent(email)}/from-url`,
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ url, title: "" }),
            });
        if (!res.ok) {
            let detail = `Server returned ${res.status}`;
            try { detail = (await res.json()).detail || detail; } catch (e) {}
            throw new Error(detail);
        }
        trackBookJob((await res.json()).job_id);
    } catch (e) {
        if (body) {
            const msg = String(e.message || e);
            body.innerHTML = `<div class="reader-empty"><p>Could not fetch that.</p>
                <p class="reader-empty-hint">${typeof escapeHtml === "function"
                    ? escapeHtml(msg) : msg}</p>
                <button class="reader-add-btn" onclick="pickReaderPdf()">Upload a PDF instead</button></div>`;
        }
    }
}

async function onReaderPdfChosen(fileInput) {
    const file = fileInput && fileInput.files && fileInput.files[0];
    if (!file) return;
    fileInput.value = "";                 // so the same file can be re-picked
    await uploadBook(file);
}

async function uploadBook(file) {
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email) return;

    const body = document.getElementById("reader-body");
    if (body) {
        body.innerHTML = `<div class="reader-empty"><p>Opening
            ${typeof escapeHtml === "function" ? escapeHtml(file.name) : file.name}…</p></div>`;
    }

    const form = new FormData();
    form.append("file", file);
    try {
        const res = await fetch(
            `/api/books/${encodeURIComponent(email)}/upload`,
            { method: "POST", body: form });
        if (!res.ok) {
            let detail = `Server returned ${res.status}`;
            try { detail = (await res.json()).detail || detail; } catch (e) {}
            throw new Error(detail);
        }
        const record = (await res.json()).book;
        await loadReaderIndex();
        await openBook(record.book_id);
    } catch (e) {
        if (body) {
            body.innerHTML = `<div class="reader-empty"><p>Could not open that PDF.</p>
                <p class="reader-empty-hint">${typeof escapeHtml === "function"
                    ? escapeHtml(String(e.message || e)) : String(e.message || e)}</p>
                <button class="reader-add-btn" onclick="pickReaderPdf()">Try another</button></div>`;
        }
    }
}

async function deleteActiveBook() {
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email || !activeBookId || !activeBook) return;
    if (!confirm(`Remove “${activeBook.title}” from your books?`)) return;
    try {
        await fetch(`/api/books/${encodeURIComponent(email)}/${encodeURIComponent(activeBookId)}`,
            { method: "DELETE" });
    } catch (e) { /* fall through to the reload either way */ }
    activeBookId = null;
    activeBook = null;
    await loadReaderIndex();
    renderReaderPicker();
    if (readerBooks.length) await openBook(readerBooks[0].book_id);
    else { renderReader(); stageBookRef(); }
}

// --- Pane plumbing ---------------------------------------------------------

async function toggleReader(force) {
    const pane = document.getElementById("reader-pane");
    const btn = document.getElementById("reader-btn");
    if (!pane) return;

    readerOpen = force === undefined ? !readerOpen : !!force;
    pane.style.display = readerOpen ? "flex" : "none";
    if (btn) btn.classList.toggle("is-on", readerOpen);

    if (!readerOpen) {
        if (typeof syncPaneSplit === "function") syncPaneSplit();
        stageBookRef();
        saveReaderUi();
        return;
    }

    // The right-hand panes compete for one column.
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

    if (!readerBooks.length) await loadReaderIndex();
    renderReaderPicker();

    if (!activeBookId) {
        const ui = loadReaderUi();
        if (ui.zoom) readerZoom = ui.zoom;
        const target = (ui.bookId && readerBooks.some(b => b.book_id === ui.bookId))
            ? ui.bookId
            : (readerBooks[0] && readerBooks[0].book_id);
        if (target) await openBook(target);
        else renderReader();
    } else {
        renderReader();
        stageBookRef();
    }
    saveReaderUi();
}

/**
 * The tutor turned the page. Follow it.
 *
 * Driven from the tool_result SSE event rather than by polling: the result is
 * already on the wire and arrives the instant the turn happens, so a poller
 * would only add latency and a second source of truth.
 *
 * Detected by SHAPE, not by tool name — the tool_result event carries
 * {type, tool_use_id, is_error, content} and no tool_name, so a book_id and a
 * page together are what identify one of ours.
 */
function onReaderToolResult(content) {
    if (!content) return;
    let data;
    try { data = JSON.parse(content); } catch (e) { return; }

    // open_book returns a job id, not a page — the download runs in the
    // background. Matched on the "bj_" prefix, because request_demo also
    // returns a job_id and its jobs belong to the demo pane's poller.
    if (data && typeof data.job_id === "string" && data.job_id.startsWith("bj_")) {
        trackBookJob(data.job_id);
        return;
    }

    if (!data || !data.book_id || typeof data.page !== "number") return;

    if (data.book_id !== activeBookId) {
        // The tutor opened a different book — follow it there.
        loadReaderIndex().then(() => {
            toggleReader(true).then(() => openBook(data.book_id));
        });
        return;
    }
    if (!readerOpen) toggleReader(true);
    fetchBook(activeBookId).then(record => {
        if (record) { activeBook = record; renderReader(); }
    });
}

// --- Background fetches ----------------------------------------------------

// Job ids the tutor started this page-load, so polling can stop when they are
// all done. Reconciled against THIS set rather than the response: jobs are
// reaped after a TTL and lost on a restart, and polling the response's own
// list would keep the interval alive for the life of the tab.
const trackedBookJobs = new Set();
let bookJobTimer = null;
let bookJobFailures = 0;

function trackBookJob(jobId) {
    if (!jobId) return;
    trackedBookJobs.add(jobId);
    if (!bookJobTimer) bookJobTimer = setInterval(pollBookJobs, 2000);
}

async function pollBookJobs() {
    if (!trackedBookJobs.size) {
        clearInterval(bookJobTimer);
        bookJobTimer = null;
        return;
    }
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email) return;

    let jobs;
    try {
        const res = await fetch(`/api/book-jobs/${encodeURIComponent(email)}`);
        if (!res.ok) throw new Error(`Server returned ${res.status}`);
        jobs = (await res.json()).jobs || [];
        bookJobFailures = 0;
    } catch (e) {
        // Give up rather than hammering a server that is gone.
        if (++bookJobFailures > 30) {
            clearInterval(bookJobTimer);
            bookJobTimer = null;
        }
        return;
    }

    for (const job of jobs) {
        if (!trackedBookJobs.has(job.job_id)) continue;
        if (job.state === "ready" && job.book_id) {
            trackedBookJobs.delete(job.job_id);
            await loadReaderIndex();
            await toggleReader(true);
            await openBook(job.book_id);
        } else if (job.state === "failed" || job.state === "cancelled") {
            trackedBookJobs.delete(job.job_id);
            console.warn(`Book fetch failed: ${job.error || job.state}`);
            // The tutor is told too, through its own tool result — this is
            // only so the pane does not sit blank with no explanation.
            if (readerOpen && !activeBook) {
                const body = document.getElementById("reader-body");
                if (body) {
                    const msg = job.error || "that download did not finish";
                    body.innerHTML = `<div class="reader-empty">
                        <p>Could not open that book.</p>
                        <p class="reader-empty-hint">${typeof escapeHtml === "function"
                            ? escapeHtml(msg) : msg}</p>
                        <button class="reader-add-btn" onclick="pickReaderPdf()">Add a PDF instead</button>
                    </div>`;
                }
            }
        }
    }
}

/** Pick up fetches still running from before a reload. */
async function resumeBookJobs() {
    const email = typeof getEmail === "function" ? getEmail() : "";
    if (!email) return;
    try {
        const res = await fetch(`/api/book-jobs/${encodeURIComponent(email)}`);
        if (!res.ok) return;
        for (const job of (await res.json()).jobs || []) {
            if (["queued", "fetching", "rendering"].includes(job.state)) {
                trackBookJob(job.job_id);
            }
        }
    } catch (e) { /* nothing to resume */ }
}

/** Re-sync after a turn, in case an SSE event was missed (backgrounded tab). */
function reconcileReader() {
    if (!readerOpen || !activeBookId) return;
    fetchBook(activeBookId).then(record => {
        if (!record) return;
        const before = JSON.stringify(readerPos());
        activeBook = record;
        if (JSON.stringify(readerPos()) !== before) renderReader();
    });
}

// --- Wrap the shared pane helpers (see the note at the top of this file) ----

if (typeof openRightPane === "function") {
    const _openRightPane = openRightPane;
    openRightPane = function () {
        const reader = document.getElementById("reader-pane");
        if (reader && reader.style.display !== "none") return reader;
        return _openRightPane();
    };
}

// Exclusivity has to be bidirectional: opening notes or a demo must close the
// reader, not merely the other way round. Without this two panes stack in a
// column sized for one, which reads as a rendering bug rather than a logic one.
if (typeof toggleNotes === "function") {
    const _toggleNotes = toggleNotes;
    toggleNotes = async function (force) {
        const opening = force === undefined ? !notesOpen : !!force;
        if (opening && readerOpen) await toggleReader(false);
        return _toggleNotes.apply(this, arguments);
    };
}

if (typeof toggleDemo === "function") {
    const _toggleDemo = toggleDemo;
    toggleDemo = async function (force) {
        const opening = force === undefined ? !demoOpen : !!force;
        if (opening && readerOpen) await toggleReader(false);
        return _toggleDemo.apply(this, arguments);
    };
}

document.addEventListener("DOMContentLoaded", () => {
    // A fetch the tutor started before a reload is still running server-side.
    resumeBookJobs();

    const input = document.getElementById("reader-pdf-input");
    if (input) input.addEventListener("change", () => onReaderPdfChosen(input));

    // Arrow keys turn pages, but never while someone is typing.
    document.addEventListener("keydown", (e) => {
        if (!readerOpen || !activeBook) return;
        const el = document.activeElement;
        if (el && el.closest("input, textarea, select, [contenteditable]")) return;
        if (e.key === "ArrowRight") { e.preventDefault(); readerNext(); }
        else if (e.key === "ArrowLeft") { e.preventDefault(); readerPrev(); }
    });

    // Drop a PDF anywhere on the open pane to read it.
    const pane = document.getElementById("reader-pane");
    if (pane) {
        pane.addEventListener("dragover", (e) => {
            if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) {
                e.preventDefault();
                pane.classList.add("is-dropping");
            }
        });
        pane.addEventListener("dragleave", () => pane.classList.remove("is-dropping"));
        pane.addEventListener("drop", (e) => {
            pane.classList.remove("is-dropping");
            const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
            if (!file) return;
            e.preventDefault();
            if (file.type === "application/pdf" || /\.pdf$/i.test(file.name)) uploadBook(file);
        });
    }
});
