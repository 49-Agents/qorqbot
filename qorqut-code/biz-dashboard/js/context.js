// ---------------------------------------------------------------- context
// The Context tab: upload text (goes to context/_inbox, filed by the queue), browse the topic tree
// like folders, read and edit any file. Every save is one commit. Summaries are rewritten by the
// server's summariser after a filing or a save, deepest topic first, up to the root.
let _ctxTimer = null, _ctxOpen = null, _ctxData = null;
const ctxOpen = () => { if (!_ctxOpen) { try { _ctxOpen = new Set(JSON.parse(ls("ctxopen") || "[]")); } catch (e) { _ctxOpen = new Set(); } } return _ctxOpen; };
const ctxSaveOpen = () => ls("ctxopen", JSON.stringify([...ctxOpen()]));
const SVG_CARET = `<svg class="car" width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 2l3 3-3 3"/></svg>`;
const SVG_FOLDER = `<svg class="fo" width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"><path d="M1.5 3.5h4l1.5 1.5h5.5v6h-11z"/></svg>`;
const fmtAgo = t => { const s = Math.max(0, Math.round(Date.now() / 1000 - t)); return s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`; };
const isTopic = p => p != null && !p.endsWith(".md");           // state.ctx is a topic path ("" = root) or a file path
const ctxCount = n => (n.notes || []).length + (n.children || []).reduce((s, k) => s + ctxCount(k), 0);
const ctxFind = (node, path) => { if (!node) return null; if ((node.path || "") === path) return node; for (const c of node.children || []) { const h = ctxFind(c, path); if (h) return h; } return null; };

async function renderContext(c) {
  clearInterval(_ctxTimer); _ctxTimer = null;
  const d = await api(`/biz/${state.biz}/context`);
  c.innerHTML = `<div class="ctxwrap">
    <div class="ctxbar"><button class="primary" id="ctxup">Upload context</button><div class="ctxq" id="ctxq"></div></div>
    <div class="split ctxsplit"><div class="listcol ctxtree" id="ctxtree"></div><div class="pane ctxview" id="ctxview"></div></div></div>`;
  $("#ctxup").onclick = uploadModal;
  if (state.ctx == null) state.ctx = "";
  ctxPaint(d);
  ctxShow(state.ctx);
  ctxWatch(d);
}

function ctxBusy(d) {
  const s = d.summarizing || {};
  return !!((d.filing && d.filing.current) || (d.inbox || []).some(i => !i.failed) || s.current || s.queued);
}
function ctxWatch(d) {
  clearInterval(_ctxTimer); _ctxTimer = null;
  if (!ctxBusy(d)) return;
  _ctxTimer = setInterval(async () => {
    if (state.tab !== "context" || !$("#ctxtree")) return clearInterval(_ctxTimer);
    const d2 = await api(`/biz/${state.biz}/context`);
    ctxPaint(d2);
    if (state.ctx && state.ctx.startsWith("_inbox/") && !(d2.inbox || []).some(i => `_inbox/${i.id}.md` === state.ctx)) { state.ctx = ""; ctxShow(""); }
    else if (isTopic(state.ctx)) ctxShow(state.ctx);       // summaries land while we look
    if (!ctxBusy(d2)) clearInterval(_ctxTimer);
  }, 8000);
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
  if (s.current) bits.push(`<span class="run">summarising ${esc(s.current.topic)}</span>${s.queued ? ` +${s.queued}` : ""}`);
  const last = (f.recent || [])[0], lastS = (s.recent || [])[0];
  if (!cur && !waiting && !failed && !s.current) {
    bits.push(last ? `inbox empty · last filing ${last.ok ? "ok" : `<span class="bad">failed</span>`} ${fmtAgo(last.at)} ago (${last.seconds}s)` : "inbox empty");
    if (lastS) bits.push(lastS.ok ? `summaries up to date ($${(lastS.cost || 0).toFixed(4)} last)` : `<span class="bad">summary of ${esc(lastS.topic)} failed: ${esc(lastS.error || "")}</span>`);
  }
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
    notes.forEach(n => row("n", `${node.path}/${n}`, depth + 1, esc((node.titles || {})[n] || n.replace(/^\d{4}-\d{2}-\d{2}-/, "").replace(/\.md$/, "").replace(/-/g, " ")), esc(n.slice(0, 10))));
    kids.forEach(k => walk(k, depth + 1));
  };
  const kids = (d.tree && d.tree.children) || [];
  if (!kids.length) rows.push(`<div class="empty pad">No topics yet. Upload something; the filer makes them.</div>`);
  kids.forEach(k => walk(k, 0));
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

function ctxShow(p) { return isTopic(p) ? openTopic(p) : openCtx(p); }

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
  const s = d.summarizing || {}, busy = s.current && (s.current.topic === p || (s.current.topic || "").startsWith(p + "/"));
  const kids = node.children || [], notes = node.notes || [];
  const head = `${ctxCrumbs(root ? "" : p.split("/").slice(0, -1).join("/"), root ? "" : node.slug)}
    <div class="th"><h2>${esc(root ? "All context" : node.title)}</h2>
      <div class="pa">${root ? "" : `<button class="ghost sm" id="tpedit" title="edit _topic.md">Edit description</button><button class="ghost sm" id="tpsum" ${busy ? "disabled" : ""}>${busy ? "summarising…" : "Refresh summary"}</button>`}</div></div>
    ${node.about ? `<p class="about">${esc(node.about)}</p>` : root ? `<p class="about">Pillars are the owner's truth. Topics hold what was uploaded and filed, each with a generated summary.</p>` : ""}`;
  const sum = root ? "" : node.summary ? `<div class="sec"><div class="sh">Summary <span class="stamp">${esc((node.summarized || "").replace("T", " ").replace(/:\d\dZ/, "Z"))}</span></div>${mdLines(node.summary)}</div>`
    : `<div class="sec"><div class="sh">Summary</div><div class="empty">${busy ? "being written now" : notes.length || kids.length ? "not written yet; it comes after the next filing or save, or press Refresh summary" : "empty topic"}</div></div>`;
  const pill = root ? `<div class="sec"><div class="sh">Pillars</div><div class="cards">${(d.pillars || []).map(n => `<div class="card f" data-p="${esc(n)}"><div class="ct">${esc(n.replace(/\.md$/, ""))}</div><div class="cb">owner-written</div></div>`).join("")}</div></div>` : "";
  const subs = kids.length ? `<div class="sec"><div class="sh">${root ? "Topics" : "Subtopics"}</div><div class="cards">${kids.map(k => `<div class="card t" data-p="${esc(k.path)}"><div class="ct">${SVG_FOLDER}${esc(k.title || k.slug)}<span class="cnt">${ctxCount(k)}</span></div><div class="cb">${esc(k.about || "")}</div><div class="cs">${esc(k.slug)}${k.summary ? "" : " · no summary yet"}</div></div>`).join("")}</div></div>` : "";
  const nts = notes.length ? `<div class="sec"><div class="sh">Notes</div><div class="notes">${notes.map(n => { const m = ctxNoteMeta(n); return `<div class="note" data-p="${esc(p)}/${esc(n)}"><div class="nt">${esc((node.titles || {})[n] || n)}</div><div class="nm2">${esc(m.date)}${m.rest ? ` · ${esc(m.rest)}` : ""}</div></div>`; }).join("")}</div></div>` : root ? "" : `<div class="sec"><div class="sh">Notes</div><div class="empty">none here; ${kids.length ? "they sit in the subtopics" : "nothing filed yet"}</div></div>`;
  v.innerHTML = head + sum + pill + subs + nts;
  wireCrumbs(v);
  v.querySelectorAll(".card, .note").forEach(el => el.onclick = () => { if (el.classList.contains("t")) { ctxOpen().add(el.dataset.p); ctxSaveOpen(); } ctxGo(el.dataset.p); });
  const ed = $("#tpedit"); if (ed) ed.onclick = () => ctxGo(`${p}/_topic.md`);
  const rs = $("#tpsum"); if (rs) rs.onclick = async () => { rs.disabled = true; const r = await post(`/biz/${state.biz}/context/summarize`, { topic: p }); toast(r.error || `summarising ${p}`); const d2 = await api(`/biz/${state.biz}/context`); ctxPaint(d2); openTopic(p); ctxWatch(d2); };
}
const ctxNoteMeta = n => ({ date: n.slice(0, 10), rest: "" });

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
  const title = meta.title || (p.endsWith("/_topic.md") ? "description" : p.split("/").pop());
  v.innerHTML = `${ctxCrumbs(dir, p.split("/").pop())}<div class="ph"><h2>${esc(title)}</h2><div class="pa">${ro ? `<span class="ro">${p === "INDEX.md" ? "generated" : "filed upload, immutable"}</span>` : `<button class="ghost sm" id="ctxedit">Edit</button>`}</div></div>
    ${chips ? `<div class="chips">${chips}</div>` : ""}<pre class="ctxbody">${esc(body)}</pre>`;
  wireCrumbs(v);
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
