'use strict';
// Tests for the Linux desktop helpers: applications-menu entry, sandbox status, tray check.
// Everything runs against temporary HOME / XDG_DATA_HOME folders and plain env objects.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test, beforeEach, afterEach } = require('node:test');
const desktop = require('../desktop');

let tmp;
let home;
let parent;
let appImage;
let icon;

const envFor = (extra = {}) => ({ HOME: home, APPIMAGE: appImage, ...extra });
const entryFile = (dataHome = path.join(home, '.local', 'share')) => path.join(dataHome, 'applications', 'qorqbot.desktop');

beforeEach(() => {
  tmp = fs.realpathSync(fs.mkdtempSync('/tmp/qbd-'));
  home = path.join(tmp, 'home');
  parent = path.join(home, 'Qorqbot');
  appImage = path.join(home, 'Apps', 'Qorqbot-1.0.0-x86_64.AppImage');
  icon = path.join(tmp, 'qorqbot.png');
  fs.mkdirSync(home, { recursive: true });
  fs.writeFileSync(icon, 'PNG-BYTES');
});

afterEach(() => fs.rmSync(tmp, { recursive: true, force: true }));

// ---------------------------------------------------------------- applications menu

test('the data directory follows XDG_DATA_HOME only when it is absolute', () => {
  assert.equal(desktop.dataHome({}, '/home/a'), '/home/a/.local/share');
  assert.equal(desktop.dataHome({ XDG_DATA_HOME: '/data/x' }, '/home/a'), '/data/x');
  assert.equal(desktop.dataHome({ XDG_DATA_HOME: 'relative/dir' }, '/home/a'), '/home/a/.local/share');
  assert.equal(desktop.dataHome({ XDG_DATA_HOME: '' }, '/home/a'), '/home/a/.local/share');
});

test('Exec arguments are quoted for the Desktop Entry specification', () => {
  assert.equal(desktop.quoteExecArgument('/home/a b/Q.AppImage'), '"/home/a b/Q.AppImage"');
  assert.equal(desktop.quoteExecArgument('/x/$HOME/`id`/"q"'), '"/x/\\\\$HOME/\\\\`id\\\\`/\\\\"q\\\\""');
  assert.equal(desktop.quoteExecArgument('/x/100%/a\\b'), '"/x/100%%/a\\\\\\\\b"');
});

test('the menu entry has the fields the window manager and the menu need', () => {
  const text = desktop.desktopEntry('/opt apps/Qorqbot.AppImage');
  const lines = text.trimEnd().split('\n');
  assert.equal(lines[0], '[Desktop Entry]');
  assert.ok(lines.includes('Exec="/opt apps/Qorqbot.AppImage" %U'));
  for (const line of ['Type=Application', 'Name=Qorqbot', 'Icon=qorqbot', 'StartupWMClass=qorqbot', 'Categories=Office;', 'Terminal=false']) {
    assert.ok(lines.includes(line), line);
  }
});

test('nothing is done unless the app runs as an AppImage', () => {
  assert.equal(desktop.startupPlan({ env: { HOME: home }, home, installParent: parent }), 'none');
  assert.equal(desktop.startupPlan({ env: { HOME: home, QORQBOT_DESKTOP_INTEGRATION: 'accept' }, home, installParent: parent }), 'none');
  assert.equal(fs.existsSync(entryFile()), false);
});

test('the first AppImage launch asks, and the answer is never asked again', () => {
  const plan = () => desktop.startupPlan({ env: envFor(), home, installParent: parent });
  assert.equal(plan(), 'ask');
  desktop.saveChoice(parent, 'declined');
  assert.equal(plan(), 'none');
  assert.equal(desktop.savedChoice(parent), 'declined');
  assert.equal(fs.statSync(path.join(parent, 'desktop-integration.json')).mode & 0o777, 0o600);
  desktop.saveChoice(parent, 'accepted');
  assert.equal(plan(), 'none', 'an accepted entry that was deleted by hand is not offered again');
});

test('an unreadable answer file counts as never asked', () => {
  fs.mkdirSync(parent, { recursive: true });
  fs.writeFileSync(path.join(parent, 'desktop-integration.json'), '{not json');
  assert.equal(desktop.savedChoice(parent), null);
  fs.writeFileSync(path.join(parent, 'desktop-integration.json'), JSON.stringify({ answer: 'maybe' }));
  assert.equal(desktop.savedChoice(parent), null);
  assert.equal(desktop.startupPlan({ env: envFor(), home, installParent: parent }), 'ask');
});

