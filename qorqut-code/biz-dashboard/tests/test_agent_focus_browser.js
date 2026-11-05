// Isolated browser acceptance: no owner Chrome, live services or agent models.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo = path.resolve(__dirname, '..');
const proposal = JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/signoffs.json'),'utf8')).data.items[0];
const agent = '11111111-1111-4111-8111-111111111111';
const attentionPinned=process.argv.includes('--attention-pinned');
let desktopHits = 0, doneWrites = 0, loseResponse = false, sessionOwner = true, sessionReads = 0;
const storyReads = [], storyPaths = [], directoryReads = [];
let directoryFails = false;
const submissions = [], requests = [], decisions = [], comments = [], globalEmailReads = [], taskWrites=[];
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
    {type:'story', id:'story-c', business_id:'biz-c', business_slug:'longname', business_name:'Extraordinarily Long Business Name Holdings', label:'Story for the spring campaign', reason:'A Story that needs a direction.', resource_type:'stories', state:'planned'},
    {type:'story', id:'story-d', business_id:'biz-c', business_slug:'longname', business_name:'Extraordinarily Long Business Name Holdings', label:'Story whose creator left the business', reason:'A retired creator.', resource_type:'stories', state:'planned'},
    {type:'story', id:'story-o', business_id:'biz-a', business_slug:'software', business_name:'Qorqut Software', label:'Story the owner wrote', reason:'An owner-created Story.', resource_type:'stories', state:'planned'},
  ], browser:{request_id:'input-1',state:'pending',prompt:'Enter the verification code',app:'Chrome',tab:'Fastmail · Domains'},
  activity:{command:'tab-list',phase:'finished',at:new Date().toISOString()}, desktop_control_url:null};
// Focus cards are presented by Ordubashy; each source keeps its own creator, which differs.
board.items=board.items.map(item=>({id:item.id,revision:1,label:item.label,state:item.state,reason:item.reason,archived:false,created_by:agent,
  source:{type:item.type,id:item.id,business_id:item.business_id},resolved_source:item,
  conversation:{type:item.type,id:item.id,business_id:item.business_id},source_revision:1,references:[]}));
