// Every object entry point opens the same single channel: the owner's private thread with Ordubashy. There is no
// worker, peer or agent-discussion channel; no request names an agent. Task detail is driven
// through their real mount helpers here; the Signoff detail and agent-inbox entry points are driven in
// tests/test_signoffs_browser.js and tests/test_agent_focus_browser.js.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo=path.resolve(__dirname,'..');
const shell=[
  '<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/chat-view.css"><link rel="stylesheet" href="/css/conversation.css">',
  '<div id="host"></div>',
  '<script>function esc(s){return String(s??"").replace(/[&<>"\x27]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\x27":"&#39;"}[c]||c))}',
  'function newRequestId(){return crypto.randomUUID()}',
  'function coreData(x){if(x.status==="error"){const e=new Error(x.error.message);e.code=x.error.code;throw e}return x.data}',
  'window.state={biz:"alpha",bizs:[],agents:[],session:{is_owner:true}};window.aicon=()=>"";window.agentGlyph=()=>"";window.globalAgentProfile=()=>null;',
  'window.calls=[];window.readCursor=0;window.legacyPayload=false;window.failSend=0;window.delayGet=0;',
  'window.post=async(path,body)=>{const name=path.split("/").pop();calls.push({path,name,body});',
  'if(name==="owner_conversation_mark_read"){window.readCursor=body.sequence;return {status:"ok",data:{conversation:{id:"c"},read_state:{last_read_sequence:body.sequence,unread:false}}};}',
  'if(name==="owner_conversation_send"&&window.failSend>0){window.failSend--;return {status:"error",error:{code:"network",message:"Connection lost"}};}',
  'if(name==="owner_conversation_send")return {status:"ok",data:{message:{id:"sent-"+calls.length,body:body.text},conversation:{id:"c"}}};',
  'if(name==="owner_conversation_get"){if(window.delayGet)await new Promise(resolve=>setTimeout(resolve,window.delayGet));const data={conversation:{id:"c",latest_sequence:1,revision:1,message_count:1},messages:[{id:"m1-"+body.source.id,sequence:1,body:"Private line for "+body.source.type,created_by:"chief",created_by_name:"Ordubashy",author_kind:"agent",author_role:"coordinator",is_owner:false,created_at:"2026-09-15T09:00:00Z"}],next_before:null,participants:[],read_state:{last_read_sequence:window.readCursor,unread:window.readCursor<1}};',
  'if(window.legacyPayload){delete data.read_state;delete data.participants;}return {status:"ok",data};}',
  'return {status:"ok",data:{notes:[],next_before:null,items:[],comments:[]}};};',
  'window.api=async path=>(await fetch("/api"+path)).json();',
  '</script><script src="/js/safe-links.js"></script><script src="/js/chat-view.js"></script><script src="/js/conversation.js"></script><script src="/js/task-progress.js"></script>'
].join('');
const server=http.createServer((req,res)=>{
  if(req.url==='/')return res.end(shell);
  const target=path.join(repo,req.url);
  if(fs.existsSync(target)){res.setHeader('Content-Type',req.url.endsWith('.js')?'text/javascript':'text/css');return res.end(fs.readFileSync(target));}
  res.statusCode=404;res.end();
});
const cases=[
  {name:'task detail',source:{type:'task',id:'task-1',biz:'alpha',revision:3},entry:'taskProgress',privatePath:'/biz/alpha/operations/owner_conversation_get',scope:'business',expected:{type:'task',id:'task-1'}},
  {name:'Global task',source:{type:'task',id:'task-g',biz:null},privatePath:'/root/operations/owner_conversation_get',scope:'global',expected:{type:'task',id:'task-g'}},
  {name:'Signoff',source:{type:'signoff',id:'sg-1',biz:'alpha'},privatePath:'/biz/alpha/operations/owner_conversation_get',scope:'business',expected:{type:'signoff',id:'sg-1'}},
  {name:'Focus Reference',source:{type:'reference',id:'card-1',biz:null},privatePath:'/root/operations/owner_conversation_get',scope:'global',expected:{type:'reference',id:'card-1'}},
  {name:'Focus card for a business object',source:{type:'signoff',id:'sg-2',biz:null},adapter:{privateGlobal:true,privateSource:{type:'signoff',id:'sg-2',business_id:'biz-1'}},
   privatePath:'/root/operations/owner_conversation_get',scope:'global',expected:{type:'signoff',id:'sg-2',business_id:'biz-1'}},
];
const noAgentSelector=calls=>calls.filter(call=>call.name.startsWith('owner_conversation_')).every(call=>!('agent_id' in call.body)&&!('peer' in call.body)&&!('role' in call.body));
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  try {
    const page=await browser.newPage({viewport:{width:900,height:900}}),errors=[];
    page.setDefaultTimeout(10000);
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:'+server.address().port);
    for(const item of cases) {
      await page.evaluate(({source,entry,adapter})=>{
        window.calls=[];window.readCursor=0;
        const host=document.querySelector('#host');host.replaceChildren();
        const mount=document.createElement('div');host.append(mount);
        if(entry==='taskProgress')taskProgressMount(mount,{id:source.id,revision:source.revision},source.biz);
        else conversationMount(mount,source,{...(adapter || {})});
      },item);
      // taskProgressMount expands on mount; a bare conversationMount defaults to collapsed.
      if(!item.entry)await page.getByRole('button',{name:'Comment',exact:true}).click();
      await page.waitForFunction(()=>calls.some(call=>call.name==='owner_conversation_get'));
      const first=await page.evaluate(()=>calls.find(call=>call.name==='owner_conversation_get'));
      assert.equal(first.path,item.privatePath,item.name+' private route');
      assert.equal(first.body.scope,item.scope,item.name+' private scope');
      assert.deepEqual(first.body.source,item.expected,item.name+' canonical source');
      await page.getByText('Private line for '+item.expected.type,{exact:true}).waitFor();
      assert.equal(await page.locator('.gchat-composer').isVisible(),true,item.name+' can reply to Ordubashy');
      assert.equal(await page.locator('[role="tablist"],[role="tab"],.conversation-channel').count(),0,item.name+' has no channel selector');
      assert.equal(await page.getByRole('region',{name:'Conversation with Ordubashy'}).count(),1,item.name+' is named for Ordubashy');
      assert.equal(await page.evaluate(()=>calls.some(call=>['owner_conversation_peers','tasks_thread_get','signoffs_notes_list','email_comments_list','focus_get'].includes(call.name))),false,item.name+' reads no peer or team thread');
      assert.equal(await page.evaluate(noAgentSelector,await page.evaluate(()=>calls)),true,item.name+' names no agent');
      await page.waitForFunction(()=>calls.filter(call=>call.name==='owner_conversation_mark_read').length>=1);
      assert.equal(await page.evaluate(()=>calls.filter(call=>call.name==='owner_conversation_mark_read').length),1,item.name+' acknowledges once');
    }
    // An older core that returns neither read_state nor participants still reads and acknowledges once.
    await page.evaluate(()=>{
      window.calls=[];window.readCursor=0;window.legacyPayload=true;
      const host=document.querySelector('#host');host.replaceChildren();
      const mount=document.createElement('div');mount.dataset.probe='legacy';host.append(mount);
      conversationMount(mount,{type:'task',id:'legacy-task',biz:'alpha'},{}).setUnread(false);
    });
    await page.locator('[data-probe="legacy"] .conversation-toggle').click();
    await page.getByText('Private line for task',{exact:true}).waitFor();
    await page.waitForFunction(()=>calls.filter(call=>call.name==='owner_conversation_mark_read').length===1);
    assert.equal(await page.evaluate(()=>readCursor),1);
    assert.equal(await page.locator('[data-probe="legacy"] [data-conversation-unread]').isVisible(),false,'the acknowledged channel leaves no dot');
    await page.evaluate(()=>{window.legacyPayload=false;});

    // Drafts belong to one source, never to another object.
    await page.evaluate(()=>{
      const host=document.querySelector('#host');host.replaceChildren();
      for(const id of ['task-1','task-2']) {
        const mount=document.createElement('div');mount.dataset.probe=id;host.append(mount);
        conversationMount(mount,{type:'task',id,biz:'alpha'},{});
      }
    });
    await page.locator('[data-probe="task-1"] .conversation-toggle').click();
    await page.locator('[data-probe="task-1"] textarea').fill('Only for task one');
    assert.equal(await page.locator('[data-probe="task-2"] textarea').inputValue(),'');
    assert.deepEqual(await page.evaluate(()=>[
      conversationState({type:'task',id:'task-1',biz:'alpha'}).text,
      conversationState({type:'task',id:'task-2',biz:'alpha'}).text]),['Only for task one','']);

    // A failed send keeps its frozen retry: the retry reuses the same request key, and only then is the draft cleared.
    await page.evaluate(()=>{window.calls=[];window.failSend=1;});
    await page.locator('[data-probe="task-1"] .gchat-send').click();
    await page.locator('[data-probe="task-1"] [role="alert"]').waitFor();
    assert.equal(await page.locator('[data-probe="task-1"] textarea').inputValue(),'Only for task one','a failed send keeps the draft');
    await page.locator('[data-probe="task-1"] .gchat-send').click();
    await page.waitForFunction(()=>calls.filter(call=>call.name==='owner_conversation_send').length===2);
    const sends=await page.evaluate(()=>calls.filter(call=>call.name==='owner_conversation_send'));
    assert.deepEqual(sends[0].body,sends[1].body,'the retry reuses the exact request and key');
    assert.deepEqual(Object.keys(sends[0].body).sort(),['idempotency_key','scope','source','text']);
    await page.waitForFunction(()=>document.querySelector('[data-probe="task-1"] textarea').value==='');
    assert.equal(await page.locator('[data-probe="task-2"] textarea').inputValue(),'','another source never receives the draft');

    // A slow response that lands after the owner moved to another record never paints into it.
    await page.evaluate(()=>{
      window.calls=[];window.delayGet=400;
      const host=document.querySelector('#host');host.replaceChildren();
      const mount=document.createElement('div');mount.dataset.probe='slow';host.append(mount);
      window.slow=conversationMount(mount,{type:'task',id:'slow-task',biz:'alpha'},{});slow.expand();
    });
    await page.evaluate(()=>{slow.dispose();document.querySelector('#host').replaceChildren();window.delayGet=0;const mount=document.createElement('div');mount.dataset.probe='next';document.querySelector('#host').append(mount);conversationMount(mount,{type:'email',id:'next-mail',biz:'alpha'},{}).expand();});
    await page.getByText('Private line for email',{exact:true}).waitFor();
    await page.waitForTimeout(500);
    assert.equal(await page.locator('#host').innerText().then(text=>text.includes('Private line for task')),false,'a late response is discarded');
    assert.equal(await page.evaluate(()=>calls.filter(call=>call.name==='owner_conversation_mark_read').every(call=>call.body.source.id!=='slow-task')),true,'a discarded load acknowledges nothing');

    assert.deepEqual(errors,[]);
    console.log('Record conversations: one Ordubashy channel on every entry point, no agent selector, per-source drafts, exact-key retry and stale responses passed.');
  } finally { await browser.close();server.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
