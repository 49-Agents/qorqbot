// ---------------------------------------------------------------- context
// The Context tab: upload text (goes to context/_inbox, filed by the queue), browse the topic tree
// like folders, read and edit any file. Every save is one commit. Summaries are rewritten by the
// server's summariser after a filing or a save, deepest topic first, up to the root.
let _ctxTimer = null, _ctxOpen = null, _ctxData = null;
const ctxOpen = () => { if (!_ctxOpen) { try { _ctxOpen = new Set(JSON.parse(ls("ctxopen") || "[]")); } catch (e) { _ctxOpen = new Set(); } } return _ctxOpen; };
const ctxSaveOpen = () => ls("ctxopen", JSON.stringify([...ctxOpen()]));
const SVG_CARET = `<svg class="car" width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 2l3 3-3 3"/></svg>`;
const SVG_FOLDER = `<svg class="fo" width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"><path d="M1.5 3.5h4l1.5 1.5h5.5v6h-11z"/></svg>`;
const SVG_FILE = `<svg class="fi" width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"><path d="M3 1.5h5l3 3v8h-8z"/><path d="M8 1.5v3h3"/></svg>`;
const fmtAgo = t => { const s = Math.max(0, Math.round(Date.now() / 1000 - t)); return s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`; };
const isTopic = p => p != null && !p.endsWith(".md");           // state.ctx is a topic path ("" = root) or a file path
const ctxCount = n => (n.notes || []).length + (n.children || []).reduce((s, k) => s + ctxCount(k), 0);
const ctxFind = (node, path) => { if (!node) return null; if ((node.path || "") === path) return node; for (const c of node.children || []) { const h = ctxFind(c, path); if (h) return h; } return null; };
const PERSON_VIEW_RE = /^_people\/([a-z][a-z0-9-]{1,63})$/;     // "_people/<slug>" (no .md) is the person view, not a raw file
const uploadId = p => { const m = /^_(?:inbox|dumps)\/(\d{4}-\d{2}-\d{2}-\d{4}-[0-9a-z]{4})\.md$/.exec(p || ""); return m ? m[1] : ""; };
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

async function renderContext(c) {
  clearTimeout(_ctxTimer); _ctxTimer = null;
  const d = await api(`/biz/${state.biz}/context`);
  c.innerHTML = `<div class="ctxwrap">
    <div class="ctxbar"><button class="primary" id="ctxup">Upload context</button><input type="search" id="ctxsearch" placeholder="Search context" autocomplete="off"><div class="ctxq" id="ctxq"></div></div>
    <div class="split ctxsplit"><div class="listcol ctxtree" id="ctxtree"></div><div class="pane ctxview" id="ctxview"></div></div></div>`;
  $("#ctxup").onclick = uploadModal;
  wireCtxSearch();
  if (state.ctx == null) state.ctx = "";
  ctxPaint(d);
  ctxShow(state.ctx);
  ctxWatch(d);
}

// ------------------------------------------------------------- search
let _ctxSearchTimer = null;
function wireCtxSearch() {
  const inp = $("#ctxsearch");
  if (!inp) return;
  inp.oninput = () => {
    clearTimeout(_ctxSearchTimer);
    const q = inp.value.trim();
    if (q.length < 2) { ctxShow(state.ctx); return; }
    _ctxSearchTimer = setTimeout(() => ctxSearch(q), 250);
  };
  inp.onkeydown = e => {
    if (e.key !== "Escape") return;
    clearTimeout(_ctxSearchTimer);
    inp.value = "";
    ctxShow(state.ctx);
  };
}

function ctxMark(snippet, q) {
  const s = snippet || "";
  const i = s.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return esc(s);
  return `${esc(s.slice(0, i))}<mark>${esc(s.slice(i, i + q.length))}</mark>${esc(s.slice(i + q.length))}`;
}

async function ctxSearch(q) {
  const v = $("#ctxview");
  if (!v) return;
  const r = await api(`/biz/${state.biz}/context/search?q=${encodeURIComponent(q)}`);
  const inp = $("#ctxsearch");
  if (!inp || inp.value.trim() !== q) return;   // a newer query is already in flight
  const hits = r.hits || [];
  const rows = hits.map(h => `<div class="card" data-p="${esc(h.path)}" data-k="${esc(h.kind)}" title="${esc(h.title)}"><div class="ct"><span class="kd">${esc(h.kind)}</span><span class="tt">${esc(cap(h.title))}</span></div>
    <div class="cb">${ctxMark(h.snippet, q)}</div><div class="cs">${esc(h.path)}</div></div>`).join("");
  v.innerHTML = `<div class="sec"><div class="sh">Search: ${esc(q)} <span class="stamp">${hits.length} hit${hits.length === 1 ? "" : "s"}</span></div>
    <div class="cards">${rows || `<div class="empty">no matches</div>`}</div></div>`;
  v.querySelectorAll(".card").forEach(el => el.onclick = () => {
    const p = el.dataset.p;
    if (el.dataset.k === "person") return ctxGo(p.replace(/\.md$/, ""));
    if (el.dataset.k === "map") {                          // a map hit opens its upload
      const id = p.replace(/^_maps\//, "").replace(/\.md$/, "");
      return ctxGo(((_ctxData || {}).dumps || []).some(u => u.id === id) ? `_dumps/${id}.md` : `_inbox/${id}.md`);
    }
    if (p.endsWith("/_topic.md")) {
      const dir = p.split("/").slice(0, -1).join("/");
      ctxOpen().add(dir); ctxSaveOpen();
      return ctxGo(dir);
    }
    ctxGo(p);
  });
}

function ctxBusy(d) {
  const s = d.summarizing || {};
  return !!((d.filing && d.filing.current) || (d.inbox || []).some(i => !i.failed) || s.current || s.queued);
}
// Poll the tree while the tab is open: every 8 s while the queues are busy, every 20 s when idle (work
// arrives from the filer sweep and from other clients too). Re-render only what changed.
const ctxSig = (d, p) => JSON.stringify([ctxFind((d || {}).tree, p || ""), p ? null : (d || {}).people]);
let _ctxLastSig = "";
function ctxWatch(d) {
  clearTimeout(_ctxTimer); _ctxTimer = null;
  const tick = async () => {
    if (state.tab !== "context" || !$("#ctxtree")) { _ctxTimer = null; return; }
    const d2 = await api(`/biz/${state.biz}/context`);
    if (state.tab !== "context" || !$("#ctxtree")) { _ctxTimer = null; return; }
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
  const failed = inbox.filter(i => i.failed).length, waiting = inbox.length - failed;
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

function ctxTree(d) {
  const t = $("#ctxtree"), open = ctxOpen(), rows = [];
  const row = (cls, p, ind, html, sub) => rows.push(`<div class="item ${cls} ${p === state.ctx ? "on" : ""}" data-p="${esc(p)}" style="--ind:${ind * 14}px"><div class="nm">${html}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`);
  row("r", "", 0, `${SVG_FOLDER}<span class="tt">context</span><span class="cnt">${ctxCount(d.tree || {})}</span>`);
  rows.push(`<div class="h">Pillars</div>`);
  (d.pillars || []).forEach(n => row("f", n, 0, esc(n)));
  row("f idx", "INDEX.md", 0, `INDEX.md <em>generated</em>`);
  rows.push(`<div class="h">Topics</div>`);
  const walk = (node, depth) => {
    const notes = node.notes || [], kids = node.children || [], isOpen = open.has(node.path);
    row(`t ${isOpen ? "open" : ""}`, node.path, depth, `${SVG_CARET}<span class="tt">${esc(node.title || node.slug)}</span><span class="cnt">${ctxCount(node)}</span>`, esc(node.slug));
    if (!isOpen) return;
    notes.forEach(n => row("n", `${node.path}/${n}`, depth + 1, esc(cap((node.titles || {})[n] || n.replace(/^\d{4}-\d{2}-\d{2}-/, "").replace(/\.md$/, "").replace(/-/g, " "))), esc(n.slice(0, 10))));
    kids.forEach(k => walk(k, depth + 1));
  };
  const kids = (d.tree && d.tree.children) || [];
  if (!kids.length) rows.push(`<div class="empty pad">No topics yet. Upload something; the filer makes them.</div>`);
  kids.forEach(k => walk(k, 0));
  const people = d.people || [];
  if (people.length) {
    rows.push(`<div class="h">People</div>`);
    people.forEach(p => row("p", `_people/${p.slug}`, 0, `<span class="tt">${esc(p.name)}</span><span class="cnt">${personNoteCount(p)}</span>`, esc([p.role, p.org].filter(Boolean).join(" · "))));
  }
  const dumps = d.dumps || [];
  if (dumps.length) {
    rows.push(`<div class="h">Uploads</div>`);
    dumps.slice(0, 40).forEach(u => row("u d", `_dumps/${u.id}.md`, 0,
      `<span class="tt">${esc(u.notes || u.topic || u.preview || u.id)}</span>${u.map ? `<span class="cnt" title="has a mind map">map</span>` : ""}`,
      `${esc(u.id)} · ${u.chars} chars`));
    if (dumps.length > 40) rows.push(`<div class="empty pad">${dumps.length - 40} older uploads; search finds them</div>`);
  }
  const inbox = d.inbox || [];
  if (inbox.length) {
    rows.push(`<div class="h">Inbox</div>`);
    inbox.forEach(i => {
      const cur = d.filing && d.filing.current && d.filing.current.id === i.id;
      const st = cur ? `<span class="run">filing</span>` : i.failed ? `<span class="bad">failed</span>` : i.attempts ? `retry ${i.attempts}` : "waiting";
      row(`u ${i.failed ? "failed" : ""}`, `_inbox/${i.id}.md`, 0, `<span class="tt">${esc(i.notes || i.topic || i.preview || i.id)}</span>${i.failed ? `<button class="retry" data-id="${esc(i.id)}" title="queue it again">retry</button>` : ""}`,
          `${esc(i.id)} · ${i.chars} chars · ${st}${i.error ? ` · ${esc(String(i.error).slice(0, 80))}` : ""}`);
    });
  }
  t.innerHTML = rows.join("");
  t.querySelectorAll(".item").forEach(el => el.onclick = e => {
    const p = el.dataset.p;
    if (e.target.closest(".retry")) return ctxRetry(e.target.closest(".retry").dataset.id);
    if (el.classList.contains("t")) {
      if (e.target.closest(".car") || p === state.ctx) { open.has(p) ? open.delete(p) : open.add(p); ctxSaveOpen(); }
      else open.add(p), ctxSaveOpen();
    }
    ctxGo(p);
  });
}

function ctxGo(p) { state.ctx = p; if (_ctxData) ctxTree(_ctxData); ctxShow(p); }

function ctxShow(p) {
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
function wireCrumbs(v) { v.querySelectorAll(".crumbs a").forEach(a => a.onclick = () => { if (a.dataset.p) { ctxOpen().add(a.dataset.p); ctxSaveOpen(); } ctxGo(a.dataset.p); }); }

const mdLines = text => `<div class="sum">${text.split("\n").map(l => l.trim()).filter(Boolean).map(l => l.startsWith("- ") ? `<div class="li">${esc(l.slice(2))}</div>` : `<p>${esc(l)}</p>`).join("")}</div>`;

// a topic as a folder: description, generated summary, subtopics, notes
function openTopic(p) {
  const v = $("#ctxview"), d = _ctxData || {}, root = !p;
  const node = root ? Object.assign({ title: "context", about: "", children: [], notes: [] }, d.tree || {}) : ctxFind(d.tree, p);
  if (!node) { v.innerHTML = `<div class="empty">no such topic</div>`; return; }
  _ctxLastSig = ctxSig(d, p);
  const s = d.summarizing || {}, busy = s.current && (s.current.topic === p || (s.current.topic || "").startsWith(p + "/"));
  const kids = node.children || [], notes = node.notes || [];
  const mappable = kids.length || notes.length || (root && (d.people || []).length);
  const mapMode = !!mappable && ls("ctxmapmode") === "1";        // the folder as a mind map instead of cards; remembered
  const head = `${ctxCrumbs(root ? "" : p.split("/").slice(0, -1).join("/"), root ? "" : node.slug)}
    <div class="th"><h2>${esc(root ? "All context" : node.title)}</h2>
      <div class="pa">${mappable ? `<button class="ghost sm" id="tpmap" title="${mapMode ? "back to the cards" : "this folder as a mind map"}">${mapMode ? "Cards" : "Map"}</button>` : ""}${root ? "" : `<button class="ghost sm" id="tpedit" title="edit _topic.md">Edit description</button><button class="ghost sm" id="tpsum" ${busy ? "disabled" : ""}>${busy ? "summarising…" : "Refresh summary"}</button>`}</div></div>
    ${node.about ? `<p class="about">${esc(node.about)}</p>` : ""}`;
  const sum = root ? "" : node.summary ? `<div class="sec"><div class="sh">Summary <span class="stamp">${esc((node.summarized || "").replace("T", " ").replace(/:\d\dZ/, "Z"))}</span></div>${mdLines(node.summary)}</div>`
    : `<div class="sec"><div class="sh">Summary</div><div class="empty">${busy ? "being written now" : notes.length || kids.length ? "not written yet; it comes after the next filing or save, or press Refresh summary" : "empty topic"}</div></div>`;
  const pill = root ? `<div class="sec"><div class="sh">Pillars</div><div class="cards">${(d.pillars || []).map(n => `<div class="card f" data-p="${esc(n)}"><div class="ct"><span class="tt">${esc(n.replace(/\.md$/, ""))}</span></div><div class="cb">owner-written</div></div>`).join("")}</div></div>` : "";
  const subs = kids.length ? `<div class="sec"><div class="sh">${root ? "Topics" : "Subtopics"}</div><div class="cards">${kids.map(k => { const kt = k.title || k.slug; return `<div class="card t" data-p="${esc(k.path)}" title="${esc(kt)}"><div class="ct">${SVG_FOLDER}<span class="tt">${esc(cap(kt))}</span><span class="cnt">${ctxCount(k)}</span></div><div class="cb">${esc(k.about || "")}</div><div class="cs">${esc(k.slug)}${k.summary ? "" : " · no summary yet"}</div></div>`; }).join("")}</div></div>` : "";
  const people = root ? (d.people || []) : [];
  const ppl = people.length ? `<div class="sec"><div class="sh">People</div><div class="cards">${people.map(pn => { const n = personNoteCount(pn), path = "_people/" + pn.slug; return `<div class="card p" data-p="${esc(path)}"><div class="ct"><span class="tt">${esc(pn.name)}</span></div><div class="cb">${esc([pn.role, pn.org].filter(Boolean).join(" · "))}</div><div class="cs">${n} note${n === 1 ? "" : "s"}</div></div>`; }).join("")}</div></div>` : "";
  const nts = notes.length ? `<div class="sec"><div class="sh">Files</div><div class="cards">${notes.map(n => { const m = ctxNoteMeta(n), ft = (node.titles || {})[n] || n, body = node.summaries?.[n] || node.previews?.[n] || ""; return `<div class="card n" data-p="${esc(p)}/${esc(n)}" title="${esc(ft)}"><div class="ct">${SVG_FILE}<span class="tt">${esc(cap(ft))}</span></div><div class="cb">${esc(body)}</div><div class="cs">${esc(m.date)}${m.rest ? ` · ${esc(m.rest)}` : ""}</div></div>`; }).join("")}</div></div>` : root ? "" : `<div class="sec"><div class="sh">Files</div><div class="empty">none here; ${kids.length ? "they sit in the subtopics" : "nothing filed yet"}</div></div>`;
  if (mapMode) {
    const tree = ctxMapTree(d, node, root);
    v.innerHTML = head + `<div class="sec"><div class="sh">Map <span class="stamp">${mapCount(tree) - 1} nodes</span></div><div id="topicmap"></div></div>`;
    mindmap($("#topicmap"), tree, { links: true, legend: MAP_LEGEND,
      onOpen: n => { if (n.kind === "topic") { ctxOpen().add(n.path); ctxSaveOpen(); } ctxGo(n.path); } });
  } else {
    v.innerHTML = head + sum + pill + subs + ppl + nts;
  }
  wireCrumbs(v);
  v.querySelectorAll(".card").forEach(el => el.onclick = () => { if (el.classList.contains("t")) { ctxOpen().add(el.dataset.p); ctxSaveOpen(); } ctxGo(el.dataset.p); });
  const mp = $("#tpmap"); if (mp) mp.onclick = () => { ls("ctxmapmode", mapMode ? "0" : "1"); openTopic(p); };
  const ed = $("#tpedit"); if (ed) ed.onclick = () => ctxGo(`${p}/_topic.md`);
  const rs = $("#tpsum"); if (rs) rs.onclick = async () => { rs.disabled = true; const r = await post(`/biz/${state.biz}/context/summarize`, { topic: p }); toast(r.error || `summarising ${p}`); const d2 = await api(`/biz/${state.biz}/context`); ctxPaint(d2); openTopic(p); ctxWatch(d2); };
}
const ctxNoteMeta = n => ({ date: n.slice(0, 10), rest: "" });

// a person: bio, generated summary, the notes filed under them, where they're mentioned
async function openPerson(slug) {
  const v = $("#ctxview"), d = _ctxData || {};
  const person = findPerson(slug);
  if (!person) { v.innerHTML = `<div class="empty">no such person</div>`; return; }
  const s = d.summarizing || {}, busy = s.current && s.current.topic === `person:${slug}`;
  const r = await api(`/biz/${state.biz}/context/person/${slug}`);
  if (state.ctx !== `_people/${slug}`) return;    // navigated away while the notes were loading
  const notesText = (r && r.notes_text) || "";
  const head = `<div class="crumbs"><a data-p="">context</a><span class="sep">›</span><a data-p="">People</a><span class="sep">›</span><span class="cur">${esc(person.name)}</span></div>
    <div class="th"><h2>${esc(person.name)}</h2>
      <div class="pa"><button class="ghost sm" id="ctxpedit">Edit</button><button class="ghost sm" id="ctxpsum" ${busy ? "disabled" : ""}>${busy ? "summarising…" : "Refresh summary"}</button></div></div>`;
  const metaLine = [person.role, person.org, person.email].filter(Boolean).join(" · ");
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
  v.innerHTML = head + meta + aka + about + sum + notesSec + ment;
  wireCrumbs(v);
  v.querySelectorAll(".card").forEach(el => el.onclick = () => ctxGo(el.dataset.p));
  const ed = $("#ctxpedit"); if (ed) ed.onclick = () => ctxGo(`_people/${slug}.md`);
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

async function ctxRetry(id) {
  const r = await post(`/biz/${state.biz}/context/retry`, { id });
  toast(r.error ? r.error : `${id} queued again`);
  renderContext($("#content"));
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

// a file: pillar, note, _topic.md, INDEX.md, upload
async function openCtx(p) {
  const v = $("#ctxview");
  const r = await api(`/biz/${state.biz}/context/file?p=${encodeURIComponent(p)}`);
  if (r.error) { v.innerHTML = `<div class="empty">${esc(r.error)}</div>`; return; }
  const ro = p === "INDEX.md" || p.startsWith("_dumps/");
  const { meta, body } = ctxSplit(r.text);
  const dir = p.includes("/") && !p.startsWith("_") ? p.split("/").slice(0, -1).join("/") : "";
  const chips = CTX_META.filter(k => meta[k]).map(k => `<span class="chip ${k === "error" ? "bad" : ""}"><b>${esc(k)}</b>${esc(meta[k])}</span>`).join("");
  const uid = uploadId(p);
  const um = uid ? await api(`/biz/${state.biz}/context/map?id=${uid}`) : null;   // its mind map and where it went
  if (state.ctx !== p) return;                                                     // navigated away meanwhile
  const title = meta.title || (uid ? (meta.notes || meta.topic || `Upload ${uid}`) : p.endsWith("/_topic.md") ? "description" : p.split("/").pop());
  const text = uid ? `<div class="sec"><div class="sh">Text <span class="stamp">${body.trim().length.toLocaleString()} chars</span></div><pre class="ctxbody">${esc(body)}</pre></div>` : `<pre class="ctxbody">${esc(body)}</pre>`;
  v.innerHTML = `${ctxCrumbs(dir, p.split("/").pop())}<div class="ph"><h2>${esc(title)}</h2><div class="pa">${ro ? `<span class="ro">${p === "INDEX.md" ? "generated" : "filed upload, immutable"}</span>` : `<button class="ghost sm" id="ctxedit">Edit</button>`}</div></div>
    ${chips ? `<div class="chips">${chips}</div>` : ""}${uid ? uploadSections(uid, um || {}) : ""}${text}`;
  wireCrumbs(v);
  if (uid) wireUpload(v, uid, um || {});
  if (ro) return;
  $("#ctxedit").onclick = () => {
    v.innerHTML = `${ctxCrumbs(dir, p.split("/").pop())}<div class="ph"><h2>${esc(title)}</h2><div class="pa"><span class="ro">editing</span></div></div>
      <div class="ctxedit"><textarea id="ctxta" spellcheck="false"></textarea>
      <div class="row"><span class="hint">one commit per save; the topic's summary is rewritten after</span><button class="ghost" id="ctxcancel">Cancel</button><button class="primary" id="ctxsave">Save</button></div></div>`;
    wireCrumbs(v);
    const ta = $("#ctxta"); ta.value = r.text; ta.focus();
    $("#ctxcancel").onclick = () => openCtx(p);
    $("#ctxsave").onclick = async () => {
      if (ta.value === r.text) return openCtx(p);
      $("#ctxsave").disabled = true;
      const s = await post(`/biz/${state.biz}/context/save`, { path: p, text: ta.value });
      if (s.error) { toast(s.error); $("#ctxsave").disabled = false; return; }
      toast(s.warn ? `saved, but: ${s.warn}` : `saved ${p}`);
      const d = await api(`/biz/${state.biz}/context`); ctxPaint(d); ctxWatch(d);
      openCtx(p);
    };
    ta.onkeydown = e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") $("#ctxsave").click(); };
  };
}

