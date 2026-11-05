'use strict';
// node --test biz-dashboard/tests/*.test.js
// Runs the real `submit` of js/global-chat.js (cut out of the source) against fake session, catalog, view and network: no DOM,
// no server and no CLI. It checks what a slash command does to the draft, the attachments and the Terminal view.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const slashCommands = require('../js/slash-commands.js');

const source = fs.readFileSync(path.join(__dirname, '../js/global-chat.js'), 'utf8');
const between = (from, to) => {
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `global-chat.js no longer contains ${from}`);
  return source.slice(start, end);
};
const submitSource = between('  async function submit(text) {', '  // A definitely undelivered input is resent');
const refusalSource = between('const GLOBAL_CHAT_REFUSALS = new Set', 'const GLOBAL_CHAT_INPUT_LABEL');
const { GLOBAL_CHAT_REFUSALS, GLOBAL_CHAT_REFUSAL_TEXT } = new Function(`${refusalSource}; return {GLOBAL_CHAT_REFUSALS, GLOBAL_CHAT_REFUSAL_TEXT};`)();

const CODEX = [{name: '/compact', where: 'chat'}, {name: '/new', where: 'chat'}, {name: '/review', where: 'terminal'},
  {name: '/model', where: 'terminal'}, {name: '/status', where: 'terminal'}];
const CLAUDE = [{name: '/compact', where: 'chat'}, {name: '/model', where: 'terminal'}, {name: '/cost', where: 'terminal'},
  {name: '/git:sync', where: 'chat', source: 'yours'}];
const SCOPE = 'agent-1\ncodex\nconversation-1';

// One composer holding `draft` and one attached file, sending to `session` with `catalog` loaded (null: not loaded yet).
function composer({session, catalog, draft, refuse = null}) {
  const calls = {admitted: [], uploads: [], banners: [], toasts: [], terminal: 0, accepted: []};
  const sends = new Map(), inflight = new Map(), drafts = new Map([[SCOPE, draft]]);
  const file = {key: 'file-1', file: {name: 'notes.txt'}};
  const files = new Map([[SCOPE, [file]]]);
  const view = {draft, getDraft: () => view.draft, setDraft: value => { view.draft = value; }, scrollToBottom() {}};
  const names = ['composerScope', 'selectedFiles', 'canSend', 'session', 'slashCommands', 'commandCatalog', 'label', 'showBanner',
    'GLOBAL_CHAT_SENDS', 'GLOBAL_CHAT_INFLIGHT', 'GLOBAL_CHAT_DRAFTS', 'GLOBAL_CHAT_FILES', 'GLOBAL_CHAT_MAX', 'GLOBAL_CHAT_REFUSALS',
    'liveBinding', 'newRequestId', 'newestPosition', 'api', 'base', 'coreData', 'admit', 'globalChatAccept', 'options', 'toast',
    'live', 'view', 'takeInput', 'load', 'paintSession', 'refusalText'];
  const values = [SCOPE, () => files.get(SCOPE) || [], () => true, session, slashCommands, () => catalog, 'Ada',
    text => calls.banners.push(text), sends, inflight, drafts, files, 17000, GLOBAL_CHAT_REFUSALS,
    () => ({binding_id: 'binding-0001'}), () => 'request-1', () => 3,
    async (url, request) => { calls.uploads.push([url, request.headers['X-Chat-Binding']]); return {data: {reference: '[file saved]'}}; },
    '/root/agents/agent-1/chat', result => result.data,
    async (text, key, pin) => {
      calls.admitted.push([text, key, pin]);
      if (refuse) throw Object.assign(new Error('refused'), {code: refuse});
      return {input: {id: 'input-1', state: 'waiting', created_at: '2026-11-04T10:00:00Z'}};
    },
    (...args) => calls.accepted.push(args), {openTerminal: () => { calls.terminal += 1; }}, text => calls.toasts.push(text),
    () => true, view, () => {}, () => {}, () => {}, problem => GLOBAL_CHAT_REFUSAL_TEXT[problem.code] || "Couldn't send. Try again"];
  const submit = new Function(...names, `${submitSource}; return submit;`)(...values);
  return {submit, calls, sends, inflight, drafts, files, view, file};
}

