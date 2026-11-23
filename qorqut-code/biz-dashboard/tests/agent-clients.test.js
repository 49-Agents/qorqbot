'use strict';
// node --test biz-dashboard/tests/agent-clients.test.js
// Claude Code and Codex set-up rows: the copy of every state, the polling, the screen before the walkthrough and where
// the component is mounted. mount and preflight run against the small fake DOM below (no browser, no dependencies).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
global.esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
global.coreData = result => result.data;
const calls = [];
let reply = null;
global.api = async url => { calls.push({method: 'GET', url}); return {status: 'ok', data: reply}; };
global.post = async (url, body) => { calls.push({method: 'POST', url, body}); return {status: 'ok', data: reply}; };
const clients = require('../js/agent-clients.js');

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
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
// Timers collected and fired by hand; `delays` is what each was scheduled for.
function clock() {
  const timers = [], delays = [];
  return {timers, delays, schedule: (fn, ms) => { timers.push(fn); delays.push(ms); return timers.length; },
    cancel: id => { if (timers[id - 1]) timers[id - 1] = null; },
    fire: async () => { const fn = timers.splice(0).find(Boolean); if (fn) { await fn(); await flush(); } return !!fn; }};
}

const client = (over = {}) => ({installed: false, version: null, source: null, signed_in: false, account: null,
  install: {phase: 'not_downloaded', progress: null, message: null}, sign_in: {state: 'idle', reason: null}, ...over});
const view = (claude = {}, codex = {}) => ({claude: client(claude), codex: client(codex)});
const row = (provider, over, extra) => clients.rowState(provider, client(over), extra);
const installed = {installed: true, version: '2.1.3', source: 'managed', install: {phase: 'ready', progress: 100, message: null}};

test('nothing installed: the button is Download and the row says Qorqbot does it', () => {
  const state = row('claude', {});
  assert.equal(state.key, 'download');
  assert.equal(state.pill, 'Not installed');
  assert.deepEqual(state.actions.map(a => [a.id, a.label]), [['download', 'Download']]);
  assert.match(state.detail, /Qorqbot downloads Claude Code for you\./);
  assert.equal(row('codex', {}).name, 'Codex');
});

test('a download shows its progress and offers Cancel', () => {
  const state = row('claude', {install: {phase: 'downloading', progress: 42.4, message: null}});
  assert.equal(state.key, 'downloading');
  assert.equal(state.progress, 42);
  assert.equal(state.indeterminate, false);
  assert.deepEqual(state.actions.map(a => [a.id, a.label]), [['cancel_download', 'Cancel']]);
  assert.match(state.detail, /Downloading Claude Code/);
  assert.equal(row('claude', {install: {phase: 'downloading', progress: null}}).indeterminate, true);
  assert.equal(row('claude', {install: {phase: 'downloading', progress: 180}}).progress, 100);
});

test('installed but not signed in: the button is Connect and the row names the account to use', () => {
  const claude = row('claude', installed);
  assert.equal(claude.key, 'connect');
  assert.equal(claude.pill, 'Sign in needed');
  assert.deepEqual(claude.actions.map(a => [a.id, a.label]), [['connect', 'Connect']]);
  assert.match(claude.detail, /Sign in with your Claude account/);
  assert.match(row('codex', installed).detail, /your ChatGPT account/);
});

test('a pending sign-in says to finish in the browser window and offers Cancel', () => {
  const state = row('codex', {...installed, sign_in: {state: 'connecting', reason: null}});
  assert.equal(state.key, 'connecting');
  assert.match(state.detail, /^Finish signing in in the browser window that opened\./);
  assert.deepEqual(state.actions.map(a => [a.id, a.label]), [['cancel_connect', 'Cancel']]);
});

test('a failed sign-in gives its reason and the one next action', () => {
  const state = row('claude', {...installed, sign_in: {state: 'failed', reason: 'The sign-in window was closed.'}});
  assert.equal(state.key, 'connect');
  assert.equal(state.detail, 'Signing in did not finish (The sign-in window was closed). Press Connect to try again.');
  assert.deepEqual(state.actions.map(a => a.id), ['connect']);
  assert.match(row('claude', {...installed, sign_in: {state: 'failed', reason: null}}).detail, /^Signing in did not finish\. Press Connect/);
});

test('signed in: Ready with the account and no button', () => {
  const state = row('claude', {...installed, signed_in: true, account: {email: 'sam@example.com', plan: 'Max'}});
  assert.equal(state.key, 'ready');
  assert.equal(state.tone, 'ok');
  assert.equal(state.pill, 'Ready');
  assert.equal(state.detail, 'Signed in as sam@example.com (Max). Version 2.1.3.');
  assert.deepEqual(state.actions, []);
  assert.equal(row('codex', {...installed, signed_in: true, version: null}).detail, 'Signed in.');
});

