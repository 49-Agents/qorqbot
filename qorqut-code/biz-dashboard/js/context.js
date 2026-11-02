// ---------------------------------------------------------------- context
// Home, search, object, graph and people pages read the published graph. Uploads are registered source
// documents addressed by their document identity (`_source/<id>`); their filing facts come from the recorded
// `filing` projection and the owner diagnostics (see context-source.js).
let _ctxTimer = null, _ctxUnwatch = null, _ctxData = null, _ctxGeneration = 0;
const ctxPageCache = new Map();
let _ctxGraph = null, _ctxNavigation = 0;
function ctxClearGraph() { if (_ctxGraph) { _ctxGraph.destroy(); _ctxGraph = null; } }
const SVG_FILE = `<svg class="fi" width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"><path d="M3 1.5h5l3 3v8h-8z"/><path d="M8 1.5v3h3"/></svg>`;
// bottom-of-sidebar / row icons, lucide 15px stroke 1.75
const SVG_BRAIN = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/><path d="M17.6 6.5a3 3 0 0 0 .4-1.375"/><path d="M6.003 5.125A3 3 0 0 0 6.401 6.5"/><path d="M3.477 10.896a4 4 0 0 1 .585-.396"/><path d="M19.938 10.5a4 4 0 0 1 .585.396"/><path d="M6 18a4 4 0 0 1-1.967-.516"/><path d="M19.967 17.484A4 4 0 0 1 18 18"/></svg>`;
const SVG_PEOPLE = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>`;
const SVG_HOME = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/></svg>`;
const SVG_INBOX = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>`;
const isTopic = p => p != null && !p.endsWith(".md");           // state.ctx is a topic path ("" = root) or a file path
const ctxFind = (node, path) => { if (!node) return null; if ((node.path || "") === path) return node; for (const c of node.children || []) { const h = ctxFind(c, path); if (h) return h; } return null; };
const PERSON_VIEW_RE = /^_people\/([a-z][a-z0-9-]{1,63})$/;     // "_people/<slug>" (no .md) is the person view, not a raw file
const OBJ_PAGE_RE = /^_obj\/(.{0,200})$/;                           // "_obj/<ref>" is a published-graph object page; the reader validates the ref
const findPerson = slug => ((_ctxData || {}).people || []).find(p => p.slug === slug);
const cap = (s, n = 70) => {
  s = String(s || "");
  if (s.length <= n) return s;
  const sp = s.lastIndexOf(" ", n - 1);
  const cut = sp > n * 0.6 ? sp : n - 1;
  return s.slice(0, cut).trimEnd() + "…";
};

let ctxFullRead = null;
const ctxDataFits = (data, page) => !!data && (ctxIndependentPage(page) ||
  (!data.navigation_only && (!data.graph_only || page === '')));
