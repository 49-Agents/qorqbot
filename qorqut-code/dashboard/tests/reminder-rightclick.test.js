'use strict';
const {test}=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm'), path=require('node:path');
const source=name=>fs.readFileSync(path.join(__dirname,'../js',name),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('right-click surfaces coalesce an occurrence and retain uncertain request for recovery',async()=>{
  let resolveWrite, rejectWrite, serial=0, refreshes=0, opened=0; const writes=[], listeners={}, timers=new Map(); let timerId=0;
  const reminder={reminder_id:'r',recipient_id:'owner',project_id:'b',revision:7,state:'due',occurrence_id:'one',text:'Repeat',recurrence:{schedule:'0 9 * * *'}};
  const context={document:{addEventListener(){},dispatchEvent(){},createElement:()=>new Node()},window:{addEventListener(){}},Event:class{},embeddedAgentPane:true,
    state:{session:{principal_id:'owner'},projects:[{id:'b',slug:'acme'}]},loadSession:async()=>({principal_id:'owner',principal_kind:'human',is_owner:true}),
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
  const toast=box.children[1], chip={dataset:{reminderKey:'r'},closest(s){return s==='[data-reminder-key]' || s==='[data-reminder-key],[data-signoff-key]'?this:null}};
  const event={target:chip,currentTarget:host,preventDefault(){this.prevented=true}};
  listeners.contextmenu(event);toast.listeners.contextmenu({preventDefault(){}});listeners.contextmenu(event);await tick();
  assert.equal(event.prevented,true);assert.equal(writes.length,1);assert.equal(writes[0].url,'/project/acme/operations/reminders_dismiss');assert.deepEqual(writes[0].args,{reminder_id:'r',revision:7,occurrence_id:'one',idempotency_key:'key-1'});
  rejectWrite(new Error('lost response'));await tick();assert.equal(context.attempts.size,1);assert.equal(box.children.includes(toast),true);
  await assert.rejects(context.dismissDue(reminder),/unconfirmed/);assert.equal(writes.length,1);
  const retry=context.submit([...context.attempts.values()][0]);await tick();assert.deepEqual(writes[1],writes[0]);resolveWrite({reminder:{...reminder,state:'scheduled',occurrence_id:null}});await retry;
  const next=context.dismissDue({...reminder,revision:8,occurrence_id:'two'});await tick();assert.equal(writes[2].args.occurrence_id,'two');assert.notEqual(writes[2].args.idempotency_key,'key-1');resolveWrite({reminder:{...reminder,state:'scheduled',occurrence_id:null}});await next;
  listeners.click({target:chip,currentTarget:host});assert.equal(opened,1);assert.equal(writes.length,3);
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
  let writes=0;const ctx={document:{dispatchEvent(){}},Event:class{},window:{},embeddedAgentPane:true,state:{session:{principal_id:'owner'},projects:[]},
    loadSession:async()=>({principal_id:'owner',principal_kind:'human',is_owner:true}),post:async()=>{writes++},coreData:x=>x,newRequestId:()=> 'key',Date,Intl};
  vm.createContext(ctx);vm.runInContext(source('reminders.js')+'\nthis.dismissDue=remindersDismissDue;',ctx);
  const r={reminder_id:'r',revision:1,state:'due',occurrence_id:'o',recipient_id:'owner'};
  await assert.rejects(ctx.dismissDue({...r,occurrence_id:null}),/occurrence/);
  await assert.rejects(ctx.dismissDue({...r,project_id:'missing'}),/project/);
  await assert.rejects(ctx.dismissDue({...r,recipient_id:'other'}),/owner/);assert.equal(writes,0);
});

test('every note has a close button that only closes it, and notes last 10 seconds',async()=>{
  class Node {constructor(){this.listeners={};this.style={setProperty(){}};this.classList={add(){},remove(){}};} addEventListener(n,f){this.listeners[n]=f} setAttribute(){} remove(){} }
  const box={children:[],appendChild(n){this.children.push(n)}},calls=[],timers=[];
  const ctx={document:{createElement:()=>new Node()},$:()=>box,esc:String,agentGlyph:()=>'',requestAnimationFrame(){},setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length},clearTimeout(){},Date,Promise};
  vm.createContext(ctx);const s=source('core.js');vm.runInContext(s.slice(s.indexOf('const NOTE_MAX'),s.indexOf('// ---------------------------------------------------------------- byte formatting'))+'\nthis.notify=notify;',ctx);
  ctx.notify({text:'reply',replyAgentId:'a',onClick:()=>calls.push('open')});ctx.notify({text:'plain'});
  for(const el of box.children){assert.match(el.innerHTML,/class="toast-x" aria-label="Close notification"/);assert.match(el.className,/has-x/);}
  assert.equal(timers[0].ms,10000-260);
  let stopped=false;const x={closest:sel=>sel==='.toast-x'?x:null};
  box.children[0].listeners.click({target:x,stopPropagation(){stopped=true}});await tick();
  assert.equal(stopped,true);assert.deepEqual(calls,[]);
  box.children[0].listeners.click({target:{closest:()=>null}});await tick();assert.deepEqual(calls,[]);
});

test('a note with actions shows them on their own line, stays until handled and keeps clicks apart', async () => {
  class Node {constructor(){this.listeners={};this.style={setProperty(){}};this.classList={add(){},remove(){}};} addEventListener(n,f){this.listeners[n]=f} setAttribute(){} remove(){} }
  const box={children:[],appendChild(n){this.children.push(n)}},calls=[],timers=[],toasts=[];
  const ctx={document:{createElement:()=>new Node()},$:()=>box,esc:String,agentGlyph:()=>'',requestAnimationFrame(){},setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length},clearTimeout(){},Date,Promise};
  vm.createContext(ctx);const s=source('core.js');vm.runInContext(s.slice(s.indexOf('const NOTE_MAX'),s.indexOf('// ---------------------------------------------------------------- byte formatting'))+'\nthis.notify=notify;',ctx);
  let fail=true;
  ctx.notify({text:'Text Fares.',sub:'Reminder · due now',reminder:true,onClick:()=>calls.push('open'),actions:[
    {label:'In 1 hour',run:()=>calls.push('hour')},{label:'Tomorrow',run:()=>calls.push('tomorrow')},{label:'Done',run:async()=>{if(fail)throw new Error('Could not dismiss');calls.push('done');}}]});
  const el=box.children[0];
  assert.match(el.className,/has-actions/);
  assert.match(el.innerHTML,/<span class="reply-name">Text Fares.<\/span><span class="reply-preview">Reminder · due now<\/span>/);
  assert.match(el.innerHTML,/<div class="toast-actions sg-body sg-chat"><div class="sg"><div class="sg-actions"><button type="button" class="sg-reject" data-note-action="0">In 1 hour<\/button><button type="button" class="sg-reject" data-note-action="1">Tomorrow<\/button><button type="button" class="sg-approve sg-primary-action" data-note-action="2">Done<\/button>/);
  assert.equal(timers.length,0);
  const button=index=>({dataset:{noteAction:String(index)}}),click=index=>{const b=button(index);let stopped=false;el.listeners.click({target:{closest:sel=>sel==='[data-note-action]'?b:null},stopPropagation(){stopped=true}});return stopped;};
  assert.equal(click(0),true);await tick();assert.deepEqual(calls,['hour']);
  // A failed action keeps its note and shows the error as a toast.
  ctx.notify({text:'Second',reminder:true,onClick:()=>calls.push('open2'),actions:[{label:'Done',run:async()=>{throw new Error('Could not dismiss');}}]});
  const second=box.children.at(-1),before=box.children.length;
  second.listeners.click({target:{closest:sel=>sel==='[data-note-action]'?{dataset:{noteAction:'0'}}:null},stopPropagation(){}});await tick();await tick();
  assert.equal(box.children.length,before+1);assert.match(box.children.at(-1).innerHTML,/Could not dismiss/);assert.deepEqual(calls,['hour']);
  // Clicking the card itself still opens the detail.
  second.listeners.click({target:{closest:()=>null}});await tick();assert.deepEqual(calls,['hour','open2']);
});

