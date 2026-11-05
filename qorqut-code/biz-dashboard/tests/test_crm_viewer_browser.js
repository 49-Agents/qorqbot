// Standalone CRM viewer (/tables/view) and the one-time canonical record route (/?open_task=...). Links are built from the
// entry's record_ref with an explicit scope, open the original Task/Story/Customer/Email in the shared record workspace and
// make no write or Focus card. Malformed, mixed, unknown and denied targets never fall through. Synthetic data only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {startCrmServer} = require('./support/crm_server');
const shots = process.env.CRM_SHOTS || null;
if (shots) fs.mkdirSync(shots, {recursive: true});

const ALPHA = '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const server = startCrmServer(), api = server.api;
const cols = [{id: 'record_name', name: 'Name', type: 'text', record_field: 'title'}, {id: 'note', name: 'Note', type: 'text'}];
const gcrm = api.addTable('global', {title: 'Viewer CRM <i>g</i>', table_kind: 'crm', crm_config: {stages: [{id: 'lead', name: 'Lead <b>L</b>'}]}, columns: cols});
const acrm = api.addTable('alpha', {title: 'Alpha viewer CRM', table_kind: 'crm', crm_config: {stages: []}, columns: cols});
const gen = api.addTable('global', {title: 'Plain viewer table', table_kind: 'general', columns: [{id: 'name', name: 'Name', type: 'text'}]});
const specs = [['t-plain', 'task', 'Global plain task'], ['s-story', 'story', 'Global story'], ['c-cust', 'customer', 'Global customer'], ['e-mail', 'email', 'Global email']];
for (const [id, task_type, title] of specs) api.addEntry(gcrm.id, api.addTask({id, task_type, title: title + ' <script>x</script>', email_conversation_id: task_type === 'email' ? 'conv' : undefined}), {stage_id: 'lead', cells: {note: '<img src=x>'}});
api.addEntry(gcrm.id, api.addTask({id: 'same', title: 'Same id GLOBAL'}));
api.addEntry(acrm.id, api.addTask({id: 'same', business_id: ALPHA, title: 'Same id ALPHA'}));
api.addTask({id: 'odd id/with space&more', title: 'Odd id task'});
api.addEntry(gcrm.id, api.task(null, 'odd id/with space&more'));
const hiddenTask = api.addTask({id: 'gone', title: 'Hidden title'});
api.addEntry(gcrm.id, hiddenTask); api.flags.hidden.add('global:gone');

const until = async (check, what) => { for (let i = 0; i < 280; i++) { if (await check()) return; await new Promise(r => setTimeout(r, 25)); } throw new Error('Timed out waiting for ' + what); };
const writes = () => server.requests.filter(r => /focus_present|focus_set_archived|crm_entries_(add|update|remove)|tasks_(update|create)|tables_(update|comment)|crm_activity_record/.test(r.path));

