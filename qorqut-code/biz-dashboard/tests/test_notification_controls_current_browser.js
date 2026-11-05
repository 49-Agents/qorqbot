#!/usr/bin/env node
// The dashboard has no persistent background-process tray. The real entry page (index.html + main.js) starts against
// synthetic DTOs, loads no tray module, element or style, issues no tray-only status reads on a change event or a
// 60-second clock tick, and ordinary toasts keep their desktop and mobile gutters.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const requests = [], streams = new Set();
const owner = {principal_id: 'owner', principal_kind: 'human', display_name: 'Owner', is_owner: true};
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  requests.push(url.pathname + url.search);
  const json = (data, status = 200) => {res.statusCode = status; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data));};
  const ok = data => json({status: 'ok', data});
  if (url.pathname === '/') {res.setHeader('Content-Type', 'text/html'); return res.end(fs.readFileSync(path.join(root, 'index.html')));}
  if (url.pathname.endsWith('/events') || url.pathname.endsWith('/stream')) {
    res.writeHead(200, {'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache'}); res.write(': open\n\n');
    if (url.pathname === '/api/root/events') streams.add(res);
    return req.on('close', () => streams.delete(res));
  }
  if (url.pathname === '/api/session') return ok(owner);
  if (url.pathname === '/api/biz') return json([{id: 'b1', slug: 'alpha', name: 'Alpha'}]);
  if (url.pathname === '/api/root/reply-notifications') return ok({items: []});
  if (url.pathname === '/api/root/agents') return ok({agents: []});
  if (url.pathname === '/api/root/agent-pins') return ok({initialized: true, pinned_agents: []});
  if (url.pathname === '/api/root/terminals') return json([]);
  if (url.pathname === '/api/me/tasks') return ok({tasks: []});
  if (url.pathname === '/api/me/agents/activity') return ok({agents: [], next_cursor: null});
  if (url.pathname === '/api/root/attention-queue') return ok({items: []});
  if (url.pathname.startsWith('/api/')) return ok({items: [], agents: [], tasks: [], signoffs: [], current: null, queued: 0});
  const file = path.join(root, url.pathname);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) {res.statusCode = 404; return res.end();}
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'application/octet-stream');
  res.end(fs.readFileSync(file));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined});
  try {
    assert.equal(fs.existsSync(path.join(root, 'js/processes.js')), false, 'the tray module is deleted');
    assert.equal(fs.existsSync(path.join(root, 'css/processes.css')), false, 'the tray style is deleted');
    assert.doesNotMatch(fs.readFileSync(path.join(root, 'css/agent-workspace.css'), 'utf8'), /process-tray/, 'no tray selector remains');
    const page = await browser.newPage({viewport: {width: 900, height: 700}}), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {if (message.type() === 'error' && !/Failed to load resource/.test(message.text())) errors.push('console: ' + message.text());});
    await page.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
    await page.clock.install();
    await page.goto(origin);
    // Startup completed through main.js: businesses loaded, the owner session landed and the change stream is connected.
    await page.waitForFunction(() => state.bizs.length === 1 && state.session?.is_owner === true && typeof changeEvents !== 'undefined' && changeEvents !== null);
    await page.waitForFunction(() => document.querySelector('#content')?.children.length > 0);
    await until(() => streams.size === 1 && requests.includes('/api/root/reply-notifications'));
    assert.equal(await page.evaluate(() => typeof processTray), 'undefined', 'no tray module is defined');
    assert.equal(await page.locator('#process-tray, .process-tray, .process-item, .process-tray-toggle').count(), 0, 'no tray element exists');
    assert.equal(await page.getByRole('button', {name: /notifications/i}).count(), 0, 'no Notifications toggle exists');
    assert.equal(requests.some(p => /processes\.(js|css)$/.test(p)), false, 'the entry page requests no tray asset');
    assert.equal(await page.evaluate(() => ls('process-tray-hidden')), null);
    // A jobs_routines change and a 60-second clock tick make the app's own subscribers refresh, but none reads tray status.
    const start = requests.length;
    for (const stream of streams) stream.write(`data: ${JSON.stringify({kinds: ['jobs_routines'], business_id: null})}\n\n`);
    await page.clock.fastForward(61000);
    await page.clock.fastForward(61000);
    await new Promise(resolve => setTimeout(resolve, 300));
    assert.equal(requests.slice(start).some(p => /[?&]view=processes/.test(p)), false, 'no tray-only routine or Context status read');
    assert.equal(await page.locator('.toast').count(), 0, 'background status raises no popup');
    // Ordinary toasts still show in the corner, with the narrow-screen gutter.
    await page.evaluate(() => toast('Saved'));
    await page.locator('.toast').waitFor();
    const gutter = () => page.locator('#toasts').evaluate(el => {const style = getComputedStyle(el); return [style.right, style.bottom];});
    assert.deepEqual(await gutter(), ['20px', '20px']);
    await page.setViewportSize({width: 390, height: 700});
    assert.deepEqual(await gutter(), ['12px', '12px'], 'mobile toast gutter');
    assert.deepEqual(errors, [], 'startup and the change event raise no page errors');
    console.log('No background-process tray: real startup, no tray module/element/toggle/status reads, toasts keep desktop and mobile gutters.');
  } finally {await browser.close(); for (const stream of streams) stream.end(); await new Promise(resolve => server.close(resolve));}
})().catch(e => {console.error(e); process.exitCode = 1; server.close();});
async function until(check) {for (let i = 0; i < 100; i++) {if (check()) return; await new Promise(resolve => setTimeout(resolve, 50));} throw new Error('Timed out waiting for startup requests');}
