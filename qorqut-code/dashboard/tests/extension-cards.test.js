// The generic card of an extension that follows the contract, drawn from its entry alone, and the Connections screen
// unchanged while no such extension is found.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const cards = require('../js/extension-cards.js');

const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const ORDUBASHY = {agent_id: 'agent-1', name: 'Ordubashy', role: 'ordubashy', on: true, in_effect: false};
const WORKER = {agent_id: 'agent-2', name: 'Researcher', role: 'global-worker', on: false, in_effect: false};
const entry = (status, extra = {}) => ({id: 'keyed', kind: 'contract', name: 'Keyed', summary: 'Tells its agents whether a token is saved.',
  version: '1.0.0', delivery: 'downloadable', status, note: null, can_set_up: !['unsupported', 'not_installed'].includes(status),
  setup: {kind: 'key', fields: [{id: 'token', label: 'Token', secret: true}, {id: 'account', label: 'Account', secret: false}]},
  agents: [ORDUBASHY, WORKER], ...extra});
const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');

test('every contract status has a label', () => {
  const labels = Object.fromEntries(['not_set_up', 'setting_up', 'ready', 'needs_attention', 'unsupported', 'not_installed']
    .map(status => [status, cards.status({status}).label]));
  assert.deepEqual(labels, {not_set_up: 'Not set up', setting_up: 'Setting up', ready: 'Ready', needs_attention: 'Needs attention',
    unsupported: 'Not available', not_installed: 'Not installed'});
  assert.equal(cards.status({status: 'something_new'}).label, 'Unknown');
  assert.equal(cards.status({status: 'ready'}).tone, 'ok');
});

test('a key is set up on the card: Set up, Cancel while setting up, Try again after a refusal, Disconnect when ready', () => {
  assert.deepEqual(cards.steps(entry('not_set_up')), [{kind: 'key', label: 'Set up'}]);
  assert.deepEqual(cards.steps(entry('setting_up')), [{kind: 'key', label: 'Set up'}, {kind: 'cancel', label: 'Cancel', quiet: true}]);
  assert.deepEqual(cards.steps(entry('needs_attention', {note: 'Keyed did not accept this token.'})), [{kind: 'key', label: 'Try again'}]);
  assert.deepEqual(cards.steps(entry('ready')), [{kind: 'disconnect', label: 'Disconnect', quiet: true}]);
  for (const status of ['unsupported', 'not_installed']) assert.deepEqual(cards.steps(entry(status)), []);
  assert.deepEqual(cards.note(entry('needs_attention', {note: 'Keyed did not accept this token.'})),
    {text: 'Keyed did not accept this token.', tone: 'warn'});
  assert.equal(cards.note(entry('ready')), null);
});

test('a refused folder or code part shows only its note: no set-up step, no form and no switches', () => {
  const finding = 'Its code could not be loaded: its tool names must start with \'keyed_\'.';
  const refused = entry('needs_attention', {note: finding, can_set_up: false});
  assert.deepEqual(cards.steps(refused), []);
  assert.deepEqual(cards.switches(refused), []);
  const html = cards.render(refused, {rows: true, esc});
  assert.match(html, /Its code could not be loaded/);
  assert.doesNotMatch(html, /data-ext-action|data-ext-form|data-ext-switch|Try again/);
  // The server decides; a note alone does not take the step away.
  assert.deepEqual(cards.steps(entry('needs_attention', {note: finding})), [{kind: 'key', label: 'Try again'}]);
});

