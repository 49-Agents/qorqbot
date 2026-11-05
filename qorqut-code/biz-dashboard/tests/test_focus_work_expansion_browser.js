// Focus grouped-work expansion (ordinary Tasks) against the real Focus pane and synthetic DTOs. No owner Chrome, live services or agent models.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo = path.resolve(__dirname, '..');
const proposal = JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/signoffs.json'),'utf8')).data.items[0];
const agent = '11111111-1111-4111-8111-111111111111';
let desktopHits = 0, doneWrites = 0, loseResponse = false, sessionOwner = true, sessionReads = 0;
const workCalls = [], directoryReads = [];
let failRelations = false;
// Background-read controls: hold every Task/relationship read until released, fail them, or change the children served for task-c.
let holdWork = false, failWork = false, childPatch = null;
const workGates = [];
// Route-level faults: hold or fail one route only, deny one parent, and observe overlapping reads of the same branch.
let holdRoute = null, failRoute = null, denyParent = null;
const overlap = {active: new Map(), max: 0};
const taskWrites = [];
let directoryFails = false;
const submissions = [], requests = [], decisions = [], comments = [], globalEmailReads = [];
// One private thread per card binding, plus the owner's durable read cursor.
const marketingCalls = [], marketingThreads = {};
const privateThreads = new Map(), privateSends = [], privateReads = [], privateUnread = new Set(['signoff-g']);
const privateThread = id => {
  if (!privateThreads.has(id)) privateThreads.set(id, {messages: [{id: id+'-m1', sequence: 1, body: 'Private note about '+id, created_by: 'chief', created_by_name: 'Ordubashy', author_kind: 'agent', author_role: 'coordinator', is_owner: false, created_at: new Date().toISOString()}], read: 0});
  return privateThreads.get(id);
};
let desktopSurface = null;
let releaseTaskSave = null, holdTaskSave = false, markReadUnread = false;
let releaseFocusRead = null, holdFocusRead = false, focusBoardReads = 0;
let holdPrivateId=null, releasePrivateRead=null, holdPrivateSendId=null, releasePrivateSend=null, losePrivateSend=false, forbiddenPrivateId=null;
let extraPage=null,failFocusRead=false;
const privateGets=[],privateSavedKeys=new Set(),focusMoves=[],focusEdits=[],focusWriteKeys=new Set();
let moveFailure=null,editFailure=null,holdMove=false,releaseMove=null,holdEdit=false,releaseEdit=null,loseMoveResponse=false,loseEditResponse=false;
const participants=Array.from({length:6},(_,i)=>({created_by:'agent-'+i,created_by_name:'Agent '+i,author_kind:'agent'}));
const board = {revision: 2, status: 'Connecting your mailbox', updated_at: new Date().toISOString(), stale: false,
  items: [
    {type:'task', id:'task-a', business_id:'biz-a', business_slug:'software', business_name:'Qorqut Software', label:'Finish mailbox mapping <img src=x>', reason:'The remaining setup task.', needs_input:false, resource_type:'tasks', state:'open'},
    {type:'signoff', id:'signoff-b', business_id:'biz-b', business_slug:'runotak', business_name:'Runotak', label:'Review outreach direction', reason:'Ready for your decision.', needs_input:true, resource_type:'signoffs', category:proposal.category, state:'pending'},
    {type:'signoff', id:'signoff-g', business_id:null, business_slug:null, business_name:'Global', label:'Shared instructions', reason:'Global review.', resource_type:'signoffs', category:proposal.category, state:'pending'},
    {type:'task', id:'task-c', business_id:'biz-c', business_slug:'longname', business_name:'Extraordinarily Long Business Name Holdings', label:'Spring campaign group', reason:'A work group.', resource_type:'tasks', state:'open'},
    {type:'task', id:'task-d', business_id:null, business_slug:null, business_name:'Global', label:'Global work group', reason:'A Global group.', resource_type:'tasks', state:'open'},
    {type:'task', id:'task-cy', business_id:'biz-c', business_slug:'longname', business_name:'Extraordinarily Long Business Name Holdings', label:'Cycle A', reason:'Concerns B.', resource_type:'tasks', state:'open'},
  ], browser:{request_id:'input-1',state:'pending',prompt:'Enter the verification code',app:'Chrome',tab:'Fastmail · Domains'},
  activity:{command:'tab-list',phase:'finished',at:new Date().toISOString()}, desktop_control_url:null};
// Focus cards are presented by Ordubashy; each source keeps its own creator, which differs.
board.items=board.items.map(item=>({id:item.id,revision:1,label:item.label,state:item.state,reason:item.reason,archived:false,created_by:agent,
  source:{type:item.type,id:item.id,business_id:item.business_id},resolved_source:item,
  conversation:{type:item.type,id:item.id,business_id:item.business_id},source_revision:1,references:[]}));
// An independent Focus card for the same Task: separate cards are never deduplicated by the cycle guard.
board.items.push({...board.items.at(-1),id:'card-cy-twin',label:'Cycle A (second card)'});
board.items[0].display_name='Mailbox setup';
board.items[0].requested_response='Confirm the mailbox mapping.';
board.items[0].references=[{type:'reference',id:'related-a',label:'Mapping guide',summary:'Canonical related text'}];
board.items[1].display_name='Review the first full outreach draft';
board.items[3].display_name='Spring campaign Story with a deliberately long heading that cannot fit on one collapsed row and needs to remain readable in the selected conversation without changing the source title';
// A standalone Reference, an email, a Reference bound to the task above, and one nobody can attribute.
const standalone=(id,label,extra={})=>({id,revision:1,label,state:null,reason:'Look at '+label+'.',archived:false,created_by:agent,
  source:null,reference:{title:label,target:{type:'url',url:'https://example.test/'+id}},resolved_source:null,
  conversation:{type:'reference',id,business_id:null},source_revision:null,references:[],...extra});
board.items.push(
  standalone('ref-standalone','Competitor pricing page'),
  {...standalone('mail-a','Customer price question'),state:null,source:{type:'email',id:'mail-a',business_id:'biz-a'},reference:null,
    resolved_source:{type:'email',id:'mail-a',business_id:'biz-a',business_slug:'software',business_name:'Qorqut Software',label:'Customer price question'},
    conversation:{type:'email',id:'mail-a',business_id:'biz-a'}},
  standalone('ref-context','Notes on the mapping',{context:{type:'task',id:'task-a',business_id:'biz-a',business_slug:'software',business_name:'Qorqut Software'},context_business_slug:'software',context_label:'Finish mailbox mapping',
    conversation:{type:'task',id:'task-a',business_id:'biz-a'},source_revision:1}),
  standalone('ref-unknown','Loose end',{created_by:null}),
  {id:'gone-1',revision:1,archived:false,unavailable:true});
