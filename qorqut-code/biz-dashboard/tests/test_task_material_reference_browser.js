// Isolated check that a task note's email reference opens as a historical reference, never fetching mail; no live service or records.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const CONVERSATION='5f3d7a36-2c4b-4a4e-9a51-8a3f2d6e1c01';
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
 try {
  const page=await browser.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',route=>{
   const url=new URL(route.request().url());
   if(url.hostname!=='task.test')return route.abort();
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:`<!doctype html><meta charset="utf-8"><button id="wsbtn" hidden></button><div id="host"></div>${
     ['core','tags','work-groups','relationships','task-progress'].map(name=>`<script src="/js/${name}.js"></script>`).join('')}`});
   const file=path.join(root,url.pathname);return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  });
  await page.goto('http://task.test');
  await page.evaluate(()=>{
   state.scope='business';state.biz='other';window.calls=[];window.tabs=[];window.toasts=[];
   selectBiz=async biz=>{state.biz=biz;state.scope='business';};
   goTab=async name=>{state.tab=name;tabs.push(name);};
   mkShow=()=>{};notify=message=>toasts.push(message);
   const reply=data=>new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
   window.fetch=async(path,options)=>{
    const args=JSON.parse(options?.body || '{}');calls.push({path,args});
    if(path.endsWith('/stories_resource_get'))return reply({status:'ok',data:{resource:{id:args.resource_id}}});
    if(path.endsWith('/email_conversation_get'))return reply({status:'ok',data:{messages:[{id:'message-1'}],next_cursor:null}});
    return reply({status:'error',error:{message:'unexpected '+path}});
   };
  });
  const reference={type:'material',id:CONVERSATION,label:'Re: Pilot',resource_type:'email_conversations'};
  // A cancelled beforeOpen leaves the current view and reads nothing.
  await page.evaluate(ref=>taskProgressOpenReference('acme',ref,()=>false),reference);
  assert.deepEqual(await page.evaluate(()=>({calls,biz:state.biz,modal:!!document.querySelector('.historical-email')})),{calls:[],biz:'other',modal:false});
  // An unavailable reference does nothing, not even beforeOpen.
  assert.equal(await page.evaluate(async()=>{let asked=false;await taskProgressOpenReference('acme',{unavailable:true},()=>{asked=true;});return asked||calls.length;}),0);
  // The email reference shows an explicit historical note: no mail request, no business switch, no tab change.
  await page.evaluate(ref=>taskProgressOpenReference('acme',ref,()=>true),reference);
  const opened=await page.evaluate(()=>({calls,tabs,biz:state.biz,toasts,note:document.querySelector('.historical-email')?.textContent}));
  // No retired Story resource lookup: the conversation id is already the item identity.
  assert.deepEqual(opened.calls.map(call=>call.path).filter(path=>/stories_/.test(path)),[]);
  assert.deepEqual(opened.calls,[]);
  assert.deepEqual({tabs:opened.tabs,biz:opened.biz,toasts:opened.toasts},{tabs:[],biz:'other',toasts:[]});
  assert.match(opened.note,/historical email reference/);
  assert.deepEqual(errors,[]);
  console.log('Task material reference: email reference is historical and fetches nothing; unavailable and cancelled opens do nothing.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
