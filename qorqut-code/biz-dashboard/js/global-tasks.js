// Global-only task UI. The personal taskbox remains a separate business aggregate.
// This uses the same typed task operations and revision checks as business tasks.
function globalTasksMount(host, {agentId = null} = {}) {
  if (!host) return;
  const token = {};
  host._globalTasks = token;
  const current = () => host.isConnected && host._globalTasks === token;
  let config, assignees, rows = [], cursor = null, selected = null;
  let search = '', snoozeFilter = 'active', includeClosed = false, busy = false, attempt = null;
  let ownerFilter = 'all';
  const invoke = async (name, args = {}) => coreData(await post(`/root/operations/${name}`, {scope: 'global', ...args}));
  const label = id => assignees.find(a => a.id === id)?.display_name || id;
  const message = text => { if (current()) host.querySelector('[role="status"]').textContent = text; };

  function shell(content) {
    host.innerHTML = `<section class="global-task-space" aria-label="Global tasks">
      <p class="global-task-heading">${agentId ? 'Assigned tasks' : 'Global tasks'}</p>
      <div class="global-task-content">${content}</div><p role="status" aria-live="polite"></p></section>`;
  }

  function list() {
    if (!current()) return;
    selected = null;
    shell(`<div class="global-task-tools">${agentId ? '' : `<div class="global-task-filters" role="group" aria-label="Assigned to">${[['mine','Mine'],['agents','Agents'],['all','All']].map(([v,label])=>`<button type="button" data-owner-filter="${v}" aria-pressed="${ownerFilter===v}">${label}</button>`).join('')}</div>`}<button type="button" data-new>New task</button><button type="button" data-refresh aria-label="Refresh tasks">Refresh</button></div>
      <form data-search><label>Search Global tasks<input name="search" type="search" maxlength="500" value="${esc(search)}"></label>
      <label>Task visibility<select name="snooze_filter">${['active','snoozed','all'].map(v=>`<option value="${v}" ${snoozeFilter===v?'selected':''}>${v[0].toUpperCase()+v.slice(1)}</option>`).join('')}</select></label>
      <label><input type="checkbox" name="closed" ${includeClosed ? 'checked' : ''}>Include closed and archived</label><button>Search</button></form>
      <div class="global-task-list">${rows.length ? rows.map(t => `<button class="global-task-row" data-task="${esc(t.id)}" type="button">
        <span>${esc(t.title)}</span><small>${esc(t.status)}${t.archived ? ' · archived' : ''}${t.due ? ' · due '+esc(t.due) : ''}</small>
        ${taskSnoozed(t) ? `<small>${esc(taskSnoozeLabel(t))}</small>` : ''}
        <small>${esc(t.assignee_ids.map(label).join(', ') || 'Unassigned')}</small></button>`).join('') : '<p>No Global tasks found.</p>'}</div>
      ${cursor ? '<button type="button" data-more>Load more</button>' : ''}`);
    host.querySelector('[data-new]').onclick = () => edit(null);
    host.querySelectorAll('[data-owner-filter]').forEach(button => button.onclick = () => { ownerFilter = button.dataset.ownerFilter; load(); });
    host.querySelector('[data-refresh]').onclick = () => load();
    host.querySelector('[data-search]').onsubmit = e => {
      e.preventDefault(); const form = e.currentTarget;
      search = form.elements.search.value.trim(); includeClosed = form.elements.closed.checked; load();
    };
    host.querySelector('[name="snooze_filter"]').onchange = e => { snoozeFilter = e.target.value; load(); };
    host.querySelectorAll('[data-task]').forEach(button => button.onclick = () => open(button.dataset.task));
    host.querySelector('[data-more]')?.addEventListener('click', () => load(true));
  }

  async function load(more = false) {
    if (busy) return;
    busy = true; message('Loading…');
    host.querySelectorAll('input,select,button').forEach(b => b.disabled = true);
    try {
      const assignee = agentId || (ownerFilter === 'mine' ? assignees.find(a => a.is_owner)?.id : null);
      if (ownerFilter === 'mine' && !assignee) throw new Error('Your task identity is unavailable. Refresh to retry.');
      let next = more ? cursor : null, data;
      const agents = new Set(assignees.filter(a => a.kind === 'agent').map(a => a.id));
      do {
        data = await invoke('tasks_list', {search, snooze_filter: snoozeFilter, include_closed: includeClosed, include_archived: includeClosed,
          ...(assignee ? {assignee_id: assignee} : {}), ...(next ? {cursor:next} : {}), limit:50});
        if (!current()) return;
        if (!agentId && ownerFilter === 'agents') data.tasks = data.tasks.filter(t => t.assignee_ids.some(id => agents.has(id)));
        next = data.next_cursor;
      } while (!data.tasks.length && next);
      if (!current()) return;
      rows = more ? [...rows, ...data.tasks] : data.tasks; cursor = data.next_cursor; list();
    } catch (error) { message(error.message || 'Tasks unavailable. Use Refresh to retry.'); }
    finally { busy = false; if (current()) host.querySelectorAll('input,select,button').forEach(b => b.disabled = false); }
  }

  async function open(id) {
    if (busy) return;
    busy = true; message('Loading task…');
    try {
      const [data, c, a] = await Promise.all([invoke('tasks_get', {task_id: id}), invoke('tasks_config_get'), invoke('assignees_list')]);
      if (current()) { config = c.config; assignees = a.assignees; edit(data.task); }
    } catch (error) { message(error.message || 'Task unavailable. Refresh the list.'); }
    finally { busy = false; }
  }

  function edit(task) {
    if (!current()) return;
    selected = task;
    const assigned = task?.assignee_ids || [agentId || assignees.find(a => a.is_owner)?.id].filter(Boolean);
    const choices = [...assignees, ...assigned.filter(id => !assignees.some(a => a.id === id)).map(id => ({id, display_name: id+' (inactive)'}))];
    shell(`<button type="button" data-back>Back to Global tasks</button><h3>${task ? 'Global task' : 'New Global task'}</h3>
      <form data-editor><label>Title<input name="title" required maxlength="1000" value="${esc(task?.title || '')}"></label>
      <label>Status<select name="status" aria-label="Status">${config.states.map(s => `<option value="${esc(s.name)}" ${s.name === (task?.status || config.states.find(s => s.default)?.name) ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select></label>
      <fieldset><legend>Assignees</legend>${choices.map(a => `<label><input type="checkbox" name="assignee" value="${esc(a.id)}" ${assigned.includes(a.id) ? 'checked' : ''}>${esc(a.display_name || a.name)}</label>`).join('')}</fieldset>
      <label>Due date<input name="due" type="date" value="${esc(task?.due || '')}"></label>
      ${taskSnoozePicker(task || {})}<small>Shared deferral. Status, due date and dependencies stay unchanged.</small>
      <label>Tags (comma-separated)<input name="tags" value="${esc((task?.tags || []).join(', '))}"></label>
      ${task ? `<label><input name="archived" type="checkbox" ${task.archived ? 'checked' : ''}>Archived</label>` : ''}
      <label>${task ? 'Add comment' : 'Initial note'}<textarea name="note" maxlength="2000"></textarea></label>
      <button data-save>${task ? 'Save task' : 'Create task'}</button></form>
      ${task ? `<section aria-label="Task notes"><h4>Notes</h4>${task.notes.map(n => `<article><small>${esc(n.created_at)} · ${esc(label(n.created_by))}</small><p class="global-task-note">${esc(n.body)}</p></article>`).join('') || '<p>No notes yet.</p>'}</section>` : ''}
      <button type="button" data-retry hidden>Retry exact save</button><button type="button" data-reload hidden>Discard edits and reload current task</button>`);
    host.querySelector('[data-back]').onclick = () => { list(); load(); };
    taskSnoozeWire(host);
    host.querySelector('[data-editor]').onsubmit = e => {
      e.preventDefault(); if (busy || attempt) return;
      const f = e.currentTarget.elements;
      const changes = {title: f.title.value.trim(), status: f.status.value, due: f.due.value || null,
        assignee_ids: [...host.querySelectorAll('[name="assignee"]:checked')].map(i => i.value),
        tags: [...new Set(f.tags.value.split(',').map(s => s.trim()).filter(Boolean))], ...(task ? {archived: f.archived.checked} : {})};
      if (!changes.title) { message('A title is required.'); return; }
      try {
        const snooze = taskSnoozeChange(host, task || {});
        if (snooze !== undefined) changes.snooze = snooze;
      } catch (error) { message(error.message); return; }
      const args = task ? {task_id: task.id, changes, expected_revision: task.revision} : {...changes, id: newRequestId()};
      if (f.note.value.trim()) args.note = f.note.value.trim();
      attempt = {name: task ? 'tasks_update' : 'tasks_create', args: {...args, idempotency_key: newRequestId()}};
      save();
    };
    host.querySelector('[data-retry]').onclick = save;
    host.querySelector('[data-reload]').onclick = () => { attempt = null; if (selected) open(selected.id); else edit(null); };
  }

  async function save() {
    if (busy || !attempt) return;
    busy = true; message('Saving…');
    host.querySelectorAll('input,select,textarea,button').forEach(e => e.disabled = true);
    try {
      const data = await invoke(attempt.name, attempt.args);
      attempt = null;
      if (current()) { edit(data.task); message('Saved.'); }
    } catch (error) {
      if (!current()) return;
      const conflict = error.code === 'revision_conflict';
      if (!conflict && error.httpStatus >= 400 && error.httpStatus < 500) {
        attempt = null;
        host.querySelectorAll('input,select,textarea,button').forEach(e => e.disabled = false);
        message(error.message || 'Save rejected. Check the fields and try again.');
        return;
      }
      message(conflict ? 'This task changed elsewhere. Your edits were not applied. Reload it before editing again.' :
        'Save could not be confirmed. Retry the same save: '+(error.message || 'Connection error'));
      const button = host.querySelector(conflict ? '[data-reload]' : '[data-retry]');
      button.hidden = false; button.disabled = false;
    } finally { busy = false; }
  }

  shell('<p>Loading Global tasks…</p>');
  Promise.all([invoke('tasks_config_get'), invoke('assignees_list')]).then(([c, a]) => {
    if (!current()) return;
    config = c.config; assignees = a.assignees; list(); load();
  }).catch(error => {
    if (!current()) return;
    message(error.message || 'Global tasks unavailable.');
    const retry = document.createElement('button'); retry.textContent = 'Retry'; retry.onclick = () => globalTasksMount(host, {agentId});
    host.querySelector('.global-task-content').appendChild(retry);
  });
}
