'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../js/conversation.js'),'utf8');
function load() {
  // At load the thread script only registers a document listener and publishes itself on the window.
  const ctx={document:{addEventListener(){}},window:{}};
  vm.createContext(ctx);
  vm.runInContext(source + '\nglobalThis.conversationPlanEntry = conversationPlanEntry;',ctx);
  return ctx.conversationPlanEntry;
}
const STEPS=[
  {id:'s1',text:'List three tools.',state:'completed',note:'Found three.'},
  {id:'s2',text:'Compare their prices.',state:'pending',current:true},
  {id:'s4',text:'Check list import.',state:'pending',added:true},
  {id:'s3',text:'Write a recommendation.',state:'skipped',note:'Only the table is wanted.'},
];
test('a plan is one line: how far it is and the step in hand',()=>{
  const entry=load()(STEPS,false);
  assert.equal(entry.key,'plan');assert.equal(entry.speaker,'notice');
  assert.equal(entry.text,'Plan: 2 of 4 steps done. Now: Compare their prices.');
  assert.deepEqual(JSON.parse(JSON.stringify(entry.actions)),[{key:'plan-steps',label:'Show steps'}]);
});
test('opened, the plan lists every step by position with its outcome and no identifiers',()=>{
  const entry=load()(STEPS,true);
  assert.equal(entry.key,'plan-open');
  assert.deepEqual(entry.text.split('\n'),[
    'Plan: 2 of 4 steps done. Now: Compare their prices.',
    '✓ 1. List three tools. (Found three.)',
    '→ 2. Compare their prices.',
    '○ 3. Check list import.',
    '– 4. Write a recommendation. (skipped: Only the table is wanted.)',
  ]);
  assert.equal(/\bs\d\b/.test(entry.text),false);
  assert.equal(entry.actions[0].label,'Hide steps');
});
test('a finished plan says so and a long step is clipped on the line only',()=>{
  const plan=load();
  const done=plan([{id:'s1',text:'One.',state:'completed'},{id:'s2',text:'Two.',state:'skipped'}],false);
  assert.equal(done.text,'Plan: all 2 steps finished.');
  const long='x'.repeat(400);
  const closed=plan([{id:'s1',text:long,state:'pending',current:true}],false);
  assert.equal(closed.text.length<200,true);assert.equal(closed.text.endsWith('…'),true);
  assert.equal(plan([{id:'s1',text:long,state:'pending',current:true}],true).text.includes(long),true);
});