const codexQueue = {provider: 'codex', transport: 'codex_queue', can_send: true};
const codexTerminal = {provider: 'codex', transport: 'terminal', can_send: true};
const claudeTerminal = {provider: 'claude', transport: 'terminal', can_send: true};

test('a known Codex command is refused for the shared server before any upload or send, leaving draft and attachments alone', async () => {
  for (const draft of ['/compact', '/compact keep decisions', '  /new', '/COMPACT']) {
    const chat = composer({session: codexQueue, catalog: CODEX, draft});
    await chat.submit(draft);
    assert.deepEqual(chat.calls.uploads, [], draft);
    assert.deepEqual(chat.calls.admitted, [], draft);
    assert.equal(chat.calls.banners.length, 1, draft);
    assert.match(chat.calls.banners[0], /runs only in Ada’s Terminal\. Open Terminal and run it there\./);
    assert.equal(chat.calls.terminal, 0);
    assert.equal(chat.sends.size + chat.inflight.size, 0);
    assert.equal(chat.drafts.get(SCOPE), draft);
    assert.equal(chat.view.getDraft(), draft);
    assert.deepEqual(chat.files.get(SCOPE), [chat.file]);
    assert.equal(chat.file.saved, undefined);
  }
});

test('other text and a Terminal session are sent as before', async () => {
  for (const [session, catalog, draft] of [[codexQueue, CODEX, '/tmp/foo'], [codexQueue, CODEX, 'hello /compact'],
    [codexQueue, CODEX, '/unknown'], [codexQueue, CODEX, 'plain words'], [codexTerminal, CODEX, '/compact'],
    [codexTerminal, CODEX, '/new keep this'], [claudeTerminal, CLAUDE, '/compact']]) {
    const chat = composer({session, catalog, draft});
    await chat.submit(draft);
    assert.deepEqual(chat.calls.banners, ['']);
    assert.equal(chat.calls.uploads.length, 1, draft);
    assert.deepEqual(chat.calls.admitted, [[`${draft}\n\n[file saved]`, 'request-1', 'binding-0001']], draft);
    assert.equal(chat.view.getDraft(), '', draft);
    assert.equal(chat.files.has(SCOPE), false, draft);
  }
});

test('before the catalog has loaded the command is sent and the backend refusal is shown with the draft kept', async () => {
  const chat = composer({session: codexQueue, catalog: null, draft: '/compact', refuse: 'terminal_required'});
  await chat.submit('/compact');
  assert.equal(chat.calls.admitted.length, 1);
  assert.deepEqual(chat.calls.banners, [GLOBAL_CHAT_REFUSAL_TEXT.terminal_required]);
  assert.match(GLOBAL_CHAT_REFUSAL_TEXT.terminal_required, /Terminal/);
  assert.ok(GLOBAL_CHAT_REFUSALS.has('terminal_required'));
  assert.equal(chat.view.getDraft(), '/compact');
  assert.equal(chat.drafts.get(SCOPE), '/compact');
  assert.deepEqual(chat.files.get(SCOPE), [chat.file]);
  assert.equal(chat.sends.size, 0);
  assert.equal(chat.calls.terminal, 0);
});

test('Claude /model with a name does not open Terminal; a bare /model and other menu commands still do', async () => {
  for (const [draft, opens] of [['/model sonnet', false], ['/model   opus', false], ['/model\nsonnet', false], ['/model', true],
    ['/model ', true], ['/cost', true], ['/cost now', true], ['/compact', false], ['/git:sync', false]]) {
    const chat = composer({session: claudeTerminal, catalog: CLAUDE, draft});
    await chat.submit(draft);
    assert.equal(chat.calls.admitted.length, 1, draft);
    assert.equal(chat.calls.terminal, opens ? 1 : 0, draft);
    assert.equal(chat.calls.toasts.length, opens ? 1 : 0, draft);
    if (opens) assert.match(chat.calls.toasts[0], /^Finish \/(model|cost) in Ada’s Terminal\.$/);
    assert.equal(chat.calls.accepted[0][4], draft.trim().startsWith('/'), draft);
  }
});

test('Codex menu commands in a Terminal session still open Terminal', async () => {
  const chat = composer({session: codexTerminal, catalog: CODEX, draft: '/model gpt-6'});
  await chat.submit('/model gpt-6');
  assert.equal(chat.calls.terminal, 1);
});
