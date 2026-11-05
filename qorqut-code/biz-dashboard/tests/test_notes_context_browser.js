#!/usr/bin/env node
// Real temporary shared runtime, SQLite, jobs and file receipts. No production services.
// PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_shared_runtime_browser.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const CORE = process.env.BAZARLYK_CORE_ROOT || path.resolve(__dirname, '../../biz-core');
const DASHBOARD = path.resolve(__dirname, '..');
const OUT = process.env.SHARED_SCREENSHOTS || path.join(__dirname, 'artifacts/shared-runtime');

async function startFixture() {
  const child = spawn(process.env.BAZARLYK_TEST_PYTHON || path.join(CORE, '.venv/bin/python'), ['tests/service_browser_fixture.py', '--dashboard-root', DASHBOARD], {cwd:CORE, env:{...process.env,PYTHONPATH:'.'}, stdio:['ignore','pipe','pipe']});
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  const details = await new Promise((resolve,reject) => {
    let stdout = '';
    const timeout = setTimeout(()=>reject(new Error('Fixture startup timed out: '+stderr)),180000);
    child.stdout.on('data',chunk=>{
      stdout += chunk;
      for (const line of stdout.split('\n')) {
        if (!line.startsWith('{"url":')) continue;
        try { const data = JSON.parse(line); clearTimeout(timeout); resolve(data); } catch (_) {}
      }
    });
    child.once('exit',code=>{clearTimeout(timeout);reject(new Error(`Fixture exited ${code}: ${stderr}`));});
    child.once('error',error=>{clearTimeout(timeout);reject(error);});
  }).catch(error=>{child.kill('SIGINT');throw error;});
  return {details, async stop() {
    if (child.exitCode !== null) return;
    await new Promise(resolve=>{
      const timeout = setTimeout(()=>child.kill('SIGTERM'),10000);
      child.once('exit',()=>{clearTimeout(timeout);resolve();}); child.kill('SIGINT');
    });
  }};
}

