// Global task UI, also embedded above the business aggregate in the Tasks pane.
// This uses the same typed task operations and revision checks as business tasks.
function globalTasksMount(host, {agentId = null, heading = null, compact = false, initialTaskId = null, initialCreate = false, initialParentId = null, session = null, onCreated = null, onBack = null, embedded = false, onNavigate = null, renderRelated = null, onCanonical = null} = {}) {
  if (!host) return;
  host._globalTasks?.dispose?.();
  const token = {};
  host._globalTasks = token;
  const current = () => host.isConnected && host._globalTasks === token && !token.disposed;
  // Every controller the open editor mounted (record surface, type editor, task conversation) goes together, once, when
  // the editor is replaced, the list returns, or this mount is replaced or removed.
  function disposeEditor() {
    const parts = editorParts; editorParts = null; recordSurface = null;
    try { token.moveDialog?.close(); } catch (_) { /* already gone */ } token.moveDialog = null;
    token.related?.dispose?.(); token.related = null;
    if (!parts) return;
    parts.surface?.dispose(); parts.progress?.dispose?.(); parts.typeEditor?.dispose();
  }
  let leavePrompt = null, child = null;
  token.dispose = () => { token.disposed = true; leavePrompt?.finish(false); child?.dispose(); disposeEditor(); token.stateUnsubscribe?.(); token.stateUnsubscribe = null; };
  token.dirty = () => !!child?.controller?.dirty();
  token.beforeLeave = async () => child ? child.close() : true;
  token.changed = message => {
    if (!current()) return;
    const status = host.querySelector('[data-global-task-status]');
    if (status) status.textContent = message;
    // Reveal the existing deliberately-labelled discard/reload control; do not make a
    // doomed Save the only way to reach the canonical type.
    const reload = host.querySelector('[data-reload]');
    if (reload) reload.hidden = false;
  };
  const goTask = (id, label, task = null) => {
    if (embedded && typeof onNavigate === 'function') return onNavigate({type:'task', id, business_id:task?.business_id ?? null,
      business_slug:task?.business_slug ?? null, label:label || task?.title || id});
    return taskOpenReference({id, business_id:null, label:label || task?.title, task_type:task?.task_type});
  };
  let assignees, rows = [], cursor = null, selected = null, recordSurface = null, editorParts = null;
  let search = '', snoozeFilter = 'active', includeClosed = false, busy = false, attempt = null;
  let ownerFilter = 'all', filtersPending = false, rowsQuery = null, confirming = false;
  if (initialCreate && session) {
    session.onDispose(() => token.dispose());
    session.setBeforeLeave(() => {
      if (busy || confirming) { message('Please wait for the current save to finish.'); return false; }
      if (!attempt && !token.dirty()) return true;
      if (leavePrompt) return leavePrompt.promise;
      const box = document.createElement('div');
      box.setAttribute('role', 'group'); box.setAttribute('aria-label', 'Discard Global task');
      const notice = document.createElement('p');
      notice.textContent = attempt ? 'This create request may have succeeded. Discarding it cannot undo the request, and you will lose the ability to retry it.' : 'Discard your unsaved changes?';
      const keep = document.createElement('button'); keep.type = 'button'; keep.textContent = 'Keep editing';
      const discard = document.createElement('button'); discard.type = 'button'; discard.textContent = 'Discard changes';
      box.append(notice, keep, discard);
      const content = host.firstElementChild, previousFocus = document.activeElement;
      if (content) content.inert = true;
      host.append(box);
      let resolve; const promise = new Promise(done => { resolve = done; });
      const finish = accepted => {
        if (!leavePrompt) return;
        leavePrompt = null; box.remove(); if (content) content.inert = false;
        if (accepted) attempt = null;
        else if (previousFocus?.isConnected && content?.contains(previousFocus)) previousFocus.focus();
        resolve(accepted);
      };
      leavePrompt = {promise, finish}; keep.onclick = () => finish(false); discard.onclick = () => finish(true); keep.focus();
      return promise;
    });
  }
  token.stateUnsubscribe = taskStateSubscribe((scope, id, snapshot) => {
    if (!current()) { token.stateUnsubscribe?.(); token.stateUnsubscribe = null; return; }
    if (scope !== null) return;
    const task = rows.find(item => String(item.id) === String(id));
    if (!task || !snapshot?.task) return;
    Object.assign(task, snapshot.task);
    // The editor owns its draft fields. Status controls elsewhere still update
    // through taskStateBind, but a status transition can change list visibility.
    if (host.querySelector('[data-editor]')) return;
    const closed = tClosed(task);
    const rowEl = [...host.querySelectorAll('[data-task]')].find(el => String(el.dataset.task) === String(id));
    if (rowEl && closed && !includeClosed && snapshot.pending == null) rowEl.remove();
    else if (rowEl) taskStateBind(rowEl.querySelector('.ball'), null, task);
    else if (!closed || includeClosed) list();
  });
  host.setTaskFilters = (query, visibility) => {
    if (search === query && snoozeFilter === visibility) return;
    search = query; snoozeFilter = visibility;
    if (!assignees || busy || host.querySelector('[data-editor]')) { filtersPending = true; return; }
    load();
  };
  // The root route is Global context; the Task operations infer it, so no scope field is sent.
  const invoke = async (name, args = {}) => coreData(await post(`/root/operations/${name}`, args));
  const message = text => { if (current()) host.querySelector('[data-global-task-status]').textContent = text; };

  function shell(content) {
    disposeEditor();
    token.dirty = () => !!child?.controller?.dirty();
    host.innerHTML = `<section class="global-task-space" aria-label="Global tasks">
      <p class="global-task-heading">${esc(heading || (agentId ? 'Assigned tasks' : 'Global tasks'))}</p>
      <div class="global-task-content">${content}</div><p data-global-task-status role="status" aria-live="polite"></p></section>`;
  }

  const avatar = (id, prefix = '') => {
    const actor = assignees.find(a => a.id === id);
    const name = actor?.display_name || actor?.name || 'Unknown';
    return identityAvatar(actor?.kind === 'agent' ? 'agent' : actor?.kind === 'human' ? 'founder' : 'unknown', name, globalAgentProfile(id) || actor || {}, prefix + name, true);
  };
  function row(task) {
    return taskRowLayout(task, {
      attributes: `data-task="${esc(task.id)}" tabindex="0" role="button" aria-label="${esc(task.title)}"`,
      leading: taskBusinessLabel('Global', true),
      assigner: `<span class="task-assigner-column">${avatar(task.created_by, 'Created by ')}</span>`,
      trailing: (tDue(task) || '<span class="tdl"></span>') + `<span class="task-assignee-column">${task.assignee_ids.slice(0, 2).map(id => avatar(id, 'Assigned to ')).join('')}${task.assignee_ids.length > 2 ? `<span class="task-assignee-more">+${task.assignee_ids.length - 2}</span>` : ''}</span><span class="task-chips" aria-hidden="true"></span>`,
      closed: tClosed(task),
    });
  }

  function list() {
    if (!current()) return;
    selected = null;
    const visibleRows = rows.filter(task => includeClosed || taskStateSnapshot(null, task.id).pending != null || !tClosed(task));
    shell(`<div class="global-task-tools">${agentId ? '' : `<div class="global-task-filters" role="group" aria-label="Assigned to">${[['mine','Mine'],['agents','Agents'],['all','All']].map(([v,label])=>`<button type="button" data-owner-filter="${v}" aria-pressed="${ownerFilter===v}">${label}</button>`).join('')}</div>`}${agentId ? '' : '<button type="button" data-new>New task</button>'}<button type="button" class="${agentId ? 'agent-refresh' : ''}" data-refresh aria-label="Refresh tasks">${agentId ? aicon('refresh-cw','currentColor',14) : 'Refresh'}</button></div>
      ${compact ? '' : `<form data-search><label>Search Global tasks<input name="search" type="search" maxlength="500" value="${esc(search)}"></label>
      <label>Task visibility<select name="snooze_filter">${['active','snoozed','all'].map(v=>`<option value="${v}" ${snoozeFilter===v?'selected':''}>${v[0].toUpperCase()+v.slice(1)}</option>`).join('')}</select></label>
      <label><input type="checkbox" name="closed" ${includeClosed ? 'checked' : ''}>Include closed and archived</label><button>Search</button></form>`}
      <div class="personal-task-list">${visibleRows.length ? visibleRows.map(row).join('') : '<p>No Global tasks found.</p>'}</div>
      ${cursor ? '<button type="button" data-more>Load more</button>' : ''}`);
    host.querySelector('[data-new]')?.addEventListener('click', () => window.taskOpenGlobalCreate());
    host.querySelectorAll('[data-owner-filter]').forEach(button => button.onclick = () => { ownerFilter = button.dataset.ownerFilter; load(); });
    host.querySelector('[data-refresh]').onclick = () => load();
    host.querySelector('[data-search]')?.addEventListener('submit', e => {
      e.preventDefault(); const form = e.currentTarget;
      search = form.elements.search.value.trim(); includeClosed = form.elements.closed.checked; load();
    });
    host.querySelector('[name="snooze_filter"]')?.addEventListener('change', e => { snoozeFilter = e.target.value; load(); });
    host.querySelectorAll('[data-task]').forEach(row => {
      const task = rows.find(item => String(item.id) === String(row.dataset.task));
      taskStateBind(row.querySelector('.ball'), null, task);
      const ball = row.querySelector('.ball');
      if (ball) ball.onclick = event => { event.stopPropagation(); changeState(row.dataset.task); };
      row.onclick = event => { event.stopPropagation(); goTask(row.dataset.task,task?.title,task); };
      row.onkeydown = event => { if (event.target === row && ['Enter', ' '].includes(event.key)) { event.preventDefault(); goTask(row.dataset.task,task?.title,task); } };
    });
    host.querySelector('[data-more]')?.addEventListener('click', () => load(true));
  }

  async function changeState(id, requested) {
    const task = rows.find(t => t.id === id);
    if (!task) return;
    const status = requested || tNext(task.status);
    if (!status || !T_STATES.includes(status) || status === task.status) return;
    const snapshot = await taskStateSet(null, task, status);
    if (!current() || !snapshot?.error) return;
    message(uiFailure(snapshot.error));
  }

  async function load(more = false) {
    if (busy) return;
    busy = true; filtersPending = false; message('Loading…');
    const queryKey = JSON.stringify([search, snoozeFilter, includeClosed, ownerFilter, agentId]);
    const previous = rowsQuery === queryKey ? rows.map(task => ({...task})) : [];
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
      data.tasks = data.tasks.map(task => taskStateObserve(null, task));
      // A list read can omit a task that was closed when it started but reopened
      // locally while the response was in flight. Retain that newer observation.
      if (!more) for (const prior of previous) {
        const snapshot = taskStateSnapshot(null, prior.id);
        if (!data.tasks.some(task => task.id === prior.id) && snapshot.task &&
            (snapshot.pending != null || snapshot.task.revision > prior.revision || snapshot.task.status !== prior.status)) {
          data.tasks.push({...prior, ...snapshot.task});
        }
      }
      rowsQuery = queryKey;
      rows = more ? [...rows, ...data.tasks] : data.tasks; cursor = data.next_cursor; list();
    } catch (error) { message(uiFailure(error, {load: true})); }
    finally { busy = false; if (current()) { host.querySelectorAll('input,select,button').forEach(b => b.disabled = false); if (filtersPending) load(); } }
  }

  async function open(id, {adoptType = false} = {}) {
    if (busy) return;
    busy = true; message('Loading task…');
    try {
      const [data, a] = await Promise.all([invoke('tasks_get', {task_id: id}), invoke('assignees_list')]);
      if (current()) {
        assignees = a.assignees;
        // An explicit reload/discard of a Task whose canonical type changed adopts the
        // workspace's existing subtype renderer; a normal same-type reopen keeps this editor.
        if (adoptType && onCanonical && selected && data.task && (data.task.task_type || 'task') !== (selected.task_type || 'task')) {
          if (await onCanonical(data.task, {reload: true, changes: null})) return;
        }
        edit(data.task);
      }
    } catch (error) { message(uiFailure(error, {load: true})); }
    finally { busy = false; }
  }

  function edit(task, {recordPanelOpen = false} = {}) {
    if (!current()) return;
    selected = task;
    // Story, Customer and Email records open their Ordubashy conversation first, with an optional panel beside it.
    // The embedded workspace owns the one private Chat tab.
    const special = !embedded && !!task && TASK_SPECIAL_TYPES.includes(task.task_type) && typeof recordSurfaceMount === 'function';
    const assigned = task?.assignee_ids || [agentId || assignees.find(a => a.is_owner)?.id].filter(Boolean);
    const choices = [...assignees, ...assigned.filter(id => !assignees.some(a => a.id === id)).map(id => ({id, display_name: 'Unavailable assignee'}))];
    shell(`<div class="global-task-editor-heading">${embedded ? '' : '<button type="button" data-back>← Back</button>'}<h3>${task ? 'Global task' : 'New Global task'}</h3>${task && !embedded && task.task_type !== 'email' ? '<button type="button" class="quiet" data-task-move>Move to project…</button>' : ''}</div>
      ${special ? '<section class="record-thread-host" data-record-surface aria-label="Record conversation"><div data-task-progress></div></section>' : ''}
      <form data-editor><label class="global-task-wide">Title<input name="title" required maxlength="1000" value="${esc(task?.title || '')}"></label>
      ${taskTypeFields(task || {})}
      <label data-task-status-field>Status<select name="status" aria-label="Status">${T_STATES.map(name => `<option value="${name}" ${name === (task?.status || 'open') ? 'selected' : ''}>${T_LABEL[name]}</option>`).join('')}</select></label>

      <label>Due date<input name="due" type="date" value="${esc(task?.due || '')}"></label>
      <fieldset><legend>Assignees</legend>${choices.map(a => `<label><input type="checkbox" name="assignee" value="${esc(a.id)}" ${assigned.includes(a.id) ? 'checked' : ''}>${esc(a.display_name || a.name)}</label>`).join('')}</fieldset>
      <div class="global-task-wide">${taskSnoozePicker(task || {})}</div>
      <div class="global-task-wide" data-task-tags></div>
      ${task ? `<label><input name="archived" type="checkbox" ${task.archived ? 'checked' : ''}>Archived</label>` : ''}
      <button data-save>${task ? 'Save task' : 'Create task'}</button></form>
      ${task ? `<section aria-label="Related work"><h4>Linked tasks</h4><div class="task-cross-links"></div><h4>Related work</h4>${task.email_conversation_id ? '<button type="button" data-task-read-email>Historical email reference</button>' : ''}<button type="button" data-add-child>Add child task</button><div class="global-task-related"></div><div data-task-family></div></section>` : ''}
      ${task && !special ? '<section aria-label="Task notes"><h4>Progress notes</h4><div data-task-progress></div></section>' : ''}
      <button type="button" data-retry hidden>Retry exact save</button><button type="button" data-reload hidden>Discard edits and reload current task</button>`);
    host.querySelector('[data-back]')?.addEventListener('click', () => {
      if (initialCreate && session) { void session.close(); return; }
      if(onBack && initialTaskId){onBack();return;}
      list(); load();
    });
    const typeEditor = taskTypeMount(host, null, task || {});
    host.querySelector('[data-task-move]')?.addEventListener('click', () => {
      if (token.dirty()) { message('Save or discard your edits before moving this task.'); return; }
      if (token.moveDialog) return;
      const leaveAndOpen = reference => { if (!current()) return; list(); void load(); void taskMoveOpenReference(reference); };
      const handle = taskMoveOpen({task, sourceBusinessId: null, sourceSlug: null, sourceName: 'Global',
        onClosed: () => { if (token.moveDialog === handle || !handle) token.moveDialog = null; },
        onSourceLost: () => {
          if (!current()) return;
          // Forget the unreadable record before the list repaints, so a stale cached row cannot show its title.
          rows = rows.filter(item => String(item.id) !== String(task.id));
          list(); message('This task is no longer available.');
        },
        onSourceMoved: () => { if (current()) message('This task has moved. Reopen it from its new project.'); },
        onMoved: leaveAndOpen, onMovedElsewhere: leaveAndOpen});
      token.moveDialog = handle;
    });
    host.querySelector('[data-task-read-email]')?.addEventListener('click', () => taskEmailOpen(task, null));
    host.querySelector('[data-add-child]')?.addEventListener('click', () => {
      if (!embedded) { taskCreateDialog(null, {parentId:task.id, onCreated:()=>open(task.id)}); return; }
      if (child || !current()) return;
      const parentContent = host.firstElementChild, scrollTop = host.scrollTop;
      const childHost = document.createElement('div');
      parentContent.hidden = true; parentContent.inert = true; host.append(childHost);
      let beforeLeave = () => true, cleanups = [], dead = false;
      const adapter = {
        isCurrent: () => !dead && current(),
        setBeforeLeave(fn) { beforeLeave = fn; },
        onDispose(fn) { cleanups.push(fn); },
        async close() {
          if (dead || !await beforeLeave()) return false;
          dispose(); return true;
        },
      };
      function dispose() {
        if (dead) return;
        dead = true; for (const cleanup of cleanups.splice(0)) cleanup();
        childHost.remove(); parentContent.hidden = false; parentContent.inert = false;
        host.scrollTop = scrollTop; child = null;
      }
      child = {close: () => adapter.close(), dispose, controller: null};
      child.controller = globalTasksMount(childHost, {initialCreate: true, initialParentId: task.id, session: adapter,
        onCreated: async () => {
          try {
            const data = await invoke('tasks_get', {task_id: task.id});
            if (!current() || !data?.task) return;
            task.children = data.task.children || []; task.children_has_more = data.task.children_has_more;
            paintFamily();
          } catch (_) { if (current()) message('Child created. Refresh the task to update related work.'); }
        }});
    });
    const progress = task && !embedded ? taskProgressMount(host.querySelector('[data-task-progress]'), task, null) : null;
    // What the generic controls last agreed with the server on; only a control that differs from it is a user edit.
    const form = host.querySelector('[data-editor]');
    const sortedIds = ids => [...ids].sort();
    const valuesFrom = value => ({title: value.title || '', due: value.due || null, assignee_ids: sortedIds(value.assignee_ids || []), archived: !!value.archived, status: value.status || 'open'});
    const assigneeBoxes = () => [...host.querySelectorAll('[name="assignee"]')];
    let baseline = task ? valuesFrom(task) : valuesFrom({assignee_ids: assigned}), surface = null;
    const showValues = values => {
      const f = form.elements;
      f.title.value = values.title; f.due.value = values.due || ''; f.status.value = values.status;
      if (f.archived) f.archived.checked = values.archived;
      for (const box of assigneeBoxes()) box.checked = values.assignee_ids.includes(box.value);
    };
    if (special) {
      surface = recordSurfaceMount(host.querySelector('[data-record-surface]'), {task: {id: task.id, task_type: task.task_type, business_id: null, email_conversation_id: task.email_conversation_id}, biz: null,
        // A panel save updates this editor's task in place; the conversation beside the panel is never remounted.
        onTask: fresh => {
          Object.assign(task, fresh);
          const index = rows.findIndex(item => item.id === task.id);
          if (index >= 0) Object.assign(rows[index], fresh);
          // Title and Archived follow a panel change only while the form has no edit of its own for them.
          const f = form.elements, next = valuesFrom(task);
          if (f.title.value.trim() === baseline.title) f.title.value = next.title;
          if (f.archived && f.archived.checked === baseline.archived) f.archived.checked = next.archived;
          baseline = {...baseline, title: next.title, archived: next.archived};
        },
        openTask: link => link.business_id ? taskLinkOpen(link) : open(link.task_id)});
      recordSurface = surface;
      if (recordPanelOpen) surface?.open();
    } else recordSurface = null;
    editorParts = {surface, progress, typeEditor};
    if (task && typeof taskLinksMount==='function') taskLinksMount(host.querySelector('.task-cross-links'), null, task, {open: link => embedded && typeof onNavigate === 'function'
      ? onNavigate({type:'task', id:link.id || link.task_id, business_id:link.business_id ?? null, business_slug:link.business_slug ?? null, label:link.label || link.title || link.id || link.task_id})
      : taskLinkOpen(link)});
    if (task && embedded && typeof renderRelated === 'function') {
      token.related?.dispose?.(); token.related = renderRelated(host.querySelector('.global-task-related'), task) || null;
    } else if (task) workRelatedChips(host.querySelector('.global-task-related'), null, 'tasks', task.id);
    function paintFamily() {
      const family = host.querySelector('[data-task-family]'); if (!family || !task) return;
      family.innerHTML = [...(task.parent ? [['Parent', task.parent]] : []), ...(task.children || []).map(item => ['Child', item])].map(([role, other]) => `<button type="button" class="chip task-link-chip" data-open-task="${esc(other.id)}">${esc(role)}: ${taskShowsState(other) ? esc(T_LABEL[other.status] || other.status)+' · ' : taskTypeIcon(other.task_type)}${esc(tChipLabel(other.title))}${customerStateChip(other)}</button>`).join(' ') || '<p class="muted">No parent or child tasks.</p>';
      if (task.children_has_more) family.innerHTML += ' <span class="muted">and more</span>';
      family.querySelectorAll('[data-open-task]').forEach(button => button.onclick = () => goTask(button.dataset.openTask, button.title, task));
    }
    paintFamily();
    taskSnoozeWire(host);
    const tagPicker = tagPickerMount(host.querySelector('[data-task-tags]'), {biz: null, labels: task?.tags || [], label: 'Tags'});
    const initialTypeValues = !task ? JSON.stringify(typeEditor.values()) : null;
    token.dirty = () => {
      try {
        if (child?.controller?.dirty()) return true;
        if (task ? typeEditor?.dirty?.() : JSON.stringify(typeEditor.values()) !== initialTypeValues) return true;
        const f = form.elements;
        const assignees = [...host.querySelectorAll('[name="assignee"]:checked')].map(box => box.value);
        const current = valuesFrom({title: f.title?.value || '', due: f.due?.value || null, assignee_ids: assignees,
          archived: !!f.archived?.checked, status: f.status?.value || 'open'});
        if (JSON.stringify(current) !== JSON.stringify(baseline)) return true;
        if (taskSnoozeChange(host, task || {}) !== undefined) return true;
        if (tagPicker && JSON.stringify(tagNormalize(tagPicker.labels() || [])) !== JSON.stringify(tagNormalize(task?.tags || []))) return true;
      } catch (_) { return true; }
      return false;
    };
    form.onsubmit = async e => {
      e.preventDefault(); if (busy || attempt || confirming) return;
      const f = form.elements, title = f.title.value.trim();
      if (!title) { message('A title is required.'); return; }
      let changes;
      try {
        const values = typeEditor.values(), snooze = taskSnoozeChange(host, task || {});
        if (!task) {
          changes = {title, due: f.due.value || null, assignee_ids: assigneeBoxes().filter(i => i.checked).map(i => i.value), tags: tagPicker.labels(), ...values, ...(initialParentId != null ? {parent_id: initialParentId} : {})};
          if (taskShowsState(changes)) changes.status = f.status.value;
          if (snooze !== undefined) changes.snoozed_until = snooze;
        } else {
          // An update carries only what the user changed here; panel-owned fields and a stale Archived flag are never resent.
          const now = {title, due: f.due.value || null, assignee_ids: sortedIds(assigneeBoxes().filter(i => i.checked).map(i => i.value)), archived: f.archived.checked, status: f.status.value};
          changes = {};
          for (const key of ['title', 'due', 'assignee_ids', 'archived']) if (JSON.stringify(now[key]) !== JSON.stringify(baseline[key])) changes[key] = now[key];
          if (JSON.stringify(tagNormalize(tagPicker.labels())) !== JSON.stringify(tagNormalize(task.tags || []))) changes.tags = tagPicker.labels();
          Object.assign(changes, taskTypeChanges(task, values));
          if (taskShowsState({task_type: values.task_type}) && now.status !== baseline.status) changes.status = now.status;
          if (snooze !== undefined) changes.snoozed_until = snooze;
          if (!Object.keys(changes).length) { message('There are no changes to save.'); return; }
        }
      } catch (error) { message(error.message); return; }
      // Leaving Customer clears its details; the owner confirms that before anything is sent.
      confirming = true;
      const confirmed = await typeEditor.confirmConversion();
      confirming = false;
      if (!confirmed || !current()) return;
      const args = task ? {task_id: task.id, changes, expected_revision: task.revision} : {...changes, id: newRequestId()};
      attempt = {name: task ? 'tasks_update' : 'tasks_create', args: {...args, idempotency_key: newRequestId()}};
      attempt.apply = fresh => {
        // Saving the same Story/Customer/Email record keeps its surface and conversation mounted.
        if (!task || !recordSurface || fresh.id !== task.id || fresh.task_type !== task.task_type || !TASK_SPECIAL_TYPES.includes(fresh.task_type)) return false;
        for (const key of Object.keys(task)) if (!(key in fresh)) delete task[key];
        Object.assign(task, fresh);
        const index = rows.findIndex(item => item.id === task.id);
        if (index >= 0) Object.assign(rows[index], fresh);
        baseline = valuesFrom(task);
        showValues(baseline);
        recordSurface.sync(task);
        return true;
      };
      save();
    };
    host.querySelector('[data-retry]').onclick = save;
    host.querySelector('[data-reload]').onclick = () => { attempt = null; if (selected) open(selected.id, {adoptType: true}); else edit(null); };
  }

  async function save() {
    if (busy || !attempt) return;
    busy = true; message('Saving…');
    host.querySelectorAll('input,select,textarea,button').forEach(e => e.disabled = true);
    try {
      const data = await invoke(attempt.name, attempt.args);
      if (initialCreate && attempt.name === 'tasks_create' && (!data?.task || typeof data.task !== 'object' || typeof data.task.id !== 'string' || !data.task.id.trim() || data.task.id !== attempt.args.id)) {
        throw Object.assign(new Error('The create response was invalid.'), {code: 'invalid_response'});
      }
      const apply = attempt.apply;
      const submitted = attempt.args.changes;
      attempt = null;
      if (current()) {
        const fresh = taskStateObserve(null, data.task);
        // The workspace owns Task subtype adoption. Only the canonical successful response is
        // passed; a handled result has already disposed this editor or deliberately kept it.
        if (initialCreate && !selected && session) {
          void refreshTasks();
          if (onCreated) await onCreated(fresh);
          busy = false;
          token.dirty = () => false;
          await session.close();
          return;
        }
        if (onCanonical && await onCanonical(fresh, {reload: false, changes: submitted})) return;
        if (apply?.(fresh)) {
          host.querySelectorAll('input,select,textarea,button').forEach(e => e.disabled = false);
          message('Saved.');
        } else { const panelOpen = !!recordSurface?.isOpen(); edit(fresh, {recordPanelOpen: panelOpen}); message('Saved.'); }
      }
    } catch (error) {
      if (!current()) return;
      const conflict = error.code === 'revision_conflict';
      const create = initialCreate && attempt?.name === 'tasks_create';
      const uncertain = ['request_timeout', 'invalid_response'].includes(error.code) || [408, 429].includes(error.httpStatus);
      const definiteCreate = !uncertain && (['validation_error', 'forbidden', 'policy_denied', 'invalid_principal', 'not_found', 'revision_conflict'].includes(error.code) || [400, 401, 403, 404, 409, 422].includes(error.httpStatus));
      if (create ? definiteCreate : !conflict && error.httpStatus >= 400 && error.httpStatus < 500) {
        attempt = null;
        host.querySelectorAll('input,select,textarea,button').forEach(e => e.disabled = false);
        message(uiFailure(error));
        return;
      }
      message(conflict && !create ? 'Changed since you opened it. Refresh' : "Couldn't confirm. Try again");
      const button = host.querySelector(conflict && !create ? '[data-reload]' : '[data-retry]');
      button.hidden = false; button.disabled = false;
    } finally { busy = false; }
  }

  shell('<p>Loading Global tasks…</p>');
  Promise.all([invoke('assignees_list'), loadGlobalAgentProfiles().catch(() => [])]).then(([a]) => {
    if (!current()) return;
    assignees = a.assignees; if (initialCreate) edit(null); else { list(); initialTaskId ? open(initialTaskId) : load(); }
  }).catch(error => {
    if (!current()) return;
    message(uiFailure(error, {load: true}));
    const retry = document.createElement('button'); retry.textContent = 'Retry'; retry.onclick = () => globalTasksMount(host, {agentId, heading, compact, initialTaskId, initialCreate, initialParentId, session, onCreated, onBack, embedded, onNavigate, renderRelated, onCanonical});
    host.querySelector('.global-task-content').appendChild(retry);
  });
  return token;
}

window.taskOpenGlobalCreate = function () {
  if (typeof embeddedAgentPane !== 'undefined' && embeddedAgentPane) return window.parent.taskOpenGlobalCreate();
  if (!window.focusExpansion?.open) { toast('Focus is unavailable.'); return null; }
  return window.focusExpansion.open({title: 'New Global task', mount(host, session) {
    globalTasksMount(host, {initialCreate: true, session});
  }});
};
