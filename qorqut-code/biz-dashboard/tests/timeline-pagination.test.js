'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(require.resolve('../js/agent-timeline.js'),'utf8');
function setup(){const calls=[],paints=[];const p={agentId:'ord',timeline:{ready:true,loading:false,seq:0,date:'2026-11-10',rows:null,cursor:null,cursors:new Set()}};
 const ctx={p,Map,Set,Date,structuredClone,onChange:()=>{},accessLost:e=>e.code==='forbidden',AGPAD_POLL_MS:12000,agpadActive:()=>true,agentTimelineCurrent:(a,b)=>a.timeline===b,agentTimelinePaint:p=>paints.push(p.timeline.rows),uiFailure:e=>e.message,
 acOp:(_op,args)=>new Promise((resolve,reject)=>calls.push({args,resolve,reject}))};vm.createContext(ctx);
 vm.runInContext(fs.readFileSync(require.resolve('../js/read-cache.js'),'utf8'),ctx);
 vm.runInContext(source.slice(source.indexOf('async function agentTimelineRefresh'),source.indexOf('\nfunction agentTimelineTick')),ctx);
 return {p,ctx,calls,paints,read:more=>ctx.agentTimelineRefresh(p,more)};}
const tick=()=>new Promise(r=>setImmediate(r));
test('first page paints without fetching the next; Load more deduplicates overlapping records',async()=>{
 const h=setup(),first=h.read();await tick();assert.equal(h.calls.length,1);h.calls[0].resolve({actions:[{action_id:'a'}],next_cursor:'next'});await first;
 assert.equal(h.p.timeline.rows.length,1);assert.equal(h.calls.length,1);
 const more=h.read(true);assert.equal(h.calls[1].args.cursor,'next');h.calls[1].resolve({actions:[{action_id:'a'},{action_id:'b'}],next_cursor:null});await more;
 assert.deepEqual(Array.from(h.p.timeline.rows,x=>x.action_id),['a','b']);
});
test('a late page cannot replace another date and a failed continuation retains visible rows',async()=>{
 const h=setup(),first=h.read();await tick();h.calls[0].resolve({actions:[{action_id:'a'}],next_cursor:'next'});await first;
 const more=h.read(true);h.calls[1].reject(Error('Offline'));await more;assert.equal(h.p.timeline.rows[0].action_id,'a');assert.equal(h.p.timeline.cursor,'next');
 const retry=h.read(true);h.p.timeline={...h.p.timeline,date:'2026-11-09',rows:null,loading:false};h.calls[2].resolve({actions:[{action_id:'wrong'}]});await retry;assert.equal(h.p.timeline.rows,null);
});
