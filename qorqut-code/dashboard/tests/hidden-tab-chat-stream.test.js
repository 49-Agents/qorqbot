'use strict';
// node --test qorqut-code/dashboard/tests/hidden-tab-chat-stream.test.js
// A hidden tab holds no chat stream: after a grace period the stream closes (so the server stops its loop for it), and the return
// reads the chat state and reopens the stream from that state's cursor (js/global-chat.js `connect`, `closeStream`).
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const code = fs.readFileSync(path.join(__dirname, '../js/global-chat.js'), 'utf8');
const between = (from, to) => {
  const start = code.indexOf(from), end = code.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `global-chat.js no longer contains ${from}`);
  return code.slice(start, end);
};
const streamCode = between('  function closeStream()', '  // A managed agent\'s activity changes often.') + between('  function connect() {', '  function takeoverShell');

function chat({hidden = false} = {}) {
  let now = 0, seq = 0;
  const timers = new Map(), sources = [], loads = [], listeners = [];
  const document = {hidden, addEventListener: (type, fn) => type === 'visibilitychange' && listeners.push(fn)};
  class FakeSource {
    constructor(url) { this.url = url; this.closed = false; this.handlers = {}; sources.push(this); }
    addEventListener(type, fn) { this.handlers[type] = fn; }
    close() { this.closed = true; }
  }
  FakeSource.CLOSED = 2;
  const env = {
    document, EventSource: FakeSource, evUrl: url => url, base: '/root/agents/a1/chat', setTimeout: (fn, ms) => { timers.set(++seq, {fn, at: now + ms}); return seq; },
    clearTimeout: id => timers.delete(id), live: () => true, paintSession() {}, showBanner() {}, showManagedEvent() {}, invalidatePage() {}, listen: (target, type, fn) => target.addEventListener(type, fn),
  };
  const names = Object.keys(env);
  const body = `
    let stream = null, retryTimer = null, retryDelay = 1000, streamDown = false, parked = false, parkTimer = null, finished = false,
        bannerText = '', reconnecting = false, cursor = 0, generation = 0, loading = false, again = false, lastRead = 0;
    async function load() { env.loads.push(cursor); cursor = Math.max(cursor, 7); if (!stream) connect(); }
    ${streamCode}
    return {get stream() { return stream; }, get parked() { return parked; }, connect, load, setFinished(v) { finished = v; }, get cursor() { return cursor; }};`;
  const parts = new Function('env', ...names, body)({loads}, ...names.map(name => env[name]));
  const advance = ms => {
    const end = now + ms;
    for (;;) {
      const due = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      timers.delete(due[0]); now = due[1].at; due[1].fn();
    }
    now = end;
  };
  const setHidden = value => { document.hidden = value; listeners.forEach(fn => fn()); };
  return Object.assign(parts, {sources, loads, advance, setHidden, document});
}
const settle = () => new Promise(resolve => setImmediate(resolve));
const open = c => c.sources.filter(source => !source.closed);

test('the stream stays open through a short switch to another tab', () => {
  const c = chat(); c.connect();
  assert.equal(open(c).length, 1);
  c.setHidden(true); c.advance(29000);
  c.setHidden(false); c.advance(60000);
  assert.equal(open(c).length, 1);
  assert.equal(c.sources.length, 1, 'no connection was opened or closed');
  assert.deepEqual(c.loads, [], 'and nothing was re-read');
});

test('a tab hidden for the grace period closes the stream, and the return reads the state once and reopens it from the state cursor', async () => {
  const c = chat(); c.connect();
  c.setHidden(true); c.advance(30000);
  assert.equal(open(c).length, 0);
  assert.equal(c.parked, true);
  c.advance(600000);
  assert.equal(c.sources.length, 1, 'nothing reconnects while hidden');
  c.setHidden(false); await settle();
  assert.deepEqual(c.loads, [0], 'one state read on the return');
  assert.equal(open(c).length, 1);
  assert.equal(open(c)[0].url, '/root/agents/a1/chat/events?cursor=7', 'reopened from the cursor the state read returned');
  assert.equal(c.parked, false);
});

test('a retry or a read that lands while the tab is hidden does not reopen the stream', async () => {
  const c = chat(); c.connect();
  c.setHidden(true); c.advance(30000);
  await c.load();
  c.connect();
  assert.equal(open(c).length, 0);
  c.setHidden(false); await settle();
  assert.equal(open(c).length, 1);
});

test('a chat that mounts in a hidden tab opens no stream until the tab is back', async () => {
  const c = chat({hidden: true}); c.connect();
  assert.equal(c.sources.length, 0);
  c.setHidden(false); await settle();
  assert.equal(open(c).length, 1);
});

test('a finished chat is not reopened on the return', async () => {
  const c = chat(); c.connect();
  c.setHidden(true); c.advance(30000);
  c.setFinished(true);
  c.setHidden(false); await settle();
  assert.deepEqual(c.loads, []);
  assert.equal(open(c).length, 0);
});
