'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { platformFor } = require('../platform');

test('macOS keeps the Homebrew folders, hides the window on close and sets up in seconds', () => {
  const mac = platformFor('darwin');
  assert.equal(mac.id, 'darwin');
  assert.deepEqual(mac.extraPath, ['/opt/homebrew/bin', '/usr/local/bin']);
  assert.equal(mac.defaultShell, '/bin/zsh');
  assert.equal(mac.closeQuits, false);
  assert.equal(mac.firstLaunch, 'a few seconds');
});

test('Ubuntu has no Homebrew folder, quits on close and sets up in about a minute', () => {
  const linux = platformFor('linux');
  assert.equal(linux.id, 'linux');
  assert.deepEqual(linux.extraPath, ['/usr/local/bin']);
  assert.equal(linux.defaultShell, '/bin/bash');
  assert.equal(linux.closeQuits, true);
  assert.equal(linux.firstLaunch, 'about a minute');
});

test('an unsupported platform is refused by name', () => {
  assert.throws(() => platformFor('win32'), /win32/);
});
