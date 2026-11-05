'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require('/tmp/node_modules/playwright');

async function fixture() {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage();
  await page.setContent('<div id="controls">Focus controls</div><div id="host"></div><div id="chat">Agent chat</div>');
  await page.evaluate(() => {
    window.state={bizs:[{id:'p1',slug:'one',name:'One'},{id:'p2',slug:'two',name:'Two'}]};
    window.TASK_TYPE_LABELS={task:'Task'}; window.T_VIEW_ICON={tasks:'<svg></svg>'};
    window.taskTypeIcon=()=>''; window.esc=s=>String(s); window.modal=()=>{throw Error('overlay');};
    window.accessLost=()=>false;window.sourceGone=()=>false;window.uiFailure=e=>e.message || 'Failed';
    window.toast=()=>{};window.newRequestId=()=> 'same-key';window.coreData=x=>x;
    window.clearEtagCache=()=>{};window.taskStateForget=()=>{};window.storeInvalidateAll=()=>{};
    window.calls=[];window.movedEvents=[]; document.addEventListener('qorqut-task-moved',e=>movedEvents.push(e.detail));
    window.mode='success';window.dirty=false;window.hookResult=true;window.hookCount=0;
    window.post=async (url,args)=>{
      calls.push({url,args:structuredClone(args)});
      if(url.endsWith('tasks_move_targets'))return {projects:[{id:'p2',name:'Two'}]};
      if(url.endsWith('tasks_move_to_project_preview'))return {preview:{token:'preview-token',can_move:true,source:{name:args.source_business_id?'One':'Global'},destination:{name:'Two'},tasks:[{id:'t1',title:'Parent'},{id:'child',title:'Child'}],roots:[{previous_parent_id:null}],counts:{tasks:2}}};
      if(url.endsWith('tasks_move_to_project')) {
        if(mode==='lost')throw Error('connection lost');
        if(mode==='pending')return new Promise(resolve=>{window.resolveMove=()=>resolve({destination:{id:'p2',slug:'two',name:'Two'},moved:[{id:'t1',business_id:'p2'},{id:'child',business_id:'p2'}]});});
        return {destination:{id:'p2',slug:'two',name:'Two'},moved:[{id:'t1',business_id:'p2'},{id:'child',business_id:'p2'}]};
      }
      throw Error(url);
    };
  });
  for (const file of ['task-move.js','record-workspace.js']) await page.addScriptTag({path:path.join(__dirname,'../js',file)});
  await page.evaluate(() => {
    rwDashboardMount=async host=>{
      host.innerHTML='<input value="draft" aria-label="Draft">';
      return {dirty:()=>dirty,discard:()=>{dirty=false;},beforeLeave:async()=>{hookCount++;return hookResult;},dispose(){}};
    };
    window.ws=recordWorkspaceMount(document.querySelector('#host'),{entry:'focus',closeFirst:true});
    window.openTask=scope=>ws.open({source:{type:'task',id:'t1',business_id:scope},biz:scope?'one':null,label:'Parent',task_type:'task',can_thread:false});
  });
  return {browser,page};
}
const tick=page=>page.waitForTimeout(30);
for (const scope of [null,'p1']) test(`Focus move preview and cancel retains ${scope || 'Global'} record`,async()=>{
  const {browser,page}=await fixture(); try {
    await page.evaluate(scope=>openTask(scope),scope); await tick(page);
    await page.locator('[aria-label="Draft"]').fill('my draft');
    await page.locator('.rw-move').click();
    assert.equal(await page.locator('.overlay').count(),0);
    assert.equal(await page.locator('#controls').isVisible(),true);assert.equal(await page.locator('#chat').isVisible(),true);
    await page.selectOption('[data-move-destination]','p2');await page.locator('.tmove-tasks li').last().waitFor();
    const preview=await page.evaluate(()=>calls.find(c=>c.url.endsWith('tasks_move_to_project_preview')).args);
    assert.deepEqual(preview,{source_business_id:scope,destination_project_id:'p2',task_ids:['t1']});
    assert.match(await page.locator('.tmove-visibility').first().innerText(),/private/i);
    await page.locator('[data-move-cancel]').click();
    assert.equal(await page.locator('[aria-label="Draft"]').inputValue(),'my draft');
    assert.equal(await page.evaluate(()=>calls.filter(c=>c.url.endsWith('tasks_move_to_project')).length),0);
  } finally {await browser.close();}
});
test('dirty guard, child hook, uncertain retry and canonical replacement',async()=>{
  const {browser,page}=await fixture(); try {
    await page.evaluate(()=>openTask(null));await tick(page);
    await page.evaluate(()=>{dirty=true;hookResult=false;});await page.locator('.rw-move').click();await tick(page);
    assert.equal(await page.locator('.rw-move-panel').count(),0);
    await page.evaluate(()=>{hookResult=true;});await page.locator('.rw-move').click();
    assert.match(await page.locator('.rw-leave-choice').innerText(),/unsaved/);
    await page.getByRole('button',{name:'Keep editing'}).click();assert.equal(await page.evaluate(()=>dirty),true);
    await page.locator('.rw-move').click();await page.getByRole('button',{name:'Discard',exact:true}).click();
    await page.locator('[data-move-destination]').waitFor();await page.selectOption('[data-move-destination]','p2');
    await page.locator('[data-move-confirm]:not([disabled])').waitFor();await page.evaluate(()=>{mode='lost';});await page.locator('[data-move-confirm]').click();
    await page.getByText('Couldn’t confirm the move.',{exact:false}).waitFor();
    assert.equal(await page.locator('[data-move-destination]').isDisabled(),true);
    await page.locator('.rw-close').click();await page.getByText('The move outcome is unknown.',{exact:false}).waitFor();
    await page.getByRole('button',{name:'Keep',exact:true}).click();
    await page.evaluate(()=>{mode='success';});await page.locator('[data-move-confirm]').click();
    await page.waitForFunction(()=>ws.current()?.source.business_id==='p2');
    const requests=await page.evaluate(()=>calls.filter(c=>c.url.endsWith('tasks_move_to_project')).map(c=>c.args));
    assert.deepEqual(requests[0],requests[1]);assert.equal(requests[0].idempotency_key,'same-key');assert.equal(requests[0].preview_token,'preview-token');
    assert.equal(await page.evaluate(()=>movedEvents.length),1);
  } finally {await browser.close();}
});
test('pending move requires explicit leave and later success invalidates',async()=>{
  const {browser,page}=await fixture(); try {
    await page.evaluate(()=>openTask('p1'));await tick(page);await page.locator('.rw-move').click();
    await page.selectOption('[data-move-destination]','p2');await page.locator('[data-move-confirm]:not([disabled])').waitFor();
    await page.evaluate(()=>{mode='pending';});await page.locator('[data-move-confirm]').click();await page.locator('.rw-close').click();
    await page.getByText('The move is still in progress.',{exact:false}).waitFor();await page.getByRole('button',{name:'Leave',exact:true}).click();
    await page.waitForFunction(()=>!ws.isOpen());await page.evaluate(()=>resolveMove());await page.waitForFunction(()=>movedEvents.length===1);
    assert.equal(await page.locator('.overlay').count(),0);
  } finally {await browser.close();}
});

