// Global profiles have their own roster and terminal identity, with no business selection.
// Registered business agents can also be picked here; the Global screen stays put.
let globalAgentsGeneration = 0;
// Attention links use the mounted roster's normal selection path. The page shell,
// menu, lookup and divider stay in place while the destination agent changes.
async function globalAgentsNavigate(agentId, options = {}) {
  const root = document.querySelector('.global-agents');
  if (!root?.openAgent) return false;
  await root.openAgent(agentId, options);
  return true;
}
const GLOBAL_WORKSPACE_TERMINAL = '__qorqut_workspace_terminal__';
// One owner shell keeps the original id, route and tmux session. Older saved or
// linked Terminal 2/3 selections open it instead; their sessions and routes are untouched.
const globalWorkspaceTerminal = id => /^__qorqut_workspace_terminal(?:_[23])?__$/.test(id || '')
  ? {id: GLOBAL_WORKSPACE_TERMINAL, label: 'Terminal', base: '/root/workspace-terminal'} : null;
const GLOBAL_PINS_KEY = 'ag:global-pins:v1';
// The lookup panel (business agents and Terminal) collapses on its own, and starts collapsed until the owner
// chooses otherwise; this is neither the whole-navigation toggle nor pane state.
const GLOBAL_BIZ_COLLAPSED_KEY = 'ag:global-biz-collapsed:v1';
const globalLegacyPins = () => {
  try { const ids = JSON.parse(ls(GLOBAL_PINS_KEY) || '[]'); return new Set(Array.isArray(ids) ? ids.filter(id => typeof id === 'string') : []); }
  catch (_) { return new Set(); }
};
let globalSavedPins = null;
let globalPinWrites = Promise.resolve();
const globalPinnedAgents = () => new Set(globalSavedPins || globalLegacyPins());
const globalAcceptPins = data => {
  globalSavedPins = data.pinned_agents;
  ls(GLOBAL_PINS_KEY, JSON.stringify(globalSavedPins));
};
async function globalLoadPins() {
  await globalPinWrites;
  let data = coreData(await api('/root/agent-pins'));
  if (!data.initialized) data = coreData(await post('/root/agent-pins', {initialize: [...globalLegacyPins()]}));
  globalAcceptPins(data);
}
function globalSavePin(id, pinned) {
  const write = globalPinWrites.then(async () => {
    const data = coreData(await post('/root/agent-pins', {agent_id: id, pinned}));
    globalAcceptPins(data);
  });
  globalPinWrites = write.catch(() => {});
  return write;
}
// Retired profiles and archival records never reach the list, whatever was pinned.
const globalActiveProfile = agent => !!agent && typeof agent.agent_id === 'string' && !agent.archival_record && (agent.status === undefined || agent.status === 'active');
const globalTextParts = value => (Array.isArray(value) ? value : [value]).filter(part => typeof part === 'string' || typeof part === 'number').map(String);
const globalNormalize = value => String(value ?? '').toLowerCase().replace(/\s+/g, ' ').trim();
// Every name a person might type for a business agent: its business (current name,
// slug and any supplied aliases), the agent, its role, and its department.
function globalBusinessHaystack(biz, agent) {
  const department = DEPTS[departmentId(agent.department)];
  return globalNormalize([biz.name, biz.slug, ...globalTextParts(biz.aliases), agent.name, agent.display_name, agent.role?.id, agent.kind,
    ...globalTextParts(agent.aliases), agent.department, department?.id, department?.label, ...globalTextParts(department?.aliases)].join(' '));
}
const GLOBAL_LOOKUP_LABEL = 'Find agent or terminal';
const GLOBAL_TERMINAL_HAYSTACK = 'terminal shell workspace qorqut';
const GLOBAL_LOOKUP_ICON = '<svg class="global-biz-find" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>';
const GLOBAL_BIZ_CHEVRON = '<svg class="global-biz-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
const GLOBAL_TERMINAL_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/></svg>';
async function renderGlobalAgents(c) {
  if (typeof agentWorkspaceMenuPlace === 'function') agentWorkspaceMenuPlace();
  const generation = ++globalAgentsGeneration;
  const current = () => generation === globalAgentsGeneration && c.isConnected
    && state.scope === 'global' && state.globalTab === 'agents';
  c.innerHTML = '<section class="pane global-content"><div class="global-eyebrow">Global</div><h1>Agents</h1><p class="muted" role="status">Loading agents…</p></section>';
  let agents;
  try {
    agents = globalAgentProfiles.length ? globalAgentProfiles : await loadGlobalAgentProfiles();
    if (!Array.isArray(agents)) throw new Error('The Global agent list is unavailable.');
    await globalLoadPins();
  } catch (error) {
    if (!current()) return;
    c.innerHTML = `<section class="pane global-content"><div class="global-eyebrow">Global</div><h1>Agents</h1><p role="alert">${esc(error.message)}</p><button id="global-agents-retry">Retry</button></section>`;
    $('#global-agents-retry', c).onclick = () => renderGlobalAgents(c);
    return;
  }
  if (!current()) return;
  c.innerHTML = `<div class="split global-agents agsplit">
    <div class="listcol"><div class="ctop"><div class="global-eyebrow">Global</div><div class="agent-nav-heading"><h1>Agents</h1><span class="agent-nav-actions">${agentNavButton()}</span></div></div>
      <div class="global-nav-body">
        <div class="crows global-primary" id="global-primary-roster">
          <div class="global-roster" id="global-agent-list"></div>
          <div class="global-pinned-list" id="global-pinned-agent-list"></div>
        </div>
        <section class="global-biz-nav" aria-label="Agent and terminal lookup">
          <div class="global-biz-tools">
            <button type="button" class="quiet global-biz-toggle" id="global-biz-toggle" aria-expanded="false" aria-controls="global-biz-region" aria-label="${GLOBAL_LOOKUP_LABEL}">${GLOBAL_LOOKUP_ICON}${GLOBAL_BIZ_CHEVRON}</button>
            <input type="search" class="global-biz-search" id="global-biz-search" aria-label="${GLOBAL_LOOKUP_LABEL}" placeholder="Find…" autocomplete="off" spellcheck="false">
          </div>
          <div class="global-biz-region" id="global-biz-region">
            <div class="global-biz-status" id="global-biz-status" role="status" hidden></div>
            <div class="crows global-biz-list" id="global-biz-agent-list"></div>
          </div>
        </section>
      </div></div>
    <section class="global-agent-session" aria-label="Agent session"><div class="chat termmode" id="global-agent-terminal"></div></section>
    <div class="agent-pane-divider" data-agent-pane-divider role="separator" aria-label="Terminal and agent pane divider" aria-orientation="vertical" tabindex="0"></div>
    <aside class="tasks agpad" id="agpad" aria-label="Agent pane"></aside>
  </div>`;
  wireAgentNavToggle(c, 'global');
  const list = $('#global-agent-list', c), pane = $('#global-agent-terminal', c), nav = $('.listcol', c);
  const bizList = $('#global-biz-agent-list', c), search = $('#global-biz-search', c), bizStatus = $('#global-biz-status', c);
  const pinnedList = $('#global-pinned-agent-list', c), bizRegion = $('#global-biz-region', c), bizToggle = $('#global-biz-toggle', c);
  if (document.body.classList.contains('agent-workspace-app')) {
    // Keep the trigger at the top of the rail. Only its search and results
    // float beside the rail, so opening lookup never moves the trigger or roster.
    $('.global-nav-body', c).prepend($('.global-biz-nav', c));
    bizRegion.prepend(search);
  }
  // Icon-only rows: the agent name lives in the accessible label, with no visible text, tooltip or presence ball.
  list.innerHTML = agents.map(agent => `<button class="item agent" type="button" data-global-agent="${esc(agent.agent_id)}" aria-pressed="false" aria-label="${esc(agent.display_name || agent.name)}">
    <span class="nm">${identityAvatar("agent", agent.display_name || agent.name, {...agent, ...agent.presentation})}</span>
  </button>`.replace(/\s*\n\s*/g, '')).join('');
  agentNavPaint(); agentNavRefresh();
  if (typeof replyNotifications !== 'undefined') replyNotifications.paint();
  const params = new URLSearchParams(location.search);
  const linkedFocus = params.has('focus_item');
  const linkedWorkspace = agTakeLinkedWorkspace('#/global/agents');

  // ---- registered business agents: read once per view (and per explicit retry), searched in memory
  const businesses = (state.bizs || []).filter(biz => biz && biz.slug);
  const rosters = new Map();                       // business slug -> {biz, agents} | {biz, error}
  let rosterLoaded = !businesses.length, selected, selectionVersion = 0;
  const readBusiness = async biz => {
    const rows = await pageRead(`/biz/${encodeURIComponent(biz.slug)}/agents?summary=1`);
    if (!Array.isArray(rows)) throw new Error('Agents unavailable');
    return rows.filter(globalActiveProfile);
  };
  const loadBusinesses = async slugs => {
    const targets = businesses.filter(biz => slugs.includes(biz.slug));
    const results = await Promise.allSettled(targets.map(readBusiness));
    if (!current()) return;
    results.forEach((result, index) => rosters.set(targets[index].slug, result.status === 'fulfilled'
      ? {biz: targets[index], agents: result.value} : {biz: targets[index], error: result.reason}));
    rosterLoaded = true;
    paintBusinessAgents();
  };
  const businessItems = () => {
    const items = new Map();
    for (const {biz, agents: rows = []} of rosters.values())
      for (const agent of rows) if (!items.has(agent.agent_id)) items.set(agent.agent_id, {id: agent.agent_id, biz, agent, haystack: globalBusinessHaystack(biz, agent)});
    return [...items.values()];
  };
  const compare = (a, b) => String(a).localeCompare(String(b));
  const paintSelection = () => {
    nav.querySelectorAll('[data-global-agent],[data-business-agent]').forEach(button => {
      const on = (button.dataset.globalAgent || button.dataset.businessAgent) === selected;
      button.classList.toggle('on', on); button.setAttribute('aria-pressed', String(on));
    });
  };
  // One row renderer for promoted pins and lookup results, so both stay identical.
  const pinControl = (id, label, pinned) => `<button type="button" class="quiet global-biz-pin" data-pin-agent="${esc(id)}" aria-pressed="${pinned}" aria-label="${pinned ? 'Unpin' : 'Pin'} ${esc(label)}">${ICO_PIN}</button>`;
  const businessRow = ({id, biz, agent}, pinned) => {
    const name = agent.display_name || agent.name, business = biz.name || biz.slug, department = dept(agent.department);
    return `<div class="global-biz-row"><button class="item agent global-biz-agent" type="button" data-business-agent="${esc(id)}" aria-pressed="false" aria-label="${esc(`${name}, ${business}, ${department.label}`)}">
        <span class="nm"><span class="global-biz-icons${pinned ? ' pinned' : ''}" aria-hidden="true">${pinned ? '' : identityAvatar('business', business, {}, business)}<span class="identity-avatar identity-department" style="--department-color:${department.color}">${departmentIcon(agent.department, 11)}</span></span>
          <span class="global-biz-text"><span class="global-biz-name">${esc(business)}</span></span></span>
      </button>${pinControl(id, `${name}, ${business}`, pinned)}</div>`;
  };
  // The owner's one shell is an ordinary lookup result: same identity, route and session, selectable and pinnable.
  const terminalRow = pinned => `<div class="global-biz-row"><button class="item agent global-biz-agent global-workspace-terminal" type="button" data-global-agent="${GLOBAL_WORKSPACE_TERMINAL}" aria-pressed="false">
      <span class="nm">${GLOBAL_TERMINAL_ICON}<span class="tt">Terminal</span></span>
    </button>${pinControl(GLOBAL_WORKSPACE_TERMINAL, 'Terminal', pinned)}</div>`;
  const byBusinessThenName = (a, b) => compare(a.biz.name || a.biz.slug, b.biz.name || b.biz.slug)
    || compare(a.agent.display_name || a.agent.name, b.agent.display_name || b.agent.name) || compare(a.id, b.id);
  // The collapsed list keeps its scroll offset: a hidden scroller reports none.
  let bizScroll = 0;
  const paintBusinessAgents = () => {
    const tokens = globalNormalize(search.value).split(' ').filter(Boolean), pins = globalPinnedAgents();
    const all = businessItems();
    // Pins lead the navigation whatever the lookup says; only the lookup results are filtered.
    const promoted = all.filter(item => pins.has(item.id)).sort(byBusinessThenName), terminalPinned = pins.has(GLOBAL_WORKSPACE_TERMINAL);
    const rows = all.filter(item => !pins.has(item.id) && tokens.every(token => item.haystack.includes(token))).sort(byBusinessThenName);
    const terminalFound = !terminalPinned && tokens.every(token => GLOBAL_TERMINAL_HAYSTACK.includes(token));
    const failed = [...rosters.values()].filter(roster => roster.error);
    bizStatus.hidden = !failed.length;
    bizStatus.innerHTML = failed.length ? `<span>Could not load agents for ${failed.map(roster => esc(roster.biz.name || roster.biz.slug)).join(', ')}.</span><button type="button" class="quiet" data-global-biz-retry>Retry</button>` : '';
    const note = rows.length ? '' : !rosterLoaded ? 'Loading business agents…'
      : tokens.length && !terminalFound ? 'No matching agents'
      : !all.length ? (failed.length ? '' : 'No active business agents.')
      : !tokens.length && !terminalFound ? 'Everything is pinned.' : '';
    const scroll = bizRegion.hidden ? bizScroll : bizList.scrollTop;
    pinnedList.innerHTML = promoted.map(item => businessRow(item, true)).join('') + (terminalPinned ? terminalRow(true) : '');
    bizList.innerHTML = (terminalFound ? terminalRow(false) : '') + rows.map(item => businessRow(item, false)).join('')
      + (note ? `<p class="empty pad" role="status">${note}</p>` : '');
    if (bizRegion.hidden) bizScroll = scroll; else bizList.scrollTop = scroll;
    agentNavPaint(); paintSelection();
    if (typeof replyNotifications !== 'undefined') replyNotifications.paint();
  };
  const setBizCollapsed = (collapsed, remember = true) => {
    if (collapsed && !bizRegion.hidden) bizScroll = bizList.scrollTop;
    nav.classList.toggle('biz-collapsed', collapsed);
    bizRegion.hidden = search.hidden = collapsed;
    const label = collapsed ? GLOBAL_LOOKUP_LABEL : 'Collapse agent lookup';
    bizToggle.setAttribute('aria-expanded', String(!collapsed));
    bizToggle.setAttribute('aria-label', label);
    if (!collapsed) bizList.scrollTop = bizScroll;
    if (remember) ls(GLOBAL_BIZ_COLLAPSED_KEY, collapsed ? '1' : '0');
  };
  // Collapsed until the owner says otherwise; opening goes straight to typing, closing returns to the control.
  setBizCollapsed(ls(GLOBAL_BIZ_COLLAPSED_KEY) !== '0', false);
  bizToggle.onclick = () => {
    const collapse = !bizRegion.hidden;
    setBizCollapsed(collapse);
    (collapse ? bizToggle : search).focus();
  };
  search.oninput = paintBusinessAgents;
  search.onkeydown = event => {
    if (event.key !== 'Escape') return;
    event.stopPropagation();
    if (search.value) { search.value = ''; paintBusinessAgents(); }
    else { setBizCollapsed(true); bizToggle.focus(); }
  };
  bizStatus.onclick = event => {
    if (!event.target.closest('[data-global-biz-retry]')) return;
    const retry = [...rosters.values()].filter(roster => roster.error).map(roster => roster.biz.slug);
    retry.forEach(slug => rosters.delete(slug)); rosterLoaded = false;
    paintBusinessAgents(); loadBusinesses(retry);
  };
  // Pinned and lookup rows share one handler. Pinning only moves a row: it never selects or remounts anything.
  let pinSaving = false;
  const pinBusinessAgent = async id => {
    if (pinSaving) return;
    pinSaving = true;
    const wasPinned = globalPinnedAgents().has(id);
    try {
      await globalSavePin(id, !wasPinned);
      if (!current()) return;
      paintBusinessAgents();
      const control = root => root.querySelector(`[data-pin-agent="${CSS.escape(id)}"]`);
      if (!wasPinned) control(pinnedList)?.focus();
      else if (bizRegion.hidden) bizToggle.focus();
      else (control(bizList) || search).focus();
    } catch (error) {
      toast('Could not save pin. Please try again.');
    } finally { pinSaving = false; }
  };
  const businessClick = event => {
    const pin = event.target.closest('[data-pin-agent]');
    if (pin) { pinBusinessAgent(pin.dataset.pinAgent); return; }
    const terminal = event.target.closest('[data-global-agent]');
    if (terminal) {
      if (event.ctrlKey || event.metaKey) { event.preventDefault(); agentOpenTab(GLOBAL_WORKSPACE_TERMINAL); return; }
      select(GLOBAL_WORKSPACE_TERMINAL);
      return;
    }
    const button = event.target.closest('[data-business-agent]');
    if (!button) return;
    if (event.ctrlKey || event.metaKey) { event.preventDefault(); agentOpenTab(button.dataset.businessAgent); return; }
    const item = businessItems().find(candidate => candidate.id === button.dataset.businessAgent);
    if (item) selectBusiness(item);
  };
  bizList.onclick = pinnedList.onclick = businessClick;
  paintBusinessAgents();

  // ---- selection
  // Global Chat displays the same native conversation as Terminal. Switching views only changes the UI.
  // One shared lifecycle (global-chat.js) serves Global agents and registered business agents.
  const disposeChat = () => agentViewDispose();
  const select = (agent, requestedView = null, remember = true, leaveChecked = false) => {
    if (!current()) return;
    selectionVersion++;
    const workspace = typeof agent === 'string' ? globalWorkspaceTerminal(agent) : null;
    const id = workspace ? workspace.id : agent?.agent_id;
    if (!id) return;
    if (selected === id) {
      if (requestedView && _agpad?.agentId === id) agpadRequestView(_agpad, requestedView);
      return;
    }
    if (!leaveChecked && typeof agentWorkspaceCanLeave === 'function' && _agpad?.workspaceFrame) {
      const previous = _agpad;
      return agentWorkspaceCanLeave(previous).then(ok => { if (ok && _agpad === previous) select(agent, requestedView, remember, true); });
    }
    selected = id;
    if (remember) ls('ag:scope:global', selected);
    paintSelection();
    pane.dataset.agentPane = workspace ? 'off' : 'on';
    disposeChat();
    if (!workspace) mountAgentView(pane, agent, {terminal: {key: agent.agent_id, base: `/root/terminal/${encodeURIComponent(agent.agent_id)}`}, isCurrent: () => current() && selected === agent.agent_id});
    else mountTerminal(selected, pane, () => {}, workspace.base);
    agpadMount(workspace ? '' : (agent.display_name || agent.name), $('#agpad', c), workspace ? {
      biz: null, open: false, isCurrent: () => false,
    } : {
      biz: null, agentId: agent.agent_id, workspaceScope: 'global', workspaceRole: agent.role?.id || null,
      businessGrants: agent.scope?.allowed_business_ids || [],
      initialView: requestedView, open: true,
      isCurrent: () => current() && selected === agent.agent_id,
    });
  };
  // A business agent keeps the Global screen: its own business terminal and a business-scoped pane.
  const selectBusiness = ({id, biz, agent}, requestedView = null, remember = true, leaveChecked = false) => {
    if (!current()) return;
    selectionVersion++;
    if (selected === id) {
      if (requestedView && _agpad?.agentId === id) agpadRequestView(_agpad, requestedView);
      return;
    }
    if (!leaveChecked && typeof agentWorkspaceCanLeave === 'function' && _agpad?.workspaceFrame) {
      const previous = _agpad;
      return agentWorkspaceCanLeave(previous).then(ok => { if (ok && _agpad === previous) selectBusiness({id, biz, agent}, requestedView, remember, true); });
    }
    selected = id;
    if (remember) ls('ag:scope:global', id);
    paintSelection();
    pane.dataset.agentPane = 'on';
    disposeChat();
    // Registered business agents chat through the same universal endpoint, by their registered id; Terminal is the exact business route.
    mountAgentView(pane, {...agent, agent_id: id}, {terminal: {key: agent.name, base: `/biz/${encodeURIComponent(biz.slug)}/terminal/${encodeURIComponent(agent.name)}`}, isCurrent: () => current() && selected === id});
    agpadMount(agent.name, $('#agpad', c), {
      biz: biz.slug, agentId: id, workspaceScope: 'business', workspaceRole: agent.role?.id || agent.role_id || null,
      initialView: requestedView, open: ls(`agpad:${biz.slug}`) !== 'off',
      isCurrent: () => current() && selected === id,
    });
  };
  list.onclick = event => {
    const button = event.target.closest('[data-global-agent]');
    if (!button) return;
    if (event.ctrlKey || event.metaKey) { event.preventDefault(); agentOpenTab(button.dataset.globalAgent); return; }
    select(agents.find(a => a.agent_id === button.dataset.globalAgent));
  };
  const linkedAgent = params.get('agent');
  if (linkedAgent) { const url = new URL(location.href); url.searchParams.delete('agent'); history.replaceState(null, '', url); }
  const remembered = linkedAgent || (linkedFocus ? agents.find(a => a.role?.id === 'ordubashy')?.agent_id : ls('ag:scope:global'));
  const roster = loadBusinesses(businesses.map(biz => biz.slug));
  $('.global-agents', c).openAgent = async (id, {chat = false, view = null, viewboxItem = null} = {}) => {
    const version = ++selectionVersion;
    const agent = agents.find(candidate => candidate.agent_id === id);
    if (!agent && !rosterLoaded) await roster;
    if (!current() || version !== selectionVersion) return;
    const business = !agent && businessItems().find(candidate => candidate.id === id);
    if (!agent && !business) throw new Error('This agent is no longer available in the agent list.');
    if (selected === id) {
      if (view && _agpad?.agentId === id && _agpad.view !== view) await agpadSetView(_agpad, view);
    } else if (agent) await select(agent, view);
    else await selectBusiness(business, view);
    // A cancelled unsaved-edit prompt or a newer selection must stay where it is.
    if (!current() || selected !== id || (view && _agpad?.view !== view)) return;
    if (viewboxItem) agviewboxReveal(_agpad, viewboxItem);
    if (chat) {
      pane.querySelector('[data-chat-return]:not(:disabled)')?.click();
      pane.querySelector('.gchat-jump')?.click();
      pane.querySelector('.gchat-composer textarea')?.focus({preventScroll: true});
    }
  };
  const global = globalWorkspaceTerminal(remembered) ? GLOBAL_WORKSPACE_TERMINAL : agents.find(a => a.agent_id === remembered);
  const fallback = () => agents.find(a => a.role?.id === 'ordubashy') || agents[0] || GLOBAL_WORKSPACE_TERMINAL;
  if (global || !remembered || !businesses.length) select(global || fallback(), linkedWorkspace);
  else {
    // A saved or linked business agent is resolved from the business rosters, so the default
    // agent is never mounted first. A failed roster does not overwrite the saved choice.
    await roster;
    if (!current() || selected !== undefined) return;
    const item = businessItems().find(candidate => candidate.id === remembered);
    if (item) selectBusiness(item, linkedWorkspace);
    else select(fallback(), linkedWorkspace, ![...rosters.values()].some(entry => entry.error));
  }
}
