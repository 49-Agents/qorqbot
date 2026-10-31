// Shared business tables and original data uploads use the same operations as MCP.
let _data = {biz: null, mode: 'tables', selected: null, table: null, rows: [], cursor: null, query: '', order: '', descending: false, archived: false, department: '', purpose: '', purposes: []};
let _dataGeneration = 0, _dataDetail = 0;
// Keep drafts when navigating away; only Save writes them to the service.
const _dataDrafts = new Map();
const dataDraftKey = () => JSON.stringify([state.biz, _data.selected]);
const dataDraft = () => _dataDrafts.get(dataDraftKey());
window.addEventListener('beforeunload', event => {
  if (_dataDrafts.size) { event.preventDefault(); event.returnValue = ''; }
});
const dataCall = async (biz, op, args = {}) => coreData(await post(`/biz/${encodeURIComponent(biz)}/operations/${op}`, args));
const dataActive = (biz, node) => state.scope === 'business' && state.biz === biz && state.tab === 'tables' && node?.isConnected;
const dataIconButton = (id, label, icon) => `<button class="data-icon" type="button" id="${id}" title="${esc(label)}" aria-label="${esc(label)}">${icon}</button>`;
const dataValue = value => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const dataCell = (row, key) => row && Object.hasOwn(row.cells, key) ? row.cells[key] : null;
const dataDownload = (biz, id) => `/api/biz/${encodeURIComponent(biz)}/data-uploads/${encodeURIComponent(id)}/download`;
const dataSettingsIcon = document.querySelector('.ritem[data-tab="settings"] svg').outerHTML;
document.querySelector('#tablesnav').innerHTML = aicon('database', 'currentColor', 20);

async function renderTables(container) {
  const biz = state.biz, generation = ++_dataGeneration;
  ++_dataDetail;
  if (_data.biz !== biz) _data = {biz, mode: 'tables', selected: null, table: null, rows: [], cursor: null, query: '', order: '', descending: false, archived: false, department: '', purpose: '', purposes: []};
  container.innerHTML = `<section class="data-workspace"><header class="data-top"><div class="ttabs"><button class="ttab ${_data.mode === 'tables' ? 'on' : ''}" data-data-mode="tables">Tables</button><button class="ttab ${_data.mode === 'uploads' ? 'on' : ''}" data-data-mode="uploads">Data uploads</button></div><span class="data-spacer"></span>${dataIconButton('data-new', _data.mode === 'tables' ? 'New table' : 'Upload data', _data.mode === 'tables' ? SVG_TPLUS : ICO_UPLOAD)}${dataIconButton('data-refresh', 'Refresh', aicon('refresh-cw', 'currentColor', 18))}</header><p class="data-error" id="data-error" role="status" hidden></p><div class="data-layout"><aside class="data-list"><label class="data-filter">Search<input class="inp" id="data-search" type="search"></label>${_data.mode === 'tables' ? `<label class="data-archived"><input type="checkbox" id="data-archived" ${_data.archived ? 'checked' : ''}>Archived</label>` : ''}<div id="data-items"></div><button class="btn" id="data-list-more" hidden>Load more</button></aside><section class="data-detail" id="data-detail"><p class="muted data-empty">${_data.mode === 'tables' ? 'No table selected' : 'No upload selected'}</p></section></div></section>`;
  container.querySelectorAll('[data-data-mode]').forEach(button => button.onclick = () => { _data.mode = button.dataset.dataMode; _data.selected = null; _data.table = null; renderTables(container); });
  container.querySelector('#data-new').onclick = () => _data.mode === 'tables' ? dataTableEditor() : dataUploadDialog();
  container.querySelector('#data-refresh').onclick = () => renderTables(container);
  const search = container.querySelector('#data-search');
  if (_data.mode === 'tables') {
    search.closest('label').insertAdjacentHTML('afterend', `<label class="data-filter">Type<select class="inp" id="data-purpose-filter"><option value="">All types</option></select></label><label class="data-filter">Department<input class="inp" id="data-department" value="${esc(_data.department)}"></label>`);
    container.querySelector('#data-new').disabled = true;
  }
  let timer, listCursor = null, listGeneration = 0;
  const load = async (append = false) => {
    const sequence = ++listGeneration, mode = _data.mode;
    try {
      const result = await dataCall(biz, mode === 'tables' ? 'tables_list' : 'data_uploads_list', {limit: 50, ...(search.value.trim() ? {query: search.value.trim()} : {}), ...(mode === 'tables' ? {include_archived: _data.archived, ...(_data.department ? {department: _data.department} : {}), ...(_data.purpose ? {purpose: _data.purpose} : {})} : {}), ...(append && listCursor ? {cursor: listCursor} : {})});
      if (!dataActive(biz, container) || generation !== _dataGeneration || sequence !== listGeneration) return;
      if (mode === 'tables') {
        _data.purposes = result.purposes;
        const filter = container.querySelector('#data-purpose-filter');
        filter.innerHTML = '<option value="">All types</option>' + _data.purposes.map(item => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join('');
        filter.value = _data.purpose;
        container.querySelector('#data-new').disabled = false;
      }
      const list = container.querySelector('#data-items');
      if (!append) list.innerHTML = '';
      for (const item of result.items) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'data-item'; button.dataset.id = item.id;
        button.innerHTML = `<strong>${esc(mode === 'tables' ? item.title : item.metadata.filename)}</strong><span>${esc(mode === 'tables' ? `${item.row_count} rows${item.archived ? ' · Archived' : ''}` : fmtBytes(item.metadata.size))}</span>`;
        button.onclick = () => mode === 'tables' ? dataOpenTable(item.id) : dataOpenUpload(item.id);
        list.appendChild(button);
      }
      if (!list.children.length) list.innerHTML = '<p class="data-empty muted">No results</p>';
      listCursor = result.next_cursor; container.querySelector('#data-list-more').hidden = !listCursor;
      if (_data.selected && !append) await (mode === 'tables' ? dataOpenTable(_data.selected) : dataOpenUpload(_data.selected));
    } catch (error) { if (dataActive(biz, container) && generation === _dataGeneration) dataError(error); }
  };
  search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => { if (dataActive(biz, search)) load(); }, 200); };
  const archived = container.querySelector('#data-archived'); if (archived) archived.onchange = () => { _data.archived = archived.checked; load(); };
  const purpose = container.querySelector('#data-purpose-filter');
  if (purpose) purpose.onchange = () => { _data.purpose = purpose.value; _data.selected = null; renderTables(container); };
  const department = container.querySelector('#data-department');
  if (department) department.onchange = () => { _data.department = department.value.trim(); _data.selected = null; renderTables(container); };
  container.querySelector('#data-list-more').onclick = () => load(true);
  await load();
}

