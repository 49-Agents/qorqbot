'use strict';
// node --test biz-dashboard/tests/global-chat-opening.test.js
// The owner's message to a sleeping or off agent: it is in the conversation at once, comes back to the composer when the open fails,
// is never shown twice, and a file sent to a sleeping agent goes the same way (js/global-chat.js `opening`, `openingBubble`, `submit`).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const slashCommands = require('../js/slash-commands.js');
const agentRuntime = require('../js/agent-runtime.js');

const source = fs.readFileSync(path.join(__dirname, '../js/global-chat.js'), 'utf8');
const between = (from, to) => {
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `global-chat.js no longer contains ${from}`);
  return source.slice(start, end);
};
const submitSource = between('  async function submit(text) {', '  // A definitely undelivered input is resent');
const openingSource = between('  const opening = {', '  let freshChoice = null;');
const bubbleSource = between('  function openingBubble() {', '  function presentReceipts');
const stateSource = between('const GLOBAL_CHAT_INPUT_STATE = {', '// The expanded text');
const entrySource = between('  const presentEntry = entry =>', '  const isAgentMessage');
const globalChatText = text => String(text ?? '').replace(/\r\n/g, '\n').trimEnd();

const asleep = {provider: 'claude', can_send: true, binding: null, state: 'offline', managed: {open: false, off: false, phase: 'idle'}};
const off = {...asleep, managed: {open: false, off: true, phase: 'idle'}};
const SCOPE = 'agent-1\nmanaged';

function chat({session = asleep, files = [], admit, binding = 'binding-9999', draft = 'hello'}) {
  const calls = {admitted: [], uploads: [], banners: [], accepted: []};
  const opened = new Map(), sends = new Map(), inflight = new Map(), drafts = new Map([[SCOPE, draft]]), fileStore = new Map([[SCOPE, files]]);
  const entries = new Map();
  const view = {draft, getDraft: () => view.draft, setDraft: v => { view.draft = v; }, scrollToBottom() {}};
  const state = {composerScope: SCOPE, session};
  const make = new Function('GLOBAL_CHAT_OPENING', 'agentId', 'agentRuntime', 'session', 'composerScope', 'GLOBAL_CHAT_DRAFTS', 'view', 'live', 'coreData', 'api', 'stateUrl', 'reads',
    'globalChatText', 'entries', 'GLOBAL_CHAT_INPUT_STATE',
    `${stateSource.replace('const GLOBAL_CHAT_INPUT_STATE', 'const UNUSED')}; ${openingSource}; ${bubbleSource}; return {opening, openingBubble};`);
  const api = async (url, request) => {
    if (request?.method === 'POST') { calls.uploads.push({url, headers: request.headers}); return {data: {reference: '[file saved]'}}; }
    return {data: {session: {binding: binding ? {binding_id: binding} : null}}};
  };
  const states = new Function(`${stateSource}; return GLOBAL_CHAT_INPUT_STATE;`)();
  const parts = make(opened, 'agent-1', agentRuntime, session, SCOPE, drafts, view, () => true, r => r.data, api, () => '/state', {signal: null}, globalChatText, entries, states);
  const names = ['composerScope', 'selectedFiles', 'canSend', 'session', 'slashCommands', 'commandCatalog', 'label', 'showBanner', 'GLOBAL_CHAT_SENDS', 'GLOBAL_CHAT_INFLIGHT',
    'GLOBAL_CHAT_DRAFTS', 'GLOBAL_CHAT_FILES', 'GLOBAL_CHAT_MAX', 'GLOBAL_CHAT_REFUSALS', 'liveBinding', 'newRequestId', 'newestPosition', 'api', 'base', 'coreData', 'admit',
    'globalChatAccept', 'live', 'view', 'takeInput', 'load', 'paintSession', 'refusalText', 'opening', 'agentRuntime'];
  const submit = new Function(...names, `${submitSource}; return submit;`)(SCOPE, () => fileStore.get(SCOPE) || [], () => true, session, slashCommands, () => [], 'Ada',
    t => calls.banners.push(t), sends, inflight, drafts, fileStore, 17000, new Set(['session_closed', 'open_timeout', 'driver_unavailable']), () => null, () => 'request-1', () => 3, api, '/chat', r => r.data,
    async (text, key, pin) => { calls.admitted.push([text, key, pin]); return admit(text, key, pin); },
    (...a) => calls.accepted.push(a), () => true, view, () => {}, () => {}, () => {}, p => p.code, parts.opening, agentRuntime);
  return {...parts, submit, calls, view, drafts, entries, sends, opened, state};
}
const later = () => new Promise(resolve => setImmediate(resolve));
const ok = async () => ({input: {id: 'input-1', state: 'waiting', created_at: '2026-11-19T10:00:00Z'}});

