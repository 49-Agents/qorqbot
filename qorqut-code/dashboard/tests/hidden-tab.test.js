'use strict';
// node --test qorqut-code/dashboard/tests/hidden-tab.test.js
// A tab that is not visible asks the server for nothing the person could perceive from outside it. The reads behind a desktop
// notification (replies, reminders, signoffs) keep their pace while that kind is on for this browser; everything else stops, and
// the return to the tab reads once, however many triggers (visibility, focus, held-back events) arrive together.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = name => fs.readFileSync(path.join(__dirname, '../js', name), 'utf8');
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };

// A page: a fake clock and timers, a document whose visibility the test flips, the real store.js and desktop-notify.js, and the
// requests every `api`/`post` call records. `window` is the context itself, as in the browser.
function page({notify = {}, extra = {}} = {}) {
  let now = 1_000_000, seq = 0;
  const timers = new Map(), listeners = {document: {}, window: {}}, requests = [];
  const add = (where, type, fn) => { (listeners[where][type] ||= []).push(fn); };
  const fire = (where, type) => (listeners[where][type] || []).slice().forEach(fn => fn({}));
  const document = {hidden: false, addEventListener: (t, f) => add('document', t, f), removeEventListener() {}, dispatchEvent() {},
    querySelectorAll: () => [], querySelector: () => null, createElement: () => ({}), hasFocus: () => true, body: {append() {}}};
  const Notification = Object.assign(function () {}, {permission: notify.permission || 'default'});
  const context = {document, Date: {now: () => now}, JSON, Promise, Map, Set, Object, Array, Math, String, Number, Event: class {},
    URLSearchParams, Intl, console, Notification,
    addEventListener: (t, f) => add('window', t, f), removeEventListener() {},
    localStorage: {getItem: () => JSON.stringify(notify.prefs || {}), setItem() {}},
    sessionStorage: {getItem: () => null, setItem() {}},
    setTimeout: (fn, ms) => { timers.set(++seq, {fn, at: now + ms}); return seq; },
    setInterval: (fn, ms) => { timers.set(++seq, {fn, at: now + ms, every: ms}); return seq; },
    clearTimeout: id => timers.delete(id), clearInterval: id => timers.delete(id),
    coreData: x => x, accessLost: () => false, esc: String, notify() {}, ...extra};
  context.window = context;
  vm.createContext(context);
  const run = (...names) => names.forEach(name => vm.runInContext(source(name), context));
  run('store.js', 'desktop-notify.js');
  const advance = async ms => {
    const end = now + ms;
    for (;;) {
      const due = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      const [id, timer] = due; now = timer.at;
      if (timer.every) timer.at += timer.every; else timers.delete(id);
      timer.fn(); await flush();
    }
    now = end;
  };
  // Back on the tab: the visibility change, then the window focus, as the browser raises them.
  const setHidden = async hidden => { document.hidden = hidden; fire('document', 'visibilitychange'); if (!hidden) fire('window', 'focus'); await flush(); };
  return {context, document, run, advance, setHidden, requests, fire, get now() { return now; }};
}
const count = (p, url) => p.requests.filter(r => r.startsWith(url)).length;
const notifying = kind => ({permission: 'granted', prefs: {[kind]: true}});

// ---------------------------------------------------------------- the shared helpers
test('tabHiddenFor is false while visible, true while hidden, and false for a hidden kind that desktop notifications keep alive', () => {
  const p = page({notify: notifying('replies')});
  assert.equal(p.context.tabHiddenFor('signoffs'), false);
  p.document.hidden = true;
  assert.equal(p.context.tabHiddenFor(), true);
  assert.equal(p.context.tabHiddenFor('signoffs'), true, 'signoffs are off for this browser');
  assert.equal(p.context.tabHiddenFor('replies'), false, 'replies are on and allowed');
  const refused = page({notify: {permission: 'denied', prefs: {replies: true}}});
  refused.document.hidden = true;
  assert.equal(refused.context.tabHiddenFor('replies'), true, 'the browser does not allow notifications');
});

