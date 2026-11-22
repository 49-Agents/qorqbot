'use strict';
// The Work tab: active work (Tasks, Stories, Routines) for all projects or one, grouped by status, as a List or a Board.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Browser checks are optional. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

test('Work is Ordubashy’s tab, just before the Explorer, with its assets after the Explorer’s', () => {
  const context = vm.createContext({window: {}, T_VIEW_LABEL: {}, T_VIEW_ICON: {}, setInterval() {}});
  vm.runInContext(read('js/testing-mode.js'), context);
  vm.runInContext(read('js/agentpane.js'), context);
  for (const testing of [false, true]) {
    context.testingModeSetState(testing);
    const views = context.agentPaneViews('global', 'ordubashy');
    assert.ok(views.includes('work'));
    assert.equal(views.indexOf('work'), views.indexOf('explorer') - 1);
  }
  for (const role of ['global-worker', 'learning-applier', null]) {
    assert.ok(!context.agentPaneViews('global', role, ['work']).includes('work'));
    assert.equal(context.agentPaneViewAllowed(role, 'work'), false);
  }
  assert.equal(context.agentPaneViewAllowed('ordubashy', 'work'), true);
  assert.match(read('js/agentpane.js'), /if \(view === "work"\) \{ agentWorkMount\(p\); return; \}/);
  const html = read('index.html');
  for (const asset of ['/js/agent-work.js', '/css/agent-work.css']) assert.ok(html.includes(asset), asset);
  assert.ok(html.indexOf('/js/agent-explorer.js') < html.indexOf('/js/agent-work.js'));
  assert.ok(html.indexOf('/css/agent-explorer.css') < html.indexOf('/css/agent-work.css'));
});

test('the pairing and leave guards cover Work', () => {
  const pane = read('js/agentpane.js'), workspace = read('js/agent-workspace.js'), agents = read('js/global-agents.js');
  assert.match(pane, /const agpadPairedView = p => p\?\.view === 'focus' \|\| p\?\.view === 'explorer' \|\| p\?\.view === 'work';/);
  assert.match(pane, /p\.explorerWorkspace\?\.isOpen\?\.\(\) \|\| p\.workWorkspace\?\.isOpen\?\.\(\)/);
  assert.match(workspace, /\['focus', 'explorer', 'work'\]\.includes\(p\.view\)/);
  assert.match(workspace, /p\.view === 'explorer' \? p\.explorerMount : p\.view === 'work' \? p\.workMount : p\.focusMount/);
  assert.match(workspace, /p\?\.view === 'explorer' \|\| p\?\.view === 'work' \|\| p\?\.workspaceFrame/);
  assert.match(workspace, /p\?\.view === 'work' && p\.workCanLeave && !await p\.workCanLeave\(\)/);
  assert.match(agents, /_agpad\?\.explorerWorkspace\?\.isOpen\?\.\(\) \|\| _agpad\?\.workWorkspace\?\.isOpen\?\.\(\)/);
  assert.match(agents, /\['focus', 'explorer', 'work'\]\.includes\(_agpad\?\.view\)/);
});

