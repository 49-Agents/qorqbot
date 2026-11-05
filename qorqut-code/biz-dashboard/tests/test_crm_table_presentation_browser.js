// Approved CRM table presentation and interactions against synthetic Operations only.
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {startCrmServer} = require('./support/crm_server');

const server = startCrmServer(), api = server.api;
const table = api.addTable('global', {
  title: 'Partner records', description: 'Synthetic table description.', table_kind: 'crm',
  crm_config: {stages: [{id: 'lead', name: 'Lead'}, {id: 'active', name: 'Active'}, {id: 'retired', name: 'Retired', archived: true}]},
  columns: [
    {id: 'name', name: 'Name', type: 'text', record_field: 'title'},
    {id: 'email', name: 'Email', type: 'text', record_field: 'customer_profile.email'},
    {id: 'company', name: 'Company', type: 'text', record_field: 'customer_profile.company'},
    {id: 'next_step', name: 'Follow up', type: 'text'},
    {id: 'nextstep_label', name: 'Next step', type: 'text'},
    {id: 'score', name: 'Score', type: 'number'},
    {id: 'notes', name: 'Notes', type: 'text'},
  ],
});
for (const [id, title, email, company, score, stage, next] of [
  ['record-a', 'Ada Lovelace', 'ada@example.test', 'Analytical Engines', 30, null, 'Call'],
  ['record-b', 'Grace Hopper', 'grace@example.test', 'Compiler Labs', 10, 'retired', 'Email'],
  ['record-c', 'Katherine Johnson', 'kat@example.test', 'Orbital Math', 20, null, 'Meet'],
]) {
  const record = api.addTask({id, title, task_type: 'customer', customer_profile: {email, company}});
  api.addEntry(table.id, {id: record.id, business_id: null}, {stage_id: stage, cells: {next_step: next, nextstep_label: 'hidden', score, notes: 'visible'}});
}