// an upload's two maps: the model-drawn mind map of its text, and where it went (notes, topics, people)
function uploadSections(uid, um) {
  const m = um.map, t = um.trace || {}, filed = t.where === "_dumps", notes = t.notes || [];
  _ctxMapAt = m ? `${m.at}|${m.nodes}` : ""; _ctxMapping = !!um.mapping;
  const stamp = m ? `<span class="stamp">${m.nodes} nodes · ${esc(String(m.at || "").slice(0, 10))}${m.model ? ` · ${esc(m.model)}` : ""}</span>` : "";
  const btn = um.mapping ? `<button class="ghost sm" disabled>drawing…</button>` : `<button class="ghost sm" id="dumpdraw">${m ? "Redraw" : "Draw mind map"}</button>`;
  const mapBody = m ? `<div id="dumpmap"></div>` : `<div class="empty">${um.mapping ? "being drawn now from the text below" : um.error ? esc(um.error) : "not drawn yet; it is drawn after filing, or press Draw mind map"}</div>`;
  const cards = notes.length ? `<div class="cards">${notes.map(n => `<div class="card n" data-p="${esc(n.path)}" title="${esc(n.title)}"><div class="ct">${SVG_FILE}<span class="tt">${esc(cap(n.title || n.path))}</span></div>
      <div class="cb">${esc(n.summary || "")}</div><div class="cs">${esc(n.topic)}${(n.people || []).length ? ` · ${esc(n.people.join(", "))}` : ""}</div></div>`).join("")}</div>`
    : `<div class="empty">${filed ? `no notes were cut from it${t.result ? `: ${esc(t.result)}` : ""}` : "not filed yet; the filer splits it into notes when the queue runs"}</div>`;
  const chip = (path, label, extra = "") => `<span class="chip go" data-p="${esc(path)}" ${extra}>${esc(label)}</span>`;
  const topics = (t.topics || []).length || (t.also || []).length ? `<div class="row"><span class="lbl">topics</span>${(t.topics || []).map(x => chip(x, x)).join("")}${(t.also || []).map(x => chip(x, `${x} (also)`, 'title="cross-referenced, not filed into"')).join("")}</div>` : "";
  const people = (t.people || []).length ? `<div class="row"><span class="lbl">people</span>${t.people.map(pp => chip(`_people/${pp.slug}`, pp.name)).join("")}</div>` : "";
  return `<div class="sec"><div class="sh">Mind map ${stamp}<span class="ml">${btn}</span></div>${mapBody}</div>
    <div class="sec trace"><div class="sh">Where it went</div>${cards}${topics}${people}</div>`;
}
function wireUpload(v, uid, um) {
  if (um.map && um.map.tree) mindmap($("#dumpmap"), ctxMapIdea(um.map.tree), { legend: `<span>${esc(cap(um.map.root || "", 60))}</span>` });
  v.querySelectorAll(".trace .card, .trace .chip.go").forEach(el => el.onclick = () => {
    const p = el.dataset.p;
    if (isTopic(p) && !p.startsWith("_people/")) { ctxOpen().add(p); ctxSaveOpen(); }
    ctxGo(p);
  });
  const b = $("#dumpdraw");
  if (b) b.onclick = async () => {
    b.disabled = true;
    const r = await post(`/biz/${state.biz}/context/map`, { id: uid });
    toast(r.error || `drawing the mind map of ${uid}`);
    const d = await api(`/biz/${state.biz}/context`); ctxPaint(d); ctxWatch(d);
    if (state.ctx && uploadId(state.ctx) === uid) openCtx(state.ctx);
  };
}

