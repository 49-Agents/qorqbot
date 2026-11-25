// Context graph (mockup): Ordubashy's right-pane view of the Global Context base as a graph. Map shows every linked
// record as a dot coloured by family; Focus shows one record and its neighbourhood in rings, all labelled; the card under
// the graph names the record, quotes it and lists its connections. Drawn with the vendored force-graph (js/vendor).
//
// MOCKUP: the overview is read by walking every context_home page (about 18 reads of 60). A real build wants one compact
// overview read (ids, names, families, edge pairs) from the Context reader instead of walking Home.
const CBRAIN_FAMILIES = [
  ['people', 'People', ['person']],
  ['orgs', 'Organizations', ['organization', 'company']],
  ['topics', 'Topics', ['topic', 'business_topic', 'business_opportunity']],
  ['projects', 'Projects', ['project']],
  ['agents', 'Agents', ['agent']],
  ['materials', 'Materials', ['note', 'document', 'table', 'directory']],
  ['other', 'Other', []],
];
const CBRAIN_PREFS = 'cbrain:prefs:v1';
const CBRAIN_RING_CAP = 44, CBRAIN_FOCUS_CAP = 90;   // labelled first ring; whole focus
const cbrainFamily = type => (CBRAIN_FAMILIES.find(([, , types]) => types.includes(type)) || CBRAIN_FAMILIES[CBRAIN_FAMILIES.length - 1])[0];
const cbrainFirst = (attributes, key) => Array.isArray(attributes?.[key]) && attributes[key].length ? String(attributes[key][0]) : '';
const cbrainPlain = text => String(text || '').replace(/^#+\s*/gm, '').replace(/[*_`>]/g, '').replace(/\s+/g, ' ').trim();
const cbrainTypeLabel = type => String(type || 'record').replace(/_/g, ' ');

function contextBrainMount(host, {isCurrent = () => host.isConnected} = {}) {
  const reads = new AbortController();
  const disposers = [];
  let prefs = {mode: 'map', depth: 1, hidden: [], unlinked: true, card: true};
  try { prefs = {...prefs, ...JSON.parse(ls(CBRAIN_PREFS) || '{}')}; } catch (_) { /* defaults */ }
  const save = () => ls(CBRAIN_PREFS, JSON.stringify({depth: prefs.depth, hidden: prefs.hidden, unlinked: prefs.unlinked, card: prefs.card, mode: prefs.mode, last: prefs.last}));

  host.innerHTML = `<section class="cbrain" aria-label="Context graph">
    <header class="cbrain-head">
      <button type="button" class="cbrain-icon cbrain-back" aria-label="Back to the previous record" hidden><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg></button>
      <div class="cbrain-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input type="search" placeholder="Search context" aria-label="Search context" autocomplete="off" spellcheck="false"><div class="cbrain-results" role="listbox" hidden></div></div>
      <div class="cbrain-seg" role="group" aria-label="View"><button type="button" class="cbrain-segb" data-mode="map">Map</button><button type="button" class="cbrain-segb" data-mode="focus">Focus</button></div>
      <div class="cbrain-seg cbrain-depth" role="group" aria-label="Depth"><button type="button" class="cbrain-segb" data-depth="1">1</button><button type="button" class="cbrain-segb" data-depth="2">2</button></div>
    </header>
    <div class="cbrain-filters" role="group" aria-label="Families"></div>
    <div class="cbrain-stage"><div class="cbrain-canvas"></div><p class="cbrain-status" role="status">Reading context…</p>
      <div class="cbrain-zoom"><button type="button" class="cbrain-icon" data-zoom="fit" aria-label="Fit to view"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg></button></div></div>
    <aside class="cbrain-card" hidden></aside>
  </section>`;
  const $q = selector => host.querySelector(selector);
  const root = $q('.cbrain'), stage = $q('.cbrain-stage'), canvasHost = $q('.cbrain-canvas'), status = $q('.cbrain-status');
  const search = $q('.cbrain-search input'), results = $q('.cbrain-results'), card = $q('.cbrain-card'), backButton = $q('.cbrain-back');

  const nodes = new Map(), edges = new Map(), adjacency = new Map();
  let totals = null, graph = null, palette = {}, selected = null, trail = [], hover = null, hoverLink = null, mapData = null;
  let fitPending = false, dead = false;
  const live = () => !dead && isCurrent();

  // ---- palette: read from CSS at paint time so the light and dark themes both apply
  const readPalette = () => {
    const style = getComputedStyle(root), v = name => style.getPropertyValue(name).trim();
    palette = {fg: v('--fg'), fg2: v('--fg-2'), fg3: v('--fg-3'), bg: v('--bg'), line: v('--cb-link'), lineHi: v('--cb-link-hi'), halo: v('--cb-halo'),
      family: Object.fromEntries(CBRAIN_FAMILIES.map(([key]) => [key, v(`--cb-${key}`)]))};
  };
  const themeWatch = new MutationObserver(() => { readPalette(); paintFilters(); });
  themeWatch.observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
  disposers.push(() => themeWatch.disconnect());

  // ---- data
  const op = (operation, args) => pageReadPost(`/root/operations/${operation}`, args, reads.signal).then(coreData);
  const addNode = item => {
    if (!item || item.kind !== 'node' || nodes.has(item.id)) return;
    const a = item.attributes || {}, type = cbrainFirst(a, 'basic_entity_type');
    nodes.set(item.id, {id: item.id, ref: item.ref, name: cbrainFirst(a, 'name') || cbrainPlain(item.summary).slice(0, 60) || 'Untitled',
      type, family: cbrainFamily(type), aliases: (a.aliases || []).map(String),
      text: cbrainPlain(cbrainFirst(a, 'definition') || item.summary || cbrainFirst(a, 'body')), count: item.relationship_count || 0});
  };
  const addEdge = rel => {
    if (!rel || rel.kind !== 'edge' || edges.has(rel.id)) return false;
    edges.set(rel.id, {id: rel.id, source: rel.from.id, target: rel.to.id, name: cbrainFirst(rel.attributes, 'name') || cbrainFirst(rel.attributes, 'tags') || 'related',
      kind: (cbrainFirst(rel.attributes, 'tags') || cbrainFirst(rel.attributes, 'name') || 'related').replace(/_/g, ' ')});
    return true;
  };
  const rebuildAdjacency = () => {
    adjacency.clear();
    for (const id of nodes.keys()) adjacency.set(id, []);
    for (const edge of edges.values()) {
      if (!nodes.has(edge.source) || !nodes.has(edge.target)) continue;
      adjacency.get(edge.source).push({edge, other: edge.target});
      if (edge.target !== edge.source) adjacency.get(edge.target).push({edge, other: edge.source});
    }
    for (const node of nodes.values()) node.degree = Math.max(adjacency.get(node.id).length, node.count);
    [...nodes.values()].sort((a, b) => b.degree - a.degree).forEach((node, rank) => { node.rank = rank; });
  };
  async function loadAll() {
    let cursor = null, pages = 0;
    const started = performance.now();
    do {
      const data = await op('context_home', {limit: 60, ...(cursor ? {cursor} : {})});
      if (!live()) return;
      if (data.status !== 'ready') {
        status.textContent = data.status === 'not_initialized' ? 'Context has not been set up yet.' : 'Context cannot be read right now.';
        return;
      }
      totals = data.totals;
      for (const item of data.nodes || []) { addNode(item); for (const rel of item.relationships || []) addEdge(rel); }
      pages++;
      status.textContent = `Reading context… ${nodes.size.toLocaleString()} of ${totals.nodes.toLocaleString()}`;
      cursor = data.page?.has_more ? data.page.next_cursor : null;
    } while (cursor && pages < 60);
    // Home pages carry at most a few relationships per record; read the rest around every record that has more.
    const truncated = [...nodes.values()].filter(node => node.count > 5).sort((a, b) => b.count - a.count);
    for (let i = 0; i < truncated.length; i += 6) {
      await Promise.all(truncated.slice(i, i + 6).map(node => expand(node.id, 1)));
      if (!live()) return;
    }
    rebuildAdjacency();
    root.dataset.reads = String(pages);
    root.dataset.loadMs = String(Math.round(performance.now() - started));
    status.hidden = true;
    start();
  }
  // All relationships around one record (depth 1 or 2), following pages.
  const expanded = new Map();
  async function expand(id, depth) {
    if ((expanded.get(id) || 0) >= depth) return false;
    const node = nodes.get(id);
    let cursor = null, added = false, guard = 0;
    do {
      const data = await op('context_expand', {ref: node.ref, depth, direction: 'both', limit: 200, ...(cursor ? {cursor} : {})});
      if (data.status !== 'ready') return added;
      for (const rel of data.relationships || []) added = addEdge(rel) || added;
      cursor = data.page?.has_more ? data.page.next_cursor : null;
    } while (cursor && ++guard < 10);
    expanded.set(id, depth);
    return added;
  }

  // ---- what is shown
  const familyShown = node => !prefs.hidden.includes(node.family);
  function buildMap() {
    const keep = [...nodes.values()].filter(node => familyShown(node) && (!prefs.unlinked || adjacency.get(node.id).length));
    const ids = new Set(keep.map(node => node.id));
    mapData = {nodes: keep.map(node => (node.map ||= {id: node.id, base: node})),
      links: [...edges.values()].filter(edge => ids.has(edge.source) && ids.has(edge.target) && edge.source !== edge.target)
        .map(edge => ({source: edge.source, target: edge.target, edge}))};
    return mapData;
  }
  function buildFocus() {
    const center = nodes.get(selected), ring = new Map([[center.id, 0]]);
    let frontier = [center.id];
    for (let level = 1; level <= prefs.depth; level++) {
      const next = new Map();
      // Depth 2 does not walk on through hubs (agents such as Ordubashy link to hundreds of records): it would only show
      // the hub's own neighbourhood again.
      for (const id of frontier) for (const {other} of (level > 1 && nodes.get(id).degree > 40 ? [] : adjacency.get(id))) {
        if (ring.has(other) || !familyShown(nodes.get(other))) continue;
        next.set(other, Math.max(next.get(other) || 0, nodes.get(other).degree));
      }
      const room = level === 1 ? CBRAIN_RING_CAP : CBRAIN_FOCUS_CAP - ring.size;
      const chosen = [...next.keys()].sort((a, b) => nodes.get(b).degree - nodes.get(a).degree).slice(0, Math.max(0, room));
      for (const id of chosen) ring.set(id, level);
      frontier = chosen;
    }
    const list = [...ring].map(([id, level]) => ({id, base: nodes.get(id), level, ...(level === 0 ? {fx: 0, fy: 0} : {})}));
    const links = [...edges.values()].filter(edge => ring.has(edge.source) && ring.has(edge.target) && edge.source !== edge.target)
      .map(edge => ({source: edge.source, target: edge.target, edge}));
    // Nodes per ring, to space each ring for its own size.
    const counts = [0, 0, 0];
    for (const level of ring.values()) counts[level]++;
    return {nodes: list, links, counts, hidden: [...adjacency.get(center.id)].filter(({other}) => !ring.has(other)).length};
  }

  // ---- drawing
  const radius = node => node.level === 0 ? 7 : prefs.mode === 'focus' ? 3 + Math.min(5, Math.sqrt(node.base.degree) * 0.8) : 1.6 + Math.min(7, Math.sqrt(node.base.degree) * 0.75);
  const neighbourSet = id => new Set([id, ...(adjacency.get(id) || []).map(({other}) => other)]);
  let lit = null;
  const setHover = node => {
    hover = node; lit = node ? neighbourSet(node.id) : null;
    canvasHost.style.cursor = node ? 'pointer' : '';
    redraw();
  };
  // force-graph pauses painting once the layout is still; a hover drawn by this view has to ask for a frame.
  const redraw = () => { if (graph) graph.zoom(graph.zoom()); };
  const shortLabel = (text, max) => text.length > max ? text.slice(0, max - 1) + '…' : text;
  function drawNode(node, ctx, scale) {
    const r = radius(node), focus = prefs.mode === 'focus';
    const dim = lit && !lit.has(node.id);
    ctx.globalAlpha = dim ? 0.13 : 1;
    if (node.id === selected || node === hover) {
      ctx.beginPath(); ctx.arc(node.x, node.y, r + 3.2, 0, 2 * Math.PI); ctx.fillStyle = palette.halo; ctx.fill();
    }
    ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
    ctx.fillStyle = palette.family[node.base.family] || palette.fg2; ctx.fill();
    if (node.id === selected) { ctx.lineWidth = 1.4 / scale; ctx.strokeStyle = palette.fg; ctx.stroke(); }
    let show;
    if (focus) {
      // When the ring is drawn small, label every k-th record so labels keep about 13px apart; hover shows the rest.
      const every = Math.ceil(13 / (ringSpacing * scale));
      show = node.level === 0 || (node.level === 1 && (every <= 1 || node.slot % every === 0)) || scale > 1.8 || node === hover || (hover && lit && lit.has(node.id));
    }
    else show = node === hover || (!dim && (node.base.rank < (stage.clientWidth < 520 ? 7 : 12) || scale > 1.6));
    if (!show || (dim && node !== hover)) { ctx.globalAlpha = 1; return; }
    const size = (focus && node.level === 0 ? 12.5 : 11) / scale;
    ctx.font = `${node.level === 0 || node === hover ? 600 : 450} ${size}px ${getComputedStyle(root).getPropertyValue('--sans') || 'Inter, sans-serif'}`;
    const text = shortLabel(node.base.name, focus ? (stage.clientWidth < 520 ? 16 : 24) : 22);
    let x = node.x, y = node.y, align = 'center', base = 'top';
    if (focus && node.level > 0) {
      // Labels sit outside their ring, pointing away from the centre, so neighbours do not stack on one another.
      const gap = r + 5 / scale;
      x = node.x + node.side * gap; y = node.y;
      align = node.side > 0 ? 'left' : 'right'; base = 'middle';
    } else y = node.y + r + 3 / scale;
    ctx.textAlign = align; ctx.textBaseline = base;
    ctx.lineWidth = 3 / scale; ctx.strokeStyle = palette.bg; ctx.lineJoin = 'round'; ctx.strokeText(text, x, y);
    ctx.fillStyle = node.level === 0 || node === hover || node.id === selected ? palette.fg : (focus && node.level > 1 ? palette.fg2 : palette.fg2);
    if (focus && node.level === 1) ctx.fillStyle = palette.fg;
    ctx.fillText(text, x, y);
    ctx.globalAlpha = 1;
  }
  const linkHot = link => (hover && (link.source.id === hover.id || link.target.id === hover.id)) || link === hoverLink;
  function drawLinkLabel(link, ctx, scale) {
    // Relation names: the link under the pointer, or in Focus every link of a hovered record that has only a few.
    const fewLinks = prefs.mode === 'focus' && hover && (adjacency.get(hover.id)?.length || 0) <= 8 && linkHot(link);
    if (link !== hoverLink && !fewLinks) return;
    const {source: a, target: b} = link;
    if (!a || !b || a.x === undefined) return;
    const x = (a.x + b.x) / 2, y = (a.y + b.y) / 2, size = 10 / scale, text = shortLabel(link.edge.name, 28);
    ctx.font = `500 ${size}px ${getComputedStyle(root).getPropertyValue('--sans') || 'Inter, sans-serif'}`;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    const w = ctx.measureText(text).width + 8 / scale, h = size + 5 / scale;
    ctx.fillStyle = palette.bg; ctx.globalAlpha = 0.92;
    ctx.beginPath(); ctx.roundRect(x - w / 2, y - h / 2, w, h, 4 / scale); ctx.fill();
    ctx.globalAlpha = 1; ctx.fillStyle = palette.fg2; ctx.fillText(text, x, y);
  }

  // Focus labels sit outside the outermost labelled ring, so its fit leaves room for them at the sides.
  const fit = () => {
    if (!graph) return;
    const width = stage.clientWidth || 600;
    graph.zoomToFit(400, prefs.mode === 'focus' && prefs.depth === 1 ? Math.min(150, width * 0.3) : prefs.mode === 'focus' ? 36 : 24);
    // A small neighbourhood would otherwise be blown up to fill the pane.
    setTimeout(() => { if (graph && graph.zoom() > 1.6) graph.zoom(1.6, 250); }, 420);
  };
  // ---- the graph
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
      .nodePointerAreaPaint((node, color, ctx) => { ctx.fillStyle = color; ctx.beginPath(); ctx.arc(node.x, node.y, radius(node) + 3, 0, 2 * Math.PI); ctx.fill(); })
      .linkColor(link => linkHot(link) ? palette.lineHi : (lit ? 'rgba(0,0,0,0)' : palette.line))
      .linkWidth(link => linkHot(link) ? 1.2 : prefs.mode === 'focus' ? 0.8 : 0.5)
      .linkCanvasObjectMode(() => 'after')
      .linkCanvasObject(drawLinkLabel)
      .linkHoverPrecision(4)
      .onRenderFramePre(() => readPalette())
      .onNodeHover(node => setHover(node || null))
      .onLinkHover(link => { hoverLink = link || null; redraw(); })
      .onNodeClick(node => focusOn(node.id))
      .onBackgroundClick(() => { if (prefs.mode === 'map') { selected = null; paintCard(); } })
      .onEngineStop(() => { if (fitPending) { fitPending = false; fit(); } })
      .cooldownTime(4000)
      .minZoom(0.2).maxZoom(8);
    graph.d3Force('charge').strength(-26).distanceMax(260);
    graph.d3Force('link').distance(26);
    const resize = () => { const box = stage.getBoundingClientRect(); if (box.width && box.height) graph.width(box.width).height(box.height); };
    const observer = new ResizeObserver(resize);
    observer.observe(stage); resize();
    disposers.push(() => observer.disconnect());
    showMap();   // the tab always opens on the Map; Focus reopens the last record
  }
  function showMap() {
    prefs.mode = 'map'; save(); syncControls();
    graph.d3Force('radial', gravity).cooldownTicks(Infinity);
    graph.d3Force('charge').strength(-26).distanceMax(260);
    graph.d3Force('link').distance(26).strength(link => 1 / Math.min(adjacency.get(link.source.id ?? link.source)?.length || 1, adjacency.get(link.target.id ?? link.target)?.length || 1));
    const data = buildMap();
    fitPending = true;
    graph.warmupTicks(data.nodes.some(node => node.x === undefined) ? 120 : 0).graphData(data);
    paintCard();
    status.hidden = data.nodes.length > 0; status.textContent = 'No records match these filters.';
  }
  async function focusOn(id, {push = true} = {}) {
    if (!nodes.has(id)) return;
    if (push && selected && selected !== id && prefs.mode === 'focus') trail.push(selected);
    if (!push && !selected) trail = [];
    selected = id; prefs.mode = 'focus'; prefs.last = id; save(); syncControls(); setHover(null);
    paintFocus();
    paintCard();
    // The overview only carries relationships the Home pages returned; read this record's whole neighbourhood once.
    const added = await expand(id, 1).catch(() => false);
    if (added && live() && selected === id) { rebuildAdjacency(); paintFocus(); paintCard(); }
  }
  // Focus is laid out, not simulated: the first ring is spread so its labels are evenly spaced top to bottom on each
  // side (labels point outward and never stack), and the second ring sits outside it, each record near its parents.
  function paintFocus() {
    const data = buildFocus(), wide = stage.clientWidth || 600;
    const ring1 = data.nodes.filter(node => node.level === 1), ring2 = data.nodes.filter(node => node.level === 2);
    const order = (a, b) => CBRAIN_FAMILIES.findIndex(([key]) => key === a.base.family) - CBRAIN_FAMILIES.findIndex(([key]) => key === b.base.family)
      || b.base.degree - a.base.degree;
    ring1.sort(order);
    const r1 = Math.max(110, ring1.length * 5.4), r2 = r1 + Math.max(100, Math.min(170, wide * 0.18));
    const right = ring1.slice(0, Math.ceil(ring1.length / 2)), left = ring1.slice(right.length);
    const place = (list, side) => list.forEach((node, index) => {
      const t = list.length === 1 ? 0 : (index / (list.length - 1)) * 2 - 1;   // -1 (top) .. 1 (bottom)
      const y = t * r1 * 0.9 * (side > 0 ? 1 : -1);
      node.fx = side * Math.sqrt(Math.max(0, r1 * r1 - y * y)); node.fy = y; node.side = side; node.slot = index;
    });
    ringSpacing = (r1 * 0.9 * 2) / Math.max(1, right.length - 1);
    place(right, 1); place(left, -1);
    const angleOf = new Map(ring1.map(node => [node.id, Math.atan2(node.fy, node.fx)]));
    for (const node of ring2) {
      let sx = 0, sy = 0;
      for (const {other} of adjacency.get(node.id)) if (angleOf.has(other)) { sx += Math.cos(angleOf.get(other)); sy += Math.sin(angleOf.get(other)); }
      node.want = Math.atan2(sy, sx);
    }
    ring2.sort((a, b) => a.want - b.want).forEach((node, index) => {
      const angle = ring2.length < 12 ? node.want : -Math.PI + (index + 0.5) / ring2.length * 2 * Math.PI;
      node.fx = Math.cos(angle) * r2; node.fy = Math.sin(angle) * r2; node.side = Math.cos(angle) >= 0 ? 1 : -1;
    });
    for (const node of data.nodes) { node.x = node.fx; node.y = node.fy; }
    graph.d3Force('radial', null);
    fitPending = true;
    graph.warmupTicks(0).cooldownTicks(1).graphData(data);
    root.dataset.focusHidden = String(data.hidden);
    focusHidden = data.hidden;
  }
  let focusHidden = 0, ringSpacing = 20;

  // ---- controls
  function syncControls() {
    root.dataset.mode = prefs.mode;
    host.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === prefs.mode)));
    host.querySelectorAll('[data-depth]').forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.depth) === prefs.depth)));
    $q('[data-mode="focus"]').disabled = !selected && !(prefs.last && nodes.has(prefs.last));
    backButton.hidden = !(prefs.mode === 'focus' && trail.length);
  }
  function paintFilters() {
    const counts = Object.fromEntries(CBRAIN_FAMILIES.map(([key]) => [key, 0]));
    for (const node of nodes.values()) if (!prefs.unlinked || adjacency.get(node.id)?.length) counts[node.family]++;
    $q('.cbrain-filters').innerHTML = CBRAIN_FAMILIES.filter(([key]) => counts[key]).map(([key, label]) =>
      `<button type="button" class="cbrain-chip" data-family="${key}" aria-pressed="${!prefs.hidden.includes(key)}"><i style="background:var(--cb-${key})"></i>${label}<span>${counts[key]}</span></button>`).join('')
      + `<button type="button" class="cbrain-chip cbrain-unlinked" aria-pressed="${!prefs.unlinked}" title="Records with no links">Unlinked<span>${[...nodes.values()].filter(node => !adjacency.get(node.id)?.length).length}</span></button>`;
  }
  $q('.cbrain-filters').onclick = event => {
    const chip = event.target.closest('.cbrain-chip');
    if (!chip || !graph) return;
    if (chip.classList.contains('cbrain-unlinked')) prefs.unlinked = !prefs.unlinked;
    else prefs.hidden = prefs.hidden.includes(chip.dataset.family) ? prefs.hidden.filter(key => key !== chip.dataset.family) : [...prefs.hidden, chip.dataset.family];
    save(); paintFilters();
    if (prefs.mode === 'map') showMap(); else { paintFocus(); paintCard(); }
  };
  host.querySelectorAll('[data-mode]').forEach(button => button.onclick = () => {
    if (!graph || button.dataset.mode === prefs.mode) return;
    if (button.dataset.mode === 'map') { trail = []; showMap(); } else focusOn(selected || prefs.last, {push: false});
  });
  host.querySelectorAll('[data-depth]').forEach(button => button.onclick = () => {
    prefs.depth = Number(button.dataset.depth); save(); syncControls();
    if (graph && prefs.mode === 'focus') { paintFocus(); paintCard(); }
  });
  backButton.onclick = () => { const id = trail.pop(); if (id) focusOn(id, {push: false}); };
  $q('[data-zoom="fit"]').onclick = () => fit();

  // ---- search
  let matches = [], active = 0;
  const findMatches = query => {
    const q = query.trim().toLocaleLowerCase();
    if (!q) return [];
    const score = node => {
      const name = node.name.toLocaleLowerCase();
      if (name === q) return 0;
      if (name.startsWith(q)) return 1;
      if (name.includes(q)) return 2;
      if (node.aliases.some(alias => alias.toLocaleLowerCase().includes(q))) return 3;
      return 9;
    };
    return [...nodes.values()].map(node => [score(node), node]).filter(([s]) => s < 9)
      .sort((a, b) => a[0] - b[0] || b[1].degree - a[1].degree).slice(0, 8).map(([, node]) => node);
  };
  const paintResults = () => {
    results.hidden = !matches.length;
    results.innerHTML = matches.map((node, index) => `<button type="button" role="option" class="cbrain-result" data-id="${esc(node.id)}" aria-selected="${index === active}">
      <i style="background:var(--cb-${node.family})"></i><span>${esc(node.name)}</span><small>${esc(cbrainTypeLabel(node.type))}</small></button>`).join('');
  };
  const pick = id => { search.value = ''; matches = []; paintResults(); search.blur(); focusOn(id); };
  search.oninput = () => { matches = findMatches(search.value); active = 0; paintResults(); };
  search.onkeydown = event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); active = (active + (event.key === 'ArrowDown' ? 1 : matches.length - 1)) % Math.max(1, matches.length); paintResults(); }
    else if (event.key === 'Enter' && matches[active]) { event.preventDefault(); pick(matches[active].id); }
    else if (event.key === 'Escape') { search.value = ''; matches = []; paintResults(); }
  };
  results.onmousedown = event => { const row = event.target.closest('[data-id]'); if (row) { event.preventDefault(); pick(row.dataset.id); } };
  search.onblur = () => setTimeout(() => { if (live()) { matches = []; paintResults(); } }, 120);

  // ---- the card
  function paintCard() {
    const node = selected && nodes.get(selected);
    card.hidden = !node;
    if (!node) return;
    const groups = new Map();
    for (const {edge, other} of adjacency.get(node.id)) {
      const label = edge.kind;   // grouped by the relation's tag (within, related, uses…); the full name shows on hover
      if (!groups.has(label)) groups.set(label, []);
      if (!groups.get(label).includes(other)) groups.get(label).push(other);
    }
    const sorted = [...groups].sort((a, b) => b[1].length - a[1].length);
    const links = adjacency.get(node.id).length;
    card.classList.toggle('collapsed', !prefs.card);
    card.innerHTML = `<header class="cbrain-card-head">
        <i class="cbrain-dot" style="background:var(--cb-${node.family})"></i><h3 title="${esc(node.name)}">${esc(node.name)}</h3>
        <span class="cbrain-pill">${esc(cbrainTypeLabel(node.type))}</span>
        <button type="button" class="cbrain-icon cbrain-card-toggle" aria-expanded="${prefs.card}" aria-label="${prefs.card ? 'Collapse details' : 'Expand details'}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></button>
      </header>
      <div class="cbrain-card-body">
        ${node.text ? `<p class="cbrain-text">${esc(node.text)}</p>` : '<p class="cbrain-text muted">No description yet.</p>'}
        <p class="cbrain-meta">${links} ${links === 1 ? 'link' : 'links'}${prefs.mode === 'focus' && focusHidden ? ` · ${focusHidden} not drawn` : ''}</p>
        <div class="cbrain-groups">${sorted.slice(0, 6).map(([label, ids]) => `<div class="cbrain-group"><span class="cbrain-rel">${esc(label)}</span>
          ${ids.slice(0, 6).map(id => `<button type="button" class="cbrain-link" data-id="${esc(id)}"><i style="background:var(--cb-${nodes.get(id).family})"></i>${esc(shortLabel(nodes.get(id).name, 28))}</button>`).join('')}
          ${ids.length > 6 ? `<span class="cbrain-more">+${ids.length - 6}</span>` : ''}</div>`).join('')}
          ${sorted.length > 6 ? `<p class="cbrain-meta">${sorted.length - 6} more kinds of link</p>` : ''}</div>
      </div>`;
    card.querySelector('.cbrain-card-toggle').onclick = () => { prefs.card = !prefs.card; save(); paintCard(); };
    card.querySelectorAll('.cbrain-link').forEach(button => button.onclick = () => focusOn(button.dataset.id));
  }

  syncControls();
  loadAll().catch(error => {
    if (!live() || error?.name === 'AbortError') return;
    status.hidden = false;
    status.textContent = 'Could not read context. ' + (error?.message || '');
  });
  return () => {
    dead = true;
    reads.abort();
    for (const dispose of disposers) dispose();
    if (graph) { graph.pauseAnimation(); graph._destructor?.(); }
    graph = null;
    host.replaceChildren();
  };
}
