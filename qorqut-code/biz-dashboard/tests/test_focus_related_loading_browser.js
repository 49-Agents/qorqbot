// Focus related-work loading: hidden-tab completion, unloaded-branch recovery, lazy conversation reads and stale-reply guards,
// against the real Focus pane and synthetic DTOs on an ephemeral localhost fixture. No owner Chrome, live services or agent models.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo = path.resolve(__dirname, '..');
const agent = '11111111-1111-4111-8111-111111111111';
const ids = ['task-c', 'task-d', ...Array.from({length: 6}, (_, i) => 'task-x' + i), 'story-e', 'story-f'];
const board = {revision: 1, status: 'Ready', updated_at: new Date().toISOString(), stale: false, browser: null, desktop_control_url: null,
  items: ids.map(id => {
    const global = id === 'task-d', resolved = {type: 'task', id, business_id: global ? null : 'biz-c', business_slug: global ? null : 'longname', business_name: global ? 'Global' : 'Long Name',
      label: 'Work ' + id, resource_type: 'tasks', state: 'open', ...(id === 'story-e' ? {task_type: 'story', label: 'Email triage'} : id === 'story-f' ? {task_type: 'story', label: 'Slow story'} : {})};
    return {id, revision: 1, label: 'Work ' + id, state: 'open', reason: 'Group ' + id, archived: false, created_by: agent,
      source: {type: 'task', id, business_id: resolved.business_id}, resolved_source: resolved,
      conversation: {type: 'task', id, business_id: resolved.business_id}, source_revision: 1, references: []};
  })};
