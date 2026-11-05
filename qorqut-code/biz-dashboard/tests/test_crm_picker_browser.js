// The scope-aware existing-record picker: a Project CRM opened from Global Focus, cross-scope links, the same Task id in two
// scopes, every Task subtype, pagination, stale (out-of-order) results, exact retries and removal from the chosen set.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {startCrmServer} = require('./support/crm_server');
const {launch, until} = require('./support/crm_page');
const shots = process.env.CRM_SHOTS || null;
if (shots) fs.mkdirSync(shots, {recursive: true});

const ALPHA = '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const server = startCrmServer(), api = server.api;
const cols = [{id: 'record_name', name: 'Name', type: 'text', record_field: 'title'}];
const alphaCrm = api.addTable('alpha', {title: 'Alpha pipeline', table_kind: 'crm', crm_config: {stages: []}, columns: cols});
const globalCrm = api.addTable('global', {title: 'Global pipeline', table_kind: 'crm', crm_config: {stages: []}, columns: cols});
api.addTask({id: 'dup', title: 'Dup in Global', task_type: 'task'});
api.addTask({id: 'dup', business_id: ALPHA, title: 'Dup in Alpha', task_type: 'story'});
api.addTask({id: 'a-cust', business_id: ALPHA, title: 'Alpha customer', task_type: 'customer'});
api.addTask({id: 'a-mail', business_id: ALPHA, title: 'Alpha email thread', task_type: 'email', email_conversation_id: 'c'});
for (let n = 0; n < 60; n++) api.addTask({id: 'g-' + n, title: 'Global bulk ' + String(n).padStart(2, '0'), task_type: 'task'});
const adds = () => server.requests.filter(r => r.path.endsWith('/operations/crm_entries_add'));

