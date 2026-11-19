'use strict';
// Linux desktop helpers for the Qorqbot app: the applications-menu entry of an
// AppImage, the Chromium sandbox status and the system-tray check.
//
// No Electron imports: everything here is plain Node so it can be tested
// directly, with HOME, XDG_DATA_HOME and APPIMAGE passed in as data.
//
// Applications menu. A .deb installs its own /usr/share/applications entry, so
// nothing is done for it (APPIMAGE unset). An AppImage has no installer, so the
// first launch offers once to write ~/.local/share/applications/qorqbot.desktop
// and the 512px icon. The answer is remembered in ~/Qorqbot/desktop-integration.json
// and never asked again; File > Add to / Remove from Applications Menu stays
// available. An entry this module wrote follows the AppImage when it is moved.
//
// Sandbox. Chromium decides about its sandbox before any JavaScript runs, so the
// app cannot switch it off from main.js: `app.commandLine.appendSwitch('no-sandbox')`
// is too late (verified with Electron 44 on Ubuntu 24.04: the process aborts with
// "The SUID sandbox helper binary was found, but is not configured correctly"
// before main.js is read). Only the command line (--no-sandbox) or the
// ELECTRON_DISABLE_SANDBOX=1 environment variable works. Ubuntu 23.10+ blocks
// unprivileged user namespaces for unconfined programs
// (kernel.apparmor_restrict_unprivileged_userns=1) and an AppImage cannot carry a
// setuid chrome-sandbox, so electron-builder's generated AppRun script probes
// `unshare -Ur true` and adds --no-sandbox only when namespaces are unusable. Where
// they work (Fedora, Ubuntu 22.04, Debian) the sandbox stays on. The .deb gets
// electron-builder's AppArmor profile (userns allowed) or a setuid chrome-sandbox
// from its post-install script. This module only reports which case applies so the
// log and the About dialog can say it; every window keeps `sandbox: true` in its
// webPreferences, so renderers never get Node either way.

const { execFile } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const DESKTOP_FILE_NAME = 'qorqbot.desktop';
const ICON_NAME = 'qorqbot';
const MANAGED_KEY = 'X-Qorqbot-Managed';
const CHOICE_FILE = 'desktop-integration.json';
const STATUS_WATCHER = 'org.kde.StatusNotifierWatcher';

// ---------------------------------------------------------------- applications menu

/** The XDG data directory: $XDG_DATA_HOME when it is an absolute path, else ~/.local/share. */
function dataHome(env = process.env, home = os.homedir()) {
  const configured = env.XDG_DATA_HOME;
  return configured && path.isAbsolute(configured) ? configured : path.join(home, '.local', 'share');
}

function locations(env, home) {
  const base = dataHome(env, home);
  return {
    desktopFile: path.join(base, 'applications', DESKTOP_FILE_NAME),
    icon: path.join(base, 'icons', 'hicolor', '512x512', 'apps', ICON_NAME + '.png'),
  };
}

/** One Exec argument as the Desktop Entry specification wants it: in double quotes, with the characters the
 * quoting rules reserve escaped, then the string-level backslash escaping, and % doubled for field codes. */