test('a word in backticks in the note is shown as inline code, and the note stays text', () => {
  const html = cards.render(entry('needs_attention', {can_set_up: false,
    note: '`version` must be three numbers, not `<img src=x onerror=alert(1)>`.'}), {rows: true, esc});
  assert.match(html, /<code>version<\/code> must be three numbers, not <code>&lt;img src=x onerror=alert\(1\)(&gt;|>)<\/code>\./);
  assert.doesNotMatch(html, /`|<img/);
});

test('no set-up step for kind none, and sign-in and pairing say they are not available yet', () => {
  const none = entry('ready', {setup: {kind: 'none', fields: []}});
  assert.deepEqual(cards.steps(none), []);
  assert.equal(cards.note(none), null);
  for (const kind of ['sign-in', 'pairing']) {
    const item = entry('not_set_up', {setup: {kind, fields: []}});
    assert.deepEqual(cards.steps(item), []);
    assert.deepEqual(cards.note(item), {text: 'Setting this up from Qorqbot is not available yet.', tone: 'info'});
    const html = cards.render(item, {rows: true, esc});
    assert.doesNotMatch(html, /data-ext-form|data-ext-action|<img|href=/);
    assert.match(html, /Not set up/);
  }
});

test('the card is drawn from the entry: name, summary, status, the fields form and one switch per agent', () => {
  const html = cards.render(entry('setting_up', {note: 'Waiting for the token.'}), {rows: true, esc});
  for (const text of ['Keyed', 'Tells its agents whether a token is saved.', 'Setting up', 'Waiting for the token.', 'Ordubashy', 'Researcher'])
    assert.ok(html.includes(text), text);
  assert.match(html, /<form class="cx-key" data-ext-form="keyed" hidden>/);
  assert.match(html, /type="password" name="token" autocomplete="off"/);
  assert.match(html, /type="text" name="account" autocomplete="off"/);
  assert.doesNotMatch(html, /value=/, 'nothing is filled in');
  assert.match(html, /data-agent-id="agent-1" checked>/);
  assert.match(html, /data-agent-id="agent-2">/);
  assert.match(html, /On once it is ready/);
  assert.doesNotMatch(cards.render(entry('ready'), {rows: true, esc}), /data-ext-form/, 'a set-up extension has no form');
  assert.match(cards.render(entry('ready'), {esc}), /^<article class="cx-card" data-cx-card="keyed" data-cx-contract/);
  assert.doesNotMatch(cards.render(entry('unsupported'), {rows: true, esc}), /data-ext-switch|data-ext-action|data-ext-form/);
  assert.match(cards.render(entry('ready', {name: '<b>', summary: '"x"'}), {rows: true, esc}), /&lt;b>.*&quot;x&quot;/s);
});

test('the requests follow the shapes of qorqut.extension_connections', () => {
  assert.equal(cards.path('keyed'), '/root/connections/keyed');
  assert.deepEqual(cards.request.start(), {action: 'start'});
  assert.deepEqual(cards.request.cancel(), {action: 'cancel'});
  assert.deepEqual(cards.request.disconnect(), {action: 'disconnect'});
  assert.deepEqual(cards.request.accept({token: 't'}), {action: 'accept', values: {token: 't'}});
  assert.deepEqual(cards.request.switch('agent-1', false), {action: 'switch', agent_id: 'agent-1', on: false});
  const shapes = read('../app/qorqut/extension_connections.py');
  for (const action of ['start', 'accept', 'cancel', 'disconnect', 'switch']) assert.ok(shapes.includes(`{"action": "${action}"`), action);
});

test('the card script is loaded before the Connections panel', () => {
  const index = read('index.html');
  assert.ok(index.indexOf('/js/extension-cards.js') > 0 && index.indexOf('/js/extension-cards.js') < index.indexOf('/js/connectors.js'));
});

// The Connections panel drawn in a small stand-in for the page: every element a selector asks for is kept, so what the
// panel writes into it can be read back. Timers never fire and only the extensions list answers.
function drawPanel(source, connectors, options) {
  class Element {
    constructor() { this.parts = new Map(); this.innerHTML = ''; this.hidden = false; this.dataset = {}; this.isConnected = true; this.disabled = false; }
    querySelector(selector) {
      if (selector.includes(':not([hidden])')) return null;
      if (!this.parts.has(selector)) this.parts.set(selector, new Element());
      return this.parts.get(selector);
    }
    querySelectorAll() { return []; }
    addEventListener() {}
    dispatchEvent() {}
    appendChild(child) { this.child = child; }
  }
  const document = {createElement: () => new Element(), activeElement: null, visibilityState: 'visible', addEventListener() {}, body: new Element()};
  const container = Object.assign(new Element(), {ownerDocument: document});
  const context = vm.createContext({
    document,
    window: {addEventListener() {}},
    MutationObserver: class { observe() {} disconnect() {} },
    CustomEvent: class {},
    setTimeout: () => 0, clearTimeout() {},
    coreData: result => result,
    api: async route => route === '/root/connectors' ? {connectors: structuredClone(connectors), platform: 'linux'} : new Promise(() => {}),
    post: () => new Promise(() => {}),
  });
  vm.runInContext(`const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));`, context);
  vm.runInContext(read('js/extension-cards.js'), context);
  vm.runInContext(`${source}\n;globalThis.panel = connectorsPanel;`, context);
  context.panel.mount(container, options);
  return container.child;
}

test('with no contract extension the Connections panel draws exactly as before', async t => {
  let before;
  try { before = execFileSync('git', ['show', 'origin/dev:qorqut-code/dashboard/js/connectors.js'], {cwd: __dirname, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore']}); }
  catch { t.skip('the version before is not available here'); return; }
  const today = [
    {id: 'openrouter', kind: 'extension', clients: ['claude-code', 'codex'], status: 'not_set_up'},
    {id: 'treg', kind: 'extension', clients: ['claude-code', 'codex'], status: 'connected'},
    {id: 'whatsapp', kind: 'extension', clients: ['claude-code', 'codex'], status: 'not_installed'},
  ];
  for (const options of [{}, {rows: true}, {rows: true, compact: true}]) {
    const [now, then] = [read('js/connectors.js'), before].map(source => drawPanel(source, today, options));
    await new Promise(resolve => setImmediate(resolve));
    const list = root => root.querySelector('[data-cx-list]').innerHTML;
    assert.ok(list(now).includes('data-cx-card="treg"') || list(now).includes('data-cx-id="treg"'), 'today\'s cards are drawn');
    assert.equal(list(now), list(then));
    assert.equal(now.innerHTML, then.innerHTML);
    assert.equal(now.className, then.className);
  }
  // The same panel with a contract extension adds its card after today's.
  const keyed = drawPanel(read('js/connectors.js'), [...today, entry('not_set_up')], {rows: true});
  await new Promise(resolve => setImmediate(resolve));
  assert.match(keyed.querySelector('[data-cx-list]').innerHTML, /data-cx-contract/);
});
