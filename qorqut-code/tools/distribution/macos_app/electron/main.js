'use strict';
// Qorqbot for Mac: one window that shows set-up/status, then the dashboard.
//
// Lifecycle: the app being open means Qorqbot is running. Closing the window
// hides it (Dock icon reopens it); Quit (Cmd+Q) stops the local server first.

const { app, BrowserWindow, Menu, clipboard, dialog, ipcMain, shell } = require('electron');
const path = require('node:path');
const { Engine, INSTALL_COMMAND } = require('./engine');

const STATUS_PAGE = path.join(__dirname, 'ui', 'status.html');
const HELP_PAGE = path.join(__dirname, 'ui', 'help.html');
const START_PAGE = path.join(__dirname, 'ui', 'start.html');

let engine;
let mainWindow = null;
let helpWindow = null;
let guideWindow = null;
let quitting = false;
let busy = false;           // an engine operation is in progress
let pendingChoice = null;   // resolves when the status page answers a question
let state = { phase: 'starting', title: 'Starting Qorqbot', steps: [], log: [], error: null, choice: null };

// ---------------------------------------------------------------- status page state

function publish(patch) {
  state = { ...state, ...patch };
  if (mainWindow && !mainWindow.isDestroyed() && isStatusPage(mainWindow.webContents)) {
    mainWindow.webContents.send('qorqbot:state', state);
  }
  rebuildMenu();
}

function step(label, status = 'active') {
  const steps = state.steps.map((item) => (item.status === 'active' ? { ...item, status: 'done' } : item));
  steps.push({ label, status });
  publish({ steps });
}

function finishSteps(status = 'done') {
  publish({ steps: state.steps.map((item) => (item.status === 'active' ? { ...item, status } : item)) });
}

function logLine(line) {
  publish({ log: [...state.log, line].slice(-200) });
}

function isStatusPage(contents) {
  return contents.getURL().startsWith('file:');
}

function showStatusPage() {
  if (!mainWindow) return Promise.resolve();
  if (isStatusPage(mainWindow.webContents)) {
    mainWindow.webContents.send('qorqbot:state', state);
    return Promise.resolve();
  }
  return mainWindow.loadFile(STATUS_PAGE);
}

function ask(choice) {
  publish({ choice });
  return new Promise((resolve) => { pendingChoice = resolve; });
}

// ---------------------------------------------------------------- engine flows

function serverOrigin() {
  return engine.url ? new URL(engine.url).origin : null;
}

async function boot() {
  if (busy) return;
  busy = true;
  publish({ phase: 'starting', title: 'Starting Qorqbot', steps: [], log: [], error: null, choice: null });
  await showStatusPage();
  try {
    step('Checking Python 3.12, git and tmux');
    const { missing } = await engine.prerequisites();
    if (missing.length) {
      finishSteps('failed');
      publish({
        phase: 'error', title: 'Install the programs Qorqbot needs',
        error: { message: `Qorqbot needs ${missing.join(', ')} on this Mac. Install them with Homebrew in Terminal, then choose Try Again.`,
          command: INSTALL_COMMAND, detail: '' },
      });
      return;
    }
    if (!engine.isInstalled()) {
      step('Setting up Qorqbot in ' + engine.root + ' (first launch, about a minute)');
      await engine.install({ onLine: logLine });
    } else if (engine.updateAvailable()) {
      step('Update available');
      const answer = await ask({
        message: `This app contains Qorqbot ${engine.bundledVersion}. The installed version is ${engine.installedVersion()}. Your records stay in place. Close agent terminals before updating.`,
        buttons: [{ id: 'later', label: 'Later' }, { id: 'update', label: 'Update Now', primary: true }],
      });
      publish({ choice: null });
      if (answer === 'update') {
        step('Updating to ' + engine.bundledVersion);
        await engine.update({ onLine: logLine });
      }
    }
    step('Starting the local server');
    const url = (await engine.status()) || (await engine.start({ onLine: logLine }));
    finishSteps();
    publish({ phase: 'running', title: 'Qorqbot is running', error: null });
    await openDashboard(url);
  } catch (error) {
    finishSteps('failed');
    publish({
      phase: 'error', title: 'Qorqbot could not start',
      error: { message: error.message, command: error.command || '', detail: error.detail || '' },
    });
  } finally {
    busy = false;
    rebuildMenu();
  }
}

