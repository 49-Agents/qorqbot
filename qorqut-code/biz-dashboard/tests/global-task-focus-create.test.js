'use strict';
const {test}=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../js/global-tasks.js'),'utf8');
const tick=()=>new Promise(r=>setImmediate(r));
class Node {
  constructor(){this.children=[];this.isConnected=true;this.dataset={};this.hidden=false;this.disabled=false;}
  append(...nodes){this.children.push(...nodes);}
  remove(){this.isConnected=false;}
  setAttribute(){} focus(){} contains(){return true;}
  addEventListener(name,fn){this['on'+name]=fn;}
}
function harness(embedded=false){
  const calls=[], writes=[], pending=[], nodes={}, session={closed:0,setBeforeLeave(fn){this.leave=fn;},onDispose(fn){this.dispose=fn;},async close(){if(await this.leave()){this.closed++;this.dispose();return true;}return false;}};
  let spec, ids=0;
  const host=new Node(); host.firstElementChild=new Node();
  const fields={title:{value:''},due:{value:''},status:{value:'open'}};
  const form=new Node();form.elements=fields;
  const assignee={value:'owner',checked:true};
  const status=new Node(), back=new Node(), retry=new Node(), reload=new Node(), newButton=new Node();
  const selectors={'[data-editor]':form,'[data-global-task-status]':status,'[data-back]':back,'[data-retry]':retry,'[data-reload]':reload,'[data-new]':newButton,'[data-refresh]':new Node(),'[data-task-tags]':new Node()};
  host.querySelector=s=>selectors[s]||null;
  host.querySelectorAll=s=>s==='[name="assignee"]'||s==='[name="assignee"]:checked'?[assignee]:[];
  const ctx={window:{parent:{taskOpenGlobalCreate(){calls.push('parent');}},focusExpansion:{open(s){spec=s;s.mount(host,session);return session;}}},embeddedAgentPane:embedded,document:{activeElement:null,createElement:()=>new Node()},
    taskStateSubscribe:()=>()=>{}, taskStateObserve:(_,task)=>task, post:async(url,args)=>{calls.push([url,args]);if(url.endsWith('assignees_list'))return {assignees:[{id:'owner',is_owner:true}]};if(url.endsWith('tasks_create')){writes.push(args);return new Promise((resolve,reject)=>pending.push({resolve,reject}));}return {tasks:[]};},coreData:x=>x,loadGlobalAgentProfiles:async()=>[],esc:x=>String(x),T_STATES:['open'],T_LABEL:{open:'Open'},taskTypeFields:()=>'',taskSnoozePicker:()=>'',taskSnoozeWire(){},taskSnoozeChange:()=>undefined,taskTypeMount:()=>({values:()=>({task_type:'task',overview:''}),confirmConversion:async()=>true,dispose(){}}),tagPickerMount:()=>({labels:()=>[]}),tagNormalize:x=>x,taskShowsState:()=>true,newRequestId:()=>`id-${++ids}`,uiFailure:e=>e.message,refreshTasks:()=>{calls.push('refresh');return Promise.resolve();},toast(){},identityAvatar:()=>'',globalAgentProfile:()=>null,taskRowLayout:()=>'',taskStateSnapshot:()=>({}),taskStateBind(){},tClosed:()=>false};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  return {ctx,calls,writes,pending,host,fields,form,back,retry,newButton,session,get spec(){return spec;}};
}
async function opened(){const h=harness();h.ctx.window.taskOpenGlobalCreate();await tick();return h;}
test('normal and embedded entry routes only create intent',async()=>{
 const h=await opened();assert.equal(h.spec.title,'New Global task');assert.equal(h.writes.length,0);assert.equal(h.calls.filter(c=>Array.isArray(c)&&c[0].endsWith('tasks_list')).length,0);
 const e=harness(true);e.ctx.window.taskOpenGlobalCreate();assert.deepEqual(e.calls,['parent']);assert.equal(e.spec,undefined);
});
test('cancel clean and keep or discard dirty without writes',async()=>{
 let h=await opened();await h.session.close();assert.equal(h.session.closed,1);assert.equal(h.writes.length,0);
 h=await opened();h.fields.title.value='Draft';let closing=h.session.close();let prompt=h.host.children.at(-1);prompt.children[1].onclick();assert.equal(await closing,false);assert.equal(h.session.closed,0);
 closing=h.session.close();prompt=h.host.children.at(-1);prompt.children[2].onclick();assert.equal(await closing,true);assert.equal(h.writes.length,0);
});
test('create preserves payload and retry key; pending blocks close and unknown warns on discard',async()=>{
 const h=await opened();h.fields.title.value='  Example  ';h.fields.due.value='2027-01-01';h.form.onsubmit({preventDefault(){}});await tick();
 assert.deepEqual(JSON.parse(JSON.stringify(h.writes[0])),{title:'Example',due:'2027-01-01',assignee_ids:['owner'],tags:[],task_type:'task',overview:'',status:'open',id:'id-1',idempotency_key:'id-2'});
 assert.equal(await h.session.close(),false);h.pending[0].reject(Error('lost'));await tick();
 let closing=h.session.close();let prompt=h.host.children.at(-1);assert.match(prompt.children[0].textContent,/may have succeeded/);prompt.children[1].onclick();assert.equal(await closing,false);
 h.retry.onclick();await tick();assert.deepEqual(JSON.parse(JSON.stringify(h.writes[1])),JSON.parse(JSON.stringify(h.writes[0])));
 h.pending[1].resolve({task:{id:'id-1'}});await tick();await tick();assert.equal(h.session.closed,1);assert.ok(h.calls.includes('refresh'));
});

