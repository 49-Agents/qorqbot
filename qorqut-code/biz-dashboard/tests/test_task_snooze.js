// Pure UI semantics: San Juan time conversion (browser zone ignored), expiry, unchanged drafts and validation.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
process.env.TZ = 'America/New_York';
class Clock extends Date { static now() { return Date.UTC(2029,0,1); } }
const context = vm.createContext({Date:Clock, esc:s=>String(s??'')});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/san-juan-time.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/task-snooze.js'),'utf8'),context);
const run = code => vm.runInContext(code,context);
assert.equal(run('taskSnoozed({})'),false);
assert.equal(run('taskSnoozed({snoozed_until:null})'),false);
assert.equal(run('taskSnoozed({snoozed_until:"2029-01-01T00:00:00Z"})'),false);
assert.equal(run('taskSnoozed({snoozed_until:"2029-01-01T00:00:00.001Z"})'),true);
assert.equal(run('taskSnoozed({snooze:{indefinite:true}})'),false,'the retired indefinite snooze read model is ignored');
assert.equal(run('taskSnoozeLocal("2030-01-02T09:30:12.123456Z")'),'2030-01-02T05:30');
assert.match(run('taskSnoozeLabel({snoozed_until:"2029-01-02T09:30:00Z"})'),/^Snoozed until /);
assert.equal(run('taskSnoozeLabel({snoozed_until:"2020-01-02T09:30:00Z"})'),'');
const picker = run('taskSnoozePicker({snoozed_until:"2029-01-02T09:30:00Z"})');
assert.match(picker,/data-initial="until"/);
assert.doesNotMatch(picker,/indefinite|further notice/i,'no indefinite snooze input');
function editor(mode, initial, value='') {
  context.host = {querySelector:selector=>selector==='[data-snooze-mode]' ? {value:mode,dataset:{initial}} : {value}};
}
editor('until','none','2030-01-02T05:30');
assert.equal(run('taskSnoozeChange(host)'),'2030-01-02T09:30:00.000Z');
editor('none','until');
assert.equal(run('taskSnoozeChange(host)'),null);
editor('until','until','2020-01-02T05:30');
assert.equal(run('taskSnoozeChange(host,{snoozed_until:"2020-01-02T09:30:12.123456Z"})'),undefined,
  'An unchanged timestamp that expires while editing is not resubmitted or rounded');
editor('until','none','2020-01-02T04:30');
assert.throws(()=>run('taskSnoozeChange(host)'),/future snooze/);
editor('until','none','');
assert.throws(()=>run('taskSnoozeChange(host)'),/future snooze/);
editor('none','none');
assert.equal(run('taskSnoozeChange(host,{snoozed_until:"2020-01-02T09:30:00Z"})'),undefined,
  'Ordinary edits preserve stored expired metadata');
console.log('task snooze UI checks passed');
