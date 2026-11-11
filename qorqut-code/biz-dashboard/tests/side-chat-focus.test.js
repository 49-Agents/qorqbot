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
  window.post=async(url,body)=>{
   calls.push({url,body});
   if(url.endsWith('/side_chats_create')){if(hold)return new Promise(resolve=>window.finishCreate=resolve);if(failCreate)throw Error('Offline');return {signoff:{id:'created',title:body.title,review_business_id:null}};}
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
