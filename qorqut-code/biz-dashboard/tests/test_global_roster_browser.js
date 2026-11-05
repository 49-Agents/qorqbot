// Isolated browser acceptance for Global → Agents' roster: the supported global agents (any number of workers) plus the owner's
// workspace Terminal, with no business-agent lookup or pins. Real dashboard assets against synthetic APIs.
// No owner Chrome, live services, tmux sessions or agent models.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo = path.resolve(__dirname, '..');
const id = n => '00000000-0000-4000-8000-' + String(n).padStart(12, '0');
const ORDUBASHY = id(1), FOUNDER = id(2), WORKER = id(3), LEARNER = id(4), OLD_GLOBAL = id(5), RETIRED = id(6), WORKER_2 = id(7), INACTIVE_WORKER = id(8), BUSINESS_WORKER = id(9), BUSINESS_DEV = id(11), MARKETING = id(12);
const TERMINAL = '__qorqut_workspace_terminal__', UNAVAILABLE = 'This agent is no longer active. Its history is kept.';
const ecosystem = (allowed_business_ids = []) => ({kind: 'ecosystem', allowed_business_ids, global_capabilities: []});
const global = (agent_id, name, display_name, role, extra = {}) => ({agent_id, name, display_name, role: {id: role}, status: 'active', presentation: {}, scope: ecosystem(), ...extra});
const profiles = [
  global(ORDUBASHY, 'chief', 'Ordubashy', 'ordubashy', {scope: ecosystem(['biz-a'])}),
  // A retired role is never listed even when its profile still says active.
  global(FOUNDER, 'founder', 'Founder', 'founder'),
  global(WORKER, 'growth', 'Growth Worker', 'global-worker', {presentation: {department: 'marketing', icon: 'rocket'}}),
  global(WORKER_2, 'research', 'Research Worker', 'global-worker', {presentation: {icon: 'cat'}}),
  // The retired Marketing role is not a supported global role, so an active profile is never listed or started.
  global(MARKETING, 'marketing', 'Marketing', 'global-marketing'),
  global(LEARNER, 'applier', 'Learning Applier', 'learning-applier'),
  // A retired worker and a business-scoped record claiming the worker role are refused like any other role.
  global(INACTIVE_WORKER, 'gone', 'Retired Worker', 'global-worker', {status: 'retired'}),
  global(BUSINESS_WORKER, 'bizworker', 'Business Worker', 'global-worker', {home_business_id: 'biz-a', scope: {kind: 'business', business_id: 'biz-a'}}),
  // An active global profile of an unsupported role, and a retired archival record, are never listed.
  global(OLD_GLOBAL, 'legacy', 'Legacy Helper', 'legacy-helper'),
  {agent_id: RETIRED, name: 'old', display_name: 'Old Agent', status: 'retired', archival_record: true},
  // A stale business-scoped record claiming a supported role is refused like the host refuses it.
  global(BUSINESS_DEV, 'stale', 'Stale Business Record', 'ordubashy', {home_business_id: 'biz-a', scope: {kind: 'business', business_id: 'biz-a'}}),
];
const businesses = [{slug: 'project-a', name: 'Project', id: 'biz-a', business_id: 'biz-a', agents: ['dev']}];
const requests = [], forbidden = [];
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  requests.push({method: req.method, path: url.pathname});
  const reply = data => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({status: 'ok', data})); };
  const json = (data, status = 200) => { res.statusCode = status; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data)); };
  // The roster never reads business rosters, pins or business terminals.
  if (/^\/api\/biz\/[^/]+\/(agents|terminal)/.test(url.pathname) || url.pathname === '/api/root/agent-pins') {
    forbidden.push(url.pathname); return json({status: 'error', error: {message: 'Not read by the roster'}}, 404);
  }
  if (url.pathname === '/api/biz') return json(businesses);
  if (url.pathname === '/api/root/agents') return reply({agents: profiles});
  if (url.pathname === '/api/me/agents/activity') return reply({agents: [{agent_id: ORDUBASHY, state: 'idle'}], next_cursor: null});
  if (url.pathname.endsWith('/inbox')) return reply({items: [], subscriptions: [], watcher_active: false});
  if (url.pathname.endsWith('/browser-surfaces')) return json({agent_id: url.pathname.split('/').at(-2), configured: true, browsers: [], desktop: null});
  if (url.pathname === '/api/root/operations/workspace_types') return json({status: 'error', error: {message: 'Built-in declaration'}}, 404);
  if (url.pathname === '/') {
    res.setHeader('Content-Type', 'text/html');
    const styles = fs.readFileSync(path.join(repo, 'index.html'), 'utf8').match(/<link rel="stylesheet"[^>]+>/g).join('\n');
    return res.end(`<!doctype html><html><head><meta charset="utf-8">${styles}</head><body class="agent-workspace-app">
      <div id="toasts"></div><button id="wsbtn" hidden></button><div id="host" style="height:100vh;display:flex;min-width:0"></div>
      <script src="/js/core.js"></script><script src="/js/san-juan-time.js"></script><script src="/js/task-state.js"></script><script src="/js/tags.js"></script><script src="/js/work-groups.js"></script><script src="/js/tasks.js"></script><script src="/js/agents.js"></script><script src="/js/global-chat.js"></script>
      <script>
      state.scope='global';state.biz=null;state.globalTab='agents';
      window.storyRelatedChips=()=>{};var SVG_BRAIN="",GLOBAL_ICONS=new Proxy({},{get:()=>""});window.globalIcon=()=>'';window.sgAgentAttentionPaint=()=>{};
      window.terminalMounts=[];window.mountTerminal=(agent,pane,onStat,base)=>{terminalMounts.push({agent,base:base||null});pane.textContent='Terminal '+(base||agent);};
      window.chatMounts=[];window.mountGlobalChat=(pane,agent,opts={})=>{chatMounts.push(agent.agent_id);pane.innerHTML='<div data-global-chat="'+agent.agent_id+'">Chat</div>';return {dispose(){},focus(){}};};
      window.renderRoster=()=>renderGlobalAgents(document.querySelector('#host'));
      </script>
      <script src="/js/safe-links.js"></script><script src="/js/conversation.js"></script><script src="/js/signoffs.js"></script><script src="/js/agentpane.js"></script><script src="/js/global-calendar.js"></script><script src="/js/agent-focus.js"></script><script src="/js/agent-browser.js"></script><script src="/js/agent-workspace.js"></script><script src="/js/agent-fleet.js"></script><script src="/js/global-agents.js"></script><script src="/js/agent-shortcuts.js"></script>
      <script>api('/biz').then(list=>{state.bizs=list;return renderRoster();});</script></body></html>`);
  }
  if (url.pathname.startsWith('/api/')) return json({status: 'error', error: {message: 'Not part of this fixture'}}, 404);
  const file = path.join(repo, url.pathname);
  if (!file.startsWith(repo + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css'); res.end(fs.readFileSync(file));
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1300, height: 900}}), errors = [];
    page.setDefaultTimeout(10000);
    page.on('pageerror', e => { errors.push(e.message); console.error(e.message); });
    await page.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
    const rosterIds = () => page.locator('#global-agent-list [data-global-agent]').evaluateAll(list => list.map(el => el.dataset.globalAgent));
    const selected = () => page.locator('.global-agents [data-global-agent][aria-pressed="true"]').evaluateAll(list => list.map(el => el.dataset.globalAgent));
    const toasts = () => page.locator('#toasts .tx').allInnerTexts();

    await page.goto(origin + '/');
    await page.locator('[data-global-chat]').waitFor();
    assert.deepEqual(await rosterIds(), [ORDUBASHY, LEARNER, WORKER, WORKER_2], 'Ordubashy, Learning Applier, then workers; the retired Marketing profile is absent');
    assert.deepEqual(await page.locator('#global-agent-list [data-global-agent]').evaluateAll(list => list.map(el => el.getAttribute('aria-label'))),
      ['Ordubashy', 'Learning Applier', 'Growth Worker', 'Research Worker']);
    assert.equal(await page.locator('[data-business-agent], [data-pin-agent], input[type=search], #global-biz-toggle, #global-pinned-agent-list').count(), 0, 'no business lookup, search or pins');
    const terminal = page.locator('.global-workspace-terminals .global-workspace-terminal');
    assert.equal(await page.locator('.global-workspace-terminal').count(), 1, 'one Terminal entry');
    assert.equal(await page.getByRole('button', {name: 'History', exact: true}).count(), 0, 'retired-agent History is removed');
    assert.equal(await terminal.isVisible(), true, 'Terminal is always visible');
    assert.equal((await terminal.innerText()).trim(), '', 'Terminal is icon-only');
    assert.equal(await terminal.getAttribute('aria-label'), 'Terminal');
    assert.equal(await page.locator('#global-primary-roster .global-workspace-terminal').count(), 0, 'Terminal is separate from the agents');
    const rosterBox = await page.locator('#global-primary-roster').boundingBox(), terminalBox = await terminal.boundingBox();
    assert(terminalBox.y >= rosterBox.y + rosterBox.height, 'Terminal sits below the agents');
    const navBox = await page.locator('.global-nav-body').boundingBox();
    const terminalGroupBox = await page.locator('.global-workspace-terminals').boundingBox();
    assert(Math.abs(terminalGroupBox.y + terminalGroupBox.height - navBox.y - navBox.height) < 2, 'Terminal stays at the bottom of the navigation');
    assert.deepEqual(await selected(), [ORDUBASHY], 'Ordubashy is the default');
    assert.deepEqual(await page.evaluate(() => chatMounts), [ORDUBASHY]);
    assert.equal(await page.evaluate(() => _agpad.agentId), ORDUBASHY);

    // Selecting by exact UUID: a worker keeps its own pane; Terminal mounts only the owner shell.
    await page.locator(`[data-global-agent="${WORKER}"]`).click();
    await page.waitForFunction(worker => _agpad.agentId === worker, WORKER);
    assert.deepEqual(await selected(), [WORKER]);
    assert.equal(await page.evaluate(() => localStorage.getItem('ag:scope:global')), WORKER);
    await terminal.click();
    assert.deepEqual(await selected(), [TERMINAL]);
    assert.deepEqual(await page.evaluate(() => terminalMounts.at(-1)), {agent: TERMINAL, base: '/root/workspace-terminal'});

    // Every global identity wears the Qorqbot mascot; a worker color is derived from its UUID and a saved catalog icon does not replace it.
    const glyph = agent => page.locator(`#global-agent-list [data-global-agent="${agent}"] svg`).first();
    for (const agent of [WORKER, WORKER_2]) assert.equal(await glyph(agent).evaluate(svg => svg.classList.contains('qorqbot-agent')), true, 'workers wear the mascot');
    assert.match(await glyph(WORKER).locator('g').first().getAttribute('fill'), /^#(448bc6|4b9b75|ba7253|8e70b6|529697)$/, 'a worker takes a palette color, not a department color');
    // The eyes' gaze transform animates, so compare the body and color only.
    const mascotBody = agent => glyph(agent).evaluate(svg => svg.querySelector('g[fill]').outerHTML);
    const mascotBefore = await mascotBody(WORKER_2);
    // Renaming and re-iconing keeps the UUID: same selection, new label and the same mascot.
    await page.locator(`[data-global-agent="${WORKER_2}"]`).click();
    await page.waitForFunction(worker => _agpad.agentId === worker, WORKER_2);
    Object.assign(profiles.find(p => p.agent_id === WORKER_2), {display_name: 'Analyst Worker', presentation: {icon: 'brain'}});
    await page.evaluate(() => { globalAgentProfiles = []; globalAgentProfilesRequest = null; return renderRoster(); });
    await page.waitForFunction(worker => _agpad.agentId === worker, WORKER_2);
    assert.deepEqual(await selected(), [WORKER_2], 'the renamed worker keeps its UUID selection');
    assert.equal(await page.locator(`[data-global-agent="${WORKER_2}"]`).getAttribute('aria-label'), 'Analyst Worker');
    assert.equal(await mascotBody(WORKER_2), mascotBefore, 'the same UUID keeps its mascot and color after the icon metadata changes');
    // A worker opens its own Chat and pane by UUID and is remembered; Chat mounts only for the selected agent.
    await page.locator(`[data-global-agent="${WORKER_2}"]`).click();
    await page.waitForFunction(worker => _agpad.agentId === worker, WORKER_2);
    assert.deepEqual(await selected(), [WORKER_2]);
    assert.equal(await page.evaluate(() => localStorage.getItem('ag:scope:global')), WORKER_2);
    assert.equal(await page.evaluate(() => chatMounts.at(-1)), WORKER_2);
    await page.evaluate(() => renderRoster());
    await page.waitForFunction(worker => _agpad.agentId === worker, WORKER_2);
    assert.deepEqual(await selected(), [WORKER_2], 'the remembered worker is restored');

    // Keyboard cycling covers every agent and the Terminal.
    await page.locator(`[data-global-agent="${WORKER_2}"]`).click();
    await page.locator(`[data-global-agent="${WORKER_2}"]`).focus();
    await page.keyboard.press('Shift+Tab');
    assert.deepEqual(await selected(), [TERMINAL], 'Shift+Tab moves from the last agent to Terminal');
    await page.keyboard.press('Shift+Tab');
    assert.deepEqual(await selected(), [ORDUBASHY], 'and wraps to the first agent');

    // Navigation to a former business agent, a retired record or an unsupported profile opens nothing.
    const mountsBefore = await page.evaluate(() => [chatMounts.length, terminalMounts.length]);
    for (const target of [MARKETING, FOUNDER, BUSINESS_DEV, RETIRED, OLD_GLOBAL, INACTIVE_WORKER, BUSINESS_WORKER]) {
      await assert.rejects(page.evaluate(agent => globalAgentsNavigate(agent, {chat: true}), target), new RegExp(UNAVAILABLE.replace('.', '\\.')));
    }
    assert.deepEqual(await page.evaluate(() => [chatMounts.length, terminalMounts.length]), mountsBefore, 'no chat or shell is mounted');
    assert.deepEqual(await selected(), [ORDUBASHY], 'the current selection is kept');
    assert.equal(await page.locator('[data-global-agent]').count(), 5, 'the unavailable agent is not added to the roster');

    // A saved or linked unavailable agent shows the short message and falls back to Ordubashy.
    for (const [setup, label] of [[() => localStorage.setItem('ag:scope:global', '00000000-0000-4000-8000-000000000011'), 'saved'],
      [() => history.replaceState(null, '', '/?agent=00000000-0000-4000-8000-000000000006'), 'linked']]) {
      await page.evaluate(() => { document.querySelector('#toasts').innerHTML = ''; window.chatMounts = []; });
      await page.evaluate(setup);
      await page.evaluate(() => renderRoster());
      await page.locator('[data-global-chat]').waitFor();
      assert.deepEqual(await toasts(), [UNAVAILABLE], label + ' unavailable agent explains itself');
      assert.deepEqual(await page.evaluate(() => chatMounts), [ORDUBASHY], label + ' unavailable agent falls back to Ordubashy');
      assert.equal(await page.evaluate(() => localStorage.getItem('ag:scope:global')), ORDUBASHY);
    }
    // Older Terminal 2/3 selections still open the one Terminal without a message.
    await page.evaluate(() => { document.querySelector('#toasts').innerHTML = ''; localStorage.setItem('ag:scope:global', '__qorqut_workspace_terminal_2__'); });
    await page.evaluate(() => renderRoster());
    await page.waitForFunction(() => document.querySelector('.global-workspace-terminal')?.getAttribute('aria-pressed') === 'true');
    assert.deepEqual(await toasts(), []);

    assert.deepEqual(forbidden, [], 'no business roster, pin or business terminal reads');
    assert.deepEqual(errors, []);
    console.log('Global roster: Ordubashy, two workers, Learning Applier, standalone Terminal, no lookup or pins, unavailable-agent navigation, keyboard cycling passed.');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
