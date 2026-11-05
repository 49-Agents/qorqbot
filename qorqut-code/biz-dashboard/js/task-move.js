// Move to project: one controller for saved Tasks, Stories and Customers on every surface (record workspace, Task
// drawer, Global editor). The source scope is always the record's own canonical business_id (null is Global) and the
// destination is always a project the owner chose from tasks_move_targets; nothing is inferred from the sidebar.
// A preview is a read-only snapshot. Move is only ever sent for the exact preview on screen, with one idempotency key
// per attempt; a transport failure retries that same key and payload, anything else needs a fresh preview.
const TASK_MOVE_COUNTS = [['tasks', 'tasks (with descendants)'], ['comments', 'comments'], ['private_messages', 'private messages'],
  ['project_links', 'project links'], ['focus_cards', 'Pinned cards'], ['crm_entries', 'CRM entries']];
const TASK_MOVE_LISTED = 50;
const taskMoveCall = async (operation, args, slug = null) =>
  coreData(await post(`${slug == null ? '/root' : `/biz/${encodeURIComponent(slug)}`}/operations/${operation}`, args));
// A reply that carries a server code is a decision; anything else (network, timeout, 5xx, torn body) may have committed.
const taskMoveUncertain = error => !error?.code || error.code === 'invalid_response' || error.code === 'request_timeout' || error.httpStatus >= 500;
// The canonical opening reference named by a record_moved error, or null when it is not complete.
function taskMoveDestination(error) {
  const details = error?.details || {}, destination = details.destination, task = details.task;
  if (error?.code !== 'record_moved' || !destination?.id || !task?.id || task.type !== 'task') return null;
  return {destination, task: {type: 'task', id: task.id, business_id: task.business_id ?? null}};
}
// Old-scope caches go only after the server confirmed the move; the refresh then re-reads every affected list.
// The status cache is keyed by project slug (null for Global); map the canonical project ids explicitly.
const taskMoveSlug = businessId => businessId == null ? null
  : (state.bizs || []).find(b => b.id === businessId || b.business_id === businessId)?.slug;
function taskMoveInvalidate(ids, sourceBusinessId, destination) {
  try { clearEtagCache(); } catch (_) { /* the caches are an optimisation */ }
  try {
    const source = taskMoveSlug(sourceBusinessId), target = destination?.slug ?? taskMoveSlug(destination?.id);
    if (source !== undefined) taskStateForget(source, ids);
    if (target !== undefined) taskStateForget(target, ids);
  } catch (_) { /* idem */ }
  try { if (typeof tGen === 'number') tGen++; void refreshTasks?.(); } catch (_) { /* its own error state */ }
  try { storeInvalidateAll(); } catch (_) { /* idem */ }
  try { if (typeof _agpad !== 'undefined' && _agpad && typeof agfocusRefresh === 'function') void agfocusRefresh(_agpad); } catch (_) { /* idem */ }
  document.dispatchEvent(new CustomEvent('qorqut-task-moved', {detail: {ids: [...ids], destination}}));
}

