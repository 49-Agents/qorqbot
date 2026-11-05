// Table editor against the retained backend contract (H/domains/tables/operations.py): no purposes/purpose/required_columns,
// strict arguments (unknown fields are rejected like the operations' additionalProperties:false), a revision bump on every
// write including row-only edits, and revision_conflict on a stale expected_revision. Synthetic mock API only; no live endpoint.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const businesses = [{id: 'biz-a', slug: 'widget', name: 'Widget', agents: []}];
const scope = {kind: 'ecosystem', allowed_business_ids: [], global_capabilities: []};
const KEY = /^[a-z][a-z0-9_]{0,63}$/;
const TABLE_KEYS = ['title', 'description', 'columns', 'tags', 'archived'];
const store = {widget: new Map(), root: new Map()}, rowsOf = new Map(), calls = [];
let serial = 0;
const fail = (code, message, details = {}) => ({error: {code, message, details}});
const view = (table, owner) => ({id: table.id, business_id: owner === 'root' ? null : 'biz-a', title: table.title, description: table.description,
  columns: table.columns, tags: table.tags, archived: table.archived, created_by: 'owner', revision: table.revision, created_at: 'then', updated_at: 'now',
  row_count: rowsOf.get(table.id).length, versioning: {state: 'pending', version_id: 'v' + table.revision, commit: null}});
const checkFields = (value, allowed, what) => {
  const extra = Object.keys(value).filter(key => !allowed.includes(key));
  if (extra.length) throw fail('validation_error', `${what}: unexpected ${extra.join(', ')}`);
};
const checkColumns = columns => {
  const ids = new Set(), names = new Set();
  if (!Array.isArray(columns) || !columns.length) throw fail('validation_error', 'columns required');
  for (const column of columns) {
    checkFields(column, ['id', 'name', 'type'], 'column');
    if (!KEY.test(column.id) || !['text', 'number', 'boolean', 'date', 'json'].includes(column.type)) throw fail('validation_error', 'bad column');
    const name = column.name.trim().toLowerCase();
    if (!name || ids.has(column.id) || names.has(name)) throw fail('validation_error', 'Column IDs and names must be non-empty and unique');
    ids.add(column.id); names.add(name);
  }
};
function operate(owner, op, args) {
  const tables = store[owner], {scope: _scope, idempotency_key, rationale, ...rest} = args;
  if (op === 'tables_list') {
    checkFields(rest, ['limit', 'cursor', 'query', 'include_archived', 'topic'], 'tables_list');
    return {items: [...tables.values()].filter(t => rest.include_archived || !t.archived).map(t => view(t, owner)), next_cursor: null};
  }
  if (op === 'tables_get') return {table: view(tables.get(rest.table_id), owner)};
  if (op === 'tables_rows_list') {
    checkFields(rest, ['limit', 'cursor', 'table_id', 'query', 'equals', 'order_by', 'descending'], 'tables_rows_list');
    const table = tables.get(rest.table_id);
    return {items: rowsOf.get(table.id).map(({id, position, cells}) => ({id, position, cells})), next_cursor: null, table_revision: table.revision};
  }
  if (op === 'tables_create') {
    checkFields(rest, [...TABLE_KEYS, 'task_ids'], 'tables_create');
    if (!rest.title?.trim()) throw fail('validation_error', 'Table title must not be blank');
    checkColumns(rest.columns);
    const table = {id: 'tbl-' + (++serial), title: rest.title.trim(), description: rest.description ?? '', columns: rest.columns, tags: rest.tags ?? {}, archived: rest.archived ?? false, revision: 1};
    tables.set(table.id, table); rowsOf.set(table.id, []);
    return {table: view(table, owner), rows: [], deleted_ids: [], job_id: 'job'};
  }
  const table = tables.get(rest.table_id);
  if (rest.expected_revision !== table.revision) throw fail('revision_conflict', 'Current resource revision is required', {current_revision: table.revision});
  if (op === 'tables_update') {
    checkFields(rest, ['table_id', 'expected_revision', 'changes'], 'tables_update'); checkFields(rest.changes, TABLE_KEYS, 'changes');
    if (rest.changes.columns) {
      checkColumns(rest.changes.columns);
      const kept = new Set(rest.changes.columns.map(c => c.id));
      for (const row of rowsOf.get(table.id)) for (const key of Object.keys(row.cells)) if (!kept.has(key)) delete row.cells[key];
    }
    Object.assign(table, rest.changes); table.revision++;
    return {table: view(table, owner), rows: [], deleted_ids: [], job_id: 'job'};
  }
  if (op === 'tables_rows_write') {
    checkFields(rest, ['table_id', 'expected_revision', 'rows', 'delete_ids'], 'tables_rows_write');
    const known = new Set(table.columns.map(c => c.id)), rows = rowsOf.get(table.id), written = [];
    for (const item of rest.rows || []) {
      checkFields(item, ['id', 'cells', 'source_upload_id'], 'row');
      if (Object.keys(item.cells).some(key => !known.has(key))) throw fail('validation_error', 'Cells name an unknown column');
      let row = item.id && rows.find(r => r.id === item.id);
      if (item.id && !row) throw fail('not_found', 'A row to update does not belong to this table; omit id to insert');
      if (!row) { row = {id: 'row-' + (++serial), position: rows.length, cells: {}}; rows.push(row); }
      Object.assign(row.cells, item.cells); written.push(row);
    }
    const deleted = rest.delete_ids || [];
    rowsOf.set(table.id, rows.filter(row => !deleted.includes(row.id)));
    table.revision++;
    return {table: view(table, owner), rows: written, deleted_ids: deleted, job_id: 'job'};
  }
  throw fail('not_found', op);
}
const columnsOf = [{id: 'name', name: 'Name', type: 'text'}, {id: 'qty', name: 'Qty', type: 'number'}];
const seed = (owner, title, columns, rows) => {
  const table = {id: 'tbl-' + (++serial), title, description: '', columns, tags: {}, archived: false, revision: 1};
  store[owner].set(table.id, table); rowsOf.set(table.id, rows.map((cells, position) => ({id: 'row-' + (++serial), position, cells})));
  return table.id;
};
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/api/')) {
    let body = '';
    for await (const chunk of req) body += chunk;
    const op = (url.pathname.match(/\/operations\/(\w+)$/) || [])[1], owner = url.pathname.startsWith('/api/biz/') ? 'widget' : 'root';
    let data = {items: [], messages: [], tags: [], agents: [], totals: {}, next_cursor: null}, raw = false, error = null;
    if (op && op.startsWith('tables_')) {
      const args = JSON.parse(body || '{}'); calls.push({owner, op, args});
      try { data = operate(owner, op, args); } catch (failure) { if (!failure.error) throw failure; error = failure.error; }
    }
    else if (url.pathname === '/api/biz') { data = businesses; raw = true; }
    else if (url.pathname === '/api/session') data = {principal_id: 'owner', principal_kind: 'human', is_owner: true, is_installation_owner: true};
    else if (url.pathname === '/api/root/agents') data = {agents: [{agent_id: 'ordu', name: 'ordu', display_name: 'Ordubashy', status: 'active', role: {id: 'ordubashy'}, scope}]};
    else if (url.pathname === '/api/root/agent-pins') data = {initialized: true, pinned_agents: []};
    else if (op === 'focus_get') data = {revision: 1, items: []};
    else if (op === 'calendar_calendars_list') data = {configured: false, calendars: [], errors: []};
    else if (url.pathname.endsWith('/browser-surfaces')) data = {surfaces: []};
    res.setHeader('Content-Type', 'application/json');
    if (error) { res.statusCode = 409; return res.end(JSON.stringify({status: 'error', error})); }
    return res.end(JSON.stringify(raw ? data : {status: 'ok', data}));
  }
  const file = path.resolve(root, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});
