'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname, '..');

async function fixture(run) {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="chat" style="height:400px;width:400px"></main>');
    for (const file of ['app.css', 'css/chat-view.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;'}[c]));
      window.state = {projects: [{id: 'acme', slug: 'acme'}]};
      window.coreData = response => response.data;
      window.pendingReads = [];
      window.opened = [];
      window.post = (url, args) => new Promise(resolve => pendingReads.push({url, args, resolve}));
    });
    await page.addScriptTag({path: path.join(root, 'js/safe-links.js')});
    await page.addScriptTag({path: path.join(root, 'js/chat-view.js')});
    await page.evaluate(() => {
      window.view = mountChatView(document.querySelector('#chat'), {onTask: ref => opened.push(ref)});
      window.reference = (id, type = 'task', project_id = null) => ({type, id, project_id, label: 'Saved ' + id, state: 'working'});
      window.entry = (ref, typed = false) => ({key: 'record', speaker: 'reference', referenceOnly: true,
        typedReferences: typed ? [ref] : [], references: [{key: 'link', label: ref.label, value: ref}]});
      window.render = record => view.render({entries: [record], composer: {hidden: true}});
      window.finishRead = (index, title) => {
        const read = pendingReads[index], type = read.args.table_id ? 'table' : 'task';
        read.resolve({data: {[type]: {id: read.args.table_id || read.args.task_id, title}}});
      };
    });
    await run(page);
  } finally { await browser.close(); }
}

test('a task supplied as both reference types renders one labelled, working link', {skip: !available}, async () => {
  await fixture(async page => {
    await page.evaluate(() => render(entry(reference('outreach'), true)));
    const buttons = page.locator('.gchat-log button');
    assert.equal(await buttons.count(), 1);
    assert.equal(await buttons.innerText(), 'Saved outreach');
    await page.waitForFunction(() => pendingReads.length === 1);
    await page.evaluate(() => finishRead(0, 'Influencers and marketing'));
    await page.waitForFunction(() => document.querySelector('.gchat-log button').textContent === 'Influencers and marketing');
    await buttons.click();
    assert.deepEqual(await page.evaluate(() => opened.map(ref => ref.id)), ['outreach']);
    assert.equal(await buttons.count(), 1);
  });
});

test('ordinary task and table references paint immediately and ignore stale reads after replacement', {skip: !available}, async () => {
  await fixture(async page => {
    const button = page.locator('.gchat-log button');
    await page.evaluate(() => render(entry(reference('old-task'))));
    assert.equal(await button.innerText(), 'Saved old-task');
    assert.match(await button.getAttribute('class'), /gchat-reference-block/);
    await page.waitForFunction(() => pendingReads.length === 1);
    await page.evaluate(() => render(entry(reference('new-table', 'table'))));
    assert.equal(await button.innerText(), 'Saved new-table');
    await page.waitForFunction(() => pendingReads.length === 2);
    await page.evaluate(() => { finishRead(1, 'Creator CRM'); finishRead(0, 'Obsolete task'); });
    await page.waitForFunction(() => document.querySelector('.gchat-log button').textContent === 'Creator CRM');
    assert.equal(await button.count(), 1);
    assert.equal(await button.innerText(), 'Creator CRM');
    await page.evaluate(() => render({...entry(reference('held')), references: [{key: 'link', value: reference('held'), disabled: true}]}));
    assert.equal(await button.innerText(), 'Unavailable reference');
    assert.equal(await button.isDisabled(), true);
  });
});

test('task references with the same ID in different projects remain distinct', {skip: !available}, async () => {
  await fixture(async page => {
    await page.evaluate(() => render({...entry(reference('same', 'task', 'acme')), typedReferences: [reference('same')]}));
    assert.equal(await page.locator('.gchat-log button').count(), 2);
    assert.equal(await page.locator('.gchat-log button:empty').count(), 0);
    await page.waitForFunction(() => pendingReads.length === 2);
    assert.deepEqual(await page.evaluate(() => pendingReads.map(read => read.url).sort()),
      ['/project/acme/operations/tasks_get', '/root/operations/tasks_get']);
  });
});

test('a retired agent reference is not drawn; the other references keep their order and mounted components', {skip: !available}, async () => {
  await fixture(async page => {
    await page.evaluate(() => {
      window.a = reference('a', 'agent'); window.b = reference('b', 'agent');
      window.task = reference('task'); window.table = reference('table', 'table');
      window.browserRef = {...reference('browser', 'browser'), viewer_url: 'https://example.com/'};
      window.draw = refs => render({key: 'record', speaker: 'agent', text: 'Synthetic references', typedReferences: refs});
      draw([a, task, table, b, browserRef]);
      const host = document.querySelector('.gchat-typed');
      window.saved = {attachment: host.children[0], browser: host.querySelector('iframe')};
    });
    assert.deepEqual(await page.locator('.gchat-typed > *').evaluateAll(els => els.map(el => el.className)), ['gchat-attachment', 'gchat-browser-slot']);
    assert.deepEqual(await page.locator('.gchat-typed > .gchat-attachment > *').allTextContents(), ['Saved task', 'Saved table']);
    assert.equal(await page.locator('.gchat-typed').evaluate(host => /Saved [ab]\b/.test(host.textContent)), false);
    await page.evaluate(() => draw([b, task, table, a, browserRef]));
    assert.equal(await page.evaluate(() => saved.attachment === document.querySelector('.gchat-attachment') && saved.browser === document.querySelector('iframe')), true);
    // A message that carried only agent references shows no reference area at all.
    await page.evaluate(() => draw([a, b]));
    assert.equal(await page.locator('.gchat-typed').count(), 0);
  });
});
