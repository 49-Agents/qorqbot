'use strict';
// node --test dashboard/tests/chat-managed.test.js
// Chat for an agent Qorqbot runs itself (js/agent-runtime.js and its use in js/global-chat.js and js/slash-commands.js). Every agent is
// run that way: the session wording for each state (asleep, opening, working, ready, off, cannot start), Stop, the activity line,
// the question cards, the refusal words and the unavailable slash-command refusal.
// The bar and shell run against the small fake DOM below; global-chat.js functions are cut out of the source and run
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
const managed = (over = {}, session = {}) => ({provider: 'claude', state: 'live', can_send: true, binding: BINDING, working: false,
  managed: {open: true, phase: 'idle', activity: '', turn_started_at: null, can_stop: true, pending: [], ...over}, ...session});
const closedSession = (over = {}) => ({provider: 'claude', state: 'offline', reason: 'session_closed', can_send: true, binding: null,
  managed: {open: false, phase: 'idle', activity: '', turn_started_at: null, can_stop: false, pending: [], ...over}});
const offSession = () => closedSession({off: true});
// A leftover permission request: the server never sends one now, and the page draws nothing for it.
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

test('every session is managed: `execution` is neither needed nor read, and no session is a terminal one', () => {
  for (const session of [managed(), {...managed(), execution: 'managed'}, {...managed(), execution: 'terminal'}, {...managed(), execution: undefined}]) {
    assert.equal(runtime.managedOf(session).open, true);
    assert.equal(runtime.summary(session, 'Ada').key, 'live');
    assert.equal(runtime.startHint(session, 'Ada'), 'Send a message to start Ada');
  }
  assert.equal(runtime.managedOf(null), null);
  assert.equal(runtime.summary(null, 'Ada'), null);
  // a session that arrives with no `managed` part at all reads as open while it is live, and closed once the server says so
  assert.equal(runtime.managedOf({state: 'live', can_send: true}).open, true);
  assert.equal(runtime.managedOf({state: 'offline', reason: 'session_closed'}).open, false);
  assert.equal(runtime.closed({state: 'offline', reason: 'session_closed', can_send: true}), true);
  const ui = bar();
  ui.update(managed({phase: 'working', can_stop: true}, {execution: 'terminal'}));
  assert.equal(ui.el.hidden, false);
  assert.equal(ui.el.querySelector('.mchat-stop').hidden, false);
});

test('an asleep agent that was never started invites a message; with history it says it is asleep and wakes by itself', () => {
  const session = closedSession();
  assert.deepEqual(runtime.summary(session, 'Ada'), {key: 'closed', line: ''});
  assert.deepEqual(runtime.summary(session, 'Ada', true, {history: true}), {key: 'closed', line: 'Ada is asleep. It wakes when there is something to do.'});
  assert.equal(runtime.startHint(session, 'Ada'), 'Send a message to start Ada');
  assert.equal(runtime.emptyText(session, 'Ada'), 'Send a message to start Ada. Its conversation will appear here.');
  assert.equal(runtime.placeholder(session, 'Ada', false), 'Message Ada to start it…');
  assert.equal(runtime.placeholder(session, 'Ada', true), 'Message Ada…');
  assert.equal(runtime.canOpen(session), true);
  for (const text of [runtime.summary(session, 'Ada', true, {history: true}).line, runtime.startHint(session, 'Ada'), runtime.emptyText(session, 'Ada'),
    runtime.placeholder(session, 'Ada', false), runtime.refusalText('session_closed')]) {
    assert.doesNotMatch(text, /terminal|tmux|\bCLI\b|session id|claude|>_|!|—|–/i);
  }
  // Closed by the server's word or by the managed summary alone.
  assert.equal(runtime.closed({...closedSession(), reason: undefined}), true);
  assert.equal(runtime.closed({...closedSession(), managed: undefined, reason: 'session_closed'}), true);
  assert.equal(runtime.canOpen({...closedSession(), can_send: false}), false);
  assert.equal(runtime.canOpen({...closedSession(), binding: BINDING}), false);
});

test('an agent the owner turned off says it stays off until a message is sent, and a message is still welcome', () => {
  const session = offSession();
  assert.equal(runtime.managedOf(session).off, true);
  assert.equal(runtime.managedOf(session).open, false);
  assert.deepEqual(runtime.summary(session, 'Ada'), {key: 'off', line: 'Ada is off. It stays off until you send a message.'});
  assert.deepEqual(runtime.summary(session, 'Ada', true, {history: true}), {key: 'off', line: 'Ada is off. It stays off until you send a message.'});
  assert.equal(runtime.startHint(session, 'Ada'), 'Send a message to turn Ada on');
  assert.equal(runtime.emptyText(session, 'Ada'), 'Ada is off. Send a message to turn it on.');
  assert.equal(runtime.placeholder(session, 'Ada', true), 'Message Ada to turn it on…');
  assert.equal(runtime.placeholder(session, 'Ada', false), 'Message Ada to turn it on…');
  assert.equal(runtime.canOpen(session), true, 'the composer is usable: the message turns it on');
  // off wins over a stale open flag, and over the server's closed text; a message on its way reads as starting
  assert.equal(runtime.managedOf({...managed(), managed: {open: true, off: true}}).open, false);
  assert.equal(runtime.summary(closedSession({off: true, closed_text: 'x'}), 'Ada').key, 'off');
  assert.deepEqual(runtime.summary(session, 'Ada', true, {opening: true}), {key: 'starting', line: 'Starting Ada…'});
  for (const text of [runtime.summary(session, 'Ada').line, runtime.startHint(session, 'Ada'), runtime.emptyText(session, 'Ada'), runtime.placeholder(session, 'Ada', true)]) {
    assert.doesNotMatch(text, /terminal|tmux|\bCLI\b|session id|!|—|–/i);
  }
});

