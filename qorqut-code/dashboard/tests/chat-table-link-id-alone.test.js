'use strict';
// A table link with its id alone is read from the root route, which finds the table's project from the id; a stated project
// keeps its own route.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname, '..');
const TABLE = '3f2c4d5e-1a2b-4c3d-8e9f-0a1b2c3d4e5f', PROJECT = '7c9e6679-7425-40de-944b-e07fc1f90ae7';

async function fixture(run) {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage();
    await page.route('http://dashboard.test/', route => route.fulfill({contentType: 'text/html', body: '<main id="chat" style="height:400px;width:400px"></main>'}));
    await page.goto('http://dashboard.test/');
    for (const file of ['app.css', 'css/chat-view.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.evaluate(project => {
      window.esc = value => String(value ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;'}[c]));
      window.state = {projects: [{id: project, slug: 'acme'}]};
      window.coreData = response => response.data;
      window.pendingReads = [];
      window.opened = [];
      window.post = (url, args) => new Promise(resolve => pendingReads.push({url, args, resolve}));
    }, PROJECT);
    await page.addScriptTag({path: path.join(root, 'js/safe-links.js')});
    await page.addScriptTag({path: path.join(root, 'js/chat-view.js')});
    await page.evaluate(() => {
      window.view = mountChatView(document.querySelector('#chat'), {onReference: ref => opened.push(ref)});
      window.render = (value, key = 'm') => view.render({entries: [{key, speaker: 'agent', text: 'A table', references: [{key: 'r', label: 'Table', value}]}],
        composer: {hidden: true}});
    });
    await run(page);
  } finally { await browser.close(); }
}

test('a table link with the id alone becomes a reference without a project', {skip: !available}, async () => {
  await fixture(async page => {
    const refs = await page.evaluate(([table, project]) => {
      const body = document.createElement('div');
      body.innerHTML = `<a class="safe-link" href="/tables/view?table_id=${table}">Alone</a><a class="safe-link" href="/tables/view?table_id=${table}&project_id=${project}">Stated</a>`;
      return chatViewExtractTableLinks(body).map(ref => ref.value);
    }, [TABLE, PROJECT]);
    assert.deepEqual(refs, [{type: 'table', id: TABLE}, {type: 'table', id: TABLE, project_id: PROJECT}]);
  });
});

test('an id-alone table reference is read by its id and opens with the project it reports', {skip: !available}, async () => {
  await fixture(async page => {
    await page.evaluate(table => render({type: 'table', id: table}), TABLE);
    await page.waitForFunction(() => pendingReads.length === 1);
    assert.deepEqual(await page.evaluate(() => pendingReads.map(read => [read.url, read.args])), [['/root/operations/tables_get', {table_id: TABLE}]]);
    const button = page.locator('.gchat-log .gchat-reference');
    await page.evaluate(([table, project]) => pendingReads[0].resolve({data: {table: {id: table, title: 'Creator CRM', project_id: project}}}), [TABLE, PROJECT]);
    await page.waitForFunction(() => document.querySelector('.gchat-log .gchat-reference').textContent === 'Creator CRM');
    await button.click();
    assert.deepEqual(await page.evaluate(() => opened), [{type: 'table', id: TABLE, project_id: PROJECT}]);
  });
});

test('an id-alone table reference in Global opens as Global and one that is not found opens nothing', {skip: !available}, async () => {
  await fixture(async page => {
    await page.evaluate(table => render({type: 'table', id: table}), TABLE);
    await page.waitForFunction(() => pendingReads.length === 1);
    await page.evaluate(table => pendingReads[0].resolve({data: {table: {id: table, title: 'General', project_id: null}}}), TABLE);
    await page.waitForFunction(() => document.querySelector('.gchat-log .gchat-reference').textContent === 'General');
    await page.locator('.gchat-log .gchat-reference').click();
    assert.deepEqual(await page.evaluate(() => opened), [{type: 'table', id: TABLE, project_id: null}]);
    await page.evaluate(() => { opened.length = 0; render({type: 'table', id: '9f2c4d5e-1a2b-4c3d-8e9f-0a1b2c3d4e5f'}, 'other'); });
    await page.waitForFunction(() => pendingReads.length === 2);
    await page.evaluate(() => pendingReads[1].resolve({data: {}}));
    await page.waitForFunction(() => document.querySelector('.gchat-log .gchat-reference').textContent === 'Unavailable reference');
    assert.equal(await page.locator('.gchat-log .gchat-reference').isDisabled(), true);
  });
});

test('a table reference that states its project keeps that project route', {skip: !available}, async () => {
  await fixture(async page => {
    await page.evaluate(([table, project]) => render({type: 'table', id: table, project_id: project}), [TABLE, PROJECT]);
    await page.waitForFunction(() => pendingReads.length === 1);
    assert.deepEqual(await page.evaluate(() => pendingReads.map(read => [read.url, read.args])), [['/project/acme/operations/tables_get', {table_id: TABLE}]]);
  });
});
