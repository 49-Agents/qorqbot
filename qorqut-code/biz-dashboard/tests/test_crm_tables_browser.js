// CRM on the existing Tables screen, Global and project, against the synthetic CRM Operations in tests/support. The real
// rendered Tables, Task editor, record workspace and modal components run in a browser; nothing is mocked in the UI.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {startCrmServer} = require('./support/crm_server');
const shots = process.env.CRM_SHOTS || null;
if (shots) fs.mkdirSync(shots, {recursive: true});

const server = startCrmServer(), api = server.api;
// A hostile title and stage name must stay text everywhere they are shown.
const HOSTILE = '<img src=x onerror="window.__pwned=1"> Rosa <b>Bold</b>';
api.addTask({id: 'cust-1', title: HOSTILE, task_type: 'customer', customer_kind: 'person', customer_state: 'Lead', tags: ['influencer'], customer_profile: {email: 'rosa@example.test', company: 'Alvarez Dental'}});
api.addTask({id: 'task-1', title: 'Plain follow-up task', task_type: 'task', status: 'open'});
api.addTask({id: 'story-1', title: 'Spring outreach story', task_type: 'story'});
api.addTask({id: 'mail-1', title: 'Historical pricing thread', task_type: 'email', email_conversation_id: 'conv-1'});
api.addTask({id: 'cust-1', business_id: '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', title: 'Project-scope twin', task_type: 'customer', customer_kind: 'company'});
api.addTask({id: 'gone-1', title: 'Secret hidden title', task_type: 'customer'});

const until = async (check, what) => { for (let i = 0; i < 240; i++) { if (await check()) return; await new Promise(r => setTimeout(r, 25)); } throw new Error('Timed out waiting for ' + what); };
const ops = (op, scope) => api.log.filter(entry => entry.op === op && (!scope || entry.scope === scope));

