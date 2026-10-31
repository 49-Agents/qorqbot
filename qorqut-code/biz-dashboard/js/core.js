const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ls = (k, v) => { try { return v === undefined ? localStorage.getItem(k) : localStorage.setItem(k, v); } catch (e) { return null; } };
// one scheme for every business
const DEPTS = {
  marketing: { label: "Marketing", icon: "circle", color: "#b5482b" },
  accounting: { label: "Accounting", icon: "square", color: "#4d6b53" },
  "data-science": { label: "Data science", icon: "triangle", color: "#3d5a80" },
  code: { label: "Code", icon: "square", color: "#7a4f7a" },
  general: { label: "General", icon: "circle", color: "#9a968e" },
};
const COLORS = { red: "#b5482b", amber: "#c8912a", green: "#4d6b53", teal: "#3f7f7a", blue: "#3d5a80", plum: "#7a4f7a", ink: "#1c1b19" };
const SHAPES = ["circle", "square", "triangle"];
const TAG_DEPT = { marketing: "marketing", operations: "accounting", research: "data-science", coding: "code" };
const dept = k => DEPTS[k] || DEPTS.general;
const hex = c => COLORS[c] || COLORS.ink;
const glyph = (shape, colorHex, sizePx) => `<span class="g ${SHAPES.includes(shape) ? shape : "circle"}" style="--gs:${sizePx}px;--gc:${colorHex}"></span>`;
const agentGlyph = (m, size) => glyph((m || {}).icon, hex((m || {}).color), size);

// lazy: the render functions live in later script files
const TABS = { agents: c => renderAgents(c), marketing: c => renderAnalytics(c), accounting: c => renderMoney(c), context: c => renderContext(c),
  datascience: c => soon(c, "Experiments and metric deep-dives."),
  progress: c => soon(c, "Tasks, milestones and what shipped."),
  code: c => soon(c, "Repos, commits and deploys.") };
let verbose0 = ls("verbose") === "1", tab0 = ls("tab");
const state = { biz: null, bizs: [], tab: TABS[tab0] ? tab0 : "agents", agent: null, agents: [], liveNow: [], ctx: null, live: null, verbose: verbose0 };
const api = async (p, opt) => { const r = await fetch("/api" + p, opt); return r.json(); };
const post = (p, body) => api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
let toastT; const toast = m => { const t = $("#toast"); t.textContent = m; t.classList.add("show"); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("show"), 2600); };
const fmtMoney = n => (n == null ? "?" : (n < 0 ? "-" : "") + (Math.abs(n) / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const fmtV = c => c == null ? "?" : (Array.isArray(c) ? (c[0] == null ? "?" : (c[1] ? "~" : "") + Number(c[0]).toLocaleString()) : Number(c).toLocaleString());

// ---------------------------------------------------------------- businesses
async function loadBiz() {
  state.bizs = await api("/biz");
  paintBiz();
  if (!state.biz && state.bizs.length) selectBiz(location.hash.slice(1) || state.bizs[0].slug);
}
function paintBiz() {
  const el = $("#bizlist");
  el.innerHTML = state.bizs.map(b => {
    const m = b.agent_meta || {};
    return `<a class="biz ${b.slug === state.biz ? "on" : ""}" href="#${b.slug}" data-slug="${esc(b.slug)}">
      <div class="name">${esc(b.name)}</div><div class="meta">${esc(b.stage || "")} · ${b.agents.length} agent${b.agents.length === 1 ? "" : "s"}</div>
      ${b.agents.length ? `<div class="gr">${b.agents.map(n => agentGlyph(m[n], 8)).join("")}</div>` : ""}</a>`;
  }).join("") || `<div class="empty" style="padding:0 22px">No business repos under the root.</div>`;
  el.querySelectorAll(".biz").forEach(a => a.onclick = e => { e.preventDefault(); selectBiz(a.dataset.slug); });
}
function selectBiz(slug) {
  const b = state.bizs.find(x => x.slug === slug); if (!b) return;
  state.biz = slug; state.agent = b.agents[0] || null; state.ctx = null; location.hash = slug;
  document.querySelectorAll(".biz").forEach(a => a.classList.toggle("on", a.dataset.slug === slug));
  renderTab(); refreshSide();
}
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  state.tab = t.dataset.tab; ls("tab", state.tab);
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("on", x === t)); renderTab();
});
document.querySelectorAll(".tab").forEach(t => t.classList.toggle("on", t.dataset.tab === state.tab));

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
