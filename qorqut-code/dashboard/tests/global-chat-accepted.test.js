'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../js/global-chat.js'), 'utf8');
const normalizer = source.match(/^const globalChatText = .*;$/m)[0];
const start = source.indexOf('  function acceptedBubbles(visibleIds) {');
const end = source.indexOf('  function presentReceipts()', start);
assert.ok(start >= 0 && end > start);
const makeMatcher = new Function('held', 'entries', `
  ${normalizer}
  const GLOBAL_CHAT_ACCEPTED = new Map([['scope', held]]), GLOBAL_CHAT_HIDDEN = new Set();
  ${source.match(/^const GLOBAL_CHAT_ECHO_SLACK = .*;$/m)[0]}
  const globalChatScope = () => 'scope', agentId = 'agent', conversation = {}, inputs = [];
  const GLOBAL_CHAT_INPUT_STATE = {submitted: 'Sent'};
  ${source.slice(start, end)}
  return () => acceptedBubbles(new Set());
`);

function fixture(sent, transcript) {
  const records = sent.map((text, i) => ({id: `input-${i}`, text, position: 10, sequence: i,
    state: 'submitted', createdAt: Date.parse('2026-11-08T23:03:57Z')}));
  const held = {records: [...records], consumed: new Set()};
  const entries = new Map(transcript.map((text, i) => [`entry-${i}`, {id: `entry-${i}`, text,
    speaker: 'user', position: 20 + i, timestamp: '2026-11-08T23:04:07Z'}]));
  return {records, entries, match: makeMatcher(held, entries)};
}

test('an echo stamped a moment before the receipt settles it; a much older identical entry does not', () => {
  const at = Date.parse('2026-11-08T23:03:57Z');
  const stamped = offset => {
    const {records, entries, match} = fixture(['hello'], ['hello']);
    entries.get('entry-0').timestamp = new Date(at + offset).toISOString();
    match();
    return records[0].entry;
  };
  assert.equal(stamped(-130), 'entry-0');      // a session that was just opened writes the line slightly before the receipt
  assert.equal(stamped(-60000), undefined);    // an old identical message is not this send
});

test('trailing whitespace does not leave a Sent bubble; original text is preserved', () => {
  for (const suffix of [' ', '\t', '\n', ' \r\n']) {
    const text = `Has my trip been updated?${suffix}`;
    const chat = fixture([text], ['Has my trip been updated?']);
    assert.deepEqual(chat.match(), []);
    assert.equal(chat.records[0].text, text);
    assert.equal(chat.entries.get('entry-0').text, 'Has my trip been updated?');
  }
});

test('internal whitespace and different message content remain distinct', () => {
  for (const [sent, transcript] of [['a\n  b', 'a b'], ['hello', 'hello there']]) {
    assert.equal(fixture([sent], [transcript]).match().length, 1);
  }
});

test('one transcript entry settles only one of two identical sends', () => {
  assert.equal(fixture(['hello ', 'hello '], ['hello']).match().length, 1);
  assert.equal(fixture(['hello ', 'hello '], ['hello', 'hello']).match().length, 0);
});

// The first message to an agent whose session was never started: it is accepted under the agent's opening scope while no
// conversation exists yet, and the session goes live with an empty transcript a few seconds before the message is captured.
test('the message that opened a session stays on screen until its transcript entry arrives, and is shown once', () => {
  const scopeLines = ['globalChatKey', 'globalChatScope'].map(name => source.match(new RegExp(`^const ${name} = .*;$`, 'm'))[0]).join('\n');
  const accepted = new Map(), entries = new Map(), page = {conversation: null, live: false};
  const bubbles = new Function('GLOBAL_CHAT_ACCEPTED', 'entries', 'page', `
    ${normalizer}
    ${scopeLines}
    ${source.match(/^const GLOBAL_CHAT_ECHO_SLACK = .*;$/m)[0]}
    const GLOBAL_CHAT_HIDDEN = new Set(), agentId = 'agent', inputs = [], GLOBAL_CHAT_INPUT_STATE = {waiting: 'Sending…', submitted: 'Sent'};
    const viewingLive = () => page.live;
    let conversation;
    ${source.slice(start, end)}
    return () => { conversation = page.conversation; return acceptedBubbles(new Set()); };
  `)(accepted, entries, page);
  // What submit records once the message is accepted (globalChatAccept), under the scope the composer had: the opening one.
  accepted.set('agent\nmanaged', {records: [{id: 'input-1', entry: null, text: 'hi, help me get set up', position: -1, command: false,
    state: 'waiting', sequence: 1, createdAt: Date.parse('2026-11-27T10:00:00Z')}], consumed: new Set()});
  const texts = () => bubbles().map(row => row.model.text);
  // Live, with nothing captured yet: the server sends no conversation.
  page.live = true;
  assert.deepEqual(texts(), ['hi, help me get set up']);
  // The conversation is captured, still without the owner's message.
  page.conversation = {provider: 'claude', conversation_id: 'c-1'};
  assert.deepEqual(texts(), ['hi, help me get set up']);
  assert.equal(accepted.has('agent\nmanaged'), false);
  // Its own entry arrives: the bubble goes, so it is never shown twice.
  entries.set('e-1', {id: 'e-1', speaker: 'user', text: 'hi, help me get set up', position: 0, timestamp: '2026-11-27T10:00:12Z'});
  assert.deepEqual(texts(), []);
  assert.deepEqual(texts(), []);
});
