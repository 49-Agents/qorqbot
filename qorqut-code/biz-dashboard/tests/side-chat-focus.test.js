'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
let chromium;try{({chromium}=require('playwright'));}catch(_){}
const executable=process.env.CHROME_PATH || chromium?.executablePath();
const browserTest=(name,run)=>test(name,{skip:!chromium || !fs.existsSync(executable)},run);
const root=path.join(__dirname,'..');
async function fixture(){
 const browser=await chromium.launch({headless:true,executablePath:executable});const page=await browser.newPage({viewport:{width:1200,height:850}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));await page.clock.install();
 await page.setContent('<main style="display:flex;height:780px"><section style="width:45%"><textarea aria-label="Main chat draft"></textarea></section><aside class="agfocus" style="width:55%;display:flex;flex-direction:column"><section class="agfocus-surface" hidden><header class="agfocus-surface-bar"><button data-surface-close>Back</button><span class="agfocus-surface-title"></span></header><div class="agfocus-surface-body"></div></section><div class="agfocus-layout">Focus list</div></aside></main>');
 for(const file of ['app.css','css/agent-focus.css','css/conversation.css','css/chat-view.css','css/signoffs.css'])await page.addStyleTag({path:path.join(root,file)});
 await page.evaluate(()=>{
  window.$=(selector,host=document)=>host.querySelector(selector);window.state={scope:'global',globalTab:'agents',bizs:[{id:'p',slug:'project'}]};
  window.coreData=value=>value;window.uiFailure=e=>e.message;window.newRequestId=(()=>{let n=0;return()=>String(++n);})();window.toast=()=>{};
  window.modal=()=>{throw Error('Side chat must not open a modal');};window.calls=[];window.hold=null;window.failCreate=false;
  window.renameReceipts=new Map();window.failRename=false;window.holdRename=false;
  window.post=async(url,body)=>{
   calls.push({url,body});
   if(url.endsWith('/side_chats_create')){if(hold)return new Promise(resolve=>window.finishCreate=resolve);if(failCreate)throw Error('Offline');return {signoff:{id:'created',title:body.title,review_business_id:null}};}
   if(url.endsWith('/side_chats_rename')){
    if(holdRename)await new Promise(resolve=>window.finishRename=resolve);
    if(!renameReceipts.has(body.idempotency_key))renameReceipts.set(body.idempotency_key,{signoff:{id:body.signoff_id,title:body.title}});
    if(failRename){failRename=false;throw Error('Lost rename response');}
    return renameReceipts.get(body.idempotency_key);
   }
   if(url.endsWith('/owner_conversation_get'))return {messages:[],conversation:{latest_sequence:0,message_count:0}};
   return {};
  };
  window.attentionQueue={refresh:async()=>{}};window.cardUnsaved=false;window.cardPending=false;
  window.sgChatMount=()=>({hasUnsaved:()=>cardUnsaved,hasPendingWrite:()=>cardPending,dispose(){},refresh(){}});
  window._agpad={agentId:'ord',agent:'Ordubashy',open:true,view:'focus',views:['focus','browser'],workspaceRole:'ordubashy',focusMount:document.querySelector('.agfocus'),el:document.querySelector('aside')};
  window.AGPAD_FOCUS_SURFACES=new Set(['browser','desktop','viewbox']);window.agentPaneViewAllowed=()=>true;window.agpadFocusSurfaces=()=>true;window.agpadActive=p=>p===_agpad;
  window.agpadRememberView=()=>{};window.agfocusPaint=()=>{};window.agpadAnimateContent=()=>{};
 });
 for(const name of ['safe-links','san-juan-time','chat-view','conversation'])await page.addScriptTag({path:path.join(root,'js',name+'.js')});
 await page.evaluate(()=>window.esc=safeLinkEsc);
 const pane=fs.readFileSync(path.join(root,'js/agentpane.js'),'utf8');await page.addScriptTag({content:pane.slice(pane.indexOf('// ------------------------------------------------------------ Focus nested surfaces'),pane.indexOf('function agpadVisibility('))});
 const focus=fs.readFileSync(path.join(root,'js/agent-focus.js'),'utf8'),escapeStart=focus.indexOf('  const escape=event=>{');
 await page.addScriptTag({content:'{const p=_agpad;'+focus.slice(escapeStart,focus.indexOf("  document.addEventListener('keydown',escape);",escapeStart))+"document.addEventListener('keydown',escape);}"});
 await page.addScriptTag({path:path.join(root,'js/side-chats.js')});await page.evaluate(()=>document.querySelector('[data-surface-close]').onclick=()=>agpadSurfaceClose(_agpad));
 return {browser,page,errors};
}
browserTest('new side chat uses Focus, retains drafts, and retries create without duplicating it',async()=>{
 const {browser,page,errors}=await fixture();try{
  await page.evaluate(()=>sideChatCreate());await page.getByRole('textbox',{name:'Title (optional)'}).fill('A question');await page.getByRole('textbox',{name:'First message'}).fill('Please check this');
  await page.locator('[data-surface-close]').click();assert.equal(await page.locator('.agfocus-surface').isVisible(),false);
  await page.evaluate(()=>sideChatCreate());assert.equal(await page.getByRole('textbox',{name:'First message'}).inputValue(),'Please check this');
  await page.evaluate(()=>failCreate=true);await page.locator('.side-chat-create button').click();await page.getByText('Offline',{exact:true}).waitFor();
  await page.evaluate(()=>failCreate=false);await page.getByRole('button',{name:'Retry send',exact:true}).click();await page.locator('.side-chat-thread').waitFor();
  assert.equal(await page.locator('.agfocus-surface-title').innerText(),'A question');
  assert.equal(await page.locator('.overlay,.modal').count(),0);assert.equal(await page.locator('.side-chat-create').count(),0);
  const creates=await page.evaluate(()=>calls.filter(c=>c.url.endsWith('/side_chats_create')));assert.equal(creates.length,2);assert.equal(creates[0].body.idempotency_key,creates[1].body.idempotency_key);
  await page.getByRole('textbox',{name:'Main chat draft'}).fill('Main chat stays usable');assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});
browserTest('Focus conversation preserves drafts, stops hidden polling, and closes the exact side chat separately',async()=>{
 const {browser,page,errors}=await fixture();try{
  await page.evaluate(()=>sideChatOpen({id:'one',title:'Project question',source:{type:'signoff',id:'one',business_id:'p'}}));
  await page.getByRole('textbox',{name:'Side chat message'}).fill('Unsent reply');
  const pane=await page.locator('.agfocus-surface').boundingBox(),composer=await page.getByRole('textbox',{name:'Side chat message'}).boundingBox();assert.ok(composer.y+composer.height<=pane.y+pane.height+1);
  await page.locator('[data-surface-close]').click();
  const count=await page.evaluate(()=>calls.length);await page.clock.fastForward(20000);assert.equal(await page.evaluate(()=>calls.length),count);
  assert.equal(await page.evaluate(()=>calls.some(c=>c.url.endsWith('/side_chats_close'))),false);
  await page.evaluate(()=>sideChatOpen({id:'two',title:'Other question'}));assert.equal(await page.getByRole('textbox',{name:'Side chat message'}).inputValue(),'');
  await page.evaluate(()=>sideChatOpen({id:'one',title:'Project question',source:{type:'signoff',id:'one',business_id:'p'}}));assert.equal(await page.getByRole('textbox',{name:'Side chat message'}).inputValue(),'Unsent reply');
  await page.getByRole('button',{name:'Close side chat',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.agfocus-surface').hidden);
  const closed=await page.evaluate(()=>calls.filter(c=>c.url.endsWith('/side_chats_close')));assert.equal(closed.length,1);assert.equal(closed[0].url,'/biz/project/operations/side_chats_close');assert.equal(closed[0].body.signoff_id,'one');assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});
browserTest('pending sends and question edits block leaving inline; detached creates do not displace newer content',async()=>{
 const {browser,page,errors}=await fixture();try{
  await page.evaluate(()=>{hold=true;return sideChatCreate();});await page.getByRole('textbox',{name:'First message'}).fill('Question');await page.locator('.side-chat-create button').click();
  await page.waitForFunction(()=>typeof finishCreate==='function');await page.locator('[data-surface-close]').click();assert.equal(await page.locator('.side-chat-create').isVisible(),true);
  await page.evaluate(()=>{agpadSurfaceDispose(_agpad);return focusExpansion.open({title:'Newer view',mount:host=>{host.textContent='Newer content';}});});
  await page.evaluate(()=>finishCreate({signoff:{id:'late',title:'Late result'}}));await page.waitForTimeout(20);assert.equal(await page.locator('.agfocus-surface-title').innerText(),'Newer view');
  await page.evaluate(()=>sideChatOpen({id:'one',title:'Question'}));
  await page.evaluate(()=>{const view=document.querySelector('.side-chat-thread')._conversation;view.state.sending=true;});
  await page.locator('[data-surface-close]').click();assert.equal(await page.locator('.side-chat-thread').isVisible(),true);
  await page.evaluate(()=>{const view=document.querySelector('.side-chat-thread')._conversation;view.state.sending=false;view.state.inputRequests=[{type:'signoff',id:'question'}];cardUnsaved=true;view._paint();});
  await page.locator('[data-surface-close]').click();assert.equal(await page.locator('.side-chat-thread').isVisible(),true);assert.equal(await page.locator('.overlay,.modal').count(),0);
  await page.evaluate(()=>cardUnsaved=false);await page.locator('[data-surface-close]').click();assert.equal(await page.locator('.agfocus-surface').isVisible(),false);assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});

browserTest('clicking the name selects it; Enter and blur save while the message draft remains',async()=>{
 const {browser,page,errors}=await fixture();try{
  await page.evaluate(()=>{window.chatItem={id:'one',title:'Older chat'};return sideChatOpen(chatItem);});
  const heading=page.locator('.side-chat-title-button'),input=page.getByRole('textbox',{name:'Side chat name',exact:true});
  const composer=page.getByRole('textbox',{name:'Side chat message',exact:true});
  await composer.fill('Keep this draft');await heading.click();
  assert.deepEqual(await input.evaluate(el=>({focused:document.activeElement===el,start:el.selectionStart,end:el.selectionEnd})),{focused:true,start:0,end:10});
  await input.fill('  Budget <2027> & plans  ');await input.press('Enter');
  await page.waitForFunction(()=>document.querySelector('.side-chat-title-button').textContent==='Budget <2027> & plans');
  assert.equal(await page.locator('.agfocus-surface').getAttribute('aria-label'),'Budget <2027> & plans');
  assert.equal(await composer.inputValue(),'Keep this draft');
  await heading.click();await input.fill('Saved on blur');await composer.click();
  await page.waitForFunction(()=>document.querySelector('.side-chat-title-button').textContent==='Saved on blur');
  await page.locator('[data-surface-close]').click();await page.evaluate(()=>sideChatOpen(chatItem));
  assert.equal(await heading.innerText(),'Saved on blur');assert.equal(await composer.inputValue(),'Keep this draft');
  const renames=await page.evaluate(()=>calls.filter(c=>c.url.endsWith('/side_chats_rename')));
  assert.equal(renames.length,2);assert.equal(renames[0].body.title,'Budget <2027> & plans');
  assert.equal(renames[0].body.scope,'global');assert.equal(renames[0].body.signoff_id,'one');
  await heading.click();await input.fill('A long name '.repeat(18));await page.setViewportSize({width:600,height:850});
  assert.equal(await input.evaluate(el=>el.getBoundingClientRect().right<=innerWidth),true);
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});

browserTest('Escape cancels name editing without leaving the chat, and blank names are not saved',async()=>{
 const {browser,page,errors}=await fixture();try{
  await page.evaluate(()=>sideChatOpen({id:'one',title:'Older chat'}));
  const heading=page.locator('.side-chat-title-button'),input=page.getByRole('textbox',{name:'Side chat name',exact:true});
  await heading.focus();await heading.press('Enter');await input.fill('Discard this');await input.press('Escape');
  assert.equal(await heading.innerText(),'Older chat');assert.equal(await heading.evaluate(el=>document.activeElement===el),true);
  assert.equal(await page.locator('.side-chat-thread').isVisible(),true);
  await heading.click();await input.fill('   ');await input.press('Enter');
  assert.equal(await page.locator('.side-chat-title-error').innerText(),'Enter a side chat name.');
  assert.equal(await input.isVisible(),true);assert.equal(await input.getAttribute('maxlength'),'200');
  await input.press('Escape');assert.equal(await page.evaluate(()=>calls.filter(c=>c.url.endsWith('/side_chats_rename')).length),0);
  await heading.press('Escape');await page.waitForFunction(()=>document.querySelector('.agfocus-surface').hidden);
  assert.equal(await page.locator('.side-chat-title-button').count(),0);assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});

browserTest('a lost project rename response keeps the draft and reuses the receipt before leaving',async()=>{
 const {browser,page,errors}=await fixture();try{
  await page.evaluate(()=>{window.chatItem={id:'one',title:'Project chat',source:{type:'signoff',id:'one',business_id:'p'}};return sideChatOpen(chatItem);});
  const input=page.getByRole('textbox',{name:'Side chat name',exact:true});
  await page.locator('.side-chat-title-button').click();await input.fill('Project plans');
  await page.evaluate(()=>failRename=true);await input.press('Enter');await page.getByText('Lost rename response',{exact:true}).waitFor();
  assert.equal(await input.inputValue(),'Project plans');assert.equal(await input.evaluate(el=>el.readOnly),false);
  await page.evaluate(()=>holdRename=true);await page.locator('[data-surface-close]').click();
  await page.waitForFunction(()=>typeof finishRename==='function');assert.equal(await page.locator('.side-chat-thread').isVisible(),true);
  await page.evaluate(()=>finishRename());await page.waitForFunction(()=>document.querySelector('.agfocus-surface').hidden);
  const renames=await page.evaluate(()=>calls.filter(c=>c.url.endsWith('/side_chats_rename')));
  assert.equal(renames.length,2);assert.deepEqual(renames[0].body,renames[1].body);
  assert.equal(renames[0].url,'/biz/project/operations/side_chats_rename');assert.equal(renames[0].body.scope,'business');
  await page.evaluate(()=>sideChatOpen(chatItem));assert.equal(await page.locator('.side-chat-title-button').innerText(),'Project plans');
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});

browserTest('a detached rename cannot change the title of newer Focus content',async()=>{
 const {browser,page,errors}=await fixture();try{
  await page.evaluate(()=>{holdRename=true;return sideChatOpen({id:'one',title:'Old chat'});});
  await page.locator('.side-chat-title-button').click();const input=page.getByRole('textbox',{name:'Side chat name',exact:true});
  await input.fill('Renamed chat');await input.press('Enter');await page.waitForFunction(()=>typeof finishRename==='function');
  await page.evaluate(()=>{agpadSurfaceDispose(_agpad);return focusExpansion.open({title:'Newer view',mount:host=>{host.textContent='Newer content';}});});
  await page.evaluate(()=>finishRename());await page.waitForTimeout(20);
  assert.equal(await page.locator('.agfocus-surface-title').innerText(),'Newer view');
  assert.equal(await page.locator('.side-chat-title-button').count(),0);assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});
browserTest('references in a side chat open a referenced side chat as that side chat and a Task as a Task',async()=>{
 const {browser,page,errors}=await fixture();try{
  await page.evaluate(()=>{
   const original=post;
   window.post=async(url,body)=>url.endsWith('/owner_conversation_get') && body.source?.id==='one'
    ?{messages:[{id:'m1',body:'See these',author_kind:'agent',created_at:'2026-11-13T10:00:00Z',refs:[{type:'signoff',id:'two'},{type:'task',id:'t1'}]}],conversation:{latest_sequence:1,message_count:1}}
    :original(url,body);
   window.attentionQueue={refresh:async()=>{},sideChat:id=>id==='two'?{id:'two',title:'Second chat',source:{type:'signoff',id:'two',business_id:null}}:null};
   window.opened=[];window.taskProgressOpenReference=async(biz,ref)=>{opened.push(ref.type+':'+ref.id);};
  });
  await page.evaluate(()=>sideChatOpen({id:'one',title:'First chat',source:{type:'signoff',id:'one',business_id:null}}));
  await page.locator('.side-chat-thread .gchat-ref-row-task').click();
  assert.deepEqual(await page.evaluate(()=>opened),['task:t1']);
  await page.locator('.side-chat-thread button.gchat-reference',{hasText:'signoff'}).click();
  await page.waitForFunction(()=>document.querySelector('.side-chat-title-button')?.textContent==='Second chat');
  assert.deepEqual(await page.evaluate(()=>opened),['task:t1']);assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});
