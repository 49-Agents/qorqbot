// Context Map uses only stored directional relationships. Upload outlines use ctxmap.js.
const ctxGraphHash = s => Array.from(String(s)).reduce((h, c) => ((h * 31) + c.codePointAt(0)) >>> 0, 0);

// context_find cursors bind both the query and scope snapshot. Keep each list in
// a private accumulator and return it only after the server confirms completion;
// callers never render a partial first page as if it were the whole graph.
async function ctxFindAllPages(readPage, {query = "", limit = 500} = {}) {
  if (typeof readPage !== "function" || !Number.isInteger(limit) || limit < 1 || limit > 500)
    throw new Error("Invalid context page reader");
  const items = [], cursors = new Set(), refs = new Set();
  let cursor = null, total = null, pages = 0;
  for (;;) {
    const page = await readPage({query, limit, ...(cursor ? {cursor} : {})});
    if (!page || !Array.isArray(page.items) || !Number.isSafeInteger(page.total) || page.total < 0 ||
        typeof page.complete !== "boolean" || !(page.next_cursor === null || typeof page.next_cursor === "string"))
      throw new Error("Context search returned an incomplete page response");
    if (total !== null && page.total !== total)
      throw new Error("Context search snapshot changed while loading; reload to try again");
    total = page.total;
    for (const item of page.items) {
      const ref = item && typeof item === "object" && !Array.isArray(item) ? item.ref : null;
      if (typeof ref !== "string" || !/^<object>[^<>]+<\/object>$/.test(ref) || refs.has(ref))
        throw new Error("Context search returned a malformed or repeated canonical reference; reload to try again");
      refs.add(ref);
    }
    items.push(...page.items);
    pages++;
    if (items.length > total)
      throw new Error("Context search returned more items than its snapshot contains");
    if (page.complete) {
      if (page.next_cursor !== null || items.length !== total)
        throw new Error("Context search marked an incomplete snapshot complete; reload to try again");
      return {items, total, complete: true, pages};
    }
    if (!page.items.length || !page.next_cursor || cursors.has(page.next_cursor))
      throw new Error("Context search is incomplete and has no usable continuation; reload to try again");
    cursors.add(page.next_cursor);
    cursor = page.next_cursor;
  }
}

// Canonical context_find projection. Relationships remain first-class nodes so
// a stored multi-endpoint record is represented by endpoint membership spokes,
// never by inferred pairs between its endpoints.
function ctxFindGraphData(items) {
  const rows = Array.isArray(items) ? items : [], nodes = [], edges = [], byRef = new Map();
  const isRef = value => typeof value === "string" && /^<object>[^<>]+<\/object>$/.test(value);
  const isRelationship = item => item?.is_relationship === true || item?.kind === "relationship";
  const colors = Object.assign(Object.create(null), {topic:"#849a75", material:"#b3b7bd", note:"#b3b7bd", person:"#72a99f", task:"#d88a85",
    story:"#d4b463", table:"#83b5cf", agent:"#b997c2", project:"#849a75", idea:"#d4b463", transcript:"#b3b7bd",
    occurrence:"#d88a85", event:"#d88a85", company:"#83b5cf", platform:"#83b5cf", other:"#aaaaaa", relationship:"#b69861"});
  const refLabel = ref => String(ref || "").replace(/^<object>|<\/object>$/g, "").replace(/[_-]+/g, " ");
  const makeNode = (item, kind, idPrefix) => {
    const ref = item.ref;
    const node = {id: `${idPrefix}:${ref}`, kind, label: String(item.name || refLabel(ref)),
      tip: String(item.summary || item.definition || ""), color: Object.hasOwn(colors,kind) ? colors[kind] : "#aaaaaa",
      item: {...item, type: kind, id: ref, ref}};
    return node;
  };
  for (const item of rows) {
    if (!item || isRelationship(item) || !isRef(item.ref) || byRef.has(item.ref)) continue;
    const kind = item.kind === "business_topic" ? "topic" : String(item.kind || "other");
    const node = makeNode(item, kind, "object");
    nodes.push(node); byRef.set(item.ref, node);
  }
  const relationships = [];
  for (const item of rows) {
    if (!item || !isRelationship(item) || !isRef(item.ref) || byRef.has(item.ref)) continue;
    const rawEndpoints = Array.isArray(item.endpoints) ? item.endpoints.slice() : [];
    const node = makeNode({...item, endpoints: rawEndpoints,
      unavailableEndpointCount: 0,
      malformedEndpointCount: Array.isArray(item.endpoints) ? rawEndpoints.filter(ref => !isRef(ref)).length : 1},
    "relationship", "relationship");
    nodes.push(node); byRef.set(item.ref, node); relationships.push(node);
  }
  relationships.forEach(node => {
    const endpointRefs = [...new Set(node.item.endpoints.filter(isRef))];
    node.item.unavailableEndpointCount = endpointRefs.filter(ref => !byRef.has(ref)).length;
    endpointRefs.forEach((endpointRef, index) => {
      const endpoint = byRef.get(endpointRef);
      if (!endpoint) return;
      edges.push({id: `endpoint:${encodeURIComponent(node.item.ref)}:${index}`, source: node.id, target: endpoint.id,
        kind: "endpoint", role: "endpoint", membership: true, relationshipRef: node.item.ref, endpointRef});
    });
  });
  return {rootId: null, nodes, edges, counts: {objects: nodes.filter(node => node.kind !== "relationship").length,
    relationships: nodes.filter(node => node.kind === "relationship").length, endpointLinks: edges.length}};
}

