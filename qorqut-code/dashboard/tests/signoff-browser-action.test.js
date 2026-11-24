'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const path = require('node:path');
const fs = require('node:fs');
const vm = require('node:vm');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const browserTest = (name, run) => test(name, {skip: !available && 'Local Playwright Chromium is unavailable'}, run);
const fixture = pathToFileURL(path.join(__dirname, 'fixtures/signoff-browser-action.html')).href;
const AGENT = '3f6c1b9e-2a47-4d1e-9b0c-5e8a7d2c4f10', TAB = 'A1B2C3D4E5F60718293A4B5C6D7E8F90';

// The chip, evaluated from the real attention-queue.js with the dashboard globals stubbed.
const queueSource = fs.readFileSync(path.join(__dirname, '..', 'js', 'attention-queue.js'), 'utf8');
const browserSvg = /browser: '(<svg[^']*<\/svg>)'/.exec(fs.readFileSync(path.join(__dirname, '..', 'js', 'chat-view.js'), 'utf8'))[1];
function chip() {
  const body = queueSource.slice(queueSource.indexOf('  const messageOperations='), queueSource.indexOf('  function paint(host){'));
  const context = {window: {}, esc: s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])),
    T_VIEW_ICON: {}, CHAT_VIEW_ICONS: {browser: browserSvg}, SG_BELL: ''};
  vm.createContext(context);
  vm.runInContext(`(()=>{${body}\nthis.api={signoffIcon,label,button,askKind};})()`, context);
  return context.api;
}

test('a browser action chip wears the browser icon, says Browser and leaves other chips unchanged', () => {
  const q = chip();
  const row = {kind: 'signoff', id: 's1', title: 'Sign in to Mercury'};
  for (const item of [{...row, asks: 'browser_action'}, {...row, input: {kind: 'browser_action'}}]) {
    const icon = q.signoffIcon(item);
    assert.match(icon, /data-signoff-kind="browser"/);
    assert.ok(icon.includes('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/>'), 'the browser icon of Chat\'s tab card');
    assert.equal(q.label(item), 'Browser · Sign in to Mercury');
    assert.match(q.button(item), /aria-label="Browser · Sign in to Mercury"/);
  }
  // A question is still the bold question mark, a request the open circle, a review the clipboard; labels as before.
  const question = {...row, asks: 'question'}, request = {...row, asks: 'request'}, review = {...row, operation_name: 'x'};
  assert.match(q.signoffIcon(question), /data-signoff-kind="question"[^>]*><svg[^>]*><text /);
  assert.match(q.signoffIcon(request), /data-signoff-kind="request"/);
  assert.match(q.signoffIcon(review), /data-signoff-kind="approval"/);
  assert.deepEqual([question, request, review].map(q.label), ['Question · Sign in to Mercury', 'Request · Sign in to Mercury', 'Review · Sign in to Mercury']);
  assert.equal(q.askKind({asks: true}), 'question');
  assert.equal(q.askKind({input: {kind: 'request'}}), 'request');
  assert.equal(q.askKind({input: {kind: 'draft_review'}}), null);
});

