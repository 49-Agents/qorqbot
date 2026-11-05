// Native Table and CRM cards on Focus: paginated member expansion, scoped identities, race guards, canonical navigation,
// independent pinning/archive lifecycle and the separate team vs private conversations. Real Focus, record workspace and
// Task components against the synthetic CRM Operations; no live data, providers or agent sessions.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {startCrmServer} = require('./support/crm_server');
const shots = process.env.CRM_SHOTS || null;
if (shots) fs.mkdirSync(shots, {recursive: true});

const server = startCrmServer(), api = server.api;
server.actAs('member');   // an authorized non-owner member: the installation owner reads the team thread but writes through the private channel
const crm = api.addTable('global', {title: 'Influencer CRM <b>x</b>', table_kind: 'crm', crm_config: {stages: [{id: 'lead', name: 'Lead'}, {id: 'won', name: 'Won'}]},
  columns: [{id: 'record_name', name: 'Name', type: 'text', record_field: 'title'}]});
const partners = api.addTable('global', {title: 'Partner CRM', table_kind: 'crm', crm_config: {stages: []}, columns: [{id: 'record_name', name: 'Name', type: 'text', record_field: 'title'}]});
const empty = api.addTable('global', {title: 'Empty CRM', table_kind: 'crm', crm_config: {stages: []}, columns: [{id: 'record_name', name: 'Name', type: 'text', record_field: 'title'}]});
const inventory = api.addTable('global', {title: 'Ordinary inventory', table_kind: 'general', row_count: 3});
const alphaCrm = api.addTable('alpha', {title: 'Alpha CRM', table_kind: 'crm', crm_config: {stages: []}, columns: [{id: 'record_name', name: 'Name', type: 'text', record_field: 'title'}]});