function ctxGraphData(d, scope, isRoot) {
  return ctxRelationshipGraph(d.graph, isRoot ? null : scope.document_id);
}

function ctxRelationshipGraph(data, topicId) {
  const key=item=>`${item.type}:${item.id}`;
  let edges=data.edges.map(edge=>({...edge,source:key(edge.source),target:key(edge.target),kind:edge.role}));
  let items=data.nodes;
  const rootId=topicId?`topic:${topicId}`:null;
  if(rootId){
    edges=edges.filter(edge=>edge.source===rootId||edge.target===rootId);
    const ids=new Set([rootId,...edges.flatMap(edge=>[edge.source,edge.target])]);
    items=items.filter(item=>ids.has(key(item)));
  }
  const colors={topic:'#849a75',material:'#b3b7bd',person:'#72a99f',task:'#d88a85',story:'#d4b463',table:'#83b5cf',agent:'#b997c2',relationship:'#b69861'};
  const nodes=items.map(item=>({id:key(item),item,kind:item.type,label:item.label,tip:item.summary || '',
    path:item.path || null,color:colors[item.type] || '#aaaaaa'}));
  return {rootId,nodes,edges};
}

// Deterministic starting positions and a bounded spring/collision pass. Saved nodes stay fixed so
// arriving notes do not rearrange a map the user has already explored. No continuously running force loop.
function ctxGraphLayout(graph, saved = {}) {
  const nodes = graph.nodes.map(n => ({ ...n })), byId = new Map(nodes.map(n => [n.id, n]));
  const adjacency = new Map(nodes.map(n => [n.id, []]));
  graph.edges.forEach(e => { if(e.source!==e.target){adjacency.get(e.source).push(byId.get(e.target));adjacency.get(e.target).push(byId.get(e.source));} });
  const visited=new Set(),components=[];
  for(const seed of [...nodes].sort((a,b)=>adjacency.get(b.id).length-adjacency.get(a.id).length || a.id.localeCompare(b.id))){
    if(visited.has(seed.id))continue;
    const members=[seed];visited.add(seed.id);
    for(let i=0;i<members.length;i++)for(const neighbor of adjacency.get(members[i].id))if(!visited.has(neighbor.id)){visited.add(neighbor.id);members.push(neighbor);}
    components.push(members);
  }
  // Pack disconnected components in two dimensions; only real edges attract nodes.
  const sizes=components.map(m=>Math.max(180,Math.sqrt(m.length)*190));
  const shelfWidth=Math.max(0,...sizes,Math.sqrt(sizes.reduce((sum,size)=>sum+(size+120)**2,0)));
  let x=0,y=0,rowHeight=0;
  components.forEach((members,index)=>{
    const size=sizes[index];
    if(x && x+size>shelfWidth){x=0;y+=rowHeight+120;rowHeight=0;}
    const cx=x+size/2,cy=y+size/2;
    members.forEach((n,i)=>{const angle=i*2.39996,r=65*Math.sqrt(i);n.x=cx+Math.cos(angle)*r;n.y=cy+Math.sin(angle)*r;n.cx=cx;n.cy=cy;});
    x+=size+120;rowHeight=Math.max(rowHeight,size);
  });
  nodes.forEach(n => {
    if (n.id === graph.rootId) { n.x = 0; n.y = 0; }
    const p = saved[n.id];
    n.fixed = !!(p && Number.isFinite(p.x) && Number.isFinite(p.y) && Math.abs(p.x) < 1e7 && Math.abs(p.y) < 1e7);
    if (n.fixed) { n.x = p.x; n.y = p.y; }
  });
  // A new node starts near saved neighbours, even if those have been dragged far from the default layout.
  nodes.filter(n => !n.fixed).forEach(n => {
    const linked = adjacency.get(n.id).filter(k => k.fixed);
    if (!linked.length) return;
    const a = (ctxGraphHash(n.id) % 360) * Math.PI / 180;
    n.x = linked.reduce((s, k) => s + k.x, 0) / linked.length + 150 * Math.cos(a);
    n.y = linked.reduce((s, k) => s + k.y, 0) / linked.length + 150 * Math.sin(a);
  });
  const move = (n, x, y) => { if (!n.fixed && n.id !== graph.rootId) { n.x += x; n.y += y; } };
  if (nodes.some(n => !n.fixed)) for (let iteration = 0; iteration < 240; iteration++) {
    const cooling = 1 - iteration / 300;
    for (const e of graph.edges) {
      const a = byId.get(e.source), b = byId.get(e.target), dx = b.x - a.x, dy = b.y - a.y, dist = Math.hypot(dx, dy) || 1;
      const length = a.id === graph.rootId && a.kind === "root" ? 285 : e.kind === "contains" ? 155 : 170;
      const f = Math.max(-20, Math.min(20, (dist - length) * .06)) * cooling;
      move(a, dx / dist * f, dy / dist * f); move(b, -dx / dist * f, -dy / dist * f);
    }
    // Local collision pairs only, rather than comparing every node with every other node.
    const cells = new Map(), cellSize = 155;
    nodes.forEach((n, i) => {
      const cx = Math.floor(n.x / cellSize), cy = Math.floor(n.y / cellSize);
      for (let x = cx - 1; x <= cx + 1; x++) for (let y = cy - 1; y <= cy + 1; y++) {
        for (const j of cells.get(`${x},${y}`) || []) {
          const b = nodes[j]; let dx = n.x - b.x, dy = n.y - b.y;
          if (!dx && !dy) { dx = .1; dy = .1; }
          const dist = Math.hypot(dx, dy), gap = 115;
          if (dist >= gap) continue;
          const f = (gap - dist) * .24 * cooling;
          move(n, dx / dist * f, dy / dist * f); move(b, -dx / dist * f, -dy / dist * f);
        }
      }
      const key = `${cx},${cy}`; if (!cells.has(key)) cells.set(key, []); cells.get(key).push(i);
    });
    nodes.forEach(n => move(n, (n.cx - n.x) * .001 * cooling, (n.cy - n.y) * .001 * cooling));
  }
  return nodes;
}

