const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ls = (k, v) => { try { return v === undefined ? localStorage.getItem(k) : localStorage.setItem(k, v); } catch (e) { return null; } };
// one scheme for every business
const DEPTS = {
  marketing: { label: "Marketing", icon: "circle", color: "#b5482b" },
  accounting: { label: "Accounting", icon: "square", color: "#4d6b53" },
  "data-science": { label: "Data science", icon: "triangle", color: "#3d5a80" },
  code: { label: "Code", icon: "square", color: "#7a4f7a" },
  general: { label: "General", icon: "circle", color: "#8a8a8a" },
};
const COLORS = { red: "#b5482b", amber: "#c8912a", green: "#4d6b53", teal: "#3f7f7a", blue: "#3d5a80", plum: "#7a4f7a", ink: "#f5f0e8" };
const SHAPES = ["circle", "square", "triangle"];
const TAG_DEPT = { marketing: "marketing", operations: "accounting", research: "data-science", coding: "code" };
const dept = k => DEPTS[k] || DEPTS.general;
const hex = c => COLORS[c] || COLORS.ink;
const glyph = (shape, colorHex, sizePx) => `<span class="g ${SHAPES.includes(shape) ? shape : "circle"}" style="--gs:${sizePx}px;--gc:${colorHex}"></span>`;
// one look per department, same in every business: the agent glyph is its department glyph
const agentGlyph = (m, size) => { const d = dept((m || {}).department); return glyph(d.icon, d.color, size); };

// lazy: the render functions live in later script files
const TABS = { agents: c => renderAgents(c), context: c => renderContext(c),
  marketing: c => renderMarketing(c), accounting: c => soon(c, "The cash ledger and what recurs."),
  datascience: c => soon(c, "Experiments and metric deep-dives."),
  progress: c => soon(c, "Tasks, milestones and what shipped."),
  code: c => soon(c, "Repos, commits and deploys.") };
let verbose0 = ls("verbose") === "1", tab0 = ls("tab");
const state = { biz: null, bizs: [], tab: TABS[tab0] ? tab0 : "agents", agent: null, agents: [], liveNow: [], ctx: null, live: null, verbose: verbose0 };
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const authHead = TOKEN ? { Authorization: "Bearer " + TOKEN } : {};
const api = async (p, opt = {}) => { const r = await fetch("/api" + p, { ...opt, headers: { ...(opt.headers || {}), ...authHead } }); return r.json(); };
const post = (p, body) => api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
// EventSource can't set headers, so carry the token as a query param.
const evUrl = p => "/api" + p + (TOKEN ? (p.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN) : "");
// ---------------------------------------------------------------- notifications
// One queue for every toast: bottom-right, newest at the bottom, up to NOTE_MAX on screen, the rest wait in
// FIFO order. A note rises in, stays NOTE_MS (hovering holds it, a click dismisses it), then sinks out and the
// next one takes its slot. A backlog longer than NOTE_QUEUE_MAX folds its tail into one "N more" note so a
// burst never floods the corner. `notify({text, sub?, color?, ms?})`; `toast(text)` is the short form.
const NOTE_MAX = 3, NOTE_MS = 4200, NOTE_QUEUE_MAX = 6;
const _notes = { queue: [], live: 0 };
function notify(n) {
  if (typeof n === "string") n = { text: n };
  if (!n || !n.text) return;
  _notes.queue.push(n);
  if (_notes.queue.length > NOTE_QUEUE_MAX) {
    const keep = _notes.queue.slice(0, NOTE_QUEUE_MAX - 1), rest = _notes.queue.slice(NOTE_QUEUE_MAX - 1);
    const folded = rest.reduce((k, x) => k + (x.n || 1), 0);
    _notes.queue = keep.concat([{ text: `${folded} more`, sub: "in the log", n: folded, fold: true }]);
  }
  notePump();
}
function notePump() {
  const box = $("#toasts"); if (!box) return;
  while (_notes.live < NOTE_MAX && _notes.queue.length) {
    const n = _notes.queue.shift(); _notes.live++;
    const el = document.createElement("div");
    el.className = "toast in" + (n.fold ? " fold" : "");
    if (n.color) el.style.setProperty("--c", n.color);
    el.innerHTML = `<div class="tx">${esc(n.text)}</div>${n.sub ? `<div class="tsub">${esc(n.sub)}</div>` : ""}`;
    box.appendChild(el);
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.remove("in")));
    let t, gone = false;
    const go = () => { if (gone) return; gone = true; clearTimeout(t); el.classList.add("out"); setTimeout(() => { el.remove(); _notes.live--; notePump(); }, 260); };
    const arm = () => { clearTimeout(t); t = setTimeout(go, n.ms || NOTE_MS); };
    el.addEventListener("mouseenter", () => clearTimeout(t));
    el.addEventListener("mouseleave", arm);
    el.addEventListener("click", go);
    arm();
  }
}
const toast = m => notify(m);

