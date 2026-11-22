'use strict';
// Real-browser check of the trim: dropped rows leave the DOM and the feed stays at the bottom. The trim decision itself is tested in
// global-chat-compaction.test.js; here the real chat view paints the entries the way mountGlobalChat does.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname, '..');

test('rows removed by a trim leave the page and the feed stays at the bottom', {skip: !available && 'Local Playwright Chromium is unavailable'}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage({viewport: {width: 600, height: 600}});
    await page.setContent('<main id="chat" style="display:flex;flex-direction:column;height:400px;width:500px"></main>');
    for (const file of ['app.css', 'css/chat-view.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.addStyleTag({content: '.gchat-msg { min-height: 60px; }'});
    await page.evaluate(() => { window.esc = v => String(v ?? ''); window.agentGlyph = () => ''; });
    await page.addScriptTag({path: path.join(root, 'js/safe-links.js')});
    await page.addScriptTag({path: path.join(root, 'js/chat-view.js')});
    const result = await page.evaluate(async () => {
      const rows = (from, to) => Array.from({length: to - from}, (_, i) => ({key: 'e' + (from + i), id: 'e' + (from + i), position: from + i, speaker: 'user', text: 'message ' + (from + i), timestamp: 1700000000000}));
      const view = mountChatView(document.querySelector('#chat'), {olderOnScroll: true});
      const wait = () => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
      view.render({entries: rows(0, 120), composer: {hidden: true}}, {scroll: 'bottom'});
      await wait();
      const sc = document.querySelector('.gchat-scroll'), before = document.querySelectorAll('.gchat-log > .gchat-msg').length;
      view.render({entries: rows(70, 120), composer: {hidden: true}}, {scroll: 'bottom'});   // what paintEntries('bottom') does after a trim
      await wait();
      return {before, after: document.querySelectorAll('.gchat-log > .gchat-msg').length, first: document.querySelector('.gchat-log > .gchat-msg').dataset.id,
        gap: sc.scrollHeight - sc.scrollTop - sc.clientHeight, atBottom: view.isAtBottom(), scrollable: sc.scrollHeight > sc.clientHeight};
    });
    assert.deepEqual([result.before, result.after, result.first], [120, 50, 'e70']);
    assert.equal(result.scrollable, true);
    assert.equal(result.atBottom, true);
    assert.ok(result.gap < 2, `bottom gap ${result.gap}`);
  } finally { await browser.close(); }
});
