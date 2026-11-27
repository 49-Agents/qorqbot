'use strict';
// node --test dashboard/tests/global-chat-mid-work.test.js
// Only two things from an agent are conversation messages: the reply that ends a turn and a card's message. Text an agent writes
// mid-work is left out of Chat (and drawn only in Testing mode); while the agent works, the newest such line is the activity line's
// note (js/global-chat.js `isMidWork`, `shownInChat`, `midWorkNote`, `groupActivity`, `loadOlder`, `fillShown`, `trimLoaded`).
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
const moduleSource = [line(/^const GLOBAL_CHAT_ECHO_SLACK = .*;$/m), line(/^const globalChatText = .*;$/m), line(/^const GLOBAL_CHAT_PAGE = .*;$/m), line(/^const GLOBAL_CHAT_OLDER = .*;$/m), line(/^const GLOBAL_CHAT_LINK_WAIT = .*;$/m), line(/^const GLOBAL_CHAT_LOADED_MAX = .*;$/m), line(/^const GLOBAL_CHAT_LOADED_KEEP = .*;$/m),
  between('const globalChatKey = ', 'function globalChatReadable'), between('const GLOBAL_CHAT_INPUT_LABEL = ', 'const globalChatBase')].join('\n');
const mountSource = [between('  const entries = new Map();', '  let session = null'), between('  function renderedBoundary() {', '  function scheduleRead'),
  between('  const presentEntry = entry =>', '  // The live session'), between('  function acceptedBubbles(visibleIds) {', '  function presentReceipts'),
  between('  const paintEntries = ', '  const paintInputs'), between('  function invalidatePage() {', '  // ---- composer'),
  between('  const stateUrl = ', '  // ---- the event stream')].join('\n');

const C = {conversation_id: 'conv-c', provider: 'codex', continues: null};
const at = '2026-11-21T09:30:00Z';
const user = (id, position) => ({id, position, speaker: 'user', text: id, timestamp: at, partial: null});
// Claude: mid-work text is `partial: true`, the turn's final reply `partial: false` with its completion.
const claudeWork = (id, position, text = id) => ({id, position, speaker: 'agent', text, timestamp: at, partial: true, completion_sequence: null});
const reply = (id, position, sequence) => ({id, position, speaker: 'agent', text: id, timestamp: at, partial: false, completion_sequence: sequence});
// Codex: commentary is `partial: null`; a turn without a final answer later promotes its last message (a completion, same id).
const codexWork = (id, position, text = id) => ({id, position, speaker: 'agent', text, timestamp: at, partial: null, completion_sequence: null});
const card = (id, position) => ({id, position, speaker: 'agent', text: id, timestamp: at, partial: false, completion_sequence: null, references: []});
const nudge = (id, position) => ({id, position, speaker: 'notice', text: '', timestamp: at, system: {verified: true, input_type: 'inbox_nudge'}});
const sent = (id, position) => ({id, position, speaker: 'notice', text: '', timestamp: at, agent_message: {recipients: [{agent_id: 'w', display_name: 'Worker'}]}});
const flush = () => new Promise(resolve => setImmediate(resolve));

function chat(network, {testing = false} = {}) {
  const calls = {urls: [], paints: [], banners: []};
  const view = {visible: [], getVisibleKeys: () => view.visible, bottom: true, isAtBottom: () => view.bottom, setDraft() {}, getDraft: () => ''};
  const mode = {testing};
  const api = async url => { calls.urls.push(url); return {data: await network(url)}; };
  const make = new Function('api', 'view', 'calls', 'agentGlyph', 'GLOBAL_CHAT_ACCEPTED', 'GLOBAL_CHAT_OPENING', 'GLOBAL_CHAT_HIDDEN', 'mode', `
    ${moduleSource}
    const agentId = 'agent-1', base = '/root/agents/agent-1/chat', reads = {signal: null}, coreData = result => result.data, contextStatus = null, timelineBlock = null;
    const testingModeEnabled = () => mode.testing;
    let session = null, conversation = null, inputs = [], older = null, shown, clears = 0, cursor = 0, lastRead = 0, composerScope = null;
    let stream = {}, readTimer = null, loading = false, again = false, generation = 0, reading = 0, olderBusy = false, alive = true;
    const live = () => alive, bindComposer = () => {}, saveDraft = () => {}, connect = () => {}, retry = () => {}, paintSession = () => {};
    const showBanner = text => calls.banners.push(text);
    const paint = (patch = {}, scroll) => { if (patch.entries) calls.paints.push({rows: patch.entries, scroll, busy: olderBusy}); };
    ${mountSource}
    return {load, loadOlder, trimLoaded, fillShown, midWorkNote, renderedBoundary, acceptedBubbles, olderAbove, entries, earlier,
      keys: () => orderedEntries(loadedParts()).map(row => row.key), rows: () => orderedEntries(loadedParts())};`);
  return {...make(api, view, calls, () => '<svg></svg>', new Map(), new Map(), new Set(), mode), calls, view, mode};
}
// One conversation: `state()` answers the state read, `pages[before]` a history read.
const single = (state, pages = {}) => chat(url => {
  const query = new URL(url, 'http://chat.test').searchParams;
  if (url.includes('/history?')) return pages[query.get('before')] || {entries: [], older: null};
  return {conversation: C, session: {provider: 'codex'}, inputs: [], cursor: 1, last_read_sequence: 0, ...state()};
});

