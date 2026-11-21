'use strict';
// node --test dashboard/tests/agent-mode.test.js
// How agents run: the page's answer (default managed), the Settings row's states, the confirmation texts, the switch and its
// polling, and where the row and the extra rows are mounted. No DOM; the row is also checked in a real browser by hand.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
global.esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
global.coreData = result => result.data;
const mode = require('../js/agent-mode.js');
const trust = require('../js/agent-trust-row.js');

const view = (extra = {}) => ({mode: 'managed', recommended: 'managed', terminals: {available: true, reason: null, detail: null, agents: []},
  busy: [], switching: null, last: null, ...extra});
const failure = (code, message, extra = {}) => Object.assign(new Error(message), {code, serverMessage: message, details: {}, ...extra});
// The extra rows listen on the page for the mode changing.
global.document = {addEventListener() {}, removeEventListener() {}};
const flush = () => new Promise(resolve => setImmediate(resolve));

test('the page is managed until a read says terminal, and a failed or odd read never says it', async () => {
  const fresh = () => { delete require.cache[require.resolve('../js/agent-mode.js')]; return require('../js/agent-mode.js'); };
  let m = fresh();
  assert.equal(m.mode(), 'managed'); assert.equal(m.terminals(), false); assert.equal(m.view(), null);
  await m.load({read: async () => { throw failure('network_unavailable', 'x'); }, retry: [], timeout: 5});
  assert.equal(m.terminals(), false);
  for (const odd of [{}, {mode: 'Terminal'}, {mode: 'tmux'}, null, {mode: 42}]) { m.set(odd); assert.equal(m.terminals(), false, JSON.stringify(odd)); }
  await m.load({read: async () => view({mode: 'terminal'}), retry: []});
  assert.equal(m.mode(), 'terminal'); assert.equal(m.terminals(), true); assert.equal(m.view().mode, 'terminal');
  m = fresh();
  await m.load({read: async () => { throw failure('not_found', 'x', {httpStatus: 404}); }, retry: []});
  assert.equal(m.terminals(), false); assert.equal(m.view(), null);
});

test('load resolves within its limit when the read is slow, and a late answer that says terminal still arrives', async () => {
  delete require.cache[require.resolve('../js/agent-mode.js')];
  const m = require('../js/agent-mode.js');
  let answer; const slow = new Promise(resolve => { answer = resolve; });
  const started = Date.now();
  await m.load({read: () => slow, timeout: 20, retry: []});
  assert.ok(Date.now() - started < 1000);
  assert.equal(m.terminals(), false, 'the page starts managed');
  answer(view({mode: 'terminal'})); await flush();
  assert.equal(m.terminals(), true, 'and repaints when the answer says terminal');
});

test('a failed boot read is tried again in the background', async () => {
  delete require.cache[require.resolve('../js/agent-mode.js')];
  const m = require('../js/agent-mode.js');
  let calls = 0;
  await m.load({read: async () => { if (++calls < 3) throw failure('network_unavailable', 'x'); return view({mode: 'terminal'}); },
    retry: [1, 1], timeout: 200});
  assert.equal(calls, 3); assert.equal(m.terminals(), true);
});

test('agent-mode-changed is dispatched when the mode changes', () => {
  delete require.cache[require.resolve('../js/agent-mode.js')];
  const events = [];
  const saved = global.document;
  global.document = {dispatchEvent: event => events.push(event.detail.mode)};
  global.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init.detail; } };
  try {
    const m = require('../js/agent-mode.js');
    m.set(view()); m.set(view({mode: 'terminal'})); m.set(view({mode: 'terminal'})); m.set(view());
    assert.deepEqual(events, ['terminal', 'managed']);
  } finally { global.document = saved; delete global.CustomEvent; }
});

