'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
let chromium;
try { ({chromium}=require('playwright')); } catch (_) {}
const executable=process.env.CHROME_PATH || chromium?.executablePath();
const available=!!chromium && fs.existsSync(executable);
const browserTest=(name,run)=>test(name,{skip:!available && 'Local Playwright Chromium is unavailable'},run);
const root=path.join(__dirname,'..');

async function fixture({mobile=false,many=false}={}) {
  const browser=await chromium.launch({headless:true,executablePath:executable});
  const page=await browser.newPage({viewport:{width:mobile?390:1200,height:800},hasTouch:mobile,isMobile:mobile});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.setContent('<meta name="viewport" content="width=device-width, initial-scale=1"><body class="agent-workspace-app"><main id="host" style="display:flex;height:100vh;min-width:0"></main></body>');
  for(const file of ['app.css','css/global.css','css/agentpane.css','css/agent-workspace.css'])await page.addStyleTag({path:path.join(root,file)});
  await page.evaluate(many=>{
    window.$=(selector,host=document)=>host.querySelector(selector);
    window.esc=value=>String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    window.state={scope:'global',globalTab:'agents'};
    window.saved=new Map([['ag:scope:global','atlas'],['agent-nav:global','0']]);
    window.ls=(key,value)=>value===undefined?saved.get(key):saved.set(key,value);
    window.globalAgentProfiles=[{agent_id:'ord',display_name:'Ordubashy',role:{id:'ordubashy'},scope:{kind:'ecosystem'}},
      {agent_id:'atlas',display_name:'Atlas',role:{id:'global-worker'},scope:{kind:'ecosystem'}}];
    if(many)for(let i=0;i<40;i++)globalAgentProfiles.push({agent_id:'worker-'+i,display_name:'Worker '+i,role:{id:'global-worker'},scope:{kind:'ecosystem'}});
    window.primaryAgent=agent=>agent.role.id==='ordubashy';
    window.agentGlyph=(agent,size)=>'<svg data-profile="'+esc(agent.agent_id)+'" width="'+size+'" height="'+size+'" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="currentColor"/></svg>';
    window.GLOBAL_ICONS={settings:'<svg viewBox="0 0 24 24"></svg>'};
    window.testingModeReady=async()=>{};window.testingModeEnabled=()=>true;window.testingModeSubscribe=()=>()=>{};
    window.agentNavPaint=()=>{};window.agentNavRefresh=()=>{};
    window.agTakeLinkedWorkspace=()=>null;window.agentWorkspaceTakeRequest=()=>null;window.agentWorkspaceRehydrate=()=>null;
    window.agentWorkspaceRequest=null;window.agentWorkspaceApplied=null;window._agpad=null;
    window.agentViewDispose=()=>{};window.mounts=[];
    window.mountAgentView=(pane,agent)=>{mounts.push(agent.agent_id);pane.innerHTML='<p style="padding-top:48px">Chat with '+esc(agent.display_name)+'</p>';};
    window.mountTerminal=(_id,pane)=>{pane.textContent='Workspace terminal';};
    window.agpadMount=(_name,pane,options)=>{
      window._agpad={agentId:options.agentId};pane.hidden=!options.open;pane.textContent='Agent workspace';
      pane.closest('.agsplit').classList.toggle('pad-off',!options.open);
    };
    window.goGlobalTab=tab=>{window.destination=tab;};window.onboarding={open(){window.walkthrough=true;}};
  },many);
  await page.addScriptTag({path:path.join(root,'js/global-agents.js')});
  await page.evaluate(()=>renderGlobalAgents(document.querySelector('#host')));
  return {browser,page,errors};
}

