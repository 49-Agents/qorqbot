#!/usr/bin/env node
// Global > Tables reuse the business table view in their own namespace. Real isolated API, SQLite, files and
// Git through the biz-core browser fixture (--tables --global-tables). No production data or model calls.
// Requires Playwright (see README) and a biz-core checkout with the Global tables migration.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn, execFileSync} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const core = process.env.BAZARLYK_CORE_ROOT || path.resolve(__dirname, '../../biz-core');
const out = process.env.TABLE_SCREENSHOTS || '/tmp/qorqut-global-table-screenshots';

(async () => {
  const child = spawn(process.env.BAZARLYK_TEST_PYTHON || path.join(core, '.venv/bin/python'),
    ['tests/service_browser_fixture.py', '--tables', '--global-tables', '--dashboard-root', path.resolve(__dirname, '..')],
    {cwd: core, env: {...process.env, PYTHONPATH: '.'}, stdio: ['ignore', 'pipe', 'pipe']});
  let browser, stderr = '';
  child.stderr.on('data', data => { stderr += data; });
  try {
    const details = await new Promise((resolve, reject) => {
      let stdout = '';
      const timer = setTimeout(() => reject(new Error('Fixture timeout: ' + stderr)), 90000);
      child.stdout.on('data', data => {
        stdout += data;
        for (const line of stdout.split('\n')) {
          if (!line.startsWith('{"url":')) continue;
          try { const value = JSON.parse(line); clearTimeout(timer); resolve(value); } catch (_) {}
        }
      });
      child.once('exit', code => { clearTimeout(timer); reject(new Error('Fixture exited ' + code + ': ' + stderr)); });
      child.once('error', error => { clearTimeout(timer); reject(error); });
    });
    fs.mkdirSync(out, {recursive: true});
    browser = await chromium.launch({headless: true, executablePath: process.env.CHROME_BIN, chromiumSandbox: process.env.CHROMIUM_SANDBOX !== '0'});
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    await context.route('**/*', route => route.request().url().startsWith(details.origin) ? route.continue() : route.abort());
    const page = await context.newPage(), errors = [], requests = [];
    page.setDefaultTimeout(20000);
    page.on('pageerror', error => errors.push(error.message + ' @ ' + page.url().split('#')[1] + '\n' + (error.stack || '').split('\n').slice(0, 4).join('\n')));
    page.on('request', request => requests.push({method: request.method(), path: new URL(request.url()).pathname, body: request.postData()}));
    const tableCalls = () => requests.filter(request => /\/operations\/tables_/.test(request.path));
    const save = async () => {
      await page.locator('.modal .mfoot .primary').click();
      await page.waitForFunction(() => !document.querySelector('.modal') || document.querySelector('.data-form-error')?.textContent);
      assert.equal(await page.evaluate(() => document.querySelector('.data-form-error')?.textContent || ''), '');
    };
    const rowCount = count => page.waitForFunction(n => document.querySelectorAll('.data-grid tbody tr').length === n, count);
    const saveRows = async () => {
      await page.locator('#data-save').click();
      await page.waitForFunction(() => !document.querySelector('#data-draft-actions') || document.querySelector('#data-draft-actions').hidden || !document.querySelector('#data-error').hidden);
      assert(await page.locator('#data-error').isHidden(), await page.locator('#data-error').textContent());
    };
    const editCell = async (column, value) => {
      const cell = page.locator(`.data-grid tbody tr:first-child [data-column="${column}"]`);
      if (!await cell.locator('[data-cell-input]').count()) await cell.locator('.data-cell').click();
      await cell.locator('[data-cell-input]').fill(value);
    };
    const globalCall = (op, args = {}) => page.evaluate(([operation, body]) => dataCall(null, operation, body), [op, args]);
    const openGlobalTables = async () => {
      await page.evaluate(() => { location.hash = '/global/tables'; });
      await page.waitForFunction(() => state.scope === 'global' && state.globalTab === 'tables' && document.querySelector('#data-new') && !document.querySelector('#data-new').disabled);
    };
    const openBusinessTables = async () => {
      await page.evaluate(slug => selectBiz(slug), details.business_slug);
      await page.waitForFunction(() => state.scope === 'business');
      await page.locator('.rail [data-tab=context]').click();
      await page.locator('#ctxrows [data-p=_tables]').click();
      await page.waitForFunction(() => document.querySelector('#data-new') && !document.querySelector('#data-new').disabled);
    };
    const waitForGit = async id => {
      for (let n = 0; n < 40; n++) {
        const saved = await globalCall('tables_history', {table_id: id}).then(result => result.items.length > 0 && result.items.every(version => version.git_commit));
        if (saved) return;
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      throw new Error('Git publication did not finish');
    };
    await page.goto(details.url);
    await page.waitForFunction(() => typeof state !== 'undefined' && state.bizs?.length);
    console.log('Fixture loaded');

    // A business table proves the namespaces stay apart in both directions.
    await page.evaluate(slug => selectBiz(slug), details.business_slug);
    await page.locator('#agentrows').waitFor();
    await page.evaluate(async () => dataCall(state.biz, 'tables_create', {title: 'Widget CRM', columns: [{id: 'name', name: 'Name', type: 'text'}],
      idempotency_key: newRequestId()}));
    // Let the agent terminal finish its first frames before leaving it: xterm throws when its viewport refresh
    // lands after disposal, which is an existing terminal race unrelated to tables.
    await page.locator('.termhost .xterm').first().waitFor({timeout: 5000}).catch(() => {});
    await page.evaluate(() => new Promise(resolve => setTimeout(() => requestAnimationFrame(() => requestAnimationFrame(resolve)), 500)));
    await page.locator('.rail [data-tab=context]').click();
    await page.locator('#ctxrows [data-p=_tables]').waitFor();

    await openGlobalTables();
    const rail = page.locator('.global-rail[data-global-tab=tables]');
    assert(await rail.isVisible());
    assert.equal(await rail.getAttribute('aria-current'), 'page');
    assert.equal(await page.evaluate(() => location.hash), '#/global/tables');
    // Only tables: no uploads switch, Type filter, Story chips or metrics/finance types. Topic tags
    // are shared with businesses; there is no Department filter.
    assert.equal(await page.locator('[data-data-mode], #data-purpose-filter').count(), 0);
    assert.equal(await page.locator('#data-department').count(), 0, 'no Department filter');
    await page.locator('.data-item').filter({hasText: 'Retired vendor list'}).waitFor();
    assert.equal(await page.locator('.data-item').filter({hasText: 'Widget CRM'}).count(), 0, 'Business tables never appear in Global');
    await page.screenshot({path: path.join(out, 'desktop-global-list.png'), animations: 'disabled'});

    await page.locator('#data-new').click();
    assert.equal(await page.locator('.modal #data-purpose').count(), 0, 'No purpose picker');
    assert.deepEqual(await page.locator('.modal [data-tags]').evaluateAll(nodes => nodes.map(node => node.dataset.tags)), ['topics'], 'Global keeps the Topics tag editor');
    await page.locator('#data-title').fill('Global playbook');
    await page.locator('[data-tags=topics]').fill('Launch, Ops, Launch');
    await page.locator('#data-column-add').click();
    const amount = page.locator('.data-column').last();
    await amount.locator('[data-field=id]').fill('amount');
    await amount.locator('[data-field=name]').fill('Amount');
    await amount.locator('select').selectOption('number');
    await save();
    await page.locator('.data-heading h1').filter({hasText: 'Global playbook'}).waitFor();
    const tableId = await page.evaluate(() => _data.selected);
    const stored = (await globalCall('tables_get', {table_id: tableId})).table;
    assert.equal(stored.business_id ?? null, null);
    assert(!('purpose' in stored), 'The retained backend returns no purpose');
    assert.deepEqual(stored.tags.topics, ['Launch', 'Ops'], 'Tags are saved, trimmed and de-duplicated');
    assert.deepEqual(stored.tags.campaigns ?? [], []); assert.deepEqual(stored.tags.departments ?? [], []);
    const created = tableCalls().find(request => request.path.endsWith('/tables_create') && JSON.parse(request.body).title === 'Global playbook');
    assert.equal(JSON.parse(created.body).scope, 'global');
    assert(!('purpose' in JSON.parse(created.body)), 'A create never sends a purpose');
    assert.equal(await page.locator('#data-stories button, #metrics-columns, #metrics-readings, [data-linked-table], .data-download').count(), 0, 'No Story, metrics, finance or upload controls in Global');
    assert.equal(await page.evaluate(() => _data.scope + ':' + _data.biz), 'global:null');
    console.log('Created a Global table');

    // Tags round-trip through the editor and an update.
    await page.locator('#data-edit-table').click();
    assert.deepEqual(await page.locator('.modal [data-tags]').evaluateAll(nodes => nodes.map(node => node.value)), ['Launch, Ops']);
    assert.equal(await page.locator('.modal #data-purpose').count(), 0);
    await page.screenshot({path: path.join(out, 'desktop-global-editor-tags.png'), animations: 'disabled'});
    await page.locator('[data-tags=topics]').fill('Launch, Ops, Q4');
    await save();
    await page.waitForFunction(() => _data.table?.tags.topics.length === 3);
    assert.deepEqual((await globalCall('tables_get', {table_id: tableId})).table.tags.topics, ['Launch', 'Ops', 'Q4']);
    const updated = tableCalls().filter(request => request.path.endsWith('/tables_update')).map(request => JSON.parse(request.body)).find(body => body.table_id === tableId);
    assert.equal(updated.scope, 'global');
    assert.deepEqual(updated.changes.tags.topics, ['Launch', 'Ops', 'Q4']);

    // Rows: an explicit batch, a retried save reusing its idempotency key, and drafts that survive navigation.
    await page.locator('#data-row-new').click();
    await editCell('name', 'Launch <checklist>');
    await editCell('amount', '42');
    const attempts = [];
    const failOnce = async route => {
      attempts.push(route.request().postDataJSON());
      if (attempts.length === 1) return route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({status: 'error', error: {message: 'Temporary test failure'}})});
      return route.continue();
    };
    await page.route('**/operations/tables_rows_write', failOnce);
    await page.locator('#data-save').click();
    await page.locator('#data-error').waitFor({state: 'visible'});
    await saveRows();
    await page.unroute('**/operations/tables_rows_write', failOnce);
    assert.equal(attempts.length, 2);
    assert.equal(attempts[0].idempotency_key, attempts[1].idempotency_key, 'Retry must reuse its idempotency key');
    assert(attempts.every(body => body.scope === 'global'), 'Row writes carry the explicit Global scope');
    await rowCount(1);
    assert((await page.locator('.data-grid tbody').textContent()).includes('Launch <checklist>'));
    assert.equal((await globalCall('tables_rows_list', {table_id: tableId})).items[0].cells.amount, 42);

    await editCell('name', 'Unsaved Global draft');
    assert.deepEqual(await page.evaluate(() => [..._dataDrafts.keys()].map(key => JSON.parse(key).slice(0, 2))), [['global', null]]);
    await page.evaluate(() => { location.hash = '/global/costs'; });
    await page.waitForFunction(() => state.scope === 'global' && state.globalTab === 'costs');
    await openGlobalTables();
    await page.locator('.data-item').filter({hasText: 'Global playbook'}).click();
    assert.equal(await page.locator('[data-cell-input=name]').inputValue(), 'Unsaved Global draft', 'The draft survives leaving the page');

    // The same table ID space is separate: business navigation neither shows nor saves the Global draft.
    await openBusinessTables();
    assert.equal(await page.locator('.data-item').filter({hasText: 'Global playbook'}).count(), 0);
    assert.equal(await page.locator('.data-item').filter({hasText: 'Retired vendor list'}).count(), 0);
    await page.locator('.data-item').filter({hasText: 'Widget CRM'}).click();
    await page.locator('.data-heading h1').filter({hasText: 'Widget CRM'}).waitFor();
    assert(await page.locator('#data-draft-actions').isHidden(), 'A Global draft never follows the user into a business');
    assert.equal(await page.evaluate(() => _dataDrafts.size), 1, 'Only the Global draft exists');
    await openGlobalTables();
    await page.locator('.data-item').filter({hasText: 'Global playbook'}).click();
    assert.equal(await page.locator('[data-cell-input=name]').inputValue(), 'Unsaved Global draft');
    await page.locator('#data-discard').click();
    await page.locator('.data-cell').filter({hasText: 'Launch <checklist>'}).waitFor();

    // A slow Global detail response cannot paint over the business view opened meanwhile.
    let release, arrived, heldOnce = false;
    const held = new Promise(resolve => { release = resolve; });
    const started = new Promise(resolve => { arrived = resolve; });
    const delayed = async route => {
      if (heldOnce || route.request().postDataJSON().scope !== 'global') return route.continue();
      heldOnce = true;
      const response = await route.fetch(); arrived(); await held; await route.fulfill({response});
    };
    await page.route('**/operations/tables_rows_list', delayed);
    await page.evaluate(() => { dataOpenTable(_data.selected); });
    await started;
    await openBusinessTables();
    await page.locator('.data-item').filter({hasText: 'Widget CRM'}).click();
    await page.locator('.data-heading h1').filter({hasText: 'Widget CRM'}).waitFor();
    release();
    await new Promise(resolve => setTimeout(resolve, 400));
    await page.unroute('**/operations/tables_rows_list', delayed);
    assert.equal(await page.locator('.data-heading h1').textContent(), 'Widget CRM');
    assert(!(await page.locator('#data-detail').textContent()).includes('Launch <checklist>'), 'Stale Global rows must not paint into a business');
    assert.equal(await page.evaluate(() => _data.scope), 'business');
    await openGlobalTables();
    await page.locator('.data-item').filter({hasText: 'Global playbook'}).click();
    await rowCount(1);

    // An open editor keeps the scope it opened in: navigating away neither retargets its save nor changes its payload.
    const created_by_title = title => tableCalls().filter(request => request.path.endsWith('/tables_create')).map(request => ({path: request.path, body: JSON.parse(request.body)})).find(call => call.body.title === title);
    const listTitles = (bizId, query) => page.evaluate(async ([target, text]) => (await dataCall(target, 'tables_list', {query: text, limit: 50})).items.map(item => item.title), [bizId, query]);
    await page.locator('#data-new').click();
    await page.locator('#data-title').fill('Retained Global editor');
    await page.locator('[data-tags=topics]').fill('held');
    await page.evaluate(slug => selectBiz(slug), details.business_slug);
    await page.waitForFunction(() => state.scope === 'business');
    const bizId = await page.evaluate(() => state.biz);
    assert.equal(await page.locator('.modal').count(), 1, 'The open Global editor outlives the navigation');
    assert.equal(await page.locator('.modal #data-purpose').count(), 0);
    await save();
    const retainedGlobal = created_by_title('Retained Global editor');
    assert.equal(retainedGlobal.path, '/api/root/operations/tables_create', 'A Global editor still saves to Global from a business page');
    assert.equal(retainedGlobal.body.scope, 'global');
    assert.deepEqual(retainedGlobal.body.tags.topics, ['held']);
    assert(!('purpose' in retainedGlobal.body), 'No business purpose leaks into a Global payload');
    assert.deepEqual(await listTitles(null, 'Retained'), ['Retained Global editor']);
    assert.deepEqual(await listTitles(bizId, 'Retained'), [], 'The Global table is not created in the business the page shows');
    assert.equal(await page.evaluate(() => _data.scope), 'business', 'Saving a retained editor does not repaint the other scope');

    await openBusinessTables();
    await page.locator('#data-new').click();
    assert.equal(await page.locator('.modal #data-purpose').count(), 0);
    await page.locator('#data-title').fill('Retained business editor');
    await page.locator('[data-tags=topics]').fill('kept');
    await page.evaluate(() => { location.hash = '/global/tables'; });
    await page.waitForFunction(() => state.scope === 'global' && state.globalTab === 'tables' && document.querySelector('#data-new') && !document.querySelector('#data-new').disabled);
    assert.equal(await page.locator('.modal').count(), 1, 'The open business editor outlives the navigation');
    assert.equal(await page.locator('.modal #data-purpose').count(), 0, 'It is still the business editor');
    await save();
    const retainedBusiness = created_by_title('Retained business editor');
    assert.equal(retainedBusiness.path, `/api/biz/${encodeURIComponent(bizId)}/operations/tables_create`, 'A business editor still saves to its business from Global');
    assert(!('scope' in retainedBusiness.body));
    assert(!('purpose' in retainedBusiness.body), 'No purpose in a business payload');
    assert.deepEqual(retainedBusiness.body.tags.topics, ['kept']);
    assert.deepEqual(await listTitles(bizId, 'Retained'), ['Retained business editor']);
    assert.deepEqual(await listTitles(null, 'Retained'), ['Retained Global editor'], 'The business table is not created in Global');
    assert.equal(await page.locator('.data-item').filter({hasText: 'Retained business editor'}).count(), 0, 'The Global list never shows the business table');
    assert.equal(await page.evaluate(() => _data.scope + ':' + _data.selected), 'global:null');
    await page.locator('.data-item').filter({hasText: 'Global playbook'}).click();
    await rowCount(1);

    // The owner archives and restores directly; nothing is applied until the service answers.
    await page.locator('#data-edit-table').click();
    await page.locator('#data-table-archived').check();
    await save();
    await page.waitForFunction(() => _data.table?.archived === true);
    assert(await page.locator('#data-row-new').isDisabled(), 'Archived rows are read-only');
    await page.locator('#data-edit-table').click();
    await page.locator('#data-table-archived').uncheck();
    await save();
    await page.waitForFunction(() => _data.table?.archived === false);

    // An agent's archive request is a Signoff the owner approves; the table changes only after approval.
    assert.equal((await globalCall('tables_get', {table_id: details.global_table_id})).table.archived, false);
    await page.evaluate(() => { openTasks(true); tSetView('signoffs'); });
    const row = page.locator(`#tbody .sg-row[data-id="${details.global_table_archive_signoff_id}"]`);
    await row.waitFor();
    await row.locator('.sg-rowhead').click();
    await row.locator('[data-sg="approve"]:visible:enabled').waitFor();
    await page.screenshot({path: path.join(out, 'desktop-global-archive-signoff.png'), animations: 'disabled'});
    assert.equal((await globalCall('tables_get', {table_id: details.global_table_id})).table.archived, false, 'Nothing applies before approval');
    await row.locator('[data-sg="approve"]:visible').click();
    await row.waitFor({state: 'detached'});
    for (let n = 0; n < 40 && !(await globalCall('tables_get', {table_id: details.global_table_id})).table.archived; n++) await new Promise(resolve => setTimeout(resolve, 250));
    assert.equal((await globalCall('tables_get', {table_id: details.global_table_id})).table.archived, true);
    await page.evaluate(() => { openTasks(false); });
    await page.locator('#data-refresh').click();
    await page.locator('.data-item').filter({hasText: 'Retired vendor list'}).waitFor({state: 'detached'});
    await page.locator('#data-archived').check();
    await page.locator('.data-item').filter({hasText: 'Retired vendor list'}).waitFor();
    await page.locator('#data-archived').uncheck();

    // History is a per-mutation Git publication under the Global path.
    await waitForGit(tableId);
    const listed = execFileSync('git', ['-C', details.root, 'ls-tree', '-r', '--name-only', 'refs/heads/qorqut-tables'], {encoding: 'utf8'}).split('\n');
    assert(listed.includes(`global/tables/${tableId}.json`), listed.join(','));
    assert(!listed.some(name => name.startsWith('global/') && !name.startsWith('global/tables/')));
    const published = JSON.parse(execFileSync('git', ['-C', details.root, 'show', `refs/heads/qorqut-tables:global/tables/${tableId}.json`], {encoding: 'utf8'}));
    assert.equal(published.table.title, 'Global playbook');
    assert.equal(published.rows.length, 1);

    // Transient toasts and the Processes tray sit bottom-left over the pinned Settings button, so the reachability
    // check covers the navigation cluster that includes the new Tables button; the toasts are also waited out.
    await page.waitForFunction(() => !document.querySelector('#toasts').children.length, null, {timeout: 30000});
    for (const [name, width, height] of [['desktop', 1440, 1000], ['tablet', 900, 900], ['small-mobile', 320, 700], ['mobile', 390, 844]]) {
      await page.setViewportSize({width, height});
      await page.screenshot({path: path.join(out, name + '-global-table.png'), animations: 'disabled'});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'No horizontal page scroll at ' + width);
      assert(await page.locator('.data-grid-wrap').evaluate((el, minimum) => el.clientWidth > minimum, width < 390 ? 200 : 250));
      let rails;
      for (let attempt = 0; attempt < 10; attempt++) {
        rails = await page.locator('.global-rail:not(.rbottom):visible').evaluateAll(buttons => buttons.map(button => {
          const rect = button.getBoundingClientRect(), top = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
          return button.contains(top) || button.dataset.globalTab + ' covered by ' + (top?.outerHTML || '').slice(0, 160);
        }));
        if (rails.every(value => value === true)) break;
        await new Promise(resolve => setTimeout(resolve, 300));
      }
      assert(rails.length >= 5 && rails.every(value => value === true), 'Global rail buttons stay reachable at ' + width + ': ' + JSON.stringify(rails));
    }
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#data-edit-table').click();
    await page.screenshot({path: path.join(out, 'mobile-global-editor.png'), animations: 'disabled'});
    assert(await page.locator('.modal').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    await page.locator('.modal .mx').click();
    await page.setViewportSize({width: 1440, height: 1000});

    // Every table call made from Global used the root route with the explicit scope, none a business or null path.
    const inGlobal = tableCalls().filter(request => request.path.startsWith('/api/root/operations/'));
    assert(inGlobal.length > 10, 'Global table calls used the root operation route');
    assert(inGlobal.every(request => JSON.parse(request.body).scope === 'global'), 'Root table calls always name scope:global');
    assert(!requests.some(request => /\/biz\/(null|undefined)(\/|$)/.test(request.path)), 'No business path with a null slug');
    assert(tableCalls().filter(request => request.path.startsWith('/api/biz/')).every(request => !('scope' in JSON.parse(request.body))), 'Business calls keep their default routes');
    // The business agent terminal's xterm can throw from its own viewport refresh when it is left within its first frames;
    // that existing race is outside tables, so only that exact error is set aside.
    assert.deepEqual(errors.filter(error => !/reading 'dimensions'[\s\S]*xterm\.js/.test(error)), []);
    console.log('PASS: Global Tables navigation, General-only create/edit with tags, Department filter, retained-editor scope binding, typed rows with batch Save/retry, per-scope drafts, stale-response isolation, owner archive, agent archive Signoff, Git history path, responsive layouts, no /biz/null');
    console.log('Screenshots: ' + out);
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null) await new Promise(resolve => {
      const timer = setTimeout(() => child.kill('SIGTERM'), 10000);
      child.once('exit', () => { clearTimeout(timer); resolve(); }); child.kill('SIGINT');
    });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
