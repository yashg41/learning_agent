// PyMentor Notes — a free-positioning canvas beside the chat.
//
// Model (Samsung Notes, deliberately): fixed width equal to the pane, grows
// downward forever, no zoom and no 2D pan. Blocks carry absolute x/y and
// nothing reflows underneath them. An optional PDF sits behind as a page
// column you annotate on top of.
//
// Loaded after app.js and depends on it for: escapeHtml, currentEmail,
// currentSessionId, API_BASE. Everything here is a global function because
// index.html wires buttons with inline onclick, exactly as app.js does.

// =====================================================================
// State
// =====================================================================

let notesOpen = false;
let note = null;                 // the open note document
let activeNoteId = null;
let noteIndex = [];              // [{id, title, updated_at, pinned_session_id}]
let selectedIds = new Set();
let editing = null;              // {blockId, ta}
let activeTool = "select";
let stickyTool = false;          // shift-picked tool survives one use
let drag = null;
let undoStack = [];
let pdfImportAvailable = false;
let lastCanvasPoint = null;
let notesBusy = false;
let spaceHeld = false;

const NOTES_MIN_CANVAS_H = 1200;
const NOTES_GROW_STEP = 600;
const NOTES_GROW_MARGIN = 160;
// How much wider than the pane the board is, so there is room to pan sideways
// at any zoom. Applies to the canvas element only — pageScale() and
// rescaleForWidth() keep measuring the pane, never this. See applyZoom().
const NOTES_CANVAS_WIDTH_FACTOR = 2;
const DRAG_THRESHOLD = 3;
const SAVE_DEBOUNCE = 800;
const SAVE_MAX_WAIT = 5000;
const UNDO_DEPTH = 20;
const MAX_NOTE_IMAGE_BYTES = 5 * 1024 * 1024;   // matches MAX_ATTACHMENT_BYTES
const NOTE_IMAGE_TYPES = ["image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"];

const STICKY_COLORS = ["amber", "green", "blue", "pink", "violet"];

// Text colours are literal hex, not theme tokens: a note block can sit on a
// white PDF page or a coloured sticky, so "the theme's text colour" is not
// meaningful. These read on both light and dark backgrounds.
const TEXT_COLORS = [
    { key: "default", css: "currentColor" },
    { key: "red",     css: "#dc2626" },
    { key: "amber",   css: "#b45309" },
    { key: "green",   css: "#15803d" },
    { key: "blue",    css: "#1d4ed8" },
    { key: "violet",  css: "#6d28d9" },
];

const STROKE_COLORS = [
    { key: "accent",  css: "#8b5cf6" },
    { key: "success", css: "#22c55e" },
    { key: "warning", css: "#f59e0b" },
    { key: "error",   css: "#ef4444" },
    { key: "muted",   css: "#a1a1aa" },
];

const FONT_SIZE_MIN = 10;
const FONT_SIZE_MAX = 40;

// Zoom is a pure view transform: block coordinates are always stored at 1.0,
// and canvasPoint() divides it back out. Nothing in the model ever sees it,
// so zooming can never drift the saved positions.
const ZOOM_MIN = 0.25;
const ZOOM_MAX = 4;
// Ratio-based rather than additive, so a press feels like the same amount of
// zoom at 40% as at 300%. Zoom is button/keyboard only — see initNotes().
const ZOOM_BUTTON_RATIO = 1.2;
let zoom = 1;


// =====================================================================
// Small helpers
// =====================================================================

function notesPaneEl() { return document.getElementById("notes-pane"); }
function canvasEl() { return document.getElementById("notes-canvas"); }
function scrollEl() { return document.getElementById("notes-scroll"); }
function blocksEl() { return document.getElementById("notes-blocks"); }
function blockEl(id) { return document.querySelector(`.nb-block[data-id="${id}"]`); }
function blockById(id) { return note && note.blocks.find(b => b.id === id); }
/**
 * Canvas width in model units.
 *
 * clientWidth ignores CSS transforms, and applyZoom() no longer touches the
 * element's width, so this is the same at every zoom level. That is what
 * keeps pageScale() and rescaleForWidth() independent of zoom — the property
 * that stops zooming from ever rewriting block coordinates.
 */
function canvasWidth() {
    // Measured from the scroll container, which is never transformed and
    // whose width applyZoom() never touches. Reading the canvas itself would
    // make every layout calculation zoom-dependent.
    //
    // offsetWidth, not clientWidth: zooming in adds a horizontal scrollbar,
    // and clientWidth excludes it. That ~15px drop would read as a pane
    // resize and shift every annotation off the passage it annotates.
    const host = document.getElementById("notes-scroll");
    if (host && host.offsetWidth) return host.offsetWidth;
    const c = canvasEl();
    return c ? c.clientWidth : 700;
}
function newId(p) { return p + Math.random().toString(36).slice(2, 10); }

function notesUiKey() {
    const email = typeof getEmail === "function" ? getEmail() : "";
    return `pymentor:notes-ui:${email || "_anon"}`;
}

function saveNotesUi() {
    try {
        localStorage.setItem(notesUiKey(), JSON.stringify({
            open: notesOpen, noteId: activeNoteId,
        }));
    } catch (e) {}
}

function loadNotesUi() {
    try { return JSON.parse(localStorage.getItem(notesUiKey()) || "{}"); }
    catch (e) { return {}; }
}

/**
 * Markdown for note content.
 *
 * Deliberately NOT app.js's renderMarkdown: note text is authored by the
 * user, stored on the server and re-rendered on every load, which makes it a
 * stored-XSS surface the chat transcript is not. marked dropped its sanitizer
 * in v8, so escape first — fenced code still renders, because escaping runs
 * before the parser sees the backticks.
 */
function renderNoteMarkdown(text) {
    if (!text) return "";
    const safe = escapeHtml(text);
    if (typeof marked === "undefined") return safe.replace(/\n/g, "<br>");
    return marked.parse(safe, { breaks: true, gfm: true });
}

function notesToast(msg, isError) {
    const host = scrollEl();
    if (!host) return;
    const prev = host.querySelector(".notes-toast");
    if (prev) prev.remove();
    const el = document.createElement("div");
    el.className = "notes-toast" + (isError ? " is-error" : "");
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => el.remove(), 3600);
}

function setSaveState(state) {
    const el = document.getElementById("notes-save-state");
    if (!el) return;
    el.classList.toggle("is-error", state === "error");
    el.textContent = state === "saving" ? "Saving…"
        : state === "saved" ? "Saved"
        : state === "error" ? "Save failed — retrying"
        : "";
}

function setBusy(on, label) {
    notesBusy = on;
    const host = scrollEl();
    if (!host) return;
    const prev = host.querySelector(".notes-busy");
    if (prev) prev.remove();
    if (!on) return;
    const el = document.createElement("div");
    el.className = "notes-busy";
    el.textContent = label || "Working…";
    host.appendChild(el);
}

// =====================================================================
// Autosave
//
// Debounced so ordinary typing never produces a save per keystroke, with a
// ceiling so continuous typing still checkpoints.
// =====================================================================

let saveTimer = null, saveCeiling = null, saving = false, dirty = false;

function scheduleSave() {
    if (!activeNoteId || !note) return;
    dirty = true;
    setSaveState("saving");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flushSave, SAVE_DEBOUNCE);
    if (!saveCeiling) saveCeiling = setTimeout(flushSave, SAVE_MAX_WAIT);
}

async function flushSave() {
    clearTimeout(saveTimer); saveTimer = null;
    clearTimeout(saveCeiling); saveCeiling = null;
    if (!activeNoteId || !note || !dirty) return;

    // Never two PUTs of the same note in flight: there is no server-side
    // locking, so overlapping writes are exactly how an update gets lost.
    if (saving) { scheduleSave(); return; }

    const email = getEmail();
    if (!email) return;

    saving = true;
    const payload = {
        title: note.title,
        canvas_height: Math.round(note.canvas_height),
        next_z: note.next_z,
        blocks: note.blocks,
        groups: note.groups,
        doc: note.doc,
    };
    dirty = false;
    try {
        const res = await fetch(
            `${API_BASE}/api/notes/${encodeURIComponent(email)}/${activeNoteId}`,
            {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
        if (!res.ok) throw new Error("HTTP " + res.status);
        setSaveState("saved");
    } catch (e) {
        // Put the flag back and retry: uvicorn runs with reload=True, so a
        // save landing during a restart must recover rather than be dropped.
        dirty = true;
        setSaveState("error");
        setTimeout(flushSave, 3000);
    } finally {
        saving = false;
        if (dirty) scheduleSave();
    }
}

function pushUndo() {
    if (!note) return;
    undoStack.push(JSON.stringify({
        blocks: note.blocks, groups: note.groups, canvas_height: note.canvas_height,
    }));
    if (undoStack.length > UNDO_DEPTH) undoStack.shift();
}

function undoNotes() {
    if (!note || !undoStack.length) return;
    const snap = JSON.parse(undoStack.pop());
    note.blocks = snap.blocks;
    note.groups = snap.groups;
    note.canvas_height = snap.canvas_height;
    selectedIds = new Set();
    renderCanvas();
    scheduleSave();
}

// =====================================================================
// Note list / open / create / rename / delete / pin
// =====================================================================

async function loadNoteIndex() {
    const email = getEmail();
    if (!email) { noteIndex = []; return; }
    try {
        const res = await fetch(`${API_BASE}/api/notes/${encodeURIComponent(email)}`);
        if (!res.ok) throw new Error(res.status);
        noteIndex = (await res.json()).notes || [];
    } catch (e) {
        noteIndex = [];
    }
    renderNotePicker();
}

function renderNotePicker() {
    const sel = document.getElementById("notes-picker");
    if (!sel) return;
    if (!noteIndex.length) {
        sel.innerHTML = `<option value="">No notes yet</option>`;
        return;
    }
    sel.innerHTML = noteIndex.map(n => {
        const pin = n.pinned_session_id ? "📌 " : "";
        const doc = n.has_doc ? " (PDF)" : "";
        return `<option value="${escapeHtml(n.id)}"${n.id === activeNoteId ? " selected" : ""}>` +
               `${pin}${escapeHtml(n.title)}${doc}</option>`;
    }).join("");
}

async function openNote(noteId) {
    const email = getEmail();
    if (!email || !noteId) return;
    await flushSave();
    try {
        const res = await fetch(
            `${API_BASE}/api/notes/${encodeURIComponent(email)}/${noteId}`);
        if (!res.ok) throw new Error(res.status);
        note = await res.json();
    } catch (e) {
        notesToast("Could not open that note", true);
        return;
    }
    activeNoteId = noteId;
    selectedIds = new Set();
    undoStack = [];
    editing = null;
    if (!note.canvas_height) note.canvas_height = NOTES_MIN_CANVAS_H;
    if (!note.groups) note.groups = {};
    layoutPages();
    renderCanvas();
    renderNotePicker();
    syncPinBtn();
    setSaveState("");
    saveNotesUi();
}

async function onNotePicked() {
    const sel = document.getElementById("notes-picker");
    if (sel && sel.value) await openNote(sel.value);
}

async function createNote(title) {
    const email = getEmail();
    if (!email) { notesToast("Set your email first", true); return null; }
    const name = typeof title === "string" && title
        ? title
        : prompt("Name this note:", "Untitled note");
    if (name === null) return null;
    try {
        const res = await fetch(`${API_BASE}/api/notes/${encodeURIComponent(email)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: name || "Untitled note" }),
        });
        if (!res.ok) throw new Error(res.status);
        const created = await res.json();
        await loadNoteIndex();
        await openNote(created.id);
        return created;
    } catch (e) {
        notesToast("Could not create that note", true);
        return null;
    }
}

async function renameNote() {
    if (!note) return;
    const name = prompt("Rename note:", note.title);
    if (!name) return;
    note.title = name;
    renderNotePicker();
    // Ship just the title — the block array doesn't need to travel for a rename.
    const email = getEmail();
    try {
        await fetch(`${API_BASE}/api/notes/${encodeURIComponent(email)}/${activeNoteId}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: name }),
        });
        await loadNoteIndex();
    } catch (e) {
        notesToast("Rename failed", true);
    }
}