(async () => {
  const origin = await server.listen();
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1400, height: 900}});
    page.setDefaultTimeout(12000);
    page.on('pageerror', error => { throw error; });
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
    await page.goto(origin + '/#/global/agents');
    await page.waitForFunction(() => typeof renderTables === 'function' && state.bizs?.length);
    await page.evaluate(async () => {
      state.scope = 'global'; state.biz = null; state.globalTab = 'tables';
      await renderTables(document.querySelector('#content'));
    });
    await page.locator('.data-item').filter({hasText: 'Partner records'}).click();
    await page.locator('.crm-grid tbody tr').first().waitFor();

    assert.equal(await page.locator('.crm-record-count').textContent(), '3 records');
    assert.equal(await page.locator('.crm-title').textContent(), 'Partner records');
    assert.equal(await page.locator('.crm-description').textContent(), 'Synthetic table description.');
    assert.equal(await page.locator('.crm-grid th').allTextContents().then(values => values.includes('Name')), false, 'the identity title is not repeated as a projected column');
    assert.equal(await page.locator('.crm-grid th').allTextContents().then(values => values.includes('Email')), true, 'configured email column is preserved');
    assert.equal(await page.locator('.crm-grid th').allTextContents().then(values => values.includes('Follow up') || values.includes('Next step')), false, 'conventional next step columns are hidden only from presentation');
    assert.equal(await page.locator('.crm-grid th').allTextContents().then(values => values.includes('Score')), true, 'actual configured local columns remain visible');
    assert.equal(await page.locator('#crm-pin-table, [data-crm-add], [data-crm-filter-stage]').count(), 0, 'CRM pin, Add entry and stage filtering controls are absent');
    assert.equal(await page.locator('#data-new').isHidden(), true, 'table creation is not a user-facing Tables entry point');
    assert.equal(await page.locator('.crm-record-meta').count(), 0, 'identity is name only');
    assert.equal(await page.locator('.crm-record-cell').first().innerText(), 'Ada Lovelace');
    assert.equal(await page.locator('.crm-grid th').allTextContents().then(values=>values.includes('Company')),true,'configured company column is preserved');
    assert.equal(await page.locator('.crm-team').count(),0);
    assert.equal(api.log.filter(call=>call.op==='tables_thread_get').length,0,'no shared discussion read in CRM');

    for (let index = 0; index < 55; index++) {
      const record = api.addTask({id: 'bulk-' + index, title: 'Bulk record ' + index});
      api.addEntry(table.id, {id: record.id, business_id: null}, {cells: {score: 100 + index, next_step: 'hidden', notes: 'bulk'}});
    }
    const lateLow = api.addTask({id: 'late-low', title: 'Late low score'});
    api.addEntry(table.id, {id: lateLow.id, business_id: null}, {cells: {score: 1, notes: 'late'}});
    const nullScore = api.addTask({id: 'null-score', title: 'No score'});
    api.addEntry(table.id, {id: nullScore.id, business_id: null}, {cells: {score: null, notes: 'empty'}});
    table.revision++;
    await page.locator('#data-refresh').click();
    await page.locator('.data-item').filter({hasText: 'Partner records'}).click();
    await page.locator('.crm-grid tbody tr').first().waitFor();

    await page.locator('[data-crm-sort=score]').click();
    await page.waitForFunction(() => document.querySelector('.crm-grid tbody tr .crm-open')?.textContent === 'Late low score');
    assert.equal(await page.locator('.crm-grid th[aria-sort=ascending]').textContent(), 'Score');
    let request = api.log.filter(call => call.op === 'crm_entries_list').at(-1);
    assert.equal(request.args.order_by, 'score');
    assert.equal(request.args.descending, false);
    await page.locator('[data-crm-sort=score]').click();
    await page.waitForFunction(() => document.querySelector('.crm-grid tbody tr .crm-open')?.textContent === 'Bulk record 54');
    assert.equal(await page.locator('.crm-grid th[aria-sort=descending]').textContent(), 'Score');
    await page.waitForFunction(() => document.querySelector('[data-crm-more]') && !document.querySelector('[data-crm-more]').hidden);
    while (!(await page.locator('[data-crm-more]').isHidden())) {
      const before = await page.locator('.crm-grid tbody tr').count();
      await page.locator('[data-crm-more]').click();
      await page.waitForFunction(previous => document.querySelectorAll('.crm-grid tbody tr').length > previous || document.querySelector('[data-crm-more]').hidden, before);
    }
    assert.match(await page.locator('.crm-grid tbody tr').last().textContent(), /No score/, 'null values remain last across server-sorted pages');
    request = api.log.filter(call => call.op === 'crm_entries_list').at(-1);
    assert.equal(request.args.descending, true);
    await page.locator('[data-crm-sort=score]').press('Enter');
    await page.waitForFunction(() => document.querySelector('.crm-grid th[aria-sort=ascending]')?.textContent === 'Score');
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.crmSort), 'score', 'keyboard sorting retains focus on its header');
    await page.locator('[data-crm-query]').fill('Katherine');
    await page.waitForFunction(() => document.querySelectorAll('.crm-grid tbody tr').length === 1);
    request = api.log.filter(call => call.op === 'crm_entries_list').at(-1);
    assert.equal(request.args.query, 'Katherine', 'sorting preserves the current search query');
    assert.equal(request.args.order_by, 'score');

    await page.locator('[data-crm-query]').fill('');
    const ada = page.locator('.crm-grid tbody tr').filter({hasText: 'Ada Lovelace'});
    const notes = ada.locator('[data-cell-input=notes]');
    const saveHold = server.hold('crm_entries_update');
    await notes.fill('pending local edit');
    assert.equal(await page.locator('[data-crm-sort=score]').isDisabled(), true, 'dirty rows disable sorting');
    await page.locator('[data-crm-save]').click();
    await saveHold.startedPromise;
    assert.equal(await page.locator('[data-crm-sort=score]').isDisabled(), true, 'a pending save keeps sorting disabled');
    saveHold.release();
    await page.waitForFunction(() => document.querySelector('[data-crm-draft]').hidden);
    const initialUpdates = api.log.filter(call => call.op === 'crm_entries_update').length;
    await ada.locator('.crm-stage-cycle').click();
    await page.waitForFunction(() => document.querySelector('[data-crm-status]')?.textContent.includes('moved to Lead'));
    assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('crm-stage-cycle')), true, 'stage cycling returns keyboard focus to the stage control');
    const firstWrite = api.log.filter(call => call.op === 'crm_entries_update').at(-1);
    assert.deepEqual(firstWrite.args.changes, {stage_id: 'lead'}, 'stage save is an immediate stage-only update');
    assert.ok(firstWrite.args.expected_revision);
    assert.ok(firstWrite.args.idempotency_key);
    assert.equal(api.log.filter(call => call.op === 'crm_entries_update').length, initialUpdates + 1);
    await page.locator('[data-crm-undo]').click();
    await page.waitForFunction(() => document.querySelector('.crm-grid tbody tr [aria-label^="Stage for Ada Lovelace"]')?.textContent === 'No stage');
    assert.equal(api.log.filter(call => call.op === 'crm_entries_update').at(-1).args.changes.stage_id, null, 'Undo restores the prior unset stage');
    assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('crm-stage-cycle')), true, 'Undo returns focus to the changed row');
    const adaStage = page.locator('.crm-grid tbody tr').filter({hasText: 'Ada Lovelace'}).locator('.crm-stage-cycle');
    await adaStage.press('Enter');
    await page.waitForFunction(() => document.querySelector('.crm-grid tbody tr [aria-label^="Stage for Ada Lovelace"]')?.textContent === 'Lead');
    await page.locator('.crm-grid tbody tr').filter({hasText: 'Ada Lovelace'}).locator('.crm-stage-cycle').press('Enter');
    await page.waitForFunction(() => document.querySelector('.crm-grid tbody tr [aria-label^="Stage for Ada Lovelace"]')?.textContent === 'Active');
    await page.locator('.crm-grid tbody tr').filter({hasText: 'Ada Lovelace'}).locator('.crm-stage-cycle').press('Enter');
    await page.waitForFunction(() => document.querySelector('.crm-grid tbody tr [aria-label^="Stage for Ada Lovelace"]')?.textContent === 'Lead');

    const retired = page.locator('.crm-grid tbody tr').filter({hasText: 'Grace Hopper'}).locator('.crm-stage-cycle');
    const retiredBefore = api.entries.get(table.id).find(entry => entry.record_task_id === 'record-b').stage_id;
    api.flags.conflictNext = true;
    await retired.click();
    await page.locator('[data-crm-error]').waitFor({state: 'visible'});
    assert.equal(api.entries.get(table.id).find(entry => entry.record_task_id === 'record-b').stage_id, retiredBefore, 'a conflict does not report or apply a stage change');
    assert.match(await page.locator('[data-crm-error]').textContent(), /latest records have been reloaded/);
    const hold = server.hold('crm_entries_update');
    await retired.click();
    await hold.startedPromise;
    assert.equal(await retired.isDisabled(), true, 'other stage controls are disabled while a write is pending');
    hold.release();
    await page.waitForFunction(() => document.querySelector('.crm-grid tbody tr [aria-label^="Stage for Grace Hopper"]')?.textContent === 'Lead');
    assert.equal(api.entries.get(table.id).find(entry => entry.record_task_id === 'record-b').stage_id, 'lead', 'retired current stages advance to the first active stage');
    const graceEntry = api.entries.get(table.id).find(entry => entry.record_task_id === 'record-b');
    graceEntry.stage_id = 'active'; graceEntry.revision++; table.revision++;
    const updateCountBeforeStaleUndo = api.log.filter(call => call.op === 'crm_entries_update').length;
    await page.locator('[data-crm-undo]').click();
    await page.waitForFunction(() => document.querySelector('[data-crm-status]')?.textContent.includes('changed elsewhere'));
    assert.equal(api.log.filter(call => call.op === 'crm_entries_update').length, updateCountBeforeStaleUndo, 'stale Undo never writes over a newer stage');
    assert.equal(await page.locator('[data-crm-undo]').isHidden(), true, 'external revisions clear the old Undo action');

    api.threads.get(table.id).push({id: 'thread-ref', kind: 'system', body: 'Membership changed.', created_at: '2026-10-28T12:00:00Z',
      change: {kind: 'crm_membership', operation: 'updated', record: {type: 'task', id: 'record-a', business_id: null}}});
    await page.evaluate(() => {
      const host = document.createElement('div'); host.id = 'seam-host'; document.body.append(host);
      const testTable = _data.table;
      window.__openRecordResult = null;
      window.__seamMount = crmTableMount(host, {biz: null, table: testTable, draftKey: 'seam', drafts: new Map(), isActive: () => true,
        onOpenRecord: entry => { window.__openRecordResult = {id: entry.record?.id || entry.record_ref.id, business_id: entry.record_ref.business_id}; }});
    });
    await page.locator('#seam-host .crm-grid tbody .crm-open').first().click();
    assert.deepEqual(await page.evaluate(() => window.__openRecordResult), {id: 'record-a', business_id: null}, 'optional opener receives the canonical entry and scope');
    await page.locator('#seam-host .crm-grid tbody tr').filter({hasText: 'Ada Lovelace'}).locator('.crm-task-state').click();
    assert.deepEqual(await page.evaluate(() => window.__openRecordResult), {id: 'record-a', business_id: null}, 'clicking a non-control row cell opens the record');
    assert.equal(await page.locator('#seam-host .crm-thread-list').count(),0,'the CRM does not mount shared discussion');
    assert.ok(api.threads.get(table.id).some(message=>message.id==='thread-ref'),'stored discussion history is retained');
    const beforeStageOpen = await page.evaluate(() => window.__openRecordResult);
    await page.locator('#seam-host .crm-grid tbody tr').filter({hasText: 'Ada Lovelace'}).locator('.crm-stage-cycle').click();
    await page.waitForFunction(() => document.querySelector('#seam-host [data-crm-status]')?.textContent.includes('moved to'));
    assert.deepEqual(await page.evaluate(() => window.__openRecordResult), beforeStageOpen, 'stage controls do not open the row');

    await page.evaluate(() => {
      const host = document.createElement('div'); host.id = 'reject-host'; document.body.append(host);
      crmTableMount(host, {biz: null, table: _data.table, draftKey: 'reject', drafts: new Map(), isActive: () => true,
        onOpenRecord: () => { window.__rejectCalled = true; return Promise.reject(new Error('opener rejected')); }});
    });
    await page.locator('#reject-host .crm-grid tbody tr .crm-task-state').first().click();
    await page.locator('#reject-host [data-crm-error]').waitFor({state: 'visible'});
    assert.equal(await page.evaluate(() => window.__rejectCalled), true, 'the supplied opener ran');
    assert.equal(await page.locator('#reject-host .modal').count(), 0, 'a rejected paired opener does not fall through to the modal');

    const inactive = api.addTable('global', {title: 'Retired stages', table_kind: 'crm',
      crm_config: {stages: [{id: 'old', name: 'Old', archived: true}]}, columns: [{id: 'name', name: 'Name', type: 'text', record_field: 'title'}]});
    api.addEntry(inactive.id, {id: 'record-a', business_id: null}, {stage_id: 'old'});
    await page.evaluate(inactiveTable => {
      const host = document.createElement('div'); host.id = 'inactive-host'; document.body.append(host);
      crmTableMount(host, {biz: null, table: inactiveTable, draftKey: 'inactive', drafts: new Map(), isActive: () => true});
    }, inactive);
    await page.locator('#inactive-host .crm-grid tbody .crm-stage-cycle').waitFor();
    assert.equal(await page.locator('#inactive-host .crm-grid tbody .crm-stage-cycle').isDisabled(), true, 'a table with only retired stages cannot cycle');
    const archivedTable = {...table, archived: true};
    await page.evaluate(archived => {
      const host = document.createElement('div'); host.id = 'archived-host'; document.body.append(host);
      crmTableMount(host, {biz: null, table: archived, draftKey: 'archived', drafts: new Map(), isActive: () => true});
    }, archivedTable);
    await page.locator('#archived-host .crm-grid tbody .crm-stage-cycle').first().waitFor();
    assert.equal(await page.locator('#archived-host .crm-grid tbody .crm-stage-cycle').first().isDisabled(), true, 'archived tables are read only');

    server.denied.add('crm_entries_list');
    await page.evaluate(() => {
      const host = document.createElement('div'); host.id = 'recovery-host'; document.body.append(host);
      crmTableMount(host, {biz: null, table: _data.table, draftKey: 'recovery', drafts: new Map(), isActive: () => true});
    });
    await page.locator('#recovery-host .crm-unavailable-state').waitFor();
    server.denied.delete('crm_entries_list');
    await page.locator('#recovery-host .crm-unavailable-state button').click();
    await page.locator('#recovery-host .crm-grid tbody tr').first().waitFor();
    const sortCallsBeforeRecoveryClick = api.log.filter(call => call.op === 'crm_entries_list').length;
    await page.locator('#recovery-host [data-crm-sort=score]').click();
    await page.waitForFunction(() => document.querySelector('#recovery-host .crm-grid th[aria-sort]'));
    assert.equal(api.log.filter(call => call.op === 'crm_entries_list').length, sortCallsBeforeRecoveryClick + 1, 'retry rebuilds do not duplicate sort listeners');

    console.log('CRM table presentation, sorting, stage cycling/Undo and optional opener seam passed.');
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
