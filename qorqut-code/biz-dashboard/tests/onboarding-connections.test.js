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

test('the overview covers the browser and extensions, and both work with either agent', () => {
  const text = step('connect-overview');
  assert.match(text, /built into Qorqbot/);
  assert.match(text, /Works with both Claude Code and Codex/);
  assert.match(text, /<b>Extensions:<\/b>/);
  assert.match(text, /work with both Claude Code and Codex/);
  assert.match(text, /Start with treg/);
  assert.ok(!/Claude connectors|same Claude account|claude\.ai/.test(text));
});

test('the tour names where extensions are, then shows the browser, then ends in Chat with the first message', () => {
  for (const removed of ["{id: 'connectors'", "{id: 'connect-more'"]) assert.ok(!tour.includes(removed), removed);
  assert.match(step('connect-overview'), /Settings under Extensions/);
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

test('extensions are shown open below the browser, and no Claude connector is offered', () => {
  const panel = read('js/connectors.js');
  assert.match(panel, /<section class="cx-extensions" data-cx-extensions>/);
  assert.ok(panel.indexOf('data-cx-browser ') < panel.indexOf('data-cx-extensions>'));
  for (const removed of ['cx-direct', 'claude.ai', 'Claude connectors', 'Gmail', 'Notion', 'Google Drive'])
    assert.ok(!panel.includes(removed), removed);
});

test('Chat tells the owner to start the agent in Terminal and offers no Start button', () => {
  const chat = read('js/global-chat.js');
  for (const removed of ['startButton', 'runStart', 'agentStartPlan', 'GLOBAL_CHAT_STARTS', 'agent_terminal_start'])
    assert.ok(!chat.includes(removed), removed);
  assert.match(chat, /Open the terminal \(>_\) and run/);
  assert.ok(!read('js/agent-start.js').includes('agent_terminal_start'));
});