function quoteExecArgument(value) {
  const quoted = String(value).replace(/[\\"`$]/g, '\\$&');
  return '"' + quoted.replace(/\\/g, '\\\\').replace(/%/g, '%%') + '"';
}

function desktopEntry(appImage) {
  return [
    '[Desktop Entry]',
    'Type=Application',
    'Name=Qorqbot',
    'Comment=Qorqbot desktop app',
    `Exec=${quoteExecArgument(appImage)} %U`,
    `Icon=${ICON_NAME}`,
    'Terminal=false',
    'StartupNotify=true',
    'StartupWMClass=qorqbot',
    'Categories=Office;', // one main category, as in the .deb: a second one lists the app twice in the menu
    `${MANAGED_KEY}=true`,
    '',
  ].join('\n');
}

function readIfExists(file) {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

/** What the applications menu has for this AppImage.
 *   installed: an entry exists (ours or not)   managed: we wrote it   stale: ours, but points elsewhere */
function menuEntryState({ appImage, env = process.env, home = os.homedir() }) {
  const where = locations(env, home);
  const existing = readIfExists(where.desktopFile);
  if (existing === null) return { installed: false, managed: false, stale: false, ...where };
  const managed = new RegExp(`^${MANAGED_KEY}=true\\s*$`, 'm').test(existing);
  return { installed: true, managed, stale: Boolean(appImage) && managed && existing !== desktopEntry(appImage), ...where };
}

/** Write the menu entry for APPIMAGE and copy the icon (when ICONSOURCE exists). Returns the paths written. */
function addToMenu({ appImage, iconSource, env = process.env, home = os.homedir() }) {
  if (!appImage || !path.isAbsolute(appImage)) throw new Error('Qorqbot is not running from an AppImage.');
  const where = locations(env, home);
  const state = menuEntryState({ appImage, env, home });
  if (state.installed && !state.managed) throw new Error(`${where.desktopFile} already exists and was not created by Qorqbot.`);
  fs.mkdirSync(path.dirname(where.desktopFile), { recursive: true });
  fs.writeFileSync(where.desktopFile, desktopEntry(appImage), { mode: 0o644 });
  let icon = null;
  if (iconSource) {
    try {
      const data = fs.readFileSync(iconSource); // also works for a file inside app.asar
      fs.mkdirSync(path.dirname(where.icon), { recursive: true });
      fs.writeFileSync(where.icon, data, { mode: 0o644 });
      icon = where.icon;
    } catch { /* no icon is not a reason to skip the entry */ }
  }
  return { desktopFile: where.desktopFile, icon };
}

/** Remove an entry this module wrote, and its icon. Returns false when there was nothing of ours to remove. */
function removeFromMenu({ env = process.env, home = os.homedir() }) {
  const state = menuEntryState({ env, home });
  if (!state.installed || !state.managed) return false;
  fs.rmSync(state.desktopFile, { force: true });
  fs.rmSync(state.icon, { force: true });
  return true;
}

function choiceFile(installParent) {
  return path.join(installParent, CHOICE_FILE);
}

/** 'accepted', 'declined' or null when the person was never asked. An unreadable file counts as never asked. */
function savedChoice(installParent) {
  try {
    const answer = JSON.parse(fs.readFileSync(choiceFile(installParent), 'utf8')).answer;
    return answer === 'accepted' || answer === 'declined' ? answer : null;
  } catch {
    return null;
  }
}

function saveChoice(installParent, answer) {
  fs.mkdirSync(installParent, { recursive: true, mode: 0o700 });
  fs.writeFileSync(choiceFile(installParent), JSON.stringify({ answer, at: new Date().toISOString() }) + '\n', { mode: 0o600 });
}

/** What to do about the applications menu at start-up.
 *   'none'     not an AppImage, already handled, declined earlier, or an entry that is not ours
 *   'refresh'  our entry points at a moved AppImage: rewrite it silently
 *   'ask'      first time: offer to add it
 *   'accept' | 'decline'   QORQBOT_DESKTOP_INTEGRATION answered for an automated check */
function startupPlan({ env = process.env, home = os.homedir(), installParent }) {
  if (!env.APPIMAGE) return 'none';
  const state = menuEntryState({ appImage: env.APPIMAGE, env, home });
  if (state.installed) return state.stale ? 'refresh' : 'none';
  const preset = env.QORQBOT_DESKTOP_INTEGRATION;
  if (preset === 'accept' || preset === 'decline') return preset;
  return savedChoice(installParent) === null ? 'ask' : 'none';
}

// ---------------------------------------------------------------- sandbox

function readNumber(read, file) {
  try {
    const value = Number(String(read(file)).trim());
    return Number.isFinite(value) ? value : null;
  } catch {
    return null;
  }
}

/** Kernel settings that decide whether Chromium's user-namespace sandbox can work. null means "not present". */
function userNamespaceFacts(read = (file) => fs.readFileSync(file, 'utf8')) {
  return {
    apparmorRestricted: readNumber(read, '/proc/sys/kernel/apparmor_restrict_unprivileged_userns'),
    cloneAllowed: readNumber(read, '/proc/sys/kernel/unprivileged_userns_clone'),
    maxNamespaces: readNumber(read, '/proc/sys/user/max_user_namespaces'),
  };
}

/** Pure. Whether Chromium's process sandbox is on for this run and why, for the log and the About dialog.
 *   argv: process.argv   env: process.env   facts: userNamespaceFacts() */
function sandboxStatus({ appImage = false, argv = [], env = {}, facts = {} }) {
  const restricted = facts.apparmorRestricted === 1 || facts.cloneAllowed === 0 || facts.maxNamespaces === 0;
  const disabledByFlag = argv.includes('--no-sandbox');
  const disabledByEnv = Boolean(env.ELECTRON_DISABLE_SANDBOX) && env.ELECTRON_DISABLE_SANDBOX !== '0';
  const sandboxed = !disabledByFlag && !disabledByEnv;
  let note;
  if (sandboxed) {
    note = 'Browser sandbox: on.';
  } else if (restricted) {
    note = appImage
      ? 'Browser sandbox: off. This system blocks unprivileged user namespaces (Ubuntu 23.10 and newer), which an AppImage needs for the sandbox. Install the .deb package for a sandboxed app.'
      : 'Browser sandbox: off. This system blocks unprivileged user namespaces.';
  } else {
    note = 'Browser sandbox: off (started with --no-sandbox).';
  }
  return { sandboxed, userNamespacesRestricted: restricted, appImage: Boolean(appImage), note };
}

// ---------------------------------------------------------------- system tray

/** Pure. Reads the answer of org.freedesktop.DBus.NameHasOwner from gdbus ("(true,)") or dbus-send ("boolean true"). */
function parseNameHasOwner(output) {
  const text = String(output);
  if (/\btrue\b/.test(text)) return true;
  if (/\bfalse\b/.test(text)) return false;
  return null;
}

function runTool(program, args, timeout) {
  return new Promise((resolve) => {
    execFile(program, args, { timeout, encoding: 'utf8' }, (error, stdout) => {
      if (error && error.code === 'ENOENT') resolve({ missing: true });
      else resolve({ ok: !error, stdout: stdout || '' });
    });
  });
}

/** Whether a desktop panel is showing tray icons (a StatusNotifierWatcher owns its name on the session bus).
 * true/false, or null when this cannot be told because neither gdbus nor dbus-send is installed. RUN is replaceable
 * for tests: (program, args, timeoutMs) => Promise<{ missing } | { ok, stdout }>. */
async function trayHostPresent({ run = runTool, timeout = 2000 } = {}) {
  const attempts = [
    ['gdbus', ['call', '--session', '--dest', 'org.freedesktop.DBus', '--object-path', '/org/freedesktop/DBus',
      '--method', 'org.freedesktop.DBus.NameHasOwner', STATUS_WATCHER]],
    ['dbus-send', ['--session', '--print-reply', '--dest=org.freedesktop.DBus', '/org/freedesktop/DBus',
      'org.freedesktop.DBus.NameHasOwner', 'string:' + STATUS_WATCHER]],
  ];
  for (const [program, args] of attempts) {
    const result = await run(program, args, timeout);
    if (result.missing) continue;
    // No session bus, a timeout or an error all mean no panel can be showing an icon.
    return result.ok ? parseNameHasOwner(result.stdout) === true : false;
  }
  return null;
}

module.exports = {
  DESKTOP_FILE_NAME, ICON_NAME,
  dataHome, desktopEntry, quoteExecArgument, menuEntryState, addToMenu, removeFromMenu,
  savedChoice, saveChoice, startupPlan,
  userNamespaceFacts, sandboxStatus,
  parseNameHasOwner, trayHostPresent,
};
