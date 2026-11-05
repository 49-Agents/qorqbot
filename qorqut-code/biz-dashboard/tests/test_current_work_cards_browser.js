// Isolated current-work presentation, with read-only fixture data and stub task editor.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined});
 try{
 const page=await browser.newPage({viewport:{width:800,height:800}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.setContent('<button id="wsbtn" hidden></button><button id="outside">Outside</button><aside class="agpad" style="width:420px"><div id="agpad-body"></div></aside>');
 for(const file of ['app.css','css/agent-fleet.css','css/agent-focus.css'])await page.addStyleTag({content:fs.readFileSync(path.join(root,file),'utf8')});
 for(const file of ['js/core.js','js/san-juan-time.js','js/agents.js'])await page.addScriptTag({path:path.join(root,file)});
 await page.evaluate(()=>{
 window.agpadInboxAge=()=> 'now';window.agpadActive=()=>true;
 state.bizs=[{id:'biz',slug:'studio',name:'Studio'}];
 window.tFetchTask=async()=>({id:'task',title:'Task editor'});
 window.tDetail=(task,options)=>{if(!options?.host)throw Error('Modal not allowed');options.host.textContent=task.title;};
 window.globalTasksMount=(host)=>{host.textContent='Global editor';};
 window.p={el:document.querySelector('.agpad'),view:'status',activity:{agent_id:'agent',name:'Marketing',display_name:'Marketing',role_id:'marketing',department:'marketing',state:'running',sessions:[{session_id:'one',items:[{task_id:'task',business_id:'biz',business_name:'Studio',summary:'Review campaign',blocked:true,blockers:[{title:'Owner input'}]},{summary:'Coordinate global work',business_id:null}]}]}};
 });
 await page.addScriptTag({path:path.join(root,'js/agent-fleet.js')});
 await page.evaluate(()=>agentCurrentWorkMount(p));
 const card=page.locator('.fleet-work').first();assert.equal(await card.getAttribute('aria-expanded'),'false');
 assert.equal(await card.locator('.fleet-work-details').isVisible(),false);assert.equal(await card.locator('[title]').count(),0);
 assert.equal(await card.locator('h3').evaluate(el=>getComputedStyle(el).fontWeight),'200');
 await card.locator('h3').click();assert.equal(await card.locator('.fleet-work-details').isVisible(),true);
 await card.locator('h3').click();assert.equal(await card.getAttribute('aria-expanded'),'true');
 await page.evaluate(()=>{p.activity.sessions[0].items[0].summary='Updated campaign';agentCurrentWorkPaint(p);});
 assert.equal(await card.getAttribute('aria-expanded'),'true');
 await page.locator('#outside').click();assert.equal(await card.getAttribute('aria-expanded'),'false');
 await card.getByRole('button',{name:'Manage work card'}).click();await page.getByRole('option',{name:'Expand details',exact:true}).click();
 await page.getByText('Task editor',{exact:true}).waitFor();assert.equal(await page.getByRole('dialog').count(),0);
 await page.getByRole('button',{name:'← Agent status',exact:true}).click();assert.equal(await card.isVisible(),true);
 await page.setViewportSize({width:390,height:844});await page.locator('.agpad').evaluate(el=>el.style.width='100%');
 await page.screenshot({path:'/tmp/current-work-cards.png'});assert.deepEqual(errors,[]);
 console.log('Current-work cards: collapse, outside dismissal, refresh persistence, no tooltips, inline details and typography passed.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
