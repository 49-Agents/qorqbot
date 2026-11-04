'use strict';
// Install/start/stop/update engine for the Qorqbot Mac app.
//
// The app bundle carries the exact native archive unchanged. Qorqbot runs from
// a writable folder at a short fixed path (~/Qorqbot/qorqbot), so the first
// launch unpacks the archive there and runs the folder's own offline setup.
// Every server operation goes through that folder's ./qorqbot command; this
// module never edits the folder's runtime or records itself.
//
// No Electron imports: the engine is plain Node so it can be tested directly.

const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const semver = require('semver');

const INSTALL_COMMAND = 'brew install python@3.12 git tmux';
// native_launch.py refuses roots whose Unix socket paths exceed 104 bytes;
// '<root>/.run/c/org.chromium.Chromium.XXXXXX/SingletonSocket' needs root <= 50.
const MAX_ROOT_LENGTH = 50;
const PORT_RANGE = 20;
const READY_TIMEOUT_MS = 180_000;
const LOGIN_SHELL_TIMEOUT_MS = 5_000;

class EngineError extends Error {
  // code: machine-readable kind; detail: log excerpt or extra text for the UI.
  constructor(code, message, { detail = '', command = '' } = {}) {
    super(message);
    this.code = code;
    this.detail = detail;
    this.command = command;
  }
}

function sha256File(file) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash('sha256');
    fs.createReadStream(file).on('error', reject).on('data', (chunk) => hash.update(chunk))
      .on('end', () => resolve(hash.digest('hex')));
  });
}

function readTrimmed(file) {
  return fs.readFileSync(file, 'utf8').trim();
}

function portFree(port) {
  return new Promise((resolve) => {
    const probe = net.createServer();
    probe.once('error', () => resolve(false));
    probe.listen({ port, host: '127.0.0.1', exclusive: true }, () => probe.close(() => resolve(true)));
  });
}