(async()=>{
  const fixture = await startFixture(); let browser;
  try {
    browser = await chromium.launch({headless:true,args:['--no-sandbox']});
    const page = await browser.newPage({viewport:{width:1500,height:1000}}), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(fixture.details.url + '#' + fixture.details.business_slug); await page.locator('#agentrows').waitFor();
    await page.waitForFunction(()=>state.biz && tLastBiz === state.biz);
    await page.evaluate(()=>{openTasks(true);tSetView('businessNotes');});
    await page.locator('#ntnew').waitFor();
    assert.equal(await page.locator('[data-v="journal"]').count(),0);
    assert(await page.locator('.jrhcell[data-key]').count()>0);
    await page.locator('.jrhcell[data-key]').first().click();
    await page.locator('#nttitle').waitFor();
    const selectedDate = await page.evaluate(()=>ntById(ntSel).date);
    assert.match(selectedDate,/^\d{4}-\d{2}-\d{2}$/);
    assert.equal(await page.evaluate(()=>'category' in ntById(ntSel)),false,'Project notes carry no fixed category');
    await page.locator('#nttitle').fill('Structured field notes');
    let row=page.locator('#ntdoc .ntline').first();
    await row.fill('- Send proposal');
    row=page.locator('#ntdoc .ntline').first();await page.locator('#ntdoc').press('End');
    await page.locator('#ntdoc').press('Shift+Enter');await page.keyboard.insertText('Include prices');
    assert.equal(await page.locator('#ntdoc .ntline').count(),1);
    assert.equal(await row.textContent(),'Send proposal\nInclude prices');
    await page.locator('#ntdoc').press('Enter');await page.keyboard.insertText('Separate free text <safe>.');
    assert.equal(await page.locator('#ntdoc .ntline').count(),2);
    assert.equal(await page.locator('.ntball').count(),1);
    await page.locator('.ntball').click();await page.locator('.ntball').click();
    await page.evaluate(()=>ntSaveNow());
    const saved=await page.evaluate(()=>({id:ntSel,body:ntById(ntSel).content}));
    assert.equal(saved.body,'- Send proposal\u2028Include prices\nSeparate free text <safe>.');
    await page.evaluate(()=>ntGoHome());await page.locator('#ntnew').waitFor();
    await page.evaluate(id=>ntSelect(id),saved.id);await page.locator('#nttitle').waitFor();
    assert.equal(await page.locator('#ntdoc .ntline').first().textContent(),'Send proposal\nInclude prices');
    assert.equal(await page.locator('.ntb-in_progress').count(),1);
    // Upload must flush this unsaved change first.
    await page.locator('#ntdoc .ntline').nth(1).fill('Separate free text <updated>.');
    await page.locator('#ntupload').click();
    await page.waitForFunction(()=>!!ntById(ntSel)?.uploadId);
    await page.waitForFunction(()=>!ntUploadingIds.size);
    assert.equal(await page.locator('#ntupload').isDisabled(),true);
    const result=await page.evaluate(async()=>{
      const n=ntById(ntSel);
      return {note:n, source:await api('/biz/'+state.biz+'/context/file?p='+encodeURIComponent('_dumps/'+n.uploadId+'.md'))};
    });
    assert(result.note.content.includes('<updated>'));
    assert.equal(result.note.changedSinceUpload,false);
    assert(result.note.filing);
    await page.locator('#ntdoc .ntline').nth(1).fill('New revision.');await page.evaluate(()=>ntSaveNow());
    assert.match(await page.locator('#ntfiling').textContent(),/Changed since submission/);
    assert.equal(await page.locator('#ntupload').isEnabled(),true);
    await page.locator('#ntupload').click();await page.waitForFunction(id=>ntById(ntSel)?.uploadId!==id,result.note.uploadId);
    await page.waitForFunction(()=>!ntUploadingIds.size);
    await page.locator('#ntviewcontext').click();await page.locator('#ctxview').waitFor();
    assert.equal(await page.evaluate(()=>state.tab),'context');
    assert.equal(await page.evaluate(()=>state.ctx),'_dumps/'+await page.evaluate(()=>ntById(ntSel).uploadId)+'.md');
    await page.evaluate(()=>{openTasks(true); tSetView('businessNotes');});
    await page.locator('#nttitle').waitFor();
    assert.equal(await page.evaluate(()=>ntSel), saved.id);
    await page.evaluate(()=>ntGoHome());
    await page.locator('#ntnew').waitFor();
    await page.evaluate(async()=>{
      await ntFetch();
      await ntSelect(ntNotes.find(n=>n.title==='Literal strategy fixture').id);
    });
    await page.locator('#nttitle').waitFor();
    assert.equal(await page.locator('.ntball').count(),1);
    const literal = '- Literal dash\n\n⸻\nFree text <tag>.\n';
    assert.equal(await page.evaluate(()=>ntById(ntSel).content),literal);
    await page.locator('#ntdoc .ntline').last().fill('- New task');
    assert.equal(await page.locator('.ntball').count(),2);
    await page.locator('.ntball').last().dispatchEvent('click');
    await page.evaluate(()=>ntSaveNow());
    await page.evaluate(async()=>{const id=ntSel;await ntGoHome();await ntSelect(id);});
    assert.equal(await page.locator('.ntball').count(),2);
    assert.equal(await page.locator('#ntdoc .ntline').last().textContent(),'New task');
    assert.match(await page.locator('.ntball').last().getAttribute('aria-label'),/White/);
    assert.equal(await page.evaluate(()=>ntById(ntSel).content),literal + '- New task');
    assert.match(await page.locator('[data-v="businessNotes"] svg').innerHTML(),/M16 8 2 22/);
    assert.deepEqual(errors,[]);
    console.log('Project notes activity, multiline boundaries, status persistence, save-before-upload, resubmission and Context navigation passed.');
  } finally {if(browser)await browser.close();await fixture.stop();}
})().catch(error=>{console.error(error);process.exitCode=1;});