function uploadModal() {
  const body = `<div class="fl">Context</div><textarea class="inp ta" id="uptext" placeholder="paste anything: notes from a call, a doc, a thread, a decision" spellcheck="false"></textarea>
    <div class="fmsg" id="upmsg">the filer splits it by topic and files it; the raw text is kept</div>
    <div class="fl">Notes <span class="opt">optional</span></div><input class="inp" id="upnotes" placeholder="where it came from: call with Sarah, PH hunter" autocomplete="off">
    <div class="fl">Topic <span class="opt">optional</span></div><input class="inp" id="uptopic" placeholder="hint: producthunt launch, pricing, brand voice" autocomplete="off">
    <div class="ferr" id="uperr" hidden></div>`;
  modal({
    title: "Upload context", body, width: 560,
    onOpen: el => {
      const ta = el.querySelector("#uptext"), msg = el.querySelector("#upmsg");
      ta.oninput = () => { const n = ta.value.trim().length; msg.textContent = n ? `${n.toLocaleString()} chars` : "the filer splits it by topic and files it; the raw text is kept"; };
      ta.onkeydown = e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") el.querySelector(".mfoot .primary").click(); };
    },
    actions: [
      { label: "Cancel", onClick: close => close() },
      { label: "Upload", primary: true, onClick: async (close, el) => {
        const err = el.querySelector("#uperr"), text = el.querySelector("#uptext").value.trim();
        const fail = t => { err.hidden = false; err.textContent = t; };
        if (!text) return fail("nothing to upload");
        err.hidden = true;
        const b = el.querySelector(".mfoot .primary"); b.disabled = true;
        let r; try { r = await post(`/biz/${state.biz}/context/upload`, { text, notes: el.querySelector("#upnotes").value, topic: el.querySelector("#uptopic").value }); }
        catch (e) { r = { error: "could not reach the server" }; }
        if (!r || r.error) { b.disabled = false; return fail((r && r.error) || "upload failed"); }
        close(); toast(r.warn ? `uploaded ${r.id}, but: ${r.warn}` : `uploaded ${r.id}; filing starts now`);
        state.ctx = `_inbox/${r.id}.md`;
        renderContext($("#content")); if (typeof refreshSide === "function") refreshSide();
      } },
    ],
  });
}
