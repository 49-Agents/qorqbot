// Business Browser resource against the mocked owner-only /biz/<slug>/browser-surfaces route; no live browser or agents.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const scope={kind:'ecosystem',allowed_business_ids:['b-studio','b-acme'],global_capabilities:[]};
const agents=[['g-ordu','ordubashy'],['g-founder','founder'],['g-marketing','global-worker'],['g-learn','learning-applier']]
 .map(([agent_id,role_id])=>({agent_id,name:role_id,display_name:role_id,role_id,scope}));
// Businesses carry no agents: the browser is a business resource.
const bizs=[{id:'b-studio',slug:'studio',name:'Studio',owner:'Owner',agents:[]},{id:'b-acme',slug:'acme',name:'Acme',owner:'Owner',agents:[]}];
const one=(id,extra='')=>({id,label:id,kind:'browser',viewer_url:`http://dashboard.test/screen/${id}${extra}`});
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try {
  const errors=[],reads=[];
  let surfaces={
   studio:{business_id:'b-studio',configured:true,browsers:[one('studio-browser','?qorqut_width=1280&qorqut_height=800')],desktop:null},
   acme:{business_id:'b-acme',configured:false,browsers:[],desktop:null},
  };
  let hold=null,fail=false;
  const routes=async page=>{
   page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',async route=>{
    const url=new URL(route.request().url()),p=url.pathname;
    if(url.hostname!=='dashboard.test')return route.abort();
    if(p.startsWith('/screen/'))return route.fulfill({contentType:'text/html',body:'<body style="background:#223">stream</body>'});
    if(p==='/api/biz')return route.fulfill({json:bizs});
    if(p==='/api/me/tasks')return route.fulfill({json:{tasks:[]}});
    if(p==='/api/root/agents')return route.fulfill({json:{status:'ok',data:{agents}}});
    const m=p.match(/^\/api\/biz\/([^/]+)\/browser-surfaces$/);
    if(m){
     reads.push(m[1]);
     if(hold&&hold.slug===m[1])await new Promise(r=>{hold.release=r;hold.reached();});
     if(fail)return route.fulfill({status:503,json:{status:'error',error:{code:'unavailable',message:'Sandbox viewer offline at 10.0.0.9'}}});
     return route.fulfill({json:surfaces[m[1]]});
    }
    if(/^\/api\/biz\/[^/]+\/(agents|terminal)/.test(p))errors.push('unexpected agent read '+p);
    if(p.startsWith('/api/'))return route.fulfill({json:[]});
    const file=path.resolve(root,'.'+(p==='/'?'/index.html':p));
    return file.startsWith(root+path.sep)&&fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
   });
  };
  const page=await browser.newPage({viewport:{width:1200,height:850}});
  await routes(page);
  await page.goto('http://dashboard.test/#studio');
  await page.locator('[data-business-task-page]').waitFor();
  const frames=()=>page.locator('iframe.agbrowser-frame');

  // The Browser route opens its one browser; no agent, picker, terminal, IDs or endpoint text.
  await page.evaluate(()=>goTab('browser'));
  await frames().waitFor();
  assert.equal(await frames().getAttribute('src'),'http://dashboard.test/screen/studio-browser');
  assert.equal(await frames().getAttribute('aria-label'),'Studio browser');
  await page.waitForFunction(()=>document.querySelector('iframe.agbrowser-frame').style.height!=='');
  const box=await frames().boundingBox();
  assert(Math.abs(box.width/box.height-1280/800)<0.02,JSON.stringify(box));
  const text=await page.locator('[data-business-browser]').innerText();
  assert(!/studio-browser|b-studio|browser-surfaces|configured/i.test(text),text);
  assert.equal(await page.locator('[data-business-browser] [data-browser-surface], [data-business-browser] [data-agent-mode], .agent-terminal').count(),0);
  assert.deepEqual(reads,['studio']);

  // Never polled; Reconnect re-reads once and leaves one stream.
  await page.waitForTimeout(1200);
  assert.deepEqual(reads,['studio']);
  await page.getByRole('button',{name:'Reconnect'}).click();
  await page.waitForFunction(()=>document.querySelectorAll('iframe.agbrowser-frame').length===1);
  assert.deepEqual(reads,['studio','studio']);

  // Leaving for another resource disposes the frame.
  await page.evaluate(()=>goTab('tasks'));
  await page.locator('[data-business-task-page]').waitFor();
  assert.equal(await page.locator('iframe').count(),0);

  // A late answer for the business just left never mounts in the next one.
  hold={slug:'studio'};const reached=new Promise(r=>hold.reached=r);
  await page.evaluate(()=>{goTab('browser');});
  await reached;
  await page.evaluate(()=>selectBiz('acme'));
  await page.getByText('Browser unavailable.',{exact:true}).waitFor();
  hold.release();hold=null;
  await page.waitForTimeout(150);
  assert.equal(await page.locator('iframe').count(),0,'Acme has no browser and Studio\'s late answer is dropped');
  assert.equal(await page.evaluate(()=>state.biz),'acme');

  // Refused: another business's id, more than one browser, an unsafe viewer URL, or a failed read.
  const refused=async(answer,message)=>{
   surfaces.acme=answer;
   await page.getByRole('button',{name:'Reconnect'}).click();
   await page.getByText(message,{exact:true}).waitFor();
   assert.equal(await page.locator('iframe').count(),0,JSON.stringify(answer));
  };
  await refused({business_id:'b-studio',configured:true,browsers:[one('studio-browser')],desktop:null},"Couldn't load. Retry");
  await refused({business_id:'b-acme',configured:true,browsers:[one('acme-a'),one('acme-b')],desktop:null},"Couldn't load. Retry");
  await refused({business_id:'b-acme',configured:true,browsers:[{...one('acme-a'),viewer_url:'javascript:alert(1)'}],desktop:null},'Browser unavailable.');
  await refused({business_id:'b-acme',configured:true,browsers:[],desktop:null},'Browser unavailable.');
  fail=true;
  await refused(surfaces.acme,"Couldn't load. Retry");
  assert.equal(await page.getByText('10.0.0.9').count(),0,'the server message is never shown');
  fail=false;
  surfaces.acme={business_id:'b-acme',configured:true,browsers:[one('acme-browser')],desktop:null};
  await page.getByRole('button',{name:'Reconnect'}).click();
  await frames().waitFor();
  assert.equal(await frames().getAttribute('src'),'http://dashboard.test/screen/acme-browser');
  // Switching business while a stream is open disposes it before the next read.
  await page.evaluate(()=>selectBiz('studio'));
  await page.waitForFunction(()=>[...document.querySelectorAll('iframe')].map(f=>f.src).join()==='http://dashboard.test/screen/studio-browser');

  // No global agent pane offers the retired Project management entry any more; the embedded Project
  // workspace itself still renders its Business Browser section when opened directly.
  assert.deepEqual(await page.evaluate(()=>['ordubashy','founder','global-worker','learning-applier'].map(r=>agentPaneViews('global',r).includes('business'))),[false,false,false,false]);
  assert.equal(await page.evaluate(()=>agentWorkspaceSections('studio').some(([key])=>key==='browser')),true);
  const embed=await browser.newPage({viewport:{width:900,height:700}});
  await routes(embed);
  await embed.goto('http://dashboard.test/?agent-pane-embed=business&business=studio&section=browser#studio');
  await embed.locator('iframe.agbrowser-frame').waitFor();
  assert.equal(await embed.locator('iframe.agbrowser-frame').getAttribute('src'),'http://dashboard.test/screen/studio-browser');

  assert.deepEqual(errors,[]);
  console.log('Business Browser: one business resource, exact-business refusal, disposal, no polling and embedded section passed.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
