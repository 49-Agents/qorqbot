// Mocked-API acceptance for retained surfaces touched by the clean-dashboard change: a pending Signoff from a historical email proposal (no Mail
// screen), general Tables create/update/row editing in the shared drawer, and drawer table links. Real dashboard assets against a
// synthetic API; no host, live services or mail. Run with PLAYWRIGHT_MODULE and NODE_PATH pointing at a Playwright install.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo = path.resolve(__dirname, '..');
const ALPHA = '5b0c1a52-3f0e-4d3a-9d44-7f6a2d8f1e11';
const PROPOSAL = {
  id: '30000000-0000-4000-8000-0000000000a1', review_business_id: ALPHA, proposal_revision: 1, payload_hash: 'a'.repeat(64), status_revision: 1, state: 'pending',
  operation_name: 'email_send_reply', operation_version: 1, proposer_principal_id: 'p1', proposer_display_name: 'Onbashy', proposer_agent_id: 'p1', initiated_by_principal_id: null,
  title: 'Send reply: Quote for bolts', affected_scope: {kind: 'business', business_ids: [ALPHA]}, created_at: '2026-09-07T12:00:00Z', updated_at: '2026-09-07T12:00:00Z', updated_sequence: 1,
  decision: null, supersedes_id: null, superseded_by_id: null, applied_at: null, apply_result: null, error: null, expected_targets: [], category: 'email',
  preview: {title: 'Send reply: Quote for bolts', rationale: 'Customer asked for a quote.', affected_scope: {kind: 'business', business_ids: [ALPHA]}, resources: [{resource_type: 'email', resource_id: 'c1', label: 'Quote for bolts'}], changes: [{kind: 'structured', resource: {resource_type: 'email', resource_id: 'c1', label: 'Quote for bolts / reply'}, fields: [{path: 'to', before: null, after: ['buyer@example.test']}, {path: 'subject', before: null, after: 'Re: Quote for bolts'}, {path: 'body', before: null, after: 'Hello, the quote for bolts is attached.'}]}], effects: ['Sends one email after approval.']},
};
const ok = data => ({request_id: 'r', status: 'ok', data, warnings: []});
const calls = [], errors = [];
fs.mkdirSync(path.join(__dirname, 'artifacts'), {recursive: true});
const table = {id: 't1', title: 'Widget orders', description: '', columns: [{id: 'name', name: 'Name', type: 'text'}, {id: 'qty', name: 'Qty', type: 'number'}],
  row_count: 1, revision: 3, archived: false, append_only: false, required_columns: [], readonly_columns: [], identity_columns: [], tags: {topics: ['orders']}, purpose: 'general'};
let tables = [table], rows = [{id: 'r1', position: 0, cells: {name: 'Bolts', qty: 4}}];
const ORDU = '00000000-0000-4000-8000-000000000001', WORKER = '00000000-0000-4000-8000-000000000003', LEARNER = '00000000-0000-4000-8000-000000000004';
const ecosystem = {kind: 'ecosystem', allowed_business_ids: [], global_capabilities: []};
const agents = [{agent_id: ORDU, name: 'chief', display_name: 'Ordubashy', role: {id: 'ordubashy'}, status: 'active', presentation: {}, scope: ecosystem},
  {agent_id: LEARNER, name: 'applier', display_name: 'Learning Applier', role: {id: 'learning-applier'}, status: 'active', presentation: {}, scope: ecosystem},
  {agent_id: WORKER, name: 'helper', display_name: 'Helper Worker', role: {id: 'global-worker'}, status: 'active', presentation: {}, scope: ecosystem}];

