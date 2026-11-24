'use strict';
// Only the browser cards of the last 10 rows are live; an older browser reference is a one-line chip with no iframe until it is clicked.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname, '..');

async function fixture(run) {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage({viewport: {width: 700, height: 700}});
    await page.setContent('<main id="chat" style="display:flex;flex-direction:column;height:600px;width:600px"></main>');
    for (const file of ['app.css', 'css/chat-view.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.evaluate(() => { window.esc = v => String(v ?? ''); window.agentGlyph = () => ''; });
    await page.addScriptTag({path: path.join(root, 'js/safe-links.js')});
    await page.addScriptTag({path: path.join(root, 'js/chat-view.js')});
    await page.evaluate(() => {
      // A local address nothing answers on: the iframes never reach a real browser viewer.
      window.row = (n, refs) => ({key: 'r' + n, id: 'r' + n, position: n, speaker: 'agent', text: 'message ' + n, timestamp: 1700000000000,
        typedReferences: refs || [{type: 'browser', id: 'b' + n, label: 'Viewer ' + n, viewer_url: 'http://127.0.0.1:9/view/' + n}]});
      window.rows = (from, to) => Array.from({length: to - from}, (_, i) => row(from + i));
      window.view = mountChatView(document.querySelector('#chat'), {});
      window.draw = list => view.render({entries: list, composer: {hidden: true}});
      window.frames = () => [...document.querySelectorAll('.gchat-msg')].map(n => [n.dataset.id, !!n.querySelector('iframe'), !!n.querySelector('.gchat-browser-chip')]);
      window.node = id => document.querySelector(`.gchat-msg[data-id="${id}"]`);
    });
    await run(page);
  } finally { await browser.close(); }
}
const browserTest = (name, run) => test(name, {skip: !available && 'Local Playwright Chromium is unavailable'}, () => fixture(run));

browserTest('the last 10 rows hold live cards and the first 2 hold chips without an iframe', async page => {
  await page.evaluate(() => draw(rows(0, 12)));
  const found = await page.evaluate(() => frames());
  assert.deepEqual(found.slice(0, 2), [['r0', false, true], ['r1', false, true]]);
  assert.ok(found.slice(2).every(([, frame, chip]) => frame && !chip));
  assert.equal(await page.locator('iframe').count(), 10);
  const chip = page.locator('.gchat-browser-chip').first();
  assert.equal(await chip.getAttribute('aria-label'), 'Open Viewer 0 browser');
  assert.equal((await chip.innerText()).trim(), 'Viewer 0');
});

browserTest('a chip click loads its live card in place, and it stays live through renders and new messages', async page => {
  await page.evaluate(() => draw(rows(0, 12)));
  await page.locator('.gchat-browser-chip').first().click();
  assert.equal(await page.evaluate(() => !!node('r0').querySelector('iframe') && !node('r0').querySelector('.gchat-browser-chip')), true);
  await page.evaluate(() => { window.kept = node('r0').querySelector('iframe'); draw(rows(0, 12)); draw(rows(0, 15)); draw(rows(0, 30)); });
  assert.equal(await page.evaluate(() => kept === node('r0').querySelector('iframe') && kept.isConnected), true, 'the opened card survives, far outside the window');
  assert.equal(await page.evaluate(() => !!node('r1').querySelector('iframe')), false, 'the other chip stays a chip');
});

browserTest('a row that leaves the window becomes a chip and its iframe is gone; rows that stay keep the same iframe', async page => {
  await page.evaluate(() => { draw(rows(0, 12)); window.stay = node('r11').querySelector('iframe'); window.leaving = node('r2').querySelector('iframe'); draw(rows(0, 13)); });
  assert.equal(await page.evaluate(() => frames().find(f => f[0] === 'r2').slice(1).join()), 'false,true', 'r2 left the last 10');
  assert.equal(await page.evaluate(() => leaving.isConnected && leaving.src), false, 'its iframe is removed and blanked');
  assert.equal(await page.evaluate(() => stay === node('r11').querySelector('iframe')), true);
  assert.equal(await page.locator('iframe').count(), 10);
  await page.evaluate(() => { window.again = node('r5').querySelector('iframe'); draw(rows(0, 13)); draw(rows(0, 13)); });
  assert.equal(await page.evaluate(() => again === node('r5').querySelector('iframe')), true, 'repaints do not recreate an iframe');
});

browserTest('an expanded card and a focused card are not collapsed; they are looked at again on the next render', async page => {
  await page.evaluate(() => draw(rows(0, 12)));
  await page.evaluate(() => { node('r2').querySelector('.gchat-browser-expand').click(); });
  await page.evaluate(() => { window.focusedFrame = node('r3').querySelector('iframe'); focusedFrame.focus(); draw(rows(0, 14)); });
  assert.equal(await page.evaluate(() => [node('r2').querySelectorAll('iframe').length, node('r3').querySelectorAll('iframe').length].join()), '1,1', 'r2 (expanded) and r3 (focus) stay live although both left the window');
  assert.equal(await page.evaluate(() => document.activeElement === focusedFrame || node('r3').contains(document.activeElement)), true);
  await page.evaluate(() => { node('r2').querySelector('.gchat-browser-expand').click(); document.activeElement?.blur(); draw(rows(0, 14)); });
  assert.equal(await page.evaluate(() => frames().filter(f => f[0] === 'r2' || f[0] === 'r3').map(f => f.slice(1).join()).join('|')), 'false,true|false,true', 'with nothing holding them they collapse on the next render');
});

browserTest('an older row prepended later mounts as a chip with no iframe', async page => {
  await page.evaluate(() => { draw(rows(10, 22)); draw(rows(0, 22)); });
  const found = await page.evaluate(() => frames());
  assert.equal(found.length, 22);
  assert.ok(found.slice(0, 12).every(([, frame, chip]) => !frame && chip), 'every older row is a chip');
  assert.equal(await page.locator('iframe').count(), 10);
});

browserTest('unavailable browser references and other reference types are unchanged outside the window', async page => {
  await page.evaluate(() => {
    const other = [{type: 'unknown-kind', id: 'x', label: 'Mystery'}, {type: 'browser', id: 'gone', label: 'Gone', viewer_url: 'http://127.0.0.1:9/', available: false},
      {type: 'tab', id: 't', label: 'A tab', agent_id: '00000000-0000-4000-8000-000000000000', tab_id: 'A'.repeat(32)}];
    draw([row(0, other), ...rows(1, 14)]);
  });
  assert.equal(await page.evaluate(() => node('r0').querySelectorAll('iframe, .gchat-browser-chip').length), 0, 'no browser card or chip');
  assert.equal(await page.evaluate(() => !!node('r0').querySelector('.gchat-tab-reference') && !node('r0').querySelector('.gchat-browser-chip')), true, 'the tab button is the tab button');
  assert.equal(await page.evaluate(() => node('r0').querySelector('.gchat-reference-unavailable') !== null), true);
});