function dataError(error) {
  const node = document.querySelector('#data-error'); if (node) { node.hidden = false; node.textContent = error.message || String(error); }
}
function dataSelect(id) {
  _data.selected = id;
  document.querySelectorAll('.data-item').forEach(node => node.classList.toggle('on', node.dataset.id === id));
  document.querySelector('.data-workspace')?.classList.add('has-selection');
}
function dataBack() { _data.selected = null; ++_dataDetail; document.querySelector('.data-workspace')?.classList.remove('has-selection'); }

async function dataOpenTable(id, append = false) {
  const biz = state.biz, panel = document.querySelector('#data-detail'), generation = ++_dataDetail;
  if (!panel) return;
  if (_data.selected !== id) {
    _data.query = ''; _data.order = ''; _data.descending = false;
    panel.innerHTML = '<p class="data-empty muted">Loading...</p>';
  }
  dataSelect(id);
  try {
    const draft = dataDraft();
    const result = draft ? {table: draft.table} : await dataCall(biz, 'tables_get', {table_id: id});
    const found = draft ? {items: draft.rows, table_revision: draft.table.revision, next_cursor: draft.cursor} : await dataCall(biz, 'tables_rows_list', {table_id: id, limit: 50, ...(_data.query ? {query: _data.query} : {}), ...(_data.order ? {order_by: _data.order, descending: _data.descending} : {}), ...(append && _data.cursor ? {cursor: _data.cursor} : {})});
    if (!dataActive(biz, panel) || generation !== _dataDetail || _data.selected !== id) return;
    if (found.table_revision !== result.table.revision) throw new Error('Table changed while loading. Refresh to read the current version.');
    document.querySelector('#data-error').hidden = true;
    _data.table = result.table; _data.rows = append && !draft ? [..._data.rows, ...found.items] : found.items; _data.cursor = found.next_cursor;
    const keepSearchFocus = document.activeElement === panel.querySelector('#data-row-query');
    const table = result.table;
    const listed = document.querySelector(`.data-item[data-id="${id}"]`);
    if (listed) {
      listed.querySelector('strong').textContent = table.title;
      listed.querySelector('span').textContent = `${table.row_count} rows${table.archived ? ' · Archived' : ''}`;
    }
    panel.innerHTML = `<header class="data-heading">${dataIconButton('data-back', 'Back to tables', aicon('arrow-left', 'currentColor', 18))}<h1>${esc(table.title)}</h1><span class="data-spacer"></span>${dataIconButton('data-edit-table', 'Edit table and columns', dataSettingsIcon)}</header>${table.description ? `<p class="data-description">${esc(table.description)}</p>` : ''}<div id="data-stories"></div><div class="data-row-tools"><label>Search rows<input type="search" id="data-row-query" class="inp" value="${esc(_data.query)}"></label><span class="data-spacer"></span><div id="data-draft-actions" hidden><button type="button" class="btn" id="data-discard">Discard</button><button type="button" class="btn primary" id="data-save">Save</button></div>${dataIconButton('data-row-delete', 'Remove selected rows', SVG_X)}${dataIconButton('data-row-new', 'Add row', SVG_TPLUS)}</div><div class="data-grid-wrap"><table class="data-grid"><thead><tr><th class="data-check"><input type="checkbox" id="data-select-all" aria-label="Select visible rows"></th>${table.columns.map(column => `<th><button type="button" data-sort="${column.id}" ${column.type === 'json' ? 'disabled' : ''}>${esc(column.name)}${_data.order === column.id ? (_data.descending ? ' ↓' : ' ↑') : ''}</button></th>`).join('')}</tr></thead><tbody></tbody></table></div><footer class="data-footer"><span id="data-row-count"></span><button class="btn" id="data-more" ${_data.cursor ? '' : 'hidden'}>Load more</button></footer>`;
    panel.querySelector('#data-back').onclick = dataBack;
    panel.querySelector('#data-edit-table').onclick = () => dataTableEditor(table);
    panel.querySelector('#data-row-new').disabled = table.archived;
    panel.querySelector('#data-row-new').onclick = () => {
      const draft = dataStartDraft(), row = {id: newRequestId(), cells: {}, position: table.row_count + draft.added.size};
      draft.added.set(row.id, row); dataRenderRows();
      panel.querySelector(`[data-row="${row.id}"] .data-cell`)?.click();
    };
    panel.querySelector('#data-save').onclick = dataSaveRows;
    panel.querySelector('#data-discard').onclick = () => { _dataDrafts.delete(dataDraftKey()); panel.innerHTML = '<p class="data-empty muted">Loading...</p>'; dataOpenTable(id); };
    panel.querySelector('#data-more').onclick = () => dataOpenTable(id, true);
    if (keepSearchFocus) panel.querySelector('#data-row-query').focus();
    let timer;
    const query = panel.querySelector('#data-row-query');
    query.oninput = () => {
      _data.query = query.value; ++_dataDetail;
      clearTimeout(timer);
      timer = setTimeout(() => { if (dataActive(biz, query) && _data.selected === id) dataOpenTable(id); }, 350);
    };
    panel.querySelectorAll('[data-sort]').forEach(button => button.onclick = () => { _data.descending = _data.order === button.dataset.sort && !_data.descending; _data.order = button.dataset.sort; dataOpenTable(id); });
    panel.querySelector('#data-row-delete').onclick = () => {
      const ids = [...panel.querySelectorAll('[data-row-select]:checked')].map(node => node.dataset.rowSelect), draft = dataStartDraft();
      for (const rowId of ids) { if (!draft.added.delete(rowId)) draft.deleted.add(rowId); draft.edits.delete(rowId); }
      dataRenderRows();
    };
    dataRenderRows();
    storyRelatedChips(panel.querySelector('#data-stories'), biz, 'data_tables', id);
  } catch (error) { if (dataActive(biz, panel) && generation === _dataDetail) dataError(error); }
}

