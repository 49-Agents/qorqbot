// One document hosts the native screens. Navigation reuses it, while its page
// state stays separate from the selected agent, transcript and chat draft.
const AGENT_WORKSPACE_BIZ_KEY = 'agent-workspace:business:v1';
const AGENT_WORKSPACE_SECTION_KEY = 'agent-workspace:section:v1';
const AGENT_WORKSPACE_SECTIONS = [
  ['marketing', 'Marketing'], ['progress', 'Progress'], ['research', 'Research'],
  ['milestones', 'Milestones'], ['accounting', 'Accounting'], ['tasks', 'Tasks'], ['browser', 'Browser'],
];
const AGENT_WORKSPACE_GLOBAL_SECTIONS = [
  ['overview', 'Overview'], ['costs', 'Costs'], ['context', 'Context'], ['tables', 'Tables'],
];
const agentWorkspaceEmbedded = () => new URLSearchParams(location.search).has('agent-pane-embed');
const agentWorkspaceSections = business => business === 'global' ? AGENT_WORKSPACE_GLOBAL_SECTIONS : AGENT_WORKSPACE_SECTIONS;
const agentWorkspaceSectionKey = business => AGENT_WORKSPACE_SECTION_KEY + (business === 'global' ? ':global' : '');
const agentWorkspaceSection = (business, section = ls(agentWorkspaceSectionKey(business))) =>
  agentWorkspaceSections(business).some(([key]) => key === section) ? section : agentWorkspaceSections(business)[0][0];
const agentWorkspaceSectionIcon = key => ({overview: GLOBAL_ICONS.globe, costs: GLOBAL_ICONS.costs,
  context: SVG_BRAIN, tables: GLOBAL_ICONS.tables, marketing: MK_ICO.queue,
  progress: GLOBAL_ICONS.analytics, research: aicon('search', 'currentColor', 18),
  milestones: aicon('flag', 'currentColor', 18), accounting: GLOBAL_ICONS.costs, tasks: T_VIEW_ICON.tasks, browser: T_VIEW_ICON.browser})[key];

// Copy only the shared directory and owner session, never the parent's mutable
// navigation or editor state. The child still uses native authenticated reads.
function agentWorkspaceSnapshot() { return structuredClone({bizs: state.bizs, session: state.session}); }

Object.assign(T_VIEW_LABEL, {business: 'Business management', ideas: 'Ideas', global_context: 'Global context'});
Object.assign(T_VIEW_ICON, {
  business: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12a24 24 0 0 0 18 0M10 13h4"/></svg>',
  ideas: GLOBAL_ICONS.ideas, global_context: SVG_BRAIN,
});

// Every global agent has the business view; Ordubashy keeps Focus first; Learning Applier keeps its own first.
function agentWorkspaceViews(scope, role) {
  if (scope !== 'global') return [];
  if (role === 'ordubashy') return ['ideas', 'business'];
  if (role === 'global-marketing') return ['business'];
  if (role === 'learning-applier') return ['global_context', 'business'];
  return [];
}
function agentWorkspaceDefault(p) {
  if (p.workspaceScope !== 'global') return null;
  if (['ordubashy', 'global-marketing'].includes(p.workspaceRole)) return 'focus';
  return agentWorkspaceViews(p.workspaceScope, p.workspaceRole)[0] || null;
}

let agentWorkspaceDocument = null;
function agentWorkspaceLoadDocument() {
  if (!agentWorkspaceDocument) agentWorkspaceDocument = fetch('/', {credentials: 'same-origin'})
    .then(response => {
      if (!response.ok) throw new Error('This pane could not load.');
      return response.text();
    }).catch(error => { agentWorkspaceDocument = null; throw error; });
  return agentWorkspaceDocument;
}

