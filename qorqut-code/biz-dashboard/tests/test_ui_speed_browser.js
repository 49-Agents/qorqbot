#!/usr/bin/env node
// Workspace navigation and scope isolation against synthetic read-only business APIs.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo = path.resolve(__dirname, '..');
const requests = [], errors = [];
let holdSummary = false, pendingSummary;
const server = http.createServer((req,res) => {
  const url = new URL(req.url,'http://localhost');
  if (url.pathname.startsWith('/api/')) {
    requests.push({url:req.url,method:req.method});
    let data = [];
    if (url.pathname === '/api/biz') data = [{slug:'studio',name:'Example Studio',id:'business-one',agents:[]}];
    if (url.pathname === '/api/me/tasks') data = {principal_id:'owner', tasks:[]};
    if (url.pathname.endsWith('/dashboard_summary_get')) {
      data = {status:'ok',data:{items:[{slug:'studio',tasks:1,signoffs:2,requests:1,activity:Array.from({length:14},(_,i)=>({date:new Date(Date.now()-(13-i)*864e5).toISOString().slice(0,10),count:i===13?2:0}))}]}};
      if (holdSummary) { pendingSummary = res; return; }
    }
    if (url.pathname.endsWith('/context_navigation_get')) data={status:'ok',data:{navigation_only:true,counts:{story:0,table:0,person:0}}};
    if (url.pathname.endsWith('/context')) data=url.searchParams.get('view')==='graph'
      ? {graph_only:true,graph:{nodes:[],edges:[]}}
      : {tree:{children:[]},graph:{nodes:[],edges:[]},pillars:[],people:[],inbox:[],dumps:[]};
    if (url.pathname === '/api/session') data = {status:'ok',data:{principal_id:'owner'}};
    if (url.pathname === '/api/root/agents') data = {status:'ok',data:{agents:[{agent_id:'ordu',name:'ordubashy',display_name:'Ordubashy',role_id:'ordubashy'}]}};
    res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify(data)); return;
  }
  const file = path.resolve(repo,'.'+(url.pathname==='/'?'/index.html':url.pathname));
  if (!file.startsWith(repo+path.sep)||!fs.existsSync(file)) {res.writeHead(404).end();return;}
  res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'}).end(fs.readFileSync(file));
});

(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser = await chromium.launch({headless:true});
  const origin = 'http://127.0.0.1:'+server.address().port;
  try {
    const page=await browser.newPage();
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',r=>new URL(r.request().url()).origin===origin?r.continue():r.abort());
    await page.goto(origin+'/#/global/overview');
    await page.locator('.global-metric').first().waitFor();
    assert.deepEqual(await page.locator('.global-metric strong').allTextContents(),['1','2']);
    assert.equal(requests.filter(r=>r.url.endsWith('/dashboard_summary_get')).length,1);
    assert.equal(requests.filter(r=>/\/biz\/studio\/(live|tasks|log)/.test(r.url)).length,0);
    await page.evaluate(()=>{window.mountedAgents=[];mountTerminal=(agent)=>mountedAgents.push(agent);});
    await page.locator('[data-global-tab="agents"]').click();
    await page.locator('[data-global-agent="ordu"]').waitFor();
    assert.equal(requests.filter(r=>r.url.endsWith('/terminals')).length,0);
    assert.ok(await page.evaluate(()=>mountedAgents.includes('ordu')));

    holdSummary=true;
    await page.locator('[data-global-tab="overview"]').click();
    // Previous totals paint immediately while refresh is held.
    await page.locator('.global-metric').first().waitFor();
    await page.waitForFunction(()=>!pageReads.signal.aborted);
    const started=Date.now();
    while(!pendingSummary && Date.now()-started<3000) await new Promise(r=>setTimeout(r,10));
    assert.ok(pendingSummary);
    const aborted=new Promise(resolve=>pendingSummary.on('close',resolve));
    await page.locator('[data-global-tab="agents"]').click();
    await page.locator('[data-global-agent="ordu"]').waitFor();
    await Promise.race([aborted,new Promise((_,reject)=>setTimeout(()=>reject(new Error('Navigation did not abort the summary read')),3000))]);
    await page.evaluate(()=>{
      openLearning=()=>{document.querySelector('#ctxview').textContent='Learning ready';};
      state.scope='business';state.biz='studio';state.tab='context';state.ctx='_learning';renderTab();
    });
    await page.getByText('Learning ready',{exact:true}).waitFor();
    await page.waitForFunction(()=>_ctxData?.navigation_only);
    assert.equal(requests.filter(r=>r.url==='/api/biz/studio/context').length,0);
    await page.evaluate(()=>ctxGo(''));
    await page.waitForFunction(()=>_ctxData?.graph_only);
    await page.locator('#topicmap').waitFor();
    assert.equal(requests.filter(r=>r.url==='/api/biz/studio/context?view=graph').length,1);
    assert.equal(requests.filter(r=>r.url==='/api/biz/studio/context').length,0);
    await page.evaluate(()=>ctxGo('_people'));
    await page.waitForFunction(()=>_ctxData && !_ctxData.navigation_only && !_ctxData.graph_only);
    assert.equal(requests.filter(r=>r.url==='/api/biz/studio/context').length,1);
    assert.deepEqual(errors,[]);
    console.log('Speed: one summary load, no business fanout, immediate cached totals, navigation cancellation, agents mount without terminal status.');
  } finally {pendingSummary?.destroy();await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
