'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
let chromium;
try { ({chromium}=require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable=process.env.CHROME_PATH || chromium?.executablePath();
const available=!!chromium && fs.existsSync(executable);
const browserTest=(name,run)=>test(name,{skip:!available && 'Local Playwright Chromium is unavailable'},run);
const root=path.join(__dirname,'..');

async function fixture(width=900) {
  const browser=await chromium.launch({headless:true,executablePath:executable});
  const page=await browser.newPage({viewport:{width,height:700}});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.setContent('<main id="pane" style="display:flex;flex-direction:column;height:650px;width:100%;min-width:0;--ag-ctl:30px"></main>');
  await page.clock.install();
  for(const file of ['app.css','css/agent-workspace.css','css/agentpane.css','css/chat-view.css','css/workflow-panel.css','css/global-chat.css'])await page.addStyleTag({path:path.join(root,file)});
  await page.evaluate(()=>{
    window.esc=value=>String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    window.$=(selector,host=document)=>host.querySelector(selector);
    window.identityAvatar=(_kind,name,agent)=>'<span class="identity-avatar" data-agent-id="'+esc(agent.agent_id)+'" aria-label="'+esc(name)+'">A</span>';
    window.state={bizs:[]};window.coreData=value=>value;window.evUrl=value=>value;
    window.onChange=()=>()=>{};window.accessLost=e=>['forbidden','invalid_principal'].includes(e?.code);
    window._agpad={agentId:'ord',attentionAgentId:'another-todo-agent'};
    window.EventSource=class extends EventTarget {
      static CLOSED=2;
      constructor(){super();window.stream=this;}
      close(){}
    };
    window.binding={provider:'codex',conversation_id:'conversation',binding_id:'binding'};
    window.working=true;window.focusReads=[];window.todoReads=[];window.actionReads=[];window.override=null;
    window.activePeerReads=0;window.maxPeerReads=0;window.rosterOverride=null;
    window.profile=(id,role='global-worker')=>({agent_id:id,name:id,role:{id:role},scope:{kind:'ecosystem'},status:'active'});
    window.profiles=[profile('ord','ordubashy')];
    window.loadGlobalAgentProfiles=async()=>rosterOverride ? rosterOverride() : structuredClone(profiles);
    window.globalRosterAgent=agent=>agent.status==='active' && agent.scope?.kind==='ecosystem' && ['ordubashy','global-worker','learning-applier'].includes(agent.role?.id);
    window.attentions={
      ord:{state:'focused',focus:{type:'task',id:'launch',label:'Coordinate launch'},intent:'Check the remaining tasks.'},
      atlas:{state:'focused',focus:{type:'task',id:'research',label:'Verify sources'},intent:'Compare the original documents.'}
    };
    window.api=async url=>url.includes('/commands')?{commands:[]}:{session:{state:'live',can_send:true,working,binding},conversation:binding,entries:[],inputs:[],cursor:0};
    window.post=async(url,args,options)=>{
      if(url.endsWith('/actions_list')){actionReads.push(structuredClone(args));return {actions:[]};}
      if(!url.endsWith('/todo_get'))return {};
      if(args.projection!=='focus'){todoReads.push(structuredClone(args));throw Error('Focus board must not load Todo lists');}
      focusReads.push(structuredClone(args));
      const peer=args.agent_id!=='ord';
      if(peer){activePeerReads++;maxPeerReads=Math.max(maxPeerReads,activePeerReads);}
      try {
        await Promise.resolve();
        return override ? await override(args,options) : {agent_id:args.agent_id,attention:structuredClone(attentions[args.agent_id])};
      }finally{if(peer)activePeerReads--;}
    };
  });
  for(const file of ['read-cache','safe-links','san-juan-time','chat-view','slash-commands','workflow-panel','global-chat'])await page.addScriptTag({path:path.join(root,'js',file+'.js')});
  await page.evaluate(()=>{
    window.mount=(id='ord',role='ordubashy')=>{
      window.controller?.dispose();
      window.controller=mountGlobalChat(document.querySelector('#pane'),profile(id,role),{openTerminal:()=>{}});
    };
    mount();
  });
  if(width>700)await page.waitForFunction(()=>document.querySelector('.gchat-focus-label')?.textContent==='Coordinate launch');
  return {browser,page,errors};
}

browserTest('focus strip is the top of the composer and never loads a Todo list',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    assert.equal(await page.locator('.gchat-focus-intent').innerText(),'Check the remaining tasks.');
    assert.equal(await page.locator('.wfpanel-focus-icon .identity-avatar').getAttribute('data-agent-id'),'ord');
    assert.equal(await page.locator('.gchat-focus-label').evaluate(el=>getComputedStyle(el).fontSize),'14px');
    assert.equal(await page.locator('.wfpanel ol,.wfpanel-step,.wfpanel-list-heading').count(),0);
    assert.ok(!(await page.locator('.wfpanel').innerText()).includes('Todo'));
    // One box: the strip is inside the composer form, above the text field, below the feed.
    assert.equal(await page.locator('.gchat-composer > .wfpanel').count(),1);
    const focus=await page.locator('.wfpanel').boundingBox(),feed=await page.locator('.gchat-scroll').boundingBox(),composer=await page.locator('.gchat-composer').boundingBox(),input=await page.locator('.gchat-composer textarea').boundingBox();
    assert.ok(feed.y+feed.height<=composer.y && Math.abs(focus.y-composer.y)<=1.5 && focus.y+focus.height<=input.y);
    assert.ok(focus.x<=composer.x+1.5 && focus.x+focus.width>=composer.x+composer.width-1.5);
    assert.ok(feed.height>200);
    const label=await page.locator('.gchat-focus-label').boundingBox(),dots=await page.locator('.gchat-working-dots').boundingBox();
    assert.ok(Math.abs(dots.y+dots.height/2-(label.y+label.height/2))<1);
    assert.equal(await page.locator('.wfpanel .gchat-working-tag').count(),1);
    assert.equal(await page.locator('.gchat-session').isVisible(),false);
    await page.locator('.gchat-composer textarea').fill('Unsent draft');
    await page.evaluate(()=>{_agpad.attentionAgentId='atlas';working=false;stream.dispatchEvent(new MessageEvent('chat',{data:JSON.stringify({kind:'changed',cursor:1})}));});
    await page.waitForFunction(()=>document.querySelector('.gchat-working-tag').hidden);
    assert.equal(await page.locator('.gchat-focus-label').innerText(),'Coordinate launch');
    assert.equal(await page.locator('.gchat-composer textarea').inputValue(),'Unsent draft');
    assert.deepEqual(await page.evaluate(()=>todoReads),[]);
    assert.deepEqual(await page.evaluate(()=>actionReads),[]);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('clicking focus, icon, board, or peer rows toggles; Enter and Space also work',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{profiles.push(profile('atlas'));mount();});
    await page.waitForFunction(()=>document.querySelector('.wfpanel-agent-focus')?.textContent==='Verify sources');
    await page.locator('.gchat-focus-label').click();
    assert.equal(await page.locator('.wfpanel-head').getAttribute('aria-expanded'),'false');
    assert.equal(await page.locator('.wfpanel-agents').isVisible(),false);
    assert.equal(await page.locator('.gchat-focus-intent').isVisible(),false);
    assert.ok((await page.locator('.wfpanel').boundingBox()).height<55);
    assert.deepEqual(await page.locator('.wfpanel .identity-avatar:visible').evaluateAll(nodes=>nodes.map(el=>el.dataset.agentId)),['ord']);
    await page.locator('.wfpanel-focus-icon').click();
    await page.locator('.wfpanel-agent').click();
    assert.equal(await page.locator('.wfpanel-head').getAttribute('aria-expanded'),'false');
    await page.getByRole('button',{name:'Expand focus details'}).press('Enter');
    assert.equal(await page.locator('.wfpanel-agents').isVisible(),true);
    await page.getByRole('button',{name:'Collapse focus details'}).press('Space');
    assert.equal(await page.locator('.wfpanel-agents').isVisible(),false);
    await page.locator('.wfpanel').click({position:{x:5,y:22}});
    assert.equal(await page.locator('.gchat-focus-intent').isVisible(),true);
    await page.evaluate(()=>{
      const label=document.querySelector('.gchat-focus-label'),selection=window.getSelection(),range=document.createRange();
      range.selectNodeContents(label);selection.removeAllRanges();selection.addRange(range);
      label.dispatchEvent(new MouseEvent('click',{bubbles:true}));
    });
    assert.equal(await page.locator('.wfpanel-head').getAttribute('aria-expanded'),'true');
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('expanded board only shows agents with a focus and hides them when focus clears',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      profiles.push(profile('atlas'),profile('idle'),profile('unavailable'),profile('learning','learning-applier'),{...profile('retired'),status:'retired'});
      attentions.idle={state:'unfocused',focus:null,intent:'Hidden idle intent'};
      attentions.unavailable={state:'focused',focus:{unavailable:true}};mount();
    });
    await page.waitForFunction(()=>document.querySelector('.wfpanel-agent-focus')?.textContent==='Verify sources' && focusReads.some(read=>read.agent_id==='unavailable') && activePeerReads===0);
    assert.deepEqual(await page.locator('.wfpanel-agent').evaluateAll(nodes=>nodes.map(el=>el.dataset.agentId)),['atlas']);
    assert.deepEqual(await page.locator('.wfpanel-agent-focus').allTextContents(),['Verify sources']);
    assert.deepEqual(await page.locator('.wfpanel-agent-name').allTextContents(),['atlas']);
    assert.ok(!(await page.locator('.wfpanel').innerText()).includes('Compare the original documents'));
    assert.equal(await page.evaluate(()=>focusReads.some(read=>['retired','learning'].includes(read.agent_id))),false);
    await page.evaluate(()=>{attentions.idle={state:'focused',focus:{label:'New active focus'}};profiles=profiles.filter(agent=>agent.agent_id!=='atlas');});
    await page.clock.fastForward(12000);
    await page.waitForFunction(()=>document.querySelector('[data-agent-id="idle"] .wfpanel-agent-focus')?.textContent==='New active focus');
    assert.equal(await page.locator('.wfpanel-agent[data-agent-id="atlas"]').count(),0);
    await page.evaluate(()=>{attentions.idle={state:'unfocused',focus:null};});
    await page.clock.fastForward(12000);
    await page.waitForFunction(()=>document.querySelectorAll('.wfpanel-agent').length===0);
    assert.equal(await page.locator('.wfpanel-agents').isVisible(),false);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('worker chats keep Ordubashy first and keep their own working dots by the composer',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{profiles.push(profile('atlas'));mount('atlas','global-worker');});
    await page.waitForFunction(()=>document.querySelector('.wfpanel-agent-focus')?.textContent==='Verify sources');
    assert.equal(await page.locator('.gchat-focus-label').innerText(),'Coordinate launch');
    assert.equal(await page.locator('.wfpanel-focus-icon .identity-avatar').getAttribute('data-agent-id'),'ord');
    assert.equal(await page.locator('.wfpanel .gchat-working-tag').count(),0);
    assert.equal(await page.locator('.gchat-session .gchat-working-tag').getAttribute('aria-label'),'atlas is working');
    assert.equal(await page.locator('.gchat-session .gchat-working-tag').isVisible(),true);
    await page.locator('.gchat-focus-label').click();
    assert.deepEqual(await page.locator('.wfpanel .identity-avatar:visible').evaluateAll(nodes=>nodes.map(el=>el.dataset.agentId)),['ord']);
    assert.equal(await page.locator('[data-global-chat]').getAttribute('data-global-chat'),'atlas');
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('refresh failures preserve the last focus; clearing and denied reads hide intent',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{override=async()=>{throw Error('Offline');};});
    await page.clock.fastForward(12000);
    assert.equal(await page.locator('.gchat-focus-label').innerText(),'Coordinate launch');
    await page.evaluate(()=>{override=null;attentions.ord={state:'unfocused',focus:null,intent:'Hidden idle intent'};});
    await page.clock.fastForward(12000);
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent==='No focus set');
    assert.equal(await page.locator('.gchat-focus-intent').isVisible(),false);
    assert.equal(await page.locator('.gchat-focus-intent').textContent(),'');
    await page.evaluate(()=>{override=async()=>{throw Object.assign(Error('Denied'),{code:'forbidden'});};});
    await page.clock.fastForward(12000);
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent==='Focus unavailable');
    assert.equal(await page.locator('.gchat-focus-intent').isVisible(),false);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('collapse stops peer reads while Ordubashy keeps refreshing; late roster results are ignored',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{profiles.push(profile('atlas'));mount();});
    await page.waitForFunction(()=>document.querySelector('.wfpanel-agent-focus')?.textContent==='Verify sources');
    await page.locator('.gchat-focus-label').click();
    const peers=await page.evaluate(()=>focusReads.filter(read=>read.agent_id!=='ord').length);
    await page.evaluate(()=>{attentions.ord.focus.label='New coordinator focus';attentions.atlas.focus.label='Review evidence';});
    await page.clock.fastForward(24000);
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent==='New coordinator focus');
    assert.equal(await page.evaluate(()=>focusReads.filter(read=>read.agent_id!=='ord').length),peers);
    await page.locator('.gchat-focus-label').click();
    await page.waitForFunction(()=>document.querySelector('.wfpanel-agent-focus')?.textContent==='Review evidence');
    await page.evaluate(()=>{rosterOverride=()=>new Promise(resolve=>window.finishRoster=resolve);});
    await page.clock.fastForward(12000);
    await page.waitForFunction(()=>typeof finishRoster==='function');
    await page.locator('.gchat-focus-label').click();
    await page.evaluate(()=>finishRoster([profile('late')]));
    assert.equal(await page.locator('.gchat-focus-label').innerText(),'New coordinator focus');
    assert.equal(await page.locator('.wfpanel-agent[data-agent-id="late"]').count(),0);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('hidden chats stop reads and disposed boards ignore late results',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    const reads=await page.evaluate(()=>focusReads.length);
    await page.evaluate(()=>{document.querySelector('#pane').hidden=true;});
    await page.clock.fastForward(24000);
    assert.equal(await page.evaluate(()=>focusReads.length),reads);
    await page.evaluate(()=>{
      document.querySelector('#pane').hidden=false;
      override=args=>new Promise(resolve=>window.finishOld=()=>resolve({agent_id:args.agent_id,attention:{state:'focused',focus:{label:'Late focus'}}}));
    });
    await page.clock.fastForward(12000);
    await page.waitForFunction(()=>typeof finishOld==='function');
    await page.evaluate(()=>mount('learning','learning-applier'));
    await page.evaluate(()=>finishOld());
    assert.equal(await page.locator('.wfpanel').count(),0);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('long focus and intent remain readable, bounded, and escaped',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{document.querySelector('#pane').style.width='320px';attentions.ord={state:'focused',focus:{label:'A'.repeat(300)},intent:'<img src=x onerror=alert(1)> '+ 'A long recorded intent. '.repeat(35)};});
    await page.clock.fastForward(12000);
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent.length===300);
    assert.equal(await page.locator('.wfpanel img').count(),0);
    assert.ok((await page.locator('.gchat-focus-intent').textContent()).startsWith('<img src=x onerror=alert(1)>'));
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.ok((await page.locator('.wfpanel').boundingBox()).height<=260);
    const composer=await page.locator('.gchat-composer').boundingBox();assert.ok(composer.y+composer.height<=650);
    await page.locator('.gchat-context').focus();await page.keyboard.press('End');
    assert.ok(await page.locator('.gchat-context').evaluate(el=>el.scrollHeight>el.clientHeight));
    assert.ok(await page.locator('.wfpanel-expanded').evaluate(el=>el.scrollHeight>el.clientHeight));
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('focus paints while chat history is pending and remounts from the shared snapshot',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{controller.dispose();uiReads.invalidate('agent-focus:',{drop:true});api=()=>new Promise(()=>{});focusReads=[];mount();});
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label')?.textContent==='Coordinate launch');
    assert.equal(await page.evaluate(()=>focusReads.length),1);
    assert.equal(await page.evaluate(()=>{mount();return document.querySelector('.gchat-focus-label').textContent;}),'Coordinate launch');
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('workflow focus shows its name and intent without steps or Todo rows',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{attentions.ord={state:'focused',focus:{type:'workflow',label:'Release UI'},intent:'Finish the approved release.',workflow:{steps:[{label:'Must not render',status:'active'}]}};});
    await page.clock.fastForward(12000);
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent==='Release UI');
    assert.equal(await page.locator('.gchat-focus-intent').innerText(),'Finish the approved release.');
    assert.ok(!(await page.locator('.wfpanel').innerText()).includes('Must not render'));
    assert.equal(await page.locator('.wfpanel-step,.wfpanel-steps').count(),0);
    assert.deepEqual(await page.evaluate(()=>todoReads),[]);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('many agents stay scrollable, refresh keeps position, and peer reads are bounded',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      for(let i=0;i<24;i++){profiles.push(profile('worker-'+i));attentions['worker-'+i]={state:'focused',focus:{label:'Focus '+i}};}
      mount();
    });
    await page.waitForFunction(()=>document.querySelector('[data-agent-id="worker-23"] .wfpanel-agent-focus')?.textContent==='Focus 23');
    assert.equal(await page.locator('.wfpanel-agent').count(),24);
    assert.ok(await page.evaluate(()=>maxPeerReads<=3 && maxPeerReads>0));
    assert.ok((await page.locator('.wfpanel').boundingBox()).height<=260);
    await page.locator('.wfpanel-expanded').evaluate(el=>{el.scrollTop=el.scrollHeight;});
    const previous=await page.locator('.wfpanel-expanded').evaluate(el=>el.scrollTop);assert.ok(previous>0);
    await page.evaluate(()=>{attentions['worker-23'].focus.label='Updated last focus';});
    await page.clock.fastForward(12000);
    await page.waitForFunction(()=>document.querySelector('[data-agent-id="worker-23"] .wfpanel-agent-focus')?.textContent==='Updated last focus');
    assert.equal(await page.locator('.wfpanel-expanded').evaluate(el=>el.scrollTop),previous);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('mobile hides the board and stops reads; desktop restores it without losing the draft',async()=>{
  const {browser,page,errors}=await fixture(390);
  try {
    assert.equal(await page.locator('.wfpanel').isVisible(),false);
    assert.equal(await page.locator('.gchat-session .gchat-working-tag').isVisible(),true);
    await page.clock.fastForward(24000);assert.equal(await page.evaluate(()=>focusReads.length),0);
    await page.setViewportSize({width:701,height:700});
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent==='Coordinate launch');
    assert.equal(await page.locator('.wfpanel .gchat-working-tag').isVisible(),true);
    await page.locator('.gchat-composer textarea').fill('Keep my draft');
    await page.setViewportSize({width:700,height:700});
    assert.equal(await page.locator('.wfpanel').isVisible(),false);
    await page.locator('.gchat-session .gchat-working-tag').waitFor({state:'visible'});
    const reads=await page.evaluate(()=>focusReads.length);await page.clock.fastForward(24000);
    assert.equal(await page.evaluate(()=>focusReads.length),reads);
    assert.equal(await page.locator('.gchat-composer textarea').inputValue(),'Keep my draft');
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('the strip has no resize grip and its marks centre on their text line',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{profiles.push(profile('atlas'));mount();});
    await page.waitForFunction(()=>document.querySelector('.wfpanel-agent-focus')?.textContent==='Verify sources');
    assert.equal(await page.locator('[role=separator],.wfpanel-resize').count(),0);
    // The centre of a text is the middle of its capitals where the browser trims text to them, else its line box.
    const offsets=await page.evaluate(()=>{
      const mid=el=>{const r=el.getBoundingClientRect();return r.top+r.height/2;};
      const label=document.querySelector('.gchat-focus-label'),summary=document.querySelector('.wfpanel-agent-summary');
      const marks={avatar:'.wfpanel-focus-icon .identity-avatar',dots:'.wfpanel-context-row .gchat-working-dots',arrow:'.wfpanel-chevron svg'};
      const out=Object.fromEntries(Object.entries(marks).map(([name,q])=>[name,mid(document.querySelector(q))-mid(label)]));
      out.peer=mid(document.querySelector('.wfpanel-agent .identity-avatar'))-mid(summary);
      return out;
    });
    for(const [name,offset] of Object.entries(offsets))assert.ok(Math.abs(offset)<0.6,name+' is '+offset+'px off its text');
    // A focus that wraps keeps the marks on its first line.
    await page.evaluate(()=>{document.querySelector('.gchat-focus-label').textContent='A focus long enough to wrap onto a second line. '.repeat(6);});
    const first=await page.evaluate(()=>{
      const label=document.querySelector('.gchat-focus-label'),range=document.createRange();range.selectNodeContents(label);
      const line=range.getClientRects()[0],avatar=document.querySelector('.wfpanel-focus-icon .identity-avatar').getBoundingClientRect();
      return {lines:label.getClientRects()[0].height>30,inside:avatar.top+avatar.height/2>line.top && avatar.top+avatar.height/2<line.bottom};
    });
    assert.deepEqual(first,{lines:true,inside:true});
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

browserTest('missing Ordubashy is explicit instead of substituting the current worker',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{profiles=[profile('atlas')];mount('atlas','global-worker');});
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent==='Ordubashy unavailable');
    await page.waitForFunction(()=>document.querySelector('.wfpanel-agent-focus')?.textContent==='Verify sources');
    assert.equal(await page.locator('.gchat-focus-intent').isVisible(),false);
    assert.equal(await page.locator('.gchat-session .gchat-working-tag').isVisible(),true);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
