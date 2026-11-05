// The shared San Juan time utility ignores the browser/host zone.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
process.env.TZ = 'Asia/Tokyo';
const context = vm.createContext({Date, Intl, Number, Object, String});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../js/san-juan-time.js'), 'utf8'), context);
const run = code => vm.runInContext(code, context);
assert.equal(run('SJ_ZONE'), 'America/Puerto_Rico');
// San Juan midnight is 04:00 UTC; UTC midnight is still the previous San Juan day.
assert.equal(run('sjDate("2026-03-02T03:59:59Z")'), '2026-03-01');
assert.equal(run('sjDate("2026-03-02T04:00:00Z")'), '2026-03-02');
assert.equal(run('sjDate("2026-03-02T00:00:00Z")'), '2026-03-01');
assert.equal(run('sjTime("2026-03-02T03:30:00Z")'), '23:30');
assert.equal(run('sjToday(Date.UTC(2026, 2, 2, 3, 30))'), '2026-03-01');
// No DST: the offset is the same in winter and summer.
assert.equal(run('sjTime("2026-07-01T12:00:00Z")'), '08:00');
assert.equal(run('sjInstant("2026-03-01", 23, 30).toISOString()'), '2026-03-02T03:30:00.000Z');
assert.equal(run('sjInstant("2026-07-01", 8).toISOString()'), '2026-07-01T12:00:00.000Z');
// Literal calendar dates never shift.
assert.equal(run('sjAddDays("2026-03-01", -1)'), '2026-02-28');
assert.equal(run('sjAddDays("2026-12-31", 1)'), '2027-01-01');
assert.equal(run('sjDate("") + sjDate("not a date") + sjDateTime(null)'), '');
assert.match(run('sjDateTime("2026-03-02T03:30:00Z")'), /Mar 1, 2026/);
// The shared activity log formatter is precision-aware: a backfilled day or hour never shows its anchor as a time.
vm.runInContext(fs.readFileSync(path.join(__dirname, '../js/side.js'), 'utf8'), context);
assert.equal(run('JSON.stringify(logWhen({ts: "2026-03-01T04:00:00Z", occurred_date: "2026-03-01", time_precision: "day"}))'),
  JSON.stringify({day: '2026-03-01', time: '', full: '2026-03-01, time unknown'}));
assert.equal(run('JSON.stringify(logWhen({ts: "2026-03-01T17:00:00Z", occurred_date: "2026-03-01", occurred_hour: 13, time_precision: "hour"}))'),
  JSON.stringify({day: '2026-03-01', time: '13:00', full: '2026-03-01 13:00'}));
assert.equal(run('logWhen({ts: "2026-03-02T03:30:00Z", occurred_date: "2026-03-01", time_precision: "instant"}).time'), '23:30');
assert.equal(run('logWhen({ts: "2026-03-02T03:30:00Z"}).day'), '2026-03-01', 'legacy rows without precision stay instants');
console.log('san juan time checks passed');
