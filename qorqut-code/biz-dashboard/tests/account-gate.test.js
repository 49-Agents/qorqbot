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

test('the sign-in screen blocks the page behind it and reports a resent code as a status, not an error', () => {
  const source = require('node:fs').readFileSync(require('node:path').join(__dirname, '../js/account-gate.js'), 'utf8');
  assert.match(source, /blocked = \[\.\.\.document\.body\.children\]\.filter\(el => el !== root && !el\.inert\)/);
  assert.match(source, /blocked\.forEach\(el => \{ el\.inert = false; \}\)/);
  assert.match(source, /<p class="account-gate-note" data-account-note role="status">/);
  assert.match(source, /showNote\(section, 'A new code is on its way\.'\)/);
  assert.ok(!source.includes("showError(section, {message: 'A new code"));
});