async function openDashboard(url) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    await mainWindow.loadURL(url);
    if (engine.claimFirstRunGuide()) showGuide();
  }
}

async function stopServer() {
  if (busy) return;
  busy = true;
  publish({ phase: 'stopping', title: 'Stopping Qorqbot', steps: [], log: [], error: null, choice: null });
  await showStatusPage();
  step('Stopping the local server');
  try {
    await engine.stop();
    finishSteps();
    publish({ phase: 'stopped', title: 'Qorqbot is stopped' });
  } catch (error) {
    finishSteps('failed');
    publish({ phase: 'error', title: 'Qorqbot could not be stopped', error: { message: error.message, detail: error.detail || '', command: '' } });
  } finally {
    busy = false;
    rebuildMenu();
  }
}

async function restartServer() {
  await stopServer();
  if (state.phase === 'stopped') await boot();
}

// ---------------------------------------------------------------- windows

function webPreferences() {
  return { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true };
}

function guardNavigation(contents) {
  contents.on('will-navigate', (event, target) => {
    const url = new URL(target);
    if (url.protocol === 'file:' || url.origin === serverOrigin()) return;
    event.preventDefault();
    openExternal(target);
  });
  contents.setWindowOpenHandler(({ url }) => {
    const target = new URL(url);
    if (target.origin === serverOrigin()) {
      return { action: 'allow', overrideBrowserWindowOptions: { width: 1200, height: 800, webPreferences: webPreferences() } };
    }
    openExternal(url);
    return { action: 'deny' };
  });
}

