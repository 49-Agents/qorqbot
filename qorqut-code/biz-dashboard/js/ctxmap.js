// ---------------------------------------------------------------- context maps
// Mind maps as plain SVG, no libraries. `mindmap(el, root, opts)` draws a {label, kind, children} tree as a
// two-sided tidy tree: root in the middle, first-level branches split right and left, each branch its own
// colour. Drag to pan, wheel to zoom, click a badge to fold or unfold a branch, click a label to go there
// (opts.onOpen). Two adapters feed it: `ctxMapTree` (the context tree: topics, notes, people, with the
// `also` / `people` cross-references as dashed links) and `ctxMapIdea` (an upload's model-drawn mind map,
// context/_maps/<id>.md via `bizctx map show --json`). `mapMermaid` exports either as Mermaid mindmap text.

const MAP_FONT = "12.5px Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif";
const MAP_HUES = ["#b5482b", "#c8912a", "#4d6b53", "#3f7f7a", "#3d5a80", "#7a4f7a"];   // COLORS, one per branch
const MAP_LINE = 17, MAP_PADX = 10, MAP_PADY = 5, MAP_GAPX = 46, MAP_GAPY = 8, MAP_WRAP = 32, MAP_MAXLINES = 3;
const MAP_FOLD_ABOVE = 70;      // more visible nodes than this: start with depth >= 2 folded
let _mapMeasure = null;
const MAP_ICONA = `width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"`;
const SVG_MAPFOLD = `<svg ${MAP_ICONA}><path d="M12 22v-6"/><path d="M12 8V2"/><path d="M4 12H2"/><path d="M10 12H8"/><path d="M16 12h-2"/><path d="M22 12h-2"/><path d="m15 19-3-3-3 3"/><path d="m15 5-3 3-3-3"/></svg>`;
const SVG_MAPUNFOLD = `<svg ${MAP_ICONA}><path d="M12 22v-6"/><path d="M12 8V2"/><path d="M4 12H2"/><path d="M10 12H8"/><path d="M16 12h-2"/><path d="M22 12h-2"/><path d="m15 19-3 3-3-3"/><path d="m15 5-3-3-3 3"/></svg>`;
const SVG_MAPFIT = `<svg ${MAP_ICONA}><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>`;
const SVG_MAPCOPY = `<svg ${MAP_ICONA}><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;

function mapWidth(s) {
  if (!_mapMeasure) { _mapMeasure = document.createElement("canvas").getContext("2d"); _mapMeasure.font = MAP_FONT; }
  return _mapMeasure.measureText(s).width;
}

// word-wrap to `cols` characters, at most `max` lines, the last one ellipsised
function mapWrap(text, cols = MAP_WRAP, max = MAP_MAXLINES) {
  const words = String(text || "").split(/\s+/).filter(Boolean), lines = [];
  let cur = "";
  for (const w of words) {
    if (cur && (cur + " " + w).length > cols) { lines.push(cur); cur = ""; }
    cur = cur ? cur + " " + w : w;
  }
  if (cur) lines.push(cur);
  if (lines.length > max) { lines.length = max; lines[max - 1] = lines[max - 1].slice(0, cols - 1).replace(/\s+\S*$/, "") + "…"; }
  return lines.length ? lines : ["-"];
}

const mapCount = n => 1 + (n.children || []).reduce((s, k) => s + mapCount(k), 0);

// measure every node, assign keys, depth and branch colour; `folded` decides which branches are closed
function mapPrepare(node, depth, hue, key, folded) {
  node.key = node.id || key;
  node.depth = depth; node.hue = hue;
  node.lines = mapWrap(node.label, depth === 0 ? 26 : MAP_WRAP, depth === 0 ? 2 : MAP_MAXLINES);
  node.w = Math.ceil(Math.max(...node.lines.map(mapWidth))) + MAP_PADX * 2 + (depth === 0 ? 10 : 0);
  node.h = node.lines.length * MAP_LINE + MAP_PADY * 2;
  node.kids = node.children || [];
  node.open = node.kids.length > 0 && !folded.has(node.key);
  node.kids.forEach((k, i) => mapPrepare(k, depth + 1, depth === 0 ? MAP_HUES[i % MAP_HUES.length] : hue, `${node.key}/${i}`, folded));
}

// height of the visible subtree
function mapSH(node) {
  const kids = node.open ? node.kids : [];
  node.sh = kids.length ? Math.max(node.h, kids.reduce((s, k) => s + mapSH(k), 0) + MAP_GAPY * (kids.length - 1)) : node.h;
  return node.sh;
}

// place a node at centre (cx, cy) and stack its open children outward on `side` (+1 right, -1 left)
function mapPlace(node, cx, cy, side) {
  node.cx = cx; node.cy = cy; node.side = side;
  const kids = node.open ? node.kids : [];
  if (!kids.length) return;
  const total = kids.reduce((s, k) => s + k.sh, 0) + MAP_GAPY * (kids.length - 1);
  let y = cy - total / 2;
  for (const k of kids) {
    mapPlace(k, cx + side * (node.w / 2 + MAP_GAPX + k.w / 2), y + k.sh / 2, side);
    y += k.sh + MAP_GAPY;
  }
}

// root in the middle; with two sides the first-level branches go right and left, balanced by height
function mapLayout(root, sides) {
  mapSH(root);
  root.cx = 0; root.cy = 0; root.side = 0;
  const kids = root.open ? root.kids : [];
  let right = kids, left = [];
  if (sides === 2 && kids.length > 1) {
    right = []; left = [];
    let hr = 0, hl = 0;
    for (const k of kids) { if (hr <= hl) { right.push(k); hr += k.sh + MAP_GAPY; } else { left.push(k); hl += k.sh + MAP_GAPY; } }
  }
  const stack = (arr, side) => {
    const total = arr.reduce((s, k) => s + k.sh, 0) + MAP_GAPY * Math.max(0, arr.length - 1);
    let y = -total / 2;
    for (const k of arr) { mapPlace(k, side * (root.w / 2 + MAP_GAPX + k.w / 2), y + k.sh / 2, side); y += k.sh + MAP_GAPY; }
  };
  stack(right, 1); stack(left, -1);
}

const mapVisible = (node, out = []) => { out.push(node); if (node.open) node.kids.forEach(k => mapVisible(k, out)); return out; };
const mapBoxed = n => n.depth <= 1 || n.kind === "person" || n.kind === "group";
// where an edge meets a node: boxed nodes at mid-height, text-only nodes at their underline
const mapAnchor = (n, outward) => {
  const x = n.cx + (outward ? 1 : -1) * n.side * n.w / 2;
  return [x, mapBoxed(n) ? n.cy : n.cy + n.h / 2 - 2];
};
const mapCurve = (x1, y1, x2, y2) => { const m = (x1 + x2) / 2; return `M${x1.toFixed(1)} ${y1.toFixed(1)} C${m.toFixed(1)} ${y1.toFixed(1)}, ${m.toFixed(1)} ${y2.toFixed(1)}, ${x2.toFixed(1)} ${y2.toFixed(1)}`; };
const svgEl = (tag, attrs = {}) => { const e = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };

function mindmap(el, root, opts = {}) {
  const sides = opts.sides || ((root.children || []).length >= 4 ? 2 : 1);
  const folded = new Set();
  const total = mapCount(root);
  const openDepth = opts.openDepth != null ? opts.openDepth : (total > MAP_FOLD_ABOVE ? 2 : 99);
  (function seed(n, d, key) { const k = n.id || key; if (d >= openDepth && (n.children || []).length) folded.add(k); (n.children || []).forEach((c, i) => seed(c, d + 1, `${k}/${i}`)); })(root, 0, "root");
  el.innerHTML = `<div class="mapwrap"><div class="mapbar">
      <span class="sp"></span>
      ${opts.links ? `<label><input type="checkbox" id="mlinks" ${ls("ctxmaplinks") === "0" ? "" : "checked"}> links</label>` : ""}
      <button data-a="less" title="Fold all">${SVG_MAPFOLD}</button>
      <button data-a="more" title="Unfold all">${SVG_MAPUNFOLD}</button>
      <button data-a="fit" title="Fit to view">${SVG_MAPFIT}</button>
      <button data-a="mermaid" title="Copy as Mermaid">${SVG_MAPCOPY}</button>
    </div><div class="mapbox"><svg><g class="vp"></g></svg><div class="hint">drag to pan · wheel to zoom</div></div></div>`;
  const box = el.querySelector(".mapbox"), svg = el.querySelector("svg"), vp = el.querySelector(".vp");
  const view = { k: 1, tx: 0, ty: 0 };
  const apply = () => vp.setAttribute("transform", `translate(${view.tx} ${view.ty}) scale(${view.k})`);
  let nodes = [];

  const draw = () => {
    mapPrepare(root, 0, "", "root", folded);
    mapLayout(root, sides);
    nodes = mapVisible(root);
    vp.innerHTML = "";
    const edges = svgEl("g"), links = svgEl("g"), gn = svgEl("g");
    vp.append(edges, links, gn);
    for (const n of nodes) {
      if (n.depth === 0 || !n.open) continue;
      for (const k of n.kids) {
        const [x1, y1] = mapAnchor(n, true), [x2, y2] = mapAnchor(k, false);
        edges.appendChild(svgEl("path", { class: "medge", d: mapCurve(x1, y1, x2, y2), stroke: k.hue || "#8a8a8a" }));
      }
    }
    if (root.open) for (const k of root.kids) {
      const x1 = root.cx + k.side * root.w / 2, [x2, y2] = mapAnchor(k, false);
      edges.appendChild(svgEl("path", { class: "medge", d: mapCurve(x1, root.cy, x2, y2), stroke: k.hue }));
    }
    const wantLinks = opts.links && (!el.querySelector("#mlinks") || el.querySelector("#mlinks").checked);
    if (wantLinks) {
      const byId = new Map(nodes.map(n => [n.key, n]));
      for (const n of nodes) for (const to of (n.links || [])) {
        const t = byId.get(to);
        if (!t || t === n) continue;
        links.appendChild(svgEl("path", { class: "mlink", d: mapCurve(n.cx, n.cy - n.h / 2, t.cx, t.cy - t.h / 2) }));
      }
    }
    for (const n of nodes) {
      const g = svgEl("g", { class: `mnode d${Math.min(n.depth, 2)} ${n.kind || ""} ${n.kids.length ? "" : "leaf"} ${opts.onOpen && n.path != null ? "go" : ""}`.trim() });
      const x = n.cx - n.w / 2, y = n.cy - n.h / 2;
      if (mapBoxed(n)) g.appendChild(svgEl("rect", { x, y, width: n.w, height: n.h, rx: n.depth === 0 ? 10 : 7, stroke: n.depth === 0 ? "none" : (n.kind === "group" ? "" : n.hue || "#8a8a8a") }));
      else g.appendChild(svgEl("line", { class: "ul", x1: x, y1: y + n.h - 2, x2: x + n.w, y2: y + n.h - 2, stroke: n.hue || "#8a8a8a" }));
      const t = svgEl("text", { x: n.cx, y: y + MAP_PADY + 12.5, "text-anchor": "middle" });
      n.lines.forEach((ln, i) => { const ts = svgEl("tspan", { x: n.cx, dy: i ? MAP_LINE : 0 }); ts.textContent = ln; t.appendChild(ts); });
      g.appendChild(t);
      t.onclick = e => { e.stopPropagation(); if (opts.onOpen && n.path != null) opts.onOpen(n); else if (n.kids.length) toggle(n); };
      if (n.kids.length) {
        const bx = n.cx + (n.side || 1) * (n.w / 2 + 9), by = mapBoxed(n) ? n.cy : n.cy + n.h / 2 - 2;
        const b = svgEl("g", { class: `badge ${n.open ? "" : "closed"}` });
        b.appendChild(svgEl("circle", { cx: bx, cy: by, r: 7.5, stroke: n.hue || "#8a8a8a" }));
        const bt = svgEl("text", { x: bx, y: by }); bt.textContent = n.open ? "–" : String(n.kids.length); b.appendChild(bt);
        b.onclick = e => { e.stopPropagation(); toggle(n); };
        g.appendChild(b);
      }
      if (n.tip && typeof tip === "function") tip(g, () => `<div class="tt">${esc(n.tip)}</div>`);
      gn.appendChild(g);
    }
    apply();
  };
  const toggle = n => { folded.has(n.key) ? folded.delete(n.key) : folded.add(n.key); draw(); };
  const fit = () => {
    if (!nodes.length) return;
    const W = box.clientWidth || 800, H = box.clientHeight || 500;
    const xs0 = Math.min(...nodes.map(n => n.cx - n.w / 2 - 20)), xs1 = Math.max(...nodes.map(n => n.cx + n.w / 2 + 20));
    const ys0 = Math.min(...nodes.map(n => n.cy - n.h / 2)), ys1 = Math.max(...nodes.map(n => n.cy + n.h / 2));
    view.k = Math.min(1.25, (W - 32) / Math.max(1, xs1 - xs0), (H - 32) / Math.max(1, ys1 - ys0));
    view.tx = W / 2 - ((xs0 + xs1) / 2) * view.k; view.ty = H / 2 - ((ys0 + ys1) / 2) * view.k;
    apply();
  };
  const foldTo = depth => { folded.clear(); (function walk(n, d) { if (d >= depth && n.kids.length) folded.add(n.key); n.kids.forEach(k => walk(k, d + 1)); })(root, 0); draw(); fit(); };
  el.querySelectorAll(".mapbar button").forEach(b => b.onclick = () => {
    const a = b.dataset.a;
    if (a === "fit") fit();
    else if (a === "less") foldTo(1);
    else if (a === "more") foldTo(99);
    else if (a === "mermaid") { const txt = mapMermaid(root); (navigator.clipboard ? navigator.clipboard.writeText(txt) : Promise.reject()).then(() => toast("Mermaid mindmap copied"), () => modal({ title: "Mermaid mindmap", body: `<pre>${esc(txt)}</pre>`, width: 620 })); }
  });
  const lk = el.querySelector("#mlinks"); if (lk) lk.onchange = () => { ls("ctxmaplinks", lk.checked ? "1" : "0"); draw(); };
  // pan and zoom
  let drag = null;
  box.onmousedown = e => { if (e.button !== 0) return; drag = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty }; box.classList.add("drag"); };
  window.addEventListener("mousemove", e => { if (!drag) return; view.tx = drag.tx + e.clientX - drag.x; view.ty = drag.ty + e.clientY - drag.y; apply(); });
  window.addEventListener("mouseup", () => { drag = null; box.classList.remove("drag"); });
  box.onwheel = e => {
    e.preventDefault();
    const r = box.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const k2 = Math.min(3, Math.max(0.2, view.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
    view.tx = mx - (mx - view.tx) * k2 / view.k; view.ty = my - (my - view.ty) * k2 / view.k; view.k = k2; apply();
  };
  draw();
  fit();
  return { draw, fit, root };
}

// Mermaid `mindmap` text of a {label, children} tree; brackets, braces and quotes cannot appear in node text
const mmText = s => { s = String(s || "").replace(/\s+/g, " ").replace(/[\[\]{}()"`]/g, "").replace(/::/g, ":").trim(); return s.length > 60 ? s.slice(0, 59).trimEnd() + "…" : (s || "-"); };
function mapMermaid(root) {
  const out = ["mindmap", `  root((${mmText(root.label)}))`];
  const walk = (n, d) => (n.children || []).forEach(k => { out.push("  ".repeat(d + 1) + (k.kind === "topic" ? `(${mmText(k.label)})` : mmText(k.label))); walk(k, d + 1); });
  walk(root, 1);
  return out.join("\n") + "\n";
}

