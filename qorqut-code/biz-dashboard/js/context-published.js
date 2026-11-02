// Readers of the shared published Context graph (context_home / context_find / context_object / context_expand). One implementation serves the Global Graph/object routes and the business Context Home, Graph,
// search and object routes: only the operation path differs (/root/operations/<op> or /biz/<slug>/operations/<op>),
// and both read the same published graph. Reads only; nothing here writes, initializes Context or fetches a source.
//
// Pages are bound to the revision of their first page and to their exact request; only returned opaque cursors are
// followed, and no loop ever walks the whole graph. References, revisions and commits decide what is requested and
// accepted but are never shown: people see names, summaries, content and plain status.
const PUB_LIMITS = Object.freeze({home: 30, find: 50, expand: 50, history: 10, text: 16000});
const PUB_REF_RE = /^(node|edge|source):[a-z0-9]{5}$/;
const pubRefKind = ref => typeof ref === 'string' && PUB_REF_RE.test(ref) ? ref.slice(0, ref.indexOf(':')) : null;
let pubMountGeneration = 0;

const pubPath = (scope, operation) => scope?.kind === 'root' ? `/root/operations/${operation}`
  : scope?.kind === 'business' && scope.id ? `/biz/${encodeURIComponent(scope.id)}/operations/${operation}` : null;

// ---- the closed response schemas (mirrors qorqut_context.integration.reading.schemas): an unknown or retired field is rejected
const pubIs = (() => {
  const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const string = value => typeof value === 'string', nullable = check => value => value === null || check(value);
  const count = value => Number.isSafeInteger(value) && value >= 0;
  const re = pattern => value => typeof value === 'string' && pattern.test(value);
  const list = check => value => Array.isArray(value) && value.every(check);
  const lit = expected => value => value === expected;
  const oneOf = (...allowed) => value => allowed.includes(value);
  const either = (...checks) => value => checks.some(check => check(value));
  const shape = (required, optional = {}) => value => plain(value) &&
    Object.keys(required).every(key => Object.hasOwn(value, key) && required[key](value[key])) &&
    Object.keys(value).every(key => Object.hasOwn(required, key) || Object.hasOwn(optional, key)) &&
    Object.keys(optional).every(key => !Object.hasOwn(value, key) || optional[key](value[key]));
  const commit = re(/^([0-9a-f]{40}|[0-9a-f]{64})$/), id = re(/^[a-z0-9]{5}$/);
  const refOf = kind => re(new RegExp(`^${kind}:[a-z0-9]{5}$`));
  const member = value => ['string', 'number', 'boolean'].includes(typeof value);
  const attributes = value => plain(value) && Object.keys(value).every(key => list(member)(value[key]));
  const mode = oneOf('GENERAL', 'OPERATIONAL_ACTION'), operation = oneOf('add', 'edit', 'remove');
  const bool = value => typeof value === 'boolean';
  const endpoint = shape({kind: lit('node'), id, ref: refOf('node'), summary: nullable(string), truncated: bool});
  const node = shape({kind: lit('node'), id, ref: refOf('node'), summary: string, attributes, truncated: bool});
  const relationship = shape({kind: lit('edge'), id, ref: refOf('edge'), summary: string, attributes, truncated: bool, from: endpoint, to: endpoint},
    {direction: oneOf('out', 'in', 'self'), depth: value => Number.isSafeInteger(value) && value >= 1});
  const sourceSummary = shape({id, ref: refOf('source'), title: string, source_kind: string, origin: string, origin_ref: nullable(string), created_at: nullable(string)});
  const source = shape({kind: lit('source'), id, ref: refOf('source'), title: string, source_kind: string, origin: string, origin_ref: nullable(string),
    created_at: nullable(string), input_mode: mode, withdrawn: bool, excerpt: nullable(string), truncated: bool});
  const homeNode = shape({kind: lit('node'), id, ref: refOf('node'), summary: string, attributes, truncated: bool, relationship_count: count,
    relationships: list(relationship), relationships_truncated: bool});
  const page = shape({limit: value => Number.isSafeInteger(value) && value >= 1, returned: count, has_more: bool, next_cursor: nullable(string)},
    {window_exhausted: bool});
  const stored = nullable(shape({kind: oneOf('node', 'edge'), id: string, ref: string, summary: string, attributes}, {from_id: string, to_id: string}));
  const evidence = shape({commit, event_id: id, recorded_at: string, input_mode: mode, source: sourceSummary});
  const support = shape({count, items: list(evidence), truncated: bool});
  const supports = shape({count, items: list(either(node, relationship)), truncated: bool});
  const development = shape({date: either(string, Number.isSafeInteger), description: string, citations: list(commit)});
  const historyEntry = shape({event_id: id, commit, recorded_at: string, input_mode: mode, operation, explanation: string, citations: list(commit),
    support_after: list(commit), developments: list(development), source: sourceSummary, before: stored, after: stored});
  const sourceHistoryEntry = shape({event_id: id, commit, recorded_at: string, input_mode: mode, operation, change_count: count});
  const history = entry => shape({total: count, items: list(entry), page});
  const unavailable = shape({status: oneOf('not_initialized', 'unavailable'), revision: value => value === null, reason: shape({code: string, message: string})});
  const ready = (fields) => shape({status: lit('ready'), revision: commit, ...fields});
  const content = either(value => value === null, shape({text: string, offset: count, limit: value => Number.isSafeInteger(value) && value >= 1,
    total_characters: count, has_more: bool, next_cursor: nullable(string)}));
  const totals = shape({nodes: count, edges: count, sources: count, events: count});
  const objectKinds = {
    node: ready({object: shape({kind: lit('node'), id, ref: refOf('node'), summary: string, attributes}), relationship_count: count,
      relationships: list(relationship), relationships_truncated: bool, support, history: history(historyEntry)}),
    edge: ready({object: shape({kind: lit('edge'), id, ref: refOf('edge'), summary: string, attributes, from: endpoint, to: endpoint}),
      support, history: history(historyEntry)}),
    source: ready({object: shape({kind: lit('source'), id, ref: refOf('source'), title: string, source_kind: string, origin: string, origin_ref: nullable(string),
      created_at: nullable(string), input_mode: mode, operation, withdrawn: bool}), content, supports, history: history(sourceHistoryEntry)})
  };
  const out = {
    context_home: ready({empty: bool, totals, nodes: list(homeNode), page}),
    context_find: ready({query: string, items: list(either(node, relationship, source)), page}),
    context_expand: ready({origin: node, depth: value => [1, 2, 3].includes(value), direction: oneOf('out', 'in', 'both'), total: count,
      relationships: list(relationship), page}),
    context_object: value => plain(value) && plain(value.object) && Object.hasOwn(objectKinds, value.object.kind) && objectKinds[value.object.kind](value)
  };
  return {out, unavailable, plain, count};
})();

