// Isolated archive Signoff and directory refresh; no live service or terminals.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const proposal = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/signoffs.json'))).data.items[0];
Object.assign(proposal, {
  review_business_id: null, operation_name: 'business_archive', title: 'Archive business: Alpha',
  attention: { urgent: true, notify: true, blocking: false },
});
const owner = '40000000-0000-4000-8000-000000000001';
let businesses = [{ id: 'alpha', slug: 'alpha', name: 'Alpha', agents: [] }];
let writes = 0;
const envelope = data => ({ status: 'ok', data });
const server = http.createServer(async (req, res) => {
  const route = new URL(req.url, 'http://localhost').pathname;
  if (route.startsWith('/api/')) {
    let body = [];
    if (route === '/api/biz') body = businesses;
    if (route === '/api/session') body = envelope({ principal_id: owner, principal_kind: 'human', is_owner: true });
    if (route === '/api/me/tasks') body = { principal_id: owner, tasks: [] };
    if (route === '/api/me/attention') body = envelope({ businesses: [], urgent: [] });
    if (route === '/api/me/attention/deliveries') body = envelope({ items: [] });
    if (route.endsWith('/live')) body = { sessions: [], requests: [], signoffs_pending: 1 };
    if (route === '/api/root/signoffs') body = envelope({
      review_business_id: null, items: [proposal], counts: { pending: proposal.state === 'pending' ? 1 : 0 },
      updated_cursor: String(proposal.status_revision), next_cursor: null,
    });
    if (route === '/api/root/signoffs/' + proposal.id) body = envelope({ signoff: proposal });
    if (route === '/api/root/signoffs/' + proposal.id + '/approve') {
      let raw = ''; for await (const chunk of req) raw += chunk;
      const request = JSON.parse(raw);
      assert.equal(request.expected_status_revision, proposal.status_revision);
      assert.equal(request.payload_hash, proposal.payload_hash);
      writes++;
      proposal.state = 'approved'; proposal.status_revision++; proposal.updated_sequence++;
      body = envelope({ signoff: proposal });
    }
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
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/#/global/overview`);
    await page.getByRole('heading', { name: 'Your projects' }).waitFor();
    await page.evaluate(() => { openTasks(true); tSetView('signoffs'); });
    const row = page.locator(`.sg-row[data-id="${proposal.id}"]`);
    await row.waitFor();
    assert.match(await row.innerText(), /Archive business: Alpha/);
    assert.equal(await page.evaluate(() => state.bizs.length), 1);
    await row.locator('.sg-approve').first().click();
    await row.waitFor({ state: 'detached' });
    assert.equal(writes, 1);
    assert.equal(await page.evaluate(() => state.bizs.length), 1, 'approval alone must not hide the business');
    await page.evaluate(() => { window.archivePanel = document.querySelector('#tbody'); });
    proposal.state = 'applied'; proposal.status_revision++; proposal.updated_sequence++;
    businesses = [];
    await page.evaluate(() => sgRefresh());
    await page.waitForFunction(() => state.bizs.length === 0);
    assert.deepEqual(await page.evaluate(() => [state.scope, tView, archivePanel === document.querySelector('#tbody')]), ['global', 'signoffs', true]);
    await page.evaluate(() => sgRefresh());
    await page.locator('#wsbtn').click();
    assert.deepEqual(await page.locator('.workspace-menu .workspace-name').allTextContents(), ['Global']);
    assert.equal(writes, 1);
    assert.deepEqual(errors, []);
    console.log('Archive browser checks passed: owner approval, delayed application, menu refresh, retained Signoff view.');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
