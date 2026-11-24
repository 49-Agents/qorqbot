'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(require.resolve('../js/read-cache.js'),'utf8');
const tick=()=>new Promise(r=>setImmediate(r));
function setup(overrides={}){const ctx={structuredClone,Date,Promise,Map,Set,onChange:()=>{},accessLost:e=>e.code==='forbidden',coreData:value=>value,...overrides};vm.createContext(ctx);vm.runInContext(source+'\nthis.cache=uiReads;',ctx);return Object.assign(ctx.cache,{focusRead:ctx.agentFocusRead});}
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

test('focus refresh failures preserve the last recorded value and explicit clearing replaces it',async()=>{
 let response={agent_id:'a',attention:{state:'focused',focus:{label:'Recorded focus'}}},failure=null;
 const c=setup({post:async()=>{if(failure)throw failure;return response;}}),seen=[];
 c.subscribe('agent-focus:a',value=>seen.push(value));
 await c.focusRead('a');
 failure=Error('Offline');assert.equal((await c.focusRead('a',{force:true})).attention.focus.label,'Recorded focus');
 await assert.rejects(c.focusRead('b',{force:true}),/Offline/);
 failure=null;response={agent_id:'a',attention:{state:'focused',focus:{unavailable:true}}};
 assert.equal((await c.focusRead('a',{force:true})).attention.focus.label,'Recorded focus');assert.equal(seen.length,1);
 response={agent_id:'a',attention:{state:'unfocused',focus:null}};await c.focusRead('a',{force:true});
 failure=Error('Offline');assert.equal((await c.focusRead('a',{force:true})).attention.state,'unfocused');
 assert.equal(c.peek('agent-focus:a').attention.focus,null);
});
test('superseded focus reads return the latest recorded snapshot without publishing stale data',async()=>{
 let response={agent_id:'a',attention:{state:'focused',focus:{label:'Initial'}}},finish;
 const c=setup({post:()=>response || new Promise(resolve=>finish=resolve)});
 await c.focusRead('a');response=null;const old=c.focusRead('a',{force:true});await tick();
 c.invalidate('agent-focus:');response={agent_id:'a',attention:{state:'focused',focus:{label:'New focus'}}};await c.focusRead('a');
 finish({agent_id:'a',attention:{state:'focused',focus:{label:'Obsolete'}}});
 assert.equal((await old).attention.focus.label,'New focus');assert.equal(c.peek('agent-focus:a').attention.focus.label,'New focus');
});
test('revoked or removed focus records are not restored from the previous snapshot',async()=>{
 for(const code of ['forbidden','not_found']){
  let failure=false;const c=setup({post:async()=>{if(failure)throw Object.assign(Error('Unavailable'),{code});return {agent_id:'a',attention:{state:'focused',focus:{label:'Recorded focus'}}};}});
  await c.focusRead('a');failure=true;await assert.rejects(c.focusRead('a',{force:true}),{code});assert.equal(c.peek('agent-focus:a'),null);
 }
});
