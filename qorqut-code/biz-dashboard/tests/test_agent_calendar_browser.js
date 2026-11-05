#!/usr/bin/env node
// Actual dashboard + agent pane in Chromium, synthetic API only. No live writes or agents.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'..');
const profiles = [['ordu','Ordubashy','ordubashy'],['marketing','Marketing','global-worker']]
  .map(([agent_id,display_name,role]) => ({agent_id,name:agent_id,display_name,status:'active',role:{id:role},scope:{kind:'ecosystem',allowed_business_ids:[],global_capabilities:[]}}));
const calendars = [
  {connection_id:'personal',id:'primary',provider:'google',name:'Personal',writable:true,organizers:['owner@example.com']},
  {connection_id:'business',id:'work',provider:'fastmail',name:'Business',writable:true,organizers:['owner@business.example']},
];
let configured = true, failed = false, eventReads = [], writes = [];
// Personal-status fixture: owner preference store with revisions, request replay and injectable failures.
const statuses = new Map(), lastRequest = new Map(), statusPosts = [], providerOps = [], masters = {};
const statusState = {calendarsFail:false, fail:null, postMode:null, delay:0, reads:0, pageSize:1};
const statusKey = i => [i.connection_id,i.calendar_id,i.event_id,i.occurrence_start].join('|');
const putStatus = (target,status) => { const old = statuses.get(statusKey(target)); const item = {...target,status,revision:(old?.revision || 0)+1}; statuses.set(statusKey(item),item); return item; };
const envelope = (res,code,error) => res.writeHead(code,{'Content-Type':'application/json'}).end(JSON.stringify({status:'error',error}));
let events = [
  {id:'personal-1',connection_id:'personal',calendar_id:'primary',title:'Apartment viewing',start:'2026-10-14T14:00:00Z',end:'2026-10-14T15:00:00Z'},
  {id:'work-1',connection_id:'business',calendar_id:'work',title:'Business call',start:'2026-10-14T14:15:00Z',end:'2026-10-14T14:45:00Z'},
  {id:'all-day',connection_id:'personal',calendar_id:'primary',title:'Delivery day',start:'2026-10-14',end:'2026-10-15'},
].map(event => ({...event,status:'confirmed',revision:'1',attendees:[],recurrence:[],reminders:[],organizer:'owner@example.com'}));
const server = http.createServer(async (req,res) => {
  const url = new URL(req.url,'http://fixture');
  if (url.pathname.startsWith('/api/')) {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const args = raw ? JSON.parse(raw) : {};
    let data = {items:[],messages:[],purposes:[],tags:[],agents:[],totals:{}};
    if (/\/calendar_event_(update|delete|respond|create)$/.test(url.pathname)) providerOps.push(url.pathname.split('/').pop());
    if (url.pathname === '/api/root/calendar-statuses') {
      res.setHeader('Cache-Control','private, no-store');
      if (req.method === 'GET') {
        statusState.reads++;
        if (statusState.fail) return envelope(res,statusState.fail,{code:statusState.fail === 401 ? 'unauthorized' : 'unavailable',message:'Statuses temporarily unavailable'});
        const snapshot = [...statuses.values()].map(item => ({...item}));
        if (statusState.delay) await new Promise(resolve => setTimeout(resolve,statusState.delay));
        data = {items:snapshot};
      } else {
        statusPosts.push(args);
        const key = statusKey(args), old = statuses.get(key), previous = lastRequest.get(key);
        const {request_id,...payload} = args;
        if (previous?.request_id === request_id && JSON.stringify(previous.payload) === JSON.stringify(payload)) data = {item:previous.item};
        else if ((old?.revision || 0) !== args.expected_revision) return envelope(res,409,{code:'conflict',message:'Status changed since it was read'});
        else {
          const item = putStatus(args,args.status); lastRequest.set(key,{request_id,payload,item});
          if (statusState.postMode === 'lost') return envelope(res,503,{code:'unavailable',message:'Connection lost'});
          data = {item};
        }
      }
    } else if (url.pathname === '/api/biz') data = [];
    else if (url.pathname === '/api/session') data = {principal_id:'owner',principal_kind:'human',is_owner:true,is_installation_owner:true};
    else if (url.pathname === '/api/root/agents') data = {agents:profiles};
    else if (url.pathname === '/api/root/agent-pins') data = {initialized:true,pinned_agents:[]};
    else if (url.pathname.endsWith('/focus_get')) data = {revision:1,items:[{id:'focus-1',revision:1,label:'Review the proposal',display_name:'Review proposal',reference:{title:'Review the proposal',target:{type:'url',url:'https://example.test'}},references:[]}]};
    else if (url.pathname.endsWith('/workspace_types')) data = {types:[{key:'focus',display_name:'Focus',supported_roles:['ordubashy','global-worker'],renderer:'focus_board'}]};
    else if (url.pathname.endsWith('/browser-surfaces')) data = {surfaces:[]};
    else if (/\/agents\/[^/]+\/chat$/.test(url.pathname)) data = {agent_id:url.pathname.split('/')[4],session:{state:'stopped',can_send:false},revision:0,cursor:0,entries:[],conversations:[],inputs:[]};
    else if (url.pathname.endsWith('/calendar_calendars_list') && statusState.calendarsFail) return envelope(res,503,{code:'unavailable',message:'Calendar service temporarily unavailable'});
    else if (url.pathname.endsWith('/calendar_calendars_list')) data = {configured,calendars:configured ? calendars : [],errors:[]};
    else if (url.pathname.endsWith('/calendar_events_list')) {
      eventReads.push(args);
      if (failed && args.connection_id === 'business') {
        res.writeHead(503,{'Content-Type':'application/json'}).end(JSON.stringify({status:'error',error:{message:'Business calendar temporarily unavailable'}})); return;
      }
      const day = args.start.slice(0,10);
      const matches = events.filter(e => e.connection_id === args.connection_id && e.start.slice(0,10) >= day && e.start.slice(0,10) < args.end.slice(0,10));
      const offset = args.cursor ? 1 : 0;
      data = {events:matches.slice(offset,offset+statusState.pageSize),next_cursor:matches.length>offset+statusState.pageSize ? 'next-page' : null};
    } else if (url.pathname.endsWith('/calendar_event_get')) data = {event:masters[args.event_id] || events.find(e => e.id === args.event_id)};
    else if (url.pathname.endsWith('/calendar_event_update')) {
      writes.push(args);
      const event = events.find(e => e.id === args.event_id); Object.assign(event,args.changes); data = {event};
    }
    res.setHeader('Content-Type','application/json');
    res.end(JSON.stringify(url.pathname === '/api/biz' ? data : {status:'ok',data})); return;
  }
  const file = path.resolve(root,'.'+(url.pathname === '/' ? '/index.html' : url.pathname));
  if (!file.startsWith(root+path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
  res.setHeader('Content-Type',file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});
(async () => {
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport:{width:1500,height:900},timezoneId:'America/New_York'}), errors = [];
    page.on('pageerror',error => errors.push(error.message));
    await page.route('https://**',route => route.abort());
    await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
    await page.clock.install({time:new Date('2026-10-14T13:30:00Z')});
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.goto(origin+'/#/global/agents');
    const excerpt = page.getByRole('region',{name:"Today's calendar",exact:true});
    await excerpt.getByRole('button',{name:/Apartment viewing/}).waitFor();
    await excerpt.getByRole('button',{name:/Delivery day/}).waitFor();
    assert.equal(await page.getByRole('tab',{name:'Calendar',exact:true}).count(),0,'Calendar is a drawer tab, not an agent tab');
    assert.equal(await excerpt.evaluate(el => el.nextElementSibling.classList.contains('attention-queue')),true);
    assert((await excerpt.boundingBox()).height < 100,'single-line queue leaves Focus room');
    assert.equal(await excerpt.locator('.calendar-axis,.calendar-week').count(),0,'Focus has no empty time grid');
    const personal = await excerpt.getByRole('button',{name:/Apartment viewing/}).boundingBox();
    const business = await excerpt.getByRole('button',{name:/Business call/}).boundingBox();
    assert.equal(personal.y,business.y,'events form one horizontal queue');
    assert.equal(personal.height,business.height,'card size does not depend on duration');
    assert(Math.abs(business.x-personal.x-personal.width-8)<1,'events sit next to each other without empty-hour spacing');
    assert.deepEqual(await excerpt.locator('.calendar-queue-event strong').allTextContents(),['Delivery day','Apartment viewing','Business call']);
    assert.equal(await excerpt.locator('.calendar-queue-event > span').nth(1).innerText(),'10:00 AM');
    assert(eventReads.some(r => r.start === '2026-10-14T04:00:00.000Z' && r.end === '2026-10-15T04:00:00.000Z'),'today uses local day boundaries');
    assert(eventReads.some(r => r.cursor === 'next-page'),'excerpt follows provider pagination');
    await page.screenshot({path:'/tmp/ordubashy-focus-event-queue.png'});
    await excerpt.getByRole('button',{name:/Apartment viewing/}).click();
    await page.getByRole('button',{name:'Edit',exact:true}).click();
    await page.getByLabel('Title',{exact:true}).fill('Updated apartment viewing');
    await page.getByRole('button',{name:'Save',exact:true}).click();
    await excerpt.getByRole('button',{name:/Updated apartment viewing/}).waitFor();
    assert.equal(writes.length,1,'excerpt uses existing calendar editing');
    const chat = page.locator('[data-global-chat]');
    await chat.evaluate(el => { window.calendarChat = el; });
    await page.setViewportSize({width:800,height:900});
    const queue = excerpt.locator('.calendar-today-queue');
    assert(await queue.evaluate(el => el.scrollWidth > el.clientWidth),'narrow Focus scrolls its event queue');
    assert(await page.locator('#agpad-body').evaluate(el => el.scrollWidth <= el.clientWidth),'queue stays inside the pane');
    await queue.evaluate(el => el.scrollLeft = 120);
    const scrollLeft = await queue.evaluate(el => el.scrollLeft);
    events[0].location = 'New location';
    await page.clock.fastForward(61000);
    await page.waitForFunction(() => document.querySelector('.calendar-today-queue').scrollLeft === 120);
    assert.equal(await queue.evaluate(el => el.scrollLeft),scrollLeft,'provider refresh preserves queue scroll');
    await page.screenshot({path:'/tmp/ordubashy-focus-event-queue-narrow.png'});
    await page.setViewportSize({width:1500,height:900});
    await page.getByRole('button',{name:"Open today's calendar",exact:true}).click();
    await page.locator('.calendar-embedded .calendar-event',{hasText:'Updated apartment viewing'}).waitFor();
    const mainEvent = page.locator('.calendar-embedded .calendar-event',{hasText:'Updated apartment viewing'});
    assert.equal(await mainEvent.evaluate(el => el.style.top),'128px','main Calendar still uses a vertical time axis');
    assert.equal(await mainEvent.evaluate(el => el.style.height),'64px','main Calendar still scales duration vertically');
    assert.equal(await page.evaluate(() => location.hash),'#/global/agents');
    assert.equal(await page.getByRole('combobox',{name:'Calendar view',exact:true}).inputValue(),'1','agent pane defaults to a readable day');
    assert.equal(await chat.evaluate(el => el === window.calendarChat),true,'chat is preserved');
    await page.getByRole('button',{name:'Next days',exact:true}).click();
    await page.getByRole('button',{name:'Add event',exact:true}).click();
    assert.equal(await page.getByLabel('Start',{exact:true}).inputValue(),'2026-10-15T10:00','new event uses the pane-selected date');
    await page.getByRole('button',{name:'Cancel',exact:true}).click();
    await page.getByRole('button',{name:'Previous days',exact:true}).click();
    await page.locator('.calendar-embedded .calendar-event',{hasText:'Updated apartment viewing'}).waitFor();
    await page.getByRole('button',{name:'Load more from Personal',exact:true}).click();
    await page.locator('.calendar-embedded .calendar-all-day-event',{hasText:'Delivery day'}).waitFor();
    await page.screenshot({path:'/tmp/ordubashy-calendar-pane.png'});
    await page.setViewportSize({width:800,height:900});
    await page.waitForFunction(() => document.querySelectorAll('.calendar-embedded .calendar-day').length === 1);
    assert(await page.locator('#agpad-body').evaluate(el => el.scrollWidth <= el.clientWidth),'narrow pane stays within width');
    await page.setViewportSize({width:1500,height:900});
    await page.evaluate(() => openTasks(false));   // close the shared drawer; Focus keeps its own mount
    await page.getByRole('tab',{name:'Focus',exact:true}).click();
    await excerpt.getByRole('button',{name:/Updated apartment viewing/}).waitFor();
    // The next poll updates provider edits made outside this dashboard.
    events[0].title = 'Changed by Ordubashy';
    await page.clock.fastForward(61000);
    await excerpt.getByRole('button',{name:/Changed by Ordubashy/}).waitFor();
    failed = true;
    await page.clock.fastForward(61000);
    await excerpt.getByRole('status').filter({hasText:'Business calendar temporarily unavailable'}).waitFor();
    assert.equal(await excerpt.getByRole('button',{name:/Changed by Ordubashy/}).count(),1,'other provider survives a partial failure');
    failed = false; configured = false;
    await page.clock.fastForward(61000);
    await excerpt.getByText('Connect your calendars to see today.',{exact:true}).waitFor();
    assert.equal(await excerpt.locator('.calendar-today-queue').isVisible(),false);
    configured = true; events = [];
    await page.clock.fastForward(61000);
    await excerpt.getByText('No events today.',{exact:true}).waitFor();
    assert.equal(await excerpt.locator('.calendar-today-queue').isVisible(),false,'empty days leave no blank queue');
    await page.clock.fastForward(24*60*60*1000);
    await page.waitForFunction(() => document.querySelector('.calendar-today [data-calendar-day]')?.dataset.calendarDay === '2026-10-15');
    // Switching away disposes polling, even though Focus itself continues to exist for Marketing.
    await page.evaluate(() => document.querySelector('[data-global-agent="marketing"]').click());
    await page.waitForFunction(() => _agpad?.workspaceRole === 'global-worker');
    assert.equal(await page.getByRole('tab',{name:'Calendar',exact:true}).count(),0);
    assert.equal(await excerpt.count(),0);
    const reads = eventReads.length;
    await page.clock.fastForward(120000);
    assert.equal(eventReads.length,reads,'no personal calendar reads for Marketing');
    await page.goto(origin+'/#/global/calendar');
    await page.getByRole('button',{name:'Add event',exact:true}).waitFor({state:'visible'});
    assert.equal(await page.locator('#taskspanel .calendar-embedded').count(),1,'the old Calendar URL opens the shared drawer Calendar');
    assert.equal(await page.locator('#taskspanel .calendar-day').count(),1);
    // ---- Personal status: synthetic owner preferences over provider events; provider writes must never happen.
    await page.close();
    const shots = '/absolute/path/to/artifacts/focus-calendar-status';
    fs.mkdirSync(shots,{recursive:true});
    const mk = (id,connection,calendar,title,start,end,extra = {}) => ({id,connection_id:connection,calendar_id:calendar,title,start,end,status:'confirmed',revision:'1',attendees:[],recurrence:[],reminders:[],organizer:'owner@example.com',...extra});
    const standup = {recurrence:['RRULE:FREQ=DAILY']};
    const seed = () => {
      configured = true; failed = false; statusState.pageSize = 50; statusState.fail = null; statusState.postMode = null; statusState.delay = 0;
      events = [mk('s-all','personal','primary','Delivery day','2026-10-14','2026-10-15'),
        mk('s-plan','personal','primary','Planning sync','2026-10-14T14:00:00Z','2026-10-14T15:00:00Z'),
        mk('cal-href-1','business','work','Standup','2026-10-14T15:00:00Z','2026-10-14T15:30:00Z',standup),
        mk('s-move','personal','primary','Move review','2026-10-14T15:30:00Z','2026-10-14T16:00:00Z'),
        mk('s-dentist','personal','primary','Dentist visit','2026-10-14T16:00:00Z','2026-10-14T17:00:00Z'),
        mk('s-lunch','personal','primary','Lunch with Sam','2026-10-14T16:30:00Z','2026-10-14T17:30:00Z'),
        mk('cal-href-1','business','work','Standup','2026-10-14T18:00:00Z','2026-10-14T18:30:00Z',standup),
        mk('s-gone','personal','primary','Provider cancelled','2026-10-14T19:00:00Z','2026-10-14T19:30:00Z',{status:'cancelled'})];
      masters['cal-href-1'] = mk('cal-href-1','business','work','Standup','2026-10-01T15:00:00Z','2026-10-01T15:30:00Z',standup);
      statuses.clear(); lastRequest.clear(); statusPosts.length = 0;
      putStatus({connection_id:'personal',calendar_id:'primary',event_id:'s-dentist',occurrence_start:''},'completed');
      putStatus({connection_id:'personal',calendar_id:'primary',event_id:'s-lunch',occurrence_start:''},'cancelled');
      putStatus({connection_id:'personal',calendar_id:'primary',event_id:'s-move',occurrence_start:''},'rescheduled');
    };
    seed();
    const opsBefore = providerOps.length, pageErrors = [];
    const fresh = async (viewport = {width:1500,height:900}) => {
      const p = await browser.newPage({viewport,timezoneId:'America/New_York'});
      p.on('pageerror',error => pageErrors.push(error.message));
      await p.route('https://**',route => route.abort());
      await p.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
      await p.clock.install({time:new Date('2026-10-14T13:30:00Z')});
      await p.goto(origin+'/#/global/agents');
      return p;
    };
    const chip = (p,match) => p.locator(`.calendar-today .calendar-queue-event[aria-label*="${match}"]`);
    const label = async (p,match) => chip(p,match).getAttribute('aria-label');
    const waitLabel = (p,match,re) => p.waitForFunction(([m,source]) => new RegExp(source).test(document.querySelector(`.calendar-today .calendar-queue-event[aria-label*="${m}"]`)?.getAttribute('aria-label') || ''),[match,re.source]);
    const opacity = locator => locator.evaluate(el => Number(getComputedStyle(el).opacity));
    const dialog = p => p.getByRole('dialog');
    const noteOf = p => dialog(p).locator('.calendar-status-note');
    const pickStatus = async (p,value) => { await dialog(p).getByLabel('Personal status',{exact:true}).selectOption(value); await dialog(p).getByRole('button',{name:'Save status',exact:true}).click(); };
    const closeDialog = p => dialog(p).locator('.mfoot').getByRole('button',{name:'Close',exact:true}).click();
    const target = (id,occurrence_start = '',connection = 'personal',calendar = 'primary') => ({connection_id:connection,calendar_id:calendar,event_id:id,occurrence_start});

    let p = await fresh();
    let focus = p.getByRole('region',{name:"Today's calendar",exact:true});
    await chip(p,'Planning sync').waitFor();
    await waitLabel(p,'Dentist visit',/Completed/);
    assert.deepEqual(await focus.locator('.calendar-queue-event strong').allTextContents(),['Delivery day','Planning sync','Standup','Move review','Dentist visit','Lunch with Sam','Standup'],'chronological order and provider-cancelled omission are unchanged');
    const ys = await focus.locator('.calendar-queue-event').evaluateAll(els => els.map(el => el.getBoundingClientRect().top));
    assert(ys.every(y => Math.abs(y-ys[0]) < 1),'one row');
    const plan = await chip(p,'Planning sync').boundingBox(), dentist = await chip(p,'Dentist visit').boundingBox(), moved = await chip(p,'Move review').boundingBox(), lunch = await chip(p,'Lunch with Sam').boundingBox();
    assert.equal(dentist.height,plan.height,'completed chip keeps the row height');
    assert(dentist.width < plan.width*0.6,'completed chip is narrower');
    assert(await opacity(chip(p,'Dentist visit')) < await opacity(chip(p,'Planning sync')),'completed chip is dimmer');
    assert.match(await label(p,'Dentist visit'),/Dentist visit.*Completed/);
    assert.equal(await chip(p,'Dentist visit').getAttribute('title'),await label(p,'Dentist visit'),'tooltip carries the full title and status');
    assert((await chip(p,'Dentist visit').locator('strong').boundingBox()).width <= 1,'title is visually hidden while collapsed');
    assert.equal(await chip(p,'Dentist visit').locator('.calendar-status-mark').isVisible(),true,'checkmark shown');
    assert(moved.width >= plan.width*0.6 && await chip(p,'Move review').locator('strong').isVisible(),'Rescheduled keeps a normal-width chip');
    assert.match(await label(p,'Move review'),/Rescheduled/);
    assert.equal(await chip(p,'Move review').locator('.calendar-status-mark').isVisible(),true,'Rescheduled has an icon, not color alone');
    assert.equal(await chip(p,'Planning sync').locator('.calendar-status-mark').count(),0,'Pending stays visually quiet');
    assert(lunch.width >= plan.width*0.6 && await opacity(chip(p,'Lunch with Sam')) < 1,'personal Cancelled stays visible and muted');
    assert.equal(await chip(p,'Lunch with Sam').locator('strong').evaluate(el => getComputedStyle(el).textDecorationLine),'line-through');
    assert.match(await label(p,'Lunch with Sam'),/Cancelled/);
    assert.equal(await focus.getByRole('button',{name:/Provider cancelled/}).count(),0,'provider-cancelled events keep their existing omission');
    await p.screenshot({path:`${shots}/focus-desktop.png`,clip:{x:0,y:0,width:1500,height:260}});

    // Keyboard opens the same modal; full title and Personal status are visible; Pending restores the full chip.
    await chip(p,'Dentist visit').focus(); await p.keyboard.press('Enter');
    await dialog(p).getByText('Dentist visit',{exact:true}).first().waitFor();
    assert.equal(await dialog(p).getByLabel('Personal status',{exact:true}).inputValue(),'completed');
    await dialog(p).getByText('Only for you; guests aren’t notified.',{exact:false}).waitFor();
    assert.equal(await dialog(p).getByRole('button',{name:'Save status',exact:true}).isDisabled(),true,'unchanged status is not saved');
    assert.equal(await dialog(p).getByRole('button',{name:'Edit',exact:true}).count(),1,'event editing is still separate');
    await pickStatus(p,'pending');
    await noteOf(p).filter({hasText:'Saved: Pending'}).waitFor();
    assert.deepEqual({...statusPosts.at(-1),request_id:undefined},{...target('s-dentist'),status:'pending',expected_revision:1,request_id:undefined});
    assert.match(statusPosts.at(-1).request_id,/^[0-9a-f-]{36}$/);
    await closeDialog(p);
    await waitLabel(p,'Dentist visit',/^(?!.*Completed)/);
    assert((await chip(p,'Dentist visit').boundingBox()).width >= plan.width*0.6 && await chip(p,'Dentist visit').locator('strong').isVisible(),'Pending restores the full chip');

    // All four statuses persist through reload.
    for (const [status,re,expected] of [['completed',/Completed/,1],['cancelled',/Cancelled/,2],['rescheduled',/Rescheduled/,3],['pending',/^(?!.*(Completed|Cancelled|Rescheduled))/,4]]) {
      await chip(p,'Planning sync').click();
      await pickStatus(p,status);
      await noteOf(p).filter({hasText:`Saved: ${status[0].toUpperCase()+status.slice(1)}`}).waitFor();
      await closeDialog(p);
      await waitLabel(p,'Planning sync',re);
      await p.reload(); await chip(p,'Planning sync').waitFor(); await waitLabel(p,'Planning sync',re);
      assert.equal(statusPosts.at(-1).status,status); assert.equal(statuses.get(statusKey({...target('s-plan')})).revision,expected);
    }
    assert.deepEqual(statusPosts.filter(post => post.event_id === 's-plan').map(post => post.expected_revision),[0,1,2,3],'expected_revision follows the displayed annotation');
    assert.equal(providerOps.length,opsBefore,'status saves never call provider update/delete/respond');

    // A lost response is uncertain: no success shown, and the identical retry reuses its request id.
    statusState.postMode = 'lost';
    await chip(p,'Dentist visit').click();
    await pickStatus(p,'completed');
    await noteOf(p).filter({hasText:'Couldn’t confirm the save'}).waitFor();
    assert.equal(await noteOf(p).filter({hasText:'Not saved'}).count(),0,'an uncertain save is not reported as unsaved');
    assert.equal(await noteOf(p).filter({hasText:/^Saved/}).count(),0);
    assert.equal(await dialog(p).getByRole('button',{name:'Save status',exact:true}).isDisabled(),false,'retry stays available');
    assert.match(await label(p,'Dentist visit'),/^(?!.*Completed)/,'failed save does not repaint the chip');
    statusState.postMode = null;
    await dialog(p).getByRole('button',{name:'Save status',exact:true}).click();
    await noteOf(p).filter({hasText:'Saved: Completed'}).waitFor();
    const retries = statusPosts.filter(post => post.event_id === 's-dentist').slice(-2);
    assert.equal(retries[0].request_id,retries[1].request_id,'identical retry keeps the request id');
    await closeDialog(p); await waitLabel(p,'Dentist visit',/Completed/);

    // A stale save reads current preferences and stays recoverable.
    await chip(p,'Dentist visit').click();
    putStatus(target('s-dentist'),'rescheduled');   // another browser changed it
    await pickStatus(p,'pending');
    await noteOf(p).filter({hasText:'Changed elsewhere'}).waitFor();
    assert.equal(await dialog(p).getByLabel('Personal status',{exact:true}).inputValue(),'rescheduled');
    await waitLabel(p,'Dentist visit',/Rescheduled/);
    await pickStatus(p,'pending');
    await noteOf(p).filter({hasText:'Saved: Pending'}).waitFor();
    assert.equal(statusPosts.at(-1).expected_revision,statuses.get(statusKey(target('s-dentist'))).revision-1);
    await closeDialog(p); await waitLabel(p,'Dentist visit',/^(?!.*(Completed|Rescheduled))/);

    // Recurring occurrences with identical event_id never bleed, even when detail returns the series master.
    const standupA = chip(p,'Standup · 11:00'), standupB = chip(p,'Standup · 2:00');
    await standupA.click();
    await dialog(p).getByText('occurrence only',{exact:false}).waitFor();
    await pickStatus(p,'completed');
    await noteOf(p).filter({hasText:'Saved: Completed'}).waitFor();
    assert.deepEqual({...statusPosts.at(-1),request_id:undefined},{...target('cal-href-1','2026-10-14T15:00:00.000Z','business','work'),status:'completed',expected_revision:0,request_id:undefined});
    await closeDialog(p);
    await waitLabel(p,'Standup · 11:00',/Completed/);
    assert.match(await standupB.getAttribute('aria-label'),/^(?!.*Completed)/,'the other occurrence is untouched');
    assert.equal((await standupB.locator('strong').boundingBox()).width > 1,true);
    await standupB.click();
    await dialog(p).getByLabel('Personal status',{exact:true}).waitFor();
    assert.equal(await dialog(p).getByLabel('Personal status',{exact:true}).inputValue(),'pending');
    await closeDialog(p);

    // The drawer Calendar shares the saved labels, keeps grid geometry, and a drawer save reaches Focus.
    await p.getByRole('button',{name:"Open today's calendar",exact:true}).click();
    const drawer = p.locator('#taskspanel .calendar-embedded');
    await drawer.locator('.calendar-event',{hasText:'Planning sync'}).waitFor();
    await drawer.getByRole('button',{name:/Load more|^$/}).count();
    await drawer.locator('.calendar-event',{hasText:'Lunch with Sam'}).waitFor();
    assert.equal(await drawer.locator('.calendar-event',{hasText:'Standup'}).count(),2,'identical-id occurrences both render');
    const grid = drawer.locator('.calendar-event.calendar-status-completed',{hasText:'Standup'});
    assert.equal(await grid.count(),1);
    assert.equal(await grid.evaluate(el => el.style.height),'32px','completed drawer events keep their grid geometry');
    assert(await grid.locator('strong').isVisible(),'drawer events keep their title');
    await p.screenshot({path:`${shots}/calendar-drawer-desktop.png`});
    await drawer.locator('.calendar-event',{hasText:'Lunch with Sam'}).click();
    await pickStatus(p,'pending');
    await noteOf(p).filter({hasText:'Saved: Pending'}).waitFor();
    await closeDialog(p);
    await waitLabel(p,'Lunch with Sam',/^(?!.*Cancelled)/);   // the Focus mount behind the drawer repainted without a reload

    // Slow reads cannot undo a just-saved annotation; concurrent mounts share one read.
    statusState.delay = 600;
    const statusReadsBefore = statusState.reads;
    await p.evaluate(() => { window.slowRead = calendarStatuses.refresh(); calendarStatuses.refresh(); });
    await p.evaluate(async () => { const t = {connection_id:'personal',calendar_id:'primary',event_id:'s-lunch',occurrence_start:''};
      await calendarStatuses.save(t,'completed',calendarStatuses.get(t).revision,'race-save-1'); });
    await p.evaluate(() => window.slowRead);
    assert.equal(statusState.reads-statusReadsBefore,1,'concurrent refreshes share one read');
    assert.equal(await p.evaluate(() => calendarStatuses.get({connection_id:'personal',calendar_id:'primary',event_id:'s-lunch',occurrence_start:''}).status),'completed','a slower, older read does not revert the save');
    statusState.delay = 0;
    await waitLabel(p,'Lunch with Sam',/Completed/);

    // Transient read failure keeps the last known labels and shows the failure; content stays usable.
    statusState.fail = 503;
    await drawer.getByRole('button',{name:'Refresh calendars',exact:true}).click();
    await drawer.locator('.calendar-status',{hasText:'showing the last known'}).waitFor();
    await waitLabel(p,'Lunch with Sam',/Completed/);
    await drawer.locator('.calendar-event',{hasText:'Planning sync'}).click();
    await dialog(p).getByText('Planning sync',{exact:true}).first().waitFor();
    assert.equal(await dialog(p).getByLabel('Personal status',{exact:true}).isDisabled(),false,'known preferences stay editable after a transient read failure');
    await closeDialog(p);
    statusState.fail = null;
    await drawer.getByRole('button',{name:'Refresh calendars',exact:true}).click();
    await drawer.locator('.calendar-event',{hasText:'Planning sync'}).waitFor();
    await p.waitForFunction(() => !document.querySelector('#taskspanel .calendar-status').textContent.includes('last known'));

    // Lost authorization clears private statuses and disables editing.
    statusState.fail = 401;
    await drawer.getByRole('button',{name:'Refresh calendars',exact:true}).click();
    await waitLabel(p,'Lunch with Sam',/^(?!.*Completed)/);
    await drawer.locator('.calendar-event',{hasText:'Planning sync'}).click();
    await noteOf(p).filter({hasText:'unavailable'}).waitFor();
    assert.equal(await dialog(p).getByLabel('Personal status',{exact:true}).isDisabled(),true);
    assert.equal(await dialog(p).getByRole('button',{name:'Save status',exact:true}).isDisabled(),true);
    await closeDialog(p);
    statusState.fail = null;
    await drawer.getByRole('button',{name:'Refresh calendars',exact:true}).click();
    await waitLabel(p,'Lunch with Sam',/Completed/);
    assert.equal(providerOps.length,opsBefore,'no provider writes anywhere in the status flows');
    await p.close();

    // First read failure: status is unknown (never asserted Pending) and cannot be written until a retry succeeds.
    seed();
    statusState.fail = 503;
    p = await fresh(); focus = p.getByRole('region',{name:"Today's calendar",exact:true});
    await chip(p,'Dentist visit').waitFor();
    await focus.getByRole('status').filter({hasText:'Personal statuses unavailable'}).waitFor();
    assert((await chip(p,'Dentist visit').locator('strong').boundingBox()).width > 1,'unknown status does not collapse a chip');
    assert.match(await label(p,'Dentist visit'),/^(?!.*(Pending|Completed))/);
    await chip(p,'Planning sync').click();
    await noteOf(p).filter({hasText:'Status unknown'}).waitFor();
    assert.equal(await dialog(p).getByLabel('Personal status',{exact:true}).isDisabled(),true);
    assert.equal(await dialog(p).getByRole('button',{name:'Save status',exact:true}).isDisabled(),true);
    const before = statusPosts.length;
    statusState.fail = null;
    await dialog(p).getByRole('button',{name:'Retry',exact:true}).click();
    await p.waitForFunction(() => !document.querySelector('[role=dialog] select[name=personal_status]').disabled);
    assert.equal(statusPosts.length,before,'nothing was written against a guessed revision');
    await closeDialog(p);
    await waitLabel(p,'Dentist visit',/Completed/);
    await p.close();

    // Opaque ids never collide in the browser cache, even with NUL characters.
    seed();
    p = await fresh();
    await chip(p,'Planning sync').waitFor();
    const nulA = target('d','','a\u0000b','c'), nulB = target('d','','a','b\u0000c');
    assert.equal(await p.evaluate(([a,b]) => calendarStatusKey(a) !== calendarStatusKey(b),[nulA,nulB]),true);
    await p.evaluate(async ([a,b]) => { await calendarStatuses.save(a,'completed',0,'nul-a'); await calendarStatuses.save(b,'cancelled',0,'nul-b'); },[nulA,nulB]);
    assert.deepEqual(await p.evaluate(([a,b]) => [calendarStatuses.get(a).status,calendarStatuses.get(b).status],[nulA,nulB]),['completed','cancelled']);

    // A transient provider refresh failure keeps the visible queue, and a label save still repaints it.
    statusState.calendarsFail = true;
    await p.clock.fastForward(61000);
    focus = p.getByRole('region',{name:"Today's calendar",exact:true});
    await focus.getByRole('status').filter({hasText:'Calendar service temporarily unavailable'}).waitFor();
    assert.equal(await focus.locator('.calendar-queue-event').count(),7,'last loaded queue stays visible');
    await chip(p,'Planning sync').click();
    await pickStatus(p,'completed');
    await noteOf(p).filter({hasText:'Saved: Completed'}).waitFor();
    await closeDialog(p);
    await waitLabel(p,'Planning sync',/Completed/);
    statusState.calendarsFail = false;
    await p.close();

    // An editor opened before the first read settles follows the store; a dirty draft keeps its own revision.
    seed();
    statusState.delay = 2500;
    p = await fresh();
    await p.getByRole('button',{name:"Open today's calendar",exact:true}).click();
    const drawerEvent = p.locator('#taskspanel .calendar-event',{hasText:'Dentist visit'});
    await drawerEvent.waitFor();
    await drawerEvent.click();
    await noteOf(p).filter({hasText:'Status unknown'}).waitFor();
    const select = dialog(p).getByLabel('Personal status',{exact:true});
    assert.equal(await select.isDisabled(),true);
    await p.waitForFunction(() => !document.querySelector('[role=dialog] select[name=personal_status]').disabled);
    assert.equal(await select.inputValue(),'completed','the late first read reaches the open editor');
    assert.equal(await noteOf(p).filter({hasText:'Status unknown'}).count(),0,'stale unknown message cleared');
    statusState.delay = 0;
    putStatus(target('s-dentist'),'rescheduled');
    await p.evaluate(() => calendarStatuses.refresh());
    await p.waitForFunction(() => document.querySelector('[role=dialog] select[name=personal_status]').value === 'rescheduled');
    await select.selectOption('pending');   // dirty draft
    putStatus(target('s-dentist'),'cancelled');
    await p.evaluate(() => calendarStatuses.refresh());
    await noteOf(p).filter({hasText:'Changed elsewhere'}).waitFor();
    assert.equal(await select.inputValue(),'pending','a dirty draft is never rebased');
    const postsBefore = statusPosts.length;
    await dialog(p).getByRole('button',{name:'Save status',exact:true}).click();
    await noteOf(p).filter({hasText:'Now Cancelled'}).waitFor();
    assert.equal(statusPosts.length,postsBefore+1);
    assert.equal(statusPosts.at(-1).expected_revision,2,'the draft is posted against its original revision and conflicts');
    assert.equal(await select.inputValue(),'cancelled');
    await p.close();

    // Narrow layout and screenshots with varied statuses.
    seed();
    putStatus(target('cal-href-1','2026-10-14T15:00:00.000Z','business','work'),'completed');
    p = await fresh({width:800,height:900}); focus = p.getByRole('region',{name:"Today's calendar",exact:true});
    await waitLabel(p,'Dentist visit',/Completed/); await waitLabel(p,'Standup · 11:00',/Completed/);
    assert(await focus.locator('.calendar-today-queue').evaluate(el => el.scrollWidth > el.clientWidth),'narrow queue scrolls');
    assert(await p.locator('#agpad-body').evaluate(el => el.scrollWidth <= el.clientWidth),'narrow pane stays within its width');
    const nplan = await chip(p,'Planning sync').boundingBox(), ndentist = await chip(p,'Dentist visit').boundingBox();
    assert.equal(nplan.height,ndentist.height); assert(ndentist.width < nplan.width);
    await p.screenshot({path:`${shots}/focus-narrow.png`,clip:{x:0,y:0,width:800,height:260}});
    await p.getByRole('button',{name:"Open today's calendar",exact:true}).click();
    await p.locator('#taskspanel .calendar-event',{hasText:'Dentist visit'}).waitFor();
    await p.screenshot({path:`${shots}/calendar-drawer-narrow.png`});
    await p.close();
    assert.deepEqual(pageErrors,[]);
    assert.deepEqual(errors,[]);
    console.log('PASS: Ordubashy Calendar, personal statuses (collapsed completed chips, four states, persistence, stale/uncertain saves, recurring occurrences, shared mounts, read failure, lost access), compact horizontal Today queue, vertical Calendar, edit, pagination, local dates, refresh, partial errors, empty states, lifecycle, role scope, narrow pane and global Calendar');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode=1; });
