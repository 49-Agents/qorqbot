// Context lives in the Learning Applier's pane, Calendar, Costs and Tables in the shared drawer (Ordubashy and Learning Applier have no Business management); there is no main menu. Synthetic mocked APIs only:
// no agent, terminal or live record is started or touched.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const scope = {kind: 'ecosystem', allowed_business_ids: [], global_capabilities: []};
const allProfiles = [['ordu', 'Ordubashy', 'ordubashy'], ['founder', 'Founder', 'founder'], ['learning', 'Learning Applier', 'learning-applier'], ['market', 'Marketing', 'global-worker']]
  .map(([agent_id, display_name, role]) => ({agent_id, name: agent_id, display_name, status: 'active', role: {id: role}, scope}));
let profiles = allProfiles;
let businesses = [{id: 'biz-a', slug: 'widget', name: 'Widget', agents: ['dev']}];
const columns = [{id: 'name', name: 'Name', type: 'text'}, {id: 'qty', name: 'Qty', type: 'text'}];
const tableFor = (id, title, extra = {}) => ({id, title, revision: 1, row_count: 1, columns, archived: false, append_only: false, tags: {}, description: '', ...extra});
const tables = {root: [tableFor('gt1', 'Global stock'), tableFor('gt2', 'Global ledger', {append_only: true})], widget: [tableFor('bt1', 'Widget orders')]};
const calls = [];
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
    else if (op === 'tables_list') data = {items: tables[owner], next_cursor: null};
    else if (op === 'tables_get') data = {table: tables[owner].find(t => t.id === JSON.parse(body).table_id)};
    else if (op === 'tables_rows_list') data = {items: [{id: 'r1', position: 0, cells: {name: 'Bolts', qty: '4', entry_id: 'e1'}}], table_revision: 1, next_cursor: null};
    else if (op === 'focus_get') data = {revision: 1, items: []};
    else if (op === 'workspace_types') data = {types: [{key: 'focus', display_name: 'Focus', supported_roles: ['ordubashy'], renderer: 'focus_board'}, {key: 'campaign_review', display_name: 'Outreach', supported_roles: ['ordubashy'], renderer: 'campaign_review'}]};
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
    const page = await browser.newPage({viewport: {width: 1400, height: 900}}), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
    const selectedAgent = () => page.locator('#global-agent-list [aria-pressed=true]').getAttribute('aria-label');
    const selectedTab = () => page.locator('#agpad .ttab[aria-selected=true]').getAttribute('data-v');
    const frame = () => page.frameLocator('.agent-workspace-frame');
    const frameHash = () => page.frames().find(f => f !== page.mainFrame()).evaluate(() => location.hash);
    const frameAt = hash => page.waitForFunction(h => [...document.querySelectorAll('iframe')].some(f => { try { return f.contentWindow.location.hash === h; } catch (_) { return false; } }), hash);
    const open = async hash => { await page.evaluate(h => { location.hash = h; }, hash); };

    // 1. There is no main menu, workspace switcher or global rail entry.
    await page.goto(origin + '/#/global/agents');
    await page.locator('.agfocus').waitFor();
    assert.equal(await page.locator('#wsbtn').count(), 0, 'no workspace switcher');
    assert.equal(await page.locator('[data-global-tab], .global-rail, .workspace-menu-anchor').count(), 0);

    // 1b. Stored Ideas destinations fall back through the generic routing. Each case is a fresh page load in its own
    // browser context, recording every request from the first navigation, so deleted assets and APIs cannot be fetched.
    const staleLoad = async (label, {url = '/', storage = {}, historyState = null}, check) => {
      const context = await browser.newContext({viewport: {width: 1400, height: 900}}), stale = await context.newPage();
      const requests = [], staleErrors = [];
      stale.on('request', request => requests.push(new URL(request.url()).pathname));
      stale.on('pageerror', error => staleErrors.push(error.message));
      await stale.addInitScript(([items, entry]) => {
        window.EventSource = class { addEventListener() {} close() {} };
        if (location.pathname === '/' && !sessionStorage.getItem('stale-seeded')) {
          sessionStorage.setItem('stale-seeded', '1');
          for (const [key, value] of Object.entries(items)) localStorage.setItem(key, value);
          if (entry) history.replaceState(entry, '');
        }
      }, [storage, historyState]);
      await stale.goto(origin + url);
      await check(stale, label);
      await stale.waitForTimeout(400);
      assert.equal(await stale.locator('[data-global-context=ideas], [data-v=ideas], .ideas-page').count(), 0, label + ': no Ideas entry');
      assert.equal(await stale.evaluate(() => typeof renderIdeas + typeof ideaDrafts), 'undefinedundefined', label);
      assert.deepEqual(requests.filter(path => /idea/i.test(path)), [], label + ': no Ideas asset or API request');
      assert.deepEqual(staleErrors, [], label + ': no script errors');
      await context.close();
    };
    const overviewFallback = async (stale, label) => {
      // There is no Overview page: the generic default is the Agents landing.
      await stale.locator('.agfocus').waitFor({timeout: 5000});
      assert.equal(await stale.evaluate(() => state.scope === 'global' && state.globalTab), 'agents', label + ': Agents fallback');
    };
    const contextGraphFallback = async (stale, label) => {
      const inner = stale.frameLocator('.agent-workspace-frame');
      await inner.locator('.global-context').waitFor();
      assert.equal(await inner.locator('[data-global-context=graph]').getAttribute('aria-current'), 'page', label + ': Graph selected');
      await inner.locator('.global-context-graph-view').waitFor();
      assert.equal(await inner.locator('[data-global-context=ideas]').count(), 0);
      assert.equal(await stale.locator('#global-agent-list [aria-pressed=true]').getAttribute('aria-label'), 'Learning Applier', label);
      assert.equal(await stale.locator('#agpad .ttab[aria-selected=true]').getAttribute('data-v'), 'global_context', label);
    };
    const orduFocusFallback = async (stale, label) => {
      await stale.locator('.agfocus').waitFor();
      assert.equal(await stale.locator('#global-agent-list [aria-pressed=true]').getAttribute('aria-label'), 'Ordubashy', label);
      assert.equal(await stale.locator('#agpad .ttab[aria-selected=true]').getAttribute('data-v'), 'focus', label);
    };
    await staleLoad('top-level ideas', {url: '/#/global/ideas'}, (p, l) => overviewFallback(p, l));
    await staleLoad('top-level idea record', {url: '/#/global/ideas/idea-1'}, (p, l) => overviewFallback(p, l));
    await staleLoad('context ideas', {url: '/#/global/context/ideas'}, contextGraphFallback);
    await staleLoad('context idea record', {url: '/#/global/context/ideas/idea-1'}, contextGraphFallback);
    await staleLoad('saved route and tab', {storage: {'global-tab': 'ideas', 'workspace-route': '#/global/ideas'}}, (p, l) => overviewFallback(p, l));
    await staleLoad('saved context route', {storage: {'global-tab': 'context', 'workspace-route': '#/global/context/ideas/idea-1'}}, contextGraphFallback);
    await staleLoad('saved history agent pane descriptor', {url: '/#/global/agents', historyState: {agentPane: {agentId: 'ordu', view: 'ideas', route: 'ideas'}}}, orduFocusFallback);

    // 2. Old Context URLs open the Learning Applier's Context tab at the requested sub-route.
    await open('/global/context/people');
    await frame().locator('.global-context .ctxtree').waitFor();
    assert.equal(await selectedAgent(), 'Learning Applier');
    assert.equal(await selectedTab(), 'global_context');
    assert.equal(await page.locator('#agpad .ttab[data-v=global_context]').getAttribute('aria-label'), 'Context');
    assert.equal(await page.evaluate(() => location.hash), '#/global/agents', 'the retired URL is replaced');
    assert.equal(await frameHash(), '#/global/context/people');
    assert.deepEqual(await frame().locator('[data-global-context]').evaluateAll(b => b.map(x => x.getAttribute('aria-label'))),
      ['Graph', 'People', 'Learning', 'Filed context']);
    // Refresh keeps the Context sub-route.
    await page.reload();
    await frame().locator('.global-context').waitFor();
    await frameAt('#/global/context/people');
    assert.equal(await selectedAgent(), 'Learning Applier');
    // 3. Tables is a shared-drawer tab (no agent selection needed): Global by default, scope picker, exact record/mode/filters.
    const drawer = () => page.frameLocator('#taskspanel iframe[data-workspace-host=drawer-tables]');
    const drawerQuery = () => page.evaluate(() => Object.fromEntries(new URLSearchParams(location.search)));
    const picker = () => page.locator('#taskspanel .ov-tables-scope select');
    const tabs = () => page.locator('#taskspanel .ttab').evaluateAll(t => t.map(x => x.dataset.v));
    await open('/global/tables');
    await drawer().locator('.data-workspace').waitFor();
    assert.equal(await selectedAgent(), 'Learning Applier', 'the selected agent is unchanged');
    assert.equal(await page.locator('#taskspanel .ttab[data-v=tables][aria-selected=true]').count(), 1);
    assert.equal(await page.locator('#agpad .ttab[data-v=tables]').count(), 0, 'no agent Tables tab');
    assert.deepEqual((await tabs()).filter(v => ['reports', 'tables', 'activity'].includes(v)), ['tables', 'activity']);
    assert.equal(await picker().inputValue(), 'global');
    assert.equal(await page.evaluate(() => location.hash), '#/global/agents', 'the retired URL is replaced');
    assert.deepEqual(await drawerQuery(), {drawer: 'tables', table_scope: 'global'});
    assert.equal(await page.locator('[data-business-section=tables]').count(), 0);
    await drawer().locator('.data-item[data-id=gt1]').click();
    await drawer().locator('#data-detail .data-grid').waitFor();
    assert.match(await drawer().locator('#data-detail h1').textContent(), /Global stock/);
    assert.equal((await drawerQuery()).table_id, 'gt1');
    // Reload restores the exact record; Back/Forward restore their stored entry.
    await page.reload();
    await drawer().locator('#data-detail h1').filter({hasText: 'Global stock'}).waitFor();
    assert.deepEqual(await drawerQuery(), {drawer: 'tables', table_scope: 'global', table_id: 'gt1'});
    assert.equal(await selectedAgent(), 'Learning Applier');
    // A narrow screen keeps the picker and the native screen inside the viewport.
    await page.setViewportSize({width: 390, height: 800});
    const inside = async locator => { const box = await locator.boundingBox(); return box && box.width > 0 && box.x >= 0 && box.x + box.width <= 391; };
    assert.equal(await inside(picker()) && await inside(page.locator('#taskspanel iframe[data-workspace-host=drawer-tables]')), true);
    await page.setViewportSize({width: 1400, height: 900});
    // An unsaved edit blocks tab switches, closing and scope changes until it is saved or discarded; same-view syncs keep it.
    const cell = drawer().locator('.data-grid tbody tr:first-child [data-column="name"]');
    await cell.locator('.data-cell').click();
    await cell.locator('[data-cell-input]').fill('Nuts');
    await drawer().locator('#data-save').waitFor();
    await page.evaluate(() => tSetView('tables'));
    assert.equal(await drawer().locator('[data-cell-input]').inputValue(), 'Nuts', 'a same-view sync keeps the mounted frame and its draft');
    const refuse = async action => { page.once('dialog', dialog => dialog.dismiss()); await action(); await page.waitForTimeout(200); };
    await refuse(() => page.locator('#taskspanel .ttab[data-v=costs]').click());
    assert.equal(await page.evaluate(() => tView), 'tables', 'cancelling a tab switch keeps the editor');
    await refuse(() => page.keyboard.press('Escape'));
    assert.equal(await page.evaluate(() => tPanelOpen()), true, 'cancelling a close keeps the drawer');
    await refuse(() => picker().selectOption('widget'));
    assert.equal(await picker().inputValue(), 'global', 'cancelling a scope change keeps the scope');
    assert.equal(await drawer().locator('[data-cell-input]').inputValue(), 'Nuts');
    assert.deepEqual(await drawerQuery(), {drawer: 'tables', table_scope: 'global', table_id: 'gt1'});
    page.once('dialog', dialog => dialog.accept());
    await picker().selectOption('widget');
    await picker().evaluate(select => select.value === 'widget');
    await drawer().locator('.data-item[data-id=bt1]').waitFor();
    assert.equal(await drawer().locator('[data-cell-input]').count(), 0, 'a confirmed discard clears the draft');
    assert.deepEqual(await drawerQuery(), {drawer: 'tables', table_scope: 'widget'});
    // A business record and mode survive reload exactly; the scope is remembered for the next normal opening.
    await page.evaluate(() => tablesOpen('widget', 'bt1', 'tables'));
    await drawer().locator('#data-detail h1').filter({hasText: 'Widget orders'}).waitFor();
    assert.deepEqual(await drawerQuery(), {drawer: 'tables', table_scope: 'widget', table_id: 'bt1'});
    await page.reload();
    await drawer().locator('#data-detail h1').filter({hasText: 'Widget orders'}).waitFor();
    assert.equal(await picker().inputValue(), 'widget');
    assert.equal(await drawer().locator('#data-department').count(), 0, 'no Department filter');
    await page.locator('#taskspanel .ttab[data-v=charts]').click();
    assert.deepEqual(Object.keys(await drawerQuery()).filter(k => k.startsWith('table_')), [], 'table fields leave with the tab');
    await page.locator('#taskspanel .ttab[data-v=tables]').click();
    await drawer().locator('.data-workspace').waitFor();
    assert.equal(await picker().inputValue(), 'widget', 'the Tables tab opens the remembered drawer scope');
    // A cold start on a bare link resolves the remembered scope only after the directory loads.
    await page.goto(origin + '/?drawer=tables#/global/agents');
    await drawer().locator('.data-workspace').waitFor();
    assert.equal(await picker().inputValue(), 'widget');
    assert.equal((await drawerQuery()).table_scope, 'widget');
    // Closing releases the frame; reopening restores the selection scope, not editor contents.
    await page.evaluate(() => openTasks(false));
    assert.equal(await page.locator('#taskspanel iframe').count(), 0);
    // An unknown scope is unavailable, never the same record in another scope.
    await page.goto(origin + '/?drawer=tables&table_scope=nowhere&table_id=gt1#/global/agents');
    await page.getByRole('alert').filter({hasText: 'unavailable'}).waitFor();
    assert.equal(await page.locator('#taskspanel iframe').count(), 0);
    await page.goto(origin + '/?drawer=tables&table_id=gt1#/global/agents');
    await page.getByRole('alert').filter({hasText: 'unavailable'}).waitFor();
    await page.goto(origin + '/?workspace=tables#/global/agents');
    await drawer().locator('.data-workspace.data-global').waitFor();
    assert.equal(await picker().inputValue(), 'global', '?workspace=tables opens Global');
    await page.evaluate(() => openTasks(false));
    // A business table link from the outer Focus shell opens that business's record in the drawer; the agent stays.
    await page.evaluate(() => tablesOpen('widget', 'bt1'));
    await drawer().locator('#data-detail h1').filter({hasText: 'Widget orders'}).waitFor();
    assert.equal(await selectedAgent(), 'Learning Applier');
    assert.equal(await page.locator('.agent-workspace-frame[data-workspace-host=drawer-tables]').count(), 1);
    // A late report from a removed drawer frame neither reaches the agent pane nor changes the descriptor.
    const stale = await page.evaluate(() => {
      const frame = document.querySelector('iframe[data-workspace-host=drawer-tables]'), child = frame.contentWindow;
      const report = child.eval('agentWorkspaceReportParent'); let leaked = 0; const agentReport = window.agentWorkspaceReport; window.agentWorkspaceReport = () => { leaked++; };
      document.querySelector('#taskspanel iframe').remove();
      report({table: 'other', mode: 'uploads'});
      drawerTablesReport(window, {table: 'other'});   // a window that is not the live frame
      window.agentWorkspaceReport = agentReport;
      return leaked;
    });
    assert.equal(stale, 0);
    assert.equal((await drawerQuery()).table_id, 'bt1');
    await page.evaluate(() => openTasks(false));

    // Old Calendar URL and ?workspace=calendar open the shared drawer's Calendar tab; no calendar agent tab exists, and the agent stays.
    await open('/global/calendar');
    await page.locator('#taskspanel .ttab[data-v=calendar][aria-selected=true]').waitFor();
    assert.equal(await selectedAgent(), 'Learning Applier');
    assert.equal(await page.locator('#agpad .ttab[data-v=calendar]').count(), 0);
    assert.equal(await page.evaluate(() => new URLSearchParams(location.search).get('drawer')), 'calendar');
    assert.deepEqual(Object.keys(await drawerQuery()).filter(k => k.startsWith('table_')), []);
    await page.evaluate(() => openTasks(false));
    // No role offers the retired Project management or Paper Outreach entries; Learning Applier keeps Context only.
    const agentTabs = () => page.locator('#agpad .ttab').evaluateAll(t => t.map(x => x.dataset.v));
    await page.locator('[data-global-agent=ordu]').click();
    await page.locator('#agpad .ttab[data-v=focus]').waitFor();
    assert.equal((await agentTabs()).includes('business'), false, 'Ordubashy has no Project management entry');
    assert.equal((await agentTabs()).includes('campaign_review'), false, 'a declared Paper Outreach type adds no pane entry');
    assert.deepEqual(await page.evaluate(() => ['system_dev', 'incidents', 'business', 'campaign_review'].map(view => [agpadRequestView(_agpad, view), _agpad.views.includes(view)])),
      [[false, false], [false, false], [false, false], [false, false]], 'retired view keys never add a pane view');
    assert.equal((await agentTabs()).some(v => ['system_dev', 'incidents', 'business', 'campaign_review'].includes(v)), false);
    // A stale p.views array cannot be used to select or mount a retired view.
    const staleFrameCount = await page.locator('.agent-workspace-frame').count();
    const staleView = await page.evaluate(() => _agpad.view);
    await page.evaluate(() => {
      _agpad.views = [...new Set([..._agpad.views, 'business', 'campaign_review', 'threads'])];
      for (const view of ['business', 'campaign_review', 'threads']) agpadSetView(_agpad, view);
      agpadWorkspaceMount(_agpad, 'business');
    });
    assert.equal(await page.evaluate(() => _agpad.view), staleView, 'a stale selected view never replaces the current tab');
    assert.equal(await page.locator('.agent-workspace-frame').count(), staleFrameCount, 'a stale selected view mounts no workspace');
    assert.equal(await page.evaluate(() => agpadHeadViews(_agpad).some(view => ['business', 'campaign_review', 'threads'].includes(view))), false,
      'stale views add no header tab');
    assert.equal(await page.evaluate(() => _agpad.views.includes('viewbox') && agentPaneViewAllowed('ordubashy', 'viewbox')), true,
      'Ordubashy keeps Viewbox');
    await page.locator('[data-global-agent=learning]').click();
    await page.locator('#agpad .ttab[data-v=global_context]').waitFor();
    assert.equal((await agentTabs()).includes('business'), false);
    assert.equal(await page.evaluate(() => agpadRequestView(_agpad, 'business')), false, 'Learning Applier cannot be given the pane by a direct request');
    assert.equal(await page.evaluate(() => agpadRequestView(_agpad, 'viewbox')), false, 'Learning Applier cannot be given Viewbox by a direct request');
    assert.deepEqual(await page.evaluate(() => {
      _agpad.views = [...new Set([..._agpad.views, 'viewbox'])];
      const before = _agpad.view;
      agpadSetView(_agpad, 'viewbox');
      return {header: agpadHeadViews(_agpad).includes('viewbox'), changed: _agpad.view !== before};
    }), {header: false, changed: false}, 'a stale Viewbox view adds no tab and cannot replace Context');
    await page.evaluate(() => { _agpad.views = _agpad.views.filter(view => view !== 'viewbox'); });
    await page.locator('[data-global-agent=market]').click();
    await page.locator('[data-global-agent=market][aria-pressed=true]').waitFor();
    assert.equal((await agentTabs()).includes('business'), false, 'a general worker has no Project management entry');
    assert.notEqual(await page.locator('#agpad .ttab[aria-selected=true]').getAttribute('data-v'), 'business');
    assert.equal(await page.locator('#agpad .ttab[data-v=tables]').count(), 0);
    // A stale Project management descriptor is dropped and the pane opens its normal first view.
    await page.evaluate(() => history.replaceState({agentPane: {agentId: 'ordu', view: 'business'}}, '', '/#/global/agents'));
    await page.reload();
    await page.locator('#global-agent-list [aria-label="Ordubashy"][aria-pressed=true]').waitFor();
    await page.locator('#agpad .ttab[data-v=focus]').waitFor();
    assert.equal(await page.evaluate(() => history.state?.agentPane || null), null, 'the stale Project management descriptor is cleared');
    assert.equal(await page.locator('#agpad .ttab[data-v=business]').count(), 0);
    assert.equal(await page.evaluate(() => _agpad.view), 'focus', 'the pane falls back to Focus');
    // Business descriptors for an agent without Project management (Learning Applier): a table selection lands in the drawer at its exact scope; anything else in the role's own default.
    const retire = async (agentPane, agentName) => {
      await page.evaluate(agentPane => history.replaceState({agentPane}, '', '/#/global/agents'), agentPane);
      await page.reload();
      await page.locator(`#global-agent-list [aria-label="${agentName}"][aria-pressed=true]`).waitFor();
    };
    await retire({agentId: 'learning', view: 'business', section: 'tables', scope: 'widget', table: 'bt1', mode: 'tables'}, 'Learning Applier');
    await drawer().locator('#data-detail h1').filter({hasText: 'Widget orders'}).waitFor();
    assert.equal(await picker().inputValue(), 'widget');
    assert.equal(await page.evaluate(() => history.state?.agentPane || null), null, 'the obsolete descriptor is cleared');
    await page.evaluate(() => openTasks(false));
    await retire({agentId: 'learning', view: 'business', section: 'tasks'}, 'Learning Applier');
    await page.locator('#agpad .ttab[data-v=global_context][aria-selected=true]').waitFor();
    assert.equal(await page.evaluate(() => tPanelOpen()), false);
    assert.equal(await page.evaluate(() => history.state?.agentPane || null), null);
    await page.locator('[data-global-agent=learning]').click();
    await page.locator('#agpad .ttab[data-v=global_context][aria-selected=true]').waitFor();
    // A business link from a pane with no Business view takes the guarded top-level selection of that exact business.
    await page.evaluate(() => agentWorkspaceEscape({business: 'widget'}));
    await page.waitForFunction(() => location.hash === '#widget' && state.biz === 'widget');
    await open('/global/costs');
    await page.locator('#taskspanel .ttab.on[data-v=costs]').waitFor();
    assert.equal(await page.evaluate(() => location.hash), '#/global/agents', 'old Costs links open the shared drawer over Agents');

    // 4. A missing target agent fails clearly and starts nothing.
    profiles = allProfiles.filter(p => !['learning-applier', 'ordubashy'].includes(p.role.id));
    await page.goto(origin + '/#/global/settings');
    await page.evaluate(() => { globalAgentProfiles = []; globalAgentProfilesRequest = undefined; });
    await page.reload();
    await page.waitForTimeout(300);
    calls.length = 0;
    await open('/global/context/people');
    await page.getByRole('alert').filter({hasText: 'not available'}).waitFor();
    assert.equal(await page.locator('.global-agents').count(), 0, 'no other agent is shown');
    assert.equal(await page.locator('.agent-workspace-frame').count(), 0);
    assert.equal(calls.some(c => /\/chat$|terminal|\/start/.test(c.path)), false, 'nothing is started or opened');
    assert.equal(await page.evaluate(() => location.hash), '#/global/context/people');
    // A Global table link needs no agent at all: the drawer opens even when Ordubashy is absent.
    await page.evaluate(() => { tablesOpen(null, 'gt1'); });
    await drawer().locator('#data-detail h1').filter({hasText: 'Global stock'}).waitFor();
    profiles = allProfiles;
    await page.reload();
    await drawer().locator('#data-detail h1').filter({hasText: 'Global stock'}).waitFor();
    await page.evaluate(() => openTasks(false));
    // Learning Applier keeps Context and no pane offers the retired Business management view.
    await page.locator('[data-global-agent=learning]').click();
    await page.locator('#agpad .ttab[data-v=global_context]').waitFor();
    assert.equal(await page.locator('#agpad .ttab[data-v=business]').count(), 0);
    assert.deepEqual(errors, []);
    console.log('Agent pane destinations: menu removal, Context redirect, shared-drawer Tables (scope, record, reload, unsaved guards, unavailable scope, late reports) and Calendar, failure states passed.');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