browserTest('navigation is hidden by default and hover overlays without resizing or remounting chat',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    const trigger=page.locator('[data-agent-nav-trigger]'),nav=page.locator('.agent-nav-overlay');
    assert.equal(await nav.isVisible(),false);
    assert.equal(await trigger.getAttribute('data-selected-agent'),'atlas');
    assert.equal(await trigger.locator('[data-profile]').getAttribute('data-profile'),'atlas');
    const before=await page.locator('.global-agent-session').boundingBox();
    assert.equal(before.x,0);
    await trigger.hover();
    assert.equal(await nav.isVisible(),true);
    assert.equal(await trigger.getAttribute('aria-expanded'),'true');
    assert.equal((await nav.boundingBox()).width,280);
    assert.deepEqual(await page.locator('.global-agent-session').boundingBox(),before);
    await nav.locator('[data-global-agent="ord"]').hover();
    assert.equal(await nav.isVisible(),true);
    // Popovers remain above unrelated high-z-index UI surfaces.
    await page.evaluate(()=>{const cover=document.createElement('div');cover.id='cover';cover.style='position:fixed;inset:0;z-index:2147483647;background:transparent';document.body.append(cover);});
    assert.equal(await nav.evaluate(el=>{const r=el.getBoundingClientRect();return el.contains(document.elementFromPoint(r.x+20,r.y+20));}),true);
    await page.evaluate(()=>document.querySelector('#cover').remove());
    await page.mouse.move(900,500);
    assert.equal(await nav.isVisible(),false);
    assert.equal(await trigger.getAttribute('aria-expanded'),'false');
    assert.deepEqual(await page.evaluate(()=>mounts),['atlas']);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('the trigger follows agent selection and mouse focus never keeps the menu open',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    const trigger=page.locator('[data-agent-nav-trigger]'),nav=page.locator('.agent-nav-overlay');
    await trigger.hover();
    await nav.locator('[data-global-agent="ord"]').click();
    assert.equal(await trigger.getAttribute('data-selected-agent'),'ord');
    assert.equal(await trigger.locator('[data-profile]').getAttribute('data-profile'),'ord');
    assert.match(await trigger.getAttribute('aria-label'),/Ordubashy selected/);
    await page.mouse.move(900,500);
    assert.equal(await nav.isVisible(),false);
    await trigger.hover();
    await nav.locator('.global-workspace-terminal').click();
    assert.match(await trigger.getAttribute('aria-label'),/Terminal selected/);
    assert.equal(await trigger.locator('.identity-avatar').count(),0);
    assert.equal(await page.locator('.global-agent-session').evaluate(el=>el.getBoundingClientRect().width),1200);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('keyboard opens the menu, Escape restores the trigger, and long rosters scroll',async()=>{
  const {browser,page,errors}=await fixture({many:true});
  try {
    const trigger=page.locator('[data-agent-nav-trigger]'),nav=page.locator('.agent-nav-overlay');
    await trigger.focus();await page.keyboard.press('ArrowDown');
    assert.equal(await nav.isVisible(),true);
    assert.equal(await page.evaluate(()=>document.activeElement.dataset.globalAgent),'atlas');
    assert.ok((await nav.boundingBox()).height<=734);
    assert.ok(await nav.locator('.global-primary').evaluate(el=>el.scrollHeight>el.clientHeight));
    await page.keyboard.press('Escape');
    assert.equal(await nav.isVisible(),false);
    assert.equal(await trigger.evaluate(el=>el===document.activeElement),true);
    await page.keyboard.press('Enter');
    assert.equal(await nav.isVisible(),true);
    await page.locator('.global-agent-session').evaluate(el=>{el.tabIndex=0;el.focus();});
    assert.equal(await nav.isVisible(),false);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('touch opens and dismisses navigation without reserving a mobile sidebar',async()=>{
  const {browser,page,errors}=await fixture({mobile:true});
  try {
    const trigger=page.locator('[data-agent-nav-trigger]'),nav=page.locator('.agent-nav-overlay');
    assert.equal(await nav.isVisible(),false);
    await trigger.tap();assert.equal(await nav.isVisible(),true);
    assert.ok((await nav.boundingBox()).width<=366);
    await trigger.tap();assert.equal(await nav.isVisible(),false);
    await trigger.tap();assert.equal(await nav.isVisible(),true);
    await page.touchscreen.tap(370,450);assert.equal(await nav.isVisible(),false);
    const chat=await page.locator('.global-agent-session').boundingBox();
    assert.equal(chat.x,0);assert.equal(chat.width,390);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});