const operation = (name, body) => {
  switch (name) {
    case 'tables_list': return {items: tables.map(({id, title, row_count, archived}) => ({id, title, row_count, archived})), purposes: [{id: 'general', label: 'General'}], next_cursor: null};
    case 'tables_get': return {table: tables.find(item => item.id === body.table_id)};
    case 'tables_rows_list': return {items: rows, table_revision: tables.find(item => item.id === body.table_id).revision, next_cursor: null};
    case 'tables_create': {
      const created = {...table, id: 't2', title: body.changes ? body.changes.title : body.title, columns: body.columns, row_count: 0, revision: 1, tags: body.tags, purpose: body.purpose};
      tables = [...tables, created]; return {table: created};
    }
    case 'tables_update': {
      const current = tables.find(item => item.id === body.table_id);
      if (body.expected_revision !== current.revision) throw Object.assign(new Error('stale'), {status: 409});
      Object.assign(current, body.changes, {revision: current.revision + 1}); return {table: current};
    }
    case 'tables_rows_write': {
      const current = tables.find(item => item.id === body.table_id);
      for (const row of body.rows) {
        const existing = row.id && rows.find(item => item.id === row.id);
        if (existing) Object.assign(existing.cells, row.cells); else rows = [...rows, {id: 'r' + (rows.length + 1), position: rows.length, cells: row.cells}];
      }
      current.row_count = rows.length; current.revision++; return {table: current};
    }
    default: return {items: [], links: [], comments: [], subscriptions: []};
  }
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const json = (data, status = 200) => { res.statusCode = status; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data)); };
  const route = url.pathname;
  if (route === '/api/root/events') { res.writeHead(200, {'Content-Type': 'text/event-stream'}); res.write(': idle\n\n'); return; }
  if (route.startsWith('/api/')) {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = raw ? JSON.parse(raw) : {};
    const op = /\/operations\/([a-z_]+)$/.exec(route);
    // The host's workspace-type declaration is absent: the dashboard's built-in seed applies.
    if (op && op[1] === 'workspace_types') return json({status: 'error', error: {message: 'Built-in declaration'}}, 404);
    if (op) {
      calls.push({op: op[1], scope: route.startsWith('/api/root/') ? 'root' : decodeURIComponent(route.split('/')[3]), body});
      try { return json(ok(operation(op[1], body))); }
      catch (error) { return json({request_id: 'r', status: 'error', error: {code: 'revision_conflict', message: error.message, retryable: false, details: {}}}, error.status || 500); }
    }
    if (route === '/api/biz') return json([{id: ALPHA, business_id: ALPHA, slug: 'alpha', name: 'Alpha', agents: [], agent_meta: {}}]);
    if (route === '/api/session') return json(ok({principal_id: 'owner', principal_kind: 'human', display_name: 'Owner', is_owner: true}));
    if (route === '/api/root/signoffs') return json(ok({items: [PROPOSAL], counts: {pending: 1}, next_cursor: null, updated_cursor: 'x', has_more: false}));
    if (route === '/api/root/signoffs/' + PROPOSAL.id) return json(ok({signoff: PROPOSAL}));
    if (route === '/api/root/signoffs/live' || route.endsWith('/signoffs/live')) return json(ok({sessions: [], requests: [], routine_failures: []}));
    if (route === '/api/biz/alpha/signoffs') return json(ok({review_business_id: ALPHA, items: [], counts: {pending: 0}, next_cursor: null, updated_cursor: 'x', has_more: false}));
    if (route === '/api/root/extensions') return json(ok({extensions: []}));
    if (route === '/api/root/agents') return json(ok({agents}));
    if (route === '/api/me/agents/activity') return json(ok({agents: [], next_cursor: null}));
    if (route.endsWith('/inbox')) return json(ok({items: [], subscriptions: [], watcher_active: false}));
    if (route.endsWith('/browser-surfaces')) return json({agent_id: route.split('/').at(-2), configured: false, browsers: [], desktop: null});
    if (route.endsWith('/viewbox')) return json(ok({items: [], revision: 0}));
    if (route === '/api/root/terminals') return json([]);
    if (route === '/api/root/reply-notifications') return json(ok({items: []}));
    if (route === '/api/root/attention-queue') return json(ok({items: []}));
    if (route === '/api/me/attention') return json(ok({businesses: [], urgent: []}));
    if (route === '/api/me/attention/deliveries') return json(ok({items: []}));
    if (route.endsWith('/tasks') || route.endsWith('/log')) return json([]);
    if (route.endsWith('/live')) return json({sessions: [], requests: [], routine_failures: []});
    if (route.endsWith('/env')) return json(ok({pairs: []}));
    // Reads of panes this scenario does not exercise (agent chat, Focus layout, calendar status, the global Signoff list) are refused, not faked.
    if (/^\/api\/root\/(agents\/[0-9a-f-]+\/(chat|focus-layout)|calendar-statuses)$/.test(route)) return json({status: 'error', error: {message: 'Not part of this fixture'}}, 404);
    errors.push('Unexpected API ' + req.method + ' ' + route); return json({status: 'error', error: {message: 'No fixture'}}, 404);
  }
  const file = path.resolve(repo, '.' + (route === '/' ? '/index.html' : route));
  if (!file.startsWith(repo + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'); res.end(fs.readFileSync(file));
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.setDefaultTimeout(10000);
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
    const last = op => calls.filter(call => call.op === op).at(-1);
    await page.goto(origin + '/#alpha');
    await page.waitForFunction(() => typeof state !== 'undefined' && state.biz === 'alpha' && document.querySelector('[data-business-task-page]'));

    // 1. Visible path: Agents > supported agent > shared drawer > Signoffs (no Mail section). The retired
    // Project management and Paper Outreach entries add no pane tab; project data stays reachable through Charts.
    await page.goto(origin + '/#/global/agents');
    await page.locator('#global-agent-list [data-global-agent]').first().waitFor();
    const tabs = () => page.locator('#agpad .ttab').evaluateAll(list => list.map(tab => tab.dataset.v));
    const selectedTab = () => page.locator('#agpad .ttab[aria-selected=true]').getAttribute('data-v');
    // Coordinator opens on Focus; the learner on Context; a worker on inbox/status and not on Project management.
    await page.locator(`[data-global-agent="${ORDU}"]`).click();
    await page.waitForFunction(id => _agpad && _agpad.agentId === id, ORDU);
    assert.equal(await selectedTab(), 'focus');
    assert.equal((await tabs()).includes('business'), false, 'Ordubashy has no Project management entry');
    assert.equal((await tabs()).includes('campaign_review'), false, 'Ordubashy has no Paper Outreach entry');
    await page.locator(`[data-global-agent="${LEARNER}"]`).click();
    await page.waitForFunction(id => _agpad && _agpad.agentId === id, LEARNER);
    assert.equal(await selectedTab(), 'global_context');
    assert.equal((await tabs()).includes('business'), false, 'Learning Applier keeps Context only');
    await page.locator(`[data-global-agent="${WORKER}"]`).click();
    await page.waitForFunction(id => _agpad && _agpad.agentId === id, WORKER);
    assert.equal(['inbox', 'status'].includes(await selectedTab()), true, 'a worker starts on inbox/status');
    assert.equal((await tabs()).includes('business'), false, 'a worker has no Project management entry');
    assert.equal(await page.locator('[data-business-section=email]').count(), 0, 'there is no Mail section');
    await page.evaluate(() => { openTasks(true); tSetView('signoffs'); });
    await page.waitForFunction(() => tPanelOpen() && tView === 'signoffs');
    await page.locator('#taskspanel .ttab[data-v=signoffs][aria-selected=true]').waitFor();
    const row = page.locator('#taskspanel .sg-row', {hasText: 'Quote for bolts'});
    await row.waitFor();
    assert.equal(await row.locator('.business-label').first().textContent(), 'Alpha', 'the global queue labels the proposal with its project');
    await row.locator('.sg-rowhead').click();
    await page.locator('#taskspanel .sg-preview', {hasText: 'Sends one email after approval'}).waitFor();
    assert.match(await page.locator('#taskspanel').innerText(), /Alpha/, 'the review detail shows the Alpha identity');
    assert.match(await page.locator('#taskspanel').innerText(), /Sends one email after approval/);
    assert.equal(await page.locator('#taskspanel .sg-detail .sg-error').count(), 0, 'the detail loads completely');
    await page.locator('#taskspanel .sg-actionarea .sg-approve').waitFor();
    assert.equal(await page.locator('#taskspanel .sg-actionarea .sg-approve').isEnabled(), true, 'the owner approve control is enabled');
    assert.equal(await page.locator('#taskspanel').isVisible(), true, 'Signoffs are visible in the surrounding drawer, not an invisible embedded one');
    await page.screenshot({path: path.join(__dirname, 'artifacts', 'signoffs.png')});
    await page.evaluate(() => openTasks(false));
    await page.goto(origin + '/#alpha');
    await page.waitForFunction(() => typeof state !== 'undefined' && state.biz === 'alpha');

    // 2. General Tables in the shared drawer: create, update, row editing. Tags carry Topics only.
    // The Tables tab hosts the native screen in a same-origin frame.
    const drawer = () => page.frameLocator('#taskspanel iframe[data-workspace-host=drawer-tables]');
    await page.evaluate(() => ovOpen('tables', {scope: 'alpha'}));
    await drawer().locator('#data-items .data-item', {hasText: 'Widget orders'}).waitFor();
    await drawer().locator('#data-new:not([disabled])').waitFor();
    await drawer().locator('#data-new').click();
    await drawer().locator('#data-title').fill('Supplier list');
    assert.deepEqual(await drawer().locator('[data-tags]').evaluateAll(nodes => nodes.map(node => node.dataset.tags)), ['topics'], 'the tag editor is Topics only');
    await drawer().locator('[data-tags=topics]').fill('suppliers, parts');
    await drawer().locator('.mfoot .primary').click();
    await drawer().locator('#data-detail h1', {hasText: 'Supplier list'}).waitFor();
    const created = last('tables_create');
    assert.equal(created.scope, 'alpha');
    assert.equal(created.body.title, 'Supplier list');
    assert.equal(created.body.purpose, 'general');
    assert.deepEqual(created.body.tags, {topics: ['suppliers', 'parts']});
    assert.match(created.body.idempotency_key, /^[0-9a-f-]{36}$/);
    assert.equal(Object.hasOwn(created.body, 'department') || Object.hasOwn(created.body.tags, 'departments') || Object.hasOwn(created.body.tags, 'campaigns'), false, 'no retired taxonomy keys');

    await drawer().locator('#data-items .data-item', {hasText: 'Widget orders'}).click();
    await drawer().locator('#data-detail h1', {hasText: 'Widget orders'}).waitFor();
    await drawer().locator('#data-edit-table').click();
    await drawer().locator('#data-title').fill('Widget orders (open)');
    await drawer().locator('.mfoot .primary').click();
    await drawer().locator('#data-detail h1', {hasText: 'Widget orders (open)'}).waitFor();
    const updated = last('tables_update');
    assert.equal(updated.body.table_id, 't1'); assert.equal(updated.body.expected_revision, 3);
    assert.equal(updated.body.changes.title, 'Widget orders (open)');
    assert.deepEqual(updated.body.changes.tags, {topics: ['orders']});

    // Row editing: edit an existing cell and add a row, then save once with the current revision.
    await drawer().locator('[data-row="r1"] [data-column="qty"]').click();
    await drawer().locator('[data-row="r1"] [data-cell-input=qty]').fill('9');
    await drawer().locator('#data-row-count').click();
    await drawer().locator('#data-row-new').click();
    await drawer().locator('tbody tr:last-child [data-cell-input=name]').fill('Nuts');
    await drawer().locator('#data-row-count').click();
    await drawer().locator('#data-save').click();
    await drawer().locator('#data-draft-actions').waitFor({state: 'hidden'});
    const write = last('tables_rows_write');
    assert.equal(write.body.table_id, 't1'); assert.equal(write.body.expected_revision, 4);
    assert.deepEqual(write.body.rows.map(row => [row.id || null, row.cells]), [[null, {name: 'Nuts'}], ['r1', {qty: 9}]]);
    assert.deepEqual(write.body.delete_ids, []);
    assert.equal(rows.find(row => row.id === 'r1').cells.qty, 9, 'the mocked host applied the edit');
    await page.evaluate(() => openTasks(false));

    // 3. A link from outside the Tables view opens the shared drawer at the exact scope and record (a business page keeps its own in-place tables).
    await page.evaluate(() => tablesOpen(null, 't1'));
    await drawer().locator('#data-detail h1', {hasText: 'Widget orders (open)'}).waitFor();
    assert.equal(await page.evaluate(() => tPanelOpen()), true);
    const query = await page.evaluate(() => Object.fromEntries(new URLSearchParams(location.search)));
    assert.deepEqual([query.drawer, query.table_scope, query.table_id], ['tables', 'global', 't1']);
    assert.equal(last('tables_get').scope, 'root', 'the Global table is read from the root scope');
    assert.deepEqual(await page.locator('#taskspanel .ttab').evaluateAll(tabs => tabs.map(tab => tab.dataset.v)).then(v => v.filter(x => ['reports', 'finance', 'metrics', 'subscriptions'].includes(x))), [], 'no retired drawer tabs');
    assert.equal(await page.evaluate(() => typeof cmpreviewMount + typeof WORKSPACE_RENDERERS.get('campaign_review') + [...document.scripts].filter(x => /extensions|campaign-review/.test(x.src)).length), 'undefinedundefined0', 'the core-only app loads no Paper renderer or asset');
    assert.deepEqual(errors, []);
    console.log('Retained surfaces: retired pane entries, visible Signoffs path, no Mail section, general Tables create/update/row editing, drawer table link passed.');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
