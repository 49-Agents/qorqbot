// Existing Notes editor in the agent pane; disposable API, no production data.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {startCrmServer}=require('./support/crm_server');
(async()=>{
 const fixture=startCrmServer();
 fixture.api.addTable('global',{id:'journal-linked-table',title:'Linked CRM',table_kind:'crm',columns:[]});
 await fixture.listen();
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || '/usr/bin/google-chrome',args:['--no-sandbox']});
 try {
  const page=await browser.newPage({viewport:{width:1400,height:1000}});page.setDefaultTimeout(12000);
  const errors=[],notes=[],writes=[];let failSave=false;
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/root/notes**',async route=>{
   const req=route.request(),url=new URL(req.url()),id=url.pathname.split('/')[4];
   let data=notes;
   if(req.method()==='POST') {
    if(failSave)return route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({error:'Fixture save failed'})});
    const body=req.postDataJSON();writes.push(body);
    if(id){data=notes.find(n=>n.id===id);Object.assign(data,body,{revision:data.revision+1,content_hash:'hash-'+writes.length});}
    else {data={id:'note-'+(notes.length+1),title:'Daily',content:'',statuses:{},created_at:new Date().toISOString(),updated_at:new Date().toISOString(),revision:1,content_hash:'initial',...body};notes.push(data);}
   }
   return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.goto(fixture.origin+'/#/global/agents');
  await page.waitForFunction(()=>!!document.querySelector('.global-agents')?.openAgent);
  await page.evaluate(()=>selectGlobal('journal'));
  const journal=page.frameLocator('.agent-workspace-frame');
  await journal.locator('#ntnew').click();
  await journal.locator('#nttitle').fill('Preserved Journal');
  await journal.locator('#ntdoc .ntline').first().fill('My existing editor, moved into the agent pane.');
  const toggle=journal.locator('#ntnavtoggle');
  await toggle.click();
  assert.equal(await toggle.getAttribute('aria-expanded'),'true');
  assert.equal(await journal.locator('#ntsearch').isVisible(),true);
  await journal.locator('#ntsearch').press('Escape');
  assert.equal(await toggle.getAttribute('aria-expanded'),'false');
  assert.equal(await journal.locator('#ntside').evaluate(el=>el.hidden && el.inert),true);
  assert.equal(await journal.locator('#ntdoc .ntline').first().textContent(),'My existing editor, moved into the agent pane.');
  assert.equal(await journal.locator('#nttitle').evaluate(el=>getComputedStyle(el).fontSize),'20px');
  assert.equal(await page.locator('#taskswrap,#taskspanel').count(),0);
  assert.equal(await page.evaluate(()=>agentPaneViewAllowed('learning-applier','journal')),false);
  await page.evaluate(()=>agpadSetView(_agpad,'focus'));
  await page.waitForFunction(()=>!document.querySelector('.agent-workspace-frame'));
  assert.equal(notes[0].title,'Preserved Journal');
  assert.equal(notes[0].content,'My existing editor, moved into the agent pane.');
  assert.ok(writes.at(-1).expected_revision,'existing revision contract retained');
  await page.evaluate(()=>agpadSetView(_agpad,'journal'));
  await journal.locator('#nttitle').waitFor();
  assert.equal(await journal.locator('#nttitle').inputValue(),'Preserved Journal');
  assert.equal(await toggle.getAttribute('aria-expanded'),'false');
  if(process.env.JOURNAL_SCREENSHOT)await page.screenshot({path:process.env.JOURNAL_SCREENSHOT});
  failSave=true;
  await journal.locator('#ntdoc .ntline').first().click();
  await journal.locator('#ntdoc .ntline').first().fill('Unsaved draft');
  await page.evaluate(()=>agpadSetView(_agpad,'focus'));
  await journal.locator('#nterror').waitFor();
  assert.equal(await journal.locator('#ntdoc .ntline').first().textContent(),'Unsaved draft');
  failSave=false;
  await page.evaluate(()=>agpadSetView(_agpad,'focus'));
  await page.waitForFunction(()=>!document.querySelector('.agent-workspace-frame'));
  assert.equal(notes[0].content,'Unsaved draft');
  await page.keyboard.press('Tab');await page.keyboard.press('Tab');
  assert.equal(await page.locator('#taskswrap,#taskspanel').count(),0);
  await page.goto(fixture.origin+'/#/global/notes');
  await journal.locator('#nttitle').waitFor();
  assert.equal(await journal.locator('#nttitle').inputValue(),'Preserved Journal');
  await page.setViewportSize({width:390,height:844});
  await toggle.click();
  assert.equal(await journal.locator('#ntsearch').isVisible(),true);
  await toggle.click();
  assert.equal(await journal.locator('body').evaluate(e=>e.scrollWidth<=innerWidth+1),true);
  await page.setViewportSize({width:1400,height:1000});
  await page.evaluate(()=>tablesOpen(null,'journal-linked-table'));
  await page.locator('.agfocus-dock .rw-title').filter({hasText:'Linked CRM'}).waitFor();
  assert.equal(await page.locator('#taskswrap,#taskspanel').count(),0);
  await page.goto(fixture.origin+'/?drawer=notes');
  await journal.locator('#nttitle').waitFor();
  assert.equal(new URL(page.url()).searchParams.has('drawer'),false);
  await page.goto(fixture.origin+'/#/global/calendar');
  await page.waitForFunction(()=>location.hash==='#/global/agents' && !!document.querySelector('.global-agents'));
  assert.equal(await page.locator('#taskswrap,#taskspanel,.ov-drawer').count(),0);
  assert.deepEqual(errors,[]);
  console.log('Journal: existing editor, save-on-leave, failed-save draft protection, reopen, legacy route, narrow layout and drawer removal passed.');
 } finally {await browser.close();await fixture.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