test('visibleInterval does not run while hidden and runs on its pace again when the tab is back', async () => {
  const p = page();
  let runs = 0;
  p.context.visibleInterval(() => runs++, 60000);
  await p.advance(120000); assert.equal(runs, 2);
  await p.setHidden(true); await p.advance(300000); assert.equal(runs, 2);
  await p.setHidden(false); await p.advance(60000); assert.equal(runs, 3);
});

test('onTabBack runs once for the visibility change and the focus of one return, and again for every later return', async () => {
  const p = page();
  let runs = 0;
  p.context.onTabBack(() => runs++);
  await p.setHidden(true); assert.equal(runs, 0, 'never while hidden');
  await p.setHidden(false); assert.equal(runs, 1);
  await p.setHidden(true); await p.setHidden(false);
  assert.equal(runs, 2, 'a quick second return is a return of its own');
});

test('onTabBack: a window focus with the tab visible all along runs the handler, and two within a second run it once', async () => {
  const p = page();
  let runs = 0;
  p.context.onTabBack(() => runs++);
  p.fire('window', 'focus'); assert.equal(runs, 1);
  p.fire('window', 'focus'); assert.equal(runs, 1);
  await p.advance(1001);
  p.fire('window', 'focus'); assert.equal(runs, 2);
});

test('onTabBack with focus off listens to the visibility change only', async () => {
  const p = page();
  let runs = 0;
  p.context.onTabBack(() => runs++, {focus: false});
  p.fire('window', 'focus'); assert.equal(runs, 0);
  await p.setHidden(true); await p.setHidden(false); assert.equal(runs, 1);
});

// ---------------------------------------------------------------- reply notifications
function replyPage(notify) {
  const p = page({notify, extra: {
    api: async url => { p.requests.push(url); return {items: []}; },
    loadGlobalAgentProfiles: async () => { p.requests.push('/root/agents'); }, primaryAgent: () => true, globalAgentProfile: () => null}});
  p.run('reply-notifications.js');
  vm.runInContext('this.replies = replyNotifications', p.context);
  p.context.replies.start();
  return p;
}

test('reply notifications: a hidden tab with replies off asks for nothing and one return asks once', async () => {
  const p = replyPage();
  await flush();
  assert.equal(count(p, '/root/reply-notifications'), 1);
  await p.setHidden(true);
  p.requests.length = 0;
  await p.advance(600000);
  assert.deepEqual(p.requests, []);
  await p.setHidden(false);
  assert.equal(count(p, '/root/reply-notifications'), 1, 'one read for the visibility change and the focus together');
  assert.equal(count(p, '/root/agents'), 1);
  await p.advance(10000);
  assert.equal(count(p, '/root/reply-notifications'), 2, 'the visible pace is back');
});

test('reply notifications: with replies on for desktop notifications a hidden tab still reads every 30 seconds', async () => {
  const p = replyPage(notifying('replies'));
  await flush();
  await p.setHidden(true);
  p.requests.length = 0;
  await p.advance(120000);
  assert.equal(count(p, '/root/reply-notifications'), 4);
});

// ---------------------------------------------------------------- reminders
function reminderPage(notify) {
  class Node { constructor() { this.style = {setProperty() {}}; this.classList = {add() {}, remove() {}}; this.isConnected = true; } addEventListener() {} setAttribute() {} appendChild() {} remove() {} }
  const p = page({notify, extra: {
    embeddedAgentPane: false, state: {session: {principal_id: 'owner'}, projects: []}, loadSession: async () => ({principal_id: 'owner', principal_kind: 'human', is_owner: true}),
    post: async url => { p.requests.push(url); return {reminders: []}; }, api: async () => ({}), requestAnimationFrame() {}, $: () => new Node(), agentGlyph: () => ''}});
  p.run('relative-time.js', 'reminders.js');
  return p;
}

test('reminders: a hidden tab with reminders off asks for nothing and the return polls once', async () => {
  const p = reminderPage();
  await flush();
  assert.equal(count(p, '/root/operations/reminders_list'), 1);
  await p.setHidden(true);
  p.requests.length = 0;
  await p.advance(600000);
  assert.deepEqual(p.requests, []);
  await p.setHidden(false);
  assert.equal(count(p, '/root/operations/reminders_list'), 1);
  await p.advance(30000);
  assert.equal(count(p, '/root/operations/reminders_list'), 2);
});