function pubCheck(operation, data) {
  const ok = data && data.status === 'ready' ? pubIs.out[operation]?.(data) : pubIs.unavailable(data);
  if (ok) return data;
  const error = new Error('The Context reader returned a response that does not match the published graph contract.');
  error.code = 'invalid_response';
  throw error;
}
async function pubRead(scope, operation, args, signal) {
  const path = pubPath(scope, operation);
  if (!path) throw new Error('A global or business Context scope is required');
  return pubCheck(operation, coreData(await pageReadPost(path, args, signal)));
}

// ---- revision- and request-bound paging
// One collection (Home nodes, search hits, expansion, object history, change events). `apply` checks one page against
// everything already accepted and either appends it or records a visible, recoverable error that keeps the earlier pages.
function pubCollection({read, items, pageOf, keyOf, limit, verify}) {
  const c = {items: [], revision: null, status: 'idle', reason: null, cursor: null, hasMore: false, exhausted: false, error: null, loading: false,
    total: null, epoch: 0, seenKeys: new Set(), seenCursors: new Set(), pages: 0, first: null};
  const integrity = message => { const error = new Error(message); error.integrity = true; return error; };
  const options = {read, items, pageOf, keyOf, limit, verify};
  c.fresh = () => pubCollection(options);
  // Take over a fully validated candidate in one step; any older in-flight request of this collection is superseded.
  const ADOPTED = ['items', 'revision', 'status', 'reason', 'cursor', 'hasMore', 'exhausted', 'total', 'seenKeys', 'seenCursors', 'pages', 'first', 'last'];
  // Supersede every outstanding request of this collection without touching what it shows (a pending result is dropped, and its
  // cleanup can no longer unlock newer work).
  c.invalidate = () => { c.epoch++; c.loading = false; };
  c.adopt = next => { for (const key of ADOPTED) c[key] = next[key]; c.error = null; c.loading = false; c.epoch++; };
  c.restart = () => {
    Object.assign(c, {items: [], revision: null, status: 'idle', reason: null, cursor: null, hasMore: false, exhausted: false, error: null, total: null,
      seenKeys: new Set(), seenCursors: new Set(), pages: 0, first: null, loading: false});
    c.epoch++;   // a request issued before this restart is superseded: its result and its cleanup are ignored
  };
  // Accept one page that was requested with `used` (null for the first page).
  c.apply = (data, used) => {
    if (data.status !== 'ready') {
      if (used) throw integrity('The published Context stopped being readable while paging; the pages already loaded are kept.');
      c.status = data.status; c.reason = data.reason; return;
    }
    if (used && data.revision !== c.revision) throw integrity('The Context was published again while paging. Earlier pages are kept; restart to read the new publication.');
    const rows = items(data), page = pageOf(data);
    if (page.returned !== rows.length || page.limit !== limit) throw integrity('A page did not match the size that was requested.');
    if (page.has_more !== (page.next_cursor !== null) || (page.has_more && !rows.length)) throw integrity('A page offered an unusable continuation.');
    if (page.has_more && (page.next_cursor === used || c.seenCursors.has(page.next_cursor))) throw integrity('A page repeated a cursor that did not advance.');
    verify?.(data, c, used);
    const keys = rows.map(keyOf);
    if (new Set(keys).size !== keys.length || keys.some(key => c.seenKeys.has(key))) throw integrity('A page repeated records that were already loaded.');
    keys.forEach(key => c.seenKeys.add(key));
    if (used) c.seenCursors.add(used);
    c.revision = data.revision; c.status = 'ready'; c.reason = null; c.pages++;
    c.items = c.items.concat(rows);
    c.hasMore = page.has_more; c.cursor = page.next_cursor; c.exhausted = page.window_exhausted === true;
    if (!used) c.first = data;
    c.last = data;
  };
  // Load the first page (after a restart) or the continuation. The result says whether it was superseded.
  c.load = async live => {
    if (c.loading) return;
    if (!live()) { c.aborted = true; return; }   // never issue a read for a view or request that is already gone
    const used = c.cursor, epoch = c.epoch;
    c.loading = true; c.error = null;
    try {
      const data = await read(used);
      if (epoch !== c.epoch || !live()) { c.aborted = true; return; }
      c.apply(data, used);
    } catch (error) {
      if (epoch !== c.epoch || !live()) { c.aborted = true; return; }
      c.error = error;
    } finally { if (epoch === c.epoch) c.loading = false; }
  };
  // Staged refresh of the same identity: a separate candidate rereads from the first page through the pages already loaded
  // (bounded), with every page checked against that candidate's own revision. Only a fully valid candidate replaces the
  // shown collection; a transient or integrity failure keeps it with a recoverable error, lost access clears it.
  c.refresh = async live => {
    // A refresh supersedes a continuation or an older refresh that is still in flight: their results are ignored.
    const epoch = ++c.epoch, target = Math.max(1, c.pages), next = c.fresh();
    c.loading = true;
    try {
      const current = () => epoch === c.epoch && live();   // a newer refresh, restart or adoption also stops this walk
      await pubWalk(next, target, current);
      if (!current()) return;
      if (next.error) {
        if (accessLost(next.error) || sourceGone(next.error)) { c.restart(); c.error = next.error; }
        else { next.error.refresh = true; c.error = next.error; }
        return;
      }
      c.adopt(next);
    } finally { if (epoch === c.epoch) c.loading = false; }
  };
  return c;
}
// Read a candidate's first page and then continuations until `target` pages are loaded or the collection ends.
async function pubWalk(candidate, target, live) {
  if (!live()) return;
  await candidate.load(live);
  while (live() && !candidate.aborted && !candidate.error && candidate.status === 'ready' && candidate.hasMore && candidate.pages < target) await candidate.load(live);
}
// Identical markup keeps the existing nodes (selection, scroll, focus); the caller wires handlers afterwards either way.
// `html` carries an empty `.pub-errslot`; the error markup is placed into it separately, so an error appearing or clearing never
// rebuilds the content around it.
const PUB_ERRSLOT = '<div class="pub-errslot"></div>';
function pubSet(host, html, errorHtml = '') {
  if (host._pubHtml !== html || !host.firstElementChild) { host.innerHTML = html; host._pubHtml = html; }
  const slot = host.querySelector('.pub-errslot');
  if (slot && slot._html !== errorHtml) { slot.innerHTML = errorHtml; slot._html = errorHtml; }
}
// A failed refresh keeps its pages and their continuation; a failed continuation hides the control that failed.
const pubContinues = collection => collection.hasMore && !(collection.error && !collection.error.refresh);

