'use strict';
// node --test dashboard/tests/*.test.js
// Runs the real `submit` of js/global-chat.js (cut out of the source) against fake session, catalog, view and network: no DOM,
// no server and no client program. It checks what a slash command does to the draft and the attachments.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const slashCommands = require('../js/slash-commands.js');
const agentTerminalView = require('../js/agent-terminal-view.js');

const source = fs.readFileSync(path.join(__dirname, '../js/global-chat.js'), 'utf8');
const between = (from, to) => {
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `global-chat.js no longer contains ${from}`);
  return source.slice(start, end);
};
const submitSource = between('  async function submit(text) {', '  // A definitely undelivered input is resent');
const refusalSource = between('const GLOBAL_CHAT_REFUSALS = new Set', 'const GLOBAL_CHAT_INPUT_LABEL');
const { GLOBAL_CHAT_REFUSALS, GLOBAL_CHAT_REFUSAL_TEXT } = new Function(`${refusalSource}; return {GLOBAL_CHAT_REFUSALS, GLOBAL_CHAT_REFUSAL_TEXT};`)();

const CODEX = [{name: '/compact', where: 'chat'}, {name: '/new', where: 'chat'}, {name: '/review', where: 'chat'},
  {name: '/model', where: 'chat'}, {name: '/status', where: 'chat'}];
const CLAUDE = [{name: '/compact', where: 'chat'}, {name: '/model', where: 'chat'}, {name: '/cost', where: 'chat'},
  {name: '/git:sync', where: 'chat', source: 'yours'}];
const SCOPE = 'agent-1\ncodex\nconversation-1';

// One composer holding `draft` and one attached file, sending to `session` with `catalog` loaded (null: not loaded yet).
// `terminals`: the page runs agents in terminals, so the new module is present (and the catalog was passed through it).
function composer({session, catalog, draft, refuse = null, terminals = false}) {
  const calls = {admitted: [], uploads: [], banners: [], accepted: [], toasts: [], terminal: 0};
  const sends = new Map(), inflight = new Map(), drafts = new Map([[SCOPE, draft]]);
  const file = {key: 'file-1', file: {name: 'notes.txt'}};
  const files = new Map([[SCOPE, [file]]]);
  const view = {draft, getDraft: () => view.draft, setDraft: value => { view.draft = value; }, scrollToBottom() {}};
  const names = ['composerScope', 'selectedFiles', 'canSend', 'session', 'slashCommands', 'commandCatalog', 'label', 'showBanner',
    'GLOBAL_CHAT_SENDS', 'GLOBAL_CHAT_INFLIGHT', 'GLOBAL_CHAT_DRAFTS', 'GLOBAL_CHAT_FILES', 'GLOBAL_CHAT_MAX', 'GLOBAL_CHAT_REFUSALS',
    'liveBinding', 'newRequestId', 'newestPosition', 'api', 'base', 'coreData', 'admit', 'globalChatAccept',
    'live', 'view', 'takeInput', 'load', 'paintSession', 'refusalText', 'opening', 'options', 'toast', 'agentTerminalView'];
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
    (...args) => calls.accepted.push(args),
    () => true, view, () => {}, () => {}, () => {}, problem => GLOBAL_CHAT_REFUSAL_TEXT[problem.code] || "Couldn't send. Try again",
    {holds: () => false, busy: scope => inflight.has(scope), after: () => null, start: () => false, restore() {}, end() {}, pin: async () => null},
    {openTerminal: () => { calls.terminal += 1; }}, text => calls.toasts.push(text), terminals ? agentTerminalView : undefined];
  const submit = new Function(...names, `${submitSource}; return submit;`)(...values);
  return {submit, calls, sends, inflight, drafts, files, view, file};
}

const codexManaged = {provider: 'codex', transport: 'managed', can_send: true};
const claudeManaged = {provider: 'claude', transport: 'managed', can_send: true};

