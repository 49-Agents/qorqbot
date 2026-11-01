// UI-only projection for the Global context relationship browser. Canonical
// objects and relationships remain unchanged and searchable by their exact refs.
const GLOBAL_CONTEXT_GRAPH_PAGE_SIZES = Object.freeze([4, 8, 12]);

function globalContextGraphRole(node) {
  const item = node?.item || {}, kind = String(item.kind || node?.kind || "other");
  if (kind === "note" || kind === "transcript")
    return { role: "source-document", reason: "Source documents stay available in search and details." };
  const source = item.source && typeof item.source === "object" ? item.source : {};
  const wrapPath = typeof source.wrap?.path === "string" ? source.wrap.path.replace(/\\/g, "/") : "";
  const withoutMarker = wrapPath.replace(/\/_topic\.md$/i, "").replace(/_topic\.md$/i, "");
  const finalDirectory = withoutMarker.split("/").filter(Boolean).at(-1)?.toLocaleLowerCase() || "";
  const collectionDirectories = new Set(["marketing", "launchpads", "personal", "operating", "owner-strategy"]);
  if (kind === "topic" && source.wrap && collectionDirectories.has(finalDirectory))
    return { role: "collection", reason: "Imported collection; its canonical record stays available in search and details." };
  const foundingDocumentNames = new Set(["brand", "product", "icp", "positioning", "founding"]);
  if (kind === "topic" && source.created_by === "idea_conversion" && foundingDocumentNames.has(String(item.name || "").trim().toLocaleLowerCase()))
    return { role: "source-document", reason: "Generated source document; its canonical text stays available in search and details." };
  return { role: "knowledge", reason: "" };
}

