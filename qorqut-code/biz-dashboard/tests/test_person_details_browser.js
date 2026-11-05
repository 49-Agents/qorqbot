// Exercise the actual editor and modal with an isolated browser and synthetic data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
(async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage();
    await page.goto('about:blank');
    await page.addScriptTag({content:`const state={biz:'studio'}; const SVG_X='×'; let _menu=null; let _modal=null; const esc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'); let seq=0; const newRequestId=()=>String(++seq); const $=s=>document.querySelector(s); window.calls=[]; window.fail=false;
      async function storyCall(biz,op,args,options){
        if(op==='context_items_get')return {item:{id:'person-1',label:'A <Person>',affiliation:'external',revision:4,contact:{email:'a@example.org',phone:'+123',notes:'Email first'},summary:'Customer'}};
        calls.push({biz,op,args,options}); if(window.fail)throw Error('Newer edit exists'); return {};
      }`});
    const core=fs.readFileSync(path.join(__dirname,'../js/core.js'),'utf8')+'\n'+fs.readFileSync(path.join(__dirname,'../js/san-juan-time.js'),'utf8');
    const start=core.indexOf('function modal('); const end=core.indexOf('\n}',start)+2;
    await page.addScriptTag({content:core.slice(start,end)});
    await page.addScriptTag({content:fs.readFileSync(path.join(__dirname,'../js/context.js'),'utf8')});
    await page.evaluate(()=>editPersonDetails('studio','person-1'));
    assert.equal(await page.locator('.mt').textContent(),'A <Person>');
    await page.locator('[name="affiliation"]').selectOption('internal');
    await page.locator('[name="email"]').fill('new@example.org');
    await page.getByRole('button',{name:'Save',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('.modal'));
    const call=await page.evaluate(()=>calls[0]);
    assert.deepEqual(call.args,{document_id:'person-1',changes:{affiliation:'internal',contact:{email:'new@example.org'}}});
    assert.equal(call.options.expected_revision,4);
    await page.evaluate(()=>{window.fail=true;return editPersonDetails('studio','person-1');});
    await page.locator('[name="summary"]').fill('Updated summary');
    await page.getByRole('button',{name:'Save',exact:true}).click();
    await page.getByRole('alert').filter({hasText:'Newer edit exists'}).waitFor();
    assert.equal(await page.locator('[name="summary"]').inputValue(),'Updated summary');
    assert.equal(await page.getByRole('button',{name:'Save',exact:true}).isEnabled(),true);
    await page.getByRole('button',{name:'Save',exact:true}).click();
    assert.equal(await page.evaluate(()=>calls[1].options.idempotency_key===calls[2].options.idempotency_key),true);
    console.log('Person editor: escaped labels, partial updates, revision binding and retained drafts passed');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
