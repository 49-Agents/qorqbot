'use strict';
// Bridge for the app's own local pages (status and help). The dashboard is
// served over http and gets nothing from here; main.js also rejects IPC from
// any frame that is not a local file.
const { contextBridge, ipcRenderer } = require('electron');

if (location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('qorqbot', {
    platform: process.platform,
    getState: () => ipcRenderer.invoke('qorqbot:get-state'),
    paths: () => ipcRenderer.invoke('qorqbot:paths'),
    action: (name) => ipcRenderer.invoke('qorqbot:action', String(name)),
    onState: (callback) => ipcRenderer.on('qorqbot:state', (_event, state) => callback(state)),
  });
}
