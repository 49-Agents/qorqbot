// Run against a disposable local Qorqbot service with QORQBOT_PREVIEW_URL set.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');

let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* optional */ }
const url = process.env.QORQBOT_PREVIEW_URL;
const available = !!(url && chromium && fs.existsSync(chromium.executablePath()));

test('live app serves browser setup and Connections at desktop and mobile widths', {skip: !available && 'Local preview or Playwright unavailable'}, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1200, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url, {waitUntil: 'domcontentloaded'});
    const browserResult = await page.evaluate(async () => {
      const response = await fetch('/api/root/browser-setup');
      return {status: response.status, body: await response.json()};
    });
    assert.equal(browserResult.status, 200);
    assert.equal(browserResult.body.status, 'ok');
    assert.equal(browserResult.body.data.kind, 'built_in');
    assert.equal(browserResult.body.data.ready, true, JSON.stringify(browserResult.body.data.agents));
    const refusals = await page.evaluate(async () => {
      const send = async options => (await fetch('/api/root/browser-setup', options)).status;
      return {
        badBody: await send({method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({action: 'other'})}),
        bearer: await send({headers: {authorization: 'Bearer not-the-dashboard'}}),
      };
    });
    assert.deepEqual(refusals, {badBody: 422, bearer: 403});
    const skip = page.locator('.onboarding.is-welcome [data-tour-skip]');
    if (await skip.isVisible()) await skip.click();
    await page.locator('[data-global-settings]').first().click();
    await page.locator('[data-cx-browser-status]').waitFor();
    await page.locator('[data-cx-browser-status]').getByText('Ready').waitFor();
    await page.locator('[data-cx-extensions] [data-cx-card="treg"]').waitFor();
    assert.equal(await page.locator('details.cx-direct, [data-cx-card="gmail"]').count(), 0);
    assert.equal(await page.locator('[data-cx-browser] [data-cx-action]').count(), 0);
    if (process.env.CONNECTIONS_SCREENSHOT_DIR)
      await page.screenshot({path: path.join(process.env.CONNECTIONS_SCREENSHOT_DIR, 'connections-live-desktop.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (process.env.CONNECTIONS_SCREENSHOT_DIR)
      await page.screenshot({path: path.join(process.env.CONNECTIONS_SCREENSHOT_DIR, 'connections-live-mobile.png'), fullPage: true});
    assert.deepEqual(errors, []);
    console.log('Browser setup:', browserResult.body.data.ready ? 'ready' : browserResult.body.data.agents.map(a => a.state).join(','));
  } finally { await browser.close(); }
});
