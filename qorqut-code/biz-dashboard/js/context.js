// ---------------------------------------------------------------- context
// The Context tab: upload text (goes to context/_inbox, filed by the queue), browse the topic tree,
// read and edit any file. Every save is one commit. Nothing here talks to a model.
let _ctxTimer = null, _ctxOpen = null;
const ctxOpen = () => { if (!_ctxOpen) { try { _ctxOpen = new Set(JSON.parse(ls("ctxopen") || "[]")); } catch (e) { _ctxOpen = new Set(); } } return _ctxOpen; };
const ctxSaveOpen = () => ls("ctxopen", JSON.stringify([...ctxOpen()]));
const SVG_CARET = `<svg class="car" width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 2l3 3-3 3"/></svg>`;
const fmtAgo = t => { const s = Math.max(0, Math.round(Date.now() / 1000 - t)); return s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`; };

async function renderContext(c) {
  clearInterval(_ctxTimer); _ctxTimer = null;
  const d = await api(`/biz/${state.biz}/context`);
  c.innerHTML = `<div class="ctxwrap">
    <div class="ctxbar"><button class="primary" id="ctxup">Upload context</button><div class="ctxq" id="ctxq"></div></div>
    <div class="split ctxsplit"><div class="listcol ctxtree" id="ctxtree"></div><div class="pane ctxview" id="ctxview"></div></div></div>`;
  $("#ctxup").onclick = uploadModal;
  ctxPaint(d);
  if (state.ctx) openCtx(state.ctx);
  else $("#ctxview").innerHTML = `<div class="empty">Pick a file. Pillars are the owner's truth, topics hold what was uploaded and filed.</div>`;
  ctxWatch(d);
}

function ctxBusy(d) { return !!((d.filing && d.filing.current) || (d.inbox || []).some(i => !i.failed)); }
function ctxWatch(d) {
  clearInterval(_ctxTimer); _ctxTimer = null;
  if (!ctxBusy(d)) return;
  _ctxTimer = setInterval(async () => {
    if (state.tab !== "context" || !$("#ctxtree")) return clearInterval(_ctxTimer);
    const d2 = await api(`/biz/${state.biz}/context`);
    ctxPaint(d2);
    if (state.ctx && state.ctx.startsWith("_inbox/") && !(d2.inbox || []).some(i => `_inbox/${i.id}.md` === state.ctx)) { state.ctx = null; $("#ctxview").innerHTML = `<div class="empty">Filed. Its notes are in the tree now.</div>`; }
    if (!ctxBusy(d2)) clearInterval(_ctxTimer);
  }, 8000);
}

function ctxPaint(d) {
  const q = $("#ctxq"), inbox = d.inbox || [], f = d.filing || {}, cur = f.current, failed = inbox.filter(i => i.failed).length, waiting = inbox.length - failed;
  const bits = [];
  if (d.error) bits.push(`<span class="bad">${esc(d.error)}</span>`);
  if (d.has_filer === false) bits.push(`<span class="bad">no agents/_filer in this business; copy it from biz-template</span>`);
  if (cur) bits.push(`<span class="run">filing ${esc(cur.id)} · ${fmtAgo(cur.started)}</span>`);
  if (waiting) bits.push(`${waiting} waiting${cur ? "" : ` · next sweep within ${Math.round((f.every || 300) / 60)} min`}`);
  if (failed) bits.push(`<span class="bad">${failed} failed</span>`);
  const last = (f.recent || [])[0];
  if (!cur && !waiting && !failed) bits.push(last ? `inbox empty · last filing ${last.ok ? "ok" : `<span class="bad">failed</span>`} ${fmtAgo(last.at)} ago (${last.seconds}s)` : "inbox empty");
  q.innerHTML = bits.join(`<span class="dot">·</span>`);
  ctxTree(d);
}

function ctxTree(d) {
  const t = $("#ctxtree"), open = ctxOpen(), rows = [];
  const row = (cls, p, ind, html, sub) => rows.push(`<div class="item ${cls} ${p === state.ctx ? "on" : ""}" data-p="${esc(p)}" style="--ind:${ind * 14}px"><div class="nm">${html}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`);
  rows.push(`<div class="h">Pillars</div>`);
  (d.pillars || []).forEach(n => row("f", n, 0, esc(n)));
  row("f idx", "INDEX.md", 0, `INDEX.md <em>generated</em>`);
  rows.push(`<div class="h">Topics</div>`);
  const walk = (node, depth) => {
    const notes = node.notes || [], kids = node.children || [], all = ctxCount(node), isOpen = open.has(node.path);
    row(`t ${isOpen ? "open" : ""}`, `${node.path}/_topic.md`, depth, `${SVG_CARET}<span class="tt">${esc(node.title || node.slug)}</span><span class="cnt">${all}</span>`, esc(node.slug));
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
    if (el.classList.contains("t") && (e.target.closest(".car") || p === state.ctx)) {
      const tp = p.replace(/\/_topic\.md$/, "");
      open.has(tp) ? open.delete(tp) : open.add(tp); ctxSaveOpen(); ctxTree(d); return;
    }
    if (el.classList.contains("t")) { const tp = p.replace(/\/_topic\.md$/, ""); open.add(tp); ctxSaveOpen(); }
    state.ctx = p; ctxTree(d); openCtx(p);
  });
}
const ctxCount = n => (n.notes || []).length + (n.children || []).reduce((s, k) => s + ctxCount(k), 0);

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

async function openCtx(p) {
  const v = $("#ctxview");
  const r = await api(`/biz/${state.biz}/context/file?p=${encodeURIComponent(p)}`);
  if (r.error) { v.innerHTML = `<div class="empty">${esc(r.error)}</div>`; return; }
  const ro = p === "INDEX.md" || p.startsWith("_dumps/");
  const { meta, body } = ctxSplit(r.text);
  const chips = CTX_META.filter(k => meta[k]).map(k => `<span class="chip ${k === "error" ? "bad" : ""}"><b>${esc(k)}</b>${esc(meta[k])}</span>`).join("");
  v.innerHTML = `<div class="ph"><div class="pp">context/${esc(p)}</div><div class="pa">${ro ? `<span class="ro">${p === "INDEX.md" ? "generated" : "filed upload, immutable"}</span>` : `<button class="ghost" id="ctxedit">Edit</button>`}</div></div>
    ${chips ? `<div class="chips">${chips}</div>` : ""}<pre class="ctxbody">${esc(body)}</pre>`;
  if (ro) return;
  $("#ctxedit").onclick = () => {
    v.innerHTML = `<div class="ph"><div class="pp">context/${esc(p)}</div><div class="pa"><span class="ro">editing</span></div></div>
      <div class="ctxedit"><textarea id="ctxta" spellcheck="false"></textarea>
      <div class="row"><span class="hint">one commit per save</span><button class="ghost" id="ctxcancel">Cancel</button><button class="primary" id="ctxsave">Save</button></div></div>`;
    const ta = $("#ctxta"); ta.value = r.text; ta.focus();
    $("#ctxcancel").onclick = () => openCtx(p);
    $("#ctxsave").onclick = async () => {
      if (ta.value === r.text) return openCtx(p);
      $("#ctxsave").disabled = true;
      const s = await post(`/biz/${state.biz}/context/save`, { path: p, text: ta.value });
      if (s.error) { toast(s.error); $("#ctxsave").disabled = false; return; }
      toast(s.warn ? `saved, but: ${s.warn}` : `saved ${p}`);
      const d = await api(`/biz/${state.biz}/context`); ctxPaint(d);
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
        renderContext($("#content")); refreshSide && refreshSide();
      } },
    ],
  });
}
