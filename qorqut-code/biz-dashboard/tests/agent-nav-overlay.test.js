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
  for(const file of ['app.css','css/context.css','css/global.css','css/global-chat.css','css/agentpane.css','css/agent-workspace.css','css/agent-workspace-nav.css','css/agent-timeline.css'])await page.addStyleTag({path:path.join(root,file)});
  await page.evaluate(many=>{
    window.$=(selector,host=document)=>host.querySelector(selector);
    window.esc=value=>String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    window.state={scope:'global',globalTab:'agents'};
    window.saved=new Map([['ag:scope:global','atlas'],['agent-nav:global','0']]);
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

async function reopenNav(page) {
  await page.evaluate(()=>{
    const nav=document.querySelector('.agent-nav-overlay');
    window.navClosed=new Promise(resolve=>nav.addEventListener('toggle',function closed(event){
      if(event.newState!=='closed')return;
      nav.removeEventListener('toggle',closed);resolve();
    }));
  });
  await page.keyboard.press('Escape');await page.evaluate(()=>navClosed);
  await page.locator('[data-agent-nav-trigger]').focus();await page.keyboard.press('Enter');
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
    assert.deepEqual(await nav.boundingBox(),{x:0,y:0,width:400,height:page.viewportSize().height});
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
    await page.mouse.move(420,780);await page.waitForFunction(()=>!document.querySelector('.agent-nav-overlay').matches(':popover-open'));
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
    await page.mouse.move(420,780);await page.waitForFunction(()=>!document.querySelector('.agent-nav-overlay').matches(':popover-open'));
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
    assert.equal(await pane.locator('h2').innerText(),'Weekly Report');
    assert.equal(await pane.locator('[data-agent]').count(),0);
    assert.equal(await page.evaluate(()=>actionReads[0].body.performer),'atlas');
    const bounds=await pane.boundingBox();assert.ok(bounds.x>=400);assert.ok(bounds.x+bounds.width<=1200);
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
    await reopenNav(page);
    await page.waitForFunction(()=>held.length===3);await page.evaluate(()=>held[2].reject(Error('Offline')));
    await page.waitForFunction(()=>document.querySelector('.agent-nav-timeline [data-status]').textContent.includes('unavailable'));
    assert.equal(await page.locator('.agent-nav-events li').count(),0);
    await reopenNav(page);
    await page.waitForFunction(()=>held.length===4);
    await page.keyboard.press('Escape');
    await page.evaluate(()=>held[3].resolve({actions:[{action_id:'late',text:'Hidden action'}]}));
    assert.equal(await page.locator('.agent-nav-overlay').isVisible(),false);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('weekly action totals load every page before showing counts or filtering earlier days',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      const original=post;window.weekReads=[];
      window.post=(url,body)=>{
        if(!url.endsWith('/actions_list'))return original(url,body);
        weekReads.push(body);
        if(body.cursor)return new Promise(resolve=>{window.finishWeek=resolve;});
        return Promise.resolve({actions:Array.from({length:200},(_,i)=>({
          action_id:'today-'+i,text:'Today '+i,occurred:{date:'2026-11-11'},
        })),next_cursor:'older-actions'});
      };
    });
    await page.mouse.move(34,32);await page.waitForFunction(()=>weekReads.length===2);
    const pane=page.locator('.agent-nav-timeline'),chart=pane.locator('.agent-nav-chart').first();
    assert.equal(await chart.isVisible(),false);
    assert.equal(await pane.locator('.agent-nav-events li').count(),0);
    assert.equal(await pane.locator('[data-status]').innerText(),'Loading actions…');
    assert.deepEqual(await page.evaluate(()=>weekReads),[
      {performer:'atlas',since:'2026-11-05',until:'2026-11-11',limit:200},
      {performer:'atlas',since:'2026-11-05',until:'2026-11-11',limit:200,cursor:'older-actions'},
    ]);
    await page.evaluate(()=>finishWeek({actions:[
      {action_id:'today-199',text:'Duplicate from the first page',occurred:{date:'2026-11-11'}},
      ...Array.from({length:25},(_,i)=>({action_id:'extra-'+i,text:'Another action',occurred:{date:'2026-11-11'}})),
      ...Array.from({length:3},(_,i)=>({action_id:'yesterday-'+i,text:'Yesterday',occurred:{date:'2026-11-10'}})),
      {action_id:'first-day',text:'Start of the week',occurred:{date:'2026-11-05'}},
    ],next_cursor:null}));
    await chart.waitFor();
    assert.deepEqual(await chart.locator('.agent-nav-chart-count').allTextContents(),['1','0','0','0','0','3','225']);
    assert.equal(await pane.locator('.agent-nav-events li').count(),229);
    await chart.locator('[aria-label="2026-11-10: 3 recorded actions"]').click();
    assert.equal(await pane.locator('.agent-nav-events li').count(),3);
    assert.deepEqual(await pane.locator('.agent-nav-events time').allTextContents(),Array(3).fill('2026-11-10'));
    await chart.locator('[aria-label="2026-11-05: 1 recorded actions"]').click();
    assert.equal(await pane.locator('.agent-nav-events').innerText(),'2026-11-05\n\nStart of the week');
    await pane.locator('[data-reset]').click();assert.equal(await pane.locator('.agent-nav-events li').count(),229);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('weekly actions stay anchored to today in the installation timezone, including empty weeks',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.clock.setFixedTime(new Date('2026-11-12T02:00:00Z'));
    await page.evaluate(()=>sjConfigure('America/La_Paz'));
    await page.mouse.move(34,32);
    const pane=page.locator('.agent-nav-timeline'),chart=pane.locator('.agent-nav-chart').first();await chart.waitFor();
    assert.deepEqual(await page.evaluate(()=>actionReads[0].body),
      {performer:'atlas',since:'2026-11-05',until:'2026-11-11',limit:200});
    assert.equal(await chart.locator('.agent-nav-chart-day').last().getAttribute('aria-label'),'2026-11-11: 0 recorded actions');
    assert.equal(await chart.locator('[aria-label="2026-11-10: 1 recorded actions"]').count(),1);
    await page.evaluate(()=>{const original=post;window.post=(url,body)=>url.endsWith('/actions_list')?Promise.resolve({actions:[]}):original(url,body);});
    await reopenNav(page);await page.waitForFunction(()=>document.querySelector('[data-status]').textContent.includes('No actions'));
    assert.deepEqual(await chart.locator('.agent-nav-chart-count').allTextContents(),Array(7).fill('0'));
    assert.equal(await pane.locator('[data-status]').innerText(),'No actions recorded in the last 7 days.');
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('failed or looping action pages never expose partial weekly counts and can be retried',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      const original=post;window.loopPages=false;window.pageReads=[];
      window.post=(url,body)=>{
        if(!url.endsWith('/actions_list'))return original(url,body);
        pageReads.push(body);
        if(body.cursor && !loopPages)return Promise.reject(Error('Offline'));
        return Promise.resolve({actions:[{action_id:'partial',text:'Partial week',occurred:{date:'2026-11-11'}}],next_cursor:'older-actions'});
      };
    });
    const pane=page.locator('.agent-nav-timeline'),chart=pane.locator('.agent-nav-chart').first();
    await page.mouse.move(34,32);await page.waitForFunction(()=>document.querySelector('[data-status]').textContent.includes('unavailable'));
    assert.equal(await chart.isVisible(),false);
    assert.equal(await pane.locator('.agent-nav-events li').count(),0);
    assert.equal(await page.locator('.agent-nav-tasks-chart').isVisible(),true);
    await page.evaluate(()=>{window.loopPages=true;window.pageReads=[];});
    await reopenNav(page);await page.waitForFunction(()=>pageReads.length>=2 && document.querySelector('[data-status]').textContent.includes('unavailable'));
    assert.equal(await page.evaluate(()=>pageReads.length),2);
    assert.equal(await chart.isVisible(),false);
    await page.evaluate(()=>{const original=post;window.post=(url,body)=>url.endsWith('/actions_list')?Promise.resolve({actions:[]}):original(url,body);});
    await reopenNav(page);await chart.waitFor();
    assert.deepEqual(await chart.locator('.agent-nav-chart-count').allTextContents(),Array(7).fill('0'));
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('switching agents or closing navigation cancels older weekly action pages',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      const original=post;window.weekReads=[];window.weekHeld=[];
      window.post=(url,body,options)=>{
        if(!url.endsWith('/actions_list'))return original(url,body);
        weekReads.push(body);
        if(body.cursor)return new Promise(resolve=>weekHeld.push({resolve,signal:options.signal}));
        return Promise.resolve({actions:[{action_id:body.performer,text:body.performer,occurred:{date:'2026-11-11'}}],
          next_cursor:body.performer==='atlas'?'older-actions':null});
      };
    });
    await page.mouse.move(34,32);await page.waitForFunction(()=>weekHeld.length===1);
    await page.locator('[data-global-agent="ord"]').click();
    await page.locator('.agent-nav-events li').waitFor();
    assert.equal(await page.evaluate(()=>weekHeld[0].signal.aborted),true);
    await page.evaluate(()=>weekHeld[0].resolve({actions:[{action_id:'stale',text:'Stale action',occurred:{date:'2026-11-10'}}],next_cursor:'never-read'}));
    assert.equal(await page.evaluate(()=>weekReads.length),3);
    assert.equal(await page.locator('.agent-nav-events p').innerText(),'ord');
    await page.locator('[data-global-agent="atlas"]').click();await page.waitForFunction(()=>weekHeld.length===2);
    await page.keyboard.press('Escape');
    await page.waitForFunction(()=>weekHeld[1].signal.aborted);
    assert.equal(await page.evaluate(()=>weekHeld[1].signal.aborted),true);
    await page.evaluate(()=>weekHeld[1].resolve({actions:[],next_cursor:'never-read'}));
    assert.equal(await page.evaluate(()=>weekReads.length),5);
    assert.equal(await page.locator('.agent-nav-events li').count(),0);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});