test('a failed download says why and offers Try again; a missing reason still names a next step', () => {
  const state = row('claude', {install: {phase: 'error', progress: null, message: 'Could not reach the download server'}});
  assert.equal(state.key, 'error');
  assert.equal(state.detail, 'Could not reach the download server. Check your internet connection, then try again.');
  assert.deepEqual(state.actions.map(a => [a.id, a.label]), [['download', 'Try again']]);
  assert.match(row('claude', {install: {phase: 'error'}}).detail, /^The download did not finish\. Check your internet connection/);
});

test('a computer Qorqbot cannot download for says so, points to the product site and offers Check again', () => {
  const state = row('codex', {install: {phase: 'unsupported', progress: null, message: 'No build for this processor'}});
  assert.equal(state.key, 'unsupported');
  assert.match(state.detail, /^No build for this processor\. Install it yourself from github\.com\/openai\/codex, then press Check again\.$/);
  assert.deepEqual(state.actions.map(a => [a.id, a.label]), [['refresh', 'Check again']]);
  assert.match(row('claude', {install: {phase: 'unsupported'}}).detail, /claude\.com\/code/);
});

test('an installed product is never shown as a download state, whatever the download says', () => {
  assert.equal(row('claude', {installed: true, install: {phase: 'not_downloaded'}}).key, 'connect');
  assert.equal(row('claude', {installed: true, source: 'system', install: {phase: 'unsupported'}, signed_in: true}).key, 'ready');
});

test('every state that is blocked has a next action or is plainly under way, and none uses jargon', () => {
  const states = [client(), client({install: {phase: 'error'}}), client({install: {phase: 'unsupported'}}), client({...installed}),
    client({...installed, sign_in: {state: 'failed', reason: 'x'}}), client({install: {phase: 'downloading', progress: 5}}),
    client({...installed, sign_in: {state: 'connecting'}}), client({...installed, signed_in: true}), client({install: {phase: 'ready'}})];
  for (const it of states) for (const p of clients.PROVIDERS) {
    const s = clients.rowState(p, it);
    assert.ok(s.detail.length > 10, s.key);
    assert.ok(!/\b(CLI|binary|PATH|OAuth|token|npm|terminal command)\b/.test(s.detail + s.pill), s.key + ': ' + s.detail);
    if (['download', 'connect', 'error', 'unsupported', 'connecting', 'downloading'].includes(s.key)) assert.ok(s.actions.length, s.key);
    for (const a of s.actions) assert.ok(a.label && a.aria, s.key);
  }
});

test('a request on its way turns the button into a labelled busy state instead of a bare disabled one', () => {
  const down = row('claude', {}, {pending: 'download'});
  assert.deepEqual(down.actions.map(a => [a.label, a.disabled, a.busy]), [['Starting…', true, true]]);
  const connect = row('claude', installed, {pending: 'connect'});
  assert.equal(connect.actions[0].label, 'Opening your browser…');
  const cancel = row('claude', {install: {phase: 'downloading', progress: 3}}, {pending: 'cancel_download'});
  assert.equal(cancel.actions[0].label, 'Cancelling…');
  assert.equal(row('claude', {}, {error: 'Nope'}).error, 'Nope');
});

test('the dashboard continues once any one product is installed and signed in', () => {
  assert.equal(clients.hasReady(view()), false);
  assert.equal(clients.hasReady(view({...installed})), false);
  assert.equal(clients.hasReady(view({signed_in: true})), false);
  assert.equal(clients.hasReady(view({...installed, signed_in: true})), true);
  assert.equal(clients.hasReady(view({}, {...installed, signed_in: true})), true);
  assert.equal(clients.hasReady(null), false);
  assert.equal(clients.hasReady({}), false);
});

test('reading continues while a download or sign-in is under way and stops once settled', () => {
  assert.equal(clients.needsPoll(view()), false);
  assert.equal(clients.needsPoll(view({install: {phase: 'downloading', progress: 10}})), true);
  assert.equal(clients.needsPoll(view({}, {...installed, sign_in: {state: 'connecting'}})), true);
  assert.equal(clients.needsPoll(view({install: {phase: 'ready'}})), true);
  assert.equal(clients.needsPoll(view({...installed, sign_in: {state: 'failed', reason: 'x'}})), false);
  assert.equal(clients.needsPoll(view({install: {phase: 'error'}}, {...installed, signed_in: true})), false);
});

