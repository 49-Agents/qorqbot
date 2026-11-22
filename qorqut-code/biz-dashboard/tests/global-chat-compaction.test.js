'use strict';
// node --test biz-dashboard/tests/global-chat-compaction.test.js
// A compaction closes an agent's conversation and opens a fresh one. Chat follows the fresh one and keeps the earlier ones reachable
// above it: a "Context compacted" marker at the top of a continued conversation, and "Load older messages" going on into the one
// before (js/global-chat.js `earlier`, `compactionMarker`, `orderedEntries`, `loadedParts`, `olderAbove`, `load`, `loadOlder`).
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
const moduleSource = [line(/^const GLOBAL_CHAT_ECHO_SLACK = .*;$/m), line(/^const globalChatText = .*;$/m), line(/^const GLOBAL_CHAT_PAGE = .*;$/m), line(/^const GLOBAL_CHAT_OLDER = .*;$/m), line(/^const GLOBAL_CHAT_LOADED_MAX = .*;$/m), line(/^const GLOBAL_CHAT_LOADED_KEEP = .*;$/m),
  between('const globalChatKey = ', 'function globalChatReadable'), between('const GLOBAL_CHAT_INPUT_LABEL = ', 'const globalChatBase')].join('\n');
const mountSource = [between('  const entries = new Map();', '  let session = null'), between('  function renderedBoundary() {', '  function scheduleRead'),
  between('  const presentEntry = entry =>', '  // The live session'), between('  function acceptedBubbles(visibleIds) {', '  function presentReceipts'),
  between('  const paintEntries = ', '  const paintInputs'), between('  function invalidatePage() {', '  // ---- composer'),
  between('  const stateUrl = ', '  // ---- the event stream')].join('\n');

const AT_B = '2026-11-20T10:00:00Z', AT_C = '2026-11-21T09:00:00Z';
const A = {conversation_id: 'conv-a', provider: 'codex', continues: null};
const B = {conversation_id: 'conv-b', provider: 'claude', continues: {conversation_id: 'conv-a', provider: 'codex', at: AT_B}};
const C = {conversation_id: 'conv-c', provider: 'claude', continues: {conversation_id: 'conv-b', provider: 'claude', at: AT_C}};
const entry = (id, position, speaker = 'agent', extra = {}) => ({id, position, speaker, text: id, timestamp: '2026-11-21T09:30:00Z', ...extra});
const nudge = (id, position) => ({id, position, speaker: 'notice', text: '', system: {verified: true, input_type: 'inbox_nudge'}});

// One mounted Chat's state and loading code. `network(url)` answers every read; `chat.state` is the default (current) state answer.
function chat(network) {
  const calls = {urls: [], paints: [], banners: []};
  const view = {visible: [], getVisibleKeys: () => view.visible, bottom: true, isAtBottom: () => view.bottom, setDraft() {}, getDraft: () => ''};
  const api = async url => { calls.urls.push(url); return {data: await network(url)}; };
  const make = new Function('api', 'view', 'calls', 'agentGlyph', 'GLOBAL_CHAT_ACCEPTED', 'GLOBAL_CHAT_OPENING', 'GLOBAL_CHAT_HIDDEN', `
    ${moduleSource}
    const agentId = 'agent-1', base = '/root/agents/agent-1/chat', reads = {signal: null}, coreData = result => result.data, contextStatus = null, timelineBlock = null;
    let session = null, conversation = null, inputs = [], older = null, shown, clears = 0, cursor = 0, lastRead = 0, composerScope = null;
    let stream = {}, readTimer = null, loading = false, again = false, generation = 0, reading = 0, olderBusy = false, alive = true;
    const live = () => alive, bindComposer = () => {}, saveDraft = () => {}, connect = () => {}, retry = () => {}, paintSession = () => {};
    const showBanner = text => calls.banners.push(text);
    const paint = (patch = {}, scroll) => { if (patch.entries) calls.paints.push({rows: patch.entries, scroll, busy: olderBusy}); };
    ${mountSource}
    return {load, loadOlder, trimLoaded, queueTrim, invalidatePage, renderedBoundary, acceptedBubbles, openingBubble, newestPosition, olderAbove, earlier, entries,
      rows: () => orderedEntries(loadedParts()), keys: () => orderedEntries(loadedParts()).map(row => row.key), olderCursor: () => older, lastRead: () => lastRead,
      restore() { generation++; loading = false; again = false; }, leave() { alive = false; }};`);
  const accepted = new Map(), opening = new Map();
  return {...make(api, view, calls, () => '<svg></svg>', accepted, opening, new Set()), calls, view, accepted, opening};
}
// The three conversations of two compactions: A (oldest, another provider), then B, then C (the one Chat follows). The positions of the
// earlier conversations are larger than the current one's, as byte positions of separate archives can be.
function chain({current = () => ({conversation: C, entries: [entry('c1', 10, 'user'), entry('c2', 20)], older: null})} = {}) {
  const pages = {
    'conv-b': {conversation: B, entries: [entry('b3', 7000, 'user'), entry('b4', 8000, 'agent', {completion_sequence: 99})], older: 7000},
    'conv-b@7000': {entries: [entry('b1', 5000), entry('b2', 6000)], older: null},
    'conv-a': {conversation: A, entries: [entry('a2', 9500), entry('a1', 9000)], older: null},
  };
  return chat(url => {
    const query = new URL(url, 'http://chat.test').searchParams, id = query.get('conversation');
    if (!id) return {session: {provider: 'claude'}, inputs: [], cursor: 1, last_read_sequence: 0, ...current()};
    const page = pages[url.includes('/history?') ? `${id}@${query.get('before')}` : id];
    if (!page) throw Object.assign(new Error('not found'), {code: 'not_found'});
    return page;
  });
}
const MARK_C = 'compacted:claude\nconv-c', MARK_B = 'compacted:claude\nconv-b';

