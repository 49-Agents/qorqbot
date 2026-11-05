// PR5 (HANDOFF-ui-speedup-stage2, 4.5): js/events.js's EventSource client and
// js/store.js's subscription registry. A synthetic, fully hand-controlled SSE
// endpoint stands in for biz-core's real /api/root/events (PR2), formatted to
// match its documented wire shape exactly (default message = one commit,
// `id:` = seq, `data:` carries kinds/business_id; `event: reset` with no id;
// a bare `retry:` field to make the reconnect test fast and deterministic).
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const businesses=[{id:'business-alpha',slug:'alpha',name:'Alpha',agents:[]}];
let sseRes=null,sseSeq=0,lastEventIdSeen=null;
function sseSend(res,{event,id,data}){
  let msg='';
  if(event)msg+=`event: ${event}\n`;
  if(id!=null)msg+=`id: ${id}\n`;
  msg+=`data: ${JSON.stringify(data??{})}\n\n`;
  res.write(msg);
}
const server=http.createServer((req,res)=>{
  const pathname=new URL(req.url,'http://localhost').pathname;
  if(pathname==='/api/root/events'){
    lastEventIdSeen=req.headers['last-event-id']||null;
    res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-store','Connection':'keep-alive'});
    res.write('retry: 100\n\n'); // dataless: sets the reconnect interval, dispatches no message
    sseRes=res;
    req.on('close',()=>{if(sseRes===res)sseRes=null;});
    return;
  }
  if(pathname.startsWith('/api/')){
    let body=[];
    if(pathname==='/api/biz')body=businesses;
    if(pathname==='/api/session')body={status:'ok',data:{principal_id:'owner'}};
    if(pathname==='/api/me/tasks')body={principal_id:'owner',tasks:[]};
    res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(body));return;
  }
  const file=path.resolve(root,'.'+(pathname==='/'?'/index.html':pathname));
  if(!file.startsWith(root+path.sep)||!fs.existsSync(file)){res.writeHead(404).end();return;}
  res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});
  res.end(fs.readFileSync(file));
});
const waitUntil=async(check,tries=50)=>{for(let i=0;i<tries&&!check();i++)await new Promise(r=>setTimeout(r,100));return check();};
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const url=`http://127.0.0.1:${server.address().port}/#/global/overview`;
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
  try{
    const page=await browser.newPage({viewport:{width:1100,height:800}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(url);
    await page.getByRole('heading',{name:'Your projects'}).waitFor();

    assert(await waitUntil(()=>!!sseRes),'the tab must open one EventSource to /api/root/events on load');

    await page.evaluate(()=>{
      window.__calls=[];
      window.__unsubA=onChange(['tasks'],'business-alpha',()=>window.__calls.push('alpha-tasks'));
      window.__unsubB=onChange(['tasks','signoffs'],null,()=>window.__calls.push('global-any'));
    });

    // An exact-scope match notifies both the business subscriber and the
    // null-scope (every business) aggregate subscriber.
    sseSend(sseRes,{id:++sseSeq,data:{seq:sseSeq,tables:['tasks'],kinds:['tasks'],business_id:'business-alpha',operation:'tasks_create'}});
    await new Promise(r=>setTimeout(r,400));
    assert.deepEqual(await page.evaluate(()=>window.__calls),['alpha-tasks','global-any']);

    // A coarse/global event (business_id: null) a subscription never declared is a no-op.
    await page.evaluate(()=>window.__calls=[]);
    sseSend(sseRes,{id:++sseSeq,data:{seq:sseSeq,tables:[],kinds:['jobs_routines'],business_id:null,operation:'routines_run'}});
    await new Promise(r=>setTimeout(r,400));
    assert.deepEqual(await page.evaluate(()=>window.__calls),[],'neither subscription declared jobs_routines');

    // A coarse/global event for a declared kind reaches only the null-scope subscription.
    await page.evaluate(()=>window.__calls=[]);
    sseSend(sseRes,{id:++sseSeq,data:{seq:sseSeq,tables:['signoffs'],kinds:['signoffs'],business_id:null,operation:'signoffs_notification_tick'}});
    await new Promise(r=>setTimeout(r,400));
    assert.deepEqual(await page.evaluate(()=>window.__calls),['global-any'],'only the null-scope subscription declared signoffs');

    // A different business's event never reaches a business-scoped subscription.
    await page.evaluate(()=>window.__calls=[]);
    sseSend(sseRes,{id:++sseSeq,data:{seq:sseSeq,tables:['tasks'],kinds:['tasks'],business_id:'business-beta',operation:'tasks_create'}});
    await new Promise(r=>setTimeout(r,400));
    assert.deepEqual(await page.evaluate(()=>window.__calls),['global-any'],'a different business must not reach a business-scoped subscription');

    // 3 rapid events collapse into exactly 1 call per matching handler (250ms debounce).
    await page.evaluate(()=>window.__calls=[]);
    for(let i=0;i<3;i++)sseSend(sseRes,{id:++sseSeq,data:{seq:sseSeq,tables:['tasks'],kinds:['tasks'],business_id:'business-alpha',operation:'tasks_update'}});
    await new Promise(r=>setTimeout(r,400));
    assert.deepEqual(await page.evaluate(()=>window.__calls),['alpha-tasks','global-any'],'3 rapid events must debounce into 1 call per handler');

    // A hidden tab queues instead of firing, and flushes once on visibilitychange.
    await page.evaluate(()=>window.__calls=[]);
    await page.evaluate(()=>Object.defineProperty(document,'hidden',{configurable:true,get:()=>true}));
    sseSend(sseRes,{id:++sseSeq,data:{seq:sseSeq,tables:['tasks'],kinds:['tasks'],business_id:'business-alpha',operation:'tasks_update'}});
    await new Promise(r=>setTimeout(r,400));
    assert.deepEqual(await page.evaluate(()=>window.__calls),[],'a hidden tab must queue rather than fire immediately');
    await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'));});
    await new Promise(r=>setTimeout(r,400));
    assert.deepEqual(await page.evaluate(()=>window.__calls),['alpha-tasks','global-any'],'becoming visible must flush the queued invalidation');

    // An explicit reset fires every subscription, regardless of declared kind/scope.
    await page.evaluate(()=>window.__calls=[]);
    sseRes.write('event: reset\ndata: {}\n\n');
    await new Promise(r=>setTimeout(r,400));
    assert.deepEqual((await page.evaluate(()=>window.__calls)).sort(),['alpha-tasks','global-any'],'reset must fire every subscription');

    // A wildcard kinds list (an external writer outside the pool --
    // changes.py's publish_wildcard, wire shape tables/kinds:['*']) fires
    // every subscription too, the same as an explicit reset -- but arrives
    // as a normal onmessage payload, not the `reset` event type, so it needs
    // its own check rather than falling out of storeNotify's per-kind match.
    await page.evaluate(()=>window.__calls=[]);
    sseSend(sseRes,{id:++sseSeq,data:{seq:sseSeq,tables:['*'],kinds:['*'],business_id:null,operation:'external_write'}});
    await new Promise(r=>setTimeout(r,400));
    assert.deepEqual((await page.evaluate(()=>window.__calls)).sort(),['alpha-tasks','global-any'],'a wildcard kinds list must fire every subscription, the same as reset');

    // A dropped connection reconnects on its own (native EventSource
    // behaviour), carries Last-Event-ID, and the reconnect invalidates
    // everything -- the same guarantee as an explicit reset.
    await page.evaluate(()=>window.__calls=[]);
    const droppedId=sseSeq;
    sseRes.end();sseRes=null;
    assert(await waitUntil(()=>!!sseRes),'the browser must reconnect on its own after the connection drops');
    assert.equal(lastEventIdSeen,String(droppedId),'the reconnect must carry Last-Event-ID from the last event this tab saw');
    await new Promise(r=>setTimeout(r,400));
    assert.deepEqual((await page.evaluate(()=>window.__calls)).sort(),['alpha-tasks','global-any'],'a reconnect must invalidate everything, the same as an explicit reset');

    // Unsubscribing actually stops delivery.
    await page.evaluate(()=>{window.__unsubA();window.__unsubB();window.__calls=[];});
    sseSend(sseRes,{id:++sseSeq,data:{seq:sseSeq,tables:['tasks'],kinds:['tasks'],business_id:'business-alpha',operation:'tasks_update'}});
    await new Promise(r=>setTimeout(r,400));
    assert.deepEqual(await page.evaluate(()=>window.__calls),[],'an unsubscribed handler must never fire again');

    assert.deepEqual(errors,[]);
    console.log('Dashboard change events: scope matching, debounce, hidden-tab queueing, reset, native reconnect with Last-Event-ID and unsubscribe all passed');
  }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
