'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { attentionLabel, quitLabel, statusLabel, trayMenuTemplate } = require('../tray-menu');

const URL = 'http://127.0.0.1:1049';
const running = { busy: false, running: true, phase: 'running', url: URL, ownsServer: true, attention: 0 };
const stopped = { busy: false, running: false, phase: 'stopped', url: null, ownsServer: true, attention: 0 };

function actions() {
  const calls = [];
  const record = (name) => () => calls.push(name);
  return {
    calls,
    show: record('show'), openInBrowser: record('openInBrowser'), start: record('start'),
    stop: record('stop'), restart: record('restart'), quit: record('quit'),
  };
}

const labels = (items) => items.map((item) => (item.type === 'separator' ? '-' : item.label));
const find = (items, label) => items.find((item) => item.label === label);

test('the status line says working, running at the address, needs attention or stopped', () => {
  assert.equal(statusLabel({ ...running, busy: true }), 'Status: working…');
  assert.equal(statusLabel(running), `Status: running at ${URL}`);
  assert.equal(statusLabel({ ...stopped, phase: 'error' }), 'Status: needs attention');
  assert.equal(statusLabel(stopped), 'Status: stopped');
  assert.equal(statusLabel({ ...stopped, phase: 'starting' }), 'Status: stopped');
  // Working wins over everything else, as in the Server menu.
  assert.equal(statusLabel({ ...stopped, phase: 'error', busy: true }), 'Status: working…');
});

test('quitting says whether the server stops with the app', () => {
  assert.equal(quitLabel({ ownsServer: true, running: true }), 'Quit Qorqbot (stops the server)');
  assert.equal(quitLabel({ ownsServer: true, running: false }), 'Quit Qorqbot (stops the server)');
  assert.equal(quitLabel({ ownsServer: false, running: true }), 'Quit Qorqbot (server keeps running in Terminal)');
  assert.equal(quitLabel({ ownsServer: false, running: false }), 'Quit Qorqbot (stops the server)');
});

test('the menu bar menu of a running server', () => {
  const items = trayMenuTemplate(running, actions());
  assert.deepEqual(labels(items), [
    `Status: running at ${URL}`, '-', 'Show Qorqbot', 'Open in Browser', '-',
    'Stop Server', 'Restart Server', '-', 'Quit Qorqbot (stops the server)',
  ]);
  assert.equal(items[0].enabled, false, 'the status line is not clickable');
  assert.equal(find(items, 'Open in Browser').enabled, true);
  assert.equal(find(items, 'Stop Server').enabled, true);
  assert.equal(find(items, 'Restart Server').enabled, true);
  assert.equal(find(items, 'Show Qorqbot').enabled, undefined, 'Show is always available');
});

test('the menu bar menu of a stopped server offers Start and nothing that needs a server', () => {
  const items = trayMenuTemplate(stopped, actions());
  assert.deepEqual(labels(items), [
    'Status: stopped', '-', 'Show Qorqbot', 'Open in Browser', '-',
    'Start Server', 'Restart Server', '-', 'Quit Qorqbot (stops the server)',
  ]);
  assert.equal(find(items, 'Open in Browser').enabled, false);
  assert.equal(find(items, 'Start Server').enabled, true);
  assert.equal(find(items, 'Restart Server').enabled, false);
  assert.equal(find(trayMenuTemplate({ ...stopped, phase: 'error' }, actions()), 'Status: needs attention').enabled, false);
});

test('while an operation is in progress the server controls are off and Show still works', () => {
  for (const model of [{ ...running, busy: true }, { ...stopped, busy: true }]) {
    const items = trayMenuTemplate(model, actions());
    assert.equal(items[0].label, 'Status: working…');
    for (const label of ['Start Server', 'Stop Server', 'Restart Server']) {
      const item = find(items, label);
      if (item) assert.equal(item.enabled, false, label);
    }
    assert.notEqual(find(items, 'Show Qorqbot').enabled, false);
  }
});

test('a server that belongs to Terminal is not described as stopping with the app', () => {
  const items = trayMenuTemplate({ ...running, ownsServer: false }, actions());
  assert.equal(items.at(-1).label, 'Quit Qorqbot (server keeps running in Terminal)');
});

test('each entry calls its own action', () => {
  const handlers = actions();
  const items = trayMenuTemplate(running, handlers);
  for (const label of ['Show Qorqbot', 'Open in Browser', 'Stop Server', 'Restart Server', 'Quit Qorqbot (stops the server)']) find(items, label).click();
  trayMenuTemplate(stopped, handlers).find((item) => item.label === 'Start Server').click();
  assert.deepEqual(handlers.calls, ['show', 'openInBrowser', 'stop', 'restart', 'quit', 'start']);
});

test('waiting items are shown above the controls and open the window', () => {
  assert.equal(attentionLabel(1), '1 item needs you');
  assert.equal(attentionLabel(3), '3 items need you');
  const handlers = actions();
  const items = trayMenuTemplate({ ...running, attention: 3 }, handlers);
  assert.deepEqual(labels(items).slice(0, 3), [`Status: running at ${URL}`, '3 items need you', '-']);
  items[1].click();
  assert.deepEqual(handlers.calls, ['show']);
  assert.equal(labels(trayMenuTemplate(running, actions())).includes('0 items need you'), false);
});

test('the template is plain data that Electron can build a menu from', () => {
  const allowed = new Set(['label', 'enabled', 'click', 'type']);
  for (const model of [running, stopped, { ...running, busy: true, attention: 2 }]) {
    for (const item of trayMenuTemplate(model, actions())) {
      for (const key of Object.keys(item)) assert.ok(allowed.has(key), key);
      if (item.type === 'separator') assert.deepEqual(Object.keys(item), ['type']);
      else assert.equal(typeof item.label, 'string');
    }
  }
});
