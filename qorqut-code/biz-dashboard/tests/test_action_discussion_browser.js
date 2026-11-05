// Action discussion on the shared Chat view, isolated: stubbed operations and fixed synthetic messages.
//   PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_action_discussion_browser.js
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const SCRIPTS = ['core', 'safe-links', 'san-juan-time', 'chat-view', 'task-state', 'tags', 'work-groups', 'tasks', 'actions'];
const html = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/safe-links.css"><link rel="stylesheet" href="/css/chat-view.css"><link rel="stylesheet" href="/css/actions.css">
<div id="one" style="width:420px;padding:12px"></div><div id="two" style="width:420px;padding:12px"></div><button id="wsbtn" hidden></button>
${SCRIPTS.map(name => `<script src="/js/${name}.js"></script>`).join('')}`;

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  try {
    const page = await browser.newPage({viewport: {width: 1000, height: 800}}), errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.hostname !== 'work.test') return route.abort();
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: html});
      const file = path.join(root, url.pathname);
      return fs.existsSync(file) ? route.fulfill({path: file}) : route.fulfill({status: 404, body: ''});
    });
    await page.goto('http://work.test/');
    await page.evaluate(() => {
      state.session = {principal_id: 'owner-1', display_name: 'Owner', is_owner: true};
      window.threadCalls = []; window.holds = []; window.holdAction = null;
      const json = data => new Response(JSON.stringify({status: 'ok', data}), {headers: {'Content-Type': 'application/json'}});
      const people = [{id: 'owner-1', display_name: 'Owner'}, {id: 'agent-m', display_name: 'Marketer'}];
      const msg = (id, author, recorder, body, at) => ({id, kind: 'comment', author_id: author, recorder_id: recorder, body, created_at: at});
      window.fetch = async (url, options = {}) => {
        const op = String(url).split('/operations/')[1], args = JSON.parse(options.body || '{}');
        if (op !== 'actions_thread_get') return json({});
        if (args.action_id === 'empty') return json({participants: people, messages: [], next_cursor: null});
        threadCalls.push(args);
        if (holdAction === args.action_id) await new Promise(resolve => holds.push(resolve));
        if (args.cursor === 'older-1') return json({participants: people, messages: [msg('m1', 'agent-m', 'agent-m', 'First note.', '2030-01-02T08:00:00Z'), msg('m2', 'agent-m', 'agent-m', 'Second note.', '2030-01-02T09:00:00Z')], next_cursor: null});
        return json({participants: people, messages: [msg('m3', 'agent-m', 'agent-m', 'Ran the send.', '2030-01-02T10:00:00Z'), msg('m4', 'owner-1', 'agent-m', 'Owner agreed by phone.', '2030-01-02T11:00:00Z'),
          {id: 'm5', kind: 'system', author_id: null, recorder_id: null, body: 'Action corrected.', created_at: '2030-01-02T12:00:00Z'}], next_cursor: args.action_id === 'a1' ? 'older-1' : null});
      };
      window.A = id => ({action_id: id, text: 'Sent the launch email', revision: 2, can_change: true, tags: [], withdrawn: false});
      acDiscussionMount(document.querySelector('#one'), A('a1'));
    });
    const one = page.locator('#one');
    await one.getByText('Ran the send.').waitFor();

    // Read-only: no visible composer (a hidden form may exist), no discussion-specific message renderer; revisions and follow stay outside the view.
    assert.equal(await one.locator('textarea:visible, [contenteditable]:visible, .gchat-composer:visible, .gchat-send:visible').count(), 0, 'no visible composer');
    assert.equal(await one.locator('.gchat-send').evaluateAll(b => b.filter(x => !x.disabled && x.closest('.gchat-composer')?.offsetParent).length), 0, 'no dispatchable send');
    assert.equal(await one.locator('.ac-message, .ac-thread').count(), 0, 'no standalone message DOM');
    assert.equal(await one.locator('.ac-revisions, .ac-follow').count(), 2);
    assert.equal(await one.locator('.chat-view .ac-revisions, .chat-view .ac-follow').count(), 0);
    assert.match(await one.locator('.gchat-msg', {hasText: 'Owner agreed by phone.'}).innerText(), /Owner · recorded by Marketer/);
    assert.equal(await one.locator('.gchat-msg').count(), 3);
    assert.equal(await one.locator('.gchat-msg .gchat-time').count(), 3, 'every message has the shared timestamp');
    assert.equal(await one.locator('.gchat-msg', {hasText: 'Action corrected.'}).locator('.gchat-time').count(), 1);

    // Empty text only for a loaded empty list.
    assert.equal(await one.locator('.gchat-empty:visible').count(), 0, 'a populated discussion shows no empty text');
    await page.evaluate(() => { document.body.insertAdjacentHTML('beforeend', '<div id="three"></div>'); acDiscussionMount(document.querySelector('#three'), A('empty')); });
    await page.locator('#three .gchat-empty:visible').waitFor();
    assert.equal((await page.locator('#three .gchat-empty').innerText()).trim(), 'No discussion yet.');
    await page.evaluate(() => { document.querySelector('#three')._acDiscussion.dispose(); });

    // Exact cursor paging; older history goes in front and preserves order.
    assert.deepEqual(await page.evaluate(() => threadCalls[0]), {action_id: 'a1', limit: 20});
    await one.locator('.gchat-older').click();
    await one.getByText('First note.').waitFor();
    assert.deepEqual(await page.evaluate(() => threadCalls[1]), {action_id: 'a1', limit: 20, cursor: 'older-1'});
    assert.deepEqual(await one.locator('.gchat-msg .gchat-body').allInnerTexts().then(t => t.map(x => x.trim())), ['First note.', 'Second note.', 'Ran the send.', 'Owner agreed by phone.', 'Action corrected.']);
    assert.equal(await one.locator('.gchat-older:visible').count(), 0, 'no further history control after the last page');

    // Stale protection: dispose while the first load is in flight, and again through the list repaint disposal.
    await page.evaluate(() => { holdAction = 'a2'; acDiscussionMount(document.querySelector('#two'), A('a2')); });
    await page.waitForFunction(() => threadCalls.some(c => c.action_id === 'a2'));
    assert.equal(await page.evaluate(() => acDiscussions.size), 2);
    await page.evaluate(() => acDiscussionsDispose());
    assert.equal(await page.evaluate(() => acDiscussions.size), 0, 'list replacement disposes every mounted view');
    await page.evaluate(() => { holdAction = null; holds.splice(0).forEach(release => release()); });
    await page.waitForTimeout(100);
    assert.equal(await page.locator('#two .gchat-msg').count(), 0, 'late response does not paint a disposed view');
    assert.equal(await page.evaluate(() => document.querySelector('#two')._acDiscussion), undefined);

    // A fresh mount after disposal works, and closing/reopening re-lays-out the same view without another load.
    await page.evaluate(() => { document.querySelector('#two').innerHTML = ''; acDiscussionMount(document.querySelector('#two'), A('a3')); });
    await page.locator('#two').getByText('Ran the send.').waitFor();
    const loads = await page.evaluate(() => threadCalls.length);
    await page.evaluate(() => { const handle = document.querySelector('#two')._acDiscussion; handle.layout(); handle.layout(); });
    assert.equal(await page.evaluate(() => threadCalls.length), loads, 'layout never reloads or acknowledges');
    assert.deepEqual(errors, []);
    console.log('Action discussion on the shared Chat view: read-only form omission, attribution, shared timestamps, exact cursor paging, disposal and stale-response protection passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
