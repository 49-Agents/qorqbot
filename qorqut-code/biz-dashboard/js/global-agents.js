// Global profiles have their own roster and terminal identity, with no business selection.
let globalAgentsGeneration = 0;
const GLOBAL_WORKSPACE_TERMINAL = '__qorqut_workspace_terminal__';
const GLOBAL_TERMINAL_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/></svg>';
async function renderGlobalAgents(c) {
  const generation = ++globalAgentsGeneration;
  const current = () => generation === globalAgentsGeneration && c.isConnected
    && state.scope === 'global' && state.globalTab === 'agents';
  c.innerHTML = '<section class="pane global-content"><div class="global-eyebrow">Global</div><h1>Agents</h1><p class="muted" role="status">Loading agents…</p></section>';
  let agents, terms;
  try {
    const [roster, terminals] = await Promise.all([api('/root/agents'), api('/root/terminals')]);
    agents = coreData(roster).agents;
    if (!Array.isArray(agents)) throw new Error('The Global agent list is unavailable.');
    if (!Array.isArray(terminals)) throw new Error(terminals.error || 'Terminal status is unavailable.');
    terms = terminals;
  } catch (error) {
    if (!current()) return;
    c.innerHTML = `<section class="pane global-content"><div class="global-eyebrow">Global</div><h1>Agents</h1><p role="alert">${esc(error.message)}</p><button id="global-agents-retry">Retry</button></section>`;
    $('#global-agents-retry', c).onclick = () => renderGlobalAgents(c);
    return;
  }
  if (!current()) return;
  c.innerHTML = `<div class="split global-agents agsplit">
    <div class="listcol"><div class="ctop"><div class="global-eyebrow">Global</div><h1>Agents</h1></div><div class="crows" id="global-agent-list"></div></div>
    <section class="global-agent-session" aria-label="Agent session"><div class="chat termmode" id="global-agent-terminal"></div></section>
    <aside class="tasks agpad" id="agpad" aria-label="Agent pane"></aside>
  </div>`;
  const list = $('#global-agent-list', c), pane = $('#global-agent-terminal', c);
  list.innerHTML = `<button class="item agent global-workspace-terminal" type="button" data-global-agent="${GLOBAL_WORKSPACE_TERMINAL}">
    <span class="nm">${GLOBAL_TERMINAL_ICON}<span class="tt">Terminal</span></span>
    <span class="ldot" data-global-agent-status title="Idle"></span>
  </button>` + agents.map(agent => `<button class="item agent" type="button" data-global-agent="${esc(agent.agent_id)}">
    <span class="nm">${identityAvatar("agent", agent.display_name || agent.name, {...agent, ...agent.presentation})}<span class="tt">${esc(agent.display_name || agent.name)}</span></span>
    <span class="ldot ${terms.some(t => t.agent_id === agent.agent_id && !t.done) ? 'live' : ''}" data-global-agent-status title="${terms.some(t => t.agent_id === agent.agent_id && !t.done) ? 'Running' : 'Idle'}"></span>
  </button>`).join('');
  let selected;
  const select = agent => {
    if (!current()) return;
    const workspace = agent === GLOBAL_WORKSPACE_TERMINAL;
    selected = workspace ? GLOBAL_WORKSPACE_TERMINAL : agent.agent_id;
    ls('ag:scope:global', selected);
    list.querySelectorAll('[data-global-agent]').forEach(button => {
      const on = button.dataset.globalAgent === selected;
      button.classList.toggle('on', on); button.setAttribute('aria-pressed', String(on));
    });
    pane.dataset.agentPane = workspace ? 'off' : 'on';
    mountTerminal(selected, pane, status => {
      if (!current() || selected !== (workspace ? GLOBAL_WORKSPACE_TERMINAL : agent.agent_id)) return;
      const button = [...list.querySelectorAll('[data-global-agent]')].find(b => b.dataset.globalAgent === selected);
      const dot = $('[data-global-agent-status]', button);
      dot.classList.toggle('live', status === 'live');
      dot.title = status === 'live' ? 'Running' : status === 'ended' ? 'Exited' : 'Disconnected';
    }, workspace ? '/root/workspace-terminal' : `/root/terminal/${encodeURIComponent(agent.agent_id)}`);
    agpadMount(workspace ? '' : (agent.display_name || agent.name), $('#agpad', c), workspace ? {
      biz: null, open: false, isCurrent: () => false,
    } : {
      biz: null, agentId: agent.agent_id, views: ['inbox', 'memory'], open: ls('agpad:null') !== 'off',
      isCurrent: () => current() && selected === agent.agent_id,
    });
  };
  list.querySelectorAll('[data-global-agent]').forEach(button => {
    button.onclick = () => select(button.dataset.globalAgent === GLOBAL_WORKSPACE_TERMINAL
      ? GLOBAL_WORKSPACE_TERMINAL : agents.find(a => a.agent_id === button.dataset.globalAgent));
  });
  const remembered = ls('ag:scope:global');
  select(remembered === GLOBAL_WORKSPACE_TERMINAL ? GLOBAL_WORKSPACE_TERMINAL
    : agents.find(a => a.agent_id === remembered) || agents[0] || GLOBAL_WORKSPACE_TERMINAL);
}