// ------------------------------------------------------------- adapters
// the context tree as a map: topic -> subtopics -> notes; at the root also the Core pillars and every person.
// `links` on a note point at "topic:<path>" (also) and "person:<slug>" (people), matched by node id.
function ctxMapTree(d, node, isRoot) {
  const mk = t => ({
    label: t.title || t.slug, kind: "topic", id: `topic:${t.path}`, path: t.path, tip: t.about || "",
    children: [
      ...(t.children || []).map(mk),
      ...(t.notes || []).map(n => ({
        label: (t.titles || {})[n] || n.replace(/^\d{4}-\d{2}-\d{2}-/, "").replace(/\.md$/, "").replace(/-/g, " "),
        kind: "note", id: `note:${t.path}/${n}`, path: `${t.path}/${n}`, tip: (t.summaries || {})[n] || (t.previews || {})[n] || "",
        links: [...((t.also || {})[n] || []).map(a => `topic:${a}`), ...((t.people || {})[n] || []).map(s => `person:${s}`)], children: [] })),
    ],
  });
  if (!isRoot) return mk(node);
  const biz = (state.bizs || []).find(b => b.slug === state.biz);
  const root = { label: (biz && biz.name) || state.biz || "context", kind: "root", id: "root", children: (d.tree && d.tree.children || []).map(mk) };
  const pillars = d.pillars || [];
  if (pillars.length) root.children.unshift({ label: "Core", kind: "group", id: "core", children: pillars.map(n => ({
    label: titleCase(n.replace(/\.md$/, "")), kind: "note", id: `pillar:${n}`, path: n, tip: "owner-written", children: [] })) });
  const people = d.people || [];
  if (people.length) root.children.push({ label: "People", kind: "group", id: "people", children: people.map(p => ({
    label: p.name + (p.role ? ` · ${p.role}` : ""), kind: "person", id: `person:${p.slug}`, path: `_people/${p.slug}`, tip: p.about || p.summary || "", children: [] })) });
  return root;
}

// an upload's model-drawn mind map ({text, children} from bizctx map show --json) as renderer input
const ctxMapIdea = (t, depth = 0) => ({ label: t.text, kind: depth ? "idea" : "root", children: (t.children || []).map(c => ctxMapIdea(c, depth + 1)) });