function dataStartDraft() {
  let draft = dataDraft();
  if (!draft) {
    ++_dataDetail;
    draft = {table: _data.table, rows: _data.rows, cursor: _data.cursor, edits: new Map(), added: new Map(), deleted: new Set(), saving: false};
    _dataDrafts.set(dataDraftKey(), draft);
  }
  return draft;
}

function dataSyncDraft() {
  const panel = document.querySelector('#data-detail'), draft = dataDraft();
  const dirty = !!draft && !!(draft.edits.size || draft.added.size || draft.deleted.size);
  if (draft && !dirty) _dataDrafts.delete(dataDraftKey());
  panel.querySelector('#data-draft-actions').hidden = !dirty;
  panel.querySelectorAll('#data-save, #data-discard, #data-row-new, .data-cell, .data-cell-input').forEach(node => { node.disabled = !!draft?.saving || _data.table.archived; });
  panel.querySelectorAll('#data-row-query, #data-more, #data-edit-table').forEach(node => { node.disabled = dirty; });
  panel.querySelectorAll('[data-sort]').forEach(node => { node.disabled = dirty || _data.table.columns.find(column => column.id === node.dataset.sort).type === 'json'; });
  panel.querySelector('#data-row-delete').disabled = !!draft?.saving || _data.table.archived || !panel.querySelector('[data-row-select]:checked');
  panel.querySelector('#data-save').textContent = draft?.saving ? 'Saving...' : 'Save';
}

