'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const path = require('node:path');

const fixture = path.join(__dirname, 'fixtures/agent-browser-open-tab.html');
const chrome = ['google-chrome', 'chromium', 'chromium-browser'].find(bin => spawnSync(bin, ['--version']).status === 0);
const AGENT = '3f6c1b9e-2a47-4d1e-9b0c-5e8a7d2c4f10', A = 'A'.repeat(32), B = 'B'.repeat(32);
const UNAVAILABLE = 'This agent’s browser can’t be opened here.';

// The real agent-browser.js and agent-browser-live.js in headless Chrome, with the API, EventSource and agent navigation stubbed.
// This covers the dashboard side of "open this tab" (entry point -> Browser mount -> live view on the tab); the real agent
// navigation and pane lifecycle (globalAgentsNavigate, agpadSetView) are stubbed, not exercised.
let cached = null;
function results() {
  if (cached) return cached;
  const result = spawnSync(chrome, ['--headless=new', '--no-sandbox', '--disable-gpu', '--allow-file-access-from-files', '--window-size=600,600',
    '--virtual-time-budget=10000', '--dump-dom', 'file://' + fixture], {encoding: 'utf8', maxBuffer: 1 << 26});
  const raw = /<pre id="out"[^>]*>(.*?)<\/pre>/s.exec(result.stdout)[1].replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
  return cached = JSON.parse(raw);
}

test('opening a tab selects it in the live view, switching a view already open on another tab', {skip: !chrome && 'Chrome not installed'}, () => {
  const out = results();
  // Cold: the agent is navigated to in its Browser mode, and the one live view is on the asked-for tab (not the first page).
  assert.equal(out.cold.result, true);
  assert.deepEqual(out.cold.navigations, [[AGENT, 'browser']]);
  assert.deepEqual([out.cold.open, out.cold.picker, out.cold.live], [[B], B, true]);
  assert.equal(out.cold.consumed, true);
  assert.deepEqual(out.cold.toasts, []);
  // Open on the first tab, then asked for the second: one new stream, the old one closed.
  assert.deepEqual([out.baseline.open, out.baseline.picker], [[A], A]);
  assert.deepEqual([out.switched.result, out.switched.open, out.switched.picker, out.switched.streams, out.switched.firstClosed], [true, [B], B, 1, true]);
});

test('a request is spent by the mount it caused, expires, and is dropped when the agent was not reached', {skip: !chrome && 'Chrome not installed'}, () => {
  const out = results();
  // Later, ordinary opens of Browser show the usual first page.
  assert.deepEqual([out.afterwards.open, out.afterwards.picker], [[A], A]);
  // A tab that has closed meanwhile falls back to the live view's usual pick, without an error or a notice.
  assert.deepEqual([out.gone.result, out.gone.open, out.gone.picker, out.gone.toasts], [true, [A], A, []]);
  // The owner stayed where they were (cancelled the unsaved-changes prompt): no toast, and the request is gone.
  assert.deepEqual(out.cancelled, {result: false, spent: true, toasts: []});
  // A mount that comes long after the request is an ordinary open.
  assert.deepEqual([out.late.open, out.late.picker], [[A], A]);
});

test('an agent this dashboard does not show, or malformed ids, navigate nowhere', {skip: !chrome && 'Chrome not installed'}, () => {
  const out = results();
  assert.deepEqual(out.unknownAgent, {result: false, navigations: [], toasts: [UNAVAILABLE], wanted: null});
  assert.deepEqual(out.badIds, [false, false, false, false, false, false]);
  assert.deepEqual(out.badIdsEffects, {navigations: 0, toasts: 0, wanted: null});
});

test('a pane that has not offered Browser yet offers it, and a configured viewer does not hide the asked-for tab', {skip: !chrome && 'Chrome not installed'}, () => {
  const out = results();
  assert.equal(out.undiscovered.result, true);
  assert.deepEqual([out.undiscovered.views, out.undiscovered.live, out.undiscovered.syncs], [['focus', 'browser'], true, [AGENT]]);
  assert.deepEqual([out.undiscovered.open, out.undiscovered.picker], [[B], B]);
  // Without a request the configured viewer is shown, as before.
  assert.deepEqual([out.configuredPlain.live, out.configuredPlain.configuredFrame], [false, true]);
  assert.deepEqual([out.configuredTab.result, out.configuredTab.live, out.configuredTab.configuredFrame, out.configuredTab.open], [true, true, false, [B]]);
});
