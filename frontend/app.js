// PyMentor Frontend — SSE streaming + knowledge dashboard + multi-session

const API_BASE = "";
let currentEmail = "";
let currentSessionId = null;
let isStreaming = false;

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
        return;
    }
    currentEmail = email;
    await loadSessions(email);
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
        renderSessionList(sessions, activeSession);

        // Auto-select active session and load its history
        if (activeSession && sessions.some(s => s.session_id === activeSession)) {
            currentSessionId = activeSession;
        } else if (sessions.length > 0) {
            currentSessionId = sessions[sessions.length - 1].session_id;
        } else {
            currentSessionId = null;
        }

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
        const isActive = s.session_id === (currentSessionId || activeSession);
        const date = s.last_active
            ? s.last_active.substring(0, 10)
            : s.created_at ? s.created_at.substring(0, 10) : "";
        const name = s.name || "Untitled Session";

        html += `
            <div class="session-item${isActive ? " active" : ""}"
                 onclick="switchSession('${s.session_id}')"
                 data-session-id="${s.session_id}">
                <div class="session-name" title="${escapeHtml(name)}">${escapeHtml(name)}</div>
                <div class="session-date">${date}</div>
            </div>`;
    }

    container.innerHTML = html;
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

    // Update UI
    document.querySelectorAll(".session-item").forEach(el => {
        el.classList.toggle("active", el.dataset.sessionId === sessionId);
    });

    // Clear chat and load history
    clearChat();
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

function clearChat() {
    const container = document.getElementById("chat-messages");
    container.innerHTML = `
        <div class="empty-state">
            <div class="emoji">&#127891;</div>
            Start asking Python questions!
        </div>`;

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
                    const div = addMessage(turn.content, "user");
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

function addMessage(content, type, container) {
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
    constructor({ messagesEl, inputEl, sendBtnEl, sessionId = null, isMain = false, onSessionId = null }) {
        this.messagesEl = messagesEl;
        this.inputEl = inputEl;
        this.sendBtnEl = sendBtnEl;
        this.sessionId = sessionId;
        this.isMain = isMain;
        this.streaming = false;
        // Notified when the SDK reports the real session id (it differs from
        // any placeholder we sent).
        this.onSessionId = onSessionId;
    }

    async send() {
        const email = getEmail();
        if (!email) {
            addMessage("Please enter your email first.", "error", this.messagesEl);
            return;
        }

        const message = this.inputEl.value.trim();
        if (!message) return;
        if (this.streaming) return;   // one turn at a time per surface

        this.inputEl.value = "";
        this.inputEl.style.height = "auto";
        if (this.isMain) currentEmail = email;

        addMessage(message, "user", this.messagesEl);

        this.sendBtnEl.disabled = true;
        this.inputEl.disabled = true;
        this.streaming = true;
        if (this.isMain) {
            isStreaming = true;
            setStatus("Thinking...", false);
        }

        const isFirstMessage = !this.sessionId;

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
                                    const sessionName = message.substring(0, 40);
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

function getMainChat() {
    if (!mainChat) {
        mainChat = new ChatController({
            messagesEl: document.getElementById("chat-messages"),
            inputEl: document.getElementById("message-input"),
            sendBtnEl: document.getElementById("send-btn"),
            isMain: true,
        });
    }
    // Keep in sync with session switching, which mutates the global.
    mainChat.sessionId = currentSessionId;
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
    const grip = document.getElementById("side-chat-resize");
    if (!win || !header) return;

    let mode = null;         // "drag" | "resize"
    let startX = 0, startY = 0, startLeft = 0, startTop = 0, startW = 0, startH = 0;

    function beginDrag(e) {
        // Ignore clicks on the header buttons.
        if (e.target.closest("button")) return;
        const r = win.getBoundingClientRect();
        // Switch from right/bottom anchoring to left/top so dragging is
        // absolute rather than fighting the CSS defaults.
        win.style.left = r.left + "px";
        win.style.top = r.top + "px";
        win.style.right = "auto";
        win.style.bottom = "auto";
        mode = "drag";
        startX = e.clientX; startY = e.clientY;
        startLeft = r.left; startTop = r.top;
        header.setPointerCapture(e.pointerId);
    }

    function beginResize(e) {
        const r = win.getBoundingClientRect();
        mode = "resize";
        startX = e.clientX; startY = e.clientY;
        startW = r.width; startH = r.height;
        grip.setPointerCapture(e.pointerId);
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
        } else {
            win.style.width = Math.max(300, startW + dx) + "px";
            win.style.height = Math.max(240, startH + dy) + "px";
        }
    }

    function endDrag() { mode = null; }

    header.addEventListener("pointerdown", beginDrag);
    header.addEventListener("pointermove", onMove);
    header.addEventListener("pointerup", endDrag);
    if (grip) {
        grip.addEventListener("pointerdown", beginResize);
        grip.addEventListener("pointermove", onMove);
        grip.addEventListener("pointerup", endDrag);
    }
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

    // Individual quizzes (most recent first)
    const sorted = [...quizzes].reverse();
    for (const q of sorted.slice(0, 10)) {
        const pct = q.percentage || 0;
        const cls = pct >= 80 ? "good" : pct >= 50 ? "ok" : "poor";
        const date = q.timestamp ? q.timestamp.substring(0, 16).replace("T", " ") : "";
        html += `
            <div class="quiz-item">
                <span class="quiz-score ${cls}">${q.score}/${q.total} (${pct}%)</span>
                <div class="quiz-date">${escapeHtml(date)}</div>
                <div class="episode-topics" style="margin-top:4px">${(q.topics || []).map(t => `<span class="topic-tag">${escapeHtml(t)}</span>`).join("")}</div>
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
    el.style.color = isError ? "#f7768e" : "#9ece6a";
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

    // Side-chat window: drag/resize handlers and its own auto-resizing input.
    initSideChatWindow();
    const sideInput = document.getElementById("side-chat-input");
    if (sideInput) sideInput.addEventListener("input", () => autoResizeInput(sideInput));

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