async function deleteNote() {
    if (!note) return;
    if (!confirm(`Delete "${note.title}"? This cannot be undone.`)) return;
    const email = getEmail();
    try {
        const res = await fetch(
            `${API_BASE}/api/notes/${encodeURIComponent(email)}/${activeNoteId}`,
            { method: "DELETE" });
        if (!res.ok) throw new Error(res.status);
    } catch (e) {
        notesToast("Delete failed", true);
        return;
    }
    note = null;
    activeNoteId = null;
    await loadNoteIndex();
    if (noteIndex.length) await openNote(noteIndex[0].id);
    else renderCanvas();
}

function syncPinBtn() {
    const btn = document.getElementById("notes-pin-btn");
    if (!btn) return;
    const pinned = note && note.pinned_session_id
        && note.pinned_session_id === currentSessionId;
    btn.classList.toggle("is-on", !!pinned);
    // currentSessionId is null until the first turn completes, and pinning to
    // null would silently do nothing — say why instead.
    btn.disabled = !note || !currentSessionId;
    btn.title = !currentSessionId
        ? "Send a message first, then you can pin this note to the chat"
        : pinned ? "Unpin from this chat" : "Pin this note to this chat";
}

async function togglePinNote() {
    if (!note || !currentSessionId) return;
    const email = getEmail();
    const willPin = note.pinned_session_id !== currentSessionId;
    try {
        const res = await fetch(
            `${API_BASE}/api/notes/${encodeURIComponent(email)}/${activeNoteId}/pin`,
            {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ session_id: willPin ? currentSessionId : null }),
            });
        if (!res.ok) throw new Error(res.status);
        note.pinned_session_id = willPin ? currentSessionId : null;
        syncPinBtn();
        await loadNoteIndex();
        notesToast(willPin ? "Pinned to this chat" : "Unpinned");
    } catch (e) {
        notesToast("Could not update the pin", true);
    }
}

/**
 * Called when the main chat's session id appears or changes.
 *
 * Editing a message forks a new session, so a note pinned to the old id would
 * silently vanish from a conversation the user still thinks of as the same
 * one. Re-pin it forward. (The server also walks parent_session_id, so this
 * is belt and braces — but it keeps the pin exact rather than inherited.)
 */
async function notesOnSessionChange(newSessionId) {
    if (!newSessionId) return;
    const email = getEmail();
    const previous = note && note.pinned_session_id;

    if (note && previous && previous !== newSessionId) {
        try {
            await fetch(
                `${API_BASE}/api/notes/${encodeURIComponent(email)}/${activeNoteId}/pin`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ session_id: newSessionId }),
                });
            note.pinned_session_id = newSessionId;
        } catch (e) { /* keep the old pin; the ancestry walk still finds it */ }
    }
    syncPinBtn();

    // If a different note is pinned to this session, surface it.
    if (!notesOpen || !email) return;
    try {
        const res = await fetch(
            `${API_BASE}/api/notes/${encodeURIComponent(email)}/pinned/${newSessionId}`);
        if (!res.ok) return;
        const { note_id } = await res.json();
        if (note_id && note_id !== activeNoteId) await openNote(note_id);
    } catch (e) {}
}

// =====================================================================
// Pane open/close
// =====================================================================

async function toggleNotes(force) {
    const pane = notesPaneEl();
    if (!pane) return;
    notesOpen = force !== undefined ? force : !notesOpen;
    pane.style.display = notesOpen ? "flex" : "none";
    syncPaneSplit();   // show/hide the divider and re-apply the stored split

    const btn = document.getElementById("notes-btn");
    if (btn) btn.classList.toggle("is-on", notesOpen);

    if (!notesOpen) { await flushSave(); saveNotesUi(); return; }

    // The right-hand panes all want the same half of the centre column;
    // several at flex:1 leaves none of them usable.
    if (typeof notebookOpen !== "undefined" && notebookOpen
        && typeof toggleNotebook === "function") {
        toggleNotebook();
    }
    if (typeof demoOpen !== "undefined" && demoOpen
        && typeof toggleDemo === "function") {
        await toggleDemo(false);
    }

    const email = getEmail();
    if (!email) { renderCanvas(); saveNotesUi(); return; }

    if (!noteIndex.length) await loadNoteIndex();

    if (!activeNoteId) {
        const ui = loadNotesUi();
        let target = null;
        if (currentSessionId) {
            try {
                const res = await fetch(
                    `${API_BASE}/api/notes/${encodeURIComponent(email)}/pinned/${currentSessionId}`);
                if (res.ok) target = (await res.json()).note_id;
            } catch (e) {}
        }
        if (!target && ui.noteId && noteIndex.some(n => n.id === ui.noteId)) {
            target = ui.noteId;
        }
        if (!target && noteIndex.length) target = noteIndex[0].id;

        if (target) await openNote(target);
        else await createNote("My notes");
    }
    layoutPages();
    renderCanvas();
    saveNotesUi();
}

// =====================================================================
// Rendering
// =====================================================================

function renderCanvas() {
    const canvas = canvasEl();
    if (!canvas) return;

    const email = getEmail();
    const emptyHost = document.getElementById("notes-empty");
    if (emptyHost) {
        const msg = !email
            ? "Set your email above to start taking notes."
            : !note ? "Create a note to get started." : "";
        emptyHost.textContent = msg;
        emptyHost.style.display = msg ? "flex" : "none";
    }
    const toolbar = document.getElementById("notes-toolbar");
    if (toolbar) toolbar.style.display = note ? "flex" : "none";
    const addSpace = document.querySelector(".notes-add-space");
    if (addSpace) addSpace.style.display = note ? "block" : "none";

    if (!note) { canvas.style.display = "none"; return; }
    canvas.style.display = "block";
    applyZoom();          // owns both canvas dimensions, including zoom fill

    renderPages();
    renderBlocks();
    renderArrows();
    renderSelectionUi();
}

function renderPages() {
    const host = document.getElementById("notes-pages");
    if (!host) return;
    if (!note.doc || !note.doc.pages || !note.doc.pages.length) {
        host.innerHTML = "";
        return;
    }
    const scale = pageScale();
    host.innerHTML = note.doc.pages.map((pg, i) => {
        const w = Math.round(pg.w * scale);
        const h = Math.round(pg.h * scale);
        return `<div class="nb-page" style="top:${Math.round(pg.y)}px;width:${w}px;height:${h}px">
            <span class="nb-page-num">${i + 1}</span>
            <img src="${escapeHtml(pg.src)}" alt="Page ${i + 1}" loading="lazy" draggable="false">
        </div>`;
    }).join("");
}

function renderBlocks() {
    const host = blocksEl();
    if (!host) return;
    const ordered = [...note.blocks].sort((a, b) => (a.z || 0) - (b.z || 0));
    host.innerHTML = ordered.map(blockHtml).join("");
}

function blockHtml(b) {
    if (b.type === "arrow") {
        // Arrows are drawn in the SVG layer; they have no DOM box of their own.
        return "";
    }
    const sel = selectedIds.has(b.id) ? " is-selected" : "";
    const multi = selectedIds.size > 1 ? " is-multi" : "";
    const grouped = b.groupId ? " nb-grouped" : "";
    const st = b.style || {};
    const bg = b.type === "sticky" ? ` data-bg="${escapeHtml(st.bg || "amber")}"` : "";
    const style = `left:${b.x}px;top:${b.y}px;width:${b.w}px;height:${b.h}px;z-index:${b.z || 0}`;

    // Font size and colour are applied to .nb-body so every markdown element
    // inside inherits them; headings stay relative via em in the stylesheet.
    let bodyStyle = "";
    if (st.fontSize) bodyStyle += `font-size:${st.fontSize}px;`;
    if (st.fg && st.fg !== "default") {
        const c = TEXT_COLORS.find(t => t.key === st.fg);
        if (c) bodyStyle += `color:${c.css};`;
    }
    if (b.type === "text" && st.bg && st.bg !== "none") {
        bodyStyle += `background:${stickySwatch(st.bg)};color:#1c1917;`;
    }
    // Transparent keeps a dashed outline so an empty box is still findable
    // and draggable — an invisible block with no handle is unusable.
    if (st.bg === "none") {
        bodyStyle += "background:transparent;border-style:dashed;";
    }
    if (st.opacity && st.opacity < 1) bodyStyle += `opacity:${st.opacity};`;

    return `<div class="nb-block nb-${b.type}${sel}${multi}${grouped}" data-id="${b.id}"${bg} style="${style}">
        <div class="nb-body"${bodyStyle ? ` style="${bodyStyle}"` : ""}>${blockBodyHtml(b)}</div>
        <div class="nb-resize" data-resize="${b.id}"></div>
    </div>`;
}

function blockBodyHtml(b) {
    const st = b.style || {};
    switch (b.type) {
        case "text":
        case "sticky":
            return b.content
                ? renderNoteMarkdown(b.content)
                : `<span class="nb-placeholder">Double-click to write…</span>`;
        case "code":
            return `<div class="nb-code-lang">${escapeHtml(st.lang || "code")}</div>
                    <pre><code>${escapeHtml(b.content || "")}</code></pre>`;
        case "image":
            if (st.uploading) return "Uploading image…";
            return st.src
                ? `<img src="${escapeHtml(st.src)}" alt="${escapeHtml(st.alt || "note image")}" draggable="false">`
                : `<span class="nb-placeholder">Image unavailable</span>`;
        case "shape":
            return shapeSvg(b) + `<div class="nb-shape-label">${
                b.content ? renderNoteMarkdown(b.content) : ""}</div>`;
        default:
            return "";
    }
}

function shapeColor(name, fallback) {
    if (name === "none") return "transparent";
    // Literal hex, matching STROKE_COLORS: an SVG stroke set to a CSS var
    // resolves against the SVG element, and the swatch in the toolbar has to
    // show the same colour that actually renders.
    const c = STROKE_COLORS.find(s => s.key === name);
    if (c) return c.css;
    const fills = { amber: "#fde68a", green: "#bbf7d0", blue: "#bfdbfe",
                    pink: "#fbcfe8", violet: "#ddd6fe" };
    return fills[name] || fallback;
}

function shapeSvg(b) {
    const st = b.style || {};
    const stroke = shapeColor(st.stroke, "var(--accent)");
    const fill = shapeColor(st.bg, "transparent");
    const kind = st.shape || "rect";
    // viewBox 0..100 with preserveAspectRatio=none lets one path stretch to
    // any block size, so resizing needs no re-computation of geometry.
    let shape;
    if (kind === "ellipse") {
        shape = `<ellipse cx="50" cy="50" rx="48" ry="48" vector-effect="non-scaling-stroke"
                  fill="${fill}" stroke="${stroke}" stroke-width="2"/>`;
    } else if (kind === "diamond") {
        shape = `<polygon points="50,2 98,50 50,98 2,50" vector-effect="non-scaling-stroke"
                  fill="${fill}" stroke="${stroke}" stroke-width="2"/>`;
    } else {
        shape = `<rect x="2" y="2" width="96" height="96" rx="4" vector-effect="non-scaling-stroke"
                  fill="${fill}" stroke="${stroke}" stroke-width="2"/>`;
    }
    return `<svg viewBox="0 0 100 100" preserveAspectRatio="none">${shape}</svg>`;
}

