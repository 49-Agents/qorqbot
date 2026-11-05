// Outer Table metadata on revocation and retry, delayed replies from a previously selected Table, the unsaved-ancestor modal
// rule, and the Add-entry dialog while its write is pending. Real frontend against the synthetic local mock.
const assert = require('node:assert/strict');
const {startCrmServer} = require('./support/crm_server');
const {launch, until} = require('./support/crm_page');

const ALPHA = '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const server = startCrmServer(), api = server.api;
server.actAs('member');
const cols = [{id: 'record_name', name: 'Name', type: 'text', record_field: 'title'}, {id: 'secret_col', name: 'Confidential column', type: 'text'}];
const probe = api.addTable('global', {title: 'Probe CRM', description: 'Synthetic confidential table description', table_kind: 'crm', crm_config: {stages: []}, columns: cols});
const other = api.addTable('global', {title: 'Other CRM', table_kind: 'crm', crm_config: {stages: []}, columns: cols});
const alphaCrm = api.addTable('alpha', {title: 'Alpha CRM', description: 'Alpha confidential description', table_kind: 'crm', crm_config: {stages: []}, columns: cols});
for (const table of [probe, other, alphaCrm]) api.addEntry(table.id, api.addTask({id: 'r-' + table.id.slice(0, 4), business_id: table.business_id, assignee_ids: ['owner'], title: 'Record of ' + table.title, overview: 'x'}), {cells: {secret_col: 'secret cell'}});

