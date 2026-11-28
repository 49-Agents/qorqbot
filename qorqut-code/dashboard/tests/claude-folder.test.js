'use strict';
// node --test dashboard/tests/claude-folder.test.js
// The Claude Code folder row: what it says about the folder agents use, and what it says after a restart. The row itself is
// checked in a real browser against a running server.
const test = require('node:test');
const assert = require('node:assert/strict');

global.esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const folder = require('../js/claude-folder.js');

test('the help names the folder in use, and how to go back when one is chosen', () => {
  assert.equal(folder.rowHelp(null), 'Checking…');
  const chosen = folder.rowHelp({path: '/Users/o/.claude-personal', effective: '/Users/o/.claude-personal', default: '/Users/o/.claude'});
  assert.match(chosen, /\/Users\/o\/\.claude-personal/);
  assert.match(chosen, /Clear the field to go back to \/Users\/o\/\.claude\./);
  const none = folder.rowHelp({path: null, effective: '/Users/o/.claude', default: '/Users/o/.claude'});
  assert.match(none, /^Agents use \/Users\/o\/\.claude\./);
  assert.match(none, /~\/\.claude-personal/);
  assert.doesNotMatch(folder.rowHelp({path: null, effective: null, default: 5}), /undefined|null/);
});

test('the restart note counts agents, says when none ran, and explains Terminals mode', () => {
  assert.equal(folder.restartNote(null), '');
  assert.equal(folder.restartNote({restarted: 2, continued: 0, failed: 0, terminals: false}), '2 agents restarted with this folder.');
  assert.equal(folder.restartNote({restarted: 1, failed: 0}), '1 agent restarted with this folder.');
  assert.equal(folder.restartNote({restarted: 0, failed: 0}), 'No Claude agents were running. Each one uses this folder when it next starts.');
  assert.equal(folder.restartNote({restarted: 1, failed: 2}),
    '1 agent restarted with this folder. 2 did not restart; they use the folder on the next message.');
  assert.equal(folder.restartNote({restarted: 0, failed: 1}), '1 did not restart; it uses the folder on the next message.');
  assert.match(folder.restartNote({terminals: true, restarted: 0}), /terminals/);
  assert.equal(folder.restartNote({restarted: 'x', failed: -3}), 'No Claude agents were running. Each one uses this folder when it next starts.');
});
