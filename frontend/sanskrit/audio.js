// =====================================================================
// Speak the Sanskrit.
//
// Click any Devanagari phrase in the chat to hear it; shift-click (or
// long-press on touch) for a slow reading. Verse blocks get a persistent
// pair of buttons, because a verse is what gets replayed while learning to
// chant.
//
// WHY DELEGATION, NOT BUTTONS IN THE DOM
// app.js already wraps every Devanagari run in <span lang="sa"> — see
// tagDevanagariIn(). It would be natural to append a <button> to each span
// there, and it would be wrong three times over:
//
//   1. enrichMessage() is called from five places and every one of them
//      does `innerHTML = ...` first, destroying child nodes. Buttons would
//      vanish on the next render.
//   2. tagDevanagariIn sets `span.textContent = match`, which wipes any
//      child element on a re-tag.
//   3. A button inside the span inherits --font-deva, the forced
//      `letter-spacing: normal` and `font-synthesis: none`.
//
// One delegated listener on the chat container has none of those problems,
// costs one listener instead of hundreds of nodes, and needs no change to
// the tagger at all. The affordance itself is pure CSS (a hover-revealed
// ::after glyph), so nothing is added to the DOM.
//
// WHAT THIS IS NOT
// The voice is Hindi (hi-IN), because no browser ships a Sanskrit one.
// Hindi TTS deletes the inherent schwa — रामः can come out as "rām",
// which in Sanskrit is not an accent slip but a deleted nominative ending.
// Measured on this machine the macOS hi-IN voice keeps the visarga and the
// medial vowels, so it is useful; but it is an aid, not an authority, and
// the UI says so once rather than pretending otherwise.
// =====================================================================

const TTS_PREFS_KEY = "sanskrit:tts";

// Slow enough to hear where syllables and sandhi joins fall, without the
// pitch artefacts that appear at the very bottom of the range.
const RATE_NORMAL = 0.9;
const RATE_SLOW = 0.5;

// Below this, a "long press" is just a slow click.
const LONG_PRESS_MS = 450;

let ttsVoice = null;
let ttsReady = false;
let ttsUnavailable = false;
let lastSpokenEl = null;

function ttsSupported() {
    return typeof window !== "undefined"
        && "speechSynthesis" in window
        && typeof window.SpeechSynthesisUtterance === "function";
}

function loadTtsPrefs() {
    try {
        return JSON.parse(localStorage.getItem(TTS_PREFS_KEY) || "{}");
    } catch {
        return {};
    }
}

function saveTtsPrefs(patch) {
    try {
        const next = { ...loadTtsPrefs(), ...patch };
        localStorage.setItem(TTS_PREFS_KEY, JSON.stringify(next));
    } catch {
        /* private mode — the feature still works, it just won't remember */
    }
}

/**
 * Pick the best available voice for Devanagari.
 *
 * Preference order: an explicit saved choice, then any hi-IN voice, then
 * any other Indic voice (Kannada and Tamil voices do NOT delete schwa, so
 * they mangle Sanskrit less than Hindi does even though the language is
 * wrong), then nothing — in which case the feature disables itself rather
 * than reading Devanagari with an English voice, which produces noise.
 */
function pickVoice() {
    const voices = window.speechSynthesis.getVoices();
    if (!voices.length) return null;

    const saved = loadTtsPrefs().voiceURI;
    if (saved) {
        const match = voices.find(v => v.voiceURI === saved);
        if (match) return match;
    }

    const hindi = voices.filter(v => /^hi([-_]|$)/i.test(v.lang));
    if (hindi.length) {
        // A local voice does not truncate long text and works offline.
        return hindi.find(v => v.localService) || hindi[0];
    }

    // Dravidian fallback: wrong language, better phonetics than English.
    const indic = voices.filter(v => /^(kn|ta|te|ml|mr|bn|gu|pa)([-_]|$)/i.test(v.lang));
    if (indic.length) {
        return indic.find(v => v.localService) || indic[0];
    }
    return null;
}

/**
 * Resolve the voice list.
 *
 * getVoices() is asynchronous in Chrome — the first call routinely returns
 * an empty array and the list only arrives with `voiceschanged`. Safari
 * populates synchronously and may never fire the event. So: try now, listen
 * for the event, and give up after a timeout so a browser that does neither
 * cannot leave the UI in a permanent "loading" state.
 */
