'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../js/task-move.js'),'utf8');
function fixture() {
  const events=[],forgot=[];
  const ctx={state:{bizs:[{id:'p1',slug:'one'},{id:'p2',slug:'two'}]},
    clearEtagCache(){},taskStateForget:(slug,ids)=>forgot.push([slug,ids]),storeInvalidateAll(){},
    document:{dispatchEvent:e=>events.push(e)},CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}}};
  vm.createContext(ctx);vm.runInContext(source + '\nglobalThis.taskMoveUncertain = taskMoveUncertain;',ctx);
  return {ctx,events,forgot};
}
test('uncertain transport outcomes keep the retry classification',()=>{
  const {ctx}=fixture();
  for (const error of [{},{code:'request_timeout'},{code:'invalid_response'},{code:'oops',httpStatus:503}]) assert.equal(ctx.taskMoveUncertain(error),true);
  for (const error of [{code:'revision_conflict'},{code:'move_blocked'},{code:'forbidden',httpStatus:403}]) assert.equal(ctx.taskMoveUncertain(error),false);
});
test('moved destination requires an authorized task identity',()=>{
  const {ctx}=fixture();
  assert.equal(ctx.taskMoveDestination({code:'record_moved',details:{destination:{id:'p2'},task:{id:'x',type:'story'}}}),null);
  const found=ctx.taskMoveDestination({code:'record_moved',details:{destination:{id:'p2'},task:{id:'x',type:'task',business_id:'p2'}}});
  assert.deepEqual(JSON.parse(JSON.stringify(found)),{destination:{id:'p2'},task:{type:'task',id:'x',business_id:'p2'}});
});
test('confirmed move invalidates the exact source and destination scope',()=>{
  const {ctx,events,forgot}=fixture();
  ctx.taskMoveInvalidate(['x','child'],'p1',{id:'p2',slug:'two'});
  assert.deepEqual(forgot,[['one',['x','child']],['two',['x','child']]]);
  assert.equal(events[0].type,'qorqut-task-moved');assert.deepEqual(Array.from(events[0].detail.ids),['x','child']);
});

test('hosted move retries the exact failed request and replaces its canonical reference',async()=>{
  class Node {
    constructor(){this.dataset={};this.children=[];this.hidden=false;this.value='';this.textContent='';}
    append(...children){this.children.push(...children);}
    replaceChildren(...children){this.children=children;}
    setAttribute(){}
  }
  class Host extends Node {
    constructor(){super();this.nodes={};}
    set innerHTML(value){
      this.nodes={};for(const selector of ['[data-move-source]','[data-move-destination]','[data-move-preview]',
        '[data-move-error]','[data-move-cancel]','[data-move-confirm]','.tmove'])this.nodes[selector]=new Node();
      this.buttons=[this.nodes['[data-move-cancel]'],this.nodes['[data-move-confirm]']];
    }
    querySelector(selector){return this.nodes[selector];}
    querySelectorAll(){return this.buttons;}
  }
  const requests=[], events=[];let failure=true,opened=null;
  const ctx={state:{bizs:[]},document:{createElement:()=>new Node(),dispatchEvent:e=>events.push(e)},
    Option:class extends Node {constructor(text,value){super();this.textContent=text;this.value=value;}},
    CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},
    coreData:x=>x,newRequestId:()=> 'key-1',accessLost:()=>false,sourceGone:()=>false,uiFailure:()=> 'error',
    clearEtagCache(){},taskStateForget(){},storeInvalidateAll(){},
    post:async(url,args)=>{
      if(url.endsWith('tasks_move_targets'))return {projects:[{id:'p2',name:'Two'}]};
      if(url.endsWith('tasks_move_to_project_preview'))return {preview:{token:'revision-token',can_move:true,tasks:[],counts:{},source:{name:'Global'},destination:{name:'Two'}}};
      requests.push(JSON.parse(JSON.stringify(args)));
      if(failure){failure=false;throw Error('lost response');}
      return {destination:{id:'p2',slug:'two'},moved:[{id:'task-1',business_id:'p2'}]};
    }};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  const host=new Host();const controller=ctx.taskMoveOpen({task:{id:'task-1',title:'Task'},sourceBusinessId:null,hostedContent:host,onMoved:r=>{opened=r;}});
  const flush=()=>new Promise(resolve=>setImmediate(resolve));
  await flush();const select=host.querySelector('[data-move-destination]');select.value='p2';select.onchange();await flush();
  host.querySelector('[data-move-confirm]').onclick();await flush();
  assert.equal(controller.status().uncertain,true);assert.equal(select.disabled,true);
  controller.retry();await flush();
  assert.deepEqual(requests[0],requests[1]);assert.equal(requests[0].preview_token,'revision-token');assert.equal(requests[0].idempotency_key,'key-1');
  assert.equal(opened.business_id,'p2');assert.equal(events[0].type,'qorqut-task-moved');
});
