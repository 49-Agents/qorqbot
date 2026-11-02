// One document hosts the native screens. Navigation reuses it, while its page
// state stays separate from the selected agent, transcript and chat draft.
const AGENT_WORKSPACE_BIZ_KEY = 'agent-workspace:business:v1';
const AGENT_WORKSPACE_SECTION_KEY = 'agent-workspace:section:v1';
const AGENT_WORKSPACE_SECTIONS = [
  ['tasks', 'Tasks'], ['progress', 'Progress'], ['tables', 'Tables'],
];
const AGENT_WORKSPACE_GLOBAL_SECTIONS = [
  ['overview', 'Overview'], ['costs', 'Costs'], ['context', 'Context'], ['tables', 'Tables'],
];
// The embedded page is showing Tables: the Tables section of Business management (or the retired standalone view key).
const embeddedTablesView = () => {
  const params = new URLSearchParams(location.search), view = params.get('agent-pane-embed');
  return view === 'tables' || (view === 'business' && params.get('section') === 'tables');
};
const agentWorkspaceEmbedded = () => new URLSearchParams(location.search).has('agent-pane-embed');
const agentWorkspaceSections = business => business === 'global' ? AGENT_WORKSPACE_GLOBAL_SECTIONS : AGENT_WORKSPACE_SECTIONS;
const agentWorkspaceSectionKey = business => AGENT_WORKSPACE_SECTION_KEY + (business === 'global' ? ':global' : '');
// The sections a pane offers: a business has no Tables section here (the shared drawer's Tables tab covers it). An embedded
// page validates against the full list, since the pane that frames it has already chosen.
const agentWorkspaceOffered = (business, role) => agentWorkspaceSections(business).filter(([key]) => !(business !== 'global' && key === 'tables'));
const agentWorkspaceSection = (business, section = ls(agentWorkspaceSectionKey(business)), role = null) => {
  const sections = role ? agentWorkspaceOffered(business, role) : agentWorkspaceSections(business);
  return sections.some(([key]) => key === section) ? section : sections[0][0];
};
const agentWorkspaceSectionIcon = key => ({overview: GLOBAL_ICONS.globe, costs: GLOBAL_ICONS.costs,
  context: SVG_BRAIN, tables: GLOBAL_ICONS.tables,
  progress: GLOBAL_ICONS.analytics, tasks: T_VIEW_ICON.tasks})[key];

// Copy only the shared directory and owner session, never the parent's mutable
// navigation or editor state. The child still uses native authenticated reads.
function agentWorkspaceSnapshot() { return structuredClone({bizs: state.bizs, session: state.session}); }

Object.assign(T_VIEW_LABEL, {business: 'Project management', global_context: 'Context', journal: 'Journal', calendar: 'Calendar'});
Object.assign(T_VIEW_ICON, {
  business: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12a24 24 0 0 0 18 0M10 13h4"/></svg>',
  global_context: SVG_BRAIN,
  journal: T_VIEW_ICON.notes,
});

// The old standalone Context page is a view of one registered agent, selected by stable role id (Tables is a
// shared-drawer tab; Business management keeps its own contextual Tables section). A missing agent is an explicit
// unavailable state, never another agent's content.
const AGENT_PANE_DESTINATIONS = {
  context: {role: 'learning-applier', view: 'global_context', label: 'Context'},
  calendar: {role: 'ordubashy', view: 'calendar', label: 'Calendar'},
  journal: {role: 'ordubashy', view: 'journal', label: 'Journal'},
  notes: {role: 'ordubashy', view: 'journal', label: 'Journal'},
};
let agentWorkspaceRequest = null, agentWorkspaceApplied = 'null';
// Only a role whose pane offers the view may store or restore its descriptor; no role inherits another's.
const AGENT_PANE_DESCRIPTOR_VIEWS = ['global_context', 'business', 'journal', 'calendar'];
const agentWorkspaceDescriptorRole = (view, role) => view === 'global_context' ? role === 'learning-applier'
  : view === 'journal' || view === 'calendar' ? role === 'ordubashy'
  : view === 'business' && agentWorkspaceViews('global', role).includes('business');