(async () => {
  const {browser, page, errors} = await launch(server);
  try {
    const show = async title => {
      await page.evaluate(() => { document.querySelector('#ctxembedded')?.remove(); state.scope = 'global'; state.biz = null; state.globalTab = 'tables'; return renderTables(document.querySelector('#content')); });
      await page.locator('.data-item', {hasText: title}).click();
      await page.locator('.crm-grid tbody tr').first().waitFor();
    };
    const openProject = async title => {
      await page.evaluate(() => {
        document.querySelector('#content').replaceChildren();
        state.scope = 'business'; state.biz = 'alpha'; state.tab = 'context'; state.ctx = '_tables';
        let host = document.querySelector('#ctxembedded');
        if (!host) { host = document.createElement('div'); host.id = 'ctxembedded'; host.style.cssText = 'position:fixed;inset:0;z-index:5;background:var(--bg,#111);display:flex'; document.body.append(host); }
        return renderTables(host);
      });
      await page.locator('.data-item', {hasText: title}).click();
      await page.locator('.crm-grid tbody tr').first().waitFor();
    };
    const body = () => page.evaluate(() => document.querySelector('#data-detail, #ctxembedded, #content').innerText);

    // ---- Revocation clears the outer description, heading, related area and list label; a held retry stays unavailable.
    await show('Probe CRM');
    assert.match(await body(), /Synthetic confidential table description/);
    server.flags.denyScope = 'global';
    await page.evaluate(() => _crmMount.refresh());
    await page.locator('.crm-unavailable-state').waitFor();
    assert.equal(await page.locator('.data-description').count(), 0, 'the description is gone');
    assert.equal(await page.locator('#data-edit-table, #crm-pin-table, #data-copy-link').count(), 0, 'no actions for an unavailable Table');
    let text = await body();
    assert.ok(!/Synthetic confidential|Confidential column|secret cell|Record of Probe/.test(text), 'no source metadata remains rendered');
    assert.equal(await page.evaluate(() => _data.table.columns.length + (_data.table.title ? 1 : 0)), 0, 'the outer Table reference is no longer the old Table');
    // Held retry (the scope reads fine again, but the answer has not arrived): still nothing of the old Table is shown.
    server.flags.denyScope = null;
    const held = server.hold('tables_get');
    await page.getByRole('button', {name: 'Check access again'}).click();
    await held.startedPromise;
    assert.equal(await page.locator('.crm-grid').count(), 0, 'no cached grid while the retry is pending');
    assert.ok(!/Confidential column/.test(await body()), 'no cached column names while the retry is pending');
    held.release();
    await page.locator('.crm-grid tbody tr').first().waitFor();
    // Recovery with an unchanged Table revision restores heading, settings and configuration from the fresh response.
    assert.equal(await page.locator('.crm-title').textContent(), 'Probe CRM');
    assert.equal(await page.locator('#data-edit-table').count(), 1, 'Settings are back');
    assert.match(await body(), /Synthetic confidential table description/);
    assert.equal(await page.evaluate(() => _data.table.title), 'Probe CRM');
    // A retry while still denied remains unavailable with no old columns.
    server.flags.denyScope = 'global';
    await page.evaluate(() => _crmMount.refresh());
    await page.locator('.crm-unavailable-state').waitFor();
    await page.getByRole('button', {name: 'Check access again'}).click();
    await page.locator('.crm-unavailable-state').getByText('This table is unavailable.').waitFor();
    assert.equal(await page.locator('.crm-grid').count(), 0);
    server.flags.denyScope = null;
    // Project route: the related-work area is cleared too.
    await openProject('Alpha CRM');
    assert.equal(await page.locator('#data-work').count(), 1);
    server.flags.denyScope = 'alpha';
    await page.evaluate(() => _crmMount.refresh());
    await page.locator('.crm-unavailable-state').waitFor();
    assert.equal(await page.locator('#data-work, .data-description').count(), 0, 'related labels and description are gone in a Project');
    server.flags.denyScope = null;

    // ---- A delayed metadata reply for a previously selected Table never replaces the selected one.
    await show('Probe CRM');
    api.tables.get(probe.id).revision += 1;                     // the next entries read reports a newer revision => metadata reload
    const slow = server.hold('tables_get');
    await page.evaluate(() => { window.__old = _crmMount.refresh(); });
    await slow.startedPromise;
    await page.locator('.data-item', {hasText: 'Other CRM'}).click();
    await page.locator('.crm-grid tbody tr', {hasText: 'Record of Other CRM'}).waitFor();
    slow.release();
    await new Promise(r => setTimeout(r, 400));
    assert.equal(await page.locator('.crm-title').textContent(), 'Other CRM');
    assert.equal(await page.evaluate(() => _data.table.title), 'Other CRM', 'the outer Table reference stays the selected Table');
    await page.locator('#data-edit-table').click();
    assert.equal(await page.locator('#data-title').inputValue(), 'Other CRM', 'Settings target the Table that is shown');
    await page.locator('.modal .mx').click();

    // ---- A clean child never authorizes discarding a dirty ancestor when another dialog replaces.
    await show('Other CRM');
    await page.locator('.crm-open').first().click();
    const overview = page.locator('[data-task-overview]');
    await overview.waitFor();
    await overview.fill('dirty ancestor edit');
    await overview.focus();
    await page.evaluate(() => {
      window.__child = modal({stack: true, title: 'Clean child', body: '<p>child</p>', actions: [{label: 'OK', onClick: close => close()}]});
    });
    await page.evaluate(() => { window.__replacing = modal({title: 'Replacing dialog', body: '<p>replace</p>', actions: [{label: 'Done', onClick: close => close()}]}); });
    assert.equal(await page.locator('.crm-record-host').count(), 1, 'the dirty record workspace was not disposed');
    assert.equal(await overview.inputValue(), 'dirty ancestor edit', 'the draft is intact');
    assert.equal(await page.locator('.modal').count(), 3, 'the replacing dialog stacked above both');
    await page.evaluate(() => { window.__replacing.close(); window.__child.close(); });
    assert.equal(await page.evaluate(() => document.activeElement?.matches('[data-task-overview]')), true, 'focus returns to where it was');
    await page.keyboard.press('Escape');
    await page.locator('.modal', {hasText: 'Unsaved changes'}).getByRole('button', {name: 'Keep editing'}).click();
    assert.equal(await overview.inputValue(), 'dirty ancestor edit');
    await page.keyboard.press('Escape');
    await page.locator('.modal', {hasText: 'Unsaved changes'}).getByRole('button', {name: 'Discard'}).click();
    await page.locator('.crm-record-host').waitFor({state: 'detached'});

    // The approved CRM table removes the Add-entry entry point and dialog.
    await show('Other CRM');
    assert.equal(await page.locator('[data-crm-add],.crm-add').count(), 0);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
  console.log('CRM unavailable: outer metadata cleared and restored from fresh reads, stale metadata replies dropped, dirty ancestors preserved, removed Add-entry entry point absent passed');
})().catch(error => { console.error(error); process.exit(1); });
