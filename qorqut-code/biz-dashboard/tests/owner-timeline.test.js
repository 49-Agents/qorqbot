const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const read = name => fs.readFileSync(path.join(__dirname, '..', 'js', name), 'utf8');
function timeline(overrides = {}) {
  const context = vm.createContext({
    URL, document:{addEventListener() {}, dispatchEvent() {}, querySelectorAll:() => []}, CustomEvent:class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    localStorage:{store:{}, getItem(k) { return this.store[k] ?? null; }, setItem(k, v) { this.store[k] = v; }},
    coreData:body => body.data, SJ_ZONE:'UTC', ...overrides,
  });
  vm.runInContext(read('safe-links.js'), context);
  vm.runInContext('const esc = safeLinkEsc;', context);
  vm.runInContext(read('san-juan-time.js'), context);
  vm.runInContext('const AICONS = {};', context);
  vm.runInContext(read('actions.js'), context);
  vm.runInContext(read('owner-timeline.js'), context);
  return context;
}
const action = (id, instant) => ({action_id:id, text:`Action ${id}`, occurred:{date:instant.slice(0, 10), precision:'instant', instant, time_zone:'UTC'}});
const message = (id, at, extra = {}) => ({id, at, channel:'chat', via:'dashboard', agent:{id:'o', name:'Ordubashy', role:'ordubashy'}, text:`Message ${id}`, followed_by:{reply:null, records:[]}, ...extra});

test('messages sit between actions by time, newest first, and a run to one place is one entry', () => {
  const t = timeline();
  const entries = t.ownerTimelineMerge(
    [action('a1', '2026-11-10T10:00:00Z'), action('a2', '2026-11-10T12:00:00Z')],
    [message('m1', '2026-11-10T11:00:00Z'), message('m2', '2026-11-10T11:05:00Z'), message('m3', '2026-11-10T11:30:00Z'),
      message('m4', '2026-11-10T12:05:00Z')]);
  assert.deepEqual(entries.map(e => e.kind === 'action' ? e.action.action_id : e.items.map(i => i.id).join('+')),
    ['m4', 'a2', 'm3', 'm1+m2', 'a1']);
});

test('messages to different places or with an action between them are not grouped', () => {
  const t = timeline();
  const other = {channel:'thread', source:{type:'task', id:'t1', title:'Fix invoices'}};
  const entries = t.ownerTimelineMerge([action('a1', '2026-11-10T11:02:00Z')],
    [message('m1', '2026-11-10T11:00:00Z'), message('m2', '2026-11-10T11:04:00Z'), message('m3', '2026-11-10T11:05:00Z', other)]);
  assert.equal(entries.filter(e => e.kind === 'messages').length, 3);
});

test('a message row has its own icon, says where it was written and what followed', () => {
  const t = timeline();
  const [entry] = t.ownerTimelineMerge([], [
    message('m1', '2026-11-10T11:00:00Z', {text:'Send the <draft>', followed_by:{reply:{at:'2026-11-10T11:01:00Z', preview:'Sending'}, records:[]}}),
    message('m2', '2026-11-10T11:03:00Z', {followed_by:{reply:{at:'2026-11-10T11:04:00Z', preview:'On it'},
      records:[{at:'2026-11-10T11:20:00Z', change:'Task “Draft” moved to review'}], more_records:3}})]);
  const html = t.ownerTimelineRow(entry);
  assert.ok(html.includes('class="owner-timeline-icon"><svg'));
  assert.ok(html.includes('You · in Chat'));
  assert.ok(html.includes('“Send the &lt;draft&gt;”'));
  assert.ok(html.includes('+ 1 more message<'));
  assert.ok(html.includes('Then 11:04</span> Replied: On it'));
  assert.ok(html.includes('Then 11:20</span> Task “Draft” moved to review'));
  assert.ok(html.includes('Then 11:01</span> Replied: Sending'));
  assert.ok(html.includes('+ 3 more'));
});

test('places read as the owner would name them, and private text is not shown', () => {
  const t = timeline();
  assert.equal(t.ownerTimelinePlace(message('m', 'x', {via:'telegram'})), 'on Telegram');
  assert.equal(t.ownerTimelinePlace(message('m', 'x', {agent:{id:'w', name:'Ayaz', role:'worker'}})), "in Ayaz's chat");
  assert.equal(t.ownerTimelinePlace({channel:'note', source:{type:'task', id:'t', title:'Rent'}}), 'note on “Rent”');
  assert.equal(t.ownerTimelinePlace({channel:'thread', source:{type:'signoff', id:'s', title:'Daily report'}}), 'in “Daily report”');
  const [entry] = t.ownerTimelineMerge([], [message('m1', '2026-11-10T11:00:00Z', {redacted:true, text:'secret'})]);
  assert.ok(!t.ownerTimelineRow(entry).includes('secret'));
});

test('the switch is on by default and remembers being turned off', () => {
  const t = timeline();
  assert.equal(t.ownerTimelineIsShown(), true);
  assert.ok(t.ownerTimelineToggleHtml().includes('aria-checked="true"'));
  t.ownerTimelineSetShown(false);
  assert.equal(t.localStorage.store['qorqut.timeline.ownerMessages'], 'off');
  assert.equal(timeline({localStorage:t.localStorage}).ownerTimelineIsShown(), false);
});

test('messages are read page by page until the cursor ends', async () => {
  const calls = [];
  const t = timeline({post:async (url, args) => {
    calls.push(args);
    return {data:args.cursor ? {items:[message('m2', '2026-11-10T11:00:00Z')], next_cursor:null}
      : {items:[message('m1', '2026-11-10T10:00:00Z')], next_cursor:'c1'}};
  }});
  const items = await t.ownerTimelineLoad({since:'2026-11-10T00:00:00Z', until:'2026-11-11T00:00:00Z', agentId:'w'});
  assert.equal(items.map(i => i.id).join(), 'm1,m2');
  assert.equal(calls[0].agent_id, 'w');
  assert.equal(calls[1].cursor, 'c1');
});