function dataRenderRows() {
  const panel = document.querySelector('#data-detail'), table = _data.table, draft = dataDraft();
  const rows = [..._data.rows, ...(draft?.added.values() || [])].filter(row => !draft?.deleted.has(row.id));
  const body = panel.querySelector('tbody');
  body.innerHTML = rows.map(row => `<tr data-row="${row.id}"><td class="data-check"><input type="checkbox" data-row-select="${row.id}" aria-label="Select row ${row.position + 1}"></td>${table.columns.map(column => `<td data-column="${column.id}"></td>`).join('')}</tr>`).join('');
  let empty = panel.querySelector('#data-empty-rows');
  if (!empty) { empty = document.createElement('p'); empty.id = 'data-empty-rows'; empty.className = 'data-empty muted'; empty.textContent = 'No rows'; body.closest('table').after(empty); }
  empty.hidden = rows.length > 0;
  for (const row of rows) for (const column of table.columns) {
    const cell = body.querySelector(`[data-row="${row.id}"] [data-column="${column.id}"]`);
    dataRenderCell(cell, row, column);
  }
  panel.querySelector('#data-row-count').textContent = `${rows.length} of ${table.row_count + (draft?.added.size || 0) - (draft?.deleted.size || 0)} rows`;
  body.querySelectorAll('[data-row-select]').forEach(node => { node.onchange = dataSyncDraft; });
  const all = panel.querySelector('#data-select-all'); all.checked = false;
  all.onchange = () => { body.querySelectorAll('[data-row-select]').forEach(node => { node.checked = all.checked; }); dataSyncDraft(); };
  dataSyncDraft();
}