// Map an old top-level destination (including the legacy Context aliases) to its agent view and sub-route.
function agentWorkspaceLegacyDestination(tab, hash) {
  if (embeddedAgentPane) return null;
  const parts = hash.split('/')[2] === tab ? hash.split('/').slice(3) : [];
  const alias = {learning: ['context', ['learning']]}[tab];
  const key = alias ? alias[0] : tab;
  return AGENT_PANE_DESTINATIONS[key] ? {...AGENT_PANE_DESTINATIONS[key], tab: key, route: (alias ? alias[1] : parts).join('/')} : null;
}
async function agentWorkspaceRedirect(destination) {
  const navigation = workspaceNavigation;
  let agent = null;
  try {
    agent = (await loadGlobalAgentProfiles()).find(candidate => globalRosterAgent(candidate)
      && (candidate.role?.id || candidate.role_id) === destination.role) || null;
  } catch (_) { /* reported as unavailable below */ }
  if (navigation !== workspaceNavigation) return false;
  if (!agent) {
    state.globalTab = destination.tab;
    renderTab(); syncWorkspaceNav();
    return false;
  }
  agentWorkspaceRequest = {agentId: agent.agent_id, view: destination.view, route: destination.route || ''};
  ls('ag:scope:global', agent.agent_id);
  // Replace, not push: Back must not return to the retired URL and redirect again. The outer history entry
  // keeps a small descriptor (never editor contents) so a refresh or Back/Forward reopens the same destination.
  history.replaceState({...(history.state || {}), agentPane: agentWorkspaceRequest}, '', location.pathname + location.search + '#/global/agents');
  return true;
}
function agentWorkspaceTakeRequest() { const request = agentWorkspaceRequest; agentWorkspaceRequest = null; return request; }
function agentWorkspaceStore(descriptor) {
  if (embeddedAgentPane || location.hash !== '#/global/agents') return;
  const next = {...(history.state || {})};
  if (descriptor) next.agentPane = descriptor; else delete next.agentPane;
  history.replaceState(next, '');
  agentWorkspaceApplied = JSON.stringify(descriptor || null);
}
// A stored descriptor names its own agent; the roster then checks that agent is active and has the view's role.
function agentWorkspaceRehydrate() {
  let descriptor = history.state?.agentPane;
  if (!descriptor || typeof descriptor !== 'object' || location.hash !== '#/global/agents') return null;
  // The retired standalone Tables key translates here (a retired Tables record was always Global's).
  if (descriptor.view === 'tables') {
    agentWorkspaceStore(null);
    globalThis.ovOpen?.('tables', {scope: 'global', id: descriptor.table, mode: descriptor.mode});
    return null;
  }
  return AGENT_PANE_DESCRIPTOR_VIEWS.includes(descriptor.view) ? descriptor : null;
}
// A pane view or agent change that leaves the described destination drops it, so stale records never reopen.
function agentWorkspaceViewChanged(p) {
  const descriptor = history.state?.agentPane;
  if (descriptor && (descriptor.agentId !== p.agentId || descriptor.view !== p.view)) agentWorkspaceStore(null);
}
// The embedded Context/Tables page reports its route or selection; the parent replaces its own history state.
function agentWorkspaceReport(view, patch) {
  const p = _agpad;
  if (!p?.agentId || p.view !== view || !agentWorkspaceDescriptorRole(view, p.workspaceRole)) return;
  const prior = history.state?.agentPane;
  agentWorkspaceStore({...(prior?.agentId === p.agentId && prior.view === view ? prior : {agentId: p.agentId, view}), ...patch});
}
// A frame hosted by the shared Tables drawer reports only to that drawer, which checks the exact live frame; it never
// falls back to the agent pane's report, even once the frame has been removed. `drawerOnly` fields concern only that host.
const agentWorkspaceDrawerHost = () => window.frameElement?.dataset.workspaceHost === 'drawer-tables'
  || new URLSearchParams(location.search).get('workspace-host') === 'drawer-tables';
