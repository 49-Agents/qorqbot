// Linked ?workspace=<key> views: removed/unknown keys never create a tab; installed types honor late discovery. Synthetic mocked APIs only:
// no agent, terminal or live record is started or touched.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const scope = {kind: 'ecosystem', allowed_business_ids: [], global_capabilities: []};
const allProfiles = [['ordu', 'Ordubashy', 'ordubashy'], ['founder', 'Founder', 'founder'], ['worker', 'Worker', 'global-worker'], ['project', 'Project Agent', 'business-agent'], ['learning', 'Learning Applier', 'learning-applier']]
  .map(([agent_id, display_name, role]) => ({agent_id, name: agent_id, display_name, status: 'active', role: {id: role}, scope}));
let profiles = allProfiles;
const businesses = [{id: 'biz-a', slug: 'widget', name: 'Widget', agents: ['dev']}];
const columns = [{id: 'name', name: 'Name', type: 'text'}, {id: 'qty', name: 'Qty', type: 'text'}];
const tableFor = (id, title, extra = {}) => ({id, title, revision: 1, row_count: 1, columns, archived: false, append_only: false, tags: {}, description: '', ...extra});
const tables = {root: [tableFor('gt1', 'Global stock'), tableFor('gt2', 'Global ledger', {append_only: true})], widget: [tableFor('bt1', 'Widget orders')]};
const calls = [];
let typesGate = Promise.resolve(), typesFail = false, openGate = () => {};
const holdTypes = () => { typesGate = new Promise(resolve => { openGate = resolve; }); };
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  res.setHeader('X-Frame-Options', 'DENY');
  res.setHeader('Content-Security-Policy', "frame-ancestors 'none'");
  if (url.pathname.startsWith('/api/')) {
    let body = '';
    for await (const chunk of req) body += chunk;
    calls.push({path: url.pathname, body});
    let data = {items: [], messages: [], purposes: [], tags: [], agents: [], totals: {}, next_cursor: null}, raw = false;
    const op = (url.pathname.match(/\/operations\/(\w+)$/) || [])[1], owner = url.pathname.startsWith('/api/biz/widget/') ? 'widget' : 'root';
    if (url.pathname === '/api/biz') { data = businesses; raw = true; }
    else if (url.pathname === '/api/session') data = {principal_id: 'owner', principal_kind: 'human', is_owner: true, is_installation_owner: true};
    else if (url.pathname === '/api/root/agents') data = {agents: profiles};
    else if (url.pathname === '/api/root/agent-pins') data = {initialized: true, pinned_agents: []};
    else if (op === 'tables_list') data = {items: tables[owner], purposes: [], next_cursor: null};
    else if (op === 'tables_get') data = {table: tables[owner].find(t => t.id === JSON.parse(body).table_id)};
    else if (op === 'tables_rows_list') data = {items: [{id: 'r1', position: 0, cells: {name: 'Bolts', qty: '4', entry_id: 'e1'}}], table_revision: 1, next_cursor: null};
    else if (op === 'focus_get') data = {revision: 1, items: []};
    else if (op === 'workspace_types') {
      await typesGate;
      if (typesFail) { res.statusCode = 500; return res.end('{}'); }
      data = {types: [{key: 'focus', display_name: 'Focus', supported_roles: ['ordubashy'], renderer: 'focus_board'},
        {key: 'demo', display_name: 'Demo', supported_roles: ['someone-else'], renderer: 'focus_board'},
        {key: 'threads', display_name: 'Threads', supported_roles: allProfiles.map(p => p.role.id), renderer: 'threads'},
        {key: 'old_threads', display_name: 'Old Threads', supported_roles: allProfiles.map(p => p.role.id), renderer: 'threads'}]};
    }
    else if (op === 'calendar_calendars_list') data = {configured: false, calendars: [], errors: []};
    else if (/\/agents\/[^/]+\/chat$/.test(url.pathname)) data = {agent_id: url.pathname.split('/')[4], session: {state: 'live', reason: 'live', can_send: true, working: false, provider: 'claude', conversation_id: 'c', transport: 'terminal', detail: 'Live', binding: {binding_id: 'b', agent_id: 'a', provider: 'claude', conversation_id: 'c', generation: 1, transport: 'terminal'}}, revision: 0, cursor: 0, conversation: {conversation_id: 'c', provider: 'claude', complete: true, finalized: false, error: null, captured_length: 0}, conversations: [], entries: [], older: null, rendered_boundary: 0, inputs: [], last_read_sequence: 0, newest_sequence: 0};
    else if (url.pathname.endsWith('/browser-surfaces')) data = {surfaces: []};
    res.setHeader('Content-Type', 'application/json');
    return res.end(JSON.stringify(raw ? data : {status: 'ok', data}));
  }
  const file = path.resolve(root, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  try {
    const open = async (link, savedView = null) => {
      const context = await browser.newContext({viewport: {width: 1400, height: 900}}), page = await context.newPage(), errors = [], requests = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('request', request => requests.push(new URL(request.url()).pathname));
      // Records every tab key that ever appears, so a transient named tab cannot hide behind the final state.
      await page.addInitScript(savedView => {
        window.EventSource = class { addEventListener() {} close() {} };
        if (savedView) {
          const agent = new URLSearchParams(location.search).get('agent') || 'learning';
          localStorage.setItem('agpad:view:' + agent, savedView);
          history.replaceState({agentPane: {agentId: agent, view: savedView}}, '');
        }
        window.__tabsSeen = new Set();
        new MutationObserver(() => document.querySelectorAll('#agpad .ttab').forEach(tab => window.__tabsSeen.add(tab.dataset.v)))
          .observe(document, {subtree: true, childList: true, attributes: true});
      }, savedView);
      page.on('response', response => { if (/\.(js|css)$/.test(new URL(response.url()).pathname) && !response.ok()) errors.push('Missing asset: ' + response.url()); });
      await page.goto(origin + link);
      await page.locator('#agpad .ttab[aria-selected=true]').waitFor();
      return {context, page, errors, requests,
        seen: () => page.evaluate(() => [...window.__tabsSeen]),
        tab: () => page.locator('#agpad .ttab[aria-selected=true]').getAttribute('data-v')};
    };
    const finish = async ({context, errors, requests}) => {
      assert.deepEqual(errors, []);
      assert.deepEqual(requests.filter(path => /idea/i.test(path)), []);
      await context.close();
    };

    // 1. A removed key never creates a tab, even transiently, whether discovery is immediate, delayed or failing.
    for (const mode of ['immediate', 'delayed', 'failed']) {
      typesFail = mode === 'failed';
      if (mode === 'delayed') holdTypes();
      const view = await open('/?agent=ordu&workspace=ideas#/global/agents');
      assert.equal(await view.tab(), 'focus', mode + ': the default view stays usable');
      openGate();
      await view.page.waitForTimeout(500);
      assert.equal(await view.tab(), 'focus', mode);
      assert(!(await view.seen()).includes('ideas'), mode + ': no Ideas tab ever appeared');
      assert.equal(await view.page.locator('.agpad-unsupported').count(), 0, mode);
      await finish(view);
    }
    typesFail = false;

    // 2. A valid installed type whose declaration arrives late is honored once it arrives.
    holdTypes();
    let view = await open('/?agent=ordu&workspace=demo#/global/agents');
    assert.equal(await view.tab(), 'focus', 'default view while discovery is pending');
    assert(!(await view.seen()).includes('demo'));
    openGate();
    await view.page.locator('#agpad .ttab[data-v=demo][aria-selected=true]').waitFor();
    await view.page.locator('.agpad-unsupported').filter({hasText: "not available to this agent's role"}).waitFor();
    await finish(view);

    // 3. A key that discovery never installs is not honored (failed discovery uses the fallback).
    typesFail = true;
    view = await open('/?agent=ordu&workspace=demo#/global/agents');
    await view.page.waitForTimeout(500);
    assert.equal(await view.tab(), 'focus');
    assert(!(await view.seen()).includes('demo'));
    await finish(view);
    typesFail = false;

    // 4. The owner selecting another view while discovery is pending wins.
    holdTypes();
    view = await open('/?agent=ordu&workspace=demo#/global/agents');
    await view.page.locator('#agpad .ttab[data-v=status]').click();
    assert.equal(await view.tab(), 'status');
    openGate();
    await view.page.waitForTimeout(500);
    assert.equal(await view.tab(), 'status', 'late discovery does not override the owner');
    await finish(view);

    // 5. Selecting another agent while discovery is pending: the late key does not touch the new pane.
    holdTypes();
    view = await open('/?agent=ordu&workspace=demo#/global/agents');
    await view.page.locator('#global-agent-list').getByRole('button', {name: 'Learning Applier', exact: true}).click();
    await view.page.locator('#agpad .ttab[data-v=global_context][aria-selected=true]').waitFor();
    openGate();
    await view.page.waitForTimeout(500);
    assert.equal(await view.tab(), 'global_context');
    assert.equal(await view.page.locator('#agpad .ttab[data-v=demo]').count(), 0);
    await finish(view);

    // Retired Threads declarations, base views, saved selections and links never resurrect the screen.
    for (const mode of ['immediate', 'delayed', 'failed']) {
      typesFail = mode === 'failed';
      if (mode === 'delayed') holdTypes();
      for (const profile of allProfiles) {
        view = await open('/?agent=' + profile.agent_id + '&workspace=threads#/global/agents', 'threads');
        openGate();
        await view.page.waitForFunction(() => workspaceDeclarationAsked && !workspaceDeclarationPending);
        assert(!(await view.seen()).includes('threads'), mode + ': no Threads for ' + profile.agent_id);
        const actual = await view.page.evaluate(() => {
          const p = _agpad;
          p.workspaceBase.push('threads');
          agpadWorkspaceViewsSync(p);
          const requested = agpadRequestView(p, 'threads');
          agpadSetView(p, 'threads');
          return {requested, selected: p.view, views: p.views, types: workspaceTypes().map(t => t.key)};
        });
        assert.equal(actual.requested, false);
        assert.notEqual(actual.selected, 'threads');
        assert(!actual.views.includes('threads'));
        assert(!actual.types.includes('threads') && !actual.types.includes('old_threads'));
        assert.equal(await view.page.locator('.agpad-unsupported').count(), 0);
        await finish(view);
      }
    }
    typesFail = false;

    // Learning Applier uses the existing Context fallback; old Viewbox selections and shortcuts cannot mount it.
    for (const link of ['/?agent=learning&workspace=viewbox&viewbox_item=old#/global/agents', '/?agent=learning#/global/agents']) {
      view = await open(link, 'viewbox');
      await view.page.waitForFunction(() => workspaceDeclarationAsked && !workspaceDeclarationPending);
      assert.equal(await view.tab(), 'global_context');
      const attempted = await view.page.evaluate(() => {
        const p = _agpad, fallback = p.view;
        const views = agentPaneViews('global', 'learning-applier', ['threads', 'viewbox', 'inbox'], false, p.agentId);
        const requested = agpadRequestView(p, 'viewbox');
        agpadSetView(p, 'viewbox');
        agpadSurfaceRequest(p, 'viewbox');
        agpadSurfaceOpen(p, 'viewbox');
        return {views, requested, selected: p.view, fallback, surface: p.surface?.kind || null};
      });
      assert.equal(attempted.requested, false);
      assert.equal(attempted.selected, attempted.fallback);
      assert.equal(attempted.surface, null);
      assert(!attempted.views.includes('threads') && !attempted.views.includes('viewbox'));
      assert(!(await view.seen()).includes('viewbox'));
      assert.equal(await view.page.locator('[data-focus-surface="viewbox"],.agvb').count(), 0);
      assert(!view.requests.some(p => /agents\/learning\/viewbox$/.test(p)));
      await finish(view);
    }

    // Even a stale server granting Focus and Viewbox to Learning Applier cannot bypass the nested-surface guard.
    view = await open('/?agent=learning#/global/agents');
    await view.page.waitForFunction(() => workspaceDeclarationAsked && !workspaceDeclarationPending);
    await view.page.evaluate(() => {
      workspaceDeclaration.find(t => t.key === 'focus').supported_roles.push('learning-applier');
      workspaceDeclaration.push({key:'viewbox',supported_roles:['learning-applier'],renderer:'viewbox'});
      const p = _agpad;
      p.views = agentPaneViews(p.workspaceScope, p.workspaceRole, p.workspaceBase, false, p.agentId);
      return agpadSetView(p, 'focus');
    });
    await view.page.locator('.agfocus-surface').waitFor({state:'attached'});
    assert.equal(await view.page.evaluate(() => {
      const p = _agpad;
      p.views.push('viewbox'); // also exercise a stale pane/header shortcut.
      agpadSetView(p, 'viewbox');
      agpadSurfaceRequest(p, 'viewbox');
      agpadSurfaceOpen(p, 'viewbox');
      return p.view === 'focus' && !p.surface && !p.vb;
    }), true);
    assert.equal(await view.page.locator('[data-focus-surface="viewbox"],.agvb').count(), 0);
    await finish(view);

    view = await open('/?agent=ordu&workspace=viewbox#/global/agents');
    await view.page.locator('.agvb').waitFor();
    assert.equal(await view.page.evaluate(() => _agpad.views.includes('viewbox') && _agpad.surface?.kind === 'viewbox'), true);
    await finish(view);

    console.log('Linked workspace views: removed/unknown keys, late valid discovery, failure, view and agent changes, retired Threads and role-bound Viewbox passed.');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