test('reminders: with reminders on for desktop notifications a hidden tab still polls every 30 seconds', async () => {
  const p = reminderPage(notifying('reminders'));
  await flush();
  await p.setHidden(true);
  p.requests.length = 0;
  await p.advance(120000);
  assert.equal(count(p, '/root/operations/reminders_list'), 4);
});

// ---------------------------------------------------------------- signoff attention strip
function attentionPage(notify) {
  const p = page({notify, extra: {
    api: async url => { p.requests.push(url); return {items: []}; }, state: {scope: 'global', projects: []}, SVG_X: '', toast() {}, sgCreateView() {}, ctxMenu() {}}});
  p.run('signoff-attention.js');
  return p;
}

test('signoff strip: a hidden tab with signoffs off reads nothing; the return reads once even with a held-back event', async () => {
  const p = attentionPage();
  await flush();
  assert.equal(count(p, '/me/attention'), 2, 'the first read is the overview and the deliveries');
  await p.setHidden(true);
  p.requests.length = 0;
  await p.advance(600000);
  assert.deepEqual(p.requests, []);
  p.context.storeNotify('signoffs', null);          // an event held back while hidden
  await p.advance(1000);
  assert.deepEqual(p.requests, []);
  p.requests.length = 0;
  await p.setHidden(false);
  await p.advance(1000);
  assert.equal(count(p, '/me/attention'), 4, 'the return reads overview and deliveries once, and the held event one trailing read');
});

test('signoff strip: with signoffs on for desktop notifications a hidden tab still reads every 60 seconds', async () => {
  const p = attentionPage(notifying('signoffs'));
  await flush();
  await p.setHidden(true);
  p.requests.length = 0;
  await p.advance(180000);
  assert.equal(count(p, '/me/attention'), 6);
});

// ---------------------------------------------------------------- Focus attention queue and strip
function queuePage({mount = true} = {}) {
  const host = () => ({isConnected: true, innerHTML: '', addEventListener() {}, removeEventListener() {}, contains: () => false, querySelectorAll: () => [], querySelector: () => null});
  const p = page({extra: {
    api: async url => { p.requests.push(url); return {items: []}; }, post: async () => ({items: []}), loadSession: async () => ({}),
    toast() {}, AbortController, focusExpansion: {async open() { return {}; }}, state: {session: {principal_id: 'owner'}, projects: []}, getComputedStyle: () => ({}),
    embeddedAgentPane: true, requestAnimationFrame() {}, $: () => host(), agentGlyph: () => ''}});
  p.run('relative-time.js', 'reminders.js', 'attention-queue.js');
  vm.runInContext('this.queue = attentionQueue', p.context);
  p.mountViews = () => { p.context.queue.mount(host()); p.context.queue.mountSignoffs(host(), () => '', () => {}); };
  if (mount) p.mountViews();
  return p;
}

test('Focus queue and strip: a hidden tab reads nothing and the return reads each once', async () => {
  const p = queuePage();
  await flush();
  assert.equal(count(p, '/root/attention-queue'), 1);
  assert.equal(count(p, '/root/signoffs'), 1);
  await p.setHidden(true);
  p.requests.length = 0;
  await p.advance(600000);
  assert.deepEqual(p.requests, [], 'neither the 10 s queue timer nor the 60 s strip timer reads while hidden');
  await p.setHidden(false);
  await p.advance(1000);
  assert.equal(count(p, '/root/attention-queue'), 1, 'the visibility change and the focus share one read');
  assert.equal(count(p, '/root/signoffs'), 1);
});

test('Focus queue and strip: events held back while hidden add one trailing read each, as they always did', async () => {
  const p = queuePage();
  await flush();
  await p.setHidden(true);
  await p.advance(600000);
  p.context.storeNotify('signoffs', null);
  await p.advance(1000);
  p.requests.length = 0;
  await p.setHidden(false);
  await p.advance(1000);
  assert.equal(count(p, '/root/attention-queue'), 2);
  assert.equal(count(p, '/root/signoffs'), 2);
});

