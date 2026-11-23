'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Browser checks are optional. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const browserTest = (name, fn) => test(name, {skip: (!chromium || !fs.existsSync(executable)) && 'Local Playwright Chromium is unavailable'}, fn);
const source = fs.readFileSync(path.join(__dirname, '..', 'js', 'attention-queue.js'), 'utf8');
const helper = source.slice(source.indexOf('  const CHIP_KEYS='), source.indexOf('  function paintSignoffs(){'));

const chip = (key, unread) => `<button type="button" data-signoff-key="${key}" data-unread="${unread}" style="width:40px;height:40px"><svg width="18" height="18"><path d="M1 1h10"/></svg></button>`;

browserTest('a Signoff chip keeps its click when the row repaints between press and release', async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage();
    await page.setContent('<div id="row" style="display:flex;gap:8px"></div>');
    await page.addScriptTag({content: helper + ';window.patchChips=patchChips;'});
    for (const before of ['', '<button type="button" data-reminder-key="R1" style="width:40px;height:40px">r</button>']) {
      await page.evaluate(html => {
        const host = document.getElementById('row'); host.innerHTML = html; window.clicked = [];
        host.onclick = event => window.clicked.push(event.target.closest('[data-signoff-key]')?.dataset.signoffKey || 'row');
      }, chip('A', true) + chip('B', false));
      const pressed = await page.locator('[data-signoff-key="B"]').boundingBox();
      await page.mouse.move(pressed.x + 20, pressed.y + 20); await page.mouse.down();
      // The reply refresh after opening A clears its dot (and a due reminder may appear in front) while B is held down.
      await page.evaluate(html => patchChips(document.getElementById('row'), html), before + chip('A', false) + chip('B', false));
      const released = await page.locator('[data-signoff-key="B"]').boundingBox();
      await page.mouse.move(released.x + 20, released.y + 20); await page.mouse.up();
      assert.deepEqual(await page.evaluate(() => window.clicked), ['B']);
      assert.equal(await page.getAttribute('[data-signoff-key="A"]', 'data-unread'), 'false');
      assert.equal(await page.evaluate(() => document.getElementById('row').children.length), before ? 3 : 2);
    }
  } finally { await browser.close(); }
});