/**
 * Arrows live in one SVG layer under the blocks.
 *
 * Each arrow gets its own <marker>: SVG markers do not inherit the stroke of
 * the line that references them, so a shared marker would force every arrow
 * to be one colour. The transparent wide duplicate line is the hit target —
 * a 2px line is essentially unclickable.
 */
function renderArrows() {
    const svg = document.getElementById("notes-arrows");
    if (!svg) return;
    const arrows = note.blocks.filter(b => b.type === "arrow")
                              .sort((a, b) => (a.z || 0) - (b.z || 0));
    if (!arrows.length) { svg.innerHTML = ""; return; }

    let defs = "", body = "";
    for (const a of arrows) {
        const st = a.style || {};
        const x1 = st.flipX ? a.x + a.w : a.x;
        const x2 = st.flipX ? a.x : a.x + a.w;
        const y1 = st.flipY ? a.y + a.h : a.y;
        const y2 = st.flipY ? a.y : a.y + a.h;
        const col = shapeColor(st.stroke, "var(--accent)");
        const mid = "m_" + a.id;
        defs += `<marker id="${mid}" viewBox="0 0 10 10" refX="9" refY="5"
                   markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                   <path d="M0,0 L10,5 L0,10 z" fill="${col}"/></marker>`;
        const head = st.head !== "none" ? ` marker-end="url(#${mid})"` : "";
        const tail = st.head === "both" ? ` marker-start="url(#${mid})"` : "";
        const w = selectedIds.has(a.id) ? 3 : 2;
        const dash = selectedIds.has(a.id) ? ` stroke-dasharray="6 3"` : "";
        body += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"
                   stroke="${col}" stroke-width="${w}"${dash}${head}${tail}/>
                 <line class="nb-arrow-hit" data-id="${a.id}"
                   x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"
                   stroke="transparent" stroke-width="16"/>`;
    }
    // Everything interpolated is generated (ids, fixed colour names, numbers).
    svg.innerHTML = `<defs>${defs}</defs>${body}`;
}

function positionBlock(b) {
    const el = blockEl(b.id);
    if (!el) { renderArrows(); return; }
    el.style.left = b.x + "px";
    el.style.top = b.y + "px";
    el.style.width = b.w + "px";
    el.style.height = b.h + "px";
}

function applyZ() {
    for (const b of note.blocks) {
        const el = blockEl(b.id);
        if (el) el.style.zIndex = b.z || 0;
    }
    renderArrows();
}

// =====================================================================
// Selection
// =====================================================================

/**
 * Clicking any member of a group selects the whole group.
 *
 * Honouring the grouping promise here — at selection time — means drag,
 * delete, z-order and styling all inherit it for free instead of each
 * re-implementing the same lookup.
 */
function expandSelection(ids) {
    const out = new Set();
    for (const id of ids) {
        const b = blockById(id);
        if (!b) continue;
        if (b.groupId) {
            for (const o of note.blocks) if (o.groupId === b.groupId) out.add(o.id);
        } else {
            out.add(id);
        }
    }
    return out;
}

function setSelection(ids) {
    selectedIds = expandSelection(new Set(ids));
    renderSelectionUi();
}

function toggleSelected(id) {
    const next = new Set(selectedIds);
    if (next.has(id)) {
        const b = blockById(id);
        if (b && b.groupId) {
            for (const o of note.blocks) if (o.groupId === b.groupId) next.delete(o.id);
        } else next.delete(id);
    } else next.add(id);
    setSelection(next);
}

function renderSelectionUi() {
    document.querySelectorAll(".nb-block").forEach(el => {
        const on = selectedIds.has(el.dataset.id);
        el.classList.toggle("is-selected", on);
        el.classList.toggle("is-multi", on && selectedIds.size > 1);
    });
    renderArrows();
    renderSelToolbar();
}

function selectionBBox() {
    let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    for (const id of selectedIds) {
        const b = blockById(id);
        if (!b) continue;
        x1 = Math.min(x1, b.x); y1 = Math.min(y1, b.y);
        x2 = Math.max(x2, b.x + b.w); y2 = Math.max(y2, b.y + b.h);
    }
    return x1 === Infinity ? null : { x1, y1, x2, y2 };
}

/**
 * The formatting ribbon, fixed in the pane header like a word processor.
 *
 * Deliberately not a popup over the canvas: a floating bar covers the block
 * you're editing, moves as the selection moves, and can't stay open while you
 * type. Fixed means the controls are always in the same place and can act on
 * the caret selection mid-edit.
 *
 * Targets, in priority order: the text selected inside an open editor, else
 * the whole block being edited, else the selected blocks.
 */
function renderFormatBar() {
    const bar = document.getElementById("notes-format");
    if (!bar) return;

    const blocks = editing
        ? [blockById(editing.blockId)].filter(Boolean)
        : [...selectedIds].map(blockById).filter(Boolean);

    if (!note || !blocks.length) {
        const panHint = zoom > 1
            ? " · hold space or ⌥ and drag to pan"
            : "";
        bar.innerHTML = `<span class="nb-format-hint">Select a block, or double-click the canvas to write${panHint}</span>`;
        return;
    }

    const anyGrouped = blocks.some(b => b.groupId);
    // Which controls apply depends on what's selected — stroke colours for a
    // text block or headings for an arrow would just be noise.
    const hasProse = blocks.some(b => b.type === "text" || b.type === "sticky");
    const hasFill = blocks.some(b => ["text", "sticky", "shape"].includes(b.type));
    const hasStroke = blocks.some(b => b.type === "shape" || b.type === "arrow");
    const hasArrow = blocks.some(b => b.type === "arrow");
    const fs = (blocks[0].style && blocks[0].style.fontSize) || 14;

    const sep = `<span class="nb-tool-sep"></span>`;
    let html = "";

    if (hasProse) {
        html += `<button class="header-btn" data-head="1" title="Heading 1">H1</button>
                 <button class="header-btn" data-head="2" title="Heading 2">H2</button>
                 <button class="header-btn" data-head="3" title="Heading 3">H3</button>
                 <button class="header-btn" data-head="0" title="Body text">¶</button>
                 ${sep}
                 <button class="header-btn" data-wrap="**" title="Bold (⌘B)"><b>B</b></button>
                 <button class="header-btn" data-wrap="_" title="Italic (⌘I)"><i>I</i></button>
                 <button class="header-btn" data-wrap="\`" title="Inline code">&lt;&gt;</button>
                 <button class="header-btn" data-list="- " title="Bullet list">&bull;</button>
                 <button class="header-btn" data-list="1. " title="Numbered list">1.</button>
                 <button class="header-btn" data-list="- [ ] " title="Checklist">&#9744;</button>
                 ${sep}
                 <button class="header-btn" data-font="-2" title="Smaller">A&minus;</button>
                 <span class="nb-fontsize">${fs}</span>
                 <button class="header-btn" data-font="2" title="Larger">A+</button>
                 ${sep}`;
        html += TEXT_COLORS.map(c =>
            `<button class="nb-swatch nb-swatch-fg" data-fg="${c.key}" title="Text: ${c.key}"
                     style="color:${c.css}">A</button>`).join("") + sep;
    }

    if (hasFill) {
        html += STICKY_COLORS.map(c =>
            `<button class="nb-swatch" data-color="${c}" title="Fill: ${c}"
                     style="background:${stickySwatch(c)}"></button>`).join("");
        // Transparent is the one that matters over a PDF: annotate without
        // hiding the page underneath.
        html += `<button class="nb-swatch nb-swatch-none" data-color="none"
                         title="Transparent (see the page through it)">&empty;</button>`;
        const op = blocks[0].style && blocks[0].style.opacity;
        html += `<select class="nb-select" data-opacity title="Opacity">
            ${[100, 80, 60, 40, 20].map(v =>
                `<option value="${v / 100}"${(op || 1) === v / 100 ? " selected" : ""}
                 >${v}%</option>`).join("")}
        </select>${sep}`;
    }

    if (hasStroke) {
        html += STROKE_COLORS.map(c =>
            `<button class="nb-swatch nb-swatch-stroke" data-stroke="${c.key}"
                     title="Line: ${c.key}" style="border-color:${c.css}"></button>`).join("") + sep;
    }

    if (hasArrow) {
        html += `<button class="header-btn" data-head-style="end" title="Arrow one end">&rarr;</button>
                 <button class="header-btn" data-head-style="both" title="Arrow both ends">&harr;</button>
                 <button class="header-btn" data-head-style="none" title="Plain line">&mdash;</button>
                 ${sep}`;
    }

    if (!editing) {
        if (selectedIds.size > 1) html += `<button class="header-btn" data-act="group">Group</button>`;
        if (anyGrouped) html += `<button class="header-btn" data-act="ungroup">Ungroup</button>`;
        html += `<button class="header-btn" data-act="front" title="Bring to front">&uarr;</button>
                 <button class="header-btn" data-act="delete" title="Delete (Del)">&times;</button>`;
    }

    bar.innerHTML = html;
}

// Kept as the single entry point callers already use.
function renderSelToolbar() { renderFormatBar(); }

function stickySwatch(c) {
    return { amber: "#fde68a", green: "#bbf7d0", blue: "#bfdbfe",
             pink: "#fbcfe8", violet: "#ddd6fe" }[c] || "#fde68a";
}

// =====================================================================
// Blocks: create / mutate / delete
// =====================================================================

function addBlock(spec) {
    if (!note) return null;
    pushUndo();
    const b = Object.assign({
        id: newId("b_"),
        type: "text",
        // Fallback only — every real creation path passes explicit,
        // zoom-relative geometry. Scaled here too so it can't be the one
        // place that ignores zoom.
        x: 40, y: 40, w: 280 / zoom, h: 120 / zoom,
        z: note.next_z++,
        groupId: null,
        content: "",
        style: {},
    }, spec);
    note.blocks.push(b);
    maybeGrowCanvas();
    renderCanvas();
    return b;
}

function removeBlocks(ids) {
    if (!note || !ids.size) return;
    pushUndo();
    note.blocks = note.blocks.filter(b => !ids.has(b.id));
    selectedIds = new Set();
    renderCanvas();
    scheduleSave();
}

function bringToFront(ids) {
    if (!note) return;
    // Keep members' relative order and make them contiguous, so a non-member
    // can't end up sandwiched inside a group that was raised as a unit.
    const members = note.blocks.filter(b => ids.has(b.id))
                               .sort((a, b) => (a.z || 0) - (b.z || 0));
    for (const b of members) b.z = note.next_z++;
    applyZ();
    scheduleSave();
}

function groupSelected() {
    if (!note || selectedIds.size < 2) return;
    pushUndo();
    const gid = newId("g_");
    for (const id of selectedIds) {
        const b = blockById(id);
        if (b) b.groupId = gid;
    }
    note.groups[gid] = { id: gid };
    renderCanvas();
    scheduleSave();
}

function ungroupSelected() {
    if (!note || !selectedIds.size) return;
    pushUndo();
    const touched = new Set();
    for (const id of selectedIds) {
        const b = blockById(id);
        if (b && b.groupId) { touched.add(b.groupId); b.groupId = null; }
    }
    for (const gid of touched) delete note.groups[gid];
    renderCanvas();
    scheduleSave();
}

/**
 * Set or clear the heading level of every selected prose block.
 *
 * Rewrites the markdown source rather than storing a separate "level" field,
 * so the content stays plain markdown — it round-trips to the chat, to a
 * paste, and to anything else that understands markdown, with no decoder.
 */
function headingSelected(level) {
    if (!note || !selectedIds.size) return;
    pushUndo();
    for (const id of selectedIds) {
        const b = blockById(id);
        if (!b || (b.type !== "text" && b.type !== "sticky")) continue;
        const lines = (b.content || "").split("\n");
        // Only the first line carries the block's heading; deeper lines are
        // the user's own structure and must be left alone.
        const idx = lines.findIndex(l => l.trim() !== "");
        if (idx === -1) {
            if (level > 0) b.content = "#".repeat(level) + " ";
            continue;
        }
        const bare = lines[idx].replace(/^\s*#{1,6}\s*/, "");
        lines[idx] = level > 0 ? "#".repeat(level) + " " + bare : bare;
        b.content = lines.join("\n");
    }
    renderCanvas();
    scheduleSave();
}

/**
 * Wrap each selected block's text in a markdown marker (bold/italic/code).
 *
 * Applies to the whole block: there is no caret to read outside edit mode,
 * and a per-word toolbar would need a live selection inside a textarea that
 * has already lost focus to the toolbar itself.
 */
/**
 * Wrap the caret selection inside the open editor, Word-style.
 *
 * Returns true if it handled the action. The textarea's selectionStart/End
 * survive the toolbar taking focus because the ribbon's mousedown calls
 * preventDefault, so the caret never actually leaves.
 */
function wrapInEditor(marker) {
    if (!editing) return false;
    const ta = editing.ta;
    const { selectionStart: s, selectionEnd: e, value } = ta;
    if (s === e) return false;                 // nothing selected — fall back

    const picked = value.slice(s, e);
    const already = picked.startsWith(marker) && picked.endsWith(marker)
        && picked.length > marker.length * 2;
    const next = already
        ? picked.slice(marker.length, -marker.length)
        : marker + picked + marker;

    ta.value = value.slice(0, s) + next + value.slice(e);
    // Keep the same text selected so the button can be toggled straight back.
    ta.setSelectionRange(s, s + next.length);
    ta.focus();
    const b = blockById(editing.blockId);
    if (b) b.content = ta.value;
    scheduleSave();
    return true;
}

/** Prefix every selected line (or the caret's line) with a list marker. */
function listInEditor(marker) {
    if (!editing) return false;
    const ta = editing.ta;
    const { selectionStart: s, selectionEnd: e, value } = ta;

    const from = value.lastIndexOf("\n", s - 1) + 1;
    let to = value.indexOf("\n", e);
    if (to === -1) to = value.length;

    const lines = value.slice(from, to).split("\n");
    // Numbered lists renumber; bullets and checkboxes repeat the same marker.
    const numbered = /^\d+\.\s/.test(marker);
    const stripped = lines.map(l => l.replace(/^(\s*)(?:[-*]\s\[[ xX]\]\s|[-*]\s|\d+\.\s)/, "$1"));
    const allHad = lines.every((l, i) => l !== stripped[i]);

    const next = allHad
        ? stripped
        : stripped.map((l, i) => {
            const indent = l.match(/^\s*/)[0];
            const body = l.slice(indent.length);
            return indent + (numbered ? `${i + 1}. ` : marker) + body;
        });

    const replacement = next.join("\n");
    ta.value = value.slice(0, from) + replacement + value.slice(to);
    ta.setSelectionRange(from, from + replacement.length);
    ta.focus();
    const b = blockById(editing.blockId);
    if (b) b.content = ta.value;
    scheduleSave();
    return true;
}

/** Set the heading level of the caret's line inside the open editor. */
function headingInEditor(level) {
    if (!editing) return false;
    const ta = editing.ta;
    const { selectionStart: s, value } = ta;
    const from = value.lastIndexOf("\n", s - 1) + 1;
    let to = value.indexOf("\n", s);
    if (to === -1) to = value.length;

    const bare = value.slice(from, to).replace(/^\s*#{1,6}\s*/, "");
    const next = level > 0 ? "#".repeat(level) + " " + bare : bare;
    ta.value = value.slice(0, from) + next + value.slice(to);
    ta.setSelectionRange(from + next.length, from + next.length);
    ta.focus();
    const b = blockById(editing.blockId);
    if (b) b.content = ta.value;
    scheduleSave();
    return true;
}

function wrapSelected(marker) {
    if (!note || !selectedIds.size) return;
    pushUndo();
    for (const id of selectedIds) {
        const b = blockById(id);
        if (!b || (b.type !== "text" && b.type !== "sticky")) continue;
        const text = (b.content || "").trim();
        if (!text) continue;
        b.content = text.split("\n").map(line => {
            const m = line.match(/^(\s*#{1,6}\s*|\s*[-*]\s+)?(.*)$/);
            const prefix = m[1] || "";
            const body = m[2];
            if (!body.trim()) return line;
            // Toggle: strip the marker if it's already wrapping this line.
            if (body.startsWith(marker) && body.endsWith(marker)
                && body.length > marker.length * 2) {
                return prefix + body.slice(marker.length, -marker.length);
            }
            return prefix + marker + body + marker;
        }).join("\n");
    }
    renderCanvas();
    scheduleSave();
}

/**
 * Blocks a styling action applies to: the one being edited, else the
 * selection. Editing implies a target even though selectedIds is empty.
 */
function styleTargets() {
    if (editing) {
        const b = blockById(editing.blockId);
        return b ? [b] : [];
    }
    return [...selectedIds].map(blockById).filter(Boolean);
}

/**
 * Re-render after a style change without disturbing an open editor.
 *
 * renderCanvas() rebuilds .notes-blocks, which would rip out the live
 * textarea and drop the caret. While editing, write the affected properties
 * straight onto the existing .nb-body instead.
 */
function refreshAfterStyle(targets) {
    if (!editing) { renderCanvas(); return; }
    for (const b of targets) {
        const el = blockEl(b.id);
        if (!el) continue;
        const body = el.querySelector(".nb-body");
        if (!body) continue;
        const st = b.style || {};
        body.style.fontSize = st.fontSize ? st.fontSize + "px" : "";
        const c = TEXT_COLORS.find(t => t.key === st.fg);
        body.style.color = c && st.fg !== "default" ? c.css : "";
        if (st.bg && st.bg !== "none") {
            body.style.background = stickySwatch(st.bg);
            body.style.color = "#1c1917";
            body.style.borderStyle = "";
        } else if (st.bg === "none") {
            body.style.background = "transparent";
            body.style.borderStyle = "dashed";
        }
        body.style.opacity = st.opacity && st.opacity < 1 ? String(st.opacity) : "";
        // The editor sits inside .nb-body, so it inherits size but needs the
        // colour explicitly — a textarea does not inherit `color`.
        if (editing.ta) {
            editing.ta.style.fontSize = body.style.fontSize;
            editing.ta.style.color = body.style.color;
        }
    }
    renderFormatBar();
}

function bumpFontSize(delta) {
    const targets = styleTargets();
    if (!note || !targets.length) return;
    pushUndo();
    for (const b of targets) {
        const cur = (b.style && b.style.fontSize) || 14;
        b.style = Object.assign({}, b.style, {
            fontSize: Math.max(FONT_SIZE_MIN, Math.min(FONT_SIZE_MAX, cur + delta)),
        });
    }
    refreshAfterStyle(targets);
    scheduleSave();
}

function styleSelected(patch) {
    const targets = styleTargets();
    if (!note || !targets.length) return;
    pushUndo();
    for (const b of targets) {
        b.style = Object.assign({}, b.style, patch);
        // "No fill" on a sticky turns it back into a plain text block, so the
        // colour buttons are reversible rather than a one-way conversion.
        if (patch.bg === "none" && b.type === "sticky") {
            b.type = "text";
            delete b.style.bg;
        }
    }
    refreshAfterStyle(targets);
    scheduleSave();
}

// =====================================================================
// Canvas growth
// =====================================================================

function contentBottom() {
    let bottom = NOTES_MIN_CANVAS_H;
    if (note.doc && note.doc.pages && note.doc.pages.length) {
        const scale = pageScale();
        const last = note.doc.pages[note.doc.pages.length - 1];
        bottom = Math.max(bottom, last.y + last.h * scale + 40);
    }
    for (const b of note.blocks) bottom = Math.max(bottom, b.y + b.h);
    return bottom;
}

function maybeGrowCanvas() {
    if (!note) return;
    const want = contentBottom() + NOTES_GROW_MARGIN;
    if (want > note.canvas_height) {
        note.canvas_height = want;
        applyZoom();     // writes both canvas dimensions and the wrapper
    }
}

function addCanvasSpace() {
    if (!note) return;
    note.canvas_height += NOTES_GROW_STEP;
    applyZoom();     // single writer for the canvas and wrapper dimensions
    scrollEl().scrollTop = scrollEl().scrollHeight;
    scheduleSave();
}

// =====================================================================
// PDF pages
// =====================================================================

function pageScale() {
    if (!note || !note.doc || !note.doc.pages || !note.doc.pages.length) return 1;
    return canvasWidth() / note.doc.pages[0].w;
}

/** Stack pages down the canvas at the current pane width. */
function layoutPages() {
    if (!note || !note.doc || !note.doc.pages || !note.doc.pages.length) return;
    const scale = pageScale();
    let y = 0;
    for (const pg of note.doc.pages) {
        pg.y = y;
        y += pg.h * scale + (note.doc.page_gap || 24);
    }
    if (note.canvas_height < y + 80) note.canvas_height = y + 80;
    if (!note.doc.layout_width) note.doc.layout_width = canvasWidth();
}

/**
 * Keep annotations glued to the passage they annotate when the pane resizes.
 *
 * Pages scale with pane width but blocks are stored in absolute canvas
 * coordinates, so without this a collapsed sidebar would slide every note off
 * its content. Scale blocks by the same ratio the pages move by.
 */
function rescaleForWidth() {
    if (!note || !note.doc || !note.doc.layout_width) return;
    // Via canvasWidth(), which measures the scroll container's offsetWidth:
    // the canvas carries the zoom transform, and clientWidth would swing with
    // the scrollbar. Either would let a zoom masquerade as a pane resize and
    // permanently multiply every block's coordinates.
    const now = canvasWidth();
    const was = note.doc.layout_width;
    if (!was || !now || Math.abs(now - was) < 2) return;

    const r = now / was;
    // A legitimate pane resize is at most a few times the old width. Anything
    // beyond that is a runaway loop, and applying it would corrupt the note
    // irreversibly — refuse and resync instead.
    if (r > 4 || r < 0.25) {
        console.warn("[notes] refusing implausible rescale", { was, now, ratio: r });
        note.doc.layout_width = now;
        return;
    }
    for (const b of note.blocks) {
        b.x *= r; b.y *= r; b.w *= r; b.h *= r;
    }
    note.canvas_height *= r;
    note.doc.layout_width = now;
    layoutPages();
    renderCanvas();
    scheduleSave();
}

async function importPdfFile(file) {
    if (!note) { notesToast("Open or create a note first", true); return; }
    const email = getEmail();
    if (!email) return;

    if (/\.sdocx?$/i.test(file.name)) {
        notesToast("Samsung Notes files can't be read directly — in Samsung Notes use "
            + "Save as file → PDF, then drop that here.", true);
        return;
    }
    if (!pdfImportAvailable) {
        notesToast("PDF import needs PyMuPDF on the server", true);
        return;
    }

    setBusy(true, `Importing ${file.name}…`);
    try {
        const fd = new FormData();
        fd.append("file", file);
        const res = await fetch(
            `${API_BASE}/api/notes/${encodeURIComponent(email)}/${activeNoteId}/document`,
            { method: "POST", body: fd });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || "Import failed");
        }
        const { doc } = await res.json();
        note.doc = doc;
        note.doc.layout_width = canvasWidth();
        layoutPages();
        renderCanvas();
        scheduleSave();
        notesToast(`Imported ${doc.pages.length} page${doc.pages.length === 1 ? "" : "s"}`);
    } catch (e) {
        notesToast(String(e.message || e), true);
    } finally {
        setBusy(false);
    }
}

function pickPdf() {
    const input = document.getElementById("notes-pdf-input");
    if (input) input.click();
}

async function clearPdf() {
    if (!note || !note.doc) return;
    if (!confirm("Remove the PDF pages? Your notes on top will be kept.")) return;
    const email = getEmail();
    try {
        await fetch(
            `${API_BASE}/api/notes/${encodeURIComponent(email)}/${activeNoteId}/document`,
            { method: "DELETE" });
        note.doc = null;
        renderCanvas();
    } catch (e) {
        notesToast("Could not remove the PDF", true);
    }
}

function jumpToPage(n) {
    if (!note || !note.doc || !note.doc.pages) return;
    const pg = note.doc.pages[n - 1];
    if (!pg) return;
    // pg.y is a model coordinate; the scroll container works in screen pixels.
    scrollEl().scrollTop = pg.y * zoom;
}

// =====================================================================
// Images
// =====================================================================

function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
        const fr = new FileReader();
        fr.onload = () => {
            const s = String(fr.result);
            resolve(s.slice(s.indexOf(",") + 1));   // strip the data: prefix
        };
        fr.onerror = () => reject(fr.error);
        fr.readAsDataURL(blob);
    });
}

async function insertImageBlock(file, pt) {
    if (!note) return;
    if (!NOTE_IMAGE_TYPES.includes(file.type)) {
        notesToast("That image type isn't supported", true);
        return;
    }
    if (file.size > MAX_NOTE_IMAGE_BYTES) {
        notesToast("Image is larger than 5MB", true);
        return;
    }
    // Optimistic placeholder, so a slow upload doesn't look like nothing
    // happened. Removed again if the upload fails.
    const ph = addBlock({
        // Zoom-relative, so a screenshot pasted while zoomed into a small
        // region lands sized for that region.
        type: "image", x: pt.x, y: pt.y, w: 320 / zoom, h: 220 / zoom,
        style: { uploading: true },
    });
    try {
        const b64 = await blobToBase64(file);
        const email = getEmail();
        const res = await fetch(
            `${API_BASE}/api/notes/${encodeURIComponent(email)}/${activeNoteId}/images`,
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ data: b64, content_type: file.type }),
            });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || "upload failed");
        }
        const j = await res.json();
        const live = blockById(ph.id);
        if (!live) return;                      // deleted mid-upload
        live.style = { src: j.src, alt: file.name || "pasted image" };
        if (j.w && j.h) live.h = Math.round(live.w * (j.h / j.w));
        maybeGrowCanvas();
        renderCanvas();
        scheduleSave();
    } catch (e) {
        note.blocks = note.blocks.filter(b => b.id !== ph.id);
        renderCanvas();
        notesToast(String(e.message || e), true);
    }
}

// =====================================================================
// Chat capture — select text in a message, send it to the open note
// =====================================================================

/**
 * The button is one fixed element on <body>, driven by a document-level
 * mouseup listener rather than a child of each message. Every innerHTML
 * rewrite during streaming destroys message children (see app.js), so a
 * per-message button would have to be re-attached constantly; this has
 * nothing to re-attach.
 */
function initSendToNotes() {
    const btn = document.createElement("button");
    btn.className = "send-to-notes-btn";
    btn.textContent = "✎ Send to notes";
    btn.hidden = true;
    document.body.appendChild(btn);

    let captured = "";

    // mousedown + preventDefault, not click: a click handler fires after the
    // selection has already collapsed, leaving nothing to capture.
    btn.addEventListener("mousedown", (e) => {
        e.preventDefault();
        if (captured) sendSelectionToNotes(captured);
        btn.hidden = true;
    });

    document.addEventListener("mouseup", (e) => {
        if (btn.contains(e.target)) return;
        // Deferred a tick: on mouseup the selection isn't final yet in WebKit,
        // so reading it synchronously yields the previous range.
        setTimeout(() => {
            const sel = window.getSelection();
            const text = sel ? String(sel).trim() : "";
            let inChat = false;
            if (sel && sel.rangeCount && text) {
                const node = sel.getRangeAt(0).commonAncestorContainer;
                const el = node.nodeType === 1 ? node : node.parentElement;
                inChat = !!(el && el.closest("#chat-messages, #side-chat-messages"));
            }
            if (!text || !inChat) { btn.hidden = true; captured = ""; return; }

            captured = text;
            const r = sel.getRangeAt(0).getBoundingClientRect();
            const w = 140;
            btn.style.left = Math.max(8,
                Math.min(r.left + r.width / 2 - w / 2, window.innerWidth - w - 8)) + "px";
            btn.style.top = Math.max(8, r.top - 38) + "px";
            btn.hidden = false;
        }, 0);
    });

    document.addEventListener("mousedown", (e) => {
        if (!btn.contains(e.target)) btn.hidden = true;
    });
    document.addEventListener("scroll", () => { btn.hidden = true; }, true);
}

async function sendSelectionToNotes(text) {
    if (!notesOpen) await toggleNotes(true);
    if (!activeNoteId) await createNote("My notes");
    if (!note) return;

    // Append below everything, so captures accumulate as a running log rather
    // than stacking on top of each other at a fixed point.
    let bottom = 24;
    for (const b of note.blocks) bottom = Math.max(bottom, b.y + b.h + 16);

    const width = Math.max(220, canvasWidth() - 48);
    const b = addBlock({
        type: "text", x: 24, y: bottom, w: width,
        h: estimateTextHeight(text, width), content: text,
    });
    maybeGrowCanvas();
    renderCanvas();
    scheduleSave();
    const el = blockEl(b.id);
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
    notesToast("Added to notes");
}

/**
 * Rough block height for a chunk of text, in model units.
 *
 * `scale` is 1/zoom when the block's font is being scaled down: the character
 * width and line height shrink with the font, so they have to scale together
 * with the width or a zoomed-in block comes out far too tall.
 */
function estimateTextHeight(text, width, scale) {
    const s = scale || 1;
    const charW = 7.2 * s;
    const lineH = 21 * s;
    const perLine = Math.max(20, Math.floor((width || 300) / charW));
    const lines = String(text).split("\n")
        .reduce((n, l) => n + Math.max(1, Math.ceil(l.length / perLine)), 0);
    return Math.min(600 * s, Math.max(70 * s, lines * lineH + 24 * s));
}

// =====================================================================
// Editing
// =====================================================================

function enterEdit(b) {
    if (editing) exitEdit();
    const el = blockEl(b.id);
    if (!el) return;
    const body = el.querySelector(".nb-body");
    const ta = document.createElement("textarea");
    ta.className = "nb-edit" + (b.type === "code" ? " is-mono" : "");
    ta.value = b.content || "";
    ta.spellcheck = false;
    body.innerHTML = "";
    body.appendChild(ta);
    el.classList.add("is-editing");
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);

    editing = { blockId: b.id, ta };
    renderSelToolbar();

    ta.addEventListener("input", scheduleSave);
    ta.addEventListener("blur", exitEdit);
    // Keep the ribbon's active state in step with where the caret is.
    ta.addEventListener("select", renderFormatBar);
    ta.addEventListener("keyup", renderFormatBar);
    ta.addEventListener("click", renderFormatBar);
    ta.addEventListener("keydown", (ev) => {
        // Escape or Cmd/Ctrl+Enter commits. Plain Enter must stay available —
        // a markdown list needs newlines.
        if (ev.key === "Escape" || (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey))) {
            ev.preventDefault();
            ta.blur();
        } else if (ev.metaKey || ev.ctrlKey) {
            const k = ev.key.toLowerCase();
            if (k === "b") { ev.preventDefault(); wrapInEditor("**"); }
            else if (k === "i") { ev.preventDefault(); wrapInEditor("_"); }
            else if (k === "e") { ev.preventDefault(); wrapInEditor("`"); }
        }
        ev.stopPropagation();    // canvas shortcuts must not fire while typing
    });
    // Paste inside a textarea is plain text editing — don't let the canvas
    // handler turn it into a new block.
    ta.addEventListener("paste", (ev) => ev.stopPropagation());
}

