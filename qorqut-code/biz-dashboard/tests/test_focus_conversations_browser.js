// Shared record conversations reached through Focus against a fixture backend. No live records,
// terminal process or agent model is touched by this test.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const profiles = [['ordu', 'Ordubashy', 'ordubashy'], ['marketing', 'Marketing', 'global-marketing'], ['founder', 'Founder', 'founder'], ['learning', 'Learning Applier', 'learning-applier']]
  .map(([agent_id, display_name, role]) => ({agent_id, name: agent_id, display_name, status: 'active', role: {id: role}, presentation: {department: 'operations'}, scope: {kind: 'ecosystem', allowed_business_ids: ['biz-a', 'biz-b'], global_capabilities: []}}));
const businesses = [{id: 'biz-a', slug: 'widget', name: 'Widget', agents: ['dev']}, {id: 'biz-b', slug: 'second', name: 'Second', agents: ['dev']}];
const states = ['open', 'working', 'waiting', 'done', 'cancelled'];
const board = {revision: 1, items: states.map((state, i) => ({id: 'focus-' + i, revision: 1, label: state, state,
  source: {type: 'task', id: 'task-' + i, business_id: 'biz-a'}, source_revision: 1,
  resolved_source: {type: 'task', id: 'task-' + i, business_id: 'biz-a', business_slug: 'widget', business_name: 'Widget', label: state, state}, references: []}))};
board.items.push({id:'focus-parent', revision:1, label:'Launch preparation', state:'open', source:{type:'task',id:'task-parent',business_id:'biz-a'}, source_revision:1,
  resolved_source:{type:'task',id:'task-parent',business_id:'biz-a',business_slug:'widget',business_name:'Widget',label:'Launch preparation',state:'open'}, references:[]});
board.items.push({id:'focus-crm',revision:1,label:'Launch CRM',state:'open',source:{type:'table',id:'crm-project',business_id:'biz-a'},resolved_source:{type:'table',id:'crm-project',business_id:'biz-a',business_slug:'widget',label:'Launch CRM',resource_type:'data_tables'},references:[]});
const signoff={...JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/signoffs.json'),'utf8')).data.items[0],id:'signoff-choice',review_business_id:null,category:'input_request',title:'Choose the launch date',
  input:{title:'Choose the launch date',explanation:'Can the launch proceed?',kind:'question',selection_mode:'single',choices:[{id:'yes',label:'Yes'},{id:'hold',label:'Hold'}],selection_required:true,notes_required:false}};
board.items.push({id:'focus-signoff',revision:1,label:signoff.title,display_name:'Launch decision',source:{type:'signoff',id:signoff.id,business_id:null},state:'pending'});
const otherSignoffs=[{...signoff,id:'message',category:'internal_change',operation_name:'whatsapp_send',title:'Send WhatsApp message'},{...signoff,id:'email',category:'internal_change',operation_name:'account_email_reply',title:'Send email reply'}];
for(const item of board.items)item.conversation=item.source;
const signoffAnswers=[];
const calls = [];

