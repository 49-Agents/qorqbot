// Story, Customer and Email records: thread-first opening, the optional left-side panel, Customer editing and
// state suggestions, historical email references, Story links/activity, Ordubashy-only record sends and layout.
// Synthetic mocked APIs on an isolated local port only: no live dashboard, external email or agent session.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo = path.resolve(__dirname, '..');
const artifacts = process.env.RECORD_PANEL_ARTIFACTS || path.resolve(repo, '..', 'screenshots');

const api = require('./fixtures/record_panels_api');
const {LONG, GLONG, version, flags, db, states, stateKey, log, compat, privateSends, privateReads, privateMarks, relationships, emailOps, associated, activityYear} = api;
// A retired email message relationship on the Story: it must read as a historical reference, never as Context.
relationships.alpha.push({id: 'rel-msg', revision: 1, role: 'concerns', source: {type: 'material', id: 'msg-9'}, target: {type: 'task', id: 'story-1'},
  source_item: {type: 'material', id: 'msg-9', label: 'Retired message', resource_type: 'email_messages'}, target_item: {type: 'task', id: 'story-1', label: 'Record', resource_type: 'tasks'}});

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const json = (data, status = 200) => { res.statusCode = status; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data)); };
  if (await api.handle(req, res, url)) return;
  if (url.pathname === '/api/session') return json({principal_id: 'owner-1', principal_kind: 'human', is_owner: true});
  if (url.pathname.startsWith('/api/')) return json({status: 'ok', data: {}});
  if (url.pathname === '/') {
    const styles = fs.readFileSync(path.join(repo, 'index.html'), 'utf8').match(/<link rel="stylesheet"[^>]+>/g).join('\n');
    const scripts = ['core', 'san-juan-time', 'task-state', 'task-snooze', 'tags', 'safe-links', 'chat-view', 'conversation', 'work-groups', 'relationships', 'context-source', 'tasks', 'task-progress', 'record-panel', 'global-tasks', 'agent-focus'];
    res.setHeader('Content-Type', 'text/html');
    return res.end(`<!doctype html><html><head><meta charset="utf-8">${styles}</head><body style="margin:0">
      <div id="toasts"></div><div id="host" style="width:100%"></div>
      ${scripts.map(name => `<script src="/js/${name}.js"></script>`).join('')}
      <script>
      state.bizs = [{id: 'biz-1', slug: 'alpha', name: 'Alpha'}]; state.biz = 'alpha'; state.scope = 'business';
      window.globalAgentProfile = () => null; window.loadGlobalAgentProfiles = async () => [];
      window.refreshSideSoon = () => {}; window.taskSetupMount = undefined;
      window.toasts = []; const baseToast = window.toast; window.toast = text => { window.toasts.push(text); };
      window.openBusiness = async id => { const task = await tFetchTask('alpha', id); window.detail = tDetail(task); return task; };
      window.openGlobal = id => { const host = document.querySelector('#host'); host.replaceChildren(); globalTasksMount(host, {initialTaskId: id}); };
      </script></body></html>`);
  }
  const file = path.join(repo, url.pathname);
  if (!file.startsWith(repo + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.svg') ? 'image/svg+xml' : 'text/css');
  res.end(fs.readFileSync(file));
});