// People are graph nodes explicitly classified by their stored attributes, never a different DTO kind.
function pubPerson(item) {
  if (!item || item.kind !== 'node' || !Array.isArray(item.attributes?.basic_entity_type) ||
      !item.attributes.basic_entity_type.includes('person')) return null;
  const text = key => (item.attributes[key] || []).filter(value => typeof value === 'string' && value.trim()).join(' · ');
  return {...item, name: text('name') || item.summary || 'Person', role: text('role'), org: text('org')};
}

// ---- rendering (escaped text only; attribute values and summaries are always data, never markup)
const PUB_COPY = {loadFailed: "Couldn't load. Retry", changed: 'Context changed. Reload', more: 'More available', loadMore: 'Load more'};
const pubNeutral = {node: 'Item', edge: 'Relationship', source: 'Source'};
const pubName = (text, kind) => text == null || text === '' ? pubNeutral[kind] : text;
const pubOperation = operation => ({add: 'Added', edit: 'Updated', remove: 'Removed'}[operation] || 'Updated');
const pubAttributeChips = attributes => Object.entries(attributes || {}).map(([name, members]) =>
  `<span class="chip pub-attr"><b>${esc(name)}</b>${members.length ? esc(members.map(String).join(', ')) : '—'}</span>`).join('');
// The reference stays in a data attribute for navigation only; it is never shown or offered as a title.
const pubRefButton = (ref, label) => `<button type="button" class="pub-ref chip go" data-pub-ref="${esc(ref)}">${esc(label)}</button>`;

function pubEdgeLine(edge, focusRef = null) {
  const end = e => e.ref === focusRef ? `<span class="pub-self">${esc(pubName(e.summary, 'node'))}</span>` : pubRefButton(e.ref, pubName(e.summary, 'node'));
  return `<div class="card n pub-edge" data-pub-edge="${esc(edge.ref)}"><div class="ct"><span class="kd">Relationship</span>
    <span class="tt">${end(edge.from)} <span class="pub-arrow" aria-hidden="true">→</span> ${end(edge.to)}</span></div>
    <div class="cb">${esc(edge.summary)}</div>${pubAttributes(edge.attributes)}</div>`;
}
const pubAttributes = attributes => Object.keys(attributes || {}).length ? `<div class="chips pub-attrs">${pubAttributeChips(attributes)}</div>` : '';

function pubNodeCard(node) {
  const shown = node.relationships || [], more = node.relationship_count - shown.length;
  return `<article class="card n pub-node" data-pub-node="${esc(node.ref)}"><div class="ct"><span class="kd">Item</span>
    <span class="tt"><button type="button" class="pub-open" data-pub-ref="${esc(node.ref)}">${esc(pubName(node.summary, 'node'))}</button></span></div>
    <div class="cs">${node.relationship_count} relationship${node.relationship_count === 1 ? '' : 's'}${node.relationships_truncated || more > 0 ? ` · <span class="pub-trunc">${PUB_COPY.more}</span>` : ''}</div>
    ${pubAttributes(node.attributes)}</article>`;
}
function pubSourceCard(source) {
  return `<article class="card n pub-source" data-pub-source="${esc(source.ref)}"><div class="ct"><span class="kd">Source${source.withdrawn ? ' · Removed' : ''}</span>
    <span class="tt"><button type="button" class="pub-open" data-pub-ref="${esc(source.ref)}">${esc(pubName(source.title, 'source'))}</button></span></div>
    ${source.excerpt ? `<div class="cb">${esc(source.excerpt)}${source.truncated ? ` <small class="pub-trunc">${PUB_COPY.more}</small>` : ''}</div>` : ''}</article>`;
}
function pubFindCard(item) {
  if (item.kind === 'source') return pubSourceCard(item);
  if (item.kind === 'edge') return `<article class="card n pub-edge-result" data-pub-result="${esc(item.ref)}"><div class="ct"><span class="kd">Relationship</span>
    <span class="tt"><button type="button" class="pub-open" data-pub-ref="${esc(item.ref)}">${esc(pubName(item.summary, 'edge'))}</button></span></div>
    <div class="cs">${pubRefButton(item.from.ref, pubName(item.from.summary, 'node'))} → ${pubRefButton(item.to.ref, pubName(item.to.summary, 'node'))}</div>
    ${pubAttributes(item.attributes)}</article>`;
  return `<article class="card n pub-node" data-pub-result="${esc(item.ref)}"><div class="ct"><span class="kd">Item</span>
    <span class="tt"><button type="button" class="pub-open" data-pub-ref="${esc(item.ref)}">${esc(pubName(item.summary, 'node'))}</button></span></div>
    ${pubAttributes(item.attributes)}</article>`;
}

function pubUnavailableHtml(status, reason, retryLabel) {
  if (status === 'not_initialized') return `<div class="empty pub-state" data-pub-state="not_initialized" role="status"><strong>Nothing has been added to Context yet.</strong>
    <button type="button" class="bt pub-retry">${esc(retryLabel)}</button></div>`;
  return `<div class="empty bad pub-state" data-pub-state="unavailable" role="alert"><strong>${PUB_COPY.loadFailed}</strong>
    <button type="button" class="bt pub-retry">${esc(retryLabel)}</button></div>`;
}
// An integrity error means the Context moved under the reader (or a page did not fit); a transient one is simply retried.
// A failed refresh also offers a staged reload (never a continuation of the pages it was replacing).
const pubErrorHtml = error => `<div class="bad pub-error" role="alert" data-pub-error="${error.integrity ? 'integrity' : 'transient'}">
  ${error.integrity ? PUB_COPY.changed : PUB_COPY.loadFailed} <button type="button" class="bt pub-${error.integrity || error.refresh ? 'restart' : 'again'}">${error.integrity ? 'Reload' : 'Retry'}</button></div>`;

