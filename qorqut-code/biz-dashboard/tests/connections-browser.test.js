const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* optional local browser test dependency */ }
const available = !!chromium && fs.existsSync(chromium.executablePath());

test('Connections renders browser readiness and the extensions, with treg set up by a key', {skip: !available && 'Playwright Chromium unavailable'}, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1120, height: 900}});
    await page.setContent('<main id="panel"></main>');
    await page.addStyleTag({content: `:root{--bg:#111820;--elevated:#1c2730;--wash:#19232c;--hover:#29353f;--fg:#f3f7f7;--fg-2:#b8c6c9;--fg-3:#809398;--line-2:#34424a;--ok:#83c398;--warn:#e5b572;--accent:#a2d7b3;--mono:monospace;--r:12px;--r-lg:17px;--r-sm:8px;--r-full:999px}body{margin:0;background:var(--bg);color:var(--fg);font-family:Arial,sans-serif}main{max-width:980px;margin:35px auto;padding:0 20px}button{background:var(--fg);border:0;border-radius:7px;padding:8px 14px;cursor:pointer}.quiet{background:var(--hover);color:var(--fg)}`});
    await page.addStyleTag({path: path.join(__dirname, '../css/connectors.css')});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.coreData = value => value;
      window.browserState = {kind: 'built_in', tool_installed: true, chrome_installed: true, ready: false, state: 'missing',
        explanation: 'The browser hasn’t been set up yet. Press Retry setup.', agents: [{name: 'Ordubashy', connected: false}]};
      window.tregStatus = 'not_set_up';
      window.sent = [];
      window.api = async url => url === '/root/browser-setup' ? window.browserState : url === '/root/browser-accounts' ? {signing_in: null, sites: []} :
        {platform: 'linux', clients: [],
          connectors: [{id: 'treg', kind: 'extension', status: window.tregStatus}, {id: 'whatsapp', kind: 'extension', status: 'not_installed'}]};
      window.post = async (url, body) => {
        window.sent.push({url, body});
        if (url === '/root/browser-setup' && body.action === 'retry') {
          window.browserState = {...window.browserState, ready: true, state: 'healthy', explanation: '',
            agents: [{name: 'Ordubashy', connected: true}]};
          return window.browserState;
        }
        if (url === '/root/extensions/treg/key') {
          if (body.key === 'refused-by-treg-0000') throw new Error('treg did not accept this key');
          window.tregStatus = body.key === null ? 'not_set_up' : 'connected';
          return {status: window.tregStatus};
        }
        return window.api(url);
      };
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/connectors.js')});
    await page.evaluate(() => connectorsPanel.mount(document.querySelector('#panel'), {rows: true}));
    await assert.doesNotReject(() => page.locator('[data-cx-browser-status]').getByText('Setup needed').waitFor());
    assert.deepEqual(await page.locator('[data-cx-list] [data-cx-card]').evaluateAll(cards => cards.map(card => card.dataset.cxCard)), ['treg', 'whatsapp']);
    assert.equal(await page.locator('[data-cx-extensions]').isVisible(), true);
    assert.equal(await page.locator('details.cx-direct, [data-cx-progress], [data-cx-refresh]').count(), 0);
    assert.doesNotMatch(await page.locator('#panel').textContent(), /claude\.ai|Claude connectors|Gmail/);
    assert.equal(await page.locator('[data-cx-browser] [data-cx-action]').count(), 0);
    await page.locator('[data-cx-browser-retry]').click();
    await page.locator('[data-cx-browser-status]').getByText('Ready').waitFor();
    // The treg key: the field opens from Add key, a refused key stays in the open field with the reason, a good one connects.
    const tregCard = page.locator('[data-cx-card="treg"]');
    assert.equal(await tregCard.locator('[data-cx-key]').isVisible(), false);
    await tregCard.getByRole('button', {name: 'Add key'}).click();
    await tregCard.locator('input[name="key"]').fill('refused-by-treg-0000');
    await tregCard.getByRole('button', {name: 'Save key'}).click();
    await tregCard.locator('[data-cx-key-error]').getByText('treg did not accept this key').waitFor();
    assert.equal(await tregCard.locator('.cx-status').textContent(), 'Not set up');
    await tregCard.locator('input[name="key"]').fill('  made-up-treg-key-for-tests  ');
    await tregCard.getByRole('button', {name: 'Save key'}).click();
    await tregCard.locator('.cx-status').getByText('Connected').waitFor();
    assert.deepEqual(await page.evaluate(() => window.sent.at(-1)), {url: '/root/extensions/treg/key', body: {key: 'made-up-treg-key-for-tests'}});
    assert.equal(await tregCard.locator('input[name="key"]').inputValue(), '', 'a saved key is never shown again');
    assert.equal(await page.locator('[data-cx-card="whatsapp"] [data-cx-action]').count(), 0);
    assert.equal(await page.locator('[data-cx-browser]').evaluate(element => element.scrollWidth <= element.clientWidth), true);
    if (process.env.CONNECTIONS_SCREENSHOT_DIR)
      await page.screenshot({path: path.join(process.env.CONNECTIONS_SCREENSHOT_DIR, 'connections-desktop.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 850});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (process.env.CONNECTIONS_SCREENSHOT_DIR)
      await page.screenshot({path: path.join(process.env.CONNECTIONS_SCREENSHOT_DIR, 'connections-mobile.png'), fullPage: true});
  } finally { await browser.close(); }
});

