const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
 const server=http.createServer((req,res)=>{
  if(req.url==='/'){res.setHeader('content-type','text/html; charset=utf-8');return res.end(`<link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/global-progress.css"><div id="chart" style="margin:24px"></div><script>const esc=s=>String(s);const coreData=x=>x;let calls=0;const pageReads=new AbortController();const pageReadPost=async()=>{calls++;return {series:Array.from({length:365},(_,i)=>{const d=new Date();d.setUTCDate(d.getUTCDate()-364+i);return {date:d.toISOString().slice(0,10),tasks_done:i%9,inbox_messages:i%16,learnings_accepted:i%3,new_tickets:i%5,cold_outreach_emails_sent:i%8}})}};</script><script src="/js/san-juan-time.js"></script><script src="/js/global-progress.js"></script><script>globalProgressMount(document.querySelector('#chart'),[{slug:'one'},{slug:'two'}]);</script>`);}
  const file=path.join(root,req.url);res.setHeader('content-type',req.url.endsWith('.css')?'text/css':'text/javascript; charset=utf-8');res.end(fs.readFileSync(file));
 });await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true});
 try{const page=await browser.newPage({viewport:{width:1440,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(`http://127.0.0.1:${server.address().port}`);
 await page.locator('.gp-today').first().waitFor();assert.equal(await page.locator('.gp-today').count(),3);
 assert.deepEqual(await page.locator('svg g[data-scale-ceiling]').evaluateAll(els=>els.map(el=>[el.className.baseVal,Number(el.dataset.scaleCeiling)])),[['gp-tasks',8],['gp-inbox',16],['gp-learnings',4]]);
 assert.equal(await page.locator('.global-progress-legend').textContent(),'Tasks doneInbox messagesAccepted learnings');
 assert.equal(await page.locator('.gp-cold-outreach').count(),0,'no cold-outreach series');
 assert.doesNotMatch(await page.locator('svg').getAttribute('aria-label'),/cold outreach/);
 assert.deepEqual(await page.locator('.gp-axis-label').allTextContents(),['0','2','4','6','8']);
 assert.equal(await page.locator('.gp-axis-title').textContent(),'Tasks done');
 for(const n of [30,120,365]){await page.getByRole('button',{name:n+' days',exact:true}).click();assert.equal(await page.locator('polyline').first().evaluate(el=>el.points.numberOfItems),n-1);}
 assert.equal(await page.evaluate(()=>calls),1);
 await page.locator('svg').focus();assert.match(await page.locator('output').textContent(),/4 tasks done · 12 inbox messages/);assert.doesNotMatch(await page.locator('output').textContent(),/cold outreach/);
 await page.keyboard.press('ArrowLeft');assert.match(await page.locator('output').textContent(),/3 tasks done · 11 inbox messages/);assert.doesNotMatch(await page.locator('output').textContent(),/cold outreach/);
 await page.getByRole('button',{name:'30 days',exact:true}).click();await page.screenshot({path:'/tmp/global-progress-desktop.png'});
 await page.setViewportSize({width:430,height:850});await page.screenshot({path:'/tmp/global-progress-mobile.png'});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));assert.deepEqual(errors,[]);
 console.log('Chart totals, 30/120/365 ranges, dashed today, cached switches, keyboard and mobile layout passed');
 }finally{await browser.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;});