// A live check shared by every view: the same mount, still attached, not aborted, and the caller's route is still current.
function pubLive(host, generation, signal, current) {
  return () => generation === pubMountGeneration && host.isConnected && !signal.aborted && (!current || current());
}
function pubWireRefs(root, nav) {
  root.querySelectorAll('[data-pub-ref]').forEach(button => button.onclick = event => {
    event.stopPropagation();
    nav.open(button.dataset.pubRef);
  });
  root.querySelectorAll('[data-pub-go]').forEach(link => link.onclick = () => nav.go(link.dataset.pubGo));
}

// ---- Home (business) ----------------------------------------------------------------------------------------------
function pubMountHome(host, scope, nav) {
  const generation = ++pubMountGeneration, signal = pageReads.signal, live = pubLive(host, generation, signal, nav.current);
  const collection = pubCollection({limit: PUB_LIMITS.home, items: data => data.nodes, pageOf: data => data.page, keyOf: node => node.ref,
    read: cursor => pubRead(scope, 'context_home', {limit: PUB_LIMITS.home, ...(cursor ? {cursor} : {})}, signal)});
  const paint = () => {
    if (!live()) return;
    const crumbs = `<div class="crumbs"><span class="cur">${esc(nav.homeLabel)}</span></div>`;
    let body;
    if (collection.status === 'idle') body = collection.error ? pubErrorHtml(collection.error) : '<p class="empty pad" role="status">Loading…</p>';
    else if (collection.status !== 'ready') body = pubUnavailableHtml(collection.status, collection.reason, 'Retry');
    else {
      const first = collection.first || collection.last, totals = first.totals;
      body = `<div class="th"><h2>Shared Context</h2></div>
        <div class="meta pub-totals">${totals.nodes} item${totals.nodes === 1 ? '' : 's'} · ${totals.edges} relationship${totals.edges === 1 ? '' : 's'} · ${totals.sources} source${totals.sources === 1 ? '' : 's'}</div>
        ${first.empty ? '<div class="empty pub-state" data-pub-state="empty" role="status">Nothing has been added to Context yet.</div>' : `
        <div class="sec"><div class="sh">Most connected</div>
        <div class="cards">${collection.items.map(pubNodeCard).join('')}</div>${pubContinues(collection) ? `<button type="button" class="bt pub-more">${PUB_COPY.loadMore}</button>` : ''}</div>`}
        ${PUB_ERRSLOT}<p class="cs"><button type="button" class="quiet pub-refresh">Refresh</button></p>`;
    }
    pubSet(host, `<section class="pub-view pub-home" aria-label="Shared Context">${crumbs}${body}</section>`, collection.error && collection.status === 'ready' ? pubErrorHtml(collection.error) : '');
    pubWireRefs(host, nav);
    const more = host.querySelector('.pub-more'); if (more) more.onclick = load;
    host.querySelectorAll('.pub-again').forEach(b => b.onclick = load);
    host.querySelectorAll('.pub-restart, .pub-retry, .pub-refresh').forEach(b => { b.onclick = reload; b.disabled = collection.loading; });
  };
  async function load() { if (collection.loading) return; paint(); await collection.load(live); paint(); }
  // The shown pages stay mounted while the same Context is read again.
  async function reload() {
    if (collection.status === 'idle' && !collection.items.length) { collection.restart(); return load(); }
    host.querySelectorAll('.pub-restart, .pub-retry, .pub-refresh').forEach(b => { b.disabled = true; });
    await collection.refresh(live);
    paint();
  }
  collection.restart(); host._pubHtml = null; host.innerHTML = '<p class="empty pad" role="status">Loading…</p>';
  load();
}

// ---- Search (business) --------------------------------------------------------------------------------------------
function pubMountSearch(host, scope, query, nav) {
  const generation = ++pubMountGeneration, signal = pageReads.signal, live = pubLive(host, generation, signal, nav.current);
  const collection = pubCollection({limit: PUB_LIMITS.find, items: data => data.items, pageOf: data => data.page, keyOf: item => item.ref,
    verify: data => { if (data.query !== query) { const error = new Error('The search results were for a different query.'); error.integrity = true; throw error; } },
    read: cursor => pubRead(scope, 'context_find', {query, limit: PUB_LIMITS.find, ...(cursor ? {cursor} : {})}, signal)});
  const paint = () => {
    if (!live()) return;
    let body;
    if (collection.status === 'idle') body = collection.error ? pubErrorHtml(collection.error) : '<p class="empty pad" role="status">Searching…</p>';
    else if (collection.status !== 'ready') body = pubUnavailableHtml(collection.status, collection.reason, 'Retry');
    else body = `<div class="cards">${collection.items.map(pubFindCard).join('') ||
      '<div class="empty pub-state" data-pub-state="no-results">No results.</div>'}</div>
      ${pubContinues(collection) ? `<button type="button" class="bt pub-more">${PUB_COPY.loadMore}</button>` : ''}
      ${collection.exhausted ? `<div class="pub-window" role="status">${PUB_COPY.more}. Narrow the search to see more.</div>` : ''}
      ${PUB_ERRSLOT}`;
    pubSet(host, `<section class="pub-view sec ctx-search-results"><div class="sh">Search: ${esc(query)}${collection.hasMore ? ` <span class="stamp">${PUB_COPY.more}</span>` : ''}</div>${body}</section>`, collection.error && collection.status === 'ready' ? pubErrorHtml(collection.error) : '');
    pubWireRefs(host, nav);
    const more = host.querySelector('.pub-more'); if (more) more.onclick = load;
    host.querySelectorAll('.pub-again').forEach(b => b.onclick = load);
    host.querySelectorAll('.pub-restart, .pub-retry').forEach(b => b.onclick = reload);
  };
  async function load() { if (collection.loading) return; paint(); await collection.load(live); paint(); }
  async function reload() {
    if (collection.status === 'idle' && !collection.items.length) { collection.restart(); return load(); }
    await collection.refresh(live);
    paint();
  }
  host._pubHtml = null; collection.restart(); load();
}

