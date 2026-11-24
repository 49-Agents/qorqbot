'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../js/tasks.js'),'utf8');
const a=source.indexOf('function taskTypeMount('),b=source.indexOf('function taskEmailOpen(',a);
function setup(focus=true){
 let generic=0,statesDisposed=0;const type={value:'task',focus(){}},customer={},overview={hidden:false};
 const first={hidden:false,inert:false},second={hidden:true,inert:false};let panel;
 function element(tag){return {tag,children:[],append(...items){this.children.push(...items);},setAttribute(){},focus(){},remove(){this.removed=true;}};}
 const host={isConnected:true,children:[first,second],closest:()=>focus?{}:null,querySelector:s=>({'[data-task-type]':type,'[data-task-customer]':customer,'[data-task-overview-field]':overview}[s]||null),
  querySelectorAll:()=>[],append(node){panel=node;}};
 const ctx={document:{createElement:element},TASK_TYPE_LABELS:{task:'Task',story:'Story'},customerStatesMount:()=>({load(){},dispose(){statesDisposed++;}}),
  taskConfirm:async()=>{generic++;return true;},Node:{TEXT_NODE:3}};
 vm.createContext(ctx);vm.runInContext(source.slice(a,b),ctx);
 const editor=ctx.taskTypeMount(host,'project',{id:'customer',task_type:'customer'});
 return {editor,host,type,first,second,get panel(){return panel;},get generic(){return generic;},get statesDisposed(){return statesDisposed;}};
}
test('Focus conversion shows warning and Cancel preserves editor and sends no write',async()=>{
 const h=setup();const result=h.editor.confirmConversion();assert.equal(h.first.hidden,true);assert.equal(h.first.inert,true);
 assert.match(h.panel.children[1].textContent,/clears its kind, state, profile and notes/);h.panel.children[2].onclick();
 assert.equal(await result,false);assert.equal(h.first.hidden,false);assert.equal(h.second.hidden,true);assert.equal(h.first.inert,false);assert.equal(h.generic,0);
});
test('Focus Change type is explicit and stale selections cannot confirm',async()=>{
 const h=setup();let result=h.editor.confirmConversion();h.panel.children[3].onclick();assert.equal(await result,true);
 result=h.editor.confirmConversion();h.type.value='story';h.panel.children[3].onclick();assert.equal(await result,false);
});
test('disposing a pending confirmation resolves false',async()=>{
 const h=setup();const result=h.editor.confirmConversion();h.editor.dispose();assert.equal(await result,false);assert.equal(h.statesDisposed,1);assert.equal(h.panel.removed,true);
});
test('non-Focus conversion retains existing shared confirmation',async()=>{
 const h=setup(false);assert.equal(await h.editor.confirmConversion(),true);assert.equal(h.generic,1);assert.equal(h.panel,undefined);
});

test('a detached record cannot confirm the stale conversion',async()=>{
 const h=setup();const result=h.editor.confirmConversion();h.host.isConnected=false;h.panel.children[3].onclick();assert.equal(await result,false);
});
