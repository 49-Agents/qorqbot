// The Global Agents page lists the supported global agents (including every permanent worker).
// Business agents are not resident: they are never listed, selected or started here.
let globalAgentsGeneration = 0;
let globalTestingModeUnsubscribe = null;
let globalRosterUnsubscribe = null;
let globalModeUnsubscribe = null;
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
    document.querySelectorAll('#global-agent-list .qorqbot-agent, .agent-nav-trigger .qorqbot-agent').forEach(svg => {
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
  return await root.openAgent(agentId, options) === true;
}
const GLOBAL_WORKSPACE_TERMINAL = '__qorqut_workspace_terminal__';
// One owner shell keeps the original id, route and tmux session. Older saved or
// linked Terminal 2/3 selections open it instead; their sessions and routes are untouched.
// `globalWorkspaceTerminalId` only recognizes a saved or linked id, whether or not the page still offers that entry.
const globalWorkspaceTerminalId = id => /^__qorqut_workspace_terminal(?:_[23])?__$/.test(id || '');
const globalWorkspaceTerminal = id => globalWorkspaceTerminalId(id)
  ? {id: GLOBAL_WORKSPACE_TERMINAL, label: 'Terminal', base: '/root/workspace-terminal'} : null;
// The shell is offered only while agents run in terminals. Where it is not offered, a saved or linked shell id is not an agent:
// it is read as "nothing saved" and the page opens Ordubashy without a message.
const globalRetiredSelection = globalWorkspaceTerminalId;
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
// Reuse the shared focus snapshots, limiting concurrent reads and discarding updates after close.
function agentNavFocusMount(nav) {
  let generation=0;
  const current=token=>token===generation && nav.isConnected && nav.matches(':popover-open');
  const paint=(label,data)=>{
    const attention=data?.attention,focus=attention?.focus;
    const text=!attention || focus?.unavailable ? 'Focus unavailable'
      : attention.state==='focused' && focus ? focus.label || attention.task?.label || 'Focused' : '-';
    const full=String(text).replace(/\s+/g,' ').trim(),characters=Array.from(full);
    label.textContent=characters.length>50?characters.slice(0,50).join('')+'...':full;
    label.title=full;
  };
  return {
    async open(){
      const token=++generation,labels=[...nav.querySelectorAll('[data-agent-focus]')];let index=0;
      await Promise.all(Array.from({length:Math.min(3,labels.length)},async()=>{
        while(current(token) && index<labels.length){
          const label=labels[index++];
          if(label.closest('[data-overseer-coming-soon]')){label.textContent='Coming soon';continue;}
          try {const data=await agentFocusRead(label.dataset.agentFocus);if(current(token))paint(label,data);}
          catch(_){if(current(token))paint(label,null);}
        }
      }));
    },
    close(){generation++;},
  };
}
// Native popovers sit above the workspace without taking a layout column.
// Hover spans the trigger and menu; native button activation also supports keyboard and touch.
function wireGlobalAgentNavOverlay(root) {
  const trigger=root.querySelector('[data-agent-nav-trigger]'), nav=root.querySelector('.agent-nav-overlay');
  const timeline=agentNavTimelineMount(nav),focus=agentNavFocusMount(nav);
  let closeTimer;
  const inside=target=>target instanceof Node && (trigger.contains(target) || nav.contains(target));
  const open=()=>{clearTimeout(closeTimer);if(!nav.matches(':popover-open'))nav.showPopover();trigger.setAttribute('aria-expanded','true');};
  // While the walkthrough points at something in the menu (globalAgentNavHold), leaving it does not close it.
  const close=()=>{clearTimeout(closeTimer);if(nav.dataset.held!==undefined)return;if(nav.matches(':popover-open'))nav.hidePopover();trigger.setAttribute('aria-expanded','false');};
  let keyboard=false;
  for(const host of [trigger,nav,timeline.panel]) {
    host.onpointerenter=event=>{if(event.pointerType!=='touch'){keyboard=false;open();}};
    host.onpointerleave=event=>{if(event.pointerType!=='touch' && !inside(event.relatedTarget)){clearTimeout(closeTimer);closeTimer=setTimeout(close,400);}};
    host.addEventListener('focusout',event=>{if(keyboard && !inside(event.relatedTarget))close();});
    host.onkeydown=event=>{
      keyboard=true;
      if(event.key==='Escape'){event.preventDefault();close();trigger.focus({preventScroll:true});}
      else if(host===trigger && event.key==='ArrowDown'){
        event.preventDefault();open();(nav.querySelector('[aria-pressed="true"]') || nav.querySelector('button'))?.focus();
      }
    };
  }
  nav.addEventListener('toggle',()=>{
    const opened=nav.matches(':popover-open');trigger.setAttribute('aria-expanded',String(opened));
    if(opened){timeline.open();void focus.open();}else {clearTimeout(closeTimer);timeline.close();focus.close();}
  });
  // Rows added while the menu is open get their focus line too.
  timeline.reloadFocus=()=>{if(nav.matches(':popover-open'))void focus.open();};
  return timeline;
}
// The walkthrough holds the agent menu open while a step points at a button inside it. A held menu is a manual popover, so a
// click elsewhere (the walkthrough's own buttons) does not dismiss it; releasing restores the usual hover menu.
function globalAgentNavHold(hold) {
  const nav=document.querySelector('#global-agent-navigation'), trigger=document.querySelector('[data-agent-nav-trigger]');
  if(!nav) return false;
  if(hold){
    if(nav.dataset.held===undefined){
      if(nav.matches(':popover-open'))nav.hidePopover();
      nav.dataset.held='';nav.popover='manual';
    }
    if(!nav.matches(':popover-open'))nav.showPopover();
    trigger?.setAttribute('aria-expanded','true');
    return true;
  }
  if(nav.dataset.held===undefined) return false;
  delete nav.dataset.held;
  if(nav.matches(':popover-open'))nav.hidePopover();
  nav.popover='auto';trigger?.setAttribute('aria-expanded','false');
  return true;
}
// Retired profiles and archival records never reach the list.
const globalActiveProfile = agent => !!agent && typeof agent.agent_id === 'string' && !agent.archival_record && (agent.status === undefined || agent.status === 'active');
// A global profile has ecosystem scope and no home business; a business-scoped
// record claiming a supported role is refused here as the host refuses it.
const globalRosterAgent = agent => globalActiveProfile(agent) && GLOBAL_AGENT_ROLES.includes(agent.role?.id || agent.role_id)
  && agent.home_business_id == null && agent.scope?.kind === 'ecosystem';
// Retired or unknown agents are history, not something to open or start.
const GLOBAL_AGENT_UNAVAILABLE = 'This agent is no longer active. Its history is kept.';
// Question mark in a circle, matching the stroke weight of the Settings icon.
const GLOBAL_GUIDE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.6 9.3a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.3-2.4 3.8"/><path d="M12 17h.01"/></svg>';
const GLOBAL_TERMINAL_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/></svg>';
const GLOBAL_MENU_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>';
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
  globalRosterUnsubscribe?.();
  globalRosterUnsubscribe = null;
  globalModeUnsubscribe?.();
  globalModeUnsubscribe = null;
  const generation = ++globalAgentsGeneration;
  const current = () => generation === globalAgentsGeneration && c.isConnected
    && state.scope === 'global' && state.globalTab === 'agents';
  c.innerHTML = '<section class="pane global-content"><div class="global-eyebrow">Global</div><h1>Agents</h1><p class="muted" role="status">Loading agents…</p></section>';
  let agents;
  try {
    // Read the owner preference together with the roster, before any Overseer
    // control is painted or its chat/workspace is mounted.
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
  let rosterOverseer = agents.find(overseerRole) || null;
  // Asked each time, never kept: the answer can arrive after the first paint and changes after a switch.
  const shellMode = () => typeof agentMode !== 'undefined' && agentMode.terminals();
  const shellButton = () => `<button class="item agent global-workspace-terminal" type="button" data-global-agent="${GLOBAL_WORKSPACE_TERMINAL}" aria-pressed="false" data-tip="Terminal" aria-label="Terminal"><span class="nm">${GLOBAL_TERMINAL_ICON}<span class="agent-nav-label">Terminal</span></span></button>`;
  c.innerHTML = `<div class="split global-agents agsplit agent-nav-overlay-mode">
    <button type="button" class="quiet agent-nav-trigger" data-agent-nav-trigger popovertarget="global-agent-navigation" aria-controls="global-agent-navigation" aria-expanded="false" aria-label="Agent navigation">${GLOBAL_MENU_ICON}</button>
    <div class="listcol agent-nav-overlay" id="global-agent-navigation" popover="auto" role="navigation" aria-label="Agent navigation">
      <div class="global-nav-body">
        <div class="crows global-primary" id="global-primary-roster">
          <div class="global-roster" id="global-agent-list"></div>
        </div>
        <div class="crows global-workspace-links">
          <button class="item agent global-workspace-guide" type="button" data-global-guide data-tip="Walkthrough" aria-label="Walkthrough"><span class="nm">${GLOBAL_GUIDE_ICON}<span class="agent-nav-label">Walkthrough</span></span></button>${shellMode() ? shellButton() : ''}
          <button class="item agent global-workspace-settings" type="button" data-global-settings data-tip="Settings" aria-label="Settings"><span class="nm">${GLOBAL_ICONS.settings}<span class="agent-nav-label">Settings</span></span></button>
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
    <section class="global-agent-session" aria-label="Agent session"><div class="chat agentchat" id="global-agent-chat"></div></section>
    <div class="agent-pane-divider" data-agent-pane-divider role="separator" aria-label="Chat and agent pane divider" aria-orientation="vertical" tabindex="0"></div>
    <aside class="tasks agpad" id="agpad" aria-label="Agent pane"></aside>
  </div>`;
  const navTimeline=wireGlobalAgentNavOverlay($('.global-agents', c));
  const list = $('#global-agent-list', c), pane = $('#global-agent-chat', c), nav = $('.listcol', c);
  // The owner's workspace shell is an entry of this menu while agents run in terminals. A saved selection or a link that names it
  // while the menu has no such entry (or any other id that no longer exists) is not an error: it falls back to Ordubashy without a message.
  const hasWorkspaceShell = () => !!nav.querySelector(`[data-global-agent="${GLOBAL_WORKSPACE_TERMINAL}"]`);
  const workspaceShell = id => hasWorkspaceShell() && typeof mountTerminal === 'function' ? globalWorkspaceTerminal(id) : null;
  const comingSoon = $('#overseer-coming-soon', c);
  const comingSoonError = $('.overseer-coming-soon-error', comingSoon);
  const comingSoonRetry = $('.overseer-coming-soon-retry', comingSoon);
  // The wider overlay shows the same saved avatar alongside the agent's name.
  // The unavailable Overseer row keeps its real avatar and gains an explicit hover/accessible status label.
  const paintRoster = () => {
    list.innerHTML = agents.map((agent, index) => {
      const gated = overseerRole(agent) && !testingModeEnabled();
      const name = agent.display_name || agent.name;
      return `${index > 0 && globalAgentNavRank(agents[index - 1]) < 2 && globalAgentNavRank(agent) >= 2
        ? '<span class="global-agent-group-divider" aria-hidden="true"></span>' : ''}<button class="item agent${gated ? ' overseer-coming-soon-entry' : ''}" type="button" data-global-agent="${esc(agent.agent_id)}" aria-pressed="false" aria-label="${esc(gated ? OVERSEER_UNAVAILABLE_LABEL : name)}"${gated ? ` title="${esc(OVERSEER_UNAVAILABLE_TITLE)}" data-overseer-coming-soon="1"` : ''}>
      <span class="nm">${globalAgentNavAvatar(agent)}<span class="agent-nav-copy"><span class="agent-nav-label">${esc(name)}</span><span class="agent-nav-focus" data-agent-focus="${esc(agent.agent_id)}">Loading focus…</span></span></span><span class="agent-nav-status"><span class="agent-nav-working" data-agent-presence="${esc(agent.agent_id)}" role="img" aria-label="Working" hidden><i></i><i></i><i></i></span></span>
    </button>`.replace(/\s*\n\s*/g, '');
    }).join('');
  };
  paintRoster();
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
    const agent=agents.find(candidate=>candidate.agent_id===selected), trigger=$('[data-agent-nav-trigger]',c);
    const shell=!agent && selected===GLOBAL_WORKSPACE_TERMINAL && hasWorkspaceShell();
    const name=agent ? agent.display_name || agent.name : shell ? 'Terminal' : 'Agents';
    trigger.innerHTML=agent ? globalAgentNavAvatar(agent) : shell ? GLOBAL_TERMINAL_ICON : GLOBAL_MENU_ICON;
    trigger.dataset.selectedAgent=selected;
    navTimeline.setAgent(agent);
    trigger.setAttribute('aria-label',`Agent navigation, ${name} selected`);
    globalAgentGazeRefresh?.();
  };

  // ---- selection
  // Selecting an agent mounts its Chat and its agent pane; the backend session is never started or stopped by selecting.
  let shellMounted = false;
  const disposeChat = () => {
    agentViewDispose();
    // Leaving the shell closes its viewer; the shell itself keeps running on the server.
    if (shellMounted) { shellMounted = false; if (typeof teardownTerm === 'function') teardownTerm(); }
  };
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
    // Release only this browser view. The backend agent session,
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
  // The roster changes while the page is open (a worker is created, one is retired): the menu follows without touching the chat,
  // the agent pane or the selection.
  const rosterKey = list => list.map(agent => [agent.agent_id, agent.display_name || agent.name, agent.role?.id, agent.presentation?.glyph ?? '', agent.presentation?.color ?? ''].join('|')).join('\n');
  let rosterReading = false, rosterAgain = false;
  const refreshRoster = async () => {
    if (rosterReading) { rosterAgain = true; return; }
    rosterReading = true;
    try {
      if (typeof uiReads !== 'undefined') uiReads.invalidate('agent-roster');   // never the snapshot from before the change
      const profiles = await loadGlobalAgentProfiles();
      if (!current() || !Array.isArray(profiles)) return;
      const next = profiles.filter(globalRosterAgent).sort((a, b) => globalAgentNavRank(a) - globalAgentNavRank(b));
      if (rosterKey(next) === rosterKey(agents)) return;
      agents = next; rosterOverseer = agents.find(overseerRole) || null;
      paintRoster(); paintOverseerEntry(); paintSelection(); agentNavPaint(); agentNavRefresh(true);
      if (typeof replyNotifications !== 'undefined') replyNotifications.paint();
      navTimeline.reloadFocus?.();
    } catch (_) { /* the next change event reads it again */ }
    finally { rosterReading = false; if (rosterAgain) { rosterAgain = false; if (current()) void refreshRoster(); } }
  };
  if (typeof onChange === 'function') globalRosterUnsubscribe = onChange(['agents'], null, () => { void refreshRoster(); });
  const select = (agent, requestedView = null, remember = true, leaveChecked = false) => {
    if (!current()) return;
    const selection = leaveChecked || ++selectionVersion;
    const workspace = typeof agent === 'string' ? workspaceShell(agent) : null;
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
        if (requestedView && _agpad?.agentId === id) return agpadRequestView(_agpad, requestedView);
        return true;
      }
    }
    if (!leaveChecked && typeof agentWorkspaceCanLeave === 'function' && (_agpad?.workspaceFrame || _agpad?.journalMount || _agpad?.surface?.beforeLeave || _agpad?.focusWorkspace?.isOpen?.()
        || (_agpad?.view === 'focus' && typeof agentWorkspacePairedBusy === 'function' && agentWorkspacePairedBusy()))) {
      const previous = _agpad, frame = previous.workspaceFrame, journal = previous.journalMount;
      return agentWorkspaceCanLeave(previous).then(ok => {
        if (ok && selection === selectionVersion && _agpad === previous && previous.workspaceFrame === frame && previous.journalMount === journal)
          return select(agent, requestedView, remember, selection);
        return false;
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
    if (!workspace) mountAgentView(pane, agent, {isCurrent: () => current() && selected === agent.agent_id});
    else { mountTerminal(selected, pane, () => {}, workspace.base); shellMounted = true; }
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
    select(workspaceShell(id) ? GLOBAL_WORKSPACE_TERMINAL : agents.find(a => a.agent_id === id));
  };
  $('.global-agents', c).openAgent = async (id, {chat = false, view = null, viewboxItem = null} = {}) => {
    ++selectionVersion;
    if (!current()) return;
    const agent = agents.find(candidate => candidate.agent_id === id);
    if (!agent) throw new Error(GLOBAL_AGENT_UNAVAILABLE);
    const gateTarget = overseerRole(agent) && !testingModeEnabled();
    const selectedRequest = select(agent, gateTarget ? null : view), selection = selectionVersion;
    const selectedResult = await selectedRequest;
    // A cancelled unsaved-edit prompt, the Testing mode gate or a newer selection
    // must stay where it is; no chat or workspace control is focused.
    if (selection !== selectionVersion || selectedResult === false || !current() || selected !== id || gated || _agpad?.agentId !== id) return false;
    if (!gateTarget && view && agpadShownView(_agpad) !== view) return false;
    if (viewboxItem) agviewboxReveal(_agpad, viewboxItem);
    if (chat) {
      pane.querySelector('.gchat-jump')?.click();
      pane.querySelector('.gchat-composer textarea')?.focus({preventScroll: true});
    }
    return true;
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
  const rememberedRaw = request?.agentId || linkedAgent || (linkedFocus ? agents.find(a => a.role?.id === 'ordubashy')?.agent_id : ls('ag:scope:global'));
  const savedShell = globalRetiredSelection(rememberedRaw), staleShell = savedShell && !workspaceShell(rememberedRaw);
  const remembered = staleShell ? null : rememberedRaw;
  // The shell has no workspace view, so a stored workspace descriptor for it means nothing.
  if (savedShell) request = null;
  const target = savedShell ? (staleShell ? undefined : GLOBAL_WORKSPACE_TERMINAL) : agents.find(a => a.agent_id === remembered);
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
  if (remembered && !target && !staleShell) toast(GLOBAL_AGENT_UNAVAILABLE);
  // ?workspace= links translate at this boundary: Tables is a drawer tab (it opens Global's list); Calendar became a request above.
  let linkedView = linkedWorkspace;
  if (linkedView === 'calendar') linkedView = null;
  else if (linkedView === 'tables') { linkedView = null; globalThis.ovOpen?.('tables', {scope: 'global'}); }
  agentWorkspaceRequest = request;
  agentWorkspaceApplied = JSON.stringify(request || null);
  const first = target || agents.find(a => a.role?.id === 'ordubashy') || agents[0] || (workspaceShell(GLOBAL_WORKSPACE_TERMINAL) ? GLOBAL_WORKSPACE_TERMINAL : null);
  if (first) select(first, request?.view || linkedView);
  else pane.innerHTML = '<p class="muted" role="status">No agents are available.</p>';
  const firstVersion = selectionVersion;
  agentWorkspaceRequest = null;
  // The mode can be answered a moment after this first paint, and changes after a switch followed by a reload. The menu follows it:
  // the shell entry appears or goes, a shell that is open when terminals end gives way to Ordubashy, and a shell id that was saved
  // or linked (and so fell back above) opens when terminals turn out to be on, if nothing else was chosen meanwhile.
  const paintShellEntry = () => {
    const links = nav.querySelector('.global-workspace-links'), existing = nav.querySelector('.global-workspace-terminal');
    if (!shellMode()) { existing?.remove(); return; }
    if (!existing) links.querySelector('[data-global-settings]').insertAdjacentHTML('beforebegin', shellButton());
  };
  // The Chat of the agent that stays open gets or loses its Terminal view: that is decided when the view is mounted
  // (js/agent-terminal-view.js), so a mode answered after the first paint mounts it again.
  const remountAgentChat = () => {
    const agent = agents.find(a => a.agent_id === selected);
    if (!agent || gated || pane.dataset.agentPane !== 'on') return;
    disposeChat();
    mountAgentView(pane, agent, {isCurrent: () => current() && selected === agent.agent_id});
  };
  const onModeChanged = () => {
    if (!current()) return;
    const had = hasWorkspaceShell(), before = selectionVersion;
    paintShellEntry();
    if (had !== hasWorkspaceShell()) {
      if (had && selected === GLOBAL_WORKSPACE_TERMINAL) {
        const fallback = agents.find(a => a.role?.id === 'ordubashy') || agents[0];
        selected = null;
        if (fallback) select(fallback, null, true); else { disposeChat(); pane.innerHTML = '<p class="muted" role="status">No agents are available.</p>'; }
      } else if (savedShell && selectionVersion === firstVersion) select(GLOBAL_WORKSPACE_TERMINAL);
      paintSelection();
      if (typeof agentNavPaint === 'function') agentNavPaint();
    }
    if (selectionVersion === before) remountAgentChat();
  };
  document.addEventListener('agent-mode-changed', onModeChanged);
  globalModeUnsubscribe = () => document.removeEventListener('agent-mode-changed', onModeChanged);
}
