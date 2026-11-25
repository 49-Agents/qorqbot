'use strict';
// The Todo tab (view attentionBoard) is gone: the owner keeps todos as "-" lines in the Focus notes.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');

function pane() {
  const context = vm.createContext({window: {}, T_VIEW_LABEL: {}, T_VIEW_ICON: {}, setInterval() {}, document: {body: {classList: {contains: () => false}}}});
  vm.runInContext(read('js/agentpane.js'), context);
  return context;
}

test('neither Ordubashy nor a permanent worker is offered the Todo tab', () => {
  const context = pane();
  for (const [role, browser] of [['ordubashy', false], ['global-worker', false], ['global-worker', true]]) {
    for (const agentId of [null, 'agent-1']) {
      const views = [...context.agentPaneViews('global', role, [], browser, agentId)];
      assert.ok(views.includes('focus'), `${role} keeps Focus`);
      assert.ok(!views.includes('attentionBoard'), `${role} has no Todo tab`);
    }
  }
  assert.equal(context.agentPaneViewAllowed('ordubashy', 'attentionBoard'), false);
  assert.equal(context.agentPaneViewAllowed('global-worker', 'attentionBoard'), false);
});

test('a remembered or linked Todo view falls back to Focus', () => {
  const context = pane();
  for (const role of ['ordubashy', 'global-worker']) {
    const views = [...context.agentPaneViews('global', role, [], false, 'agent-1')];
    // The pane's first view, where a refused initial view lands, is Focus.
    assert.equal(views[0], 'focus');
    const p = {workspaceRole: role, views, view: 'focus'};
    // Refused outright: no workspace-type wait, no pending view and no tab added.
    assert.equal(context.agpadRequestView(p, 'attentionBoard'), false);
    assert.equal(p.pendingView, undefined);
    assert.ok(!p.views.includes('attentionBoard'));
  }
  // Mounting the retired key directly does nothing rather than drawing a board.
  assert.equal(context.agpadWorkspaceMount({workspaceRole: 'ordubashy'}, 'attentionBoard'), undefined);
});

test('no dashboard code still names the board', () => {
  assert.ok(!fs.existsSync(path.join(__dirname, '../js/attention-board.js')));
  assert.ok(!fs.existsSync(path.join(__dirname, '../js/attention-agents.js')));
  assert.doesNotMatch(read('index.html'), /attention-(board|agents)\.js/);
  for (const file of ['js/agentpane.js', 'js/agent-workspace.js', 'js/tasks.js', 'js/onboarding.js'])
    // Only the retired-view list (and comments) may still name the key.
    assert.doesNotMatch(read(file).replace(/AGPAD_RETIRED_PANE_VIEWS = new Set\([^)]*\)/, '').replace(/^\s*\/\/.*$/gm, ''), /attentionBoard/, file);
});
