// Record activity and the Table team thread in the shared record workspace: paged activity, safe recorded email/message
// rendering, the explicit idempotent record form (it never sends or verifies), team-thread failure/retry/follow/draft
// retention. Real app components against the synthetic CRM Operations; no live data or provider.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {startCrmServer} = require('./support/crm_server');
const shots = process.env.CRM_SHOTS || null;
if (shots) fs.mkdirSync(shots, {recursive: true});

const server = startCrmServer(), api = server.api;
server.actAs('member');   // an authorized non-owner member: the installation owner reads the team thread but writes through the private channel
const crm = api.addTable('global', {title: 'Thread CRM', table_kind: 'crm', crm_config: {stages: []}, columns: [{id: 'record_name', name: 'Name', type: 'text', record_field: 'title'}]});
const mail = api.addTask({id: 'mail-1', task_type: 'email', title: 'Recorded email record', email_conversation_id: 'conv-1'});
api.addEntry(crm.id, mail);
api.activity.set('global:mail-1', Array.from({length: 60}, (_, n) => ({id: 'a' + String(n).padStart(3, '0'), kind: 'email', occurred_at: new Date(Date.UTC(2026, 9, 1, 0, n)).toISOString(), recorded_at: '2026-10-28T09:00:00Z',
  actor: {id: 'ordu', display_name: 'Ordubashy'}, summary: n === 59 ? 'Newest <script>alert(1)</script>' : 'Message ' + n, body: n === 59 ? '<img src=x onerror=alert(1)> hello' : '',
  source: n === 59 ? {type: 'email', label: 'Mailbox <b>name</b>', url: 'javascript:alert(1)'} : {type: 'email', label: 'Mail ' + n, url: 'https://example.invalid/m/' + n}, origin: 'recorded'})));

const until = async (check, what) => { for (let i = 0; i < 280; i++) { if (await check()) return; await new Promise(r => setTimeout(r, 25)); } throw new Error('Timed out waiting for ' + what); };