function dataRenderCell(cell, row, column, editing = false) {
  if (editing) ++_dataDetail;
  const edits = dataDraft()?.edits.get(row.id), changed = edits?.has(column.id);
  const value = changed ? edits.get(column.id) : dataCell(row, column.id);
  cell.classList.toggle('data-modified', !!changed);
  if (column.type !== 'boolean' && (editing || changed)) {
    const input = document.createElement(column.type === 'text' || column.type === 'json' ? 'textarea' : 'input');
    input.className = 'data-cell-input'; input.dataset.cellInput = column.id;
    input.setAttribute('aria-label', column.name);
    if (input.tagName === 'INPUT') { input.type = column.type; if (column.type === 'number') input.step = 'any'; }
    const original = column.type === 'json' && dataCell(row, column.id) != null ? JSON.stringify(dataCell(row, column.id)) : dataValue(dataCell(row, column.id));
    input.value = changed ? dataValue(value) : original;
    input.oninput = () => {
      const draft = dataStartDraft();
      const values = draft.edits.get(row.id) || new Map();
      if (input.value === original && !input.validity.badInput) values.delete(column.id);
      else values.set(column.id, input.validity.badInput ? `Invalid ${column.type}` : input.value);
      if (values.size) draft.edits.set(row.id, values); else draft.edits.delete(row.id);
      input.setCustomValidity('');
      try { dataParseCell(input.value, column, input); } catch (error) { input.setCustomValidity(error.message); }
      cell.classList.toggle('data-modified', values.has(column.id)); dataSyncDraft();
    };
    input.onblur = () => { if (!dataDraft()?.edits.get(row.id)?.has(column.id)) dataRenderCell(cell, row, column); };
    input.onkeydown = event => {
      if (event.key === 'Escape') {
        const draft = dataDraft(), values = draft?.edits.get(row.id);
        values?.delete(column.id); if (values && !values.size) draft.edits.delete(row.id);
        dataRenderCell(cell, row, column); dataSyncDraft(); cell.querySelector('button').focus();
      } else if (event.key === 'Enter' && input.tagName === 'INPUT') input.blur();
    };
    cell.replaceChildren(input);
    if (editing) input.focus();
    return;
  }
  const button = document.createElement('button'); button.type = 'button'; button.className = 'data-cell';
  button.disabled = _data.table.archived || !!dataDraft()?.saving;
  button.title = `${column.name}: ${dataValue(value).slice(0, 1000)}`;
  if (column.type === 'boolean') {
    button.setAttribute('role', 'checkbox'); button.setAttribute('aria-checked', value == null ? 'mixed' : String(value));
    button.setAttribute('aria-label', column.name);
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.tabIndex = -1;
    checkbox.setAttribute('aria-hidden', 'true'); checkbox.checked = value === true; checkbox.indeterminate = value == null;
    button.appendChild(checkbox);
    button.onclick = () => {
      const draft = dataStartDraft(), values = draft.edits.get(row.id) || new Map(), next = value !== true;
      if (next === dataCell(row, column.id)) values.delete(column.id); else values.set(column.id, next);
      if (values.size) draft.edits.set(row.id, values); else draft.edits.delete(row.id);
      dataRenderCell(cell, row, column); dataSyncDraft(); cell.querySelector('button').focus();
    };
  } else {
    button.textContent = dataValue(value).slice(0, 300) || '-';
    button.onclick = () => dataRenderCell(cell, row, column, true);
  }
  cell.replaceChildren(button);
}

function dataParseCell(value, column, input) {
  if (input && !input.validity.valid) throw new Error(`Enter a valid ${column.type} for ${column.name}.`);
  if (value === '' || value == null) return null;
  if (column.type === 'number') {
    if (!Number.isFinite(Number(value))) throw new Error(`Enter a valid number for ${column.name}.`);
    return Number(value);
  }
  if (column.type === 'json') { try { return JSON.parse(value); } catch (_) { throw new Error(`Enter valid JSON for ${column.name}.`); } }
  if (column.type === 'date' && (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(Date.parse(value)) || new Date(value).toISOString().slice(0, 10) !== value)) throw new Error(`Enter a valid date for ${column.name}.`);
  return value;
}

async function dataSaveRows() {
  const biz = state.biz, id = _data.selected, key = dataDraftKey(), draft = dataDraft(), panel = document.querySelector('#data-detail');
  const visible = () => dataActive(biz, document.querySelector('#data-detail')) && _data.mode === 'tables' && _data.selected === id;
  if (!draft || draft.saving) return;
  try {
    document.querySelector('#data-error').hidden = true;
    for (const input of panel.querySelectorAll('.data-cell-input')) if (!input.reportValidity()) return;
    const ids = new Set([...draft.added.keys(), ...draft.edits.keys()]);
    const rows = [...ids].map(rowId => ({...(draft.added.has(rowId) ? {} : {id: rowId}), cells: Object.fromEntries([...(draft.edits.get(rowId) || [])].map(([columnId, value]) => [columnId, dataParseCell(value, draft.table.columns.find(column => column.id === columnId))]))}));
    const args = {table_id: id, expected_revision: draft.table.revision, rows, delete_ids: [...draft.deleted]};
    const binding = JSON.stringify(args);
    if (draft.attempt?.binding !== binding) draft.attempt = {binding, key: newRequestId()};
    draft.saving = true; dataSyncDraft();
    await dataCall(biz, 'tables_rows_write', {...args, idempotency_key: draft.attempt.key});
    _dataDrafts.delete(key);
    if (visible()) await dataOpenTable(id);
  } catch (error) {
    if (visible()) dataError(error);
  } finally {
    draft.saving = false;
    if (visible() && document.querySelector('#data-save')) dataSyncDraft();
  }
}

