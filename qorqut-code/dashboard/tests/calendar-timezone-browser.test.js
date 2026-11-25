const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname,'..');

async function fixture(browser, browserZone, appZone = 'UTC') {
  const context = await browser.newContext({viewport:{width:1200,height:800},timezoneId:browserZone,locale:'en-US'});
  const page = await context.newPage(), errors = [];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',route=>route.abort());
  await page.setContent('<div id="calendar"></div>');
  await page.clock.install({time:new Date('2026-11-11T02:54:00Z')});
  for (const file of ['app.css','css/global-calendar.css']) await page.addStyleTag({path:path.join(root,file)});
  await page.evaluate(() => {
    window.$ = (selector,host=document)=>host.querySelector(selector);
    window.esc = value=>String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    window.coreData = value=>value;
    window.accessLost = ()=>false;
    window.AICONS = {}; window.T_VIEW_ICON = {calendar:''};
    window.clearEtagCache = ()=>{};
    window.loadSession = async ()=>({is_owner:true,principal_kind:'human',principal_id:'owner'});
    window.api = async ()=>({items:[]});
    window.calls = [];
    const base = {connection_id:'conn',calendar_id:'cal',revision:'1',recurrence:[],attendees:[]};
    window.events = [
      {...base,id:'early',title:'Early event',start:'2026-11-11T00:30:00Z',end:'2026-11-11T01:00:00Z'},
      {...base,id:'planning',title:'Planning session',start:'2026-11-11T02:00:00Z',end:'2026-11-11T05:00:00Z'},
      {...base,id:'holiday',title:'Veterans Day',start:'2026-11-11',end:'2026-11-12'},
    ];
    window.post = async (url,args)=>{
      calls.push({url,args:structuredClone(args)});
      if (url.endsWith('calendar_calendars_list')) return {configured:true,errors:[],calendars:[{connection_id:'conn',id:'cal',name:'Work',writable:true,organizers:[]}]};
      if (url.endsWith('calendar_events_list')) return {events:structuredClone(events),next_cursor:null};
      throw new Error('Unexpected request '+url);
    };
  });
  for (const file of ['san-juan-time','global-calendar']) await page.addScriptTag({path:path.join(root,'js',file+'.js')});
  await page.evaluate(zone=>{
    sjConfigure({operational_timezone:zone});
    window.disposeCalendar = calendarMount(document.querySelector('#calendar'));
  },appZone);
  await page.waitForFunction(()=>document.querySelector('.calendar-status').textContent==='');
  return {context,page,errors};
}

