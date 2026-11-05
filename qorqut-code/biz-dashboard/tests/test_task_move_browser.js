// Move to project owner UI against the real app shell, record workspace and task drawers. Operations are mocked by a
// local synthetic server; no live service, record, provider or model is touched.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const agent = '11111111-1111-4111-8111-111111111111';
const A = '0a000000-0000-4000-8000-00000000000a', B = '0b000000-0000-4000-8000-00000000000b', C = '0c000000-0000-4000-8000-00000000000c';
const businesses = [
  {id: A, business_id: A, slug: 'alpha', name: 'Alpha Studio', agents: ['dev'], owner: 'owner'},
  {id: B, business_id: B, slug: 'beta', name: 'Beta Lab', agents: ['dev'], owner: 'owner'},
  {id: C, business_id: C, slug: 'gamma', name: 'Gamma Works', agents: ['dev'], owner: 'owner'},
];
const profiles = [['ordu', 'Ordubashy', 'ordubashy']].map(([agent_id, display_name, role]) =>
  ({agent_id, name: agent_id, display_name, status: 'active', role: {id: role}, presentation: {department: 'operations'},
    scope: {kind: 'ecosystem', allowed_business_ids: [A, B], global_capabilities: []}}));

// Where every synthetic Task lives now. A read through any other scope answers like the real backend.
const tasks = {
  'g-story': {id: 'g-story', title: 'Funding story', task_type: 'story', status: 'open', revision: 4, overview: 'Funding overview.', business_id: null, business_slug: null,
    tags: [], assignee_ids: [], fields: {}, children: [{id: 'g-child', title: 'Pitch deck', status: 'open', revision: 1, task_type: 'task', business_id: null}], links: [], note_count: 0, created_by: 'owner'},
  'p-task': {id: 'p-task', title: 'Alpha ordinary task', task_type: 'task', status: 'working', revision: 2, business_id: A, business_slug: 'alpha',
    tags: [], assignee_ids: [], fields: {}, children: [], created_by: 'owner'},
  'p-cust': {id: 'p-cust', title: 'Alpha customer', task_type: 'customer', status: 'open', revision: 1, overview: 'A customer.', customer_kind: 'person', customer_profile: {},
    business_id: A, business_slug: 'alpha', tags: [], assignee_ids: [], fields: {}, children: []},
  'r-task': {id: 'r-task', title: 'Alpha revoked task', task_type: 'task', status: 'open', revision: 1, business_id: A, business_slug: 'alpha', tags: [], assignee_ids: [], fields: {}, children: []},
  'e-task': {id: 'e-task', title: 'Alpha moved elsewhere', task_type: 'task', status: 'open', revision: 1, business_id: A, business_slug: 'alpha', tags: [], assignee_ids: [], fields: {}, children: []},
};
const moved = {};                                  // id -> {business_id, slug, name} when relocated
const calls = {focus: [], targets: [], preview: [], move: [], taskGet: [], other: [], itemsList: [], candidates: []};
let behavior = {};                                  // per scenario hooks
const card = item => ({
  id: item.id, revision: 1, label: item.label, state: item.state, reason: '', archived: false, created_by: agent, unavailable: false, references: [],
  source: {type: 'task', id: item.id, business_id: item.business_id ?? null},
  resolved_source: {type: 'task', id: item.id, business_id: item.business_id ?? null, business_slug: item.business_slug ?? null, business_name: item.business_name ?? null,
    label: item.label, state: item.state, task_type: item.task_type, resource_type: 'tasks', revision: 1},
  conversation: {type: 'task', id: item.id, business_id: item.business_id ?? null}, source_revision: 1,
});
const board = {revision: 2, status: '', browser: null, activity: null, items: [
  card({id: 'g-story', label: 'Funding story', state: 'open', task_type: 'story', business_id: null, business_name: 'Global'}),
  card({id: 'p-task', label: 'Alpha ordinary task', state: 'working', task_type: 'task', business_id: A, business_slug: 'alpha', business_name: 'Alpha Studio'}),
  card({id: 'p-cust', label: 'Alpha customer', state: 'open', task_type: 'customer', business_id: A, business_slug: 'alpha', business_name: 'Alpha Studio'}),
  card({id: 'r-task', label: 'Alpha revoked task', state: 'open', task_type: 'task', business_id: A, business_slug: 'alpha', business_name: 'Alpha Studio'}),
  card({id: 'e-task', label: 'Alpha moved elsewhere', state: 'open', task_type: 'task', business_id: A, business_slug: 'alpha', business_name: 'Alpha Studio'}),
]};

