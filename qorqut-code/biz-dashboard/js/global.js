// Global is an explicit workspace scope, never a synthetic business slug.
// Overview reads summaries for authorized businesses; other global workflows remain a shell.
const GLOBAL_PAGES = {
  overview: { title: 'Overview', icon: 'globe' },
  agents: { title: 'Agents', icon: 'bot', heading: 'Ordubashy agents', description: 'Coordinate work across your businesses with ecosystem-level agents.', detail: 'Ordubashy agents and their workspaces will live here.' },
  signoffs: { title: 'Signoffs', icon: 'check' },
  requests: { title: 'Requests', icon: 'inbox', heading: 'Global requests', description: 'A shared place for requests that belong to the wider ecosystem.', detail: 'Global requests and their review workflow will live here.' },
  evolution: { title: 'Learning & evolution', icon: 'sparkles', heading: 'Learning & evolution', description: 'Turn shared learnings into improvements across the ecosystem.', detail: 'Learning application and evolution workflows will live here.' },
  analytics: { title: 'Analytics', icon: 'chart-column', soon: true },
  brief: { title: 'Brief', icon: 'pen-tool', soon: true },
  routines: { title: 'Routines', icon: 'clock', heading: 'Global routines', description: 'Scheduled work that belongs to the whole ecosystem.', detail: 'Global routine management and run history will live here.' },
};
function globalIcon(name) {
  const extra = { check: '<path d="m5 12 4 4L19 6"/>', inbox: '<path d="M4 4h16l2 10v6H2v-6L4 4Z"/><path d="M2 14h6l2 3h4l2-3h6"/>', clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>' };
  return `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${extra[name] || AICONS[name]}</svg>`;
}
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
  if (global || tView === 'tasks') tSetView('tasks');
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
  syncWorkspaceNav(); renderTab();
}
let globalRender = 0;
function renderGlobal(c) {
  sgGlobal.suspend();
  if (state.globalTab === 'signoffs') {
    c.innerHTML = '<section class="pane global-signoffs"><div id="global-signoff-body"></div></section>';
    sgGlobal.paint(); return;
  }
  if (state.globalTab === 'routines') return renderGlobalRoutines(c);
  const generation = ++globalRender, page = GLOBAL_PAGES[state.globalTab];
  c.innerHTML = `<section class="pane global-content" aria-label="${esc(page.title)}">
    <div class="global-eyebrow">Global</div>
    ${state.globalTab === 'overview' ? `<header class="global-heading"><h1>Your businesses</h1><p>What needs your attention, in one place.</p></header>
      <div class="global-grid">${state.bizs.map(b => `<article class="global-business" data-business="${esc(b.slug)}">
        <button class="global-business-head" data-open-business="${esc(b.slug)}"><span class="global-business-letter">${esc((b.name || b.slug).charAt(0).toUpperCase())}</span><span><h2>${esc(b.name || b.slug)}</h2></span><span class="global-arrow" aria-hidden="true">↗</span></button>
        <div class="global-summary" aria-live="polite"><p class="global-summary-note">Loading summary…</p></div>
      </article>`).join('') || '<p class="muted">No businesses yet.</p>'}</div>` : `<header class="global-heading"><h1>${page.heading}</h1><p>${page.description}</p></header><div class="global-placeholder"><span class="global-placeholder-icon">${globalIcon(page.icon)}</span><h2>A home for ${state.globalTab === 'agents' ? 'Ordubashy' : page.title.toLowerCase()}</h2><p>${page.detail}</p><span class="global-status">Coming soon</span></div>`}
    </section>`;
  c.querySelectorAll('[data-open-business]').forEach(el => el.onclick = () => selectBiz(el.dataset.openBusiness));
  if (state.globalTab === 'overview') loadGlobalSummaries(c, generation);
}
function renderGlobalRoutines(c) {
  c.innerHTML = '<section class="pane global-routines"><div class="global-eyebrow">Global</div><div id="global-routine-body"></div></section>';
  tPaintRoutines();
}
const sgGlobal = sgCreateView({
  global: true,
  workspace: () => state.scope === 'global' ? 'scope:global' : null,
  active: () => state.scope === 'global' && state.globalTab === 'signoffs',
  body: () => $('#global-signoff-body'),
  prefix: 'global-',
});
setInterval(() => { if (state.scope === 'global' && state.globalTab === 'signoffs') sgGlobal.refresh(); }, 12000);
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
    const metric = (label, count, view, hint = '') => `<button class="global-metric" data-summary-view="${view}" ${view === 'questions' ? 'aria-disabled="true"' : ''} aria-label="${esc(label)}: ${count == null ? 'unavailable' : count}" title="${esc(hint || (count == null ? 'Summary unavailable' : label))}"><strong class="${count === 0 ? 'zero' : ''}">${count == null ? '—' : count}</strong><span class="global-metric-icon">${view === 'requests' ? globalIcon('inbox') : T_VIEW_ICON[view]}</span></button>`;
    const logs = logResult.status === 'fulfilled' && Array.isArray(logResult.value) ? logResult.value : null;
    $('.global-summary', card).innerHTML = `<div class="global-metrics">${metric('Tasks', mine?.length, 'tasks', 'Open tasks assigned to you')}${metric('Signoffs', pending, 'signoffs')}${metric('Requests', requests, 'requests')}</div>
      ${globalActivityChart(logs)}
      <details class="global-details"><summary aria-label="Details for ${esc(b.name)}" title="Details">···</summary><div class="global-detail-panel">
        ${b.one_liner ? `<p class="global-business-description">${esc(b.one_liner)}</p>` : ''}
        <p class="global-detail-label">Tasks for you</p>
        ${mine?.length ? `<ul class="global-task-preview">${mine.slice(0, 3).map(t => `<li>${esc(t.title)}</li>`).join('')}</ul>${mine.length > 3 ? `<p class="global-summary-note">+${mine.length - 3} more</p>` : ''}` : `<p class="global-summary-note">${mine ? 'No open tasks assigned to you.' : 'Task summary unavailable.'}</p>`}
        <p class="global-summary-note">Questions for you · Coming soon</p>
        ${mine == null || pending == null || requests == null ? '<p class="global-summary-note">Some summaries are unavailable.</p>' : ''}
      </div></details>`;
    const details = $('.global-details', card);
    card.onpointerenter = event => { if (event.pointerType === 'mouse') details.open = true; };
    card.onpointerleave = () => { if (!details.contains(document.activeElement)) details.open = false; };
    details.addEventListener('focusout', event => { if (!card.matches(':hover') && !details.contains(event.relatedTarget)) details.open = false; });
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
  const days = globalActivityDays(rows), total = days.reduce((sum, d) => sum + d.count, 0), max = Math.max(20, Math.ceil(Math.max(...days.map(d => d.count)) / 10) * 10);
  const points = days.map((d, i) => ({x: 22 + i * 96 / (days.length - 1), y: 30 - d.count / max * 26}));
  const label = `Daily actions, last 14 days (UTC), scale 0–${max}: ${days.map(d => `${d.date}: ${d.count}`).join(', ')}`;
  return `<div class="global-activity"><span><strong>${total}</strong> actions</span>
    <svg class="global-spark" viewBox="0 0 122 36" role="img" aria-label="${esc(label)}" data-y-max="${max}">
      <text x="16" y="8" text-anchor="end">${max}</text><text x="16" y="32" text-anchor="end">0</text>
      <path class="global-spark-grid" d="M22 4H118M22 30H118"/>
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

document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && state.scope === 'global') {
    document.querySelectorAll('.global-details[open]').forEach(details => { details.open = false; });
  }
});