test('different agent selection and Focus controls await the inline dirty decision',async()=>{
  const {browser,page}=await fixture(); try {
    await page.evaluate(()=>openTask(null));await tick(page);
    const fs=require('node:fs');const src=fs.readFileSync(path.join(__dirname,'../js/global-agents.js'),'utf8');
    const select=src.slice(src.indexOf('  const select = (agent'),src.indexOf('  comingSoonRetry.onclick'));
    const open=src.slice(src.indexOf("  $('.global-agents', c).openAgent ="),src.indexOf('  const linkedAgent =',src.indexOf("  $('.global-agents', c).openAgent =")));
    await page.evaluate(({select,open})=>{
      window.route=(new Function('ws',`let selectionVersion=0, selected='a', gated=false;
        let _agpad={agentId:'a',view:'focus',focusWorkspace:ws};
        const root={},c={},pane={dataset:{},querySelector(){return null;}},agents=[{agent_id:'a',role:{id:'ordubashy'}},{agent_id:'b',role:{id:'global-worker'}}];
        const current=()=>true,globalWorkspaceTerminal=()=>null,overseerRole=()=>false,testingModeEnabled=()=>true;
        const enterComingSoon=()=>{},leaveComingSoon=()=>{},paintSelection=()=>{},disposeChat=()=>{},ls=()=>{};
        const agentWorkspaceCanLeave=async p=>{await p.focusWorkspace.close();return !p.focusWorkspace.isOpen();};
        const agpadRequestView=async(p,v)=>{if(!await agentWorkspaceCanLeave(p))return false;p.view=v;return true;},agpadShownView=p=>p?.view;
        const mountAgentView=()=>{},mountTerminal=()=>{},agviewboxReveal=()=>{};
        const agpadMount=(name,el,opts)=>{_agpad={agentId:opts.agentId,view:opts.initialView||'focus'};};
        const $=(selector)=>selector==='.global-agents'?root:{};
        ${select}\n${open}\nreturn {open:(id,opts)=>root.openAgent(id,opts),current:()=>_agpad,view:v=>agpadRequestView(_agpad,v)};`))(ws);
      dirty=true;
    },{select,open});
    await page.evaluate(()=>{window.nav=route.open('b',{view:'focus'});});
    await page.getByRole('button',{name:'Keep editing'}).click();
    assert.equal(await page.evaluate(()=>nav),false);assert.equal(await page.evaluate(()=>route.current().agentId),'a');
    await page.evaluate(()=>{window.nav=route.view('browser');});
    await page.getByRole('button',{name:'Keep editing'}).click();assert.equal(await page.evaluate(()=>nav),false);
    await page.evaluate(()=>{window.nav=route.view('desktop');});
    await page.getByRole('button',{name:'Keep editing'}).click();assert.equal(await page.evaluate(()=>nav),false);
    await page.evaluate(()=>{window.nav=route.open('b',{view:'focus'});});
    await page.getByRole('button',{name:'Discard',exact:true}).click();
    assert.equal(await page.evaluate(()=>nav),true);assert.equal(await page.evaluate(()=>route.current().agentId),'b');
    assert.equal(await page.locator('.overlay').count(),0);
  } finally {await browser.close();}
});

test('Global dashboard adapter forwards optional child beforeLeave',async()=>{
  const {browser,page}=await fixture();try {
    const result=await page.evaluate(async()=>{
      window.TASK_SPECIAL_TYPES=[];let count=0;
      window.globalTasksMount=()=>({dirty:()=>false,beforeLeave:async()=>{count++;return false;},dispose(){}});
      const host=document.createElement('div');document.body.append(host);
      const ctrl=await rwTaskDashboard(host,{source:{id:'t1',business_id:null},label:'Parent'},
        {alive:()=>true,normalizeTaskType(){},prefetchedTask:{id:'t1',title:'Parent',task_type:'task'}},async()=>({}));
      return [await ctrl.beforeLeave(),count];
    });
    assert.deepEqual(result,[false,1]);
  } finally {await browser.close();}
});
