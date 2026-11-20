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
const SHELL='__qorqut_workspace_terminal__';
const entry=`[data-global-agent="${SHELL}"]`;

// `mode`: 'absent' (no agentMode at all), 'managed' or 'terminal'. The answer can be changed later through `window.setMode`.
async function fixture({mode='absent',remembered=SHELL,link=null}={}) {
  const browser=await chromium.launch({headless:true,executablePath:executable});
  const page=await browser.newPage({viewport:{width:1200,height:800}});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.route('http://qorqbot.test/**',route=>route.fulfill({contentType:'text/html',body:'<body></body>'}));
  await page.goto('http://qorqbot.test/'+(link?'?agent='+encodeURIComponent(link):''));
  await page.setContent('<meta name="viewport" content="width=device-width, initial-scale=1"><body class="agent-workspace-app"><main id="host" style="display:flex;height:100vh;min-width:0"></main></body>');
  for(const file of ['app.css','css/context.css','css/global.css','css/global-chat.css','css/agentpane.css','css/agent-workspace.css','css/agent-workspace-nav.css','css/agent-timeline.css','css/legacy-terminals.css'])await page.addStyleTag({path:path.join(root,file)});
  await page.evaluate(({mode,remembered})=>{
    window.$=(selector,host=document)=>host.querySelector(selector);
    window.esc=value=>String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    window.state={scope:'global',globalTab:'agents'};
    window.saved=new Map([['ag:scope:global',remembered],['agent-nav:global','0']]);window.toasts=[];window.toast=text=>toasts.push(text);
    window.ls=(key,value)=>value===undefined?saved.get(key):saved.set(key,value);
    window.globalAgentProfiles=[{agent_id:'ord',display_name:'Ordubashy',role:{id:'ordubashy'},scope:{kind:'ecosystem'}},
      {agent_id:'atlas',display_name:'Atlas',role:{id:'global-worker'},scope:{kind:'ecosystem'}}];
    window.primaryAgent=agent=>agent?.role?.id==='ordubashy';
    window.agentGlyph=(agent,size)=>'<svg width="'+size+'" height="'+size+'" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="currentColor"/></svg>';
    window.GLOBAL_ICONS={settings:'<svg viewBox="0 0 24 24"></svg>'};
    window.testingModeReady=async()=>{};window.testingModeEnabled=()=>true;window.testingModeSubscribe=()=>()=>{};
    window.coreData=value=>value;window.acOccurred=action=>action.occurred?.date || 'Undated';
    window.post=async url=>url.endsWith('/global_progress_get')?{series:[],timezone:'UTC'}:{actions:[]};
    window.agentFocusRead=async id=>({agent_id:id,attention:{state:'focused',focus:{label:'Review the plan'}}});
    window.agentNavPaint=()=>{};window.agentNavRefresh=()=>{};
    window.agTakeLinkedWorkspace=()=>null;window.agentWorkspaceTakeRequest=()=>null;window.agentWorkspaceRehydrate=()=>null;
    window.agentWorkspaceRequest=null;window.agentWorkspaceApplied=null;window._agpad=null;
    window.agentViewDispose=()=>{};window.mounts=[];window.torn=0;
    window.mountAgentView=(pane,agent)=>{mounts.push(agent.agent_id);pane.innerHTML='<p style="padding-top:48px">Chat with '+esc(agent.display_name)+'</p>';};
    window.mountTerminal=(_id,pane,_stat,base)=>{mounts.push('shell:'+base);pane.textContent='Workspace shell';};
    window.teardownTerm=()=>{torn++;};
    window.agpadMount=(_name,pane,options)=>{
      window._agpad={agentId:options.agentId};pane.hidden=!options.open;pane.textContent='Agent workspace';
      pane.closest('.agsplit').classList.toggle('pad-off',!options.open);
    };
    window.goGlobalTab=tab=>{window.destination=tab;};window.onboarding={open(){window.walkthrough=true;}};
    // Same contract as js/agent-mode.js: `terminals()` and an `agent-mode-changed` event on document.
    window.setMode=next=>{window.agentMode={terminals:()=>next==='terminal'};document.dispatchEvent(new CustomEvent('agent-mode-changed'));};
    if(mode!=='absent')window.agentMode={terminals:()=>mode==='terminal'};
  },{mode,remembered});
  await page.addScriptTag({path:path.join(root,'js/san-juan-time.js')});
  await page.clock.setFixedTime(new Date('2026-11-11T18:00:00Z'));
  await page.addScriptTag({path:path.join(root,'js/agent-nav-timeline.js')});
  await page.addScriptTag({content:fs.readFileSync(path.join(root,'js/global-agents.js'),'utf8')});
  await page.evaluate(()=>renderGlobalAgents(document.querySelector('#host')));
  return {browser,page,errors};
}

