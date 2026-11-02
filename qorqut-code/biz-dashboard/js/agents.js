// Shared agent navigation helpers: the nav toggle, the new-tab link and the right-click menu.
// Agents are global; businesses have no resident agent page.
const agentNavKey = scope => `agent-nav:${scope || state.biz || 'global'}`;
function agentNavButton() {
  return '<button type="button" class="quiet agent-nav-toggle" data-agent-nav-toggle aria-expanded="true" aria-label="Hide agent navigation">' +
    '<svg class="agent-nav-hide" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M15 9l-3 3 3 3"/></svg>' +
    '<svg class="agent-nav-show" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M12 9l3 3-3 3"/></svg></button>';
}
function wireAgentNavToggle(c, scope) {
  const root = c.querySelector('.agsplit'), button = c.querySelector('[data-agent-nav-toggle]');
  if (!root || !button) return;
  const set = hidden => {
    root.classList.toggle('nav-off', hidden);
    button.setAttribute('aria-expanded', String(!hidden));
    button.setAttribute('aria-label', hidden ? 'Show agent navigation' : 'Hide agent navigation');
    if (typeof agentWorkspaceMenuPlace === 'function') agentWorkspaceMenuPlace(c);
  };
  set(ls(agentNavKey(scope)) === '1');
  button.onclick = () => { const hidden = !root.classList.contains('nav-off'); set(hidden); root._agentPaneDividerRefresh?.(); ls(agentNavKey(scope), hidden ? '1' : '0'); };
}
function agentOpenTab(identity) {
  const url=new URL(location.href);url.searchParams.delete('focus_item');
  url.searchParams.set('agent',identity);url.hash='/global/agents';
  window.open(url.href,'_blank','noopener');
}

// ------------------------------------------------------------------ context menu
// A hand-rolled right-click menu, one at a time: fixed div in document.body, clamped to the
// viewport after mount, dismissed on outside mousedown / another contextmenu / scroll / resize / Escape.
let _cmenuEl = null;
function closeCmenu() {
  if (!_cmenuEl) return;
  const el = _cmenuEl; _cmenuEl = null;
  document.removeEventListener("mousedown", el._onDoc, true);
  document.removeEventListener("contextmenu", el._onDoc, true);
  document.removeEventListener("keydown", el._onKey, true);
  window.removeEventListener("scroll", el._onAway, true);
  window.removeEventListener("resize", el._onAway, true);
  el.remove();
}
function ctxMenu(x, y, items) {
  closeCmenu();
  const el = document.createElement("div");
  el.className = "cmenu";
  el.innerHTML = items.map((it, i) => `<button type="button" class="ci ${it.danger ? "danger" : ""}" data-i="${i}">${it.icon}<span>${esc(it.label)}</span></button>`).join("");
  document.body.appendChild(el);
  _cmenuEl = el;
  el.querySelectorAll(".ci").forEach((b, i) => b.onclick = e => { e.stopPropagation(); closeCmenu(); items[i].onClick(); });
  const r = el.getBoundingClientRect();
  const cx = Math.min(x, window.innerWidth - r.width - 6);
  const cy = Math.min(y, window.innerHeight - r.height - 6);
  el.style.left = Math.max(6, cx) + "px";
  el.style.top = Math.max(6, cy) + "px";
  el._onDoc = e => { if (!el.contains(e.target)) closeCmenu(); };
  el._onKey = e => { if (e.key === "Escape") closeCmenu(); };
  el._onAway = () => closeCmenu();
  document.addEventListener("mousedown", el._onDoc, true);
  document.addEventListener("contextmenu", el._onDoc, true);
  document.addEventListener("keydown", el._onKey, true);
  window.addEventListener("scroll", el._onAway, true);
  window.addEventListener("resize", el._onAway, true);
  return el;
}
const ICO_PIN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>`;
const ICO_PENCIL = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg>`;
const ICO_TRASH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>`;
const ICO_PLUS = `<svg class="ico-plus" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg>`;
