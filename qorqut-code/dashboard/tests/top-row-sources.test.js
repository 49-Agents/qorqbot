'use strict';
// node --test qorqut-code/dashboard/tests/top-row-sources.test.js
// The top row reads three sources (Signoffs, due reminders, calendar). Each keeps its own single read in flight and its own one
// trailing read, and paints when it finishes: a slow calendar never holds back a Signoff chip.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = name => fs.readFileSync(path.join(__dirname,'../js',name),'utf8');
const flush = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };
const NAMES = ['signoffs','reminders','calendar'];

// The real attention-queue.js with the dashboard globals faked: a clock and timers the test advances, reads that can be held
// open per source, and the handlers the file registers (tab return, `signoffs` change, calendar change).
function rig() {
  let now = 1_000_000, seq = 0;
  const timers = new Map(), handlers = {back:[], change:[]}, listeners = {}, calendarChanges = new Set(), toasts = [];
  const reads = {signoffs:0,reminders:0,calendar:0}, active = {signoffs:0,reminders:0,calendar:0}, maxActive = {signoffs:0,reminders:0,calendar:0};
  const holds = new Set(), gates = {signoffs:[],reminders:[],calendar:[]}, fail = new Set();
  const data = {signoffs:[{id:'s1',title:'One',state:'pending',operation_name:'x',proposal_revision:1,payload_hash:'h'}]};
  let maxTimers = 0;
  async function read(name, result) {
    reads[name]++; active[name]++; maxActive[name] = Math.max(maxActive[name], active[name]);
    try {
      if (holds.has(name)) await new Promise(resolve => gates[name].push(resolve));
      if (fail.has(name)) throw new Error(name + ' down');
      return result();
    } finally { active[name]--; }
  }
  class FakeDate extends Date { static now() { return now; } }
  const context = {Date:FakeDate, Promise, Map, Set, URLSearchParams, AbortController, JSON, Intl, esc:value => String(value ?? '').replace(/"/g,'&quot;'),
    coreData:x => x, accessLost:() => false, document:{hidden:false, addEventListener(){}}, window:{addEventListener(){}}, T_VIEW_ICON:{calendar:''},
    api:async url => url.startsWith('/root/signoffs') ? read('signoffs', () => ({items:data.signoffs.map(s => ({...s})), next_cursor:null})) : {items:[]},
    post:async () => ({items:[]}), calendarChanges, remVerifiedOwner:async () => 'owner', remAttempts:new Map(), toast:text => toasts.push(text),
    remindersOwnerDue:() => read('reminders', () => []),
    calendarQueueLoad:() => read('calendar', () => ({items:[],calendars:[],errors:[]})),
    setTimeout:(fn, ms) => { timers.set(++seq, {fn, at:now + ms}); maxTimers = Math.max(maxTimers, timers.size); return seq; },
    clearTimeout:id => timers.delete(id), onTabBack:fn => handlers.back.push(fn), onChange:(kinds, scope, fn) => handlers.change.push({kinds, fn}), tabHiddenFor:() => false};
  vm.createContext(context);
  vm.runInContext(source('attention-queue.js') + '\nthis.queue = attentionQueue;', context);
  const host = () => ({isConnected:true, innerHTML:'', hidden:true, addEventListener(name, fn){listeners[name] = fn;}, removeEventListener(){}, contains:() => false});
  const advance = async ms => {
    const end = now + ms;
    for (;;) {
      const due = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      now = due[1].at; timers.delete(due[0]); due[1].fn(); await flush();
    }
    now = end;
  };
  const release = name => { gates[name].splice(0).forEach(resolve => resolve()); };
  return {context, host, advance, release, holds, fail, data, reads, active, maxActive, handlers, listeners, calendarChanges, toasts, timers,
    get maxTimers() { return maxTimers; },
    changeSignoffs: () => handlers.change.filter(h => h.kinds.includes('signoffs')).forEach(h => h.fn()),
    tabBack: () => handlers.back.forEach(fn => fn()),
    chip: key => ({dataset:{signoffKey:key}, closest(selector){ return selector.split(',').some(part => part.trim() === '[data-signoff-key]') ? this : null; }}),
    button: attribute => ({dataset:{}, closest(selector){ return selector.split(',').some(part => part.trim() === attribute) ? this : null; }})};
}
const counts = r => NAMES.map(name => r.reads[name]);

// ---------------------------------------------------------------- behaviour that must not change
test('Signoff chips are painted from a completed Signoff read', async () => {
  const r = rig(), host = r.host();
  r.context.queue.mountSignoffs(host); await flush();
  assert.match(host.innerHTML, /data-signoff-key="s1"/);
  assert.equal(host.hidden, false);
});

test('a failed source shows its retry button and the button reads again', async () => {
  for (const [name, attribute, pattern] of [['signoffs','data-signoffs-retry',/data-signoffs-retry/],['reminders','data-reminders-retry',/data-reminders-retry/],['calendar','data-calendar-retry',/data-calendar-retry/]]) {
    const r = rig(), host = r.host();
    r.fail.add(name);
    r.context.queue.mountSignoffs(host); await flush();
    assert.match(host.innerHTML, pattern, name);
    const before = r.reads[name];
    r.fail.delete(name);
    r.listeners.click({target:r.button('[' + attribute + ']'), currentTarget:host}); await flush();
    assert.equal(r.reads[name], before + 1, name);
    assert.doesNotMatch(host.innerHTML, pattern, name);
  }
});

test('a hidden tab reads nothing, however it is triggered', async () => {
  const r = rig(), host = r.host();
  r.context.document.hidden = true;
  r.context.queue.mountSignoffs(host); await flush();
  r.changeSignoffs(); r.tabBack(); r.calendarChanges.forEach(fn => fn()); await flush();
  await r.advance(300000);
  assert.deepEqual(counts(r), [0,0,0]);
});

test('a mount within 60 seconds of a success does not read again', async () => {
  const r = rig();
  r.context.queue.mountSignoffs(r.host()); await flush();
  assert.deepEqual(counts(r), [1,1,1]);
  r.context.queue.mountSignoffs(r.host()); await flush();
  assert.deepEqual(counts(r), [1,1,1]);
});

test('repeated triggers during a Signoff read give exactly one trailing Signoff read', async () => {
  const r = rig();
  r.holds.add('signoffs');
  r.context.queue.mountSignoffs(r.host()); await flush();
  assert.equal(r.reads.signoffs, 1);
  r.changeSignoffs(); r.changeSignoffs(); r.tabBack(); void r.context.queue.refreshSignoffs(); await flush();
  assert.equal(r.reads.signoffs, 1, 'nothing starts while one is in flight');
  r.holds.delete('signoffs'); r.release('signoffs'); await flush();
  assert.equal(r.reads.signoffs, 2);
  assert.equal(r.maxActive.signoffs, 1);
});

test('refreshSignoffs settles when its reads have settled', async () => {
  const r = rig();
  r.context.queue.mountSignoffs(r.host()); await flush();
  r.holds.add('signoffs'); r.holds.add('calendar');
  let done = false;
  void r.context.queue.refreshSignoffs().then(() => { done = true; });
  await flush(); assert.equal(done, false);
  r.holds.clear(); r.release('signoffs'); await flush(); assert.equal(done, false, 'the calendar read is still open');
  r.release('calendar'); await flush(); assert.equal(done, true);
});
