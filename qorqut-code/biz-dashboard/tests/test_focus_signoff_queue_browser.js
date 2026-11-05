// Native workspace shell and screens against a fixture backend. No live records,
// terminal process or agent model is touched by this test.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const profiles = [['ordu', 'Ordubashy', 'ordubashy'], ['marketing', 'Marketing', 'global-worker'], ['founder', 'Founder', 'founder'], ['learning', 'Learning Applier', 'learning-applier']]
  .map(([agent_id, display_name, role]) => ({agent_id, name: agent_id, display_name, status: 'active', role: {id: role}, presentation: {department: 'operations'}, scope: {kind: 'ecosystem', allowed_business_ids: ['biz-a', 'biz-b'], global_capabilities: []}}));
const businesses = [{id: 'biz-a', slug: 'widget', name: 'Widget', agents: ['dev']}, {id: 'biz-b', slug: 'second', name: 'Second', agents: ['dev']}];
const states = ['open', 'working', 'waiting', 'done', 'cancelled'];
const board = {revision: 1, items: states.map((state, i) => ({id: 'focus-' + i, revision: 1, label: state, state,
  source: {type: 'task', id: 'task-' + i, business_id: 'biz-a'}, source_revision: 1,
  resolved_source: {type: 'task', id: 'task-' + i, business_id: 'biz-a', business_slug: 'widget', business_name: 'Widget', label: state, state}, references: []}))};
board.items.push({id:'focus-parent', revision:1, label:'Launch preparation', state:'open', source:{type:'task',id:'task-parent',business_id:'biz-a'}, source_revision:1,
  resolved_source:{type:'task',id:'task-parent',business_id:'biz-a',business_slug:'widget',business_name:'Widget',label:'Launch preparation',state:'open'}, references:[]});
const signoff={...JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/signoffs.json'),'utf8')).data.items[0],id:'signoff-choice',review_business_id:null,category:'input_request',title:'Choose the launch date',
  input:{title:'Choose the launch date',explanation:'Can the launch proceed?',kind:'question',selection_mode:'single',choices:[{id:'yes',label:'Yes'},{id:'hold',label:'Hold'}],selection_required:true,notes_required:false}};
