#!/usr/bin/env node
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const core = process.env.BAZARLYK_CORE_ROOT || [
  path.resolve(__dirname,'../../app'),
  path.resolve(__dirname,'../../qorqut-code/app'),
].find(candidate => fs.existsSync(path.join(candidate,'tests/calendar_browser_fixture.py')));
assert.ok(core, 'Set BAZARLYK_CORE_ROOT to the paired Qorqut 1.2 app/ directory');

(async () => {
  const child = spawn(process.env.TEST_PYTHON || 'python3',['tests/calendar_browser_fixture.py','--dashboard-root',path.resolve(__dirname,'..')],
    {cwd:core,env:{...process.env,PYTHONPATH:'.'},stdio:['ignore','pipe','pipe']});
  let browser, stderr = '';
  child.stderr.on('data',data => stderr += data);
  try {
    const {url} = await new Promise((resolve,reject) => {
      let stdout = '';
      const timer = setTimeout(() => reject(new Error('Fixture timed out: '+stderr)),60000);
      child.stdout.on('data',data => { stdout += data; const line = stdout.split('\n').find(s => s.startsWith('{"url":'));
        if (line) { clearTimeout(timer); resolve(JSON.parse(line)); } });
      child.on('exit',code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${stderr}`)); });
    });
    browser = await chromium.launch({headless:true,args:['--no-sandbox']});
    const page = await browser.newPage({viewport:{width:1440,height:1000},timezoneId:'America/New_York'});
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror',error => errors.push(error.message));
    await page.route('**/*',route => route.request().url().startsWith(url) ? route.continue() : route.abort());
    await page.goto(url+'/#/global/calendar');
    await page.getByRole('heading',{name:'Calendar',exact:true}).waitFor();
    await page.locator('.calendar-event',{hasText:'Personal appointment'}).waitFor();
    await page.locator('.calendar-event',{hasText:'Business invitation'}).waitFor();
    assert.equal(await page.locator('.calendar-day').count(),3);
    assert.equal(await page.locator('[data-calendar-today]').count(),0);
    assert.equal(await page.locator('.calendar-dayhead strong').count(),0);
    assert.doesNotMatch(await page.locator('.calendar-page').innerText(),/America\/New_York|Sample data/);
    assert.match(await page.locator('.calendar-clock').innerText(),/\d+:\d{2}/);
    const selectedDate = await page.locator('.calendar-day').first().getAttribute('data-calendar-day');
    const boxes = await page.locator('.calendar-event').evaluateAll(els => els.map(el => {
      const rect = el.getBoundingClientRect(); return {left:rect.left,right:rect.right,top:el.style.top,height:el.style.height};
    }));
    assert.equal(boxes[0].top,'128px'); assert.equal(boxes[0].height,'32px');
    assert.ok(boxes[0].right <= boxes[1].left || boxes[1].right <= boxes[0].left,'Simultaneous provider events need separate lanes');
    assert.equal(await page.locator('.calendar-event').first().evaluate(el => el.scrollHeight <= el.clientHeight),true);
    await page.getByRole('combobox',{name:'Calendar view',exact:true}).selectOption('7');
    await page.locator('.calendar-event',{hasText:'Personal appointment'}).waitFor();
    assert.equal(await page.locator('.calendar-day').count(),7);
    await page.getByRole('combobox',{name:'Calendar view',exact:true}).selectOption('1');
    await page.locator('.calendar-event',{hasText:'Personal appointment'}).waitFor();
    assert.equal(await page.locator('.calendar-range').innerText(),'Today');
    await page.getByRole('button',{name:'Next days',exact:true}).click();
    assert.equal(await page.locator('.calendar-range').innerText(),'Tomorrow');
    await page.getByRole('button',{name:'Previous days',exact:true}).click();
    await page.getByRole('button',{name:'Previous days',exact:true}).click();
    assert.equal(await page.locator('.calendar-range').innerText(),'Yesterday');
    await page.locator(`[data-calendar-date="${selectedDate}"]`).click();
    await page.getByRole('combobox',{name:'Calendar view',exact:true}).selectOption('3');
    await page.locator('.calendar-event',{hasText:'Personal appointment'}).waitFor();

    await page.getByRole('button',{name:'Add event',exact:true}).click();
    await page.getByLabel('Title',{exact:true}).fill('Review calendar tools');
    await page.getByLabel('Guests',{exact:true}).fill('guest@example.com');
    await page.getByLabel('Repeat',{exact:true}).selectOption('RRULE:FREQ=WEEKLY');
    await page.getByLabel('Reminder',{exact:true}).selectOption('10');
    await page.getByLabel('Add Google Meet link').check();
    // A failed write keeps the draft and retry key.
    const requests = [];
    await page.route('**/api/root/operations/calendar_event_create',async route => {
      requests.push(route.request().postDataJSON());
      if (requests.length === 1) return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({status:'error',error:{code:'provider_unavailable',message:'Please retry'}})});
      await route.continue();
    });
    await page.getByRole('button',{name:'Save',exact:true}).click();
    await page.getByRole('alert').filter({hasText:'Please retry'}).waitFor();
    assert.equal(await page.getByLabel('Title',{exact:true}).inputValue(),'Review calendar tools');
    await page.getByRole('button',{name:'Save',exact:true}).click();
    await page.locator('.calendar-event',{hasText:'Review calendar tools'}).waitFor();
    assert.equal(requests[0].idempotency_key,requests[1].idempotency_key);
    assert.equal(requests[1].google_meet,true);
    assert.deepEqual(requests[1].reminders,[10]);

    await page.locator('.calendar-event',{hasText:'Review calendar tools'}).click();
    await page.getByRole('link',{name:'Join Google Meet'}).waitFor();
    await page.getByRole('button',{name:'Edit',exact:true}).click();
    await page.getByLabel('Title',{exact:true}).fill('Updated calendar review');
    await page.getByRole('button',{name:'Save',exact:true}).click();
    await page.locator('.calendar-event',{hasText:'Updated calendar review'}).waitFor();

    await page.locator('.calendar-event',{hasText:'Business invitation'}).click();
    await page.getByRole('button',{name:'Accept',exact:true}).click();
    await page.locator('.calendar-event',{hasText:'Business invitation'}).click();
    await page.locator('.calendar-detail',{hasText:'accepted'}).waitFor();
    await page.locator('.mfoot').getByRole('button',{name:'Close',exact:true}).click();

    await page.locator('.calendar-event',{hasText:'Updated calendar review'}).click();
    await page.getByRole('button',{name:'Delete',exact:true}).click();
    await page.getByRole('button',{name:'Delete',exact:true}).click();
    await page.locator('.calendar-event',{hasText:'Updated calendar review'}).waitFor({state:'detached'});
    await page.getByRole('button',{name:'Next days',exact:true}).click();
    await page.getByRole('button',{name:'Previous days',exact:true}).click();
    await page.locator('.calendar-event',{hasText:'Personal appointment'}).waitFor();
    await page.locator('.calendar-page').evaluate(el => el.scrollTop = 0);
    await page.screenshot({path:'/tmp/ordubashy-calendar-desktop.png',fullPage:true,animations:'disabled'});
    await page.setViewportSize({width:640,height:844});
    await page.waitForFunction(() => document.querySelectorAll('.calendar-day').length === 1);
    assert.equal(await page.getByRole('combobox',{name:'Calendar view',exact:true}).count(),0);
    await page.setViewportSize({width:390,height:844});
    await page.waitForFunction(() => document.querySelectorAll('.calendar-day').length === 1);
    await page.locator('.calendar-event',{hasText:'Personal appointment'}).waitFor();
    assert.equal(await page.locator('.calendar-week').evaluate(el => el.scrollWidth <= el.clientWidth),true);
    await page.getByRole('button',{name:'Add event',exact:true}).click();
    await page.getByRole('combobox',{name:'Calendar',exact:true}).selectOption('1');
    assert.equal(await page.getByLabel('Add Google Meet link').isVisible(),false);
    await page.getByLabel('Invitation from').selectOption('owner@business.example');
    await page.getByLabel('Title',{exact:true}).fill('Business appointment');
    await page.screenshot({path:'/tmp/ordubashy-calendar-form.png',fullPage:true,animations:'disabled'});
    await page.getByRole('button',{name:'Save',exact:true}).click();
    await page.locator('.calendar-event',{hasText:'Business appointment'}).waitFor();
    await page.screenshot({path:'/tmp/ordubashy-calendar-mobile.png',fullPage:true,animations:'disabled'});
    // Imported titles are data even in the detail modal, not executable HTML.
    const unsafeTitle = '<img src=x onerror="window.calendarTitleExecuted=true">';
    await page.route('**/api/root/operations/calendar_event_get', async route => {
      const response = await route.fetch();
      const result = await response.json();
      result.data.event.title = unsafeTitle;
      await route.fulfill({response, json:result});
    });
    await page.locator('.calendar-event',{hasText:'Business appointment'}).click();
    await page.locator('.modal .mt').filter({hasText:unsafeTitle}).waitFor();
    assert.equal(await page.locator('.modal .mt img').count(),0);
    assert.equal(await page.evaluate(() => !!window.calendarTitleExecuted),false);
    await page.locator('.mfoot').getByRole('button',{name:'Close',exact:true}).click();

    // Rendering preserves all-day dates and actual early/late/overnight intervals.
    await page.setViewportSize({width:1440,height:1000});
    await page.waitForFunction(() => document.querySelectorAll('.calendar-day').length === 3);
    const intervals = await page.evaluate(() => {
      const first = new Date(document.querySelector('.calendar-day').dataset.calendarDay+'T00:00:00');
      const at = (offset,hour,minute=0) => {const date=calendarAddDays(first,offset);date.setHours(hour,minute,0,0);return date.toISOString();};
      return {early:[at(0,6,30),at(0,7,30)],late:[at(0,22,30),at(0,23,30)],overnight:[at(0,23,30),at(1,0,30)],allDay:[calendarDateString(first),calendarDateString(calendarAddDays(first,2))]};
    });
    await page.route('**/api/root/operations/calendar_events_list',async route => {
      const response=await route.fetch(),result=await response.json();
      if(route.request().postDataJSON().connection_id==='personal') {
        const template=result.data.events[0];
        result.data.events=Object.entries(intervals).map(([id,[start,end]])=>({...template,id,title:id,start,end,status:'confirmed'}));
        result.data.next_cursor=null;
      }
      await route.fulfill({response,json:result});
    });
    await page.getByRole('button',{name:'Refresh calendars',exact:true}).click();
    await page.locator('.calendar-event',{hasText:'early'}).waitFor();
    const early=page.locator('.calendar-event',{hasText:'early'});
    assert.equal(await early.evaluate(el=>el.style.top),'416px');
    assert.equal(await early.evaluate(el=>el.style.height),'64px');
    assert.equal(await page.locator('.calendar-event',{hasText:'late'}).evaluate(el=>el.style.height),'64px');
    assert.equal(await page.locator('.calendar-event',{hasText:'overnight'}).count(),2);
    assert.deepEqual(await page.locator('.calendar-event',{hasText:'overnight'}).evaluateAll(els=>els.map(el=>el.style.height)),['32px','32px']);
    assert.equal(await page.locator('.calendar-all-day-event',{hasText:'allDay'}).count(),2);
    assert.equal(await page.locator('.calendar-day').first().evaluate(el=>el.getBoundingClientRect().height),1536);
    assert.deepEqual(errors,[]);
    console.log('Calendar browser check passed: time grid, overlap lanes, day/week navigation, local clock, all-day/overnight/early/late events, create/edit/delete, RSVP, retry, recurrence/reminders/Meet and mobile.');
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null) await new Promise(resolve => { child.once('exit',resolve); child.kill('SIGINT'); });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
