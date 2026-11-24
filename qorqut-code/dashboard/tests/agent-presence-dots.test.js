'use strict';
// The navigation's working dots ask for each agent's state alone: one small request per refresh, no paging.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function load(reply) {
  const dot = id => ({dataset: {agentPresence: id}, hidden: true, setAttribute() {}});
  const dots = [dot('a1'), dot('a2')];
  const requests = [];
  const context = vm.createContext({
    document: {hidden: false, querySelectorAll: () => dots, addEventListener() {}},
    setInterval() {}, onChange() {},
    api: async url => { requests.push(url); return reply(); },
    coreData: value => value.data,
    accessLost: error => error.httpStatus === 403,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', 'agent-fleet.js'), 'utf8'), context);
  return {refresh: () => context.agentNavRefresh(true), dots, requests};
}
const states = {data: {agents: [{agent_id: 'a1', state: 'running'}, {agent_id: 'a2', state: 'idle'}]}};

test('one refresh is one request for the states, and only a running agent shows its dot', async () => {
  const page = load(() => states);
  await page.refresh();
  assert.deepEqual(page.requests, ['/me/agents/presence']);
  assert.deepEqual(page.dots.map(dot => [dot.dataset.state, dot.hidden]), [['running', false], ['idle', true]]);
});

test('a passing failure keeps the last states and lost access clears them', async () => {
  let failure = null;
  const page = load(() => { if (failure) throw failure; return states; });
  await page.refresh();
  failure = Object.assign(new Error('Unavailable'), {httpStatus: 503});
  await page.refresh();
  assert.equal(page.dots[0].hidden, false);
  failure = Object.assign(new Error('Forbidden'), {httpStatus: 403});
  await page.refresh();
  assert.deepEqual(page.dots.map(dot => [dot.dataset.state, dot.hidden]), [['unknown', true], ['unknown', true]]);
});
