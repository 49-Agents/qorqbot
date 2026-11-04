'use strict';
// Engine tests against a fake Qorqbot folder whose ./qorqbot has the same CLI
// surface (setup, status, stop, update, --port) and serves HTTP with Python.
//   npm test   (macOS, needs python3.12 on PATH)
const assert = require('node:assert/strict');
const { execFileSync, spawnSync } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { test, beforeEach, afterEach } = require('node:test');
const { Engine, EngineError, INSTALL_COMMAND } = require('../engine');

// String.raw keeps \${ literally; it is unescaped below so bash sees ${.
const FAKE_QORQBOT = String.raw`#!/bin/bash
root=$(cd "$(dirname "$0")" && pwd -P)
mkdir -p "$root/.run"
case "\${1-}" in
  setup) echo setup >>"$root/calls"; echo 'Preparing Qorqbot from the bundled files'; echo 'Qorqbot is ready.';;
  status)
    if [ -f "$root/.run/pid" ] && kill -0 "$(cat "$root/.run/pid")" 2>/dev/null; then
      echo "Running at $(cat "$root/.run/url")$([ -f "$root/.run/terminal" ] && echo ' from Terminal')"; exit 0; fi
    echo Stopped; exit 1;;
  stop)
    echo stop >>"$root/calls"
    if [ -f "$root/.run/pid" ]; then kill "$(cat "$root/.run/pid")" 2>/dev/null; rm -f "$root/.run/pid"; fi
    echo Stopped;;
  install-command)
    [ -n "\${FAKE_COMMAND_FAIL:-}" ] && { echo 'Qorqbot: /x/qorqbot already exists and was not created by Qorqbot'; exit 1; }
    echo install-command >>"$root/calls"; echo "Installed /x/qorqbot";;
  update)
    echo "update $2 $4" >>"$root/calls"
    [ -n "\${FAKE_UPDATE_FAIL:-}" ] && { echo 'Qorqbot update: refused'; exit 1; }
    version=$(tar -xzOf "$2" qorqbot/runtime/PAYLOAD.json | "$QORQUT_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["version"])')
    printf '{"version": "%s"}' "$version" >"$root/runtime/PAYLOAD.json";;
  --port)
    [ -n "\${FAKE_START_FAIL:-}" ] && { echo 'Qorqbot: exited during startup'; exit 1; }
    echo "start $2" >>"$root/calls"
    echo "http://127.0.0.1:$2" >"$root/.run/url"; echo $$ >"$root/.run/pid"
    exec "$QORQUT_PYTHON" -m http.server "$2" --bind 127.0.0.1 --directory "$root/runtime";;
esac
`.replaceAll('\\${', '${');

const PYTHON = execFileSync('/bin/sh', ['-c', 'command -v python3.12']).toString().trim();
let base;
let engines;

function makePayload(version, { checksum } = {}) {
  const payload = path.join(base, `payload-${version}`);
  const stage = path.join(base, `stage-${version}`);
  fs.rmSync(payload, { recursive: true, force: true });
  fs.rmSync(stage, { recursive: true, force: true });
  fs.mkdirSync(path.join(stage, 'qorqbot', 'runtime'), { recursive: true });
  fs.mkdirSync(payload);
  fs.writeFileSync(path.join(stage, 'qorqbot', 'qorqbot'), FAKE_QORQBOT, { mode: 0o755 });
  fs.writeFileSync(path.join(stage, 'qorqbot', 'runtime', 'PAYLOAD.json'), JSON.stringify({ version }));
  const name = `qorqbot-${version}-macos14-arm64.tar.gz`;
  execFileSync('/usr/bin/tar', ['-czf', path.join(payload, name), '-C', stage, 'qorqbot']);
  const digest = crypto.createHash('sha256').update(fs.readFileSync(path.join(payload, name))).digest('hex');
  fs.writeFileSync(path.join(payload, 'ARCHIVE_NAME'), name + '\n');
  fs.writeFileSync(path.join(payload, 'ARCHIVE_SHA256'), (checksum || digest) + '\n');
  fs.writeFileSync(path.join(payload, 'VERSION'), version + '\n');
  return { payload, archive: path.join(payload, name), digest };
}

