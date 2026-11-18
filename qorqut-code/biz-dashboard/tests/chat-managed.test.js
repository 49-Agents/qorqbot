'use strict';
// node --test biz-dashboard/tests/chat-managed.test.js
// Chat for an agent Qorqbot runs itself (js/agent-runtime.js and its use in js/global-chat.js and js/slash-commands.js): the
// session wording, Stop, the activity line, permission and question cards, the runtime switch, the hidden Terminal and the managed
// slash-command refusal, plus proof that a session without `execution` still reads, and is sent to, as a terminal one.
// The bar, switch and shell run against the small fake DOM below; global-chat.js functions are cut out of the source and run
// against fakes (no browser, no server, no dependencies).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// ---- a minimal fake DOM: just what js/agent-clients.js touches (innerHTML parsing, attribute selectors, bubbling, focus).
const VOID = new Set(['img', 'br', 'input', 'hr']);
const decode = text => text.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
const kebab = name => name.replace(/[A-Z]/g, c => '-' + c.toLowerCase());
class FakeNode {
  constructor(tag, doc) {
    this.tag = tag; this.doc = doc; this.attrs = {}; this.nodes = []; this.parent = null; this.listeners = {}; this.style = {}; this.inert = false;
    const self = this;
    this.dataset = new Proxy({}, {
      get: (_, key) => self.attrs['data-' + kebab(String(key))],
      set: (_, key, value) => { self.attrs['data-' + kebab(String(key))] = String(value); return true; },
    });
  }
  get className() { return this.attrs.class || ''; } set className(v) { this.attrs.class = String(v); }
  get hidden() { return 'hidden' in this.attrs; } set hidden(v) { if (v) this.attrs.hidden = ''; else delete this.attrs.hidden; }
  get disabled() { return 'disabled' in this.attrs; } set disabled(v) { if (v) this.attrs.disabled = ''; else delete this.attrs.disabled; }
  get children() { return this.nodes.filter(n => n instanceof FakeNode); }
  get firstElementChild() { return this.children[0] || null; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === this.doc.root; }
  get textContent() { return this.nodes.map(n => (n instanceof FakeNode ? n.textContent : n.data)).join(''); }
  set textContent(v) { this.#empty(); this.nodes = [{data: String(v), parent: this}]; }
  setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  #empty() { for (const n of this.nodes) { n.parent = null; this.doc.detached(n); } this.nodes = []; }
  append(...kids) { for (const kid of kids) { if (kid.parent) kid.remove(); kid.parent = this; this.nodes.push(kid); } }
  replaceChildren(...kids) { this.#empty(); this.append(...kids); }
  remove() { if (!this.parent) return; this.parent.nodes = this.parent.nodes.filter(n => n !== this); this.parent = null; this.doc.detached(this); }
  contains(other) { for (let n = other; n; n = n.parent) if (n === this) return true; return false; }
  set innerHTML(html) { this.#empty(); this.append(...this.doc.parse(html)); }
  get innerHTML() { return this.textContent; }
  insertAdjacentHTML(where, html) {
    assert.equal(where, 'afterend');
    const siblings = this.parent.nodes, at = siblings.indexOf(this) + 1, kids = this.doc.parse(html);
    kids.forEach(k => { k.parent = this.parent; }); siblings.splice(at, 0, ...kids);
  }
  matches(sel) {
    const parts = sel.match(/^[a-z0-9]+|\.[\w-]+|\[[\w-]+(?:="[^"]*")?\]|:not\(.*\)$/g) || [];
    return parts.every(part => {
      if (part.startsWith('.')) return this.className.split(/\s+/).includes(part.slice(1));
      if (part.startsWith('[')) { const [, k, v] = part.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/); return k in this.attrs && (v === undefined || this.attrs[k] === v); }
      if (part.startsWith(':not(')) return !this.matches(part.slice(5, -1));
      return this.tag === part;
    });
  }
  // Compound selectors, and descendants separated by a space.
  querySelectorAll(sel) {
    const chain = sel.trim().split(/\s+/), out = [];
    const within = (n, depth) => { for (const k of n.children) {
      if (k.matches(chain[depth])) { if (depth === chain.length - 1) { if (!out.includes(k)) out.push(k); } else within(k, depth + 1); }
      within(k, depth);
    } };
    within(this, 0); return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { for (let n = this; n instanceof FakeNode; n = n.parent) if (n.matches(sel)) return n; return null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(f => f !== fn); }
  dispatchEvent(event) {
    event.target ||= this;
    if (event.type === 'click' && event.target.disabled) return false; // a disabled button is never clicked
    for (let n = this; n; n = n.parent) {
      for (const fn of [...(n.listeners[event.type] || [])]) fn(event);
      if (event.type === 'click' && typeof n.onclick === 'function') n.onclick(event);
      if (!event.bubbles) break;
    }
    return true;
  }
  focus() { if (!this.disabled) this.doc.activeElement = this; }
  get parentNode() { return this.parent; }
  get value() { return '_value' in this ? this._value : this.attrs.value ?? ''; } set value(v) { this._value = String(v); }
  prepend(...kids) { for (const kid of kids) { if (kid.parent) kid.remove(); kid.parent = this; } this.nodes.unshift(...kids); }
  insertBefore(kid, ref) { if (kid.parent) kid.remove(); kid.parent = this; this.nodes.splice(this.nodes.indexOf(ref), 0, kid); return kid; }
}
class FakeDocument extends FakeNode {
  constructor() {
    super('#document', null); this.doc = this;
    this.root = this; this.body = new FakeNode('body', this); this.append(this.body);
    this.activeElement = this.body; this.visibilityState = 'visible'; this.created = 0; this.failOn = null;
  }
  createElement(tag) { this.created++; if (this.failOn === 'createElement') throw new Error('no screen expected'); return new FakeNode(tag, this); }
  detached(node) { if (node instanceof FakeNode && node.contains(this.activeElement)) this.activeElement = this.body; }
  addEventListener(type, fn) { if (this.failOn === 'addEventListener') throw new Error('listener failed'); super.addEventListener(type, fn); }
  parse(html) {
    const top = new FakeNode('div', this); let at = top;
    const token = /<!--[\s\S]*?-->|<\/([a-z0-9]+)\s*>|<([a-z0-9]+)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>|([^<]+|<)/gi;
    for (let m; (m = token.exec(html));) {
      if (m[1]) { if (at.tag === m[1].toLowerCase() && at.parent) at = at.parent; }
      else if (m[2]) {
        const el = new FakeNode(m[2].toLowerCase(), this);
        for (const a of (m[3] || '').matchAll(/([^\s=>\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) el.attrs[a[1]] = decode(a[2] ?? a[3] ?? a[4] ?? '');
        at.append(el); if (!VOID.has(el.tag) && !/\/>$/.test(m[0])) at = el;
      } else if (m[4]) at.append({data: decode(m[4]), parent: null});
    }
    const kids = [...top.nodes]; top.nodes = []; kids.forEach(k => { k.parent = null; }); return kids;
  }
}
let doc, visited;
function freshPage(...siblings) {
  doc = new FakeDocument(); global.document = doc;
  global.CustomEvent = global.Event = class { constructor(type, init = {}) { this.type = type; this.bubbles = !!init.bubbles; } };
  visited = {}; global.sessionStorage = {getItem: key => visited[key] ?? null, setItem: (key, value) => { visited[key] = value; }};
  const page = siblings.map(name => { const el = doc.createElement('main'); el.id = name; doc.body.append(el); return el; });
  doc.created = 0; return page;
}

const click = el => el.dispatchEvent({type: 'click', target: el, bubbles: true});
const press = (el, key) => { const event = {type: 'keydown', key, target: el, bubbles: true, prevented: false, preventDefault() { this.prevented = true; }}; el.dispatchEvent(event); return event; };
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const runtime = require('../js/agent-runtime.js');
const slashCommands = require('../js/slash-commands.js');
global.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

const BINDING = {provider: 'claude', conversation_id: 'conversation-1', binding_id: 'binding-1'};
const PAYLOAD = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
const managed = (over = {}, session = {}) => ({provider: 'claude', state: 'live', can_send: true, binding: BINDING, working: false, execution: 'managed',
  managed: {open: true, phase: 'idle', activity: '', turn_started_at: null, can_stop: true, pending: [], ...over}, ...session});
const closedSession = () => ({provider: 'claude', state: 'offline', reason: 'session_closed', can_send: true, binding: null, execution: 'managed',
  managed: {open: false, phase: 'idle', activity: '', turn_started_at: null, can_stop: false, pending: []}});
const terminalSession = (over = {}) => ({provider: 'claude', state: 'live', can_send: true, binding: BINDING, working: false, ...over});
const permission = (over = {}) => ({request_id: 'req-1', kind: 'permission', tool: 'Bash', input_summary: 'npm test', questions: [], ...over});
const question = (over = {}) => ({request_id: 'req-2', kind: 'question', tool: 'AskUserQuestion', input_summary: '', questions: [
  {question: 'Which branch?', header: 'Branch', multiSelect: false, options: [{label: 'main', description: 'The default'}, {label: 'dev', description: ''}]}], ...over});

// One bar on a fresh page; `sent` records every post, `fail` makes the next ones throw.
function bar(over = {}) {
  freshPage();
  const sent = [], changes = [], errors = [];
  const state = {fail: null, hold: null, binding: 'binding-1', time: 1000};
  const made = runtime.createBar({label: 'Ada', doc: doc,
    send: async (route, body) => { sent.push([route, body]); if (state.hold) await state.hold; if (state.fail) throw state.fail; return {}; },
    binding: () => state.binding, onChange: () => changes.push(1), onError: text => errors.push(text), now: () => state.time, ...over});
  doc.body.append(made.el);
  return {...made, sent, changes, errors, state};
}
const cardsOf = ui => ui.el.querySelectorAll('.mchat-card');

test('a session without `execution` is a terminal one: nothing managed shows, whatever else it carries', () => {
  for (const session of [terminalSession(), {...terminalSession(), execution: 'terminal'}, {...terminalSession(), execution: undefined, managed: {open: true}}, null, undefined]) {
    assert.equal(runtime.execution(session), 'terminal');
    assert.equal(runtime.isManaged(session), false);
    assert.equal(runtime.managedOf(session), null);
    assert.equal(runtime.summary(session, 'Ada'), null);
    assert.equal(runtime.startHint(session, 'Ada'), null);
    assert.equal(runtime.emptyText(session, 'Ada'), null);
    assert.equal(runtime.refusalText(session, 'terminal_offline'), null);
    assert.equal(runtime.canOpen(session), false);
    assert.equal(runtime.closed(session), false);
    assert.deepEqual(runtime.activity(session), {text: '', stop: false});
  }
  const ui = bar();
  ui.update(terminalSession({managed: {open: true, phase: 'working', can_stop: true, pending: [permission()]}}));
  assert.equal(ui.el.hidden, true);
  assert.equal(cardsOf(ui).length, 0);
});

test('a closed managed session says sending a message starts it and never mentions a terminal or `claude`', () => {
  const session = closedSession();
  assert.deepEqual(runtime.summary(session, 'Ada'), {key: 'closed', line: 'Sending a message starts Ada.'});
  assert.equal(runtime.startHint(session, 'Ada'), 'Send a message to start Ada');
  assert.equal(runtime.emptyText(session, 'Ada'), 'Send a message to start Ada. Its conversation will appear here.');
  assert.equal(runtime.canOpen(session), true);
  for (const text of [runtime.summary(session, 'Ada').line, runtime.startHint(session, 'Ada'), runtime.emptyText(session, 'Ada'), runtime.refusalText(session, 'terminal_offline')]) {
    assert.doesNotMatch(text, /terminal|claude|>_/i);
  }
  // Closed by the server's word or by the managed summary alone.
  assert.equal(runtime.closed({...closedSession(), reason: undefined}), true);
  assert.equal(runtime.closed({...closedSession(), managed: undefined, reason: 'session_closed'}), true);
  assert.equal(runtime.canOpen({...closedSession(), can_send: false}), false);
  assert.equal(runtime.canOpen({...closedSession(), binding: BINDING}), false);
});

test('an open managed session reads starting, live, working or disabled like a terminal one', () => {
  assert.equal(runtime.summary(managed({phase: 'starting'}), 'Ada').key, 'starting');
  assert.equal(runtime.summary(managed({phase: 'idle'}), 'Ada').key, 'live');
  assert.equal(runtime.summary(managed({phase: 'working'}), 'Ada').key, 'working');
  assert.equal(runtime.summary(managed({phase: 'blocked'}), 'Ada').key, 'working');
  assert.equal(runtime.summary(managed({phase: ''}, {working: true}), 'Ada').key, 'working');
  assert.equal(runtime.summary(managed(), 'Ada', false).key, 'starting');
  assert.equal(runtime.summary(managed({}, {reason: 'binding_not_ready'}), 'Ada').key, 'starting');
  assert.equal(runtime.summary(managed({}, {reason: 'capture_disabled'}), 'Ada').key, 'disabled');
  assert.equal(runtime.summary(managed({}, {state: 'offline', can_send: false}), 'Ada').key, 'offline');
});

test('Stop is shown only while the agent works, and only when the server says it can stop', () => {
  const ui = bar(), stop = () => ui.el.querySelector('.mchat-stop');
  for (const [phase, can, shown] of [['idle', true, false], ['starting', true, false], ['closing', true, false], ['working', true, true], ['blocked', true, true], ['working', false, false]]) {
    ui.update(managed({phase, can_stop: can}));
    assert.equal(stop().hidden, !shown, `${phase} ${can}`);
  }
  ui.update(closedSession());
  assert.equal(stop().hidden, true);
  assert.equal(ui.el.querySelector('.mchat-activity').hidden, true);
});

test('Stop posts the interrupt with the live binding, stays disabled while it is in flight, and refuses a second press', async () => {
  const ui = bar(); let release; ui.state.hold = new Promise(done => { release = done; });
  ui.update(managed({phase: 'working'}));
  const stop = ui.el.querySelector('.mchat-stop');
  click(stop); click(stop);
  assert.equal(stop.disabled, true);
  assert.deepEqual(ui.sent, [['/interrupt', {expected_binding: 'binding-1'}]]);
  release(); await flush();
  assert.equal(stop.disabled, false);
  assert.equal(ui.changes.length, 1);
  assert.deepEqual(ui.errors, []);
});

test('a failed Stop tells the owner and a missing binding posts nothing', async () => {
  const ui = bar(); ui.state.fail = Object.assign(new Error('no'), {code: 'terminal_changed'});
  ui.update(managed({phase: 'working'}));
  click(ui.el.querySelector('.mchat-stop')); await flush();
  assert.deepEqual(ui.errors, ['Couldn’t stop. Try again.']);
  ui.errors.length = 0; ui.sent.length = 0; ui.state.binding = null;
  click(ui.el.querySelector('.mchat-stop')); await flush();
  assert.deepEqual(ui.sent, []);
  assert.equal(ui.errors.length, 1);
});

test('the activity line shows what the agent is doing as text, and waits for the owner when a card is open', () => {
  const ui = bar(), text = () => ui.el.querySelector('.mchat-activity-text').textContent, line = () => ui.el.querySelector('.mchat-activity');
  ui.update(managed({phase: 'working', activity: 'Reading src/app.py'}));
  assert.equal(line().hidden, false);
  assert.equal(text(), 'Reading src/app.py');
  ui.update(managed({phase: 'working', activity: ''}));
  assert.equal(text(), 'Working');
  ui.update(managed({phase: 'blocked', activity: 'Running Bash', pending: [permission()]}));
  assert.equal(text(), 'Waiting for your answer');
  ui.update(managed({phase: 'idle', activity: 'Reading src/app.py'}));
  assert.equal(line().hidden, true);
  assert.equal(runtime.activity(managed({phase: 'working', activity: 'x'.repeat(500)})).text.length, 160);
});

test('activity, tool, summary, question and option text from the server is escaped: no element is ever created from it', () => {
  const ui = bar();
  ui.update(managed({phase: 'working', activity: PAYLOAD}));
  for (const tag of ['img', 'script']) assert.equal(ui.el.querySelector(tag), null, tag);
  assert.ok(ui.el.querySelector('.mchat-activity-text').textContent.includes('<img src=x'));
  ui.update(managed({phase: 'working', activity: PAYLOAD, pending: [
    permission({tool: PAYLOAD, input_summary: PAYLOAD}),
    question({request_id: 'req-"><script>alert(3)</script>', questions: [{question: PAYLOAD, header: PAYLOAD, options: [{label: PAYLOAD, description: PAYLOAD}]}]})]}));
  for (const tag of ['img', 'script']) assert.equal(ui.el.querySelector(tag), null, tag);
  assert.ok(ui.el.querySelector('.mchat-card-title').textContent.includes('<img src=x'));
  assert.ok(ui.el.querySelector('.mchat-card-input').textContent.includes('<script>'));
  assert.ok(ui.el.querySelector('.mchat-question').textContent.includes('<img src=x'));
  assert.equal(cardsOf(ui)[1].dataset.requestId, 'req-"><script>alert(3)</script>');
  for (const card of [permission({tool: PAYLOAD, input_summary: PAYLOAD}), question({questions: [{question: PAYLOAD, options: [{label: PAYLOAD, description: PAYLOAD}]}]})]) {
    assert.doesNotMatch(runtime.cardHtml(card, PAYLOAD), /<img|<script/);
  }
  assert.equal(runtime.escapeHtml(`<a href="x">'&`), '&lt;a href=&quot;x&quot;&gt;&#39;&amp;');
});

test('a permission request is a card with Allow and Deny, keyboard reachable, that posts the answer with the request and binding', async () => {
  const ui = bar(); ui.update(managed({phase: 'blocked', pending: [permission()]}));
  const [card] = cardsOf(ui);
  assert.equal(card.getAttribute('role'), 'group');
  assert.match(card.getAttribute('aria-label'), /Permission/);
  assert.match(card.querySelector('.mchat-card-title').textContent, /^Ada wants to use Bash$/);
  assert.equal(card.querySelector('.mchat-card-input').textContent, 'npm test');
  const buttons = card.querySelectorAll('button');
  assert.deepEqual(buttons.map(b => b.textContent), ['Allow', 'Deny']);
  // Native buttons: focusable and operable from the keyboard with no extra wiring, and none is taken out of the tab order.
  for (const b of buttons) { assert.equal(b.getAttribute('type'), 'button'); assert.equal(b.getAttribute('tabindex'), null); }
  click(buttons[0]); await flush();
  assert.deepEqual(ui.sent, [['/answer', {expected_binding: 'binding-1', request_id: 'req-1', decision: 'allow'}]]);
  const again = bar(); again.update(managed({pending: [permission()]}));
  click(cardsOf(again)[0].querySelectorAll('button')[1]); await flush();
  assert.deepEqual(again.sent[0][1].decision, 'deny');
});

test('buttons stay disabled while the answer is in flight and after it is taken, and a failed answer re-enables them with a message', async () => {
  const ui = bar(); let release; ui.state.hold = new Promise(done => { release = done; });
  ui.update(managed({pending: [permission()]}));
  const [card] = cardsOf(ui), [allow, deny] = card.querySelectorAll('button');
  click(allow); click(deny); click(allow);
  assert.equal(allow.disabled && deny.disabled, true);
  assert.equal(ui.sent.length, 1);
  release(); await flush();
  assert.equal(allow.disabled && deny.disabled, true, 'sent: waiting for the server to clear the card');
  assert.equal(ui.changes.length, 1);
  // The server still lists it long after: let the owner try again.
  ui.state.time += 9000; ui.update(managed({pending: [permission()]}));
  assert.equal(allow.disabled || deny.disabled, false);

  const failing = bar(); failing.state.fail = Object.assign(new Error('no'), {code: 'internal'});
  failing.update(managed({pending: [permission()]}));
  const [again] = cardsOf(failing), [yes] = again.querySelectorAll('button');
  click(yes); await flush();
  assert.equal(yes.disabled, false);
  assert.equal(again.querySelector('.mchat-card-error').hidden, false);
  assert.equal(again.querySelector('.mchat-card-error').textContent, 'Couldn’t send your answer. Try again.');
  failing.state.fail = Object.assign(new Error('gone'), {code: 'not_found'});
  click(yes); await flush();
  assert.equal(again.querySelector('.mchat-card-error').textContent, 'This is no longer waiting for an answer.');
});

test('cards follow the pending list: the same card is kept (with what was typed), new ones appear, answered ones go', () => {
  const ui = bar(); ui.update(managed({pending: [permission(), question()]}));
  const [first, second] = cardsOf(ui);
  assert.deepEqual(cardsOf(ui).map(c => c.dataset.requestId), ['req-1', 'req-2']);
  second.querySelector('[data-q-other]').value = 'typed';
  ui.update(managed({phase: 'blocked', activity: 'newer', pending: [permission(), question()]}));
  assert.equal(cardsOf(ui)[0], first); assert.equal(cardsOf(ui)[1], second);
  assert.equal(second.querySelector('[data-q-other]').value, 'typed');
  ui.update(managed({pending: [question(), permission({request_id: 'req-3'})]}));
  assert.deepEqual(cardsOf(ui).map(c => c.dataset.requestId), ['req-2', 'req-3']);
  ui.update(managed({pending: []}));
  assert.equal(cardsOf(ui).length, 0);
  assert.equal(ui.el.querySelector('.mchat-cards').hidden, true);
  // A closed session shows no card even if a stale one is listed; an unknown kind or a missing id is not drawn.
  ui.update(managed({pending: [permission({kind: 'other'}), permission({request_id: ''})]}));
  assert.equal(cardsOf(ui).length, 0);
  ui.update({...closedSession(), managed: {open: false, pending: [permission()]}});
  assert.equal(cardsOf(ui).length, 0);
});

test('a question card sends the chosen option and typed text as the answer; an unanswered question posts nothing', async () => {
  const ui = bar(); ui.update(managed({pending: [question()]}));
  const [card] = cardsOf(ui), send = card.querySelector('[data-answer="submit"]');
  assert.equal(card.querySelector('legend').textContent.includes('Which branch?'), true);
  assert.equal(card.querySelector('.mchat-question-tag').textContent, 'Branch');
  assert.deepEqual(card.querySelectorAll('[data-q]').map(i => [i.getAttribute('type'), i.value]), [['radio', 'main'], ['radio', 'dev']]);
  assert.match(card.querySelector('.mchat-option').textContent, /The default/);
  click(send); await flush();
  assert.deepEqual(ui.sent, []);
  assert.match(card.querySelector('.mchat-card-error').textContent, /Choose or type an answer/);
  card.querySelectorAll('[data-q]')[1].checked = true;
  click(send); await flush();
  assert.deepEqual(ui.sent, [['/answer', {expected_binding: 'binding-1', request_id: 'req-2', decision: {answers: {'Which branch?': 'dev'}}}]]);

  const typed = bar(); typed.update(managed({pending: [question()]}));
  const [t] = cardsOf(typed); t.querySelector('[data-q-other]').value = ' release/9 ';
  const event = press(t.querySelector('[data-q-other]'), 'Enter'); await flush();
  assert.equal(event.prevented, true);
  assert.deepEqual(typed.sent[0][1].decision, {answers: {'Which branch?': 'release/9'}});
});

test('multi-select and several questions: every question needs an answer, choices are joined', async () => {
  const two = [{question: 'Which files?', multiSelect: true, options: [{label: 'a.py'}, {label: 'b.py'}]}, {question: 'Why?', options: []}];
  const ui = bar(); ui.update(managed({pending: [question({questions: two})]}));
  const [card] = cardsOf(ui);
  assert.deepEqual(card.querySelectorAll('[data-q]').map(i => i.getAttribute('type')), ['checkbox', 'checkbox']);
  assert.equal(card.querySelectorAll('[data-q-other]')[1].getAttribute('placeholder'), 'Your answer…');
  card.querySelectorAll('[data-q]').forEach(i => { i.checked = true; });
  click(card.querySelector('[data-answer="submit"]')); await flush();
  assert.equal(ui.sent.length, 0, 'the second question is unanswered');
  card.querySelectorAll('[data-q-other]')[1].value = 'speed';
  click(card.querySelector('[data-answer="submit"]')); await flush();
  assert.deepEqual(ui.sent[0][1].decision, {answers: {'Which files?': 'a.py, b.py', 'Why?': 'speed'}});
  assert.deepEqual(runtime.questionList(['Plain?']), [{question: 'Plain?', header: '', options: [], multi: false}]);
});

test('without a binding an answer posts nothing and says so', async () => {
  const ui = bar(); ui.state.binding = null; ui.update(managed({pending: [permission()]}));
  const [card] = cardsOf(ui); click(card.querySelector('button')); await flush();
  assert.deepEqual(ui.sent, []);
  assert.match(card.querySelector('.mchat-card-error').textContent, /Not connected/);
});

// ---- the runtime switch
function toggle() {
  freshPage();
  const sent = [], errors = [], done = [], state = {fail: null, hold: null};
  const made = runtime.createToggle({label: 'Ada', doc,
    send: async (route, body) => { sent.push([route, body]); if (state.hold) await state.hold; if (state.fail) throw state.fail; return {}; },
    onDone: () => done.push(1), onError: text => errors.push(text)});
  doc.body.append(made.el);
  return {...made, sent, errors, done, state, buttons: () => made.el.querySelectorAll('[data-runtime]')};
}

test('the switch marks the current way, posts the other one, and reloads when the server accepts', async () => {
  const ui = toggle(); ui.update(terminalSession(), true);
  assert.equal(ui.el.hidden, false);
  assert.equal(ui.el.getAttribute('aria-label'), 'How Ada runs');
  assert.deepEqual(ui.buttons().map(b => [b.textContent, b.getAttribute('aria-pressed'), b.disabled]), [['Terminal', 'true', true], ['Managed', 'false', false]]);
  click(ui.buttons()[0]); assert.deepEqual(ui.sent, []);
  click(ui.buttons()[1]); await flush();
  assert.deepEqual(ui.sent, [['/runtime', {mode: 'managed'}]]);
  assert.equal(ui.done.length, 1);
  ui.update(managed(), true);
  assert.deepEqual(ui.buttons().map(b => b.getAttribute('aria-pressed')), ['false', 'true']);
  click(ui.buttons()[0]); await flush();
  assert.deepEqual(ui.sent[1], ['/runtime', {mode: 'terminal'}]);
});

test('the switch disables both buttons while a change is in flight and shows the server’s refusal in plain words', async () => {
  const ui = toggle(); let release; ui.state.hold = new Promise(done => { release = done; });
  ui.update(terminalSession(), true);
  click(ui.buttons()[1]); click(ui.buttons()[1]);
  assert.deepEqual(ui.buttons().map(b => b.disabled), [true, true]);
  assert.equal(ui.sent.length, 1);
  ui.state.fail = Object.assign(new Error('Conflict'), {code: 'session_open', httpStatus: 409});
  release(); await flush();
  assert.deepEqual(ui.errors, ['A session is still open. End it, then switch.']);
  assert.equal(ui.done.length, 0);
  assert.equal(ui.buttons()[1].disabled, false);
});

test('refusal wording: each known reason, the server’s own plain message, and a safe default', () => {
  const refuse = (code, message = '', details) => runtime.runtimeRefusal(Object.assign(new Error(message), {code, details}));
  assert.match(refuse('session_open'), /session is still open/i);
  assert.match(refuse('waiting_inputs'), /still waiting/);
  assert.match(refuse('terminal_present'), /Terminal is still running/);
  assert.match(refuse('unsupported_role'), /worker agents/);
  assert.match(refuse('unsupported_runtime'), /Claude Code/);
  assert.match(refuse('conflict', '', {reason: 'session_open'}), /session is still open/i);
  assert.equal(refuse('some_new_code', 'Claude Code is not set up on this computer.'), 'Claude Code is not set up on this computer.');
  assert.equal(refuse('network_unavailable', 'Qorqbot is not responding.'), 'Couldn’t switch. Try again.');
  assert.equal(refuse('x', 'y'.repeat(300)), 'Couldn’t switch. Try again.');
  assert.equal(refuse(''), 'Couldn’t switch. Try again.');
});

test('an open managed session offers End session, which posts the close; a closed or terminal one does not', async () => {
  const ui = toggle(), end = () => ui.el.querySelector('[data-runtime-close]');
  ui.update(managed(), true); assert.equal(end().hidden, false);
  click(end()); await flush();
  assert.deepEqual(ui.sent, [['/close', {}]]);
  assert.equal(ui.done.length, 1);
  ui.update(closedSession(), true); assert.equal(end().hidden, true);
  ui.update(terminalSession(), true); assert.equal(end().hidden, true);
  ui.state.fail = new Error('x'); ui.update(managed(), true); click(end()); await flush();
  assert.deepEqual(ui.errors, ['Couldn’t end the session. Try again.']);
});

test('the switch is offered to Claude Code workers and to any agent already managed, and to nothing else', () => {
  const worker = {role: {id: 'global-worker'}}, codexWorker = {role: {id: 'global-worker'}, runtime: 'codex'};
  assert.equal(runtime.eligible(worker, terminalSession()), true);
  assert.equal(runtime.eligible({role_id: 'global-worker', runtime: 'claude-code'}, terminalSession()), true);
  assert.equal(runtime.eligible(codexWorker, terminalSession()), false);
  assert.equal(runtime.eligible(worker, terminalSession({provider: 'codex'})), false);
  assert.equal(runtime.eligible({role: {id: 'ordubashy'}}, terminalSession()), false);
  assert.equal(runtime.eligible({role: {id: 'learning-applier'}}, terminalSession()), false);
  assert.equal(runtime.eligible({role: {id: 'ordubashy'}}, managed()), true, 'an agent already managed can always go back');
});

// ---- slash commands
test('an unavailable slash command is refused for a managed agent and nothing else', () => {
  const cost = {name: '/cost', where: 'unavailable'}, compact = {name: '/compact', where: 'chat'}, model = {name: '/model', where: 'terminal'};
  assert.equal(slashCommands.managedRefusal(managed(), cost, 'Ada'), '/cost isn’t available when Qorqbot runs Ada. Switch Ada to Terminal to use it.');
  assert.equal(slashCommands.refusal(managed(), cost, 'Ada'), slashCommands.managedRefusal(managed(), cost, 'Ada'));
  assert.equal(slashCommands.refusal(managed(), compact, 'Ada'), null);
  assert.equal(slashCommands.refusal(managed(), null, 'Ada'), null);
  assert.equal(slashCommands.refusal(terminalSession(), cost, 'Ada'), null);
  assert.equal(slashCommands.refusal({...terminalSession(), execution: 'terminal'}, cost, 'Ada'), null);
  assert.equal(slashCommands.refusal(terminalSession(), model, 'Ada'), null);
  // The Codex shared-server refusal is unchanged.
  const codex = {provider: 'codex', transport: 'codex_queue'};
  assert.equal(slashCommands.refusal(codex, model, 'Ada'), slashCommands.sharedServerRefusal(codex, model, 'Ada'));
  assert.match(slashCommands.refusal(codex, model, 'Ada'), /runs only in Ada’s Terminal/);
  assert.equal(slashCommands.opensTerminal(cost, '/cost', 'claude'), false);
});

// ---- the Chat code in js/global-chat.js, cut out of the source and run against fakes
const source = read('js/global-chat.js');
const between = (from, to) => {
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `global-chat.js no longer contains ${from}`);
  return source.slice(start, end);
};
function statusParts({session, entries = 0, inputs = 0, viewing = true, options = {openTerminal() {}}}) {
  const body = between('  function sessionSummary() {', '  // ---- slash commands') + between('  function startHint() {', '  // ---- managed runtime');
  return new Function('session', 'label', 'agentRuntime', 'viewingLive', 'entries', 'shownInputs', 'hasLocal', 'options',
    `${body}; return {sessionSummary, startHint, emptyState};`)(session, 'Ada', runtime, () => viewing, {size: entries}, {length: inputs}, false, options);
}

test('terminal agents keep their exact wording; a session with no `execution` reads the same as one marked terminal', () => {
  const offline = {provider: 'claude', state: 'offline', reason: 'process_gone', can_send: false, binding: null, launch_commands: ['claude']};
  for (const execution of [{}, {execution: 'terminal'}]) {
    const chat = statusParts({session: {...offline, ...execution}});
    assert.deepEqual(chat.sessionSummary(), {key: 'offline', line: 'Open the terminal (>_) and run claude to start Ada.'});
    assert.equal(chat.startHint(), 'Open the terminal (>_) and run claude to start Ada');
    assert.deepEqual(chat.emptyState(), {text: 'Open the terminal (>_) and run claude to start Ada. Its conversation will appear here.', action: {key: 'terminal', label: 'Open terminal'}});
    const bare = statusParts({session: {...offline, launch_commands: [], ...execution}});
    assert.equal(bare.startHint(), 'Start or resume Ada in Terminal');
    assert.equal(statusParts({session: terminalSession(execution)}).sessionSummary().key, 'live');
    assert.equal(statusParts({session: terminalSession({working: true, ...execution})}).sessionSummary().key, 'working');
    assert.equal(statusParts({session: terminalSession(execution), viewing: false}).sessionSummary().key, 'starting');
    assert.deepEqual(statusParts({session: terminalSession(execution)}).emptyState(), {text: 'Your agent conversation appears here.', action: null});
  }
});

test('a closed managed agent’s summary, empty state and start hint never send the owner to a terminal', () => {
  const chat = statusParts({session: closedSession()});
  assert.deepEqual(chat.sessionSummary(), {key: 'closed', line: 'Sending a message starts Ada.'});
  assert.equal(chat.startHint(), 'Send a message to start Ada');
  assert.deepEqual(chat.emptyState(), {text: 'Send a message to start Ada. Its conversation will appear here.', action: null});
  assert.deepEqual(statusParts({session: closedSession(), entries: 3}).emptyState(), {text: '', action: null});
  assert.deepEqual(statusParts({session: managed()}).emptyState(), {text: 'Your agent conversation appears here.', action: null});
  assert.equal(statusParts({session: managed({phase: 'working'})}).sessionSummary().key, 'working');
});

test('a closed managed composer is enabled and its message goes without a binding pin; a terminal one still pins', async () => {
  const canSendSource = between('  const canSend = () =>', '  const selectedFiles');
  const admitSource = between('  let freshChoice = null;', '  function takeInput');
  const make = session => (...args) => new Function('session', 'viewingLive', 'pairedHost', 'finished', 'agentRuntime', 'base', 'post', 'coreData',
    'managedBar', 'live', 'view', 'submit',
    `${canSendSource}${admitSource}; return {canSend, admit};`)(...args, null, () => true, {getDraft: () => ''}, async () => {});
  const posts = [];
  const post = async (url, body) => { posts.push([url, body]); return {data: {}}; };
  const closed = make()(closedSession(), () => false, null, false, runtime, '/root/agents/a/chat', post, r => r.data);
  assert.equal(closed.canSend(), true);
  await closed.admit('hello', 'key-1', null);
  assert.deepEqual(posts[0], ['/root/agents/a/chat/messages', {text: 'hello', idempotency_key: 'key-1'}]);
  assert.equal('expected_binding' in posts[0][1], false);
  await closed.admit('hello', 'key-2', 'binding-1');
  assert.deepEqual(posts[1][1], {text: 'hello', idempotency_key: 'key-2', expected_binding: 'binding-1'});
  const cases = [[closedSession(), false, true], [{...closedSession(), can_send: false}, false, false], [terminalSession(), true, true],
    [terminalSession(), false, false], [terminalSession({state: 'offline', binding: null, can_send: false}), false, false], [managed(), true, true], [managed(), false, false]];
  for (const [session, viewing, want] of cases) assert.equal(make()(session, () => viewing, null, false, runtime, '', post, r => r).canSend(), want, JSON.stringify([session.execution, viewing]));
  assert.equal(make()(closedSession(), () => false, {}, false, runtime, '', post, r => r).canSend(), false, 'a takeover blocks it');
});

// ---- the pieces of global-chat.js that carry a refusal: the retry key, the offer of a new conversation, the flag it sends
function admission({session, responses, draft = 'the draft'}) {
  const source = between('  let freshChoice = null;', '  function takeInput');
  const posts = [], offers = [], submitted = [];
  const managedBar = {offer: (what, choose) => offers.push({what, choose}), clearOffer: () => offers.push('cleared')};
  const post = async (url, body) => { posts.push(body); const next = responses.shift(); if (next instanceof Error) throw next; return {data: next ?? {}}; };
  const coreData = r => r.data;
  const state = {submit: async () => {}};
  const api = new Function('session', 'agentRuntime', 'base', 'post', 'coreData', 'managedBar', 'live', 'view', 'submit',
    `${source}; return {admit, startFresh};`)(session, runtime, '/chat', post, coreData, managedBar, () => true, {getDraft: () => draft},
    async text => { submitted.push(text); await state.submit(text); });
  return {...api, posts, offers, submitted, state};
}
const refusal = code => Object.assign(new Error(code), {code});

test('a conversation that can’t be continued offers a new one, which resends the draft with new_conversation and never otherwise', async () => {
  const chat = admission({session: closedSession(), responses: [refusal('resume_unavailable'), {input: {}}, {input: {}}]});
  await assert.rejects(chat.admit('hello', 'key-1', null), problem => problem.code === 'resume_unavailable' && problem.offered === true && !problem.keepAttempt);
  assert.equal('new_conversation' in chat.posts[0], false, 'the first request never asks for it');
  const [{what, choose}] = chat.offers;
  assert.equal(what.label, 'Start a new conversation');
  assert.match(what.text, /file is missing/);
  assert.match(what.text, /Nothing was sent/);
  choose(); await flush();
  assert.deepEqual(chat.submitted, ['the draft']);
  // the resend is the next request: a fresh key, flagged; the flag ends with it
  chat.state.submit = async text => { await chat.admit(text, 'key-2', null); };
  choose(); await flush();
  assert.deepEqual(chat.posts[1], {text: 'the draft', idempotency_key: 'key-2', new_conversation: true});
  await chat.admit('later', 'key-3', null);
  assert.equal('new_conversation' in chat.posts[2], false);
});

test('the new-conversation flag follows its request through a retry with the same key, and never rides on another or a pinned one', async () => {
  const chat = admission({session: closedSession(), responses: [refusal('open_timeout'), {input: {}}, {input: {}}, {input: {}}]});
  chat.state.submit = async text => { await chat.admit(text, 'key-9', null); };
  await assert.rejects(chat.startFresh('hello'), problem => problem.code === 'open_timeout' && problem.keepAttempt === true);
  assert.equal(chat.posts[0].new_conversation, true);
  await chat.admit('hello', 'key-9', null);                       // the retry keeps the key, so it keeps the flag
  assert.equal(chat.posts[1].new_conversation, true);
  await chat.admit('another message', 'key-10', null);            // a different request is a different message
  assert.equal('new_conversation' in chat.posts[2], false);
  const pinned = admission({session: closedSession(), responses: [{input: {}}]});
  pinned.state.submit = async text => { await pinned.admit(text, 'key-11', 'binding-1'); };
  await pinned.startFresh('hello');
  assert.equal('new_conversation' in pinned.posts[0], false, 'a live session is never replaced');
});

test('"not yet" refusals keep the request (same key on the retry); other refusals drop it; a terminal agent is offered nothing', async () => {
  for (const code of ['open_timeout', 'open_busy', 'session_opening', 'host_unverified', 'binding_not_ready']) {
    const chat = admission({session: closedSession(), responses: [refusal(code)]});
    await assert.rejects(chat.admit('hello', 'key-1', null), problem => problem.keepAttempt === true && !problem.offered, code);
    assert.deepEqual(chat.offers, [], code);
  }
  for (const code of ['session_closed', 'cli_unavailable', 'terminal_present', 'validation_error', 'driver_unavailable']) {
    const chat = admission({session: closedSession(), responses: [refusal(code)]});
    await assert.rejects(chat.admit('hello', 'key-1', null), problem => !problem.keepAttempt && !problem.offered, code);
  }
  const terminal = admission({session: terminalSession(), responses: [refusal('resume_unavailable')]});
  await assert.rejects(terminal.admit('hello', 'key-1', 'binding-1'), problem => !problem.offered);
  assert.deepEqual(terminal.offers, []);
});

test('the refusal wording: the offer replaces the banner, the new codes have plain words, and a terminal agent keeps its own', () => {
  const source = between('  const refusalText = problem =>', '  // One admission request.');
  const GLOBAL_CHAT_REFUSAL_TEXT = {};
  const text = session => new Function('session', 'agentRuntime', 'GLOBAL_CHAT_REFUSAL_TEXT', `${source}; return refusalText;`)(session, runtime, GLOBAL_CHAT_REFUSAL_TEXT);
  assert.equal(text(closedSession())({code: 'resume_unavailable', offered: true}), '');
  assert.match(text(closedSession())({code: 'open_busy'}), /Several sessions are starting/);
  assert.match(text(closedSession())({code: 'terminal_present'}), /Terminal is still running/);
  assert.match(text(closedSession())({code: 'session_opening'}), /being opened/);
  assert.equal(text(terminalSession())({code: 'open_busy'}), "Couldn't send. Try again");
  const refusals = between('const GLOBAL_CHAT_REFUSALS = new Set', 'const GLOBAL_CHAT_REFUSAL_TEXT');
  const known = new Function(`${refusals}; return GLOBAL_CHAT_REFUSALS;`)();
  for (const code of ['resume_unavailable', 'open_busy', 'session_opening', 'terminal_present']) assert.equal(known.has(code), true, code);
});

test('the bar offers one action after a refusal, runs it once, and drops it when a session is open', () => {
  const ui = bar(); ui.update(closedSession());
  const notice = ui.el.querySelector('.mchat-notice');
  assert.equal(notice.hidden, true);
  const chosen = [];
  ui.offer({text: 'The earlier conversation can’t be continued because its file is missing. Nothing was sent.', label: 'Start a new conversation'}, () => chosen.push(1));
  assert.equal(notice.hidden, false);
  assert.equal(ui.el.hidden, false);
  assert.match(notice.querySelector('.mchat-notice-text').textContent, /file is missing/);
  assert.equal(notice.querySelector('.mchat-notice-action').textContent, 'Start a new conversation');
  ui.update(closedSession());
  assert.equal(notice.hidden, false, 'it stays while the session is closed');
  click(notice.querySelector('.mchat-notice-action'));
  assert.deepEqual(chosen, [1]);
  assert.equal(notice.hidden, true);
  click(notice.querySelector('.mchat-notice-action'));
  assert.deepEqual(chosen, [1], 'a second press runs nothing');
  ui.offer({text: 't', label: 'l'}, () => chosen.push(2));
  ui.update(managed());
  assert.equal(notice.hidden, true, 'an open session ends the offer');
  assert.deepEqual(runtime.newConversationOffer(terminalSession(), 'resume_unavailable'), null);
  assert.equal(runtime.newConversationOffer(closedSession(), 'resume_unavailable').label, 'Start a new conversation');
  assert.equal(runtime.newConversationOffer(closedSession(), 'open_timeout'), null);
});

test('a session that ended unexpectedly says why in the server’s plain words; an ordinary close does not', () => {
  const crashed = {...closedSession(), managed: {...closedSession().managed, closed_text: 'Claude Code stopped unexpectedly. Send a message to start the session again.'}};
  assert.equal(runtime.summary(crashed, 'Ada').line, 'Claude Code stopped unexpectedly. Send a message to start the session again.');
  assert.equal(runtime.summary(closedSession(), 'Ada').line, 'Sending a message starts Ada.');
  assert.equal(runtime.managedOf(crashed).closedText.includes('stopped unexpectedly'), true);
});

// ---- the permission card shows what is being allowed
test('a permission card shows the whole command, escaped and scrollable, with the reason and path, and Allow works', async () => {
  const command = 'git clone https://example.com/a.git &&\n  echo "<b>hi</b>" && rm -rf ./build ' + 'x'.repeat(900);
  const ui = bar(); ui.update(managed({phase: 'blocked', pending: [permission({input_summary: 'git clone https://example.com/a.git && echo…', input_text: command, input_truncated: false,
    decision_reason: 'Bash(rm *) needs approval <i>', blocked_path: '/home/me/project/build'})]}));
  const [card] = cardsOf(ui), pre = card.querySelector('.mchat-card-input');
  assert.equal(pre.textContent, command, 'all of it, with its line breaks, as text');
  assert.equal(pre.getAttribute('tabindex'), '0', 'scrollable with the keyboard');
  assert.equal(card.querySelector('.mchat-card-reason').textContent, 'Bash(rm *) needs approval <i>');
  assert.equal(card.querySelector('.mchat-card-path code').textContent, '/home/me/project/build');
  assert.equal(card.querySelector('.mchat-card-note'), null);
  const allow = card.querySelector('[data-answer="allow"]');
  assert.equal(allow.disabled, false);
  assert.equal(card.querySelectorAll('b, i').length, 0, 'no element is made from the server’s text');
  click(allow); await flush();
  assert.equal(ui.sent[0][1].decision, 'allow');
});

test('when any of the input was cut Allow is disabled with a plain reason and cannot be pressed; Deny still works', async () => {
  const ui = bar(); ui.update(managed({phase: 'blocked', pending: [permission({input_text: 'rm -rf /tmp/a…', input_truncated: true})]}));
  const [card] = cardsOf(ui), allow = card.querySelector('[data-answer="allow"]'), deny = card.querySelector('[data-answer="deny"]');
  assert.equal(allow.disabled, true);
  assert.match(card.querySelector('.mchat-card-note').textContent, /too long to show in full, so it can’t be allowed from here\. You can deny it\./);
  assert.equal(allow.getAttribute('aria-describedby'), card.querySelector('.mchat-card-note').getAttribute('id'));
  click(allow); await flush();
  assert.deepEqual(ui.sent, []);
  click(deny); await flush();
  assert.equal(ui.sent[0][1].decision, 'deny');
  // a failed answer unlocks the buttons again, but never the one that was turned off for good
  const failing = bar(); failing.state.fail = Object.assign(new Error('no'), {code: 'internal'});
  failing.update(managed({pending: [permission({input_text: 'x', input_truncated: true})]}));
  const [again] = cardsOf(failing);
  click(again.querySelector('[data-answer="deny"]')); await flush();
  assert.equal(again.querySelector('[data-answer="deny"]').disabled, false);
  assert.equal(again.querySelector('[data-answer="allow"]').disabled, true);
  // an older server that only sends a clipped one-line summary cannot show everything either
  const old = bar(); old.update(managed({pending: [permission({input_summary: 'a long command that was clipped…'})]}));
  assert.equal(cardsOf(old)[0].querySelector('[data-answer="allow"]').disabled, true);
  const plain = bar(); plain.update(managed({pending: [permission({input_summary: 'npm test'})]}));
  assert.equal(cardsOf(plain)[0].querySelector('[data-answer="allow"]').disabled, false);
  assert.equal(cardsOf(plain)[0].querySelector('.mchat-card-input').textContent, 'npm test');
});

test('questions are answered the way the server checks them: every question, a single choice or typed text, several choices joined', async () => {
  const asked = [{question: 'Which database?', header: 'DB', multiSelect: false, options: [{label: 'Postgres', description: 'x'}, {label: 'SQLite', description: 'y'}]},
    {question: 'Which checks?', header: 'Checks', multiSelect: true, options: [{label: 'Lint', description: ''}, {label: 'Types', description: ''}]}];
  const ui = bar(); ui.update(managed({pending: [question({questions: asked})]}));
  const [card] = cardsOf(ui);
  assert.deepEqual(card.querySelectorAll('[data-q]').map(i => i.getAttribute('type')), ['radio', 'radio', 'checkbox', 'checkbox']);
  const inputs = card.querySelectorAll('[data-q]');
  inputs[0].checked = true; inputs[1].checked = true;     // a single-choice question sends one label
  inputs[2].checked = true; inputs[3].checked = true;     // a multi-select sends them all, in order
  click(card.querySelector('[data-answer="submit"]')); await flush();
  assert.deepEqual(ui.sent[0][1].decision, {answers: {'Which database?': 'Postgres', 'Which checks?': 'Lint, Types'}});
  const typed = bar(); typed.update(managed({pending: [question({questions: asked})]}));
  const [other] = cardsOf(typed);
  other.querySelectorAll('[data-q]')[1].checked = true;
  other.querySelectorAll('[data-q-other]')[0].value = 'Something else entirely';
  other.querySelectorAll('[data-q]')[2].checked = true;
  other.querySelectorAll('[data-q-other]')[1].value = 'Format';
  click(other.querySelector('[data-answer="submit"]')); await flush();
  assert.deepEqual(typed.sent[0][1].decision, {answers: {'Which database?': 'Something else entirely', 'Which checks?': 'Lint, Format'}},
    'the typed text is the answer to a single-choice question; it is added to the labels of a multi-select');
  assert.equal(other.querySelectorAll('[data-q-other]')[0].getAttribute('maxlength'), '2000');
});

test('the new refusal words and which refusals are only "not yet"', () => {
  const closed = closedSession();
  assert.match(runtime.refusalText(closed, 'resume_unavailable'), /file is missing/);
  assert.match(runtime.refusalText(closed, 'open_busy'), /Several sessions are starting/);
  assert.match(runtime.refusalText(closed, 'terminal_present'), /Close it/);
  assert.equal(runtime.refusalText(terminalSession(), 'open_busy'), null);
  for (const code of ['open_timeout', 'open_busy', 'session_opening', 'host_unverified', 'binding_not_ready']) assert.equal(runtime.isRetryable(code), true, code);
  for (const code of ['resume_unavailable', 'resume_failed', 'session_closed', 'cli_unavailable', 'terminal_present', 'validation_error']) assert.equal(runtime.isRetryable(code), false, code);
});

test('the real submit sends a closed managed agent’s message with no pin, and a plain message to a terminal agent with its pin', async () => {
  const submitSource = between('  async function submit(text) {', '  // A definitely undelivered input is resent');
  const run = async (session, binding) => {
    const admitted = [], banners = [];
    const names = ['composerScope', 'selectedFiles', 'canSend', 'session', 'slashCommands', 'commandCatalog', 'label', 'showBanner', 'GLOBAL_CHAT_SENDS', 'GLOBAL_CHAT_INFLIGHT',
      'GLOBAL_CHAT_DRAFTS', 'GLOBAL_CHAT_FILES', 'GLOBAL_CHAT_MAX', 'GLOBAL_CHAT_REFUSALS', 'liveBinding', 'newRequestId', 'newestPosition', 'api', 'base', 'coreData', 'admit',
      'globalChatAccept', 'options', 'toast', 'live', 'view', 'takeInput', 'load', 'paintSession', 'refusalText'];
    const view = {draft: 'hi', getDraft: () => view.draft, setDraft: v => { view.draft = v; }, scrollToBottom() {}};
    const values = ['scope', () => [], () => true, session, slashCommands, () => [{name: '/cost', where: 'unavailable'}], 'Ada', t => banners.push(t),
      new Map(), new Map(), new Map(), new Map(), 17000, new Set(), () => binding, () => 'req-1', () => 0, async () => ({}), '/x', r => r,
      async (t, k, pin) => { admitted.push([t, k, pin]); return {input: {id: 'i1', state: 'waiting', created_at: '2026-11-04T10:00:00Z'}}; },
      () => {}, {}, () => {}, () => true, view, () => {}, () => {}, () => {}, p => p.code];
    const submit = new Function(...names, `${submitSource}; return submit;`)(...values);
    await submit('hi'); await submit('/cost');
    return {admitted, banners};
  };
  const closed = await run(closedSession(), null);
  assert.deepEqual(closed.admitted, [['hi', 'req-1', null]]);
  assert.deepEqual(closed.banners.filter(Boolean), ['/cost isn’t available when Qorqbot runs Ada. Switch Ada to Terminal to use it.']);
  const terminal = await run(terminalSession(), BINDING);
  assert.deepEqual(terminal.admitted, [['hi', 'req-1', 'binding-1'], ['/cost', 'req-1', 'binding-1']]);
  assert.deepEqual(terminal.banners.filter(Boolean), []);
});

// syncManaged against the fake DOM: what the Chat shell adds, and what it must not add to a terminal agent.
function shell({agent, session, openTerminal = () => {}}) {
  freshPage();
  const root = doc.createElement('div'), chatView = doc.createElement('div'), sessionLine = doc.createElement('div');
  const sw = doc.createElement('div'); sw.className = 'gchat-session-switch'; sw.innerHTML = '<div class="gchat-view-toggle"><button>Chat</button><button>Terminal</button></div>';
  chatView.append(sessionLine); root.append(sw, chatView); doc.body.append(root);
  const reported = [], cleanups = [], banners = [], loads = [];
  const sync = new Function('agentRuntime', 'agent', 'label', 'sessionLine', 'liveBinding', 'paintSession', 'load', 'showBanner', 'cleanups', '$q', 'root', 'options', 'coreData', 'post', 'base', 'state',
    `let runtimeToggle = null, managedBar = null, reportedExecution = null; let session = state.session;
     ${between('  const runtimeSend =', '  // One repaint: the native status line')}
     return {syncManaged, bar: () => managedBar, toggle: () => runtimeToggle, set: value => { session = value; }};`)(
    runtime, agent, 'Ada', sessionLine, () => state.session?.binding, () => {}, () => loads.push(1), t => banners.push(t), cleanups, sel => root.querySelector(sel), root,
    {onExecution: mode => reported.push(mode)}, r => r, async () => ({}), '/chat', {session});
  return {root, sw, chatView, sessionLine, sync, reported, cleanups, banners, loads, viewSwitch: () => root.querySelector('.gchat-view-toggle')};
}
const WORKER = {agent_id: 'a1', role: {id: 'global-worker'}}, BOSS = {agent_id: 'o1', role: {id: 'ordubashy'}};

test('a terminal agent that cannot be switched gets no managed element and its Chat/Terminal switch stays', () => {
  const s = shell({agent: BOSS, session: terminalSession()});
  const before = s.root.querySelectorAll('div').length;
  s.sync.syncManaged();
  assert.equal(s.sync.bar(), null); assert.equal(s.sync.toggle(), null);
  assert.equal(s.root.querySelectorAll('div').length, before);
  assert.equal(s.viewSwitch().hidden, false);
  assert.equal(s.root.dataset.execution, undefined);
  assert.deepEqual(s.reported, [false]);
  s.sync.syncManaged();
  assert.deepEqual(s.reported, [false], 'reported once');
});

test('a switchable terminal worker shows only the switch; the Terminal view is still there', () => {
  const s = shell({agent: WORKER, session: terminalSession()});
  s.sync.syncManaged();
  assert.equal(s.sync.bar(), null);
  assert.ok(s.sync.toggle());
  assert.equal(s.sw.firstElementChild, s.sync.toggle().el, 'the switch sits before the Chat/Terminal buttons');
  assert.equal(s.sync.toggle().el.hidden, false);
  assert.equal(s.viewSwitch().hidden, false);
});

test('a managed agent gets the Stop block above the session line, loses the Terminal switch, and the host is told', () => {
  const s = shell({agent: WORKER, session: managed({phase: 'working', activity: 'Editing', can_stop: true})});
  s.sync.syncManaged();
  const bar = s.sync.bar();
  assert.ok(bar);
  assert.equal(s.chatView.children[0], bar.el);
  assert.equal(s.chatView.children[1], s.sessionLine);
  assert.equal(bar.el.querySelector('.mchat-stop').hidden, false);
  assert.equal(bar.el.querySelector('.mchat-activity-text').textContent, 'Editing');
  assert.equal(s.viewSwitch().hidden, true, 'no Terminal for a managed agent');
  assert.equal(s.root.dataset.execution, 'managed');
  assert.deepEqual(s.reported, [true]);
  // Back to terminal: the block hides and the switch returns.
  s.sync.set(terminalSession()); s.sync.syncManaged();
  assert.equal(bar.el.hidden, true);
  assert.equal(s.viewSwitch().hidden, false);
  assert.deepEqual(s.reported, [true, false]);
  assert.equal(s.cleanups.length, 1);
});

test('the event stream’s managed summary is shown at once and only for a managed session', () => {
  const fn = between('  function showManagedEvent(data) {', '  function connect()');
  const run = (session, data) => {
    let painted = 0, current = session;
    new Function('agentRuntime', 'paintSession', 'get', 'set', `let session = get(); ${fn}; showManagedEvent(${JSON.stringify(data)}); set(session);`)(runtime, () => painted++, () => current, v => { current = v; });
    return {session: current, painted};
  };
  const live = managed({phase: 'idle', activity: ''});
  const patched = run(live, {managed: {phase: 'working', activity: 'Reading'}});
  assert.equal(patched.painted, 1);
  assert.equal(patched.session.managed.phase, 'working');
  assert.equal(patched.session.managed.can_stop, true, 'fields the summary leaves out are kept');
  assert.equal(run(terminalSession(), {managed: {phase: 'working'}}).painted, 0);
  assert.equal(run(live, {revision: 3}).painted, 0);
  assert.equal(run(live, {managed: {open: false}}).painted, 0, 'a session that opened or closed is read in full');
});

test('mountAgentView: a managed agent has no Terminal; a terminal agent opens it as before', () => {
  const body = between('let globalAgentViewActive = null;', 'function mountGlobalChat(');
  freshPage();
  const chats = [], terminals = [], closed = [];
  const build = () => new Function('document', 'esc', 'mountGlobalChat', 'mountTerminal', 'globalChatViewToggle', 'globalChatOpenFocusReference', `${body}; return mountAgentView;`)(
    doc, global.esc, (host, agent, options) => { const chat = {options, disposed: false, dispose() { this.disposed = true; }}; chats.push(chat); return chat; },
    (key, host) => { terminals.push(key); return () => closed.push(key); }, () => '<button type="button" data-chat-return></button>', () => true);
  const views = [];
  const mount = () => { const pane = doc.createElement('main'); doc.body.append(pane); const view = build()(pane, {agent_id: 'a1', name: 'Ada'}, {terminal: {key: 'a1', base: '/t'}, isCurrent: () => true}); views.push(view); return view; };
  try {

  const managedView = mount();
  assert.equal(chats.length, 1);
  managedView.showTerminal();
  assert.equal(terminals.length, 1, 'terminal agent: Terminal opens');
  chats[0].options.onExecution(true);
  assert.deepEqual(closed, ['a1'], 'the Terminal viewer is released when the agent turns managed');
  managedView.showTerminal(); managedView.showTerminal(true);
  assert.equal(terminals.length, 1, 'managed agent: Terminal never opens');
  managedView.dispose();

  const plain = mount();
  chats[1].options.onExecution(false);
  plain.showTerminal();
  assert.equal(terminals.length, 2);
  plain.showChat();
  plain.showTerminal();
  assert.equal(terminals.length, 2, 'a warm Terminal is kept, not rebuilt');
  } finally { views.forEach(view => view.dispose()); }
});
