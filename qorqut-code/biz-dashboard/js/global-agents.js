// Global profiles have their own roster and terminal identity, with no business selection.
let globalAgentsGeneration = 0;
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
  c.innerHTML = `<div class="split global-agents">
    <div class="listcol"><div class="ctop"><div class="global-eyebrow">Global</div><h1>Agents</h1></div><div class="crows" id="global-agent-list"></div></div>
    <section class="global-agent-session" aria-label="Agent session"><header class="global-agent-heading" id="global-agent-heading"></header><div class="chat termmode" id="global-agent-terminal"></div></section>
  </div>`;
  if (!agents.length) {
    $('#global-agent-list', c).innerHTML = '<p class="empty pad">No global agents yet.</p>';
    $('#global-agent-heading', c).textContent = 'Registered global agents will appear here.';
    return;
  }
  const list = $('#global-agent-list', c), pane = $('#global-agent-terminal', c);
  list.innerHTML = agents.map(agent => `<button class="item agent" type="button" data-global-agent="${esc(agent.agent_id)}">
    <span class="nm">${globalIcon('agents')}<span class="tt">${esc(agent.display_name || agent.name)}</span></span>
    <span class="ldot ${terms.some(t => t.agent_id === agent.agent_id && !t.done) ? 'live' : ''}" data-global-agent-status title="${terms.some(t => t.agent_id === agent.agent_id && !t.done) ? 'Running' : 'Idle'}"></span>
  </button>`).join('');
  let selected;
  const select = agent => {
    if (!current()) return;
    selected = agent.agent_id;
    ls('ag:scope:global', selected);
    list.querySelectorAll('[data-global-agent]').forEach(button => {
      const on = button.dataset.globalAgent === selected;
      button.classList.toggle('on', on); button.setAttribute('aria-pressed', String(on));
    });
    $('#global-agent-heading', c).innerHTML = `<h2>${esc(agent.display_name || agent.name)}</h2><p>${esc(agent.assignment_brief)}</p><p class="muted">Type <code>claude</code> in the terminal to begin, or <code>claude --continue</code> to resume.</p>`;
    mountTerminal(agent.agent_id, pane, status => {
      if (!current() || selected !== agent.agent_id) return;
      const button = [...list.querySelectorAll('[data-global-agent]')].find(b => b.dataset.globalAgent === selected);
      const dot = $('[data-global-agent-status]', button);
      dot.classList.toggle('live', status === 'live');
      dot.title = status === 'live' ? 'Running' : status === 'ended' ? 'Exited' : 'Disconnected';
    }, `/root/terminal/${encodeURIComponent(agent.agent_id)}`);
  };
  list.querySelectorAll('[data-global-agent]').forEach(button => {
    button.onclick = () => select(agents.find(a => a.agent_id === button.dataset.globalAgent));
  });
  select(agents.find(a => a.agent_id === ls('ag:scope:global')) || agents[0]);
}
