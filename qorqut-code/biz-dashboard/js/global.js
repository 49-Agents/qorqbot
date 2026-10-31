// Global is an explicit workspace scope, never a synthetic business slug.
// Overview reads summaries for authorized businesses; agents use registered root workspaces.
const GLOBAL_PAGES = {
  overview: { title: 'Overview', icon: 'globe' },
  agents: { title: 'Agents', icon: 'agents' },
  settings: { title: 'Settings', icon: 'settings' },
  analytics: { title: 'Analytics', icon: 'analytics', soon: true },
};
// Reuse the actual business navigation and drawer glyphs, including placeholder pages.
const GLOBAL_ICONS = {
  globe: LN_SCOPE_BY_KEY.global.ico,
  agents: document.querySelector('.ritem[data-tab="agents"] svg').outerHTML,
  signoffs: T_VIEW_ICON.signoffs,
  requests: MK_ICO.queue,
  analytics: document.querySelector('.ritem[data-tab="progress"] svg').outerHTML,
  routines: T_VIEW_ICON.routines,
  settings: document.querySelector('.ritem[data-tab="settings"] svg').outerHTML,
};
function globalIcon(name) { return GLOBAL_ICONS[name]; }
for (const [id, page] of Object.entries(GLOBAL_PAGES)) {
  const button = document.createElement('button');
  button.className = 'ritem global-rail'; button.type = 'button'; button.hidden = true;
  button.dataset.globalTab = id;
  button.title = page.title + (page.soon ? ' — Coming soon' : '');
  button.setAttribute('aria-label', button.title);
  if (page.soon) { button.classList.add('soon'); button.setAttribute('aria-disabled', 'true'); }
  button.innerHTML = globalIcon(page.icon); button.onclick = () => goGlobalTab(id);
  $('#ritems').appendChild(button);
}
function syncWorkspaceNav() {
  const global = state.scope === 'global';
  document.querySelectorAll('.ritem[data-tab]').forEach(el => { el.hidden = global; });
  document.querySelectorAll('.global-rail').forEach(el => {
    el.hidden = !global;
    const selected = global && el.dataset.globalTab === state.globalTab;
    el.classList.toggle('on', selected);
    if (selected) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current');
  });
  $('#taskswrap').hidden = false;
  tSetView(tView);
  tPaintSig = null; paintTasks();
  $('#toasts').hidden = false;
  if (global) {
    $('#foot').innerHTML = ''; $('#foot').title = '';
  }
  paintBiz(); moveInd();
}
async function selectGlobal(tab = ls('global-tab') || 'overview') {
  const navigation = ++workspaceNavigation;
  // Preserve edits before removing the business's editor and its autosave timers.
  if (state.scope !== 'global') {
    if (typeof storyView !== 'undefined' && storyView && !await storyCanLeave()) return;
    if (!await ntFlush()) return;
    if (_jState && (_jState.dirty || _jState.saving)) {
      await jSave();
      if (_jState.dirty || _jState.saving) return;
    }
    if (navigation !== workspaceNavigation) return;
    jClearTimers(); clearTimeout(_ctxTimer);
    if (rtDialog) { rtDialog.close(); rtDialog = null; }
    state.scope = 'global'; state.biz = null; state.agent = null; state.ctx = null;
    tLastBiz = null; tGen++; rtGeneration++; rtBiz = null;
    state.agents = []; state.tasks = []; state.live = null; state.liveNow = [];
    if (typeof sgBusinessChanged === 'function') sgBusinessChanged();
  }
  goGlobalTab(GLOBAL_PAGES[tab]?.soon ? 'overview' : tab);
}
function goGlobalTab(tab) {
  if (state.scope !== 'global') return;
  if (GLOBAL_PAGES[tab]?.soon) return;
  state.globalTab = Object.hasOwn(GLOBAL_PAGES, tab) ? tab : 'overview';
  ls('global-tab', state.globalTab);
  location.hash = `/global/${state.globalTab}`;
  renderTab(); syncWorkspaceNav();
  // Old page links now open the corresponding drawer tab.
  if (['routines', 'signoffs'].includes(tab)) { openTasks(true); tSetView(tab); }
}
let globalRender = 0;
function renderGlobal(c) {
  if (state.globalTab === 'settings') return renderSettings(c);
  if (state.globalTab === 'agents') return renderGlobalAgents(c);
  const generation = ++globalRender, page = GLOBAL_PAGES[state.globalTab];
  c.innerHTML = `<section class="pane global-content" aria-label="${esc(page.title)}">
    <div class="global-eyebrow">Global</div>
    ${state.globalTab === 'overview' ? `<header class="global-heading"><h1>Your businesses</h1><p>What needs your attention, in one place.</p></header>
      <div class="global-grid">${state.bizs.map(b => `<article class="global-business" data-business="${esc(b.slug)}">
        <button class="global-business-head" data-open-business="${esc(b.slug)}"><span class="global-business-letter">${esc((b.name || b.slug).charAt(0).toUpperCase())}</span><span class="global-business-name"><h2>${esc(b.name || b.slug)}</h2></span><span class="global-action-count" aria-label="Actions loading">${T_VIEW_ICON.activity}<strong>—</strong></span></button>
        <div class="global-summary" aria-live="polite"><p class="global-summary-note">Loading summary…</p></div>
      </article>`).join('') || '<p class="muted">No businesses yet.</p>'}</div>` : `<header class="global-heading"><h1>${page.heading}</h1><p>${page.description}</p></header><div class="global-placeholder"><span class="global-placeholder-icon">${globalIcon(page.icon)}</span><h2>A home for ${page.title.toLowerCase()}</h2><p>${page.detail}</p><span class="global-status">Coming soon</span></div>`}
    </section>`;
  c.querySelectorAll('[data-open-business]').forEach(el => el.onclick = () => selectBiz(el.dataset.openBusiness));
  if (state.globalTab === 'overview') loadGlobalSummaries(c, generation);
}
async function loadGlobalSummaries(c, generation) {
  const current = () => state.scope === 'global' && state.globalTab === 'overview' && generation === globalRender;
  let ownerId;
  try { ownerId = coreData(await api('/session')).principal_id; } catch (_) { /* Unavailable owner identity is shown as unknown, never zero. */ }
  if (!current()) return;
  await Promise.all(state.bizs.map(async b => {
    const slug = encodeURIComponent(b.slug);
    const [tasksResult, liveResult, logResult] = await Promise.allSettled([api(`/biz/${slug}/tasks`), api(`/biz/${slug}/live`), api(`/biz/${slug}/log?n=0`)]);
    if (!current()) return;
    const tasks = tasksResult.status === 'fulfilled' && Array.isArray(tasksResult.value) ? tasksResult.value : null;
    const live = liveResult.status === 'fulfilled' && liveResult.value && !liveResult.value.error ? liveResult.value : null;
    const mine = tasks && ownerId ? tasks.filter(t => t.assignee_id === ownerId && !['done', 'dropped'].includes(t.status)) : null;
    const pending = live && Number.isInteger(live.signoffs_pending) ? live.signoffs_pending : null;
    const requests = live && !live.requests_error && Array.isArray(live.requests) ? live.requests.filter(r => r.state === 'submitted').length : null;
    const card = [...c.querySelectorAll('[data-business]')].find(el => el.dataset.business === b.slug);
    if (!card) return;
    const metric = (label, count, view, hint = '') => `<button class="global-metric" data-summary-view="${view}" ${view === 'questions' ? 'aria-disabled="true"' : ''} aria-label="${esc(label)}: ${count == null ? 'unavailable' : count}" title="${esc(hint || (count == null ? 'Summary unavailable' : label))}"><strong class="${count === 0 ? 'zero' : ''}">${count == null ? '—' : count}</strong><span class="global-metric-icon">${view === 'requests' ? globalIcon('requests') : T_VIEW_ICON[view]}</span></button>`;
    const logs = logResult.status === 'fulfilled' && Array.isArray(logResult.value) ? logResult.value : null;
    $('.global-summary', card).innerHTML = `<div class="global-metrics">${metric('Tasks', mine?.length, 'tasks', 'Open tasks assigned to you')}${metric('Signoffs', pending, 'signoffs')}${metric('Requests', requests, 'requests')}</div>
      ${globalActivityChart(logs)}`;
    const total = logs ? globalActivityDays(logs).reduce((sum, day) => sum + day.count, 0) : null;
    const actionCount = $('.global-action-count', card);
    actionCount.querySelector('strong').textContent = total == null ? '—' : total;
    actionCount.setAttribute('aria-label', total == null ? 'Actions unavailable' : `${total} actions in the last 14 days`);
    card.querySelectorAll('[data-summary-view]').forEach(el => el.onclick = async () => {
      const view = el.dataset.summaryView;
      if (view === 'questions') return;
      await selectBiz(b.slug);
      if (state.biz !== b.slug || state.scope !== 'business') return;
      if (view === 'requests') goTab('marketing');
      else { openTasks(true); tSetView(view); }
    });
  }));
}
// Fourteen UTC calendar days, including today. n=0 reads the full journal so busy
// days cannot be silently undercounted by the ordinary recent-activity limit.
function globalActivityDays(rows, now = new Date()) {
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const days = Array.from({length: 14}, (_, i) => ({date: new Date(today - (13 - i) * 86400000).toISOString().slice(0, 10), count: 0}));
  const byDay = new Map(days.map(day => [day.date, day]));
  for (const row of rows) {
    const timestamp = Date.parse(row.ts || row.created_at);
    if (!Number.isFinite(timestamp) || timestamp > now.getTime()) continue;
    const day = byDay.get(new Date(timestamp).toISOString().slice(0, 10));
    if (day) day.count++;
  }
  return days;
}
function globalActivityChart(rows) {
  if (!rows) return '<div class="global-activity"><span>Activity unavailable</span></div>';
  const days = globalActivityDays(rows), max = Math.max(20, Math.ceil(Math.max(...days.map(d => d.count)) / 10) * 10);
  const points = days.map((d, i) => ({x: 22 + i * 218 / (days.length - 1), y: 64 - d.count / max * 60}));
  const label = `Daily actions, last 14 days (UTC), scale 0–${max}: ${days.map(d => `${d.date}: ${d.count}`).join(', ')}`;
  return `<div class="global-activity">
    <svg class="global-spark" viewBox="0 0 244 70" preserveAspectRatio="none" role="img" aria-label="${esc(label)}" data-y-max="${max}">
      <text x="16" y="8" text-anchor="end">${max}</text><text x="16" y="66" text-anchor="end">0</text>
      <path class="global-spark-grid" d="M22 4H240M22 64H240"/>
      <polyline class="global-spark-line" points="${points.map(p => `${p.x},${p.y}`).join(' ')}"/>
      ${days.map((d, i) => `<circle class="global-spark-point" cx="${points[i].x}" cy="${points[i].y}" r="2"><title>${d.date} · ${d.count} action${d.count === 1 ? '' : 's'} (UTC)</title></circle>`).join('')}
    </svg></div>`;
}
window.addEventListener('hashchange', () => {
  if (location.hash.startsWith('#/global')) {
    const tab = location.hash.split('/')[2] || 'overview';
    if (state.scope !== 'global' || state.globalTab !== tab) selectGlobal(tab);
  } else {
    const slug = location.hash.slice(1);
    if (slug !== state.biz && state.bizs.some(b => b.slug === slug)) selectBiz(slug);
  }
});