browserTest('task chart uses historical daily counts independently of Todo membership',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      window.progressReads=[];const original=post;
      window.post=async(url,body)=>{
        if(!url.endsWith('/global_progress_get'))return {...await original(url,body),next_cursor:body.cursor?null:'older-actions'};
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
    assert.match(await page.locator('[data-tasks-status]').innerText(),/^Last 7 days · America\/La_Paz$/);
    const pane=page.locator('.agent-nav-timeline');
    assert.equal((await pane.boundingBox()).width,Math.min(960,page.viewportSize().width-464));
    assert.equal(await pane.locator('[data-refresh]').count(),0);
    assert.equal(await pane.locator('[data-more]').count(),0);
    assert.equal(await pane.locator('[data-status]').isVisible(),false);
    await page.waitForTimeout(250);
    const bounds=await pane.boundingBox(),viewport=page.viewportSize();
    assert.equal(bounds.y,32);
    assert.equal(viewport.height-bounds.y-bounds.height,32);
    assert.equal(viewport.width-bounds.x-bounds.width,32);
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
    await reopenNav(page);await page.waitForFunction(()=>progressHeld.length===3);
    await page.evaluate(()=>progressHeld[2].reject(Error('Offline')));
    await page.waitForFunction(()=>document.querySelector('[data-tasks-status]').textContent.includes('unavailable'));
    assert.equal(await chart.isVisible(),false);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});


