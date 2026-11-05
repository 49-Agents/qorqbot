#!/usr/bin/env node
// The real static app with synthetic read-only activity and an inline task editor.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'..'), calls = [], errors = [];
const now = '2026-09-13T12:00:00Z', bizId = 'business-one';
// Three global agents, then 416 former business agents the Fleet must not list.
const globalRoles = ['ordubashy','learning-applier','global-worker'];
const agents = Array.from({length:420},(_,i) => ({agent_id:'agent-'+i,name:'agent-'+i,display_name:i ? 'Agent '+i : 'Ordubashy',role_id:globalRoles[i] || 'coder',business_id:i>2 ? bizId : null,business_slug:i>2 ? 'studio' : null,business_name:i>2 ? 'Example Studio' : 'Global',state:i%4===0?'running':i%4===1?'idle':i%4===2?'offline':'unknown',last_seen_at:i%4===3?null:now,sessions:[]}));
agents[0].sessions = [
  {session_id:'c1',runtime:'codex',state:'running',last_seen_at:now,work_updated_at:now,items:[{summary:'Prepare launch review',business_id:bizId,task_id:'launch',title:'Launch plan',blocked:false,blockers:[]}]},
  {session_id:'c2',runtime:'claude-code',state:'running',last_seen_at:now,work_updated_at:now,items:[{summary:'Coordinate campaign',business_id:bizId,task_id:null,blocked:true,blockers:[{title:'Supply Pixel ID',task_id:'pixel'}]}]},
];
agents[1].sessions = [{session_id:'idle',runtime:'claude-code',state:'idle',last_seen_at:now,work_updated_at:now,items:[{summary:'Review completed',business_id:bizId,task_id:null,blocked:false,blockers:[]}]}];
agents[2].sessions = [{session_id:'old',runtime:'codex',state:'offline',last_seen_at:'2026-09-13T09:00:00Z',work_updated_at:'2026-09-13T09:00:00Z',items:[{summary:'<img src=x onerror=alert(1)> Earlier task',business_id:bizId,task_id:null,blocked:false,blockers:[]}]}];
// A worker's one authoritative assignment is its current_work; its historical session items are never extra jobs.
agents[2].current_work = {revision:3,item:{summary:'Draft <b>pricing</b> brief',business_id:bizId,business_slug:'studio',task_id:'launch',title:'Launch plan',blocked:true,blocked_reason:'Waiting for <i>mailbox</i> access'},updated_at:now,reviewed_at:'2026-09-13T08:00:00Z',stale:true};
agents.push({agent_id:'worker-idle',name:'worker-idle',display_name:'Idle Worker',role_id:'global-worker',business_id:null,business_slug:null,business_name:'Global',state:'idle',last_seen_at:now,sessions:[{session_id:'hist',runtime:'claude-code',state:'idle',last_seen_at:now,work_updated_at:now,items:[{summary:'Historical session job',business_id:null,task_id:null}]}],inbox:null,current_work:{revision:0,item:null,updated_at:null,reviewed_at:null,stale:false}});
const notes = Array.from({length:45},(_,i)=>({id:'note-'+i,body:'Progress '+i,created_by:'coder',created_by_name:'Coder',created_at:`2026-09-13T${String(Math.floor(i/6)).padStart(2,'0')}:${String(i%6*10).padStart(2,'0')}:00Z`,refs:i===44?[{type:'signoff',id:'approval',label:'Pixel approval',state:'applied'},{type:'signoff',id:'gone',label:'Old request',unavailable:true},{unavailable:true}]:[]}));
notes[0].at = notes[0].created_at; delete notes[0].created_at;
const task = {id:'launch',title:'Launch plan',status:'doing',owners:['agent:coder'],assignee_ids:['coder'],by:'agent:coder',created:'2026-09-12T12:00:00Z',updated:now,revision:1,note_count:45,notes_next_cursor:'25',notes:notes.slice(25),blocked_by:[],tags:[]};
let fail = false, held = null;
const server = http.createServer(async(req,res) => {
  const url = new URL(req.url,'http://localhost');
  if (url.pathname.startsWith('/api/')) {
    let text = ''; for await(const c of req) text+=c;
    const body = text ? JSON.parse(text) : {}; calls.push({url:req.url,body,method:req.method});
    const ok = data => ({status:'ok',data}); let data = [];
    if (url.pathname === '/api/biz') data = [{id:bizId,slug:'studio',name:'Example Studio',agents:[]}];
    else if (url.pathname === '/api/session') data = ok({principal_id:'owner'});
    else if (url.pathname === '/api/me/tasks') data = {principal_id:'owner',tasks:[]};
    else if (url.pathname === '/api/me/agents/activity') {
      if (held) {const gate=held;held=null;await gate;}
      if (fail) {res.writeHead(503,{'Content-Type':'application/json'}).end(JSON.stringify({status:'error',error:{message:'Temporary activity failure'}}));return;}
      const filtered = url.searchParams.has('agent_id') ? agents.filter(a=>a.agent_id===url.searchParams.get('agent_id')) : agents;
      const start = Number(url.searchParams.get('cursor') || 0), end = start+200;
      data = ok({agents:filtered.slice(start,end),next_cursor:end<filtered.length?String(end):null,as_of:now});
    } else if (url.pathname.endsWith('/task/launch')) data = {task};
    else if (url.pathname.endsWith('/tasks_notes_list')) {
      const end = Number(body.cursor || notes.length), start = Math.max(0,end-20);
      data = ok({notes:notes.slice(start,end),next_cursor:start?String(start):null});
    } else if (url.pathname.endsWith('/context_reviews_list')) data = ok({items:[],next_cursor:null});
    else if (url.pathname.endsWith('/live')) data = {sessions:[],signoffs_pending:0,platform_signoffs:[]};
    else if (url.pathname.endsWith('/log')) data = [];
    res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify(data));return;
  }
  const file = path.resolve(root,'.'+(url.pathname==='/'?'/index.html':url.pathname));
  if (!file.startsWith(root+path.sep)||!fs.existsSync(file)){res.writeHead(404).end();return;}
  res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'}).end(fs.readFileSync(file));
});
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const origin=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,args:process.env.CHROMIUM_SANDBOX==='0'?['--no-sandbox']:[]});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}}); page.setDefaultTimeout(10000);
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',r=>new URL(r.request().url()).origin===origin?r.continue():r.abort());
    await page.clock.install({time:new Date(now)});
    const activityCalls = () => calls.filter(c=>c.url.startsWith('/api/me/agents/activity')).length;
    // The retired Fleet page is gone: its URL lands on Agents and nothing here reads the activity feed for a page.
    await page.goto(origin+'/#/global/fleet');
    await page.waitForFunction(()=>location.hash==='#/global/agents'&&state.globalTab==='agents');
    assert.equal(await page.locator('[data-fleet-agent],[data-fleet-refresh],[data-global-tab="fleet"],[data-global-tab="overview"]').count(),0);
    // A taskless, titleless item still shows its blocked reason, and the card avatar wears the worker's chosen icon.
    const card = await page.evaluate(() => {
      globalAgentProfiles = [{agent_id:'w-icon',name:'wi',display_name:'Icon Worker',role:{id:'global-worker'},presentation:{department:'marketing',icon:'rocket'}}];
      const host = document.createElement('div');
      host.innerHTML = agentWorkHTML({agent_id:'w-icon',name:'wi',role_id:'global-worker',sessions:[],current_work:{revision:1,item:{summary:'Review campaign',blocked_reason:'Waiting for mailbox access'},updated_at:null,reviewed_at:null,stale:false}});
      const svg = document.createElementNS('http://www.w3.org/2000/svg','svg'); svg.innerHTML = AICONS.rocket;
      return {reason:host.querySelector('.fleet-blocked-reason')?.textContent, tag:host.querySelector('.sg-priority-tag')?.textContent, rocket:host.querySelector('.fleet-work-agent svg').innerHTML === svg.innerHTML};
    });
    assert.deepEqual(card, {reason:'Waiting for mailbox access', tag:'Blocked', rocket:true});
    // The existing agent pane shares the snapshot, without a terminal connection.
    await page.evaluate(()=>{
      const wrap=document.createElement('div');wrap.id='agent-pane-fixture';wrap.className='agsplit';wrap.innerHTML='<aside class="agpad" id="agpad"></aside>';document.body.appendChild(wrap);
      agpadMount('Ordubashy', wrap.firstElementChild, {agentId:'agent-0',biz:null,open:true,isCurrent:()=>true,views:['status']});
    });
    await page.locator('#agpad .fleet-work').first().waitFor();
    assert.equal(await page.locator('#agpad .fleet-work').count(),2);
    assert.equal(await page.locator('#agpad-body .agpad-current').count(),1);
    assert(await page.locator('#agpad [data-activity-state] .agent-provider svg').count() > 0);
    assert.equal(await page.locator('#agpad .fleet-work .agent-provider').count(),0);
    assert.equal(await page.locator('#agpad .agent-session').count(),0);
    assert.equal(await page.locator('#agpad .fleet-work-business').first().textContent(),'Example Studio');
    const workCard=page.locator('#agpad .fleet-work[data-work-task]');
    assert.equal(await workCard.getAttribute('role'),'group');
    assert.equal(await workCard.getAttribute('aria-expanded'),'false');
    assert.equal(await workCard.locator('.fleet-work-details').isVisible(),false);
    await page.evaluate(()=>{const el=document.querySelector('#agent-pane-fixture');el.style.cssText='position:fixed;inset:20px 20px auto auto;width:440px;height:620px;z-index:10;background:var(--bg);display:block';el.firstElementChild.style.cssText='width:100%;height:100%';});
    await page.locator('#agpad').screenshot({path:'/tmp/agent-status-pane.png'});
    assert(calls.some(c=>c.url==='/api/me/agents/activity?agent_id=agent-0'));
    await page.evaluate(()=>{
      const fixture=document.createElement('div');fixture.id='indicator-fixture';
      // The app pane header combines Status and Inbox into one overview tab.
      fixture.innerHTML='<button class="ttab" data-v="status"></button><span data-agent-overview="inbox"></span>';
      document.body.appendChild(fixture);
      window.indicatorPane={el:fixture,agentId:'indicator-agent'};
      agentNavSnapshots.set('indicator-agent',{state:'running',inbox:{state:'pending',reason:'offline',last_attempt_at:null}});
      agentPaneIndicators(indicatorPane);
    });
    assert.equal(await page.locator('#indicator-fixture [data-agent-overview="inbox"] .agpad-tab-dot').isVisible(),true);
    assert.equal(await page.locator('#indicator-fixture [data-v="status"] .agpad-tab-dot').getAttribute('data-state'),'running');
    await page.evaluate(()=>{agentNavSnapshots.set('indicator-agent',{state:'idle',inbox:{state:'idle',reason:null,last_attempt_at:null}});agentPaneIndicators(indicatorPane);});
    assert.equal(await page.locator('#indicator-fixture [data-agent-overview="inbox"] .agpad-tab-dot').isVisible(),false);
    assert.equal(await page.locator('#indicator-fixture [data-v="status"] .agpad-tab-dot').getAttribute('data-state'),'idle');
    await page.evaluate(()=>{agentNavSnapshots.delete('indicator-agent');agentPaneIndicators(indicatorPane);});
    assert.equal(await page.locator('#indicator-fixture [data-v="status"] .agpad-tab-dot').getAttribute('data-state'),'unknown');
    await page.locator('#indicator-fixture').evaluate(el=>el.remove());
    const beforePaneTick=activityCalls();
    await page.clock.fastForward(59000);assert.equal(activityCalls(),beforePaneTick);
    await page.clock.fastForward(1000);await page.waitForFunction(()=>!_agpad.activityPending);
    assert.equal(activityCalls()-beforePaneTick,1); // The selected agent's own snapshot.
    assert.deepEqual(await page.evaluate(()=>agpadGroups([
      {id:'created',status:'open',owners:['agent:beta'],created_by:'a',created_by_name:'Alpha',created_by_kind:'agent'},
      {id:'other',status:'open',owners:['agent:beta'],created_by:'h',created_by_name:'Alpha',created_by_kind:'human',notes:[{by:'agent:alpha'}]}
    ],'alpha').waiting.map(t=>t.id)),['created']);
    await workCard.getByRole('button',{name:'Manage work card',exact:true}).click();
    await page.getByRole('option',{name:'Expand details',exact:true}).click();
    await page.locator('.agfocus-detail .tdetail').waitFor();
    assert.equal(await page.getByRole('dialog').count(),0);
    assert.equal(await page.locator('.agfocus-detail .dtitle').textContent(),'Launch plan');
    await page.getByRole('button',{name:'← Agent status',exact:true}).click();
    await page.locator('#agpad .agpad-current').waitFor();
    assert.equal(await page.locator('.agfocus-detail').count(),0);
    await page.evaluate(()=>{_agpad=null;document.querySelector('#agent-pane-fixture').remove();});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:'/tmp/agent-fleet-mobile.png'});
    assert.deepEqual(errors,[]);
    assert.deepEqual(calls.filter(c=>c.method!=='GET'&&!/\/(dashboard_summary_get|tasks_get|global_progress_get|tasks_notes_list|context_reviews_list|tasks_config_get|assignees_list|stories_resource_get|metrics_kpis|owner_conversation_peers|owner_conversation_get)$/.test(c.url)),[]);
    console.log('Agent activity: Fleet page retired, status-pane work cards from current_work, presence indicators, 60-second polling, inline task details/back navigation and mobile layout passed.');
  } finally {await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
