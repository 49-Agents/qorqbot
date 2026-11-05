// The composed qualification: the real combined dashboard talks to the real composed owner HTTP application.
//
// No product API response is intercepted, stubbed or fabricated. The browser is served by the actual composed host
// (which serves the dashboard's own files); requests outside that synthetic origin are blocked. The small
// /__fixture control channel belongs to the Python harness and only applies stored-state changes a concurrent writer
// or an authorization change would cause; it never answers a product read and never recreates an operation handler.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const base = process.env.PROJECT_COMPOSITION_BASE;
const fixture = JSON.parse(fs.readFileSync(process.env.PROJECT_COMPOSITION_FIXTURE, 'utf8'));
const shots = process.env.PROJECT_COMPOSITION_SHOTS || null;
const origin = new URL(base).origin;
const P = fixture.projects;

const shot = async (target, name) => { if (shots) await target.screenshot({path: path.join(shots, name)}); };
const waitFor = async (check, what) => {
  for (let i = 0; i < 400; i++) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 25)); }
  throw new Error('Timed out waiting for ' + what);
};
const decode = body => { try { return JSON.parse(body || 'null'); } catch (_) { return null; } };
const scopeKey = business => business || 'global';
const sequenceOf = (rows, type, id) => (rows.find(row => row.source.type === type && row.source.id === id) || {}).latest_sequence ?? 0;
const countVisible = locator => locator.evaluateAll(nodes => nodes.filter(node => node.offsetParent !== null).length);

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 950}});
    page.setDefaultTimeout(30000);
    const errors = [], expected = [], notes = [], calls = [], captured = [];
    page.on('pageerror', error => errors.push('pageerror: ' + error.message));
    page.on('requestfailed', request => {
      const url = request.url();
      if (new URL(url).origin !== origin) return notes.push('blocked external request: ' + url);
      // A speculative queue read in flight when its panel is disposed is cancelled, not failed.
      if (String(request.failure()?.errorText || '').includes('ERR_ABORTED')) return notes.push('cancelled speculative read: ' + url);
      errors.push('requestfailed: ' + url + ' (' + (request.failure()?.errorText || '') + ')');
    });
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.origin !== origin || request.method() !== 'POST') return;
      calls.push({path: url.pathname, body: decode(request.postData())});
    });
    page.on('response', async response => {
      const url = new URL(response.url());
      if (url.origin !== origin) return;
      if (response.status() >= 400) {
        // The agent pane's own best-effort workspace declaration falls back to its built-in Focus view
        // when the business-scoped read has no Global grant at the root. That is a documented fallback, not a
        // product response used by this qualification.
        if (url.pathname.endsWith('/operations/workspace_types') && response.status() === 403) {
          notes.push('agent-pane workspace declaration fell back to its built-in views (workspace_types 403)');
        } else if (response.status() === 403 && url.pathname.endsWith('/operations/owner_conversation_get')) {
          // Only the deliberately retained foreign pane may lose its private channel after the membership change.
          expected.push({section: 'revoked-foreign-chat', path: url.pathname,
            body: decode(response.request().postData())});
        } else if (response.status() === 409 && url.pathname.endsWith('/operations/tasks_update')) {
          // The two deliberate stale-revision writes prove the canonical conflict path without overwriting.
          expected.push({section: 'stale-revision', path: url.pathname, body: decode(response.request().postData())});
        } else errors.push('http ' + response.status() + ': ' + url.pathname);
      }
      if (response.status() < 300 && /\/operations\/(context_get|project_item_get|project_items_list|tables_rows_list|routines_runs|jobs_get|dev_tickets_list|owner_conversations_list|relationships_list)$/.test(url.pathname)) {
        try { captured.push({path: url.pathname, body: await response.json()}); } catch (_) { /* no body */ }
      }
    });

    // The browser may only reach the synthetic composed origin; every external request is aborted.
    await page.route('**/*', route => new URL(route.request().url()).origin === origin
      ? route.continue() : route.abort());

    const pane = () => page.locator('.agfocus-dock .rw-record:not([hidden])').first();
    const focusCard = label => page.locator('.agfocus-items .agfocus-card').filter({has: page.locator('.ttl', {hasText: label})}).first();
    const opCalls = suffix => calls.filter(call => call.path.endsWith('/operations/' + suffix));
    const lastCall = suffix => opCalls(suffix).at(-1);
    const capturedFor = (suffix, predicate = () => true) => captured.filter(row => row.path.endsWith('/operations/' + suffix) && predicate(row.body));
    const providerCalls = () => calls.filter(call => /whatsapp_send|account_email|signoffs_review|signoffs_reject$/.test(call.path));
    const directory = async () => {
      const result = await page.evaluate(async () => {
        const response = await fetch('/api/root/operations/owner_conversations_list', {method: 'POST',
          headers: {'Content-Type': 'application/json'}, body: JSON.stringify({scope: 'global'})});
        return {status: response.status, body: await response.json()};
      });
      assert.equal(result.status, 200);
      return result.body.data.items;
    };
    const control = async (action, body) => {
      const result = await page.evaluate(async ([action, body]) => {
        const response = await fetch('/__fixture/' + action, {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body)});
        return {status: response.status, body: await response.json()};
      }, [action, body]);
      assert.equal(result.status, 200, JSON.stringify(result));
      return result.body;
    };
    const closeFocus = async () => {
      await pane().locator('.rw-close').click();
      await page.waitForFunction(() => document.querySelector('.agfocus .agfocus-dock').hidden);
    };
    const goFocus = async () => { await page.locator('.ttab[data-v="focus"]').click(); await page.locator('.agfocus-items').waitFor(); };
    const conversationTitles = async () => (await directory()).map(row => row.resolved_source.label);
    const openConversation = async title => {
      const row = (await directory()).find(row => row.resolved_source.label === title);
      assert(row, 'Existing record conversation: ' + title);
      await page.evaluate(async row => {
        agfocusShowDock(_agpad);
        await _agpad.focusWorkspace.open(rwRecordFromItem(row, {can_thread: true}), {root: true});
      }, row);
      await pane().waitFor();
    };
    const sendMessage = async text => {
      const composer = pane().locator('.rw-chat .agfocus-conversation > .conversation-inline-compose textarea');
      await composer.waitFor();
      await composer.fill(text);
      await pane().locator('.rw-chat .agfocus-conversation > .conversation-inline-compose .gchat-composer').getByRole('button', {name: 'Send', exact: true}).click();
      await pane().locator('.rw-chat').getByText(text, {exact: true}).first().waitFor();
    };
    const openProjectDashboard = async () => {
      await pane().locator('[data-rw-tab="dashboard"]').click();
      await pane().locator('.rw-project-name').waitFor();
    };
    const section = async name => { await pane().locator(`[data-project-section="${name}"]`).click(); };
    const projectRow = label => pane().locator('.rw-project-row').filter({has: page.locator('.rw-project-open', {hasText: label})}).first();
    const openProjectRow = async label => { await projectRow(label).locator('.rw-project-open').click(); };

    await page.goto(base + '/#/global/agents');
    await page.locator('[data-focus-add]').waitFor();
    assert.equal(await page.locator('.agfocus-items .agfocus-card').count(), 0, 'Focus starts empty');

    // ----------------------------------------------------------------------------------------------------------
    // A. Reading a Project's record and inventory never pins it to Focus and never creates a Threads entry.
    // ----------------------------------------------------------------------------------------------------------
    await page.evaluate(async ([id, label]) => {
      agfocusShowDock(_agpad);
      await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'project', id, business_id: id}, label}, {can_thread: true}));
    }, [P.maple, 'Maple Works']);
    await pane().waitFor();
    await pane().locator('[data-rw-tab="chat"]').waitFor();
    await openProjectDashboard();
    assert.equal(await pane().locator('.rw-project-name').textContent(), 'Maple Works');
    await section('work');
    await projectRow('Maple shared deliverable').waitFor();
    assert.equal(await page.locator('.agfocus-items .agfocus-card').count(), 0, 'an inventory read does not pin the Project');
    assert.equal(opCalls('focus_present').length, 0, 'no focus_present was sent by reading');
    assert.equal((await directory()).some(row => row.source.type === 'project' && row.source.id === P.maple), false,
      'reading an empty Project channel creates no Threads entry');
    await closeFocus();

    // ----------------------------------------------------------------------------------------------------------
    // B. Pin the actual Project through the UI (explicit presentation only) and open its Chat.
    // ----------------------------------------------------------------------------------------------------------
    await page.locator('[data-focus-add]').click();
    const add = page.locator('.modal');
    await add.waitFor();
    await add.locator('[name="kind"][value="project"]').check();
    const projectOptions = await add.locator('[name="project"] option').evaluateAll(nodes => nodes.map(node => node.textContent));
    assert.deepEqual(projectOptions, ['Harbor Light', 'Maple Works'], 'only accessible actual Projects are offered');
    await add.locator('[name="project"]').selectOption(P.harbor);
    await add.getByRole('button', {name: 'Add'}).click();
    await add.waitFor({state: 'detached'});
    const harborCard = focusCard('Harbor Light');
    await harborCard.waitFor();
    const projectCardId = await harborCard.getAttribute('data-card-id');
    assert.equal(await harborCard.locator('.ttl').textContent(), 'Harbor Light', 'the real Project name wins');
    await waitFor(() => opCalls('focus_present').length === 1, 'focus_present');
    assert.deepEqual(lastCall('focus_present').body.source, {type: 'project', id: P.harbor, business_id: P.harbor});
    await shot(page, 'focus-project-pinned-desktop.png');

    // Clicking the card opens the workspace with Chat by default; only the chevron expands.
    const workReadsBefore = opCalls('project_items_list').filter(call => call.body?.root_tasks_only).length;
    await harborCard.locator('.ttl').click();
    await pane().waitFor();
    assert.equal(await pane().locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true', 'Chat is the default tab');
    assert.equal(await pane().locator('[data-rw-tab="dashboard"]').count(), 1);
    assert.equal(await pane().locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Project dashboard');
    assert.equal(await opCalls('project_items_list').filter(call => call.body?.root_tasks_only).length, workReadsBefore,
      'selecting the card does not expand Project work');
    assert.equal(await pane().locator('.rw-details, [data-record-details], .context-disclosure').count(), 0, 'no obsolete Details/Context surface');
    await sendMessage('Harbor owner hello');
    assert.deepEqual(lastCall('owner_conversation_send').body.source, {type: 'project', id: P.harbor, business_id: P.harbor});
    await closeFocus();
    await goFocus();
    assert.equal((await directory()).some(row => row.source.type === 'project' && row.source.id === P.harbor), true,
      'the first real private message makes the Project Thread appear');

    // ----------------------------------------------------------------------------------------------------------
    // C. Expand the Focus Project: top-level Task/Story/Customer work, scoped hierarchy, >25 rows and pagination.
    // ----------------------------------------------------------------------------------------------------------
    await goFocus();
    await page.locator(`[data-card-id="${projectCardId}"] .agfocus-project-toggle`).click();
    const derived = (kind, business, id) => page.locator(`[data-card-id="${projectCardId}/work/${kind}:${scopeKey(business)}:${id}"]`);
    const derivedVisible = async (kind, business, id) => countVisible(derived(kind, business, id));
    await derived('task', P.harbor, fixture.bulk.ids[0]).waitFor();
    const expansion = lastCall('project_items_list');
    assert.equal(expansion.path, '/api/root/operations/project_items_list');
    assert.deepEqual({project_id: expansion.body.project_id, section: expansion.body.section,
      root_tasks_only: expansion.body.root_tasks_only, include_closed: expansion.body.include_closed,
      include_archived: expansion.body.include_archived, limit: expansion.body.limit, scope: expansion.body.scope},
      {project_id: P.harbor, section: 'work', root_tasks_only: true, include_closed: true, include_archived: true,
        limit: 25, scope: 'global'});
    assert.equal(await derivedVisible('task', P.harbor, fixture.story.id), 1, 'the Story is top-level Project work');
    assert.equal(await derivedVisible('task', P.harbor, fixture.customer.id), 1, 'the Customer is top-level Project work');
    assert.equal(await derivedVisible('task', P.harbor, fixture.child.id), 0, 'a Story child is not a top-level row');
    await derived('task', P.harbor, fixture.owned_linked.id).waitFor();
    assert.equal(await derivedVisible('task', P.maple, fixture.foreign.id), 1, 'a linked foreign Task appears with its own scope');
    const globalLinkedRow = derived('task', null, fixture.global_linked.id);
    await globalLinkedRow.waitFor();
    assert.equal(await countVisible(page.locator(`[data-card-id$="${fixture.global_linked.id}"]`)), 1,
      'a linked Global Task appears once with its null-UUID scope');
    assert.equal(await countVisible(page.locator(`[data-card-id$="${fixture.shared.id}"]`)), 2,
      'the same id in two scopes is never collapsed');
    assert.equal(await derivedVisible('task', P.harbor, fixture.shared.id), 1);
    assert.equal(await derivedVisible('task', P.maple, fixture.shared.id), 1);
    // The hierarchy stays scoped: expanding the Story reads only its children, under the Story's own path.
    const storyRowId = `${projectCardId}/work/task:${P.harbor}:${fixture.story.id}`;
    await page.locator(`[data-card-id="${storyRowId}"] .task-subtasks-toggle`).click();
    await page.locator(`[data-card-id="${storyRowId}/task/task:${P.harbor}:${fixture.child.id}"]`).waitFor();
    assert.equal(lastCall('tasks_list').body.parent_id, fixture.story.id);
    // The same shared projection labels the Focus related Action chip by its text, not its identifier.
    await page.locator('.agfocus-reference-chip').filter({hasText: fixture.action.text}).waitFor();
    assert.equal(await page.locator('.agfocus-reference-chip').filter({hasText: fixture.action.id}).count(), 0,
      'the Focus related Action chip never falls back to the raw identity');
    assert.equal(await page.locator('.agfocus-reference-chip').filter({hasText: 'Send the harbor update'}).count(), 1,
      'the Story Signoff reference keeps its canonical title');
    // Pagination continues beyond the first 25 roots.
    assert.equal(await page.locator('.agfocus-work-more').count() >= 1, true, 'a second inventory page is available');
    const firstPage = opCalls('project_items_list').filter(call => call.body?.root_tasks_only).at(-1);
    await page.locator('.agfocus-work-more').first().click();
    await waitFor(() => opCalls('project_items_list').filter(call => call.body?.root_tasks_only).length > 1, 'second page');
    const secondPage = opCalls('project_items_list').filter(call => call.body?.root_tasks_only).at(-1);
    assert.ok(secondPage.body.cursor, 'the continuation reads the returned cursor');
    assert.notEqual(secondPage.body.cursor, firstPage.body.cursor);
    await derived('task', P.harbor, fixture.bulk.ids[26]).waitFor();
    await shot(page, 'focus-project-expanded-desktop.png');

    // The Story and ordinary Task are both reachable through Focus (Project expansion).
    await derived('task', P.harbor, fixture.story.id).locator('.ttl').click();
    await pane().waitFor();
    assert.equal(await pane().locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true');
    await closeFocus();
    await derived('task', P.harbor, fixture.ordinary.id).locator('.ttl').click();
    await pane().waitFor();
    assert.equal(await pane().locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true');
    await sendMessage('Ordinary task hello');
    await closeFocus();
    await derived('task', null, fixture.global_linked.id).locator('.ttl').click();
    await pane().waitFor();
    assert.equal(await pane().locator('.rw-title').textContent(), fixture.global_linked.title);
    assert.equal(await pane().locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true');
    assert.equal(await pane().locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Task dashboard');
    await closeFocus();

    // ----------------------------------------------------------------------------------------------------------
    // D. The Project dashboard: real section/candidate/link/unlink envelopes, explicit Global scope.
    // ----------------------------------------------------------------------------------------------------------
    await harborCard.locator('.ttl').click();
    await pane().waitFor();
    await openProjectDashboard();
    assert.equal(await pane().locator('.rw-project-name').textContent(), 'Harbor Light');
    assert.match(await pane().locator('.rw-project-summary').textContent(), /synthetic harbor pilot/);
    const recordRead = lastCall('project_record_get');
    assert.equal(recordRead.path, '/api/root/operations/project_record_get');
    assert.deepEqual({scope: recordRead.body.scope, project_id: recordRead.body.project_id},
      {scope: 'global', project_id: P.harbor});
    const listRead = lastCall('project_items_list');
    assert.deepEqual({section: listRead.body.section, scope: listRead.body.scope, limit: listRead.body.limit},
      {section: 'work', scope: 'global', limit: 25});
    await section('work');
    await projectRow('Owned and linked deliverable').waitFor();
    assert.match(await projectRow('Owned and linked deliverable').locator('.rw-project-assoc').textContent(), /Owned · Linked/,
      'ownership and an explicit link yield one row with both flags');
    assert.equal(await page.getByText('Hidden deliverable', {exact: true}).count(), 0, 'a denied linked target emits no title');
    await shot(pane(), 'project-dashboard-desktop.png');

    // Link a Global record from the UI; explicit null scope is sent and the target keeps its own scope.
    await pane().locator('[data-project-link]').click();
    const linkDialog = page.locator('.modal').last();
    await linkDialog.waitFor();
    await linkDialog.locator('[data-link-scope]').selectOption('');
    await linkDialog.locator('[data-link-type]').selectOption('task');
    await waitFor(() => calls.some(call => call.path.endsWith('/operations/project_items_candidates')
      && call.body?.type === 'task' && call.body?.source_business_id === null), 'the explicit Global candidate read');
    const globalCandidates = calls.filter(call => call.path.endsWith('/operations/project_items_candidates')
      && call.body?.type === 'task' && call.body?.source_business_id === null).at(-1);
    assert.equal(globalCandidates.path, '/api/root/operations/project_items_candidates');
    assert.deepEqual({project_id: globalCandidates.body.project_id, source_business_id: globalCandidates.body.source_business_id,
      type: globalCandidates.body.type, limit: globalCandidates.body.limit},
      {project_id: P.harbor, source_business_id: null, type: 'task', limit: 25});
    await linkDialog.locator('.rw-link-option').filter({hasText: fixture.global.title}).click();
    await linkDialog.waitFor({state: 'detached'});
    await projectRow(fixture.global.title).waitFor();
    const globalLink = lastCall('project_items_link');
    assert.equal(globalLink.path, '/api/root/operations/project_items_link');
    assert.deepEqual({project_id: globalLink.body.project_id, item: globalLink.body.item},
      {project_id: P.harbor, item: {type: 'task', id: fixture.global.id, business_id: null}});
    assert.match(globalLink.body.idempotency_key, /^[0-9a-f-]{36}$/);
    assert.equal(await projectRow(fixture.global.title).locator('.rw-project-scope').textContent(), 'Global');
    const globalRow = capturedFor('project_items_list', body => (body.data?.items || []).some(item => item.id === fixture.global.id))
      .flatMap(row => row.body.data.items).find(item => item.id === fixture.global.id);
    assert.equal(globalRow.business_id, null);
    // Unlink it through the same real envelope, using the exact visible link revision.
    await projectRow(fixture.global.title).locator('.rw-project-unlink').click();
    await waitFor(async () => await projectRow(fixture.global.title).count() === 0, 'Global row removal');
    const unlink = lastCall('project_items_unlink');
    assert.deepEqual({project_id: unlink.body.project_id, link_id: unlink.body.link_id,
      expected_revision: unlink.body.expected_revision},
      {project_id: P.harbor, link_id: globalRow.association.link_id, expected_revision: globalRow.association.link_revision});
    assert.match(unlink.body.idempotency_key, /^[0-9a-f-]{36}$/);

    // Link then unlink a foreign Project's record; its source ownership and parent never change.
    await pane().locator('[data-project-link]').click();
    const foreignDialog = page.locator('.modal').last();
    await foreignDialog.waitFor();
    await foreignDialog.locator('[data-link-scope]').selectOption(P.maple);
    await foreignDialog.locator('[data-link-type]').selectOption('task');
    await waitFor(() => calls.some(call => call.path.endsWith('/operations/project_items_candidates')
      && call.body?.type === 'task' && call.body?.source_business_id === P.maple), 'the explicit foreign candidate read');
    const foreignCandidates = calls.filter(call => call.path.endsWith('/operations/project_items_candidates')
      && call.body?.type === 'task' && call.body?.source_business_id === P.maple).at(-1);
    assert.deepEqual({source_business_id: foreignCandidates.body.source_business_id, type: foreignCandidates.body.type},
      {source_business_id: P.maple, type: 'task'});
    await foreignDialog.locator('.rw-link-option').filter({hasText: fixture.foreign_candidate.title}).click();
    await foreignDialog.waitFor({state: 'detached'});
    await projectRow(fixture.foreign_candidate.title).waitFor();
    const foreignLink = lastCall('project_items_link');
    assert.deepEqual(foreignLink.body.item, {type: 'task', id: fixture.foreign_candidate.id, business_id: P.maple});
    assert.equal(await projectRow(fixture.foreign_candidate.title).locator('.rw-project-scope').textContent(), 'Maple Works');
    await projectRow(fixture.foreign_candidate.title).locator('.rw-project-unlink').click();
    await waitFor(async () => await projectRow(fixture.foreign_candidate.title).count() === 0, 'foreign row removal');

    // Open a pre-linked foreign Task, then revoke the owner's reach in the real app: the retained pane cannot show
    // stale content and the next real inventory read omits the target without a title.
    await projectRow(fixture.foreign.title).locator('.rw-project-open').click();
    await pane().waitFor();
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.tdetail, .record-overview, [data-editor]').first().waitFor();
    assert.match(await pane().locator('.rw-title').textContent(), /Maple shared deliverable/);
    const beforeBack = opCalls('project_items_list').length;
    await pane().locator('.rw-back').click();
    await pane().locator('.rw-project-name').waitFor();
    // The reveal's own revalidation finishes before the authorization change; the cached row then remains
    // mounted until it is selected, which is exactly the retained-denial case.
    await waitFor(() => opCalls('project_items_list').length > beforeBack, 'the reveal revalidation');
    await page.waitForTimeout(150);
    await control('revoke-membership', {business_id: P.maple});
    if (await projectRow(fixture.foreign.title).count()) {
      await projectRow(fixture.foreign.title).locator('.rw-project-open').click();
      await page.waitForTimeout(400);
      assert.equal(await pane().locator('.rw-title').filter({hasText: /Maple shared deliverable/}).count(), 0,
        'a denied retained item does not stay in the pane');
    }
    assert.equal(await pane().locator('.rw-project-name').count(), 1, 'the Project pane remains');
    await section('actions');
    await section('work');
    await pane().locator('.rw-project-name').waitFor();
    assert.equal(await projectRow(fixture.foreign.title).count(), 0, 'the denied target disappeared from the fresh inventory');
    const paneShows = async text => pane().getByText(text, {exact: true})
      .evaluateAll(nodes => nodes.filter(node => node.offsetParent !== null).length);
    assert.equal(await paneShows('Maple shared deliverable'), 0, 'no denied title is leaked by the dashboard');
    assert.equal(await paneShows('Hidden deliverable'), 0, 'no revoked target title is leaked by the dashboard');
    await closeFocus();
    // The Focus expansion's own mounted snapshot is a cache too: a fresh expansion read cannot resurrect the target.
    const beforeRefresh = opCalls('project_items_list').filter(call => call.body?.root_tasks_only).length;
    await page.locator(`[data-card-id="${projectCardId}"] .agfocus-project-toggle`).click();
    await page.locator(`[data-card-id="${projectCardId}"] .agfocus-project-toggle`).click();
    await waitFor(() => opCalls('project_items_list').filter(call => call.body?.root_tasks_only).length > beforeRefresh,
      'a fresh Project expansion read');
    assert.equal(await derivedVisible('task', P.maple, fixture.foreign.id), 0, 'a fresh expansion omits the denied target');
    assert.equal(await page.locator('.agfocus-items').getByText('Maple shared deliverable', {exact: true})
      .evaluateAll(nodes => nodes.filter(node => node.offsetParent !== null).length), 0,
      'the Focus board leaks no denied title');
    await control('restore-membership', {business_id: P.maple});
    await harborCard.locator('.ttl').click();
    await pane().waitFor();
    await openProjectDashboard();
    await section('work');
    await projectRow(fixture.foreign.title).waitFor();

    // ----------------------------------------------------------------------------------------------------------
    // E. The actual Project readers: Action, paged Context document, table rows, reminder, routine/job, Dev report.
    // ----------------------------------------------------------------------------------------------------------
    await section('actions');
    await openProjectRow(fixture.action.text);
    await pane().waitFor();
    await pane().locator('.ac-action-reader .rw-action-text').waitFor();
    assert.match(await pane().locator('.ac-action-reader').textContent(), /Confirmed the harbor pilot window with the supplier/);
    assert.deepEqual(lastCall('project_item_get').body.item, {type: 'action', id: fixture.action.id, business_id: null});
    await pane().locator('.rw-back').click();
    await pane().locator('.rw-project-name').waitFor();

    await section('documents');
    await openProjectRow('Harbor pilot brief');
    await pane().waitFor();
    await pane().locator('.rw-document .work-source').waitFor();
    assert.equal(await pane().locator('.rw-document [data-record-complete]').count(), 0, 'a long document is not shown whole');
    assert.equal(await pane().locator('.rw-document [data-record-more]').count(), 1, 'the canonical continuation control is present');
    const firstEnvelope = capturedFor('project_item_get', body => body.data?.item?.id === fixture.document.id)
      .at(-1).body.data.record;
    assert.equal(firstEnvelope.document.character_offset, 0);
    assert.ok(firstEnvelope.document.content_hash);
    assert.equal(firstEnvelope.complete, false);
    assert.ok(firstEnvelope.next_cursor, 'the top-level complete/next_cursor contract is kept');
    const firstText = await pane().locator('.rw-document .work-source').textContent();
    for (let pageNumber = 0; pageNumber < 8 && !(await pane().locator('.rw-document [data-record-complete]').count()); pageNumber++) {
      await pane().locator('.rw-document [data-record-more]').click();
      await page.waitForTimeout(120);
    }
    await pane().locator('.rw-document [data-record-complete]').waitFor();
    const fullText = await pane().locator('.rw-document .work-source').textContent();
    assert.ok(fullText.length > firstText.length && fullText.length > 6000, 'Read more joins the bounded chunks');
    assert.match(fullText, /Section 17:/);
    const continuations = capturedFor('context_get').map(row => row.body.data);
    assert.equal(continuations[0].document.character_offset, Array.from(firstText).length);
    assert.ok(continuations.length >= 2);
    assert.equal(continuations.at(-1).complete, true, 'the last chunk closes the canonical envelope');
    assert.ok(continuations.slice(1).every(row => row.document.character_offset > continuations[0].document.character_offset));
    assert.ok(continuations.every(row => row.document.content_hash === firstEnvelope.document.content_hash));
    assert.equal(lastCall('context_get').body.scope, 'business', 'the Project document is read in its own canonical scope');
    assert.ok(lastCall('context_get').body.cursor);
    await shot(pane(), 'project-document-desktop.png');
    await pane().locator('.rw-back').click();
    await pane().locator('.rw-project-name').waitFor();

    await section('tables');
    await openProjectRow('Harbor launch plan');
    await pane().waitFor();
    // Tables now have their own private Chat; select the data dashboard explicitly.
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.rw-table tbody tr').first().waitFor();
    assert.match(await pane().locator('.rw-table tbody tr').first().textContent(), /Channel 1/);
    assert.equal(await pane().locator('.rw-table tbody tr').count(), 50);
    const tableFirst = lastCall('tables_rows_list');
    assert.equal(tableFirst.body.limit, 50);
    await pane().locator('.rw-table-reader').getByRole('button', {name: 'More rows', exact: true}).click();
    await waitFor(async () => (await pane().locator('.rw-table tbody tr').count()) === 60, 'second table page');
    assert.match(await pane().locator('.rw-table tbody tr').last().textContent(), /Channel 60/);
    assert.ok(lastCall('tables_rows_list').body.cursor);
    assert.equal(capturedFor('tables_rows_list').at(-1).body.data.items.length, 10);
    await shot(pane(), 'project-table-desktop.png');
    await pane().locator('.rw-back').click();
    await pane().locator('.rw-project-name').waitFor();

    await section('other');
    await openProjectRow('Call the harbor pilot owner');
    await pane().waitFor();
    assert.equal(await pane().locator('.rw-reminder .rw-reminder-text').textContent(), 'Call the harbor pilot owner');
    assert.match(await pane().locator('.rw-reminder .rw-reminder-meta').textContent(), /Scheduled for:/);
    assert.equal(await pane().locator('[data-rw-tab="chat"]').count(), 0, 'a reminder opens only its dashboard');
    await pane().locator('.rw-back').click();
    await pane().locator('.rw-project-name').waitFor();
    await projectRow('Harbor checkout retry loop').waitFor();
    await openProjectRow('Harbor checkout retry loop');
    await pane().waitFor();
    await pane().locator('.rw-ticket .rw-ticket-body').first().waitFor();
    const ticketText = await pane().locator('.rw-ticket').textContent();
    assert.match(ticketText, /First paragraph of the harbor report/);
    assert.match(ticketText, /Second paragraph records the retry evidence/);
    assert.match(ticketText, /Replaced the retry guard and verified the harbor flow/);
    assert.equal(await pane().locator('.rw-ticket').getByRole('button').count(), 0, 'the Dev report is read-only');
    await pane().locator('.rw-back').click();
    await pane().locator('.rw-project-name').waitFor();

    await section('automation');
    await openProjectRow('Harbor pilot digest');
    await pane().waitFor();
    await pane().locator('.rw-routine .rw-run-row').first().waitFor();
    assert.equal(await pane().locator('.rw-routine .rw-run-row').count(), 25, 'the first runs page is bounded');
    assert.deepEqual({routine_id: lastCall('routines_runs').body.routine_id, limit: lastCall('routines_runs').body.limit},
      {routine_id: fixture.routine.id, limit: 25});
    assert.equal(await pane().locator('.rw-routine .rw-run-job').count(), 0, 'the referenced job starts on the continuation page');
    const firstRuns = lastCall('routines_runs');
    await pane().locator('.rw-routine [data-routine-more]').click();
    await pane().locator('.rw-routine .rw-run-job').first().waitFor();
    const secondRuns = lastCall('routines_runs');
    assert.ok(secondRuns.body.cursor && secondRuns.body.cursor !== firstRuns.body.cursor);
    assert.ok(await pane().locator('.rw-routine .rw-run-row').count() > 25);
    await pane().locator('.rw-routine .rw-run-job').first().click();
    await pane().waitFor();
    await pane().locator('.rw-job').waitFor();
    const jobMeta = await pane().locator('.rw-job').textContent();
    assert.match(jobMeta, /Kind/);
    assert.match(jobMeta, /State/);
    assert.match(jobMeta, /succeeded/i);
    assert.match(jobMeta, /Attempts/);
    assert.equal(lastCall('jobs_get').body.job_id, fixture.routine.job_id);
    await shot(pane(), 'project-routine-job-desktop.png');
    await pane().locator('.rw-back').click();
    await pane().locator('.rw-routine').waitFor();
    await pane().locator('.rw-back').click();
    await pane().locator('.rw-project-name').waitFor();
    await closeFocus();

    // The owner's standalone Dev report read uses the exact ticket_id selector and checks the actual source scope.
    await page.evaluate(async id => {
      agfocusShowDock(_agpad);
      await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'dev_ticket', id, business_id: null},
        label: 'Standalone report'}, {can_thread: false}));
    }, fixture.standalone_ticket.id);
    await pane().waitFor();
    await pane().locator('.rw-ticket').waitFor();
    const standaloneRead = lastCall('dev_tickets_list');
    assert.equal(standaloneRead.path, '/api/root/operations/dev_tickets_list');
    assert.deepEqual({ticket_id: standaloneRead.body.ticket_id, status: standaloneRead.body.status,
      details: standaloneRead.body.details}, {ticket_id: fixture.standalone_ticket.id, status: 'all', details: true});
    assert.match(await pane().locator('.rw-ticket').textContent(), /The standalone second paragraph/);
    await closeFocus();
    // A Project Dev report opened as a standalone Global reference is refused by the reader's own scope check.
    await page.evaluate(async id => {
      agfocusShowDock(_agpad);
      await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'dev_ticket', id, business_id: null},
        label: 'Wrong scope report'}, {can_thread: false}));
    }, fixture.ticket.id);
    await page.waitForTimeout(400);
    assert.equal(await page.locator('.rw-ticket').filter({hasText: 'Harbor checkout retry loop'}).count(), 0,
      'a Project report is not shown by a standalone Global selector');

    // ----------------------------------------------------------------------------------------------------------
    // F. Story through Focus and existing conversations; overview edit, child hierarchy, related records, draft and Back.
    // ----------------------------------------------------------------------------------------------------------
    await goFocus();
    await harborCard.locator('.ttl').click();
    await pane().waitFor();
    await openProjectDashboard();
    await section('work');
    await projectRow(fixture.story.title).locator('.rw-project-open').click();
    await pane().waitFor();
    assert.equal(await pane().locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true', 'Chat is default from the Project row');
    assert.equal(await pane().locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Story dashboard');
    const storyRelated = await directory();
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.record-overview').waitFor();
    assert.match(await pane().locator('.record-overview').textContent(), /First paragraph of the launch story/);
    assert.match(await pane().locator('.record-overview').textContent(), /Second paragraph with the decision context/);
    await pane().locator('.record-child-row').filter({hasText: fixture.child.title}).waitFor();
    const signoffSection = pane().locator('.record-section').filter({has: page.locator('h4', {hasText: 'Signoffs'})});
    const actionSection = pane().locator('.record-section').filter({has: page.locator('h4', {hasText: 'Actions'})});
    await signoffSection.locator('.record-link-open').filter({hasText: 'Send the harbor update'}).waitFor();
    assert.match(await signoffSection.textContent(), /Send the harbor update/);
    assert.match(await signoffSection.textContent(), /Signoff · pending/);
    const actionRow = actionSection.locator('.record-link-open').filter({hasText: fixture.action.text});
    await actionRow.waitFor();
    assert.match(await actionSection.textContent(), /Action/);
    assert.equal(await actionSection.textContent().then(text => text.includes(fixture.action.id)), false,
      'the Story Action row does not show the raw global identifier');
    await actionRow.click();
    await pane().locator('.ac-action-reader .rw-action-text').waitFor();
    assert.match(await pane().locator('.ac-action-reader').textContent(), /Confirmed the harbor pilot window with the supplier/);
    assert.equal(lastCall('actions_get').body.action_id, fixture.action.id, 'the related row opens the canonical Action reader');
    await pane().locator('.rw-back').click();
    await pane().locator('.record-overview').waitFor();
    const afterStoryDashboard = await directory();
    assert.deepEqual(afterStoryDashboard.map(row => row.latest_sequence), storyRelated.map(row => row.latest_sequence),
      'opening a dashboard adds no message and changes no thread activity');
    assert.equal(providerCalls().length, 0, 'opening related records starts no provider operation');
    await shot(pane(), 'story-dashboard-desktop.png');

    // Edit the overview with a real revision-checked write; keep a Chat draft through the round trip.
    await pane().locator('[data-rw-tab="chat"]').click();
    await pane().locator('.rw-chat .agfocus-conversation > .conversation-inline-compose textarea').fill('Story chat draft retained');
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.record-overview').click();
    await pane().locator('[data-story-overview]').fill('The composed Story overview was saved from the real dashboard.');
    await pane().locator('[data-story-save]').click();
    await pane().locator('[data-story-read]').filter({hasText: 'The composed Story overview was saved'}).waitFor();
    const storySave = lastCall('tasks_update');
    assert.equal(storySave.body.changes.overview, 'The composed Story overview was saved from the real dashboard.');
    assert.equal(typeof storySave.body.expected_revision, 'number');
    await pane().locator('[data-rw-tab="chat"]').click();
    assert.equal(await pane().locator('.rw-chat .agfocus-conversation > .conversation-inline-compose textarea').inputValue(), 'Story chat draft retained');

    // A real stale revision keeps the local overview and offers the latest revision.
    await control('touch-task', {task_id: fixture.story.id, overview: 'A concurrent writer changed this overview.'});
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.record-overview').click();
    await pane().locator('[data-story-overview]').fill('Local story edit that must survive the conflict');
    await pane().locator('[data-story-save]').click();
    await pane().locator('[data-story-error]').waitFor();
    assert.match(await pane().locator('[data-story-error]').textContent(), /changed since|Changed since/);
    assert.equal(await pane().locator('[data-story-overview]').inputValue(), 'Local story edit that must survive the conflict');
    assert.equal(await pane().locator('[data-story-latest]').isVisible(), true);
    await pane().locator('[data-story-latest]').click();
    await pane().locator('[data-story-error]').filter({hasText: 'Loaded the latest revision'}).waitFor();
    assert.equal(await pane().locator('[data-story-overview]').inputValue(), 'Local story edit that must survive the conflict',
      'adopting the latest revision keeps the typed fields');
    await shot(pane(), 'story-stale-revision-desktop.png');
    await pane().locator('[data-story-overview]').fill('The composed Story overview was saved from the real dashboard.');
    await pane().locator('[data-story-save]').click();
    await pane().locator('[data-story-read]').filter({hasText: 'The composed Story overview was saved'}).waitFor();
    await pane().locator('.rw-back').click();
    await pane().locator('.rw-project-name').waitFor();
    await closeFocus();

    // Reopen the same scoped Story conversation in Focus, with its own draft per fresh selection.
    await goFocus();
    await openConversation(fixture.story.title);
    assert.equal(await pane().locator('.rw-title').textContent(), fixture.story.title);
    assert.equal(await pane().locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true');
    await pane().locator('.rw-chat .agfocus-conversation > .conversation-inline-compose textarea').waitFor();
    assert.equal(await pane().locator('.rw-chat .agfocus-conversation > .conversation-inline-compose textarea').inputValue(),
      'Story chat draft retained', 'Focus selects the same source and keeps its shared Chat draft');
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.record-overview').waitFor();
    assert.equal(await pane().locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Story dashboard');
    await closeFocus();

    // Ordinary Task conversation in Focus: default Chat and the canonical Task dashboard.
    await openConversation(fixture.ordinary.title);
    assert.equal(await pane().locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true');
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.tdetail').waitFor();
    assert.equal(await pane().locator('.rw-details, .context-disclosure').count(), 0);
    await shot(pane(), 'focus-task-desktop.png');
    await closeFocus();
    // The linked Global Task is also the same source in Focus, with Chat default and its own dashboard.
    await openConversation(fixture.global_linked.title);
    assert.equal(await pane().locator('.rw-title').textContent(), fixture.global_linked.title);
    assert.equal(await pane().locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true');
    await pane().locator('.rw-chat').getByText('Global private seed', {exact: true}).waitFor();
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('[data-editor]').waitFor();
    assert.equal(await pane().locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Task dashboard');
    await closeFocus();

    // ----------------------------------------------------------------------------------------------------------
    // G. Task -> Story conversion against the real revision-checked write, preserving Chat draft and Back.
    // ----------------------------------------------------------------------------------------------------------
    await goFocus();
    await harborCard.locator('.ttl').click();
    await pane().waitFor();
    await openProjectDashboard();
    await section('work');
    await projectRow(fixture.convertible.title).locator('.rw-project-open').click();
    await pane().waitFor();
    await pane().locator('.rw-chat .agfocus-conversation > .conversation-inline-compose textarea').fill('Conversion chat draft');
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.tdetail').waitFor();
    await pane().locator('[data-task-type]').selectOption('story');
    await pane().locator('[data-task-overview]').fill('Converted from the composed dashboard.');
    await pane().locator('[data-save-type]').click();
    await pane().locator('.record-overview').waitFor();
    assert.equal(await pane().locator('.tdetail').count(), 0, 'the old Task editor is disposed');
    assert.equal(await pane().locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Story dashboard');
    const conversionWrite = calls.filter(call => call.path.endsWith('/task/' + fixture.convertible.id)).at(-1);
    assert.ok(conversionWrite, 'the conversion used the real Project task route');
    assert.equal(conversionWrite.body.set.task_type, 'story');
    assert.equal(conversionWrite.body.set.overview, 'Converted from the composed dashboard.');
    assert.equal(conversionWrite.body.expected_revision, fixture.convertible.revision);
    assert.match(await pane().locator('.record-overview').textContent(), /Converted from the composed dashboard/);
    await pane().locator('[data-rw-tab="chat"]').click();
    assert.equal(await pane().locator('.rw-chat .agfocus-conversation > .conversation-inline-compose textarea').inputValue(), 'Conversion chat draft',
      'the same Chat draft survives the conversion');
    await pane().locator('.rw-back').click();
    await pane().locator('.rw-project-name').waitFor();
    await closeFocus();

    // A stale opening descriptor is normalized by the first authorized canonical read: the header/body become Story.
    await page.evaluate(async ([id, biz, project]) => {
      agfocusShowDock(_agpad);
      await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id, business_id: project, business_slug: biz},
        label: 'Convertible harbor work', task_type: 'task'}, {biz}), {can_thread: true});
    }, [fixture.convertible.id, 'harbor-light', P.harbor]);
    await pane().waitFor();
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.record-overview').waitFor();
    assert.equal(await pane().locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Story dashboard',
      'the first canonical read aligns the header with the actual subtype');
    assert.equal(await pane().locator('.tdetail').count(), 0, 'no generic editor survives the canonical subtype');
    await closeFocus();
    // An absent subtype descriptor is aligned by the same first canonical read.
    await page.evaluate(async ([id, biz, project]) => {
      agfocusShowDock(_agpad);
      await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id, business_id: project, business_slug: biz},
        label: 'Convertible harbor work'}, {biz}), {can_thread: true});
    }, [fixture.convertible.id, 'harbor-light', P.harbor]);
    await pane().waitFor();
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.record-overview').waitFor();
    assert.equal(await pane().locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Story dashboard',
      'an absent subtype descriptor is also aligned by the canonical read');
    await closeFocus();

    // ----------------------------------------------------------------------------------------------------------
    // H. Global Task: retained Reload discards only dashboard fields; Chat text and attachments are kept.
    // ----------------------------------------------------------------------------------------------------------
    await page.evaluate(async id => {
      agfocusShowDock(_agpad);
      await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id, business_id: null},
        label: 'Global follow-up', task_type: 'task'}, {can_thread: true}), {can_thread: true});
    }, fixture.global.id);
    await pane().waitFor();
    assert.equal(await pane().locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true');
    assert.equal(await pane().locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Task dashboard');
    const globalChat = pane().locator('.rw-chat .agfocus-conversation > .conversation-inline-compose textarea');
    await globalChat.waitFor();
    await globalChat.fill('KEEP GLOBAL CHAT DRAFT');
    await pane().locator('.rw-chat .agfocus-conversation > .conversation-inline-compose input[type="file"]').setInputFiles({name: 'harbor-notes.txt', mimeType: 'text/plain',
      buffer: Buffer.from('a synthetic attachment')});
    await pane().locator('.gchat-file-card').filter({hasText: 'harbor-notes.txt'}).waitFor();
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('[data-editor]').waitFor();
    await pane().locator('[data-task-overview]').fill('Dashboard field that Reload must discard');
    await control('touch-task', {task_id: fixture.global.id, overview: 'Canonical Global overview from another writer'});
    await pane().locator('[data-save]').click();
    await pane().locator('[data-reload]').waitFor();
    await pane().locator('[data-reload]').click();
    await waitFor(async () => await pane().locator('[data-task-overview]').inputValue() === 'Canonical Global overview from another writer',
      'the canonical Global overview after Reload');
    assert.equal(await pane().locator('[data-task-overview]').inputValue(), 'Canonical Global overview from another writer',
      'Reload discards only the dashboard fields');
    await pane().locator('[data-rw-tab="chat"]').click();
    assert.equal(await pane().locator('.rw-chat .agfocus-conversation > .conversation-inline-compose textarea').inputValue(), 'KEEP GLOBAL CHAT DRAFT');
    assert.equal(await pane().locator('.gchat-file-card').filter({hasText: 'harbor-notes.txt'}).count(), 1,
      'the Chat attachment survives the dashboard Reload');
    await shot(pane(), 'global-task-reload-chat-desktop.png');
    await closeFocus();

    // ----------------------------------------------------------------------------------------------------------
    // I. Signoffs: canonical WhatsApp and retained historical email bodies, the main Chat comment composer, no send.
    // ----------------------------------------------------------------------------------------------------------
    await goFocus();
    await harborCard.locator('.ttl').click();
    await pane().waitFor();
    await openProjectDashboard();
    await section('signoffs');
    const directoryBeforeSignoffs = await directory();
    await projectRow('Send the harbor update').locator('.rw-project-open').click();
    await pane().waitFor();
    assert.equal(await pane().locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true', 'the Signoff opens its Chat first');
    await sendMessage('Signoff private comment from the composed dashboard');
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.rw-signoff-panel .sg-rowhead').first().click();
    await pane().locator('.rw-dashboard .sg-detail:not([hidden]) .sg-whatsapp').waitFor();
    const whatsapp = await pane().locator('.rw-dashboard').textContent();
    assert.match(whatsapp, /Harbor Pilot/, 'the canonical WhatsApp recipient is shown');
    const whatsappMessage = await pane().locator('.rw-dashboard .sg-whatsapp textarea.sg-text').inputValue();
    assert.match(whatsappMessage, /the pilot starts at 09:00/);
    assert.match(whatsappMessage, /\n\nBring the signed brief\./, 'the full multiline WhatsApp message is shown');
    assert.equal(await pane().locator('.rw-dashboard .sg-conversation textarea').count(), 0, 'no second composer inside the card');
    assert.equal(await pane().locator('.rw-dashboard .sg-approve').count() >= 1, true, 'canonical review controls remain visible');
    await shot(pane(), 'signoff-whatsapp-desktop.png');
    assert.equal(providerCalls().length, 0, 'nothing was approved or sent');
    await pane().locator('.rw-back').click();
    await pane().locator('.rw-project-name').waitFor();

    await projectRow('Send the harbor launch note').locator('.rw-project-open').click();
    await pane().waitFor();
    assert.equal(await pane().locator('.rw-title').textContent(), 'Send the harbor launch note');
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.rw-signoff-panel .sg-rowhead').first().click();
    await pane().locator('.rw-dashboard .sg-detail:not([hidden]) .sg-email').waitFor();
    const email = await pane().locator('.rw-dashboard').textContent();
    assert.match(email, /pilot@example\.test/, 'the retained historical email recipient is shown');
    assert.match(email, /The harbor pilot starts on Monday/, 'the subject is shown');
    assert.match(email, /Hello from Harbor Light,/);
    assert.match(email, /The signed brief is attached to this note\./);
    assert.equal(await pane().locator('.rw-dashboard .sg-conversation textarea').count(), 0);
    await shot(pane(), 'signoff-email-desktop.png');
    assert.equal(providerCalls().length, 0, 'opening the email Signoff starts no provider operation');
    const directoryAfterSignoffs = await directory();
    for (const source of [['project', P.harbor], ['task', fixture.story.id], ['signoff', fixture.signoffs.email]]) {
      assert.equal(sequenceOf(directoryAfterSignoffs, ...source), sequenceOf(directoryBeforeSignoffs, ...source),
        'only the explicit private comment changed thread activity: ' + source.join(':'));
    }
    const commentThread = directoryAfterSignoffs.find(row => row.source.type === 'signoff' && row.source.id === fixture.signoffs.whatsapp);
    assert.ok(commentThread && commentThread.latest_sequence > sequenceOf(directoryBeforeSignoffs, 'signoff', fixture.signoffs.whatsapp),
      'the comment landed on the exact Signoff source');
    await pane().locator('.rw-back').click();
    await pane().locator('.rw-project-name').waitFor();
    await closeFocus();

    // ----------------------------------------------------------------------------------------------------------
    // H. Pending-Signoff chips open the standard Signoff review modal for the exact proposal; closing it leaves the
    //    current workspace, its edits and the Focus list untouched.
    // ----------------------------------------------------------------------------------------------------------
    await goFocus();
    const signoffChip = title => page.locator(`.focus-signoff-chip[aria-label="Review ${title}"]`);
    await signoffChip('Send the harbor update').waitFor();
    await signoffChip('Send the harbor launch note').waitFor();
    const cardsBeforeSignoffChips = await page.locator('.agfocus-items .agfocus-card').evaluateAll(nodes => nodes.map(node => node.dataset.cardId));
    const openSignoffModal = async id => {
      await page.locator('.modal .attention-signoff-view').waitFor();
      assert.equal(await page.locator('.modal').count(), 1, 'one Signoff review modal');
      assert.equal(await page.locator(`.attention-signoff-view .sg-row[data-id="${id}"]`).count(), 1, 'the exact Signoff is selected');
    };
    const closeSignoffModal = async () => {
      await page.locator('.modal').getByRole('button', {name: 'Close', exact: true}).click();
      await page.waitForFunction(() => !document.querySelector('.attention-signoff-view'));
      assert.equal(await page.locator('.modal').count(), 0, 'closing disposes the Signoff view');
    };

    // A selected Focus card and its record workspace stay exactly as they were behind the modal.
    await harborCard.locator('.ttl').click();
    await pane().waitFor();
    assert.equal(await harborCard.getAttribute('aria-expanded'), 'true');

    // An unpinned business Signoff opens the exact business proposal through the owner global route.
    await signoffChip('Send the harbor update').click();
    await openSignoffModal(fixture.signoffs.whatsapp);
    assert.equal(await page.locator(`.attention-signoff-view .sg-row[data-id="${fixture.signoffs.whatsapp}"] .sg-rowmeta .identity-business`).count(), 1,
      'the Project review scope survives');
    assert.equal(await pane().locator('.rw-title').textContent(), 'Harbor Light', 'the prior record workspace stays mounted');
    assert.equal(await harborCard.getAttribute('aria-expanded'), 'true', 'the selected Focus card keeps its state');
    await closeSignoffModal();
    assert.equal(await pane().locator('.rw-title').textContent(), 'Harbor Light');
    assert.deepEqual(await page.locator('.agfocus-items .agfocus-card').evaluateAll(nodes => nodes.map(node => node.dataset.cardId)),
      cardsBeforeSignoffChips, 'reviewing a Signoff does not pin, unpin or reorder a Focus card');

    // An unpinned Global Signoff keeps the explicit null scope and still leaves the workspace alone.
    await signoffChip('Send the harbor launch note').click();
    await openSignoffModal(fixture.signoffs.email);
    assert.equal(await page.locator(`.attention-signoff-view .sg-row[data-id="${fixture.signoffs.email}"] .sg-rowmeta .identity-global`).count(), 1,
      'Global scope is explicit');
    assert.equal(await pane().locator('.rw-title').textContent(), 'Harbor Light');
    await closeSignoffModal();
    assert.equal(await pane().locator('.rw-title').textContent(), 'Harbor Light');
    assert.deepEqual(await page.locator('.agfocus-items .agfocus-card').evaluateAll(nodes => nodes.map(node => node.dataset.cardId)),
      cardsBeforeSignoffChips, 'a Global Signoff leaves the Focus list unchanged');

    // A dirty Story dashboard behind the modal keeps its selection and edits; the modal never discards them.
    await openProjectDashboard();
    await section('work');
    await projectRow(fixture.story.title).locator('.rw-project-open').click();
    await pane().waitFor();
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.record-overview').click();
    await pane().locator('[data-story-overview]').fill('Unsaved Story edit that must survive a Signoff review');
    await signoffChip('Send the harbor update').click();
    await openSignoffModal(fixture.signoffs.whatsapp);
    assert.equal(await pane().locator('.rw-title').textContent(), fixture.story.title, 'the modal keeps the dirty Story pane');
    assert.equal(await pane().locator('[data-story-overview]').inputValue(),
      'Unsaved Story edit that must survive a Signoff review', 'the modal keeps the Story edits');
    assert.equal(await pane().locator('.rw-signoff-panel').count(), 0, 'no Signoff dashboard replaced the dirty Story');
    await closeSignoffModal();
    assert.equal(await pane().locator('[data-story-overview]').inputValue(),
      'Unsaved Story edit that must survive a Signoff review', 'closing the modal keeps the Story edits');
    await pane().locator('.rw-close').click();
    const closeDiscard = page.locator('.modal').last();
    await closeDiscard.waitFor();
    await closeDiscard.getByRole('button', {name: 'Discard'}).click();
    await closeDiscard.waitFor({state: 'detached'});
    await page.waitForFunction(() => document.querySelector('.agfocus-dock')?.hidden === true);

    // ----------------------------------------------------------------------------------------------------------
    // J. Thread ordering: edits and links do not reorder; another actual private message does.
    // ----------------------------------------------------------------------------------------------------------
    await goFocus();
    const orderBefore = await conversationTitles();
    await goFocus();
    await harborCard.locator('.ttl').click();
    await pane().waitFor();
    await openProjectDashboard();
    await section('work');
    // A real link/unlink round trip is a Project edit, not thread activity.
    await pane().locator('[data-project-link]').click();
    const orderDialog = page.locator('.modal').last();
    await orderDialog.waitFor();
    await orderDialog.locator('[data-link-scope]').selectOption(P.maple);
    await orderDialog.locator('[data-link-type]').selectOption('task');
    await orderDialog.locator('.rw-link-option').filter({hasText: fixture.foreign_candidate.title}).click();
    await orderDialog.waitFor({state: 'detached'});
    await projectRow(fixture.foreign_candidate.title).waitFor();
    await projectRow(fixture.foreign_candidate.title).locator('.rw-project-unlink').click();
    await waitFor(async () => await projectRow(fixture.foreign_candidate.title).count() === 0, 'the ordering link round trip');
    await projectRow(fixture.story.title).locator('.rw-project-open').click();
    await pane().waitFor();
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.record-overview').click();
    await pane().locator('[data-story-overview]').fill('An edit that must not reorder Threads.');
    await pane().locator('[data-story-save]').click();
    await pane().locator('[data-story-read]').filter({hasText: 'An edit that must not reorder'}).waitFor();
    await pane().locator('.rw-back').click();
    await pane().locator('.rw-project-name').waitFor();
    await closeFocus();
    await goFocus();
    assert.deepEqual(await conversationTitles(), orderBefore, 'dashboard edits and links do not change Thread ordering');
    await goFocus();
    await harborCard.locator('.ttl').click();
    await pane().waitFor();
    await sendMessage('A second real Project private message changes the order');
    await closeFocus();
    await goFocus();
    const orderAfter = await conversationTitles();
    assert.equal(orderAfter[0], 'Harbor Light', 'a real private message moves its Thread to the front');
    assert.notDeepEqual(orderAfter, orderBefore);
    await shot(page, 'focus-conversation-order-desktop.png');

    // ----------------------------------------------------------------------------------------------------------
    // K. Narrow layout: one full-width pane at 390px, readable Story/Project/Signoff.
    // ----------------------------------------------------------------------------------------------------------
    const noOverflow = () => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1 && document.body.scrollWidth <= innerWidth + 1);
    const assertDashboardScroll = async label => {
      const metrics = await pane().locator('.rw-dashboard').evaluate(el => {
        const before = {scrollHeight: el.scrollHeight, clientHeight: el.clientHeight};
        el.scrollTop = el.scrollHeight;
        return {...before, top: el.scrollTop, overflow: getComputedStyle(el).overflowY};
      });
      assert.equal(metrics.scrollHeight > metrics.clientHeight, true, label + ' has populated content beyond the viewport');
      assert.equal(metrics.top > 0, true, label + ' can scroll to its populated content');
      assert.match(metrics.overflow, /auto|scroll/);
      const head = await pane().locator('.rw-head').boundingBox();
      assert.ok(head && head.y >= 0 && head.y < 844, label + ' keeps its header usable while scrolling');
      assert.equal(await pane().locator('.rw-head .rw-tabs [role="tab"]').count(), 2, label + ' keeps both existing tabs');
      await pane().locator('.rw-dashboard').evaluate(el => {el.scrollTop = 0;});
    };
    await page.setViewportSize({width: 390, height: 844});
    await page.waitForTimeout(250);
    await openConversation(fixture.story.title);
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.record-overview').waitFor();
    // Wait for the child and both associated readers, not just the overview, before the mobile screenshot.
    await pane().locator('.record-child-row').filter({hasText: fixture.child.title}).waitFor();
    const mobileSignoffs = pane().locator('.record-section').filter({has: page.locator('h4', {hasText: 'Signoffs'})});
    const mobileActions = pane().locator('.record-section').filter({has: page.locator('h4', {hasText: 'Actions'})});
    await mobileSignoffs.locator('.record-link-open').filter({hasText: 'Send the harbor update'}).waitFor();
    await mobileActions.locator('.record-link-open').filter({hasText: fixture.action.text}).waitFor();
    await assertDashboardScroll('the Story dashboard');
    assert.equal(await noOverflow(), true, 'the populated Story dashboard fits 390px');
    await shot(pane(), 'story-dashboard-390.png');
    await closeFocus();
    await goFocus();
    await harborCard.locator('.ttl').click();
    await pane().waitFor();
    await openProjectDashboard();
    await section('work');
    // The screenshot qualifies populated rows: a long owned title and a linked foreign/Global scope label.
    await projectRow(fixture.owned_linked.title).waitFor();
    await projectRow(fixture.global_linked.title).waitFor();
    assert.equal(await projectRow(fixture.global_linked.title).locator('.rw-project-scope').textContent(), 'Global');
    assert.match(await projectRow(fixture.owned_linked.title).locator('.rw-project-label').textContent(), /Owned and linked/);
    await waitFor(() => pane().locator('.rw-project-empty').evaluate(el => el.hidden), 'the Project inventory to finish loading');
    await waitFor(async () => !String(await pane().locator('.rw-project-more').textContent()).includes('Loading'), 'the Project first page to settle');
    await assertDashboardScroll('the Project dashboard');
    assert.equal(await noOverflow(), true, 'the populated Project dashboard fits 390px');
    await shot(pane(), 'project-dashboard-390.png');
    await section('signoffs');
    await projectRow('Send the harbor update').locator('.rw-project-open').click();
    await pane().waitFor();
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.rw-signoff-panel .sg-rowhead').first().click();
    await pane().locator('.rw-dashboard .sg-detail:not([hidden]) .sg-whatsapp').waitFor();
    assert.equal(await noOverflow(), true, 'the full Signoff body fits 390px');
    assert.match(await pane().locator('.rw-dashboard .sg-whatsapp textarea.sg-text').inputValue(), /the pilot starts at 09:00/);
    await shot(pane(), 'signoff-whatsapp-390.png');
    await closeFocus();

    // ----------------------------------------------------------------------------------------------------------
    // Evidence: no shadow Project Task, no fabricated success, exact comment counts.
    // ----------------------------------------------------------------------------------------------------------
    assert.equal(opCalls('tasks_create').length, 0, 'reading/linking/pinning never created a Project Task');
    assert.equal(opCalls('focus_present').length >= 1, true);
    assert.equal(opCalls('project_items_link').length >= 2, true);
    assert.equal(opCalls('project_items_unlink').length >= 2, true);
    assert.equal(providerCalls().length, 0, 'no provider operation, approval or send was requested');
    assert.deepEqual(errors, []);
    const deniedChats = expected.filter(item => item.section === 'revoked-foreign-chat');
    assert.equal(deniedChats.length, 1, 'the retained foreign pane attempted its private channel once and was denied');
    assert.deepEqual(deniedChats[0].body.source, {type: 'task', id: fixture.foreign.id, business_id: P.maple});
    const conflicts = expected.filter(item => item.section === 'stale-revision');
    assert.equal(conflicts.length, 2, 'both deliberate stale writes surfaced the canonical revision conflict');
    assert.deepEqual(conflicts.map(item => item.path), [
      '/api/biz/harbor-light/operations/tasks_update', '/api/root/operations/tasks_update']);
    const summary = {
      project: P.harbor, projectCardId, commentThreadId: commentThread.id,
      focusPresentCalls: opCalls('focus_present').length,
      projectListCalls: opCalls('project_items_list').length,
      linkCalls: opCalls('project_items_link').length,
      unlinkCalls: opCalls('project_items_unlink').length,
      readers: {context: opCalls('context_get').length, table: opCalls('tables_rows_list').length,
        routine: opCalls('routines_runs').length, job: opCalls('jobs_get').length,
        dev: opCalls('dev_tickets_list').length},
      threadOrder: orderAfter,
      notes,
    };
    if (shots) fs.writeFileSync(path.join(shots, 'composed-result.json'), JSON.stringify(summary, null, 2) + '\n');
    console.log('Composed Project qualification passed: ' + JSON.stringify(summary));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
