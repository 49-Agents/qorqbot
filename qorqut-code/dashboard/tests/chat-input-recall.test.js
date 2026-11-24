'use strict';
// node --test dashboard/tests/chat-input-recall.test.js
// ArrowUp in an empty composer recalls the owner's own sent messages (js/chat-view.js); the pure model, cut out of its source.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../js/chat-view.js'), 'utf8');
const start = source.indexOf('const chatInputRecallModel = {'), end = source.indexOf('// ---- the view', start);
assert.ok(start >= 0 && end > start, 'chat-view.js no longer contains chatInputRecallModel');
const recall = new Function(`${source.slice(start, end)}; return chatInputRecallModel;`)();

const user = (text, extra = {}) => ({key: text, speaker: 'user', text, ...extra});
const agent = text => ({key: text, speaker: 'agent', text});
const notice = (text, extra = {}) => ({key: text, speaker: 'notice', text, ...extra});

test('history keeps only what the owner typed, oldest first', () => {
  const entries = [
    user('first'), agent('reply'),
    notice('You have new inbox items', {notice: {label: 'Inbox nudge', verified: true, inputType: 'inbox_nudge', markerOnly: true}}),
    notice('', {notice: {label: 'Activity', verified: true, markerOnly: true, activity: {nudgeCount: 2, recipients: []}}}),
    notice('unverified nudge text', {notice: {label: 'Inbox nudge', verified: false, inputType: 'inbox_nudge'}}),
    notice('Session recovery', {notice: {inputType: 'bootstrap_recovery'}}),
    user('[telegram] from the phone', {collapsedSource: 'telegram'}),
    user('interpreted for the owner', {recorder: 'agent'}),
    user('', {referenceOnly: true}), user('   '),
    user('second'), user('second'), agent('second'), user('third\nline'),
  ];
  const pending = [notice('', {inputType: 'inbox_nudge', state: 'queued'}), user('private', {collapsedSource: 'telegram'}), user('unsent', {state: 'failed'})];
  assert.deepEqual(recall.history(entries, pending), ['first', 'second', 'third\nline', 'unsent']);
  assert.deepEqual(recall.history(), []);
  assert.deepEqual(recall.history(null, null), []);
});

test('ArrowUp walks back from an empty draft and ArrowDown returns to it', () => {
  const history = ['one', 'two', 'three'];
  let step = recall.step(null, 'ArrowUp', '', history);
  assert.deepEqual(step, {text: 'three', state: {index: 2, text: 'three'}});
  step = recall.step(step.state, 'ArrowUp', step.text, history);
  assert.equal(step.text, 'two');
  step = recall.step(step.state, 'ArrowUp', step.text, history);
  assert.equal(step.text, 'one');
  // The oldest message stays; the key is still consumed.
  const oldest = recall.step(step.state, 'ArrowUp', step.text, history);
  assert.deepEqual(oldest, {text: 'one', state: {index: 0, text: 'one'}});
  step = recall.step(oldest.state, 'ArrowDown', oldest.text, history);
  assert.equal(step.text, 'two');
  step = recall.step(step.state, 'ArrowDown', step.text, history);
  assert.equal(step.text, 'three');
  assert.deepEqual(recall.step(step.state, 'ArrowDown', step.text, history), {text: '', state: null});
});

test('arrows keep their ordinary meaning outside a recall', () => {
  const history = ['one', 'two'];
  assert.equal(recall.step(null, 'ArrowUp', 'typing', history), null);
  assert.equal(recall.step(null, 'ArrowUp', ' ', history), null);
  assert.equal(recall.step(null, 'ArrowDown', '', history), null);
  assert.equal(recall.step(null, 'ArrowUp', '', []), null);
  assert.equal(recall.step(null, 'Enter', '', history), null);
  // An edited recall is an ordinary draft again.
  const state = recall.step(null, 'ArrowUp', '', history).state;
  assert.equal(recall.step(state, 'ArrowUp', 'two!', history), null);
  assert.equal(recall.step(state, 'ArrowDown', 'two!', history), null);
  // Cleared by hand, the next ArrowUp starts again from the newest message.
  assert.equal(recall.step(state, 'ArrowUp', '', history).text, 'two');
});

test('a recall keeps its place when messages load or arrive between presses', () => {
  const state = recall.step(null, 'ArrowUp', '', ['b', 'c']).state;
  assert.equal(recall.step(state, 'ArrowUp', 'c', ['a', 'b', 'c', 'd']).text, 'b');
  assert.equal(recall.step(state, 'ArrowDown', 'c', ['a', 'b', 'c', 'd']).text, 'd');
  // The recalled message is no longer loaded: step from the newest end.
  assert.equal(recall.step(state, 'ArrowUp', 'c', ['x', 'y']).text, 'y');
  assert.deepEqual(recall.step(state, 'ArrowDown', 'c', ['x', 'y']), {text: '', state: null});
});

test('each conversation recalls only its own rows', () => {
  const chat = recall.history([user('chat message'), notice('nudge')]);
  const thread = recall.history([user('thread message')]);
  assert.equal(recall.step(null, 'ArrowUp', '', chat).text, 'chat message');
  assert.equal(recall.step(null, 'ArrowUp', '', thread).text, 'thread message');
});
