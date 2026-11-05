// Task/Action/thread/tag and Story-removal consumers against synthetic DTOs. No live host, data or browser state.
//   PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_operational_consumers_browser.js
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const SCRIPTS = ['core', 'safe-links', 'san-juan-time', 'chat-view', 'task-state', 'tags', 'work-groups', 'tasks', 'task-snooze', 'actions', 'uploads', 'task-progress', 'signoffs', 'relationships', 'email'];
const SHOTS = process.env.EVIDENCE_DIR;
const html = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/tasks.css"><link rel="stylesheet" href="/css/safe-links.css"><link rel="stylesheet" href="/css/chat-view.css"><link rel="stylesheet" href="/css/actions.css"><link rel="stylesheet" href="/css/work-groups.css"><link rel="stylesheet" href="/css/conversation.css"><link rel="stylesheet" href="/css/email.css">
<button id="wsbtn" hidden></button><div id="host" style="padding:16px"></div><div id="actionhost" style="padding:16px"></div>
${SCRIPTS.map(name => `<script src="/js/${name}.js"></script>`).join('')}`;

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  try {
    for (const viewport of [{width: 1100, height: 900}, {width: 390, height: 800}]) {
      const page = await browser.newPage({viewport}), errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.hostname !== 'work.test') return route.abort();
        if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: html});
        const file = path.join(root, url.pathname);
        return fs.existsSync(file) ? route.fulfill({path: file}) : route.fulfill({status: 404, body: ''});
      });
      await page.goto('http://work.test/');
      await page.evaluate(() => {
        state.biz = 'studio'; state.scope = 'business'; state.session = {principal_id: 'owner-1', display_name: 'Owner', is_owner: true};
        state.bizs = [{slug: 'studio', id: 'biz-1', name: 'Studio', agents: ['marketer'], owner: 'Owner'}];
        window.requests = []; window.writes = [];
        const base = {revision: 1, closed: false, archived: false, due: '2030-01-05', snoozed_until: null, snoozed: false, blocked_by: [], blocks: [], unresolved_blockers: [], owners: ['agent:marketer'], tags: ['marketing-campaign'], business: state.bizs[0]};
        window.parent = {...base, id: 'parent-1', title: 'Launch group', status: 'working', archived: true, children: [{id: 'child-1', title: 'Draft copy', status: 'open'}, {id: 'child-2', title: 'Pick images', status: 'waiting'}], children_has_more: false, parent: null, blocked_by: ['dep-1'], unresolved_blockers: ['dep-1'], campaign: {responsible_agent_id: null}};
        window.tasksList = [{...base, id: 'task-a', title: 'Alpha task', status: 'open'}, {...base, id: 'task-b', title: 'Beta task', status: 'waiting'}];
        const json = data => new Response(JSON.stringify({status: 'ok', data}), {headers: {'Content-Type': 'application/json'}});
        window.fetch = async (url, options = {}) => {
          url = String(url); const op = url.split('/operations/')[1], args = JSON.parse(options.body || '{}');
          requests.push({url, op, args});
          if (op === 'tasks_update') { writes.push({op, args}); Object.assign(parent, args.changes); parent.revision++; return json({task: {...parent}}); }
          if (/\/task\/[^/]+$/.test(url) && options.method === 'POST') { writes.push({op: 'task_write', args}); Object.assign(parent, args.set); parent.revision++; return json({task: {...parent}}); }
          if (op === 'tasks_list') return json({tasks: args.tags?.includes('marketing-campaign') ? [{...parent}] : tasksList, next_cursor: null});
          if (op === 'tags_list') return json({tags: [{id: 'g1', label: 'launch', created_at: '2030-01-01T00:00:00Z'}], next_cursor: null});
          if (op === 'agents_list') return json({agents: [{agent_id: 'agent-m', display_name: 'Marketer', status: 'active'}]});
          if (op === 'campaign_responsible_set') { writes.push({op, args}); parent.campaign = {responsible_agent_id: args.agent_id}; parent.revision++; return json({task: {...parent}}); }
          if (/\/signoffs(\?|$)/.test(url)) return new Response(JSON.stringify({status: 'ok', data: {items: [{id: 'so-1', state: 'pending', category: 'platform_action', title: 'Post to channel', operation_name: 'platform.post', created_at: '2030-01-01T00:00:00Z', status_revision: 4, proposal_revision: 1, payload_hash: 'h', platform: {campaign_id: null}, attention: {}}], review_business_id: 'biz-1', counts: {pending: 1}, next_cursor: null}}), {headers: {'Content-Type': 'application/json'}});
          if (/\/session$/.test(url)) return json({principal_kind: 'human', is_owner: true, principal_id: 'owner-1'});
          if (op === 'campaign_attribution_set') { writes.push({op, args}); return json({}); }
          if (op === 'context_reviews_list') {
            if (window.failRelated) return new Response(JSON.stringify({status: 'error', error: {code: 'unavailable', message: 'Related context is unavailable'}}), {status: 503, headers: {'Content-Type': 'application/json'}});
            return json({items: [{relationship_id: 'r1', direction: 'outgoing', role: 'concerns', pending: true, available: true, context: '', latest_review: null,
              source_item: {type: 'task', id: 'rel-task', label: 'Related launch task', resource_type: 'tasks'}}], next_cursor: null, item_revision: 1});
          }
          // UNINTEGRATED section G contract: the frozen host root route has no relationships_list yet; this mock is the requested
          // canonical read (with its explicit `scope`), not evidence that the real Global route answers it.
          if (op === 'relationships_list') return json({items: [{id: 'rel-g', role: 'concerns', source: args.item, target: {type: 'task', id: 'other-g'},
            source_item: {type: 'task', id: args.item.id, label: 'This Global task'}, target_item: {type: 'task', id: 'other-g', label: 'Linked Global task', resource_type: 'tasks'}}], next_cursor: null});
          if (op === 'actions_thread_get') return json({participants: [{id: 'owner-1', display_name: 'Owner'}, {id: 'agent-m', display_name: 'Marketer'}], messages: [
            {id: 'm1', kind: 'comment', author_id: 'agent-m', recorder_id: 'agent-m', body: 'Ran the send.', created_at: '2030-01-02T10:00:00Z'},
            {id: 'm2', kind: 'comment', author_id: 'owner-1', recorder_id: 'agent-m', body: 'Owner agreed by phone.', created_at: '2030-01-02T11:00:00Z'}], next_cursor: null});
          if (op === 'actions_correct') { writes.push({op, args}); return json({action: {action_id: 'global/a1', text: args.text, revision: 2, can_change: true, tags: args.tags || [], withdrawn: false}}); }
          if (op === 'actions_follow') { writes.push({op, args}); return json({}); }
          return json({});
        };
        tFetchTask = async (_biz, id) => ({...parent, id, title: id === 'dep-1' ? 'A deliberately long dependency title that must wrap without covering its label' : parent.title, blocked_by: []});
        taskProgressMount = host => { host.textContent = 'Conversation'; };
        tDetail({...parent}, {host: document.querySelector('#host')});
      });
      const host = page.locator('#host');
      await host.locator('.tdetail').waitFor();

      // The short "Depends on" label sits beside its value without overlap; the informational note is separate help.
      await host.locator('[data-blocked-by] button').first().waitFor();
      const geometry = await host.evaluate(el => {
        const key = [...el.querySelectorAll('.k')].find(node => node.textContent.trim() === 'Depends on'), value = key.nextElementSibling, a = key.getBoundingClientRect(), b = value.getBoundingClientRect();
        return {overlap: !(a.right <= b.left + 1 || b.right <= a.left + 1 || a.bottom <= b.top + 1 || b.bottom <= a.top + 1), help: value.querySelector('.task-field-help')?.textContent, scrolls: document.documentElement.scrollWidth > innerWidth};
      });
      assert.equal(geometry.overlap, false, 'label and value do not overlap'); assert.match(geometry.help, /Informational/); assert.equal(geometry.scrolls, false);
      assert.equal(await page.evaluate(() => document.body.textContent.includes('contextRelatedChips is not defined')), false);
      if (SHOTS) { fs.mkdirSync(SHOTS, {recursive: true}); await host.screenshot({path: path.join(SHOTS, `task-detail-${viewport.width}.png`)}); }

      // Fixed states: completing the parent writes only the parent; children and archive/due stay as read.
      await page.locator('#td-status').click();
      await page.locator('.menu .mi').filter({hasText: 'Done'}).click();
      await page.waitForFunction(() => writes.some(w => w.op === 'tasks_update'));
      let write = await page.evaluate(() => writes.find(w => w.op === 'tasks_update').args);
      assert.deepEqual(write.changes, {status: 'done'});
      assert.equal(write.task_id ?? write.id, 'parent-1');
      assert.equal(await page.evaluate(() => writes.length), 1, 'only the parent is written');
      assert.deepEqual(await page.evaluate(() => parent.children.map(c => c.status)), ['open', 'waiting'], 'children keep their own states');
      assert.equal(await page.evaluate(() => [parent.archived, parent.due]).then(v => v.join()), 'true,2030-01-05', 'archive and due are untouched by state');

      // One shared snooze timestamp; it never carries due or archive.
      await page.evaluate(() => { writes.length = 0; });
      await page.locator('#td-snooze').click();
      await page.getByRole('button', {name: 'Tomorrow', exact: true}).click();
      await page.locator('.task-controls-popover').getByRole('button', {name: 'Apply', exact: true}).click();
      await page.waitForFunction(() => writes.some(w => typeof w.args.set?.snoozed_until === 'string'));
      write = await page.evaluate(() => writes.at(-1).args);
      assert.deepEqual(Object.keys(write.set), ['snoozed_until']);

      // Tag catalog: applying a label writes names only, the owning scope stays in the request path.
      await page.getByRole('textbox', {name: 'Add a tag', exact: true}).click();
      await page.locator('.tag-option[data-tag="launch"]').click();
      await page.getByRole('textbox', {name: 'Add a tag', exact: true}).fill('brand new');
      await page.keyboard.press('Enter');
      await page.getByRole('button', {name: 'Save tags', exact: true}).click();
      await page.waitForFunction(() => writes.some(w => w.args.set?.tags?.includes('brand new')));
      assert.deepEqual(await page.evaluate(() => writes.at(-1).args.set.tags), ['marketing-campaign', 'launch', 'brand new']);
      assert.equal(await page.evaluate(() => requests.filter(r => r.op === 'tags_list').every(r => r.url.includes('/biz/studio/') && !('business_id' in r.args))), true);

      assert.equal(await page.locator('#td-responsible').count(), 0, 'no campaign responsibility control');

      // Related resources use the real helper: a successful open, then an unavailable read that can be retried.
      await page.locator('#td-work').getByRole('button', {name: 'Related context', exact: true}).waitFor();
      await page.locator('#td-work').getByText('1 related · 1 pending', {exact: true}).waitFor();
      await page.evaluate(() => { window.openedTasks = []; const fetchTask = tFetchTask; tFetchTask = async (biz, id) => { openedTasks.push(id); return {...(await fetchTask(biz, id)), id, title: 'Opened ' + id}; }; });
      await page.locator('#td-work').getByRole('button', {name: 'Related context', exact: true}).click();
      await page.locator('.context-source', {hasText: 'Related launch task'}).click();
      await page.waitForFunction(() => openedTasks.includes('rel-task'));
      await page.evaluate(() => { window.failRelated = true; const box = document.createElement('div'); box.id = 'retry-box'; document.body.append(box); workRelatedChips(box, 'studio', 'tasks', 'parent-1'); });
      await page.locator('#retry-box').getByRole('button', {name: 'Retry related context', exact: true}).waitFor();
      assert.match(await page.locator('#retry-box').innerText(), /Related context is unavailable/);
      await page.evaluate(() => { window.failRelated = false; });
      await page.locator('#retry-box').getByRole('button', {name: 'Retry related context', exact: true}).click();
      await page.locator('#retry-box').getByText(/1 related/).waitFor();

      // An email link is a historical reference: it opens an explicit note and reads no message, conversation or Context item.
      await page.evaluate(() => { window.requests.length = 0; return workOpenLink({biz: 'studio'}, {resource_type: 'email_messages', resource_id: 'msg-2', current: {label: 'Re: Pilot'}}); });
      await page.locator('.modal').getByText(/historical email reference/).waitFor();
      assert.equal(await page.evaluate(() => requests.length), 0, 'a historical email reference fetches nothing');
      await page.keyboard.press('Escape');

      // Global related work: authorized canonical relationships only, no pending count or review controls, and opening an item
      // never switches the page to a business.
      await page.evaluate(() => {
        window.selected = []; window.globalOpened = [];
        selectBiz = async slug => { selected.push(slug); };
        globalTasksMount = (el, options) => { globalOpened.push(options.initialTaskId); el.textContent = 'Global task ' + options.initialTaskId; };
        const box = document.createElement('div'); box.id = 'global-related'; document.body.append(box);
        requests.length = 0; return contextRelatedChips(box, null, {type: 'task', id: 'global-1'});
      });
      await page.locator('#global-related').getByText('1 related', {exact: true}).waitFor();
      assert.equal(await page.locator('#global-related').innerText().then(text => /pending/.test(text)), false, 'Global shows no pending review count');
      assert.deepEqual(await page.locator('#global-related button').allInnerTexts(), ['Related context']);
      assert.deepEqual(await page.evaluate(() => requests.map(r => [r.url.split('/operations/')[0], r.op, r.args.scope, r.args.role])), [['/api/root', 'relationships_list', 'global', undefined]]);
      assert.equal(await page.evaluate(() => requests.some(r => r.op.startsWith('context_reviews_'))), false, 'Global never reads or writes relationship reviews');
      await page.locator('#global-related').getByRole('button', {name: 'Related context', exact: true}).click();
      assert.equal(await page.locator('.modal .context-relation button:not(.context-source)').count(), 0, 'no review or acknowledgment controls');
      await page.locator('.context-source', {hasText: 'Linked Global task'}).click();
      await page.waitForFunction(() => globalOpened.includes('other-g'));
      assert.deepEqual(await page.evaluate(() => selected), [], 'opening Global related work never calls selectBiz(null)');
      await page.evaluate(() => { for (let i = 0; i < 4 && document.querySelector('.overlay'); i++) _modal?.close(); });
      await page.waitForFunction(() => !document.querySelector('.overlay'));

      // Multiply linked material selection reads Tasks, never Stories.
      await page.evaluate(() => workChooseTasks('studio', [], tasks => { window.chosen = tasks; }));
      await page.locator('.work-association-choice input[data-task-id="task-a"]').check();
      await page.locator('.work-association-choice input[data-task-id="task-b"]').check();
      await page.locator('.modal').getByRole('button', {name: 'Use selected tasks', exact: true}).click();
      const payload = await page.evaluate(() => upPayload({text: 'note', notes: '', topic: '', tasks: chosen}));
      assert.deepEqual(payload.task_ids, ['task-a', 'task-b']); assert.equal('story_ids' in payload, false);
      assert.equal(typeof workCampaignOptions, 'undefined', 'campaign options are retired');

      // Actions: correction is an immutable revision with the exact revision; the thread shows author vs recorder;
      // the owner gets follow controls but no shared comment box.
      await page.evaluate(() => {
        const action = {action_id: 'global/a1', text: 'Sent the launch email', revision: 1, can_change: true, tags: [], withdrawn: false};
        const box = document.querySelector('#actionhost'); acDiscussionMount(box, action);
      });
      await page.getByText('Ran the send.').waitFor();
      assert.match(await page.locator('#actionhost .gchat-msg').filter({hasText: 'Owner agreed by phone.'}).innerText(), /Owner · recorded by Marketer/);
      assert.equal(await page.locator('#actionhost textarea:visible, #actionhost [contenteditable]:visible, #actionhost .gchat-send:visible').count(), 0, 'no visible shared owner comment box');
      await page.locator('#actionhost [data-follow="true"]').click();
      await page.waitForFunction(() => writes.some(w => w.op === 'actions_follow'));
      assert.equal(await page.evaluate(() => writes.find(w => w.op === 'actions_follow').args.following), true);

      // Signoffs keep their ordinary review card; the campaign attribution control and badge are gone.
      await page.evaluate(() => {
        const box = document.createElement('div'); box.id = 'sgbox'; document.body.append(box);
        window.sgView = sgCreateView({category: 'platform_action', prefix: 'x-', waitingOnly: true, active: () => true, body: () => box});
        sgView.paint();
      });
      await page.locator('#sgbox').getByText('Post to channel').waitFor();
      assert.equal(await page.locator('[data-campaign-change], .campaign-badge, [data-sg=campaign-filter]').count(), 0);
      assert.equal(await page.evaluate(() => requests.some(r => r.op === 'campaign_attribution_set')), false);

      // No active Story request anywhere in this session.
      assert.deepEqual(await page.evaluate(() => requests.filter(r => /stor(y|ies)/i.test(r.url + JSON.stringify(r.args))).map(r => r.op)), []);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, 'fits the viewport');
      assert.deepEqual(errors, []);
      if (SHOTS) { fs.mkdirSync(SHOTS, {recursive: true}); await page.screenshot({path: path.join(SHOTS, `operational-consumers-${viewport.width}.png`), fullPage: true}); }
      await page.close();
    }
    console.log('Operational consumers: fixed state/snooze/due/archive, parent without child changes, tag catalog, no campaign controls, task_ids, Action thread attribution and no Story requests passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
