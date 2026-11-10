'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
let chromium;
try { ({chromium}=require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const available=chromium && fs.existsSync(process.env.CHROME_PATH || chromium.executablePath());
const browserTest=(name,fn)=>test(name,{skip:!available && 'Local Playwright Chromium is unavailable'},fn);

async function fixture(width=720) {
  const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH || undefined});
  const page=await browser.newPage({viewport:{width,height:700}});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.setContent('<div id="agpad" class="agpad"><div id="agpad-body" class="tbody" style="height:650px"></div></div>');
  for (const file of ['app.css','css/tasks.css','css/agent-focus.css']) await page.addStyleTag({path:path.join(__dirname,'..',file)});
  await page.evaluate(()=>{
    window.$=(selector,host=document)=>host.querySelector(selector);
    window.state={bizs:[{id:'project-id',slug:'project'}]};
    window.esc=s=>String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    window.agentRole=a=>a?.role?.id || a?.role_id;
    const profile=(id,name,role='global-worker',status='active')=>({agent_id:id,display_name:name,role:{id:role},status,scope:{kind:'ecosystem'}});
    window.agents=[profile('atlas','Atlas'),profile('ord','Ordubashy','ordubashy'),profile('cedar','Cedar'),profile('lens','Lens'),profile('learning','Learning','learning-applier'),profile('retired','Retired','global-worker','retired')];
    window.globalRosterAgent=a=>a.status==='active' && a.scope.kind==='ecosystem';
    window.globalAgentProfile=id=>agents.find(a=>a.agent_id===id);
    window.identityAvatar=(_kind,name)=>'<span class="identity-avatar" aria-hidden="true">'+esc(name[0])+'</span>';
    window.SVG_X='<svg></svg>';window.coreData=x=>x;window.newRequestId=()=> 'request';window.toast=()=>{};
    window.accessLost=e=>['forbidden','invalid_principal'].includes(e?.code);
    window.taskStateObserve=(_scope,task)=>task;window.taskStateSubscribe=()=>()=>{};window.taskStateBind=()=>{};
    window.taskStateSet=(scope,task)=>{window.stateChange={scope,id:task.id};};window.tNext=s=>s;
    window.taskShowsState=()=>true;window.customerStateText=()=>'';
    window.agfocusContext=()=>{};window.agfocusUnread=()=>{};window.agfocusOverlayClose=()=>{};window.agfocusOverlayOpen=()=>{};
    window.agfocusRowHtml=()=>'<div class="trow agfocus-row"><span class="agfocus-notification"></span><span class="agfocus-marker"></span><span class="agfocus-scope"></span><div class="task-headline"><p class="ttl"></p><span class="agfocus-flag" hidden></span></div><div class="agfocus-actions"></div></div>';
    window.rwRecordFromItem=({source})=>({source,can_thread:true});
    window.opened=[];window.keepEditor=false;
    window.recordWorkspaceMount=(_host,options)=>{
      let open=false,alive=true;
      return {open:async record=>{open=true;opened.push(record.source);return true;},alive:()=>alive,isOpen:()=>open,
        close:async()=>{if(keepEditor)return;open=false;options.onClose();},dispose:()=>{open=false;alive=false;}};
    };
    window.agpadActive=p=>p.active;
    window.p={agentId:'ord',agent:'Ordubashy',view:'attentionBoard',active:true,el:$('#agpad')};
    window.item=(id,title,business=null,status='open',workers=[])=>({membership_id:'member-'+id,membership_revision:1,task_ref:{type:'task',id,business_id:business},
      task:{id,title,status,revision:1,worker_ids:workers,workers:workers.map(id=>({id,display_name:id,kind:'agent'}))}});
    window.boards={
      ord:{agent_id:'ord',owner_id:'owner',attention:{state:'focused',focus:{type:'task',id:'shared-id',business_id:null,label:'Coordinate launch'},intent:'Check the remaining launch tasks.'},items:[item('shared-id','Global task',null,'working',['owner'])],next_cursor:null},
      atlas:{agent_id:'atlas',owner_id:'owner',attention:{state:'focused',focus:{type:'task',id:'shared-id',business_id:'project-id',label:'Project research'},intent:'Verify the sources <img src=x onerror=alert(1)>.'},items:[item('shared-id','Project task','project-id','open',['atlas']),item('extra','Another task')],next_cursor:null},
      cedar:{agent_id:'cedar',owner_id:'owner',attention:{state:'focused',focus:{type:'workflow',id:'no-membership',label:'Focused but empty'},intent:'Not a Todo membership.'},items:[],next_cursor:null},
      lens:{agent_id:'lens',owner_id:'owner',attention:{state:'unfocused'},items:[item('finished','Completed, still on Todo',null,'done')],next_cursor:null}
    };
    window.calls=[];window.readOverride=null;window.removeOverride=null;window.directoryError=null;
    window.api=async()=>{if(directoryError)throw directoryError;return {agents:structuredClone(agents)};};
    window.post=async(url,args)=>{
      calls.push({url,args:structuredClone(args)});
      if (url.endsWith('/todo_remove')) {
        if (removeOverride) return removeOverride(args);
        boards[args.agent_id].items=boards[args.agent_id].items.filter(item=>item.membership_id!==args.membership_id);
        return {};
      }
      if (readOverride) {const result=await readOverride(args);if(result!==undefined)return result;}
      const data=structuredClone(boards[args.agent_id]);
      if(args.limit===1)data.items=data.items.slice(0,1);
      return data;
    };
    window.workCall=async(scope,_operation,{task_id})=>({task:boards[scope?'atlas':'ord'].items.find(item=>item.task.id===task_id)?.task});
  });
  for (const file of ['attention-board','attention-agents']) await page.addScriptTag({path:path.join(__dirname,'../js',file+'.js')});
  await page.evaluate(async()=>{attentionBoardMount(p);await p.attentionCache.run?.promise;await p.attentionAgents.run?.promise;});
  return {browser,page,errors};
}
const agentIds=page=>page.locator('[data-attention-agent]').evaluateAll(rows=>rows.map(row=>row.dataset.attentionAgent));
const titles=page=>page.locator('[data-attention-list] .ttl').allTextContents();
async function select(page,id) {
  await page.locator('[data-attention-agent="'+id+'"]').click();
  await page.waitForFunction(id=>p.attentionAgentId===id && !p.attentionCache.run,id);
}
async function refresh(page) {
  await page.evaluate(async()=>{p.attentionAgents.retryAt=0;await attentionAgentsRefresh(p);});
}

