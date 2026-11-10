'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(require.resolve('../js/read-cache.js'),'utf8');
const tick=()=>new Promise(r=>setImmediate(r));
function setup(){const ctx={structuredClone,Date,Promise,Map,Set,onChange:()=>{},accessLost:e=>e.code==='forbidden'};vm.createContext(ctx);vm.runInContext(source+'\nthis.cache=uiReads;',ctx);return ctx.cache;}
test('simultaneous readers share one request and get independent snapshots',async()=>{
 const c=setup();let calls=0,finish;
 const loader=()=>{calls++;return new Promise(r=>finish=r);};
 const first=c.read('focus:a',loader),second=c.read('focus:a',loader);await tick();assert.equal(calls,1);
 finish({intent:'saved'});const [a,b]=await Promise.all([first,second]);a.intent='local';assert.equal(b.intent,'saved');assert.equal(c.peek('focus:a').intent,'saved');
 assert.equal((await c.read('focus:a',loader)).intent,'saved');assert.equal(calls,1);
});
test('scope keys isolate records and invalidation prevents old responses restoring stale data',async()=>{
 const c=setup();let finish;const old=c.read('task:a:1',()=>new Promise(r=>finish=r));await tick();
 c.invalidate('task:a:');await c.read('task:b:1',async()=>({title:'B'}));await c.read('task:a:1',async()=>({title:'new A'}));
 finish({title:'old A'});await assert.rejects(old,{name:'AbortError'});
 assert.equal(c.peek('task:a:1').title,'new A');assert.equal(c.peek('task:b:1').title,'B');
});
test('cached content is available during refresh; denied access drops it for subscribers',async()=>{
 const c=setup(),seen=[];c.subscribe('focus:a',x=>seen.push(x));await c.read('focus:a',async()=>({intent:'old'}));
 let finish;const refresh=c.read('focus:a',()=>new Promise(r=>finish=r),{force:true});await tick();assert.equal(c.peek('focus:a').intent,'old');finish({intent:'new'});await refresh;
 await assert.rejects(c.read('focus:a',async()=>{throw Object.assign(Error('Denied'),{code:'forbidden'});},{force:true}));
 assert.equal(c.peek('focus:a'),null);assert.equal(seen.at(-1),null);
});
test('a superseded failure cannot erase a newer authorized snapshot',async()=>{
 const c=setup();let fail;const old=c.read('focus:a',()=>new Promise((_,r)=>fail=r));await tick();c.invalidate('focus:a');
 await c.read('focus:a',async()=>({intent:'new'}));fail(Object.assign(Error('Denied'),{code:'forbidden'}));await assert.rejects(old);
 assert.equal(c.peek('focus:a').intent,'new');
});