async function ctxReadFindAll(scope, query = "", signal = pageReads.signal) {
  const collection = pubCollection({limit: PUB_LIMITS.find, items: data => data.items, pageOf: data => data.page,
    keyOf: item => item.ref, read: cursor => pubRead(scope, 'context_find',
      {query, limit: PUB_LIMITS.find, ...(cursor ? {cursor} : {})}, signal)});
  do {
    await collection.load(() => !signal?.aborted);
    if (signal?.aborted) throw new Error('Request cancelled');
    if (collection.error) throw collection.error;
    if (collection.status !== 'ready' || collection.exhausted) throw new Error('People could not be loaded');
  } while (collection.hasMore);
  return {items: collection.items, revision: collection.revision};
}
async function ctxLoadData(biz, full = !ctxIndependentPage(state.ctx)) {
  if (!full) return coreData(await pageReadPost(`/biz/${encodeURIComponent(biz)}/operations/context_navigation_get`, {}));
  const signal = pageReads.signal;
  const view = state.ctx === '' ? '?view=graph' : '';
  if (ctxFullRead?.biz === biz && ctxFullRead.signal === signal && ctxFullRead.view === view) return ctxFullRead.promise;
  // The root Graph pages the published graph itself (pubMountGraph); only the sidebar navigation counts are read here.
  const baseGraph = async () => ({...coreData(await pageReadPost(`/biz/${encodeURIComponent(biz)}/operations/context_navigation_get`, {}, signal)),
    navigation_only: false, graph_only: true, canonical_graph: true, tree: {path: "", children: []}, people: [], inbox: [], dumps: []});
  const read = {biz, signal, view, promise: view === '?view=graph' ? baseGraph() : pageRead(`/biz/${encodeURIComponent(biz)}/context${view}`)};
  ctxFullRead = read;
  try { return await read.promise; } finally { if (ctxFullRead === read) ctxFullRead = null; }
}
async function renderContext(c) {
  ctxClearGraph();
  clearTimeout(_ctxTimer); _ctxTimer = null;
  if (typeof _ctxUnwatch === 'function') { _ctxUnwatch(); _ctxUnwatch = null; }
  const biz = state.biz, generation = ++_ctxGeneration;
  c.innerHTML = `<div class="ctxwrap">
    <div class="split ctxsplit"><div class="listcol ctxtree" id="ctxtree">
      <div class="ctop"><input type="search" id="ctxsearch" placeholder="Search context" autocomplete="off"></div>
      <div class="crows" id="ctxrows"></div>
      <div class="cfoot" id="ctxq"></div>
    </div><div class="pane ctxview" id="ctxview"></div></div></div>`;
  wireCtxSearch();
  if (state.ctx == null) state.ctx = ls(`ctxpage:${biz}`) ?? contextTab0 ?? "_home";
  _ctxData = ctxPageCache.get(biz) || null;
  _ctxQueue = null; _ctxPaintError = "";
  ctxTree(_ctxData || {loading:true});
  ctxQueueRefresh();
  const current = () => generation === _ctxGeneration && state.scope !== 'global' && state.biz === biz && state.tab === 'context' && c.isConnected;
  const loadTree = async () => {
    try {
      const d = await ctxLoadData(biz);
      if (!current() || !ctxDataFits(d, state.ctx)) return;
      const changed = state.ctx !== '' && (!_ctxData || ctxSig(d, state.ctx) !== ctxSig(_ctxData, state.ctx));   // the root Graph pages itself
      ctxPaint(d);
      if (changed && !ctxIndependentPage(state.ctx)) await ctxShow(state.ctx);
      if (current()) ctxWatch();
    } catch (error) {
      if (!current()) return;
      const q = $('#ctxq', c);
      q.innerHTML = `<span class="bad" role="alert">${esc(error.message || 'Could not load context.')}</span> <button type="button" class="quiet" data-context-reload>Reload context</button>`;
      q.querySelector('[data-context-reload]').onclick = event => {
        event.currentTarget.disabled = true;
        event.currentTarget.textContent = 'Reloading…';
        loadTree();
      };
    }
  };
  const treeLoad = loadTree();
  // These pages fetch their own data; the context tree need not delay them.
  const pageLoad = ctxShow(state.ctx);
  await Promise.all([treeLoad, pageLoad]);
}

// ------------------------------------------------------------- search
let _ctxSearchTimer = null;
function wireCtxSearch() {
  const inp = $("#ctxsearch");
  if (!inp) return;
  inp.oninput = () => {
    clearTimeout(_ctxSearchTimer);
    const q = inp.value.trim();
    if (q.length < 2) { if ($(".ctx-search-results")) ctxGo(state.ctx); return; }
    _ctxSearchTimer = setTimeout(() => ctxSearch(q), 250);
  };
  inp.onkeydown = e => {
    if (e.key !== "Escape") return;
    clearTimeout(_ctxSearchTimer);
    inp.value = "";
    ctxGo(state.ctx);
  };
}

function ctxNav() {
  const biz = state.biz, page = state.ctx;
  return {open: ref => ctxGo(`_obj/${ref}`), go: token => ctxGo(token), current: () => state.biz === biz && state.ctx === page && state.tab === 'context',
    crumbs: [{label: 'context base', go: '_home'}], homeLabel: 'context base', backLabel: 'Back to context base'};
}
async function ctxSearch(q) {
  const v = $("#ctxview");
  if (!v) return;
  const inp = $("#ctxsearch"), biz = state.biz, selected = state.ctx;
  if (!inp || inp.value.trim() !== q) return;
  v.classList.remove('ctxembedded-view', 'global-context-graph-view');
  ctxClearGraph();
  const nav = ctxNav();
  nav.current = () => state.biz === biz && state.ctx === selected && state.tab === 'context' && $("#ctxsearch")?.value.trim() === q;
  pubMountSearch(v, {kind: 'business', id: biz}, q, nav);
}

const ctxSig = (d, p) => (["_learning", "_tables", "_home", "_uploads"].includes(p) || p?.startsWith("_obj/") || p?.startsWith("_source/")) ? p // These views own their refreshes.
  : p === "_people" ? JSON.stringify([d?.people,d?.graph?.nodes.filter(n=>n.type==='person'),d?.graph?.edges.filter(e=>e.source.type==='person' || e.target.type==='person')])
  : JSON.stringify([(d || {}).tree,
    (d || {}).people, p ? null : (d || {}).pillars,
    (d || {}).graph]);