function agentWorkspaceFrame(view, business, section) {
  const frame = document.createElement('iframe');
  const params = new URLSearchParams({'agent-pane-embed': view});
  if (business) params.set('business', business);
  if (section) params.set('section', section);
  frame.className = 'agent-workspace-frame';
  frame.setAttribute('aria-label', view === 'business' ? `${business === 'global' ? 'Global' : business} · ${section}` : T_VIEW_LABEL[view]);
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
        doc.write(html);
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
  if (view !== 'business') {
    const frame = agentWorkspaceFrame(view);
    body.replaceChildren(frame);
    p.workspaceFrame = frame;
    p.workspaceDispose = () => { frame.remove(); p.workspaceFrame = null; };
    return true;
  }
  const businesses = workspaceBusinesses();
  const saved = ls(AGENT_WORKSPACE_BIZ_KEY);
  let business = saved === 'global' || businesses.some(b => b.slug === saved) ? saved : 'global';
  let section = agentWorkspaceSection(business);
  body.innerHTML = `<section class="agent-business-workspace" aria-label="Business management">
    <div class="agent-business-controls"><nav class="agent-business-sections" aria-label="Business sections"></nav></div>
    <div class="agent-business-screen"></div></section>`;
  const picker = document.createElement('label'), head = $('.thead', p.el);
  picker.className = 'agent-business-picker';
  picker.innerHTML = `<select aria-label="Choose business"><option value="global">Global</option>${businesses.map(b => `<option value="${esc(b.slug)}">${esc(b.name || b.slug)}</option>`).join('')}</select>`;
  head.classList.add('agent-business-head');
  head.append(picker);
  const select = $('select', picker), screen = $('.agent-business-screen', body);
  const paint = async () => {
    select.value = business;
    $('.agent-business-sections', body).innerHTML = agentWorkspaceSections(business).map(([key, label]) =>
      `<button type="button" data-business-section="${key}" aria-label="${label}" aria-pressed="${key === section}">${agentWorkspaceSectionIcon(key)}</button>`).join('');
    if (p.workspaceFrame) {
      const frame = p.workspaceFrame;
      if (!await frame.workspaceReady) return;
      if (!frame.isConnected) return;
      frame.setAttribute('aria-label', `${business === 'global' ? 'Global' : business} · ${section}`);
      await frame.contentWindow.agentWorkspaceNavigateEmbedded(view, business, section);
    } else {
      p.workspaceFrame = agentWorkspaceFrame(view, business, section);
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
      ls(AGENT_WORKSPACE_BIZ_KEY, business); ls(agentWorkspaceSectionKey(business), section);
      await paint();
    } finally { changing = false; }
  };
  select.onchange = () => change(select.value, agentWorkspaceSection(select.value));
  $('.agent-business-sections', body).onclick = event => {
    const button = event.target.closest('[data-business-section]');
    if (button && button.dataset.businessSection !== section) change(business, button.dataset.businessSection);
  };
  p.workspaceDispose = () => { picker.remove(); head.classList.remove('agent-business-head'); p.workspaceFrame?.remove(); p.workspaceFrame = null; };
  paint();
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
  _dataDrafts.clear(); ideaDrafts.clear(); personalDrafts.clear(); workingMemoryDrafts.clear();
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
    await agentWorkspaceNavigateEmbedded(view, business, agentWorkspaceSection(business, params.get('section')));
    // Reuse the parent's data-change stream; no background drawers or agent feeds.
    connectEvents();
  } catch (error) {
    c.innerHTML = `<p role="alert">Couldn't load. Retry</p><button type="button" data-embed-retry>Retry</button>`;
    $('[data-embed-retry]', c).onclick = agentWorkspaceBootEmbedded;
  }
}

