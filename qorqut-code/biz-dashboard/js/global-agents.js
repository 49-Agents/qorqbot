// The Global Agents page lists the supported global agents (including every permanent worker) and the owner's workspace Terminal.
// Business agents are not resident: they are never listed, selected or started here.
let globalAgentsGeneration = 0;
let globalTestingModeUnsubscribe = null;
const OVERSEER_ROLE = 'learning-applier';
const overseerRole = agent => (agent?.role?.id || agent?.role_id) === OVERSEER_ROLE;
const OVERSEER_UNAVAILABLE_LABEL = 'Overseer, available in testing mode only.';
const OVERSEER_UNAVAILABLE_TITLE = 'Overseer — Available in testing mode only';
// One page-level listener keeps every visible navigation face looking at the
// pointer, even over Chat. Only pointer/layout changes schedule a frame.
let globalAgentGazeRefresh;
function wireGlobalAgentGaze() {
  if (globalAgentGazeRefresh) { globalAgentGazeRefresh(); return; }
  let pointer = null, frame = 0;
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const paint = () => {
    frame = 0;
    document.querySelectorAll('#global-agent-list .qorqbot-agent').forEach(svg => {
      const box = svg.getBoundingClientRect();
      if (!box.width || !box.height) return;
      const dx = pointer ? pointer.x - (box.left + box.width / 2) : 0;
      const dy = pointer ? pointer.y - (box.top + box.height / 2) : 0;
      const factor = reducedMotion.matches ? 0 : box.width * .045 / Math.max(100, Math.hypot(dx, dy));
      svg.querySelectorAll('.qorqbot-gaze').forEach(eye => {
        const matrix = eye.parentElement.getScreenCTM();
        if (!matrix) return;
        const inverse = matrix.inverse();
        const x = (inverse.a * dx + inverse.c * dy) * factor;
        const y = (inverse.b * dx + inverse.d * dy) * factor;
        eye.setAttribute('transform', `translate(${x.toFixed(3)} ${y.toFixed(3)})`);
      });
    });
  };
  const schedule = () => { if (!frame) frame = requestAnimationFrame(paint); };
  globalAgentGazeRefresh = schedule;
  document.addEventListener('pointermove', event => {
    if (event.pointerType === 'touch') return;
    pointer = {x:event.clientX, y:event.clientY}; schedule();
  }, {passive:true});
  const reset = () => { pointer = null; schedule(); };
  document.documentElement.addEventListener('pointerleave', reset);
  window.addEventListener('blur', reset);
  window.addEventListener('resize', schedule, {passive:true});
  document.addEventListener('scroll', schedule, {passive:true, capture:true});
  reducedMotion.addEventListener('change', schedule);
  schedule();
}

// Attention links use the mounted roster's normal selection path. The page shell,
// menu and divider stay in place while the destination agent changes.
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
// Ordubashy and Learning Applier are singletons; any number of permanent workers share `global-worker`.
const GLOBAL_AGENT_ROLES = ['ordubashy', 'global-worker', 'learning-applier'];
// Keep the owner's primary agents first, independent of API order.
const globalAgentNavRank = agent => {
  const role = agent.role?.id || agent.role_id;
  if (role === 'ordubashy') return 0;
  if (role === 'learning-applier') return 1;
  return 2;
};
// Size varies by location; the saved profile and shared renderer select the artwork.
function globalAgentNavAvatar(agent) {
  const primary = primaryAgent(agent), name = agent.display_name || agent.name;
  return `<span class="identity-avatar identity-agent ${primary ? 'nav-primary-face' : 'nav-secondary-icon'}" role="img" aria-label="${esc(name)}">${agentGlyph(agent, primary ? 31.68 : 18)}</span>`;
}
// Retired profiles and archival records never reach the list.
const globalActiveProfile = agent => !!agent && typeof agent.agent_id === 'string' && !agent.archival_record && (agent.status === undefined || agent.status === 'active');
// A global profile has ecosystem scope and no home business; a business-scoped
// record claiming a supported role is refused here as the host refuses it.
const globalRosterAgent = agent => globalActiveProfile(agent) && GLOBAL_AGENT_ROLES.includes(agent.role?.id || agent.role_id)
  && agent.home_business_id == null && agent.scope?.kind === 'ecosystem';
