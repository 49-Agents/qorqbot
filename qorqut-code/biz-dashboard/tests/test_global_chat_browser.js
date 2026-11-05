#!/usr/bin/env node
// Real Chromium DOM with the real js/global-chat.js, js/safe-links.js and js/core.js, but a MOCKED transport: fetch and
// EventSource are scripted in the page with synthetic records shaped like the reviewed native Chat DTO
// (qorqut-code docs/v1.2/NATIVE_CHAT_DTO.md). It proves the Chat view's behaviour, not a live session or agent.
//   PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_global_chat_browser.js
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
let checks = 0;
const check = async (label, action) => { await action(); checks++; console.log('ok ' + label); };

(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1000, height: 800}, locale:'en-US', timezoneId:'America/La_Paz'});
    const errors = [];
    page.on('pageerror', error => errors.push(error.stack || error.message));
    await page.route('http://fixture.test/**', route => route.fulfill({body: '<html></html>', contentType: 'text/html'}));
    await page.goto('http://fixture.test/');
    await page.setContent('<div id="toasts"></div><button id="wsbtn"></button><div id="pane" style="height:520px;width:760px;display:flex;flex-direction:column"></div>');
    await page.addScriptTag({path: path.join(root, 'js/core.js')});
    await page.addScriptTag({path: path.join(root, 'js/safe-links.js')});
    await page.addStyleTag({path: path.join(root, 'css/chat-view.css')});
    await page.addStyleTag({path: path.join(root, 'css/global-chat.css')});
    await page.evaluate(() => {
      window.calls = []; window.sources = [];
      // script.states: agent id -> current native conversation state.
      window.script = {states: {}, live: {binding_id: 'b1', provider: 'claude', conversation_id: 'conv-1', generation: 1}, queries: [], history: null, postError: null, inputs: new Map(), hold: null, completions: new Set(), readError: null};
      window.EventSource = class {
        static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
        constructor(url) { this.url = url; this.readyState = 1; this.l = {}; window.sources.push(this); }
        addEventListener(type, fn) { (this.l[type] ||= []).push(fn); }
        close() { this.readyState = 2; this.closed = true; }
        emit(type, data) { (this.l[type] || []).forEach(fn => fn({data: typeof data === 'string' ? data : JSON.stringify(data)})); }
        fail(closed) { this.readyState = closed ? 2 : 0; this.onerror && this.onerror(); }
      };
      window.fetch = async (url, options = {}) => {
        const method = (options.method || 'GET').toUpperCase(), raw = options.body instanceof Blob;
        const body = options.body && !raw ? JSON.parse(options.body) : null;
        window.calls.push({url, method, body});
        const respond = (data, status = 200) => new Response(JSON.stringify(status === 200 ? {status: 'ok', data} : {status: 'error', error: data}), {status});
        const match = /^\/api\/root\/agents\/([^/?]+)\/chat(\/[a-z]+)?(\?.*)?$/.exec(url);
        if (!match) return respond({code: 'not_found', message: 'unexpected ' + method + ' ' + url}, 404);
        const agent = decodeURIComponent(match[1]), tail = match[2] || '', query = new URLSearchParams(match[3] || '');
        if (method === 'POST' && tail === '/attachments') {
          const name = decodeURIComponent(options.headers['X-File-Name']);
          const file = `/absolute/path/to/qorqut/operations/agents/ordubashy/downloads/upload-1/${name}`;
          return respond({id: 'upload-1', name, size: options.body.size, path: file,
            url: '/files/view?path=' + encodeURIComponent(file), reference: `[${name}](<${file}>)`});
        }
        if (method === 'GET' && tail === '') {
          window.script.queries.push(Object.fromEntries(query));
          if (query.has('conversation') || query.has('provider')) return respond({code: 'not_found', message: 'Chat follows the current conversation'}, 404);
          return respond(window.script.states[agent]);
        }
        if (method === 'GET' && tail === '/history') return respond(window.script.history);
        if (method === 'POST' && tail === '/messages') {
          if (window.script.hold) await window.script.hold;
          if (window.script.postError) return respond(window.script.postError.error, window.script.postError.status || 422);
          let input = window.script.inputs.get(body.idempotency_key);
          if (input && input.pin !== body.expected_binding) return respond({code: 'idempotency_conflict', message: 'pin changed'}, 409);
          if (!input && body.expected_binding !== window.script.live.binding_id) return respond({code: 'terminal_changed', message: 'binding changed'}, 422);
          const created = !input;
          if (created) {
            input = {id: 'in-' + window.script.inputs.size, kind: 'input', sequence: window.script.inputs.size + 1, origin: 'owner', input_type: 'chat', state: window.script.nextState || 'waiting',
              reason: null, body: window.script.bodyless ? null : body.text, session_id: 's1', evidence: {}, created_at: '2026-10-04T21:25:00Z', settled_at: null,
              binding: {...window.script.live, agent_id: agent, transport: 'terminal'}, idempotency_key: body.idempotency_key, pin: body.expected_binding};
            window.script.inputs.set(body.idempotency_key, input);
            window.script.states[agent].inputs = [input, ...window.script.states[agent].inputs];
          }
          if (window.script.loseResponse && created) return respond({code: 'unavailable', message: 'Lost after admission'}, 503);
          const {pin, ...visible} = input;
          return respond({input: visible, binding: input.binding, created});
        }
        if (method === 'POST' && tail === '/read') {
          if (window.script.readError) return respond(window.script.readError, 422);
          const state = window.script.states[agent];
          state.last_read_sequence = Math.max(state.last_read_sequence, body.sequence);
          return respond({last_read_sequence: state.last_read_sequence, newest_sequence: state.newest_sequence});
        }
        return respond({code: 'not_found', message: 'unexpected ' + method + ' ' + tail}, 404);
      };
      window.EV = n => ({id: 'id' + n, kind: 'message', speaker: 'agent', text: '', timestamp: null, position: n * 100, partial: false, identity: 'native_id',
        completion_sequence: null, completion_id: null, system: null});
      window.liveSession = (extra = {}) => ({state: 'live', reason: 'live', can_send: true, working: false, provider: 'claude', conversation_id: 'conv-1', transport: 'terminal', detail: 'Live in Terminal', ...extra});
    });
    await page.addScriptTag({path: path.join(root, 'js/chat-view.js')});
    await page.addScriptTag({path: path.join(root, 'js/global-chat.js')});

    // Synthetic DTO builders (node side; serialised into the page)
    let completion = 0;
    const ent = (n, speaker, text, extra = {}) => ({id: 'id' + n, kind: 'message', speaker: speaker === 'owner' ? 'user' : speaker, text, timestamp: null, position: n * 100, partial: false,
      identity: 'native_id', completion_sequence: speaker === 'agent' && extra.seq ? extra.seq : null, completion_id: speaker === 'agent' && extra.seq ? 'c' + extra.seq : null, system: null, ...extra.entry});
    const withSeq = (n, text, seq) => ent(n, 'agent', text, {seq});
    const binding = (extra = {}) => ({binding_id: 'b1', agent_id: 'agent-a', provider: 'claude', conversation_id: 'conv-1', generation: 1, transport: 'terminal', ...extra});
    const session = (extra = {}) => ({state: 'live', reason: 'live', can_send: true, working: false, provider: 'claude', conversation_id: 'conv-1', transport: 'terminal', detail: 'Live in Terminal', binding: binding(), ...extra});
    const conv = (id = 'conv-1') => ({conversation_id: id, provider: 'claude', complete: true, finalized: false, error: null, captured_length: 1});
    const base = (entries, extra = {}) => {
      const sequences = entries.map(e => e.completion_sequence).filter(Boolean);
      if (extra.inputs) extra = {...extra, inputs: extra.inputs.map(i => ({binding: binding(), idempotency_key: null, ...i}))};
      return {agent_id: 'agent-a', name: 'Marketing', session: session(), revision: 10, cursor: 10, conversation: conv(), conversations: [{conversation_id: 'conv-1', provider: 'claude', updated_at: '2026-10-04T21:25:00Z', current: true}],
        entries, older: null, rendered_boundary: Math.max(0, ...sequences), inputs: [], actions: [], last_read_sequence: 0, newest_sequence: Math.max(0, ...sequences), ...extra};
    };
    const mount = async (state, id = 'agent-a', name = 'Marketing', keep = false) => {
      if (!keep) await page.waitForFunction(() => GLOBAL_CHAT_INFLIGHT.size === 0, null, {timeout: 1500}).catch(() => {});   // let a previous check's send settle
      await page.evaluate(([state, id, name, keep]) => {
        if (!keep && typeof GLOBAL_CHAT_ACCEPTED !== "undefined") GLOBAL_CHAT_ACCEPTED.clear();
        window.calls.length = 0; window.sources.length = 0; window.terminalOpened = 0;
        window.script.loseResponse = false; window.script.states[id] = state; window.script.live = state.session?.binding ? {...state.session.binding} : window.script.live; window.script.postError = null; window.script.hold = null; window.script.readError = null; window.script.bodyless = false; window.script.nextState = null;
        window.chat?.dispose();
        window.chat = mountGlobalChat(document.querySelector('#pane'), {agent_id: id, display_name: name}, {openTerminal: () => window.terminalOpened = (window.terminalOpened || 0) + 1});
      }, [state, id, name, keep]);
      await page.waitForFunction(() => window.sources.length === 1);
      await page.waitForFunction(() => !document.querySelector('.gchat-session[data-state="loading"]'));
    };
    const script = (patch, id = 'agent-a') => page.evaluate(([patch, id]) => { Object.assign(window.script.states[id], patch); }, [patch, id]);
    const emit = (type, data) => page.evaluate(([type, data]) => window.sources.at(-1).emit(type, data), [type, data]);
    let cursorNow = 10;
    const changed = async (patch = {}, id = 'agent-a') => {
      cursorNow = Math.max(cursorNow, await page.evaluate(id => window.script.states[id].cursor, id)) + 1;
      await script({cursor: cursorNow, revision: cursorNow, ...patch}, id);
      await emit('chat', {cursor: cursorNow, kind: 'changed', data: {revision: cursorNow, newest_sequence: 0, waiting: 0}});
    };
    const push = async (entry, extra = {}) => {
      const state = await page.evaluate(() => window.script.states['agent-a']);
      const entries = [...state.entries.filter(e => e.id !== entry.id), entry].sort((a, b) => a.position - b.position);
      const sequences = entries.map(e => e.completion_sequence).filter(Boolean);
      await changed({entries, newest_sequence: Math.max(0, ...sequences), rendered_boundary: Math.max(0, ...sequences), ...extra});
    };
    const posts = tail => page.evaluate(tail => window.calls.filter(c => c.method === 'POST' && c.url.endsWith(tail)), tail);
    const gets = () => page.evaluate(() => window.calls.filter(c => c.method === 'GET').map(c => c.url));
    const order = () => page.locator('.gchat-log .gchat-msg').evaluateAll(nodes => nodes.map(n => n.dataset.id));
    const box = () => page.getByRole('textbox', {name: /^Message /});
    const sessionText = () => page.locator('.gchat-session-text').innerText();
    const settle = () => page.waitForTimeout(550);

    await check('owner and agent text use 13.25px while the composer keeps its size', async () => {
      await mount(base([ent(1, 'owner', 'hello'), ent(2, 'agent', 'Paragraph\n\n- item')]));
      await page.waitForSelector('.gchat-msg[data-id="id2"] li');
      const sizes = await page.evaluate(() => ['.gchat-user .gchat-body', '.gchat-msg[data-id="id2"] p', '.gchat-msg[data-id="id2"] li', '.gchat-composer textarea']
        .map(selector => getComputedStyle(document.querySelector(selector)).fontSize));
      assert.deepEqual(sizes, ['13.25px', '13.25px', '13.25px', '15.75px']);
      const look = await page.evaluate(() => {
        const user = getComputedStyle(document.querySelector('.gchat-user .gchat-body')), agent = getComputedStyle(document.querySelector('.gchat-agent .gchat-body'));
        return {user: user.backgroundColor, agent: agent.backgroundColor, agentBorder: agent.borderTopWidth, max: getComputedStyle(document.querySelector('.gchat-agent')).maxWidth};
      });
      assert.notEqual(look.user, 'rgba(0, 0, 0, 0)');
      assert.notEqual(look.agent, 'rgba(0, 0, 0, 0)');
      assert.equal(look.agentBorder, '0px');
      assert.match(look.max, /760px/, 'the transcript column is capped');
    });
    await check('Telegram relays collapse; direct input stays visible and keyboard toggles expansion', async () => {
      await mount(base([
        ent(1, 'user', 'Direct Chat input'),
        ent(2, 'user', '[telegram] Full Telegram text'),
        ent(3, 'user', 'Qorqut owner message via Telegram (correlation example).\n\nOlder Telegram text'),
      ]));
      const telegram = page.locator('.gchat-msg[data-id="id2"] details');
      assert.equal(await page.locator('.gchat-msg[data-id="id1"] .gchat-text').isVisible(), true);
      assert.equal(await telegram.locator('.gchat-text').isVisible(), false);
      assert.equal(await page.locator('.gchat-msg[data-id="id3"] details').count(), 1);
      assert.equal(await telegram.locator('svg').count(), 1);
      await telegram.locator('summary').click();
      assert.equal(await telegram.locator('.gchat-text').isVisible(), true);
      assert.equal(await telegram.locator('.gchat-text').innerText(), '[telegram] Full Telegram text');
      await telegram.locator('summary').focus();
      await page.keyboard.press('Enter');
      assert.equal(await telegram.locator('.gchat-text').isVisible(), false);
    });

    await check('the shared stylesheet carries the production workspace look: 22px rhythm, padded message bubbles and rounded composer', async () => {
      await mount(base([ent(1, 'owner', 'hello'), ent(2, 'agent', 'reply text', {seq: 1})]));
      const tokens = await page.addStyleTag({content: ':root { --line: #444; --line-2: #555; --surface: #161616; }'});
      const look = await page.evaluate(() => {
        const css = (selector, ...props) => { const style = getComputedStyle(document.querySelector(selector)); return props.map(prop => style[prop]); };
        return {
          gap: css('.gchat-log', 'rowGap'), spacing: css('.gchat-msg + .gchat-msg', 'marginTop'), agent: css('.gchat-agent .gchat-body', 'paddingLeft', 'lineHeight'), user: css('.gchat-user .gchat-body', 'borderTopLeftRadius', 'lineHeight'),
          composer: css('.gchat-composer', 'borderTopWidth', 'borderTopLeftRadius', 'paddingTop', 'columnGap'), area: css('.gchat-composer textarea', 'borderTopWidth', 'fontSize'),
          send: css('.gchat-send', 'height', 'borderTopLeftRadius'), inset: css('.gchat-scroll', 'paddingTop'),
          hooks: ['.chat-view', '.gchat-scroll', '.gchat-log', '.gchat-older', '.gchat-jump', '.gchat-banner', '.gchat-composer', '.gchat-pending-list', '.gchat-empty'].map(selector => !!document.querySelector(`.gchat[data-global-chat] ${selector}`)),
        };
      });
      await tokens.evaluate(node => node.remove());
      assert.deepEqual(look.gap, ['0px']); assert.deepEqual(look.spacing, ['22px']);
      assert.deepEqual(look.agent, ['18px', '21.8625px']);
      assert.deepEqual(look.user, ['24px', '21.8625px']);
      assert.deepEqual(look.composer, ['1px', '32px', '10px', '8px']);
      assert.deepEqual(look.area, ['0px', '15.75px']);
      assert.deepEqual(look.send, ['36px', '18px']);
      assert.deepEqual(look.inset, ['38px'], 'the native floating header keeps its reserved space');
      assert.equal(look.hooks.every(Boolean), true, 'one shared root inside the native shell exposes every consumer hook');
      assert.equal(await page.locator('.gchat-composer textarea').getAttribute('maxlength'), '17000');
    });
    await check('timestamps use the entry timestamp in the browser time zone', async () => {
      const at = (n, role, timestamp) => ({...ent(n, role, 'Hello'), timestamp});
      await mount(base([at(1, 'owner', '2026-10-04T21:25:00Z'), at(2, 'agent', '2026-10-04T01:15:00Z'), at(3, 'agent', null), at(4, 'owner', 'invalid')]));
      const time = id => page.locator(`.gchat-msg[data-id="${id}"] .gchat-time`);
      assert.equal(await time('id1').innerText(), 'Oct 4, 05:25 PM');
      assert.equal(await time('id2').innerText(), 'Oct 3, 09:15 PM', 'local midnight can differ from the UTC date');
      assert.equal(await time('id2').getAttribute('datetime'), '2026-10-04T01:15:00.000Z');
      assert.match(await time('id2').getAttribute('title'), /2026.*9:15:00 PM.*GMT-4/);
      assert.equal(await time('id3').count(), 0);
      assert.equal(await time('id4').count(), 0);
      await push(at(5, 'agent', '2026-10-04T21:26:00Z'));
      assert.equal(await time('id5').innerText(), 'Oct 4, 05:26 PM');
      await push(at(5, 'agent', '2026-10-04T21:27:00Z'));
      assert.equal(await time('id5').innerText(), 'Oct 4, 05:27 PM');
      assert.equal(await time('id5').count(), 1, 'updates never duplicate the timestamp');
    });
    await check('entries keep stable ids and archive order; replays and rewrites never duplicate or move them', async () => {
      await mount(base([ent(3, 'agent', 'third'), ent(1, 'owner', 'first'), ent(2, 'agent', 'second')].sort((a, b) => a.position - b.position)));
      assert.deepEqual(await order(), ['id1', 'id2', 'id3']);
      await page.locator('.gchat-msg[data-id="id2"]').evaluate(node => { node.dataset.mark = 'kept'; });
      await push(ent(4, 'agent', 'fourth'));
      await push(ent(4, 'agent', 'fourth'));
      await changed();
      assert.deepEqual(await order(), ['id1', 'id2', 'id3', 'id4']);
      assert.equal(await page.locator('.gchat-msg[data-id="id2"]').getAttribute('data-mark'), 'kept', 'unchanged entries are the same elements');
      await push({...ent(4, 'agent', 'fourth'), id: 'id4', text: 'fourth, finished', partial: false, completion_sequence: 7, completion_id: 'c7'});
      assert.equal(await page.locator('.gchat-msg[data-id="id4"]').innerText().then(t => t.includes('finished')), true);
      assert.equal(await page.locator('.gchat-msg').count(), 4);
      // A partial narration entry is visibly provisional, then becomes the finished reply under the same id.
      await push({...ent(5, 'agent', 'checking…'), partial: true});
      assert.equal(await page.locator('.gchat-msg[data-id="id5"].gchat-partial').count(), 1);
      await push({...ent(5, 'agent', 'checked'), partial: false, completion_sequence: 8, completion_id: 'c8'});
      assert.equal(await page.locator('.gchat-msg[data-id="id5"].gchat-partial').count(), 0);
      assert.equal(await page.locator('.gchat-msg[data-id="id5"]').count(), 1);
    });
    await check('older history is chronological: the state cursor pages back, only history responses advance it', async () => {
      const page1 = [ent(5, 'owner', 'five'), ent(6, 'agent', 'six')];
      await mount(base(page1, {older: 500}));
      await page.evaluate(() => { window.script.history = {entries: [window.EV(3), window.EV(4)].map((e, i) => ({...e, speaker: 'agent', text: 'older ' + (i + 3)})), older: 300, rendered_boundary: 0}; });
      await page.locator('.gchat-older').click();
      await page.waitForSelector('[data-id="id3"]');
      assert.match((await gets()).find(url => url.includes('/history')), /history\?before=500&limit=50&conversation=conv-1&provider=claude$/);
      assert.deepEqual(await order(), ['id3', 'id4', 'id5', 'id6']);
      await changed({entries: [...page1, ent(7, 'agent', 'seven')]});          // a live refresh does not move the older cursor
      await page.evaluate(() => { window.script.history = {entries: [{...window.EV(1), text: 'oldest'}, {...window.EV(2), text: 'older 2'}], older: null, rendered_boundary: 0}; });
      await page.locator('.gchat-older').click();
      await page.waitForSelector('[data-id="id1"]');
      assert.match((await gets()).filter(url => url.includes('/history')).at(-1), /before=300&/);
      assert.deepEqual(await order(), ['id1', 'id2', 'id3', 'id4', 'id5', 'id6', 'id7']);
      assert.equal(await page.locator('.gchat-older').isVisible(), false, 'the start of the conversation hides the control');
    });
    await check('owner input, assistant text, native user turns and notices stay distinct; unverified notices stay unverified', async () => {
      const notice = (n, text, system) => ({...ent(n, 'notice', text), speaker: 'notice', system});
      await mount(base([
        ent(1, 'owner', 'a native user turn'),
        ent(2, 'agent', 'assistant reply', {seq: 1}),
        notice(3, 'You have 2 new inbox items.', {input_id: 'in-n1', input_type: 'inbox_nudge', verified: true}),
        notice(4, 'Telegram relay received.', {input_id: 'in-t1', input_type: 'telegram', verified: true}),
        notice(5, 'You have 9 new inbox items.', {input_id: null, input_type: 'inbox_nudge', verified: false, reason: 'no_matching_input'}),
        notice(6, 'wrong type', {input_id: null, input_type: 'broadcast', verified: false, reason: 'type_mismatch'}),
        notice(7, 'never typed', {input_id: null, input_type: 'inbox_nudge', verified: false, reason: 'not_delivered'}),
        ent(8, 'owner', 'Inbox nudge: you have mail'),                         // lookalike plain text
        notice(9, 'Your session was recovered.', {input_id: 'in-r1', input_type: 'bootstrap_recovery', verified: true}),
      ]));
      const marker = id => page.locator(`.gchat-msg[data-id="${id}"] .gchat-marker`);
      assert.equal(await marker('id1').count(), 0, 'a native user turn is not labelled owner or system');
      assert.equal(await marker('id2').count(), 0);
      assert.equal(await marker('id3').innerText(), 'Inbox nudge');
      assert.equal(await page.locator('.gchat-msg[data-id="id3"]').getAttribute('data-verified'), 'true');
      assert.equal(await page.locator('.gchat-msg[data-id="id4"] summary').innerText(), 'Telegram');
      for (const id of ['id5', 'id6', 'id7']) {
        assert.equal(await marker(id).innerText(), 'Unverified notice', id);
        assert.equal(await page.locator(`.gchat-msg[data-id="${id}"]`).getAttribute('data-verified'), 'false');
      }
      assert.equal(await page.locator('.gchat-session').getAttribute('title'), null, 'no session tooltip');
      for (const id of ['id5', 'id6', 'id7']) assert.equal(await marker(id).getAttribute('title'), null, 'no receipt-mechanics tooltip on ' + id);
      assert.equal(await page.locator('.gchat-msg[data-id="id3"] .gchat-text').count(), 0, 'a verified Inbox nudge is only its compact marker');
      assert.equal(await page.locator('.gchat-msg[data-id="id6"] .gchat-text').innerText(), 'wrong type', 'an unverified notice keeps its text');
      assert.equal(await page.locator('.gchat-msg[data-id="id4"] .gchat-text').isVisible(), false, 'Telegram is collapsed by default');
      assert.equal(await marker('id8').count(), 0, 'equal text alone verifies nothing');
      assert.equal(await marker('id9').innerText(), 'Session recovery', 'bootstrap recovery has its own system label, never Inbox nudge');
      assert.equal(await page.locator('.gchat-msg[data-id="id1"] .gchat-origin, .gchat-msg .gchat-owner').count(), 0);
      await page.addStyleTag({content: ':root{--danger:rgb(200,0,0);--fg-2:rgb(100,100,100)}'});   // the app theme tokens
      const style = id => page.locator(`.gchat-msg[data-id="${id}"] .gchat-marker`).evaluate(node => getComputedStyle(node).color);
      assert.notEqual(await style('id3'), await style('id5'), 'a verified and an unverified marker look different');
      assert.equal(await page.locator('.gchat-msg[data-id="id9"] .gchat-text').count(), 0, 'session recovery is only its compact label');
      if (process.env.CHAT_SHOT_DIR) {                       // the styled Chat mount with status, a verified nudge and the composer
        const sheet = await page.addStyleTag({path: path.join(root, 'app.css')});
        for (const [name, width] of [['desktop', 760], ['narrow', 390]]) {
          await page.setViewportSize({width: width + 20, height: 600});
          await page.evaluate(w => { document.body.style.margin = '0'; document.querySelector('#pane').style.width = w + 'px'; }, width);
          await page.locator('#pane').screenshot({path: `${process.env.CHAT_SHOT_DIR}/chat-${name}.png`});
        }
        await page.evaluate(() => { document.querySelector('#pane').style.width = '760px'; });
        await sheet.evaluate(node => node.remove());
        await page.setViewportSize({width: 1000, height: 800});
      }
    });
    await check('outgoing agent messages are one centered "Messaged [SVGs] N Bots" row; adjacent entries merge by distinct recipient and any other entry breaks the group', async () => {
      const recipient = (id, name, extra = {}) => ({agent_id: id, display_name: name, role: {id: 'global-worker'}, presentation: {department: 'operations', icon: 'brain'}, ...extra});
      const sent = (n, ...recipients) => ({...ent(n, 'notice', ''), speaker: 'notice', agent_message: {message_ids: ['m' + n], recipients}});
      const atlas = recipient('r-atlas', 'Atlas'), scout = recipient('r-scout', 'Scout', {presentation: {department: 'marketing', icon: 'circle'}});
      const evil = recipient('r-evil', '<img src=x onerror="window.pwned=1">', {role: {id: 'founder'}, presentation: {}});
      const entries = [
        sent(1, atlas),
        ent(2, 'owner', 'a user message'),
        sent(3, atlas), sent(4, scout, atlas), sent(5, evil),
        ent(6, 'agent', 'assistant reply', {seq: 1}),
        sent(7, scout),
        {...ent(8, 'notice', 'You have 2 new inbox items.'), speaker: 'notice', system: {input_id: 'in-n1', input_type: 'inbox_nudge', verified: true}},
        sent(9, atlas),
      ];
      await mount(base(entries));
      assert.deepEqual(await order(), ['id1', 'id2', 'id3', 'id6', 'id7', 'id8', 'id9'], 'adjacent sends collapse into the first entry; user, assistant and other notices break groups');
      const row = id => page.locator(`.gchat-msg[data-id="${id}"] .gchat-agent-message`);
      assert.equal((await row('id1').innerText()).replace(/\s+/g, ' ').trim(), 'Messaged');
      assert.equal((await row('id3').innerText()).replace(/\s+/g, ' ').trim(), 'Messaged 3 Bots', 'distinct recipients only: Atlas twice counts once');
      assert.deepEqual(await row('id3').locator('.gchat-recipient').evaluateAll(n => n.map(x => x.getAttribute('aria-label'))), ['Atlas', 'Scout', '<img src=x onerror="window.pwned=1">'], 'first-seen order');
      assert.equal(await row('id3').locator('.gchat-recipient').first().getAttribute('title'), 'Atlas');
      assert.equal(await row('id3').getAttribute('aria-label'), 'Messaged Atlas, Scout, <img src=x onerror="window.pwned=1">, 3 Bots');
      assert.equal(await page.locator('.gchat-agent-message img').count(), 0, 'names are escaped, never markup');
      assert.equal(await page.evaluate(() => window.pwned), undefined);
      // the exact production glyphs for each recipient's full identity (id and name pick a worker's colour and silhouette)
      const parity = await page.evaluate(() => {
        const mk = (id, name, role, p) => { const d = document.createElement('div'); d.innerHTML = agentGlyph({...p, agent_id: id, display_name: name, role}, 25); return d.innerHTML; };
        const rows = [...document.querySelectorAll('.gchat-msg[data-id="id3"] .gchat-recipient')].map(n => n.innerHTML);
        const op = {department: 'operations', icon: 'brain'};
        return [rows[0] === mk('r-atlas', 'Atlas', {id: 'global-worker'}, op), rows[1] === mk('r-scout', 'Scout', {id: 'global-worker'}, {department: 'marketing', icon: 'circle'}),
          rows[2] === mk('r-evil', '<img src=x onerror="window.pwned=1">', {id: 'founder'}, {}),
          mk('r-atlas', 'Atlas', {id: 'global-worker'}, op) !== mk('r-other', 'Other', {id: 'global-worker'}, op)];
      });
      assert.deepEqual(parity, [true, true, true, true]);
      assert.equal(await row('id1').locator('svg').count(), 1);
      assert.notEqual(await row('id3').locator('svg').nth(0).innerHTML(), await row('id3').locator('svg').nth(1).innerHTML(), 'each recipient has its own identity glyph');
      const look = await page.locator('.gchat-msg[data-id="id1"]').evaluate(node => {
        const body = node.querySelector('.gchat-body'), c = getComputedStyle(body), m = getComputedStyle(node.querySelector('.gchat-marker')), r = node.getBoundingClientRect(), host = node.parentElement.getBoundingClientRect();
        const svg = node.querySelector('svg').getBoundingClientRect();
        return {border: c.borderTopWidth, mborder: m.borderTopWidth, bg: c.backgroundColor, markerBg: m.backgroundColor, centre: Math.abs((r.left + r.right) / 2 - (host.left + host.right) / 2), svg: svg.width};
      });
      assert.equal(look.border, '0px'); assert.equal(look.mborder, '0px');
      assert.equal(look.bg, 'rgba(0, 0, 0, 0)'); assert.equal(look.markerBg, 'rgba(0, 0, 0, 0)');
      assert.ok(look.centre < 24, 'centered in the log, allowing for the scrollbar');
      assert.equal(look.svg, 25);
      assert.equal(await page.locator('.gchat-msg[data-id="id1"] button').count(), 0, 'no clicks, drawer or action is added');
      assert.equal(await page.locator('.gchat-msg[data-id="id2"] .gchat-agent-message').count(), 0);
      // replay, newer arrival and history do not double count
      await changed({entries});
      await changed({entries: [...entries, sent(10, scout)]});
      await page.waitForFunction(() => document.querySelector('.gchat-msg[data-id="id9"] .gchat-agent-message')?.innerText.includes('2 Bots'));
      assert.equal((await row('id9').innerText()).replace(/\s+/g, ' ').trim(), 'Messaged 2 Bots');
      assert.equal((await row('id3').innerText()).replace(/\s+/g, ' ').trim(), 'Messaged 3 Bots');
      await mount(base([...entries, sent(10, scout)], {older: 100}));
      await page.evaluate(() => { window.script.history = {entries: [{...window.EV(0), position: 50, id: 'id0', speaker: 'notice', agent_message: {message_ids: ['m0'], recipients: [{agent_id: 'r-scout', display_name: 'Scout', role: {id: 'global-worker'}, presentation: {}}]}}], older: null, rendered_boundary: 0}; });
      await page.locator('.gchat-older').click();
      await page.waitForSelector('[data-id="id0"]');
      assert.deepEqual((await order()).slice(0, 2), ['id0', 'id2'], 'an older send adjacent to the first row joins it under the older key');
      assert.equal((await row('id0').innerText()).replace(/\s+/g, ' ').trim(), 'Messaged 2 Bots', 'Atlas + Scout, no double count');
      // a different agent never inherits these groups
      await mount(base([sent(1, scout), ent(2, 'owner', 'other conversation')], {agent_id: 'agent-b'}), 'agent-b', 'Other');
      assert.equal((await row('id1').innerText()).replace(/\s+/g, ' ').trim(), 'Messaged');
      assert.equal(await page.locator('.gchat-agent-message').count(), 1);
      // a send arriving in a merged position never reads as a completed reply
      assert.equal(await page.locator('.gchat-agent .gchat-body').count(), 0);
    });
    await check('every notice style is borderless centered muted text; unverified, delivery states and actions stay visible; layout holds at 320px', async () => {
      const notice = (n, text, system) => ({...ent(n, 'notice', text), speaker: 'notice', system});
      const worker = (id, name) => ({agent_id: id, display_name: name, role: {id: 'global-worker'}, presentation: {department: 'operations', icon: 'brain'}});
      const sent = (n, ...recipients) => ({...ent(n, 'notice', ''), speaker: 'notice', agent_message: {message_ids: ['m' + n], recipients}});
      const failed = {id: 'in-f', kind: 'input', sequence: 1, origin: 'system', input_type: 'broadcast', state: 'uncertain', reason: null, body: 'Broadcast body kept readable', session_id: 's1', evidence: {}, created_at: '2026-10-04T21:25:00Z', settled_at: null};
      await mount(base([
        ent(1, 'owner', 'hello'), ent(2, 'agent', 'reply', {seq: 1}),
        notice(3, 'You have 2 new inbox items.', {input_id: 'in-n1', input_type: 'inbox_nudge', verified: true}),
        notice(4, 'You have 9 new inbox items.', {input_id: null, input_type: 'inbox_nudge', verified: false, reason: 'no_matching_input'}),
        notice(5, 'Your session was recovered.', {input_id: 'in-r1', input_type: 'bootstrap_recovery', verified: true}),
        notice(6, 'Plain system text.', null),
        sent(7, worker('3f6c2a10-1111-4a5b-8c1d-aaaaaaaaaaa1', 'Atlas'), worker('9be04d77-2222-4c3e-9f2a-bbbbbbbbbbb2', 'Scout'), worker('c81a5e39-3333-4d7f-a0b3-ccccccccccc5', 'Harbor')),
      ].map((e, i) => i === 6 ? e : e), {inputs: [failed]}));
      const styles = await page.locator('.gchat-notice').evaluateAll(nodes => nodes.map(node => {
        const body = node.querySelector('.gchat-body'), c = getComputedStyle(body);
        const marker = node.querySelector('.gchat-marker');
        return {id: node.dataset.id || node.dataset.inputId, border: c.borderTopWidth + c.borderLeftWidth, bg: c.backgroundColor, align: c.textAlign, markerBorder: marker ? getComputedStyle(marker).borderTopWidth : '0px',
          markerBg: marker ? getComputedStyle(marker).backgroundColor : 'rgba(0, 0, 0, 0)', color: getComputedStyle(marker || body).color};
      }));
      assert.ok(styles.length >= 6, 'nudge, unverified, recovery, plain, outgoing and a pending notice');
      for (const row of styles) { assert.equal(row.border, '0px0px', row.id); assert.equal(row.bg, 'rgba(0, 0, 0, 0)', row.id); assert.equal(row.markerBorder, '0px', row.id); assert.equal(row.markerBg, 'rgba(0, 0, 0, 0)', row.id); assert.equal(row.align, 'center', row.id); }
      const glyphs = await page.locator('.gchat-msg[data-id="id7"] .gchat-recipient svg').evaluateAll(n => n.map(x => x.outerHTML));
      assert.equal(new Set(glyphs).size, 3, 'workers with identical presentation keep distinct real-identity glyphs');
      assert.equal(await page.locator('.gchat-msg[data-id="id4"] .gchat-marker').innerText(), 'Unverified notice');
      assert.equal(await page.locator('.gchat-msg[data-id="id4"] .gchat-text').innerText(), 'You have 9 new inbox items.', 'an unverified notice keeps its body');
      assert.equal(await page.locator('.gchat-msg[data-id="id6"] .gchat-text').innerText(), 'Plain system text.');
      const pending = page.locator('.gchat-pending[data-input-id="in-f"]');
      assert.equal(await pending.locator('.gchat-text').innerText(), 'Broadcast body kept readable');
      assert.equal(await pending.locator('.gchat-state').innerText(), 'Delivery unconfirmed');
      assert.equal(await pending.locator('[data-dismiss]').count(), 1, 'delivery actions stay');
      assert.equal(await page.locator('.gchat-msg[data-id="id1"] .gchat-body').evaluate(n => getComputedStyle(n).backgroundColor), 'rgb(18, 107, 212)', 'user bubbles are unchanged');
      for (const width of [760, 320]) {
        await page.setViewportSize({width: width + 20, height: 700});
        await page.evaluate(w => { document.querySelector('#pane').style.width = w + 'px'; }, width);
        const fit = await page.evaluate(() => { const log = document.querySelector('.gchat-scroll'); return {x: log.scrollWidth - log.clientWidth, page: document.documentElement.scrollWidth - document.documentElement.clientWidth}; });
        assert.ok(fit.x <= 0 && fit.page === 0, `no horizontal overflow at ${width}px`);
      }
      if (process.env.CHAT_SHOT_DIR) {
        await page.addStyleTag({path: path.join(root, 'app.css')});
        for (const [name, width] of [['desktop', 760], ['narrow', 320]]) {
          await page.setViewportSize({width: width + 20, height: 700});
          await page.evaluate(w => { document.body.style.margin = '0'; document.querySelector('#pane').style.width = w + 'px'; document.querySelector('#pane').style.height = '620px'; }, width);
          await page.locator('#pane').screenshot({path: `${process.env.CHAT_SHOT_DIR}/chat-system-markers-${name}.png`});
        }
        await page.evaluate(() => { document.querySelector('#pane').style.height = '520px'; });
      }
      await page.setViewportSize({width: 1000, height: 800});
      await page.evaluate(() => { document.querySelector('#pane').style.width = '760px'; });
    });
    await check('an input receipt is not shown twice: a verified notice absorbs its delivery record', async () => {
      const nudge = {...ent(3, 'notice', 'New inbox item.'), speaker: 'notice', system: {input_id: 'in-n1', input_type: 'inbox_nudge', verified: true}};
      const input = (id, state, extra = {}) => ({id, kind: 'input', sequence: 1, origin: 'system', input_type: 'inbox_nudge', state, reason: null, body: 'New inbox item.', session_id: 's1', evidence: {}, created_at: '2026-10-04T21:25:00Z', settled_at: null, ...extra});
      await mount(base([nudge], {inputs: [input('in-n1', 'submitting'), input('in-n2', 'waiting', {body: 'Another nudge'})]}));
      assert.equal(await page.locator('.gchat-text', {hasText: 'New inbox item.'}).count(), 0, 'the expanded nudge text is not printed');
      assert.equal(await page.locator('.gchat-msg .gchat-marker', {hasText: 'Inbox nudge'}).count(), 1, 'only the verified transcript entry is shown');
      assert.equal(await page.locator('.gchat-pending .gchat-text').count(), 0);
      assert.equal(await page.locator('.gchat-pending').count(), 0, 'nudge transport receipts are not owner-facing');
    });
    await check('backend nudge receipts never reappear after refresh or remount; owner delivery failures stay visible', async () => {
      const inputs = ['waiting', 'submitting', 'submitted', 'uncertain', 'failed', 'undelivered'].map((state, i) => ({
        id: 'internal-' + i, kind: 'input', sequence: i + 1, origin: 'system', input_type: 'inbox_nudge', state,
        body: 'Check the inbox.', binding: binding(), reason: 'paste_unverified', session_id: 's1', evidence: {},
      }));
      inputs.push({id: 'my-message', kind: 'input', sequence: 9, origin: 'owner', input_type: 'chat', state: 'uncertain',
        body: 'My request', binding: binding(), session_id: 's1', evidence: {}});
      const assertVisible = async () => {
        assert.equal(await page.locator('.gchat-pending').count(), 1);
        assert.equal(await page.locator('.gchat-pending').getAttribute('data-input-id'), 'my-message');
        assert.equal(await page.locator('.gchat-pending .gchat-state').innerText(), 'Delivery unconfirmed');
        assert.equal(await page.locator('.gchat-marker').count(), 0);
      };
      await mount(base([], {inputs}));
      await assertVisible();
      await changed({inputs});
      await assertVisible();
      await page.evaluate(() => GLOBAL_CHAT_HIDDEN.clear());
      await mount(base([], {inputs}));
      await assertVisible();
      assert.equal((await posts('/messages')).length, 0, 'hiding bookkeeping never resends or acknowledges a nudge');
    });
    await check('the file picker queues locally; Send copies first and delivers a clickable reference, not file contents', async () => {
      await mount(base([]));
      await page.locator('.gchat-composer input[type="file"]').setInputFiles({name: 'launch notes.txt', mimeType: 'text/plain', buffer: Buffer.from('PRIVATE FILE BYTES')});
      assert.match(await page.locator('.gchat-file-choice').innerText(), /launch notes.txt/);
      assert.equal((await posts('/attachments')).length, 0, 'picking does not upload');
      const sendBounds = await page.locator('.gchat-send').boundingBox(), attachBounds = await page.getByRole('button', {name: 'Attach files', exact: true}).boundingBox();
      assert.ok(attachBounds.x < sendBounds.x, 'attachment button is to the left of Send');
      await page.locator('.gchat-send').click();
      await page.waitForSelector('.gchat-pending');
      const message = (await posts('/messages')).at(-1).body.text;
      assert.match(message, /operations\/agents\/ordubashy\/downloads\/upload-1\/launch notes.txt/);
      assert.ok(!message.includes('PRIVATE FILE BYTES'));
      assert.deepEqual(await page.evaluate(() => window.calls.filter(c => c.method === 'POST').map(c => c.url.split('/').at(-1))), ['attachments', 'messages']);
      assert.equal(await page.locator('.gchat-file-choice').count(), 0);
      await page.getByRole('button', {name: 'Preview launch notes.txt', exact: true}).click();
      assert.match(await page.locator('.gchat-preview-content iframe').getAttribute('src'), /\/files\/view\?path=/);
    });
    await check('pasted images queue locally and send through the same attachment path', async () => {
      await mount(base([]));
      await box().evaluate(el => {
        const data = new DataTransfer(); data.items.add(new File(['clipboard bytes'], 'image.png', {type:'image/png'}));
        el.dispatchEvent(new ClipboardEvent('paste', {bubbles:true, cancelable:true, clipboardData:data}));
      });
      assert.match(await page.locator('.gchat-file-choice').innerText(), /image.png/);
      assert.equal((await posts('/attachments')).length, 0);
      await page.locator('.gchat-send').click();
      await page.waitForSelector('.gchat-pending');
      assert.equal((await posts('/attachments')).length, 1);
      assert.match((await posts('/messages')).at(-1).body.text, /downloads\/upload-1\/image.png/);
      assert.equal(await page.getByRole('button', {name:'Preview image.png',exact:true}).count(), 1);
    });
    await check('dragged files stay with their agent and can be removed before Send', async () => {
      await mount(base([]));
      await page.evaluate(() => {
        const transfer = new DataTransfer(); transfer.items.add(new File(['hello'], 'dropped.txt'));
        document.querySelector('.gchat-scroll').dispatchEvent(new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer:transfer}));
      });
      assert.equal(await page.locator('.gchat-file-choice').count(), 1);
      await mount(base([]), 'another-agent', 'Founder');
      assert.equal(await page.locator('.gchat-file-choice').count(), 0);
      await mount(base([]));
      assert.match(await page.locator('.gchat-file-choice').innerText(), /dropped.txt/);
      await page.getByRole('button', {name: 'Remove dropped.txt', exact: true}).click();
      assert.equal(await page.locator('.gchat-file-choice').count(), 0);
      assert.equal((await posts('/attachments')).length, 0);
    });
    await check('an unconfirmed message retries with its original uploaded copy and message key', async () => {
      await mount(base([]));
      await page.locator('.gchat-composer input[type="file"]').setInputFiles({name: 'retry.txt', mimeType: 'text/plain', buffer: Buffer.from('hello')});
      await page.evaluate(() => { window.script.loseResponse = true; });
      await page.locator('.gchat-send').click();
      await page.waitForSelector('.gchat-banner:not([hidden])');
      await page.evaluate(() => { window.script.loseResponse = false; });
      await page.locator('.gchat-send').click();
      await page.waitForSelector('.gchat-pending');
      assert.equal((await posts('/attachments')).length, 1);
      const messages = await posts('/messages');
      assert.equal(messages.length, 2);
      assert.equal(messages[0].body.idempotency_key, messages[1].body.idempotency_key);
      assert.equal(messages[0].body.text, messages[1].body.text);
    });
    await check('session status: live, working, starting capture, capture disabled and offline', async () => {
      await mount(base([], {session: session({working: true})}));
      assert.equal(await page.locator('.gchat-top .identity-avatar').getAttribute('aria-label'), 'Marketing');
      assert.equal(await page.locator('.gchat-top').innerText(), '', 'the header shows only the agent icon');
      assert.equal(await page.locator('.gchat-status, .gchat-name').count(), 0);
      assert.equal(await sessionText(), '');
      assert.equal(await box().isEnabled(), true);
      assert.equal(await page.locator('.gchat-session').getAttribute('data-state'), 'working');
      assert.equal(await page.locator('.gchat-working-tag').isVisible(), true);
      assert.equal(await page.locator('.gchat-working-tag').getAttribute('aria-label'), 'Marketing is working');
      const workingStyle = await page.locator('.gchat-working-tag').evaluate(el => {
        const style = getComputedStyle(el), parent = getComputedStyle(el.parentElement);
        return [style.borderTopWidth, style.backgroundColor, parent.paddingTop, parent.paddingBottom,
          el.getBoundingClientRect().left - document.querySelector('.gchat-composer').getBoundingClientRect().left];
      });
      assert.deepEqual(workingStyle, ['0px', 'rgba(0, 0, 0, 0)', '4px', '4px', 0]);
      assert.equal(await page.locator('.gchat-working-dots i').count(), 3);
      await page.emulateMedia({reducedMotion: 'reduce'});
      assert.equal(await page.locator('.gchat-working-dots i').first().evaluate(node => getComputedStyle(node).animationName), 'none');
      await page.emulateMedia({reducedMotion: 'no-preference'});
      if (process.env.CHAT_SHOT_DIR) {
        const sheet = await page.addStyleTag({path: path.join(root, 'app.css')});
        await page.locator('#pane').screenshot({path: `${process.env.CHAT_SHOT_DIR}/chat-working.png`});
        await sheet.evaluate(node => node.remove());
      }
      await page.evaluate(() => window.sources.at(-1).fail(false));
      assert.equal(await page.locator('.gchat-working-tag').isVisible(), false, 'a disconnected stream must not imply active work');
      await page.evaluate(() => window.sources.at(-1).emit('open', {}));
      await changed({session: session()});
      assert.equal(await sessionText(), '', 'no line repeating the status chip'); assert.equal(await page.locator('.gchat-session').isHidden(), true);
      assert.equal(await page.locator('.gchat-top').innerText(), '', 'live status never becomes header text');
      await changed({session: session({reason: 'capture_not_ready', can_send: false})});
      assert.equal(await page.locator('.gchat-session').getAttribute('data-state'), 'starting');
      assert.equal(await box().isDisabled(), true);
      assert.match(await page.locator('.gchat-empty').innerText(), /Connecting/);
      await changed({session: session()});
      assert.equal(await box().isEnabled(), true, 'the composer enables itself when capture becomes ready');
      await changed({session: session({reason: 'capture_disabled', can_send: false})});
      assert.equal(await page.locator('.gchat-session').getAttribute('data-state'), 'disabled');
      assert.equal(await box().isDisabled(), true);
      await changed({session: session({state: 'offline', reason: 'no_session', can_send: false, transport: null})});
      assert.match(await sessionText(), /Start or resume Marketing in Terminal to send/);
      assert.equal(await box().isDisabled(), true);
      assert.equal(await page.locator('.gchat-empty [data-empty-action="terminal"]').count(), 1);
      assert.equal(await page.evaluate(() => window.terminalOpened || 0), 0, 'Chat never opens or starts the terminal by itself');
      await page.locator('.gchat-empty [data-empty-action="terminal"]').click();
      assert.equal(await page.evaluate(() => window.terminalOpened), 1, 'only the owner opens it');
      assert.equal(await page.evaluate(() => window.calls.filter(c => c.method === 'POST').length), 0);
    });
    await check('Enter sends once with a key; a lost response retries the same key and shows the message as pending', async () => {
      await mount(base([]));
      await box().fill('first line');
      await box().press('Shift+Enter');
      assert.equal(await box().inputValue(), 'first line\n');
      await box().type('second');
      await page.evaluate(() => { window.script.postError = {status: 503, error: {code: 'unavailable', message: 'Service restarting'}}; });
      await box().press('Enter');
      await page.waitForSelector('.gchat-banner:not([hidden])');
      assert.equal((await page.locator('.gchat-banner').innerText()).trim(), "Couldn't confirm. Try again");
      assert.equal(await box().inputValue(), 'first line\nsecond');
      await page.evaluate(() => { window.script.postError = null; });
      await box().press('Enter');
      await page.waitForSelector('.gchat-pending[data-state="waiting"]');
      const sent = await posts('/messages');
      assert.equal(sent.length, 2);
      assert.equal(sent[0].body.idempotency_key, sent[1].body.idempotency_key, 'an ambiguous retry reuses its key');
      assert.equal(sent[1].body.text, 'first line\nsecond');
      assert.equal(await box().inputValue(), '');
      assert.match(await page.locator('.gchat-pending .gchat-state').innerText(), /^Waiting$/);
      assert.equal(await page.locator('.gchat-pending .gchat-marker').count(), 0);
      await box().fill('first line\nsecond');
      await box().press('Enter');
      const third = await posts('/messages');
      assert.notEqual(third.at(-1).body.idempotency_key, sent[0].body.idempotency_key, 'a deliberate new send gets a new key');
    });
    await check('refusals are shown truthfully, keep the text and never reuse a key', async () => {
      await mount(base([]));
      const refuse = async (code, expected) => {
        await page.evaluate(code => { window.script.postError = {status: 422, error: {code, message: 'server ' + code}}; }, code);
        await page.evaluate(() => { document.querySelector('.gchat-banner').hidden = true; });
        await box().fill('please do this');
        await box().press('Enter');
        await page.waitForFunction(() => !document.querySelector('.gchat-banner').hidden);
        assert.match(await page.locator('.gchat-banner').innerText(), expected, code);
        assert.equal(await box().inputValue(), 'please do this');
        assert.equal(await page.locator('.gchat-pending').count(), 0, 'a refusal stores no input');
      };
      await refuse('terminal_offline', /Not running\. Start or resume it in Terminal/);
      await refuse('capture_not_ready', /^Connecting\. Try again in a moment\.$/);
      await refuse('capture_disabled', /^Chat unavailable\.$/);
      await refuse('idempotency_conflict', /^Couldn't send\. Try again$/);
      const keys = (await posts('/messages')).map(call => call.body.idempotency_key);
      assert.equal(new Set(keys).size, keys.length);
      assert.equal(await page.evaluate(() => window.terminalOpened || 0), 0);
    });
    await check('delivery states: uncertain is never retyped; undelivered resend is an explicit owner action with its own key', async () => {
      const input = (id, state, extra = {}) => ({id, kind: 'input', sequence: 1, origin: 'owner', input_type: 'chat', state, reason: null, body: 'text of ' + id, session_id: 's1', evidence: {}, created_at: '2026-10-04T21:25:00Z', settled_at: null, ...extra});
      await mount(base([], {inputs: [input('in-u', 'undelivered'), input('in-x', 'uncertain'), input('in-f', 'failed', {reason: 'refused by transport'}), input('in-s', 'submitted')]}));
      assert.equal(await page.locator('.gchat-pending').count(), 3, 'a submitted input is confirmed by the transcript, not shown');
      assert.match(await page.locator('[data-input-id="in-x"] .gchat-state').innerText(), /^Delivery unconfirmed$/);
      assert.equal(await page.locator('[data-input-id="in-x"] [data-resend]').count(), 0, 'no automatic or offered resend of an uncertain input');
      assert.match(await page.locator('[data-input-id="in-u"] .gchat-state').innerText(), /^Not delivered$/);
      assert.doesNotMatch(await page.locator('[data-input-id="in-f"] .gchat-state').innerText(), /refused by transport/);
      assert.match(await page.locator('[data-input-id="in-f"] .gchat-state').innerText(), /Not delivered/);
      await page.waitForTimeout(600);
      assert.equal((await posts('/messages')).length, 0, 'nothing is resent automatically');
      await page.locator('[data-input-id="in-u"] [data-resend]').click();
      await page.waitForFunction(() => !document.querySelector('[data-input-id="in-u"]'));
      const resent = await posts('/messages');
      assert.equal(resent.length, 1);
      assert.equal(resent[0].body.text, 'text of in-u');
      await page.locator('[data-input-id="in-x"] [data-dismiss]').click();
      assert.equal(await page.locator('[data-input-id="in-x"]').count(), 0);
    });
    await check('a resend lost in transit reuses its key', async () => {
      const input = {id: 'in-lost', kind: 'input', sequence: 1, origin: 'owner', input_type: 'chat', state: 'undelivered', reason: null, body: 'again please', session_id: 's1', evidence: {}, created_at: '2026-10-04T21:25:00Z', settled_at: null};
      await mount(base([], {inputs: [input]}));
      await page.evaluate(() => { window.script.postError = {status: 503, error: {code: 'unavailable', message: 'down'}}; });
      await page.locator('[data-resend]').click();
      await page.waitForFunction(() => !document.querySelector('.gchat-banner').hidden);
      assert.equal((await page.locator('.gchat-banner').innerText()).trim(), "Couldn't confirm. Try again");
      await page.evaluate(() => { window.script.postError = null; });
      await page.locator('[data-resend]').click();
      await page.waitForFunction(() => !document.querySelector('[data-input-id="in-lost"]'));
      const sent = await posts('/messages');
      assert.equal(sent.length, 2);
      assert.equal(sent[0].body.idempotency_key, sent[1].body.idempotency_key);
    });
    await check('a busy agent still takes the message through the same API; it waits as pending until typed', async () => {
      await mount(base([], {session: session({working: true})}));
      await box().fill('while you work');
      await box().press('Enter');
      await page.waitForSelector('.gchat-pending[data-state="waiting"]');
      assert.equal((await posts('/messages')).length, 1);
      await changed({inputs: [{id: 'in-0', kind: 'input', sequence: 1, origin: 'owner', input_type: 'chat', state: 'submitted', reason: null, body: null, session_id: 's1', evidence: {}, created_at: '2026-10-04T21:25:00Z', settled_at: '2026-10-04T21:25:05Z'}],
        entries: [ent(1, 'owner', 'while you work')]});
      await page.waitForFunction(() => !document.querySelector('.gchat-pending'));
      assert.deepEqual(await order(), ['id1']);
    });
    await check('draft and unconfirmed send are bound to the exact agent and conversation, and survive switching', async () => {
      await mount(base([], {agent_id: 'agent-a'}), 'agent-a', 'Marketing');
      await box().fill('half written');
      await mount({...base([], {agent_id: 'agent-b'}), name: 'Dev'}, 'agent-b', 'Dev');
      assert.equal(await box().inputValue(), '', 'another agent never sees the draft');
      await box().fill('dev draft');
      await mount(base([]), 'agent-a', 'Marketing');
      assert.equal(await box().inputValue(), 'half written');
      // an unconfirmed send keeps its key across a switch away and back
      await page.evaluate(() => { window.script.postError = {status: 503, error: {code: 'unavailable', message: 'Lost response'}}; });
      await box().press('Enter');
      await page.waitForFunction(() => document.querySelector('.gchat-banner').textContent.includes("Couldn't confirm"));
      const key = (await posts('/messages')).at(-1).body.idempotency_key;
      await mount(base([]), 'agent-b', 'Dev');
      assert.equal(await box().inputValue(), 'dev draft');
      await mount(base([]), 'agent-a', 'Marketing');
      assert.equal(await box().inputValue(), 'half written');
      await page.evaluate(() => { window.script.postError = null; });
      await box().press('Enter');
      await page.waitForFunction(() => document.querySelector('.gchat-composer textarea').value === '');
      assert.equal((await posts('/messages')).at(-1).body.idempotency_key, key);
      // a different conversation of the same agent has its own composer
      const two = base([], {conversations: [{conversation_id: 'conv-2', provider: 'claude', updated_at: '2026-10-05T00:00:00Z', current: true}, {conversation_id: 'conv-1', provider: 'claude', updated_at: '2026-10-04T00:00:00Z', current: false}], conversation: conv('conv-2'), session: session({binding: binding({binding_id: 'b2', conversation_id: 'conv-2'})})});
      await mount(two, 'agent-a');
      assert.equal(await box().inputValue(), '', 'conv-2 does not receive the conv-1 draft');
    });
    await check('a send still in flight survives switching agents and views and shows its status on return', async () => {
      await mount(base([]), 'agent-a', 'Marketing');
      const admitted = await page.evaluate(() => window.script.inputs.size);
      await page.evaluate(() => { window.script.hold = new Promise(resolve => { window.releasePost = resolve; }); });
      await box().fill('slow one');
      await box().press('Enter');
      await page.waitForFunction(() => document.querySelector('.gchat-send').disabled);
      await mount(base([]), 'agent-b', 'Dev');
      await mount(base([]), 'agent-a', 'Marketing');
      assert.equal(await page.locator('.gchat-send').isDisabled(), true, 'the in-flight send is still shown as busy');
      assert.equal(await box().inputValue(), 'slow one');
      await page.evaluate(() => window.releasePost());
      await page.waitForFunction(() => document.querySelector('.gchat-composer textarea').value === '' && !document.querySelector('.gchat-send').disabled);
      assert.equal(await page.evaluate(() => window.script.inputs.size), admitted + 1, 'exactly one admitted input, no duplicate from switching');
      await page.waitForSelector('.gchat-pending[data-state="waiting"]');
    });
    const twoConversations = (extra = {}) => base([ent(1, 'owner', 'live convo')], {conversations: [{conversation_id: 'conv-2', provider: 'codex', updated_at: '2026-10-05T00:00:00Z', current: true}, {conversation_id: 'conv-1', provider: 'claude', updated_at: '2026-10-04T00:00:00Z', current: false}],
      conversation: {...conv('conv-2'), provider: 'codex'}, session: session({provider: 'codex', conversation_id: 'conv-2', binding: binding({binding_id: 'b2', provider: 'codex', conversation_id: 'conv-2'})}), ...extra});
    await check('Chat follows the current conversation without offering a history selector', async () => {
      await mount(twoConversations());
      assert.equal(await page.getByRole('combobox', {name: 'Conversation', exact: true}).count(), 0);
      assert.equal(await page.getByRole('button', {name: 'Back to current', exact: true}).count(), 0);
      assert.equal(await box().isEnabled(), true);
      assert.deepEqual(await order(), ['id1']);
      assert.deepEqual(await page.evaluate(() => window.script.queries.at(-1)), {limit: '50'});
      assert.equal((await posts('/messages')).length, 0);
    });
    await check('the composer is enabled only for the exact live provider/conversation binding', async () => {
      // Same conversation id under another provider is not the live session.
      await mount(base([ent(1, 'owner', 'x')], {session: session({binding: binding({provider: 'codex'})})}));
      assert.equal(await box().isDisabled(), true);
      await changed({session: session({binding: null, can_send: false, reason: 'binding_not_ready'})});
      assert.equal(await box().isDisabled(), true);
      assert.equal(await page.locator('.gchat-session').getAttribute('data-state'), 'starting');
      await changed({session: session()});
      assert.equal(await box().isEnabled(), true);
    });
    await check('the first paint accepts no draft and an unknown pre-load conversation has no draft key', async () => {
      await page.evaluate(() => { window.script.states['agent-a'] = null; });
      const state = base([]);
      await page.evaluate(state => {
        window.chat?.dispose(); window.sources.length = 0;
        let release; window.script.states['agent-a'] = state;
        const real = window.fetch; window.fetch = (url, o) => url.includes('/chat?') ? new Promise(r => { release = () => r(real(url, o)); }) : real(url, o);
        window.chat = mountGlobalChat(document.querySelector('#pane'), {agent_id: 'agent-a', display_name: 'Marketing'}, {});
        window.releaseState = () => { release(); window.fetch = real; };
      }, state);
      assert.equal(await box().isDisabled(), true);
      assert.equal(await page.locator('.gchat-send').isDisabled(), true);
      await page.evaluate(() => window.releaseState());
      await page.waitForFunction(() => !document.querySelector('.gchat-composer textarea').disabled);
    });
    await check('receipts follow their own binding after a fresh load, in sequence order, across ended sessions', async () => {
      const input = (id, sequence, extra = {}) => ({id, kind: 'input', sequence, origin: 'owner', input_type: 'chat', state: 'undelivered', reason: null, body: 'text ' + id, session_id: 's0', evidence: {}, created_at: '2026-10-04T21:25:00Z', settled_at: null, idempotency_key: 'k-' + id, ...extra});
      const old = binding({binding_id: 'b0', generation: 0});            // an ended session of the same conversation
      const other = binding({binding_id: 'bx', provider: 'codex', conversation_id: 'conv-9'});
      // actual P2 order: descending by sequence in the contract; the UI must not depend on it
      await mount(base([], {inputs: [input('in-3', 3, {binding: binding()}), input('in-1', 1, {binding: old}), input('in-2', 2, {binding: other}), input('in-4', 4, {binding: null}), input('in-5', 5, {binding: binding({conversation_id: 'conv-1', provider: 'codex'})})]}));
      assert.deepEqual(await page.locator('.gchat-pending').evaluateAll(nodes => nodes.map(n => n.dataset.inputId)), ['in-1', 'in-3'], 'only exact provider/conversation matches, ascending by sequence');
    });
    await check('only definitely undelivered owner Chat text can be offered again; other producers are status only', async () => {
      const input = (id, extra = {}) => ({id, kind: 'input', sequence: Number(id.slice(-1)), origin: 'owner', input_type: 'chat', state: 'undelivered', reason: null, body: 'text ' + id, session_id: 's0', evidence: {}, created_at: '2026-10-04T21:25:00Z', settled_at: null, ...extra});
      await mount(base([], {inputs: [input('n-1', {origin: 'system', input_type: 'inbox_nudge'}), input('n-2', {input_type: 'broadcast'}), input('n-3', {input_type: 'telegram', body: null}), input('n-4', {state: 'uncertain'}), input('n-5', {state: 'failed'}), input('n-6', {origin: 'system', input_type: 'bootstrap_recovery'})]}));
      for (const id of ['n-1', 'n-2', 'n-3', 'n-4', 'n-6']) {
        assert.equal(await page.locator(`[data-input-id="${id}"] [data-resend], [data-input-id="${id}"] [data-reuse]`).count(), 0, id);
      }
      assert.equal(await page.locator('[data-input-id="n-5"] [data-resend]').count(), 1);
      assert.equal(await page.locator('[data-input-id="n-1"]').count(), 0, 'internal inbox delivery stays in the backend');
      assert.equal(await page.locator('[data-input-id="n-6"] .gchat-marker').innerText(), 'Session recovery', 'a pending recovery input is a system marker and is never owner text');
    });
    await check('undelivered messages from another conversation stay out of the current Chat', async () => {
      const lost = {id: 'old-1', kind: 'input', sequence: 1, origin: 'owner', input_type: 'chat', state: 'undelivered', reason: null, body: 'lost text', session_id: 's0', evidence: {}, created_at: '2026-10-04T21:25:00Z', settled_at: null,
        binding: {binding_id: 'b0', agent_id: 'agent-a', provider: 'claude', conversation_id: 'conv-1', generation: 0, transport: 'terminal'}, idempotency_key: 'k-old'};
      await mount(twoConversations({inputs: [lost]}));
      await box().fill('existing draft');
      assert.equal(await page.locator('[data-input-id="old-1"]').count(), 0);
      assert.equal(await page.locator('[data-resend], [data-reuse]').count(), 0);
      assert.equal(await box().inputValue(), 'existing draft');
      assert.equal((await posts('/messages')).length, 0);
    });
    await check('send pins the observed binding; an ambiguous retry keeps its key, text and original pin', async () => {
      await mount(base([]));
      await page.evaluate(() => { window.script.loseResponse = true; });
      await box().fill('pinned text');
      await box().press('Enter');
      await page.waitForFunction(() => document.querySelector('.gchat-banner').textContent.includes("Couldn't confirm"));
      // the live session moves to a new generation of the same conversation before the retry
      await page.evaluate(() => { window.script.loseResponse = false; });
      await changed({session: session({binding: binding({binding_id: 'b9', generation: 2})})});
      await page.evaluate(() => { window.script.live = {binding_id: 'b9', provider: 'claude', conversation_id: 'conv-1', generation: 2}; });
      await box().press('Enter');
      const sent = await posts('/messages');
      assert.equal(sent[0].body.expected_binding, 'b1');
      assert.equal(sent[1].body.expected_binding, 'b1', 'the pin is never refreshed under an existing ambiguous key');
      assert.equal(sent[0].body.idempotency_key, sent[1].body.idempotency_key);
      // a deliberate new send takes the new generation's binding
      await page.waitForFunction(() => document.querySelector('.gchat-composer textarea').value === '');
      await box().fill('a fresh one');
      await box().press('Enter');
      await page.waitForFunction(() => window.script.inputs.size >= 2);
      assert.equal((await posts('/messages')).at(-1).body.expected_binding, 'b9');
    });
    await check('a stale binding is refused as terminal_changed: the text and draft stay and nothing is moved', async () => {
      await mount(base([]));
      await page.evaluate(() => { window.script.live = {binding_id: 'b-new', provider: 'claude', conversation_id: 'conv-1', generation: 5}; });
      await box().fill('keep me');
      await box().press('Enter');
      await page.waitForFunction(() => !document.querySelector('.gchat-banner').hidden);
      assert.match(await page.locator('.gchat-banner').innerText(), /^Not delivered\. Check the agent/);
      assert.equal(await box().inputValue(), 'keep me');
      assert.equal(await page.locator('.gchat-pending').count(), 0);
    });
    await check('a reset during a held state fetch is not reduced to an ordinary reload', async () => {
      await mount(base([withSeq(1, 'old', 8)], {cursor: 90, revision: 90, last_read_sequence: 8, newest_sequence: 8}));
      await page.waitForFunction(() => window.script.states['agent-a'].last_read_sequence === 8);
      // hold the next state fetch, emit a "changed" (in flight), then a reset restoring a lower cursor and read position
      await page.evaluate(() => {
        const real = window.fetch; let first = true;
        window.fetch = (url, o) => { const hold = url.includes('/chat?') && first; first = false; return hold ? new Promise(r => { window.releaseOld = () => r(real(url, o)); }) : real(url, o); };
        window.restoreFetch = () => { window.fetch = real; };
      });
      await page.evaluate(() => window.sources.at(-1).emit('chat', {cursor: 91, kind: 'changed', data: {}}));
      await page.evaluate(() => { window.script.states['agent-a'] = {...window.script.states['agent-a'], cursor: 5, revision: 5, last_read_sequence: 0, newest_sequence: 1, entries: [{...window.EV(1), id: 'restored', text: 'restored', completion_sequence: 1, completion_id: 'c1', speaker: 'agent'}]}; });
      await page.evaluate(() => window.sources.at(-1).emit('reset', {}));
      await page.waitForSelector('[data-id="restored"]');
      await page.evaluate(() => window.releaseOld());            // the obsolete response (high cursor, old entries) arrives late
      await page.waitForTimeout(150);
      assert.deepEqual(await order(), ['restored'], 'the old page does not come back');
      await page.evaluate(() => window.restoreFetch());
      await page.waitForFunction(() => window.script.states['agent-a'].last_read_sequence === 1);
      assert.equal((await page.evaluate(() => window.sources.at(-1).url)).includes('cursor=5') || (await page.evaluate(() => window.sources.at(-1).url)).includes('cursor=0'), true);
      await emit('chat', {cursor: 6, kind: 'changed', data: {}});    // a post-reset event is not swallowed by the old cursor
      await page.waitForTimeout(100);
      assert.ok((await gets()).length > 1);
    });
    await check('a conversation resumed in Terminal appears automatically and keeps its own draft and binding', async () => {
      const current = twoConversations();
      await mount(current);
      await box().fill('current draft');
      const resumed = {
        conversation: conv('terminal-resumed'),
        session: session({conversation_id: 'terminal-resumed', binding: binding({binding_id: 'resumed-binding', conversation_id: 'terminal-resumed'})}),
        entries: [ent(29, 'owner', 'resumed conversation')], inputs: [], older: null,
      };
      await changed(resumed);
      await page.waitForSelector('[data-id="id29"]');
      assert.equal(await box().isEnabled(), true);
      assert.equal(await box().inputValue(), '', 'the previous conversation draft is not carried over');
      await box().fill('resumed draft');
      await changed(current);
      await page.waitForSelector('[data-id="id1"]');
      assert.equal(await box().inputValue(), 'current draft');
      await changed(resumed);
      await page.waitForSelector('[data-id="id29"]');
      assert.equal(await box().inputValue(), 'resumed draft');
      assert.equal((await posts('/messages')).length, 0, 'conversation changes never send or start anything');
      await page.evaluate(binding => { window.script.live = binding; }, resumed.session.binding);
      await box().press('Enter');
      await page.waitForFunction(() => document.querySelector('.gchat-composer textarea').value === '');
      const sent = await posts('/messages');
      assert.equal(sent.length, 1);
      assert.equal(sent[0].body.expected_binding, 'resumed-binding');
      assert.equal(sent[0].body.text, 'resumed draft');
    });
    await check('a reset invalidates the displayed page: no stale read or send while its state is held, and the draft survives', async () => {
      await mount(base([ent(1, 'owner', 'q'), withSeq(2, 'unread reply', 4)], {newest_sequence: 4, last_read_sequence: 0}));
      await page.waitForFunction(() => window.script.states['agent-a'].last_read_sequence === 4);
      await page.evaluate(() => { window.script.states['agent-a'].last_read_sequence = 0; });
      await box().fill('my draft');
      await page.evaluate(() => {
        const real = window.fetch; window.fetch = (url, o) => url.includes('/chat?') ? new Promise(r => { window.releaseReset = () => r(real(url, o)); }) : real(url, o);
        window.restoreFetch = () => { window.fetch = real; };
        window.calls.length = 0;
      });
      await page.evaluate(() => window.sources.at(-1).emit('reset', {}));
      assert.equal(await page.locator('.gchat-msg').count(), 0, 'old entries are gone at once');
      assert.equal(await box().isDisabled(), true);
      await page.evaluate(() => { const s = document.querySelector('.gchat-scroll'); s.dispatchEvent(new Event('scroll')); document.dispatchEvent(new Event('visibilitychange')); document.body.setAttribute('data-x', '1'); });
      await page.evaluate(() => document.querySelector('.gchat-composer').requestSubmit());
      await settle();
      assert.equal((await posts('/read')).length, 0, 'nothing is acknowledged against the old page');
      assert.equal((await posts('/messages')).length, 0);
      await page.evaluate(() => { window.script.states['agent-a'] = {...window.script.states['agent-a'], cursor: 3, revision: 3, last_read_sequence: 0, entries: [{...window.EV(1), id: 'fresh', text: 'fresh', speaker: 'agent', completion_sequence: 1, completion_id: 'c1'}], newest_sequence: 1}; window.releaseReset(); window.restoreFetch(); });
      await page.waitForSelector('[data-id="fresh"]');
      assert.equal(await box().inputValue(), 'my draft');
      await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/read')));
      assert.deepEqual((await posts('/read')).map(r => r.body), [{sequence: 1}], 'only the new page\'s boundary is posted');
    });
    await check('an older-history response for a conversation that changed meanwhile is not merged', async () => {
      await mount(base([ent(5, 'owner', 'five')], {older: 500}));
      await page.evaluate(() => {
        window.script.history = {entries: [{...window.EV(3), text: 'older of conv-1'}], older: null, rendered_boundary: 0};
        const real = window.fetch;
        window.fetch = (url, o) => url.includes('/history') ? new Promise(r => { window.releaseHistory = () => r(real(url, o)); }) : real(url, o);
        window.restoreFetch = () => { window.fetch = real; };
      });
      await page.locator('.gchat-older').click();
      // the live conversation changes automatically (no selection involved)
      await changed({conversation: {...conv('conv-2')}, entries: [ent(7, 'owner', 'in conv-2')], older: null,
        conversations: [{conversation_id: 'conv-2', provider: 'claude', updated_at: null, current: true}]});
      await page.waitForSelector('[data-id="id7"]');
      await page.evaluate(() => window.releaseHistory());
      await page.waitForTimeout(150);
      assert.deepEqual(await order(), ['id7'], 'the stale page is not merged into the new conversation');
      await page.evaluate(() => window.restoreFetch());
    });
    await check('header clearance holds without a selector even when older conversations exist', async () => {
      for (const width of [760, 320]) {
        await page.locator('#pane').evaluate((el, width) => { el.style.width = width + 'px'; }, width);
        for (const state of [base([ent(1, 'owner', 'short')]), twoConversations()]) {
          await mount(state);
          assert.equal(await page.getByRole('combobox', {name: 'Conversation', exact: true}).count(), 0);
          const clear = await page.evaluate(() => {
            const top = document.querySelector('.gchat-top').getBoundingClientRect(), first = document.querySelector('.gchat-log .gchat-msg').getBoundingClientRect();
            return first.top >= top.bottom - 1;
          });
          assert.equal(clear, true, 'header clearance, width ' + width);
        }
      }
      await page.locator('#pane').evaluate(el => { el.style.width = '760px'; });
    });
    await check('read: only completed entries rendered in the visible Chat advance the boundary; newest_sequence is never posted', async () => {
      // newest_sequence is deliberately ahead of everything rendered
      await mount(base([ent(1, 'owner', 'q'), withSeq(2, 'a reply', 3), withSeq(4, 'second reply', 5)], {newest_sequence: 9, rendered_boundary: 5}));
      await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/chat/read')));
      const reads = await posts('/read');
      assert.deepEqual(reads.map(r => r.body), [{sequence: 5}]);
      await settle();
      assert.equal((await posts('/read')).length, 1, 'an acknowledged boundary is not posted again');
      assert.equal(await page.evaluate(() => window.script.states['agent-a'].last_read_sequence), 5);
    });
    await check('read: a later concurrent reply stays unread; restart and replay do not resurrect or re-read', async () => {
      const state = base([withSeq(1, 'early', 2)], {newest_sequence: 2});
      await mount(state);
      await page.waitForFunction(() => window.script.states['agent-a'].last_read_sequence === 2);
      // The reply finishes while the owner is scrolled away from the end: it is not rendered into view.
      const long = Array.from({length: 40}, (_, i) => ent(10 + i, 'owner', 'filler ' + i + '\n\nmore text\n\nmore text'));
      await push(long[0]);
      for (const entry of long.slice(1)) await push(entry);
      await page.evaluate(() => { const s = document.querySelector('.gchat-scroll'); s.scrollTop = 0; s.dispatchEvent(new Event('scroll')); });
      await page.evaluate(() => { window.calls.length = 0; });
      await push(withSeq(60, 'a later reply', 4));
      await settle();
      assert.equal((await posts('/read')).length, 0, 'an event and its fetched page alone, off-screen, do not read');
      assert.equal(await page.evaluate(() => window.script.states['agent-a'].last_read_sequence), 2);
      assert.equal(await page.locator('.gchat-jump').isVisible(), true);
      await page.locator('.gchat-jump').click();
      await page.waitForFunction(() => window.script.states['agent-a'].last_read_sequence === 4);
      assert.deepEqual((await posts('/read')).map(r => r.body), [{sequence: 4}]);
      // restart (a new mount reading the saved boundary) and a replayed event post nothing
      await page.evaluate(() => { window.calls.length = 0; });
      await mount(await page.evaluate(() => window.script.states['agent-a']));
      await emit('chat', {cursor: 1, kind: 'changed', data: {}});
      await settle();
      assert.equal((await posts('/read')).length, 0);
    });
    await check('read: covered, hidden and closed Chats never acknowledge', async () => {
      await mount(base([], {newest_sequence: 5}));
      await page.evaluate(() => { document.querySelector('#pane').inert = true; });
      await push(withSeq(1, 'covered reply', 5));
      await settle();
      assert.equal((await posts('/read')).length, 0, 'an inert pane');
      assert.equal(await page.evaluate(() => globalChatReadable('agent-a')), false);
      await page.evaluate(() => { document.querySelector('#pane').inert = false; window.cover = modal({title: 'Covered', body: 'Dialog'}); });
      await settle();
      assert.equal((await posts('/read')).length, 0, 'a modal over the Chat');
      await page.evaluate(() => _modal.close());
      await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/read')));
      assert.deepEqual((await posts('/read')).map(r => r.body), [{sequence: 5}]);
      // a hidden tab
      await mount(base([], {newest_sequence: 7}));
      await page.evaluate(() => Object.defineProperty(document, 'hidden', {configurable: true, get: () => true}));
      await push(withSeq(1, 'while hidden', 7));
      await settle();
      assert.equal((await posts('/read')).length, 0, 'a hidden tab');
      await page.evaluate(() => { delete document.hidden; document.dispatchEvent(new Event('visibilitychange')); });
      await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/read')));
      assert.deepEqual((await posts('/read')).map(r => r.body), [{sequence: 7}]);
      // a closed Chat (disposed): arrivals are only background notifications
      await page.evaluate(() => { window.chat.dispose(); window.calls.length = 0; });
      await settle();
      assert.equal(await page.evaluate(() => window.calls.length), 0);
    });
    await check('read: an invalid boundary or a failed post is retried later, never assumed', async () => {
      await mount(base([], {}));
      await page.evaluate(() => { window.script.readError = {code: 'invalid_read_boundary', message: 'not a completion'}; });
      await push(withSeq(1, 'reply', 3));
      await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/read')));
      assert.equal(await page.evaluate(() => window.script.states['agent-a'].last_read_sequence), 0);
      await page.evaluate(() => { window.script.readError = null; });
      await page.locator('.gchat-scroll').evaluate(node => node.dispatchEvent(new Event('scroll')));
      await page.waitForFunction(() => window.script.states['agent-a'].last_read_sequence === 3);
    });
    await check('events: replayed cursors are ignored, reconnect resumes from the cursor, reset reloads, done stops sending', async () => {
      await mount(base([ent(1, 'owner', 'hi'), withSeq(2, 'there', 1)], {cursor: 40, revision: 40}));
      cursorNow = 40;
      await page.evaluate(() => { window.calls.length = 0; });
      await emit('chat', {cursor: 40, kind: 'changed', data: {}});
      await emit('chat', {cursor: 12, kind: 'changed', data: {}});
      await page.waitForTimeout(100);
      assert.equal((await gets()).length, 0, 'an event at or before the saved cursor causes no reload');
      await changed({entries: [ent(1, 'owner', 'hi'), withSeq(2, 'there', 1), withSeq(3, 'three', 2)]});
      await page.waitForSelector('[data-id="id3"]');
      assert.match(await page.evaluate(() => window.sources[0].url), /events\?cursor=40$/);
      await page.evaluate(() => window.sources[0].fail(false));
      assert.match(await page.locator('.gchat-banner').innerText(), /Reconnecting/);
      await page.evaluate(() => window.sources[0].fail(true));
      await page.waitForFunction(() => window.sources.length === 2, null, {timeout: 6000});
      assert.match(await page.evaluate(() => window.sources[1].url), /events\?cursor=41$/);
      await page.evaluate(() => window.sources[1].emit('open', {}));
      assert.equal(await page.locator('.gchat-banner').isVisible(), false);
      await page.evaluate(() => { window.script.states['agent-a'] = {...window.script.states['agent-a'], cursor: 5, revision: 5, entries: [{...window.EV(1), id: 'z', text: 'restored'}], last_read_sequence: 0}; window.sources.at(-1).emit('reset', {}); });
      await page.waitForSelector('[data-id="z"]');
      assert.deepEqual(await order(), ['z']);
      await emit('done', {});
      assert.equal(await box().isDisabled(), true);
      assert.match(await page.locator('.gchat-banner').innerText(), /no longer available/);
      assert.equal(await page.evaluate(() => window.sources.at(-1).closed), true);
    });
    await check('dispose releases the stream and every listener, and a disposed view makes no further requests', async () => {
      await mount(base([]));
      await box().fill('half written');
      const before = await page.evaluate(() => window.calls.length);
      await page.evaluate(() => window.chat.dispose());
      assert.equal(await page.evaluate(() => window.sources.at(-1).closed), true);
      await page.evaluate(() => { document.dispatchEvent(new Event('visibilitychange')); });
      await page.waitForTimeout(700);
      assert.equal(await page.evaluate(() => window.calls.length), before);
    });
    await check('no retired runner, stop, action or runtime endpoint is ever used', async () => {
      await mount(base([withSeq(1, 'reply', 1)], {actions: [{id: 'legacy', kind: 'permission', state: 'pending', request: {title: 'ignored'}}]}));
      await box().fill('hello');
      await box().press('Enter');
      await page.waitForSelector('.gchat-pending');
      await settle();
      const urls = await page.evaluate(() => window.calls.map(c => c.url));
      assert.deepEqual(urls.filter(url => /\/(stop|actions|runtime|restart|start)/.test(url)), []);
      assert.equal(await page.locator('.gchat-card, [data-answer], .gchat-stop').count(), 0, 'the legacy always-empty actions field is not rendered');
    });

    await check('attachment chips open one on-demand preview inside chat and release it on close or navigation', async () => {
      const file='/absolute/path/to/businesses/project-a/content/launch/media/landing-01-A.png';
      const message={...ent(1,'agent',''),id:'references',text:
        `Here are the mockups:\n\n- [Centered terminals](${file})\n- ![Same image](${file})\n- [HTML preview](/files/view?path=demo%2Fpreview.html)\n- [Costs ledger](/tables/view?table_id=costs)\n- [Website](https://example.com/preview)\n\n\`[Code sample](/files/view?path=not-a-preview.md)\``};
      const requested=[];
      const record=request=>{ if (/\/api\/root\/files\/image\?|\/(?:files|tables)\/view\?|https:\/\/example.com\/preview/.test(request.url())) requested.push(request.url()); };
      page.on('request',record);
      await page.locator('#pane').evaluate(el=>el.style.width='760px');
      await page.route('**/api/root/files/image?**',route=>route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=','base64')}));
      await page.route('https://example.com/preview',route=>route.fulfill({contentType:'text/html',body:'<p>Website preview</p>'}));
      await mount(base([message]));
      const card=page.locator('.gchat-msg[data-id="references"]');
      assert.equal(await card.locator('.gchat-body a').count(),5);
      assert.equal(await card.locator('.gchat-reference').count(),4,'duplicate references produce one chip');
      assert.equal(await card.locator('.gchat-body code a').count(),0);
      const preview=page.getByRole('dialog',{name:'Document preview'});
      assert.equal(await preview.isVisible(),false);
      assert.equal(await page.locator('.gchat img,.gchat iframe').count(),0,'nothing loads before clicking a chip');
      assert.deepEqual(requested,[],'link rendering makes no document or image request');
      await card.getByRole('button',{name:'Preview Centered terminals',exact:true}).click();
      assert.equal(await preview.isVisible(),true);
      const image=preview.locator('img');
      await image.evaluate(el=>el.complete ? null : new Promise(resolve=>el.addEventListener('load',resolve,{once:true})));
      assert.equal(await image.getAttribute('src'),'/api/root/files/image?path='+encodeURIComponent(file));
      assert.deepEqual(requested,['http://fixture.test/api/root/files/image?path='+encodeURIComponent(file)]);
      assert.equal(await preview.getByRole('link',{name:'Open preview in new tab'}).getAttribute('href'),'http://fixture.test/files/view?path='+encodeURIComponent(file));
      await preview.getByRole('button',{name:'Close preview'}).click();
      assert.equal(await preview.isVisible(),false);
      assert.equal(await page.locator('.gchat img,.gchat iframe').count(),0);
      assert.equal(await card.getByRole('button',{name:'Preview Centered terminals',exact:true}).evaluate(e=>e===document.activeElement),true);
      await card.getByRole('button',{name:'Preview HTML preview',exact:true}).click();
      const frame=preview.locator('iframe');
      assert.equal(await frame.getAttribute('src'),'http://fixture.test/files/view?path=demo%2Fpreview.html');
      assert.equal(await frame.getAttribute('title'),null,'preview has no hover title bubble');
      assert.equal(await frame.getAttribute('aria-label'),'HTML preview preview','preview retains its accessible name');
      assert.equal(await frame.getAttribute('sandbox'),null,'same reader shell as Viewbox');
      await frame.evaluate(el=>el.dataset.stable='yes');
      await push(message);
      assert.equal(await frame.getAttribute('data-stable'),'yes','replay does not recreate frames');
      await preview.getByRole('button',{name:'Close preview'}).press('Escape');
      assert.equal(await preview.isVisible(),false);
      await card.getByRole('button',{name:'Preview Website',exact:true}).click();
      assert.equal(await frame.getAttribute('src'),'https://example.com/preview');
      assert.equal(await frame.getAttribute('sandbox'),'allow-scripts allow-forms');
      await preview.getByRole('button',{name:'Close preview'}).click();
      await card.getByRole('button',{name:'Preview Costs ledger',exact:true}).click();
      assert.equal(await frame.getAttribute('src'),'http://fixture.test/tables/view?table_id=costs');
      assert.equal(await page.locator('.gchat iframe').count(),1);
      const size=await preview.boundingBox();
      await page.mouse.move(size.x+size.width-3,size.y+size.height-3);
      await page.mouse.down();
      await page.mouse.move(size.x+size.width-83,size.y+size.height-63,{steps:5});
      await page.mouse.up();
      const resized=await preview.boundingBox();
      assert.ok(resized.width<size.width-40&&resized.height<size.height-30,'dragging the resize handle changes both dimensions');
      await page.locator('#pane').evaluate(el=>el.style.width='320px');
      assert.equal(await card.evaluate(el=>el.scrollWidth<=el.clientWidth+1),true,'chips fit a narrow chat');
      assert.equal(await preview.evaluate(el=>{
        const r=el.getBoundingClientRect(),p=el.closest('.gchat').getBoundingClientRect();
        return r.left>=p.left&&r.right<=p.right&&r.top>=p.top&&r.bottom<=p.bottom;
      }),true,'preview stays inside the chat');
      assert.equal(await preview.evaluate(el=>getComputedStyle(el).resize),'both');
      assert.equal((await posts('/messages')).length,0);
      await page.evaluate(()=>window.chat.dispose());
      assert.equal(await page.locator('.gchat iframe').count(),0);
      page.off('request',record);
      await page.locator('#pane').evaluate(el=>el.style.width='760px');
      await mount(base([]));
    });

    await check('Markdown pipe tables render as semantic tables; look-alikes and incomplete tables stay text', async () => {
      const shot = '| Subscription | Cost/month |\n|---|---|\n| IPRoyal | $230 |\n| LLM | $200 |\n| Compute | $70 |\n| Domains | $12 |\n| Email | $20 |\n| Misc | $8 |';
      const bodies = [
        'Costs:\n\n' + shot,
        'a | b\n:--- | ---:\nx | y',
        '| L | C | R |\n|:---|:---:|---:|\n| `a|b` | [site](https://example.com) **bold** | <b>raw</b> \\| pipe | extra |\n| short |',
        'a | b\nplain | text',
        '```\n| a | b |\n|---|---|\n| 1 | 2 |\n```',
        '| a | b |',
        'Title\n---\n\nbefore\n***',
        '| a | b |\n|---|\n| 1 | 2 |',
        '| a | b |\n|--|--|\n| 1 | 2 |',
      ];
      await mount(base(bodies.map((body, i) => ent(i + 1, 'agent', body))));
      await page.waitForSelector('.gchat-msg[data-id="id7"]');
      const one = page.locator('.gchat-msg[data-id="id1"]');
      assert.equal(await one.locator('.gc-table-wrap > table > thead > tr > th').count(), 2);
      assert.equal(await one.locator('th[scope="col"]').count(), 2);
      assert.deepEqual(await one.locator('tbody tr').evaluateAll(rows => rows.map(r => [...r.cells].map(c => c.textContent))),
        [['IPRoyal', '$230'], ['LLM', '$200'], ['Compute', '$70'], ['Domains', '$12'], ['Email', '$20'], ['Misc', '$8']]);
      assert.equal(await one.locator('.gchat-body').evaluate(node => node.textContent.includes('|---')), false);
      const two = page.locator('.gchat-msg[data-id="id2"]');
      assert.deepEqual(await two.locator('th, td').evaluateAll(cells => cells.map(c => c.textContent)), ['a', 'b', 'x', 'y']);
      assert.deepEqual(await two.locator('th, td').evaluateAll(cells => cells.map(c => c.className)), ['gc-left', 'gc-right', 'gc-left', 'gc-right']);
      const three = page.locator('.gchat-msg[data-id="id3"]');
      assert.deepEqual(await three.locator('th').evaluateAll(cells => cells.map(c => c.className)), ['gc-left', 'gc-center', 'gc-right']);
      assert.equal(await three.locator('tbody tr:first-child td').count(), 3);      // extra cell ignored
      assert.equal(await three.locator('tbody tr:last-child td').count(), 3);       // short row padded
      assert.equal(await three.locator('tbody tr:last-child td:nth-child(2)').innerText(), '');
      assert.equal(await three.locator('td:nth-child(1) code').first().innerText(), 'a|b');
      assert.equal(await three.locator('td:nth-child(2) a.safe-link').count(), 1);
      assert.equal(await three.getByRole('button', {name: 'Preview site', exact: true}).count(), 1, 'links within tables keep attachment previews');
      assert.equal(await three.locator('td:nth-child(2) strong').innerText(), 'bold');
      assert.equal(await three.locator('td:nth-child(3)').first().innerText(), '<b>raw</b> | pipe');
      assert.equal(await three.locator('td b').count(), 0);
      for (const id of ['id4', 'id5', 'id6', 'id8', 'id9']) assert.equal(await page.locator(`.gchat-msg[data-id="${id}"] table`).count(), 0, id);
      assert.match(await page.locator('.gchat-msg[data-id="id5"] pre').innerText(), /\| a \| b \|\n\|---\|---\|/);
      assert.match(await page.locator('.gchat-msg[data-id="id4"]').innerText(), /a \| b/);
      assert.equal(await page.locator('.gchat-msg[data-id="id7"] table').count(), 0);
      const shotSize = await one.locator('td').first().evaluate(node => getComputedStyle(node).fontSize);
      assert.equal(shotSize, await one.locator('.gchat-body').evaluate(node => getComputedStyle(node).fontSize));
      if (process.env.CHAT_TABLE_SHOT) await one.screenshot({path: process.env.CHAT_TABLE_SHOT});
    });

    // ---- accepted messages stay visible until their native transcript echo (browser-only bridge)
    const accepted = async (text, state = 'submitted') => {
      await page.evaluate(([state]) => { window.script.nextState = state; window.script.bodyless = state === 'submitted'; }, [state]);
      await box().fill(text); await box().press('Enter');
      await page.waitForFunction(() => document.querySelector('#pane textarea')?.value === '');
    };
    const copies = text => page.locator('.gchat-msg', {hasText: text}).count();
    const echo = (n, text, at = '2026-10-04T21:26:00Z') => ent(n, 'owner', text, {entry: {timestamp: at}});
    const retag = (patch, id = 'agent-a') => page.evaluate(([patch, id]) => { window.script.states[id].inputs = window.script.states[id].inputs.map(i => Object.assign(i, patch)); }, [patch, id]);
    await check('a submitted body-less receipt keeps the sent text as "Sent" through stale reads until its exact echo, then exactly one copy remains', async () => {
      await mount(base([ent(1, 'agent', 'earlier reply')]));
      await accepted('visible after admit');
      await page.waitForSelector('.gchat-pending[data-state="submitted"]');
      assert.equal(await copies('visible after admit'), 1);
      assert.equal((await page.locator('.gchat-pending .gchat-state').innerText()).trim(), 'Sent');
      assert.equal(await page.locator('.gchat-pending [data-pending-action]').count(), 0, 'no Resend or Dismiss for a submitted message');
      await changed();                                              // stale read: receipt submitted, body cleared, no user entry
      await settle();
      assert.equal(await copies('visible after admit'), 1);
      await retag({state: 'submitted'}); await page.evaluate(() => { window.script.states['agent-a'].inputs = []; }); await changed();
      await settle();
      assert.equal(await copies('visible after admit'), 1, 'a read omitting the receipt does not erase it');
      await push(ent(2, 'agent', 'visible after admit'));                // an agent reply with the same text is not the echo
      assert.equal(await page.locator('.gchat-pending').count(), 1);
      await push(echo(3, 'visible after admit'));
      await page.waitForFunction(() => !document.querySelector('.gchat-pending'));
      assert.equal(await page.locator('.gchat-user', {hasText: 'visible after admit'}).count(), 1);
      assert.equal(await page.locator('.gchat-pending').count(), 0);
    });
    await check('a waiting receipt turning submitted keeps one visible bubble', async () => {
      await mount(base([]));
      await accepted('gap message', 'waiting');
      await page.waitForSelector('.gchat-pending[data-state="waiting"]');
      await retag({state: 'submitted', body: null, settled_at: '2026-10-04T21:25:02Z'}); await changed();
      await page.waitForSelector('.gchat-pending[data-state="submitted"]');
      assert.equal(await copies('gap message'), 1);
      await push(echo(1, 'gap message'));
      await page.waitForFunction(() => !document.querySelector('.gchat-pending'));
      assert.equal(await copies('gap message'), 1);
    });
    await check('two identical sends need two separate echoes; older identical history is not an echo', async () => {
      await mount(base([echo(1, 'twice', '2026-10-04T21:20:00Z')]));
      await accepted('twice'); await accepted('twice');
      await page.waitForFunction(() => document.querySelectorAll('.gchat-pending').length === 2);
      await changed();
      assert.equal(await page.locator('.gchat-pending').count(), 2, 'the earlier identical entry matches neither send');
      await push(echo(2, 'twice'));
      await page.waitForFunction(() => document.querySelectorAll('.gchat-pending').length === 1);
      assert.equal(await page.locator('.gchat-log .gchat-user', {hasText: 'twice'}).count(), 2);
      await push(echo(3, 'twice', '2026-10-04T21:27:00Z'));
      await page.waitForFunction(() => !document.querySelector('.gchat-pending'));
      assert.equal(await page.locator('.gchat-log .gchat-user', {hasText: 'twice'}).count(), 3);
    });
    await check('an entry without a timestamp must lie beyond the position seen at send time', async () => {
      await mount(base([ent(1, 'owner', 'untimed')]));
      await accepted('untimed');
      await changed();
      assert.equal(await page.locator('.gchat-pending').count(), 1);
      await push(ent(2, 'owner', 'untimed'));
      await page.waitForFunction(() => !document.querySelector('.gchat-pending'));
    });
    await check('accepted bubbles stay inside their agent and conversation and survive switching', async () => {
      await mount(base([]));
      await accepted('stay here');
      await page.waitForSelector('.gchat-pending');
      await mount({...base([], {agent_id: 'agent-b'}), name: 'Dev', session: session({binding: binding({agent_id: 'agent-b'})})}, 'agent-b', 'Dev', true);
      assert.equal(await copies('stay here'), 0, 'not shown for another agent');
      await mount(base([]), 'agent-a', 'Marketing', true);
      await page.waitForSelector('.gchat-pending');
      const other = base([], {conversation: conv('conv-2'), session: session({conversation_id: 'conv-2', binding: binding({conversation_id: 'conv-2'})})});
      await mount(other, 'agent-a', 'Marketing', true);
      assert.equal(await copies('stay here'), 0, 'not shown for another conversation');
      await push(echo(1, 'stay here'));
      await mount(base([]), 'agent-a', 'Marketing', true);
      assert.equal(await page.locator('.gchat-pending').count(), 1, 'another conversation echo consumed nothing');
      assert.equal(await copies('stay here'), 1);
    });
    await check('a send answered after switching away is still shown on return', async () => {
      await mount(base([]));
      await page.evaluate(() => { window.script.nextState = 'submitted'; window.script.bodyless = true; window.script.release = null; window.script.hold = new Promise(done => { window.script.release = done; }); });
      await box().fill('answered late'); await box().press('Enter');
      await page.waitForFunction(() => window.calls.some(c => c.method === 'POST' && c.url.endsWith('/messages')));
      await page.evaluate(() => window.chat.dispose());
      await page.evaluate(() => { window.script.release(); window.script.hold = null; });
      await page.waitForFunction(() => window.script.inputs.size > 0 && GLOBAL_CHAT_ACCEPTED.size > 0);
      await mount(base([]), 'agent-a', 'Marketing', true);
      await page.evaluate(() => { window.script.states['agent-a'].inputs = []; });
      await changed();
      await page.waitForSelector('.gchat-pending[data-state="submitted"]');
      assert.equal(await copies('answered late'), 1);
    });
    await check('a message with an uploaded-file reference matches its echo exactly', async () => {
      await mount(base([]));
      await page.locator('.gchat-composer input[type="file"]').setInputFiles({name: 'brief.txt', mimeType: 'text/plain', buffer: Buffer.from('x')});
      await page.getByRole('button', {name: 'Remove brief.txt', exact: true}).waitFor();
      await accepted('with file');
      await page.waitForSelector('.gchat-pending[data-state="submitted"]');
      const payload = (await posts('/messages')).at(-1).body.text;
      assert.match(payload, /^with file\n\n\[brief\.txt\]/);
      assert.equal(await page.locator('.gchat-pending').innerText().then(t => t.includes('brief.txt')), true);
      await push(echo(1, 'with file'));
      assert.equal(await page.locator('.gchat-pending').count(), 1, 'a partial text is not the echo');
      await push(echo(2, payload.replace(/\n/g, '\r\n')));
      await page.waitForFunction(() => !document.querySelector('.gchat-pending'));
    });
    await check('a lost response, the echo, then the same-key retry leaves exactly one copy (send and explicit resend)', async () => {
      await mount(base([]));
      await page.evaluate(() => { window.script.nextState = 'submitted'; window.script.bodyless = true; window.script.loseResponse = true; });
      await box().fill('lost then echoed'); await box().press('Enter');
      await page.waitForSelector('.gchat-banner:not([hidden])');
      await push(echo(1, 'lost then echoed'));
      await page.evaluate(() => { window.script.loseResponse = false; });
      await box().press('Enter');
      await page.waitForFunction(() => !document.querySelector('.gchat-pending') && document.querySelector('#pane textarea').value === '');
      const keys = (await posts('/messages')).map(call => call.body.idempotency_key);
      assert.equal(keys.length, 2); assert.equal(keys[0], keys[1]);
      assert.equal(await copies('lost then echoed'), 1);
      const input = {id: 'in-lost2', kind: 'input', sequence: 1, origin: 'owner', input_type: 'chat', state: 'undelivered', reason: null, body: 'resend then echo', session_id: 's1', evidence: {}, created_at: '2026-10-04T21:25:00Z', settled_at: null};
      await mount(base([], {inputs: [input]}));
      await page.evaluate(() => { window.script.nextState = 'submitted'; window.script.bodyless = true; window.script.loseResponse = true; });
      await page.locator('[data-resend]').click();
      await page.waitForFunction(() => !document.querySelector('.gchat-banner').hidden);
      await push(echo(1, 'resend then echo'));
      await page.evaluate(() => { window.script.loseResponse = false; });
      await page.locator('[data-resend]').click();
      await page.waitForFunction(() => !document.querySelector('[data-input-id="in-lost2"]'));
      await settle();
      assert.equal((await posts('/messages')).length, 2);
      assert.equal(await page.locator('.gchat-pending').count(), 0);
      assert.equal(await copies('resend then echo'), 1);
    });
    await check('a CRLF payload reconciles with an LF echo while the sent text stays exact', async () => {
      const input = {id: 'in-crlf', kind: 'input', sequence: 1, origin: 'owner', input_type: 'chat', state: 'undelivered', reason: null, body: 'line one\r\nline two', session_id: 's1', evidence: {}, created_at: '2026-10-04T21:25:00Z', settled_at: null};
      await mount(base([], {inputs: [input]}));
      await page.evaluate(() => { window.script.nextState = 'submitted'; window.script.bodyless = true; });
      await page.locator('[data-resend]').click();
      await page.waitForSelector('.gchat-pending[data-state="submitted"]');
      assert.equal((await posts('/messages')).at(-1).body.text, 'line one\r\nline two');
      await push(echo(1, 'line one\nline two'));
      await page.waitForFunction(() => !document.querySelector('.gchat-pending'));
      assert.equal(await copies('line two'), 1);
    });

    await check('a wide table scrolls inside its own wrapper and never widens the bubble or page', async () => {
      const wide = '| ' + Array.from({length: 8}, (_, i) => 'Column heading ' + i).join(' | ') + ' |\n|' + '---|'.repeat(8) + '\n| ' + Array.from({length: 8}, (_, i) => 'value number ' + i).join(' | ') + ' |';
      await page.setViewportSize({width: 420, height: 800});
      await page.evaluate(() => { document.querySelector('#pane').style.width = '300px'; });
      await mount(base([ent(1, 'agent', wide)]));
      await page.waitForSelector('.gc-table-wrap');
      const m = await page.evaluate(() => {
        const wrap = document.querySelector('.gc-table-wrap'), body = document.querySelector('.gchat-body'), pane = document.querySelector('#pane'), scroll = document.querySelector('.gchat-scroll');
        return {wrap: [wrap.scrollWidth, wrap.clientWidth], body: body.getBoundingClientRect().right, pane: pane.getBoundingClientRect().right,
          page: document.documentElement.scrollWidth - document.documentElement.clientWidth, scrollX: scroll.scrollWidth - scroll.clientWidth, y: getComputedStyle(scroll).overflowY};
      });
      assert.ok(m.wrap[0] > m.wrap[1], 'table overflows its wrapper');
      assert.ok(m.body <= m.pane + 1);
      assert.equal(m.page, 0);
      assert.ok(m.scrollX <= 0);
      assert.equal(m.y, 'auto');
      await page.getByRole('region', {name: 'Table', exact: true}).focus();
      await page.keyboard.press('ArrowRight');
      await page.waitForFunction(() => document.querySelector('.gc-table-wrap').scrollLeft > 0);
      await page.setViewportSize({width: 1000, height: 800});
      await page.evaluate(() => { document.querySelector('#pane').style.width = '760px'; });
    });


    assert.deepEqual(errors, []);
    console.log(`Global chat view (mocked transport): ${checks} checks passed.`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
