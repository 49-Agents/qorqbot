'use strict';
// Record types an extension adds: the Type editor lists them after the base types, draws one fieldset of each type's fields,
// sends the type and its values with the Task's other custom fields, and with no type active is exactly as before.
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../js/tasks.js'),'utf8');
const slice=(from,to)=>{const a=source.indexOf(from),b=source.indexOf(to,a);assert.ok(a>=0&&b>a,from);return source.slice(a,b);};
const helpers=slice('// Record types extensions add on top','// A Customer\'s free-text state label.');
const editor=slice('// `overviewOwned`','function taskEmailOpen(');
const LABELS={task:'Task',story:'Story',customer:'Customer',email:'Email'};
const CONNECTION={id:'connection',label:'Connection',plural:'Connections',based_on:'customer',extension:'network',fields:[
 {id:'met_at',label:'Where we met',type:'string'},{id:'closeness',label:'Closeness',type:'enum',values:['close','friendly','distant']},
 {id:'last_contact',label:'Last contact',type:'date'}]};
const VALUES={met_at:'Lisbon',closeness:'friendly',last_contact:'2026-11-20'};
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

function context(types=[]){
 const ctx={TASK_TYPE_LABELS:LABELS,esc,customerFieldsHtml:()=>'<fieldset data-task-customer hidden></fieldset>',
  uiReads:{read:async(key,loader)=>loader()},workCall:async()=>({record_types:ctx.served||types}),customerStatesMount:()=>({load(){},dispose(){}}),
  customerFieldsRead:()=>({customer_kind:'person',customer_state:'',customer_profile:{}}),Node:{TEXT_NODE:3},JSON};
 vm.createContext(ctx);vm.runInContext(helpers+editor+';this.taskTypeLabel=taskTypeLabel;this.taskRecordTypes=()=>taskRecordTypes;',ctx);
 return ctx;
}
async function loaded(types){const ctx=context(types);assert.equal(await ctx.taskRecordTypesLoad(),types.length>0);return ctx;}
// The Type select's options exactly as the editor drew them before record types existed.
const before=task=>Object.entries(LABELS).filter(([value])=>value!=='email'||task.task_type==='email').map(([value,label])=>`<option value="${value}" ${(task.task_type||'task')===value?'selected':''}>${label}</option>`).join('');

test('with no record type active the Type editor and labels are as before',async()=>{
 const ctx=await loaded([]);
 for(const task of [{},{task_type:'story',id:'a'},{task_type:'customer',id:'b',record_type:'connection'},{task_type:'email',id:'c'}]){
  const html=ctx.taskTypeFields(task);
  assert.ok(html.includes(`aria-label="Task type">${before(task)}</select>`),JSON.stringify(task));
  assert.ok(html.includes('<fieldset data-task-customer hidden></fieldset>\n    </div>'));
  assert.equal(ctx.taskTypeLabel(task),LABELS[task.task_type||'task']);
 }
 const customer={id:'b',task_type:'customer',fields:{priority:2}};
 assert.deepEqual({...ctx.taskTypeChanges(customer,{task_type:'customer'})},{});
 assert.deepEqual({...ctx.taskTypeChanges({id:'d',task_type:'task'},{task_type:'task',overview:'New'})},{overview:'New'});
});

test('active record types follow the base types by label and a record selects its own',async()=>{
 const ctx=await loaded([CONNECTION]);
 const html=ctx.taskTypeFields({id:'a',task_type:'customer',record_type:'connection',fields:VALUES});
 assert.ok(html.includes('<option value="task" >Task</option><option value="story" >Story</option><option value="customer" >Customer</option>'
  +'<option value="customer:connection" selected>Connection</option></select>'));
 assert.equal(ctx.taskTypeLabel({task_type:'customer',record_type:'connection'}),'Connection');
 // A record whose type is off for everyone, or of another base, shows its base type.
 assert.equal(ctx.taskTypeLabel({task_type:'story',record_type:'connection'}),'Story');
 assert.match(ctx.taskTypeOptionsHtml({task_type:'story',record_type:'connection'}),/<option value="story" selected>Story/);
});

