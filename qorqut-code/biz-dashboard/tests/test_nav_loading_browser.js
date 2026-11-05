// Navigation timing and stale-response checks with deferred API responses; no live data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const server = http.createServer((req, res) => {
  if (req.url.startsWith('/api/')) { res.setHeader('Content-Type','application/json'); return res.end('{}'); }
  const file = req.url === '/' ? 'index.html' : req.url.slice(1);
  try {
    let body = fs.readFileSync(path.join(root,file));
    if (file === 'index.html') body = body.toString().replace('<script src="/js/main.js"></script>','');
    res.setHeader('Content-Type',file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(body);
  } catch { res.statusCode=404;res.end(); }
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    const page=await browser.newPage();page.setDefaultTimeout(5000);
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.evaluate(()=>{
      state.bizs=[{id:'a',slug:'alpha',name:'Alpha',agents:[]}];state.biz='alpha';state.scope='business';state.tab='settings';
      window.calls=[];window.pending={};
      window.fetch=url=>{const key=url.replace(/^\/api/,'');calls.push(key);return new Promise(resolve=>{pending[key]=data=>resolve(new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}}));});};
      window.tree={tree:{children:[]},people:[],graph:{nodes:[],edges:[]}};
      window.nav=goTab('agents');
    });
    assert.equal(await page.locator('#agentlist').count(),1);
    assert.match(await page.locator('#agentrows').textContent(),/Loading agents/);
    assert(await page.evaluate(()=>{const item=document.querySelector('.ritem[data-tab="agents"]'),indicator=document.querySelector('#rind');return indicator.style.transform===`translateY(${item.offsetTop-indicator.offsetTop}px)`;}));
    await page.evaluate(()=>goTab('agents'));
    assert.equal(await page.evaluate(()=>calls.filter(url=>url.endsWith('/agents')).length),1);
    await page.evaluate(()=>{state.ctx='_learning';window.contextNav=goTab('context');});
    assert.equal(await page.locator('#ctxtree').count(),1);
    assert.equal(await page.evaluate(()=>calls.filter(url=>url.endsWith('/learnings')).length),1);
    await page.evaluate(()=>{pending['/biz/alpha/learnings']({items:[]});});
    await page.waitForSelector('#lnsearch');
    assert.equal(await page.locator('#ctxrows [data-p="_learning"]').count(),1);
    // A late Agents response must not replace the Context page.
    await page.evaluate(async()=>{pending['/biz/alpha/agents']([]);pending['/biz/alpha/terminals']([]);await nav;});
    assert.equal(await page.locator('#ctxtree').count(),1);
    await page.evaluate(async()=>{pending['/biz/alpha/context'](tree);await contextNav;});
    assert.equal(await page.evaluate(()=>calls.filter(url=>url.endsWith('/learnings')).length),1);
    // Independent Stories and Tables renders start before the context tree resolves.
    for (const selected of ['_stories','_tables']) {
      await page.evaluate(p=>{
        window.childRenders=0;
        renderStories=renderTables=()=>{childRenders++;};
        state.ctx=p;window.nextRender=renderContext(document.querySelector('#content'));
      },selected);
      assert.equal(await page.evaluate(()=>childRenders),1);
      await page.evaluate(async()=>{pending['/biz/alpha/context'](tree);await nextRender;});
      assert.equal(await page.evaluate(()=>childRenders),1);
    }
    // A context response for a page that was left cannot paint the new page.
    await page.evaluate(()=>{state.ctx='';window.oldContext=renderContext(document.querySelector('#content'));window.nextAgents=goTab('agents');});
    await page.evaluate(async()=>{pending['/biz/alpha/context'](tree);await oldContext;});
    assert.equal(await page.locator('#agentlist').count(),1);
    await page.evaluate(async()=>{pending['/biz/alpha/agents']([]);pending['/biz/alpha/terminals']([]);await nextAgents;});
    await page.evaluate(()=>{
      state.agents=[{name:'alpha'},{name:'beta'}];state.agent='alpha';paintList();
      window.agentShell=document.querySelector('.agsplit');window.agentList=document.querySelector('#agentrows');
      window.switches=[];window.paneSwitches=[];
      mountTerminal=name=>switches.push(name);agpadMount=name=>paneSwitches.push(name);
      window.callsBeforeSwitch=calls.filter(url=>/\/(agents|terminals)$/.test(url)).length;
    });
    await page.locator('#agentrows [data-a="beta"]').click();
    await page.locator('#agentrows [data-a="beta"]').click();
    await page.locator('#agentrows [data-a="alpha"]').click();
    assert.deepEqual(await page.evaluate(()=>switches),['beta','alpha']);
    assert.deepEqual(await page.evaluate(()=>paneSwitches),['beta','alpha']);
    assert(await page.evaluate(()=>agentShell===document.querySelector('.agsplit') && agentList===document.querySelector('#agentrows') && calls.filter(url=>/\/(agents|terminals)$/.test(url)).length===callsBeforeSwitch));
    await page.evaluate(()=>{
      const sample=document.createElement('section');sample.id='learning-test';document.querySelector('#content').replaceChildren(sample);
      sample.innerHTML=lnCardHtml({id:'sample',state:'open',tag:'rule',scope:'biz',text:'Example learning',passes:3,evidence_more:[{owner_quote:'Repeated evidence'}],variants:[{text:'Alternative wording',scope:'biz'}]},[]);
    });
    assert.equal(await page.locator('#learning-test .ln-evidence-tools > *').count(),3);
    const summaries=page.locator('#learning-test .ln-evidence-tools summary');
    await summaries.first().click();
    assert.equal(await page.locator('#learning-test details[open] .lnquote').textContent(),'“Repeated evidence”');
    await summaries.first().click();await summaries.last().click();
    assert.match(await page.locator('#learning-test details[open]').textContent(),/Alternative wording/);
    assert.equal(await summaries.first().evaluate(el=>getComputedStyle(el).borderRadius),'999px');
    assert.deepEqual(errors,[]);
    console.log('Navigation loading browser checks passed');
  } finally {await browser.close();server.close();}
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
