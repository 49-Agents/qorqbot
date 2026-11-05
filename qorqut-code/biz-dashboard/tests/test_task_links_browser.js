// Cross-scope chips, selector, reverse-edge removal and the actual icon assets.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://fixture/**',route=>{
   const name=new URL(route.request().url()).pathname;
   if(name.startsWith('/icons/task-types/'))return route.fulfill({contentType:'image/svg+xml',body:fs.readFileSync(path.join(root,name))});
   return route.fulfill({contentType:'text/html',body:'<div id="links"></div>'});
  });await page.goto('http://fixture/');
  await page.addStyleTag({path:path.join(root,'css/tasks.css')});
  const core=fs.readFileSync(path.join(root,'js/core.js'),'utf8'),tasks=fs.readFileSync(path.join(root,'js/tasks.js'),'utf8');
  await page.addScriptTag({content:`let _modal=null,_menu=null;const SVG_X='x';${core.slice(core.indexOf('function modal('),core.indexOf('// ---------------------------------------------------------------- dropdown'))}
   const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
   const tChipLabel=s=>s,uiFailure=e=>e.message,toast=e=>{throw Error(e)},newRequestId=()=>String(Math.random()),coreData=r=>r.data,sjDateTime=s=>s,T_LABEL={open:'Open'};
   const state={bizs:[{id:'a',slug:'first',name:'First'},{id:'b',slug:'second',name:'Second'}]};
   let writes=[],opened=[],related=[];
   const source={id:'same-id',business_id:'a',revision:1,related_tasks:[]};
   const post=async(url,args)=>{
    if(url.endsWith('tasks_list'))return {data:{tasks:[{id:'same-id',business_id:url.includes('/second/')?'b':null,title:'Prospective client',task_type:'story'}]}};
    if(url.endsWith('tasks_update')){writes.push({url,args});source.revision++;related=args.changes.related_task.linked?[{task_id:'same-id',business_id:'b',business_slug:'second',business_name:'Second',title:'Prospective client',task_type:'story',link_source:{task_id:'same-id',business_id:'a',revision:source.revision}}]:[];return {data:{task:source}};}
    return {data:{task:{...source,related_tasks:related}}};
   };
   ${tasks.slice(tasks.indexOf('const TASK_TYPE_LABELS'),tasks.indexOf('function taskTypeFields'))}`});
  await page.addScriptTag({path:path.join(root,'js/work-groups.js')});
  await page.evaluate(()=>taskLinksMount(document.querySelector('#links'),'first',source,{open:link=>opened.push(link)}));
  await page.getByRole('button',{name:'Link task',exact:true}).click();
  await page.getByLabel('Task workspace').selectOption('second');
  await page.locator('.task-link-option').filter({hasText:'Prospective client'}).click();
  await page.waitForFunction(()=>!document.querySelector('.modal'));
  await page.getByRole('button',{name:'Story Prospective client',exact:true}).click();
  assert.equal(await page.evaluate(()=>opened[0].business_id),'b');
  assert.deepEqual(await page.evaluate(()=>writes[0].args.changes.related_task),{task_id:'same-id',business_id:'b',linked:true});
  const icon=page.locator('#links .task-type-icon-story');
  assert.equal(await icon.evaluate(el=>Math.round(el.getBoundingClientRect().height)),18);
  assert.match(await icon.evaluate(el=>getComputedStyle(el).maskImage),/story.svg/);
  assert.equal(await page.evaluate(async()=>{const r=await fetch('/icons/task-types/story.svg');return r.status;}),200);
  // From the other end, removal targets the original source and its exact revision.
  await page.evaluate(()=>{const other={id:'other',business_id:'b',revision:4,related_tasks:[{task_id:'same-id',business_id:'a',title:'Linked story',task_type:'email',link_source:{task_id:'same-id',business_id:'a',revision:9}}]};return taskLinksMount(document.querySelector('#links'),'second',other);});
  await page.getByRole('button',{name:'Unlink Linked story',exact:true}).click();
  await page.waitForFunction(()=>writes.length===2);
  const removed=await page.evaluate(()=>writes[1]);
  assert.match(removed.url,/\/first\/operations\/tasks_update$/);
  assert.equal(removed.args.expected_revision,9);
  assert.deepEqual(removed.args.changes.related_task,{task_id:'other',business_id:'b',linked:false});
  assert.deepEqual(errors,[]);
  console.log('Task link picker, scoped chips, reverse unlink and icon checks passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