function ctxGraphAnchor(n, to) {
  const dx = to.sx - n.sx, dy = to.sy - n.sy, length = Math.hypot(dx, dy) || 1, ux = dx / length, uy = dy / length;
  let distance = n.radius;
  if (n.kind === "person") {
    const vertices = [[0, -n.radius], [n.radius, n.radius], [-n.radius, n.radius]], hits = [];
    vertices.forEach((a, i) => {
      const b = vertices[(i + 1) % 3], vx = b[0] - a[0], vy = b[1] - a[1], den = ux * vy - uy * vx;
      if (Math.abs(den) < .00001) return;
      const t = (a[0] * vy - a[1] * vx) / den, u = (a[0] * uy - a[1] * ux) / den;
      if (t >= 0 && u >= 0 && u <= 1) hits.push(t);
    });
    if (hits.length) distance = Math.min(...hits);
  }
  return { x: n.sx + ux * (distance + 1), y: n.sy + uy * (distance + 1) };
}

function ctxGraphMermaid(graph) {
  const ids = new Map(graph.nodes.map((n, i) => [n.id, `n${i}`])), lines = ["graph LR"];
  graph.nodes.forEach(n => lines.push(`  ${ids.get(n.id)}["${mmText(n.label).replace(/</g, "").replace(/>/g, "").replace(/&/g, "and")}"]`));
  graph.edges.forEach(e => lines.push(e.membership
    ? `  ${ids.get(e.source)} ---|endpoint| ${ids.get(e.target)}`
    : `  ${ids.get(e.source)} -->|${mmText(e.role || e.kind)}| ${ids.get(e.target)}`));
  return lines.join("\n") + "\n";
}