function exitEdit() {
    if (!editing) return;
    const { blockId, ta } = editing;
    const b = blockById(blockId);
    editing = null;
    if (b) {
        b.content = ta.value;
        const el = blockEl(blockId);
        if (el) el.classList.remove("is-editing");
        renderCanvas();
        scheduleSave();
    }
}

// =====================================================================
// Pointer interaction: create, drag, resize, marquee
// =====================================================================

function canvasPoint(e) {
    // getBoundingClientRect already accounts for scroll — computing this from
    // scrollTop by hand is the classic off-by-scroll bug. The rect is the
    // *scaled* box, so divide the zoom back out to land in model space.
    const r = canvasEl().getBoundingClientRect();
    return {
        x: Math.round((e.clientX - r.left) / zoom),
        y: Math.round((e.clientY - r.top) / zoom),
    };
}

function applyZoom() {
    const canvas = canvasEl();
    if (!canvas) return;

    // The canvas is pinned to an explicit pixel width taken from the scroll
    // container — which is never transformed and never resized by us. That
    // breaks the feedback loop: previously the canvas was width:100% of a
    // wrapper whose width was computed from the canvas, so each zoom
    // multiplied the measurement by the zoom factor, the ResizeObserver read
    // it as a pane resize, rescaleForWidth() multiplied every block's
    // coordinates, and it ran away (677px → 15014px in a dozen frames,
    // corrupting the note on every pass).
    // Same measurement everything else uses, so the canvas, the pages and the
    // rescale check can never disagree about how wide "the pane" is.
    const modelW = canvasWidth();

    // The canvas is deliberately wider than the pane so there is somewhere to
    // pan sideways at any zoom — at exactly pane width there is no horizontal
    // scroll range at 100% and the gesture does nothing.
    //
    // This width is for the ELEMENT only. Every layout calculation —
    // canvasWidth(), pageScale(), rescaleForWidth() — keeps using the pane
    // width via canvasWidth(), which measures the scroll container and never
    // this element. That separation is load-bearing: when the canvas's own
    // width fed back into the measurement, rescaleForWidth() read it as a
    // pane resize and multiplied every block's coordinates on each pass
    // (677px → 15014px in a dozen frames, corrupting the note).
    const boardW = modelW * NOTES_CANVAS_WIDTH_FACTOR;

    // Zoomed out, a canvas of exactly boardW would occupy only `zoom` of the
    // pane and the grid would stop in mid-air. Extend it so the *scaled*
    // result still fills the viewport. Height gets the same treatment so the
    // grid reaches the bottom of a short note too.
    const fillW = zoom < 1 ? Math.ceil(boardW / zoom) : boardW;
    canvas.style.width = fillW + "px";

    // Height: at least the note's own height, but extended when zooming out
    // so the grid reaches the bottom of the viewport instead of ending in a
    // hard edge partway down.
    const host = scrollEl();
    const viewH = host ? host.clientHeight : 0;
    const modelH = note ? note.canvas_height : 0;
    const fillH = zoom < 1 ? Math.max(modelH, Math.ceil(viewH / zoom)) : modelH;
    canvas.style.height = Math.round(fillH) + "px";

    // transform-origin at the top-left keeps model (0,0) pinned to the
    // canvas corner, so no offset correction is needed anywhere else.
    canvas.style.transform = zoom === 1 ? "" : `scale(${zoom})`;
    canvas.style.transformOrigin = "0 0";
    // Counter-scale the grab handles so they stay a constant on-screen size —
    // at 0.5x a 12px dot would render as 6px and be nearly unclickable.
    canvas.style.setProperty("--nb-inv-zoom", String(1 / zoom));

    // A transform doesn't affect layout, so without a sized wrapper the
    // scroll container would clip a zoomed-in canvas. Both dimensions come
    // from model values, never from a measurement of the canvas itself.
    const wrap = document.getElementById("notes-canvas-wrap");
    if (wrap && note) {
        // Reserve the note's real scaled height, not the inflated fill height:
        // the extra canvas exists only so the grid reaches the edges, and
        // counting it would invent scroll space below the content.
        wrap.style.height = Math.round(note.canvas_height * zoom) + "px";
        // Reserve the scaled board, but never less than the canvas element
        // itself: below 1.0 the element is widened by 1/zoom so the grid still
        // fills the viewport, and a wrapper narrower than that would clip it
        // and leave nothing to pan across.
        wrap.style.width = Math.round(Math.max(boardW * zoom, fillW * zoom)) + "px";
    }
    const label = document.getElementById("notes-zoom-level");
    if (label) label.textContent = Math.round(zoom * 100) + "%";

    // Grey out at the limits, so a button that has stopped responding looks
    // like it has stopped rather than like it's broken.
    const zin = document.getElementById("notes-zoom-in");
    const zout = document.getElementById("notes-zoom-out");
    if (zin) zin.disabled = zoom >= ZOOM_MAX - 0.001;
    if (zout) zout.disabled = zoom <= ZOOM_MIN + 0.001;
}

