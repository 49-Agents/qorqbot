// Lost access, pending writes and actor authority in the CRM Table view, the team discussion and Task activity. Revocation
// (first page or a later page) drops every cached entry, cell, draft and editor; ordinary failures keep drafts; edits made
// across a write's completion boundary are never erased; the installation owner reads the team thread but cannot post to it.
const assert = require('node:assert/strict');
const {startCrmServer} = require('./support/crm_server');
const {launch, until} = require('./support/crm_page');

const server = startCrmServer(), api = server.api;
server.actAs('member');
const crm = api.addTable('global', {title: 'Revocable CRM', table_kind: 'crm', crm_config: {stages: [{id: 'lead', name: 'Lead'}]},
  columns: [{id: 'record_name', name: 'Name', type: 'text', record_field: 'title'}, {id: 'note', name: 'Note', type: 'text'}]});
for (let n = 0; n < 55; n++) api.addEntry(crm.id, api.addTask({id: 'r-' + n, title: 'Secret record ' + n, task_type: 'customer'}), {cells: {note: 'private note ' + n}});

(async () => {
  const {browser, page, errors} = await launch(server);
  try {
    const open = async () => {
      await page.evaluate(() => { state.scope = 'global'; state.biz = null; state.globalTab = 'tables'; return renderTables(document.querySelector('#content')); });
      await page.locator('.data-item', {hasText: 'Revocable CRM'}).click();
      await until(async () => await page.locator('.crm-grid tbody tr').count() === 50, 'first page');
    };
    const noDisclosure = async what => {
      assert.equal(await page.locator('.crm-grid').count(), 0, what + ': no grid');
      const text = await page.evaluate(() => document.body.innerText + [...document.querySelectorAll('input,textarea')].map(n => n.value).join(' '));
      assert.ok(!/Secret record|private note|typed secret/.test(text), what + ': no stale disclosure');
      assert.equal(await page.evaluate(() => _dataDrafts.size), 0, what + ': drafts dropped');
      assert.match(await page.locator('.crm-unavailable-state').textContent(), /This table is unavailable/);
      assert.equal(await page.locator('.crm-team textarea, .crm-thread-compose').count(), 0, what + ': no active editor');
    };

    // ---- Revocation on a later page.
    await open();
    await page.locator('textarea[data-cell-input=note]').first().fill('typed secret');
    assert.equal(await page.evaluate(() => _dataDrafts.size), 1);
    server.flags.denyScope = 'global';
    await page.locator('.crm-grid').locator('xpath=..').locator('xpath=..').getByRole('button', {name: 'Load more'}).evaluate(el => { el.disabled = false; el.click(); });
    await page.locator('.crm-unavailable-state').waitFor();
    await noDisclosure('later page');
    assert.match(await page.locator('.data-heading h1').textContent(), /Table unavailable/);
    // Recovery: nothing stale comes back; the owner re-reads from the authorized source.
    server.flags.denyScope = null;
    await page.getByRole('button', {name: 'Check access again'}).click();
    await until(async () => await page.locator('.crm-grid tbody tr').count() === 50, 'recovered');
    assert.equal(await page.locator('textarea[data-cell-input=note]').first().inputValue(), 'private note 0', 'the revoked edit did not come back');

    // ---- Revocation on a first-page refresh (record closed / workspace shown again).
    await page.evaluate(() => { window.__tableRefresh = _crmMount.refresh(); });
    server.flags.denyScope = 'global';
    await page.evaluate(() => _crmMount.refresh());
    await page.locator('.crm-unavailable-state').waitFor();
    await noDisclosure('first page');
    server.flags.denyScope = null;

    // ---- An ordinary failure keeps what is shown and every draft.
    await page.getByRole('button', {name: 'Check access again'}).click();
    await until(async () => await page.locator('.crm-grid tbody tr').count() === 50, 'recovered again');
    await page.locator('textarea[data-cell-input=note]').first().fill('kept edit');
    server.failing.add('crm_entries_list');
    await page.evaluate(() => _crmMount.refresh());
    await page.locator('[data-crm-error]').filter({hasText: /load/i}).waitFor();
    assert.equal(await page.locator('textarea[data-cell-input=note]').first().inputValue(), 'kept edit');
    assert.equal(await page.evaluate(() => _dataDrafts.size), 1, 'a network failure keeps the draft');
    server.failing.delete('crm_entries_list');

    // ---- Edits made while a write is pending are frozen out, and the earlier save cannot erase later work.
    const hold = server.hold('crm_entries_update');
    await page.getByRole('button', {name: 'Save'}).first().click();
    await hold.startedPromise;
    assert.equal(await page.locator('textarea[data-cell-input=note]').first().isDisabled(), true, 'submitted inputs are frozen');
    assert.equal(await page.locator('select[aria-label^="Stage for"]').first().isDisabled(), true, 'stage editors are frozen too');
    hold.release();
    await until(async () => await page.evaluate(() => _dataDrafts.size) === 0, 'save finished');
    assert.equal(await page.locator('textarea[data-cell-input=note]').first().inputValue(), 'kept edit');
    assert.equal(await page.locator('textarea[data-cell-input=note]').first().isDisabled(), false, 'editable again after the save');
    await page.locator('textarea[data-cell-input=note]').first().fill('later work');
    assert.equal(await page.evaluate(() => _dataDrafts.size), 1, 'a new edit after completion becomes a new draft');

    // ---- The team composer is frozen while sending and keeps text on failure.
    const thread = page.locator('.crm-team');
    const compose = thread.locator('.crm-thread-compose textarea');
    await compose.fill('first message');
    const sendHold = server.hold('tables_comment');
    await thread.getByRole('button', {name: 'Send to team'}).click();
    await sendHold.startedPromise;
    assert.equal(await compose.evaluate(el => el.readOnly), true, 'composer is frozen while the send is pending');
    sendHold.release();
    await until(async () => await compose.inputValue() === '', 'sent and cleared');
    await compose.fill('retry me');
    server.failing.add('tables_comment');
    await thread.getByRole('button', {name: 'Send to team'}).click();
    await thread.locator('.ferr').waitFor();
    assert.equal(await compose.inputValue(), 'retry me', 'a failed send keeps the draft');
    server.failing.delete('tables_comment');
    // Revocation of the discussion removes the draft and the editor.
    server.denied.add('tables_comment');
    await thread.getByRole('button', {name: 'Send to team'}).click();
    await thread.getByText('This discussion is unavailable.').waitFor();
    assert.equal(await thread.locator('textarea').evaluate(el => !el.offsetParent && el.value === ''), true, 'no visible private draft or active editor');
    server.denied.delete('tables_comment');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }

  // ---- The installation owner: read-only team discussion, no automatic relay.
  const comments = () => api.threads.get(crm.id).filter(m => m.kind === 'comment').length;
  const before = comments();
  const owner = startCrmServer({api});
  owner.actAs('owner');
  const second = await launch(owner);
  const showTable = async p => {
    await p.evaluate(() => { state.scope = 'global'; state.biz = null; state.globalTab = 'tables'; return renderTables(document.querySelector('#content')); });
    await p.locator('.data-item', {hasText: 'Revocable CRM'}).click();
  };
  try {
    await showTable(second.page);
    await second.page.locator('.crm-thread-owner').waitFor();
    assert.equal(await second.page.locator('.crm-thread-compose').isHidden(), true, 'the owner has no team composer');
    assert.match(await second.page.locator('.crm-thread-owner').textContent(), /private conversation/);
    assert.equal(comments(), before, 'the owner attempted no team comment');
    assert.equal(owner.privateSends.length, 0, 'nothing is relayed to the private channel');
  } finally { await second.browser.close(); await owner.close(); }

  // ---- A refused post (the backend still answers channel_required) claims nothing and keeps the draft.
  const split = startCrmServer({api});
  split.actAs('member');
  api.actor = {id: 'owner', installationOwner: true};
  const third = await launch(split);
  try {
    await showTable(third.page);
    const compose = third.page.locator('.crm-thread-compose textarea');
    await compose.fill('Stay in the draft');
    await third.page.locator('.crm-thread-compose').getByRole('button', {name: 'Send to team'}).click();
    await third.page.locator('.crm-thread .ferr').filter({hasText: 'cannot post to the team discussion'}).waitFor();
    assert.equal(await compose.inputValue(), 'Stay in the draft', 'the draft is neither discarded nor moved');
    assert.equal(comments(), before, 'no team comment was recorded');
    assert.equal(split.privateSends.length, 0, 'nothing was relayed to the private channel');
    // Task activity honors the actor's authority: the owner cannot record on a Global Task.
    const record = await third.page.evaluate(async () => { try { await crmCall(null, 'crm_activity_record', {task_id: 'r-0', kind: 'email', summary: 's', body: '', occurred_at: new Date().toISOString(), source: {kind: 'email'}, event_key: 'k'}); return 'ok'; } catch (e) { return e.code; } });
    assert.equal(record, 'channel_required', 'the Global installation owner follows the Task-comment channel restriction');
  } finally { await third.browser.close(); await split.close(); await server.close(); }
  console.log('CRM revocation: cleared entries/cells/drafts/editors on first/later page loss with recovery, preserved drafts on network errors, frozen pending edits, composer freeze, owner read-only team thread passed');
})().catch(error => { console.error(error); process.exit(1); });
