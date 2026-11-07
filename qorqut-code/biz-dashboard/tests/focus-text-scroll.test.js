'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Browser checks are optional. */ }
const browserAvailable = !!chromium && require('node:fs').existsSync(chromium.executablePath());
const browserTest = (name, fn) => test(name, {skip: !browserAvailable && 'Local Playwright Chromium is unavailable'}, fn);

async function fixture() {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1000,height:700}});
  await page.setContent('<div class="agpad"><div class="tbody"><section class="agfocus"><header>Focus</header><div class="agfocus-layout"><div class="agfocus-items"></div></div></section></div></div>');
  await page.addStyleTag({path:path.join(__dirname,'../css/agent-focus.css')});
  await page.addStyleTag({content:'.agpad {width:600px} .tbody {height:450px}'});
  await page.evaluate(() => {
    window.coreData=x=>x;
    window.saved={revision:1,card_ids:[],blocks:[{id:'note',text:Array.from({length:90},(_,i)=>`Line ${i}: a note to keep in view.`).join('\n'),before_id:null,after_id:null}]};
    window.api=async()=>structuredClone(saved);
    window.post=async(url,data)=>{saved={...saved,revision:saved.revision+1,blocks:structuredClone(data.blocks)};return structuredClone(saved);};
    window.agfocusPaint=p=>focusText.reconcile(p,p.focusMount,p.focusMount.querySelector('.agfocus-items'));
  });
  await page.addScriptTag({path:path.join(__dirname,'../js/focus-text.js')});
  await page.evaluate(()=>{
    window.panel={agentId:'agent',focusMount:document.querySelector('.agfocus')};
    focusText.mount(panel,panel.focusMount);
  });
  await page.locator('textarea').waitFor();
  await page.evaluate(()=>{
    const ta=document.querySelector('textarea'), board=document.querySelector('.agfocus-layout');
    ta.focus({preventScroll:true});
    const position=ta.value.indexOf('Line 60:')+8;
    ta.setSelectionRange(position,position);
    board.scrollTop=ta.offsetTop-board.offsetTop+60*20-150;
  });
  await page.waitForTimeout(50);
  return {browser,page};
}

const view=page=>page.evaluate(()=>{
  const ta=document.querySelector('textarea'),board=document.querySelector('.agfocus-layout');
  const line=ta.value.slice(0,ta.selectionStart).split('\n').length-1;
  return {scroll:board.scrollTop,caret:ta.getBoundingClientRect().top+6+line*20-ta.scrollTop,
    top:board.getBoundingClientRect().top,bottom:board.getBoundingClientRect().bottom,
    selected:ta.selectionStart,focused:document.activeElement===ta,height:ta.offsetHeight,internalScroll:ta.scrollTop};
});

browserTest('editing a long Focus note preserves the visible line through input and autosave',async()=>{
  const {browser,page}=await fixture();try {
    const before=await view(page);
    assert.ok(before.scroll>500);
    await page.keyboard.type('edited');
    const after=await view(page);
    assert.equal(after.scroll,before.scroll,'typing must not jump to the top of the note');
    assert.equal(after.selected,before.selected+6);
    assert.ok(after.focused);
    assert.ok(after.caret>=after.top && after.caret+20<=after.bottom,'the edited line stays visible');
    await page.waitForFunction(()=>saved.revision>1);
    const saved=await view(page);
    assert.equal(saved.scroll,before.scroll,'autosave repaint must preserve the same viewport');
    assert.equal(saved.selected,after.selected);
    assert.equal(saved.internalScroll,0);
  } finally {await browser.close();}
});

browserTest('Focus notes still grow and shrink while keeping the caret visible',async()=>{
  const {browser,page}=await fixture();try {
    const before=await view(page);
    await page.keyboard.press('Enter');
    const grown=await view(page);
    assert.equal(grown.height,before.height+20);
    assert.ok(grown.caret>=grown.top && grown.caret+20<=grown.bottom);
    await page.keyboard.press('Backspace');
    const shrunk=await view(page);
    assert.equal(shrunk.height,before.height);
    assert.equal(shrunk.scroll,before.scroll);
    assert.ok(shrunk.caret>=shrunk.top && shrunk.caret+20<=shrunk.bottom);
  } finally {await browser.close();}
});

browserTest('adding blank lines at the end keeps the caret visible without scrolling inside the note',async()=>{
  const {browser,page}=await fixture();try {
    await page.keyboard.press('Control+End');
    const before=await view(page);
    await page.keyboard.press('Enter');
    await page.keyboard.press('Enter');
    const after=await view(page);
    assert.equal(after.height,before.height+40);
    assert.equal(after.internalScroll,0);
    // Chromium reveals the caret glyph, not the line box's extra leading.
    assert.ok(after.caret>=after.top && after.caret+16<=after.bottom,JSON.stringify({before,after}));
  } finally {await browser.close();}
});