test('other text, and a command on a session Qorqbot runs, are sent', async () => {
  for (const [session, catalog, draft] of [[codexManaged, CODEX, '/tmp/foo'], [codexManaged, CODEX, 'hello /compact'],
    [codexManaged, CODEX, '/unknown'], [codexManaged, CODEX, 'plain words'], [codexManaged, CODEX, '/compact'],
    [codexManaged, CODEX, '/new keep this'], [claudeManaged, CLAUDE, '/compact']]) {
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
  const chat = composer({session: codexManaged, catalog: null, draft: '/compact', refuse: 'managed_command'});
  await chat.submit('/compact');
  assert.equal(chat.calls.admitted.length, 1);
  assert.deepEqual(chat.calls.banners, [GLOBAL_CHAT_REFUSAL_TEXT.managed_command]);
  assert.equal(GLOBAL_CHAT_REFUSAL_TEXT.managed_command, 'This command isn’t available when Qorqbot runs the agent.');
  assert.doesNotMatch(GLOBAL_CHAT_REFUSAL_TEXT.managed_command, /terminal/i);
  assert.ok(GLOBAL_CHAT_REFUSALS.has('managed_command'));
  assert.equal(chat.view.getDraft(), '/compact');
  assert.equal(chat.drafts.get(SCOPE), '/compact');
  assert.deepEqual(chat.files.get(SCOPE), [chat.file]);
  assert.equal(chat.sends.size, 0);
});

test('a menu command is sent like any message: the composer opens nothing, whatever the catalog lists', async () => {
  for (const [draft, session, catalog] of [['/model sonnet', claudeManaged, CLAUDE], ['/model', claudeManaged, CLAUDE], ['/cost', claudeManaged, CLAUDE],
    ['/compact', claudeManaged, CLAUDE], ['/model gpt-6', codexManaged, CODEX]]) {
    const chat = composer({session, catalog, draft});
    await chat.submit(draft);
    assert.equal(chat.calls.admitted.length, 1, draft);
    assert.equal(chat.calls.banners.filter(Boolean).length, 0, draft);
    assert.equal(chat.calls.accepted[0][4], true, `${draft} is remembered as a command`);
  }
});

// Terminals mode: a terminal agent has a Terminal to finish a menu command in.
const claudeTerminal = {provider: 'claude', execution: 'terminal', state: 'live', can_send: true, launch_commands: ['claude']};

test('on a terminal agent a command that opens a menu is sent, and the Terminal opens after it', async () => {
  const catalog = agentTerminalView.catalog([{name: '/model', where: 'unavailable'}, {name: '/cost', where: 'unavailable'}, ...CLAUDE.filter(item => item.where === 'chat')]);
  for (const [draft, opens] of [['/model', true], ['/model sonnet', false], ['/cost', true], ['/compact', false], ['plain words', false]]) {
    const chat = composer({session: claudeTerminal, catalog, draft, terminals: true});
    await chat.submit(draft);
    assert.equal(chat.calls.admitted.length, 1, draft);
    assert.equal(chat.calls.banners.filter(Boolean).length, 0, `${draft} is not refused`);
    assert.equal(chat.calls.terminal, opens ? 1 : 0, draft);
    assert.equal(chat.calls.toasts.length, opens ? 1 : 0, draft);
    if (opens) assert.match(chat.calls.toasts[0], /^Finish \/(model|cost) in Ada’s Terminal\.$/);
  }
});

test('a managed agent never opens a Terminal, even when the module is present', async () => {
  for (const draft of ['/model', '/cost', '/compact']) {
    const chat = composer({session: claudeManaged, catalog: [{name: '/model', where: 'terminal'}, {name: '/cost', where: 'terminal'}, {name: '/compact', where: 'chat'}], draft, terminals: true});
    await chat.submit(draft);
    assert.equal(chat.calls.admitted.length, 1, draft);
    assert.equal(chat.calls.terminal, 0, draft);
    assert.deepEqual(chat.calls.toasts, [], draft);
  }
});

test('a terminal agent that is not running refuses with the terminal sentence and reloads', async () => {
  const chat = composer({session: claudeTerminal, catalog: [], draft: 'hello', refuse: 'terminal_offline', terminals: true});
  await chat.submit('hello');
  assert.ok(GLOBAL_CHAT_REFUSALS.has('terminal_offline'));
  assert.equal(chat.sends.size, 0, 'a refusal stores nothing, so its request is dropped');
  assert.equal(chat.view.getDraft(), 'hello');
});
