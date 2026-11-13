'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const path=require('node:path');
const fs=require('node:fs');
let chromium;
try { ({chromium}=require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable=process.env.CHROME_PATH || chromium?.executablePath();
const available=!!chromium && fs.existsSync(executable);
const browserTest=(name,run)=>test(name,{skip:!available && 'Local Playwright Chromium is unavailable'},run);
const fixture=pathToFileURL(path.join(__dirname,'fixtures/signoff-focus-layout.html')).href;

browserTest('Focus signoff keeps its decision footer visible and preserves guarded decisions',async()=>{
  const browser=await chromium.launch({headless:true,executablePath:executable,args:['--allow-file-access-from-files']});
  try {
    const page=await browser.newPage({viewport:{width:800,height:700}}), errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(fixture);
    const ready=async id=>{await page.evaluate(id=>openItem(id),id);await page.waitForSelector('.sg-detail-footer [data-sg="approve"]:not([disabled])');};
    for(const id of ['long','action','small']) {
      await ready(id);
      const initial=await page.evaluate(()=>{
        const body=document.querySelector('.sg-detail-body'),footer=document.querySelector('.sg-detail-footer'),board=document.querySelector('.agfocus-surface-body');
        const field=document.querySelector('.sg-decision-feedback'), approve=footer.querySelector('[data-sg="approve"]'),reject=footer.querySelector('[data-sg="reject"]');
        const r=e=>e.getBoundingClientRect(), color=e=>getComputedStyle(e).backgroundColor;
        const result={long:body.scrollHeight>body.clientHeight,outer:board.scrollHeight>board.clientHeight,right:Math.abs(r(body).right-r(board).right)<2,bottom:Math.abs(r(footer).bottom-r(board).bottom)<2,left:r(field).right<=r(approve).left,rows:field.rows,wrap:field.wrap,height:r(field).height,effects:document.querySelectorAll('.sg-effects').length,approval:color(approve),quiet:color(reject)};
        body.scrollTop=body.scrollHeight;result.scrolled=body.scrollTop>0;result.bottomAfter=r(footer).bottom;result.bottomBefore=r(board).bottom;
        field.value='Reason\nsecond line';field.dispatchEvent(new Event('input',{bubbles:true}));
        result.rejectColor=color(footer.querySelector('[data-sg="reject"]'));result.approveQuiet=color(footer.querySelector('[data-sg="approve"]'));result.value=field.value;
        field.value='   ';field.dispatchEvent(new Event('input',{bubbles:true}));result.whitespaceColor=color(footer.querySelector('[data-sg="approve"]'));
        field.value='';field.dispatchEvent(new Event('input',{bubbles:true}));result.clearedColor=color(footer.querySelector('[data-sg="approve"]'));
        return result;
      });
      assert.equal(initial.long,id!=='small',id);assert.equal(initial.scrolled,id!=='small',id);assert.equal(initial.outer,false);assert(initial.right&&initial.bottom&&initial.left);assert.equal(initial.bottomAfter,initial.bottomBefore);assert.equal(initial.rows,1);assert.equal(initial.wrap,'off');assert.equal(initial.height,36);assert.equal(initial.effects,0);assert.equal(initial.value,'Reason\nsecond line');assert.notEqual(initial.approval,initial.quiet);assert.equal(initial.rejectColor,initial.approval);assert.equal(initial.approveQuiet,initial.quiet);assert.equal(initial.whitespaceColor,initial.approval);assert.equal(initial.clearedColor,initial.approval);
    }
    await page.setViewportSize({width:360,height:700});await page.evaluate(()=>document.querySelector('.agfocus').style.width='300px');
    assert(await page.evaluate(()=>{const footer=document.querySelector('.sg-detail-footer').getBoundingClientRect();return [...document.querySelectorAll('.sg-detail-footer button,.sg-detail-footer textarea')].every(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.left>=footer.left&&r.right<=footer.right})}));
    await page.setViewportSize({width:800,height:700});await page.evaluate(()=>document.querySelector('.agfocus').style.width='560px');
    await ready('short');await page.focus('[data-sg-edit-field="body"]');await page.fill('[data-sg-edit-field="body"]','Unfinished edit');await page.click('.agfocus-close');
    assert(await page.locator('.sg-focus').count());assert.match(await page.locator('.agfocus-expansion-content > .sg-notice').textContent(),/Save or discard/);
    await page.click('[data-sg="cancel-message"]');await page.click('.agfocus-close');assert.equal(await page.locator('.agfocus-signoff-open').count(),0);
    await page.evaluate(()=>testHarness.openGeneric());
    const generic=await page.evaluate(()=>{const s=document.querySelector('.agfocus-surface'),b=s.querySelector('.agfocus-surface-body'),c=s.querySelector('.agfocus-expansion-content'),t=s.querySelector('.agfocus-surface-title');return {title:t.textContent,visible:getComputedStyle(t).display!=='none',background:getComputedStyle(b).backgroundColor,padding:getComputedStyle(c).paddingLeft,border:getComputedStyle(b).borderTopWidth}});
    assert.equal(generic.title,'Reminder');assert(generic.visible);assert.notEqual(generic.background,'rgba(0, 0, 0, 0)');assert.equal(generic.padding,'24px');assert.equal(generic.border,'1px');
    await page.evaluate(()=>{testHarness.holdRead();openItem('action')});await page.waitForTimeout(100);assert.equal(await page.locator('.sg-detail-footer [data-sg="approve"]:not([disabled])').count(),0);assert.equal(await page.evaluate(()=>testHarness.calls.length),0);await page.evaluate(()=>testHarness.releaseRead());await page.waitForSelector('.sg-detail-footer [data-sg="approve"]:not([disabled])');
    await page.click('.sg-detail-footer [data-sg="reject"]');assert.equal(await page.evaluate(()=>testHarness.calls.length),0);
    await page.fill('.sg-decision-feedback','Please revise');await page.click('.sg-detail-footer [data-sg="reject"]');assert.equal(await page.evaluate(()=>testHarness.calls.length),1);assert(await page.locator('.sg-detail-footer [data-sg="reject"]').isDisabled());await page.locator('.sg-detail-footer [data-sg="reject"]').evaluate(e=>e.click());assert.equal(await page.evaluate(()=>testHarness.calls.length),1);assert.match(await page.evaluate(()=>testHarness.calls[0].url),/\/reject$/);assert.equal(await page.evaluate(()=>testHarness.calls[0].body.feedback),'Please revise');await page.evaluate(()=>testHarness.releaseWrite());await page.waitForTimeout(50);
    await ready('short');await page.click('.sg-detail-footer [data-sg="approve"]');assert.equal(await page.evaluate(()=>testHarness.calls.length),2);assert.match(await page.evaluate(()=>testHarness.calls[1].url),/\/approve$/);assert(await page.locator('.sg-detail-footer [data-sg="approve"]').isDisabled());await page.evaluate(()=>testHarness.releaseWrite());await page.waitForTimeout(50);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});