test('rendered times, columns, provider ranges and navigation follow UTC in different browser zones',
  {skip:!available && 'Local Playwright Chromium is unavailable'}, async () => {
    const browser = await chromium.launch({headless:true,executablePath:executable});
    try {
      for (const browserZone of ['America/Los_Angeles','Pacific/Auckland']) {
        const {context,page,errors} = await fixture(browser,browserZone);
        try {
          assert.equal(await page.locator('.calendar-clock').textContent(),'2:54 AM');
          assert.equal(await page.locator('.calendar-clock').getAttribute('title'),'UTC');
          // Week view is the default on a wide pane and starts on Monday.
          assert.deepEqual(await page.locator('.calendar-dayheads .calendar-dayhead-name').allTextContents(),['Mon','Tue','Wed','Thu','Fri','Sat','Sun']);
          assert.deepEqual(await page.locator('.calendar-dayheads .calendar-dayhead-num').allTextContents(),['9','10','11','12','13','14','15']);
          assert.equal(await page.locator('.calendar-dayhead.is-today .calendar-dayhead-num').textContent(),'11');
          assert.equal(await page.locator('.calendar-range').textContent(),'9 – 15 November');
          assert.deepEqual(await page.locator('[data-calendar-view]').evaluateAll(list=>list.map(b=>b.textContent)),['Day','Week']);
          for (const gone of ['today','refresh','add']) assert.equal(await page.locator(`[data-calendar-${gone}]`).count(),0);
          const block = page.locator('.calendar-day[data-calendar-day="2026-11-11"] .calendar-event').filter({hasText:'Planning session'});
          assert.equal(await block.count(),1);
          // The whole day fits the grid without scrolling: the hour row is the grid's height shared by 24.
          const hour = await page.evaluate(()=>parseFloat(document.querySelector('.calendar-page').style.getPropertyValue('--calendar-hour')));
          assert.ok(hour >= 16, `hour ${hour}`);
          assert.ok(await page.locator('.calendar-week').evaluate(g=>g.scrollHeight <= g.clientHeight + 1), 'the grid scrolls');
          const box = await block.evaluate(el=>({top:parseFloat(getComputedStyle(el).top),height:parseFloat(getComputedStyle(el).height)}));
          assert.ok(Math.abs(box.top-2*hour) < 1 && Math.abs(box.height-3*hour) < 1, JSON.stringify({box,hour}));
          assert.match(await block.getAttribute('title'),/2:00 AM – 5:00 AM/);
          assert.equal(await block.locator('.calendar-event-time').textContent(),'2 – 5 AM');
          // The event and its calendar in the list share one colour.
          assert.equal(await block.getAttribute('data-calendar-tone'),'0');
          assert.equal(await page.locator('.calendar-source').getAttribute('data-calendar-tone'),'0');
          assert.equal(await page.locator('.calendar-source-name').textContent(),'Work');
          assert.equal(await page.locator('.calendar-tick').first().textContent(),'12 AM');
          // The grid always shows the whole day, whatever the events.
          assert.equal(await page.locator('.calendar-tick').count(),24);
          assert.equal(await page.locator('.calendar-tick').last().textContent(),'11 PM');
          assert.equal(Math.abs(await page.locator('.calendar-day').first().evaluate(el=>el.offsetHeight) - 24*hour) < 1.5, true);
          // The now line sits on today's column at 2:54 on a grid that starts at midnight.
          assert.equal(await page.locator('.calendar-now').count(),1);
          assert.equal(Math.abs(await page.locator('.calendar-day[data-calendar-day="2026-11-11"] .calendar-now').evaluate(el=>parseFloat(el.style.top)) - 2.9*hour) < .01, true);
          assert.equal(await page.locator('.calendar-all-day-cell').nth(2).textContent(),'Veterans Day');
          const reads = await page.evaluate(()=>calls.filter(c=>c.url.endsWith('calendar_events_list')).map(c=>[c.args.start,c.args.end]));
          assert.ok(reads.some(([start,end])=>start==='2026-11-09T00:00:00.000Z' && end==='2026-11-16T00:00:00.000Z'));
          assert.equal(await page.locator('[data-calendar-view="7"]').getAttribute('aria-pressed'),'true');
          assert.equal(await page.locator('.calendar-day').last().getAttribute('data-calendar-day'),'2026-11-15');
          // The mini month marks today with a dot and outlines the week as one range.
          assert.equal(await page.locator('.calendar-monthday.is-today').getAttribute('data-calendar-date'),'2026-11-11');
          assert.equal(await page.locator('.calendar-monthday.is-today').evaluate(el=>getComputedStyle(el,'::after').width),'3px');
          assert.deepEqual(await page.locator('.calendar-monthday.in-range').evaluateAll(list=>list.map(b=>b.dataset.calendarDate)),
            ['2026-11-09','2026-11-10','2026-11-11','2026-11-12','2026-11-13','2026-11-14','2026-11-15']);
          assert.equal(await page.locator('.calendar-monthday.range-start').count(),1);
          assert.equal(await page.locator('.calendar-monthday.range-end').count(),1);
          await page.locator('[data-calendar-next]').click();
          assert.equal(await page.locator('.calendar-day').first().getAttribute('data-calendar-day'),'2026-11-16');
          assert.equal(await page.locator('.calendar-now').count(),0);
          await page.locator('[data-month-next]').click();
          // A day chosen in the mini month opens as a Day view.
          await page.locator('[data-calendar-date="2026-12-01"]').click();
          assert.equal(await page.locator('.calendar-day').count(),1);
          assert.equal(await page.locator('.calendar-day').first().getAttribute('data-calendar-day'),'2026-12-01');
          assert.equal(await page.locator('[data-calendar-view="1"]').getAttribute('aria-pressed'),'true');
          assert.equal(await page.locator('.calendar-monthday.in-range').count(),1);
          await page.locator('[data-calendar-view="7"]').click();
          assert.equal(await page.locator('.calendar-day').first().getAttribute('data-calendar-day'),'2026-11-30');
          assert.equal(await page.locator('.calendar-dayhead-name').first().textContent(),'Mon');
          // A Monday-first week is one row of the mini month, cut at the month edge (30 November is not shown).
          assert.equal(await page.locator('.calendar-monthday.in-range').count(),6);
          assert.equal(await page.locator('.calendar-monthday.range-start').count(),1);
          assert.deepEqual(errors,[]);
        } finally { await context.close(); }
      }
    } finally { await browser.close(); }
  });

test('a UTC browser renders app-local overnight segments on their correct dates',
  {skip:!available && 'Local Playwright Chromium is unavailable'}, async () => {
    const browser = await chromium.launch({headless:true,executablePath:executable});
    try {
      const {page,errors} = await fixture(browser,'UTC','America/New_York');
      assert.equal(await page.locator('.calendar-clock').textContent(),'9:54 PM');
      assert.equal(await page.locator('.calendar-day').first().getAttribute('data-calendar-day'),'2026-11-09');
      const block = page.locator('.calendar-day[data-calendar-day="2026-11-10"] .calendar-event').filter({hasText:'Planning session'});
      assert.match(await block.getAttribute('title'),/9:00 PM – 12:00 AM/);
      assert.equal(await page.locator('.calendar-day[data-calendar-day="2026-11-11"] .calendar-event').count(),0);
      assert.equal(await page.locator('.calendar-all-day-cell').nth(2).textContent(),'Veterans Day');
      assert.equal(await page.evaluate(()=>calls.find(c=>c.url.endsWith('calendar_events_list')).args.start),'2026-11-09T05:00:00.000Z');
      assert.deepEqual(errors,[]);
    } finally { await browser.close(); }
  });
