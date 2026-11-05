#!/usr/bin/env node
// Real Chromium with the real shared Chat view and record-thread controller over synthetic records: consecutive
// related Task/Action entries form one flat attachment list with no header, counts or collapse control, a message
// between runs stays in place, a refresh that adds or removes a reference keeps the same rows in place, rows open the
// exact canonical value, unavailable rows stay inert, a message's own Task/Action references group inside that
// message, an inline Signoff keeps its mount lifecycle, and later related pages load from the feed viewport with no
// paging button or failure retry UI.
// No live dashboard, API, provider or native session is touched.
//   PLAYWRIGHT_MODULE=/path/to/playwright CHROMIUM_EXECUTABLE=/path/to/chrome node tests/test_thread_reference_groups_browser.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const SHOTS = process.env.THREAD_REFERENCE_GROUP_SHOTS || '';
const LONG = 'Confirm the final launch checklist with the operations team and every external contractor before the Friday publication window closes';

(async () => {
  if (SHOTS) fs.mkdirSync(SHOTS, {recursive: true});
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1200, height: 420}, locale: 'en-US'});
    const errors = [];
    page.on('pageerror', error => errors.push(error.stack || error.message));
    page.setDefaultTimeout(10000);
    await page.setContent(`<div class="rw" style="height:100vh;padding:16px;box-sizing:border-box"><div class="rw-records">
      <article class="rw-record"><header class="rw-head"><span class="rw-title">Launch the new site</span></header>
      <div class="rw-body"><div class="rw-pane rw-chat"><div class="agfocus-conversation rw-conversation" id="host"></div></div></div></article>
      </div></div>`);
    for (const file of ['app.css', 'css/chat-view.css', 'css/conversation.css', 'css/record-workspace.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.addScriptTag({path: path.join(root, 'js/safe-links.js')});
    await page.addScriptTag({path: path.join(root, 'js/chat-view.js')});
    await page.addScriptTag({path: path.join(root, 'js/conversation.js')});
    await page.addScriptTag({content: `
      const esc = safeLinkEsc;
      const state = {session: {is_owner: true, principal_id: 'owner'}, bizs: [{id: 'biz-1', slug: 'widget'}]};
      const coreData = response => response.data;
      const newRequestId = () => String(Math.random());
      const toast = () => {};
      const uiFailure = error => error?.message || 'Failed';
      window.opened = []; window.mounted = []; window.disposed = []; window.marks = [];
      window.relatedCalls = 0; window.relatedFail = false;
      window.sgChatMount = (ref, host) => {
        const el = document.createElement('div'); el.className = 'sg-card'; el.dataset.signoff = ref.id;
        el.textContent = 'Signoff · ' + (ref.label || ''); host.append(el); mounted.push(ref.id);
        return {refresh() {}, dispose() { disposed.push(ref.id); el.remove(); }};
      };
      window.messages = [
        {id: 'm1', sequence: 1, body: 'I grouped these follow-ups under the launch work.',
          created_by: 'chief', created_by_name: 'Ordubashy', author_kind: 'agent', created_at: '2026-10-27T10:00:00Z',
          refs: [
            {type: 'task', id: 'msg-task-1', business_id: null, label: 'Reply to the investor', state: 'waiting'},
            {type: 'task', id: 'msg-task-2', business_id: null, label: 'Text Tony'},
            {type: 'action', id: 'msg-action-1', business_id: null, label: 'Log the call'},
            {type: 'material', id: 'material-1', business_id: null, label: 'Call notes'}
          ]},
        {id: 'm2', sequence: 2, body: 'The standalone items below stay separate.',
          created_by: 'chief', created_by_name: 'Ordubashy', author_kind: 'agent', created_at: '2026-10-27T10:04:00Z'}
      ];
      window.related = [
        {type: 'task', id: 'task-1', business_id: null, label: 'Reply to Vusal, Ergen investor', state: 'open', created_at: '2026-10-27T10:01:00Z'},
        {type: 'task', id: 'task-2', business_id: null, label: 'Text Tony Chen', state: 'working', created_at: '2026-10-27T10:02:00Z'},
        {type: 'action', id: 'action-unavailable', business_id: null, label: 'Archived follow-up', unavailable: true, created_at: '2026-10-27T10:02:30Z'},
        {type: 'action', id: 'action-1', business_id: null, label: 'Consolidate networking tasks', state: 'withdrawn', created_at: '2026-10-27T10:03:00Z'},
        {type: 'task', id: 'task-3', business_id: 'biz-1', label: 'Talk with Romeo Ju about building the AI CMO', state: 'working', created_at: '2026-10-27T10:05:00Z'},
        {type: 'action', id: 'action-2', business_id: null, label: 'Draft the follow-up email', created_at: '2026-10-27T10:06:00Z'},
        {type: 'signoff', id: 'sg-1', business_id: null, label: 'Approve the launch message', state: 'pending', created_at: '2026-10-27T10:07:00Z'}
      ];
      window.longRecord = {type: 'task', id: 'task-long', business_id: null, label: ${JSON.stringify(LONG)}, state: 'waiting', created_at: '2026-10-27T10:02:45Z'};
      window.sourceMessages = id => id === 'story-b' ? [] : messages;
      const pageSize = 2;
      const post = async (path, args = {}) => {
        if (path.endsWith('owner_conversation_get')) {
          const list = sourceMessages(args.source?.id);
          return {data: {conversation: {latest_sequence: list.length, message_count: list.length}, messages: list,
            participants: [], next_before: null, read_state: {unread: false}}};
        }
        if (path.endsWith('owner_conversation_related')) {
          relatedCalls++;
          if (relatedFail) throw Object.assign(new Error('Unavailable'), {code: 'forbidden'});
          const offset = args.offset || 0;
          return {data: {items: related.slice(offset, offset + pageSize),
            next_offset: offset + pageSize < related.length ? offset + pageSize : null}};
        }
        if (path.endsWith('owner_conversation_mark_read')) {
          marks.push(args);
          return {data: {conversation: {}, read_state: {last_read_sequence: 1, unread: false}}};
        }
        throw Error('Unexpected ' + path);
      };
      window.mountThread = (options = {}) => {
        const host = options.host || document.querySelector('#host');
        window.chat?.dispose?.();
        host.replaceChildren();
        window.chat = conversationMount(host, {type: 'task', id: options.id || 'story-a', biz: 'widget'},
          {inlineComposer: true, related: true, dismissOnOutside: false, privateGlobal: true,
            privateSource: {type: 'task', id: options.id || 'story-a'}, sourceTitle: options.title,
            openReference: ref => { opened.push(ref); return true; }});
        chat.expand();
        host.querySelector('.conversation-toggle')?.remove();
        return chat;
      };
    `});
    const group = index => page.locator('#host .gchat-ref-group-related').nth(index);
    const rowCount = index => group(index).locator('.gchat-ref-row').count();
    const rowIds = index => group(index).locator('.gchat-ref-row').evaluateAll(nodes => nodes.map(node => node.closest('.gchat-msg').dataset.id));
    // The owner reaches the feed end by scrolling: moving away from the end and back arms exactly one next page.
    const scrollEnd = async direction => page.evaluate(where => {
      const feed = document.querySelector('#host .gchat-scroll');
      feed.scrollTop = where === 'top' ? 0 : feed.scrollHeight;
      feed.dispatchEvent(new Event('scroll'));
    }, direction);
    const loadNextRelatedPage = async () => {
      const before = await page.evaluate(() => window.chat.state.relatedOffset);
      await scrollEnd('top');
      await page.waitForTimeout(30);
      await scrollEnd('bottom');
      await page.waitForFunction(old => window.chat.state.relatedOffset !== old, before);
    };
    const loadAllRelated = async () => {
      for (let i = 0; i < 10; i++) {
        if (await page.evaluate(() => window.chat.state.relatedOffset == null)) return;
        await loadNextRelatedPage();
      }
    };

    // ---- grouping and chronology
    await page.evaluate(() => { window.chat = mountThread({title: 'Launch the new site'}); });
    await page.locator('#host .gchat-ref-group-related').first().waitFor();

    // A CRM Table and customer record use the same left rule and inset as Task attachments. Secondary text wraps
    // below its title, and neither reference draws a surrounding card at a 360px layout width.
    const crmReferences = await page.evaluate(() => {
      const view = document.createElement('div'); view.className = 'chat-view'; view.style.width = '360px';
      const attachment = document.createElement('div'); attachment.className = 'gchat-attachment';
      const table = document.createElement('button');
      chatViewPaintReference(table, {type: 'table', id: 'crm', label: 'CRM'}, {data: {title: 'Atlas contacts', table_kind: 'crm', row_count: 1,
        description: 'A long synthetic table description that wraps beneath the title without truncation or horizontal overflow.'.repeat(2)}}, () => {});
      const person = document.createElement('button');
      chatViewPaintReference(person, {type: 'task', id: 'person', label: 'Customer', task_type: 'customer'}, {data: {title: 'Alex Morgan', task_type: 'customer',
        customer_profile: {email: 'alex@example.test', company: 'Atlas Research'}}}, () => {});
      attachment.append(table, person); view.append(attachment); document.body.append(view);
      const shape = el => {
        const s = getComputedStyle(el), content = el.querySelector('.gchat-reference-content');
        const icon = el.querySelector('.gchat-reference-icon'), title = el.querySelector('.gchat-reference-title');
        return {left: s.borderLeftWidth, other: [s.borderTopWidth, s.borderRightWidth, s.borderBottomWidth], padding: s.padding,
          background: s.backgroundColor, radius: s.borderRadius, overflow: el.scrollWidth - el.clientWidth, verticalOverflow: el.scrollHeight - el.clientHeight,
          title: el.querySelector('.gchat-reference-title').textContent, detail: el.querySelector('.gchat-reference-detail')?.textContent || '',
          count: el.querySelector('.gchat-reference-count')?.textContent || '', icon: !!el.querySelector('.gchat-reference-icon'),
          iconX: icon.getBoundingClientRect().left, titleX: title.getBoundingClientRect().left,
          detailIndent: el.querySelector('.gchat-reference-detail')?.getBoundingClientRect().left - title.getBoundingClientRect().left};
      };
      const containerStyle = getComputedStyle(attachment);
      return {table: shape(table), person: shape(person), width: view.clientWidth, overflow: view.scrollWidth - view.clientWidth,
        container: {left: containerStyle.borderLeftWidth, other: [containerStyle.borderTopWidth, containerStyle.borderRightWidth, containerStyle.borderBottomWidth],
          padding: containerStyle.paddingLeft, background: containerStyle.backgroundColor}};
    });
    assert.deepEqual([crmReferences.container.left, ...crmReferences.container.other, crmReferences.container.padding, crmReferences.container.background],
      ['2px', '0px', '0px', '0px', '9px', 'rgba(0, 0, 0, 0)']);
    assert.deepEqual([crmReferences.table.left, ...crmReferences.table.other, crmReferences.table.padding, crmReferences.table.background, crmReferences.table.radius],
      ['0px', '0px', '0px', '0px', '6px 8px', 'rgba(0, 0, 0, 0)', '0px']);
    assert.deepEqual([crmReferences.person.left, ...crmReferences.person.other, crmReferences.person.padding, crmReferences.person.background, crmReferences.person.radius],
      ['0px', '0px', '0px', '0px', '6px 8px', 'rgba(0, 0, 0, 0)', '0px']);
    assert.equal(crmReferences.table.count, '1 record');
    assert.match(crmReferences.table.detail, /wraps beneath the title/);
    assert.equal(crmReferences.person.title, 'Alex Morgan');
    assert.equal(crmReferences.person.detail, 'alex@example.test · Atlas Research');
    assert.equal(crmReferences.table.icon && crmReferences.person.icon, true);
    assert.deepEqual([crmReferences.table.iconX, crmReferences.person.iconX, crmReferences.table.titleX, crmReferences.person.titleX],
      [crmReferences.table.iconX, crmReferences.table.iconX, crmReferences.table.titleX, crmReferences.table.titleX], 'reference icon and title columns align');
    assert.equal(crmReferences.table.detailIndent, 0, 'table description aligns beneath its title content');
    assert.ok(crmReferences.table.overflow <= 1 && crmReferences.person.overflow <= 1 && crmReferences.overflow <= 1, JSON.stringify(crmReferences));
    assert.ok(crmReferences.table.verticalOverflow <= 1 && crmReferences.person.verticalOverflow <= 1, 'long descriptions are not clipped');
    await page.locator('body > .chat-view').evaluate(el => el.remove());

    await page.waitForFunction(() => document.querySelectorAll('#host .gchat-ref-group-related').length === 1);
    assert.equal(await rowCount(0), 2, 'the first related page loads its two rows');
    assert.equal(await group(0).getAttribute('aria-label'), 'Related work in Launch the new site', 'the accessible group name still says whose thread this is');
    assert.deepEqual(await group(0).evaluate(el => [...el.children].map(child => child.className)), ['gchat-ref-rows'], 'the flat list holds only its rows');
    assert.equal(await page.locator('#host .gchat-ref-group-head, #host .gchat-ref-group-title, #host .gchat-ref-group-counts, #host .gchat-ref-group-toggle').count(), 0, 'no Story heading, counts or collapse control is drawn anywhere');
    assert.equal(await page.locator('#host .conversation-related-more').count(), 0, 'no explicit related paging control is drawn');
    assert.equal(await page.evaluate(() => {
      const feed = document.querySelector('#host .gchat-scroll');
      return feed.scrollHeight > feed.clientHeight;
    }), true, 'the feed can reach its end by scrolling');
    await page.waitForTimeout(150);
    assert.equal(await rowCount(0), 2, 'opening the thread does not page eagerly');
    assert.equal(await page.evaluate(() => window.chat.state.relatedPages), 1);

    await loadNextRelatedPage();
    assert.equal(await rowCount(0), 4);

    // A failed later page shows no retry UI and no stale rows, never blocks the composer, and is not retried
    // by continued scrolling until a normal refresh.
    await page.evaluate(() => { window.relatedFail = true; });
    await loadNextRelatedPage();
    await page.waitForFunction(() => document.querySelectorAll('#host .gchat-ref-group-related').length === 0);
    assert.equal(await page.locator('#host .conversation-related-more').count(), 0, 'a failed page draws no retry control');
    assert.equal(await page.locator('#host [role="alert"]').count(), 0, 'a failed related read shows no banner');
    assert.equal(await page.locator('#host textarea').isDisabled(), false, 'a failed related read never blocks the composer');
    assert.equal(await page.evaluate(() => window.chat.state.related.length), 0, 'a failed read retains no related records');
    const callsAfterFailure = await page.evaluate(() => window.relatedCalls);
    await scrollEnd('top'); await page.waitForTimeout(30); await scrollEnd('bottom');
    await page.waitForTimeout(150);
    assert.equal(await page.evaluate(() => window.relatedCalls), callsAfterFailure, 'continued scrolling does not retry a failed page');
    await page.evaluate(() => { window.relatedFail = false; });
    await page.evaluate(() => chat.refresh());
    await page.locator('#host .gchat-ref-group-related').first().waitFor();

    await loadAllRelated();
    await page.waitForFunction(() => document.querySelectorAll('#host .gchat-ref-group-related').length === 2);
    assert.equal(await rowCount(0), 4);
    assert.equal(await rowCount(1), 2);
    // Canonical chronological order: entries never move across the intervening message.
    const layout = await page.evaluate(() => [...document.querySelector('#host .gchat-log').children].flatMap(node => {
      if (node.classList.contains('gchat-ref-group')) return [...node.querySelectorAll('.gchat-ref-rows > .gchat-msg')].map(row => row.dataset.id);
      return [node.dataset.id];
    }));
    assert.deepEqual(layout, ['m1', 'related:task:global:task-1', 'related:task:global:task-2', 'related:action:global:action-unavailable',
      'related:action:global:action-1', 'm2', 'related:task:biz-1:task-3', 'related:action:global:action-2', 'related:signoff:global:sg-1']);
    assert.deepEqual(await rowIds(0), ['related:task:global:task-1', 'related:task:global:task-2',
      'related:action:global:action-unavailable', 'related:action:global:action-1']);
    const messageIndex = await page.evaluate(() => [...document.querySelector('#host .gchat-log').children]
      .findIndex(node => node.dataset.id === 'm2'));
    const groupIndexes = await page.evaluate(() => [...document.querySelector('#host .gchat-log').children]
      .flatMap((node, index) => node.classList.contains('gchat-ref-group') ? [index] : []));
    assert.equal(groupIndexes.length, 2);
    assert.ok(groupIndexes[0] < messageIndex && messageIndex < groupIndexes[1], 'a message between runs stays in place');
    assert.equal(await page.locator('#host .gchat-msg[data-id="m2"] .gchat-body').innerText(), 'The standalone items below stay separate.');
    assert.equal(await page.locator('#host .gchat-msg[data-id="m1"] .gchat-time').count(), 1, 'message time is unchanged');
    assert.equal(await page.locator('#host .gchat-msg[data-id="m1"] .gchat-time').getAttribute('datetime'), '2026-10-27T10:00:00.000Z');
    assert.equal(await page.locator('#host .gchat-msg[data-id="m1"] .gchat-author').count(), 1, 'message author is unchanged');
    // Status is informational: the ball has no checkbox or state action.
    assert.equal(await group(0).locator('input,[role="checkbox"],[aria-pressed]').count(), 0);
    assert.equal(await page.locator('#host .gchat-msg[data-id="related:task:global:task-2"] .gchat-task-state.working').count(), 1);
    assert.equal(await page.locator('#host .gchat-msg[data-id="related:task:global:task-2"] .gchat-reference-state-hidden').innerText(), 'Working');

    // ---- a refresh that adds and removes a record keeps the same flat list with its rows in place
    await page.evaluate(() => { window.keptRow = document.querySelector('#host .gchat-msg[data-id="related:task:global:task-1"] button.gchat-ref-row'); });
    await page.evaluate(() => { related.splice(2, 0, longRecord); return chat.refresh(); });
    await page.waitForFunction(() => document.querySelectorAll('#host .gchat-msg[data-id="related:task:global:task-long"]').length === 1);
    assert.equal(await page.locator('#host .gchat-msg[data-id="related:task:global:task-1"] button.gchat-ref-row').evaluate(el => el === window.keptRow), true, 'refresh keeps the existing row node');
    assert.equal(await rowCount(0), 5, 'the added record joins the same flat list');
    assert.equal(await page.locator('#host .gchat-ref-group-related').count(), 2, 'refresh does not duplicate groups');
    assert.equal(await group(0).locator('.gchat-ref-rows').isVisible(), true, 'the flat list is never collapsed');
    await page.evaluate(() => { related.splice(related.findIndex(item => item.id === 'task-long'), 1); return chat.refresh(); });
    await page.waitForFunction(() => document.querySelectorAll('#host .gchat-msg[data-id="related:task:global:task-long"]').length === 0);
    assert.equal(await rowCount(0), 4, 'removing a record removes exactly its row');
    assert.equal(await page.locator('#host .gchat-ref-group-related').count(), 2, 'removing a record keeps both runs');
    assert.equal(await page.locator('#host .gchat-ref-row').count(), 9, 'no duplicated rows after the refresh');

    // ---- rows keep the existing opener and canonical scope; unavailable rows are inert
    await page.locator('#host .gchat-msg[data-id="related:task:global:task-1"] button.gchat-ref-row').click();
    const firstOpened = await page.evaluate(() => opened[0]);
    assert.equal(firstOpened.type, 'task');
    assert.equal(firstOpened.id, 'task-1');
    assert.equal(firstOpened.label, 'Reply to Vusal, Ergen investor');
    assert.equal(firstOpened.state, 'open');
    assert.equal(firstOpened.available, true);
    await page.locator('#host .gchat-msg[data-id="related:action:global:action-1"] button.gchat-ref-row').click();
    assert.deepEqual((await page.evaluate(() => opened[1])), {type: 'action', id: 'action-1', business_id: null, label: 'Consolidate networking tasks',
      state: 'withdrawn', created_at: '2026-10-27T10:03:00Z'});
    await page.locator('#host .gchat-msg[data-id="related:task:biz-1:task-3"] button.gchat-ref-row').click();
    const scoped = await page.evaluate(() => opened[2]);
    assert.equal(scoped.id, 'task-3');
    assert.equal(scoped.business_id, 'biz-1');
    assert.equal(scoped.business_slug, 'widget', 'the canonical business scope reaches the existing opener');
    const beforeDisabled = await page.evaluate(() => opened.length);
    const unavailable = page.locator('#host .gchat-msg[data-id="related:action:global:action-unavailable"] button.gchat-ref-row');
    assert.equal(await unavailable.isDisabled(), true, 'an unavailable reference cannot navigate');
    await unavailable.click({force: true});
    assert.equal(await page.evaluate(() => opened.length), beforeDisabled);
    // Action state is honest: a known state is shown, an unknown one is omitted, nothing defaults to Done.
    assert.equal(await page.locator('#host .gchat-msg[data-id="related:action:global:action-1"] .gchat-ref-row-meta').innerText(), 'Action · Withdrawn');
    assert.equal(await page.locator('#host .gchat-msg[data-id="related:action:global:action-2"] .gchat-ref-row-meta').innerText(), 'Action');
    assert.equal((await group(0).innerText()).includes('Done'), false);

    // Flat attachment geometry: a 2px left rule with a small inset, no enclosing card, and a row box that only
    // gains its inset outline and wash on hover (so nothing moves).
    const geometry = await page.evaluate(() => {
      const group = document.querySelector('#host .gchat-ref-group-related');
      const row = group.querySelector('.gchat-ref-row-task'), groupStyle = getComputedStyle(group), rowStyle = getComputedStyle(row);
      return {leftRule: groupStyle.borderLeftWidth, topBorder: groupStyle.borderTopWidth, inset: groupStyle.paddingLeft,
        background: groupStyle.backgroundColor, rowRadius: rowStyle.borderTopLeftRadius, rowPadding: rowStyle.paddingTop,
        rowBorder: rowStyle.borderTopWidth, rowShadow: rowStyle.boxShadow};
    });
    assert.deepEqual(geometry, {leftRule: '2px', topBorder: '0px', inset: '9px', background: 'rgba(0, 0, 0, 0)',
      rowRadius: '6px', rowPadding: '6px', rowBorder: '0px', rowShadow: 'none'});
    const flatRow = page.locator('#host .gchat-msg[data-id="related:task:global:task-1"] button.gchat-ref-row');
    await flatRow.scrollIntoViewIfNeeded();
    const flatRowBox = await flatRow.boundingBox();
    await flatRow.hover();
    await page.waitForFunction(() => {
      const row = document.querySelector('#host .gchat-msg[data-id="related:task:global:task-1"] button.gchat-ref-row');
      return row && getComputedStyle(row).backgroundColor !== 'rgba(0, 0, 0, 0)';
    }, null, {timeout: 4000});
    assert.deepEqual(await flatRow.boundingBox(), flatRowBox, 'the flat row does not move on hover');
    assert.match(await flatRow.evaluate(el => getComputedStyle(el).boxShadow), /inset/, 'hover draws the inset outline');
    await page.mouse.move(0, 0);

    // ---- references attached to a message group only inside that message
    const own = page.locator('#host .gchat-msg[data-id="m1"] .gchat-ref-group-inline');
    assert.equal(await own.count(), 1, 'the message holds exactly one flat attachment list');
    assert.equal(await own.locator('.gchat-ref-row').count(), 3);
    assert.deepEqual(await own.evaluate(el => [...el.children].map(child => child.className)), ['gchat-ref-rows'], 'the message list adds no header, count or collapse control either');
    assert.equal(await page.locator('#host .gchat-msg[data-id="m1"] .gchat-references > .gchat-reference').innerText(), 'Call notes', 'other references keep their chip');
    assert.equal(await page.locator('#host .gchat-msg[data-id="m1"] .gchat-body').innerText(), 'I grouped these follow-ups under the launch work.');
    await own.locator('button.gchat-ref-row').first().click();
    assert.deepEqual((await page.evaluate(() => opened.at(-1))), {type: 'task', id: 'msg-task-1', business_id: null, label: 'Reply to the investor', state: 'waiting'});
    await page.locator('#host .gchat-msg[data-id="m1"] .gchat-references > .gchat-reference').click();
    assert.deepEqual((await page.evaluate(() => opened.at(-1))), {type: 'material', id: 'material-1', business_id: null, label: 'Call notes'});

    // ---- an inline Signoff keeps its own mounted lifecycle
    await page.locator('#host .sg-card').waitFor();
    assert.deepEqual(await page.evaluate(() => mounted), ['sg-1']);
    // ---- scrolling helpers see the rows inside their group rather than the wrapper alone
    await page.setViewportSize({width: 1200, height: 900});
    await page.evaluate(() => document.querySelector('#host .gchat-msg[data-id="related:task:global:task-1"]').scrollIntoView({block: 'center'}));
    assert.equal(await page.locator('#host .gchat-ref-row').count(), 9, 'entry rows sit inside groups and inside the message');

    // ---- desktop screenshot of the actual record-thread DOM and CSS
    if (SHOTS) await page.screenshot({path: path.join(SHOTS, 'story-related-group-desktop.png')});

    // ---- narrow width: wrapped labels and no horizontal overflow
    await page.setViewportSize({width: 320, height: 800});
    await page.evaluate(() => { related.splice(2, 0, longRecord); return chat.refresh(); });
    await page.waitForFunction(() => document.querySelectorAll('#host .gchat-msg[data-id="related:task:global:task-long"]').length === 1);
    const narrow = await page.evaluate(() => {
      const group = document.querySelector('#host .gchat-ref-group-related');
      const row = document.querySelector('#host .gchat-msg[data-id="related:task:global:task-long"]');
      const title = row.querySelector('.gchat-ref-row-title');
      return {groupRight: group.getBoundingClientRect().right, innerWidth, titleWidth: title.getBoundingClientRect().width,
        titleHeight: title.getBoundingClientRect().height, rowOverflow: row.scrollWidth - row.clientWidth,
        pageOverflow: document.documentElement.scrollWidth - innerWidth};
    });
    assert.ok(narrow.groupRight <= narrow.innerWidth + 1, JSON.stringify(narrow));
    assert.ok(narrow.rowOverflow <= 1, 'a long title wraps without overflowing its row');
    assert.ok(narrow.titleHeight > 30, 'the long title stays readable on multiple lines');
    assert.ok(narrow.pageOverflow <= 1, 'the thread does not scroll horizontally at 320px');
    if (SHOTS) await page.screenshot({path: path.join(SHOTS, 'story-related-group-320.png')});

    // ---- a removed related Signoff disposes its card; no stale or duplicate rows remain
    await page.setViewportSize({width: 1200, height: 900});
    await page.evaluate(() => { related.splice(related.findIndex(item => item.id === 'sg-1'), 1); return chat.refresh(); });
    await page.waitForFunction(() => document.querySelectorAll('#host .sg-card').length === 0);
    assert.deepEqual(await page.evaluate(() => disposed), ['sg-1']);
    assert.equal(await page.locator('#host .gchat-ref-group-related').count(), 2);
    assert.equal(await page.locator('#host .gchat-ref-row').count(), 10, 'no duplicated rows after the refresh');

    // ---- a source switch builds a fresh thread without duplicates, and the fallback title is used
    await page.evaluate(() => mountThread({id: 'story-b'}));
    await page.locator('#host .gchat-ref-group-related').first().waitFor();
    assert.equal(await page.locator('#host .gchat-ref-group-related').count(), 1, 'a new source never reuses the old group nodes');
    assert.equal(await page.locator('#host .gchat-ref-group-related').getAttribute('aria-label'), 'Related work', 'an unsupplied source title falls back without inventing one');
    assert.equal(await page.locator('#host .gchat-ref-group-related .gchat-ref-row').count(), 2, 'the new thread shows only its first loaded page');
    const uniqueRows = await page.evaluate(() => {
      const ids = [...document.querySelectorAll('#host .gchat-ref-row')].map(row => row.closest('.gchat-msg').dataset.id);
      return [ids.length, new Set(ids).size];
    });
    assert.deepEqual(uniqueRows, [2, 2], 'rows are unique after switching sources');
    await page.evaluate(() => { chat.dispose(); document.querySelector('#host').replaceChildren(); });
    assert.equal(await page.locator('#host .gchat-ref-row').count(), 0, 'disposing the thread releases its rows');

    // ---- the shared view's scroll helpers still address the real rows inside a group
    const groupedView = await page.evaluate(() => {
      const host = document.createElement('div');
      host.id = 'view-probe';
      // Shorter than the flat list so the probe starts scrolled to the feed end, as a real thread does.
      host.style.cssText = 'position:fixed;left:0;top:0;width:520px;height:220px;display:flex;flex-direction:column;background:var(--bg);z-index:99';
      document.body.append(host);
      const view = mountChatView(host, {recordReferences: true, sourceKey: 'task:global:story-c', sourceTitle: 'Probe record'});
      const task = (n, label, state) => ({key: 't' + n, speaker: 'reference', referenceOnly: true, referenceRow: 'task',
        timestamp: `2026-10-27T10:0${n}:00Z`, typedReferences: [{type: 'task', id: 't' + n, business_id: null, label, state, available: true}]});
      const action = (n, label, state) => ({key: 'a' + n, speaker: 'reference', referenceOnly: true, referenceRow: 'action',
        timestamp: `2026-10-27T10:0${n}:30Z`, typedReferences: [],
        references: [{key: 'open', label, value: {type: 'action', id: 'a' + n, label, state}}]});
      view.render({entries: [
        {key: 'm0', speaker: 'agent', text: 'before', timestamp: '2026-10-27T09:59:00Z'},
        task(1, 'One', 'open'), task(2, 'Two', 'working'), action(1, 'Recorded work', 'withdrawn'),
        {key: 'm1', speaker: 'agent', text: 'between', timestamp: '2026-10-27T10:02:00Z'}, task(3, 'Three', 'waiting')
      ]});
      const result = {groups: host.querySelectorAll('.gchat-ref-group-related').length, visible: view.getVisibleKeys(),
        anchor: view.getScrollState().anchorKey};
      view.dispose(); host.remove();
      return result;
    });
    assert.equal(groupedView.groups, 2);
    assert.ok(['t1', 't2', 'a1'].some(key => groupedView.visible.includes(key)), JSON.stringify(groupedView));
    assert.ok(['t1', 't2', 'a1'].includes(groupedView.anchor), JSON.stringify(groupedView));

    assert.deepEqual(errors, []);
    console.log('Thread reference groups: contiguous flat Task/Action attachments, no header/counts/collapse, refresh, scopes, message refs, Signoff lifecycle and 320px layout passed.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
