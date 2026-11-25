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

function pane() {
  const context = vm.createContext({window: {}, T_VIEW_LABEL: {}, T_VIEW_ICON: {}, setInterval() {}});
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
  // Calendar and Work stay.
  assert.equal(context.agentPaneViewAllowed('ordubashy', 'calendar'), true);
  assert.equal(context.agentPaneViewAllowed('ordubashy', 'work'), true);
});

test('a Journal route is dropped while Testing mode is off, and the help text names the four views', () => {
  assert.match(read('js/global-agents.js'), /request\?\.view === 'journal' && !testingModeEnabled\(\)\) \{ request = null; agentWorkspaceStore\(null\); \}/);
  assert.match(read('js/agent-workspace.js'), /!testingModeEnabled\(\)\) return;\s+await selectGlobal\('journal'\)/);
  assert.match(read('js/settings.js'), /Also shows the Timeline, Journal, Files and Context views in Ordubashy’s pane\./);
});