test('the row: one label, two choices, the current one selected, a help line for it', () => {
  let row = mode.rowState(view());
  assert.equal(row.key, 'ready'); assert.equal(row.label, 'How agents run');
  assert.deepEqual(row.choices.map(c => [c.label, c.selected, c.disabled]), [['Managed sessions (recommended)', true, false], ['Terminals (legacy)', false, false]]);
  assert.equal(row.help, 'Qorqbot starts and stops each agent’s session for you. You only use Chat.');
  row = mode.rowState(view({mode: 'terminal'}));
  assert.deepEqual(row.choices.map(c => [c.selected, c.disabled]), [[false, false], [true, false]]);
  assert.equal(row.help, 'Each agent runs in its own terminal on this computer, as in earlier versions. Only Claude Code agents can run this way.');
});

test('terminals are disabled with the plain reason: no tmux, Codex agents named, anything else', () => {
  const off = terminals => mode.rowState(view({terminals: {available: false, ...terminals}}));
  let row = off({reason: 'tmux_missing', detail: 'tmux not found at /usr/bin'});
  assert.equal(row.choices[1].disabled, true); assert.equal(row.choices[0].disabled, false);
  assert.equal(row.notice, 'Terminals need tmux, which is not installed on this computer.');
  assert.ok(!/\/usr|not found/.test(row.notice), 'the server detail is not shown');
  row = off({reason: 'codex_agents', agents: [{agent_id: 'a1', name: 'Ada'}]});
  assert.equal(row.notice, 'Ada uses Codex. Only Claude Code agents can run in terminals. Set it to Claude Code under Client, model and effort first.');
  row = off({reason: 'codex_agents', agents: [{name: 'Ada'}, {name: 'Atlas'}, {name: 'Bo'}]});
  assert.equal(row.notice, 'Ada, Atlas and Bo use Codex. Only Claude Code agents can run in terminals. Set them to Claude Code under Client, model and effort first.');
  row = off({reason: 'tmux_broken', detail: 'exit 1'});
  assert.equal(row.notice, 'Terminals can’t be used on this computer right now.');
  assert.equal(off({reason: null}).choices[1].disabled, true);
});

test('terminal mode without tmux says so and keeps Managed sessions selectable', () => {
  const row = mode.rowState(view({mode: 'terminal', terminals: {available: false, reason: 'tmux_missing'}}));
  assert.match(row.notice, /terminals need tmux, which is not installed on this computer/);
  assert.equal(row.choices[1].selected, true);
  assert.deepEqual(row.choices.map(c => c.disabled), [false, false]);
});

test('a failed read is a retry, never Terminals selected; a missing route hides the row; a read in progress shows nothing', () => {
  const failed = mode.rowState(null, {loadError: 'x'});
  assert.equal(failed.key, 'unreadable'); assert.equal(failed.notice, 'Couldn’t read this setting.');
  assert.deepEqual(failed.actions, [{id: 'retry', label: 'Try again'}]); assert.deepEqual(failed.choices, []);
  assert.equal(mode.rowState(null, {missing: true}).key, 'hidden');
  assert.equal(mode.rowState(view(), {missing: true}).key, 'hidden');
  assert.equal(mode.rowState(null).key, 'hidden');
});

test('while a switch runs the row shows progress, in either direction, and the choices are disabled', () => {
  let row = mode.rowState(view({switching: {to: 'terminal', step: 'ending', done: 3, total: 10, current: 'Ada'}}));
  assert.equal(row.key, 'switching'); assert.equal(row.progress.text, 'Switching to Terminals: 3 of 10…');
  assert.deepEqual(row.choices.map(c => c.disabled), [true, true]); assert.equal(row.choices[0].selected, true);
  row = mode.rowState(view({mode: 'terminal', switching: {to: 'managed', step: 'ending', done: 1, total: 4, current: null}}));
  assert.equal(row.progress.text, 'Switching to Managed sessions: 1 of 4…');
  row = mode.rowState(view({switching: {to: 'terminal', done: 0, total: 0}}));
  assert.equal(row.progress.text, 'Switching to Terminals…'); assert.equal(row.progress.indeterminate, true);
  row = mode.rowState(view(), {pending: 'terminal'});
  assert.equal(row.key, 'switching'); assert.deepEqual(row.choices.map(c => c.disabled), [true, true]);
});

