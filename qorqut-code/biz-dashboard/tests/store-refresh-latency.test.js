'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync(require.resolve('../js/store.js'), 'utf8');

function harness() {
  let now = 0, seq = 0; const timers = new Map(), listeners = [];
  const document = {hidden: false, addEventListener: (t, f) => t === 'visibilitychange' && listeners.push(f)};
  const context = {document, Date: {now: () => now},
    setTimeout: (fn, ms) => { const id = ++seq; timers.set(id, {fn, at: now + ms}); return id; },
    clearTimeout: id => timers.delete(id)};
  vm.createContext(context);
  vm.runInContext(code + ';this.onChange=onChange;this.storeNotify=storeNotify;this.storeInvalidateAll=storeInvalidateAll;', context);
  const advance = ms => {
    const end = now + ms;
    for (;;) {
      const next = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      timers.delete(next[0]); now = next[1].at; next[1].fn();
    }
    now = end;
  };
  const setHidden = h => { document.hidden = h; if (!h) listeners.forEach(f => f()); };
  return {context, advance, setHidden, timers, get now() { return now; }};
}

test('a short burst fires once, 250ms after the last event', () => {
  const h = harness(); let calls = 0;
  h.context.onChange(['task'], null, () => calls++);
  h.context.storeNotify('task', 'b'); h.advance(100); h.context.storeNotify('task', 'b'); h.advance(100); h.context.storeNotify('task', 'b');
  h.advance(249); assert.equal(calls, 0);
  h.advance(1); assert.equal(calls, 1);
  h.advance(5000); assert.equal(calls, 1);
});

test('a continuous stream refreshes within 1000ms and keeps refreshing', () => {
  const h = harness(); const at = [];
  h.context.onChange(['task'], null, () => at.push(h.now));
  for (let i = 0; i < 40; i++) { h.context.storeNotify('task', null); h.advance(100); }
  assert.ok(at.length >= 3, String(at));
  assert.ok(at[0] <= 1000);
  for (let i = 1; i < at.length; i++) assert.ok(at[i] - at[i - 1] <= 1100);
});

test('subscriptions and scopes stay separate', () => {
  const h = harness(); const calls = {a: 0, b: 0};
  h.context.onChange(['task'], 'a', () => calls.a++); h.context.onChange(['task'], 'b', () => calls.b++);
  h.context.storeNotify('task', 'a'); h.advance(250);
  assert.deepEqual({...calls}, {a: 1, b: 0});
  h.context.storeNotify('task', null); h.advance(250);
  assert.deepEqual({...calls}, {a: 2, b: 1});
});

test('hiding before the timer suppresses the read and visibility resumes once', () => {
  const h = harness(); let calls = 0;
  h.context.onChange(['task'], null, () => calls++);
  h.context.storeNotify('task', null); h.context.storeNotify('task', null);
  h.advance(100); h.setHidden(true);
  h.advance(5000); assert.equal(calls, 0); assert.equal(h.timers.size, 0);
  h.setHidden(false); assert.equal(calls, 0);
  h.advance(249); assert.equal(calls, 0);
  h.advance(1); assert.equal(calls, 1); h.advance(5000); assert.equal(calls, 1);
});

test('a timer that fires after visibility changed does not run the handler', () => {
  const h = harness(); let calls = 0;
  h.context.onChange(['task'], null, () => calls++);
  h.context.storeNotify('task', null);
  h.context.document.hidden = true; h.advance(250);
  assert.equal(calls, 0);
  h.setHidden(false); h.advance(250); assert.equal(calls, 1);
});

test('unsubscribe cancels timers and pending work', () => {
  const h = harness(); let calls = 0;
  const off = h.context.onChange(['task'], null, () => calls++);
  h.context.storeNotify('task', null); off(); h.advance(2000); assert.equal(calls, 0);
  const off2 = h.context.onChange(['task'], null, () => calls++);
  h.setHidden(true); h.context.storeNotify('task', null); off2(); h.setHidden(false); h.advance(2000);
  assert.equal(calls, 0); assert.equal(h.timers.size, 0);
});

test('an event raised inside the handler starts a new bounded batch', () => {
  const h = harness(); let calls = 0;
  h.context.onChange(['task'], null, () => { if (++calls === 1) h.context.storeNotify('task', null); });
  h.context.storeNotify('task', null); h.advance(250); assert.equal(calls, 1);
  h.advance(249); assert.equal(calls, 1); h.advance(1); assert.equal(calls, 2);
});
