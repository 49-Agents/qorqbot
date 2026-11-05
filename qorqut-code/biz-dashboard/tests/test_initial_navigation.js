#!/usr/bin/env node
// Exercise startup routing and the real Global roster renderer with isolated state.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const source = name => fs.readFileSync(path.join(__dirname, '../js', name), 'utf8');
const startup = source('core.js').split('async function loadBiz(')[1].split('\nasync function selectBiz(')[0];
async function route(hash, saved, businesses = [{slug:'studio'}], preserveNavigation = false) {
  const selected = [];
  const context = vm.createContext({
    location:{hash}, state:{biz:null,bizs:[]}, api:async()=>businesses, loadSession:async()=>null,
    ls:()=>saved, document:{querySelector:()=>null},
    ovLegacy:hash=>/^#\/global\/overview/.test(hash)?{view:'charts'}:undefined, ovOpenLegacy:legacy=>selected.push(['legacy',legacy.view]),
    selectGlobal:tab=>selected.push(['global',tab]), selectBiz:slug=>selected.push(['business',slug]),
  });
  vm.runInContext('async function loadBiz(' + startup, context);
  await context.loadBiz({preserveNavigation});
  return {selected, hash:context.location.hash};
}
async function agentSelection(remembered, agents) {
  let selected;
  const pane = {isConnected:true,dataset:{},classList:{toggle(){}},setAttribute(){},querySelector:()=>({}),querySelectorAll:()=>[]};
  const context = vm.createContext({
    state:{scope:'global',globalTab:'agents'}, location:{hash:'',search:'',href:'http://test/'}, history:{replaceState(){}}, URLSearchParams, globalAgentProfiles:agents,
    loadGlobalAgentProfiles:async()=>agents,
    ls:(key,value)=>value === undefined ? (key === 'ag:scope:global' ? remembered : null) : value,
    $:()=>pane, GLOBAL_ICONS:{settings:''}, goGlobalTab(){}, esc:String, broadcastButton:()=>'', wireBroadcastButton(){},
    agentNavButton:()=>'', wireAgentNavToggle(){}, agTakeLinkedWorkspace:()=>null, agentWorkspaceTakeRequest:()=>null, agentWorkspaceRehydrate:()=>null, agentWorkspaceStore(){}, AGENT_PANE_DESCRIPTOR_ROLES:{},
    qorqbotAgentColor:()=>'#000', qorqbotAgentGlyph:()=>'', qorqbotAgentVariant:()=>({}), identityAvatar:()=>'', agentNavPaint(){}, agentNavRefresh(){}, agpadMount(){}, toast(){}, agentViewDispose(){},
    mountAgentView:(_pane,agent)=>{selected=agent.agent_id;}, mountTerminal:id=>{selected=id;},
  });
  vm.runInContext(source('global-agents.js'),context);
  context.wireGlobalAgentGaze = () => {}; // Pointer animation is outside this startup-routing fixture.
  await context.renderGlobalAgents(pane);
  return selected;
}
(async()=>{
  assert.deepEqual((await route('','')).selected,[['global','agents']]);
  assert.deepEqual((await route('','',[])).selected,[['global','agents']]);
  assert.deepEqual((await route('','#studio')).selected,[['business','studio']]);
  assert.deepEqual((await route('','#/global/context/personal')).selected,[['global','context']]);
  assert.equal((await route('','#/global/context/personal')).hash,'#/global/context/personal');
  assert.deepEqual((await route('#studio','#/global/agents')).selected,[['business','studio']]);
  // A retired Overview URL opens its drawer view over Agents instead of selecting a page.
  assert.deepEqual((await route('#/global/overview','#studio')).selected,[['legacy','charts']]);
  assert.deepEqual((await route('','#archived')).selected,[['global','agents']]);
  assert.deepEqual((await route('','',[],true)).selected,[]);
  const agents = [{agent_id:'learner',role:{id:'learning-applier'},scope:{kind:'ecosystem',allowed_business_ids:[],global_capabilities:[]}},{agent_id:'commander',display_name:'Custom name',role:{id:'ordubashy'},scope:{kind:'ecosystem',allowed_business_ids:[],global_capabilities:[]}}];
  assert.equal(await agentSelection(null,agents),'commander');
  assert.equal(await agentSelection('learner',agents),'learner');
  // A remembered retired Founder is not on the roster: fall back to Ordubashy, never open it.
  assert.equal(await agentSelection('founder',agents),'commander');
  assert.equal(await agentSelection('removed',agents),'commander');
  assert.equal(await agentSelection('__qorqut_workspace_terminal__',agents),'__qorqut_workspace_terminal__');
  assert.equal(await agentSelection(null,agents.slice(0,1)),'learner');
  assert.equal(await agentSelection(null,[]),'__qorqut_workspace_terminal__');
  console.log('Initial navigation: first visit, saved routes, deep links, refresh, role-based selection and missing-agent fallbacks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
