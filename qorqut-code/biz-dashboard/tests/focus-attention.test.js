'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

test('record notifications mount the exact thread in Focus and protect sends', async () => {
  let spec, source, disposed = false, expanded = false, guard, refreshed = 0;
  const conversation = {state:{sending:false}, expand(){expanded=true;}, dispose(){disposed=true;}};
  const context = {sessionStorage:{getItem(){return null;}}, document:{addEventListener(){},querySelectorAll(){return [];}},
    window:{focusExpansion:{async open(value){spec=value;return {};}}},
    loadGlobalAgentProfiles:async()=>{}, globalAgentProfile:()=>({role:{id:'ordubashy'}}),
    conversationMount(_host, value){source=value;return conversation;},
    api:async()=>{refreshed++;return {items:[]};}, coreData:x=>x,
    setTimeout:()=>0, clearTimeout(){}, Event:class {}, primaryAgent:()=>true};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/reply-notifications.js'),'utf8')+'\nthis.subject=replyNotifications;',context);
  await context.subject.open({kind:'thread',agent_id:'agent-1',agent_name:'A',biz:'project',source:{type:'task',id:'task-1',business_id:'biz-1'}});
  assert.equal(spec.agentId,'agent-1');
  const dispose=spec.mount({}, {setBeforeLeave(fn){guard=fn;}});
  assert.deepEqual({...source},{type:'task',id:'task-1',biz:'project',business_id:'biz-1'});
  assert.equal(expanded,true);
  assert.equal(await guard(),true);
  conversation.state.sending=true;
  assert.equal(await guard(),false);
  dispose(); assert.equal(disposed,true); assert.equal(refreshed,1);
});

test('reminder opening only reads and dismissal uses the exact due occurrence', async () => {
  class Node {
    constructor(tag){this.tag=tag;this.children=[];this.isConnected=true;this.dataset={};}
    append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
    contains(node){return node === this || this.children.some(child=>child.contains?.(node));}
    remove(){this.isConnected=false;} setAttribute(){} getClientRects(){return [];}
  }
  const calls=[], reminder={reminder_id:'rem-1',revision:7,state:'due',occurrence_id:'occ-2',text:'Test',recurrence:{schedule:'0 9 * * *',timezone:'UTC'}};
  let finishDismiss;
  const context={document:{createElement:tag=>new Node(tag),dispatchEvent(){},addEventListener(){},removeEventListener(){}},window:{addEventListener(){},removeEventListener(){}},Event:class{},loadSession:async()=>({principal_id:'owner',principal_kind:'human',is_owner:true}),embeddedAgentPane:true,state:{session:{principal_id:'owner'},bizs:[]},
    post:async (_url,args)=>{calls.push(args); if(args.occurrence_id) return await new Promise(resolve=>{finishDismiss=resolve;}); return {reminder};},
    coreData:x=>x, accessLost:()=>false,sourceGone:()=>false,uiFailure:()=>'',newRequestId:()=> 'key-1',Intl,Date};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/reminders.js'),'utf8')+'\nthis.mount=remindersDetailMount;',context);
  const host=new Node('div'), view=context.mount(host,reminder);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.length,1); assert.equal(calls[0].reminder_id,'rem-1');
  const find=(node,text)=>node.textContent===text ? node : node.children.map(child=>find(child,text)).find(Boolean);
  find(host,'Dismiss').onclick();
  assert.equal(view.isBusy(),true);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls[1].occurrence_id,'occ-2'); assert.equal(calls[1].revision,7);
  finishDismiss({reminder:{...reminder,state:'dismissed'}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(view.isBusy(),false); view.dispose();
});

