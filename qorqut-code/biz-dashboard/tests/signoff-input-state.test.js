'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../js/task-state.js'), 'utf8');
const task = (inputVersion, pending, extra={}) => ({id:'work', revision:1, work_status:'working',
  status:pending?'needs_input':'working', input_version:inputVersion, input_needed:pending,
  input_requests:pending?[{id:'question'}]:[], ...extra});
function controller(post=async()=>{}) {
  const context=vm.createContext({setTimeout,clearTimeout,post,coreData:x=>x,newRequestId:()=> 'request'});
  vm.runInContext(source,context);
  return context;
}

test('an answer clears input at the same task revision and stale reads cannot bring it back',()=>{
  const c=controller(), events=[];
  c.taskStateSubscribe((_,__,snapshot)=>events.push(snapshot.task.status));
  c.taskStateObserve(null,task(1,true));
  const answered=c.taskStateObserve(null,task(2,false));
  assert.equal(answered.status,'working');
  assert.equal(answered.revision,1);
  assert.deepEqual(events,['needs_input','working']);
  assert.equal(c.taskStateObserve(null,task(1,true)).status,'working');
  assert.equal(events.length,2);
});

test('new input observation survives an older task revision without reverting newer work',()=>{
  const c=controller();
  c.taskStateObserve(null,task(1,true,{revision:3,work_status:'blocked',title:'latest'}));
  const observed=c.taskStateObserve(null,task(2,false,{revision:2,title:'old'}));
  assert.equal(observed.status,'blocked');
  assert.equal(observed.title,'latest');
  assert.equal(observed.revision,3);
});

test('an in-flight work edit survives a Signoff answer, and status does not briefly clear a pending question',async()=>{
  let resolve;
  const c=controller(()=>new Promise(r=>resolve=r));
  const current=task(1,true);
  c.taskStateObserve(null,current);
  const save=c.taskStateSet(null,current,'blocked');
  assert.equal(c.taskStateSnapshot(null,'work').task.status,'needs_input');
  c.taskStateObserve(null,task(2,false));
  assert.equal(c.taskStateSnapshot(null,'work').task.status,'blocked');
  resolve({task:task(1,true,{revision:2,work_status:'blocked'})});
  await save;
  assert.equal(c.taskStateSnapshot(null,'work').task.status,'blocked');
  assert.equal(c.taskStateSnapshot(null,'work').task.input_version,2);
});

test('source scopes with the same task id keep independent input state',()=>{
  const c=controller();
  c.taskStateObserve(null,task(1,true));
  c.taskStateObserve('project',task(2,false));
  assert.equal(c.taskStateSnapshot(null,'work').task.status,'needs_input');
  assert.equal(c.taskStateSnapshot('project','work').task.status,'working');
});

test('pending cards stay visible independently of old-message pagination and are deduplicated in history',()=>{
  const script=fs.readFileSync(require.resolve('../js/conversation.js'),'utf8');
  class Node {
    constructor(){this.children=[];this.hidden=false;}
    append(node){node.parent=this;this.children.push(node);}
    remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}
    replaceChildren(){this.children=[];}
  }
  let mounted=0,disposed=0;
  const c=vm.createContext({state:{bizs:[]},conversationEntries:()=>[],document:{createElement:()=>new Node()},
    sgChatMount:()=>{mounted++;return {dispose(){disposed++;}};}});
  vm.runInContext(script.slice(script.indexOf('function conversationRecordKey'),script.indexOf('// ---- Task context')),c);
  const request={type:'signoff',id:'old-question',business_id:null,created_at:'2020-01-01',title:'Choose a date'};
  const s={notes:[{created_at:'2026-11-10'}],before:5,related:[request],inputRequests:[request]};
  assert.equal(c.conversationTimeline(s,{related:true},{type:'task',id:'work'}).length,0);
  const host=new Node(),inputs=c.conversationInputMount(host);
  inputs.update(s.inputRequests);
  assert.equal(host.hidden,false);
  assert.equal(mounted,1);
  inputs.update(s.inputRequests);
  assert.equal(mounted,1,'Polling preserves the existing editor');
  inputs.update([{unavailable:true,link_id:'binding'}]);
  assert.equal(disposed,1);
  assert.match(host.children[1].textContent,/unavailable/);
  inputs.update([]);
  assert.equal(host.hidden,true);
});
