const test = require('node:test');
const assert = require('node:assert');
const gate = require('../js/account-gate.js');

test('a newer release is announced with its download page', () => {
  const message = gate.updateMessage({update: {version: '1.3.1', url: 'https://qorqbot.com/downloads/'}});
  assert.strictEqual(message.text, 'Qorqbot 1.3.1 is available.');
  assert.strictEqual(message.url, 'https://qorqbot.com/downloads/');
  assert.strictEqual(message.version, '1.3.1');
});

test('nothing is announced without a newer release', () => {
  assert.strictEqual(gate.updateMessage({update: null}), null);
  assert.strictEqual(gate.updateMessage({}), null);
  assert.strictEqual(gate.updateMessage(null), null);
});

test('the license and privacy links point at the website pages', () => {
  assert.strictEqual(gate.LICENSE_URL, 'https://qorqbot.com/license/');
  assert.strictEqual(gate.PRIVACY_URL, 'https://qorqbot.com/privacy/');
});