function httpStatus(url) {
  return new Promise((resolve) => {
    const request = http.get(url, { timeout: 1000 }, (response) => {
      response.resume();
      resolve(response.statusCode);
    });
    request.on('timeout', () => request.destroy());
    request.on('error', () => resolve(0));
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class Engine {
  /**
   * @param {object} options
   * @param {string} options.payload  directory with ARCHIVE_NAME, ARCHIVE_SHA256, VERSION and the archive
   * @param {string} [options.home]    install parent (default ~/Qorqbot)
   * @param {number} [options.firstPort]
   * @param {object} [options.env]     base environment (default process.env)
   * @param {boolean} [options.loginPath] read PATH from the login shell (default true)
   */
  constructor({ payload, home, firstPort = 1049, env = process.env, loginPath = true }) {
    this.payload = payload;
    this.installParent = home || path.join(os.homedir(), 'Qorqbot');
    this.root = path.join(this.installParent, 'qorqbot');
    this.logFile = path.join(this.installParent, 'launcher.log');
    this.portFile = path.join(this.installParent, 'server-port');
    this.appLog = path.join(this.root, 'instance', 'logs', 'application.log');
    this.firstPort = firstPort;
    this.baseEnv = { ...env };
    this.loginPath = loginPath;
    this.env = null;
    this.bundledVersion = readTrimmed(path.join(payload, 'VERSION'));
    this.archive = path.join(payload, readTrimmed(path.join(payload, 'ARCHIVE_NAME')));
    this.archiveSha256 = readTrimmed(path.join(payload, 'ARCHIVE_SHA256'));
    this.server = null; // child process started by this engine
    this.url = null;
  }

  // ---------------------------------------------------------------- logging

  ensureParent() {
    fs.mkdirSync(this.installParent, { recursive: true, mode: 0o700 });
    fs.chmodSync(this.installParent, 0o700);
  }

  log(line) {
    this.ensureParent();
    const stamp = new Date().toISOString().replace('T', ' ').slice(0, 19);
    fs.appendFileSync(this.logFile, `${stamp} ${line}\n`);
  }

  logTail(lines = 8) {
    try {
      return fs.readFileSync(this.logFile, 'utf8').trimEnd().split('\n').slice(-lines).join('\n');
    } catch {
      return '';
    }
  }

  // ---------------------------------------------------------------- environment

  /** Finder-started apps get a minimal PATH; add the login shell's PATH and Homebrew. */
  async environment() {
    if (this.env) return this.env;
    let extra = '';
    if (this.loginPath) extra = await this.readLoginPath();
    const base = this.baseEnv.PATH || '/usr/bin:/bin:/usr/sbin:/sbin';
    const env = { ...this.baseEnv };
    env.PATH = [extra, base, '/opt/homebrew/bin', '/usr/local/bin'].filter(Boolean).join(':');
    delete env.ELECTRON_RUN_AS_NODE;
    this.env = env;
    return env;
  }

  readLoginPath() {
    const shell = this.baseEnv.SHELL || '/bin/zsh';
    return new Promise((resolve) => {
      let output = '';
      let finished = false;
      const child = spawn(shell, ['-ilc', 'printf "__QBPATH__%s__QBPATH__" "$PATH"'], {
        stdio: ['ignore', 'pipe', 'ignore'], env: this.baseEnv, detached: true,
      });
      const done = (value) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        resolve(value);
      };
      const timer = setTimeout(() => {
        this.log('login shell PATH lookup timed out; using default locations');
        try { process.kill(-child.pid, 'SIGKILL'); } catch { /* already gone */ }
        done('');
      }, LOGIN_SHELL_TIMEOUT_MS);
      child.stdout.on('data', (chunk) => { output += chunk; });
      child.on('error', () => done(''));
      child.on('close', () => {
        const matches = [...output.matchAll(/__QBPATH__(.*?)__QBPATH__/gs)];
        done(matches.length ? matches[matches.length - 1][1] : '');
      });
    });
  }

  which(command, env) {
    for (const directory of env.PATH.split(':')) {
      if (!directory) continue;
      const candidate = path.join(directory, command);
      try {
        fs.accessSync(candidate, fs.constants.X_OK);
        if (fs.statSync(candidate).isFile()) return candidate;
      } catch { /* keep looking */ }
    }
    return null;
  }

  pythonUsable(python, env) {
    return new Promise((resolve) => {
      const child = spawn(python, ['-c', 'import sys, venv; sys.exit(sys.version_info[:2] != (3, 12))'],
        { env, stdio: 'ignore' });
      child.on('error', () => resolve(false));
      child.on('close', (code) => resolve(code === 0));
    });
  }

  /** @returns {{missing: string[], python: string|null}} */
  async prerequisites() {
    const env = await this.environment();
    const missing = [];
    let python = env.QORQUT_PYTHON || this.which('python3.12', env);
    if (!python && fs.existsSync('/opt/homebrew/opt/python@3.12/bin/python3.12')) {
      python = '/opt/homebrew/opt/python@3.12/bin/python3.12';
    }
    if (!python || !(await this.pythonUsable(python, env))) {
      missing.push('Python 3.12');
      python = null;
    } else {
      env.QORQUT_PYTHON = python;
    }
    if (!this.which('git', env)) missing.push('git');
    if (!this.which('tmux', env)) missing.push('tmux');
    if (missing.length) this.log('missing prerequisites: ' + missing.join(', '));
    return { missing, python };
  }

  async requirePrerequisites() {
    const { missing } = await this.prerequisites();
    if (missing.length) {
      throw new EngineError('prerequisites', `Qorqbot needs ${missing.join(', ')} on this Mac.`,
        { command: INSTALL_COMMAND });
    }
  }

  // ---------------------------------------------------------------- folder commands

  /** Run ./qorqbot ARGS in the folder; stream output lines to onLine and the log. */
  async run(args, { onLine = () => {}, allowFailure = false } = {}) {
    const env = await this.environment();
    return new Promise((resolve, reject) => {
      const child = spawn('./qorqbot', args, { cwd: this.root, env, stdio: ['ignore', 'pipe', 'pipe'] });
      let output = '';
      const take = (chunk) => {
        const text = chunk.toString();
        output += text;
        fs.appendFileSync(this.logFile, text);
        for (const line of text.split('\n')) if (line.trim()) onLine(line);
      };
      child.stdout.on('data', take);
      child.stderr.on('data', take);
      child.on('error', reject);
      child.on('close', (code) => {
        if (code !== 0 && !allowFailure) {
          reject(new EngineError('command', `./qorqbot ${args[0]} failed (exit ${code}).`, { detail: this.logTail() }));
        } else {
          resolve({ code, output });
        }
      });
    });
  }

  /** True when quitting the app should stop the server: it is running and was not started from Terminal.
   * A Terminal-started server is only attached to and keeps running after the app quits. */
  ownsServer() {
    return Boolean(this.url) && !this.terminalServer;
  }

  /** Install the `qorqbot` Terminal command for this folder. Resolves to the launcher's message. */
  async installCommand() {
    if (!this.isInstalled()) throw new EngineError('command', 'Qorqbot is not installed yet.');
    const { code, output } = await this.run(['install-command'], { allowFailure: true });
    if (code !== 0) {
      throw new EngineError('command', 'The qorqbot command was not installed.', { detail: output.trim() });
    }
    return output.trim();
  }

  // True exactly once per install folder: the first time the dashboard opens, the app shows Getting Started.
  claimFirstRunGuide() {
    try {
      fs.writeFileSync(path.join(this.installParent, '.getting-started-shown'), new Date().toISOString() + '\n', { flag: 'wx', mode: 0o600 });
      return true;
    } catch (error) {
      if (error.code === 'EEXIST') return false;
      this.log(`getting-started marker: ${error.message}`);
      return false;
    }
  }

  isInstalled() {
    try {
      fs.accessSync(path.join(this.root, 'qorqbot'), fs.constants.X_OK);
      return true;
    } catch {
      return false;
    }
  }

  installedVersion() {
    try {
      return JSON.parse(fs.readFileSync(path.join(this.root, 'runtime', 'PAYLOAD.json'), 'utf8')).version || null;
    } catch {
      return null;
    }
  }

  updateAvailable() {
    const current = this.installedVersion();
    return Boolean(this.isInstalled() && semver.valid(current) && semver.valid(this.bundledVersion)
      && semver.gt(this.bundledVersion, current));
  }

  async install({ onLine = () => {} } = {}) {
    this.ensureParent();
    if (fs.existsSync(this.root)) {
      throw new EngineError('incomplete', `${this.root} exists but is not a complete Qorqbot folder. Nothing was replaced. Move it aside and try again.`);
    }
    if (this.root.length > MAX_ROOT_LENGTH) {
      throw new EngineError('path', `The install folder path is too long for local terminal sockets (${this.root}).`);
    }
    onLine('Verifying the bundled package');
    if ((await sha256File(this.archive)) !== this.archiveSha256) {
      throw new EngineError('checksum', 'The bundled Qorqbot package is damaged (checksum mismatch). Download Qorqbot again.');
    }
    const staging = path.join(this.installParent, `.installing.${process.pid}`);
    await fsp.rm(staging, { recursive: true, force: true });
    await fsp.mkdir(staging, { mode: 0o700 });
    this.log(`installing ${this.bundledVersion} into ${this.root}`);
    onLine('Unpacking into ' + this.root);
    const code = await new Promise((resolve) => {
      const tar = spawn('/usr/bin/tar', ['-xzf', this.archive, '-C', staging], { stdio: 'ignore' });
      tar.on('error', () => resolve(-1));
      tar.on('close', resolve);
    });
    if (code !== 0 || !fs.existsSync(path.join(staging, 'qorqbot', 'qorqbot'))) {
      await fsp.rm(staging, { recursive: true, force: true });
      throw new EngineError('unpack', 'Could not unpack the bundled Qorqbot package.');
    }
    await fsp.rename(path.join(staging, 'qorqbot'), this.root);
    await fsp.rmdir(staging);
    onLine('Preparing the Python environment (first launch only)');
    try {
      await this.run(['setup'], { onLine });
    } catch (error) {
      throw new EngineError('setup', 'Qorqbot setup did not finish.', { detail: error.detail || String(error) });
    }
    this.log('setup complete');
  }

  async update({ onLine = () => {} } = {}) {
    if (!this.updateAvailable()) {
      throw new EngineError('update', 'This app does not contain a newer Qorqbot version. The installed version was kept.');
    }
    const from = this.installedVersion();
    if (await this.status()) {
      onLine('Stopping the running server');
      await this.stop();
    }
    this.log(`updating ${from} -> ${this.bundledVersion}`);
    const { code } = await this.run(['update', this.archive, '--sha256', this.archiveSha256], { onLine, allowFailure: true });
    if (code !== 0) {
      throw new EngineError('update', 'The update was not applied. The installed version is unchanged.', { detail: this.logTail() });
    }
    this.log('update complete');
  }

  /** URL of the server running from this folder, or null. */
  async status() {
    if (!this.isInstalled()) return null;
    const { code, output } = await this.run(['status'], { allowFailure: true });
    const match = output.match(/Running at (\S+)/);
    this.url = code === 0 && match ? match[1] : null;
    this.terminalServer = Boolean(this.url) && /from Terminal/.test(output);
    if (this.url) this.savePort(Number(new URL(this.url).port));
    return this.url;
  }

  savedPort() {
    let value;
    try {
      value = readTrimmed(this.portFile);
    } catch (error) {
      if (error.code === 'ENOENT') return null;
      throw error;
    }
    const port = Number(value);
    if (!/^\d+$/.test(value) || !Number.isInteger(port) || port < 1024 || port > 65535) {
      throw new EngineError('ports', 'The saved server port is invalid. Check ' + this.portFile);
    }
    return port;
  }

  savePort(port) {
    if (!Number.isInteger(port) || port < 1024 || port > 65535) {
      throw new EngineError('ports', 'The server did not report a valid local port.');
    }
    this.ensureParent();
    fs.writeFileSync(this.portFile, String(port) + '\n', { mode: 0o600 });
  }

  async start({ onLine = () => {} } = {}) {
    await this.requirePrerequisites();
    const saved = this.savedPort();
    let port = saved ?? this.firstPort;
    while (!(await portFree(port))) {
      if (saved !== null) {
        throw new EngineError('ports', `Qorqbot's saved port ${port} is in use. Close the program using it and try again; existing agents need this same address.`);
      }
      port += 1;
      if (port > this.firstPort + PORT_RANGE) {
        throw new EngineError('ports', `Ports ${this.firstPort}-${this.firstPort + PORT_RANGE} are all in use.`);
      }
    }
    this.savePort(port);
    const url = `http://127.0.0.1:${port}`;
    this.log(`starting on ${url}`);
    onLine('Starting the local server on ' + url);
    const env = await this.environment();
    const log = fs.openSync(this.logFile, 'a');
    const child = spawn('./qorqbot', ['--port', String(port)], { cwd: this.root, env, stdio: ['ignore', log, log] });
    fs.closeSync(log);
    let exited = false;
    let startError = null;
    child.on('error', (error) => { startError = error; exited = true; });
    child.on('exit', () => { exited = true; if (this.server === child) { this.server = null; this.url = null; } });
    this.server = child;
    const deadline = Date.now() + READY_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (exited) {
        throw new EngineError('start', 'Qorqbot stopped during start-up.', { detail: startError?.message || this.logTail() });
      }
      if ((await httpStatus(url)) === 200) {
        this.log(`ready at ${url} (pid ${child.pid})`);
        this.url = url;
        this.terminalServer = false;
        return url;
      }
      await sleep(400);
    }
    throw new EngineError('timeout', 'Qorqbot did not become ready within three minutes.', { detail: this.logTail() });
  }

  async stop() {
    if (!this.isInstalled()) return;
    await this.run(['stop']);
    const child = this.server;
    if (child && child.exitCode === null && child.signalCode === null) {
      await Promise.race([new Promise((resolve) => child.once('exit', resolve)), sleep(30_000)]);
    }
    this.server = null;
    this.url = null;
    this.log('stopped');
  }
}

module.exports = { Engine, EngineError, INSTALL_COMMAND, MAX_ROOT_LENGTH };