test('each type gets one details fieldset drawn from its fields',async()=>{
 const flags={...CONNECTION,id:'flagged',label:'Flagged',fields:[{id:'vip',label:'VIP',type:'bool'},{id:'rank',label:'Rank',type:'int'}]};
 const ctx=await loaded([CONNECTION,flags]);
 const html=ctx.taskRecordFieldsHtml({fields:{...VALUES,vip:true,rank:3}});
 assert.match(html,/<fieldset class="task-record-fields global-task-wide" data-task-record="connection" hidden><legend>Connection details<\/legend>/);
 assert.match(html,/Where we met<input class="editable-field" type="text" data-record-field="met_at" data-record-kind="string" autocomplete="off" value="Lisbon">/);
 assert.match(html,/<select class="ef-control" data-record-field="closeness" data-record-kind="enum"><option value=""><\/option><option value="close" >close<\/option><option value="friendly" selected>friendly<\/option>/);
 assert.match(html,/type="date" data-record-field="last_contact" data-record-kind="date" autocomplete="off" value="2026-11-20"/);
 assert.match(html,/<input type="checkbox" data-record-field="vip" data-record-kind="bool" checked> VIP/);
 assert.match(html,/type="number" step="1" data-record-field="rank" data-record-kind="int" autocomplete="off" value="3"/);
 assert.match(html,/<legend>Flagged details<\/legend>/);
});

test('changes carry the record type and its values only when they changed',async()=>{
 const ctx=await loaded([CONNECTION]);
 const record={id:'a',task_type:'customer',record_type:'connection',fields:{...VALUES,other:'x'}};
 const same={task_type:'customer',record_type:'connection',fields:{...VALUES,other:'x'}};
 assert.deepEqual({...ctx.taskTypeChanges(record,same)},{});
 assert.deepEqual(JSON.parse(JSON.stringify(ctx.taskTypeChanges(record,{...same,fields:{...same.fields,closeness:'close'}}))),{fields:{...VALUES,other:'x',closeness:'close'}});
 assert.deepEqual({...ctx.taskTypeChanges({id:'b',task_type:'customer'},{task_type:'customer',record_type:'connection',fields:VALUES})},{record_type:'connection',fields:VALUES});
 assert.deepEqual({...ctx.taskTypeChanges(record,{task_type:'customer',record_type:null})},{record_type:null});
});

