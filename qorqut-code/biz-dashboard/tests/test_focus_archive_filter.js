// Exercise the real nested-row projection with cached history, without live data.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const ctx = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../js/agent-focus.js'), 'utf8'), ctx);
const task = (id, extra = {}) => ({id, source: {type: 'task', id}, ...extra});
const parent = task('triage'), group = task('other', {_workParent: 'triage'});
const active = task('active', {_workParent: 'other'});
const archived = task('archived', {_workParent: 'other', archived: true});
const completed = task('done', {_workParent: 'other', state: 'done'});
const related = task('related', {_workParent: 'other', _workKind: 'related', archived: true});
const grandchild = task('grandchild', {_workParent: 'archived'});
const p = {focusWork: new Map([
  ['triage', {expanded: true, items: [group]}],
  ['other', {expanded: true, items: [active, archived, completed, related]}],
  ['archived', {expanded: true, items: [grandchild]}],
])};
const ids = () => Array.from(ctx.agfocusVisibleItems(p, [parent]), row => row.id);
assert.deepEqual(ids(), ['triage', 'other', 'active', 'done'], 'hide archived descendants and their branches; keep unarchived completed work');
p.focusIncludearchived = true;
assert.deepEqual(ids(), ['triage', 'other', 'active', 'archived', 'grandchild', 'done', 'related'], 'Archived reveals children, related Tasks and nested branches');
p.focusIncludearchived = false;
assert.deepEqual(ids(), ['triage', 'other', 'active', 'done'], 'turning Archived off hides the same cached rows again');
assert.equal(p.focusWork.get('other').items.length, 4, 'filtering never removes history from the snapshot');
assert.equal(archived.archived, true, 'archive state is unchanged');
console.log('Focus archive filter: nested children, related Tasks, archived branches, toggle round-trip and retained history passed.');