test('Focus signoff cancellation requires confirmation and revalidates its identity', async () => {
  const source=fs.readFileSync(path.join(__dirname,'../js/signoffs.js'),'utf8');
  const command=source.slice(source.indexOf('  async function executionCommand(action)'),source.indexOf('  function mount()',source.indexOf('  async function executionCommand(action)')));
  let calls=0, resolveConfirm;
  const item={id:'s1',status_revision:4,proposal_revision:2,payload_hash:'hash',review_business_id:'b1'};
  const sg={items:new Map([['s1',item]]),selected:'s1',slug:'project',verified:new Map([['s1',4]]),detailErrors:new Map(),executions:new Map(),error:null};
  const options={execution:true,confirmExecutionCancel:()=>new Promise(resolve=>{resolveConfirm=resolve;})};
  const execute=new Function('sg','options','active','owner','workspace','binding','newRequestId','post','coreData','loadDetail','sgViews','render','ownedWrites',`${command}; return executionCommand;`)(sg,options,()=>true,()=> 'owner',()=>sg.slug,s=>`${s.id}:${s.proposal_revision}:${s.payload_hash}`,()=> 'key',async()=>{calls++;return {};},x=>x,async()=>{},[],()=>{},new Set());
  let pending=execute('cancel-execution'); resolveConfirm(false); await pending; assert.equal(calls,0);
  pending=execute('cancel-execution'); item.status_revision=5; resolveConfirm(true); await pending; assert.equal(calls,0);
  item.status_revision=4; pending=execute('cancel-execution'); resolveConfirm(true); await pending; assert.equal(calls,1);
});

test('signoff pending-write check is scoped to content and includes uncertain receipts', () => {
  const source=fs.readFileSync(path.join(__dirname,'../js/signoffs.js'),'utf8');
  const start=source.indexOf('    hasPendingWrite: () => {'), end=source.indexOf('\n    async save()',start);
  const busyKeySource=source.match(/  const busyKey = (.*);/)[1];
  const factory=new Function('sg','messageEdits','options','ownedWrites','reviewEdits','sgIsQuestionnaire','owner','binding',`const busyKey=${busyKeySource}; return {${source.slice(start,end)}}`);
  const item=id=>({id,proposal_revision:2,payload_hash:'hash'});
  const sg={slug:'a',selected:'one',items:new Map([['one',item('one')],['other',item('other')]]),busy:new Map(),executions:new Map()};
  const edits=new Map(), reviews=new Map(), view=factory(sg,edits,{},new Set(),reviews,()=>false,()=> 'owner',s=>`${s.id}:${s.proposal_revision}:${s.payload_hash}`);
  sg.busy.set('b:one:2:hash',{unknown:true}); assert.equal(view.hasPendingWrite(),false);
  sg.busy.set('a:other:2:hash',{unknown:true}); assert.equal(view.hasPendingWrite(),false);
  sg.busy.set('a:one:2:hash',{unknown:true}); assert.equal(view.hasPendingWrite(),true);
  sg.busy.clear(); sg.executions.set('a:one:execution',{unknown:true}); assert.equal(view.hasPendingWrite(),true);
  sg.executions.clear(); edits.set('one',{receipt:{}}); assert.equal(view.hasPendingWrite(),true);
  edits.clear();reviews.set('one',{receipt:{}});assert.equal(view.hasPendingWrite(),true);
});

test('Focus signoff host guards drafts and writes and keeps cancellation confirmation inline', async () => {
  class Node { constructor(){this.children=[];this.isConnected=true;this.hidden=false;} append(...n){this.children.push(...n);} replaceChildren(...n){this.children=n;} setAttribute(){} focus(){} }
  let spec, guard, viewOptions, dirty=false, pending=false, disposed=false;
  const context={window:{focusExpansion:{open(s){spec=s;return Promise.resolve({});}}},document:{createElement:()=>new Node()},embeddedAgentPane:true,
    sgCreateView(options){viewOptions=options;return {paint(){},dispose(){disposed=true;},hasUnsaved:()=>dirty,hasPendingWrite:()=>pending};}};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/signoff-attention.js'),'utf8'),context);
  await context.window.sgFocusOpen('Signoff',{workspace:()=> 'project'});
  const host=new Node(),dispose=spec.mount(host,{isCurrent:()=>true,setBeforeLeave(fn){guard=fn;}});
  assert.equal(host.children[2].className,'sg-focus');assert.equal(viewOptions.focusLayout,true);
  dirty=true; assert.equal(await guard(),false); dirty=false;
  pending=true; assert.equal(await guard(),false); pending=false; assert.equal(await guard(),true);
  const answer=viewOptions.confirmExecutionCancel(); assert.equal(await guard(),false);
  host.children[1].children[1].onclick(); assert.equal(await answer,false);
  const answer2=viewOptions.confirmExecutionCancel(); host.children[1].children[2].onclick(); assert.equal(await answer2,true);
  dispose(); assert.equal(disposed,true);
});

