#!/usr/bin/env node
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require('playwright');
const repo = path.resolve(__dirname, '..');
let globalPairs = [{key:'SHARED_KEY',value:'global-value'}], localPairs = [], failLoad = false, failSave = false, releaseSave, businessFetches = 0;
const writes = [], errors = [];
const server = http.createServer(async (req,res) => {
  const url = new URL(req.url,'http://localhost');
  if (url.pathname.startsWith('/api/')) {
    let data = [];
    if (url.pathname === '/api/biz') {
      businessFetches += 1;
      const state = businessFetches === 1 ? 'pending' : 'ready';
      data = [{slug:'studio',name:'Studio',id:'studio-id',agents:[],environment:{mode:'sandbox',state,
        previews:[{port:4173,url:'http://preview.test/app?cap=private'},{port:4174,url:'http://user:secret@preview.test/private'}],
        browser_viewer_url:'https://viewer.test/business-cap'}}];
    }
    if (url.pathname === '/api/me/tasks') data = {principal_id:'owner',tasks:[]};
    if (url.pathname === '/api/session') data = {status:'ok',data:{principal_id:'owner',is_owner:true}};
    if (url.pathname.endsWith('/live')) data = {sessions:[],requests:[]};
    if (url.pathname.endsWith('/env')) {
      if (failLoad && req.method === 'GET' || failSave && req.method === 'POST') {
        res.writeHead(503,{'Content-Type':'application/json'}).end(JSON.stringify({error:'Environment unavailable'}));return;
      }
      const global = url.pathname === '/api/root/env';
      if (req.method === 'POST') {
        let body = '';for await(const chunk of req) body += chunk;
        const pairs = JSON.parse(body).pairs; writes.push({path:url.pathname,pairs});
        if(global) globalPairs = pairs;else localPairs = pairs;
        if (releaseSave === 'hold') await new Promise(resolve => { releaseSave = resolve; });
      }
      data = {pairs:global?globalPairs:localPairs,inherited:global?[]:globalPairs};
    }
    res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify(data));return;
  }
  const file = path.resolve(repo,'.'+(url.pathname==='/'?'/index.html':url.pathname));
  if(!file.startsWith(repo+path.sep)||!fs.existsSync(file)){res.writeHead(404).end();return;}
  res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'}).end(fs.readFileSync(file));
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const origin = 'http://127.0.0.1:'+server.address().port;
 const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
 try{
  const page = await browser.newPage({viewport:{width:1280,height:900}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',r=>new URL(r.request().url()).origin===origin?r.continue():r.abort());
  await page.goto(origin+'/#/global/settings');
  await page.locator('.envrow').waitFor();
  assert.equal(await page.locator('#agent-config').count(),0);
  await page.locator('.ev').fill('rotated-global');
  await page.locator('#envsave').click();
  await page.waitForFunction(()=>!envView.dirty);
  assert.deepEqual(writes[0],{path:'/api/root/env',pairs:[{key:'SHARED_KEY',value:'rotated-global'}]});
  await page.evaluate(async()=>{await selectBiz('studio');goTab('settings');});
  await page.locator('.envrow').waitFor();
  await page.getByRole('heading',{name:'Project environment'}).waitFor();
  assert.equal(await page.locator('.env-runtime-state').innerText(),'Ready');
  assert(businessFetches >= 2); // settings fetched status after the initial pending /api/biz payload
  assert.equal(await page.getByRole('link',{name:'Open private preview'}).count(),1);
  assert.equal(await page.getByRole('link',{name:'Open browser viewer'}).getAttribute('rel'),'noopener noreferrer');
  assert.equal(await page.locator('a[href^="javascript:"]').count(),0);
  assert.equal(await page.locator('#agent-config').count(),0);
  assert(await page.locator('.ev').isDisabled());
  assert.equal(await page.locator('.envsource').innerText(),'Global');
  await page.getByRole('button',{name:'Override SHARED_KEY',exact:true}).click();
  await page.locator('.ev').fill('business-value');
  await page.locator('#envsave').click();await page.waitForFunction(()=>!envView.dirty);
  assert.deepEqual(localPairs,[{key:'SHARED_KEY',value:'business-value'}]);
  assert.equal(globalPairs[0].value,'rotated-global');
  await page.reload();await page.locator('.envrow').waitFor();
  assert.equal(await page.locator('.ev').inputValue(),'business-value');
  await page.getByRole('button',{name:'Use Global for SHARED_KEY'}).click();
  await page.locator('#envsave').click();await page.waitForFunction(()=>!envView.dirty);
  assert.deepEqual(localPairs,[]);
  assert.equal(await page.locator('.ev').inputValue(),'rotated-global');
  await page.locator('#envadd').click();
  await page.locator('.envrow:last-child .ek').fill('PRIVATE');
  await page.locator('.envrow:last-child .ev').fill('business-only');
  failSave=true;
  await page.locator('#envsave').click();await page.getByText('Environment unavailable',{exact:true}).waitFor();
  assert.equal(await page.locator('.envrow:last-child .ev').inputValue(),'business-only');
  assert.equal(await page.locator('#envsave').isDisabled(),false);
  failSave=false;releaseSave='hold';
  await page.locator('#envsave').click();
  while(typeof releaseSave!=='function') await page.waitForTimeout(10);
  await page.evaluate(()=>selectGlobal('settings'));
  await page.getByRole('heading',{name:'Global environment'}).waitFor();
  releaseSave();releaseSave=null;await page.waitForTimeout(100);
  assert.equal(await page.locator('.envrow').count(),1);
  assert.equal(await page.locator('.ev').inputValue(),'rotated-global');
  assert.equal(writes.at(-1).path,'/api/biz/studio/env');
  failLoad=true;await page.reload();await page.locator('#envretry').waitFor();
  assert(await page.locator('#envsave').isDisabled());assert(await page.locator('#envadd').isDisabled());
  failLoad=false;await page.locator('#envretry').click();await page.locator('.envrow').waitFor();
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.locator('.envrow').evaluate(el=>el.getBoundingClientRect().right>innerWidth),false);
  assert.deepEqual(errors,[]);
  fs.mkdirSync('/tmp/global-environment',{recursive:true});
  await page.screenshot({path:'/tmp/global-environment/mobile.png'});
  await page.setViewportSize({width:1280,height:900});
  await page.screenshot({path:'/tmp/global-environment/desktop.png'});
  console.log('Global environment: shared icon, no agent config, save, inheritance, override/revert, refresh, failed requests, scope race and mobile passed.');
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1});
