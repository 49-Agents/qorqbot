// Isolated ordinary Project notes in the business drawer; no live service.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const owner = '40000000-0000-4000-8000-000000000001', founder = '40000000-0000-4000-8000-00000000000f';
const dev = '40000000-0000-4000-8000-0000000000de';
const now = '2026-09-24T12:00:00Z';
// Real-shaped project UUIDs, distinct from their URL slugs: `target_business_id` must be the UUID, never the slug.
const ALPHA_ID = '5b0c1a52-3f0e-4d3a-9d44-7f6a2d8f1e11', BETA_ID = '9e2d7c40-81aa-4b6c-8f5e-2c1d0b3a6f77';
const note = (id, title, author, content = '') => ({ id, kind: 'business_owned_note', authorId: author, title, content,
  revision: 1, content_hash: 'sha256:' + id, date: '2026-09-24', pinned: false, statuses: {}, createdAt: now, updatedAt: now, metadata: { tags: [] } });
let notes = [note('n-strategy', 'Positioning direction', founder, 'Lead with teams'),
  note('n-marketing', 'Launch channels', owner, 'Reddit first'),
  note('n-engineering', 'Adopt typed migrations', dev, 'Adopt typed schema migrations')];
let isOwner = true, created = [], updates = [], lists = [], rootCalls = [], mutations = [];
const envelope = data => ({ status: 'ok', data });
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost'), route = url.pathname;
  if (route.startsWith('/api/')) {
    let raw = ''; for await (const chunk of req) raw += chunk;
    let body = [];
    if (route === '/api/biz') body = [{ id: ALPHA_ID, slug: 'alpha', name: 'Alpha', agents: ['business-agent'], agent_meta: {} }, { id: BETA_ID, slug: 'beta', name: 'Beta', agents: [], agent_meta: {} }];
    else if (route === '/api/session') body = envelope({ principal_id: owner, principal_kind: 'human', is_owner: isOwner });
    else if (route === '/api/root/notes') { rootCalls.push({ method: req.method, search: url.search, raw }); body = [{...note('journal', 'Personal journal', owner, '- A journal task'), kind: 'note'}]; }
    else if (route === '/api/root/agents') body = envelope({ agents: [{ agent_id: founder, name: 'founder', display_name: 'Founder', role: { id: 'founder' } }] });
    else if (route === '/api/me/tasks') body = { principal_id: owner, tasks: [] };
    else if (route === '/api/me/attention') body = envelope({ businesses: [], urgent: [] });
    else if (route === '/api/me/attention/deliveries') body = envelope({ items: [] });
    else if (route === '/api/biz/alpha/notes' && req.method === 'GET') {
      lists.push(url.search);
      body = url.searchParams.get('target_business_id') === ALPHA_ID && !url.searchParams.has('category') ? notes : { error: 'target required' };
    } else if (route === '/api/biz/alpha/notes' && req.method === 'POST') {
      const request = JSON.parse(raw); created.push(request); mutations.push({ path: route, request });
      const saved = note('n-new-' + created.length, request.title, owner);
      notes.push(saved); body = saved;
    } else if (route.startsWith('/api/biz/alpha/notes/') && req.method === 'POST') {
      const request = JSON.parse(raw), id = decodeURIComponent(route.split('/')[5]); updates.push({ id, request }); mutations.push({ path: route, request });
      const saved = notes.find(n => n.id === id);
      Object.assign(saved, { title: request.title ?? saved.title, content: request.content ?? saved.content, revision: saved.revision + 1 });
      body = saved;
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
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'],
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const base = `http://127.0.0.1:${server.address().port}/`;
  const open = async () => {
    await page.goto(base + '#alpha');
    await page.waitForFunction(() => state.biz === 'alpha' && state.session);
    await page.evaluate(() => { openTasks(true); tSetView('businessNotes'); });
  };
  const host = page.locator('#tbody');
  try {
    await open();
    assert.equal(await page.locator('.ttab[data-v="businessNotes"]').getAttribute('aria-label'), 'Project notes');
    assert.equal(await page.locator('.ttab[data-v="globalNotes"]').count(), 1, 'Global notes stay beside business notes');
    await host.getByText('Positioning direction').first().waitFor();
    assert.equal(await host.locator('[data-nt-category], .ntcats').count(), 0, 'no fixed category tabs');
    await host.getByText('Launch channels').first().waitFor();
    await host.getByText('Adopt typed migrations').first().waitFor();
    assert.ok(lists.length > 0 && lists.every(q => q === '?target_business_id=' + ALPHA_ID), 'every list sends only the selected project');
    // Creating a note sends the explicit project target and no category.
    await host.locator('#ntnew').click();
    await host.locator('#nttitle').waitFor();
    assert.deepEqual(created.at(-1), { title: 'Project note', target_business_id: ALPHA_ID });
    assert.equal(await host.locator('#ntvisibility').count(), 0, 'journal visibility is not a business note control');
    assert.match(await host.locator('#ntmeta').textContent(), /by you/);
    await host.locator('#ntdoc .ntline').first().fill('Notebook zoom sample');
    // Notebook shortcuts scale the paper only, without editing or replacing its selection.
    const zoom = () => host.locator('#ntlinewrap').evaluate(el => Number(getComputedStyle(el).zoom));
    const frame = () => page.evaluate(() => ['#nttitle', '#ntmeta', '#ntside'].map(selector => {
      const r = document.querySelector(selector).getBoundingClientRect();
      return [r.x, r.y, r.width, r.height];
    }));
    const beforeFrame = await frame();
    const beforeLine = await host.locator('#ntdoc .ntline').first().boundingBox();
    const beforeSelection = await page.evaluate(() => ntSelection());
    await host.locator('#ntdoc').press('Control+=');
    assert.equal(await zoom(), 1.1);
    assert.deepEqual(await frame(), beforeFrame, 'header and navigation keep their size and position');
    assert.ok((await host.locator('#ntdoc .ntline').first().boundingBox()).height > beforeLine.height);
    assert.deepEqual(await page.evaluate(() => ntSelection()), beforeSelection);
    await host.locator('#ntdoc').press('Control+Shift+=');
    assert.equal(await zoom(), 1.2);
    await host.locator('#ntdoc').press('Control+-');
    assert.equal(await zoom(), 1.1);
    await host.locator('#ntdoc').press('Control+0');
    assert.equal(await zoom(), 1);
    // Browser defaults are cancelled only for focus in the open notebook.
    const cancelled = selector => page.locator(selector).evaluate(el => {
      el.focus();
      return !el.dispatchEvent(new KeyboardEvent('keydown', {key: '-', ctrlKey: true, bubbles: true, cancelable: true}));
    });
    assert.equal(await cancelled('#ntsearch'), false);
    assert.equal(await cancelled('#nteditor'), true);
    assert.equal(await zoom(), .9);
    for (let i = 0; i < 20; i++) await host.locator('#nteditor').press('Control+-');
    assert.equal(await zoom(), .5);
    for (let i = 0; i < 20; i++) await host.locator('#nteditor').press('Control+=');
    assert.equal(await zoom(), 2);
    await host.locator('#nteditor').press('Control+0');
    assert.equal(await host.locator('#ntdoc').textContent(), 'Notebook zoom sample');
    await host.locator('#nttitle').fill('Channel plan');
    await page.evaluate(() => ntSaveNow());
    assert.equal(updates.at(-1).request.title, 'Channel plan');
    assert.equal(updates.at(-1).request.target_business_id, ALPHA_ID, 'mutations carry the project UUID, not the slug');
    assert.equal('category' in updates.at(-1).request, false);
    await host.locator('#ntdoc').press('Control+Alt+=');
    assert.equal(await host.locator('.ntrule').count(), 1, 'divider has a separate shortcut');
    await host.locator('#ntdoc').press('Control+z');
    await host.locator('#ntdoc').press('Control+=');
    await page.evaluate(async () => { const id = ntSel; await ntGoHome(); await ntSelect(id); });
    assert.equal(await zoom(), 1.1, 'reopening this note keeps its zoom');
    // Founder-authored note shows its recorded author; Dev is a historical author shown by name.
    await page.evaluate(devId => { state.agents = [{ agent_id: devId, name: 'dev', display_name: 'Dev', role_id: 'dev' }]; }, dev);
    await page.evaluate(() => ntGoHome());
    assert.doesNotMatch(await host.textContent(), /written by you and|Engineering decisions/);
    await host.getByText('Positioning direction').first().click();
    await host.locator('#nttitle').waitFor();
    assert.equal(await zoom(), 1, 'another note starts at its own zoom');
    assert.match(await host.locator('#ntmeta').textContent(), /by Founder/);
    assert.equal(await host.locator('#nttitle').evaluate(el => el.readOnly), false);
    await page.evaluate(() => ntGoHome());
    await host.getByText('Adopt typed migrations').first().click();
    await host.locator('#nttitle').waitFor();
    assert.match(await host.locator('#ntmeta').textContent(), /by Dev/);
    // A Story link to a project note opens it in the one Project notes list.
    await page.evaluate(() => ntGoHome());
    await page.evaluate(() => workOpenLink({ biz: 'alpha' }, { resource_type: 'documents', resource_id: 'n-marketing',
      current: { kind: 'business_owned_note', path: 'notes/2026-09-24-launch-channels.md' } }));
    await page.waitForFunction(() => document.querySelector('#nttitle')?.value === 'Launch channels');
    await page.evaluate(() => ntGoHome());
    await host.getByText('Positioning direction').first().click();
    await host.locator('#nttitle').waitFor();
    // Archive keeps the project captured when the dialog opened: switching the selected project meanwhile must not retarget it.
    const archiveId = await page.evaluate(() => ntSel);
    await page.evaluate(id => ntDeleteFlow(id), archiveId);
    await page.evaluate(() => { state.biz = 'beta'; });
    await page.locator('.modal .mfoot .primary').click();
    await page.waitForFunction(() => true);
    await page.waitForTimeout(200);
    const archived = mutations.find(m => m.path.endsWith(`/${archiveId}/delete`));
    assert.ok(archived, 'archive request was sent');
    assert.equal(archived.path, `/api/biz/alpha/notes/${archiveId}/delete`, 'archive stays on the captured project route');
    assert.equal(archived.request.target_business_id, ALPHA_ID, 'archive carries the captured project UUID, not beta');
    // An outstanding edit also keeps its captured project.
    await page.evaluate(id => { window.latePatch = ntPatch('alpha', id, { title: 'Late edit' }); state.biz = 'beta'; }, archiveId);
    await page.evaluate(() => latePatch);
    assert.equal(mutations.at(-1).request.target_business_id, ALPHA_ID, 'an edit started on alpha never retargets to beta');
    assert.equal(mutations.at(-1).path, `/api/biz/alpha/notes/${archiveId}`);
    // Upload sends the same UUID.
    await page.evaluate(() => { state.biz = 'alpha'; });
    await page.evaluate(async () => { await ntFetch(); await ntGoHome(); await ntSelect('n-strategy'); });
    await page.locator('#nttitle').waitFor();
    await page.evaluate(() => ntDoUpload('n-strategy'));
    await page.waitForTimeout(300);
    const uploaded = mutations.find(m => m.path.endsWith('/upload'));
    assert.ok(uploaded, 'upload request was sent');
    assert.equal(uploaded.request.target_business_id, ALPHA_ID, 'upload carries the project UUID');
    assert.ok(mutations.every(m => m.request.target_business_id === ALPHA_ID && !('category' in m.request)), 'every project-note mutation carries the UUID and no category');
    // Unknown project id: nothing is sent and no slug is substituted.
    const before = mutations.length;
    await page.evaluate(() => { state.bizs = state.bizs.filter(b => b.slug !== 'alpha'); });
    await page.evaluate(() => ntPatch('alpha', ntSel, { title: 'No id' }));
    assert.equal(mutations.length, before, 'a project without a known id sends no mutation');
    await page.evaluate(id => { state.bizs.push({ id, slug: 'alpha', name: 'Alpha', agents: [], agent_meta: {} }); }, ALPHA_ID);
    // Another human reads the notes but cannot write them.
    isOwner = false;
    await page.reload();
    await page.waitForFunction(() => state.biz === 'alpha' && state.session && state.session.is_owner === false);
    await page.evaluate(() => { openTasks(true); tSetView('businessNotes'); });
    await host.locator('#nttitle').waitFor();
    assert.equal(await host.locator('#nttitle').evaluate(el => el.readOnly), true);
    assert.equal(await host.locator('#ntdel').count() + await host.locator('#ntnew2').count() + await host.locator('#ntupload').count(), 0);
    assert.match(await host.locator('#ntmeta').textContent(), /Read only/);
    await host.locator('#ntdoc .ntline').first().click();
    await page.keyboard.press('Control+-');
    assert.equal(await zoom(), .9, 'read-only notebook can be focused and zoomed');
    await page.evaluate(() => ntGoHome());
    await host.getByText('Late edit').first().waitFor();
    assert.equal(await host.locator('#ntnew').count(), 0);
    await page.evaluate(() => selectGlobal('notes'));
    await host.getByText('Personal journal').first().click();
    await host.locator('#nttitle').waitFor();
    await host.locator('#ntdoc').press('Control+=');
    assert.equal(await zoom(), 1.1, 'personal journal uses the same content zoom');
    assert.equal(await host.locator('#ntdoc').textContent(), 'A journal task');
    assert.ok(rootCalls.length > 0 && rootCalls.every(c => !c.search.includes('target_business_id') && !c.raw.includes('target_business_id')), 'personal notes never target Project storage');
    assert.deepEqual(errors, []);
    console.log('Project notes browser checks passed: one list with explicit target_business_id, no category, author, read-only for non-owners, personal notes untouched.');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