board.items.push({id:'focus-signoff',revision:1,label:signoff.title,display_name:'Launch decision',source:{type:'signoff',id:signoff.id,business_id:null},state:'pending'});
const otherSignoffs=[{...signoff,id:'message',category:'internal_change',operation_name:'whatsapp_send',title:'Send WhatsApp message'},{...signoff,id:'email',category:'internal_change',operation_name:'account_email_reply',title:'Send email reply'}];
// The same owner queue includes a Signoff whose exact review scope is a Project.
const businessSignoff={...signoff,id:'signoff-biz',review_business_id:'biz-a',title:'Approve the widget plan',input:{...signoff.input,title:'Approve the widget plan'}};
const chatEntries=Array.from({length:24},(_,n)=>({id:'chat-'+n,kind:'message',speaker:n%2?'agent':'user',text:('Background conversation line '+n+' kept while a queue Signoff is reviewed. ').repeat(3),timestamp:null,position:(n+1)*100,partial:false,identity:'native_id',completion_sequence:null,completion_id:null,system:null}));
const signoffAnswers=[];
const calls = [];
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  // Production forbids navigating an iframe to the dashboard document.
  res.setHeader('X-Frame-Options', 'DENY');
  res.setHeader('Content-Security-Policy', "frame-ancestors 'none'");
  if (url.pathname.startsWith('/api/')) {
    calls.push(url.pathname);
    let data = {items: [], messages: [], purposes: [], tags: [], agents: [], totals: {}};
    if (url.pathname === '/api/biz') data = businesses;
    else if (/\/biz\/[^/]+\/agents/.test(url.pathname)) data = [];
    else if (url.pathname === '/api/session') data = {principal_id: 'owner', principal_kind: 'human', is_owner: true, is_installation_owner: true};
    else if (url.pathname === '/api/root/signoffs') data={items:[signoff,businessSignoff,...otherSignoffs].filter(s=>s.state==='pending'),next_cursor:null};
    else if (url.pathname === '/api/root/signoffs/signoff-choice') data={signoff};
    else if (url.pathname === '/api/root/signoffs/signoff-choice/respond') {
      let raw='';for await(const chunk of req)raw+=chunk;signoffAnswers.push(JSON.parse(raw));
      signoff.state='answered';signoff.status_revision++;data={signoff};
    }
    else if (url.pathname === '/api/root/signoffs/signoff-biz') data={signoff:businessSignoff};
    else if (url.pathname === '/api/root/signoffs/signoff-biz/respond') {
      let raw='';for await(const chunk of req)raw+=chunk;signoffAnswers.push(JSON.parse(raw));
      businessSignoff.state='answered';businessSignoff.status_revision++;data={signoff:businessSignoff};
    }
    else if (url.pathname === '/api/root/agents') data = {agents: profiles};
    else if (url.pathname === '/api/root/agent-pins') data = {initialized: true, pinned_agents: []};
    else if (url.pathname.endsWith('/focus_get')) data = board;
    else if (url.pathname.endsWith('/workspace_types')) data = {types: [{key: 'focus', display_name: 'Focus', supported_roles: ['ordubashy', 'global-worker'], renderer: 'focus_board'}]};
    else if (url.pathname.endsWith('/tasks_list')) data = {tasks:[{id:'child-task',title:'Review the homepage',status:'waiting',revision:1,archived:false,closed:false}],next_cursor:null};
    else if (url.pathname.endsWith('/relationships_list')) data = {items:[],next_cursor:null};
    else if (url.pathname.endsWith('/global_progress_get')) data = {series:Array.from({length:365}, (_,i) => ({date:new Date(Date.UTC(2026,0,1+i)).toISOString().slice(0,10),tasks_done:i%9,emails_sent:i%25,inbox_messages:i%16,learnings_accepted:i%3,new_tickets:i%5,cold_outreach_emails_sent:i%8}))};
    else if (url.pathname.endsWith('/focus-layout')) data = {revision:1,blocks:[],card_ids:board.items.map(item=>item.id)};
    else if (url.pathname.endsWith('/calendar_calendars_list')) data = {configured:true,calendars:[],errors:[]};
    else if (url.pathname.endsWith('/browser-surfaces')) data = {agent_id:url.pathname.split('/')[4],browsers:[{id:'browser',label:'Browser',kind:'browser',viewer_url:`http://${req.headers.host}/screen/browser`}],desktop:{id:'desktop',label:'Desktop',kind:'desktop',viewer_url:`http://${req.headers.host}/screen/desktop`}};
    else if (/\/agents\/[^/]+\/chat$/.test(url.pathname)) data = {agent_id:url.pathname.split('/')[4], session: {state: 'live', reason: 'live', can_send: true, working: false, provider: 'claude', conversation_id: 'conv-1', transport: 'terminal', detail: 'Live', binding: {binding_id: 'b1', agent_id: 'a', provider: 'claude', conversation_id: 'conv-1', generation: 1, transport: 'terminal'}}, revision: 0, cursor: 0, conversation: {conversation_id: 'conv-1', provider: 'claude', complete: true, finalized: false, error: null, captured_length: 0}, conversations: [{conversation_id: 'conv-1', provider: 'claude', updated_at: null, current: true}], entries: chatEntries, older: null, rendered_boundary: 0, inputs: [], last_read_sequence: 0, newest_sequence: 0};
    res.setHeader('Content-Type', 'application/json');
    return res.end(JSON.stringify(url.pathname === '/api/biz' || /\/biz\/[^/]+\/agents/.test(url.pathname) ? data : {status: 'ok', data}));
  }
  const file = path.resolve(root, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});
