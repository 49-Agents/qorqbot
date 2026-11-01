// Global profiles have their own roster and terminal identity, with no business selection.
let globalAgentsGeneration = 0;
const GLOBAL_WORKSPACE_TERMINAL = '__qorqut_workspace_terminal__';
// Three owner shells; Terminal 1 keeps the original id, route and tmux session.
const GLOBAL_WORKSPACE_TERMINALS = [1, 2, 3].map(n => ({ id: n === 1 ? GLOBAL_WORKSPACE_TERMINAL : `__qorqut_workspace_terminal_${n}__`,
  label: `Terminal ${n}`, base: n === 1 ? '/root/workspace-terminal' : `/root/workspace-terminal/${n}` }));
const globalWorkspaceTerminal = id => GLOBAL_WORKSPACE_TERMINALS.find(t => t.id === id) || null;
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
    <div class="listcol"><div class="ctop"><div class="global-eyebrow">Global</div><div class="agent-nav-heading"><h1>Agents</h1><span class="agent-nav-actions">${broadcastButton()}${agentNavButton()}</span></div></div><div class="crows" id="global-agent-list"></div></div>
    <section class="global-agent-session" aria-label="Agent session"><div class="chat termmode" id="global-agent-terminal"></div></section>
    <div class="agent-pane-divider" data-agent-pane-divider role="separator" aria-label="Terminal and agent pane divider" aria-orientation="vertical" tabindex="0"></div>
    <aside class="tasks agpad" id="agpad" aria-label="Agent pane"></aside>
  </div>`;
  wireBroadcastButton(c, {scope:'global'});
  wireAgentNavToggle(c, 'global');
  const list = $('#global-agent-list', c), pane = $('#global-agent-terminal', c);
  list.innerHTML = agents.map(agent => `<button class="item agent" type="button" data-global-agent="${esc(agent.agent_id)}">
    <span class="nm">${identityAvatar("agent", agent.display_name || agent.name, {...agent, ...agent.presentation})}<span class="tt">${esc(agent.display_name || agent.name)}</span></span>
    <span class="agent-presence" data-agent-presence="${esc(agent.agent_id)}" role="img"></span>
  </button>`).join('') + `<div class="global-workspace-terminals">${GLOBAL_WORKSPACE_TERMINALS.map(t => `<button class="item agent global-workspace-terminal" type="button" data-global-agent="${t.id}">
    <span class="nm">${GLOBAL_TERMINAL_ICON}<span class="tt">${t.label}</span></span>
    <span class="ldot" data-global-agent-status title="Terminal not connected"></span>
  </button>`).join('')}</div>`;
  agentNavPaint(); agentNavRefresh();
  const params = new URLSearchParams(location.search);
  const linkedFocus = params.has('focus_item');
  const linkedWorkspace = agTakeLinkedWorkspace('#/global/agents');
  let selected;
  const select = (agent, requestedView = null) => {
    if (!current()) return;
    const workspace = typeof agent === 'string' ? globalWorkspaceTerminal(agent) : null;
    selected = workspace ? workspace.id : agent.agent_id;
    ls('ag:scope:global', selected);
    list.querySelectorAll('[data-global-agent]').forEach(button => {
      const on = button.dataset.globalAgent === selected;
      button.classList.toggle('on', on); button.setAttribute('aria-pressed', String(on));
    });
    pane.dataset.agentPane = workspace ? 'off' : 'on';
    mountTerminal(selected, pane, status => {
      if (!current() || selected !== (workspace ? workspace.id : agent.agent_id)) return;
      const button = [...list.querySelectorAll('[data-global-agent]')].find(b => b.dataset.globalAgent === selected);
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
  list.querySelectorAll('[data-global-agent]').forEach(button => {
    button.onclick = event => {
      if(event.ctrlKey || event.metaKey){event.preventDefault();agentOpenTab(button.dataset.globalAgent);return;}
      select(globalWorkspaceTerminal(button.dataset.globalAgent)
        ? button.dataset.globalAgent : agents.find(a => a.agent_id === button.dataset.globalAgent));
    };
  });
  const linkedAgent=params.get('agent');
  if(linkedAgent){const url=new URL(location.href);url.searchParams.delete('agent');history.replaceState(null,'',url);}
  const remembered = linkedAgent || (linkedFocus ? agents.find(a=>a.role?.id==='ordubashy')?.agent_id : ls('ag:scope:global'));
  select(globalWorkspaceTerminal(remembered) ? remembered
    : agents.find(a => a.agent_id === remembered) || agents.find(a => a.role?.id === 'ordubashy') || agents[0] || GLOBAL_WORKSPACE_TERMINAL, linkedWorkspace);
}
