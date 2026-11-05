// Isolated UI-boundary check: the owner dashboard offers no project creation. Everything here is a MOCK
// (a synthetic local server); it proves only what the dashboard renders and calls. It does NOT prove the
// host denies business_create to callers; that backend enforcement is qualified separately. No live service,
// terminal or provider is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const owner = '40000000-0000-4000-8000-000000000001';
let businesses = [];
const calls = [];
const envelope = data => ({ status: 'ok', data });
const server = http.createServer(async (req, res) => {
  const route = new URL(req.url, 'http://localhost').pathname;
  if (route.startsWith('/api/')) {
    let raw = ''; for await (const chunk of req) raw += chunk;
    calls.push(route);
    let body = [];
    if (route === '/api/biz') body = businesses;
    if (route === '/api/session') body = envelope({ principal_id: owner, principal_kind: 'human', is_owner: true });
    if (route === '/api/me/tasks') body = { principal_id: owner, tasks: [] };
    if (route === '/api/me/attention') body = envelope({ businesses: [], urgent: [] });
    if (route === '/api/me/attention/deliveries') body = envelope({ items: [] });
    if (route === '/api/root/businesses/archived') body = envelope({ items: [{ id: 'old-id', slug: 'old', name: 'Old', archived_at: '2026-01-02T00:00:00Z' }] });
    if (route === '/api/root/operations/dashboard_summary_get') body = envelope({ items: [] });
    res.writeHead(200, { 'Content-Type': 'application/json' });
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
  const base = `http://127.0.0.1:${server.address().port}/`;
  const assertAbsent = async label => {
    assert.equal(await page.getByRole('button', { name: /new project/i }).count(), 0, `${label}: no New project button`);
    assert.equal(await page.getByRole('form', { name: /new project/i }).count(), 0, `${label}: no creation form`);
    assert.equal(await page.locator('#project-new, #project-create, .project-create, #pc-name').count(), 0, `${label}: no creation elements`);
    assert.equal(await page.evaluate(() => typeof projectCreateMount + typeof projectCreate), 'undefinedundefined', `${label}: creation script not loaded`);
  };
  try {
    // Zero projects: usable, with concise guidance and no creation control.
    await page.goto(base + '#/global/overview');
    await page.getByRole('heading', { name: 'Your projects' }).waitFor();
    await page.getByText('No projects yet. Ask Ordubashy to create one.').waitFor();
    await assertAbsent('Global, zero projects');
    await page.getByRole('heading', { name: 'Archived projects' }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Restore' }).count(), 1, 'restore remains');

    // Populated: project cards still open the project; still no creation control.
    businesses = [{ id: 'p-tea', slug: 'tea', name: 'Tea', agents: [] }];
    await page.reload();
    await page.locator('[data-open-business="tea"]').waitFor();
    await assertAbsent('Global, populated');
    await page.locator('[data-open-business="tea"]').click();
    await page.waitForFunction(() => state.scope === 'business' && state.biz?.slug === 'tea' || location.hash === '#tea');

    assert.equal(calls.some(route => /business_create/.test(route)), false, 'the dashboard never calls business_create');
    assert.deepEqual(errors, []);
    console.log('Project creation absence checks passed (mocked UI boundary, Charts): no New project control, form, script or business_create call with zero and populated projects; restore and project navigation remain. The retained Project-management compatibility view is covered by tests/test_agent_pane_destinations_browser.js.');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
