'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../js/agent-focus.js'), 'utf8');
const code = source.slice(source.indexOf('async function agfocusRefresh(p)'), source.indexOf('// Grouped work: a Task row expands'));
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  const mount = {}, p = {focusMount: mount, view: 'focus', focusWork: new Map([['t1', {expanded: true}]]), focusPrivateRead: new Map(), focusPrivateUnread: new Map()};
  const h = {p, mount, active: true, hidden: false, paints: [], expands: [], reads: [], releaseExpand: null};
  const context = {
    Date, Array, Set, Map, Promise, Error, Number,
    agpadActive: (q, allowHidden) => h.active && q === p && (allowHidden || !h.hidden),
    agfocusPageKey: () => 'k', coreData: x => x, accessLost: () => false, AGFOCUS_POLL_MS: 1000,
    agfocusTextHost: () => null,
    post: () => new Promise((res, rej) => h.reads.push({res, rej})),
    agfocusVisibleItems: (q, items) => items,
    agfocusRestorable: (q, item) => !!q.focusWork.get(item.id)?.expanded,
    // Mirrors the real guard: a hidden pane never starts a new branch read.
    agfocusExpandWork: (q, item) => h.hidden ? Promise.resolve() : new Promise(res => h.expands.push({item, res})),
    agfocusPaint: q => { h.paints.push({items: q.focus?.items.map(i => i.id), done: !q.focusPending}); h.onPaint?.(); },
  };
  vm.createContext(context);
  vm.runInContext(code + ';this.agfocusRefresh=agfocusRefresh;', context);
  h.refresh = () => context.agfocusRefresh(p);
  h.data = () => ({items: [{id: 't1'}], revision: 1});
  return h;
}

test('main data paints before a blocked expanded branch settles, then final paint follows', async () => {
  const h = harness(); let settled = false;
  const run = h.refresh().then(() => { settled = true; });
  h.reads[0].res(h.data()); await tick();
  assert.equal(h.p.focus.revision, 1);
  assert.deepEqual(h.paints, [{items: ['t1'], done: false}]);
  assert.equal(h.expands.length, 1); assert.equal(settled, false); assert.equal(h.p.focusPending, true);
  h.expands[0].res(); await run;
  assert.equal(settled, true); assert.equal(h.p.focusPending, false);
  assert.equal(h.paints.length, 2); assert.equal(h.paints[1].done, true);
});

test('hidden pane does not paint early or start branch reads', async () => {
  const h = harness(); const run = h.refresh();
  h.hidden = true; h.reads[0].res(h.data()); await run;
  assert.equal(h.expands.length, 0);
  assert.equal(h.paints.filter(x => !x.done).length, 0);
});

test('navigation away during the read does not paint the wrong pane', async () => {
  const h = harness(); const run = h.refresh();
  h.p.focusMount = {}; h.reads[0].res(h.data()); await tick();
  // The obsolete response is discarded and one fresh read is requested for the current pane.
  assert.equal(h.reads.length, 2); assert.equal(h.p.focus, undefined); assert.equal(h.expands.length, 0);
  assert.equal(h.paints.filter(x => !x.done).length, 0);
  h.active = false; h.reads[1].res(h.data()); await run;
});

test('a pane closed by the early paint starts no branch reads', async () => {
  const h = harness();
  h.onPaint = () => { h.active = false; };
  const run = h.refresh();
  h.reads[0].res(h.data()); await run;
  assert.equal(h.paints.length, 1);
  assert.equal(h.expands.length, 0);
  assert.equal(h.p.focusPending, false);
});

test('a failed branch keeps its failure behaviour and later completion still repaints', async () => {
  const h = harness(); const run = h.refresh();
  h.reads[0].res(h.data()); await tick();
  assert.equal(h.paints.length, 1);
  h.p.focusWork.get('t1').expanded = false; // what a failed first load does
  h.expands[0].res(); await run;
  assert.equal(h.p.focusError, ''); assert.equal(h.p.focusPending, false);
  assert.equal(h.paints.length, 2); assert.equal(h.paints[1].done, true);
});
