// Synthetic contract check; no server or product session is used.
const assert=require('node:assert/strict'), path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
 try {
  const page=await browser.newPage();
  await page.setContent('<div id="pane"><div id="agpad-body"></div></div>');
  await page.evaluate(()=>{
   window.state={bizs:[]}; window.$=(s,e=document)=>e.querySelector(s); window.esc=s=>String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
   window.globalAgentProfile=()=>null; window.identityAvatar=(kind,name)=>`<span class="identity-avatar" data-kind="${kind}">${name}</span>`;
   window.T_STATES=['open','working','blocked','waiting','done','cancelled']; window.T_LABEL={open:'Open',working:'Working',blocked:'Blocked',waiting:'Waiting',done:'Done',cancelled:'Cancelled'};
   window.tNext=s=>({open:'working',working:'waiting',waiting:'done',done:'open',blocked:'open'})[s]; window.tDotHtml=s=>s; window.dropdown=()=>{};
   window.newRequestId=()=>Math.random().toString(); window.coreData=x=>x; window.toast=x=>{window.lastError=x}; window.agpadActive=()=>true;
   window.rwRecordFromItem=x=>({source:x.source,can_thread:x.source.type==='task'});
   window.recordWorkspaceMount=(host,options)=>({open:(record,opts)=>{window.opened={record,opts}},close:()=>options.onClose(),dispose:()=>{}});
   window.taskShowsState=t=>!['story','customer'].includes(t?.task_type);
   window.tBall=t=>`<span class="task-type-marker">${t.task_type}</span>`;
   window.customerStateText=t=>t?.customer_state || '';window.rwTypeIcon=()=>'';window.SVG_X='<svg></svg>';
   window.tControlPopover=(anchor,html)=>{const el=document.createElement('div');el.innerHTML=html;document.body.append(el);return {el,close:()=>el.remove()}};
   window.requests=[]; window.workers=[{id:'other',kind:'agent',display_name:'Atlas'},{id:'chief',kind:'agent',display_name:'Ordubashy'},{id:'owner',kind:'human',display_name:'Owner',is_owner:true}];
   window.tasks={a:{id:'a',title:'Blocked task',status:'blocked',revision:1,worker_ids:['owner','chief','other'],workers:window.workers,user_needed:true},
     d:{id:'d',title:'Done latest',status:'done',revision:1}, e:{id:'e',title:'Done older',status:'done',revision:1},
     f:{id:'f',title:'Off-page canonical',status:'working',revision:2}, g:{id:'g',title:'Project focused',status:'blocked',revision:3},
     story:{id:'story',title:'Story header',status:'open',revision:1,task_type:'story'}};
   window.post=async(path,args)=>{window.requests.push({path,args});if(path.endsWith('attention_get')) return {revision:4,attention:{state:'focused',focus:{type:'task',id:'a',business_id:null,label:'Focused task'},intent:'Finish it'},items:(args.cursor?['d','e']:['a']).map(id=>({membership_id:'m-'+id,membership_revision:2,task_ref:{type:'task',id,business_id:null},task:{...window.tasks[id]}})).concat(args.cursor?[]:[{membership_id:'project-a',membership_revision:1,task_ref:{type:'task',id:'a',business_id:'missing-project'},task:{id:'a',title:'Unavailable project task',status:'open',revision:99}}]),next_cursor:args.cursor?null:'page2'};if(path.endsWith('attention_remove'))return {};if(path.endsWith('tasks_update')){const t=window.tasks[args.task_id];Object.assign(t,args.changes,{revision:t.revision+1});return {task:{...t}};}if(path.endsWith('tasks_get'))return {task:window.tasks[args.task_id]};throw Error(path)};
   window.workCall=async(scope,op,args)=>op==='workers_list'?{workers:window.workers}:window.post((scope?`/biz/${scope}`:'/root')+'/operations/'+op,args);
   window.taskStateBind=(button,scope,task)=>{button.dataset.taskStateKey=JSON.stringify([scope,task.id]);for(const status of window.T_STATES)button.classList.toggle(status,status===task.status);button.setAttribute('aria-label',window.T_LABEL[task.status]);button.dataset.bound='true';};
   window.p={el:document.querySelector('#pane'),agentId:'chief',view:'attentionBoard'};
  });
  await page.addScriptTag({path:path.resolve(__dirname,'../js/task-state.js')});
  await page.addScriptTag({path:path.resolve(__dirname,'../js/agent-focus.js')});
  await page.addScriptTag({path:path.resolve(__dirname,'../js/attention-board.js')});
  await page.evaluate(()=>attentionBoardMount(p));
  await page.waitForSelector('[data-attention-member]');
  const focused=page.locator('[data-attention-focus]');
  // One continuous, status-ordered list: no state sections or headings remain.
  assert.equal(await page.locator('[data-attention-list] [data-attention-status]').count(),0);
  assert.equal(await page.locator('[data-attention-list] h2').count(),0);
  assert.equal(await page.locator('[data-attention-list] > .agfocus-items').count(),1);
  assert.deepEqual(await page.locator('[data-attention-member] .ttl').allTextContents(),['Blocked task','Unavailable project task','Done latest','Done older']);
  // The focused header row uses the shared Focus state ball for an ordinary Task, not a generic type icon.
  assert.equal(await focused.locator('.agfocus-task-state.blocked').count(),1);
  assert.equal(await focused.locator('.task-type-marker').count(),0);
  assert.equal(await focused.locator('.agfocus-task-state').getAttribute('aria-label'),'Blocked');
  assert.match(await focused.innerText(),/Focused on[\s\S]*Focused task[\s\S]*Intent: Finish it/);
  assert.equal(await page.locator('.task-workers').first().getAttribute('aria-label'),'Workers: Atlas, Ordubashy, Owner · User needed');
  assert.deepEqual(await page.locator('.task-workers').first().locator('.identity-avatar').allTextContents(),['Atlas','Ordubashy','Owner']);
  await page.getByText('Focused task').click(); assert.equal((await page.evaluate(()=>window.opened)).record.source.id,'a');
  await page.evaluate(()=>p.attentionBoard.workspace.close());
  await page.evaluate(()=>{p.attentionBoard.data.attention={state:'focused',focus:{type:'workflow',id:'run-1',business_id:null,label:'Research workflow'},task:{type:'task',id:'a',business_id:null},intent:'Find evidence'};attentionBoardPaint(p)});
  assert.equal(await page.getByRole('button',{name:'Research workflow'}).count(),0);
  assert.match(await page.locator('[data-attention-focus]').innerText(),/Focused on[\s\S]*Research workflow/);
  await page.getByText('Blocked task',{exact:true}).first().click(); assert.equal((await page.evaluate(()=>window.opened)).record.source.id,'a');
  await page.evaluate(()=>p.attentionBoard.workspace.close());
  assert.equal(await page.evaluate(()=>taskStateSnapshot(null,'a').task.title),'Blocked task');
  assert.equal(await page.locator('[data-attention-member]').filter({hasText:'Unavailable project task'}).locator('.ball').isDisabled(),true);
  await page.locator('.task-workers').first().click();
  await page.evaluate(()=>taskStateObserve(null,{...window.tasks.a,revision:2,worker_ids:['owner'],workers:[window.workers[2]]}));
  await page.locator('[data-worker-save]').click();
  assert.match(await page.locator('.task-worker-picker [role=alert]').innerText(),/Task changed/);
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.path.endsWith('tasks_update')).length),0);
  await page.evaluate(()=>document.querySelector('.task-worker-picker').parentElement.remove());
  await page.evaluate(()=>{window.tasks.a.revision=3;taskStateObserve(null,{...window.tasks.a})});
  await page.waitForTimeout(20);
  await page.locator('.task-workers').first().click(); await page.locator('[data-worker-index="0"]').uncheck(); await page.locator('[data-worker-save]').click();
  await page.waitForFunction(()=>requests.some(r=>r.path.endsWith('tasks_update')));
  const write=await page.evaluate(()=>requests.find(r=>r.path.endsWith('tasks_update')).args);
  assert.deepEqual(write.changes,{worker_ids:['chief','owner']});assert.equal(write.expected_revision,3);
  await page.getByRole('button',{name:'Remove Done older from Attention board'}).click();
  await page.waitForFunction(()=>requests.some(r=>r.path.endsWith('attention_remove')));
  const removal=await page.evaluate(()=>requests.find(r=>r.path.endsWith('attention_remove')).args);
  assert.equal(removal.membership_id,'m-e');assert.equal(removal.expected_membership_revision,2);
  assert.equal((await page.evaluate(()=>requests.filter(r=>r.path.endsWith('attention_get')).slice(0,2))).length,2);
  // A focused Task outside the paginated membership page is read once through the existing scoped read, never per paint.
  await page.evaluate(()=>{p.attentionBoard.data.attention={state:'focused',focus:{type:'task',id:'f',business_id:null,label:'Off-page focus'},intent:'Read it',focus_generation:7};attentionBoardPaint(p);attentionBoardPaint(p);});
  await page.waitForFunction(()=>document.querySelector('[data-attention-focus] .agfocus-task-state.working'));
  assert.equal(await focused.locator('.agfocus-task-state').getAttribute('aria-label'),'Working');
  let reads=await page.evaluate(()=>requests.filter(r=>r.path.endsWith('tasks_get')&&r.args.task_id==='f'));
  assert.equal(reads.length,1,'one scoped read for one focus identity');
  assert.equal(reads[0].path,'/root/operations/tasks_get');
  await page.evaluate(()=>attentionBoardPaint(p));
  await page.waitForTimeout(30);
  reads=await page.evaluate(()=>requests.filter(r=>r.path.endsWith('tasks_get')&&r.args.task_id==='f'));
  assert.equal(reads.length,1,'repaint reuses the resolved task');
  await page.getByText('Off-page focus').click(); assert.equal((await page.evaluate(()=>window.opened)).record.source.id,'f');
  await page.evaluate(()=>p.attentionBoard.workspace.close());
  // A known Project keeps its scoped route; a missing Project is never read as Global and stays an inert state marker.
  await page.evaluate(()=>{state.bizs=[{id:'p1',business_id:'p1',slug:'proj'}];p.attentionBoard.data.attention={state:'focused',focus:{type:'task',id:'g',business_id:'p1',label:'Project focus'},intent:'Check',focus_generation:8};attentionBoardPaint(p)});
  await page.waitForFunction(()=>document.querySelector('[data-attention-focus] .agfocus-task-state.blocked'));
  reads=await page.evaluate(()=>requests.filter(r=>r.path.endsWith('tasks_get')&&r.args.task_id==='g'));
  assert.equal(reads.length,1);assert.equal(reads[0].path,'/biz/proj/operations/tasks_get');
  await page.evaluate(()=>{p.attentionBoard.data.attention={state:'focused',focus:{type:'task',id:'missing',business_id:'missing-project',label:'Missing project'},intent:'Check',focus_generation:9};attentionBoardPaint(p)});
  await page.waitForTimeout(40);
  assert.equal((await page.evaluate(()=>requests.filter(r=>r.path.endsWith('tasks_get')&&r.args.task_id==='missing'))).length,0,'unresolved business references are never read as Global');
  assert.equal(await focused.locator('.agfocus-task-state').isDisabled(),true);
  assert.equal(await focused.locator('.agfocus-task-state').getAttribute('aria-label'),'Task project unavailable');
  assert.equal(await focused.locator('.agfocus-task-state').evaluate(el=>['open','working','blocked','waiting','done','cancelled'].some(status=>el.classList.contains(status))),false,'no invented status');
  assert.equal(await focused.locator('.task-type-marker').count(),0,'not a checkbox-like type icon');
  // Story/customer Tasks keep the special type marker instead of the ordinary state ball.
  await page.evaluate(()=>{p.attentionBoard.data.attention={state:'focused',focus:{type:'task',id:'story',business_id:null,label:'Story header'},intent:'Story',focus_generation:10};attentionBoardPaint(p)});
  await page.waitForFunction(()=>document.querySelector('[data-attention-focus] .task-type-marker')?.textContent==='story');
  assert.equal(await focused.locator('.agfocus-task-state').count(),0);
  // The shared header ball keeps the existing state control: it writes through the same task-state path.
  await page.evaluate(()=>{p.attentionBoard.data.attention={state:'focused',focus:{type:'task',id:'a',business_id:null,label:'Focused task'},intent:'Finish it',focus_generation:11};attentionBoardPaint(p)});
  await focused.locator('.agfocus-task-state').click();
  await page.waitForFunction(()=>requests.some(r=>r.path.endsWith('tasks_update')&&r.args.changes?.status));
  const headerWrite=await page.evaluate(()=>requests.find(r=>r.path.endsWith('tasks_update')&&r.args.changes?.status).args);
  assert.deepEqual(headerWrite.changes,{status:'open'});assert.equal(headerWrite.task_id,'a');
  await page.waitForFunction(()=>document.querySelector('[data-attention-focus] .agfocus-task-state.open'));
  console.log('attention board synthetic browser checks passed');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
