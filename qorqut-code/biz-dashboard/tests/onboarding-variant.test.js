'use strict';
// The tour's two variants (terminal, managed): the decision, the derived step lists, resume across variants and the managed
// start step's pure logic. node --test tests/onboarding-variant.test.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const plan = require('../js/onboarding-plan.js');
const runtime = require('../js/agent-runtime.js');

// The source step list, read from onboarding.js the way the tour defines it (agent buttons and targets are never called).
const source = fs.readFileSync(path.join(__dirname, '../js/onboarding.js'), 'utf8');
const listText = source.slice(source.indexOf('  const allSteps = ['), source.indexOf('\n  ];', source.indexOf('  const allSteps = [')) + 5);
const ctx = {CALENDAR_GUIDE_URL: 'x', agentButton: () => null, pane: () => null, sgAttentionNavigate: () => {}, document: {querySelector: () => null}};
vm.createContext(ctx); vm.runInContext(listText.replace('const allSteps', 'this.allSteps'), ctx);
const all = [...ctx.allSteps];
const ids = list => list.map(step => step.id);

const ready = {claude: {installed: true, signed_in: true}, codex: {installed: false, signed_in: false}};
const offline = {state: 'offline', reason: 'no_session'};
const managedClosed = {state: 'offline', execution: 'managed', can_send: true, managed: {open: false}};
const input = (over = {}) => ({replay: false, profile: {runtime: 'claude-code'}, session: offline, clients: ready, ...over});

test('a first walkthrough is managed when Claude Code is ready and he is not running in a terminal', () => {
  assert.equal(plan.chooseVariant(input()), 'managed');
  assert.equal(plan.chooseVariant(input({profile: {}})), 'managed');
  assert.equal(plan.chooseVariant(input({profile: {runtime: ''}})), 'managed');
  assert.equal(plan.chooseVariant(input({profile: {profile: {runtime: 'claude-code'}}})), 'managed');
  assert.equal(plan.chooseVariant(input({session: {state: 'offline'}})), 'managed');
});

test('a first walkthrough stays in the terminal when Claude Code is not ready or his runtime is another', () => {
  for (const claude of [{installed: true, signed_in: false}, {installed: false, signed_in: true}, {}, undefined])
    assert.equal(plan.chooseVariant(input({clients: {claude, codex: {installed: true, signed_in: true}}})), 'terminal');
  assert.equal(plan.chooseVariant(input({profile: {runtime: 'codex'}})), 'terminal');
  assert.equal(plan.chooseVariant(input({profile: {profile: {runtime: 'codex'}}})), 'terminal');
});

test('a first walkthrough stays in the terminal when he is starting or online there', () => {
  assert.equal(plan.chooseVariant(input({session: {state: 'live', can_send: false}})), 'terminal');
  assert.equal(plan.chooseVariant(input({session: {state: 'live', can_send: true}})), 'terminal');
});

test('a session that already says managed gives the managed tour, in a first walkthrough and in a replay', () => {
  for (const replay of [false, true]) {
    assert.equal(plan.chooseVariant(input({replay, session: managedClosed})), 'managed');
    // Whatever the profile and the clients say: the mode he is in decides.
    assert.equal(plan.chooseVariant(input({replay, session: managedClosed, clients: null, profile: {runtime: 'codex'}})), 'managed');
    assert.equal(plan.chooseVariant(input({replay, session: {state: 'live', can_send: true, execution: 'managed'}})), 'managed');
  }
});

test('a replay never switches him: it follows the current mode', () => {
  assert.equal(plan.chooseVariant(input({replay: true})), 'terminal');
  assert.equal(plan.chooseVariant(input({replay: true, session: {execution: 'terminal', state: 'live', can_send: true}})), 'terminal');
});

test('an unreadable session or agent-clients status gives the terminal tour', () => {
  assert.equal(plan.chooseVariant(input({session: null})), 'terminal');
  assert.equal(plan.chooseVariant(input({clients: null})), 'terminal');
  assert.equal(plan.chooseVariant(input({session: null, clients: null})), 'terminal');
  assert.equal(plan.chooseVariant({}), 'terminal');
});

test('the terminal variant is exactly today’s steps in order', () => {
  const list = plan.variantSteps(all, 'terminal');
  assert.deepEqual(ids(list), ['assistant', 'start', 'terminal', 'focus', 'attention', 'calendar', 'journal', 'projects', 'signoffs',
    'settings', 'privacy', 'connect-overview', 'browser-setup', 'chat']);
  assert.ok(list.every(step => !step.variant || step.variant === 'terminal'));
  // Nothing of the managed variant leaks into it: not the replaced fields, not the extra key.
  assert.ok(list.every(step => !('managed' in step)));
  assert.match(list[0].body, /Next, you start him in his own terminal\.$/);
  assert.match(list.at(-1).hint, />_ icon above Chat opens his terminal/);
  assert.match(list.at(-1).offline, /Open his terminal/);
  assert.deepEqual(plan.chapters(list), [{id: 'online', start: 0, end: 2}, {id: 'workspace', start: 3, end: 6},
    {id: 'projects', start: 7, end: 8}, {id: 'yours', start: 9, end: 9}, {id: 'connect', start: 10, end: 13}]);
});

