'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const tick=()=>new Promise(r=>setImmediate(r));
function setup(){
 const calls=[],paints=[],state={biz:'a',tasks:[],tasksBiz:null};
 const ctx={state,structuredClone,URLSearchParams,Map,Set,Date,onChange:()=>{},taskStateSubscribe:()=>{},taskStateObserve:(_scope,t)=>t,accessLost:e=>e.code==='forbidden',
 api:url=>new Promise((resolve,reject)=>calls.push({url,resolve,reject})),document:{querySelector:()=>null}};
 vm.createContext(ctx);for(const name of ['read-cache','business-tasks'])vm.runInContext(fs.readFileSync(require.resolve('../js/'+name+'.js'),'utf8'),ctx);
 ctx.businessTasksPaint=()=>paints.push({biz:state.tasksBiz,ids:state.tasks.map(x=>x.id)});
 return {ctx,state,calls,paints,read:more=>ctx.businessTasksLoad(more),run:code=>vm.runInContext(code,ctx)};
}
const row=id=>({id,title:id});
test('first-page tasks, explicit continuation and refresh retain the requested depth',async()=>{
 const h=setup(),first=h.read();await tick();assert.equal(h.calls.length,1);h.calls[0].resolve({tasks:[row('a')],next_cursor:'next'});await first;assert.equal(h.calls.length,1);
 const more=h.read(true);assert.match(h.calls[1].url,/cursor=next/);h.calls[1].resolve({tasks:[row('a'),row('b')],next_cursor:null});await more;assert.deepEqual(Array.from(h.state.tasks,t=>t.id),['a','b']);
 h.run("uiReads.invalidate('business-tasks:')");const refresh=h.read();await tick();h.calls[2].resolve({tasks:[row('a')],next_cursor:'next'});await tick();h.calls[3].resolve({tasks:[row('b')],next_cursor:null});await refresh;assert.equal(h.state.tasks.length,2);
});
test('changing a filter ignores a late response; revisiting a query paints its cached page immediately',async()=>{
 const h=setup(),old=h.read();await tick();h.run("btSearch='rare note'");const next=h.read();await tick();assert.match(h.calls[1].url,/search=rare\+note/);
 h.calls[1].resolve({tasks:[row('matching')],next_cursor:null});await next;h.calls[0].resolve({tasks:[row('wrong')],next_cursor:null});await old;
 assert.equal(h.state.tasks[0].id,'matching');h.run("btSearch='other'");const other=h.read();await tick();h.calls[2].resolve({tasks:[],next_cursor:null});await other;
 h.run("btSearch='rare note'");const cached=h.read();assert.equal(h.state.tasks[0].id,'matching');await cached;assert.equal(h.calls.length,3);
});
test('continuation failures retain rows and authorization loss removes cached data',async()=>{
 const h=setup(),first=h.read();await tick();h.calls[0].resolve({tasks:[row('a')],next_cursor:'next'});await first;
 const more=h.read(true);h.calls[1].reject(Error('Offline'));await more;assert.equal(h.state.tasks.length,1);
 const retry=h.read(true);h.calls[2].reject(Object.assign(Error('Denied'),{code:'forbidden'}));await retry;assert.equal(h.state.tasks.length,0);assert.equal(h.run('uiReads.peek(businessTasksKey())'),null);
});