function dataForm(title, body, save, after, label = 'Save') {
  const biz = state.biz; let attempt;
  return modal({title: esc(title), body: `<div class="data-form">${body}<p class="data-form-error" role="alert"></p></div>`, width: 680,
    actions: [{label, primary: true, onClick: async (close, el) => {
      const button = el.querySelector('.mfoot .primary'); button.disabled = true;
      try {
        const result = await save(el, async (op, args) => {
          const binding = JSON.stringify([op, args]); if (attempt?.binding !== binding) attempt = {binding, key: newRequestId()};
          return dataCall(biz, op, {...args, idempotency_key: attempt.key});
        });
        close(); if (state.biz === biz && state.scope === 'business') await after?.(result);
      } catch (error) { el.querySelector('.data-form-error').textContent = error.message; }
      finally { if (button.isConnected) button.disabled = false; }
    }}]});
}

function dataTableEditor(table = null) {
  const columns = (table?.columns || [{id: 'name', name: 'Name', type: 'text'}]).map(column => ({...column}));
  const editor = dataForm(table ? 'Edit table' : 'New table', `<label>Title<input class="inp" id="data-title" maxlength="200" value="${esc(table?.title || '')}"></label><label>Description<textarea class="inp" id="data-description">${esc(table?.description || '')}</textarea></label><fieldset><legend>Columns</legend><div id="data-columns"></div><button type="button" class="btn" id="data-column-add">${SVG_TPLUS}Add column</button></fieldset>${['topics', 'campaigns', 'departments'].map(key => `<label>${key[0].toUpperCase() + key.slice(1)}<input class="inp" data-tags="${key}" value="${esc((table?.tags[key] || []).join(', '))}"></label>`).join('')}${table ? `<label class="data-binary"><input type="checkbox" id="data-table-archived" ${table.archived ? 'checked' : ''}>Archived</label>` : ''}`, async (el, submit) => {
    const changes = {title: el.querySelector('#data-title').value.trim(), description: el.querySelector('#data-description').value,
      columns: [...el.querySelectorAll('.data-column')].map(row => ({id: row.querySelector('[data-field=id]').value, name: row.querySelector('[data-field=name]').value, type: row.querySelector('select').value})),
      tags: Object.fromEntries([...el.querySelectorAll('[data-tags]')].map(node => [node.dataset.tags, [...new Set(node.value.split(',').map(value => value.trim()).filter(Boolean))]])),
      ...(table ? {archived: el.querySelector('#data-table-archived').checked} : {})};
    return submit(table ? 'tables_update' : 'tables_create', table ? {table_id: table.id, expected_revision: table.revision, changes} : {...changes, purpose: el.querySelector('#data-purpose').value});
  }, async result => { if (state.tab === 'tables') { _data.selected = result.table.id; _data.mode = 'tables'; await renderTables(document.querySelector('#content')); } });
  const host = editor.el.querySelector('#data-columns');
  const add = (column, existing = false, required = false) => {
    const row = document.createElement('div'); row.className = 'data-column';
    row.innerHTML = `<input class="inp" data-field="id" aria-label="Column ID" value="${esc(column.id)}" ${existing ? 'readonly' : ''}><input class="inp" data-field="name" aria-label="Column name" value="${esc(column.name)}"><select class="inp" aria-label="Column type">${['text','number','boolean','date','json'].map(kind => `<option ${kind === column.type ? 'selected' : ''}>${kind}</option>`).join('')}</select><button type="button" class="data-icon" title="Remove column" aria-label="Remove column">${SVG_X}</button>`;
    row.querySelector('button').onclick = () => row.remove(); host.appendChild(row);
    if (required) {
      row.querySelector('[data-field=id]').readOnly = true;
      row.querySelector('select').disabled = true;
      row.querySelector('button').disabled = true;
    }
  };
  columns.forEach(column => add(column, !!table, table?.required_columns.includes(column.id)));
  if (!table) {
    editor.el.querySelector('#data-title').closest('label').insertAdjacentHTML('beforebegin', `<label>Type<select class="inp" id="data-purpose">${_data.purposes.map(item => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join('')}</select></label>`);
    const purpose = editor.el.querySelector('#data-purpose');
    purpose.onchange = () => {
      const template = _data.purposes.find(item => item.id === purpose.value);
      host.replaceChildren();
      (template.columns || columns).forEach(column => add(column, false, !!template.columns));
      editor.el.querySelector('[data-tags=departments]').value = _data.department || (template.tags?.departments || []).join(', ');
    };
    purpose.value = _data.purpose || 'general';
    purpose.onchange();
  }
  editor.el.querySelector('#data-column-add').onclick = () => { let n = 1; const ids = [...host.querySelectorAll('[data-field=id]')].map(node => node.value); while (ids.includes('column_' + n)) n++; add({id: 'column_' + n, name: 'Column ' + n, type: 'text'}); };
}

