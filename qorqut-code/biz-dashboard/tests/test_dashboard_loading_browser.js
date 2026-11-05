// Static app with explicit server failures; no production access.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try {
  const page=await browser.newPage(),errors=[];let failed=true;
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',route=>{
   const pathname=new URL(route.request().url()).pathname;
   if(pathname==='/api/biz')return route.fulfill({status:failed?503:200,json:failed?{status:'error',error:{message:'Maintenance in progress.'}}:[{slug:'ProjectA',name:'ProjectA',agents:[]}]});
   if(pathname.startsWith('/api/'))return route.fulfill({json:[]});
   const file=path.resolve(root,'.'+(pathname==='/'?'/index.html':pathname));
   return file.startsWith(root+path.sep)&&fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  });
  await page.goto('http://dashboard.test/#ProjectA');
  await page.getByText('Maintenance in progress.',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>Array.isArray(state.bizs)),true);
  failed=false;await page.locator('#business-retry').click();
  // A business deep link opens its Tasks page.
  await page.locator('[data-business-task-page]').waitFor();
  assert.equal(await page.evaluate(()=>state.biz),'ProjectA');
  assert.equal(await page.evaluate(()=>evUrl('/stream')),'/api/stream');
  assert.equal(await page.locator('input[type=password]').count(),0);
  assert.deepEqual(errors,[]);
  console.log('Dashboard server-error recovery, business deep link and token-free loading passed.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
