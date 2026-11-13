'use strict';
// A side chat (and a paired thread) takes over the Chat area under a small header; "Chat" hands the area back.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname, '..');

async function fixture(browser) {
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
    window.toast = () => {};
    window.EventSource = class extends EventTarget { static CLOSED = 2; close() {} };
    const binding = {provider: 'codex', conversation_id: 'conversation-1', binding_id: 'binding-1'};
    const entries = [{id: 'entry-1', position: 1, speaker: 'agent', text: 'Main chat message', completion_sequence: 1}];
    window.api = async () => ({session: {state: 'live', can_send: true, binding}, conversation: binding, entries: [...entries], cursor: 1, inputs: []});
    window.post = async (url, body) => ({last_read_sequence: body.sequence});
  });
  for (const file of ['safe-links', 'chat-view', 'slash-commands', 'xterm', 'terminal', 'global-chat']) await page.addScriptTag({path: path.join(root, 'js', file + '.js')});
  await page.evaluate(() => {
    window.controller = mountAgentView(document.querySelector('#pane'), {agent_id: 'agent-1', name: 'Demo'}, {
      terminal: {key: 'agent-1', base: '/terminal/agent-1'}, isCurrent: () => true, view: 'chat',
    });
    // A side chat stand-in: it may refuse to leave, like one with a message still sending.
    window.side = {allowLeave: true, disposed: 0};
    window.openSide = title => controller.openSide({title, mount(host, session) {
      host.innerHTML = `<p class="side-body">${title} body</p>`;
      session.setBeforeLeave(() => side.allowLeave);
      return () => { side.disposed++; };
    }});
  });
  await page.locator('.gchat-msg').first().waitFor();
  return {page, errors};
}
const takeover = page => page.evaluate(() => ({
  kind: document.querySelector('.gchat-takeover')?.dataset.takeover || null,
  title: document.querySelector('.gchat-takeover-title')?.textContent || '',
  body: document.querySelector('.side-body')?.textContent || '',
  chat: !!document.querySelector('.gchat')?.isConnected,
}));

test('a side chat takes over the Chat area and "Chat" brings the main chat back with its draft', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser);
    await page.locator('.gchat-composer textarea').fill('Unsent main draft');
    await page.evaluate(() => openSide('Pricing page'));
    assert.deepEqual(await takeover(page), {kind: 'side', title: 'Pricing page', body: 'Pricing page body', chat: false});
    await page.click('.gchat-takeover-back');
    await page.locator('.gchat-msg').first().waitFor();
    assert.deepEqual(await takeover(page), {kind: null, title: '', body: '', chat: true});
    assert.equal(await page.locator('.gchat-composer textarea').inputValue(), 'Unsent main draft');
    assert.equal(await page.evaluate(() => side.disposed), 1);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('a side chat that cannot leave yet stays put, and another one replaces it once it can', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser);
    await page.evaluate(() => openSide('First'));
    await page.evaluate(() => { side.allowLeave = false; });
    assert.equal(await page.evaluate(() => openSide('Second')), null);
    await page.click('.gchat-takeover-back');
    assert.deepEqual(await takeover(page), {kind: 'side', title: 'First', body: 'First body', chat: false});
    await page.evaluate(() => { side.allowLeave = true; });
    assert.notEqual(await page.evaluate(() => openSide('Second').then(Boolean)), false);
    assert.deepEqual(await takeover(page), {kind: 'side', title: 'Second', body: 'Second body', chat: false});
    assert.equal(await page.evaluate(() => side.disposed), 1);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('opening a side chat from the Terminal view shows it in the Chat area', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser);
    await page.click('[data-terminal]:visible');
    await page.evaluate(() => openSide('From terminal'));
    assert.deepEqual(await takeover(page), {kind: 'side', title: 'From terminal', body: 'From terminal body', chat: false});
    assert.equal(await page.locator('.gchat-takeover').isVisible(), true);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
