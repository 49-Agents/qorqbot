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
  for(const file of ['app.css','css/context.css','css/global.css','css/agentpane.css','css/agent-workspace.css','css/agent-workspace-nav.css'])await page.addStyleTag({path:path.join(root,file)});
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
    window.coreData=value=>value;window.acOccurred=action=>action.occurred?.date || 'Undated';
    window.actionReads=[];window.post=async(url,body)=>{if(url.endsWith('/global_progress_get'))return {series:Array.from({length:7},(_,i)=>({date:'2026-11-'+String(i+5).padStart(2,'0'),tasks_done:0})),timezone:'UTC'};actionReads.push({url,body});return {actions:[{action_id:body.performer+'-1',text:'Reviewed the plan',occurred:{date:'2026-11-10',precision:'day'}}]};};
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
  await page.addScriptTag({path:path.join(root,'js/san-juan-time.js')});
  await page.clock.setFixedTime(new Date('2026-11-11T18:00:00Z'));
  await page.addScriptTag({path:path.join(root,'js/agent-nav-timeline.js')});
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
    { const box=await trigger.boundingBox(); await page.mouse.move(box.x+box.width/2,box.y+box.height/2); }
    assert.equal(await nav.isVisible(),true);
    assert.equal(await trigger.getAttribute('aria-expanded'),'true');
    assert.deepEqual(await nav.boundingBox(),{x:0,y:0,width:280,height:page.viewportSize().height});
    assert.equal(await trigger.locator('.agent-nav-caret').count(),0);
    assert.equal(await nav.locator('.ctop').count(),0);
    assert.equal(await nav.evaluate(el=>getComputedStyle(el).padding),'12px');
    const row=nav.locator('[data-global-agent="ord"]'),dots=row.locator('.agent-nav-working');
    await dots.evaluate(el=>el.hidden=false);
    const rb=await row.boundingBox(),db=await dots.boundingBox();
    assert.ok(Math.abs(db.y+db.height/2-rb.y-rb.height/2)<1);
    assert.ok(db.x>rb.x+rb.width-45);
    assert.equal((await dots.locator('i').first().boundingBox()).width,5);
    assert.equal(await nav.evaluate(el=>getComputedStyle(el,'::backdrop').backdropFilter),'blur(6px)');
    assert.deepEqual(await page.locator('.global-agent-session').boundingBox(),before);
    await nav.locator('[data-global-agent="ord"]').hover();
    assert.equal(await nav.isVisible(),true);
    // Popovers remain above unrelated high-z-index UI surfaces.
    await page.evaluate(()=>{const cover=document.createElement('div');cover.id='cover';cover.style='position:fixed;inset:0;z-index:2147483647;background:transparent';document.body.append(cover);});
    assert.equal(await nav.evaluate(el=>{const r=el.getBoundingClientRect();return el.contains(document.elementFromPoint(r.x+20,r.y+20));}),true);
    await page.evaluate(()=>document.querySelector('#cover').remove());
    await page.mouse.move(300,780);await page.waitForFunction(()=>!document.querySelector('.agent-nav-overlay').matches(':popover-open'));
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
    { const box=await trigger.boundingBox(); await page.mouse.move(box.x+box.width/2,box.y+box.height/2); }
    await nav.locator('[data-global-agent="ord"]').click();
    assert.equal(await trigger.getAttribute('data-selected-agent'),'ord');
    assert.equal(await trigger.locator('[data-profile]').getAttribute('data-profile'),'ord');
    assert.match(await trigger.getAttribute('aria-label'),/Ordubashy selected/);
    await page.mouse.move(300,780);await page.waitForFunction(()=>!document.querySelector('.agent-nav-overlay').matches(':popover-open'));
    assert.equal(await nav.isVisible(),false);
    { const box=await trigger.boundingBox(); await page.mouse.move(box.x+box.width/2,box.y+box.height/2); }
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
    assert.equal((await nav.boundingBox()).height,page.viewportSize().height);
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
    await page.touchscreen.tap(370,450);assert.equal(await nav.isVisible(),false);
    await trigger.tap();assert.equal(await nav.isVisible(),true);
    await page.touchscreen.tap(370,450);assert.equal(await nav.isVisible(),false);
    const chat=await page.locator('.global-agent-session').boundingBox();
    assert.equal(chat.x,0);assert.equal(chat.width,390);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

 browserTest('actions pane shares the top layer, follows selection, and filters the chart',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    const nav=page.locator('.agent-nav-overlay'),pane=page.locator('.agent-nav-timeline');
    assert.equal(await page.evaluate(()=>actionReads.length),0);
    await page.mouse.move(34,32);
    await pane.locator('.agent-nav-events li').waitFor();
    assert.equal(await pane.locator('[data-agent]').innerText(),'Atlas');
    assert.equal(await page.evaluate(()=>actionReads[0].body.performer),'atlas');
    const bounds=await pane.boundingBox();assert.ok(bounds.x>=280);assert.ok(bounds.x+bounds.width<=1200);
    await page.mouse.move(bounds.x+30,bounds.y+30);await page.waitForTimeout(450);
    assert.equal(await nav.isVisible(),true);
    await pane.locator('.agent-nav-chart-day').first().click();
    assert.equal(await pane.locator('.agent-nav-events li').count(),0);
    await pane.locator('[data-reset]').click();assert.equal(await pane.locator('.agent-nav-events li').count(),1);
    await page.evaluate(()=>{const cover=document.createElement('div');cover.id='cover';cover.style='position:fixed;inset:0;z-index:2147483647';document.body.append(cover);});
    assert.equal(await pane.evaluate(el=>{const r=el.getBoundingClientRect();return el.contains(document.elementFromPoint(r.x+20,r.y+20));}),true);
    await page.evaluate(()=>document.querySelector('#cover').remove());
    await nav.locator('[data-global-agent="ord"]').click();
    await page.waitForFunction(()=>actionReads.at(-1)?.body.performer==='ord');
    assert.equal(await pane.locator('[data-agent]').innerText(),'Ordubashy');
    await page.keyboard.press('Escape');assert.equal(await nav.isVisible(),false);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
 });