async function agentWorkspaceNavigateEmbedded(view, business = 'global', section) {
  if (business !== 'global' && !state.bizs.some(b => b.slug === business)) throw new Error('This business is unavailable.');
  section = agentWorkspaceSection(business, section);
  state.scope = view !== 'business' || business === 'global' ? 'global' : 'business';
  state.biz = state.scope === 'global' ? null : business;
  state.ctx = null;
  let hash;
  if (state.scope === 'business') {
    state.tab = ['marketing', 'accounting', 'tasks', 'browser'].includes(section) ? section : 'progress';
    businessProgressBiz = business; businessProgressView = section;
    hash = '#' + business;
  } else {
    state.globalTab = view === 'business' ? section : 'context';
    hash = '#/global/' + state.globalTab + (view === 'ideas' ? '/ideas' : view === 'global_context' ? '/graph' : '');
  }
  const params = new URLSearchParams({'agent-pane-embed': view, business, section});
  history.replaceState(null, '', location.pathname + '?' + params + hash);
  renderedGlobalHash = hash;
  renderTab();
}

function agentWorkspaceRenderEmbedded(c) {
  if (!agentWorkspaceEmbedded()) return false;
  const view = new URLSearchParams(location.search).get('agent-pane-embed');
  if (state.scope === 'business' && state.tab === 'progress') {
    if (businessProgressView === 'milestones') renderMilestones(c);
    else if (businessProgressView === 'research') c.innerHTML = '<p class="muted agent-workspace-empty">No research yet.</p>';
    else renderBusinessProgress(c);
    return true;
  }
  if (state.scope !== 'global') return false;
  if (view === 'business' && state.globalTab === 'overview') {
    c.innerHTML = '<section class="pane global-content" aria-label="Overview"><div id="global-progress"></div></section>';
    globalProgressMount($('#global-progress', c));
    return true;
  }
  const route = location.hash.split('/')[3];
  if (state.globalTab === 'context' && view === 'ideas') {
    if (route === 'ideas') { renderIdeas(c); return true; }
    if (route === 'library') { renderGlobalLibrary(c); return true; }
  }
  return false;
}

let agentWorkspaceMenu = null;
function agentWorkspaceMenuPlace(c) {
  if (!agentWorkspaceMenu) return;
  const {anchor, show} = agentWorkspaceMenu;
  const top = c?.querySelector('.agsplit:not(.nav-off) > .listcol > .ctop');
  const parent = top || document.body;
  if (anchor.parentElement !== parent) {
    show(false);
    parent.prepend(anchor);
  }
  document.body.classList.toggle('workspace-menu-floating', !top);
}

function agentWorkspaceMenuMount() {
  document.body.classList.add('agent-workspace-app');
  const anchor = document.createElement('div');
  anchor.className = 'workspace-menu-anchor';
  anchor.innerHTML = '<button type="button" class="quiet workspace-menu-toggle" aria-label="Main menu" aria-expanded="false" aria-controls="rail"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg></button>';
  const toggle = $('button', anchor), rail = $('#rail');
  anchor.appendChild(rail);
  let timer;
  const show = value => { clearTimeout(timer); document.body.classList.toggle('workspace-menu-open', value); toggle.setAttribute('aria-expanded', String(value)); rail.inert = !value; };
  agentWorkspaceMenu = {anchor, show};
  agentWorkspaceMenuPlace();
  toggle.onclick = () => show(!document.body.classList.contains('workspace-menu-open'));
  anchor.onpointerenter = event => { if (event.pointerType !== 'touch') show(true); };
  anchor.onpointerleave = () => { timer = setTimeout(() => { if (!document.querySelector('.workspace-menu') && !anchor.contains(document.activeElement)) show(false); }, 180); };
  rail.addEventListener('click', event => { if (event.target.closest('.ritem')) { show(false); toggle.focus(); } });
  document.addEventListener('pointerdown', event => { if (!anchor.contains(event.target) && !event.target.closest('.workspace-menu')) show(false); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && document.body.classList.contains('workspace-menu-open')) { show(false); toggle.focus(); } });
  window.addEventListener('beforeunload', event => {
    const child = _agpad?.workspaceFrame?.contentWindow;
    if (!child) return;
    const pending = new Event('beforeunload', {cancelable: true});
    child.dispatchEvent(pending);
    if (pending.defaultPrevented) { event.preventDefault(); event.returnValue = ''; }
  });
}