(async () => {
  const origin = await server.listen();
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1300, height: 1000}});
    page.setDefaultTimeout(12000);
    const errors = [], dialogs = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('dialog', d => { dialogs.push(d.message()); d.dismiss(); });
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
    await page.goto(origin + '/?open_task=mail-1&scope=global#/global/agents');
    const section = page.locator('.rw-activity-section');
    await section.waitFor();
    assert.equal(api.log.filter(e => e.op === 'crm_activity_list').length, 0, 'activity loads only when opened');
    await section.locator('summary').first().click();
    await until(async () => await section.locator('.crm-activity-item').count() === 50, 'first activity page');
    assert.equal(await section.locator('.crm-activity-item').first().getAttribute('data-activity-id'), 'a059');

    // ---- Recorded text is inert; an unsafe source URL is not a link; wording does not imply sending/verification.
    assert.equal(await section.locator('script, img').count(), 0);
    assert.equal(await section.locator('.crm-activity-item').first().locator('a').count(), 0, 'javascript: source is not linked');
    assert.equal(await section.locator('.crm-activity-item').first().locator('.crm-activity-source').textContent(), 'Source: Mailbox <b>name</b>');
    assert.match(await section.locator('.crm-activity-meta').first().textContent(), /not sent from here and is not provider-verified/);
    const safeLink = section.locator('.crm-activity-item').nth(1).locator('a');
    assert.equal(await safeLink.getAttribute('rel'), 'noopener noreferrer');
    assert.equal(await section.getByText(/mailbox connected|verified by|sent to/i).count(), 0);
    await section.getByRole('button', {name: 'Older activity'}).click();
    await until(async () => await section.locator('.crm-activity-item').count() === 60, 'older activity');
    assert.equal(await section.locator('[data-activity-id="a059"]').count(), 1, 'no duplicates');
    assert.equal(await section.getByRole('button', {name: 'Older activity'}).isHidden(), true);
    if (shots) await page.screenshot({path: shots + '/crm-record-activity.png'});

    // ---- The record form: validation, idempotent replay and honest disclaimer.
    const rec = section.locator('.crm-record-form');
    await rec.locator('summary').click();
    assert.match(await rec.locator('form > p.muted').first().textContent(), /does not send mail, connect a mailbox or verify a provider/);
    // The text is required (the Domain descriptor's BODY has minLength 1): an empty body is refused before any request.
    await rec.locator('[name=summary]').fill('Customer replied');
    await rec.locator('[name=occurred_at]').fill('2026-10-27T10:00');
    await rec.getByRole('button', {name: 'Record'}).click();
    assert.equal(await rec.locator('[name=body]').evaluate(node => node.validity.valueMissing), true, 'the empty text is flagged as missing');
    assert.equal(api.log.filter(e => e.op === 'crm_activity_record').length, 0, 'an empty body sends nothing');
    await rec.locator('[name=body]').fill('Thanks, the quote works for us.');
    // A server-side validation refusal (blank summary) keeps every field.
    await rec.locator('[name=summary]').fill('   ');
    await rec.getByRole('button', {name: 'Record'}).click();
    await rec.locator('.ferr').filter({hasText: 'server refused'}).waitFor();
    assert.equal(await rec.locator('[name=body]').inputValue(), 'Thanks, the quote works for us.', 'the draft survives a validation refusal');
    assert.equal((api.activity.get('global:mail-1') || []).length, 60, 'nothing was recorded by the refused entry');
    api.log.length = 0;
    await rec.locator('[name=summary]').fill('Customer replied');
    await rec.locator('[name=url]').fill('javascript:alert(1)');
    await rec.getByRole('button', {name: 'Record'}).click();
    assert.match(await rec.locator('.ferr').textContent(), /http or https/);
    assert.equal(api.log.filter(e => e.op === 'crm_activity_record').length, 0, 'nothing written for a refused URL');
    await rec.locator('[name=url]').fill('https://example.invalid/thread/9');
    await rec.locator('[name=event_key]').fill('import-1');
    await rec.getByRole('button', {name: 'Record'}).click();
    await rec.locator('[role=status]').filter({hasText: 'Recorded.'}).waitFor();
    const recorded = api.log.filter(e => e.op === 'crm_activity_record');
    assert.equal(recorded.length, 1);
    assert.equal(recorded[0].args.event_key, 'import-1');
    assert.equal(recorded[0].args.source.url, 'https://example.invalid/thread/9');
    // Exact retry (same key, same normalized payload) is a duplicate; changed content under the same key conflicts and keeps the draft.
    const fillSame = async url => {
      await rec.locator('[name=summary]').fill('Customer replied');
      await rec.locator('[name=body]').fill('Thanks, the quote works for us.');
      await rec.locator('[name=occurred_at]').fill('2026-10-27T10:00');
      await rec.locator('[name=url]').fill(url);
      await rec.locator('[name=event_key]').fill('import-1');
      await rec.getByRole('button', {name: 'Record'}).click();
    };
    await fillSame('https://example.invalid/thread/9');
    await rec.locator('[role=status]').filter({hasText: 'Already recorded.'}).waitFor();
    assert.equal((api.activity.get('global:mail-1') || []).filter(a => a._event_key === 'import-1').length, 1, 'exact retry stays one record');
    await fillSame('https://example.invalid/thread/OTHER');
    await rec.locator('.ferr').filter({hasText: 'different content'}).waitFor();
    assert.equal(await rec.locator('[name=summary]').inputValue(), 'Customer replied', 'the conflicting draft is retained');
    assert.equal((api.activity.get('global:mail-1') || []).filter(a => a._event_key === 'import-1').length, 1, 'a conflict records nothing');
    await rec.locator('[name=summary]').fill('');
    await rec.locator('[name=url]').fill('');
    await rec.locator('[name=event_key]').fill('');

    // ---- Unavailable activity: no stale items, ordinary message.
    await rec.locator('[name=summary]').fill('Unsent private note');
    server.flags.denyScope = 'global';
    await section.getByRole('button', {name: 'Refresh activity'}).click();
    await until(async () => /unavailable/.test(await section.locator('.crm-activity-status').textContent()), 'denied activity');
    assert.equal(await section.locator('.crm-activity-item').count(), 0);
    assert.equal(await rec.isHidden(), true, 'the record form is gone with the activity');
    assert.equal(await rec.locator('[name=summary]').inputValue(), '', 'its draft text is cleared, not merely hidden');
    server.flags.denyScope = null;

    // ---- Table team thread: failure/retry, comment with a stable key, follow state, draft retention.
    const host = await page.evaluate(() => { const el = document.createElement('div'); el.id = 'thread-host'; el.style.cssText = 'position:fixed;inset:0;background:#fff;z-index:99999;overflow:auto'; document.body.append(el); return true; });
    api.flags.failThreadOnce = true;
    await page.evaluate(id => { window.__thread = crmThreadMount(document.getElementById('thread-host'), {biz: null, table: {id, columns: []}, navigate: null}); }, crm.id);
    const retry = page.getByRole('button', {name: 'Retry loading discussion'});
    await retry.waitFor();
    await retry.click();
    await page.locator('.crm-thread-message, .crm-thread li').first().waitFor().catch(() => {});
    const field = page.locator('#thread-host textarea');
    await field.fill('Hello <b>team</b>');
    await page.evaluate(() => { window.__thread.dispose(); });
    await page.evaluate(id => { window.__thread = crmThreadMount(document.getElementById('thread-host'), {biz: null, table: {id, columns: []}, navigate: null}); }, crm.id);
    assert.equal(await page.locator('#thread-host textarea').inputValue(), 'Hello <b>team</b>', 'the unsent draft is retained');
    await page.locator('#thread-host form button[type=submit], #thread-host form button:has-text("Send")').first().click();
    await until(async () => api.log.some(e => e.op === 'tables_comment'), 'comment sent');
    const comment = api.log.find(e => e.op === 'tables_comment');
    assert.equal(comment.args.text, 'Hello <b>team</b>');
    assert.ok(comment.args.idempotency_key);
    await until(async () => await page.locator('#thread-host .crm-thread-body').filter({hasText: 'Hello <b>team</b>'}).count() === 1, 'comment shown as text');
    assert.equal(await page.locator('#thread-host b').count(), 0);
    const follow = page.locator('#thread-host').getByRole('button', {name: /Follow/});
    assert.equal(await follow.getAttribute('aria-pressed'), 'true');
    await follow.click();
    await until(async () => api.follows.get(crm.id) === false, 'unfollowed');
    await until(async () => await follow.getAttribute('aria-pressed') === 'false', 'follow state repainted');
    assert.equal(server.privateSends.length, 0, 'team thread never writes the private conversation');
    assert.deepEqual(dialogs, []);
    assert.deepEqual(errors, []);
    if (shots) await page.screenshot({path: shots + '/crm-team-thread.png'});
  } finally { await browser.close(); await server.close(); }
  console.log('CRM record: paged activity, inert recorded email/message text, honest idempotent record form, unavailable activity and team-thread retry/draft/follow passed');
})().catch(error => { console.error(error); process.exit(1); });
