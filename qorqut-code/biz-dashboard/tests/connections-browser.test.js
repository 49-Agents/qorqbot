const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* optional local browser test dependency */ }
const available = !!chromium && fs.existsSync(chromium.executablePath());

test('Connections renders browser readiness and keeps external services optional', {skip: !available && 'Playwright Chromium unavailable'}, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1120, height: 900}});
    await page.setContent('<main id="panel"></main>');
    await page.addStyleTag({content: `:root{--bg:#111820;--elevated:#1c2730;--wash:#19232c;--hover:#29353f;--fg:#f3f7f7;--fg-2:#b8c6c9;--fg-3:#809398;--line-2:#34424a;--ok:#83c398;--warn:#e5b572;--accent:#a2d7b3;--mono:monospace;--r:12px;--r-lg:17px;--r-sm:8px;--r-full:999px}body{margin:0;background:var(--bg);color:var(--fg);font-family:Arial,sans-serif}main{max-width:980px;margin:35px auto;padding:0 20px}button{background:var(--fg);border:0;border-radius:7px;padding:8px 14px;cursor:pointer}.quiet{background:var(--hover);color:var(--fg)}`});
    await page.addStyleTag({path: path.join(__dirname, '../css/connectors.css')});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.coreData = value => value;
      window.browserState = {kind: 'built_in', tool_installed: true, chrome_installed: true, ready: false,
        agents: [{name: 'Ordubashy', state: 'missing', diagnostic: 'Preparing agent browser…'}]};
      window.api = async url => url === '/root/browser-setup' ? window.browserState :
        {checking: false, claude_account: {signed_in: true, method: 'claude.ai', email: 'owner@example.com'},
          connectors: [{id: 'gmail', kind: 'claude', tier: 'recommended', status: 'not_added'},
            {id: 'google_calendar', kind: 'claude', tier: 'suggested', status: 'not_added'}]};
      window.post = async (url, body) => {
        if (url === '/root/browser-setup' && body.action === 'retry') {
          window.browserState = {...window.browserState, ready: true,
            agents: [{name: 'Ordubashy', state: 'healthy', diagnostic: 'Ready.'}]};
          return window.browserState;
        }
        return window.api(url);
      };
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/connectors.js')});
    await page.evaluate(() => connectorsPanel.mount(document.querySelector('#panel'), {rows: true}));
    await assert.doesNotReject(() => page.locator('[data-cx-browser-status]').getByText('Setup needed').waitFor());
    assert.equal(await page.locator('[data-cx-tier="recommended"] [data-cx-card]').count(), 1);
    assert.equal(await page.locator('[data-cx-browser] [data-cx-action]').count(), 0);
    await page.locator('[data-cx-browser-retry]').click();
    await page.locator('[data-cx-browser-status]').getByText('Ready').waitFor();
    assert.match(await page.locator('[data-cx-progress]').innerText(), /0 of 1/);
    assert.equal(await page.locator('[data-cx-browser]').evaluate(element => element.scrollWidth <= element.clientWidth), true);
    if (process.env.CONNECTIONS_SCREENSHOT_DIR)
      await page.screenshot({path: path.join(process.env.CONNECTIONS_SCREENSHOT_DIR, 'connections-desktop.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 850});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (process.env.CONNECTIONS_SCREENSHOT_DIR)
      await page.screenshot({path: path.join(process.env.CONNECTIONS_SCREENSHOT_DIR, 'connections-mobile.png'), fullPage: true});
  } finally { await browser.close(); }
});
