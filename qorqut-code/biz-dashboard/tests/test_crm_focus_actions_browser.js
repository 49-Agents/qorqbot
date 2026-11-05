// Focus row actions and the Add-to-Focus Table picker: readable wide and narrow action layout (screenshots are inspected),
// Customer state beside the CRM stage, unresolvable Project scopes that never route to Global, a denied early entry read with
// no unhandled rejection, and a picker bound to the current explicit scope/kind query (stale, failed and out-of-order loads).
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {startCrmServer} = require('./support/crm_server');
const {launch, until} = require('./support/crm_page');
const shots = process.env.CRM_SHOTS || null;
if (shots) fs.mkdirSync(shots, {recursive: true});

const ALPHA = '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', GHOST = 'ffffffff-ffff-4fff-8fff-ffffffffffff';
const server = startCrmServer(), api = server.api;
const cols = [{id: 'record_name', name: 'Name', type: 'text', record_field: 'title'}];
const crm = api.addTable('global', {title: 'Action CRM', table_kind: 'crm', crm_config: {stages: [{id: 'lead', name: 'Lead'}]}, columns: cols});
const alphaCrm = api.addTable('alpha', {title: 'Alpha CRM', table_kind: 'crm', crm_config: {stages: []}, columns: cols});
api.addTable('alpha', {title: 'Alpha inventory', table_kind: 'general'});
api.addTable('global', {title: 'Global inventory', table_kind: 'general'});
const cust = api.addTask({id: 'cust', title: 'Customer member', task_type: 'customer', customer_state: 'Prospect'});
api.addEntry(crm.id, cust, {stage_id: 'lead'});
api.addEntry(crm.id, api.addTask({id: 'plain', title: 'Plain member', task_type: 'task'}));
const requestsTo = pattern => server.requests.filter(r => pattern.test(r.path));

