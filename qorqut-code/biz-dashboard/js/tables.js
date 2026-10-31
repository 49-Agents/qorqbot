// Shared business tables and original data uploads use the same operations as MCP.
let _data = {biz: null, mode: 'tables', selected: null, table: null, rows: [], cursor: null, query: '', order: '', descending: false, archived: false};
let _dataGeneration = 0, _dataDetail = 0;
const dataCall = async (biz, op, args = {}) => coreData(await post(`/biz/${encodeURIComponent(biz)}/operations/${op}`, args));
const dataActive = (biz, node) => state.scope === 'business' && state.biz === biz && state.tab === 'tables' && node?.isConnected;
const dataIconButton = (id, label, icon) => `<button class="data-icon" type="button" id="${id}" title="${esc(label)}" aria-label="${esc(label)}">${icon}</button>`;
const dataValue = value => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const dataCell = (row, key) => row && Object.hasOwn(row.cells, key) ? row.cells[key] : null;
const dataDownload = (biz, id) => `/api/biz/${encodeURIComponent(biz)}/data-uploads/${encodeURIComponent(id)}/download`;
document.querySelector('#tablesnav').innerHTML = aicon('database', 'currentColor', 20);

async function renderTables(container) {
  const biz = state.biz, generation = ++_dataGeneration;
  ++_dataDetail;
  if (_data.biz !== biz) _data = {biz, mode: 'tables', selected: null, table: null, rows: [], cursor: null, query: '', order: '', descending: false, archived: false};
  container.innerHTML = `<section class="data-workspace"><header class="data-top"><div class="ttabs"><button class="ttab ${_data.mode === 'tables' ? 'on' : ''}" data-data-mode="tables">Tables</button><button class="ttab ${_data.mode === 'uploads' ? 'on' : ''}" data-data-mode="uploads">Data uploads</button></div><span class="data-spacer"></span>${dataIconButton('data-new', _data.mode === 'tables' ? 'New table' : 'Upload data', _data.mode === 'tables' ? SVG_TPLUS : ICO_UPLOAD)}${dataIconButton('data-refresh', 'Refresh', aicon('refresh-cw', 'currentColor', 18))}</header><p class="data-error" id="data-error" role="status" hidden></p><div class="data-layout"><aside class="data-list"><label class="data-filter">Search<input class="inp" id="data-search" type="search"></label>${_data.mode === 'tables' ? `<label class="data-archived"><input type="checkbox" id="data-archived" ${_data.archived ? 'checked' : ''}>Archived</label>` : ''}<div id="data-items"></div><button class="btn" id="data-list-more" hidden>Load more</button></aside><section class="data-detail" id="data-detail"><p class="muted data-empty">${_data.mode === 'tables' ? 'No table selected' : 'No upload selected'}</p></section></div></section>`;
  container.querySelectorAll('[data-data-mode]').forEach(button => button.onclick = () => { _data.mode = button.dataset.dataMode; _data.selected = null; _data.table = null; renderTables(container); });
  container.querySelector('#data-new').onclick = () => _data.mode === 'tables' ? dataTableEditor() : dataUploadDialog();
  container.querySelector('#data-refresh').onclick = () => renderTables(container);
  const search = container.querySelector('#data-search');
  let timer, listCursor = null, listGeneration = 0;
  const load = async (append = false) => {
    const sequence = ++listGeneration, mode = _data.mode;
    try {
      const result = await dataCall(biz, mode === 'tables' ? 'tables_list' : 'data_uploads_list', {limit: 50, ...(search.value.trim() ? {query: search.value.trim()} : {}), ...(mode === 'tables' ? {include_archived: _data.archived} : {}), ...(append && listCursor ? {cursor: listCursor} : {})});
      if (!dataActive(biz, container) || generation !== _dataGeneration || sequence !== listGeneration) return;
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
    const result = await dataCall(biz, 'tables_get', {table_id: id});
    const found = await dataCall(biz, 'tables_rows_list', {table_id: id, limit: 50, ...(_data.query ? {query: _data.query} : {}), ...(_data.order ? {order_by: _data.order, descending: _data.descending} : {}), ...(append && _data.cursor ? {cursor: _data.cursor} : {})});
    if (!dataActive(biz, panel) || generation !== _dataDetail || _data.selected !== id) return;
    if (found.table_revision !== result.table.revision) throw new Error('Table changed while loading. Refresh to read the current version.');
    document.querySelector('#data-error').hidden = true;
    _data.table = result.table; _data.rows = append ? [..._data.rows, ...found.items] : found.items; _data.cursor = found.next_cursor;
    const keepSearchFocus = document.activeElement === panel.querySelector('#data-row-query');
    const table = result.table;
    panel.innerHTML = `<header class="data-heading">${dataIconButton('data-back', 'Back to tables', aicon('arrow-left', 'currentColor', 18))}<h1>${esc(table.title)}</h1><span class="data-spacer"></span>${dataIconButton('data-edit-table', 'Edit table and columns', aicon('wrench', 'currentColor', 18))}<button class="btn" id="data-history">History</button></header>${table.description ? `<p class="data-description">${esc(table.description)}</p>` : ''}<div class="data-tags">${Object.entries(table.tags).filter(([,v]) => v.length).map(([key, values]) => `<span>${esc(key)}: ${esc(values.join(', '))}</span>`).join('')}</div><div id="data-stories"></div><div class="data-row-tools"><label>Search rows<input type="search" id="data-row-query" class="inp" value="${esc(_data.query)}"></label><span id="data-save-status">${table.versioning.state === 'saved' ? 'Saved to Git' : 'Git snapshot pending'}</span><span class="data-spacer"></span>${dataIconButton('data-row-delete', 'Remove selected rows', SVG_X)}${dataIconButton('data-row-new', 'Add row', SVG_TPLUS)}</div><div class="data-grid-wrap"><table class="data-grid"><thead><tr><th class="data-check"><input type="checkbox" id="data-select-all" aria-label="Select visible rows"></th>${table.columns.map(column => `<th><button type="button" data-sort="${column.id}" ${column.type === 'json' ? 'disabled' : ''}>${esc(column.name)}${_data.order === column.id ? (_data.descending ? ' ↓' : ' ↑') : ''}</button></th>`).join('')}</tr></thead><tbody>${_data.rows.map(row => `<tr data-row="${row.id}"><td class="data-check"><input type="checkbox" data-row-select="${row.id}" aria-label="Select row ${row.position + 1}"></td>${table.columns.map(column => `<td><button type="button" class="data-cell" data-edit-row="${row.id}" title="${esc(dataValue(dataCell(row, column.id)).slice(0, 1000))}">${esc(dataValue(dataCell(row, column.id)).slice(0, 300)) || '<span class="muted">-</span>'}</button></td>`).join('')}</tr>`).join('')}</tbody></table>${_data.rows.length ? '' : '<p class="data-empty muted">No rows</p>'}</div><footer class="data-footer"><span>${_data.rows.length} of ${table.row_count} rows</span><button class="btn" id="data-more" ${_data.cursor ? '' : 'hidden'}>Load more</button></footer>`;
    panel.querySelector('#data-back').onclick = dataBack;
    panel.querySelector('#data-edit-table').onclick = () => dataTableEditor(table);
    panel.querySelector('#data-history').onclick = () => dataHistory(table);
    panel.querySelector('#data-row-new').disabled = table.archived;
    panel.querySelector('#data-row-new').onclick = () => dataRowEditor(table);
    panel.querySelectorAll('[data-edit-row]').forEach(button => { button.disabled = table.archived; button.onclick = () => dataRowEditor(table, _data.rows.find(row => row.id === button.dataset.editRow)); });
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
    const remove = panel.querySelector('#data-row-delete'), selected = () => [...panel.querySelectorAll('[data-row-select]:checked')].map(node => node.dataset.rowSelect);
    const sync = () => { remove.disabled = table.archived || !selected().length; };
    panel.querySelectorAll('[data-row-select]').forEach(node => node.onchange = sync);
    panel.querySelector('#data-select-all').onchange = event => { panel.querySelectorAll('[data-row-select]').forEach(node => { node.checked = event.target.checked; }); sync(); };
    sync();
    remove.onclick = () => { const ids = selected(); dataForm('Remove rows', `<p>Remove ${ids.length} selected rows from ${esc(table.title)}?</p>`, async (_el, submit) => submit('tables_rows_write', {table_id: id, delete_ids: ids, expected_revision: table.revision}), () => dataOpenTable(id), 'Remove'); };
    storyRelatedChips(panel.querySelector('#data-stories'), biz, 'data_tables', id);
    dataWatchHistory(biz, panel.querySelector('#data-save-status'), table, generation);
  } catch (error) { if (dataActive(biz, panel) && generation === _dataDetail) dataError(error); }
}

async function dataWatchHistory(biz, node, table, generation) {
  for (let n = 0; n < 20 && table.versioning.state !== 'saved'; n++) {
    await new Promise(resolve => setTimeout(resolve, 1500));
    if (!dataActive(biz, node) || generation !== _dataDetail) return;
    try {
      const current = (await dataCall(biz, 'tables_get', {table_id: table.id})).table;
      if (!dataActive(biz, node) || generation !== _dataDetail) return;
      if (current.revision !== table.revision) { node.textContent = 'Table changed. Refresh for updates.'; return; }
      if (current.versioning.state === 'saved') { node.textContent = 'Saved to Git'; return; }
    } catch (_) { return; }
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
    return submit(table ? 'tables_update' : 'tables_create', table ? {table_id: table.id, expected_revision: table.revision, changes} : changes);
  }, async result => { if (state.tab === 'tables') { _data.selected = result.table.id; _data.mode = 'tables'; await renderTables(document.querySelector('#content')); } });
  const host = editor.el.querySelector('#data-columns');
  const add = (column, existing = false) => {
    const row = document.createElement('div'); row.className = 'data-column';
    row.innerHTML = `<input class="inp" data-field="id" aria-label="Column ID" value="${esc(column.id)}" ${existing ? 'readonly' : ''}><input class="inp" data-field="name" aria-label="Column name" value="${esc(column.name)}"><select class="inp" aria-label="Column type">${['text','number','boolean','date','json'].map(kind => `<option ${kind === column.type ? 'selected' : ''}>${kind}</option>`).join('')}</select><button type="button" class="data-icon" title="Remove column" aria-label="Remove column">${SVG_X}</button>`;
    row.querySelector('button').onclick = () => row.remove(); host.appendChild(row);
  };
  columns.forEach(column => add(column, !!table));
  editor.el.querySelector('#data-column-add').onclick = () => { let n = 1; const ids = [...host.querySelectorAll('[data-field=id]')].map(node => node.value); while (ids.includes('column_' + n)) n++; add({id: 'column_' + n, name: 'Column ' + n, type: 'text'}); };
}