// 130 members across three pages. Position 0 is a Story that also belongs to the Partner CRM.
const shared = api.addTask({id: 'shared-story', title: 'Shared outreach story', task_type: 'story'});
api.addTask({id: 'shared-child', title: 'Draft the shared outreach note', parent_id: 'shared-story'});
api.addTask({id: 'story-b', title: 'Cycle B story', task_type: 'story'});
api.addTask({id: 'plain', title: 'Plain member task', task_type: 'task'});
api.addTask({id: 'cust', title: 'Customer member', task_type: 'customer', customer_state: 'Prospect'});
api.addTask({id: 'mail', title: 'Historical email member', task_type: 'email', email_conversation_id: 'conv'});
api.addTask({id: 'hidden', title: 'Hidden record title', task_type: 'task'});
api.addTask({id: 'alpha-task', business_id: '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', title: 'Alpha scoped task', task_type: 'task'});
api.addTask({id: 'plain', business_id: '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', title: 'Plain twin in Alpha', task_type: 'task'});
const members = [shared, 'story-b', 'plain', 'cust', 'mail', 'hidden'].map(item => typeof item === 'string' ? api.task(null, item) : item);
for (const task of members) api.addEntry(crm.id, task, task.id === 'shared-story' ? {stage_id: 'lead'} : {});
for (let n = 0; n < 124; n++) api.addEntry(crm.id, api.addTask({id: 'bulk-' + n, title: 'Bulk member ' + n}));
api.addEntry(partners.id, shared, {stage_id: null});
api.addEntry(alphaCrm.id, api.task('11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'alpha-task'));
api.addEntry(alphaCrm.id, api.task('11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'plain'));
api.flags.hidden.add('global:hidden');
server.edges.push(['shared-story', 'story-b'], ['story-b', 'shared-story']);   // a two-Task concerns cycle
for (const table of [crm, partners, empty, inventory, alphaCrm]) server.pin({type: 'table', id: table.id, business_id: table.business_id});

const until = async (check, what) => { for (let i = 0; i < 280; i++) { if (await check()) return; await new Promise(r => setTimeout(r, 25)); } throw new Error('Timed out waiting for ' + what); };
let page;
const topEntries = cardId => page.evaluate(id => [...document.querySelectorAll(`.agfocus-items [data-card-id^="card-${id}/crm/"]`)]
  .filter(row => /^[^/]+\/crm\/[^/]+\/[^/]+$/.test(row.dataset.cardId) && row.offsetParent !== null).map(row => row.dataset.cardId), cardId);
const listOps = () => api.log.filter(entry => entry.op === 'crm_entries_list');

(async () => {
  const origin = await server.listen();
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  try {
    page = await browser.newPage({viewport: {width: 1400, height: 1000}});
    const errors = [];
    page.setDefaultTimeout(12000);
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
    await page.goto(origin + '/#/global/agents');
    const card = id => page.locator(`.agfocus-items [data-card-id="card-${id}"]`);
    await card(crm.id).waitFor();
    const entryRows = cardId => page.locator(`.agfocus-items [data-card-id^="card-${cardId}/crm/"]:visible`);

    // ---- Native Table cards: CRM and ordinary Tables are present sources; only the CRM expands.
    assert.equal(await card(crm.id).locator('.ttl').textContent(), 'Influencer CRM <b>x</b>', 'names are text');
    assert.equal(await page.locator('.agfocus-items b').count(), 0);
    assert.equal(await card(crm.id).locator('.agfocus-flag').textContent(), 'CRM');
    assert.equal(await card(inventory.id).locator('.agfocus-flag').textContent(), 'Table');
    assert.equal(await card(inventory.id).locator('.agfocus-crm-toggle').count(), 0, 'an ordinary Table has no operational children');
    assert.equal(await card(crm.id).locator('.agfocus-crm-toggle').getAttribute('aria-expanded'), 'false');

    // ---- Paginated expansion: 130 members over 50-row pages, scoped, in membership order, no duplicates.
    await card(crm.id).locator('.agfocus-crm-toggle').click();
    await until(async () => (await topEntries(crm.id)).length === 50, 'first page');
    assert.deepEqual(listOps().at(-1).args, {table_id: crm.id, include_archived: true, limit: 50, scope: 'global'});
    const firstRow = entryRows(crm.id).first();
    assert.match(await firstRow.getAttribute('data-card-id'), new RegExp(`^card-${crm.id}/crm/table:global:${crm.id}/[0-9a-f-]{36}$`), 'row ids carry the scoped Table and membership id');
    assert.equal(await firstRow.locator('.ttl').textContent(), 'Shared outreach story');
    assert.match(await firstRow.locator('.agfocus-flag').textContent(), /Stage: Lead/, 'CRM stage shows beside Task state');
    assert.equal(await page.locator(`[data-card-id="card-${crm.id}"] .agfocus-crm-toggle`).getAttribute('aria-expanded'), 'true');
    assert.equal(await entryRows(crm.id).locator('[data-focus-snooze]:visible').count(), 0, 'entry rows are not Focus cards');
    await card(crm.id).getByRole('button', {name: 'More entries'}).click();
    await until(async () => (await topEntries(crm.id)).length === 100, 'second page');
    assert.equal(listOps().at(-1).args.cursor.startsWith('c:'), true);
    await card(crm.id).getByRole('button', {name: 'More entries'}).click();
    await until(async () => (await topEntries(crm.id)).length === 130, 'third page');
    assert.equal(await card(crm.id).getByRole('button', {name: 'More entries'}).count(), 0, 'no more pages');
    const ids = await topEntries(crm.id);
    assert.equal(new Set(ids).size, ids.length, 'no duplicate siblings');
    assert.equal(ids.length, 130, 'all members are reachable: 130 rows');
    assert.ok(await entryRows(crm.id).filter({hasText: 'Bulk member 123'}).count(), 'a member beyond 100 is reachable');
    assert.deepEqual(await entryRows(crm.id).evaluateAll(rows => rows.slice(0, 3).map(row => row.querySelector('.ttl').textContent)), ['Shared outreach story', 'Cycle B story', 'Plain member task']);
    if (shots) await page.screenshot({path: path.join(shots, 'focus-crm-expanded.png')});

    // Unavailable member: no title, no scope, no cached values.
    const hiddenRow = page.locator(`[data-card-id^="card-${crm.id}/crm/"].agfocus-unavailable:visible`);
    assert.equal(await hiddenRow.count(), 1);
    assert.equal(await hiddenRow.locator('.ttl').textContent(), 'This item is no longer available.');
    assert.equal((await page.content()).includes('Hidden record title'), false);

    // ---- Same Task in two CRMs: independent path expansion (scoped Table + membership in the branch key).
    await card(partners.id).locator('.agfocus-crm-toggle').click();
    await until(async () => (await topEntries(partners.id)).length === 1, 'partner entry');
    const inCrm = entryRows(crm.id).first(), inPartners = entryRows(partners.id).first();
    await inCrm.locator('.task-subtasks-toggle').click();
    await until(async () => (await page.locator(`[data-card-id^="${await inCrm.getAttribute('data-card-id')}/"]:visible`).count()) >= 2, 'children of the story in the first CRM');
    assert.equal(await inPartners.locator('.task-subtasks-toggle').getAttribute('aria-expanded'), 'false', 'the same Task in another CRM stays collapsed');
    const crmChildren = await page.locator(`[data-card-id^="${await inCrm.getAttribute('data-card-id')}/task/"]`).evaluateAll(rows => rows.map(row => [row.dataset.cardId, row.querySelector('.ttl').textContent]));
    assert.deepEqual(crmChildren.map(row => row[1]), ['Draft the shared outreach note', 'Cycle B story']);
    // Path-cycle guard: Story A concerns B and B concerns A. B's child list never repeats A.
    const bRow = page.locator(`[data-card-id="${crmChildren[1][0]}"]`);
    await bRow.locator('.task-subtasks-toggle').click();
    await until(async () => (await bRow.locator('.task-subtasks-toggle').getAttribute('aria-expanded')) === 'true', 'B expanded');
    await page.waitForTimeout(300);
    assert.deepEqual(await page.locator(`[data-card-id^="${crmChildren[1][0]}/"]:visible`).evaluateAll(rows => rows.map(row => row.dataset.cardId)), [], 'the cycle back to the ancestor is not rendered');
    await inPartners.locator('.task-subtasks-toggle').click();
    await until(async () => (await page.locator(`[data-card-id^="${await inPartners.getAttribute('data-card-id')}/task/"]:visible`).count()) >= 2, 'children in the second CRM');
    assert.notEqual(await inCrm.getAttribute('data-card-id'), await inPartners.getAttribute('data-card-id'));
    // Collapsing one path leaves the other.
    await inPartners.locator('.task-subtasks-toggle').click();
    assert.equal(await page.locator(`[data-card-id^="${await inPartners.getAttribute('data-card-id')}/task/"]:visible`).count(), 0);
    assert.equal(await inCrm.locator('.task-subtasks-toggle').getAttribute('aria-expanded'), 'true');

    // ---- Empty CRM has an explicit state.
    await card(empty.id).locator('.agfocus-crm-toggle').click();
    await card(empty.id).locator('.agfocus-crm-empty').waitFor();
    assert.equal(await card(empty.id).locator('.agfocus-crm-empty').textContent(), 'No entries in this CRM');

    // ---- Duplicate suppression when the list shifts between pages.
    await card(crm.id).locator('.agfocus-crm-toggle').click();     // collapse
    assert.deepEqual(await topEntries(crm.id), []);
    server.flags.repeatFirst = true;
    await card(crm.id).locator('.agfocus-crm-toggle').click();
    await until(async () => (await topEntries(crm.id)).length === 50, 'reopened first page');
    await card(crm.id).getByRole('button', {name: 'More entries'}).click();
    await until(async () => (await topEntries(crm.id)).length >= 99, 'shifted page');
    const shifted = await topEntries(crm.id);
    assert.equal(new Set(shifted).size, shifted.length, 'a repeated entry is shown once');
    assert.equal(shifted.filter(id => id.endsWith('/' + shifted[0].split('/').at(-1))).length, 1);
    server.flags.repeatFirst = false;

    // ---- Collapse during a load never shows stale rows; reopen starts a clean read.
    await card(crm.id).locator('.agfocus-crm-toggle').click();     // collapse
    let release;
    const started = new Promise(resolve => { server.flags.holdCrmList = {match: body => body.table_id === crm.id, started: resolve}; });
    const readsBefore = listOps().length;
    await card(crm.id).locator('.agfocus-crm-toggle').click();
    await started;
    await card(crm.id).locator('.agfocus-crm-toggle').click();     // collapse while the read is in flight
    server.flags.holdCrmList.release();
    server.flags.holdCrmList = null;
    await page.waitForTimeout(400);
    assert.deepEqual(await topEntries(crm.id), [], 'a read that finishes after collapse is discarded');
    await card(crm.id).locator('.agfocus-crm-toggle').click();     // reopen
    await until(async () => (await topEntries(crm.id)).length === 50, 'reopened');
    assert.ok(listOps().length > readsBefore + 1);
    // A board refresh while expanded keeps exactly the rows that were loaded.
    await card(crm.id).getByRole('button', {name: 'More entries'}).click();
    await until(async () => (await topEntries(crm.id)).length >= 99, 'second page again');
    await page.evaluate(() => { _agpad.nextFocus = 0; return agfocusRefresh(_agpad); });
    await until(() => server.boardReads >= 2, 'board refresh');
    await page.waitForTimeout(300);
    const afterRefresh = await topEntries(crm.id);
    assert.equal(new Set(afterRefresh).size, afterRefresh.length);
    assert.ok(afterRefresh.length >= 99, 'refresh keeps the loaded depth');

    // ---- A CRM membership opens its Task in paired mode; it has no per-row Pin action.
    const before = JSON.stringify([api.tables.get(crm.id), api.task(null, 'plain')]);
    const plainRow = entryRows(crm.id).filter({hasText: 'Plain member task'});
    assert.equal(await plainRow.locator('[data-focus-extra=pin]').count(), 0, 'CRM entries do not expose the removed Pin action');
    assert.deepEqual(await plainRow.locator('[data-focus-extra]').evaluateAll(nodes => nodes.map(node => node.getAttribute('aria-label'))), ['Remove Plain member task from CRM']);
    assert.equal(server.focusPresents.length, 0, 'opening a CRM row does not create a Focus card');
    assert.equal(JSON.stringify([api.tables.get(crm.id), api.task(null, 'plain')]), before, 'CRM row navigation changes neither the Table nor the Task');
    await plainRow.locator('.ttl').click();
    await page.locator('.agfocus-dock .rw-record:not([hidden]) .rw-title').filter({hasText: 'Plain member task'}).waitFor();
    assert.deepEqual(await page.evaluate(() => _agpad.focusWorkspace.current().source), {type: 'task', id: 'plain', business_id: null});
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current().biz), null, 'Global member routes to the root, not a Project');
    assert.equal(await page.locator('.agfocus-layout.agfocus-paired-workspace').count(), 1, 'CRM row details use the paired workspace');
    assert.equal(await page.locator('.agfocus-dock .rw-context').textContent(), 'Influencer CRM <b>x</b> /', 'the row retains its CRM Back context as text');
    await page.locator('.agfocus-dock .rw-close').click();
    await card(crm.id).waitFor();
    assert.equal(await card(crm.id).count(), 1);

    // ---- Distinct lifecycle controls: card archive vs table archive vs CRM remove vs record archive.
    assert.deepEqual(await card(crm.id).locator('[data-focus-extra]').evaluateAll(nodes => nodes.map(node => node.getAttribute('aria-label'))), ['Archive Focus card', 'Archive table (the table itself)']);
    assert.deepEqual(await entryRows(crm.id).first().locator('[data-focus-extra]').evaluateAll(nodes => nodes.map(node => node.getAttribute('aria-label'))), ['Remove Shared outreach story from CRM']);
    await entryRows(partners.id).first().locator('[data-focus-extra=remove]').evaluate(el => el.click());
    await page.locator('.modal .mfoot .primary').click();
    await until(async () => (await topEntries(partners.id)).length === 0, 'removed from CRM');
    assert.equal(ops('crm_entries_remove').at(-1).args.table_id, partners.id);
    assert.equal(ops('crm_entries_remove').at(-1).args.expected_revision, api.tables.get(partners.id).revision - 1);
    assert.equal(api.task(null, 'shared-story').archived, false, 'removal never archives the record');
    assert.equal(api.entries.get(crm.id).some(entry => entry.record_task_id === 'shared-story' && !entry.removed), true, 'the other CRM keeps the record');
    await card(partners.id).locator('.agfocus-crm-empty').waitFor();
    const tableBefore = api.tables.get(inventory.id).archived;
    await card(inventory.id).locator('[data-focus-extra=card]').evaluate(el => el.click());
    await until(() => server.archives.length === 1, 'card archive');
    assert.deepEqual([server.archives[0].item_id, server.archives[0].archived, server.archives[0].expected_card_revision], ['card-' + inventory.id, true, 1]);
    assert.equal(api.tables.get(inventory.id).archived, tableBefore, 'archiving the Focus card never archives the Table');
    assert.equal(ops('tables_update').length, 0);
    await card(empty.id).locator('[data-focus-extra=table]').evaluate(el => el.click());
    await page.locator('.modal .mfoot .primary').click();
    await until(() => ops('tables_update').length === 1, 'table archive');
    assert.deepEqual(ops('tables_update')[0].args.changes, {archived: true});
    assert.equal(api.tables.get(empty.id).archived, true);
    assert.equal(server.archives.length, 1, 'archiving the Table never archives its Focus card');
    assert.equal(api.task(null, 'plain').status, 'open', 'no member Task changed');
    assert.equal(await card(empty.id).count(), 1, 'the card stays on Focus');

    // ---- Project-scoped CRM: scoped routes and same Task id in two scopes.
    await card(alphaCrm.id).locator('.agfocus-crm-toggle').click();
    await until(async () => (await topEntries(alphaCrm.id)).length === 2, 'alpha entries');
    const alphaList = listOps().filter(entry => entry.scope === 'alpha').at(-1);
    assert.equal(alphaList.args.table_id, alphaCrm.id);
    assert.equal(api.log.some(entry => entry.scope === 'global' && entry.args.table_id === alphaCrm.id), false, 'project tables never use the Global route');
    await entryRows(alphaCrm.id).filter({hasText: 'Plain twin in Alpha'}).locator('.ttl').click();
    await page.locator('.agfocus-dock .rw-record:not([hidden]) .rw-title').filter({hasText: 'Plain twin in Alpha'}).waitFor();
    assert.deepEqual(await page.evaluate(() => _agpad.focusWorkspace.current().source), {type: 'task', id: 'plain', business_id: '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa'}, 'same id, other scope');
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current().biz), 'alpha');
    await page.locator('.agfocus-dock .rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus .agfocus-dock').hidden);

    // ---- Team thread and private conversation are separate: sources, drafts and send routes.
    await card(crm.id).locator('.ttl').click();
    const record = () => page.locator('.agfocus-dock .rw-record:not([hidden])');
    await record().locator('.rw-title').filter({hasText: 'Influencer CRM'}).waitFor();
    assert.deepEqual(await page.evaluate(() => _agpad.focusWorkspace.current().source), {type: 'table', id: crm.id, business_id: null});
    assert.deepEqual(await record().locator('.rw-tabs [role=tab]').evaluateAll(nodes => nodes.map(node => node.getAttribute('aria-label'))), ['Table dashboard'], 'paired CRM has one dashboard tab');
    assert.equal(await record().locator('[data-rw-tab=chat]').count(), 0, 'paired CRM does not add a second private Chat tab');
    const privateThread = page.locator('.gchat-paired-record'), privateComposer = privateThread.locator('.conversation-inline-compose textarea');
    assert.equal(await privateThread.getAttribute('data-conversation'), 'table:global:' + crm.id, 'the existing left chat is bound to the CRM source');
    await privateComposer.fill('Private draft only for Ordubashy');
    await record().locator('.crm-thread-compose textarea').fill('Team draft for everyone');
    assert.equal(await privateComposer.inputValue(), 'Private draft only for Ordubashy', 'the private draft remains in the left conversation');
    assert.equal(await record().locator('.crm-thread-compose textarea').inputValue(), 'Team draft for everyone', 'drafts are independent');
    await record().locator('.crm-thread-compose').getByRole('button', {name: 'Send to team'}).click();
    await record().locator('.crm-thread-message').filter({hasText: 'Team draft for everyone'}).waitFor();
    assert.equal(ops('tables_comment').length, 1);
    assert.equal(ops('tables_comment')[0].args.text, 'Team draft for everyone');
    assert.equal(server.privateSends.length, 0, 'a team message never goes to the private channel');
    assert.equal(await privateComposer.inputValue(), 'Private draft only for Ordubashy', 'the private draft survived the team send');
    await privateThread.locator('.conversation-inline-compose .gchat-send').click();
    await until(() => server.privateSends.length === 1, 'private send');
    assert.deepEqual(server.privateSends[0].source, {type: 'table', id: crm.id}, 'the private channel is keyed by the Table source (Global omits business_id, as for every Global source)');
    assert.equal(server.privateSends[0].text, 'Private draft only for Ordubashy');
    assert.equal(ops('tables_comment').length, 1, 'a private message never reaches the team thread');
    assert.equal((await page.locator('.crm-thread-list').textContent()).includes('Private draft'), false);
    // Membership developments appear in the team thread under current authority; they carry no cell values.
    if (shots) await page.screenshot({path: path.join(shots, 'focus-crm-workspace.png')});

    // ---- Focus Add can select an existing Table or CRM; it never creates one.
    const tablesBefore = api.tables.size;
    await page.locator('[data-focus-add]').click();
    await page.locator('input[name=kind][value=table]').check();
    await until(async () => (await page.locator('select[name=table] option').count()) >= 3, 'table choices');
    assert.ok((await page.locator('select[name=table] option').allTextContents()).some(label => label === 'Table · Ordinary inventory'), 'ordinary Tables are offered');
    await page.locator('select[name=table_kind]').selectOption('crm');
    await until(async () => { const labels = await page.locator('select[name=table] option').allTextContents(); return labels.length > 0 && labels.every(label => label.startsWith('CRM')); }, 'CRM filter');
    assert.equal(ops('tables_list').at(-1).args.table_kind, 'crm');
    await page.locator('select[name=table]').selectOption({label: 'CRM · Partner CRM'});
    await page.locator('.modal .mfoot .primary').click();
    await until(() => server.focusPresents.at(-1)?.source?.id === partners.id, 'present existing table');
    assert.equal(api.tables.size, tablesBefore, 'Focus never creates a table merely to pin it');
    assert.equal(ops('tables_create').length, 0);

    assert.deepEqual(errors, []);
    console.log('CRM Focus: Table/CRM cards, 130-member paging, scoped identities, cycle/duplicate/race guards, paired navigation, lifecycle controls and team/private separation passed');
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
function ops(name) { return api.log.filter(entry => entry.op === name); }