test('a message to a sleeping or off agent is a pending bubble at once, the composer is empty meanwhile, and it turns into the accepted bubble', async () => {
  for (const session of [asleep, off]) {
    let release;
    const c = chat({session, admit: () => new Promise(resolve => { release = () => resolve(ok()); })});
    const sending = c.submit('hello');
    await later();
    assert.equal(c.opened.has('agent-1'), true);
    assert.equal(c.view.getDraft(), '', 'the composer is cleared, not filled twice');
    const [bubble] = c.openingBubble();
    assert.equal(bubble.model.text, 'hello');
    assert.equal(bubble.model.speaker, 'user');
    assert.equal(bubble.model.state, 'submitting');
    assert.equal(bubble.model.stateLabel, 'Sending…');
    assert.equal(c.openingBubble().length, 1);
    release(); await sending;
    assert.equal(c.opened.has('agent-1'), false);
    assert.deepEqual(c.openingBubble(), []);
    assert.equal(c.calls.accepted.length, 1);
    assert.deepEqual(c.calls.banners, ['']);
    assert.equal(c.view.getDraft(), '');
  }
});

test('a failed open puts the message back in the composer with the refusal, and nothing stays shown', async () => {
  const c = chat({admit: async () => { throw Object.assign(new Error('x'), {code: 'driver_unavailable'}); }});
  await c.submit('hello');
  assert.equal(c.view.getDraft(), 'hello');
  assert.equal(c.drafts.get(SCOPE), 'hello');
  assert.deepEqual(c.openingBubble(), []);
  assert.equal(c.opened.size, 0);
  assert.match(c.calls.banners.at(-1), /driver_unavailable|Couldn|Claude/);
  assert.equal(c.calls.accepted.length, 0);
});

test('a "still starting" refusal also restores the text and keeps the request key for the retry', async () => {
  const c = chat({admit: async () => { throw Object.assign(new Error('x'), {code: 'open_timeout', keepAttempt: true}); }});
  await c.submit('hello');
  assert.equal(c.view.getDraft(), 'hello');
  assert.equal(c.sends.get(SCOPE).key, 'request-1');
});

test('text typed while the session opens is kept, with the failed text ahead of it', async () => {
  let fail;
  const c = chat({admit: () => new Promise((_, reject) => { fail = () => reject(Object.assign(new Error('x'), {code: 'driver_unavailable'})); })});
  const sending = c.submit('hello');
  await later();
  c.view.setDraft('and also');
  fail(); await sending;
  assert.equal(c.view.getDraft(), 'hello\nand also');
});

test('the bubble is hidden the moment the transcript already holds the message, so it is never shown twice', async () => {
  let release;
  const c = chat({admit: () => new Promise(resolve => { release = () => resolve(ok()); })});
  const sending = c.submit('hello');
  await later();
  assert.equal(c.openingBubble().length, 1);
  c.entries.set('e1', {id: 'e1', speaker: 'agent', text: 'hello', position: 9, timestamp: new Date().toISOString()});
  assert.equal(c.openingBubble().length, 1, 'an agent entry with the same text is not the owner’s message');
  c.entries.set('e0', {id: 'e0', speaker: 'user', text: 'hello', position: 2, timestamp: new Date().toISOString()});
  assert.equal(c.openingBubble().length, 1, 'an older owner entry is not this message');
  c.entries.set('e2', {id: 'e2', speaker: 'user', text: 'hello', position: 10, timestamp: new Date().toISOString()});
  assert.equal(c.openingBubble().length, 0);
  release(); await sending;
});

test('a message to an open session shows no opening bubble and keeps its draft until it is accepted', async () => {
  const live = {...asleep, binding: {binding_id: 'binding-1', provider: 'claude', conversation_id: 'c'}, state: 'live', managed: {open: true, off: false, phase: 'idle'}};
  let release;
  const c = chat({session: live, admit: () => new Promise(resolve => { release = () => resolve(ok()); })});
  const sending = c.submit('hello');
  await later();
  assert.equal(c.opened.size, 0);
  assert.deepEqual(c.openingBubble(), []);
  release(); await sending;
});

