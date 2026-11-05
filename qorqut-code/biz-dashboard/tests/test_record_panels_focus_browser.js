// The real Focus route for Story, Customer and Email Tasks: the Agents workspace, the Focus board, and one shared record
// workspace whose Chat is the fresh default and whose dashboard replaces the chat area. Only the network is synthetic:
// an isolated local port, no live service or agent session.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const api = require('./fixtures/record_panels_api');
const repo = path.resolve(__dirname, '..');
const artifacts = process.env.RECORD_PANEL_ARTIFACTS || path.resolve(repo, '..', 'screenshots');
const agent = '11111111-1111-4111-8111-111111111111';

// Focus cards over the Task records the shared Operations mock serves (business `alpha` and Global).
const cardOf = (id, label, taskType, business) => {
  const resolved = {type: 'task', id, business_id: business ? 'biz-1' : null, business_slug: business ? 'alpha' : null, business_name: business ? 'Alpha' : 'Global', label, task_type: taskType};
  return {id, revision: 1, label, state: 'open', reason: '', archived: false, created_by: agent,
    source: {type: 'task', id, business_id: resolved.business_id}, resolved_source: resolved,
    conversation: {type: 'task', id, business_id: resolved.business_id}, source_revision: 1, references: []};
};
const board = {revision: 1, status: 'Working', updated_at: new Date().toISOString(), stale: false, browser: null, activity: null, desktop_control_url: null,
  items: [cardOf('story-1', 'Spring campaign story', 'story', true), cardOf('cust-2', 'Marta Ruiz', 'customer', true), cardOf('mail-1', 'Pricing question thread', 'email', true), cardOf('plain-1', 'An ordinary task', null, true)]};