if (attentionPinned) {
  board.items[0].task={id:'task-a',title:'Mailbox setup',status:'open',revision:1,worker_ids:['owner',agent,'dev-1'],user_needed:true,workers:[{id:'owner',kind:'human',display_name:'Owner',is_owner:true},{id:agent,kind:'agent',display_name:'Ordubashy',role_id:'ordubashy'},{id:'dev-1',kind:'agent',display_name:'Dev Builder',role_id:'global-worker'}]};
  const story=board.items[3]; story.source.type='task'; story.resolved_source.type='task';story.resolved_source.task_type='story';story.conversation.type='task';story.state='open';story.task={id:'story-c',title:'Story',task_type:'story',status:'open',revision:1,worker_ids:[],workers:[]};
}
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
    if(url.pathname.endsWith('owner_conversation_peers'))return reply({peers:[{channel:'private',agent_id:'chief',name:'Ordubashy',role:'ordubashy',unread:privateUnread.has(body.source.id)},{channel:'worker',agent_id:'mkt-1',name:'Marketing',role:'global-worker',unread:false}]});
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
    globalEmailReads.push(url.pathname);
    let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);
    if(body.scope==='global'){globalEmailReads.push(url.pathname);return reply({conversation:{id:'mail-g',subject:'Dinner plans'},messages:[{id:'g1',from:{address:'friend@example.test'},body:'See you at eight',has_attachments:true,received_at:new Date().toISOString()}],next_cursor:null});}
    return reply({conversation:{id:'email-a',subject:'Customer price'},messages:[{id:body.cursor?'older-mail':'new-mail',from:{address:'customer@example.test'},body:body.cursor?'Earlier email':'Latest email',received_at:new Date().toISOString()}],next_cursor:body.cursor?null:'older'});
  }
  if(url.pathname.endsWith('/operations/email_comments_list'))return reply({notes:[{id:'internal',body:'Check the price.',created_by:'owner',author_kind:'human',created_at:new Date().toISOString()}],next_before:null});
  if(attentionPinned && url.pathname.endsWith('/operations/tasks_list')) return reply({tasks:[{id:'child-1',title:'Story child',status:'open',revision:1}],next_cursor:null});
  if(attentionPinned && url.pathname.endsWith('/operations/relationships_list')) return reply({items:[],next_cursor:null});
  if(attentionPinned && url.pathname.endsWith('/operations/workers_list')) return reply({workers:board.items[0].task.workers});
  if(url.pathname.endsWith('/operations/tasks_config_get'))return reply({config:{states:[{name:'open'},{name:'doing'},{name:'done'}]},revision:1});
  if(url.pathname.endsWith('/operations/tasks_update')){
    let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);taskWrites.push({path:url.pathname,body});
    if(holdTaskSave)await new Promise(resolve=>{releaseTaskSave=resolve;});
    assert.equal(body.expected_revision,board.items[0].source_revision);board.items[0].state=body.changes.status;board.items[0].source_revision++;return reply({task:{id:'task-a',revision:board.items[0].source_revision,status:body.changes.status}});
  }
  // The task's creator differs from the Focus presenter (Ordubashy) and from its assignee.
  if(url.pathname.endsWith('/operations/tasks_get'))return reply({task:{id:'task-a',revision:board.items[0].source_revision,status:board.items[0].state,tags:['marketing'],
    created_by:'dev-1',created_by_name:'Dev Builder',created_by_kind:'agent',assignee_ids:['assignee-1']}});
  // Canonical Story records carry only the creator's UUID: no name, no kind. Its creator is a business agent that
  // the Global roster never lists (story-c), a creator no directory knows any more (story-d), or the owner (story-o).
  if(url.pathname.endsWith('/operations/stories_get')) {
    let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);storyReads.push(body.story_id);storyPaths.push(url.pathname);
    return reply({story:{id:body.story_id,revision:1,created_by:{'story-c':'biz-planner','story-d':'departed-agent','story-o':'owner'}[body.story_id],tags:[],links:process.argv.includes('--story-context') && body.story_id!=='story-d'?[{resource_type:'tasks',resource_id:'task-a',item:{type:'task',id:'task-a'},current:{label:'Linked task',state:board.items[0].state,revision:board.items[0].source_revision}},{resource_type:'agent_profiles',resource_id:'onbashy',current:{label:'Onbashy',type:'agent'}},...Array.from({length:6},(_,i)=>({resource_type:'documents',resource_id:'extra-'+i,current:{label:'Additional reference document '+i}})),{resource_type:'documents',resource_id:'note-1',item:{type:'material',id:'note-1'},current:{label:'Research notes',summary:'Research detail'}},{resource_type:'tasks',resource_id:'gone-child',current:null}]:[]}});
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
  if(url.pathname===`/api/root/agents/${agent}/browser-surfaces`){res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({agent_id:agent,configured:true,browsers:attentionPinned?[{id:'b1',kind:'browser',label:'Browser',viewer_url:'https://example.test/browser'}]:[],desktop:desktopSurface}));}
  if(url.pathname==='/api/root/terminals'){res.setHeader('Content-Type','application/json');return res.end('[]');}
  if(url.pathname==='/desktop/'){desktopHits++;res.setHeader('Content-Type','text/html');return res.end('<body style="background:#1c2630;color:white;font:20px sans-serif;padding:40px">Test desktop — no remote connection</body>');}
  if(url.pathname==='/files/view'){res.setHeader('Content-Type','text/html');return res.end('<p>File preview</p>');}
  if(url.pathname==='/') {
    res.setHeader('Content-Type','text/html');
    const styles = fs.readFileSync(path.join(repo,'index.html'),'utf8').match(/<link rel="stylesheet"[^>]+>/g).join('\n');
    return res.end(`<!doctype html><html><head><meta charset="utf-8">${styles}</head><body>
      <div id="toasts"></div><nav hidden><div class="ritem" data-tab="progress"><svg viewBox="0 0 24 24"></svg></div><div class="ritem" data-tab="settings"><svg viewBox="0 0 24 24"></svg></div></nav><button id="wsbtn" hidden></button><div id="host" style="height:100vh;display:flex;min-width:0"></div>
      <!-- the dashboard's own load order, so every shared global exists as in the app --><script src="/js/core.js"></script><script src="/js/safe-links.js"></script><script src="/js/chat-view.js"></script><script src="/js/san-juan-time.js"></script><script src="/js/store.js"></script><script src="/js/agents.js"></script><script src="/js/context-source.js"></script><script src="/js/context.js"></script><script src="/js/learning.js"></script><script src="/js/conversation.js"></script><script src="/js/work-groups.js"></script><script src="/js/relationships.js"></script><script src="/js/signoffs.js"></script><script src="/js/task-snooze.js"></script><script src="/js/tags.js"></script><script src="/js/task-state.js"></script><script src="/js/tasks.js"></script><script src="/js/task-progress.js"></script><script src="/js/record-panel.js"></script><script src="/js/global-tasks.js"></script><script src="/js/record-workspace.js"></script><script src="/js/agentpane.js"></script><script src="/js/agent-focus.js"></script><script src="/js/attention-board.js"></script><script src="/js/agent-viewbox.js"></script><script src="/js/agent-browser.js"></script><script src="/js/global-agents.js"></script><script src="/js/global-calendar.js"></script><script src="/js/global.js"></script><script src="/js/agent-workspace.js"></script>
      <script>
      state.scope='global';state.biz=null;state.globalTab='agents';state.bizs=[];
      window.storyRelatedChips=()=>{};
      window.globalIcon=()=>''; window.broadcastButton=()=>'';window.wireBroadcastButton=()=>{};window.agentNavPaint=()=>{};window.agentNavRefresh=()=>{};
      window.mountTerminal=()=>{};window.mountGlobalChat=()=>({dispose(){}});
      window.agentViewDispose=()=>{};window.mountAgentView=()=>{};
      window.workingMemoryMount=el=>el.textContent='Working memory form';
      window.sgAgentAttentionPaint=()=>{};
      window.tFetchTask=async(biz,id)=>({business:{slug:biz},id,title:'Task detail'});
      window.tDetail=(task,{host}={})=>{window.openedTask=task;host.textContent='Existing task editor'};
      </script>

      <script>
      // Exercise the retained dock and its resize contract in this fixture.
      // The workspace fixture covers the production shell and review overlay.
      agfocusOverlayOpen=()=>{};
      renderGlobalAgents(document.querySelector('#host'));
      </script></body></html>`);
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
    if(attentionPinned) {
      const tab=page.getByRole('tab',{name:'Pinned',exact:true}); await tab.waitFor();
      assert.equal(await tab.getAttribute('aria-selected'),'true');
      assert.equal(await page.getByRole('tab',{name:'Attention board'}).count(),1);
      const row=page.locator('[data-card-id="task-a"]'); await row.waitFor();
      const worker=row.locator('[data-task-workers]'); await worker.waitFor();
      assert.match(await worker.getAttribute('aria-label'),/Dev Builder, Ordubashy, Owner.*User needed/);
      const icons=worker.locator('.identity-avatar'); assert.equal(await icons.count(),3);
      assert.equal(await icons.last().locator('svg').count(),1);
      const boxes=await icons.evaluateAll(nodes=>nodes.map(n=>{const r=n.getBoundingClientRect();return {x:r.x,width:r.width}}));
      assert.ok(Math.abs((boxes[0].x+boxes[0].width-boxes[1].x)/boxes[0].width-.2)<.03,JSON.stringify(boxes));
      await worker.click(); await page.locator('.task-worker-picker').waitFor();
      assert.equal(await page.locator('[data-worker-index]:checked').count(),3);
      await page.keyboard.press('Escape');
      const story=page.locator('[data-card-id="story-c"]'); await story.getByRole('button',{name:'Expand subtasks'}).click();
      const child=page.locator('[data-card-id="story-c/task/task:biz-c:child-1"]'); await child.waitFor();
      await child.locator('.ttl').click(); await page.locator('.agfocus-dock:not([hidden])').waitFor();
      assert.ok(privateGets.some(x=>x.source.id==='child-1' && x.source.business_id==='biz-c'));
      await page.getByRole('button',{name:'Browser',exact:true}).click();
      await page.locator('.agfocus-surface:not([hidden])').waitFor();
      await page.getByRole('button',{name:'Back to Pinned'}).click();
      assert.equal(await page.locator('.agfocus-items').isVisible(),true);
      await page.evaluate(()=>{const item=_agpad.focus.items.find(i=>i.id==='task-a');delete item.resolved_source.business_slug;item.context_business_slug='software';agfocusPaint(_agpad);});
      await row.locator('.agfocus-task-state').click();
      await until(()=>taskWrites.length===1,'context scoped state write');
      assert.equal(taskWrites[0].path,'/api/biz/software/operations/tasks_update');
      await page.evaluate(()=>{const item=_agpad.focus.items.find(i=>i.id==='task-a');delete item.context_business_slug;item.resolved_source.label='unresolved';agfocusPaint(_agpad);window.menuCalls=0;window.oldTaskMenu=taskBoardStateMenu;taskBoardStateMenu=()=>{window.menuCalls++};});
      const ball=row.locator('.agfocus-task-state');
      assert.equal(await ball.isDisabled(),true); assert.equal(await ball.getAttribute('data-task-state-key'),null);
      await page.evaluate(()=>taskStateObserve('software',{id:'task-a',status:'done',revision:99}));
      assert.equal(await ball.isDisabled(),true);assert.equal(await ball.getAttribute('data-task-state-key'),null);
      await ball.dispatchEvent('contextmenu');await ball.dispatchEvent('keydown',{key:'ArrowDown'});
      assert.equal(await page.evaluate(()=>window.menuCalls),0);assert.equal(taskWrites.length,1);
      assert.deepEqual(errors,[]); console.log('Pinned tab, shared workers, overlap, Story child thread, nested Browser and scoped state guards passed'); return;
    }
    if(process.argv.includes('--story-context')) {
      await page.getByRole('tab',{name:'Pinned',exact:true}).click();
      const story=page.locator('[data-card-id="story-c"]');
      await story.waitFor();
      await story.getByRole('button',{name:'Expand Story items'}).click();
      const child=page.locator('[data-card-id="story-c/tasks/task-a"]');
      await child.waitFor();
      assert.equal(await page.locator('.agfocus-story-child').count(),2);
      assert.equal(await page.locator('[data-card-id="story-c/agent_profiles/onbashy"]').count(),0);
      const refs=story.locator('.agfocus-story-references');
      assert.equal(await refs.locator('.agfocus-reference-chip').count(),7);
      assert.equal(await refs.locator('.ball,.sdot').count(),0);
      assert.equal(await refs.locator('[data-kind="file"]').count(),7);
      const more=refs.locator('.agfocus-reference-more');await more.waitFor();
      assert.match(await more.innerText(),/\+\d+ more/);await more.click();
      assert.equal(await refs.locator('.agfocus-reference-chip:visible').count(),7);
      assert.ok((await refs.boundingBox()).y<(await child.boundingBox()).y);
      const popupPromise=page.waitForEvent('popup');await refs.getByRole('button',{name:'Research notes'}).click();
      const popup=await popupPromise;await popup.waitForLoadState();assert.match(popup.url(),/files\/view\?document_id=note-1&business_id=biz-c/);await popup.close();
      await more.click();assert.equal(await more.getAttribute('aria-expanded'),'false');
      await page.setViewportSize({width:800,height:900});
      await page.waitForTimeout(100);
      assert.ok(await refs.evaluate(el=>el.scrollWidth<=el.clientWidth));
      await page.setViewportSize({width:1300,height:900});
      assert.equal(await story.getAttribute('aria-expanded'),'false','icon expands children, not chat');
      assert.ok((await child.boundingBox()).x>(await story.boundingBox()).x);
      assert.equal(await child.locator('.agfocus-scope').isVisible(),false,'same-business child omits repeated business badge');
      const assertChildWidth=async()=>{
        const headline=await child.locator('.task-headline').boundingBox(),row=await child.locator('.agfocus-row').boundingBox(),actions=await child.locator('.agfocus-actions').boundingBox();
        assert.ok(headline.width>row.width*.5,'child title uses available row width');
        assert.ok(headline.x+headline.width<=actions.x+1,'title does not overlap actions');
      };
      await assertChildWidth();
      await page.setViewportSize({width:800,height:900});await assertChildWidth();
      await page.setViewportSize({width:1300,height:900});
      assert.equal(await child.locator('[data-focus-snooze]').isVisible(),false);
      assert.match(await page.locator('[data-card-id="story-c/tasks/gone-child"]').innerText(),/This item is no longer available/);
      // The visual dot shrinks while its clickable control keeps its dimensions.
      await page.mouse.move(0,0);
      const dot=child.locator('.agfocus-task-state .core');
      await page.waitForTimeout(150);
      const small=await dot.boundingBox(),hit=await child.locator('.agfocus-task-state').boundingBox();
      await child.locator('.agfocus-marker').hover();await page.waitForTimeout(150);
      const large=await dot.boundingBox();
      assert.ok(Math.abs(large.width/small.width-2)<.05);
      assert.equal((await child.locator('.agfocus-task-state').boundingBox()).width,hit.width);
      // Refresh between pointer-down/up must retain the state hit target.
      const ball=child.locator('.agfocus-task-state'),bounds=await ball.boundingBox();
      await page.mouse.move(bounds.x+bounds.width/2,bounds.y+bounds.height/2);await page.mouse.down();
      await page.evaluate(()=>{window.heldBall=document.querySelector('[data-card-id="story-c/tasks/task-a"] .agfocus-task-state');_agpad.focus.items[0].label+=' updated';agfocusPaint(_agpad);});
      assert.equal(await page.evaluate(()=>heldBall.isConnected),true);
      await page.mouse.up();
      await page.waitForFunction(()=>document.querySelector('[data-card-id="story-c/tasks/task-a"] .agfocus-task-state').getAttribute('aria-label')==='Working');
      assert.equal(await child.getAttribute('aria-expanded'),'false');
      await child.locator('.ttl').click();
      const dock=page.locator('.agfocus-dock');await dock.waitFor();
      assert.equal(await page.evaluate(()=>{
        const panel=document.createElement('div');panel.className='taskswrap';
        panel.style.cssText='top:100px;left:100px;width:80px;height:80px;background:black';
        const overlay=document.createElement('div');overlay.className='agfocus-dock agfocus-chat-overlay';
        overlay.style.cssText='top:100px;left:100px;width:80px;height:80px';
        document.body.append(panel,overlay);
        const above=document.elementFromPoint(120,120)===panel;panel.remove();overlay.remove();return above;
      }),true,'right-side pane stays above Focus chat even when chat comes later in DOM');
      await page.waitForFunction(()=>!document.querySelector('.agfocus-dock .agfocus-task-details').textContent.includes('Loading'));
      const dimensions=await dock.evaluate(el=>{const region=el.querySelector('.agfocus-region'),context=el.querySelector('.agfocus-thread-context');return [context.getBoundingClientRect().height,region.clientHeight-parseFloat(getComputedStyle(region).paddingTop)-parseFloat(getComputedStyle(region).paddingBottom)];});
      assert.ok(Math.abs(dimensions[0]/dimensions[1]-.2)<.015,JSON.stringify(dimensions));
      assert.equal(await dock.locator('.conversation-channel-icon').first().evaluate(el=>getComputedStyle(el).zoom),'1.3');
      await page.evaluate(()=>agfocusRefresh(_agpad));
      assert.equal(await child.getAttribute('aria-expanded'),'true','board refresh retains child conversation');
      await page.locator('[data-focus-thread-close]').click();
      await story.getByRole('button',{name:'Collapse Story items'}).click();
      assert.equal(await child.isVisible(),false);
      await story.locator('.ttl').click();assert.equal(await story.getAttribute('aria-expanded'),'true');
      // A live-style Story can have only Focus references, including Global tasks.
      await page.evaluate(()=>{
        const item=_agpad.focus.items.find(item=>item.id==='story-d');
        item.references=[{type:'task',id:'global-child',business_id:null,business_slug:null,business_name:'Global',label:'Global launch task',state:'open',revision:2},{type:'reference',resource_type:'data_tables',id:'table-ref',business_id:null,label:'Results table'},{type:'url',url:'https://example.test/research',label:'Research website'}];
        const linked=_agpad.focus.items.find(item=>item.id==='story-c');
        linked.references=[{type:'task',id:'task-a',business_id:'biz-c',label:'Duplicate task',state:'open'}];
      });
      await story.getByRole('button',{name:'Expand Story items'}).click();
      await child.waitFor();assert.equal(await page.locator('[data-card-id="story-c/tasks/task-a"]').count(),1);
      const referenceStory=page.locator('[data-card-id="story-d"]');
      await referenceStory.getByRole('button',{name:'Expand Story items'}).click();
      const globalChild=page.locator('[data-card-id="story-d/tasks/global-child"]');await globalChild.waitFor();
      assert.equal(await referenceStory.locator('.agfocus-reference-chip[data-kind=table]').count(),1);
      assert.equal(await referenceStory.locator('.agfocus-reference-chip[data-kind=link]').count(),1);
      assert.equal(await globalChild.locator('.agfocus-scope').isVisible(),true,'Global child keeps its different scope visible');
      assert.match(await globalChild.innerText(),/Global launch task/);
      assert.ok((await globalChild.boundingBox()).x>(await referenceStory.boundingBox()).x);
      assert.equal(await globalChild.evaluate(el=>el._item.conversation.business_id),null);
      assert.equal(await globalChild.evaluate(el=>el._item.resolved_source.business_slug),null);
      await page.reload();await page.getByRole('tab',{name:'Pinned',exact:true}).click();
      await page.locator('[data-card-id="story-c/tasks/task-a"]').waitFor();
      await page.locator('[data-card-id="story-c"]').getByRole('button',{name:'Collapse Story items'}).click();
      await page.reload();await page.getByRole('tab',{name:'Pinned',exact:true}).click();
      await page.locator('[data-card-id="story-c"]').getByRole('button',{name:'Expand Story items'}).waitFor();
      assert.equal(await page.locator('[data-card-id="story-c/tasks/task-a"]').count(),0);
      await page.evaluate(async()=>{const item=_agpad.focus.items.find(item=>item.id==='story-c');item.resolved_source={...(item.resolved_source || item.source),business_id:null,business_slug:null};await agfocusToggleStory(_agpad,item,true);});
      await page.locator('[data-card-id="story-c/tasks/task-a"]').waitFor();
      assert.ok(storyPaths.includes('/api/root/operations/stories_get'),'Global Story children load through the root route');
      assert.deepEqual(errors,[]);console.log('Story persistence, references, scope, deduplication, Story children, mid-click status refresh, source chat, 20% context and 1.3x icons passed.');
      return;
    }
    const focusTab=page.getByRole('tab',{name:'Pinned',exact:true});
    await focusTab.waitFor();
    assert.equal(await page.locator("#agpad .ttab").first().getAttribute("data-v"),"focus");
    assert.equal(await focusTab.getAttribute('aria-selected'),'true');
    await page.waitForFunction(()=>document.querySelector('[data-v="focus"]')?.classList.contains('agfocus-attention'));
    assert.equal(await page.getByRole('tab',{name:'Agent inbox',exact:true}).getAttribute('aria-selected'),'false');
    const agentPopupPromise=page.waitForEvent('popup');
    await page.locator('[data-global-agent="other"]').click({modifiers:['Control']});
    const agentPopup=await agentPopupPromise;
    await agentPopup.waitForFunction(()=>document.querySelector('[data-global-agent="other"]')?.getAttribute('aria-pressed')==='true');
    assert.equal(await page.locator(`[data-global-agent="${agent}"]`).getAttribute('aria-pressed'),'true');
    await agentPopup.close();
    await page.locator('[data-global-agent="other"]').click();
    assert.equal(await focusTab.count(),0);
    await page.locator(`[data-global-agent="${agent}"]`).click();
    await focusTab.click();
    await page.waitForFunction(()=>document.querySelector('.ttab[data-v="browser"]')?.dataset.attention==='true');
    assert.equal(await page.getByText('Enter the verification code',{exact:true}).count(),0);
    assert.equal(await page.locator('.agfocus img').count(),0);
    assert.equal(await page.locator('.agfocus-items > article').count(),11);
    assert.equal(desktopHits,0);
    assert.equal(await page.locator('.agfocus .conversation-toggle,.agfocus-creator').count(),0);
    assert.equal(await page.getByRole('heading',{name:'From founder',exact:true}).count(),0);
    const firstCard=page.locator('.agfocus-items [data-card-id="task-a"]');
    const thread=id=>page.locator(`[data-focus-thread-id="${id}"]`);
    const firstThread=thread('task-a');
    assert.equal(await firstCard.locator('.ttl').textContent(),'Mailbox setup');
    assert.equal(await firstCard.locator('.ttl').getAttribute('title'),null);
    assert.equal(await firstCard.getAttribute('aria-label'),'Mailbox setup');
    assert.equal(await page.locator('.agfocus-items .agfocus-card .ttl').nth(1).textContent(),'Review the first full outreach draft');
    assert.equal(await page.locator('.agfocus-items .agfocus-card .ttl').nth(2).textContent(),board.items[2].label);
    assert.equal(await firstCard.locator('.agfocus-scope-chip').innerText(),'Qorqut Software');
    assert.equal(await page.locator('.agfocus :is(.agfocus-meta,.agfocus-department,.agfocus-people,.agfocus-reason)').count(),0);

    assert.equal(await firstCard.locator('.agfocus-marker .ball').getAttribute('aria-label'),'Open');
    for (const type of ['story','email']) {
      await page.evaluate(type=>{const item=_agpad.focus.items.find(i=>i.id==='task-a');item.source.task_type=type;if(item.resolved_source)item.resolved_source.task_type=type;agfocusPaint(_agpad);},type);
      assert.equal(await firstCard.locator('.agfocus-marker .task-type-icon-'+type).count(),1);
      assert.equal(await firstCard.locator('.ball, .task-headline .task-type-icon').count(),0);
    }
    await page.evaluate(()=>{const item=_agpad.focus.items.find(i=>i.id==='task-a');item.source.task_type='task';if(item.resolved_source)item.resolved_source.task_type='task';agfocusPaint(_agpad);});
    assert.equal(await firstCard.locator('.agfocus-marker .ball').count(),1,'normal Tasks regain their state control');
    if(process.env.TASK_TYPE_SMOKE){assert.deepEqual(errors,[]);console.log('Focus Task/Story/Email state marker checks passed');return;}

    assert.equal(await page.locator('[data-card-id="signoff-g"] .agfocus-global-chip').getAttribute('aria-label'),'Global');
    assert.equal(await page.locator('.agfocus-row [title]').count(),0,'Focus rows have no hover tooltips');

    // Nothing private is read or acknowledged just because collapsed rows mounted and polled.
    await page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    assert.deepEqual(requests.filter(path=>path.includes('/operations/owner_conversation_')),[],'collapsed rows never touch the private channel');
    assert.equal(privateReads.length,0);

    // ---- compact rows: the Tasks-table rhythm, status, business, title, controls ----
    const layout=()=>page.evaluate(()=>{
      const rect=el=>{const r=el?.getBoundingClientRect();return r?{x:r.x,y:r.y,right:r.right,bottom:r.bottom,w:r.width,h:r.height}:null;};
      return [...document.querySelectorAll('.agfocus-items > .agfocus-card')].map(card=>{
        const row=card.querySelector('.agfocus-row'),chip=row.querySelector('.agfocus-scope-chip');
        const box=getComputedStyle(card);
        return {id:card.dataset.cardId,card:rect(card),row:rect(row),scope:rect(row.querySelector('.agfocus-scope')),chip:rect(chip),
          chipClipped:chip?chip.scrollWidth>chip.clientWidth+1:false,chipText:chip?.textContent.trim() || '',
          grip:rect(row.querySelector('[data-focus-grip]')),details:rect(row.querySelector('[data-manage]')),snooze:rect(row.querySelector('[data-focus-snooze]')),
          status:rect(row.querySelector('.agfocus-marker')),title:rect(row.querySelector('.ttl')),titleClipped:row.querySelector('.ttl').scrollWidth>row.querySelector('.ttl').clientWidth+1 || row.querySelector('.ttl').scrollHeight>row.querySelector('.ttl').clientHeight+1,
          actions:rect(row.querySelector('.agfocus-actions')),region:card.querySelector('.agfocus-region')?getComputedStyle(card.querySelector('.agfocus-region')).display:null,
          box:{border:box.borderTopWidth,background:box.backgroundColor,margin:box.marginTop,padding:box.paddingTop,radius:box.borderRadius}};
      });
    });
    const assertRows=(rows,what)=>{
      const cards=rows.filter(row=>row.id!=='gone-1');
      assert.deepEqual(rows.map(row=>row.id),[...board.items.map(item=>item.id)],what+': board order is kept');
      for(const row of cards) {
        assert.ok(row.row.h>=31.5 && row.row.h<=34,`${what}: ${row.id} is one compact row (${row.row.h})`);
        assert.equal(row.region,'none',`${what}: ${row.id} shows no details while collapsed`);
        assert.equal(row.title.h<=34,true,`${what}: ${row.id} title stays on one line`);
        assert.ok(row.status.right<=row.scope.x+0.5 && row.scope.right<=row.title.x+0.5 && row.title.right<=row.actions.x+0.5,`${what}: ${row.id} orders status, business, title, controls`);
        assert.ok(Math.abs(row.scope.x-cards[0].scope.x)<0.5 && Math.abs(row.status.x-cards[0].status.x)<0.5 && Math.abs(row.title.x-cards[0].title.x)<0.5 && Math.abs(row.actions.right-cards[0].actions.right)<0.5,`${what}: ${row.id} columns align with the first row`);
        assert.ok(row.actions.right<=row.row.right+0.5 && row.row.right<=row.card.right+0.5,`${what}: ${row.id} controls stay inside the pane`);
        assert.deepEqual(row.box,{border:'0px',background:'rgba(0, 0, 0, 0)',margin:'0px',padding:'0px',radius:row.box.radius},`${what}: ${row.id} has no card box`);
      }
      for(let i=1;i<rows.length;i++)assert.ok(rows[i].card.y-rows[i-1].card.bottom<=2.5 && rows[i].card.y-rows[i-1].card.bottom>=-0.5,`${what}: rows are stacked without card gaps`);
    };
    const wideRows=await layout();
    assertRows(wideRows,'wide');
    const byId=rows=>Object.fromEntries(rows.map(row=>[row.id,row]));
    assert.equal(await page.locator('.agfocus-creator').count(),0,'assigner icons are removed');
    for(const row of wideRows.filter(row=>row.id!=='gone-1')){assert.equal(row.grip,null);assert.equal(row.details,null);assert.ok(row.snooze,'far-right snooze remains');}
    // Business name or the Global globe comes first; a long name ellipsizes while its full text stays available.
    assert.equal(byId(wideRows)['task-a'].chipText,'Qorqut Software');
    assert.equal(byId(wideRows)['story-c'].chipText,'Extraordinarily Long Business Name Holdings');
    assert.equal(byId(wideRows)['story-c'].chipClipped,true);
    assert.equal(byId(wideRows)['story-c'].titleClipped,true,'a long Focus title truncates on its collapsed row');
    assert.equal(byId(wideRows)['task-a'].titleClipped,false);
    assert.equal(byId(wideRows)['signoff-g'].chipText,'');
    assert.deepEqual(storyReads,[],'no creator-only source reads');
    assert.deepEqual(directoryReads,[],'no creator-only directory reads');
    // The same size, weight and status ball as a Tasks table row.
    const tasksRow=await page.evaluate(()=>{
      const host=document.createElement('div');host.className='tasks';host.style.cssText='position:absolute;left:0;top:0;width:640px;visibility:hidden';
      host.innerHTML=`<div class="trow personal-task"><span class="task-business-column">${taskBusinessLabel('Runotak')}</span><div class="task-headline">${tBall({status:'open'})}<p class="ttl">Probe</p></div>${tAssignerAvatars({created_by:'dev-1',created_by_name:'Dev Builder',created_by_kind:'agent'})}</div>`;
      document.body.append(host);
      const measure=(scope)=>{const title=getComputedStyle(scope.querySelector('.ttl')),ball=scope.querySelector('.ball').getBoundingClientRect(),core=scope.querySelector('.ball .core').getBoundingClientRect();
        return {row:scope.querySelector('.trow').getBoundingClientRect().height,font:[title.fontSize,title.fontWeight,title.letterSpacing],ball:[ball.width,ball.height],core:[core.width,core.height]};};
      const tasks=measure(host);host.remove();
      const focus=measure(document.querySelector('[data-card-id="task-a"]'));
      return {tasks,focus};
    });
    assert.equal(Math.round(tasksRow.focus.row),32,'Focus uses compact rows');
    assert.deepEqual(tasksRow.focus.ball,[26,26],'Focus state hit targets are preserved');
    await page.locator('#agpad-body .agfocus-layout').evaluate(el=>el.scrollTop=0);
    await page.screenshot({path:'/tmp/qorqut-focus-rows.png'});

    assert.equal(await firstCard.locator('[data-manage]').getAttribute('title'),null);
    assert.equal(await firstCard.locator('.conversation-toggle').count(),0);
    assert.equal(await firstThread.locator('textarea').getAttribute('title'),null);
    const commentInput=firstThread.getByRole('textbox',{name:'Leave a comment',exact:true});
    assert.equal(await firstCard.getAttribute('aria-expanded'),'false');
    const popupPromise=page.waitForEvent('popup');
    await firstCard.locator('.ttl').click({modifiers:['Control']});
    const popup=await popupPromise;
    await popup.getByText('Existing task editor',{exact:true}).waitFor();
    assert.equal(await popup.evaluate(()=>window.opener),null);
    assert.equal(await firstCard.getAttribute('aria-expanded'),'false');
    assert.equal(await page.locator('.agfocus-detail').count(),0);
    await popup.close();

    // The status ball and the menu are independent of the row: neither expands it.
    for(const label of ['Working','Waiting','Done','Open']){
      await firstCard.locator('.agfocus-task-state').click();
      await page.waitForFunction(label=>document.querySelector('.agfocus-task-state')?.getAttribute('aria-label')===label,label);
    }
    await page.waitForFunction(()=>document.querySelector('.agfocus-task-state')?.getAttribute('aria-busy')==='false');
    assert.equal(await firstCard.getAttribute('aria-expanded'),'false');
    await firstCard.locator('.ttl').click();
    await page.locator('.agfocus-dock [data-manage]').click();
    await page.getByText('Existing task editor',{exact:true}).waitFor();
    assert.equal(await page.locator('.menu').count(),0,'Open details uses the existing source view');
    await page.getByRole('button',{name:'Back to Focus',exact:true}).click();
    assert.equal(await firstCard.getAttribute('aria-expanded'),'false');

    assert.equal(await commentInput.isVisible(),false);
    assert.equal(await page.locator('.agfocus-reason').count(),0);
    await firstCard.locator('.ttl').click();
    assert.equal(await firstCard.getAttribute('aria-expanded'),'true');

    assert.equal(await commentInput.isVisible(),true);
    assert.equal(await commentInput.evaluate(el=>el===document.activeElement),true);
    assert.equal(await commentInput.evaluate(el=>getComputedStyle(el).backgroundColor),'rgba(0, 0, 0, 0)');
    // Opening a card for the first time expands its chat by default too.
    assert.equal(await firstThread.locator('.conversation-panel').isVisible(),true);
    await commentInput.fill('Please check this mapping');
    await page.locator('[data-focus-refresh]').click();
    assert.equal(await firstCard.getAttribute('aria-expanded'),'true','Refresh keeps the selected dock open');
    assert.equal(await commentInput.inputValue(),'Please check this mapping');
    await firstThread.getByRole('button',{name:'Send',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.agfocus-dock textarea').value==='');
    // The owner's card composer is the private channel, routed through Global
    // with the owning business named inside the canonical source.
    assert.equal(comments.length,0,'the owner never posts to the agent discussion');
    assert.equal(privateSends[0].text,'Please check this mapping');
    assert.equal(privateSends[0].path,'/api/root/operations/owner_conversation_send');
    assert.equal(privateSends[0].scope,'global');
    assert.deepEqual(privateSends[0].source,{type:'task',id:'task-a',business_id:'biz-a'});
    await commentInput.fill('One more detail');await commentInput.press('Control+Enter');
    await page.waitForFunction(()=>document.querySelector('.agfocus-dock textarea').value==='');
    assert.equal(privateSends.length,2);assert.equal(privateSends[1].text,'One more detail');
    assert.equal(comments.length,0);
    // Refresh keeps the selected chat visible and its composer independent of the row.
    assert.equal(await firstThread.locator('.conversation-panel').isVisible(),true);
    assert.equal(await page.locator('.agfocus-detail').count(),0);
    assert.equal(await page.locator('.agfocus-people,.agfocus-person').count(),0);
    assert.equal(await page.locator('.agfocus-dock .conversation-toggle').count(),0,'no duplicate Comment beside Send');
    await firstThread.locator('.gchat-scroll').click();
    assert.equal(await firstCard.getAttribute('aria-expanded'),'true');
    assert.equal(await firstThread.locator('.conversation-panel').isVisible(),true);
    await firstCard.locator('.ttl').click();
    assert.equal(await firstCard.getAttribute('aria-expanded'),'false');
    assert.equal(await firstCard.locator('.agfocus-region').isVisible(),false);
    await firstCard.locator('.ttl').click();
    assert.equal(await firstCard.getAttribute('aria-expanded'),'true');
    assert.equal(await firstThread.locator('.conversation-panel').isVisible(),true);
    const taskReadsBefore=requests.filter(p=>p.endsWith('/tasks_get')).length;
    const focusReadsBefore=requests.filter(p=>p.endsWith('/focus_get')).length;
    holdTaskSave=true;
    await firstCard.locator('.agfocus-task-state').click();
    await page.waitForFunction(()=>document.querySelector('.agfocus-task-state')?.getAttribute('aria-label')==='Working');
    assert.equal(await firstCard.locator('.agfocus-task-state').getAttribute('aria-busy'),'true');
    assert.equal(requests.filter(p=>p.endsWith('/tasks_get')).length,taskReadsBefore);
    while(!releaseTaskSave)await new Promise(resolve=>setTimeout(resolve,5));
    holdTaskSave=false;releaseTaskSave();
    await page.waitForFunction(()=>document.querySelector('.agfocus-task-state')?.getAttribute('aria-busy')==='false');
    assert.equal(requests.filter(p=>p.endsWith('/focus_get')).length,focusReadsBefore);

    for(const label of ['Waiting','Done','Open']){
      await firstCard.locator('.agfocus-task-state').click();
      await page.waitForFunction(label=>document.querySelector('.agfocus-task-state')?.getAttribute('aria-label')===label,label);
    }
    await page.waitForFunction(()=>document.querySelector('.agfocus-task-state')?.getAttribute('aria-busy')==='false');
    assert.equal(await firstCard.getAttribute('aria-expanded'),'true');
    const boardReadsBefore=focusBoardReads;holdFocusRead=true;
    await page.evaluate(()=>{window.refreshA=agfocusRefresh(_agpad);window.refreshB=agfocusRefresh(_agpad);});
    while(!releaseFocusRead)await new Promise(resolve=>setTimeout(resolve,5));
    releaseFocusRead();
    await page.evaluate(()=>Promise.all([window.refreshA,window.refreshB]));
    assert.equal(focusBoardReads,boardReadsBefore+2,'refresh during an in-flight read runs afterwards');

    // Unread is a noninteractive dot beside the title and part of the row name.
    const globalCard=page.locator('[data-card-id="signoff-g"]'),globalThread=thread('signoff-g'),dot=globalCard.locator('.agfocus-unread');
    await dot.waitFor();
    assert.equal(await globalCard.getAttribute('aria-expanded'),'false');
    assert.equal(await globalCard.locator('.agpad-tab-dot').count(),0);
    assert.equal(await dot.evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(255, 255, 255)');
    assert.equal(await globalCard.locator('.agfocus-actions').evaluate(el=>getComputedStyle(el).opacity),'1','a dimmed collapsed card still shows the dot');
    const dotBox=await dot.boundingBox(),titleBox=await globalCard.locator('.ttl').boundingBox();
    const stateBox=await globalCard.locator('.agfocus-marker').boundingBox();
    assert.ok(dotBox.x+dotBox.width<stateBox.x && dotBox.width===5.5,'unread dot sits left of the state ball');
    assert.equal(await dot.getAttribute('tabindex'),null,'the dot is noninteractive');
    const signature=()=>page.evaluate(()=>document.querySelector('.agfocus-items').dataset.signature);
    const unchanged=await signature();
    await page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    assert.equal(await signature(),unchanged,'an unchanged board keeps its signature');
    assert.equal(await dot.isVisible(),true);
    board.items[2].reason='Global review, refreshed.';board.revision++;
    await page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    await page.waitForFunction(revision=>Number(document.querySelector('.agfocus').dataset.revision)===revision,board.revision);
    assert.notEqual(await signature(),unchanged);
    assert.equal(await dot.isVisible(),true,'a repaint keeps an unread private reply marked');
    // The accessible row name carries the unread state.
    const globalToggle=globalCard.locator('.ttl');
    assert.equal(await globalCard.getAttribute('aria-label'),'Shared instructions, unread private reply');
    const readsBefore=privateReads.length;
    await globalToggle.click();
    await until(()=>privateReads.length>readsBefore,'a read acknowledgment');
    assert.deepEqual(privateReads.at(-1).source,{type:'signoff',id:'signoff-g'});
    assert.equal(privateReads.at(-1).scope,'global');
    assert.equal(privateReads.at(-1).sequence,1);
    await page.waitForFunction(()=>document.querySelector('[data-card-id="signoff-g"] .agfocus-unread')?.hidden===true);
    assert.equal(await globalCard.getAttribute('aria-label'),'Shared instructions');
    await page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    assert.equal(await dot.isVisible(),false,'the cleared dot stays cleared after a refresh');
    // A Focus record thread is only the Ordubashy conversation: no tablist, worker or team channel.
    assert.equal(await globalThread.locator('[role="tab"],[role="tablist"],.conversation-channel').count(),0);
    assert.equal(await globalThread.locator('.conversation-panel').getAttribute('aria-label'),'Conversation with Ordubashy');

    // A board read already in flight when the acknowledgment lands cannot revive
    // the dot: its projection predates the new read cursor.
    privateThread('signoff-g').messages.push({id:'signoff-g-m2',sequence:2,body:'A second private reply',created_by:'chief',created_by_name:'Ordubashy',author_kind:'agent',author_role:'coordinator',is_owner:false,created_at:new Date().toISOString()});
    privateUnread.add('signoff-g');
    await page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    await page.waitForFunction(()=>document.querySelector('[data-card-id="signoff-g"] .agfocus-unread')?.hidden===false);
    releaseFocusRead=null;holdFocusRead=true;
    // The held response also carries a board change, so it must repaint on arrival.
    board.items[2].reason='Global review, in flight.';board.revision++;
    const staleRead=page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    while(!releaseFocusRead)await new Promise(resolve=>setTimeout(resolve,5));
    const readsBeforeSecond=privateReads.length;
    await globalToggle.click();await globalToggle.click();
    await until(()=>privateReads.length>readsBeforeSecond,'a second read acknowledgment');
    assert.equal(privateReads.at(-1).sequence,2);
    await page.waitForFunction(()=>document.querySelector('[data-card-id="signoff-g"] .agfocus-unread')?.hidden===true);
    releaseFocusRead();await staleRead;
    await page.waitForFunction(revision=>Number(document.querySelector('.agfocus').dataset.revision)===revision,board.revision);
    assert.equal(await dot.isVisible(),false,'a board response older than the acknowledgment cannot revive the dot');
    await page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    assert.equal(await dot.isVisible(),false,'a later board read confirms the cleared state');
    await page.locator('[data-focus-thread-close]').click();
    await page.waitForFunction(()=>document.querySelector('[data-focus-thread-id="signoff-g"] .conversation-panel')?.hidden===true);

    // If a newer private reply arrives between the private GET and mark-read,
    // the server keeps unread=true. The dot and Focus projection must survive,
    // and the callback must not claim the card was read.
    privateUnread.delete('signoff-g');
    privateThread('signoff-g').messages.push({id:'signoff-g-m3',sequence:3,body:'A reply arrived during read',created_by:'chief',created_by_name:'Ordubashy',author_kind:'agent',author_role:'coordinator',is_owner:false,created_at:new Date().toISOString()});
    // Hold a board request before opening Private. Its false projection must not
    // clear the local unread override created by the later mark-read response.
    holdFocusRead=true;
    const staleUnreadBoard=page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    while(!releaseFocusRead)await new Promise(resolve=>setTimeout(resolve,5));
    assert.equal(await dot.isVisible(),false,'the private GET starts with a clear dot');
    markReadUnread=true;
    const readsBeforeUnreadAck=privateReads.length;
    await globalToggle.click();
    await until(()=>privateReads.length>readsBeforeUnreadAck,'the concurrent unread acknowledgment');
    await new Promise(resolve=>setTimeout(resolve,50));
    await page.waitForFunction(()=>document.querySelector('[data-card-id="signoff-g"] .agfocus-unread')?.hidden===false);
    assert.equal(await page.evaluate(()=>_agpad.focus.items.find(item=>item.id==='signoff-g').private_unread),true,'Focus projection stays unread');
    releaseFocusRead();await staleUnreadBoard;
    await page.waitForFunction(()=>document.querySelector('[data-card-id="signoff-g"] .agfocus-unread')?.hidden===false);
    assert.equal(await dot.isVisible(),true,'a board response started before the acknowledgment cannot clear the dot');
    privateUnread.delete('signoff-g');
    await page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    await page.waitForFunction(()=>document.querySelector('[data-card-id="signoff-g"] .agfocus-unread')?.hidden===true);
    assert.equal(await dot.isVisible(),false,'a fresh authoritative board response clears the local override');
    markReadUnread=false;

    // The agent-inbox modal keys the same conversation state as its board card.
    await page.evaluate(()=>agpadOpenInboxSource({inboxSource:'task-a',sourceType:'focus_cards'}));
    await page.locator('#focus-inbox-conversation').waitFor();
    assert.equal(await page.locator('#focus-inbox-conversation').getAttribute('data-conversation'),'task:software:task-a');
    // This mount has no startCollapsed override, so it's expanded by default already.
    await page.locator('#focus-inbox-conversation textarea').fill('Shared private draft');
    assert.equal(await firstThread.locator('.conversation-inline-compose textarea').inputValue(),'Shared private draft');
    await page.locator('#focus-inbox-conversation textarea').fill('');
    await page.evaluate(()=>{while(_modal)_modal.close();});

    // Owner mode comes from the shared /api/session read, not a hand-set flag.
    assert.equal(await page.evaluate(async()=>{await loadSession(true);return state.session?.is_owner;}),true);
    assert.ok(sessionReads>0);
    sessionOwner=false;
    assert.deepEqual(await page.evaluate(async()=>{
      await loadSession(true);
      const host=document.createElement('div');document.body.append(host);
      const view=conversationMount(host,{type:'reference',id:'teammate-card',biz:null},{read:async()=>({notes:[],next_before:null})});
      view.expand();
      const result=[view.channel(),!host.querySelector('.gchat-composer').hidden];
      view.collapse();host.remove();
      return result;
    }),['team',true],'a teammate keeps the agent discussion composer');
    sessionOwner=true;
    await page.evaluate(()=>loadSession(true));

    assert.equal(await page.locator('.agfocus-detail').count(),0);
    await page.locator('[data-focus-thread-close]').click();
    const commentToggle=firstCard.locator('.ttl');
    assert.equal(await firstCard.getAttribute('aria-expanded'),'false');
    await commentToggle.click();assert.equal(await firstThread.locator('.conversation-panel').isVisible(),true);
    assert.equal(await firstCard.getAttribute('aria-expanded'),'true');
    assert.equal(await page.locator('.agfocus-dock .conversation-toggle').count(),0);
    assert.equal(await firstCard.locator('[title]').count(),0);
    await firstThread.locator('.gchat-scroll').click();
    await page.locator('.agfocus-heading .global-eyebrow').click();
    assert.equal(await firstThread.locator('.conversation-panel').isVisible(),true,'Focus interactions do not dismiss the dock');
    await commentToggle.click();assert.equal(await firstThread.locator('.conversation-panel').isVisible(),false);
    await commentToggle.click();
    await page.locator('[data-focus-thread-close]').click();
    await firstCard.locator('.ttl').click();
    const menu=page.locator('.agfocus-dock [data-manage]');
    await menu.click();
    await page.getByText('Existing task editor',{exact:true}).waitFor();
    await page.getByRole('button',{name:'Back to Focus',exact:true}).click();


    assert.equal(await page.locator('[data-open]').count(),0);
    await page.getByRole('button',{name:'Hide agent navigation',exact:true}).click();
    assert.equal(await page.locator('#global-agent-list').isVisible(),false);
    assert.equal(await page.getByRole('button',{name:'Show agent navigation',exact:true}).isVisible(),true);
    await page.getByRole('button',{name:'Show agent navigation',exact:true}).click();
    assert.equal(await page.locator('#global-agent-list').isVisible(),true);
    assert.equal(await page.evaluate(()=>{
      const button=document.querySelector('[data-card-id="task-a"]');
      button.focus();agfocusPaint(_agpad);
      return button===document.activeElement && button.isConnected;
    }),true);
    // Ordubashy's Desktop lives behind its single Browser entry's mode toggle: unavailable until a
    // desktop surface is configured, then one lazy iframe with the pending-input controls.
    const browserTab=page.locator('.ttab[data-v="browser"]');
    await browserTab.click();
    await page.locator('[data-agent-mode="desktop"]').click();
    await page.getByText('Desktop is not configured.',{exact:false}).waitFor();
    assert.equal(await page.locator('iframe').count(),0);
    assert.equal(await page.locator('.agfocus-note').count(),0);
    assert.equal(desktopHits,0);
    desktopSurface={kind:'desktop',id:'ordubashy-desktop',label:'Desktop',viewer_url:origin+'/desktop/?keep=1&qorqut_width=1920&qorqut_height=1080#/session'};
    await page.locator('[data-desktop-retry]').click();
    await page.locator('.agfocus-note').waitFor();
    assert.equal(await page.locator('.agfocus > .agfocus-browser .agfocus-handoff').count(),0);
    await page.locator('.agfocus-note').fill('Entered the code.');
    await page.locator('.agfocus-note').focus();
    board.status='Waiting for your browser input';board.revision++;
    await page.evaluate(()=>agfocusRefresh(_agpad));
    await page.waitForFunction(status=>_agpad.focus.status===status,board.status);
    assert.equal(await page.getByText(board.status,{exact:true}).count(),0);
    assert.equal(await page.locator('.agfocus-updated,.agfocus-status').count(),0);
    assert.equal(await page.locator('.agfocus-note').inputValue(),'Entered the code.');
    assert.equal(await page.evaluate(()=>document.activeElement.classList.contains('agfocus-note')),true);
    await page.locator('iframe').waitFor();
    assert.equal(await page.locator('iframe').getAttribute('src'),origin+'/desktop/?keep=1#/session');
    await page.waitForFunction(()=>document.querySelector('iframe')?.contentDocument?.body?.textContent.includes('Test desktop'));
    assert.equal(desktopHits,1);
    const displayFit = await page.evaluate(() => ({
      wide: agfocusDesktopFit(1300,900,1920,1080),
      short: agfocusDesktopFit(1000,500,1920,1080),
      narrow: agfocusDesktopFit(500,900,1600,1000),
      url: agfocusDesktopURL('http://example.test/desktop?keep=1&qorqut_width=1600&qorqut_height=1000#/session')
    }));
    assert.deepEqual(displayFit.wide,{width:1300,height:731.25});
    assert.ok(Math.abs(displayFit.short.width - 888.8888888888889) < 1e-9);
    assert.equal(displayFit.short.height,500);
    assert.deepEqual(displayFit.narrow,{width:500,height:312.5});
    assert.deepEqual(displayFit.url,{url:'http://example.test/desktop?keep=1#/session',width:1600,height:1000});
    const desktopBox = await page.locator('.agfocus-desktop-frame').boundingBox();
    assert.ok(Math.abs(desktopBox.width / desktopBox.height - 1920 / 1080) < 0.02);
    await page.setViewportSize({width:700,height:900});
    await page.waitForFunction(() => {
      const frame=document.querySelector('.agfocus-desktop-frame'), body=frame?.closest('.agfocus-desktop-view');
      // The fit is applied on the next frame, so wait for it to settle at the display's aspect ratio.
      const box=frame?.getBoundingClientRect();
      return frame && body && box.width <= body.getBoundingClientRect().width + 0.5 && Math.abs(box.width / box.height - 1920 / 1080) < 0.02;
    });
    const narrowFrame = await page.locator('.agfocus-desktop-frame').boundingBox();
    assert.ok(Math.abs(narrowFrame.width / narrowFrame.height - 1920 / 1080) < 0.02);
    assert.equal(desktopHits,1);
    await page.setViewportSize({width:1300,height:900});
    await page.screenshot({path:'/tmp/qorqut-focus-desktop.png'});
    await focusTab.click();
    await page.locator('.agfocus-items > .agfocus-card').first().waitFor();
    assert.equal(await page.locator('iframe').count(),0);
    assert.equal(doneWrites,0);
    assert.equal(board.browser.state,'pending');
    await page.locator('[data-card-id=task-a] .ttl').click();await page.locator('.agfocus-dock [data-manage]').click();
    assert.equal(await page.evaluate(()=>state.scope),'global');
    assert.equal(await page.evaluate(()=>openedTask.business.slug),'software');
    assert.equal(await page.locator('.overlay').count(),0);
    assert.equal(await page.locator('.agfocus-detail-body').innerText(),'Existing task editor');
    await page.getByRole('button',{name:'Back to Focus',exact:true}).click();
    // A stored email Focus reference is historical: an explicit note, no mail request, in either scope.
    for (const item of [{type:'email',id:'email-a',business_id:'biz-a',business_slug:'software',label:'Price review'},{type:'email',id:'mail-g',business_id:null,business_slug:null,label:'Dinner plans'}]) {
      await page.evaluate(item=>agfocusOpenItem(_agpad,item),item);
      await page.locator('.agfocus-detail').getByText(/historical email reference/).waitFor();
      assert.equal(await page.locator('.agfocus-detail :is(.email-message,.email-comments,.email-controls,.email-attachments,textarea)').count(),0);
      await page.getByRole('button',{name:'Back to Focus',exact:true}).click();
    }
    assert.deepEqual(globalEmailReads,[],'no mail endpoint was requested');
    const viewCount = await page.evaluate(()=>sgViews.size);
    for (const id of ['signoff-b','signoff-g']) {
      const i = id==='signoff-b'?1:2;
      await page.locator(`[data-card-id="${id}"] .ttl`).click();
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      await page.locator('.agfocus-dock [data-manage]').click();
        await page.locator(`.agfocus-signoff .sg-row[data-id="${id}"]`).waitFor();
      assert.equal(await page.evaluate(()=>state.scope),'global');
      assert.equal(await page.locator('.agfocus-signoff .sg-detail [data-sg="approve"]').isEnabled(),true);
      if(id==='signoff-b') {
        // Every shared conversation mounts collapsed; a Signoff detail opens it with its Comment toggle.
        assert.equal(await page.locator('.agfocus-signoff .conversation-panel').isVisible(),false);
        await page.locator('.agfocus-signoff .conversation-toggle').click();
        assert.equal(await page.locator('.agfocus-signoff .conversation-panel').isVisible(),true);
        assert.equal(await page.locator('.agfocus-signoff .sg-detail [data-sg="reject"]').isVisible(),true);
        await page.locator('.agfocus-signoff .sg-detail [data-sg="reject"]').click();
        assert.equal(await page.locator('.agfocus-signoff .conversation-panel').isVisible(),true);
        // Decision feedback has its own field, separate from both channels.
        await page.getByText('Add a message before rejecting. Use the decision message field.',{exact:true}).waitFor();
        await page.locator('.agfocus-signoff .sg-feedback').fill('Please revise this scope');
        await page.getByRole('button',{name:'Send and reject',exact:true}).waitFor();
        await page.getByRole('button',{name:'Send and approve',exact:true}).waitFor();
        assert.equal(await page.locator('.agfocus-signoff .conversation-channel').count(),0);
        await page.locator('.agfocus-signoff .conversation-panel textarea').fill('A private thought for Ordubashy');
        assert.equal(await page.locator('.agfocus-signoff .sg-feedback').inputValue(),'Please revise this scope');
        await page.locator('.agfocus-signoff .sg-detail [data-sg="approve"]').click();
        await page.waitForFunction(()=>!document.querySelector('.agfocus-signoff .sg-detail [data-sg="reject"]'));
        assert.equal(decisions.length,1);assert.equal(decisions[0].feedback,'Please revise this scope');
        // The private draft was never part of the decision and was never sent.
        assert.equal(privateSends.filter(send=>send.source.id==='signoff-b').length,0);
      }
      await page.getByRole('button',{name:'Back to Focus',exact:true}).click();
      assert.equal(await page.evaluate(()=>sgViews.size),viewCount);
    }
    assert.ok(requests.includes('/api/biz/runotak/signoffs/signoff-b'));
    assert.ok(requests.includes('/api/root/signoffs/signoff-g'));
    await page.locator('#agpad-body .agfocus-layout').evaluate(el=>el.scrollTop=0);
    await page.screenshot({path:'/tmp/qorqut-focus-board.png'});
    await browserTab.click();
    await page.locator('.agfocus-note').waitFor();
    assert.equal(await page.locator('.agfocus-note').inputValue(),'Entered the code.');
    loseResponse=true;
    await page.getByRole('button',{name:'I’m done',exact:true}).click();
    // Stop automatic refresh briefly to exercise a lost-response retry of the exact payload.
    await page.evaluate(()=>{_agpad.nextFocus=Date.now()+60000});
    await page.getByRole('button',{name:'Retry same response',exact:true}).waitFor();
    await page.evaluate(()=>{_agpad.nextFocus=Date.now()+60000});
    await page.getByRole('button',{name:'Retry same response',exact:true}).click();
    await page.getByText('Input sent to Ordubashy.',{exact:true}).waitFor();
    assert.equal(doneWrites,1);assert.deepEqual(submissions[0],submissions[1]);
    await focusTab.click();
    assert.equal(await browserTab.getAttribute('data-attention'),'false');
    board.browser=null;board.revision++;
    // Awaiting the refresh also covers one already in flight from mounting the Focus tab.
    await page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    await page.waitForFunction(()=>_agpad.focus.browser===null);
    await browserTab.click();
    await page.locator('iframe').waitFor();
    assert.equal(await page.locator('.agfocus-note').count(),0);
    await focusTab.click();
    board.browser={request_id:'input-2',state:'pending',prompt:'Another input',app:'Chrome',tab:'Other tab'};board.revision++;
    await page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    await page.waitForFunction(()=>_agpad.focus.browser?.request_id==='input-2');
    assert.equal(await browserTab.getAttribute('data-attention'),'true');
    // An unsafe desktop address is never framed: Reconnect reads it and refuses it.
    desktopSurface={...desktopSurface,viewer_url:'javascript:alert(1)'};
    const hitsBeforeVisit=desktopHits;
    await browserTab.click();
    await until(()=>desktopHits===hitsBeforeVisit+1,'the cached desktop to load');
    await page.locator('[data-desktop-retry]').click();
    await page.getByText('Desktop is not configured.',{exact:false}).waitFor();
    assert.equal(await page.locator('iframe').count(),0);
    assert.equal(await page.locator('.agfocus-note').count(),0);
    assert.equal(desktopHits,hitsBeforeVisit+1,'the refused address was never loaded');
    await focusTab.click();
    await page.locator('.agfocus-items > .agfocus-card').first().waitFor();

    // ---- rows: keyboard, placement, drafts, refresh stability, retained conversations ----
    if(await page.locator('.agfocus-dock').isVisible())await page.locator('[data-focus-thread-close]').click();
    assert.equal(await page.locator('.agfocus-card[aria-expanded="true"]').count(),0);
    await page.evaluate(()=>{window.taskOpens=[];const original=window.tOpenTask;window.tOpenTask=id=>{taskOpens.push(id);return original(id);};});
    const outreach=page.locator('[data-card-id="signoff-b"]'),outreachThread=thread('signoff-b');
    const opened=()=>outreach.getAttribute('aria-expanded');
    const waiting=privateThread('signoff-b'),waitingSequence=waiting.messages.at(-1).sequence+1;
    waiting.messages.push({id:'signoff-b-m'+waitingSequence,sequence:waitingSequence,body:'A newer private reply',created_by:'chief',created_by_name:'Ordubashy',author_kind:'agent',author_role:'coordinator',is_owner:false,created_at:new Date().toISOString()});
    const readsBeforeOpen=privateReads.length,privateGetsBefore=requests.filter(path=>path.endsWith('/owner_conversation_get')).length;
    await outreach.focus();await page.keyboard.press('Enter');
    assert.equal(await opened(),'true','Enter opens the focused row');
    const geometry=()=>page.evaluate(()=>{
      const list=document.querySelector('.agfocus-items').getBoundingClientRect(),dock=document.querySelector('.agfocus-dock').getBoundingClientRect(),layout=document.querySelector('.agfocus-layout').getBoundingClientRect();
      const rows=[...document.querySelectorAll('.agfocus-items > .agfocus-card')];
      return {list:{x:list.x,y:list.y,w:list.width},dock:{x:dock.x,y:dock.y,w:dock.width,h:dock.height},layoutWidth:layout.width,
        statusHitFits:rows.every(row=>{const ball=row.querySelector('.agfocus-task-state'),title=row.querySelector('.ttl');return !ball || ball.getBoundingClientRect().right<=title.getBoundingClientRect().left+0.5;}),
        underList:rows.some(row=>row.querySelector('.agfocus-region:not([hidden])')),visible:document.querySelectorAll('.agfocus-dock .conversation-panel:not([hidden])').length,
        composerFocused:document.activeElement===document.querySelector('.agfocus-dock textarea')};
    });
    const placement=await geometry();
    assert.ok(placement.dock.x>placement.list.x+placement.list.w,'the conversation is to the right of the list');
    assert.ok(Math.abs(placement.list.w-placement.dock.w)<1 && Math.abs(placement.list.w*2+8-placement.layoutWidth)<1,'a fixed 50/50 split includes the gap');
    assert.ok(Math.abs(placement.dock.y-placement.list.y)<1,'the dock top-aligns with the list');
    assert.equal(placement.underList,false);assert.equal(placement.statusHitFits,true,'compact status hit areas do not cover the headline');assert.equal(placement.visible,1);assert.equal(placement.composerFocused,true);
    assert.equal(await outreachThread.locator('.conversation-channel').count(),0,'no channel selector in a record thread');
    await until(()=>privateReads.length>readsBeforeOpen,'the explicit open to acknowledge');
    assert.equal(privateReads.at(-1).sequence,waitingSequence);assert.equal(privateReads.at(-1).scope,'global');assert.equal(privateReads.at(-1).source.id,'signoff-b');
    assert.equal(requests.filter(path=>path.endsWith('/owner_conversation_get')).length>privateGetsBefore,true);
    assert.equal(await page.locator('.agfocus :is(.agfocus-reason,.agfocus-people,.agfocus-department,.agfocus-meta)').count(),0);
    for(const item of board.items)if(item.reason)assert.equal(await page.getByText(item.reason,{exact:true}).count(),0,'presentation reasons are absent');
    await outreach.focus();await page.keyboard.press(' ');
    assert.equal(await opened(),'false','Space collapses the focused row');
    assert.equal(await page.locator('.agfocus-dock').isVisible(),false);
    const closedWidth=await page.locator('.agfocus-items').evaluate(el=>el.getBoundingClientRect().width);
    assert.ok(closedWidth>placement.list.w*1.9,'closing restores the full list width');
    await page.keyboard.press('Enter');assert.equal(await opened(),'true');
    await page.locator('.agfocus-heading .global-eyebrow').click();assert.equal(await opened(),'true');
    await outreachThread.locator('textarea').fill('Draft survives the row');
    await outreach.locator('.ttl').click();assert.equal(await opened(),'false');
    assert.equal(await outreachThread.locator('textarea').inputValue(),'Draft survives the row');
    await outreach.locator('.agfocus-scope').click();assert.equal(await opened(),'true');
    await page.evaluate(()=>{window.keepHost=_agpad.focusCards.get('signoff-b')._conversationHost;window.keepInput=keepHost.querySelector('textarea');keepInput.focus();});
    board.items[1].reason='Ready for your decision, updated.';board.revision++;
    await page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    await page.waitForFunction(revision=>Number(document.querySelector('.agfocus').dataset.revision)===revision,board.revision);
    assert.equal(await page.evaluate(()=>_agpad.focusCards.get('signoff-b')._conversationHost===keepHost && keepHost.querySelector('textarea')===keepInput && document.activeElement===keepInput && keepInput.value==='Draft survives the row'),true,'refresh preserves the host, draft and focus');
    await firstCard.locator('.ttl').click();
    assert.equal(await opened(),'false');assert.equal(await firstCard.getAttribute('aria-expanded'),'true');
    assert.equal(await page.locator('.agfocus-card[aria-expanded="true"]').count(),1);
    await firstThread.locator('textarea').fill('Task draft stays separate');
    await outreach.locator('.ttl').click();
    assert.equal(await outreachThread.locator('textarea').inputValue(),'Draft survives the row');
    assert.equal(await firstThread.locator('textarea').inputValue(),'Task draft stays separate');
    await outreach.locator('.ttl').click();
    const [removed]=board.items.splice(1,1);board.revision++;
    await page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    assert.equal(await page.locator('.agfocus-items > .agfocus-card').count(),10,'the visible list contains only the canonical page');
    assert.equal(await page.locator('.agfocus-retained [data-card-id="signoff-b"]').count(),1);
    assert.equal(await outreachThread.locator('textarea').inputValue(),'Draft survives the row');
    board.items.splice(1,0,removed);board.revision++;
    await page.evaluate(()=>{_agpad.nextFocus=0;return agfocusRefresh(_agpad);});
    assert.deepEqual(await page.evaluate(()=>[...document.querySelectorAll('.agfocus-items > .agfocus-card')].map(card=>card.dataset.cardId)),board.items.map(item=>item.id));
    await outreach.locator('.agfocus-scope').click();assert.equal(await opened(),'true');
    assert.equal(await page.evaluate(()=>_agpad.focusCards.get('signoff-b')._conversationHost===keepHost),true,'leaving the page retains the mount');
    await outreachThread.locator('textarea').fill('');
    assert.equal(privateSends.filter(send=>send.source.id==='signoff-b').length,0,'navigation never sends a draft');

    // Height changes are vertical only, accessible, persisted, and clamped to the pane.
    const handle=page.getByRole('separator',{name:'Thread height',exact:true});
    assert.equal(await handle.getAttribute('aria-orientation'),'horizontal');
    assert.equal(await handle.getAttribute('aria-valuenow'),'320');
    const beforeResize=await geometry();
    const dragBox=await handle.boundingBox();
    await page.mouse.move(dragBox.x+dragBox.width/2,dragBox.y+dragBox.height/2);await page.mouse.down();
    await page.mouse.move(dragBox.x+dragBox.width/2,dragBox.y+dragBox.height/2+60,{steps:6});await page.mouse.up();
    assert.equal(await handle.getAttribute('aria-valuenow'),'380');
    assert.ok(Math.abs((await geometry()).dock.w-beforeResize.dock.w)<1,'vertical dragging leaves width unchanged');
    await handle.focus();await page.keyboard.press('ArrowUp');
    assert.equal(await handle.getAttribute('aria-valuenow'),'360');
    assert.equal(await page.evaluate(()=>localStorage.getItem('ag:focus-thread-height:v1')),'360');
    for(let i=0;i<20;i++)await page.keyboard.press('ArrowUp');
    assert.equal(await handle.getAttribute('aria-valuenow'),'180','minimum clamp');
    for(let i=0;i<40;i++)await page.keyboard.press('ArrowDown');
    assert.equal(await handle.getAttribute('aria-valuenow'),await handle.getAttribute('aria-valuemax'),'maximum clamp');
    await page.setViewportSize({width:1300,height:500});
    await page.waitForFunction(()=>{const dock=document.querySelector('.agfocus-dock').getBoundingClientRect(),body=document.querySelector('#agpad-body').getBoundingClientRect(),send=document.querySelector('.agfocus-dock .gchat-send').getBoundingClientRect();return dock.bottom<=Math.min(innerHeight,body.bottom) && send.bottom<=dock.bottom;});
    await page.screenshot({path:'/tmp/qorqut-focus-thread-short.png'});
    const shortHeight=Number(await handle.getAttribute('aria-valuenow'));
    assert.ok(shortHeight>=180 && shortHeight<380,'resize clamps to the short pane');
    await page.setViewportSize({width:1300,height:900});
    await handle.focus();await page.keyboard.press('ArrowUp');
    const persisted=await handle.getAttribute('aria-valuenow');
    await page.getByRole('tab',{name:'Agent inbox',exact:true}).click();await focusTab.click();
    await outreach.locator('.ttl').click();
    assert.equal(await handle.getAttribute('aria-valuenow'),persisted,'height restores after a real view remount');
    await page.locator('[data-focus-thread-close]').click();
    // Each channel keeps older history, expanded bodies and its reading position.
    const history=privateThread('task-a');
    for(let sequence=4;sequence<=33;sequence++)history.messages.push({id:'task-a-m'+sequence,sequence,summary:'Task history '+sequence,
      body:'Full task history message '+sequence+' contains canonical details that remain available when the short preview opens.',
      created_by:'chief',created_by_name:'Ordubashy',author_kind:'agent',is_owner:false,created_at:new Date().toISOString()});
    await firstCard.locator('.ttl').click();
    await thread('task-a').getByRole('button',{name:'Load older messages',exact:true}).waitFor();
    await thread('task-a').getByRole('button',{name:'Load older messages',exact:true}).click();
    await page.waitForFunction(()=>_agpad.focusCards.get('task-a')._conversationHost._conversation.states.private.notes.length===33);
    assert.ok(privateGets.some(body=>body.source.id==='task-a' && body.before===14),'older history uses the existing before cursor');
    await thread('task-a').locator('.gchat-scroll').evaluate(el=>el.scrollTop=80);
    const readingTop=await thread('task-a').locator('.gchat-scroll').evaluate(el=>el.scrollTop);
    await outreach.locator('.ttl').click();await firstCard.locator('.ttl').click();
    assert.equal(await thread('task-a').locator('.gchat-scroll').evaluate(el=>el.scrollTop),readingTop,'switching rows restores the reading position');
    assert.equal(await thread('task-a').locator('.agfocus-request').innerText(),'Confirm the mailbox mapping.');
    await thread('task-a').getByRole('button',{name:'Mapping guide',exact:true}).click();
    assert.equal(await page.locator('.agfocus-detail-body').innerText(),'Canonical related text','related source context remains available');
    const readsWhileDetail=privateReads.length;
    await page.evaluate(()=>{_agpad.nextFocus=0;agfocusTick(_agpad);return _agpad.focusRequest;});
    assert.equal(privateReads.length,readsWhileDetail,'a hidden board never acknowledges private history');
    await page.getByRole('button',{name:'Back to Focus',exact:true}).click();
    await page.evaluate(()=>agfocusOpenItem(_agpad,{type:'reference',id:'generic-preview',reason:'This Focus reason is not a source description.'}));
    assert.equal(await page.locator('.agfocus-detail-body').innerText(),'No preview available.','generic preview never falls back to the Focus reason');
    await page.getByRole('button',{name:'Back to Focus',exact:true}).click();

    // Actual pointer and keyboard moves use canonical server order and keep thread state.
    const originalOrder=[...board.items],moveRow=page.locator('[data-card-id="ref-standalone"]');
    await page.evaluate(()=>{window.orderedHost=_agpad.focusCards.get('task-a')._conversationHost;});
    if(await firstCard.getAttribute('aria-expanded')!=='true')await firstCard.locator('.ttl').click();
    await thread('task-a').locator('textarea').fill('Draft follows the order');
    await page.waitForFunction(()=>!_agpad.focusCards.get('task-a')._conversationHost._conversation.states.private.loading);
    let readsBeforeMoves=privateReads.length;const sendsBeforeMoves=privateSends.length;
    const waitBoard=()=>page.waitForFunction(revision=>Number(document.querySelector('.agfocus').dataset.revision)===revision && !_agpad.focusOrderPending,board.revision);
    const assertOrder=async()=>assert.deepEqual(await page.locator('.agfocus-items > .agfocus-card').evaluateAll(rows=>rows.map(row=>row.dataset.cardId)),board.items.filter(item=>!item.archived && !item.deferred_until).map(item=>item.id));
    const dragTo=async(source,target,after=false,cancel=null)=>{
      await page.locator('#agpad-body .agfocus-layout').evaluate(el=>el.scrollTop=0);
      const grip=page.locator(`[data-card-id="${source}"] .agfocus-row`),from=await grip.boundingBox();
      const to=await page.locator(`[data-card-id="${target}"] .agfocus-row`).boundingBox();
      await page.mouse.move(from.x+from.width/2,from.y+from.height/2);await page.mouse.down();await page.waitForTimeout(140);
      const count=focusMoves.length;
      await page.mouse.move(from.x+from.width/2,to.y+(after?to.height-6:6),{steps:5});
      assert.equal(focusMoves.length,count,'crossing never writes');
      if(source!==target)assert.equal(await page.locator(`[data-card-id="${target}"]`).evaluate(el=>el.classList.contains('agfocus-drop-before') || el.classList.contains('agfocus-drop-after')),true);
      if(cancel==='escape')await page.keyboard.press('Escape');
      if(cancel==='pointer')await grip.dispatchEvent('pointercancel',{pointerId:1});
      await page.mouse.up();
    };
    const startMoves=focusMoves.length;
    await dragTo('ref-standalone','ref-standalone');assert.equal(focusMoves.length,startMoves,'self drop is inert');
    await dragTo('ref-standalone','task-a',false,'escape');assert.equal(focusMoves.length,startMoves,'Escape cancels');
    await dragTo('ref-standalone','task-a',false,'pointer');assert.equal(focusMoves.length,startMoves,'pointercancel cancels');
    await dragTo('ref-standalone','task-a');
    await until(()=>focusMoves.length===startMoves+1,'pointer reorder');await waitBoard();
    assert.equal(focusMoves.at(-1).before_id,'task-a');assert.equal(focusMoves.at(-1).item_id,'ref-standalone');
    assert.equal(board.items[0].id,'ref-standalone');await assertOrder();
    const atTop=focusMoves.length;await moveRow.focus();await page.keyboard.press('Alt+ArrowUp');assert.equal(focusMoves.length,atTop,'page boundary never implies an off-page move');
    assert.equal(await page.evaluate(()=>_agpad.focusSelected),'task-a','drop never toggles a row');
    // Shared safe links (main's renderer) inside the docked thread: a click or Enter belongs to the link alone.
    {
      const linked=privateThread('task-a'),sequence=linked.messages.at(-1).sequence+1;
      linked.messages.push({id:'task-a-m'+sequence,sequence,body:'Read [the pricing brief](https://example.test/safe-link-click) first',created_by:'chief',created_by_name:'Ordubashy',author_kind:'agent',author_role:'coordinator',is_owner:false,created_at:new Date().toISOString()});
      await page.evaluate(()=>_agpad.focusCards.get('task-a')._conversationHost._conversation.refresh());
      const link=thread('task-a').locator('a.safe-link',{hasText:'the pricing brief'});
      await link.waitFor();
      assert.equal(await link.getAttribute('target'),'_blank');
      const state=()=>page.evaluate(()=>{const host=_agpad.focusCards.get('task-a')._conversationHost;return {selected:_agpad.focusSelected,same:host===window.orderedHost,
        expanded:host._conversation.isExpanded(),draft:host.querySelector('textarea').value,mounted:!!document.querySelector('[data-focus-thread-id="task-a"] .gchat-scroll'),
        open:document.querySelectorAll('.agfocus-card.open,.agfocus-card[aria-expanded="true"]').length};});
      const linkBefore=await state(),linkSends=privateSends.length,linkMoves=focusMoves.length;
      assert.equal(linkBefore.draft,'Draft follows the order');assert.equal(linkBefore.same,true);assert.equal(linkBefore.mounted,true);
      let popup=page.waitForEvent('popup');await link.click();(await popup).close();
      popup=page.waitForEvent('popup');await link.focus();await page.keyboard.press('Enter');(await popup).close();
      assert.deepEqual(await state(),linkBefore,'a safe link never toggles, selects, collapses or rewrites the thread');
      assert.equal(privateSends.length,linkSends,'a safe link never sends');assert.equal(focusMoves.length,linkMoves,'a safe link never moves');
    }
    const incidental=await moveRow.locator('.agfocus-row').boundingBox();const beforeIncidental=focusMoves.length;
    await page.mouse.move(incidental.x+incidental.width/2,incidental.y+incidental.height/2);await page.mouse.down();await page.mouse.move(incidental.x+incidental.width/2,incidental.y+incidental.height/2+3);await page.mouse.up();assert.equal(focusMoves.length,beforeIncidental,'below-threshold gestures never write');
    assert.equal(await moveRow.getAttribute('aria-expanded'),'true','a quick row click opens its thread');
    await firstCard.locator('.ttl').click();
    await page.waitForFunction(()=>['task-a','ref-standalone'].every(id=>!_agpad.focusCards.get(id)._conversationHost._conversation.states.private.loading));readsBeforeMoves=privateReads.length;
    const beforeNoop=focusMoves.length;await dragTo('ref-standalone','task-a');
    assert.equal(focusMoves.length,beforeNoop,'an unchanged insertion is inert');
    const grip=moveRow;
    assert.match(await grip.getAttribute('aria-describedby'),/move-instructions/);
    await grip.focus();await page.keyboard.press('Alt+ArrowDown');
    await until(()=>focusMoves.length===beforeNoop+1,'keyboard reorder');await waitBoard();
    assert.equal(focusMoves.at(-1).after_id,'task-a');assert.equal(board.items[1].id,'ref-standalone');await assertOrder();
    assert.equal(await page.locator('[data-focus-announcement]').innerText(),'Pinned item moved.');
    await dragTo('ref-standalone','signoff-b',true);await until(()=>focusMoves.at(-1)?.after_id==='signoff-b','pointer after insertion');await waitBoard();await assertOrder();
    // A native touch gesture goes through the same captured pointer path.
    const touch=await page.context().newCDPSession(page),touchFrom=await grip.boundingBox(),touchTo=await firstCard.locator('.agfocus-row').boundingBox();
    await touch.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:touchFrom.x+touchFrom.width/2,y:touchFrom.y+touchFrom.height/2}]});
    await page.waitForTimeout(140);
    await touch.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:touchFrom.x+touchFrom.width/2,y:touchTo.y+6}]});
    await touch.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    await until(()=>focusMoves.at(-1)?.before_id==='task-a','touch reorder');await waitBoard();await touch.detach();await assertOrder();
    // One outstanding reorder disables every grip; no fresh-revision conflict replay.
    holdMove=true;releaseMove=null;await grip.focus();await page.keyboard.press('Alt+ArrowDown');
    await until(()=>!!releaseMove,'held move');
    assert.equal(await page.locator('.agfocus-items .agfocus-card[data-move-disabled="false"]').count(),0);
    const heldCount=focusMoves.length;await page.evaluate(()=>agfocusMove(_agpad,'story-c','task-a','before_id'));
    assert.equal(focusMoves.length,heldCount);releaseMove();await waitBoard();
    const beforeConflict=board.items.map(item=>item.id);moveFailure='revision_conflict';await grip.focus();await page.keyboard.press('Alt+ArrowUp');
    await page.locator('.agfocus-error').filter({hasText:'Could not move Pinned item.'}).waitFor();
    assert.deepEqual(board.items.map(item=>item.id),beforeConflict);await assertOrder();
    assert.equal(await page.locator('[data-focus-retry-move]').isVisible(),false,'conflicts never retry with a refreshed revision');
    loseMoveResponse=true;await grip.focus();await page.keyboard.press('Alt+ArrowUp');
    await page.getByRole('button',{name:'Retry same move',exact:true}).waitFor();const uncertainMove={...focusMoves.at(-1)};
    await page.getByRole('button',{name:'Retry same move',exact:true}).click();await waitBoard();
    assert.deepEqual(focusMoves.at(-1),uncertainMove,'uncertain moves reuse exact arguments and key');await assertOrder();
    const archived=standalone('archived-order','Archived order',{archived:true});board.items.push(archived);board.revision++;
    await page.locator('[data-focus-archived]').click();await waitBoard();
    assert.equal(await page.locator('[data-card-id="archived-order"]').getAttribute('data-move-disabled'),'true');
    assert.equal(await page.locator('[data-card-id="gone-1"] [data-focus-grip]').count(),0,'unavailable source has no actionable grip');
    await page.locator('[data-focus-archived]').click();board.items=board.items.filter(item=>item.id!=='archived-order');board.revision++;await page.evaluate(()=>agfocusRefresh(_agpad));
    assert.deepEqual(await page.evaluate(()=>({selected:_agpad.focusSelected,sameHost:_agpad.focusCards.get('task-a')._conversationHost===orderedHost})),{selected:'task-a',sameHost:true});
    assert.equal(await thread('task-a').locator('textarea').inputValue(),'Draft follows the order');
    assert.equal(privateReads.length,readsBeforeMoves,'reorder never acknowledges a private thread');
    assert.equal(privateSends.length,sendsBeforeMoves,'reorder never sends a draft');
    // Captured dragging scrolls near a pane edge; cancellation leaves all positions unchanged.
    board.items.push(...Array.from({length:15},(_,i)=>standalone('scroll-'+i,'Scroll '+i)));board.revision++;
    await page.evaluate(()=>agfocusRefresh(_agpad));await page.locator('#agpad-body .agfocus-layout').evaluate(el=>el.scrollTop=0);
    const edgeFrom=await grip.boundingBox(),scrollPane=await page.locator('#agpad-body .agfocus-layout').boundingBox(),edgeMoveCount=focusMoves.length;
    await page.mouse.move(edgeFrom.x+edgeFrom.width/2,edgeFrom.y+edgeFrom.height/2);await page.mouse.down();await page.waitForTimeout(140);
    await page.mouse.move(edgeFrom.x+edgeFrom.width/2,scrollPane.y+scrollPane.height-8,{steps:4});
    await page.waitForFunction(()=>document.querySelector('#agpad-body .agfocus-layout').scrollTop>0);
    await page.keyboard.press('Escape');await page.mouse.up();assert.equal(focusMoves.length,edgeMoveCount,'autoscroll alone never writes');
    board.items=originalOrder;board.revision++;
    await page.evaluate(()=>agfocusRefresh(_agpad));
    // Postponement edits only the Focus card, closes the dock and retains its source draft.
    const taskWritesBefore=requests.filter(path=>path.endsWith('/tasks_update')).length;
    const snooze=()=>page.locator('.agfocus-items [data-card-id="task-a"] [data-focus-snooze]');
    await page.locator('#agpad-body .agfocus-layout').evaluate(el=>el.scrollTop=0);
    await snooze().click();
    assert.equal(await firstCard.getAttribute('aria-expanded'),'true','picker does not toggle the row');
    const postponePopup=()=>page.getByRole('dialog',{name:'Postpone Pinned item',exact:true});
    assert.equal(await postponePopup().locator('input').count(),0);
    assert.deepEqual(await postponePopup().locator('[data-focus-snooze-preset]').allTextContents(),['1h','3h','Tomorrow','Day after tomorrow','Saturday','Monday','In a week','In 2 weeks']);
    holdEdit=true;releaseEdit=null;const hourStarted=Date.now();
    await postponePopup().getByRole('button',{name:'1h',exact:true}).click();await until(()=>!!releaseEdit,'held postpone');
    const hourEdit=focusEdits.at(-1);
    assert.equal(hourEdit.item_id,'task-a');assert.deepEqual(Object.keys(hourEdit.changes),['deferred_until']);
    assert.ok(Math.abs(Date.parse(hourEdit.changes.deferred_until)-hourStarted-3600000)<2000);
    assert.equal(await snooze().isDisabled(),true);
    assert.equal(await firstCard.getAttribute('data-move-disabled'),'true','pending row cannot drag');
    assert.equal(await postponePopup().locator('button:enabled,input:enabled').count(),0);
    releaseEdit();await page.waitForFunction(()=>!_agpad.focusSnoozes.get('task-a')?.pending && !_agpad.focusSelected);
    assert.equal(await page.locator('.agfocus-items [data-card-id="task-a"]').count(),0);
    assert.equal(await page.locator('.agfocus-dock').isVisible(),false);
    assert.equal(await thread('task-a').locator('textarea').inputValue(),'Draft follows the order');
    await page.locator('[data-focus-deferred]').click();
    await snooze().waitFor();assert.equal(await firstCard.getAttribute('aria-label'),'Mailbox setup, postponed');
    await firstCard.locator('.ttl').click();
    assert.equal(await thread('task-a').locator('textarea').inputValue(),'Draft follows the order');
    const returnNow=async()=>{await snooze().click();await postponePopup().getByRole('button',{name:'Return to Focus now',exact:true}).click();await page.waitForFunction(()=>!_agpad.focusSnoozes.get('task-a') && !_agpad.focus.items.find(item=>item.id==='task-a')?.deferred_until);};
    await returnNow();assert.deepEqual(focusEdits.at(-1).changes,{deferred_until:null});
    assert.equal(await firstCard.getAttribute('aria-expanded'),'true');
    await snooze().click();const tomorrow=await page.evaluate(()=>agfocusSnoozeTime('tomorrow').toISOString());
    await postponePopup().getByRole('button',{name:'Tomorrow',exact:true}).click();
    await page.waitForFunction(()=>!_agpad.focusSnoozes.get('task-a'));
    assert.equal(focusEdits.at(-1).changes.deferred_until,tomorrow);
    await returnNow();
    await snooze().click();const threeHours=Date.now();
    await postponePopup().getByRole('button',{name:'3h',exact:true}).click();await page.waitForFunction(()=>!_agpad.focusSnoozes.get('task-a'));
    assert.ok(Math.abs(Date.parse(focusEdits.at(-1).changes.deferred_until)-threeHours-3*3600000)<2000);await returnNow();
    // A revision conflict leaves the active card and does not overwrite via retry.
    editFailure='revision_conflict';await snooze().click();await postponePopup().getByRole('button',{name:'1h',exact:true}).click();
    await page.locator('.agfocus-error').filter({hasText:'Could not postpone Pinned item.'}).waitFor();
    assert.equal(board.items.find(item=>item.id==='task-a').deferred_until,null);
    assert.equal(await postponePopup().getByRole('button',{name:'Retry same postpone',exact:true}).count(),0);
    await postponePopup().getByRole('button',{name:'Close',exact:true}).click();
    loseEditResponse=true;await snooze().click();await postponePopup().getByRole('button',{name:'1h',exact:true}).click();
    await postponePopup().getByRole('button',{name:'Retry same postpone',exact:true}).waitFor();const uncertainEdit=JSON.parse(JSON.stringify(focusEdits.at(-1)));
    await postponePopup().getByRole('button',{name:'Retry same postpone',exact:true}).click();await page.waitForFunction(()=>!_agpad.focusSnoozes.get('task-a'));
    assert.deepEqual(focusEdits.at(-1),uncertainEdit,'uncertain postpone retries frozen revision/time/key');await returnNow();
    // Existing server filtering returns an expired deferral on the next poll.
    await page.locator('[data-focus-deferred]').click();
    const taskCard=board.items.find(item=>item.id==='task-a');taskCard.deferred_until=new Date(Date.now()+60000).toISOString();taskCard.revision++;board.revision++;
    await page.evaluate(()=>agfocusRefresh(_agpad));assert.equal(await page.locator('.agfocus-items [data-card-id="task-a"]').count(),0);
    taskCard.deferred_until=new Date(Date.now()-1000).toISOString();board.revision++;
    await page.evaluate(()=>agfocusRefresh(_agpad));await snooze().waitFor();
    taskCard.deferred_until=null;board.revision++;await page.evaluate(()=>agfocusRefresh(_agpad));
    await firstCard.locator('.ttl').click();
    assert.equal(await thread('task-a').locator('textarea').inputValue(),'Draft follows the order');
    assert.equal(requests.filter(path=>path.endsWith('/tasks_update')).length,taskWritesBefore,'Focus snooze never writes task state');
    assert.equal(await page.locator('.agfocus .conversation-toggle,.agfocus-creator,.agfocus-framing h4').count(),0);


    // Pagination retains the keyed host off-page, then returns its original draft.
    await page.evaluate(()=>{window.paginationHost=_agpad.focusCards.get('task-a')._conversationHost;});
    extraPage=[standalone('page-two','Second page reference')];board.next_offset=50;board.revision++;
    await page.evaluate(()=>agfocusRefresh(_agpad));
    const readsBeforePages=privateReads.length;
    await page.getByRole('button',{name:'Next',exact:true}).click();
    await page.locator('.agfocus-items [data-card-id="page-two"]').waitFor();
    assert.equal(await page.locator('.agfocus-items > .agfocus-card').count(),1);
    assert.equal(await page.locator('.agfocus-dock').isVisible(),false);
    assert.equal(await thread('task-a').locator('textarea').inputValue(),'Draft follows the order');
    await page.getByRole('button',{name:'Previous',exact:true}).click();
    await page.locator('.agfocus-items [data-card-id="task-a"]').waitFor();
    assert.equal(await page.evaluate(()=>_agpad.focusCards.get('task-a')._conversationHost===paginationHost),true);
    assert.equal(privateReads.length,readsBeforePages,'hidden page mounts never acknowledge private threads');
    extraPage=null;delete board.next_offset;board.revision++;await page.evaluate(()=>agfocusRefresh(_agpad));

    // A private read finishing after selection changes can only update its own source.
    const staleStory=page.locator('[data-card-id="story-c"]');
    const storyAcks=privateReads.filter(body=>body.source.id==='story-c').length;
    holdPrivateId='story-c';releasePrivateRead=null;await staleStory.locator('.ttl').click();
    await until(()=>!!releasePrivateRead,'the delayed Story private read');
    await firstCard.locator('.ttl').click();releasePrivateRead();
    await page.waitForFunction(()=>!_agpad.focusCards.get('story-c')._conversationHost._conversation.states.private.loading);
    assert.equal(privateReads.filter(body=>body.source.id==='story-c').length,storyAcks,'switching away suppresses the delayed acknowledgment');
    assert.equal(await page.locator('.agfocus-dock .gchat-scroll').innerText().then(text=>text.includes('Private note about story-c')),false,'a stale response does not replace the selected source');

    // Source rebinding disposes the old mount, so its late response cannot paint or acknowledge.
    const rebound=board.items.find(item=>item.id==='ref-unknown'),reboundRow=page.locator('[data-card-id="ref-unknown"]');
    holdPrivateId='ref-unknown';releasePrivateRead=null;await reboundRow.locator('.ttl').click();
    await until(()=>!!releasePrivateRead,'the delayed reference read');
    await page.evaluate(()=>{window.staleView=_agpad.focusCards.get('ref-unknown')._conversationHost._conversation;});
    const oldReferenceAcks=privateReads.filter(body=>body.source.id==='ref-unknown').length;
    rebound.conversation={...rebound.conversation,id:'rebound-reference'};board.revision++;
    await page.evaluate(()=>agfocusRefresh(_agpad));
    await thread('ref-unknown').getByText('Private note about rebound-reference',{exact:true}).waitFor();
    releasePrivateRead();await page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,50)));
    assert.equal(privateReads.filter(body=>body.source.id==='ref-unknown').length,oldReferenceAcks);
    assert.equal(await thread('ref-unknown').locator('.gchat-scroll').innerText().then(text=>text.includes('Private note about ref-unknown')),false);
    assert.equal(await page.evaluate(()=>Object.values(staleView.states).every(s=>!s.mounts.has(_agpad.focusCards.get('ref-unknown')._conversationHost))),true,'old channel mount memberships are removed');

    // Access revocation clears inaccessible content and disables the old composer.
    await page.evaluate(()=>{window.inaccessibleInput=_agpad.focusCards.get('ref-unknown')._conversationHost.querySelector('textarea');window.inaccessibleView=_agpad.focusCards.get('ref-unknown')._conversationHost._conversation;});
    forbiddenPrivateId='rebound-reference';
    await page.evaluate(()=>_agpad.focusCards.get('ref-unknown')._conversationHost._conversation.refresh());
    assert.equal(await page.locator('.agfocus-dock').isVisible(),false);
    assert.equal(await reboundRow.locator('.ttl').innerText(),'This item is no longer available.');
    assert.equal(await reboundRow.getAttribute('aria-label'),'Unavailable Pinned item','an inaccessible row does not retain the old accessible name');
    assert.equal(await thread('ref-unknown').locator('.gchat-scroll,textarea,.gchat-send').count(),0);
    assert.equal(await page.evaluate(()=>inaccessibleInput.disabled && !inaccessibleInput.isConnected),true);
    forbiddenPrivateId=null;rebound.conversation={...rebound.conversation,id:'ref-unknown'};board.revision++;
    await page.evaluate(()=>agfocusRefresh(_agpad));
    await reboundRow.locator('.agfocus-row').waitFor({state:'visible'});

    // A pending send survives switching and refresh; uncertain delivery retries the same request.
    const pendingRow=page.locator('[data-card-id="ref-standalone"]'),pendingThread=thread('ref-standalone');
    await pendingRow.locator('.ttl').click();await pendingThread.locator('textarea').fill('Retained pending private send');
    holdPrivateSendId='ref-standalone';releasePrivateSend=null;losePrivateSend=true;
    await pendingThread.getByRole('button',{name:'Send',exact:true}).click();await until(()=>!!releasePrivateSend,'the held private send');
    const pendingSendCount=privateSends.length;
    await pendingRow.locator('[data-focus-snooze]').click();await postponePopup().getByRole('button',{name:'1h',exact:true}).click();
    await page.waitForFunction(()=>!_agpad.focusSnoozes.get('ref-standalone')?.pending && !_agpad.focusSelected);
    assert.equal(await page.locator('.agfocus-items [data-card-id="ref-standalone"]').count(),0,'snoozing removes a selected pending-send row');
    assert.equal(await pendingThread.locator('.gchat-send').isDisabled(),true,'snooze retains the pending send');
    assert.equal(privateSends.length,pendingSendCount,'snooze never duplicates a send');
    await page.locator('[data-focus-deferred]').click();await pendingRow.locator('.ttl').click();
    await firstCard.locator('.ttl').click();await page.evaluate(()=>agfocusRefresh(_agpad));
    assert.equal(await pendingThread.locator('textarea').inputValue(),'Retained pending private send');
    assert.equal(await pendingThread.locator('.gchat-send').isDisabled(),true,'the retained pending send stays disabled while hidden');
    await pendingRow.locator('.ttl').click();releasePrivateSend();
    await pendingThread.getByRole('button',{name:'Retry Send',exact:true}).waitFor();
    await page.waitForFunction(()=>!_agpad.focusCards.get('ref-standalone')._conversationHost._conversation.states.private.sending);
    const uncertain=privateSends.at(-1);
    await firstCard.locator('.ttl').click();await pendingRow.locator('.ttl').click();
    await pendingThread.getByRole('button',{name:'Retry Send',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.agfocus-dock textarea')?.value==='');
    assert.deepEqual(privateSends.at(-1),uncertain,'the retry reuses the exact private payload and idempotency key');
    assert.equal(privateThread('ref-standalone').messages.filter(message=>message.body==='Retained pending private send').length,1);
    assert.equal(await pendingRow.getAttribute('aria-expanded'),'true','Send never dismisses the dock');
    await pendingRow.locator('[data-focus-snooze]').click();await postponePopup().getByRole('button',{name:'Return to Focus now',exact:true}).click();await page.waitForFunction(()=>!_agpad.focusSnoozes.get('ref-standalone'));
    await page.locator('[data-focus-deferred]').click();

    // The opt-in adapter leaves every other inline caller's outside-click default intact.
    await page.evaluate(()=>{const host=document.createElement('div');host.id='outside-probe';document.body.append(host);window.outsideView=conversationMount(host,{type:'reference',id:'outside-probe',biz:null},{inlineComposer:true});outsideView.expand();});
    await page.locator('.agfocus-heading .global-eyebrow').click();
    assert.equal(await page.evaluate(()=>outsideView.isExpanded()),false);
    assert.equal(await pendingRow.getAttribute('aria-expanded'),'true');
    await page.evaluate(()=>{outsideView.dispose();document.querySelector('#outside-probe').remove();});

    // Actual Focus teardown removes retained mounts and resize listeners while shared drafts stay.
    await page.evaluate(()=>{window.retainedMounts=[..._agpad.focusCards.values()].map(node=>node._conversationHost).filter(host=>host?._conversation);window.retainedViews=retainedMounts.map(host=>host._conversation);window.oldResize=document.querySelector('.agfocus-thread-resize');window.savedHeight=localStorage.getItem('ag:focus-thread-height:v1');});
    await page.getByRole('tab',{name:'Agent inbox',exact:true}).click();
    assert.equal(await page.evaluate(()=>retainedMounts.every((host,i)=>!host._conversation && Object.values(retainedViews[i].states).every(s=>!s.mounts.has(host)))),true);
    assert.equal(await page.evaluate(()=>{oldResize.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp'}));return localStorage.getItem('ag:focus-thread-height:v1')===savedHeight;}),true,'the retired handle cannot resize the next view');
    await focusTab.click();
    await firstCard.locator('.ttl').click();
    assert.equal(await thread('task-a').locator('textarea').inputValue(),'Draft follows the order','shared source drafts survive teardown');
    await page.locator('[data-focus-thread-close]').click();

    // A failed board read from a disposed mount cannot install an error on its replacement.
    holdFocusRead=true;releaseFocusRead=null;
    await page.evaluate(()=>{window.oldBoardRead=agfocusRefresh(_agpad);});
    await until(()=>!!releaseFocusRead,'the old board request');
    await page.getByRole('tab',{name:'Agent inbox',exact:true}).click();await focusTab.click();
    failFocusRead=true;releaseFocusRead();await page.evaluate(()=>oldBoardRead);
    assert.equal(await page.evaluate(()=>_agpad.focusError), '');
    assert.equal(await page.locator('.agfocus-error').isVisible(),false);

    assert.deepEqual(await page.evaluate(()=>taskOpens),[],'row, composer and conversation clicks never open the task drawer');

    // ---- screenshots: collapsed rows and the right dock, normal and narrow panes ----
    const closeThread=async()=>{if(await page.locator('.agfocus-dock').isVisible())await page.locator('[data-focus-thread-close]').click();};
    const openTaskRow=async()=>{await closeThread();await page.locator('[data-card-id="task-a"] .ttl').click();await thread('task-a').locator('.conversation-panel').waitFor();};
    await page.locator('#agpad-body .agfocus-layout').evaluate(el=>el.scrollTop=0);
    await page.screenshot({path:'/tmp/qorqut-focus-rows.png'});
    await openTaskRow();
    const screenshotHandle=page.getByRole('separator',{name:'Thread height',exact:true}),screenshotBox=await screenshotHandle.boundingBox(),screenshotHeight=Number(await screenshotHandle.getAttribute('aria-valuenow'));
    await page.mouse.move(screenshotBox.x+screenshotBox.width/2,screenshotBox.y+screenshotBox.height/2);await page.mouse.down();
    await page.mouse.move(screenshotBox.x+screenshotBox.width/2,screenshotBox.y+screenshotBox.height/2+320-screenshotHeight);await page.mouse.up();
    await page.screenshot({path:'/tmp/qorqut-focus-expanded.png'});
    const normalOpen=await geometry();
    await page.locator('#agpad-body').evaluate(el=>el.scrollTop=1000);
    await page.waitForFunction(()=>{const dock=document.querySelector('.agfocus-dock').getBoundingClientRect(),body=document.querySelector('#agpad-body').getBoundingClientRect();return dock.top>=body.top && dock.bottom<=body.bottom;});
    await page.locator('#agpad-body .agfocus-layout').evaluate(el=>el.scrollTop=0);
    assert.equal(await page.evaluate(()=>{const body=document.querySelector('#agpad-body');return body.scrollWidth<=body.clientWidth;}),true,'normal pane: no horizontal scrolling');
    await closeThread();
    await page.getByRole('button',{name:'Hide agent navigation',exact:true}).click();
    await page.setViewportSize({width:700,height:900});
    await page.waitForFunction(()=>document.querySelector('.agfocus').getBoundingClientRect().width<340);
    const narrowPane=await page.locator('.agfocus-items').evaluate(el=>el.getBoundingClientRect().width);
    assert.ok(narrowPane<330,'the pane is narrow: '+narrowPane);
    const narrowRows=await layout();
    for(const row of narrowRows.filter(row=>row.id!=='gone-1')) {
      assert.ok(row.row.h>=31.5 && row.row.h<=44,`narrow: ${row.id} stays compact (${row.row.h})`);
      assert.ok(row.actions.right<=row.row.right+0.5 && row.status.right<=row.scope.x+0.5 && row.scope.right<=row.title.x+0.5 && row.title.right<=row.actions.x+0.5,`narrow: ${row.id} keeps row order`);
      assert.equal(row.region,'none');
    }
    assert.equal(byId(narrowRows)['story-c'].chipClipped,true);
    assert.equal(await page.evaluate(()=>{const body=document.querySelector('#agpad-body');return document.documentElement.scrollWidth<=innerWidth && body.scrollWidth<=body.clientWidth;}),true,'narrow: no horizontal scrolling');
    await page.locator('#agpad-body .agfocus-layout').evaluate(el=>el.scrollTop=0);
    await page.screenshot({path:'/tmp/qorqut-focus-rows-narrow.png'});
    await openTaskRow();
    await page.screenshot({path:'/tmp/qorqut-focus-expanded-narrow.png'});
    const narrowOpen=await geometry();
    assert.ok(narrowOpen.dock.x>narrowOpen.list.x+narrowOpen.list.w && Math.abs(narrowOpen.dock.w-narrowOpen.list.w)<1,'a narrow pane still splits beside the list');
    assert.equal(narrowOpen.underList,false);assert.equal(narrowOpen.statusHitFits,true);assert.equal(narrowOpen.visible,1);
    assert.equal(await page.evaluate(()=>{const body=document.querySelector('#agpad-body');return document.documentElement.scrollWidth<=innerWidth && body.scrollWidth<=body.clientWidth;}),true,'narrow: no horizontal scrolling with the dock');
    await page.getByRole('separator',{name:'Thread height',exact:true}).focus();
    for(let i=0;i<10;i++)await page.keyboard.press('ArrowUp');
    await page.waitForFunction(()=>{const dock=document.querySelector('.agfocus-dock').getBoundingClientRect(),feed=document.querySelector('.agfocus-dock .gchat-scroll').getBoundingClientRect(),send=document.querySelector('.agfocus-dock .gchat-send').getBoundingClientRect();return dock.height===180 && feed.height>0 && send.bottom<=dock.bottom;});
    fs.writeFileSync('/tmp/qorqut-focus-geometry.json',JSON.stringify({normal:normalOpen,narrow:narrowOpen,shortHeight,narrowMinimumHeight:180},null,2));
    await closeThread();
    await page.locator('[data-card-id="story-c"] .agfocus-scope').click();
    assert.equal(await page.locator('[data-card-id="story-c"] .ttl').evaluate(el=>getComputedStyle(el).whiteSpace),'nowrap','selected rows retain a single-line headline');
    assert.equal(await page.locator('[data-focus-thread-title]').textContent(),board.items[3].display_name,'the thread retains the full heading');
    assert.equal(await page.locator('[data-card-id="story-c"] .ttl').textContent(),board.items[3].display_name);
    await closeThread();await page.setViewportSize({width:1300,height:900});
    assert.deepEqual(errors,[]);
    console.log('Focus: right dock, geometry, vertical resize/persistence, routing, unread selection, retained state, stale-response guards, retry, canonical reorder/pagination, source details and narrow/short layouts passed.');
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
