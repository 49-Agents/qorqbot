#!/usr/bin/env node
const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; };
let request, shown = [], painted = [];
const pane = {isConnected:true, innerHTML:''};
const sandbox = vm.createContext({
  state:{biz:'a', scope:'business', tab:'context', ctx:'_people'},
  clearTimeout(){}, api:() => request.promise, ls(){},
  $:() => pane, esc:String,
  show:data => shown.push(data), paint:data => painted.push(data),
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../js/context.js'), 'utf8'), sandbox);
vm.runInContext(`
  wireCtxSearch = () => {};
  ctxTree = d => paint(d);
  ctxWatch = () => {};
  ctxShow = () => show(_ctxData);
`, sandbox);
const run = code => vm.runInContext(code, sandbox);
(async () => {
  request = deferred();
  const initial = run('renderContext({isConnected:true})');
  assert.equal(shown.at(-1), null);
  request.resolve({people:[{slug:'alice'}]}); await initial;
  assert.equal(shown.at(-1).people[0].slug, 'alice');
  request = deferred(); shown = [];
  const cached = run('renderContext({isConnected:true})');
  assert.equal(shown[0].people[0].slug, 'alice', 'cached people appear before network');
  request.resolve({people:[{slug:'alice'}]}); await cached;
  assert.equal(shown.length, 1, 'unchanged data does not redraw the page');
  request = deferred();
  const fresh = run('renderContext({isConnected:true})');
  request.resolve({people:[{slug:'bob'}]}); await fresh;
  assert.equal(shown.at(-1).people[0].slug, 'bob', 'changed data refreshes the page');
  request = deferred(); run('state.biz = "b"');
  const other = run('renderContext({isConnected:true})');
  assert.equal(shown.at(-1), null, 'cache is isolated by business');
  const count = painted.length;
  run('state.tab = "agents"');
  request.resolve({people:[{slug:'carol'}]}); await other;
  assert.equal(painted.length, count, 'late response cannot paint another page');
  console.log('Context navigation cache checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