test('Focus queue and strip: an event that arrives right after a return is read, not dropped', async () => {
  const p = queuePage();
  await flush();
  await p.setHidden(true); await p.advance(600000); await p.setHidden(false); await p.advance(300);
  p.requests.length = 0;
  p.context.storeNotify('signoffs', null);
  await p.advance(400);
  assert.equal(count(p, '/root/attention-queue'), 1);
  assert.equal(count(p, '/root/signoffs'), 1);
});

// ---------------------------------------------------------------- every periodic read comes back after a long hide
const away = async p => { await p.setHidden(true); await p.advance(300000); await p.setHidden(false); await p.advance(1000); p.requests.length = 0; };

test('after five minutes hidden the Focus queue is read every 10 s again and the strip every 60 s', async () => {
  const p = queuePage();
  await flush(); await away(p);
  await p.advance(60000);
  assert.equal(count(p, '/root/attention-queue'), 6);
  assert.equal(count(p, '/root/signoffs'), 1);
});

test('a tab that returns while no Focus view is mounted starts the reads when a view mounts later', async () => {
  const p = queuePage({mount: false});
  await flush(); await away(p);
  await p.advance(120000);
  assert.deepEqual(p.requests, []);
  p.mountViews(); await flush();
  assert.equal(count(p, '/root/attention-queue'), 1);
  p.requests.length = 0;
  await p.advance(60000);
  assert.equal(count(p, '/root/attention-queue'), 6);
  assert.equal(count(p, '/root/signoffs'), 1);
});

test('a view that mounts while the tab is hidden reads when the tab is back and keeps its pace', async () => {
  const p = queuePage({mount: false});
  await flush();
  await p.setHidden(true); await p.advance(300000);
  p.mountViews(); await flush();
  assert.deepEqual(p.requests, []);
  await p.setHidden(false); await p.advance(1000);
  assert.equal(count(p, '/root/attention-queue'), 1);
  p.requests.length = 0;
  await p.advance(60000);
  assert.equal(count(p, '/root/attention-queue'), 6);
});

test('after five minutes hidden the signoff strip is read every 60 s again', async () => {
  const p = attentionPage();
  await flush(); await away(p);
  await p.advance(120000);
  assert.equal(count(p, '/me/attention'), 4);
});

test('after five minutes hidden reply notifications are read every 10 s again', async () => {
  const p = replyPage();
  await flush(); await away(p);
  await p.advance(30000);
  assert.equal(count(p, '/root/reply-notifications'), 3);
});

test('after five minutes hidden reminders are polled every 30 s again', async () => {
  const p = reminderPage();
  await flush(); await away(p);
  await p.advance(90000);
  assert.equal(count(p, '/root/operations/reminders_list'), 3);
});

// ---------------------------------------------------------------- Context tab
test('after five minutes hidden the Context tab is read every 60 s again, and a return with another tab open reads nothing', async () => {
  const text = source('context.js');
  const start = text.indexOf('let _ctxHeld = null;'), end = text.indexOf('// The footer speaks only about the Context queue');
  assert.ok(start > 0 && end > start);
  const p = page({extra: {state: {tab: 'context', project: 'p1', ctx: {}}, isTopic: () => false, ctxDataFits: () => true, ctxPaint() {}, ctxQueueRefresh() {},
    ctxSig: () => '', ctxShow() {}, ctxLoadData: async () => { p.requests.push('ctx'); return {}; }, _ctxTimer: null, _ctxUnwatch: null, _ctxLastSig: ''}});
  p.context.$ = () => ({isConnected: true});
  vm.runInContext(text.slice(start, end), p.context);
  p.context.ctxWatch();
  await p.advance(60000);
  assert.equal(count(p, 'ctx'), 1);
  await p.setHidden(true); await p.advance(300000);
  assert.equal(count(p, 'ctx'), 1, 'nothing while hidden');
  await p.setHidden(false); await flush();
  assert.equal(count(p, 'ctx'), 2, 'one read on the return');
  await p.advance(120000);
  assert.equal(count(p, 'ctx'), 4, 'and the 60 s pace is back');
  // Another tab is open when the tab returns: the held read does nothing, and the chain ends as it does on any tab change.
  await p.setHidden(true); await p.advance(300000);
  p.context.state.tab = 'tasks';
  const before = count(p, 'ctx');
  await p.setHidden(false); await flush();
  assert.equal(count(p, 'ctx'), before);
});
