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
      window.api = async url => url === '/root/browser-setup' ? window.browserState : url === '/root/browser-accounts' ? {sites: []} :
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

test('the WhatsApp card links a phone: code and steps while linking, refreshed until connected', {skip: !available && 'Playwright Chromium unavailable'}, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1120, height: 900}});
    const codes = [];
    await page.route('**/api/root/extensions/whatsapp/qr*', route => { codes.push(route.request().url()); route.fulfill({status: 404, body: ''}); });
    // A real origin, so the code image's relative address is requested (and answered here) instead of failing to load.
    await page.route('http://qorqbot.test/', route => route.fulfill({contentType: 'text/html', body: '<main id="panel"></main>'}));
    await page.goto('http://qorqbot.test/');
    await page.addStyleTag({path: path.join(__dirname, '../css/connectors.css')});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.coreData = value => value;
      window.setup = {state: 'not_set_up', note: '', qr: false, send_ready: false};
      window.sent = [];
      window.reads = 0;
      window.api = async url => {
        if (url === '/root/browser-setup') return {kind: 'built_in', tool_installed: true, chrome_installed: true, ready: true, state: 'healthy', explanation: '', agents: []};
        if (url === '/root/browser-accounts') return {sites: []};
        if (url === '/root/extensions/whatsapp/setup') { window.reads += 1; return window.setup; }
        return {platform: 'linux', clients: [], connectors: [{id: 'whatsapp', kind: 'extension', status: window.setup.state}]};
      };
      window.post = async (url, body) => {
        window.sent.push({url, body});
        window.setup = body.action === 'link' ? {state: 'linking', note: '', qr: true, send_ready: false} : {state: 'not_set_up', note: '', qr: false, send_ready: false};
        return window.setup;
      };
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/connectors.js')});
    await page.evaluate(() => connectorsPanel.mount(document.querySelector('#panel'), {rows: true}));
    const card = page.locator('[data-cx-card="whatsapp"]');
    await card.getByRole('button', {name: 'Link phone'}).click();
    await card.locator('img[data-cx-wa-code]').waitFor();
    assert.deepEqual(await page.evaluate(() => window.sent.at(-1)), {url: '/root/extensions/whatsapp/setup', body: {action: 'link'}});
    assert.match(await card.locator('[data-cx-wa-link] ol').textContent(), /Linked devices/);
    // The code is read again while linking; the same image element takes the new address.
    const image = await card.locator('img[data-cx-wa-code]').elementHandle();
    await page.waitForFunction(() => window.reads >= 1, null, {timeout: 8000});
    await page.waitForFunction(element => /v=[1-9]/.test(element.getAttribute('src')) && element.isConnected, image, {timeout: 8000});
    assert.ok(codes.length >= 1);
    await page.evaluate(() => { window.setup = {state: 'connected', note: '', qr: false, send_ready: true}; });
    await card.locator('.cx-status').getByText('Connected').waitFor({timeout: 8000});
    assert.equal(await card.locator('[data-cx-wa-link]').count(), 0);
    const settled = await page.evaluate(() => window.reads);
    await page.waitForTimeout(3500);
    assert.equal(await page.evaluate(() => window.reads), settled, 'no reading once connected');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
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
      window.api = async url => url === '/root/browser-setup' ? window.browserState : url === '/root/browser-accounts' ? {sites: []}
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
      window.accounts = {sites: [{site: 'slack.com', session_saved: true}, {site: 'notion.so', session_saved: false}]};
      window.api = async url => url === '/root/browser-accounts' ? window.accounts : url === '/root/browser-setup'
        ? {kind: 'built_in', tool_installed: true, chrome_installed: true, ready: true, state: 'healthy', agents: [{name: 'Ordubashy', connected: true}]}
        : {platform: 'linux', clients: [], connectors: [{id: 'treg', kind: 'extension', status: 'not_set_up'}]};
      window.post = async (url, body) => {
        window.sent.push(body);
        window.accounts = {sites: window.accounts.sites.filter(item => item.site !== body.site)};
        return window.accounts;
      };
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/connectors.js')});
    await page.evaluate(() => connectorsPanel.mount(document.querySelector('#panel'), {rows: true}));
    await page.locator('[data-cx-site="slack.com"]').getByText('Session saved', {exact: true}).waitFor();
    assert.equal(await page.locator('[data-cx-accounts] input, [data-cx-accounts] form').count(), 0, 'Settings cannot start a sign-in');
    assert.match(await page.locator('[data-cx-accounts]').textContent(), /ask Ordubashy in Chat/);
    assert.match(await page.locator('[data-cx-accounts]').textContent(), /can’t block those actions in code/);
    assert.equal(await page.locator('[data-cx-site="notion.so"] [data-cx-site-action]').textContent(), 'Remove');
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
      window.api = async url => url === '/root/browser-setup' ? window.browserState : url === '/root/browser-accounts' ? {sites: []}
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

