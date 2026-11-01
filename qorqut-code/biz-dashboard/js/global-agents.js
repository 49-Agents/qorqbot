// Global profiles have their own roster and terminal identity, with no business selection.
// Registered business agents can also be picked here; the Global screen stays put.
let globalAgentsGeneration = 0;
const GLOBAL_WORKSPACE_TERMINAL = '__qorqut_workspace_terminal__';
// One owner shell keeps the original id, route and tmux session. Older saved or
// linked Terminal 2/3 selections open it instead; their sessions and routes are untouched.
const globalWorkspaceTerminal = id => /^__qorqut_workspace_terminal(?:_[23])?__$/.test(id || '')
  ? {id: GLOBAL_WORKSPACE_TERMINAL, label: 'Terminal', base: '/root/workspace-terminal'} : null;
const GLOBAL_PINS_KEY = 'ag:global-pins:v1';
const globalPinnedAgents = () => {
  try { const ids = JSON.parse(ls(GLOBAL_PINS_KEY) || '[]'); return new Set(Array.isArray(ids) ? ids.filter(id => typeof id === 'string') : []); }
  catch (_) { return new Set(); }
};
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
const GLOBAL_TERMINAL_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/></svg>';
async function renderGlobalAgents(c) {
  const generation = ++globalAgentsGeneration;
  const current = () => generation === globalAgentsGeneration && c.isConnected
    && state.scope === 'global' && state.globalTab === 'agents';
  c.innerHTML = '<section class="pane global-content"><div class="global-eyebrow">Global</div><h1>Agents</h1><p class="muted" role="status">Loading agents…</p></section>';
  let agents;
  try {
    agents = globalAgentProfiles.length ? globalAgentProfiles : await loadGlobalAgentProfiles();
    if (!Array.isArray(agents)) throw new Error('The Global agent list is unavailable.');
  } catch (error) {
    if (!current()) return;
    c.innerHTML = `<section class="pane global-content"><div class="global-eyebrow">Global</div><h1>Agents</h1><p role="alert">${esc(error.message)}</p><button id="global-agents-retry">Retry</button></section>`;
    $('#global-agents-retry', c).onclick = () => renderGlobalAgents(c);
    return;
  }
  if (!current()) return;
  c.innerHTML = `<div class="split global-agents agsplit">
    <div class="listcol"><div class="ctop"><div class="global-eyebrow">Global</div><div class="agent-nav-heading"><h1>Agents</h1><span class="agent-nav-actions">${broadcastButton()}${agentNavButton()}</span></div></div>
      <div class="crows global-roster" id="global-agent-list"></div>
      <section class="global-biz-nav" aria-label="Business agents">
        <input type="search" class="global-biz-search" id="global-biz-search" aria-label="Search business agents" placeholder="Search agents…" autocomplete="off" spellcheck="false">
        <div class="global-biz-status" id="global-biz-status" role="status" hidden></div>
        <div class="crows global-biz-list" id="global-biz-agent-list"></div>
        <div class="global-workspace-terminals"><button class="item agent global-workspace-terminal" type="button" data-global-agent="${GLOBAL_WORKSPACE_TERMINAL}">
          <span class="nm">${GLOBAL_TERMINAL_ICON}<span class="tt">Terminal</span></span>
          <span class="ldot" data-global-agent-status title="Terminal not connected"></span>
        </button></div>
      </section></div>
    <section class="global-agent-session" aria-label="Agent session"><div class="chat termmode" id="global-agent-terminal"></div></section>
    <div class="agent-pane-divider" data-agent-pane-divider role="separator" aria-label="Terminal and agent pane divider" aria-orientation="vertical" tabindex="0"></div>
    <aside class="tasks agpad" id="agpad" aria-label="Agent pane"></aside>
  </div>`;
  wireBroadcastButton(c, {scope:'global'});
  wireAgentNavToggle(c, 'global');
  const list = $('#global-agent-list', c), pane = $('#global-agent-terminal', c), nav = $('.listcol', c);
  const bizList = $('#global-biz-agent-list', c), search = $('#global-biz-search', c), bizStatus = $('#global-biz-status', c);
  list.innerHTML = agents.map(agent => `<button class="item agent" type="button" data-global-agent="${esc(agent.agent_id)}">
    <span class="nm">${identityAvatar("agent", agent.display_name || agent.name, {...agent, ...agent.presentation})}<span class="tt">${esc(agent.display_name || agent.name)}</span></span>
    <span class="agent-presence" data-agent-presence="${esc(agent.agent_id)}" role="img"></span>
  </button>`).join('');
  agentNavPaint(); agentNavRefresh();
  const params = new URLSearchParams(location.search);
  const linkedFocus = params.has('focus_item');
  const linkedWorkspace = agTakeLinkedWorkspace('#/global/agents');

  // ---- registered business agents: read once per view (and per explicit retry), searched in memory
  const businesses = (state.bizs || []).filter(biz => biz && biz.slug);
  const rosters = new Map();                       // business slug -> {biz, agents} | {biz, error}
  let rosterLoaded = !businesses.length, selected;
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
  const paintBusinessAgents = () => {
    const tokens = globalNormalize(search.value).split(' ').filter(Boolean), pins = globalPinnedAgents();
    const all = businessItems();
    const rows = all.filter(item => tokens.every(token => item.haystack.includes(token)))
      .sort((a, b) => (pins.has(b.id) - pins.has(a.id)) || compare(a.biz.name || a.biz.slug, b.biz.name || b.biz.slug)
        || compare(a.agent.display_name || a.agent.name, b.agent.display_name || b.agent.name) || compare(a.id, b.id));
    const failed = [...rosters.values()].filter(roster => roster.error);
    bizStatus.hidden = !failed.length;
    bizStatus.innerHTML = failed.length ? `<span>Could not load agents for ${failed.map(roster => esc(roster.biz.name || roster.biz.slug)).join(', ')}.</span><button type="button" class="quiet" data-global-biz-retry>Retry</button>` : '';
    const scroll = bizList.scrollTop;
    bizList.innerHTML = rows.map(({id, biz, agent}) => {
      const name = agent.display_name || agent.name, business = biz.name || biz.slug, department = dept(agent.department);
      const pinned = pins.has(id);
      return `<div class="global-biz-row"><button class="item agent global-biz-agent" type="button" data-business-agent="${esc(id)}" aria-pressed="false" aria-label="${esc(`${name}, ${business}, ${department.label}`)}">
        <span class="nm"><span class="global-biz-icons" aria-hidden="true">${identityAvatar('business', business, {}, business)}<span class="identity-avatar identity-department" style="--department-color:${department.color}">${departmentIcon(agent.department, 11)}</span></span>
          <span class="global-biz-text"><span class="tt">${esc(name)}</span><span class="global-biz-name">${esc(business)}</span></span></span>
        <span class="agent-presence" data-agent-presence="${esc(id)}" role="img"></span>
      </button><button type="button" class="quiet global-biz-pin" data-pin-agent="${esc(id)}" aria-pressed="${pinned}" aria-label="${pinned ? 'Unpin' : 'Pin'} ${esc(name)}, ${esc(business)}" title="${pinned ? 'Unpin' : 'Pin'}">${ICO_PIN}</button></div>`;
    }).join('') || `<p class="empty pad" role="status">${!rosterLoaded ? 'Loading business agents…' : all.length ? 'No matching agents' : failed.length ? '' : 'No active business agents.'}</p>`;
    bizList.scrollTop = scroll;
    agentNavPaint(); paintSelection();
  };
  search.oninput = paintBusinessAgents;
  search.onkeydown = event => { if (event.key === 'Escape' && search.value) { event.stopPropagation(); search.value = ''; paintBusinessAgents(); } };
  bizStatus.onclick = event => {
    if (!event.target.closest('[data-global-biz-retry]')) return;
    const retry = [...rosters.values()].filter(roster => roster.error).map(roster => roster.biz.slug);
    retry.forEach(slug => rosters.delete(slug)); rosterLoaded = false;
    paintBusinessAgents(); loadBusinesses(retry);
  };
  bizList.onclick = event => {
    const pin = event.target.closest('[data-pin-agent]');
    if (pin) {
      const pins = globalPinnedAgents(), id = pin.dataset.pinAgent;
      if (!pins.delete(id)) pins.add(id);
      ls(GLOBAL_PINS_KEY, JSON.stringify([...pins]));
      paintBusinessAgents();
      bizList.querySelector(`[data-pin-agent="${CSS.escape(id)}"]`)?.focus();
      return;
    }
    const button = event.target.closest('[data-business-agent]');
    if (!button) return;
    if (event.ctrlKey || event.metaKey) { event.preventDefault(); agentOpenTab(button.dataset.businessAgent); return; }
    const item = businessItems().find(candidate => candidate.id === button.dataset.businessAgent);
    if (item) selectBusiness(item);
  };
  paintBusinessAgents();

  // ---- selection
  const select = (agent, requestedView = null, remember = true) => {
    if (!current()) return;
    const workspace = typeof agent === 'string' ? globalWorkspaceTerminal(agent) : null;
    selected = workspace ? workspace.id : agent.agent_id;
    if (remember) ls('ag:scope:global', selected);
    paintSelection();
    pane.dataset.agentPane = workspace ? 'off' : 'on';
    mountTerminal(selected, pane, status => {
      if (!current() || selected !== (workspace ? workspace.id : agent.agent_id)) return;
      const button = [...list.querySelectorAll('[data-global-agent]')].find(b => b.dataset.globalAgent === selected) || $('.global-workspace-terminal', c);
      const dot = $('[data-global-agent-status]', button);
      if (!dot) return;
      dot.classList.toggle('live', status === 'live');
      dot.title = status === 'live' ? 'Terminal connected' : status === 'ended' ? 'Terminal exited' : 'Terminal disconnected';
    }, workspace ? workspace.base : `/root/terminal/${encodeURIComponent(agent.agent_id)}`);
    agpadMount(workspace ? '' : (agent.display_name || agent.name), $('#agpad', c), workspace ? {
      biz: null, open: false, isCurrent: () => false,
    } : {
      biz: null, agentId: agent.agent_id, workspaceScope: 'global', workspaceRole: agent.role?.id || null,
      businessGrants: agent.scope?.allowed_business_ids || [],
      initialView: requestedView, open: linkedFocus || ls('agpad:null') !== 'off',
      isCurrent: () => current() && selected === agent.agent_id,
    });
  };
  // A business agent keeps the Global screen: its own business terminal and a business-scoped pane.
  const selectBusiness = ({id, biz, agent}, requestedView = null, remember = true) => {
    if (!current()) return;
    selected = id;
    if (remember) ls('ag:scope:global', id);
    paintSelection();
    pane.dataset.agentPane = 'on';
    mountTerminal(agent.name, pane, () => {}, `/biz/${encodeURIComponent(biz.slug)}/terminal/${encodeURIComponent(agent.name)}`);
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
  $('.global-workspace-terminal', c).onclick = event => {
    if (event.ctrlKey || event.metaKey) { event.preventDefault(); agentOpenTab(GLOBAL_WORKSPACE_TERMINAL); return; }
    select(GLOBAL_WORKSPACE_TERMINAL);
  };
  const linkedAgent = params.get('agent');
  if (linkedAgent) { const url = new URL(location.href); url.searchParams.delete('agent'); history.replaceState(null, '', url); }
  const remembered = linkedAgent || (linkedFocus ? agents.find(a => a.role?.id === 'ordubashy')?.agent_id : ls('ag:scope:global'));
  const roster = loadBusinesses(businesses.map(biz => biz.slug));
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