const whatsapp={...signoff,id:'whatsapp',input:null,category:'internal_change',operation_name:'whatsapp_send',title:'Send the project update',review_business_id:'biz-a',created_at:'2026-10-27T09:30:00Z',preview:{changes:[{kind:'structured',resource:{resource_type:'whatsapp_account',resource_id:'account'},fields:Object.entries({chat_id:'12345678901@s.whatsapp.net',chat_name:'Alex',text:'Hi Alex, the updated homepage is ready.\nPlease review the mobile layout before Friday. <script>window.previewInjected=true</script>'}).map(([path,after])=>({path,after}))}]}};
const email={...signoff,id:'email-preview',input:null,category:'internal_change',operation_name:'account_email_reply',title:'Reply to the project review',created_at:'2026-10-27T09:45:00Z',preview:{changes:[{kind:'structured',resource:{resource_type:'fastmail_account',resource_id:'account'},fields:Object.entries({from_address:'owner@example.test',to:['alex@example.test'],subject:'Homepage review',body:'Thanks Alex, we will update the mobile layout today.'}).map(([path,after])=>({path,after}))}]}};
board.items.push({id:'focus-email',revision:1,label:email.title,state:'pending',source:{type:'signoff',id:email.id,business_id:null},conversation:{type:'signoff',id:email.id,business_id:null}});
let previewDenied=false;
const related=[{type:'signoff',id:'signoff-choice',business_id:null,label:'Approve the message',state:'pending',created_at:'2026-10-27T09:00:00Z'},{type:'task',id:'subtask',business_id:null,label:'Review homepage',state:'working',created_at:'2026-10-27T10:00:00Z'}];
related.push({type:'signoff',id:whatsapp.id,business_id:'biz-a',business_slug:'widget',label:whatsapp.title,state:'pending',created_at:whatsapp.created_at});
const messages=id=>[{id:'message-'+id,sequence:1,body:'A real private message',created_by:'owner',author_kind:'human',is_owner:true,created_at:'2026-10-27T08:00:00Z',refs:[{type:'task',id:'subtask',business_id:null,label:'Review homepage'},{type:'signoff',id:whatsapp.id,business_id:'biz-a',business_slug:'widget',label:'Project update'}]}];
const sent=[];let relatedFail=false;
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
    else if (url.pathname === '/api/root/signoffs') data={items:[signoff,...otherSignoffs,email].filter(s=>s.state==='pending'),next_cursor:null};
    else if (url.pathname === '/api/root/signoffs/signoff-choice') data={signoff};
    else if(url.pathname === '/api/biz/widget/signoffs/whatsapp') {
      if(previewDenied){res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({status:'error',error:{code:'forbidden',message:'Unavailable'}}));}
      data={signoff:whatsapp};
    }
    else if(url.pathname === '/api/root/signoffs/email-preview')data={signoff:email};
    else if (url.pathname === '/api/root/signoffs/signoff-choice/respond') {
      let raw='';for await(const chunk of req)raw+=chunk;signoffAnswers.push(JSON.parse(raw));
      signoff.state='answered';signoff.status_revision++;data={signoff};
    }
    else if (url.pathname === '/api/root/agents') data = {agents: profiles};
    else if (url.pathname === '/api/root/agent-pins') data = {initialized: true, pinned_agents: []};
    else if(url.pathname.endsWith('/owner_conversation_related')) {
      if(relatedFail){res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({status:'error',error:{code:'forbidden',message:'Unavailable'}}));}
      let raw='';for await(const chunk of req)raw+=chunk;const offset=JSON.parse(raw).offset || 0;
      data={items:related.slice(offset,offset+1),next_offset:offset+1<related.length?offset+1:null};
    }
    else if(url.pathname.includes('/owner_conversation_')) {
      let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);
      if(url.pathname.endsWith('/owner_conversation_send')){sent.push(body);}
      data={conversation:{id:body.source.id,source:body.source,audience:'owner_ordubashy',revision:1,latest_sequence:1,message_count:1},messages:messages(body.source.id),next_before:null,participants:[],read_state:{last_read_sequence:1,unread:false}};
    }
    else if(url.pathname.endsWith('/tasks_get')) {
      let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);
      data={task:{id:body.task_id,title:'Task',status:'working',revision:1,tags:[],assignee_ids:[]}};
    }
    else if(url.pathname.endsWith('/focus_get')) data=board;
    else if(url.pathname.endsWith('/tables_get')) data={table:{id:'crm-project',title:'Launch CRM',description:'Project pipeline',business_id:'biz-a',columns:[{id:'name',name:'Name'}],rows_count:1,table_kind:'crm'}};
    else if (url.pathname.endsWith('/workspace_types')) data={types:[{key:'focus',display_name:'Focus',supported_roles:['ordubashy'],renderer:'focus_board'},{key:'threads',display_name:'Threads',supported_roles:['ordubashy'],renderer:'threads'}]};
    else if (url.pathname.endsWith('/tasks_list')) data = {tasks:[{id:'child-task',title:'Review the homepage',status:'waiting',revision:1,archived:false,closed:false}],next_cursor:null};
    else if (url.pathname.endsWith('/relationships_list')) data = {items:[],next_cursor:null};
    else if (url.pathname.endsWith('/global_progress_get')) data = {series:Array.from({length:365}, (_,i) => ({date:new Date(Date.UTC(2026,0,1+i)).toISOString().slice(0,10),tasks_done:i%9,emails_sent:i%25,inbox_messages:i%16,learnings_accepted:i%3,new_tickets:i%5,cold_outreach_emails_sent:i%8}))};
    else if (url.pathname.endsWith('/focus-layout')) data = {revision:1,blocks:[{id:'11111111-1111-4111-8111-111111111111',text:'Normal Focus note',before_id:null,after_id:null}],card_ids:board.items.map(item=>item.id)};
    else if (url.pathname.endsWith('/calendar_calendars_list')) data = {configured:true,calendars:[],errors:[]};
    else if (url.pathname.endsWith('/browser-surfaces')) data = {agent_id:url.pathname.split('/')[4],browsers:[{id:'browser',label:'Browser',kind:'browser',viewer_url:`http://${req.headers.host}/screen/browser`}],desktop:{id:'desktop',label:'Desktop',kind:'desktop',viewer_url:`http://${req.headers.host}/screen/desktop`}};
    else if (/\/agents\/[^/]+\/chat$/.test(url.pathname)) data = {agent_id:url.pathname.split('/')[4], session: {state: 'live', reason: 'live', can_send: true, working: false, provider: 'claude', conversation_id: 'conv-1', transport: 'terminal', detail: 'Live', binding: {binding_id: 'b1', agent_id: 'a', provider: 'claude', conversation_id: 'conv-1', generation: 1, transport: 'terminal'}}, revision: 0, cursor: 0, conversation: {conversation_id: 'conv-1', provider: 'claude', complete: true, finalized: false, error: null, captured_length: 0}, conversations: [{conversation_id: 'conv-1', provider: 'claude', updated_at: null, current: true}], entries: [{id:'main-message',speaker:'assistant',text:'Open the CRM or a task from this conversation',timestamp:'2026-10-27T08:00:00Z',position:1,references:[]}], older: null, rendered_boundary: 0, inputs: [], last_read_sequence: 0, newest_sequence: 0};
    res.setHeader('Content-Type', 'application/json');
    return res.end(JSON.stringify(url.pathname === '/api/biz' || /\/biz\/[^/]+\/agents/.test(url.pathname) ? data : {status: 'ok', data}));
  }
  const file = path.resolve(root, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});
