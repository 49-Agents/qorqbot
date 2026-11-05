// Real dashboard + real disposable Host/Domain. No product response is intercepted or fabricated.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.CRM_COMPOSITION_BASE;
const fixture = JSON.parse(fs.readFileSync(process.env.CRM_COMPOSITION_FIXTURE, 'utf8'));
const shots = process.env.CRM_COMPOSITION_SHOTS;
const result = {records: []};

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  const page = await browser.newPage({viewport: {width: 1450, height: 1000}});
  const errors = [];
  page.setDefaultTimeout(20000);
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
  async function action(operation, click, expected = 'ok') {
    const response = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith('/operations/' + operation));
    await click();
    const body = await (await response).json();
    assert.equal(body.status, expected, operation + ': ' + JSON.stringify(body));
    return body;
  }
  async function read(biz, operation, args) {
    return page.evaluate(async ([biz, operation, args]) => {
      const route = biz ? '/api/biz/' + biz : '/api/root';
      const response = await fetch(route + '/operations/' + operation, {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify(biz ? args : {scope:'global', ...args})});
      const body = await response.json();
      if (body.status !== 'ok') throw new Error(operation + ': ' + JSON.stringify(body));
      return body.data;
    }, [biz, operation, args]);
  }
  try {
    for (const [biz, business, label] of [[null, null, 'Global'], ['harbor-light', fixture.projects.harbor, 'Project']]) {
      console.log('CRM browser: ' + label);
      await page.goto(origin + '/#/global/agents');
      await page.reload();
      await page.waitForFunction(() => typeof renderTables === 'function' && state.bizs?.length);
      await page.evaluate(biz => {
        state.scope = biz ? 'business' : 'global'; state.biz = biz;
        state.globalTab = 'tables'; state.tab = 'context'; state.ctx = '_tables';
        return biz ? renderContext(document.querySelector('#content')) : renderTables(document.querySelector('#content'));
      }, biz);
      await page.locator('#data-new').click();
      await page.locator('#data-title').fill(label + ' CRM browser');
      await page.locator('#data-kind').selectOption('crm');
      await page.locator('#data-stage-add').click();
      await page.locator('.data-stage [data-field=name]').fill('Contacted');
      await page.locator('#data-column-add').click();
      const local = page.locator('.data-column').last();
      await local.locator('[data-field=id]').fill('next_step');
      await local.locator('[data-field=name]').fill('Next step');
      const table = (await action('tables_create', () => page.locator('.modal .mfoot .primary').click())).data.table;
      assert.equal(table.table_kind, 'crm'); assert.equal(table.business_id, business);
      await page.locator('[data-crm-add]').click();
      await page.locator('.crm-add [name=title]').fill(label + ' sample contact');
      await page.locator('.crm-add [data-customer-profile=email]').fill(label.toLowerCase() + '@example.test');
      const added = (await action('crm_entries_add', () => page.locator('.modal .mfoot .primary').click())).data;
      const entry = added.entry;
      assert.equal(entry.record.task_type, 'customer'); assert.equal(entry.record_ref.business_id, business);
      const row = () => page.locator('.crm-grid tbody tr').filter({hasText: label + ' sample contact'});
      await row().waitFor();
      await row().locator('[data-cell-input=next_step]').fill('Review the brief');
      await row().locator('.crm-stage select').selectOption('contacted');
      const changed = (await action('crm_entries_update', () => page.locator('[data-crm-save]').click())).data;
      assert.equal(changed.entry.stage_id, 'contacted');
      assert.equal(changed.entry.cells.next_step, 'Review the brief');
      await page.locator('[data-crm-draft]').waitFor({state:'hidden'});
      const thread = await read(biz, 'tables_thread_get', {table_id: table.id});
      assert.ok(thread.messages.length >= 2);
      await page.locator('.crm-thread-message').first().waitFor();
      const tablePin = (await action('focus_present', () => page.locator('#crm-pin-table').click())).data.item;
      const recordPin = (await action('focus_present', () => row().getByRole('button', {name:'Pin ' + label + ' sample contact to Focus', exact:true}).click())).data.item;

      await row().locator('.crm-open').click();
      const activity = page.locator('.crm-record-host .rw-activity-section');
      await activity.locator('summary').first().click();
      await activity.locator('.crm-activity-item').first().waitFor();
      const recording = activity.locator('.crm-record-form');
      await recording.locator('summary').click();
      await recording.locator('[name=summary]').fill('Media kit received');
      await recording.locator('[name=body]').fill('Synthetic email observation from the browser.');
      await recording.locator('[name=occurred_at]').fill('2026-10-20T10:00');
      const observation = await action('crm_activity_record', () => recording.getByRole('button', {name:'Record',exact:true}).click(), biz ? 'ok' : 'error');
      if (biz) {
        assert.equal(observation.data.event.summary, 'Media kit received');
        await activity.locator('.crm-activity-item').filter({hasText:'Media kit received'}).waitFor();
      } else {
        assert.equal(observation.error.code, 'channel_required');
        assert.equal(await recording.locator('[name=body]').inputValue(), 'Synthetic email observation from the browser.');
      }
      // A fresh navigation discards the deliberately retained forbidden Global draft.
      await page.goto(origin + '/#/global/agents');
      await page.reload();
      const card = page.locator(`.agfocus-items [data-card-id="${tablePin.id}"]`);
      await card.waitFor();
      await card.locator('.agfocus-crm-toggle').click();
      const member = page.locator(`.agfocus-items [data-card-id^="${tablePin.id}/crm/"]`).filter({hasText:label + ' sample contact'});
      await member.waitFor();
      await member.locator('.ttl').click();
      const pane = page.locator('.agfocus-dock .rw-record:not([hidden])').first();
      await pane.locator('[data-rw-tab=dashboard]').click();
      await pane.locator('[data-customer-name]').waitFor();
      assert.equal(await pane.locator('[data-customer-name]').inputValue(), label + ' sample contact');
      await page.locator(`.agfocus-items [data-card-id="${recordPin.id}"]`).waitFor();
      if (shots) await page.screenshot({path:path.join(shots, label.toLowerCase() + '-crm-focus.png')});
      const original = (await read(biz, 'tasks_get', {task_id:entry.record_ref.id})).task;
      assert.equal(original.status, 'open');
      result.records.push({business_id:business, table_id:table.id, task_id:original.id, table_pin:tablePin.id, task_pin:recordPin.id});
    }
    assert.deepEqual(errors, []);
    fs.writeFileSync(process.env.CRM_COMPOSITION_RESULT, JSON.stringify(result));
    console.log('Real CRM browser flows passed: Global + Project, create/add/edit, discussion, Focus expansion, activity and owner guard.');
  } catch (error) {
    if (shots) await page.screenshot({path:path.join(shots,'failure.png')});
    throw error;
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode=1;});
