// Move to project, composed: the real integrated dashboard talks to the real composed owner HTTP application.
//
// No product API response is intercepted, stubbed or fabricated: the Move operations (tasks_move_targets, the preview and
// the mutation), the Task/discussion/private-conversation/Focus/CRM reads and every dashboard shell read are answered by the
// synthetic host that serves this dashboard. Requests outside that loopback origin are aborted. The small /__fixture channel
// belongs to the Python harness: it digests stored rows read-only and applies the one stored-state change a concurrent writer
// would cause (through the real tasks_update operation); it never answers a product read. Direct API calls from this driver
// (marked with the x-fixture-driver header) are the owner's authenticated session reading back what the UI did.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const base = process.env.TASK_MOVE_COMPOSITION_BASE;
const fixture = JSON.parse(fs.readFileSync(process.env.TASK_MOVE_COMPOSITION_FIXTURE, 'utf8'));
const shots = process.env.TASK_MOVE_COMPOSITION_SHOTS || null;
const resultFile = process.env.TASK_MOVE_COMPOSITION_RESULT || null;
const origin = new URL(base).origin;
const P = fixture.projects;

const waitFor = async (check, what, attempts = 600) => {
  for (let i = 0; i < attempts; i++) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 25)); }
  throw new Error('Timed out waiting for ' + what);
};
const decode = text => { try { return JSON.parse(text || 'null'); } catch (_) { return null; } };
const countsText = counts => [`${counts.tasks} tasks (with descendants)`, `${counts.comments} comments`, `${counts.private_messages} private messages`,
  `${counts.project_links} project links`, `${counts.focus_cards} Focus cards`, `${counts.crm_entries} CRM entries`];

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
    args: ['--no-sandbox', '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1']});
  const result = {origin, steps: [], uiMoves: [], driverReads: [], notes: [], blockedExternal: []};
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 950}});
    page.setDefaultTimeout(30000);
    const errors = [], uiCalls = [], uiResponses = [], allowed = [];
    page.on('pageerror', error => errors.push('pageerror: ' + error.message));
    page.on('requestfailed', request => {
      const url = request.url();
      if (new URL(url).origin !== origin) { result.blockedExternal.push(url); return; }
      if (String(request.failure()?.errorText || '').includes('ERR_ABORTED')) return result.notes.push('cancelled read: ' + url);
      errors.push('requestfailed: ' + url + ' (' + (request.failure()?.errorText || '') + ')');
    });
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.origin !== origin || request.method() !== 'POST' || request.headers()['x-fixture-driver']) return;
      uiCalls.push({path: url.pathname, body: decode(request.postData())});
    });
    page.on('response', async response => {
      const url = new URL(response.url());
      if (url.origin !== origin) return;
      const driver = !!response.request().headers()['x-fixture-driver'];
      let body = null;
      if (response.request().method() === 'POST' && url.pathname.includes('/operations/')) { try { body = await response.json(); } catch (_) { /* no body */ } }
      if (!driver && body) uiResponses.push({path: url.pathname, status: response.status(), request: decode(response.request().postData()), body});
      if (response.status() >= 400 && !driver) {
        const hit = allowed.find(item => !item.used && item.status === response.status() && url.pathname.endsWith(item.suffix));
        if (hit) { hit.used = true; hit.code = body?.error?.code; }
        else errors.push(`http ${response.status()}: ${url.pathname} ${body?.error?.code || ''}`);
      }
    });
    // The browser may only reach the synthetic composed origin; every external request is aborted.
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());

    // -- direct owner-session reads and the fixture channel ---------------------------------------------------------------
    const direct = async (operation, body, slug = null) => {
      const out = await page.evaluate(async ([target, payload]) => {
        const response = await fetch(target, {method: 'POST', headers: {'Content-Type': 'application/json', 'x-fixture-driver': '1'}, body: JSON.stringify(payload)});
        return {status: response.status, body: await response.json()};
      }, [`/api/${slug ? 'biz/' + slug : 'root'}/operations/${operation}`, body]);
      result.driverReads.push({operation, slug, status: out.status, code: out.body?.error?.code || null});
      return out;
    };
    const control = async (action, body = {}) => {
      const out = await page.evaluate(async ([target, payload]) => {
        const response = await fetch(target, {method: 'POST', headers: {'Content-Type': 'application/json', 'x-fixture-driver': '1'}, body: JSON.stringify(payload)});
        return {status: response.status, body: await response.json()};
      }, ['/__fixture/' + action, body]);
      assert.equal(out.status, 200, JSON.stringify(out));
      return out.body;
    };
    // Opening a record acknowledges its private chat (a read cursor and an idempotent receipt): ordinary UI behaviour that is not
    // part of a Move. A baseline is therefore taken once those acknowledgements have settled.
    const settled = async () => {
      let last = null, stable = 0;
      for (let i = 0; i < 80 && stable < 3; i++) {
        const now = JSON.stringify(await control('digest'));
        stable = now === last ? stable + 1 : 0; last = now;
        await new Promise(resolve => setTimeout(resolve, 150));
      }
      assert.ok(stable >= 3, 'the stored rows never settled');
      return JSON.parse(last);
    };
    const allow = (status, suffix) => { const item = {status, suffix, used: false}; allowed.push(item); return item; };
    const step = name => { console.log('STEP: ' + name); result.steps.push(name); };
    const opCalls = suffix => uiCalls.filter(call => call.path.endsWith('/operations/' + suffix));
    const opResponses = suffix => uiResponses.filter(row => row.path.endsWith('/operations/' + suffix));
    const shot = async (target, name) => { if (shots) await target.screenshot({path: path.join(shots, name)}); };

    // -- real UI handles --------------------------------------------------------------------------------------------------
    const pane = () => page.locator('.agfocus-dock .rw-record:not([hidden])').first();
    const dialog = () => page.locator('.modal', {hasText: 'Move to project'}).filter({has: page.locator('.tmove')});
    const currentSource = () => page.evaluate(() => _agpad.focusWorkspace.current()?.source || null);
    const currentSlug = () => page.evaluate(() => _agpad.focusWorkspace.current()?.biz ?? null);
    const goFocus = async () => {
      // The Tasks drawer slides over the Focus board once a row was opened from it: Escape is how its owner puts it away.
      if (await page.evaluate(() => tPanelOpen())) {
        await page.keyboard.press('Escape');
        await waitFor(() => page.evaluate(() => !tPanelOpen()), 'the Tasks drawer to close');
      }
      if (await page.evaluate(() => state.scope !== 'global')) await page.evaluate(() => selectGlobal('agents'));
      await page.locator('[data-focus-add]').waitFor();
      await page.locator('.agfocus-items .agfocus-card').first().waitFor();
    };
    const closeWorkspace = async () => {
      if (await page.evaluate(() => !!_agpad?.focusWorkspace?.isOpen())) await page.evaluate(() => _agpad.focusWorkspace.close());
      await waitFor(async () => !(await page.evaluate(() => !!_agpad?.focusWorkspace?.isOpen())), 'the workspace to close');
    };
    const openCard = async id => {
      await page.locator(`.agfocus-items [data-card-id="${id}"] .ttl`).click();
      await pane().locator('.rw-head').waitFor();
    };
    const openMove = async trigger => {
      await trigger.click();
      await dialog().locator('[data-move-destination] option', {hasText: 'Beta Lab'}).waitFor({state: 'attached'});
    };
    const choose = async name => { await dialog().locator('[data-move-destination]').selectOption({label: name}); await dialog().locator('.tmove-route').waitFor(); };
    const previewText = () => dialog().locator('[data-move-preview]').innerText();
    const cleanly = async (name, action) => { await action(); assert.deepEqual(errors, [], 'no unexpected page or HTTP error after: ' + name); };

    await page.goto(base + '/#global/agents');
    await page.locator('[data-focus-add]').waitFor();
    await page.locator('.agfocus-items .agfocus-card').first().waitFor();
    assert.equal(await page.locator('.agfocus-items .agfocus-card').count(), 6, 'the six seeded Focus cards');

    // ==========================================================================================================================
    // 1. A saved Global Story from Focus: tree, outside parent, discussion, private chats, Focus cards and Project links.
    // ==========================================================================================================================
    await cleanly('scenario 1', async () => {
      step('1. Global Story from Focus: open Move, see the real preservation counts, cancel, prove nothing moved.');
      const S = fixture.story;
      await openCard(S.card);
      assert.equal(await pane().locator('.rw-title').textContent(), S.title);
      assert.deepEqual(await currentSource(), {type: 'task', id: S.id, business_id: null});
      const before = await settled();
      await openMove(pane().locator('.rw-move'));
      assert.deepEqual(opCalls('tasks_move_targets').map(call => [call.path, call.body]),
        [['/api/root/operations/tasks_move_targets', {source_business_id: null}]], 'Global is an explicit null source on the root route');
      assert.deepEqual(await dialog().locator('[data-move-destination] option').allTextContents(),
        ['Choose a project…', 'Alpha Studio', 'Beta Lab', 'Gamma Works'], 'the real project directory, source excluded, no inventory read');
      assert.equal(opCalls('project_items_list').length + opCalls('project_items_candidates').length, 0, 'destinations never come from project inventory');
      assert.equal(opCalls('tasks_move_to_project_preview').length, 0, 'nothing is previewed or selected automatically');
      assert.equal(await dialog().locator('[data-move-confirm]').isDisabled(), true);
      await choose('Beta Lab');
      assert.deepEqual(opCalls('tasks_move_to_project_preview').map(call => call.body),
        [{source_business_id: null, destination_project_id: P.beta, task_ids: [S.id]}]);
      let text = await previewText();
      assert.match(text, /From Global to Beta Lab/);
      for (const line of countsText(S.expected_counts)) assert.ok(text.includes(line), `the preview shows "${line}" (storage says so)\n${text}`);
      assert.match(text, /leaves its current parent, which stays where it is/, 'the outside parent is detached and stays behind');
      assert.match(text, /Archived child \(archived\)/);
      for (const title of [S.title, 'Completed child', 'Grandchild of the completed child']) assert.ok(text.includes(title), title);
      assert.match(text, /Ordinary task access follows the destination project’s rules/);
      assert.match(text, /Private owner–Ordubashy conversations stay private/);
      assert.equal(await dialog().locator('[data-move-confirm]').isDisabled(), false);
      await shot(dialog(), 'scenario-1-preview.png');
      // Cancel: nothing was sent and not one stored row changed.
      await dialog().locator('[data-move-cancel]').click();
      await waitFor(async () => (await dialog().count()) === 0, 'the dialog to close');
      assert.equal(opCalls('tasks_move_to_project').length, 0, 'cancel never mutates');
      assert.deepEqual(await settled(), before, 'a cancelled Move leaves every stored row exactly as it was');
      assert.deepEqual(await currentSource(), {type: 'task', id: S.id, business_id: null}, 'the record is still the Global one');

      step('1. Preview again, then an explicit confirm: destination navigation, same ids, audit once.');
      await openMove(pane().locator('.rw-move'));
      await choose('Beta Lab');
      assert.equal(opCalls('tasks_move_to_project').length, 0);
      await dialog().locator('[data-move-confirm]').click();
      await waitFor(async () => (await dialog().count()) === 0, 'the dialog to close after the move');
      await waitFor(async () => (await currentSource())?.business_id === P.beta, 'navigation to the destination record');
      assert.deepEqual(await currentSource(), {type: 'task', id: S.id, business_id: P.beta}, 'the same Task id at its returned destination');
      assert.equal(await currentSlug(), 'beta');
      assert.equal(await pane().locator('.rw-back').isHidden(), true, 'the old-scope entry was replaced, not left behind Back');
      await pane().locator('[data-rw-tab="dashboard"]').click();
      await pane().getByText('Completed child').first().waitFor();
      assert.equal(await pane().locator('.rw-title').textContent(), S.title);
      await shot(pane(), 'scenario-1-moved-record.png');
      const sent = opCalls('tasks_move_to_project');
      assert.equal(sent.length, 1, 'exactly one mutation request');
      const move = sent[0].body;
      assert.deepEqual({...move, preview_token: undefined, idempotency_key: undefined},
        {source_business_id: null, destination_project_id: P.beta, task_ids: [S.id], preview_token: undefined, idempotency_key: undefined});
      assert.ok(move.preview_token && move.idempotency_key);
      const response = opResponses('tasks_move_to_project').at(-1);
      assert.equal(response.status, 200);
      assert.equal(response.body.data.destination.id, P.beta);
      assert.deepEqual(response.body.data.moved.map(row => row.id).sort(), [...S.ids].sort());
      assert.ok(response.body.data.moved.every(row => row.business_id === P.beta));
      assert.deepEqual(response.body.data.counts, S.expected_counts, 'the mutation reports the same preserved counts');
      result.uiMoves.push({label: 'global-story', key: move.idempotency_key, token: move.preview_token, move_id: response.body.data.move_id,
        moved: response.body.data.moved.map(row => row.id)});

      step('1. Old Global reads return the authorized record_moved; the destination reads the same discussion and private chat.');
      for (const [operation, body] of [['tasks_get', {task_id: S.id, scope: 'global'}], ['tasks_thread_get', {task_id: S.id, scope: 'global'}], ['tasks_notes_list', {task_id: S.id, scope: 'global'}],
        ['tasks_get', {task_id: S.grandchild, scope: 'global'}]]) {
        const old = await direct(operation, body);
        assert.equal(old.status, 409, operation + ' ' + JSON.stringify(old.body));
        assert.equal(old.body.error.code, 'record_moved');
        assert.equal(old.body.error.details.destination.id, P.beta);
        assert.equal(old.body.error.details.destination.slug, 'beta');
        assert.deepEqual(old.body.error.details.task, {type: 'task', id: body.task_id, business_id: P.beta});
      }
      // The private owner chat answers like every other Task read: the owner, who may read the Task where it is now, is told where.
      const oldChat = await direct('owner_conversation_get', {scope: 'global', source: {type: 'task', id: S.id, business_id: null}});
      assert.equal(oldChat.status, 409, JSON.stringify(oldChat.body));
      assert.equal(oldChat.body.error.code, 'record_moved');
      assert.equal(oldChat.body.error.details.destination.id, P.beta);
      assert.deepEqual(oldChat.body.error.details.task, {type: 'task', id: S.id, business_id: P.beta});
      const fresh = await direct('tasks_get', {task_id: S.id}, 'beta');
      assert.equal(fresh.status, 200);
      assert.equal(fresh.body.data.task.business_id, P.beta);
      assert.ok(!fresh.body.data.task.parent_id, 'the root is detached from the outside parent');
      const chat = await direct('owner_conversation_get', {source: {type: 'task', id: S.id, business_id: P.beta}}, 'beta');
      assert.equal(chat.status, 200);
      assert.deepEqual(chat.body.data.messages.map(row => row.body), ['Private note about the funding story.']);
      const board = await direct('focus_get', {scope: 'global'});
      const cards = Object.fromEntries(board.body.data.items.map(item => [item.id, item.source]));
      assert.equal(cards[S.card].business_id, P.beta, 'the Focus card follows the Story');
      assert.equal(cards[S.grandchild_card].business_id, P.beta, 'the Focus card of a descendant follows it too');

      step('1. A same-key retry over HTTP replays the receipt and never executes twice.');
      const afterMove = await settled();
      const replay = await direct('tasks_move_to_project', move);
      assert.equal(replay.status, 200);
      assert.deepEqual(replay.body.data, response.body.data, 'the replay returns the original result');
      assert.deepEqual(await settled(), afterMove, 'the replay changed no stored row and added no audit entry');
    });

    // ==========================================================================================================================
    // 2. A project Task through the normal drawer, then its historical references through the real CRM activity consumer.
    // ==========================================================================================================================
    await cleanly('scenario 2', async () => {
      step('2. Project Task Tasks list -> drawer -> Move to project: destination navigation and source refresh.');
      const H = fixture.holder;
      await page.evaluate(() => selectBiz('alpha'));
      await page.locator(`.business-task-list .trow[data-id="${H.id}"]`).waitFor();
      assert.ok(await page.locator(`.business-task-list .trow[data-id="${H.plain}"]`).count() === 1, 'an unrelated Alpha Task is listed too');
      const observed = await page.evaluate(([slug, id]) => !!taskStateSnapshot(slug, id)?.task, ['alpha', H.id]);
      await page.locator(`.business-task-list .trow[data-id="${H.id}"]`).click({position: {x: 360, y: 12}});
      const drawer = page.locator('.modal', {hasText: 'Task · Alpha Studio'}).filter({has: page.locator('.tdetail')});
      await drawer.waitFor();
      assert.ok((await drawer.innerText()).includes(H.title));
      await drawer.locator('.mfoot button', {hasText: 'Move to project…'}).click();
      await dialog().locator('[data-move-destination] option', {hasText: 'Gamma Works'}).waitFor({state: 'attached'});
      assert.deepEqual(opCalls('tasks_move_targets').at(-1).body, {source_business_id: P.alpha}, 'the drawer sends the task’s own project, not the sidebar');
      assert.deepEqual(await dialog().locator('[data-move-destination] option').allTextContents(), ['Choose a project…', 'Beta Lab', 'Gamma Works']);
      await choose('Gamma Works');
      let text = await previewText();
      for (const line of countsText(H.expected_counts)) assert.ok(text.includes(line), `the preview shows "${line}"\n${text}`);
      assert.match(text, /From Alpha Studio to Gamma Works/);
      assert.match(text, /becomes top-level in the destination\./, 'a root without a parent is not described as detached');
      assert.doesNotMatch(text, /leaves its current parent/);
      await dialog().locator('[data-move-confirm]').click();
      await waitFor(async () => (await dialog().count()) === 0, 'the dialog to close');
      await waitFor(() => page.evaluate(() => state.biz === 'gamma'), 'navigation to the destination project');
      const destinationDrawer = page.locator('.modal', {hasText: 'Task · Gamma Works'}).filter({has: page.locator('.tdetail')});
      await destinationDrawer.waitFor();
      assert.ok((await destinationDrawer.innerText()).includes(H.title), 'the drawer opens the same Task in the destination project');
      assert.equal(await page.locator('.modal', {hasText: 'Task · Alpha Studio'}).count(), 0, 'the old-scope drawer is gone');
      if (observed) assert.equal(await page.evaluate(([slug, id]) => taskStateSnapshot(slug, id)?.task ?? null, ['alpha', H.id]), null, 'the source scope’s cached state was forgotten');
      const sent = opCalls('tasks_move_to_project').at(-1);
      const reply = opResponses('tasks_move_to_project').at(-1);
      assert.equal(reply.status, 200);
      assert.deepEqual(sent.body.task_ids, [H.id]);
      assert.deepEqual(reply.body.data.moved.map(row => row.id).sort(), [H.id, H.follower].sort());
      result.uiMoves.push({label: 'project-task', key: sent.body.idempotency_key, token: sent.body.preview_token, move_id: reply.body.data.move_id,
        moved: reply.body.data.moved.map(row => row.id)});
      await destinationDrawer.locator('.mfoot button', {hasText: 'Close'}).click();
      await page.evaluate(() => selectBiz('alpha'));
      await page.locator(`.business-task-list .trow[data-id="${H.plain}"]`).waitFor();
      assert.equal(await page.locator(`.business-task-list .trow[data-id="${H.id}"]`).count(), 0, 'the moved Task left the source list');
      assert.equal(await page.locator(`.business-task-list .trow[data-id="${H.follower}"]`).count(), 0, 'and so did its descendant');
      await page.evaluate(() => selectBiz('gamma'));
      await page.locator(`.business-task-list .trow[data-id="${H.id}"]`).waitFor();
      assert.equal(await page.locator(`.business-task-list .trow[data-id="${H.follower}"]`).count(), 1, 'the destination list has the descendant');

      step('2. The returned historical references carry the canonical scope of the Task each note meant.');
      const thread = await direct('tasks_thread_get', {task_id: H.id}, 'gamma');
      assert.equal(thread.status, 200, JSON.stringify(thread.body));
      const refs = Object.fromEntries(thread.body.data.notes.filter(note => (note.refs || []).length).map(note => [note.refs[0].id, note.refs[0]]));
      assert.equal(refs[H.follower].business_id, P.gamma, 'a note about a Task that moved with its holder names the destination');
      assert.equal(refs[H.follower].business_slug, 'gamma');
      assert.equal(refs[H.follower].label, H.follower_title, 'never the Global Task that only shares its bare id');
      assert.equal(refs[H.behind].business_id, P.alpha, 'a note about a Task left behind keeps naming where it stayed');
      assert.equal(refs[H.behind].business_slug, 'alpha');
      assert.equal(refs[H.behind].label, H.behind_title, 'never the destination Task that only shares its bare id');

      step('2. The holder’s CRM linked activity follows the original targets; each Open lands on the right project record.');
      await goFocus();
      await openCard(H.card);
      assert.deepEqual(await currentSource(), {type: 'task', id: H.id, business_id: P.gamma}, 'the pinned Focus card follows the moved Task');
      await pane().locator('[data-rw-tab="dashboard"]').click();
      await pane().locator('.rw-activity-section > summary').click();
      await pane().locator('.crm-activity-item').first().waitFor();
      const activity = opResponses('crm_activity_list').at(-1);
      assert.equal(activity.status, 200);
      const linked = activity.body.data.items.filter(item => item.origin === 'linked');
      const shown = await pane().locator('.crm-activity').innerText();
      const link = (text, id) => linked.find(item => (item.body || item.summary || '').includes(text) && item.source?.id === id);
      const follower = link(H.texts.follower, H.follower), behind = link(H.texts.behind, H.behind);
      assert.ok(follower, 'activity of the Task that moved with its holder\n' + JSON.stringify(linked));
      assert.equal(follower.source.business_id, P.gamma);
      assert.ok(behind, 'activity of the Task left behind');
      assert.equal(behind.source.business_id, P.alpha);
      for (const decoyText of [H.texts.global_decoy, H.texts.gamma_decoy]) {
        assert.ok(!JSON.stringify(activity.body.data.items).includes(decoyText), 'no same-id decoy activity is attached');
        assert.ok(!shown.includes(decoyText), 'and none is shown');
      }
      await shot(pane(), 'scenario-2-linked-activity.png');
      await pane().locator('.crm-activity-item').getByRole('button', {name: 'Open ' + H.follower_title, exact: true}).click();
      await waitFor(async () => (await currentSource())?.id === H.follower, 'the follow-up record');
      assert.deepEqual(await currentSource(), {type: 'task', id: H.follower, business_id: P.gamma});
      assert.equal(await currentSlug(), 'gamma');
      await pane().locator('.rw-title', {hasText: H.follower_title}).waitFor();
      await pane().locator('.rw-back').click();
      await waitFor(async () => (await currentSource())?.id === H.id, 'back at the holder');
      // Back restores the holder exactly as it was left: its Activity is still open.
      assert.equal(await pane().locator('.rw-activity-section').evaluate(node => node.open), true);
      await pane().locator('.crm-activity-item').first().waitFor();
      await pane().locator('.crm-activity-item').getByRole('button', {name: 'Open ' + H.behind_title, exact: true}).click();
      await waitFor(async () => (await currentSource())?.id === H.behind, 'the left-behind record');
      assert.deepEqual(await currentSource(), {type: 'task', id: H.behind, business_id: P.alpha}, 'the Task left behind opens in its own project');
      assert.equal(await currentSlug(), 'alpha');
      await pane().locator('.rw-title', {hasText: H.behind_title}).waitFor();
      await shot(pane(), 'scenario-2-left-behind-record.png');
      assert.ok(!(await pane().innerText()).includes('Gamma decoy'), 'the destination decoy never opens');
    });

    // ==========================================================================================================================
    // 3. A concrete blocker, a stale preview and a duplicate submission.
    // ==========================================================================================================================
    await cleanly('scenario 3 blocker', async () => {
      step('3. A Story containing an Email-bound Task: real preview, Move disabled, nothing changes.');
      const B = fixture.blocked;
      await closeWorkspace();
      await goFocus();
      await openCard(B.card);
      await pane().locator('.rw-move').waitFor();
      const before = await settled();
      await openMove(pane().locator('.rw-move'));
      await choose('Beta Lab');
      const text = await previewText();
      assert.match(text, /This move is blocked:/);
      assert.match(text, /A Task bound to an email conversation cannot move/);
      assert.ok(text.includes(`(${B.mail_title})`), 'the blocker names the Task it concerns');
      for (const line of countsText(B.expected_counts)) assert.ok(text.includes(line), line);
      assert.equal(await dialog().locator('[data-move-confirm]').isDisabled(), true, 'Move is disabled by the real blocker');
      await dialog().locator('[data-move-confirm]').click({force: true, timeout: 2000}).catch(() => {});
      await shot(dialog(), 'scenario-3-blocked-preview.png');
      const preview = opResponses('tasks_move_to_project_preview').at(-1).body.data.preview;
      assert.equal(preview.can_move, false);
      assert.deepEqual(preview.blockers.map(item => [item.code, item.task_id]), [['email_binding', B.mail]]);
      await dialog().locator('[data-move-cancel]').click();
      await waitFor(async () => (await dialog().count()) === 0, 'the dialog to close');
      assert.equal(opCalls('tasks_move_to_project').filter(call => call.body.task_ids.includes(B.id)).length, 0, 'a blocked preview is never submitted');
      assert.deepEqual(await settled(), before, 'a blocked Move leaves every record intact');
    });

    await cleanly('scenario 3 stale', async () => {
      step('3. A descendant changes after the preview: real revision conflict, fresh preview, explicit second confirmation.');
      const T = fixture.stale;
      await closeWorkspace();
      await openCard(T.card);
      await pane().locator('.rw-move').waitFor();
      const before = await settled();
      await openMove(pane().locator('.rw-move'));
      await choose('Beta Lab');
      const first = opResponses('tasks_move_to_project_preview').at(-1).body.data.preview;
      assert.ok(first.tasks.some(row => row.title === T.child_title));
      assert.equal(first.can_move, true);
      const edited = await control('revise-title', {business_id: P.alpha, task_id: T.child, title: T.edited_title});
      assert.ok(edited.revision > 1);
      const edit = await settled();
      assert.notDeepEqual(edit.tables.tasks, before.tables.tasks, 'the concurrent edit really changed storage');
      const conflict = allow(409, '/operations/tasks_move_to_project');
      const previewsBefore = opCalls('tasks_move_to_project_preview').length;
      await dialog().locator('[data-move-confirm]').click();
      await dialog().locator('[data-move-error]', {hasText: 'changed since the preview'}).waitFor();
      await waitFor(async () => opCalls('tasks_move_to_project_preview').length === previewsBefore + 1, 'the fresh preview');
      assert.ok(conflict.used && conflict.code === 'revision_conflict', 'the host refused the stale token with revision_conflict');
      assert.equal(opCalls('tasks_move_to_project').filter(call => call.body.task_ids.includes(T.id)).length, 1, 'the stale submission is never retried automatically');
      assert.equal(await dialog().locator('[data-move-destination]').inputValue(), P.beta, 'the chosen destination is preserved');
      await dialog().locator('[data-move-confirm]:not([disabled])').waitFor();
      const text = await previewText();
      assert.ok(text.includes(T.edited_title), 'the fresh preview reads the edited descendant');
      assert.deepEqual(await settled(), edit, 'no optimistic move: the stale attempt changed nothing, and the Story is still in Alpha');
      const second = opResponses('tasks_move_to_project_preview').at(-1).body.data.preview;
      assert.notEqual(second.token, first.token, 'a fresh token');
      await shot(dialog(), 'scenario-3-stale-conflict.png');
      await dialog().locator('[data-move-confirm]').click();
      await waitFor(async () => (await dialog().count()) === 0, 'the dialog to close');
      await waitFor(async () => (await currentSource())?.business_id === P.beta, 'navigation to the destination');
      assert.deepEqual(await currentSource(), {type: 'task', id: T.id, business_id: P.beta});
      const sent = opCalls('tasks_move_to_project').filter(call => call.body.task_ids.includes(T.id));
      assert.equal(sent.length, 2);
      assert.equal(sent[0].body.preview_token, first.token);
      assert.equal(sent[1].body.preview_token, second.token, 'the second Move carries the fresh token');
      assert.notEqual(sent[1].body.idempotency_key, sent[0].body.idempotency_key, 'a new preview gets a new idempotency key');
      const reply = opResponses('tasks_move_to_project').at(-1);
      assert.equal(reply.status, 200);
      result.uiMoves.push({label: 'stale-story', key: sent[1].body.idempotency_key, token: second.token, move_id: reply.body.data.move_id,
        moved: reply.body.data.moved.map(row => row.id), conflictedKey: sent[0].body.idempotency_key});

      step('3. The duplicate same-key submission returns the receipt and the audit still holds one move.');
      const afterMove = await settled();
      const replay = await direct('tasks_move_to_project', sent[1].body);
      assert.equal(replay.status, 200);
      assert.deepEqual(replay.body.data, reply.body.data);
      assert.deepEqual(await settled(), afterMove, 'a retry never executes twice');
      assert.equal(afterMove.moves, 4 + 2 + 2 + fixture.chat.seed_move.ids.length,
        'the audit holds the three moves (4 + 2 + 2 Task rows) and the fixture’s own history (the Chat Tasks moved before the page loaded)');
    });


    // ==========================================================================================================================
    // 4. A project Story whose child's Action and decided Signoff were recorded before the Story moved.
    // ==========================================================================================================================
    await cleanly('scenario 4', async () => {
      step('4. A project Story with an Action and a decided Signoff on its child: Move it to Beta through the real UI.');
      const L = fixture.launch;
      await closeWorkspace();
      await goFocus();
      await openCard(L.card);
      assert.deepEqual(await currentSource(), {type: 'task', id: L.id, business_id: P.alpha});
      await openMove(pane().locator('.rw-move'));
      await choose('Beta Lab');
      await dialog().locator('[data-move-confirm]:not([disabled])').waitFor();
      await dialog().locator('[data-move-confirm]').click();
      await waitFor(async () => (await dialog().count()) === 0, 'the dialog to close after the move');
      await waitFor(async () => (await currentSource())?.business_id === P.beta, 'navigation to the destination Story');
      assert.deepEqual(await currentSource(), {type: 'task', id: L.id, business_id: P.beta});
      const sent = opCalls('tasks_move_to_project').at(-1), reply = opResponses('tasks_move_to_project').at(-1);
      assert.equal(reply.status, 200);
      assert.deepEqual(sent.body.task_ids, [L.id]);
      assert.deepEqual(reply.body.data.moved.map(row => row.id).sort(), [L.id, L.child].sort());
      result.uiMoves.push({label: 'launch-story', key: sent.body.idempotency_key, token: sent.body.preview_token, move_id: reply.body.data.move_id,
        moved: reply.body.data.moved.map(row => row.id)});

      step('4. The destination Story still lists the pre-move Action and Signoff, from the project they were recorded in.');
      await pane().locator('[data-rw-tab="dashboard"]').click();
      const section = title => pane().locator('.record-section').filter({has: page.locator('h4', {hasText: new RegExp('^' + title + '\\b')})}).first();
      const actionRow = section('Actions').locator('.record-link-open').filter({hasText: L.action.label});
      const signoffRow = section('Signoffs').locator('.record-link-open').filter({hasText: L.signoff.label});
      await actionRow.waitFor();
      await signoffRow.waitFor();
      assert.equal(await actionRow.count(), 1, 'the Action is listed once');
      assert.equal(await signoffRow.count(), 1, 'the Signoff is listed once');
      for (const kind of ['action', 'signoff']) {
        const read = opResponses('tasks_associated_records').filter(row => row.request.record_type === kind && row.request.task_id === L.id).at(-1);
        assert.equal(read.status, 200);
        assert.equal(read.path, '/api/biz/beta/operations/tasks_associated_records', 'read through the destination Story');
        const item = read.body.data.items.find(row => row.id === L[kind].id);
        assert.ok(item, `the ${kind} is returned\n${JSON.stringify(read.body.data)}`);
        assert.equal(item.business_id, P.alpha, `the ${kind} names the project that owns it, not the Story's new project`);
        assert.ok(item.task_ids.includes(L.child), 'through the child Task it was recorded on');
      }
      await shot(pane(), 'scenario-4-moved-story-history.png');

      step('4. Opening each pre-move record reads the real record in its own (source) scope.');
      await actionRow.click();
      await waitFor(async () => (await currentSource())?.id === L.action.id, 'the Action record');
      const action = await currentSource();
      assert.equal(action.business_id, P.alpha, 'the Action opens in the project it was recorded in');
      assert.equal(await currentSlug(), 'alpha');
      await pane().locator('.rw-title', {hasText: L.action.label}).waitFor();
      await pane().locator('.rw-back').click();
      await waitFor(async () => (await currentSource())?.id === L.id, 'back at the Story');
      await pane().locator('[data-rw-tab="dashboard"]').click().catch(() => {});
      await signoffRow.waitFor();
      await signoffRow.click();
      await waitFor(async () => (await currentSource())?.id === L.signoff.id, 'the Signoff record');
      const signoff = await currentSource();
      assert.equal(signoff.business_id, P.alpha, 'the Signoff opens in the review project it belongs to');
      assert.equal(await currentSlug(), 'alpha');
      await pane().locator('.rw-title', {hasText: L.signoff.label}).waitFor();
      await shot(pane(), 'scenario-4-source-signoff.png');
    });

    // ==========================================================================================================================
    // 5. A Task reference captured in native Chat before its Task moved.
    // ==========================================================================================================================
    await cleanly('scenario 5', async () => {
      step('5. Ordubashy’s native Chat shows the Task references; a Task that moved before load is inert and nothing is disclosed.');
      const C = fixture.chat;
      await closeWorkspace();
      await page.evaluate(() => selectGlobal('agents'));
      await page.locator('button.gchat-task-reference').first().waitFor();
      const chip = title => page.locator('button.gchat-task-reference', {hasText: title});
      const inert = page.locator('.gchat-reference-unavailable');
      assert.equal(await chip(C.live.title).count(), 1, 'the live Task reference is an opener');
      assert.equal(await chip(C.reused.title).count(), 1, 'the reference whose id Alpha reuses resolves to the Task Alpha holds now');
      assert.equal(await chip(C.reused.original_title).count(), 0);
      assert.equal(await inert.count(), 1, 'the Task that moved before the page loaded is unavailable and inert');
      assert.equal(await inert.evaluate(el => el.tagName), 'SPAN');
      assert.ok(!(await page.locator('.gchat-msg').allInnerTexts()).join('\n').includes(C.gone.title), 'the unavailable reference discloses no title');

      step('5. The exact current Task in the old scope wins over the relocation lookup, even though the original of that id moved.');
      const readsBefore = opCalls('owner_conversation_get').length;
      await chip(C.reused.title).click();
      await pane().locator('.rw-title', {hasText: C.reused.title}).waitFor();
      assert.deepEqual(await currentSource(), {type: 'task', id: C.reused.id, business_id: P.alpha}, 'the Alpha Task is opened, not the moved original');
      const readOf = id => opResponses('owner_conversation_get').filter(row => row.request.source?.id === id).at(-1);
      await waitFor(async () => opCalls('owner_conversation_get').length > readsBefore && !!readOf(C.reused.id), 'the record chat read');
      const reusedRead = readOf(C.reused.id);
      assert.equal(reusedRead.status, 200, 'the old-scope record is read, not answered as moved');
      assert.equal(await pane().locator('.rw-moved').count(), 0, 'no moved notice for a Task that is really here');
      await closeWorkspace();

      step('5. The Task moves for real while the page stays open; its earlier reference then opens the moved-record notice.');
      const selection = {source_business_id: P.alpha, destination_project_id: P.beta, task_ids: [C.live.id]};
      const preview = await direct('tasks_move_to_project_preview', selection, 'alpha');
      assert.equal(preview.status, 200, JSON.stringify(preview.body));
      const body = {...selection, preview_token: preview.body.data.preview.token, idempotency_key: 'chat-reference-move-' + C.live.id};
      const moved = await direct('tasks_move_to_project', body, 'alpha');
      assert.equal(moved.status, 200, JSON.stringify(moved.body));
      result.uiMoves.push({label: 'chat-task', key: body.idempotency_key, token: body.preview_token, move_id: moved.body.data.move_id,
        moved: moved.body.data.moved.map(row => row.id), driver: true});
      assert.equal(await chip(C.live.title).count(), 1, 'the open Chat still shows the reference it captured before the move');
      const refusal = allow(409, '/operations/owner_conversation_get');
      // Opening Chat also reads the paired Task details. Both reads must refuse the same old-scope record.
      const detailRefusal = allow(409, '/api/biz/alpha/operations/tasks_get');
      await chip(C.live.title).click();
      await pane().locator('.rw-moved').waitFor();
      assert.match(await pane().locator('.rw-moved').innerText(), /This record moved to Beta Lab\./);
      assert.equal(await pane().locator('.rw-moved button', {hasText: 'Open in new project'}).count(), 1);
      const stale = readOf(C.live.id);
      assert.equal(stale.status, 409);
      assert.equal(stale.body.error.code, 'record_moved');
      assert.equal(stale.body.error.details.destination.id, P.beta);
      await waitFor(async () => detailRefusal.used, 'the old-scope Task details refusal');
      const staleDetails = opResponses('tasks_get').filter(row => row.status === 409).at(-1);
      assert.equal(staleDetails.path, '/api/biz/alpha/operations/tasks_get');
      assert.equal(staleDetails.request.task_id, C.live.id, 'the paired details read targets the same moved Task');
      assert.equal(staleDetails.body.error.code, 'record_moved');
      assert.equal(staleDetails.body.error.details.destination.id, P.beta);
      await shot(pane(), 'scenario-5-moved-notice.png');
      assert.ok(refusal.used && refusal.code === 'record_moved', 'the host refused the old-scope read as record_moved');
      await pane().locator('.rw-moved button', {hasText: 'Open in new project'}).click();
      await waitFor(async () => (await currentSource())?.business_id === P.beta, 'the record in the destination project');
      assert.deepEqual(await currentSource(), {type: 'task', id: C.live.id, business_id: P.beta}, 'the same Task id in its destination project');
      assert.equal(await currentSlug(), 'beta');
      await pane().locator('.rw-title', {hasText: C.live.title}).waitFor();
      assert.ok(!(await pane().innerText()).includes(C.gamma_decoy.title), 'the Gamma Task that shares the bare id is never substituted');
      await waitFor(async () => readOf(C.live.id)?.status === 200, 'the destination chat read');
      assert.deepEqual([...new Set(opCalls('owner_conversation_get').filter(call => call.body?.source?.id === C.live.id).map(call => call.body.source.business_id))].sort(),
        [P.alpha, P.beta].sort(), 'the Task’s record was read in the scope it was captured in and the one it moved to, never in Gamma, which only shares its id');
      await shot(pane(), 'scenario-5-destination-record.png');
    });

    await shot(page, 'final.png');
    assert.deepEqual(errors, [], 'no unexpected page or HTTP error');
    assert.ok(allowed.every(item => item.used), 'every expected refusal happened: ' + JSON.stringify(allowed.filter(item => !item.used)));
    result.expectedRefusals = allowed.map(item => ({status: item.status, suffix: item.suffix, code: item.code}));
    assert.ok(result.blockedExternal.every(url => new URL(url).origin !== origin));
    if (resultFile) fs.writeFileSync(resultFile, JSON.stringify(result, null, 2));
    console.log('task move composition checks passed');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