test('a contract extension gets the generic card: the key is posted once, the form is empty after, switches post', {skip: !available && 'Playwright Chromium unavailable'}, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1120, height: 900}});
    await page.setContent('<main id="panel"></main>');
    await page.addStyleTag({path: path.join(__dirname, '../css/connectors.css')});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.coreData = value => value;
      window.keyed = {id: 'keyed', kind: 'contract', name: 'Keyed', summary: 'Tells its agents whether a token is saved.', version: '1.0.0',
        delivery: 'downloadable', status: 'not_set_up', note: null, can_set_up: true,
        setup: {kind: 'key', fields: [{id: 'token', label: 'Token', secret: true}]},
        agents: [{agent_id: 'agent-1', name: 'Ordubashy', role: 'ordubashy', on: true, in_effect: false}]};
      window.paired = {id: 'linked', kind: 'contract', name: 'Linked', summary: 'Paired by a code.', version: '1.0.0', delivery: 'built-in',
        status: 'not_set_up', note: null, can_set_up: true, setup: {kind: 'pairing', fields: []}, agents: []};
      window.sent = [];
      window.api = async url => url === '/root/browser-setup' ? {kind: 'built_in', tool_installed: true, chrome_installed: true, ready: true, state: 'healthy', explanation: '', agents: []}
        : url === '/root/browser-accounts' ? {sites: []}
        : {platform: 'linux', clients: [], connectors: [{id: 'whatsapp', kind: 'extension', status: 'not_installed'}, window.keyed, window.paired]};
      window.post = async (url, body) => {
        window.sent.push({url, body});
        if (body.action === 'accept') {
          if (body.values.token === 'wrong') { window.keyed = {...window.keyed, status: 'needs_attention', note: 'Keyed did not accept this token.'}; return window.keyed; }
          window.keyed = {...window.keyed, status: 'ready', note: null, agents: window.keyed.agents.map(agent => ({...agent, in_effect: agent.on}))};
        }
        if (body.action === 'switch') window.keyed = {...window.keyed, agents: window.keyed.agents.map(agent => ({...agent, on: body.on, in_effect: body.on && window.keyed.status === 'ready'}))};
        if (body.action === 'disconnect') window.keyed = {...window.keyed, status: 'not_set_up'};
        return window.keyed;
      };
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/extension-cards.js')});
    await page.addScriptTag({path: path.join(__dirname, '../js/connectors.js')});
    await page.evaluate(() => connectorsPanel.mount(document.querySelector('#panel'), {rows: true}));
    const card = page.locator('[data-cx-card="keyed"]');
    await card.locator('.cx-status').getByText('Not set up').waitFor();
    assert.deepEqual(await page.locator('[data-cx-list] [data-cx-card]').evaluateAll(cards => cards.map(card => card.dataset.cxCard)), ['whatsapp', 'keyed', 'linked']);
    assert.match(await card.textContent(), /On once it is ready/);
    // Set up is not offered again while its form is open; Close brings it back.
    await card.getByRole('button', {name: 'Set up'}).click();
    assert.equal(await card.locator('[data-ext-form]').isVisible(), true);
    assert.equal(await card.getByRole('button', {name: 'Set up'}).isVisible(), false);
    await card.getByRole('button', {name: 'Close'}).click();
    assert.equal(await card.getByRole('button', {name: 'Set up'}).isVisible(), true);
    // A refused token: the card says why and offers Try again.
    await card.getByRole('button', {name: 'Set up'}).click();
    await card.locator('input[name="token"]').fill('wrong');
    await card.getByRole('button', {name: 'Save'}).click();
    await card.locator('.cx-note').getByText('Keyed did not accept this token.').waitFor();
    await card.getByRole('button', {name: 'Try again'}).click();
    await card.locator('input[name="token"]').fill('  keyed-test-token  ');
    await card.getByRole('button', {name: 'Save'}).click();
    await card.locator('.cx-status').getByText('Ready').waitFor();
    assert.deepEqual(await page.evaluate(() => window.sent.filter(item => item.body.action === 'accept').at(-1)),
      {url: '/root/connections/keyed', body: {action: 'accept', values: {token: 'keyed-test-token'}}});
    assert.equal(await page.evaluate(() => window.sent.filter(item => item.body.values?.token === 'keyed-test-token').length), 1, 'posted once');
    assert.equal(await card.locator('input[name="token"]').count(), 0, 'no form once ready, so nothing typed is shown again');
    assert.doesNotMatch(await page.locator('#panel').innerHTML(), /keyed-test-token/);
    // The switch posts and the card follows the answer.
    await card.locator('[data-ext-switch]').uncheck();
    await page.waitForFunction(() => window.sent.at(-1).body.action === 'switch');
    assert.deepEqual(await page.evaluate(() => window.sent.at(-1)), {url: '/root/connections/keyed', body: {action: 'switch', agent_id: 'agent-1', on: false}});
    await card.getByRole('button', {name: 'Disconnect'}).click();
    await card.locator('.cx-status').getByText('Not set up').waitFor();
    // A refused code part: the note with its backticked word as code, and nothing to click that would do nothing.
    await page.evaluate(() => {
      window.keyed = {...window.keyed, status: 'needs_attention', can_set_up: false,
        note: 'Its code could not be loaded: its tool names must start with `keyed_`.'};
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await card.locator('.cx-status').getByText('Needs attention').waitFor();
    assert.equal(await card.locator('.cx-note code').textContent(), 'keyed_');
    assert.equal(await card.locator('button, input, form').count(), 0);
    // Pairing is not set up here yet: status and the sentence, no button and no image.
    const linked = page.locator('[data-cx-card="linked"]');
    assert.match(await linked.textContent(), /Setting this up from Qorqbot is not available yet\./);
    assert.equal(await linked.locator('button, img').count(), 0);
    await page.setViewportSize({width: 390, height: 850});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  } finally { await browser.close(); }
});

