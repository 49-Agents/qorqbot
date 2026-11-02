// Read-only Step list for the workflow Ordubashy currently has in Focus, shown directly above the native chat composer.
// Source is the workflow summary in attention_get's focus (exact run, engine step states); the panel never starts or controls a run.
const WORKFLOW_PANEL_REFRESH_MS = 5000;
const WORKFLOW_PANEL_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
const WORKFLOW_PANEL_STATE = {completed: 'done', active: 'working', pending: 'open'};
const WORKFLOW_PANEL_TEXT = {completed: 'Done', active: 'Working', pending: 'Upcoming'};

function workflowPanelStepBall(step, blocked) {
  const ball = document.createElement('span');
  ball.className = 'ball ' + (blocked ? 'blocked' : WORKFLOW_PANEL_STATE[step.status] || 'open');
  const core = document.createElement('span'); core.className = 'core'; ball.append(core);
  return ball;
}

function workflowPanelMount(composer, agentId) {
  const panel = document.createElement('section');
  panel.className = 'wfpanel agfocus'; panel.hidden = true; panel.setAttribute('aria-label', 'Workflow steps');
  const head = document.createElement('button');
  head.type = 'button'; head.className = 'quiet wfpanel-head';
  const name = document.createElement('span'); name.className = 'wfpanel-name';
  const symbol = document.createElement('span'); symbol.className = 'wfpanel-symbol'; symbol.innerHTML = T_VIEW_ICON.workflow;
  const chevron = document.createElement('span'); chevron.className = 'wfpanel-chevron'; chevron.innerHTML = WORKFLOW_PANEL_ICON;
  head.append(symbol, name, chevron);
  const list = document.createElement('ol'); list.className = 'wfpanel-steps agfocus-items';
  panel.append(head, list);
  composer.before(panel);
  let open = true, disposed = false, busy = false, token = 0, last = 0, dirty = false, trailing = null, summary = null;

  const paint = () => {
    const hidden = !summary || summary.unavailable || !summary.steps?.length;
    panel.hidden = hidden;
    if (hidden) { list.replaceChildren(); return; }
    name.textContent = summary.name || 'Workflow';
    head.setAttribute('aria-expanded', String(open));
    head.setAttribute('aria-label', (open ? 'Hide steps of ' : 'Show steps of ') + name.textContent);
    panel.classList.toggle('wfpanel-collapsed', !open);
    list.hidden = !open;
    const rows = [];
    for (const step of summary.steps) {
      const blocked = !!(step.current && summary.blocked);
      const row = document.createElement('li');
      row.className = 'agfocus-card agfocus-collapsed';
      if (step.current) row.setAttribute('aria-current', 'step');
      const status = blocked ? 'Blocked' : step.current && summary.paused ? 'Paused' : WORKFLOW_PANEL_TEXT[step.status] || step.status;
      row.setAttribute('aria-label', step.label + ': ' + status);
      row.innerHTML = agfocusRowHtml();
      row.querySelector('.ttl').textContent = step.label;
      row.querySelector('.agfocus-marker').append(workflowPanelStepBall(step, blocked));
      if (blocked && summary.blocker_reason) {
        const reason = document.createElement('p'); reason.className = 'agfocus-error'; reason.textContent = summary.blocker_reason; row.append(reason);
      }
      rows.push(row);
    }
    list.replaceChildren(...rows);
  };

  // One guarded read of the existing Attention endpoint (single-item page); an older answer never replaces a newer one.
  async function refresh(force = false) {
    if (disposed) return;
    // Skipped requests are never lost: one trailing refresh runs once the throttle, the in-flight read or a hidden tab clears.
    if (busy || document.hidden || (!force && Date.now() - last < WORKFLOW_PANEL_REFRESH_MS)) {
      dirty = true;
      if (!busy && !document.hidden && !trailing) trailing = setTimeout(() => { trailing = null; void refresh(); }, Math.max(0, WORKFLOW_PANEL_REFRESH_MS - (Date.now() - last)));
      return;
    }
    clearTimeout(trailing); trailing = null; dirty = false;
    busy = true; last = Date.now();
    const mine = ++token;
    try {
      const data = coreData(await post('/root/operations/attention_get', {scope: 'global', agent_id: agentId, limit: 1}));
      if (disposed || mine !== token) return;
      summary = data?.agent_id === agentId && data.attention?.focus?.type === 'workflow' ? data.attention.workflow || null : null;
    } catch (_) {
      if (disposed || mine !== token) return;
      summary = null;
    } finally {
      busy = false;
      if (!disposed && dirty && !trailing && !document.hidden) trailing = setTimeout(() => { trailing = null; void refresh(); }, Math.max(0, WORKFLOW_PANEL_REFRESH_MS - (Date.now() - last)));
    }
    paint();
  }
  head.onclick = () => { open = !open; paint(); };
  const onVisible = () => { if (!document.hidden) void refresh(); };
  document.addEventListener('visibilitychange', onVisible);
  return {
    refresh,
    dispose() { disposed = true; token++; clearTimeout(trailing); document.removeEventListener('visibilitychange', onVisible); panel.remove(); },
  };
}
