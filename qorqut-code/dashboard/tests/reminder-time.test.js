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


test('due times are relative minutes, hours and days across timezone offsets', () => {
  const source = fs.readFileSync(path.join(__dirname, '../js/relative-time.js'), 'utf8');
  const format = vm.runInNewContext(source + '\nrelativeDueTime');
  const now = Date.parse('2026-11-14T12:00:00Z');
  assert.equal(format('2026-11-14T07:05:00-05:00', now), 'in 5 minutes');
  assert.equal(format('2026-11-14T10:00:00Z', now), '2 hours ago');
  assert.equal(format('2026-11-16T12:00:00Z', now), 'in 2 days');
  assert.equal(format('2026-11-14T12:00:10Z', now), 'now');
  assert.equal(format('invalid', now), '—');
});