test('normal mode leaves mid-work lines out; the final reply and a card message stay; Testing mode draws them as before', async () => {
  const page = [user('u1', 10), claudeWork('w1', 20), codexWork('w2', 30), card('k1', 40), claudeWork('w3', 50), reply('r1', 60, 1)];
  const c = single(() => ({entries: page, older: null}));
  await c.load();
  assert.deepEqual(c.keys(), ['u1', 'k1', 'r1']);
  c.mode.testing = true;
  assert.deepEqual(c.keys(), ['u1', 'w1', 'w2', 'k1', 'w3', 'r1'], 'Testing mode: every entry, as today');
  const rows = c.rows();
  assert.equal(rows[1].partial, true, 'Claude mid-work keeps its partial style in Testing mode');
  assert.equal(rows[2].partial, false, 'Codex commentary renders as before');
  // the toggle repaints through `paintEntries` (the subscriber), and the hidden page then reads nothing more
  // an earlier conversation kept above (a compaction) is drawn by the same rule
  c.mode.testing = false;
  c.earlier.push({conversation: {conversation_id: 'conv-b', provider: 'claude', continues: null}, entries: new Map([['b1', user('b1', 5)], ['bw', claudeWork('bw', 6)], ['br', reply('br', 7, 0)]]), older: null});
  assert.deepEqual(c.keys(), ['b1', 'br', 'u1', 'k1', 'r1']);
  assert.match(source, /testingModeSubscribe\(\(\) => \{ if \(live\(\)\) \{ paintEntries\('preserve'\); fillShown\(\); \} \}\)/);
});

test('a hidden mid-work line never splits an activity run', async () => {
  const c = single(() => ({entries: [user('u1', 10), sent('m1', 20), codexWork('w1', 30), sent('m2', 40), reply('r1', 50, 1)], older: null}));
  await c.load();
  const rows = c.rows();
  assert.deepEqual(rows.map(row => row.key), ['u1', 'm1', 'r1']);
  assert.equal(rows[1].notice.activity.recipients.length, 1, 'm1 and m2 are one run');
});

test('a Codex line with partial null is hidden until its completion is recorded, then shown on the next read without a reload', async () => {
  let state = {entries: [user('u1', 10), codexWork('w1', 20)], older: null};
  const c = single(() => state);
  await c.load();
  assert.deepEqual(c.keys(), ['u1']);
  // the turn ends without a final answer: the server binds the completion to the same entry, and the "changed" event reads the state
  state = {entries: [user('u1', 10), {...codexWork('w1', 20), completion_sequence: 7, completion_id: 'reply-7'}], older: null};
  await c.load();
  assert.deepEqual(c.keys(), ['u1', 'w1']);
  assert.equal(c.calls.paints.at(-1).scroll, 'auto', 'an ordinary repaint, as for any new message');
  c.view.visible = ['u1', 'w1'];
  assert.equal(c.renderedBoundary(), 7, 'the promoted reply counts for the read position once it is on screen');
});

test('the read position counts only rendered replies: hidden mid-work lines carry none', async () => {
  const c = single(() => ({entries: [user('u1', 10), claudeWork('w1', 20), reply('r1', 30, 4), claudeWork('w2', 40)], older: null}));
  await c.load();
  c.view.visible = c.keys();
  assert.equal(c.renderedBoundary(), 4);
});

