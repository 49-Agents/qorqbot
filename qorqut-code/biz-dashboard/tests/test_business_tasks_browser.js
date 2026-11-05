// Business Tasks landing, global-only agent navigation and the embedded Tasks section; mocked, no live service.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const scope={kind:'ecosystem',allowed_business_ids:['b-studio'],global_capabilities:[]};
const agents=[
 {agent_id:'g-ordu',name:'ordubashy',display_name:'Ordubashy',role_id:'ordubashy',scope},
 {agent_id:'g-founder',name:'founder',display_name:'Founder',role_id:'founder',scope},
 {agent_id:'g-marketing',name:'marketing',display_name:'Marketing',role_id:'global-worker',scope},
 {agent_id:'g-learn',name:'learning-applier',display_name:'Learning Applier',role_id:'learning-applier',scope},
 // A former business agent claims a supported role but is business-scoped: never offered.
 {agent_id:'b-dev',name:'dev',display_name:'Old Dev',role_id:'founder',home_business_id:'b-studio',scope:{kind:'business',business_id:'b-studio'}},
];
const business={id:'b-studio',slug:'studio',name:'Studio',owner:'Owner',agents:[]};
const task=(id,title,owners,extra={})=>({id,title,status:'open',revision:1,owners,tags:[],business_id:'b-studio',snoozed_until:null,...extra});
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try {
  const page=await browser.newPage({viewport:{width:1200,height:850}}),errors=[],posts=[],unexpected=[],updates=[],writes=[];
  let failCreate=false;
  // Owners mix an exact assignee id, a historical kind:name and a former agent with no current choice.
  let tasks=[task('t1','Owner follow-up',['human:Owner']),task('t2','Agent draft',['agent:marketing','g-ordu','agent:old-dev']),task('t3','Unassigned review',[]),task('t4','Finished thing',['human:Owner'],{status:'done'})];
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{ if(!sessionStorage.getItem('seeded')){ sessionStorage.setItem('seeded','1'); localStorage.setItem('tab','agents'); } });
  await page.route('**/*',route=>{
   const url=new URL(route.request().url()),p=url.pathname,method=route.request().method();
   if(url.hostname!=='dashboard.test')return route.abort();
   if(p==='/api/biz')return route.fulfill({json:[business]});
   if(p==='/api/me/tasks')return route.fulfill({json:{tasks:[]}});
   if(p==='/api/biz/studio/tasks')return route.fulfill({json:tasks});
   if(p==='/api/root/agents')return route.fulfill({json:{status:'ok',data:{agents}}});
   const ok=data=>route.fulfill({json:{status:'ok',data}});
   if(p==='/api/biz/studio/operations/assignees_list')return ok({assignees:[{id:'owner-id',kind:'human',display_name:'Owner',name:null,is_owner:true},{id:'g-ordu',kind:'agent',display_name:'Ordubashy',name:'ordubashy',is_owner:false},{id:'g-marketing',kind:'agent',display_name:'Marketing',name:'marketing',is_owner:false}]});
   if(p==='/api/biz/studio/operations/tasks_update'){
    const body=route.request().postDataJSON(),t=tasks.find(x=>x.id===body.task_id);updates.push({path:p,body});
    if(!t||t.revision!==body.expected_revision)return route.fulfill({status:409,json:{status:'error',error:{code:'revision_conflict',message:'stale'}}});
    Object.assign(t,body.changes);t.revision++;return ok({task:{...t}});
   }
   const one=p.match(/^\/api\/biz\/studio\/task\/([^/]+)$/);
   if(one&&method==='POST'){
    const body=route.request().postDataJSON(),t=tasks.find(x=>x.id===decodeURIComponent(one[1]));writes.push({id:t.id,body});
    Object.assign(t,body.set);t.revision++;return route.fulfill({json:{ok:true}});
   }
   if(p==='/api/biz/studio/operations/tasks_create'&&method==='POST'&&failCreate)return route.abort('failed');
   if(p==='/api/biz/studio/operations/tasks_create'&&method==='POST'){const body=route.request().postDataJSON();posts.push(body);const created=task('t5',body.title,body.assignee_ids,body);tasks=[...tasks,created];return ok({task:created});}
   if(p.startsWith('/api/biz/studio/agents'))unexpected.push(p);
   if(p.startsWith('/api/'))return route.fulfill({json:[]});
   const file=path.resolve(root,'.'+(p==='/'?'/index.html':p));
   return file.startsWith(root+path.sep)&&fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  });

  // A saved Agents tab lands on Tasks; every Task the business owns is listed, not only the owner's.
  await page.goto('http://dashboard.test/#studio');
  const pageEl=page.locator('[data-business-task-page]');
  await pageEl.getByText('Agent draft',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>state.tab),'tasks');
  assert.equal(await page.locator('#rail, .ritem').count(),0,'the shell starts without a navigation rail');
  for(const title of ['Owner follow-up','Agent draft','Unassigned review'])
   assert.equal(await pageEl.getByText(title,{exact:true}).count(),1,`${title} is listed`);
  assert.equal(await pageEl.getByText('Finished thing',{exact:true}).count(),0,'closed tasks stay out of Active');
  await pageEl.locator('[data-business-task-visibility]').selectOption('all');
  await pageEl.getByText('Finished thing',{exact:true}).waitFor();
  await pageEl.locator('[data-business-task-visibility]').selectOption('active');
  assert.equal(await page.locator('#agentrows, .agent-terminal, [data-agent-start]').count(),0,'no roster, terminal or start control');

  // New task: the owner creates one in the selected business.
  await pageEl.getByRole('button',{name:'New task'}).click();
  await page.locator('[data-task-create] [name=title]').fill('Write the brief');
  await page.getByLabel('Task type',{exact:true}).selectOption('story');
  await page.locator('[data-task-create] [name=title]').press('Enter');
  await pageEl.locator('.trow').filter({hasText:'Write the brief'}).waitFor();
  assert.equal(await pageEl.locator('.trow').filter({hasText:'Write the brief'}).locator('.ball, .ttl .task-type-icon').count(),0);
  assert.equal(await pageEl.locator('.trow').filter({hasText:'Write the brief'}).locator('.task-headline > .task-type-marker .task-type-icon-story').count(),1);
  assert.equal(posts[0].title,'Write the brief');assert.deepEqual(posts[0].assignee_ids,['owner-id']);assert.equal(posts[0].task_type,'story');

  // A status edit writes the business scope, Task id and expected revision, and the row and Active filter follow at once.
  const row=title=>pageEl.locator('.trow').filter({hasText:title});
  for(const [from,to] of [['open','working'],['working','waiting'],['waiting','done']]){
   await row('Owner follow-up').locator('.ball').click();
   const deadline=Date.now()+5000;while(!updates.some(u=>u.body.changes.status===to)&&Date.now()<deadline)await page.waitForTimeout(20);
   const update=updates.find(u=>u.body.changes.status===to);
   assert.equal(update.path,'/api/biz/studio/operations/tasks_update');
   assert.deepEqual([update.body.task_id,update.body.expected_revision,update.body.changes],['t1',updates.indexOf(update)+1,{status:to}]);
   if(to!=='done')await row('Owner follow-up').locator(`.ball[aria-label="Status: ${to[0].toUpperCase()+to.slice(1)}"]`).waitFor();
  }
  await row('Owner follow-up').waitFor({state:'detached'});
  assert.equal(updates.length,3,'one write per status step');

  // The assignee picker uses assignees_list ids: an exact id and a kind:name owner both check their choice; a former agent stays as an unavailable choice.
  await row('Agent draft').getByText('Agent draft',{exact:true}).click();
  await page.locator('#td-who').click();
  const picker=page.locator('.task-controls-popover');
  await picker.locator('input[value="g-ordu"]').waitFor();
  assert.equal(await picker.locator('input[value="g-ordu"]').isChecked(),true);
  assert.equal(await picker.locator('input[value="g-marketing"]').isChecked(),true);
  assert.equal(await picker.locator('input[value="owner-id"]').isChecked(),false);
  assert.equal(await picker.locator('input[value="agent:old-dev"]').isChecked(),true);
  assert.equal(await picker.locator('label').filter({hasText:'Unavailable assignee'}).count(),1);
  assert.equal(await picker.locator('input[value="agent:marketing"]').count(),0,'a matched kind:name owner is not duplicated');
  await picker.getByRole('button',{name:'Cancel',exact:true}).click();

  // Snoozing a Task moves it out of Active and into Snoozed.
  await page.locator('#td-snooze').click();
  await page.getByRole('button',{name:'Tomorrow',exact:true}).click();
  await page.locator('.task-controls-popover').getByRole('button',{name:'Apply',exact:true}).click();
  {const deadline=Date.now()+5000;while(!writes.length&&Date.now()<deadline)await page.waitForTimeout(20);}
  assert.equal(writes[0].id,'t2');assert.equal(writes[0].body.expected_revision,1);
  assert.equal(typeof writes[0].body.set.snoozed_until,'string');
  await page.keyboard.press('Escape');
  await row('Agent draft').waitFor({state:'detached'});
  await pageEl.locator('[data-business-task-visibility]').selectOption('snoozed');
  await row('Agent draft').waitFor();
  assert.equal(await row('Unassigned review').count(),0);
  await pageEl.locator('[data-business-task-visibility]').selectOption('active');

  // A create whose result is unknown keeps the typed title, says so briefly and is not retried.
  failCreate=true;
  await pageEl.getByRole('button',{name:'New task'}).click();
  const titleBox=page.locator('[data-task-create] [name=title]');
  await titleBox.fill('Lost write');
  await titleBox.press('Enter');
  await page.getByText("Couldn't confirm. Try again",{exact:true}).first().waitFor();
  assert.equal(await titleBox.inputValue(),'Lost write');
  assert.equal(await titleBox.isEnabled(),false,'an uncertain write retains the exact retry payload');
  assert.equal(posts.length,1,'the failed create is not recorded or retried');
  assert.equal(await row('Lost write').count(),0);
  await titleBox.press('Escape');
  failCreate=false;

  // An old agent deep link and the agents tab both route to Tasks.
  await page.evaluate(()=>{state.tab='marketing';goTab('agents');});
  assert.equal(await page.evaluate(()=>state.tab),'tasks');

  // Route names come from the business TABS table: aliases resolve, unknown and prototype names are rejected, and a dirty guard blocks every switch.
  const route=name=>page.evaluate(async n=>{await goTab(n);return [state.tab,state.ctx,ls('tab')];},name);
  assert.deepEqual(await route('nonexistent'),['tasks',null,'tasks'],'an unknown name is rejected');
  for(const name of ['constructor','toString','__proto__','hasOwnProperty'])
   assert.equal((await route(name))[0],'tasks',`${name} is not a route`);
  assert.deepEqual(await route('browser'),['browser',null,'browser']);
  await page.evaluate(()=>{window.__canLeave=agentWorkspaceCanLeave;agentWorkspaceCanLeave=async()=>false;});
  assert.deepEqual(await route('tasks'),['browser',null,'browser'],'an unsaved-work guard keeps the current section');
  assert.deepEqual(await route('agents'),['browser',null,'browser'],'the guard runs before the agents alias');
  await page.evaluate(()=>{agentWorkspaceCanLeave=window.__canLeave;});
  assert.deepEqual(await route('agents'),['tasks',null,'tasks'],'agents is the Tasks route');
  // Context pages are not part of this fixture's mocked API; the alias is checked against the route state only.
  await page.evaluate(()=>{window.__renderContext=renderContext;renderContext=()=>{};});
  assert.deepEqual(await route('tables'),['context','_tables','context'],'tables is the Context Tables page');
  await page.evaluate(()=>{renderContext=window.__renderContext;state.ctx=null;});
  await page.evaluate(()=>goTab('tasks'));
  await pageEl.waitFor();
  assert.equal(await page.locator('#rail, .ritem').count(),0,'switching sections never creates a rail');

  // The Progress and Settings glyphs keep their exact artwork now that no rail supplies them.
  const icons=await page.evaluate(()=>({progress:[NAV_PROGRESS_ICON,T_VIEW_ICON.charts,GLOBAL_ICONS.analytics],settings:[NAV_SETTINGS_ICON,dataSettingsIcon,GLOBAL_ICONS.settings]}));
  for(const copy of icons.progress){assert.equal(copy,icons.progress[0]);assert(copy.includes('M9 18h6m-5 3h4')&&copy.includes('viewBox="0 0 24 24"')&&copy.includes('width="20"'));}
  for(const copy of icons.settings){assert.equal(copy,icons.settings[0]);assert(copy.includes('M12.22 2h-.44')&&copy.includes('<circle cx="12" cy="12" r="3"/>'));}

  // A profile link to a former business agent is unavailable; a global one opens the global agent.
  await page.evaluate(()=>agentProfileOpen('b-dev'));
  const unavailable=await page.evaluate(()=>GLOBAL_AGENT_UNAVAILABLE);
  await page.getByText(unavailable,{exact:true}).first().waitFor();
  assert.equal(await page.evaluate(()=>state.scope),'business');

  // Tab+a selects the global Agents screen.
  await page.locator('body').click({position:{x:5,y:5}});
  await page.keyboard.down('Tab');
  await page.keyboard.press('a');
  await page.keyboard.up('Tab');
  await page.waitForFunction(()=>state.scope==='global'&&state.globalTab==='agents');

  // Pane views (workspace registry unavailable, so the built-in fallback applies): Ordubashy and general workers have Focus, Learning Applier its Context; Founder has none and Project management has no pane entry.
  assert.deepEqual(await page.evaluate(()=>['ordubashy','founder','global-worker','learning-applier'].map(r=>agentPaneViews('global',r).filter(v=>['global_context','business','focus'].includes(v)))),
   [['focus'],[],['focus'],['global_context']]);

  // The embedded business section renders the same Tasks page.
  const embed=await browser.newPage();
  embed.on('pageerror',e=>errors.push(e.message));
  await embed.route('**/*',route=>{
   const url=new URL(route.request().url()),p=url.pathname;
   if(url.hostname!=='dashboard.test')return route.abort();
   if(p==='/api/biz')return route.fulfill({json:[business]});
   if(p==='/api/me/tasks')return route.fulfill({json:{tasks:[]}});
   if(p==='/api/biz/studio/tasks')return route.fulfill({json:tasks});
   if(p==='/api/root/agents')return route.fulfill({json:{status:'ok',data:{agents}}});
   if(p.startsWith('/api/'))return route.fulfill({json:[]});
   const file=path.resolve(root,'.'+(p==='/'?'/index.html':p));
   return file.startsWith(root+path.sep)&&fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  });
  await embed.goto('http://dashboard.test/?agent-pane-embed=business&business=studio&section=tasks#studio');
  await embed.locator('[data-business-task-page]').getByText('Unassigned review',{exact:true}).waitFor();
  assert.equal(await embed.evaluate(()=>state.tab),'tasks');
  assert.equal(await embed.locator('#rail, .ritem').count(),0,'the embedded shell has no rail either');

  assert.deepEqual(unexpected,[],'no business agent roster is fetched');
  assert.deepEqual(errors,[]);
  console.log('Business Tasks landing, all-business tasks, global-only assignment and navigation passed.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
