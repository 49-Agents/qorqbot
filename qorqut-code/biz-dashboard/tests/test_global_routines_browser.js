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
  const child = spawn('python3', ['tests/service_browser_fixture.py', '--dashboard-root', DASHBOARD, '--global-signoffs'], {cwd:CORE, env:{...process.env,PYTHONPATH:'.'}, stdio:['ignore','pipe','pipe']});
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  const details = await new Promise((resolve,reject) => {
    let stdout = '';
    const timeout = setTimeout(()=>reject(new Error('Fixture startup timed out: '+stderr)),120000);
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
  fs.mkdirSync(OUT,{recursive:true});
  const fixture=await startFixture(), {details}=fixture;
  let browser,page,checks=0;
  const errors=[],posts=[];
  try {
    browser=await chromium.launch({headless:true,args:['--no-sandbox']});
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    await context.route('**/*',route=>route.request().url().startsWith(details.origin)?route.continue():route.abort());
    page=await context.newPage();page.setDefaultTimeout(15000);
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{if(request.method()==='POST'&&request.url().includes('/api/'))posts.push({url:request.url(),body:request.postDataJSON()});});
    const check=async(label,fn)=>{await fn();checks++;console.log('ok '+label);};
    const screenshot=name=>page.screenshot({path:path.join(OUT,name+'.png'),animations:'disabled'});
    const withinViewport=async selector=>page.locator(selector).evaluate(el=>{const r=el.getBoundingClientRect();return r.width>0&&r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;});
    await page.goto(details.url);await page.locator('#agentrows').waitFor();
    await page.evaluate(async businessId=>{
      for(const global of [false,true]) {
        coreData(await post(global?'/root/routines':'/biz/widget/routines',{
          op:'add',
          definition:{name:global?'Global repository check':'Repository check',program:'repository.status',arguments:{},schedule:'17 4 * * *',timezone:'UTC',timeout_seconds:900,departments:['general'],why:'Isolated routine fixture.',status:'active',target:global?'each-business':'business'}
        }));
      }
    },details.business_id);
    await check('Global routines use only root endpoints and need no business review context',async()=>{
      await page.setViewportSize({width:1440,height:1000});
      await page.evaluate(()=>selectGlobal('routines'));
      await page.waitForFunction(()=>rtData && rtGlobal() && rtData.routines.some(r=>r.name==='Global repository check'));
      const pane=page.locator('#tbody');
      assert.equal(await page.evaluate(()=>state.biz),null);
      assert.equal(await page.locator('.global-review select').count(),0);
      assert.equal(await pane.locator('.rttitle').filter({hasText:/^Repository check$/}).count(),0);
      assert.equal(await pane.locator('.rtnav').evaluate(el=>getComputedStyle(el).flexDirection),'column');
      await pane.locator('#rtnew').click();
      assert.equal(await page.locator('#rtform [data-scope="biz"]').count(),0);
      await page.locator('[name="name"]').fill('Global shell check');
      await page.locator('[name="program"]').selectOption('repository.status');
      await page.locator('[name="why"]').fill('Check shared global routine management.');
      await page.waitForFunction(()=>/\d{4}-\d{2}-\d{2}/.test(document.querySelector('#rtpreview').textContent));
      await page.locator('.mfoot .primary').click();await page.locator('.overlay').waitFor({state:'detached'});
      await page.waitForFunction(()=>rtData.routines.some(r=>r.name==='Global shell check'));
      const request=posts.filter(p=>p.body.op==='add').at(-1);
      assert(request.url.endsWith('/root/routines'));
      assert.equal('review_business_id' in request.body,false);
      assert.equal(request.body.definition.target,'each-business');
      await screenshot('global-routines');
    });
    await check('Global routines can edit, pause, resume, run, inspect history and delete',async()=>{
      const pane=page.locator('#tbody');
      const row=()=>pane.locator('.rtrow').filter({has:page.getByRole('button',{name:'Global shell check',exact:true})});
      const action=async label=>{await row().locator('[data-action="more"]').click();await page.locator('.menu').getByText(label,{exact:true}).click();};
      await action('Edit routine');
      await page.locator('[name="why"]').fill('Updated global routine purpose.');
      await page.locator('.mfoot .primary').click();await page.locator('.overlay').waitFor({state:'detached'});
      await page.waitForFunction(()=>rtData.routines.some(r=>r.name==='Global shell check'&&r.why==='Updated global routine purpose.'));
      await action('Pause');
      await pane.locator('#rtpaused').check();
      await page.waitForFunction(()=>rtData.routines.some(r=>r.name==='Global shell check'&&r.status==='paused'));
      await action('Resume');
      await page.waitForFunction(()=>rtData.routines.some(r=>r.name==='Global shell check'&&r.status==='active'));
      await row().locator('[data-action="run"]').click();
      await page.waitForFunction(()=>Array.isArray(rtRuns)&&rtRuns.some(r=>r.name==='Global shell check'));
      await pane.locator('[data-page="logs"]').click();
      await pane.locator('.rtrunrow').filter({hasText:'Global shell check'}).first().click();
      assert(await page.locator('.rtdetail').isVisible());
      await page.locator('.mx').click();
      // The fixture keeps jobs queued; stop that exact global job through the shared control.
      await page.evaluate(async()=>{
        const run=rtRuns.find(r=>r.name==='Global shell check');
        const routine=rtData.routines.find(r=>r.name==='Global shell check');
        await rtStop({id:run.job_id,business_id:run.business_id,definition_ref_json:{routine_id:routine.id,scope:'global'}},{disabled:false});
      });
      const stop=posts.filter(p=>p.body.op==='stop').at(-1);
      assert(stop.url.endsWith('/root/routines'));assert.equal('review_business_id' in stop.body,false);
      await pane.locator('[data-page="active"]').click();
      await action('Delete routine');await page.locator('.mfoot .primary').click();
      await page.waitForFunction(()=>!rtData.routines.some(r=>r.name==='Global shell check'));
      const mutations=posts.filter(p=>['set','pause','resume','run','rm'].includes(p.body.op)&&p.url.endsWith('/root/routines'));
      for(const op of ['set','pause','resume','run','rm'])assert(mutations.some(p=>p.body.op===op));
      await page.setViewportSize({width:390,height:844});
      assert(await withinViewport('#tbody #rtnew'));
      await screenshot('global-routines-mobile');
    });
    assert.deepEqual(errors,[]);
    await check('Global signoffs approve and reject exact proposals in the Global queue',async()=>{
      await page.setViewportSize({width:1280,height:900});
      await page.evaluate(()=>goGlobalTab('signoffs'));
      const pane=page.locator('#tbody');
      const approved=pane.locator(`.sg-row[data-id="${details.global_approve_id}"]`);
      const rejected=pane.locator(`.sg-row[data-id="${details.global_reject_id}"]`);
      await approved.waitFor(); await rejected.waitFor();
      assert.equal(await pane.locator(`.sg-row[data-id="${details.signoff_id}"]`).count(),0);
      await approved.locator('.sg-rowhead').click();
      await approved.locator('[data-sg="approve"]:enabled').waitFor();
      await screenshot('global-signoffs');
      await approved.locator('[data-sg="approve"]').click();
      await approved.waitFor({state:'detached'});
      await rejected.locator('.sg-rowhead').click();
      await rejected.locator('[data-sg="reject"]:enabled').waitFor();
      await rejected.locator('.sg-feedback').fill('Keep this routine business-specific.');
      await rejected.locator('[data-sg="reject"]').click();
      await rejected.waitFor({state:'detached'});
      const outcomes=await page.evaluate(async ids=>Promise.all(ids.map(async id=>coreData(await api('/root/signoffs/'+id)).signoff)),[details.global_approve_id,details.global_reject_id]);
      assert(['approved','applied'].includes(outcomes[0].state));
      assert.equal(outcomes[1].state,'rejected');
      assert.equal(outcomes[1].decision.feedback,'Keep this routine business-specific.');
      assert(posts.filter(p=>p.url.includes('/signoffs/')).every(p=>p.url.includes('/root/signoffs/')));
      await page.evaluate(()=>selectBiz('widget'));
      await page.evaluate(()=>{openTasks(true);tSetView('signoffs');});
      await page.locator(`#tbody .sg-row[data-id="${details.signoff_id}"]`).waitFor();
      assert.equal(await page.locator(`#tbody .sg-row[data-id="${details.global_approve_id}"]`).count(),0);
    });
    console.log(`${checks} global routine lifecycle scenarios passed.`);
  } catch(error) {
    if(page){await page.screenshot({path:path.join(OUT,'failure.png')}).catch(()=>{});console.error((await page.locator('body').innerText().catch(()=>'' )).slice(-3000));}
    throw error;
  } finally {if(browser)await browser.close();await fixture.stop();}
})().catch(error=>{console.error(error);process.exitCode=1;});
