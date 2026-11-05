// A transient failed presence read keeps the last successful agent presence dots; lost access clears them; a later success replaces them.
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage();
    await page.route('http://presence.test/**', route => route.fulfill({contentType: 'text/html', body: '<html></html>'}));
    await page.goto('http://presence.test/');
    await page.setContent('<span data-agent-presence="agent-1"></span>');
    for (const file of ['core']) await page.addScriptTag({path: path.join(__dirname, '../js', file + '.js')});
    await page.evaluate(() => {
      window.fixture = {failure: null, state: 'running', calls: 0};
      window.fetch = async () => {
        fixture.calls++;
        if (fixture.failure) return new Response(JSON.stringify({status: 'error', error: {code: fixture.failure.code, message: 'failed'}}), {status: fixture.failure.status});
        return new Response(JSON.stringify({status: 'ok', data: {agents: [{agent_id: 'agent-1', state: fixture.state, inbox: {state: 'idle'}}], next_cursor: null}}), {status: 200});
      };
    });
    await page.addScriptTag({path: path.join(__dirname, '../js/agent-fleet.js')});
    const dot = () => page.locator('[data-agent-presence]').getAttribute('data-state');
    await page.evaluate(() => agentNavRefresh());
    assert.equal(await dot(), 'running');
    await page.evaluate(() => {agentNavNext = 0; fixture.failure = {status: 503, code: 'temporarily_unavailable'}; return agentNavRefresh();});
    assert.equal(await page.evaluate(() => fixture.calls), 2);
    assert.equal(await dot(), 'running', 'a transient failure keeps the last successful presence');
    await page.evaluate(() => {agentNavNext = 0; fixture.failure = null; fixture.state = 'idle'; return agentNavRefresh();});
    assert.equal(await dot(), 'idle', 'the next success replaces it');
    await page.evaluate(() => {agentNavNext = 0; fixture.failure = {status: 403, code: 'forbidden'}; return agentNavRefresh();});
    assert.equal(await dot(), 'unknown', 'lost access clears the cached presence');
    console.log('Agent presence retention passed.');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