let _ctxLastSig = "";
// Stage 2 PR6: push replaces the 8s/20s self-reschedule. `documents`/`jobs_routines`
// events trigger an immediate check; a 60s safety poll bounds staleness the same way
// as every other Stage 2 site (section 2.3: a missed event delays a view by at most
// 60s). Root Graph (`?view=graph`) and the independent pages (_learning/_tables/
// the nav-only operation) already fetch a light shape via ctxLoadData's
// own view selection; every other page (topic/person/_people/_uploads) needs the
// full shape ctxPaint/ctxPageCache expect, so `tick` still calls the same
// ctxLoadData(biz) the old poll used -- just far less often. Scoped to this mount's
// own business: a fresh subscription replaces the old one (see the
// clear above and at renderContext's top) on every load, so there is no reason to
// accept another business's over-match here.
function ctxWatch() {
  if (typeof _ctxUnwatch === 'function') _ctxUnwatch();
  clearTimeout(_ctxTimer); _ctxTimer = null;
  const biz = state.biz, tree = $("#ctxtree");
  const tick = async () => {
    if (state.tab !== "context" || state.biz !== biz || !tree.isConnected) return;
    let d2;
    try { d2 = await ctxLoadData(biz); } catch (_) {
      if (state.tab === "context" && state.biz === biz && tree.isConnected) _ctxTimer = setTimeout(tick, 60000);
      return;
    }
    if (state.tab !== "context" || state.biz !== biz || !tree.isConnected) return;
    if (!ctxDataFits(d2, state.ctx)) { _ctxTimer = setTimeout(tick, 100); return; }   // raced a navigation mid-fetch: retry once the shape matches
    ctxPaint(d2);
    if (isTopic(state.ctx)) { if (ctxSig(d2, state.ctx) !== _ctxLastSig) ctxShow(state.ctx); }   // summaries, notes, people landed
    ctxQueueRefresh();
    _ctxTimer = setTimeout(tick, 60000);
  };
  _ctxUnwatch = typeof onChange === 'function' ? onChange(['documents', 'jobs_routines', 'context_queue'], biz, tick) : null;
  _ctxTimer = setTimeout(tick, 60000);
}

// The footer speaks only about the Context queue: counts from the process view's `context_queue`, or an explicit error.
let _ctxQueue = null, _ctxPaintError = "", _ctxQueueRead = 0;
function ctxQueueDraw() {
  const q = $("#ctxq");
  if (!q || q.querySelector("[data-context-reload]")) return;
  const bits = [];
  if (_ctxPaintError) bits.push(`<span class="bad">${esc(_ctxPaintError)}</span>`);
  if (_ctxQueue?.error) bits.push(`<span class="bad" role="alert" data-src-queue-error>${esc(_ctxQueue.error)}</span>`);
  else if (_ctxQueue) bits.push(srcQueueHtml(_ctxQueue.queue));
  q.innerHTML = bits.filter(Boolean).join(`<span class="dot">·</span>`);
}
async function ctxQueueRefresh() {
  const biz = state.biz, generation = _ctxGeneration, read = ++_ctxQueueRead, signal = pageReads.signal;
  try {
    const data = await srcReadProcesses({kind: "business", id: biz}, signal);
    if (data?.error) throw new Error("Context queue status is unavailable");
    if (!srcQueueValid(data?.context_queue)) throw new Error("Context queue status was not returned");
    if (read !== _ctxQueueRead || generation !== _ctxGeneration || state.biz !== biz) return;
    _ctxQueue = {queue: data.context_queue};
  } catch (error) {
    if (read !== _ctxQueueRead || generation !== _ctxGeneration || state.biz !== biz) return;
    _ctxQueue = {error: "Context needs attention"};
  }
  ctxQueueDraw();
  if ($("#ctxrows")) ctxTree(_ctxData || {loading: true});
}

function ctxPaint(d) {
  if (!d.error) ctxPageCache.set(state.biz, d);
  _ctxData = d;
  _ctxPaintError = d.error ? String(d.error) : "";
  ctxQueueDraw();
  ctxTree(d);
}

