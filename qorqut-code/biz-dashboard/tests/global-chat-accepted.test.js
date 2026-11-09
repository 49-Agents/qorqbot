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