// ---- Graph --------------------------------------------------------------------------------------------------------
// Seeded by context_home pages; "Load more" appends the next page of the same revision. The view is a bounded
// window onto the published graph and never claims to be the complete graph.
function pubMountGraph(host, scope, nav, {storageKey, caption}) {
  const generation = ++pubMountGeneration, signal = pageReads.signal, live = pubLive(host, generation, signal, nav.current);
  const collection = pubCollection({limit: PUB_LIMITS.home, items: data => data.nodes, pageOf: data => data.page, keyOf: node => node.ref,
    read: cursor => pubRead(scope, 'context_home', {limit: PUB_LIMITS.home, ...(cursor ? {cursor} : {})}, signal)});
  host.innerHTML = `<section class="global-context-base global-context-graph pub-view" aria-label="Context graph">
    <div class="global-context-graph-status"><div class="global-context-base-status pub-graph-status" role="status">Loading…</div>
      <button type="button" class="bt pub-more" hidden>${PUB_COPY.loadMore}</button>
      <button type="button" class="bt global-context-base-reload pub-refresh" hidden>Refresh</button></div>
    <div class="pub-graph-notes" role="status"></div><div class="global-context-graph-host"></div></section>`;
  const status = host.querySelector('.pub-graph-status'), more = host.querySelector('.pub-more'), refresh = host.querySelector('.pub-refresh');
  const notes = host.querySelector('.pub-graph-notes'), graphHost = host.querySelector('.global-context-graph-host');
  let graphSignature = null;
  const paint = () => {
    if (!live()) return;
    notes.replaceChildren(); more.hidden = true; refresh.hidden = false; refresh.disabled = collection.loading;
    if (collection.status === 'idle') {
      ctxClearGraph(); graphHost.replaceChildren(); graphSignature = null; refresh.hidden = true;
      status.textContent = collection.error ? PUB_COPY.loadFailed : 'Loading…';
      if (collection.error) { more.hidden = false; more.textContent = 'Retry'; status.dataset.pubError = 'transient'; } else delete status.dataset.pubError;
      return;
    }
    if (collection.status !== 'ready') {
      ctxClearGraph(); graphHost.replaceChildren(); graphSignature = null; refresh.hidden = true;
      status.innerHTML = pubUnavailableHtml(collection.status, collection.reason, 'Retry');
      status.querySelector('.pub-retry').onclick = reload;
      return;
    }
    const first = collection.first || collection.last, totals = first.totals;
    if (first.empty) {
      ctxClearGraph(); graphHost.replaceChildren(); graphSignature = null;
      status.textContent = 'Nothing has been added to Context yet.';
      status.dataset.pubState = 'empty'; return;
    }
    delete status.dataset.pubState;
    const model = contextPublishedGraphModel(collection.items, totals);
    const truncatedNodes = collection.items.filter(node => node.relationships_truncated).length;
    model.countLabel = (objects, connections) => `${objects} item${objects === 1 ? '' : 's'} · ${connections} connection${connections === 1 ? '' : 's'}`;
    status.textContent = collection.hasMore || model.loadedNodes < totals.nodes ? `${model.loadedNodes} of ${totals.nodes} items shown · ${PUB_COPY.more}` : `${totals.nodes} item${totals.nodes === 1 ? '' : 's'}`;
    if (truncatedNodes) notes.textContent = `${PUB_COPY.more}: some items have more connections than are shown. Open an item for the rest.`;
    if (collection.error) {
      const message = document.createElement('div');
      message.className = 'bad pub-error'; message.setAttribute('role', 'alert'); message.dataset.pubError = collection.error.integrity ? 'integrity' : 'transient';
      message.textContent = collection.error.integrity ? PUB_COPY.changed : PUB_COPY.loadFailed;
      notes.append(message);
      more.hidden = false; more.textContent = collection.error.integrity ? 'Reload' : 'Retry';
    } else if (collection.hasMore) { more.hidden = false; more.textContent = PUB_COPY.loadMore; }
    // The same graph (camera, layout, selection) stays mounted unless the published data it draws has changed;
    // a changed graph is rebuilt and restores its view from the saved storage key.
    const signature = JSON.stringify([collection.items, totals]);
    if (signature === graphSignature && _ctxGraph && graphHost.firstChild) return;
    graphSignature = signature;
    ctxClearGraph();
    _ctxGraph = contextGraph(graphHost, model.graph, {knowledgeGraph: model, storageKey, caption,
      onOpen: node => { const ref = node.item?.ref; if (ref) nav.open(ref); }});
  };
  async function load() { if (collection.loading) return; more.disabled = true; await collection.load(live); more.disabled = false; paint(); }
  async function reload() {
    if (collection.status === 'idle' && !collection.items.length) { collection.restart(); paint(); return load(); }
    more.disabled = true; refresh.disabled = true;
    await collection.refresh(live);
    more.disabled = false;
    paint();
  }
  // A failed refresh (or a publication change) retries as a staged refresh; a failed continuation retries the next page.
  more.onclick = () => { if (collection.error && (collection.error.integrity || collection.error.refresh)) reload(); else load(); };
  refresh.onclick = reload;
  collection.restart(); load();
}

// ---- Object (node / edge / source) ---------------------------------------------------------------------------------
// A stored record in the history, shown as content only: its text, attributes and endpoint names.
function pubStoredHtml(label, stored) {
  if (stored === null) return `<div class="pub-stored" data-pub-stored="${label}"><b>${label}:</b> <span class="pub-none">None</span></div>`;
  // Relationship endpoints are neutral links to the exact returned item; the identifier is only the navigation target.
  const end = (name, id) => typeof id === 'string' && id ? `<div class="cs pub-end">${name}: ${pubRefButton('node:' + id, pubNeutral.node)}</div>` : '';
  return `<div class="pub-stored" data-pub-stored="${label}"><b>${label}:</b> ${esc(stored.summary)}${pubAttributes(stored.attributes)}${end('From', stored.from_id)}${end('To', stored.to_id)}</div>`;
}
// Which relationship endpoints differ between the stored before and after records.
function pubEndpointChanges(entry) {
  if (!entry.before || !entry.after) return [];
  return [['Source changed', 'from_id'], ['Destination changed', 'to_id']]
    .filter(([, key]) => (entry.before[key] ?? null) !== (entry.after[key] ?? null)).map(([text]) => text);
}
function pubHistoryDetails(entry) {
  const sameSummary = entry.before && entry.after && entry.before.summary === entry.after.summary;
  return `<details class="pub-history-details"><summary>What changed</summary>
    ${pubEndpointChanges(entry).map(text => `<div class="cs pub-unchanged">${text}</div>`).join('')}
    ${sameSummary && !pubEndpointChanges(entry).length ? '<div class="cs pub-unchanged">The text is unchanged; see the details below.</div>' : ''}
    ${pubStoredHtml('Before', entry.before)}${pubStoredHtml('After', entry.after)}
    ${entry.developments.map(d => `<div class="cs pub-development">${esc(String(d.date))}: ${esc(d.description)}</div>`).join('')}</details>`;
}
function pubHistoryHtml(history, kind) {
  const rows = history.items.map(entry => kind === 'source'
    ? `<div class="card n pub-history${entry.operation === 'remove' ? ' pub-tombstone' : ''}" data-pub-history="${esc(entry.event_id)}"><div class="ct"><span class="kd">${pubOperation(entry.operation)}</span><span class="tt">${esc(entry.recorded_at)}</span></div>
        <div class="cs">${entry.change_count} change${entry.change_count === 1 ? '' : 's'}</div></div>`
    : `<div class="card n pub-history${entry.operation === 'remove' ? ' pub-tombstone' : ''}" data-pub-history="${esc(entry.event_id)}">
        <div class="ct"><span class="kd">${pubOperation(entry.operation)}</span><span class="tt">${esc(entry.recorded_at)}</span></div>
        <div class="cb">${esc(entry.explanation)}</div>
        <div class="cs">${pubRefButton(entry.source.ref, pubName(entry.source.title, 'source'))}</div>
        ${pubHistoryDetails(entry)}</div>`).join('');
  return `<div class="sec pub-history-section"><div class="sh">History</div>
    ${rows ? `<div class="cards">${rows}</div>` : '<div class="empty">No history yet.</div>'}
    ${history.hasMore && !history.error ? `<button type="button" class="bt pub-history-more">${PUB_COPY.loadMore}</button>` : ''}
    ${history.error ? pubErrorHtml(history.error).replace('pub-again', 'pub-history-again').replace('pub-restart', 'pub-history-restart') : ''}</div>`;
}