browserTest('navigation shows safe focus previews and unread dots follow working dots',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      window.fullFocus='Review <draft> '+ '🚀'.repeat(50);
      window.agentFocusRead=async id=>({agent_id:id,attention:id==='ord'?{state:'focused',focus:{label:fullFocus}}:{state:'idle'}});
      window.globalAgentProfile=id=>globalAgentProfiles.find(agent=>agent.agent_id===id);
      window.loadGlobalAgentProfiles=async()=>globalAgentProfiles;
      window.api=async()=>({items:[{id:'reply',kind:'global',agent_id:'ord',chat:true,unread:true}]});
    });
    await page.addScriptTag({path:path.join(root,'js/reply-notifications.js')});
    await page.evaluate(()=>replyNotifications.refresh());
    await page.mouse.move(34,32);
    await page.waitForFunction(()=>document.querySelector('[data-agent-focus="ord"]').textContent.endsWith('...'));
    assert.equal(await page.locator('[data-agent-focus="ord"]').innerText(),await page.evaluate(()=>Array.from(fullFocus).slice(0,50).join('')+'...'));
    assert.equal(await page.locator('[data-agent-focus="ord"]').getAttribute('title'),await page.evaluate(()=>fullFocus));
    assert.equal(await page.locator('[data-agent-focus="ord"] draft').count(),0);
    assert.equal(await page.locator('[data-agent-focus="atlas"]').innerText(),'-');
    const row=page.locator('[data-global-agent="ord"]'),dots=row.locator('.agent-nav-working'),unread=row.locator('.chat-unread-dot');
    await dots.evaluate(el=>el.hidden=false);
    const working=await dots.boundingBox(),notification=await unread.boundingBox();
    assert.ok(notification.x>=working.x+working.width);
    const bounds=await row.boundingBox();
    assert.ok(notification.y < working.y);
    assert.ok(Math.abs(notification.y-bounds.y-8)<2);
    assert.ok(Math.abs(bounds.x+bounds.width-notification.x-notification.width-10)<2);
    assert.equal(await row.locator('.nm > .chat-unread-dot').count(),0);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('focus preview reads stop scheduling after navigation closes',async()=>{
  const {browser,page,errors}=await fixture({many:true});
  try {
    await page.evaluate(()=>{window.focusHeld=[];window.agentFocusRead=id=>new Promise(resolve=>focusHeld.push({id,resolve}));});
    await page.mouse.move(34,32);await page.waitForFunction(()=>focusHeld.length===3);
    await page.mouse.move(420,780);await page.waitForFunction(()=>!document.querySelector('.agent-nav-overlay').matches(':popover-open'));
    await page.evaluate(()=>focusHeld.forEach(read=>read.resolve({attention:{state:'focused',focus:{label:'Stale focus'}}})));
    await page.waitForTimeout(50);
    assert.equal(await page.evaluate(()=>focusHeld.length),3);
    assert.equal(await page.locator('[data-agent-focus]').first().textContent(),'Loading focus…');
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