delete board.items[3].resolved_source.task_type;
const focusRequests = [];

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const reply = data => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({status: 'ok', data})); };
  if (url.pathname.endsWith('/operations/focus_get')) {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw || '{}'); focusRequests.push(body);
    if (body.conversation) return reply({item: {thread: {notes: [], participants: []}}});
    if (body.item_id) return reply({item: board.items.find(item => item.id === body.item_id)});
    return reply({...board, items: board.items});
  }
  // The Today area's own reads, with their real empty DTOs (no connected calendars).
  if (url.pathname.endsWith('/operations/calendar_calendars_list')) return reply({calendars: [], errors: []});
  if (url.pathname.endsWith('/calendar-statuses')) return reply({items: []});
  if (url.pathname.includes('/operations/')) { if (await api.handle(req, res, url)) return; return reply({}); }
  if (api.handled && await api.handle(req, res, url)) return;
  if (url.pathname.endsWith('/focus')) return reply(board);
  if (url.pathname.endsWith('/inbox')) return reply({items: [], subscriptions: []});
  if (url.pathname === '/api/session') return reply({principal_id: 'owner', principal_kind: 'human', display_name: 'Owner One', is_owner: true});
  if (url.pathname === '/api/root/agent-pins') return reply({initialized: true, pinned_agents: []});
  if (url.pathname === '/api/root/agents') return reply({agents: [{agent_id: agent, name: 'chief', display_name: 'Ordubashy', role: {id: 'ordubashy'}, scope: {kind: 'ecosystem', allowed_business_ids: [], global_capabilities: []}}]});
  if (url.pathname === `/api/root/agents/${agent}/browser-surfaces`) return reply({agent_id: agent, configured: true, browsers: [], desktop: null});
  if (url.pathname === '/api/root/terminals') { res.setHeader('Content-Type', 'application/json'); return res.end('[]'); }
  if (url.pathname.startsWith('/api/')) return reply({});
  if (url.pathname === '/') {
    res.setHeader('Content-Type', 'text/html');
    const index = fs.readFileSync(path.join(repo, 'index.html'), 'utf8');
    const styles = index.match(/<link rel="stylesheet"[^>]+>/g).join('\n');
    // The dashboard's own load order, so every shared global exists exactly as in the app.
    const need = new Set(['core', 'san-juan-time', 'task-state', 'task-snooze', 'tags', 'work-groups', 'relationships', 'context-source', 'tasks', 'task-progress', 'record-panel', 'record-workspace', 'global-tasks', 'agents', 'safe-links', 'chat-view', 'conversation', 'signoffs', 'agentpane', 'global-calendar', 'agent-focus', 'agent-browser', 'global-agents', 'global', 'context', 'store', 'agent-workspace', 'learning']);
    const scripts = [...index.matchAll(/<script src="\/js\/([^"]+)\.js"/g)].map(match => match[1]).filter(name => need.has(name));
    return res.end(`<!doctype html><html><head><meta charset="utf-8">${styles}</head><body>
      <div id="toasts"></div><nav hidden><div class="ritem" data-tab="progress"><svg viewBox="0 0 24 24"></svg></div><div class="ritem" data-tab="settings"><svg viewBox="0 0 24 24"></svg></div></nav>
      <div id="host" style="height:100vh;display:flex;min-width:0"></div>
      ${scripts.map(name => `<script src="/js/${name}.js"></script>`).join('')}
      <script>
      state.scope = 'global'; state.biz = null; state.globalTab = 'agents'; state.bizs = [{id: 'biz-1', slug: 'alpha', name: 'Alpha'}];
      window.storyRelatedChips = () => {}; window.broadcastButton = () => ''; window.wireBroadcastButton = () => {}; window.agentNavPaint = () => {}; window.agentNavRefresh = () => {};
      window.mountTerminal = () => {}; window.mountGlobalChat = () => ({dispose() {}}); window.agentViewDispose = () => {}; window.mountAgentView = () => {};
      window.workingMemoryMount = el => el.textContent = 'Working memory form'; window.sgAgentAttentionPaint = () => {};
      window.opened = [];
      window.tFetchTask = async (biz, id) => ({business: {slug: biz}, id, title: 'Task detail'});
      window.tDetail = (task, {host} = {}) => { window.opened.push(task.id); host.textContent = 'Existing task editor'; };
      renderGlobalAgents(document.querySelector('#host'));
      </script></body></html>`);
  }
  const file = path.join(repo, url.pathname);
  if (!file.startsWith(repo + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.svg') ? 'image/svg+xml' : 'text/css');
  res.end(fs.readFileSync(file));
});

const until = async (check, what) => { for (let i = 0; i < 200; i++) { if (await check()) return; await new Promise(r => setTimeout(r, 25)); } throw new Error('Timed out waiting for ' + what); };