test('the marker is the first row of a continued conversation loaded to its top, and reads like the Session recovery marker', async () => {
  const c = chain();
  await c.load();
  assert.deepEqual(c.keys(), [MARK_C, 'c1', 'c2']);
  const [marker] = c.rows();
  assert.equal(marker.speaker, 'notice');
  assert.equal(marker.text, '');
  assert.equal(marker.timestamp, AT_C);
  assert.deepEqual(marker.notice, {label: 'Context compacted', verified: true, inputType: 'context_compacted', markerOnly: true});
  assert.deepEqual(marker.typedReferences, []);
  assert.equal(c.calls.paints.at(-1).scroll, 'bottom');
  assert.deepEqual(c.calls.urls, ['/root/agents/agent-1/chat?limit=30'], 'nothing earlier is loaded by itself');
});

test('no marker while the conversation still has an older page, and none for a conversation that continues nothing', async () => {
  const paged = chain({current: () => ({conversation: C, entries: [entry('c2', 20)], older: 20})});
  await paged.load();
  assert.deepEqual(paged.keys(), ['c2']);
  assert.equal(paged.olderAbove(), true);
  const plain = chain({current: () => ({conversation: A, entries: [entry('a1', 9000)], older: null})});
  await plain.load();
  assert.deepEqual(plain.keys(), ['a1']);
  assert.equal(plain.olderAbove(), false);
  await plain.loadOlder();
  assert.equal(plain.calls.urls.length, 1, 'a conversation that continues nothing has nothing above its top');
  const none = chain({current: () => ({conversation: null, entries: [], older: null})});
  await none.load();
  assert.deepEqual(none.keys(), []);
  assert.equal(none.olderAbove(), false);
});

test('a continued conversation with no entries yet still shows its marker and offers the earlier one', async () => {
  const c = chain({current: () => ({conversation: C, entries: [], older: null})});
  await c.load();
  assert.deepEqual(c.keys(), [MARK_C]);
  assert.equal(c.olderAbove(), true);
  await c.loadOlder();
  assert.deepEqual(c.keys(), ['b3', 'b4', MARK_C]);
});

