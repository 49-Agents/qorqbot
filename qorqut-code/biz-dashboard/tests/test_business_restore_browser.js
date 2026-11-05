// Isolated owner restore of an archived business; no live service or terminals.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const owner = '40000000-0000-4000-8000-000000000001';
const beta = { business_id: 'b0000000-0000-4000-8000-000000000002', slug: 'beta', name: 'Beta', revision: 4, archived_at: '2026-09-20T10:00:00Z' };
let businesses = [{ id: 'alpha', slug: 'alpha', name: 'Alpha', agents: [] }];
let archived = [beta], restores = [], failNext = true, listing = true;
const envelope = data => ({ status: 'ok', data });
const server = http.createServer(async (req, res) => {
  const route = new URL(req.url, 'http://localhost').pathname;
  if (route.startsWith('/api/')) {
    let raw = ''; for await (const chunk of req) raw += chunk;
    let body = [], status = 200;
    if (route === '/api/biz') body = businesses;
    if (route === '/api/session') body = envelope({ principal_id: owner, principal_kind: 'human', is_owner: true });
    if (route === '/api/me/tasks') body = { principal_id: owner, tasks: [] };
    if (route === '/api/me/attention') body = envelope({ businesses: [], urgent: [] });
    if (route === '/api/me/attention/deliveries') body = envelope({ items: [] });
    if (route === '/api/root/businesses/archived') {
      if (listing) body = envelope({ items: archived });
      else { status = 404; body = { status: 'error', error: { code: 'not_found', message: 'Endpoint is not registered' } }; }
    }
    if (route === '/api/root/business-restore') {
      const request = JSON.parse(raw);
      restores.push(request);
      if (failNext) {
        failNext = false; status = 409;
        body = { status: 'error', error: { code: 'workspace_conflict', message: 'The business agent folder is occupied; resolve it before restore' } };
      } else {
        archived = []; businesses = [...businesses, { id: beta.business_id, slug: 'beta', name: 'Beta', agents: ['business-agent', 'dev'] }];
        body = envelope({ business_id: beta.business_id, agent_id: 'agent-beta', display_name: 'Onbashy', restored: true,
          agents: [{ agent_id: 'agent-beta', role_id: 'business-agent', display_name: 'Onbashy', mode: 'create' },
                   { agent_id: 'agent-beta-dev', role_id: 'dev', display_name: 'Dev', mode: 'create' }] });
      }
    }
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(body)); return;
  }
  const file = path.resolve(root, '.' + (route === '/' ? '/index.html' : route));
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
  res.writeHead(200, { 'Content-Type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' });
  res.end(fs.readFileSync(file));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'],
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/#/global/overview`);
    await page.getByRole('heading', { name: 'Your projects' }).waitFor();
    const section = page.locator('#global-archived');
    await section.getByText('Beta', { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => state.bizs.length), 1);
    await section.getByRole('button', { name: 'Restore' }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByText('returns to the project menu with its resources', { exact: false }).waitFor();
    await dialog.getByText('No agent is restored and no session starts', { exact: false }).waitFor();
    assert.equal(await dialog.getByText(/Onbashy|Dev/).count(), 0);
    await dialog.getByRole('button', { name: 'Restore' }).click();
    // The failure stays terse; backend detail is for the logs.
    await dialog.locator('#business-restore-error').filter({ hasText: "Changed since you opened it. Refresh" }).waitFor();
    assert.equal(await dialog.getByText('The business agent folder is occupied', { exact: false }).count(), 0);
    assert.equal(restores.length, 1);
    assert.deepEqual(Object.keys(restores[0]).sort(), ['business_id', 'expected_revision', 'idempotency_key']);
    assert.equal(restores[0].business_id, beta.business_id);
    assert.equal(restores[0].expected_revision, 4);
    await dialog.getByRole('button', { name: 'Restore' }).click();
    await dialog.waitFor({ state: 'detached' });
    assert.equal(restores.length, 2);
    assert.equal(restores[1].idempotency_key, restores[0].idempotency_key, 'a retry reuses the same request key');
    await page.locator('.toast').filter({ hasText: 'Beta restored' }).waitFor();
    assert.equal(await page.getByText(/restored with/).count(), 0, 'restore names no agents');
    await page.waitForFunction(() => state.bizs.some(b => b.slug === 'beta'));
    await page.waitForFunction(() => document.querySelector('#global-archived')?.hidden === true);
    // A core without the archived listing shows no restore section.
    listing = false;
    await page.evaluate(() => renderTab());
    await page.getByRole('heading', { name: 'Your projects' }).waitFor();
    await page.waitForTimeout(100);
    assert.equal(await page.locator('#global-archived').isHidden(), true);
    assert.deepEqual(errors, []);
    console.log('Restore browser checks passed: archived listing, owner restore of business resources without agents, terse conflict, retry with the same key, menu refresh.');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
