// Full-page, relationship-first renderer for the Global Context route only.
// The ordinary Context Map renderer remains the renderer for other graph views.
function contextGlobalKnowledgeGraph(el, graph, opts = {}) {
  const model = opts.knowledgeGraph;
  if (!model) throw new Error("The Global context graph needs its canonical projection");
  const abort = new AbortController();
  const listen = (target, name, fn, options = {}) => target.addEventListener(name, fn, { ...options, signal: abort.signal });
  let saved = {};
  try { saved = JSON.parse(ls(opts.storageKey) || "{}") || {}; } catch (_) { /* invalid local view */ }
  const state = model.stateFrom(saved.state);
  const canonicalIds = new Set(model.canonical.map(node => node.id));
  let selected = canonicalIds.has(saved.selected) ? saved.selected : state.selectedId;
  state.selectedId = selected;
  const kinds = [...new Set(model.knowledge.map(node => node.kind))].sort();
  let shownKinds = new Set(Array.isArray(state.shownKinds) ? state.shownKinds : kinds);
  let width = 0, height = 0, frame = 0, saveTimer = 0, destroyed = false;
  let nodes = [], renderedIds = new Set(), edges = [], visibleState = { objectIds: [], relationshipIds: [], overflowOwners: [] };
  let searchMatches = [], searchOffset = 0, limitMessage = "", projectionNotice = "";
  let suppressPointerClick = false, suppressPointerClickTimer = 0;
  const clearPointerClickSuppression = () => { suppressPointerClick = false; clearTimeout(suppressPointerClickTimer); suppressPointerClickTimer = 0; };
  const swallowSuppressedPointerClick = event => {
    if (!suppressPointerClick || event.detail === 0) return false;
    event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation?.(); clearPointerClickSuppression(); return true;
  };
  const camera = { k: 1, x: 0, y: 0 }, minZoom = .04, maxZoom = 4;
  const byId = model.byId, byRef = model.byRef;
  const labelFor = node => model.resultLabel(node);
  const pageCapacityFor = (canvasWidth, canvasHeight) => canvasWidth < 480 || canvasHeight < 360 ? 4 : canvasWidth < 900 ? 8 : 12;
  const neighborhoodLimitFor = (canvasWidth, canvasHeight) => canvasWidth < 480 || canvasHeight < 360 ? 16 : canvasWidth < 900 ? 32 : 48;

  let kindHtml = "";
  kinds.forEach(kind => { kindHtml += '<label><input type="checkbox" data-kind="' + esc(kind) + '" ' + (shownKinds.has(kind) ? "checked" : "") + "><span>" + esc(globalContextGraphKindLabel(kind)) + "</span></label>"; });
  el.innerHTML = '<div class="ctxgraph global-context-knowledge-graph" aria-label="Global context graph">' +
    '<div class="ctxgraph-bar"><span class="ctxgraph-caption">Context graph <small>Global context</small></span>' +
    '<div class="ctxgraph-search"><input type="search" aria-label="Search all Global context records" placeholder="Search all context…" autocomplete="off" aria-expanded="false" aria-controls="global-context-graph-results">' +
    '<div class="ctxgraph-results" id="global-context-graph-results" role="listbox" aria-label="Global context search results" hidden></div></div>' +
    '<button type="button" data-action="back" class="ctxgraph-back" aria-label="Back to previous focus" title="Back to previous focus" disabled>← Back</button>' +
    '<button type="button" data-action="copy" class="ctxgraph-copy" title="Copy the recorded context graph as Mermaid" aria-label="Copy recorded context graph as Mermaid">' + SVG_MAPCOPY + '</button></div>' +
    '<div class="ctxgraph-types" role="group" aria-label="Filter visible connection kinds">' + kindHtml + '</div>' +
    '<div class="ctxgraph-neighborhood" role="group" aria-label="Connection paging">' +
    '<label for="global-context-page-owner">Connections of</label><select id="global-context-page-owner" data-page-owner aria-label="Choose connections to page"></select>' +
    '<span class="ctxgraph-page-range" data-page-range aria-live="polite"></span>' +
    '<button type="button" data-page="-1" class="ctxgraph-control" aria-label="Previous connection page">‹</button>' +
    '<button type="button" data-page="1" class="ctxgraph-control" aria-label="Next connection page">›</button></div>' +
    '<div class="ctxgraph-canvas" tabindex="0" role="group" aria-label="Global context relationship graph. Drag nodes or the background to move the view; use plus, minus, arrow keys and Fit to navigate.">' +
    '<svg class="ctxgraph-network" role="group" aria-label="Recorded relationship lines; use Tab to select a visible connection"><g class="ctxgraph-edges"></g></svg><div class="ctxgraph-nodes"></div>' +
    '<div class="ctxgraph-empty-state" role="status" aria-live="polite" hidden></div>' +
    '<div class="ctxgraph-zoom"><button type="button" data-action="out" aria-label="Zoom out">−</button><output aria-label="Zoom level">100%</output><button type="button" data-action="in" aria-label="Zoom in">+</button><button type="button" data-action="fit" title="Fit visible connections" aria-label="Fit visible connections">' + SVG_MAPFIT + '</button></div>' +
    '<div class="ctxgraph-reader" aria-label="Selected canonical record" aria-live="polite" hidden></div></div>' +
    '<div class="ctxgraph-footer"><div class="ctxgraph-legend"><span><i class="ctxgraph-business-key"></i>Business</span><span><i class="ctxgraph-person-key"></i>Person</span><span><i class="ctxgraph-concept-key"></i>Concept</span><span><i class="ctxgraph-product-key"></i>Product / platform / project</span><span><i class="ctxgraph-relationship-key"></i>Recorded connection</span></div>' +
    '<span class="ctxgraph-count" aria-live="polite"></span></div><div class="ctxgraph-sr" role="status" aria-live="polite"></div></div>';

  const root = el.querySelector(".ctxgraph"), canvas = root.querySelector(".ctxgraph-canvas"), svg = root.querySelector(".ctxgraph-network");
  const layer = root.querySelector(".ctxgraph-nodes"), edgeLayer = root.querySelector(".ctxgraph-edges");
  const reader = root.querySelector(".ctxgraph-reader"), search = root.querySelector(".ctxgraph-search input");
  const results = root.querySelector(".ctxgraph-results"), status = root.querySelector(".ctxgraph-sr");
  const countStatus = root.querySelector(".ctxgraph-count"), pageOwner = root.querySelector("[data-page-owner]"), pageRange = root.querySelector("[data-page-range]");
  const emptyState = root.querySelector(".ctxgraph-empty-state");

  const save = () => {
    if (!opts.storageKey || !width || !height) return;
    const centerX = (width / 2 - camera.x) / camera.k, centerY = (height / 2 - camera.y) / camera.k;
    state.selectedId = selected; state.shownKinds = [...shownKinds];
    try { ls(opts.storageKey, JSON.stringify({ state, selected, camera: { k: camera.k, cx: centerX, cy: centerY } })); }
    catch (_) { /* private mode or quota: the graph remains interactive */ }
  };
  const persist = () => { clearTimeout(saveTimer); saveTimer = setTimeout(save, 250); };
  const schedule = () => { if (!destroyed && !frame) frame = requestAnimationFrame(() => { frame = 0; if (!destroyed) draw(); }); };
  const page = ownerId => model.page(state, ownerId, shownKinds);
  const canvasLimit = () => neighborhoodLimitFor(width, height);
  const pointFor = node => ({ x: Number(node.x) || 0, y: Number(node.y) || 0 });
  const hash = text => Array.from(String(text)).reduce((value, ch) => ((value * 31) + ch.codePointAt(0)) >>> 0, 0);
  const selectedRelationEndpoints = node => node?.kind === "relationship" ? (Array.isArray(node.item?.endpoints) ? node.item.endpoints : []) : [];

  function layout(visible) {
    const visibleObjects = visible.objectIds.map(id => byId.get(id)).filter(Boolean);
    const visibleObjectIds = new Set(visibleObjects.map(node => node.id));
    const visibleRelations = visible.relationshipIds.map(id => byId.get(id)).filter(Boolean);
    const adjacency = new Map(visibleObjects.map(node => [node.id, new Set()]));
    visibleRelations.forEach(rel => {
      const endpoints = (model.relationshipEndpoints.get(rel.id) || []).filter(id => visibleObjectIds.has(id));
      endpoints.forEach(id => endpoints.forEach(other => { if (other !== id) adjacency.get(id).add(other); }));
    });
    const focusId = state.focusId;
    const distance = new Map(), queue = [];
    if (focusId && visibleObjectIds.has(focusId)) { distance.set(focusId, 0); queue.push(focusId); }
    while (queue.length) {
      const id = queue.shift();
      [...(adjacency.get(id) || [])].sort().forEach(other => {
        if (distance.has(other)) return;
        distance.set(other, distance.get(id) + 1); queue.push(other);
      });
    }
    visibleObjects.forEach(node => { if (!distance.has(node.id)) distance.set(node.id, 1); });
    const previous = state.positions && typeof state.positions === "object" ? state.positions : {};
    const positions = new Map();
    if (focusId && visibleObjectIds.has(focusId)) positions.set(focusId, { x: 0, y: 0 });
    const groups = new Map();
    visibleObjects.filter(node => node.id !== focusId).forEach(node => {
      const ring = distance.get(node.id) || 1;
      if (!groups.has(ring)) groups.set(ring, []);
      groups.get(ring).push(node);
    });
    const pageSize = state.pageSize || 12;
    const baseRadius = pageSize === 4 ? 180 : pageSize === 8 ? 265 : 390;
    for (const [ring, members] of [...groups.entries()].sort((a, b) => a[0] - b[0])) {
      members.sort((a, b) => String(a.item?.ref || a.id).localeCompare(String(b.item?.ref || b.id)));
      const radius = baseRadius + Math.max(0, ring - 1) * 250;
      const locked = members.filter(node => Number.isFinite(previous[node.id]?.x) && Number.isFinite(previous[node.id]?.y));
      locked.forEach(node => positions.set(node.id, { x: previous[node.id].x, y: previous[node.id].y }));
      const fresh = members.filter(node => !positions.has(node.id));
      if (!fresh.length) continue;
      const seed = (hash(focusId || "context") % 360) * Math.PI / 180 + (ring - 1) * .31;
      const slots = Math.max(12, members.length + fresh.length);
      const occupied = locked.map(node => Math.atan2(previous[node.id].y, previous[node.id].x));
      const chosen = [];
      fresh.forEach(node => {
        let best = null;
        for (let index = 0; index < slots; index++) {
          const angle = seed + index * Math.PI * 2 / slots;
          const gap = [...occupied, ...chosen].reduce((min, prior) => {
            const d = Math.abs(((angle - prior + Math.PI * 3) % (Math.PI * 2)) - Math.PI);
            return Math.min(min, d);
          }, Math.PI * 2);
          if (!best || gap > best.gap) best = { angle, gap };
        }
        chosen.push(best.angle);
        positions.set(node.id, { x: Math.cos(best.angle) * radius, y: Math.sin(best.angle) * radius });
      });
    }
    visibleObjects.forEach(node => { const position = positions.get(node.id) || { x: 0, y: 0 }; node.x = position.x; node.y = position.y; });
    const relationPositions = new Map(), pairGroups = new Map();
    visibleRelations.forEach(rel => {
      const endpoints = (model.relationshipEndpoints.get(rel.id) || []).filter(id => visibleObjectIds.has(id));
      if (endpoints.length === 2 && (Array.isArray(rel.item?.endpoints) ? rel.item.endpoints.length : 0) === 2) {
        const key = endpoints.slice().sort().join("\u0000");
        if (!pairGroups.has(key)) pairGroups.set(key, []);
        pairGroups.get(key).push({ rel, endpoints });
      } else if (endpoints.length >= 2) {
        const points = endpoints.map(id => positions.get(id));
        relationPositions.set(rel.id, { x: points.reduce((sum, p) => sum + p.x, 0) / points.length,
          y: points.reduce((sum, p) => sum + p.y, 0) / points.length });
      }
    });
    pairGroups.forEach(group => {
      group.sort((a, b) => a.rel.id.localeCompare(b.rel.id));
      group.forEach((row, index) => {
        const a = positions.get(row.endpoints[0]), b = positions.get(row.endpoints[1]), dx = b.x - a.x, dy = b.y - a.y;
        const length = Math.hypot(dx, dy) || 1, offset = (index - (group.length - 1) / 2) * 20;
        relationPositions.set(row.rel.id, { x: (a.x + b.x) / 2 - dy / length * offset,
          y: (a.y + b.y) / 2 + dx / length * offset });
        row.offset = offset;
      });
    });
    visibleRelations.forEach(rel => {
      const position = relationPositions.get(rel.id) || { x: 0, y: 0 };
      rel.x = position.x; rel.y = position.y;
    });
    const nextPositions = { ...previous };
    visibleObjects.forEach(node => { if (node.id !== focusId) nextPositions[node.id] = pointFor(node); });
    state.positions = nextPositions;
    const junctions = visibleRelations.filter(rel => (Array.isArray(rel.item?.endpoints) ? rel.item.endpoints.length : 0) >= 3);
    const allNodes = [...visibleObjects, ...junctions];
    const visibleEdges = [];
    visibleRelations.forEach(rel => {
      const endpoints = (model.relationshipEndpoints.get(rel.id) || []).filter(id => visibleObjectIds.has(id));
      const completeEligible = model.relationshipEndpoints.get(rel.id) || [];
      if ((Array.isArray(rel.item?.endpoints) ? rel.item.endpoints.length : 0) === 2) {
        const group = pairGroups.get(completeEligible.slice().sort().join("\u0000")) || [];
        const row = group.find(item => item.rel.id === rel.id);
        visibleEdges.push({ id: rel.id + ":line", relationshipId: rel.id, source: endpoints[0], target: endpoints[1], offset: row?.offset || 0, direct: true });
      } else endpoints.forEach(endpointId => visibleEdges.push({ id: rel.id + ":" + endpointId, relationshipId: rel.id,
        source: rel.id, target: endpointId, direct: false }));
    });
    return { nodes: allNodes, visibleObjects, visibleRelations, visibleEdges, positions };
  }

  function makeEntry(node) {
    const entry = document.createElement("div");
    entry.className = "ctxgraph-node-entry";
    entry.dataset.nodeEntry = node.id;
    const button = document.createElement("button");
    button.type = "button"; button.className = "ctxgraph-node " + String(node.kind).replace(/[^a-zA-Z0-9_-]/g, "-");
    button.dataset.node = node.id; button.setAttribute("aria-pressed", String(selected === node.id));
    button.setAttribute("aria-label", labelFor(node) + ", " + globalContextGraphKindLabel(node.kind) + (node.defaultCanvas === false ? ", source or filing record" : ""));
    button.title = node.label;
    const dot = document.createElement("span"); dot.className = "ctxgraph-dot"; dot.setAttribute("aria-hidden", "true"); button.append(dot);
    entry.append(button);
    const label = document.createElement("span"); label.className = "ctxgraph-node-label";
    label.textContent = node.label || node.item?.ref || node.id; label.setAttribute("aria-hidden", "true");
    entry.append(label); entry._label = label;
    return entry;
  }

  function paintPageControls() {
    root.dataset.focusId = state.focusId || "";
    const choices = [...new Set([state.focusId, ...(visibleState.includedOwners || [])].filter(id => model.knowledgeIds.has(id)))];
    pageOwner.replaceChildren(...choices.map(id => {
      const option = document.createElement("option"); option.value = id; option.textContent = model.byId.get(id)?.label || id; return option;
    }));
    const activeOwner = choices.includes(state.pageOwner) ? state.pageOwner : state.focusId;
    pageOwner.value = activeOwner || "";
    const info = activeOwner ? page(activeOwner) : { start: 0, end: 0, total: 0, previous: false, next: false };
    pageRange.textContent = info.total ? (info.start + 1) + "–" + info.end + " of " + info.total : "No recorded connections";
    const previousButton = root.querySelector('[data-page="-1"]'), nextButton = root.querySelector('[data-page="1"]');
    previousButton.disabled = !info.previous; nextButton.disabled = !info.next;
    const backButton = root.querySelector('[data-action="back"]'); backButton.disabled = !state.history.length;
    root.dataset.pageCapacity = String(state.pageSize); root.dataset.canvasWidth = String(width); root.dataset.canvasHeight = String(height);
    root.dataset.visibleLimit = String(canvasLimit());
    root.dataset.overflowOwners = (visibleState.overflowOwners || []).join(",");
    root.dataset.suspendedOwners = (visibleState.suspendedOwners || []).join(",");
  }

  function updateCount() {
    const objectCount = visibleState.objectIds.length, relationCount = visibleState.relationshipIds.length;
    countStatus.textContent = "Objects " + objectCount + "/" + model.knowledgeCount + " · connections " + relationCount + "/" +
      model.graph.counts.eligibleRelationships + " (2+ objects) · records " + model.canonical.length + " searchable / " +
      model.relationshipCount + " relationships";
    const overflow = visibleState.overflowOwners.length, suspended = (visibleState.suspendedOwners || []).length;
    let automaticCapacityMessage = "";
    if (suspended && overflow) automaticCapacityMessage = suspended + " expanded neighborhood" + (suspended === 1 ? " is" : "s are") +
      " temporarily hidden, and part of the current page is outside the visible-node limit. Use a larger view or change pages to explore.";
    else if (suspended) automaticCapacityMessage = suspended + " expanded neighborhood" + (suspended === 1 ? " is" : "s are") +
      " temporarily hidden at this size. Focus an object to explore it, or use a larger view.";
    else if (overflow) automaticCapacityMessage = overflow + " expanded neighborhood" + (overflow === 1 ? " is" : "s are") +
      " held outside the visible-node limit at this size. Focus an object, change pages, or collapse a neighborhood to explore it.";
    const capacityMessage = projectionNotice || automaticCapacityMessage;
    status.textContent = capacityMessage || "Recorded connections are shown from the stored relationship records.";
    const focus = model.byId.get(state.focusId), allNeighbors = focus ? model.neighbors(focus.id) : [], shownNeighbors = focus ? model.neighbors(focus.id, shownKinds) : [];
    const noFilteredFocusConnections = !!focus && allNeighbors.length > 0 && shownNeighbors.length === 0;
    const isolatedFocus = !!focus && allNeighbors.length === 0;
    const visibleNotice = capacityMessage || (noFilteredFocusConnections
      ? "No connections under the active filters for " + (focus.label || "this focus") + ". Change the filters or search all stored records."
      : isolatedFocus ? "No stored eligible connections for " + (focus.label || "this focus") + ". Search or browse the complete stored inventory." : "");
    emptyState.hidden = !!visibleState.objectIds.length && !visibleNotice;
    emptyState.dataset.placement = visibleNotice ? "notice" : "empty";
    if (!visibleState.objectIds.length) emptyState.textContent = model.knowledgeCount
      ? "No eligible objects are available in this view. Search all stored records to open any canonical object or relationship."
      : "No knowledge objects are available in the default graph. Search all stored records to open canonical sources and relationships.";
    else if (visibleNotice) emptyState.textContent = visibleNotice;
    root.dataset.visibleObjects = String(objectCount); root.dataset.visibleRelationships = String(relationCount);
  }

  function makeRenderedEdges(sourceEdges) {
    edgeLayer.replaceChildren();
    return sourceEdges.map(edge => {
      const rel = byId.get(edge.relationshipId), attributes = { class: "ctxgraph-edge " + (edge.direct ? "recorded-line" : "membership"),
        "data-edge": edge.id, "data-relationship": edge.relationshipId };
      if (edge.direct) Object.assign(attributes, { tabindex: "0", role: "button",
        "aria-label": "Recorded relationship " + (rel?.label || edge.relationshipId) + ". Select to read its canonical endpoints." });
      const path = svgEl("path", attributes);
      edgeLayer.append(path); return { ...edge, el: path };
    });
  }

  function rebuild() {
    if (!width || !height) return;
    visibleState = model.visible(state, { shownKinds, maxOrdinaryNodes: canvasLimit() });
    const placement = layout(visibleState);
    nodes = placement.nodes;
    renderedIds = new Set(nodes.map(node => node.id));
    layer.replaceChildren(...nodes.map(makeEntry));
    edges = makeRenderedEdges(placement.visibleEdges);
    paintPageControls(); updateCount(); draw();
  }

  function placeLabels() {
    const canvasBox = canvas.getBoundingClientRect(), prior = [];
    const rows = nodes.map(node => ({ node, entry: layer.querySelector('[data-node-entry="' + CSS.escape(node.id) + '"]') }))
      .filter(row => row.entry?._label);
    const degree = node => model.neighborSets.get(node.id)?.size || model.relationshipEndpoints.get(node.id)?.length || 0;
    rows.sort((a, b) => (a.node.id === state.focusId ? -1 : b.node.id === state.focusId ? 1 :
      selected === a.node.id ? -1 : selected === b.node.id ? 1 : degree(b.node) - degree(a.node) || a.node.id.localeCompare(b.node.id)));
    const reserved = [...layer.querySelectorAll(".ctxgraph-node"), ...root.querySelectorAll(".ctxgraph-zoom button")]
      .map(element => ({ entry: element.closest(".ctxgraph-node-entry"), box: element.getBoundingClientRect() }));
    if (!reader.hidden) reserved.push({ entry: null, box: reader.getBoundingClientRect() });
    const intersects = (a, b) => a.left < b.right + 3 && a.right > b.left - 3 && a.top < b.bottom + 3 && a.bottom > b.top - 3;
    for (const { node, entry } of rows) {
      const label = entry._label;
      label.style.visibility = "visible";
      if (node.kind === "relationship" && selected !== node.id && !entry.matches(":hover") && !entry.querySelector("button:focus")) {
        label.style.visibility = "hidden"; continue;
      }
      const directions = node.id === state.focusId ? ["top", "bottom", "right", "left"] : ["right", "left", "top", "bottom", "top-right", "bottom-right", "top-left", "bottom-left"];
      let best = null;
      directions.forEach(side => {
        label.dataset.side = side;
        const box = label.getBoundingClientRect(); let score = 0;
        if (box.left < canvasBox.left + 2 || box.right > canvasBox.right - 2 || box.top < canvasBox.top + 2 || box.bottom > canvasBox.bottom - 2) score += 50;
        reserved.forEach(item => { if (item.entry !== entry && intersects(box, item.box)) score += 25; });
        prior.forEach(boxOther => { if (intersects(box, boxOther)) score += 30; });
        if (!best || score < best.score) best = { side, box, score };
      });
      label.dataset.side = best.side; label.dataset.collision = String(best.score > 0);
      prior.push(label.getBoundingClientRect());
    }
  }

  function draw() {
    if (!width || !height) return;
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    nodes.forEach(node => {
      node.sx = node.x * camera.k + camera.x; node.sy = node.y * camera.k + camera.y;
      const entry = layer.querySelector('[data-node-entry="' + CSS.escape(node.id) + '"]');
      if (!entry) return;
      entry.style.left = node.sx + "px"; entry.style.top = node.sy + "px";
      const button = entry.querySelector("[data-node]");
      button.classList.toggle("selected", selected === node.id); button.setAttribute("aria-pressed", String(selected === node.id));
    });
    edges.forEach(edge => {
      const a = byId.get(edge.source), b = byId.get(edge.target), rel = byId.get(edge.relationshipId);
      if (!a || !b || !rel || !renderedIds.has(a.id) || !renderedIds.has(b.id) || (!edge.direct && !renderedIds.has(rel.id))) { edge.el.style.display = "none"; return; }
      let d;
      if (edge.direct) {
        const dx = b.sx - a.sx, dy = b.sy - a.sy, length = Math.hypot(dx, dy) || 1;
        const offset = edge.offset * camera.k, mx = (a.sx + b.sx) / 2 - dy / length * offset, my = (a.sy + b.sy) / 2 + dx / length * offset;
        d = "M" + a.sx + " " + a.sy + " Q" + mx + " " + my + " " + b.sx + " " + b.sy;
      } else d = "M" + rel.sx + " " + rel.sy + " L" + b.sx + " " + b.sy;
      edge.el.setAttribute("d", d); edge.el.classList.toggle("hot", selected === rel.id || selected === a.id || selected === b.id);
      edge.el.style.display = "";
    });
    placeLabels();
    root.querySelector(".ctxgraph-zoom output").textContent = Math.round(camera.k * 100) + "%";
  }

  function relationListFor(node) {
    return (model.incident.get(node.id) || []).map(id => byId.get(id)).filter(Boolean)
      .sort((a, b) => String(a.label).localeCompare(String(b.label)) || a.id.localeCompare(b.id));
  }
  function renderObjectReader(node) {
    const item = node.item || {}, role = node.displayRole;
    const connected = relationListFor(node);
    const note = role === "knowledge" ? "" : '<p class="ctxgraph-display-note">' + esc(node.displayReason) + '</p>';
    const tags = Array.isArray(item.tags) && item.tags.length ? '<div class="ctxgraph-tags">' + item.tags.map(tag => '<span>' + esc(tag) + '</span>').join("") + '</div>' : "";
    const summary = item.summary ? '<p>' + esc(item.summary) + '</p>' : "";
    const definition = item.definition ? '<div class="ctxgraph-definition"><strong>Definition</strong><p>' + esc(item.definition) + '</p></div>' : "";
    const focusButton = role === "knowledge" ? '<button type="button" class="ctxgraph-reader-action" data-action="focus">Focus here</button>' : "";
    const neighbors = role === "knowledge" ? model.neighbors(node.id, shownKinds).length : 0;
    const isExpanded = state.expanded.includes(node.id);
    const canChangeExpansion = isExpanded || renderedIds.has(node.id);
    const expandButton = role === "knowledge" && canChangeExpansion && neighbors && node.id !== state.focusId ? '<button type="button" class="ctxgraph-reader-action" data-action="' + (isExpanded ? "collapse" : "expand") + '">' +
      (isExpanded ? "Collapse connections" : "Expand connections") + ' · ' + neighbors + '</button>' : "";
    const outsideViewNote = role === "knowledge" && !renderedIds.has(node.id)
      ? '<p class="ctxgraph-limit-note" role="status">This record is outside the current view. Use Focus here to open its connections.</p>' : "";
    const connectedHtml = connected.length ? '<section class="ctxgraph-reader-section"><h4>Recorded connection records (' + connected.length + ')</h4>' + connected.map(rel => {
      const reason = rel.bookkeeping ? "Filing/projection record · detail only" : rel.defaultCanvas ? "Substantive recorded connection" : "Source/collection-related record · detail only";
      return '<div class="ctxgraph-related-record"><button type="button" class="quiet" data-select="' + esc(rel.id) + '">' + esc(labelFor(rel)) + '</button><small>' + esc(reason) +
        ((rel.item?.tags || []).length ? ' · ' + esc(rel.item.tags.join(" · ")) : "") + '</small></div>';
    }).join("") + '</section>' : '<p class="ctxgraph-no-records">No relationship records reference this object.</p>';
    reader.hidden = false;
    reader.innerHTML = '<div class="ctxgraph-reader-head"><div><div class="ctxgraph-kind">' + esc(globalContextGraphKindLabel(node.kind)) +
      (role !== "knowledge" ? ' · Stored source' : "") + '</div><h3>' + esc(labelFor(node)) + '</h3></div>' +
      '<button type="button" data-action="clear" class="ctxgraph-reader-close" aria-label="Close record details">×</button></div>' + note + summary + definition + tags +
      outsideViewNote + (limitMessage ? '<p class="ctxgraph-limit-note" role="status">' + esc(limitMessage) + '</p>' : "") +
      '<div class="ctxgraph-reader-actions">' + focusButton + expandButton + (item.ref ? '<button type="button" class="ctxgraph-open" data-action="open">Open canonical record →</button>' : "") +
      '<span>' + connected.length + ' recorded relationship' + (connected.length === 1 ? "" : "s") + '</span></div>' + connectedHtml + '<div class="ctxgraph-reviews"></div>';
    opts.onSelect?.(node, reader.querySelector(".ctxgraph-reviews"));
  }
  function renderRelationshipReader(node) {
    const item = node.item || {}, endpoints = Array.isArray(item.endpoints) ? item.endpoints : [];
    const endpointRows = endpoints.map((ref, index) => {
      const endpoint = typeof ref === "string" ? byRef.get(ref) : null;
      const label = endpoint?.label || (typeof ref === "string" ? ref : JSON.stringify(ref));
      const action = endpoint ? '<button type="button" class="quiet" data-select="' + esc(endpoint.id) + '">' + esc(label) + '</button>' : '<span>' + esc(label) + ' <small>Unavailable in this snapshot</small></span>';
      return '<div class="ctxgraph-endpoint"><span>' + (index + 1) + '.</span>' + action + '</div>';
    }).join("");
    const definition = item.definition ? '<div class="ctxgraph-definition"><strong>Definition</strong><p>' + esc(item.definition) + '</p></div>' : "";
    const summary = item.summary ? '<p>' + esc(item.summary) + '</p>' : "";
    const tags = Array.isArray(item.tags) && item.tags.length ? '<div class="ctxgraph-tags">' + item.tags.map(tag => '<span>' + esc(tag) + '</span>').join("") + '</div>' : "";
    const source = item.source && typeof item.source === "object" ? Object.entries(item.source).map(pair => pair[0] + ": " +
      (typeof pair[1] === "string" ? pair[1] : JSON.stringify(pair[1]))).join(" · ") : "";
    const listed = (model.relationshipEndpoints.get(node.id) || []).filter(id => renderedIds.has(id)).length;
    const offCanvas = Math.max(0, endpoints.length - listed);
    const statusLine = node.bookkeeping ? "Filing/projection record; excluded from the default canvas." : node.defaultCanvas
      ? "Recorded relationship; shown when at least two eligible endpoints are in the current neighborhood." : "Stored relationship; too few eligible endpoints for a default graph connection.";
    const unavailable = item.unavailableEndpointCount || 0, malformed = item.malformedEndpointCount || 0;
    reader.hidden = false;
    reader.innerHTML = '<div class="ctxgraph-reader-head"><div><div class="ctxgraph-kind">Relationship · Stored record</div><h3>' + esc(labelFor(node)) + '</h3></div>' +
      '<button type="button" data-action="clear" class="ctxgraph-reader-close" aria-label="Close relationship details">×</button></div>' +
      '<p class="ctxgraph-display-note">' + esc(statusLine) + '</p>' + summary + definition + tags +
      '<section class="ctxgraph-reader-section"><h4>Canonical endpoints (' + endpoints.length + ')</h4>' + (endpointRows || '<p>No endpoints recorded.</p>') +
      (offCanvas ? '<small>' + offCanvas + ' endpoint' + (offCanvas === 1 ? " is" : "s are") + ' outside the visible neighborhood.</small>' : "") +
      (unavailable ? '<small>' + unavailable + ' stored endpoint' + (unavailable === 1 ? " is" : "s are") + ' unavailable in this snapshot.</small>' : "") +
      (malformed ? '<small>' + malformed + ' endpoint value' + (malformed === 1 ? " is" : "s are") + ' malformed in the stored record.</small>' : "") + '</section>' +
      (source ? '<p class="ctxgraph-provenance">Recorded source: ' + esc(source) + '</p>' : "") +
      '<div class="ctxgraph-reader-actions">' + (item.ref ? '<button type="button" class="ctxgraph-open" data-action="open">Open canonical relationship →</button>' : "") +
      '<span>' + endpoints.length + ' stored endpoint' + (endpoints.length === 1 ? "" : "s") + '</span></div><div class="ctxgraph-reviews"></div>';
    opts.onSelect?.(node, reader.querySelector(".ctxgraph-reviews"));
  }
  function showReader() {
    if (!selected || !byId.has(selected)) { reader.hidden = true; reader.replaceChildren(); schedule(); return; }
    const node = byId.get(selected);
    if (node.kind === "relationship") renderRelationshipReader(node); else renderObjectReader(node);
    status.textContent = node.label + (node.kind === "relationship" ? ", relationship record" : ", canonical object record");
    draw();
  }

  function select(id, { reveal = false, focus = false } = {}) {
    selected = canonicalIds.has(id) ? id : null;
    state.selectedId = selected; limitMessage = ""; projectionNotice = "";
    if (selected && reveal) model.reveal(state, selected, shownKinds);
    if (selected && focus && model.knowledgeIds.has(selected)) { focusHere(selected, false); return; }
    rebuild(); showReader();
    if (!selected) { status.textContent = "Record selection cleared"; canvas.focus({ preventScroll: true }); }
    persist();
  }

  function centerOnFocus() {
    const node = byId.get(state.focusId);
    if (!node || !Number.isFinite(node.x)) return;
    camera.x = width / 2 - node.x * camera.k; camera.y = height / 2 - node.y * camera.k; schedule();
  }
  function focusHere(id, pushHistory = true) {
    const oldHistory = state.history.slice();
    if (!model.setFocus(state, id)) return;
    if (!pushHistory) state.history = oldHistory;
    selected = id; state.selectedId = id; limitMessage = ""; projectionNotice = "";
    rebuild(); showReader(); centerOnFocus(); persist();
    requestAnimationFrame(() => layer.querySelector('[data-node="' + CSS.escape(id) + '"]')?.focus({ preventScroll: true }));
  }
  function backFocus() {
    let prior = null;
    while (state.history.length && !prior) { const candidate = state.history.pop(); if (model.knowledgeIds.has(candidate)) prior = candidate; }
    if (!prior) return;
    state.focusId = prior; state.pageOwner = prior; state.expanded = [prior]; state.pages[prior] = 0; state.positions = {};
    selected = prior; state.selectedId = prior; limitMessage = ""; projectionNotice = ""; rebuild(); showReader(); centerOnFocus(); persist();
  }
  function expandConnections() {
    if (!selected || !model.knowledgeIds.has(selected)) return;
    const neighbors = model.neighbors(selected, shownKinds);
    if (!neighbors.length) { limitMessage = "No substantive recorded connections are available for this object."; showReader(); return; }
    if (state.expanded.includes(selected)) {
      if (selected === state.focusId) return;
      state.expanded = state.expanded.filter(id => id !== selected);
      if (state.pageOwner === selected) state.pageOwner = state.focusId;
      limitMessage = ""; projectionNotice = ""; rebuild(); showReader(); persist(); return;
    }
    const draft = JSON.parse(JSON.stringify(state));
    draft.expanded.push(selected); draft.pageOwner = selected; draft.pages[selected] = Number(draft.pages[selected]) || 0;
    const visible = model.visible(draft, { shownKinds, maxOrdinaryNodes: canvasLimit() });
    if (visible.overflowOwners.length || visible.objectIds.length > canvasLimit()) {
      limitMessage = "This expansion would exceed the visible-node limit at this size. Use Focus here to explore its connections, or collapse another neighborhood.";
      projectionNotice = limitMessage; updateCount(); showReader(); return;
    }
    Object.assign(state, draft); limitMessage = ""; projectionNotice = ""; rebuild(); showReader(); persist();
  }

  function renderSearch() {
    const end = Math.min(searchOffset + 12, searchMatches.length);
    const buttons = searchMatches.slice(searchOffset, end).map(node => '<button type="button" role="option" aria-selected="false" data-select="' + esc(node.id) + '">' +
      esc(labelFor(node)) + '<span>' + esc(globalContextGraphKindLabel(node.kind)) +
      (node.displayRole && node.displayRole !== "knowledge" ? ' · ' + esc(node.displayRole.replace(/-/g, " ")) : "") + '</span></button>').join("");
    const pager = searchMatches.length > 12 ? '<div class="ctxgraph-search-pager"><span>' + (searchOffset + 1) + "–" + end + " of " + searchMatches.length +
      '</span><button type="button" class="ctxgraph-control" data-search-page="-1" ' + (searchOffset === 0 ? "disabled" : "") + ' aria-label="Previous search results">‹</button>' +
      '<button type="button" class="ctxgraph-control" data-search-page="1" ' + (end >= searchMatches.length ? "disabled" : "") + ' aria-label="Next search results">›</button></div>' : "";
    results.innerHTML = '<div class="ctxgraph-result-count">' + searchMatches.length + ' matching stored records</div>' +
      (buttons || '<div class="ctxgraph-result-count">No matching stored records.</div>') + pager;
  }

  root.querySelectorAll("[data-kind]").forEach(input => listen(input, "change", () => {
    if (input.checked) shownKinds.add(input.dataset.kind); else shownKinds.delete(input.dataset.kind);
    if (state.shownKinds) state.shownKinds = [...shownKinds];
    limitMessage = ""; projectionNotice = "";
    rebuild(); if (selected) showReader(); persist();
  }));
  listen(pageOwner, "change", () => {
    state.pageOwner = model.knowledgeIds.has(pageOwner.value) ? pageOwner.value : state.focusId;
    limitMessage = ""; projectionNotice = ""; paintPageControls(); updateCount(); persist();
  });
  listen(root, "click", event => {
    if (swallowSuppressedPointerClick(event)) return;
    const button = event.target.closest("button"); if (!button) return;
    if (button.dataset.node) {
      const id = button.dataset.node;
      select(id, { reveal: true }); return;
    }
    if (button.dataset.select) { select(button.dataset.select, { reveal: true }); search.value = ""; results.hidden = true; search.setAttribute("aria-expanded", "false"); return; }
    if (button.dataset.page) {
      const owner = pageOwner.value || state.focusId, info = page(owner), nextStart = info.start + Number(button.dataset.page) * info.pageSize;
      const draft = JSON.parse(JSON.stringify(state)); draft.pageOwner = owner; model.setPage(draft, owner, nextStart, shownKinds);
      const preview = model.visible(draft, { shownKinds, maxOrdinaryNodes: canvasLimit() });
      if (preview.overflowOwners.length || preview.objectIds.length > canvasLimit()) {
        projectionNotice = "That page would exceed the visible-node limit while other neighborhoods are expanded. Focus an object or collapse a neighborhood, then try again.";
        limitMessage = projectionNotice; updateCount(); if (selected) showReader(); return;
      }
      Object.assign(state, draft); limitMessage = ""; projectionNotice = ""; rebuild(); if (selected) showReader(); persist(); return;
    }
    if (button.dataset.searchPage) {
      searchOffset = Math.max(0, Math.min(Math.floor((searchMatches.length - 1) / 12) * 12, searchOffset + Number(button.dataset.searchPage) * 12));
      renderSearch(); results.querySelector("button[role=option]")?.focus({ preventScroll: true }); return;
    }
    if (button.dataset.action === "back") { backFocus(); return; }
    if (button.dataset.action === "focus" && selected && model.knowledgeIds.has(selected)) { focusHere(selected); return; }
    if (button.dataset.action === "expand") { expandConnections(); return; }
    if (button.dataset.action === "collapse" && selected && selected !== state.focusId) {
      state.expanded = state.expanded.filter(id => id !== selected); if (state.pageOwner === selected) state.pageOwner = state.focusId;
      limitMessage = ""; projectionNotice = ""; rebuild(); showReader(); persist(); return;
    }
    if (button.dataset.action === "in") zoom(1.2);
    else if (button.dataset.action === "out") zoom(1 / 1.2);
    else if (button.dataset.action === "fit") fit();
    else if (button.dataset.action === "clear") select(null);
    else if (button.dataset.action === "open" && selected && opts.onOpen) opts.onOpen(byId.get(selected));
    else if (button.dataset.action === "copy") {
      const text = ctxGraphMermaid(graph);
      (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(() => toast("Context graph copied"), () => {
        if (!destroyed) modal({ title: "Recorded context graph · Mermaid", body: "<pre>" + esc(text) + "</pre>", width: 620 });
      });
    }
  });
  listen(edgeLayer, "click", event => {
    if (swallowSuppressedPointerClick(event)) return;
    const edge = event.target.closest("path[data-relationship]");
    if (edge) select(edge.dataset.relationship, { reveal: true });
  });
  listen(edgeLayer, "keydown", event => {
    const edge = event.target.closest("path[data-relationship][role=button]");
    if (edge && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); select(edge.dataset.relationship, { reveal: true }); }
  });
  listen(search, "input", () => {
    const query = search.value.trim().toLocaleLowerCase();
    searchMatches = query ? model.searchNodes.filter(node => model.searchText(node).includes(query)) : [];
    searchOffset = 0; results.hidden = !query; search.setAttribute("aria-expanded", String(!!query));
    if (query) { renderSearch(); status.textContent = searchMatches.length + " matching stored records"; }
  });
  listen(search, "keydown", event => {
    if (event.key === "Escape") { search.value = ""; searchMatches = []; results.hidden = true; search.setAttribute("aria-expanded", "false"); }
    else if (event.key === "Enter" && searchMatches.length) { event.preventDefault(); select(searchMatches[searchOffset].id, { reveal: true }); }
    else if (event.key === "ArrowDown" && !results.hidden) { event.preventDefault(); results.querySelector("button[role=option]")?.focus(); }
  });
  listen(canvas, "keydown", event => {
    const delta = { ArrowLeft: [-30, 0], ArrowRight: [30, 0], ArrowUp: [0, -30], ArrowDown: [0, 30] }[event.key];
    if (delta) { event.preventDefault(); camera.x -= delta[0]; camera.y -= delta[1]; schedule(); persist(); }
    else if (event.key === "+" || event.key === "=") { event.preventDefault(); zoom(1.2); }
    else if (event.key === "-") { event.preventDefault(); zoom(1 / 1.2); }
    else if (event.key === "0") { event.preventDefault(); fit(); }
    else if (event.key === "Escape") select(null);
  });
  listen(root, "keydown", event => { if (event.key === "Tab") event.stopPropagation(); });

  function zoom(factor, x = width / 2, y = height / 2) {
    const k = Math.max(minZoom, Math.min(maxZoom, camera.k * factor)), ratio = k / camera.k;
    camera.x = x - (x - camera.x) * ratio; camera.y = y - (y - camera.y) * ratio; camera.k = k; schedule(); persist();
  }
  function boxes() {
    return [...layer.querySelectorAll(".ctxgraph-node,.ctxgraph-node-label")].filter(item => getComputedStyle(item).visibility !== "hidden")
      .map(item => item.getBoundingClientRect());
  }
  function fit() {
    if (!nodes.length || !width || !height) return;
    const gutter = Math.min(56, Math.max(20, Math.min(width, height) * .1));
    camera.k = 1; camera.x = 0; camera.y = 0; draw();
    for (let iteration = 0; iteration < 3; iteration++) {
      const rects = boxes(); if (!rects.length) return;
      const canvasBox = canvas.getBoundingClientRect();
      const x0 = Math.min(...rects.map(rect => rect.left - canvasBox.left)), x1 = Math.max(...rects.map(rect => rect.right - canvasBox.left));
      const y0 = Math.min(...rects.map(rect => rect.top - canvasBox.top)), y1 = Math.max(...rects.map(rect => rect.bottom - canvasBox.top));
      const factor = Math.min(1.35, (width - gutter * 2) / Math.max(1, x1 - x0), (height - gutter * 2) / Math.max(1, y1 - y0));
      const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
      const worldCenterX = (cx - camera.x) / camera.k, worldCenterY = (cy - camera.y) / camera.k;
      camera.k = Math.max(minZoom, Math.min(maxZoom, camera.k * factor));
      camera.x = width / 2 - worldCenterX * camera.k; camera.y = height / 2 - worldCenterY * camera.k;
      draw();
    }
    persist();
  }
  function pointerPoint(event) { const rect = canvas.getBoundingClientRect(); return { x: event.clientX - rect.left, y: event.clientY - rect.top }; }
  const pointers = new Map(), capturedPointers = new Set(); let drag = null, pinch = null;
  const capturePointer = pointerId => {
    try { canvas.setPointerCapture(pointerId); capturedPointers.add(pointerId); } catch (_) { /* pointer ended during a route change */ }
  };
  const releasePointer = pointerId => {
    try { if (canvas.hasPointerCapture(pointerId)) canvas.releasePointerCapture(pointerId); } catch (_) { /* browser already released it */ }
    capturedPointers.delete(pointerId);
  };
  const suppressNextPointerClick = () => {
    clearPointerClickSuppression(); suppressPointerClick = true;
    suppressPointerClickTimer = setTimeout(clearPointerClickSuppression, 500);
  };
  listen(window, "pointerdown", clearPointerClickSuppression, { capture: true });
  listen(canvas, "pointerdown", event => {
    if (event.button !== 0 || event.target.closest(".ctxgraph-zoom,.ctxgraph-reader,.ctxgraph-control,.ctxgraph-empty-state")) return;
    if (pointers.size >= 2) return;
    const p = pointerPoint(event); pointers.set(event.pointerId, p);
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { distance: Math.hypot(a.x - b.x, a.y - b.y), k: camera.k,
        wx: ((a.x + b.x) / 2 - camera.x) / camera.k, wy: ((a.y + b.y) / 2 - camera.y) / camera.k,
        cx: (a.x + b.x) / 2, cy: (a.y + b.y) / 2, moved: false };
      pointers.forEach((_, pointerId) => capturePointer(pointerId));
      drag = null; return;
    }
    const nodeButton = event.target.closest("[data-node]");
    drag = { id: event.pointerId, start: p, last: p, node: nodeButton?.dataset.node || null, moved: false };
  });
  listen(window, "pointermove", event => {
    if (!pointers.has(event.pointerId)) return;
    const p = pointerPoint(event); pointers.set(event.pointerId, p);
    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const centerX = (a.x + b.x) / 2, centerY = (a.y + b.y) / 2;
      if (Math.abs(Math.hypot(a.x - b.x, a.y - b.y) - pinch.distance) > 4 || Math.hypot(centerX - pinch.cx, centerY - pinch.cy) > 4)
        pinch.moved = true;
      if (!pinch.moved) return;
      camera.k = Math.max(minZoom, Math.min(maxZoom, pinch.k * Math.hypot(a.x - b.x, a.y - b.y) / Math.max(1, pinch.distance)));
      camera.x = centerX - pinch.wx * camera.k; camera.y = centerY - pinch.wy * camera.k; schedule(); persist(); return;
    }
    if (!drag || drag.id !== event.pointerId) return;
    if (!drag.moved && Math.hypot(p.x - drag.start.x, p.y - drag.start.y) > 4) {
      drag.moved = true; capturePointer(event.pointerId); canvas.classList.add("dragging");
    }
    if (!drag.moved) { drag.last = p; return; }
    if (drag.moved && drag.node) {
      const node = byId.get(drag.node);
      if (node && model.knowledgeIds.has(node.id)) {
        node.x += (p.x - drag.last.x) / camera.k; node.y += (p.y - drag.last.y) / camera.k;
        state.positions[node.id] = pointFor(node);
      }
      draw(); persist();
    } else if (drag.moved && !drag.node) { camera.x += p.x - drag.last.x; camera.y += p.y - drag.last.y; draw(); persist(); }
    drag.last = p;
  });
  const endPointer = event => {
    if (!pointers.has(event.pointerId)) return;
    const moved = !!pinch || !!(drag?.id === event.pointerId && drag.moved);
    const active = [...pointers.keys()];
    if (moved) suppressNextPointerClick();
    pointers.clear(); drag = null; pinch = null; canvas.classList.remove("dragging");
    active.forEach(releasePointer);
  };
  const cancelPointers = () => {
    if (!pointers.size) return;
    if (pinch || drag?.moved) suppressNextPointerClick();
    const active = [...pointers.keys()];
    pointers.clear(); drag = null; pinch = null; canvas.classList.remove("dragging"); active.forEach(releasePointer);
  };
  listen(window, "pointerup", endPointer); listen(window, "pointercancel", endPointer);
  listen(canvas, "lostpointercapture", endPointer); listen(window, "blur", cancelPointers);
  listen(document, "visibilitychange", () => { if (document.hidden) cancelPointers(); });
  listen(canvas, "wheel", event => { event.preventDefault(); const p = pointerPoint(event); zoom(Math.exp(-event.deltaY * (event.deltaMode === 1 ? .025 : .0015)), p.x, p.y); }, { passive: false });

  const resize = () => {
    if (destroyed) return;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h || (w === width && h === height)) return;
    const first = width === 0;
    camera.x += (w - width) / 2; camera.y += (h - height) / 2; width = w; height = h;
    root.dataset.canvasWidth = String(width); root.dataset.canvasHeight = String(height);
    model.setCapacity(state, pageCapacityFor(w, h), selected, shownKinds);
    limitMessage = ""; projectionNotice = "";
    rebuild();
    if (selected) showReader();
    if (first) {
      const savedCamera = saved.camera;
      if (savedCamera && Number.isFinite(savedCamera.cx) && Number.isFinite(savedCamera.cy) && Number.isFinite(savedCamera.k) && savedCamera.k >= minZoom && savedCamera.k <= maxZoom) {
        camera.k = savedCamera.k; camera.x = width / 2 - savedCamera.cx * camera.k; camera.y = height / 2 - savedCamera.cy * camera.k; draw();
      } else fit();
    }
  };
  const observer = new ResizeObserver(resize); observer.observe(canvas);
  listen(window, "pagehide", save);
  if (selected) model.reveal(state, selected, shownKinds);
  rebuild(); showReader(); resize();
  return { fit, destroy() {
    if (destroyed) return;
    destroyed = true; clearTimeout(saveTimer); cancelAnimationFrame(frame); save(); observer.disconnect(); abort.abort();
    for (const pointerId of [...capturedPointers]) releasePointer(pointerId);
    pointers.clear(); drag = null; pinch = null; canvas.classList.remove("dragging"); clearPointerClickSuppression();
  } };
}