// A controller with a manual clock: scheduled reads are collected and fired by hand.
function harness(reads, acts = {}) {
  const timers = [], delays = [];
  let i = 0;
  const sent = [];
  const ctl = clients.controller({
    read: async () => { const next = reads[Math.min(i++, reads.length - 1)]; if (next instanceof Error) throw next; return next; },
    act: async (provider, action) => { sent.push([provider, action]); const out = acts[action]; if (out instanceof Error) throw out; return out; },
    schedule: (fn, ms) => { timers.push(fn); delays.push(ms); return timers.length; }, cancel: id => { if (timers[id - 1]) timers[id - 1] = null; },
    delay: 2000, slow: 15000,
  });
  const fire = async () => { const fn = timers.splice(0).find(Boolean); if (fn) await fn(); return !!fn; };
  return {ctl, fire, sent, timers, delays, reads: () => i};
}

test('a download is polled until it is installed, then reading stops', async () => {
  const down = n => view({install: {phase: 'downloading', progress: n}});
  const h = harness([down(10), down(60), view({...installed})]);
  await h.ctl.refresh();
  assert.equal(h.ctl.state.view.claude.install.progress, 10);
  assert.equal(await h.fire(), true);
  assert.equal(h.ctl.state.view.claude.install.progress, 60);
  assert.equal(await h.fire(), true);
  assert.equal(h.ctl.state.view.claude.installed, true);
  assert.equal(await h.fire(), false);
  assert.equal(h.reads(), 3);
});

test('pressing Download sends the action, marks the row busy meanwhile, then keeps reading while it downloads', async () => {
  const downloading = view({install: {phase: 'downloading', progress: 0}});
  const busy = [];
  const h = harness([view({...installed})], {download: downloading});
  const watched = clients.controller({read: async () => view(), act: async () => { busy.push({...watched.state.pending}); return downloading; },
    schedule: () => 1, cancel() {}});
  await watched.refresh();
  await watched.run('claude', 'download');
  assert.deepEqual(busy, [{claude: 'download'}]);
  assert.deepEqual(watched.state.pending, {});
  assert.equal(clients.needsPoll(watched.state.view), true);
  await h.ctl.run('claude', 'download');
  assert.deepEqual(h.sent, [['claude', 'download']]);
  assert.equal(await h.fire(), true);
  assert.equal(h.ctl.state.view.claude.installed, true);
});

test('a failed action is reported on its own row and the view is kept', async () => {
  const h = harness([view({...installed})], {connect: new Error('Qorqbot could not open your browser.')});
  await h.ctl.refresh();
  await h.ctl.run('claude', 'connect');
  assert.equal(h.ctl.state.errors.claude, 'Qorqbot could not open your browser.');
  assert.equal(h.ctl.state.view.claude.installed, true);
  assert.equal(clients.rows(h.ctl.state.view, h.ctl.state)[0].error, 'Qorqbot could not open your browser.');
  assert.equal(clients.rows(h.ctl.state.view, h.ctl.state)[1].error, '');
});

test('a sign-in is polled until it completes, and Cancel goes back to Connect', async () => {
  const connecting = view({...installed, sign_in: {state: 'connecting', reason: null}});
  const h = harness([connecting, view({...installed, signed_in: true, account: {email: 'a@b.co'}})].slice(1), {connect: connecting, cancel_connect: view({...installed})});
  await h.ctl.run('claude', 'connect');
  assert.equal(clients.rows(h.ctl.state.view)[0].key, 'connecting');
  assert.equal(await h.fire(), true);
  assert.equal(clients.rows(h.ctl.state.view)[0].key, 'ready');
  assert.equal(await h.fire(), false);
  await h.ctl.run('claude', 'cancel_connect');
  assert.deepEqual(h.sent, [['claude', 'connect'], ['claude', 'cancel_connect']]);
  assert.equal(clients.rows(h.ctl.state.view)[0].key, 'connect');
  assert.equal(await h.fire(), false);
});

test('a read that fails while polling is retried quietly; five failures in a row are shown', async () => {
  const boom = new Error('offline');
  const h = harness([view({install: {phase: 'downloading', progress: 5}}), boom, boom, boom, boom, boom]);
  await h.ctl.refresh();
  for (let n = 1; n <= 4; n++) { await h.fire(); assert.equal(h.ctl.state.loadError, '', `failure ${n}`); }
  await h.fire();
  assert.equal(h.ctl.state.loadError, 'offline');
  assert.equal(h.ctl.state.view.claude.install.phase, 'downloading');
});

test('a first read that fails is reported, and Try again reads again', async () => {
  const h = harness([new Error('Could not reach Qorqbot.'), view()]);
  await h.ctl.refresh();
  assert.equal(h.ctl.state.view, null);
  assert.equal(h.ctl.state.loadError, 'Could not reach Qorqbot.');
  await h.ctl.refresh();
  assert.equal(h.ctl.state.loadError, '');
  assert.ok(h.ctl.state.view);
});

