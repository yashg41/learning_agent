/* Dropzone client. Shared by the laptop (index.html) and tablet (t) pages. */

const IS_TABLET = document.body.dataset.role === "tablet";

// The token arrives once as ?t=..., is exchanged for an HttpOnly cookie by the
// page response, then stripped from the address bar so it stops leaking through
// history, screenshots and Referer headers.
(function stripToken() {
  const url = new URL(location.href);
  if (url.searchParams.has("t")) {
    url.searchParams.delete("t");
    history.replaceState({}, "", url.pathname + url.search + url.hash);
  }
})();

const state = {
  seq: 0,
  items: new Map(),
  live: false,
  lastEvent: 0,
};

const els = {
  items: document.getElementById("items"),
  status: document.getElementById("status"),
  dot: document.getElementById("dot"),
  toast: document.getElementById("toast"),
};

// --- utilities ---

function toast(message, isError) {
  els.toast.textContent = message;
  els.toast.className = "toast show" + (isError ? " err" : "");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => (els.toast.className = "toast"), 2600);
}

function human(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function ago(ts) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 10) return "just now";
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  return Math.floor(s / 3600) + "h ago";
}

async function api(path, options) {
  const res = await fetch(path, { credentials: "same-origin", ...options });
  if (res.status === 401) {
    setStatus("session expired — rescan the QR code", false);
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res;
}

function setStatus(text, live) {
  els.status.textContent = text;
  if (live === true) els.dot.className = "dot live";
  else if (live === false) els.dot.className = "dot";
  else els.dot.className = "dot poll";
}

// --- image normalization ---

// Safari accepts ONLY image/png for clipboard writes, so anything that is not
// already a PNG is re-encoded here. Done in the browser because the server has
// no imaging library and shouldn't grow one for this.
function toPng(blob) {
  if (blob.type === "image/png") return Promise.resolve(blob);
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      canvas.getContext("2d").drawImage(img, 0, 0);
      URL.revokeObjectURL(url);
      canvas.toBlob(
        (png) => (png ? resolve(png) : reject(new Error("PNG encode failed"))),
        "image/png"
      );
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("could not decode image"));
    };
    img.src = url;
  });
}

// --- sending ---

async function sendBlob(blob, filename) {
  const prepared = blob.type.startsWith("image/") ? await toPng(blob) : blob;
  const form = new FormData();
  const name = filename || (prepared.type === "image/png" ? "pasted.png" : "pasted");
  form.append("file", prepared, name);
  form.append("filename", name);
  await api("/api/file", { method: "POST", body: form });
  toast("Sent " + human(prepared.size));
}

async function sendText(text) {
  await api("/api/text", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  toast("Text sent");
}

async function handlePaste(event) {
  const dt = event.clipboardData;
  if (!dt) return;

  // Two distinct sources, and both matter: `items` carries raw bitmaps from a
  // screenshot, `files` carries anything copied in Finder. Reading only one
  // silently misses the other case.
  const blobs = [];
  for (const item of dt.items || []) {
    if (item.kind === "file") {
      const file = item.getAsFile();
      if (file) blobs.push(file);
    }
  }
  for (const file of dt.files || []) {
    if (!blobs.some((b) => b.size === file.size && b.type === file.type)) {
      blobs.push(file);
    }
  }

  if (blobs.length) {
    event.preventDefault();
    for (const blob of blobs) {
      try {
        await sendBlob(blob, blob.name);
      } catch (err) {
        toast(err.message, true);
      }
    }
    return;
  }

  const text = dt.getData("text/plain");
  if (text && text.trim()) {
    event.preventDefault();
    try {
      await sendText(text);
    } catch (err) {
      toast(err.message, true);
    }
  }
}

// --- receiving ---

function render() {
  const items = [...state.items.values()].sort((a, b) => b.seq - a.seq);

  if (!items.length) {
    els.items.innerHTML = IS_TABLET
      ? '<div class="empty">Nothing yet. Paste on your laptop, or send something up from here.</div>'
      : '<div class="empty">Nothing yet. Paste above, or send something from your tablet.</div>';
    return;
  }

  els.items.innerHTML = "";
  for (const item of items) {
    els.items.appendChild(renderItem(item));
  }
}

function renderItem(item) {
  const node = document.createElement("div");
  node.className = "item";

  const meta = document.createElement("div");
  meta.className = "meta";
  // Origin matters now that either device can send.
  const origin = item.source && item.source !== "device" ? ` · from ${item.source}` : "";
  meta.innerHTML =
    `<span>${item.kind === "text" ? "Text" : "Image"}</span>` +
    `<span>·</span><span>${human(item.size)}</span>` +
    `<span>·</span><span>${ago(item.createdAt)}${origin}</span>` +
    `<span class="spacer"></span>`;
  node.appendChild(meta);

  if (item.kind === "text") {
    // A real textarea, not a <pre>: over plain http there is no clipboard API
    // at all, so the only dependable way to lift text off the page is to let
    // the OS do it — tap Select all, then use the native Copy menu.
    const area = document.createElement("textarea");
    area.className = "textitem";
    area.readOnly = true;
    area.value = item.text + (item.truncated ? "\n…" : "");
    area.rows = Math.min(12, area.value.split("\n").length + 1);
    node.appendChild(area);
    node._textarea = area;
  } else {
    const img = document.createElement("img");
    img.className = "shot";
    img.src = item.url;
    img.alt = item.filename || "pasted image";
    img.loading = "lazy";
    node.appendChild(img);
  }

  node.appendChild(buildActions(item, node._textarea));
  return node;
}

// Select the item's visible text so the OS's own Copy menu can take it.
// This is the only path that cannot fail on a plain-http origin: no
// clipboard API, no execCommand, no permissions — just a selection the user
// then copies with the native menu.
function selectFor(textarea) {
  if (!textarea) return;
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);

  // Try the deprecated path opportunistically — when it works the user is
  // saved a step, and when it doesn't the text is still selected.
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (_) {}

  toast(copied ? "Copied — paste into your notes" : "Selected — tap Copy on the menu");
}

