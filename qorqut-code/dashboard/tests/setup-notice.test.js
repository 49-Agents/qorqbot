const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const notice = require('../js/setup-notice.js');

let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* optional local browser test dependency */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && !!executable && fs.existsSync(executable);
const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');

test('a missing Chrome is reported with where to get it', () => {
  const message = notice.message({chrome_installed: false});
  assert.strictEqual(message.text, 'Qorqbot needs Google Chrome so your agents can work on the web.');
  assert.strictEqual(message.action, 'Get Chrome');
  assert.strictEqual(message.url, 'https://www.google.com/chrome/');
});

test('nothing is shown when Chrome is installed or unknown', () => {
  assert.strictEqual(notice.message({chrome_installed: true}), null);
  assert.strictEqual(notice.message({}), null);
  assert.strictEqual(notice.message(null), null);
});

test('the notice stands above the welcome tour and the first-run screens, which leave it usable', () => {
  const layer = (file, selector) => Number(new RegExp(`\\${selector} \\{[^}]*z-index: ?(\\d+)`).exec(read(file))[1]);
  const notices = layer('css/connectors.css', '.setup-notices');
  for (const [file, selector] of [['css/onboarding.css', '.onboarding'], ['css/account-gate.css', '.account-gate'],
    ['css/agent-clients.css', '.ac-gate']])
    assert.ok(notices > layer(file, selector), selector);
  for (const file of ['js/account-gate.js', 'js/agent-clients.js'])
    assert.match(read(file), /el\.className !== 'setup-notices'/, file);
});

test('the notice looks again while it shows and goes by itself once Chrome is installed', {skip: !available && 'Playwright Chromium unavailable'}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage({viewport: {width: 1120, height: 800}});
    await page.clock.install();
    await page.setContent('<main class="app">Dashboard</main>');
    await page.addStyleTag({path: path.join(__dirname, '../css/connectors.css')});
    await page.addStyleTag({path: path.join(__dirname, '../css/agent-clients.css')});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.coreData = value => value;
      window.reads = 0;
      window.chrome = false;
      window.api = async url => { if (url === '/root/about') window.reads += 1; return {chrome_installed: window.chrome}; };
      // A first-run screen covering the page, as js/agent-clients.js opens it.
      const gate = document.createElement('div');
      gate.className = 'ac-gate';
      gate.innerHTML = '<section>get your agents ready</section>';
      document.body.append(gate);
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/setup-notice.js')});
    const bar = page.locator('.chrome-notice');
    await bar.getByText('Qorqbot needs Google Chrome').waitFor();
    // Visible above the first-run screen: what is under the notice's link is the link.
    assert.strictEqual(await page.evaluate(() => {
      const link = document.querySelector('.chrome-notice a');
      const box = link.getBoundingClientRect();
      return document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2) === link;
    }), true);
    assert.strictEqual(await bar.getByRole('link', {name: 'Get Chrome'}).getAttribute('href'), 'https://www.google.com/chrome/');
    const first = await page.evaluate(() => window.reads);
    await page.clock.runFor(notice.RECHECK_MS);
    await page.waitForFunction(count => window.reads > count, first);
    assert.strictEqual(await page.locator('.chrome-notice').count(), 1, 'still needed: shown once, not painted again');
    await page.evaluate(() => { window.chrome = true; });
    await page.clock.runFor(notice.RECHECK_MS);
    await bar.waitFor({state: 'detached'});
    const after = await page.evaluate(() => window.reads);
    await page.clock.runFor(notice.RECHECK_MS * 3);
    assert.strictEqual(await page.evaluate(() => window.reads), after, 'no more reads once it is gone');
  } finally { await browser.close(); }
});
