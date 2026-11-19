'use strict';
// Qorqbot for Linux: one window that shows set-up/status, then the dashboard.
//
// Lifecycle: the app being open means Qorqbot is running. Closing the window
// hides it to the system tray when the desktop shows one; without a tray there
// is no Dock to reopen a hidden window from, so closing quits. Quit (Ctrl+Q or
// the tray menu) stops the local server first.
//
// The window is only a frame around the existing dashboard: the dashboard is
// served by the local server and loaded unchanged.

const { app, BrowserWindow, Menu, Tray, clipboard, dialog, ipcMain, nativeImage, shell } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const desktop = require('./desktop');
const { Engine } = require('./engine');

// WM class / Wayland app id must match the .desktop file (StartupWMClass=qorqbot, qorqbot.desktop): Electron derives
// both from desktopName in package.json, not from the name set here.
app.setName('Qorqbot');
if (typeof app.setDesktopName === 'function') app.setDesktopName(desktop.DESKTOP_FILE_NAME);

const STATUS_PAGE = path.join(__dirname, 'ui', 'status.html');
const HELP_PAGE = path.join(__dirname, 'ui', 'help.html');
const START_PAGE = path.join(__dirname, 'ui', 'start.html');
const ICON_FILE = path.join(__dirname, 'icons', 'qorqbot.png');

let engine;
let mainWindow = null;
let helpWindow = null;
let guideWindow = null;
let tray = null;
let appIcon = null;
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

/** The app icon, read as bytes so it also works from inside app.asar. Undefined until icons/qorqbot.png exists. */
function loadIcon() {
  try {
    const image = nativeImage.createFromBuffer(fs.readFileSync(ICON_FILE));
    return image.isEmpty() ? undefined : image;
  } catch {
    return undefined;
  }
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
      return { action: 'allow', overrideBrowserWindowOptions: { width: 1200, height: 800, icon: appIcon, webPreferences: webPreferences() } };
    }
    openExternal(url);
    return { action: 'deny' };
  });
}

function openExternal(url) {
  const { protocol } = new URL(url);
  if (['http:', 'https:', 'mailto:'].includes(protocol)) shell.openExternal(url);
}

/** Open a file or folder with the desktop's default program; fall back to showing it in the file manager. */
async function openPathOrReveal(target) {
  const problem = await shell.openPath(target);
  if (problem) shell.showItemInFolder(target);
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1360, height: 880, minWidth: 900, minHeight: 600, title: 'Qorqbot', icon: appIcon,
    backgroundColor: '#f4f1ea', show: false, webPreferences: webPreferences(),
  });
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('close', (event) => {
    if (quitting) return;
    event.preventDefault();
    closeMainWindow();
  });
  mainWindow.on('closed', () => { mainWindow = null; });
  mainWindow.webContents.on('did-fail-load', (_event, code, description, url) => {
    if (url && !url.startsWith('file:') && code !== -3) {
      publish({ phase: 'error', title: 'The dashboard did not load', error: { message: `${description} (${url})`, detail: engine.logTail() } });
      showStatusPage();
    }
  });
}

