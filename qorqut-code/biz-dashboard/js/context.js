// ---------------------------------------------------------------- context
// Home, search, object, graph and people pages read the context base. Uploads
// retain the legacy navigation for compatibility, with the owner's base ledger
// shown alongside it whenever the base reader is available.
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
const titleCase = s => String(s || "").replace(/[-_]+/g, " ").replace(/\b\w/g, c => c.toUpperCase());
const fmtAgo = t => { const s = Math.max(0, Math.round(Date.now() / 1000 - t)); return s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`; };
const isTopic = p => p != null && !p.endsWith(".md");           // state.ctx is a topic path ("" = root) or a file path
const ctxFind = (node, path) => { if (!node) return null; if ((node.path || "") === path) return node; for (const c of node.children || []) { const h = ctxFind(c, path); if (h) return h; } return null; };
const PERSON_VIEW_RE = /^_people\/([a-z][a-z0-9-]{1,63})$/;     // "_people/<slug>" (no .md) is the person view, not a raw file
const OBJ_PAGE_RE = /^_obj\/(<object>[^<>]{1,200}<\/object>)$/;    // "_obj/<object>…</object>" is a context-base object page
const uploadId = p => {
  const data = _ctxData || {};
  const registered = [...(data.inbox || []), ...(data.dumps || [])].find(row => String(row.path || '').replace(/^context\//, '') === p);
  if (registered) return registered.id;
  const legacy = /^_(?:inbox|dumps)\/(\d{4}-\d{2}-\d{2}-\d{4}-[0-9a-z]{4})\.md$/.exec(p || '');
  return legacy ? legacy[1] : '';
};
const ctxMapStamp = (d, id) => { const m = ((d || {}).maps || []).find(x => x.id === id); return m ? `${m.at}|${m.nodes}` : ""; };
let _ctxMapAt = "", _ctxMapping = false;                        // the upload view's mind map state, so the watcher knows when to redraw
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
async function ctxLoadData(biz, full = !ctxIndependentPage(state.ctx)) {
  if (!full) return coreData(await pageReadPost(`/biz/${encodeURIComponent(biz)}/operations/context_navigation_get`, {}));
  const signal = pageReads.signal;
  const view = state.ctx === '' ? '?view=graph' : '';
  if (ctxFullRead?.biz === biz && ctxFullRead.signal === signal && ctxFullRead.view === view) return ctxFullRead.promise;
  const baseGraph = async () => {
    const items = (coreData(await pageReadPost(`/biz/${encodeURIComponent(biz)}/operations/context_find`, {query: "", limit: 500})).items || []);
    const nodes = items.filter(item => !item.is_relationship).map(item => ({type: item.kind === "person" ? "person" : "topic", id: item.ref, label: item.name, summary: item.summary || item.definition || ""}));
    const nodeTypes = new Map(nodes.map(node => [node.id, node.type]));
    const edges = [];
    items.filter(item => item.is_relationship).forEach(rel => {
      const endpoints = rel.endpoints || [];
      for (let i = 1; i < endpoints.length; i++) edges.push({source: {type: nodeTypes.get(endpoints[0]) || "topic", id: endpoints[0]}, target: {type: nodeTypes.get(endpoints[i]) || "topic", id: endpoints[i]}, role: (rel.tags || []).join(", ") || "related", updated_at: rel.updated_at});
    });
    return {graph_only: true, tree: {path: "", children: []}, people: [], inbox: [], dumps: [], graph: {nodes, edges}, counts: {person: nodes.filter(n => n.type === "person").length}};
  };
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
  ctxTree(_ctxData || {loading:true});
  const current = () => generation === _ctxGeneration && state.scope !== 'global' && state.biz === biz && state.tab === 'context' && c.isConnected;
  const treeLoad = ctxLoadData(biz).then(async d => {
    if (!current() || !ctxDataFits(d, state.ctx)) return;
    const changed = !_ctxData || ctxSig(d, state.ctx) !== ctxSig(_ctxData, state.ctx);
    ctxPaint(d);
    if (changed && !ctxIndependentPage(state.ctx)) await ctxShow(state.ctx);
    if (current()) ctxWatch();
  }).catch(error => {
    if (current()) $('#ctxq', c).innerHTML = `<span class="bad" role="alert">${esc(error.message || 'Could not load context.')}</span>`;
  });
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

function ctxMark(snippet, q) {
  const s = snippet || "";
  const i = s.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return esc(s);
  return `${esc(s.slice(0, i))}<mark>${esc(s.slice(i, i + q.length))}</mark>${esc(s.slice(i + q.length))}`;
}

async function ctxSearch(q) {
  if (storyView && !await storyCanLeave()) return;
  const v = $("#ctxview");
  if (!v) return;
  const biz = state.biz, selected = state.ctx;
  let hits;
  try { hits = (coreData(await pageReadPost(`/biz/${encodeURIComponent(biz)}/operations/context_find`, {query: q})).items) || []; }
  catch (error) { if (v.isConnected && state.biz === biz) v.innerHTML = `<div class="empty">${esc(error.message || 'Search failed.')}</div>`; return; }
  const inp = $("#ctxsearch");
  if (!v.isConnected || state.biz !== biz || state.ctx !== selected || !inp || inp.value.trim() !== q) return;
  storyDispose();
  v.classList.remove('ctxembedded-view');
  ctxClearGraph();
  v.innerHTML = ctxSearchHtml(q, hits);
  wireObjectLinks(v);
}

function ctxBusy(d) {
  const s = d.summarizing || {};
  return !!((d.filing && d.filing.current) || (d.inbox || []).some(i => ["queued", "running", "waiting_signoff", "cancelling"].includes(i.status)) || s.current || s.queued);
}
const ctxSig = (d, p) => (["_learning", "_tables", "_home"].includes(p) || p?.startsWith("_stories") || p?.startsWith("_obj/")) ? p // These views own their refreshes.
  : p === "_people" ? JSON.stringify([d?.people,d?.graph?.nodes.filter(n=>n.type==='person'),d?.graph?.edges.filter(e=>e.source.type==='person' || e.target.type==='person')])
  : p === "_uploads" ? JSON.stringify([(d || {}).inbox || [], (d || {}).dumps || []])
  : JSON.stringify([(d || {}).tree,
    (d || {}).people, p ? null : (d || {}).pillars,
    (d || {}).graph]);
let _ctxLastSig = "";
// Stage 2 PR6: push replaces the 8s/20s self-reschedule. `documents`/`jobs_routines`
// events trigger an immediate check; a 60s safety poll bounds staleness the same way
// as every other Stage 2 site (section 2.3: a missed event delays a view by at most
// 60s). Root Graph (`?view=graph`) and the independent pages (_learning/_tables/
// _stories*, the nav-only operation) already fetch a light shape via ctxLoadData's
// own view selection; every other page (topic/person/_people/_uploads) needs the
// full shape ctxPaint/ctxPageCache expect, so `tick` still calls the same
// ctxLoadData(biz) the old poll used -- just far less often. Scoped to this mount's
// own business, like stories.js: a fresh subscription replaces the old one (see the
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
    const uid = uploadId(state.ctx);
    if (state.ctx && state.ctx.startsWith("_inbox/") && !(d2.inbox || []).some(i => `_inbox/${i.id}.md` === state.ctx)) {
      ctxGo((d2.dumps || []).some(u => u.id === uid) ? `_dumps/${uid}.md` : "");     // filed while we looked: follow it
    }
    else if (isTopic(state.ctx)) { if (ctxSig(d2, state.ctx) !== _ctxLastSig) ctxShow(state.ctx); }   // summaries, notes, people landed
    else if (uid && (ctxMapStamp(d2, uid) !== _ctxMapAt || (_ctxMapping && !ctxBusy(d2)))) openCtx(state.ctx);   // its map landed (or failed)
    _ctxTimer = setTimeout(tick, 60000);
  };
  _ctxUnwatch = typeof onChange === 'function' ? onChange(['documents', 'jobs_routines'], biz, tick) : null;
  _ctxTimer = setTimeout(tick, 60000);
}

function ctxPaint(d) {
  if (!d.error) ctxPageCache.set(state.biz, d);
  _ctxData = d;
  const q = $("#ctxq"), inbox = d.inbox || [], f = d.filing || {}, s = d.summarizing || {}, cur = f.current;
  const failed = inbox.filter(i => i.failed).length, waiting = inbox.filter(i => ["queued", "running", "waiting_signoff", "cancelling"].includes(i.status)).length;
  const bits = [];
  if (d.error) bits.push(`<span class="bad">${esc(d.error)}</span>`);
  if (d.has_filer === false) bits.push(`<span class="bad">no agents/_filer in this business; copy it from biz-template</span>`);
  if (cur) bits.push(`<span class="run">filing ${esc(cur.id)} · ${fmtAgo(cur.started)}</span>`);
  if (waiting) bits.push(`${waiting} waiting${cur ? "" : ` · next sweep within ${Math.round((f.every || 300) / 60)} min`}`);
  if (failed) bits.push(`<span class="bad">${failed} failed</span>`);
  if (s.current) {
    const t = String(s.current.topic || "");
    bits.push(`<span class="run">${t.startsWith("map:") ? `mapping ${esc(t.slice(4))}` : `summarising ${esc(t)}`}</span>${s.queued ? ` +${s.queued}` : ""}`);
  }
  // idle: nothing; the bar only speaks while something is queued, running or failed
  q.innerHTML = bits.join(`<span class="dot">·</span>`);
  ctxTree(d);
}

// Flat Context workspace navigation. Topics remain available in the overview.
function ctxTree(d) {
  const t = $("#ctxrows"), rows = [];
  const isRoot = !state.ctx;                                  // "" is the Context root; "_learning" is a page of its own
  rows.push(`<div class="item ov ${state.ctx === '_home' || (state.ctx != null && OBJ_PAGE_RE.test(state.ctx)) ? 'on' : ''}" data-p="_home"><div class="nm">${SVG_HOME}<span class="tt">Home</span></div></div>`);
  rows.push(`<div class="item ov ${state.ctx === '_learning' ? 'on' : ''}" data-p="_learning"><div class="nm">${SVG_BRAIN}<span class="tt">Learning</span></div></div>`);
  const graphNodes = d.graph?.nodes || [];
  rows.push(`<div class="ctx-counts">${[['story','Stories','_stories'],['table','Tables','_tables'],['person','People','_people']].map(([type,label,path]) => `<button type="button" data-count-page="${path}"><b>${d.loading ? "—" : d.counts?.[type] ?? graphNodes.filter(n=>n.type===type).length}</b><span>${label}</span></button>`).join('')}</div>`);
  rows.push(`<div class="item ov ${isRoot ? 'on' : ''}" data-p=""><div class="nm">${aicon('share-2','currentColor',15)}<span class="tt">Graph</span></div></div>`);
  for (const [key, label, icon] of [['_stories', 'Stories', `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><path d="M6 7v10M12 5h8M12 11h6M12 19h8"/></svg>`], ['_tables', 'Tables', aicon('database', 'currentColor', 15)]]) {
    rows.push(`<button type="button" class="item bt ${(state.ctx === key || key === '_stories' && state.ctx?.startsWith('_stories/')) ? 'on' : ''}" data-p="${key}"><div class="nm">${icon}<span class="tt">${label}</span></div></button>`);
  }
  const inbox0 = d.inbox || [], failed0 = inbox0.filter(i => i.failed).length, waiting0 = inbox0.filter(i => ["queued", "running", "waiting_signoff", "cancelling"].includes(i.status)).length;
  const badge0 = failed0 ? `<span class="bdot bad"></span>` : waiting0 ? `<span class="bdot acc"></span>` : "";
  const peopleOn0 = state.ctx === "_people" || (state.ctx != null && PERSON_VIEW_RE.test(state.ctx));
  rows.push(`<div class="item bt ${peopleOn0 ? "on" : ""}" data-p="_people"><div class="nm">${SVG_PEOPLE}<span class="tt">People</span></div></div>`);
  rows.push(`<div class="item bt ${state.ctx === "_uploads" ? "on" : ""}" data-p="_uploads"><div class="nm">${SVG_INBOX}<span class="tt">Uploads</span>${badge0}</div></div>`);
  t.innerHTML = rows.join("");
  t.querySelectorAll("[data-count-page]").forEach(el => el.onclick = () => ctxGo(el.dataset.countPage));
  t.querySelectorAll(".item").forEach(el => el.onclick = () => ctxGo(el.dataset.p));
}

async function ctxGo(p) {
  const pane = $("#ctxview"), biz = state.biz, navigation = ++_ctxNavigation;
  if (typeof storyView !== 'undefined' && storyView && !await storyCanLeave()) return;
  if (navigation !== _ctxNavigation || !pane?.isConnected || state.biz !== biz || state.tab !== 'context') return;
  clearTimeout(_ctxSearchTimer);
  const search = $("#ctxsearch"); if (search) search.value = '';
  state.ctx = p;
  ctxTree(_ctxData || {loading:true});
  return ctxShow(p);
}

const ctxIndependentPage = p => p === '_learning' || p === '_tables' || p === '_home' || p?.startsWith('_stories') || p?.startsWith('_obj/');
async function ctxShow(p) {
  if (p === "_tasks") { p = ""; state.ctx = ""; }
  ls(`ctxpage:${state.biz}`, (["_tables", "_people", "_learning", "_uploads", "_home"].includes(p) || p?.startsWith("_stories")) ? p : "");
  storyDispose();
  const pane = $("#ctxview");
  pane?.classList.toggle('ctxembedded-view', (['_tables'].includes(p) || p?.startsWith('_stories')));
  ctxClearGraph();
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
  if ((['_tables'].includes(p) || p?.startsWith('_stories'))) {
    _ctxLastSig = ctxSig(_ctxData, p);
    pane.innerHTML = '<div id="ctxembedded"></div>';
    const content = pane.firstElementChild;
    if (p?.startsWith('_stories')) return renderStories(content);
    if (p === '_tables') return renderTables(content);
  }
  if (p === "_home") { _ctxLastSig = ctxSig(_ctxData, p); return openHome(); }
  const om = p != null && OBJ_PAGE_RE.exec(p);
  if (om) { _ctxLastSig = ctxSig(_ctxData, p); return openObject(om[1]); }
  if (p === "_people") return openPeople();
  if (p === "_uploads") return openUploads();
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

// --- context base rendering (pure; tests/test_context_home_render.js slices this block) ---
const refName = ref => String(ref || "").replace(/^<object>|<\/object>$/g, "").replace(/_/g, " ");
const objChip = (ref, name) => ref ? `<span class="chip go" data-ref="${esc(ref)}">${esc(name || refName(ref))}</span>` : "";
const day = t => esc(String(t || "").slice(0, 10));
function ctxSearchHtml(q, hits) {
  const rows = hits.map(h => `<div class="card n" data-ref="${esc(h.ref)}" title="${esc(h.name || "")}"><div class="ct"><span class="kd">${esc(h.kind || "")}</span><span class="tt">${esc(cap(h.name || refName(h.ref)))}</span></div>
    <div class="cb">${ctxMark(h.definition || "", q)}</div>${h.home_ref ? `<div class="cs">${esc(refName(h.home_ref))}</div>` : ""}</div>`).join("");
  return `<div class="sec ctx-search-results"><div class="sh">Search: ${esc(q)} <span class="stamp">${hits.length} hit${hits.length === 1 ? "" : "s"}</span></div>
    <div class="cards">${rows || `<div class="empty">no matches in the context base</div>`}</div></div>`;
}
// the home pack, exactly what an agent's Context Home section carries: living brief, room by salience, recent impacts, open actions
function ctxHomeHtml(pack) {
  const home = pack.home || {}, room = pack.B_room || [], recent = pack.recent_impacts || [], actions = pack.open_actions || [];
  const head = `<div class="crumbs"><span class="cur">context base</span></div><div class="th"><h2>${esc(home.name || "Home")}</h2><div class="pa">${objChip(home.ref, "open")}</div></div>`;
  const brief = `<div class="sec"><div class="sh">Living brief</div>${(pack.A_brief || "").trim() ? mdLines(pack.A_brief) : `<div class="empty">No brief yet.</div>`}</div>`;
  const roomRows = room.map(r => {
    const others = (r.with || []).map(w => objChip(w.ref, w.name)).join("");
    const last = r.last_impact ? `<div class="cs">${day(r.last_impact.at || r.last_impact.created_at)} · ${esc(cap(r.last_impact.substance || r.last_impact.headline || "", 120))}</div>` : "";
    return `<div class="card n" data-ref="${esc(r.relationship)}"><div class="ct"><span class="kd">${esc((r.tags || []).join(", ") || "related")}</span><span class="tt">${others || esc(refName(r.relationship))}</span></div>${last}</div>`;
  }).join("");
  const roomSec = `<div class="sec"><div class="sh">Room <span class="stamp">${room.length}${pack.room_total > room.length ? ` of ${pack.room_total}` : ""} by salience</span></div>${roomRows ? `<div class="cards">${roomRows}</div>` : `<div class="empty">Nothing in the room yet.</div>`}</div>`;
  const recentRows = recent.map(i => `<div class="card n"${i.object_ref ? ` data-ref="${esc(i.object_ref)}"` : ""}><div class="ct"><span class="kd">${day(i.created_at)}</span><span class="tt">${esc(cap(i.headline || "", 90))}</span></div><div class="cb">${esc(i.substance || i.summary || "")}</div></div>`).join("");
  const recentSec = pack.recent_impacts ? `<div class="sec"><div class="sh">Recent impacts</div>${recentRows ? `<div class="cards">${recentRows}</div>` : `<div class="empty">Nothing filed yet.</div>`}</div>` : `<div class="sec">${objChip(home.ref, "View history and actions")}</div>`;
  const actionRows = actions.map(a => `<div class="card n"${a.object_ref ? ` data-ref="${esc(a.object_ref)}"` : ""}><div class="ct"><span class="kd">${esc(a.status || "open")}</span><span class="tt">${esc(cap(a.text || a.title || a.summary || a.id || "", 110))}</span></div>${a.due ? `<div class="cs">due ${day(a.due)}</div>` : ""}</div>`).join("");
  const actionSec = actions.length ? `<div class="sec"><div class="sh">Open actions</div><div class="cards">${actionRows}</div></div>` : "";
  return head + brief + roomSec + recentSec + actionSec;
}
// one object: card facts, relationships, its timeline of impacts, and the neighbourhood context_expand returns
function ctxObjectHtml(surface, expand, changes = []) {
  const o = surface.object || {}, name = o.name || refName(o.ref);
  const head = `<div class="crumbs"><a data-p="_home">context base</a><span class="sep">›</span><span class="cur">${esc(name)}</span></div>
    <div class="th"><h2>${esc(name)}</h2><div class="pa">${o.home_ref && o.home_ref !== o.ref ? objChip(o.home_ref, `home: ${refName(o.home_ref)}`) : ""}</div></div>`;
  const metaBits = [o.kind, o.created_at ? `since ${String(o.created_at).slice(0, 10)}` : "", o.created_by_upload ? `from upload ${String(o.created_by_upload).slice(0, 8)}` : ""].filter(Boolean);
  const meta = metaBits.length ? `<div class="meta">${esc(metaBits.join(" · "))}</div>` : "";
  const definition = (surface.definition || o.definition) ? `<p class="about">${esc(surface.definition || o.definition)}</p>` : "";
  const attrs = Object.entries(surface.attributes || o.attributes || {}).filter(([k, v]) => v != null && v !== "" && k !== "aliases");
  const aliases = ((surface.attributes || o.attributes || {}).aliases || []);
  const chips = [...attrs.map(([k, v]) => `<span class="chip"><b>${esc(k)}</b>${esc(Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : v)}</span>`),
    ...aliases.map(a => `<span class="chip" title="also called"><b>aka</b>${esc(a)}</span>`), ...(o.tags || []).map(t => `<span class="chip">${esc(t)}</span>`)].join("");
  const attrSec = chips ? `<div class="chips">${chips}</div>` : "";
  const rels = (surface.relationships || []).map(r => `<div class="card n" data-ref="${esc(r.ref)}"><div class="ct"><span class="kd">${esc((r.tags || []).join(", ") || "related")}</span><span class="tt">${(r.others || []).map(x => objChip(x)).join("") || esc(r.name || refName(r.ref))}</span></div>${r.definition ? `<div class="cb">${esc(cap(r.definition, 160))}</div>` : ""}</div>`).join("");
  const relSec = `<div class="sec"><div class="sh">Relationships <span class="stamp">${(surface.relationships || []).length}</span></div>${rels ? `<div class="cards">${rels}</div>` : `<div class="empty">none</div>`}</div>`;
  const tl = (surface.timeline || []).map(i => `<div class="card n"><div class="ct"><span class="kd">${day(i.created_at)}</span><span class="tt">${esc(cap(i.headline || "", 90))}</span></div><div class="cb">${esc(i.substance || "")}</div>${i.upload_id ? `<div class="cs">${esc(String(i.upload_id).slice(0, 12))}</div>` : ""}</div>`).join("");
  const tlSec = `<div class="sec"><div class="sh">Timeline <span class="stamp">${(surface.timeline || []).length}</span></div>${tl ? `<div class="cards">${tl}</div>` : `<div class="empty">no impacts yet</div>`}</div>`;
  const changeRows = changes.map(change => {
    const actor = change.actor_name || change.actor_id || "unknown actor";
    const kind = change.action_kind === "direct_edit" ? "Direct edit" : change.action_kind === "filing" ? "Filing" : titleCase(change.action_kind || "Change");
    const scope = change.submission_business_id || change.business_id || "global";
    const evidence = JSON.stringify({before: change.before_state, after: change.after_state});
    return `<details class="card n ctx-change"><summary><span class="kd">${esc(kind)}</span><span class="tt">${esc(actor)}</span><span class="cs">${esc(change.outcome || "recorded")} · ${esc(String(change.created_at || "").slice(0, 19))}</span></summary><div class="cb">${esc(change.rationale || change.source_reference || "")}</div><div class="cs">scope ${esc(scope)} · ${esc(evidence)}</div></details>`;
  }).join("");
  const changeSec = changes.length ? `<div class="sec"><div class="sh">Change history <span class="stamp">${changes.length}</span></div><div class="cards">${changeRows}</div></div>` : "";
  const near = ((expand && expand.nodes) || []).filter(n => n.ref !== o.ref).map(n => objChip(n.ref, n.name)).join("");
  const nearSec = near ? `<div class="sec"><div class="sh">Neighbourhood</div><div class="chips">${near}</div></div>` : "";
  const brief = (surface.brief || "").trim() ? `<div class="sec"><div class="sh">Living brief</div>${mdLines(surface.brief)}</div>` : "";
  return head + meta + definition + attrSec + brief + relSec + tlSec + changeSec + nearSec;
}
// --- end context base rendering ---

function wireObjectLinks(v) {
  v.querySelectorAll("[data-ref]").forEach(el => el.onclick = e => { e.stopPropagation(); ctxGo(`_obj/${el.dataset.ref}`); });
}
async function openHome() {
  const v = $("#ctxview"), biz = state.biz;
  v.innerHTML = `<p class="empty pad" role="status">Loading the context base…</p>`;
  let pack;
  try { pack = coreData(await pageReadPost(`/biz/${encodeURIComponent(biz)}/operations/context_home`, {})); }
  catch (error) { if (state.biz === biz && state.ctx === "_home" && v.isConnected) v.innerHTML = `<div class="empty">${esc(error.message || "Could not load the context base.")}</div>`; return; }
  if (state.biz !== biz || state.ctx !== "_home" || !v.isConnected) return;
  v.innerHTML = ctxHomeHtml(pack || {});
  wireObjectLinks(v);
}
async function openObject(ref, pageOverride = null) {
  const v = $("#ctxview"), biz = state.biz, page = pageOverride || `_obj/${ref}`;
  v.innerHTML = `<p class="empty pad" role="status">Loading…</p>`;
  let surface, expand = null, changes = [];
  try {
    surface = coreData(await pageReadPost(`/biz/${encodeURIComponent(biz)}/operations/context_object`, {ref}));
    try { expand = coreData(await pageReadPost(`/biz/${encodeURIComponent(biz)}/operations/context_expand`, {ref})); } catch (_) { expand = null; }
    try { changes = coreData(await pageReadPost(`/biz/${encodeURIComponent(biz)}/operations/context_changes`, {ref, limit: 25})).changes || []; } catch (_) { changes = []; }
  } catch (error) { if (state.biz === biz && state.ctx === page && v.isConnected) v.innerHTML = `<div class="empty">${esc(error.message || "No such object.")}</div>`; return; }
  if (state.biz !== biz || state.ctx !== page || !v.isConnected) return;
  v.innerHTML = ctxObjectHtml(surface || {}, expand, changes);
  wireCrumbs(v);
  wireObjectLinks(v);
}

// Resolve a legacy authored path to a canonical object by durable content
// identity exposed through context_find. This is only a reader projection:
// raw uploads, INDEX.md and unmapped files stay on the legacy file reader.
async function baseObjectForLegacyPath(path, text, documentId = "") {
  if (!path || path === "INDEX.md" || path.startsWith("_inbox/") || path.startsWith("_dumps/") || path.startsWith("_maps/")) return null;
  // The legacy reader carries the original UUID even when the path has been
  // renamed. Ask the compatibility projection to resolve that durable mapping
  // before considering any prose/name fallback.
  if (documentId) {
    try {
      const projected = coreData(await pageReadPost(`/biz/${encodeURIComponent(state.biz)}/operations/context_get`, {document_id: documentId}));
      const doc = projected?.document;
      if (doc?.canonical_ref) return {ref: doc.canonical_ref, kind: String(doc.kind || "").replace(/^context_/, "")};
    } catch (_) { /* unmapped legacy material continues through the old reader */ }
  }
  const {meta} = ctxSplit(text || "");
  const leaf = path.split("/").pop().replace(/\.md$/, "");
  const parent = path.split("/").slice(-2, -1)[0] || "";
  const queries = [...new Set([meta.name, meta.title, leaf === "_topic" ? parent : leaf].filter(q => String(q || "").trim().length >= 2))];
  const wanted = path.startsWith("_people/") ? "person" : leaf === "_topic" ? "topic" : "note";
  for (const query of queries) {
    try {
      const items = coreData(await pageReadPost(`/biz/${encodeURIComponent(state.biz)}/operations/context_find`, {query, limit: 30})).items || [];
      const exact = items.find(item => item.kind === wanted && String(item.name || "").toLowerCase() === String(query).toLowerCase());
      const candidate = exact || items.find(item => item.kind === wanted);
      if (candidate?.ref) return candidate;
    } catch (_) { return null; }
  }
  return null;
}

// A topic opens its scoped relationship graph.
function openTopic(p) {
  ctxClearGraph();
  const v = $("#ctxview"), d = _ctxData || {}, root = !p;
  const node = root ? Object.assign({ title: "context", about: "", children: [], notes: [] }, d.tree || {}) : ctxFind(d.tree, p);
  if (!node) { v.innerHTML = `<div class="empty">no such topic</div>`; return; }
  _ctxLastSig = ctxSig(d, p);
  const graph = ctxGraphData(d, node, root);
  v.innerHTML = `${root ? '' : ctxCrumbs(p)}<div id="topicmap"></div>`;
  const biz = state.biz;
  _ctxGraph = contextGraph($("#topicmap"), graph, { storageKey: `ctxgraph:v4:${biz}:${p || ''}`,
    onOpen:n=>contextOpenItem(biz,n.item),
    onSelect:(n,host)=>contextReviewPanel(host,biz,{type:n.item.type,id:n.item.id}) });
  wireCrumbs(v);
}


// a person: bio, generated summary, the notes filed under them, where they're mentioned
async function openPerson(slug) {
  const v = $("#ctxview"), d = _ctxData || {};
  let person = findPerson(slug);
  if (!person) { v.innerHTML = `<div class="empty">no such person</div>`; return; }
  try {
    if (person.document_id) {
      const projected = coreData(await pageReadPost(`/biz/${encodeURIComponent(state.biz)}/operations/context_get`, {document_id: person.document_id}));
      if (projected?.document?.canonical_ref) return openObject(projected.document.canonical_ref, `_people/${slug}`);
    }
    const found = coreData(await pageReadPost(`/biz/${encodeURIComponent(state.biz)}/operations/context_find`, {query: person.name, limit: 20})).items || [];
    const base = found.find(item => item.kind === "person" && String(item.name || "").toLowerCase() === String(person.name || "").toLowerCase());
    if (base?.ref) return openObject(base.ref, `_people/${slug}`);
  } catch (_) { /* legacy person view remains the compatibility fallback */ }
  const s = d.summarizing || {}, busy = s.current && s.current.topic === `person:${slug}`;
  const r = await api(`/biz/${state.biz}/context/person/${slug}`);
  if (state.ctx !== `_people/${slug}`) return;    // navigated away while the notes were loading
  person = {...person, ...r};
  const notesText = (r && r.notes_text) || "";
  const head = `<div class="crumbs"><a data-p="">context</a><span class="sep">›</span><a data-p="_people">People</a><span class="sep">›</span><span class="cur">${esc(person.name)}</span></div>
    <div class="th"><h2>${esc(person.name)}</h2>
      <div class="pa"><button class="ghost sm" id="ctxpedit">Edit details</button><button class="ghost sm" id="ctxpnotes">Edit notes</button><button class="ghost sm" id="ctxpsum" ${busy ? "disabled" : ""}>${busy ? "summarising…" : "Refresh summary"}</button></div></div>`;
  const contact = person.contact || {};
  const metaLine = [person.affiliation || "external", person.role, person.org, contact.email, contact.phone].filter(Boolean).join(" · ");
  const meta = metaLine ? `<div class="meta">${esc(metaLine)}</div>` : "";
  const akaList = person.aka || [];
  const aka = akaList.length ? `<div class="aka">${akaList.map(a => `<span class="chip">${esc(a)}</span>`).join("")}</div>` : "";
  const about = person.about ? `<p class="about">${esc(person.about)}</p>` : "";
  const sum = person.summary ? `<div class="sec"><div class="sh">Summary <span class="stamp">${esc((person.summarized || "").replace("T", " ").replace(/:\d\dZ/, "Z"))}</span></div>${mdLines(person.summary)}</div>`
    : `<div class="sec"><div class="sh">Summary</div><div class="empty">${busy ? "being written now" : "No summary yet."}</div></div>`;
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
  v.innerHTML = head + meta + aka + about + contactNotes + sum + notesSec + ment;
  if(d.graph){
    const related=document.createElement('div');related.className='story-associations';v.appendChild(related);
    contextRelatedChips(related,state.biz,{type:'person',id:person.document_id}).catch(e=>toast(e.message));
  }
  wireCrumbs(v);
  v.querySelectorAll(".card").forEach(el => el.onclick = () => ctxGo(el.dataset.p));
  const ed = $("#ctxpedit"); if (ed) ed.onclick = () => editPersonDetails(state.biz, person.document_id, () => openPerson(slug)).catch(e => toast(e.message));
  const ne = $("#ctxpnotes"); if (ne) ne.onclick = () => ctxGo(`_people/${slug}.md`);
  const rs = $("#ctxpsum"); if (rs) rs.onclick = async () => {
    rs.disabled = true;
    const rr = await post(`/biz/${state.biz}/context/summarize`, { person: slug });
    toast(rr.error || `summarising ${person.name}`);
    const d2 = await api(`/biz/${state.biz}/context`); ctxPaint(d2); openPerson(slug); ctxWatch();
  };
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
  v.innerHTML = '<p class="empty pad" role="status">Loading people from the context base…</p>';
  let items;
  try { items = (coreData(await pageReadPost(`/biz/${encodeURIComponent(biz)}/operations/context_find`, {query: "", limit: 500})).items || []).filter(item => item.kind === "person"); }
  catch (error) { v.innerHTML = `<div class="empty">${esc(error.message || "Could not load people.")}</div>`; return; }
  if (state.biz !== biz || state.ctx !== "_people" || !v.isConnected) return;
  _ctxLastSig = JSON.stringify(items);
  v.innerHTML = `<div class="jrhome"><div class="jrhhead"><h2>People</h2><span class="stamp">${items.length} in context base</span></div><div class="jrgrid">${items.map(person => writingCard({id:person.ref,title:person.name,updated:person.updated_at,role:person.attributes?.role || '',org:person.attributes?.org || ''})).join('')}</div>${items.length ? '' : '<p class="jrfaint">No people yet.</p>'}</div>`;
  v.querySelectorAll('.jrcard').forEach(el => el.onclick=()=>ctxGo(`_obj/${el.dataset.id}`));
}

// context base receipt (metadata.context_base, written by the context.base_file job): one small chip per upload
function ctxBaseChip(item) {
  const b = item && item.context_base;
  if (!b || !b.status) return "";
  const s = b.summary || {};
  const counts = b.status === "filed" ? [s.developments != null ? `${s.developments} dev` : "", s.impacts != null ? `${s.impacts} imp` : "", s.actions != null ? `${s.actions} act` : ""].filter(Boolean).join(" · ") : "";
  const title = b.status === "filed" ? `context base: filed${s.commit ? ` · ${String(s.commit).slice(0, 8)}` : ""}${counts ? ` · ${counts}` : ""}` : `context base: failed · ${String(b.error || "").slice(0, 120)}`;
  return `<span class="cnt ${b.status === "failed" ? "bad" : ""}" title="${esc(title)}">base ${b.status === "filed" ? "✓" : "✗"}</span>`;
}
// uploads: what's still filing (or failed) up top, then every filed dump — the tree's old Inbox/Uploads sections
async function openUploads() {
  const v = $("#ctxview"), d = _ctxData || {}, inbox = d.inbox || [], dumps = d.dumps || [];
  let baseUploads = [];
  try {
    baseUploads = (coreData(await pageReadPost(`/biz/${encodeURIComponent(state.biz)}/operations/context_filings`, {limit: 60})).uploads || []);
  } catch (_) { /* legacy installations and non-owner viewers keep the old reader */ }
  const base = baseUploads.length ? `<div class="sec"><div class="sh">Context base ledger</div><div class="cards">${baseUploads.map(u => {
    const p = u.provenance || {}, a = p.attribution || {};
    const who = a.display_name || a.principal_id || p.by || "unknown actor";
    const state = u.status || "pending";
    const title = u.notes || u.upload_id;
    return `<div class="card u d" data-p="_dumps/${esc(u.upload_id)}.md"><div class="ct"><span class="tt">${esc(title)}</span><span class="cnt ${state === "failed" ? "bad" : ""}" title="${esc(`base ${state}; ${who}`)}">base ${esc(state)}</span></div><div class="cb">${esc(who)}${p.origin ? ` · ${esc(p.origin)}` : ""}</div><div class="cs">${esc(u.upload_id)} · ${String(u.text || "").length} chars</div></div>`;
  }).join("")}</div></div>` : "";
  _ctxLastSig = ctxSig(d, "_uploads");
  const head = `<div class="crumbs"><a data-p="">context</a><span class="sep">›</span><span class="cur">Uploads</span></div><div class="th"><h2>Uploads</h2></div>`;
  const filing = inbox.length ? `<div class="sec"><div class="sh">Filing</div><div class="cards">${inbox.map(i => {
    const cur = d.filing && d.filing.current && d.filing.current.id === i.id;
    const st = cur ? `<span class="run">filing</span>` : i.failed ? `<span class="bad">failed</span>` : esc(({queued: "queued", running: "filing", waiting_signoff: "awaiting approval", cancelled: "cancelled", cancelling: "cancelling"})[i.status] || "status unavailable");
    return `<div class="card u ${i.failed ? "failed" : ""}" data-p="_inbox/${esc(i.id)}.md"><div class="ct"><span class="tt">${esc(i.notes || i.topic || i.preview || i.id)}</span>${i.retryable ? `<button class="retry" data-id="${esc(i.id)}" title="queue it again">retry</button>` : ""}</div><div class="cb">${st}${ctxBaseChip(i)}</div><div class="cs">${esc(i.id)} · ${i.chars} chars${i.error ? ` · ${esc(String(i.error).slice(0, 80))}` : ""}</div></div>`;
  }).join("")}</div></div>` : "";
  const up = dumps.length ? `<div class="sec"><div class="sh">Uploads</div><div class="cards">${dumps.slice(0, 60).map(u => `<div class="card u d" data-p="_dumps/${esc(u.id)}.md"><div class="ct"><span class="tt">${esc(u.notes || u.topic || u.preview || u.id)}</span>${u.map ? `<span class="cnt" title="has a mind map">map</span>` : ""}${ctxBaseChip(u)}</div><div class="cb">${esc(u.id)}</div><div class="cs">${u.chars} chars</div></div>`).join("")}</div></div>${dumps.length > 60 ? `<div class="empty pad">${dumps.length - 60} older uploads; search finds them</div>` : ""}`
    : `<div class="empty">nothing uploaded yet</div>`;
  v.innerHTML = head + base + filing + up;
  wireCrumbs(v);
  v.querySelectorAll(".card").forEach(el => el.onclick = e => { if (e.target.closest(".retry")) return; ctxGo(el.dataset.p); });
  v.querySelectorAll(".retry").forEach(el => el.onclick = e => { e.stopPropagation(); ctxRetry(el.dataset.id, el); });
}

const ctxRetryPending = new Set();
async function ctxRetry(id, button) {
  if (ctxRetryPending.has(id)) return;
  ctxRetryPending.add(id);
  if (button) button.disabled = true;
  try {
    const r = await post(`/biz/${state.biz}/context/retry`, { id });
    toast(r.error ? r.error : `${id} filing queued`);
    renderContext($("#content"));
  } catch (error) {
    toast(error.message || "Could not queue filing");
  } finally {
    ctxRetryPending.delete(id);
    if (button) button.disabled = false;
  }
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


async function openCtx(p) {
  const biz = state.biz;
  const v = $("#ctxview");
  const r = await api(`/biz/${encodeURIComponent(biz)}/context/file?p=${encodeURIComponent(p)}`);
  if (state.biz !== biz || state.ctx !== p || !v.isConnected) return;
  if (r.error) { v.innerHTML = `<div class="empty">${esc(r.error)}</div>`; return; }
  const ro = p === "INDEX.md" || p.startsWith("_dumps/");
  const { meta } = ctxSplit(r.text);                 // for the chip row
  const { fm, sep, body } = jSplitFm(r.text);          // lossless, for editing/saving
  const dir = p.includes("/") && !p.startsWith("_") ? p.split("/").slice(0, -1).join("/") : "";
  const chips = CTX_META.filter(k => meta[k]).map(k => `<span class="chip ${k === "error" ? "bad" : ""}"><b>${esc(k)}</b>${esc(meta[k])}</span>`).join("") +
    (r.material?.material_type ? `<span class="chip"><b>type</b>${esc(r.material.material_type)}</span><span class="chip"><b>origin</b>${esc(r.material.origin)}</span>${(r.material.tags || []).map(tag=>`<span class="chip">${esc(tag)}</span>`).join('')}` : '');
  const uid = uploadId(p);
  if (!uid) {
    const base = await baseObjectForLegacyPath(p, r.text, r.document_id);
    if (base && state.ctx === p) return openObject(base.ref, p);
  }
  let um = null, filing = null;
  if (uid) {
    try {
      const base = coreData(await pageReadPost(`/biz/${encodeURIComponent(state.biz)}/operations/context_upload_map`, {upload_id: uid}));
      um = {map: {tree: base.outline, nodes: (base.outline || []).length, at: base.updated_at}, trace: {where: "_dumps"}, mapping: false};
    } catch (_) { um = await api(`/biz/${state.biz}/context/map?id=${uid}`); }
    try { filing = coreData(await pageReadPost(`/biz/${encodeURIComponent(state.biz)}/operations/context_filings`, {upload_id: uid})); }
    catch (_) { /* legacy mode and non-owner viewers keep the old upload detail */ }
  }
  if (state.ctx !== p) return;                                                     // navigated away meanwhile
  const title = meta.title || (uid ? (meta.notes || meta.topic || `Upload ${uid}`) : p.endsWith("/_topic.md") ? "description" : p.split("/").pop());
  v.innerHTML = `${ctxCrumbs(dir, p.split("/").pop())}
    <div class="jcol"><div class="jhead"><h2 class="jt">${esc(title)}</h2>${ro ? `<span class="ro">${p === "INDEX.md" ? "generated" : "filed upload, immutable"}</span>` : `<span class="jsave" id="jsave"></span><button class="jbtn" id="jsavebtn" type="button">Save</button><button class="jbtn" id="jdiscard" type="button">Discard</button>`}</div>
    ${chips ? `<div class="chips">${chips}</div>` : ""}<hr class="jhr">
    <div id="ctx-stories" class="small" aria-label="Related stories"></div>
    ${uid ? uploadFilingEvidence(filing) + uploadSections(uid, um || {}) : ""}
    ${ro ? `<div class="jbody">${esc(body)}</div>` : `<textarea class="jta" id="jta" spellcheck="false" placeholder="Start writing…"></textarea>`}</div>`;
  wireCrumbs(v);
  if(r.document_id)storyRelatedChips(v.querySelector('#ctx-stories'),biz,uid?'uploads':'documents',r.document_id,async()=>{
    if(_jState?.dirty){await jSave();if(_jState?.dirty)return false;}
    return true;
  });
  if (uid) wireUpload(v, uid, um || {});
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

function uploadFilingEvidence(data) {
  if (!data?.upload) return "";
  const upload = data.upload, provenance = upload.provenance || {}, attribution = provenance.attribution || {};
  const decisions = data.decisions || {}, verify = decisions.verify || {}, calls = decisions.model_calls || decisions.calls || [];
  const actor = attribution.display_name || attribution.name || attribution.principal_id || provenance.by || "unresolved actor";
  const kind = attribution.principal_kind || attribution.kind || "unknown";
  const summary = decisions.summary || {};
  const counts = [summary.developments != null ? `${summary.developments} developments` : "",
    summary.impacts != null ? `${summary.impacts} impacts` : "",
    summary.actions != null ? `${summary.actions} actions` : ""].filter(Boolean).join(" · ");
  const evidence = [
    `<span class="chip"><b>actor</b>${esc(actor)}</span>`,
    `<span class="chip"><b>kind</b>${esc(kind)}</span>`,
    provenance.origin ? `<span class="chip"><b>source</b>${esc(provenance.origin)}</span>` : "",
    upload.status ? `<span class="chip"><b>status</b>${esc(upload.status)}</span>` : "",
    counts ? `<span class="chip"><b>applied</b>${esc(counts)}</span>` : "",
    verify.model ? `<span class="chip"><b>verifier</b>${esc(verify.model)}</span>` : "",
    Array.isArray(calls) ? `<span class="chip"><b>model calls</b>${calls.length}</span>` : ""
  ].filter(Boolean).join("");
  return `<div class="sec ctx-filing-evidence"><div class="sh">Base filing evidence</div><div class="chips">${evidence}</div>` +
    (provenance.document_id || upload.upload_id ? `<div class="cs">upload ${esc(provenance.document_id || upload.upload_id)}</div>` : "") + `</div>`;
}

// an upload's two maps: the model-drawn mind map of its text, and where it went (notes, topics, people)
function uploadSections(uid, um) {
  const m = um.map, t = um.trace || {}, filed = t.where === "_dumps", notes = t.notes || [];
  _ctxMapAt = m ? `${m.at}|${m.nodes}` : ""; _ctxMapping = !!um.mapping;
  const stamp = m ? `<span class="stamp">${m.nodes} nodes · ${esc(String(m.at || "").slice(0, 10))}${m.model ? ` · ${esc(m.model)}` : ""}</span>` : "";
  const btn = um.mapping ? `<button class="ghost sm" disabled>drawing…</button>` : `<button class="ghost sm" id="dumpdraw">${m ? "Redraw" : "Draw mind map"}</button>`;
  const mapBody = m ? `<div id="dumpmap"></div>` : `<div class="empty">${um.mapping ? "being drawn now from the text below" : um.error ? esc(um.error) : "No mind map yet."}</div>`;
  const cards = notes.length ? `<div class="cards">${notes.map(n => `<div class="card n" data-p="${esc(n.path)}" data-tree="${esc(n.tree || "context")}" title="${esc(n.title)}"><div class="ct">${SVG_FILE}<span class="tt">${esc(cap(n.title || n.path))}</span></div>
      <div class="cb">${esc(n.summary || "")}</div><div class="cs">${esc(n.topic)}${(n.people || []).length ? ` · ${esc(n.people.join(", "))}` : ""}</div></div>`).join("")}</div>`
    : `<div class="empty">${filed ? `no notes were cut from it${t.result ? `: ${esc(t.result)}` : ""}` : "Not filed yet."}</div>`;
  const chip = (path, label, extra = "") => `<span class="chip${path.startsWith("marketing-content/") ? "" : " go"}" data-p="${esc(path)}" ${extra}>${esc(label)}</span>`;
  const topics = (t.topics || []).length || (t.also || []).length ? `<div class="row"><span class="lbl">topics</span>${(t.topics || []).map(x => chip(x, x)).join("")}${(t.also || []).map(x => chip(x, `${x} (also)`, 'title="cross-referenced, not filed into"')).join("")}</div>` : "";
  const people = (t.people || []).length ? `<div class="row"><span class="lbl">people</span>${t.people.map(pp => chip(`_people/${pp.slug}`, pp.name)).join("")}</div>` : "";
  // a call or meeting: who the owner said was there and when. A picked Person links to their page; a typed name is plain.
  const up = [...((_ctxData || {}).inbox || []), ...((_ctxData || {}).dumps || [])].find(u => u.id === uid) || {};
  const who = (up.participants || []).map(x => {
    const person = x.person_id && ((_ctxData || {}).people || []).find(pp => pp.document_id === x.person_id);
    return person ? chip(`_people/${person.slug}`, x.name) : `<span class="chip">${esc(x.name)}</span>`;
  });
  const encounter = ["call", "meeting"].includes(up.origin)
    ? `<div class="sec trace"><div class="row"><span class="lbl">${esc(up.origin)}</span>${who.length ? `<span class="small muted">with</span>${who.join("")}` : ""}${up.occurred_on ? `<span class="small muted">on ${esc(up.occurred_on)}</span>` : ""}</div></div>` : "";
  return `${encounter}<div class="sec"><div class="sh">Mind map ${stamp}<span class="ml">${btn}</span></div>${mapBody}</div>
    <div class="sec trace"><div class="sh">Where it went</div>${cards}${topics}${people}</div>`;
}
function wireUpload(v, uid, um) {
  if (um.map && um.map.tree) mindmap($("#dumpmap"), ctxMapIdea(um.map.tree));
  v.querySelectorAll(".trace .card, .trace .chip.go").forEach(el => el.onclick = () => el.dataset.tree === "marketing-content" ? mkOpenNote(el.dataset.p) : ctxGo(el.dataset.p));
  const b = $("#dumpdraw");
  if (b) b.onclick = async () => {
    b.disabled = true;
    const r = await post(`/biz/${state.biz}/context/map`, { id: uid });
    toast(r.error || `drawing the mind map of ${uid}`);
    const d = await api(`/biz/${state.biz}/context`); ctxPaint(d); ctxWatch();
    if (state.ctx && uploadId(state.ctx) === uid) openCtx(state.ctx);
  };
}

async function editPersonDetails(biz, id, after) {
  const {item} = await storyCall(biz, 'context_items_get', {item:{type:'person', id}});
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
        await storyCall(biz,'context_people_update',{document_id:id,changes},{expected_revision:item.revision,idempotency_key:attempt.key});
      }
      close(); if(state.biz===biz) await after?.();
    } catch(e) {el.querySelector('.data-form-error').textContent=e.message;}
    finally {if(button.isConnected) button.disabled=false;}
  }}]});
}