test('QORQBOT_DESKTOP_INTEGRATION answers for automated checks', () => {
  assert.equal(desktop.startupPlan({ env: envFor({ QORQBOT_DESKTOP_INTEGRATION: 'accept' }), home, installParent: parent }), 'accept');
  assert.equal(desktop.startupPlan({ env: envFor({ QORQBOT_DESKTOP_INTEGRATION: 'decline' }), home, installParent: parent }), 'decline');
  desktop.saveChoice(parent, 'declined');
  assert.equal(desktop.startupPlan({ env: envFor({ QORQBOT_DESKTOP_INTEGRATION: 'accept' }), home, installParent: parent }), 'accept',
    'the preset wins over an earlier answer');
  assert.equal(desktop.startupPlan({ env: envFor({ QORQBOT_DESKTOP_INTEGRATION: 'whatever' }), home, installParent: parent }), 'none');
});

test('adding writes the entry and the 512px icon under ~/.local/share', () => {
  const env = envFor();
  const written = desktop.addToMenu({ appImage, iconSource: icon, env, home });
  assert.equal(written.desktopFile, entryFile());
  assert.equal(written.icon, path.join(home, '.local/share/icons/hicolor/512x512/apps/qorqbot.png'));
  assert.equal(fs.readFileSync(written.icon, 'utf8'), 'PNG-BYTES');
  assert.equal(fs.readFileSync(written.desktopFile, 'utf8'), desktop.desktopEntry(appImage));
  assert.equal(desktop.menuEntryState({ appImage, env, home }).installed, true);
  assert.equal(desktop.startupPlan({ env, home, installParent: parent }), 'none');
});

test('XDG_DATA_HOME decides where the entry and the icon go', () => {
  const dataHome = path.join(tmp, 'xdg');
  const env = envFor({ XDG_DATA_HOME: dataHome });
  desktop.addToMenu({ appImage, iconSource: icon, env, home });
  assert.ok(fs.existsSync(entryFile(dataHome)));
  assert.ok(fs.existsSync(path.join(dataHome, 'icons/hicolor/512x512/apps/qorqbot.png')));
  assert.equal(fs.existsSync(path.join(home, '.local')), false);
  assert.equal(desktop.removeFromMenu({ env, home }), true);
  assert.equal(fs.existsSync(entryFile(dataHome)), false);
});

test('a missing icon file does not stop the entry', () => {
  const written = desktop.addToMenu({ appImage, iconSource: path.join(tmp, 'missing.png'), env: envFor(), home });
  assert.equal(written.icon, null);
  assert.ok(fs.existsSync(written.desktopFile));
});

test('a moved AppImage is noticed and the entry is refreshed', () => {
  const env = envFor();
  desktop.addToMenu({ appImage, iconSource: icon, env, home });
  const moved = path.join(home, 'Downloads', 'Qorqbot-1.0.0-x86_64.AppImage');
  const movedEnv = envFor({ APPIMAGE: moved });
  assert.equal(desktop.menuEntryState({ appImage: moved, env: movedEnv, home }).stale, true);
  assert.equal(desktop.startupPlan({ env: movedEnv, home, installParent: parent }), 'refresh');
  desktop.addToMenu({ appImage: moved, iconSource: icon, env: movedEnv, home });
  assert.match(fs.readFileSync(entryFile(), 'utf8'), /^Exec="[^"]*Downloads\/Qorqbot-1\.0\.0-x86_64\.AppImage" %U$/m);
  assert.equal(desktop.startupPlan({ env: movedEnv, home, installParent: parent }), 'none');
});

test('an entry that Qorqbot did not write is never replaced, refreshed or removed', () => {
  const env = envFor();
  fs.mkdirSync(path.dirname(entryFile()), { recursive: true });
  fs.writeFileSync(entryFile(), '[Desktop Entry]\nName=Mine\nExec=/x/other\n');
  assert.throws(() => desktop.addToMenu({ appImage, iconSource: icon, env, home }), /not created by Qorqbot/);
  assert.equal(desktop.startupPlan({ env, home, installParent: parent }), 'none');
  assert.equal(desktop.removeFromMenu({ env, home }), false);
  assert.match(fs.readFileSync(entryFile(), 'utf8'), /Name=Mine/);
});

test('removing deletes the entry and the icon and reports when nothing was there', () => {
  const env = envFor();
  assert.equal(desktop.removeFromMenu({ env, home }), false);
  const { icon: installedIcon } = desktop.addToMenu({ appImage, iconSource: icon, env, home });
  assert.equal(desktop.removeFromMenu({ env, home }), true);
  assert.equal(fs.existsSync(entryFile()), false);
  assert.equal(fs.existsSync(installedIcon), false);
});

test('adding outside an AppImage is refused', () => {
  assert.throws(() => desktop.addToMenu({ appImage: undefined, env: { HOME: home }, home }), /not running from an AppImage/);
  assert.throws(() => desktop.addToMenu({ appImage: 'relative.AppImage', env: { HOME: home }, home }), /not running from an AppImage/);
});

// ---------------------------------------------------------------- sandbox

const status = (input) => desktop.sandboxStatus(input);

