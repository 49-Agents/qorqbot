'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = name => fs.readFileSync(path.join(__dirname,'../js',name),'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

test('calendar events join the queue an hour before they start and keep a stable discard key', async () => {
  const calls = [], base = {connection_id:'conn',calendar_id:'cal',revision:'1',recurrence:[],attendees:[]};
  const events = [
    {...base,id:'soon',title:'Soon',start:'2026-11-11T03:30:00Z',end:'2026-11-11T04:00:00Z'},
    {...base,id:'earlier',title:'Earlier',start:'2026-11-10T20:00:00Z',end:'2026-11-10T21:00:00Z'},
    {...base,id:'later',title:'Later',start:'2026-11-11T05:00:00Z',end:'2026-11-11T06:00:00Z'},
    {...base,id:'holiday',title:'Veterans Day',start:'2026-11-11',end:'2026-11-12'},
    {...base,id:'gone',title:'Cancelled',status:'cancelled',start:'2026-11-11T03:00:00Z',end:'2026-11-11T04:00:00Z'},
  ];
  const context = {Date, Intl, JSON, Promise, Map, Set, URLSearchParams, esc:String, coreData:x => x, accessLost:() => false, clearEtagCache(){},
    document:{addEventListener(){},removeEventListener(){}}, window:{}, AICONS:{}, T_VIEW_ICON:{calendar:''}, newRequestId:() => 'key',
    loadSession:async () => ({}), api:async () => ({items:[]}), $:() => null,
    post:async (url, args) => {
      calls.push({url,args});
      if (url.endsWith('calendar_calendars_list')) return {configured:true,errors:[],calendars:[{connection_id:'conn',id:'cal',name:'Work',writable:true,organizers:[]}]};
      if (url.endsWith('calendar_events_list')) return {events:structuredClone(events),next_cursor:null};
      throw new Error('Unexpected request ' + url);
    }};
  vm.createContext(context);
  vm.runInContext(source('san-juan-time.js') + '\n' + source('global-calendar.js') + '\nthis.load = calendarQueueLoad; this.configure = sjConfigure;', context);
  context.configure({operational_timezone:'UTC'});
  const now = Date.parse('2026-11-11T02:54:00Z'), result = await context.load(now);
  const read = calls.find(call => call.url.endsWith('calendar_events_list')).args;
  assert.equal(read.start, '2026-11-04T02:54:00.000Z');
  assert.equal(read.end, '2026-11-11T04:54:00.000Z');
  assert.deepEqual(Array.from(result.items, row => [row.event.id, new Date(row.appear).toISOString()]), [
    ['earlier','2026-11-10T19:00:00.000Z'], ['holiday','2026-11-11T00:00:00.000Z'],
    ['soon','2026-11-11T02:30:00.000Z'], ['later','2026-11-11T04:00:00.000Z']]);
  assert.equal(result.items.find(row => row.event.id === 'soon').key, 'calendar:2026-11-11:["conn","cal","soon","2026-11-11T03:30:00Z"]');
  assert.equal(result.items.find(row => row.event.id === 'holiday').key, 'calendar:2026-11-11:["conn","cal","holiday","2026-11-11"]');
  assert.match(result.items[0].label, /^Earlier · .*· Work$/);
  assert.deepEqual(result.errors, []);
});

