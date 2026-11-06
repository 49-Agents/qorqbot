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
  if (state.globalTab === 'costs') return renderGlobalCosts(c);
  if (state.globalTab === 'tables') return renderTables(c);
  return renderGlobalAgents(c);
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
