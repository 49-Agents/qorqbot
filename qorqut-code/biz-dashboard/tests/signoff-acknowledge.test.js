'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {spawnSync}=require('node:child_process');
const path=require('node:path');
const chrome=['google-chrome','chromium','chromium-browser'].find(bin=>spawnSync(bin,['--version']).status===0);

test('something to read has its own group, one Acknowledge button, Markdown text and a separate comment box', {skip:!chrome && 'Chrome not installed'},()=>{
  const file=path.join(__dirname,'fixtures/signoff-acknowledge.html');
  const run=spawnSync(chrome,['--headless=new','--no-sandbox','--disable-gpu','--allow-file-access-from-files','--window-size=800,1400','--virtual-time-budget=6000','--dump-dom','file://'+file],{encoding:'utf8',maxBuffer:1<<25});
  assert.equal(run.status,0,run.stderr);
  const match=/<pre id="out"[^>]*>(.*?)<\/pre>/s.exec(run.stdout);
  assert.ok(match,'Fixture must report a result');
  const out=JSON.parse(match[1].replace(/&quot;/g,'"').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&'));
  assert.equal(out.error,undefined,JSON.stringify(out));
  for(const name of ['ownGroup','markdown','oneButton','noAnswerField','commentBox','questionUnchanged','acknowledged','onePost','inlineCard','disposed']) assert.equal(out[name],true,name);
  // Acknowledging says nothing: the request is the binding alone.
  assert.equal(out.payload.id,'report');
  assert.equal(out.payload.action,'respond');
  assert.deepEqual(Object.keys(out.payload.body).sort(),['expected_status_revision','idempotency_key','payload_hash','proposal_revision']);
});