(async () => {
  fs.mkdirSync(artifacts, {recursive: true});
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  try {
    const page = await browser.newPage({viewport: {width: 1300, height: 900}}), errors = [];
    page.setDefaultTimeout(10000);
    page.on('pageerror', error => { errors.push(error.message); console.error(error.message); });
    await page.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
    await page.goto(origin);
    const modal = page.locator('.modal').first();
    const closeModals = () => page.evaluate(() => { while (_modal) _modal.close(); });
    const ops = (name, scope) => log.filter(entry => entry.op === name && (!scope || entry.scope === scope));
    const openBusiness = async id => { await page.evaluate(id => openBusiness(id), id); await modal.locator('.conversation-panel').waitFor(); };
    const surface = () => modal.locator('.record-surface'); if (process.env.DBG) page.on('console', m => console.log('console:', m.text()));

    // ---- Every special type opens the Ordubashy conversation first, with the panel closed.
    for (const [id, label, icon] of [['story-1', 'Story dashboard', 'story'], ['cust-1', 'Customer context', 'customer'], ['mail-1', 'Email reference', 'email']]) {
      log.length = 0; privateReads.length = 0;
      await openBusiness(id);
      const toggle = surface().getByRole('button', {name: label, exact: true});
      assert.equal(await toggle.getAttribute('aria-expanded'), 'false', id + ' panel starts closed');
      assert.equal(await surface().locator('.record-panel').isVisible(), false);
      assert.equal(await toggle.locator('.task-type-icon-' + icon).count(), 1, id + ' button wears its type icon');
      await surface().getByText('Ordubashy line for ' + id).waitFor();
      assert.equal(privateReads.length >= 1 && privateReads.every(read => read.source.id === id && !('agent_id' in read)), true, id + ' reads only the default Ordubashy conversation');
      assert.equal(ops('tasks_get').filter(entry => entry.body.task_id === id).length, 0, id + ' does not load panel data before it is opened');
      assert.equal(await modal.locator('.conversation-channel,[role=tablist]').count(), 0, id + ' has no agent or team selector');
      await closeModals();
    }
    await openBusiness('plain-1');
    assert.equal(await modal.locator('.record-surface,.record-toggle').count(), 0, 'ordinary Tasks keep their detail without a record panel');
    assert.equal(await modal.locator('.conversation-panel').count(), 1);
    await closeModals();

    // ---- Opening and closing keeps the very same conversation mount, draft and scroll.
    await openBusiness('story-1');
    const composer = surface().locator('.conversation-panel textarea');
    await composer.fill('Draft that must survive');
    await page.evaluate(() => { window.mountNode = document.querySelector('.modal .conversation-panel'); window.mountHost = document.querySelector('.modal [data-conversation]'); });
    const toggle = surface().getByRole('button', {name: 'Story dashboard', exact: true});
    await toggle.click();
    await surface().locator('.record-panel').waitFor({state: 'visible'});
    assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
    assert.equal(await page.evaluate(() => mountNode === document.querySelector('.modal .conversation-panel') && mountHost === document.querySelector('.modal [data-conversation]')), true, 'the conversation mount is not rebuilt');
    assert.equal(await composer.inputValue(), 'Draft that must survive');
    const order = await surface().evaluate(el => { const panel = el.querySelector('.record-panel').getBoundingClientRect(), main = el.querySelector('.record-main').getBoundingClientRect(); return {panelLeft: panel.right <= main.left + 1, sameRow: Math.abs(panel.top - main.top) < 40}; });
    assert.deepEqual(order, {panelLeft: true, sameRow: true}, 'the panel sits to the left of the conversation');
    await page.screenshot({path: path.join(artifacts, 'story-panel-wide.png')});
    await toggle.click();
    assert.equal(await surface().locator('.record-panel').isVisible(), false);
    assert.equal(await composer.inputValue(), 'Draft that must survive');
    assert.equal(await page.evaluate(() => mountNode === document.querySelector('.modal .conversation-panel')), true);
    assert.deepEqual(errors, []);

    // ---- Story dashboard: click-to-edit overview, Focus-style children, heading link control,
    // canonical associated Signoffs/Actions, historical email links and compact paged activity.
    log.length = 0;
    await toggle.click();
    const panel = surface().locator('.record-panel');
    await panel.getByRole('heading', {name: 'Overview'}).waitFor();
    assert.equal(await panel.locator('.record-overview').innerText(), 'Original overview', 'read mode shows the overview as text');
    assert.equal(await panel.locator('[data-story-overview]').isVisible(), false, 'read mode is not a permanent textarea');
    assert.equal(await panel.locator('[data-story-edit]').count(), 0, 'the separate Edit button is gone');
    assert.equal(await panel.locator('[data-story-save]').isVisible(), false, 'Save only exists while editing');
    assert.equal(await panel.locator('[data-story-discard]').isVisible(), false, 'Discard only exists while editing');
    // Clicking the rendered overview opens the editor; Save and Discard become visible together.
    await panel.locator('.record-overview').click();
    await panel.locator('[data-story-overview]').waitFor({state: 'visible'});
    assert.equal(await panel.locator('[data-story-save]').isVisible(), true);
    assert.equal(await panel.locator('[data-story-discard]').isVisible(), true);
    await panel.locator('[data-story-overview]').fill('Edited overview');
    // A repeated activation never resets the typed draft.
    await panel.evaluate(el => el.querySelector('[data-story-read]').click());
    assert.equal(await panel.locator('[data-story-overview]').inputValue(), 'Edited overview', 'a repeated click keeps the draft');
    await panel.locator('[data-story-save]').click();
    await expectEventually(() => ops('tasks_update').length === 1);
    const saved = ops('tasks_update')[0].body;
    assert.deepEqual(saved.changes, {overview: 'Edited overview'});
    assert.equal(saved.expected_revision, 1);
    assert.equal(await composer.inputValue(), 'Draft that must survive', 'saving data leaves the draft alone');
    assert.equal(await page.evaluate(() => mountNode === document.querySelector('.modal .conversation-panel')), true, 'saving data leaves the conversation mounted');
    await panel.locator('[data-story-save]').waitFor({state: 'hidden'});
    assert.equal(await panel.locator('[data-story-save]').isVisible(), false, 'a successful save returns to read mode');
    // Keyboard activation opens the same editor, and Discard returns to the rendered overview.
    await panel.locator('.record-overview').focus();
    await page.keyboard.press('Enter');
    await panel.locator('[data-story-overview]').waitFor({state: 'visible'});
    await panel.locator('[data-story-overview]').fill('Discarded text');
    await panel.locator('[data-story-discard]').click();
    assert.equal(await panel.locator('.record-overview').innerText(), 'Edited overview', 'Discard restores the saved overview');
    assert.equal(await panel.locator('[data-story-overview]').isVisible(), false);
    // A stale revision keeps the typed overview and offers the latest revision.
    db.alpha['story-1'].revision = 9;
    await panel.locator('.record-overview').click();
    await panel.locator('[data-story-overview]').fill('Typed after a conflict');
    await panel.locator('[data-story-save]').click();
    await panel.getByRole('alert').filter({hasText: 'changed since you opened it'}).waitFor();
    assert.equal(await panel.locator('[data-story-overview]').inputValue(), 'Typed after a conflict');
    await panel.getByRole('button', {name: 'Use latest revision'}).click();
    await panel.getByText('Loaded the latest revision').waitFor();
    assert.equal(await panel.locator('[data-story-overview]').inputValue(), 'Typed after a conflict', 'the edit is kept after adopting the latest revision');
    await panel.locator('[data-story-save]').click();
    await expectEventually(() => ops('tasks_update').length === 3);
    assert.equal(ops('tasks_update')[2].body.expected_revision, 9);
    // Email conversations can no longer be linked; the section is only a read-only list of historical references.
    assert.equal(await panel.getByRole('button', {name: /Link email conversation/}).count(), 0);
    assert.equal(await panel.locator('.record-link-open', {hasText: 'Retired message'}).count(), 1, 'it appears once, under historical references only');
    await panel.getByRole('button', {name: 'Retired message', exact: true}).click();
    await page.locator('.historical-email').getByText(/historical email reference/).waitFor();
    assert.equal(ops('context_items_get').some(entry => entry.body.item?.id === 'msg-9'), false, 'a retired message is never fetched as Context');
    await page.evaluate(() => _modal.close());   // only the stacked historical note

    // ---- Child tasks use the Focus row presentation: status/type markers, compact rows and nested Stories.
    const childRows = panel.locator('.record-child-list .record-child-row');
    await childRows.filter({hasText: 'Draft the launch'}).waitFor();
    assert.equal(ops('tasks_list').some(entry => entry.body.parent_id === 'story-1'), true, 'children come from the parent filter');
    assert.equal(await childRows.count(), 2, 'the first child page is shown');
    assert.equal(await childRows.filter({hasText: 'Draft the launch'}).locator('.record-child-marker .ball').count(), 1, 'an ordinary child wears its status marker');
    const childStory = childRows.filter({hasText: 'Plan the launch party'});
    assert.equal(await childStory.locator('.record-child-marker .task-subtasks-toggle').count(), 1, 'a child Story wears the Story expand toggle');
    assert.equal(await childStory.locator('.record-child-toggle').getAttribute('aria-expanded'), 'false');
    await childStory.locator('.record-child-toggle').click();
    await panel.locator('.record-child-row', {hasText: 'Shortlist the caterers'}).waitFor();
    assert.equal(await childStory.locator('.record-child-toggle').getAttribute('aria-expanded'), 'true');
    assert.equal(await panel.locator('.record-child-row', {hasText: 'Shortlist the caterers'}).evaluate(el => getComputedStyle(el).getPropertyValue('--record-depth').trim()), '1', 'a nested child Story indents its children');
    await childStory.locator('.record-child-toggle').click();
    await expectEventually(async () => await panel.locator('.record-child-row', {hasText: 'Shortlist the caterers'}).count() === 0);
    await panel.getByRole('button', {name: 'More child tasks'}).click();
    await expectEventually(async () => await childRows.count() === 4);
    assert.equal(await panel.locator('.record-child-row', {hasText: 'Quiet archived'}).locator('.record-child-flag').textContent(), 'Archived', 'archived children stay visible and labelled');
    assert.equal(await childRows.filter({hasText: 'Archive the old brief'}).locator('.record-child-marker .ball.done').count(), 1, 'a completed child keeps its done marker');

    // ---- Linked Tasks: the add control sits at the right of the heading and keeps the existing picker,
    // duplicate prevention, unlink and revision checks.
    const linksSection = panel.locator('.record-section', {hasText: 'Linked tasks'});
    const addLink = linksSection.locator('.record-section-head').getByRole('button', {name: 'Link task'});
    assert.equal(await addLink.count(), 1, 'one heading add control');
    assert.equal(await addLink.getAttribute('title'), 'Link task');
    assert.equal(await addLink.locator('svg').count(), 1, 'the chain-link glyph is the control');
    assert.equal(await linksSection.locator('.task-links').getByRole('button', {name: 'Link task'}).count(), 0, 'the list no longer carries a second add control');
    const updatesBeforeLink = ops('tasks_update').length;
    await addLink.click();
    await page.locator('.task-link-options .task-link-option', {hasText: 'Draft the launch note'}).click();
    await expectEventually(() => ops('tasks_update').length === updatesBeforeLink + 1);
    const linkWrite = ops('tasks_update').at(-1).body;
    assert.deepEqual(linkWrite.changes, {related_task: {task_id: 'child-1', business_id: 'biz-1', linked: true}});
    assert.equal(linkWrite.expected_revision, db.alpha['story-1'].revision - 1, 'the link writes against the current revision');
    await panel.locator('.task-link-chip', {hasText: 'Draft the launch note'}).waitFor();
    assert.equal(await panel.locator('.task-link-chip', {hasText: 'Draft the launch note'}).count(), 1, 'one chip for one link');
    // Linking the same target again never duplicates the chip.
    await addLink.click();
    await page.locator('.task-link-options .task-link-option', {hasText: 'Draft the launch note'}).click();
    await expectEventually(() => ops('tasks_update').length === updatesBeforeLink + 2);
    assert.equal(await panel.locator('.task-link-chip', {hasText: 'Draft the launch note'}).count(), 1, 'the duplicate link stays one chip');
    const unlink = panel.locator('.task-link-pair', {hasText: 'Draft the launch note'}).getByRole('button', {name: 'Unlink Draft the launch note'});
    await unlink.click();
    await expectEventually(() => ops('tasks_update').length === updatesBeforeLink + 3);
    assert.deepEqual(ops('tasks_update').at(-1).body.changes, {related_task: {task_id: 'child-1', business_id: 'biz-1', linked: false}});
    await expectEventually(async () => await panel.locator('.task-link-chip', {hasText: 'Draft the launch note'}).count() === 0);

    // ---- Signoffs and Actions come from the canonical recursive associated-record read.
    const signoffSection = panel.locator('.record-section').filter({has: page.locator('h4', {hasText: 'Signoffs'})});
    const actionSection = panel.locator('.record-section').filter({has: page.locator('h4', {hasText: 'Actions'})});
    await signoffSection.getByRole('button', {name: 'Approve the spring budget'}).waitFor();
    const associatedSignoff = associated.calls.filter(entry => entry.record_type === 'signoff').at(-1);
    assert.deepEqual([associatedSignoff.task_id, associatedSignoff.include_descendants], ['story-1', true], 'the recursive Story read is requested explicitly');
    assert.equal(associated.calls.every(entry => !('scope' in entry)), true, 'a Task operation never names a scope argument');
    assert.equal(await signoffSection.getByRole('button', {name: 'Approve the linked task'}).count(), 0, 'a linked Task is not a descendant');
    assert.equal(await signoffSection.getByRole('button', {name: 'Approve the photographer brief'}).count(), 1, 'a descendant Signoff is included');
    assert.match(await signoffSection.locator('.record-link', {hasText: 'Approve the spring budget'}).innerText(), /approved/, 'completed history is shown');
    assert.equal(await actionSection.getByRole('button', {name: 'Draft the venue checklist'}).count(), 1, 'a deep descendant Action is included');
    await actionSection.getByRole('button', {name: 'More actions'}).click();
    await actionSection.getByRole('button', {name: 'Book the launch photographer'}).waitFor();
    assert.match(await actionSection.locator('.record-link', {hasText: 'Book the launch photographer'}).innerText(), /withdrawn/, 'withdrawn history is shown');
    assert.equal(await signoffSection.getByRole('button', {name: 'Approved the launch date'}).count(), 0, 'no invented row');
    await signoffSection.getByRole('button', {name: 'More signoffs'}).click();
    await signoffSection.getByRole('button', {name: 'Approve the launch date'}).waitFor();
    assert.equal(await signoffSection.getByRole('button', {name: 'Approve the linked task'}).count(), 0, 'the linked Task history never joins after paging');
    // A failed read keeps both sections visible with a retry instead of an empty result.
    await closeModals();
    flags.failAssociatedOnce = true;
    log.length = 0;
    await openBusiness('story-1');
    await surface().getByRole('button', {name: 'Story dashboard', exact: true}).click();
    const failurePanel = surface().locator('.record-panel');
    await failurePanel.locator('.record-section').filter({has: page.locator('h4', {hasText: 'Signoffs'})})
      .getByText(/Couldn't load/).waitFor();
    assert.equal(await failurePanel.locator('.record-section').filter({has: page.locator('h4', {hasText: 'Signoffs'})}).isVisible(), true, 'a failure never hides the section');
    await failurePanel.getByRole('button', {name: 'Retry signoffs'}).click();
    await failurePanel.locator('.record-section').filter({has: page.locator('h4', {hasText: 'Signoffs'})})
      .getByRole('button', {name: 'Approve the spring budget'}).waitFor();
    // A successful empty answer hides the whole section.
    await closeModals();
    await openBusiness('story-empty');
    await surface().getByRole('button', {name: 'Story dashboard', exact: true}).click();
    const emptyPanel = surface().locator('.record-panel');
    await emptyPanel.getByRole('heading', {name: 'Overview'}).waitFor();
    await expectEventually(async () => await emptyPanel.locator('.record-section').filter({has: page.locator('h4', {hasText: 'Signoffs'})}).isVisible() === false);
    await expectEventually(async () => await emptyPanel.locator('.record-section').filter({has: page.locator('h4', {hasText: 'Actions'})}).isVisible() === false);
    await closeModals();

    // ---- Compact activity: actor glyphs, text and timestamp columns, with date change rules and paging.
    log.length = 0;
    await openBusiness('story-1');
    await surface().getByRole('button', {name: 'Story dashboard', exact: true}).click();
    const activityPanel = surface().locator('.record-panel');
    await activityPanel.locator('.record-activity-entry').first().waitFor();
    assert.equal(await activityPanel.locator('.record-activity-entry').count(), 20);
    assert.match(await activityPanel.locator('.record-activity-entry').first().innerText(), /Activity entry 45/);
    assert.equal(await activityPanel.locator('.record-activity-avatar .identity-avatar').count(), 20, 'each actor shows an avatar');
    assert.equal(await activityPanel.locator('.record-activity-clock').count(), 20, 'the time is always visible');
    assert.equal(await activityPanel.locator('.record-activity-entry').first().locator('.record-activity-date').innerText(), activityYear === 2026 ? 'Oct 2' : 'Oct 2, 2026', 'the first row shows its month and day');
    assert.equal(await activityPanel.locator('.record-activity-entry').nth(1).locator('.record-activity-date').count(), 0, 'an unchanged date is not repeated');
    const firstStamp = activityPanel.locator('.record-activity-entry').first().locator('time');
    assert.equal(await firstStamp.getAttribute('datetime'), '2026-10-02T10:00:00Z', 'the full instant stays in datetime');
    assert.match(await firstStamp.getAttribute('title'), /2026/, 'the full timestamp stays in the tooltip');
    await activityPanel.getByRole('button', {name: 'Older activity'}).click();
    await expectEventually(async () => await activityPanel.locator('.record-activity-entry').count() === 40);
    await activityPanel.getByRole('button', {name: 'Older activity'}).click();
    await expectEventually(async () => await activityPanel.locator('.record-activity-entry').count() === 45);
    assert.equal(await activityPanel.getByRole('button', {name: 'Older activity'}).isVisible(), false);
    assert.equal(await activityPanel.locator('textarea.gchat-input,.gchat-composer').count(), 0, 'activity is read-only: no second composer');
    assert.equal(await activityPanel.locator('.record-activity-date').count(), 1, 'the preceding date carries across the older pages');
    // Failed loads offer a retry.
    flags.failThreadOnce = true;
    await activityPanel.getByRole('button', {name: 'Refresh activity'}).click();
    await activityPanel.getByRole('alert').filter({hasText: /Couldn't load/}).waitFor();
    await activityPanel.getByRole('button', {name: 'Refresh activity'}).click();
    await expectEventually(async () => await activityPanel.locator('.record-activity-entry').count() === 20);
    await closeModals();

    // ---- Activity date boundaries in the app's display timezone (America/Puerto_Rico).
    log.length = 0;
    await openBusiness('story-dates');
    await surface().getByRole('button', {name: 'Story dashboard', exact: true}).click();
    const dated = surface().locator('.record-panel');
    await dated.locator('.record-activity-entry').first().waitFor();
    const labels = () => dated.locator('.record-activity-date').evaluateAll(nodes => nodes.map(node => node.textContent));
    await expectEventually(async () => (await labels()).length === 5);
    assert.deepEqual(await labels(), ['Oct 29', 'Oct 28', 'Oct 27', `Dec 31, ${activityYear - 1}`, `Jan 2, ${activityYear - 2}`],
      'month/day once per changed date; year only when the first row is outside the current year or a year differs from the row above');
    assert.deepEqual(await dated.locator('.record-activity-clock').evaluateAll(nodes => nodes.map(node => node.textContent)),
      ['08:00', '10:05', '05:00', '19:59', '08:00', '08:00'], 'times are the app display timezone');
    assert.equal(await dated.locator('.record-activity-entry').first().locator('.record-activity-avatar svg').count(), 1, 'a system entry uses the activity glyph');
    assert.equal(await dated.locator('.record-activity-entry').first().locator('.identity-avatar').count(), 0);
    assert.equal(await dated.locator('.record-activity-entry').nth(1).locator('.identity-avatar').count(), 1, 'an agent entry uses the actor avatar');
    await closeModals();
    await openBusiness('story-old');
    await surface().getByRole('button', {name: 'Story dashboard', exact: true}).click();
    const oldPanel = surface().locator('.record-panel');
    await oldPanel.locator('.record-activity-entry').first().waitFor();
    assert.deepEqual(await oldPanel.locator('.record-activity-date').evaluateAll(nodes => nodes.map(node => node.textContent)),
      [`Jun 1, ${activityYear - 4}`, 'May 1'], 'the first row outside the current year shows its year; an unchanged following year does not');
    await closeModals();

    // ---- Customer: typed fields, scoped suggestions, state independent of status/archive, conflict preservation.
    log.length = 0;
    await openBusiness('cust-1');
    await surface().getByRole('button', {name: 'Customer context', exact: true}).click();
    const customer = surface().locator('.record-panel');
    await customer.getByLabel('Name', {exact: true}).waitFor();
    assert.equal(await customer.getByLabel('Name', {exact: true}).inputValue(), 'Rosa Alvarez');
    assert.equal(await customer.getByLabel('Company', {exact: true}).inputValue(), 'Alvarez Dental');
    assert.equal(await customer.locator('[data-customer-notes]').inputValue(), 'Met at the fair');
    assert.equal(await customer.locator('[data-customer-kind]').inputValue(), 'person');
    await expectEventually(async () => (await customer.locator('[data-customer-state-options] option').evaluateAll(list => list.map(o => o.value))).join() === 'Prospect,Active');
    assert.equal(ops('tasks_config_get')[0].scope, 'alpha', 'suggestions come from the record scope');
    // Saving only the state sends only the state and keeps unsaved profile edits.
    await customer.getByLabel('Phone', {exact: true}).fill('555-0100');
    await customer.locator('[data-customer-state]').fill('  Negotiating   deal ');
    await customer.getByRole('button', {name: 'Set state'}).click();
    await expectEventually(() => ops('tasks_update').length === 1);
    assert.deepEqual(ops('tasks_update')[0].body.changes, {customer_state: 'Negotiating deal'});
    await page.waitForTimeout(150);
    assert.equal(await customer.getByLabel('Phone', {exact: true}).inputValue(), '555-0100', 'a state change keeps unsaved profile edits');
    assert.equal(db.alpha['cust-1'].status, 'open', 'a state label never moves the Task status');
    assert.equal(db.alpha['cust-1'].archived, false, 'a state label never archives');
    await expectEventually(async () => await customer.locator('[data-customer-state-options] option[value="Negotiating deal"]').count() === 1, 'a new label extending the choices');
    // Saving the profile sends only the profile and keeps the state.
    await customer.getByRole('button', {name: 'Save details'}).click();
    await expectEventually(() => ops('tasks_update').length === 2);
    assert.deepEqual(Object.keys(ops('tasks_update')[1].body.changes), ['customer_profile']);
    assert.equal(ops('tasks_update')[1].body.changes.customer_profile.phone, '555-0100');
    assert.equal(ops('tasks_update')[1].body.changes.customer_profile.notes, 'Met at the fair');
    assert.equal(db.alpha['cust-1'].customer_state, 'Negotiating deal');
    // Explicit Archive uses the existing boolean and leaves the state alone.
    await customer.getByRole('button', {name: 'Archive', exact: true}).click();
    await expectEventually(() => ops('tasks_update').length === 3);
    assert.deepEqual(ops('tasks_update')[2].body.changes, {archived: true});
    assert.equal(db.alpha['cust-1'].customer_state, 'Negotiating deal');
    await customer.getByRole('button', {name: 'Unarchive', exact: true}).waitFor();
    await customer.getByRole('button', {name: 'Unarchive', exact: true}).click();
    await expectEventually(() => ops('tasks_update').length === 4);
    assert.deepEqual(ops('tasks_update')[3].body.changes, {archived: false});
    // An uncertain state save is retried with the same key.
    flags.loseUpdateOnce = true;
    await customer.locator('[data-customer-state]').fill('Won');
    await customer.getByRole('button', {name: 'Set state'}).click();
    await customer.getByRole('alert').filter({hasText: /Couldn't confirm/}).waitFor();
    await customer.getByRole('button', {name: 'Set state'}).click();
    await expectEventually(() => ops('tasks_update').length === 6);
    assert.equal(ops('tasks_update')[4].body.idempotency_key, ops('tasks_update')[5].body.idempotency_key, 'the retry reuses the exact request key');
    // Revision conflict preserves unsaved profile and state.
    db.alpha['cust-1'].revision += 5;
    await customer.getByLabel('Company', {exact: true}).fill('Typed company');
    await customer.locator('[data-customer-state]').fill('Typed state');
    await customer.getByRole('button', {name: 'Save details'}).click();
    await customer.getByRole('alert').filter({hasText: 'changed since you opened it'}).waitFor();
    assert.equal(await customer.getByLabel('Company', {exact: true}).inputValue(), 'Typed company');
    assert.equal(await customer.locator('[data-customer-state]').inputValue(), 'Typed state');
    await customer.getByRole('button', {name: 'Use latest revision'}).click();
    await customer.getByText('Loaded the latest revision').waitFor();
    assert.equal(await customer.getByLabel('Company', {exact: true}).inputValue(), 'Typed company');
    assert.equal(await customer.locator('[data-customer-state]').inputValue(), 'Typed state');
    // Actionable existing-Context links.
    await customer.getByRole('button', {name: 'Link Context'}).click();
    await page.locator('.record-pick-option', {hasText: 'Rosa contact'}).click();
    await customer.getByRole('button', {name: 'Rosa contact', exact: true}).waitFor();
    const context = ops('relationships_create').at(-1).body;
    assert.deepEqual([context.source.id, context.target, context.role], ['person-1', {type: 'task', id: 'cust-1'}, 'concerns']);
    await page.screenshot({path: path.join(artifacts, 'customer-panel-wide.png')});
    await closeModals();

    // ---- Converting a Customer away asks first and sends the resets explicitly.
    log.length = 0;
    await openBusiness('cust-1');
    await modal.locator('[data-task-type]').selectOption('task');
    await modal.getByRole('button', {name: 'Save type'}).click();
    const confirm = page.locator('.modal', {hasText: 'Remove customer details?'});
    await confirm.waitFor();
    await confirm.getByRole('button', {name: 'Cancel'}).click();
    await page.waitForTimeout(150);
    assert.equal(compat.length, 0, 'cancelling the confirmation sends nothing');
    await modal.getByRole('button', {name: 'Save type'}).click();
    await page.locator('.modal', {hasText: 'Remove customer details?'}).getByRole('button', {name: 'Change type'}).click();
    await expectEventually(() => compat.length === 1);
    const conversion = compat[0].set;
    assert.equal(conversion.task_type, 'task');
    assert.deepEqual([conversion.customer_kind, conversion.customer_state, conversion.customer_profile], [null, '', {}]);
    await closeModals();

    // ---- Email record: a historical reference only; no mail endpoint is read and the Ordubashy thread stays beside it.
    log.length = 0; emailOps.reads.length = 0; emailOps.comments = 0;
    await openBusiness('mail-1');
    await surface().getByRole('button', {name: 'Email reference', exact: true}).click();
    const mail = surface().locator('.record-panel');
    await mail.getByText(/historical email reference/).waitFor();
    assert.equal(emailOps.reads.length + emailOps.comments, 0, 'no mail endpoint was read');
    assert.equal(await mail.locator('.email-messages,.email-internal,.email-comments,.gchat-composer,textarea').count(), 0);
    assert.equal(await surface().locator('.record-main .gchat-composer').count(), 1, 'the Task\'s Ordubashy composer remains beside the reference');
    await closeModals();

    // ---- Global records use the same behavior and Global reads.
    log.length = 0; privateReads.length = 0;
    await page.evaluate(() => openGlobal('cust-g'));
    const gsurface = page.locator('#host .record-surface');
    await gsurface.locator('.conversation-panel').waitFor();
    await gsurface.getByText('Ordubashy line for cust-g').waitFor();
    assert.equal(privateReads[0].scope, 'global');
    assert.equal(await gsurface.locator('.record-panel').isVisible(), false);
    await gsurface.getByRole('button', {name: 'Customer context', exact: true}).click();
    const gcustomer = gsurface.locator('.record-panel');
    await gcustomer.getByLabel('Name', {exact: true}).waitFor();
    await expectEventually(async () => (await gcustomer.locator('[data-customer-state-options] option').evaluateAll(list => list.map(o => o.value))).join() === 'Founder lead');
    assert.equal(ops('tasks_config_get').at(-1).scope, 'global', 'Global suggestions are Global only');
    await closeModals();
    await page.evaluate(() => openGlobal('mail-g'));
    await page.locator('#host .record-surface').getByRole('button', {name: 'Email reference', exact: true}).click();
    await page.locator('#host .record-panel').getByText(/historical email reference/).waitFor();
    assert.equal(emailOps.reads.length, 0);
    assert.equal(await page.locator('#host .record-panel textarea').count(), 0);

    // ---- Record sends go only to the Ordubashy default conversation.
    await page.evaluate(() => openGlobal('story-g'));
    const story = page.locator('#host .record-surface');
    await story.locator('.conversation-panel textarea').fill('Only Ordubashy hears this');
    await story.locator('.gchat-send').click();
    await expectEventually(() => privateSends.length === 1);
    assert.equal(privateSends[0].text, 'Only Ordubashy hears this');
    assert.deepEqual(Object.keys(privateSends[0]).sort(), ['idempotency_key', 'scope', 'source', 'text'], 'a record send names no agent, peer or team channel');
    assert.equal(ops('owner_conversation_peers').length, 0, 'no peer discovery runs');

    // ---- Stale panel responses never paint another record.
    await page.evaluate(() => openGlobal('story-g'));
    await page.locator('#host .record-surface').waitFor();
    flags.holdTaskGet = 'story-g';
    await page.locator('#host .record-surface').getByRole('button', {name: 'Story dashboard', exact: true}).click();
    await expectEventually(() => flags.releaseTaskGet !== null);
    await page.evaluate(() => openGlobal('cust-g'));
    await page.locator('#host .record-surface[data-record-type="customer"]').waitFor();
    flags.releaseTaskGet(); flags.releaseTaskGet = null;
    await page.waitForTimeout(250);
    assert.equal(await page.locator('#host .record-surface[data-record-type="customer"] .record-panel').isVisible(), false, 'the new record opens with its panel closed');
    assert.equal(await page.locator('#host [data-story-overview]').count(), 0, 'a late Story response does not paint into the Customer');


    // ================================================================ same-record saves keep the surface
    const registrations = () => page.evaluate(() => [...conversationStates.values()].reduce((sum, st) => sum + [...st.mounts].filter(node => node.isConnected).length, 0));
    const keepConversation = (selector) => page.evaluate(selector => { window.keepNode = document.querySelector(selector + ' .conversation-panel'); window.keepScroll = null; }, selector);
    const sameConversation = selector => page.evaluate(selector => window.keepNode === document.querySelector(selector + ' .conversation-panel') && window.keepNode.isConnected, selector);
    const scrollState = async selector => {
      const scroller = page.locator(selector + ' .gchat-scroll');
      assert.ok(await scroller.evaluate(el => el.scrollHeight > el.clientHeight + 40), 'the fixture conversation really scrolls');
      await scroller.evaluate(el => { el.scrollTop = 120; });
      return scroller.evaluate(el => el.scrollTop);
    };

    // ---- Global Customer: archive in the panel, then an unrelated generic edit neither undoes it nor remounts anything.
    log.length = 0;
    await page.evaluate(() => openGlobal('cust-g'));
    let gs = page.locator('#host .record-surface');
    await gs.getByText('Ordubashy line for cust-g').waitFor();
    await gs.getByRole('button', {name: 'Customer context', exact: true}).click();
    let gp = gs.locator('.record-panel');
    await gp.locator('[data-customer-name]').waitFor();
    await keepConversation('#host');
    await gs.locator('.conversation-panel textarea').fill('Draft that stays');
    const rememberedScroll = await scrollState('#host');
    await page.evaluate(() => { window.keepSurface = document.querySelector('#host .record-surface')._recordSurface; });
    await gp.getByRole('button', {name: 'Archive', exact: true}).click();
    await gp.getByRole('button', {name: 'Unarchive', exact: true}).waitFor();
    assert.equal(await page.locator('#host [data-editor] [name=archived]').isChecked(), true, 'the Archived checkbox follows the confirmed panel action');
    await page.locator('#host [data-editor] [name=title]').fill('Renamed through the generic form');
    await page.locator('#host [data-editor] [data-save]').click();
    await expectEventually(() => db.global['cust-g'].title === 'Renamed through the generic form');
    const genericSave = ops('tasks_update', 'global').at(-1).body;
    assert.deepEqual(genericSave.changes, {title: 'Renamed through the generic form'}, 'a generic save sends only what the user changed (no stale archive flag, no type or panel fields)');
    assert.equal(db.global['cust-g'].archived, true, 'the unrelated save leaves the record archived');
    assert.equal(await page.locator('#host [data-editor] [name=archived]').isChecked(), true);
    assert.equal(await sameConversation('#host'), true, 'the conversation node survives a generic save');
    assert.equal(await page.evaluate(() => window.keepSurface === document.querySelector('#host .record-surface')._recordSurface), true, 'the record surface survives a generic save');
    assert.equal(await gs.locator('.conversation-panel textarea').inputValue(), 'Draft that stays');
    assert.equal(await page.locator('#host .gchat-scroll').evaluate(el => el.scrollTop), rememberedScroll, 'the conversation scroll position survives a generic save');
    assert.equal(await gp.isVisible(), true, 'an open panel stays open');
    // The panel still writes with the shared latest revision: no conflict after the generic save.
    await gp.getByRole('button', {name: 'Unarchive', exact: true}).click();
    await expectEventually(() => db.global['cust-g'].archived === false);
    assert.equal(ops('tasks_update', 'global').at(-1).body.expected_revision, db.global['cust-g'].revision - 1, 'the panel wrote against the revision the generic save produced');
    await expectEventually(async () => !(await page.locator('#host [data-editor] [name=archived]').isChecked()), 'the checkbox following Unarchive');
    // A deliberate pending edit to Archived is not overwritten by a panel action on something else, and saves alone.
    await gp.getByRole('button', {name: 'Archive', exact: true}).click();
    await expectEventually(() => db.global['cust-g'].archived === true);
    await expectEventually(() => page.locator('#host [data-editor] [name=archived]').isChecked());
    await page.locator('#host [data-editor] [name=archived]').uncheck();
    await gp.locator('[data-customer-state]').fill('Pending choice');
    await gp.getByRole('button', {name: 'Set state', exact: true}).click();
    await expectEventually(() => db.global['cust-g'].customer_state === 'Pending choice');
    await page.waitForTimeout(100);
    assert.equal(await page.locator('#host [data-editor] [name=archived]').isChecked(), false, 'a deliberate unsaved Archived edit is kept');
    await page.locator('#host [data-editor] [data-save]').click();
    await expectEventually(() => db.global['cust-g'].archived === false);
    assert.deepEqual(ops('tasks_update', 'global').at(-1).body.changes, {archived: false}, 'the deliberate edit is the only thing sent');

    // ---- Unsaved panel profile survives an unrelated generic save and is then saved against the new revision.
    await gp.locator('[data-customer-profile="phone"]').fill('555-9999');
    await page.locator('#host [data-editor] [name=archived]').uncheck();
    await page.locator('#host [data-editor] [name=due]').fill('2026-12-01');
    const before = ops('tasks_update', 'global').length;
    await page.locator('#host [data-editor] [data-save]').click();
    await expectEventually(() => ops('tasks_update', 'global').length === before + 1);
    assert.deepEqual(ops('tasks_update', 'global').at(-1).body.changes, {due: '2026-12-01'});
    assert.equal(await gp.locator('[data-customer-profile="phone"]').inputValue(), '555-9999', 'unsaved profile text is kept');
    assert.equal(await sameConversation('#host'), true);
    await gp.getByRole('button', {name: 'Save details', exact: true}).click();
    await expectEventually(() => db.global['cust-g'].customer_profile.phone === '555-9999');
    assert.equal(await gp.getByRole('alert').filter({hasText: 'changed since'}).count(), 0, 'no revision conflict after the generic save');

    // ---- Canonical state spelling: the server's first spelling replaces what was typed; no duplicate variants.
    await gp.locator('[data-customer-state]').fill('founder   LEAD');
    await gp.getByRole('button', {name: 'Set state', exact: true}).click();
    await expectEventually(async () => await gp.locator('[data-customer-state]').inputValue() === 'Founder lead');
    await gp.locator('[data-customer-state]').fill('ＦＯＵＮＤＥＲ ＬＥＡＤ');
    await gp.getByRole('button', {name: 'Set state', exact: true}).click();
    await expectEventually(() => ops('tasks_update', 'global').filter(entry => entry.body.changes.customer_state).length >= 2);
    await expectEventually(async () => await gp.locator('[data-customer-state]').inputValue() === 'Founder lead');
    assert.equal(db.global['cust-g'].customer_state, 'Founder lead');
    assert.deepEqual(states.global.filter(label => stateKey(label) === stateKey('founder lead')), ['Founder lead'], 'no case or Unicode variant joined the vocabulary');
    assert.deepEqual(await gp.locator('[data-customer-state-options] option').evaluateAll(list => list.map(o => o.value).filter(v => v.toLowerCase().includes('founder'))), ['Founder lead'], 'the suggestions list one spelling');
    // A brand-new label is stored as written (whitespace collapsed) and shown that way.
    await gp.locator('[data-customer-state]').fill('  Warm   intro ');
    await gp.getByRole('button', {name: 'Set state', exact: true}).click();
    await expectEventually(async () => await gp.locator('[data-customer-state]').inputValue() === 'Warm intro');

    // ---- Global Story: unsaved overview survives an unrelated generic save.
    await page.evaluate(() => openGlobal('story-g'));
    gs = page.locator('#host .record-surface');
    await gs.getByText('Ordubashy line for story-g').waitFor();
    await gs.getByRole('button', {name: 'Story dashboard', exact: true}).click();
    gp = gs.locator('.record-panel');
    await gp.locator('.record-overview').waitFor();
    await gp.locator('.record-overview').click();
    await keepConversation('#host');
    await gp.locator('[data-story-overview]').fill('Unsaved story overview');
    await page.locator('#host [data-editor] [name=title]').fill('Global story, renamed');
    await page.locator('#host [data-editor] [data-save]').click();
    await expectEventually(() => db.global['story-g'].title === 'Global story, renamed');
    assert.deepEqual(ops('tasks_update', 'global').at(-1).body.changes, {title: 'Global story, renamed'}, 'an existing Story never resends its overview from the generic form');
    assert.equal(await gp.locator('[data-story-overview]').inputValue(), 'Unsaved story overview');
    assert.equal(await sameConversation('#host'), true);
    await gp.locator('[data-story-save]').click();
    await expectEventually(() => db.global['story-g'].overview === 'Unsaved story overview');
    assert.equal(await gp.getByRole('alert').filter({hasText: 'changed since'}).count(), 0);
    // A panel rename shows in the clean generic title; a typed generic title is left alone.
    assert.equal(await page.locator('#host [data-editor] [name=title]').inputValue(), 'Global story, renamed');

    // ---- Business detail: due date, tags or assignee edits keep the very same surface, conversation and panel edits.
    log.length = 0; compat.length = 0;
    await openBusiness('story-1');
    await surface().getByRole('button', {name: 'Story dashboard', exact: true}).click();
    const bp = surface().locator('.record-panel');
    await bp.locator('.record-overview').waitFor();
    await bp.locator('.record-overview').click();
    await keepConversation('.modal');
    await surface().locator('.conversation-panel textarea').fill('Business draft stays');
    const businessScroll = await scrollState('.modal');
    await page.evaluate(() => { window.keepSurface = document.querySelector('.modal .record-surface')._recordSurface; });
    await bp.locator('[data-story-overview]').fill('Unsaved business overview');
    await modal.locator('#td-due').fill('2026-11-11');
    await expectEventually(() => compat.length === 1);
    assert.deepEqual(compat[0].set, {due: '2026-11-11'});
    await expectEventually(() => page.evaluate(() => document.querySelector('.modal #td-due')?.value === '2026-11-11'));
    assert.equal(await sameConversation('.modal'), true, 'the business conversation node survives a generic edit');
    assert.equal(await page.evaluate(() => window.keepSurface === document.querySelector('.modal .record-surface')._recordSurface), true);
    assert.equal(await surface().locator('.conversation-panel textarea').inputValue(), 'Business draft stays');
    assert.equal(await surface().locator('.gchat-scroll').evaluate(el => el.scrollTop), businessScroll);
    assert.equal(await bp.locator('[data-story-overview]').inputValue(), 'Unsaved business overview');
    assert.equal(await bp.isVisible(), true);
    // The controls around the surface are live again, and the panel writes against the new revision.
    await modal.locator('#td-due').fill('2026-11-12');
    await expectEventually(() => compat.length === 2);
    assert.equal(await sameConversation('.modal'), true, 'a second generic edit still keeps the conversation');
    await bp.locator('[data-story-save]').click();
    await expectEventually(() => db.alpha['story-1'].overview === 'Unsaved business overview');
    assert.equal(await bp.getByRole('alert').filter({hasText: 'changed since'}).count(), 0, 'no conflict after generic edits');
    await closeModals();

    // ---- Linked Context opens real content in its own scope, and says so honestly when it is gone.
    const linkAndOpen = async (panel, typeValue, choice, expectText) => {
      await panel.getByRole('button', {name: 'Link Context', exact: true}).click();
      await page.getByLabel('Context type').selectOption(typeValue);
      await page.locator('.record-pick-option', {hasText: choice}).click();
      const open = panel.getByRole('button', {name: choice, exact: true});
      await open.waitFor();
      await open.click();
      await page.getByText(expectText).first().waitFor();
    };
    const closeTop = async () => { await page.locator('.overlay').last().locator('.mx').click(); };

    // Long registered Context: bounded chunks, an explicit continuation and honest completion.
    const readLong = async (panel, label, tag, scopeName) => {
      await linkAndOpen(panel, 'material', label, tag + ' page A');
      const reader = page.locator('.overlay').last();
      assert.equal(await reader.getByText(tag + ' page B').count(), 0, 'only the first bounded chunk is shown at first');
      assert.equal(await reader.locator('[data-record-complete]').count(), 0, 'an unfinished document does not claim to be complete');
      await reader.locator('[data-record-more]').click();
      await reader.getByText(tag + ' page B').waitFor();
      assert.equal(await reader.getByText(tag + ' page A').count(), 1, 'earlier text stays, nothing is duplicated');
      await reader.locator('[data-record-more]').click();
      await reader.getByText(tag + ' page C').waitFor();
      while (await reader.locator('[data-record-more]').count()) { await reader.locator('[data-record-more]').click(); await page.waitForTimeout(50); }
      await reader.locator('[data-record-complete]').waitFor();
      assert.equal(await reader.locator('[data-record-more]').count(), 0);
      const reads = ops('context_get', scopeName).filter(entry => [LONG, GLONG].includes(entry.body.document_id));
      assert.equal(reads.length, 4);
      assert.equal(reads.every(entry => entry.body.scope === (scopeName === 'global' ? 'shared' : 'business')), true);
      assert.deepEqual(reads.map(entry => Boolean(entry.body.cursor)), [false, true, true, true]);
      await closeTop();
    };
    const longReaderCases = async (panel, label, tag, scopeName) => {
      await readLong(panel, label, tag, scopeName);
      // The document changes between pages: the rest is never joined to the old text.
      await panel.getByRole('button', {name: label, exact: true}).first().click();
      const reader = page.locator('.overlay').last();
      await reader.getByText(tag + ' page A').waitFor();
      await reader.locator('[data-record-more]').click(); await reader.getByText(tag + ' page B').waitFor();
      version.bump = true;
      await reader.locator('[data-record-more]').click();
      await reader.locator('[data-record-changed]').waitFor();
      assert.equal(await reader.getByText(tag + ' page C').count(), 0, 'no text from a different version is appended');
      version.bump = false;
      await reader.getByRole('button', {name: 'Read from the start', exact: true}).click();
      await reader.getByText(tag + ' page A').waitFor();
      assert.equal(await reader.getByText(tag + ' page B').count(), 0, 'a restart shows one version only');
      // Closing the reader while a continuation is pending leaves no late result behind.
      flags.holdDocGet = true;
      await reader.locator('[data-record-more]').click();
      await expectEventually(() => flags.releaseDocGet !== null);
      await closeTop();
      flags.releaseDocGet(); flags.releaseDocGet = null;
      await page.waitForTimeout(150);
      assert.equal(await page.getByText(tag + ' page B').count(), 0, 'a late chunk after close paints nothing');
      assert.equal(await page.locator('.record-surface').count() > 0, true, 'the record stays open');
    };
    // A lookup still pending when the record closes opens no stale reader.
    const pendingLookup = async (panel, label, id, leave = closeModals) => {
      const open = panel.getByRole('button', {name: label, exact: true}).first(); await open.waitFor();
      flags.holdItemGet = id;
      await open.click();
      await expectEventually(() => flags.releaseItemGet !== null);
      await leave();
      flags.releaseItemGet(); flags.releaseItemGet = null;
      await page.waitForTimeout(150);
      assert.equal(await page.locator('.overlay').count(), 0, 'closing the record while the lookup is pending opens no modal later');
    };
    log.length = 0;
    await openBusiness('cust-2');
    await surface().getByRole('button', {name: 'Customer context', exact: true}).click();
    const cp = surface().locator('.record-panel');
    await cp.locator('[data-customer-name]').waitFor();
    await linkAndOpen(cp, 'material', 'Founder note', 'Business note body text'); await closeTop();
    await linkAndOpen(cp, 'material', 'Clinic intake upload', 'Business upload body text'); await closeTop();
    await linkAndOpen(cp, 'person', 'Rosa contact', 'Practice owner'); await closeTop();
    await linkAndOpen(cp, 'material', 'Removed material', 'unavailable'); assert.equal(await page.locator('[data-record-unavailable]').count(), 1, 'a removed item shows an honest unavailable state'); await closeTop();
    assert.ok(ops('context_items_get', 'alpha').length >= 4, 'business items are read through the business scope');
    assert.equal(ops('context_get', 'alpha').length >= 1, true);
    assert.deepEqual(ops('notes_get', 'alpha').map(entry => Object.keys(entry.body).sort()), [['document_id', 'idempotency_key']], 'a note read sends only its document id');
    assert.equal(ops('context_items_get', 'global').length, 0, 'a business record never reads Global Context');
    await longReaderCases(cp, 'Business long note', 'Business', 'alpha');
    await pendingLookup(cp, 'Founder note', 'note-1');
    await openBusiness('cust-2');
    await closeModals();
    log.length = 0;
    await page.evaluate(() => openGlobal('cust-g'));
    gs = page.locator('#host .record-surface');
    await gs.getByRole('button', {name: 'Customer context', exact: true}).click();
    const gcp = gs.locator('.record-panel');
    await gcp.locator('[data-customer-name]').waitFor();
    await linkAndOpen(gcp, 'material', 'Global founder note', 'Global note body text'); await closeTop();
    await linkAndOpen(gcp, 'material', 'Shared intake upload', 'Global upload body text'); await closeTop();
    await linkAndOpen(gcp, 'person', 'Sam contact', 'Advisor'); await closeTop();
    await linkAndOpen(gcp, 'material', 'Removed Global material', 'unavailable'); await closeTop();
    assert.ok(ops('context_items_get', 'global').length >= 4, 'Global items are read through the Global scope');
    assert.equal(ops('context_items_get', 'alpha').length, 0, 'a Global record never borrows the selected business');
    assert.equal(ops('context_get', 'global').every(entry => entry.body.scope === 'shared'), true);
    assert.deepEqual(ops('notes_get', 'global').map(entry => Object.keys(entry.body).sort()), [['document_id', 'idempotency_key']], 'a Global note read sends only its document id and is not asked of the registered library');
    assert.equal(ops('context_get', 'global').some(entry => entry.body.document_id === 'gnote-1'), false);
    await longReaderCases(gcp, 'Global long topic', 'Global', 'global');
    await pendingLookup(gcp, 'Global founder note', 'gnote-1', () => page.evaluate(() => openGlobal('story-g')));

    // ---- Real close/navigation tears everything down: no live registrations or stale callbacks afterwards.
    for (let round = 0; round < 3; round++) {
      await openBusiness('story-1');
      await surface().getByRole('button', {name: 'Story dashboard', exact: true}).click();
      await surface().locator('.record-panel .record-overview').waitFor();
      await closeModals();
      await page.evaluate(() => openGlobal('story-g'));
      await page.locator('#host .record-surface .conversation-panel').waitFor();
      await page.locator('#host .record-surface').getByRole('button', {name: 'Story dashboard', exact: true}).click();
      await page.evaluate(() => { document.querySelector('#host').replaceChildren(); });
      await page.evaluate(() => openGlobal('story-g'));   // re-mounting over the same host disposes the previous mount
    }
    await page.evaluate(() => { document.querySelector('#host')._globalTasks.dispose(); document.querySelector('#host').replaceChildren(); });
    assert.equal(await registrations(), 0, 'no connected conversation registration outlives its record');
    // A response that lands after the record was closed neither paints nor starts follow-up reads.
    await openBusiness('story-1');
    flags.holdTaskGet = 'story-1';
    await surface().getByRole('button', {name: 'Story dashboard', exact: true}).click();
    await expectEventually(() => flags.releaseTaskGet !== null);
    await closeModals();
    const readsBefore = log.length;
    flags.releaseTaskGet(); flags.releaseTaskGet = null;
    await page.waitForTimeout(250);
    assert.deepEqual(log.slice(readsBefore).map(entry => entry.op), [], 'a late panel load starts no more requests after close');
    // A late associated answer for a closed Story paints into neither the next Story nor the other scope.
    flags.holdAssociated = 'story-1';
    await openBusiness('story-1');
    await surface().getByRole('button', {name: 'Story dashboard', exact: true}).click();
    await expectEventually(() => !!flags.releaseAssociated, 'the held associated read');
    await closeModals();
    await page.evaluate(() => openGlobal('story-g'));
    const globalSurface = page.locator('#host .record-surface');
    await globalSurface.getByRole('button', {name: 'Story dashboard', exact: true}).click();
    const globalPanel = globalSurface.locator('.record-panel');
    await globalPanel.locator('.record-section').filter({has: page.locator('h4', {hasText: 'Signoffs'})}).getByRole('button', {name: 'Global signoff'}).waitFor();
    flags.releaseAssociated(); flags.releaseAssociated = null;
    await page.waitForTimeout(600);
    assert.equal((await globalPanel.innerText()).includes('Approve the spring budget'), false, 'the closed Story associated response is discarded');
    assert.equal(await globalPanel.locator('.record-link-open:visible', {hasText: 'Approve the spring budget'}).count(), 0, 'no late business row becomes visible');
    assert.equal(await globalPanel.getByRole('button', {name: 'Global signoff'}).isVisible(), true, 'the Global Story keeps its own rows');
    assert.equal(associated.calls.some(entry => entry.namespace === 'global' && entry.record_type === 'signoff'), true, 'a Global Story reads its own scope');
    await page.evaluate(() => { document.querySelector('#host')._globalTasks?.dispose?.(); document.querySelector('#host').replaceChildren(); });
    if (process.env.DBG) console.log(JSON.stringify(await page.evaluate(() => [...conversationStates.entries()].map(([k, st]) => [k, [...st.mounts].filter(n => n.isConnected).map(n => (n.parentElement?.className || '') + '|' + n.id + '|' + n.className)]).filter(e => e[1].length))));
    assert.equal(await registrations(), 0);
    assert.deepEqual(errors, []);

    // ---- The obsolete Focus side-panel wrapper is gone; the shared workspace owns record panes now.
    for (const gone of ['agfocusRecordSurface', 'agfocusThreadHeading', 'agfocusOpenRecord', 'agfocusLoadThreadContext'])
      assert.equal(await page.evaluate(name => typeof window[name] === 'function', gone), false, 'Removed Focus wrapper: ' + gone);
    assert.equal(await page.locator('[data-manage],.agfocus-thread-context,.record-toggle[aria-label="Story dashboard"]').count(), 0);

    // ---- Narrow layout stacks the panel without losing the conversation.
    await page.setViewportSize({width: 520, height: 900});
    await page.evaluate(() => openGlobal('story-g'));
    const narrow = page.locator('#host .record-surface');
    await narrow.getByRole('button', {name: 'Story dashboard', exact: true}).click();
    await narrow.locator('.record-panel').waitFor({state: 'visible'});
    const stacked = await narrow.evaluate(el => { const p = el.querySelector('.record-panel').getBoundingClientRect(), m = el.querySelector('.record-main').getBoundingClientRect(); return p.bottom <= m.top + 1 && Math.abs(p.left - m.left) < 4; });
    assert.equal(stacked, true, 'a narrow surface stacks the panel above the conversation');
    await page.screenshot({path: path.join(artifacts, 'story-panel-narrow.png')});
    await narrow.getByRole('button', {name: 'Story dashboard', exact: true}).click();
    assert.equal(await narrow.locator('.conversation-panel textarea').isVisible(), true);

    assert.deepEqual(errors, []);
    console.log('Record panels (outside Focus/Threads): thread-first opens, left panel and stable conversation, Story read/edit, Customer editing and states, historical email reference, links and activity, Ordubashy-only sends, stale guards and narrow layout passed.');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });

async function expectEventually(check, what = 'condition') {
  for (let i = 0; i < 200; i++) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 25)); }
  throw new Error('Timed out waiting for ' + what);
}
