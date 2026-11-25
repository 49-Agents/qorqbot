'use strict';
// node --test dashboard/tests/global-chat-draft-compaction.test.js
// The owner's unsent draft and attached files survive a compaction: when the agent's conversation is compacted Chat binds its composer
// to the fresh conversation, and what was typed (and attached) moves with it instead of staying behind with the closed conversation
// (js/global-chat.js `bindComposer`, `carryComposer`, `saveDraft`, `load`). Only sending or clearing it removes it.
// The real code is cut out of the source and run against a fake network and view: no DOM and no server.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../js/global-chat.js'), 'utf8');
const between = (from, to) => {
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `global-chat.js no longer contains ${from}`);
  return source.slice(start, end);
};
const line = pattern => source.match(pattern)[0];
const moduleSource = [line(/^const GLOBAL_CHAT_ECHO_SLACK = .*;$/m), line(/^const globalChatText = .*;$/m), line(/^const GLOBAL_CHAT_PAGE = .*;$/m),
  line(/^const GLOBAL_CHAT_OLDER = .*;$/m), line(/^const GLOBAL_CHAT_LINK_WAIT = .*;$/m), line(/^const GLOBAL_CHAT_LOADED_MAX = .*;$/m),
  line(/^const GLOBAL_CHAT_LOADED_KEEP = .*;$/m), between('const globalChatKey = ', 'function globalChatReadable'),
  between('const GLOBAL_CHAT_INPUT_LABEL = ', 'const globalChatBase')].join('\n');
const mountSource = [between('  const entries = new Map();', '  let session = null'), between('  function renderedBoundary() {', '  function scheduleRead'),
  between('  const presentEntry = entry =>', '  // The live session'), between('  function acceptedBubbles(visibleIds) {', '  function presentReceipts'),
  line(/^ {2}const selectedFiles = .*;$/m), between('  const paintEntries = ', '  const paintInputs'), between('  function invalidatePage() {', '  const refusalText'),
  between('  const stateUrl = ', '  // ---- the event stream')].join('\n');
const mountStart = between('  // A new mount shows the agent', '  paint();\n  // Slash commands');

const A = {conversation_id: 'conv-a', provider: 'claude', continues: null};
const B = {conversation_id: 'conv-b', provider: 'claude', continues: {conversation_id: 'conv-a', provider: 'claude', at: '2026-11-25T10:00:00Z'}};
const entry = (id, position) => ({id, position, speaker: 'agent', text: id, timestamp: '2026-11-25T09:30:00Z'});
const flush = () => new Promise(resolve => setImmediate(resolve));
const SESSION = {provider: 'claude', can_send: true, managed: {open: true}};

// The module-level stores, shared by every mount of one page (as in the browser).
function page() {
  return {drafts: new Map(), files: new Map(), composer: new Map(), inflight: new Map(), sends: new Map()};
}
// One mounted Chat of agent-1 on `stores`. `state()` is the backend's current answer; the view holds a real draft.
function chat(stores, state) {
  const view = {draft: '', sets: 0, visible: [], getVisibleKeys: () => [], isAtBottom: () => true,
    setDraft(text) { if (view.draft !== text) { view.draft = text; view.sets++; } }, getDraft: () => view.draft};
  const network = async url => {
    if (url.includes('conversation=')) return {data: {conversation: A, entries: [entry('a1', 5)], older: null}};
    return {data: {session: SESSION, inputs: [], cursor: 1, last_read_sequence: 0, ...state()}};
  };
  const make = new Function('api', 'view', 'agentGlyph', 'GLOBAL_CHAT_ACCEPTED', 'GLOBAL_CHAT_OPENING', 'GLOBAL_CHAT_HIDDEN', 'GLOBAL_CHAT_DRAFTS',
    'GLOBAL_CHAT_FILES', 'GLOBAL_CHAT_COMPOSER', 'GLOBAL_CHAT_INFLIGHT', 'GLOBAL_CHAT_SENDS', `
    ${moduleSource}
    const agentId = 'agent-1', base = '/root/agents/agent-1/chat', reads = {signal: null}, coreData = result => result.data, contextStatus = null, timelineBlock = null;
    const agentRuntime = {canOpen: session => !!session?.managed};
    let session = null, conversation = null, inputs = [], older = null, shown, clears = 0, cursor = 0, lastRead = 0, composerScope = null;
    let stream = {}, readTimer = null, loading = false, again = false, generation = 0, reading = 0, olderBusy = false, alive = true;
    const live = () => alive, connect = () => {}, retry = () => {}, paintSession = () => {}, showBanner = () => {}, paint = () => {};
    ${mountStart}
    ${mountSource}
    return {load, invalidatePage, saveDraft, selectedFiles, scope: () => composerScope, type(text) { view.setDraft(text); saveDraft(); }};`);
  const mount = make(network, view, () => '', new Map(), new Map(), new Set(), stores.drafts, stores.files, stores.composer, stores.inflight, stores.sends);
  return {...mount, view};
}

