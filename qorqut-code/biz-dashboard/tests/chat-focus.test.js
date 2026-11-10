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

async function fixture(width=700) {
  const browser=await chromium.launch({headless:true,executablePath:executable});
  const page=await browser.newPage({viewport:{width,height:700}});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.setContent('<main id="pane" style="display:flex;flex-direction:column;height:650px;width:100%;min-width:0"></main>');
  await page.clock.install();
  for(const file of ['app.css','css/chat-view.css','css/global-chat.css'])await page.addStyleTag({path:path.join(root,file)});
  await page.evaluate(()=>{
    window.esc=value=>String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    window.$=(selector,host=document)=>host.querySelector(selector);
    window.identityAvatar=()=>'<span class="identity-avatar">A</span>';
    window.state={bizs:[]};window.coreData=value=>value;window.evUrl=value=>value;
    window._agpad={agentId:'ord',attentionAgentId:'another-todo-agent'};
    window.EventSource=class extends EventTarget {
      static CLOSED=2;
      constructor(){super();window.stream=this;}
      close(){}
    };
    window.binding={provider:'codex',conversation_id:'conversation',binding_id:'binding'};
    window.working=true;window.focusReads=[];window.override=null;
    window.attentions={
      ord:{state:'focused',focus:{type:'task',id:'launch',label:'Coordinate launch'},intent:'Check the remaining tasks.'},
      atlas:{state:'focused',focus:{type:'task',id:'research',label:'Verify sources'},intent:'Compare the original documents.'}
    };
    window.api=async url=>url.includes('/commands')?{commands:[]}:{session:{state:'live',can_send:true,working,binding},conversation:binding,entries:[],inputs:[],cursor:0};
    window.post=async(url,args,options)=>{
      if(!url.endsWith('/todo_get'))return {};
      focusReads.push(structuredClone(args));
      if(override)return override(args,options);
      return {agent_id:args.agent_id,attention:structuredClone(attentions[args.agent_id])};
    };
    window.workflowPanelMount=()=>({refresh(){},dispose(){}});
  });
  for(const file of ['safe-links','chat-view','slash-commands','global-chat'])await page.addScriptTag({path:path.join(root,'js',file+'.js')});
  await page.evaluate(()=>{
    window.mount=(id='ord',role='ordubashy')=>{
      window.controller?.dispose();
      window.controller=mountGlobalChat(document.querySelector('#pane'),{agent_id:id,name:id,role:{id:role}});
    };
    mount();
  });
  await page.waitForFunction(()=>document.querySelector('.gchat-focus-label')?.textContent==='Coordinate launch');
  return {browser,page,errors};
}

browserTest('chat focus sits beside the working dots above the input and follows chat identity, not Todo',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    assert.equal(await page.locator('.gchat-focus-intent').textContent(),'Check the remaining tasks.');
    const dots=await page.locator('.gchat-working-dots').boundingBox();
    const focus=await page.locator('.gchat-focus').boundingBox();
    const composer=await page.locator('.gchat-composer').boundingBox();
    assert.ok(focus.x>=dots.x+dots.width);
    assert.ok(focus.y+focus.height<=composer.y);
    assert.deepEqual(await page.evaluate(()=>focusReads.map(read=>[read.agent_id,read.projection])),[['ord','focus']]);
    await page.evaluate(()=>{_agpad.attentionAgentId='atlas';});
    await page.clock.runFor(12000);
    assert.equal(await page.locator('.gchat-focus-label').textContent(),'Coordinate launch');
    assert.equal(await page.evaluate(()=>focusReads.every(read=>read.agent_id==='ord')),true);
    await page.locator('.gchat-composer textarea').fill('Unsent draft');
    await page.evaluate(()=>{working=false;stream.dispatchEvent(new MessageEvent('chat',{data:JSON.stringify({kind:'changed',cursor:1})}));});
    await page.waitForFunction(()=>document.querySelector('.gchat-working-tag').hidden);
    assert.equal(await page.locator('.gchat-focus').isVisible(),true);
    assert.equal(await page.locator('.gchat-composer textarea').inputValue(),'Unsent draft');
    await page.evaluate(()=>mount('atlas','global-worker'));
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label')?.textContent==='Verify sources');
    assert.equal(await page.locator('.gchat-focus-intent').textContent(),'Compare the original documents.');
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('missing or unavailable focus is explicit and an error never retains an old intent',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{attentions.ord={state:'unfocused',focus:null,intent:''};});
    await page.clock.runFor(12000);
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent==='No focus set');
    assert.equal(await page.locator('.gchat-focus-intent').isVisible(),false);
    await page.evaluate(()=>{attentions.ord={state:'focused',focus:{unavailable:true},intent:'Must not be shown'};});
    await page.clock.runFor(12000);
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent==='Focus unavailable');
    assert.equal(await page.locator('.gchat-focus-intent').textContent(),'');
    await page.evaluate(()=>{override=async()=>{throw Object.assign(Error('Denied'),{code:'forbidden'});};});
    await page.clock.runFor(12000);
    assert.equal(await page.locator('.gchat-focus-intent').textContent(),'');
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('hidden chats stop focus polling and switching agents discards a late response',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    const count=await page.evaluate(()=>focusReads.length);
    await page.evaluate(()=>{document.querySelector('#pane').hidden=true;});
    await page.clock.runFor(24000);
    assert.equal(await page.evaluate(()=>focusReads.length),count);
    await page.evaluate(()=>{
      document.querySelector('#pane').hidden=false;
      override=(args,options)=>new Promise(resolve=>{window.oldSignal=options.signal;window.finishOld=()=>resolve({agent_id:args.agent_id,attention:{state:'focused',focus:{label:'Old focus'},intent:'Old intent'}});});
    });
    await page.clock.runFor(12000);
    await page.waitForFunction(()=>typeof finishOld==='function');
    await page.evaluate(()=>{override=null;mount('atlas','global-worker');});
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label')?.textContent==='Verify sources');
    assert.equal(await page.evaluate(()=>oldSignal.aborted),true);
    await page.evaluate(()=>finishOld());
    assert.equal(await page.locator('.gchat-focus-intent').textContent(),'Compare the original documents.');
    await page.evaluate(()=>mount('learning','learning-applier'));
    const after=await page.evaluate(()=>focusReads.length);
    await page.clock.runFor(24000);
    assert.equal(await page.evaluate(()=>focusReads.length),after);
    assert.equal(await page.locator('.gchat-focus').isVisible(),false);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('long focus text stays readable on a narrow chat and is rendered only as text',async()=>{
  const {browser,page,errors}=await fixture(320);
  try {
    await page.evaluate(()=>{attentions.ord={state:'focused',focus:{label:'A'.repeat(300)},intent:'<img src=x onerror=alert(1)> '+ 'A long recorded intent. '.repeat(35)};});
    await page.clock.runFor(12000);
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent.length===300);
    assert.equal(await page.locator('.gchat-focus img').count(),0);
    assert.match(await page.locator('.gchat-focus-intent').textContent(),/^<img/);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    const box=await page.locator('.gchat-focus').boundingBox();
    assert.ok(box.height<=90);
    await page.locator('.gchat-focus').focus();await page.keyboard.press('End');
    assert.ok(await page.locator('.gchat-focus').evaluate(el=>el.scrollHeight>el.clientHeight));
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});
