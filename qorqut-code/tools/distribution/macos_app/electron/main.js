'use strict';
// Qorqbot desktop app (macOS and Ubuntu): one window that shows set-up/status, then the dashboard.
//
// Lifecycle: the app being open means Qorqbot is running. Quit (Cmd+Q on the
// Mac, Ctrl+Q on Ubuntu) stops the local server first. Closing the window
// hides it on the Mac (the Dock icon reopens it); on Ubuntu there is no Dock
// to reopen it from, so closing the window quits. Every per-platform value
// comes from engine.platform (platform.js).

const { app, BrowserWindow, Menu, clipboard, dialog, ipcMain, shell } = require('electron');
const path = require('node:path');
const os = require('node:os');
const { spawn } = require('node:child_process');
const { Engine, AGENT_CLIS } = require('./engine');
const { installCommand } = require('./platform');

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
let checklistTimer = null; // re-checks programs while the set-up checklist is shown
let state = { phase: 'starting', title: 'Starting Qorqbot', steps: [], log: [], error: null, choice: null, checklist: null };

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
  clearInterval(checklistTimer);
  checklistTimer = null;
  publish({ phase: 'starting', title: 'Starting Qorqbot', steps: [], log: [], error: null, choice: null });
  await showStatusPage();
  try {
    step('Checking the programs Qorqbot uses');
    const checklist = await engine.checklist();
    publish({ checklist });
    if (!checklist.ready) {
      finishSteps('failed');
      publish({ phase: 'setup', title: 'Install the programs Qorqbot needs' });
      watchChecklist();
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

// While the checklist is shown, re-check every few seconds so items turn green as they are installed and start-up
// continues by itself once nothing required is missing.
function watchChecklist() {
  clearInterval(checklistTimer);
  checklistTimer = setInterval(async () => {
    if (state.phase !== 'setup' || busy) { clearInterval(checklistTimer); checklistTimer = null; return; }
    try {
      const checklist = await engine.checklist({ log: false });
      if (state.phase !== 'setup') return;
      publish({ checklist });
      if (checklist.ready) {
        clearInterval(checklistTimer);
        checklistTimer = null;
        boot();
      }
    } catch (error) {
      engine.log(`checklist: ${error.message}`);
    }
  }, 3000);
}

async function openDashboard(url) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    // First-run onboarding is the dashboard's own walkthrough; Getting Started stays in the Help menu.
    await mainWindow.loadURL(url);
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
      // The before-quit flow shows "Stopping Qorqbot before quitting" in this window.
      if (engine.platform.closeQuits) app.quit();
      else mainWindow.hide();
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

// Opens the dashboard's walkthrough menu, whether it was finished, skipped or never started.
async function showWalkthrough() {
  if (!mainWindow || mainWindow.isDestroyed() || isStatusPage(mainWindow.webContents)) return;
  mainWindow.show();
  const opened = await mainWindow.webContents.executeJavaScript(
    "typeof onboarding !== 'undefined' && typeof onboarding.open === 'function' ? (onboarding.open(), true) : false",
  ).catch(() => false);
  if (!opened) {
    await dialog.showMessageBox(mainWindow, { type: 'info', message: 'The walkthrough is not available on this page.', detail: 'Choose it again once the dashboard has finished loading, or open Walkthrough in Settings.' });
  }
}

async function installTerminalCommand() {
  try {
    const message = await engine.installCommand();
    await dialog.showMessageBox(mainWindow, {
      type: 'info', message: 'The qorqbot command is installed',
      detail: message + '\n\nqorqbot opens the same dashboard in your browser. Use either the app or the browser; both talk to the same server.',
    });
  } catch (error) {
    await dialog.showMessageBox(mainWindow, { type: 'warning', message: error.message, detail: error.detail || '' });
  }
}

function rebuildMenu() {
  const running = state.phase === 'running' && Boolean(engine && engine.url);
  const statusLabel = busy ? 'Status: working…'
    : running ? `Status: running at ${engine.url}`
      : state.phase === 'error' ? 'Status: needs attention' : 'Status: stopped';
  const mac = engine.platform.id === 'darwin';
  const terminalName = mac ? 'Terminal' : 'the terminal';
  const quitItem = {
    label: engine && !engine.ownsServer() && running ? `Quit Qorqbot (server keeps running in ${terminalName})` : 'Quit Qorqbot (stops the server)',
    accelerator: 'CmdOrCtrl+Q', click: () => app.quit(),
  };
  const gettingStarted = { label: 'Getting Started', click: showGuide };
  const howItWorks = { label: 'How Qorqbot Works', click: showHelp };
  const viewMenu = {
    label: 'View',
    submenu: [
      { label: 'Reload', accelerator: 'CmdOrCtrl+R', click: (_item, window) => window && window.webContents.reload() },
      { label: 'Open in Browser', accelerator: 'CmdOrCtrl+Shift+B', enabled: running, click: () => openExternal(engine.url) },
      { type: 'separator' },
      { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
      { type: 'separator' },
      { role: 'togglefullscreen' },
      { role: 'toggleDevTools', label: 'Developer Tools' },
    ],
  };
  const serverMenu = {
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
      { label: mac ? 'Reveal Install Folder in Finder' : 'Open Install Folder', click: () => shell.showItemInFolder(engine.root) },
    ],
  };
  const helpItems = [
    { label: 'Show Walkthrough', enabled: running && !busy, click: showWalkthrough },
    gettingStarted,
    howItWorks,
    { type: 'separator' },
    { label: 'Install Terminal Command…', enabled: Boolean(engine && engine.isInstalled()) && !busy, click: installTerminalCommand },
  ];
  const template = mac
    ? [
      {
        label: 'Qorqbot',
        submenu: [
          { role: 'about', label: 'About Qorqbot' },
          gettingStarted,
          howItWorks,
          { type: 'separator' },
          { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' },
          { type: 'separator' },
          quitItem,
        ],
      },
      { role: 'editMenu' },
      viewMenu,
      serverMenu,
      { role: 'windowMenu' },
      { role: 'help', submenu: helpItems },
    ]
    : [
      { label: 'File', submenu: [quitItem] },
      { role: 'editMenu' },
      viewMenu,
      serverMenu,
      { role: 'windowMenu' },
      { role: 'help', submenu: [...helpItems, { type: 'separator' }, { role: 'about', label: 'About Qorqbot' }] },
    ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// Opens a terminal for the user to paste the install command into.
async function openTerminal() {
  const { terminal } = engine.platform;
  if (terminal.app) return shell.openPath(terminal.app);
  const env = await engine.environment();
  for (const name of terminal.commands) {
    const found = engine.which(name, env);
    if (found) {
      spawn(found, [], { cwd: os.homedir(), detached: true, stdio: 'ignore' }).unref();
      return;
    }
  }
  await dialog.showMessageBox(mainWindow, { type: 'info', message: 'No terminal program was found.', detail: 'Open a terminal yourself and run the command shown.' });
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
    case 'copy-command': return clipboard.writeText(state.phase === 'setup' ? state.checklist?.command || installCommand(engine.platform)
      : state.error?.command || installCommand(engine.platform));
    case 'open-homebrew': return engine.platform.packageManager ? shell.openExternal(engine.platform.packageManager.url) : undefined;
    case 'copy-details': return clipboard.writeText([state.error?.message, state.error?.detail].filter(Boolean).join('\n\n'));
    case 'open-terminal': return openTerminal();
    case 'show-log': return shell.openPath(engine.logFile);
    case 'reveal-folder': return shell.showItemInFolder(engine.isInstalled() ? engine.root : engine.installParent);
    case 'help': return showHelp();
    case 'guide': return showGuide();
    default:
      // Only the fixed documentation links are opened; the page cannot name an arbitrary URL.
      if (typeof action === 'string' && action.startsWith('agent-docs:')) {
        const agent = AGENT_CLIS.find((item) => item.id === action.slice('agent-docs:'.length));
        if (agent) shell.openExternal(agent.url);
        return;
      }
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
      copyright: 'Copyright 49 Agents LLC. PolyForm Noncommercial License 1.0.0.',
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
    // A server started from Terminal belongs to that Terminal; quitting the app leaves it running.
    if (!engine || !engine.isInstalled() || !engine.ownsServer()) return finish();
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
