'use strict';
// node --test biz-dashboard/tests/*.test.js
const assert = require('node:assert/strict');
const { test } = require('node:test');

function browser({permission = 'default', visible = true, focused = true} = {}) {
  const store = new Map();
  const shown = [];
  global.localStorage = {getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v))};
  global.document = {documentElement: {dataset: {}, style: {}}, visibilityState: visible ? 'visible' : 'hidden', hasFocus: () => focused};
  global.matchMedia = () => ({matches: false, addEventListener() {}});
  global.window = {focus() {}};
  global.Notification = function (title, options) { shown.push({title, ...options}); this.close = () => {}; };
  global.Notification.permission = permission;
  global.Notification.requestPermission = async () => (global.Notification.permission = 'granted');
  return {store, shown};
}
const fresh = path => { delete require.cache[require.resolve(path)]; return require(path); };

test('appearance defaults to dark at normal size and applies a saved choice', () => {
  browser();
  const appearance = fresh('../js/appearance.js');
  assert.deepEqual(appearance.get(), {theme: 'dark', textSize: 'default'});
  assert.equal(document.documentElement.dataset.theme, 'dark');
  appearance.set({theme: 'light', textSize: 'large'});
  assert.equal(document.documentElement.dataset.theme, 'light');
  assert.equal(document.documentElement.style.zoom, '1.1');
  assert.deepEqual(fresh('../js/appearance.js').get(), {theme: 'light', textSize: 'large'});
});

test('appearance ignores unknown saved values', () => {
  const {store} = browser();
  store.set('qorqbot:appearance', JSON.stringify({theme: 'neon', textSize: 'huge'}));
  assert.deepEqual(fresh('../js/appearance.js').get(), {theme: 'dark', textSize: 'default'});
});

test('desktop notifications are off until turned on, and then only in the background', async () => {
  const {shown} = browser({visible: true, focused: true});
  const notify = fresh('../js/desktop-notify.js');
  assert.deepEqual(notify.prefs(), {replies: false, signoffs: false});
  assert.equal(notify.send({kind: 'replies', title: 'Ordubashy'}), null);
  assert.deepEqual(await notify.enable('replies', true), {replies: true, signoffs: false});
  assert.equal(notify.send({kind: 'replies', title: 'Ordubashy'}), null, 'not while the dashboard is in front');
  document.visibilityState = 'hidden';
  assert.ok(notify.send({kind: 'replies', title: 'Ordubashy', body: 'Done'}));
  assert.equal(notify.send({kind: 'signoffs', title: 'Waiting on you'}), null);
  assert.deepEqual(shown.map(item => item.title), ['Ordubashy']);
});

test('a refused permission keeps notifications off', async () => {
  browser({permission: 'denied'});
  const notify = fresh('../js/desktop-notify.js');
  assert.deepEqual(await notify.enable('signoffs', true), {replies: false, signoffs: false});
  assert.equal(notify.send({kind: 'test', title: 'Qorqbot', force: true}), null);
});