function setZoom(next, anchorClientY) {
    const scroll = scrollEl();
    const prev = zoom;
    zoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, next));
    if (Math.abs(zoom - prev) < 0.0005) {
        // Clamped at a limit. Still refresh so the buttons show their
        // disabled state — otherwise the first press at the ceiling looks
        // like nothing happened.
        applyZoom();
        return;
    }

    // Keep whatever was under the cursor (or the viewport centre) fixed,
    // otherwise zooming in on a big PDF throws you to an unrelated page.
    let anchorDoc = null;
    if (scroll) {
        const rect = scroll.getBoundingClientRect();
        const y = anchorClientY === undefined
            ? rect.top + rect.height / 2
            : anchorClientY;
        anchorDoc = (scroll.scrollTop + (y - rect.top)) / prev;
    }

    applyZoom();

    if (scroll && anchorDoc !== null) {
        const rect = scroll.getBoundingClientRect();
        const y = anchorClientY === undefined
            ? rect.height / 2
            : anchorClientY - rect.top;
        scroll.scrollTop = anchorDoc * zoom - y;
    }
    renderFormatBar();
}

function zoomBy(dir) {
    // Multiply rather than add: +0.25 is a huge jump at 50% and a nudge at
    // 300%. A constant ratio feels the same everywhere.
    const next = dir > 0 ? zoom * ZOOM_BUTTON_RATIO : zoom / ZOOM_BUTTON_RATIO;
    // Snap to exactly 1 when passing near it, so "back to 100%" is reachable
    // by button without overshooting to 99.6%.
    setZoom(Math.abs(next - 1) < 0.06 ? 1 : next);
}

