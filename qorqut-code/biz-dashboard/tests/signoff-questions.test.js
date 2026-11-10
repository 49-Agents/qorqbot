'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {spawnSync}=require('node:child_process');
const path=require('node:path');
const chrome=['google-chrome','chromium','chromium-browser'].find(bin=>spawnSync(bin,['--version']).status===0);

test('grouped Signoff answers are local until Done, shared across views, recoverable and submitted once', {skip:!chrome && 'Chrome not installed'},()=>{
  const file=path.join(__dirname,'fixtures/signoff-questions.html');
  const run=spawnSync(chrome,['--headless=new','--no-sandbox','--disable-gpu','--allow-file-access-from-files','--window-size=800,1400','--virtual-time-budget=6000','--dump-dom','file://'+file],{encoding:'utf8',maxBuffer:1<<25});
  assert.equal(run.status,0,run.stderr);
  const match=/<pre id="out"[^>]*>(.*?)<\/pre>/s.exec(run.stdout);
  assert.ok(match,'Fixture must report a result');
  const out=JSON.parse(match[1].replace(/&quot;/g,'"').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&'));
  assert.equal(out.error,undefined,JSON.stringify(out));
  for(const name of ['singleDone','uniqueLabels','boundaryEvidence','localErrors','independent','radioKeyboard','notesRequired','peerSync','pollKeepsCaret','singleChoice','unsubmitted','remountKeepsDraft',
    'narrowFits','wholeFormLocked','onePost','saved','globalKeepsReceipt','cleared','retry','lost','conflict','closed','serverError','serverRetry','staleRetained','staleDeliberateRetry','unknown','legacyDraft']) assert.equal(out[name],true,name);
  assert.deepEqual(out.payload.answers,[
    {question_id:'location',selected_choices:[],notes:'Use the garden instead.'},
    {question_id:'materials',selected_choices:['no','yes'],notes:'  Bring both.\nKeep this spacing.  '},
    {question_id:'details',selected_choices:[],notes:''},
  ]);
  assert.deepEqual(Object.keys(out.payload).sort(),['answers','expected_status_revision','idempotency_key','payload_hash','proposal_revision']);
});