// task: {id, title, task_type?}; sourceBusinessId: the record's canonical scope (null is Global, undefined refuses).
// onMoved(reference, result) runs only while the dialog is still open; the invalidation above always runs.
// onMovedElsewhere(reference) is the owner's choice to open a record the server reports as already moved.
function taskMoveOpen({task, sourceBusinessId, sourceName = '', onMoved = null, sourceSlug = null, onSourceLost = null, onSourceMoved = null, onMovedElsewhere = null, onClosed = null} = {}) {
  if (!task?.id || sourceBusinessId === undefined) { toast('This record’s project is unavailable.'); return null; }
  const taskIds = [String(task.id)];
  let alive = true, closeModal = null, el = null;
  let targets = null, targetsFailed = false, destinationId = '', preview = null, previewGeneration = 0, previewLoading = false;
  let notice = '', failure = '', pending = false, attempt = null, moved = null;
  const select = () => el?.querySelector('[data-move-destination]');
  const confirmButton = () => el?.querySelector('[data-move-confirm]');
  const label = value => String(value ?? '');

  const body = `<div class="tmove">
    <p class="tmove-source" data-move-source></p>
    <label class="tmove-field">Destination project<select data-move-destination aria-label="Destination project" disabled><option value="">Loading projects…</option></select></label>
    <div class="tmove-preview" data-move-preview aria-live="polite"></div>
    <p class="ferr" role="alert" data-move-error hidden></p></div>`;

  function paint() {
    if (!el || !alive) return;
    const sel = select(), source = preview?.source?.name || sourceName || (sourceBusinessId == null ? 'Global' : 'its current project');
    el.querySelector('[data-move-source]').textContent = `Move “${label(task.title || task.id)}” from ${source}.`;
    sel.disabled = pending || !targets || !targets.length;
    if (targets && sel.dataset.ready !== 'yes') {
      sel.replaceChildren(new Option(targets.length ? 'Choose a project…' : 'No other active projects', ''));
      for (const project of targets) sel.append(new Option(project.name, project.id));
      sel.dataset.ready = 'yes';
    }
    sel.value = destinationId;
    const box = el.querySelector('[data-move-preview]');
    box.replaceChildren();
    if (targetsFailed) box.append(taskMoveText('p', 'muted', 'Projects could not be loaded.'), taskMoveButton('Retry', () => void loadTargets()));
    else if (!destinationId) box.append(taskMoveText('p', 'muted', targets && !targets.length ? 'There is no other active project to move this record to.' : 'Choose a destination to see exactly what moves.'));
    else if (previewLoading) box.append(taskMoveText('p', 'muted', 'Loading preview…'));
    else if (preview) paintPreview(box);
    const errorEl = el.querySelector('[data-move-error]');
    errorEl.textContent = [notice, failure].filter(Boolean).join(' ');
    errorEl.hidden = !errorEl.textContent;
    if (moved) {
      const where = moved;
      if (onMovedElsewhere) errorEl.append(' ', taskMoveButton('Open the record in its new project', () => {
        const reference = {type: 'task', id: where.task.id, business_id: where.task.business_id ?? where.destination.id, business_slug: where.destination.slug ?? null,
          business_name: where.destination.name ?? null, label: task.title, task_type: task.task_type};
        closeModal?.();
        onMovedElsewhere(reference);
      }));
    }
    const ok = !!preview && !previewLoading && preview.can_move === true && !(preview.blockers || []).length && !pending && !moved;
    const confirm = confirmButton();
    confirm.disabled = !ok;
    confirm.textContent = pending ? 'Moving…' : 'Move';
    el.querySelector('[data-move-cancel]').textContent = pending ? 'Close' : 'Cancel';
    if (pending) box.append(taskMoveText('p', 'muted tmove-pending', 'Closing this dialog does not cancel the move; it stays in progress.'));
    el.querySelector('.tmove').setAttribute('aria-busy', String(pending || previewLoading));
  }
  function taskMoveText(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; node.textContent = text; return node; }
  function taskMoveButton(text, onClick) { const b = taskMoveText('button', 'quiet', text); b.type = 'button'; b.onclick = onClick; return b; }
  function paintPreview(box) {
    const titles = new Map((preview.tasks || []).map(row => [row.id, row.title]));
    const heading = taskMoveText('p', 'tmove-route', `From ${label(preview.source?.name || 'Global')} to ${label(preview.destination?.name)}`);
    const counts = document.createElement('ul'); counts.className = 'tmove-counts';
    for (const [key, noun] of TASK_MOVE_COUNTS) counts.append(taskMoveText('li', '', `${preview.counts?.[key] ?? 0} ${noun}`));
    box.append(heading, counts);
    const roots = (preview.roots || []).filter(root => root.previous_parent_id != null);
    if (roots.length) box.append(taskMoveText('p', 'tmove-detach', roots.length === 1
      ? 'This record becomes top-level in the destination and leaves its current parent, which stays where it is.'
      : `${roots.length} selected records become top-level in the destination and leave their current parents, which stay where they are.`));
    else box.append(taskMoveText('p', 'tmove-detach', 'This record becomes top-level in the destination.'));
    const list = document.createElement('ul'); list.className = 'tmove-tasks'; list.setAttribute('aria-label', 'Records that move');
    for (const row of (preview.tasks || []).slice(0, TASK_MOVE_LISTED)) {
      list.append(taskMoveText('li', row.archived ? 'tmove-archived' : '', `${label(row.title || row.id)}${row.archived ? ' (archived)' : ''}`));
    }
    box.append(list);
    if ((preview.tasks || []).length > TASK_MOVE_LISTED) box.append(taskMoveText('p', 'muted', `and ${(preview.tasks || []).length - TASK_MOVE_LISTED} more`));
    box.append(taskMoveText('p', 'tmove-visibility', 'Ordinary task access follows the destination project’s rules. Private owner–Ordubashy conversations stay private, and moving never grants anyone membership.'));
    if (preview.visibility?.summary) box.append(taskMoveText('p', 'tmove-visibility', label(preview.visibility.summary)));
    if ((preview.blockers || []).length) {
      const blockers = document.createElement('ul'); blockers.className = 'tmove-blockers'; blockers.setAttribute('role', 'alert');
      for (const blocker of preview.blockers) {
        const row = taskMoveText('li', '', `${label(blocker.message || blocker.code)}${blocker.task_id && titles.get(blocker.task_id) ? ` (${titles.get(blocker.task_id)})` : ''}`);
        blockers.append(row);
      }
      box.append(taskMoveText('p', 'tmove-blocked', 'This move is blocked:'), blockers);
    }
  }

  // A denial must not disclose anything, so every lost-access reply ends in the same plain message.
  function lost() {
    preview = null; previewLoading = false; targets = []; destinationId = ''; failure = 'You no longer have access to move this record.';
    if (select()) select().dataset.ready = '';
    paint(); void revalidateSource();
  }
  // A denial may concern only the destination. Re-read the source through its normal read: if that is denied or gone the
  // surface drops the record; otherwise the source and its edits stay.
  async function revalidateSource() {
    try {
      await taskMoveCall('tasks_get', {task_id: taskIds[0]}, sourceSlug);
    } catch (error) {
      // The result belongs to this dialog and the editor that opened it; once either is gone it is ignored.
      if (!alive) return;
      if (accessLost(error) || sourceGone(error)) onSourceLost?.();
      else if (error.code === 'record_moved') onSourceMoved?.(error);
    }
  }
  function reportMoved(error) {
    const where = taskMoveDestination(error);
    preview = null; previewLoading = false;
    if (where) { moved = where; failure = `This record has moved to ${label(where.destination.name || 'another project')}.`; }
    else failure = 'This record has moved.';
    paint();
  }

  async function loadTargets() {
    targetsFailed = false; failure = ''; paint();
    try {
      const data = await taskMoveCall('tasks_move_targets', {source_business_id: sourceBusinessId});
      if (!alive) return;
      if (!Array.isArray(data.projects)) throw Object.assign(new Error('invalid'), {code: 'invalid_response'});
      targets = data.projects.filter(project => project?.id && project.name != null);
    } catch (error) {
      if (!alive) return;
      if (accessLost(error)) return lost();
      if (error.code === 'record_moved') return reportMoved(error);
      targets = null; targetsFailed = true;
      failure = error.code === 'result_too_large' ? 'There are too many projects to list.' : uiFailure(error, {load: true});
    }
    paint();
  }
  async function loadPreview({keepNotice = false} = {}) {
    const generation = ++previewGeneration, destination = destinationId;
    preview = null; attempt = null; failure = ''; if (!keepNotice) notice = '';
    if (!destination) { previewLoading = false; paint(); return; }
    previewLoading = true; paint();
    try {
      const data = await taskMoveCall('tasks_move_to_project_preview', {source_business_id: sourceBusinessId, destination_project_id: destination, task_ids: taskIds});
      if (!alive || generation !== previewGeneration) return;
      if (!data.preview?.token) throw Object.assign(new Error('invalid'), {code: 'invalid_response'});
      preview = data.preview;
    } catch (error) {
      if (!alive || generation !== previewGeneration) return;
      if (accessLost(error)) return lost();
      if (error.code === 'record_moved') return reportMoved(error);
      failure = uiFailure(error, {load: true});
    }
    previewLoading = false; paint();
  }
  async function submit() {
    if (pending || !preview || preview.can_move !== true || (preview.blockers || []).length) return;
    // One key per exact (destination, token) attempt: a transport retry repeats it, a new preview never does.
    if (!attempt || attempt.token !== preview.token || attempt.destination !== destinationId) {
      attempt = {key: newRequestId(), token: preview.token, destination: destinationId};
    }
    const request = {source_business_id: sourceBusinessId, destination_project_id: destinationId, task_ids: taskIds,
      preview_token: preview.token, idempotency_key: attempt.key};
    pending = true; failure = ''; notice = ''; paint();
    try {
      const data = await taskMoveCall('tasks_move_to_project', request);
      const mine = (data.moved || []).find(row => String(row.id) === taskIds[0]);
      if (!data.destination?.id || !mine) throw Object.assign(new Error('invalid'), {code: 'invalid_response'});
      pending = false;
      taskMoveInvalidate(data.moved.map(row => row.id), sourceBusinessId, data.destination);
      const reference = {type: 'task', id: taskIds[0], business_id: mine.business_id ?? data.destination.id,
        business_slug: data.destination.slug ?? null, business_name: data.destination.name ?? null, label: task.title, task_type: task.task_type};
      const open = alive;
      closeModal?.();
      if (open) onMoved?.(reference, data);
    } catch (error) {
      pending = false;
      if (!alive) return;
      if (taskMoveUncertain(error)) { failure = 'Couldn’t confirm the move. Try Move again to retry safely.'; paint(); return; }
      attempt = null;
      if (accessLost(error)) return lost();
      if (error.code === 'record_moved') return reportMoved(error);
      if (error.code === 'revision_conflict' || error.code === 'move_blocked') {
        // Keep the chosen destination, but never reuse the old preview: show a fresh one and wait for an explicit Move.
        notice = error.code === 'revision_conflict' ? 'The record changed since the preview. Review the updated preview before moving.'
          : 'The move is blocked. Review the updated preview.';
        return void loadPreview({keepNotice: true});
      }
      notice = uiFailure(error);
      return void loadPreview({keepNotice: true});
    }
  }

  const dialog = modal({title: 'Move to project', width: 560, stack: true, body,
    actions: [{label: 'Cancel', onClick: close => close()}, {label: 'Move', primary: true, onClick: () => void submit()}],
    onOpen: root => {
      el = root;
      const foot = el.querySelectorAll('.mfoot button');
      foot[0].dataset.moveCancel = ''; foot[1].dataset.moveConfirm = ''; foot[1].disabled = true;
      select().onchange = () => { destinationId = select().value; moved = null; void loadPreview(); };
      paint(); void loadTargets();
    },
    onClose: () => { alive = false; previewGeneration++; el = null; onClosed?.(); }});
  closeModal = dialog.close;
  return {close: dialog.close, el: dialog.el};
}

// The canonical opener for surfaces that are not the record workspace (Task drawer, Global editor).
function taskMoveOpenReference(reference) {
  return Promise.resolve(taskLinkOpen({business_id: reference.business_id, business_slug: reference.business_slug, task_id: reference.id}))
    .catch(error => toast(uiFailure(error, {load: true})));
}