function initTts() {
    if (!ttsSupported()) {
        ttsUnavailable = true;
        document.body.classList.add("tts-unavailable");
        return;
    }

    const settle = () => {
        if (ttsReady) return;
        ttsVoice = pickVoice();
        if (!ttsVoice) return;          // keep waiting; the timeout ends it
        ttsReady = true;
        document.body.classList.add("tts-ready");
    };

    settle();
    window.speechSynthesis.addEventListener("voiceschanged", settle);

    setTimeout(() => {
        if (ttsReady) return;
        settle();
        if (!ttsReady) {
            // No usable voice on this machine. Hide the affordance rather
            // than offering a button that silently does nothing.
            ttsUnavailable = true;
            document.body.classList.add("tts-unavailable");
        }
    }, 2500);
}

/**
 * Strip what should not be pronounced.
 *
 * DEVA_RUN_RE (app.js) deliberately swallows trailing whitespace, daṇḍa and
 * punctuation so the span covers the visible phrase. None of that should
 * reach the synthesizer: a daṇḍa is a full stop, and some voices read
 * stray punctuation aloud by name.
 */
function speakableText(raw) {
    return (raw || "")
        .replace(/[।॥]/g, " ")
        .replace(/\s+/g, " ")
        .trim();
}

/**
 * Speak one phrase. `slow` halves the rate for hearing syllable boundaries.
 */
function speakSanskrit(text, { slow = false, sourceEl = null } = {}) {
    if (!ttsReady || ttsUnavailable) return;
    const utteranceText = speakableText(text);
    if (!utteranceText) return;

    // Without cancel() a second click queues behind the first instead of
    // replacing it, so an impatient double-click reads the phrase twice.
    window.speechSynthesis.cancel();

    const u = new SpeechSynthesisUtterance(utteranceText);
    u.voice = ttsVoice;
    // Set lang as well as voice: some engines consult it for text
    // normalisation independently of which voice was chosen.
    u.lang = ttsVoice.lang || "hi-IN";
    u.rate = slow ? RATE_SLOW : RATE_NORMAL;
    u.pitch = 1;

    if (sourceEl) {
        clearSpeakingMark();
        lastSpokenEl = sourceEl;
        sourceEl.classList.add("is-speaking");
        const done = () => {
            sourceEl.classList.remove("is-speaking");
            if (lastSpokenEl === sourceEl) lastSpokenEl = null;
        };
        u.onend = done;
        u.onerror = done;
    }

    window.speechSynthesis.speak(u);
    noteFirstPlayback();
}

function clearSpeakingMark() {
    if (lastSpokenEl) lastSpokenEl.classList.remove("is-speaking");
    document.querySelectorAll(".is-speaking")
        .forEach(el => el.classList.remove("is-speaking"));
}

/**
 * Say once, quietly, that this is a Hindi voice.
 *
 * The learner is trying to acquire pronunciation, so letting them assume
 * this is authoritative Sanskrit would be the one genuinely harmful thing
 * this feature could do. Shown after the first playback (not before — a
 * warning about a sound you have not heard yet is noise), and dismissible
 * for good.
 */
function noteFirstPlayback() {
    if (loadTtsPrefs().noticeSeen) return;
    if (document.querySelector(".tts-notice")) return;

    const el = document.createElement("div");
    el.className = "tts-notice";
    el.innerHTML = `
        <span>This is a Hindi voice — no browser ships a Sanskrit one.
        Good for rhythm and word shape; not authoritative on vowel length
        or the inherent <em>a</em>.</span>
        <button type="button" class="tts-notice-close" title="Don't show this again">Got it</button>
    `;
    el.querySelector("button").onclick = () => {
        saveTtsPrefs({ noticeSeen: true });
        el.remove();
    };
    document.body.appendChild(el);
    setTimeout(() => el.classList.add("is-in"), 10);
}

// --- Delegated click handling ----------------------------------------
//
// Bound to document once. Every Devanagari span in every message — past,
// present, streamed, replayed from history, or inside a side chat — is
// covered without any of them knowing this file exists.

let pressTimer = null;
let pressFired = false;

