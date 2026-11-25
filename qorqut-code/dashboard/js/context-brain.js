// Context graph (mockup): Ordubashy's right-pane view of the Global Context base as a graph. Map shows every linked
// record as a dot coloured by family; Focus shows one record and the records linked to it, all named; the card under
// the graph names the record, quotes it and lists its connections. Drawn with the vendored force-graph (js/vendor).
//
// The whole graph comes from one owner read, context_overview: every record as {id, name, type, degree}, most linked
// first, and every link as {id, from, to, name}. A record's text is read with context_object when it is opened.
const CBRAIN_FAMILIES = [
  ['people', 'People', ['person']],
  ['orgs', 'Organizations', ['organization', 'company']],
  ['topics', 'Topics', ['topic', 'business_topic', 'business_opportunity']],
  ['projects', 'Projects', ['project']],
  ['agents', 'Agents', ['agent']],
  ['materials', 'Materials', ['note', 'document', 'table', 'directory']],
  ['other', 'Other', []],
];
const CBRAIN_PREFS = 'cbrain:prefs:v2';
const CBRAIN_REFRESH_MS = 60000;
const CBRAIN_EASE_MS = 320;
const cbrainFamily = type => (CBRAIN_FAMILIES.find(([, , types]) => types.includes(type)) || CBRAIN_FAMILIES[CBRAIN_FAMILIES.length - 1])[0];
const cbrainFamilyIndex = key => CBRAIN_FAMILIES.findIndex(([family]) => family === key);
const cbrainFirst = (attributes, key) => Array.isArray(attributes?.[key]) && attributes[key].length ? String(attributes[key][0]) : '';
const cbrainPlain = text => String(text || '').replace(/^#+\s*/gm, '').replace(/[*_`>]/g, '').replace(/\s+/g, ' ').trim();
const cbrainTypeLabel = type => String(type || 'record').replace(/_/g, ' ');
// A short label: cut at a word boundary with an ellipsis.
function cbrainShort(text, max = 28) {
  text = String(text || '');
  if (text.length <= max) return text;
  const cut = text.slice(0, max - 1), space = cut.lastIndexOf(' ');
  return (space > max * 0.5 ? cut.slice(0, space) : cut).replace(/[\s,.;:–-]+$/, '') + '…';
}
// An excerpt that ends on a sentence (or failing that a word), never mid-word or mid-date.
function cbrainExcerpt(text, max = 300) {
  if (text.length <= max) return {text, cut: false};
  const head = text.slice(0, max);
  const sentence = Math.max(...['. ', '! ', '? '].map(mark => head.lastIndexOf(mark)));
  if (sentence > max * 0.45) return {text: head.slice(0, sentence + 1), cut: true};
  return {text: cbrainShort(head, max), cut: true};
}

// The graph read once per page session: reopening the tab, or switching agents and back, draws at once; a copy older
// than a minute is read again in the background and replaces it from the next opening only if Context changed.
let cbrainCache = null, cbrainRefreshing = null;

function cbrainStore() {
  const nodes = new Map(), edges = new Map(), adjacency = new Map(), details = new Map();
  const addNode = item => {
    if (!item || typeof item.id !== 'string' || nodes.has(item.id)) return;
    const type = String(item.type || ''), name = String(item.name || '').trim() || `Unnamed ${cbrainTypeLabel(type)}`;
    nodes.set(item.id, {id: item.id, ref: `node:${item.id}`, name, label: cbrainShort(name), type, family: cbrainFamily(type),
      text: null, count: Number(item.degree) || 0, order: nodes.size});
  };
  const addEdge = item => {
    if (!item || typeof item.id !== 'string' || edges.has(item.id)) return;
    const name = String(item.name || '').replace(/_/g, ' ').trim() || 'related';
    edges.set(item.id, {id: item.id, source: item.from, target: item.to, name, kind: String(item.tag || '').replace(/_/g, ' ').trim() || name});
  };
  const rebuild = () => {
    adjacency.clear();
    for (const id of nodes.keys()) adjacency.set(id, []);
    for (const edge of edges.values()) {
      if (!nodes.has(edge.source) || !nodes.has(edge.target)) continue;
      adjacency.get(edge.source).push({edge, other: edge.target});
      if (edge.target !== edge.source) adjacency.get(edge.target).push({edge, other: edge.source});
    }
    for (const node of nodes.values()) node.degree = Math.max(adjacency.get(node.id).length, node.count);
    [...nodes.values()].sort((a, b) => b.degree - a.degree || a.order - b.order).forEach((node, rank) => { node.rank = rank; });
  };
  return {nodes, edges, adjacency, details, addNode, addEdge, rebuild, totals: null, revision: null, at: 0, reads: 0};
}

// Reads the whole graph into a fresh store with one request.
async function cbrainLoad(signal) {
  const data = coreData(await pageReadPost('/root/operations/context_overview', {}, signal));
  if (data?.status !== 'ready') {
    const error = new Error(data?.status === 'not_initialized' ? 'Context is not set up yet.' : 'Context can’t be read right now.');
    error.plain = true; throw error;
  }
  const store = cbrainStore();
  store.totals = data.totals; store.revision = data.revision; store.truncated = !!data.truncated;
  for (const item of data.nodes || []) store.addNode(item);
  for (const item of data.edges || []) store.addEdge(item);
  store.rebuild();
  store.at = Date.now();
  store.reads = 1;
  return store;
}
// One record's text (definition, else summary, else body), read once per revision. Null when it can't be read.
async function cbrainDetail(store, id, signal) {
  if (store.details.has(id)) return store.details.get(id);
  const pending = pageReadPost('/root/operations/context_object', {ref: `node:${id}`, history_limit: 1}, signal).then(coreData)
    .then(data => {
      if (data?.status !== 'ready' || !data.object) return null;
      const a = data.object.attributes || {};
      return cbrainPlain(cbrainFirst(a, 'definition') || data.object.summary || cbrainFirst(a, 'body'));
    })
    .catch(error => { store.details.delete(id); throw error; });
  store.details.set(id, pending);
  return pending;
}

function contextBrainMount(host, {isCurrent = () => host.isConnected} = {}) {
  const reads = new AbortController(), mountedAt = performance.now();
  const disposers = [];
  let prefs = {mode: 'map', hidden: [], unlinked: true, card: true, last: null};
  try { prefs = {...prefs, ...JSON.parse(ls(CBRAIN_PREFS) || '{}')}; } catch (_) { /* defaults */ }
  prefs.mode = 'map';   // the tab always opens on the Map; Focus reopens the last record
  const save = () => ls(CBRAIN_PREFS, JSON.stringify({hidden: prefs.hidden, unlinked: prefs.unlinked, card: prefs.card, last: prefs.last}));

  host.innerHTML = `<section class="cbrain" aria-label="Context graph" data-mode="map">
    <header class="cbrain-head">
      <button type="button" class="cbrain-icon cbrain-back" aria-label="Back to the previous record" title="Back" hidden><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg></button>
      <div class="cbrain-search">${searchField({placeholder: "Search context", label: "Search context"})}<div class="cbrain-results" role="listbox" hidden></div></div>
      <div class="cbrain-seg" role="group" aria-label="View"><button type="button" class="cbrain-segb" data-mode="map" aria-pressed="true">Map</button><button type="button" class="cbrain-segb" data-mode="focus" aria-pressed="false">Focus</button></div>
    </header>
    <div class="cbrain-filters" role="group" aria-label="Families"></div>
    <div class="cbrain-stage"><div class="cbrain-canvas"></div><p class="cbrain-status" role="status">Loading context…</p>
      <div class="cbrain-tip" hidden></div>
      <button type="button" class="cbrain-icon cbrain-fit" aria-label="Fit to view" title="Fit to view" hidden><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg></button></div>
    <aside class="cbrain-card" hidden></aside>
  </section>`;
  const $q = selector => host.querySelector(selector);
  const root = $q('.cbrain'), stage = $q('.cbrain-stage'), canvasHost = $q('.cbrain-canvas'), status = $q('.cbrain-status');
  const search = $q('.cbrain-search input'), results = $q('.cbrain-results'), card = $q('.cbrain-card'), backButton = $q('.cbrain-back');
  const tip = $q('.cbrain-tip'), fitButton = $q('.cbrain-fit');

  let store = null, graph = null, palette = {}, selected = null, trail = [], hover = null, hoverLink = null;
  let fitPending = false, dead = false, focusInfo = {shown: 0, total: 0}, cardMore = false, openGroups = new Set(), tweenFrame = 0;
  let pointer = {x: 0, y: 0};
  const live = () => !dead && isCurrent();
  // The canvas is sized from the stage's box (the pane may be scaled with CSS zoom), so the layout uses the same box.
  const size = () => { const box = stage.getBoundingClientRect(); return {w: box.width || 560, h: box.height || 520}; };

  // ---- palette: read from CSS so the light and dark themes both apply
  const readPalette = () => {
    const style = getComputedStyle(root), v = name => style.getPropertyValue(name).trim();
    palette = {fg: v('--fg'), fg2: v('--fg-2'), fg3: v('--fg-3'), bg: v('--bg'), line: v('--cb-link'), lineHi: v('--cb-link-hi'),
      lineFaint: v('--cb-link-faint'), ring: v('--cb-ring'), sans: v('--sans') || 'Inter, sans-serif',
      family: Object.fromEntries(CBRAIN_FAMILIES.map(([key]) => [key, v(`--cb-${key}`)]))};
  };
  const themeWatch = new MutationObserver(() => { readPalette(); paintFilters(); redraw(); });
  themeWatch.observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
  disposers.push(() => themeWatch.disconnect());

  // ---- what is shown
  const familyShown = node => !prefs.hidden.includes(node.family);
  const linksOf = id => store.adjacency.get(id) || [];
  function buildMap() {
    const keep = [...store.nodes.values()].filter(node => familyShown(node) && (!prefs.unlinked || linksOf(node.id).length));
    const ids = new Set(keep.map(node => node.id));
    return {nodes: keep.map(node => (node.map ||= {id: node.id, base: node})),
      links: [...store.edges.values()].filter(edge => ids.has(edge.source) && ids.has(edge.target) && edge.source !== edge.target)
        .map(edge => ({source: edge.source, target: edge.target, edge}))};
  }

  // ---- Focus geometry. Graph units equal screen pixels in Focus (zoom 1, centred on the record), so the layout is
  // computed for the pane's real size: neighbours in two columns that bow outward, evenly spaced to the pane's height,
  // labels pointing away from the middle and cut to the room they have.
  function focusGeometry(count) {
    const {w, h} = size();
    const padY = 26, labelRoom = Math.max(92, Math.min(210, w * 0.34));
    const rx = Math.max(44, Math.min(w / 2 - labelRoom - 10, 260));
    const usable = Math.max(120, h - padY * 2);
    const rows = Math.max(3, Math.floor(usable / 17));
    return {w, h, padY, labelRoom, rx, usable, rows, cap: Math.min(rows * 2, 72)};
  }
  function buildFocus() {
    const center = store.nodes.get(selected);
    const neighbours = new Map();
    for (const {other} of linksOf(center.id)) {
      if (other === center.id || neighbours.has(other)) continue;
      const node = store.nodes.get(other);
      if (familyShown(node)) neighbours.set(other, node);
    }
    const geo = focusGeometry(neighbours.size);
    const chosen = [...neighbours.values()].sort((a, b) => b.degree - a.degree).slice(0, geo.cap)
      .sort((a, b) => cbrainFamilyIndex(a.family) - cbrainFamilyIndex(b.family) || b.degree - a.degree);
    const right = chosen.slice(0, Math.ceil(chosen.length / 2)), left = chosen.slice(right.length);
    const list = [{id: center.id, base: center, level: 0, tx: 0, ty: 0, side: 0}];
    const place = (column, side) => {
      const m = column.length;
      const spacing = m > 1 ? Math.min(geo.usable / (m - 1), 46) : 0, span = spacing * (m - 1);
      column.forEach((node, index) => {
        const y = m > 1 ? -span / 2 + index * spacing : 0;
        const bow = Math.sqrt(Math.max(0, 1 - (y / (geo.usable / 2 + 24)) ** 2));
        list.push({id: node.id, base: node, level: 1, side, tx: side * geo.rx * (0.5 + 0.5 * bow), ty: y});
      });
    };
    place(right, 1); place(left, -1);
    const ids = new Set(list.map(node => node.id));
    const links = [...store.edges.values()].filter(edge => ids.has(edge.source) && ids.has(edge.target) && edge.source !== edge.target)
      .map(edge => ({source: edge.source, target: edge.target, edge, spoke: edge.source === center.id || edge.target === center.id}));
    // Two records can be linked more than once; their spoke shows one label naming every relation.
    const pairs = new Map();
    for (const link of links) if (link.spoke) {
      const key = link.source === center.id ? link.target : link.source;
      if (!pairs.has(key)) { pairs.set(key, link); link.names = []; }
      pairs.get(key).names.push(link.edge.name);
    }
    focusInfo = {shown: chosen.length, total: neighbours.size, geo};
    return {nodes: list, links};
  }

  // ---- drawing
  const radius = node => prefs.mode === 'focus'
    ? (node.level === 0 ? 8 : 3.4 + Math.min(3, Math.sqrt(node.base.degree) * 0.45))
    : 1.8 + Math.min(6.5, Math.sqrt(node.base.degree) * 0.72);
  const neighbourSet = id => new Set([id, ...linksOf(id).map(({other}) => other)]);
  let lit = null;
  const setHover = node => {
    hover = node; lit = node ? neighbourSet(node.id) : null;
    canvasHost.style.cursor = node ? 'pointer' : '';
    showTip();
    redraw();
  };
  // force-graph stops repainting once the layout is still; hover and easing ask for a frame this way.
  const redraw = () => { if (graph) graph.zoom(graph.zoom()); };
  const nodeColour = node => palette.family[node.base.family] || palette.fg2;
  function drawNode(node, ctx, scale) {
    const r = radius(node), focus = prefs.mode === 'focus';
    const dim = lit && !lit.has(node.id);
    ctx.globalAlpha = dim ? (focus ? 0.28 : 0.12) : 1;
    if (node.id === selected && focus) {
      ctx.beginPath(); ctx.arc(node.x, node.y, r + 5, 0, 2 * Math.PI); ctx.fillStyle = palette.ring; ctx.fill();
    }
    ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
    ctx.fillStyle = nodeColour(node); ctx.fill();
    if ((node.id === selected && focus) || node === hover) {
      ctx.lineWidth = 1.5 / scale; ctx.strokeStyle = palette.fg; ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }

  // Labels are drawn after the frame, in priority order, so a label never lands on another label or on a large dot.
  const font = (weight, px, scale) => `${weight} ${px / scale}px ${palette.sans}`;
  function fitText(ctx, text, max) {
    if (ctx.measureText(text).width <= max) return text;
    let lo = 1, hi = text.length;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (ctx.measureText(cbrainShort(text, mid)).width <= max) lo = mid; else hi = mid - 1; }
    return cbrainShort(text, Math.max(4, lo));
  }
  const halo = (ctx, text, x, y, scale) => {
    ctx.lineWidth = 3.5 / scale; ctx.strokeStyle = palette.bg; ctx.lineJoin = 'round'; ctx.strokeText(text, x, y);
  };
  const overlaps = (box, boxes) => boxes.some(b => box.x < b.x + b.w && box.x + box.w > b.x && box.y < b.y + b.h && box.y + box.h > b.y);
  function drawLabels(ctx, scale) {
    if (!graph) return;
    const data = graph.graphData();
    if (prefs.mode === 'focus') return drawFocusLabels(ctx, scale, data);
    // Map: the hovered record and its neighbours first, then hubs by rank; everything once zoomed in far enough.
    const many = scale > 1.7, hubCount = size().w < 480 ? 7 : 12;
    let candidates = data.nodes.filter(node => node.x !== undefined && (node === hover || (lit ? lit.has(node.id) : (many || node.base.rank < hubCount))));
    candidates.sort((a, b) => (b === hover) - (a === hover) || a.base.rank - b.base.rank);
    candidates = candidates.slice(0, lit ? 15 : many ? 80 : hubCount);
    const boxes = [];
    // Large dots are obstacles too, so a hub's label never sits on another hub.
    for (const node of data.nodes) if (node.base.rank < 60 && node.x !== undefined) {
      const r = radius(node) + 1 / scale; boxes.push({x: node.x - r, y: node.y - r, w: 2 * r, h: 2 * r, id: node.id});
    }
    const px = 11, gap = 4 / scale;
    for (const node of candidates) {
      const hot = node === hover;
      ctx.font = font(hot ? 600 : 500, px, scale);
      const text = fitText(ctx, hot ? node.base.name : node.base.label, (hot ? 260 : 150) / scale);
      const tw = ctx.measureText(text).width, th = (px + 2) / scale, r = radius(node);
      const spots = [
        {x: node.x - tw / 2, y: node.y + r + gap, align: 'center', tx: node.x, ty: node.y + r + gap},
        {x: node.x - tw / 2, y: node.y - r - gap - th, align: 'center', tx: node.x, ty: node.y - r - gap - th},
        {x: node.x + r + gap, y: node.y - th / 2, align: 'left', tx: node.x + r + gap, ty: node.y - th / 2},
        {x: node.x - r - gap - tw, y: node.y - th / 2, align: 'right', tx: node.x - r - gap, ty: node.y - th / 2},
      ];
      const others = boxes.filter(b => b.id !== node.id);
      const spot = spots.find(s => !overlaps({x: s.x, y: s.y, w: tw, h: th}, others)) || (hot ? spots[0] : null);
      if (!spot) continue;
      boxes.push({x: spot.x - 2 / scale, y: spot.y, w: tw + 4 / scale, h: th});
      ctx.textAlign = spot.align; ctx.textBaseline = 'top';
      halo(ctx, text, spot.tx, spot.ty, scale);
      ctx.fillStyle = hot || node.base.rank < 5 ? palette.fg : palette.fg2;
      ctx.fillText(text, spot.tx, spot.ty);
    }
  }
  function drawFocusLabels(ctx, scale, data) {
    const geo = focusInfo.geo;
    if (!geo) return;
    for (const node of data.nodes) {
      if (node.x === undefined) continue;
      const r = radius(node), hot = node === hover;
      if (node.level === 0) {
        ctx.font = font(600, 12.5, scale);
        const text = fitText(ctx, node.base.name, Math.max(120, geo.rx * 1.5) / scale);
        ctx.textAlign = 'center'; ctx.textBaseline = 'top';
        const y = node.y + r + 9 / scale;
        halo(ctx, text, node.x, y, scale); ctx.fillStyle = palette.fg; ctx.fillText(text, node.x, y);
        continue;
      }
      const dim = lit && !lit.has(node.id);
      ctx.globalAlpha = dim ? 0.35 : 1;
      ctx.font = font(hot ? 600 : 450, 11.5, scale);
      const x = node.x + node.side * (r + 7 / scale);
      // Hover shows the whole name in the tooltip; the label keeps to its column.
      const text = fitText(ctx, node.base.name, (geo.labelRoom - r - 12) / scale);
      ctx.textAlign = node.side > 0 ? 'left' : 'right'; ctx.textBaseline = 'middle';
      halo(ctx, text, x, node.y, scale);
      ctx.fillStyle = hot ? palette.fg : palette.fg2;
      if (!lit && node.base.degree > 8) ctx.fillStyle = palette.fg;
      ctx.fillText(text, x, node.y);
      ctx.globalAlpha = 1;
    }
  }
  const linkTouches = (link, node) => node && (link.source.id === node.id || link.target.id === node.id);
  const linkHot = link => link === hoverLink || (hover && linkTouches(link, hover) && (prefs.mode === 'map' || link.spoke || hover.id === selected));
  // In Focus only the record's own links are drawn; links between neighbours appear faintly while one of them is hovered.
  const linkVisible = link => prefs.mode === 'map' || link.spoke || (hover && hover.id !== selected && linkTouches(link, hover));
  function drawLinkLabel(link, ctx, scale) {
    const focusSpoke = prefs.mode === 'focus' && link.names && hover && hover.id !== selected && linkTouches(link, hover);
    if (link !== hoverLink && !focusSpoke) return;
    const {source: a, target: b} = link;
    if (!a || !b || a.x === undefined) return;
    const x = (a.x + b.x) / 2, y = (a.y + b.y) / 2;
    ctx.font = font(500, 10.5, scale);
    const names = focusSpoke ? [...new Set(link.names)] : [link.edge.name];
    const text = fitText(ctx, names[0], 220 / scale) + (names.length > 1 ? `  +${names.length - 1}` : '');
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    const w = ctx.measureText(text).width + 12 / scale, h = 18 / scale;
    ctx.fillStyle = palette.bg; ctx.strokeStyle = palette.line; ctx.lineWidth = 1 / scale;
    ctx.beginPath(); ctx.roundRect(x - w / 2, y - h / 2, w, h, 5 / scale); ctx.fill(); ctx.stroke();
    ctx.fillStyle = palette.fg; ctx.fillText(text, x, y);
  }

  // A small tooltip with the whole name and type, beside the pointer.
  function showTip() {
    if (!hover) { tip.hidden = true; return; }
    tip.innerHTML = `<strong>${esc(hover.base.name)}</strong><span><em>${esc(cbrainTypeLabel(hover.base.type))}</em> · ${hover.base.degree} ${hover.base.degree === 1 ? 'link' : 'links'}</span>`;
    tip.hidden = false;
    placeTip();
  }
  function placeTip() {
    if (tip.hidden) return;
    const w = stage.clientWidth, h = stage.clientHeight, tw = tip.offsetWidth, th = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(w - tw - 8, pointer.x + 14)) + 'px';
    tip.style.top = Math.max(8, Math.min(h - th - 8, pointer.y + 16)) + 'px';
  }
  // Pointer in the stage's own CSS pixels (the pane is drawn with CSS zoom).
  const onMove = event => {
    const box = stage.getBoundingClientRect(), f = box.width / (stage.clientWidth || box.width) || 1;
    pointer = {x: (event.clientX - box.left) / f, y: (event.clientY - box.top) / f}; placeTip();
  };
  stage.addEventListener('pointermove', onMove);
  disposers.push(() => stage.removeEventListener('pointermove', onMove));

  // ---- view
  let userMoved = false;
  const fitMap = (ms = 450) => {
    if (!graph) return;
    userMoved = false;
    graph.zoomToFit(ms, 28);
    setTimeout(() => { if (graph && prefs.mode === 'map' && graph.zoom() > 2.2) graph.zoom(2.2, 200); }, ms + 20);
  };
  const fitFocus = (ms = 0) => { if (graph) { graph.centerAt(0, 0, ms); graph.zoom(1, ms); } };
  const fit = () => prefs.mode === 'map' ? fitMap() : fitFocus(300);

  // A weak pull to the middle keeps small islands (pairs, little stars) from drifting to the edge of the map.
  let gravityNodes = [];
  const gravity = Object.assign(alpha => { for (const node of gravityNodes) { node.vx -= node.x * 0.07 * alpha; node.vy -= node.y * 0.07 * alpha; } },
    {initialize: list => { gravityNodes = list; }});
  function start() {
    readPalette();
    paintFilters();
    graph = ForceGraph()(canvasHost);
    root.__cbrainGraph = graph;   // mockup: lets the browser check read the drawn data
    graph
      .backgroundColor('rgba(0,0,0,0)')
      .nodeId('id')
      .nodeLabel(() => '')
      .nodeCanvasObject(drawNode)
      .nodePointerAreaPaint((node, color, ctx) => { ctx.fillStyle = color; ctx.beginPath(); ctx.arc(node.x, node.y, radius(node) + 4, 0, 2 * Math.PI); ctx.fill(); })
      .linkVisibility(linkVisible)
      .linkColor(link => linkHot(link) ? palette.lineHi : prefs.mode === 'focus' && !link.spoke ? palette.lineFaint : (lit && prefs.mode === 'map' ? 'rgba(0,0,0,0)' : palette.line))
      .linkWidth(link => linkHot(link) ? 1.3 : prefs.mode === 'focus' ? 0.9 : 0.5)
      .linkCanvasObjectMode(() => 'after')
      .linkCanvasObject(drawLinkLabel)
      .linkHoverPrecision(3)
      .linkLabel(() => '')
      .onRenderFramePost(drawLabels)
      .onNodeHover(node => setHover(node || null))
      .onLinkHover(link => { hoverLink = link || null; redraw(); })
      .onNodeClick(node => { if (prefs.mode === 'focus' && node.id === selected) return; focusOn(node.id); })
      .onEngineStop(() => { if (fitPending && prefs.mode === 'map') { fitPending = false; fitMap(); } })
      .cooldownTime(3500)
      .minZoom(0.2).maxZoom(8);
    let lastSize = '';
    const resize = () => {
      const box = stage.getBoundingClientRect();
      if (!box.width || !box.height) return;
      const key = `${Math.round(box.width)}x${Math.round(box.height)}`;
      if (key === lastSize) return;
      lastSize = key;
      graph.width(box.width).height(box.height);
      // The new size reaches the renderer on its next update, which rescales the view; lay Focus out after that.
      if (prefs.mode === 'focus' && selected) { clearTimeout(refocus); refocus = setTimeout(() => { if (graph && prefs.mode === 'focus' && selected) paintFocus({ease: false}); }, 40); }
      else { clearTimeout(refit); refit = setTimeout(() => { if (graph && prefs.mode === 'map' && !userMoved) fitMap(200); }, 120); }
    };
    let refit = 0, refocus = 0;
    // A Map the owner has zoomed or dragged keeps its view when the pane is resized.
    const moved = () => { if (prefs.mode === 'map') userMoved = true; };
    canvasHost.addEventListener('wheel', moved, {passive: true});
    canvasHost.addEventListener('pointerdown', moved);
    disposers.push(() => { canvasHost.removeEventListener('wheel', moved); canvasHost.removeEventListener('pointerdown', moved); });
    const observer = new ResizeObserver(resize);
    observer.observe(stage); resize();
    disposers.push(() => observer.disconnect());
    fitButton.hidden = false;
    showMap();
  }
  // The first Map of a session grows in: the most linked records first, a batch per frame, each new record placed beside
  // a record it links to, so the structure shows at once and fills in while the layout settles.
  const CBRAIN_BATCH = 150;
  let growFrame = 0;
  const endOf = end => end?.id ?? end;
  function showMap() {
    cancelAnimationFrame(tweenFrame); cancelAnimationFrame(growFrame);
    prefs.mode = 'map'; trail = []; syncControls();
    graph.d3Force('radial', gravity);
    graph.d3Force('charge').strength(-26).distanceMax(260);
    graph.d3Force('link').distance(26).strength(link => 1 / Math.min(linksOf(endOf(link.source)).length || 1, linksOf(endOf(link.target)).length || 1));
    const data = buildMap();
    for (const node of data.nodes) { delete node.fx; delete node.fy; }
    setHover(null);
    paintCard();
    status.hidden = data.nodes.length > 0; status.textContent = 'No records match these filters.';
    // A layout already settled this session is drawn as it was.
    if (!data.nodes.some(node => node.x === undefined)) {
      fitPending = false;
      graph.cooldownTicks(0).warmupTicks(0).graphData(data);
      fitMap(0);
      return;
    }
    const order = [...data.nodes].sort((a, b) => a.base.rank - b.base.rank), shown = [], placed = new Set();
    let next = 0;
    const grow = () => {
      if (!graph || prefs.mode !== 'map') return;
      const batch = order.slice(next, next + CBRAIN_BATCH), first = next === 0;
      next += batch.length;
      for (const node of batch) {
        if (node.x === undefined) {
          const anchor = linksOf(node.id).map(({other}) => store.nodes.get(other)?.map).find(other => other && placed.has(other.id) && other.x !== undefined);
          const angle = Math.random() * 2 * Math.PI, spread = anchor ? 14 + Math.random() * 18 : 30 + Math.sqrt(shown.length) * 9;
          node.x = (anchor?.x || 0) + Math.cos(angle) * spread; node.y = (anchor?.y || 0) + Math.sin(angle) * spread;
          node.vx = node.vy = 0;
        }
        placed.add(node.id); shown.push(node);
      }
      const links = data.links.filter(link => placed.has(endOf(link.source)) && placed.has(endOf(link.target)));
      graph.cooldownTicks(Infinity).warmupTicks(first ? 40 : 6).graphData({nodes: shown.slice(), links});
      root.dataset.drawn = String(shown.length);
      if (first) {
        root.dataset.firstDrawMs = String(Math.round(performance.now() - mountedAt));
        // The rest grows outward from the hubs: start a little wider than the hubs alone need.
        graph.zoomToFit(0, 28); graph.zoom(graph.zoom() * 0.6, 0);
      }
      if (next < order.length) { if (!first) graph.zoomToFit(220, 28); growFrame = requestAnimationFrame(grow); }
      else { fitPending = true; graph.zoomToFit(260, 28); root.dataset.fullDrawMs = String(Math.round(performance.now() - mountedAt)); }
    };
    grow();
  }
  async function focusOn(id, {push = true} = {}) {
    if (!store.nodes.has(id)) return;
    const wasFocus = prefs.mode === 'focus';
    if (push && wasFocus && selected && selected !== id) trail.push(selected);
    if (id !== selected) { cardMore = false; openGroups = new Set(); }
    selected = id; prefs.mode = 'focus'; prefs.last = id; save(); syncControls(); setHover(null);
    status.hidden = true;
    paintFocus({ease: true});
    paintCard();
    loadText(id);
  }
  // Focus is laid out, not simulated. Records ease from where they were drawn (on the Map or the previous Focus) to their
  // new places; new ones grow out of the record that was clicked.
  function paintFocus({ease}) {
    cancelAnimationFrame(tweenFrame);
    const before = new Map();
    if (graph && ease) {
      const data = graph.graphData(), mapMode = !data.nodes.some(node => node.level !== undefined);
      const {w, h} = size();
      for (const node of data.nodes) {
        if (node.x === undefined) continue;
        if (mapMode) { const p = graph.graph2ScreenCoords(node.x, node.y); before.set(node.id, {x: p.x - w / 2, y: p.y - h / 2}); }
        else before.set(node.id, {x: node.x, y: node.y});
      }
    }
    const data = buildFocus();
    const origin = before.get(selected) || {x: 0, y: 0};
    for (const node of data.nodes) {
      const from = ease ? (before.get(node.id) || origin) : {x: node.tx, y: node.ty};
      node.sx = from.x; node.sy = from.y; node.x = node.fx = from.x; node.y = node.fy = from.y;
    }
    graph.d3Force('radial', null);
    graph.cooldownTicks(0).warmupTicks(0).graphData(data);
    fitFocus(0);
    const t0 = performance.now();
    const step = now => {
      const k = Math.min(1, (now - t0) / CBRAIN_EASE_MS), e = 1 - (1 - k) ** 3;
      for (const node of data.nodes) { node.x = node.fx = node.sx + (node.tx - node.sx) * e; node.y = node.fy = node.sy + (node.ty - node.sy) * e; }
      redraw();
      if (k < 1 && live()) tweenFrame = requestAnimationFrame(step);
    };
    tweenFrame = requestAnimationFrame(step);
    root.dataset.focusShown = String(focusInfo.shown);
  }

  // ---- controls
  function syncControls() {
    root.dataset.mode = prefs.mode;
    host.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === prefs.mode)));
    backButton.hidden = !(prefs.mode === 'focus' && trail.length);
    fitButton.title = fitButton.ariaLabel = prefs.mode === 'focus' ? 'Recentre' : 'Fit to view';
  }
  function paintFilters() {
    if (!store) return;
    const counts = Object.fromEntries(CBRAIN_FAMILIES.map(([key]) => [key, 0]));
    let unlinked = 0;
    for (const node of store.nodes.values()) {
      const linked = linksOf(node.id).length > 0;
      if (!linked) unlinked++;
      if (!prefs.unlinked || linked) counts[node.family]++;
    }
    $q('.cbrain-filters').innerHTML = CBRAIN_FAMILIES.filter(([key]) => counts[key]).map(([key, label]) =>
      `<button type="button" class="cbrain-chip" data-family="${key}" aria-pressed="${!prefs.hidden.includes(key)}" title="${prefs.hidden.includes(key) ? 'Show' : 'Hide'} ${esc(label.toLowerCase())}"><i style="--dot:var(--cb-${key})"></i>${label}<span>${counts[key]}</span></button>`).join('')
      + `<button type="button" class="cbrain-chip cbrain-unlinked" aria-pressed="${!prefs.unlinked}" title="${prefs.unlinked ? 'Show' : 'Hide'} records with no links">Unlinked<span>${unlinked}</span></button>`;
  }
  $q('.cbrain-filters').onclick = event => {
    const chip = event.target.closest('.cbrain-chip');
    if (!chip || !graph) return;
    if (chip.classList.contains('cbrain-unlinked')) prefs.unlinked = !prefs.unlinked;
    else prefs.hidden = prefs.hidden.includes(chip.dataset.family) ? prefs.hidden.filter(key => key !== chip.dataset.family) : [...prefs.hidden, chip.dataset.family];
    save(); paintFilters();
    if (prefs.mode === 'map') showMap(); else { paintFocus({ease: true}); paintCard(); }
  };
  // Focus always has a record: the one selected, the last one opened, or the most linked record shown.
  const focusDefault = () => {
    if (selected && store.nodes.has(selected)) return selected;
    if (prefs.last && store.nodes.has(prefs.last)) return prefs.last;
    return [...store.nodes.values()].filter(familyShown).sort((a, b) => b.degree - a.degree)[0]?.id;
  };
  host.querySelectorAll('[data-mode]').forEach(button => button.onclick = () => {
    if (!graph || button.dataset.mode === prefs.mode) return;
    if (button.dataset.mode === 'map') showMap(); else { const id = focusDefault(); if (id) focusOn(id, {push: false}); }
  });
  backButton.onclick = () => { const id = trail.pop(); if (id) focusOn(id, {push: false}); };
  fitButton.onclick = () => fit();

  // ---- search
  let matches = [], active = 0;
  const findMatches = query => {
    const q = query.trim().toLocaleLowerCase();
    if (!q || !store) return [];
    const score = node => {
      const name = node.name.toLocaleLowerCase();
      if (name === q) return 0;
      if (name.startsWith(q)) return 1;
      if (name.split(/\s+/).some(word => word.startsWith(q))) return 2;
      if (name.includes(q)) return 3;
      return 9;
    };
    return [...store.nodes.values()].map(node => [score(node), node]).filter(([s]) => s < 9)
      .sort((a, b) => a[0] - b[0] || b[1].degree - a[1].degree).slice(0, 8).map(([, node]) => node);
  };
  const paintResults = () => {
    const query = search.value.trim();
    results.hidden = !query || document.activeElement !== search;
    results.innerHTML = matches.length ? matches.map((node, index) => `<button type="button" role="option" class="cbrain-result" data-id="${esc(node.id)}" aria-selected="${index === active}" title="${esc(node.name)}">
      <i style="--dot:var(--cb-${node.family})"></i><span>${esc(node.name)}</span><small>${esc(cbrainTypeLabel(node.type))} · ${node.degree} ${node.degree === 1 ? 'link' : 'links'}</small></button>`).join('')
      : `<p class="cbrain-noresult">No records match.</p>`;
  };
  const pick = id => { search.value = ''; matches = []; search.blur(); paintResults(); focusOn(id); };
  search.oninput = () => { matches = findMatches(search.value); active = 0; paintResults(); };
  search.onfocus = () => { matches = findMatches(search.value); paintResults(); };
  search.onkeydown = event => {
    if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && matches.length) {
      event.preventDefault(); active = (active + (event.key === 'ArrowDown' ? 1 : matches.length - 1)) % matches.length; paintResults();
    }
    else if (event.key === 'Enter' && matches[active]) { event.preventDefault(); pick(matches[active].id); }
    else if (event.key === 'Escape') { search.value = ''; matches = []; search.blur(); paintResults(); }
  };
  results.onmousedown = event => { const row = event.target.closest('[data-id]'); event.preventDefault(); if (row) pick(row.dataset.id); };
  search.onblur = () => setTimeout(() => { if (live()) paintResults(); }, 100);

  // ---- the card. Header and connections come from the overview at once; the text is read when the record is opened.
  function loadText(id) {
    const node = store.nodes.get(id);
    if (!node || node.text !== null) return;
    node.textFailed = false;
    cbrainDetail(store, id, reads.signal).then(text => {
      node.text = text ?? null; node.textFailed = text == null;
      if (live() && selected === id) paintCard();
    }).catch(error => {
      if (error?.name === 'AbortError') return;
      node.textFailed = true;
      if (live() && selected === id) paintCard();
    });
  }
  function paintCard() {
    const node = selected && prefs.mode === 'focus' && store.nodes.get(selected);
    card.hidden = !node;
    root.classList.toggle('cbrain-has-card', !!node);
    if (!node) return;
    const groups = new Map();
    for (const {edge, other} of linksOf(node.id)) {
      if (other === node.id) continue;
      if (!groups.has(edge.kind)) groups.set(edge.kind, []);
      if (!groups.get(edge.kind).includes(other)) groups.get(edge.kind).push(other);
    }
    const sorted = [...groups].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
    const links = new Set(linksOf(node.id).map(({other}) => other).filter(id => id !== node.id)).size, excerpt = cbrainExcerpt(node.text || '');
    const shownNote = focusInfo.shown < focusInfo.total ? `<span class="cbrain-note">${focusInfo.shown} drawn · ${focusInfo.total - focusInfo.shown} more</span>` : '';
    card.classList.toggle('collapsed', !prefs.card);
    card.innerHTML = `<header class="cbrain-card-head">
        <i class="cbrain-dot" style="--dot:var(--cb-${node.family})"></i><h3 title="${esc(node.name)}">${esc(node.name)}</h3>
        <span class="cbrain-pill">${esc(cbrainTypeLabel(node.type))}</span>
        <span class="cbrain-count">${links} ${links === 1 ? 'link' : 'links'}</span>
        <button type="button" class="cbrain-icon cbrain-card-toggle" aria-expanded="${prefs.card}" aria-label="${prefs.card ? 'Collapse details' : 'Expand details'}" title="${prefs.card ? 'Collapse' : 'Expand'}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></button>
      </header>
      <div class="cbrain-card-body">
        ${node.text === null ? `<p class="cbrain-text muted cbrain-text-wait">${node.textFailed ? 'Description couldn’t be read.' : 'Reading description…'}</p>`
          : node.text ? `<p class="cbrain-text${cardMore ? ' open' : ''}">${esc(cardMore ? node.text : excerpt.text)}${excerpt.cut ? ` <button type="button" class="cbrain-more-text">${cardMore ? 'Less' : 'More'}</button>` : ''}</p>`
          : '<p class="cbrain-text muted">No description yet.</p>'}
        ${shownNote ? `<p class="cbrain-meta">${shownNote}</p>` : ''}
        ${sorted.length ? `<div class="cbrain-groups">${sorted.map(([label, ids]) => {
          const open = openGroups.has(label), list = open ? ids : ids.slice(0, 5);
          return `<div class="cbrain-group"><span class="cbrain-rel" title="${esc(label)}">${esc(cbrainShort(label, 22))}</span><div class="cbrain-links">
            ${list.map(id => { const other = store.nodes.get(id); return `<button type="button" class="cbrain-link" data-id="${esc(id)}" title="${esc(other.name)}"><i style="--dot:var(--cb-${other.family})"></i><span>${esc(other.label)}</span></button>`; }).join('')}
            ${ids.length > 5 ? `<button type="button" class="cbrain-link cbrain-link-more" data-group="${esc(label)}">${open ? 'Fewer' : `+${ids.length - 5}`}</button>` : ''}</div></div>`;
        }).join('')}</div>` : ''}
      </div>`;
    card.querySelector('.cbrain-card-toggle').onclick = () => { prefs.card = !prefs.card; save(); paintCard(); };
    card.querySelector('.cbrain-more-text')?.addEventListener('click', () => { cardMore = !cardMore; paintCard(); });
    card.querySelectorAll('.cbrain-link[data-id]').forEach(button => button.onclick = () => focusOn(button.dataset.id));
    card.querySelectorAll('[data-group]').forEach(button => button.onclick = () => {
      const label = button.dataset.group; if (openGroups.has(label)) openGroups.delete(label); else openGroups.add(label); paintCard();
    });
  }

  // ---- data: the session copy if there is one, otherwise read now
  const begin = loaded => {
    if (!live()) return;
    store = loaded;
    root.dataset.reads = String(store.reads || 0);
    status.hidden = true;
    start();
  };
  const refresh = () => {
    if (cbrainRefreshing) return cbrainRefreshing;
    cbrainRefreshing = cbrainLoad(new AbortController().signal)
      // Context unchanged: keep the copy (and its settled layout), only mark it fresh.
      .then(next => { if (!cbrainCache || next.revision !== cbrainCache.revision) cbrainCache = next; else cbrainCache.at = next.at; return cbrainCache; })
      .finally(() => { cbrainRefreshing = null; });
    return cbrainRefreshing;
  };
  if (cbrainCache) {
    begin(cbrainCache);
    if (Date.now() - cbrainCache.at > CBRAIN_REFRESH_MS) refresh().catch(() => {});
  } else {
    (cbrainRefreshing || refresh())
      .then(loaded => {
        root.dataset.loadMs = String(Math.round(performance.now() - mountedAt));
        begin(loaded);
      })
      .catch(error => {
        if (!live() || error?.name === 'AbortError') return;
        status.hidden = false;
        status.textContent = error?.plain ? error.message : 'Couldn’t load context. Try again later.';
      });
  }
  return () => {
    dead = true;
    cancelAnimationFrame(tweenFrame); cancelAnimationFrame(growFrame);
    reads.abort();
    for (const dispose of disposers) dispose();
    if (graph) { graph.pauseAnimation(); graph._destructor?.(); }
    graph = null;
    host.replaceChildren();
  };
}
