#!/usr/bin/env node
// Workspace navigation and scope isolation against synthetic read-only business APIs.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo = path.resolve(__dirname, '..');
const requests = [], errors = [];
let businesses = [{slug:'studio',name:'Example Studio',id:'10000000-0000-4000-8000-000000000001',agents:[]}], releaseContext, routinesError = false;
const server = http.createServer((req,res) => {
  const url = new URL(req.url,'http://localhost');
  if (url.pathname.startsWith('/api/')) {
    requests.push({url:req.url,method:req.method});
    let data = [];
    if (url.pathname === '/api/biz') data = businesses;
    if (url.pathname === '/api/me/tasks') data = {principal_id:'owner', tasks:[]};
    if (url.pathname === '/api/root/routines') data = routinesError ? {error:'Global routines unavailable'} : {routines:[{id:'private',scope:'biz',name:'Business-only routine'}],programs:[],enabled:true,root:true,current:{id:'business-job',definition_ref_json:{scope:'business'}},queued:[],invalid:[]};
    if (url.pathname === '/api/session') data = {status:'ok',data:{principal_id:'owner'}};
    if (url.pathname === '/api/root/agents') data = {status:'ok',data:{agents:[]}};
    if (url.pathname.startsWith('/api/biz/') && url.pathname.endsWith('/tasks')) data = [
      {id:'one',title:'Review launch plan',assignee_ids:['agent','owner'],status:'open',closed:false},
      {id:'two',title:'Agent work',assignee_ids:['agent'],status:'open',closed:false},
      {id:'three',title:'Finished work',assignee_ids:['owner'],status:'done',closed:true},
      {id:'four',title:'Archived work',assignee_ids:['owner'],status:'open',closed:false,archived:true},
      {id:'five',title:'Custom closed status',assignee_ids:['owner'],status:'resolved',closed:true}];
    if (url.pathname.endsWith('/log')) data = [{ts:new Date().toISOString()}, {ts:new Date().toISOString()}];
    if (url.pathname.endsWith('/live')) data = {sessions:[],signoffs_pending:2,platform_signoffs:[{state:'pending'}, {state:null}, {state:'applied'}]};
    if (url.pathname.endsWith('/context')) {
      data = {tree:{children:[]},pillars:[],people:[],inbox:[]};
      if (releaseContext === 'hold') { releaseContext = () => res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify(data)); return; }
    }
    if (url.pathname.endsWith('/learnings')) data = {items:[]};
    res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify(data)); return;
  }
  const file = path.resolve(repo,'.'+(url.pathname==='/'?'/index.html':url.pathname));
  if (!file.startsWith(repo+path.sep)||!fs.existsSync(file)) {res.writeHead(404).end();return;}
  res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'}).end(fs.readFileSync(file));
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',r=>new URL(r.request().url()).origin===origin?r.continue():r.abort());
    await page.goto(origin+'/#/global/overview');
    await page.getByRole('heading',{name:'Your projects'}).waitFor();
    await page.locator('.global-metric').first().waitFor();
    assert.deepEqual(await page.locator('.global-metric strong').allTextContents(),['1','2','1']);
    assert.equal(await page.locator('.global-sidebar').count(),0);
    assert.equal(await page.locator('.global-business details').count(),0);
    assert.equal(await page.locator('.global-spark-point').count(),14);
    assert.equal(await page.locator('.global-action-count strong').innerText(),'2');
    assert.equal(await page.locator('.global-spark').getAttribute('data-y-max'),'20');
    const scales = await page.evaluate(() => [0, 19, 20, 21, 47].map(count => {
      const host = document.createElement('div');
      host.innerHTML = globalActivityChart(Array.from({length:count}, () => ({ts:new Date().toISOString()})));
      const svg = host.querySelector('svg');
      return {max: Number(svg.dataset.yMax), points: svg.querySelector('polyline').getAttribute('points').split(' ').map(p => Number(p.split(',')[1]))};
    }));
    assert.deepEqual(scales.map(s=>s.max),[20,20,20,30,50]);
    assert.ok(scales.every(s=>s.points.every(y=>y>=4 && y<=64)));

    const counts = await page.evaluate(() => globalActivityDays([
      {ts:'2026-08-25T23:59:59Z'}, {ts:'2026-08-26T00:00:00Z'},
      {ts:'2026-08-27T01:00:00+02:00'}, {ts:'2026-09-08T09:00:00Z'},
      {ts:'2026-09-08T13:00:00Z'}, {ts:'invalid'}
    ], new Date('2026-09-08T12:00:00Z')).map(d=>d.count));
    assert.deepEqual(counts,[2,0,0,0,0,0,0,0,0,0,0,0,0,1]);
    const width = (await page.locator('.global-business').boundingBox()).width;
    assert.ok(width >= 424 && width <= 426);
    assert.equal(await page.locator('.global-business-head .global-action-count svg').count(),1);
    assert.equal(await page.locator('.global-spark').evaluate(el=>el.getBoundingClientRect().height),70);
    for (const name of ['analytics']) {
      const item = page.locator(`[data-global-tab="${name}"]`);
      assert.equal(await item.getAttribute('aria-disabled'),'true');
      assert.match(await item.getAttribute('title'),/Coming soon/);
      await item.evaluate(el=>el.click());
      assert.equal(await page.evaluate(()=>state.globalTab),'overview');
    }
    await page.locator('[data-global-tab="learning"]').click();
    await page.getByRole('heading', {name:'Learning',exact:true}).waitFor();
    await page.waitForFunction(() => _ln.biz === 'global');
    assert.ok(requests.some(r => r.url === '/api/root/learnings'));
    assert.equal(await page.evaluate(() => state.biz), null);
    await page.locator('[data-global-tab="overview"]').click();
    assert.equal(await page.locator('[data-global-tab=requests], [data-global-tab=evolution]').count(),0);
    for (const removed of ['requests','evolution']) {
      await page.evaluate(tab=>selectGlobal(tab),removed);
      assert.equal(await page.evaluate(()=>state.globalTab),'overview');
    }
    assert.equal(await page.evaluate(()=>state.biz),null);
    assert.equal(await page.locator('#thandle').count(),0);
    await page.locator('#wsbtn').click();
    assert.equal(await page.locator('.menu .mi').first().innerText(),'Global');
    await page.keyboard.press('Escape');
    for (const tab of ['brief','routines','signoffs']) assert.equal(await page.locator('.global-rail[data-global-tab="'+tab+'"]').count(),0);
    await page.locator('.global-rail[data-global-tab="agents"]').click();
    await page.getByText('No global agents yet.').waitFor();
    await page.locator('.global-rail[data-global-tab="overview"]').click();
    await page.keyboard.press('Tab'); await page.keyboard.press('Tab');
    assert.deepEqual(await page.locator('#taskspanel .ttab').evaluateAll(els=>els.map(el=>el.dataset.v)),['tasks','signoffs','routines']);
    await page.locator('#taskspanel .ttab[data-v="routines"]').click();
    await page.locator('#tbody #rtnew').waitFor();
    await page.waitForFunction(()=>rtData && rtGlobal());
    assert.equal(await page.locator('#tbody .rtrow').count(),0);
    assert.equal(await page.locator('#tbody #rtkill').count(),0);
    assert.ok(requests.some(r=>r.url === '/api/root/routines'));
    routinesError = true;
    await page.evaluate(()=>rtRefresh());
    assert.equal(await page.locator('#tbody #rtnew').isDisabled(),true);
    assert.match(await page.locator('#tbody [role="alert"]').innerText(),/Global routines unavailable/);
    routinesError = false;
    await page.locator('#rtretry').click();
    await page.waitForFunction(()=>rtData && !rtError);
    assert.equal(await page.locator('#tbody #rtnew').isDisabled(),false);
    await page.reload();
    await page.getByRole('heading',{name:'Your projects',exact:true}).waitFor();
    await page.locator('.global-metric').first().waitFor();
    await page.waitForTimeout(100);
    const before = requests.length;
    await page.evaluate(()=>{refreshSide();refreshTasks();openTasks(true);tSetView('journal');goTab('agents')});
    await page.keyboard.down('Tab');await page.keyboard.press('t');await page.keyboard.up('Tab');
    await page.waitForTimeout(650);
    assert.deepEqual(requests.slice(before).filter(r=>r.url!=='/api/me/tasks'),[]);
    assert.equal(await page.locator('#thandle').isVisible(),true);
    console.log('ok business summaries, one navigation, coming-soon items, and no background business polling');
    await page.locator('#wsbtn').click();await page.locator('.menu .mi').filter({hasText:'Example Studio'}).click();
    await page.locator('#agentrows').waitFor();
    assert.equal(await page.evaluate(()=>state.biz),'studio');
    assert.equal(await page.locator('#taskswrap').evaluate(el=>el.hidden),false);
    // A slow business context response must not replace the Global workspace.
    releaseContext = 'hold';
    await page.evaluate(()=>{goTab('context');});
    const contextDeadline = Date.now() + 10000;
    while(typeof releaseContext!=='function' && Date.now() < contextDeadline) await page.waitForTimeout(10);
    assert.equal(typeof releaseContext, 'function', JSON.stringify({errors, requests: requests.slice(-10), state: await page.evaluate(() => ({scope:state.scope,tab:state.tab}))}));
    await page.locator('#wsbtn').click();await page.locator('.workspace-global').click();
    await page.getByRole('heading',{name:'Your projects',exact:true}).waitFor();
    releaseContext();releaseContext=null;
    await page.waitForTimeout(150);
    assert.equal(await page.locator('#ctxtree').count(),0);
    await page.goBack();
    await page.locator('#ctxtree').waitFor();
    await page.goForward();
    await page.getByRole('heading',{name:'Your projects',exact:true}).waitFor();
    console.log('ok business return, browser history, and delayed business response isolation');
    await page.locator('.global-rail[data-global-tab="overview"]').click();
    await page.locator('.global-metric').first().waitFor();
    await page.waitForTimeout(350);
    if(process.env.GLOBAL_SCREENSHOTS){fs.mkdirSync(process.env.GLOBAL_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.GLOBAL_SCREENSHOTS,'global-desktop.png')});}
    await page.setViewportSize({width:390,height:844});
    const outside = await page.locator('.global-business').evaluateAll(els=>els.some(el=>{const r=el.getBoundingClientRect();return r.left<0||r.right>innerWidth}));
    assert.equal(outside,false);
    await page.locator('.global-rail[data-global-tab="overview"]').focus();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.dataset.globalTab),'agents');
    await page.keyboard.press('Enter');
    await page.getByRole('heading',{name:'Agents',exact:true}).waitFor();
    if(process.env.GLOBAL_SCREENSHOTS)await page.screenshot({path:path.join(process.env.GLOBAL_SCREENSHOTS,'global-mobile.png')});
    businesses=[];
    await page.goto(origin);
    await page.getByRole('heading',{name:'Agents',exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>state.scope),'global');
    assert.equal(requests.some(r=>r.method!=='GET'||/\/biz\/(null|undefined|global)\//.test(r.url)),false);
    assert.deepEqual(errors,[]);
    console.log('ok narrow layout, keyboard navigation, and installation without businesses');
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1});
