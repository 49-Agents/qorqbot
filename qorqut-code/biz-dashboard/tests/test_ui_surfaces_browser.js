// Isolated interaction checks; no API or live workspace data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.join(__dirname, '..');
const index = fs.readFileSync(path.join(root,'index.html'),'utf8');
const styles = index.match(/<link rel="stylesheet"[^>]+>/g).join('\n');
const quietControls = `<section class="agpad" style="height:auto"><button id="pane-hint" title="Refresh records" aria-label="Refresh records">Refresh</button><button id="pane-custom" aria-label="Open pane menu">Menu</button><button id="pane-icon" title="Pane action"><svg width="14" height="14" aria-hidden="true"><circle r="4" cx="7" cy="7"/></svg></button><button id="pane-data" class="data-cell" title="Saved table value">Value</button><abbr id="pane-prose" title="Prose explanation">Prose</abbr></section><div role="navigation"><button id="nav-hint" title="Go to reports" aria-label="Reports">Reports</button></div>`;
const fixture = `${styles}<body style="padding:40px"><button id="wsbtn" hidden></button><button id="hint" title="A helpful hint">Hover</button><button id="custom" title="Fallback">Custom</button><select id="choice"><option value="same">Alpha</option><option disabled>Disabled</option><optgroup label="Group"><option value="same">Beta</option></optgroup></select><button id="after">After</button>${quietControls}<script src="/js/core.js"></script><script src="/js/san-juan-time.js"></script><script src="/js/ui-surfaces.js"></script>`;
const server = http.createServer((req,res)=>{
  if(req.url === '/') {res.setHeader('Content-Type','text/html');return res.end(fixture);}
  const file=path.join(root,req.url);
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/css');
  try{res.end(fs.readFileSync(file));}catch{res.statusCode=404;res.end();}
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  try{
    const page=await browser.newPage();page.setDefaultTimeout(5000);const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.evaluate(()=>{window.changes=[];choice.addEventListener('input',()=>changes.push('input'));choice.addEventListener('change',()=>changes.push('change'));tip(custom,'Custom hint');});
    await page.locator('#hint').hover();await page.waitForSelector('.tip');
    assert.equal(await page.locator('.tip').textContent(),'A helpful hint');
    assert.equal(await page.locator('#hint').getAttribute('title'),'');
    assert.equal(await page.locator('.tip').evaluate(el=>getComputedStyle(el).borderRadius),'14px');
    await page.evaluate(()=>hint.title='Updated hint');await page.waitForFunction(()=>document.querySelector('.tip')?.textContent==='Updated hint');
    await page.locator('#custom').hover();await page.waitForFunction(()=>document.querySelector('.tip')?.textContent==='Custom hint');
    assert.equal(await page.locator('.tip').count(),1);assert.equal(await page.locator('#hint').getAttribute('title'),'Updated hint');
    await page.evaluate(()=>tip(document.querySelector('#pane-custom'),'Custom pane hint'));
    for (const selector of ['#pane-hint','#pane-custom','#pane-icon','#nav-hint']) {
      await page.locator(selector).hover();await page.waitForTimeout(160);
      assert.equal(await page.locator('.tip').count(),0,selector+' has no hover hint');
      await page.locator(selector).focus();await page.waitForTimeout(160);
      assert.equal(await page.locator('.tip').count(),0,selector+' has no focus hint');
      assert(await page.locator(selector).getAttribute('aria-label'),'accessible name is preserved');
    }
    assert.equal(await page.locator('#pane-icon').getAttribute('aria-label'),'Pane action','title-only icon retains its accessible name');
    await page.locator('#pane-data').hover();await page.waitForFunction(()=>document.querySelector('.tip')?.textContent==='Saved table value');
    await page.locator('#pane-prose').hover();await page.waitForFunction(()=>document.querySelector('.tip')?.textContent==='Prose explanation');
    await page.evaluate(()=>document.body.classList.add('agent-pane-embedded'));
    await page.locator('#custom').hover();await page.waitForTimeout(160);
    assert.equal(await page.locator('.tip').count(),0,'embedded control custom hint is suppressed');
    await page.locator('#hint').focus();await page.waitForTimeout(160);
    assert.equal(await page.locator('.tip').count(),0,'embedded control title hint is suppressed');
    await page.locator('#pane-data').hover();await page.waitForFunction(()=>document.querySelector('.tip')?.textContent==='Saved table value');
    await page.evaluate(()=>document.body.classList.remove('agent-pane-embedded'));
    await page.locator('#choice').click();await page.waitForSelector('.select-menu');
    await page.locator('.mi[aria-disabled="true"]').click({force:true});assert.equal(await page.locator('.select-menu').count(),1);
    await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');
    assert.equal(await page.locator('#choice').evaluate(el=>el.selectedIndex),2);
    assert.deepEqual(await page.evaluate(()=>changes),['input','change']);
    assert.equal(await page.locator('#choice').getAttribute('aria-expanded'),'false');
    await page.locator('#choice').focus();await page.keyboard.press('Space');await page.keyboard.press('Escape');
    assert.equal(await page.locator('.menu').count(),0);
    await page.keyboard.press('ArrowDown');await page.keyboard.press('a');await page.keyboard.press('Enter');
    assert.equal(await page.locator('#choice').evaluate(el=>el.selectedIndex),0);
    await page.keyboard.press('Space');await page.keyboard.press('Tab');assert.equal(await page.locator('.menu').count(),0);
    await page.setViewportSize({width:320,height:500});await page.locator('#choice').click();
    assert(await page.locator('.menu').evaluate(el=>el.getBoundingClientRect().right<=innerWidth));
    await page.keyboard.press('Escape');
    assert(!index.includes('data-tab="code"'));assert.deepEqual(errors,[]);
    console.log('UI surface browser checks passed');
  }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