test('without an installed Chrome the card follows the download of Qorqbot’s own browser', {skip: !available && 'Playwright Chromium unavailable'}, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1120, height: 900}});
    await page.setContent('<main id="panel"></main>');
    await page.addStyleTag({path: path.join(__dirname, '../css/connectors.css')});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.coreData = value => value;
      window.sent = [];
      window.setup = chrome => ({kind: 'built_in', tool_installed: true, chrome_installed: false, ready: false, managed_chrome: chrome,
        state: 'unsupported', explanation: 'Install Chrome, then press Retry setup.', agents: [{name: 'Ordubashy', connected: false}]});
      window.browserState = window.setup({state: 'downloading', explanation: 'Qorqbot is downloading its own browser (about 200 MB). This card updates by itself.'});
      window.api = async url => url === '/root/browser-setup' ? window.browserState : url === '/root/browser-accounts' ? {signing_in: null, sites: []}
        : {platform: 'linux', clients: [], connectors: []};
      window.post = async (url, body) => { window.sent.push({url, body}); return window.browserState; };
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/connectors.js')});
    await page.evaluate(() => connectorsPanel.mount(document.querySelector('#panel'), {rows: true}));
    const status = page.locator('[data-cx-browser-status]'), detail = page.locator('[data-cx-browser-detail]');
    await status.getByText('Downloading').waitFor();
    assert.match(await detail.textContent(), /downloading its own browser/);
    assert.doesNotMatch(await detail.textContent(), /Install/);
    // The owner's words for a failure replace the old "install Chrome" dead end, and Retry setup is offered.
    const painted = () => page.evaluate(() => new Promise(done => document.querySelector('#panel').addEventListener('browser-setup-painted', done, {once: true})));
    let next = painted();
    await page.evaluate(() => { window.browserState = window.setup({state: 'failed', explanation: 'Qorqbot couldn’t download its own browser. Check this computer’s internet connection.'}); });
    await next;
    await status.getByText('Download failed').waitFor();
    assert.match(await detail.textContent(), /internet connection/);
    assert.equal(await page.locator('[data-cx-browser-retry]').isEnabled(), true);
    await page.locator('[data-cx-browser-retry]').click();
    assert.deepEqual(await page.evaluate(() => window.sent.at(-1)), {url: '/root/browser-setup', body: {action: 'retry'}});
    // A build that cannot start on this computer is the one case that still asks for Google Chrome.
    await page.evaluate(() => { window.browserState = window.setup({state: 'unusable', explanation: 'Qorqbot’s own browser didn’t start on this computer. Install Google Chrome from google.com/chrome, then press Retry setup.'}); });
    await page.locator('[data-cx-browser-retry]').click();
    await status.getByText('Chrome needed').waitFor();
    assert.match(await detail.textContent(), /google\.com\/chrome/);
    // A server from before the download existed sends no state: the card says what it always said.
    await page.evaluate(() => { window.browserState = window.setup(undefined); });
    await page.locator('[data-cx-browser-retry]').click();
    await detail.getByText(/isn’t installed on this computer/).waitFor();
    assert.equal(await status.textContent(), 'Chrome needed');
  } finally { await browser.close(); }
});

