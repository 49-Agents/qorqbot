// The owner inbox delivery status DTO ({state, reason, last_attempt_at}) renders truthfully, with no read inference.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'js', 'agentpane.js'), 'utf8');
const start = source.indexOf('const AGENT_DELIVERY_STATES'), end = source.indexOf('// Current participants route replies');
const context = vm.createContext({});
vm.runInContext(source.slice(start, end) + ';this.view = agentDeliveryView;', context);
const view = context.view;
const cases = {
  idle: [false, /Nothing waiting/], pending: [true, /pending/], deferred: [true, /deferred/], submitted: [false, /processing unconfirmed/],
  uncertain: [true, /not resent/], stale: [true, /Session changed/], unavailable: [true, /unavailable/], unknown: [true, /unavailable/],
};
for (const [state, [attention, label]] of Object.entries(cases)) {
  const result = view({state, reason: null, last_attempt_at: '2026-10-12T10:00:00Z'});
  assert.equal(result.state, state); assert.equal(result.attention, attention, state); assert.match(result.label, label, state);
  assert.doesNotMatch(result.label, /\b(read|acknowledged|delivered)\b/i, state + ' claims nothing was read');
}
assert.match(view({state: 'pending', reason: 'offline'}).reason, /starts one manually/, 'offline waits for a manual session start');
assert.equal(view({state: 'martian'}).state, 'unknown', 'an unrecognised state is never presented as healthy');
assert.equal(view(null).state, 'unknown');
assert.equal(view({state: 'deferred', reason: 'not_typed:composer_occupied'}).reason, 'composer occupied');
console.log('Agent delivery view: every state, offline/manual start, uncertainty and unknown states passed.');
