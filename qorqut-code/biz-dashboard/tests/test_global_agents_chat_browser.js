#!/usr/bin/env node
// Real js/global-agents.js and js/global-chat.js in Chromium over MOCKED dependencies (fetch, EventSource, mountTerminal and the
// agent pane are stubs). It checks which transport each selection uses; it does not start any agent or terminal.
//   PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_global_agents_chat_browser.js
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
let checks = 0;
const check = async (label, action) => { await action(); checks++; console.log('ok ' + label); };

(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1300, height: 800}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.stack || error.message));
    await page.route('http://fixture.test/**', route => route.fulfill({body: '<html></html>', contentType: 'text/html'}));
    await page.goto('http://fixture.test/');
    await page.setContent('<div id="toasts"></div><button id="wsbtn"></button><div id="host" style="height:700px"></div>');
    await page.addScriptTag({path: path.join(root, 'js/core.js')});
    await page.addScriptTag({path: path.join(root, 'js/safe-links.js')});
    await page.addScriptTag({path: path.join(root, 'js/chat-view.js')});
    await page.addScriptTag({path: path.join(root, 'js/global-chat.js')});
    await page.evaluate(() => {
      window.mounts = []; window.sources = []; window.torn = 0; window.calls = [];
      window.EventSource = class { constructor(url) { this.url = url; this.l = {}; window.sources.push(this); } addEventListener(t, f) { (this.l[t] ||= []).push(f); } close() { this.closed = true; } };
      const chatState = id => ({agent_id: id, name: id, session: {state: 'live', reason: 'live', can_send: true, working: false, provider: 'claude', conversation_id: 'conv-1', transport: 'terminal', detail: 'Live', binding: {binding_id: 'b1', agent_id: 'a', provider: 'claude', conversation_id: 'conv-1', generation: 1, transport: 'terminal'}}, revision: 0, cursor: 0, conversation: {conversation_id: 'conv-1', provider: 'claude', complete: true, finalized: false, error: null, captured_length: 0}, conversations: [{conversation_id: 'conv-1', provider: 'claude', updated_at: null, current: true}], entries: [], older: null, rendered_boundary: 0, inputs: [], last_read_sequence: 0, newest_sequence: 0});
      window.fetch = async (url, options = {}) => {
        window.calls.push({url, method: options.method || 'GET'});
        const chat = /^\/api\/root\/agents\/([^/]+)\/chat(?:\?.*)?$/.exec(url);
        const data = chat ? chatState(decodeURIComponent(chat[1])) : {};
        return new Response(JSON.stringify({status: 'ok', data}), {status: 200});
      };
      // Dependencies of global-agents.js that belong to other screens.
      window.globalAgentProfiles = [{agent_id: 'g-marketing', name: 'marketing', display_name: 'Marketing', role: {id: 'global-worker'}, status: 'active', scope: {kind: 'ecosystem', allowed_business_ids: [], global_capabilities: []}},
        {agent_id: 'g-ordu', name: 'ordubashy', display_name: 'Ordubashy', role: {id: 'ordubashy'}, status: 'active', scope: {kind: 'ecosystem', allowed_business_ids: [], global_capabilities: []}}];
      Object.assign(window, {
        loadGlobalAgentProfiles: async () => window.globalAgentProfiles, broadcastButton: () => '', agentNavButton: () => '', wireBroadcastButton() {}, wireAgentNavToggle() {},
        agentNavPaint() {}, agentNavRefresh() {}, agTakeLinkedWorkspace: () => null, agentWorkspaceTakeRequest: () => null, agentWorkspaceRehydrate: () => null, agentWorkspaceCanLeave: () => true, agentWorkspaceMenuPlace() {}, AGENT_PANE_DESCRIPTOR_ROLES: {}, GLOBAL_ICONS: {settings: ''}, agentOpenTab() {}, identityAvatar: () => '',
        agpadMount: (name, el, opts) => { window.mounts.push({pane: name, opts}); el.innerHTML='<div class="thead"></div>'; },
        teardownTerm: () => { window.torn++; window.sharedTerm = false; },
        mountTerminal: (agent, pane, onStat, base) => { window.sharedTerm = true; window.mounts.push({terminal: base}); pane.innerHTML = '<div class="fake-terminal">terminal ' + base + '</div>'; },
      });
      state.scope = 'global'; state.globalTab = 'agents'; state.bizs = [{slug: 'widget', name: 'Widget'}];
    });
    await page.addScriptTag({content: 'let _agpad = null;'});   // the pane controller's current-pane slot (js/agentpane.js)
    await page.addScriptTag({path: path.join(root, 'js/global-agents.js')});
    await page.addScriptTag({path: path.join(root, 'js/reply-notifications.js')});
    const terminals = () => page.evaluate(() => window.mounts.filter(m => m.terminal).map(m => m.terminal));
    const render = () => page.evaluate(async () => { window.mounts.length = 0; window.sources.length = 0; localStorage.clear(); await renderGlobalAgents(document.querySelector('#host')); });
    const pane = '#global-agent-terminal';
    // The chat root appears at once; its stream opens after the first state read.
    const connected = id => page.waitForFunction(id => window.sources.some(s => !s.closed && s.url.includes('/agents/' + id + '/chat/events')), id);

    await check('a global agent opens the chat, never a terminal', async () => {
      await render();
      await page.waitForSelector(`${pane} [data-global-chat="g-ordu"]`);            // Ordubashy is the default selection
      await connected('g-ordu');
      assert.deepEqual(await terminals(), []);
      assert.equal(await page.locator('.fake-terminal').count(), 0);
      assert.equal(await page.evaluate(() => window.sources.length), 1);
      assert(await page.evaluate(() => window.sources[0].url.includes('/root/agents/g-ordu/chat/events')));
      assert.equal(await page.locator(pane).getAttribute('data-agent-pane'), 'on');
      assert.equal(await page.evaluate(() => window.mounts.find(m => m.pane)?.opts.agentId), 'g-ordu');   // the agent pane (Focus, tabs) is unchanged
    });

    await check('switching agents disposes the previous chat stream and mounts the next', async () => {
      await page.locator('[data-global-agent="g-marketing"]').first().click();
      await page.waitForSelector(`${pane} [data-global-chat="g-marketing"]`);
      await connected('g-marketing');
      assert.equal(await page.locator('[data-global-chat]').count(), 1);
      assert.equal(await page.evaluate(() => window.sources[0].closed), true);
      assert.equal(await page.evaluate(() => window.sources.at(-1).closed || false), false);
    });

    await check('the workspace Terminal keeps the shell-only terminal transport', async () => {
      await page.locator('[data-global-agent="__qorqut_workspace_terminal__"]').first().waitFor();
      await page.evaluate(() => document.querySelector('[data-global-agent="__qorqut_workspace_terminal__"]').click());
      await page.waitForSelector('.fake-terminal');
      assert.equal(await page.locator('[data-global-chat]').count(), 0);
      assert.equal(await page.evaluate(() => window.sources.every(s => s.closed)), true);
      assert.deepEqual((await terminals()).slice(-1), ['/root/workspace-terminal']);
      await page.evaluate(() => document.querySelector('[data-global-agent="g-marketing"]').click());
      await page.waitForSelector('[data-global-chat="g-marketing"]');
      await connected('g-marketing');
    });

    await check('a former business agent is neither listed nor opened, and drafts stay per global agent', async () => {
      assert.equal(await page.locator('[data-business-agent]').count(), 0);
      assert.equal(await page.evaluate(() => window.calls.some(c => /^\/api\/biz\/|agent-pins/.test(c.url))), false, 'no business roster or pin reads');
      const before = await page.evaluate(() => window.sources.length);
      await assert.rejects(page.evaluate(() => globalAgentsNavigate('b-dev', {chat: true})), /no longer active/);
      assert.equal(await page.evaluate(() => window.sources.length), before, 'no chat is opened for it');
      assert.equal(await page.locator('[data-global-chat="b-dev"]').count(), 0);
      await page.locator('.gchat-composer textarea').fill('Marketing draft');
      await page.evaluate(() => document.querySelector('[data-global-agent="g-ordu"]').click());
      await page.waitForSelector('[data-global-chat="g-ordu"]');
      assert.equal(await page.locator('.gchat-composer textarea').inputValue(), '');
      assert.equal(await page.evaluate(() => window.sources.filter(s => !s.closed).length <= 1), true, 'only one live stream');
      await page.evaluate(() => document.querySelector('[data-global-agent="g-marketing"]').click());
      await page.waitForSelector('[data-global-chat="g-marketing"]');
      assert.equal(await page.locator('.gchat-composer textarea').inputValue(), 'Marketing draft', 'the per-agent draft survives switching');
      await page.locator('.gchat-composer textarea').fill('');
    });

    await check('leaving the workspace Terminal for Global Chat releases the shared terminal viewer', async () => {
      await page.evaluate(() => document.querySelector('[data-global-agent="__qorqut_workspace_terminal__"]').click());
      await page.waitForSelector('.fake-terminal');
      assert.equal(await page.evaluate(() => window.sharedTerm), true);
      await page.evaluate(() => document.querySelector('[data-global-agent="g-marketing"]').click());
      await page.waitForSelector('[data-global-chat="g-marketing"]');
      assert.equal(await page.evaluate(() => window.sharedTerm), false, 'Terminal -> Global Chat tears the shell down');
      await page.evaluate(() => document.querySelector('[data-global-agent="__qorqut_workspace_terminal__"]').click());
      await page.waitForSelector('.fake-terminal');
      assert.equal(await page.evaluate(() => window.sharedTerm), true);
      await page.evaluate(() => document.querySelector('[data-global-agent="g-ordu"]').click());
      await page.waitForSelector('[data-global-chat="g-ordu"]');
      assert.equal(await page.evaluate(() => window.sharedTerm), false, 'Terminal -> another Global Chat tears the shell down');
      await connected('g-ordu');
      assert.equal(await page.evaluate(() => window.sources.filter(s => !s.closed).length), 1, 'only the current chat stream remains');
      // Terminal -> Chat inside an agent view releases the viewer too (a reconnect replaces it in mountTerminal).
      await page.locator('.gchat-session-switch [data-terminal]').click();
      await page.waitForSelector('.gchat-diagnostic .fake-terminal');
      assert.equal(await page.evaluate(() => window.sharedTerm), true);
      await page.locator('[data-chat-return]').click();
      await page.waitForSelector('[data-global-chat="g-ordu"]');
      assert.equal(await page.evaluate(() => window.sharedTerm), false);
    });

    await check('Chat/Terminal toggles the interactive shell without stopping the agent', async () => {
      await page.evaluate(() => document.querySelector('[data-global-agent="g-marketing"]').click());
      await page.waitForSelector('[data-global-chat="g-marketing"]');
      await connected('g-marketing');
      assert.deepEqual((await terminals()).filter(base => base.includes('g-marketing')), []);
      await page.locator('.gchat-composer textarea').fill('Unsent draft');
      assert.equal(await page.locator('#agpad .gchat-view-toggle').count(), 0);
      await page.locator('.gchat-session-switch [data-terminal]').click();
      await page.waitForSelector('.gchat-diagnostic .fake-terminal');
      assert.equal(await page.evaluate(() => window.sources.every(s => s.closed)), true, 'the chat stream is released while the diagnostic terminal shows');
      assert.deepEqual((await terminals()).slice(-1), ['/root/terminal/g-marketing']);
      assert.equal(await page.locator('[data-global-chat]').count(), 0);
      assert.equal(await page.locator('.gchat-view-toggle [data-terminal]').getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator('.gchat-view-toggle [data-chat-return]').isEnabled(), true);
      const mountsBeforeRefresh = (await terminals()).length;
      await page.getByRole('button', {name:'Refresh', exact:true}).click();
      assert.equal((await terminals()).length, mountsBeforeRefresh + 1);
      assert.deepEqual((await terminals()).slice(-1), ['/root/terminal/g-marketing']);

      await page.locator('[data-chat-return]').click();
      await page.waitForSelector('[data-global-chat="g-marketing"]');
      await connected('g-marketing');   // the mount exists before its conversation state and draft are restored
      assert.equal(await page.locator('.gchat-composer textarea').inputValue(), 'Unsent draft');
      assert.equal(await page.locator('.gchat-view-toggle [data-chat-return]').getAttribute('aria-pressed'), 'true');
      const oldSources = await page.evaluate(() => window.sources.length);
      await page.getByRole('button', {name:'Refresh', exact:true}).click();
      await page.waitForFunction(count => window.sources.length > count, oldSources);
      assert.equal(await page.locator('.gchat-composer textarea').inputValue(), 'Unsent draft');
      assert.equal(await page.evaluate(() => window.sources.filter(s => !s.closed).length), 1);

      assert.equal(await page.evaluate(() => window.calls.filter(c => /\/chat\/(runtime|stop|actions)/.test(c.url)).length), 0, 'view changes never stop, disable or restart the agent');
    });

    await check('leaving the Global agents screen releases the chat stream', async () => {
      await connected('g-marketing');
      await page.evaluate(() => { document.querySelector('#host').innerHTML = ''; });
      await page.waitForFunction(() => window.sources.every(s => s.closed), null, {timeout: 6000});
    });

    await check('unread chat replies mark the agent row, and clear when read', async () => {
      await render();
      await page.evaluate(async () => {
        window.fetch = async url => new Response(JSON.stringify({status: 'ok', data: {items: window.feed || []}}), {status: 200});
        window.feed = [{id: 'gchat:1', kind: 'global', chat: true, agent_id: 'g-marketing', agent_name: 'Marketing', text: 'Done', unread: true, sequence: 5}];
        await replyNotifications.refresh();
      });
      assert.equal(await page.locator('[data-global-agent="g-marketing"] .chat-unread-dot').count(), 1);
      assert.equal(await page.locator('[data-global-agent="g-ordu"] .chat-unread-dot').count(), 0);
      assert.equal(await page.evaluate(() => [replyNotifications.unreadAgent('g-marketing'), replyNotifications.unreadAgent('g-ordu'), replyNotifications.unreadChat()]).then(v => v.join()), 'true,false,true');
      await page.evaluate(async () => { window.feed = []; await replyNotifications.refresh(); });
      assert.equal(await page.locator('.chat-unread-dot').count(), 0);
    });

    await check('a reply is toasted once, but not while that chat is open on screen; clicking opens that chat', async () => {
      await page.evaluate(async () => { window.feed = []; document.querySelector('#toasts').innerHTML = ''; await replyNotifications.refresh(); });
      // Ordubashy's chat is the one on screen, so a Marketing reply is announced and an Ordubashy reply is not.
      await page.evaluate(() => { window.feed = [{id: 'gchat:2', kind: 'global', chat: true, agent_id: 'g-marketing', agent_name: 'Marketing', text: 'Hi', unread: true, sequence: 1}]; });
      await page.evaluate(() => replyNotifications.refresh());
      assert.equal(await page.locator('.toast').count(), 1);
      await page.evaluate(() => replyNotifications.refresh());
      assert.equal(await page.locator('.toast').count(), 1, 'the same reply is not announced twice');
      await page.evaluate(() => replyNotifications.open(window.feed[0]));
      assert.equal(await page.evaluate(() => localStorage.getItem('ag:scope:global')), 'g-marketing');
      await render();
      await page.evaluate(() => localStorage.setItem('ag:scope:global', 'g-marketing'));
      await page.evaluate(async () => { await renderGlobalAgents(document.querySelector('#host')); });
      await page.waitForSelector('[data-global-chat="g-marketing"]');
      await page.evaluate(() => { window.feed = [{id: 'gchat:3', kind: 'global', chat: true, agent_id: 'g-marketing', agent_name: 'Marketing', text: 'Again', unread: true, sequence: 2}]; document.querySelector('#toasts').innerHTML = ''; });
      await page.evaluate(() => replyNotifications.refresh());
      assert.equal(await page.locator('.toast').count(), 0, 'no toast for a chat that is already visible');
    });

    assert.deepEqual(errors, []);
    console.log(`Global agents chat selection (mocked dependencies): ${checks} checks passed.`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