test('attention overflow opens without acknowledging unrelated unread entries', async () => {
  const handlers={}, calls=[], rows=Array.from({length:7},(_,i)=>({id:'id-'+i,kind:'global',agent_id:'a'+i,agent_name:'Agent',text:'message '+i,viewed_ids:['id-'+i]}));
  let spec;
  const host={isConnected:true,addEventListener(type,fn){handlers[type]=fn;},removeEventListener(){},innerHTML:''};
  const context={document:{addEventListener(){},hidden:false},window:{addEventListener(){},focusExpansion:{async open(s){spec=s;return {};}}},
    api:async()=>({items:rows}),post:async(_path,body)=>{calls.push(body);return {items:rows};},coreData:x=>x,
    setTimeout:()=>0,clearTimeout(){},esc:String,AbortController,URLSearchParams,Date,toast(){},accessLost:()=>false};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/attention-queue.js'),'utf8')+'\nthis.queue=attentionQueue;',context);
  const dispose=context.queue.mount(host); await new Promise(resolve=>setImmediate(resolve));
  const more={hasAttribute:name=>name==='data-attention-more',closest:()=>more};handlers.click({target:more});
  assert.equal(spec.title,'New attention');assert.equal(calls.length,0);
  const list={isConnected:true,innerHTML:'',classList:{add(){}},addEventListener(){},removeEventListener(){}};
  const cleanup=spec.mount(list);assert.equal(calls.length,0);cleanup();dispose();
});

