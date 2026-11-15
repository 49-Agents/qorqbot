'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const read=name=>fs.readFileSync(path.join(__dirname,'../js',name+'.js'),'utf8');

test('opening a pinned thread selects Chat without opening or closing a record',async()=>{
  const opened=[];
  const ctx={_agpad:{workspaceRole:'ordubashy',focusMount:{isConnected:true}},agfocusLeftChatReady:()=>true,
    agfocusPairedSource:async(...args)=>{opened.push(args);return true;},agfocusOpenTarget:async()=>{throw new Error('record opened');},toast:()=>{}};
  ctx.window=ctx.parent=ctx;vm.createContext(ctx);vm.runInContext(read('thread-pins')+'\nthis.pins=threadPins;',ctx);
  const pin={source:{type:'project',id:'demo',business_id:'demo'},title:'Demo'};
  assert.equal(await ctx.pins.open(pin),true);
  assert.deepEqual(JSON.parse(JSON.stringify(opened)),[[{source:pin.source,label:'Demo'},null]]);
});

test('a permanent worker opens a pinned thread as its Focus record',async()=>{
  const targets=[],toasts=[];
  const ctx={_agpad:{workspaceRole:'global-worker',focusMount:{isConnected:true}},agfocusLeftChatReady:()=>true,
    agfocusPairedSource:async()=>{throw new Error('Chat area used');},agfocusOpenTarget:async(...args)=>{targets.push(args);return true;},toast:m=>toasts.push(m)};
  ctx.window=ctx.parent=ctx;vm.createContext(ctx);vm.runInContext(read('thread-pins')+'\nthis.pins=threadPins;',ctx);
  const pin={source:{type:'task',id:'t1',business_id:'demo'},title:'Call back'};
  assert.equal(await ctx.pins.open(pin),true);
  assert.equal(targets.length,1);assert.deepEqual(toasts,[]);
  assert.deepEqual(JSON.parse(JSON.stringify(targets[0].slice(1))),[{source:{...pin.source,label:'Call back'},can_thread:true},{root:true,paired:false}]);
});

test('a pinned thread refused by Chat says why',async()=>{
  const toasts=[];let busy=true;
  const ctx={_agpad:{workspaceRole:'ordubashy',focusMount:{isConnected:true}},agfocusLeftChatReady:()=>true,
    agfocusPairedSource:async()=>false,agentWorkspacePairedBusy:()=>busy,toast:m=>toasts.push(m)};
  ctx.window=ctx.parent=ctx;vm.createContext(ctx);vm.runInContext(read('thread-pins')+'\nthis.pins=threadPins;',ctx);
  const pin={source:{type:'project',id:'demo'},title:'Demo'};
  assert.equal(await ctx.pins.open(pin),false);busy=false;
  assert.equal(await ctx.pins.open(pin),false);
  assert.match(toasts[0],/before leaving this thread/);assert.equal(toasts[1],'This thread is unavailable.');
});

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
