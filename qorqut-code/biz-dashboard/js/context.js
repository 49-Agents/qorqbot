// ---------------------------------------------------------------- context
// Upload originals, browse logical topic memberships, and inspect the shared relationship map.
// Authored text and filer-maintained summaries remain separate.
let _ctxTimer = null, _ctxData = null, _ctxGeneration = 0;
let _ctxGraph = null;
function ctxClearGraph() { if (_ctxGraph) { _ctxGraph.destroy(); _ctxGraph = null; } }
const SVG_FOLDER = `<svg class="fo" width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"><path d="M1.5 3.5h4l1.5 1.5h5.5v6h-11z"/></svg>`;
const SVG_FILE = `<svg class="fi" width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"><path d="M3 1.5h5l3 3v8h-8z"/><path d="M8 1.5v3h3"/></svg>`;
const SVG_PIN = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1z"/></svg>`;
// bottom-of-sidebar / row icons, lucide 15px stroke 1.75
const SVG_LIB = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="m16 6 4 14"/><path d="M12 6v14"/><path d="M8 8v12"/><path d="M4 4v16"/></svg>`;
const SVG_BRAIN = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/><path d="M17.6 6.5a3 3 0 0 0 .4-1.375"/><path d="M6.003 5.125A3 3 0 0 0 6.401 6.5"/><path d="M3.477 10.896a4 4 0 0 1 .585-.396"/><path d="M19.938 10.5a4 4 0 0 1 .585.396"/><path d="M6 18a4 4 0 0 1-1.967-.516"/><path d="M19.967 17.484A4 4 0 0 1 18 18"/></svg>`;
const SVG_PEOPLE = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>`;
const SVG_INBOX = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>`;
const titleCase = s => String(s || "").replace(/[-_]+/g, " ").replace(/\b\w/g, c => c.toUpperCase());
const fmtAgo = t => { const s = Math.max(0, Math.round(Date.now() / 1000 - t)); return s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`; };
const isTopic = p => p != null && !p.endsWith(".md");           // state.ctx is a topic path ("" = root) or a file path
const ctxCount = n => (n.notes || []).length + (n.children || []).reduce((s, k) => s + ctxCount(k), 0);
const ctxFind = (node, path) => { if (!node) return null; if ((node.path || "") === path) return node; for (const c of node.children || []) { const h = ctxFind(c, path); if (h) return h; } return null; };
const PERSON_VIEW_RE = /^_people\/([a-z][a-z0-9-]{1,63})$/;     // "_people/<slug>" (no .md) is the person view, not a raw file
const uploadId = p => {
  const data = _ctxData || {};
  const registered = [...(data.inbox || []), ...(data.dumps || [])].find(row => String(row.path || '').replace(/^context\//, '') === p);
  if (registered) return registered.id;
  const legacy = /^_(?:inbox|dumps)\/(\d{4}-\d{2}-\d{2}-\d{4}-[0-9a-z]{4})\.md$/.exec(p || '');
  return legacy ? legacy[1] : '';
};
const ctxMapStamp = (d, id) => { const m = ((d || {}).maps || []).find(x => x.id === id); return m ? `${m.at}|${m.nodes}` : ""; };
let _ctxMapAt = "", _ctxMapping = false;                        // the upload view's mind map state, so the watcher knows when to redraw
const personNoteCount = p => Array.isArray(p.notes) ? p.notes.length : (p.notes || 0);
const findPerson = slug => ((_ctxData || {}).people || []).find(p => p.slug === slug);
const cap = (s, n = 70) => {
  s = String(s || "");
  if (s.length <= n) return s;
  const sp = s.lastIndexOf(" ", n - 1);
  const cut = sp > n * 0.6 ? sp : n - 1;
  return s.slice(0, cut).trimEnd() + "…";
};

// one dot color per top-level topic: its department if it has one, a couple of hand-picked extras, else a
// stable hash pick so the same topic always lands on the same color
function topicColor(slug) {
  if (DEPTS[slug]) return DEPTS[slug].color;
  if (slug === "product") return COLORS.blue;
  if (slug === "misc") return COLORS.ink;
  const keys = Object.keys(COLORS);
  let sum = 0;
  for (let i = 0; i < slug.length; i++) sum += slug.charCodeAt(i);
  return COLORS[keys[sum % keys.length]];
}
// last time each top-level topic was opened, so the sidebar can show a "something new" dot; per business
const ctxSeenKey = () => `ctxseen:${state.biz}`;
const ctxSeenGet = () => { try { return JSON.parse(ls(ctxSeenKey()) || "{}"); } catch (e) { return {}; } };
const ctxSeenSave = m => ls(ctxSeenKey(), JSON.stringify(m));

async function renderContext(c) {
  ctxClearGraph();
  clearTimeout(_ctxTimer); _ctxTimer = null;
  const biz = state.biz, generation = ++_ctxGeneration;
  const d = await api(`/biz/${biz}/context`);
  if (generation !== _ctxGeneration || state.scope === "global" || state.biz !== biz || state.tab !== "context" || !c.isConnected) return;
  c.innerHTML = `<div class="ctxwrap">
    <div class="split ctxsplit"><div class="listcol ctxtree" id="ctxtree">
      <div class="ctop"><button class="primary" id="ctxup">${ICO_UPLOAD}Upload context</button><input type="search" id="ctxsearch" placeholder="Search context" autocomplete="off"></div>
      <div class="lnstatus" id="lnstatus"></div>
      <div class="crows" id="ctxrows"></div>
      <div class="cfoot" id="ctxq"></div>
    </div><div class="pane ctxview" id="ctxview"></div></div></div>`;
  $("#ctxup").onclick = () => uploadModal();
  wireCtxSearch();
  if (state.ctx == null) state.ctx = ls(`ctxpage:${biz}`) || contextTab0 || "";
  ctxPaint(d);
  if (typeof lnFetch === "function") lnFetch();   // fills the status pane and the rail badge; learning.js repaints itself after that
  await ctxShow(state.ctx);
  if (state.biz === biz && state.tab === "context" && c.isConnected) ctxWatch(d);
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
  const r = await api(`/biz/${biz}/context/search?q=${encodeURIComponent(q)}`);
  const inp = $("#ctxsearch");
  if (!v.isConnected || state.biz !== biz || state.ctx !== selected || !inp || inp.value.trim() !== q) return;
  const hits = r.hits || [];
  storyDispose();
  v.classList.remove('ctxembedded-view');
  ctxClearGraph();
  const rows = hits.map(h => `<div class="card" data-p="${esc(h.path)}" data-k="${esc(h.kind)}" title="${esc(h.title)}"><div class="ct"><span class="kd">${esc(h.kind)}</span><span class="tt">${esc(cap(h.title))}</span></div>
    <div class="cb">${ctxMark(h.snippet, q)}</div><div class="cs">${esc(h.path)}</div></div>`).join("");
  v.innerHTML = `<div class="sec ctx-search-results"><div class="sh">Search: ${esc(q)} <span class="stamp">${hits.length} hit${hits.length === 1 ? "" : "s"}</span></div>
    <div class="cards">${rows || `<div class="empty">no matches</div>`}</div></div>`;
  v.querySelectorAll(".card").forEach(el => el.onclick = () => {
    const p = el.dataset.p;
    if (el.dataset.k === "person") return ctxGo(p.replace(/\.md$/, ""));
    if (el.dataset.k === "map") {                          // a map hit opens its upload
      const id = p.replace(/^_maps\//, "").replace(/\.md$/, "");
      return ctxGo(((_ctxData || {}).dumps || []).some(u => u.id === id) ? `_dumps/${id}.md` : `_inbox/${id}.md`);
    }
    if (p.endsWith("/_topic.md")) return ctxGo(p.split("/").slice(0, -1).join("/"));
    ctxGo(p);
  });
}

function ctxBusy(d) {
  const s = d.summarizing || {};
  return !!((d.filing && d.filing.current) || (d.inbox || []).some(i => ["queued", "running", "waiting_signoff", "cancelling"].includes(i.status)) || s.current || s.queued);
}
// Poll the tree while the tab is open: every 8 s while the queues are busy, every 20 s when idle (work
// arrives from the filer sweep and from other clients too). Re-render only what changed.
const ctxSig = (d, p) => ["_learning", "_stories", "_tables", "_tasks"].includes(p) ? p // These views own their refreshes.
  : p === "_people" ? JSON.stringify((d || {}).people || [])
  : p === "_uploads" ? JSON.stringify([(d || {}).inbox || [], (d || {}).dumps || []])
  : JSON.stringify([ls("ctxmapmode") === "1" ? (d || {}).tree : ctxFind((d || {}).tree, p || ""),
    !p || ls("ctxmapmode") === "1" ? (d || {}).people : null, p ? null : (d || {}).pillars,
    ls("ctxmapmode") === "1" ? (d || {}).graph : null]);
let _ctxLastSig = "";
function ctxWatch(d) {
  clearTimeout(_ctxTimer); _ctxTimer = null;
  const biz = state.biz, tree = $("#ctxtree");
  const tick = async () => {
    if (state.tab !== "context" || !$("#ctxtree")) { _ctxTimer = null; return; }
    const d2 = await api(`/biz/${biz}/context`);
    if (state.tab !== "context" || state.biz !== biz || !tree.isConnected) return;
    ctxPaint(d2);
    const uid = uploadId(state.ctx);
    if (state.ctx && state.ctx.startsWith("_inbox/") && !(d2.inbox || []).some(i => `_inbox/${i.id}.md` === state.ctx)) {
      ctxGo((d2.dumps || []).some(u => u.id === uid) ? `_dumps/${uid}.md` : "");     // filed while we looked: follow it
    }
    else if (isTopic(state.ctx)) { if (ctxSig(d2, state.ctx) !== _ctxLastSig) ctxShow(state.ctx); }   // summaries, notes, people landed
    else if (uid && (ctxMapStamp(d2, uid) !== _ctxMapAt || (_ctxMapping && !ctxBusy(d2)))) openCtx(state.ctx);   // its map landed (or failed)
    _ctxTimer = setTimeout(tick, ctxBusy(d2) ? 8000 : 20000);
  };
  _ctxTimer = setTimeout(tick, ctxBusy(d) ? 8000 : 20000);
}

function ctxPaint(d) {
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
  rows.push(`<div class="item ov ${isRoot ? "on" : ""}" data-p=""><div class="nm">${SVG_LIB}<span class="tt">Context</span></div></div>`);
  for (const [key, label, icon] of [['_stories', 'Stories', `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><path d="M6 7v10M12 5h8M12 11h6M12 19h8"/></svg>`], ['_tasks', 'All tasks', SVG_TINBOX], ['_tables', 'Tables', aicon('database', 'currentColor', 15)]]) {
    rows.push(`<button type="button" class="item bt ${state.ctx === key ? 'on' : ''}" data-p="${key}"><div class="nm">${icon}<span class="tt">${label}</span></div></button>`);
  }
  const inbox0 = d.inbox || [], failed0 = inbox0.filter(i => i.failed).length, waiting0 = inbox0.filter(i => ["queued", "running", "waiting_signoff", "cancelling"].includes(i.status)).length;
  const badge0 = failed0 ? `<span class="bdot bad"></span>` : waiting0 ? `<span class="bdot acc"></span>` : "";
  const peopleOn0 = state.ctx === "_people" || (state.ctx != null && PERSON_VIEW_RE.test(state.ctx));
  rows.push(`<div class="item bt ${peopleOn0 ? "on" : ""}" data-p="_people"><div class="nm">${SVG_PEOPLE}<span class="tt">People</span></div></div>`);
  rows.push(`<div class="item ov ${state.ctx === "_learning" ? "on" : ""}" data-p="_learning"><div class="nm">${SVG_BRAIN}<span class="tt">Learning</span></div></div>`);
  rows.push(`<div class="item bt ${state.ctx === "_uploads" ? "on" : ""}" data-p="_uploads"><div class="nm">${SVG_INBOX}<span class="tt">Uploads</span>${badge0}</div></div>`);
  t.innerHTML = rows.join("");
  t.querySelectorAll(".item").forEach(el => el.onclick = () => ctxGo(el.dataset.p));
}

async function ctxGo(p) {
  const pane = $("#ctxview"), biz = state.biz;
  if (typeof storyView !== 'undefined' && storyView && !await storyCanLeave()) return;
  if (!pane?.isConnected || state.biz !== biz || state.tab !== 'context') return;
  clearTimeout(_ctxSearchTimer);
  const search = $("#ctxsearch"); if (search) search.value = '';
  state.ctx = p;
  if (_ctxData) ctxTree(_ctxData);
  return ctxShow(p);
}

function ctxShow(p) {
  ls(`ctxpage:${state.biz}`, ["_stories", "_tables", "_tasks", "_people", "_learning", "_uploads"].includes(p) ? p : "");
  storyDispose();
  const pane = $("#ctxview");
  pane?.classList.toggle('ctxembedded-view', ['_stories', '_tables', '_tasks'].includes(p));
  ctxClearGraph();
  jClearTimers();
  if (p === "_learning") {                                   // learning.js loads after this file; it may not be there at all
    _ctxLastSig = ctxSig(_ctxData, p);                       // a constant, so the tree watcher leaves this pane alone
    if (typeof openLearning === "function") return openLearning();
    const v = $("#ctxview"); if (v) v.innerHTML = `<div class="empty">learning.js not loaded</div>`;
    return;
  }
  if (['_stories', '_tables', '_tasks'].includes(p)) {
    _ctxLastSig = ctxSig(_ctxData, p);
    pane.innerHTML = '<div id="ctxembedded"></div>';
    const content = pane.firstElementChild;
    if (p === '_stories') return renderStories(content);
    if (p === '_tables') return renderTables(content);
    return renderAllTasks(content);
  }
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

const mdLines = text => `<div class="sum">${text.split("\n").map(l => l.trim()).filter(Boolean).map(l => l.startsWith("- ") ? `<div class="li">${esc(l.slice(2))}</div>` : `<p>${esc(l)}</p>`).join("")}</div>`;

// a topic as a folder: description, generated summary, subtopics, notes
function openTopic(p) {
  ctxClearGraph();
  const v = $("#ctxview"), d = _ctxData || {}, root = !p;
  const node = root ? Object.assign({ title: "context", about: "", children: [], notes: [] }, d.tree || {}) : ctxFind(d.tree, p);
  if (!node) { v.innerHTML = `<div class="empty">no such topic</div>`; return; }
  _ctxLastSig = ctxSig(d, p);
  if (!root) {                          // mark the top-level topic (and its descendants) as seen today, for the sidebar's freshness dot
    const top = p.split("/")[0], seen = ctxSeenGet(), today = new Date().toISOString().slice(0, 10);
    if (seen[top] !== today) { seen[top] = today; ctxSeenSave(seen); if (_ctxData) ctxTree(_ctxData); }
  }
  const s = d.summarizing || {}, busy = s.current && (s.current.topic === p || (s.current.topic || "").startsWith(p + "/"));
  const kids = node.related_topics || node.children || [], notes = node.notes || [];
  const mappable = d.graph ? d.graph.nodes.length : kids.length || notes.length || (root && ((d.people || []).length || (d.pillars || []).length));
  const mapMode = !!mappable && ls("ctxmapmode") === "1";        // the folder as a graph instead of cards; remembered
  const head = `${ctxCrumbs(root ? "" : p.split("/").slice(0, -1).join("/"), root ? "" : node.slug)}
    <div class="th"><h2>${esc(root ? "All context" : node.title)}</h2>
      <div class="pa">${mappable ? `<button class="ghost sm pab" id="tpmap" title="${mapMode ? "back to the cards" : "this folder as a graph"}">${mapMode ? "Cards" : "Map"}</button>` : ""}${root ? "" : `<button class="ghost sm pab" id="tpedit" title="edit _topic.md">Edit description</button><button class="ghost sm pab" id="tpsum" ${busy ? "disabled" : ""}>${busy ? "summarising…" : "Refresh summary"}</button>`}</div></div>
    ${node.about ? `<p class="about">${esc(node.about)}</p>` : ""}`;
  const sum = root ? "" : node.summary ? `<div class="sec"><div class="sh">Summary <span class="stamp">${esc((node.summarized || "").replace("T", " ").replace(/:\d\dZ/, "Z"))}</span></div>${mdLines(node.summary)}</div>`
    : `<div class="sec"><div class="sh">Summary</div><div class="empty">${busy ? "being written now" : notes.length || kids.length ? "not written yet; it comes after the next filing or save, or press Refresh summary" : "empty topic"}</div></div>`;
  const pill = root ? `<div class="sec"><div class="sh">${SVG_PIN}Pinned</div><div class="cards">${(d.pillars || []).map(n => `<div class="card f" data-p="${esc(n)}"><span class="pin">${SVG_PIN}</span><div class="ct"><span class="tt">${esc(titleCase(n.replace(/\.md$/, "")))}</span></div><div class="cb">owner-written</div></div>`).join("")}</div></div>` : "";
  const subs = kids.length ? `<div class="sec"><div class="sh">${root ? "Topics" : "Subtopics"}</div><div class="cards">${kids.map(k => { const kt = k.title || k.slug; return `<div class="card t" data-p="${esc(k.path)}" title="${esc(kt)}"><div class="ct">${SVG_FOLDER}<span class="tt">${esc(cap(kt))}</span><span class="cnt">${ctxCount(k)}</span></div><div class="cb">${esc(k.about || "")}</div><div class="cs">${esc(k.slug)}${k.summary ? "" : " · no summary yet"}</div></div>`; }).join("")}</div></div>` : "";
  const nts = notes.length ? `<div class="sec"><div class="sh">Files</div><div class="cards">${notes.map(n => { const path = node.paths?.[n] || `${p}/${n}`, m = ctxNoteMeta(path.split('/').pop()), ft = (node.titles || {})[n] || n, body = node.summaries?.[n] || node.previews?.[n] || ""; return `<div class="card n" data-p="${esc(path)}" title="${esc(ft)}"><div class="ct">${SVG_FILE}<span class="tt">${esc(cap(ft))}</span></div><div class="cb">${esc(body)}</div><div class="cs">${esc(m.date)}${m.rest ? ` · ${esc(m.rest)}` : ""}</div></div>`; }).join("")}</div></div>` : root ? "" : `<div class="sec"><div class="sh">Files</div><div class="empty">No related materials.</div></div>`;
  if (mapMode) {
    const business = (state.bizs || []).find(b => b.slug === state.biz);
    const graph = ctxGraphData(d, node, root, business && business.name || state.biz || "Context");
    v.innerHTML = head + `<div class="sec"><div id="topicmap"></div></div>`;
    _ctxGraph = contextGraph($("#topicmap"), graph, { storageKey: `ctxgraph:v2:${state.biz}:${p || ""}`,
      onOpen:n=>contextOpenItem(state.biz,n.item),
      onSelect:(n,host)=>contextReviewPanel(host,state.biz,{type:n.item.type,id:n.item.id}) });
  } else {
    v.innerHTML = head + sum + pill + subs + nts;
    if(!root){
      const related=document.createElement('div');related.className='story-associations';v.appendChild(related);
      contextRelatedChips(related,state.biz,{type:'topic',id:node.document_id}).catch(e=>toast(e.message));
    }
  }
  wireCrumbs(v);
  v.querySelectorAll(".card").forEach(el => el.onclick = () => ctxGo(el.dataset.p));
  const mp = $("#tpmap"); if (mp) mp.onclick = () => { ls("ctxmapmode", mapMode ? "0" : "1"); openTopic(p); };
  const ed = $("#tpedit"); if (ed) ed.onclick = () => ctxGo(`${p}/_topic.md`);
  const rs = $("#tpsum"); if (rs) rs.onclick = async () => { rs.disabled = true; const r = await post(`/biz/${state.biz}/context/summarize`, { topic: p }); toast(r.error || `summarising ${p}`); const d2 = await api(`/biz/${state.biz}/context`); ctxPaint(d2); openTopic(p); ctxWatch(d2); };
}
const ctxNoteMeta = n => ({ date: n.slice(0, 10), rest: "" });

// a person: bio, generated summary, the notes filed under them, where they're mentioned
async function openPerson(slug) {
  const v = $("#ctxview"), d = _ctxData || {};
  let person = findPerson(slug);
  if (!person) { v.innerHTML = `<div class="empty">no such person</div>`; return; }
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
    : `<div class="sec"><div class="sh">Summary</div><div class="empty">${busy ? "being written now" : "not written yet; it comes after the next note or save, or press Refresh summary"}</div></div>`;
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
    const d2 = await api(`/biz/${state.biz}/context`); ctxPaint(d2); openPerson(slug); ctxWatch(d2);
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

// everyone: the People cards that used to sit on the root view, now behind the sidebar's People row
function openPeople() {
  const v = $("#ctxview"), d = _ctxData || {}, people = d.people || [];
  _ctxLastSig = ctxSig(d, "_people");
  const head = `<div class="crumbs"><a data-p="">context</a><span class="sep">›</span><span class="cur">People</span></div><div class="th"><h2>People</h2></div>`;
  const body = people.length ? `<div class="sec"><div class="sh">People</div><div class="cards">${people.map(pn => { const n = personNoteCount(pn), path = "_people/" + pn.slug; return `<div class="card p" data-p="${esc(path)}"><div class="ct"><span class="tt">${esc(pn.name)}</span></div><div class="cb">${esc([pn.role, pn.org].filter(Boolean).join(" · "))}</div><div class="cs">${n} note${n === 1 ? "" : "s"}</div></div>`; }).join("")}</div></div>`
    : `<div class="empty">no people yet</div>`;
  v.innerHTML = head + body;
  wireCrumbs(v);
  v.querySelectorAll(".card").forEach(el => el.onclick = () => ctxGo(el.dataset.p));
}

// uploads: what's still filing (or failed) up top, then every filed dump — the tree's old Inbox/Uploads sections
function openUploads() {
  const v = $("#ctxview"), d = _ctxData || {}, inbox = d.inbox || [], dumps = d.dumps || [];
  _ctxLastSig = ctxSig(d, "_uploads");
  const head = `<div class="crumbs"><a data-p="">context</a><span class="sep">›</span><span class="cur">Uploads</span></div><div class="th"><h2>Uploads</h2></div>`;
  const filing = inbox.length ? `<div class="sec"><div class="sh">Filing</div><div class="cards">${inbox.map(i => {
    const cur = d.filing && d.filing.current && d.filing.current.id === i.id;
    const st = cur ? `<span class="run">filing</span>` : i.failed ? `<span class="bad">failed</span>` : esc(({queued: "queued", running: "filing", waiting_signoff: "awaiting approval", cancelled: "cancelled", cancelling: "cancelling"})[i.status] || "status unavailable");
    return `<div class="card u ${i.failed ? "failed" : ""}" data-p="_inbox/${esc(i.id)}.md"><div class="ct"><span class="tt">${esc(i.notes || i.topic || i.preview || i.id)}</span>${i.retryable ? `<button class="retry" data-id="${esc(i.id)}" title="queue it again">retry</button>` : ""}</div><div class="cb">${st}</div><div class="cs">${esc(i.id)} · ${i.chars} chars${i.error ? ` · ${esc(String(i.error).slice(0, 80))}` : ""}</div></div>`;
  }).join("")}</div></div>` : "";
  const up = dumps.length ? `<div class="sec"><div class="sh">Uploads</div><div class="cards">${dumps.slice(0, 60).map(u => `<div class="card u d" data-p="_dumps/${esc(u.id)}.md"><div class="ct"><span class="tt">${esc(u.notes || u.topic || u.preview || u.id)}</span>${u.map ? `<span class="cnt" title="has a mind map">map</span>` : ""}</div><div class="cb">${esc(u.id)}</div><div class="cs">${u.chars} chars</div></div>`).join("")}</div></div>${dumps.length > 60 ? `<div class="empty pad">${dumps.length - 60} older uploads; search finds them</div>` : ""}`
    : `<div class="empty">nothing uploaded yet</div>`;
  v.innerHTML = head + filing + up;
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
  const um = uid ? await api(`/biz/${state.biz}/context/map?id=${uid}`) : null;   // its mind map and where it went
  if (state.ctx !== p) return;                                                     // navigated away meanwhile
  const title = meta.title || (uid ? (meta.notes || meta.topic || `Upload ${uid}`) : p.endsWith("/_topic.md") ? "description" : p.split("/").pop());
  v.innerHTML = `${ctxCrumbs(dir, p.split("/").pop())}
    <div class="jcol"><div class="jhead"><h2 class="jt">${esc(title)}</h2>${ro ? `<span class="ro">${p === "INDEX.md" ? "generated" : "filed upload, immutable"}</span>` : `<span class="jsave" id="jsave"></span><button class="jbtn" id="jsavebtn" type="button">Save</button><button class="jbtn" id="jdiscard" type="button">Discard</button>`}</div>
    ${chips ? `<div class="chips">${chips}</div>` : ""}<hr class="jhr">
    <div id="ctx-stories" class="small" aria-label="Related stories"></div>
    ${uid ? uploadSections(uid, um || {}) : ""}
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

// an upload's two maps: the model-drawn mind map of its text, and where it went (notes, topics, people)
function uploadSections(uid, um) {
  const m = um.map, t = um.trace || {}, filed = t.where === "_dumps", notes = t.notes || [];
  _ctxMapAt = m ? `${m.at}|${m.nodes}` : ""; _ctxMapping = !!um.mapping;
  const stamp = m ? `<span class="stamp">${m.nodes} nodes · ${esc(String(m.at || "").slice(0, 10))}${m.model ? ` · ${esc(m.model)}` : ""}</span>` : "";
  const btn = um.mapping ? `<button class="ghost sm" disabled>drawing…</button>` : `<button class="ghost sm" id="dumpdraw">${m ? "Redraw" : "Draw mind map"}</button>`;
  const mapBody = m ? `<div id="dumpmap"></div>` : `<div class="empty">${um.mapping ? "being drawn now from the text below" : um.error ? esc(um.error) : "not drawn yet; it is drawn after filing, or press Draw mind map"}</div>`;
  const cards = notes.length ? `<div class="cards">${notes.map(n => `<div class="card n" data-p="${esc(n.path)}" data-tree="${esc(n.tree || "context")}" title="${esc(n.title)}"><div class="ct">${SVG_FILE}<span class="tt">${esc(cap(n.title || n.path))}</span></div>
      <div class="cb">${esc(n.summary || "")}</div><div class="cs">${esc(n.topic)}${(n.people || []).length ? ` · ${esc(n.people.join(", "))}` : ""}</div></div>`).join("")}</div>`
    : `<div class="empty">${filed ? `no notes were cut from it${t.result ? `: ${esc(t.result)}` : ""}` : "not filed yet; the filer splits it into notes when the queue runs"}</div>`;
  const chip = (path, label, extra = "") => `<span class="chip${path.startsWith("marketing-content/") ? "" : " go"}" data-p="${esc(path)}" ${extra}>${esc(label)}</span>`;
  const topics = (t.topics || []).length || (t.also || []).length ? `<div class="row"><span class="lbl">topics</span>${(t.topics || []).map(x => chip(x, x)).join("")}${(t.also || []).map(x => chip(x, `${x} (also)`, 'title="cross-referenced, not filed into"')).join("")}</div>` : "";
  const people = (t.people || []).length ? `<div class="row"><span class="lbl">people</span>${t.people.map(pp => chip(`_people/${pp.slug}`, pp.name)).join("")}</div>` : "";
  return `<div class="sec"><div class="sh">Mind map ${stamp}<span class="ml">${btn}</span></div>${mapBody}</div>
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
    const d = await api(`/biz/${state.biz}/context`); ctxPaint(d); ctxWatch(d);
    if (state.ctx && uploadId(state.ctx) === uid) openCtx(state.ctx);
  };
}

// The upload box: text (required), notes (attribution), topic (a hint) → POST context/upload, then the new
// upload opens in the Context tab; the filer can choose Context or marketing reference topics.
function uploadModal({stories=[],onUploaded}={}) {
  const biz=state.biz;let selectedStories=[...stories],request;
  const msg = "the filer splits it by topic and files it; the raw text is kept";
  const body = `<div class="fl">Context</div><textarea class="inp ta" id="uptext" placeholder="paste anything: notes from a call, a doc, a thread, a decision" spellcheck="false"></textarea>
    <div class="fmsg" id="upmsg">${esc(msg)}</div>
    <div class="fl">Notes <span class="opt">optional</span></div><input class="inp" id="upnotes" placeholder="where it came from: call with Sarah, PH hunter" autocomplete="off">
    <div class="fl">Topic <span class="opt">optional</span></div><input class="inp" id="uptopic" placeholder="hint: producthunt launch, pricing, brand voice" autocomplete="off">
    <div class="fl">Stories <span class="opt">optional</span></div><button class="quiet" type="button" id="ctxuploadstories">${selectedStories.length?selectedStories.map(story=>esc(story.title)).join(', '):'Choose stories'}</button>
    <div class="ferr" id="uperr" hidden></div>`;
  modal({
    title: "Upload context", body, width: 560,
    onOpen: el => {
      const ta = el.querySelector("#uptext"), m = el.querySelector("#upmsg");
      el.querySelector('#ctxuploadstories').onclick=()=>storyChooseIds(biz,selectedStories,stories=>{selectedStories=stories;el.querySelector('#ctxuploadstories').textContent=stories.length?stories.map(story=>story.title).join(', '):'Choose stories';});
      ta.oninput = () => { const n = ta.value.trim().length; m.textContent = n ? `${n.toLocaleString()} chars` : msg; };
      ta.onkeydown = e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") el.querySelector(".mfoot .primary").click(); };
    },
    actions: [
      { label: "Cancel", onClick: close => close() },
      { label: "Upload", icon: ICO_UPLOAD, primary: true, onClick: async (close, el) => {
        const err = el.querySelector("#uperr"), text = el.querySelector("#uptext").value.trim();
        const fail = t => { err.hidden = false; err.textContent = t; };
        if(state.biz!==biz)return fail('The selected business changed. Reopen the upload form.');
        if (!text) return fail("nothing to upload");
        err.hidden = true;
        const b = el.querySelector(".mfoot .primary"); b.disabled = true;
        const payload = { text, notes: el.querySelector("#upnotes").value, topic: el.querySelector("#uptopic").value, story_ids:selectedStories.map(story=>story.id) };
        const signature=JSON.stringify(payload);
        if(request?.signature!==signature)request={signature,key:newRequestId()};
        let r; try { r = await post(`/biz/${biz}/context/upload`, {...payload,idempotency_key:request.key}); }
        catch (e) { r = { error: "could not reach the server" }; }
        if (!r || r.error) { b.disabled = false; return fail((r && r.error) || "upload failed"); }
        close(); toast(r.warn ? `uploaded ${r.id}, but: ${r.warn}` : `uploaded ${r.id}; filing queued`);
        if(onUploaded)await onUploaded(r);
        else {state.ctx = `_inbox/${r.id}.md`;renderContext($("#content"));}
        if (typeof refreshSide === "function") refreshSide();
      } },
    ],
  });
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

// All business tasks, independent of the personal drawer's assignment filter.
async function renderAllTasks(container) {
  const biz = state.biz;
  container.innerHTML = `<section class="ctx-all-tasks"><header class="data-heading"><h1>All tasks</h1><button class="btn" id="all-tasks-refresh">Refresh</button></header>
    <div class="data-row-tools"><label>Search tasks<input class="inp" type="search" id="all-tasks-search"></label>
    <label>Status<select class="inp" id="all-tasks-status"><option value="">All statuses</option>${T_STATES.map(s => `<option value="${s}">${T_LABEL[s]}</option>`).join('')}</select></label></div>
    <p id="all-tasks-message" role="status">Loading tasks…</p><div class="data-grid-wrap"><table class="data-grid"><thead><tr><th>Task</th><th>Status</th><th>Assignees</th><th>Due</th><th>Tags</th></tr></thead><tbody></tbody></table></div></section>`;
  let rows = [], request = 0;
  const current = () => container.isConnected && state.biz === biz && state.tab === 'context' && state.ctx === '_tasks';
  const message = container.querySelector('#all-tasks-message');
  const search = container.querySelector('#all-tasks-search'), status = container.querySelector('#all-tasks-status');
  const paint = () => {
    const query = search.value.trim().toLowerCase();
    const visible = rows.filter(t => (!status.value || t.status === status.value) &&
      [t.title, ...(t.owners || []), ...(t.tags || [])].join(' ').toLowerCase().includes(query));
    message.textContent = `${visible.length} ${visible.length === 1 ? 'task' : 'tasks'}`;
    const body = container.querySelector('tbody');
    body.innerHTML = visible.map(t => `<tr><td><button class="quiet" data-task="${esc(t.id)}">${esc(t.title)}</button></td><td>${esc(T_LABEL[t.status] || t.status)}</td><td>${esc((t.owners || []).map(tShort).join(', ') || 'Unassigned')}</td><td>${esc(t.due || '—')}</td><td>${esc((t.tags || []).join(', '))}</td></tr>`).join('');
    body.querySelectorAll('[data-task]').forEach(button => button.onclick = async () => {
      try { const task = await tFetchTask(biz, button.dataset.task); if (current()) tDetail(task); }
      catch (error) { if (current()) message.textContent = error.message; }
    });
  };
  const refresh = async () => {
    const sequence = ++request;
    try {
      const result = await api(`/biz/${encodeURIComponent(biz)}/tasks?all=1`);
      if (!current() || sequence !== request) return;
      if (!Array.isArray(result)) throw new Error('Could not load tasks. Use Refresh to retry.');
      rows = result.sort((a,b) => Number(tClosed(a)) - Number(tClosed(b)) || String(a.title).localeCompare(String(b.title)));
      paint();
    } catch (error) { if (current() && sequence === request) message.textContent = error.message; }
  };
  search.oninput = paint; status.onchange = paint;
  container.querySelector('#all-tasks-refresh').onclick = refresh;
  await refresh();
}