(async () => {
  fs.mkdirSync(artifacts, {recursive: true});
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  try {
    const page = await browser.newPage({viewport: {width: 1900, height: 900}}), errors = [];
    page.setDefaultTimeout(10000);
    page.on('pageerror', error => { errors.push(error.message); console.error(error.message); });
    await page.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
    await page.goto(origin);
    await page.getByRole('tab', {name: 'Focus', exact: true}).click();
    const card = id => page.locator(`[data-card-id="${id}"]`);
    for (const id of ['story-1', 'cust-2', 'mail-1', 'plain-1']) await card(id).waitFor();
    // A successful page: the Today area settled without caught failures anywhere on the Focus board.
    await until(async () => !(await page.locator('.calendar-status').textContent()).includes('Loading'), 'the Today area settling');
    const failureText = () => page.evaluate(() => /Cannot read|undefined|TypeError|Something went wrong/i.test(document.querySelector('.agfocus').innerText));
    assert.equal(await failureText(), false, 'no caught failure is rendered on the Focus board');
    await page.evaluate(() => { window.nativeSession = document.querySelector('.global-agent-session'); });
    const nativeKept = () => page.evaluate(() => window.nativeSession === document.querySelector('.global-agent-session') && window.nativeSession.isConnected);
    const selected = id => card(id).evaluate(el => el.getAttribute('aria-expanded') === 'true');
    const dock = page.locator('.agfocus-dock');
    const regionOf = id => card(id).locator('.agfocus-region');   // hosts the record conversation before it moves to the dock
    const live = () => page.locator('.agfocus-dock .record-surface');
    const select = async id => { await card(id).locator('.ttl').click(); await until(() => selected(id), id + ' selected'); };

    // ---- A Story card: one workspace, Chat default, its dashboard replaces the chat area.
    await select('story-1');
    const pane = () => page.locator('.agfocus-dock .rw-record:not([hidden])');
    const view = pane();
    await view.getByText('Ordubashy line for story-1').waitFor();
    assert.equal(await dock.evaluate(el => el.classList.contains('agfocus-chat-overlay')), true, 'the workspace overlays the native Chat area');
    assert.equal(await page.locator('.global-agent-session').evaluate(el => el.inert), true, 'the native session is inert while the overlay is open');
    assert.deepEqual(await view.locator('.rw-tabs [role="tab"]').evaluateAll(nodes => nodes.map(node => node.getAttribute('aria-label'))),
      ['Story dashboard', 'Chat']);
    assert.equal(await view.locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true', 'Chat is the fresh default');
    const selectedTab = await view.locator('[data-rw-tab="chat"]').evaluate(node => {
      const style = getComputedStyle(node); return {shadow: style.boxShadow, background: style.backgroundColor};
    });
    assert.match(selectedTab.shadow, /0px 0px 0px 1px inset$/, 'the selected record tab is outlined on every edge');
    assert.equal(selectedTab.shadow.includes('-2px'), false, 'no shorter inset bottom line remains');
    assert.notEqual(selectedTab.background, 'rgba(0, 0, 0, 0)', 'the selected record tab keeps its background');
    assert.equal(await view.locator('.rw-dashboard').isVisible(), false);
    assert.equal(await page.locator('.agfocus-dock .record-toggle,.agfocus-dock .agfocus-thread-context,.agfocus-dock [data-manage]').count(), 0);
    await view.locator('.conversation-inline-compose textarea').fill('Story draft kept');
    await view.locator('[data-rw-tab="dashboard"]').click();
    await view.locator('[data-story-read]').waitFor();
    assert.equal(await view.locator('.rw-chat').isVisible(), false, 'dashboard and chat are never side by side');
    const geometry = await view.evaluate(el => { const dash = el.querySelector('.rw-dashboard').getBoundingClientRect(), body = el.querySelector('.rw-body').getBoundingClientRect(); return {fullWidth: Math.abs(dash.width - body.width) < 2}; });
    assert.deepEqual(geometry, {fullWidth: true}, 'the dashboard is full width inside the record workspace');
    assert.equal(await view.locator('[data-story-overview]').isVisible(), false, 'the Story reads until the owner chooses Edit');
    assert.equal(await view.locator('.record-child-list .record-child-row').count(), 2, 'the first child page renders as Focus-style rows');
    assert.equal(await view.locator('.record-child-list .record-child-row', {hasText: 'Plan the launch party'}).locator('.task-subtasks-toggle').count(), 1, 'a child Story offers expansion in place');
    await page.screenshot({path: path.join(artifacts, 'focus-route-story-wide.png')});
    // A child opens by pushing onto the same workspace, and Back returns to the Story dashboard.
    await view.locator('.record-child-list .record-child-row', {hasText: 'Draft the launch'}).click();
    const child = pane();
    await child.locator('.rw-title').filter({hasText: 'Draft the launch note'}).waitFor();
    assert.equal(await child.locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true', 'a followed record is a fresh Chat-first selection');
    await child.locator('[data-rw-tab="dashboard"]').click();
    await until(async () => (await page.evaluate(() => window.opened)).includes('child-1'), 'the child dashboard opened through the workspace');
    assert.equal(await child.locator('.rw-back').isVisible(), true);
    await child.locator('.rw-back').click();
    await view.locator('[data-story-read]').waitFor();
    assert.equal(await view.locator('[data-rw-tab="dashboard"]').getAttribute('aria-selected'), 'true', 'Back restores the Story dashboard tab');

    // ---- Switch to a Customer: its own Chat first, then its full-width dashboard.
    await select('cust-2');
    assert.equal(await selected('story-1'), false);
    await view.getByText('Ordubashy line for cust-2').waitFor();
    assert.equal(await dock.getByText('Ordubashy line for story-1').count(), 0, 'the Story conversation is not shown for the Customer');
    await view.locator('[data-rw-tab="dashboard"]').click();
    await view.locator('[data-customer-name]').waitFor();
    assert.equal(await view.locator('[data-customer-name]').inputValue(), 'Marta Ruiz');
    await view.getByRole('button', {name: 'Archive', exact: true}).click();
    await view.getByRole('button', {name: 'Unarchive', exact: true}).waitFor();
    assert.equal(api.db.alpha['cust-2'].archived, true);
    assert.equal(api.db.alpha['cust-2'].status, 'open', 'archiving never changes the Task status');
    await page.screenshot({path: path.join(artifacts, 'focus-route-customer-wide.png')});

    // ---- Email Task: its historical reference is the dashboard; the Chat composer stays in its own tab.
    await select('mail-1');
    await view.getByText('Ordubashy line for mail-1').waitFor();
    await view.locator('[data-rw-tab="dashboard"]').click();
    await view.locator('.rw-dashboard').getByText(/historical email reference/).waitFor();
    assert.equal(await view.locator('.rw-dashboard textarea').count(), 0, 'the reference has no nested composer');
    await view.locator('[data-rw-tab="chat"]').click();
    assert.equal(await view.locator('.conversation-inline-compose textarea').count(), 1, 'the Ordubashy composer stays in the Chat tab');

    // ---- A plain Task dashboard is the existing task editor, still inside the shared workspace.
    await select('plain-1');
    await view.locator('[data-rw-tab="dashboard"]').click();
    await view.locator('.rw-dashboard').getByText('Existing task editor').waitFor();
    assert.equal(await page.locator('.agfocus-dock .record-toggle').count(), 0);

    // ---- Back to the Story: Chat first with the draft kept, nothing from another record painted.
    await select('story-1');
    await view.getByText('Ordubashy line for story-1').waitFor();
    assert.equal(await view.locator('.rw-conversation .conversation-inline-compose textarea').inputValue(), 'Story draft kept', 'the Story draft survived the round trip');
    assert.equal(await page.locator('.agfocus-dock').getByText('Marta Ruiz').count(), 0, 'no Customer field is painted into the Story');

    // ---- Narrow: exactly one full-width pane, never a stack of two.
    await view.locator('[data-rw-tab="dashboard"]').click();
    await view.locator('[data-story-read]').waitFor();
    await page.setViewportSize({width: 390, height: 844});
    await page.waitForTimeout(150);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, 'no horizontal overflow at 390px');
    assert.equal(await view.locator('.rw-dashboard').isVisible(), true);
    assert.equal(await view.locator('.rw-chat').isVisible(), false, 'the dashboard occupies the full record width');
    await page.screenshot({path: path.join(artifacts, 'focus-route-story-narrow.png')});
    await page.setViewportSize({width: 1900, height: 900});

    // ---- Leaving Focus restores the native Chat area, untouched.
    await page.keyboard.press('Escape');
    await until(async () => !(await selected('story-1')), 'the selection closing');
    assert.equal(await page.locator('.global-agent-session').evaluate(el => el.inert), false, 'the native session is usable again');
    assert.equal(await nativeKept(), true, 'the native Chat area node was never replaced');
    assert.equal(await failureText(), false, 'no failure rendered at the end either');
    assert.deepEqual(errors, []);
    console.log('Focus route: shared record workspace with Chat default, full-width Story/Customer/Email/plain dashboards, child Back, drafts, narrow layout and native Chat preservation passed.');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