test('every text the owner reads is plain: no command, path, dash or technical word', () => {
  const texts = [mode.confirmText('terminal'), mode.confirmText('managed'), mode.confirmText('terminal', {trust: true}).trust, ...Object.values(mode.HELP), ...Object.values(mode.DONE), mode.busyText(['Ada', 'Atlas'])]
    .flatMap(item => typeof item === 'string' ? [item] : Object.values(item));
  for (const text of texts) assert.ok(!/—|–|\/|`|cutover|transport|socket|--|\.json/.test(text), text);
});

test('the confirmations say what the owner decided they say', () => {
  const toTerminals = mode.confirmText('terminal'), toManaged = mode.confirmText('managed');
  assert.equal(toTerminals.title, 'Switch to Terminals?');
  assert.equal(toTerminals.body, 'Every agent’s session ends now. Each agent that is on starts again in its own terminal, in the same conversation. From then on an agent that is not running in its terminal receives nothing until it is started. You can switch back here at any time.');
  assert.equal(toManaged.title, 'Switch to Managed sessions?');
  assert.equal(toManaged.body, 'Every agent’s terminal ends now. Agents then start by themselves when you or another agent writes to them, in the same conversation. You can switch back here at any time.');
  assert.deepEqual([toTerminals.confirm, toTerminals.cancel], ['Switch', 'Not now']);
  assert.equal(mode.busyText(['Ada', 'Atlas']), 'Ada and Atlas are in the middle of a turn. Wait for them to finish, or switch now and cut the turn off.');
  assert.equal(mode.busyText(['Ada']), 'Ada is in the middle of a turn. Wait for it to finish, or switch now and cut the turn off.');
  assert.deepEqual(mode.DONE, {terminal: 'Agents now run in terminals.', managed: 'Agents now run as managed sessions.'});
});

test('a switch to Terminals asks for folder trust only when it is not given yet, and never on the way back', () => {
  assert.equal(mode.confirmText('terminal').trust, null);
  assert.equal(mode.confirmText('managed', {trust: true}).trust, null);
  const asked = mode.confirmText('terminal', {trust: true}).trust;
  assert.equal(asked.label, 'Let Qorqbot mark each agent’s own folder as trusted in Claude Code');
  assert.match(asked.help, /Nothing else is trusted, and you can turn it off in Settings\./);
  assert.match(asked.help, /Without it, no agent is started for you/);
  assert.equal(mode.normalize(view()).terminals.trusted, true, 'a server that says nothing is not asked about');
  assert.equal(mode.normalize(view({terminals: {available: true, trusted: false}})).terminals.trusted, false);
});

// ---- the controller

function setup(options = {}) {
  const sent = [], paints = [], done = [], reads = [], notes = [];
  const timers = [];
  let current = options.view || view();
  const ctl = mode.controller({
    read: async () => { reads.push(1); if (options.fail) throw options.fail; return current; },
    act: async (to, force, trust) => { sent.push(trust ? {to, force, trust} : {to, force}); if (options.refuse) { const r = options.refuse(sent.length); if (r) throw r; } return options.answer ? options.answer(to) : view({switching: {to, step: 'ending', done: 0, total: 3}}); },
    onChange: state => paints.push(JSON.stringify(state)), onDone: (text, info) => { done.push(text); notes.push(!!(info && info.note)); },
    schedule: (fn, ms) => { timers.push({fn, ms}); return timers.length; }, cancel: () => {}, hidden: options.hidden || (() => false),
  });
  return {ctl, sent, paints, done, notes, reads, timers, set: next => { current = next; }};
}

test('choosing the other mode sends {mode} once; a second request while one is on its way sends nothing', async () => {
  const t = setup(); await t.ctl.refresh();
  const first = t.ctl.request('terminal'), second = await t.ctl.request('terminal');
  assert.equal(second.busy, true);
  await first;
  assert.deepEqual(t.sent, [{to: 'terminal', force: false}]);
  assert.equal(t.ctl.request && (await t.ctl.request('terminal')).busy, true, 'and none while the view says a switch runs');
  assert.equal(t.sent.length, 1);
});

test('turns_running gives the names, and the forced second request carries force: true', async () => {
  const t = setup({refuse: n => n === 1 ? failure('turns_running', 'x', {details: {agents: [{agent_id: 'a', name: 'Ada'}, {agent_id: 'b', name: 'Atlas'}]}}) : null});
  await t.ctl.refresh();
  const refused = await t.ctl.request('terminal');
  assert.equal(refused.ok, false);
  assert.deepEqual(refused.refusal.names, ['Ada', 'Atlas']);
  assert.equal(refused.refusal.text, 'Ada and Atlas are in the middle of a turn. Wait for them to finish, or switch now and cut the turn off.');
  assert.equal(t.ctl.state.error, '', 'the refusal is asked, not shown as an error');
  const forced = await t.ctl.request('terminal', {force: true});
  assert.equal(forced.ok, true);
  assert.deepEqual(t.sent, [{to: 'terminal', force: false}, {to: 'terminal', force: true}]);
  assert.equal(t.ctl.state.view.switching.to, 'terminal');
});

test('with no names in the refusal the view’s busy agents are used', async () => {
  const t = setup({view: view({busy: [{agent_id: 'a', name: 'Ada'}]}), refuse: () => failure('turns_running', 'x')});
  await t.ctl.refresh();
  assert.deepEqual((await t.ctl.request('managed')).refusal.names, ['Ada']);
});

test('other refusals show the server’s sentence; a bare failure shows a plain one', async () => {
  let t = setup({refuse: () => failure('codex_agents', 'Ada uses Codex. Only Claude Code agents can run in terminals')});
  await t.ctl.refresh(); await t.ctl.request('terminal');
  assert.equal(t.ctl.state.error, 'Ada uses Codex. Only Claude Code agents can run in terminals.');
  t = setup({refuse: () => new Error('')});
  await t.ctl.refresh(); await t.ctl.request('terminal');
  assert.equal(t.ctl.state.error, 'Couldn’t switch. Try again.');
  assert.equal(mode.rowState(t.ctl.state.view, {error: t.ctl.state.error}).error, 'Couldn’t switch. Try again.');
});

test('already in that mode changes nothing and starts no polling', async () => {
  const t = setup({answer: () => ({changed: false, ...view()})});
  await t.ctl.refresh(); t.timers.length = 0;
  await t.ctl.request('managed');
  assert.equal(t.timers.length, 0); assert.equal(t.done.length, 0); assert.equal(t.ctl.state.pending, null);
});

test('polling: reads every 2 seconds until the switch clears, then reports done once', async () => {
  const t = setup();
  await t.ctl.refresh(); await t.ctl.request('terminal');
  assert.equal(t.timers.length, 1); assert.equal(t.timers[0].ms, 2000);
  t.set(view({switching: {to: 'terminal', step: 'starting', done: 2, total: 3}}));
  await t.timers.pop().fn(); await flush();
  assert.equal(t.ctl.state.view.switching.done, 2); assert.equal(t.timers.length, 1);
  t.set(view({mode: 'terminal'}));
  await t.timers.pop().fn(); await flush();
  assert.deepEqual(t.done, ['Agents now run in terminals.']);
  assert.equal(t.ctl.state.done, 'Agents now run in terminals.'); assert.equal(t.timers.length, 0, 'polling stops');
  await t.ctl.refresh(); assert.equal(t.done.length, 1, 'a later read does not reload again');
});

test('a switch seen running after a page reload is watched and finished the same way', async () => {
  const t = setup({view: view({switching: {to: 'managed', step: 'ending', done: 1, total: 2}, mode: 'terminal'})});
  await t.ctl.refresh();
  assert.equal(t.ctl.state.watching, 'managed'); assert.equal(t.timers.length, 1);
  t.set(view({mode: 'managed'}));
  await t.timers.pop().fn(); await flush();
  assert.deepEqual(t.done, ['Agents now run as managed sessions.']);
});

test('a finished switch that carries a note shows the note instead of the plain done text', async () => {
  const t = setup();
  await t.ctl.refresh(); await t.ctl.request('terminal');
  t.set(view({mode: 'terminal', last: {to: 'terminal', ok: true, message: 'Agents now run in terminals. Atlas could not be started; start it in its Terminal'}}));
  await t.timers.pop().fn(); await flush();
  assert.deepEqual(t.done, ['Agents now run in terminals. Atlas could not be started; start it in its Terminal.']);
  assert.deepEqual(t.notes, [true]);
  assert.equal(t.ctl.state.error, '');
});

test('the trust consent travels with the request, also when it is sent again with force', async () => {
  const t = setup(); await t.ctl.refresh();
  await t.ctl.request('terminal', {trust: true});
  assert.deepEqual(t.sent, [{to: 'terminal', force: false, trust: true}]);
  const busy = setup({refuse: n => n === 1 ? Object.assign(new Error('x'), {code: 'turns_running', details: {agents: [{name: 'Ada'}]}}) : null});
  await busy.ctl.refresh();
  assert.deepEqual((await busy.ctl.request('terminal', {trust: true})).refusal.names, ['Ada']);
  await busy.ctl.request('terminal', {force: true, trust: true});
  assert.deepEqual(busy.sent[1], {to: 'terminal', force: true, trust: true});
});

test('the server’s plain done sentence is not a note', async () => {
  const t = setup();
  await t.ctl.refresh(); await t.ctl.request('terminal');
  t.set(view({mode: 'terminal', last: {to: 'terminal', ok: true, message: 'Agents now run in terminals.'}}));
  await t.timers.pop().fn(); await flush();
  assert.deepEqual(t.done, ['Agents now run in terminals.']);
  assert.deepEqual(t.notes, [false]);
});

test('a page that did not see a switch never reloads by itself', async () => {
  const t = setup({view: view({mode: 'terminal', last: {to: 'terminal', ok: true, message: ''}})});
  await t.ctl.refresh(); await t.ctl.refresh();
  assert.equal(t.done.length, 0); assert.equal(t.timers.length, 0); assert.equal(t.ctl.state.error, '');
});

test('a failed switch shows the server’s sentence and the real mode, and does not reload', async () => {
  const t = setup();
  await t.ctl.refresh(); await t.ctl.request('terminal');
  t.set(view({mode: 'managed', last: {to: 'terminal', ok: false, message: 'Ada’s session did not end. Press Switch again to finish.'}}));
  await t.timers.pop().fn(); await flush();
  assert.equal(t.ctl.state.error, 'Ada’s session did not end. Press Switch again to finish.');
  assert.equal(t.done.length, 0);
  const row = mode.rowState(t.ctl.state.view, {error: t.ctl.state.error});
  assert.equal(row.choices[0].selected, true); assert.equal(row.choices[1].selected, false);
  assert.deepEqual(row.choices.map(c => c.disabled), [false, false], 'and the owner can try again');
});

test('a switch that ends with no change and no sentence says so plainly', async () => {
  const t = setup();
  await t.ctl.refresh(); await t.ctl.request('terminal');
  t.set(view({mode: 'managed'}));
  await t.timers.pop().fn(); await flush();
  assert.equal(t.ctl.state.error, 'Couldn’t switch. Try again.'); assert.equal(t.done.length, 0);
});

test('polling pauses while the page is hidden and resumes when it is visible', async () => {
  let hidden = false;
  const t = setup({hidden: () => hidden});
  await t.ctl.refresh(); await t.ctl.request('terminal');
  assert.equal(t.timers.length, 1);
  hidden = true;
  const reads = t.reads.length;
  await t.timers.pop().fn(); await flush();
  assert.equal(t.reads.length, reads, 'a timer that fires while hidden reads nothing'); assert.equal(t.timers.length, 0, 'and sets none');
  hidden = false;
  t.set(view({switching: {to: 'terminal', done: 1, total: 3}}));
  await t.ctl.resume();
  assert.equal(t.reads.length, reads + 1); assert.equal(t.timers.length, 1);
});

test('reads that fail during a switch are tried again, and the owner is told after five', async () => {
  const ctl = mode.controller({read: async () => { throw failure('network_unavailable', 'The network is down.'); }, view: view({switching: {to: 'terminal', done: 0, total: 2}}),
    schedule: () => 1, cancel() {}, hidden: () => false});
  for (let i = 0; i < 4; i++) { await ctl.refresh({quiet: true}); assert.equal(ctl.state.loadError, ''); }
  await ctl.refresh({quiet: true});
  assert.equal(ctl.state.loadError, 'The network is down.');
});

test('a failed first read is the retry state; a 404 or 405 is a missing route; a good read after a failure recovers', async () => {
  let fail = failure('network_unavailable', 'down');
  const ctl = mode.controller({read: async () => { if (fail) throw fail; return view({mode: 'terminal'}); }, schedule: () => 1, cancel() {}});
  await ctl.refresh();
  assert.equal(mode.rowState(ctl.state.view, ctl.state).key, 'unreadable');
  fail = null; await ctl.refresh();
  assert.equal(mode.rowState(ctl.state.view, ctl.state).choices[1].selected, true);
  for (const status of [404, 405]) {
    const missing = mode.controller({read: async () => { throw failure('invalid_response', 'x', {httpStatus: status}); }});
    await missing.refresh();
    assert.equal(mode.rowState(missing.state.view, missing.state).key, 'hidden', String(status));
    assert.equal(missing.state.loadError, '');
  }
  const forbidden = mode.controller({read: async () => { throw failure('forbidden', 'No.', {httpStatus: 403}); }});
  await forbidden.refresh();
  assert.equal(mode.rowState(forbidden.state.view, forbidden.state).key, 'unreadable');
});

test('a stopped controller reads and sends nothing more', async () => {
  const t = setup(); await t.ctl.refresh(); t.ctl.stop();
  const reads = t.reads.length; await t.ctl.refresh(); assert.equal(t.reads.length, reads);
});

// ---- the extra rows (Terminals mode only)

test('the extra rows say what the product said, for Claude Code only', () => {
  const [start, row] = trust.rows({granted: true});
  assert.equal(start.label, 'Starting an agent'); assert.equal(row.label, 'Trusted agent folders');
  assert.equal(row.action.label, 'Turn off'); assert.equal(trust.rows({granted: false})[1].action.label, 'Turn on');
  assert.equal(trust.rows(null, true)[1].action.id, 'retry');
  for (const text of [trust.START, trust.ON, trust.OFF]) assert.ok(!/codex/i.test(text), text);
});

function fakeContainer() {
  const listeners = {};
  return {isConnected: true, style: {}, innerHTML: '', addEventListener: (type, fn) => { listeners[type] = fn; },
    click: async id => listeners.click({target: {closest: () => ({dataset: {atrAction: id}, disabled: false})}})};
}

test('in managed mode the extra rows draw nothing and send no request', async () => {
  const calls = []; const box = fakeContainer();
  const panel = trust.mount(box, {read: async () => { calls.push('read'); return {granted: true}; }, isActive: () => false});
  await flush(); await panel.refresh(); panel.stop();
  assert.deepEqual(calls, []); assert.equal(box.innerHTML, ''); assert.equal(box.style.display, 'none');
});

test('in terminal mode they read the trust setting; a 404 hides them; a failed read offers a retry; Turn off sends granted false', async () => {
  let box = fakeContainer(); const calls = [];
  let panel = trust.mount(box, {isActive: () => true, read: async () => ({granted: true}), revoke: async () => { calls.push('revoke'); }});
  await flush();
  assert.match(box.innerHTML, /Starting an agent/); assert.match(box.innerHTML, /Trusted agent folders/); assert.match(box.innerHTML, /Turn off/);
  await box.click('revoke'); await flush();
  assert.deepEqual(calls, ['revoke']); panel.stop();
  box = fakeContainer();
  panel = trust.mount(box, {isActive: () => true, read: async () => { throw failure('not_found', 'x', {httpStatus: 404}); }});
  await flush(); assert.equal(box.style.display, 'none'); assert.equal(box.innerHTML, ''); panel.stop();
  box = fakeContainer();
  panel = trust.mount(box, {isActive: () => true, read: async () => { throw failure('network_unavailable', 'x'); }});
  await flush(); assert.match(box.innerHTML, /Starting an agent/); assert.match(box.innerHTML, /Could not read this setting\./); assert.match(box.innerHTML, /Try again/);
  panel.stop();
});

test('turning it on opens the consent dialog', async () => {
  const box = fakeContainer(); const calls = [];
  const panel = trust.mount(box, {isActive: () => true, read: async () => ({granted: false}), consent: async () => { calls.push('consent'); }});
  await flush(); assert.match(box.innerHTML, /Turn on/);
  await box.click('grant'); await flush(); assert.deepEqual(calls, ['consent']); panel.stop();
});

// ---- where it is wired

test('Settings mounts the mode row first in Agents and the extra rows after the Claude Code rows, with no terminal word of its own', () => {
  const settings = read('js/settings.js');
  assert.ok(settings.indexOf('data-agent-mode>') < settings.indexOf('data-agent-clients>'));
  assert.ok(settings.indexOf('data-agent-clients>') < settings.indexOf('data-agent-extras>'));
  assert.match(settings, /paintAgentMode\(pane\); paintAgentClients\(pane\)/);
  assert.match(settings, /hint: 'How agents run, Claude Code, Codex, model and effort'/);
  assert.ok(!/terminal/i.test(settings));
  assert.ok(!/data-choice="\$\{[^}]*mode/i.test(settings));
  assert.ok(!read('js/agent-mode.js').includes('data-choice'), 'Settings binds every data-choice to the appearance setting');
});

test('the page loads the mode before every script that asks it, and its stylesheet', () => {
  const page = read('index.html');
  const at = name => page.indexOf(name);
  assert.ok(at('/js/agent-mode.js') > at('/js/core.js') && at('/js/agent-mode.js') < at('/js/settings.js'));
  assert.ok(at('/js/agent-mode.js') < at('/js/xterm.js'));
  for (const later of ['/js/agent-trust-row.js', '/js/agent-start.js', '/js/terminal.js', '/js/global-chat.js', '/js/main.js']) assert.ok(at('/js/agent-mode.js') < at(later), later);
  assert.ok(at('/js/agent-start.js') < at('/js/agent-trust-row.js') || at('/js/agent-trust-row.js') > 0);
  assert.ok(at('/js/terminal.js') < at('/js/global-chat.js'));
  assert.ok(at('/css/xterm.css') < at('/css/global-chat.css'));
  for (const sheet of ['/css/xterm.css', '/css/legacy-terminals.css', '/css/agent-mode.css']) assert.match(page, new RegExp(`href="${sheet.replace(/\./g, '\\.')}"`));
});

test('main.js reads the mode at boot, without waiting for it, before the walkthrough chain', () => {
  const main = read('js/main.js');
  const at = main.indexOf('agentMode.load()');
  assert.ok(at > 0 && at < main.indexOf('loadProject().then('));
  assert.ok(!/await\s+agentMode/.test(main));
});

test('the styles cover the light theme through tokens and the narrow layout', () => {
  const css = read('css/agent-mode.css');
  assert.match(css, /max-width: 860px/);
  assert.ok(!/#[0-9a-f]{3,6}\b/i.test(css), 'colours come from the theme tokens, so both themes follow');
});