function freePort() {
  return new Promise((resolve) => {
    const server = net.createServer().listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function engineFor(payload, extra = {}) {
  const engine = new Engine({
    payload, home: path.join(base, 'h'), firstPort: await freePort(),
    env: { PATH: process.env.PATH, HOME: base, QORQUT_PYTHON: PYTHON, ...extra.env }, loginPath: false, ...extra.options,
  });
  engines.push(engine);
  return engine;
}

function calls(engine) {
  const file = path.join(engine.root, 'calls');
  return fs.existsSync(file) ? fs.readFileSync(file, 'utf8').trim().split('\n') : [];
}

beforeEach(() => {
  // /tmp keeps the install path under the socket-path limit.
  base = fs.realpathSync(fs.mkdtempSync('/tmp/qbn-'));
  engines = [];
});

afterEach(async () => {
  for (const engine of engines) {
    if (engine.isInstalled()) spawnSync('./qorqbot', ['stop'], { cwd: engine.root });
  }
  fs.rmSync(base, { recursive: true, force: true });
});

test('first launch installs, sets up, starts and reports the URL', async () => {
  const engine = await engineFor(makePayload('1.0.0').payload);
  const lines = [];
  assert.equal(engine.isInstalled(), false);
  await engine.install({ onLine: (line) => lines.push(line) });
  const url = await engine.start();
  assert.equal(url, `http://127.0.0.1:${engine.firstPort}`);
  assert.deepEqual(calls(engine), ['setup', `start ${engine.firstPort}`]);
  assert.ok(lines.some((line) => line.includes('Preparing Qorqbot')), 'setup output is streamed');
  assert.equal(fs.statSync(engine.installParent).mode & 0o777, 0o700);
  assert.deepEqual(fs.readdirSync(engine.installParent).filter((name) => name.startsWith('.installing')), []);
  assert.equal(await engine.status(), url);
});

test('stop, status and restart reuse the installation', async () => {
  const engine = await engineFor(makePayload('1.0.0').payload);
  await engine.install();
  await engine.start();
  await engine.stop();
  assert.equal(await engine.status(), null);
  assert.equal(engine.server, null);
  await engine.start();
  assert.deepEqual(calls(engine).filter((call) => call === 'setup'), ['setup']);
  assert.equal(calls(engine).filter((call) => call.startsWith('start')).length, 2);
});

test('a running server from an earlier session is adopted by status', async () => {
  const { payload } = makePayload('1.0.0');
  const first = await engineFor(payload);
  await first.install();
  const url = await first.start();
  const second = await engineFor(payload);
  assert.equal(await second.status(), url);
});

test('busy port moves to the next port', async () => {
  const engine = await engineFor(makePayload('1.0.0').payload);
  await engine.install();
  const busy = net.createServer();
  await new Promise((resolve) => busy.listen(engine.firstPort, '127.0.0.1', resolve));
  try {
    const url = await engine.start();
    assert.equal(url, `http://127.0.0.1:${engine.firstPort + 1}`);
  } finally {
    busy.close();
  }
});

test('a new app session keeps the chosen port after a competing app exits', async () => {
  const { payload } = makePayload('1.0.0');
  const engine = await engineFor(payload);
  await engine.install();
  const busy = net.createServer();
  await new Promise(resolve => busy.listen(engine.firstPort, '127.0.0.1', resolve));
  try {
    const original = await engine.start();
    await engine.stop();
    await new Promise(resolve => busy.close(resolve));
    const next = await engineFor(payload, { options: { firstPort: engine.firstPort } });
    assert.equal(await next.start(), original);
  } finally { if (busy.listening) busy.close(); }
});

test('a busy saved port is refused without moving existing agents to another address', async () => {
  const engine = await engineFor(makePayload('1.0.0').payload);
  await engine.install();
  await engine.start();
  await engine.stop();
  const busy = net.createServer();
  await new Promise(resolve => busy.listen(engine.firstPort, '127.0.0.1', resolve));
  try {
    await assert.rejects(engine.start(), error => error.code === 'ports');
    assert.equal(calls(engine).filter(call => call.startsWith('start')).length, 1);
  } finally { busy.close(); }
});

test('bad checksum installs nothing', async () => {
  const engine = await engineFor(makePayload('1.0.0', { checksum: '0'.repeat(64) }).payload);
  await assert.rejects(engine.install(), (error) => error instanceof EngineError && error.code === 'checksum');
  assert.equal(fs.existsSync(engine.root), false);
});

test('an incomplete existing folder is never replaced', async () => {
  const engine = await engineFor(makePayload('1.0.0').payload);
  fs.mkdirSync(engine.root, { recursive: true });
  fs.writeFileSync(path.join(engine.root, 'keep'), 'owner data');
  await assert.rejects(engine.install(), (error) => error.code === 'incomplete');
  assert.equal(fs.readFileSync(path.join(engine.root, 'keep'), 'utf8'), 'owner data');
});

test('a too-long install path is refused before unpacking', async () => {
  const { payload } = makePayload('1.0.0');
  const engine = await engineFor(payload, { options: { home: path.join(base, 'x'.repeat(40)) } });
  await assert.rejects(engine.install(), (error) => error.code === 'path');
  assert.equal(fs.existsSync(engine.root), false);
});

test('missing or wrong Python is reported with the install command', async () => {
  const fake = path.join(base, 'python3.11');
  fs.writeFileSync(fake, '#!/bin/sh\nexit 1\n', { mode: 0o755 });
  for (const python of ['/nonexistent/python3.12', fake]) {
    const engine = await engineFor(makePayload('1.0.0').payload, { env: { QORQUT_PYTHON: python } });
    const { missing } = await engine.prerequisites();
    assert.deepEqual(missing, ['Python 3.12']);
    await assert.rejects(engine.start(), (error) => error.code === 'prerequisites' && error.command === INSTALL_COMMAND);
  }
});

test('present prerequisites report nothing missing', async () => {
  const engine = await engineFor(makePayload('1.0.0').payload);
  assert.deepEqual((await engine.prerequisites()).missing, []);
});

test('start failure carries the log tail', async () => {
  const engine = await engineFor(makePayload('1.0.0').payload, { env: { FAKE_START_FAIL: '1' } });
  await engine.install();
  await assert.rejects(engine.start(), (error) => error.code === 'start' && error.detail.includes('exited during startup'));
});

test('a newer bundle updates through the folder updater', async () => {
  const engine = await engineFor(makePayload('1.0.0').payload);
  await engine.install();
  await engine.start();
  const newer = makePayload('1.1.0');
  const updater = await engineFor(newer.payload);
  assert.equal(updater.updateAvailable(), true);
  await updater.update();
  assert.ok(calls(updater).includes('stop'), 'running server is stopped first');
  assert.ok(calls(updater).includes(`update ${newer.archive} ${newer.digest}`));
  assert.equal(updater.installedVersion(), '1.1.0');
  assert.equal(updater.updateAvailable(), false);
});

test('a refused update keeps the installed version and reports why', async () => {
  const engine = await engineFor(makePayload('1.0.0').payload);
  await engine.install();
  const updater = await engineFor(makePayload('1.1.0').payload, { env: { FAKE_UPDATE_FAIL: '1' } });
  await assert.rejects(updater.update(), (error) => error.code === 'update' && error.detail.includes('refused'));
  assert.equal(updater.installedVersion(), '1.0.0');
});

test('same version is not offered as an update', async () => {
  const engine = await engineFor(makePayload('1.0.0').payload);
  assert.equal(engine.updateAvailable(), false);
  await engine.install();
  assert.equal(engine.updateAvailable(), false);
});

test('older releases, older prereleases and build metadata never trigger an update', async () => {
  for (const [current, bundled, expected] of [
    ['1.3.0-preview.1', '1.3.0-local.85a24e2e7724', false],
    ['1.3.0-preview.1', '1.3.0-preview.2', true],
    ['1.1.0', '1.0.0', false],
    ['1.1.0', '1.1.0-rc.1', false],
    ['1.1.0-rc.10', '1.1.0-rc.2', false],
    ['1.1.0+build.1', '1.1.0+build.2', false],
    ['1.1.0-rc.2', '1.1.0-rc.10', true],
    ['1.1.0-rc.2', '1.1.0', true],
  ]) {
    const installed = await engineFor(makePayload(current).payload);
    fs.rmSync(installed.root, { recursive: true, force: true });
    await installed.install();
    const updater = await engineFor(makePayload(bundled).payload);
    assert.equal(updater.updateAvailable(), expected, `${current} -> ${bundled}`);
    if (!expected) {
      await assert.rejects(updater.update(), error => error.code === 'update');
      assert.equal(calls(updater).some(call => call.startsWith('update ')), false);
    }
  }
});

test('login shell PATH is read between markers, ignoring banners', async () => {
  const shell = path.join(base, 'fakeshell');
  fs.writeFileSync(shell, '#!/bin/sh\necho "banner noise"\nprintf "__QBPATH__/login/bin:/usr/bin__QBPATH__"\n', { mode: 0o755 });
  const engine = await engineFor(makePayload('1.0.0').payload, { env: { SHELL: shell, PATH: '/bin' }, options: { loginPath: true } });
  const env = await engine.environment();
  assert.equal(env.PATH, '/login/bin:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin');
});

test('a hanging login shell times out and falls back', async () => {
  const shell = path.join(base, 'hangshell');
  fs.writeFileSync(shell, '#!/bin/sh\nsleep 30\n', { mode: 0o755 });
  const engine = await engineFor(makePayload('1.0.0').payload, { env: { SHELL: shell, PATH: '/bin' }, options: { loginPath: true } });
  const started = Date.now();
  const env = await engine.environment();
  assert.ok(Date.now() - started < 8000);
  assert.equal(env.PATH, '/bin:/opt/homebrew/bin:/usr/local/bin');
  assert.match(fs.readFileSync(engine.logFile, 'utf8'), /timed out/);
});

test('quitting stops an app-started server but not one started from Terminal', async () => {
  const engine = await engineFor(makePayload('1.0.0').payload);
  assert.equal(engine.ownsServer(), false, 'nothing running');
  await engine.install();
  await engine.start();
  assert.equal(engine.ownsServer(), true);
  fs.writeFileSync(path.join(engine.root, '.run', 'terminal'), '');
  const attached = await engineFor(makePayload('1.0.0').payload);
  assert.ok(await attached.status());
  assert.equal(attached.ownsServer(), false);
  fs.rmSync(path.join(engine.root, '.run', 'terminal'));
  await attached.status();
  assert.equal(attached.ownsServer(), true, 'an orphaned app server is still stopped on quit');
});

test('the Terminal command is installed through the folder launcher', async () => {
  const engine = await engineFor(makePayload('1.0.0').payload);
  await assert.rejects(engine.installCommand(), /not installed yet/);
  await engine.install();
  assert.equal(await engine.installCommand(), 'Installed /x/qorqbot');
  assert.ok(calls(engine).includes('install-command'));
  const failing = await engineFor(makePayload('1.0.0').payload, { env: { FAKE_COMMAND_FAIL: '1' } });
  await assert.rejects(failing.installCommand(), (error) => error instanceof EngineError && /already exists/.test(error.detail));
});

function onlyCommands(engine, available) {
  engine.which = (command) => (available.includes(command) ? '/fake/bin/' + command : null);
  return engine;
}

test('checklist installs only the missing required programs and never blocks on agent CLIs', async () => {
  const engine = onlyCommands(await engineFor(makePayload('1.0.0').payload), ['brew', 'git']);
  const list = await engine.checklist();
  assert.equal(list.ready, false);
  assert.equal(list.command, 'brew install tmux');
  assert.deepEqual(list.tools.map((tool) => [tool.id, tool.ok]), [['python', true], ['git', true], ['tmux', false]]);
  assert.deepEqual(list.agents.map((agent) => [agent.id, agent.ok]), [['claude', false], ['codex', false]]);
  assert.equal(list.agentReady, false);
  assert.equal(list.homebrew, true);
});

test('checklist is ready without any agent CLI and reports a missing Homebrew', async () => {
  const engine = onlyCommands(await engineFor(makePayload('1.0.0').payload), ['git', 'tmux', 'codex']);
  const list = await engine.checklist();
  assert.equal(list.ready, true);
  assert.equal(list.command, '');
  assert.equal(list.homebrew, false);
  assert.equal(list.agentReady, true);
  assert.deepEqual(list.agents.filter((agent) => agent.ok).map((agent) => agent.id), ['codex']);
});

test('checklist names the Homebrew formula for a missing Python', async () => {
  const engine = onlyCommands(await engineFor(makePayload('1.0.0').payload, { env: { QORQUT_PYTHON: '/nonexistent/python3.12' } }), []);
  const list = await engine.checklist({ log: false });
  assert.equal(list.command, 'brew install python@3.12 git tmux');
  assert.doesNotMatch(fs.existsSync(engine.logFile) ? fs.readFileSync(engine.logFile, 'utf8') : '', /missing prerequisites/);
});