// Flat Context workspace navigation. Topics remain available in the overview.
function ctxTree(d) {
  const t = $("#ctxrows"), rows = [];
  const isRoot = !state.ctx;                                  // "" is the Context root; "_learning" is a page of its own
  rows.push(`<div class="item ov ${state.ctx === '_home' || (state.ctx != null && OBJ_PAGE_RE.test(state.ctx)) ? 'on' : ''}" data-p="_home"><div class="nm">${SVG_HOME}<span class="tt">Home</span></div></div>`);
  rows.push(`<div class="item ov ${state.ctx === '_learning' ? 'on' : ''}" data-p="_learning"><div class="nm">${SVG_BRAIN}<span class="tt">Learning</span></div></div>`);
  const graphNodes = d.graph?.nodes || [];
  rows.push(`<div class="ctx-counts">${[['table','Tables','_tables'],['person','People','_people']].map(([type,label,path]) => `<button type="button" data-count-page="${path}"><b>${d.loading || d.counts?.[type] == null && !graphNodes.length ? "—" : d.counts?.[type] ?? graphNodes.filter(n=>n.type===type || n.kind===type).length}</b><span>${label}</span></button>`).join('')}</div>`);
  rows.push(`<div class="item ov ${isRoot ? 'on' : ''}" data-p=""><div class="nm">${aicon('share-2','currentColor',15)}<span class="tt">Graph</span></div></div>`);
  rows.push(`<button type="button" class="item bt ${state.ctx === '_tables' ? 'on' : ''}" data-p="_tables"><div class="nm">${aicon('database', 'currentColor', 15)}<span class="tt">Tables</span></div></button>`);
  const cq = _ctxQueue?.queue, attention = cq && (cq.blocked || cq.completion_conflict || cq.queue_blocked), active = cq && (cq.queued || cq.processing || cq.discard_requested || cq.publication_pending_completion);
  const badge0 = attention ? `<span class="bdot bad"></span>` : active ? `<span class="bdot acc"></span>` : "";
  const peopleOn0 = state.ctx === "_people" || (state.ctx != null && PERSON_VIEW_RE.test(state.ctx));
  rows.push(`<div class="item bt ${peopleOn0 ? "on" : ""}" data-p="_people"><div class="nm">${SVG_PEOPLE}<span class="tt">People</span></div></div>`);
  rows.push(`<div class="item bt ${state.ctx === "_uploads" ? "on" : ""}" data-p="_uploads"><div class="nm">${SVG_INBOX}<span class="tt">Uploads</span>${badge0}</div></div>`);
  t.innerHTML = rows.join("");
  t.querySelectorAll("[data-count-page]").forEach(el => el.onclick = () => ctxGo(el.dataset.countPage));
  t.querySelectorAll(".item").forEach(el => el.onclick = () => ctxGo(el.dataset.p));
}

async function ctxGo(p) {
  const pane = $("#ctxview"), biz = state.biz, navigation = ++_ctxNavigation;
  if (navigation !== _ctxNavigation || !pane?.isConnected || state.biz !== biz || state.tab !== 'context') return;
  clearTimeout(_ctxSearchTimer);
  const search = $("#ctxsearch"); if (search) search.value = '';
  state.ctx = p;
  ctxTree(_ctxData || {loading:true});
  return ctxShow(p);
}

const ctxIndependentPage = p => p === '_learning' || p === '_tables' || p === '_home' || p === '_people' || p === '_uploads' || p?.startsWith('_obj/') || p?.startsWith('_source/');
async function ctxShow(p) {
  if (p === "_tasks") { p = ""; state.ctx = ""; }
    ls(`ctxpage:${state.biz}`, (["_tables", "_people", "_learning", "_uploads", "_home"].includes(p)) ? p : "");
  const pane = $("#ctxview");
  pane?.classList.toggle('ctxembedded-view', p === '_tables');
  ctxClearGraph();
  pane?.classList.remove('global-context-graph-view');
  pubMountGeneration++;
  jClearTimers();
  if (!ctxDataFits(_ctxData, p) && !ctxIndependentPage(p)) {
    const biz = state.biz;
    pane.innerHTML = '<p class="empty pad" role="status">Loading context…</p>';
    try {
      const data = await ctxLoadData(biz, true);
      if (state.biz !== biz || state.ctx !== p || !pane.isConnected) return;
      ctxPaint(data); ctxWatch();
    } catch (error) {
      if (state.biz === biz && state.ctx === p && pane.isConnected) pane.innerHTML = '<p class="empty pad">Could not load context. Select the page to retry.</p>';
      return;
    }
  }
  if (!_ctxData && !ctxIndependentPage(p)) {
    const pane = $('#ctxview');
    if (pane) pane.innerHTML = '<p class="empty pad" role="status">Loading context…</p>';
    return;
  }
  if (p === "_learning") {                                   // learning.js loads after this file; it may not be there at all
    _ctxLastSig = ctxSig(_ctxData, p);                       // a constant, so the tree watcher leaves this pane alone
    if (typeof openLearning === "function") return openLearning();
    const v = $("#ctxview"); if (v) v.innerHTML = `<div class="empty">learning.js not loaded</div>`;
    return;
  }
  if (p === '_tables') {
    _ctxLastSig = ctxSig(_ctxData, p);
    pane.innerHTML = '<div id="ctxembedded"></div>';
    const content = pane.firstElementChild;
    return renderTables(content);
  }
  if (p === "_home") { _ctxLastSig = ctxSig(_ctxData, p); return openHome(); }
  const om = p != null && OBJ_PAGE_RE.exec(p);
  if (om) { _ctxLastSig = ctxSig(_ctxData, p); return openObject(om[1]); }
  if (p === "_people") return openPeople();
  if (p === "_uploads") { _ctxLastSig = ctxSig(_ctxData, p); return openUploads(); }
  const src = p != null && /^_source\/(.*)$/.exec(p);
  if (src) { _ctxLastSig = ctxSig(_ctxData, p); return openSource(src[1]); }
  const m = p != null && PERSON_VIEW_RE.exec(p);
  if (m) return openPerson(m[1]);
  return isTopic(p) ? openTopic(p) : openCtx(p);
}

