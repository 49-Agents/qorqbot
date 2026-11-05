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
const OUT = process.env.SHARED_SCREENSHOTS || path.join(__dirname, 'artifacts/agent-inbox');

async function startFixture() {
  const child = spawn('python3', ['tests/service_browser_fixture.py', '--dashboard-root', DASHBOARD, '--inbox'], {cwd:CORE, env:{...process.env,PYTHONPATH:'.'}, stdio:['ignore','pipe','pipe']});
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
 const fixture=await startFixture(); let browser, page;
 try {
  browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,args:['--no-sandbox']});
  page=await browser.newPage({viewport:{width:1500,height:1000}}); const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(fixture.details.url+'#widget');
  await page.locator('#agpad-body-tab-inbox').click();
  await page.locator('#agpad-body .agpad-inbox-status[data-delivery-state]').waitFor();
  const messageBody = '<script>window.MESSAGE_INJECTED=true</script>\nPlease review this message.';
  const sent = await page.request.post(fixture.details.origin + '/api/biz/widget/operations/agents_message_send', {
    headers: {Origin:fixture.details.origin}, data: {to_agent_id:fixture.details.agent_id, message:messageBody, summary:'Review this message for owner', idempotency_key:require('node:crypto').randomUUID()}
  });
  assert.equal(sent.status(), 200, await sent.text());
  await page.evaluate(()=>agpadRefreshInbox(_agpad));
  const message = page.locator('.agpad-message');
  assert.equal(await message.locator('.agpad-message-body').isVisible(),false);
  await message.getByText('Review this message for owner',{exact:true}).waitFor();
  await message.getByText('Message from Owner', {exact:true}).click();
  assert.equal(await message.locator('.agpad-message-body').innerText(), messageBody);
  assert.equal(await page.evaluate(()=>window.MESSAGE_INJECTED), undefined);
  assert.equal(await message.evaluate(el=>el.open), true);
  const story=page.locator('[data-inbox-source="'+fixture.details.inbox_story_id+'"]').first();
  await story.waitFor();
  assert.equal(await page.locator('#agpad-body').getByText('Subscriptions',{exact:true}).count(),1);
  assert(await page.locator('.agpad-inbox-item').evaluateAll(rows=>rows.every(row=>row.scrollHeight<=row.clientHeight+2)), 'Inbox cards must fit all wrapped text');
  fs.mkdirSync(OUT,{recursive:true});
  await page.screenshot({path:path.join(OUT,'inbox.png')});
  await story.click();
  await page.waitForFunction(id=>storyView?.story?.id===id,fixture.details.inbox_story_id);
  await page.evaluate(()=>goTab('agents'));
  await page.locator('#agpad-body-tab-inbox').click();
  const note=page.locator('[data-inbox-source="'+fixture.details.inbox_note_id+'"]').first();
  await note.click();
  // Business-owned strategy notes open as their current source.
  const source=page.locator('.modal[role="dialog"]');
  await source.locator('.story-source',{hasText:'Updated inbox note'}).waitFor();
  assert.equal(await source.locator('.mt').innerText(),'Inbox browser note');
  await source.locator('.mx').click();
  await page.evaluate(()=>selectGlobal('agents'));
  await page.locator('[data-global-agent="'+fixture.details.inbox_global_agent_id+'"]').click();
  await page.locator('#agpad-body-tab-inbox').click();
  const globalDocument=page.locator('[data-inbox-source="'+fixture.details.inbox_global_document_id+'"]');
  await globalDocument.waitFor();
  await page.screenshot({path:path.join(OUT,'global-inbox.png')});
  await globalDocument.click();
  await page.getByText('Global source body for inbox browser',{exact:true}).waitFor();
  assert.deepEqual(errors,[]);
  console.log('Agent inbox browser: direct message expansion/escaping, pending/subscriptions, delivery state, Story, note and Global source navigation passed. Screenshots: '+OUT);
 } catch(error) { if(page) { await page.screenshot({path:path.join(OUT,'failure.png')}); console.error((await page.locator('body').innerText()).slice(-2500)); } throw error; } finally {if(browser)await browser.close();await fixture.stop();}
})().catch(error=>{console.error(error);process.exitCode=1;});