test('the owner installs and removes a downloadable extension on its card, and sees the result after the restart', {skip: !available && 'Playwright Chromium unavailable'}, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1120, height: 900}});
    await page.setContent('<main id="panel"></main>');
    await page.addStyleTag({path: path.join(__dirname, '../css/connectors.css')});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.coreData = value => value;
      const base = {id: 'network', kind: 'contract', name: 'Network', summary: 'Network tools.', version: '1.0.0', delivery: 'downloadable',
        note: null, setup: null, agents: [], result: null, restarting: false};
      window.network = {...base, status: 'not_installed', offers: [{action: 'install', version: '1.0.0'}]};
      window.sent = [];
      window.down = 0;   // reads that fail while Qorqbot restarts
      window.api = async url => {
        if (url === '/root/browser-setup') return {kind: 'built_in', tool_installed: true, chrome_installed: true, ready: true, state: 'healthy', explanation: '', agents: []};
        if (url === '/root/browser-accounts') return {sites: []};
        if (window.down > 0) { window.down -= 1; throw new Error('Qorqbot is restarting'); }
        return {platform: 'linux', clients: [], connectors: [window.network]};
      };
      window.post = async (url, body) => {
        window.sent.push({url, body});
        if (window.gate) await window.gate;
        const waiting = {...window.network, offers: [], result: 'Qorqbot restarts to apply this change.', restarting: true};
        window.down = 1;
        window.network = body.action === 'install'
          ? {...base, status: 'not_set_up', setup: {kind: 'none', fields: []}, offers: [{action: 'remove'}], result: 'Installed Network 1.0.0. Qorqbot restarted.'}
          : {...base, status: 'not_installed', offers: [{action: 'install', version: '1.0.0'}],
             result: body.delete_data ? 'Removed Network. Its private data was deleted.' : 'Removed Network.'};
        return waiting;
      };
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/extension-cards.js')});
    await page.addScriptTag({path: path.join(__dirname, '../js/connectors.js')});
    await page.evaluate(() => connectorsPanel.mount(document.querySelector('#panel'), {rows: true}));
    const card = page.locator('[data-cx-card="network"]');
    await card.locator('.cx-status').getByText('Not installed').waitFor();
    await card.getByRole('button', {name: 'Install'}).click();
    await card.getByText('Qorqbot restarts to apply this change.').waitFor();
    assert.equal(await card.getByRole('button').count(), 0, 'nothing more to click while the change waits');
    await card.getByText('Installed Network 1.0.0. Qorqbot restarted.').waitFor({timeout: 15000});
    assert.deepEqual(await page.evaluate(() => window.sent.at(-1)), {url: '/root/connections/network', body: {action: 'install', version: '1.0.0'}});
    // Remove opens the same two choices as the card for Ordubashy's ask.
    await card.getByRole('button', {name: 'Remove', exact: true}).click();
    const choices = card.locator('[data-ext-remove]');
    await choices.getByText('Qorqbot restarts once. Projects, tables and records it created stay.').waitFor();
    // Pressing one of the two choices disables both until the answer is back.
    await page.evaluate(() => { window.gate = new Promise(resolve => { window.answer = resolve; }); });
    await choices.getByRole('button', {name: 'Remove and delete its data'}).click();
    await page.waitForFunction(() => window.sent.length === 2);
    assert.deepEqual(await choices.locator('[data-ext-action]').evaluateAll(buttons => buttons.map(button => button.disabled)), [true, true]);
    await page.evaluate(() => { window.gate = null; window.answer(); });
    await card.getByText('Removed Network. Its private data was deleted.').waitFor({timeout: 15000});
    assert.deepEqual(await page.evaluate(() => window.sent.at(-1)), {url: '/root/connections/network', body: {action: 'remove', delete_data: true}});
    assert.equal(await page.evaluate(() => window.sent.length), 2);
  } finally { await browser.close(); }
});