function globalContextGraphProject(items) {
  const sourceItems = Array.isArray(items) ? items : [];
  const canonicalGraph = ctxFindGraphData(sourceItems);
  const canonical = canonicalGraph.nodes.map(node => {
    const copy = { ...node, item: { ...node.item } };
    // ctxFindGraphData preserves the stored kind in item.kind, while its display
    // kind normalizes business_topic to topic for other context maps.
    if (copy.item.kind === "business_topic") copy.kind = "business_topic";
    copy.displayRole = copy.kind === "relationship" ? "relationship" : globalContextGraphRole(copy).role;
    copy.displayReason = copy.kind === "relationship" ? "" : globalContextGraphRole(copy).reason;
    return copy;
  });
  const byId = new Map(canonical.map(node => [node.id, node]));
  const byRef = new Map(canonical.filter(node => typeof node.item?.ref === "string").map(node => [node.item.ref, node]));
  const objects = canonical.filter(node => node.kind !== "relationship");
  const knowledge = objects.filter(node => node.displayRole === "knowledge");
  const knowledgeIds = new Set(knowledge.map(node => node.id));
  const relationships = canonical.filter(node => node.kind === "relationship");
  const generatedDocumentIds = new Set(objects.filter(node => node.displayRole === "source-document" &&
    node.item?.source?.created_by === "idea_conversion").map(node => node.id));
  const isBookkeeping = node => {
    const source = node.item?.source || {};
    if (["wrap", "within_home", "related_home"].includes(source.auto)) return true;
    if (source.created_by !== "idea_conversion") return false;
    return (Array.isArray(node.item?.endpoints) ? node.item.endpoints : []).some(ref => {
      const endpoint = byRef.get(ref);
      return endpoint && generatedDocumentIds.has(endpoint.id);
    });
  };
  const neighborSets = new Map(knowledge.map(node => [node.id, new Set()]));
  const incident = new Map(canonical.map(node => [node.id, []]));
  const shownRelationships = [], relationshipEndpoints = new Map();
  relationships.forEach(node => {
    const refs = Array.isArray(node.item?.endpoints) ? node.item.endpoints : [];
    const endpointIds = refs.map(ref => byRef.get(ref)?.id).filter(id => id && knowledgeIds.has(id));
    const uniqueEndpointIds = [...new Set(endpointIds)];
    relationshipEndpoints.set(node.id, uniqueEndpointIds);
    refs.forEach(ref => {
      const endpoint = byRef.get(ref);
      if (endpoint) incident.get(endpoint.id)?.push(node.id);
    });
    node.item.unavailableEndpointCount = Array.isArray(node.item?.endpoints)
      ? [...new Set(refs.filter(ref => typeof ref === "string"))].filter(ref => !byRef.has(ref)).length
      : 1;
    node.item.malformedEndpointCount = Array.isArray(node.item?.endpoints)
      ? refs.filter(ref => typeof ref !== "string" || !/^<object>[^<>]+<\/object>$/.test(ref)).length
      : 1;
    node.bookkeeping = isBookkeeping(node);
    node.defaultCanvas = !node.bookkeeping && uniqueEndpointIds.length >= 2;
    if (!node.defaultCanvas) return;
    shownRelationships.push(node);
    uniqueEndpointIds.forEach(id => {
      uniqueEndpointIds.forEach(otherId => { if (otherId !== id) neighborSets.get(id).add(otherId); });
    });
  });
  incident.forEach(list => list.sort((a, b) => a.localeCompare(b)));
  const graphNodes = [...knowledge, ...shownRelationships];
  const graphNodeIds = new Set(graphNodes.map(node => node.id));
  const graphEdges = canonicalGraph.edges.filter(edge => graphNodeIds.has(edge.source) && graphNodeIds.has(edge.target) &&
    knowledgeIds.has(edge.target)).map(edge => ({ ...edge }));
  const graph = {
    rootId: null, nodes: graphNodes, edges: graphEdges,
    counts: {
      objects: knowledge.length, relationships: shownRelationships.length, endpointLinks: graphEdges.length,
      eligibleRelationships: shownRelationships.length,
      substantiveRelationships: relationships.filter(node => !node.bookkeeping).length,
      storedObjects: objects.length, storedRelationships: relationships.length,
      storedEndpointLinks: canonicalGraph.edges.length, storedRecords: canonical.length
    }
  };
  const byLabel = new Map();
  canonical.forEach(node => {
    const key = String(node.label || "").trim().toLocaleLowerCase();
    if (!byLabel.has(key)) byLabel.set(key, []);
    byLabel.get(key).push(node.id);
  });
  const resultLabel = node => {
    const key = String(node.label || "").trim().toLocaleLowerCase(), duplicates = byLabel.get(key) || [];
    if (duplicates.length < 2) return node.label;
    const refPart = String(node.item?.ref || node.id).replace(/^<object>|<\/object>$/g, "");
    return `${node.label} · ${refPart}`;
  };
  const neighbors = (id, shownKinds = null) => [...(neighborSets.get(id) || [])]
    .map(otherId => byId.get(otherId)).filter(node => node && (!shownKinds || shownKinds.has(node.kind)))
    .sort((a, b) => String(a.item?.ref || a.id).localeCompare(String(b.item?.ref || b.id)));
  const stateFrom = saved => {
    const validId = id => typeof id === "string" && byId.has(id);
    const savedFocus = validId(saved?.focusId) && knowledgeIds.has(saved.focusId) ? saved.focusId : null;
    const degree = node => (neighborSets.get(node.id)?.size || 0);
    const defaultFocus = knowledge.slice().sort((a, b) => degree(b) - degree(a) ||
      String(a.item?.ref || a.id).localeCompare(String(b.item?.ref || b.id)))[0]?.id || null;
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
    if (node.kind === "relationship") targets = (node.item?.endpoints || []).map(ref => byRef.get(ref)).filter(endpoint => endpoint && knowledgeIds.has(endpoint.id));
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
      ? (selectedNode.item?.endpoints || []).map(ref => byRef.get(ref)?.id).filter(Boolean)
      : selectedNode && knowledgeIds.has(selectedNode.id) ? [selectedNode.id] : [];
    const nextPages = { ...state.pages }, owners = new Set([...state.expanded, state.focusId, ...Object.keys(state.pages)]);
    owners.forEach(ownerId => {
      const list = neighbors(ownerId, shownKinds), oldPage = page(state, ownerId, shownKinds), oldAnchor = oldPage.items[0]?.id;
      let anchor = selectedRefs.find(id => list.some(node => node.id === id)) || oldAnchor;
      if (!anchor) { nextPages[ownerId] = 0; return; }
      const index = list.findIndex(node => node.id === anchor), max = Math.max(0, Math.floor((list.length - 1) / nextSize) * nextSize);
      nextPages[ownerId] = index < 0 ? 0 : Math.min(Math.floor(index / nextSize) * nextSize, max);
    });
    state.pages = nextPages; state.pageSize = nextSize; return true;
  };
  const visible = (state, { shownKinds = null, maxOrdinaryNodes = 48 } = {}) => {
    const owners = [...new Set([state.focusId, ...state.expanded].filter(id => knowledgeIds.has(id)))];
    if (owners.length > maxOrdinaryNodes) {
      // On smaller canvases keep the hard node cap. Temporarily render only the
      // focus page while retaining every saved expansion for a larger view.
      const focusId = knowledgeIds.has(state.focusId) ? state.focusId : null;
      const objects = new Set(focusId ? [focusId] : []), includedOwners = focusId ? [focusId] : [];
      const pageItems = focusId ? page(state, focusId, shownKinds).items.filter(node => !objects.has(node.id)) : [];
      const room = Math.max(0, maxOrdinaryNodes - objects.size);
      pageItems.slice(0, room).forEach(node => objects.add(node.id));
      const overflowOwners = pageItems.length > room ? [focusId] : [];
      const suspendedOwners = owners.filter(id => id !== focusId);
      const relationIds = relationships.filter(node => node.defaultCanvas &&
        (relationshipEndpoints.get(node.id) || []).filter(id => objects.has(id)).length >= 2).map(node => node.id);
      return { objectIds: [...objects], relationshipIds: relationIds, includedOwners, overflowOwners, suspendedOwners };
    }
    // Keep active expansion roots visible even when paging another owner drops
    // them from that owner's current page. Their own pages can then keep their
    // recorded branches attached without inventing endpoint pairs.
    const objects = new Set(owners), includedOwners = [], overflowOwners = [];
    owners.filter(Boolean).forEach(ownerId => {
      const pageInfo = page(state, ownerId, shownKinds), add = pageInfo.items.map(node => node.id).filter(id => !objects.has(id));
      if (objects.size + add.length > maxOrdinaryNodes) { overflowOwners.push(ownerId); return; }
      add.forEach(id => objects.add(id));
      includedOwners.push(ownerId);
    });
    const relationIds = relationships.filter(node => node.defaultCanvas &&
      (relationshipEndpoints.get(node.id) || []).filter(id => objects.has(id)).length >= 2).map(node => node.id);
    return { objectIds: [...objects], relationshipIds: relationIds, includedOwners, overflowOwners, suspendedOwners: [] };
  };
  const setFocus = (state, id) => {
    if (!knowledgeIds.has(id)) return false;
    if (state.focusId && state.focusId !== id) state.history.push(state.focusId);
    state.history = state.history.slice(-20); state.focusId = id; state.pageOwner = id; state.expanded = [id];
    state.pages[id] = 0; state.positions = {}; return true;
  };
  const searchText = node => `${node.label || ""} ${JSON.stringify(node.item || {})}`.toLocaleLowerCase();
  return {
    canonical, graph, byId, byRef, objects, knowledge, relationships, searchNodes: canonical,
    objectCount: objects.length, knowledgeCount: knowledge.length, relationshipCount: relationships.length,
    sourceDocumentCount: objects.filter(node => node.displayRole === "source-document").length,
    collectionCount: objects.filter(node => node.displayRole === "collection").length,
    storedEndpointCount: canonicalGraph.edges.length, shownRelationshipIds: new Set(shownRelationships.map(node => node.id)),
    neighborSets, relationshipEndpoints, incident, knowledgeIds, resultLabel, neighbors, page, setPage, reveal,
    setCapacity, visible, setFocus, stateFrom, searchText
  };
}

function globalContextGraphKindLabel(kind) {
  const known = { business_topic: "Business", topic: "Concept", person: "Person", product: "Product", platform: "Platform",
    project: "Project", note: "Note", transcript: "Transcript", relationship: "Relationship", idea: "Idea",
    task: "Task", material: "Material", table: "Table", agent: "Agent", event: "Event", occurrence: "Occurrence",
    company: "Company", other: "Other" };
  return known[kind] || String(kind || "other").replace(/[-_]+/g, " ").replace(/\b\w/g, letter => letter.toUpperCase());
}
