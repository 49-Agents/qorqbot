'use strict';
// Testing mode also gates the agent pane's Journal, Files and Context views and the record reader's Timeline tab (the latter is
// checked in agent-work.test.js). A state that is still loading counts as off.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

function pane(extra = {}) {
  const context = vm.createContext({window: {}, T_VIEW_LABEL: {}, T_VIEW_ICON: {}, setInterval() {}, ...extra});
  vm.runInContext(read('js/testing-mode.js'), context);
  vm.runInContext(read('js/agentpane.js'), context);
  return context;
}

test('Journal, Files, Timeline and Context are Ordubashy’s only while Testing mode is on', () => {
  const context = pane();
  const gated = ['journal', 'files', 'timeline', 'context_graph'];
  // Still loading: off.
  for (const view of gated) assert.equal(context.agentPaneViewAllowed('ordubashy', view), false, view);
  context.testingModeSetState(false);
  assert.ok(!context.agentPaneViews('global', 'ordubashy').some(view => gated.includes(view)));
  context.testingModeSetState(true);
  for (const view of gated) assert.equal(context.agentPaneViewAllowed('ordubashy', view), true, view);
  for (const role of ['global-worker', 'learning-applier', null]) assert.equal(context.agentPaneViewAllowed(role, 'journal'), false);
  context.testingModeSetState(false);
  for (const view of gated) assert.equal(context.agentPaneViewAllowed('ordubashy', view), false, view);
  // Work stays; Calendar follows the calendar connection instead (below).
  assert.equal(context.agentPaneViewAllowed('ordubashy', 'work'), true);
});

// Calendar is offered only once a calendar is known to be connected: hidden while the check is loading, failed or says none.
function calendarPane(reply) {
  const reads = [];
  const context = pane({document: {body: null}, console, coreData: value => value, accessLost: () => false,
    agentWorkspaceViews: (scope, role) => scope === 'global' && role === 'ordubashy' ? ['calendar', 'context_graph', 'journal'] : [],
    post: async url => { reads.push(url); return reply(); }});
  vm.runInContext(read('js/global-calendar.js'), context);
  const run = code => vm.runInContext(code, context);
  const shown = [];
  context.agpadSetView = (p, view) => { shown.push(view); p.view = view; p.viewShown = true; };
  run("_agpad = {agentId: 'a', workspaceScope: 'global', workspaceRole: 'ordubashy', workspaceBase: [], view: '', views: agentPaneViews('global', 'ordubashy', [], false, 'a')}");
  return {context, run, reads, shown, views: () => run('_agpad.views')};
}

test('Calendar is hidden until a calendar is connected, shown once one is, and a start-up link waits for the one check', async () => {
  let answer;
  const h = calendarPane(() => new Promise(resolve => { answer = resolve; }));
  assert.equal(h.context.agentPaneViewAllowed('ordubashy', 'calendar'), false, 'unknown: hidden');
  assert.ok(!h.views().includes('calendar'));
  h.run('agpadCalendarWatch()');
  // A Calendar link at start-up waits; the pane shows its default view meanwhile, and the link shares the one check.
  const request = h.run("agpadRequestView(_agpad, 'calendar')");
  assert.deepEqual(h.shown, ['focus']);
  assert.equal(h.reads.length, 1);
  answer({configured: true, calendars: [], errors: []});
  assert.equal(await request, true);
  assert.deepEqual(h.shown, ['focus', 'calendar']);
  assert.ok(h.views().includes('calendar'), 'the pane gains the Calendar once connected');
  assert.equal(h.context.agentPaneViewAllowed('ordubashy', 'calendar'), true);
  // The list later says there is none: the Calendar goes, and a link is refused at once.
  h.run("calendarConnection.learn({configured: false, calendars: [], errors: []})");
  assert.equal(h.context.agentPaneViewAllowed('ordubashy', 'calendar'), false);
  assert.ok(!h.views().includes('calendar'));
  assert.equal(h.run("agpadRequestView(_agpad, 'calendar')"), false);
  assert.equal(h.reads.length, 1, 'no further reads of its own');
});

test('a failed start-up check keeps the Calendar hidden and refuses a waiting link', async () => {
  const h = calendarPane(() => { throw new Error('offline'); });
  h.run('agpadCalendarWatch()');
  assert.equal(await h.run("agpadRequestView(_agpad, 'calendar')"), false);
  assert.equal(h.run('calendarConnection.state'), 'unknown');
  assert.equal(h.context.agentPaneViewAllowed('ordubashy', 'calendar'), false);
  assert.ok(!h.views().includes('calendar'));
  assert.equal(await h.run('calendarConnection.check()'), 'unknown');
  assert.equal(h.reads.length, 1, 'the start-up check is never repeated');
});

test('a Journal route is dropped while Testing mode is off, and the help text names the four views', () => {
  assert.match(read('js/global-agents.js'), /request\?\.view === 'journal' && !testingModeEnabled\(\)\) \{ request = null; agentWorkspaceStore\(null\); \}/);
  assert.match(read('js/agent-workspace.js'), /!testingModeEnabled\(\)\) return;\s+await selectGlobal\('journal'\)/);
  assert.match(read('js/settings.js'), /Also shows the Timeline, Journal, Files and Context views in Ordubashy’s pane\./);
});