test('reminder Focus opening and disposal do not dismiss the reminder', async () => {
  class Node { constructor(){this.children=[];this.isConnected=true;this.dataset={};} append(...n){this.children.push(...n);} replaceChildren(...n){this.children=n;} contains(node){return node===this || this.children.some(child=>child.contains?.(node));} remove(){this.isConnected=false;} setAttribute(){} getClientRects(){return [];} }
  const calls=[]; let spec, guard;
  const reminder={reminder_id:'r',revision:2,state:'due',occurrence_id:'o',text:'Due'};
  const context={document:{createElement:()=>new Node(),dispatchEvent(){},addEventListener(){},removeEventListener(){}},window:{focusExpansion:{async open(s){spec=s;return {};}},addEventListener(){},removeEventListener(){}},Event:class{},loadSession:async()=>({principal_id:'owner',principal_kind:'human',is_owner:true}),embeddedAgentPane:true,
    state:{session:{principal_id:'owner'},bizs:[]},post:async(_path,args)=>{calls.push(args);return {reminder};},coreData:x=>x,
    accessLost:()=>false,sourceGone:()=>false,uiFailure:()=>'',Intl,Date};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/reminders.js'),'utf8')+'\nthis.open=remindersOpenDetail;',context);
  await context.open(reminder);
  assert.equal(calls.length,0);
  const dispose=spec.mount(new Node(),{setTitle(){},setBeforeLeave(fn){guard=fn;}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(await guard(),true);assert.equal(calls.length,1);assert.equal(calls[0].reminder_id,'r');
  dispose();assert.equal(calls.length,1);
});

test('embedded task and project signoff links dispatch canonical identity to parent', async () => {
  const received=[];
  const context={window:{},parent:{async sgAttentionNavigate(...args){received.push(args);return 'parent-result';}},embeddedAgentPane:true};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/signoff-attention.js'),'utf8'),context);
  context.sgAttentionNavigate=context.window.sgAttentionNavigate;
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/task-progress.js'),'utf8'),context);
  assert.equal(await context.taskProgressOpenReference('project',{type:'signoff',id:'complete-1'}),'parent-result');
  assert.equal(await context.window.sgAttentionNavigate(null,'pending-2'),'parent-result');
  assert.deepEqual(received,[['project','complete-1'],[null,'pending-2']]);
  context.parent={};
  await assert.rejects(()=>context.taskProgressOpenReference('project',{type:'signoff',id:'missing'}),/Focus pane is unavailable/);
});

test('uncertain reminder dismissal remains retryable after disposal with identical request', async () => {
  class Node {constructor(){this.children=[];this.isConnected=true;this.dataset={};} append(...n){this.children.push(...n);} replaceChildren(...n){this.children=n;} contains(node){return node===this || this.children.some(child=>child.contains?.(node));} remove(){this.isConnected=false;} setAttribute(){} getClientRects(){return [];}}
  const reminder={reminder_id:'r',business_id:'b',revision:7,state:'due',occurrence_id:'occ-1',text:'Due'};
  const writes=[];let tries=0;
  const context={document:{createElement:()=>new Node(),dispatchEvent(){},addEventListener(){},removeEventListener(){}},window:{addEventListener(){},removeEventListener(){}},Event:class{},loadSession:async()=>({principal_id:'owner',principal_kind:'human',is_owner:true}),embeddedAgentPane:true,state:{session:{principal_id:'owner'},bizs:[{id:'b',slug:'project'}]},
    post:async(path,args)=>{if(path.endsWith('reminders_get'))return {reminder};writes.push({path,args:{...args}});if(!tries++)throw new Error('lost');return {reminder:{...reminder,state:'dismissed'}};},
    coreData:x=>x,accessLost:()=>false,sourceGone:()=>false,uiFailure:()=> 'unknown',newRequestId:()=> 'original-key',Intl,Date};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/reminders.js'),'utf8')+'\nthis.mount=remindersDetailMount;',context);
  const find=(node,text)=>node.textContent===text ? node : node.children.map(child=>find(child,text)).find(Boolean);
  let host=new Node(),view=context.mount(host,reminder);await new Promise(resolve=>setImmediate(resolve));
  find(host,'Dismiss').onclick();await new Promise(resolve=>setImmediate(resolve));view.dispose();
  host=new Node();view=context.mount(host,reminder);await new Promise(resolve=>setImmediate(resolve));assert.equal(writes.length,1);
  find(host,'Dismiss').onclick();await new Promise(resolve=>setImmediate(resolve));assert.equal(writes.length,1);
  find(host,'Retry same request').onclick();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(writes.length,2);assert.deepEqual(writes[1],writes[0]);assert.equal(writes[0].args.occurrence_id,'occ-1');view.dispose();
});

test('create and update uncertain requests retain scope and keys independently', async () => {
  const calls=[];let serial=0,fail=true;
  const context={document:{dispatchEvent(){}},Event:class{},embeddedAgentPane:true,state:{session:{principal_id:'owner'},bizs:[{id:'b',slug:'p'}]},
    post:async(path,args)=>{calls.push({path,args:{...args}});if(fail)throw new Error('lost');return {};},coreData:x=>x,newRequestId:()=>`key-${++serial}`,Intl,Date};
  context.loadSession=async()=>({principal_id:context.state.session.principal_id,principal_kind:'human',is_owner:true});
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/reminders.js'),'utf8')+'\nthis.make=remAttempt;this.submit=remSubmitAttempt;this.pending=remPending;',context);
  const create=await context.make(null,'p','editor','reminders_create',{text:'one',at:'time'});
  await assert.rejects(()=>context.submit(create));
  assert.equal(context.pending(context.state.session.principal_id,null,'p','editor').length,1);assert.equal(context.pending(context.state.session.principal_id,null,'q','editor').length,0);
  const different=await context.make(null,'p','editor','reminders_create',{text:'two',at:'time'});assert.notEqual(create.args.idempotency_key,different.args.idempotency_key);
  const reminder={reminder_id:'r',business_id:'b',occurrence_id:'o'};
  const update=await context.make(reminder,'p','editor','reminders_update',{reminder_id:'r',revision:2,text:'new'});
  await assert.rejects(()=>context.submit(update));assert.equal(context.pending(context.state.session.principal_id,reminder,'p','editor').length,1);
  context.state.session.principal_id='other';assert.equal(context.pending(context.state.session.principal_id,reminder,'p','editor').length,0);
  context.state.session.principal_id='owner';fail=false;await context.submit(context.pending(context.state.session.principal_id,null,'p','editor')[0]);
  assert.deepEqual(calls[2],calls[0]);assert.equal(context.pending(context.state.session.principal_id,null,'p','editor').length,0);
});

test('duplicate editor leave requests keep one discard prompt and do not discard the draft', async () => {
  const source=fs.readFileSync(path.join(__dirname,'../js/reminders.js'),'utf8');
  const start=source.indexOf('  const guard = async () => {',source.indexOf('function remindersEditor'));
  const end=source.indexOf("  confirmBox.querySelector('[data-stay]')",start);
  const box={hidden:true}, construct=new Function('confirmBox',`let busy=false,initial='original',pendingConfirm=null,pendingPromise=null; const snapshot=()=> 'edited'; ${source.slice(start,end)}; return {guard,answer,baseline:()=>initial};`);
  const state=construct(box),first=state.guard(),second=state.guard();
  assert.equal(box.hidden,false);assert.equal(state.baseline(),'original');
  state.answer(false);assert.equal(await first,false);assert.equal(await second,false);assert.equal(state.baseline(),'original');
});

test('reminder attempts require a fresh human owner and retain uncertain timeout and rate-limit keys', async () => {
  let session=null, failure={httpStatus:408,code:'timeout'}, key=0, writes=0;
  const context={document:{dispatchEvent(){}},Event:class{},embeddedAgentPane:true,state:{session:{principal_id:'stale'},bizs:[]},
    loadSession:async(force)=>{assert.equal(force,true);return session;},newRequestId:()=>String(++key),
    post:async()=>{writes++;throw failure;},coreData:x=>x,Intl,Date};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/reminders.js'),'utf8')+'\nthis.make=remAttempt;this.submit=remSubmitAttempt;this.pending=remPending;',context);
  await assert.rejects(()=>context.make(null,null,'editor','reminders_create',{text:'x'}));assert.equal(writes,0);
  session={principal_id:'owner',principal_kind:'human',is_owner:true};
  const a=await context.make(null,null,'editor','reminders_create',{text:'x'});
  const b=await context.make(null,null,'editor','reminders_create',{text:'x'});assert.notEqual(a.args.idempotency_key,b.args.idempotency_key);
  await assert.rejects(()=>context.submit(b));assert.equal(context.pending('owner',null,null,'editor').length,1);
  for(const problem of [{httpStatus:408,code:'timeout'},{httpStatus:429,code:'rate_limit'},{code:'request_timeout'},{code:'invalid_response'},{httpStatus:503,code:'server_error'},{}]){
    failure=problem;await assert.rejects(()=>context.submit(a));assert.equal(context.pending('owner',null,null,'editor').length,2);
  }
  session=null;await assert.rejects(()=>context.submit(a));assert.equal(context.pending('owner',null,null,'editor').length,2);
  session={principal_id:'other',principal_kind:'human',is_owner:true};await assert.rejects(()=>context.submit(a));
  assert.equal(context.pending('other',null,null,'editor').length,0);assert.equal(writes,7);
});

test('unconfirmed list recovers a disappeared reminder without writes on open', async () => {
  class Node {constructor(){this.children=[];this.isConnected=true;} append(...n){this.children.push(...n);} replaceChildren(...n){this.children=n;} setAttribute(){} }
  let spec,session={principal_id:'owner',principal_kind:'human',is_owner:true},fail=true,writes=[];
  const context={document:{createElement:()=>new Node(),dispatchEvent(){}},Event:class{},embeddedAgentPane:true,
    window:{focusExpansion:{async open(s){spec=s;return {}; }},addEventListener(){},removeEventListener(){}},
    state:{session:{principal_id:'owner'},bizs:[]},loadSession:async()=>session,newRequestId:()=> 'saved-key',
    post:async(path,args)=>{writes.push({path,args:{...args}});if(fail)throw new Error('lost');return {};},coreData:x=>x,Intl,Date};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/reminders.js'),'utf8')+'\nthis.make=remAttempt;this.submit=remSubmitAttempt;this.open=remindersOpenUnconfirmed;',context);
  const attempt=await context.make({reminder_id:'gone',occurrence_id:'occ',text:'Due'},null,'detail','reminders_dismiss',{reminder_id:'gone',revision:1,occurrence_id:'occ'});
  await assert.rejects(()=>context.submit(attempt));await context.open();
  let host=new Node(),dispose=spec.mount(host,{isCurrent:()=>true,setBeforeLeave(){}});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(writes.length,1);dispose();host=new Node();dispose=spec.mount(host,{isCurrent:()=>true,setBeforeLeave(){}});await new Promise(resolve=>setImmediate(resolve));
  const find=(node,text)=>node.textContent===text ? node : node.children.map(child=>find(child,text)).find(Boolean);
  assert.ok(find(host,'Retry same request'));
  session={principal_id:'other',principal_kind:'human',is_owner:true};
  await find(host,'Retry same request').onclick();assert.equal(writes.length,1);
  assert.equal(find(host,'Retry same request'),undefined);
  session={principal_id:'owner',principal_kind:'human',is_owner:true};dispose();host=new Node();
  dispose=spec.mount(host,{isCurrent:()=>true,setBeforeLeave(){}});await new Promise(resolve=>setImmediate(resolve));
  fail=false;await find(host,'Retry same request').onclick();
  assert.deepEqual(writes[1],writes[0]);dispose();
});

test('retrying an older editor request leaves a different current draft untouched', async () => {
  const source=fs.readFileSync(path.join(__dirname,'../js/reminders.js'),'utf8');
  const start=source.indexOf('  async function submitAttempt(attempt, isRetry = false)');
  const end=source.indexOf('  async function refreshPending()',start);
  const part=source.slice(start,end),message={hidden:true},button={disabled:false},form={inert:false,isConnected:true,querySelector:()=>message};
  let savedCalls=0;
  const make=new Function('el','form','opts','remSubmitAttempt','refreshPending','paintPending',`let busy=false,initial='original';const snapshot=()=> 'different draft';const editing=true;const remNormalize=x=>x;${part};return {submitAttempt,baseline:()=>initial};`);
  const view=make({querySelector:()=>button},form,{onSaved:()=>savedCalls++},async()=>({reminder:{id:'old'}}),async()=>{},()=>{});
  await view.submitAttempt({args:{text:'older'}},true);
  assert.equal(view.baseline(),'original');assert.equal(savedCalls,0);assert.match(message.textContent,/current draft is unchanged/);
});

test('Focus exposes recovery when the due reminder is gone and rechecks owner on entry', async () => {
  const listeners={}, attempts=new Map([['one',{owner:'owner'}]]);let opened=0, session='owner';
  const host={isConnected:true,innerHTML:'',hidden:false,addEventListener(type,fn){listeners[type]=fn;},removeEventListener(){}};
  const context={document:{addEventListener(){}},window:{addEventListener(){}},remAttempts:attempts,remVerifiedOwner:async()=>session,
    remindersOpenUnconfirmed:()=>{opened++;},remindersOwnerDue:async()=>[],api:async()=>({items:[]}),coreData:x=>x,
    esc:String,URLSearchParams,setTimeout:()=>0,clearTimeout(){},accessLost:()=>false,Date};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/attention-queue.js'),'utf8')+'\nthis.queue=attentionQueue;',context);
  const dispose=context.queue.mountSignoffs(host);await new Promise(resolve=>setImmediate(resolve));
  assert.match(host.innerHTML,/Unconfirmed reminder changes/);
  listeners.click({target:{closest:selector=>selector==='[data-reminder-recovery]'?{}:null}});assert.equal(opened,1);
  dispose();
});

test('ordinary reminder Create prompts for retry or deliberate separate creation after an uncertain result', async () => {
  class Node {
    constructor(){this.children=[];this.isConnected=true;this.hidden=false;this.disabled=false;}
    append(...n){this.children.push(...n);} replaceChildren(...n){this.children=n;} setAttribute(){} focus(){}
  }
  const makeHost=()=>{
    const host=new Node(),form=new Node(),message=new Node(),pending=new Node(),confirm=new Node(),save=new Node(),cancel=new Node(),stay=new Node(),discard=new Node();
    const fields=[['text','same'],['repeat','once'],['at','2099-01-01T10:00'],['weekday','0'],['time','09:00'],['cron',''],['timezone','UTC'],['critical',''],['linktype',''],['linkscope',''],['linkid','']].map(([name,value])=>({name,value,type:name==='critical'?'checkbox':'text',checked:false}));
    for(const f of fields)fields[f.name]=f;form.elements=fields;form.querySelector=()=>message;form.querySelectorAll=()=>[];
    confirm.querySelector=selector=>selector==='[data-stay]'?stay:discard;
    const nodes={'.rem-form':form,'.rem-editor-confirm':confirm,'.rem-editor-pending':pending,'.rem-editor-save':save,'.rem-editor-cancel':cancel};
    host.querySelector=selector=>nodes[selector];return {host,form,pending,save};
  };
  let key=0, fail=true, focusHandler;const writes=[];
  const context={document:{createElement:()=>new Node(),dispatchEvent(){}},Event:class{},embeddedAgentPane:true,
    window:{addEventListener(type,fn){if(type==='focus')focusHandler=fn;},removeEventListener(){}},state:{session:{principal_id:'owner'},bizs:[]},
    loadSession:async()=>({principal_id:'owner',principal_kind:'human',is_owner:true}),newRequestId:()=>`key-${++key}`,
    post:async(path,args)=>{writes.push({path,args:{...args}});if(fail)throw new Error('lost');return {reminder:{id:'created'}};},
    coreData:x=>x,esc:String,uiFailure:()=> 'retry',Intl,Date};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/reminders.js'),'utf8')+'\nthis.editor=remindersEditor;',context);
  const session={setTitle(){},setBeforeLeave(){},onDispose(){},isCurrent:()=>true};
  const flush=()=>new Promise(resolve=>setImmediate(resolve));
  let h=makeHost();context.editor(null,{host:h.host,session,onCancel(){},onSaved(){}});await flush();
  h.save.onclick();await flush();assert.equal(writes.length,1);
  h=makeHost();context.editor(null,{host:h.host,session,onCancel(){},onSaved(){}});await flush();assert.equal(writes.length,1);
  h.save.onclick();await flush();assert.equal(writes.length,1);
  const find=(node,text)=>node.textContent===text ? node : node.children.map(child=>find(child,text)).find(Boolean);
  fail=false;find(h.pending,'Retry same request').onclick();await flush();assert.deepEqual(writes[1],writes[0]);
  fail=true;h.save.onclick();await flush();assert.equal(writes.length,3);
  h.save.onclick();await flush();assert.equal(writes.length,3);
  fail=false;find(h.pending,'Create separate').onclick();await flush();
  assert.equal(writes.length,4);assert.notEqual(writes[2].args.idempotency_key,writes[3].args.idempotency_key);
  assert.equal(writes[2].args.text,writes[3].args.text);
  let resolveWrite, saved=0;
  context.post=()=>new Promise(resolve=>{resolveWrite=resolve;});
  h=makeHost();h.form.elements.text.value='draft during pending';
  context.editor(null,{host:h.host,session,onCancel(){},onSaved(){saved++;}});await flush();
  h.save.onclick();await flush();assert.equal(h.form.inert,true);
  focusHandler();assert.equal(h.form.inert,true);assert.equal(h.form.elements.text.value,'draft during pending');
  resolveWrite({reminder:{id:'created'}});await flush();
  assert.equal(saved,1);assert.equal(h.form.inert,false);assert.equal(h.form.elements.text.value,'draft during pending');
});

test('ordinary dismissal of a different recurring occurrence uses a separate request', async () => {
  class Node {constructor(){this.children=[];this.isConnected=true;this.dataset={};} append(...n){this.children.push(...n);} replaceChildren(...n){this.children=n;} contains(node){return node===this || this.children.some(child=>child.contains?.(node));} remove(){this.isConnected=false;} setAttribute(){} getClientRects(){return [];}}
  let current={reminder_id:'r',revision:4,state:'due',occurrence_id:'one',text:'Repeat'}, key=0;const writes=[];
  const context={document:{createElement:()=>new Node(),dispatchEvent(){},addEventListener(){},removeEventListener(){}},window:{addEventListener(){},removeEventListener(){}},Event:class{},embeddedAgentPane:true,
    state:{session:{principal_id:'owner'},bizs:[]},loadSession:async()=>({principal_id:'owner',principal_kind:'human',is_owner:true}),newRequestId:()=>`key-${++key}`,
    post:async(path,args)=>{if(path.endsWith('reminders_get'))return {reminder:current};writes.push({...args});throw new Error('lost');},
    coreData:x=>x,accessLost:()=>false,sourceGone:()=>false,uiFailure:()=> 'retry',Intl,Date};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/reminders.js'),'utf8')+'\nthis.mount=remindersDetailMount;',context);
  const find=(n,t)=>n.textContent===t?n:n.children.map(c=>find(c,t)).find(Boolean),flush=()=>new Promise(resolve=>setImmediate(resolve));
  let host=new Node(),view=context.mount(host,current);await flush();find(host,'Dismiss').onclick();await flush();view.dispose();
  current={...current,occurrence_id:'two'};host=new Node();view=context.mount(host,current);await flush();
  find(host,'Dismiss').onclick();await flush();assert.equal(writes.length,2);
  assert.equal(writes[0].occurrence_id,'one');assert.equal(writes[1].occurrence_id,'two');assert.notEqual(writes[0].idempotency_key,writes[1].idempotency_key);view.dispose();
});

test('agent-to-agent communications advance the feed without owner popups', async () => {
  const notes=[], urls=[];
  const feed=[{event_id:5,sender_name:'A',recipient_name:'B',sender_id:'a',recipient_id:'b'}];
  const context={sessionStorage:{getItem(){return null;},setItem(){}}, document:{addEventListener(){},querySelectorAll(){return [];},dispatchEvent(){}},
    window:{}, loadGlobalAgentProfiles:async()=>{}, globalAgentProfile:()=>({role:{id:'ordubashy'}}), Event:class {},
    api:async url=>{urls.push(url);return {items:[],communications:{next_cursor:urls.length===1?1:5,items:urls.length===1?[]:feed}};},
    coreData:x=>x, notify:n=>notes.push(n), setTimeout:()=>0, clearTimeout(){}, primaryAgent:()=>true};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/reply-notifications.js'),'utf8')+'\nthis.subject=replyNotifications;',context);
  await context.subject.refresh();
  await context.subject.refresh();
  assert.equal(urls.length,2);
  assert.match(urls[1],/communications_after=1/);
  assert.deepEqual(notes,[]);
});