const agentWorkspaceReportParent = (patch, drawerOnly = false) => {
  if (!embeddedAgentPane || !parent || parent === window) return;
  if (agentWorkspaceDrawerHost()) parent.drawerTablesReport?.(window, {...patch, scope: state.scope === 'global' ? 'global' : state.biz});
  else if (!drawerOnly) parent.agentWorkspaceReport?.(new URLSearchParams(location.search).get('agent-pane-embed'), patch);
};
window.addEventListener('popstate', async () => {
  if (embeddedAgentPane || state.scope !== 'global' || state.globalTab !== 'agents' || location.hash !== '#/global/agents') return;
  if (JSON.stringify(history.state?.agentPane || null) === agentWorkspaceApplied) return;
  if (await agentWorkspaceCanLeave(_agpad)) renderTab();
});
function agentWorkspaceUnavailable(c, tab) {
  const {label, role} = AGENT_PANE_DESTINATIONS[tab];
  c.innerHTML = `<section class="pane global-content" aria-label="${esc(label)}"><div class="global-eyebrow">Global</div><h1>${esc(label)}</h1>
    <p role="alert">${esc(label)} opens in the ${role === 'ordubashy' ? 'Ordubashy' : 'Learning Applier'} agent pane, and that agent is not available.</p>
    <button type="button" id="agent-pane-retry">Retry</button></section>`;
  $('#agent-pane-retry', c).onclick = () => { globalAgentProfilesRequest = undefined; goGlobalTab(tab); };
}
// Embedded pages cannot change the surrounding app; they ask it to open an agent or a business view.
async function agentWorkspaceEscape({agent, business, table, signoffs}) {
  if (signoffs) return sgAttentionNavigate(null);
  if (agent) return globalAgentsNavigate(agent);
  if (table) return tablesOpen(table.scope === 'global' ? null : table.scope, table.id, table.mode);
  if (!state.bizs.some(b => b.slug === business)) { toast('This project is unavailable.'); return; }
  // A selected agent with no Business pane leaves the business to the guarded top-level selection.
  if (!_agpad?.views.includes('business')) return selectBiz(business);
  ls(AGENT_WORKSPACE_BIZ_KEY, business); ls(agentWorkspaceSectionKey(business), 'tasks');
  await agpadSetView(_agpad, 'business');
}

// The right agent pane keeps only Focus, the agent's own workspaces and Learning Applier's Context.
// Project management left this navigation; its data stays reachable through the existing Global
// Projects page and Project records.
// This offers a pane, not an agent tool grant. Backend authorization is unchanged.
function agentWorkspaceViews(scope, role) {
  if (scope !== 'global') return [];
  if (role === 'learning-applier') return ['global_context'];
  if (role === 'ordubashy') return ['calendar', 'journal'];
  return [];
}
// Ordubashy opens Focus and Learning Applier Context; a general worker opens its saved allowed browser choice or inbox/status.
function agentWorkspaceDefault(p) {
  if (p.workspaceScope !== 'global') return null;
  if (p.workspaceRole === 'ordubashy') return 'focus';
  if (p.workspaceRole === 'global-worker') return null;
  return agentWorkspaceViews(p.workspaceScope, p.workspaceRole)[0] || null;
}

// A general worker's first view is its inbox/status, not Project management, when no saved choice applies.
function agentWorkspaceFallback(p) {
  if (p.workspaceScope !== 'global' || p.workspaceRole !== 'global-worker') return null;
  return p.views.find(view => view === 'inbox') || p.views.find(view => view === 'status') || null;
}