const previewFor = (body, token = 'tok-1') => {
  const t = tasks[body.task_ids[0]], dest = businesses.find(b => b.id === body.destination_project_id);
  return {preview: {token, source: {business_id: body.source_business_id, name: body.source_business_id ? 'Alpha Studio' : 'Global'},
    destination: {id: dest.id, name: dest.name, slug: dest.slug},
    roots: [{id: t.id, title: t.title, previous_parent_id: t.id === 'p-task' ? 'old-parent' : null}],
    tasks: [{id: t.id, title: t.title, parent_id: null, task_type: t.task_type, archived: false},
      ...(t.id === 'g-story' ? [{id: 'g-child', title: 'Pitch deck', parent_id: 'g-story', task_type: 'task', archived: false},
        {id: 'g-old', title: 'Done old work', parent_id: 'g-story', task_type: 'task', archived: true}] : [])],
    counts: {tasks: t.id === 'g-story' ? 3 : 1, comments: 5, private_messages: 2, project_links: 1, focus_cards: 1, crm_entries: 0},
    visibility: {summary: 'Visible to members of ' + dest.name + '.'}, blockers: [], can_move: true}};
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  let raw = ''; for await (const chunk of req) raw += chunk;
  const body = raw ? JSON.parse(raw) : {};
  const reply = data => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({status: 'ok', data})); return true; };
  const fail = (code, message = 'Unavailable', status = code === 'forbidden' ? 403 : 400, details = {}) => {
    res.statusCode = status; res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({status: 'error', error: {code, message, details}})); return true;
  };
  if (!url.pathname.startsWith('/api/')) {
    const file = path.resolve(root, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
    if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.svg') ? 'image/svg+xml' : file.endsWith('.png') ? 'image/png' : file.endsWith('.woff2') ? 'font/woff2' : 'text/html');
    res.end(fs.readFileSync(file));
    return;
  }
  const op = (url.pathname.match(/\/operations\/([^/]+)$/) || [])[1];
  const scopeSlug = (url.pathname.match(/^\/api\/biz\/([^/]+)\/operations\//) || [])[1] || null;
  if (url.pathname === '/api/biz') { res.setHeader('Content-Type', 'application/json'); return res.end(JSON.stringify(businesses)); }
  if (url.pathname === '/api/session') return reply({principal_id: 'owner', principal_kind: 'human', is_owner: true, is_installation_owner: true});
  if (url.pathname === '/api/root/agents') return reply({agents: profiles});
  if (url.pathname === '/api/root/agent-pins') return reply({initialized: true, pinned_agents: []});
  if (/\/biz\/[^/]+\/agents/.test(url.pathname)) return reply([]);
  if (url.pathname.endsWith('/workspace_types')) return reply({types: [{key: 'focus', display_name: 'Focus', supported_roles: ['ordubashy'], renderer: 'focus_board'}]});
  if (url.pathname.endsWith('/calendar_calendars_list')) return reply({configured: false, calendars: [], errors: []});
  if (url.pathname.endsWith('/calendar_today')) return reply({calendar: null, events: []});
  if (url.pathname.endsWith('/focus_notes_list')) return reply({notes: [], revision: 1});
  if (url.pathname.endsWith('/reply-notifications')) return reply({items: []});
  if (url.pathname.endsWith('/browser-surfaces')) return reply({browsers: [], desktop: null});
  if (url.pathname.endsWith('/focus-layout')) return reply({revision: 1, blocks: [], card_ids: board.items.map(item => item.id)});
  if (op === 'focus_get') calls.focus.push(Date.now());
  if (op === 'focus_get') return reply({...board, items: board.items, next_offset: null});
  if (op === 'assignees_list') return reply({assignees: [{id: 'owner', kind: 'human', display_name: 'Owner', is_owner: true}]});
  if (op === 'tasks_list') return reply({tasks: (behavior.listRows || []).filter(id => behavior.denyGet !== id).map(id => ({...tasks[id], assignee_ids: [], created_by: 'owner'})), next_cursor: null});
  if (op === 'relationships_list' || op === 'owner_conversation_related') return reply({items: [], next_cursor: null});
  if (op === 'tasks_thread_get') return reply({notes: [], next_cursor: null});
  if (op === 'project_items_list') { calls.itemsList.push(body); return fail('internal_error', 'Paper inventory is broken', 500); }
  if (op === 'project_items_candidates') { calls.candidates.push(body); return fail('internal_error', 'Paper inventory is broken', 500); }
  if (op === 'tasks_get') {
    calls.taskGet.push({scope: scopeSlug, task_id: body.task_id});
    const getGate = behavior.getGate; behavior.getGate = null;
    if (getGate) await getGate.promise;
    if (behavior.denyGet === body.task_id) return fail('forbidden', 'Hidden source details', 403);
    const task = tasks[body.task_id], where = moved[body.task_id];
    if (!task) return fail('not_found', 'No task', 404);
    const slug = where ? where.slug : task.business_slug;
    if ((scopeSlug || null) !== (slug || null)) {
      if (where && behavior.hideMovedFrom !== scopeSlug) return fail('record_moved', 'Moved', 409, {destination: {id: where.business_id, name: where.name, slug: where.slug}, task: {type: 'task', id: task.id, business_id: where.business_id}});
      return fail('not_found', 'No task', 404);
    }
    return reply({task: {...task, ...(where ? {business_id: where.business_id, business_slug: where.slug} : {})}});
  }
  if (op === 'tasks_move_targets') {
    calls.targets.push({path: url.pathname, body});
    if (behavior.targets) return behavior.targets(body, {reply, fail});
    return reply({projects: businesses.filter(b => b.id !== body.source_business_id).map(b => ({id: b.id, name: b.name, slug: b.slug})), next_cursor: null});
  }
  if (op === 'tasks_move_to_project_preview') {
    calls.preview.push({path: url.pathname, body});
    if (behavior.preview) { const out = await behavior.preview(body, {reply, fail, previewFor}); if (out !== undefined) return out; }
    return reply(previewFor(body));
  }
  if (op === 'tasks_move_to_project') {
    calls.move.push({path: url.pathname, body});
    if (behavior.move) { const out = await behavior.move(body, {reply, fail, res}); if (out !== undefined) return out; }
    const dest = businesses.find(b => b.id === body.destination_project_id);
    for (const id of body.task_ids) moved[id] = {business_id: dest.id, slug: dest.slug, name: dest.name};
    return reply({move_id: 'move-1', destination: {id: dest.id, name: dest.name, slug: dest.slug},
      moved: body.task_ids.map(id => ({id, business_id: dest.id, parent_id: null, revision: 9})), counts: {tasks: 1, comments: 0, private_messages: 0, project_links: 0, focus_cards: 0, crm_entries: 0}});
  }
  if (op && op.startsWith('owner_conversation_')) {
    if (op === 'owner_conversation_get' || op === 'owner_conversation_read') {
      const id = body.source?.id, where = moved[id];
      if (where && (body.source.business_id ?? null) !== where.business_id) return fail('record_moved', 'Moved', 409, {destination: {id: where.business_id, name: where.name, slug: where.slug}, task: {type: 'task', id, business_id: where.business_id}});
    }
    return reply({conversation: {id: 'c', source: body.source, audience: 'owner_ordubashy', revision: 1, latest_sequence: 0, message_count: 0}, messages: [], next_before: null,
      participants: [], read_state: {last_read_sequence: 0, unread: false}});
  }
  calls.other.push(url.pathname);
  return reply({items: [], messages: [], purposes: [], tags: [], agents: [], totals: {}});
});

const until = async (check, what) => { for (let i = 0; i < 240; i++) { if (await check()) return; await new Promise(r => setTimeout(r, 25)); } throw new Error('Timed out waiting for ' + what); };
const gate = () => { let release; const promise = new Promise(r => { release = r; }); return {promise, release}; };

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  const out = process.env.TASK_MOVE_SHOTS || null;
  try {
    const page = await browser.newPage({viewport: {width: 1400, height: 900}});
    const errors = [];
    page.setDefaultTimeout(12000);
    page.on('pageerror', error => errors.push(error.message));
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
    await page.goto(origin + '/#/global/agents');
    await page.locator('.agfocus-items [data-card-id="g-story"]').waitFor();
    const pane = () => page.locator('.agfocus-dock .rw-record:not([hidden])');
    const dialog = () => page.locator('.modal', {hasText: 'Move to project'}).filter({has: page.locator('.tmove')});
    const openCard = async id => { await page.locator(`.agfocus-items [data-card-id="${id}"] .ttl`).click(); await pane().locator('.rw-head').waitFor(); };
    const chooseDestination = async name => dialog().locator('[data-move-destination]').selectOption({label: name});
    const reset = () => { behavior = {}; for (const key of Object.keys(calls)) calls[key].length = 0; };

    console.log(`STEP: Global Story from Focus: shared controller, null source, project-directory destinations, honest preview.`); // ---- Global Story from Focus: shared controller, null source, project-directory destinations, honest preview.
    await openCard('g-story');
    const moveButton = pane().locator('.rw-move');
    await moveButton.waitFor();
    assert.equal(await moveButton.isVisible(), true, 'a saved Story shows Move to project…');
    await moveButton.click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    assert.deepEqual(calls.targets.map(c => c.body), [{source_business_id: null}], 'Global is an explicit null source');
    assert.equal(calls.targets[0].path, '/api/root/operations/tasks_move_targets');
    assert.deepEqual(await dialog().locator('[data-move-destination] option').allTextContents(), ['Choose a project…', 'Alpha Studio', 'Beta Lab', 'Gamma Works']);
    assert.equal(calls.preview.length, 0, 'no destination is selected or previewed automatically');
    assert.equal(await dialog().locator('[data-move-confirm]').isDisabled(), true);
    assert.equal(calls.itemsList.length + calls.candidates.length, 0, 'destinations never come from project inventory/candidates');
    await chooseDestination('Beta Lab');
    await dialog().locator('.tmove-route').waitFor();
    assert.deepEqual(calls.preview.map(c => c.body), [{source_business_id: null, destination_project_id: B, task_ids: ['g-story']}]);
    const text = await dialog().locator('[data-move-preview]').innerText();
    assert.match(text, /From Global to Beta Lab/);
    assert.match(text, /3 tasks \(with descendants\)/);
    assert.match(text, /5 comments/);
    assert.match(text, /2 private messages/);
    assert.match(text, /top-level in the destination/);
    assert.match(text, /Done old work \(archived\)/);
    assert.match(text, /Ordinary task access follows the destination project’s rules/);
    assert.match(text, /Private owner–Ordubashy conversations stay private/);
    assert.match(text, /Visible to members of Beta Lab/);
    assert.equal(await dialog().locator('[data-move-confirm]').isDisabled(), false);
    if (out) await dialog().screenshot({path: path.join(out, 'preview.png')});
    // Cancel makes no mutation and leaves the record where it was.
    await dialog().locator('[data-move-cancel]').click();
    assert.equal(await dialog().count(), 0);
    assert.equal(calls.move.length, 0, 'cancel never mutates');
    assert.equal(await pane().locator('.rw-title').textContent(), 'Funding story');

    console.log(`STEP: Project Task control (Focus): the canonical project id is the source, not the sidebar.`); // ---- Project Task control (Focus): the canonical project id is the source, not the sidebar.
    reset();
    await openCard('p-task');
    await pane().locator('.rw-move').click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    assert.deepEqual(calls.targets.map(c => c.body), [{source_business_id: A}]);
    assert.deepEqual(await dialog().locator('[data-move-destination] option').allTextContents(), ['Choose a project…', 'Beta Lab', 'Gamma Works']);
    await chooseDestination('Gamma Works');
    await dialog().locator('.tmove-route').waitFor();
    assert.match(await dialog().locator('.tmove-detach').innerText(), /leaves its current parent/);
    // Changing the destination invalidates the old preview and a late reply for the old destination is ignored.
    const slow = gate();
    behavior.preview = async (body, h) => { if (body.destination_project_id === B) { await slow.promise; return h.reply(h.previewFor(body, 'tok-slow-B')); } };
    await chooseDestination('Beta Lab');
    await dialog().locator('[data-move-preview]', {hasText: 'Loading preview…'}).waitFor();
    assert.equal(await dialog().locator('[data-move-confirm]').isDisabled(), true, 'Move is disabled while loading');
    behavior.preview = null;
    await chooseDestination('Gamma Works');
    await dialog().locator('.tmove-route', {hasText: 'Gamma Works'}).waitFor();
    slow.release();
    await page.waitForTimeout(150);
    assert.match(await dialog().locator('.tmove-route').innerText(), /to Gamma Works/, 'the late Beta preview never replaces the current one');
    assert.equal(await dialog().locator('[data-move-confirm]').isDisabled(), false);
    await dialog().locator('[data-move-cancel]').click();

    console.log(`STEP: Blocked move: concrete blockers are listed and Move stays disabled.`); // ---- Blocked move: concrete blockers are listed and Move stays disabled.
    reset();
    behavior.preview = (body, h) => { const p = h.previewFor(body); p.preview.blockers = [{code: 'email_binding', message: 'A historical Email Task has a mailbox binding.', task_id: 'p-task'}]; p.preview.can_move = false; return h.reply(p); };
    await pane().locator('.rw-move').click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    await chooseDestination('Beta Lab');
    await dialog().locator('.tmove-blockers').waitFor();
    assert.match(await dialog().locator('.tmove-blockers').innerText(), /historical Email Task has a mailbox binding/);
    assert.equal(await dialog().locator('[data-move-confirm]').isDisabled(), true);
    await dialog().locator('[data-move-confirm]').click({force: true}).catch(() => {});
    assert.equal(calls.move.length, 0, 'a blocked preview can never be submitted');
    await dialog().locator('[data-move-cancel]').click();

    console.log(`STEP: Stale preview: the destination stays chosen, a fresh preview is required, then an explicit Move with a new key.`); // ---- Stale preview: the destination stays chosen, a fresh preview is required, then an explicit Move with a new key.
    reset();
    let n = 0;
    behavior.preview = (body, h) => h.reply(h.previewFor(body, 'tok-' + (++n)));
    behavior.move = (body, h) => { if (body.preview_token === 'tok-1') return h.fail('revision_conflict', 'Changed', 409); };
    await pane().locator('.rw-move').click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    await chooseDestination('Beta Lab');
    await dialog().locator('.tmove-route').waitFor();
    await dialog().locator('[data-move-confirm]').click();
    await until(() => calls.preview.length === 2, 'the fresh preview');
    await dialog().locator('[data-move-error]', {hasText: 'changed since the preview'}).waitFor();
    assert.equal(await dialog().locator('[data-move-destination]').inputValue(), B, 'the chosen destination is preserved');
    assert.equal(calls.move.length, 1, 'a stale preview is never retried automatically');
    await dialog().locator('[data-move-confirm]:not([disabled])').waitFor();
    await dialog().locator('[data-move-confirm]').click();
    await until(() => calls.move.length === 2, 'the explicit second Move');
    assert.equal(calls.move[1].body.preview_token, 'tok-2');
    assert.notEqual(calls.move[1].body.idempotency_key, calls.move[0].body.idempotency_key, 'a new preview gets a new key');
    const focusReadsBefore = calls.focus.length;
    await until(async () => (await dialog().count()) === 0, 'the dialog to close after success');
    await until(() => calls.focus.length > focusReadsBefore, 'the Focus refresh after success');
    // Success: same task id, returned destination, old-scope read never repeated for the new record.
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await until(() => calls.taskGet.some(c => c.scope === 'beta' && c.task_id === 'p-task'), 'the destination read');
    assert.deepEqual(await page.evaluate(() => _agpad.focusWorkspace.current().source), {type: 'task', id: 'p-task', business_id: B});
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current().biz), 'beta');
    assert.equal(await pane().locator('.rw-back').isHidden(), true, 'the old-scope entry is replaced, not left behind Back');
    await page.waitForFunction(() => document.querySelector('.agfocus-dock .rw-record:not([hidden])') && !document.querySelector('.agfocus-dock .rw-record:not([hidden]) .rw-status'));

    console.log(`STEP: Idempotent transport: double click sends one request; a lost reply retries the exact key/payload.`); // ---- Idempotent transport: double click sends one request; a lost reply retries the exact key/payload.
    reset();
    await openCard('p-cust');
    await pane().locator('.rw-move').click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Gamma Works'}).waitFor({state: 'attached'});
    await chooseDestination('Gamma Works');
    await dialog().locator('.tmove-route').waitFor();
    const hold = gate(); let lost = 1;
    behavior.move = async (body, h) => { await hold.promise; if (lost-- > 0) return h.fail('temporarily_unavailable', 'Reply lost', 503); };
    await dialog().locator('[data-move-confirm]').dblclick();
    await until(() => calls.move.length === 1, 'the pending request');
    assert.equal(await dialog().locator('[data-move-confirm]').isDisabled(), true, 'Move is disabled while pending');
    assert.equal(await dialog().locator('[data-move-destination]').isDisabled(), true);
    hold.release();
    await dialog().locator('[data-move-error]', {hasText: 'Couldn’t confirm the move'}).waitFor();
    assert.equal(calls.move.length, 1, 'the double click sent exactly one request');
    await dialog().locator('[data-move-confirm]:not([disabled])').click();
    await until(() => calls.move.length === 2, 'the transport retry');
    assert.deepEqual(calls.move[1].body, calls.move[0].body, 'the retry repeats the exact payload and idempotency key');
    await until(() => page.evaluate(() => _agpad.focusWorkspace.current()?.source.business_id) .then(id => id === C), 'navigation to the destination');
    assert.deepEqual(await page.evaluate(() => _agpad.focusWorkspace.current().source), {type: 'task', id: 'p-cust', business_id: C});

    console.log(`STEP: Lost access: no preview, no disclosure, nothing moves.`); // ---- Lost access: no preview, no disclosure, nothing moves.
    reset();
    behavior.preview = (body, h) => h.fail('forbidden', 'Hidden project details must not appear', 403);
    await openCard('e-task');
    await pane().locator('.rw-move').click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    await chooseDestination('Beta Lab');
    await dialog().locator('[data-move-error]', {hasText: 'no longer have access'}).waitFor();
    assert.doesNotMatch(await dialog().innerText(), /Hidden project details/);
    assert.equal(await dialog().locator('[data-move-confirm]').isDisabled(), true);
    assert.equal(calls.move.length, 0);
    assert.deepEqual(await dialog().locator('[data-move-destination] option').allTextContents(), ['No other active projects'], 'stale destination options are cleared');
    await dialog().locator('[data-move-cancel]').click();
    await until(() => calls.taskGet.some(c => c.task_id === 'e-task'), 'the source revalidation');
    assert.equal(await pane().locator('.rw-title').textContent(), 'Alpha moved elsewhere', 'a destination-only denial keeps the source');

    console.log(`STEP: Revoked source: the originating record is dropped, nothing is disclosed.`);
    reset();
    behavior.preview = (body, h) => h.fail('forbidden', 'Hidden project details must not appear', 403);
    await openCard('r-task');
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.record-panel, .tdetail, .rw-dashboard >> nth=0').first().waitFor();
    await pane().locator('.rw-move').click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    behavior.denyGet = 'r-task';
    await chooseDestination('Beta Lab');
    await until(async () => (await page.locator('.agfocus-dock .rw-record:not([hidden])').count()) === 0, 'the revoked source to be dropped');
    assert.equal(await dialog().count(), 0, 'the dialog goes with the dropped source');
    assert.doesNotMatch(await page.locator('body').innerText(), /Hidden (project|source) details/);

    console.log(`STEP: Close while a Move is in flight: no UI callback runs, no navigation, no page error.`); // ---- Close while a Move is in flight: no UI callback runs, no navigation, no page error.
    reset();
    await openCard('e-task');
    const late = gate();
    behavior.move = async (body, h) => { await late.promise; };
    await pane().locator('.rw-move').click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    await chooseDestination('Beta Lab');
    await dialog().locator('.tmove-route').waitFor();
    assert.equal(await dialog().locator('[data-move-cancel]').textContent(), 'Cancel');
    await dialog().locator('[data-move-confirm]').click();
    await until(() => calls.move.length === 1, 'the in-flight move');
    assert.equal(await dialog().locator('[data-move-cancel]').textContent(), 'Close', 'Cancel is relabelled while the move is in flight');
    assert.match(await dialog().innerText(), /does not cancel the move/);
    await dialog().locator('[data-move-cancel]').click();
    assert.equal(await dialog().count(), 0, 'dismissal stays available while pending');
    const titleBefore = await pane().locator('.rw-title').textContent();
    late.release();
    await page.waitForTimeout(300);
    assert.equal(await pane().locator('.rw-title').textContent(), titleBefore);
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current().source.business_id), A, 'a dismissed dialog never navigates');
    assert.deepEqual(errors, []);

    console.log(`STEP: record_moved: an old-scope record offers the authorized destination through the existing opening flow.`); // ---- record_moved: an old-scope record offers the authorized destination through the existing opening flow.
    reset();
    assert.equal(moved['e-task'].slug, 'beta');
    await page.evaluate(() => _agpad.focusWorkspace.close());
    await openCard('e-task');
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.rw-moved').waitFor();
    assert.match(await pane().locator('.rw-moved').innerText(), /moved to Beta Lab/);
    await pane().locator('.rw-moved button').click();
    await until(() => calls.taskGet.some(c => c.scope === 'beta' && c.task_id === 'e-task'), 'the destination open');
    assert.deepEqual(await page.evaluate(() => _agpad.focusWorkspace.current().source), {type: 'task', id: 'e-task', business_id: B});

    console.log(`STEP: Dirty edit guard: Save/Discard/Keep editing is the owner's choice, never implicit.`); // ---- Dirty edit guard: Save/Discard/Keep editing is the owner's choice, never implicit.
    reset();
    for (const key of Object.keys(moved)) delete moved[key];
    await page.evaluate(() => _agpad.focusWorkspace.close());
    await openCard('g-story');
    await pane().locator('[data-rw-tab="dashboard"]').click();
    // The Story overview is itself the edit activation now (no separate Edit button).
    await pane().locator('[data-story-read]').click();
    await pane().locator('[data-story-overview]').fill('Typed but unsaved');
    await pane().locator('.rw-move').click();
    await page.locator('.modal', {hasText: 'Unsaved changes'}).waitFor();
    assert.equal(calls.targets.length, 0, 'the move dialog waits behind the editor guard');
    await page.getByRole('button', {name: 'Keep editing'}).click();
    assert.equal(await dialog().count(), 0);
    assert.equal(await pane().locator('[data-story-overview]').inputValue(), 'Typed but unsaved', 'Keep editing preserves the edit');
    await pane().locator('.rw-move').click();
    // The Story editor has its own Discard button now; the guard's choice is the one inside the modal.
    await page.locator('.modal', {hasText: 'Unsaved changes'}).getByRole('button', {name: 'Discard'}).click();
    await dialog().waitFor();
    await dialog().locator('[data-move-cancel]').click();

    console.log(`STEP: The Task drawer and Global editor reuse the same controller with their own canonical scope.`); // ---- The Task drawer and Global editor reuse the same controller with their own canonical scope.
    reset();
    await page.evaluate(() => { void _agpad.focusWorkspace.close(); });
    if (await page.locator('.modal', {hasText: 'Unsaved changes'}).count()) await page.locator('.modal', {hasText: 'Unsaved changes'}).getByRole('button', {name: 'Discard'}).click();
    await page.evaluate(([A]) => { window.__t = {...tasks0(), business_id: A}; function tasks0() { return {id: 'p-task', title: 'Alpha ordinary task', task_type: 'task', status: 'working', revision: 2, tags: [], children: [], owners: []}; } }, [A]);
    await page.evaluate(() => { tDetail(window.__t); });
    const drawerMove = page.locator('.modal .mfoot button', {hasText: 'Move to project…'});
    await drawerMove.waitFor();
    await drawerMove.click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    assert.deepEqual(calls.targets.map(c => c.body), [{source_business_id: A}], 'the Task drawer uses the task’s own canonical project');
    await dialog().locator('[data-move-cancel]').click();
    await page.keyboard.press('Escape');
    await page.evaluate(() => { const host = document.createElement('div'); host.id = 'globalhost'; document.body.append(host); globalTasksMount(host, {initialTaskId: 'g-story'}); });
    await page.locator('#globalhost [data-task-move]').waitFor();
    await page.locator('#globalhost [data-task-move]').click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    assert.deepEqual(calls.targets.at(-1).body, {source_business_id: null});
    await dialog().locator('[data-move-cancel]').click();

    await page.evaluate(() => { document.querySelector('#globalhost')._globalTasks.dispose(); document.querySelector('#globalhost').remove(); });

    console.log(`STEP: Editor disposal while a preview or move is pending: dialogs close, callbacks stay silent, caches still invalidate.`);
    reset();
    // Global editor, pending Move.
    await page.evaluate(() => { const host = document.createElement('div'); host.id = 'globalhost2'; document.body.append(host); globalTasksMount(host, {initialTaskId: 'g-story'}); });
    await page.locator('#globalhost2 [data-task-move]').click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    await chooseDestination('Beta Lab');
    await dialog().locator('.tmove-route').waitFor();
    const holdGlobal = gate();
    behavior.move = async () => { await holdGlobal.promise; };
    await dialog().locator('[data-move-confirm]').click();
    await until(() => calls.move.length === 1, 'the Global move request');
    const bizBefore = await page.evaluate(() => state.biz);
    const focusBeforeDispose = calls.focus.length;
    await page.evaluate(() => { document.querySelector('#globalhost2')._globalTasks.dispose(); });
    assert.equal(await dialog().count(), 0, 'disposing the Global editor closes its dialog');
    holdGlobal.release();
    await until(() => calls.focus.length > focusBeforeDispose, 'cache invalidation after the confirmed late success');
    await page.waitForTimeout(200);
    assert.equal(await page.locator('.modal').count(), 0, 'no orphan modal after the late success');
    assert.equal(await page.evaluate(() => state.biz), bizBefore, 'a late success never navigates');
    await page.evaluate(() => document.querySelector('#globalhost2').remove());
    // Global editor, preview pending at disposal.
    reset();
    for (const key of Object.keys(moved)) delete moved[key];
    await page.evaluate(() => { const host = document.createElement('div'); host.id = 'globalhost3'; document.body.append(host); globalTasksMount(host, {initialTaskId: 'g-story'}); });
    await page.locator('#globalhost3 [data-task-move]').click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    const holdPreview = gate();
    behavior.preview = async (body, h) => { await holdPreview.promise; return h.reply(h.previewFor(body)); };
    await chooseDestination('Beta Lab');
    await until(() => calls.preview.length === 1, 'the pending preview');
    await page.evaluate(() => { document.querySelector('#globalhost3')._globalTasks.dispose(); });
    assert.equal(await dialog().count(), 0);
    holdPreview.release();
    await page.waitForTimeout(200);
    assert.equal(await page.locator('.modal').count(), 0);
    await page.evaluate(() => document.querySelector('#globalhost3').remove());
    // Task drawer, pending Move; the drawer closes underneath.
    reset();
    await page.evaluate(([A]) => { window.__t2 = {id: 'p-task', title: 'Alpha ordinary task', task_type: 'task', status: 'working', revision: 2, tags: [], children: [], owners: [], business_id: A}; window.__m2 = tDetail(window.__t2); }, [A]);
    await page.locator('.modal .mfoot button', {hasText: 'Move to project…'}).click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Gamma Works'}).waitFor({state: 'attached'});
    await chooseDestination('Gamma Works');
    await dialog().locator('.tmove-route').waitFor();
    const holdDrawer = gate();
    behavior.move = async () => { await holdDrawer.promise; };
    await dialog().locator('[data-move-confirm]').click();
    await until(() => calls.move.length === 1, 'the drawer move request');
    const drawerBiz = await page.evaluate(() => state.biz);
    await page.evaluate(() => window.__m2.close());
    assert.equal(await page.locator('.modal').count(), 0, 'closing the drawer closes its Move dialog too');
    holdDrawer.release();
    await page.waitForTimeout(250);
    assert.equal(await page.locator('.modal').count(), 0, 'a late drawer success opens nothing');
    assert.equal(await page.evaluate(() => state.biz), drawerBiz);

    console.log(`STEP: A delayed source revalidation never touches a newer editor; a lost source leaves the cached list.`);
    reset();
    behavior.listRows = ['g-story'];
    await page.evaluate(() => { const host = document.createElement('div'); host.id = 'globalhost4'; document.body.append(host); globalTasksMount(host, {}); });
    await page.locator('#globalhost4 [data-task="g-story"]').click();
    await page.locator('#globalhost4 [data-task-move]').click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    behavior.preview = (body, h) => h.fail('forbidden', 'Hidden project details must not appear', 403);
    const readGate = gate();
    behavior.getGate = readGate;
    await chooseDestination('Beta Lab');
    await dialog().locator('[data-move-error]', {hasText: 'no longer have access'}).waitFor();
    await dialog().locator('[data-move-cancel]').click();
    await page.locator('#globalhost4 [data-back]').click();
    await page.locator('#globalhost4 [data-new]').click();
    await page.locator('#globalhost4 input[name="title"]').fill('Unsaved new title');
    behavior.denyGet = 'g-story';
    readGate.release();
    await page.waitForTimeout(300);
    assert.equal(await page.locator('#globalhost4 input[name="title"]').inputValue(), 'Unsaved new title', 'a late source-loss result never discards a newer editor');
    await page.evaluate(() => { document.querySelector('#globalhost4')._globalTasks.dispose(); document.querySelector('#globalhost4').remove(); });
    // A source lost while its own dialog is current drops the exact row from the cached list.
    reset();
    behavior.listRows = ['g-story'];
    await page.evaluate(() => { const host = document.createElement('div'); host.id = 'globalhost5'; document.body.append(host); globalTasksMount(host, {}); });
    await page.locator('#globalhost5 [data-task="g-story"]').waitFor();
    await page.locator('#globalhost5 [data-task="g-story"]').click();
    await page.locator('#globalhost5 [data-task-move]').click();
    await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    behavior.preview = (body, h) => h.fail('forbidden', 'Hidden project details must not appear', 403);
    behavior.denyGet = 'g-story';
    await chooseDestination('Beta Lab');
    await page.locator('#globalhost5 [data-global-task-status]', {hasText: 'no longer available'}).waitFor();
    assert.equal(await page.locator('#globalhost5 [data-task="g-story"]').count(), 0, 'the unreadable record is gone from the cached rows');
    assert.doesNotMatch(await page.locator('#globalhost5').innerText(), /Funding story/);
    await page.evaluate(() => { document.querySelector('#globalhost5')._globalTasks.dispose(); document.querySelector('#globalhost5').remove(); });

    console.log(`STEP: Status cache invalidation is exact to the moved scoped identities.`);
    const snapshots = await page.evaluate(([A, B]) => {
      state.bizs = state.bizs || [];
      taskStateObserve('alpha', {id: 'same-id', revision: 1, status: 'open'});
      taskStateObserve('gamma', {id: 'same-id', revision: 1, status: 'open'});
      taskStateObserve('beta', {id: 'same-id', revision: 1, status: 'open'});
      // A pending local intent for the unrelated project must survive.
      taskStateSet('gamma', {id: 'same-id', revision: 1, status: 'open'}, 'done');
      const before = taskStateSnapshot('gamma', 'same-id');
      taskMoveInvalidate(['same-id'], A, {id: B, slug: 'beta', name: 'Beta Lab'});
      return {before: before.pending, gamma: taskStateSnapshot('gamma', 'same-id'), alpha: taskStateSnapshot('alpha', 'same-id').task, beta: taskStateSnapshot('beta', 'same-id').task};
    }, [A, B]);
    assert.equal(snapshots.before, 'done');
    assert.equal(snapshots.gamma.pending, 'done', 'pending intent for an unrelated same-id task survives');
    assert.equal(snapshots.gamma.task.status, 'done');
    assert.equal(snapshots.alpha, null, 'the source scope is forgotten');
    assert.equal(snapshots.beta, null, 'the destination scope is forgotten');
    const unrelated = await page.evaluate(() => { taskStateObserve('gamma', {id: 'keep', revision: 3, status: 'working'}); taskMoveInvalidate(['other'], '0a000000-0000-4000-8000-00000000000a', {slug: 'beta'}); return taskStateSnapshot('gamma', 'keep').task?.revision; });
    assert.equal(unrelated, 3);

    assert.deepEqual(errors, []);
    console.log('task move browser checks passed');
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
