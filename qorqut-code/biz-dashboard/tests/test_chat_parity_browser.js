#!/usr/bin/env node
// Visual parity of the one shared Chat view across its controllers: the real native Chat controller (js/global-chat.js) and the real
// thread controller (js/conversation.js) are mounted in equal containers with every production stylesheet layer (app, conversation,
// Focus, chat-view, global-chat, agent-workspace). Mocked transport only; any external request fails the run.
//   PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_chat_parity_browser.js
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const BODY = 'Same representative body with `code`, a **bold** word and enough text to wrap across the narrow container width twice over.';

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1300, height: 900}, locale: 'en-US', timezoneId: 'America/La_Paz'});
    const errors = [], external = [];
    page.on('pageerror', error => errors.push(error.stack || error.message));
    await page.route('**/*', route => {
      const url = route.request().url();
      if (url.startsWith('http://fixture.test/')) return route.fulfill({body: '<html></html>', contentType: 'text/html'});
      // app.css @imports a web font; it is answered locally with an empty sheet (system fonts), never fetched.
      if (/^https:\/\/fonts\.(googleapis|gstatic)\.com\//.test(url)) return route.fulfill({body: '', contentType: 'text/css'});
      external.push(url); return route.abort();
    });
    await page.goto('http://fixture.test/');
    await page.setContent(`<div style="display:flex;gap:20px">
      <div class="agsplit"><div id="native-box" class="global-agent-session" style="width:560px;height:520px;display:flex;flex-direction:column;position:relative"><div id="pane" style="flex:1;min-height:0;display:flex;flex-direction:column"></div></div></div>
      <div class="agfocus"><div class="agfocus-layout"><aside id="thread-box" class="agfocus-dock" style="width:560px;height:520px;position:relative;display:flex;flex-direction:column"><div id="thread"></div></aside></div></div></div>
      <div id="toasts"></div><button id="wsbtn"></button>`);
    // The production workspace ancestor class that activates the agent-workspace rules.
    await page.evaluate(() => document.body.classList.add('agent-workspace-app'));
    for (const file of ['app.css', 'css/conversation.css', 'css/agent-focus.css', 'css/chat-view.css', 'css/global-chat.css', 'css/agent-workspace.css', 'css/agent-workspace-nav.css'])
      await page.addStyleTag({path: path.join(root, file)});
    for (const file of ['js/core.js', 'js/safe-links.js']) await page.addScriptTag({path: path.join(root, file)});
    await page.evaluate(body => {
      window.state = {session: {is_owner: true}, biz: 'widget', bizs: [], agents: []};
      window.EventSource = class { constructor() { this.readyState = 1; } addEventListener() {} close() {} };
      const agentEntry = {id: 'id1', kind: 'message', speaker: 'agent', text: body, timestamp: '2026-10-04T21:25:00Z', position: 100, partial: false, identity: 'native_id', completion_sequence: 1, completion_id: 'c1', system: null};
      const ownerEntry = {...agentEntry, id: 'id2', speaker: 'user', text: 'Owner reply', position: 200, completion_sequence: null, completion_id: null};
      const binding = {binding_id: 'b1', agent_id: 'agent-a', provider: 'claude', conversation_id: 'conv-1', generation: 1, transport: 'terminal'};
      const nativeState = {agent_id: 'agent-a', name: 'Marketing', revision: 1, cursor: 1, entries: [agentEntry, ownerEntry], older: null, rendered_boundary: 1, inputs: [], actions: [], last_read_sequence: 1, newest_sequence: 1,
        session: {state: 'live', reason: 'live', can_send: true, working: false, provider: 'claude', conversation_id: 'conv-1', transport: 'terminal', detail: 'Live in Terminal', binding},
        conversation: {conversation_id: 'conv-1', provider: 'claude', complete: true, finalized: false, error: null, captured_length: 1}, conversations: [{conversation_id: 'conv-1', provider: 'claude', updated_at: '2026-10-04T21:25:00Z'}]};
      const operation = async path => {
        if (path.endsWith('owner_conversation_peers')) return {data: {peers: []}};
        if (path.endsWith('owner_conversation_get')) return {data: {conversation: {latest_sequence: 2, message_count: 2}, messages: [
          {id: 'm1', sequence: 1, body, created_by: 'dev', created_by_name: 'Dev', author_kind: 'agent', created_at: '2026-10-04T21:25:00Z'},
          {id: 'm2', sequence: 2, body: 'Owner reply', created_by: 'o', created_by_name: 'Owner', author_kind: 'user', is_owner: true, created_at: '2026-10-04T21:25:00Z'}], read_state: {last_read_sequence: 2, unread: false}}};
        if (path.endsWith('tasks_thread_get')) return {data: {notes: [
          {id: 'n1', sequence: 1, body, created_by: 'dev', created_by_name: 'Dev', author_kind: 'agent', created_at: '2026-10-04T21:25:00Z'},
          {id: 'n2', sequence: 2, body: 'Owner reply', created_by: 'o', created_by_name: 'Owner', author_kind: 'user', is_owner: true, created_at: '2026-10-04T21:25:00Z'}]}};
        if (path.endsWith('tasks_get')) return {data: {task: {revision: 1}}};
        return {data: {}};
      };
      window.fetch = async url => {
        if (/\/chat$/.test(String(url).split('?')[0])) return new Response(JSON.stringify({status: 'ok', data: nativeState}));
        if (String(url).includes('/operations/')) return new Response(JSON.stringify({status: 'ok', ...(await operation(String(url).split('/operations/')[1]))}));
        return new Response(JSON.stringify({status: 'error', error: {code: 'not_found', message: 'unexpected ' + url}}), {status: 404});
      };
    }, BODY);
    for (const file of ['js/chat-view.js', 'js/conversation.js', 'js/global-chat.js']) await page.addScriptTag({path: path.join(root, file)});
    await page.evaluate(() => {
      window.native = mountGlobalChat(document.querySelector('#pane'), {agent_id: 'agent-a', display_name: 'Marketing'}, {openTerminal() {}});
      window.thread = conversationMount(document.querySelector('#thread'), {type: 'task', id: 'task1', biz: 'widget'}, {channel: 'team', inlineComposer: true, dismissOnOutside: false});
      thread.expand();
    });
    await page.locator('#pane .gchat-msg').first().waitFor();
    await page.locator('#thread .gchat-msg').first().waitFor();
    await page.locator('#thread textarea').waitFor({state: 'visible'});

    const read = scope => page.evaluate(scope => {
      const root = document.querySelector(scope), css = (el, props) => { const s = getComputedStyle(el); return Object.fromEntries(props.map(p => [p, s[p]])); };
      const agent = [...root.querySelectorAll('.gchat-msg')].find(n => n.querySelector('.gchat-body')?.textContent.includes('Same representative'));
      const ownerMsg = [...root.querySelectorAll('.gchat-msg')].find(n => n.querySelector('.gchat-body')?.textContent.includes('Owner reply'));
      const body = agent.querySelector('.gchat-body'), box = root.querySelector('.gchat-composer'), area = box.querySelector('textarea'), send = box.querySelector('.gchat-send');
      const typo = ['fontFamily', 'fontSize', 'lineHeight', 'fontWeight', 'letterSpacing', 'color'];
      const edge = ['paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'backgroundColor', 'borderTopWidth', 'borderTopColor', 'borderRadius'];
      const rect = el => { const r = el.getBoundingClientRect(); return [r.width, r.height]; };
      return {body: css(body, [...typo, ...edge]), ownerBody: css(ownerMsg.querySelector('.gchat-body'), [...typo, ...edge]), ownerMsg: css(ownerMsg, ['marginBottom', 'gap', 'display', 'alignSelf', 'maxWidth']), msg: css(agent, ['marginBottom', 'gap', 'display']), log: css(root.querySelector('.gchat-log'), ['gap', 'paddingLeft', 'paddingRight']),
        composer: css(box, [...typo, ...edge]), textarea: css(area, [...typo, 'paddingTop', 'paddingLeft', 'minHeight']), send: css(send, [...typo, 'height', 'paddingLeft', 'paddingRight', 'borderRadius']),
        sendSize: rect(send), composerSize: rect(box), scroller: rect(root.querySelector('.gchat-scroll')), view: rect(root.querySelector('.chat-view'))};
    }, scope);
    const native = await read('#native-box'), thread = await read('#thread-box');
    for (const key of ['body', 'ownerBody', 'ownerMsg', 'msg', 'log', 'composer', 'textarea', 'send']) assert.deepEqual(thread[key], native[key], key + ' computed styles match main Chat');
    assert.deepEqual(thread.sendSize, native.sendSize, 'Send dimensions match');
    assert.equal(native.sendSize[1], 36, 'Send is the main Chat height');
    assert.ok(parseFloat(thread.body.fontSize) === 13.25, 'no Focus-specific shrink of the body text');
    assert.ok(parseFloat(thread.textarea.fontSize) >= 15, 'no Focus-specific shrink of the composer text');
    const inner = await page.evaluate(() => ['#native-box', '#thread-box'].map(s => { const el = document.querySelector(s), c = getComputedStyle(el); return el.clientWidth - parseFloat(c.paddingLeft) - parseFloat(c.paddingRight); }));
    assert.deepEqual([native.view[0], thread.view[0]], inner, 'each view fills its container exactly (no extra inset)');

    // Focus dock containment: everything stays inside the dock and the visible composer is reachable.
    const inside = await page.evaluate(() => {
      const dock = document.querySelector('#thread-box').getBoundingClientRect(), within = el => { const r = el.getBoundingClientRect(); return r.left >= dock.left - 1 && r.right <= dock.right + 1 && r.top >= dock.top - 1 && r.bottom <= dock.bottom + 1; };
      return ['.gchat-scroll', '.gchat-composer', '.gchat-send', 'textarea'].map(s => [s, within(document.querySelector('#thread-box ' + s))]);
    });
    for (const [selector, ok] of inside) assert.equal(ok, true, selector + ' stays inside the Focus dock');

    // A collapsed inline thread keeps its externally hosted composer visible and usable.
    await page.evaluate(() => thread.collapse());
    assert.equal(await page.locator('#thread-box .gchat-scroll:visible').count(), 0, 'the feed is collapsed');
    assert.equal(await page.locator('#thread-box textarea').isVisible(), true, 'the hosted composer stays visible');
    assert.equal(await page.locator('#thread-box .gchat-send').isVisible(), true);
    const collapsed = await page.evaluate(() => { const s = getComputedStyle(document.querySelector('#thread-box textarea')); return [s.fontSize, s.lineHeight]; });
    assert.deepEqual(collapsed, [thread.textarea.fontSize, thread.textarea.lineHeight], 'collapsed composer keeps the shared typography');

    assert.deepEqual(external, [], 'no external traffic');
    assert.deepEqual(errors, [], 'no page errors');
    console.log('Chat parity: native and thread controllers share body, composer and Send styling; Focus dock containment and hosted composer visibility passed.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