// ---------------------------------------------------------------- tokens & money
// USD per 1M tokens at peak; fallback only, used when an event carries no `cost`.
const RATES = { "deepseek-v4-flash": { hit: 0.014, miss: 0.44, out: 1.32 } };

// 1234567 -> "1.2M" | 340000 -> "340K" | 12 -> "12"
function fmtTok(n) {
  if (n == null || isNaN(n)) return "0";
  const v = Number(n), a = Math.abs(v), s = v < 0 ? "-" : "";
  const trim = x => x.toFixed(1).replace(/\.0$/, "");
  if (a >= 1e6) return s + trim(a / 1e6) + "M";
  if (a >= 1e5) return s + Math.round(a / 1e3) + "K";
  if (a >= 1e3) return s + trim(a / 1e3) + "K";
  return s + String(Math.round(a));
}

// 0.31 -> "$0.31" | 0.0042 -> "$0.0042" (4 decimals under a cent)
function fmtUsd(x) {
  if (x == null || isNaN(x)) return "?";
  const v = Number(x), a = Math.abs(v), s = v < 0 ? "-" : "";
  if (a === 0) return "$0.00";
  if (a < 0.01) return s + "$" + a.toFixed(4);
  if (a >= 1000) return s + "$" + a.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return s + "$" + a.toFixed(2);
}

// ---------------------------------------------------------------- modal
const SVG_X = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3.5 3.5l7 7M10.5 3.5l-7 7"/></svg>`;
const SVG_CHECK = `<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 6.2 4.8 8.5 9.5 3.5"/></svg>`;

let _modal = null;
function modal({ title = "", body = "", actions = [], width = 520, onOpen } = {}) {
  if (_modal) _modal.close();
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `<div class="modal" style="width:${Number(width) || 520}px" role="dialog" aria-modal="true">
    <div class="mhead"><div class="mt">${title}</div><button class="mx" type="button" aria-label="Close">${SVG_X}</button></div>
    <div class="mbody">${body}</div>
    ${actions.length ? `<div class="mfoot"></div>` : ""}
  </div>`;
  const el = ov.firstElementChild;
  let closed = false;
  const close = () => {
    if (closed) return; closed = true;
    document.removeEventListener("keydown", onKey, true);
    ov.remove(); if (_modal && _modal.el === el) _modal = null;
  };
  // an open dropdown owns Escape (both listeners sit on document capture, so stopPropagation alone does not help)
  const onKey = e => { if (e.key === "Escape" && !_menu) { e.stopPropagation(); close(); } };
  const foot = el.querySelector(".mfoot");
  actions.forEach(a => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = a.primary ? "primary" : "ghost";
    b.textContent = a.label || "";
    b.onclick = () => { try { a.onClick && a.onClick(close, el); } catch (err) { console.error(err); } };
    foot.appendChild(b);
  });
  el.querySelector(".mx").onclick = close;
  ov.onmousedown = e => { if (e.target === ov) close(); };
  document.addEventListener("keydown", onKey, true);
  document.body.appendChild(ov);
  _modal = { el, close };
  const f = el.querySelector("input, textarea, select, .selbtn");
  if (f) try { f.focus(); } catch (e) {}
  if (onOpen) try { onOpen(el); } catch (e) { console.error(e); }
  return { el, close };
}

// ---------------------------------------------------------------- dropdown
let _menu = null;
function dropdown(anchorEl, items, onPick, current) {
  if (_menu) _menu.close();
  const list = (items || []).map(it => (typeof it === "string" ? { value: it, label: it } : it));
  const m = document.createElement("div");
  m.className = "menu";
  m.innerHTML = list.map((it, i) => `<div class="mi ${it.value === current ? "on" : ""}" data-i="${i}">
    <span class="ck">${SVG_CHECK}</span><span class="lb">${it.html != null ? it.html : esc(it.label != null ? it.label : it.value)}</span></div>`).join("")
    || `<div class="mi" style="opacity:.5;cursor:default">nothing</div>`;
  document.body.appendChild(m);
  if (anchorEl && anchorEl.classList) anchorEl.classList.add("open");

  const r = anchorEl ? anchorEl.getBoundingClientRect() : { left: 20, right: 200, top: 20, bottom: 20, width: 180 };
  m.style.minWidth = Math.max(r.width, 160) + "px";
  const h = m.offsetHeight, w = m.offsetWidth;
  let top = r.bottom + 6;
  if (top + h > window.innerHeight - 12) top = Math.max(12, r.top - h - 6);
  let left = r.left;
  if (left + w > window.innerWidth - 12) left = Math.max(12, window.innerWidth - w - 12);
  m.style.top = top + "px"; m.style.left = left + "px";

  let hi = Math.max(0, list.findIndex(it => it.value === current));
  const rows = [...m.querySelectorAll(".mi[data-i]")];
  const paint = () => rows.forEach((el, i) => el.classList.toggle("hi", i === hi));
  paint();

  let closed = false;
  const close = () => {
    if (closed) return; closed = true;
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("mousedown", onDown, true);
    window.removeEventListener("resize", close); window.removeEventListener("scroll", close, true);
    if (anchorEl && anchorEl.classList) anchorEl.classList.remove("open");
    m.remove(); if (_menu && _menu.el === m) _menu = null;
  };
  const pick = i => { const it = list[i]; close(); if (it && onPick) onPick(it.value, it); };
  const onKey = e => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); e.stopPropagation(); hi = (hi + 1) % rows.length; paint(); rows[hi].scrollIntoView({ block: "nearest" }); }
    else if (e.key === "ArrowUp") { e.preventDefault(); e.stopPropagation(); hi = (hi - 1 + rows.length) % rows.length; paint(); rows[hi].scrollIntoView({ block: "nearest" }); }
    else if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); pick(hi); }
  };
  const onDown = e => { if (!m.contains(e.target) && e.target !== anchorEl && !(anchorEl && anchorEl.contains(e.target))) close(); };
  rows.forEach((el, i) => { el.onclick = () => pick(i); el.onmouseenter = () => { hi = i; paint(); }; });
  document.addEventListener("keydown", onKey, true);
  setTimeout(() => document.addEventListener("mousedown", onDown, true), 0);
  window.addEventListener("resize", close); window.addEventListener("scroll", close, true);
  _menu = { el: m, close };
  return { el: m, close };
}