// Copy an image without the clipboard API, for plain-http origins.
// An <img> selected inside a contenteditable host is copied as a real bitmap
// by execCommand — the same thing the browser's own "Copy Image" menu does.
function copyImageLegacy(item) {
  const host = document.createElement("div");
  host.contentEditable = "true";
  // Must be on-screen to be selectable, but effectively invisible.
  host.style.cssText =
    "position:fixed;top:0;left:0;width:1px;height:1px;overflow:hidden;opacity:0;";
  const img = document.createElement("img");

  img.onload = () => {
    try {
      const range = document.createRange();
      range.selectNode(img);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);

      const ok = document.execCommand("copy");
      sel.removeAllRanges();
      toast(
        ok
          ? "Copied — paste into your notes"
          : "Could not copy — use Save, or long-press the image",
        !ok
      );
    } catch (_) {
      toast("Could not copy — use Save, or long-press the image", true);
    } finally {
      document.body.removeChild(host);
    }
  };

  img.onerror = () => {
    document.body.removeChild(host);
    toast("Could not load image — try Save", true);
  };

  host.appendChild(img);
  document.body.appendChild(host);

  // Embed the bitmap as a data URI rather than pointing at the item URL.
  // A remote src copies as a *link*, which is what makes some notes apps
  // paste a URL instead of the picture.
  fetch(item.url, { credentials: "same-origin" })
    .then((r) => r.blob())
    .then(
      (blob) =>
        new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = reject;
          reader.readAsDataURL(blob);
        })
    )
    .then((dataUri) => {
      img.src = dataUri;
    })
    .catch(() => {
      img.src = item.url; // last resort: at least copy something
    });
}

