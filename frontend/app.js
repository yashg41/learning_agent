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

function renderMarkdown(text) {
    if (!text) return "";
    if (typeof marked === "undefined") return escapeHtml(text);
    return marked.parse(text, { breaks: true, gfm: true });
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
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
        return;
    }
    currentEmail = email;
    // Persist identity: without this every refresh drops it and blanks every
    // panel, even though the notebook already used localStorage for cells.
    try { localStorage.setItem("pymentor:email", email); } catch (e) {}
    updateLearnerChip(email);
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
        if (data.name) {
            if (sessionId === currentSessionId) setChatTitle(data.name);
            loadSessions(email);
        }
    } catch (e) {
        // Non-critical — the existing name stays.
    }
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
                case "error":
                    flushAssistant();
                    addMessage(turn.content, "error");
                    break;
                case "side_chat_summary":
                    // A finished tangent, folded back in as one card.
                    flushAssistant();
                    renderSideSummaryCard(turn, container);
                    break;
                case "side_chat_start":
                    // Boundary marker — the summary card carries the payload,
                    // so nothing to render here.
                    break;
            }
        }
        // Flush any trailing assistant text
        flushAssistant();

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
    }

    clearAttachments() {
        this.pendingAttachments = [];
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
                                // Auto-name the session with first message
                                if (isFirstMessage) {
                                    // An image-only opening turn has no text
                                    // to name from — don't PATCH a blank name.
                                    const sessionName = message.substring(0, 40)
                                        || (attachments.length ? "Image question" : "");
                                    if (this.isMain) setChatTitle(sessionName);
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
                            scrollChatIfPinned(this.messagesEl);
                            break;

                        case "tool_call":
                            hideThinking(this.messagesEl);
                            // Finalize current assistant markdown before card
                            if (assistantDiv && assistantText) {
                                assistantDiv.classList.remove("streaming");
                                assistantDiv.innerHTML = renderMarkdown(assistantText);
                            }
                            addToolCard(event.tool_name, event.tool_input, event.tool_use_id, this.messagesEl);
                            // Reset assistant div for post-tool text
                            assistantDiv = null;
                            assistantText = "";
                            deltaText = "";
                            // Tool calls can run long — show the indicator
                            // again so the wait isn't silent.
                            showThinking(this.messagesEl);
                            break;

                        case "tool_result":
                            // Optionally update tool card
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
                    // Re-attach: every innerHTML write above destroys child
                    // nodes, so the launcher can only be added once the text
                    // has settled.
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
                    addMessage("Your message was not sent — it's back in the box, press Send to retry.", "error", this.messagesEl);
                }
            } finally {
                this.sendBtnEl.disabled = false;
                this.inputEl.disabled = false;
                this.streaming = false;
                this.inputEl.focus();

                if (this.isMain) {
                    isStreaming = false;
                    setStatus(this.sessionId ? `Session: ${this.sessionId.substring(0, 8)}...` : "Ready", true);
                    // Only the main chat refreshes the session list: it calls
                    // loadSessionHistory, which wipes and rebuilds the main
                    // message list. Doing that from a side-chat would destroy
                    // the main conversation's DOM mid-stream.
                    if (currentEmail) {
                        // Once a conversation has a real subject, replace the
                        // first-message title with one drawn from what was
                        // actually covered — an opener is often just "hi", and
                        // topics drift. Fire-and-forget: a failed retitle just
                        // leaves the original name.
                        if (this.turnCount === RETITLE_AFTER_TURNS && this.sessionId) {
                            retitleSession(currentEmail, this.sessionId);
                        }
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
            attachStripEl: document.getElementById("side-attach-strip"),
            attachBtnEl: document.getElementById("side-attach-btn"),
            attachInputEl: document.getElementById("side-attach-input"),
        });
    }
    return sideChat;
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

    for (const [level, items] of Object.entries(groups)) {
        if (items.length === 0) continue;
        html += `<div class="concept-group ${level}">`;
        html += `<h3>${level} <span class="count">(${items.length})</span></h3>`;
        for (const c of items) {
            const reviewDate = c.last_reviewed ? c.last_reviewed.substring(5, 10) : "";
            html += `
                <div class="concept-item">
                    <div class="concept-dot ${level}"></div>
                    <span class="concept-name">${escapeHtml(c.name || c.concept_id)}</span>
                    <span class="concept-category">${escapeHtml(c.category || "")}</span>
                </div>`;
        }
        html += `</div>`;
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

function notebookKey() {
    const email = getEmail();
    return email ? `pymentor:cells:${email}` : "pymentor:cells:_anon";
}

function saveCells() {
    const cells = [...document.querySelectorAll(".cell")].map(el => ({
        code: el.querySelector(".cell-textarea").value,
        output: el.querySelector(".cell-output").textContent,
        status: el.dataset.status || "",
        isError: el.querySelector(".cell-output").classList.contains("error"),
    }));
    try { localStorage.setItem(notebookKey(), JSON.stringify(cells)); } catch {}
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
        if (c.status) {
            el.dataset.status = c.status;
            const s = el.querySelector(".cell-status");
            s.textContent = c.status;
        }
    }
}

function toggleNotebook() {
    notebookOpen = !notebookOpen;
    document.getElementById("notebook-pane").style.display = notebookOpen ? "flex" : "none";
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
        <textarea class="cell-textarea" spellcheck="false" placeholder="# Python code — runs in your per-user venv"></textarea>
        <pre class="cell-output empty"></pre>
    `;
    container.appendChild(div);
    const ta = div.querySelector(".cell-textarea");
    ta.value = initialCode;
    ta.addEventListener("input", saveCells);
    ta.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            runCell(id);
        }
    });
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

async function runCell(id) {
    const email = getEmail();
    if (!email) {
        alert("Enter your email first — the runner uses a per-user venv.");
        return;
    }
    const el = document.getElementById(id);
    if (!el) return;
    const code = el.querySelector(".cell-textarea").value;
    const out = el.querySelector(".cell-output");
    const status = el.querySelector(".cell-status");

    out.textContent = "";
    out.classList.remove("error");
    out.classList.remove("empty");
    status.className = "cell-status running";
    status.textContent = "Running...";
    el.dataset.status = "Running...";

    try {
        const res = await fetch(`${API_BASE}/api/code/run`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email, code }),
        });
        if (!res.ok) {
            const detail = await res.text();
            out.textContent = `[runner error ${res.status}] ${detail}`;
            out.classList.add("error");
            status.className = "cell-status fail";
            status.textContent = `error (${res.status})`;
            el.dataset.status = status.textContent;
            saveCells();
            return;
        }
        const data = await res.json();
        const stderr = data.stderr || "";
        const stdout = data.stdout || "";
        let combined = stdout;
        if (stderr.trim()) {
            combined = (stdout ? stdout + "\n" : "") + stderr;
        }
        if (!combined) combined = "(no output)";
        out.textContent = combined;
        const failed = data.exit_code !== 0 || data.timed_out;
        if (failed) out.classList.add("error");
        status.className = `cell-status ${failed ? "fail" : "ok"}`;
        status.textContent = failed
            ? (data.timed_out ? "timeout" : `exit ${data.exit_code}`) + ` · ${data.duration_ms}ms`
            : `ok · ${data.duration_ms}ms`;
        el.dataset.status = status.textContent;
        saveCells();
    } catch (err) {
        out.textContent = `Connection error: ${err.message}`;
        out.classList.add("error");
        status.className = "cell-status fail";
        status.textContent = "network error";
        el.dataset.status = status.textContent;
        saveCells();
    }
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
    });

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
