'use strict';
// The reminder link editor names a record by its type and id; a reminder saved without a change to its link keeps the stored reference.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname, '..');
const PROJECT = '7c9e6679-7425-40de-944b-e07fc1f90ae7';

async function fixture(run) {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="host"></main>');
    await page.evaluate(project => {
      window.writes = [];
      window.state = {session: {principal_id: 'owner'}, projects: [{id: project, slug: 'acme', name: 'Acme'}]};
      window.esc = value => String(value ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
      window.coreData = value => value;
      window.newRequestId = (() => { let n = 0; return () => `key-${++n}`; })();
      window.loadSession = async () => ({principal_id: 'owner', principal_kind: 'human', is_owner: true});
      window.uiFailure = () => 'failed';
      window.toast = () => {};
      window.post = async (url, args) => { if (/reminders_(create|update)$/.test(url)) writes.push({url, args: {...args}}); return {reminder: {id: 'r1', text: args.text || 'x', revision: 2}}; };
      window.session = {setTitle() {}, setBeforeLeave() {}, onDispose() {}, isCurrent: () => true};
    }, PROJECT);
    await page.addScriptTag({path: path.join(root, 'js/reminders.js')});
    await run(page);
  } finally { await browser.close(); }
}

const open = (page, reminder) => page.evaluate(reminder => {
  document.getElementById('host').replaceChildren();
  window.editor = remindersEditor(reminder, {host: document.getElementById('host'), session, onCancel() {}, onSaved() {}});
}, reminder);

test('a typed link is sent as its type and id, with no project to choose', {skip: !available}, async () => {
  await fixture(async page => {
    await open(page, null);
    assert.equal(await page.locator('select[name="linkscope"]').count(), 0);
    assert.deepEqual(await page.locator('.rem-form select').evaluateAll(list => list.map(select => select.name)), ['repeat', 'weekday', 'linktype']);
    await page.fill('textarea[name="text"]', 'Check the table');
    await page.fill('input[name="at"]', '2099-01-01T10:00');
    await page.click('summary:has-text("Link to an item")');
    await page.selectOption('select[name="linktype"]', 'table');
    await page.fill('input[name="linkid"]', ' 3f2c4d5e-1a2b-4c3d-8e9f-0a1b2c3d4e5f ');
    await page.click('.rem-editor-save');
    await page.waitForFunction(() => writes.length === 1);
    assert.deepEqual(await page.evaluate(() => writes[0].args.linked_item), {type: 'table', id: '3f2c4d5e-1a2b-4c3d-8e9f-0a1b2c3d4e5f'});
  });
});

test('a reminder saved with its link unchanged keeps the stored reference, and a link can be removed', {skip: !available}, async () => {
  await fixture(async page => {
    const reminder = {id: 'r1', revision: 1, text: 'Call back', at: '2099-01-01T10:00:00+00:00', time_critical: false, recipient_id: 'owner',
      linked_item: {type: 'task', id: 'ab3cd', project_id: PROJECT}};
    await open(page, reminder);
    await page.fill('textarea[name="text"]', 'Call back today');
    await page.click('.rem-editor-save');
    await page.waitForFunction(() => writes.length === 1);
    const args = await page.evaluate(() => writes[0].args);
    assert.equal(args.text, 'Call back today');
    assert.equal('linked_item' in args, false);
    await open(page, reminder);
    await page.check('input[name="unlink"]');
    await page.click('.rem-editor-save');
    await page.waitForFunction(() => writes.length === 2);
    assert.equal(await page.evaluate(() => writes[1].args.linked_item), null);
  });
});
