// Focus workspace mode boundary: ordinary Stories retain their established right-pane
// conversation before and after a paired CRM session. All mounts and network routing are real
// dashboard code backed by the reusable synthetic local Operations fixture.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {createCrmPreview}=require('./support/crm_preview_fixture');
const SHOTS=process.env.CRM_PREVIEW_SHOTS || '';

(async()=>{
  if(SHOTS)fs.mkdirSync(SHOTS,{recursive:true});
  const fixture=createCrmPreview(),origin=await fixture.server.listen();
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,args:['--no-sandbox']});
  try {
    const page=await browser.newPage({viewport:{width:1400,height:900}}),errors=[];
    page.setDefaultTimeout(12000);page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
    await page.goto(origin+'/#/global/agents');
    const story=page.locator('.agfocus-items [data-card-id="card-g-'+fixture.ids.story+'"]');
    await story.locator('.ttl').waitFor();
    await page.locator('.gchat-msg .gchat-table-reference').first().waitFor();
    const referenceList=page.locator('.gchat-msg .gchat-attachment').first();
    await referenceList.waitFor();
    const referenceRows=referenceList.locator(':scope > .gchat-reference-block');
    assert.equal(await referenceRows.count(),4,'consecutive Table and customer references share one attachment rule');
    assert.equal(await referenceList.getAttribute('aria-label'),'Table and record references');
    const desktopGeometry=await page.evaluate(()=>{
      const list=document.querySelector('.gchat-msg .gchat-attachment'), rows=[...list.querySelectorAll(':scope > .gchat-reference-block')];
      const point=el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,width:r.width};};
      return {container:{...point(list),border:getComputedStyle(list).borderLeftWidth,inset:getComputedStyle(list).paddingLeft},rows:rows.map(row=>({
        icon:point(row.querySelector('.gchat-reference-icon')),title:point(row.querySelector('.gchat-reference-title')),
        border:getComputedStyle(row).borderLeftWidth,padding:getComputedStyle(row).padding,background:getComputedStyle(row).backgroundColor,
        titleText:row.querySelector('.gchat-reference-title').textContent,count:row.querySelector('.gchat-reference-count')?.textContent||'',
        detail:row.querySelector('.gchat-reference-detail')?.textContent||'',detailHeight:row.querySelector('.gchat-reference-detail')?.getBoundingClientRect().height||0,
        overflow:row.scrollWidth-row.clientWidth,scrollHeight:row.scrollHeight,clientHeight:row.clientHeight}))};
    });
    assert.deepEqual([desktopGeometry.container.border,desktopGeometry.container.inset],['2px','9px']);
    assert.deepEqual(desktopGeometry.rows.map(row=>row.border),['0px','0px','0px','0px'],'each reference has only the shared ancestor rule');
    assert.deepEqual(desktopGeometry.rows.map(row=>row.padding),['6px 8px','6px 8px','6px 8px','6px 8px']);
    assert.deepEqual(desktopGeometry.rows.map(row=>row.titleText),['Influencer partnerships','Campaign reference links','Maya Chen','Atlas Studio']);
    assert.deepEqual(desktopGeometry.rows.map(row=>row.count),['','','','']);
    assert.deepEqual(desktopGeometry.rows.slice(2).map(row=>row.detail),['','']);
    assert.deepEqual(desktopGeometry.rows.slice(0,2).map(row=>row.detail),['Creators and follow-ups for the…','A regular synthetic table for…']);
    assert.equal(fixture.crm.description,'Creators and follow-ups for the November launch.','display truncation never changes the stored description');
    const equalX=values=>values.every(value=>Math.abs(value-values[0])<=1);
    assert.equal(equalX(desktopGeometry.rows.map(row=>row.icon.left)),true,'all Table/person icons share one left edge');
    assert.equal(equalX(desktopGeometry.rows.map(row=>row.title.left)),true,'all Table/person titles share one left edge');
    assert.ok(desktopGeometry.rows.every(row=>row.overflow<=1&&row.scrollHeight<=row.clientHeight+1),JSON.stringify(desktopGeometry));
    if(SHOTS)await referenceList.screenshot({path:path.join(SHOTS,'reference-attachments-detail.png')});
    if(SHOTS)await page.screenshot({path:path.join(SHOTS,'table-and-record-references.png'),fullPage:true});
    const referenceBefore=await referenceRows.first().boundingBox();
    await referenceRows.first().hover();
    assert.deepEqual(await referenceRows.first().boundingBox(),referenceBefore,'reference hover does not move the row');
    assert.match(await referenceRows.first().evaluate(el=>getComputedStyle(el).boxShadow),/inset/);
    const focused=await referenceRows.first().evaluate(el=>{el.focus({focusVisible:true});return {active:document.activeElement===el,outline:getComputedStyle(el).outlineWidth};});
    assert.equal(focused.active,true,'the flat references remain keyboard reachable');
    assert.equal(focused.outline,'2px','keyboard focus remains visible');
    await page.setViewportSize({width:360,height:900});
    const narrowGeometry=await page.evaluate(()=>{
      const list=document.querySelector('.gchat-msg .gchat-attachment'),rows=[...list.querySelectorAll(':scope > .gchat-reference-block')],box=list.getBoundingClientRect();
      return {right:box.right,width:innerWidth,overflow:list.scrollWidth-list.clientWidth,icons:rows.map(row=>row.querySelector('.gchat-reference-icon').getBoundingClientRect().left),
        titles:rows.map(row=>row.querySelector('.gchat-reference-title').getBoundingClientRect().left),vertical:rows.map(row=>row.scrollHeight-row.clientHeight)};
    });
    assert.ok(narrowGeometry.right<=narrowGeometry.width+1&&narrowGeometry.overflow<=1,JSON.stringify(narrowGeometry));
    assert.equal(equalX(narrowGeometry.icons),true,'narrow icon column remains aligned');
    assert.equal(equalX(narrowGeometry.titles),true,'narrow title column remains aligned');
    assert.ok(narrowGeometry.vertical.every(overflow=>overflow<=1),JSON.stringify(narrowGeometry));
    await page.setViewportSize({width:1400,height:900});
    const openStory=async()=>{
      await story.locator('.ttl').click();
      await page.locator('.agfocus-dock .rw-record:not([hidden]) .rw-conversation .gchat-scroll').waitFor();
      assert.equal(await page.locator('.agfocus-layout.agfocus-paired-workspace').count(),0);
      assert.equal(await page.locator('.rw-tab[data-rw-tab="chat"]').count(),1);
    };
    await openStory();
    await page.locator('.rw-close').click();
    const crmCard=page.locator('.agfocus-items [data-card-id="card-'+fixture.ids.crm+'"]');
    await crmCard.locator('.ttl').click();
    await page.locator('.crm-grid tbody tr').first().waitFor();
    assert.equal(await page.locator('.agfocus-layout.agfocus-paired-workspace').count(),1);
    assert.equal(await page.locator('.agfocus-calendar').count(),1,'calendar remains mounted for restoration');
    assert.equal(await page.locator('.agfocus-calendar').isVisible(),false);
    assert.equal(await page.locator('.attention-queue').isVisible(),false);
    assert.equal(await page.locator('.agpad > .thead').isVisible(),false);
    assert.equal(await page.locator('.crm-team').count(),0);
    assert.equal(fixture.api.log.filter(call=>call.op==='tables_thread_get').length,0,'CRM does not read the removed team discussion');
    assert.equal(await page.locator('.crm-record-meta').count(),0);
    assert.ok((await page.locator('.crm-grid th').allTextContents()).includes('Email'));
    assert.ok((await page.locator('.crm-grid th').allTextContents()).includes('Company'));
    const tableTop=await page.locator('.rw-head:visible').boundingBox();
    const paneTop=(await page.locator('.agpad').boundingBox()).y;
    assert.ok(tableTop.y-paneTop<20,'selected CRM starts near the pane top: '+tableTop.y);
    if(SHOTS)await page.screenshot({path:path.join(SHOTS,'crm-table-simplified.png'),fullPage:true});
    assert.equal(await page.locator('.rw-tab[data-rw-tab="chat"]').count(),0);
    assert.equal(await page.locator('.gchat-composer textarea,.conversation-inline-compose textarea').count(),1);
    await page.locator('.crm-grid tbody tr .crm-open').first().click();
    await page.locator('.rw-record[data-navigation-context="crm-record"] .rw-title').filter({hasText:'Maya Chen'}).waitFor();
    assert.equal(await page.locator('.gchat-paired-record').evaluate(node=>node.dataset.conversation),'task:global:'+fixture.ids.maya);
    await page.locator('.rw-context:visible').waitFor();
    assert.equal(await page.locator('.agfocus-calendar').isVisible(),false,'calendar stays hidden on a CRM record');
    if(SHOTS)await page.screenshot({path:path.join(SHOTS,'known-parent-crm-navigation.png'),fullPage:true});
    await page.getByRole('button',{name:'Back to Influencer partnerships',exact:true}).click();
    await page.locator('.crm-grid').waitFor();
    assert.equal(await page.locator('.gchat-paired-record').evaluate(node=>node.dataset.conversation),'table:global:'+fixture.ids.crm);
    assert.equal(await page.locator('.agfocus-calendar').isVisible(),false,'Back keeps the CRM layout');
    await page.locator('.rw-close').click();
    await page.locator('[data-global-chat="ordu"]').waitFor();
    assert.equal(await page.locator('.agfocus-calendar').isVisible(),true,'close restores calendar');
    assert.equal(await page.locator('.agpad > .thead').isVisible(),true,'close restores pane navigation');
    assert.equal(await page.locator('.attention-queue').isVisible(),true,'close restores Focus controls');
    await openStory();
    assert.deepEqual(errors,[]);
    console.log('Paired preview: ordinary Story behavior is preserved before and after CRM/row navigation.');
  } finally {await browser.close();await fixture.server.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
