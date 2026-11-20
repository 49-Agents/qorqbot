'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
let chromium;
try { ({chromium}=require('playwright')); } catch (_) {}
if (!chromium) { try { ({chromium}=require(require('node:module').createRequire('/home/alp/qorqut/worktrees/draft-review/verification/').resolve('playwright'))); } catch (_) {} }
const executable=process.env.CHROME_PATH || chromium?.executablePath();
const available=!!chromium && fs.existsSync(executable);
const browserTest=(name,run)=>test(name,{skip:!available && 'Local Playwright Chromium is unavailable'},run);
const root=path.join(__dirname,'..');

// `remembered`: the saved selection; `link`: an agent id named by an old deep link.
async function fixture({remembered='__qorqut_workspace_terminal__',link=null}={}) {
  const mobile=false,many=false;
  const browser=await chromium.launch({headless:true,executablePath:executable});
  const page=await browser.newPage({viewport:{width:mobile?390:1200,height:800},hasTouch:mobile,isMobile:mobile});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.route('http://qorqbot.test/**',route=>route.fulfill({contentType:'text/html',body:'<body></body>'}));
  await page.goto('http://qorqbot.test/'+(link?'?agent='+encodeURIComponent(link):''));
  await page.setContent('<meta name="viewport" content="width=device-width, initial-scale=1"><body class="agent-workspace-app"><main id="host" style="display:flex;height:100vh;min-width:0"></main></body>');
  for(const file of ['app.css','css/context.css','css/global.css','css/global-chat.css','css/agentpane.css','css/agent-workspace.css','css/agent-workspace-nav.css','css/agent-timeline.css'])await page.addStyleTag({path:path.join(root,file)});
  await page.evaluate(({many,remembered,link})=>{
    window.$=(selector,host=document)=>host.querySelector(selector);
    window.esc=value=>String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    window.state={scope:'global',globalTab:'agents'};
    window.saved=new Map([['ag:scope:global',remembered],['agent-nav:global','0']]);window.toasts=[];window.toast=text=>toasts.push(text);
    window.ls=(key,value)=>value===undefined?saved.get(key):saved.set(key,value);
    window.globalAgentProfiles=[{agent_id:'ord',display_name:'Ordubashy',role:{id:'ordubashy'},scope:{kind:'ecosystem'}},
      {agent_id:'atlas',display_name:'Atlas',role:{id:'global-worker'},scope:{kind:'ecosystem'}}];
    if(many)for(let i=0;i<40;i++)globalAgentProfiles.push({agent_id:'worker-'+i,display_name:'Worker '+i,role:{id:'global-worker'},scope:{kind:'ecosystem'}});
    window.primaryAgent=agent=>agent?.role?.id==='ordubashy';
    window.agentGlyph=(agent,size)=>'<svg data-profile="'+esc(agent.agent_id)+'" width="'+size+'" height="'+size+'" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="currentColor"/></svg>';
    window.GLOBAL_ICONS={settings:'<svg viewBox="0 0 24 24"></svg>'};
    window.testingModeReady=async()=>{};window.testingModeEnabled=()=>true;window.testingModeSubscribe=()=>()=>{};
    window.coreData=value=>value;window.acOccurred=action=>action.occurred?.date || 'Undated';
    window.actionReads=[];window.post=async(url,body)=>{if(url.endsWith('/global_progress_get'))return {series:Array.from({length:7},(_,i)=>({date:'2026-11-'+String(i+5).padStart(2,'0'),tasks_done:0})),timezone:'UTC'};actionReads.push({url,body});return {actions:[{action_id:body.performer+'-1',text:'Reviewed the plan',occurred:{date:'2026-11-10',precision:'day'}}]};};
    window.agentFocusRead=async id=>({agent_id:id,attention:{state:'focused',focus:{label:'Review the launch plan'}}});
    window.agentNavPaint=()=>{};window.agentNavRefresh=()=>{};
    window.agTakeLinkedWorkspace=()=>null;window.agentWorkspaceTakeRequest=()=>null;window.agentWorkspaceRehydrate=()=>null;
    window.agentWorkspaceRequest=null;window.agentWorkspaceApplied=null;window._agpad=null;
    window.agentViewDispose=()=>{};window.mounts=[];
    window.mountAgentView=(pane,agent)=>{mounts.push(agent.agent_id);pane.innerHTML='<p style="padding-top:48px">Chat with '+esc(agent.display_name)+'</p>';};
    window.agpadMount=(_name,pane,options)=>{
      window._agpad={agentId:options.agentId};pane.hidden=!options.open;pane.textContent='Agent workspace';
      pane.closest('.agsplit').classList.toggle('pad-off',!options.open);
    };
    window.goGlobalTab=tab=>{window.destination=tab;};window.onboarding={open(){window.walkthrough=true;}};
  },{many,remembered,link});
  await page.addScriptTag({path:path.join(root,'js/san-juan-time.js')});
  await page.clock.setFixedTime(new Date('2026-11-11T18:00:00Z'));
  await page.addScriptTag({path:path.join(root,'js/agent-nav-timeline.js')});
  let code=fs.readFileSync(path.join(root,'js/global-agents.js'),'utf8');
  await page.addScriptTag({content:code});
  await page.evaluate(()=>renderGlobalAgents(document.querySelector('#host')));
  return {browser,page,errors};
}

const OLD_IDS=['__qorqut_workspace_terminal__','__qorqut_workspace_terminal_2__','__qorqut_workspace_terminal_3__'];

browserTest('a saved selection of the retired workspace shell falls back to Ordubashy, silently',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    assert.equal(await page.locator('[data-global-agent="__qorqut_workspace_terminal__"]').count(),0);
    assert.deepEqual(await page.evaluate(()=>mounts),['ord']);
    assert.deepEqual(await page.evaluate(()=>toasts),[]);
    assert.equal(await page.locator('.global-agent-session').innerText(),'Chat with Ordubashy');
    assert.deepEqual(errors,[]);
  } finally { await browser.close(); }
});

browserTest('an old shell link, or a Terminal 2 or 3 selection, falls back without a message',async()=>{
  for(const id of OLD_IDS){
    for(const options of [{remembered:id},{remembered:'ord',link:id}]){
      const {browser,page,errors}=await fixture(options);
      try { assert.deepEqual(await page.evaluate(()=>mounts),['ord'],id); assert.deepEqual(await page.evaluate(()=>toasts),[],id); assert.deepEqual(errors,[],id); }
      finally { await browser.close(); }
    }
  }
});

browserTest('an id that was never an agent still says so',async()=>{
  const {browser,page}=await fixture({remembered:'gone-agent'});
  try { assert.deepEqual(await page.evaluate(()=>toasts),['This agent is no longer active. Its history is kept.']); }
  finally { await browser.close(); }
});