function sanskritTargetFrom(event) {
    const el = event.target instanceof Element ? event.target : null;
    if (!el) return null;
    // Never hijack a real control that happens to contain Devanagari.
    if (el.closest("button, a, input, textarea, select")) return null;
    return el.closest('[lang="sa"], .verse');
}

function handleSanskritClick(event) {
    if (pressFired) {          // the long-press already spoke it
        pressFired = false;
        return;
    }
    if (!ttsReady) return;
    const target = sanskritTargetFrom(event);
    if (!target) return;
    speakSanskrit(target.textContent, {
        // Shift for slow on desktop; the long-press path covers touch.
        slow: event.shiftKey,
        sourceEl: target,
    });
}

function handlePressStart(event) {
    if (!ttsReady) return;
    const target = sanskritTargetFrom(event);
    if (!target) return;
    pressFired = false;
    pressTimer = setTimeout(() => {
        pressFired = true;
        speakSanskrit(target.textContent, { slow: true, sourceEl: target });
    }, LONG_PRESS_MS);
}

function cancelPress() {
    if (pressTimer) {
        clearTimeout(pressTimer);
        pressTimer = null;
    }
}

// --- Verse controls ---------------------------------------------------

// A blockquote whose content is essentially all Devanagari is a verse,
// whatever markup the model reached for. The prompt asks for
// <div class="verse">, but a model will sometimes use "> " instead, and a
// verse that silently loses its play buttons because of markdown choice is
// a bad failure. Promoting it here means the feature does not depend on the
// model remembering an instruction.
const VERSE_MIN_DEVA_RATIO = 0.5;

function promoteVerseBlocks(root) {
    const scope = root || document;
    scope.querySelectorAll("blockquote").forEach(bq => {
        if (bq.classList.contains("verse")) return;
        const text = (bq.textContent || "").trim();
        if (!text) return;
        const deva = (text.match(/[ऀ-ॿ]/g) || []).length;
        const letters = (text.match(/\S/g) || []).length;
        if (letters && deva / letters >= VERSE_MIN_DEVA_RATIO) {
            bq.classList.add("verse");
        }
    });
}

/**
 * Give every .verse block a persistent play/slow pair.
 *
 * Inline spans rely on hover, which is fine for a word in passing but not
 * for the thing you replay twenty times while learning to chant. Idempotent
 * so it can be called after any render.
 */
function attachVerseControls(root) {
    if (!ttsReady || ttsUnavailable) return;
    const scope = root || document;
    promoteVerseBlocks(scope);
    scope.querySelectorAll(".verse").forEach(verse => {
        if (verse.querySelector(".verse-audio")) return;

        const bar = document.createElement("div");
        bar.className = "verse-audio";
        bar.contentEditable = "false";

        const mk = (label, title, slow) => {
            const b = document.createElement("button");
            b.type = "button";
            b.className = "verse-audio-btn";
            b.textContent = label;
            b.title = title;
            b.onclick = (e) => {
                e.stopPropagation();   // the delegated handler would fire too
                // Read the verse text only — not the buttons' own labels.
                const clone = verse.cloneNode(true);
                clone.querySelectorAll(".verse-audio").forEach(n => n.remove());
                speakSanskrit(clone.textContent, { slow, sourceEl: verse });
            };
            return b;
        };

        bar.appendChild(mk("▶", "Play", false));
        bar.appendChild(mk("▶▶", "Play slowly — hear each syllable", true));
        verse.appendChild(bar);
    });
}

// Stop talking when the learner navigates away or hits Escape — a long
// verse reading over the top of a new question is worse than no audio.
function stopSpeaking() {
    if (!ttsSupported()) return;
    window.speechSynthesis.cancel();
    clearSpeakingMark();
}

document.addEventListener("DOMContentLoaded", () => {
    initTts();

    document.addEventListener("click", handleSanskritClick);
    document.addEventListener("pointerdown", handlePressStart);
    document.addEventListener("pointerup", cancelPress);
    document.addEventListener("pointercancel", cancelPress);
    document.addEventListener("pointermove", cancelPress);

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") stopSpeaking();
    });

    // Chrome keeps speaking after the tab is hidden, which is disconcerting
    // when it is a language you are trying to learn.
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) stopSpeaking();
    });

    window.addEventListener("beforeunload", stopSpeaking);
});

// Exposed for app.js to call after a message renders.
window.attachVerseControls = attachVerseControls;
