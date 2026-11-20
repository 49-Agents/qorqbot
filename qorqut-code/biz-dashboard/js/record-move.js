// Move to project for whole Tables and project notes. Same contract as the Task move: the source is the record's own project,
// the destination is one the owner chose (the Task move's destination list), the preview is a read-only snapshot, and Move
// is sent only for the exact preview on screen with one idempotency key per attempt; a transport failure retries that key.
const RECORD_MOVE_KINDS = {
  table: {preview: 'tables_move_to_project_preview', move: 'tables_move_to_project', key: 'table_ids', listed: 'tables', noun: 'table',
    counts: [['rows', 'rows'], ['uploads', 'uploaded files'], ['crm_entries', 'CRM entries'], ['versions', 'history captures'],
      ['comments', 'comments'], ['project_links', 'project links'], ['historical_relationships', 'links kept in this project as history']]},
  note: {preview: 'notes_move_to_project_preview', move: 'notes_move_to_project', key: 'document_ids', listed: 'notes', noun: 'note',
    counts: [['versions', 'earlier versions'], ['project_links', 'project links'], ['historical_relationships', 'links kept in this project as history']]},
};

const RECORD_MOVE_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 9V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H20a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-1"/><path d="M2 13h10"/><path d="m9 16 3-3-3-3"/></svg>';

// kind: 'table' | 'note'; record: {id, title}; sourceSlug and sourceBusinessId name the record's own project.
// onMoved(destination, result) runs after the server confirmed the move.
function recordMoveOpen({kind, record, sourceSlug, sourceBusinessId, onMoved = null} = {}) {
  const spec = RECORD_MOVE_KINDS[kind];
  if (!spec || !record?.id || !sourceBusinessId) { toast('Only a project’s tables and notes can be moved.'); return null; }
  let alive = true, el = null, closeModal = null, targets = null, destinationId = '', preview = null, generation = 0;
  let pending = false, attempt = null, failure = '', notice = '';
  const call = (operation, args) => taskMoveCall(operation, args, sourceSlug);
  const text = (tag, className, value) => { const node = document.createElement(tag); if (className) node.className = className; node.textContent = String(value ?? ''); return node; };
  const body = `<div class="tmove"><p class="tmove-source" data-move-source></p>
    <label class="tmove-field">Destination project<select data-move-destination aria-label="Destination project" disabled><option value="">Loading projects…</option></select></label>
    <div class="tmove-preview" data-move-preview aria-live="polite"></div><p class="ferr" role="alert" data-move-error hidden></p></div>`;

  function paint() {
    if (!el || !alive) return;
    el.querySelector('[data-move-source]').textContent = `Move the ${spec.noun} “${record.title || record.id}” to another project.`;
    const select = el.querySelector('[data-move-destination]');
    select.disabled = pending || !targets || !targets.length;
    if (targets && select.options.length !== targets.length + 1) {
      select.replaceChildren(new Option(targets.length ? 'Choose a project' : 'No other project is available', ''),
        ...targets.map(project => new Option(project.name, project.id)));
      select.value = destinationId;
    }
    const box = el.querySelector('[data-move-preview]'); box.replaceChildren();
    if (notice) box.append(text('p', 'tmove-notice', notice));
    if (destinationId && !preview && !failure) box.append(text('p', 'muted', 'Reading what would move…'));
    if (preview) {
      box.append(text('p', 'tmove-route', `From ${preview.source?.name} to ${preview.destination?.name}`));
      const counts = document.createElement('ul'); counts.className = 'tmove-counts';
      for (const [key, noun] of spec.counts) counts.append(text('li', '', `${preview.counts?.[key] ?? 0} ${noun}`));
      box.append(counts);
      if (preview.visibility?.summary) box.append(text('p', 'tmove-visibility', preview.visibility.summary));
      if ((preview.blockers || []).length) {
        const list = document.createElement('ul'); list.className = 'tmove-blockers'; list.setAttribute('role', 'alert');
        for (const blocker of preview.blockers) list.append(text('li', '', blocker.message || blocker.code));
        box.append(text('p', 'tmove-blocked', 'This move is blocked:'), list);
      }
    }
    const error = el.querySelector('[data-move-error]'); error.hidden = !failure; error.textContent = failure;
    const confirm = el.querySelector('[data-move-confirm]');
    if (confirm) { confirm.disabled = pending || !preview || preview.can_move !== true; confirm.textContent = pending ? 'Moving…' : 'Move'; }
  }
  async function loadTargets() {
    try {
      const data = await taskMoveCall('tasks_move_targets', {source_business_id: sourceBusinessId}, sourceSlug);
      if (!alive) return;
      targets = (data.projects || []).filter(project => project?.id && project.name != null);
    } catch (error) { if (!alive) return; targets = []; failure = uiFailure(error, {load: true}); }
    paint();
  }
  async function loadPreview() {
    const mine = ++generation; preview = null; attempt = null; failure = '';
    if (!destinationId) return paint();
    paint();
    try {
      const data = await call(spec.preview, {source_business_id: sourceBusinessId, destination_project_id: destinationId, [spec.key]: [record.id]});
      if (!alive || mine !== generation) return;
      if (!data.preview?.token) throw Object.assign(new Error('invalid'), {code: 'invalid_response'});
      preview = data.preview;
    } catch (error) { if (!alive || mine !== generation) return; failure = uiFailure(error, {load: true}); }
    paint();
  }
  async function submit() {
    if (pending || !preview || preview.can_move !== true) return;
    if (!attempt || attempt.token !== preview.token) attempt = {key: newRequestId(), token: preview.token};
    pending = true; failure = ''; notice = ''; paint();
    try {
      const data = await call(spec.move, {source_business_id: sourceBusinessId, destination_project_id: destinationId, [spec.key]: [record.id],
        preview_token: preview.token, idempotency_key: attempt.key});
      if (!data.destination?.id) throw Object.assign(new Error('invalid'), {code: 'invalid_response'});
      pending = false;
      try { clearEtagCache(); storeInvalidateAll(); } catch (_) { /* the caches are an optimisation */ }
      const open = alive; closeModal?.();
      toast(`Moved to ${data.destination.name}.`);
      if (open) onMoved?.(data.destination, data);
    } catch (error) {
      pending = false;
      if (!alive) return;
      if (taskMoveUncertain(error)) { failure = 'Couldn’t confirm the move. Try Move again to retry safely.'; return paint(); }
      attempt = null;
      notice = error.code === 'revision_conflict' ? `The ${spec.noun} changed since the preview. Review the updated preview before moving.`
        : error.code === 'move_blocked' ? 'The move is blocked. Review the updated preview.' : uiFailure(error);
      await loadPreview();
    }
  }
  const dialog = modal({title: 'Move to project', width: 520, stack: true, body,
    actions: [{label: 'Cancel', onClick: close => close()}, {label: 'Move', primary: true, onClick: () => void submit()}],
    onOpen: root => {
      el = root;
      const foot = el.querySelectorAll('.mfoot button'); foot[1].dataset.moveConfirm = ''; foot[1].disabled = true;
      el.querySelector('[data-move-destination]').onchange = event => { destinationId = event.target.value; notice = ''; void loadPreview(); };
      paint(); void loadTargets();
    },
    onClose: () => { alive = false; generation++; el = null; }});
  closeModal = dialog.close;
  return dialog;
}
