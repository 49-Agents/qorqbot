'use strict';
// A Signoff attachment can be viewed in full: it covers the whole pane it sits in, shows only its body and a minimize
// button in the top right corner, and goes back on that button or Escape.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const root = path.join(__dirname, '..');

test('view in full covers the pane, with only a minimize button, and Escape brings it back', {skip: (!chromium || !fs.existsSync(executable)) && 'Local Playwright Chromium is unavailable'}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage({viewport: {width: 1400, height: 900}});
    await page.route('http://qorqbot.test/**', route => route.fulfill({contentType: 'text/html', body: '<!doctype html><body></body>'}));
    await page.goto('http://qorqbot.test/');
    await page.setContent('<div style="display:flex;height:900px"><div style="width:600px"></div><div class="agpad" style="flex:1"><div class="tbody" style="height:880px;margin-top:20px"><section class="agfocus" style="height:100%;overflow:auto"><div style="height:300px"></div><div class="sg-detail"><div class="sg-attachments" id="host"></div></div></section></div></div></div>');
    await page.addStyleTag({content: ':root{--fg:#eee;--fg-2:#aaa;--bg:#111;--line:#333;--line-2:#444;--hover:#222}'});
    for (const file of ['app.css', 'css/signoffs.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.evaluate(() => {
      window.esc = v => String(v ?? ''); window.coreData = x => x; window.toast = () => {};
      const data = {preview_kind: 'text', text: 'line\n'.repeat(200), name: 'matrix.md', path: 'files/matrix.md', urls: {}};
      window.fetch = async () => new Response(JSON.stringify({status: 'ok', data}), {headers: {'Content-Type': 'application/json'}});
    });
    for (const file of ['js/safe-links.js', 'js/signoff-attachments.js']) await page.addScriptTag({path: path.join(root, file)});
    await page.evaluate(() => {
      const source = document.createElement('div');
      source.innerHTML = '<div class="sg-rationale"><a class="safe-link" href="/files/view?path=files/matrix.md">Matrix</a></div>';
      sgAttachmentsMount(document.querySelector('#host'), source);
    });
    await page.waitForSelector('.sg-attachment-full-toggle');
    const rect = sel => page.$eval(sel, el => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height].map(Math.round); });
    assert.equal(await page.getAttribute('.sg-attachment-full-toggle', 'title'), 'View in full');
    await page.click('.sg-attachment-full-toggle');
    assert.deepEqual(await rect('.sg-attachment'), await rect('.agfocus'), 'it covers the whole right pane');
    const shown = await page.$$eval('.sg-attachment-head > *', nodes => nodes.filter(n => getComputedStyle(n).display !== 'none').map(n => n.className));
    assert.deepEqual(shown, ['quiet sg-attachment-full-toggle'], 'only the minimize button is left');
    const [x, y, w] = await rect('.sg-attachment-full-toggle'), [px, py, pw] = await rect('.agfocus');
    assert.ok(x + w > px + pw - 30 && y < py + 20, 'minimize sits in the top right corner');
    assert.equal(await page.getAttribute('.sg-attachment-full-toggle', 'title'), 'Minimize');
    await page.keyboard.press('Escape');
    assert.equal(await page.$eval('.sg-attachment', el => el.classList.contains('sg-attachment-full') || !!el.style.top), false);
    await page.click('.sg-attachment-full-toggle');
    await page.click('.sg-attachment-full-toggle');
    assert.equal(await page.$eval('.sg-attachment', el => el.classList.contains('sg-attachment-full')), false, 'the minimize button brings it back');
  } finally { await browser.close(); }
});