// ---------------------------------------------------------------- tooltip
let _tipEl = null, _tipT = null;
function _tipHide() { clearTimeout(_tipT); if (_tipEl) { _tipEl.remove(); _tipEl = null; } }
function tip(el, htmlOrFn) {
  if (!el) return;
  el._tipHtml = htmlOrFn;
  if (el._tipBound) return el;
  el._tipBound = true;
  el.addEventListener("mouseenter", () => {
    clearTimeout(_tipT);
    _tipT = setTimeout(() => {
      const src = el._tipHtml;
      let html; try { html = typeof src === "function" ? src(el) : src; } catch (e) { html = ""; }
      if (html == null || html === "") return;
      _tipHide();
      const t = document.createElement("div");
      t.className = "tip"; t.innerHTML = html;
      document.body.appendChild(t); _tipEl = t;
      const r = el.getBoundingClientRect(), w = t.offsetWidth, h = t.offsetHeight, gap = 8;
      let left, top;
      if (r.left > window.innerWidth * 0.6) { left = r.left - w - gap; top = r.top; }
      else { left = r.left; top = r.bottom + gap; }
      if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - gap);
      left = Math.min(Math.max(8, left), window.innerWidth - w - 8);
      top = Math.min(Math.max(8, top), window.innerHeight - h - 8);
      t.style.left = left + "px"; t.style.top = top + "px";
    }, 120);
  });
  el.addEventListener("mouseleave", _tipHide);
  el.addEventListener("mousedown", _tipHide);
  return el;
}

// ---------------------------------------------------------------- businesses
async function loadBiz() {
  state.bizs = await api("/biz");
  paintBiz();
  if (!state.biz && state.bizs.length) selectBiz(location.hash.slice(1) || state.bizs[0].slug);
}
function paintBiz() {
  const b = state.bizs.find(x => x.slug === state.biz);
  $("#wsletter").textContent = b && b.name ? b.name.charAt(0).toUpperCase() : "·";
  $("#wsbtn").title = b ? b.name : "Businesses";
}
function selectBiz(slug) {
  const b = state.bizs.find(x => x.slug === slug); if (!b) return;
  state.biz = slug; state.agent = b.agents[0] || null; state.ctx = null; location.hash = slug;
  paintBiz();
  renderTab(); refreshSide();
}
$("#wsbtn").onclick = () => dropdown($("#wsbtn"), state.bizs.map(b => ({
  value: b.slug,
  html: `${esc(b.name)} <span class="muted small">${b.agents.length}</span>`,
})), slug => selectBiz(slug), state.biz);
document.querySelectorAll(".ritem").forEach(t => t.onclick = () => {
  state.tab = t.dataset.tab; ls("tab", state.tab);
  document.querySelectorAll(".ritem").forEach(x => x.classList.toggle("on", x === t));
  renderTab(); moveInd();
});
document.querySelectorAll(".ritem").forEach(t => t.classList.toggle("on", t.dataset.tab === state.tab));
moveInd();
window.addEventListener("resize", moveInd);
function moveInd() {
  const on = document.querySelector(".ritem.on"), ind = $("#rind");
  if (!on || !ind) return;
  ind.hidden = false;
  ind.style.transform = `translateY(${on.offsetTop - ind.offsetTop}px)`;
}

function renderTab() {
  const c = $("#content"); c.innerHTML = "";
  if (!state.biz) return;
  (TABS[state.tab] || TABS.agents)(c);
}
function soon(c, line) {
  const p = document.createElement("div"); p.className = "pane soonpane";
  p.innerHTML = `<div class="t">Coming soon</div><div class="muted small">${esc(line)}</div>`;
  c.appendChild(p);
}