function zoomReset() { setZoom(1); }

function setTool(tool, sticky) {
    // Ignore a missing tool rather than clearing the toolbar: a non-tool
    // button that happens to share the class would otherwise leave the canvas
    // with no active tool and every button unlit.
    if (!tool) return;
    activeTool = tool;
    stickyTool = !!sticky;
    document.querySelectorAll(".nb-tool").forEach(el => {
        el.classList.toggle("is-active", el.dataset.tool === tool);
    });

    // Grab cursor while the hand is armed. Space-to-pan sets the same class
    // independently, so only clear it when space is not also being held —
    // otherwise ending a space-pan with the hand tool on drops the cursor.
    const scroll = scrollEl();
    if (scroll) {
        if (tool === "hand") scroll.classList.add("can-pan");
        else if (!spaceHeld) scroll.classList.remove("can-pan");
    }
}

/**
 * Default geometry for a new block, in model units.
 *
 * Sizes are divided by the zoom so a new block is always the same size *on
 * screen* — which means at 200% it covers half the page area it would at
 * 100%. That is the behaviour you want when annotating: zoom in to a small
 * region, and the box you draw fits that region rather than swamping it.
 * Font size is scaled the same way, so the text inside stays legible too.
 */
function toolDefaults(tool, pt) {
    const s = 1 / zoom;
    const base = { x: pt.x, y: pt.y };
    // Only record a fontSize when zoomed; at 1.0 the stylesheet default is
    // correct and writing 14 explicitly would just add noise to the document.
    const fs = zoom === 1 ? {} : { fontSize: round1(14 * s) };
    switch (tool) {
        case "text":    return Object.assign(base, { type: "text", w: 280 * s, h: 120 * s,
                                                     style: Object.assign({}, fs) });
        case "sticky":  return Object.assign(base, { type: "sticky", w: 220 * s, h: 160 * s,
                                                     style: Object.assign({ bg: "amber" }, fs) });
        case "code":    return Object.assign(base, { type: "code", w: 340 * s, h: 160 * s,
                                                     style: Object.assign({ lang: "python" }, fs) });
        case "rect":    return Object.assign(base, { type: "shape", w: 200 * s, h: 120 * s,
                                                     style: { shape: "rect", stroke: "accent" } });
        case "ellipse": return Object.assign(base, { type: "shape", w: 180 * s, h: 140 * s,
                                                     style: { shape: "ellipse", stroke: "accent" } });
        case "diamond": return Object.assign(base, { type: "shape", w: 180 * s, h: 140 * s,
                                                     style: { shape: "diamond", stroke: "accent" } });
        default:        return null;
    }
}

function round1(n) { return Math.round(n * 10) / 10; }

function onCanvasPointerDown(e) {
    if (!note || notesBusy) return;

    // Pan with the hand tool, the middle button, space held, or ⌥/alt. The
    // modifiers are the gestures map and design tools already use; the hand
    // tool is the version that needs no key held and no trackpad gesture the
    // browser might claim for itself.
    //
    // This branch returns, so everything below — selection, marquee, block
    // creation, enterEdit — is skipped while the hand is active. Pure pan mode
    // falls out of the control flow rather than needing a guard per feature.
    // An open editor keeps its own pointer events even with the hand armed:
    // the textarea is the more specific target, and panning from inside it
    // would make text unselectable for no benefit.
    const inEditor = editing && e.target.closest(".nb-edit");

    if (!inEditor
        && (e.button === 1 || spaceHeld || e.altKey || activeTool === "hand")) {
        const scroll = scrollEl();
        if (!scroll) return;
        e.preventDefault();
        drag = {
            kind: "pan",
            startX: e.clientX, startY: e.clientY,
            left0: scroll.scrollLeft, top0: scroll.scrollTop,
            pointerId: e.pointerId,
            moved: false,
        };
        canvasEl().setPointerCapture(e.pointerId);
        scroll.classList.add("is-panning");
        return;
    }

    if (e.button !== 0) return;

    // A pointerdown inside the open editor belongs to the textarea: clicking
    // to place the caret, or dragging to select text. Handling it here closed
    // the editor and started a block drag instead, so the caret could not be
    // moved with the mouse and selecting a line dragged the whole block.
    if (inEditor) return;

    if (editing) { exitEdit(); }

    const resizeNode = e.target.closest(".nb-resize");
    const pt = canvasPoint(e);
    lastCanvasPoint = pt;

    if (resizeNode) { beginResize(e, resizeNode.dataset.resize); return; }

    // The handle hangs ~5px outside the block, so a near-miss on its element
    // would otherwise fall through to "empty canvas" and clear the selection.
    if (selectedIds.size === 1) {
        const only = blockById([...selectedIds][0]);
        // Tolerance is in model units, so divide by zoom to keep the grab
        // area a constant number of screen pixels.
        const tol = 8 / zoom;
        if (only && only.type !== "arrow"
            && Math.abs(pt.x - (only.x + only.w)) <= tol
            && Math.abs(pt.y - (only.y + only.h)) <= tol) {
            beginResize(e, only.id);
            return;
        }
    }

    // Hit-test the model, not the DOM: a preceding render (selection, edit
    // commit) can have replaced the element under the cursor, and a detached
    // node's closest(".nb-block") is null — which would read as "empty
    // canvas" and spawn a block on top of the one that was clicked.
    const hitBlock = blockAtPoint(pt);

    if (activeTool !== "select" && !hitBlock) {
        if (activeTool === "arrow") { beginArrowDraw(e, pt); return; }
        const spec = toolDefaults(activeTool, pt);
        if (spec) {
            const b = addBlock(spec);
            setSelection([b.id]);
            scheduleSave();
            if (b.type === "text" || b.type === "sticky" || b.type === "code") enterEdit(b);
        }
        if (!stickyTool) setTool("select");
        return;
    }

    if (hitBlock) { beginBlockDrag(e, hitBlock.id); return; }

    // Empty canvas: rubber band.
    if (!e.shiftKey) setSelection([]);
    beginMarquee(e, pt);
}

function beginBlockDrag(e, blockId) {
    const b = blockById(blockId);
    if (!b) return;

    if (e.shiftKey || e.metaKey || e.ctrlKey) toggleSelected(blockId);
    else if (!selectedIds.has(blockId)) setSelection([blockId]);
    else setSelection(selectedIds);

    const origins = [...selectedIds].map(id => {
        const blk = blockById(id);
        return blk ? { id, x: blk.x, y: blk.y } : null;
    }).filter(Boolean);
    if (!origins.length) return;

    drag = {
        kind: "move",
        startX: e.clientX, startY: e.clientY,
        origins,
        moved: false,
        pointerId: e.pointerId,
    };
    canvasEl().classList.add("is-dragging");
    canvasEl().setPointerCapture(e.pointerId);
    e.preventDefault();
}

function beginResize(e, blockId) {
    const b = blockById(blockId);
    if (!b) return;
    drag = {
        kind: "resize",
        startX: e.clientX, startY: e.clientY,
        block: b, w0: b.w, h0: b.h,
        aspect: b.type === "image" && b.h ? b.w / b.h : null,
        moved: false,
        pointerId: e.pointerId,
    };
    canvasEl().classList.add("is-dragging");
    canvasEl().setPointerCapture(e.pointerId);
    e.preventDefault();
    e.stopPropagation();
}

function beginMarquee(e, pt) {
    drag = {
        kind: "marquee",
        startX: e.clientX, startY: e.clientY,
        ox: pt.x, oy: pt.y,
        additive: e.shiftKey,
        base: new Set(selectedIds),
        moved: false,
        pointerId: e.pointerId,
    };
    canvasEl().setPointerCapture(e.pointerId);
}

function beginArrowDraw(e, pt) {
    const b = addBlock({
        type: "arrow", x: pt.x, y: pt.y, w: 1, h: 1,
        style: { stroke: "accent", head: "end", flipX: false, flipY: false },
    });
    drag = {
        kind: "arrow",
        startX: e.clientX, startY: e.clientY,
        ox: pt.x, oy: pt.y, block: b,
        moved: false,
        pointerId: e.pointerId,
    };
    canvasEl().setPointerCapture(e.pointerId);
    e.preventDefault();
}