test('a refused change shows its sentence on the card, no answer says so and is checked again, and a waiting change offers nothing', {skip: !available && 'Playwright Chromium unavailable'}, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1120, height: 900}});
    await page.setContent('<main id="panel"></main>');
    await page.addStyleTag({path: path.join(__dirname, '../css/connectors.css')});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.coreData = value => value;
      const keyed = {id: 'keyed', kind: 'contract', name: 'Keyed', summary: 'Tells its agents whether a token is saved.', version: '1.0.0',
        delivery: 'downloadable', status: 'ready', note: null, can_set_up: true, setup: {kind: 'key', fields: [{id: 'token', label: 'Token', secret: true}]},
        agents: [{agent_id: 'agent-1', name: 'Ordubashy', role: 'ordubashy', on: true, in_effect: true}],
        offers: [{action: 'update', version: '1.0.1'}, {action: 'remove'}], result: null, restarting: false};
      window.listed = [keyed];
      window.reads = 0;
      window.api = async url => {
        if (url === '/root/browser-setup') return {kind: 'built_in', tool_installed: true, chrome_installed: true, ready: true, state: 'healthy', explanation: '', agents: []};
        if (url === '/root/browser-accounts') return {sites: []};
        window.reads += 1;
        return {platform: 'linux', clients: [], connectors: structuredClone(window.listed)};
      };
      window.answer = null;
      window.post = async () => window.answer();
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/extension-cards.js')});
    await page.addScriptTag({path: path.join(__dirname, '../js/connectors.js')});
    await page.evaluate(() => connectorsPanel.mount(document.querySelector('#panel'), {rows: true}));
    const card = page.locator('[data-cx-card="keyed"]');
    await card.locator('.cx-status').getByText('Ready').waitFor();
    // The removal choices open in place of the card's own Remove.
    await card.getByRole('button', {name: 'Remove', exact: true}).click();
    assert.equal(await card.locator('[data-ext-action="remove-choose"]').isVisible(), false);
    await card.locator('[data-ext-remove-cancel]').click();
    assert.equal(await card.locator('[data-ext-action="remove-choose"]').isVisible(), true);
    // A change refused before the restart is an ordinary answer: its sentence on the card, as a warning.
    await page.evaluate(() => { window.answer = async () => ({...window.listed[0], failed: true,
      result: 'Keyed was not updated. The downloaded bundle does not have the checksum the index gives.'}); });
    await card.getByRole('button', {name: 'Update to 1.0.1'}).click();
    const said = card.locator('[data-ext-result]');
    await said.getByText('Keyed was not updated. The downloaded bundle does not have the checksum the index gives.').waitFor();
    assert.equal(await said.getAttribute('data-tone'), 'warn');
    // No answer at all: the card says so, and the list is read again until Qorqbot answers.
    const before = await page.evaluate(() => window.reads);
    await page.evaluate(() => { window.answer = async () => { const failure = new Error("Qorqbot isn't responding."); failure.code = 'network_unavailable'; throw failure; }; });
    await card.getByRole('button', {name: 'Update to 1.0.1'}).click();
    await said.getByText('Qorqbot did not answer. It may be restarting. This card is checked again until it answers.').waitFor();
    assert.equal(await page.locator('[data-cx-error]').isVisible(), false, 'nothing at the top of the page');
    await page.evaluate(() => { window.listed = [{...window.listed[0], offers: [], restarting: true, result: 'Qorqbot restarts to apply this change.'}]; });
    await card.getByText('Qorqbot restarts to apply this change.').waitFor({timeout: 10000});
    assert.ok(await page.evaluate(() => window.reads) > before);
    // While its change waits for the restart the card offers nothing: no Disconnect, no switch.
    assert.equal(await card.locator('button, input').count(), 0);
  } finally { await browser.close(); }
});

