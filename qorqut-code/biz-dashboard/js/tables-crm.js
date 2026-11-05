// The CRM view of a Table on the existing Tables screen (project and Global). Rows are memberships in independent Task
// records: shared fields are read from the Task and open its editor, local cells keep their draft/save/conflict behaviour,
// and stage changes save independently. Remove deletes only the membership. Depends on js/crm.js and js/tables.js.
function crmTableMount(host, {biz, table, onTable, onOpenRecord, draftKey, drafts, isActive, onUnavailable, onAvailable}) {
  let current = table, entries = [], cursor = null, loading = false, generation = 0, alive = true, saving = false, unavailable = false, timer;
  let error, tbody, empty, metaEpoch = 0, stageUndo = null;
  const view = {query: '', order: '', descending: false, archived: false, removed: false};
  const draft = () => drafts.get(draftKey);
  const readonly = () => !!current.archived;
  const stages = () => current.crm_config?.stages || [];
  const activeStages = () => stages().filter(stage => !stage.archived);
  const columns = () => (current.columns || []).filter(column => {
    const id = String(column.id || '');
    const name = String(column.name || '').trim().toLowerCase();
    if (['next_step', 'next-step', 'nextStep'].includes(id) || name === 'next step') return false;
    return column.record_field !== 'title';
  });
  const $q = selector => host.querySelector(selector);
  const fail = (failure, text) => { error.textContent = text ?? uiFailure(failure); error.hidden = false; };
  const dirty = () => !!draft() && (draft().edits.size > 0);
  const originalLocal = (entry, column) => entry.local_cells && Object.hasOwn(entry.local_cells, column.id) ? entry.local_cells[column.id] : entry.cells && Object.hasOwn(entry.cells, column.id) ? entry.cells[column.id] : null;
  const openRecord = entry => {
    try {
      const result = typeof onOpenRecord === 'function' ? onOpenRecord(entry) : crmOpenRecord(entry, {onClosed: () => refresh()});
      return result && typeof result.then === 'function' ? result.catch(failure => { if (alive && !unavailable) fail(failure); }) : result;
    } catch (failure) { if (alive && !unavailable) fail(failure); return undefined; }
  };

  const stageLabel = entry => stages().find(stage => stage.id === entry.stage_id)?.name || entry.stage_id || 'No stage';
  const stageButton = entryId => [...host.querySelectorAll('tr[data-entry-id]')].find(row => row.dataset.entryId === entryId)?.querySelector('.crm-stage-cycle');
  function stageAfter(entry) {
    const available = activeStages();
    if (!available.length || entry.removed || entry.unavailable) return null;
    const index = available.findIndex(stage => stage.id === entry.stage_id);
    return available[index < 0 ? 0 : (index + 1) % available.length];
  }
  const nextStage = entry => readonly() || saving || entry._stageSaving ? null : stageAfter(entry);
  function clearStageUndo(message = '') {
    if (!stageUndo) return;
    stageUndo = null;
    const undo = $q('[data-crm-undo]');
    if (undo) { undo.hidden = true; undo.disabled = true; undo.onclick = null; }
    if (message && alive && !unavailable) $q('[data-crm-status]').textContent = message;
  }
  async function cycleStage(entry) {
    const next = nextStage(entry); if (!next) return;
    const previous = entry.stage_id ?? null;
    const restoreFocus = host.contains(document.activeElement) && document.activeElement.classList.contains('crm-stage-cycle');
    entry._stageSaving = true; saving = true; error.hidden = true; syncDraft(); paint();
    try {
      const args = {table_id: current.id, entry_id: entry.id, expected_revision: current.revision, changes: {stage_id: next.id}};
      const result = await crmCall(biz, 'crm_entries_update', {...args, idempotency_key: newRequestId()});
      if (!live()) return;
      adopt(result.table);
      if (draft()) draft().baseRevision = result.table.revision;
      const changed = entries.find(item => item.id === entry.id);
      if (changed) changed.stage_id = next.id;
      stageUndo = {entryId: entry.id, previous, current: next.id, revision: result.table.revision};
      const feedback = $q('[data-crm-status]');
      feedback.textContent = `${entry.record?.title || 'Record'} moved to ${next.name}.`;
      const undo = $q('[data-crm-undo]'); undo.hidden = false; undo.disabled = dirty();
      undo.onclick = () => writeStage(stageUndo);
      paint();
    } catch (failure) {
      if (!alive) return;
      if (accessLost(failure) || sourceGone(failure)) return invalidate();
      const message = crmConflict(failure) ? 'The table changed. The stage was not saved; the latest records have been reloaded.' : uiFailure(failure);
      fail(failure, message);
      if (crmConflict(failure)) { await reloadTable(); if (alive && !unavailable) { await load(); fail(failure, message); } }
    } finally {
      entry._stageSaving = false; saving = false;
      if (alive && !unavailable) { paint(); if (restoreFocus) stageButton(entry.id)?.focus(); }
    }
  }
  async function writeStage(token) {
    if (!token || token !== stageUndo || saving || dirty() || readonly()) return;
    const entry = entries.find(item => item.id === token.entryId);
    if (!entry || entry.unavailable || entry.removed || entry.stage_id !== token.current || current.revision !== token.revision) {
      clearStageUndo('Undo is no longer available because this record changed.'); return;
    }
    const restoreFocus = host.contains(document.activeElement) && document.activeElement.hasAttribute('data-crm-undo');
    entry._stageSaving = true; saving = true; error.hidden = true; syncDraft(); paint();
    try {
      // Confirm the Table revision before using this Undo token. An agent may have changed the same stage since it was shown.
      const fresh = (await crmCall(biz, 'tables_get', {table_id: current.id})).table;
      if (!live()) return;
      if (fresh.revision !== token.revision) {
        adopt(fresh); await load();
        if (alive && !unavailable) $q('[data-crm-status]').textContent = 'The table changed elsewhere. Undo is no longer available.';
        clearStageUndo(); return;
      }
      if (entry.stage_id !== token.current) { clearStageUndo('The stage changed elsewhere. Undo is no longer available.'); return; }
      const args = {table_id: current.id, entry_id: token.entryId, expected_revision: token.revision, changes: {stage_id: token.previous}};
      const result = await crmCall(biz, 'crm_entries_update', {...args, idempotency_key: newRequestId()});
      if (!live()) return;
      adopt(result.table); if (draft()) draft().baseRevision = result.table.revision; entry.stage_id = token.previous;
      stageUndo = null;
      $q('[data-crm-status]').textContent = 'Stage change undone.';
      await load();
    } catch (failure) {
      if (!alive) return;
      if (accessLost(failure) || sourceGone(failure)) return invalidate();
      const message = crmConflict(failure) ? 'The table changed. The stage was not undone; the latest records have been reloaded.' : uiFailure(failure);
      fail(failure, message);
      if (crmConflict(failure)) { await reloadTable(); if (alive && !unavailable) { await load(); fail(failure, message); } }
    } finally {
      entry._stageSaving = false; saving = false;
      if (alive && !unavailable) { paint(); if (restoreFocus) stageButton(token.entryId)?.focus(); }
    }
  }
  const edit = (entry, patch) => {
    if (!draft()) drafts.set(draftKey, {crm: true, table: current, baseRevision: current.revision, edits: new Map(), attempts: new Map()});
    const record = draft().edits.get(entry.id) || {cells: new Map()};
    if (patch.stage !== undefined) { if (patch.stage === (entry.stage_id ?? '')) delete record.stage; else record.stage = patch.stage; }
    if (patch.cell) {
      const [column, value, same] = patch.cell;
      if (same) record.cells.delete(column.id); else record.cells.set(column.id, value);
    }
    if (!record.cells.size && record.stage === undefined) draft().edits.delete(entry.id); else draft().edits.set(entry.id, record);
    if (!draft().edits.size) drafts.delete(draftKey);
    syncDraft();
  };
  function syncDraft() {
    if (unavailable) return;
    const pending = dirty();
    $q('[data-crm-draft]').hidden = !pending;
    const undo = $q('[data-crm-undo]'); undo.hidden = !stageUndo; undo.disabled = pending || saving || !stageUndo;
    host.querySelectorAll('[data-crm-sort]').forEach(node => { node.disabled = pending || saving; });
    host.querySelectorAll('[data-crm-remove],[data-crm-restore]').forEach(node => { node.disabled = pending || saving || readonly(); node.title = pending ? 'Save or discard your edits first' : ''; });
    host.querySelectorAll('[data-crm-query],[data-crm-archived],[data-crm-removed],[data-crm-more]').forEach(node => { node.disabled = pending || saving; });
    // The submitted inputs are frozen while a write is pending, so a completing save can never discard a newer edit.
    if (saving) host.querySelectorAll('tbody input,tbody select,tbody textarea').forEach(node => { node.disabled = true; });
    $q('[data-crm-save]').disabled = saving; $q('[data-crm-discard]').disabled = saving;
    $q('[data-crm-save]').textContent = saving ? 'Saving...' : 'Save';
  }
  function paintHead() {
    const head = host.querySelector('thead');
    const sortable = column => !column.record_field && ['text', 'number', 'boolean', 'date'].includes(column.type);
    head.innerHTML = `<tr><th scope="col">Record</th>${columns().map(column => {
      const active = view.order === column.id, sort = sortable(column);
      return `<th scope="col" class="${column.type === 'number' ? 'crm-numeric' : ''}"${active ? ` aria-sort="${view.descending ? 'descending' : 'ascending'}"` : ''}>${sort ? `<button type="button" class="crm-sort" data-crm-sort="${esc(column.id)}" aria-label="Sort by ${esc(column.name)}">${esc(column.name)}</button>` : esc(column.name)}</th>`;
    }).join('')}${stages().length ? '<th scope="col">Stage</th>' : ''}<th scope="col">Task state</th><th scope="col"><span class="crm-sr">Actions</span></th></tr>`;
    $q('[data-crm-stage-filter]')?.remove();
  }
  function localCell(entry, column) {
    const td = crmNode('td', column.type === 'number' ? 'crm-numeric' : ''); td.dataset.column = column.id;
    const record = draft()?.edits.get(entry.id);
    const changed = record?.cells.has(column.id);
    const original = originalLocal(entry, column);
    const label = `${column.name} for ${entry.record?.title || 'entry'}`;
    td.classList.toggle('data-modified', !!changed);
    if (column.type === 'boolean') {
      const box = crmNode('input'); box.type = 'checkbox'; box.setAttribute('aria-label', label);
      const value = changed ? record.cells.get(column.id) : original;
      box.checked = value === true; box.indeterminate = value == null; box.disabled = readonly() || saving || !!entry.removed;
      box.onchange = () => { edit(entry, {cell: [column, box.checked, box.checked === original]}); td.classList.toggle('data-modified', draft()?.edits.get(entry.id)?.cells.has(column.id) || false); };
      td.append(box); return td;
    }
    const input = crmNode(column.type === 'text' || column.type === 'json' ? 'textarea' : 'input', 'data-cell-input');
    if (input.tagName === 'INPUT') { input.type = column.type; if (column.type === 'number') input.step = 'any'; }
    input.setAttribute('aria-label', label); input.dataset.cellInput = column.id;
    const shown = column.type === 'json' && original != null ? JSON.stringify(original) : dataValue(original);
    input.value = changed ? record.cells.get(column.id) : shown;
    input.disabled = readonly() || saving || !!entry.removed;
    input.oninput = () => {
      input.setCustomValidity('');
      try { dataParseCell(input.value, column, input); } catch (failure) { input.setCustomValidity(failure.message); }
      edit(entry, {cell: [column, input.value, input.value === shown && !input.validity.badInput]});
      td.classList.toggle('data-modified', draft()?.edits.get(entry.id)?.cells.has(column.id) || false);
    };
    td.append(input); return td;
  }
  function row(entry) {
    const tr = crmNode('tr'); tr.dataset.entryId = entry.id;
    if (entry.unavailable) {
      // An unavailable record shows nothing but its membership position: no cached title, cells, stage or scope.
      tr.classList.add('crm-unavailable');
      const td = crmNode('td', 'crm-unavailable-cell', 'This record is unavailable.'); td.colSpan = (current.columns || []).length + 1 + (stages().length ? 1 : 0) + 1;
      const actions = crmNode('td', 'crm-actions');
      actions.append(...(entry.removed ? [] : [removeButton(entry, 'this unavailable entry')]));
      tr.append(td, actions); return tr;
    }
    const record = entry.record || {};
    const ref = crmRecordRef(entry);
    const name = crmNode('td', 'crm-record-cell');
    const open = crmButton(`Open record ${record.title || ''}`, 'data-cell crm-open', record.title || 'Untitled');
    open.onclick = () => openRecord(entry);
    name.append(open);
    tr.append(name);
    tr.classList.toggle('crm-removed', !!entry.removed);
    for (const column of columns()) {
      if (column.record_field) {
        const td = crmNode('td', `crm-shared${column.type === 'number' ? ' crm-numeric' : ''}`); td.dataset.column = column.id;
        const button = crmButton(`${column.name}: ${crmValue(entry.cells?.[column.id])}. Open record to edit`, 'data-cell', crmValue(entry.cells?.[column.id]).slice(0, 300) || '-');
        button.title = 'Shared field. Opens the record to edit.'; button.onclick = () => openRecord(entry);
        td.append(button); tr.append(td);
      } else tr.append(localCell(entry, column));
    }
    if (stages().length) {
      const td = crmNode('td', 'crm-stage'), target = stageAfter(entry), canCycle = !!nextStage(entry);
      const reason = !activeStages().length ? 'No active stages' : readonly() ? 'Table is read only' : entry.removed ? 'Removed entry' : 'Save is in progress';
      const button = crmButton(`Advance stage for ${record.title || 'entry'}${target ? ` to ${target.name}` : ''}`, 'data-cell crm-stage-cycle', stageLabel(entry));
      button.disabled = !canCycle; button.setAttribute('aria-label', `Stage for ${record.title || 'entry'}: ${stageLabel(entry)}. ${target ? `Advance to ${target.name}` : reason}`);
      button.onclick = () => cycleStage(entry); td.append(button); tr.append(td);
    }
    // Task state is the Task's own; completing it never removes the entry or moves its stage.
    tr.append(crmNode('td', 'crm-task-state', record.task_type && record.task_type !== 'task' ? TASK_TYPE_LABELS[record.task_type] + (record.status ? ' · ' + (T_LABEL[record.status] || record.status) : '') : T_LABEL[record.status] || record.status || ''));
    const actions = crmNode('td', 'crm-actions');
    if (entry.removed) {
      const restore = crmButton(`Restore ${record.title || 'entry'} to CRM`, 'quiet', 'Restore'); restore.dataset.crmRestore = '';
      restore.onclick = () => runStructural(() => crmCall(biz, 'crm_entries_add', {table_id: current.id, expected_revision: current.revision, record: ref, idempotency_key: newRequestId()}));
      actions.append(restore);
    } else {
      actions.append(removeButton(entry, record.title || 'entry'));
    }
    tr.append(actions);
    tr.addEventListener('click', event => {
      const cell = event.target.closest('td');
      if (!cell || cell.classList.contains('crm-actions') || cell.classList.contains('crm-stage')) return;
      if (event.target.closest('button,input,select,textarea,a,[contenteditable="true"]')) return;
      openRecord(entry);
    });
    return tr;
  }
  function removeButton(entry, label) {
    const button = crmButton(`Remove ${label} from CRM`, 'quiet', 'Remove from CRM'); button.dataset.crmRemove = '';
    button.title = 'Removes the membership only. The record and its history are unchanged.';
    button.onclick = async () => {
      if (!await taskConfirm({title: 'Remove from this CRM?', message: 'This removes the entry from this table only. The record, its history and its other tables are unchanged.', confirmLabel: 'Remove from CRM'})) return;
      await runStructural(() => crmCall(biz, 'crm_entries_remove', {table_id: current.id, entry_id: entry.id, expected_revision: current.revision, idempotency_key: newRequestId()}));
    };
    return button;
  }
  async function runStructural(operation) {
    error.hidden = true;
    try { const result = await operation(); if (result?.table) adopt(result.table); await load(); }
    catch (failure) {
      if (!alive) return;
      if (accessLost(failure) || sourceGone(failure)) return invalidate();
      fail(failure); if (crmConflict(failure)) await reloadTable();
    }
  }
  // Table metadata (title, columns, stages, entry count) is adopted everywhere it is shown; local drafts are left untouched.
  // A reply that outlives its mount, its selection or an unavailable state is dropped before it can touch local or outer state.
  const live = () => alive && !unavailable && isActive();
  function adopt(next) {
    if (!live() || next?.id !== current.id) return;
    if (stageUndo && next.revision !== stageUndo.revision) clearStageUndo('The table changed elsewhere. Undo is no longer available.');
    current = next; const title = $q('.crm-title'); if (title) title.textContent = next.title || '';
    onTable?.(next); const pending = draft(); if (pending) pending.table = next;
  }
  async function reloadTable() {
    const epoch = ++metaEpoch;
    try {
      const fresh = (await crmCall(biz, 'tables_get', {table_id: current.id})).table;
      if (epoch !== metaEpoch || !live()) return;
      clearStageUndo('The table was refreshed. Undo is no longer available.');
      adopt(fresh); paintHead();
    } catch (failure) { if (epoch === metaEpoch && alive && (accessLost(failure) || sourceGone(failure))) invalidate(); /* otherwise the error text above stays */ }
  }
  // Revoked or removed access: drop every cached entry, cell, draft and editor, and leave only an honest unavailable state.
  function invalidate() {
    if (unavailable || !alive) return;
    unavailable = true; generation++; metaEpoch++; loading = false; saving = false; entries = []; cursor = null; clearTimeout(timer);
    clearStageUndo();
    drafts.delete(draftKey);
    // Nothing of the old Table (columns, stages, title) survives as the active reference.
    current = {id: current.id, columns: [], crm_config: null, archived: true, unavailable: true};
    host.replaceChildren();
    const box = crmNode('div', 'crm-unavailable-state');
    const text = crmNode('p', 'muted', 'This table is unavailable.'); text.setAttribute('role', 'status');
    const again = crmButton('Check access again', 'quiet', 'Check again');
    again.onclick = () => retry(again, text);
    box.append(text, again); host.append(box);
    try { onUnavailable?.(); } catch (_) { /* the host keeps its own state */ }
  }
  // The retry stays unavailable until fresh authorized Table metadata AND rows have both been read; only then are the
  // columns, configuration and outer controls restored, all from that fresh response.
  let retrying = false;
  async function retry(button, text) {
    if (retrying) return;
    retrying = true; button.disabled = true; text.textContent = 'Checking access…';
    const epoch = ++metaEpoch, mine = generation;
    try {
      const fresh = (await crmCall(biz, 'tables_get', {table_id: current.id})).table;
      if (!alive || epoch !== metaEpoch || mine !== generation || !isActive()) return;
      const first = await crmCall(biz, 'crm_entries_list', {table_id: fresh.id, limit: CRM_PAGE, include_archived: false, include_removed: false});
      if (!alive || epoch !== metaEpoch || mine !== generation || !isActive()) return;
      unavailable = false; current = fresh; entries = []; cursor = null; view.query = ''; view.order = ''; view.descending = false; view.archived = false; view.removed = false;
      build(); entries = first.items || []; cursor = first.next_cursor || null; paint();
      try { onAvailable?.(fresh); } catch (_) { /* the host keeps its own state */ }
    } catch (failure) {
      if (!alive || unavailable === false) return;
      text.textContent = accessLost(failure) || sourceGone(failure) ? 'This table is unavailable.' : uiFailure(failure, {load: true});
      button.disabled = false;
    } finally { retrying = false; }
  }
  function paint() {
    if (unavailable) return;
    paintHead(); tbody.replaceChildren(...entries.map(row));
    const text = view.query || view.archived || view.removed ? 'No matching records.' : 'No records in this table.';
    empty.hidden = entries.length > 0 || loading; empty.textContent = text;
    const count = $q('[data-crm-count]');
    count.hidden = !Number.isFinite(current.row_count);
    if (!count.hidden) count.textContent = `${current.row_count} records`;
    $q('[data-crm-more]').hidden = !cursor;
    syncDraft();
  }
  // keep > 0 re-reads pages until the entries already shown are covered again (a refresh keeps its pagination and filters).
  async function load(append = false, keep = 0) {
    if (unavailable || (loading && append)) return;
    const own = ++generation; loading = true;
    const stale = () => !alive || own !== generation || unavailable || !isActive();
    try {
      const args = after => ({table_id: current.id, limit: CRM_PAGE, include_archived: view.archived, include_removed: view.removed,
        ...(view.query ? {query: view.query} : {}), ...(view.order ? {order_by: view.order, descending: view.descending} : {}), ...(after ? {cursor: after} : {})});
      let after = append ? cursor : null, items = [], data;
      for (;;) {
        data = await crmCall(biz, 'crm_entries_list', args(after));
        if (stale()) return;
        items.push(...(data.items || [])); after = data.next_cursor || null;
        if (!keep || !after || items.length >= keep) break;
      }
      if (data.table_revision !== current.revision && !dirty()) { await reloadTable(); if (stale()) return; }
      if (stageUndo) {
        const undoEntry = items.find(item => item.id === stageUndo.entryId);
        if (undoEntry && (undoEntry.unavailable || undoEntry.removed || undoEntry.stage_id !== stageUndo.current))
          clearStageUndo('The stage changed elsewhere. Undo is no longer available.');
      }
      const seen = new Set(append ? entries.map(entry => entry.id) : []);
      const next = items.filter(entry => !seen.has(entry.id));
      entries = append ? [...entries, ...next] : next; cursor = after; error.hidden = true;
    } catch (failure) {
      if (!alive || own !== generation) return;
      // Lost access (first page or later page) drops everything cached; any other failure keeps what is shown and the drafts.
      if (accessLost(failure) || sourceGone(failure)) { loading = false; return invalidate(); }
      fail(failure, crmConflict(failure) ? 'Table changed while loading. Refresh to read the current version.' : uiFailure(failure, {load: true}));
    } finally { if (own === generation) loading = false; }
    if (alive && own === generation) paint();
  }
  const refresh = () => { clearStageUndo('The table was refreshed. Undo is no longer available.'); return load(false, entries.length); };
  const sortClick = event => {
    const sort = event.target.closest('[data-crm-sort]'); if (!sort || saving || dirty()) return;
    const nextOrder = sort.dataset.crmSort, restoreFocus = document.activeElement === sort;
    view.descending = view.order === nextOrder ? !view.descending : false;
    view.order = nextOrder; cursor = null;
    void load().then(() => {
      if (!restoreFocus || !alive || unavailable) return;
      [...host.querySelectorAll('[data-crm-sort]')].find(button => button.dataset.crmSort === nextOrder)?.focus();
    });
  };
  host.addEventListener('click', sortClick);
  async function save() {
    const pending = draft(); if (!pending || saving) return;
    for (const input of host.querySelectorAll('.data-cell-input')) if (!input.reportValidity()) return;
    saving = true; error.hidden = true; syncDraft();
    let revision = pending.baseRevision, failed = false;
    try {
      for (const [entryId, record] of [...pending.edits]) {
        const changes = {};
        if (record.cells.size) changes.cells = Object.fromEntries([...record.cells].map(([id, value]) => [id, dataParseCell(typeof value === 'boolean' ? value : value, current.columns.find(column => column.id === id))]));
        if (record.stage !== undefined) changes.stage_id = record.stage === '' ? null : record.stage;
        const args = {table_id: current.id, entry_id: entryId, expected_revision: revision, changes};
        const binding = JSON.stringify(args), previous = pending.attempts.get(entryId);
        const attempt = previous?.binding === binding ? previous : {binding, key: newRequestId()};
        pending.attempts.set(entryId, attempt);
        const result = await crmCall(biz, 'crm_entries_update', {...args, idempotency_key: attempt.key});
        revision = result.table.revision; pending.baseRevision = revision; pending.edits.delete(entryId); pending.attempts.delete(entryId);
        adopt(result.table);
      }
      drafts.delete(draftKey);
    } catch (failure) {
      // Every unsaved edit stays in the draft; a stale table offers a rebase instead of discarding anything.
      failed = true;
      if (accessLost(failure) || sourceGone(failure)) { saving = false; return invalidate(); }
      if (crmConflict(failure)) {
        fail(failure, 'Changed since you opened it. Your edits are kept. Reload the latest table to continue.');
        const reload = crmButton('Reload latest table', 'quiet', 'Reload latest'); reload.onclick = async () => { await reloadTable(); if (draft()) draft().baseRevision = current.revision; error.hidden = true; await load(); };
        error.append(' ', reload);
      } else fail(failure);
    } finally { saving = false; }
    if (alive) { if (failed) paint(); else await load(); }
  }
  // Wiring
  function build() {
    host.replaceChildren();
    host.classList.add('crm-table');
    host.innerHTML = `<div class="crm-header"><div class="crm-record-count" data-crm-count></div><h2 class="crm-title">${esc(current.title || '')}</h2>${current.description ? `<p class="crm-description">${esc(current.description)}</p>` : ''}</div>
    <div class="crm-tools data-row-tools"><label class="crm-search-label">Search records<input type="search" class="inp crm-search" data-crm-query placeholder="Search records" value=""></label>
      <label class="data-binary"><input type="checkbox" data-crm-archived>Archived records</label>
      <label class="data-binary"><input type="checkbox" data-crm-removed>Removed entries</label>
      <span class="data-spacer"></span>
      <div class="crm-draft-actions" data-crm-draft hidden><button type="button" class="btn" data-crm-discard>Discard</button><button type="button" class="btn primary" data-crm-save>Save</button></div>
      </div>
    <p class="data-error" data-crm-error role="alert" hidden></p>
    <div class="data-grid-wrap crm-grid-wrap"><table class="data-grid crm-grid"><thead></thead><tbody></tbody></table></div>
    <p class="data-empty muted" data-crm-empty role="status" hidden></p>
    <footer class="data-footer"><span data-crm-status role="status" aria-live="polite"></span><button type="button" class="btn crm-undo" data-crm-undo hidden>Undo</button><button type="button" class="btn" data-crm-more hidden>Load more</button></footer>`;
    error = $q('[data-crm-error]'); tbody = host.querySelector('tbody'); empty = $q('[data-crm-empty]');
    $q('[data-crm-query]').oninput = event => { view.query = event.target.value.trim(); clearTimeout(timer); timer = setTimeout(() => { if (alive) load(); }, 300); };
    $q('[data-crm-archived]').onchange = event => { view.archived = event.target.checked; load(); };
    $q('[data-crm-removed]').onchange = event => { view.removed = event.target.checked; load(); };
    $q('[data-crm-more]').onclick = () => load(true);
    $q('[data-crm-save]').onclick = save;
    $q('[data-crm-discard]').onclick = discard;
    paintHead();
  }
  function discard() { drafts.delete(draftKey); if (alive && !unavailable) void load(); }
  build(); void load();
  return {dirty, refresh, discard, save: async () => { await save(); return !dirty() && !unavailable; },
    dispose() { alive = false; generation++; clearTimeout(timer); host.removeEventListener('click', sortClick); host.replaceChildren(); }};
}

