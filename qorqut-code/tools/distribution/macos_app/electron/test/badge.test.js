'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { ATTENTION_PATH, attentionCount, attentionUrl, badgeText } = require('../badge');

const ok = (data) => ({ status: 'ok', data, request_id: 'r1' });

test('the count is the new items the attention queue returns', () => {
  assert.equal(attentionCount(ok({ items: [] })), 0);
  assert.equal(attentionCount(ok({ items: [{ id: 'signoff:1' }, { id: 'reminder:2' }, { id: 'viewbox:3' }] })), 3);
  assert.equal(attentionCount(ok({ items: [{ id: 'a' }], side_chats: [], discarded: ['calendar:2026-01-01:x'] })), 1);
});

test('unread side chats count, read ones do not', () => {
  const chats = [{ id: 'a', unread: true }, { id: 'b', unread: false }, { id: 'c', unread: true }, null, { id: 'd' }];
  assert.equal(attentionCount(ok({ items: [{ id: 'x' }], side_chats: chats })), 3);
  assert.equal(attentionCount(ok({ items: [], side_chats: 'not a list' })), 0);
});

test('anything that is not a successful attention queue gives no count', () => {
  for (const body of [
    null, undefined, 42, 'ok', [], {},
    { status: 'error', error: { code: 'forbidden', message: 'Attention queue is owner-only' } },
    { status: 'ok', error: { code: 'x' }, data: { items: [] } },
    { status: 'ok' },
    ok(null), ok({}), ok({ items: 'many' }), ok({ items: { length: 4 } }),
  ]) assert.equal(attentionCount(body), null, JSON.stringify(body));
});

test('the Dock badge is empty for none, the number up to 99, then 99+', () => {
  assert.equal(badgeText(0), '');
  assert.equal(badgeText(1), '1');
  assert.equal(badgeText(42), '42');
  assert.equal(badgeText(99), '99');
  assert.equal(badgeText(100), '99+');
  assert.equal(badgeText(5000), '99+');
  for (const bad of [-1, 1.5, NaN, Infinity, null, undefined, '3']) assert.equal(badgeText(bad), '', String(bad));
});

test('the queue is read from the server the app started', () => {
  assert.equal(ATTENTION_PATH, '/api/root/attention-queue');
  assert.equal(attentionUrl('http://127.0.0.1:1049'), 'http://127.0.0.1:1049/api/root/attention-queue');
  assert.equal(attentionUrl('http://127.0.0.1:1061'), 'http://127.0.0.1:1061/api/root/attention-queue');
  assert.equal(attentionUrl(null), null);
  assert.equal(attentionUrl(''), null);
});