browserTest('only nonempty active agent boards appear, with recorded focus and intent',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    assert.deepEqual(await agentIds(page),['ord','atlas','lens']);
    assert.equal(await page.locator('[data-attention-agent=atlas] .attention-agent-intent').textContent(),'Verify the sources <img src=x onerror=alert(1)>.');
    assert.equal(await page.locator('[data-attention-agent] img').count(),0);
    assert.equal(await page.locator('[data-attention-agent=lens] .attention-agent-focus').textContent(),'No focus set');
    assert.equal(await page.locator('.attention-queue').count(),0);
    assert.equal(await page.evaluate(()=>calls.some(call=>['learning','retired'].includes(call.args.agent_id))),false);
    assert.equal(await page.evaluate(()=>calls.some(call=>!call.url.endsWith('/todo_get'))),false);
    await page.evaluate(()=>{boards.cedar.items=[item('new','New task')];boards.lens.items=[];});
    await refresh(page);
    assert.deepEqual(await agentIds(page),['ord','atlas','cedar']);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('agent selection keeps chat identity and opens the exact scoped task',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await select(page,'atlas');
    assert.equal(await page.evaluate(()=>p.agentId),'ord');
    assert.deepEqual(await titles(page),['Project task','Another task']);
    assert.equal(await page.locator('.attention-list-heading h2').textContent(),'Atlas’s Todo');
    assert.equal(await page.locator('[data-attention-agent=atlas]').getAttribute('aria-pressed'),'true');
    await page.locator('[data-attention-list] .ttl').first().click();
    assert.deepEqual(await page.evaluate(()=>({id:opened[0].id,business:opened[0].business_id,slug:opened[0].business_slug})),{id:'shared-id',business:'project-id',slug:'project'});
    await page.evaluate(()=>{keepEditor=true;});
    await page.locator('[data-attention-agent=ord]').click();
    assert.equal(await page.evaluate(()=>p.attentionAgentId),'atlas');
    await page.evaluate(()=>{keepEditor=false;});
    await select(page,'ord');
    assert.deepEqual(await titles(page),['Global task']);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('removing a worker membership while switching agents never touches the other board',async()=>{
  const {browser,page}=await fixture();
  try {
    await select(page,'atlas');
    await page.evaluate(()=>{removeOverride=args=>new Promise(resolve=>{finishRemoval=()=>{boards[args.agent_id].items=boards[args.agent_id].items.filter(item=>item.membership_id!==args.membership_id);resolve({});};});});
    await page.getByRole('button',{name:'Remove Project task from Todo',exact:true}).click();
    await select(page,'ord');
    await page.evaluate(()=>finishRemoval());
    assert.deepEqual(await titles(page),['Global task']);
    assert.deepEqual(await page.evaluate(()=>calls.filter(call=>call.url.endsWith('/todo_remove')).map(call=>({agent:call.args.agent_id,member:call.args.membership_id}))),[{agent:'atlas',member:'member-shared-id'}]);
    await select(page,'atlas');
    assert.deepEqual(await titles(page),['Another task']);
  } finally {await browser.close();}
});

browserTest('full board pagination cannot replace a different selected agent with a late page',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      readOverride=args=>{
        if(args.agent_id!=='atlas' || args.limit===1)return;
        if(!args.cursor)return {...structuredClone(boards.atlas),items:[boards.atlas.items[0]],next_cursor:'page-2'};
        return new Promise(resolve=>{finishPage=()=>resolve({...structuredClone(boards.atlas),items:[boards.atlas.items[1]],next_cursor:null});});
      };
    });
    await page.locator('[data-attention-agent=atlas]').click();
    await page.waitForFunction(()=>typeof finishPage==='function');
    assert.deepEqual(await titles(page),['Project task']);
    await select(page,'ord');
    await page.evaluate(()=>finishPage());
    await page.waitForFunction(()=>!p.attentionCaches.get('atlas').run);
    assert.deepEqual(await titles(page),['Global task']);
    await page.evaluate(()=>{readOverride=args=>args.agent_id==='atlas' && args.limit!==1
      ? {...structuredClone(boards.atlas),items:[boards.atlas.items[args.cursor?1:0]],next_cursor:args.cursor?null:'page-2'} : undefined;});
    await select(page,'atlas');
    assert.deepEqual(await titles(page),['Project task','Another task']);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('refresh failures stay distinct from empty boards; access loss clears cached records',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{readOverride=args=>{if(args.agent_id==='atlas')throw Error('Offline');};});
    await refresh(page);
    assert.deepEqual(await agentIds(page),['ord','atlas','lens']);
    assert.equal(await page.locator('[data-attention-agent=atlas] .attention-agent-stale').textContent(),'Couldn’t refresh');
    await page.evaluate(()=>{readOverride=null;});
    await select(page,'atlas');
    await page.locator('[data-attention-list] .ttl').first().click();
    await page.evaluate(()=>{directoryError=Object.assign(Error('Access denied'),{code:'forbidden'});});
    await refresh(page);
    assert.deepEqual(await agentIds(page),[]);
    assert.deepEqual(await titles(page),[]);
    assert.equal(await page.locator('[data-attention-record]').isVisible(),false);
    assert.equal(await page.evaluate(()=>[...p.attentionCaches.values()].some(cache=>cache.data!==null)),false);
    assert.match(await page.locator('[data-attention-error]').textContent(),/Access denied/);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('narrow panes support keyboard selection, the owner filter and full intent without overflow',async()=>{
  const {browser,page,errors}=await fixture(320);
  try {
    await page.evaluate(()=>{boards.atlas.attention.intent='A long recorded intention. '.repeat(35);});
    await refresh(page);
    await page.locator('[data-attention-agent=atlas]').focus();await page.keyboard.press('Enter');
    await page.waitForFunction(()=>p.attentionAgentId==='atlas' && !p.attentionCache.run);
    assert.equal(await page.locator('.attention-intent').textContent(),'A long recorded intention. '.repeat(35));
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.locator('.attention-owner-toggle').click();
    assert.deepEqual(await titles(page),[]);
    assert.match(await page.locator('[data-attention-list]').textContent(),/No tasks for this filter/);
    assert.deepEqual(await agentIds(page),['ord','atlas','lens']);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('leaving Todo stops overview batches and ignores an in-flight response',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      calls=[];readOverride=args=>args.agent_id==='atlas' ? new Promise(resolve=>{finishOverview=()=>resolve(structuredClone(boards.atlas));}) : undefined;
      p.attentionAgents.retryAt=0;window.refreshing=attentionAgentsRefresh(p);
    });
    await page.waitForFunction(()=>typeof finishOverview==='function');
    await page.evaluate(async()=>{p.workspaceDispose();p.view='focus';finishOverview();await refreshing;});
    assert.equal(await page.evaluate(()=>calls.some(call=>call.args.agent_id==='lens')),false);
    assert.equal(await page.evaluate(()=>p.attentionBoard),null);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});
