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
      window.state = {bizs: [{id: 'project', slug: 'project'}]};
      window.coreData = response => response.data;
      window.pendingReads = [];
      window.opened = [];
      window.post = (url, args) => new Promise(resolve => pendingReads.push({url, args, resolve}));
    });
    await page.addScriptTag({path: path.join(root, 'js/safe-links.js')});
    await page.addScriptTag({path: path.join(root, 'js/chat-view.js')});
    await page.evaluate(() => {
      window.view = mountChatView(document.querySelector('#chat'), {onTask: ref => opened.push(ref)});
      window.reference = (id, type = 'task', business_id = null) => ({type, id, business_id, label: 'Saved ' + id, state: 'working'});
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
    await page.evaluate(() => render({...entry(reference('same', 'task', 'project')), typedReferences: [reference('same')]}));
    assert.equal(await page.locator('.gchat-log button').count(), 2);
    assert.equal(await page.locator('.gchat-log button:empty').count(), 0);
    await page.waitForFunction(() => pendingReads.length === 2);
    assert.deepEqual(await page.evaluate(() => pendingReads.map(read => read.url).sort()),
      ['/biz/project/operations/tasks_get', '/root/operations/tasks_get']);
  });
});

test('agent runs wrap inline and preserve reference order and mounted components', {skip: !available}, async () => {
  await fixture(async page => {
    await page.evaluate(() => {
      window.a = reference('a', 'agent'); window.b = reference('b', 'agent');
      window.c = reference('c', 'agent'); window.task = reference('task');
      window.table = reference('table', 'table');
      window.browserRef = {...reference('browser', 'browser'), viewer_url: 'https://example.com/'};
      window.draw = refs => render({key: 'record', speaker: 'agent', text: 'Synthetic references', typedReferences: refs});
      draw([a, b, task, table, c, browserRef]);
      const host = document.querySelector('.gchat-typed');
      window.saved = {a: host.children[0].children[0], b: host.children[0].children[1],
        firstGroup: host.children[0], attachment: host.children[1], c: host.children[2].children[0],
        browser: host.querySelector('iframe')};
    });
    assert.deepEqual(await page.locator('.gchat-typed > *').evaluateAll(els => els.map(el => el.className)),
      ['gchat-agent-references', 'gchat-attachment', 'gchat-agent-references', 'gchat-browser-slot']);
    assert.equal(await page.locator('[aria-label="Agent references"]').count(), 2);
    assert.equal(await page.evaluate(() => saved.a.getBoundingClientRect().top === saved.b.getBoundingClientRect().top), true);
    assert.deepEqual(await page.locator('.gchat-typed > .gchat-attachment > *').allTextContents(), ['Saved task', 'Saved table']);
    if (process.env.CHAT_REFERENCE_SCREENSHOT_DIR) await page.locator('#chat').screenshot({path: path.join(process.env.CHAT_REFERENCE_SCREENSHOT_DIR, 'normal.png')});
    await page.evaluate(() => draw([a, {...b, label: 'Renamed b'}, task, table, c, browserRef]));
    assert.equal(await page.evaluate(() => saved.firstGroup === document.querySelector('.gchat-agent-references') && saved.b === saved.firstGroup.children[1] && saved.browser === document.querySelector('iframe')), true);
    await page.evaluate(() => draw([b, a, task, table, {...c, available: false, label: 'Secret identity'}, browserRef]));
    assert.deepEqual(await page.locator('.gchat-typed > *').evaluateAll(els => els.map(el => el.textContent.replace(/\s+/g, ' ').trim())),
      ['Saved bSaved a', 'Saved taskSaved table', 'Unavailable reference', 'Saved browser ↗']);
    assert.equal(await page.evaluate(() => {
      const host = document.querySelector('.gchat-typed');
      return host.children[0].children[0] === saved.b && host.children[0].children[1] === saved.a &&
        host.children[1] === saved.attachment && host.children[2].children[0] !== saved.c &&
        saved.browser === host.querySelector('iframe') && !saved.firstGroup.isConnected;
    }), true);
    await page.evaluate(() => draw([b, a, task, table, c, browserRef]));
    assert.equal(await page.locator('.gchat-reference-unavailable').count(), 0);
    await page.evaluate(() => draw([a, b, c, browserRef]));
    assert.equal(await page.locator('.gchat-agent-references').count(), 1);
    assert.equal(await page.locator('.gchat-attachment').count(), 0);
    assert.equal(await page.evaluate(() => !saved.attachment.isConnected && saved.browser === document.querySelector('iframe')), true);
    await page.evaluate(() => {
      document.querySelector('#chat').style.width = '150px';
      draw([a, b, {...c, label: 'A very long synthetic agent name that must truncate without overflow'}, browserRef]);
    });
    const geometry = await page.locator('.gchat-agent-references').evaluate(group => {
      const children = [...group.children].map(el => el.getBoundingClientRect());
      const bounds = group.getBoundingClientRect();
      return {wraps: children.some(rect => rect.top > children[0].top), fits: children.every(rect => rect.right <= bounds.right + 0.5), noOverflow: group.scrollWidth <= group.clientWidth};
    });
    assert.deepEqual(geometry, {wraps: true, fits: true, noOverflow: true});
    if (process.env.CHAT_REFERENCE_SCREENSHOT_DIR) await page.locator('#chat').screenshot({path: path.join(process.env.CHAT_REFERENCE_SCREENSHOT_DIR, 'narrow.png')});
    await page.evaluate(() => draw([]));
    assert.equal(await page.locator('.gchat-typed').count(), 0);
  });
});
