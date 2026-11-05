'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../js/tasks.js'),'utf8');
const start=source.indexOf('function taskProjectCreateMount('),end=source.indexOf('function taskCreateDialog(',start);
function setup({embedded=false}={}) {
 const calls=[],alerts=[],origin={isConnected:true};let refreshes=0, disposed=0, lost=false;
 const title={value:'',focus(){},disabled:false},error={hidden:true,textContent:''},submit={disabled:false,textContent:'Create'},panel={hidden:true};
 const cancel={},keep={focus(){}},discard={},message={textContent:''};
 const form={elements:{title},hidden:false,inert:false,reportValidity:()=>true,querySelectorAll:()=>[title]};
 const nodes={'[data-project-create]':form,'[data-project-error]':error,'[data-project-submit]':submit,'[data-project-discard]':panel,
  '[data-project-cancel]':cancel,'[data-project-keep]':keep,'[data-project-discard-confirm]':discard,'[data-project-discard-message]':message};
 const host={innerHTML:'',querySelector:s=>nodes[s]};let guard=null,cleanup=null,session;
 const fake={setBeforeLeave(fn){guard=fn;},async close(){if(guard && !await guard())return false;cleanup?.();return true;}};
 const ctx={state:{bizs:[{id:'p1',slug:'project'}]},embeddedAgentPane:embedded,document:{activeElement:origin},
  parent:{taskCreateProjectOpen(...args){calls.push(['parent',...args]);return 'forwarded';}},
  window:{focusExpansion:{async open(spec){session=fake;cleanup=spec.mount(host,session);return session;}}},
  esc:v=>String(v),taskTypeFields:()=>'',taskTypeMount:()=>({values:()=>({task_type:'task'}),dispose(){disposed++;}}),
  toast:m=>alerts.push(m),uiFailure:e=>e.message,newRequestId:()=> 'key-1',tGen:0,clearEtagCache:()=>{},refreshTasks:()=>refreshes++,
  workCall:async(biz,op,payload)=>{calls.push([biz,op,payload]);if(op==='assignees_list')return {assignees:[{id:'owner',is_owner:true}]};if(lost){lost=false;throw Error('lost');}return {task:{id:'created'}};}};
 vm.createContext(ctx);vm.runInContext(source.slice(start,source.indexOf('// The owner drawer',end)),ctx);
 return {ctx,calls,alerts,title,error,submit,panel,form,cancel,keep,discard,message,origin,get session(){return session;},get refreshes(){return refreshes;},get disposed(){return disposed;},lose(){lost=true;}};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('Project form opens and cancels without a write; invalid scope is refused',async()=>{
 const h=setup();assert.equal(await h.ctx.taskCreateProjectOpen({business_id:'bad',business_slug:'project'}),null);assert.equal(h.alerts.length,1);
 const session=await h.ctx.taskCreateDialog('project');assert.ok(session);
 assert.deepEqual(h.calls,[]);assert.equal(await session.close(),true);assert.deepEqual(h.calls,[]);assert.equal(h.disposed,1);
});
test('embedded Project forwards only the typed data, without a callback',async()=>{
 const h=setup({embedded:true});const callback=()=>{};
 assert.equal(await h.ctx.taskCreateDialog('project',{initial:{title:'x'},onCreated:callback}),'forwarded');
 assert.equal(h.calls[0][0],'parent');assert.equal(h.calls[0].length,2);assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0][1])),{business_id:'p1',business_slug:'project',initial:{title:'x'}});
});
test('dirty close offers keep and explicit discard without writing',async()=>{
 const h=setup();const session=await h.ctx.taskCreateProjectOpen({business_id:'p1',business_slug:'project'});h.title.value='Draft';
 const keep=session.close();await tick();assert.equal(h.panel.hidden,false);h.keep.onclick();assert.equal(await keep,false);
 const discard=session.close();await tick();h.discard.onclick();assert.equal(await discard,true);assert.equal(h.calls.length,0);
});
test('unknown create outcome retains payload and key, retry succeeds',async()=>{
 const h=setup();let created=null;const session=await h.ctx.taskCreateProjectOpen({business_id:'p1',business_slug:'project'},task=>{created=task;});
 h.title.value='Draft';h.lose();await h.form.onsubmit({preventDefault(){}});assert.equal(h.submit.textContent,'Retry');
 const close=session.close();await tick();assert.match(h.message.textContent,/may have succeeded/);h.keep.onclick();assert.equal(await close,false);
 await h.form.onsubmit({preventDefault(){}});const writes=h.calls.filter(c=>c[1]==='tasks_create');
 assert.equal(writes.length,2);assert.deepEqual(writes[0][2],writes[1][2]);assert.equal(writes[0][2].idempotency_key,'key-1');assert.equal(h.refreshes,1);assert.equal(created.id,'created');
});

test('an in-flight create refuses closing until the request settles',async()=>{
 const h=setup();let release;h.ctx.workCall=async(biz,op)=>op==='assignees_list'?{assignees:[]}:new Promise(resolve=>release=resolve);
 const session=await h.ctx.taskCreateDialog('project');h.title.value='Draft';
 const creating=h.form.onsubmit({preventDefault(){}});await tick();assert.equal(await session.close(),false);
 release({task:{id:'created'}});await creating;assert.equal(h.refreshes,1);
});

test('real workCall sends the identical network key and payload after a lost response',async()=>{
 const h=setup(), sent=[];let count=0;
 h.ctx.coreData=value=>value;h.ctx.post=async(url,body)=>{if(url.endsWith('/assignees_list'))return {assignees:[]};sent.push({url,body});if(!count++)throw Error('lost');return {task:{id:'made'}};};
 const source=fs.readFileSync(require.resolve('../js/work-groups.js'),'utf8');
 const start=source.indexOf('async function workCall('),end=source.indexOf('// The shared item',start);vm.runInContext(source.slice(start,end),h.ctx);
 let result;await h.ctx.taskCreateDialog('project',{onCreated:task=>result=task});h.title.value='Network test';
 await h.form.onsubmit({preventDefault(){}});await h.form.onsubmit({preventDefault(){}});
 assert.equal(sent.length,2);assert.deepEqual(sent[0],sent[1]);assert.equal(sent[0].body.idempotency_key,'key-1');assert.equal(result.id,'made');
});
for(const failure of [{malformed:true},{httpStatus:408,code:'validation_error'},{httpStatus:429,code:'forbidden'},{code:'request_timeout'},{code:'invalid_response'}])
 test('unconfirmed create retains its request: '+JSON.stringify(failure),async()=>{
  const h=setup(),calls=[];let count=0,created=0;
  h.ctx.workCall=async(biz,op,args,options)=>{if(op==='assignees_list')return {assignees:[]};calls.push({args,options});
   if(!count++){if(failure.malformed)return {};throw Object.assign(Error('uncertain'),failure);}return {task:{id:'made'}};};
  await h.ctx.taskCreateDialog('project',{onCreated:()=>created++});h.title.value='Draft';
  await h.form.onsubmit({preventDefault(){}});assert.equal(created,0);assert.equal(h.refreshes,0);assert.equal(h.submit.textContent,'Retry');
  await h.form.onsubmit({preventDefault(){}});assert.equal(created,1);assert.deepEqual(calls[0],calls[1]);assert.equal(calls[0].options.idempotency_key,'key-1');
 });
