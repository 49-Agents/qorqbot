'use strict';
// Qorqbot for Mac: one window that shows set-up/status, then the dashboard.
//
// Lifecycle: the app being open means Qorqbot is running. Closing the window
// hides it (the Dock icon and the menu bar item reopen it); Quit (Cmd+Q) stops
// the local server first. The menu bar item shows the server status and can
// start, stop and restart it; the Dock badge counts what is waiting for you.

const { app, BrowserWindow, Menu, Tray, clipboard, dialog, ipcMain, nativeImage, shell } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { attentionCount, attentionUrl, badgeText } = require('./badge');
const { Engine } = require('./engine');
const { runsFromTemporaryPlace } = require('./placement');
const { quitLabel, statusLabel, trayMenuTemplate } = require('./tray-menu');

const STATUS_PAGE = path.join(__dirname, 'ui', 'status.html');
const HELP_PAGE = path.join(__dirname, 'ui', 'help.html');
const START_PAGE = path.join(__dirname, 'ui', 'start.html');
const ATTENTION_POLL_MS = 30_000;

let engine;
let mainWindow = null;
let helpWindow = null;
let guideWindow = null;
let quitting = false;
let busy = false;           // an engine operation is in progress
let pendingChoice = null;   // resolves when the status page answers a question
let tray = null;            // the menu bar item; null when its icon was not built in (a development run)
let trayKey = '';           // what the menu bar item shows now, so it is rebuilt only when that changes
let attention = 0;          // new items waiting for the owner, from the dashboard's attention queue
let attentionTimer = null;
let attentionReading = false;
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
  const firstLaunch = !engine.isInstalled();
  publish({ phase: 'starting', title: firstLaunch ? 'Setting up Qorqbot' : 'Starting Qorqbot', steps: [], log: [], error: null, choice: null });
  await showStatusPage();
  try {
    if (!engine.isInstalled()) {
      step('Setting up Qorqbot (first launch, a few seconds)');
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
    readAttention();
    await openDashboard(url);
  } catch (error) {
    finishSteps('failed');
    publish({
      phase: 'error', title: 'Qorqbot could not start',
      error: { message: error.message, detail: error.detail || '' },
    });
  } finally {
    busy = false;
    rebuildMenu();
  }
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
    publish({ phase: 'error', title: 'Qorqbot could not be stopped', error: { message: error.message, detail: error.detail || '' } });
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
      publish({ phase: 'error', title: 'The dashboard did not load', error: { message: `${description} (${url})`, detail: engine.logTail() } });
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

/** What the Server menu and the menu bar item both show. */
function menuModel() {
  const running = state.phase === 'running' && Boolean(engine && engine.url);
  return {
    busy, running, phase: state.phase, url: engine ? engine.url : null,
    ownsServer: !engine || engine.ownsServer(), attention,
  };
}

function rebuildMenu() {
  const model = menuModel();
  const { running } = model;
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
        { label: quitLabel(model), accelerator: 'Cmd+Q', click: () => app.quit() },
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
        { label: statusLabel(model), enabled: false },
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
      submenu: [
        { label: 'Show Walkthrough', enabled: running && !busy, click: showWalkthrough },
        { label: 'Getting Started', click: showGuide },
        { label: 'How Qorqbot Works', click: showHelp },
        { type: 'separator' },
        { label: 'Install Terminal Command…', enabled: Boolean(engine && engine.isInstalled()) && !busy, click: installTerminalCommand },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
  syncMenuBar();
}

// ---------------------------------------------------------------- menu bar item and Dock badge

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
  if (process.platform === 'darwin') app.focus({ steal: true });
}

const trayActions = {
  show: showMainWindow,
  openInBrowser: () => { if (engine && engine.url) openExternal(engine.url); },
  start: boot,
  stop: stopServer,
  restart: restartServer,
  quit: () => app.quit(),
};

/** The menu bar picture: a monochrome template image that macOS tints for light, dark and the selected state.
 * build_dmg.py renders it from the brand mark into ui/; a development run has none and the item is left out. */
function trayImage() {
  const read = (name) => {
    try { return fs.readFileSync(path.join(__dirname, 'ui', name)); } catch { return null; }
  };
  const normal = read('trayTemplate.png');
  if (!normal) return null;
  const image = nativeImage.createEmpty();
  image.addRepresentation({ scaleFactor: 1, buffer: normal });
  const retina = read('trayTemplate@2x.png');
  if (retina) image.addRepresentation({ scaleFactor: 2, buffer: retina });
  if (image.isEmpty()) return null;
  image.setTemplateImage(true);
  return image;
}

function createTray() {
  if (tray) return;
  try {
    const image = trayImage();
    if (!image) return;
    tray = new Tray(image);
    syncMenuBar();
  } catch (error) {
    tray = null;
    engine.log('The menu bar item is not available: ' + error.message);
  }
}

function destroyTray() {
  if (attentionTimer) clearInterval(attentionTimer);
  attentionTimer = null;
  if (tray && !tray.isDestroyed()) tray.destroy();
  tray = null;
}

/** Keeps the Dock badge and the menu bar item in step with rebuildMenu(); only a change is applied. */
function syncMenuBar() {
  const model = menuModel();
  if (!model.running && attention) {
    attention = 0;
    model.attention = 0;
    if (app.dock) app.dock.setBadge('');
  }
  if (!tray || tray.isDestroyed()) return;
  const key = JSON.stringify(model);
  if (key === trayKey) return;
  trayKey = key;
  tray.setToolTip('Qorqbot: ' + statusLabel(model).replace(/^Status: /, ''));
  tray.setContextMenu(Menu.buildFromTemplate(trayMenuTemplate(model, trayActions)));
}

/** Reads the same attention queue the dashboard reads and shows its count on the Dock icon. A read that fails leaves
 * the badge as it was unless the server is gone; the server stopping clears it (syncMenuBar). Never throws. */
async function readAttention() {
  const url = state.phase === 'running' && engine && engine.url ? attentionUrl(serverOrigin()) : null;
  if (!url || attentionReading) return;
  attentionReading = true;
  try {
    const response = await fetch(url, { headers: { accept: 'application/json' }, signal: AbortSignal.timeout(5000) });
    const count = response.ok ? attentionCount(await response.json()) : null;
    if (count !== null && count !== attention && state.phase === 'running') {
      attention = count;
      if (app.dock) app.dock.setBadge(badgeText(count));
      syncMenuBar();
    }
  } catch {
    // The server is busy or gone. One stopped from Terminal (`qorqbot stop`) never tells the app, so ask the folder.
    await noticeStoppedServer();
  } finally {
    attentionReading = false;
  }
}

/** Shows the stopped page, and so clears the badge and the status lines, when the server ended outside the app. */
async function noticeStoppedServer() {
  if (busy || quitting || state.phase !== 'running') return;
  const url = await engine.status().catch(() => engine.url);
  if (url || busy || quitting || state.phase !== 'running') return;
  engine.log('the server stopped outside the app');
  publish({ phase: 'stopped', title: 'Qorqbot is stopped', steps: [], log: [], error: null, choice: null });
  await showStatusPage();
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
    case 'copy-details': return clipboard.writeText([state.error?.message, state.error?.detail].filter(Boolean).join('\n\n'));
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

// ---------------------------------------------------------------- Applications folder

/** Opened from the disk image, Downloads or the Desktop, offer to move the app into Applications. Resolves true
 * when the app moved and is reopening from there. QORQBOT_MOVE=accept|decline answers for the release check. */
async function offerMoveToApplications() {
  if (process.platform !== 'darwin' || !app.isPackaged || app.isInApplicationsFolder()) return false;
  const bundle = path.resolve(process.execPath, '..', '..', '..');
  if (!runsFromTemporaryPlace(bundle, app.getPath('home'))) return false;
  const preset = process.env.QORQBOT_MOVE;
  let accepted = preset === 'accept';
  if (preset !== 'accept' && preset !== 'decline') {
    const { response } = await dialog.showMessageBox({
      type: 'question', buttons: ['Move to Applications', 'Not Now'], defaultId: 0, cancelId: 1,
      message: 'Move Qorqbot to your Applications folder?',
      detail: 'Qorqbot was opened from the disk image or a downloads folder. In Applications it stays where you can find it, and the disk image can be ejected.',
    });
    accepted = response === 0;
  }
  if (!accepted) return false;
  try {
    return app.moveToApplicationsFolder({
      // A copy that is running is left alone; an older copy is replaced only when the person says so.
      conflictHandler: (conflict) => conflict === 'exists' && (preset === 'accept' || dialog.showMessageBoxSync({
        type: 'question', buttons: ['Replace', 'Cancel'], defaultId: 0, cancelId: 1,
        message: 'Replace the Qorqbot that is already in Applications?',
      }) === 0),
    });
  } catch (error) {
    dialog.showMessageBoxSync({
      type: 'info', message: 'Qorqbot could not move itself',
      detail: 'Drag Qorqbot into the Applications folder and open it from there. ' + error.message,
    });
    return false;
  }
}

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

  app.whenReady().then(async () => {
    if (await offerMoveToApplications()) return;
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
    createTray();
    attentionTimer = setInterval(readAttention, ATTENTION_POLL_MS);
    boot();
  });

  app.on('activate', () => {
    if (mainWindow) mainWindow.show();
  });

  app.on('before-quit', (event) => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    const finish = () => { destroyTray(); app.exit(0); };
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