test('Global list New opens the parent entry point',async()=>{
 const h=harness();let invoked=0;h.ctx.window.taskOpenGlobalCreate=()=>{invoked++;};
 h.ctx.globalTasksMount(h.host);await tick();await tick();h.newButton.onclick();assert.equal(invoked,1);assert.equal(h.writes.length,0);
});
for (const outcome of ['malformed', 'wrong-id', 'timeout', 'rate-limit', 'coded-timeout']) test(`${outcome} create retains the exact request for retry`,async()=>{
 const h=await opened();h.fields.title.value='Draft';h.form.onsubmit({preventDefault(){}});await tick();
 if(outcome==='malformed')h.pending[0].resolve({task:null});
 else if(outcome==='wrong-id')h.pending[0].resolve({task:{id:'different'}});
 else h.pending[0].reject(Object.assign(Error(outcome),outcome==='coded-timeout'?{code:'request_timeout',httpStatus:400}:{httpStatus:outcome==='timeout'?408:429}));
 await tick();assert.equal(h.session.closed,0);assert.equal(h.calls.includes('refresh'),false);assert.equal(h.retry.hidden,false);
 h.retry.onclick();await tick();assert.deepEqual(JSON.parse(JSON.stringify(h.writes[1])),JSON.parse(JSON.stringify(h.writes[0])));
 h.pending[1].resolve({task:{id:'id-1'}});await tick();await tick();assert.equal(h.session.closed,1);
});
async function parentOpened({deferRefresh=false}={}){
 const h=harness(), c=harness(), add=new Node(), family=new Node();family.querySelectorAll=()=>[];
 const previous=h.host.querySelector;h.host.querySelector=s=>s==='[data-add-child]'?add:s==='[data-task-family]'?family:previous(s);
 h.fields.archived={checked:false};
 const task={id:'parent',title:'Parent',status:'open',assignee_ids:['owner'],tags:[],children:[],revision:1};
 let childHostUsed=false, reads=0, resolveRefresh;
 h.ctx.document.createElement=tag=>tag==='div'&&!childHostUsed?(childHostUsed=true,c.host):new Node();
 h.ctx.post=async(url,args)=>{h.calls.push([url,args]);if(url.endsWith('assignees_list'))return {assignees:[{id:'owner',is_owner:true}]};if(url.endsWith('tasks_get')){reads++;if(reads>1 && deferRefresh)return new Promise(resolve=>{resolveRefresh=resolve;});return {task:{...task,children:reads>1?[{id:'id-1',title:'Child',status:'open'}]:[]}};}if(url.endsWith('tasks_create')){h.writes.push(args);return new Promise((resolve,reject)=>h.pending.push({resolve,reject}));}return {tasks:[]};};
 h.ctx.tChipLabel=x=>x;h.ctx.customerStateChip=()=>'';h.ctx.taskTypeIcon=()=>'';
 const controller=h.ctx.globalTasksMount(h.host,{initialTaskId:'parent',embedded:true});await tick();await tick();
 h.fields.title.value='Parent draft';add.onclick();await tick();
 return {h,c,controller,add,family,get reads(){return reads;}, resolveRefresh: result=>resolveRefresh(result)};
}
test('child cancel guards draft and preserves parent editor without writes',async()=>{
 const {h,c,controller}=await parentOpened();assert.equal(h.writes.length,0);assert.equal(h.host.firstElementChild.hidden,true);
 c.fields.title.value='Child draft';assert.equal(controller.dirty(),true);
 let leaving=controller.beforeLeave();let prompt=c.host.children.at(-1);prompt.children[1].onclick();assert.equal(await leaving,false);assert.equal(h.host.firstElementChild.hidden,true);
 leaving=controller.beforeLeave();prompt=c.host.children.at(-1);prompt.children[2].onclick();assert.equal(await leaving,true);assert.equal(h.host.firstElementChild.hidden,false);assert.equal(h.fields.title.value,'Parent draft');assert.equal(h.writes.length,0);
});
test('child create retains parent identity and draft through unknown outcome and success',async()=>{
 const p=await parentOpened(),{h,c,controller}=p;c.fields.title.value='Child';c.form.onsubmit({preventDefault(){}});await tick();
 assert.equal(h.writes[0].parent_id,'parent');assert.equal(await controller.beforeLeave(),false);
 h.pending[0].reject(Error('lost'));await tick();c.retry.onclick();await tick();assert.deepEqual(JSON.parse(JSON.stringify(h.writes[1])),JSON.parse(JSON.stringify(h.writes[0])));
 h.pending[1].resolve({task:{id:'id-1',title:'Child'}});await tick();await tick();assert.equal(h.fields.title.value,'Parent draft');assert.equal(h.host.firstElementChild.hidden,false);assert.match(p.family.innerHTML,/Child/);assert.ok(p.reads>=2);
});

test('clean child Back restores parent without creating',async()=>{
 const {h,c}=await parentOpened();c.back.onclick();await tick();assert.equal(h.host.firstElementChild.hidden,false);assert.equal(h.fields.title.value,'Parent draft');assert.equal(h.writes.length,0);
});

test('confirmed child closes before slow relationship refresh and ignores late result after disposal',async()=>{
 const p=await parentOpened({deferRefresh:true}),{h,c,controller}=p;c.fields.title.value='Child';c.form.onsubmit({preventDefault(){}});await tick();
 h.pending[0].resolve({task:{id:'id-1',title:'Child'}});await tick();await tick();
 assert.equal(h.host.firstElementChild.hidden,false);assert.equal(h.fields.title.value,'Parent draft');assert.equal(await controller.beforeLeave(),true);assert.equal(p.reads,2);
 h.fields.title.value='Still editable';p.family.innerHTML='unchanged';controller.dispose();p.resolveRefresh({task:{id:'parent',children:[{id:'id-1',title:'Late child',status:'open'}]}});await tick();
 assert.equal(p.family.innerHTML,'unchanged');assert.equal(h.fields.title.value,'Still editable');assert.equal(h.writes.length,1);
});
