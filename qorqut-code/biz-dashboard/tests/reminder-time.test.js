'use strict';
const {test} = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
test('reminder times deduplicate equivalent instants only', () => {
  const source = fs.readFileSync(path.join(__dirname, '../js/reminders.js'), 'utf8');
  const expression = source.match(/const remSameTime = .*;/)[0];
  const same = vm.runInNewContext(expression + '\nremSameTime', {Date, Number});
  assert.equal(same('2026-11-06T12:00:00Z', '2026-11-06T07:00:00-05:00'), true);
  assert.equal(same('2026-11-06T12:00:00Z', '2026-11-07T12:00:00Z'), false);
  assert.equal(same('invalid', 'invalid'), false);
  assert.equal(same(null, null), false);
});