test('a due reminder note offers In 1 hour, Tomorrow and Done wired to snooze and dismiss', async () => {
  const writes=[],notes=[];
  const reminder={reminder_id:'r',recipient_id:'owner',revision:7,state:'due',occurrence_id:'one',text:'Text Fares.',due_at:new Date().toISOString()};
  const ctx={document:{addEventListener(){},dispatchEvent(){}},window:{addEventListener(){}},Event:class{},state:{session:{principal_id:'owner'},projects:[]},
    loadSession:async()=>({principal_id:'owner',principal_kind:'human',is_owner:true}),newRequestId:()=>`k${writes.length}`,coreData:x=>x,Date,Intl,Map,Set,JSON,Promise,
    setTimeout:()=>0,clearTimeout(){},notify:n=>notes.push(n),
    post:async(url,args)=>{if(url.endsWith('reminders_list'))return {reminders:[reminder]};writes.push({url,args:{...args}});return {reminder:{...reminder,state:'scheduled',occurrence_id:null}};}};
  vm.createContext(ctx);vm.runInContext(source('relative-time.js')+source('reminders.js'),ctx);
  await tick();await tick();await tick();
  assert.equal(notes.length,1);assert.equal(notes[0].text,'Text Fares.');assert.equal(notes[0].sub,'Reminder · due now');
  assert.deepEqual(Array.from(notes[0].actions,a=>a.label),['In 1 hour','Tomorrow','Done']);
  await notes[0].actions[0].run();assert.match(writes[0].url,/reminders_snooze$/);assert.equal(writes[0].args.occurrence_id,undefined);
  const hour=Date.parse(writes[0].args.at)-Date.now();assert.ok(hour>3500000&&hour<3700000);
  await notes[0].actions[1].run();assert.equal(new Date(writes[1].args.at).getHours(),9);
  await notes[0].actions[2].run();assert.match(writes[2].url,/reminders_dismiss$/);assert.equal(writes[2].args.occurrence_id,'one');
});