(async () => {
  const origin = await server.listen();
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  const external = [];
  try {
    const page = await browser.newPage({viewport: {width: 1400, height: 900}});
    const errors = [];
    page.setDefaultTimeout(12000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.route('**/*', route => { const url = new URL(route.request().url()); if (url.origin === origin) return route.continue(); external.push(url.href); return route.abort(); });
    await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
    await page.goto(origin + '/#/global/agents');
    await page.waitForFunction(() => typeof renderTables === 'function' && state.bizs?.length);
    const openGlobal = () => page.evaluate(() => { state.scope = 'global'; state.biz = null; state.globalTab = 'tables'; return renderTables(document.querySelector('#content')); });
    const openProject = () => page.evaluate(() => {
      document.querySelector('#content').replaceChildren();
      state.scope = 'business'; state.biz = 'alpha'; state.tab = 'context'; state.ctx = '_tables';
      let host = document.querySelector('#ctxembedded');
      if (!host) { host = document.createElement('div'); host.id = 'ctxembedded'; host.style.cssText = 'position:fixed;inset:0;z-index:5;background:var(--bg,#111);display:flex'; document.body.append(host); }
      return renderTables(host);
    });
    const save = async () => {
      await page.locator('.modal .mfoot .primary').click();
      await page.waitForFunction(() => !document.querySelector('.modal') || document.querySelector('.data-form-error')?.textContent);
      assert.equal(await page.evaluate(() => document.querySelector('.data-form-error')?.textContent || ''), '');
    };
    const newTable = async (title, kind) => {
      assert.equal(await page.locator('#data-new').isHidden(), true, 'table creation control is intentionally hidden');
      await page.evaluate(() => dataTableEditor());
      await page.locator('#data-title').fill(title);
      if (kind) await page.locator('#data-kind').selectOption(kind);
    };

    // ---- General tables are unchanged: no CRM controls, ordinary rows, rows write path.
    await openGlobal();
    await newTable('Ordinary inventory', 'general');
    assert.equal(await page.locator('#data-stages-box').isVisible(), false, 'General tables have no stage editor');
    assert.equal(await page.locator('[data-field=source]:visible').count(), 0, 'General columns have no record-field source');
    await save();
    await page.locator('.data-heading h1').filter({hasText: 'Ordinary inventory'}).waitFor();
    assert.equal(ops('tables_create', 'global')[0].args.table_kind, undefined);
    assert.equal(await page.locator('.crm-host').count(), 0, 'a general table never mounts the CRM view');
    assert.equal(await page.locator('#data-row-new').count(), 1, 'general rows keep the ordinary Add row control');
    assert.equal(ops('crm_entries_list').length, 0, 'general rows are never CRM entries');

    // ---- Create a Global CRM: defaults, stages, one local column.
    await newTable('Global relationships', 'crm');
    assert.equal(await page.locator('#data-stages-box').isVisible(), true);
    assert.deepEqual(await page.locator('.data-column [data-field=id]').evaluateAll(nodes => nodes.map(node => node.value)), ['record_name', 'roles', 'email'], 'CRM default columns');
    assert.deepEqual(await page.locator('.data-column [data-field=source]').evaluateAll(nodes => nodes.map(node => node.value)), ['title', 'tags', 'customer_profile.email']);
    await page.locator('#data-stage-add').click();
    await page.locator('.data-stage [data-field=name]').last().fill('Lead');
    await page.locator('#data-stage-add').click();
    await page.locator('.data-stage [data-field=name]').last().fill('<i>Active</i> client');
    await page.locator('#data-column-add').click();
    const local = page.locator('.data-column').last();
    await local.locator('[data-field=id]').fill('follow_up');
    await local.locator('[data-field=name]').fill('Follow-up');
    await page.locator('#data-column-add').click();
    const amount = page.locator('.data-column').last();
    await amount.locator('[data-field=id]').fill('budget');
    await amount.locator('[data-field=name]').fill('Budget');
    await amount.locator('select[aria-label="Column type"]').selectOption('number');
    await save();
    await page.locator('.crm-host').waitFor();
    const created = ops('tables_create', 'global').at(-1).args;
    assert.equal(created.table_kind, 'crm');
    assert.deepEqual(created.crm_config.stages, [{id: 'lead', name: 'Lead'}, {id: 'i_active_i_client', name: '<i>Active</i> client'}]);
    assert.deepEqual(created.columns.map(column => [column.id, column.record_field || null]), [['record_name', 'title'], ['roles', 'tags'], ['email', 'customer_profile.email'], ['follow_up', null], ['budget', null]]);
    assert.ok(created.idempotency_key);
    assert.equal(await page.locator('.crm-grid tbody tr').count(), 0, 'a new CRM starts with no memberships');
    assert.equal(await page.locator('[data-crm-add], [data-crm-pin], [data-crm-columns]').count(), 0, 'removed CRM controls stay absent');
    const globalCrm = await page.evaluate(() => _data.selected);

    // ---- The kind is fixed after creation.
    await page.locator('#data-edit-table').click();
    assert.equal(await page.locator('#data-kind').count(), 0, 'kind is not editable');
    await page.locator('.modal .mx').click();

    // ---- Seed membership through the synthetic API; the removed Add entry and pin controls are not needed.
    const synthetic = api.addTask({id: 'new-1', title: HOSTILE + ' (new)', task_type: 'customer', customer_kind: 'person', customer_state: 'Lead', tags: ['influencer', 'client'], customer_profile: {email: 'new@example.test'}});
    for (const [task, stage] of [[synthetic, 'lead'], [api.task(null, 'cust-1'), 'lead'], [api.task(null, 'task-1'), null], [api.task(null, 'story-1'), null], [api.task(null, 'mail-1'), null]])
      api.addEntry(globalCrm, {id: task.id, business_id: task.business_id}, {stage_id: stage});
    api.tables.get(globalCrm).revision++;
    await page.evaluate(() => _crmMount.refresh());
    await page.locator('.crm-grid tbody tr').filter({hasText: 'new@example.test'}).waitFor();
    assert.equal(ops('crm_entries_add', 'global').length, 0, 'fixture seeding bypasses removed membership controls');
    assert.equal(await page.evaluate(() => window.__pwned), undefined, 'hostile titles never become markup');
    assert.equal(await page.locator('.crm-grid img, .crm-grid b').count(), 0);
    assert.equal((await page.locator('.crm-record-cell .crm-open').first().textContent()), HOSTILE + ' (new)');

    // ---- Seeded Task subtypes remain distinct and unconverted.
    const row = title => page.locator('.crm-grid tbody tr').filter({has: page.locator('.crm-open', {hasText: title})});
    await until(() => page.locator('.crm-grid tbody tr').count().then(count => count === 5), '5 seeded entries');
    assert.deepEqual(await page.locator('.crm-task-state').allTextContents(), ['Customer · Open', 'Customer · Open', 'Open', 'Story · Open', 'Email · Open']);
    assert.equal(api.task(null, 'mail-1').task_type, 'email', 'no conversion');
    if (shots) await page.screenshot({path: path.join(shots, 'crm-global-table.png')});

    // ---- Local cells are drafts: Save writes exact revisions with a stable idempotency key; a conflict keeps them.
    const plain = row('Plain follow-up task');
    await plain.locator('[data-cell-input=follow_up]').fill('Call on Tuesday');
    await plain.locator('[data-cell-input=budget]').fill('120.5');
    assert.equal(ops('crm_entries_update').length, 0, 'editing never auto-saves');
    assert.equal(await page.locator('[data-crm-draft]').isVisible(), true);
    assert.equal(await page.locator('[data-crm-sort]').first().isDisabled(), true, 'sorting waits for the draft');
    api.flags.conflictNext = true;
    await page.locator('[data-crm-save]').click();
    await page.locator('[data-crm-error]').filter({hasText: 'Your edits are kept'}).waitFor();
    assert.equal(await plain.locator('[data-cell-input=follow_up]').inputValue(), 'Call on Tuesday', 'draft survives the conflict');
    const staleTry = ops('crm_entries_update').at(-1).args;
    await page.locator('[data-crm-save]').click();
    await until(() => ops('crm_entries_update').length >= 2, 'retry');
    const retry = ops('crm_entries_update').at(-1).args;
    assert.equal(retry.idempotency_key, staleTry.idempotency_key, 'an identical retry reuses its key');
    assert.equal(retry.expected_revision, staleTry.expected_revision);
    await until(async () => (await page.locator('[data-crm-draft]').isHidden()), 'draft saved');
    assert.deepEqual(retry.changes, {cells: {follow_up: 'Call on Tuesday', budget: 120.5}});
    const stored = api.entries.get(globalCrm).find(entry => entry.record_task_id === 'task-1');
    assert.deepEqual(stored.cells, {follow_up: 'Call on Tuesday', budget: 120.5});
    assert.equal(stored.stage_id, null);
    assert.equal(api.task(null, 'task-1').status, 'open', 'a CRM stage never changes Task state');
    assert.equal(api.task(null, 'task-1').revision, 1, 'a CRM edit never writes the Task');

    // Stage changes save immediately as CRM membership data, independent of local cell drafts.
    await plain.locator('.crm-stage-cycle').click();
    await until(() => api.entries.get(globalCrm).find(entry => entry.record_task_id === 'task-1').stage_id === 'lead', 'stage cycle write');
    assert.equal(api.task(null, 'task-1').revision, 1, 'stage cycling never writes the Task');

    // A genuine external change: Save keeps the draft, offers a rebase, and the rebased save uses the new revision.
    await plain.locator('[data-cell-input=follow_up]').fill('Call on Wednesday');
    api.tables.get(globalCrm).revision += 1;
    await page.locator('[data-crm-save]').click();
    await page.locator('[data-crm-error]').filter({hasText: 'Your edits are kept'}).waitFor();
    assert.equal(await plain.locator('[data-cell-input=follow_up]').inputValue(), 'Call on Wednesday');
    await page.locator('[data-crm-error]').getByRole('button', {name: 'Reload latest table'}).click();
    await until(() => page.locator('[data-crm-error]').isHidden(), 'rebase');
    assert.equal(await plain.locator('[data-cell-input=follow_up]').inputValue(), 'Call on Wednesday', 'rebase keeps the draft');
    await page.locator('[data-crm-save]').click();
    await until(async () => (await page.locator('[data-crm-draft]').isHidden()), 'rebased save');
    assert.equal(ops('crm_entries_update').at(-1).args.expected_revision, api.tables.get(globalCrm).revision - 1);
    assert.equal(api.entries.get(globalCrm).find(entry => entry.record_task_id === 'task-1').cells.follow_up, 'Call on Wednesday');

    // ---- Shared fields are read-only here and open the existing Task editor; nothing is copied into a cell.
    assert.equal(await row('Plain follow-up task').locator('.crm-shared input, .crm-shared textarea').count(), 0, 'projected columns have no input');
    await row('Historical pricing thread').locator('.crm-shared button').click();
    await page.locator('.crm-record-host .rw-record').waitFor();
    assert.equal(await page.locator('.crm-record-host .rw-title').textContent(), 'Historical pricing thread');
    assert.deepEqual(await page.locator('.crm-record-host .rw-tabs [role=tab]').evaluateAll(nodes => nodes.map(node => node.getAttribute('aria-label'))), ['Email dashboard', 'Chat']);
    if (shots) await page.screenshot({path: path.join(shots, 'crm-record-open.png')});
    await page.locator('.modal .mx').click();
    assert.equal(ops('crm_entries_update').every(entry => !('email' in (entry.args.changes.cells || {}))), true, 'record-sourced keys are never written to cells');

    // ---- A Task's own state is independent of the CRM: completing it keeps the entry and its stage.
    api.task(null, 'task-1').status = 'done'; api.task(null, 'task-1').revision++;
    await page.locator('[data-crm-query]').fill('Plain'); await page.locator('[data-crm-query]').fill('');
    await until(async () => (await plain.locator('.crm-task-state').textContent()) === 'Done', 'done task state');
    assert.equal(await plain.locator('.crm-stage-cycle').textContent(), 'Lead', 'stage is unchanged by the Task state');
    assert.equal(await page.locator('.crm-grid tbody tr').count(), 5, 'completed Tasks remain CRM members');

    // ---- An unavailable record exposes no cached title, cells, stage or scope.
    api.addEntry(globalCrm, {id: 'gone-1', business_id: null}, {stage_id: 'lead', cells: {follow_up: 'secret cell'}});
    api.tables.get(globalCrm).revision++;
    api.flags.hidden.add('global:gone-1');
    await page.locator('[data-crm-query]').fill('x'); await page.locator('[data-crm-query]').fill('');
    await page.locator('.crm-unavailable').waitFor();
    const unavailableText = await page.locator('.crm-unavailable').textContent();
    assert.match(unavailableText, /This record is unavailable/);
    for (const secret of ['Secret hidden title', 'secret cell', 'Lead']) assert.equal(unavailableText.includes(secret), false, `unavailable row leaks ${secret}`);
    assert.equal(await page.locator('.crm-unavailable [data-cell-input], .crm-unavailable select').count(), 0);
    assert.equal(await page.locator('.crm-table').evaluate(node => node.innerHTML.includes('Secret hidden title')), false, 'the title is nowhere in the CRM view (the separate Global task list is unrelated)');
    assert.equal(await page.locator('.crm-unavailable [data-crm-remove]').count(), 1, 'its membership can still be removed');

    // ---- Remove deletes only the membership; show removed + restore revives the same membership.
    const roseEntry = api.entries.get(globalCrm).find(entry => entry.record_task_id === 'story-1');
    await row('Spring outreach story').locator('[data-crm-remove]').click();
    await page.locator('.modal .mfoot .primary').click();
    await until(() => row('Spring outreach story').count().then(count => count === 0), 'removed row');
    assert.equal(ops('crm_entries_remove').at(-1).args.entry_id, roseEntry.id);
    assert.ok(api.task(null, 'story-1'), 'the record itself is untouched');
    assert.equal(api.task(null, 'story-1').archived, false);
    await page.locator('[data-crm-removed]').check();
    await row('Spring outreach story').locator('[data-crm-restore]').click();
    await until(() => ops('crm_entries_add').at(-1)?.args.record?.id === 'story-1' && ops('crm_entries_add').length === 1, 'restore');
    assert.equal(api.entries.get(globalCrm).filter(entry => entry.record_task_id === 'story-1').length, 1, 'restored membership keeps its identity');
    assert.equal(api.entries.get(globalCrm).find(entry => entry.record_task_id === 'story-1').id, roseEntry.id);
    await page.locator('[data-crm-removed]').uncheck();

    // ---- Pagination: more than one page of members is reachable.
    for (let n = 0; n < 130; n++) { const task = api.addTask({id: 'bulk-' + n, title: 'Bulk contact ' + n}); api.addEntry(globalCrm, task); }
    api.tables.get(globalCrm).revision++;
    await page.locator('[data-crm-query]').fill('Bulk');
    await until(() => page.locator('.crm-grid tbody tr').count().then(count => count === 50), 'first page');
    await page.locator('[data-crm-more]').click();
    await until(() => page.locator('.crm-grid tbody tr').count().then(count => count === 100), 'second page');
    await page.locator('[data-crm-more]').click();
    await until(() => page.locator('.crm-grid tbody tr').count().then(count => count === 130), 'third page');
    assert.equal(await page.locator('[data-crm-more]').isHidden(), true);
    await page.locator('[data-crm-query]').fill('');

    // ---- Archived tables freeze entry editing but leave records editable.
    await page.locator('#data-edit-table').click();
    await page.locator('#data-table-archived').check();
    await save();
    await page.locator('.crm-grid tbody tr').first().waitFor();
    assert.equal(await page.locator('[data-crm-add]').count(), 0, 'archived tables still expose no Add entry control');
    assert.equal(await page.locator('.crm-grid [data-cell-input]').first().isDisabled(), true);
    await page.locator('#data-edit-table').click();
    await page.locator('#data-table-archived').uncheck();
    await save();

    // ---- Revision conflict while loading shows a recoverable message; access loss leaves nothing cached.
    server.flags.failCrmList = true;
    await page.locator('[data-crm-query]').fill('y');
    await page.locator('[data-crm-error]').waitFor({state: 'visible'});
    server.flags.failCrmList = false;
    await page.locator('[data-crm-query]').fill('');
    await until(() => page.locator('[data-crm-error]').isHidden(), 'recovered');

    // ---- Project scope: the same operations in that project's route, and the same Task id in another scope stays distinct.
    await openProject();
    await newTable('Alpha relationships', 'crm');
    await save();
    await page.locator('.crm-host').waitFor();
    const alphaCrm = await page.evaluate(() => _data.selected);
    const alphaStory = api.addTask({id: 'alpha-story', business_id: '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', title: 'Alpha customer', task_type: 'story'});
    api.addEntry(alphaCrm, {id: alphaStory.id, business_id: alphaStory.business_id});
    api.addEntry(alphaCrm, {id: 'cust-1', business_id: '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa'});
    api.tables.get(alphaCrm).revision++;
    await page.evaluate(() => _crmMount.refresh());
    await until(() => page.locator('.crm-grid tbody tr').count().then(count => count === 2), 'project seeded memberships');
    assert.equal(ops('crm_entries_add', 'alpha').length, 0, 'project fixtures use the synthetic membership API');
    assert.ok(ops('crm_entries_list', 'alpha').length > 0, 'project CRM reads use the project operation route');
    assert.equal(api.task('11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'alpha-story').task_type, 'story');
    assert.equal(api.task('11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'alpha-story').business_id, '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'record stays in project scope');
    assert.equal(api.entries.get(alphaCrm).some(entry => entry.record_task_id === 'cust-1' && entry.record_business_id === '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa'), true);
    assert.equal(api.entries.get(globalCrm).some(entry => entry.record_task_id === 'cust-1' && entry.record_business_id === null), true);
    server.flags.denyScope = 'alpha';
    await page.evaluate(() => _crmMount.refresh());
    await page.locator('.crm-unavailable-state').waitFor();
    assert.equal(await page.locator('.crm-unavailable-state').textContent(), 'This table is unavailable.Check again', 'permission loss clears project CRM data');
    server.flags.denyScope = null;
    await page.locator('.crm-unavailable-state button').click();
    await page.locator('.crm-grid tbody tr').filter({hasText: 'Project-scope twin'}).waitFor();
    await page.locator('.crm-grid .crm-open', {hasText: 'Project-scope twin'}).click();
    await page.locator('.crm-record-host .rw-title').filter({hasText: 'Project-scope twin'}).waitFor();
    await page.locator('.modal .mx').click();
    if (shots) await page.screenshot({path: path.join(shots, 'crm-project-table.png')});

    assert.deepEqual(errors, []);
    assert.deepEqual(external.filter(url => !url.startsWith('https://fonts.googleapis.com/')), [], 'no external request (the shell font stylesheet is blocked)');
    console.log('CRM Tables: general compatibility, scoped seeded membership, access loss, revisions/idempotency/conflict drafts, read-only shared fields, unavailable and removal behavior passed');
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
