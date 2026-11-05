// Isolated task-control interaction check; no live service or records.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
 try {
  const page=await browser.newPage({viewport:{width:700,height:850}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',route=>{
   const url=new URL(route.request().url());
   if(url.hostname!=='task.test')return route.abort();
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:`<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/tasks.css"><link rel="stylesheet" href="/css/conversation.css"><button id="wsbtn" hidden></button><div id="host" style="padding:24px"></div><script src="/js/core.js"></script><script src="/js/san-juan-time.js"></script><script src="/js/task-state.js"></script><script src="/js/tags.js"></script><script src="/js/work-groups.js"></script><script src="/js/tasks.js"></script><script src="/js/task-snooze.js"></script>`});
   const file=path.join(root,url.pathname);return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  });
  await page.goto('http://task.test');
  await page.evaluate(()=>{
   state.biz='studio';state.bizs=[{slug:'studio',name:'Studio',agents:['marketer'],owner:'Owner'}];
   window.task={id:'73dbb715-cfaa-4174-ad15-50530588154a',title:'Prepare the outreach batch',status:'working',revision:1,snoozed_until:null,parent:null,children:[{id:'c1',title:'Draft the intro',status:'open'}],owners:['agent:marketer'],business:state.bizs[0],blocked_by:['369b6767-d176-43de-8c9f-c0db9f72d23c'],tags:['marketing','custom-note']};
   window.writes=[];
   const originalFetch=window.fetch;
   window.fetch=async(path,options)=>{
    const args=JSON.parse(options?.body || '{}');
    if(path.endsWith('/tasks_update')) {
     writes.push(args);Object.assign(task,args.changes);task.revision++;
     return new Response(JSON.stringify({status:'ok',data:{task:{...task}}}),{headers:{'Content-Type':'application/json'}});
    }
    if(path.endsWith('/tags_list')){window.tagCalls=(window.tagCalls||[]).concat([args]);return new Response(JSON.stringify({status:'ok',data:{tags:[{id:'t1',label:'design/software',created_at:'2029-01-01T00:00:00Z'}],next_cursor:null}}),{headers:{'Content-Type':'application/json'}});}
    if(path.endsWith('/assignees_list')){window.assigneeCalls=(window.assigneeCalls||[]).concat([path]);return new Response(JSON.stringify({status:'ok',data:{assignees:[{id:'owner-id',kind:'human',display_name:'Owner',name:null,is_owner:true},{id:'g-ordu',kind:'agent',display_name:'Ordubashy',name:'ordubashy',is_owner:false}]}}),{headers:{'Content-Type':'application/json'}});}
    if(path.endsWith('/tasks_get'))return new Response(JSON.stringify({status:'ok',data:{task:{...task}}}),{headers:{'Content-Type':'application/json'}});
    return originalFetch(path,options);
   };
   tFetchTask=async()=>({...task,id:'369b6767-d176-43de-8c9f-c0db9f72d23c',title:'Approve the recipient list',blocked_by:[]});
   twrite=async(_task,args)=>{writes.push(args);Object.assign(task,args.set);task.revision++;return {task:{...task}};};
   taskProgressMount=host=>{host.textContent='Conversation';};
   state.myTasks=[task];state.tasks=[{...task}];
   tDetail(task,{host:document.querySelector('#host')});
   const other=document.createElement('div');other.id='other-pane';other.innerHTML=tRow(state.tasks[0]);document.body.append(other);tWireRows(other);

  });
  for (const type of ['story','email']) {
   await page.evaluate(type=>{task.task_type=type;document.querySelector('#host').replaceChildren();tDetail(task,{host:document.querySelector('#host')});},type);
   assert.equal(await page.locator('#td-status').count(),0);
   assert.equal(await page.locator('.task-detail-heading .task-type-icon-'+type).count(),1);
  }
  await page.evaluate(()=>{task.task_type='task';document.querySelector('#host').replaceChildren();tDetail(task,{host:document.querySelector('#host')});});
  const body=page.locator('#host');
  await page.getByRole('button',{name:'Approve the recipient list',exact:true}).waitFor();
  assert(!/[0-9a-f]{8}-[0-9a-f]{4}-/.test(await body.innerText()));
  assert.equal(await page.getByRole('button',{name:'Mark done',exact:true}).count(),0);
  assert.equal(await page.locator('.task-snooze-picker').count(),0);
  // Tags come from the shared label catalog; applying one writes only the label names and never the scope.
  await page.getByRole('textbox',{name:'Add a tag',exact:true}).click();
  await page.locator('.tag-option[data-tag="design/software"]').click();
  await page.getByRole('button',{name:'Save tags',exact:true}).click();
  await page.waitForFunction(()=>window.task.tags.includes('design/software'));
  assert.deepEqual(await page.evaluate(()=>window.task.tags),['marketing','custom-note','design/software']);
  assert.equal(await page.evaluate(()=>window.tagCalls.every(call=>!('scope' in call) && !('business_id' in call))),true);
  // Children are listed from the typed read model; their state is independent of this task's.
  assert.equal(await page.locator('#host .task-link-chip').filter({hasText:'Draft the intro'}).count(),1);
  await page.locator('#td-status').click();
  await page.locator('.menu .mi').filter({hasText:'Done'}).click();
  await page.waitForFunction(()=>document.querySelector('#td-status').getAttribute('aria-label')==='Status: Done' && document.querySelector('#td-status').getAttribute('aria-busy')==='false');
  assert.equal(await page.locator('#other-pane .ball').getAttribute('aria-label'),'Status: Done');
  assert.equal(await page.locator('#other-pane .ball').getAttribute('aria-busy'),'false');
  await page.locator('#td-who').click();
  assert.equal(await page.locator('.modal').count(),0);
  // Choices are the business's assignees_list with its IDs; a former agent stays as an unavailable checked choice.
  await page.locator('.task-controls-popover input[value="owner-id"]').check();
  assert.deepEqual(await page.evaluate(()=>window.assigneeCalls),['/api/biz/studio/operations/assignees_list']);
  assert.equal(await page.locator('.task-controls-popover label').filter({hasText:'Ordubashy'}).count(),1);
  assert.equal(await page.locator('.task-controls-popover input[value="agent:marketer"]').isChecked(),true);
  assert.equal(await page.locator('.task-controls-popover label').filter({hasText:'Unavailable assignee'}).count(),1);
  await page.locator('.task-controls-popover').getByRole('button',{name:'Apply',exact:true}).click();
  await page.waitForFunction(()=>writes.some(w=>w.set?.owners?.includes('owner-id')));
  assert.deepEqual(await page.evaluate(()=>writes.find(w=>w.set?.owners).set.owners),['agent:marketer','owner-id']);
  await page.locator('#td-snooze').click();
  await page.getByRole('button',{name:'Tomorrow',exact:true}).click();
  await page.locator('.task-controls-popover').getByRole('button',{name:'Apply',exact:true}).click();
  await page.waitForFunction(()=>writes.some(w=>typeof w.set?.snoozed_until==='string'));
  await page.locator('#td-who').click();await page.locator('.task-controls-popover').waitFor();await page.keyboard.press('Escape');
  await page.waitForFunction(()=>!document.querySelector('.task-controls-popover'));
  await page.screenshot({path:'/tmp/qorqut-task-controls.png'});
  await page.getByRole('button',{name:'Approve the recipient list',exact:true}).click();
  assert.equal(await page.locator('.dtitle').innerText(),'Approve the recipient list');
  assert.equal(await page.locator('.modal').count(),0);
  assert.deepEqual(errors,[]);
  console.log('Compact task controls: named dependencies, children, tag catalog, fixed states, assignee choices from assignees_list, snoozed_until and Escape passed.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
