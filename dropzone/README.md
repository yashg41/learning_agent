# Dropzone

A drop window on your laptop. Paste a screenshot or some text into it, and it
appears on your tablet ready to paste into your notes app.

Replaces: screenshot → save → WhatsApp → download → copy → paste.

Nothing is sent automatically. Only what you deliberately paste into the window
leaves the laptop.

## Setup (once)

```bash
./setup.sh
```

Installs the Python packages and cloudflared.

## Running

```bash
python run.py
```

Starts the learning app, Dropzone, and the tunnel together, then prints every
URL you need. Connecting a tablet is optional — the address and QR code are
always there if you want them.

| Page | Device | Address |
|---|---|---|
| Learning app | laptop | `http://127.0.0.1:8001` |
| Drop window | laptop | `http://localhost:8002/?t=TOKEN` |
| Receiver | tablet | `https://<random>.trycloudflare.com/t?t=TOKEN` |

Escape hatches, not needed for normal use: `DROPZONE=0` skips Dropzone,
`TUNNEL=0` skips the tunnel (laptop and same-Wi-Fi only).

### If the tunnel can't connect

Some networks (corporate, campus, some ISPs) block outbound port **7844**,
which cloudflared requires. `run.py` detects this, says so, and falls back to
the local Wi-Fi address — everything still works except one-tap clipboard copy
on the tablet, because browsers only expose the clipboard API over https.

On such a network your options are: use the Save button on the tablet instead
of Copy, switch to a network that allows 7844 (a phone hotspot usually does),
or set up Tailscale, which uses different ports and gives a stable https
address.

## Daily use

1. `Cmd+Ctrl+Shift+4`, drag a region — this copies to the clipboard.
   (Plain `Cmd+Shift+4` saves a file to the Desktop and never reaches the
   clipboard. That's the most common first-run confusion.)
2. `Cmd+V` in the drop window. Text and dragged-in files work too.
3. On the tablet it appears within a second or two. Tap **Copy image**, then
   paste into your notes.

## Why the tunnel isn't optional

Browsers expose `navigator.clipboard` only on secure origins. Over plain
`http://` on a LAN address the object does not exist at all — the tablet page
would load, show your screenshots, and be unable to copy any of them.

`TUNNEL=0 python run.py` runs without a tunnel. Your tablet can still reach it
over the same Wi-Fi and save images via the Save button, but one-tap copy will
not work.

The tunnel also happens to solve reachability: it works the same whether the
tablet is on your Wi-Fi, on a network with client isolation, or on mobile data.

## Design notes

**Ephemeral by design.** Items live in memory only and are gone on restart.
This is a hand-off buffer, not an archive — persisting whatever you pasted
(credentials, exam material) to disk would be a liability. Only the **3 most
recent items** are kept; older ones are dropped automatically. Also bounded to
75 MB total / 25 MB per item / 2 hour TTL.

**Separate from the learning app.** That app binds loopback and exposes a code
execution endpoint; this one must be reachable from the tablet. They run as two
processes on two ports and share nothing.

**Auth.** A token generated per run, carried in the QR link, exchanged for an
HttpOnly cookie on first load and then stripped from the address bar. Pin it
across restarts with `DROPZONE_TOKEN=...` if you want a stable QR.

**Realtime.** SSE, with a 3-second polling fallback on a shared sequence
cursor — Cloudflare quick tunnels are known to buffer SSE, and the fallback
means that degrades to "a bit slower" instead of "broken".

## Sharp edges

- **iPad long-press may paste a link instead of the image**, depending on the
  destination app. Apple Notes generally handles it correctly. Use the **Copy
  image** button first; long-press is the fallback.
- **Safari can report a successful copy when nothing was copied.** The image
  stays on screen after copying so you always have a manual path.
- **The tunnel URL changes every restart**, hence the QR code. For a permanent
  address, Tailscale + `tailscale serve` works with this same server — it just
  needs an account and a VPN app on the tablet.

## Tests

```bash
pip install qrcode pytest    # test-only, not runtime dependencies
pytest dropzone/test_qr.py
```

The QR encoder is hand-rolled to avoid a dependency, so it is pinned against
the reference `qrcode` package across 229 payload lengths and every version it
supports. A single misplaced module produces a code that silently won't scan.
