'use strict';
// Per-platform values for the Qorqbot desktop app (macOS and Ubuntu).
//
// Plain data with no Electron imports, shared by the engine, the main process
// and the tests. Everything that differs between the two desktops is named
// here, so the rest of the app asks this module instead of testing
// process.platform itself.

const TOOL_IDS = ['python', 'git', 'tmux'];

const PLATFORMS = {
  darwin: {
    id: 'darwin',
    // Used in sentences: "Qorqbot needs git on this Mac."
    computer: 'this Mac',
    // The command that installs required programs is installPrefix + package names.
    installPrefix: 'brew install',
    packages: { python: ['python@3.12'], git: ['git'], tmux: ['tmux'] },
    // A separate program the user must install before the command works, or null.
    packageManager: { label: 'Homebrew', command: 'brew', url: 'https://brew.sh' },
    // Appended to PATH: apps started from the desktop get a minimal PATH.
    extraPath: ['/opt/homebrew/bin', '/usr/local/bin'],
    defaultShell: '/bin/zsh',
    // Tried when python3.12 is not on PATH.
    pythonFallbacks: ['/opt/homebrew/opt/python@3.12/bin/python3.12'],
    // Modules a usable Python 3.12 must import.
    pythonModules: ['venv'],
    // How "Open Terminal" starts a terminal: an app to open, or commands to try in order.
    terminal: { app: '/System/Applications/Utilities/Terminal.app', commands: [] },
    fileManager: 'Finder',
    // Closing the window hides it (the Dock icon brings it back).
    closeQuits: false,
  },
  linux: {
    id: 'linux',
    computer: 'this computer',
    installPrefix: 'sudo apt install',
    // Ubuntu ships ensurepip in python3.12-venv; without it the first-launch set-up cannot build its environment.
    packages: { python: ['python3.12', 'python3.12-venv'], git: ['git'], tmux: ['tmux'] },
    packageManager: null,
    extraPath: ['/usr/local/bin'],
    defaultShell: '/bin/bash',
    pythonFallbacks: [],
    pythonModules: ['venv', 'ensurepip'],
    terminal: { app: null, commands: ['x-terminal-emulator', 'gnome-terminal', 'konsole', 'xterm'] },
    fileManager: 'Files',
    // No Dock to reopen a hidden window from: closing the window quits, which stops the server.
    closeQuits: true,
  },
};

/** The command that installs the named required programs (all of them by default). */
function installCommand(platform, ids = TOOL_IDS) {
  const names = ids.flatMap((id) => platform.packages[id]);
  return names.length ? `${platform.installPrefix} ${names.join(' ')}` : '';
}

/** @param {string} [name] a process.platform value */
function platformFor(name = process.platform) {
  const platform = PLATFORMS[name];
  if (!platform) throw new Error(`The Qorqbot desktop app does not support this platform (${name}).`);
  return platform;
}

module.exports = { platformFor, installCommand, TOOL_IDS };
