// Real app CSS and record workspace on disposable API fixtures. No live data writes.
const assert=require('node:assert/strict');
const {startCrmServer}=require('./support/crm_server');
const {launch}=require('./support/crm_page');
(async()=>{
 const fixture=startCrmServer();
 const tasks=[
  fixture.api.addTask({id:'plain',title:'Product Hunt launch',status:'done'}),
  fixture.api.addTask({id:'story',title:'Launch preparation',task_type:'story'}),
  fixture.api.addTask({id:'person',title:'Maya Chen',task_type:'customer',customer_kind:'person',customer_state:'Interested'}),
  fixture.api.addTask({id:'company',title:'Long company name that must fit the same narrow rows as Focus',task_type:'customer',customer_kind:'company'}),
 ];
 // A focused Task that is deliberately absent from the paginated membership page.
 fixture.api.addTask({id:'offpage',title:'Off the board',status:'working'});
 const messages=Array.from({length:30},(_,i)=>({id:'msg-'+i,sequence:i+1,body:('Message '+i+' with enough text to exercise the existing thread scroll area. ').repeat(12),created_by:'chief',created_by_name:'Ordubashy',author_kind:'agent',is_owner:false,created_at:'2026-10-29T12:00:00Z'}));
 fixture.privateThreads.set('task:global:plain',{messages,read:0});
 const {browser,page,errors}=await launch(fixture);
 const requests=[];
 try {
  await page.route('**/api/root/operations/attention_get',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({status:'ok',data:{attention:{state:'focused',focus:{type:'task',id:'plain',business_id:null,label:'Product Hunt launch'},intent:'Prepare launch materials'},items:tasks.map(t=>({membership_id:'m-'+t.id,membership_revision:1,task_ref:{type:'task',id:t.id,business_id:null},task:t})),next_cursor:null}})}));
  await page.route('**/api/root/operations/attention_remove',route=>{requests.push(route.request().postDataJSON());return route.fulfill({contentType:'application/json',body:JSON.stringify({status:'ok',data:{}})});});
  await page.waitForFunction(()=>typeof _agpad!=='undefined' && _agpad?.views.includes('attentionBoard'));
  await page.evaluate(()=>agpadSetView(_agpad,'attentionBoard'));
  const row=id=>page.locator(`[data-attention-member="m-${id}"]`);
  await row('plain').waitFor();
  const focused=page.locator('[data-attention-focus]');
  // One continuous list in the established state order; the old state headings are gone.
  assert.equal(await page.locator('[data-attention-list] [data-attention-status]').count(),0,'no state sections');
  assert.equal(await page.locator('[data-attention-list] h2').count(),0,'no state headings');
  assert.equal(await page.locator('[data-attention-list] > .agfocus-items').count(),1,'one membership list');
  assert.deepEqual(await page.locator('[data-attention-member] .ttl').allTextContents(),
    ['Launch preparation','Maya Chen','Long company name that must fit the same narrow rows as Focus','Product Hunt launch'],'status order is retained inside the flat list');
  // The focused ordinary Task uses the shared state ball, not a generic checkbox-like icon.
  assert.equal(await focused.locator('.agfocus-task-state.done').count(),1);
  assert.equal(await focused.locator('.task-type-marker').count(),0);
  assert.equal(await focused.locator('.agfocus-task-state').getAttribute('aria-label'),'Done');
  // The focused block separates with a subtle bottom divider, never the old left border.
  const focusStyle=await page.evaluate(()=>{
    const host=document.querySelector('[data-attention-focus]'),section=host.querySelector('section'),style=getComputedStyle(section);
    const probe=document.createElement('span');probe.style.borderBottom='1px solid var(--line-2)';document.body.append(probe);
    const line=getComputedStyle(probe).borderBottomColor;probe.remove();
    return {framing:host.classList.contains('agfocus-framing'),left:style.borderLeftWidth,bottom:style.borderBottomWidth,style:style.borderBottomStyle,color:style.borderBottomColor,line,padding:style.paddingBottom};
  });
  assert.equal(focusStyle.framing,false,'the Focus framing class and its left border are gone');
  assert.equal(focusStyle.left,'0px');
  assert.ok(parseFloat(focusStyle.bottom)>0&&parseFloat(focusStyle.bottom)<=1.5,'a subtle bottom divider');
  assert.equal(focusStyle.style,'solid');
  assert.equal(focusStyle.color,focusStyle.line);
  assert.ok(parseFloat(focusStyle.padding)>0,'the divider keeps modest spacing');
  // A focused Story or customer keeps its special type marker instead of the ordinary state ball.
  await page.evaluate(()=>{_agpad.attentionBoard.data.attention={state:'focused',focus:{type:'task',id:'story',business_id:null,label:'Launch preparation'},intent:'Story intent'};attentionBoardPaint(_agpad);});
  assert.equal(await focused.locator('.task-type-icon-story').count(),1);
  assert.equal(await focused.locator('.agfocus-task-state').count(),0);
  await page.evaluate(()=>{_agpad.attentionBoard.data.attention={state:'focused',focus:{type:'task',id:'person',business_id:null,label:'Maya Chen'},intent:'Customer intent'};attentionBoardPaint(_agpad);});
  assert.equal(await focused.locator('.task-type-icon-customer').count(),1);
  assert.equal(await focused.locator('.agfocus-flag').textContent(),'Interested');
  assert.equal(await focused.locator('.agfocus-task-state').count(),0);
  await page.evaluate(()=>{_agpad.attentionBoard.data.attention={state:'focused',focus:{type:'task',id:'plain',business_id:null,label:'Product Hunt launch'},intent:'Prepare launch materials'};attentionBoardPaint(_agpad);});
  // A focused Task outside the membership page is read once through the existing scoped read, never per paint.
  await page.evaluate(()=>{_agpad.attentionBoard.data.attention={state:'focused',focus:{type:'task',id:'offpage',business_id:null,label:'Off the board'},intent:'Resolve exact task'};attentionBoardPaint(_agpad);});
  await page.waitForFunction(()=>document.querySelector('[data-attention-focus] .agfocus-task-state.working'));
  const offpageReads=()=>fixture.api.log.filter(entry=>entry.op==='tasks_get'&&entry.args.task_id==='offpage');
  assert.equal(offpageReads().length,1,'one scoped read per focus identity');
  assert.equal(offpageReads()[0].scope,'global');
  await page.evaluate(()=>attentionBoardPaint(_agpad));
  await page.waitForTimeout(40);
  assert.equal(offpageReads().length,1,'a repaint reuses the resolved task');
  await page.evaluate(()=>{_agpad.attentionBoard.data.attention={state:'focused',focus:{type:'task',id:'offpage',business_id:'missing-project',label:'Missing project'},intent:'Unresolved'};attentionBoardPaint(_agpad);});
  await page.waitForTimeout(40);
  assert.equal(offpageReads().length,1,'an unresolved business reference is never read as Global');
  assert.equal(await focused.locator('.agfocus-task-state').isDisabled(),true);
  assert.equal(await focused.locator('.agfocus-task-state').getAttribute('aria-label'),'Task project unavailable');
  assert.equal(await focused.locator('.task-type-marker').count(),0);
  await page.evaluate(()=>{_agpad.attentionBoard.data.attention={state:'focused',focus:{type:'task',id:'plain',business_id:null,label:'Product Hunt launch'},intent:'Prepare launch materials'};attentionBoardPaint(_agpad);});
  await page.waitForFunction(()=>document.querySelector('[data-attention-focus] .agfocus-task-state.done'));
  assert.equal(await row('plain').locator('.ball.done').count(),1);
  assert.equal(await row('plain').locator('.agfocus-scope .agfocus-global-chip').count(),1,'scope checks are retained');
  assert.equal(await row('plain').locator('.agfocus-row').evaluate(el=>getComputedStyle(el).display),'grid');
  assert.equal(await page.locator('[data-attention-list] [data-task-state-menu]').count(),0,'left state control is sufficient');
  assert.equal(await row('plain').locator('[data-task-workers]').count(),0,'no empty assignment plus');
  assert.equal(await row('plain').getByRole('button',{name:'Remove Product Hunt launch from Attention board'}).textContent(),'','remove is the existing X icon');
  assert.equal(await row('plain').locator('button').filter({hasText:'Product Hunt launch'}).count(),0,'record title is the shared flat Focus headline, not a pill button');
  assert.equal(await row('story').locator('.task-type-icon-story').count(),1);
  assert.equal(await row('person').locator('.task-type-icon-customer').count(),1);
  assert.equal(await row('company').locator('.task-type-icon-customer').count(),1);
  assert.equal(await row('person').locator('.agfocus-task-state,[data-task-state-menu]').count(),0,'customer marker is not an ordinary task state');
  assert.equal(await row('person').locator('.agfocus-flag').textContent(),'Interested');
  for(const id of ['plain','story','person','company']) {
   assert.ok((await row(id).boundingBox()).height<45,'compact Focus row');
  }
  // Shared row shell and existing marker renderers cover other focused record types too.
  assert.equal(await page.evaluate(()=>{
   const types=[['project',RW_PROJECT_ICON],['table',RW_TABLE_ICON],['signoff',T_VIEW_ICON.signoffs]];
   return types.every(([type,icon])=>{const node=attentionReferenceRow(_agpad,{type,id:'sample',business_id:null,label:type});const expected=document.createElement('span');expected.innerHTML=icon;return node.querySelector('.task-type-marker').innerHTML===expected.innerHTML;});
  }),true);
  if(process.env.ATTENTION_SCREENSHOT)await page.screenshot({path:process.env.ATTENTION_SCREENSHOT});
  await row('plain').focus();await page.keyboard.press('Enter');
  const scroll=page.locator('[data-attention-record] .gchat-scroll');await scroll.waitFor();
  await page.waitForFunction(()=>{const el=document.querySelector('[data-attention-record] .gchat-scroll');return el?.scrollHeight>el.clientHeight+300 && el.clientHeight>100;});
  assert.equal(await page.locator('[data-attention-list]').isVisible(),false,'record gets the pane instead of growing beneath the list');
  const box=await scroll.boundingBox();assert.ok(box.y+box.height<=1000,'thread fits the viewport');
  await scroll.evaluate(el=>el.scrollTop=0);await scroll.hover();await page.mouse.wheel(0,500);
  await page.waitForFunction(()=>document.querySelector('[data-attention-record] .gchat-scroll').scrollTop>0);
  assert.equal(await page.locator('[data-attention-record] .gchat-composer textarea').isVisible(),true);
  await page.locator('[data-attention-record] .rw-close').click();await row('plain').waitFor();
  await page.setViewportSize({width:390,height:844});
  assert.ok(await row('company').evaluate(el=>el.scrollWidth<=el.clientWidth+1),'no narrow-row overflow');
  await row('plain').locator('.ttl').click();
  await scroll.waitFor();
  await page.waitForFunction(()=>{const el=document.querySelector('[data-attention-record] .gchat-scroll');return el?.clientHeight>100 && el.scrollHeight>el.clientHeight;});
  assert.ok((await scroll.boundingBox()).y+(await scroll.boundingBox()).height<=844);
  await page.locator('[data-attention-record] .rw-close').click();
  await page.getByRole('button',{name:'Remove Product Hunt launch from Attention board',exact:true}).click();
  assert.equal(requests[0].membership_id,'m-plain');assert.equal(requests[0].expected_membership_revision,1);
  assert.deepEqual(errors,[]);
  console.log('Attention: shared Focus rows/markers, compact responsive layout, keyboard navigation, scrollable record chat and revision-bound membership removal passed.');
 } finally {await browser.close();await fixture.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
