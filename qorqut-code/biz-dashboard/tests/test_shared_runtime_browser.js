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
const coreDataForTest = result => {assert(['ok','accepted','signoff_required'].includes(result.status),JSON.stringify(result));return result.data;};
const OUT = process.env.SHARED_SCREENSHOTS || path.join(__dirname, 'artifacts/shared-runtime');

async function startFixture() {
  const child = spawn(process.env.BAZARLYK_TEST_PYTHON || path.join(CORE,'.venv/bin/python'), ['tests/service_browser_fixture.py', '--dashboard-root', DASHBOARD], {cwd:CORE, env:{...process.env,PYTHONPATH:'.'}, stdio:['ignore','pipe','pipe']});
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  const details = await new Promise((resolve,reject) => {
    let stdout = '';
    const timeout = setTimeout(()=>reject(new Error('Fixture startup timed out: '+stderr)),60000);
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
  const fixture = await startFixture(), {details} = fixture;
  let browser, page, checks = 0;
  const errors = [], posts = [];
  try {
    browser = await chromium.launch({headless:true,args:['--no-sandbox']});
    const context = await browser.newContext({viewport:{width:1440,height:1000}});
    await context.route('**/*',route=>route.request().url().startsWith(details.origin) ? route.continue() : route.abort());
    page = await context.newPage(); page.setDefaultTimeout(15000);
    page.on('pageerror',error=>errors.push(error.stack || error.message));
    page.on('request',request=>{if(request.method()==='POST' && request.url().includes('/api/'))posts.push({url:request.url(),body:request.postDataJSON()});});
    const check = async (label,fn)=>{await fn();checks++;console.log('ok '+label);};
    const read = suffix=>page.evaluate(async suffix=>api('/biz/'+state.biz+suffix),suffix);
    const write = (suffix,body)=>page.evaluate(async ({suffix,body})=>post('/biz/'+state.biz+suffix,body),{suffix,body});
    const screenshot = name=>page.screenshot({path:path.join(OUT,name+'.png'),animations:'disabled'});
    const drawer = async name=>{await page.evaluate(name=>{openTasks(true);tSetView(name);},name);await page.locator('#tbody').waitFor();};
    const withinViewport = async selector=>{
      // Opening the drawer animates its position; inspect the settled controls.
      await page.waitForFunction(selector=>{
        const el=document.querySelector(selector);if(!el)return false;
        const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&r.left>=0&&r.top>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1;
      },selector);
      return true;
    };
    await page.goto(details.url + '#' + details.business_slug); await page.locator('#agentrows').waitFor();
    await check('canonical failures display text while coreData preserves code and details',async()=>{
      const result = await page.evaluate(async()=>{
        const response=await post('/biz/'+state.biz+'/agent',{name:'incomplete'});
        try {coreData(response);} catch(error) {return {text:response.error,code:error.code,status:error.httpStatus,details:error.details};}
      });
      assert.equal(typeof result.text,'string'); assert.notEqual(result.text,'[object Object]');
      assert.equal(result.status,422); assert.equal(typeof result.code,'string'); assert.equal(typeof result.details,'object');
    });
    await check('business agents page offers no local staffing form',async()=>{
      assert.equal(await page.getByRole('button',{name:/New agent/}).count(),0);
    });
    await check('terminal refresh sits beside the Chat/Terminal toggle',async()=>{
      await page.locator('.item.agent[data-a="business-agent"]').click();
      await page.locator('.gchat-session-switch [data-terminal]').click();
      assert.equal(await page.locator('.termpane button').count(),0);
      const refreshBox=await page.locator('[data-agent-refresh]').boundingBox();
      const toggleBox=await page.locator('.gchat-view-toggle').boundingBox();
      assert(refreshBox.x >= toggleBox.x + toggleBox.width);
      await page.locator('[data-agent-refresh]').click();
      await page.waitForSelector('.termhost .xterm');
    });
    await check('real Signoff focus stays inline, approval retains owner notes and backend applies',async()=>{
      // Agent panes no longer carry a Signoffs tab; the drawer is the one review surface.
      await drawer('signoffs');
      const row=page.locator(`#taskspanel .sg-row[data-id="${details.signoff_id}"]`);
      await row.locator('.sg-rowhead').click();await row.locator('.sg-feedback').fill('This draft must not be submitted on approval.');
      assert.equal(await page.locator('.overlay').count(),0);
      assert.equal(await page.locator('#agpad .ttab[data-v="signoffs"]').count(),0);
      assert.equal(await row.locator('.sg-decided, .sg-diff, .sg-beforeafter').count(),0);
      await screenshot('agent-signoffs-updated');
      await drawer('tasks');await drawer('signoffs');
      if(await row.locator('.sg-rowhead').getAttribute('aria-expanded')!=='true')await row.locator('.sg-rowhead').click();
      assert.equal(await row.locator('.sg-feedback').inputValue(),'This draft must not be submitted on approval.');
      await row.locator('.sg-feedback').fill('Shared feedback draft.');
      await row.locator('.sg-detail .sg-approve').click();
      const request=posts.filter(p=>p.url.endsWith('/signoffs/'+details.signoff_id+'/approve')).at(-1);
      assert.equal(request.body.feedback,'Shared feedback draft.');
      await row.waitFor({state:'detached',timeout:30000});
      const deadline = Date.now() + 30000;
      let signoff;
      do {
        const response = await read('/signoffs/'+details.signoff_id);
        assert.equal(response.status,'ok');
        signoff = response.data.signoff;
        if (signoff.state !== 'approved') break;
        await page.waitForTimeout(100);
      } while (Date.now() < deadline);
      assert.equal(signoff.state,'applied');
      assert.equal(signoff.decision.feedback,'Shared feedback draft.');
      const applied=await read('/agents/'+details.agent_id);
      assert.equal(applied.data.agent.assignment_brief,'Own the next small growth experiment.');
      await drawer('signoffs');
      await page.waitForFunction(id=>document.querySelectorAll(`.sg-row[data-id="${id}"]`).length===0,details.signoff_id,{timeout:30000});
      await screenshot('desktop-signoff-cleared');
    });
    await check('stale task editor retains its edit and sends the observed revision',async()=>{
      // Task panes adopt observed revisions, so hold task reads until the peer change is unseen.
      await drawer('tasks');
      const tasks=await read('/tasks?all=1'), task=tasks[0];
      await page.evaluate(id=>tOpenTask(id),task.id);await page.locator('.dtitle').waitFor();
      const hold=route=>route.request().method()==='GET' ? route.abort() : route.continue();
      await page.route(/\/api\/(biz\/[^/]+\/tasks|me\/tasks)/,hold);
      const changed=await write('/task/'+task.id,{set:{title:'A newer task title'},expected_revision:task.revision});assert(!changed.error);
      await page.locator('.dtitle').click();await page.locator('.dtitle-in').fill('Keep this unsaved task title');
      await page.locator('.dtitle-in').press('Enter');
      await page.locator('#td-error').waitFor();
      await page.unroute(/\/api\/(biz\/[^/]+\/tasks|me\/tasks)/,hold);
      assert.equal(await page.locator('.dtitle-in').inputValue(),'Keep this unsaved task title');
      assert.match(await page.locator('#td-error').textContent(),/revision|changed/i);
      assert.equal(posts.filter(p=>p.body.set?.title==='Keep this unsaved task title').at(-1).body.expected_revision,task.revision);
      await screenshot('desktop-task-conflict');await page.locator('.mx').click();
    });
    await check('notes retain a conflicting draft until an explicit reload',async()=>{
      const note=(await read('/notes?target_business_id='+encodeURIComponent(await page.evaluate(()=>state.bizs.find(b=>b.slug===state.biz).id))))[0];
      await drawer('businessNotes');await page.evaluate(async id=>{await ntFetch();await ntSelect(id);},note.id);await page.locator('#nttitle').waitFor();
      const changed=await write('/notes/'+note.id,{content:'Newer note evidence.',target_business_id:await page.evaluate(()=>state.bizs.find(b=>b.slug===state.biz).id),expected_revision:note.revision,expected_content_hash:note.content_hash});assert(!changed.error,JSON.stringify(changed));
      await page.locator('#nttitle').fill('My unsaved note title');await page.evaluate(()=>ntSaveNow());
      await page.locator('#nterror').waitFor();
      assert.equal(await page.locator('#nttitle').inputValue(),'My unsaved note title');
      assert.match(await page.locator('#nterror').textContent(),/revision|changed/i);
      await page.evaluate(()=>tSetView('tasks'));await page.waitForTimeout(200);
      assert.equal(await page.evaluate(()=>tView),'businessNotes');
      assert.equal((await read('/notes')).find(row=>row.id===note.id).content,'Newer note evidence.');
      await screenshot('desktop-note-conflict');
      await page.locator('#nterror button').click();
      await page.waitForFunction(()=>!ntEditState.error && ntEditState.revision>1);
    });
    await check('registered context upload IDs resolve and stale source edits conflict',async()=>{
      await page.evaluate(async()=>{openTasks(false);await goTab('context');});await page.locator('#ctxsearch').waitFor();
      const context=await read('/context'), sourcePath=context.pillars[0];assert.equal(typeof sourcePath,'string');
      await page.evaluate(path=>ctxGo(path),sourcePath);await page.locator('#jta').waitFor();
      const source=await read('/context/file?p='+encodeURIComponent(sourcePath));
      const peer=await write('/context/save',{path:sourcePath,text:source.text+'\nPeer addition.\n',expected_revision:source.revision,expected_content_hash:source.content_hash});assert(!peer.error,JSON.stringify(peer));
      await page.locator('#jta').fill('Local source draft.');await page.evaluate(()=>jSave());
      assert.equal(await page.locator('#jsave').textContent(),'save failed');assert.equal(await page.locator('#jta').inputValue(),'Local source draft.');
      assert.equal(posts.filter(p=>p.body.text&&p.body.text.endsWith('Local source draft.')).at(-1).body.expected_revision,source.revision);
      await page.locator('#jdiscard').click();
      assert.equal(await page.evaluate(()=>{_ctxData.inbox.push({id:'registered-upload',path:'context/_inbox/2026-09-08-10000000-0000-4000-8000-000000000001.md'});return uploadId('_inbox/2026-09-08-10000000-0000-4000-8000-000000000001.md');}),'registered-upload');
    });
    let routine;
    await check('routine form saves a typed program with no command or output path',async()=>{
      await drawer('routines'); await page.locator('#rtnew').click();
      await page.locator('[name="name"]').fill('Repository check');
      await page.locator('[name="program"]').selectOption('repository.status');
      await page.locator('[name="why"]').fill('Check registered repository status.');
      assert.equal(await page.locator('[name="run"], [name="commit"], [name="skip_if_live"]').count(),0);
      assert.doesNotMatch(await page.locator('#rtform').innerText(),/wait while agents/i);
      assert.equal(await page.locator('[name="why"]').first().evaluate(el=>getComputedStyle(el).appearance),'none');
      await screenshot('desktop-routine-form');
      await page.locator('.mfoot .primary').click();await page.locator('.overlay').waitFor({state:'detached'});
      const rows=await read('/routines');routine=rows.routines.find(r=>r.name==='Repository check');assert(routine);
      assert.equal(routine.program,'repository.status');assert.deepEqual(routine.arguments,{});
      const request=posts.filter(p=>p.body.op==='add').at(-1).body;
      assert.equal(request.definition.target,'business');assert.equal(request.review_business_id,undefined);
      assert(!('run' in request.definition));assert(!('commit' in request.definition));assert(!('skip_if_live' in request.definition));
    });
    await check('global routine saves through the root route without a business review destination',async()=>{
      await page.locator('#rtnew').click();
      await page.locator('[data-scope="global"]').click();
      await page.locator('[name="name"]').fill('Global repository check');
      await page.locator('[name="program"]').selectOption('repository.status');
      await page.locator('[name="why"]').fill('Review registered business repositories.');
      assert.equal(await page.locator('[name="review_business_id"]').count(),0);
      await page.waitForFunction(()=>/\d{4}-\d{2}-\d{2}/.test(document.querySelector('#rtpreview').textContent));
      await page.locator('.mfoot .primary').click();await page.locator('.overlay').waitFor({state:'detached'});
      const request=posts.filter(p=>p.url.endsWith('/root/routines')).at(-1).body;
      assert.equal(request.review_business_id,undefined);assert.equal(request.definition.target,'each-business');
      assert((await read('/routines')).routines.some(r=>r.scope==='global'&&r.name==='Global repository check'));
    });
    await check('routine definitions stay compact with scope and next time, without cost',async()=>{
      await page.evaluate(()=>rtRefresh());
      assert.equal(await page.locator('.rttoggle [data-page]').count(),2);
      const rows=page.locator('.rtrow[data-key]').filter({hasText:/Repository check|Global repository check/});assert.equal(await rows.count(),2);
      for(const row of await rows.all()) {
        assert(await row.evaluate(el=>el.scrollWidth<=el.clientWidth));
        assert.equal(await row.locator('.lnscopeico').count(),1);
        assert.match(await row.locator('.rtmeta').innerText(),/In /);
        assert.doesNotMatch(await row.innerText(),/\$|\*/);
      }
      await screenshot('desktop-routines-active');
    });
    await check('targeted stop sends the specific durable job and routine IDs',async()=>{
      const queued=await write('/routines',{op:'run',id:routine.id,expected_revision:routine.revision,review_business_id:details.business_id});
      assert(!queued.error,JSON.stringify(queued));
      const runs=await read('/routines/'+routine.id+'/runs');assert(runs.length);
      const job=await read('/jobs/'+runs[0].job_id);assert(job.data.job);
      await page.evaluate(async current=>rtStop(current,{disabled:false}),job.data.job);
      const request=posts.filter(p=>p.body.op==='stop').at(-1).body;
      assert.equal(request.job_id,runs[0].job_id);assert.equal(request.id,routine.id);assert.equal(request.target_business_id,details.business_id);
      const after=await read('/jobs/'+request.job_id);assert(['cancel_requested','cancelled','succeeded','failed'].includes(after.data.job.state));
    });
    await check('routine history has one cost and historical details without a next time',async()=>{
      await page.evaluate(()=>rtRefresh());
      await page.locator('[data-page="logs"]').click();
      const row=page.locator('.rtrunrow').first();await row.waitFor();
      assert.equal(await row.locator('.rtprice').count(),1);
      assert((await row.boundingBox()).height<=62);
      assert.doesNotMatch(await row.innerText(),/Next |undefined|null/);
      await screenshot('desktop-routines-history');
      await row.click();
      assert.match(await page.locator('.modal').innerText(),/Repository check|Widget|widget/);
      assert.doesNotMatch(await page.locator('.modal').innerText(),/Next scheduled|undefined|rc null/);
      await screenshot('desktop-routine-result');
      await page.locator('.mx').click();
      await page.locator('[data-page="active"]').click();
    });
    await check('replacement platform submission still needs the shared owner approval',async()=>{
      await drawer('signoffs');
      const original=coreDataForTest(await read('/signoffs/'+details.platform_signoff_id)).signoff;
      const submission=original.platform.submission;
      const response=await write('/operations/signoffs_platform_submit',{
        plugin_id:submission.plugin_id,tool:submission.tool,ability_agent_id:submission.agent_id,
        payload:{...submission.payload,title:'Edited workflow evidence'},why:submission.why,
        supersedes_signoff_id:original.id,expected_superseded_status_revision:original.status_revision,
        idempotency_key:'browser-platform-replacement'});
      assert.equal(response.status,'signoff_required',JSON.stringify(response));
      const identity=response.signoff.id;
      const pending=coreDataForTest(await read('/signoffs/'+identity)).signoff;
      assert.equal(pending.state,'pending');assert.equal(pending.decision_state,'pending');
      assert.equal(pending.supersedes_id,original.id);
      await page.evaluate(()=>sgRefresh());
      const replacement=page.locator(`.sg-row[data-id="${identity}"]`);
      await replacement.locator('.sg-rowhead').click();
      await replacement.locator('.sg-detail [data-sg=approve]').waitFor();
      await screenshot('desktop-signoff-replacement');
      const approved=page.waitForResponse(r=>r.url().endsWith('/signoffs/'+identity+'/approve')&&r.request().method()==='POST');
      await replacement.locator('.sg-detail [data-sg=approve]').click();
      assert.equal((await (await approved).json()).status,'accepted');
      assert.equal(coreDataForTest(await read('/signoffs/'+identity)).signoff.decision_state,'approved');
    });
    await check('mobile forms and Signoffs actions stay within the viewport',async()=>{
      await drawer('signoffs');
      await page.setViewportSize({width:390,height:844});
      await screenshot('mobile-signoffs');
      assert(await withinViewport('#taskspanel'));
      await page.evaluate(()=>{openTasks(false);goTab('agents');});
      await drawer('routines');await page.evaluate(()=>rtRefresh());
      assert(await withinViewport('#taskspanel'));
      assert(await withinViewport('.rtrow[data-key]'));
      for(const row of await page.locator('.rtrow[data-key]').all()) assert(await row.evaluate(el=>el.scrollWidth<=el.clientWidth));
      await screenshot('mobile-routines-active');
      await page.locator('[data-page="logs"]').click();
      await screenshot('mobile-routines-history');
      assert(await withinViewport('.rtrunrow'));
      await page.locator('[data-page="active"]').click();await page.locator('#rtnew').click();
      await page.locator('[name="program"]').selectOption('repository.status');
      assert(await withinViewport('.modal'));assert(await withinViewport('.mfoot .primary'));
      await screenshot('mobile-routine-form');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.locator('.mx').click();
    });
    assert.deepEqual(errors,[]);
    console.log(`${checks} real-runtime browser scenarios passed. Screenshots: ${OUT}`);
  } catch(error) {
    if(page) {await page.screenshot({path:path.join(OUT,'failure.png'),animations:'disabled'}).catch(()=>{});console.error((await page.locator('body').innerText().catch(()=>'' )).slice(-3000));}
    throw error;
  } finally {if(browser)await browser.close();await fixture.stop();}
})().catch(error=>{console.error(error);process.exitCode=1;});
