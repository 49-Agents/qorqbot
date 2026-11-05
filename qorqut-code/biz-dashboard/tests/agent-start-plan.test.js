'use strict';
// node --test biz-dashboard/tests/*.test.js
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { plan, STARTING_FOR_MS } = require('../js/agent-start.js');

const offline = {state: 'offline', reason: 'no_session'};
const granted = {granted: true};

test('a live or unloaded session offers nothing', () => {
  assert.deepEqual(plan(null, granted), {kind: 'none'});
  assert.deepEqual(plan({state: 'live', reason: 'live'}, granted), {kind: 'none'});
});

test('offline reasons a start cannot fix offer nothing', () => {
  for (const reason of ['capture_disabled', 'unsupported_runtime', 'native_only', 'other_conversation', 'selection_required', 'registering']) {
    assert.equal(plan({state: 'offline', reason}, granted).kind, 'none', reason);
  }
});

test('without consent Start asks first', () => {
  for (const trust of [null, {granted: false}]) {
    assert.deepEqual(plan(offline, trust), {kind: 'start', needsConsent: true});
  }
});

test('with consent Start is offered and nothing starts by itself', () => {
  assert.deepEqual(plan(offline, granted), {kind: 'start', needsConsent: false});
  assert.ok(!('auto' in plan(offline, granted)));
});

test('a start shows starting, then a failure once the wait runs out', () => {
  const now = 1_000_000;
  assert.deepEqual(plan(offline, granted, {startedAt: now - 5000, now}), {kind: 'starting'});
  assert.equal(plan(offline, granted, {startedAt: now - STARTING_FOR_MS - 1, now}).kind, 'failed');
});

test('a refused start reports its error', () => {
  assert.deepEqual(plan(offline, granted, {error: 'Start requires an absent terminal or idle shell'}),
    {kind: 'failed', error: 'Start requires an absent terminal or idle shell'});
});