// Retired or unknown agents are history, not something to open or start.
const GLOBAL_AGENT_UNAVAILABLE = 'This agent is no longer active. Its history is kept.';
// Question mark in a circle, matching the stroke weight of the Terminal and Settings icons.
const GLOBAL_GUIDE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.6 9.3a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.3-2.4 3.8"/><path d="M12 17h.01"/></svg>';
const GLOBAL_TERMINAL_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/></svg>';
// An agent profile link opens an active global agent; any other agent is history only.
async function agentProfileOpen(agentId) {
  if (embeddedAgentPane) return window.parent.agentWorkspaceEscape?.({agent: agentId});
  const agents = await loadGlobalAgentProfiles().catch(() => []);
  if (!agents.some(agent => agent.agent_id === agentId && globalRosterAgent(agent))) { toast(GLOBAL_AGENT_UNAVAILABLE); return; }
  ls('ag:scope:global', agentId);
  if (!await globalAgentsNavigate(agentId).catch(() => false)) await selectGlobal('agents');
}
async function renderGlobalAgents(c) {
  globalTestingModeUnsubscribe?.();
  globalTestingModeUnsubscribe = null;
  const generation = ++globalAgentsGeneration;
  const current = () => generation === globalAgentsGeneration && c.isConnected
    && state.scope === 'global' && state.globalTab === 'agents';
  c.innerHTML = '<section class="pane global-content"><div class="global-eyebrow">Global</div><h1>Agents</h1><p class="muted" role="status">Loading agents…</p></section>';
  let agents;
  try {
    // Read the owner preference together with the roster, before any Overseer
    // control is painted or its chat/terminal/workspace is mounted.
    const [profiles] = await Promise.all([
      globalAgentProfiles.length ? globalAgentProfiles : loadGlobalAgentProfiles(),
      testingModeReady(),
    ]);
    if (!Array.isArray(profiles)) throw new Error('The Global agent list is unavailable.');
    agents = profiles.filter(globalRosterAgent).sort((a, b) => globalAgentNavRank(a) - globalAgentNavRank(b));
  } catch (error) {
    if (!current()) return;
    c.innerHTML = `<section class="pane global-content"><div class="global-eyebrow">Global</div><h1>Agents</h1><p role="alert">${esc(uiFailure(error, {load: true}))}</p><button id="global-agents-retry">Retry</button></section>`;
    $('#global-agents-retry', c).onclick = () => renderGlobalAgents(c);
    return;
  }
  if (!current()) return;
  const rosterOverseer = agents.find(overseerRole) || null;
  c.innerHTML = `<div class="split global-agents agsplit">
    <div class="listcol"><div class="ctop"><div class="global-eyebrow">Global</div><div class="agent-nav-heading"><h1>Agents</h1><span class="agent-nav-actions">${agentNavButton()}</span></div></div>
      <div class="global-nav-body">
        <div class="crows global-primary" id="global-primary-roster">
          <div class="global-roster" id="global-agent-list"></div>
        </div>
        <div class="crows global-workspace-terminals">
          <button class="item agent global-workspace-guide" type="button" data-global-guide title="Walkthrough" aria-label="Walkthrough"><span class="nm">${GLOBAL_GUIDE_ICON}</span></button>
          <button class="item agent global-workspace-terminal" type="button" data-global-agent="${GLOBAL_WORKSPACE_TERMINAL}" aria-pressed="false" aria-label="Terminal"><span class="nm">${GLOBAL_TERMINAL_ICON}</span></button>
          <button class="item agent global-workspace-settings" type="button" data-global-settings title="Settings" aria-label="Settings"><span class="nm">${GLOBAL_ICONS.settings}</span></button>
        </div>
      </div></div>
    <section class="overseer-coming-soon" id="overseer-coming-soon" aria-label="Overseer" aria-live="polite" hidden>
      <div class="overseer-coming-soon-box">
        <h2>Overseer</h2>
        <p class="overseer-coming-soon-description">Overseer will be managing the agentic layer, tailoring Qorqbot for you on the lowest level.</p>
        <p class="overseer-coming-soon-hint">Available in testing mode only</p>
        <p class="overseer-coming-soon-error" role="alert" hidden></p>
        <button class="quiet overseer-coming-soon-retry" type="button" hidden>Retry</button>
      </div>
    </section>
    <section class="global-agent-session" aria-label="Agent session"><div class="chat termmode" id="global-agent-terminal"></div></section>
    <div class="agent-pane-divider" data-agent-pane-divider role="separator" aria-label="Terminal and agent pane divider" aria-orientation="vertical" tabindex="0"></div>
    <aside class="tasks agpad" id="agpad" aria-label="Agent pane"></aside>
  </div>`;
  wireAgentNavToggle(c, 'global');
  const list = $('#global-agent-list', c), pane = $('#global-agent-terminal', c), nav = $('.listcol', c);
  const comingSoon = $('#overseer-coming-soon', c);
  const comingSoonError = $('.overseer-coming-soon-error', comingSoon);
  const comingSoonRetry = $('.overseer-coming-soon-retry', comingSoon);
  // Icon-only rows: the agent name lives in the accessible label, with no visible text, tooltip or presence ball.
  // The unavailable Overseer row keeps its real avatar and gains an explicit hover/accessible status label.
  list.innerHTML = agents.map((agent, index) => {
    const gated = overseerRole(agent) && !testingModeEnabled();
    const name = agent.display_name || agent.name;
    return `${index > 0 && globalAgentNavRank(agents[index - 1]) < 2 && globalAgentNavRank(agent) >= 2
      ? '<span class="global-agent-group-divider" aria-hidden="true"></span>' : ''}<button class="item agent${gated ? ' overseer-coming-soon-entry' : ''}" type="button" data-global-agent="${esc(agent.agent_id)}" aria-pressed="false" aria-label="${esc(gated ? OVERSEER_UNAVAILABLE_LABEL : name)}"${gated ? ` title="${esc(OVERSEER_UNAVAILABLE_TITLE)}" data-overseer-coming-soon="1"` : ''}>
    <span class="nm">${globalAgentNavAvatar(agent)}</span><span class="agent-nav-working" data-agent-presence="${esc(agent.agent_id)}" role="img" aria-label="Working" hidden><i></i><i></i><i></i></span>
  </button>`.replace(/\s*\n\s*/g, '');
  }).join('');
  wireGlobalAgentGaze();
  agentNavPaint(); agentNavRefresh();
  if (typeof replyNotifications !== 'undefined') replyNotifications.paint();
  const params = new URLSearchParams(location.search);
  const linkedFocus = params.has('focus_item');
  const linkedWorkspace = agTakeLinkedWorkspace('#/global/agents');
  let selected, selectionVersion = 0;
  const paintSelection = () => {
    nav.querySelectorAll('[data-global-agent]').forEach(button => {
      const on = button.dataset.globalAgent === selected;
      button.classList.toggle('on', on); button.setAttribute('aria-pressed', String(on));
    });
  };

  // ---- selection
  // Global Chat displays the same native conversation as Terminal. Switching views only changes the UI.
  const disposeChat = () => agentViewDispose();
  const root = $('.global-agents', c);
  let gated = false;
  const paintComingSoonError = () => {
    const failed = testingModeFailed();
    comingSoonError.hidden = !failed;
    comingSoonError.textContent = failed ? testingModeMessage() : '';
    comingSoonRetry.hidden = !failed;
    comingSoonRetry.disabled = false;
  };
  const leaveComingSoon = () => {
    if (comingSoon.hidden) return;
    comingSoon.hidden = true;
    root.classList.remove('overseer-gated');
  };
  const enterComingSoon = () => {
    gated = true;
    // Release only this browser view. The backend agent session, terminal process,
    // permissions and saved data stay exactly as they were.
    disposeChat();
    if (typeof agpadDispose === 'function') agpadDispose();
    pane.innerHTML = '';
    pane.dataset.agentPane = 'off';
    root.classList.add('overseer-gated');
    comingSoon.hidden = false;
    paintComingSoonError();
  };
  const paintOverseerEntry = () => {
    if (!rosterOverseer) return;
    const row = [...list.querySelectorAll('[data-global-agent]')].find(button => button.dataset.globalAgent === rosterOverseer.agent_id);
    if (!row) return;
    const unavailable = !testingModeEnabled();
    row.classList.toggle('overseer-coming-soon-entry', unavailable);
    row.setAttribute('aria-label', unavailable ? OVERSEER_UNAVAILABLE_LABEL : (rosterOverseer.display_name || rosterOverseer.name));
    if (unavailable) {
      row.title = OVERSEER_UNAVAILABLE_TITLE;
      row.dataset.overseerComingSoon = '1';
    } else {
      row.removeAttribute('title');
      delete row.dataset.overseerComingSoon;
    }
  };
  paintOverseerEntry();
  const select = (agent, requestedView = null, remember = true, leaveChecked = false) => {
    if (!current()) return;
    selectionVersion++;
    const workspace = typeof agent === 'string' ? globalWorkspaceTerminal(agent) : null;
    const id = workspace ? workspace.id : agent?.agent_id;
    if (!id) return;
    const comingSoonTarget = !workspace && overseerRole(agent) && !testingModeEnabled();
    if (selected === id) {
      if (comingSoonTarget) {
        if (!gated) enterComingSoon();
        return;
      }
      if (gated) { gated = false; selected = null; }
      else {
        if (requestedView && _agpad?.agentId === id) agpadRequestView(_agpad, requestedView);
        return;
      }
    }
    if (!leaveChecked && typeof agentWorkspaceCanLeave === 'function' && (_agpad?.workspaceFrame || _agpad?.journalMount)) {
      const previous = _agpad, frame = previous.workspaceFrame, journal = previous.journalMount;
      return agentWorkspaceCanLeave(previous).then(ok => {
        if (ok && _agpad === previous && previous.workspaceFrame === frame && previous.journalMount === journal)
          select(agent, requestedView, remember, true);
      });
    }
    selected = id;
    if (remember) ls('ag:scope:global', selected);
    paintSelection();
    if (comingSoonTarget) { enterComingSoon(); return; }
    gated = false;
    leaveComingSoon();
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
  comingSoonRetry.onclick = async () => {
    comingSoonRetry.disabled = true;
    comingSoonError.hidden = false;
    comingSoonError.textContent = 'Retrying Testing mode…';
    await testingModeLoad(true);
    if (!current()) return;
    if (testingModeFailed()) { paintComingSoonError(); return; }
    renderTab();
  };
  // Another tab or device can change the owner preference. Close an active
  // Overseer view at once; the backend session is never stopped.
  globalTestingModeUnsubscribe = testingModeSubscribe(() => {
    if (!current()) return;
    const chosen = agents.find(agent => agent.agent_id === selected);
    if (chosen && overseerRole(chosen)) { renderTab(); return; }
    paintOverseerEntry();
  });
  nav.querySelector('[data-global-settings]').onclick = () => goGlobalTab('settings');
  nav.querySelector('[data-global-guide]').onclick = () => onboarding.open();
  nav.querySelector('.global-nav-body').onclick = event => {
    const button = event.target.closest('[data-global-agent]');
    if (!button) return;
    const id = button.dataset.globalAgent;
    if (event.ctrlKey || event.metaKey) { event.preventDefault(); agentOpenTab(id); return; }
    select(globalWorkspaceTerminal(id) ? GLOBAL_WORKSPACE_TERMINAL : agents.find(a => a.agent_id === id));
  };
  $('.global-agents', c).openAgent = async (id, {chat = false, view = null, viewboxItem = null} = {}) => {
    ++selectionVersion;
    if (!current()) return;
    const agent = agents.find(candidate => candidate.agent_id === id);
    if (!agent) throw new Error(GLOBAL_AGENT_UNAVAILABLE);
    const gateTarget = overseerRole(agent) && !testingModeEnabled();
    await select(agent, gateTarget ? null : view);
    // A cancelled unsaved-edit prompt, the Testing mode gate or a newer selection
    // must stay where it is; no chat, terminal or workspace control is focused.
    if (!current() || selected !== id || gated) return;
    if (!gateTarget && view && agpadShownView(_agpad) !== view) return;
    if (viewboxItem) agviewboxReveal(_agpad, viewboxItem);
    if (chat) {
      pane.querySelector('[data-chat-return]:not(:disabled)')?.click();
      pane.querySelector('.gchat-jump')?.click();
      pane.querySelector('.gchat-composer textarea')?.focus({preventScroll: true});
    }
  };
  const linkedAgent = params.get('agent');
  if (linkedAgent) { const url = new URL(location.href); url.searchParams.delete('agent'); history.replaceState(null, '', url); }
  let request = agentWorkspaceTakeRequest();
  // ?workspace=calendar opens the coordinator's Calendar view; without that agent it is explicitly unavailable.
  if (!request && linkedWorkspace === 'calendar') {
    const coordinator = agents.find(a => (a.role?.id || a.role_id) === 'ordubashy');
    if (!coordinator) { agentWorkspaceUnavailable(c, 'calendar'); return; }
    request = {agentId: coordinator.agent_id, view: 'calendar'};
  }
  const fresh = !!request;
  if (!request && !linkedAgent && !linkedFocus) request = agentWorkspaceRehydrate();
  const remembered = request?.agentId || linkedAgent || (linkedFocus ? agents.find(a => a.role?.id === 'ordubashy')?.agent_id : ls('ag:scope:global'));
  const target = globalWorkspaceTerminal(remembered) ? GLOBAL_WORKSPACE_TERMINAL : agents.find(a => a.agent_id === remembered);
  // A stored Business descriptor for an agent whose pane no longer offers it opens the shared Tables for a table selection,
  // and is otherwise dropped, leaving the agent on its own default view.
  if (request?.view === 'business' && target && !agentWorkspaceDescriptorRole('business', target.role?.id)) {
    if (request.section === 'tables' || request.table) globalThis.ovOpen?.('tables', ['scope', 'table', 'mode'].some(key => request[key])
      ? {scope: request.scope, id: request.table, mode: request.mode} : null);
    agentWorkspaceStore(null);
    request = null;
  }
  // A redirected Context URL never falls back to another agent's content.
  if (request && (!target || (!fresh && !agentWorkspaceDescriptorRole(request.view, target.role?.id)))) {
    c.innerHTML = `<section class="pane global-content"><div class="global-eyebrow">Global</div><h1>Agents</h1><p role="alert">${esc(GLOBAL_AGENT_UNAVAILABLE)}</p></section>`;
    return;
  }
  // A saved or linked agent that is no longer an active global opens nothing of its own.
  if (remembered && !target) toast(GLOBAL_AGENT_UNAVAILABLE);
  // ?workspace= links translate at this boundary: Tables is a drawer tab (it opens Global's list); Calendar became a request above.
  let linkedView = linkedWorkspace;
  if (linkedView === 'calendar') linkedView = null;
  else if (linkedView === 'tables') { linkedView = null; globalThis.ovOpen?.('tables', {scope: 'global'}); }
  agentWorkspaceRequest = request;
  agentWorkspaceApplied = JSON.stringify(request || null);
  select(target || agents.find(a => a.role?.id === 'ordubashy') || agents[0] || GLOBAL_WORKSPACE_TERMINAL, request?.view || linkedView);
  agentWorkspaceRequest = null;
}
