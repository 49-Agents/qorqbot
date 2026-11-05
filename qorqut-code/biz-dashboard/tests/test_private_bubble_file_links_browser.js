// The surviving private Ordubashy bubble: a typed file reference in a message is a real link into the existing file
// viewer, in a new tab, with no markup from the text, on a business and on a Global record. Only the HTTP boundary is
// synthetic (an isolated local port with a stand-in viewer page); no live file, API, provider or agent session.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo=path.resolve(__dirname,'..');
const shell=[
  '<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/chat-view.css"><link rel="stylesheet" href="/css/conversation.css">',
  '<div id="host"></div>','<script>window.proseBody="Read [the brief](/absolute/path/to/businesses/alpha/files/report.md) or [viewer](/files/view?path=widget%2Ffiles%2Freport.md), not [this](javascript:alert(1)) or <b>markup</b>.";</script>',
  '<script>function esc(s){return String(s??"").replace(/[&<>"\x27]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\x27":"&#39;"}[c]||c))}',
  'function newRequestId(){return crypto.randomUUID()}',
  'function coreData(x){if(x.status==="error"){const e=new Error(x.error.message);e.code=x.error.code;throw e}return x.data}',
  'window.state={biz:"alpha",bizs:[],agents:[],session:{is_owner:true}};window.aicon=()=>"";window.agentGlyph=()=>"";window.globalAgentProfile=()=>null;',
  'window.calls=[];window.readCursor=0;window.legacyPayload=false;window.failSend=0;window.delayGet=0;',
  'window.post=async(path,body)=>{const name=path.split("/").pop();calls.push({path,name,body});',
  'if(name==="owner_conversation_mark_read"){window.readCursor=body.sequence;return {status:"ok",data:{conversation:{id:"c"},read_state:{last_read_sequence:body.sequence,unread:false}}};}',
  'if(name==="owner_conversation_send"&&window.failSend>0){window.failSend--;return {status:"error",error:{code:"network",message:"Connection lost"}};}',
  'if(name==="owner_conversation_send")return {status:"ok",data:{message:{id:"sent-"+calls.length,body:body.text},conversation:{id:"c"}}};',
  'if(name==="owner_conversation_get"){if(window.delayGet)await new Promise(resolve=>setTimeout(resolve,window.delayGet));const data={conversation:{id:"c",latest_sequence:1,revision:1,message_count:1},messages:[{id:"m1-"+body.source.id,sequence:1,body:window.proseBody,created_by:"chief",created_by_name:"Ordubashy",author_kind:"agent",author_role:"coordinator",is_owner:false,created_at:"2026-09-15T09:00:00Z"}],next_before:null,participants:[],read_state:{last_read_sequence:window.readCursor,unread:window.readCursor<1}};',
  'if(window.legacyPayload){delete data.read_state;delete data.participants;}return {status:"ok",data};}',
  'return {status:"ok",data:{notes:[],next_before:null,items:[],comments:[]}};};',
  'window.api=async path=>(await fetch("/api"+path)).json();',
  '</script><script src="/js/safe-links.js"></script><script src="/js/chat-view.js"></script><script src="/js/conversation.js"></script><script src="/js/task-progress.js"></script>'
].join('');
const viewerRequests=[];


const server=http.createServer((req,res)=>{
  if(req.url==='/')return res.end(shell);
  if(req.url.startsWith('/files/view')){viewerRequests.push(req.url);res.setHeader('Content-Type','text/html');return res.end('<!doctype html><title>Viewer</title><main class="fv-md">Stand-in file viewer</main>');}
  const target=path.join(repo,req.url);
  if(fs.existsSync(target)){res.setHeader('Content-Type',req.url.endsWith('.js')?'text/javascript':'text/css');return res.end(fs.readFileSync(target));}
  res.statusCode=404;res.end();
});
const cases=[
  {name:'business task',source:{type:'task',id:'task-1',biz:'alpha',revision:3},entry:'taskProgress',privatePath:'/biz/alpha/operations/owner_conversation_get',scope:'business'},
  {name:'Global task',source:{type:'task',id:'task-g',biz:null},privatePath:'/root/operations/owner_conversation_get',scope:'global'},
];
const typed='/files/view?path='+encodeURIComponent('/absolute/path/to/businesses/alpha/files/report.md');
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  try {
    const context=await browser.newContext({viewport:{width:900,height:900}}),page=await context.newPage(),errors=[];
    page.setDefaultTimeout(10000);
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:'+server.address().port);
    for(const item of cases) {
      await page.evaluate(({source,entry})=>{
        window.calls=[];window.readCursor=0;
        const host=document.querySelector('#host');host.replaceChildren();
        const mount=document.createElement('div');host.append(mount);
        if(entry==='taskProgress')taskProgressMount(mount,{id:source.id,revision:source.revision},source.biz);
        else conversationMount(mount,source,{});
      },item);
      if(!item.entry)await page.getByRole('button',{name:'Comment',exact:true}).click();
      await page.waitForFunction(()=>calls.some(call=>call.name==='owner_conversation_get'));
      const first=await page.evaluate(()=>calls.find(call=>call.name==='owner_conversation_get'));
      assert.equal(first.path,item.privatePath,item.name+' reads its own private route');
      assert.equal(first.body.scope,item.scope,item.name+' keeps its scope');
      const body=page.locator('.gchat-body').first();await body.waitFor();
      const brief=body.locator('a.safe-link',{hasText:'the brief'});
      assert.equal(await brief.getAttribute('href'),typed,item.name+' typed reference chooses the file viewer URL');
      assert.equal(await brief.getAttribute('target'),'_blank');assert.equal(await brief.getAttribute('rel'),'noopener noreferrer');
      assert.equal(await body.locator('a.safe-link').count(),2,item.name+' only the two safe links');
      assert.equal(await body.locator('b,img,a[href^="javascript"]').count(),0,item.name+' no markup or script links from text');
      assert.match(await body.textContent(),/<b>markup<\/b>/);
      viewerRequests.length=0;
      const [tab]=await Promise.all([context.waitForEvent('page'),brief.click()]);
      await tab.waitForLoadState();
      assert.equal(new URL(tab.url()).pathname+new URL(tab.url()).search,typed,item.name+' opens the viewer for that exact file');
      assert.deepEqual(viewerRequests,[typed]);
      await tab.close();
      const viewer=body.locator('a.safe-link',{hasText:'viewer'});
      assert.equal(await viewer.getAttribute('href'),'/files/view?path=widget%2Ffiles%2Freport.md');
      assert.equal(await page.locator('.gchat-composer').isVisible(),true,item.name+' the bubble and composer stay in place');
    }
    assert.deepEqual(errors,[]);
    console.log('Private bubble file links: typed references open the file viewer in a new tab, safely, on business and Global records passed.');
  } finally { await browser.close(); server.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
