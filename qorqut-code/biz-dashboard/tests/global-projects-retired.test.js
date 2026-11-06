'use strict';
const {test}=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../js/global.js'),'utf8');
function setup(saved, embedded=false) {
  const storage={'global-tab':saved}, rendered=[];
  const ctx={workspaceNavigation:0,state:{scope:'global',globalTab:'agents'},embeddedAgentPane:embedded,window:{parent:{}},location:{hash:'#/global/projects'},
    ls:(key,value)=>value===undefined?storage[key]:(storage[key]=value), agentWorkspaceLegacyDestination:()=>null, tView:'', renderTab:async()=>rendered.push(ctx.state.globalTab), syncWorkspaceNav:()=>{}};
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('const GLOBAL_PAGES'),source.indexOf('// Reuse the actual business navigation'))+'\n'+source.slice(source.indexOf('async function selectGlobal'),source.indexOf('function renderGlobalContext'))+'\nthis.selectGlobal=selectGlobal;this.goGlobalTab=goGlobalTab;',ctx);
  return {ctx,storage,rendered};
}
test('old Projects links and saved tab fall back to Agents and rewrite the route',async()=>{
  for(const useSaved of [false,true]) {const {ctx,storage,rendered}=setup('projects'); await (useSaved?ctx.selectGlobal():ctx.selectGlobal('projects'));assert.equal(ctx.state.globalTab,'agents');assert.equal(ctx.location.hash,'/global/agents');assert.equal(storage['global-tab'],'agents');assert.deepEqual(rendered,['agents']);}
});
test('unrelated Settings and embedded Costs remain available',async()=>{
  const normal=setup('agents');await normal.ctx.goGlobalTab('settings');assert.equal(normal.ctx.state.globalTab,'settings');
  const embedded=setup('agents',true);await embedded.ctx.goGlobalTab('costs');assert.equal(embedded.ctx.state.globalTab,'costs');
});