function openExternal(url) {
  const { protocol } = new URL(url);
  if (['http:', 'https:', 'mailto:'].includes(protocol)) shell.openExternal(url);
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1360, height: 880, minWidth: 900, minHeight: 600, title: 'Qorqbot',
    backgroundColor: '#f4f1ea', show: false, webPreferences: webPreferences(),
  });
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('close', (event) => {
    if (!quitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
  mainWindow.on('closed', () => { mainWindow = null; });
  mainWindow.webContents.on('did-fail-load', (_event, code, description, url) => {
    if (url && !url.startsWith('file:') && code !== -3) {
      publish({ phase: 'error', title: 'The dashboard did not load', error: { message: `${description} (${url})`, detail: engine.logTail(), command: '' } });
      showStatusPage();
    }
  });
}

function showHelp() {
  if (helpWindow && !helpWindow.isDestroyed()) {
    helpWindow.show();
    return;
  }
  helpWindow = new BrowserWindow({ width: 720, height: 760, title: 'How Qorqbot Works', webPreferences: webPreferences() });
  helpWindow.loadFile(HELP_PAGE);
  helpWindow.on('closed', () => { helpWindow = null; });
}

function showGuide() {
  if (guideWindow && !guideWindow.isDestroyed()) {
    guideWindow.show();
    return;
  }
  guideWindow = new BrowserWindow({ width: 720, height: 820, title: 'Getting Started', webPreferences: webPreferences() });
  guideWindow.loadFile(START_PAGE);
  guideWindow.on('closed', () => { guideWindow = null; });
}

// ---------------------------------------------------------------- menus

function rebuildMenu() {
  const running = state.phase === 'running' && Boolean(engine && engine.url);
  const statusLabel = busy ? 'Status: working…'
    : running ? `Status: running at ${engine.url}`
      : state.phase === 'error' ? 'Status: needs attention' : 'Status: stopped';
  const template = [
    {
      label: 'Qorqbot',
      submenu: [
        { role: 'about', label: 'About Qorqbot' },
        { label: 'Getting Started', click: showGuide },
        { label: 'How Qorqbot Works', click: showHelp },
        { type: 'separator' },
        { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' },
        { type: 'separator' },
        { label: 'Quit Qorqbot (stops the server)', accelerator: 'Cmd+Q', click: () => app.quit() },
      ],
    },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { label: 'Reload', accelerator: 'Cmd+R', click: (_item, window) => window && window.webContents.reload() },
        { label: 'Open in Browser', accelerator: 'Cmd+Shift+B', enabled: running, click: () => openExternal(engine.url) },
        { type: 'separator' },
        { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
        { role: 'toggleDevTools', label: 'Developer Tools' },
      ],
    },
    {
      label: 'Server',
      submenu: [
        { label: statusLabel, enabled: false },
        { type: 'separator' },
        running
          ? { label: 'Stop Server', enabled: !busy, click: stopServer }
          : { label: 'Start Server', enabled: !busy, click: boot },
        { label: 'Restart Server', enabled: running && !busy, click: restartServer },
        { type: 'separator' },
        { label: 'Show Launcher Log', click: () => shell.openPath(engine.logFile) },
        { label: 'Show Server Log', click: () => shell.openPath(engine.appLog) },
        { label: 'Reveal Install Folder in Finder', click: () => shell.showItemInFolder(engine.root) },
      ],
    },
    { role: 'windowMenu' },
    {
      role: 'help',
      submenu: [{ label: 'Getting Started', click: showGuide }, { label: 'How Qorqbot Works', click: showHelp }],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---------------------------------------------------------------- IPC (status and help pages only)

function fromLocalPage(event) {
  return event.senderFrame && event.senderFrame.url.startsWith('file:');
}

ipcMain.handle('qorqbot:get-state', (event) => (fromLocalPage(event) ? state : null));
ipcMain.handle('qorqbot:action', async (event, action) => {
  if (!fromLocalPage(event)) return;
  switch (action) {
    case 'retry': case 'start': return boot();
    case 'copy-command': return clipboard.writeText(state.error?.command || INSTALL_COMMAND);
    case 'copy-details': return clipboard.writeText([state.error?.message, state.error?.detail].filter(Boolean).join('\n\n'));
    case 'open-terminal': return shell.openPath('/System/Applications/Utilities/Terminal.app');
    case 'show-log': return shell.openPath(engine.logFile);
    case 'reveal-folder': return shell.showItemInFolder(engine.isInstalled() ? engine.root : engine.installParent);
    case 'help': return showHelp();
    case 'guide': return showGuide();
    default:
      if (typeof action === 'string' && action.startsWith('choice:') && pendingChoice) {
        const resolve = pendingChoice;
        pendingChoice = null;
        resolve(action.slice('choice:'.length));
      }
  }
});
ipcMain.handle('qorqbot:paths', (event) => (fromLocalPage(event)
  ? { root: engine.root, log: engine.logFile, appLog: engine.appLog, version: engine.bundledVersion } : null));

// ---------------------------------------------------------------- app lifecycle

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    const payload = process.env.QORQBOT_PAYLOAD || path.join(process.resourcesPath, 'qorqbot-payload');
    engine = new Engine({
      payload,
      home: process.env.QORQBOT_HOME || undefined,
      firstPort: Number(process.env.QORQBOT_PORT || 1049),
    });
    app.setAboutPanelOptions({
      applicationName: 'Qorqbot', applicationVersion: engine.bundledVersion,
      copyright: '49 Agents LLC. Business Source License 1.1.',
    });
    app.on('web-contents-created', (_event, contents) => guardNavigation(contents));
    createMainWindow();
    rebuildMenu();
    boot();
  });

  app.on('activate', () => {
    if (mainWindow) mainWindow.show();
  });

  app.on('before-quit', (event) => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    const finish = () => app.exit(0);
    if (!engine || !engine.isInstalled()) return finish();
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.show();
      publish({ phase: 'stopping', title: 'Stopping Qorqbot before quitting', steps: [{ label: 'Stopping the local server', status: 'active' }], log: [], error: null, choice: null });
      showStatusPage();
    }
    const timeout = setTimeout(() => {
      dialog.showMessageBoxSync({ type: 'warning', message: 'Qorqbot is still shutting down.', detail: 'The app will quit now. Check the server log if Qorqbot does not stop.' });
      finish();
    }, 45_000);
    engine.stop().catch(() => {}).finally(() => { clearTimeout(timeout); finish(); });
  });
}