test('Older messages are offered (by scrolling up) at the top of a continued conversation, asks for the earlier one by its exact provider and id, then pages it', async () => {
  const c = chain();
  await c.load();
  assert.equal(c.olderAbove(), true);
  assert.match(between('  function paint(patch = {}, scroll', '  const paintEntries'), /older: \{visible: olderAbove\(\), disabled: olderBusy\}/);
  await c.loadOlder();
  assert.equal(c.calls.urls.at(-1), '/root/agents/agent-1/chat?limit=10&conversation=conv-b&provider=claude');
  assert.deepEqual(c.keys(), ['b3', 'b4', MARK_C, 'c1', 'c2'], 'the earlier conversation stands above the marker; its own top is not loaded yet');
  assert.equal(c.calls.paints.at(-1).scroll, 'prepend');
  assert.equal(c.olderAbove(), true);
  await c.loadOlder();
  assert.equal(c.calls.urls.at(-1), '/root/agents/agent-1/chat/history?before=7000&limit=10&conversation=conv-b&provider=claude');
  assert.deepEqual(c.keys(), [MARK_B, 'b1', 'b2', 'b3', 'b4', MARK_C, 'c1', 'c2']);
  assert.equal(c.rows()[0].timestamp, AT_B);
  assert.equal(c.olderAbove(), true, 'its top is reached and it continues one more');
  await c.loadOlder();
  assert.equal(c.calls.urls.at(-1), '/root/agents/agent-1/chat?limit=10&conversation=conv-a&provider=codex');
  assert.equal(c.olderAbove(), false);
  await c.loadOlder();
  assert.equal(c.calls.urls.length, 4, 'the first conversation has nothing above it');
  assert.equal(c.olderCursor(), null, 'the current conversation’s own cursor is untouched');
  assert.deepEqual(c.calls.banners.filter(Boolean), []);
});

test('three conversations are drawn oldest first, each in its own order, whatever their positions', async () => {
  const c = chain();
  await c.load();
  for (let press = 0; press < 3; press++) await c.loadOlder();
  assert.deepEqual(c.keys(), ['a1', 'a2', MARK_B, 'b1', 'b2', 'b3', 'b4', MARK_C, 'c1', 'c2']);
  assert.deepEqual(c.earlier.map(part => part.conversation.conversation_id), ['conv-a', 'conv-b']);
  assert.deepEqual([...c.entries.keys()], ['c1', 'c2'], 'the current conversation holds only its own entries');
  assert.equal(c.newestPosition(), 20);
});

test('the older page of a current conversation is still its own history page, and its marker appears when its top arrives', async () => {
  const c = chat(url => url.includes('/history?') ? {entries: [entry('c0', 5, 'user')], older: null}
    : {conversation: C, entries: [entry('c2', 20)], older: 20, session: null, inputs: [], cursor: 1});
  await c.load();
  await c.loadOlder();
  assert.equal(c.calls.urls.at(-1), '/root/agents/agent-1/chat/history?before=20&limit=10&conversation=conv-c&provider=claude');
  assert.deepEqual(c.keys(), [MARK_C, 'c0', 'c2']);
  assert.equal(c.earlier.length, 0);
  assert.equal(c.olderCursor(), null);
});

test('the marker breaks a run of activity like any other row', async () => {
  const c = chat(url => url.includes('conversation=') ? {conversation: B, entries: [entry('b1', 5000), nudge('bn', 6000)], older: 5000}
    : {conversation: C, entries: [nudge('cn', 10), entry('c1', 20)], older: null, session: null, inputs: [], cursor: 1});
  await c.load();
  await c.loadOlder();
  assert.deepEqual(c.keys(), ['b1', 'bn', MARK_C, 'cn', 'c1']);
  assert.deepEqual(c.rows().map(row => row.notice?.label), [undefined, 'Activity', 'Context compacted', 'Activity', undefined]);
});

test('a live update keeps the loaded earlier conversations', async () => {
  let current = {conversation: C, entries: [entry('c1', 10, 'user'), entry('c2', 20)], older: null};
  const c = chain({current: () => current});
  await c.load();
  await c.loadOlder(); await c.loadOlder();
  current = {conversation: {...C}, entries: [entry('c2', 20), entry('c3', 30)], older: 20};
  await c.load();
  assert.deepEqual(c.keys(), [MARK_B, 'b1', 'b2', 'b3', 'b4', MARK_C, 'c1', 'c2', 'c3']);
  assert.equal(c.calls.paints.at(-1).scroll, 'auto');
  assert.equal(c.earlier.length, 1);
});