function pubEvidenceHtml(support) {
  const rows = support.items.map(item => `<div class="card n pub-evidence"><div class="ct"><span class="kd">Source</span>
    <span class="tt">${pubRefButton(item.source.ref, pubName(item.source.title, 'source'))}</span></div>
    <div class="cs">${esc(item.recorded_at)}</div></div>`).join('');
  return `<div class="sec pub-support"><div class="sh">Sources</div>
    ${rows ? `<div class="cards">${rows}</div>` : '<div class="empty">No sources.</div>'}${support.truncated ? `<div class="pub-trunc">${PUB_COPY.more}</div>` : ''}</div>`;
}

function pubObjectBody(view) {
  const data = view.object, object = data.object;
  const title = pubName(object.kind === 'source' ? object.title : object.summary, object.kind);
  const crumbs = `<div class="crumbs">${view.nav.crumbs.map(c => `<button type="button" class="quiet pub-crumb" data-pub-go="${esc(c.go)}">${esc(c.label)}</button><span class="sep">›</span>`).join('')}<span class="cur">${esc(title)}</span></div>`;
  const head = (name, kind) => `<div class="th"><h2>${esc(name)}</h2><div class="pa"><span class="kd">${esc(kind)}</span></div></div>`;
  let main = '';
  if (object.kind === 'node') {
    const shown = data.relationships.length;
    main = head(title, 'Item') + pubAttributes(object.attributes) +
      `<div class="sec pub-relationships"><div class="sh">Relationships</div>
        ${shown ? `<div class="cards">${data.relationships.map(edge => pubEdgeLine(edge, object.ref)).join('')}</div>` : '<div class="empty">None</div>'}
        ${data.relationships_truncated ? `<div class="pub-trunc">${PUB_COPY.more}</div>` : ''}</div>` +
      pubExpandHtml(view) + pubEvidenceHtml(data.support);
  } else if (object.kind === 'edge') {
    main = head(title, 'Relationship') + pubAttributes(object.attributes) +
      `<div class="sec pub-endpoints"><div class="sh">Connects</div><div class="cards">
        <div class="card n"><div class="ct"><span class="kd">From</span><span class="tt">${pubRefButton(object.from.ref, pubName(object.from.summary, 'node'))}</span></div></div>
        <div class="card n"><div class="ct"><span class="kd">To</span><span class="tt">${pubRefButton(object.to.ref, pubName(object.to.summary, 'node'))}</span></div></div></div></div>` +
      pubEvidenceHtml(data.support);
  } else {
    const content = view.text;
    const body = !content ? '<div class="empty pub-state" data-pub-state="no-content">No text is available for this source.</div>'
      : `<pre class="pub-source-text" tabindex="0" aria-label="Source text">${esc(content.text)}</pre>
        ${content.has_more && !view.textError ? `<button type="button" class="bt pub-text-more">${PUB_COPY.loadMore}</button>` : ''}
        ${view.textError ? pubErrorHtml(view.textError).replace('pub-again', 'pub-text-again').replace('pub-restart', 'pub-text-restart') : ''}`;
    const supports = data.supports;
    main = head(title, 'Source') +
      (object.created_at ? `<div class="meta pub-source-meta">${esc(object.created_at)}</div>` : '') +
       `${object.withdrawn ? '<div class="bad pub-withdrawn" role="status">Removed. The items it supported keep their history.</div>' : ''}
       <div class="sec pub-content"><div class="sh">Text</div>${body}</div>
       <div class="sec pub-supports"><div class="sh">Supports</div>
         <div class="cards">${supports.items.map(item => item.kind === 'edge' ? pubEdgeLine(item) : pubFindCard(item)).join('') || '<div class="empty">None</div>'}</div>
         ${supports.truncated ? `<div class="pub-trunc">${PUB_COPY.more}</div>` : ''}</div>`;
  }
  return crumbs + main + pubHistoryHtml(view.history, object.kind === 'source' ? 'source' : 'item');
}

function pubExpandHtml(view) {
  const expand = view.expand;
  if (view.changedPublication) return `<div class="sec pub-neighbourhood"><div class="sh">Connected</div><div class="bad pub-error" data-pub-error="publication-changed" role="alert">
    ${PUB_COPY.changed} <button type="button" class="bt pub-reload">Reload</button></div></div>`;
  if (expand.status === 'idle') return '<div class="sec pub-neighbourhood"><div class="sh">Connected</div><p class="empty" role="status">Loading…</p></div>';
  if (expand.status !== 'ready') return `<div class="sec pub-neighbourhood"><div class="sh">Connected</div>${pubUnavailableHtml(expand.status, expand.reason, 'Retry')}</div>`;
  return `<div class="sec pub-neighbourhood"><div class="sh">Connected</div>
    ${expand.items.length ? `<div class="cards">${expand.items.map(edge => pubEdgeLine(edge, view.ref)).join('')}</div>` : '<div class="empty">No relationships.</div>'}
    ${expand.hasMore && !expand.error ? `<button type="button" class="bt pub-expand-more">${PUB_COPY.loadMore}</button>` : ''}
    ${expand.error ? pubErrorHtml(expand.error).replace('pub-again', 'pub-expand-again').replace('pub-restart', 'pub-expand-restart') : ''}</div>`;
}

