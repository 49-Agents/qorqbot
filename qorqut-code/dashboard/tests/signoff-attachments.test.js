'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {spawnSync}=require('node:child_process');
const path=require('node:path');
const chrome=['google-chrome','chromium','chromium-browser'].find(bin=>spawnSync(bin,['--version']).status===0);

test('a Signoff shows the files, tables and records its text links to under the text', {skip:!chrome && 'Chrome not installed'},()=>{
  const file=path.join(__dirname,'fixtures/signoff-attachments.html');
  const run=spawnSync(chrome,['--headless=new','--no-sandbox','--disable-gpu','--allow-file-access-from-files','--window-size=800,1400','--virtual-time-budget=8000','--dump-dom','file://'+file],{encoding:'utf8',maxBuffer:1<<25});
  assert.equal(run.status,0,run.stderr);
  const match=/<pre id="out"[^>]*>(.*?)<\/pre>/s.exec(run.stdout);
  assert.ok(match,'Fixture must report a result');
  const out=JSON.parse(match[1].replace(/&quot;/g,'"').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&'));
  assert.equal(out.error,undefined,JSON.stringify(out));
  assert.deepEqual(out.order,['notes','leads','CRM','Linden','broken','private','log']);
  for(const name of ['placed','markdown','mdSanitized','text','table','tableScope','crm','task','broken','denied','noXss','linkScrolls','modifiedClickOpens',
    'webLinkUntouched','open','kept','acknowledged','chatCard']) assert.equal(out[name],true,name);
});