(async () => {
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    const page=await browser.newPage({viewport:{width:1400,height:900}}),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    const origin=`http://127.0.0.1:${server.address().port}`;
    await page.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
    await page.addInitScript(()=>{window.EventSource=class{addEventListener(){} close(){}};});
    await page.goto(origin+'/#/global/agents');
    await page.getByRole('button',{name:'Review Launch decision',exact:true}).waitFor();
    assert.equal(await page.locator('.focus-signoff-chip').first().evaluate(el=>getComputedStyle(el).borderWidth),'0px','Signoff chips are borderless');
    assert.equal(await page.locator('.agfocus-surface-board [data-signoff-key="signoff-choice"]').count(),1,'pending Signoff sits beside Browser/Desktop');
    assert.equal(await page.locator('.agfocus > .focus-signoff-queue').count(),0,'no duplicate Signoff row');
    assert.equal(calls.some(url=>url.endsWith('/viewbox')),false,'the toolbar does not load documents');
    assert.deepEqual(await page.locator('.focus-signoff-chip [data-signoff-kind]').evaluateAll(icons=>icons.map(icon=>icon.dataset.signoffKind)),['approval','approval','message','email']);
    assert.deepEqual(await page.locator('.focus-signoff-chip').allTextContents(),['','','',''],'queue shows only icons');
    assert.equal(await page.locator('[data-signoff-key="signoff-choice"]').getAttribute('title'),'Launch decision','hover shows the display name');
    assert.equal(await page.locator('[data-signoff-key="signoff-biz"]').getAttribute('title'),'Approve the widget plan');
    assert.equal(await page.locator('[data-signoff-key="message"]').getAttribute('title'),'Send WhatsApp message');
    if(process.env.FOCUS_SCREENSHOT)await page.locator('.attention-queue').screenshot({path:process.env.FOCUS_SCREENSHOT});
    assert.equal(await page.locator('.agfocus-items [data-card-id="focus-signoff"]').count(),0,'Signoffs leave the task list for their own queue');
    assert.equal(await page.locator('.attention-queue [data-attention-key^="signoff:"]').count(),0);
    // The main chat is the prior workspace; a queue review must neither replace it nor lose its draft and scroll.
    await page.locator('.gchat-msg').first().waitFor();
    await page.locator('.gchat-composer textarea').fill('Draft kept while the Signoff modal opens');
    await page.evaluate(()=>{const scroll=document.querySelector('.gchat-scroll');scroll.scrollTop=scroll.scrollHeight;window.__queueRoot=document.querySelector('.global-agent-session .gchat');});
    const chatReads=()=>calls.filter(path=>path==='/api/root/agents/ordu/chat').length;
    const listReads=()=>calls.filter(path=>path==='/api/root/signoffs').length;
    const chatState=()=>page.evaluate(()=>({root:document.querySelector('.global-agent-session .gchat')===window.__queueRoot,
      draft:document.querySelector('.gchat-composer textarea')?.value||'',top:document.querySelector('.gchat-scroll')?.scrollTop||0,
      entries:document.querySelectorAll('.gchat-msg').length}));
    const chatBefore=await chatState(),chatReadsBefore=chatReads();
    const cardsBefore=await page.locator('.agfocus-items .agfocus-card').evaluateAll(nodes=>nodes.map(node=>node.dataset.cardId));
    assert.equal(chatBefore.root,true);assert.ok(chatBefore.top>0,'the main chat can scroll');
    const assertWorkspaceKept=async label=>{
      assert.deepEqual(await chatState(),chatBefore,label+': main chat node, scroll and draft are retained');
      assert.equal(chatReads(),chatReadsBefore,label+': no chat state refetch');
      assert.equal(await page.locator('.agfocus-dock').isVisible(),false,label+': no record workspace opens');
      assert.deepEqual(await page.locator('.agfocus-items .agfocus-card').evaluateAll(nodes=>nodes.map(node=>node.dataset.cardId)),cardsBefore,label+': the Focus list is unchanged');
    };
    // A Global review scope opens the standard Signoff review modal for the exact proposal, not a record thread.
    await page.getByRole('button',{name:'Review Launch decision',exact:true}).click();
    await page.locator('.attention-signoff-view [data-choice="yes"]').waitFor();
    assert.equal(await page.locator('.modal').count(),1,'the queue opens a modal');
    assert.equal(await page.locator('.attention-signoff-view .sg-row[data-id="signoff-choice"]').count(),1,'the exact Global Signoff is selected');
    assert.ok(calls.includes('/api/root/signoffs/signoff-choice'),'the exact Signoff is read');
    assert.match(await page.locator('.attention-signoff-view').innerText(),/Choose the launch date/);
    assert.equal(await page.locator('.agfocus-dock .rw-record').count(),0,'no record thread mounts for the queue Signoff');
    assert.deepEqual(await chatState(),chatBefore,'opening the modal does not redraw the main chat');
    await page.locator('.attention-signoff-view [data-choice="yes"]').click();
    await page.locator('.attention-signoff-view [data-sg="respond"]').click();
    await page.waitForFunction(()=>!document.querySelector('.attention-signoff-view [data-sg="respond"]'));
    assert.equal(signoffAnswers.length,1,'the existing Signoff form submits once');
    assert.equal(signoffAnswers[0].proposal_revision,signoff.proposal_revision);
    assert.equal(signoffAnswers[0].payload_hash,signoff.payload_hash);
    const listBeforeClose=listReads();
    await page.getByRole('button',{name:'Close',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('[data-signoff-key="signoff-choice"]'));
    assert.equal(await page.locator('.attention-signoff-view').count(),0,'closing disposes the Signoff view');
    assert.ok(listReads()>listBeforeClose,'closing refreshes the queue');
    await assertWorkspaceKept('Closing the Global Signoff modal');
    // A business review scope still opens from the owner global queue and keeps its exact Project identity.
    const bizChip=page.getByRole('button',{name:'Review Approve the widget plan',exact:true});
    await bizChip.click();
    await page.locator('.attention-signoff-view [data-choice="yes"]').waitFor();
    assert.equal(await page.locator('.attention-signoff-view .sg-row[data-id="signoff-biz"]').count(),1,'the exact business Signoff is selected');
    assert.ok(calls.includes('/api/root/signoffs/signoff-biz'),'the owner route reads the exact business proposal');
    assert.match(await page.locator('.attention-signoff-view .sg-row[data-id="signoff-biz"] .sg-rowmeta').innerText(),/Widget/,'the Project review scope survives');
    assert.equal(await page.locator('.agfocus-dock .rw-record').count(),0,'no record thread mounts for a business Signoff');
    assert.deepEqual(await chatState(),chatBefore,'opening a business Signoff modal keeps the main chat');
    const listBeforeBizClose=listReads();
    await page.getByRole('button',{name:'Close',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('.attention-signoff-view'));
    await page.waitForFunction(()=>!!document.querySelector('[data-signoff-key="signoff-biz"]'));
    assert.ok(listReads()>listBeforeBizClose,'closing the business Signoff refreshes the queue');
    assert.equal(await bizChip.count(),1,'an undecided business Signoff remains queued');
    await assertWorkspaceKept('Closing the business Signoff modal');

    const queue=page.locator('.agfocus-surface-board');
    assert.equal(await queue.locator('[data-signoff-key]').count(),3,'only the decided Signoff leaves the queue');
    for(const viewport of [{width:1400,height:900},{width:390,height:844}]){
      await page.setViewportSize(viewport);
      const layout=await queue.evaluate(el=>{
        const fixed=document.querySelector('.agfocus-fixed').getBoundingClientRect(),box=el.getBoundingClientRect();
        el.scrollLeft=1000;
        return {beside:box.left>=fixed.right,nowrap:getComputedStyle(el).flexWrap==='nowrap',inside:box.right<=innerWidth,reachable:el.scrollWidth<=el.clientWidth||el.scrollLeft>0};
      });
      assert.deepEqual(layout,{beside:true,nowrap:true,inside:true,reachable:true});
    }
    for(const [operation,expected] of [['telegram_send','message'],['telegram_send_file','message'],['agents_message_send','message'],['email_send','email'],['email_reply','email'],['email_forward','email'],['account_email_send','email'],['account_email_forward','email'],['email_delete','approval'],['unknown_operation','approval']]){
      otherSignoffs[0].operation_name=operation;
      await page.evaluate(()=>attentionQueue.refreshSignoffs());
      assert.equal(await page.locator('[data-signoff-key="message"] [data-signoff-kind]').getAttribute('data-signoff-kind'),expected,operation);
    }
    otherSignoffs[0].platform={submission:{tool:'email.send'}};
    await page.evaluate(()=>attentionQueue.refreshSignoffs());
    assert.equal(await page.locator('[data-signoff-key="message"] [data-signoff-kind]').getAttribute('data-signoff-kind'),'email','platform email uses the envelope too');
    otherSignoffs.forEach(s=>s.state='answered');businessSignoff.state='answered';
    await page.evaluate(()=>attentionQueue.refreshSignoffs());
    assert.equal(await queue.isVisible(),false);
    assert.equal(await page.locator('.agfocus-divider').isVisible(),false);
    assert.equal(await page.locator('[data-focus-surface]').count(),2,'empty queue retains both controls');
    assert.deepEqual(errors,[]);
    console.log('Focus Signoff queue: modal review with exact Global/business scope, disposed close and queue refresh, preserved chat/Focus workspace, position, removal, empty state and narrow scrolling passed.');
  } finally {await browser.close();server.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