function onCanvasPointerMove(e) {
    if (!drag) return;

    if (drag.kind === "pan") {
        const scroll = scrollEl();
        if (!scroll) return;
        // Drag the content with the cursor, so moving right reveals what is
        // to the left — hence the subtraction.
        scroll.scrollLeft = drag.left0 - (e.clientX - drag.startX);
        scroll.scrollTop = drag.top0 - (e.clientY - drag.startY);
        drag.moved = true;
        return;
    }
    // Screen pixels, used only for the movement threshold — that should feel
    // the same regardless of zoom.
    const sdx = e.clientX - drag.startX;
    const sdy = e.clientY - drag.startY;
    if (!drag.moved && Math.abs(sdx) + Math.abs(sdy) < DRAG_THRESHOLD) return;
    drag.moved = true;

    // Model deltas: block coordinates are stored unscaled, so a 100px drag at
    // 2x must move the block 50 units or it races ahead of the cursor.
    const dx = sdx / zoom;
    const dy = sdy / zoom;

    if (drag.kind === "move") {
        // Clamp the delta, not each block: clamping per block would stop the
        // left-most one at 0 while the rest kept going, deforming the group.
        let minX = Infinity, minY = Infinity;
        for (const o of drag.origins) {
            minX = Math.min(minX, o.x); minY = Math.min(minY, o.y);
        }
        const adx = Math.max(dx, -minX);
        const ady = Math.max(dy, -minY);
        for (const o of drag.origins) {
            const b = blockById(o.id);
            if (!b) continue;
            b.x = o.x + adx;
            b.y = o.y + ady;
            positionBlock(b);
        }
        renderArrows();
        maybeGrowCanvas();
    } else if (drag.kind === "resize") {
        const b = drag.block;
        b.w = Math.max(60, drag.w0 + dx);
        b.h = Math.max(32, drag.h0 + dy);
        if (drag.aspect && !e.altKey) b.h = Math.max(32, b.w / drag.aspect);
        positionBlock(b);
        maybeGrowCanvas();
    } else if (drag.kind === "marquee") {
        const pt = canvasPoint(e);
        const x1 = Math.min(drag.ox, pt.x), x2 = Math.max(drag.ox, pt.x);
        const y1 = Math.min(drag.oy, pt.y), y2 = Math.max(drag.oy, pt.y);
        const m = document.getElementById("notes-marquee");
        m.hidden = false;
        m.style.left = x1 + "px"; m.style.top = y1 + "px";
        m.style.width = (x2 - x1) + "px"; m.style.height = (y2 - y1) + "px";

        const hit = new Set(drag.additive ? drag.base : []);
        for (const b of note.blocks) {
            if (b.x < x2 && b.x + b.w > x1 && b.y < y2 && b.y + b.h > y1) hit.add(b.id);
        }
        setSelection(hit);
    } else if (drag.kind === "arrow") {
        const pt = canvasPoint(e);
        const b = drag.block;
        b.x = Math.min(drag.ox, pt.x);
        b.y = Math.min(drag.oy, pt.y);
        b.w = Math.max(1, Math.abs(pt.x - drag.ox));
        b.h = Math.max(1, Math.abs(pt.y - drag.oy));
        // Direction is carried by flips so every block stays a plain rect.
        b.style.flipX = pt.x < drag.ox;
        b.style.flipY = pt.y < drag.oy;
        renderArrows();
        maybeGrowCanvas();
    }
}

function onCanvasPointerUp(e) {
    if (!drag) return;
    const d = drag;
    drag = null;
    canvasEl().classList.remove("is-dragging");
    try { canvasEl().releasePointerCapture(d.pointerId); } catch (err) {}

    if (d.kind === "pan") {
        const scroll = scrollEl();
        if (scroll) scroll.classList.remove("is-panning");
        return;    // panning is pure navigation — nothing to save
    }

    if (d.kind === "marquee") {
        const m = document.getElementById("notes-marquee");
        if (m) m.hidden = true;
        renderSelectionUi();
        return;
    }
    if (d.kind === "arrow") {
        if (!d.moved || (d.block.w < 8 && d.block.h < 8)) {
            note.blocks = note.blocks.filter(b => b.id !== d.block.id);
            renderCanvas();
        } else {
            setSelection([d.block.id]);
            scheduleSave();
        }
        if (!stickyTool) setTool("select");
        return;
    }
    if (d.moved) {
        // Only re-render on resize (content may reflow); a move already wrote
        // left/top directly, so re-rendering would be wasted work.
        if (d.kind === "resize") renderCanvas();
        else renderSelectionUi();
        scheduleSave();
    } else {
        renderSelectionUi();
    }
}

/**
 * Topmost block containing a canvas point, or null.
 *
 * Hit-tests the model rather than the DOM: selecting on pointerdown
 * re-renders .notes-blocks, so by the time a dblclick arrives its target can
 * be a detached node whose closest(".nb-block") is null. Coordinates always
 * survive that; element identity does not.
 */
function blockAtPoint(pt) {
    let hit = null;
    for (const b of note.blocks) {
        // An arrow's rect is mostly empty space around a thin diagonal, so a
        // bounding-box test would grab clicks meant for whatever is behind
        // it. Measure distance to the line itself.
        const inside = b.type === "arrow"
            ? nearArrow(b, pt, 10 / zoom)   // constant grab width on screen
            : (pt.x >= b.x && pt.x <= b.x + b.w && pt.y >= b.y && pt.y <= b.y + b.h);
        if (inside && (!hit || (b.z || 0) >= (hit.z || 0))) hit = b;
    }
    return hit;
}

/** Endpoints of an arrow, recovered from its rect plus the flip flags. */
function arrowPoints(a) {
    const st = a.style || {};
    return {
        x1: st.flipX ? a.x + a.w : a.x,
        y1: st.flipY ? a.y + a.h : a.y,
        x2: st.flipX ? a.x : a.x + a.w,
        y2: st.flipY ? a.y : a.y + a.h,
    };
}

/** Perpendicular distance from a point to the arrow's segment, within tol. */
function nearArrow(a, pt, tol) {
    const { x1, y1, x2, y2 } = arrowPoints(a);
    const dx = x2 - x1, dy = y2 - y1;
    const len2 = dx * dx + dy * dy;
    if (!len2) return Math.hypot(pt.x - x1, pt.y - y1) <= tol;
    // Project onto the segment and clamp, so the ends don't extend forever.
    let t = ((pt.x - x1) * dx + (pt.y - y1) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(pt.x - (x1 + t * dx), pt.y - (y1 + t * dy)) <= tol;
}

function onCanvasDblClick(e) {
    if (!note) return;
    // dblclick is its own listener and does not pass through the pointerdown
    // pan branch, so the hand tool has to be honoured explicitly or a stray
    // double-click would still open an editor mid-pan.
    if (activeTool === "hand") return;
    // Double-click inside the editor selects a word — leave it to the
    // textarea rather than re-entering edit mode and collapsing the caret.
    if (editing && e.target.closest(".nb-edit")) return;
    const pt = canvasPoint(e);
    const hit = blockAtPoint(pt);
    if (hit) {
        if (hit.type !== "image") enterEdit(hit);
        return;
    }
    // Empty space always makes a text block, regardless of active tool —
    // the fast path that works without thinking about the toolbar.
    // Same zoom-relative sizing as the toolbar tools, so double-clicking at
    // 200% gives a box that fits the region you zoomed in on.
    const b = addBlock(toolDefaults("text", pt));
    setSelection([b.id]);
    enterEdit(b);
    scheduleSave();
}

// =====================================================================
// Paste and drag-drop onto the canvas
// =====================================================================

async function onCanvasPaste(e) {
    if (!notesOpen || !note) return;
    const target = e.target;
    if (target && (target.tagName === "TEXTAREA" || target.tagName === "INPUT")) return;

    const items = Array.from((e.clipboardData && e.clipboardData.items) || []);
    const pt = lastCanvasPoint || { x: 40, y: (scrollEl().scrollTop || 0) + 40 };

    const imageItem = items.find(it => it.kind === "file" && NOTE_IMAGE_TYPES.includes(it.type));
    if (imageItem) {
        e.preventDefault();
        const file = imageItem.getAsFile();
        if (file) await insertImageBlock(file, pt);
        return;
    }

    const text = e.clipboardData ? e.clipboardData.getData("text/plain") : "";
    if (text && text.trim()) {
        e.preventDefault();
        // Zoom-relative, like every other creation path. estimateTextHeight
        // works in model units too, so it gets the scaled width.
        const width = 340 / zoom;
        const style = zoom === 1 ? {} : { fontSize: round1(14 / zoom) };
        const b = addBlock({
            type: "text", x: pt.x, y: pt.y, w: width,
            h: estimateTextHeight(text, width, 1 / zoom),
            content: text.trim(), style,
        });
        setSelection([b.id]);
        scheduleSave();
    }
}

function initNotesDrop() {
    const zone = scrollEl();
    if (!zone) return;
    // Depth counter, same as initAttachments in app.js: dragenter/dragleave
    // fire for every child crossed, so a boolean flickers.
    let depth = 0;
    const hasFiles = (e) => Array.from((e.dataTransfer && e.dataTransfer.types) || [])
        .includes("Files");

    zone.addEventListener("dragenter", (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        depth += 1;
        zone.classList.add("drop-active");
    });
    zone.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
    zone.addEventListener("dragleave", () => {
        depth = Math.max(0, depth - 1);
        if (!depth) zone.classList.remove("drop-active");
    });
    zone.addEventListener("drop", async (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        depth = 0;
        zone.classList.remove("drop-active");
        if (!note) { notesToast("Open or create a note first", true); return; }

        const pt = canvasPoint(e);
        for (const file of Array.from(e.dataTransfer.files)) {
            if (file.type === "application/pdf" || /\.pdf$/i.test(file.name)) {
                await importPdfFile(file);
            } else if (/\.sdocx?$/i.test(file.name)) {
                await importPdfFile(file);      // shows the Samsung guidance
            } else if (NOTE_IMAGE_TYPES.includes(file.type)) {
                await insertImageBlock(file, pt);
            } else {
                notesToast(`Can't add ${file.name}`, true);
            }
        }
    });
}

// =====================================================================
// Keyboard
// =====================================================================

function onNotesKeydown(e) {
    if (!notesOpen || !note || editing) return;
    const t = e.target;
    if (t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.isContentEditable)) return;
    if (!notesPaneEl().contains(document.activeElement)
        && !notesPaneEl().matches(":hover")) return;

    const meta = e.metaKey || e.ctrlKey;

    if (meta && e.key.toLowerCase() === "g") {
        e.preventDefault();
        if (e.shiftKey) ungroupSelected(); else groupSelected();
        return;
    }
    if (meta && (e.key === "=" || e.key === "+")) { e.preventDefault(); zoomBy(1); return; }
    if (meta && e.key === "-") { e.preventDefault(); zoomBy(-1); return; }
    if (meta && e.key === "0") { e.preventDefault(); zoomReset(); return; }
    if (meta && e.key.toLowerCase() === "z") { e.preventDefault(); undoNotes(); return; }
    if (meta && e.key.toLowerCase() === "a") {
        e.preventDefault();
        setSelection(note.blocks.map(b => b.id));
        return;
    }
    if (e.key === "Delete" || e.key === "Backspace") {
        if (selectedIds.size) { e.preventDefault(); removeBlocks(new Set(selectedIds)); }
        return;
    }
    if (e.key === "Escape") { setSelection([]); setTool("select"); return; }
    if (e.key === "Enter" && selectedIds.size === 1) {
        const b = blockById([...selectedIds][0]);
        if (b && b.type !== "image") { e.preventDefault(); enterEdit(b); }
        return;
    }
    // Single-key tool shortcuts, matching the toolbar order.
    const tools = { v: "select", h: "hand", t: "text", s: "sticky", c: "code",
                    r: "rect", o: "ellipse", d: "diamond", a: "arrow" };
    const tool = tools[e.key.toLowerCase()];
    if (tool && !meta) { setTool(tool, e.shiftKey); }
}

