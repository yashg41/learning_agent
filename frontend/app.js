// PyMentor Frontend — SSE streaming + knowledge dashboard + multi-session

const API_BASE = "";
let currentEmail = "";
let currentSessionId = null;
let isStreaming = false;

// =====================================================================
// Lightweight Markdown Renderer
// =====================================================================

function renderMarkdown(text) {
    if (!text) return "";
    let html = escapeHtml(text);

    // Code blocks (```lang\n...\n```)
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
        return `<pre><code class="lang-${lang || "text"}">${code.trim()}</code></pre>`;
    });

    // Inline code (`...`)
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

    // Headers (# ... ## ... ### ...)
    html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^## (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>');

    // Bold (**...**)
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

    // Italic (*...*)
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');

    // Unordered lists (- item)
    html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');

    // Ordered lists (1. item)
    html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');

    // Horizontal rules (---)
    html = html.replace(/^---$/gm, '<hr>');

    // Line breaks — preserve double newlines as paragraph breaks
    html = html.replace(/\n\n/g, '</p><p>');
    html = html.replace(/\n/g, '<br>');
    html = `<p>${html}</p>`;

    // Clean up empty paragraphs
    html = html.replace(/<p>\s*<\/p>/g, '');
    html = html.replace(/<p>\s*(<h[234]>)/g, '$1');
    html = html.replace(/(<\/h[234]>)\s*<\/p>/g, '$1');
    html = html.replace(/<p>\s*(<pre>)/g, '$1');
    html = html.replace(/(<\/pre>)\s*<\/p>/g, '$1');
    html = html.replace(/<p>\s*(<ul>)/g, '$1');
    html = html.replace(/(<\/ul>)\s*<\/p>/g, '$1');
    html = html.replace(/<p>\s*(<hr>)\s*<\/p>/g, '$1');

    return html;
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

        // Auto-select active session
        if (activeSession && sessions.some(s => s.session_id === activeSession)) {
            currentSessionId = activeSession;
        } else if (sessions.length > 0) {
            currentSessionId = sessions[sessions.length - 1].session_id;
        } else {
            currentSessionId = null;
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

    // Clear chat
    clearChat();

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

function addMessage(content, type) {
    const container = document.getElementById("chat-messages");
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
    return div;
}

function addToolCard(toolName, toolInput, toolUseId) {
    const container = document.getElementById("chat-messages");
    const div = document.createElement("div");
    div.className = "tool-card";
    div.id = `tool-${toolUseId}`;

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

async function sendMessage() {
    const email = getEmail();
    if (!email) {
        addMessage("Please enter your email first.", "error");
        return;
    }

    const input = document.getElementById("message-input");
    const message = input.value.trim();
    if (!message) return;

    input.value = "";
    currentEmail = email;

    // Add user message
    addMessage(message, "user");

    // Disable input while streaming
    const sendBtn = document.getElementById("send-btn");
    sendBtn.disabled = true;
    input.disabled = true;
    isStreaming = true;
    setStatus("Thinking...", false);

    // Track if this is the first message (for auto-naming)
    const isFirstMessage = !currentSessionId;

    let assistantDiv = null;
    let assistantText = "";

    try {
        const requestBody = { message, email };
        if (currentSessionId) {
            requestBody.session_id = currentSessionId;
        }

        const res = await fetch(`${API_BASE}/api/chat/stream`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(requestBody),
        });

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

                addEventLog(event);

                switch (event.type) {
                    case "session_init":
                        // Capture new session ID
                        if (event.session_id) {
                            currentSessionId = event.session_id;
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
                        setStatus(`Session: ${event.session_id?.substring(0, 8)}...`, true);
                        break;

                    case "assistant_message":
                        if (!assistantDiv) {
                            assistantDiv = addMessage("", "assistant");
                        }
                        assistantText += event.content || "";
                        // Use innerHTML with markdown rendering for live updates
                        assistantDiv.innerHTML = renderMarkdown(assistantText);
                        document.getElementById("chat-messages").scrollTop =
                            document.getElementById("chat-messages").scrollHeight;
                        break;

                    case "tool_call":
                        // Finalize current assistant message markdown before tool card
                        if (assistantDiv && assistantText) {
                            assistantDiv.innerHTML = renderMarkdown(assistantText);
                        }
                        addToolCard(event.tool_name, event.tool_input, event.tool_use_id);
                        // Reset assistant div for post-tool text
                        assistantDiv = null;
                        assistantText = "";
                        break;

                    case "tool_result":
                        // Optionally update tool card
                        break;

                    case "result":
                        // Skip duplicate result content — already captured via assistant_message
                        break;

                    case "error":
                        addMessage(event.content, "error");
                        break;

                    case "done":
                        break;
                }
            }
        }
    } catch (err) {
        addMessage(`Connection error: ${err.message}`, "error");
    }

    // Final markdown render pass
    if (assistantDiv && assistantText) {
        assistantDiv.innerHTML = renderMarkdown(assistantText);
    }

    // Re-enable input
    sendBtn.disabled = false;
    input.disabled = false;
    isStreaming = false;
    input.focus();
    setStatus("Ready", true);

    // Refresh sessions list and knowledge panels
    if (currentEmail) {
        loadSessions(currentEmail);
        refreshKnowledge(currentEmail);
        refreshEpisodes(currentEmail);
        refreshQuizzes(currentEmail);
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
// Init
// =====================================================================

document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("message-input").focus();
});