test('the activity note is the newest entry when it is a mid-work line, as one line of plain text; anything newer clears it', async () => {
  let state = {entries: [user('u1', 10), claudeWork('w1', 20, '**Startup** skills file is `required`;\n\n- let me [read it](https://example.com/x) now…')], older: null};
  const c = single(() => state);
  await c.load();
  assert.deepEqual(c.midWorkNote(), {text: 'Startup skills file is required; let me read it now…', title: 'Startup skills file is required; let me read it now…'});
  const long = 'word '.repeat(200);
  state = {entries: [...state.entries, codexWork('w2', 30, long)], older: null};
  await c.load();
  const note = c.midWorkNote();
  assert.equal(note.text.length, 300);
  assert.ok(note.text.endsWith('…'));
  assert.equal(note.title, long.trim(), 'the whole line is in the title');
  state = {entries: [...state.entries, codexWork('w3', 40, 'reading file_name and snake_case_value')], older: null};
  await c.load();
  assert.equal(c.midWorkNote().text, 'reading file_name and snake_case_value', 'underscores inside words stay');
  state = {entries: [...state.entries, card('k1', 50)], older: null};
  await c.load();
  assert.equal(c.midWorkNote(), null, 'a card message is newer');
  state = {entries: [...state.entries, reply('r1', 60, 1)], older: null};
  await c.load();
  assert.equal(c.midWorkNote(), null, 'the turn ended');
  // Shown only while the agent works and on the live conversation (`paint`); the bar shows it only beside a step.
  assert.match(between('  function paint(patch = {}, scroll', '  const paintEntries'), /syncManaged\(working && viewingLive\(\) \? midWorkNote\(\) : null\)/);
});

test('an older page made only of hidden lines reads the next one at once: no stall while more lies above', async () => {
  const pages = {
    100: {entries: [codexWork('w5', 50), codexWork('w6', 60), codexWork('w7', 70)], older: 50},
    50: {entries: [claudeWork('w3', 30), claudeWork('w4', 40)], older: 30},
    30: {entries: [user('u1', 10), reply('r0', 20, 1)], older: null},
  };
  const c = single(() => ({entries: [user('u2', 100), reply('r2', 110, 2)], older: 100}), pages);
  await c.load(); await flush();
  assert.deepEqual(c.keys(), ['u2', 'r2']);
  await c.loadOlder();
  assert.deepEqual(c.calls.urls.slice(1).map(url => new URL(url, 'http://x').searchParams.get('before')), ['100', '50', '30']);
  assert.deepEqual(c.keys(), ['u1', 'r0', 'u2', 'r2']);
  assert.equal(c.olderAbove(), false);
});

test('a state page of hidden lines alone brings in older pages until something is drawn', async () => {
  const pages = {200: {entries: [codexWork('w1', 150)], older: 150}, 150: {entries: [user('u1', 100), codexWork('w0', 120)], older: 100},
    100: {entries: [user('u0', 50)], older: null}};
  const c = single(() => ({entries: [codexWork('w2', 200), codexWork('w3', 210)], older: 200}), pages);
  await c.load(); await flush(); await flush();
  assert.deepEqual(c.keys(), ['u1'], 'reading stops at the first page that draws something');
  assert.equal(c.olderAbove(), true, 'the rest is still offered by scrolling up');
  assert.equal(c.calls.urls.length, 3);
  // Testing mode: the same page is drawn, so nothing more is read.
  const t = single(() => ({entries: [codexWork('w2', 200)], older: 200}), pages);
  t.mode.testing = true;
  await t.load(); await flush();
  assert.equal(t.calls.urls.length, 1);
});

test('the bottom trim counts and keeps only drawn entries, so hidden lines never push the drawn ones out', async () => {
  const c = single(() => ({entries: [user('u1', 10)], older: null}));
  await c.load(); await flush();
  c.entries.clear();
  for (let i = 0; i < 40; i++) c.entries.set(`u${i}`, user(`u${i}`, i * 10));
  for (let i = 0; i < 200; i++) c.entries.set(`w${i}`, codexWork(`w${i}`, 1000 + i));
  assert.equal(c.trimLoaded(), false, '40 drawn entries are under the limit, whatever is hidden');
  for (let i = 40; i < 120; i++) c.entries.set(`u${i}`, user(`u${i}`, i * 10));
  assert.equal(c.trimLoaded(), true);
  assert.equal(c.keys().length, 50, 'the newest 50 drawn entries stay');
  assert.ok(c.keys().every(key => key.startsWith('u')));
});