function dataRowEditor(table, row = null) {
  const editor = dataForm(row ? 'Edit row' : 'Add row', table.columns.map(column => `<label>${esc(column.name)}${column.type === 'boolean' ? `<span class="data-binary"><input type="checkbox" data-cell-input="${column.id}"><button type="button" class="data-icon" data-clear="${column.id}" title="Clear value" aria-label="Clear ${esc(column.name)}">${SVG_X}</button></span>` : column.type === 'json' || column.type === 'text' ? `<textarea class="inp" data-cell-input="${column.id}"></textarea>` : `<input class="inp" data-cell-input="${column.id}" type="${column.type === 'date' ? 'date' : 'number'}" step="any">`}</label>`).join(''), async (el, submit) => {
    const cells = Object.fromEntries(table.columns.map(column => {
      const input = el.querySelector(`[data-cell-input="${column.id}"]`), value = input.value;
      return [column.id, column.type === 'boolean' ? (input.indeterminate ? null : input.checked) : value === '' ? null : column.type === 'json' ? JSON.parse(value) : column.type === 'number' ? Number(value) : value];
    }));
    return submit('tables_rows_write', {table_id: table.id, expected_revision: table.revision, rows: [{...(row ? {id: row.id} : {}), cells}]});
  }, () => state.tab === 'tables' && dataOpenTable(table.id));
  for (const column of table.columns) {
    const value = dataCell(row, column.id), input = editor.el.querySelector(`[data-cell-input="${column.id}"]`);
    if (column.type === 'boolean') { input.indeterminate = value == null; input.checked = value === true; editor.el.querySelector(`[data-clear="${column.id}"]`).onclick = () => { input.indeterminate = true; }; }
    else input.value = value == null ? '' : column.type === 'json' ? JSON.stringify(value, null, 2) : String(value);
  }
}

