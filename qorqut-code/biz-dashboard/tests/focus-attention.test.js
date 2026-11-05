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
    remove(){this.isConnected=false;} setAttribute(){}
  }
  const calls=[], reminder={reminder_id:'rem-1',revision:7,state:'due',occurrence_id:'occ-2',text:'Test',recurrence:{schedule:'0 9 * * *',timezone:'UTC'}};
  let finishDismiss;
  const context={document:{createElement:tag=>new Node(tag)},embeddedAgentPane:true,state:{session:{},bizs:[]},
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
  const factory=new Function('sg','messageEdits','options','ownedWrites',`return {${source.slice(start,end)}}`);
  const sg={slug:'a',items:new Map([['one',{}]]),busy:new Map(),executions:new Map()};
  const edits=new Map(), view=factory(sg,edits,{},new Set());
  sg.busy.set('b:one:2:hash',{unknown:true}); assert.equal(view.hasPendingWrite(),false);
  sg.busy.set('a:other:2:hash',{unknown:true}); assert.equal(view.hasPendingWrite(),false);
  sg.busy.set('a:one:2:hash',{unknown:true}); assert.equal(view.hasPendingWrite(),true);
  sg.busy.clear(); sg.executions.set('a:one:execution',{unknown:true}); assert.equal(view.hasPendingWrite(),true);
  sg.executions.clear(); edits.set('one',{receipt:{}}); assert.equal(view.hasPendingWrite(),true);
});

test('Focus signoff host guards drafts and writes and keeps cancellation confirmation inline', async () => {
  class Node { constructor(){this.children=[];this.isConnected=true;this.hidden=false;} append(...n){this.children.push(...n);} replaceChildren(...n){this.children=n;} setAttribute(){} focus(){} }
  let spec, guard, viewOptions, dirty=false, pending=false, disposed=false;
  const context={window:{focusExpansion:{open(s){spec=s;return Promise.resolve({});}}},document:{createElement:()=>new Node()},embeddedAgentPane:true,
    sgCreateView(options){viewOptions=options;return {paint(){},dispose(){disposed=true;},hasUnsaved:()=>dirty,hasPendingWrite:()=>pending};}};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/signoff-attention.js'),'utf8'),context);
  await context.window.sgFocusOpen('Signoff',{workspace:()=> 'project'});
  const host=new Node(),dispose=spec.mount(host,{isCurrent:()=>true,setBeforeLeave(fn){guard=fn;}});
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
  class Node { constructor(){this.children=[];this.isConnected=true;} append(...n){this.children.push(...n);} replaceChildren(...n){this.children=n;} remove(){this.isConnected=false;} setAttribute(){} }
  const calls=[]; let spec, guard;
  const reminder={reminder_id:'r',revision:2,state:'due',occurrence_id:'o',text:'Due'};
  const context={document:{createElement:()=>new Node()},window:{focusExpansion:{async open(s){spec=s;return {};}}},embeddedAgentPane:true,
    state:{session:{},bizs:[]},post:async(_path,args)=>{calls.push(args);return {reminder};},coreData:x=>x,
    accessLost:()=>false,sourceGone:()=>false,uiFailure:()=>'',Intl,Date};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/reminders.js'),'utf8')+'\nthis.open=remindersOpenDetail;',context);
  await context.open(reminder);
  assert.equal(calls.length,0);
  const dispose=spec.mount(new Node(),{setTitle(){},setBeforeLeave(fn){guard=fn;}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(await guard(),true);assert.equal(calls.length,1);assert.equal(calls[0].reminder_id,'r');
  dispose();assert.equal(calls.length,1);
});
