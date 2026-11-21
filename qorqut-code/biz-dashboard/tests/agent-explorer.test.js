'use strict';
// The Explorer tab: Global, its Projects and their Tasks as one tree, read level by level, with the record reader on the right.
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

test('the Explorer is Ordubashy’s tab, with or without Testing mode, and sits just before Files', () => {
  const context = vm.createContext({window: {}, T_VIEW_LABEL: {}, T_VIEW_ICON: {}, setInterval() {}});
  vm.runInContext(read('js/testing-mode.js'), context);
  vm.runInContext(read('js/agentpane.js'), context);
  assert.ok(context.agentPaneViews('global', 'ordubashy').includes('explorer'));
  context.testingModeSetState(true);
  const views = context.agentPaneViews('global', 'ordubashy');
  assert.equal(views.indexOf('explorer'), views.indexOf('files') - 1);
  for (const role of ['global-worker', 'learning-applier', null]) {
    assert.ok(!context.agentPaneViews('global', role, ['explorer']).includes('explorer'));
    assert.equal(context.agentPaneViewAllowed(role, 'explorer'), false);
  }
  // Calendar and Journal lead it in the rail.
  assert.match(read('js/agentpane.js'), /\['calendar', 'journal'\]\.includes\(view\)\), \.\.\.\(roleId === "ordubashy" \? \["explorer", "files"\] : \[\]\)/);
  const html = read('index.html');
  for (const asset of ['/js/agent-explorer.js', '/css/agent-explorer.css']) assert.ok(html.includes(asset), asset);
  assert.ok(html.indexOf('/js/agent-files.js') < html.indexOf('/js/agent-explorer.js'));
});

test('the Explorer’s reader pairs with the Chat area the way Focus does, and leaving it is guarded', () => {
  const pane = read('js/agentpane.js'), workspace = read('js/agent-workspace.js');
  assert.match(pane, /const agpadPairedView = p => p\?\.view === 'focus' \|\| p\?\.view === 'explorer';/);
  assert.equal((pane.match(/agpadPairedView\((_agpad|p)\) && typeof agentWorkspacePairedRecord === 'function'/g) || []).length, 3);
  assert.match(workspace, /\(p\.view === 'explorer' \? p\.explorerMount : p\.focusMount\)\?\.isConnected === true/);
  assert.match(workspace, /p\?\.view === 'explorer' && p\.explorerCanLeave && !await p\.explorerCanLeave\(\)/);
});

