/**
 * A small flowchart renderer, used by the diagram popup.
 *
 * WHY THIS EXISTS
 * Mermaid lays a diagram out and then bakes the result: nodes become <g>
 * elements carrying transform="translate(x,y)", edges become <path> elements
 * whose "d" is a frozen list of points, and label boxes become <foreignObject>
 * elements that clip to the size mermaid measured. The layout data itself is
 * discarded.
 *
 * Making that interactive meant reverse-engineering the data back out of the
 * rendering — regex-parsing translate(), rewriting path coordinates, matching
 * edges to nodes through an undocumented id convention, growing clipped label
 * boxes — and each patch left some other baked number stale (an SVG that ended
 * up 746x67, labels sliced through their descenders).
 *
 * So this keeps the model instead of the markup. Nodes hold x/y/w/h, edges
 * hold endpoints, and the SVG is drawn FROM that on every change. Dragging is
 * then "set x/y and redraw", and edges follow because they are derived rather
 * than stored. The whole class of stale-geometry bugs cannot occur.
 *
 * SCOPE
 * Deliberately covers only what the tutor is asked to emit — flowcharts (see
 * prompt_files/pymentor/30_output_formatting.md). Of 18 diagrams in this
 * project's own transcripts: all 18 are `flowchart`, 15 LR / 3 TB-TD, 194
 * [rect] / 4 (round) / 3 {diamond}, 106 `-->`, 25 subgraphs, 12 edge labels.
 * Anything outside that makes parse() return null, and the caller falls back
 * to mermaid — an unsupported diagram must render the old way, never wrong.
 */

// --- Parsing ---------------------------------------------------------

/** Shapes, longest bracket first so [( is not read as [ then (. */
const FC_SHAPES = [
    ["([", "])", "stadium"],
    ["[(", ")]", "cylinder"],
    ["[", "]", "rect"],
    ["((", "))", "circle"],
    ["(", ")", "round"],
    ["{", "}", "diamond"],
];

/**
 * Strip mermaid's label quoting and turn <br/> into real newlines.
 * Labels arrive as `A[Plain]`, `A["Quoted, with comma"]` or with <br/>.
 */
function fcLabel(raw) {
    let s = (raw || "").trim();
    if ((s.startsWith('"') && s.endsWith('"')) ||
        (s.startsWith("'") && s.endsWith("'"))) {
        s = s.slice(1, -1);
    }
    return s.replace(/<br\s*\/?>/gi, "\n");
}

/**
 * Read one `id[Label]` (or bare `id`) at the start of a token, returning the
 * node and what is left. Bare ids are legal — `A --> B` declares both.
 */
function fcReadNode(token) {
    const m = /^([A-Za-z0-9_-]+)/.exec(token.trim());
    if (!m) return null;
    const id = m[1];
    let rest = token.trim().slice(id.length);
    for (const [open, close, shape] of FC_SHAPES) {
        if (rest.startsWith(open)) {
            const end = rest.indexOf(close, open.length);
            if (end === -1) continue;
            return {
                id,
                label: fcLabel(rest.slice(open.length, end)),
                shape,
                rest: rest.slice(end + close.length),
            };
        }
    }
    return { id, label: null, shape: null, rest };
}

/**
 * Parse flowchart source into {dir, nodes, edges, groups}.
 * Returns null for anything not understood, so the caller can fall back.
 */