(async () => {
  const {browser, page, errors} = await launch(server, {route: `/?focus_item=card-${alphaCrm.id}#/global/agents`});
  try {
    server.pin({type: 'table', id: alphaCrm.id, business_id: ALPHA});
    server.pin({type: 'table', id: globalCrm.id, business_id: null});
    await page.goto(`${server.origin}/?focus_item=card-${alphaCrm.id}#/global/agents`);
    // The Project CRM opens in Global Focus (the current screen is Global, not Alpha).
    assert.notEqual(await page.evaluate(() => state.biz), 'alpha', 'the current screen is not the CRM\'s Project');
    await page.locator('.rw-record:not([hidden]) [data-rw-tab=dashboard]').click();
    const addButton = page.locator('[data-crm-add]');
    await addButton.waitFor();
    await addButton.click();
    await page.locator('input[name=mode][value=link]').check();
    await page.locator('[data-crm-choose]').click();
    const picker = page.locator('.modal:not([aria-hidden=true])', {hasText: 'Choose existing records'});
    await picker.waitFor();
    const scope = picker.locator('[data-crm-scope]');
    assert.equal(await scope.inputValue(), 'p:' + ALPHA, 'defaults to the Table scope, not the current screen');
    await picker.locator('label.work-association-choice', {hasText: 'Dup in Alpha'}).waitFor();
    assert.equal(await picker.locator('label', {hasText: 'Dup in Global'}).count(), 0, 'no Global rows while the held scope is Alpha');
    assert.ok((await picker.locator('label.work-association-choice').allTextContents()).some(t => /Alpha customer.*Customer/.test(t)), 'subtype labels');
    await picker.locator('label', {hasText: 'Dup in Alpha'}).locator('input').check();
    await picker.locator('label', {hasText: 'Alpha email thread'}).locator('input').check();

    // Cross-scope selection keeps the full scoped reference; the same id in two scopes is two selections.
    await scope.selectOption('global');
    await picker.locator('label', {hasText: 'Dup in Global'}).waitFor();
    assert.equal(await picker.locator('label', {hasText: 'Dup in Alpha'}).count(), 0);
    await picker.locator('label', {hasText: 'Dup in Global'}).locator('input').check();
    assert.equal(await picker.locator('.crm-picked li').count(), 3);
    // Pagination (50 per page) with no duplicate rows, and a selection survives a search.
    await picker.getByRole('button', {name: 'More records'}).click();
    await until(async () => await picker.locator('label.work-association-choice').count() === 61, 'second page');
    await picker.locator('[data-crm-find]').fill('bulk 07');
    await until(async () => await picker.locator('label.work-association-choice').count() === 1, 'search');
    assert.equal(await picker.locator('.crm-picked li').count(), 3, 'the chosen set survives paging and search');
    await picker.locator('[data-crm-find]').fill('');
    // A stale answer for the previous scope never renders (out-of-order responses).
    const hold = server.hold('tasks_list');
    await scope.selectOption('p:' + ALPHA);
    await hold.startedPromise;
    await scope.selectOption('global');
    await until(async () => await picker.locator('label', {hasText: 'Dup in Global'}).count() === 1, 'global again');
    hold.release();
    await new Promise(r => setTimeout(r, 250));
    assert.equal(await picker.locator('label', {hasText: 'Dup in Alpha'}).count(), 0, 'the stale Alpha response was dropped');
    if (shots) await page.screenshot({path: shots + '/crm-picker.png'});
    // Remove one from the chosen set.
    await picker.getByRole('button', {name: 'Remove Alpha email thread from selection'}).click();
    assert.equal(await picker.locator('.crm-picked li').count(), 2);
    await picker.getByRole('button', {name: 'Use selected records'}).click();
    await page.locator('.crm-chosen li').first().waitFor();
    assert.equal(await page.locator('.crm-add .crm-chosen li').count(), 2);

    // Exact retries: the first write succeeds, the second fails; a retry reuses the key and the new revision.
    server.failing.add('crm_entries_add');
    await page.locator('.modal .mfoot .primary').click();
    await page.locator('.data-form-error').filter({hasText: /\S/}).waitFor();
    const failedKey = adds().at(-1).body.idempotency_key;
    assert.equal(await page.locator('.crm-add .crm-chosen li').count(), 2, 'nothing is lost on failure');
    server.failing.delete('crm_entries_add');
    await page.locator('.modal .mfoot .primary').click();
    await until(async () => !(await page.locator('.crm-add').count()), 'dialog closed');
    assert.equal(adds().slice(-2)[0].body.idempotency_key, failedKey, 'an exact retry reuses its idempotency key');
    const records = adds().slice(-2).map(r => r.body.record);
    assert.deepEqual(records.map(r => [r.id, r.business_id]).sort(), [['dup', ALPHA], ['dup', null]].sort(), 'full scoped refs are sent, never the Table scope');
    assert.equal(adds().slice(-2)[1].body.expected_revision, adds().slice(-2)[0].body.expected_revision + 1, 'revisions are chained');
    await until(async () => await page.locator('.crm-grid tbody tr').count() === 2, 'two entries');
    const titles = await page.locator('.crm-record-cell .crm-open').allTextContents();
    assert.deepEqual(titles.sort(), ['Dup in Alpha', 'Dup in Global']);
    assert.deepEqual(api.entries.get(alphaCrm.id).filter(e => e.record_task_id === 'dup').map(e => e.record_business_id ?? 'global').sort(), [ALPHA, 'global'].sort(), 'both scopes are members of the Project CRM');

    // ---- A selection made from a scope that is then denied is redacted everywhere and cannot be submitted.
    await page.locator('[data-crm-add]').click();
    await page.locator('input[name=mode][value=link]').check();
    await page.locator('[data-crm-choose]').click();
    const again = page.locator('.modal:not([aria-hidden=true])', {hasText: 'Choose existing records'});
    await again.locator('label', {hasText: 'Alpha customer'}).locator('input').check();
    await again.locator('[data-crm-scope]').selectOption('global');
    await again.locator('label', {hasText: 'Global bulk 03'}).locator('input').check();
    assert.equal(await again.locator('.crm-picked li').count(), 2);
    server.flags.denyScope = 'alpha';
    await again.locator('[data-crm-scope]').selectOption('p:' + ALPHA);
    await again.locator('.ferr').filter({hasText: 'unavailable'}).waitFor();
    const picked = await again.locator('.crm-picked').textContent();
    assert.ok(!picked.includes('Alpha customer'), 'the denied scope\'s selected title is redacted: ' + picked);
    assert.ok(picked.includes('Global bulk 03'), 'selections from readable scopes stay');
    await again.getByRole('button', {name: 'Use selected records'}).click();
    const copied = await page.locator('.crm-add .crm-chosen').textContent();
    assert.ok(!copied.includes('Alpha customer') && copied.includes('Global bulk 03'), 'the Add dialog copy is propagated: ' + copied);
    server.flags.denyScope = null;
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
  console.log('CRM picker: held scope default, Project CRM from Global Focus, cross-scope and same-id selection, paging, stale results, exact retries and removal passed');
})().catch(error => { console.error(error); process.exit(1); });