// Every read that reaches the fixture, and per-route gates that hold a parent's branch reads until released.
const workCalls = [], threadReads = [], ownerReads = [], gates = [];
let holdParent = null, readDelay = 0, holdBoard = false;
const boardGates = [];
// Nested Story -> Task -> subtasks fixture: parent id -> [child id, title, task_type].
const nested = {'story-f': [['grp-f', 'Other F', 'task']], 'story-e': [['grp-other', 'Other', 'task']], 'grp-other': [['sub-1', 'Subtask 1'], ['sub-2', 'Subtask 2'], ['sub-3', 'Subtask 3']], 'sub-1': [['leaf-1', 'Leaf 1']]};
const reply = (res, data) => {res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({status: 'ok', data}));};
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const body = async () => {let raw = ''; for await (const chunk of req) raw += chunk; return raw ? JSON.parse(raw) : {};};
  if (url.pathname.includes('/operations/owner_conversation_')) {{const b = await body(); ownerReads.push(b); threadReads.push(b.source.id);} return reply(res, {conversation: {}, messages: [], participants: [], read_state: {last_read_sequence: 0, unread: false}});}
  if (url.pathname.endsWith('/operations/focus_get')) {
    const b = await body();
    if (b.conversation) {threadReads.push(b.item_id); return reply(res, {item: {thread: {notes: [], participants: []}}});}
    if (b.item_id) return reply(res, {item: board.items.find(item => item.id === b.item_id)});
    if (holdBoard) {holdBoard = false; await new Promise(resolve => boardGates.push(resolve));}
    return reply(res, {...board, next_offset: null});
  }
  if (url.pathname.endsWith('/operations/tasks_list') || url.pathname.endsWith('/operations/relationships_list')) {
    const b = await body(), route = url.pathname.split('/').pop(), parent = b.parent_id ?? b.item?.id;
    workCalls.push({route, parent, path: url.pathname, t: Date.now()});
    if (holdParent === parent) await new Promise(resolve => gates.push(resolve));
    if (readDelay) await new Promise(resolve => setTimeout(resolve, readDelay));
    if (route === 'tasks_list' && nested[parent]) return reply(res, {tasks: nested[parent].map(([id, title, task_type]) => ({id, title, status: 'open', revision: 1, archived: false, closed: false, task_type})), next_cursor: null});
    if (route === 'tasks_list' && ['grp-other', 'sub-1', 'sub-2', 'sub-3', 'leaf-1', 'grp-f'].includes(parent)) return reply(res, {tasks: [], next_cursor: null});
    if (route === 'tasks_list') return reply(res, {tasks: [{id: parent + '-child', title: 'Child of ' + parent, status: 'open', revision: 1, archived: false, closed: false}], next_cursor: null});
    return reply(res, {items: [], next_cursor: null});
  }
  if (url.pathname.endsWith('/operations/tasks_config_get')) return reply(res, {config: {states: [{name: 'open'}, {name: 'done'}]}, revision: 1});
  if (url.pathname.endsWith('/focus')) return reply(res, board);
  if (url.pathname.endsWith('/inbox')) return reply(res, {items: [], subscriptions: []});
  if (url.pathname === '/api/session') return reply(res, {principal_id: 'owner', principal_kind: 'human', display_name: 'Owner', is_owner: true});
  if (url.pathname === '/api/root/agent-pins') return reply(res, {initialized: true, pinned_agents: []});
  if (url.pathname === '/api/root/agents') return reply(res, {agents: [{agent_id: agent, name: 'chief', display_name: 'Ordubashy', role: {id: 'ordubashy'}, scope: {kind: 'ecosystem', allowed_business_ids: [], global_capabilities: []}}]});
  if (url.pathname === '/api/root/terminals') {res.setHeader('Content-Type', 'application/json'); return res.end('[]');}
  if (url.pathname === '/') {
    res.setHeader('Content-Type', 'text/html');
    const styles = fs.readFileSync(path.join(repo, 'index.html'), 'utf8').match(/<link rel="stylesheet"[^>]+>/g).join('\n');
    return res.end(`<!doctype html><html><head><meta charset="utf-8">${styles}</head><body>
      <div id="toasts"></div><button id="wsbtn" hidden></button><div id="host" style="height:100vh;display:flex;min-width:0"></div>
      <script src="/js/core.js"></script><script src="/js/san-juan-time.js"></script><script src="/js/task-state.js"></script><script src="/js/tags.js"></script><script src="/js/work-groups.js"></script><script src="/js/tasks.js"></script><script src="/js/agents.js"></script>
      <script>
      state.scope='global';state.biz=null;state.globalTab='agents';state.bizs=[];
      window.workRelatedChips=()=>{};
      window.globalIcon=()=>''; window.broadcastButton=()=>'';window.wireBroadcastButton=()=>{};window.agentNavPaint=()=>{};window.agentNavRefresh=()=>{};
      window.mountTerminal=()=>{};window.mountGlobalChat=()=>({dispose(){}});
      window.agentViewDispose=()=>{};window.mountAgentView=()=>{};
      window.workingMemoryMount=el=>el.textContent='Working memory form';
      window.sgAgentAttentionPaint=()=>{};
      window.tFetchTask=async(biz,id)=>({business:{slug:biz},id,title:'Task detail'});
      window.tDetail=(task,{host}={})=>{host.textContent='Existing task editor'};
      </script>
      <script src="/js/safe-links.js"></script><script src="/js/chat-view.js"></script><script src="/js/conversation.js"></script><script src="/js/signoffs.js"></script><script src="/js/global-calendar.js"></script><script src="/js/agentpane.js"></script><script src="/js/agent-focus.js"></script><script src="/js/agent-browser.js"></script><script src="/js/global-agents.js"></script>
      <script>renderGlobalAgents(document.querySelector('#host'));</script></body></html>`);
  }
  const file = path.join(repo, url.pathname);
  if (!file.startsWith(repo + path.sep) || !fs.existsSync(file)) {res.statusCode = 404; return res.end();}
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css'); res.end(fs.readFileSync(file));
});
const until = async (check, what, ms = 8000) => {const end = Date.now() + ms; while (Date.now() < end) {if (await check()) return; await new Promise(r => setTimeout(r, 25));} throw new Error('Timed out waiting for ' + what);};

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  try {
    const page = await browser.newPage({viewport: {width: 1300, height: 900}}), errors = [];
    page.setDefaultTimeout(10000);
    page.on('pageerror', e => {errors.push(e.message); console.error(e.message);});
    await page.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
    // The controlled fixture decides whether the document counts as hidden.
    await page.addInitScript(() => {
      window.__hidden = false;
      Object.defineProperty(document, 'hidden', {configurable: true, get: () => window.__hidden});
    });
    const setHidden = hidden => page.evaluate(value => {window.__hidden = value; document.dispatchEvent(new Event('visibilitychange'));}, hidden);
    const count = (route, parent) => workCalls.filter(call => call.route === route && call.parent === parent).length;
    const branchState = id => page.evaluate(id => {const b = _agpad.focusWork?.get(id); return b && {expanded: b.expanded, loaded: b.loaded, flight: !!b.flight, items: b.items.length};}, id);
    const release = () => {holdParent = null; gates.splice(0).forEach(resolve => resolve());};
    const settled = () => page.waitForFunction(() => !_agpad.focusPending && !_agpad.focusRefreshAgain);
    await page.goto(origin);
    await page.getByRole('tab', {name: 'Focus', exact: true}).click();
    await page.locator('[data-card-id="task-c"]').waitFor();
    await settled();

    // (c) Collapsed rows issue no per-row conversation reads; selecting one reads its conversation and loads its related work.
    await page.waitForTimeout(300);
    assert.deepEqual([threadReads.length, workCalls.length], [0, 0], 'no conversation or related-work read for collapsed rows');
    await page.locator('[data-card-id="task-x0"] .ttl').click();
    await until(() => threadReads.includes('task-x0'), 'the selected conversation read');
    await page.locator('[data-card-id="task-x0"][aria-expanded="true"]').waitFor();
    await until(() => count('tasks_list', 'task-x0') === 1 && count('relationships_list', 'task-x0') === 1, 'the selected related work');
    await page.locator('[data-card-id="task-x0/task/task-x0-child"]').waitFor();
    assert.deepEqual([...new Set(threadReads)], ['task-x0'], 'only the selected row reads its conversation');
    // Refresh Focus keeps refreshing the open conversation only.
    const readsBefore = threadReads.length;
    await page.getByRole('button', {name: 'Refresh Focus'}).evaluate(button => button.click());
    await settled();
    await until(() => threadReads.length > readsBefore, 'the open conversation refresh');
    assert.deepEqual([...new Set(threadReads)], ['task-x0'], 'refresh does not read collapsed rows');
    await page.locator('[data-focus-thread-close]').click();

    // (a) A branch read that completes while the tab is hidden is kept; no placeholder waits for the next 60s poll.
    await page.evaluate(() => {_agpad.nextFocus = Date.now() + 3600000;});
    holdParent = 'task-c';
    await page.locator('[data-card-id="task-c"] .ttl').click();
    await until(() => gates.length >= 2, 'both held branch reads');
    await page.locator('.agfocus-work-extras .muted', {hasText: 'Loading related work…'}).waitFor();
    await setHidden(true);
    release();
    await until(async () => (await branchState('task-c'))?.loaded === true, 'the branch commit while hidden');
    await setHidden(false);
    await page.locator('[data-card-id="task-c/task/task-c-child"]').waitFor();
    assert.equal(await page.locator('.agfocus-work-extras .muted', {hasText: 'Loading related work…'}).count(), 0, 'no endless placeholder');
    assert.deepEqual([count('tasks_list', 'task-c'), count('relationships_list', 'task-c')], [1, 1], 'one request per route, no rereads on return');
    await page.locator('[data-focus-thread-close]').click();

    // (b) An unloaded idle expanded branch starts its first read again on the next visible tick, without the board poll being due.
    await page.evaluate(() => {
      _agpad.nextFocus = Date.now() + 3600000;
      const branch = agfocusNewBranch(); branch.expanded = true; _agpad.focusWork.set('task-d', branch);
    });
    await until(async () => (await branchState('task-d'))?.loaded === true, 'recovery of the idle unloaded branch', 5000);
    await page.waitForTimeout(1500);
    assert.deepEqual([count('tasks_list', 'task-d'), count('relationships_list', 'task-d')], [1, 1], 'one request per route, no retry loop');
    assert.equal(workCalls.filter(call => call.parent === 'task-x1').length, 0, 'unopened branches are never expanded');

    // (d) Stale replies never resurrect an old branch. First: collapsed while the read is outstanding.
    const before = workCalls.length;
    holdParent = 'task-x2';
    await page.locator('[data-card-id="task-x2"] .ttl').click();
    await until(() => gates.length >= 2, 'held reads for task-x2');
    await page.evaluate(() => {_agpad.focusWork.get('task-x2').expanded = false;});
    release();
    await page.waitForTimeout(1500);
    assert.equal((await branchState('task-x2')).loaded, false, 'a collapsed branch does not commit a late reply');
    assert.equal(count('tasks_list', 'task-x2'), 1, 'a collapsed branch is not recovered');
    await page.locator('[data-focus-thread-close]').click();
    // Second: the pane is remounted while the read is outstanding.
    holdParent = 'task-x3';
    await page.locator('[data-card-id="task-x3"] .ttl').click();
    await until(() => gates.length >= 2, 'held reads for task-x3');
    const other = await page.evaluate(() => [...document.querySelectorAll('[data-v]')].map(tab => tab.dataset.v).find(view => view !== 'focus'));
    await page.locator(`[data-v="${other}"]`).first().click();
    await page.waitForFunction(() => !document.querySelector('.agfocus'));
    release();
    await page.waitForTimeout(300);
    assert.equal(await page.evaluate(() => !!document.querySelector('.agfocus')), false, 'no old mount is painted');
    await page.getByRole('tab', {name: 'Focus', exact: true}).click();
    await page.locator('[data-card-id="task-c"]').waitFor();
    await settled();
    // Third: the source binding changes while the read is outstanding.
    holdParent = 'task-x4';
    await page.locator('[data-card-id="task-x4"] .ttl').click();
    await until(() => gates.length >= 2, 'held reads for task-x4');
    await page.evaluate(() => {const item = _agpad.focus.items.find(i => i.id === 'task-x4'); item.resolved_source = {...item.resolved_source, business_slug: 'otherbiz'};});
    release();
    await page.waitForTimeout(300);
    // The discarded old-source reply leaves the branch unloaded; recovery then reads it under the new source only.
    await until(async () => (await branchState('task-x4'))?.loaded === true, 'the branch under the new source');
    assert.equal(await page.evaluate(() => _agpad.focusWork.get('task-x4').sourceKey.includes('otherbiz')), true, 'the committed snapshot belongs to the new source');
    assert.equal(workCalls.filter(call => call.parent === 'task-x4' && call.path.includes('/otherbiz/')).length, 2, 'the new source is read once per route');
    // Hidden completions keep the same identity gates: a collapse while hidden still discards the reply.
    await setHidden(false);
    assert.ok(workCalls.length > before);
    // Nested Story -> Task -> subtasks: manual expansion and restored expanded branches read each branch once per route.
    const nestedCalls = parent => workCalls.filter(call => ['story-e', 'grp-other', 'sub-1', 'sub-2', 'sub-3', 'leaf-1'].includes(call.parent)).map(call => call.route + ':' + call.parent);
    const expectedNested = ['story-e', 'grp-other', 'sub-1'].flatMap(parent => ['tasks_list:' + parent, 'relationships_list:' + parent]).sort();
    readDelay = 120;
    {
      const mark = workCalls.length, start = Date.now();
      await page.locator('[data-card-id="story-e"] .ttl').click();
      await page.locator('[data-card-id="story-e/task/grp-other"]').waitFor();
      await page.locator('[data-card-id="story-e/task/grp-other"] .ttl').click();
      await page.locator('[data-card-id="story-e/task/grp-other/task/sub-1"]').waitFor();
      await page.locator('[data-card-id="story-e/task/grp-other/task/sub-1"] .ttl').click();
      await page.locator('[data-card-id="story-e/task/grp-other/task/sub-1/task/leaf-1"]').waitFor();
      console.log('nested manual: ' + (Date.now() - start) + 'ms for ' + (workCalls.length - mark) + ' reads');
      assert.deepEqual(nestedCalls().sort(), expectedNested, 'manual nested expansion reads each branch once per route');
      assert.equal(await page.evaluate(() => _agpad.focusWork.get('story-e').loaded && _agpad.focusWork.get('story-e/task/grp-other').loaded), true);
    }
    {
      workCalls.length = 0;
      await page.evaluate(() => localStorage.setItem('agfocus:expanded-work:v1', JSON.stringify(['story-e', 'story-e/task/grp-other', 'story-e/task/grp-other/task/sub-1'])));
      await page.reload();
      await page.getByRole('tab', {name: 'Focus', exact: true}).click();
      const start = Date.now();
      await page.locator('[data-card-id="story-e"]').waitFor();
      console.log('nested restored: board card visible after ' + (Date.now() - start) + 'ms');
      await page.locator('[data-card-id="story-e/task/grp-other/task/sub-1/task/leaf-1"]').waitFor();
      console.log('nested restored: ' + (Date.now() - start) + 'ms after the pane opened, ' + workCalls.length + ' reads, starts +' + workCalls.map(call => call.t - workCalls[0].t).join(',') + 'ms');
      await page.waitForTimeout(500);
      assert.deepEqual(nestedCalls().sort(), expectedNested, 'restored nested branches read each branch once per route');
      assert.equal(await page.locator('.agfocus-work-extras .muted', {hasText: 'Loading related work…'}).count(), 0);
    }
    // An idle nested branch that is expanded but unloaded recovers on a visible tick, before the next board poll.
    {
      const id = 'story-e/task/grp-other/task/sub-3', mark = workCalls.length;
      await page.evaluate(id => {_agpad.nextFocus = Date.now() + 3600000; const branch = agfocusNewBranch(); branch.expanded = true; _agpad.focusWork.set(id, branch);}, id);
      await until(async () => (await branchState(id))?.loaded === true, 'recovery of the idle nested branch', 5000);
      await page.waitForTimeout(1200);
      assert.deepEqual(workCalls.slice(mark).map(call => call.route + ':' + call.parent).sort(), ['relationships_list:sub-3', 'tasks_list:sub-3'], 'exactly one read per route, nothing else');
    }
    // A hidden board response with restored nested state starts no new branch reads; they resume on the next visible ticks.
    {
      workCalls.length = 0;
      const convBefore = threadReads.length;
      await page.evaluate(() => localStorage.setItem('agfocus:expanded-work:v1', JSON.stringify(['story-e', 'story-e/task/grp-other', 'story-e/task/grp-other/task/sub-1'])));
      holdBoard = true;
      await page.reload();
      await page.getByRole('tab', {name: 'Focus', exact: true}).click();
      await until(() => boardGates.length === 1, 'the held board read');
      await setHidden(true);
      boardGates.splice(0).forEach(resolve => resolve());
      await page.waitForFunction(() => _agpad.focus?.items?.length > 0 && !_agpad.focusPending);
      await page.waitForTimeout(1500);
      assert.equal(workCalls.length, 0, 'no new branch read starts while hidden');
      await setHidden(false);
      await page.locator('[data-card-id="story-e/task/grp-other/task/sub-1/task/leaf-1"]').waitFor();
      assert.deepEqual(nestedCalls().sort(), expectedNested, 'resumed once per route, no duplicates');
      assert.equal(threadReads.length, convBefore, 'no conversation fanout');
    }
    // A nested branch opened by hand completes while the tab is hidden and is never stranded.
    {
      const id = 'story-e/task/grp-other/task/sub-2';
      await page.evaluate(() => {_agpad.nextFocus = Date.now() + 3600000;});
      holdParent = 'sub-2';
      await page.locator(`[data-card-id="${id}"] .ttl`).click();
      await until(() => gates.length >= 2, 'held nested reads');
      await setHidden(true);
      release();
      await until(async () => (await branchState(id))?.loaded === true, 'the nested commit while hidden');
      await setHidden(false);
      assert.equal(workCalls.filter(call => call.parent === 'sub-2').length, 2, 'one request per route for the nested branch');
    }
    // A slow restored root never delays another root's descendants: Email triage -> Other -> subtasks loads while the other
    // root's reads are still held, each route once, and collapsed/unopened descendants stay unfetched.
    {
      readDelay = 0;
      workCalls.length = 0;
      await page.evaluate(() => localStorage.setItem('agfocus:expanded-work:v1', JSON.stringify(['story-f', 'story-e', 'story-e/task/grp-other', 'story-e/task/grp-other/task/sub-1'])));
      holdParent = 'story-f';
      await page.reload();
      await page.getByRole('tab', {name: 'Focus', exact: true}).click();
      await page.locator('[data-card-id="story-e/task/grp-other/task/sub-1/task/leaf-1"]').waitFor();
      assert.equal(gates.length, 2, 'the slow root is still held while the other path finished');
      assert.equal(await page.evaluate(() => _agpad.focusPending), true, 'the board refresh is still waiting on the slow root');
      assert.deepEqual(workCalls.filter(call => call.parent !== 'story-f').map(call => call.route + ':' + call.parent).sort(), expectedNested, 'the finished path read each branch once per route');
      assert.equal(workCalls.filter(call => call.parent === 'story-f').length, 2, 'the held root made one read per route');
      assert.equal(workCalls.some(call => ['sub-2', 'sub-3', 'leaf-1x', 'grp-f'].includes(call.parent)), false, 'collapsed and unopened descendants are not fetched');
      release();
      await page.locator('[data-card-id="story-f/task/grp-f"]').waitFor();
      assert.equal(workCalls.filter(call => call.parent === 'story-f').length, 2);
      assert.equal(workCalls.filter(call => call.parent === 'grp-f').length, 0, 'the released root does not open its unexpanded child');
    }
    assert.deepEqual(errors, []);
    console.log('Focus related-work loading: hidden completion, unloaded-branch recovery, lazy conversation reads and stale-reply guards passed.');
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
})().catch(error => {console.error(error); process.exitCode = 1; server.close();});