test('a reset, a change of conversation, a gap and a dropped page all drop the earlier conversations', async () => {
  const loaded = async current => { const c = chain(current ? {current} : undefined); await c.load(); await c.loadOlder(); assert.equal(c.earlier.length, 1); return c; };
  const reset = await loaded();
  await reset.load(true);
  assert.deepEqual(reset.keys(), [MARK_C, 'c1', 'c2']);
  assert.equal(reset.earlier.length, 0);

  const next = {conversation_id: 'conv-d', provider: 'claude', continues: {conversation_id: 'conv-c', provider: 'claude', at: '2026-11-21T12:00:00Z'}};
  let state = {conversation: C, entries: [entry('c1', 10)], older: null};
  const changed = await loaded(() => state);
  state = {conversation: next, entries: [entry('d1', 4)], older: null};
  await changed.load();
  assert.deepEqual(changed.keys(), ['compacted:claude\nconv-d', 'd1'], 'the fresh conversation starts with its own marker only');
  assert.equal(changed.earlier.length, 0);

  state = {conversation: C, entries: [entry('c1', 10)], older: null};
  const gap = await loaded(() => state);
  state = {conversation: C, entries: [entry('c9', 900)], older: 900};
  await gap.load();
  assert.deepEqual(gap.keys(), ['c9']);
  assert.equal(gap.earlier.length, 0);

  const dropped = await loaded();
  dropped.invalidatePage();
  assert.equal(dropped.earlier.length, 0);
  assert.deepEqual(dropped.calls.paints.at(-1).rows, []);
});

test('an answer for another conversation, or one that arrives after the page changed, is not applied', async () => {
  const wrong = chat(url => url.includes('conversation=') ? {conversation: A, entries: [entry('a1', 9000)], older: null}
    : {conversation: C, entries: [entry('c1', 10)], older: null, session: null, inputs: [], cursor: 1});
  await wrong.load();
  await wrong.loadOlder();
  assert.deepEqual(wrong.keys(), [MARK_C, 'c1']);
  assert.equal(wrong.earlier.length, 0);

  for (const change of ['reset', 'restore', 'leave']) {
    let release;
    const c = chat(url => url.includes('conversation=') ? new Promise(resolve => { release = () => resolve({conversation: B, entries: [entry('b4', 8000)], older: null}); })
      : {conversation: C, entries: [entry('c1', 10)], older: null, session: null, inputs: [], cursor: 1});
    await c.load();
    const pressed = c.loadOlder();
    await new Promise(resolve => setImmediate(resolve));
    await c.loadOlder();
    assert.equal(c.calls.urls.length, 2, 'a second press while one is loading asks for nothing');
    if (change === 'reset') await c.load(true); else c[change]();
    release(); await pressed;
    assert.equal(c.earlier.length, 0, change);
  }
});

test('a failed read of the earlier conversation shows the usual banner and leaves the button for another try', async () => {
  const c = chat(url => { if (url.includes('conversation=')) throw new Error('offline'); return {conversation: C, entries: [entry('c1', 10)], older: null, session: null, inputs: [], cursor: 1}; });
  await c.load();
  await c.loadOlder();
  assert.equal(c.calls.banners.at(-1), "Couldn't load. Retry");
  assert.deepEqual(c.keys(), [MARK_C, 'c1']);
  assert.equal(c.olderAbove(), true);
});

test('read marks count only the current conversation’s replies, even when an earlier one is on screen', async () => {
  const c = chain({current: () => ({conversation: C, entries: [entry('c1', 10, 'user'), entry('c2', 20, 'agent', {completion_sequence: 7})], older: null, last_read_sequence: 3})});
  await c.load();
  await c.loadOlder();
  c.view.visible = ['b3', 'b4', MARK_C, 'c1'];
  assert.equal(c.renderedBoundary(), 0, 'an earlier conversation’s reply (sequence 99) is never acknowledged');
  c.view.visible = ['b4', MARK_C, 'c1', 'c2'];
  assert.equal(c.renderedBoundary(), 7);
  assert.equal(c.lastRead(), 3, 'reading an earlier conversation leaves the read boundary alone');
});

