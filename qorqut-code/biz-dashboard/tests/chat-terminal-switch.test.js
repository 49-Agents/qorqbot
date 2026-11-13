'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname, '..');

async function fixture(browser, initial = 'chat') {
  const page = await browser.newPage({viewport: {width: 1000, height: 800}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setContent('<body><main id="pane" style="display:flex;flex-direction:column;height:700px;width:800px"></main></body>');
  for (const file of ['app.css', 'css/chat-view.css', 'css/global-chat.css', 'css/xterm.css']) await page.addStyleTag({path: path.join(root, file)});
  await page.evaluate(() => {
    window.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    window.$ = (selector, host = document) => host.querySelector(selector);
    window.identityAvatar = () => '';
    window.state = {biz: 'demo'};
    window.coreData = value => value;
    window.evUrl = value => value;
    window.calls = {loads: 0, posts: [], streams: []};
    window.EventSource = class extends EventTarget {
      static CLOSED = 2;
      constructor(url) { super(); this.url = url; calls.streams.push(this); }
      close() { this.closed = true; }
      emit(type, value) {
        const event = new MessageEvent(type, {data: JSON.stringify(value)});
        this.dispatchEvent(event);
      }
    };
    window.binding = {provider: 'codex', conversation_id: 'conversation-1', binding_id: 'binding-1'};
    window.entries = Array.from({length: 35}, (_, i) => ({id: 'entry-' + i, position: i, speaker: 'agent', text: 'Message ' + i, completion_sequence: i + 1}));
    window.api = async () => {
      calls.loads++;
      return {session: {state: 'live', can_send: true, binding}, conversation: binding, entries: [...entries], cursor: 1, inputs: []};
    };
    window.post = async (url, body) => { calls.posts.push({url, body}); return {last_read_sequence: body.sequence}; };
    window.current = true;
  });
  for (const file of ['safe-links', 'chat-view', 'slash-commands', 'xterm', 'terminal', 'global-chat']) await page.addScriptTag({path: path.join(root, 'js', file + '.js')});
  await page.evaluate(initial => {
    window.mount = () => mountAgentView(document.querySelector('#pane'), {agent_id: 'agent-1', name: 'Demo'}, {
      terminal: {key: 'agent-1', base: '/terminal/agent-1'}, isCurrent: () => current, view: initial,
    });
    window.controller = mount();
  }, initial);
  return {page, errors};
}

test('warm Chat/Terminal toggles preserve views, drafts, scroll and streams; hidden views do not read or resize', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser);
    await page.locator('.gchat-msg').first().waitFor();
    await page.locator('.gchat-composer textarea').fill('Unsent draft');
    await page.evaluate(() => {
      window.savedChat = document.querySelector('.gchat');
      document.querySelector('.gchat-scroll').scrollTop = 150;
    });
    await page.waitForTimeout(100);
    const scrollTop = await page.locator('.gchat-scroll').evaluate(el => el.scrollTop);
    await page.click('[data-terminal]:visible');
    await page.waitForTimeout(200);
    await page.evaluate(() => {
      window.savedTerminal = _term;
      _term.write(Array.from({length: 120}, (_, i) => 'terminal line ' + i + '\r\n').join(''));
    });
    await page.waitForTimeout(100);
    await page.evaluate(() => _term.scrollToLine(10));
    const terminalScroll = await page.evaluate(() => _term.buffer.active.viewportY);
    const count = await page.evaluate(() => ({loads: calls.loads, streams: calls.streams.length}));
    for (let i = 0; i < 6; i++) {
      await page.click('[data-chat-return]:visible');
      await page.click('[data-terminal]:visible');
    }
    assert.deepEqual(await page.evaluate(() => ({loads: calls.loads, streams: calls.streams.length})), count);
    assert.equal(await page.evaluate(() => _term === savedTerminal), true);
    assert.equal(await page.evaluate(() => _term.buffer.active.viewportY), terminalScroll);
    await page.click('[data-chat-return]:visible');
    assert.equal(await page.evaluate(() => document.querySelector('.gchat') === savedChat), true);
    assert.equal(await page.inputValue('.gchat-composer textarea'), 'Unsent draft');
    assert.ok(Math.abs(await page.locator('.gchat-scroll').evaluate(el => el.scrollTop) - scrollTop) < 2);
    await page.evaluate(() => { const scroll = document.querySelector('.gchat-scroll'); scroll.scrollTop = scroll.scrollHeight; });
    await page.waitForFunction(() => calls.posts.some(p => p.url.endsWith('/read') && p.body.sequence === 35));
    await page.click('[data-terminal]:visible');
    // Simulate a new reply while bottom-pinned Chat is hidden: even then it must not acknowledge it.
    await page.evaluate(() => {
      calls.posts.length = 0;
      entries.push({id: 'new', position: 35, speaker: 'agent', text: 'New hidden reply', completion_sequence: 36});
      calls.streams.find(s => s.url.includes('/chat/events')).emit('chat', {cursor: 2, kind: 'changed'});
    });
    await page.waitForFunction(() => document.querySelector('[data-id="new"]'));
    await page.waitForTimeout(500);
    assert.equal(await page.evaluate(() => calls.posts.filter(p => p.url.endsWith('/read')).length), 0);
    await page.click('[data-chat-return]:visible');
    await page.waitForFunction(() => calls.posts.some(p => p.url.endsWith('/read') && p.body.sequence === 36));
    // A hidden terminal must not squeeze the live PTY to the minimum grid, even after a window resize/open event.
    await page.waitForTimeout(150);
    await page.evaluate(() => { calls.posts.length = 0; });
    await page.setViewportSize({width: 900, height: 700});
    await page.evaluate(() => {
      document.querySelector('#pane').style.width = '600px';
      calls.streams.find(s => s.url.endsWith('/stream')).onopen();
    });
    await page.waitForTimeout(200);
    assert.equal(await page.evaluate(() => calls.posts.filter(p => p.url.endsWith('/resize')).length), 0);
    await page.click('[data-terminal]:visible');
    await page.waitForFunction(() => calls.posts.some(p => p.url.endsWith('/resize')));
    assert.equal(await page.locator('[data-agent-refresh]').count(), 0);
    assert.equal(await page.evaluate(() => calls.streams.filter(s => s.url.endsWith('/stream') && !s.closed).length), 1);
    assert.equal(await page.evaluate(() => _term === savedTerminal), true);
    await page.click('[data-chat-return]:visible');
    assert.equal(await page.locator('[data-agent-refresh]').count(), 0);
    await page.evaluate(() => controller.dispose());
    assert.equal(await page.evaluate(() => calls.streams.every(s => s.closed)), true);
    assert.equal(await page.locator('.gchat-view-host').count(), 0);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('Terminal-first navigation is lazy and replacing or detaching the agent cleans up both viewers', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser, 'terminal');
    assert.equal(await page.evaluate(() => calls.loads), 0);
    await page.click('[data-chat-return]:visible');
    await page.locator('.gchat-msg').first().waitFor();
    await page.evaluate(() => { window.oldStreams = [...calls.streams]; controller = mount(); });
    assert.equal(await page.evaluate(() => oldStreams.every(s => s.closed)), true);
    assert.equal(await page.locator('.gchat-view-host').count(), 2);
    await page.click('[data-chat-return]:visible');
    await page.locator('.gchat-msg').first().waitFor();
    await page.evaluate(() => document.querySelector('#pane').replaceChildren());
    await page.waitForFunction(() => calls.streams.every(s => s.closed), null, {timeout: 4000});
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