browserTest('managed mode, with or without agentMode: no shell entry, and a saved or linked shell id falls back silently',async()=>{
  for(const mode of ['absent','managed'])for(const options of [{remembered:SHELL},{remembered:'ord',link:SHELL},{remembered:'__qorqut_workspace_terminal_2__'}]){
    const {browser,page,errors}=await fixture({mode,...options});
    try {
      assert.equal(await page.locator(entry).count(),0,mode);
      assert.equal(await page.locator('.global-workspace-terminal').count(),0,mode);
      assert.deepEqual(await page.evaluate(()=>mounts),['ord'],mode);
      assert.deepEqual(await page.evaluate(()=>toasts),[],mode);
      assert.deepEqual(errors,[],mode);
    } finally { await browser.close(); }
  }
});

browserTest('Terminals mode: the entry is in the menu and a saved shell id selects it',async()=>{
  for(const id of [SHELL,'__qorqut_workspace_terminal_2__','__qorqut_workspace_terminal_3__']){
    const {browser,page,errors}=await fixture({mode:'terminal',remembered:id});
    try {
      assert.equal(await page.locator(entry).count(),1);
      assert.deepEqual(await page.evaluate(()=>mounts),['shell:/root/workspace-terminal'],id);
      assert.equal(await page.locator(entry).getAttribute('aria-pressed'),'true');
      assert.match(await page.locator('[data-agent-nav-trigger]').getAttribute('aria-label'),/Terminal selected/);
      assert.deepEqual(await page.evaluate(()=>toasts),[]);
      assert.deepEqual(errors,[]);
    } finally { await browser.close(); }
  }
});

browserTest('Terminals mode: a linked shell id selects it, and choosing an agent closes the shell viewer',async()=>{
  const {browser,page,errors}=await fixture({mode:'terminal',remembered:'ord',link:SHELL});
  try {
    assert.deepEqual(await page.evaluate(()=>mounts),['shell:/root/workspace-terminal']);
    await page.evaluate(()=>document.querySelector('[data-global-agent="atlas"]').click());
    assert.deepEqual(await page.evaluate(()=>mounts),['shell:/root/workspace-terminal','atlas']);
    assert.equal(await page.evaluate(()=>torn),1);
    await page.evaluate(id=>document.querySelector('[data-global-agent="'+id+'"]').click(),SHELL);
    assert.equal(await page.locator('.global-agent-session').innerText(),'Workspace shell');
    assert.deepEqual(errors,[]);
  } finally { await browser.close(); }
});

browserTest('the mode answer arrives after the first paint: the entry appears and the saved shell opens',async()=>{
  const {browser,page,errors}=await fixture({mode:'managed'});
  try {
    assert.equal(await page.locator(entry).count(),0);
    assert.deepEqual(await page.evaluate(()=>mounts),['ord']);
    await page.evaluate(()=>setMode('terminal'));
    assert.equal(await page.locator(entry).count(),1);
    assert.deepEqual(await page.evaluate(()=>mounts),['ord','shell:/root/workspace-terminal']);
    assert.deepEqual(errors,[]);
  } finally { await browser.close(); }
});

browserTest('a late Terminals answer shows the entry but does not override a choice made meanwhile',async()=>{
  const {browser,page,errors}=await fixture({mode:'managed'});
  try {
    await page.evaluate(()=>document.querySelector('[data-global-agent="atlas"]').click());
    await page.evaluate(()=>setMode('terminal'));
    assert.equal(await page.locator(entry).count(),1);
    assert.deepEqual(await page.evaluate(()=>mounts),['ord','atlas']);
    assert.deepEqual(errors,[]);
  } finally { await browser.close(); }
});

browserTest('the mode turns managed: the entry goes and an open shell gives way to Ordubashy',async()=>{
  const {browser,page,errors}=await fixture({mode:'terminal'});
  try {
    assert.equal(await page.locator(entry).getAttribute('aria-pressed'),'true');
    await page.evaluate(()=>setMode('managed'));
    assert.equal(await page.locator(entry).count(),0);
    assert.deepEqual(await page.evaluate(()=>mounts),['shell:/root/workspace-terminal','ord']);
    assert.equal(await page.evaluate(()=>torn),1);
    assert.deepEqual(await page.evaluate(()=>toasts),[]);
    assert.doesNotMatch(await page.locator('[data-agent-nav-trigger]').getAttribute('aria-label'),/Terminal/);
    assert.deepEqual(errors,[]);
  } finally { await browser.close(); }
});

test('the terminal key rule: keys typed inside a terminal are not dashboard shortcuts',()=>{
  const source=fs.readFileSync(path.join(root,'js/agent-shortcuts.js'),'utf8');
  assert.match(source,/closest\('[^']*,\.xterm,/);
});