function pubMountObject(host, scope, ref, nav) {
  const generation = ++pubMountGeneration, signal = pageReads.signal, live = pubLive(host, generation, signal, nav.current);
  const fail = message => { host.innerHTML = `<section class="pub-view"><div class="empty bad pub-route-error" role="alert">${esc(message)}</div><button type="button" class="bt pub-back">${esc(nav.backLabel)}</button></section>`;
    host.querySelector('.pub-back').onclick = () => nav.go(nav.crumbs[nav.crumbs.length - 1].go); };
  if (!pubRefKind(ref)) return fail('Unavailable');
  const kind = pubRefKind(ref);
  let loadGeneration = 0;   // one per record load or reload: results of an older one never touch the current one
  const view = {ref, nav, object: null, text: null, textPages: 0, textError: null, changedPublication: false, reloadError: null};
  const integrity = message => Object.assign(new Error(message), {integrity: true});
  const history = pubCollection({limit: PUB_LIMITS.history, items: data => data.history.items, pageOf: data => data.history.page, keyOf: entry => entry.event_id,
    verify: (data, c, used) => {
      if (data.object.ref !== ref) throw integrity('A history page was for a different record.');
      if (used && data.history.total !== c.total) throw integrity('The history changed while paging; restart to read it again.');
      c.total = data.history.total;
    },
    read: cursor => pubRead(scope, 'context_object', {ref, history_limit: PUB_LIMITS.history, ...(cursor ? {history_cursor: cursor} : {})}, signal)});
  view.history = history;
  const expand = pubCollection({limit: PUB_LIMITS.expand, items: data => data.relationships, pageOf: data => data.page, keyOf: edge => edge.ref,
    verify: (data, c, used) => {
      const bad = data.origin.ref !== ref || data.depth !== 1 || data.direction !== 'both';
      if (bad) throw integrity('A neighbourhood page was for a different request.');
      if (used && data.total !== c.total) throw integrity('The neighbourhood changed while paging; restart to read it again.');
      c.total = data.total;
    },
    read: cursor => pubRead(scope, 'context_expand', {ref, depth: 1, direction: 'both', limit: PUB_LIMITS.expand, ...(cursor ? {cursor} : {})}, signal)});
  view.expand = expand;
  const textLimit = PUB_LIMITS.text;
  // Validate one text page against the text loaded so far (`prior`, null for the first page) and the publication it belongs to.
  const nextText = (data, used, prior, revision) => {
    const content = data.content;
    if (used) {
      if (data.revision !== revision) throw integrity('The Context was published again while reading this text. The text loaded so far is kept; reload the record to read the new publication.');
      if (!content || content.offset !== prior.loaded) throw integrity('The next text page does not continue where the loaded text ends.');
      if (content.total_characters !== prior.total_characters) throw integrity('The source text changed while reading it; reload the record.');
      if (content.has_more && (content.next_cursor === used || content.next_cursor === null || !content.text.length)) throw integrity('The text continuation did not advance.');
      if (!content.has_more && content.next_cursor !== null) throw integrity('The text continuation did not advance.');
    }
    if (!content) return null;
    if (!used && (content.offset !== 0 || content.has_more !== (content.next_cursor !== null))) throw integrity('The first text page is inconsistent.');
    const length = Array.from(content.text).length;
    return {text: (used ? prior.text : '') + content.text, loaded: content.offset + length, total_characters: content.total_characters,
      has_more: content.has_more, next_cursor: content.next_cursor};
  };
  const paint = () => {
    if (!live() || !view.object) return;
    const reloadError = view.reloadError ? `<div class="bad pub-error" role="alert" data-pub-error="${view.reloadError.integrity ? 'integrity' : 'transient'}">
      ${view.reloadError.integrity ? PUB_COPY.changed : PUB_COPY.loadFailed} <button type="button" class="bt pub-reload">${view.reloadError.integrity ? 'Reload' : 'Retry'}</button></div>` : '';
    pubSet(host, `<section class="pub-view global-context-base global-context-object pub-object" data-pub-kind="${kind}" aria-label="${pubNeutral[kind]}">${PUB_ERRSLOT}${pubObjectBody({...view, object: view.object,
      history: history_(), expand: expand_()})}</section>`, reloadError);
    pubWireRefs(host, nav);
    const on = (selector, fn) => host.querySelectorAll(selector).forEach(b => b.onclick = fn);
    on('.pub-history-more, .pub-history-again', () => run(history, paint));
    host.querySelectorAll('.pub-history-more, .pub-history-again, .pub-expand-more, .pub-expand-again, .pub-expand-restart, .pub-retry').forEach(b => { b.disabled = view.reloading === true; });
    host.querySelectorAll('.pub-text-more, .pub-text-again').forEach(b => { b.disabled = view.textBusy === true || view.reloading === true; });
    host.querySelectorAll('.pub-reload').forEach(b => { b.disabled = view.reloading === true; });
    on('.pub-history-restart', () => reload());
    on('.pub-expand-more, .pub-expand-again', () => runExpand());
    on('.pub-expand-restart, .pub-retry', () => { if (view.reloading) return; expand.restart(); runExpand(); });
    on('.pub-reload', () => reload());
    on('.pub-text-more, .pub-text-again', () => runText());
    on('.pub-text-restart', () => reload());
  };
  const history_ = () => ({items: history.items, total: history.total ?? history.items.length, hasMore: history.hasMore, error: history.error});
  const expand_ = () => ({status: expand.status, reason: expand.reason, items: expand.items, total: expand.total ?? expand.items.length, hasMore: expand.hasMore, error: expand.error});
  // Every continuation is bound to the load generation it started in: a reload (or a new load) makes its result a no-op.
  const generationLive = () => { const g = loadGeneration; return () => live() && g === loadGeneration; };
  async function run(collection, repaint) {
    if (collection.loading || view.reloading) return;
    const g = loadGeneration;
    await collection.load(generationLive());
    if (g === loadGeneration) repaint();
  }
  async function runExpand() {
    if (expand.loading || view.reloading) return;
    view.changedPublication = false;
    const g = loadGeneration;
    await expand.load(generationLive());
    if (!live() || g !== loadGeneration) return;
    if (expand.status === 'ready' && expand.revision !== view.object.revision) {
      expand.restart(); view.changedPublication = true;
    }
    paint();
  }
  async function runText() {
    if (!view.text || !view.text.has_more || view.textBusy || view.reloading) return;
    const used = view.text.next_cursor, g = loadGeneration;
    view.textError = null; view.textBusy = true; paint();
    try {
      const data = await pubRead(scope, 'context_object', {ref, text_limit: textLimit, text_cursor: used}, signal);
      if (!live() || g !== loadGeneration) return;
      if (data.status !== 'ready' || data.object.kind !== 'source' || data.object.ref !== ref) throw integrity('The text page was for a different record or is unavailable.');
      view.text = nextText(data, used, view.text, view.object.revision); view.textPages++;
    } catch (error) { if (!live() || g !== loadGeneration) return; view.textError = error; }
    finally { if (g === loadGeneration) view.textBusy = false; }
    paint();
  }
  // The route-level error shown when the record cannot be read at all (first load, or lost access / gone record on reload).
  function showLoadError(error) {
    view.object = null; host._pubHtml = null;
    host.innerHTML = `<section class="pub-view"><div class="empty bad pub-route-error" role="alert" data-pub-error="${error.code === 'not_found' ? 'not-found' : 'transient'}">${error.code === 'not_found' ? 'Unavailable' : PUB_COPY.loadFailed}</div>
      <button type="button" class="bt pub-retry-object">Reload</button> <button type="button" class="bt pub-back">${esc(nav.backLabel)}</button></section>`;
    host.querySelector('.pub-retry-object').onclick = start;
    host.querySelector('.pub-back').onclick = () => nav.go(nav.crumbs[nav.crumbs.length - 1].go);
  }
  function showUnavailable(data) {
    view.object = null; host._pubHtml = null;
    host.innerHTML = `<section class="pub-view">${pubUnavailableHtml(data.status, data.reason, 'Retry')}</section>`;
    host.querySelector('.pub-retry').onclick = start;
  }
  // First load of this identity (mount or an explicit new load): nothing is shown yet, so Loading is correct.
  async function start() {
    const g = ++loadGeneration;
    view.textBusy = false; view.reloading = false;
    history.restart(); expand.restart(); view.text = null; view.textPages = 0; view.textError = null; view.object = null; view.changedPublication = false; view.reloadError = null;
    host._pubHtml = null;
    host.innerHTML = '<p class="empty pad pub-loading" role="status">Loading…</p>';
    let data;
    try { data = await pubRead(scope, 'context_object', {ref, history_limit: PUB_LIMITS.history, text_limit: textLimit}, signal); }
    catch (error) {
      if (!live() || g !== loadGeneration) return;
      showLoadError(error);
      return;
    }
    if (!live() || g !== loadGeneration) return;
    if (data.status !== 'ready') { showUnavailable(data); return; }
    if (data.object.ref !== ref) { fail('Unavailable'); return; }
    view.object = data;
    try { history.apply(data, null); if (data.object.kind === 'source') { view.text = nextText(data, null, null, data.revision); view.textPages = view.text ? 1 : 0; } }
    catch (error) { history.error = error; }
    paint();
    if (data.object.kind === 'node') runExpand();
  }
  // Same-record reload: the displayed record, text, history and neighbourhood stay until a candidate (read under a new
  // generation, through the pages already loaded, all from one publication) is complete; then it replaces them at once.
  async function reload() {
    if (!view.object || view.reloading) { if (!view.object) start(); return; }
    const depths = {history: Math.max(1, history.pages), expand: Math.max(1, expand.pages), text: view.textPages};   // frozen: the candidate rereads exactly this
    const g = ++loadGeneration, rlive = () => live() && g === loadGeneration;
    history.invalidate(); expand.invalidate();   // outstanding continuations are dropped; what is shown stays as it is
    view.reloading = true; view.reloadError = null; view.textBusy = false;
    host.querySelectorAll('.pub-reload, .pub-history-more, .pub-history-again, .pub-expand-more, .pub-expand-again, .pub-expand-restart, .pub-retry, .pub-text-more, .pub-text-again').forEach(b => { b.disabled = true; });
    try {
      const data = await pubRead(scope, 'context_object', {ref, history_limit: PUB_LIMITS.history, text_limit: textLimit}, signal);
      if (!live() || g !== loadGeneration) return;
      if (data.status !== 'ready') { showUnavailable(data); return; }
      if (data.object.ref !== ref) throw integrity('The record changed to a different one.');
      const nextHistory = history.fresh();
      nextHistory.apply(data, null);
      // The first page is already applied; keep reading continuations up to the depth that was loaded.
      while (rlive() && !nextHistory.aborted && !nextHistory.error && nextHistory.hasMore && nextHistory.pages < depths.history) await nextHistory.load(rlive);
      if (!rlive()) return;
      let text = null, textPages = 0;
      if (data.object.kind === 'source') {
        text = nextText(data, null, null, data.revision); textPages = text ? 1 : 0;
        while (rlive() && text && text.has_more && textPages < depths.text) {
          const used = text.next_cursor;
          const page = await pubRead(scope, 'context_object', {ref, text_limit: textLimit, text_cursor: used}, signal);
          if (!live() || g !== loadGeneration) return;
          if (page.status !== 'ready' || page.object.kind !== 'source' || page.object.ref !== ref) throw integrity('The text page was for a different record or is unavailable.');
          text = nextText(page, used, text, data.revision); textPages++;
        }
        if (!rlive()) return;
      }
      let nextExpand = null;
      if (data.object.kind === 'node') {
        nextExpand = expand.fresh();
        await pubWalk(nextExpand, depths.expand, rlive);
        if (!rlive()) return;
        if (nextExpand.error) throw nextExpand.error;
        if (nextExpand.status === 'ready' && nextExpand.revision !== data.revision) throw integrity('The neighbourhood belongs to a different publication; reload the record.');
      }
      if (!live() || g !== loadGeneration) return;
      if (nextHistory.error) throw nextHistory.error;
      view.object = data; history.adopt(nextHistory);
      if (nextExpand) expand.adopt(nextExpand);
      view.text = text; view.textPages = textPages; view.textError = null; view.changedPublication = false;
    } catch (error) {
      if (!live() || g !== loadGeneration) return;
      if (accessLost(error) || sourceGone(error)) { showLoadError(error); view.reloading = false; return; }
      view.reloadError = error;
    } finally { if (g === loadGeneration) view.reloading = false; }
    paint();
  }
  start();
}