browserTest('the chip opens the browser action panel: heading, tab card, note and two answers, no radios', async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable, args: ['--allow-file-access-from-files']});
  try {
    const page = await browser.newPage({viewport: {width: 800, height: 700}}), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(fixture);
    await page.addScriptTag({path: path.join(__dirname, '../js/attention-queue.js')});
    await page.evaluate(() => { const host = document.createElement('div'); host.className = 'focus-signoff-queue'; document.querySelector('.agfocus').prepend(host); attentionQueue.mountSignoffs(host); });
    const chipEl = page.locator('[data-signoff-key="browser"]');
    await chipEl.waitFor();
    assert.equal(await chipEl.getAttribute('aria-label'), 'Browser action Sign in to Mercury');
    assert.equal(await chipEl.locator('.attention-signoff').getAttribute('data-signoff-kind'), 'browser');
    assert.equal(await chipEl.locator('svg').count(), 1);
    await chipEl.click();
    await page.waitForSelector('.sg-row[data-id="browser"] .sg-actions [data-answer]:not([disabled])');
    const panel = await page.evaluate(() => {
      const row = document.querySelector('.sg-row[data-id="browser"]'), card = row.querySelector('.sg-browser-tab button');
      return {heading: row.querySelector('.sg-browser-heading').textContent, explanation: row.querySelector('.sg-rationale').textContent,
        radios: row.querySelectorAll('[role="radio"],[role="radiogroup"],[data-choice]').length,
        card: {text: card.textContent, aria: card.getAttribute('aria-label'), cls: card.className, svg: !!card.querySelector('svg'), height: card.getBoundingClientRect().height,
          agent: card.querySelector('.gchat-tab-agent').textContent, title: card.querySelector('.gchat-tab-title').textContent},
        injected: row.querySelectorAll('.sg-browser-tab img, .sg-browser-tab b').length, pwned: !!window.pwned,
        buttons: [...row.querySelectorAll('.sg-actions button')].map(b => [b.textContent, b.dataset.sg, b.dataset.answer, b.classList.contains('sg-primary-action')]),
        submit: row.querySelectorAll('.sg-actions button').length, note: row.querySelector('textarea.sg-feedback').placeholder, noteLabel: row.querySelector('textarea.sg-feedback').getAttribute('aria-label'),
        asks: row.classList.contains('sg-asks'), overflow: document.documentElement.scrollWidth > innerWidth};
    });
    assert.equal(panel.heading, 'Browser action needed');
    assert.equal(panel.explanation, 'Open the tab and sign in with your Mercury account.');
    assert.equal(panel.radios, 0);
    assert.match(panel.card.cls, /gchat-tab-reference/);
    assert.equal(panel.card.svg, true);
    assert.ok(panel.card.height < 40, 'a compact chip, as in Chat');
    // The page label and the agent's name are untrusted text: shown as text only.
    assert.equal(panel.card.title, '<img src=x onerror=window.pwned=1>Log in | Mercury');
    assert.equal(panel.card.agent, 'Ordubashy <b>');
    assert.equal(panel.injected, 0); assert.equal(panel.pwned, false);
    assert.deepEqual(panel.buttons, [['Could not do it', 'respond', 'not_done', false], ['Done', 'respond', 'done', true]]);
    assert.equal(panel.note, 'Add a note (optional)'); assert.equal(panel.noteLabel, 'Note (optional)');
    assert.equal(panel.asks, true); assert.equal(panel.overflow, false);
    // The card opens exactly that agent's tab.
    await page.click('.sg-row[data-id="browser"] .sg-browser-tab button');
    assert.deepEqual(await page.evaluate(() => opened), [[AGENT, TAB]]);
    // A repaint keeps the same card node (and the typed note).
    await page.fill('.sg-row[data-id="browser"] textarea.sg-feedback', 'Captcha solved');
    const same = await page.evaluate(() => { const card = document.querySelector('.sg-browser-tab button'); sgViews.forEach(v => v.render()); return card === document.querySelector('.sg-browser-tab button'); });
    assert.equal(same, true);
    // Done sends the choice and the note through the ordinary respond request, once.
    await page.click('.sg-row[data-id="browser"] [data-answer="done"]');
    const calls = await page.evaluate(() => testHarness.calls.map(c => ({url: c.url, body: c.body})));
    assert.equal(calls.length, 1);
    assert.match(calls[0].url, /\/respond$/);
    assert.deepEqual([calls[0].body.selected_choices, calls[0].body.notes], [['done'], 'Captcha solved']);
    assert.deepEqual(Object.keys(calls[0].body).sort(), ['expected_status_revision', 'idempotency_key', 'notes', 'payload_hash', 'proposal_revision', 'selected_choices']);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

browserTest('Could not do it answers not_done with the note; a closed or missing tab shows the plain line and still answers', async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable, args: ['--allow-file-access-from-files']});
  try {
    const page = await browser.newPage({viewport: {width: 380, height: 700}}), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(fixture);
    await page.evaluate(() => { document.body.style.width = 'auto'; document.querySelector('.agfocus').style.width = '340px'; });
    const ready = async id => { await page.evaluate(id => openItem(id), id); await page.waitForSelector(`.sg-row[data-id="${id}"] .sg-actions [data-answer]:not([disabled])`); };
    for (const id of ['browsergone', 'browserbare']) {
      await ready(id);
      const state = await page.evaluate(id => { const row = document.querySelector(`.sg-row[data-id="${id}"]`); return {button: row.querySelectorAll('.sg-browser-tab button').length,
        line: row.querySelector('.sg-browser-tab').textContent, radios: row.querySelectorAll('[role="radio"]').length, overflow: [...row.querySelectorAll('*')].some(e => e.getBoundingClientRect().right > document.querySelector('.agfocus').getBoundingClientRect().right + 1)}; }, id);
      assert.deepEqual(state, {button: 0, line: 'This tab is no longer open.', radios: 0, overflow: false}, id);
    }
    await page.fill('.sg-row[data-id="browserbare"] textarea.sg-feedback', 'Page asked for a code I do not have');
    await page.click('.sg-row[data-id="browserbare"] [data-answer="not_done"]');
    const calls = await page.evaluate(() => testHarness.calls.map(c => ({url: c.url, body: c.body})));
    assert.equal(calls.length, 1);
    assert.match(calls[0].url, /\/respond$/);
    assert.deepEqual([calls[0].body.selected_choices, calls[0].body.notes], [['not_done'], 'Page asked for a code I do not have']);
    // The two buttons fit a 380 px window.
    const fits = await page.evaluate(() => [...document.querySelectorAll('.sg-detail-footer button, .sg-detail-footer textarea')].every(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.right <= document.querySelector('.agfocus').getBoundingClientRect().right + 1; }));
    assert.equal(fits, true);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

browserTest('a tab that closes while the panel is open swaps the card for the plain line', async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable, args: ['--allow-file-access-from-files']});
  try {
    const page = await browser.newPage({viewport: {width: 800, height: 700}}), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(fixture);
    await page.evaluate(() => openItem('browser'));
    await page.waitForSelector('.sg-row[data-id="browser"] .sg-browser-tab button');
    await page.evaluate(() => { items.browser.__tab = tabs.closed; return sgRefresh(); });
    await page.waitForFunction(() => document.querySelector('.sg-row[data-id="browser"] .sg-browser-tab')?.textContent === 'This tab is no longer open.');
    assert.equal(await page.locator('.sg-browser-tab button').count(), 0);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