test('pending bubbles are matched only against the current conversation', async () => {
  const c = chain({current: () => ({conversation: C, entries: [entry('c1', 10, 'user')], older: null})});
  await c.load();
  await c.loadOlder();
  // The same words stand in the earlier conversation, at a larger position and a newer time than the send.
  c.earlier[0].entries.set('b9', {id: 'b9', speaker: 'user', text: 'hello', position: 9000, timestamp: new Date().toISOString()});
  c.accepted.set('agent-1\nclaude\nconv-c', {records: [{id: 'input-1', text: 'hello', position: 10, sequence: 1, state: 'waiting', createdAt: Date.now() - 1000}], consumed: new Set()});
  c.opening.set('agent-1', {text: 'hello', probe: 'hello', at: Date.now() - 1000, position: 10});
  assert.equal(c.acceptedBubbles(new Set()).length, 1, 'the accepted message stays until its own echo arrives');
  assert.equal(c.openingBubble().length, 1);
  assert.equal(c.newestPosition(), 10);
  c.entries.set('c2', {id: 'c2', speaker: 'user', text: 'hello', position: 20, timestamp: new Date().toISOString()});
  assert.equal(c.acceptedBubbles(new Set()).length, 0);
  assert.equal(c.openingBubble().length, 0);
});

test('older requests ask for 10 entries while the state load asks for 30', async () => {
  const c = chain();
  await c.load();
  assert.match(c.calls.urls[0], /\?limit=30/);
  await c.loadOlder(); await c.loadOlder();
  assert.deepEqual(c.calls.urls.slice(1).map(url => url.match(/limit=(\d+)/)[1]), ['10', '10'], 'one earlier-conversation read and one history page');
  assert.ok(c.calls.urls.slice(1).some(url => url.includes('/history?')));
});

// ---- the page drops old loaded messages at the bottom (trimLoaded)
const MANY = 'x';
const fill = (map, count, from = 1, step = 10, speaker = 'agent') => { for (let i = 0; i < count; i++) { const e = entry(`${MANY}${map.size}-${from + i}`, (from + i) * step, speaker); map.set(e.id, e); } };
const stateOf = (older = null) => ({conversation: C, entries: [entry('c1', 10)], older, session: null, inputs: [], cursor: 1});
const trimChat = (answer = () => ({entries: [], older: null})) => chat(url => url.includes('/history?') || url.includes('conversation=') ? answer(url) : stateOf());
const positions = map => [...map.values()].map(e => e.position).sort((a, b) => a - b);

test('nothing is dropped at 100 loaded entries or below, and 101 at the bottom leaves the newest 50', async () => {
  const c = trimChat();
  await c.load();
  c.entries.clear(); fill(c.entries, 100);
  assert.equal(c.trimLoaded(), false);
  assert.equal(c.entries.size, 100);
  fill(c.entries, 1, 101);
  assert.equal(c.trimLoaded(), true);
  assert.equal(c.entries.size, 50);
  assert.deepEqual(positions(c.entries), Array.from({length: 50}, (_, i) => (52 + i) * 10), 'the newest 50 stay');
  assert.equal(c.calls.paints.at(-1).scroll, 'bottom', 'repainted at the bottom');
  assert.equal(c.calls.paints.at(-1).rows.length, 50);
});

test('nothing is dropped while the reader is not at the bottom, however much is loaded', async () => {
  const c = trimChat();
  await c.load();
  c.entries.clear(); fill(c.entries, 500);
  c.view.bottom = false;
  assert.equal(c.trimLoaded(), false);
  assert.equal(c.entries.size, 500);
  c.view.bottom = true;
  assert.equal(c.trimLoaded(), true);
  assert.equal(c.entries.size, 50);
});

test('earlier conversations go first: whole when inside the excess, partly otherwise with their cursor at the oldest kept entry', async () => {
  const c = trimChat();
  await c.load();
  const partA = {conversation: A, entries: new Map(), older: null}, partB = {conversation: B, entries: new Map(), older: 123};
  fill(partA.entries, 30, 1); fill(partB.entries, 60, 1); c.entries.clear(); fill(c.entries, 40, 1);
  c.earlier.push(partA, partB);                      // 130 loaded: 80 must go
  assert.equal(c.trimLoaded(), true);
  assert.deepEqual(c.earlier, [partB], 'A went whole');
  assert.equal(partB.entries.size, 10);
  assert.equal(partB.older, 510, 'B lost its oldest 50 (positions 10..500); the next older page is before the oldest kept one');
  assert.equal(c.entries.size, 40, 'the current conversation is untouched');
  assert.equal(c.olderCursor(), null);
  await c.loadOlder();
  assert.equal(c.calls.urls.at(-1), '/root/agents/agent-1/chat/history?before=510&limit=10&conversation=conv-b&provider=claude');
});

