// The tour explains how agents reach outside tools, and agents are started by the owner in their terminal.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const tour = read('js/onboarding.js');
const step = id => {
  const start = tour.indexOf(`{id: '${id}'`);
  assert.ok(start >= 0, `${id} step missing`);
  return tour.slice(start, tour.indexOf('target:', start));
};

test('the overview comes right before the Settings steps and is a saved step', () => {
  assert.ok(tour.indexOf("{id: 'privacy'") < tour.indexOf("{id: 'connect-overview'"));
  assert.ok(tour.indexOf("{id: 'connect-overview'") < tour.indexOf("{id: 'browser-setup'"));
  const saved = read('../app/qorqut/onboarding_preferences.py');
  assert.ok(saved.indexOf('"connect-overview"') >= 0 && saved.indexOf('"connect-overview"') < saved.indexOf('"browser-setup"'));
});

test('the overview covers who each path works for and the same-account rule', () => {
  const text = step('connect-overview');
  assert.match(text, /built into Qorqbot/);
  assert.match(text, /both Claude Code and Codex/);
  assert.match(text, /Only agents running in Claude Code/);
  assert.match(text, /same Claude account/);
});

test('the tour points to the hidden connectors, then the browser, then ends in Chat with the first message', () => {
  for (const removed of ["{id: 'connectors'", "{id: 'connect-more'"]) assert.ok(!tour.includes(removed), removed);
  assert.match(step('connect-overview'), /Settings under Claude connectors/);
  assert.ok(tour.indexOf("{id: 'browser-setup'") < tour.indexOf("{id: 'chat'"));
  const ids = [...tour.matchAll(/\{id: '([a-z-]+)'/g)].map(match => match[1]);
  assert.equal(ids.at(-1), 'chat');
  assert.match(step('chat'), /hi, help me get set up/);
  assert.match(step('chat'), /offline: 'He isn’t running yet/);
});

test('steps that point into the agent menu hold it open, and no step mentions the retired ? button', () => {
  for (const id of ['assistant', 'settings']) assert.match(step(id), /menu: true/);
  assert.match(tour, /if \(step\.menu\) holdMenu\(true\)/);
  assert.match(read('js/global-agents.js'), /function globalAgentNavHold\(hold\)/);
  for (const file of ['js/onboarding.js', 'js/settings.js', 'js/global-agents.js']) assert.ok(!/\? button|bottom left/.test(read(file)), file);
});

test('the browser step points at the browser card together with its signed-in websites', () => {
  const panel = read('js/connectors.js');
  assert.match(tour, /target: \(\) => document\.querySelector\('\[data-cx-browser-area\]'\)/);
  const area = panel.indexOf('data-cx-browser-area');
  assert.ok(area >= 0 && area < panel.indexOf('data-cx-browser ') && panel.indexOf('data-cx-accounts') < panel.indexOf('</section></div>'));
});

test('Claude connectors are collapsed by default and Connect is explained inside', () => {
  const panel = read('js/connectors.js');
  assert.match(panel, /<details class="cx-direct" data-cx-direct>/);
  assert.match(panel, /Connect opens claude\.ai/);
  assert.ok(panel.indexOf('data-cx-browser ') < panel.indexOf('data-cx-direct>'));
});

test('Chat tells the owner to start the agent in Terminal and offers no Start button', () => {
  const chat = read('js/global-chat.js');
  for (const removed of ['startButton', 'runStart', 'agentStartPlan', 'GLOBAL_CHAT_STARTS', 'agent_terminal_start'])
    assert.ok(!chat.includes(removed), removed);
  assert.match(chat, /Open the terminal \(>_\) and run/);
  assert.ok(!read('js/agent-start.js').includes('agent_terminal_start'));
});
