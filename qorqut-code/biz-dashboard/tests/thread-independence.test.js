'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const read=name=>fs.readFileSync(path.join(__dirname,'../js',name+'.js'),'utf8');

function pairing(){
  const opened=[],cleared=[],references=[];let allow=true,rightCloses=0;
  const ctx={state:{scope:'global',globalTab:'agents'},_agpad:{open:true,view:'focus',workspaceRole:'ordubashy',agentId:'a',focusMount:{isConnected:true}},
    globalAgentViewActive:{selectPairedSource:async(...args)=>{opened.push(args);return allow;},clearPairedSource:g=>{cleared.push(g);return allow;}}};
  ctx.window=ctx;ctx.agentWorkspacePairedClose=()=>rightCloses++;ctx.agentWorkspacePairedReference=ref=>references.push(ref);
  vm.createContext(ctx);const source=read('agent-workspace');
  vm.runInContext(source.slice(source.indexOf('let agentWorkspaceDocument'),source.indexOf('function agentWorkspaceLoadDocument')),ctx);
  ctx.openSource=vm.runInContext('(source)=>agentWorkspacePairedRecord(window,source)',ctx);
  return {ctx,opened,cleared,references,setAllowed:v=>allow=v,rightCloses:()=>rightCloses};
}

test('returning to main Chat leaves the project pane open',async()=>{
  const h=pairing(),source={type:'project',id:'demo',business_id:'demo'};
  await h.ctx.openSource(source);
  assert.equal(h.opened.length,1);
  await h.opened[0][4]();
  assert.equal(h.cleared.length,1);assert.equal(h.rightCloses(),0);
});

test('a refused thread switch keeps the current thread navigation usable',async()=>{
  const h=pairing();await h.ctx.openSource({type:'project',id:'one'});
  h.setAllowed(false);
  assert.equal(await h.ctx.openSource({type:'project',id:'two'}),false);
  h.opened[0][3]({type:'project',id:'one'});assert.equal(h.references.length,1);
  h.setAllowed(true);await h.opened[0][4]();assert.equal(h.cleared.length,1);
  assert.equal(h.rightCloses(),0);
});


test('an unavailable thread leaves Chat even when its record is not open on the right',async()=>{
  const h=pairing(),checked=[];h.ctx.agentWorkspacePairedUnavailable=async source=>{checked.push(source.id);return false;};
  await h.ctx.openSource({type:'project',id:'gone'});
  await h.opened[0][2]({type:'project',id:'gone'});
  assert.deepEqual(checked,['gone']);assert.equal(h.cleared.length,1);
});

test('a busy thread in Chat holds every Focus leave guard',async()=>{
  const h=pairing();let leavable=false;
  h.ctx.globalAgentViewActive.pairedCanLeave=()=>leavable;
  assert.equal(vm.runInContext('agentWorkspacePairedBusy()',h.ctx),true);
  leavable=true;assert.equal(vm.runInContext('agentWorkspacePairedBusy()',h.ctx),false);
});

test('a failed thread mount keeps the previous thread navigation usable',async()=>{
  const h=pairing();await h.ctx.openSource({type:'project',id:'one'});
  h.ctx.globalAgentViewActive.selectPairedSource=async()=>{throw new Error('Unavailable')};
  await assert.rejects(h.ctx.openSource({type:'project',id:'two'}),/Unavailable/);
  h.opened[0][3]({type:'project',id:'one'});assert.equal(h.references.length,1);
  await h.opened[0][4]();assert.equal(h.cleared.length,1);
});