test('a stopped controller schedules and reads nothing more', async () => {
  const h = harness([view({install: {phase: 'downloading', progress: 5}})]);
  await h.ctl.refresh();
  assert.equal(h.timers.filter(Boolean).length, 1);
  h.ctl.stop();
  assert.equal(await h.fire(), false);
  assert.equal(h.reads(), 1);
});

test('Check again reads the server instead of sending an action', async () => {
  const h = harness([view({install: {phase: 'unsupported'}}), view({...installed})]);
  await h.ctl.refresh();
  await h.ctl.run('claude', 'refresh');
  assert.deepEqual(h.sent, []);
  assert.equal(h.ctl.state.view.claude.installed, true);
});

test('the default requests are GET and POST /root/agent-clients with the provider and action', async () => {
  reply = view({...installed});
  calls.length = 0;
  const seen = [];
  const ctl = clients.controller({onChange: state => seen.push(state.view), schedule: () => 1, cancel() {}});
  await ctl.refresh();
  await ctl.run('codex', 'download');
  await ctl.run('claude', 'cancel_connect');
  assert.deepEqual(calls, [
    {method: 'GET', url: '/root/agent-clients'},
    {method: 'POST', url: '/root/agent-clients', body: {provider: 'codex', action: 'download'}},
    {method: 'POST', url: '/root/agent-clients', body: {provider: 'claude', action: 'cancel_connect'}},
  ]);
  assert.equal(ctl.state.view.claude.version, '2.1.3');
});

// ---- copy: a plain message from the server, an API-key sign-in, missing values, the managed-copy note

test('an owner copy that is too old shows the server message above Download, not the generic line', () => {
  const state = row('claude', {install: {phase: 'not_downloaded', progress: null, message: 'The Claude Code on this computer is too old to use'}});
  assert.equal(state.key, 'download');
  assert.equal(state.detail, 'The Claude Code on this computer is too old to use.');
  assert.ok(!state.detail.includes('Qorqbot downloads'));
  assert.deepEqual(state.actions.map(a => [a.id, a.label]), [['download', 'Download']]);
  assert.match(row('claude', {install: {phase: 'not_downloaded', message: '  '}}).detail, /^Qorqbot downloads Claude Code for you/);
  assert.match(row('claude', {install: {phase: 'not_downloaded', message: {}}}).detail, /^Qorqbot downloads Claude Code for you/);
});

test('signed in with an API key reads sensibly and never prints undefined or null', () => {
  const signed = {...installed, signed_in: true};
  assert.equal(row('claude', {...signed, account: {method: 'api_key'}}).detail, 'Signed in with an API key. Version 2.1.3.');
  assert.equal(row('claude', {...signed, version: null, account: {method: 'api_key', email: null, plan: null}}).detail, 'Signed in with an API key.');
  assert.equal(row('claude', {...signed, account: {method: 'api_key', email: 'a@b.co'}}).detail, 'Signed in as a@b.co. Version 2.1.3.');
  assert.equal(row('claude', {...signed, account: {email: 'a@b.co', plan: null}}).detail, 'Signed in as a@b.co. Version 2.1.3.');
  assert.equal(row('claude', {...signed, account: {plan: 'Max'}}).detail, 'Signed in. Version 2.1.3.');
  assert.equal(row('claude', {...signed, account: null, version: undefined}).detail, 'Signed in.');
});

