// Owner action capture and Actions list, isolated: stubbed operations, fixed clock, Tokyo browser zone.
//   PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_actions_capture_browser.js
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
// 2026-03-02 03:30 UTC is 2026-03-01 23:30 in San Juan and 12:30 on 2026-03-02 in Tokyo.
const NOW = Date.UTC(2026, 2, 2, 3, 30);
const SCRIPTS = ['core', 'san-juan-time', 'task-snooze', 'tags', 'work-groups', 'task-state', 'tasks', 'actions', 'task-progress'];

const page_html = owner => `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/tasks.css"><link rel="stylesheet" href="/css/agentpane.css"><link rel="stylesheet" href="/css/actions.css">
<div id="taskspanel" class="tasks" style="position:static;transform:none;opacity:1;visibility:visible;width:600px"></div><div id="agpad" class="agpad" style="width:600px"></div><button id="wsbtn" hidden></button><button id="elsewhere">Elsewhere</button>
<script>
window.calls = []; window.holds = []; window.failNext = null; window.serial = 0;
const reply = (data, status = 200) => new Response(JSON.stringify(status < 400 ? {status:'ok', data} : {status:'error', error:data}), {status, headers:{'Content-Type':'application/json'}});
const store = {};
window.fetch = async (url, options = {}) => {
  url = String(url);
  if (url.endsWith('/api/biz/two/task/t-9')) { window.calls.push({op:'task:two/t-9'}); return new Response(JSON.stringify({task:{id:'t-9', title:'Ship two'}}), {headers:{'Content-Type':'application/json'}}); }
  if (url.includes('/api/biz/')) { window.calls.push({op:'task:' + url}); return new Response('{}', {status:404}); }
  if (url.endsWith('/api/session')) return reply({principal_kind:'human', is_owner:${owner}, principal_id:'owner-1', display_name:'Owner'});
  const op = url.split('/operations/')[1], body = JSON.parse(options.body || '{}');
  window.calls.push({op, body});
  if (window.failNext && op === window.failNext.op) { const failure = window.failNext; window.failNext = null; return reply(failure.error, failure.status); }
  if (window.holdNext === op) { window.holdNext = null; await new Promise(resolve => window.holds.push(resolve)); }
  const view = a => ({action_id:a.id, text:a.text, category:a.category, businesses:a.business_ids.map(id => ({id, name:id === 'b1' ? 'One' : 'Two'})),
    performed_by:{id:'owner-1', name:'Owner'}, recorded_by:{id:'owner-1', name:'Owner'}, attribution:'performed', origin:'manual',
    occurred:a.occurred, recorded_at:'2026-03-02T03:30:00Z', revision:a.revision, can_change:true, tags:[], withdrawn:false, refs:{}, detail:''});
  const occurred = rt => !rt ? {date:'2026-03-01', hour:23, precision:'instant', time_zone:'America/Puerto_Rico', instant:'2026-03-02T03:30:00Z'}
    : {date:rt.slice(0, 10), hour:rt.length > 10 ? Number(rt.slice(11)) : null, precision:rt.length > 10 ? 'hour' : 'day', time_zone:'America/Puerto_Rico'};
  if (op === 'tags_list') return reply({tags:[{id:'tg1', label:'launch', created_at:'2026-01-01T00:00:00Z'}]});
  if (op === 'actions_config_get') return reply({scope:{kind:'owner', businesses:[{id:'b1', name:'One'}, {id:'b2', name:'Two'}]}, timezone:'America/Puerto_Rico'});
  if (op === 'actions_record') { const a = {id:'global/a' + (++window.serial), text:body.text, category:body.category || null, business_ids:body.business_ids || [], occurred:occurred(body.real_time), revision:1}; store[a.id] = a; return reply({action:view(a)}); }
  if (op === 'actions_correct') {
    const a = store[body.action_id];
    if (body.expected_revision !== a.revision) return reply({code:'revision_conflict', message:'Revision changed'}, 409);
    if ('text' in body) a.text = body.text; if ('category' in body) { a.category = body.category; a.business_ids = body.business_ids; }
    if ('real_time' in body) a.occurred = occurred(body.real_time);
    a.revision++; return reply({action:view(a)});
  }
  if (op === 'actions_get') return reply({action:view(store[body.action_id])});
  if (op === 'actions_list') return reply({actions:Object.values(store).reverse().map(view).concat([{action_id:'business/b1/legacy', text:'Legacy entry', category:'business-operational',
    businesses:[{id:'b1', name:'One'}], performed_by:null, recorded_by:{id:'agent-1', name:'Onbashy'}, attribution:'recorded', origin:'legacy',
    occurred:{date:'2026-02-27', hour:null, precision:'day', time_zone:'America/Puerto_Rico'}, revision:1, can_change:false, tags:[], withdrawn:false, refs:{url:'https://example.test/pr'}, detail:'Kept detail'},
    ...[['Global task result', null, 'gt-1'], ['Two task result', 'b2', 't-9'], ['Withheld task result', undefined, 'hidden-task']].map(([text, storage, task]) => ({action_id:storage ? 'business/' + storage + '/' + task : 'global/' + task,
      text, category:'business-operational', businesses:[{id:'b1', name:'One'}], performed_by:{id:'agent-1', name:'Onbashy'}, recorded_by:{id:'agent-1', name:'Onbashy'}, attribution:'performed', origin:'agent',
      occurred:{date:'2026-02-26', hour:null, precision:'day', time_zone:'America/Puerto_Rico'}, revision:1, can_change:false, tags:[], withdrawn:false,
      refs:storage === undefined ? {} : {task}, detail:'', legacy:storage === undefined ? null : {business_id:storage, id:task}}))]), next_cursor:null});
  return reply({}, 404);
};
</script>${SCRIPTS.map(name => `<script src="/js/${name}.js"></script>`).join('')}
<script>
state.scope = 'global'; state.bizs = [{slug:'one', id:'b1', name:'One'}, {slug:'two', id:'b2', name:'Two'}];
</script>`;