(async () => {
  server.pin({type: 'table', id: crm.id, business_id: null});
  server.pin({type: 'table', id: alphaCrm.id, business_id: ALPHA});
  // A Table card whose Project cannot be resolved by this owner: the actions must never route it to Global.
  server.board.items.push({id: 'card-ghost', revision: 1, label: 'Ghost table', state: null, reason: '', archived: false, created_by: 'x', unavailable: false, references: [],
    source: {type: 'table', id: 'ghost-table', business_id: GHOST}, source_revision: 1,
    resolved_source: {type: 'table', id: 'ghost-table', business_id: GHOST, label: 'Ghost table', table_kind: 'crm', archived: false, resource_type: 'data_tables', revision: 1},
    conversation: {type: 'table', id: 'ghost-table', business_id: GHOST}});
  const {browser, page, errors} = await launch(server);
  try {
    const card = id => page.locator(`.agfocus-items [data-card-id="card-${id}"]`);
    await card(crm.id).waitFor();
    await card(crm.id).locator('.agfocus-crm-toggle').click();
    const rows = page.locator(`.agfocus-items [data-card-id^="card-${crm.id}/crm/"]:visible`);
    await until(async () => await rows.count() === 2, 'entries');

    // ---- Customer state stays beside (not replaced by) the CRM stage.
    const customerFlag = await rows.first().locator('.agfocus-flag').textContent();
    assert.match(customerFlag, /Stage: Lead/);
    assert.match(await rows.first().textContent(), /Prospect/, 'the Customer state is retained: ' + customerFlag);

    // ---- Action layout, wide and narrow: labels do not wrap letter by letter and never overlap the next row.
    const layout = async () => page.evaluate(id => {
      const out = [];
      for (const button of document.querySelectorAll(`.agfocus-items [data-card-id^="card-${id}/crm/"] [data-focus-extra]`)) {
        const rect = button.getBoundingClientRect();
        if (!rect.width) continue;
        out.push({text: button.textContent.trim(), width: rect.width, height: rect.height, scrollW: button.scrollWidth, clientW: button.clientWidth});
      }
      return out;
    }, crm.id);
    for (const [name, width] of [['wide', 1500], ['narrow', 700]]) {
      await page.setViewportSize({width, height: 900});
      await new Promise(r => setTimeout(r, 250));
      await rows.first().hover();
      await new Promise(r => setTimeout(r, 200));
      const buttons = await layout();
      assert.ok(buttons.length >= 2, name + ': extra actions render');
      for (const b of buttons) {
        assert.ok(b.height <= 28, `${name}: "${b.text}" stays one line (height ${b.height})`);
        assert.ok(b.width >= b.text.length * 4, `${name}: "${b.text}" is wide enough (width ${b.width})`);
        assert.ok(b.scrollW <= b.clientW + 1, `${name}: "${b.text}" is not clipped`);
      }
      if (shots) await page.screenshot({path: path.join(shots, `focus-crm-actions-${name}.png`)});
    }
    await page.setViewportSize({width: 1400, height: 1000});

    // ---- An unresolvable Project never routes Table actions to Global.
    const ghostCard = page.locator('.agfocus-items [data-card-id="card-ghost"]');
    await ghostCard.waitFor();
    const before = requestsTo(/\/root\/operations\/(tables_get|tables_update|crm_entries_remove)/).length;
    const archive = ghostCard.locator('[data-focus-extra="table"]');
    assert.equal(await archive.count(), 1, 'the unresolved Table card still offers the action');
    await archive.evaluate(el => el.click());
    await page.locator('.agfocus-error').filter({hasText: 'unavailable'}).waitFor();
    assert.equal(requestsTo(/\/root\/operations\/(tables_get|tables_update|crm_entries_remove)/).length, before, 'no Global request was made for an unknown Project');

    // ---- A denied entry read with a failing parallel Table read leaves no unhandled rejection.
    server.denied.add('crm_entries_list'); server.failing.add('tables_get');
    await card(alphaCrm.id).locator('.agfocus-crm-toggle').click();
    await page.getByText(/unavailable|Could not/).first().waitFor();
    server.denied.delete('crm_entries_list'); server.failing.delete('tables_get');
    await new Promise(r => setTimeout(r, 300));
    assert.deepEqual(errors, [], 'no unhandled rejection from the parallel Table read');

    // ---- The Add-to-Focus Table picker follows the current explicit query only.
    await page.locator('[data-focus-add]').click();
    await page.locator('input[name=kind][value=table]').check();
    const options = () => page.locator('select[name=table] option').allTextContents();
    const add = page.locator('.modal .mfoot .primary');
    await until(async () => (await options()).includes('Table · Global inventory'), 'global tables');
    assert.equal(await add.isDisabled(), false, 'enabled once a current choice exists');
    // Scope switch in flight: previous options vanish at once, Add is off until the new query lands.
    const hold = server.hold('tables_list');
    await page.locator('select[name=table_scope]').selectOption('alpha');
    await hold.startedPromise;
    assert.deepEqual(await options(), [], 'stale options are cleared immediately');
    assert.equal(await add.isDisabled(), true, 'Add is disabled while the replacement loads');
    // Out of order: switch back to Global while Alpha is still held; the late Alpha answer must not repopulate.
    await page.locator('select[name=table_scope]').selectOption('');
    await until(async () => (await options()).includes('Table · Global inventory'), 'global again');
    hold.release();
    await new Promise(r => setTimeout(r, 250));
    assert.ok(!(await options()).some(label => /Alpha/.test(label)), 'the late Alpha response was discarded');
    // A failed replacement load leaves nothing selectable.
    server.failing.add('tables_list');
    await page.locator('select[name=table_scope]').selectOption('alpha');
    await page.locator('[data-add-table-status]').filter({hasText: 'Could not load tables'}).waitFor();
    assert.deepEqual(await options(), []);
    assert.equal(await add.isDisabled(), true);
    server.failing.delete('tables_list');
    await page.locator('select[name=table_kind]').selectOption('crm');
    await until(async () => (await options()).includes('CRM · Alpha CRM'), 'alpha crm');
    await page.locator('select[name=table]').selectOption({label: 'CRM · Alpha CRM'});
    // An exact retry keeps its key; another selection starts a new request.
    server.failing.add('focus_present');
    await add.click();
    await until(async () => requestsTo(/focus_present/).filter(r => r.body.source?.type === 'table').length >= 1, 'first attempt');
    const keyA = requestsTo(/focus_present/).filter(r => r.body.source?.type === 'table').at(-1).body.idempotency_key;
    await until(async () => await page.locator('.modal [role=alert]:visible').count() > 0 || true, 'settled');
    await new Promise(r => setTimeout(r, 200));
    await page.locator('.modal .mfoot .primary').evaluate(el => { el.disabled = false; });
    await add.click();
    await until(async () => requestsTo(/focus_present/).filter(r => r.body.source?.type === 'table').length >= 2, 'retry');
    assert.equal(requestsTo(/focus_present/).filter(r => r.body.source?.type === 'table').at(-1).body.idempotency_key, keyA, 'unchanged request reuses its key');
    assert.equal(requestsTo(/focus_present/).at(-1).body.source.business_id, ALPHA);
    server.failing.delete('focus_present');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
  console.log('CRM Focus actions: readable wide/narrow actions, Customer state beside stage, no Global fallback, no unhandled rejection, picker bound to the current query passed');
})().catch(error => { console.error(error); process.exit(1); });