// A synthetic layer: Global and two projects. Alpha holds every kind of row; Beta can be made to fail.
async function fixture(browser, {paired = false} = {}) {
  const page = await browser.newPage({viewport: {width: 900, height: 700}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setContent('<body style="margin:0"><aside class="tasks agpad" id="agpad" style="display:flex;flex-direction:column;height:700px;width:900px"><div class="tbody" id="agpad-body" style="flex:1;min-height:0"></div></aside></body>');
  for (const file of ['app.css', 'css/tasks.css', 'css/agent-files.css', 'css/agent-explorer.css', 'css/agent-work.css', 'css/status-ball.css', 'css/record-workspace.css', 'css/agent-timeline.css'])
    await page.addStyleTag({path: path.join(root, file)});
  await page.evaluate(paired => {
    window.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    window.$ = (selector, host = document) => host.querySelector(selector);
    window.coreData = value => value; window.toast = message => { (window.toasts ||= []).push(message); };
    window.T_VIEW_LABEL = {}; window.T_VIEW_ICON = {tasks: '<svg data-icon="tasks"></svg>', signoffs: '', timeline: '<svg data-icon="timeline"></svg>'};
    window.AICONS = {globe: '<circle cx="12" cy="12" r="10"/>'};
    window.T_LABEL = {open: 'Open', working: 'Working', blocked: 'Blocked', needs_input: 'Needs your input', waiting: 'Waiting', done: 'Done', cancelled: 'Cancelled'};
    window.TASK_TYPE_LABELS = {task: 'Task', story: 'Story', customer: 'Customer', email: 'Email'};
    window.taskShowsState = task => !['story', 'customer', 'email'].includes(task?.task_type);
    window.taskTypeIcon = type => `<span class="task-type-icon task-type-icon-${type}"></span>`;
    window.taskSnoozed = task => Boolean(task.snoozed_until && Date.parse(task.snoozed_until) > Date.now());
    window.taskSnoozeLabel = task => 'Snoozed until ' + task.snoozed_until.slice(0, 10);
    window.tDue = task => `<span class="tdl far" title="Due ${task.due}">${task.due}</span>`;
    window.rtScope = scope => `<span class="lnscopeico" data-scope="${scope}"></span>`;
    window.rtNext = value => 'In 3 hours';
    window.identityAvatar = (kind, name) => `<span class="identity-avatar" data-kind="${kind}">${name}</span>`;
    window.globalAgentProfile = () => null;
    window.uiFailure = () => 'The record could not be loaded.'; window.TASK_SPECIAL_TYPES = ['story', 'customer', 'email']; window.accessLost = () => false; window.sourceGone = () => false;
    window.agentPaneViewAllowed = (role, view) => role === 'ordubashy' && view === 'work';
    window.state = {projects: [{id: 'id-alpha', project_id: 'id-alpha', slug: 'alpha', name: 'Alpha'}, {id: 'id-beta', project_id: 'id-beta', slug: 'beta', name: 'Beta'}]};
    window.conversationMount = host => { host.textContent = 'Thread'; return {expand() {}, collapse() {}, refresh() {}, canLeave: () => true, dispose() { host.replaceChildren(); }}; };
    window.menu = null;
    window.dropdown = (anchor, items, onPick, current) => { window.menu = {items, current, pick: onPick}; };
    const task = (id, title, status = 'open', extra = {}) => ({id, title, status, task_type: 'task', due: null, queue_rank: null, worker_ids: [], workers: [], children: [], children_has_more: false, ...extra});
    const agent = {id: 'w1', kind: 'agent', display_name: 'Worker One', name: 'worker-one'};
    window.LAYER = {
      '': [task('g1', 'Global chore'), task('g2', 'Acme Ltd', 'open', {task_type: 'customer'}), task('g3', 'Reply to mail', 'open', {task_type: 'email'})],
      alpha: [
        task('a1', 'Launch plan', 'working', {task_type: 'story', status_line: 'On track', worker_ids: ['w1'], workers: [agent],
          children: [{id: 'a1a', title: 'Write copy', status: 'open', task_type: 'task'}, {id: 'a1b', title: 'Old draft', status: 'done', task_type: 'task'}, {id: 'a1c', title: 'Ship it', status: 'working', task_type: 'task'}]}),
        task('a2', 'Fix billing', 'blocked', {queue_rank: 1}), task('a3', 'Sign the form', 'needs_input'),
        task('a4', 'Wait for vendor', 'waiting', {status_line: 'Vendor replies Monday'}),
        task('a5', 'Later thing', 'open', {snoozed_until: '2099-01-01T00:00:00Z'}),
        task('a6', 'Acme customer', 'open', {task_type: 'customer'}),
        task('a7', 'Due early', 'open', {due: '2026-01-01'}), task('a8', 'Zed', 'open', {queue_rank: 2}),
      ],
      beta: [task('b1', 'Beta task')],
    };
    window.ROUTINES = {
      '/root/routines': [{name: 'Weekly review', scope: 'global', status: 'active', next: '2099-01-01T00:00:00Z'}, {name: 'Not global', scope: 'project', status: 'active'}],
      '/project/alpha/routines': [{name: 'Alpha paused', scope: 'project', status: 'paused'}, {name: 'Needs a key', scope: 'project', status: 'active', scheduling_available: false, scheduling_unavailable_reason: 'Scheduling needs a key'}, {name: 'Global leak', scope: 'global', status: 'active'}],
      '/project/beta/routines': [],
    };
    window.fail = new Set(); window.routineGate = null; window.routineFail = new Set();
    window.calls = [];
    window.api = async url => {
      calls.push([url, null]);
      if (routineFail.has(url)) throw new Error('routines down');
      if (routineGate && url === '/root/routines') await routineGate;
      return {routines: ROUTINES[url] || []};
    };
    window.post = async (url, body) => {
      calls.push([url, body]);
      if (url.endsWith('/timeline_view')) return {item: body.item, title: 'T', revision: 1, further: {}, state: {goal: {id: 'g', text: 'Goal of ' + body.item.id}, decisions: [], facts: [], progress: null, questions: []},
        entries: [{id: 'g', sequence: 1, action: 'add', kind: 'goal', text: 'Goal of ' + body.item.id, created_at: '2026-11-13T10:00:00Z', sources: [], refs: []}], links: {}, closed_questions: [], next_cursor: null};
      if (url.endsWith('/tasks_get')) return {task: {...Object.values(LAYER).flat().find(row => row.id === body.task_id)}};
      const match = /^(\/root|\/project\/([^/]+))\/operations\/tasks_list$/.exec(url);
      if (!match) throw new Error('unexpected ' + url);
      const scope = match[2] || '';
      if (fail.has(scope)) throw new Error('down');
      return {tasks: (LAYER[scope] || []).map(row => ({...row})), next_cursor: null};
    };
    if (paired) {
      window.agfocusLeftChatReady = () => true;
      window.agfocusPairedSource = () => true;
    }
  }, paired);
  for (const file of ['agent-files', 'agent-work', 'timeline-panel', 'record-workspace']) await page.addScriptTag({path: path.join(root, 'js', file + '.js')});
  return {page, errors};
}
const mount = async (page, workState) => {
  await page.evaluate(workState => {
    window._agpad = window.p = {workspaceRole: 'ordubashy', el: document.querySelector('#agpad'), view: 'work', ...(workState ? {workState} : {})};
    agentWorkMount(p);
  }, workState || null);
};
const titles = (page, group) => page.evaluate(group => [...document.querySelectorAll(`[data-group="${group}"] .agwork-row .ttl, [data-group="${group}"] .agwork-card .ttl`)].map(node => node.textContent), group);
const groups = page => page.evaluate(() => [...document.querySelectorAll('.agwork-body > [data-group]')].map(node => `${node.dataset.group}:${node.querySelector('.tsh .c').textContent}`));
const withBrowser = fn => async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try { await fn(browser); } finally { await browser.close(); }
};

test('groups, order, counts, leftovers and the second line', {skip: !available}, withBrowser(async browser => {
  const {page, errors} = await fixture(browser);
  await mount(page);
  await page.locator('[data-group="routines"]').waitFor();
  // Customer and email rows never show; waiting or snoozed rows only sit in the folded group.
  assert.deepEqual(await groups(page), ['needs_input:1', 'working:1', 'blocked:1', 'open:4', 'parked:2', 'routines:3']);
  assert.deepEqual(await titles(page, 'open'), ['Due early', 'Zed', 'Beta task', 'Global chore']);
  assert.equal(await page.locator('[data-group="parked"] .agwork-row').count(), 0);
  assert.equal(await page.locator('text=Acme Ltd').count() + await page.locator('text=Acme customer').count() + await page.locator('text=Reply to mail').count(), 0);
  assert.equal(await page.locator('.agwork-scope-name').textContent(), 'All projects');
  await page.click('[data-fold]');
  assert.deepEqual(await titles(page, 'parked'), ['Later thing', 'Wait for vendor']);
  assert.equal(await page.locator('[data-fold]').textContent(), 'Hide');
  // The second line: project name, snooze label, status line; Global tasks say Global.
  const lines = await page.evaluate(() => Object.fromEntries([...document.querySelectorAll('.agwork-row')].map(row => [row.querySelector('.ttl').textContent, row.querySelector('.agwork-line')?.textContent ?? null])));
  assert.equal(lines['Launch plan'], 'Alpha · On track');
  assert.equal(lines['Later thing'], 'Alpha · Snoozed until 2099-01-01');
  assert.equal(lines['Global chore'], 'Global');
  assert.equal(await page.locator('[data-task="alpha:a1"] .task-workers .identity-avatar').count(), 1);
  assert.equal(await page.locator('[data-task="alpha:a7"] .tdl').count(), 1);
  // A Story shows its type marker; a Task shows the status ball (a span, not a button).
  assert.equal(await page.locator('[data-task="alpha:a1"] .task-type-marker').count(), 1);
  assert.equal(await page.locator('[data-task="alpha:a1"] .ball').count(), 0);
  assert.equal(await page.locator('span.ball.blocked[role=img][data-task], [data-task="alpha:a2"] span.ball.blocked[role=img]').count(), 1);
  await page.click('[data-fold]');
  assert.equal(await page.locator('[data-group="parked"] .agwork-row').count(), 0);
  // Tasks and routines were read once, 4 at a time at most; nothing is read again by layout or search.
  const reads = () => page.evaluate(() => calls.length);
  const before = await reads();
  await page.click('[data-layout="board"]'); await page.click('[data-layout="list"]');
  await page.fill('.agwork input[type=search]', 'zed');
  assert.equal(await reads(), before);
  assert.deepEqual(errors, []);
}));

test('the subtasks chip counts active children and opens them; search filters every group', {skip: !available}, withBrowser(async browser => {
  const {page, errors} = await fixture(browser);
  await mount(page);
  await page.locator('[data-group="working"]').waitFor();
  const chip = page.locator('[data-task="alpha:a1"] [data-children]');
  assert.equal((await chip.textContent()).replace(/\s+/g, ' ').trim(), '2 subtasks');
  assert.equal(await chip.getAttribute('aria-expanded'), 'false');
  assert.equal(await page.locator('.agwork-children').count(), 0);
  await chip.click();
  assert.equal(await page.locator('.agwork-children .record-child-row').count(), 2);
  assert.deepEqual(await page.locator('.agwork-children .record-child-title').allTextContents(), ['Write copy', 'Ship it']);
  assert.equal(await page.locator('.agwork-children [data-task="alpha:a1a"]').count(), 1);
  // Expanded rows are kept across a repaint (layout switch and back).
  await page.click('[data-layout="board"]'); await page.click('[data-layout="list"]');
  assert.equal(await page.locator('.agwork-children .record-child-row').count(), 2);
  await page.click('[data-task="alpha:a1"] [data-children]');
  assert.equal(await page.locator('.agwork-children').count(), 0);
  // Search: titles, status lines and project names; the parked group opens while a search is typed; routine names too.
  await page.fill('.agwork input[type=search]', 'vendor');
  assert.deepEqual(await groups(page), ['parked:1']);
  assert.deepEqual(await titles(page, 'parked'), ['Wait for vendor']);
  await page.fill('.agwork input[type=search]', 'beta');
  assert.deepEqual(await groups(page), ['open:1']);
  await page.fill('.agwork input[type=search]', 'paused');
  assert.deepEqual(await groups(page), ['routines:1']);
  await page.fill('.agwork input[type=search]', 'nothing matches');
  assert.equal((await page.locator('.agwork-body').textContent()).trim(), 'Nothing matches.');
  assert.deepEqual(errors, []);
}));

test('a finished row is left out, an unknown status counts as Open, and one project is not searched by its own name', {skip: !available}, withBrowser(async browser => {
  const {page, errors} = await fixture(browser);
  await page.evaluate(() => {
    LAYER.beta = [{...LAYER.beta[0]}, {...LAYER.beta[0], id: 'b2', title: 'Odd one', status: 'reviewing'}, {...LAYER.beta[0], id: 'b3', title: 'Closed one', status: 'done'}];
  });
  await mount(page);
  await page.locator('[data-group="routines"]').waitFor();
  assert.deepEqual(await titles(page, 'open'), ['Due early', 'Zed', 'Beta task', 'Global chore', 'Odd one']);
  assert.equal(await page.locator('text=Closed one').count(), 0);
  await page.click('[data-layout="board"]');
  assert.equal(await page.locator('.agwork-column[data-group="open"] .c').textContent(), '5');
  await page.click('[data-layout="list"]');
  await page.click('[data-scope]');
  await page.evaluate(() => menu.pick('beta'));
  await page.locator('[data-task="beta:b2"]').waitFor();
  assert.deepEqual(await groups(page), ['open:2']);
  await page.fill('.agwork input[type=search]', 'beta');
  assert.deepEqual(await titles(page, 'open'), ['Beta task']);
  // A project with nothing active says so, once its routines have answered too.
  await page.evaluate(() => { LAYER.beta = []; menu.pick('alpha'); menu.pick('beta'); });
  await page.fill('.agwork input[type=search]', '');
  await page.waitForFunction(() => document.querySelector('.agwork-body').textContent.trim() === 'No active work here.');
  assert.deepEqual(errors, []);
}));

test('the scope menu limits to one project and drops the project name from the second line', {skip: !available}, withBrowser(async browser => {
  const {page, errors} = await fixture(browser);
  await mount(page);
  await page.locator('[data-group="routines"]').waitFor();
  await page.click('[data-scope]');
  const menu = await page.evaluate(() => ({items: menu.items.map(item => item.label), current: menu.current}));
  assert.deepEqual(menu, {items: ['All projects', 'Global', 'Alpha', 'Beta'], current: '*'});
  await page.evaluate(() => { calls.length = 0; menu.pick('alpha'); });
  await page.waitForFunction(() => document.querySelector('.agwork-scope-name').textContent === 'Alpha');
  await page.locator('[data-group="routines"]').waitFor();
  assert.deepEqual(await page.evaluate(() => calls.map(call => call[0]).sort()), ['/project/alpha/operations/tasks_list', '/project/alpha/routines']);
  assert.equal(await page.locator('[data-task="alpha:a1"] .agwork-line').textContent(), 'On track');
  assert.equal(await page.locator('[data-task="beta:b1"]').count(), 0);
  // Routines of one project: its own, never Global's; no project name in their line.
  assert.deepEqual(await page.locator('.rtrow .rttitle').allTextContents(), ['Needs a key', 'Alpha paused']);
  assert.deepEqual(await page.locator('.rtrow .rtmeta').allTextContents(), ['Scheduling needs a key', 'Paused']);
  await page.evaluate(() => { calls.length = 0; menu.pick(''); });
  await page.waitForFunction(() => document.querySelector('.agwork-scope-name').textContent === 'Global');
  await page.locator('[data-group="routines"]').waitFor();
  assert.deepEqual(await page.locator('.rtrow .rttitle').allTextContents(), ['Weekly review']);
  assert.deepEqual(await titles(page, 'open'), ['Global chore']);
  assert.deepEqual(errors, []);
}));

test('All projects routines show the project beside the next run and sort active before paused', {skip: !available}, withBrowser(async browser => {
  const {page, errors} = await fixture(browser);
  await mount(page);
  await page.locator('[data-group="routines"]').waitFor();
  assert.deepEqual(await page.locator('.rtrow .rttitle').allTextContents(), ['Weekly review', 'Needs a key', 'Alpha paused']);
  assert.deepEqual(await page.locator('.rtrow .rtmeta').allTextContents(), ['In 3 hours·Global', 'Scheduling needs a key·Alpha', 'Paused·Alpha']);
  assert.equal(await page.locator('.rtrow button').count(), 0);
  assert.deepEqual(errors, []);
}));

test('the Board shows four columns with the same tasks, then the parked group and the routines', {skip: !available}, withBrowser(async browser => {
  const {page, errors} = await fixture(browser);
  await mount(page);
  await page.locator('[data-group="routines"]').waitFor();
  await page.click('[data-layout="board"]');
  assert.equal(await page.locator('[data-layout="board"]').getAttribute('aria-pressed'), 'true');
  assert.deepEqual(await page.evaluate(() => [...document.querySelectorAll('.agwork-column')].map(node => `${node.dataset.group}:${node.querySelector('.c').textContent}`)), ['needs_input:1', 'working:1', 'blocked:1', 'open:4']);
  assert.deepEqual(await titles(page, 'open'), ['Due early', 'Zed', 'Beta task', 'Global chore']);
  assert.equal(await page.locator('.agwork-card').count(), 7);
  assert.equal(await page.locator('[data-task="alpha:a1"] .agwork-card-foot .task-chip').evaluate(node => node.tagName), 'SPAN');
  assert.equal(await page.locator('[data-group="parked"]').count(), 1);
  assert.equal(await page.locator('[data-group="routines"]').count(), 1);
  // An empty column still shows, with its own line.
  await page.evaluate(() => { LAYER.alpha = LAYER.alpha.filter(row => row.status !== 'blocked'); });
  await page.click('[data-scope]');
  await page.evaluate(() => menu.pick('alpha'));
  await page.waitForFunction(() => document.querySelector('[data-group="blocked"] .c')?.textContent === '0');
  assert.match(await page.locator('.agwork-column[data-group="blocked"]').textContent(), /Nothing here\./);
  assert.deepEqual(errors, []);
}));

test('a row opens the record reader in place of the list; closing it brings the list back and reads again quietly', {skip: !available}, withBrowser(async browser => {
  const {page, errors} = await fixture(browser, {paired: true});
  await mount(page);
  await page.locator('[data-task=":g1"]').waitFor();
  await page.evaluate(() => { calls.length = 0; });
  await page.click('[data-task=":g1"]');
  await page.waitForFunction(() => document.querySelector('.agwork-record:not([hidden]) .rw-title')?.textContent === 'Global chore');
  assert.equal(await page.locator('.agwork-top').isVisible(), false);
  assert.equal(await page.locator('.agwork-body').isVisible(), false);
  assert.equal(await page.evaluate(() => p.workWorkspace.isOpen()), true);
  await page.evaluate(() => { calls.length = 0; window.sawLoading = false; new MutationObserver(() => { if (/Loading…/.test(document.querySelector('.agwork-body').textContent)) window.sawLoading = true; }).observe(document.querySelector('.agwork-body'), {childList: true, subtree: true}); });
  await page.click('.agwork-record .rw-close');
  await page.locator('.agwork-top').waitFor({state: 'visible'});
  assert.equal(await page.locator('.agwork-record').isHidden(), true);
  await page.locator('[data-task=":g1"]').waitFor();
  await page.waitForFunction(() => calls.some(([url]) => url.endsWith('/tasks_list')));
  assert.equal(await page.evaluate(() => window.sawLoading), false);
  assert.equal(await page.evaluate(() => p.workWorkspace), null);
  // A quiet re-read that fails keeps the rows it could not refresh, with no notice.
  await page.evaluate(() => { fail.add('alpha'); routineFail.add('/project/alpha/routines'); });
  await page.click('[data-task=":g1"]');
  await page.waitForFunction(() => document.querySelector('.agwork-record:not([hidden]) .rw-title')?.textContent === 'Global chore');
  await page.evaluate(() => { calls.length = 0; });
  await page.click('.agwork-record .rw-close');
  await page.locator('.agwork-top').waitFor({state: 'visible'});
  await page.waitForFunction(() => calls.filter(([url]) => url.endsWith('/routines')).length === 3 && calls.filter(([url]) => url.endsWith('/tasks_list')).length === 3);
  await page.waitForTimeout(50);
  assert.equal(await page.locator('[data-task="alpha:a1"]').count(), 1);
  assert.equal(await page.locator('[role=alert]').count(), 0);
  assert.deepEqual(await page.locator('.rtrow .rttitle').allTextContents(), ['Weekly review', 'Needs a key', 'Alpha paused']);
  await page.evaluate(() => { fail.clear(); routineFail.clear(); });
  // The state survives leaving the tab.
  await page.click('[data-layout="board"]');
  await page.evaluate(() => p.workspaceDispose());
  assert.deepEqual(await page.evaluate(() => p.workState), {scope: '*', layout: 'board', parkedOpen: false, expanded: []});
  assert.deepEqual(errors, []);
}));

test('a project that fails shows a notice; the rest stays, and Try again reads again', {skip: !available}, withBrowser(async browser => {
  const {page, errors} = await fixture(browser);
  await page.evaluate(() => { fail.add('beta'); });
  await mount(page);
  await page.locator('[role=alert]').waitFor();
  assert.match(await page.locator('[role=alert]').textContent(), /One project could not be loaded\. Try again/);
  assert.equal(await page.locator('[data-fold]').getAttribute('aria-label'), 'Show waiting or snoozed');
  assert.equal(await page.locator('[data-task="alpha:a1"]').count(), 1);
  assert.equal(await page.locator('[data-task="beta:b1"]').count(), 0);
  await page.evaluate(() => { fail.add('alpha'); calls.length = 0; });
  await page.click('[data-retry]');
  await page.waitForFunction(() => /2 projects could not be loaded/.test(document.querySelector('.agwork-body').textContent));
  await page.locator('[data-group="routines"]').waitFor();
  assert.doesNotMatch(await page.locator('.agwork-body').textContent(), /No active work here/);
  await page.evaluate(() => { fail.clear(); calls.length = 0; });
  await page.click('[data-retry]');
  await page.locator('[data-task="beta:b1"]').waitFor();
  assert.equal(await page.locator('[role=alert]').count(), 0);
  assert.equal(await page.evaluate(() => calls.filter(([url]) => url.endsWith('/tasks_list')).length), 3);
  assert.deepEqual(errors, []);
}));

test('routine reads never delay the task list: a late answer still shows up and a failed one changes nothing', {skip: !available}, withBrowser(async browser => {
  const {page, errors} = await fixture(browser);
  await page.evaluate(() => { window.routineFail.add('/project/alpha/routines'); window.routineGate = new Promise(resolve => { window.releaseRoutines = resolve; }); });
  await mount(page);
  await page.locator('[data-task="alpha:a1"]').waitFor();
  // The task groups are painted while Global's routines are still outstanding; there is no routines loading text.
  assert.equal(await page.locator('[data-group="routines"]').count(), 0);
  assert.doesNotMatch(await page.locator('.agwork-body').textContent(), /Loading/);
  const before = await groups(page);
  await page.evaluate(() => releaseRoutines());
  await page.locator('[data-group="routines"]').waitFor();
  // Alpha's read failed silently; Global's late answer is there.
  assert.deepEqual(await page.locator('.rtrow .rttitle').allTextContents(), ['Weekly review']);
  assert.deepEqual((await groups(page)).slice(0, -1), before);
  assert.equal(await page.locator('[role=alert]').count(), 0);
  // Every Task read was sent before the first Routine read.
  const order = await page.evaluate(() => calls.map(([url]) => url.endsWith('/tasks_list') ? 't' : url.endsWith('/routines') ? 'r' : '').join(''));
  assert.match(order, /^t{3}r{3}$/);
  assert.deepEqual(errors, []);
}));