test('a part dropped whole is read again through the part below it, from its newest page', async () => {
  const c = trimChat();
  await c.load();
  const partB = {conversation: B, entries: new Map(), older: null};
  fill(partB.entries, 60, 1); c.entries.clear(); fill(c.entries, 60, 1);
  c.earlier.push(partB);                             // 120: 70 go, B has 60 -> whole, then 10 of the current conversation
  assert.equal(c.trimLoaded(), true);
  assert.equal(c.earlier.length, 0);
  assert.equal(c.entries.size, 50);
  assert.equal(c.olderCursor(), 110, 'the current conversation lost positions 10..100');
});

test('the current conversation’s cursor makes the next older request return what was dropped', async () => {
  const c = trimChat(url => ({entries: [entry('d1', 690), entry('d2', 700)], older: 690}));
  await c.load();
  c.entries.clear(); fill(c.entries, 120);          // positions 10..1200
  assert.equal(c.trimLoaded(), true);
  assert.equal(c.entries.size, 50);
  assert.equal(c.olderCursor(), 710, 'the oldest kept entry is at 710; positions below it were dropped');
  await c.loadOlder();
  assert.equal(c.calls.urls.at(-1), '/root/agents/agent-1/chat/history?before=710&limit=10&conversation=conv-c&provider=claude');
  assert.equal(c.olderCursor(), 690);
});

test('entries that share a position are never split by the cut', async () => {
  const c = trimChat();
  await c.load();
  c.entries.clear();
  for (let i = 0; i < 60; i++) { c.entries.set(`a${i}`, entry(`a${i}`, 10 + i * 10)); c.entries.set(`b${i}`, entry(`b${i}`, 10 + i * 10)); }   // 120 entries, two per position
  assert.equal(c.trimLoaded(), true);
  const kept = positions(c.entries);
  assert.equal(kept.length, 50, 'the cut falls between two positions, so exactly 50 stay');
  const first = kept[0], counts = new Map(); kept.forEach(p => counts.set(p, (counts.get(p) || 0) + 1));
  assert.ok([...counts.values()].every(n => n === 2), 'both entries of every kept position are kept');
  assert.equal(c.olderCursor(), first);
  // An odd keep count: the cut lands inside a pair and moves up, keeping an extra entry.
  const d = trimChat();
  await d.load();
  d.entries.clear();
  fill(d.entries, 69, 1);
  d.entries.set('twin', entry('twin', 10 * 20));      // shares a position with the 20th entry; 70 held + 31 more
  fill(d.entries, 31, 100);
  assert.equal(d.entries.size, 101);
  d.trimLoaded();
  const rest = [...d.entries.values()];
  assert.ok(rest.length >= 50 && rest.length <= 51);
  const minKept = Math.min(...rest.map(e => e.position));
  assert.equal(d.olderCursor(), minKept);
  assert.ok(rest.every(e => e.position >= minKept));
});

test('an older page requested before a trim is discarded', async () => {
  let release;
  const gate = new Promise(done => { release = done; });
  const c = chat(url => url.includes('/history?') ? gate.then(() => ({entries: [entry('late', 5)], older: null})) : stateOf(5));
  await c.load();
  c.entries.clear(); fill(c.entries, 120);
  const pending = c.loadOlder();
  await Promise.resolve();
  assert.equal(c.trimLoaded(), true);
  release();
  await pending;
  assert.equal(c.entries.has('late'), false, 'the stale page was not applied');
  assert.equal(c.entries.size, 50);
});

test('a live update applied at the bottom triggers the trim; at any other place it does not', async () => {
  const c = trimChat();
  await c.load();
  c.view.bottom = false;
  c.entries.clear(); fill(c.entries, 130);
  await c.load();
  assert.equal(c.entries.size, 131, 'the live page joined the held entries and nothing was dropped');
  c.view.bottom = true;
  await c.load();
  assert.equal(c.entries.size, 50);
});

test('returning to the bottom triggers the trim through the view’s viewport change', async () => {
  const c = trimChat();
  await c.load();
  c.view.bottom = false;
  c.entries.clear(); fill(c.entries, 130);
  c.view.bottom = true;
  c.queueTrim(); c.queueTrim();                       // coalesced
  assert.equal(c.entries.size, 130, 'the trim waits for the end of the view’s own turn');
  await Promise.resolve();
  assert.equal(c.entries.size, 50);
  assert.match(source, /onViewportChange: \(\) => \{ scheduleRead\(\); queueTrim\(\); \}/);
});