function buildActions(item, textarea) {
  const actions = document.createElement("div");
  actions.className = "actions";

  const canWrite = window.isSecureContext && navigator.clipboard;

  if (item.kind === "text") {
    const copy = document.createElement("button");
    copy.className = "primary";
    // Label the button for what it can actually guarantee. Over plain http
    // there is no clipboard API, so it selects the text and the OS Copy menu
    // does the rest — promising "Copy" there would be a lie.
    copy.textContent = canWrite ? "Copy text" : "Select all";
    copy.addEventListener("click", () => {
      if (canWrite && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(item.text).then(
          () => toast("Copied — paste into your notes"),
          () => selectFor(textarea)
        );
      } else {
        selectFor(textarea);
      }
    });
    actions.appendChild(copy);
  } else {
    const copy = document.createElement("button");
    copy.className = "primary";
    copy.textContent = "Copy image";

    if (canWrite && navigator.clipboard.write && window.ClipboardItem) {
      copy.addEventListener("click", () => {
        // The fetch is deliberately NOT awaited. ClipboardItem is constructed
        // synchronously with a pending promise so iOS Safari keeps the user
        // gesture alive; awaiting the blob first expires the gesture and the
        // write is rejected with NotAllowedError. Do not "clean this up" into
        // async/await — that is exactly the refactor that breaks it.
        const png = fetch(item.url, { credentials: "same-origin" }).then((r) => {
          if (!r.ok) throw new Error("fetch failed");
          return r.blob();
        });

        navigator.clipboard
          .write([new ClipboardItem({ "image/png": png })])
          .then(
            // Safari can resolve this even when nothing reached the clipboard,
            // so the wording stays hedged and the manual fallback stays visible.
            () => toast("Copied — paste into your notes"),
            () => copyImageLegacy(item)
          );
      });
    } else {
      // No clipboard API on this origin (plain http). Select the image inside
      // a contenteditable host so the OS Copy menu can take the bitmap —
      // no long-press needed.
      copy.addEventListener("click", () => copyImageLegacy(item));
    }
    actions.appendChild(copy);
  }

  const open = document.createElement("a");
  open.href = item.url || "#";
  if (item.kind !== "text") {
    open.download = item.filename || "dropzone.png";
    const btn = document.createElement("button");
    btn.className = "ghost";
    btn.textContent = "Save";
    btn.addEventListener("click", () => open.click());
    actions.appendChild(btn);
  }

  const del = document.createElement("button");
  del.className = "ghost danger";
  del.textContent = "Remove";
  del.addEventListener("click", async () => {
    try {
      await api("/api/item/" + item.id, { method: "DELETE" });
      state.items.delete(item.id);
      render();
    } catch (err) {
      toast(err.message, true);
    }
  });
  actions.appendChild(del);

  if (item.kind !== "text") {
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = IS_TABLET
      ? "If Copy image doesn't paste, long-press the image and choose Copy, or use Save."
      : "You can also right-click the image and choose Copy Image.";
    actions.appendChild(hint);
  }

  return actions;
}

// --- sync: SSE with a polling fallback ---

async function pull() {
  try {
    const res = await api("/api/items?since=" + state.seq);
    const data = await res.json();
    for (const item of data.items) {
      state.items.set(item.id, item);
      state.seq = Math.max(state.seq, item.seq);
    }
    if (data.items.length) render();
    return true;
  } catch (_) {
    return false;
  }
}

async function refreshAll() {
  state.seq = 0;
  state.items.clear();
  await pull();
  render();
}

function connect() {
  const source = new EventSource("/api/events", { withCredentials: true });

  source.onopen = () => {
    state.live = true;
    state.lastEvent = Date.now();
    setStatus(IS_TABLET ? "connected" : "connected — ready for a paste", true);
  };

  source.onmessage = (event) => {
    state.lastEvent = Date.now();
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (payload.type === "clear") {
      state.items.clear();
      state.seq = payload.seq || state.seq;
      render();
    } else if (payload.type === "delete") {
      state.items.delete(payload.id);
      render();
    } else if (payload.type === "new") {
      pull();
    }
  };

  source.onerror = () => {
    state.live = false;
    setStatus("reconnecting…", null);
  };

  // Cloudflare quick tunnels are known to buffer SSE, which would leave the
  // stream silent and the page looking frozen. Polling every few seconds keeps
  // delivery working (a little slower) whenever events stop arriving. Both
  // paths share the `seq` cursor, so nothing is missed or duplicated.
  setInterval(() => {
    const quiet = Date.now() - state.lastEvent > 20000;
    if (quiet || !state.live) {
      if (!state.live) setStatus("polling for updates", null);
      pull();
    }
  }, 3000);

  // Tablets suspend background tabs aggressively; this is what makes items
  // appear instantly when the tablet is picked back up.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pull();
  });
}

// Keep relative timestamps honest without re-fetching.
setInterval(() => {
  if (state.items.size) render();
}, 30000);

// --- wiring ---

document.addEventListener("paste", handlePaste);

const clearBtn = document.getElementById("clear");
if (clearBtn) {
  clearBtn.addEventListener("click", async () => {
    if (!confirm("Clear everything from the buffer?")) return;
    try {
      await api("/api/items", { method: "DELETE" });
      state.items.clear();
      render();
      toast("Cleared");
    } catch (err) {
      toast(err.message, true);
    }
  });
}

