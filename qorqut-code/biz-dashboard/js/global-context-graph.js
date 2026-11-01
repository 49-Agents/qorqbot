// Visual projection of the published Context graph for the relationship explorer. It is built directly from the
// published node and relationship DTOs (context_home nodes carrying their relationships); the canonical records stay
// unchanged and open by their exact node:/edge: references. A relationship endpoint that is not among the loaded nodes
// becomes a distinct placeholder that carries only what the endpoint DTO itself states.
const GLOBAL_CONTEXT_GRAPH_PAGE_SIZES = Object.freeze([4, 8, 12]);

function contextPublishedGraphModel(homeNodes, totals = null) {
  const nodes = Array.isArray(homeNodes) ? homeNodes : [];
  const clip = (text, size) => { const value = String(text || ""); return value.length > size ? value.slice(0, size - 1).trimEnd() + "…" : value; };
  const canonical = [], byId = new Map(), byRef = new Map();
  const add = node => { canonical.push(node); byId.set(node.id, node); byRef.set(node.item.ref, node); return node; };
  nodes.forEach(dto => {
    if (!dto || typeof dto.ref !== "string" || byId.has(dto.ref)) return;
    add({ id: dto.ref, kind: "node", label: clip(dto.summary, 60) || dto.ref, tip: String(dto.summary || ""), color: "#849a75",
      displayRole: "knowledge", displayReason: "",
      item: { type: "node", id: dto.ref, ref: dto.ref, kind: "node", summary: String(dto.summary || ""), attributes: dto.attributes || {},
        truncated: dto.truncated === true, relationshipCount: dto.relationship_count, relationshipsTruncated: dto.relationships_truncated === true } });
  });
  const edges = new Map();
  nodes.forEach(dto => (Array.isArray(dto?.relationships) ? dto.relationships : []).forEach(edge => {
    if (edge && typeof edge.ref === "string" && !edges.has(edge.ref)) edges.set(edge.ref, edge);
  }));
  const placeholder = endpoint => {
    if (byId.has(endpoint.ref)) return;
    add({ id: endpoint.ref, kind: "endpoint", label: endpoint.summary == null ? endpoint.ref : clip(endpoint.summary, 60), tip: String(endpoint.summary || ""),
      color: "#aaaaaa", displayRole: "knowledge", displayReason: "",
      item: { type: "endpoint", id: endpoint.ref, ref: endpoint.ref, kind: "node", placeholder: true, summary: endpoint.summary == null ? "" : String(endpoint.summary),
        truncated: endpoint.truncated === true, attributes: {} } });
  };
  edges.forEach(edge => { placeholder(edge.from); placeholder(edge.to); });
  const objects = canonical.slice();
  const relationships = [];
  const incident = new Map(canonical.map(node => [node.id, []]));
  const membership = [];
  const relationshipEndpoints = new Map(), neighborSets = new Map(objects.map(node => [node.id, new Set()]));
  edges.forEach(edge => {
    const selfLoop = edge.from.ref === edge.to.ref;
    const refs = selfLoop ? [edge.from.ref] : [edge.from.ref, edge.to.ref];
    const node = { id: edge.ref, kind: "relationship", label: clip(edge.summary, 60) || edge.ref, tip: String(edge.summary || ""), color: "#b69861",
      displayRole: "relationship", displayReason: "", defaultCanvas: true, bookkeeping: false, selfLoop,
      item: { type: "relationship", id: edge.ref, ref: edge.ref, kind: "edge", summary: String(edge.summary || ""), attributes: edge.attributes || {},
        truncated: edge.truncated === true, endpoints: refs, from: edge.from.ref, to: edge.to.ref, unavailableEndpointCount: 0, malformedEndpointCount: 0 } };
    add(node); relationships.push(node);
    relationshipEndpoints.set(node.id, refs.slice());
    refs.forEach((ref, index) => {
      incident.get(ref).push(node.id);
      membership.push({ id: `endpoint:${edge.ref}:${index}`, source: node.id, target: ref, kind: "endpoint", role: "endpoint", membership: true });
    });
    if (!selfLoop) { neighborSets.get(refs[0]).add(refs[1]); neighborSets.get(refs[1]).add(refs[0]); }
  });
  incident.forEach(list => list.sort((a, b) => a.localeCompare(b)));
  const knowledge = objects, knowledgeIds = new Set(knowledge.map(node => node.id));
  const graph = {
    rootId: null, nodes: canonical.slice(), edges: membership,
    counts: { objects: knowledge.length, relationships: relationships.length, endpointLinks: membership.length, eligibleRelationships: relationships.length,
      storedObjects: knowledge.length, storedRelationships: relationships.length, storedEndpointLinks: membership.length, storedRecords: canonical.length }
  };
  const byLabel = new Map();
  canonical.forEach(node => {
    const key = String(node.label || "").trim().toLocaleLowerCase();
    if (!byLabel.has(key)) byLabel.set(key, []);
    byLabel.get(key).push(node.id);
  });
  const resultLabel = node => {
    const key = String(node.label || "").trim().toLocaleLowerCase(), duplicates = byLabel.get(key) || [];
    return duplicates.length < 2 ? node.label : `${node.label} · ${node.item.ref}`;
  };
  const neighbors = (id, shownKinds = null) => [...(neighborSets.get(id) || [])]
    .map(otherId => byId.get(otherId)).filter(node => node && (!shownKinds || shownKinds.has(node.kind)))
    .sort((a, b) => String(a.item.ref).localeCompare(String(b.item.ref)));
  const stateFrom = saved => {
    const validId = id => typeof id === "string" && byId.has(id);
    const savedFocus = validId(saved?.focusId) && knowledgeIds.has(saved.focusId) ? saved.focusId : null;
    const degree = node => (incident.get(node.id)?.length || 0);
    const defaultFocus = knowledge.filter(node => node.kind === "node").sort((a, b) => degree(b) - degree(a) || a.id.localeCompare(b.id))[0]?.id ||
      knowledge[0]?.id || null;
    const focusId = savedFocus || defaultFocus;
    const expanded = Array.isArray(saved?.expanded) ? saved.expanded.filter(id => knowledgeIds.has(id)) : [];
    if (focusId && !expanded.includes(focusId)) expanded.unshift(focusId);
    const pageSize = GLOBAL_CONTEXT_GRAPH_PAGE_SIZES.includes(saved?.pageSize) ? saved.pageSize : 12;
    const savedPages = saved?.pages && typeof saved.pages === "object" && !Array.isArray(saved.pages) ? saved.pages : {};
    const pages = {};
    Object.entries(savedPages).forEach(([id, start]) => { if (knowledgeIds.has(id) && Number.isSafeInteger(start) && start >= 0) pages[id] = start; });
    const savedPositions = saved?.positions && typeof saved.positions === "object" && !Array.isArray(saved.positions) ? saved.positions : {};
    const positions = {};
    Object.entries(savedPositions).forEach(([id, point]) => {
      if (knowledgeIds.has(id) && Number.isFinite(point?.x) && Number.isFinite(point?.y) && Math.abs(point.x) < 1e7 && Math.abs(point.y) < 1e7)
        positions[id] = { x: point.x, y: point.y };
    });
    const history = Array.isArray(saved?.history) ? saved.history.filter(id => knowledgeIds.has(id)).slice(-20) : [];
    const selectedId = validId(saved?.selectedId) ? saved.selectedId : null;
    const pageOwner = knowledgeIds.has(saved?.pageOwner) ? saved.pageOwner : focusId;
    return { focusId, selectedId, expanded: [...new Set(expanded)], pages, pageOwner, pageSize, positions, history,
      shownKinds: Array.isArray(saved?.shownKinds) ? saved.shownKinds.filter(kind => knowledge.some(node => node.kind === kind)) : null };
  };
  const page = (state, ownerId, shownKinds = null) => {
    const list = neighbors(ownerId, shownKinds), size = state.pageSize || 12, raw = Number(state.pages[ownerId]) || 0;
    const lastStart = Math.max(0, Math.floor((list.length - 1) / size) * size);
    const start = Math.max(0, Math.min(Math.floor(raw / size) * size, lastStart));
    return { items: list.slice(start, start + size), start, end: Math.min(start + size, list.length), total: list.length,
      pageSize: size, previous: start > 0, next: start + size < list.length };
  };
  const setPage = (state, ownerId, start, shownKinds = null) => {
    const info = page(state, ownerId, shownKinds), max = Math.max(0, Math.floor((info.total - 1) / info.pageSize) * info.pageSize);
    state.pages[ownerId] = Math.max(0, Math.min(Math.floor(start / info.pageSize) * info.pageSize, max));
  };
  const reveal = (state, id, shownKinds = null) => {
    const node = byId.get(id);
    if (!node) return false;
    let targets = [];
    if (node.kind === "relationship") targets = node.item.endpoints.map(ref => byRef.get(ref)).filter(endpoint => endpoint && knowledgeIds.has(endpoint.id));
    else if (knowledgeIds.has(id)) targets = [node];
    for (const ownerId of state.expanded) {
      const info = page(state, ownerId, shownKinds), list = neighbors(ownerId, shownKinds);
      const target = targets.find(item => list.some(candidate => candidate.id === item.id));
      if (target) { const index = list.findIndex(item => item.id === target.id); setPage(state, ownerId, index, shownKinds); state.pageOwner = ownerId; return true; }
      if (info.total && info.items.some(item => targets.some(targetNode => targetNode.id === item.id))) { state.pageOwner = ownerId; return true; }
    }
    return false;
  };
  const setCapacity = (state, nextSize, selectedId = null, shownKinds = null) => {
    if (!GLOBAL_CONTEXT_GRAPH_PAGE_SIZES.includes(nextSize) || nextSize === state.pageSize) return false;
    const selectedNode = byId.get(selectedId || state.selectedId), selectedRefs = selectedNode?.kind === "relationship"
      ? selectedNode.item.endpoints.map(ref => byRef.get(ref)?.id).filter(Boolean)
      : selectedNode && knowledgeIds.has(selectedNode.id) ? [selectedNode.id] : [];
    const nextPages = { ...state.pages }, owners = new Set([...state.expanded, state.focusId, ...Object.keys(state.pages)]);
    owners.forEach(ownerId => {
      const list = neighbors(ownerId, shownKinds), oldPage = page(state, ownerId, shownKinds), oldAnchor = oldPage.items[0]?.id;
      const anchor = selectedRefs.find(id => list.some(node => node.id === id)) || oldAnchor;
      if (!anchor) { nextPages[ownerId] = 0; return; }
      const index = list.findIndex(node => node.id === anchor), max = Math.max(0, Math.floor((list.length - 1) / nextSize) * nextSize);
      nextPages[ownerId] = index < 0 ? 0 : Math.min(Math.floor(index / nextSize) * nextSize, max);
    });
    state.pages = nextPages; state.pageSize = nextSize; return true;
  };
  const connecting = (objectIds, node) => (relationshipEndpoints.get(node.id) || []).filter(id => objectIds.has(id)).length >=
    (node.selfLoop ? 1 : 2);
  const visible = (state, { shownKinds = null, maxOrdinaryNodes = 48 } = {}) => {
    const owners = [...new Set([state.focusId, ...state.expanded].filter(id => knowledgeIds.has(id)))];
    if (owners.length > maxOrdinaryNodes) {
      // On smaller canvases keep the hard node cap. Temporarily render only the focus page while retaining every saved expansion.
      const focusId = knowledgeIds.has(state.focusId) ? state.focusId : null;
      const shown = new Set(focusId ? [focusId] : []), includedOwners = focusId ? [focusId] : [];
      const pageItems = focusId ? page(state, focusId, shownKinds).items.filter(node => !shown.has(node.id)) : [];
      const room = Math.max(0, maxOrdinaryNodes - shown.size);
      pageItems.slice(0, room).forEach(node => shown.add(node.id));
      const overflowOwners = pageItems.length > room ? [focusId] : [];
      const suspendedOwners = owners.filter(id => id !== focusId);
      const relationIds = relationships.filter(node => connecting(shown, node)).map(node => node.id);
      return { objectIds: [...shown], relationshipIds: relationIds, includedOwners, overflowOwners, suspendedOwners };
    }
    const shown = new Set(owners), includedOwners = [], overflowOwners = [];
    owners.filter(Boolean).forEach(ownerId => {
      const pageInfo = page(state, ownerId, shownKinds), extra = pageInfo.items.map(node => node.id).filter(id => !shown.has(id));
      if (shown.size + extra.length > maxOrdinaryNodes) { overflowOwners.push(ownerId); return; }
      extra.forEach(id => shown.add(id));
      includedOwners.push(ownerId);
    });
    const relationIds = relationships.filter(node => connecting(shown, node)).map(node => node.id);
    return { objectIds: [...shown], relationshipIds: relationIds, includedOwners, overflowOwners, suspendedOwners: [] };
  };
  const setFocus = (state, id) => {
    if (!knowledgeIds.has(id)) return false;
    if (state.focusId && state.focusId !== id) state.history.push(state.focusId);
    state.history = state.history.slice(-20); state.focusId = id; state.pageOwner = id; state.expanded = [id];
    state.pages[id] = 0; state.positions = {}; return true;
  };
  const searchText = node => `${node.label || ""} ${node.item.summary || ""} ${node.item.ref} ${JSON.stringify(node.item.attributes || {})}`.toLocaleLowerCase();
  const loadedNodes = knowledge.filter(node => node.kind === "node").length;
  return {
    canonical, graph, byId, byRef, objects, knowledge, relationships, searchNodes: canonical,
    objectCount: objects.length, knowledgeCount: knowledge.length, relationshipCount: relationships.length,
    loadedNodes, placeholderCount: knowledge.length - loadedNodes, totals,
    neighborSets, relationshipEndpoints, incident, knowledgeIds, resultLabel, neighbors, page, setPage, reveal,
    setCapacity, visible, setFocus, stateFrom, searchText
  };
}

function globalContextGraphKindLabel(kind) {
  const known = { node: "Node", endpoint: "Endpoint (not loaded)", relationship: "Relationship" };
  return known[kind] || String(kind || "other").replace(/[-_]+/g, " ").replace(/\b\w/g, letter => letter.toUpperCase());
}