test('a file for a sleeping agent is uploaded without a binding, the session that came up is read, and the message is pinned to it', async () => {
  const file = {key: 'file-1', file: {name: 'notes.txt'}};
  const c = chat({files: [file], draft: 'see this', admit: ok});
  await c.submit('see this');
  assert.equal(c.calls.uploads.length, 1);
  assert.equal('X-Chat-Binding' in c.calls.uploads[0].headers, false);
  assert.deepEqual(c.calls.admitted, [['see this\n\n[file saved]', 'request-1', 'binding-9999']]);
  assert.equal(file.savedPin, 'binding-9999', 'a retry does not upload the file again');
});

test('several files for a sleeping agent: the first opens the session, the next ones and the message name the binding that came up', async () => {
  const files = [{key: 'file-1', file: {name: 'a.txt'}}, {key: 'file-2', file: {name: 'b.txt'}}];
  const c = chat({files, draft: 'see these', admit: ok});
  await c.submit('see these');
  assert.deepEqual(c.calls.uploads.map(item => item.headers['X-Chat-Binding']), [undefined, 'binding-9999']);
  assert.deepEqual(c.calls.admitted.map(item => item[2]), ['binding-9999']);
});

test('a file alone for a sleeping agent shows its name in the pending bubble', async () => {
  const file = {key: 'file-1', file: {name: 'plan.pdf'}};
  let release;
  const c = chat({files: [file], draft: '', admit: () => new Promise(resolve => { release = () => resolve(ok()); })});
  const sending = c.submit('');
  await later(); await later();
  assert.equal(c.openingBubble()[0].model.text, 'plan.pdf');
  release(); await sending;
});

test('Attach, drop and paste follow Send: enabled whenever a message can be sent, asleep or off included', () => {
  assert.doesNotMatch(source, /filesDisabled:[^\n]*canOpen/);
  assert.match(source, /filesDisabled: !enabled \|\| sending,/);
  const onFiles = between('    onFiles: files => {', '    onAttachmentRemove');
  assert.doesNotMatch(onFiles, /canOpen/);
});

test('an entry that records a Stop is a quiet centred notice, not the owner’s message', () => {
  const globalChatNoticeLabel = () => '', globalChatMarkerOnly = () => false;
  const present = new Function('globalChatNoticeLabel', 'globalChatMarkerOnly', `${entrySource}; return presentEntry;`)(globalChatNoticeLabel, globalChatMarkerOnly);
  for (const text of ['[Request interrupted by user]', '[Request interrupted by user for tool use]', '  [Request interrupted by user]\n']) {
    const row = present({id: 'e1', speaker: 'user', text, timestamp: '2026-11-19T10:00:00Z', position: 4});
    assert.equal(row.speaker, 'notice', text);
    assert.equal(row.text, 'Stopped.');
    assert.equal(row.notice.markerOnly, false);
    assert.equal(row.notice.label, '');
  }
  for (const [speaker, text] of [['user', 'please [Request interrupted by user] ok'], ['user', 'stop'], ['agent', '[Request interrupted by user]']]) {
    assert.equal(present({id: 'e2', speaker, text, position: 5}).speaker, speaker, text);
  }
});

test('the question card never covers the last message: the end of the list is read before the card appears and kept after the repaint', () => {
  const paint = between('  function paint(patch = {}, scroll', '  const paintEntries');
  const read = paint.indexOf('view?.isAtBottom'), sync = paint.indexOf('syncManaged()'), render = paint.indexOf('view.render('), keep = paint.indexOf('view.scrollToBottom()');
  assert.ok(read > 0 && read < sync && sync < render && render < keep, 'read the position, show the card, repaint, then keep the end in view');
  const css = fs.readFileSync(path.join(__dirname, '../css/managed-chat.css'), 'utf8');
  assert.match(css, /\.mchat-card \{[^}]*max-height: 45vh/);
  assert.match(css, /\.mchat-card-body \{[^}]*overflow-y: auto/);
  const card = agentRuntime.cardHtml({request_id: 'r1', questions: [{question: 'Q?', options: ['a']}]}, 'Ada');
  assert.match(card, /<div class="mchat-card-body">[\s\S]*<\/div>\s*<p class="mchat-card-error"/);
  assert.match(card, /data-answer="submit"/);
});
