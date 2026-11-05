#!/usr/bin/env node
// Real Chromium DOM with the real js/chat-view.js, js/safe-links.js and js/core.js over synthetic presentation models and mock reference reads.
//   PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_chat_view_browser.js
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
let checks = 0;
const check = async (label, action) => { await action(); checks++; console.log('ok ' + label); };

(async () => {
  const launch = {headless: true, args: ['--no-sandbox']};
  if (process.env.CHROMIUM_EXECUTABLE) launch.executablePath = process.env.CHROMIUM_EXECUTABLE;
  const browser = await chromium.launch(launch);
  try {
    const page = await browser.newPage({viewport: {width: 1000, height: 800}, locale: 'en-US', timezoneId: 'America/La_Paz'});
    const errors = [];
    page.on('pageerror', error => errors.push(error.stack || error.message));
    const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64');
    await page.route('http://fixture.test/**', route => route.request().url().includes('/api/root/files/image')
      ? route.fulfill({body: pixel, contentType: 'image/png'}) : route.fulfill({body: '<html></html>', contentType: 'text/html'}));
    await page.goto('http://fixture.test/');
    await page.setContent('<div id="toasts"></div><button id="wsbtn"></button><div id="host" style="height:300px;width:600px;display:flex;flex-direction:column"></div><div id="outside"></div>');
    await page.addScriptTag({path: path.join(root, 'js/core.js')});
    await page.addScriptTag({path: path.join(root, 'js/safe-links.js')});
    await page.addScriptTag({path: path.join(root, 'js/chat-view.js')});
    await page.addStyleTag({path: path.join(root, 'css/chat-view.css')});
    await page.evaluate(() => {
      window.requests = [];
      state.bizs = [{id: 'biz-a', slug: 'alpha'}, {id: 'biz-b', slug: 'beta'}, {id:'22222222-2222-4222-8222-222222222222',slug:'linked-project'}];
      window.readWaiters = new Map(); window.tableWaiters = new Map(); window.readActive = 0; window.readMax = 0;
      window.fetch = async (url, options = {}) => {
        const body = options.body ? JSON.parse(options.body) : {};
        window.requests.push({url, body});
        const ok = data => new Response(JSON.stringify({status: 'ok', data}), {status: 200});
        const missing = () => new Response(JSON.stringify({status: 'error', error: {code: 'not_found', message: 'Missing'}}), {status: 404});
        const denied = () => new Response(JSON.stringify({status: 'error', error: {code: 'forbidden', message: 'Denied'}}), {status: 403});
        if (url.endsWith('/tasks_get') && body.task_id === 'denied') return denied();
        if (url.endsWith('/tasks_get') && body.task_id === 't1') return ok({task: {id: 't1', title: 'Resolved contact', task_type: 'customer', customer_profile: {email: 'person@example.test', company: 'Example Co'}}});
        if (url.endsWith('/tasks_get') && (body.task_id === 'stale' || String(body.task_id).startsWith('held-'))) {
          window.readActive++; window.readMax = Math.max(window.readMax, window.readActive);
          await new Promise(resolve => window.readWaiters.set(body.task_id, resolve));
          window.readActive--;
          return ok({task: {id: body.task_id, title: 'Late ' + body.task_id, task_type: 'task'}});
        }
        if (url.endsWith('/tables_get') && body.table_id === 'table-1') return ok({table: {id: 'table-1', title: 'Support CRM', description: 'Current support customers', table_kind: 'crm', row_count: 7}});
        if (url.endsWith('/tables_get') && body.table_id === 'table-2') return ok({table: {id: 'table-2', title: 'Reports', table_kind: 'general', description: '', row_count: 3}});
        if (url.endsWith('/tables_get') && body.table_id === 'table-3') return ok({table: {id: 'table-3', title: 'Sparse table', table_kind: 'general'}});
        if (url.endsWith('/tables_get') && body.table_id === '11111111-1111-4111-8111-111111111111') return ok({table: {id: body.table_id, title: 'Linked CRM', table_kind: 'crm', row_count: 4}});
        if (url.endsWith('/tables_get') && body.table_id === '33333333-3333-4333-8333-333333333333') {
          await new Promise(resolve => window.tableWaiters.set(body.table_id, resolve));
          return ok({table: {id: body.table_id, title: 'Delayed CRM', description: 'Loaded after replay', table_kind: 'crm', row_count: 9}});
        }
        if (url.endsWith('/tasks_get') && body.task_id === 'gone') return missing();
        return missing();
      };
      window.events = [];
      window.entry = (n, extra = {}) => ({key: 'k' + n, speaker: 'agent', text: 'message ' + n, timestamp: '2026-10-04T21:25:00Z', position: n, ...extra});
      window.many = (from, to) => Array.from({length: to - from + 1}, (_, i) => window.entry(from + i, {text: 'line ' + (from + i) + '\n\n' + 'filler '.repeat(40)}));
      window.mount = (options = {}, host = document.querySelector('#host')) => {
        window.events.length = 0; window.view?.dispose(); host.replaceChildren();
        window.requests.length = 0; window.readWaiters.clear(); window.tableWaiters.clear(); window.readActive = 0; window.readMax = 0;
        const record = name => (...args) => window.events.push([name, ...args]);
        window.view = mountChatView(host, {onDraft: record('draft'), onSubmit: record('submit'), onOlder: record('older'), onViewportChange: record('viewport'),
          onReference: (value, key) => window.events.push(['reference', value, key]), onEntryAction: record('entry-action'), onPendingAction: record('pending-action'),
          onEmptyAction: record('empty-action'), ...options});
        return window.view;
      };
    });
    // Reads the callbacks seen since the last read (viewport reports are checked separately).
    const events = () => page.evaluate(() => window.events.splice(0).filter(e => e[0] !== 'viewport'));
    const settle = () => page.waitForTimeout(80);

    await check('construction paints the shared hooks and calls no callback; the view makes no request', async () => {
      await page.evaluate(() => mount({logLabel: 'Conversation with Test', composerLabel: 'Message Test', placeholder: 'Message Test…', maxLength: 123}));
      assert.deepEqual(await page.evaluate(() => Object.keys(window.view.elements).sort()), ['banner', 'composer', 'empty', 'input', 'jump', 'log', 'older', 'pending', 'scroll', 'send']);
      assert.equal(await page.locator('.chat-view .gchat-scroll .gchat-log').getAttribute('aria-label'), 'Conversation with Test');
      assert.equal(await page.getByRole('textbox', {name: 'Message Test'}).getAttribute('maxlength'), '123');
      assert.equal(await page.getByRole('textbox', {name: 'Message Test'}).getAttribute('placeholder'), 'Message Test…');
      assert.deepEqual(await page.evaluate(() => window.events), []);
      assert.deepEqual(await page.evaluate(() => window.requests), []);
    });

    await check('entries render shared roles, markdown, timestamps, titles, author labels and structured references', async () => {
      await page.evaluate(() => window.view.render({entries: [
        entry(1, {speaker: 'user', text: 'plain **text**', timestamp: null}),
        entry(2, {text: '**bold** and [site](https://example.test/page)', author: {name: 'Marketing', avatarHtml: '<svg id="glyph"></svg>'}, title: 'Weekly update',
          references: [{key: 'r0', label: 'Task', value: {type: 'task', id: 't1', business_id: null}}, {key: 'r1', label: 'Gone', disabled: true, value: null}],
          actions: [{key: 'parent', label: 'About an earlier entry'}]}),
        entry(3, {speaker: 'notice', text: 'Moved to waiting', timestamp: 'garbage'}),
        entry(4, {speaker: 'notice', text: 'Technical', notice: {label: 'Inbox nudge', verified: true, inputType: 'inbox_nudge', markerOnly: true}}),
        entry(5, {speaker: 'notice', text: 'Looks like a nudge', notice: {label: 'Unverified notice', verified: false, inputType: 'inbox_nudge'}}),
      ]}));
      const msg = id => page.locator(`.gchat-msg[data-id="${id}"]`);
      assert.deepEqual(await page.locator('.gchat-log .gchat-msg').evaluateAll(n => n.map(x => x.dataset.id)), ['k1', 'k2', 'k3', 'k4', 'k5']);
      assert.equal(await msg('k1').locator('.gchat-text').innerHTML(), 'plain **text**', 'owner text stays plain');
      assert.equal(await msg('k1').locator('time').count(), 0, 'a missing timestamp renders nothing');
      assert.equal(await msg('k3').locator('time').count(), 0, 'an invalid timestamp renders nothing');
      assert.equal(await msg('k2').locator('strong').first().innerText(), 'Weekly update');
      assert.equal(await msg('k2').locator('.gchat-body.md strong').count(), 2, 'title and **bold** are both strong');
      assert.equal(await msg('k2').locator('a.safe-link').getAttribute('href'), 'https://example.test/page');
      assert.equal(await msg('k2').locator('.gchat-author-name').innerText(), 'Marketing');
      assert.equal(await msg('k2').locator('.gchat-avatar #glyph').count(), 1);
      assert.equal((await msg('k2').locator('time').innerText()).length > 4, true);
      assert.equal(await msg('k2').locator('time').getAttribute('datetime'), '2026-10-04T21:25:00.000Z');
      assert.equal(await msg('k2').getByRole('button', {name: 'Preview site'}).count(), 1, 'a safe link gets its preview chip');
      assert.equal(await msg('k2').getByRole('button', {name: 'Gone'}).isDisabled(), true);
      await msg('k2').locator('.gchat-reference-block').waitFor();
      await msg('k2').getByRole('button', {name: /Resolved contact/}).click();
      await msg('k2').getByRole('button', {name: 'Gone'}).click({force: true});
      await msg('k2').getByRole('button', {name: 'About an earlier entry'}).click();
      assert.deepEqual(await events(), [['reference', {type: 'task', id: 't1', business_id: null}, 'k2'], ['entry-action', 'parent', 'k2']]);
      assert.equal(await msg('k3').getAttribute('data-verified'), null, 'a plain notice carries no native provenance');
      assert.equal(await msg('k3').locator('.gchat-marker').count(), 0);
      assert.equal(await msg('k3').locator('.gchat-text').innerText(), 'Moved to waiting');
      assert.equal(await msg('k4').getAttribute('data-verified'), 'true');
      assert.equal(await msg('k4').getAttribute('data-input-type'), 'inbox_nudge');
      assert.equal(await msg('k4').locator('.gchat-text').count(), 0, 'a marker-only notice prints just its marker');
      assert.equal(await msg('k5').getAttribute('data-verified'), 'false');
      assert.equal(await page.evaluate(() => document.body.innerText.includes('k2')), false, 'ids are never printed');
    });

    await check('Table and Task refs enrich once per canonical scope, show only returned metadata, and keep canonical callback values', async () => {
      await page.evaluate(() => {
        mount();
        window.view.render({entries: [entry(10, {references: [
          {key: 'table-a', label: 'Old table label', value: {type: 'table', id: 'table-1', business_id: null}},
          {key: 'table-b', label: 'Duplicate table label', value: {type: 'table', id: 'table-1', business_id: null}},
          {key: 'table-ordinary', label: 'Old ordinary label', value: {type: 'table', id: 'table-2', business_id: null}},
          {key: 'table-no-count', label: 'Old sparse label', value: {type: 'table', id: 'table-3', business_id: null}},
          {key: 'task-a', label: 'Old task label', value: {type: 'task', id: 't1', business_id: null}},
          {key: 'task-gone', label: 'Protected label', value: {type: 'task', id: 'gone', business_id: null}},
        ]})]});
      });
      await page.locator('.gchat-msg[data-id="k10"] .gchat-reference-block').first().waitFor();
      await page.waitForFunction(() => document.querySelector('[data-id="k10"] .gchat-reference-block')?.innerText.includes('Support CRM'));
      const blockText = await page.locator('.gchat-msg[data-id="k10"] .gchat-reference-block').allInnerTexts();
      assert.equal(blockText.filter(text => text.includes('Support CRM')).length, 1, 'equivalent table refs in one message dedupe');
      assert.equal(blockText.some(text => text.includes('7 records') && text.includes('Current support customers')), true);
      assert.equal(blockText.some(text => text.includes('Reports') && text.includes('3 rows')), true);
      assert.equal(blockText.some(text => text.includes('Sparse table') && !/\b\d+ rows\b/.test(text)), true, 'a missing row_count stays omitted');
      assert.equal(blockText.some(text => text.includes('person@example.test')), true);
      assert.equal(blockText.some(text => text.includes('Example Co')), true);
      assert.equal(await page.locator('.gchat-msg[data-id="k10"] .gchat-reference-unavailable').innerText(), 'Unavailable reference');
      assert.equal(await page.locator('.gchat-msg[data-id="k10"] .gchat-reference-unavailable').isDisabled(), true);
      const reads = await page.evaluate(() => window.requests.filter(call => /\/(tables_get|tasks_get)$/.test(call.url)));
      assert.deepEqual(reads.map(call => call.body.table_id || call.body.task_id).sort(), ['gone', 't1', 'table-1', 'table-2', 'table-3'], 'duplicate refs share one read');
      await page.locator('.gchat-msg[data-id="k10"] .gchat-reference-block').first().click();
      assert.deepEqual(await events(), [['reference', {type: 'table', id: 'table-1', business_id: null}, 'k10']]);
    });

    await check('validated same-origin table links become canonical blocks, dedupe with typed and ordinary refs, and preserve prose and copy source', async () => {
      const table = '11111111-1111-4111-8111-111111111111';
      const business = '22222222-2222-4222-8222-222222222222';
      await page.evaluate(({id,biz}) => {
        mount();
        view.render({entries: [
          entry(20, {text: `Before [CRM](/tables/view?table_id=${id}) after.`}),
          entry(21, {text: `[first](/tables/view?table_id=${id}) and [again](/tables/view?table_id=${id})`, references: [{key:'ordinary', value:{type:'table',id,business_id:null}}]}),
          entry(22, {text: `Typed [link](/tables/view?table_id=${id})`, typedReferences:[{type:'table',id,business_id:null,label:'Linked'}]}),
          entry(26, {text: `[Project CRM](/tables/view?table_id=${id}&business_id=${biz})`}),
        ]});
      }, {id:table,biz:business});
      await page.waitForFunction(() => document.querySelectorAll('.gchat-reference-block').length >= 4);
      assert.equal(await page.locator('[data-id="k20"] .gchat-reference-block').count(), 1, 'one internal link yields one enriched block');
      assert.equal(await page.locator('[data-id="k20"] a.safe-link').count(), 0, 'redundant link presentation is removed');
      assert.equal(await page.locator('[data-id="k20"] .gchat-body').innerText(), 'Before after.');
      assert.equal(await page.locator('[data-id="k21"] .gchat-reference-block').count(), 1, 'repeated links and an ordinary ref dedupe');
      assert.equal(await page.locator('[data-id="k22"] .gchat-reference-block').count(), 1, 'typed ref and link dedupe');
      assert.equal(await page.evaluate(biz => requests.some(call => call.url === '/api/biz/linked-project/operations/tables_get' && call.body.table_id === '11111111-1111-4111-8111-111111111111'), business), true, 'project link uses the canonical business_id lookup');
      assert.equal(await page.locator('[data-id="k20"] .gchat-copy').count(), 1);
      await page.locator('[data-id="k20"] .gchat-reference-block').click();
      assert.deepEqual(await events(), [['reference', {type:'table',id:table,business_id:null}, 'k20']]);
    });

    await check('malformed-scope and external table lookalike links retain safe-link behavior', async () => {
      await page.evaluate(() => {
        mount();
        view.render({entries:[
          entry(23,{text:'[bad](/tables/view?table_id=11111111-1111-4111-8111-111111111111&business_id=bad)'}),
          entry(24,{text:'[external](https://evil.test/tables/view?table_id=11111111-1111-4111-8111-111111111111)'})
        ]});
      });
      assert.equal(await page.locator('[data-id="k23"] a.safe-link').count(),1);
      assert.equal(await page.locator('[data-id="k23"] .gchat-reference-block').count(),0);
      assert.equal(await page.locator('[data-id="k24"] a.safe-link').getAttribute('href'), 'https://evil.test/tables/view?table_id=11111111-1111-4111-8111-111111111111');
      assert.equal(await page.locator('[data-id="k24"] .gchat-reference-block').count(),0);
      assert.equal(await page.locator('[data-id="k24"] .gchat-reference').count(),1, 'external lookalike keeps its ordinary preview chip');
    });

    await check('link-only table reads survive transcript replays and settled links stay cached in the mounted view', async () => {
      const table = '33333333-3333-4333-8333-333333333333';
      const linkEntry = {key:'k27',speaker:'agent',text:`[Delayed CRM](/tables/view?table_id=${table})`,timestamp:'2026-10-04T21:25:00Z',position:27};
      await page.evaluate(value => { mount(); view.render({entries:[value]}); }, linkEntry);
      await page.waitForFunction(() => tableWaiters.has('33333333-3333-4333-8333-333333333333'));
      await page.evaluate(value => view.render({entries:[value, entry(28,{text:'A newer message'})]}), linkEntry);
      await page.evaluate(() => tableWaiters.get('33333333-3333-4333-8333-333333333333')());
      await page.waitForFunction(() => document.querySelector('[data-id="k27"] .gchat-reference-block')?.innerText.includes('9 records'));
      assert.match(await page.locator('[data-id="k27"] .gchat-reference-block').innerText(), /Loaded after replay/);
      const readCount = await page.evaluate(id => requests.filter(call => call.body.table_id === id).length, table);
      assert.equal(readCount,1,'the pending link read survived the replay');
      await page.evaluate(value => view.render({entries:[value, entry(28,{text:'A newer message'})]}), linkEntry);
      await page.waitForTimeout(30);
      assert.equal(await page.evaluate(id => requests.filter(call => call.body.table_id === id).length, table),1,'a replay after settlement does not refetch');
    });

    await check('unresolved scopes never fall back to Global and permission denial hides protected reference metadata', async () => {
      await page.evaluate(() => {
        mount();
        view.render({entries: [entry(11, {references: [
          {key: 'unknown-project', label: 'Private Project record', value: {type: 'task', id: 't1', business_id: 'missing-business'}},
          {key: 'denied', label: 'Previously visible', value: {type: 'task', id: 'denied', business_id: null}},
        ]})]});
      });
      await page.waitForFunction(() => document.querySelectorAll('.gchat-reference-unavailable').length === 2);
      assert.deepEqual(await page.locator('.gchat-reference-unavailable').allInnerTexts(), ['Unavailable reference', 'Unavailable reference']);
      assert.equal(await page.locator('.gchat-reference-unavailable').evaluateAll(nodes => nodes.every(node => node.tagName === 'BUTTON' && node.disabled)), true);
      const calls = await page.evaluate(() => requests.map(call => call.url));
      assert.deepEqual(calls, ['/api/root/operations/tasks_get'], 'only the Global reference reads; unresolved Project scope sends no Global request');
    });

    await check('a contradictory business slug is rejected instead of changing the canonical business-id scope', async () => {
      await page.evaluate(() => {
        mount();
        view.render({entries:[entry(25,{references:[{key:'mismatch',value:{type:'task',id:'t1',business_id:'biz-a',business_slug:'beta'}}]})]});
      });
      await page.locator('[data-id="k25"] .gchat-reference-unavailable').waitFor();
      assert.deepEqual(await page.evaluate(() => requests.map(call => call.url)), [], 'mismatched slug causes no read in either project');
    });

    await check('reference reads are limited to four concurrent requests and deduplicated within the mounted view', async () => {
      await page.evaluate(() => {
        mount();
        view.render({entries: [entry(12, {references: Array.from({length: 6}, (_, i) => ({key: 'held-' + i, label: 'Pending ' + i,
          value: {type: 'task', id: 'held-' + i, business_id: null}}))})]});
      });
      await page.waitForFunction(() => readWaiters.size === 4);
      assert.equal(await page.evaluate(() => readMax), 4);
      await page.evaluate(() => readWaiters.get('held-0')());
      await page.waitForFunction(() => readWaiters.size === 5);
      await page.evaluate(() => { for (const release of readWaiters.values()) release(); });
      await page.waitForFunction(() => readWaiters.size === 6);
      await page.evaluate(() => readWaiters.get('held-5')());
      await page.waitForFunction(() => document.querySelectorAll('.gchat-task-reference').length === 6 && [...document.querySelectorAll('.gchat-task-reference')].every(node => node.innerText.startsWith('Late held-')));
      const requested = await page.evaluate(() => requests.map(call => call.body.task_id));
      assert.equal(requested.length, 6); assert.equal(new Set(requested).size, 6);
      assert.equal(await page.evaluate(() => readMax), 4);
    });

    await check('late results are ignored after a reference leaves the view or the view is disposed', async () => {
      await page.evaluate(() => {
        mount();
        view.render({entries: [entry(13, {references: [{key: 'stale', label: 'Stale', value: {type: 'task', id: 'stale', business_id: null}}]})]});
      });
      await page.waitForFunction(() => readWaiters.has('stale'));
      await page.evaluate(() => view.render({entries: [entry(14, {references: [{key: 'live', label: 'Live', value: {type: 'task', id: 't1', business_id: null}}]})]}));
      await page.waitForFunction(() => document.querySelector('.gchat-reference-title')?.textContent === 'Resolved contact');
      await page.evaluate(() => readWaiters.get('stale')());
      await page.waitForTimeout(50);
      assert.equal(await page.locator('.gchat-reference-title').allTextContents().then(text => text.some(label => label.includes('Late stale'))), false);
      await page.evaluate(() => {
        view.render({entries: [entry(15, {references: [{key: 'held-dispose', label: 'Pending disposal', value: {type: 'task', id: 'held-dispose', business_id: null}}]})]});
      });
      await page.waitForFunction(() => readWaiters.has('held-dispose'));
      await page.evaluate(() => { view.dispose(); readWaiters.get('held-dispose')(); });
      await page.waitForTimeout(50);
      assert.equal(await page.locator('.chat-view').count(), 0);
    });

    await check('consecutive agent messages use compact gaps within an hour of the previous message', async () => {
      await page.evaluate(() => {
        mount();
        window.runEntry = (n, minutes, extra = {}) => entry(n, {timestamp: new Date(Date.UTC(2026, 9, 16, 10, minutes)).toISOString(), ...extra});
        window.runEntries = [runEntry(1,0), runEntry(2,55), runEntry(3,110), runEntry(4,170),
          runEntry(5,171,{speaker:'user'}), runEntry(6,172), runEntry(7,173,{author:{name:'Marketing'}}),
          runEntry(8,174,{author:{name:'Marketing'}}), runEntry(9,175,{timestamp:null})];
        view.render({entries:runEntries});
      });
      const compact = () => page.locator('.gchat-continuation').evaluateAll(nodes => nodes.map(n => n.dataset.id));
      assert.deepEqual(await compact(), ['k2','k3','k8'], 'pairwise gaps, hour boundary, owner interruption and author changes');
      assert.equal(await page.locator('[data-id="k2"]').evaluate(n => getComputedStyle(n).marginTop), '8px');
      assert.equal(await page.locator('[data-id="k4"]').evaluate(n => getComputedStyle(n).marginTop), '22px');
      await page.evaluate(() => view.render({entries:[runEntry(0,-5),...runEntries]}));
      assert.deepEqual(await compact(), ['k1','k2','k3','k8'], 'prepending recomputes the former first message');
      await page.evaluate(() => view.render({entries:[runEntry(0,-65),...runEntries]}));
      assert.deepEqual(await compact(), ['k2','k3','k8'], 'editing a predecessor recomputes unchanged followers');
    });

    await check('timestamps lead runs separated by an hour and regroup after prepend or edits', async () => {
      await page.evaluate(() => mount());
      await page.evaluate(() => {
        window.timed = (n, timestamp, speaker = 'agent') => entry(n, {timestamp, speaker});
        view.render({entries: [timed(1, '2026-10-04T10:00:00Z', 'user'), timed(2, '2026-10-04T10:59:59Z'),
          timed(3, '2026-10-04T11:59:59Z'), timed(4, null), timed(5, '2026-10-04T12:00:00Z')]});
      });
      const timedIds = () => page.locator('.gchat-msg:has(time)').evaluateAll(ns => ns.map(n => n.dataset.id));
      assert.deepEqual(await timedIds(), ['k1', 'k3', 'k5']);
      assert.equal(await page.locator('.gchat-msg:has(time)').evaluateAll(ns => ns.every(n => n.firstElementChild.tagName === 'TIME')), true);
      await page.evaluate(() => view.render({entries: [timed(0, '2026-10-04T09:45:00Z'), timed(1, '2026-10-04T10:00:00Z', 'user'), timed(2, '2026-10-04T10:59:59Z')], scroll: 'prepend'}));
      assert.deepEqual(await timedIds(), ['k0'], 'prepending history removes the former first timestamp without adding hourly labels to an uninterrupted run');
      await page.evaluate(() => view.render({entries: [timed(0, '2026-10-04T08:00:00Z'), timed(1, '2026-10-04T10:00:00Z', 'user'), timed(2, '2026-10-04T10:59:59Z')]}));
      assert.deepEqual(await timedIds(), ['k0', 'k1'], 'editing the preceding timestamp updates an unchanged following entry');
    });

    await check('copy fades in to the left and copies original text through Clipboard or the HTTP fallback', async () => {
      await page.evaluate(() => mount());
      await page.evaluate(() => {
        window.copied = [];
        Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: async text => copied.push(text)}});
        view.render({entries: [entry(1, {text: '**Original**\n\n[link](https://example.test)', author: {name: 'Marketing'}})]});
      });
      const copy = page.getByRole('button', {name: 'Copy message', exact: true});
      await page.mouse.move(900, 700);
      await page.waitForFunction(() => getComputedStyle(document.querySelector('.gchat-copy')).opacity === '0');
      await page.locator('.gchat-body').hover();
      await page.waitForFunction(() => getComputedStyle(document.querySelector('.gchat-copy')).opacity === '1');
      const iconBox = await copy.boundingBox(), bodyBox = await page.locator('.gchat-body').boundingBox();
      assert(iconBox.x >= 0 && iconBox.x + iconBox.width <= bodyBox.x, 'copy stays outside the bubble and inside the viewport');
      await copy.click();
      await page.getByRole('button', {name: 'Copied', exact: true}).waitFor();
      assert.deepEqual(await page.evaluate(() => copied), ['**Original**\n\n[link](https://example.test)']);
      await page.evaluate(() => {
        Object.defineProperty(navigator, 'clipboard', {configurable: true, value: undefined});
        window.originalExec = document.execCommand;
        document.execCommand = command => { if (command === 'copy') { copied.push(document.activeElement.value); return true; } return false; };
        view.render({entries: [entry(2, {speaker: 'user', text: 'HTTP message'})]});
      });
      const fallback = page.getByRole('button', {name: 'Copy message', exact: true});
      await fallback.focus();
      await page.keyboard.press('Enter');
      await page.getByRole('button', {name: 'Copied', exact: true}).waitFor();
      assert.equal(await page.evaluate(() => copied.at(-1)), 'HTTP message');
      assert.equal(await page.evaluate(() => document.activeElement.classList.contains('gchat-copy')), true, 'fallback restores focus');
      await page.evaluate(() => { document.execCommand = () => false; view.render({entries: [entry(3, {text: 'cannot copy'})]}); });
      await page.locator('.gchat-copy').focus(); await page.keyboard.press('Enter');
      await page.getByText('Couldn’t copy message.', {exact: true}).waitFor();
      assert.equal(await page.getByRole('button', {name: 'Copied', exact: true}).count(), 0, 'failed copying is not reported as successful');
      await page.evaluate(() => { document.execCommand = originalExec; });
    });

    await check('a keyed replay keeps nodes, chips and focus; a changed entry updates in place; order follows the supplied array', async () => {
      await page.evaluate(() => window.view.render({entries: [entry(1, {text: 'one'}), entry(2, {text: '[docs](https://example.test/d)'}), entry(3, {text: 'three'})]}));
      await page.getByRole('button', {name: 'Preview docs'}).focus();
      await page.evaluate(() => { window.mark = [...document.querySelectorAll('.gchat-log .gchat-msg')]; window.view.render({entries: [entry(1, {text: 'one'}), entry(2, {text: '[docs](https://example.test/d)'}), entry(3, {text: 'three changed'})]}); });
      assert.equal(await page.evaluate(() => [...document.querySelectorAll('.gchat-log .gchat-msg')].every((n, i) => n === window.mark[i])), true);
      assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), 'Preview docs');
      assert.equal(await page.locator('.gchat-msg[data-id="k3"] .gchat-text, .gchat-msg[data-id="k3"] p').first().innerText(), 'three changed');
      await page.evaluate(() => window.view.render({entries: [entry(3, {text: 'three changed'}), entry(1, {text: 'one'})]}));
      assert.deepEqual(await page.locator('.gchat-log .gchat-msg').evaluateAll(n => n.map(x => x.dataset.id)), ['k3', 'k1'], 'the view never sorts');
    });

    await check('pending receipts keep their native hooks and report actions only through callbacks', async () => {
      await page.evaluate(() => window.view.render({entries: [], pending: [
        {key: 'in-1', speaker: 'user', text: 'not typed', state: 'undelivered', stateLabel: 'Not delivered', inputType: 'chat', resendable: true,
          actions: [{key: 'resend', label: 'Resend'}, {key: 'dismiss', label: 'Dismiss'}]},
        {key: 'in-2', speaker: 'notice', text: '', state: 'waiting', stateLabel: 'Waiting', inputType: 'inbox_nudge', notice: {label: 'Inbox nudge'}, actions: []},
      ]}));
      const first = page.locator('.gchat-pending[data-input-id="in-1"]');
      assert.equal(await first.getAttribute('data-state'), 'undelivered');
      assert.equal(await first.getAttribute('data-input-type'), 'chat');
      assert.equal(await first.getAttribute('data-resendable'), 'true');
      assert.equal(await first.locator('.gchat-state').innerText(), 'Not delivered');
      assert.equal(await first.locator('[data-resend="in-1"]').count(), 1);
      assert.equal(await first.locator('[data-dismiss="in-1"]').count(), 1);
      assert.equal(await page.locator('.gchat-pending[data-input-id="in-2"] .gchat-marker').innerText(), 'Inbox nudge');
      await first.locator('[data-resend]').click();
      await first.locator('[data-dismiss]').click();
      assert.deepEqual(await events(), [['pending-action', 'resend', 'in-1'], ['pending-action', 'dismiss', 'in-1']]);
      await page.evaluate(() => window.view.render({pending: [{key: 'in-1', speaker: 'user', text: 'not typed', state: 'undelivered', stateLabel: 'Not delivered', inputType: 'chat', resendable: true,
        actions: [{key: 'resend', label: 'Resend', disabled: true}]}]}));
      assert.equal(await page.locator('[data-resend="in-1"]').isDisabled(), true);
      assert.equal(await page.locator('.gchat-pending').count(), 1, 'a patched array replaces the whole field');
      assert.equal(await page.locator('.gchat-log .gchat-msg').count(), 0, 'omitted entries keep their last value (none here)');
    });

    await check('composer: Enter sends, Shift+Enter is a newline, Ctrl/Meta+Enter always sends, IME never submits, disabled states are independent', async () => {
      await page.evaluate(() => mount());
      const box = page.getByRole('textbox', {name: 'Message'});
      await box.fill('first'); await box.press('Enter');
      await box.press('Shift+Enter'); await box.type('second');
      await box.press('Control+Enter'); await box.press('Meta+Shift+Enter');
      assert.equal(await box.inputValue(), 'first\n' + 'second'.replace('second', '') + 'second', 'Shift+Enter inserted the newline');
      assert.deepEqual((await events()).filter(e => e[0] === 'submit').map(e => e[1]), ['first', 'first\nsecond', 'first\nsecond']);
      await page.evaluate(() => { window.events.length = 0; });
      await box.evaluate(el => el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true, cancelable: true})));
      assert.deepEqual(await events(), [], 'a composition in progress never submits');
      await page.evaluate(() => window.view.render({composer: {sendDisabled: true}}));
      assert.equal(await box.isDisabled(), false, 'the text stays editable while only Send is disabled');
      await box.press('Enter');
      assert.deepEqual(await events(), [], 'a disabled Send blocks submission');
      await page.evaluate(() => window.view.render({composer: {sendDisabled: false, inputDisabled: true, sendLabel: 'Retry Send', placeholder: 'Not available', ariaLabel: 'Comment'}}));
      assert.equal(await box.count(), 0);
      const locked = page.getByRole('textbox', {name: 'Comment'});
      assert.equal(await locked.isDisabled(), true);
      assert.equal(await page.getByRole('button', {name: 'Retry Send'}).isDisabled(), false, 'the text field and Send are disabled independently');
      assert.equal(await locked.getAttribute('placeholder'), 'Not available');
      await page.evaluate(() => window.view.render({composer: {hidden: true}}));
      assert.equal(await page.locator('.gchat-composer').isVisible(), false, 'a read-only view hides the whole form');
      await page.evaluate(() => window.view.render({composer: {hidden: false, inputDisabled: false}}));
      assert.equal(await page.locator('.gchat-composer').isVisible(), true);
    });

    await check('setDraft is silent, keeps the caret when unchanged, and getDraft reads the field', async () => {
      await page.evaluate(() => mount());
      const box = page.getByRole('textbox', {name: 'Message'});
      await page.evaluate(() => window.view.setDraft('stored draft'));
      assert.equal(await page.evaluate(() => window.view.getDraft()), 'stored draft');
      assert.deepEqual(await page.evaluate(() => window.events), [], 'setDraft never reports a draft change');
      await box.evaluate(el => { el.focus(); el.setSelectionRange(3, 6); });
      await page.evaluate(() => window.view.setDraft('stored draft'));
      assert.deepEqual(await box.evaluate(el => [el.selectionStart, el.selectionEnd]), [3, 6], 'an unchanged draft keeps the selection');
      await box.type('X');
      assert.equal((await events()).at(-1)[0], 'draft');
      await page.evaluate(() => window.view.focusComposer());
      assert.equal(await box.evaluate(el => el === document.activeElement), true);
    });

    await check('the composer grows with input and restored drafts, caps and scrolls, then shrinks without a resize handle', async () => {
      await page.evaluate(() => mount());
      const box = page.locator('#host textarea');
      const height = () => box.evaluate(el => el.getBoundingClientRect().height);
      await box.fill('Hello');
      assert.equal(await height(), 36);
      assert.equal(await box.evaluate(el => getComputedStyle(el).resize), 'none');
      await box.fill('First line\nSecond line\nThird line');
      assert.ok(await height() > 36);
      assert.equal(await page.locator('#host form').evaluate(el => el.classList.contains('gchat-composer-expanded')), true);
      await page.evaluate(() => view.setDraft('Long restored line\n'.repeat(50)));
      assert.equal(await height(), 200);
      assert.equal(await box.evaluate(el => getComputedStyle(el).overflowY), 'auto');
      assert.ok(await box.evaluate(el => el.scrollHeight > el.clientHeight));
      await box.evaluate(el => { el.setSelectionRange(0, 0); el.scrollTop = 30; });
      await page.evaluate(() => view.render({composer: {sendDisabled: false}}));
      assert.equal(await box.evaluate(el => el.scrollTop), 30, 'a background repaint keeps draft scroll');
      await page.evaluate(() => view.setDraft(''));
      assert.equal(await height(), 36);
      assert.equal(await box.evaluate(el => getComputedStyle(el).overflowY), 'hidden');
      await box.fill('A line that fits in the wide composer but must wrap when the pane narrows.');
      await page.locator('#host').evaluate(el => el.style.width = '230px');
      await settle();
      assert.ok(await height() > 36, 'pane resizing remeasures wrapping');
      await page.locator('#host').evaluate(el => el.style.width = '600px');
      await page.evaluate(() => view.setDraft('Hello'));
      await settle();
      assert.equal(await height(), 36);
    });

    await check('an externally hosted composer is the same form and styles; the view owns and removes it', async () => {
      await page.evaluate(() => {
        const hostForm = document.createElement('div'); hostForm.id = 'slot'; document.body.append(hostForm);
        const footer = document.createElement('div'); footer.id = 'status'; footer.textContent = 'status line';
        mount({composerHost: hostForm, footer});
      });
      assert.equal(await page.locator('#slot > form.gchat-composer').count(), 1);
      assert.equal(await page.locator('.chat-view form.gchat-composer').count(), 0);
      assert.equal(await page.locator('#slot > #status + form.gchat-composer').count(), 1, 'the footer sits immediately before the composer');
      const style = await page.evaluate(() => { const c = getComputedStyle(document.querySelector('#slot textarea')), s = getComputedStyle(document.querySelector('#slot .gchat-send')); return [c.fontSize, s.height]; });
      assert.deepEqual(style, ['15.75px', '36px']);
      await page.locator('#slot textarea').fill('hello'); await page.locator('#slot textarea').press('Enter');
      assert.deepEqual((await events()).filter(e => e[0] === 'submit'), [['submit', 'hello']]);
      await page.evaluate(() => window.view.dispose());
      assert.equal(await page.locator('#slot form').count(), 0, 'dispose removes the external form');
      assert.equal(await page.locator('#status').count(), 1, 'dispose never removes the wrapper\'s own controls');
      await page.evaluate(() => { document.querySelector('#slot').remove(); document.querySelector('#status')?.remove(); });
    });

    await check('older, banner role, empty action and fixed-text states come only from the supplied model', async () => {
      await page.evaluate(() => mount({olderLabel: 'Older messages'}));
      assert.equal(await page.locator('.gchat-older').isHidden(), true);
      await page.evaluate(() => window.view.render({older: {visible: true}, banner: {text: "Couldn't load. Try again", role: 'alert'}, empty: {text: 'Nothing here.', action: {key: 'terminal', label: 'Open terminal'}}}));
      await page.getByRole('button', {name: 'Older messages'}).click();
      assert.equal(await page.getByRole('alert').innerText(), "Couldn't load. Try again");
      await page.getByRole('button', {name: 'Open terminal'}).click();
      assert.deepEqual(await events(), [['older'], ['empty-action', 'terminal']]);
      await page.evaluate(() => window.view.render({older: {disabled: true}, banner: {text: ''}, empty: {text: ''}}));
      assert.equal(await page.locator('.gchat-older').isDisabled(), true);
      assert.equal(await page.locator('.gchat-banner').isHidden(), true);
      assert.equal(await page.locator('.gchat-empty').isHidden(), true);
      assert.equal(await page.locator('.gchat-banner').getAttribute('role'), 'status', 'the banner returns to its default role');
    });

    await check('the first view opens at the latest message; an appended message follows the bottom, the same message while reading up shows the jump', async () => {
      await page.evaluate(() => { mount(); window.view.render({entries: many(1, 12)}); });
      await settle();
      const atBottom = () => page.evaluate(() => { const s = window.view.elements.scroll; return s.scrollHeight - s.scrollTop - s.clientHeight < 48; });
      assert.equal(await atBottom(), true, 'first open shows the latest');
      await page.evaluate(() => window.view.render({entries: many(1, 13)}));
      assert.equal(await atBottom(), true, 'a reply follows the bottom');
      assert.equal(await page.locator('.gchat-jump').isHidden(), true);
      await page.evaluate(() => { window.view.elements.scroll.scrollTop = 0; });
      await settle();
      assert.equal(await page.evaluate(() => window.view.isAtBottom()), false);
      await page.evaluate(() => window.view.render({entries: many(1, 14)}));
      assert.equal(await page.locator('.gchat-jump').isVisible(), true);
      assert.equal(await page.evaluate(() => window.view.elements.scroll.scrollTop), 0, 'reading up keeps its place');
      await page.evaluate(() => window.view.render({entries: [...many(1, 13), entry(14, {text: 'line 14 updated', partial: true})]}));
      assert.equal(await page.locator('.gchat-jump').innerText(), 'New messages ↓', 'an updated entry is not a new message');
      await page.evaluate(() => window.view.render({entries: [...many(1, 13), entry(14, {text: 'line 14 updated'}), entry(15)]}));
      assert.equal(await page.locator('.gchat-jump').innerText(), '2 new messages ↓');
      await page.locator('.gchat-jump').click();
      assert.equal(await atBottom(), true);
      assert.equal(await page.locator('.gchat-jump').isHidden(), true);
      assert.equal(await page.evaluate(() => document.activeElement === window.view.elements.input), true, 'the jump hands focus to the composer');
    });

    await check('prepending older entries keeps the visible anchor; scroll modes preserve or force the bottom', async () => {
      await page.evaluate(() => { mount(); window.view.render({entries: many(20, 30)}); });
      await settle();
      await page.evaluate(() => { window.view.elements.scroll.scrollTop = 200; });
      await settle();
      const probe = () => page.evaluate(() => { const s = window.view.elements.scroll, n = s.querySelector('[data-id="k24"]'); return n.getBoundingClientRect().top - s.getBoundingClientRect().top; });
      const before = await probe();
      await page.evaluate(() => window.view.render({entries: many(10, 30)}, {scroll: 'prepend'}));
      assert.ok(Math.abs((await probe()) - before) <= 1, 'the anchor entry stays where the reader left it');
      await page.evaluate(() => window.view.render({entries: many(10, 31)}, {scroll: 'preserve'}));
      assert.ok(Math.abs((await probe()) - before) <= 1, 'preserve never moves the reader');
      assert.equal(await page.locator('.gchat-jump').isHidden(), true, 'preserve does not announce a message');
      await page.evaluate(() => window.view.render({entries: many(10, 31)}, {scroll: 'bottom'}));
      assert.equal(await page.evaluate(() => window.view.isAtBottom()), true);
    });

    await check('hidden containers defer measurement; getScrollState/setScrollState/layout restore the reader after showing', async () => {
      await page.evaluate(() => { mount(); window.view.render({entries: many(1, 14)}); });
      await settle();
      await page.evaluate(() => { window.view.elements.scroll.scrollTop = 150; });
      await settle();
      const saved = await page.evaluate(() => { window.saved = window.view.getScrollState(); return window.saved; });
      assert.equal(saved.atBottom, false);
      assert.ok(saved.anchorKey && saved.top > 0);
      await page.evaluate(() => { document.querySelector('#host').hidden = true; document.querySelector('#host').style.display = 'none'; });
      assert.deepEqual(await page.evaluate(() => window.view.getVisibleKeys()), [], 'a hidden feed reports no visible keys');
      const whileHidden = await page.evaluate(() => { window.view.render({entries: many(1, 15)}); return window.view.getScrollState(); });
      assert.deepEqual(whileHidden, saved, 'hidden calls retain the last real state');
      await page.evaluate(() => { document.querySelector('#host').style.display = 'flex'; document.querySelector('#host').hidden = false; window.view.setScrollState(window.saved); window.view.layout(); });
      await settle();
      const after = await page.evaluate(() => window.view.getScrollState());
      assert.equal(after.atBottom, false, 'no initial auto-bottom after a saved position');
      assert.ok(Math.abs(after.top - saved.top) <= 4 && after.anchorKey === saved.anchorKey, 'the saved reading position returns');
      assert.ok((await page.evaluate(() => window.view.getVisibleKeys())).length > 0);
    });

    await check('a view first rendered while hidden opens at the latest message once it is shown', async () => {
      await page.evaluate(() => { const h = document.querySelector('#host'); h.style.display = 'none'; mount(); window.view.render({entries: many(1, 14)}, {scroll: 'bottom'}); h.style.display = 'flex'; window.view.layout(); });
      await settle();
      assert.equal(await page.evaluate(() => window.view.isAtBottom()), true);
      const edge = await page.evaluate(() => window.view.getVisibleKeys().at(-1));
      assert.equal(edge, 'k14');
    });

    await check('viewport reports are callbacks only: scrolling reports, getVisibleKeys reflects the viewport', async () => {
      await page.evaluate(() => { mount(); window.view.render({entries: many(1, 14)}); });
      await settle();
      await page.evaluate(() => { window.events.length = 0; window.view.elements.scroll.scrollTop = 0; });
      await settle();
      assert.ok((await page.evaluate(() => window.events.filter(e => e[0] === 'viewport').length)) >= 1);
      const keys = await page.evaluate(() => window.view.getVisibleKeys());
      assert.equal(keys[0], 'k1');
      assert.equal(keys.includes('k14'), false);
    });

    await check('previews open on demand, restore focus on close or Escape, and release their frame on dispose', async () => {
      await page.evaluate(() => { mount(); window.view.render({entries: [entry(1, {text: '[Report](/synthetic/businesses/a/report.html) and [Pic](/synthetic/businesses/a/p.png)'})]}); });
      const preview = page.getByRole('dialog', {name: 'Document preview'});
      assert.equal(await preview.isHidden(), true);
      assert.equal(await page.locator('iframe').count(), 0, 'nothing loads before a chip is opened');
      const chip = page.getByRole('button', {name: 'Preview Report', exact: true});
      await chip.click();
      assert.equal(await preview.isVisible(), true);
      assert.equal(await page.locator('.gchat-preview-content iframe').count(), 1);
      assert.equal(await preview.getByRole('link', {name: 'Open preview in new tab'}).getAttribute('href'), 'http://fixture.test/files/view?path=%2Fsynthetic%2Fbusinesses%2Fa%2Freport.html');
      await preview.getByRole('button', {name: 'Close preview'}).press('Escape');
      assert.equal(await preview.isHidden(), true);
      assert.equal(await page.locator('iframe').count(), 0, 'closing releases the page');
      assert.equal(await chip.evaluate(el => el === document.activeElement), true, 'focus returns to the chip');
      await page.getByRole('button', {name: 'Preview Pic', exact: true}).click();
      assert.equal(await page.locator('.gchat-preview-content img').count(), 1);
      await page.evaluate(() => window.view.dispose());
      assert.equal(await page.locator('.chat-view').count(), 0);
    });

    await check('dispose removes every listener and DOM node; later calls and events do nothing and report nothing', async () => {
      await page.evaluate(() => { mount(); window.view.render({entries: [entry(1)]}); window.kept = {...window.view.elements}; window.view.dispose(); window.events.length = 0; });
      await page.evaluate(() => {
        window.kept.input.value = 'late'; window.kept.input.dispatchEvent(new Event('input', {bubbles: true}));
        window.kept.input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
        window.kept.scroll.dispatchEvent(new Event('scroll')); window.kept.older.click(); window.kept.jump.click();
        window.view.render({entries: [entry(2)]}); window.view.setDraft('x'); window.view.layout(); window.view.dispose();
      });
      assert.deepEqual(await page.evaluate(() => window.events), []);
      assert.equal(await page.locator('#host').evaluate(el => el.childElementCount), 0);
      assert.deepEqual(await page.evaluate(() => window.view.getVisibleKeys()), []);
    });

    await check('an enabled Retry Send dispatches with a locked input; hidden or disabled Send never does', async () => {
      await page.evaluate(() => { mount(); view.setDraft('locked draft'); view.render({composer: {inputDisabled: true, sendDisabled: false, sendLabel: 'Retry Send'}}); });
      assert.equal(await page.locator('#host textarea').isDisabled(), true);
      await page.locator('#host .gchat-send').click();
      assert.deepEqual((await events()).filter(e => e[0] === 'submit'), [['submit', 'locked draft']], 'exactly one retry call');
      await page.evaluate(() => view.render({composer: {inputDisabled: true, sendDisabled: true}}));
      await page.locator('#host .gchat-send').click({force: true});
      await page.evaluate(() => view.render({composer: {hidden: true, inputDisabled: true, sendDisabled: false}}));
      await page.locator('#host .gchat-composer').evaluate(form => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
      assert.deepEqual((await events()).filter(e => e[0] === 'submit'), [], 'a disabled or hidden composer never dispatches');
    });

    await check('a disabled reference does nothing and a structured reference arrives intact', async () => {
      await page.evaluate(() => { mount(); view.render({entries: [window.entry(1, {references: [{key: 'r1', value: {type: 'task', id: 't1', business_id: null, label: 'Real'}, label: 'Real'}, {key: 'r2', value: {type: 'task', id: 't9', business_id: null}, label: 'Gone', disabled: true}]})]}); });
      await page.locator('.gchat-reference-unavailable').click({force: true});
      assert.deepEqual((await events()).filter(e => e[0] === 'reference'), []);
      await page.waitForFunction(() => document.querySelector('.gchat-reference-title')?.textContent === 'Resolved contact');
      await page.locator('.gchat-reference-block').click();
      assert.deepEqual((await events()).filter(e => e[0] === 'reference'), [['reference', {type: 'task', id: 't1', business_id: null, label: 'Real'}, 'k1']]);
    });

    await check('bannerHost moves the one shared banner beside an external composer and removes it on dispose', async () => {
      await page.evaluate(() => {
        document.body.insertAdjacentHTML('beforeend', '<div id="ext"></div>');
        mount({composerHost: document.querySelector('#ext'), bannerHost: document.querySelector('#ext')});
        view.render({banner: {text: 'Delivery failed', role: 'alert'}});
      });
      assert.equal(await page.locator('#ext [role="alert"]').innerText(), 'Delivery failed');
      assert.equal(await page.locator('#host [role="alert"]').count(), 0, 'one banner only');
      assert.deepEqual(await page.locator('#ext > *').evaluateAll(n => n.map(x => x.classList[0])), ['gchat-composer', 'gchat-banner'], 'banner follows the form');
      await page.evaluate(() => view.dispose());
      assert.equal(await page.locator('#ext > *').count(), 0);
      await page.evaluate(() => document.querySelector('#ext').remove());
    });

    await check('typed Task uses current scoped reference, keyboard activation, availability and informational fallback', async () => {
      await page.evaluate(() => {
        mount(); window.task = {type:'task', id:'t1', business_id:'biz-a', business_slug:'alpha', label:'First task', state:'open', available:true};
        window.paintTask = () => view.render({entries:[entry(1, {typedReferences:[window.task]})]}); paintTask();
      });
      const task = page.locator('.gchat-task-reference');
      await task.focus(); await page.keyboard.press('Enter');
      const initialTask = (await events()).find(e => e[0] === 'reference'); assert.equal(initialTask[1].label, 'First task'); assert.equal(initialTask[2], 'k1');
      await page.evaluate(() => { task = {...task, label:'Updated task', state:'done'}; paintTask(); });
      await page.locator('button.gchat-task-reference').click();
      assert.equal((await events()).find(e => e[0] === 'reference')[1].label, 'Updated task');
      await page.evaluate(() => { task = {...task, business_id:'biz-b', business_slug:'beta'}; paintTask(); });
      await page.locator('button.gchat-task-reference').click();
      assert.equal((await events()).find(e => e[0] === 'reference')[1].business_id, 'biz-b');
      await page.evaluate(() => { task = {...task, available:false}; paintTask(); });
      assert.equal(await page.locator('button.gchat-task-reference').count(), 0);
      await page.evaluate(() => { mount({onTask:null}); task = {...task, available:true}; paintTask(); });
      assert.equal(await page.locator('button.gchat-task-reference').count(), 0);
    });
    await check('late enrichment cannot repaint a typed reference after its business scope becomes inconsistent', async () => {
      await page.evaluate(() => {
        mount(); task = {type:'task',id:'held-scope',business_id:'biz-a',business_slug:'alpha',available:true}; paintTask();
      });
      await page.waitForFunction(() => readWaiters.has('held-scope'));
      await page.evaluate(() => { task = {...task,business_slug:'beta'}; paintTask(); });
      await page.waitForFunction(() => document.querySelector('.gchat-reference-unavailable')?.innerText === 'Unavailable reference');
      assert.equal(await page.locator('.gchat-reference-unavailable').innerText(), 'Unavailable reference');
      await page.evaluate(() => readWaiters.get('held-scope')());
      await page.waitForTimeout(50);
      assert.equal(await page.locator('.gchat-reference-unavailable').innerText(), 'Unavailable reference');
    });
    await check('private presentation retains an accessible author and shared composer orders its actions', async () => {
      await page.evaluate(() => { mount({onFiles: files => events.push(['files', files.length])}); view.render({entries:[entry(1, {author:{name:'Private agent', avatarHtml:'<svg></svg>'}, showAuthorName:false})]}); });
      assert.equal(await page.locator('.gchat-author-name').textContent(), 'Private agent');
      assert.equal(await page.locator('.gchat-avatar svg').count(), 1);
      assert.deepEqual(await page.locator('.gchat-composer').evaluate(el => [...el.children].filter(x => /TEXTAREA|BUTTON/.test(x.tagName)).map(x => x.className || x.tagName)), ['TEXTAREA','gchat-attach quiet','gchat-send gchat-icon-send']);
      await page.locator('input[type=file]').setInputFiles({name:'sample.txt', mimeType:'text/plain', buffer:Buffer.from('hello')});
      assert.deepEqual((await events()).filter(e => e[0] === 'files'), [['files',1]]);
      await page.locator('textarea').fill('first line\nsecond line');
      await page.waitForTimeout(100);
      const boxes = await page.locator('.gchat-composer').evaluate(el => { const a=el.querySelector('.gchat-attach').getBoundingClientRect(), b=el.querySelector('.gchat-send').getBoundingClientRect(), t=el.querySelector('textarea').getBoundingClientRect(); return {a:a.x,b:b.x,ay:a.y,by:b.y,t:t.bottom}; });
      assert(boxes.a < boxes.b && boxes.ay === boxes.by && boxes.ay >= boxes.t);
      await page.evaluate(() => view.render({composer:{attachments:[{key:'file-1', name:'sample.txt'}]}}));
      assert.equal(await page.locator('.gchat-file-choice').count(), 1);
      await page.getByRole('button', {name:'Remove sample.txt'}).click();
      await page.locator('.gchat-send').click();
      assert.equal((await events()).some(e => e[0] === 'submit'), true);
      await page.evaluate(() => { mount(); document.querySelector('#host').style.width='300px'; view.setDraft('first line\nsecond line'); });
      await page.waitForTimeout(100);
      const noFiles = await page.locator('.gchat-composer').evaluate(el => { const send=el.querySelector('.gchat-send').getBoundingClientRect(), box=el.getBoundingClientRect(); return {right:box.right-send.right, count:el.querySelectorAll('.gchat-attach').length}; });
      assert.equal(noFiles.count, 0); assert(noFiles.right < 20);
      await page.evaluate(() => { document.querySelector('#host').style.width='600px'; });
    });

    await check('no request is ever made by the view', async () => {
      assert.deepEqual(await page.evaluate(() => window.requests), []);
    });
    assert.deepEqual(errors, [], 'no page errors');
    console.log(`Shared Chat view (synthetic models): ${checks} checks passed.`);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