let agentWorkspaceDocument = null;
let agentWorkspacePairedGeneration = 0, agentWorkspacePairedFrame = null;
const AGENT_WORKSPACE_PRIVATE_TYPES = new Set(['task','table','reference','signoff','project']);
function agentWorkspacePairedValidSource(source) {
  return !!source && AGENT_WORKSPACE_PRIVATE_TYPES.has(source.type) && typeof source.id === 'string'
    && source.id.length > 0 && source.id.length <= 300
    && (source.business_id == null || (typeof source.business_id === 'string' && source.business_id.length > 0 && source.business_id.length <= 300));
}
function agentWorkspacePairedCurrent(child) {
  const p = _agpad;
  if (!p || !p.open || p.workspaceRole !== 'ordubashy' || !p.agentId || state.scope !== 'global'
      || state.globalTab !== 'agents' || p.view !== 'focus' || p.isCurrent?.() === false) return false;
  if (child === window) return p.focusMount?.isConnected === true;
  const frame = p.workspaceFrame;
  return !!frame?.isConnected && frame.contentWindow === child && p.el?.contains(frame);
}
// Same-origin request from the exact selected Focus frame. It has no transport fallback:
// detached, stale, foreign or malformed callers are ignored.
function agentWorkspacePairedRecord(child, source, parentTable = null) {
  if (!agentWorkspacePairedCurrent(child)) return false;
  if (source == null) {
    const generation = ++agentWorkspacePairedGeneration;
    agentWorkspacePairedFrame = child === window ? null : child;
    return globalAgentViewActive?.clearPairedSource?.(generation) ?? false;
  }
  if (!agentWorkspacePairedValidSource(source)) return false;
  if (parentTable != null && (!agentWorkspacePairedValidSource({...parentTable, type: 'table'})
      || parentTable.type !== 'table' || source.type !== 'task')) return false;
  const generation = ++agentWorkspacePairedGeneration;
  agentWorkspacePairedFrame = child === window ? null : child;
  const canonical = {type: source.type, id: source.id, business_id: source.business_id ?? null};
  return globalAgentViewActive?.selectPairedSource?.(canonical, generation, () => {
    if (generation === agentWorkspacePairedGeneration && agentWorkspacePairedCurrent(child)) {
      if (typeof child.agentWorkspacePairedUnavailable === 'function') return child.agentWorkspacePairedUnavailable(canonical);
      return agentWorkspacePairedRecord(child, null, null);
    }
    return false;
  }, ref => {
    if (generation === agentWorkspacePairedGeneration && agentWorkspacePairedCurrent(child))
      return child.agentWorkspacePairedReference?.(ref) ?? false;
    return false;
  }) ?? false;
}
function agentWorkspacePairedDetach(frame) {
  if (!frame || frame !== agentWorkspacePairedFrame) return false;
  agentWorkspacePairedFrame = null;
  const generation = ++agentWorkspacePairedGeneration;
  return globalAgentViewActive?.clearPairedSource?.(generation) ?? false;
}
window.agentWorkspacePairedRecord = agentWorkspacePairedRecord;
window.agentWorkspacePairedDetach = agentWorkspacePairedDetach;
function agentWorkspaceLoadDocument() {
  if (!agentWorkspaceDocument) agentWorkspaceDocument = fetch('/', {credentials: 'same-origin'})
    .then(response => {
      if (!response.ok) throw new Error('This pane could not load.');
      return response.text();
    }).catch(error => { agentWorkspaceDocument = null; throw error; });
  return agentWorkspaceDocument;
}