(async () => {
  const stock = seed('widget', 'Stock', columnsOf, [{name: 'Bolts', qty: 4}]);
  const emptyRows = seed('widget', 'No rows yet', columnsOf, []);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  try {
    const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage(), errors = [];
    page.setDefaultTimeout(15000);
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
    const since = () => calls.length;
    const last = (op, from = 0) => calls.slice(from).filter(call => call.op === op).at(-1);
    const save = async () => {
      await page.locator('.modal .mfoot .primary').click();
      await page.waitForFunction(() => !document.querySelector('.modal') || document.querySelector('.data-form-error')?.textContent);
    };
    const editor = {
      column: async (id, name, type) => {
        await page.locator('#data-column-add').click();
        const row = page.locator('.data-column').last();
        await row.locator('[data-field=id]').fill(id); await row.locator('[data-field=name]').fill(name); await row.locator('select[aria-label="Column type"]').selectOption(type);
      },
    };

    // ---- Project scope
    await page.goto(origin + '/');
    await page.waitForFunction(() => typeof selectBiz === 'function' && state.bizs?.some(b => b.slug === 'widget'));
    await page.evaluate(() => selectBiz('widget'));
    await page.waitForFunction(() => state.scope === 'business');
    await page.evaluate(() => goTab('tables'));
    await page.waitForFunction(() => document.querySelector('#data-new') && !document.querySelector('#data-new').disabled);
    await page.locator('.data-item').filter({hasText: 'Stock'}).waitFor();
    assert.equal(await page.locator('#data-purpose-filter, .data-filter').count(), 0, 'no Type filter');
    assert(!await page.locator('#data-new').isDisabled(), 'New table is enabled once the list loaded, with no purposes in the response');
    assert.deepEqual(last('tables_list').args, {limit: 50, include_archived: false}, 'list request carries no purpose');

    // Create from the empty editor: the single default column, no Type selector, no purpose field.
    let mark = since();
    await page.locator('#data-new').click();
    assert.equal(await page.locator('.modal #data-purpose').count(), 0);
    assert.equal(await page.locator('.modal .data-column').count(), 1);
    assert.equal(await page.locator('.modal .data-column button:disabled, .modal .data-column select:disabled').count(), 0, 'no locked template columns');
    await page.locator('#data-title').fill('Leads');
    await editor.column('stage', 'Stage', 'text');
    await save();
    await page.locator('.data-heading h1').filter({hasText: 'Leads'}).waitFor();
    assert.deepEqual(Object.keys(last('tables_create', mark).args).sort(), ['columns', 'description', 'idempotency_key', 'tags', 'title']);
    assert.deepEqual(last('tables_create', mark).args.columns.map(c => c.id), ['name', 'stage']);
    const leads = await page.evaluate(() => _data.selected);
    assert.equal(await page.locator('#data-empty-rows').isVisible(), true, 'a new table has no rows');

    // Edit an existing-column table: ids stay fixed, everything else is editable (no required_columns lock), update binds the revision.
    await page.locator('#data-edit-table').click();
    assert.equal(await page.locator('.modal .data-column [data-field=id][readonly]').count(), 2);
    assert.equal(await page.locator('.modal .data-column button:disabled, .modal .data-column select:disabled').count(), 0);
    await page.locator('.data-column').first().locator('[data-field=name]').fill('Lead');
    await page.locator('.data-column').first().locator('select[aria-label="Column type"]').selectOption('json');
    await editor.column('score', 'Score', 'number');
    mark = since();
    await save();
    assert.equal(await page.evaluate(() => document.querySelector('.data-form-error')?.textContent || ''), '');
    const update = last('tables_update', mark).args;
    assert.equal(update.expected_revision, 1);
    assert.deepEqual(update.changes.columns.map(c => c.id), ['name', 'stage', 'score']);
    assert.deepEqual(Object.keys(update.changes).sort(), ['archived', 'columns', 'description', 'tags', 'title']);
    await page.waitForFunction(() => _data.table?.revision === 2 && _data.table.columns.length === 3);

    // Row edit: draft stays local, Save sends the displayed revision, a row-only edit bumps the revision.
    await page.locator('#data-row-new').click();
    await page.locator('[data-column=stage] .data-cell').click();
    await page.locator('[data-cell-input=stage]').fill('new');
    await page.locator('[data-column=score] .data-cell').click();
    await page.locator('[data-cell-input=score]').fill('7');
    assert.equal(store.widget.get(leads).revision, 2, 'editing does not save');
    mark = since();
    await page.locator('#data-save').click();
    await page.waitForFunction(() => document.querySelector('#data-draft-actions')?.hidden);
    const write = last('tables_rows_write', mark).args;
    assert.equal(write.expected_revision, 2);
    assert.deepEqual(write.rows[0].cells, {stage: 'new', score: 7});
    assert.equal(store.widget.get(leads).revision, 3, 'a row-only edit increments the revision');
    await page.waitForFunction(() => _data.table.revision === 3 && _data.rows.length === 1);

    // Stale revision: another writer moves the table; the UI keeps the draft and shows the conflict.
    await page.locator('[data-column=stage] .data-cell').click();
    await page.locator('[data-cell-input=stage]').fill('mine');
    store.widget.get(leads).revision++;
    await page.locator('#data-save').click();
    await page.locator('#data-error').waitFor({state: 'visible'});
    assert.match(await page.locator('#data-error').textContent(), /revision/i);
    assert.equal(await page.locator('[data-cell-input=stage]').inputValue(), 'mine', 'draft retained after a conflict');
    await page.locator('#data-discard').click();
    await page.waitForFunction(() => _data.table.revision === 4);

    // Existing table with rows and an empty-rows table both open.
    await page.locator('.data-item').filter({hasText: 'No rows yet'}).click();
    await page.locator('#data-empty-rows').waitFor({state: 'visible'});
    await page.locator('.data-item').filter({hasText: 'Stock'}).click();
    await page.locator('.data-cell').filter({hasText: 'Bolts'}).waitFor();

    // ---- Global scope
    // Global tables are hosted by the drawer's embedded frame; the same document is opened directly here.
    await page.goto(origin + '/?agent-pane-embed=tables&business=global&section=tables');
    await page.waitForFunction(() => state.scope === 'global' && state.globalTab === 'tables' && document.querySelector('#data-new') && !document.querySelector('#data-new').disabled);
    assert.equal(await page.locator('.data-filter, [data-data-mode]').count(), 0);
    mark = since();
    await page.locator('#data-new').click();
    await page.locator('#data-title').fill('Global notes');
    await save();
    await page.locator('.data-heading h1').filter({hasText: 'Global notes'}).waitFor();
    const created = last('tables_create', mark);
    assert.equal(created.owner, 'root'); assert.equal(created.args.scope, 'global'); assert(!('purpose' in created.args));
    assert.equal([...store.root.values()].length, 1);
    assert.deepEqual(errors, []);
    console.log('PASS: tables_js_contract');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
