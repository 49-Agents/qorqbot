// Real isolated Chromium: the single Ordubashy channel per object, canonical drafts,
// read acknowledgment and retry safety.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo=path.resolve(__dirname,'..');
const server=http.createServer((req,res)=>{
  if(req.url==='/')return res.end(`<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/chat-view.css"><link rel="stylesheet" href="/css/conversation.css"><div id="one"></div><div id="two"></div><div id="three"></div><div id="four"></div><div id="five"></div><script>function esc(s){return String(s??"").replace(/[&<>"\x27]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","\x27":"&#39;"}[c]||c))}function newRequestId(){return crypto.randomUUID()}function coreData(x){if(x.status==="error"){const e=new Error(x.error.message);e.code=x.error.code;throw e}return x.data}</script><script src="/js/safe-links.js"></script><script src="/js/chat-view.js"></script><script src="/js/conversation.js"></script>`);
  if(req.url==='/api/session'){res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({status:'ok',data:{principal_id:'teammate',principal_kind:'human',is_owner:false}}));}
  const target=path.join(repo,req.url);if(fs.existsSync(target)){res.setHeader('Content-Type',req.url.endsWith('.js')?'text/javascript':'text/css');return res.end(fs.readFileSync(target));}res.statusCode=404;res.end();
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
 try{
 const page=await browser.newPage({viewport:{width:1000,height:800}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 page.setDefaultTimeout(10000);
 await page.goto('http://127.0.0.1:'+server.address().port);
 await page.evaluate(()=>{
  window.reads=0;window.writes=[];window.lose=true;window.calls=[];
  window.privateRead=0;window.privateFail=null;window.holdSend=null;window.holdPrivateRead=false;window.channelRequired=false;
  window.privateMessages=[{id:'p1',sequence:1,body:'Private note from Ordubashy',created_by:'agent-a',created_by_name:'Ordubashy',author_kind:'agent',author_role:'coordinator',is_owner:false,created_at:'2026-09-15T09:30:00Z'}];
  const op=path=>path.split('/').pop();
  window.post=async(path,body)=>{
    const name=op(path);calls.push({path,name,body});
    if(name==='owner_conversation_get') {
      if(window.privateFail){const code=window.privateFail;window.privateFail=null;return {status:'error',error:{code,message:'Private read failed.'}};}
      const latest=privateMessages.length?privateMessages.at(-1).sequence:0;
      const response={status:'ok',data:{conversation:{id:latest?'pc':null,source:body.source,audience:'owner_ordubashy',revision:latest?1:0,latest_sequence:latest,message_count:privateMessages.length},
        messages:[...privateMessages],next_before:null,participants:[{created_by:'owner',created_by_name:'Owner',author_kind:'human',is_owner:true},{created_by:'agent-a',created_by_name:'Ordubashy',author_kind:'agent'}],
        read_state:{last_read_sequence:privateRead,unread:privateMessages.some(m=>m.author_role==='coordinator' && m.sequence>privateRead)}}};
      if(window.holdPrivateRead)await new Promise(resolve=>{window.releasePrivateRead=resolve;});
      return response;
    }
    if(name==='owner_conversation_send') {
      writes.push({...body});
      if(window.holdSend)return new Promise(resolve=>{window.finishHeldSend=()=>resolve({status:'ok',data:{}});});
      if(window.lose){window.lose=false;return {status:'error',error:{code:'unavailable',message:'Lost response'}};}
      const sequence=(privateMessages.at(-1)?.sequence || 0)+1;
      privateMessages.push({id:'p'+sequence,sequence,body:body.text,created_by:'owner',created_by_name:'Owner',author_kind:'human',author_role:'owner',is_owner:true,created_at:new Date().toISOString()});
      return {status:'ok',data:{message:privateMessages.at(-1),conversation:{id:'pc',revision:sequence,latest_sequence:sequence,message_count:privateMessages.length}}};
    }
    if(name==='owner_conversation_mark_read'){window.privateRead=body.sequence;return {status:'ok',data:{read_state:{last_read_sequence:body.sequence,unread:false}}};}
    return {status:'ok',data:{}};
  };
  window.notes=[{id:'old-state',body:'State changed from open to doing.',entry_kind:'system',created_at:'2026-09-15T08:00:00Z',sequence:0},{id:'b',summary:'Second update needs owner review',body:'Second complete message with all of its original details and additional information preserved after expansion.',created_by:'agent-a',created_by_name:'Coordinator',author_kind:'agent',created_at:'2026-09-15T10:00:00Z',sequence:2},
   {id:'c',body:'Third <img src=x>',created_by:'agent-a',created_by_name:'Coordinator',author_kind:'agent',created_at:'2026-09-15T10:01:00Z',sequence:3},
   {id:'d',body:'Task details changed.',entry_kind:'system',created_at:'2026-09-15T10:02:00Z',sequence:4},
   {id:'e',body:'Owner reply',created_by:'owner',created_by_name:'Owner',author_kind:'human',created_at:'2026-09-15T10:03:00Z',sequence:5}];
  window.adapter={read:async args=>{reads++;return args.before?{notes:[{id:'a',body:'First',created_by:'agent-a',created_by_name:'Coordinator',author_kind:'agent',created_at:'2026-09-15T09:00:00Z',sequence:1}],next_before:null}:{notes:[...notes],next_before:2};},
    send:async r=>{if(window.channelRequired){const e=new Error('Owner comments belong to the private conversation with Ordubashy.');e.code='channel_required';throw e;}writes.push({...r});}};
  conversationMount(document.querySelector('#one'),{type:'reference',id:'stable',biz:null},adapter);
 });
 const calls=name=>page.evaluate(name=>calls.filter(call=>call.name===name).length,name);
 // The owner sees only the Ordubashy conversation; nothing is read before the panel opens.
 assert.equal(await page.locator('[role="tablist"],[role="tab"],.conversation-channel').count(),0);
 assert.equal(await page.evaluate(()=>reads),0);
 assert.equal(await calls('owner_conversation_get'),0);
 assert.equal(await page.locator('.conversation-panel').isVisible(),false);
 await page.getByRole('button',{name:'Comment',exact:true}).click();
 await page.waitForFunction(()=>calls.some(call=>call.name==='owner_conversation_get'));
 assert.equal(await page.evaluate(()=>reads),0,'no team thread is ever read');
 assert.equal(await page.evaluate(()=>calls.find(call=>call.name==='owner_conversation_get').path),'/root/operations/owner_conversation_get');
 assert.deepEqual(await page.evaluate(()=>calls.find(call=>call.name==='owner_conversation_get').body.source),{type:'reference',id:'stable'});
 assert.equal(await page.evaluate(()=>calls.find(call=>call.name==='owner_conversation_get').body.scope),'global');
 await page.getByText('Private note from Ordubashy',{exact:true}).waitFor();
 // Loading through the latest sequence acknowledges exactly that sequence.
 await page.waitForFunction(()=>privateRead===1);
 assert.equal(await calls('owner_conversation_mark_read'),1);
 assert.equal(await page.evaluate(()=>calls.find(call=>call.name==='owner_conversation_mark_read').body.sequence),1);
 assert.equal(await page.locator('.gchat-composer').isVisible(),true);

  // Drafts follow the source across mounts.
 assert.equal(await page.locator('.gchat-scroll').innerText().then(text=>text.includes('Private note from Ordubashy')),true);
 await page.locator('textarea').fill('Keep my draft');
 assert.equal(await page.evaluate(()=>conversationState({type:'reference',id:'stable',biz:null}).text),'Keep my draft');
 await page.evaluate(()=>{document.querySelector('#one').remove();conversationMount(document.querySelector('#two'),{type:'reference',id:'stable',biz:null},adapter);});
 assert.equal(await page.locator('#two textarea').inputValue(),'Keep my draft');
 assert.equal(await page.locator('.conversation-panel').isVisible(),false);
 await page.getByRole('button',{name:'Comment',exact:true}).click();
 await page.locator('textarea').press('Control+Enter');
 await page.getByRole('button',{name:'Retry Send',exact:true}).waitFor();
 assert.equal(await page.locator('textarea').inputValue(),'Keep my draft');
 await page.getByRole('button',{name:'Retry Send',exact:true}).click();
 await page.waitForFunction(()=>writes.length===2 && document.querySelector('textarea').value==='');
 assert.deepEqual(...await page.evaluate(()=>[writes[0].idempotency_key,writes[1].idempotency_key].map(key=>({key}))));
 assert.equal(await page.evaluate(()=>writes[0].text),'Keep my draft');

 // Polling reads without acknowledging; only an explicit open does.
 const marksBefore=await calls('owner_conversation_mark_read');
 await page.evaluate(()=>{privateMessages.push({id:'pr',sequence:99,body:'Later private reply',created_by:'agent-a',created_by_name:'Ordubashy',author_kind:'agent',author_role:'coordinator',is_owner:false,created_at:'2026-09-15T12:00:00Z'});});
 await page.evaluate(()=>document.querySelector('#two')._conversation.refresh());
 await page.getByText('Later private reply',{exact:true}).waitFor();
 assert.equal(await calls('owner_conversation_mark_read'),marksBefore,'polling never acknowledges');
 // A failed private load leaves the indicator exactly as it was.
 await page.evaluate(()=>{document.querySelector('#two')._conversation.setUnread(true);privateFail='unavailable';});
 await page.evaluate(()=>{const c=document.querySelector('#two')._conversation;c.collapse();c.expand();});
 await page.locator('#two [role="alert"]:visible').waitFor();
 assert.equal(await page.locator('.conversation-unread').isVisible(),true);
 assert.equal(await calls('owner_conversation_mark_read'),marksBefore);
 await page.evaluate(()=>{const c=document.querySelector('#two')._conversation;c.collapse();c.expand();});
 await page.waitForFunction(()=>privateRead===99);
 assert.equal(await page.locator('.conversation-unread').isVisible(),false,'a successful acknowledgment clears the dot');
 const marksAfterPrivateSuccess=await calls('owner_conversation_mark_read');

 // A private load that is collapsed before the response arrives must not mark it read.
 await page.evaluate(()=>{state={session:{is_owner:true}};privateRead=0;holdPrivateRead=true;});
 await page.evaluate(()=>conversationMount(document.querySelector('#four'),{type:'reference',id:'collapsed',biz:null},{}));
 await page.locator('#four .conversation-toggle').click();
 await page.waitForFunction(()=>calls.some(call=>call.name==='owner_conversation_get' && call.body.source.id==='collapsed'));
 await page.locator('#four .conversation-toggle').click();
 await page.evaluate(()=>{holdPrivateRead=false;releasePrivateRead?.();});
 await new Promise(resolve=>setTimeout(resolve,100));
 assert.equal(await calls('owner_conversation_mark_read'),marksAfterPrivateSuccess,'collapsed private load is not acknowledged');

 // Disposing the mount before that same load completes is also not a read.
 await page.evaluate(()=>{privateRead=0;holdPrivateRead=true;});
 await page.evaluate(()=>{window.five=conversationMount(document.querySelector('#five'),{type:'reference',id:'switched',biz:null},{});});
 await page.locator('#five .conversation-toggle').click();
 await page.waitForFunction(()=>calls.some(call=>call.name==='owner_conversation_get' && call.body.source.id==='switched'));
 await page.evaluate(()=>five.dispose());
 await page.evaluate(()=>{holdPrivateRead=false;releasePrivateRead?.();});
 await new Promise(resolve=>setTimeout(resolve,100));
 assert.equal(await calls('owner_conversation_mark_read'),marksAfterPrivateSuccess,'a disposed private load is not acknowledged');

 // Timeout preserves the uncertain private write's key; retry does not resend.
 await page.evaluate(()=>{
   window.realSetTimeout=window.setTimeout;
   window.setTimeout=(fn,ms,...args)=>realSetTimeout(fn,ms===15000?40:ms,...args);
   window.holdSend=true;
 });
 await page.locator('#two textarea').fill('Keep this on timeout');
 await page.getByRole('button',{name:'Send',exact:true}).click();
 await page.waitForFunction(()=>!document.querySelector('.gchat-send').disabled && document.querySelector('[role="alert"]').textContent.includes("Couldn't confirm delivery. Retry Send"));
 assert.equal(await page.locator('#two textarea').inputValue(),'Keep this on timeout');
 await page.evaluate(()=>{window.holdSend=false;window.setTimeout=window.realSetTimeout;});
 await page.getByRole('button',{name:'Retry Send',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('textarea').value==='');
 assert.deepEqual(...await page.evaluate(()=>writes.slice(-2).map(write=>({key:write.idempotency_key,text:write.text}))));
 await page.locator('#two textarea').fill('Keep after late response');
 await page.evaluate(()=>window.finishHeldSend?.());
 assert.equal(await page.locator('#two textarea').inputValue(),'Keep after late response');

 // A teammate who is not the installation owner also has only the Ordubashy conversation.
 await page.evaluate(async()=>{
   window.state={session:coreData(await (await fetch('/api/session')).json())};
   conversationMount(document.querySelector('#three'),{type:'reference',id:'guarded',biz:null},adapter);
 });
 assert.equal(await page.evaluate(()=>state.session.is_owner),false);
 const third=page.locator('#three');
 await third.getByRole('button',{name:'Comment',exact:true}).click();
 assert.equal(await third.locator('.gchat-composer').isVisible(),true);
 assert.equal(await third.locator('[role="tab"],.conversation-channel').count(),0);
 assert.equal(await page.evaluate(()=>calls.every(call=>!call.name.startsWith('owner_conversation_')||!('agent_id' in call.body))),true,'no request names an agent');
 assert.deepEqual(errors,[]);
 await page.screenshot({path:'/tmp/owner-attention-conversation.png',fullPage:true});
 console.log('Owner attention browser checks passed');
 }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1});
