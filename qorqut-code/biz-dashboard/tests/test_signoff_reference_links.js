const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try {
  const context=await browser.newContext();
  await context.route('http://qorqut:8857/**',r=>r.fulfill({contentType:'text/plain',body:'Mockup fixture'}));
  const page=await context.newPage();await page.setContent('<main></main>');
  await page.addScriptTag({path:path.join(__dirname,'../js/safe-links.js')});
  const source=fs.readFileSync(path.join(__dirname,'../js/signoffs.js'),'utf8');
  const preview=source.slice(source.indexOf('  function previewHtml('),source.indexOf('  function completePreview('));
  await page.addScriptTag({content:`const esc=safeLinkEsc;const scopeText=()=> 'Global';${preview}`});
  const explanation='[Open the homepage mockups](http://qorqut:8857/media/compare.html).\n\n[Review gallery](http://qorqut:8857/) <img src=x onerror=alert(1)> [bad](javascript:alert(1))';
  for(const signoff of [{input:{explanation}},{preview:{changes:[],rationale:explanation}}]){
   await page.evaluate(s=>document.querySelector('main').innerHTML=previewHtml(s),signoff);
   const link=page.getByRole('link',{name:'Open the homepage mockups'});
   assert.equal(await link.getAttribute('href'),'http://qorqut:8857/media/compare.html');
   assert.equal(await link.getAttribute('rel'),'noopener noreferrer');
   assert.equal(await page.locator('main a').count(),2);assert.equal(await page.locator('main img,main script').count(),0);
   const [popup]=await Promise.all([context.waitForEvent('page'),link.click()]);await popup.waitForLoadState();
   assert.equal(popup.url(),'http://qorqut:8857/media/compare.html');await popup.close();
  }
  console.log('Signoff references: input/proposal links render and open safely; HTML and unsafe schemes remain text.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
