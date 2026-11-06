'use strict';
const {test}=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm'), path=require('node:path');
const source=name=>fs.readFileSync(path.join(__dirname,'../js',name),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('right-click surfaces coalesce an occurrence and retain uncertain request for recovery',async()=>{
  let resolveWrite, rejectWrite, serial=0, refreshes=0, opened=0; const writes=[], listeners={}, timers=new Map(); let timerId=0;
  const reminder={reminder_id:'r',recipient_id:'owner',business_id:'b',revision:7,state:'due',occurrence_id:'one',text:'Repeat',recurrence:{schedule:'0 9 * * *'}};
  const context={document:{addEventListener(){},dispatchEvent(){},createElement:()=>new Node()},window:{addEventListener(){}},Event:class{},embeddedAgentPane:true,
    state:{session:{principal_id:'owner'},bizs:[{id:'b',slug:'project'}]},loadSession:async()=>({principal_id:'owner',principal_kind:'human',is_owner:true}),
    post:(url,args)=>{writes.push({url,args:{...args}});return new Promise((resolve,reject)=>{resolveWrite=resolve;rejectWrite=reject;});},coreData:x=>x,newRequestId:()=>`key-${++serial}`,
    api:async()=>({items:[]}),remindersOpenDetail:()=>{opened++},esc:String,URLSearchParams,Date,Intl,accessLost:()=>false,
    setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId},clearTimeout:id=>timers.delete(id),requestAnimationFrame(){},$ :()=>box,agentGlyph:()=>''};
  class Node {constructor(){this.listeners={};this.children=[];this.style={setProperty(){}};this.classList={add(){},remove(){}};this.isConnected=true;} addEventListener(n,f){this.listeners[n]=f} setAttribute(){} appendChild(n){n.parent=this;this.children.push(n)} remove(){this.parent.children=this.parent.children.filter(x=>x!==this)} }
  const box=new Node();vm.createContext(context);vm.runInContext(source('reminders.js')+'\nthis.dismissDue=remindersDismissDue;this.attempts=remAttempts;this.submit=remSubmitAttempt;',context);
  context.remindersOpenDetail=()=>{opened++};context.remindersOwnerDue=async()=>[reminder];
  const host={isConnected:true,innerHTML:'',addEventListener(n,f){listeners[n]=f},removeEventListener(n){delete listeners[n]},contains:()=>true};
  vm.runInContext(source('attention-queue.js')+'\nthis.queue=attentionQueue;',context);const dispose=context.queue.mountSignoffs(host);await tick();
  const src=source('core.js');vm.runInContext(src.slice(src.indexOf('const NOTE_MAX'),src.indexOf('// ---------------------------------------------------------------- byte formatting'))+'\nthis.notify=notify;',context);
  context.notify({text:'ordinary'});assert.equal(box.children[0].listeners.contextmenu,undefined);
  context.notify({text:'Due',reminder:true,onClick:()=>opened++,onDismiss:()=>context.dismissDue(reminder)});
  const toast=box.children[1], chip={dataset:{reminderKey:'r'},closest(s){return s==='[data-reminder-key]'?this:null}};
  const event={target:chip,currentTarget:host,preventDefault(){this.prevented=true}};
  listeners.contextmenu(event);toast.listeners.contextmenu({preventDefault(){}});listeners.contextmenu(event);await tick();
  assert.equal(event.prevented,true);assert.equal(writes.length,1);assert.equal(writes[0].url,'/biz/project/operations/reminders_dismiss');assert.deepEqual(writes[0].args,{reminder_id:'r',revision:7,occurrence_id:'one',idempotency_key:'key-1'});
  rejectWrite(new Error('lost response'));await tick();assert.equal(context.attempts.size,1);assert.equal(box.children.includes(toast),true);
  await assert.rejects(context.dismissDue(reminder),/unconfirmed/);assert.equal(writes.length,1);
  const retry=context.submit([...context.attempts.values()][0]);await tick();assert.deepEqual(writes[1],writes[0]);resolveWrite({reminder:{...reminder,state:'scheduled',occurrence_id:null}});await retry;
  const next=context.dismissDue({...reminder,revision:8,occurrence_id:'two'});await tick();assert.equal(writes[2].args.occurrence_id,'two');assert.notEqual(writes[2].args.idempotency_key,'key-1');resolveWrite({reminder:{...reminder,state:'scheduled',occurrence_id:null}});await next;
  listeners.click({target:chip});assert.equal(opened,1);assert.equal(writes.length,3);
  const ordinary={closest:()=>null};const unrelated={target:ordinary,currentTarget:host,preventDefault(){throw Error('unrelated menu intercepted')}};listeners.contextmenu(unrelated);
  dispose();assert.equal(listeners.contextmenu,undefined);
});

test('left-click opens reminder and unrelated reply has no context handler',async()=>{
  class Node {constructor(){this.listeners={};this.style={setProperty(){}};this.classList={add(){},remove(){}};} addEventListener(n,f){this.listeners[n]=f} setAttribute(){} }
  const box={children:[],appendChild(n){this.children.push(n)}},calls=[];
  const ctx={document:{createElement:()=>new Node()},$:()=>box,esc:String,agentGlyph:()=>'',requestAnimationFrame(){},setTimeout:()=>0,clearTimeout(){},Date,Promise};
  vm.createContext(ctx);const s=source('core.js');vm.runInContext(s.slice(s.indexOf('const NOTE_MAX'),s.indexOf('// ---------------------------------------------------------------- byte formatting'))+'\nthis.notify=notify;',ctx);
  ctx.notify({text:'due',reminder:true,onClick:()=>calls.push('open'),onDismiss:()=>calls.push('write')});ctx.notify({text:'reply',replyAgentId:'a',onClick:()=>calls.push('reply')});
  assert.equal(box.children[1].listeners.contextmenu,undefined);box.children[0].listeners.click();await tick();assert.deepEqual(calls,['open']);
});

test('incomplete, inaccessible and wrong-owner reminders cannot be dismissed',async()=>{
  let writes=0;const ctx={document:{dispatchEvent(){}},Event:class{},window:{},embeddedAgentPane:true,state:{session:{principal_id:'owner'},bizs:[]},
    loadSession:async()=>({principal_id:'owner',principal_kind:'human',is_owner:true}),post:async()=>{writes++},coreData:x=>x,newRequestId:()=> 'key',Date,Intl};
  vm.createContext(ctx);vm.runInContext(source('reminders.js')+'\nthis.dismissDue=remindersDismissDue;',ctx);
  const r={reminder_id:'r',revision:1,state:'due',occurrence_id:'o',recipient_id:'owner'};
  await assert.rejects(ctx.dismissDue({...r,occurrence_id:null}),/occurrence/);
  await assert.rejects(ctx.dismissDue({...r,business_id:'missing'}),/project/);
  await assert.rejects(ctx.dismissDue({...r,recipient_id:'other'}),/owner/);assert.equal(writes,0);
});