function strip(options = {}) {
  const now = Date.now(), writes = [], toasts = [], opened = [], listeners = {}, calendarChanges = new Set();
  const event = id => ({id, title:id, start:new Date(now + (id === 'upcoming' ? 20 : -30) * 60000).toISOString(), end:'x'});
  const row = (id, appearMinutes) => ({key:`calendar:2026-11-11:["c","cal","${id}","${id}"]`, appear:now + appearMinutes * 60000, event:event(id), label:`${id} label`});
  const rows = [row('past', -90), row('upcoming', -40), row('hidden', 20)];
  const context = {Date, Promise, Map, Set, URLSearchParams, AbortController, JSON, esc:value => String(value ?? '').replace(/"/g,'&quot;'), coreData:x => x,
    accessLost:() => false, document:{addEventListener(){}, hidden:false}, window:{addEventListener(){}}, T_VIEW_ICON:{calendar:'<svg></svg>'},
    api:async () => ({items:[]}), calendarChanges, remindersOwnerDue:async () => [], remVerifiedOwner:async () => 'owner', remAttempts:new Map(),
    setTimeout:() => 0, clearTimeout(){}, toast:text => toasts.push(text), calendarQueueLoad:async () => ({items:rows,calendars:[{id:'cal'}],errors:options.errors || []}),
    calendarOpen:(item, calendars, refresh, clicked, origin) => { opened.push({item, calendars, origin}); },
    post:(url, args) => new Promise((resolve, reject) => writes.push({url, args, resolve, reject}))};
  const host = {isConnected:true, innerHTML:'', hidden:true, addEventListener(name, fn){listeners[name] = fn;}, removeEventListener(name){delete listeners[name];}, contains:() => true};
  vm.createContext(context);
  vm.runInContext(source('attention-queue.js') + '\nthis.queue = attentionQueue;', context);
  const chip = key => ({dataset:{calendarKey:key}, closest(selector){return selector === '[data-calendar-key]' ? this : null;}});
  return {context, host, writes, toasts, opened, listeners, calendarChanges, rows, chip};
}

test('the strip shows only calendar events that have appeared, past ones included, and opens them', async () => {
  const s = strip();
  const dispose = s.context.queue.mountSignoffs(s.host);
  await tick(); await tick();
  const keys = [...s.host.innerHTML.matchAll(/data-calendar-key="([^"]+)"/g)].map(match => match[1].replace(/&quot;/g,'"'));
  assert.deepEqual(keys, [s.rows[0].key, s.rows[1].key]);
  assert.match(s.host.innerHTML, /data-calendar-key="[^"]*past[^"]*" data-calendar-state="past"/);
  assert.match(s.host.innerHTML, /data-calendar-key="[^"]*upcoming[^"]*" data-calendar-state="upcoming"/);
  assert.doesNotMatch(s.host.innerHTML, /hidden/);
  assert.equal(s.calendarChanges.size, 1);
  const target = s.chip(s.rows[1].key);
  s.listeners.click({target, currentTarget:s.host});
  assert.equal(s.opened.length, 1);
  assert.equal(s.opened[0].item.id, 'upcoming');
  assert.equal(s.opened[0].origin, target);
  dispose();
  assert.equal(s.calendarChanges.size, 0);
});

test('right-click discards a calendar chip at once, saves it, and restores it when saving fails', async () => {
  const s = strip();
  s.context.queue.mountSignoffs(s.host);
  await tick(); await tick();
  const [first, second] = s.rows;
  const event = key => ({target:s.chip(key), currentTarget:s.host, preventDefault(){this.prevented = true;}});
  const one = event(first.key);
  s.listeners.contextmenu(one);
  assert.equal(one.prevented, true);
  assert.doesNotMatch(s.host.innerHTML.replace(/&quot;/g,'"'), new RegExp(first.key.replace(/[[\]().]/g,'\\$&')));
  await tick();
  assert.equal(s.writes.length, 1);
  assert.equal(s.writes[0].url, '/root/attention-queue');
  assert.deepEqual(JSON.parse(JSON.stringify(s.writes[0].args)), {discarded:[first.key]});
  s.writes[0].resolve({items:[], side_chats:[], discarded:[first.key]});
  await tick();
  assert.doesNotMatch(s.host.innerHTML.replace(/&quot;/g,'"'), /"c","cal","past"/);
  assert.match(s.host.innerHTML.replace(/&quot;/g,'"'), /"c","cal","upcoming"/);
  // A second right-click on the same chip does not save again.
  s.listeners.contextmenu(event(first.key)); await tick();
  assert.equal(s.writes.length, 1);
  s.listeners.contextmenu(event(second.key));
  assert.doesNotMatch(s.host.innerHTML.replace(/&quot;/g,'"'), /"c","cal","upcoming"/);
  await tick();
  s.writes[1].reject(new Error('offline'));
  await tick(); await tick();
  assert.match(s.host.innerHTML.replace(/&quot;/g,'"'), /"c","cal","upcoming"/);
  assert.deepEqual(s.toasts, ['Could not discard calendar event: offline']);
});

test('a calendar read failure shows a retry control and keeps the last events', async () => {
  const s = strip({errors:['Work: provider unavailable']});
  s.context.queue.mountSignoffs(s.host);
  await tick(); await tick();
  assert.match(s.host.innerHTML, /data-calendar-retry/);
  assert.match(s.host.innerHTML, /provider unavailable/);
  assert.match(s.host.innerHTML, /data-calendar-key/);
});
