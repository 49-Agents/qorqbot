const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness(zone = 'UTC', now = '2026-11-11T02:54:00Z') {
  const calls = [];
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return Date.parse(now); }
  }
  const context = vm.createContext({Date:Clock,console,coreData:value=>value,accessLost:()=>false,
    post:async (url,args) => {
      calls.push({url,args});
      if (url.endsWith('calendar_calendars_list')) return {configured:true,errors:[],calendars:[{connection_id:'conn',id:'cal',name:'Work'}]};
      assert.ok(url.endsWith('calendar_events_list'));
      return {events:[],next_cursor:null};
    }});
  for (const file of ['san-juan-time','global-calendar']) vm.runInContext(fs.readFileSync(path.join(__dirname,`../js/${file}.js`),'utf8'),context);
  const run = code => vm.runInContext(code,context);
  run(`sjConfigure(${JSON.stringify(zone)})`);
  return {context,run,calls};
}

test('clock, today, detail and timed inputs follow the app timezone', () => {
  const h = harness();
  assert.equal(h.run('calendarZone()'),'UTC');
  assert.equal(h.run('calendarDateString(new Date())'),'2026-11-11');
  assert.equal(h.run("calendarDateLabel('2026-11-11')"),'Today');
  assert.equal(h.run("calendarDateLabel('2026-11-10')"),'Yesterday');
  assert.equal(h.run('calendarTime(new Date())'),'2:54 AM');
  const event = {start:'2026-11-11T02:00:00+00:00',end:'2026-11-11T05:00:00+00:00'};
  h.context.event = event;
  assert.equal(h.run('calendarWhenParts(event).time'),'2:00 AM – 5:00 AM UTC');
  assert.equal(h.run('calendarLocalInput(event.start)'),'2026-11-11T02:00');
  assert.equal(h.run("calendarInputInstant('2026-11-11T02:00')"),'2026-11-11T02:00:00.000Z');
  assert.equal(h.run("calendarDaySpan(event,'2026-11-10')"),null);
  assert.equal(h.run("calendarDaySpan(event,'2026-11-11').start"),120);
  assert.equal(h.run("calendarDaySpan(event,'2026-11-11').end"),300);
});

test('overnight segments split only at the app midnight and exclude an exact end boundary', () => {
  const h = harness('America/New_York');
  h.context.event = {start:'2026-11-11T04:30:00Z',end:'2026-11-11T06:15:00Z'};
  assert.equal(h.run("calendarDaySpan(event,'2026-11-10').start"),1410);
  assert.equal(h.run("calendarDaySpan(event,'2026-11-10').end"),1440);
  assert.equal(h.run("calendarDaySpan(event,'2026-11-11').start"),0);
  assert.equal(h.run("calendarDaySpan(event,'2026-11-11').end"),75);
  h.context.event.end = '2026-11-11T05:00:00Z';
  assert.equal(h.run("calendarDaySpan(event,'2026-11-11')"),null);
  assert.equal(h.run("calendarDaySpan(event,'2026-11-09')"),null);
  h.context.event = {start:'2026-11-11T05:00:00Z',end:'2026-11-11T05:00:30Z'};
  assert.equal(h.run("calendarDaySpan(event,'2026-11-11').end"),1);
});

test('all-day dates and month navigation do not shift with timezone offsets', () => {
  for (const zone of ['Pacific/Kiritimati','America/Los_Angeles']) {
    const h = harness(zone);
    h.context.event = {start:'2026-11-11',end:'2026-11-12',series_id:'series',id:'occ'};
    assert.equal(h.run('calendarDateString(event.start)'),'2026-11-11');
    assert.equal(h.run('calendarLocalInput(event.start)'),'2026-11-11');
    assert.equal(h.run('calendarStatusTarget(event).occurrence_start'),'2026-11-11');
    assert.match(h.run('calendarWhenParts(event).date'),/Nov 11, 2026/);
    assert.equal(h.run('calendarWhenParts(event).time'),'All day');
    assert.equal(h.run("calendarDaySpan(event,'2026-11-11')"),null);
    assert.equal(h.run("calendarMonthShift('2026-12-01',1)"),'2027-01-01');
    assert.equal(h.run("calendarAddDays('2028-03-01',-1)"),'2028-02-29');
  }
});

test('today queries cover app-local dates, including 23- and 25-hour DST days', async () => {
  for (const [zone,now,key,start,end] of [
    ['UTC','2026-11-11T02:54:00Z','2026-11-11','2026-11-11T00:00:00.000Z','2026-11-12T00:00:00.000Z'],
    ['Asia/Kathmandu','2026-11-10T22:00:00Z','2026-11-11','2026-11-10T18:15:00.000Z','2026-11-11T18:15:00.000Z'],
    ['America/New_York','2026-03-08T12:00:00Z','2026-03-08','2026-03-08T05:00:00.000Z','2026-03-09T04:00:00.000Z'],
    ['America/New_York','2026-11-01T12:00:00Z','2026-11-01','2026-11-01T04:00:00.000Z','2026-11-02T05:00:00.000Z'],
    ['America/Santiago','2026-09-06T12:00:00Z','2026-09-06','2026-09-06T04:00:00.000Z','2026-09-07T03:00:00.000Z'],
  ]) {
    const h = harness(zone,now), data = await h.run('calendarTodayLoad()');
    const query = h.calls.find(call=>call.url.endsWith('calendar_events_list')).args;
    assert.equal(data.key,key);
    assert.equal(data.zone,zone);
    assert.equal(query.start,start);
    assert.equal(query.end,end);
  }
});

test('typed times use the app offset and reject nonexistent wall times', () => {
  const h = harness('Asia/Kathmandu');
  assert.equal(h.run("calendarInputInstant('2026-11-11T13:15')"),'2026-11-11T07:30:00.000Z');
  h.run("sjConfigure('America/New_York')");
  assert.throws(()=>h.run("calendarInputInstant('2026-03-08T02:30')"),/daylight-saving gap/);
  assert.equal(h.run("calendarInputInstant('2026-03-08T03:30')"),'2026-03-08T07:30:00.000Z');
  assert.equal(h.run("calendarInputInstant('2026-11-01T01:30')"),'2026-11-01T05:30:00.000Z');
  assert.throws(()=>h.run("calendarInputInstant('2026-02-30T10:00')"),/valid time/);
});