const sources=['js/conversation.js','js/chat-view.js','js/agent-focus.js','js/record-workspace.js','css/agent-focus.css','css/record-workspace.css','js/global-chat.js'].map(f=>fs.readFileSync(path.join(root,f),'utf8')).join('\n');
(async()=>{
 // The custom related-record UI, its preview reader and thread-only message styling no longer exist.
 for(const gone of ['conversationSignoffPreview','gchat-related','relatedPreview','relatedRecord','conversation-thread','agthreadsOpenRecord','agthreads-details','agthreads-source','agfocus-thread-context{flex:0 0 20%','agfocusThreadHeading','agfocusOpenRecord','data-manage','record-toggle','agfocus-thread-heading'])
  assert.equal(sources.includes(gone),false,'Removed: '+gone);
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,args:['--no-sandbox']});
 const shots=process.env.FOCUS_CONVERSATION_SHOTS;
 try {
  const page=await browser.newPage({viewport:{width:1400,height:900}}),errors=[];page.setDefaultTimeout(10000);
  page.on('pageerror',e=>errors.push(e.message));
  const origin=`http://127.0.0.1:${server.address().port}`;
  await page.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
  await page.addInitScript(()=>{window.EventSource=class{addEventListener(){} close(){}};});
  await page.goto(origin+'/#/global/agents');
  await page.locator('.agfocus-items [data-card-id="focus-parent"]').waitFor();
  assert.equal(await page.locator('[data-focus-today]').count(),0);
  const tabs=await page.locator('.ttab').evaluateAll(nodes=>nodes.map(n=>n.dataset.v));
  assert.equal(tabs.includes('threads'),false);
  assert.equal(calls.some(p=>p.endsWith('owner_conversation_mark_read')),false,'Listing Focus does not acknowledge');
  const dock=page.locator('.agfocus .agfocus-dock');
  const pane=scope=>scope.locator('.rw-record:not([hidden])');
  const thread=page.locator('.global-agent-session .gchat-paired-record');
  const own=page.locator('.gchat-paired-record > .conversation-inline-compose textarea');
  const setDraft=text=>own.evaluate((el,value)=>{el.value=value;el.dispatchEvent(new Event('input',{bubbles:true}));},text);
  await page.evaluate(()=>{
    window.crmThreadMount=()=>({dispose(){},dirty(){return false;},save(){return true;},discard(){return true;}});
    window.crmTableMount=(host,options)=>{host.innerHTML='<h3>Launch CRM</h3><button type="button" data-fixture-crm-row>Global row</button>';host.querySelector('[data-fixture-crm-row]').onclick=()=>options.onOpenRecord({record_ref:{id:'global-row',business_id:null},record:{title:'Global row',task_type:'task',status:'working'}});return {dispose(){host.replaceChildren();},dirty(){return false;},save(){return true;},discard(){return true;},refresh(){}};};
  });
  await page.locator('.global-agent-session .gchat-msg[data-id="main-message"]').waitFor();
  await page.locator('#agpad .ttab[data-v="status"]').click();
  await page.waitForFunction(()=>_agpad.view==='status');
  await page.locator('.global-agent-session .gchat-msg[data-id="main-message"]').evaluate(entry=>{
    entry._entry.references=[{key:'main-row',label:'Main task',value:{type:'task',id:'main-row',business_id:null,label:'Main task'}}];
    const button=document.createElement('button');button.type='button';button.className='gchat-reference';button.dataset.refKey='main-row';button.textContent='Main task';entry.append(button);button.click();
  });
  await dock.locator('.rw-record:not([hidden]) .rw-title').filter({hasText:'Main task'}).waitFor();
  await thread.waitFor();
  assert.equal(await thread.getAttribute('data-conversation'),'task:global:main-row','A main-transcript task reference binds its canonical source');
  assert.equal(await pane(dock).locator('.rw-context').isVisible(),false,'A standalone main-chat row has no CRM parent');
  assert.equal(await page.evaluate(()=>_agpad.view),'focus','A main-chat reference selects and mounts Focus from another right-pane view');
  assert.equal(await page.locator('.agfocus-detail,.agfocus-overlay,.rw-modal').count(),0,'References open the record workspace without a legacy modal or viewer');
  assert.equal(await page.evaluate(()=>!!_agpad.focusOverlay),false,'The Focus record uses the right pane without covering the left transcript');
  assert.equal(await page.locator('.agfocus-chat-overlay').count(),0);
  assert.equal(await page.locator('.global-agent-session').evaluate(node=>node.inert),false,'The paired left transcript stays interactive');
  await pane(dock).locator('.rw-close').evaluate(button=>button.click());
  await page.waitForFunction(()=>!document.querySelector('.gchat-paired-record'));
  await page.locator('#agpad .ttab[data-v="status"]').click();
  await page.waitForFunction(()=>_agpad.view==='status');
  await page.locator('.global-agent-session .gchat-msg[data-id="main-message"]').evaluate(entry=>{
    entry._entry.references=[{key:'main-table',label:'Launch CRM',value:{type:'table',id:'crm-project',business_id:'biz-a',label:'Launch CRM'}}];
    const button=document.createElement('button');button.type='button';button.className='gchat-reference';button.dataset.refKey='main-table';button.textContent='Launch CRM';entry.append(button);button.click();
  });
  await dock.locator('.rw-record:not([hidden]) .rw-title').filter({hasText:'Launch CRM'}).waitFor();
  await thread.waitFor();
  assert.equal(await thread.getAttribute('data-conversation'),'table:widget:crm-project','The main transcript table reference binds the selected CRM source');
  await thread.locator('.gchat-user .gchat-ref-row').filter({hasText:'Review homepage'}).first().evaluate(button=>button.click());
  await dock.locator('.rw-record:not([hidden]) .rw-title').filter({hasText:'Review homepage'}).waitFor();
  assert.equal(await pane(dock).locator('.rw-context').isVisible(),false,'A main-source task reference does not inherit an unrelated selected CRM parent');
  assert.equal(await thread.getAttribute('data-conversation'),'task:global:subtask');
  await pane(dock).locator('.rw-back').click();
  await dock.locator('.rw-record:not([hidden]) .rw-title').filter({hasText:'Launch CRM'}).waitFor();
  await page.locator('.rw-dashboard [data-fixture-crm-row]').evaluate(button=>button.click());
  await dock.locator('.rw-title').filter({hasText:'Global row'}).waitFor();
  assert.equal(await pane(dock).locator('.rw-context').textContent(),'Launch CRM /','A Global row keeps its Project CRM parent breadcrumb');
  assert.equal(await thread.getAttribute('data-conversation'),'task:global:global-row','The row opens its canonical Global private conversation');
  assert.equal(await dock.locator('.rw-dashboard h3').count(),1,'The CRM mount owns its table title without a duplicate record title');
  await pane(dock).locator('.rw-back').click();
  await dock.locator('.rw-title').filter({hasText:'Launch CRM'}).waitFor();
  assert.equal(await thread.getAttribute('data-conversation'),'table:widget:crm-project','Back restores the Project CRM conversation');
  await page.locator('.rw-close').evaluate(button=>button.click());

  // A direct Focus Task/Story selection retains its established right-workspace Chat.
  await page.locator('[data-card-id="focus-parent"] .ttl').click();
  const storyThread=pane(dock).locator('.rw-conversation');
  await storyThread.locator('.gchat-reference-only').first().waitFor();
  await storyThread.locator('.sg-whatsapp').waitFor();
  assert.equal(await dock.locator('.rw-title').textContent(),'Launch preparation');
  assert.equal(await dock.locator('.agfocus-layout.agfocus-paired-workspace').count(),0,'ordinary Focus Task selection is not paired');
  assert.equal(await dock.locator('.rw-tabs [data-rw-tab="chat"]').count(),1,'ordinary Focus Task retains its Chat tab');
  assert.equal(await page.locator('.global-agent-session').isVisible(),true,'the original agent chat remains in its own pane');
  assert.equal(await storyThread.locator('.gchat-reference-only').count(),2,'related records stay in the ordinary Focus conversation');
  assert.equal(await storyThread.locator('.gchat-user .gchat-ref-row').count(),1,'the Task reference uses the grouped Story row presentation');
  assert.equal(await storyThread.locator('.gchat-user .gchat-references .gchat-reference').count(),1,'the Signoff remains on its source message');
  assert.equal(await storyThread.locator('.sg-row[data-id="whatsapp"] .sg-conversation textarea').count(),1);
  assert.equal(await page.locator('.conversation-thread,.agfocus-detail').count(),0,'No third pane or detail screen is created');
  const storyOwn=storyThread.locator(':scope > .conversation-inline-compose textarea');
  const setStoryDraft=text=>storyOwn.evaluate((el,value)=>{el.value=value;el.dispatchEvent(new Event('input',{bubbles:true}));},text);
  await setStoryDraft('Parent conversation draft');
  const taskRef=storyThread.locator('.gchat-user .gchat-ref-row').filter({hasText:'Review homepage'}).first();
  await taskRef.evaluate(button=>button.click());
  await dock.locator('.rw-title').filter({hasText:'Review homepage'}).waitFor();
  assert.equal(await pane(dock).locator('.rw-context').isVisible(),false,'Standalone references do not invent a CRM parent');
  assert.equal(await page.locator('.gchat-paired-record').count(),0,'a related Focus reference does not switch into paired mode');
  const childOwn=pane(dock).locator('.rw-conversation > .conversation-inline-compose textarea');
  assert.equal(await childOwn.inputValue(),'','the selected record has its own composer');
  await childOwn.fill('Child conversation draft');
  await pane(dock).locator('.rw-back').click();
  await dock.locator('.rw-title').filter({hasText:'Launch preparation'}).waitFor();
  assert.equal(await storyOwn.inputValue(),'Parent conversation draft','Back restores the prior source conversation and its draft');
  await taskRef.evaluate(button=>button.click());
  await dock.locator('.rw-title').filter({hasText:'Review homepage'}).waitFor();
  assert.equal(await childOwn.inputValue(),'Child conversation draft','The child source draft survives navigating away and back');
  await childOwn.fill('Private task comment');
  await pane(dock).locator('.rw-conversation > .conversation-inline-compose .gchat-send').evaluate(button=>button.click());
  await page.waitForTimeout(150);
  assert.deepEqual(sent.map(m=>[m.source.type,m.source.id,m.text,m.source.business_id ?? null]),[['task','subtask','Private task comment',null]],'The private composer sends against the selected canonical Global source');
  await page.evaluate(async()=>globalAgentViewActive.selectPairedSource({type:'task',id:'same-id',business_id:'biz-a'},10001));
  await page.waitForFunction(()=>document.querySelector('.gchat-paired-record')?.dataset.conversation==='task:widget:same-id');
  await setDraft('Project only draft');
  await page.evaluate(async()=>globalAgentViewActive.selectPairedSource({type:'task',id:'same-id',business_id:null},10002));
  await page.waitForFunction(()=>document.querySelector('.gchat-paired-record')?.dataset.conversation==='task:global:same-id');
  await setDraft('Global only draft');
  await page.evaluate(async()=>globalAgentViewActive.selectPairedSource({type:'task',id:'same-id',business_id:'biz-a'},10003));
  await page.waitForFunction(()=>document.querySelector('.gchat-paired-record')?.dataset.conversation==='task:widget:same-id');
  assert.equal(await own.inputValue(),'Project only draft','Same-id Project and Global sources retain separate drafts');
  await page.locator('.gchat-paired-record > .conversation-inline-compose .gchat-send').evaluate(button=>button.click());
  await page.waitForTimeout(150);
  assert.deepEqual(sent.map(m=>[m.source.id,m.source.business_id ?? null,m.text]),[['subtask',null,'Private task comment'],['same-id','biz-a','Project only draft']], 'privateSource send keeps the canonical scope');
  assert.equal(signoffAnswers.length,0,'Opening references never decides a signoff');
  assert.equal(calls.some(p=>p.endsWith('owner_conversation_mark_read')),false,'Detached main chat does not acknowledge messages');
  assert.deepEqual(errors,[]);console.log('Focus conversations: paired Focus→CRM→row navigation, Back contexts and drafts, isolated scope drafts and private sends passed');
 } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;});