function ctxCrumbs(p, fileName) {
  const parts = p ? p.split("/") : [];
  let acc = "";
  const out = [`<a data-p="">context</a>`];
  parts.forEach(s => { acc = acc ? `${acc}/${s}` : s; out.push(`<span class="sep">›</span><a data-p="${esc(acc)}">${esc(s)}</a>`); });
  if (fileName) out.push(`<span class="sep">›</span><span class="cur">${esc(fileName)}</span>`);
  return `<div class="crumbs">${out.join("")}</div>`;
}
function wireCrumbs(v) { v.querySelectorAll(".crumbs a").forEach(a => a.onclick = () => ctxGo(a.dataset.p)); }

const mdInline = text => esc(text).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
const mdLines = text => `<div class="sum">${text.split("\n").map(l => l.trim()).filter(Boolean).map(l => (/^#{1,6} /.test(l)) ? `<h3>${mdInline(l.replace(/^#{1,6} /, ""))}</h3>` : l.startsWith("- ") ? `<div class="li">${mdInline(l.slice(2))}</div>` : `<p>${mdInline(l)}</p>`).join("")}</div>`;


function openHome() {
  const v = $("#ctxview");
  v.classList.remove('global-context-graph-view');
  pubMountHome(v, {kind: 'business', id: state.biz}, ctxNav());
}
function openObject(ref) {
  const v = $("#ctxview");
  v.classList.remove('global-context-graph-view');
  pubMountObject(v, {kind: 'business', id: state.biz}, ref, ctxNav());
}

// A topic opens its scoped relationship graph.
function openTopic(p) {
  ctxClearGraph();
  const v = $("#ctxview"), d = _ctxData || {}, root = !p;
  if (root) {
    _ctxLastSig = ctxSig(d, p);
    v.classList.add('global-context-graph-view');
    const biz = state.biz;
    return pubMountGraph(v, {kind: 'business', id: biz}, ctxNav(), {storageKey: `ctxgraph:published:v1:business:${biz}`, caption: 'Project context'});
  }
  const node = ctxFind(d.tree, p);
  if (!node) { v.innerHTML = `<div class="empty">no such topic</div>`; return; }
  _ctxLastSig = ctxSig(d, p);
  v.innerHTML = `${ctxCrumbs(p)}<div id="topicmap"></div>`;
  const biz = state.biz;
  _ctxGraph = contextGraph($("#topicmap"), ctxGraphData(d, node, false), { storageKey: `ctxgraph:v4:${biz}:${p || ''}`,
    onOpen:n=>contextOpenItem(biz,n.item), onSelect:(n,host)=>contextReviewPanel(host,biz,{type:n.item.type,id:n.item.id}) });
  wireCrumbs(v);
}


// a person: bio, generated summary, the notes filed under them, where they're mentioned
async function openPerson(slug) {
  const v = $("#ctxview"), d = _ctxData || {};
  let person = findPerson(slug);
  if (!person) { v.innerHTML = `<div class="empty">no such person</div>`; return; }
  const r = await api(`/biz/${state.biz}/context/person/${slug}`);
  if (state.ctx !== `_people/${slug}`) return;    // navigated away while the notes were loading
  person = {...person, ...r};
  const notesText = (r && r.notes_text) || "";
  const head = `<div class="crumbs"><a data-p="">context</a><span class="sep">›</span><a data-p="_people">People</a><span class="sep">›</span><span class="cur">${esc(person.name)}</span></div>
    <div class="th"><h2>${esc(person.name)}</h2>
      <div class="pa"><button class="ghost sm" id="ctxpedit">Edit details</button><button class="ghost sm" id="ctxpnotes">Edit notes</button></div></div>`;
  const contact = person.contact || {};
  const metaLine = [person.affiliation || "external", person.role, person.org, contact.email, contact.phone].filter(Boolean).join(" · ");
  const meta = metaLine ? `<div class="meta">${esc(metaLine)}</div>` : "";
  const akaList = person.aka || [];
  const aka = akaList.length ? `<div class="aka">${akaList.map(a => `<span class="chip">${esc(a)}</span>`).join("")}</div>` : "";
  const about = person.about ? `<p class="about">${esc(person.about)}</p>` : "";
  const notesSec = `<div class="sec"><div class="sh">Notes</div>${renderPersonNotes(notesText)}</div>`;
  const mentions = person.mentions || [];
  const ment = mentions.length ? `<div class="sec"><div class="sh">Mentioned in</div><div class="cards">${mentions.map(m => {
    const i = m.lastIndexOf("/"), topicPath = i < 0 ? "" : m.slice(0, i), file = i < 0 ? m : m.slice(i + 1);
    const node = topicPath ? ctxFind(d.tree, topicPath) : null;
    const title = (node && node.titles && node.titles[file]) || file;
    const body = node?.summaries?.[file] || node?.previews?.[file] || "";
    return `<div class="card n" data-p="${esc(m)}" title="${esc(title)}"><div class="ct">${SVG_FILE}<span class="tt">${esc(cap(title))}</span></div><div class="cb">${esc(body)}</div><div class="cs">${esc(m)}</div></div>`;
  }).join("")}</div></div>` : "";
  const contactNotes = contact.notes ? `<div class="sec"><div class="sh">How to contact</div><p class="about">${esc(contact.notes)}</p></div>` : "";
  v.innerHTML = head + meta + aka + about + contactNotes + notesSec + ment;
  if(d.graph){
    const related=document.createElement('div');related.className='work-associations';v.appendChild(related);
    contextRelatedChips(related,state.biz,{type:'person',id:person.document_id}).catch(e=>toast(e.message));
  }
  wireCrumbs(v);
  v.querySelectorAll(".card").forEach(el => el.onclick = () => ctxGo(el.dataset.p));
  const ed = $("#ctxpedit"); if (ed) ed.onclick = () => editPersonDetails(state.biz, person.document_id, () => openPerson(slug)).catch(e => toast(e.message));
  const ne = $("#ctxpnotes"); if (ne) ne.onclick = () => ctxGo(`_people/${slug}.md`);

}

// the person file's "## Notes" block: "### <date> · from <id> · <topic>/<note>.md" headings, prose/bullets between
function renderPersonNotes(text) {
  if (!text || !text.trim()) return `<div class="empty">no notes yet</div>`;
  const out = [];
  let buf = [];
  const flush = () => { if (buf.length) { out.push(mdLines(buf.join("\n"))); buf = []; } };
  text.split("\n").forEach(l => {
    if (/^###\s+/.test(l)) { flush(); out.push(`<div class="nh">${esc(l.replace(/^###\s+/, ""))}</div>`); }
    else buf.push(l);
  });
  flush();
  return out.join("");
}

// Shared compact cards, newest person or relationship activity first.
async function openPeople() {
  const v = $("#ctxview"), biz = state.biz;
  v.innerHTML = '<p class="empty pad" role="status">Loading people…</p>';
  let items;
  try { items = (await ctxReadFindAll({kind: "business", id: biz}, "")).items.map(pubPerson).filter(Boolean); }
  catch (error) {
    if (state.biz === biz && state.ctx === "_people" && v.isConnected) {
      v.innerHTML = `<div class="empty">Could not load people.<br><button type="button" class="quiet" data-context-people-reload>Reload people</button></div>`;
      v.querySelector('[data-context-people-reload]').onclick = event => { event.currentTarget.disabled = true; openPeople(); };
    }
    return;
  }
  if (state.biz !== biz || state.ctx !== "_people" || !v.isConnected) return;
  _ctxLastSig = JSON.stringify(items);
  v.innerHTML = `<div class="jrhome"><div class="jrhhead"><h2>People</h2><span class="stamp">${items.length} people</span></div><div class="jrgrid">${items.map(person => writingCard({id:person.ref,title:person.name,updated:person.updated_at,role:person.role,org:person.org})).join('')}</div>${items.length ? '' : '<p class="jrfaint">No people yet.</p>'}</div>`;
  v.querySelectorAll('.jrcard').forEach(el => el.onclick=()=>ctxGo(`_obj/${el.dataset.id}`));
}

// uploads: registered source documents, listed with their current filing state. Each kind pages on its own.
function openUploads() {
  const v = $("#ctxview"), biz = state.biz, scope = {kind: "business", id: biz};
  const live = () => state.biz === biz && state.ctx === "_uploads" && state.tab === "context" && v.isConnected;
  v.classList.remove("ctxembedded-view", "global-context-graph-view");
  v.innerHTML = `<div class="crumbs"><a data-p="">context</a><span class="sep">›</span><span class="cur">Uploads</span></div><div class="th"><h2>Uploads</h2></div>
    <div data-src-lists></div>`;
  wireCrumbs(v);
  const open = id => ctxGo(`_source/${id}`);
  srcMountList(v.querySelector("[data-src-lists]"), scope, [{kind: "context_upload", label: "Uploads"}, {kind: "context_dump", label: "Dumps"}], {live, open});
}
// one source document by its identity: name, plain filing status, bounded body
function openSource(id) {
  const v = $("#ctxview"), biz = state.biz, route = `_source/${id}`;
  v.classList.remove("ctxembedded-view", "global-context-graph-view");
  srcMountSource(v, {kind: "business", id: biz}, id, {live: () => state.biz === biz && state.ctx === route && state.tab === "context",
    crumbs: `<div class="crumbs"><a data-p="">context</a><span class="sep">›</span><a data-p="_uploads">Uploads</a><span class="sep">›</span><span class="cur">Source</span></div>`,
    wireCrumbs, openObject: ref => ctxGo(`_obj/${ref}`)});
}

const CTX_META = ["title", "by", "notes", "from", "at", "part", "also", "filed_by", "topic", "processed", "result", "attempts", "error", "last_attempt"];
function ctxSplit(text) {
  if (!text.startsWith("---\n")) return { meta: {}, body: text };
  const end = text.indexOf("\n---\n", 4);
  if (end < 0) return { meta: {}, body: text };
  const meta = {};
  text.slice(4, end).split("\n").forEach(l => { const i = l.indexOf(":"); if (i > 0) meta[l.slice(0, i).trim()] = l.slice(i + 1).trim().replace(/^"(.*)"$/, "$1"); });
  return { meta, body: text.slice(end + 5) };
}

// lossless frontmatter split for the journal editor: fm kept verbatim (incl. its trailing "\n---\n"), sep is
// the run of newlines right after it (often a blank line), body is the untouched remainder. `fm + sep + taValue`
// reassembles byte-identical when the user changes nothing.
function jSplitFm(text) {
  if (text.startsWith("---\n")) {
    const end = text.indexOf("\n---\n", 4);
    if (end >= 0) {
      const rest = text.slice(end + 5);
      const m = /^\n*/.exec(rest);
      return { fm: text.slice(0, end + 5), sep: m[0], body: rest.slice(m[0].length) };
    }
  }
  return { fm: "", sep: "", body: text };
}

// autosave state for the open journal textarea: idle 4s then a visible 8s countdown, or Ctrl/Cmd+S, or blur
let _jState = null, _jIdleT = null, _jTickT = null;
function jClearTimers() { clearTimeout(_jIdleT); clearTimeout(_jTickT); _jIdleT = _jTickT = null; }
const jGrow = ta => { ta.style.height = "auto"; ta.style.height = `${ta.scrollHeight}px`; };
function jSchedule() {
  jClearTimers();
  _jIdleT = setTimeout(() => jCountdown(8), 4000);
}
function jCountdown(n) {
  const el = $("#jsave");
  if (n <= 0) return jSave();
  if (el) el.textContent = `saving in ${n}s…`;
  _jTickT = setTimeout(() => jCountdown(n - 1), 1000);
}
async function jSave() {
  const s = _jState, ta = $("#jta"), el = $("#jsave");
  if (!s || !ta || s.saving || s.biz !== state.biz) return;
  const submitted = ta.value;
  jClearTimers();
  s.saving = true;
  if (el) el.textContent = "saving…";
  try {
    const r = await post(`/biz/${encodeURIComponent(s.biz)}/context/save`, { path: s.path, text: s.fm + s.sep + submitted,
      expected_revision: s.revision, expected_content_hash: s.content_hash });
    if (_jState !== s) return;           // navigated away while saving
    if (r.error) { toast(r.error); if (el) el.textContent = "save failed"; return; }
    if (r.warn) toast(r.warn);
    s.revision = r.document.revision; s.content_hash = r.document.content_hash;
    s.dirty = ta.value !== submitted; s.body0 = submitted;
    if (s.dirty) jSchedule();
    if (el) { el.textContent = "saved"; setTimeout(() => { if ($("#jsave") === el && el.textContent === "saved") el.textContent = ""; }, 2000); }
  } catch (e) {
    toast(`save failed: ${(e && e.message) || e}`);
    if (_jState === s) { const el2 = $("#jsave"); if (el2) el2.textContent = "save failed"; }
    // keep dirty true so the next trigger (input, blur, Ctrl+S) retries
  } finally {
    s.saving = false;
  }
}

// a file: pillar, note, _topic.md, INDEX.md, upload — an always-editable writing column
async function openCtx(p) {
  const biz = state.biz;
  const v = $("#ctxview");
  const r = await api(`/biz/${encodeURIComponent(biz)}/context/file?p=${encodeURIComponent(p)}`);
  if (state.biz !== biz || state.ctx !== p || !v.isConnected) return;
  if (r.error) { v.innerHTML = `<div class="empty">${esc(r.error)}</div>`; return; }
  const ro = p === "INDEX.md";
  const { meta } = ctxSplit(r.text);                 // for the chip row
  const { fm, sep, body } = jSplitFm(r.text);          // lossless, for editing/saving
  const dir = p.includes("/") && !p.startsWith("_") ? p.split("/").slice(0, -1).join("/") : "";
  const chips = CTX_META.filter(k => meta[k]).map(k => `<span class="chip ${k === "error" ? "bad" : ""}"><b>${esc(k)}</b>${esc(meta[k])}</span>`).join("") +
    (r.material?.material_type ? `<span class="chip"><b>type</b>${esc(r.material.material_type)}</span><span class="chip"><b>origin</b>${esc(r.material.origin)}</span>${(r.material.tags || []).map(tag=>`<span class="chip">${esc(tag)}</span>`).join('')}` : '');
  const title = meta.title || (p.endsWith("/_topic.md") ? "description" : p.split("/").pop());
  v.innerHTML = `${ctxCrumbs(dir, p.split("/").pop())}
    <div class="jcol"><div class="jhead"><h2 class="jt">${esc(title)}</h2>${ro ? `<span class="ro">${p === "INDEX.md" ? "authored template index · static" : "read only"}</span>` : `<span class="jsave" id="jsave"></span><button class="jbtn" id="jsavebtn" type="button">Save</button><button class="jbtn" id="jdiscard" type="button">Discard</button>`}</div>
    ${chips ? `<div class="chips">${chips}</div>` : ""}<hr class="jhr">
    <div id="ctx-work" class="small" aria-label="Related work"></div>
        ${ro ? `<div class="jbody">${esc(body)}</div>` : `<textarea class="jta" id="jta" spellcheck="false" placeholder="Start writing…"></textarea>`}</div>`;
  wireCrumbs(v);
  if(r.document_id)workRelatedChips(v.querySelector('#ctx-work'),biz,'documents',r.document_id,async()=>{
    if(_jState?.dirty){await jSave();if(_jState?.dirty)return false;}
    return true;
  });
  if (ro) return;
  const ta = $("#jta");
  ta.value = body;
  jGrow(ta);
  _jState = { biz, path: p, revision:r.revision, content_hash:r.content_hash, fm, sep, body0: body, dirty: false, saving: false };
  ta.oninput = () => { _jState.dirty = true; jGrow(ta); jSchedule(); };
  ta.onkeydown = e => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) { e.preventDefault(); jSave(); }
    else if ((e.ctrlKey || e.metaKey) && e.key === "Enter") jSave();
  };
  ta.onblur = event => { if (_jState && _jState.dirty && !event.relatedTarget?.closest('#jdiscard')) jSave(); };
  $("#jsave").onclick = () => { if (_jState && _jState.dirty) jSave(); };
  $("#jsavebtn").onclick = () => jSave();
  // discard: back to the text the file opened with (or last had after a save elsewhere); nothing sent
  $("#jdiscard").onclick = () => {
    if (!_jState) return;
    jClearTimers();
    ta.value = _jState.body0; _jState.dirty = false; jGrow(ta);
    const el = $("#jsave"); if (el) { el.textContent = "discarded"; setTimeout(() => { if ($("#jsave") === el && el.textContent === "discarded") el.textContent = ""; }, 2000); }
  };
}

async function editPersonDetails(biz, id, after) {
  const {item} = await workCall(biz, 'context_items_get', {item:{type:'person', id}});
  const contact = item.contact || {};
  const field = (name, label, value, type='text', max=4000) => `<label>${label}<input name="${name}" type="${type}" maxlength="${max}" value="${esc(value || '')}"></label>`;
  const body = `<div class="data-form">
    <label>Affiliation<select name="affiliation"><option value="external" ${item.affiliation==='external'?'selected':''}>External</option><option value="internal" ${item.affiliation==='internal'?'selected':''}>Internal</option></select></label>
    ${field('email','Email',contact.email,'email',320)}${field('phone','Phone',contact.phone,'tel',100)}
    <label>How to contact<textarea name="contact-notes" rows="3" maxlength="4000">${esc(contact.notes || '')}</textarea></label>
    <label>Summary<textarea name="summary" rows="4">${esc(item.summary || '')}</textarea></label>
    <p class="data-form-error" role="alert"></p></div>`;
  let attempt;
  modal({stack:true, title:esc(item.label || item.name || 'Person'), body, actions:[{label:'Save', primary:true, onClick:async(close, el)=>{
    const button=el.querySelector('.mfoot .primary'); button.disabled=true;
    try {
      const value=name=>el.querySelector(`[name="${name}"]`).value;
      const email=el.querySelector('[name="email"]');
      if (!email.checkValidity()) { email.reportValidity(); return; }
      const changes={};
      if(value('affiliation')!==item.affiliation) changes.affiliation=value('affiliation');
      if(value('summary')!==(item.summary || '')) changes.summary=value('summary');
      const editedContact={email:value('email'),phone:value('phone'),notes:value('contact-notes')};
      const contactChanges=Object.fromEntries(Object.entries(editedContact).filter(([key,v])=>v!==(contact[key] || '')));
      if(Object.keys(contactChanges).length) changes.contact=contactChanges;
      if(Object.keys(changes).length) {
        const binding=JSON.stringify(changes);
        if(attempt?.binding!==binding) attempt={binding,key:newRequestId()};
        await workCall(biz,'context_people_update',{document_id:id,changes},{expected_revision:item.revision,idempotency_key:attempt.key});
      }
      close(); if(state.biz===biz) await after?.();
    } catch(e) {el.querySelector('.data-form-error').textContent=e.message;}
    finally {if(button.isConnected) button.disabled=false;}
  }}]});
}
