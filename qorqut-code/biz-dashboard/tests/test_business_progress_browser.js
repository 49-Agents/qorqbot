const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
let fail = false, held = null;
const requests = [];
const server = http.createServer(async (req,res) => {
  if (req.url.startsWith('/api/biz/')) {
    let body=''; for await (const c of req) body+=c;
    requests.push([req.url,JSON.parse(body)]);
    if (held) { const wait=held; held=null; await wait; }
    res.setHeader('Content-Type','application/json');
    if (fail) { fail=false; return res.end(JSON.stringify({error:'Temporary failure'})); }
    const other = req.url.includes('/other/');
    const data = {totals:{tasks_done:other ? 5 : 8,actions:20,signoffs_approved:7},undated:{tasks_done:3},coverage:'Recorded history only.',series:Array.from({length:JSON.parse(body).days},(_,i)=>({date:`2026-09-${String(i%28+1).padStart(2,'0')}`,tasks_done:i%2,actions:i%5,signoffs_approved:i%3}))};
    return res.end(JSON.stringify({data}));
  }
  if (req.url==='/') {
    res.setHeader('Content-Type','text/html');
    return res.end(`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/business-progress.css"></head><body style="overflow:auto"><main id="host"></main><script>
      window.state={biz:'one',tab:'progress',scope:'business'};
      window.esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.post=async(p,b)=>{const r=await fetch('/api'+p,{method:'POST',body:JSON.stringify(b)});return r.json();};window.coreData=r=>{if(r.error)throw new Error(r.error);return r.data;};
    </script><script src="/js/san-juan-time.js"></script><script src="/js/business-progress.js"></script><script>renderBusinessProgress(document.querySelector('#host'));</script></body></html>`);
  }
  const f=path.resolve(root,'.'+req.url);
  if (!f.startsWith(root+path.sep)||!fs.existsSync(f)) {res.statusCode=404;return res.end();}
  res.setHeader('Content-Type',f.endsWith('.js')?'text/javascript':'text/css');res.end(fs.readFileSync(f));
});
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1100,height:850}}),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.locator('.business-progress-metric').first().waitFor();
    assert.equal(await page.locator('.milestone-card,.milestone-history,[data-progress-view]').count(),0,'no Milestones or Research views');
    assert.equal(await page.getByRole('heading',{name:'Progress'}).count(),1);
    assert.equal(await page.locator('input,textarea,select,img').count(),0);
    await page.screenshot({path:'/tmp/business-progress-desktop.png'});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.evaluate(()=>{state.tab='progress';renderBusinessProgress(document.querySelector('#host'));});
    await page.locator('.business-progress-metric').first().waitFor();
    assert.equal(await page.locator('.business-progress-metric').count(),3);
    await page.getByRole('button',{name:'90 days',exact:true}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.business-progress-chart rect').length===270);
    assert.equal(requests.at(-1)[1].days,90);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:'/tmp/business-progress-mobile.png'});
    fail=true;
    await page.getByRole('button',{name:'30 days',exact:true}).click();
    await page.getByRole('button',{name:'Retry',exact:true}).click();
    await page.locator('.business-progress-metric').first().waitFor();
    let release; held=new Promise(r=>release=r);
    await page.evaluate(()=>{state.tab='progress';renderBusinessProgress(document.querySelector('#host'));});
    await page.waitForTimeout(50);
    await page.evaluate(()=>{state.biz='other';renderBusinessProgress(document.querySelector('#host'));});
    await page.locator('.business-progress-metric strong',{hasText:'5'}).first().waitFor();
    release(); await page.waitForTimeout(100);
    assert.equal(await page.locator('.business-progress-metric strong').first().textContent(),'5','a late read for the previous project never paints');
    assert.deepEqual(errors,[]);
    console.log('Progress: read-only, no Milestones/Research, periods, retry, stale scope and narrow layout passed.');
  } finally {await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
