// UI preview qualification only. Synthetic adapter; no production API or provider writes.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || '/usr/bin/google-chrome',args:['--no-sandbox']});
  try{
    const page=await browser.newPage({viewport:{width:1400,height:1000}});const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(process.env.REFERENCE_PREVIEW_URL || 'http://127.0.0.1:34351');
    const chat=page.locator('#reference-preview-chat');
    const signoff=chat.locator('[data-review-id="preview-signoff"]');
    const delta=chat.locator('[data-review-id="preview-delta"]');
    assert.equal(await signoff.getByRole('textbox',{name:'Message',exact:true}).isVisible(),true,'content is editable directly in chat without expanding');
    await signoff.getByRole('textbox',{name:'Message',exact:true}).click();
    await signoff.getByRole('textbox',{name:'Message',exact:true}).fill('Edited message for Maya.');
    assert.equal(await signoff.getByRole('button',{name:'Approve',exact:true}).count(),0);
    assert.deepEqual(await signoff.locator('.crr-decisions button').allTextContents(),['Save','Discard']);
    assert.equal(await page.locator('[title="Click to edit"],.crr-edit-actions').count(),0);
    assert.equal(await signoff.locator('.crr-decisions').evaluate(e=>getComputedStyle(e).justifyContent),'flex-end');
    assert.equal(await page.getByText('Save or discard before deciding',{exact:true}).count(),0);
    for(const widths of await page.locator('.crr-editable').evaluateAll(fields=>fields.map(field=>{const s=getComputedStyle(field);return [s.borderTopWidth,s.borderRightWidth,s.borderBottomWidth,s.borderLeftWidth];}))){assert.deepEqual(widths,['0px','0px','0px','2px']);}
    await signoff.getByRole('button',{name:'Discard',exact:true}).click();
    assert.match(await signoff.getByRole('textbox',{name:'Message',exact:true}).inputValue(),/Hi Maya/);
    assert.deepEqual(await signoff.locator('.crr-decisions button').allTextContents(),['Approve','Reject']);
    await page.mouse.move(0,0);await page.waitForTimeout(200);
    const fills=await signoff.locator('.crr-decisions button').evaluateAll(nodes=>nodes.map(n=>getComputedStyle(n).backgroundColor));assert.equal(fills[0],fills[1]);
    assert.equal(await signoff.locator('.crr-icon').evaluate(e=>{const expected=document.createElement('div');expected.innerHTML=aicon('mail','currentColor',17);return e.innerHTML===expected.innerHTML;}),true);
    assert.equal(await chat.locator('[data-review-id="preview-message"] .crr-icon').evaluate(e=>{const expected=document.createElement('div');expected.innerHTML=RW_CHAT_ICON;return e.innerHTML===expected.innerHTML;}),true);
    await signoff.getByRole('button',{name:'Review the launch email',exact:true}).click();
    await signoff.getByRole('textbox',{name:'Message',exact:true}).click();
    await signoff.getByRole('textbox',{name:'Message',exact:true}).fill('Hi Maya,\n\nWould you be open to a quick conversation about Qorqut next week?\n\nAlp');
    await signoff.getByRole('button',{name:'Save',exact:true}).click();
    const side=page.locator('#reference-preview-detail');
    assert.match(await side.getByRole('textbox',{name:'Message',exact:true}).inputValue(),/Would you be open/);
    assert.equal(await page.evaluate(()=>referenceReviewPreview.store.get('preview-signoff').state),'pending','save is separate from approval');
    assert.equal(await page.locator('.crr-comment-form,.crr-comments-button,.crr-comments').count(),0,'review references have no comment UI');
    assert.equal(await signoff.locator('.crr-history').count(),0);
    assert.equal(await delta.getByRole('textbox',{name:'Scope',exact:true}).isVisible(),true,'proposal content is visible without opening details');
    await delta.getByRole('textbox',{name:'Scope',exact:true}).click();
    await delta.getByRole('textbox',{name:'Scope',exact:true}).fill('Research reports only; brainstorming remains unchanged.');
    await delta.getByRole('button',{name:'Save',exact:true}).click();
    assert.equal(await page.evaluate(()=>referenceReviewPreview.store.get('preview-delta').state),'pending');
    if(process.env.REFERENCE_PREVIEW_SHOTS)await page.screenshot({path:process.env.REFERENCE_PREVIEW_SHOTS+'/expanded.png',fullPage:true});
    // Both panes see the same record. A stale edit must be kept, never silently overwrite newer text.
    await signoff.getByRole('textbox',{name:'Message',exact:true}).click();
    await signoff.getByRole('textbox',{name:'Message',exact:true}).fill('Stale local draft');
    await side.getByRole('textbox',{name:'Message',exact:true}).click();
    await side.getByRole('textbox',{name:'Message',exact:true}).fill('Newer side-pane edit');
    await side.getByRole('button',{name:'Save',exact:true}).click();
    await signoff.getByRole('button',{name:'Save',exact:true}).click();
    await signoff.getByText('This record changed. Your draft is kept; discard it to load the current version.',{exact:true}).waitFor();
    assert.equal(await signoff.getByRole('textbox',{name:'Message',exact:true}).inputValue(),'Stale local draft');
    await signoff.getByRole('button',{name:'Discard',exact:true}).click();
    assert.equal(await signoff.getByRole('textbox',{name:'Message',exact:true}).inputValue(),'Newer side-pane edit');
    await delta.getByRole('button',{name:'Accept change',exact:true}).click();
    assert.equal(await page.evaluate(()=>referenceReviewPreview.store.get('preview-delta').state),'accepted');
    await page.setViewportSize({width:390,height:900});
    const geometry=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));
    assert.ok(geometry.scroll<=geometry.width,JSON.stringify(geometry));
    if(process.env.REFERENCE_PREVIEW_SHOTS)await page.screenshot({path:process.env.REFERENCE_PREVIEW_SHOTS+'/narrow.png',fullPage:true});
    assert.deepEqual(errors,[]);console.log('Preview: expand, edit/discard/save, decision separation, stale edit guard and mobile layout passed.');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