browserTest('late action reads cannot replace another agent and failed reads stay distinct from empty',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{window.held=[];window.post=(url,body)=>url.endsWith('/global_progress_get')?Promise.resolve({series:[{date:'2026-11-11',tasks_done:0}],timezone:'UTC'}):new Promise((resolve,reject)=>held.push({body,resolve,reject}));});
    await page.mouse.move(34,32);
    await page.waitForFunction(()=>held.length===1);
    await page.locator('[data-global-agent="ord"]').click();
    await page.waitForFunction(()=>held.length===2);
    await page.evaluate(()=>{
      held[1].resolve({actions:[{action_id:'new',text:'Current agent action',occurred:{date:'2026-11-10'}}]});
      held[0].resolve({actions:[{action_id:'old',text:'Stale action',occurred:{date:'2026-11-10'}}]});
    });
    await page.locator('.agent-nav-events li').waitFor();
    assert.equal(await page.locator('.agent-nav-events').innerText().then(text=>text.includes('Stale action')),false);
    await page.locator('.agent-nav-timeline [data-refresh]').click();
    await page.waitForFunction(()=>held.length===3);await page.evaluate(()=>held[2].reject(Error('Offline')));
    await page.waitForFunction(()=>document.querySelector('.agent-nav-timeline [data-status]').textContent.includes('unavailable'));
    assert.equal(await page.locator('.agent-nav-events li').count(),0);
    await page.locator('.agent-nav-timeline [data-refresh]').click();
    await page.waitForFunction(()=>held.length===4);
    await page.keyboard.press('Escape');
    await page.evaluate(()=>held[3].resolve({actions:[{action_id:'late',text:'Hidden action'}]}));
    assert.equal(await page.locator('.agent-nav-overlay').isVisible(),false);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});


browserTest('task chart uses historical daily counts independently of Todo membership',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      window.progressReads=[];const original=post;
      window.post=async(url,body)=>{
        if(!url.endsWith('/global_progress_get'))return {...await original(url,body),next_cursor:'older-actions'};
        progressReads.push(body);
        return {series:Array.from({length:30},(_,i)=>({date:sjAddDays('2026-11-11',i-29),tasks_done:i===28?12:i===29?3:0})),timezone:'America/La_Paz'};
      };
    });
    await page.mouse.move(34,32);
    const chart=page.locator('.agent-nav-tasks-chart');await chart.waitFor();
    assert.equal(await chart.locator('[aria-label="2026-11-10: 12 completed tasks"]').count(),1);
    assert.equal(await chart.locator('[aria-label="2026-11-11: 3 completed tasks"]').count(),1);
    assert.equal(await chart.locator('.agent-nav-chart-day').count(),7);
    assert.deepEqual(await page.evaluate(()=>progressReads),[{days:30}]);
    assert.match(await page.locator('[data-tasks-status]').innerText(),/Across workspaces.*America\/La_Paz/);
    const pane=page.locator('.agent-nav-timeline'),more=pane.locator('[data-more]');
    assert.equal((await pane.boundingBox()).width,760);
    assert.ok((await more.boundingBox()).height>=42);
    assert.equal(await more.evaluate(el=>getComputedStyle(el).borderRadius),'12px');
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('historical task reads discard stale responses and distinguish errors from zero completions',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      const original=post;window.progressHeld=[];
      window.post=(url,body)=>url.endsWith('/global_progress_get')?new Promise((resolve,reject)=>progressHeld.push({resolve,reject})):original(url,body);
    });
    await page.mouse.move(34,32);await page.waitForFunction(()=>progressHeld.length===1);
    await page.locator('[data-global-agent="ord"]').click();await page.waitForFunction(()=>progressHeld.length===2);
    await page.evaluate(()=>{
      progressHeld[1].resolve({series:[{date:'2026-11-11',tasks_done:0}]});
      progressHeld[0].resolve({series:[{date:'2026-11-11',tasks_done:99}]});
    });
    const chart=page.locator('.agent-nav-tasks-chart');await chart.waitFor();
    assert.deepEqual(await chart.locator('.agent-nav-chart-count').allTextContents(),['0']);
    await page.locator('[data-refresh]').click();await page.waitForFunction(()=>progressHeld.length===3);
    await page.evaluate(()=>progressHeld[2].reject(Error('Offline')));
    await page.waitForFunction(()=>document.querySelector('[data-tasks-status]').textContent.includes('unavailable'));
    assert.equal(await chart.isVisible(),false);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
