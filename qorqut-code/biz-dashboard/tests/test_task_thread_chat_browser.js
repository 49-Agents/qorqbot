const assert=require('node:assert/strict'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async()=>{const browser=await chromium.launch({headless:true,args:['--no-sandbox']});try{
 const page=await browser.newPage({viewport:{width:1200,height:800}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.setContent('<div class="agsplit"><section class="global-agent-session" style="position:absolute;left:30px;top:20px;width:650px;height:650px"></section><aside id="agpad"><div class="agfocus"><div class="agfocus-layout"><aside class="agfocus-dock"><div id="chat-test"></div></aside></div></div></aside></div>');
 const root=path.resolve(__dirname,'..');
 for(const file of ['app.css','css/chat-view.css','css/conversation.css','css/agent-focus.css'])await page.addStyleTag({path:path.join(root,file)});
 await page.addScriptTag({path:path.join(root,'js/safe-links.js')});
 await page.addScriptTag({content:`const esc=safeLinkEsc, state={session:{is_owner:true},bizs:[{id:'biz-a',slug:'widget'}]},coreData=r=>r.data,newRequestId=()=>String(Math.random());let opened=[];window.taskProgressOpenReference=async(biz,ref)=>{opened.push([biz,ref]);};let sends=[],marks=[],uploads=[],unread=true,failSend=false;
 const api=async(path,options)=>{if(path!=='/root/thread-attachments')throw Error('Unexpected upload route');uploads.push({path,key:options.headers['Idempotency-Key'],name:decodeURIComponent(options.headers['X-File-Name']),size:options.body.size});return {data:{reference:'[image.png](</absolute/path/to/qorqut/operations/agents/ordubashy/downloads/paste-1/image.png>)'}};};
 const post=async(path,args)=>{
  if(path.endsWith('owner_conversation_peers')||path.endsWith('tasks_comment')||path.endsWith('tasks_thread_get'))throw Error('Retired record-thread route '+path);
  if('agent_id' in (args||{})||'peer' in (args||{}))throw Error('Record threads never name an agent');
  if(path.endsWith('owner_conversation_mark_read')){marks.push(args);unread=false;return {data:{conversation:{},read_state:{last_read_sequence:2,unread:false}}};}
  if(path.endsWith('owner_conversation_get')&&args.source.id==='empty')return {data:{conversation:{latest_sequence:0,message_count:0},messages:[],read_state:{unread:false}}};
  if(path.endsWith('owner_conversation_get'))return {data:{conversation:{latest_sequence:2,message_count:1},messages:[{id:'m1',sequence:2,body:'See [bad](javascript:alert(1)) now',created_by:'chief',created_by_name:'Ordubashy',author_kind:'agent',created_at:'2026-09-15T10:00:00Z',refs:[{type:'task',id:'t9',business_id:'biz-a',business_slug:'widget',label:'<b>Bad</b>',unavailable:true},{type:'dev_ticket',id:'d1',label:'Retired ticket'},{type:'task',id:'t2',business_id:'biz-a',business_slug:'widget',label:'Open task',extra:{k:1}}]}],read_state:{unread}}};
  if(path.endsWith('owner_conversation_send')){sends.push({path,args});if(failSend){failSend=false;throw Error('Network interrupted');}return {data:{}};}
  if(path.endsWith('tasks_get'))return {data:{task:{id:args.task_id,title:'Open task',task_type:'task',revision:3}}};
  throw Error('Unexpected '+path);
 };
 `});
 await page.addScriptTag({path:path.join(root,'js/chat-view.js')});
 await page.addScriptTag({path:path.join(root,'js/conversation.js')});
 await page.evaluate(()=>{window.chat=conversationMount(document.querySelector('#chat-test'),{type:'task',id:'task1',biz:'widget'},{inlineComposer:true,dismissOnOutside:false});chat.expand();});
 await page.locator('.gchat-scroll').getByText('Ordubashy',{exact:true}).first().waitFor();
 assert.equal(await page.locator('[role="tablist"],[data-channel],[data-peer-unread]').count(),0,'a record thread has no agent or team selector');
 await page.waitForFunction(()=>marks.length>=1);assert.deepEqual(await page.evaluate(()=>marks[0].source),{type:'task',id:'task1'});
 await page.locator('textarea').fill('Check the button');await page.locator('.gchat-send').click();
 await page.waitForFunction(()=>sends.length===1);
 assert.ok((await page.evaluate(()=>sends[0].path)).endsWith('owner_conversation_send'));
 assert.equal(await page.evaluate(()=>'agent_id' in sends[0].args),false,'the send names no agent');
 // An uncertain private send retains the exact request on retry.
 await page.evaluate(()=>failSend=true);await page.locator('textarea').fill('Retry me');await page.locator('.gchat-send').click();await page.locator('[role="alert"]').waitFor();await page.locator('.gchat-send').click();
 await page.waitForFunction(()=>sends.length===3);assert.deepEqual(await page.evaluate(()=>sends[1]),await page.evaluate(()=>sends[2]));
 // Messages carry the structured references.
 // The opaque reference value reaches the opener untouched; an unavailable one does nothing.
 await page.locator('#chat-test').getByText('Unavailable reference',{exact:true}).first().click({force:true});
 assert.equal(await page.evaluate(()=>opened.length),0,'a disabled reference opens nothing');
 assert.equal(await page.locator('#chat-test').getByText('Retired ticket').count(),0,'a System Dev ticket reference shows no chip');
 await page.locator('#chat-test').getByText('Open task',{exact:true}).click();
 assert.deepEqual(await page.evaluate(()=>opened),[['widget',{type:'task',id:'t2',business_id:'biz-a',business_slug:'widget',label:'Open task',extra:{k:1}}]],'the exact structured reference is opened');
 // A populated thread shows no empty text.
 assert.equal(await page.locator('#chat-test .gchat-empty:visible').count(),0,'a populated thread shows no empty text');
 // Main Chat keyboard semantics: Enter sends, Shift+Enter is a newline, composing never sends.
// Thread text uses the shared safe-link renderer; an unavailable reference stays inert and escaped.
 assert.equal(await page.locator('.gchat-scroll a[href^="javascript"]').count(),0);
 assert.equal(await page.locator('.gchat-scroll b').count(),0);
 assert.equal(await page.locator('.gchat-reference-unavailable').evaluate(el=>el.closest('button')?.disabled ?? true),true);
 assert.equal(await page.locator('.gchat-reference-unavailable').textContent(),'Unavailable reference','revoked reference labels do not expose cached metadata');

 const box=page.locator('#chat-test textarea'),beforeKeys=await page.evaluate(()=>sends.length);
 await box.fill('Line one');await box.press('Shift+Enter');await box.pressSequentially('line two');
 assert.equal(await box.inputValue(),'Line one\nline two');assert.equal(await page.evaluate(()=>sends.length),beforeKeys);
 await box.evaluate(el=>el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true,cancelable:true})));
 await page.waitForTimeout(100);assert.equal(await page.evaluate(()=>sends.length),beforeKeys,'IME composition never submits');
 // A second view of the same channel follows the draft silently.
 await page.evaluate(()=>{const host=document.createElement('div');host.id='chat-sibling';document.body.append(host);window.sibling=conversationMount(host,{type:'task',id:'task1',biz:'widget'},{dismissOnOutside:false});sibling.expand();});
 await page.waitForFunction(()=>document.querySelector('#chat-sibling textarea').value==='Line one\nline two');
 await page.evaluate(()=>{sibling.dispose();document.querySelector('#chat-sibling').remove();});
 await box.press('Enter');await page.waitForFunction(n=>sends.length===n+1,beforeKeys);
 assert.equal(await box.inputValue(),'','a confirmed send clears the shared draft');
 // Empty text appears only for a loaded empty list.
 await page.evaluate(()=>{const host=document.createElement('div');host.id='chat-empty';document.body.append(host);window.emptyChat=conversationMount(host,{type:'task',id:'empty',biz:'widget'},{dismissOnOutside:false});emptyChat.expand();});
 await page.locator('#chat-empty .gchat-empty:visible').waitFor();
 assert.equal(await page.locator('#chat-empty .gchat-empty').innerText(),'No messages yet');
 await page.evaluate(()=>{emptyChat.dispose();document.querySelector('#chat-empty').remove();});
 // A collapsed inline composer keeps a concise failed-send error and a frozen retry; Retry Send then succeeds with the same key.
 await page.evaluate(()=>chat.collapse());
 assert.equal(await page.locator('#chat-test .gchat-scroll:visible').count(),0,'the feed is collapsed');
 const inline=page.locator('#chat-test textarea'),before=await page.evaluate(()=>sends.length);
 await page.evaluate(()=>failSend=true);await inline.fill('Collapsed retry');await page.locator('#chat-test .gchat-send').click();
 await page.locator('#chat-test [role="alert"]:visible').waitFor();
 assert.match(await page.locator('#chat-test [role="alert"]').innerText(),/Couldn't confirm delivery. Retry Send/);
 assert.equal(await inline.isDisabled(),true,'the retried draft is frozen');
 assert.equal(await page.locator('#chat-test .gchat-send').innerText(),'Retry Send');
 await page.locator('#chat-test .gchat-send').click();
 await page.waitForFunction(n=>sends.length===n+2,before);
 const retried=await page.evaluate(n=>sends.slice(n),before);
 assert.deepEqual(retried[0],retried[1],'the retry reuses the request and idempotency key');
 assert.equal(await page.evaluate(n=>new Set(sends.slice(n).map(s=>s.args.idempotency_key||s.args.request_id)).size,before),1);
 await page.waitForFunction(()=>!document.querySelector('#chat-test textarea').disabled);
 // Clipboard images remain with the exact Task. Sending saves once in
 // Ordubashy's downloads; a lost comment response reuses its file and send key.
 await page.evaluate(()=>chat.expand());
 await inline.fill('Look ');
 await inline.evaluate(el=>{const data=new DataTransfer();data.setData('text/plain','here');data.items.add(new File(['IMAGE BYTES'],'image.png',{type:'image/png'}));el.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:data}));});
 assert.equal(await inline.inputValue(),'Look here');
 assert.equal(await page.locator('#chat-test .gchat-file-choice').count(),1);
 assert.equal(await page.evaluate(()=>uploads.length),0,'paste queues locally');
 const pastedBefore=await page.evaluate(()=>sends.length);
 await page.evaluate(()=>failSend=true);await page.locator('#chat-test .gchat-send').click();
 await page.locator('#chat-test [role="alert"]:visible').waitFor();
 assert.equal(await page.evaluate(()=>uploads.length),1);
 await page.locator('#chat-test .gchat-send').click();
 await page.waitForFunction(n=>sends.length===n+2,pastedBefore);
 assert.equal(await page.evaluate(()=>uploads.length),1,'retry does not copy the image again');
 const pasted=await page.evaluate(n=>sends.slice(n),pastedBefore);
 assert.deepEqual(pasted[0],pasted[1]);
 assert.match(pasted[0].args.text,/Look here\n\n\[image.png\].*agents\/ordubashy\/downloads/);
 assert.ok(!pasted[0].args.text.includes('IMAGE BYTES'));
 await page.waitForFunction(()=>!document.querySelector('#chat-test .gchat-file-choice'));
 // Drop also works on the external, collapsed Focus composer, not just the feed.
 await page.evaluate(()=>chat.collapse());
 await inline.evaluate(el=>{const data=new DataTransfer();data.items.add(new File(['notes'],'notes.txt'));el.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:data}));});
 assert.equal(await page.locator('#chat-test .gchat-file-choice').count(),1);
 await page.getByRole('button',{name:'Remove notes.txt',exact:true}).click();
 assert.equal(await page.locator('#chat-test .gchat-file-choice').count(),0);
 await page.addScriptTag({path:path.join(root,'js/agent-focus.js')});
 await page.evaluate(()=>{window.p={el:document.querySelector('#agpad')};agfocusOverlayOpen(p,document.querySelector('.agfocus'),document.querySelector('.agfocus-dock'));});
 const bounds=await page.evaluate(()=>{const a=document.querySelector('.global-agent-session').getBoundingClientRect(),b=document.querySelector('.agfocus-dock').getBoundingClientRect();return [a.x,a.y,a.width,a.height,b.x,b.y,b.width,b.height];});assert.deepEqual(bounds.slice(0,4),bounds.slice(4));
 assert.equal(await page.locator('.global-agent-session').evaluate(e=>e.inert),true);
 await page.locator('.global-agent-session').evaluate(e=>e.style.width='500px');await page.waitForFunction(()=>document.querySelector('.agfocus-dock').getBoundingClientRect().width===500);
 await page.evaluate(()=>agfocusOverlayClose(p));assert.equal(await page.locator('.global-agent-session').evaluate(e=>e.inert),false);
 assert.deepEqual(errors,[]);console.log('Task chat: Ordubashy-only read/send/retry, business group composer, terminal overlay sizing and cleanup passed.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
