'use strict';
// node --test biz-dashboard/tests/*.test.js
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { plan, STARTING_FOR_MS } = require('../js/agent-start.js');

const offline = {state: 'offline', reason: 'no_session'};
const granted = {granted: true, autostart: true};

test('a live or unloaded session offers nothing', () => {
  assert.deepEqual(plan(null, granted), {kind: 'none'});
  assert.deepEqual(plan({state: 'live', reason: 'live'}, granted), {kind: 'none'});
});

test('offline reasons a start cannot fix offer nothing', () => {
  for (const reason of ['capture_disabled', 'unsupported_runtime', 'native_only', 'other_conversation', 'selection_required', 'registering']) {
    assert.equal(plan({state: 'offline', reason}, granted, {isLead: true}).kind, 'none', reason);
  }
});

test('without consent the owner is offered Start, which asks first, and nothing starts by itself', () => {
  for (const trust of [null, {granted: false, autostart: false}]) {
    assert.deepEqual(plan(offline, trust, {isLead: true}), {kind: 'start', auto: false, needsConsent: true});
  }
});

test('with consent the lead agent starts by itself once', () => {
  assert.deepEqual(plan(offline, granted, {isLead: true}), {kind: 'start', auto: true, needsConsent: false});
  assert.equal(plan(offline, granted, {isLead: true, autoTried: true}).auto, false);
});

test('other agents and an explicitly stopped lead wait for the button', () => {
  assert.equal(plan(offline, granted, {isLead: false}).auto, false);
  assert.equal(plan(offline, {granted: true, autostart: false}, {isLead: true}).auto, false);
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
