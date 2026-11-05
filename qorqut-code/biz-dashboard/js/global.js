// Global is an explicit workspace scope, never a synthetic business slug.
// Agents use registered root workspaces. Charts live in the shared drawer (js/overview-drawer.js).
// Route metadata only: there is no main menu or rail. Costs renders as a page solely inside embedded Business management; elsewhere it is a
// shared-drawer tab. Context and Calendar are agent-pane views (AGENT_PANE_DESTINATIONS); Tables is a drawer tab. Settings is opened from
// the Agents navigation footer (below Terminal); #/global/settings stays a valid route.
const GLOBAL_PAGES = {
  agents: { title: 'Agents' },
  costs: { title: 'Costs' },
  context: { title: 'Context', pane: true },
  tables: { title: 'Tables', pane: true },
  calendar: { title: 'Calendar', pane: true },
  settings: { title: 'Settings' },
  projects: { title: 'Projects' },
};
// Reuse the actual business navigation and drawer glyphs, including placeholder pages.
const GLOBAL_ICONS = {
  fleet: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${AICONS['share-2']}</svg>`,
  learning: SVG_BRAIN,
  notes: T_VIEW_ICON.notes,
  globe: LN_SCOPE_BY_KEY.global.ico,
  agents: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/></svg>',
  costs: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${AICONS.calculator}</svg>`,
  calendar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 11h18"/></svg>',
  tables: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${AICONS.database}</svg>`,
  signoffs: T_VIEW_ICON.signoffs,
  analytics: NAV_PROGRESS_ICON,
  routines: T_VIEW_ICON.routines,
  settings: NAV_SETTINGS_ICON,
};
function globalIcon(name) { return GLOBAL_ICONS[name]; }
// Top-level pages other than Agents carry one local way back; there is no global navigation menu.
function syncWorkspaceNav() {
  $('#workspace-back').hidden = embeddedAgentPane || state.scope === 'global' && state.globalTab === 'agents';
  $('#toasts').hidden = false;
}
async function selectGlobal(tab = ls('global-tab') || 'agents') {
  const navigation = ++workspaceNavigation;
  // Preserve edits before removing the business's editor and its autosave timers.
  if (state.scope !== 'global') {
    if (typeof agentWorkspaceCanLeave === 'function' && !await agentWorkspaceCanLeave(_agpad)) return;
    if (!await ntFlush()) return;
    if (_jState && (_jState.dirty || _jState.saving)) {
      await jSave();
      if (_jState.dirty || _jState.saving) return;
    }
    if (navigation !== workspaceNavigation) return;
    jClearTimers(); clearTimeout(_ctxTimer);
    if (rtDialog) { rtDialog.close(); rtDialog = null; }
    state.scope = 'global'; state.biz = null; state.agent = null; state.ctx = null;
    clearEtagCache();
    tLastBiz = null; tGen++; rtGeneration++; rtBiz = null;
    state.agents = []; state.tasks = [];
    if (typeof sgBusinessChanged === 'function') sgBusinessChanged();
  }
  return goGlobalTab(tab);
}
async function goGlobalTab(tab) {
  // An embedded page opens an agent in the surrounding app; it never navigates its own frame there.
  if (embeddedAgentPane && tab === 'agents') return window.parent.agentWorkspaceEscape?.({agent: ls('ag:scope:global')});
  if (state.scope !== 'global') return;
  if (typeof agentWorkspaceCanLeave === 'function' && !await agentWorkspaceCanLeave(_agpad)) return;
  // The old standalone Context destination opens the owning agent's pane.
  const pane = agentWorkspaceLegacyDestination(tab, location.hash);
  if (pane) {
    if (!await agentWorkspaceRedirect(pane)) return;
    tab = 'agents';
  }
  if (tab === 'learning') {
    location.hash = '/global/context/learning';
    tab = 'context';
  }
  // Retired drawer destinations no longer expose their management screens.
  if (!embeddedAgentPane && ['routines', 'signoffs', 'costs', 'tables'].includes(tab)) tab = 'agents';
  const navigation=++workspaceNavigation;
  if(['notes', 'globalNotes', 'businessNotes'].includes(tView) && !await ntFlush())return;
  if(navigation!==workspaceNavigation)return;
  state.globalTab = Object.hasOwn(GLOBAL_PAGES, tab) ? tab : 'agents';
  ls('global-tab', state.globalTab);
  if (!(state.globalTab === "context" && location.hash.startsWith('#/global/context/')))
    location.hash = `/global/${state.globalTab}`;
  renderedGlobalHash = location.hash;
  ls('workspace-route', location.hash);
  await renderTab(); syncWorkspaceNav();

}
function renderGlobalContext(c) {
  const route = location.hash.split('/')[3] || 'graph';
  const selected = ['graph', 'people', 'object', 'learning', 'library'].includes(route) ? route : 'graph';
  const navSelected = selected === 'object' ? 'graph' : selected;
  const contextBase = ['graph', 'people', 'object'].includes(selected);
  c.innerHTML = `<div class="ctxwrap global-context"><div class="split ctxsplit ${selected === 'graph' ? 'global-context-graph-route' : ''}">
    <nav class="listcol ctxtree" aria-label="Global Context"><div class="crows">
      ${[['graph','Graph',GLOBAL_ICONS.fleet],['people','People',SVG_PEOPLE],['learning','Learning',SVG_BRAIN],['library','Filed context',GLOBAL_ICONS.notes]].map(([key,label,icon]) => `<button type="button" class="item bt ${navSelected === key ? 'on' : ''}" data-global-context="${key}" aria-label="${label}" ${navSelected === key ? 'aria-current="page"' : ''}><div class="nm">${icon}<span class="tt">${label}</span></div></button>`).join('')}

    </div></nav><div class="pane ctxview ${contextBase ? `global-context-base-view${selected === 'graph' ? ' global-context-graph-view' : ''}` : 'global-learning'}" id="ctxview"></div></div></div>`;
  c.querySelectorAll('[data-global-context]').forEach(button => button.onclick = () => {
    routeTo('/global/context/' + button.dataset.globalContext);
  });
  if (selected === 'library') return renderGlobalLibrary($('#ctxview', c));
  if (contextBase) return renderGlobalContextBase($('#ctxview', c), selected);
  return openLearning();
}

// Global library: registered Global source and context documents. Source kinds are labelled by what they are,
// never by their filing outcome; the filing state is shown from each document's recorded `filing`.
const GLOBAL_LIBRARY_KINDS = [['context_note', 'Materials'], ['context_topic', 'Topics'], ['context_person', 'People'], ['context_upload', 'Uploads'], ['context_dump', 'Dumps']];
let globalLibraryKind = 'context_note';
function renderGlobalLibrary(c) {
  const hash = location.hash, scope = {kind: 'root'};
  const live = () => c.isConnected && state.scope === 'global' && state.globalTab === 'context' && location.hash === hash;
  let id = '';
  try { id = decodeURIComponent(hash.split('/')[4] || ''); } catch (_) { id = '!'; }
  if (id) {
    return srcMountSource(c, scope, id, {live, crumbs: `<div class="crumbs"><a href="#/global/context/library">Filed context</a><span class="sep">›</span><span class="cur">Source</span></div>`});
  }
  c.innerHTML = `<div class="global-library"><div class="th"><h2>Filed context</h2><span class="sp"></span>
    <select class="inp" aria-label="Context type">${GLOBAL_LIBRARY_KINDS.map(([kind, label]) => `<option value="${kind}" ${kind === globalLibraryKind ? 'selected' : ''}>${label}</option>`).join('')}</select></div>
    <div class="global-library-list"></div></div>`;
  const select = c.querySelector('select'), host = c.querySelector('.global-library-list');
  const mount = () => {
    globalLibraryKind = select.value;
    const label = GLOBAL_LIBRARY_KINDS.find(([kind]) => kind === select.value)[1];
    srcMountList(host, scope, [{kind: select.value, label}], {live, open: docId => { routeTo(`/global/context/library/${encodeURIComponent(docId)}`); }});
  };
  select.onchange = mount;
  mount();
}
function renderGlobal(c) {
  if (!embeddedAgentPane && GLOBAL_PAGES[state.globalTab]?.pane) return agentWorkspaceUnavailable(c, state.globalTab);
  if (state.globalTab === "context") return renderGlobalContext(c);
  if (state.globalTab === 'settings') return renderSettings(c);
  if (state.globalTab === 'projects') return renderProjects(c);
  if (state.globalTab === 'costs') return renderGlobalCosts(c);
  if (state.globalTab === 'tables') return renderTables(c);
  return renderGlobalAgents(c);
}
// Projects: what a project is, every project the owner has (with its summary), archived ones, and how to start a new one.
// There is no create form on purpose: Ordubashy prepares a project and asks for the owner's Signoff (project-creation skill).
function renderProjects(c) {
  const controller = new AbortController();
  const host = document.createElement('div');
  c.appendChild(host);
  const watch = new MutationObserver(() => { if (!host.isConnected) { controller.abort(); watch.disconnect(); } });
  watch.observe(c.ownerDocument.body, {childList: true, subtree: true});
  const mount = {active: () => host.isConnected && !controller.signal.aborted, signal: controller.signal};
  const paint = () => {
    globalOverviewMount(host, mount, paint);
    const pane = host.querySelector('.global-overview');
    pane.querySelector('.global-heading').outerHTML = `<header class="projects-intro"><div><h1>Projects</h1>
      <p>A project keeps the work for one client, business or goal in its own place: its tasks, records, tables and context, apart from
        your other projects. Ordubashy coordinates the work across all of them.</p></div>
      <button type="button" data-new-project>New project</button></header>
      <p class="projects-how"><b>How to start one:</b> ask Ordubashy in Chat, for example “Create a project for Acme’s website redesign”.
        He prepares it and asks for your approval before anything is created. Open a project below to see its tasks.</p>`;
    pane.querySelector('[data-new-project]').onclick = projectsStartNew;
    // The projects first; the activity chart for all of them below.
    const grid = pane.querySelector('.global-grid'), chart = pane.querySelector('#global-progress');
    if (grid && chart) {
      chart.before(grid);
      grid.insertAdjacentHTML('beforebegin', '<h2 class="projects-section">Your projects</h2>');
      chart.insertAdjacentHTML('beforebegin', '<h2 class="projects-section">Activity across your projects</h2>');
    }
    const empty = pane.querySelector('[data-no-projects]');
    if (empty) empty.textContent = 'No projects yet. Press New project to ask Ordubashy for your first one.';
  };
  paint();
}
// Opens Ordubashy's Chat with the request started, so the owner only adds the name and purpose.
async function projectsStartNew() {
  const lead = globalAgentProfiles.find(agent => (agent.role?.id || agent.role_id) === 'ordubashy');
  await selectGlobal('agents');
  if (!lead || typeof globalAgentsNavigate !== 'function' || !await globalAgentsNavigate(lead.agent_id, {chat: true})) return;
  // Wait until Chat has read his session: while it loads, the message box is disabled whatever his state.
  for (let i = 0; i < 80; i++) {
    const box = document.querySelector('#global-agent-terminal .gchat-composer textarea');
    const state = document.querySelector('#global-agent-terminal .gchat-session')?.dataset.state;
    if (box && state && state !== 'loading') {
      if (box.disabled) { toast('Start Ordubashy first, then ask him to create the project.'); return; }
      // An unsent draft is never replaced: it stays exactly as typed.
      if (box.value !== '') {
        box.focus();
        toast('Your draft was kept. Ask Ordubashy to create a project there.');
        return;
      }
      box.value = 'Create a project for ';
      box.dispatchEvent(new Event('input', {bubbles: true}));
      box.focus(); box.setSelectionRange(box.value.length, box.value.length);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
}
// Charts in the shared drawer: the activity graph, one summary card per authorized project and the owner's archived projects.
// `host` carries the mount's abort signal and visibility check; `restored` repaints after an archived project returns.
let globalRender = 0;
function globalOverviewMount(c, host, restored) {
  const generation = ++globalRender, current = () => host.active() && generation === globalRender;
  c.innerHTML = `<section class="pane global-content global-overview" aria-label="Charts">
    <header class="global-heading"><h1>Your projects</h1></header>
    <div id="global-progress"></div><div class="global-grid">${state.bizs.map(b => `<article class="global-business" data-business="${esc(b.slug)}">
      <button class="global-business-head" data-open-business="${esc(b.slug)}"><span class="global-business-letter">${esc((b.name || b.slug).charAt(0).toUpperCase())}</span><span class="global-business-name"><h2>${esc(b.name || b.slug)}</h2></span><span class="global-action-count" aria-label="Actions loading">${T_VIEW_ICON.activity}<strong>—</strong></span></button>
      <div class="global-summary" aria-live="polite"><p class="global-summary-note">Loading summary…</p></div>
    </article>`).join('') || '<p class="muted" data-no-projects>No projects yet. Ask Ordubashy to create one.</p>'}</div><section class="global-archived" id="global-archived" aria-label="Archived projects" hidden></section>
  </section>`;
  c.querySelectorAll('[data-open-business]').forEach(el => el.onclick = () => selectBiz(el.dataset.openBusiness));
  globalProgressMount($('#global-progress', c), {signal: host.signal, active: current});
  loadGlobalSummaries(c, current, host.signal);
  if (typeof businessRestoreMount === 'function') businessRestoreMount($('#global-archived', c), restored);
}
let globalSummaryCache = null;
async function loadGlobalSummaries(c, current, signal) {
  if (globalSummaryCache) paintGlobalSummaries(c, globalSummaryCache);
  try {
    const data = coreData(await pageReadPost('/root/operations/dashboard_summary_get', {}, signal));
    if (!current()) return;
    if (!Array.isArray(data.items)) throw new Error('Summary unavailable');
    sjConfigure(data);
    globalSummaryCache = data.items; paintGlobalSummaries(c, data.items);
  } catch (error) {
    if (!current()) return;
    c.querySelectorAll('.global-summary-note').forEach(el => { el.textContent = 'Summary unavailable'; });
  }
}
function paintGlobalSummaries(c, rows) {
  for (const b of rows) {
    const card = [...c.querySelectorAll('[data-business]')].find(el => el.dataset.business === b.slug);
    if (!card) continue;
    const metric = (label, count, view) => `<button class="global-metric" data-summary-view="${view}" aria-label="${label}: ${count}"><strong class="${count === 0 ? 'zero' : ''}">${count}</strong><span class="global-metric-icon">${T_VIEW_ICON[view]}</span></button>`;
    $('.global-summary', card).innerHTML = `<div class="global-metrics">${metric('Tasks',b.tasks,'tasks')}${metric('Signoffs',b.signoffs,'signoffs')}</div>${globalActivityChart(b.activity, true)}`;
    const total = b.activity.reduce((sum, day) => sum + day.count, 0), actionCount = $('.global-action-count',card);
    actionCount.querySelector('strong').textContent = total;
    actionCount.setAttribute('aria-label', `${total} actions in the last 14 days`);
    card.querySelectorAll('[data-summary-view]').forEach(el => el.onclick = async () => {
      const view = el.dataset.summaryView;
      await selectBiz(b.slug);
      if (state.biz !== b.slug || state.scope !== 'business') return;
      if(view==='signoffs')sgAttentionNavigate(b.slug);
      else goTab('tasks');
    });
  }
}
function globalActivityDays(rows, now = new Date()) {
  const today = sjToday(now);
  const days = Array.from({length: 14}, (_, i) => ({date: sjAddDays(today, i - 13), count: 0}));
  const byDay = new Map(days.map(day => [day.date, day]));
  for (const row of rows) {
    const timestamp = Date.parse(row.ts || row.created_at);
    if (!Number.isFinite(timestamp) || timestamp > now.getTime()) continue;
    const day = byDay.get(sjDate(timestamp));
    if (day) day.count++;
  }
  return days;
}
function globalActivityChart(rows, aggregated = false) {
  if (!rows) return '<div class="global-activity"><span>Activity unavailable</span></div>';
  const days = aggregated ? rows : globalActivityDays(rows), max = Math.max(20, Math.ceil(Math.max(...days.map(d => d.count)) / 10) * 10);
  const points = days.map((d, i) => ({x: 22 + i * 218 / (days.length - 1), y: 64 - d.count / max * 60}));
  const label = `Daily actions, last 14 days (${SJ_LABEL}), scale 0–${max}: ${days.map(d => `${d.date}: ${d.count}`).join(', ')}`;
  return `<div class="global-activity">
    <svg class="global-spark" viewBox="0 0 244 70" preserveAspectRatio="none" role="img" aria-label="${esc(label)}" data-y-max="${max}">
      <text x="16" y="8" text-anchor="end">${max}</text><text x="16" y="66" text-anchor="end">0</text>
      <path class="global-spark-grid" d="M22 4H240M22 64H240"/>
      <polyline class="global-spark-line" points="${points.map(p => `${p.x},${p.y}`).join(' ')}"/>
      ${days.map((d, i) => `<circle class="global-spark-point" cx="${points[i].x}" cy="${points[i].y}" r="2"><title>${d.date} · ${d.count} action${d.count === 1 ? '' : 's'} (${SJ_LABEL})</title></circle>`).join('')}
    </svg></div>`;
}
let renderedGlobalHash = '';
window.addEventListener('hashchange', () => {
  const legacy = ovLegacy(location.hash);
  if (legacy !== undefined) { ovOpenLegacy(legacy); return; }
  if (location.hash.startsWith('#/global') || state.bizs.some(b => '#' + b.slug === location.hash))
    ls('workspace-route', location.hash);
  if (location.hash.startsWith('#/global')) {
    const tab = location.hash.split('/')[2] || 'agents';
    if (state.scope !== 'global' || state.globalTab !== tab || (!embeddedAgentPane && AGENT_PANE_DESTINATIONS[tab])) selectGlobal(tab);
    else if (['overview','context'].includes(tab) && renderedGlobalHash !== location.hash) { renderedGlobalHash = location.hash; renderTab(); }
  } else {
    const slug = location.hash.slice(1);
    if (slug !== state.biz && state.bizs.some(b => b.slug === slug)) selectBiz(slug);
  }
});