// Add: create a new record in this table's own scope (default Customer) or link existing records. Either way the
// membership is written through crm_entries_add with the table's current revision; a retry keeps its idempotency key.
function crmAddDialog(biz, table, onDone) {
  let editor = null, busy = false, chosen = [], revision = table.revision, repaint = null;
  const attempts = new Map();
  const stageOptions = (table.crm_config?.stages || []).filter(stage => !stage.archived).map(stage => `<option value="${esc(stage.id)}">${esc(stage.name)}</option>`).join('');
  const dialog = modal({title: 'Add entry', width: 640, body: `<form class="data-form crm-add" novalidate>
      <div class="focus-add-kind" role="radiogroup" aria-label="Entry source"><label><input type="radio" name="mode" value="create" checked> New record</label><label><input type="radio" name="mode" value="link"> Existing record</label></div>
      <div data-crm-create><label>Name<input class="inp" name="title" maxlength="1000" autocomplete="off" required></label>${taskTypeFields({task_type: 'customer'})}
        <label>Tags <span class="muted">(roles such as influencer, client, supplier)</span><input class="inp" name="tags" autocomplete="off"></label></div>
      <div data-crm-link hidden><button type="button" class="btn" data-crm-choose>Choose existing records</button><ul class="crm-chosen" aria-label="Chosen records"></ul></div>
      ${stageOptions ? `<label>Stage<select class="inp" name="stage"><option value="">No stage</option>${stageOptions}</select></label>` : ''}
      <p class="data-form-error" role="alert"></p></form>`,
    // While writes are pending no close route (Escape, backdrop, X, a replacing dialog) may dismiss it: the entered data and the
    // exact request keys stay in this dialog for the failure/retry path.
    beforeClose: () => !busy, isDirty: () => busy,
    actions: [{label: 'Cancel', onClick: close => close()}, {label: 'Add', primary: true, onClick: (close, el) => submit(close, el)}],
    onOpen: el => {
      const form = el.querySelector('form');
      editor = taskTypeMount(el, biz, {task_type: 'customer'});
      const chosenList = el.querySelector('.crm-chosen');
      const paintChosen = () => chosenList.replaceChildren(...chosen.map(item => {
        const li = document.createElement('li'); li.textContent = `${item.title} · ${TASK_TYPE_LABELS[item.task_type] || 'Task'} · ${item.scope_label} `;
        const drop = crmButton(`Remove ${item.title} from the chosen records`, 'quiet', 'Remove');
        drop.onclick = () => { chosen = chosen.filter(other => crmScopeKey(other.ref) !== crmScopeKey(item.ref)); paintChosen(); };
        li.append(drop); return li;
      }));
      repaint = paintChosen;
      for (const radio of form.querySelectorAll('[name=mode]')) radio.onchange = () => {
        const link = form.elements.mode.value === 'link';
        el.querySelector('[data-crm-create]').hidden = link; el.querySelector('[data-crm-link]').hidden = !link;
      };
      el.querySelector('[data-crm-choose]').onclick = () => crmChooseRecords({scopeId: table.business_id ?? null, selected: chosen, onChange: items => { chosen = items; paintChosen(); },
        onRevoke: lostScope => { chosen = chosen.filter(item => (item.ref.business_id ?? null) !== lostScope); paintChosen(); }});
      form.onsubmit = event => { event.preventDefault(); el.querySelector('.mfoot .primary').click(); };
    }});
  async function submit(close, el) {
    if (busy) return;
    const form = el.querySelector('form'), error = el.querySelector('.data-form-error'), button = el.querySelector('.mfoot .primary');
    error.textContent = '';
    const stage = form.elements.stage?.value || null;
    let jobs;
    try {
      if (form.elements.mode.value === 'link') {
        if (!chosen.length) throw new Error('Choose at least one record.');
        // Each record keeps the full scoped reference it was chosen with; the Table's scope is never written onto it.
        jobs = chosen.map(item => ({id: 'link:' + crmScopeKey(item.ref), args: {record: item.ref, ...(stage ? {stage_id: stage} : {})}}));
      } else {
        const title = form.elements.title.value.trim();
        if (!title) { form.elements.title.focus(); throw new Error('Enter a name.'); }
        const tags = [...new Set(form.elements.tags.value.split(',').map(tag => tag.trim()).filter(Boolean))];
        jobs = [{id: 'create', args: {create: {title, ...editor.values(), ...(tags.length ? {tags} : {})}, ...(stage ? {stage_id: stage} : {})}}];
      }
    } catch (failure) { error.textContent = failure.message; return; }
    // Every control is frozen while writes are pending, so a completing request cannot discard what is typed after it started.
    const controls = [...el.querySelectorAll('input,select,textarea,button')].filter(node => !node.disabled);
    busy = true; controls.forEach(node => { node.disabled = true; });
    let last = null;
    try {
      for (const job of [...jobs]) {
        const args = {table_id: table.id, expected_revision: revision, ...job.args};
        const binding = JSON.stringify(args), previous = attempts.get(job.id);
        const attempt = previous?.binding === binding ? previous : {binding, key: newRequestId()};
        attempts.set(job.id, attempt);
        last = await crmCall(biz, 'crm_entries_add', {...args, idempotency_key: attempt.key});
        revision = last.table.revision; attempts.delete(job.id); jobs.splice(jobs.indexOf(job), 1);
        chosen = chosen.filter(item => 'link:' + crmScopeKey(item.ref) !== job.id);
      }
      close(); await onDone?.(last);
    } catch (failure) {
      error.textContent = crmConflict(failure) ? 'The table changed. Close and reopen to add against the current version.' : uiFailure(failure);
      if (last?.table) onDone?.(last);
    } finally { busy = false; controls.forEach(node => { if (node.isConnected) node.disabled = false; }); repaint?.(); }
  }
  return dialog;
}
