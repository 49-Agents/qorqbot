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

// The reminders form follows the app zone (Settings > Timezone), never the browser's own zone.
function reminderZoneContext(zone) {
  const time = fs.readFileSync(path.join(__dirname, '../js/san-juan-time.js'), 'utf8');
  const source = fs.readFileSync(path.join(__dirname, '../js/reminders.js'), 'utf8');
  const pick = name => source.match(new RegExp(`(?:const ${name} = [^\\n]*;|function ${name}\\([^]*?\\n\\})`))[0];
  const code = [time, "const remPad = n => String(n).padStart(2, '0');", ...['remZone', 'remInstant', 'remOffsetISO', 'remLocalInput'].map(pick),
    `sjConfigure('${zone}'); this.api = {remZone, remInstant, remOffsetISO, remLocalInput, sjConfigure};`].join('\n');
  const context = {Date, Number, Intl, Math, String, Set, Map, Object, Array, Error, RegExp, Number};
  vm.runInNewContext(code, context);
  return context.api;
}
test('reminder wall times and offsets use the app zone', () => {
  const api = reminderZoneContext('America/Puerto_Rico');
  assert.equal(api.remZone(), 'America/Puerto_Rico');
  assert.equal(api.remOffsetISO('2026-11-25T09:00'), '2026-11-25T09:00:00-04:00');
  assert.equal(api.remInstant('2026-11-25T09:00').toISOString(), '2026-11-25T13:00:00.000Z');
  assert.equal(api.remLocalInput('2026-11-25T13:00:00Z'), '2026-11-25T09:00');
  assert.equal(api.remOffsetISO(''), null);
  api.sjConfigure('Europe/Berlin');           // a live change of the owner's setting applies at once
  assert.equal(api.remZone(), 'Europe/Berlin');
  assert.equal(api.remOffsetISO('2026-07-01T09:00'), '2026-07-01T09:00:00+02:00');
  assert.equal(api.remLocalInput('2026-07-01T07:00:00Z'), '2026-07-01T09:00');
  api.sjConfigure('America/New_York');         // a wall time skipped by the spring-forward gap is refused
  assert.equal(api.remOffsetISO('2026-03-08T02:30'), null);
});
