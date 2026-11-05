#!/usr/bin/env node
const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const requests = [];
let resolveData, resolveLogs;
const sandbox = vm.createContext({
  state:{biz:'a',scope:'business'}, tView:'routines', lnSvg:String,
  api:url => { requests.push(url); return new Promise(resolve => { if(url.includes('/runs?')) resolveLogs=resolve; else resolveData=resolve; }); },
});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/routines.js'),'utf8'),sandbox);
const run = code => vm.runInContext(code,sandbox);
run('rtRender=()=>{}');
(async()=>{
  run('tPaintRoutines()');
  const first=run('rtRefresh()');
  assert.equal(requests.length,1,'opening active routines fetches definitions only and shares the pending request');
  resolveData({routines:[{id:'one',scope:'biz'}]}); await first;
  run('state.biz="b"; tPaintRoutines()');
  assert.equal(run('rtData'),null,'another business does not inherit cached routines');
  const second=run('rtRefresh()');resolveData({routines:[]});await second;
  run('state.biz="a"; tPaintRoutines()');
  assert.equal(run('rtData.routines[0].id'),'one','returning paints cached routines before request resolves');
  const third=run('rtRefresh()');resolveData({routines:[{id:'one',scope:'biz'}]});await third;
  run('rtPage="logs"');const logs=run('rtRefresh()');
  assert.equal(requests.at(-1),'/biz/a/routines/runs?n=50','history is requested when the runs view opens');
  resolveData({routines:[]});resolveLogs([]);await logs;
  console.log('Routine loading checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
