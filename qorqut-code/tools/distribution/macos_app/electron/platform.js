'use strict';
// Per-platform values for the Qorqbot desktop app (macOS and Ubuntu).
//
// Plain data with no Electron imports, shared by the engine, the main process
// and the tests. Everything that differs between the two desktops is named
// here, so the rest of the app asks this module instead of testing
// process.platform itself.

const PLATFORMS = {
  darwin: {
    id: 'darwin',
    // Appended to PATH: apps started from the desktop get a minimal PATH.
    extraPath: ['/opt/homebrew/bin', '/usr/local/bin'],
    defaultShell: '/bin/zsh',
    // The archive carries its own Python, git and tmux, so set-up only unpacks it.
    firstLaunch: 'a few seconds',
    // Closing the window hides it (the Dock icon brings it back).
    closeQuits: false,
  },
  linux: {
    id: 'linux',
    extraPath: ['/usr/local/bin'],
    defaultShell: '/bin/bash',
    // The package installs Python 3.12, git and tmux; first launch builds the Python environment from the archive.
    firstLaunch: 'about a minute',
    // No Dock to reopen a hidden window from: closing the window quits, which stops the server.
    closeQuits: true,
  },
};

/** @param {string} [name] a process.platform value */
function platformFor(name = process.platform) {
  const platform = PLATFORMS[name];
  if (!platform) throw new Error(`The Qorqbot desktop app does not support this platform (${name}).`);
  return platform;
}

module.exports = { platformFor };