async function fixture(browser, {paired = false} = {}) {
  const page = await browser.newPage({viewport: {width: 1100, height: 700}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setContent('<body style="margin:0"><aside class="tasks agpad" id="agpad" style="display:flex;flex-direction:column;height:700px;width:1100px"><div class="tbody" id="agpad-body" style="flex:1;min-height:0"></div></aside></body>');
  for (const file of ['app.css', 'css/tasks.css', 'css/agent-files.css', 'css/agent-explorer.css', 'css/status-ball.css', 'css/record-workspace.css', 'css/timeline-panel.css'])
    await page.addStyleTag({path: path.join(root, file)});
  await page.evaluate(paired => {
    window.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    window.$ = (selector, host = document) => host.querySelector(selector);
    window.coreData = value => value; window.toast = message => { (window.toasts ||= []).push(message); };
    window.T_VIEW_LABEL = {}; window.T_VIEW_ICON = {tasks: '<svg data-icon="tasks"></svg>', signoffs: '', timeline: '<svg data-icon="timeline"></svg>'};
    window.AICONS = {globe: '<circle cx="12" cy="12" r="10"/>'};
    window.TASK_TYPE_LABELS = {task: 'Task', story: 'Story', customer: 'Customer', email: 'Email'};
    window.taskTypeIcon = type => ['story', 'customer', 'email'].includes(type) ? `<span class="task-type-icon task-type-icon-${type}"></span>` : '';
    window.agentPaneViewAllowed = (role, view) => role === 'ordubashy' && view === 'explorer';
    window.state = {bizs: [{id: 'id-alpha', business_id: 'id-alpha', slug: 'alpha', name: 'Alpha'}, {id: 'id-beta', business_id: 'id-beta', slug: 'beta', name: 'Beta'}]};
    window.conversationMount = host => { host.textContent = 'Thread'; return {expand() {}, collapse() {}, refresh() {}, canLeave: () => true, dispose() { host.replaceChildren(); }}; };
    const task = (id, title, status = 'open', extra = {}) => ({id, title, status, task_type: 'task', children: [], children_has_more: false, ...extra});
    // The synthetic layer: Global holds one Task; Alpha holds a parent with an open and a finished child, a Customer and a finished Task.
    const LAYER = {
      '/root|': [task('g1', 'Global chore')],
      '/biz/alpha|': [task('a1', 'Launch plan', 'working', {children: [{id: 'a2', title: 'Write copy', status: 'open'}, {id: 'a3', title: 'Old draft', status: 'done'}]}),
        task('a4', 'Acme Ltd', 'open', {task_type: 'customer'}), task('a5', 'Shipped thing', 'done'), task('a6', 'Only finished inside', 'open', {children: [{id: 'a7', title: 'Was done', status: 'done'}]})],
      '/biz/alpha|a1': [task('a2', 'Write copy', 'open', {parent_id: 'a1'}), task('a3', 'Old draft', 'done', {parent_id: 'a1'})],
      '/biz/alpha|a6': [task('a7', 'Was done', 'done', {parent_id: 'a6'})],
      '/biz/beta|': [],
    };
    window.calls = [];
    window.post = async (url, body) => {
      calls.push([url, body]);
      if (url.endsWith('/timeline_view')) return {item: body.item, title: 'T', revision: 1, further: {}, state: {goal: {id: 'g', text: 'Goal of ' + body.item.type + ' ' + body.item.id}, decisions: [], facts: [], progress: null, questions: []}};
      const match = /^(\/root|\/biz\/[^/]+)\/operations\/tasks_list$/.exec(url);
      if (!match) throw new Error('unexpected ' + url);
      let rows;
      if (body.search) rows = Object.entries(LAYER).filter(([key]) => key.startsWith(match[1] + '|')).flatMap(([, list]) => list).filter(row => row.title.toLowerCase().includes(body.search.toLowerCase()));
      else rows = LAYER[match[1] + '|' + (body.parent_id || '')] || [];
      if (!body.include_closed) rows = rows.filter(row => !['done', 'cancelled'].includes(row.status));
      return {tasks: rows.map(row => ({...row})), next_cursor: null};
    };
    if (paired) {
      window.agfocusLeftChatReady = () => true;
      window.pairedCalls = [];
      window.agfocusPairedSource = (record, parent) => { pairedCalls.push(record ? record.source : null); return true; };
    }
  }, paired);
  for (const file of ['agent-files', 'agent-explorer', 'timeline-panel', 'record-workspace']) {
    // The Files script registers its tab on load; only its icon helper is used here.
    await page.addScriptTag({path: path.join(root, 'js', file + '.js')});
  }
  await page.evaluate(() => {
    window._agpad = window.p = {workspaceRole: 'ordubashy', el: document.querySelector('#agpad'), view: 'explorer'};
    agentExplorerMount(p);
  });
  await page.locator('[data-row="t::g1"]').waitFor();
  return {page, errors};
}
const rows = page => page.evaluate(() => [...document.querySelectorAll('.agfiles-tree [data-row]')].map(row =>
  `${row.getAttribute('aria-level')}:${row.querySelector('.agfiles-name > span:last-of-type').firstChild.textContent}${row.hasAttribute('aria-expanded') ? (row.getAttribute('aria-expanded') === 'true' ? ' v' : ' >') : ''}${row.classList.contains('agexplorer-done') ? ' done' : ''}`));
const listCalls = page => page.evaluate(() => calls.filter(([url]) => url.endsWith('/tasks_list')).map(([url, body]) => [url, body.parent_id ?? null, body.include_closed, body.search || '']));

test('Global is the root with the Projects, then its own Tasks; a level is read when its row opens', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser);
    assert.deepEqual(await rows(page), ['1:Global v', '2:Alpha >', '2:Beta >', '2:Global chore']);
    assert.deepEqual(await listCalls(page), [['/root/operations/tasks_list', null, false, '']]);
    // The top is the search field and one finished switch: no title, no refresh, no footer.
    assert.equal(await page.locator('.agfiles-head, .agfiles-footer, .agfiles-crumbs, [data-refresh]').count(), 0);
    assert.equal(await page.locator('.agexplorer-top .agfiles-search input').getAttribute('placeholder'), 'Find a project or task');
    await page.click('[data-toggle="p:alpha"]');
    await page.locator('[data-row="t:alpha:a1"]').waitFor();
    // The finished Task is hidden; a parent whose Tasks are all finished has nothing to open.
    assert.deepEqual(await rows(page), ['1:Global v', '2:Alpha v', '3:Launch plan >', '3:Acme Ltd', '3:Only finished inside', '2:Beta >', '2:Global chore']);
    assert.equal(await page.locator('[data-row="t:alpha:a1"] .ntball.ntb-in_progress').count(), 1);
    assert.equal(await page.locator('[data-row="t:alpha:a4"] .task-type-icon-customer').count(), 1);
    await page.click('[data-toggle="t:alpha:a1"]');
    await page.locator('[data-row="t:alpha:a2"]').waitFor();
    assert.deepEqual((await listCalls(page)).slice(1), [['/biz/alpha/operations/tasks_list', null, false, ''], ['/biz/alpha/operations/tasks_list', 'a1', false, '']]);
    assert.deepEqual((await rows(page)).slice(2, 4), ['3:Launch plan v', '4:Write copy']);
    // An empty Project loses its arrow once it is read.
    await page.click('[data-toggle="p:beta"]');
    await page.waitForFunction(() => !document.querySelector('[data-row="p:beta"]').hasAttribute('aria-expanded'));
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('the finished switch reads the open levels again and shows finished rows crossed', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser);
    await page.click('[data-toggle="p:alpha"]');
    await page.locator('[data-row="t:alpha:a1"]').waitFor();
    await page.click('[data-toggle="t:alpha:a1"]');
    await page.locator('[data-row="t:alpha:a2"]').waitFor();
    await page.evaluate(() => { calls.length = 0; });
    await page.click('[data-finished]');
    assert.equal(await page.locator('[data-finished]').getAttribute('aria-pressed'), 'true');
    await page.locator('[data-row="t:alpha:a3"]').waitFor();
    assert.deepEqual(await rows(page), ['1:Global v', '2:Alpha v', '3:Launch plan v', '4:Write copy', '4:Old draft done', '3:Acme Ltd', '3:Shipped thing done', '3:Only finished inside >', '2:Beta >', '2:Global chore']);
    assert.ok((await listCalls(page)).every(call => call[2] === true));
    assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector('[data-row="t:alpha:a5"] .agfiles-name > span:last-of-type')).textDecorationLine), 'line-through');
    await page.click('[data-finished]');
    await page.waitForFunction(() => !document.querySelector('[data-row="t:alpha:a5"]') && document.querySelector('[data-row="t:alpha:a2"]'));
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('search lists matching Projects and Tasks from every scope as flat rows', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser);
    await page.fill('.agexplorer-top input[type=search]', 'a');
    await page.locator('[data-row="t:alpha:a1"]').waitFor();
    const found = await page.evaluate(() => [...document.querySelectorAll('.agfiles-tree [data-row]')].map(row => row.querySelector('.agfiles-name > span:last-of-type').textContent));
    assert.deepEqual(found.sort(), ['AlphaProject', 'Acme LtdAlpha', 'BetaProject', 'Global choreGlobal', 'Launch planAlpha'].sort());
    assert.deepEqual((await listCalls(page)).filter(call => call[3]).map(call => call[0]).sort(),
      ['/biz/alpha/operations/tasks_list', '/biz/beta/operations/tasks_list', '/root/operations/tasks_list']);
    await page.fill('.agexplorer-top input[type=search]', '');
    assert.deepEqual(await rows(page), ['1:Global v', '2:Alpha >', '2:Beta >', '2:Global chore']);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('a row opens the record reader on its timeline; Global shows its own timeline', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser, {paired: true});
    assert.match(await page.locator('.agfiles-detail').textContent(), /Select a project or a task/);
    await page.click('[data-select="p:alpha"]');
    await page.locator('.agfiles-detail .rw-timeline .tlp-block').first().waitFor();
    assert.equal(await page.locator('.agfiles-detail .rw-title').textContent(), 'Alpha');
    assert.match(await page.locator('.agfiles-detail .rw-timeline').textContent(), /Goal of project id-alpha/);
    assert.equal(await page.locator('[data-row="p:alpha"]').getAttribute('aria-selected'), 'true');
    // Beside the Chat area the thread is a toggle on the left, never a tab on the right.
    assert.equal(await page.locator('.agfiles-detail [data-rw-tab="chat"]').count(), 0);
    await page.click('.agfiles-detail .rw-open-thread');
    assert.deepEqual(await page.evaluate(() => pairedCalls.at(-1)), {type: 'project', id: 'id-alpha', business_id: 'id-alpha'});
    await page.click('[data-select="t::g1"]');
    await page.waitForFunction(() => document.querySelector('.agfiles-detail .rw-record:not([hidden]) .rw-title')?.textContent === 'Global chore');
    assert.deepEqual(await page.evaluate(() => calls.filter(([url]) => url.endsWith('/timeline_view')).at(-1)[1].item), {type: 'task', id: 'g1', business_id: null});
    await page.click('[data-select="global"]');
    await page.waitForFunction(() => /Goal of global global/.test(document.querySelector('.agfiles-detail .rw-timeline')?.textContent || ''));
    assert.equal(await page.locator('.agfiles-detail .rw-title').textContent(), 'Global');
    assert.equal(await page.locator('[data-row="global"]').getAttribute('aria-selected'), 'true');
    // Closing the reader clears the selection.
    await page.click('[data-select="p:beta"]');
    await page.locator('.agfiles-detail .rw-close').waitFor();
    await page.click('.agfiles-detail .rw-close');
    await page.waitForFunction(() => /Select a project or a task/.test(document.querySelector('.agfiles-detail').textContent));
    assert.equal(await page.locator('[data-row][aria-selected="true"]').count(), 0);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
