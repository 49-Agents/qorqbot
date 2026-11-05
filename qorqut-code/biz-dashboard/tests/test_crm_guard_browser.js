// The record workspace's unsaved-change guard on every close route (X, Escape, backdrop, nested open), forwarded CRM draft
// controls, and fresh canonical projections / Table metadata after the record or Table changes, without touching drafts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {startCrmServer} = require('./support/crm_server');
const {launch, until} = require('./support/crm_page');
const shots = process.env.CRM_SHOTS || null;
if (shots) fs.mkdirSync(shots, {recursive: true});

const server = startCrmServer(), api = server.api;
server.actAs('member');
const cols = [{id: 'record_name', name: 'Name', type: 'text', record_field: 'title'}, {id: 'ov', name: 'Overview', type: 'text', record_field: 'overview'}, {id: 'note', name: 'Note', type: 'text'}];
const crm = api.addTable('global', {title: 'Guarded CRM', table_kind: 'crm', crm_config: {stages: [{id: 'lead', name: 'Lead'}, {id: 'won', name: 'Won'}]}, columns: cols});
const alphaCrm = api.addTable('alpha', {title: 'Alpha guarded', table_kind: 'crm', crm_config: {stages: []}, columns: cols});
api.addEntry(alphaCrm.id, api.addTask({id: 'a-1', assignee_ids: ['owner'], business_id: '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', title: 'Alpha record', overview: 'alpha'}));
const second = api.addTable('global', {title: 'Second CRM', table_kind: 'crm', crm_config: {stages: []}, columns: cols});
for (let n = 0; n < 60; n++) api.addEntry(crm.id, api.addTask({id: 'g-' + n, assignee_ids: ['owner'], title: 'Guard record ' + String(n).padStart(2, '0'), overview: 'overview ' + n}));
api.addEntry(second.id, api.task(null, 'g-0'));
const revisionOf = () => api.tables.get(crm.id).revision;

