#!/usr/bin/env node
// Real isolated API, SQLite, files and Git. No production data or model calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn, execFileSync} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const core = process.env.BAZARLYK_CORE_ROOT || path.resolve(__dirname, '../../biz-core');
const out = process.env.TABLE_SCREENSHOTS || '/tmp/qorqut-table-screenshots';

(async () => {
  const child = spawn(process.env.BAZARLYK_TEST_PYTHON || path.join(core, '.venv/bin/python'), ['tests/service_browser_fixture.py', '--tables', '--dashboard-root', path.resolve(__dirname, '..')],
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
    const page = await context.newPage(), errors = [];
    page.setDefaultTimeout(20000);
    page.on('pageerror', error => errors.push(error.message));
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
    const storedTable = () => page.evaluate(async () => (await dataCall(state.biz, 'tables_get', {table_id: _data.selected})).table);
    const storedRows = () => page.evaluate(async () => (await dataCall(state.biz, 'tables_rows_list', {table_id: _data.selected})).items);
    const waitForGit = async () => {
      for (let n = 0; n < 40; n++) {
        const saved = await page.evaluate(async () => {
          const history = (await dataCall(state.biz, 'tables_history', {table_id: _data.selected})).items;
          return history.length > 0 && history.every(version => version.git_commit);
        });
        if (saved) return;
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      throw new Error('Git publication did not finish');
    };
    await page.goto(details.url);
    await page.locator('#agentrows').waitFor();
    console.log('Fixture loaded');
    await page.locator('.rail [data-tab=context]').click();
    await page.locator('#ctxrows [data-p=_tables]').click();
    await page.locator('#data-new').click();
    await page.locator('#data-title').fill('Outreach CRM');
    await page.locator('[data-tags=topics]').fill('Autumn pilot');
    for (const column of [
      {id: 'amount', name: 'Amount', type: 'number'},
      {id: 'contacted', name: 'Contacted', type: 'boolean'},
      {id: 'due', name: 'Due', type: 'date'},
      {id: 'constructor', name: 'Constructor', type: 'text'},
      {id: 'details', name: 'Details', type: 'json'}
    ]) {
      await page.locator('#data-column-add').click();
      const row = page.locator('.data-column').last();
      await row.locator('[data-field=id]').fill(column.id);
      await row.locator('[data-field=name]').fill(column.name);
      await row.locator('select').selectOption(column.type);
    }
    await save();
    await page.locator('.data-heading h1').filter({hasText: 'Outreach CRM'}).waitFor();
    assert(await page.locator('.data-item.on strong').evaluate(el =>
      getComputedStyle(el).color === getComputedStyle(document.querySelector('.data-heading h1')).color),
      'Selected table title must retain readable foreground text');
    await page.locator('#data-row-new').click();
    await editCell('name', 'Customer <literal>');
    await editCell('amount', '12.5');
    await page.getByRole('checkbox', {name: 'Contacted', exact: true}).click();
    await editCell('due', '2026-10-01');
    await editCell('details', '{"source":"example"}');
    assert.equal((await storedTable()).row_count, 0, 'Adding and editing must not auto-save');
    const attempts = [];
    const failOnce = async route => {
      attempts.push(route.request().postDataJSON().idempotency_key);
      if (attempts.length === 1) return route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({status: 'error', error: {message: 'Temporary test failure'}})});
      return route.continue();
    };
    await page.route('**/operations/tables_rows_write', failOnce);
    await page.locator('#data-save').click();
    await page.locator('#data-error').waitFor({state: 'visible'});
    assert.equal(await page.locator('[data-cell-input=name]').inputValue(), 'Customer <literal>');
    await saveRows();
    assert.equal(attempts.length, 2);
    assert.equal(attempts[0], attempts[1], 'Retry must reuse its idempotency key');
    await page.unroute('**/operations/tables_rows_write', failOnce);
    await rowCount(1);
    console.log('Created table and first row');
    assert((await page.locator('.data-grid tbody').textContent()).includes('Customer <literal>'));
    assert(!(await page.locator('.data-grid tbody').textContent()).includes('function Object'));
    assert.equal(await page.locator('#data-history, #data-save-status, .data-tags').count(), 0);
    assert(await page.locator('#data-edit-table svg path').count());
    const firstCell = page.locator('.data-cell').first();
    const foreground = await firstCell.evaluate(el => getComputedStyle(el).color);
    await firstCell.hover();
    assert.equal(await firstCell.evaluate(el => getComputedStyle(el).color), foreground, 'Hover must not darken cell text');
    await editCell('name', 'Discard me');
    await page.getByRole('checkbox', {name: 'Contacted', exact: true}).click();
    assert.equal((await storedRows())[0].cells.contacted, true);
    await page.locator('#data-discard').click();
    await page.locator('.data-cell').filter({hasText: 'Customer <literal>'}).waitFor();
    assert.equal((await storedRows())[0].cells.name, 'Customer <literal>');
    await editCell('details', '{broken');
    await page.locator('#data-save').click();
    assert(await page.locator('[data-cell-input=details]').evaluate(el => !el.checkValidity()));
    assert.deepEqual((await storedRows())[0].cells.details, {source: 'example'});
    await page.locator('#data-discard').click();
    await page.locator('.data-cell').filter({hasText: 'Customer <literal>'}).waitFor();
    await editCell('name', 'Customer A');
    await page.getByRole('checkbox', {name: 'Contacted', exact: true}).click();
    await page.locator('[data-data-mode=uploads]').click();
    await page.locator('[data-data-mode=tables]').click();
    await page.locator('.data-item').filter({hasText: 'Outreach CRM'}).click();
    assert.equal(await page.locator('[data-cell-input=name]').inputValue(), 'Customer A');
    assert.equal(await page.getByRole('checkbox', {name: 'Contacted', exact: true}).getAttribute('aria-checked'), 'false');
    let releaseSave;
    const saveHeld = new Promise(resolve => { releaseSave = resolve; });
    const holdSave = async route => { const response = await route.fetch(); await saveHeld; await route.fulfill({response}); };
    await page.route('**/operations/tables_rows_write', holdSave);
    await page.locator('#data-save').click();
    await page.locator('[data-data-mode=uploads]').click();
    await page.locator('[data-data-mode=tables]').click();
    await page.locator('.data-item').filter({hasText: 'Outreach CRM'}).click();
    await page.locator('#data-save').waitFor();
    assert(await page.locator('#data-save').isDisabled());
    releaseSave();
    await page.locator('#data-draft-actions').waitFor({state: 'hidden'});
    await page.unroute('**/operations/tables_rows_write', holdSave);
    await page.waitForFunction(() => _data.rows[0]?.cells.name === 'Customer A' && _data.rows[0]?.cells.contacted === false);
    await page.locator('#data-edit-table').click();
    await page.locator('.data-column').nth(1).locator('[data-field=name]').fill('Budget');
    await save();
    await page.locator('[data-sort=amount]').filter({hasText: 'Budget'}).waitFor();
    const tableId = await page.evaluate(() => _data.selected);
    const storyId = await page.evaluate(async () => (await storyCall(state.biz, 'stories_create', {title: 'Pilot launch', purpose: 'Coordinate outreach'}, true)).story.id);
    await page.evaluate(async({storyId,tableId})=>{
      await storyCall(state.biz,'stories_link_add',{story_id:storyId,resource_type:'data_tables',resource_id:tableId});
    },{storyId,tableId});
    await page.locator('#data-stories').getByRole('button',{name:'Related context',exact:true}).click();
    await page.locator('.modal .context-source').filter({hasText:'Pilot launch'}).waitFor();
    await page.locator('.modal .mx').click();
    await waitForGit();
    await page.screenshot({path: path.join(out, 'desktop-table.png'), animations: 'disabled'});
    const history = await page.evaluate(async () => (await dataCall(state.biz, 'tables_history', {table_id: _data.selected})).items);
    assert.equal(history.length, 4);
    assert(history.every(version => version.git_commit));
    await page.locator('[data-data-mode=uploads]').click();
    await page.locator('#data-new').click();
    const original = Buffer.from('name,amount\r\n"Sample, Inc",25.00\r\n');
    await page.locator('#data-file').setInputFiles({name: 'campaign.csv', mimeType: 'text/csv', buffer: original});
    await page.locator('#data-upload-notes').fill('October campaign export');
    await save();
    await page.locator('.data-download').waitFor();
    console.log('Uploaded original CSV');
    const downloadEvent = page.waitForEvent('download');
    await page.locator('.data-download').click();
    const download = await downloadEvent;
    assert.deepEqual(fs.readFileSync(await download.path()), original);
    const uploadId = await page.evaluate(() => _data.selected);
    // A parsed import uses the same operation as an agent's MCP call.
    await page.evaluate(async ({tableId, uploadId}) => {
      const table = (await dataCall(state.biz, 'tables_get', {table_id: tableId})).table;
      await dataCall(state.biz, 'tables_rows_write', {table_id: tableId, expected_revision: table.revision, idempotency_key: newRequestId(),
        rows: Array.from({length: 51}, (_, n) => ({cells: {name: 'Lead ' + String(n).padStart(2, '0'), amount: n}, source_upload_id: uploadId}))});
      await dataOpenUpload(uploadId);
    }, {tableId, uploadId});
    await page.locator('[data-linked-table]').click();
    await rowCount(50);
    await page.locator('#data-more').click();
    await rowCount(52);
    assert(!(await page.locator('.data-grid tbody').textContent()).includes('function Object'));
    await page.locator('#data-row-query').fill('Lead 00');
    await rowCount(1);
    assert(await page.locator('#data-row-query').evaluate(el => el === document.activeElement));
    await page.locator('#data-row-query').fill('');
    await rowCount(50);
    let release, arrived, heldOnce = false;
    const held = new Promise(resolve => { release = resolve; });
    const started = new Promise(resolve => { arrived = resolve; });
    const delayedRows = async route => {
      if (heldOnce || route.request().postDataJSON().query !== 'Lead') return route.continue();
      heldOnce = true;
      const response = await route.fetch(); arrived(); await held; await route.fulfill({response});
    };
    await page.route('**/operations/tables_rows_list', delayedRows);
    await page.locator('#data-row-query').fill('Lead');
    await started;
    await page.locator('#data-row-query').fill('Lead 01');
    release();
    await page.waitForFunction(() => _data.rows.length === 1 && _data.rows[0].cells.name === 'Lead 01');
    assert.equal(await page.locator('#data-row-query').inputValue(), 'Lead 01');
    await page.unroute('**/operations/tables_rows_list', delayedRows);
    await page.locator('#data-row-query').fill('');
    await rowCount(50);
    await page.locator('[data-sort=amount]').click();
    await page.waitForFunction(() => _data.rows[0]?.cells.amount === 0);
    await page.locator('[data-row-select]').first().check();
    await page.locator('#data-row-delete').click();
    assert.equal((await storedTable()).row_count, 52, 'Deletion must wait for Save');
    await page.locator('#data-discard').click();
    await rowCount(50);
    await page.locator('[data-row-select]').first().check();
    await page.locator('#data-row-delete').click();
    await saveRows();
    await page.waitForFunction(() => _data.table?.row_count === 51);
    for (const [name, width, height] of [['desktop', 1440, 1000], ['tablet', 900, 900], ['small-mobile', 320, 700], ['mobile', 390, 844]]) {
      await page.setViewportSize({width, height});
      await page.screenshot({path: path.join(out, name + '-table.png'), animations: 'disabled'});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      assert(await page.locator('.data-grid-wrap').evaluate((el, minimum) => el.clientWidth > minimum, width < 390 ? 200 : 250));
      for (const selector of ['#data-new', '#data-refresh']) {
        assert(await page.locator(selector).evaluate(button => {
          const rect = button.getBoundingClientRect();
          return button.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
        }), 'Table toolbar controls must not sit under the task handle');
      }
    }
    await page.locator('#data-edit-table').click();
    await page.screenshot({path: path.join(out, 'mobile-editor.png'), animations: 'disabled'});
    assert(await page.locator('.modal').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    await page.locator('#data-table-archived').check();
    await save();
    await page.waitForFunction(() => _data.table?.archived === true);
    assert(await page.locator('#data-row-new').isDisabled());
    await page.locator('#data-back').click();
    await page.locator('#data-archived').check();
    await page.locator('.data-item').filter({hasText: 'Outreach CRM'}).click();
    await page.locator('#data-edit-table').click();
    await page.locator('#data-table-archived').uncheck();
    await save();
    await waitForGit();
    assert.equal(execFileSync('git', ['-C', details.root, 'rev-list', '--count', 'refs/heads/qorqut-tables'], {encoding: 'utf8'}).trim(), '8');
    await page.reload();
    await page.locator('#data-items .data-item').waitFor();
    await page.locator('#data-items .data-item').click();
    await page.waitForFunction(() => _data.table?.row_count === 51);
    // A concurrent agent update must not be silently overwritten by a UI draft.
    await editCell('name', 'Unsaved local change');
    await page.evaluate(async () => {
      await dataCall(state.biz, 'tables_rows_write', {table_id: _data.selected, expected_revision: _data.table.revision, idempotency_key: newRequestId(),
        rows: [{id: _data.rows[0].id, cells: {name: 'Agent update'}}]});
    });
    await page.locator('#data-save').click();
    await page.locator('#data-error').waitFor({state: 'visible'});
    assert.equal(await page.locator('[data-cell-input=name]').inputValue(), 'Unsaved local change');
    assert.equal((await storedRows())[0].cells.name, 'Agent update');
    await page.locator('#data-discard').click();
    await page.locator('.data-cell').filter({hasText: 'Agent update'}).waitFor();
    // Retired domain tabs: no Marketing metrics, Accounting ledger or Department filter in the generic Tables view.
    await page.setViewportSize({width: 1440, height: 1000});
    assert.equal(await page.locator('#data-department, [data-tags=departments], [data-tags=campaigns], #metrics-columns').count(), 0);
    assert.equal(await page.locator('#data-purpose-filter, #data-purpose').count(), 0, 'the retired Type filter is gone');
    assert.deepEqual(errors, []);
    console.log('PASS: typed inline cells, boolean toggles, explicit batch Save/Discard, validation, retry, navigation during save, revision conflict, table/column CRUD, shared related context, upload/download, provenance, search/sort/pagination, archive, reload, Git, desktop/tablet/mobile');
    console.log('Screenshots: ' + out);
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null) await new Promise(resolve => {
      const timer = setTimeout(() => child.kill('SIGTERM'), 10000);
      child.once('exit', () => { clearTimeout(timer); resolve(); }); child.kill('SIGINT');
    });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