test('an open session reads starting, ready, working or disabled, in short plain words', () => {
  assert.deepEqual(runtime.summary(managed({phase: 'starting'}), 'Ada'), {key: 'starting', line: 'Starting Ada…'});
  assert.deepEqual(runtime.summary(managed({phase: 'idle'}), 'Ada'), {key: 'live', line: ''});
  assert.equal(runtime.summary(managed({phase: 'working'}), 'Ada').key, 'working');
  assert.equal(runtime.summary(managed({phase: 'working'}), 'Ada').line, '', 'the activity line says what it does');
  assert.equal(runtime.summary(managed({phase: 'blocked'}), 'Ada').key, 'working');
  assert.equal(runtime.summary(managed({phase: ''}, {working: true}), 'Ada').key, 'working');
  assert.equal(runtime.summary(managed(), 'Ada', false).key, 'starting');
  assert.equal(runtime.summary(managed({}, {reason: 'binding_not_ready'}), 'Ada').key, 'starting');
  assert.equal(runtime.summary(managed({}, {reason: 'capture_disabled'}), 'Ada').key, 'disabled');
  assert.equal(runtime.summary(managed({}, {state: 'offline', can_send: false}), 'Ada').key, 'offline');
  assert.equal(runtime.summary(managed({}, {state: 'offline', can_send: false}), 'Ada').line, 'Ada isn’t available right now.');
  // a message that starts the session is on its way
  assert.deepEqual(runtime.summary(closedSession(), 'Ada', true, {opening: true, history: true}), {key: 'starting', line: 'Starting Ada…'});
  assert.equal(runtime.placeholder(managed(), 'Ada', true), 'Message Ada…');
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

test('the activity line shows what the agent is doing as text, and waits for the owner when a question is open', () => {
  const ui = bar(), text = () => ui.el.querySelector('.mchat-activity-text').textContent, line = () => ui.el.querySelector('.mchat-activity');
  ui.update(managed({phase: 'working', activity: 'Reading src/app.py'}));
  assert.equal(line().hidden, false);
  assert.equal(text(), 'Reading src/app.py');
  ui.update(managed({phase: 'working', activity: ''}));
  assert.equal(text(), 'Working');
  ui.update(managed({phase: 'blocked', activity: 'Asking', pending: [question()]}));
  assert.equal(text(), 'Waiting for your answer');
  ui.update(managed({phase: 'idle', activity: 'Reading src/app.py'}));
  assert.equal(line().hidden, true);
  assert.equal(runtime.activity(managed({phase: 'working', activity: 'x'.repeat(500)})).text.length, 160);
});

test('the block above the session line takes room only while it has something to show', () => {
  const ui = bar();
  ui.update(managed({phase: 'idle'})); assert.equal(ui.el.hidden, true);
  ui.update(offSession()); assert.equal(ui.el.hidden, true);
  ui.update(managed({phase: 'working', activity: 'Reading'})); assert.equal(ui.el.hidden, false);
  ui.update(managed({phase: 'blocked', pending: [question()]})); assert.equal(ui.el.hidden, false);
  ui.update(managed({phase: 'idle'})); assert.equal(ui.el.hidden, true);
  ui.update(closedSession()); ui.offer({text: 't', label: 'l'}, () => {}); assert.equal(ui.el.hidden, false);
  ui.clearOffer(); assert.equal(ui.el.hidden, true);
  ui.update(null); assert.equal(ui.el.hidden, true);
});

test('no permission card is ever drawn: a permission request in the payload is ignored, question cards stay', () => {
  const ui = bar();
  ui.update(managed({phase: 'blocked', pending: [permission(), question()]}));
  assert.deepEqual(cardsOf(ui).map(c => c.dataset.requestId), ['req-2']);
  assert.equal(ui.el.querySelector('[data-answer="allow"]'), null);
  assert.equal(ui.el.querySelector('[data-answer="deny"]'), null);
  assert.deepEqual(runtime.managedOf(managed({pending: [permission()]})).pending, []);
  assert.equal(runtime.activity(managed({phase: 'blocked', pending: [permission()]})).text, 'Working', 'nothing to wait for');
});

test('activity, question and option text from the server is escaped: no element is ever created from it', () => {
  const ui = bar();
  ui.update(managed({phase: 'working', activity: PAYLOAD}));
  for (const tag of ['img', 'script']) assert.equal(ui.el.querySelector(tag), null, tag);
  assert.ok(ui.el.querySelector('.mchat-activity-text').textContent.includes('<img src=x'));
  ui.update(managed({phase: 'working', activity: PAYLOAD, pending: [
    question({request_id: 'req-"><script>alert(3)</script>', questions: [{question: PAYLOAD, header: PAYLOAD, options: [{label: PAYLOAD, description: PAYLOAD}]}]})]}));
  for (const tag of ['img', 'script']) assert.equal(ui.el.querySelector(tag), null, tag);
  assert.ok(ui.el.querySelector('.mchat-question').textContent.includes('<img src=x'));
  assert.equal(cardsOf(ui)[0].dataset.requestId, 'req-"><script>alert(3)</script>');
  assert.doesNotMatch(runtime.cardHtml(question({questions: [{question: PAYLOAD, options: [{label: PAYLOAD, description: PAYLOAD}]}]}), PAYLOAD), /<img|<script/);
  assert.equal(runtime.escapeHtml(`<a href="x">'&`), '&lt;a href=&quot;x&quot;&gt;&#39;&amp;');
});

test('a question card stays disabled while the answer is in flight and after it is taken, and a failed answer re-enables it with a message', async () => {
  const pick = card => { card.querySelectorAll('[data-q]')[0].checked = true; return card.querySelector('[data-answer="submit"]'); };
  const ui = bar(); let release; ui.state.hold = new Promise(done => { release = done; });
  ui.update(managed({pending: [question()]}));
  const [card] = cardsOf(ui), send = pick(card);
  click(send); click(send);
  assert.equal(send.disabled, true);
  assert.equal(ui.sent.length, 1);
  release(); await flush();
  assert.equal(send.disabled, true, 'sent: waiting for the server to clear the card');
  assert.equal(ui.changes.length, 1);
  // The server still lists it long after: let the owner try again.
  ui.state.time += 9000; ui.update(managed({pending: [question()]}));
  assert.equal(send.disabled, false);

  const failing = bar(); failing.state.fail = Object.assign(new Error('no'), {code: 'internal'});
  failing.update(managed({pending: [question()]}));
  const [again] = cardsOf(failing), yes = pick(again);
  click(yes); await flush();
  assert.equal(yes.disabled, false);
  assert.equal(again.querySelector('.mchat-card-error').hidden, false);
  assert.equal(again.querySelector('.mchat-card-error').textContent, 'Couldn’t send your answer. Try again.');
  failing.state.fail = Object.assign(new Error('gone'), {code: 'not_found'});
  click(yes); await flush();
  assert.equal(again.querySelector('.mchat-card-error').textContent, 'This is no longer waiting for an answer.');
});

test('cards follow the pending list: the same card is kept (with what was typed), new ones appear, answered ones go', () => {
  const ui = bar(); ui.update(managed({pending: [question({request_id: 'req-1'}), question()]}));
  const [first, second] = cardsOf(ui);
  assert.deepEqual(cardsOf(ui).map(c => c.dataset.requestId), ['req-1', 'req-2']);
  second.querySelector('[data-q-other]').value = 'typed';
  ui.update(managed({phase: 'blocked', activity: 'newer', pending: [question({request_id: 'req-1'}), question()]}));
  assert.equal(cardsOf(ui)[0], first); assert.equal(cardsOf(ui)[1], second);
  assert.equal(second.querySelector('[data-q-other]').value, 'typed');
  ui.update(managed({pending: [question(), question({request_id: 'req-3'})]}));
  assert.deepEqual(cardsOf(ui).map(c => c.dataset.requestId), ['req-2', 'req-3']);
  ui.update(managed({pending: []}));
  assert.equal(cardsOf(ui).length, 0);
  assert.equal(ui.el.querySelector('.mchat-cards').hidden, true);
  // A closed session shows no card even if a stale one is listed; an unknown kind or a missing id is not drawn.
  ui.update(managed({pending: [question({kind: 'other'}), question({request_id: ''})]}));
  assert.equal(cardsOf(ui).length, 0);
  ui.update({...closedSession(), managed: {open: false, pending: [question()]}});
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
  const ui = bar(); ui.state.binding = null; ui.update(managed({pending: [question()]}));
  const [card] = cardsOf(ui); card.querySelectorAll('[data-q]')[0].checked = true; click(card.querySelector('[data-answer="submit"]')); await flush();
  assert.deepEqual(ui.sent, []);
  assert.match(card.querySelector('.mchat-card-error').textContent, /Not connected/);
});

// ---- slash commands
test('an unavailable slash command is refused with plain words, and nothing else is', () => {
  const cost = {name: '/cost', where: 'unavailable'}, compact = {name: '/compact', where: 'chat'}, model = {name: '/model', where: 'chat'};
  assert.equal(slashCommands.unavailableRefusal(cost, 'Ada'), '/cost isn’t available when Qorqbot runs Ada.');
  assert.equal(slashCommands.refusal(managed(), cost, 'Ada'), slashCommands.unavailableRefusal(cost, 'Ada'));
  assert.equal(slashCommands.refusal({...managed(), execution: 'terminal'}, cost, 'Ada'), slashCommands.unavailableRefusal(cost, 'Ada'), '`execution` is not read');
  assert.equal(slashCommands.refusal(managed(), compact, 'Ada'), null);
  assert.equal(slashCommands.refusal(managed(), null, 'Ada'), null);
  assert.equal(slashCommands.refusal(managed(), model, 'Ada'), null);
  assert.equal(slashCommands.opensTerminal, undefined);
  assert.equal(slashCommands.managedRefusal, undefined);
});

// ---- the Chat code in js/global-chat.js, cut out of the source and run against fakes
const source = read('js/global-chat.js');
const between = (from, to) => {
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `global-chat.js no longer contains ${from}`);
  return source.slice(start, end);
};
function statusParts({session, entries = 0, inputs = 0, viewing = true, local = false, sending = false}) {
  const body = between('  const hasHistory = ', '  // ---- slash commands') + between('  function emptyState() {', '  // ---- managed runtime') + between('  const sessionLineText = ', '  // One repaint');
  return new Function('session', 'label', 'agentRuntime', 'viewingLive', 'entries', 'shownInputs', 'hasLocal', 'GLOBAL_CHAT_INFLIGHT', 'composerScope', 'GLOBAL_CHAT_OPENING', 'agentId', 'earlier', 'shownInChat',
    `${body}; return {sessionSummary, emptyState, sessionLineText, hasHistory};`)(session, 'Ada', runtime, () => viewing,
    new Map(Array.from({length: entries}, (_, i) => [`e${i}`, {id: `e${i}`, speaker: 'user'}])), {length: inputs}, local,
    {has: () => sending}, 'scope', {has: () => false}, 'agent-1', [], () => true);
}
// What the owner reads in each state: the session line, the empty state in the middle of the pane, and the composer's placeholder.
function wording(options) {
  const chat = statusParts(options), summary = chat.sessionSummary(), empty = chat.emptyState();
  return {key: summary.key, line: chat.sessionLineText(summary, empty), empty: empty.text,
    placeholder: runtime.placeholder(options.session, 'Ada', chat.hasHistory())};
}

test('the wording of every state, in the Chat code: asleep, never started, opening, working, ready, off', () => {
  // asleep and on, never started: the empty state invites a message
  assert.deepEqual(wording({session: closedSession()}), {key: 'closed', line: '', empty: 'Send a message to start Ada. Its conversation will appear here.', placeholder: 'Message Ada to start it…'});
  // asleep and on, with history: the session line says so
  assert.deepEqual(wording({session: closedSession(), entries: 3}), {key: 'closed', line: 'Ada is asleep. It wakes when there is something to do.', empty: '', placeholder: 'Message Ada…'});
  assert.equal(wording({session: closedSession(), inputs: 1}).line, 'Ada is asleep. It wakes when there is something to do.');
  // off: it stays off until the owner sends a message
  assert.deepEqual(wording({session: offSession()}), {key: 'off', line: '', empty: 'Ada is off. Send a message to turn it on.', placeholder: 'Message Ada to turn it on…'});
  assert.deepEqual(wording({session: offSession(), entries: 3}), {key: 'off', line: 'Ada is off. It stays off until you send a message.', empty: '', placeholder: 'Message Ada to turn it on…'});
  // opening: a message is on its way to a closed session, or the session is starting
  assert.deepEqual(wording({session: closedSession(), sending: true, entries: 3}), {key: 'starting', line: 'Starting Ada…', empty: '', placeholder: 'Message Ada…'});
  assert.deepEqual(wording({session: offSession(), sending: true, entries: 3}).line, 'Starting Ada…');
  assert.deepEqual(wording({session: managed({phase: 'starting'}), entries: 3}).line, 'Starting Ada…');
  assert.deepEqual(wording({session: managed({phase: 'starting'})}), {key: 'starting', line: '', empty: 'Starting Ada…', placeholder: 'Message Ada…'});
  // a send to an open session is not "opening"
  assert.equal(wording({session: managed(), entries: 3, sending: true}).key, 'live');
  // working: the activity line speaks, the session line stays quiet; idle and open: ready
  assert.deepEqual(wording({session: managed({phase: 'working'}), entries: 3}), {key: 'working', line: '', empty: '', placeholder: 'Message Ada…'});
  assert.deepEqual(wording({session: managed({phase: 'idle'}), entries: 3}), {key: 'live', line: '', empty: '', placeholder: 'Message Ada…'});
  assert.equal(wording({session: managed()}).empty, 'Your agent conversation appears here.');
  // the server's own sentence for a session that ended unexpectedly wins over "asleep"
  const crashed = closedSession({closed_text: 'Claude Code stopped unexpectedly. Send a message to start the session again.'});
  assert.equal(wording({session: crashed, entries: 3}).line, 'Claude Code stopped unexpectedly. Send a message to start the session again.');
  assert.equal(statusParts({session: null}).sessionSummary().key, 'loading');
  assert.deepEqual(statusParts({session: null}).emptyState(), {text: '', action: null});
  // nothing a state says names a terminal, tmux, the CLI or a session id, or uses an exclamation mark or a dash
  for (const options of [{session: closedSession()}, {session: closedSession(), entries: 1}, {session: offSession()}, {session: offSession(), entries: 1}, {session: closedSession(), sending: true},
    {session: managed({phase: 'starting'})}, {session: managed()}, {session: managed({phase: 'working'})}]) {
    const w = wording(options);
    for (const text of [w.line, w.empty, w.placeholder]) assert.doesNotMatch(text, /terminal|tmux|\bCLI\b|session id|>_|!|—|–/i, text);
  }
});

test('a closed or off composer is enabled and its message goes without a binding pin; a live one still pins', async () => {
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
  const cases = [[closedSession(), false, true], [offSession(), false, true], [{...closedSession(), can_send: false}, false, false], [{...offSession(), can_send: false}, false, false],
    [managed(), true, true], [managed(), false, false], [managed({}, {state: 'offline', binding: null, can_send: false}), false, false]];
  for (const [session, viewing, want] of cases) assert.equal(make()(session, () => viewing, null, false, runtime, '', post, r => r).canSend(), want, JSON.stringify([session.managed.open, session.managed.off, viewing]));
  assert.equal(make()(closedSession(), () => false, {}, false, runtime, '', post, r => r).canSend(), false, 'a takeover blocks it');
});

// ---- the pieces of global-chat.js that carry a refusal: the retry key, the offer of a new conversation, the flag it sends
function admission({session, responses, draft = 'the draft', noBar = false}) {
  const source = between('  let freshChoice = null;', '  function takeInput');
  const posts = [], offers = [], submitted = [];
  const managedBar = {offer: (what, choose) => offers.push({what, choose}), clearOffer: () => offers.push('cleared')};
  const post = async (url, body) => { posts.push(body); const next = responses.shift(); if (next instanceof Error) throw next; return {data: next ?? {}}; };
  const coreData = r => r.data;
  const state = {submit: async () => {}};
  const api = new Function('session', 'agentRuntime', 'base', 'post', 'coreData', 'managedBar', 'live', 'view', 'submit',
    `${source}; return {admit, startFresh};`)(session, runtime, '/chat', post, coreData, noBar ? null : managedBar, () => true, {getDraft: () => draft},
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

test('"not yet" refusals keep the request (same key on the retry); other refusals drop it; a live session is offered nothing', async () => {
  for (const code of ['open_timeout', 'open_busy', 'session_opening', 'host_unverified', 'binding_not_ready']) {
    const chat = admission({session: closedSession(), responses: [refusal(code)]});
    await assert.rejects(chat.admit('hello', 'key-1', null), problem => problem.keepAttempt === true && !problem.offered, code);
    assert.deepEqual(chat.offers, [], code);
  }
  for (const code of ['session_closed', 'cli_unavailable', 'validation_error', 'driver_unavailable']) {
    const chat = admission({session: closedSession(), responses: [refusal(code)]});
    await assert.rejects(chat.admit('hello', 'key-1', null), problem => !problem.keepAttempt && !problem.offered, code);
  }
  const pinned = admission({session: managed(), responses: [refusal('resume_unavailable')], noBar: true});
  await assert.rejects(pinned.admit('hello', 'key-1', 'binding-1'), problem => !problem.offered);
  assert.deepEqual(pinned.offers, []);
});

test('the refusal wording: the offer replaces the banner, and every cannot-start refusal says what the owner can do', () => {
  const source = between('  const refusalText = problem =>', '  // One admission request.');
  const GLOBAL_CHAT_REFUSAL_TEXT = {capture_disabled: 'Chat unavailable.'};
  const text = new Function('agentRuntime', 'GLOBAL_CHAT_REFUSAL_TEXT', `${source}; return refusalText;`)(runtime, GLOBAL_CHAT_REFUSAL_TEXT);
  assert.equal(text({code: 'resume_unavailable', offered: true}), '');
  assert.match(text({code: 'open_busy'}), /Several sessions are starting/);
  assert.match(text({code: 'session_opening'}), /being opened/);
  assert.equal(text({code: 'capture_disabled'}), 'Chat unavailable.');
  assert.equal(text({code: 'something_new'}), "Couldn't send. Try again");
  const refusals = between('const GLOBAL_CHAT_REFUSALS = new Set', 'const GLOBAL_CHAT_REFUSAL_TEXT');
  const known = new Function(`${refusals}; return GLOBAL_CHAT_REFUSALS;`)();
  for (const code of ['resume_unavailable', 'open_busy', 'session_opening']) assert.equal(known.has(code), true, code);
});

test('cannot start: the owner is pointed to Settings (where Claude Code and Codex are connected), never to a terminal', () => {
  for (const code of ['cli_unavailable', 'driver_unavailable']) {
    assert.match(runtime.refusalText(code), /Settings/, code);
    assert.match(runtime.refusalText(code), /Claude Code or Codex/, code);
  }
  assert.match(runtime.refusalText('driver_unavailable'), /^Couldn’t start\. Check in Settings that Claude Code or Codex is connected, then send again\.$/);
  assert.match(runtime.refusalText('cli_unavailable'), /^This agent’s client \(Claude Code or Codex\) isn’t installed yet\. Download it under Settings > Model\/Provider, then send again\.$/);
  assert.equal(runtime.refusalText('open_timeout'), 'Still starting. Try again in a moment.');
  assert.equal(runtime.refusalText('resume_failed'), 'Couldn’t continue the earlier conversation, so nothing was started.');
  assert.equal(runtime.refusalText('resume_unavailable'), 'The earlier conversation can’t be continued because its file is missing.');
  // the new-conversation offer for the two refusals about the earlier conversation
  assert.deepEqual(runtime.newConversationOffer('resume_failed'), {text: 'Couldn’t continue the earlier conversation, so nothing was started. Nothing was sent.', label: 'Start a new conversation'});
  assert.equal(runtime.newConversationOffer('cli_unavailable'), null);
  // every text the owner can read here, whatever the code
  for (const code of ['session_closed', 'terminal_changed', 'managed_command', 'driver_unavailable', 'cli_unavailable', 'resume_failed',
    'cli_not_signed_in', 'resume_unavailable', 'open_timeout', 'open_busy', 'session_opening', 'host_unverified', 'binding_not_ready']) {
    assert.doesNotMatch(runtime.refusalText(code), /terminal|tmux|\bCLI\b|session id|>_|!|—|–/i, code);
  }
  const chat = between('const GLOBAL_CHAT_REFUSAL_TEXT = {', 'const GLOBAL_CHAT_INPUT_LABEL');
  for (const [, text] of Object.entries(new Function(`${chat}; return GLOBAL_CHAT_REFUSAL_TEXT;`)())) assert.doesNotMatch(text, /terminal|tmux|\bCLI\b|session id|>_|!|—|–/i, text);
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
  assert.equal(runtime.newConversationOffer('resume_unavailable').label, 'Start a new conversation');
  assert.equal(runtime.newConversationOffer('open_timeout'), null);
});

test('a session that ended unexpectedly says why in the server’s plain words; an ordinary close does not', () => {
  const crashed = {...closedSession(), managed: {...closedSession().managed, closed_text: 'Claude Code stopped unexpectedly. Send a message to start the session again.'}};
  assert.equal(runtime.summary(crashed, 'Ada').line, 'Claude Code stopped unexpectedly. Send a message to start the session again.');
  assert.equal(runtime.summary(closedSession(), 'Ada').line, '');
  assert.equal(runtime.managedOf(crashed).closedText.includes('stopped unexpectedly'), true);
});

// ---- the permission card shows what is being allowed
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

test('the refusal words and which refusals are only "not yet"', () => {
  assert.match(runtime.refusalText('resume_unavailable'), /file is missing/);
  assert.match(runtime.refusalText('open_busy'), /Several sessions are starting/);
  assert.equal(runtime.refusalText('something_new'), null);
  for (const code of ['open_timeout', 'open_busy', 'session_opening', 'host_unverified', 'binding_not_ready']) assert.equal(runtime.isRetryable(code), true, code);
  for (const code of ['resume_unavailable', 'resume_failed', 'session_closed', 'cli_unavailable', 'validation_error']) assert.equal(runtime.isRetryable(code), false, code);
});

test('the real submit sends a closed agent’s message with no pin, and a live agent’s with its pin; an unavailable command is refused first', async () => {
  const submitSource = between('  async function submit(text) {', '  // A definitely undelivered input is resent');
  const run = async (session, binding) => {
    const admitted = [], banners = [];
    const names = ['composerScope', 'selectedFiles', 'canSend', 'session', 'slashCommands', 'commandCatalog', 'label', 'showBanner', 'GLOBAL_CHAT_SENDS', 'GLOBAL_CHAT_INFLIGHT',
      'GLOBAL_CHAT_DRAFTS', 'GLOBAL_CHAT_FILES', 'GLOBAL_CHAT_MAX', 'GLOBAL_CHAT_REFUSALS', 'liveBinding', 'newRequestId', 'newestPosition', 'api', 'base', 'coreData', 'admit',
      'globalChatAccept', 'options', 'toast', 'live', 'view', 'takeInput', 'load', 'paintSession', 'refusalText', 'opening'];
    const view = {draft: 'hi', getDraft: () => view.draft, setDraft: v => { view.draft = v; }, scrollToBottom() {}};
    const values = ['scope', () => [], () => true, session, slashCommands, () => [{name: '/cost', where: 'unavailable'}], 'Ada', t => banners.push(t),
      new Map(), new Map(), new Map(), new Map(), 17000, new Set(), () => binding, () => 'req-1', () => 0, async () => ({}), '/x', r => r,
      async (t, k, pin) => { admitted.push([t, k, pin]); return {input: {id: 'i1', state: 'waiting', created_at: '2026-11-04T10:00:00Z'}}; },
      () => {}, {}, () => {}, () => true, view, () => {}, () => {}, () => {}, p => p.code,
      {holds: () => false, busy: () => false, after: () => null, start: () => false, restore() {}, end() {}, pin: async () => null}];
    const submit = new Function(...names, `${submitSource}; return submit;`)(...values);
    await submit('hi'); await submit('/cost');
    return {admitted, banners};
  };
  const closed = await run(closedSession(), null);
  assert.deepEqual(closed.admitted, [['hi', 'req-1', null]]);
  assert.deepEqual(closed.banners.filter(Boolean), ['/cost isn’t available when Qorqbot runs Ada.']);
  const live = await run(managed(), BINDING);
  assert.deepEqual(live.admitted, [['hi', 'req-1', 'binding-1']]);
  assert.deepEqual(live.banners.filter(Boolean), ['/cost isn’t available when Qorqbot runs Ada.']);
});

// syncManaged against the fake DOM: the Stop block goes above the session line.
function shell({session}) {
  freshPage();
  const root = doc.createElement('div'), chatView = doc.createElement('div'), sessionLine = doc.createElement('div');
  chatView.append(sessionLine); root.append(chatView); doc.body.append(root);
  const cleanups = [], banners = [], loads = [], sent = [];
  const state = {session, finished: false};
  const sync = new Function('agentRuntime', 'label', 'sessionLine', 'liveBinding', 'paintSession', 'load', 'showBanner', 'cleanups', 'coreData', 'post', 'base', 'state', 'contextStatus', 'view',
    `let managedBar = null; let session = state.session; let finished = false;
     ${between('  const runtimeSend =', '  // The session line says what the empty')}
     return {syncManaged, bar: () => managedBar, set: value => { session = value; }, finish: () => { finished = true; }};`)(
    runtime, 'Ada', sessionLine, () => state.session?.binding, () => {}, () => loads.push(1), t => banners.push(t), cleanups, r => r, async (url, body) => { sent.push([url, body]); return {}; }, '/chat', state, null, null);
  return {root, chatView, sessionLine, sync, cleanups, banners, loads, sent};
}

test('a managed agent gets the Stop block above the session line', () => {
  const s = shell({session: managed({phase: 'working', activity: 'Editing', can_stop: true})});
  s.sync.syncManaged();
  const bar = s.sync.bar();
  assert.ok(bar);
  assert.equal(s.chatView.children[0], bar.el);
  assert.equal(s.chatView.children[1], s.sessionLine);
  // Stop is the composer's Send button while the agent works (chat-focus.test.js), never a button on this line.
  assert.equal(bar.el.querySelector('.mchat-stop').hidden, true);
  assert.equal(bar.el.querySelector('.mchat-activity-text').textContent, 'Editing');
  assert.equal(s.cleanups.length, 1);
  // asleep: nothing to stop
  s.sync.set(closedSession()); s.sync.syncManaged();
  assert.equal(bar.el.hidden, true);
  assert.equal(s.sync.bar(), bar, 'the same block is kept');
});

test('shown outside its block, the activity line takes no room there and still stops the agent', async () => {
  const ui = bar(), strip = doc.createElement('div');
  doc.body.append(strip); strip.append(ui.line);
  ui.update(managed({phase: 'working', activity: 'Editing', can_stop: true}));
  assert.equal(ui.line.hidden, false);
  assert.equal(ui.el.hidden, true, 'nothing left to show above the composer');
  click(ui.line.querySelector('.mchat-stop')); await flush();
  assert.equal(ui.sent.filter(([route]) => route === '/interrupt').length, 1);
});

test('the activity line shows the agent’s newest mid-work line after the step while it works, and drops it otherwise', () => {
  const ui = bar(), text = () => ui.line.querySelector('.mchat-activity-text');
  const note = {text: 'Reading the startup file…', title: 'Reading the startup file and the rest of it'};
  ui.update(managed({phase: 'working', activity: 'Reading a file'}), {note});
  assert.equal(text().textContent, 'Reading a file · Reading the startup file…');
  assert.equal(text().title, 'Reading the startup file and the rest of it');
  ui.update(managed({phase: 'working', activity: ''}), {note});
  assert.equal(text().textContent, 'Working · Reading the startup file…', 'beside the default step');
  ui.update(managed({phase: 'working', activity: 'Reading a file'}), {note: null});
  assert.equal(text().textContent, 'Reading a file', 'a newer entry cleared it');
  assert.equal(text().title, '');
  ui.update(managed({phase: 'idle'}), {note});
  assert.equal(ui.line.hidden, true, 'not working: no line, and no note');
  assert.equal(text().textContent, '');
  ui.update(managed({phase: 'working', activity: 'Editing'}));
  assert.equal(text().textContent, 'Editing', 'callers without a note are unchanged');
  ui.update(managed({phase: 'blocked', activity: 'Asking', pending: [question()]}), {note});
  assert.equal(text().textContent, 'Waiting for your answer', 'a waiting question shows alone');
  assert.equal(text().title, '');
  // the note's text is text, never markup
  ui.update(managed({phase: 'working', activity: 'Editing'}), {note: {text: PAYLOAD, title: PAYLOAD}});
  assert.equal(text().textContent, `Editing · ${PAYLOAD}`);
  assert.equal(text().querySelectorAll('img').length, 0);
});

test('the Stop block is not built before the first session read', () => {
  const s = shell({session: null});
  const before = s.root.querySelectorAll('div').length;
  s.sync.syncManaged();
  assert.equal(s.sync.bar(), null);
  assert.equal(s.root.querySelectorAll('div').length, before);
});

test('the event stream’s managed summary is shown at once, and a session that opened, closed or turned off is read in full', () => {
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
  assert.equal(run(null, {managed: {phase: 'working'}}).painted, 0);
  assert.equal(run(live, {revision: 3}).painted, 0);
  assert.equal(run(live, {managed: {open: false}}).painted, 0, 'a session that opened or closed is read in full');
  assert.equal(run(live, {managed: {off: true}}).painted, 0, 'so is one that was turned off');
  assert.equal(run(live, {managed: {off: false, phase: 'working'}}).painted, 1);
});

test('mountAgentView mounts Chat only: no Terminal view, no switch, and the options it never reads', () => {
  const body = between('let globalAgentViewActive = null;', 'function mountGlobalChat(');
  freshPage();
  const chats = [];
  const build = () => new Function('document', 'esc', 'mountGlobalChat', 'globalChatOpenFocusReference', `${body}; return mountAgentView;`)(
    doc, global.esc, (host, agent, options) => { const chat = {options, disposed: false, dispose() { this.disposed = true; }}; chats.push(chat); return chat; }, () => true);
  const mountView = build(), views = [];
  const mount = opts => { const pane = doc.createElement('main'); doc.body.append(pane); const view = mountView(pane, {agent_id: 'a1', name: 'Ada'}, {isCurrent: () => true, ...opts}); views.push({view, pane}); return {view, pane}; };
  try {
    const {view, pane} = mount();
    assert.equal(chats.length, 1);
    assert.equal(pane.children.length, 1, 'one host: Chat');
    assert.equal(view.showTerminal, undefined);
    assert.deepEqual(Object.keys(chats[0].options), ['openReference'], 'no openTerminal, no onExecution');
    view.showChat(); assert.equal(chats.length, 1, 'a warm Chat is kept, not rebuilt');
    // an old caller that still passes the Terminal options changes nothing
    const old = mount({terminal: {key: 'a1', base: '/t'}, view: 'terminal'});
    assert.equal(chats.length, 2);
    assert.equal(old.pane.children.length, 1);
    assert.equal(chats[0].disposed, true, 'mounting another view releases the first');
    old.view.dispose(); assert.equal(chats[1].disposed, true);
  } finally { views.forEach(({view}) => view.dispose()); }
});

test('the server\'s own sentence for a missing or signed-out client is shown as it is; the table is only the fallback', () => {
  const sentence = 'Helper is set to use Codex, and Codex isn\'t signed in yet. Sign in under Settings > Model/Provider, or switch Helper to Claude Code there.';
  assert.equal(runtime.refusalText('cli_not_signed_in', sentence), sentence);
  assert.equal(runtime.refusalText('cli_unavailable', '  ' + sentence + ' '), sentence);
  assert.match(runtime.refusalText('cli_not_signed_in'), /isn’t signed in yet\. Sign in under Settings > Model\/Provider/);
  assert.match(runtime.refusalText('cli_unavailable', ''), /Download it under Settings > Model\/Provider/);
  // only these codes carry a sentence written for the owner; any other code keeps the dashboard's own wording
  assert.equal(runtime.refusalText('open_busy', 'Something internal'), 'Several sessions are starting right now. Try again in a moment.');
  assert.equal(runtime.refusalText('cli_unavailable', 'x'.repeat(500)).includes('xxxx'), false);
  const source = between('  const refusalText = problem =>', '  // One admission request.');
  const text = new Function('agentRuntime', 'GLOBAL_CHAT_REFUSAL_TEXT', `${source}; return refusalText;`)(runtime, {});
  assert.equal(text({code: 'cli_not_signed_in', serverMessage: sentence}), sentence);
  const refusals = between('const GLOBAL_CHAT_REFUSALS = new Set', 'const GLOBAL_CHAT_REFUSAL_TEXT');
  assert.equal(new Function(`${refusals}; return GLOBAL_CHAT_REFUSALS;`)().has('cli_not_signed_in'), true);
});

test('while a session opens every control that is off says "Starting <agent>…", and only then', () => {
  const closed = {can_send: true, binding: null, managed: {open: false}};
  const openNotReady = {can_send: false, state: 'live', reason: 'binding_not_ready', managed: {open: true, phase: 'starting'}};
  const gone = {can_send: false, state: 'offline', managed: {open: true}};
  assert.equal(runtime.unavailableText(closed, 'Helper', {opening: true}), 'Starting Helper…');
  assert.equal(runtime.unavailableText(openNotReady, 'Helper', {opening: true}), 'Starting Helper…');
  assert.equal(runtime.unavailableText(openNotReady, 'Helper'), 'Starting Helper…', 'a session that is open and not yet bound is still opening');
  assert.equal(runtime.unavailableText({...openNotReady, reason: 'capture_not_ready', managed: {open: true}}, 'Helper'), 'Starting Helper…');
  assert.equal(runtime.unavailableText({can_send: false, state: 'offline', reason: 'registering', managed: {open: true}}, 'Helper'), 'Starting Helper…');
  assert.equal(runtime.summary({can_send: false, state: 'offline', reason: 'registering', managed: {open: true}}, 'Helper', true).line, 'Starting Helper…');
  assert.equal(runtime.unavailableText(gone, 'Helper'), 'Helper isn’t available right now.', 'not opening: the old reason');
  assert.equal(runtime.unavailableText(gone, 'Helper', {finished: true}), 'This chat is no longer available.');
  assert.equal(runtime.unavailableText(gone, 'Helper', {paired: true, opening: true}), 'Close the side conversation to message Helper.');
});

test('the client\'s own words about a failed turn are the session line, until a turn goes well', () => {
  const failed = {state: 'live', can_send: true, managed: {open: true, phase: 'idle', last_turn_status: 'error', turn_error: 'You\'ve hit your usage limit. Try again at 4:30 PM.'}};
  assert.deepEqual(runtime.summary(failed, 'Helper', true), {key: 'error', line: 'Helper couldn’t answer: You\'ve hit your usage limit. Try again at 4:30 PM.'});
  assert.equal(runtime.summary({...failed, managed: {...failed.managed, last_turn_status: 'completed', turn_error: null}}, 'Helper', true).key, 'live');
  assert.equal(runtime.summary({...failed, managed: {...failed.managed, phase: 'working'}}, 'Helper', true).key, 'working');
});

// ---- the account notice: a usage limit or a sign-in that is no longer accepted (js/agent-runtime.js, wording and sign-in in js/agent-clients.js)
const agentClients = require('../js/agent-clients.js');
const PROBLEM = (over = {}) => ({kind: 'usage_limit', provider: 'claude', resets_at: null, resets_text: 'Nov 28, 7pm (America/New_York)',
  account: {email: 'owner@example.com', plan: 'Max'}, other_provider: {id: 'codex', ready: true}, ...over});
const limited = (over = {}, session = {}) => managed({last_turn_status: 'error', turn_error: 'You’ve hit your limit', account_problem: PROBLEM(over)}, session);
const productRow = (over = {}) => ({installed: true, signed_in: true, account: {email: 'owner@example.com', plan: 'Max'}, source: 'system', version: '2.1.3',
  install: {phase: 'ready'}, sign_in: {state: 'idle', reason: null}, limit: {reached: true}, switched: null, ...over});
const connecting = (over = {}) => productRow({sign_in: {state: 'connecting', method: 'browser', page_url: 'https://claude.ai/oauth/x', accepts_code: true, code_submitted: false,
  switching: true, previous_account: {email: 'owner@example.com', plan: 'Max'}, ...over}});

// A bar with the account notice: `server.view` is what the sign-in state reads give; `server.acts` the actions sent; timers collected.
function accountBar(first = {claude: productRow(), codex: productRow({limit: {reached: false}})}, over = {}) {
  const server = {view: first, acts: [], reads: 0, others: [], timers: [], failOther: null};
  const ui = bar({clients: agentClients, useOther: async client => { server.others.push(client); if (server.failOther) throw server.failOther; return {}; },
    clientsOptions: {read: async () => { server.reads++; return server.view; }, act: async (provider, action, body) => { server.acts.push({provider, action, ...(body || {})}); return server.view; },
      schedule: fn => { server.timers.push(fn); return server.timers.length; }, cancel: id => { server.timers[id - 1] = null; }}, ...over});
  const box = () => ui.el.querySelector('.mchat-account');
  const buttons = () => box().querySelectorAll('[data-account-action]');
  const labels = () => buttons().map(b => b.textContent);
  return {ui, server, box, buttons, labels};
}

test('account_problem is read as the page uses it, and anything else is none', () => {
  assert.deepEqual(runtime.managedOf(limited()).accountProblem, {kind: 'usage_limit', provider: 'claude', resetsAt: null, resetsText: 'Nov 28, 7pm (America/New_York)',
    account: {email: 'owner@example.com', plan: 'Max'}, other: {id: 'codex', ready: true}});
  assert.equal(runtime.managedOf(managed()).accountProblem, null);
  for (const bad of [{kind: 'other', provider: 'claude'}, {kind: 'sign_in', provider: 'gemini'}, 'x', 5, [], {provider: 'claude'}]) {
    assert.equal(runtime.managedOf(managed({account_problem: bad})).accountProblem, null, JSON.stringify(bad));
  }
  const odd = runtime.managedOf(managed({account_problem: {kind: 'sign_in', provider: 'codex', account: {email: ''}, other_provider: {id: 'codex', ready: true}}})).accountProblem;
  assert.deepEqual([odd.account, odd.other], [null, null], 'no empty account, and never the same product as its own alternative');
});

test('an account problem replaces the plain “couldn’t answer” line; any other failed turn keeps it', () => {
  assert.equal(runtime.summary(limited(), 'Helper', true).key, 'live');
  assert.equal(runtime.summary(limited(), 'Helper', true).line, '');
  const plain = managed({last_turn_status: 'error', turn_error: 'Something broke'});
  assert.equal(runtime.summary(plain, 'Helper', true).line, 'Helper couldn’t answer: Something broke');
});

test('a limit shows the notice with its two buttons, and the sign-in state is read once, only while it is up', async () => {
  const t = accountBar();
  assert.equal(t.server.reads, 0, 'nothing is read before there is a problem');
  t.ui.update(managed());
  assert.equal(t.box().hidden, true);
  assert.equal(t.server.reads, 0, 'a chat without a problem never reads');
  t.ui.update(limited());
  await flush();
  assert.equal(t.server.reads, 1);
  assert.equal(t.box().hidden, false);
  assert.equal(t.ui.el.hidden, false);
  assert.match(t.box().querySelector('.mchat-notice-text').textContent, /^Claude usage limit reached\. It resets Nov 28, 7pm \(America\/New_York\)\.Signed in as owner@example\.com \(Max\)\. Every agent on Claude Code is waiting\.$/);
  assert.deepEqual(t.labels(), ['Switch Claude account', 'Use Codex for Ada']);
  assert.equal(t.buttons()[1].className, 'quiet');
  assert.equal(t.buttons()[0].getAttribute('aria-label'), 'Switch the Claude Code account');
  t.ui.update(limited());
  t.ui.update(limited());
  await flush();
  assert.equal(t.server.reads, 1, 'repeated session reads do not read again');
  assert.equal(t.server.timers.filter(Boolean).length, 0, 'and nothing polls while nothing is under way');
  // Gone with the problem: hidden, and the controller stopped.
  t.ui.update(managed({last_turn_status: 'completed'}));
  assert.equal(t.box().hidden, true);
  assert.equal(t.ui.el.hidden, true);
  assert.equal(t.box().querySelector('button'), null);
  t.ui.dispose();
});

test('the Codex button is there only while Codex is Ready, and trades places on a Codex limit', async () => {
  const t = accountBar({claude: productRow(), codex: productRow()});
  t.ui.update(limited({other_provider: {id: 'codex', ready: false}}));
  assert.deepEqual(t.labels(), ['Switch Claude account']);
  t.ui.update(limited({provider: 'codex', other_provider: {id: 'claude', ready: true}}));
  assert.deepEqual(t.labels(), ['Switch ChatGPT account', 'Use Claude Code for Ada']);
  assert.match(t.box().querySelector('.mchat-notice-text').textContent, /^Codex usage limit reached\./);
  t.ui.dispose();
});

test('Switch sends switch for the product, the notice becomes the sign-in part, and the typed code survives the session reads', async () => {
  const t = accountBar();
  t.ui.update(limited());
  await flush();
  t.server.view = {claude: connecting(), codex: productRow()};
  click(t.buttons()[0]);
  await flush();
  assert.deepEqual(t.server.acts, [{provider: 'claude', action: 'switch'}]);
  assert.match(t.box().querySelector('.mchat-notice-text').textContent, /^Finish signing in on the sign-in page\./);
  assert.match(t.box().querySelector('.mchat-notice-text').textContent, /Still signed in as owner@example\.com \(Max\) until you finish\./);
  assert.deepEqual(t.labels(), ['Cancel']);
  const link = t.box().querySelector('a[data-ac-open]');
  assert.equal(link.getAttribute('href'), 'https://claude.ai/oauth/x');
  assert.equal(link.getAttribute('target'), '_blank');
  assert.equal(link.getAttribute('rel'), 'noopener noreferrer');
  const input = t.box().querySelector('[data-ac-code]');
  input.value = 'my-code'; input.focus();
  t.ui.update(limited());           // the 2 s session read repaints Chat
  await flush();
  assert.equal(t.box().querySelector('[data-ac-code]'), input, 'the same field');
  assert.equal(doc.activeElement, input);
  // The page address changes: the part is rebuilt and keeps the text and the focus.
  t.server.view = {claude: connecting({page_url: 'https://claude.ai/oauth/y'}), codex: productRow()};
  t.server.timers.filter(Boolean).forEach(fn => fn());
  await flush();
  assert.equal(t.box().querySelector('[data-ac-code]').value, 'my-code');
  assert.equal(doc.activeElement, t.box().querySelector('[data-ac-code]'));
  // Connect and Enter both send it; the field then waits.
  t.server.view = {claude: connecting({page_url: 'https://claude.ai/oauth/y', code_submitted: true}), codex: productRow()};
  click(t.box().querySelector('[data-ac-finish]'));
  await flush();
  assert.deepEqual(t.server.acts.at(-1), {provider: 'claude', action: 'submit_code', code: 'my-code'});
  assert.equal(t.box().querySelector('[data-ac-code]').disabled, true);
  assert.equal(t.box().querySelector('[data-ac-checking]').hidden, false);
  // Cancel stops the sign-in; the old state returns, and a failure now is shown as such.
  t.server.view = {claude: productRow({sign_in: {state: 'failed', reason: 'The sign-in window was closed.'}}), codex: productRow()};
  click(t.buttons()[0]);
  await flush();
  assert.deepEqual(t.server.acts.at(-1), {provider: 'claude', action: 'cancel_connect'});
  t.ui.dispose();
});

test('a sign-in that failed after the owner tried here says so and offers Try again; one from before is not shown', async () => {
  const failed = productRow({sign_in: {state: 'failed', reason: 'The sign-in window was closed.'}});
  const old = accountBar({claude: failed, codex: productRow()});
  old.ui.update(limited()); await flush();
  assert.match(old.box().querySelector('.mchat-notice-text').textContent, /^Claude usage limit reached/, 'an old failure stays out of a fresh notice');
  old.ui.dispose();
  const t = accountBar({claude: productRow(), codex: productRow()});
  t.ui.update(limited()); await flush();
  t.server.view = {claude: failed, codex: productRow()};
  click(t.buttons()[0]); await flush();
  assert.match(t.box().querySelector('.mchat-notice-text').textContent, /^Signing in did not finish\. Nothing changed\.Still signed in as owner@example\.com \(Max\)\. The limit resets Nov 28, 7pm \(America\/New_York\)\. The sign-in window was closed\.$/);
  assert.deepEqual(t.labels(), ['Try again', 'Use Codex for Ada']);
  click(t.buttons()[0]); await flush();
  assert.deepEqual(t.server.acts.map(a => a.action), ['switch', 'switch']);
  t.ui.dispose();
});

test('a Codex device code is shown with Copy, with no field; and signing in after sign-out says Sign in again and sends connect', async () => {
  const t = accountBar({claude: productRow({signed_in: false}), codex: productRow()});
  t.ui.update(limited({kind: 'sign_in', provider: 'codex', other_provider: {id: 'claude', ready: true}}));
  await flush();
  assert.match(t.box().querySelector('.mchat-notice-text').textContent, /^Codex is signed out\.The sign-in for owner@example\.com \(Max\) is no longer accepted\.$/);
  assert.deepEqual(t.labels(), ['Sign in again']);
  t.server.view = {claude: productRow(), codex: connecting({method: 'device_code', page_url: 'https://auth.openai.com/codex/device', user_code: 'WXYZ-1234', accepts_code: false})};
  click(t.buttons()[0]); await flush();
  assert.deepEqual(t.server.acts, [{provider: 'codex', action: 'connect'}]);
  assert.equal(t.box().querySelector('[data-ac-user-code]').textContent, 'WXYZ-1234');
  assert.ok(t.box().querySelector('[data-ac-copy]'));
  assert.equal(t.box().querySelector('[data-ac-code]'), null);
  assert.match(t.box().querySelector('.mchat-notice-text').textContent, /^Open the sign-in page and type the code shown here\./);
  t.ui.dispose();
});

test('Use the other product asks the server for this agent only, and shows a refusal in the notice', async () => {
  const t = accountBar();
  t.ui.update(limited()); await flush();
  click(t.buttons()[1]); await flush();
  assert.deepEqual(t.server.others, ['codex']);
  assert.ok(t.ui.changes.length > 0, 'Chat is read again after the request');
  // A refusal shows its words and the buttons come back.
  const bad = accountBar();
  bad.server.failOther = new Error('Codex is not signed in. Sign in first.');
  bad.ui.update(limited()); await flush();
  click(bad.buttons()[1]); await flush();
  assert.match(bad.box().querySelector('.mchat-notice-error').textContent, /^Codex is not signed in\. Sign in first\.$/);
  assert.equal(bad.buttons()[0].disabled, false);
  t.ui.dispose(); bad.ui.dispose();
});

test('while the request is on its way every button waits, and the pressed one says “Switching…”', async () => {
  let release; const hold = new Promise(resolve => { release = resolve; });
  const t = accountBar(undefined, {useOther: async () => { await hold; return {}; }});
  t.ui.update(limited()); await flush();
  click(t.buttons()[1]); await flush();
  assert.deepEqual(t.buttons().map(b => [b.textContent, b.disabled]), [['Switch Claude account', true], ['Switching…', true]]);
  click(t.buttons()[0]);
  assert.deepEqual(t.server.acts, [], 'a waiting button does nothing');
  release(); await flush();
  assert.deepEqual(t.buttons().map(b => b.disabled), [false, false]);
  t.ui.dispose();
});

test('the notice keeps its own place: the one-action offer and the cards still work beside it', async () => {
  const t = accountBar();
  t.ui.update(limited({}, {})); await flush();
  const offered = [];
  t.ui.offer({text: 'The earlier conversation can’t be continued because its file is missing. Nothing was sent.', label: 'Start a new conversation'}, () => offered.push(1));
  assert.equal(t.ui.el.querySelector('.mchat-notice').hidden, false);
  assert.equal(t.box().hidden, false);
  click(t.ui.el.querySelector('.mchat-notice-action'));
  assert.deepEqual(offered, [1]);
  assert.equal(t.box().hidden, false, 'answering the offer leaves the account notice');
  t.ui.update(limited({}, {}));
  t.ui.dispose();
});

test('text from the server in the notice is escaped, and a chat without the sign-in script shows no notice', async () => {
  const t = accountBar();
  t.ui.update(limited({resets_text: '<img src=x onerror=1>', account: {email: '<b>x</b>@y.test', plan: '<i>'}})); await flush();
  assert.equal(t.box().querySelector('img'), null);
  assert.equal(t.box().querySelector('b'), null);
  assert.match(t.box().textContent, /<img src=x onerror=1>/);
  t.ui.dispose();
  const bare = bar();
  bare.update(limited());
  assert.equal(bare.el.querySelector('.mchat-account').hidden, true);
});

test('dispose ends the reads: nothing is read after a chat is closed', async () => {
  const t = accountBar({claude: connecting(), codex: productRow()});
  t.ui.update(limited()); await flush();
  const reads = t.server.reads;
  t.ui.dispose();
  t.server.timers.filter(Boolean).forEach(fn => fn());
  await flush();
  assert.equal(t.server.reads, reads);
});

test('global-chat.js hands the bar the sign-in script and the one-agent switch route', () => {
  const chat = read('js/global-chat.js');
  assert.match(chat, /clients: typeof agentClients !== 'undefined' \? agentClients : null/);
  assert.match(chat, /post\(`\$\{base\}\/use-other-product`, \{client\}\)/);
  assert.ok(read('index.html').indexOf('/js/agent-clients.js') < read('index.html').indexOf('/js/global-chat.js'));
});
