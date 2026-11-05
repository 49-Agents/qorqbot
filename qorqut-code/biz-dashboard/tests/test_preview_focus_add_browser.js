const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try {
  const page=await browser.newPage({viewport:{width:800,height:600}});
  const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5WQAAAAASUVORK5CYII=','base64');
  await page.route('http://fixture/**',route=>{
   const url=new URL(route.request().url());
   if(url.pathname.endsWith('/metadata'))return route.fulfill({json:{status:'ok',data:{name:'sample.png',path:'sample.png',preview_kind:'image',binary:false,urls:{image:'/image.png',download:'/download',copy:'/files/view?path=sample.png'}}}});
   if(url.pathname==='/image.png')return route.fulfill({contentType:'image/png',body:png});
   return route.fulfill({contentType:'text/html',body:'<main id="fv"></main><div id="fv-toast"></div>'});
  });
  await page.goto('http://fixture/files/view?path=sample.png');
  await page.addStyleTag({path:path.join(root,'css/file-viewer.css')});
  await page.addScriptTag({path:path.join(root,'js/file-viewer.js')});
  await page.waitForFunction(()=>document.querySelector('.fv-image')?.naturalWidth===1);
  assert.equal(await page.locator('.fv-frame').count(),0);
  assert.equal(await page.getByRole('button',{name:'Source',exact:true}).count(),0);
  await page.setViewportSize({width:320,height:400});
  assert.ok(await page.locator('.fv-image').evaluate(el=>el.getBoundingClientRect().right<=innerWidth));
  // Exercise the real modal and Focus add handler, isolating only transport and board refresh.
  const core=fs.readFileSync(path.join(root,'js/core.js'),'utf8');
  await page.addScriptTag({content:`let _modal=null,_menu=null;const SVG_X='x';${core.slice(core.indexOf('function modal('),core.indexOf('// ---------------------------------------------------------------- dropdown'))}
    let requests=[],retryOnce=false;const state={bizs:[]},newRequestId=()=>String(Math.random()),coreData=r=>r.data,toast=()=>{};
    const post=async(op,args)=>{requests.push({op,args});if(retryOnce){retryOnce=false;throw Error('Network error');}return {data:{}};};`});
  const tasks=fs.readFileSync(path.join(root,'js/tasks.js'),'utf8');
  await page.addScriptTag({content:"const esc=s=>String(s??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));"+tasks.slice(tasks.indexOf('// Presentation subtypes share'),tasks.indexOf('(function tInit()'))});
  const focus=fs.readFileSync(path.join(root,'js/agent-focus.js'),'utf8');
  await page.addScriptTag({content:'const agfocusRefresh=async()=>{},agfocusTextHost=()=>null;'+focus.slice(focus.indexOf('function agfocusAdd('))});
  await page.evaluate(()=>agfocusAdd({}));
  await page.locator('[name=title]').fill('Check proposal');
  await page.getByLabel('Task type',{exact:true}).selectOption('story');
  await page.getByRole('button',{name:'Add',exact:true}).click();
  assert.equal(await page.evaluate(()=>requests[0].op),'/root/operations/tasks_create');
  assert.equal(await page.evaluate(()=>requests[0].args.focus.display_name),'Check proposal');
  assert.equal(await page.evaluate(()=>requests[0].args.task_type),'story');
  await page.evaluate(()=>{retryOnce=true;agfocusAdd({});});
  await page.locator('[name=title]').fill('Gallery');await page.locator('[name=link]').fill('https://example.com/gallery');
  await page.getByRole('button',{name:'Add',exact:true}).click();
  await page.getByRole('alert').waitFor();
  await page.getByRole('button',{name:'Add',exact:true}).click();
  const calls=await page.evaluate(()=>requests);
  assert.equal(calls[1].op,'/root/operations/focus_present');assert.deepEqual(calls[1],calls[2]);
  assert.equal(await page.getByRole('dialog').count(),0);
  console.log('Image preview loads/resizes; Focus adds task/link and retries without duplication.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
