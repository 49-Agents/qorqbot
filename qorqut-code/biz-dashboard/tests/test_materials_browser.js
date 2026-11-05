#!/usr/bin/env node
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.CONTEXT_TEST_ORIGIN;
assert(origin && new URL(origin).hostname === '127.0.0.1' && new URL(origin).port !== '1049');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args:['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin+'/?token=owner-bootstrap-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx');
    await page.locator('#agentrows').waitFor();
    const material = await page.evaluate(async () => {
      await selectBiz('widget');
      const topic=(await storyCall('widget','context_topics_create',{title:'Material checks',slug:'material-checks'})).document;
      const document=(await storyCall('widget','context_notes_create',{topic_id:topic.id,title:'Exact conversation',body:'Ada: A complete conversation.\nLuka: Yes.\n',material_type:'conversation'})).document;
      await storyCall('widget','materials_update',{material_id:document.id,changes:{tags:['marketing','<customer>']}},{expected_revision:document.revision});
      const story=(await storyCall('widget','stories_create',{title:'Material classification',purpose:'Check classification display'})).story;
      await storyCall('widget','relationships_create',{source:{type:'material',id:document.id},target:{type:'story',id:story.id},role:'concerns'});
      ls('story:widget',story.id);await goTab('stories');
      return document;
    });
    const row=page.locator('#story-context-review-pane .context-relation');
    await row.first().waitFor();
    assert((await row.innerText()).includes('Conversation'));
    assert.equal(await row.locator('.context-material-label').innerText(),'authored · marketing · <customer>');
    assert.equal(await row.locator('customer').count(),0);
    for (const [name,width] of [['desktop',1440],['mobile',390]]) {
      await page.setViewportSize({width,height:1000});
      await page.screenshot({path:`/tmp/qorqut-materials-${name}.png`,fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    }
    await page.setViewportSize({width:1440,height:1000});
    await page.evaluate(async material=>{state.ctx=material.path.replace(/^context\//,'');await goTab('context');},material);
    await page.locator('#jta').waitFor();
    assert.equal(await page.locator('#jta').inputValue(),'Ada: A complete conversation.\nLuka: Yes.\n');
    const chips=await page.locator('#ctxview .chips').innerText();
    for(const value of ['conversation','authored','marketing','<customer>'])assert(chips.includes(value),chips);
    assert.deepEqual(errors,[]);
    console.log('Material labels, escaped tags, exact source and responsive views passed.');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