test('sandbox status: on when nothing disabled it', () => {
  const result = status({ appImage: true, argv: ['/x/qorqbot'], env: {}, facts: { apparmorRestricted: 0, cloneAllowed: 1, maxNamespaces: 1000 } });
  assert.equal(result.sandboxed, true);
  assert.equal(result.userNamespacesRestricted, false);
  assert.match(result.note, /sandbox: on/);
});

test('sandbox status: an AppImage on Ubuntu 24.04 runs with --no-sandbox and says why', () => {
  const result = status({ appImage: true, argv: ['/x/qorqbot', '--no-sandbox'], env: {}, facts: { apparmorRestricted: 1, cloneAllowed: 1, maxNamespaces: 63000 } });
  assert.equal(result.sandboxed, false);
  assert.equal(result.userNamespacesRestricted, true);
  assert.match(result.note, /off.*user namespaces.*AppImage.*\.deb/s);
});

test('sandbox status: each kernel setting that blocks user namespaces counts as restricted', () => {
  for (const facts of [{ apparmorRestricted: 1 }, { cloneAllowed: 0 }, { maxNamespaces: 0 }]) {
    assert.equal(status({ argv: ['--no-sandbox'], facts }).userNamespacesRestricted, true, JSON.stringify(facts));
  }
  for (const facts of [{}, { apparmorRestricted: null }, { apparmorRestricted: 0, cloneAllowed: 1, maxNamespaces: 5 }]) {
    assert.equal(status({ argv: [], facts }).userNamespacesRestricted, false, JSON.stringify(facts));
  }
});

test('sandbox status: ELECTRON_DISABLE_SANDBOX and a plain --no-sandbox are recognised', () => {
  assert.equal(status({ env: { ELECTRON_DISABLE_SANDBOX: '1' } }).sandboxed, false);
  assert.equal(status({ env: { ELECTRON_DISABLE_SANDBOX: '0' } }).sandboxed, true);
  const plain = status({ appImage: false, argv: ['--no-sandbox'], facts: {} });
  assert.equal(plain.sandboxed, false);
  assert.match(plain.note, /started with --no-sandbox/);
});

test('user namespace facts are read from /proc and tolerate missing files', () => {
  const files = {
    '/proc/sys/kernel/apparmor_restrict_unprivileged_userns': '1\n',
    '/proc/sys/user/max_user_namespaces': '256023\n',
  };
  const facts = desktop.userNamespaceFacts((file) => {
    if (file in files) return files[file];
    throw Object.assign(new Error('missing'), { code: 'ENOENT' });
  });
  assert.deepEqual(facts, { apparmorRestricted: 1, cloneAllowed: null, maxNamespaces: 256023 });
  assert.deepEqual(desktop.userNamespaceFacts(() => 'garbage'), { apparmorRestricted: null, cloneAllowed: null, maxNamespaces: null });
});

// ---------------------------------------------------------------- tray

test('NameHasOwner answers are parsed from gdbus and dbus-send output', () => {
  assert.equal(desktop.parseNameHasOwner('(true,)\n'), true);
  assert.equal(desktop.parseNameHasOwner('(false,)\n'), false);
  assert.equal(desktop.parseNameHasOwner('method return time=1 sender=:1.1 -> destination=:1.2 serial=3 reply_serial=2\n   boolean true\n'), true);
  assert.equal(desktop.parseNameHasOwner('   boolean false\n'), false);
  assert.equal(desktop.parseNameHasOwner('nonsense'), null);
});

test('the tray check asks gdbus first, then dbus-send, and says unknown when neither exists', async () => {
  const calledWith = [];
  const answers = (table) => async (program, args) => {
    calledWith.push(program);
    return table[program] || { missing: true };
  };
  assert.equal(await desktop.trayHostPresent({ run: answers({ gdbus: { ok: true, stdout: '(true,)' } }) }), true);
  assert.equal(await desktop.trayHostPresent({ run: answers({ gdbus: { ok: true, stdout: '(false,)' } }) }), false);
  calledWith.length = 0;
  assert.equal(await desktop.trayHostPresent({ run: answers({ 'dbus-send': { ok: true, stdout: '   boolean true' } }) }), true);
  assert.deepEqual(calledWith, ['gdbus', 'dbus-send']);
  assert.equal(await desktop.trayHostPresent({ run: answers({}) }), null);
});

test('no session bus, an error or a timeout means no tray is showing', async () => {
  assert.equal(await desktop.trayHostPresent({ run: async () => ({ ok: false, stdout: '' }) }), false);
  assert.equal(await desktop.trayHostPresent({ run: async (program) => (program === 'gdbus' ? { missing: true } : { ok: false, stdout: '' }) }), false);
});

test('the real tool call returns a boolean or null without throwing', async () => {
  const result = await desktop.trayHostPresent({ timeout: 1500 });
  assert.ok(result === true || result === false || result === null);
});
