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
  const saved = read('../app/bazarlyk/onboarding_preferences.py');
  assert.ok(saved.indexOf('"connect-overview"') >= 0 && saved.indexOf('"connect-overview"') < saved.indexOf('"browser-setup"'));
});

test('the overview covers who each path works for and the same-account rule', () => {
  const text = step('connect-overview');
  assert.match(text, /built into Qorqbot/);
  assert.match(text, /both Claude Code and Codex/);
  assert.match(text, /Only agents running in Claude Code/);
  assert.match(text, /same Claude account/);
});

test('Connect is explained before it is pressed', () => {
  const text = step('connectors');
  assert.match(text, /opens your connectors on claude\.ai/);
  assert.match(text, /Google’s own page/);
  assert.match(text, /checks again by itself/);
});

test('Chat tells the owner to start the agent in Terminal and offers no Start button', () => {
  const chat = read('js/global-chat.js');
  for (const removed of ['startButton', 'runStart', 'agentStartPlan', 'GLOBAL_CHAT_STARTS', 'agent_terminal_start'])
    assert.ok(!chat.includes(removed), removed);
  assert.match(chat, /Open the terminal \(>_\) and run/);
  assert.ok(!read('js/agent-start.js').includes('agent_terminal_start'));
});