const drop = document.getElementById("dropbox");
if (drop) {
  drop.addEventListener("click", () => {
    const sink = document.getElementById("sink");
    if (sink) sink.focus();
  });
  ["dragenter", "dragover"].forEach((name) =>
    drop.addEventListener(name, (e) => {
      e.preventDefault();
      drop.classList.add("hot");
    })
  );
  ["dragleave", "drop"].forEach((name) =>
    drop.addEventListener(name, () => drop.classList.remove("hot"))
  );
  drop.addEventListener("drop", async (e) => {
    e.preventDefault();
    for (const file of e.dataTransfer.files || []) {
      try {
        await sendBlob(file, file.name);
      } catch (err) {
        toast(err.message, true);
      }
    }
  });
}

if (IS_TABLET && !window.isSecureContext) {
  const banner = document.getElementById("insecure");
  if (banner) banner.hidden = false;
}

// --- sending from the tablet ---

// The file picker is the primary path: it needs no permissions, works over
// plain http, and covers photos, files and the camera. Clipboard reading is
// offered only where the browser actually supports it.
function initTabletSend() {
  const pick = document.getElementById("pickfile");
  const input = document.getElementById("fileinput");
  const textarea = document.getElementById("sendtext");
  const sendBtn = document.getElementById("sendtextbtn");
  const pasteBtn = document.getElementById("pasteclip");
  if (!pick || !input) return;

  pick.addEventListener("click", () => input.click());

  input.addEventListener("change", async () => {
    for (const file of input.files) {
      try {
        await sendBlob(file, file.name);
      } catch (err) {
        toast(err.message, true);
      }
    }
    // Reset so picking the same file twice still fires a change event.
    input.value = "";
  });

  textarea.addEventListener("input", () => {
    sendBtn.hidden = !textarea.value.trim();
  });

  sendBtn.addEventListener("click", async () => {
    const text = textarea.value.trim();
    if (!text) return;
    try {
      await sendText(text);
      textarea.value = "";
      sendBtn.hidden = true;
    } catch (err) {
      toast(err.message, true);
    }
  });

  // clipboard.read() needs a secure context and prompts for permission, so
  // only surface it when it exists — otherwise the button would just error.
  if (window.isSecureContext && navigator.clipboard && navigator.clipboard.read) {
    pasteBtn.hidden = false;
    pasteBtn.addEventListener("click", async () => {
      try {
        const items = await navigator.clipboard.read();
        let sent = false;
        for (const item of items) {
          const imageType = item.types.find((t) => t.startsWith("image/"));
          if (imageType) {
            await sendBlob(await item.getType(imageType), "pasted.png");
            sent = true;
          } else if (item.types.includes("text/plain")) {
            const blob = await item.getType("text/plain");
            const text = (await blob.text()).trim();
            if (text) {
              await sendText(text);
              sent = true;
            }
          }
        }
        if (!sent) toast("Clipboard is empty", true);
      } catch (err) {
        toast("Could not read clipboard — use Photo or file", true);
      }
    });
  }
}

// --- connect panel (laptop page only) ---

// Lives here rather than in an inline <script> because the page's CSP sets
// script-src 'self', which blocks inline blocks entirely.
function initConnectPanel() {
  const panel = document.getElementById("qrpanel");
  const hint = document.getElementById("tunnelhint");
  const img = document.getElementById("qrimg");
  const field = document.getElementById("tunnelurl");
  const note = document.getElementById("qrnote");
  if (!panel || !hint || !img) return;

  let tries = 0;

  function showHint() {
    panel.hidden = true;
    hint.hidden = false;
  }

  async function check() {
    try {
      const res = await fetch("/api/public-url", { credentials: "same-origin" });
      const { target, qr, secure } = await res.json();
      if (target) {
        field.value = target;
        note.textContent = secure
          ? "Scan once per session. The link includes the access token."
          : "Scan from your tablet on the same Wi-Fi. Transfers work; one-tap "
            + "copy needs the https tunnel.";
        // The QR arrives as a data URI on this same authenticated response,
        // so there is no second request that could fail auth.
        if (qr) img.src = qr;
        hint.hidden = secure;
        panel.hidden = false;

        // Keep checking while on the LAN fallback so the QR upgrades itself
        // if the tunnel connects later.
        if (!secure && ++tries < 20) setTimeout(check, 3000);
        return;
      }
    } catch (_) {}
    showHint();
    if (++tries < 20) setTimeout(check, 1500);
  }

  check();
}

if (IS_TABLET) initTabletSend();
else initConnectPanel();

refreshAll().then(connect);