function agentWorkspaceFrame(view, business, section, hint) {
  const frame = document.createElement('iframe');
  const params = new URLSearchParams({'agent-pane-embed': view});
  if (business) params.set('business', business);
  if (section) params.set('section', section);
  if (hint?.route) params.set('route', hint.route);
  if (hint?.host) { params.set('workspace-host', hint.host); frame.dataset.workspaceHost = hint.host; }
  for (const key of ['table', 'mode']) if (hint?.[key]) params.set(key, hint[key]);
  frame.className = 'agent-workspace-frame';
  frame.setAttribute('aria-label', ['business', 'tables'].includes(view) ? `${business === 'global' ? 'Global' : business} · ${section}` : T_VIEW_LABEL[view]);
  // The production dashboard cannot itself be framed (CSP/X-Frame-Options).
  // Populate our same-origin child with the trusted app shell instead. Keep its
  // own URL/state and reuse the shell across panes without relaxing server policy.
  const load = async () => {
    try {
      const html = await agentWorkspaceLoadDocument();
      if (!frame.isConnected) return false;
      await new Promise(resolve => {
        const child = frame.contentWindow, doc = child.document;
        doc.open();
        child.history.replaceState(null, '', '/?' + params);
        frame.addEventListener('load', () => resolve(child.agentWorkspaceReady), {once: true});
        // Download the app's scripts concurrently; defer preserves their source
        // order and completes them before the frame's load/ready boundary.
        doc.write(html.replace(/<script src="/g, '<script defer src="'));
        doc.close();
      });
      return true;
    } catch (error) {
      if (!frame.isConnected) return false;
      const doc = frame.contentDocument;
      doc.body.innerHTML = '<p role="alert"></p><button type="button">Retry</button>';
      doc.querySelector('p').textContent = "Couldn't load. Retry";
      doc.querySelector('button').onclick = () => { frame.workspaceReady = load(); };
      return false;
    }
  };
  frame.workspaceReady = load();
  return frame;
}

function agentWorkspaceMount(p, view) {
  if (!agentWorkspaceViews(p.workspaceScope, p.workspaceRole).includes(view)) return false;
  const body = $('#agpad-body', p.el);
  body.classList.add('agent-workspace-body');
  // A redirected old URL carries its sub-route and record selection to the view it opens.
  const hint = agentWorkspaceRequest?.view === view && agentWorkspaceRequest.agentId === p.agentId ? agentWorkspaceRequest : null;
  if (view === 'calendar') {
    // The existing Calendar page, mounted natively (no frame): it shares this document's personal-status store with Focus, and its
    // disposer stops its polling and listeners whenever the pane leaves the view.
    const host = document.createElement('div');
    host.className = 'agent-calendar-host';
    body.replaceChildren(host);
    const dispose = calendarMount(host, {embedded: true, isCurrent: () => _agpad === p && p.view === 'calendar' && host.isConnected});
    p.workspaceDispose = () => { dispose(); host.remove(); };
    return true;
  }
  if (view !== 'business') {
    const frame = agentWorkspaceFrame(view, undefined, undefined, hint);
    body.replaceChildren(frame);
    p.workspaceFrame = frame;
    p.workspaceDispose = () => { frame.remove(); p.workspaceFrame = null; };
    return true;
  }
  const businesses = workspaceBusinesses();
  const wanted = hint?.scope || ls(AGENT_WORKSPACE_BIZ_KEY);
  let business = wanted === 'global' || businesses.some(b => b.slug === wanted) ? wanted : 'global';
  // A linked business that is not in the directory opens no record rather than another scope's.
  if (hint?.scope && business !== hint.scope) hint.table = '';
  // A Tables link opens the Tables section (Ordubashy only); a remembered section the role no longer offers falls to the first allowed.
  const offered = () => agentWorkspaceOffered(business, p.workspaceRole);
  let section = hint?.section === 'tables' && offered().some(([key]) => key === 'tables') ? 'tables' : agentWorkspaceSection(business, undefined, p.workspaceRole);
  body.innerHTML = `<section class="agent-business-workspace" aria-label="Project management">
    <div class="agent-business-controls"><nav class="agent-business-sections" aria-label="Project sections"></nav></div>
    <div class="agent-business-screen"></div></section>`;
  const picker = document.createElement('label'), head = $('.thead', p.el);
  picker.className = 'agent-business-picker';
  picker.innerHTML = `<select aria-label="Choose project"><option value="global">Global</option>${businesses.map(b => `<option value="${esc(b.slug)}">${esc(b.name || b.slug)}</option>`).join('')}</select>`;
  head.classList.add('agent-business-head');
  head.append(picker);
  const select = $('select', picker), screen = $('.agent-business-screen', body);
  const paint = async () => {
    select.value = business;
    $('.agent-business-sections', body).innerHTML = offered().map(([key, label]) =>
      `<button type="button" data-business-section="${key}" aria-label="${label}" aria-pressed="${key === section}">${agentWorkspaceSectionIcon(key)}</button>`).join('');
    if (p.workspaceFrame) {
      const frame = p.workspaceFrame;
      if (!await frame.workspaceReady) return;
      if (!frame.isConnected) return;
      frame.setAttribute('aria-label', `${business === 'global' ? 'Global' : business} · ${section}`);
      await frame.contentWindow.agentWorkspaceNavigateEmbedded(view, business, section);
    } else {
      p.workspaceFrame = agentWorkspaceFrame(view, business, section, hint);
      screen.replaceChildren(p.workspaceFrame);
    }
  };
  let changing = false;
  const change = async (nextBusiness, nextSection) => {
    if (changing) { select.value = business; return; }
    changing = true;
    try {
      if (!await p.workspaceFrame.workspaceReady) { select.value = business; return; }
      if (!await agentWorkspaceCanLeave(p) || _agpad !== p || p.view !== view) { select.value = business; return; }
      business = nextBusiness; section = nextSection;
      ls(AGENT_WORKSPACE_BIZ_KEY, business);
      if (section === 'tables') agentWorkspaceReport('business', {section: 'tables', scope: business, table: '', mode: ''});
      else if (history.state?.agentPane?.view === 'business') agentWorkspaceStore(null);
      ls(agentWorkspaceSectionKey(business), section);
      await paint();
    } finally { changing = false; }
  };
  select.onchange = () => change(select.value, agentWorkspaceSection(select.value, undefined, p.workspaceRole));
  $('.agent-business-sections', body).onclick = event => {
    const button = event.target.closest('[data-business-section]');
    if (button && button.dataset.businessSection !== section) change(business, button.dataset.businessSection);
  };
  p.workspaceDispose = () => { picker.remove(); head.classList.remove('agent-business-head'); p.workspaceFrame?.remove(); p.workspaceFrame = null; };
  paint();
  if (section === 'tables') agentWorkspaceReport('business', {section: 'tables', scope: business, ...(hint ? {table: hint.table || '', mode: hint.mode || ''} : {})});
  return true;
}

// Native editors retain their save/leave contract when hosted in a pane.
async function agentWorkspaceCanLeave(p) {
  const child = p?.workspaceFrame?.contentWindow;
  if (child?.agentWorkspaceLeaveEmbedded) return child.agentWorkspaceLeaveEmbedded();
  return true;
}
async function agentWorkspaceLeaveEmbedded() {
  if (!await ntFlush()) return false;
  if (_jState && (_jState.dirty || _jState.saving)) { await jSave(); if (_jState.dirty || _jState.saving) return false; }
  const pending = new Event('beforeunload', {cancelable: true});
  window.dispatchEvent(pending);
  if (!pending.defaultPrevented) return true;
  if (!confirm('This pane has unsaved edits. Discard them and leave?')) return false;
  // These editors retain drafts in document-local maps. A confirmed discard
  // must clear them now that section navigation no longer destroys the frame.
  _dataDrafts.clear(); workingMemoryDrafts.clear();
  return true;
}

async function agentWorkspaceBootEmbedded() {
  document.body.classList.add('agent-pane-embedded');
  const params = new URLSearchParams(location.search), view = params.get('agent-pane-embed');
  const c = $('#content');
  c.innerHTML = '<p class="muted" role="status">Loading…</p>';
  try {
    const snapshot = window.parent !== window && window.parent.agentWorkspaceSnapshot?.();
    if (snapshot) { state.bizs = snapshot.bizs; state.session = snapshot.session; }
    else state.bizs = await api('/biz');
    await loadSession();
    const business = params.get('business') || 'global';
    // In-page route links change the pane's route without pushing history entries.
    document.addEventListener('click', event => {
      const link = event.target.closest?.('a[href^="#/"]');
      if (link && !event.defaultPrevented && event.button === 0 && !(event.ctrlKey || event.metaKey || event.shiftKey)) { event.preventDefault(); routeTo(link.getAttribute('href')); }
    });
    await agentWorkspaceNavigateEmbedded(view, business, agentWorkspaceSection(business, params.get('section')), params.get('route') || '');
    if (embeddedTablesView() && ['table', 'mode'].some(key => params.get(key))) await tablesOpen(business === 'global' ? null : business, params.get('table'), params.get('mode') || 'tables');
    // Reuse the parent's data-change stream; no background drawers or agent feeds.
    connectEvents();
  } catch (error) {
    c.innerHTML = `<p role="alert">Couldn't load. Retry</p><button type="button" data-embed-retry>Retry</button>`;
    $('[data-embed-retry]', c).onclick = agentWorkspaceBootEmbedded;
  }
}

async function agentWorkspaceNavigateEmbedded(view, business = 'global', section, route = '') {
  if (business !== 'global' && !state.bizs.some(b => b.slug === business)) throw new Error('This project is unavailable.');
  if (view !== 'tables') section = agentWorkspaceSection(business, section);
  const tablesView = view === 'tables' || (view === 'business' && section === 'tables');
  state.scope = !['business', 'tables'].includes(view) || business === 'global' ? 'global' : 'business';
  state.biz = state.scope === 'global' ? null : business;
  state.ctx = tablesView && state.scope === 'business' ? '_tables' : null;
  let hash;
  if (tablesView) {
    state.tab = 'context';
    state.globalTab = 'tables';
    hash = state.scope === 'global' ? '#/global/tables' : '#' + business;
  } else if (state.scope === 'business') {
    state.tab = section === 'tasks' ? 'tasks' : 'progress';
    hash = '#' + business;
  } else {
    state.globalTab = view === 'journal' ? 'journal' : view === 'business' ? section : 'context';
    hash = '#/global/' + state.globalTab + (view === 'global_context' ? '/' + (route || 'graph') : '');
  }
  const params = new URLSearchParams({'agent-pane-embed': view, business, section: section || ''});
  if (agentWorkspaceDrawerHost()) params.set('workspace-host', 'drawer-tables');
  history.replaceState(null, '', location.pathname + '?' + params + hash);
  renderedGlobalHash = hash;
  renderTab();
}

function agentWorkspaceRenderEmbedded(c) {
  if (!agentWorkspaceEmbedded()) return false;
  const view = new URLSearchParams(location.search).get('agent-pane-embed');
  if (view === 'journal') {
    // Host the existing Notes/Journal editor unchanged, with its own save/leave lifecycle.
    tView = 'notes';
    c.innerHTML = '<section id="tbody" class="tbody nt" aria-label="Journal"></section>';
    tPaintNotes();
    return true;
  }
  if (state.scope === 'business' && state.tab === 'progress') {
    renderBusinessProgress(c);
    return true;
  }
  if (embeddedTablesView()) {
    // The native tables list/detail/edit/history controls, over the selected Global or business scope.
    if (state.scope === 'business') { c.innerHTML = '<div id="ctxembedded"></div>'; renderTables($('#ctxembedded', c)); }
    else renderTables(c);
    return true;
  }
  if (state.scope !== 'global') return false;
  if (view === 'business' && state.globalTab === 'overview') {
    c.innerHTML = `<section class="pane global-content" aria-label="Overview"><header class="global-heading-row"><h1>Projects</h1></header>${state.bizs.length ? '' : '<p class="muted" data-no-projects>No projects yet. Ask Ordubashy to create one.</p>'}<div id="global-progress"></div></section>`;
    globalProgressMount($('#global-progress', c));
    return true;
  }
  return false;
}

// The shell startup: the embedded editors' unsaved-edit prompt is forwarded from the outer document so a reload keeps their drafts.
function agentWorkspaceShellStart() {
  document.body.classList.add('agent-workspace-app');
  $('#workspace-back button').onclick = () => selectGlobal('agents');
  window.addEventListener('beforeunload', event => {
    const child = _agpad?.workspaceFrame?.contentWindow;
    if (!child) return;
    const pending = new Event('beforeunload', {cancelable: true});
    child.dispatchEvent(pending);
    if (pending.defaultPrevented) { event.preventDefault(); event.returnValue = ''; }
  });
}

async function agentJournalOpen(id) {
  if(embeddedAgentPane)return parent.agentJournalOpen(id);
  await selectGlobal('journal');
  const frame=_agpad?.workspaceFrame;
  if(!frame || !await frame.workspaceReady || _agpad?.workspaceFrame!==frame)return;
  await frame.contentWindow.ntFetch();
  if(_agpad?.workspaceFrame===frame)await frame.contentWindow.ntSelect(id);
}

// Exact record links remain usable after removing the drawer's management screens.
async function agentOpenRecordReference(ref) {
  if(embeddedAgentPane)return parent.agentOpenRecordReference(ref);
  const agent=(await loadGlobalAgentProfiles()).find(a=>globalRosterAgent(a) && a.role?.id==='ordubashy');
  if(!agent){toast('Ordubashy is unavailable.');return false;}
  if(state.scope!=='global' || state.globalTab!=='agents')await selectGlobal('agents');
  if(!await globalAgentsNavigate(agent.agent_id,{view:'focus'}))return false;
  const pane=_agpad;
  if(pane?.agentId!==agent.agent_id || pane.view!=='focus')return false;
  return agfocusOpenPairedReference(pane,ref);
}
