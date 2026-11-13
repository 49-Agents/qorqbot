'use strict';
// Per-platform values the engine and the main process rely on.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { platformFor, installCommand, TOOL_IDS } = require('../platform');

test('macOS values', () => {
  const mac = platformFor('darwin');
  assert.equal(mac.id, 'darwin');
  assert.equal(mac.computer, 'this Mac');
  assert.equal(mac.installPrefix, 'brew install');
  assert.deepEqual(mac.packages, { python: ['python@3.12'], git: ['git'], tmux: ['tmux'] });
  assert.deepEqual(mac.packageManager, { label: 'Homebrew', command: 'brew', url: 'https://brew.sh' });
  assert.deepEqual(mac.extraPath, ['/opt/homebrew/bin', '/usr/local/bin']);
  assert.equal(mac.defaultShell, '/bin/zsh');
  assert.deepEqual(mac.pythonFallbacks, ['/opt/homebrew/opt/python@3.12/bin/python3.12']);
  assert.deepEqual(mac.pythonModules, ['venv']);
  assert.equal(mac.closeQuits, false);
});

test('Ubuntu values', () => {
  const linux = platformFor('linux');
  assert.equal(linux.id, 'linux');
  assert.equal(linux.computer, 'this computer');
  assert.equal(linux.installPrefix, 'sudo apt install');
  assert.deepEqual(linux.packages, { python: ['python3.12', 'python3.12-venv'], git: ['git'], tmux: ['tmux'] });
  assert.equal(linux.packageManager, null);
  assert.deepEqual(linux.extraPath, ['/usr/local/bin']);
  assert.equal(linux.defaultShell, '/bin/bash');
  assert.deepEqual(linux.pythonFallbacks, []);
  assert.deepEqual(linux.pythonModules, ['venv', 'ensurepip']);
  assert.equal(linux.closeQuits, true);
});

test('installCommand covers all tools, a subset and none', () => {
  assert.deepEqual(TOOL_IDS, ['python', 'git', 'tmux']);
  assert.equal(installCommand(platformFor('darwin')), 'brew install python@3.12 git tmux');
  assert.equal(installCommand(platformFor('linux')), 'sudo apt install python3.12 python3.12-venv git tmux');
  assert.equal(installCommand(platformFor('darwin'), ['tmux']), 'brew install tmux');
  assert.equal(installCommand(platformFor('linux'), ['python', 'tmux']), 'sudo apt install python3.12 python3.12-venv tmux');
  assert.equal(installCommand(platformFor('darwin'), []), '');
  assert.equal(installCommand(platformFor('linux'), []), '');
});

test('an unsupported platform throws', () => {
  assert.throws(() => platformFor('win32'), /does not support this platform \(win32\)/);
});