const until = async (check, what) => { for (let i=0;i<200;i++) { if (check()) return; await new Promise(r=>setTimeout(r,25)); } throw new Error('Timed out waiting for '+what); };
const server = http.createServer(async (req,res) => {
  const url = new URL(req.url,'http://localhost');
  requests.push(url.pathname);
  const reply = data => {res.setHeader('Content-Type','application/json');res.end(JSON.stringify({status:'ok',data}));};
  if (url.pathname.endsWith('/focus/done')) {
    let raw='';for await(const chunk of req) raw+=chunk;
    const body=JSON.parse(raw);submissions.push(body);
    assert.equal(body.request_id, board.browser.request_id);
    if(board.browser.state==='pending') {doneWrites++;board.browser={...board.browser,state:'done',note:body.note};board.revision++;}
    if(loseResponse){loseResponse=false;res.statusCode=503;return res.end('lost response');}
    return reply({completed:true,request_id:body.request_id});
  }
  if(url.pathname.includes('/operations/owner_conversation_')) {
    let raw='';for await(const chunk of req)raw+=chunk;
    const body=JSON.parse(raw);
    assert.ok(body.source && body.source.type && body.source.id, 'a canonical source is always sent');
    const marketingPeer=!!(body.peer_role || body.agent_id);
    if(marketingPeer)marketingCalls.push({op:url.pathname.split('/').pop(),peer_role:body.peer_role,agent_id:body.agent_id,text:body.text});
    const thread=marketingPeer?(marketingThreads[body.source.id] ||= {messages:[{id:'mkt-m1',sequence:1,body:'Marketing hello',created_by:'mkt-1',created_by_name:'Marketing',author_kind:'agent',is_owner:false,created_at:new Date().toISOString()}],read:0}):privateThread(body.source.id);
    if(url.pathname.endsWith('owner_conversation_send')) {
      privateSends.push({path:url.pathname,...body});
      const sequence=thread.messages.at(-1).sequence+1;
      if(!privateSavedKeys.has(body.idempotency_key))thread.messages.push({id:body.source.id+'-m'+sequence,sequence,body:body.text,created_by:'owner',created_by_name:'Owner',author_kind:'human',author_role:'owner',is_owner:true,created_at:new Date().toISOString()});
      privateSavedKeys.add(body.idempotency_key);
      if(holdPrivateSendId===body.source.id){holdPrivateSendId=null;await new Promise(resolve=>{releasePrivateSend=resolve;});}
      if(losePrivateSend){losePrivateSend=false;res.statusCode=503;return res.end('lost response');}
      return reply({message:thread.messages.at(-1),conversation:{id:body.source.id,revision:sequence,latest_sequence:sequence,message_count:thread.messages.length}});
    }
    if(url.pathname.endsWith('owner_conversation_mark_read')) {
      privateReads.push({path:url.pathname,...body});
      thread.read=body.sequence;if(markReadUnread)privateUnread.add(body.source.id);else privateUnread.delete(body.source.id);
      return reply({...(marketingPeer?{conversation:{agent_id:'mkt-1'}}:{}),read_state:{last_read_sequence:thread.read,unread:markReadUnread}});
    }
    privateGets.push(body);
    if(forbiddenPrivateId===body.source.id){res.statusCode=403;res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({status:'error',error:{code:'forbidden',message:'This source is no longer accessible'}}));}
    const messages=thread.messages.filter(message=>!body.before || message.sequence<body.before).slice(-20);
    const payload={conversation:{id:body.source.id,source:body.source,audience:marketingPeer?'owner_marketing':'owner_ordubashy',...(marketingPeer?{agent_id:'mkt-1'}:{}),revision:1,latest_sequence:thread.messages.at(-1).sequence,message_count:thread.messages.length},
      messages,next_before:thread.messages.some(message=>message.sequence<messages[0]?.sequence)?messages[0].sequence:null,
      participants:[{created_by:'owner',created_by_name:'Owner',author_kind:'human',is_owner:true},{created_by:'chief',created_by_name:'Ordubashy',author_kind:'agent'}],
      read_state:{last_read_sequence:thread.read,unread:privateUnread.has(body.source.id)}};
    if(holdPrivateId===body.source.id){holdPrivateId=null;await new Promise(resolve=>{releasePrivateRead=resolve;});}
    return reply(payload);
  }
  if(url.pathname.endsWith('/operations/focus_get')) {
    let raw='';for await(const chunk of req)raw+=chunk;
    const body=JSON.parse(raw);
    const project=item=>({...item,private_unread:privateUnread.has(item.id),private_message_count:privateThread(item.id).messages.length});
    // The payload is projected when the request arrives, so a held response is
    // genuinely stale by the time it lands.
    const payload=body.conversation?{item:{thread:{notes:[],participants}}}
      :body.item_id?{item:project(board.items.find(item=>item.id===body.item_id))}
      :{...board,items:(extraPage && body.offset===50?extraPage:board.items).filter(item=>(body.include_archived || !item.archived) && (body.include_deferred || !item.deferred_until || Date.parse(item.deferred_until)<=Date.now())).map(project),next_offset:extraPage && body.offset===50?null:board.next_offset};
    if(!body.conversation && !body.item_id) {
      focusBoardReads++;
      if(holdFocusRead) {holdFocusRead=false;await new Promise(resolve=>{releaseFocusRead=resolve;});}
    }
    if(failFocusRead && !body.item_id){failFocusRead=false;res.statusCode=503;res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({status:'error',error:{code:'temporarily_unavailable',message:'Old mount failed'}}));}
    return reply(payload);
  }
  if(url.pathname.endsWith('/operations/focus_move') || url.pathname.endsWith('/operations/focus_edit')){
    let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);
    const moving=url.pathname.endsWith('focus_move'),writes=moving?focusMoves:focusEdits;writes.push(body);
    const failure=moving?moveFailure:editFailure;
    if(failure){if(moving)moveFailure=null;else editFailure=null;res.statusCode=409;res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({status:'error',error:{code:failure,message:'Focus card changed; reread it'}}));}
    if(!focusWriteKeys.has(body.idempotency_key)){
      const index=board.items.findIndex(item=>item.id===body.item_id),item=board.items[index];
      assert.equal(body.expected_card_revision,item.revision);assert.equal(body.scope,'global');assert.ok(body.idempotency_key);
      if(moving){
        assert.equal(['before_id','after_id','position'].filter(key=>key in body).length,1);
        assert.ok(!body.position,'owner moves only against a visible anchor');
        board.items.splice(index,1);const anchor=board.items.findIndex(item=>item.id===(body.before_id || body.after_id));
        assert.ok(anchor>=0);board.items.splice(anchor+('after_id' in body?1:0),0,item);
      }else{assert.deepEqual(Object.keys(body.changes),['deferred_until']);item.deferred_until=body.changes.deferred_until;}
      item.revision++;board.revision++;focusWriteKeys.add(body.idempotency_key);
    }
    if(moving && holdMove){holdMove=false;await new Promise(resolve=>{releaseMove=resolve;});}
    if(!moving && holdEdit){holdEdit=false;await new Promise(resolve=>{releaseEdit=resolve;});}
    if((moving && loseMoveResponse) || (!moving && loseEditResponse)){if(moving)loseMoveResponse=false;else loseEditResponse=false;res.statusCode=503;return res.end('lost response');}
    return reply({item:board.items.find(item=>item.id===body.item_id),revision:board.revision});
  }
  if(url.pathname.endsWith('/operations/email_conversation_get')) {
    let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);
    if(body.scope==='global'){globalEmailReads.push(url.pathname);return reply({conversation:{id:'mail-g',subject:'Dinner plans'},messages:[{id:'g1',from:{address:'friend@example.test'},body:'See you at eight',has_attachments:true,received_at:new Date().toISOString()}],next_cursor:null});}
    return reply({conversation:{id:'email-a',subject:'Customer price'},messages:[{id:body.cursor?'older-mail':'new-mail',from:{address:'customer@example.test'},body:body.cursor?'Earlier email':'Latest email',received_at:new Date().toISOString()}],next_cursor:body.cursor?null:'older'});
  }
  if(url.pathname.endsWith('/operations/email_comments_list'))return reply({notes:[{id:'internal',body:'Check the price.',created_by:'owner',author_kind:'human',created_at:new Date().toISOString()}],next_before:null});
  if(url.pathname.endsWith('/operations/tasks_config_get'))return reply({config:{states:[{name:'open'},{name:'doing'},{name:'done'}]},revision:1});
  if(url.pathname.endsWith('/operations/tasks_update')){
    let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);taskWrites.push(body);
    return reply({task:{id:body.task_id,revision:(body.expected_revision||1)+1,status:body.changes.status}});
  }
  // The task's creator differs from the Focus presenter (Ordubashy) and from its assignee.
  if(url.pathname.endsWith('/operations/tasks_get'))return reply({task:{id:'task-a',revision:board.items[0].source_revision,status:board.items[0].state,tags:['marketing'],
    created_by:'dev-1',created_by_name:'Dev Builder',created_by_kind:'agent',assignee_ids:['assignee-1']}});
  // Grouped work: direct children (paginated) and `concerns` relationships, in the item's own scope.
  if(url.pathname.endsWith('/operations/tasks_list') || url.pathname.endsWith('/operations/relationships_list')) {
    let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);workCalls.push({path:url.pathname,body});
    const route=url.pathname.split('/').pop(),parentId=body.parent_id ?? body.item?.id;
    if(parentId==='task-c'){const now=(overlap.active.get(route)||0)+1;overlap.active.set(route,now);overlap.max=Math.max(overlap.max,now);res.on('close',()=>overlap.active.set(route,overlap.active.get(route)-1));}
    if(denyParent===parentId){res.statusCode=403;res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({status:'error',error:{code:'forbidden',message:'No longer permitted'}}));}
    if(holdWork || (holdRoute===route && parentId==='task-c'))await new Promise(resolve=>workGates.push(resolve));
    if(failRoute===route && parentId==='task-c'){res.statusCode=503;res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({status:'error',error:{code:'temporarily_unavailable',message:'Route failed'}}));}
    if(failWork){res.statusCode=503;res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({status:'error',error:{code:'temporarily_unavailable',message:'Work read failed'}}));}
    if(failRelations && url.pathname.endsWith('relationships_list')){res.statusCode=500;res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({status:'error',error:{code:'internal',message:'Relations unavailable'}}));}
    const task=(id,title,status='open')=>({id,title,status,revision:3,archived:false,closed:['done','cancelled'].includes(status)});
    if(url.pathname.endsWith('tasks_list')) {
      if(url.pathname.includes('/otherbiz/'))return reply({tasks:[task('other-child','Other business child')],next_cursor:null});
      if(body.parent_id==='task-b')return reply({tasks:[task('task-grand','Grandchild')],next_cursor:null});
      if(body.parent_id==='task-c' && childPatch && !body.cursor)return reply({tasks:childPatch.map(([id,title,status])=>task(id,title,status)),next_cursor:'page-2'});
      if(body.parent_id==='task-c')return reply(body.cursor?{tasks:[task('task-late','Late child')],next_cursor:null}:{tasks:[task('task-a','Child one'),task('task-b','Child two','done')],next_cursor:'page-2'});
      if(body.parent_id==='task-d')return reply({tasks:[task('global-child','Global child')],next_cursor:null});
      return reply({tasks:[],next_cursor:null});
    }
    const self=body.item.id,edge=(source,target,id)=>({id,role:'concerns',source,target,source_item:source.label?source:{...source,label:'Item '+source.id},target_item:target.label?target:{...target,label:'Item '+target.id}});
    if(self==='task-c')return reply(body.cursor?{items:[],next_cursor:null}:{items:[
      edge({type:'task',id:'task-c',label:'Spring campaign group'},{type:'task',id:'task-a',label:'Child one'},'e1'),
      edge({type:'task',id:'task-rel',label:'Related task',status:'open'},{type:'task',id:'task-c',label:'Spring campaign group'},'e2'),
      edge({type:'task',id:'task-c',label:'Spring campaign group'},{type:'table',id:'table-1',label:'Results table',resource_type:'data_tables'},'e3'),
      edge({type:'task',id:'task-c',label:'Spring campaign group'},{type:'material',id:'doc-1',label:'Research notes',resource_type:'documents'},'e4'),
      edge({type:'task',id:'task-c',label:'Spring campaign group'},{type:'task',id:'task-c',label:'Spring campaign group'},'e5')],next_cursor:null});
    // A real two-Task concerns cycle: A concerns B and B concerns A.
    if(self==='task-cy')return reply({items:[edge({type:'task',id:'task-cy',label:'Cycle A'},{type:'task',id:'task-cb',label:'Cycle B',status:'open'},'c1')],next_cursor:null});
    if(self==='task-cb')return reply({items:[edge({type:'task',id:'task-cb',label:'Cycle B'},{type:'task',id:'task-cy',label:'Cycle A',status:'open'},'c2')],next_cursor:null});
    return reply({items:[],next_cursor:null});
  }
  // The business identity directory: active members only, so a departed creator is absent.
  if(url.pathname.endsWith('/operations/assignees_list')) {
    directoryReads.push(url.pathname);
    if(directoryFails){res.statusCode=500;res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({status:'error',error:{code:'internal',message:'Directory unavailable'}}));}
    return reply({assignees:[{id:'owner',kind:'human',display_name:'Owner One',name:null,is_owner:true},
      {id:'biz-planner',kind:'agent',display_name:'Campaign Planner',name:'planner',is_owner:false}]});
  }
  if(url.pathname.endsWith('/operations/focus_reply')) {
    let raw='';for await(const chunk of req)raw+=chunk;comments.push(JSON.parse(raw));return reply({});
  }
  if(url.pathname.endsWith('/focus')) return reply(board);
  if(url.pathname.endsWith('/inbox')) return reply({items:[],subscriptions:[]});
  if(url.pathname==='/api/session'){sessionReads++;return reply({principal_id:sessionOwner?'owner':'teammate',principal_kind:'human',display_name:'Owner One',is_owner:sessionOwner});}
  if(url.pathname.endsWith('/operations/signoffs_notes_list'))return reply({notes:[],next_before:null,rejection_requires_message:true});
  if(url.pathname.includes('/signoffs/') && req.method==='POST') {
    let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);decisions.push(body);
    const id=url.pathname.split('/').at(-2);
    return reply({signoff:{...proposal,id,review_business_id:url.pathname.includes('/root/')?null:'biz-b',state:'rejected',
      status_revision:proposal.status_revision+1,decision:{kind:'reject',by:'owner',at:new Date().toISOString(),message_id:'posted-note'}}});
  }
  if(url.pathname.includes('/signoffs/')) {
    const id=url.pathname.split('/').at(-1);
    return reply({signoff:{...proposal,id,review_business_id:url.pathname.includes('/root/')?null:'biz-b',rejection_requires_message:true,
      ...(id==='signoff-g'?{proposer_agent_id:agent,proposer_principal_id:agent,proposer_display_name:'Ordubashy'}:{})}});
  }
  if(url.pathname==='/api/root/agent-pins')return reply({initialized:true,pinned_agents:[]});
  if(url.pathname==='/api/root/agents')return reply({agents:[
    {agent_id:agent,name:'chief',display_name:'Ordubashy',role:{id:'ordubashy'},scope:{kind:'ecosystem',allowed_business_ids:[],global_capabilities:[]}},
    {agent_id:'dev-1',name:'builder',display_name:'Dev Builder',role:{id:'dev'}},
    {agent_id:'other',name:'not-chief',display_name:'Other agent',role:{id:'learning-applier'},scope:{kind:'ecosystem',allowed_business_ids:[],global_capabilities:[]}}]});
  // Ordubashy's Browser entry reads its configured surfaces; the Desktop is unconfigured until a test adds it.
  if(url.pathname===`/api/root/agents/${agent}/browser-surfaces`){res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({agent_id:agent,configured:true,browsers:[],desktop:desktopSurface}));}
  if(url.pathname==='/api/root/terminals'){res.setHeader('Content-Type','application/json');return res.end('[]');}
  if(url.pathname==='/desktop/'){desktopHits++;res.setHeader('Content-Type','text/html');return res.end('<body style="background:#1c2630;color:white;font:20px sans-serif;padding:40px">Test desktop — no remote connection</body>');}
  if(url.pathname==='/') {
    res.setHeader('Content-Type','text/html');
    const styles = fs.readFileSync(path.join(repo,'index.html'),'utf8').match(/<link rel="stylesheet"[^>]+>/g).join('\n');
    return res.end(`<!doctype html><html><head><meta charset="utf-8">${styles}</head><body>
      <div id="toasts"></div><button id="wsbtn" hidden></button><div id="host" style="height:100vh;display:flex;min-width:0"></div>
      <script src="/js/core.js"></script><script src="/js/san-juan-time.js"></script><script src="/js/task-state.js"></script><script src="/js/tags.js"></script><script src="/js/work-groups.js"></script><script src="/js/tasks.js"></script><script src="/js/agents.js"></script>
      <script>
      state.scope='global';state.biz=null;state.globalTab='agents';state.bizs=[];
      window.workRelatedChips=()=>{};
      window.globalIcon=()=>''; window.broadcastButton=()=>'';window.wireBroadcastButton=()=>{};window.agentNavPaint=()=>{};window.agentNavRefresh=()=>{};
      window.mountTerminal=()=>{};window.mountGlobalChat=()=>({dispose(){}});
      window.agentViewDispose=()=>{};window.mountAgentView=()=>{};
      window.workingMemoryMount=el=>el.textContent='Working memory form';
      window.sgAgentAttentionPaint=()=>{};
      window.tFetchTask=async(biz,id)=>({business:{slug:biz},id,title:'Task detail'});
      window.tDetail=(task,{host}={})=>{window.openedTask=task;host.textContent='Existing task editor'};
      </script>
      <script src="/js/safe-links.js"></script><script src="/js/chat-view.js"></script><script src="/js/conversation.js"></script><script src="/js/signoffs.js"></script><script src="/js/global-calendar.js"></script><script src="/js/agentpane.js"></script><script src="/js/agent-focus.js"></script><script src="/js/agent-browser.js"></script><script src="/js/global-agents.js"></script>
      <script>renderGlobalAgents(document.querySelector('#host'));</script></body></html>`);
  }
  const file=path.join(repo,url.pathname);
  if(!file.startsWith(repo+path.sep)||!fs.existsSync(file)){res.statusCode=404;return res.end();}
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/css');res.end(fs.readFileSync(file));
});