function contextGraph(el, graph, opts = {}) {
  const abort = new AbortController(), listen = (el, name, fn, options = {}) => el.addEventListener(name, fn, { ...options, signal: abort.signal });
  let saved = {};
  try { saved = JSON.parse(ls(opts.storageKey) || "{}") || {}; } catch (_) { /* absent or invalid local view */ }
  const nodes = ctxGraphLayout(graph, saved.positions || {}), byId = new Map(nodes.map(n => [n.id, n]));
  const byRef = new Map(nodes.filter(n=>n.item?.ref || n.item?.id).map(n=>[n.item.ref || n.item.id,n]));
  const degrees = new Map(nodes.map(n => [n.id, 0]));
  graph.edges.forEach(e => { degrees.set(e.source, degrees.get(e.source) + 1); degrees.set(e.target, degrees.get(e.target) + 1); });
  let selected = byId.has(saved.selected) ? saved.selected : null, neighbors = new Set();
  let width = 0, height = 0, frame = 0, saveTimer = 0, destroyed = false;
  const camera = { k: 1, x: 0, y: 0 };
  const typeNames = Object.assign(Object.create(null), {story:'Stories',task:'Tasks',person:'People',table:'Tables',agent:'Agents',topic:'Topics',material:'Materials',
    project:'Projects',idea:'Ideas',note:'Notes',transcript:'Transcripts',occurrence:'Occurrences',event:'Events',
    company:'Companies',platform:'Platforms',other:'Other',relationship:'Relationships'});
  const knownLegacyTypes = ['story','task','person','table','agent','topic','material'];
  const graphKinds = [...new Set(nodes.map(node=>node.kind))];
  graphKinds.forEach(kind=>{if(!Object.hasOwn(typeNames,kind))typeNames[kind]=kind.replace(/[-_]+/g,' ').replace(/\b\w/g,c=>c.toUpperCase());});
  let preference;try{preference=JSON.parse(ls('ctxgraph-types-v4') || 'null');}catch(_){}
  let storedTypes, knownTypes;
  if (preference && Array.isArray(preference.shown) && Array.isArray(preference.known)) {
    storedTypes=preference.shown; knownTypes=new Set(preference.known);
  } else {
    try { storedTypes=JSON.parse(ls('ctxgraph-types-v3') || 'null'); } catch(_) { storedTypes=null; }
    if (Array.isArray(storedTypes)) knownTypes=new Set([...knownLegacyTypes,'relationship']);
    else {
      try { storedTypes=JSON.parse(ls('ctxgraph-types-v2') || 'null'); } catch(_) { storedTypes=null; }
      knownTypes=new Set(knownLegacyTypes);
    }
    if (!Array.isArray(storedTypes)) storedTypes=graphKinds.slice();
  }
  const shownTypes = new Set(storedTypes);
  graphKinds.forEach(kind=>{if(!knownTypes.has(kind))shownTypes.add(kind);knownTypes.add(kind);});
  const isVisible = n => shownTypes.has(n.kind) || n.id === selected || neighbors.has(n.id) || n.id === graph.rootId;
  const minZoom = .03, maxZoom = 4;
  const arrowId='context-arrow-'+newRequestId();
  el.innerHTML = `<div class="ctxgraph" aria-label="Context graph">
    <div class="ctxgraph-bar"><span class="ctxgraph-caption">Graph</span>
      <div class="ctxgraph-search"><input type="search" aria-label="Find a node in this map" placeholder="Find a node…" autocomplete="off"><div class="ctxgraph-results" hidden></div></div>
      <label class="ctxgraph-links"><input type="checkbox" ${ls("ctxgraph-links") !== "0" ? "checked" : ""}> All links</label>
      <button type="button" data-action="copy" title="Copy graph as Mermaid" aria-label="Copy graph as Mermaid">${SVG_MAPCOPY}</button>
    </div>
    <div class="ctxgraph-types">${Object.entries(typeNames).filter(([kind])=>nodes.some(n=>n.kind===kind)).map(([kind,label])=>`<label><input type="checkbox" data-kind="${esc(kind)}" ${shownTypes.has(kind)?'checked':''}>${esc(label)}</label>`).join('')}</div>
    <div class="ctxgraph-canvas" tabindex="0" role="group" aria-label="Context map. Drag nodes to arrange; drag the background to pan. Use plus, minus and arrow keys to navigate.">
      <svg class="ctxgraph-network" aria-hidden="true"><defs><marker id="${arrowId}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 1 L 9 5 L 0 9 Z" fill="#a6aaa6"></path></marker></defs><g class="ctxgraph-edges"></g></svg>
      <div class="ctxgraph-nodes"></div>

      <div class="ctxgraph-zoom"><button type="button" data-action="out" aria-label="Zoom out">−</button><output aria-label="Zoom level">100%</output><button type="button" data-action="in" aria-label="Zoom in">+</button><button type="button" data-action="fit" title="Fit to view" aria-label="Fit to view">${SVG_MAPFIT}</button></div>
    </div>
    <div class="ctxgraph-footer"><div class="ctxgraph-legend"><span><i class="ctxgraph-topic-key"></i>Topic</span><span><i class="ctxgraph-note-key"></i>Material</span><span><i class="ctxgraph-person-key"></i>Person</span><span><i class="ctxgraph-relationship-key"></i>Relationship</span><span>Task</span><span>Story</span><span>Table</span><span>Agent</span></div><span class="ctxgraph-count"></span></div>
    <div class="ctxgraph-reader" hidden></div><div class="ctxgraph-sr" role="status" aria-live="polite"></div>
  </div>`;
  const root = el.querySelector(".ctxgraph"), canvas = root.querySelector(".ctxgraph-canvas"), svg = root.querySelector(".ctxgraph-network");
  const layer = root.querySelector(".ctxgraph-nodes"), reader = root.querySelector(".ctxgraph-reader"), search = root.querySelector("input[type=search]");
  const results = root.querySelector(".ctxgraph-results"), links = root.querySelector(".ctxgraph-links input"), status = root.querySelector("[role=status]");
  root.querySelector(".ctxgraph-count").textContent = graph.counts
    ? `${graph.counts.objects} objects · ${graph.counts.relationships} relationships · ${graph.counts.endpointLinks} endpoint links`
    : `${nodes.length} nodes · ${graph.edges.length} edges`;
  const pairs=new Map();
  graph.edges.forEach(edge=>{const pair=[edge.source,edge.target].sort().join('|');if(!pairs.has(pair))pairs.set(pair,[]);pairs.get(pair).push(edge.id);});
  const edges = graph.edges.map(e => {
    const siblings=pairs.get([e.source,e.target].sort().join('|'));
    const path = svgEl("path", { class: `ctxgraph-edge ${e.kind}`, "data-edge": e.id });
    if (!e.membership) path.setAttribute('marker-end', `url(#${arrowId})`);
    root.querySelector(".ctxgraph-edges").append(path); return { ...e, el: path, index:siblings.indexOf(e.id), count:siblings.length };
  });
  const labelCanvas = document.createElement("canvas"), measure = labelCanvas.getContext("2d");
  const measureLabels = () => nodes.forEach(n => {
    measure.font = `${n.kind === "root" ? "600 13" : "500 11.5"}px Inter, sans-serif`;
    n.labelW = Math.min(132, Math.max(...n.lines.map(line => measure.measureText(line).width))) + 2;
    n.labelH = n.lines.length * 16;
  });
  nodes.forEach(n => {
    n.radius = n.kind === "root" ? 22 : n.kind === "topic" || n.kind === "group" ? 13 : n.kind === "person" ? 9 : n.kind === "relationship" ? 8 : 6;
    const button = document.createElement("button"); button.type = "button";
    button.className = `ctxgraph-node ${String(n.kind).replace(/[^a-zA-Z0-9_-]/g,'-')}`; button.dataset.node = n.id;
    button.style.setProperty("--node-color", n.color); button.style.setProperty("--node-size", `${n.radius * 2}px`);
    button.setAttribute("aria-label", `${n.label}, ${n.kind === "group" ? "collection" : n.kind}`); button.title = n.label;
    const dot = document.createElement("span"); dot.className = "ctxgraph-dot"; dot.setAttribute("aria-hidden", "true");
    const label = document.createElement("span"); label.className = "ctxgraph-label"; label.setAttribute("aria-hidden", "true");
    n.lines = mapWrap(String(n.label).replace(/(\S{19})/g, "$1 "), 20, 2);
    label.textContent = n.lines.join("\n"); button.append(dot, label); layer.append(button);
    n.el = button; n.labelEl = label;
  });
  measureLabels();
  function save() {
    if (!opts.storageKey || !width || !height) return;
    const positions = Object.fromEntries(nodes.map(n => [n.id, { x: n.x, y: n.y }]));
    // Only coordinates and selection are stored in the browser, never context text.
    try { ls(opts.storageKey, JSON.stringify({ positions, selected, camera: { k: camera.k, cx: (width / 2 - camera.x) / camera.k, cy: (height / 2 - camera.y) / camera.k } })); } catch (_) { /* quota/private mode: map still works */ }
  }
  function persist() { clearTimeout(saveTimer); saveTimer = setTimeout(save, 250); }
  function schedule() { if (!destroyed && !frame) frame = requestAnimationFrame(() => { frame = 0; if (!destroyed) draw(); }); }
  const overlap = (a, b) => a.x < b.x + b.w + 3 && a.x + a.w + 3 > b.x && a.y < b.y + b.h + 3 && a.y + a.h + 3 > b.y;
  function placeLabels() {
    // A screen-space grid keeps label placement bounded even when most of a large graph is visible.
    const cells = new Map(), size = 160;
    const visit = (b, fn) => { for (let x = Math.floor(b.x / size); x <= Math.floor((b.x + b.w) / size); x++) for (let y = Math.floor(b.y / size); y <= Math.floor((b.y + b.h) / size); y++) fn(`${x},${y}`); };
    const reserve = b => visit(b, key => { if (!cells.has(key)) cells.set(key, []); cells.get(key).push(b); });
    const score = b => { let hits = 0; const seen = new Set(); visit(b, key => { for (const other of cells.get(key) || []) if (!seen.has(other)) { seen.add(other); if (overlap(b, other)) hits++; } }); return hits; };
    const visible = nodes.filter(n => isVisible(n) && n.sx > -30 && n.sx < width + 30 && n.sy > -30 && n.sy < height + 30);
    visible.forEach(n => reserve({ x: n.sx - n.radius - 3, y: n.sy - n.radius - 3, w: n.radius * 2 + 6, h: n.radius * 2 + 6 }));
    const priority = n => n.id === selected ? 0 : neighbors.has(n.id) ? 1 : n.id === graph.rootId ? 2 : n.kind === "topic" || n.kind === "group" ? 3 : 4;
    visible.sort((a, b) => priority(a) - priority(b) || degrees.get(b.id) - degrees.get(a.id));
    visible.forEach(n => {
      const w = n.labelW, h = n.labelH, gap = n.radius + 8;
      const positions = [[gap, -h / 2], [-w - gap, -h / 2], [-w / 2, gap], [-w / 2, -gap - h], [gap, gap], [-gap - w, -gap - h], [gap, -gap - h], [-gap - w, gap]];
      let best = null;
      for (const [dx, dy] of positions) {
        const b = { x: n.sx + dx, y: n.sy + dy, w, h };
        const cost = score(b) + (b.x < 8 || b.y < 8 || b.x + w > width - 8 || b.y + h > height - 55 ? 8 : 0);
        if (!best || cost < best.cost) best = { ...b, cost, dx, dy };
        if (!cost) break;
      }
      n.labelEl.style.left = `${22 + best.dx}px`; n.labelEl.style.top = `${22 + best.dy}px`;
      n.labelEl.style.width = `${w}px`; n.labelEl.style.visibility = best.cost && n.id !== selected ? "hidden" : "visible";
      if (!best.cost || n.id === selected) reserve(best);
    });
  }
  function draw() {
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    nodes.forEach(n => {
      n.el.hidden = !isVisible(n);
      n.sx = n.x * camera.k + camera.x; n.sy = n.y * camera.k + camera.y;
      n.el.style.left = `${n.sx}px`; n.el.style.top = `${n.sy}px`;
      n.el.classList.toggle("selected", selected === n.id); n.el.classList.toggle("neighbor", neighbors.has(n.id));
      n.el.classList.toggle("dim", !!selected && selected !== n.id && !neighbors.has(n.id));
      n.el.setAttribute("aria-pressed", String(selected === n.id));
    });
    edges.forEach(e => {
      const a = byId.get(e.source), b = byId.get(e.target);
      if(a===b){
        const r=a.radius+32+e.index*18;
        e.el.setAttribute('d',`M${a.sx-a.radius} ${a.sy} C${a.sx-r*2} ${a.sy-r*2} ${a.sx+r*2} ${a.sy-r*2} ${a.sx+a.radius} ${a.sy}`);
      }else{
        const dx=b.sx-a.sx,dy=b.sy-a.sy,length=Math.hypot(dx,dy)||1;
        const offset=(e.index-(e.count-1)/2)*30*(e.source<e.target?1:-1);
        const control={sx:(a.sx+b.sx)/2-dy/length*offset,sy:(a.sy+b.sy)/2+dx/length*offset};
        const from=ctxGraphAnchor(a,control),to=ctxGraphAnchor(b,control);
        e.el.setAttribute('d',`M${from.x} ${from.y} Q${control.sx} ${control.sy} ${to.x} ${to.y}`);
      }
      const hot = e.source === selected || e.target === selected;
      e.el.classList.toggle("hot", !!selected && hot); e.el.classList.toggle("faded", !!selected && !hot);
      e.el.style.display = isVisible(a) && isVisible(b) && (hot || links.checked) ? "" : "none";
    });
    placeLabels();
    root.querySelector(".ctxgraph-zoom output").textContent = `${Math.round(camera.k * 100)}%`;
  }
  function showReader() {
    neighbors = new Set();
    if (!selected) { reader.hidden = true; return; }
    const n = byId.get(selected);
    graph.edges.forEach(e => { if (e.source === selected) neighbors.add(e.target); else if (e.target === selected) neighbors.add(e.source); });
    reader.hidden = false;
    const kind = n.kind === "group" ? "Collection" : n.kind === "root" ? "Business" : n.kind[0].toUpperCase() + n.kind.slice(1);
    const endpoints = n.kind === "relationship" && Array.isArray(n.item?.endpoints) ? n.item.endpoints : null;
    const endpointDetails = endpoints ? `<div class="ctxgraph-endpoints"><strong>Stored endpoints (${endpoints.length})</strong>${endpoints.length ? endpoints.map((ref,index)=>{
      const target=typeof ref==='string'?byRef.get(ref):null, label=target?.label || (typeof ref==='string'?ref:JSON.stringify(ref));
      return `<div class="ctxgraph-endpoint"><span>${index+1}.</span>${target?`<button type="button" class="quiet" data-select="${esc(target.id)}">${esc(label)}</button>`:`<span>${esc(label)} <small>Unavailable or filtered</small></span>`}</div>`;
    }).join('') : '<div class="ctxgraph-endpoint"><span>No endpoints recorded.</span></div>'}${n.item.unavailableEndpointCount ? `<small>${n.item.unavailableEndpointCount} endpoint${n.item.unavailableEndpointCount===1?' is':'s are'} unavailable or filtered from this graph.</small>` : ''}${n.item.malformedEndpointCount ? `<small>${n.item.malformedEndpointCount} malformed endpoint${n.item.malformedEndpointCount===1?'':'s'} retained in the source data.</small>` : ''}</div>` : '';
    reader.innerHTML = `<div class="ctxgraph-reader-head"><div><div class="ctxgraph-kind">${esc(kind)}${n.role ? ` · ${esc(n.role)}` : ""}${n.external ? " · related topic" : ""}</div><h3>${esc(n.label)}</h3></div><button type="button" data-action="clear" aria-label="Close selection">×</button></div>
      ${(n.item?.departments || []).map(departmentTag).join(' ')}${n.tip ? `<p>${esc(n.tip)}</p>` : ""}<div class="ctxgraph-reader-actions">${n.item || n.path != null ? `<button type="button" class="ctxgraph-open" data-action="open">Open ${esc(kind.toLowerCase())} →</button>` : ""}<span>${neighbors.size} connection${neighbors.size === 1 ? "" : "s"}</span></div>${endpointDetails}
      <div class="ctxgraph-relations">${graph.edges.filter(edge=>edge.source===selected||edge.target===selected).map(edge=>{
        const other=byId.get(edge.source===selected?edge.target:edge.source);
        const description = edge.membership ? `<div>Endpoint membership · <strong>${esc(other.label)}</strong></div>`
          : `<div>${esc(edge.source===selected?n.label:other.label)} &rarr; <strong>${esc(contextRoleLabel(edge.role || edge.kind))}</strong> &rarr; ${esc(edge.source===selected?other.label:n.label)}</div>`;
        return `<div class="ctxgraph-relation">${description}${edge.context?`<p>${esc(edge.context)}</p>`:''}<button type="button" class="quiet" data-select="${esc(other.id)}">${esc(other.label)}</button>${edge.created_at?`<small>Created ${esc(storyDate(edge.created_at))} · Edited ${esc(storyDate(edge.updated_at))}</small>`:''}</div>`;
      }).join('')}</div><div class="ctxgraph-reviews"></div>`;
    opts.onSelect?.(n,reader.querySelector('.ctxgraph-reviews'));
    status.textContent = `${n.label}, ${neighbors.size} connections`;
  }
  function select(id, center = false) {
    selected = byId.has(id) ? id : null; showReader();
    results.hidden = true; search.value = "";
    if (center && selected) {
      const n = byId.get(selected); camera.x = width / 2 - n.x * camera.k; camera.y = height / 2 - n.y * camera.k;
    }
    if (!selected) status.textContent = "Selection cleared";
    schedule(); persist();
  }
  function zoom(factor, x = width / 2, y = height / 2) {
    const k = Math.max(minZoom, Math.min(maxZoom, camera.k * factor)), ratio = k / camera.k;
    camera.x = x - (x - camera.x) * ratio; camera.y = y - (y - camera.y) * ratio; camera.k = k;
    schedule(); persist();
  }
  function fit() {
    const visible = nodes.filter(isVisible);
    if (!visible.length) return;
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    visible.forEach(n => { x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x); y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y); });
    camera.k = Math.max(minZoom, Math.min(1.25, Math.max(60, width - 160) / Math.max(1, x1 - x0), Math.max(60, height - 155) / Math.max(1, y1 - y0)));
    camera.x = (width - (x0 + x1) * camera.k) / 2; camera.y = (height - 35 - (y0 + y1) * camera.k) / 2;
    schedule(); persist();
  }

  root.querySelectorAll('[data-kind]').forEach(input=>listen(input,'change',()=>{
    if(input.checked)shownTypes.add(input.dataset.kind);else shownTypes.delete(input.dataset.kind);
    ls('ctxgraph-types-v4',JSON.stringify({shown:[...shownTypes],known:[...knownTypes]}));fit();schedule();
  }));

  // Pointer capture supports mouse, touch and pen without any global move/up listeners.
  const pointers = new Map(); let drag = null, pinch = null, suppressClick = false;
  const point = e => { const r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  listen(canvas, "pointerdown", e => {
    if (e.button !== 0 || e.target.closest(".ctxgraph-zoom")) return;
    const p = point(e); pointers.set(e.pointerId, p); canvas.setPointerCapture(e.pointerId); suppressClick = false;
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { distance: Math.hypot(a.x - b.x, a.y - b.y), k: camera.k, wx: ((a.x + b.x) / 2 - camera.x) / camera.k, wy: ((a.y + b.y) / 2 - camera.y) / camera.k };
      drag = null; suppressClick = true; return;
    }
    const button = e.target.closest("[data-node]");
    drag = { id: e.pointerId, start: p, last: p, node: button && byId.get(button.dataset.node), moved: false };
    canvas.classList.add("dragging");
  });
  listen(canvas, "pointermove", e => {
    if (!pointers.has(e.pointerId)) return;
    const p = point(e); pointers.set(e.pointerId, p);
    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      camera.k = Math.max(minZoom, Math.min(maxZoom, pinch.k * Math.hypot(a.x - b.x, a.y - b.y) / Math.max(1, pinch.distance)));
      camera.x = (a.x + b.x) / 2 - pinch.wx * camera.k; camera.y = (a.y + b.y) / 2 - pinch.wy * camera.k;
      schedule(); persist(); return;
    }
    if (!drag || drag.id !== e.pointerId) return;
    if (Math.hypot(p.x - drag.start.x, p.y - drag.start.y) > 4) drag.moved = true;
    if (drag.moved) {
      const dx = p.x - drag.last.x, dy = p.y - drag.last.y;
      if (drag.node) { drag.node.x += dx / camera.k; drag.node.y += dy / camera.k; }
      else { camera.x += dx; camera.y += dy; }
      suppressClick = true; schedule(); persist();
    }
    drag.last = p;
  });
  const endPointer = e => {
    if (!pointers.has(e.pointerId)) return;
    if (e.type === "pointerup" && drag && !drag.moved) select(drag.node ? drag.node.id : null);
    pointers.delete(e.pointerId); drag = null; pinch = null;
    if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
    canvas.classList.remove("dragging");
  };
  listen(canvas, "pointerup", endPointer); listen(canvas, "pointercancel", endPointer); listen(canvas, "lostpointercapture", endPointer);
  listen(canvas, "wheel", e => { e.preventDefault(); const p = point(e); zoom(Math.exp(-e.deltaY * (e.deltaMode === 1 ? .025 : .0015)), p.x, p.y); }, { passive: false });
  listen(root, "click", e => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.node) { if (e.detail === 0 && !suppressClick) select(b.dataset.node); suppressClick = false; return; }
    if (b.dataset.select) { select(b.dataset.select, true); byId.get(b.dataset.select).el.focus({ preventScroll: true }); return; }
    const action = b.dataset.action;
    if (action === "in") zoom(1.22);
    else if (action === "out") zoom(1 / 1.22);
    else if (action === "fit") fit();
    else if (action === "clear") { const n = byId.get(selected); select(null); (n ? n.el : canvas).focus({ preventScroll: true }); }
    else if (action === "open" && selected && opts.onOpen) opts.onOpen(byId.get(selected));
    else if (action === "copy") {
      const text = ctxGraphMermaid(graph);
      (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(() => toast("Context graph copied"), () => {
        if (!destroyed) modal({ title: "Context graph · Mermaid", body: `<pre>${esc(text)}</pre>`, width: 620 });
      });
    }
  });
  listen(canvas, "keydown", e => {
    // Node buttons retain Enter/Space activation. Alt + arrows can move a focused node without a pointer.
    const button = e.target.closest("[data-node]"), n = button && byId.get(button.dataset.node);
    const delta = { ArrowLeft: [-30, 0], ArrowRight: [30, 0], ArrowUp: [0, -30], ArrowDown: [0, 30] }[e.key];
    if (delta) {
      e.preventDefault();
      if (n && e.altKey) { n.x += delta[0] / camera.k; n.y += delta[1] / camera.k; }
      else { camera.x -= delta[0]; camera.y -= delta[1]; }
      schedule(); persist();
    } else if (e.key === "+" || e.key === "=") { e.preventDefault(); zoom(1.22); }
    else if (e.key === "-") { e.preventDefault(); zoom(1 / 1.22); }
    else if (e.key === "Escape") { e.preventDefault(); select(null); }
    else if (e.key === "0") { e.preventDefault(); fit(); }
    suppressClick = false;
  });
  listen(layer, "focusin", e => {
    const b = e.target.closest("[data-node]"), n = b && byId.get(b.dataset.node);
    if (n && (n.sx < 30 || n.sx > width - 30 || n.sy < 30 || n.sy > height - 55)) {
      camera.x = width / 2 - n.x * camera.k; camera.y = height / 2 - n.y * camera.k; schedule();
    }
  });
  listen(links, "change", () => { ls("ctxgraph-links", links.checked ? "1" : "0"); schedule(); });
  listen(search, "input", () => {
    const q = search.value.trim().toLowerCase(); results.hidden = !q;
    if (!q) return;
    const matches = nodes.filter(n => `${n.label} ${n.path || ""} ${n.role || ""} ${n.tip}`.toLowerCase().includes(q));
    results.innerHTML = `<div class="ctxgraph-result-count">${matches.length} matching nodes${matches.length > 12 ? " · first 12 shown" : ""}</div>` + matches.slice(0, 12).map(n => `<button type="button" data-select="${esc(n.id)}">${esc(n.label)}<span>${esc(n.kind)}</span></button>`).join("");
    status.textContent = `${matches.length} matching nodes`;
  });
  listen(search, "keydown", e => {
    if (e.key === "Escape") { search.value = ""; results.hidden = true; }
    if (e.key === "Enter" && !results.hidden) { e.preventDefault(); const first = results.querySelector("button"); if (first) first.click(); }
    if (e.key === "ArrowDown" && !results.hidden) { e.preventDefault(); const first = results.querySelector("button"); if (first) first.focus(); }
  });
  listen(root, "keydown", e => {
    if (e.key === "Escape") results.hidden = true;
    // The app uses Tab as a navigation chord. Within this interactive graph keep native focus traversal.
    if (e.key === "Tab") e.stopPropagation();
  });
  const resize = () => {
    if (destroyed) return;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h || (w === width && h === height)) return;
    const first = !width; camera.x += (w - width) / 2; camera.y += (h - height) / 2; width = w; height = h;
    if (first) {
      const c = saved.camera;
      if (c && Number.isFinite(c.cx) && Number.isFinite(c.cy) && c.k >= minZoom && c.k <= maxZoom) {
        camera.k = c.k; camera.x = width / 2 - c.cx * c.k; camera.y = height / 2 - c.cy * c.k;
      } else fit();
    }
    schedule();
  };
  const observer = new ResizeObserver(resize); observer.observe(canvas);
  if (document.fonts) document.fonts.ready.then(() => { if (!destroyed) { measureLabels(); schedule(); } });
  listen(window, "pagehide", save);
  showReader(); resize();
  return { fit, destroy() {
    if (destroyed) return;
    destroyed = true; clearTimeout(saveTimer); cancelAnimationFrame(frame); save(); observer.disconnect(); abort.abort();
    for (const id of pointers.keys()) if (canvas.hasPointerCapture(id)) canvas.releasePointerCapture(id);
    pointers.clear();
  } };
}
