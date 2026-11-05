'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const source=fs.readFileSync(require.resolve('../js/work-groups.js'),'utf8');
const start=source.indexOf('async function workOpenLink('),end=source.indexOf('// An unsaved creation form',start);
const make=new Function('workCall','sgAttentionNavigate','modal',source.slice(start,end)+';return workOpenLink;');
for (const state of ['pending','approved']) test(`project ${state} signoff link opens exact record`,async()=>{
  const calls=[];let reads=0;
  const open=make(async(biz,op,args)=>{reads++;assert.equal(biz,'project');assert.equal(op,'signoffs_get');assert.equal(args.signoff_id,'s1');return {signoff:{id:'s1',state}};},
    async(...args)=>{calls.push(args);return true;},()=>{throw Error('unexpected modal');});
  assert.equal(await open({biz:'project',error:err=>{throw err;}},{resource_type:'signoffs',resource_id:'s1',current:{kind:'signoff'}}),true);
  assert.equal(reads,1);assert.deepEqual(calls,[['project','s1']]);
});