test('website sign-ins are led by Ordubashy and Settings only lists and signs out', {skip: !available && 'Playwright Chromium unavailable'}, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1120, height: 900}});
    await page.setContent('<main id="panel"></main>');
    await page.addStyleTag({path: path.join(__dirname, '../css/connectors.css')});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.coreData = value => value;
      window.sent = [];
      window.accounts = {signing_in: 'github.com', sites: [{site: 'slack.com', session_saved: true}, {site: 'notion.so', session_saved: false}]};
      window.api = async url => url === '/root/browser-accounts' ? window.accounts : url === '/root/browser-setup'
        ? {kind: 'built_in', tool_installed: true, chrome_installed: true, ready: true, state: 'healthy', agents: [{name: 'Ordubashy', connected: true}]}
        : {platform: 'linux', clients: [], connectors: [{id: 'treg', kind: 'extension', status: 'not_set_up'}]};
      window.post = async (url, body) => {
        window.sent.push(body);
        window.accounts = {signing_in: null, sites: window.accounts.sites.filter(item => item.site !== body.site)};
        return window.accounts;
      };
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/connectors.js')});
    await page.evaluate(() => connectorsPanel.mount(document.querySelector('#panel'), {rows: true}));
    await page.locator('[data-cx-site="slack.com"]').getByText('Session saved', {exact: true}).waitFor();
    assert.match(await page.locator('[data-cx-signin-note]').textContent(), /close that window/);
    assert.equal(await page.locator('[data-cx-accounts] input, [data-cx-accounts] form').count(), 0, 'Settings cannot start a sign-in');
    assert.match(await page.locator('[data-cx-accounts]').textContent(), /ask Ordubashy in Chat/);
    assert.match(await page.locator('[data-cx-accounts]').textContent(), /can’t block those actions in code/);
    assert.equal(await page.locator('[data-cx-site="notion.so"] [data-cx-site-action]').textContent(), 'Remove');
    await page.evaluate(() => { window.accounts = {...window.accounts, signing_in: null}; });
    await page.locator('[data-cx-signin-note]').waitFor({state: 'hidden'});
    await page.locator('[data-cx-site="slack.com"] [data-cx-site-action]').click();
    await page.locator('[data-cx-site="slack.com"]').waitFor({state: 'detached'});
    assert.deepEqual(await page.evaluate(() => window.sent[0]), {action: 'sign_out', site: 'slack.com'});
  } finally { await browser.close(); }
});

test('every agent uses the one Shared browser, so Settings offers no browser choice', {skip: !available && 'Playwright Chromium unavailable'}, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1120, height: 900}});
    await page.setContent('<main id="panel"></main>');
    await page.addStyleTag({path: path.join(__dirname, '../css/connectors.css')});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.coreData = value => value;
      window.browserState = {kind: 'built_in', tool_installed: true, chrome_installed: true, ready: true, state: 'healthy', agents: [
        {agent_id: 'lead', name: 'Ordubashy', connected: true},
        {agent_id: 'atlas', name: 'Atlas', connected: true}]};
      window.api = async url => url === '/root/browser-setup' ? window.browserState : url === '/root/browser-accounts' ? {signing_in: null, sites: []}
        : {platform: 'linux', clients: [], connectors: []};
      window.post = async () => window.browserState;
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/connectors.js')});
    await page.evaluate(() => connectorsPanel.mount(document.querySelector('#panel'), {rows: true}));
    await page.locator('[data-cx-browser-status]').getByText('Ready').waitFor();
    assert.match(await page.locator('[data-cx-browser]').innerText(), /share one private browser/);
    assert.equal(await page.locator('#panel select').count(), 0);
    assert.doesNotMatch(await page.locator('#panel').innerText(), /Own browser|Which browser/);
  } finally { await browser.close(); }
});
