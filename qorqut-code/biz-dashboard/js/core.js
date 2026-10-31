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
  onbashlyk: { label: "Onbashlyk", icon: "triangle", color: "#c8912a" },
};
const COLORS = { red: "#b5482b", amber: "#c8912a", green: "#4d6b53", teal: "#3f7f7a", blue: "#3d5a80", plum: "#7a4f7a", ink: "#f5f0e8" };
const SHAPES = ["circle", "square", "triangle"];
const TAG_DEPT = { marketing: "marketing", operations: "accounting", research: "data-science", coding: "code", onbashlyk: "onbashlyk" };
const dept = k => DEPTS[k] || DEPTS.general;
const hex = c => COLORS[c] || COLORS.ink;
const glyph = (shape, colorHex, sizePx) => `<span class="g ${SHAPES.includes(shape) ? shape : "circle"}" style="--gs:${sizePx}px;--gc:${colorHex}"></span>`;
// 24 lucide-style icons an agent can pick, keyed by kebab-case lucide name; each value is the
// inner markup for a 24x24 stroke viewBox (paths/shapes only, no outer <svg>)
const AICONS = {
  "share-2": '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 10.5 6.8-4M8.6 13.5l6.8 4"/>',
  "refresh-cw": '<path d="M3 12a9 9 0 0 1 15.36-6.36L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.36 6.36L3 16"/><path d="M8 16H3v5"/>',
  "arrow-left": '<path d="m12 19-7-7 7-7"/><path d="M5 12h14"/>',
  bot: '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>',
  brain: '<path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/><path d="M17.599 6.5a3 3 0 0 0 .399-1.375"/><path d="M6.003 5.125A3 3 0 0 0 6.401 6.5"/><path d="M3.477 10.896a4 4 0 0 1 .585-.396"/><path d="M19.938 10.5a4 4 0 0 1 .585.396"/><path d="M6 18a4 4 0 0 1-1.967-.516"/><path d="M19.967 17.484A4 4 0 0 1 18 18"/>',
  sparkles: '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/><path d="M4 17v2"/><path d="M5 18H3"/>',
  zap: '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
  rocket: '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
  flame: '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
  ghost: '<path d="M9 10h.01"/><path d="M15 10h.01"/><path d="M12 2a8 8 0 0 0-8 8v12l3-3 2.5 2.5L12 19l2.5 2.5L17 19l3 3V10a8 8 0 0 0-8-8z"/>',
  cat: '<path d="M12 5c.67 0 1.35.09 2 .26 1.78-2 5.03-2.84 6.42-2.26 1.4.58-.42 7-.42 7 .57 1.07 1 2.24 1 3.44C21 17.9 16.97 21 12 21s-9-3-9-7.56c0-1.25.5-2.4 1-3.44 0 0-1.89-6.42-.5-7 1.39-.58 4.72.23 6.5 2.23A9.04 9.04 0 0 1 12 5z"/><path d="M8 14v.5"/><path d="M16 14v.5"/><path d="M11.25 16.25h1.5L12 17Z"/>',
  dog: '<path d="M11.25 16.25h1.5L12 17Z"/><path d="M16 14v.5"/><path d="M4.42 11.247A13.152 13.152 0 0 0 4 14.556C4 18.728 7.582 21 12 21s8-2.272 8-6.444c0-1.061-.162-2.2-.493-3.309"/><path d="M8 14v.5"/><path d="M15.997 12.44a3 3 0 0 1 2.526 3.026"/><path d="M8.003 12.44A3 3 0 0 0 5.477 15.466"/><path d="M18.5 8a2.5 2.5 0 0 0-2.5-2.5V4a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v1.5A2.5 2.5 0 0 0 5.5 8"/>',
  bird: '<path d="M16 7h.01"/><path d="M3.4 18H12a8 8 0 0 0 8-8V7a4 4 0 0 0-7.28-2.3L2 20"/><path d="m20 7 2 .5-2 .5"/><path d="M10 18v3"/><path d="M14 17.75V21"/><path d="M7 18a6 6 0 0 0 3.84-10.61"/>',
  fish: '<path d="M6.5 12c.94-3.46 4.94-6 8.5-6 3.56 0 6.06 2.54 7 6-.94 3.47-3.44 6-7 6s-7.56-2.53-8.5-6Z"/><path d="M18 12v.5"/><path d="M16 17.93a9.77 9.77 0 0 1 0-11.86"/><path d="M7 10.67C7 8 5.58 5.97 2.73 5.5c-1 1.5-1 5 .23 6.5-1.24 1.5-1.24 5-.23 6.5C5.58 18.03 7 16 7 13.33"/><path d="M10.46 7.26C10.2 5.88 9.17 4.24 8 3h5.8a2 2 0 0 1 1.98 1.67l.23 1.4"/><path d="m16.01 17.93-.23 1.4A2 2 0 0 1 13.8 21H9.5a5.96 5.96 0 0 0 1.49-3.98"/>',
  bug: '<rect width="8" height="14" x="8" y="6" rx="4"/><path d="m19 7-3 2"/><path d="m5 7 3 2"/><path d="m19 19-3-2"/><path d="m5 19 3-2"/><path d="M20 13h-4"/><path d="M4 13h4"/><path d="m10 4 1 2"/><path d="m14 4-1 2"/>',
  cpu: '<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/>',
  terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/>',
  code: '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
  "pen-tool": '<path d="M15.707 21.293a1 1 0 0 1-1.414 0l-1.586-1.586a1 1 0 0 1 0-1.414l5.586-5.586a1 1 0 0 1 1.414 0l1.586 1.586a1 1 0 0 1 0 1.414z"/><path d="m18 13-1.375-6.874a1 1 0 0 0-.746-.776L3.235 2.028a1 1 0 0 0-1.207 1.207L5.35 15.879a1 1 0 0 0 .776.746L13 18"/><path d="m2.3 2.3 7.286 7.286"/><circle cx="11" cy="11" r="2"/>',
  megaphone: '<path d="m3 11 18-5v12L3 14v-3z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/>',
  calculator: '<rect width="16" height="20" x="4" y="2" rx="2"/><line x1="8" x2="16" y1="6" y2="6"/><line x1="16" x2="16" y1="14" y2="18"/><path d="M16 10h.01"/><path d="M12 10h.01"/><path d="M8 10h.01"/><path d="M12 14h.01"/><path d="M8 14h.01"/><path d="M12 18h.01"/><path d="M8 18h.01"/>',
  "chart-column": '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
  database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  globe: '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
  wrench: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
};
const aicon = (name, colorHex, sizePx) => `<svg class="ai" width="${sizePx}" height="${sizePx}" viewBox="0 0 24 24" fill="none" stroke="${colorHex}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${AICONS[name]}</svg>`;
// one look per department, same in every business, unless the agent picked a custom icon
const agentGlyph = (m, size) => { const d = dept((m || {}).department); return (m && m.icon && AICONS[m.icon]) ? aicon(m.icon, d.color, size) : glyph(d.icon, d.color, size); };

