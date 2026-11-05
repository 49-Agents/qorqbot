'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');
const src=fs.readFileSync(require.resolve('../js/global-agents.js'),'utf8');
const selectCode=src.slice(src.indexOf('  const select = (agent'),src.indexOf('  comingSoonRetry.onclick'));
const openCode=src.slice(src.indexOf("  $('.global-agents', c).openAgent ="),src.indexOf('  const linkedAgent =',src.indexOf("  $('.global-agents', c).openAgent =")));
function roster(){
 const api={guard:async()=>true,request:async(p,v)=>{p.view=v;return true;}};
 const build=new Function('api', `
 let selectionVersion=0, selected='a', gated=false;
 let _agpad={agentId:'a',view:'focus',workspaceFrame:{},journalMount:null};
 const root={},c={},pane={dataset:{},querySelector(){return null;}},agents=[{agent_id:'a',role:{id:'ordubashy'}},{agent_id:'b',role:{id:'global-worker'}},{agent_id:'c',role:{id:'global-worker'}}];
 const current=()=>true,globalWorkspaceTerminal=()=>null,overseerRole=()=>false,testingModeEnabled=()=>true;
 const enterComingSoon=()=>{},leaveComingSoon=()=>{},paintSelection=()=>{},disposeChat=()=>{},ls=()=>{};
 const agentWorkspaceCanLeave=p=>api.guard(p),agpadRequestView=(p,v)=>api.request(p,v),agpadShownView=p=>p?.view;
 const mountAgentView=()=>{},mountTerminal=()=>{},agviewboxReveal=()=>{};
 const agpadMount=(name,el,opts)=>{_agpad={agentId:opts.agentId,view:opts.initialView||'focus'};};
 const $=(selector)=>selector==='.global-agents'?root:{};
 ${selectCode}\n${openCode}
 return {open:(id,opts)=>root.openAgent(id,opts),current:()=>_agpad,select};`);
 return {...api,...build(api),api};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('same agent awaits view guard success and refusal',async()=>{
 const r=roster();let resolve;r.api.request=(p,v)=>new Promise(done=>resolve=value=>{if(value)p.view=v;done(value);});
 const refused=r.open('a',{view:'calendar'});await tick();resolve(false);assert.equal(await refused,false);assert.equal(r.current().view,'focus');
 const accepted=r.open('a',{view:'calendar'});await tick();resolve(true);assert.equal(await accepted,true);assert.equal(r.current().view,'calendar');
});
test('different agent respects guard and ignores a stale selection',async()=>{
 const r=roster();let queue=[];r.api.guard=()=>new Promise(done=>queue.push(done));
 const refused=r.open('b',{view:'focus'});await tick();queue.shift()(false);assert.equal(await refused,false);assert.equal(r.current().agentId,'a');
 const stale=r.open('b',{view:'focus'});await tick();const latest=r.open('c',{view:'focus'});await tick();
 queue.shift()(true);assert.equal(await stale,false);queue.shift()(true);assert.equal(await latest,true);assert.equal(r.current().agentId,'c');
});