// The editor over a stand-in of the drawn form: the Type select, one fieldset per type and its controls, read back from
// the HTML the editor draws. `set` is the owner's edit: the value changes and an input event reaches the host.
function mounted(ctx,task,value,{types=[CONNECTION]}={}){
 const type={value,focus(){},set innerHTML(html){this.options=[...html.matchAll(/value="([^"]*)"/g)].map(([,v])=>({value:v}));const selected=/value="([^"]*)" selected/.exec(html);if(selected)this.value=selected[1];},options:[]};
 const overviewField={hidden:false},overview={value:task.overview||''},listeners={};
 let boxes=[];
 const draw=list=>{const values=task.fields||{};boxes=list.map(item=>({dataset:{taskRecord:item.id},hidden:true,inputs:item.fields.map(field=>({dataset:{recordField:field.id,recordKind:field.type},
  value:field.type==='bool'?'on':String(values[field.id]??''),checked:values[field.id]===true}))}));};
 draw(types);
 const customer={hidden:true,insertAdjacentHTML(){draw(ctx.taskRecordTypes());}};
 const host={isConnected:true,children:[],closest:()=>null,append(){},addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),
  querySelector:s=>({'[data-task-type]':type,'[data-task-customer]':customer,'[data-task-overview-field]':overviewField,'[data-task-overview]':overview}[s]||null),
  querySelectorAll:s=>{
   if(s==='[data-task-record]')return boxes.map(box=>Object.assign(box,{remove(){boxes=boxes.filter(item=>item!==box);}}));
   if(s==='[data-record-field]')return boxes.flatMap(box=>box.inputs);
   const one=/^\[data-task-record="(.+)"\] \[data-record-field\]$/.exec(s);
   return one?boxes.find(box=>box.dataset.taskRecord===one[1])?.inputs||[]:[];
  }};
 const editor=ctx.taskTypeMount(host,null,task);
 const input=(id,record='connection')=>boxes.find(box=>box.dataset.taskRecord===record).inputs.find(item=>item.dataset.recordField===id);
 const set=(id,next,record)=>{const target=input(id,record);if(typeof next==='boolean')target.checked=next;else target.value=next;(listeners.input||[]).forEach(fn=>fn({target}));};
 return {editor,type,get box(){return boxes.find(box=>box.dataset.taskRecord==='connection');},input,set,change(next){type.value=next;type.onchange();}};
}
const json=value=>JSON.parse(JSON.stringify(value));
test('the editor sends the chosen type, its base and its values over the other fields',async()=>{
 const ctx=await loaded([CONNECTION]);
 const task={id:'a',task_type:'customer',record_type:'connection',fields:{...VALUES,other:'x'}};
 const h=mounted(ctx,task,'customer:connection');
 assert.equal(h.box.hidden,false);
 assert.equal(h.editor.dirty(),false);
 h.set('closeness','close');h.set('met_at','');
 assert.deepEqual(JSON.parse(JSON.stringify(h.editor.values())),{task_type:'customer',record_type:'connection',fields:{closeness:'close',last_contact:'2026-11-20',other:'x'}});
 assert.equal(h.editor.dirty(),true);
 h.change('customer');
 assert.equal(h.box.hidden,true);
 assert.deepEqual({...h.editor.values()},{task_type:'customer',record_type:null});
 h.editor.reset();
 assert.equal(h.type.value,'customer:connection');assert.equal(h.input('closeness').value,'friendly');assert.equal(h.input('met_at').value,'Lisbon');
 assert.equal(h.editor.dirty(),false);
});
test('a new record of the type and a record whose type is off',async()=>{
 const ctx=await loaded([CONNECTION]);
 const created=mounted(ctx,{task_type:'customer'},'customer');
 created.change('customer:connection');created.set('met_at','Lisbon');
 assert.deepEqual(JSON.parse(JSON.stringify(created.editor.values())),{task_type:'customer',overview:'',customer_kind:'person',customer_state:'',customer_profile:{},record_type:'connection',fields:{met_at:'Lisbon'}});
 const off=context([]);
 const task={id:'a',task_type:'customer',record_type:'connection',fields:VALUES};
 const h=mounted(off,task,'customer');
 assert.deepEqual({...h.editor.values()},{task_type:'customer'});
 assert.equal(h.editor.dirty(),false);
});

const FLAGGED={id:'flagged',label:'Flagged',plural:'Flagged',based_on:'customer',extension:'flags',fields:[
 {id:'vip',label:'VIP',type:'bool'},{id:'rank',label:'Rank',type:'int'},{id:'level',label:'Level',type:'enum',values:[1,2,3]}]};
test('a bool, an int and an enum of numbers are sent as their own types',async()=>{
 const ctx=await loaded([CONNECTION,FLAGGED]);
 const task={id:'a',task_type:'customer',record_type:'flagged',fields:{vip:true,rank:3}};
 const h=mounted(ctx,task,'customer:flagged',{types:[CONNECTION,FLAGGED]});
 h.set('vip',false,'flagged');h.set('rank','12','flagged');h.set('level','2','flagged');
 assert.deepEqual(json(h.editor.values().fields),{vip:false,rank:12,level:2});
 h.set('rank','','flagged');h.set('vip',true,'flagged');
 assert.deepEqual(json(h.editor.values().fields),{vip:true,level:2});
 // Unchecking a box that held nothing leaves it out; a value that is not a whole number goes to the engine as typed.
 const fresh=mounted(ctx,{task_type:'customer'},'customer:flagged',{types:[CONNECTION,FLAGGED]});
 fresh.set('vip',true,'flagged');fresh.set('vip',false,'flagged');fresh.set('rank','1.5','flagged');
 assert.deepEqual(json(fresh.editor.values().fields),{rank:'1.5'});
});
test('a switch between two record types keeps the first type\'s values and adds the second\'s',async()=>{
 const ctx=await loaded([CONNECTION,FLAGGED]);
 const task={id:'a',task_type:'customer',record_type:'connection',fields:{...VALUES}};
 const h=mounted(ctx,task,'customer:connection',{types:[CONNECTION,FLAGGED]});
 h.set('met_at','Porto');
 h.change('customer:flagged');
 assert.equal(h.box.hidden,true);
 h.set('rank','4','flagged');
 assert.deepEqual(json(h.editor.values()),{task_type:'customer',record_type:'flagged',fields:{...VALUES,rank:4}});
 assert.deepEqual(json(ctx.taskTypeChanges(task,h.editor.values())),{record_type:'flagged',fields:{...VALUES,rank:4}});
 h.editor.reset();
 assert.equal(h.type.value,'customer:connection');assert.equal(h.input('met_at').value,'Lisbon');
 assert.deepEqual(json(h.editor.values()),{task_type:'customer',record_type:'connection',fields:VALUES});
});
test('a remount over an unsaved choice keeps the Task\'s own type as the original',async()=>{
 const ctx=await loaded([CONNECTION,FLAGGED]);
 const task={id:'a',task_type:'customer',record_type:'connection',fields:{...VALUES}};
 // The select already shows an unsaved Customer or Flagged choice when the editor is mounted again.
 const plain=mounted(ctx,task,'customer',{types:[CONNECTION,FLAGGED]});
 assert.deepEqual(json(plain.editor.values()),{task_type:'customer',record_type:null});
 assert.equal(plain.editor.dirty(),true);
 plain.editor.reset();
 assert.equal(plain.type.value,'customer:connection');assert.equal(plain.editor.dirty(),false);
 const other=mounted(ctx,task,'customer:flagged',{types:[CONNECTION,FLAGGED]});
 assert.equal(other.editor.dirty(),true);
 other.editor.reset();assert.equal(other.type.value,'customer:connection');
 // A plain Customer mounted over an unsaved Connection choice has no record type to clear.
 const customer=mounted(ctx,{id:'b',task_type:'customer'},'customer:connection',{types:[CONNECTION,FLAGGED]});
 customer.editor.reset();assert.equal(customer.type.value,'customer');
 assert.deepEqual(json(customer.editor.values()),{task_type:'customer'});
});
test('a type list that changes after the editor opens draws the choices and fieldsets again',async()=>{
 const ctx=await loaded([]);
 const task={id:'a',task_type:'customer',record_type:'connection',fields:{...VALUES}};
 // The page loaded with no type active; one is switched on before the editor's own read answers.
 ctx.served=[CONNECTION];
 const h=mounted(ctx,task,'customer',{types:[]});
 assert.equal(h.box,undefined);
 await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(h.type.options.map(option=>option.value),['task','story','customer','customer:connection']);
 // The Task's own type, now active, is selected and is the original.
 assert.equal(h.type.value,'customer:connection');
 assert.equal(h.box.hidden,false);
 assert.equal(h.editor.dirty(),false);
 assert.deepEqual(json(h.editor.values()),{task_type:'customer',record_type:'connection',fields:VALUES});
 // A choice the owner made before the list changed is kept while it is still offered.
 const owner=context([]);owner.served=[CONNECTION];
 const chosen=mounted(owner,{id:'b',task_type:'customer'},'customer',{types:[]});
 chosen.change('task');
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(chosen.type.value,'task');
});
test('a field the owner did not touch keeps its stored value exactly',async()=>{
 const ctx=await loaded([CONNECTION,FLAGGED]);
 // Values no control can show as stored: a number in a text field, an unknown choice, a rank that is not a number.
 const stored={met_at:7,closeness:'very close',last_contact:'someday',rank:'high',vip:'yes',note:'kept'};
 const task={id:'a',task_type:'customer',record_type:'connection',fields:stored};
 const h=mounted(ctx,task,'customer:connection',{types:[CONNECTION,FLAGGED]});
 h.input('met_at').value='';h.input('last_contact').value='';  // what a browser does with what it cannot show
 assert.deepEqual(json(h.editor.values().fields),stored);
 assert.equal(h.editor.dirty(),false);
 h.change('customer:flagged');
 assert.deepEqual(json(h.editor.values().fields),stored);
 h.change('customer:connection');h.set('closeness','close');
 assert.deepEqual(json(h.editor.values().fields),{...stored,closeness:'close'});
 h.editor.reset();
 assert.deepEqual(json(h.editor.values().fields),stored);
 // An unknown stored choice is drawn as its own selected option.
 const html=ctx.taskRecordFieldsHtml({fields:{closeness:'very close',level:9}});
 assert.match(html,/data-record-field="closeness" data-record-kind="enum"><option value=""><\/option><option value="very close" selected>very close<\/option><option value="close" >close<\/option>/);
 assert.match(html,/data-record-field="level" data-record-kind="enum"><option value=""><\/option><option value="9" selected>9<\/option><option value="1" >1<\/option>/);
 assert.match(ctx.taskRecordFieldsHtml({fields:{closeness:'close'}}),/<option value=""><\/option><option value="close" selected>close<\/option><option value="friendly" >friendly/);
});