test('no text path prints undefined, null or [object Object] whatever the server leaves out', () => {
  const nothing = [null, undefined, '', {}, 0];
  for (const missing of nothing) {
    const items = [
      {}, {install: null}, {install: {phase: 'error', message: missing}}, {install: {phase: 'unsupported', message: missing}},
      {install: {phase: 'not_downloaded', message: missing}}, {install: {phase: 'downloading', progress: missing}},
      {...installed, version: missing, sign_in: {state: 'failed', reason: missing}},
      {...installed, version: missing, signed_in: true, account: {email: missing, plan: missing, method: missing}},
      {...installed, version: missing, signed_in: true, account: missing},
    ];
    for (const over of items) for (const p of clients.PROVIDERS) {
      const state = clients.rowState(p, {...client(), ...over});
      const shown = [state.name, state.pill, state.detail, state.note, state.error, ...state.actions.map(a => a.label + a.aria)].join(' | ');
      assert.ok(!/undefined|null|\[object/.test(shown), shown);
    }
  }
});

test('a Ready managed copy says agents started from now on use it; a system copy or a row not Ready says nothing', () => {
  const managed = row('claude', {...installed, signed_in: true, source: 'managed'});
  assert.equal(managed.note, 'Agents use this copy from their next session on.');
  assert.equal(row('claude', {...installed, signed_in: true, source: 'system'}).note, '');
  assert.equal(row('claude', {...installed, signed_in: true, source: null}).note, '');
  assert.equal(row('claude', {...installed, source: 'managed'}).note, '', 'installed but not signed in');
  assert.equal(row('claude', {install: {phase: 'downloading', progress: 4}, source: 'managed'}).note, '');
  assert.ok(!/restart|reopen|quit/i.test(managed.note + managed.detail), 'no restart requirement is invented');
  const [host] = freshPage('host');
  const panel = clients.mount(host, {view: view({...installed, signed_in: true, source: 'managed'}, {...installed, signed_in: true, source: 'system'})});
  assert.equal(panel.root.querySelector('[data-ac-row="claude"] .ac-note').textContent, 'Agents use this copy from their next session on.');
  assert.equal(panel.root.querySelector('[data-ac-row="codex"] .ac-note'), null);
  panel.stop();
});

test('server strings are escaped: an install message with markup renders as text', () => {
  const [host] = freshPage('host');
  const evil = '<img src=x onerror=alert(1)>';
  const panel = clients.mount(host, {view: view({install: {phase: 'not_downloaded', message: evil}},
    {...installed, signed_in: true, account: {email: '<b>x</b>@a.co', plan: evil}})});
  assert.equal(panel.root.querySelector('img'), null);
  assert.equal(panel.root.querySelector('b'), null);
  const claude = panel.root.querySelector('[data-ac-row="claude"] .ac-detail');
  assert.equal(claude.textContent, evil + '.');
  assert.equal(panel.root.querySelector('[data-ac-row="codex"] .ac-detail').textContent, `Signed in as <b>x</b>@a.co (${evil}). Version 2.1.3.`);
  panel.stop();
  const failed = clients.mount(host, {view: view({install: {phase: 'error', message: evil}})});
  assert.equal(failed.root.querySelector('img'), null);
  failed.stop();
});

// ---- polling lifetime

test('after the fast polls the controller slows down instead of stopping, and coming back resumes the fast rate', async () => {
  const down = view({install: {phase: 'downloading', progress: 7}});
  const h = harness([down]);
  await h.ctl.refresh();
  assert.equal(clients.POLL_LIMIT, 450);
  for (let n = 0; n < clients.POLL_LIMIT; n++) assert.equal(await h.fire(), true, `poll ${n}`);
  const delays = h.delays;
  assert.ok(delays.slice(0, clients.POLL_LIMIT).every(ms => ms === 2000), 'fast for the first 15 minutes');
  assert.equal(clients.SLOW_MS, 15000);
  for (let n = 0; n < 3; n++) { assert.equal(await h.fire(), true, 'still polling after the limit'); }
  assert.deepEqual(delays.slice(clients.POLL_LIMIT, clients.POLL_LIMIT + 3), [15000, 15000, 15000]);
  const readsBefore = h.reads();
  await h.ctl.resume();
  assert.equal(h.reads(), readsBefore + 1, 'resume reads at once');
  assert.equal(delays[delays.length - 1], 2000, 'and the next read is at the fast rate');
  // The download finishes: everything stops, and resume has nothing to do.
  const done = harness([down, view({...installed})]);
  await done.ctl.refresh(); await done.fire();
  const reads = done.reads(); await done.ctl.resume();
  assert.equal(done.reads(), reads);
});

test('a download that outlasts the fast polls still ends in Ready on the screen', async () => {
  const [host] = freshPage('host');
  const tick = clock();
  const down = view({install: {phase: 'downloading', progress: 50}});
  let phase = down;
  const panel = clients.mount(host, {view: down, read: async () => phase, schedule: tick.schedule, cancel: tick.cancel});
  await flush();
  for (let n = 0; n < clients.POLL_LIMIT + 2; n++) await tick.fire();
  assert.equal(panel.root.querySelector('[data-ac-row="claude"]').dataset.state, 'downloading');
  assert.equal(tick.delays[tick.delays.length - 1], clients.SLOW_MS);
  phase = view({...installed, signed_in: true});
  await tick.fire();
  assert.equal(panel.root.querySelector('[data-ac-row="claude"]').dataset.state, 'ready');
  assert.equal(await tick.fire(), false, 'nothing left to read');
  panel.stop();
});

test('the tab becoming visible reads again at once; polling and the listener stop when the rows leave the page', async () => {
  const [host] = freshPage('host');
  const tick = clock();
  let reads = 0;
  const down = view({install: {phase: 'downloading', progress: 50}});
  const panel = clients.mount(host, {view: down, read: async () => { reads++; return down; }, schedule: tick.schedule, cancel: tick.cancel});
  await flush();
  assert.equal(reads, 1, 'a download under way is read straight away');
  doc.visibilityState = 'hidden'; doc.dispatchEvent({type: 'visibilitychange'}); await flush();
  assert.equal(reads, 1, 'hidden: nothing');
  doc.visibilityState = 'visible'; doc.dispatchEvent({type: 'visibilitychange'}); await flush();
  assert.equal(reads, 2);
  assert.equal(tick.delays[tick.delays.length - 1], 2000);
  assert.ok(tick.timers.some(Boolean), 'a read is scheduled');
  host.replaceChildren();
  assert.equal(panel.root.isConnected, false);
  assert.equal(await tick.fire(), true);
  assert.equal(reads, 2, 'the scheduled read does nothing once the rows are gone');
  assert.equal(await tick.fire(), false, 'and nothing is scheduled after it');
  doc.dispatchEvent({type: 'visibilitychange'}); await flush();
  assert.equal(reads, 2);
  assert.equal((doc.listeners.visibilitychange || []).length, 0, 'the listener removed itself');
});

test('stop removes the visibility listener too', () => {
  const [host] = freshPage('host');
  const panel = clients.mount(host, {view: view({...installed}), read: async () => view()});
  assert.equal(doc.listeners.visibilitychange.length, 1);
  panel.stop();
  assert.equal(doc.listeners.visibilitychange.length, 0);
});

// ---- the rows on a page: what is painted, and where the keyboard stays

test('a row repaints in place: Download goes busy, then Cancel takes the focus the button had', async () => {
  const [host] = freshPage('host');
  const tick = clock();
  const downloading = view({install: {phase: 'downloading', progress: 0}});
  let release;
  const panel = clients.mount(host, {view: view(), read: async () => downloading, schedule: tick.schedule, cancel: tick.cancel,
    act: () => new Promise(resolve => { release = () => resolve(downloading); })});
  const claude = () => panel.root.querySelector('[data-ac-row="claude"]');
  const button = claude().querySelector('[data-ac-action="download"]');
  assert.equal(claude().dataset.state, 'download');
  button.focus(); assert.equal(doc.activeElement, button);
  click(button); await flush();
  const busy = claude().querySelector('button');
  assert.equal(busy.textContent, 'Starting…');
  assert.ok(busy.disabled, 'busy, so a second press does nothing');
  assert.equal(busy.getAttribute('aria-label'), 'Starting…');
  click(busy); await flush();
  release(); await flush();
  assert.equal(claude().dataset.state, 'downloading');
  const cancel = claude().querySelector('[data-ac-action="cancel_download"]');
  assert.equal(doc.activeElement, cancel, 'focus stayed in the row');
  assert.equal(panel.root.querySelector('[data-ac-sr]').textContent.startsWith('Claude Code: Downloading.'), true);
  panel.stop();
});

test('a failed first read shows the reason with Try again; the stale view keeps its rows', async () => {
  const [host] = freshPage('host');
  let fail = true;
  const panel = clients.mount(host, {read: async () => { if (fail) throw new Error('Could not reach Qorqbot.'); return view({...installed}); }});
  await flush();
  const load = panel.root.querySelector('[data-ac-load]');
  assert.equal(load.hidden, false);
  assert.match(load.textContent, /Could not reach Qorqbot\. Try again/);
  fail = false;
  click(load.querySelector('[data-ac-reload]')); await flush();
  assert.equal(load.hidden, true);
  assert.equal(panel.root.querySelector('[data-ac-row="claude"]').dataset.state, 'connect');
  panel.stop();
});

// ---- the screen before the walkthrough

const gateEl = () => doc.body.children.find(el => el.className === 'ac-gate');

test('preflight returns at once, with no screen, when a product is already Ready', async () => {
  freshPage('app');
  doc.failOn = 'createElement';
  const ready = view({...installed, signed_in: true});
  assert.equal(await clients.preflight({read: async () => ready}), ready);
  assert.equal(gateEl(), undefined);
  assert.equal(doc.created, 0, 'no element was built for a screen');
});

test('preflight resolves without a screen when the first read fails, returns nothing usable, or is skipped', async () => {
  const [app] = freshPage('app');
  doc.failOn = 'createElement';
  assert.equal(await clients.preflight({read: async () => { throw new Error('down'); }}), null);
  assert.equal(await clients.preflight({read: () => { throw new Error('threw before returning a promise'); }}), null);
  assert.equal(await clients.preflight({read: async () => null}), null);
  assert.equal(await clients.preflight({read: async () => 'oops'}), null);
  visited['qorqbot:agent-setup-later'] = '1';
  let asked = 0;
  assert.equal(await clients.preflight({read: async () => { asked++; return view(); }}), null, 'skipped earlier this session');
  assert.equal(asked, 0);
  assert.equal(app.inert, false);
  assert.equal(gateEl(), undefined);
});

test('preflight does not wait for a first read that never answers', async () => {
  const [app] = freshPage('app');
  doc.failOn = 'createElement';
  const started = Date.now();
  const result = await clients.preflight({read: () => new Promise(() => {}), timeout: 30});
  assert.equal(result, null);
  assert.ok(Date.now() - started < 1000);
  assert.equal(gateEl(), undefined);
  assert.equal(app.inert, false);
  assert.equal(clients.FIRST_READ_MS, 5000, 'the real limit is about five seconds');
  // A read that answers late, after the limit, changes nothing.
  let late;
  const slowRead = () => new Promise(resolve => { late = resolve; });
  assert.equal(await clients.preflight({read: slowRead, timeout: 10}), null);
  late(view());
  await flush();
  assert.equal(gateEl(), undefined);
});

test('preflight resolves, and puts the page back, when building the screen throws', async () => {
  const [app, other] = freshPage('app', 'other');
  other.inert = true;
  doc.failOn = 'addEventListener';
  assert.equal(await clients.preflight({read: async () => view()}), null);
  assert.equal(gateEl(), undefined, 'the half-built screen is gone');
  assert.equal(app.inert, false, 'the page behind is usable again');
  assert.equal(other.inert, true, 'what was already inert stays so');
  doc.failOn = 'createElement';
  assert.equal(await clients.preflight({read: async () => view()}), null, 'and a failure before anything is shown');
  assert.equal(app.inert, false);
});

test('the screen blocks the page, keeps Continue off with its reason until Ready, then enables it and takes focus there', async () => {
  const [app, other] = freshPage('app', 'other');
  other.inert = true;
  const tick = clock();
  const down = view({install: {phase: 'downloading', progress: 30}});
  let current = view();
  const finished = view({...installed, signed_in: true, source: 'managed'});
  const result = clients.preflight({read: async () => current, act: async () => down, schedule: tick.schedule, cancel: tick.cancel});
  await flush();
  const gate = gateEl();
  assert.ok(gate, 'a screen is shown');
  const card = gate.querySelector('section');
  assert.equal(card.getAttribute('role'), 'dialog');
  assert.equal(card.getAttribute('aria-modal'), 'true');
  assert.equal(app.inert, true, 'the page behind cannot be reached');
  assert.equal(other.inert, true);
  const next = gate.querySelector('[data-ac-continue]'), later = gate.querySelector('[data-ac-later]'), hint = gate.querySelector('[data-ac-gate-hint]');
  assert.equal(next.disabled, true);
  assert.equal(next.getAttribute('aria-describedby'), 'ac-gate-hint');
  assert.equal(hint.textContent, 'Continue unlocks when Claude Code or Codex says Ready.');
  assert.equal(later.hidden, false);
  assert.equal(later.textContent, 'Set up later');
  assert.equal(doc.activeElement, gate.querySelector('[data-ac-action="download"]'), 'focus starts on the first action');
  click(next); await flush();
  assert.equal(gateEl(), gate, 'a disabled Continue does nothing');
  // Download starts, is polled, and finishes.
  click(gate.querySelector('[data-provider="claude"][data-ac-action]')); await flush();
  assert.equal(gate.querySelector('[data-ac-row="claude"]').dataset.state, 'downloading');
  assert.equal(next.disabled, true);
  current = finished;
  await tick.fire();
  assert.equal(next.disabled, false);
  assert.equal(hint.textContent, 'You are set. Continue to your workspace.');
  assert.equal(later.hidden, true);
  assert.equal(doc.activeElement, next);
  assert.match(gate.querySelector('[data-ac-row="claude"] .ac-note').textContent, /from their next session on/);
  click(next);
  assert.equal(await result, finished);
  assert.equal(gateEl(), undefined);
  assert.equal(app.inert, false, 'the page is reachable again');
  assert.equal(other.inert, true, 'something that was already inert is left alone');
  assert.equal(doc.listeners.visibilitychange.length, 0, 'the rows stopped');
  assert.equal(await tick.fire(), false);
  assert.equal(visited['qorqbot:agent-setup-later'], undefined, 'continuing is not skipping');
});

test('Set up later resolves, remembers the choice for this session and restores the page', async () => {
  const [app] = freshPage('app');
  const first = view();
  const result = clients.preflight({read: async () => first, schedule: () => 1, cancel() {}});
  await flush();
  const gate = gateEl();
  assert.equal(app.inert, true);
  click(gate.querySelector('[data-ac-later]'));
  assert.equal(await result, first);
  assert.equal(gateEl(), undefined);
  assert.equal(app.inert, false);
  assert.equal(visited['qorqbot:agent-setup-later'], '1');
  assert.equal(await clients.preflight({read: async () => { throw new Error('not asked again'); }}), null);
});

test('the screen is also dismissed only once: a second press does not resolve again or restore twice', async () => {
  const [app] = freshPage('app');
  const result = clients.preflight({read: async () => view(), schedule: () => 1, cancel() {}});
  await flush();
  const later = gateEl().querySelector('[data-ac-later]');
  click(later); click(later);
  await result;
  app.inert = true;
  click(later);
  assert.equal(app.inert, true, 'a finished screen does not touch the page again');
});

// ---- where the component is mounted (the real lines of settings.js and main.js, run with stand-ins)

test('Settings mounts the rows into the Agents section and tells the search filter when they paint', () => {
  const settings = read('js/settings.js');
  const start = settings.indexOf('function paintAgentClients(pane)');
  const body = settings.slice(start, settings.indexOf('\n}\n', start) + 3);
  assert.ok(start > 0);
  const [pane] = freshPage('pane');
  const host = doc.createElement('div'); host.dataset.agentClients = ''; pane.append(host);
  const painted = [];
  pane.addEventListener('settings-painted', () => painted.push(1));
  const mounted = [];
  const fake = {mount: (el, options) => { const panel = clients.mount(el, {view: view({...installed}), ...options}); mounted.push(panel); return panel; }};
  const paint = new Function('agentClients', body + '; return paintAgentClients;')(fake);
  paint(pane);
  assert.equal(mounted.length, 1);
  assert.equal(host.querySelector('[data-ac-row="claude"]').dataset.state, 'connect');
  mounted[0].root.dispatchEvent({type: 'agent-clients-painted', bubbles: true});
  assert.equal(painted.length, 1);
  paint(doc.createElement('div')); // no host: nothing happens
  assert.equal(mounted.length, 1);
  mounted[0].stop();
  assert.ok(!/terminal/i.test(settings), 'no terminal wording in Settings');
  assert.ok(!settings.includes('Starting an agent') && !settings.includes('agent-trust'), 'no start row and no folder trust');
  assert.ok(!settings.includes("coreData(await api('/root/agent-clients'))"), 'Settings has no read of its own');
  assert.match(settings, /data-agent-clients><\/div>/);
});

test('main.js starts the walkthrough after the agent screen whatever that screen does', async () => {
  const main = read('js/main.js');
  const line = main.split('\n').find(text => text.startsWith('loadBiz().then('));
  assert.ok(line && line.includes('agentClients.preflight()') && line.includes('onboarding.boot()'));
  const order = [];
  const run = async agentClients => {
    order.length = 0;
    const chain = new Function('loadBiz', 'accountGate', 'agentClients', 'onboarding',
      'return ' + line.replace(/;\s*$/, ''))(
      async () => order.push('biz'), {boot: async () => { order.push('account'); }}, agentClients, {boot: () => order.push('walkthrough')});
    await chain;
    return order.join(',');
  };
  assert.equal(await run({preflight: async () => { order.push('agents'); return null; }}), 'biz,account,agents,walkthrough');
  assert.equal(await run({preflight: () => Promise.reject(new Error('boom'))}), 'biz,account,walkthrough');
  assert.equal(await run({preflight: () => { throw new Error('threw'); }}), 'biz,account,walkthrough');
  assert.equal(await run(undefined), 'biz,account,walkthrough', 'even when the script did not load');
  assert.equal(await run({preflight: () => new Promise(resolve => setTimeout(() => resolve(null), 20))}), 'biz,account,walkthrough');
});

test('the page loads the script after the account gate and before main, with its stylesheet', () => {
  const page = read('index.html');
  assert.ok(page.indexOf('/js/agent-clients.js') > page.indexOf('/js/account-gate.js') && page.indexOf('/js/agent-clients.js') < page.indexOf('/js/main.js'));
  assert.match(page, /href="\/css\/agent-clients\.css"/);
});

test('the styles cover the light theme, reduced motion and the narrow layout', () => {
  const css = read('css/agent-clients.css');
  assert.match(css, /:root\[data-theme="light"\] \.ac-gate/);
  assert.match(css, /prefers-reduced-motion: reduce/);
  assert.match(css, /max-width: 620px/);
  assert.match(css, /\.ac-actions button:focus-visible/);
});