test('lines saying Qorqbot does not answer go once it answers, and a change that failed at the start is a warning', {skip: !available && 'Playwright Chromium unavailable'}, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1120, height: 900}});
    await page.setContent('<main id="panel"></main>');
    await page.addStyleTag({path: path.join(__dirname, '../css/connectors.css')});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.coreData = value => value;
      const card = (id, result, failed) => ({id, kind: 'contract', name: id, summary: 'A test extension.', version: '1.0.0',
        delivery: 'downloadable', status: 'ready', note: null, can_set_up: false, setup: {kind: 'none', fields: []}, agents: [],
        offers: [{action: 'remove'}], result, failed, restarting: false});
      window.down = true;
      window.api = async url => {
        if (window.down) { const failure = new Error("Qorqbot isn't responding."); failure.code = 'network_unavailable'; throw failure; }
        if (url === '/root/browser-setup') return {kind: 'built_in', tool_installed: true, chrome_installed: true, ready: true, state: 'healthy', explanation: '', agents: []};
        if (url === '/root/browser-accounts') return {sites: []};
        return {platform: 'linux', clients: [], connectors: [
          card('tablenote', 'Table Note was not updated. Its tables could not be made.', true),
          card('network', 'Installed network 1.0.0. Qorqbot restarted.', false)]};
      };
      window.post = async () => { throw new Error('nothing is posted here'); };
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/extension-cards.js')});
    await page.addScriptTag({path: path.join(__dirname, '../js/connectors.js')});
    await page.evaluate(() => connectorsPanel.mount(document.querySelector('#panel'), {rows: true}));
    // Opened while Qorqbot restarts: the page and the website sign-ins say it does not answer.
    await page.locator('[data-cx-error]').getByText("Qorqbot isn't responding.").waitFor();
    // (The sign-ins line is checked by its text and its own hidden flag: its section waits for a first list.)
    await page.waitForFunction(() => { const line = document.querySelector('[data-cx-accounts-error]');
      return !line.hidden && line.textContent === "Qorqbot isn't responding."; });
    // Back: both lines go by themselves, without a click or a focus.
    await page.evaluate(() => { window.down = false; });
    await page.locator('[data-cx-error]').waitFor({state: 'hidden', timeout: 10000});
    await page.waitForFunction(() => document.querySelector('[data-cx-accounts-error]').hidden, null, {timeout: 10000});
    const failed = page.locator('[data-cx-card="tablenote"] [data-ext-result]');
    await failed.getByText('Table Note was not updated. Its tables could not be made.').waitFor();
    assert.equal(await failed.getAttribute('data-tone'), 'warn');
    assert.equal(await page.locator('[data-cx-card="network"] [data-ext-result]').getAttribute('data-tone'), 'info');
  } finally { await browser.close(); }
});

