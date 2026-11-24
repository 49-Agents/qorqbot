const test = require('node:test');
const assert = require('node:assert');
const notice = require('../js/setup-notice.js');

test('a missing Chrome is reported with where to get it', () => {
  const message = notice.message({chrome_installed: false});
  assert.match(message.text, /Google Chrome/);
  assert.strictEqual(message.url, 'https://www.google.com/chrome/');
});

test('nothing is shown when Chrome is installed or unknown', () => {
  assert.strictEqual(notice.message({chrome_installed: true}), null);
  assert.strictEqual(notice.message({}), null);
  assert.strictEqual(notice.message(null), null);
});