function parseFlowchart(src) {
    const lines = String(src || "").split("\n");
    const nodes = new Map();
    const edges = [];
    const groups = [];
    const stack = [];          // open subgraphs, innermost last
    let dir = "TB";
    let seenHeader = false;

    const touch = (id, label, shape) => {
        let n = nodes.get(id);
        if (!n) {
            n = { id, label: id, shape: "rect", group: null };
            nodes.set(id, n);
        }
        // A later declaration with a real label wins over a bare mention.
        if (label != null) n.label = label;
        if (shape) n.shape = shape;
        if (!n.group && stack.length) n.group = stack[stack.length - 1].id;
        return n;
    };

    for (const raw of lines) {
        const line = raw.trim();
        if (!line || line.startsWith("%%")) continue;

        const header = /^(?:flowchart|graph)\s+(TB|TD|BT|LR|RL)\b/i.exec(line);
        if (header) {
            dir = header[1].toUpperCase();
            seenHeader = true;
            continue;
        }
        // Only flowcharts. sequenceDiagram, classDiagram, pie, gantt and the
        // rest have entirely different grammars — decline them.
        if (!seenHeader) {
            if (/^(flowchart|graph)\b/i.test(line)) { seenHeader = true; continue; }
            return null;
        }

        if (/^end\b/i.test(line)) { stack.pop(); continue; }

        // `subgraph Id["Title"]`, `subgraph Id[Title]`, or `subgraph Bare Title`
        const sg = /^subgraph\s+(.*)$/i.exec(line);
        if (sg) {
            const head = sg[1].trim();
            const withLabel = /^([A-Za-z0-9_-]+)\s*[[("]/.test(head)
                ? fcReadNode(head) : null;
            const g = withLabel && withLabel.label != null
                ? { id: withLabel.id, title: withLabel.label }
                : { id: `__g${groups.length}`, title: fcLabel(head) };
            g.parent = stack.length ? stack[stack.length - 1].id : null;
            groups.push(g);
            stack.push(g);
            continue;
        }

        // Styling directives carry no structure; skip rather than fail on them.
        if (/^(style|classDef|class|linkStyle|click)\b/i.test(line)) continue;

        // Edges. Split on the arrow forms mermaid uses, keeping the connector
        // so its label (`-- yes -->`) and dottedness survive.
        const parts = line.split(/(\s*(?:-{2,3}|-\.-?|={2,3})(?:[^->|]*?)?(?:-{1,2}>|>|-{2,3}|\.->)\s*|\s*-{2,3}>\s*)/);
        if (parts.length >= 3) {
            let cursor = fcReadNode(parts[0]);
            if (!cursor) continue;
            touch(cursor.id, cursor.label, cursor.shape);
            for (let i = 1; i < parts.length - 1; i += 2) {
                const conn = parts[i] || "";
                // Mermaid writes an edge label two ways:
                //   A -- yes --> B     inside the connector
                //   A -->|yes| B       immediately AFTER the arrow
                // The second form must be taken off the front of the segment
                // BEFORE reading the node, or `|yes| Reuse[...]` parses as no
                // node at all — which silently dropped both branches of every
                // decision diamond while leaving the rest of the diagram fine.
                let segment = parts[i + 1] || "";
                let label = "";
                const lead = /^\s*\|([^|]*)\|/.exec(segment);
                if (lead) {
                    label = lead[1].trim();
                    segment = segment.slice(lead[0].length);
                }
                const next = fcReadNode(segment);
                if (!next) break;
                touch(next.id, next.label, next.shape);
                if (!label) {
                    const inConn = /-{2,3}\s*([^->]+?)\s*-{1,2}>/.exec(conn)
                        || /-\.\s*([^->]+?)\s*\.?->/.exec(conn);
                    if (inConn) label = inConn[1].trim();
                }
                edges.push({
                    from: cursor.id,
                    to: next.id,
                    label: fcLabel(label),
                    dotted: /-\./.test(conn),
                });
                cursor = next;
            }
            continue;
        }

        // A standalone node declaration.
        const solo = fcReadNode(line);
        if (solo && solo.label != null) touch(solo.id, solo.label, solo.shape);
    }

    if (!nodes.size) return null;
    return { dir, nodes: [...nodes.values()], edges, groups };
}

// --- Layout ----------------------------------------------------------

const FC_PAD_X = 14;      // inside a node box, around its text
const FC_PAD_Y = 10;
const FC_GAP_RANK = 90;   // between successive ranks (along the flow)
const FC_GAP_NODE = 26;   // between siblings within a rank
const FC_LINE = 18;       // label line height
const FC_CHAR = 7.2;      // ~width of one 15px Inter character
const FC_GROUP_PAD = 16;  // subgraph frame, outside its members

/**
 * Text size, measured off the label rather than guessed from the box.
 *
 * A diamond needs a bigger box than a rectangle for the same text: its edges
 * slope inward, so the widest point is only the vertical centre line and text
 * near the top or bottom runs past the shape. Roughly doubling the box keeps
 * the label inside the sloped sides.
 */
function fcMeasure(label, shape) {
    const lines = String(label || "").split("\n");
    const widest = lines.reduce((w, l) => Math.max(w, l.length), 0);
    const w = Math.max(70, widest * FC_CHAR) + FC_PAD_X * 2;
    const h = lines.length * FC_LINE + FC_PAD_Y * 2;
    if (shape === "diamond") {
        return { w: w * 1.6, h: h * 2.2, lines };
    }
    return { w, h, lines };
}

/**
 * Assign ranks by longest path from a root, then place ranks as rows (TB) or
 * columns (LR).
 *
 * Longest path, not shortest: a node must sit after everything that feeds it,
 * so an edge never points backwards. Cycles are broken by visit order — the
 * back-edge simply does not push its target further along.
 */
function layoutFlowchart(model) {
    const { nodes, edges, dir } = model;
    const byId = new Map(nodes.map(n => [n.id, n]));
    const out = new Map(nodes.map(n => [n.id, []]));
    const indeg = new Map(nodes.map(n => [n.id, 0]));
    edges.forEach(e => {
        if (!byId.has(e.from) || !byId.has(e.to)) return;
        out.get(e.from).push(e.to);
        indeg.set(e.to, indeg.get(e.to) + 1);
    });

    const rank = new Map(nodes.map(n => [n.id, 0]));
    const queue = nodes.filter(n => indeg.get(n.id) === 0).map(n => n.id);
    // Every node starts eligible if the graph is one big cycle.
    if (!queue.length && nodes.length) queue.push(nodes[0].id);
    const seen = new Set(queue);
    for (let i = 0; i < queue.length; i++) {
        const id = queue[i];
        for (const nxt of out.get(id) || []) {
            if (rank.get(nxt) < rank.get(id) + 1) rank.set(nxt, rank.get(id) + 1);
            if (!seen.has(nxt)) { seen.add(nxt); queue.push(nxt); }
        }
    }

    nodes.forEach(n => {
        const m = fcMeasure(n.label, n.shape);
        n.w = m.w; n.h = m.h; n.lines = m.lines;
        n.rank = rank.get(n.id) || 0;
    });

    // Group nodes by rank, keeping declaration order within each.
    const ranks = new Map();
    nodes.forEach(n => {
        if (!ranks.has(n.rank)) ranks.set(n.rank, []);
        ranks.get(n.rank).push(n);
    });

    const horizontal = dir === "LR" || dir === "RL";
    let along = 0;
    [...ranks.keys()].sort((a, b) => a - b).forEach(r => {
        const row = ranks.get(r);
        // Extent of this rank across the axis it spreads on.
        const across = row.reduce(
            (s, n) => s + (horizontal ? n.h : n.w) + FC_GAP_NODE, -FC_GAP_NODE
        );
        const depth = row.reduce(
            (d, n) => Math.max(d, horizontal ? n.w : n.h), 0
        );
        let cursor = -across / 2;
        row.forEach(n => {
            if (horizontal) {
                n.x = along + (depth - n.w) / 2;
                n.y = cursor;
                cursor += n.h + FC_GAP_NODE;
            } else {
                n.x = cursor;
                n.y = along + (depth - n.h) / 2;
                cursor += n.w + FC_GAP_NODE;
            }
        });
        along += depth + FC_GAP_RANK;
    });

    // Normalise to positive coordinates so the viewBox starts at 0.
    // Offset enough that a group frame drawn around the topmost/leftmost
    // nodes still has room for its border and title.
    const inset = 20 + (model.groups && model.groups.length
        ? FC_GROUP_PAD + FC_LINE : 0);
    const minX = Math.min(...nodes.map(n => n.x));
    const minY = Math.min(...nodes.map(n => n.y));
    nodes.forEach(n => { n.x -= minX - inset; n.y -= minY - inset; });
    return model;
}

// --- Rendering -------------------------------------------------------

const FC_NS = "http://www.w3.org/2000/svg";
const el = (name, attrs) => {
    const e = document.createElementNS(FC_NS, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
};

/** Where an edge meets a box: the border point on the line between centres. */
function fcAnchor(node, tx, ty) {
    const cx = node.x + node.w / 2, cy = node.y + node.h / 2;
    const dx = tx - cx, dy = ty - cy;
    if (!dx && !dy) return { x: cx, y: cy };
    // Scale the direction until it lands on the nearer pair of sides.
    const sx = dx ? (node.w / 2) / Math.abs(dx) : Infinity;
    const sy = dy ? (node.h / 2) / Math.abs(dy) : Infinity;
    const s = Math.min(sx, sy);
    return { x: cx + dx * s, y: cy + dy * s };
}

function fcNodePath(n) {
    if (n.shape === "diamond") {
        const cx = n.x + n.w / 2, cy = n.y + n.h / 2;
        return el("polygon", {
            points: `${cx},${n.y} ${n.x + n.w},${cy} ${cx},${n.y + n.h} ${n.x},${cy}`,
            class: "fc-shape",
        });
    }
    const r = (n.shape === "round" || n.shape === "stadium") ? n.h / 2 : 6;
    return el("rect", {
        x: n.x, y: n.y, width: n.w, height: n.h, rx: r, ry: r, class: "fc-shape",
    });
}

/**
 * The fixed canvas the diagram lives on, computed once from the first layout.
 *
 * Deliberately much larger than the diagram: this is the area a node can be
 * dragged around in, so it needs slack on every side. A box that merely
 * bounded the nodes left nowhere to drag TO — the surface ended exactly where
 * the diagram did, which is what read as "an empty pane cutting out".
 *
 * Stored on the model so every redraw reuses the same coordinate system. If it
 * were recomputed per frame, dragging one node past the edge would move the
 * origin and shift every other node on screen.
 */
function fcComputeBox(model) {
    const nodes = model.nodes;
    const pad = 20 + ((model.groups || []).length ? FC_GROUP_PAD + FC_LINE : 0);
    const minX = Math.min(...nodes.map(n => n.x)) - pad;
    const minY = Math.min(...nodes.map(n => n.y)) - pad;
    const maxX = Math.max(...nodes.map(n => n.x + n.w)) + pad;
    const maxY = Math.max(...nodes.map(n => n.y + n.h)) + pad;
    const w = maxX - minX;
    const h = maxY - minY;
    // A full diagram's width of free space on each side, floored at 1200/900
    // so even a three-node chart has real room. Sized for the drag to run out
    // of intent before it runs out of canvas: at half this, a deliberate throw
    // across the window still put the node outside the box and clipped it.
    // Empty SVG area costs nothing — it paints no background.
    const slackX = Math.max(1200, w);
    const slackY = Math.max(900, h);
    model.box = {
        x: minX - slackX,
        y: minY - slackY,
        w: w + slackX * 2,
        h: h + slackY * 2,
    };
    return model.box;
}

/**
 * Draw the model into `host`. Called again on every drag, so it rebuilds
 * rather than mutating — the model is the single source of truth, and a full
 * redraw is what keeps edges honest.
 */
function renderFlowchart(host, model) {
    host.textContent = "";
    const nodes = model.nodes;
    const byId = new Map(nodes.map(n => [n.id, n]));

    // The viewBox is FIXED for the life of the diagram: computed once from the
    // initial layout (model.box) and reused on every redraw.
    //
    // It must not be recomputed per render. Doing that made the coordinate
    // system move whenever a node was dragged past the current extent: pulling
    // one node left shrank minX, which slid the viewBox origin left, which
    // pushed every OTHER node right on screen even though nothing else had
    // changed in the model. The dragged node appeared stuck while the rest of
    // the diagram slid away.
    //
    // A fixed box also has to be big enough to drag INTO — the earlier version
    // was a tight wrapper around the nodes, so there was nowhere to go. Hence
    // the generous margin in fcComputeBox: it is the canvas, not a bounding box.
    const box = model.box || fcComputeBox(model);
    const svg = el("svg", {
        class: "fc-svg",
        // width/height must stay in step with the viewBox: when those two
        // disagree the SVG reserves one size and paints another, which is
        // what produced an earlier 746x67 diagram.
        width: box.w, height: box.h,
        viewBox: `${box.x} ${box.y} ${box.w} ${box.h}`,
        xmlns: FC_NS,
    });

    const defs = el("defs");
    const marker = el("marker", {
        id: "fc-arrow", viewBox: "0 0 10 10", refX: "9", refY: "5",
        markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse",
    });
    marker.appendChild(el("path", { d: "M 0 0 L 10 5 L 0 10 z", class: "fc-arrow-head" }));
    defs.appendChild(marker);
    svg.appendChild(defs);

    // Subgraph frames first, underneath everything: they are background, and
    // a node inside one must stay clickable. Each is the bounding box of its
    // members, so it follows them when a node is dragged out of the group.
    const gGroups = el("g", { class: "fc-groups" });
    (model.groups || []).forEach(grp => {
        const members = nodes.filter(n => n.group === grp.id);
        if (!members.length) return;
        const x0 = Math.min(...members.map(n => n.x)) - FC_GROUP_PAD;
        const y0 = Math.min(...members.map(n => n.y)) - FC_GROUP_PAD - FC_LINE;
        const x1 = Math.max(...members.map(n => n.x + n.w)) + FC_GROUP_PAD;
        const y1 = Math.max(...members.map(n => n.y + n.h)) + FC_GROUP_PAD;
        gGroups.appendChild(el("rect", {
            x: x0, y: y0, width: x1 - x0, height: y1 - y0,
            rx: 8, ry: 8, class: "fc-group",
        }));
        if (grp.title) {
            const t = el("text", {
                x: (x0 + x1) / 2, y: y0 + FC_LINE,
                class: "fc-group-title", "text-anchor": "middle",
            });
            t.textContent = grp.title;
            gGroups.appendChild(t);
        }
    });
    svg.appendChild(gGroups);

    // Edges next, so node boxes paint over the line ends.
    const gEdges = el("g", { class: "fc-edges" });
    model.edges.forEach(e => {
        const a = byId.get(e.from), b = byId.get(e.to);
        if (!a || !b) return;
        const p1 = fcAnchor(a, b.x + b.w / 2, b.y + b.h / 2);
        const p2 = fcAnchor(b, a.x + a.w / 2, a.y + a.h / 2);
        gEdges.appendChild(el("line", {
            x1: p1.x, y1: p1.y, x2: p2.x, y2: p2.y,
            class: e.dotted ? "fc-edge fc-edge-dotted" : "fc-edge",
            "marker-end": "url(#fc-arrow)",
        }));
        if (e.label) {
            const t = el("text", {
                x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2 - 4,
                class: "fc-edge-label", "text-anchor": "middle",
            });
            t.textContent = e.label;
            gEdges.appendChild(t);
        }
    });
    svg.appendChild(gEdges);

    const gNodes = el("g", { class: "fc-nodes" });
    nodes.forEach(n => {
        const g = el("g", { class: "fc-node", "data-id": n.id });
        g.appendChild(fcNodePath(n));
        // Text as real <text>/<tspan>, not a <foreignObject>: a foreignObject
        // clips to its own box, which is the whole reason labels were being
        // cut through their descenders. SVG text simply paints.
        const t = el("text", {
            x: n.x + n.w / 2,
            y: n.y + n.h / 2 - ((n.lines.length - 1) * FC_LINE) / 2 + 5,
            class: "fc-label", "text-anchor": "middle",
        });
        n.lines.forEach((ln, i) => {
            const ts = el("tspan", { x: n.x + n.w / 2, dy: i ? FC_LINE : 0 });
            ts.textContent = ln;
            t.appendChild(ts);
        });
        g.appendChild(t);
        gNodes.appendChild(g);
    });
    svg.appendChild(gNodes);
    host.appendChild(svg);
    return svg;
}

/**
 * Parse, lay out and draw, wiring each node for dragging.
 *
 * Returns false if the source is not a flowchart this understands, so the
 * caller renders it with mermaid instead.
 */
function drawInteractiveFlowchart(host, src) {
    let model;
    try {
        model = parseFlowchart(src);
        if (!model) return false;
        layoutFlowchart(model);
    } catch (e) {
        return false;
    }

    const draw = () => {
        const svg = renderFlowchart(host, model);
        svg.querySelectorAll(".fc-node").forEach(g => {
            const node = model.nodes.find(n => n.id === g.dataset.id);
            if (!node) return;
            g.addEventListener("pointerdown", (ev) => {
                if (ev.button !== 0) return;
                // Keep the popup's pan handler from also grabbing this.
                ev.stopPropagation();
                ev.preventDefault();
                const sx = ev.clientX, sy = ev.clientY;
                const ox = node.x, oy = node.y;
                const scale = (typeof zoomState === "object" && zoomState)
                    ? zoomState.scale : 1;
                const move = (e2) => {
                    node.x = ox + (e2.clientX - sx) / scale;
                    node.y = oy + (e2.clientY - sy) / scale;
                    draw();   // edges recompute from the model, so they follow
                };
                const up = () => {
                    window.removeEventListener("pointermove", move);
                    window.removeEventListener("pointerup", up);
                };
                window.addEventListener("pointermove", move);
                window.addEventListener("pointerup", up);
            });
        });
        return svg;
    };
    const svg = draw();

    // Fit the diagram to the window on open.
    //
    // The view used to start at scale 1 anchored top-left, so anything wider
    // than the stage ran off the corner and had to be zoomed out by hand
    // before it could be read. Scale down to fit, but never up: blowing a
    // three-node diagram up to fill a large window makes it look broken.
    if (typeof zoomState === "object" && zoomState) {
        const stage = host.parentElement;
        const w = parseFloat(svg.getAttribute("width"));
        const h = parseFloat(svg.getAttribute("height"));
        if (stage && stage.clientWidth && w > 0 && h > 0) {
            const fit = Math.min(1, stage.clientWidth / w, stage.clientHeight / h);
            zoomState.scale = fit;
            zoomState.tx = (stage.clientWidth - w * fit) / 2;
            zoomState.ty = (stage.clientHeight - h * fit) / 2;
            if (typeof applyZoomTransform === "function") applyZoomTransform();
        }
    }
    return true;
}
