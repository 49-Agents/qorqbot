#!/usr/bin/env node
// New regression: IME and local input guards remain correct after toolbar remount.
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined});
  try {
    const page=await browser.newPage();
    page.setDefaultTimeout(5000);
    await page.setContent('<div class="client-view"><div class="client-tile"><div class="client-main"><div class="display"></div></div></div></div><input id="outside">');
    await page.evaluate(()=>{
      const listeners={};window.seen=[];window.sent=[];
      window.angular={module:()=>({run:definition=>{window.run=definition;}})};
      window.rootScope={$on:(name,fn)=>{(listeners[name] ||= []).push(fn);}};
      window.emit=name=>(listeners[name]||[]).forEach(fn=>fn({}));
      window.injector={get:name=>name==='clipboardService'?{setClipboard:value=>{sent.push(value);return Promise.resolve();}}:function(value){Object.assign(this,value);}};
      for(const type of ['keydown','keyup','beforeinput','input','compositionstart','compositionupdate','compositionend','paste']) document.addEventListener(type,e=>seen.push(type),true);
    });
    await page.addScriptTag({path:path.resolve(__dirname,'../tools/guacamole-click-focus/click-clipboard.js')});
    await page.evaluate(()=>run.at(-1)(rootScope,injector));
    await page.evaluate(()=>{emit('$routeChangeStart');emit('$routeChangeSuccess');});
    await page.locator('.qorqut-clipboard-toggle').click();
    const field=page.locator('#qorqut-clipboard-text');
    await field.pressSequentially('local text');
    assert.equal(await field.inputValue(),'local text','native local typing still works');
    await field.evaluate(el=>{
      for(const type of ['compositionstart','compositionupdate','compositionend']) {
        const event=new CompositionEvent(type,{bubbles:true,cancelable:true,data:'字'});
        el.dispatchEvent(event);if(event.defaultPrevented)throw Error(type+' default cancelled');
      }
    });
    assert.deepEqual(await page.evaluate(()=>seen),[],'remounted local input must not reach Guacamole capture handlers');
    await page.locator('[data-qorqut-send]').click();
    assert.deepEqual(await page.evaluate(()=>sent.map(x=>x.data)),['local text'],'explicit send works once');
    await page.locator('#outside').pressSequentially('z');
    assert((await page.evaluate(()=>seen)).includes('keydown'),'input outside toolbar is not suppressed');
    console.log('New clipboard reconnect/input regression passed.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