test('a refused sign-ins list is not read again, and an older answer never paints over a newer one', {skip: !available && 'Playwright Chromium unavailable'}, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1120, height: 900}});
    await page.setContent('<main id="panel"></main>');
    await page.addStyleTag({path: path.join(__dirname, '../css/connectors.css')});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.coreData = value => value;
      const card = result => ({id: 'network', kind: 'contract', name: 'network', summary: 'A test extension.', version: '1.0.0',
        delivery: 'downloadable', status: 'ready', note: null, can_set_up: false, setup: {kind: 'none', fields: []}, agents: [],
        offers: [{action: 'remove'}], result, failed: false, restarting: false});
      window.reads = {accounts: 0, list: 0};
      window.api = async url => {
        if (url === '/root/browser-setup') return {kind: 'built_in', tool_installed: true, chrome_installed: true, ready: true, state: 'healthy', explanation: '', agents: []};
        if (url === '/root/browser-accounts') {
          window.reads.accounts += 1;
          const failure = new Error('Only the owner can see website sign-ins.'); failure.code = 'forbidden'; failure.httpStatus = 403; throw failure;
        }
        // The first list answers late with what was true before; the second answers at once with what is true now.
        const read = ++window.reads.list;
        if (read === 1) { await new Promise(done => setTimeout(done, 1500)); return {platform: 'linux', clients: [], connectors: [card('Older answer.')]}; }
        return {platform: 'linux', clients: [], connectors: [card('Newer answer.')]};
      };
      window.post = async () => { throw new Error('nothing is posted here'); };
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/extension-cards.js')});
    await page.addScriptTag({path: path.join(__dirname, '../js/connectors.js')});
    await page.evaluate(() => { window.panel = connectorsPanel.mount(document.querySelector('#panel'), {rows: true}); window.panel.reload(); });
    const result = page.locator('[data-cx-card="network"] [data-ext-result]');
    await result.getByText('Newer answer.').waitFor();
    await page.waitForTimeout(6500);
    assert.equal(await result.textContent(), 'Newer answer.');
    // Qorqbot answered with a refusal: its sentence stays, and the list is not asked for again.
    assert.equal(await page.evaluate(() => window.reads.accounts), 1);
    assert.equal(await page.evaluate(() => document.querySelector('[data-cx-accounts-error]').textContent), 'Only the owner can see website sign-ins.');
  } finally { await browser.close(); }
});
