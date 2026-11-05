#!/usr/bin/env node
// Real registered global profiles and tmux shells on a temporary root and socket. No Claude.
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
const core = process.env.BAZARLYK_CORE_ROOT || path.resolve(__dirname, '../../biz-core');
const dashboard = path.resolve(__dirname, '..');
const output = process.env.GLOBAL_SCREENSHOTS || path.join(__dirname, 'artifacts/global-agents');

(async () => {
  const child = spawn(process.env.TEST_PYTHON || 'python3', ['tests/global_agents_browser_fixture.py', '--dashboard-root', dashboard],
    {cwd: core, env: {...process.env, PYTHONPATH: '.'}, stdio: ['ignore', 'pipe', 'pipe']});
  let browser, stderr = '', checks = 0;
  child.stderr.on('data', data => { stderr += data; });
  try {
    const details = await new Promise((resolve, reject) => {
      let stdout = '';
      const timer = setTimeout(() => reject(new Error('Fixture timed out: ' + stderr)), 120000);
      child.stdout.on('data', data => {
        stdout += data;
        const line = stdout.split('\n').find(line => line.startsWith('{"url":'));
        if (line) { clearTimeout(timer); resolve(JSON.parse(line)); }
      });
      child.on('error', error => { clearTimeout(timer); reject(error); });
      child.on('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${stderr}`)); });
    });
    browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.setDefaultTimeout(20000);
    const requests = [], errors = [];
    page.on('pageerror', error => errors.push(error.stack || error.message));
    page.on('request', request => requests.push({url: request.url(), method: request.method(), body: request.postData()}));
    await page.route('**/*', route => route.request().url().startsWith(details.origin) ? route.continue() : route.abort());
    const row = id => page.locator(`[data-global-agent="${id}"]`);
    const terminalText = () => page.evaluate(() => {
      if (!_term) return '';
      return Array.from({length: _term.buffer.active.length}, (_, i) => _term.buffer.active.getLine(i).translateToString()).join('\n');
    });
    const shellReady = async () => page.waitForFunction(() => _termES?.readyState === EventSource.OPEN && document.querySelector('[data-global-agent][aria-pressed="true"] .ldot.live'));
    // Actual Global agents open a chat (not enabled in this fixture); their tmux shell is reachable only as an explicit diagnostic view.
    const chatReady = id => page.locator(`[data-global-chat="${id}"]`).waitFor();
    const diagnostic = async id => {
      await row(id).click(); await chatReady(id);
      await page.locator('[data-global-chat] .gchat-empty [data-terminal]').click();
      await page.waitForFunction(() => _termES?.readyState === EventSource.OPEN);
    };
    const check = async (label, action) => { await action(); checks++; console.log('ok ' + label); };

    await check('Global deep link opens only registered global agents and their shell', async () => {
      await page.goto(details.origin + '/#/global/agents');
      await row(details.applier_id).waitFor(); await chatReady(details.applier_id);
      assert.match(await page.locator('.gchat-empty').innerText(), /Chat is not enabled/);
      assert(!requests.some(r => /\/root\/terminal\//.test(r.url)), 'a Global agent opens no terminal');
      assert(!requests.some(r => r.method === 'POST' && /\/chat\/(runtime|messages)$/.test(r.url)), 'chat is never enabled or started automatically');
      assert.equal(await page.locator('[data-global-agent]').count(), 3, 'two registered Global agents and one Terminal');
      assert.deepEqual(await page.locator('.global-workspace-terminals .tt').allInnerTexts(), ['Terminal']);
      assert.equal(await page.locator('[data-business-agent], input[type=search], [data-pin-agent]').count(), 0, 'no business-agent lookup or pins');
      assert.equal(await page.evaluate(() => state.biz), null);
      assert.equal(await page.locator('#agentrows').count(), 0);
      assert.equal(await page.locator('#agpad').count(), 1);
      assert.equal(await page.locator('#global-agent-heading').count(), 0);
      assert.equal(await page.getByText('Type claude in the terminal', {exact:false}).count(), 0);
      assert(!requests.some(r => /\/api\/biz\/.*\/terminal/.test(r.url)));
      // xterm may answer terminal device queries with control sequences.
      assert(!requests.filter(r => /\/input$/.test(r.url)).some(r => /claude|printf/.test(r.body || '')));
    });
    await check('Working memory selects an explicit scope without moving the terminal or acknowledging inbox', async () => {
      const before = requests.length;
      await page.getByRole('tab', {name:'Working memory',exact:true}).click();
      await page.getByText('No handoff yet.', {exact:false}).waitFor();
      await page.locator('.wm-scope').selectOption('widget');
      await page.waitForFunction(() => document.querySelector('.wm-scope')?.value === 'widget' && document.querySelector('.wm-revision')?.textContent.includes('No handoff yet'));
      assert.equal(await page.evaluate(() => state.biz), null);
      assert.equal(await row(details.applier_id).getAttribute('aria-pressed'), 'true');
      assert(requests.slice(before).some(r => r.url.endsWith('/api/root/operations/working_memory_get')));
      assert(requests.slice(before).some(r => r.url.endsWith('/api/biz/widget/operations/working_memory_get')));
      assert(!requests.slice(before).some(r => /acknowledge|working_memory_update/.test(r.url)));
      await page.getByRole('tab', {name:'Agent inbox',exact:true}).click();
    });
    await check('Agent panes have no Tasks tab', async () => {
      const pane = page.locator('#agpad');
      assert.equal(await pane.getByRole('tab', {name:/tasks/i}).count(), 0);
      assert.equal(await pane.getByRole('tab', {name:'Agent inbox', exact:true}).getAttribute('aria-selected'), 'true');
    });
    await check('Workspace Terminal is a plain shell rooted at the Qorqut folder', async () => {
      await row('__qorqut_workspace_terminal__').click(); await shellReady();
      await page.locator('.xterm-helper-textarea').focus();
      await page.keyboard.type("printf 'WORKSPACE:%s\\n' \"$PWD\""); await page.keyboard.press('Enter');
      await page.waitForFunction(expected => Array.from({length: _term.buffer.active.length}, (_, i) =>
        _term.buffer.active.getLine(i).translateToString()).join('\n').includes('WORKSPACE:' + expected), details.workspace);
      assert(requests.some(r => r.url.endsWith('/root/workspace-terminal/input')));
      assert(requests.some(r => r.url.endsWith('/root/workspace-terminal/resize')));
      assert.equal(await page.locator('#agpad-toggle').count(), 0);
    });
    await check('Older Terminal 2/3 selections open the one Terminal and leave its old routes alone', async () => {
      const before = requests.length;
      for (const legacy of ['__qorqut_workspace_terminal_2__', '__qorqut_workspace_terminal_3__']) {
        await page.evaluate(value => localStorage.setItem('ag:scope:global', value), legacy);
        await page.reload(); await row('__qorqut_workspace_terminal__').waitFor(); await shellReady();
        assert.equal(await row('__qorqut_workspace_terminal__').getAttribute('aria-pressed'), 'true');
        assert.equal(await page.locator('.global-workspace-terminal').count(), 1);
      }
      assert(requests.slice(before).some(r => /\/root\/workspace-terminal\/(stream|resize)$/.test(r.url)));
      assert(!requests.slice(before).some(r => /\/root\/workspace-terminal\/[23]\//.test(r.url)), 'the retired Terminal 2/3 routes are not used');
    });
    await check('Keyboard input and resize target the selected global agent in its diagnostic terminal', async () => {
      await diagnostic(details.applier_id);
      await page.locator('.xterm-helper-textarea').focus();
      await page.keyboard.type("printf 'GLOBAL_UI_MARKER\\n'"); await page.keyboard.press('Enter');
      await page.waitForFunction(() => {
        const lines = Array.from({length: _term.buffer.active.length}, (_, i) => _term.buffer.active.getLine(i).translateToString());
        return lines.some(line => line.trim() === 'GLOBAL_UI_MARKER');
      });
      assert(requests.some(r => r.url.endsWith(`/root/terminal/${details.applier_id}/input`)));
      assert(requests.some(r => r.url.endsWith(`/root/terminal/${details.applier_id}/resize`)));
    });
    await check('Switching agents and reconnecting preserve separate sessions and scrollback', async () => {
      await diagnostic(details.second_id);
      assert(!(await terminalText()).includes('GLOBAL_UI_MARKER'));
      await diagnostic(details.applier_id);
      await page.waitForFunction(() => Array.from({length: _term.buffer.active.length}, (_, i) => _term.buffer.active.getLine(i).translateToString()).join('\n').includes('GLOBAL_UI_MARKER'));
      await page.locator('[data-agent-refresh]').click(); await page.waitForFunction(() => _termES?.readyState === EventSource.OPEN);
      await page.waitForFunction(() => Array.from({length: _term.buffer.active.length}, (_, i) => _term.buffer.active.getLine(i).translateToString()).join('\n').includes('GLOBAL_UI_MARKER'));
      await page.locator('.xterm-helper-textarea').focus();
      await page.keyboard.type("printf 'AFTER_RECONNECT\\n'"); await page.keyboard.press('Enter');
      await page.waitForFunction(() => Array.from({length: _term.buffer.active.length}, (_, i) => _term.buffer.active.getLine(i).translateToString().trim()).includes('AFTER_RECONNECT'));
      fs.mkdirSync(output, {recursive: true});
      await page.screenshot({path: path.join(output, 'desktop.png')});
    });
    await check('Leaving Global disconnects the mirror and retains its selected agent', async () => {
      await row(details.second_id).click(); await chatReady(details.second_id);
      await page.evaluate(() => selectBiz('widget'));
      await page.locator('#agentrows').waitFor();
      assert.equal(await page.locator('[data-global-agent]').count(), 0);
      await page.evaluate(() => selectGlobal('agents')); await row(details.second_id).waitFor(); await chatReady(details.second_id);
      assert.equal(await row(details.second_id).getAttribute('aria-pressed'), 'true');
      await page.reload(); await row(details.second_id).waitFor(); await chatReady(details.second_id);
      assert.equal(await row(details.second_id).getAttribute('aria-pressed'), 'true');
      await page.evaluate(() => goGlobalTab('costs'));
      await page.getByRole('heading', {name: 'Your projects'}).waitFor();
      assert.equal(await page.evaluate(() => _termES), null);
    });
    await check('Late roster responses cannot reopen a terminal after navigation', async () => {
      let release;
      await page.route('**/api/root/agents', async route => { await new Promise(resolve => { release = resolve; }); await route.continue(); });
      await page.evaluate(() => goGlobalTab('agents'));
      await page.getByText('Loading agents…').waitFor();
      while (!release) await new Promise(resolve => setTimeout(resolve, 10));
      await page.evaluate(() => goGlobalTab('costs'));
      const streams = requests.filter(r => r.url.endsWith('/stream')).length;
      release(); await page.unroute('**/api/root/agents');
      await page.waitForTimeout(300);
      assert.equal(requests.filter(r => r.url.endsWith('/stream')).length, streams);
      assert.equal(await page.evaluate(() => _termES), null);
    });
    await check('Load failures are visible and Retry recovers', async () => {
      await page.route('**/api/root/agents', route => route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({status: 'error', error: {message: 'Temporarily unavailable'}})}));
      await page.evaluate(() => goGlobalTab('agents'));
      await page.getByRole('alert').waitFor();
      assert.match(await page.getByRole('alert').innerText(), /Temporarily unavailable/);
      await page.unroute('**/api/root/agents'); await page.getByRole('button', {name: 'Retry', exact: true}).click();
      await row(details.second_id).waitFor(); await chatReady(details.second_id);
    });
    await check('Narrow layout keeps the terminal inside the viewport', async () => {
      await row('__qorqut_workspace_terminal__').click(); await shellReady();
      await page.setViewportSize({width: 720, height: 900}); await page.waitForTimeout(200);
      const box = await page.locator('.termpane').boundingBox();
      assert(box.width >= 300 && box.height >= 200 && box.x >= 0 && box.x + box.width <= 720 && box.y + box.height <= 901, JSON.stringify(box));
      await page.screenshot({path: path.join(output, 'narrow.png')});
    });
    await check('No JavaScript errors or automatic Claude commands', async () => {
      assert.deepEqual(errors, []);
      assert(!requests.filter(r => r.url.endsWith('/input')).some(r => /claude/.test(r.body || '')));
    });
    console.log(`${checks} Global agent browser checks passed`);
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null) await new Promise(resolve => {
      const timer = setTimeout(() => child.kill('SIGTERM'), 10000);
      child.once('exit', () => { clearTimeout(timer); resolve(); }); child.kill('SIGINT');
    });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