(async () => {
  const {browser, page, errors} = await launch(server);
  try {
    const open = async title => {
      await page.evaluate(() => { state.scope = 'global'; state.biz = null; state.globalTab = 'tables'; return renderTables(document.querySelector('#content')); });
      await page.locator('.data-item', {hasText: title}).click();
      await until(async () => await page.locator('.crm-grid tbody tr').count() >= 1, 'rows');
    };
    await open('Guarded CRM');
    const dialogs = () => page.locator('.modal:not([aria-hidden=true])');
    const unsaved = () => page.locator('.modal', {hasText: 'Unsaved changes'});

    // ---- Every close route asks first; Keep editing retains the edit; Discard closes without saving.
    await page.locator('.crm-open', {hasText: 'Guard record 00'}).click();
    const overview = page.locator('[data-task-overview]');
    await overview.waitFor();
    await overview.fill('unsaved overview edit');
    for (const route of ['escape', 'x', 'backdrop']) {
      if (route === 'escape') await page.keyboard.press('Escape');
      else if (route === 'x') await page.locator('.modal:not([aria-hidden=true]) .mx').first().click();
      else await page.locator('.overlay').first().dispatchEvent('mousedown');
      await unsaved().waitFor();
      await unsaved().getByRole('button', {name: 'Keep editing'}).click();
      assert.equal(await overview.inputValue(), 'unsaved overview edit', route + ': the edit survives');
      assert.equal(await page.locator('.crm-record-host').count(), 1, route + ': the record stays open');
    }
    // A nested open stacks above the dirty record; it never replaces it.
    await page.evaluate(() => crmOpenRecord({record_ref: {type: 'task', id: 'g-1', business_id: null}, record: {title: 'Guard record 01', task_type: 'task'}}));
    await until(async () => await page.locator('.crm-record-host').count() === 2, 'nested record');
    await until(async () => await page.locator('[data-task-overview]').count() === 2, 'both workspaces are retained');
    await page.keyboard.press('Escape');
    await until(async () => await page.locator('.crm-record-host').count() === 1, 'only the nested record closed');
    assert.equal(await overview.inputValue(), 'unsaved overview edit');
    if (shots) await page.screenshot({path: shots + '/crm-unsaved-guard.png'});
    await page.keyboard.press('Escape');
    await unsaved().getByRole('button', {name: 'Discard'}).click();
    await until(async () => await page.locator('.crm-record-host').count() === 0, 'discarded and closed');
    assert.equal(api.task(null, 'g-0').overview, 'overview 0', 'nothing was saved by discarding');

    // ---- Saving a canonical edit refreshes the projected cell on return without bumping the Table or losing a local draft.
    await page.getByRole('button', {name: 'Load more'}).click();
    await until(async () => await page.locator('.crm-grid tbody tr').count() === 60, 'load more');
    await page.locator('textarea[data-cell-input=note]').nth(2).fill('local draft stays');
    const before = revisionOf();
    await page.locator('.crm-open', {hasText: 'Guard record 00'}).click();
    await overview.waitFor();
    await overview.fill('edited in the record');
    await page.getByRole('button', {name: 'Save task'}).click();
    await until(async () => api.task(null, 'g-0').overview === 'edited in the record', 'canonical save');
    await until(async () => await page.getByRole('button', {name: 'Save task'}).isEnabled().catch(() => false), 'editor settled');
    await page.keyboard.press('Escape');
    await page.locator('.crm-record-host').waitFor({state: 'detached'});
    await until(async () => (await page.locator('tr', {hasText: 'Guard record 00'}).locator('[data-column=ov]').textContent()).includes('edited in the record'), 'projection refreshed');
    assert.equal(revisionOf(), before, 'a Task edit never bumps or persists the Table');
    assert.equal(await page.locator('.crm-grid tbody tr').count(), 60, 'pagination is preserved by the refresh');
    assert.equal(await page.locator('textarea[data-cell-input=note]').nth(2).inputValue(), 'local draft stays', 'the local draft is untouched');
    assert.equal(await page.evaluate(() => _dataDrafts.size), 1);
    await page.getByRole('button', {name: 'Discard'}).first().click();
    // The same record projected in a second CRM shows the same fresh value.
    await open('Second CRM');
    assert.match(await page.locator('tr', {hasText: 'Guard record 00'}).locator('[data-column=ov]').textContent(), /edited in the record/);

    // ---- Metadata: the left list count, remove/restore counts, and the thread's captured stage names.
    await open('Guarded CRM');
    const label = () => page.locator('.data-item', {hasText: 'Guarded CRM'}).locator('span').textContent();
    assert.match(await label(), /CRM · 60 entries/);
    page.on('dialog', d => d.accept());
    await page.locator('tr', {hasText: 'Guard record 01'}).getByRole('button', {name: /^Remove/}).evaluate(el => el.click());
    await page.getByRole('button', {name: 'Remove from CRM'}).last().click();
    await until(async () => /CRM · 59 entries/.test(await label()), 'list count after remove');
    await page.locator('[data-crm-removed]').check();
    await page.locator('tr', {hasText: 'Guard record 01'}).getByRole('button', {name: /^Restore/}).evaluate(el => el.click());
    await until(async () => /CRM · 60 entries/.test(await label()), 'list count after restore');
    const stage = page.locator('tr', {hasText: 'Guard record 02'}).locator('.crm-stage-cycle');
    await stage.click();
    await until(async () => (await stage.textContent()).trim() === 'Lead', 'the configured first stage is saved');
    await page.locator('.crm-thread-change', {hasText: 'Lead'}).first().waitFor();
    // A Table configuration change made elsewhere reaches the open thread when the Table is reloaded.
    const table = api.tables.get(crm.id);
    table.crm_config.stages[0].name = 'Qualified lead'; table.revision += 1;
    await page.evaluate(() => _crmMount.refresh());
    await page.locator('.crm-thread-change', {hasText: 'Qualified lead'}).first().waitFor();

    // ---- The Focus workspace path (Global CRM and Project CRM): local-cell drafts use the same guard, Save/Discard are real.
    for (const [table, scopeName] of [[crm, 'global'], [alphaCrm, 'project']]) {
      server.pin({type: 'table', id: table.id, business_id: table.business_id});
      await page.goto(`${server.origin}/?focus_item=card-${table.id}#/global/agents`);
      await page.locator('.rw-record:not([hidden]) [data-rw-tab=dashboard]').click();
      const cell = page.locator('textarea[data-cell-input=note]').first();
      await cell.waitFor();
      await cell.fill('workspace draft ' + scopeName);
      const closing = page.evaluate(() => _agpad.focusWorkspace.close());
      await unsaved().waitFor();
      assert.ok(await unsaved().getByRole('button', {name: 'Save'}).count() === 1, scopeName + ': the CRM offers a real Save');
      await unsaved().getByRole('button', {name: 'Keep editing'}).click();
      await closing;
      assert.equal(await cell.inputValue(), 'workspace draft ' + scopeName, scopeName + ': Keep editing retains the draft');
      const closing2 = page.evaluate(() => _agpad.focusWorkspace.close());
      await unsaved().waitFor();
      await unsaved().getByRole('button', {name: 'Discard'}).click();
      await closing2;
      assert.equal(await page.evaluate(() => rwCrmDrafts.size), 0, scopeName + ': Discard drops the CRM draft');
      const save = async () => {
        await page.goto(`${server.origin}/?focus_item=card-${table.id}#/global/agents`);
        await page.locator('.rw-record:not([hidden]) [data-rw-tab=dashboard]').click();
        await page.locator('textarea[data-cell-input=note]').first().fill('saved from guard ' + scopeName);
        const closeSave = page.evaluate(() => _agpad.focusWorkspace.close());
        await unsaved().waitFor();
        await unsaved().getByRole('button', {name: 'Save'}).click();
        await closeSave;
      };
      await save();
      assert.ok(api.entries.get(table.id).some(entry => entry.cells.note === 'saved from guard ' + scopeName), scopeName + ': Save wrote the cell');
    }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
  console.log('CRM guard: guarded X/Escape/backdrop closes, stacked nested records, forwarded drafts, refreshed projections, list counts and thread stage names passed');
})().catch(error => { console.error(error); process.exit(1); });