test('the managed variant drops the consent step and replaces the terminal launch', () => {
  const list = plan.variantSteps(all, 'managed');
  assert.deepEqual(ids(list), ['assistant', 'managed-start', 'focus', 'attention', 'calendar', 'journal', 'projects', 'signoffs',
    'settings', 'privacy', 'connect-overview', 'browser-setup', 'chat']);
  assert.deepEqual(plan.chapters(list), [{id: 'online', start: 0, end: 1}, {id: 'workspace', start: 2, end: 5},
    {id: 'projects', start: 6, end: 7}, {id: 'yours', start: 8, end: 8}, {id: 'connect', start: 9, end: 12}]);
  assert.deepEqual(plan.chapterAt(list, 1), {id: 'online', start: 0, end: 1, number: 1, total: 5, step: 2, count: 2});
  assert.equal(plan.nextChapter(list, 1), 2);
  const start = list[1];
  assert.equal(start.title, 'start ordubashy');
  assert.ok(!start.launch && !start.terminal && !start.consent);
  assert.equal(plan.saveId(start), 'terminal');
  assert.doesNotMatch(JSON.stringify(list), /showTerminal/);
});

test('the managed variant words only the steps that sent the owner to a terminal', () => {
  const terminal = plan.variantSteps(all, 'terminal'), managed = plan.variantSteps(all, 'managed');
  const same = managed.filter(step => !['assistant', 'managed-start', 'chat'].includes(step.id));
  for (const step of same) assert.deepEqual(step, terminal.find(other => other.id === step.id), step.id);
  const meet = managed[0];
  assert.match(meet.body, /Qorqbot runs him for you, and you talk to him in Chat\.$/);
  assert.doesNotMatch(meet.body, /terminal/);
  assert.equal(meet.hint, terminal[0].hint);
  const hello = managed.at(-1);
  assert.equal(hello.hint, 'His replies appear here in Chat.');
  assert.equal(hello.body, terminal.at(-1).body);
  for (const text of [hello.hint, hello.switch, hello.blocked]) assert.doesNotMatch(text.replace('Terminal / Managed switch', ''), /terminal|>_/i);
  assert.match(hello.blocked, /Settings/);
  // No managed text in the whole list sends the owner to type claude or codex.
  assert.doesNotMatch(JSON.stringify(managed.map(({target, run, ...step}) => step)), /type <code>|type claude|Open the terminal/i);
});

test('saved progress resumes sensibly in either variant', () => {
  const terminal = plan.variantSteps(all, 'terminal'), managed = plan.variantSteps(all, 'managed');
  const at = (list, step) => ids(list)[plan.resumeIndex(list, {status: 'in_progress', step})];
  // Saved from the managed start step ('terminal') continues at the terminal launch of the terminal tour and at the start of the managed one.
  assert.equal(at(terminal, 'terminal'), 'terminal');
  assert.equal(at(managed, 'terminal'), 'managed-start');
  // Saved at the consent step (terminal tour only): the managed tour continues at its start step, not at the beginning.
  assert.equal(at(terminal, 'start'), 'start');
  assert.equal(at(managed, 'start'), 'managed-start');
  for (const step of ['assistant', 'focus', 'settings', 'chat']) {
    assert.equal(at(terminal, step), step);
    assert.equal(at(managed, step), step);
  }
  // Ids of retired steps still map in the derived lists.
  assert.equal(at(managed, 'activity'), 'calendar');
  assert.equal(at(managed, 'learning'), 'settings');
  assert.equal(at(managed, 'connectors'), 'browser-setup');
  for (const saved of [null, {status: 'completed', step: 'focus'}, {status: 'skipped', step: 'focus'}]) assert.equal(plan.resumeIndex(managed, saved), 0);
});

test('the managed start step reads checking, switching, then ready', () => {
  assert.equal(plan.managedStartPhase(null), 'checking');
  assert.equal(plan.managedStartPhase(offline), 'switching');
  assert.equal(plan.managedStartPhase({state: 'live', can_send: true}), 'switching');
  assert.equal(plan.managedStartPhase(managedClosed), 'ready');
  assert.equal(plan.managedStartPhase({...managedClosed, execution: 'managed', managed: {open: true}}), 'ready');
});

test('a refusal that names a session open in his terminal is recognised, any other is not', () => {
  for (const code of ['session_open', 'terminal_present', 'session_live', 'live_session', 'terminal_running', 'tmux_session'])
    assert.equal(plan.terminalOpenRefusal({code}), true, code);
  assert.equal(plan.terminalOpenRefusal({code: 'invalid', details: {reason: 'session_open'}}), true);
  for (const error of [{code: 'unsupported_role'}, {code: 'waiting_inputs'}, {code: 'network_unavailable'}, new Error('x'), null, undefined])
    assert.equal(plan.terminalOpenRefusal(error), false);
});

test('the hello is sendable whenever a message can open his managed session', () => {
  const r = (session, variantName = 'managed') => plan.helloReadiness(session, variantName, runtime);
  assert.equal(r(managedClosed), 'ready');
  assert.equal(r({state: 'live', can_send: true, execution: 'managed', managed: {open: true}}), 'ready');
  assert.equal(r({state: 'live', can_send: false, execution: 'managed', managed: {open: true}}), 'ready');
  // Closed and not sendable (Claude Code disconnected), or with a binding: it cannot start.
  assert.equal(r({...managedClosed, can_send: false}), 'blocked');
  assert.equal(r({...managedClosed, binding: {binding_id: 'b'}}), 'blocked');
  // A managed tour whose switch never happened, or whose session could not be read.
  assert.equal(r(offline), 'switch');
  assert.equal(r(null), 'switch');
  // The terminal tour is unchanged: online only.
  assert.equal(r({state: 'live', can_send: true}, 'terminal'), 'ready');
  assert.equal(r(offline, 'terminal'), 'offline');
  assert.equal(r(managedClosed, 'terminal'), 'offline');
  assert.equal(r(null, 'terminal'), 'offline');
});
