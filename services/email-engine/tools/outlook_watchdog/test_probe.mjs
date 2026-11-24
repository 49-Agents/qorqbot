import test from 'node:test';
import assert from 'node:assert/strict';
import {inspect} from './probe.mjs';

const address = 'sender@example.test';
const browser = (...pages) => ({contexts: () => [{pages: () => pages}]});
const page = ({url = 'https://outlook.cloud.microsoft/mail/', named = true, login = false,
               signedIn = true, evaluate} = {}) => ({
  url: () => url,
  evaluate: evaluate || (async () => ({named, login})),
  getByRole: (role, options) => {
    assert.equal(role, 'treeitem'); assert.deepEqual(options, {name: address, exact: true});
    return {count: async () => signedIn ? 1 : 0};
  },
});

test('expected mailbox responds', async () => {
  assert.equal(await inspect(browser(page()), address), 'healthy');
});
test('crashed renderer', async () => {
  assert.equal(await inspect(browser(page({evaluate: async () => {throw new Error('crashed');}})), address),
               'connection_failed');
});
test('unresponsive renderer has bounded timeout', async () => {
  const start = Date.now();
  assert.equal(await inspect(browser(page({evaluate: () => new Promise(() => {})})), address), 'connection_failed');
  assert.ok(Date.now() - start < 3000);
});
test('login page is distinguished from crash', async () => {
  assert.equal(await inspect(browser(page({url: 'https://login.microsoftonline.com/'})), address), 'login_required');
});
test('wrong or unloaded mailbox is not healthy', async () => {
  assert.equal(await inspect(browser(page({signedIn: false})), address), 'mailbox_not_ready');
});
test('named sender tab takes precedence over unrelated signed-in tab', async () => {
  assert.equal(await inspect(browser(page({signedIn: false}), page({named: false})), address), 'mailbox_not_ready');
});

test('unrelated crashed tab cannot override the healthy named sender in either order', async () => {
  const broken = page({evaluate: async () => {throw new Error('unrelated crash');}});
  assert.equal(await inspect(browser(broken, page()), address), 'healthy');
  assert.equal(await inspect(browser(page(), broken), address), 'healthy');
});
test('unresponsive unrelated tabs share a bounded deadline', async () => {
  const hung = () => page({evaluate: () => new Promise(() => {})});
  const start = Date.now();
  assert.equal(await inspect(browser(hung(), hung(), hung(), page()), address), 'healthy');
  assert.ok(Date.now() - start < 3000);
});
test('failed named sender cannot be hidden by a healthy unnamed mailbox', async () => {
  const broken = page();
  broken.getByRole = () => ({count: async () => {throw new Error('sender crash');}});
  assert.equal(await inspect(browser(broken, page({named: false})), address), 'connection_failed');
});
test('unnamed fallback cannot hide an unidentified failed tab', async () => {
  const broken = page({evaluate: async () => {throw new Error('unknown tab');}});
  assert.equal(await inspect(browser(broken, page({named: false})), address), 'connection_failed');
});