(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  try {
    const page=await browser.newPage({viewport:{width:1300,height:900},timezoneId:'America/New_York',hasTouch:true}),errors=[];
    page.setDefaultTimeout(10000);
    page.on('pageerror',e=>{errors.push(e.message);console.error(e.message)});
    await page.route('**/*',route=>route.request().url().startsWith(origin)?route.continue():route.abort());
    await page.goto(origin);
    await page.getByRole('tab',{name:'Focus',exact:true}).click();
    const group=page.locator('[data-card-id="task-c"]');
    await group.waitFor();
    // Clicking a Task opens its conversation and only its immediate related work.
    assert.equal(await page.locator('.agfocus-work-toggle').count(),0);
    await group.locator('.ttl').click();
    const child=page.locator('[data-card-id="task-c/task/task-a"]');
    await child.waitFor();
    assert.equal(await group.getAttribute('aria-expanded'),'true','one click opens both work and chat');
    assert.equal(workCalls.some(call=>call.body.parent_id==='task-a'),false,'children are not recursively expanded');
    // Children come from the scope's paginated list; relationships from the authorized concerns read.
    const first=workCalls.find(call=>call.path.endsWith('tasks_list'));
    assert.equal(first.path,'/api/biz/longname/operations/tasks_list');
    assert.equal(first.body.parent_id,'task-c');
    const relation=workCalls.find(call=>call.path.endsWith('relationships_list'));
    assert.equal(relation.path,'/api/biz/longname/operations/relationships_list');
    assert.deepEqual([relation.body.item,relation.body.role,relation.body.direction],[{type:'task',id:'task-c'},'concerns','both']);
    // Hierarchy and related work: duplicate child/self removed, related Task kept, others become reference chips.
    assert.deepEqual(await page.locator('.agfocus-work-child').evaluateAll(rows=>rows.map(row=>row.dataset.cardId)),['task-c/task/task-a','task-c/task/task-b','task-c/task/task-rel']);
    const refs=group.locator('.agfocus-work-extras');
    assert.equal(await refs.locator('.agfocus-reference-chip[data-kind=table]').count(),1);
    assert.equal(await refs.locator('.agfocus-reference-chip[data-kind=file]').count(),1);
    assert.equal(await child.locator('.agfocus-scope').isVisible(),false,'same-business child omits the repeated business badge');
    assert.ok((await child.boundingBox()).x>(await group.boundingBox()).x);
    // Derived rows are not Focus cards: no grip, no snooze, never in a Focus move.
    assert.equal(await child.getAttribute('data-move-disabled'),'true','derived rows are not independently draggable');
    {
      const row=await child.locator('.agfocus-row').boundingBox(),x=row.x+row.width/2,y=row.y+row.height/2;
      await page.mouse.move(x,y);await page.mouse.down();await page.waitForTimeout(160);await page.mouse.move(x,y+90,{steps:6});await page.mouse.up();
      await child.focus();await page.keyboard.press('Alt+ArrowDown');
      assert.equal(await child.evaluate(el=>el.querySelector('.agfocus-row').classList.contains('agfocus-dragging')),false);
      await page.mouse.move(0,0);
    }
    assert.equal(await child.locator('[data-focus-snooze]').isVisible(),false);
    assert.equal(await child.evaluate(el=>el._item._workKind),'child');
    assert.equal(await page.locator('[data-card-id="task-c/task/task-rel"]').evaluate(el=>el._item._workKind),'related');
    // Story icon collapses grouped subtasks without closing the conversation or writing state.
    await page.evaluate(()=>{const item=_agpad.focus.items.find(i=>i.id==='task-c');item.resolved_source.task_type='story';agfocusPaint(_agpad);});
    const storyToggle=group.locator('.task-subtasks-toggle');
    const writesBefore=taskWrites.length;
    await storyToggle.click();
    assert.equal(await child.isVisible(),false);
    assert.equal(await storyToggle.getAttribute('aria-expanded'),'false');
    assert.equal(await group.getAttribute('aria-expanded'),'true','collapse leaves conversation open');
    assert.ok(!(await page.evaluate(()=>JSON.parse(localStorage.getItem('agfocus:expanded-work:v1')))).includes('task-c'));
    await page.evaluate(()=>agfocusPaint(_agpad));
    assert.equal(await child.isVisible(),false,'repainting does not expand the story');
    await storyToggle.focus();await page.keyboard.press('Enter');
    await child.waitFor();
    assert.equal(await storyToggle.getAttribute('aria-expanded'),'true');
    assert.equal(taskWrites.length,writesBefore,'toggle never changes Task state');
    await page.evaluate(()=>{const item=_agpad.focus.items.find(i=>i.id==='task-c');item.resolved_source.task_type='task';agfocusPaint(_agpad);});
    if(process.env.STORY_ICON_SMOKE){assert.deepEqual(errors,[]);console.log('Story icon collapse/expand, conversation preservation and state-write isolation passed');return;}
    // More work: the next page of children, same scope.
    await group.getByRole('button',{name:'More related work'}).click();
    await page.locator('[data-card-id="task-c/task/task-late"]').waitFor();
    assert.equal(workCalls.filter(call=>call.path.endsWith('tasks_list')).at(-1).body.cursor,'page-2');
    assert.equal(await group.getByRole('button',{name:'More related work'}).count(),0);
    // A child's state is independent: it writes only that task and never changes the parent or siblings.
    const ball=child.locator('.agfocus-task-state');
    await ball.click();
    await page.waitForFunction(()=>document.querySelector('[data-card-id="task-c/task/task-a"] .agfocus-task-state').getAttribute('aria-label')!=='Open');
    assert.deepEqual(taskWrites.map(write=>write.task_id),['task-a'],'only the child is written');
    assert.equal(await group.locator('.agfocus-task-state').getAttribute('aria-label'),'Open','parent state is untouched');
    assert.equal(await page.locator('[data-card-id="task-c/task/task-b"] .agfocus-task-state').getAttribute('aria-label'),'Done');
    // The existing private conversation dock works for a child and keeps its draft across collapse and reopen.
    await child.locator('.ttl').click();
    const dock=page.locator('.agfocus-dock');await dock.waitFor();
    const thread=page.locator('[data-focus-thread-id="task-c/task/task-a"]');
    const draft=thread.locator('.conversation-inline-compose textarea');
    await draft.fill('Unsent note about the child');
    await page.locator('[data-focus-thread-close]').click();
    await group.locator('.ttl').click();
    assert.equal(await child.isVisible(),true,'opening the parent retains its direct children');
    await child.waitFor();
    await child.locator('.ttl').click();
    assert.equal(await thread.locator('.conversation-inline-compose textarea').inputValue(),'Unsent note about the child','the draft survives collapse and reopen');
    await page.locator('[data-focus-thread-close]').click();
    assert.equal(focusMoves.length,0,'derived rows never submit a Focus move');
    // Expansion persists under its own key and restores after a reload; old Story state is never read.
    assert.deepEqual(await page.evaluate(()=>JSON.parse(localStorage.getItem('agfocus:expanded-work:v1'))),['task-c','task-c/task/task-a']);
    assert.equal(await page.evaluate(()=>localStorage.getItem('agfocus:expanded-stories:v1')),null);
    await page.reload();await page.getByRole('tab',{name:'Focus',exact:true}).click();
    await page.locator('[data-card-id="task-c/task/task-a"]').waitFor();
    // A failed relationship read collapses with an error and can be retried.
    await page.evaluate(()=>localStorage.removeItem('agfocus:expanded-work:v1'));
    await page.reload();await page.getByRole('tab',{name:'Focus',exact:true}).click();
    failRelations=true;
    await page.locator('[data-card-id="task-c"] .ttl').click();
    await page.getByText(/Could not load related work/).waitFor();
    assert.equal(await page.locator('[data-card-id="task-c/task/task-a"]').isVisible(),false);
    failRelations=false;
    await page.locator('[data-focus-thread-close]').click();
    await page.locator('[data-card-id="task-c"]').locator('.ttl').click();
    await page.locator('[data-card-id="task-c/task/task-a"]').waitFor();
    // A real two-Task concerns cycle (A -> B -> A): B shows under A, but A is refused beneath B before it renders.
    const cycleReads=id=>workCalls.filter(call=>call.path.endsWith('relationships_list') && call.body.item.id===id).length;
    const cycleCard=page.locator('[data-card-id="task-cy"]');
    await cycleCard.locator('.ttl').click();
    const cycleB=page.locator('[data-card-id="task-cy/task/task-cb"]');await cycleB.waitFor();
    await cycleB.locator('.ttl').click();
    await until(()=>cycleReads('task-cb')>=1,'the derived Task relationship read');
    await page.waitForTimeout(150);
    const cycleIds=()=>page.locator('[data-card-id^="task-cy"],[data-card-id="card-cy-twin"]').evaluateAll(nodes=>nodes.map(node=>node.dataset.cardId));
    assert.deepEqual(await cycleIds(),['task-cy','task-cy/task/task-cb','card-cy-twin'],'A is not rendered again under B; the independent second card remains');
    const readsBefore=[cycleReads('task-cy'),cycleReads('task-cb')],storedBefore=await page.evaluate(()=>localStorage.getItem('agfocus:expanded-work:v1'));
    // Refresh and reopen: no path grows and no request repeats without the owner expanding something.
    await page.reload();await page.getByRole('tab',{name:'Focus',exact:true}).click();
    await page.locator('[data-card-id="task-cy"]').waitFor();await page.waitForTimeout(300);
    assert.deepEqual(await cycleIds(),['task-cy','task-cy/task/task-cb','card-cy-twin'],'reopened cycle keeps the same finite paths');
    assert.ok(cycleReads('task-cy')<=readsBefore[0]+1 && cycleReads('task-cb')<=readsBefore[1]+1,'reopen reads each Task at most once more');
    const readsOpened=[cycleReads('task-cy'),cycleReads('task-cb')];
    await page.getByRole('button',{name:'Refresh Focus'}).click();await page.waitForTimeout(300);
    assert.deepEqual(await cycleIds(),['task-cy','task-cy/task/task-cb','card-cy-twin']);
    assert.ok(cycleReads('task-cy')<=readsOpened[0]+1 && cycleReads('task-cb')<=readsOpened[1]+1,'refresh does not grow requests');
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('agfocus:expanded-work:v1')).filter(id=>id.includes('task-cy')).every(id=>id.split('/').length<=3)),true,'stored paths stay finite');
    assert.equal(workCalls.every(call=>call.body.item?.id!=='task-cy' || !call.path.includes('undefined')),true);
    // A Global group reads through the root route in Global scope.
    await page.locator('[data-card-id="task-d"]').locator('.ttl').click();
    const globalChild=page.locator('[data-card-id="task-d/task/global-child"]');await globalChild.waitFor();
    const global=workCalls.find(call=>call.path==='/api/root/operations/tasks_list');
    assert.equal(global.body.scope,undefined,'Task reads infer Global from the root route and send no scope');assert.equal(global.body.parent_id,'task-d');
    // UNINTEGRATED section G contract: the frozen host root route has no relationships_list yet, so this mock is the requested
    // `scope`-carrying request, not evidence that the real Global route answers it.
    const globalRelation=workCalls.find(call=>call.path==='/api/root/operations/relationships_list' && call.body.item.id==='task-d');
    assert.equal(globalRelation.body.scope,'global');assert.deepEqual([globalRelation.body.role,globalRelation.body.direction],['concerns','both']);
    assert.equal(await globalChild.evaluate(el=>el._item.conversation.business_id),null);
    assert.equal(workCalls.some(call=>/stor(y|ies)/i.test(call.path+JSON.stringify(call.body))),false,'no Story request');
    // Thread typography: owner and agent text share one size in a Focus thread; native Chat markup outside it keeps its own.
    {
      const thread=privateThread('task-a');
      thread.messages[0].body='Agent paragraph one\n\nAgent paragraph two';
      thread.messages.push({id:'task-a-m2',sequence:2,body:'Owner paragraph in the thread',created_by:'owner',created_by_name:'Owner',author_kind:'human',author_role:'owner',is_owner:true,created_at:new Date().toISOString()});
      await page.locator('[data-card-id="task-a"] .ttl').click();
      const region=page.locator('[data-focus-thread-id="task-a"]');
      await region.locator('.gchat-user .gchat-body').waitFor();await region.locator('.gchat-agent .gchat-body').waitFor();
      const sizes=await page.evaluate(()=>{
        const size=el=>parseFloat(getComputedStyle(el).fontSize);
        const region=document.querySelector('[data-focus-thread-id="task-a"]');
        const owner=region.querySelector('.gchat-user .gchat-body'),agent=region.querySelector('.gchat-agent .gchat-body'),agentText=agent.querySelector('p');
        const native=document.createElement('div');native.className='gchat-msg gchat-user';native.innerHTML='<div class="gchat-body">Native</div>';document.body.append(native);
        const result={owner:size(owner),agent:size(agentText),agentBody:size(agent),native:size(native.firstChild)};native.remove();return result;
      });
      assert.equal(sizes.owner,12,'owner text matches the agent paragraph size');
      assert.equal(sizes.agent,12,'agent paragraphs keep their size');assert.equal(sizes.agentBody,15.75,'agent base body text is unchanged');
      assert.equal(sizes.native,15.75,'native Chat keeps its own size');
      await page.locator('[data-focus-thread-close]').click();
    }
    // ---- Background refresh keeps the mounted snapshot, the open child thread and its draft. ----
    const settled=()=>page.waitForFunction(()=>!_agpad.focusPending && !_agpad.focusRefreshAgain);
    const heldReads=async count=>{await until(()=>workGates.length>=count,'held work reads');};
    const releaseReads=()=>{holdWork=false;workGates.splice(0).forEach(resolve=>resolve());};
    const refreshButton=()=>page.getByRole('button',{name:'Refresh Focus'}).evaluate(button=>button.click());
    const listCalls=()=>workCalls.filter(call=>call.path.endsWith('tasks_list') && call.body.parent_id==='task-c');
    const rowIds=()=>page.locator('[data-card-id^="task-c/task/"]').evaluateAll(rows=>rows.map(row=>row.dataset.cardId));
    // First load of an unexpanded card shows Loading, then its content.
    {
      holdWork=true;
      await page.locator('[data-card-id="card-cy-twin"] .ttl').click();
      await heldReads(2);
      await page.locator('.agfocus-work-extras .muted',{hasText:'Loading related work…'}).waitFor();
      releaseReads();
      await page.locator('[data-card-id="card-cy-twin/task/task-cb"]').waitFor();
      assert.equal(await page.locator('.agfocus-work-extras .muted',{hasText:'Loading related work…'}).count(),0);
      await page.locator('[data-focus-thread-close]').click();
    }
    // Load task-c's second page and a nested expansion, then write in a child thread.
    await page.locator('[data-card-id="task-c"]').getByRole('button',{name:'More related work'}).click();
    await page.locator('[data-card-id="task-c/task/task-late"]').waitFor();
    await page.locator('[data-card-id="task-c/task/task-b"] .ttl').click();
    await page.locator('[data-card-id="task-c/task/task-b/task/task-grand"]').waitFor();
    await page.locator('[data-focus-thread-close]').click();
    const childRow=page.locator('[data-card-id="task-c/task/task-a"]');
    await childRow.locator('.ttl').click();
    const childThread=page.locator('[data-focus-thread-id="task-c/task/task-a"]');
    const composer=childThread.locator('.conversation-inline-compose textarea');
    await composer.waitFor();
    await composer.focus();await composer.fill('Half-written reply to the child');
    await composer.evaluate(el=>el.setSelectionRange(5,11));
    await page.evaluate(()=>{
      const spacer=document.createElement('div');spacer.style.height='3000px';spacer.id='scroll-spacer';document.querySelector('.agfocus').append(spacer);
      const scroller=document.querySelector('#agpad-body');scroller.scrollTop=200;
      window.__keep={row:document.querySelector('[data-card-id="task-c/task/task-a"]'),thread:document.querySelector('[data-focus-thread-id="task-c/task/task-a"]'),
        composer:document.querySelector('[data-focus-thread-id="task-c/task/task-a"] textarea'),scroller,top:scroller.scrollTop,late:document.querySelector('[data-card-id="task-c/task/task-late"]')};
    });
    const keptState=()=>page.evaluate(()=>{
      const k=window.__keep,composer=document.querySelector('[data-focus-thread-id="task-c/task/task-a"] textarea');
      return {sameRow:document.querySelector('[data-card-id="task-c/task/task-a"]')===k.row,sameThread:document.querySelector('[data-focus-thread-id="task-c/task/task-a"]')===k.thread,
        sameComposer:composer===k.composer,value:composer?.value,selection:composer&&[composer.selectionStart,composer.selectionEnd],focused:document.activeElement===composer,
        top:k.scroller.scrollTop,expected:k.top,late:!!document.querySelector('[data-card-id="task-c/task/task-late"]'),sameLate:document.querySelector('[data-card-id="task-c/task/task-late"]')===k.late,
        grand:!!document.querySelector('[data-card-id="task-c/task/task-b/task/task-grand"]'),loading:/Loading related work/.test(document.querySelector('[data-card-id="task-c"]').textContent),
        selected:document.querySelector('.agfocus-dock')?.hidden===false,title:document.querySelector('[data-focus-thread-title]')?.textContent};
    });
    const expectKept=(state,note)=>{
      assert.deepEqual([state.sameRow,state.sameThread,state.sameComposer,state.focused,state.selected],[true,true,true,true,true],note+': row, thread and composer DOM stay');
      assert.equal(state.value,'Half-written reply to the child',note+': draft');assert.deepEqual(state.selection,[5,11],note+': caret selection');
      assert.equal(state.top,state.expected,note+': scroll');assert.deepEqual([state.late,state.sameLate,state.grand,state.loading],[true,true,true,false],note+': loaded page, nested row, no Loading');
    };
    expectKept(await keptState(),'before refresh');
    // Hold both reads of an ordinary refresh: nothing is cleared while the response is outstanding.
    {
      const mark=listCalls().length;
      await settled();holdWork=true;await refreshButton();await heldReads(2);
      expectKept(await keptState(),'while the refresh read is held');
      await page.waitForTimeout(150);
      expectKept(await keptState(),'after a polling tick during the wait');
      releaseReads();
      await until(()=>listCalls().length>=mark+2,'the second child page reread');
      await page.waitForTimeout(250);
      const calls=listCalls().slice(mark);
      assert.equal(calls[0].body.cursor,undefined,'refresh starts from the first page, not an old cursor');assert.equal(calls[1].body.cursor,'page-2','newly returned cursor covers the previously loaded second page');
      expectKept(await keptState(),'after the unchanged response');
      assert.deepEqual(await rowIds(),['task-c/task/task-a','task-c/task/task-b','task-c/task/task-b/task/task-grand','task-c/task/task-late','task-c/task/task-rel']);
    }
    // A transient failure keeps the loaded snapshot, the thread and the draft; the next refresh recovers.
    {
      failWork=true;await refreshButton();
      await page.getByText(/Could not refresh related work/).waitFor();
      expectKept(await keptState(),'after a failed refresh');
      assert.equal(await page.locator('.agfocus-error').isVisible(),true);
      failWork=false;await refreshButton();
      await page.waitForFunction(()=>document.querySelector('.agfocus-error').hidden);
      expectKept(await keptState(),'after the retry');
    }
    // One route fails while its sibling is held: the branch stays single-flight until both settle, nothing is half-published,
    // and a repeated refresh never overlaps the surviving read. Recovery follows once the fault clears.
    {
      overlap.max=0;const relReads=()=>workCalls.filter(call=>call.path.endsWith('relationships_list') && call.body.item.id==='task-c').length;
      await settled();holdRoute='relationships_list';failRoute='tasks_list';
      await refreshButton();await heldReads(1);
      const before=relReads();
      await refreshButton();await page.waitForTimeout(150);
      assert.equal(relReads(),before,'no second read of the branch starts while the sibling read is outstanding');
      assert.equal(await page.getByText(/Could not refresh related work/).count(),0,'the failure is reported only after both reads settle');
      expectKept(await keptState(),'while the sibling read is held after a partial failure');
      holdRoute=null;failRoute=null;
      workGates.splice(0).forEach(resolve=>resolve());
      await refreshButton();
      await page.waitForFunction(()=>document.querySelector('.agfocus-error').hidden);
      assert.equal(overlap.max,1,'reads of one route never overlapped');
      expectKept(await keptState(),'after recovery from a partial failure');
    }
    // Changed data appears after the response without recreating the open thread; a late child page is reread.
    {
      childPatch=[['task-a','Child one'],['task-new','Brand new child']];
      await refreshButton();
      await page.locator('[data-card-id="task-c/task/task-new"]').waitFor();
      assert.equal(await page.locator('.agfocus-items [data-card-id="task-c/task/task-b"]').count(),0,'a removed child is gone after the response');
      const state=await keptState();
      assert.deepEqual([state.sameRow,state.sameThread,state.sameComposer,state.value],[true,true,true,'Half-written reply to the child']);
    }
    // A stale response after the source binding changed is dropped and never shown under the new source.
    {
      await settled();holdWork=true;await refreshButton();await heldReads(2);
      const card=board.items.find(item=>item.id==='task-c');card.resolved_source={...card.resolved_source,business_slug:'otherbiz'};
      await refreshButton();
      releaseReads();
      await page.locator('[data-card-id="task-c/task/other-child"]').waitFor();
      assert.equal(await page.locator('.agfocus-items [data-card-id="task-c/task/task-new"]').count(),0,'the old source snapshot is not carried over');
      assert.ok(workCalls.some(call=>call.path==='/api/biz/otherbiz/operations/tasks_list'));
      assert.equal(await page.locator('.agfocus-dock').isVisible(),false,'the thread of a child that no longer exists closes');
      card.resolved_source={...card.resolved_source,business_slug:'longname'};
    }
    // A response that lands after the pane is unmounted changes nothing and raises no error.
    {
      await settled();holdWork=true;await refreshButton();await heldReads(1);
      const other=await page.evaluate(()=>[...document.querySelectorAll('[data-v]')].map(tab=>tab.dataset.v).find(view=>view!=='focus'));
      await page.locator(`[data-v="${other}"]`).first().click();
      await page.waitForFunction(()=>!document.querySelector('.agfocus'));
      releaseReads();await page.waitForTimeout(250);
      await page.getByRole('tab',{name:'Focus',exact:true}).click();
      await page.locator('[data-card-id="task-c/task/other-child"],[data-card-id="task-c/task/task-a"]').first().waitFor();
    }
    // Lost access is terminal: unlike a 503, it removes the affected branch and its open child thread, and only that branch.
    {
      await page.locator('[data-card-id="task-c/task/task-a"] .ttl').click();
      await page.locator('.agfocus-dock').waitFor();
      denyParent='task-c';await refreshButton();
      await page.waitForFunction(()=>!document.querySelector('.agfocus-items [data-card-id^="task-c/task/"]'));
      assert.equal(await page.locator('.agfocus-dock').isVisible(),false,'the inaccessible child thread closes');
      await page.getByText(/Could not load related work/).waitFor();
      assert.equal(await page.locator('.agfocus-items [data-card-id="card-cy-twin/task/task-cb"]').count(),1,'an unrelated branch stays');
      denyParent=null;
    }
    await page.setViewportSize({width:390,height:800});
    // The retained shell's own agent-navigation toggle collapses the list; then the Focus rows have the width of the pane.
    await page.getByRole('button',{name:'Hide agent navigation'}).click();
    await page.waitForFunction(()=>document.querySelector('.agsplit')?.classList.contains('nav-off'));
    // Disclosure, not acceptance: below 800px the retained shell splits the viewport between Chat and this pane, so the pane is
    // only about half the viewport. The pane acceptance below isolates it at the full 390px width it would have if the shell yielded it.
    const shellPane=await page.evaluate(()=>document.querySelector('.agfocus').getBoundingClientRect().width);
    assert.ok(shellPane<260,'the retained shell keeps the pane narrower than the isolated fixture ('+shellPane+'px)');
    await page.addStyleTag({content:'.split.global-agents.agsplit.nav-off{grid-template-columns:0 minmax(0,1fr)!important}.split.global-agents.agsplit .global-agent-session{display:none}'});
    await page.waitForFunction(()=>document.querySelector('.agfocus').getBoundingClientRect().width>=300);
    const bounds=await page.evaluate(()=>{
      const r=selector=>{const el=document.querySelector(selector),b=el?.getBoundingClientRect();return b&&{left:b.left,right:b.right,width:b.width,height:b.height};};
      return {vw:innerWidth,pane:r('.agfocus'),title:r('[data-card-id="task-c"] .ttl'),
        state:r('[data-card-id="task-c"] .agfocus-task-state'),snooze:r('[data-card-id="task-c"] [data-focus-snooze]')};
    });
    for(const [name,box] of Object.entries(bounds)){
      if(name==='vw'||!box)continue;
      assert.ok(box.left>=0 && box.right<=bounds.vw+0.5,`${name} stays inside the 390px viewport: ${JSON.stringify(box)}`);
    }
    assert.ok(bounds.title.width>=100,`the Task title is readable at 390px (${bounds.title.width}px wide)`);
    assert.ok(bounds.state.width>=20,'the state control keeps a usable target');
    assert.ok(bounds.pane.width>=300,`the Focus pane uses the narrow viewport (${bounds.pane.width}px)`);
    if(process.env.EVIDENCE_DIR){fs.mkdirSync(process.env.EVIDENCE_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.EVIDENCE_DIR,'focus-work-390.png')});await page.setViewportSize({width:1300,height:900});await page.screenshot({path:path.join(process.env.EVIDENCE_DIR,'focus-work-1300.png')});}
    assert.deepEqual(errors,[]);
    console.log('Focus work expansion: Task children and concerns, pagination, independent state, derived rows not curated, private draft, own storage key, retry and Global scope passed.');
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