async function dataHistory(table) {
  const biz = state.biz;
  const view = modal({title: 'Table history', body: '<div class="data-history"></div><button class="btn" id="data-history-more" hidden>Load more</button><p class="data-form-error"></p>'});
  const more = view.el.querySelector('#data-history-more');
  let cursor;
  const load = async () => {
    more.disabled = true;
    try {
      const history = await dataCall(biz, 'tables_history', {table_id: table.id, limit: 50, ...(cursor ? {cursor} : {})});
      if (!view.el.isConnected || state.biz !== biz) return;
      view.el.querySelector('.data-history').insertAdjacentHTML('beforeend', history.items.map(version => `<div><strong>Revision ${version.table_revision}</strong><span>${esc(new Date(version.created_at).toLocaleString())}</span><span>${version.git_commit ? 'Saved to Git' : 'Pending'}</span>${version.git_commit ? '' : `<button class="btn" data-retry-version="${version.id}">Retry</button>`}</div>`).join(''));
      cursor = history.next_cursor; more.hidden = !cursor;
      view.el.querySelectorAll('[data-retry-version]').forEach(button => button.onclick = async () => {
        button.disabled = true;
        try { await dataCall(biz, 'tables_history_retry', {version_id: button.dataset.retryVersion, idempotency_key: newRequestId()}); button.textContent = 'Queued'; }
        catch (error) { view.el.querySelector('.data-form-error').textContent = error.message; button.disabled = false; }
      });
    } catch (error) { view.el.querySelector('.data-form-error').textContent = error.message; more.hidden = false; }
    finally { more.disabled = false; }
  };
  more.onclick = load;
  await load();
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

async function tablesOpen(biz, id, mode = 'tables') {
  if (state.biz !== biz) return;
  await goTab('tables');
  if (state.biz !== biz || state.tab !== 'tables') return;
  _data.mode = mode; _data.selected = id;
  await renderTables(document.querySelector('#content'));
}