async function dataOpenUpload(id) {
  const biz = state.biz, panel = document.querySelector('#data-detail'), generation = ++_dataDetail;
  if (!panel) return;
  if (_data.selected !== id) panel.innerHTML = '<p class="data-empty muted">Loading...</p>';
  dataSelect(id);
  try {
    const upload = (await dataCall(biz, 'data_uploads_get', {upload_id: id})).upload;
    if (!dataActive(biz, panel) || generation !== _dataDetail) return;
    panel.innerHTML = `<header class="data-heading">${dataIconButton('data-back', 'Back to uploads', aicon('arrow-left', 'currentColor', 18))}<h1>${esc(upload.metadata.filename)}</h1></header><p class="data-description">${esc(fmtBytes(upload.metadata.size))} · ${esc(new Date(upload.created_at).toLocaleString())}</p><p class="data-description">${esc(upload.metadata.notes || '')}</p><a class="data-download" href="${dataDownload(biz, id)}" download>Download original</a><div id="data-upload-stories"></div><div class="data-linked-tables">${upload.tables.map(table => `<button class="btn" data-linked-table="${table.id}">${esc(table.title)}</button>`).join('')}</div>`;
    panel.querySelector('#data-back').onclick = dataBack;
    storyRelatedChips(panel.querySelector('#data-upload-stories'), biz, 'uploads', id);
    panel.querySelectorAll('[data-linked-table]').forEach(button => button.onclick = () => tablesOpen(biz, button.dataset.linkedTable));
  } catch (error) { if (dataActive(biz, panel) && generation === _dataDetail) dataError(error); }
}

function dataUploadDialog() {
  let file;
  const view = dataForm('Upload data', `<label class="data-upload-drop" id="data-upload-drop"><span id="data-file-name">Choose or drop a data file</span><input type="file" id="data-file" accept=".csv,.tsv,.xlsx,.xls,.ods,.json,.jsonl,.txt"></label><label>Notes<textarea class="inp" id="data-upload-notes"></textarea></label>`, async (el, submit) => {
    if (!file) throw new Error('Choose a file.');
    if (file.size > 20 * 1024 * 1024 || !file.size) throw new Error('Choose a file between 1 byte and 20 MiB.');
    const content = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result.slice(reader.result.indexOf(',') + 1)); reader.onerror = () => reject(new Error('Could not read the file.')); reader.readAsDataURL(file); });
    return submit('data_uploads_upload', {filename: file.name, content_b64: content, notes: el.querySelector('#data-upload-notes').value});
  }, async result => { if (state.tab === 'tables') { _data.mode = 'uploads'; _data.selected = result.upload.id; await renderTables(document.querySelector('#content')); } else toast('Data uploaded.'); }, 'Upload');
  const choose = value => { file = value; view.el.querySelector('#data-file-name').textContent = file ? file.name : 'Choose or drop a data file'; };
  view.el.querySelector('#data-file').onchange = event => choose(event.target.files[0]);
  const drop = view.el.querySelector('#data-upload-drop'); drop.ondragover = event => event.preventDefault(); drop.ondrop = event => { event.preventDefault(); choose(event.dataTransfer.files[0]); };
}

async function tablesOpen(biz, id, mode = 'tables', filters = {}) {
  if (state.biz !== biz) return;
  await goTab('tables');
  if (state.biz !== biz || state.tab !== 'tables') return;
  _data.mode = mode; _data.selected = id; _data.department = filters.department || ''; _data.purpose = filters.purpose || '';
  await renderTables(document.querySelector('#content'));
}