/** Closing the window hides it when a panel is showing the tray icon to bring it back from; otherwise it quits. */
async function closeMainWindow() {
  let hide = false;
  if (tray) {
    // The panel may have started after the app, or may not show tray icons at all (stock GNOME). Ask now.
    hide = process.env.QORQBOT_TRAY === 'on' || (await desktop.trayHostPresent()) !== false;
  }
  if (hide && mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.hide();
  } else {
    app.quit();
  }
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function showHelp() {
  if (helpWindow && !helpWindow.isDestroyed()) {
    helpWindow.show();
    return;
  }
  helpWindow = new BrowserWindow({ width: 720, height: 760, title: 'How Qorqbot Works', icon: appIcon, webPreferences: webPreferences() });
  helpWindow.loadFile(HELP_PAGE);
  helpWindow.on('closed', () => { helpWindow = null; });
}

function showGuide() {
  if (guideWindow && !guideWindow.isDestroyed()) {
    guideWindow.show();
    return;
  }
  guideWindow = new BrowserWindow({ width: 720, height: 820, title: 'Getting Started', icon: appIcon, webPreferences: webPreferences() });
  guideWindow.loadFile(START_PAGE);
  guideWindow.on('closed', () => { guideWindow = null; });
}

/** A message box over the main window when it is showing, else on its own. */
function messageBox(options) {
  const parent = mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible() ? mainWindow : undefined;
  const settings = { icon: appIcon, ...options };
  return parent ? dialog.showMessageBox(parent, settings) : dialog.showMessageBox(settings);
}

// ---------------------------------------------------------------- system tray

function trayMenuTemplate() {
  const running = state.phase === 'running' && Boolean(engine && engine.url);
  return [
    { label: 'Show Qorqbot', click: showMainWindow },
    { type: 'separator' },
    { label: statusLabel(running), enabled: false },
    running
      ? { label: 'Stop Server', enabled: !busy, click: () => { showMainWindow(); stopServer(); } }
      : { label: 'Start Server', enabled: !busy, click: () => { showMainWindow(); boot(); } },
    { label: 'Restart Server', enabled: running && !busy, click: () => { showMainWindow(); restartServer(); } },
    { type: 'separator' },
    { label: quitLabel(running), click: () => app.quit() },
  ];
}

/** Create the tray icon. False when it cannot be created (no icon file, disabled, or the desktop refuses). */
function createTray() {
  if (process.env.QORQBOT_TRAY === 'off' || !appIcon) return false;
  try {
    tray = new Tray(appIcon.resize({ width: 32, height: 32 }));
    tray.setToolTip('Qorqbot');
    tray.on('click', showMainWindow);
    return true;
  } catch (error) {
    tray = null;
    engine.log('system tray not available: ' + error.message);
    return false;
  }
}

// ---------------------------------------------------------------- menus

// Opens the dashboard's walkthrough menu, whether it was finished, skipped or never started.
async function showWalkthrough() {
  if (!mainWindow || mainWindow.isDestroyed() || isStatusPage(mainWindow.webContents)) return;
  showMainWindow();
  const opened = await mainWindow.webContents.executeJavaScript(
    "typeof onboarding !== 'undefined' && typeof onboarding.open === 'function' ? (onboarding.open(), true) : false",
  ).catch(() => false);
  if (!opened) {
    await messageBox({ type: 'info', message: 'The walkthrough is not available on this page.', detail: 'Choose it again once the dashboard has finished loading, or open Walkthrough in Settings.' });
  }
}

async function installTerminalCommand() {
  try {
    const message = await engine.installCommand();
    await messageBox({
      type: 'info', message: 'The qorqbot command is installed',
      detail: message + '\n\nqorqbot opens the same dashboard in your browser. Use either the app or the browser; both talk to the same server.',
    });
  } catch (error) {
    await messageBox({ type: 'warning', message: error.message, detail: error.detail || '' });
  }
}

function sandboxNote() {
  return desktop.sandboxStatus({
    appImage: Boolean(process.env.APPIMAGE), argv: process.argv, env: process.env, facts: desktop.userNamespaceFacts(),
  }).note;
}

function showAbout() {
  return messageBox({
    type: 'info', title: 'About Qorqbot', message: 'Qorqbot',
    detail: [
      `Version ${engine.bundledVersion}`,
      'Copyright 49 Agents LLC. PolyForm Noncommercial License 1.0.0.',
      '',
      `Install folder: ${engine.root}`,
      sandboxNote(),
      `Electron ${process.versions.electron}, Chromium ${process.versions.chrome}`,
    ].join('\n'),
  });
}

// ---------------------------------------------------------------- applications menu (AppImage)

function iconSource() {
  return fs.existsSync(ICON_FILE) ? ICON_FILE : null;
}

function menuEntryExists() {
  return desktop.menuEntryState({ appImage: process.env.APPIMAGE }).installed;
}

async function addToApplicationsMenu({ quiet = false } = {}) {
  try {
    const { desktopFile } = desktop.addToMenu({ appImage: process.env.APPIMAGE, iconSource: iconSource() });
    desktop.saveChoice(engine.installParent, 'accepted');
    engine.log(`applications menu entry written: ${desktopFile}`);
    if (!quiet) await messageBox({ type: 'info', message: 'Qorqbot was added to your applications menu', detail: desktopFile });
  } catch (error) {
    engine.log('applications menu entry failed: ' + error.message);
    if (!quiet) await messageBox({ type: 'warning', message: 'Qorqbot could not be added to your applications menu', detail: error.message });
  }
  rebuildMenu();
}

async function removeFromApplicationsMenu() {
  try {
    const removed = desktop.removeFromMenu({});
    desktop.saveChoice(engine.installParent, 'declined');
    engine.log('applications menu entry removed');
    await messageBox({
      type: 'info', message: removed ? 'Qorqbot was removed from your applications menu' : 'Nothing was removed',
      detail: removed ? '' : 'The applications menu entry was not created by this app, so it was left alone.',
    });
  } catch (error) {
    await messageBox({ type: 'warning', message: 'Qorqbot could not be removed from your applications menu', detail: error.message });
  }
  rebuildMenu();
}

/** First launch of an AppImage: offer once to add it to the applications menu. A moved AppImage updates the entry
 * this app wrote earlier. QORQBOT_DESKTOP_INTEGRATION=accept|decline answers for automated checks. */
async function offerApplicationsMenu() {
  const plan = desktop.startupPlan({ installParent: engine.installParent });
  if (plan === 'none') return;
  if (plan === 'refresh' || plan === 'accept') return addToApplicationsMenu({ quiet: true });
  if (plan === 'decline') return desktop.saveChoice(engine.installParent, 'declined');
  const { response } = await dialog.showMessageBox({
    type: 'question', icon: appIcon, buttons: ['Add to Applications Menu', 'Not Now'], defaultId: 0, cancelId: 1,
    message: 'Add Qorqbot to your applications menu?',
    detail: 'Qorqbot was opened from an AppImage file. Adding it puts Qorqbot in your applications menu so you can open it from there. You can change this later in the File menu.',
  });
  if (response === 0) return addToApplicationsMenu();
  desktop.saveChoice(engine.installParent, 'declined');
}

// ---------------------------------------------------------------- menu bar

function statusLabel(running) {
  if (busy) return 'Status: working…';
  if (running) return `Status: running at ${engine.url}`;
  return state.phase === 'error' ? 'Status: needs attention' : 'Status: stopped';
}

function quitLabel(running) {
  return engine && !engine.ownsServer() && running
    ? 'Quit Qorqbot (server keeps running in the terminal)' : 'Quit Qorqbot (stops the server)';
}

function rebuildMenu() {
  const running = state.phase === 'running' && Boolean(engine && engine.url);
  const appImage = Boolean(process.env.APPIMAGE);
  const fileMenu = [
    ...(appImage ? [
      menuEntryExists()
        ? { label: 'Remove from Applications Menu', click: removeFromApplicationsMenu }
        : { label: 'Add to Applications Menu…', click: () => addToApplicationsMenu() },
      { type: 'separator' },
    ] : []),
    { label: quitLabel(running), accelerator: 'Ctrl+Q', click: () => app.quit() },
  ];
  const template = [
    { label: 'File', submenu: fileMenu },
    // Edit shortcuts are shown but not registered: Chromium handles them in text fields itself, and a registered
    // Ctrl+C or Ctrl+A would reach the menu before the dashboard's agent terminals.
    {
      label: 'Edit',
      submenu: [
        { role: 'undo', registerAccelerator: false }, { role: 'redo', registerAccelerator: false },
        { type: 'separator' },
        { role: 'cut', registerAccelerator: false }, { role: 'copy', registerAccelerator: false },
        { role: 'paste', registerAccelerator: false }, { role: 'selectAll', registerAccelerator: false },
      ],
    },
    {
      label: 'View',
      submenu: [
        { label: 'Reload', accelerator: 'F5', click: (_item, window) => window && window.webContents.reload() },
        { label: 'Open in Browser', accelerator: 'Ctrl+Shift+B', enabled: running, click: () => openExternal(engine.url) },
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
        { label: statusLabel(running), enabled: false },
        { type: 'separator' },
        running
          ? { label: 'Stop Server', enabled: !busy, click: stopServer }
          : { label: 'Start Server', enabled: !busy, click: boot },
        { label: 'Restart Server', enabled: running && !busy, click: restartServer },
        { type: 'separator' },
        { label: 'Show Launcher Log', click: () => openPathOrReveal(engine.logFile) },
        { label: 'Show Server Log', click: () => openPathOrReveal(engine.appLog) },
        { label: 'Show Install Folder', click: () => openPathOrReveal(engine.isInstalled() ? engine.root : engine.installParent) },
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
        { type: 'separator' },
        { label: 'About Qorqbot', click: showAbout },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
  if (tray) tray.setContextMenu(Menu.buildFromTemplate(trayMenuTemplate()));
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
    case 'show-log': return openPathOrReveal(engine.logFile);
    case 'reveal-folder': return openPathOrReveal(engine.isInstalled() ? engine.root : engine.installParent);
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
  app.on('second-instance', showMainWindow);

  app.whenReady().then(async () => {
    const payload = process.env.QORQBOT_PAYLOAD || path.join(process.resourcesPath, 'qorqbot-payload');
    try {
      engine = new Engine({
        payload,
        home: process.env.QORQBOT_HOME || undefined,
        firstPort: Number(process.env.QORQBOT_PORT || 1049),
      });
    } catch (error) {
      // The app is missing its bundled package: nothing can be shown or started, so say so and stop.
      dialog.showErrorBox('Qorqbot cannot start', `This copy of Qorqbot is incomplete (${error.message}). Download Qorqbot again.`);
      app.exit(1);
      return;
    }
    appIcon = loadIcon();
    engine.log(`Qorqbot app ${engine.bundledVersion}${process.env.APPIMAGE ? ' (AppImage)' : ''}; ${sandboxNote()}`);
    await offerApplicationsMenu();
    app.on('web-contents-created', (_event, contents) => guardNavigation(contents));
    createMainWindow();
    createTray();
    rebuildMenu();
    boot();
  });

  // The main window is only ever hidden, never closed, until the app quits. Stay alive while the server stops.
  app.on('window-all-closed', () => {});

  app.on('before-quit', (event) => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    const finish = () => app.exit(0);
    if (tray) { tray.destroy(); tray = null; }
    // A server started from a terminal belongs to that terminal; quitting the app leaves it running.
    if (!engine || !engine.isInstalled() || !engine.ownsServer()) return finish();
    if (mainWindow && !mainWindow.isDestroyed()) {
      showMainWindow();
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
