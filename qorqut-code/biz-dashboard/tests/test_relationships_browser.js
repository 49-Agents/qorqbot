#!/usr/bin/env node
// Run against service_browser_fixture.py only, never the live service.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin=process.env.CONTEXT_TEST_ORIGIN;
assert(origin && new URL(origin).hostname==='127.0.0.1' && new URL(origin).port!=='1049');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,args:['--no-sandbox']});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(origin+'/?token=owner-bootstrap-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx');
    await page.locator('#agentrows').waitFor();
    const fixtures=await page.evaluate(async()=>{
      await selectBiz('widget');
      const story=(await storyCall('widget','stories_create',{title:'Context acceptance',purpose:'Check related evidence',details:'Owner-authored details.'})).story;
      const task=(await storyCall('widget','tasks_create',{title:'Context acceptance task'})).task;
      const source={type:'task',id:task.id},target={type:'story',id:story.id};
      for(const role of ['part_of','concerns'])await storyCall('widget','relationships_create',{source,target,role,context:'Synthetic relationship explanation.'});
      await storyCall('widget','relationships_create',{source:target,target,role:'related_to',context:'Self reference.'});
      ls('story:widget',story.id);await goTab('stories');
      return {story,task};
    });
    await page.locator('#story-context-review-pane .context-relation').first().waitFor();
    assert.equal(await page.locator('#story-context-review-pane .context-relation').count(),3);
    let row=page.locator('#story-context-review-pane .context-relation').filter({hasText:'Context acceptance task'}).first();
    assert(await row.getByRole('button',{name:'No change needed',exact:true}).isDisabled());
    let releaseRefresh,refreshStarted,refreshDone,intercepted=false;
    const held=new Promise(resolve=>releaseRefresh=resolve),started=new Promise(resolve=>refreshStarted=resolve),finished=new Promise(resolve=>refreshDone=resolve);
    const holdRefresh=async route=>{
      if(intercepted)return route.continue();
      intercepted=true;const response=await route.fetch();refreshStarted();await held;await route.fulfill({response});refreshDone();
    };
    await page.route('**/operations/context_reviews_list',holdRefresh);
    await page.evaluate(()=>storyRefresh(storyView,true));
    await started;
    await row.locator('textarea').fill('The task is already accounted for.');
    releaseRefresh();
    await finished;
    await page.unroute('**/operations/context_reviews_list',holdRefresh);
    await page.waitForTimeout(200);
    assert.equal(await row.locator('textarea').inputValue(),'The task is already accounted for.');
    await page.evaluate(()=>storyRefresh(storyView,true));
    assert.equal(await row.locator('textarea').inputValue(),'The task is already accounted for.');
    let lostResponse=false;
    await page.route('**/operations/context_review',async route=>{
      const response=await route.fetch(),result=await response.json();
      if(result.status==='ok'){lostResponse=true;await route.abort('failed');}
      else await route.fulfill({response});
    });
    await row.getByRole('button',{name:'No change needed',exact:true}).click();
    await page.locator('#story-context-review-pane .context-review-error:not([hidden])').waitFor();
    assert(lostResponse,'Response loss must follow a committed review');
    await page.unroute('**/operations/context_review');
    assert.equal(await row.locator('textarea').inputValue(),'The task is already accounted for.');
    await row.getByRole('button',{name:'No change needed',exact:true}).click();
    await page.waitForFunction(()=>document.querySelectorAll('#story-context-review-pane .context-relation').length===2).catch(async e=>{console.error(await page.locator('#story-context-review-pane').innerText());throw e;});
    await page.locator('#story-context-review-pane .context-reviewed').check();
    await page.waitForFunction(()=>document.querySelectorAll('#story-context-review-pane .context-relation').length===3);
    row=page.locator('#story-context-review-pane .context-relation').filter({has:page.getByRole('button',{name:'Reopen',exact:true})});
    await row.getByRole('button',{name:'Review history',exact:true}).click();
    await row.locator('.context-review-history').getByText('The task is already accounted for.').waitFor();
    assert.equal(await row.locator('.context-review-history>div').count(),1);
    await row.locator('textarea').fill('Reconsider the next step.');
    await row.getByRole('button',{name:'Reopen',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#story-context-review-pane [data-result=pending]'));
    await page.locator('.story-details summary').click();
    await page.locator('#story-details .story-description-text').click();
    await page.getByRole('textbox',{name:'Story details',exact:true}).fill('Revised owner-authored details.');
    await page.locator('#story-details .story-description-save').click();
    await page.waitForFunction(()=>storyView.story.details==='Revised owner-authored details.');
    await page.evaluate(async()=>{
      const task=(await storyCall(state.biz,'tasks_create',{title:'Newly related context'})).task;
      await storyCall(state.biz,'relationships_create',{source:{type:'task',id:task.id},target:{type:'story',id:storyView.selected},role:'part_of'});
      document.activeElement?.blur();
      await storyRefresh(storyView,true);
    });
    await page.locator('#story-context-review-pane .context-source').filter({hasText:'Newly related context'}).waitFor();
    await page.screenshot({path:'/tmp/qorqut-context-review-desktop.png',fullPage:true});
    await page.evaluate(async()=>{ls('ctxmapmode','1');await goTab('context');});
    await page.locator('.ctxgraph-node').first().waitFor();
    const check=await page.locator('.ctxgraph-edge').evaluateAll(edges=>({paths:edges.map(e=>e.getAttribute('d')),arrows:edges.every(e=>!!e.getAttribute('marker-end'))}));
    assert(check.paths.length>=3);assert(check.arrows);assert(new Set(check.paths).size>=3);
    assert(await page.locator('.ctxgraph-node.story').count());
    await page.screenshot({path:'/tmp/qorqut-context-map-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.waitForTimeout(400);
    assert(await page.locator('.ctxgraph-node').count());
    await page.screenshot({path:'/tmp/qorqut-context-map-mobile.png',fullPage:true});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    assert.deepEqual(errors,[]);
    console.log('PASS shared reviews, explanation gating, history, reopen, Story Details, directional/parallel/self edges, desktop/mobile rendering');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
