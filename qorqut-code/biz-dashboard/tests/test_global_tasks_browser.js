// Component acceptance: scope, drafts, escaped notes, CAS and lost-response retry.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo = path.resolve(__dirname, '..'), requests = [], receipts = new Map();
const until = async (check, what) => { for (let i=0;i<200;i++){ if (check()) return; await new Promise(r=>setTimeout(r,25)); } throw new Error('Timed out waiting for '+what); };
const agent = '11111111-1111-4111-8111-111111111111';
let task = null, lose = false, conflict = false, writes = 0, privateRead = 0;
const privateMessages = [{id:'pm-0',sequence:1,body:'<img src=x onerror=alert(1)> Script reference',created_by:'chief',created_by_name:'Ordubashy',author_kind:'agent',author_role:'coordinator',is_owner:false,created_at:'2026-09-12T10:00:00Z'}];
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/root/agents') { res.setHeader('Content-Type','application/json'); return res.end(JSON.stringify({data:{agents:[{agent_id:agent,name:'ordubashy',role:{id:'ordubashy'},presentation:{department:'operations'}}]}})); }
  if (url.pathname.startsWith('/root/operations/')) {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const args = JSON.parse(raw), op = url.pathname.split('/').at(-1);
    // The root route is Global context: Task, assignee and tag operations infer it and their schemas reject extra fields.
    // Only the Section G relationship reads and the private conversation operations declare `scope`.
    assert.equal(args.business_id, undefined);
    assert.equal(args.scope, op === 'relationships_list' || op.startsWith('owner_conversation_') ? 'global' : undefined, `${op} scope ${JSON.stringify(args)}`);
    requests.push({op, args});
    const reply = data => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({status:'ok', data})); };
    assert.notEqual(op, 'tasks_config_get', 'fixed states need no configuration read');
    // UNINTEGRATED Section G contract: the frozen host root route has no relationships_list yet (Operations adds it with an
    // optional `scope`). This fixture is the requested contract, not evidence that the real route answers.
    if (op === 'relationships_list') return reply({items:[{id:'rel-1',role:'concerns',source:args.item,target:{type:'task',id:'other-task'},source_item:{type:'task',id:args.item.id,label:'This task'},target_item:{type:'task',id:'other-task',label:'Related Global task'}}],next_cursor:null});
    if (op === 'tags_list') return reply({tags:[{id:'t1',label:'design/software',created_at:'2029-01-01T00:00:00Z'}],next_cursor:null});
    if (op === 'assignees_list') return reply({assignees:[{id:agent,display_name:'Ordubashy',kind:'agent'},{id:'owner',display_name:'Owner',is_owner:true,kind:'human'}]});
    if (op === 'tasks_list') {
      const snoozed = Date.parse(task?.snoozed_until) > Date.now();
      const match = args.snooze_filter === 'all' || Boolean(snoozed) === (args.snooze_filter === 'snoozed');
      return reply({tasks: task && match && (args.include_closed || task.status !== 'done') ? [task] : [], next_cursor:null});
    }
    if (op === 'tasks_thread_get') return reply({notes:(task?.notes || []).map((n,i)=>({...n,id:String(i),created_by_name:'Owner',author_kind:'human'})),next_cursor:null});
    if (op.startsWith('email_')) throw new Error('Unexpected retired mail operation '+op);
    if (op === 'tasks_get') return reply({task});
    if (op === 'owner_conversation_get') {
      assert.equal(args.source.business_id, undefined);
      return reply({conversation:{id:'pc-1',source:{...args.source,business_id:null},audience:'owner_ordubashy',revision:1,latest_sequence:privateMessages.length,message_count:privateMessages.length},
        messages:privateMessages,next_before:null,participants:[],read_state:{last_read_sequence:privateRead,unread:false}});
    }
    if (op === 'owner_conversation_send') {
      privateMessages.push({id:'pm-'+(privateMessages.length+1),sequence:privateMessages.length+1,body:args.text,created_by:'owner',created_by_name:'Owner',author_kind:'human',author_role:'owner',is_owner:true,created_at:new Date().toISOString()});
      return reply({message:privateMessages.at(-1),conversation:{id:'pc-1',revision:privateMessages.length+1,latest_sequence:privateMessages.length,message_count:privateMessages.length}});
    }
    if (op === 'owner_conversation_peers') return reply({peers:[{agent_id:agent,channel:'private',name:'Ordubashy',role:{id:'ordubashy'},unread:false}]});
    if (op === 'owner_conversation_mark_read') { privateRead = args.sequence; return reply({read_state:{last_read_sequence:privateRead,unread:false}}); }
    if (receipts.has(args.idempotency_key)) return reply(receipts.get(args.idempotency_key));
    if (conflict) {
      conflict = false; res.statusCode = 409; res.setHeader('Content-Type','application/json');
      return res.end(JSON.stringify({status:'error',error:{code:'revision_conflict',message:'Changed elsewhere'}}));
    }
    if (op === 'tasks_update') assert.equal(args.expected_revision, task.revision);
    task = {id:args.id || task.id, business_id:null, revision:(task?.revision || 0)+1,
      title:args.title || task?.title, status:args.status || (op==='tasks_create' ? 'open' : task?.status), assignee_ids:args.assignee_ids || task?.assignee_ids,
      task_type:args.task_type || task?.task_type || 'task',overview:args.overview ?? task?.overview ?? '',email_conversation_id:task?.email_conversation_id ?? null,
      due:null,tags:args.tags || task?.tags || [],notes:task?.notes || [],snoozed_until:args.snoozed_until || task?.snoozed_until || null,...args.changes};
    if (args.note) task.notes.push({body:args.note,created_at:'2026-09-12',created_by:'owner'});
    task.note_count = task.notes.length; task.notes_next_cursor = null;
    writes++; receipts.set(args.idempotency_key, {task:structuredClone(task)});
    if (lose) { lose=false; res.writeHead(503,{'Content-Type':'text/plain'}).end('Response lost'); return; }
    return reply({task});
  }
  if (url.pathname === '/shared.js') {
    res.setHeader('Content-Type','text/javascript');
    return res.end(fs.readFileSync(path.join(repo,'js/core.js'),'utf8').split('const TABS =')[0]+require('fs').readFileSync(require('path').join(__dirname,'../js/san-juan-time.js'),'utf8') + '\nconst ICO_UPLOAD="", ICO_CLOCK=""; const state={biz:null,bizs:[],agents:[]};\n' + fs.readFileSync(path.join(repo,'js/tasks.js'),'utf8').split('(function tInit()')[0]);
  }
  if (url.pathname === '/') {
    res.setHeader('Content-Type','text/html');
    return res.end(`<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/global-tasks.css"><link rel="stylesheet" href="/css/tasks.css"><link rel="stylesheet" href="/css/agentpane.css"></head><body><div id="host" class="tasks" style="max-width:1100px;margin:auto"></div>
      <script>window.esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.api=async url=>(await fetch(url)).json(); window.newRequestId=()=>crypto.randomUUID(); window.coreData=r=>r.data;
      window.post=async(url,body)=>{const r=await fetch(url,{method:'POST',body:JSON.stringify(body)});const d=await r.json();if(d.error){const e=new Error(d.error.message);e.code=d.error.code;e.httpStatus=r.status;throw e;}return d;};</script>
      <script src="/js/task-state.js"></script><script src="/shared.js"></script><script src="/js/task-snooze.js"></script><script src="/js/work-groups.js"></script><script src="/js/tags.js"></script><script src="/js/safe-links.js"></script><script src="/js/chat-view.js"></script><script src="/js/conversation.js"></script><script src="/js/task-progress.js"></script><script src="/js/relationships.js"></script><script src="/js/global-tasks.js"></script><script>window.mount=opts=>globalTasksMount(document.querySelector('#host'),opts);mount();</script></body></html>`);
  }
  const file = path.resolve(repo, '.'+url.pathname);
  if (!file.startsWith(repo+path.sep) || !fs.existsSync(file)) { res.statusCode=404; return res.end(); }
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/css'); res.end(fs.readFileSync(file));
});
(async () => {
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser = await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
  try {
    const page = await browser.newPage({viewport:{width:450,height:1000}}), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.getByRole('button',{name:'Mine',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('[data-owner-filter="mine"]')?.getAttribute('aria-pressed')==='true');
    await page.getByRole('button',{name:'All',exact:true}).click();
    await page.getByRole('button',{name:'New task',exact:true}).click();
    await page.getByLabel('Title',{exact:true}).fill('Personal reminder');
    for (const tag of ['design/software','custom-note']) { await page.getByLabel('Add a tag',{exact:true}).fill(tag); await page.getByLabel('Add a tag',{exact:true}).press('Enter'); }
    const createdAssignees = await page.locator('[name="assignee"]:checked').evaluateAll(nodes=>nodes.map(node=>node.value));
    await page.getByRole('button',{name:'Tomorrow',exact:true}).click();
    assert.equal(await page.getByLabel('Snooze',{exact:true}).inputValue(),'until');
    assert.match(await page.getByLabel('Snooze until',{exact:true}).inputValue(), /T09:00$/);
    await page.getByLabel('Snooze',{exact:true}).selectOption('none');
    // An owner's Global task creation never carries a team note.
    assert.equal(await page.getByLabel('Initial note').count(),0);
    lose=true;
    await page.getByRole('button',{name:'Create task',exact:true}).click();
    await page.getByRole('button',{name:'Retry exact save'}).waitFor({state:'visible'});
    assert.equal(writes,1);
    await page.getByRole('button',{name:'Retry exact save'}).click();
    await page.getByRole('button',{name:'Save task',exact:true}).waitFor();
    assert.equal(writes,1); assert.equal(await page.locator('img').count(),0);
    assert.equal(requests.filter(r=>r.op==='tasks_create').every(r=>r.args.note===undefined),true);
    assert.equal(requests.filter(r=>r.op==='tasks_update').every(r=>r.args.note===undefined && r.args.changes?.note===undefined),true);
    assert.deepEqual(task.tags,['design/software','custom-note']);
    assert.deepEqual(task.assignee_ids,createdAssignees);
    // The Chat checkpoint's private-channel tests own the discussion channels; this check covers the Task editor.
    await page.getByLabel('Title',{exact:true}).fill('Stale draft'); conflict=true;
    await page.getByRole('button',{name:'Save task',exact:true}).click();
    await page.getByRole('button',{name:'Discard edits and reload current task'}).waitFor({state:'visible'});
    assert.equal(await page.getByLabel('Title',{exact:true}).inputValue(),'Stale draft');
    await page.getByRole('button',{name:'Discard edits and reload current task'}).click();
    await page.waitForFunction(()=>document.querySelector('[name=title]')?.value==='Personal reminder');
    assert.equal(await page.getByLabel('Snooze',{exact:true}).locator('option[value=indefinite]').count(),0,'no indefinite snooze input');
    await page.getByLabel('Snooze',{exact:true}).selectOption('until');
    await page.getByLabel('Snooze until',{exact:true}).fill('2030-01-02T09:30');
    await page.getByRole('button',{name:'Save task',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('[data-global-task-status]').textContent==='Saved.');
    assert.equal(task.snoozed_until,'2030-01-02T13:30:00.000Z'); assert.equal(task.status,'open');
    await page.getByRole('button',{name:'← Back'}).click();
    await page.getByText('No Global tasks found.',{exact:true}).waitFor();
    await page.getByLabel('Task visibility').selectOption('snoozed');
    await page.getByText(/Snoozed until Jan/).waitFor();
    assert.equal(requests.filter(r=>r.op==='tasks_list').at(-1).args.snooze_filter,'snoozed');
    await page.locator('[data-task]').waitFor();
    assert.equal(await page.locator('.global-task-row').count(),0);
    assert.equal(await page.locator('[data-task] .identity-global svg').count(),1);
    assert.equal(await page.locator('[data-task] .ball[title]').count(),0);
    assert(await page.evaluate(()=>{
      const host=document.createElement('div');
      host.innerHTML=taskBusinessLabel('x'.repeat(60));
      return host.textContent.length===15 && host.textContent.endsWith('…') && host.firstChild.title.length===60;
    }));
    assert.equal(await page.locator('[data-task].trow.personal-task').count(),1);
    await page.setViewportSize({width:1400,height:950});
    await page.evaluate(()=>{
      const sample=document.createElement('div');sample.id='label-samples';
      sample.innerHTML=['Studio','Qorqut SWE Subcontractor','Global'].map(name=>taskRowLayout({title:'Confirm the setup',status:'open'}, {leading:taskBusinessLabel(name,name==='Global')})).join('');
      document.querySelector('#host').append(sample);
    });
    const aligned=()=>page.locator('#label-samples .task-headline').evaluateAll(nodes=>nodes.every(n=>Math.abs(n.getBoundingClientRect().left-nodes[0].getBoundingClientRect().left)<1));
    assert(await aligned());
    assert(await page.locator('#label-samples .business-label').evaluateAll(nodes=>nodes[0].getBoundingClientRect().width<nodes[1].getBoundingClientRect().width));
    await page.screenshot({path:'/tmp/compact-business-tags-desktop.png'});
    await page.setViewportSize({width:390,height:1000});
    assert(await aligned());
    await page.screenshot({path:'/tmp/compact-business-tags-mobile.png'});
    await page.evaluate(()=>document.querySelector('#label-samples').remove());
    await page.setViewportSize({width:1400,height:950});
    assert(await page.locator('[data-task]').evaluate(row=>{
      const box=row.getBoundingClientRect();
      return [...row.children].every(child=>child.getBoundingClientRect().bottom<=box.bottom+1);
    }));
    const listReadsBeforeState=requests.filter(r=>r.op==='tasks_list').length;
    await page.locator('[data-task] .ball').click();
    await page.waitForFunction(()=>document.querySelector('[data-task] .ball.working')?.getAttribute('aria-busy')==='false');
    assert.equal(task.status,'working');
    assert.equal(await page.locator('[data-editor]').count(),0);
    await page.locator('[data-task] .ball').click();
    await page.waitForFunction(()=>document.querySelector('[data-task] .ball.waiting')?.getAttribute('aria-busy')==='false');
    assert.equal(task.status,'waiting');
    assert.equal(requests.filter(r=>r.op==='tasks_list').length,listReadsBeforeState);
    await page.getByRole('button',{name:'New task',exact:true}).click();
    await page.getByLabel('Title',{exact:true}).fill('Keep this new task draft');
    task={...task,revision:task.revision+1,status:'open'};
    await page.evaluate(task=>taskStateObserve(null,task),task);
    assert.equal(await page.getByLabel('Title',{exact:true}).inputValue(),'Keep this new task draft');
    await page.getByRole('button',{name:'← Back'}).click();
    await page.waitForFunction(()=>document.querySelector('[data-task] .ball.open'));
    await page.locator('[data-task] .ttl').click();
    await page.locator('[data-editor]').waitFor();
    assert(requests.some(r => r.op === 'relationships_list' && r.args.item?.type === 'task'), 'Global Task detail asks for canonical related work');
    await page.getByRole('button',{name:'Related context',exact:true}).waitFor();
    assert.equal(await page.locator('.global-task-related').getByText('1 related',{exact:true}).count(), 1);
    assert.equal(await page.locator('.global-task-related button').count(), 1, 'no review or pending controls in Global');
    await page.getByLabel('Add a tag',{exact:true}).fill('verified'); await page.getByLabel('Add a tag',{exact:true}).press('Enter');
    await page.screenshot({path:'/tmp/global-task-compact-editor.png'});
    await page.getByLabel('Snooze',{exact:true}).selectOption('until');
    await page.getByLabel('Snooze until',{exact:true}).fill('2030-01-02T09:30');
    await page.getByRole('button',{name:'Save task',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('[data-global-task-status]').textContent==='Saved.');
    assert.equal(task.snoozed_until,'2030-01-02T13:30:00.000Z');
    assert.deepEqual(task.tags,['design/software','custom-note','verified']);
    assert.deepEqual(task.assignee_ids,createdAssignees);
    await page.getByRole('button',{name:'← Back'}).click();
    await page.getByText(/Snoozed until Jan/).waitFor();
    await page.getByLabel('Task visibility').selectOption('all');
    await page.locator('[data-task]').waitFor();
    assert.equal(requests.filter(r=>r.op==='tasks_list').at(-1).args.snooze_filter,'all');
    fs.mkdirSync('/dev/shm/qorqut-task-snooze-screenshots',{recursive:true});
    await page.screenshot({path:'/dev/shm/qorqut-task-snooze-screenshots/global.png'});
    await page.locator('[data-task]').click();
    const previousWrites = writes;
    await page.getByLabel('Snooze until',{exact:true}).fill('2000-01-01T09:30');
    await page.getByRole('button',{name:'Save task',exact:true}).click();
    await page.getByText('Choose a future snooze date and time.',{exact:true}).waitFor();
    assert.equal(writes,previousWrites);
    await page.getByLabel('Snooze',{exact:true}).selectOption('none');
    await page.getByRole('button',{name:'Save task',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('[data-global-task-status]').textContent==='Saved.');
    assert.equal(task.snoozed_until,null);
    await page.getByLabel('Status',{exact:true}).selectOption('done');
    await page.getByRole('button',{name:'Save task',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('[data-global-task-status]').textContent==='Saved.');
    assert.equal(task.status,'done');
    await page.evaluate(agentId=>mount({agentId,compact:true}),agent);
    await page.getByRole('button',{name:'Refresh tasks',exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'New task',exact:true}).count(),0);
    await page.waitForFunction(()=>document.querySelector('[data-global-task-status]').textContent==='');
    assert.equal(requests.filter(r=>r.op==='tasks_list').at(-1).args.assignee_id,agent);
    assert.equal(await page.locator('[data-search]').count(),0);
    const creator = await page.evaluate(id => {
      const task = {created_by:id,created_by_name:'ordubashy',created_by_kind:'agent',business:{slug:'example'}};
      const global = tAssignerAvatars(task);
      state.biz = 'example'; state.bizs = [{slug:'example',agent_meta:{ordubashy:{department:'general'}}}];
      return [global, tAssignerAvatars(task), tAssignerAvatars({...task,created_by:'different-agent'})];
    },agent);
    assert(creator[0].includes('#66d9e8') && creator[1].includes('#66d9e8'));
    assert(!creator[2].includes('#66d9e8'), 'Same name must not borrow a Global identity');
    assert.equal(await page.evaluate(() => globalAgentProfile(undefined)), null);
    await page.screenshot({path:'/tmp/ordubashy-compact-tasks.png'});

    // Global Task/Action thread calls (not reachable from this compact mount) go through the same shared caller.
    const threadCalls = await page.evaluate(async () => {
      const sent = [], original = window.post;
      window.post = async (url, body) => { sent.push({url, body}); return {status:'ok', data:{}}; };
      try {
        const source = {type:'action', id:'a1', biz:null};
        await conversationCall(source, 'actions_thread_get', {action_id:'a1', limit:20});
        await conversationCall(source, 'actions_comment', {action_id:'a1', text:'hi'}, {idempotency_key:'k1'});
        await conversationCall({type:'task', id:'t1', biz:null}, 'tasks_follow', {task_id:'t1', following:true}, {idempotency_key:'k2'});
      } finally { window.post = original; }
      return sent;
    });
    assert(threadCalls.every(c => c.url.startsWith('/root/operations/') && c.body.scope === undefined), 'Global thread calls carry no scope');
    for (const c of threadCalls) requests.push({op:c.url.split('/').at(-1), args:c.body});
    // Story/Email creation is explicit and shares the same optimistic Task editor.
    await page.evaluate(()=>mount());
    await page.getByRole('button',{name:'New task',exact:true}).click();
    await page.getByLabel('Title',{exact:true}).fill('Client story');
    await page.getByLabel('Task type',{exact:true}).selectOption('story');
    assert.equal(await page.getByRole('combobox',{name:'Status',exact:true}).count(),0, 'Stories have no status control');
    await page.getByLabel('Overview',{exact:true}).fill('Brief agreed; proposal next.');
    await page.getByRole('button',{name:'Create task',exact:true}).click();
    await page.getByRole('button',{name:'Save task',exact:true}).waitFor();
    assert.equal(task.task_type,'story');assert.equal(task.overview,'Brief agreed; proposal next.');
    assert.equal(Object.hasOwn(requests.filter(r=>r.op==='tasks_create').at(-1).args,'status'),false);
    assert.equal(requests.some(r=>['tasks_create','tasks_update'].includes(r.op) && JSON.stringify(r.args).includes('email_conversation_id')),false,'the legacy email link is never written');
    assert.equal(await page.getByRole('button',{name:'Add child task',exact:true}).count(),1);
    // Email is a historical type only: it is not offered, and no conversation can be chosen or searched.
    assert.equal(await page.getByLabel('Task type',{exact:true}).locator('option[value="email"]').count(),0);
    assert.equal(await page.getByLabel('Email conversation',{exact:true}).count(),0);
    assert.equal(task.email_conversation_id,null);
    await page.getByRole('button',{name:'← Back',exact:true}).click();
    await page.locator('[data-task] .task-type-marker .task-type-icon-story').waitFor();
    assert.equal(await page.locator('[data-task] .ball, [data-task] .ttl .task-type-icon').count(),0);
    if (process.env.GLOBAL_REQUESTS_OUT) fs.writeFileSync(process.env.GLOBAL_REQUESTS_OUT, JSON.stringify(requests.map(r => ({op:r.op, body:r.args}))));
    assert.deepEqual(errors,[]);
    console.log('Global task browser acceptance passed');
  } finally { await browser.close(); await new Promise(resolve=>server.close(resolve)); }
})().catch(error=>{console.error(error);process.exitCode=1;});