(async () => {
  const origin = await server.listen();
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  try {
    const errors = [];
    const fresh = async () => {
      const page = await browser.newPage({viewport: {width: 1300, height: 900}});
      page.setDefaultTimeout(12000);
      page.on('pageerror', e => errors.push(e.message));
      await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
      await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
      return page;
    };

    // ---- Standalone viewer: Global CRM.
    let page = await fresh();
    await page.goto(`${origin}/tables/view?table_id=${gcrm.id}`);
    await page.locator('table.tv-grid tbody tr').first().waitFor();
    const heads = await page.locator('thead th').allTextContents();
    assert.deepEqual(heads, ['Record', 'Name', 'Note', 'Stage', 'Task state']);
    assert.equal(await page.locator('tbody script, tbody img, tbody b, tbody i').count(), 0, 'values are text');
    const link = page.locator('tbody tr', {hasText: 'Global story'}).locator('a');
    assert.equal(await link.getAttribute('href'), '/?open_task=s-story&scope=global#/global/agents');
    assert.equal(await link.textContent(), 'Global story <script>x</script>');
    assert.match(await page.locator('tbody tr').first().locator('td').nth(3).textContent(), /Lead <b>L<\/b>/);
    // Odd ids are encoded by URLSearchParams and round-trip.
    const odd = await page.locator('tbody tr', {hasText: 'Odd id task'}).locator('a').getAttribute('href');
    assert.equal(new URL(odd, origin).searchParams.get('open_task'), 'odd id/with space&more');
    // Unavailable rows: plain text, no link, no title.
    const unavailable = page.locator('tbody tr', {hasText: 'Unavailable record'});
    assert.equal(await unavailable.count(), 1);
    assert.equal(await unavailable.locator('a').count(), 0);
    assert.equal(await page.getByText('Hidden title').count(), 0);
    if (shots) await page.screenshot({path: shots + '/crm-viewer-global.png'});

    // ---- A link opens the original record, in the shared workspace, with no write and no Focus card.
    const open = async (page, href) => { await page.goto(origin + href); await page.locator('.rw-shell, .rw-modal, [data-rw-record], .agfocus-dock').first().waitFor(); };
    for (const [id, type, title] of specs) {
      server.requests.length = 0;
      await page.goto(origin + `/?open_task=${id}&scope=global#/global/agents`);
      await until(async () => (await page.locator('.rw-title, .rw-head').allTextContents()).join(' ').includes(title), 'record ' + id);
      assert.equal(writes().length, 0, `${type} open makes no write`);
      assert.equal(server.focusPresents.length, 0);
      assert.equal(new URL(page.url()).search, '', 'the route is consumed once');
      assert.ok(server.requests.some(r => r.path.endsWith('/operations/tasks_get') && r.body.task_id === id));
      assert.equal(await page.getByRole('tab', {name: 'Chat'}).count() > 0, true, 'ordinary Chat tab remains');
    }
    if (shots) await page.screenshot({path: shots + '/crm-viewer-open-record.png'});
    assert.equal(server.board.items.length, 0, 'no Focus card created');

    // ---- Same id, distinct scopes.
    const aview = await fresh();
    await aview.goto(`${origin}/tables/view?table_id=${acrm.id}&business_id=${ALPHA}`);
    await aview.locator('tbody tr a').first().waitFor();
    const ahref = await aview.locator('tbody tr a').first().getAttribute('href');
    assert.equal(ahref, `/?open_task=same&business_id=${ALPHA}#/global/agents`);
    server.requests.length = 0;
    await aview.goto(origin + ahref);
    await until(async () => (await aview.locator('.rw-title, .rw-head').allTextContents()).join(' ').includes('Same id ALPHA'), 'alpha record');
    assert.ok(server.requests.some(r => r.path.includes('/biz/alpha/operations/tasks_get')));
    assert.equal(await aview.locator('.rw-record').getByText('Same id GLOBAL').count(), 0, 'the record workspace shows only the Alpha Task');
    assert.equal(writes().length, 0);
    await page.goto(origin + '/?open_task=same&scope=global#/global/agents');
    await until(async () => (await page.locator('.rw-title, .rw-head').allTextContents()).join(' ').includes('Same id GLOBAL'), 'global record');
    assert.equal(await page.locator('.rw-record').getByText('Same id ALPHA').count(), 0);

    // ---- Invalid routes never read and never fall through.
    const bad = [
      '/?open_task=&scope=global', '/?open_task=a&open_task=b&scope=global', '/?open_task=same', '/?open_task=same&scope=global&business_id=' + ALPHA,
      '/?open_task=same&scope=project', '/?open_task=same&business_id=not-a-uuid', '/?open_task=same&scope=global&extra=1',
      '/?open_task=' + 'x'.repeat(201) + '&scope=global', '/?open_task=same&scope=global&focus_item=card-x',
      '/?open_task=same&business_id=22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
    ];
    for (const href of bad) {
      const p = await fresh();
      server.requests.length = 0;
      await p.goto(origin + href + '#/global/agents');
      await until(async () => (await p.locator('.agfocus-error, [role=alert]').allTextContents()).join('').length > 0, 'error for ' + href);
      assert.equal(server.requests.filter(r => r.path.endsWith('/operations/tasks_get')).length, 0, 'no record read for ' + href);
      assert.equal(writes().length, 0);
      assert.equal(await p.locator('.rw-record, .agfocus-dock').getByText('Same id GLOBAL').count() + await p.locator('.rw-record, .agfocus-dock').getByText('Same id ALPHA').count(), 0);
      await p.close();
    }

    // ---- Denied / missing: plain unavailable state, no cached identity.
    for (const [href, setup] of [['/?open_task=missing&scope=global', () => {}], ['/?open_task=same&scope=global', () => { server.flags.denyScope = 'global'; }]]) {
      const p = await fresh(); setup();
      await p.goto(origin + href + '#/global/agents');
      await until(async () => (await p.locator('.agfocus-error, [role=alert]').allTextContents()).join('').includes('unavailable'), 'unavailable ' + href);
      assert.equal(await p.locator('.rw-record, .agfocus-dock').getByText('Same id GLOBAL').count(), 0);
      assert.equal(await p.getByText('missing').count(), 0, 'the requested id is not echoed');
      server.flags.denyScope = null; await p.close();
    }

    // ---- Existing focus_item routes still work.
    const task = api.task(null, 't-plain'); server.pin({type: 'task', id: task.id, business_id: null});
    const card = server.board.items.find(i => i.source.id === 't-plain');
    const fp = await fresh();
    await fp.goto(`${origin}/?focus_item=${card.id}#/global/agents`);
    await until(async () => (await fp.locator('.rw-title, .rw-head').allTextContents()).join(' ').includes('Global plain task'), 'focus_item still opens');

    // ---- An ordinary table has no record column; safeLinkUrl stays strict.
    const gp = await fresh();
    await gp.goto(`${origin}/tables/view?table_id=${gen.id}`);
    await gp.locator('#tv-status').filter({hasText: 'no rows'}).waitFor();
    assert.deepEqual(await gp.evaluate(() => [...document.querySelectorAll('thead th')].map(th => th.textContent)), ['Name']);
    const safe = await gp.evaluate(() => [
      safeLinkUrl('/?open_task=a&scope=global#/global/agents'), safeLinkUrl('/?open_task=a&scope=global'), safeLinkUrl('/?open_task=a&scope=global#/x'),
      safeLinkUrl('//evil.example/?open_task=a&scope=global#/global/agents'), safeLinkUrl('/?other=1#/global/agents'), safeLinkUrl('javascript:alert(1)')]);
    assert.deepEqual(safe, ['/?open_task=a&scope=global#/global/agents', null, null, null, null, null]);

    assert.deepEqual(errors, []);
  } finally { await browser.close(); await server.close(); }
  console.log('CRM viewer: scoped record links, one-time canonical open without writes, same ids in distinct scopes, rejected/denied routes, focus_item compatibility and safe rendering passed');
})().catch(error => { console.error(error); process.exit(1); });
