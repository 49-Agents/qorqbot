// Recorded Focus, Intent and the full Todo list above the native chat composer.
// Workflow focus keeps its canonical steps; only the native session controls the working dots.
const WORKFLOW_PANEL_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
const WORKFLOW_PANEL_STATE = {completed: 'done', active: 'working', pending: 'open'};
const WORKFLOW_PANEL_TEXT = {completed: 'Done', active: 'Working', pending: 'Upcoming'};

function chatFocusPanelMount(composer, agentId, {visible, onChange}) {
  const panel = document.createElement('section');
  panel.className = 'wfpanel'; panel.setAttribute('aria-label', 'Agent focus and Todo');
  panel.innerHTML = '<div class="gchat-context" tabindex="0" aria-label="Agent focus and intent"><dl><dt>Focus</dt><dd class="gchat-focus-label">Loading…</dd><dt>Intent</dt><dd class="gchat-focus-intent">Loading…</dd></dl></div>';
  const head = document.createElement('button');
  head.type = 'button'; head.className = 'quiet wfpanel-head';
  const name = document.createElement('span'); name.className = 'wfpanel-name';
  const chevron = document.createElement('span'); chevron.className = 'wfpanel-chevron'; chevron.innerHTML = WORKFLOW_PANEL_ICON;
  head.append(name, chevron);
  const list = document.createElement('ol'); list.className = 'wfpanel-steps'; list.tabIndex = 0; list.setAttribute('aria-label', 'Todo');
  panel.append(head, list);
  composer.before(panel);
  const todoKey = 'agent-todo-preview:' + agentId;
  const mobile = matchMedia('(max-width: 700px)');
  let open = true, disposed = false, pending = null, last = 0, attention = null, loaded = false, focusError = false, todoError = false;
  let todos = uiReads.peek(todoKey)?.items || null;
  const show = () => { panel.hidden = false; };

  const paint = () => {
    const focus = attention?.focus, focused = attention?.state === 'focused' && focus && !focus.unavailable;
    panel.querySelector('.gchat-focus-label').textContent = !loaded ? 'Loading…' : focusError || focus?.unavailable ? 'Focus unavailable'
      : focused ? focus.label || attention.task?.label || 'Focused' : 'No focus set';
    panel.querySelector('.gchat-focus-intent').textContent = !loaded ? 'Loading…' : focusError || focus?.unavailable ? 'Intent unavailable'
      : focused && attention.intent ? attention.intent : 'No intent recorded';
    const workflow = focused && focus.type === 'workflow' && !attention.workflow?.unavailable ? attention.workflow : null;
    name.textContent = 'Todo';
    head.setAttribute('aria-expanded', String(open));
    head.setAttribute('aria-label', (open ? 'Hide ' : 'Show ') + name.textContent.toLowerCase());
    panel.classList.toggle('wfpanel-collapsed', !open);
    list.hidden = !open;
    const workflowSteps = workflow?.steps || [];
    const steps = [...workflowSteps, ...(todos || []).map(item => ({label: item.task.title, status: item.task.status}))];
    const rows = steps.map(step => {
      const blocked = !!(step.current && workflow?.blocked);
      const row = document.createElement('li'); row.className = 'wfpanel-step';
      if (step.current) row.setAttribute('aria-current', 'step');
      const status = blocked ? 'Blocked' : step.current && workflow?.paused ? 'Paused' : WORKFLOW_PANEL_TEXT[step.status] || step.status;
      row.setAttribute('aria-label', step.label + ': ' + status);
      const ball = document.createElement('span');
      ball.className = 'ball agfocus-task-state ' + (blocked ? 'blocked' : WORKFLOW_PANEL_STATE[step.status] || step.status || 'open');
      ball.setAttribute('aria-hidden', 'true'); ball.innerHTML = '<span class="core"></span>';
      const label = document.createElement('span'); label.className = 'wfpanel-label'; label.textContent = step.label;
      row.append(ball, label);
      if (blocked && workflow.blocker_reason) {
        const reason = document.createElement('p'); reason.className = 'wfpanel-error'; reason.textContent = workflow.blocker_reason; row.append(reason);
      }
      return row;
    });
    if (workflowSteps.length) {
      const heading = text => { const row = document.createElement('li'); row.className = 'wfpanel-list-heading'; row.textContent = text; return row; };
      rows.splice(workflowSteps.length, 0, heading('Todo'));
      rows.unshift(heading('Workflow steps'));
    }
    if (!todos?.length) {
      const empty = document.createElement('li'); empty.className = 'wfpanel-empty';
      empty.textContent = todoError ? 'Todo unavailable' : todos ? 'No tasks on Todo.' : 'Loading Todo…'; rows.push(empty);
    }
    const scrollTop = list.scrollTop;
    list.replaceChildren(...rows);
    list.scrollTop = scrollTop;
    show(); onChange();
  };
  const receive = data => {
    if (disposed) return;
    attention = data?.attention || null; loaded = true; focusError = !data; paint();
  };
  const refresh = (force = false) => {
    if (disposed || mobile.matches || document.hidden || !visible() || !force && Date.now() - last < 12000) return Promise.resolve();
    if (pending) return pending;
    last = Date.now();
    // Focus can paint immediately from its shared snapshot while Todo pages load.
    pending = Promise.all([
      agentFocusRead(agentId, {force}).then(receive, () => receive(null)),
      uiReads.read(todoKey, async () => {
        const items = [], seen = new Set();
        let cursor = null;
        do {
          if (disposed || mobile.matches || !visible()) throw Error('Todo hidden');
          const data = coreData(await post('/root/operations/todo_get', {scope: 'global', agent_id: agentId, projection: 'summary', limit: 100, ...(cursor ? {cursor} : {})}));
          if (data?.agent_id !== agentId || !Array.isArray(data.items)) throw Error('Todo unavailable');
          items.push(...data.items); cursor = data.next_cursor;
          if (cursor && seen.has(cursor)) throw Error('Todo pagination did not advance');
          if (cursor) seen.add(cursor);
        } while (cursor);
        return {items};
      }, {force}).then(data => {
        if (disposed) return;
        todos = data.items; todoError = false; paint();
      }, () => {
        if (disposed) return;
        todos = null; todoError = true; paint();
      }),
    ]).finally(() => { pending = null; });
    return pending;
  };
  const unsubscribe = uiReads.subscribe(agentFocusKey(agentId), receive);
  const cached = uiReads.peek(agentFocusKey(agentId));
  if (cached) receive(cached); else paint();
  head.onclick = () => { open = !open; paint(); };
  queueMicrotask(() => { void refresh(); });
  const timer = setInterval(() => { void refresh(); }, 12000);
  const onVisible = () => { if (!document.hidden) void refresh(); };
  const onResize = () => { if (!mobile.matches) void refresh(true); };
  document.addEventListener('visibilitychange', onVisible);
  mobile.addEventListener('change', onResize);
  return {
    refresh, show,
    dispose() { disposed = true; unsubscribe(); clearInterval(timer); document.removeEventListener('visibilitychange', onVisible); mobile.removeEventListener('change', onResize); panel.remove(); },
  };
}