// =====================================================================
// Pane divider — rebalance chat against the notebook / notes canvas
// =====================================================================

const PANE_MIN = 320;             // neither side is usable below this
const PANE_SPLIT_DEFAULT = 0.5;

function paneSplitKey() {
    const email = typeof getEmail === "function" ? getEmail() : "";
    return `pymentor:pane-split:${email || "_anon"}`;
}

function openRightPane() {
    const notes = document.getElementById("notes-pane");
    if (notes && notes.style.display !== "none") return notes;
    const demo = document.getElementById("demo-pane");
    if (demo && demo.style.display !== "none") return demo;
    const nb = document.getElementById("notebook-pane");
    if (nb && nb.style.display !== "none") return nb;
    return null;
}

/**
 * Apply the split as flex-basis on the chat pane.
 *
 * The right pane keeps flex:1 and simply takes the remainder, so only one
 * number has to be stored and the layout can't end up over-constrained.
 */
function applyPaneSplit(fraction) {
    const chat = document.getElementById("chat-pane");
    const divider = document.getElementById("pane-divider");
    const right = openRightPane();
    if (!chat || !divider) return;

    if (!right) {
        // Nothing to split against — restore the chat to filling the row.
        divider.style.display = "none";
        chat.style.flex = "";
        return;
    }
    divider.style.display = "block";

    const total = chat.parentElement.clientWidth - divider.offsetWidth;
    if (total <= 0) return;

    // Clamp so neither pane can be dragged into uselessness, and so a stored
    // split from a wider window doesn't strand a pane off-screen.
    const min = Math.min(PANE_MIN, total / 2);
    const px = Math.max(min, Math.min(total - min, total * fraction));
    chat.style.flex = `0 0 ${Math.round(px)}px`;
}

function savePaneSplit(fraction) {
    try { localStorage.setItem(paneSplitKey(), String(fraction)); } catch (e) {}
}

function loadPaneSplit() {
    try {
        const v = parseFloat(localStorage.getItem(paneSplitKey()));
        if (v > 0.1 && v < 0.9) return v;
    } catch (e) {}
    return PANE_SPLIT_DEFAULT;
}

/** Re-apply on open/close/resize so the divider tracks the current layout. */
function syncPaneSplit() {
    applyPaneSplit(loadPaneSplit());
}

function initPaneDivider() {
    const divider = document.getElementById("pane-divider");
    const chat = document.getElementById("chat-pane");
    if (!divider || !chat) return;

    let dragging = null;

    divider.addEventListener("pointerdown", (e) => {
        if (e.button !== 0) return;
        const right = openRightPane();
        if (!right) return;
        e.preventDefault();
        dragging = {
            pointerId: e.pointerId,
            hostLeft: chat.parentElement.getBoundingClientRect().left,
            total: chat.parentElement.clientWidth - divider.offsetWidth,
        };
        divider.setPointerCapture(e.pointerId);
        divider.classList.add("is-dragging");
        document.body.classList.add("is-resizing-panes");
    });

    divider.addEventListener("pointermove", (e) => {
        if (!dragging || !dragging.total) return;
        const px = e.clientX - dragging.hostLeft;
        applyPaneSplit(px / dragging.total);
    });

    const end = (e) => {
        if (!dragging) return;
        try { divider.releasePointerCapture(dragging.pointerId); } catch (err) {}
        divider.classList.remove("is-dragging");
        document.body.classList.remove("is-resizing-panes");
        // Persist as a fraction, not pixels, so the split survives a window
        // resize or a different screen.
        const total = chat.parentElement.clientWidth - divider.offsetWidth;
        if (total > 0) savePaneSplit(chat.getBoundingClientRect().width / total);
        dragging = null;
    };
    divider.addEventListener("pointerup", end);
    divider.addEventListener("pointercancel", end);

    // Double-click resets to an even split — faster than dragging back.
    divider.addEventListener("dblclick", () => {
        savePaneSplit(PANE_SPLIT_DEFAULT);
        applyPaneSplit(PANE_SPLIT_DEFAULT);
    });

    // Re-clamp when the window changes, so a stored split from a wide window
    // can't leave a pane below its minimum.
    window.addEventListener("resize", syncPaneSplit);
    syncPaneSplit();
}

// =====================================================================
// Init
// =====================================================================

async function initNotes() {
    const pane = notesPaneEl();
    if (!pane) return;

    // Every listener below is added, never replaced, so a second init would
    // make one click fire two actions. Cheap to guard, hard to debug without.
    if (pane.dataset.notesInit === "1") return;
    pane.dataset.notesInit = "1";

    // Toolbar
    document.querySelectorAll(".nb-tool[data-tool]").forEach(btn => {
        btn.addEventListener("click", (e) => setTool(btn.dataset.tool, e.shiftKey));
    });

    // Formatting ribbon (delegated — the bar is re-rendered on every
    // selection change).
    const bar = document.getElementById("notes-format");
    if (bar) {
        // Keep the caret: without this the textarea loses focus on mousedown
        // and selectionStart/End collapse before the click handler runs. The
        // opacity <select> is exempt — it needs focus to open its menu.
        bar.addEventListener("mousedown", (e) => {
            if (!e.target.closest("select")) e.preventDefault();
        });
        bar.addEventListener("change", (e) => {
            const op = e.target.closest("[data-opacity]");
            if (op) styleSelected({ opacity: parseFloat(op.value) });
        });
        bar.addEventListener("click", (e) => {
            const hit = (sel) => e.target.closest(sel);

            const fill = hit("[data-color]");
            if (fill) { styleSelected({ bg: fill.dataset.color }); return; }

            const fg = hit("[data-fg]");
            if (fg) { styleSelected({ fg: fg.dataset.fg }); return; }

            const stroke = hit("[data-stroke]");
            if (stroke) { styleSelected({ stroke: stroke.dataset.stroke }); return; }

            // While editing, these act on the caret/selection like a word
            // processor; outside an edit they apply to whole blocks.
            const head = hit("[data-head]");
            if (head) {
                const lvl = parseInt(head.dataset.head, 10);
                if (!headingInEditor(lvl)) headingSelected(lvl);
                return;
            }

            const wrap = hit("[data-wrap]");
            if (wrap) {
                if (!wrapInEditor(wrap.dataset.wrap)) wrapSelected(wrap.dataset.wrap);
                return;
            }

            const list = hit("[data-list]");
            if (list) { listInEditor(list.dataset.list); return; }

            const font = hit("[data-font]");
            if (font) { bumpFontSize(parseInt(font.dataset.font, 10)); return; }

            const op = hit("[data-opacity]");
            if (op) return;   // handled on change, not click

            const headStyle = hit("[data-head-style]");
            if (headStyle) { styleSelected({ head: headStyle.dataset.headStyle }); return; }

            const act = hit("[data-act]");
            if (!act) return;
            switch (act.dataset.act) {
                case "group": groupSelected(); break;
                case "ungroup": ungroupSelected(); break;
                case "front": bringToFront(new Set(selectedIds)); break;
                case "delete": removeBlocks(new Set(selectedIds)); break;
            }
        });
    }

    const canvas = canvasEl();
    if (canvas) {
        canvas.addEventListener("pointerdown", onCanvasPointerDown);
        canvas.addEventListener("pointermove", onCanvasPointerMove);
        canvas.addEventListener("pointerup", onCanvasPointerUp);
        canvas.addEventListener("pointercancel", onCanvasPointerUp);
        canvas.addEventListener("dblclick", onCanvasDblClick);
        canvas.addEventListener("mousemove", (e) => { lastCanvasPoint = canvasPoint(e); });
    }

    // No scroll listener: the ribbon is fixed in the header, so unlike the
    // old floating bar it never needs repositioning.

    // Zoom is button/keyboard only, by choice. Wheel zoom on a trackpad fires
    // a stream of deltas and made the level impossible to land on; scrolling
    // the page is what the wheel is for here.

    document.addEventListener("paste", onCanvasPaste);
    document.addEventListener("keydown", onNotesKeydown);

    const pdfInput = document.getElementById("notes-pdf-input");
    if (pdfInput) {
        pdfInput.addEventListener("change", async () => {
            const f = pdfInput.files && pdfInput.files[0];
            if (f) await importPdfFile(f);
            pdfInput.value = "";     // allow re-picking the same file
        });
    }

    initNotesDrop();
    initSendToNotes();
    initPaneDivider();

    // Space-to-pan. Tracked at the document level so the modifier works
    // wherever the cursor is, but ignored while typing — a space in a text
    // block must stay a space.
    document.addEventListener("keydown", (e) => {
        if (e.code !== "Space" || spaceHeld || editing) return;
        const t = e.target;
        if (t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.isContentEditable)) return;
        if (!notesOpen || !notesPaneEl().matches(":hover")) return;
        spaceHeld = true;
        e.preventDefault();          // stop the page scrolling
        const s = scrollEl();
        if (s) s.classList.add("can-pan");
    });
    document.addEventListener("keyup", (e) => {
        if (e.code !== "Space") return;
        spaceHeld = false;
        const s = scrollEl();
        // Keep the grab cursor if the hand tool is what is arming it.
        if (s && activeTool !== "hand") s.classList.remove("can-pan");
    });
    // A lost keyup (tab switch mid-drag) would otherwise leave pan mode stuck.
    window.addEventListener("blur", () => {
        spaceHeld = false;
        const s = scrollEl();
        if (s) {
            s.classList.remove("is-panning");
            // A lost keyup would otherwise strand pan mode, but the hand tool
            // is a deliberate state that should survive tabbing away.
            if (activeTool !== "hand") s.classList.remove("can-pan");
        }
    });

    // Annotations are stored in absolute canvas coordinates while pages scale
    // to the pane, so a width change has to move both together.
    // Observe the SCROLL CONTAINER, not the canvas. The canvas legitimately
    // resizes on every zoom (applyZoom sets an explicit width), so observing
    // it made zoom indistinguishable from a pane resize — which is how the
    // runaway feedback loop started. The scroll container only changes when
    // the pane really changes.
    const resizeHost = scrollEl();
    if (typeof ResizeObserver !== "undefined" && resizeHost) {
        let t = null;
        new ResizeObserver(() => {
            clearTimeout(t);
            t = setTimeout(() => { if (notesOpen && note) rescaleForWidth(); }, 180);
        }).observe(resizeHost);
    }

    // Flush before the tab goes away — sendBeacon can't do PUT, and this
    // fires reliably before close on modern browsers.
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") flushSave();
    });

    try {
        const res = await fetch(`${API_BASE}/api/notes-capabilities`);
        if (res.ok) pdfImportAvailable = (await res.json()).pdf_import;
    } catch (e) {}
    const pdfBtn = document.getElementById("notes-pdf-btn");
    if (pdfBtn && !pdfImportAvailable) {
        pdfBtn.disabled = true;
        pdfBtn.title = "PDF import needs PyMuPDF installed on the server";
    }

    const ui = loadNotesUi();
    if (ui.open) await toggleNotes(true);
}