// lazy: the render functions live in later script files
const TABS = { agents: c => renderAgents(c), context: c => renderContext(c),
  marketing: c => renderMarketing(c), accounting: c => renderTables(c, {department: 'accounting'}),
  datascience: c => soon(c, "Experiments and metric deep-dives."),
  progress: c => soon(c, "Tasks, milestones and what shipped."),
  code: c => soon(c, "Repos, commits and deploys."),
  settings: c => renderSettings(c) };
let verbose0 = ls("verbose") === "1", tab0 = ls("tab");
if (tab0 === "email") tab0 = "marketing";
const contextTab0 = {stories: "_stories", tables: "_tables"}[tab0];
if (contextTab0) tab0 = "context";
const state = { scope: "business", globalTab: "overview", biz: null, bizs: [], tab: TABS[tab0] ? tab0 : "agents", agent: null, agents: [], liveNow: [], ctx: contextTab0 || null, live: null, verbose: verbose0 };
const api = async (p, opt = {}) => {
  const r = await fetch("/api" + p, opt);
  const body = await r.json();
  // Legacy views consume error text; core adapters still need the structured failure.
  if (body && typeof body === "object") {
    Object.defineProperty(body, "httpStatus", { value: r.status, enumerable: false });
    if (body.status === "error" && body.error && typeof body.error === "object") {
      Object.defineProperty(body, "coreError", { value: body.error, enumerable: false });
      body.error = body.error.message || "The request failed.";
    }
  }
  return body;
};
const post = (p, body) => api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
function newRequestId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // HTTP/Tailscale origins expose getRandomValues but not randomUUID.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
function coreData(result) {
  if (!result || !["ok", "accepted"].includes(result.status) || result.error || !result.data) {
    const source = result && (result.coreError || result.error);
    const error = new Error(typeof source === "string" ? source : (source && source.message) || "The server returned an invalid response.");
    error.code = (source && source.code) || "invalid_response";
    error.details = (source && source.details) || {};
    error.httpStatus = result && result.httpStatus;
    throw error;
  }
  return result.data;
}
const evUrl = p => "/api" + p;
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
// the upload glyph: the tasks panel's Uploads tab icon, and the leading icon on every upload button
const ICO_UPLOAD = `<svg class="ico-upload" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>`;
const SVG_X = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3.5 3.5l7 7M10.5 3.5l-7 7"/></svg>`;
const SVG_CHECK = `<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 6.2 4.8 8.5 9.5 3.5"/></svg>`;