async function open(browser, owner = true) {
  const context = await browser.newContext({timezoneId:'Asia/Tokyo', viewport:{width:1100, height:900}});
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(e.message + ' ' + e.stack));
  await page.clock.install({time:NOW});
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.hostname !== 'actions.test') return route.abort();
    if (url.pathname === '/') return route.fulfill({contentType:'text/html', body:page_html(owner)});
    const file = path.join(root, url.pathname);
    return fs.existsSync(file) ? route.fulfill({path:file}) : route.fulfill({status:404, body:''});
  });
  await page.goto('http://actions.test/');
  await page.evaluate(async () => {
    await loadSession();
    const panel = document.querySelector('#taskspanel'); panel.innerHTML = tHead({view:'tasks'}); tWireHead(panel);
    const pane = document.querySelector('#agpad'); pane.innerHTML = tHead({view:'tasks', views:['status', 'tasks', 'activity'], bodyId:'agpad-body', taskTools:false}); tWireHead(pane, null, () => {});
  });
  return {page, errors, context};
}
// The retired pending-classification helper note, including its alternate saved-business wording.
const noteText = /choose at least one business|classify this as business-operational|until then it is saved|saved businesses stay/i;
const ops = page => page.evaluate(() => calls.map(c => c.op));
const lastCall = (page, op) => page.evaluate(op => calls.filter(c => c.op === op).at(-1)?.body, op);

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  try {
    // Non-owners never see the button.
    {
      const {page, context} = await open(browser, false);
      assert.equal(await page.locator('[data-ac-capture]:visible').count(), 0);
      await context.close();
    }
    // Business chips wrap in a naturally growing box: no height cap or scrolling, always placed in view.
    {
      const {page, errors, context} = await open(browser);
      await page.getByRole('button', {name:'Record completed action'}).first().click();
      const pop = page.getByRole('dialog', {name:'Completed action'});
      await pop.waitFor();
      await page.waitForFunction(() => ac.config);
      await pop.locator('[data-ac-category="business-operational"]').click();
      const metrics = () => page.evaluate(() => {
        const rect = el => el.getBoundingClientRect(), pop = document.querySelector('#ac-pop'), fs = pop.querySelector('.ac-businesses');
        const chips = [...fs.querySelectorAll('.ac-biz')], style = getComputedStyle(fs), f = rect(fs), p = rect(pop);
        return {overflowX:style.overflowX, overflowY:style.overflowY, maxHeight:style.maxHeight, fieldsetHeight:f.height, scrollExcess:fs.scrollHeight - fs.clientHeight,
          chips:chips.length, rows:new Set(chips.map(c => Math.round(rect(c).top))).size, spillRight:Math.max(...chips.map(c => rect(c).right - f.right)),
          spillBelow:Math.max(...chips.map(c => rect(c).bottom - f.bottom)), longest:Math.max(...chips.map(c => rect(c).height)),
          top:p.top, bottom:p.bottom, left:p.left, right:p.right, natural:rect(ac.opener).top - p.height - 6, innerHeight, innerWidth,
          pageOverflow:document.documentElement.scrollWidth - innerWidth};
      });
      const placed = m => assert.ok(Math.abs(m.top - Math.max(8, Math.min(m.natural, m.innerHeight - (m.bottom - m.top) - 8))) <= 1, `placed in view ${JSON.stringify(m)}`);
      const grown = m => {
        assert.equal(m.overflowY, 'visible'); assert.equal(m.overflowX, 'visible'); assert.equal(m.maxHeight, 'none');
        assert.ok(m.scrollExcess <= 1 && m.spillBelow <= 0.5 && m.spillRight <= 0.5, `no scroll or clipping ${JSON.stringify(m)}`);
      };
      const load = count => page.evaluate(count => {
        ac.config = {scope:{kind:'owner', businesses:Array.from({length:count}, (_, i) => ({id:'bx' + i, name:i === 4 ? 'Extraordinarily' + 'Long'.repeat(20) + 'BusinessName' : i % 3 ? 'Business ' + i : 'A much longer business number ' + i}))}};
        acPaint();
      }, count);
      await load(30);
      let m = await metrics();
      grown(m); placed(m);
      assert.ok(m.fieldsetHeight > 160, `grows past the old 160px cap: ${m.fieldsetHeight}`);
      assert.ok(m.rows > 1 && m.rows < m.chips, `chips wrap ${m.rows} rows for ${m.chips} chips`);
      assert.ok(m.longest > 40, 'a name longer than the width wraps inside its chip');
      assert.equal(await pop.locator('.ac-businesses input, .ac-businesses label').count(), 0, 'no checkbox rows');
      // A panel that no longer fits above its opener stays at the top margin after resizing.
      const height = m.bottom - m.top;
      await page.setViewportSize({width:1100, height:Math.ceil(height) + 30});
      await page.waitForFunction(() => Math.abs(document.querySelector('#ac-pop').getBoundingClientRect().top - 8) <= 1);
      m = await metrics();
      grown(m); placed(m);
      assert.ok(m.bottom <= m.innerHeight - 8 + 1 && m.top >= 8, `shifted up: ${JSON.stringify(m)}`);
      await load(36);
      m = await metrics();
      grown(m); placed(m);
      assert.ok(m.top >= 8, `config load repositions: ${JSON.stringify(m)}`);
      // No helper note, with no business available or none chosen; toggling a business keeps the panel placed.
      await load(0);
      assert.equal(await page.locator('.ac-pending').count(), 0);
      assert.doesNotMatch(await pop.innerText(), noteText);
      await load(30);
      assert.equal(await page.locator('.ac-pending').count(), 0);
      assert.doesNotMatch(await pop.innerText(), noteText);
      assert.ok(await page.evaluate(() => acEffective().pending), 'business scope without a business is still pending');
      await pop.getByRole('button', {name:'Business 1', exact:true}).click();
      assert.ok(!(await page.evaluate(() => acEffective().pending)));
      m = await metrics();
      grown(m); placed(m);
      await pop.getByRole('button', {name:'Business 1', exact:true}).click();
      assert.ok(await page.evaluate(() => acEffective().pending));
      m = await metrics();
      grown(m); placed(m);
      assert.equal(await page.locator('.ac-pending').count(), 0);
      assert.doesNotMatch(await pop.innerText(), noteText);
      // Taller than the window: at the top margin, chips not clipped or scroll-capped.
      await page.setViewportSize({width:1100, height:Math.floor(height) - 120});
      await page.waitForFunction(() => Math.abs(document.querySelector('#ac-pop').getBoundingClientRect().top - 8) <= 1);
      m = await metrics();
      grown(m); placed(m);
      assert.ok(m.bottom > m.innerHeight, 'not capped to the window');
      // Narrow window: no horizontal overflow and the long name still wraps inside the panel.
      await page.setViewportSize({width:320, height:700});
      await page.waitForFunction(() => document.querySelector('#ac-pop').getBoundingClientRect().right <= innerWidth - 8 + 1);
      m = await metrics();
      grown(m); placed(m);
      assert.ok(m.pageOverflow <= 0 && m.left >= 0 && m.right <= m.innerWidth, `no horizontal overflow ${JSON.stringify(m)}`);
      assert.ok(m.longest > 40, 'the long name wraps at 320px');
      assert.deepEqual(errors, []);
      await context.close();
    }
    const {page, errors} = await open(browser);
    const buttons = page.getByRole('button', {name:'Record completed action'});
    assert.equal(await buttons.count(), 1, 'one global capture button');
    assert.equal(await page.locator('.thead [data-ac-capture]').count(), 0);
    const floating = await buttons.evaluate(el => {
      const r = el.getBoundingClientRect();
      return {right:innerWidth - r.right, bottom:innerHeight - r.bottom,
        position:getComputedStyle(el).position, z:getComputedStyle(el).zIndex,
        onTop:document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2).closest('[data-ac-capture]') === el};
    });
    assert.deepEqual(floating, {right:12, bottom:12, position:'fixed', z:'2147483646', onTop:true});
    await buttons.first().click();
    const pop = page.getByRole('dialog', {name:'Completed action'});
    await pop.waitFor();
    // Scope is three icon-only buttons; the category name lives in the accessible name and the title.
    assert.deepEqual(await pop.locator('.ac-scope').evaluateAll(els => els.map(el => [el.dataset.acCategory, el.textContent.trim(), el.getAttribute('aria-label'), el.title, el.querySelectorAll('svg').length, el.offsetWidth === el.offsetHeight])),
      ['global-systems', 'global-operational', 'business-operational'].map(id => [id, '', `Scope: ${id}`, id, 1, true]));
    assert.equal(await pop.getByRole('button', {name:'Scope: business-operational'}).count(), 1);
    assert.equal(await pop.locator('.ac-scope[aria-pressed="true"]').count(), 0, 'Unclassified by default');
    assert.doesNotMatch(await pop.innerText(), /real time|san juan|global-|business-operational/i, 'no visible scope words or time-zone labels');
    assert.equal(await pop.getByLabel('Action date').count(), 1);
    assert.equal(await pop.getByLabel('Hour').count(), 1);
    assert.equal(await page.locator('#ac-date').getAttribute('max'), '2026-03-01', 'San Juan today, not Tokyo');
    assert.ok(!(await page.locator('#ac-save').isVisible()));
    const text = page.locator('#ac-text');
    await text.fill('Signed the supplier contract');
    assert.ok(await page.locator('#ac-save').isVisible(), 'Save appears on a dirty edit');
    // Blur saves; with no category it is Unclassified.
    await page.locator('.ac-scope').first().focus();
    await page.getByText(/^Saved/).waitFor();
    assert.deepEqual(await lastCall(page, 'actions_record'), {text:'Signed the supplier contract', idempotency_key:(await lastCall(page, 'actions_record')).idempotency_key});
    assert.ok(await page.locator('#ac-new').isVisible());

    // Business category without a business stays pending and keeps the saved classification.
    await page.locator('[data-ac-category="business-operational"]').click();
    assert.deepEqual(await page.evaluate(() => { const e = acEffective(); return [e.pending, e.category, e.business_ids]; }), [true, null, []], 'pending keeps the saved Unclassified');
    assert.equal(await page.locator('.ac-pending').count(), 0);
    assert.doesNotMatch(await pop.innerText(), noteText);
    await text.fill('Signed the supplier contract for both');
    // Exactly ten idle seconds: nothing at 9.9 s, a save at 10 s.
    await page.clock.runFor(9900);
    assert.equal((await ops(page)).filter(op => op === 'actions_correct').length, 0);
    await page.clock.runFor(100);
    await page.getByText(/^Saved/).waitFor();
    let update = await lastCall(page, 'actions_correct');
    assert.equal(update.text, 'Signed the supplier contract for both');
    assert.ok(!('category' in update), 'pending business choice is not saved');
    assert.equal(update.expected_revision, 1);
    // Businesses are named toggle buttons (multiple allowed); click, Enter and Space all toggle.
    const chosen = () => pop.locator('button[data-ac-business][aria-pressed="true"]').evaluateAll(els => els.map(el => el.dataset.acBusiness));
    const business = name => pop.getByRole('button', {name, exact:true});
    assert.deepEqual(await pop.locator('button[data-ac-business]').allTextContents(), ['One', 'Two']);
    assert.equal(await pop.locator('.ac-businesses input').count(), 0, 'no checkbox glyphs');
    await business('Two').click();
    assert.deepEqual(await chosen(), ['b2']);
    assert.ok(!(await page.evaluate(() => acEffective().pending)));
    await business('Two').click();
    assert.deepEqual(await chosen(), []);
    assert.ok(await page.evaluate(() => acEffective().pending), 'no business selected is pending again');
    assert.equal(await page.locator('.ac-pending').count(), 0);
    assert.doesNotMatch(await pop.innerText(), noteText);
    await business('Two').focus();
    await page.keyboard.press('Space');
    assert.deepEqual(await chosen(), ['b2']);
    await page.keyboard.press('Enter');
    assert.deepEqual(await chosen(), []);
    await page.keyboard.press('Enter');
    assert.deepEqual(await chosen(), ['b2']);
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.acBusiness), 'b2', 'focus stays on the toggled business');
    await business('One').click();
    assert.deepEqual(await chosen(), ['b1', 'b2']);
    await page.locator('#ac-save').click();
    await page.getByText(/^Saved/).waitFor();
    update = await lastCall(page, 'actions_correct');
    assert.deepEqual([update.category, update.business_ids, update.expected_revision], ['business-operational', ['b1', 'b2'], 2]);
    assert.deepEqual(await chosen(), ['b1', 'b2'], 'selection survives the save repaint');

    // Backdating in San Juan time, with a business deselected in the same save.
    await business('One').click();
    await page.locator('#ac-date').fill('2026-02-28');
    await page.locator('#ac-hour').selectOption('13');
    await page.locator('#ac-save').click();
    await page.getByText(/^Saved/).waitFor();
    update = await lastCall(page, 'actions_correct');
    assert.deepEqual([update.real_time, update.business_ids], ['2026-02-28T13', ['b2']]);
    assert.deepEqual(await chosen(), ['b2']);

    // In-flight and later edits: serialized, newest text kept, next save carries the returned revision.
    await page.evaluate(() => { window.holdNext = 'actions_correct'; });
    await text.fill('Version A');
    await page.locator('#ac-save').click();
    await page.getByText('Saving…', {exact:true}).waitFor();
    await text.fill('Version B');
    await page.locator('.ac-scope').first().focus();                // blur while A is in flight
    assert.equal((await ops(page)).filter(op => op === 'actions_correct').length, 4, 'B waits for A');
    await page.evaluate(() => window.holds.shift()());
    await page.waitForFunction(() => calls.filter(c => c.op === 'actions_correct').length === 5);
    await page.getByText(/^Saved/).waitFor();
    assert.equal(await text.inputValue(), 'Version B', 'the stale response did not replace newer text');
    update = await lastCall(page, 'actions_correct');
    assert.deepEqual([update.text, update.expected_revision], ['Version B', 5]);

    // Uncertain failure: the exact request is retried with the same idempotency key before newer edits.
    await page.evaluate(() => { window.failNext = {op:'actions_correct', status:503, error:{code:'unavailable', message:'Down'}}; });
    await text.fill('Version C');
    await page.locator('#ac-save').click();
    await page.locator('.ac-status[data-state="error"]').waitFor();
    assert.equal(await text.inputValue(), 'Version C');
    const failed = await lastCall(page, 'actions_correct');
    await text.fill('Version D');
    await page.locator('#ac-save').click();
    await page.waitForFunction(() => calls.filter(c => c.op === 'actions_correct').length === 8);
    const updates = await page.evaluate(() => calls.filter(c => c.op === 'actions_correct').map(c => c.body));
    assert.deepEqual(updates[6], failed, 'retry is byte-identical');
    assert.equal(updates[7].text, 'Version D');
    await page.getByText(/^Saved/).waitFor();

    // Conflict keeps the draft and waits for an explicit Save.
    await page.evaluate(() => { window.failNext = {op:'actions_correct', status:409, error:{code:'revision_conflict', message:'Changed'}}; });
    await text.fill('Version E');
    await page.locator('#ac-save').click();
    await page.locator('.ac-status[data-state="conflict"]').waitFor();
    assert.equal(await text.inputValue(), 'Version E');
    const before = (await ops(page)).length;
    await page.clock.runFor(11000);
    assert.equal((await ops(page)).length, before, 'no silent overwrite after a conflict');
    await page.locator('#ac-save').click();
    await page.getByText(/^Saved/).waitFor();
    assert.equal((await lastCall(page, 'actions_correct')).text, 'Version E');

    // Esc closes, keeps the draft and restores focus; reopening shows it.
    await text.fill('Version F');
    await text.press('Escape');
    assert.equal(await page.locator('#ac-pop').count(), 0);
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.acCapture !== undefined), true);
    await page.getByText(/^Saved/).count();
    await page.waitForFunction(() => calls.filter(c => c.op === 'actions_correct').at(-1).body.text === 'Version F');
    await buttons.first().click();
    assert.equal(await text.inputValue(), 'Version F');
    await page.getByText(/^Saved/).waitFor();

    // New action starts a separate record.
    await page.locator('#ac-new').click();
    assert.equal(await text.inputValue(), '');
    await text.fill('Called the bank');
    await page.locator('#elsewhere').click();                        // outside click closes and saves
    await page.waitForFunction(() => calls.filter(c => c.op === 'actions_record').length === 2);
    const records = await page.evaluate(() => calls.filter(c => c.op === 'actions_record').map(c => c.body));
    assert.notEqual(records[0].idempotency_key, records[1].idempotency_key);

    // Actions list in Global Activity: all categories, San Juan occurrence, legacy details, reopen for edit.
    await page.evaluate(() => { const panel = document.querySelector('#taskspanel'); panel.innerHTML = tHead({view:'activity'}); tWireHead(panel); tView = 'activity'; tActivityRefresh(); });
    await page.getByText('Legacy entry', {exact:true}).waitFor();
    assert.match(await page.locator('.ac-row').filter({hasText:'Legacy entry'}).textContent(), /time unknown.*Recorded by Onbashy/s);
    assert.match(await page.locator('.ac-row').filter({hasText:'Version F'}).textContent(), /Yesterday · 13:00/, 'San Juan yesterday, although Tokyo is already 2 March');
    assert.doesNotMatch(await page.locator('.ac-row-meta').first().innerText(), /Revision|manual|legacy|\bagent\b/, 'no revision or raw origin in row metadata');
    await page.locator('.ac-row').filter({hasText:'Legacy entry'}).locator('summary').first().click();
    assert.ok(await page.getByText('Kept detail').isVisible());
    await page.locator('.ac-row').filter({hasText:'Version F'}).getByRole('button', {name:'Correct action'}).click();
    assert.equal(await text.inputValue(), 'Version F');
    assert.equal(await page.locator('#ac-date').inputValue(), '2026-02-28');

    // A failed save, closing, then Edit on the same (stale) row keeps the correction and its exact retry.
    await page.evaluate(() => { window.failNext = {op:'actions_correct', status:503, error:{code:'unavailable', message:'Down'}}; });
    await text.fill('Corrected F');
    await page.locator('#ac-save').click();
    await page.locator('.ac-status[data-state="error"]').waitFor();
    const correction = await lastCall(page, 'actions_correct');
    await page.evaluate(() => { window.failNext = {op:'actions_correct', status:503, error:{code:'unavailable', message:'Down'}}; });
    await text.press('Escape');                                     // closing retries once and fails again
    await page.waitForFunction(() => !window.failNext && !ac.inflight && ac.status === 'error');
    await page.locator('.ac-row').filter({hasText:'Version F'}).getByRole('button', {name:'Correct action'}).click();
    assert.equal(await text.inputValue(), 'Corrected F', 'reopening the same action keeps the unsaved correction');
    assert.equal(await page.locator('.ac-status').getAttribute('data-state'), 'error');
    const recordsBefore = (await ops(page)).filter(op => op === 'actions_record').length;
    await page.locator('#ac-save').click();
    await page.getByText(/^Saved/).waitFor();
    const retried = await page.evaluate(() => calls.filter(c => c.op === 'actions_correct').slice(-3).map(c => c.body));
    assert.deepEqual(retried, [correction, correction, correction], 'every retry is the identical request');
    assert.equal((await ops(page)).filter(op => op === 'actions_record').length, recordsBefore, 'no duplicate record');
    assert.equal(await text.inputValue(), 'Corrected F');

    // A save completing while the owner types mid-text keeps the caret where it was.
    await text.fill('Alpha Omega');
    await page.evaluate(() => { window.holdNext = 'actions_correct'; });
    const updatesBefore = (await ops(page)).filter(op => op === 'actions_correct').length;
    await text.press('Control+Enter');
    await page.getByText('Saving…', {exact:true}).waitFor();
    await text.evaluate(el => el.setSelectionRange(6, 6));
    await page.keyboard.type('Beta ');
    await page.evaluate(() => window.holds.shift()());
    await page.waitForFunction(() => !ac.inflight);
    assert.equal((await ops(page)).filter(op => op === 'actions_correct').length, updatesBefore + 1, 'the repaint did not save the in-progress edit');
    await page.getByText('Unsaved changes', {exact:true}).waitFor();
    assert.deepEqual(await text.evaluate(el => [el.value, el.selectionStart, el.selectionEnd, document.activeElement === el]), ['Alpha Beta Omega', 11, 11, true]);
    await page.keyboard.type('X');
    assert.equal(await text.inputValue(), 'Alpha Beta XOmega');
    await page.locator('#ac-save').click();
    await page.getByText(/^Saved/).waitFor();
    await text.press('Escape');

    // Task references open in the action's storage workspace, not the business that is open now.
    await page.evaluate(() => {
      window.opened = [];
      window.globalTasksMount = (host, options) => opened.push(['global', options.initialTaskId]);
      window.tDetail = task => opened.push(['business', task.id]);
      state.biz = 'one';
    });
    const taskButton = name => page.locator('.ac-row').filter({hasText:name}).getByRole('button', {name:'Open related task'});
    assert.equal(await taskButton('Withheld task result').count(), 0, 'withheld evidence offers no task');
    await taskButton('Global task result').click();
    await page.waitForFunction(() => opened.length === 1);
    await page.keyboard.press('Escape');
    await taskButton('Two task result').click();
    await page.waitForFunction(() => opened.length === 2);
    assert.deepEqual(await page.evaluate(() => opened), [['global', 'gt-1'], ['business', 't-9']]);
    assert.deepEqual((await ops(page)).filter(op => op.startsWith('task:')), ['task:two/t-9'], 'never the current business');

    const used = new Set((await ops(page)).filter(op => !op.startsWith('task:')));
    assert.deepEqual([...used].sort(), ['actions_config_get', 'actions_correct', 'actions_get', 'actions_list', 'actions_record'], 'no classifier call');
    assert.deepEqual(errors, []);
    console.log('actions capture browser checks passed');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