test('a compaction keeps the draft and the attached files in the composer', async () => {
  const stores = page();
  let state = {conversation: A, entries: [entry('a1', 5)], older: null};
  const c = chat(stores, () => state);
  await c.load(); await flush();
  assert.equal(c.scope(), 'agent-1\nclaude\nconv-a');
  c.type('half-written message');
  const file = {key: 'f1', file: {name: 'plan.pdf'}};
  stores.files.set(c.scope(), [file]);
  const sets = c.view.sets;

  state = {conversation: B, entries: [entry('b1', 2)], older: null};      // the compaction opens conv-b
  await c.load(); await flush();
  assert.equal(c.scope(), 'agent-1\nclaude\nconv-b', 'the composer follows the fresh conversation');
  assert.equal(c.view.draft, 'half-written message', 'the draft is still in the composer');
  assert.equal(c.view.sets, sets, 'the composer is not touched, so its caret stays where it was');
  assert.deepEqual(c.selectedFiles(), [file], 'the attached files stay attached');
  assert.equal(stores.drafts.get('agent-1\nclaude\nconv-a'), undefined, 'the draft moved; it is not kept twice');

  c.type('half-written message, finished');
  assert.equal(stores.drafts.get('agent-1\nclaude\nconv-b'), 'half-written message, finished');
});

test('a draft is cleared only when the owner clears it, and an empty composer stays empty after a compaction', async () => {
  const stores = page();
  let state = {conversation: A, entries: [], older: null};
  const c = chat(stores, () => state);
  await c.load();
  c.type('something'); c.type('');
  state = {conversation: B, entries: [], older: null};
  await c.load(); await flush();
  assert.equal(c.view.draft, '');
  assert.equal(stores.drafts.size, 0);
});

test('a mount opened after the compaction (the pane was re-rendered) shows the draft typed before it', async () => {
  const stores = page();
  let state = {conversation: A, entries: [], older: null};
  const first = chat(stores, () => state);
  await first.load();
  first.type('typed before the compaction');
  first.saveDraft();                                                       // dispose()
  state = {conversation: B, entries: [], older: null};
  const second = chat(stores, () => state);
  assert.equal(second.view.draft, 'typed before the compaction', 'shown at once on mount');
  second.saveDraft();                                                      // left before its first state arrived
  await second.load(); await flush();
  assert.equal(second.scope(), 'agent-1\nclaude\nconv-b');
  assert.equal(second.view.draft, 'typed before the compaction');
});

test('a moment without a conversation keeps what is typed, and it reaches the conversation that follows', async () => {
  const stores = page();
  let state = {conversation: A, entries: [], older: null};
  const c = chat(stores, () => state);
  await c.load();
  c.type('draft');
  state = {conversation: null, session: {provider: 'claude', can_send: true}, entries: [], older: null};
  await c.load();
  assert.equal(c.scope(), null);
  assert.equal(c.view.draft, 'draft', 'the composer keeps the text');
  c.type('draft, more');
  state = {conversation: B, entries: [], older: null};
  await c.load(); await flush();
  assert.equal(c.view.draft, 'draft, more');
  assert.equal(stores.drafts.get('agent-1\nclaude\nconv-b'), 'draft, more');
});

test('a message still being sent from the closed conversation is not carried into the fresh one', async () => {
  const stores = page();
  let state = {conversation: A, entries: [], older: null};
  const c = chat(stores, () => state);
  await c.load();
  c.type('on its way');
  stores.inflight.set('agent-1\nclaude\nconv-a', {text: 'on its way', promise: new Promise(() => {})});
  state = {conversation: B, entries: [], older: null};
  await c.load(); await flush();
  assert.equal(c.view.draft, '', 'the sent message is not offered again in the fresh conversation');
  assert.equal(stores.drafts.get('agent-1\nclaude\nconv-a'), 'on its way', 'its own outcome clears it where it was sent');
});

test('a draft the fresh conversation already holds is kept over the carried one', async () => {
  const stores = page();
  stores.drafts.set('agent-1\nclaude\nconv-b', 'written in conv-b');
  let state = {conversation: A, entries: [], older: null};
  const c = chat(stores, () => state);
  await c.load();
  c.type('written in conv-a');
  state = {conversation: B, entries: [], older: null};
  await c.load(); await flush();
  assert.equal(c.view.draft, 'written in conv-b');
});

test('a restored installation (the page is invalidated) keeps the draft', async () => {
  const stores = page();
  let state = {conversation: A, entries: [], older: null};
  const c = chat(stores, () => state);
  await c.load();
  c.type('keep me');
  c.invalidatePage();
  assert.equal(c.view.draft, 'keep me');
  state = {conversation: B, entries: [], older: null};
  await c.load(true); await flush();
  assert.equal(c.view.draft, 'keep me');
});