let _modal = null;
function modal({ title = "", body = "", actions = [], width = 520, onOpen, stack = false } = {}) {
  const parent = stack ? _modal : null;
  if (!stack) while (_modal) _modal.close();
  const parentOverlay = parent?.el.closest('.overlay');
  if (parentOverlay) {parentOverlay.inert = true;parent.el.setAttribute('aria-hidden','true');}
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
    ov.remove();
    if (_modal && _modal.el === el) _modal = parent?.el.isConnected ? parent : null;
    if (parentOverlay) {parentOverlay.inert = false;parent.el.removeAttribute('aria-hidden');}
  };
  // an open dropdown owns Escape (both listeners sit on document capture, so stopPropagation alone does not help)
  const onKey = e => { if (e.key === "Escape" && !_menu && _modal?.el === el) { e.stopPropagation(); close(); } };
  const foot = el.querySelector(".mfoot");
  actions.forEach(a => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = a.primary ? "primary" : "ghost";
    if (a.icon) { b.innerHTML = a.icon; b.append(a.label || ""); } else { b.textContent = a.label || ""; }
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
function dropdown(anchorEl, items, onPick, current, options = {}) {
  if (_menu) _menu.close();
  const list = (items || []).map(it => (typeof it === "string" ? { value: it, label: it } : it));
  const m = document.createElement("div");
  m.className = "menu" + (options.className ? " " + options.className : "");
  if (options.width) m.style.width = options.width + "px";
  m.innerHTML = list.map((it, i) => `<div class="mi ${it.value === current ? "on" : ""}" data-i="${i}">
    ${options.noCheck ? "" : `<span class="ck">${SVG_CHECK}</span>`}<span class="lb">${it.html != null ? it.html : esc(it.label != null ? it.label : it.value)}</span></div>`).join("")
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
let workspaceNavigation = 0;
async function loadBiz() {
  try {
    const businesses = await api("/biz");
    if (!Array.isArray(businesses)) throw new Error(businesses && businesses.error || "Could not load businesses.");
    state.bizs = businesses;
    paintBiz();
    if (location.hash.startsWith("#/global")) selectGlobal(location.hash.split("/")[2]);
    else if (!state.biz && state.bizs.length) selectBiz(state.bizs.some(b => b.slug === location.hash.slice(1)) ? location.hash.slice(1) : state.bizs[0].slug);
    else if (!state.biz) selectGlobal();
  } catch (error) {
    $("#content").innerHTML = `<div class="empty"><h2>Could not load the dashboard</h2><p>${esc(error.message)}</p><button class="btn" id="business-retry">Try again</button></div>`;
    $("#business-retry").onclick = loadBiz;
  }
}
function paintBiz() {
  const b = state.bizs.find(x => x.slug === state.biz);
  if (state.scope === "global") {
    $("#wsletter").innerHTML = globalIcon("globe");
    $("#wsbtn").title = "Global";
    $("#wsbtn").setAttribute("aria-label", "Workspace: Global");
    return;
  }
  $("#wsbtn").setAttribute("aria-label", b ? `Workspace: ${b.name}` : "Workspaces");
  $("#wsletter").textContent = b && b.name ? b.name.charAt(0).toUpperCase() : "·";
  $("#wsbtn").title = b ? b.name : "Businesses";
}
async function selectBiz(slug) {
  const b = state.bizs.find(x => x.slug === slug); if (!b) return;
  if (typeof storyView !== 'undefined' && storyView && !await storyCanLeave()) return;
  workspaceNavigation++;
  // the agent this business was last left on wins over its first agent (see agents.js's agKey/agSetSel)
  const lastAgent = ls(`ag:${slug}`);
  state.scope = "business";
  state.biz = slug;
  syncWorkspaceNav();
  if (typeof sgBusinessChanged === "function") sgBusinessChanged();
  state.agent = (lastAgent && b.agents.includes(lastAgent) ? lastAgent : b.agents[0]) || null;
  state.ctx = null; location.hash = slug;
  paintBiz();
  renderTab(); refreshSide();
}
const WORKSPACE_ORDER_KEY = 'workspace-business-order:v1';
const workspaceBusinessId = b => b.id || b.business_id || b.slug;
function workspaceBusinesses() {
  let order;
  try { order = JSON.parse(localStorage.getItem(WORKSPACE_ORDER_KEY) || '[]'); } catch (_) { order = []; }
  if (!Array.isArray(order)) order = [];
  const rank = new Map(order.map((id, i) => [id, i]));
  return [...state.bizs].sort((a, b) => (rank.get(workspaceBusinessId(a)) ?? Infinity) - (rank.get(workspaceBusinessId(b)) ?? Infinity));
}
function moveWorkspaceBusiness(source, target, after) {
  if (source === target) return;
  const businesses = workspaceBusinesses(), item = businesses.find(b => workspaceBusinessId(b) === source);
  const rest = businesses.filter(b => workspaceBusinessId(b) !== source);
  const index = rest.findIndex(b => workspaceBusinessId(b) === target);
  if (!item || index < 0) return;
  rest.splice(index + Number(after), 0, item);
  try { localStorage.setItem(WORKSPACE_ORDER_KEY, JSON.stringify(rest.map(workspaceBusinessId))); }
  catch (_) { toast('Could not save workspace order. Browser storage is unavailable.'); return; }
  openWorkspaceMenu();
}
function openWorkspaceMenu() {
  const businesses = workspaceBusinesses();
  const grip = '<svg viewBox="0 0 16 20" width="14" height="18" fill="currentColor" aria-hidden="true">' + [5,10,15].map(y => `<circle cx="5" cy="${y}" r="1.2"/><circle cx="11" cy="${y}" r="1.2"/>`).join('') + '</svg>';
  const menu = dropdown($('#wsbtn'), [{
    value: '/global', html: `<span class="workspace-choice workspace-global">${globalIcon('globe')}<span class="workspace-name">Global</span></span>`,
  }, ...businesses.map(b => ({
    value: b.slug,
    html: `<span class="workspace-choice"><span class="workspace-business-icon">${esc((b.name || b.slug).charAt(0).toUpperCase())}</span><span class="workspace-name">${esc(b.name)}</span>${window.sgAttentionBadge ? sgAttentionBadge(b.slug) : ""}<span class="workspace-drag" title="Drag to reorder">${grip}</span></span>`,
  }))], slug => slug === '/global' ? selectGlobal() : selectBiz(slug), state.scope === 'global' ? '/global' : state.biz,
    {className: 'workspace-menu', noCheck: true, width: 181});
  let dragged = null;
  const rows = [...menu.el.querySelectorAll('.mi')].slice(1);
  const clear = () => rows.forEach(row => { row.classList.remove('drop-before', 'drop-after', 'dragging'); });
  rows.forEach((row, i) => {
    const id = workspaceBusinessId(businesses[i]);
    row.dataset.businessId = id;
    row.draggable = true;
    row.ondragstart = event => {
      dragged = id; event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', id);
      row.classList.add('dragging');
    };
    row.ondragover = event => {
      if (!dragged || dragged === id) return;
      event.preventDefault(); event.dataTransfer.dropEffect = 'move';
      const after = event.clientY > row.getBoundingClientRect().top + row.offsetHeight / 2;
      rows.forEach(r => r.classList.remove('drop-before', 'drop-after'));
      row.classList.add(after ? 'drop-after' : 'drop-before');
    };
    row.ondrop = event => {
      if (!dragged) return;
      event.preventDefault(); event.stopPropagation();
      const after = event.clientY > row.getBoundingClientRect().top + row.offsetHeight / 2;
      moveWorkspaceBusiness(dragged, id, after); dragged = null; clear();
    };
    row.ondragend = () => { dragged = null; clear(); };
  });
}
$('#wsbtn').onclick = openWorkspaceMenu;
async function goTab(name) {
  if (state.scope === "global") return;
  const contextPage = {stories: "_stories", tables: "_tables"}[name];
  if (contextPage) name = "context";
  const t = [...document.querySelectorAll(".ritem")].find(x => x.dataset.tab === name);
  if (!t || t.classList.contains("soon")) return;
  if (typeof storyView !== 'undefined' && storyView && !await storyCanLeave()) return;
  if (contextPage) state.ctx = contextPage;
  state.tab = t.dataset.tab; ls("tab", state.tab);
  document.querySelectorAll(".ritem").forEach(x => x.classList.toggle("on", x === t));
  await renderTab(); moveInd();
}
document.querySelectorAll(".ritem").forEach(t => t.onclick = () => goTab(t.dataset.tab));
document.querySelectorAll(".ritem").forEach(t => t.classList.toggle("on", t.dataset.tab === state.tab));
moveInd();
window.addEventListener("resize", moveInd);
function moveInd() {
  const on = document.querySelector(".ritem.on:not([hidden])"), ind = $("#rind");
  if (!on || !ind) return;
  ind.hidden = false;
  ind.style.transform = `translateY(${on.offsetTop - ind.offsetTop}px)`;
}

function renderTab() {
  agRenderGen++;
  globalAgentsGeneration++;
  if (typeof rtSuspend === 'function') rtSuspend();
  if (typeof ctxClearGraph === "function") ctxClearGraph();
  if (typeof storyDispose === 'function') storyDispose();
  const c = $("#content"); c.innerHTML = "";
  if (typeof teardownTerm === "function") teardownTerm();
  if (state.scope === "global") return renderGlobal(c);
  if (!state.biz) return;
  return (TABS[state.tab] || TABS.agents)(c);
}
function soon(c, line) {
  const p = document.createElement("div"); p.className = "pane soonpane";
  p.innerHTML = `<div class="t">Coming soon</div><div class="muted small">${esc(line)}</div>`;
  c.appendChild(p);
}
