const assert=require('node:assert/strict'),path=require('node:path'),{spawn,spawnSync}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const core=process.env.BAZARLYK_CORE_ROOT || path.resolve(__dirname,'../../biz-core');
(async()=>{
 const child=spawn(process.env.BAZARLYK_PYTHON || path.join(core,'.venv/bin/python'),['tests/service_browser_fixture.py','--notes-only','--dashboard-root',path.resolve(__dirname,'..')],{cwd:core,env:{...process.env,PYTHONPATH:core},stdio:['ignore','pipe','pipe']});
 let browser,stderr='';child.stderr.on('data',b=>stderr+=b);
 try{
  const details=await new Promise((resolve,reject)=>{let out='';const timer=setTimeout(()=>reject(Error('Fixture timeout '+stderr)),120000);child.stdout.on('data',b=>{out+=b;for(const line of out.split('\n'))if(line.startsWith('{"url":')){clearTimeout(timer);resolve(JSON.parse(line));}});child.once('exit',code=>{clearTimeout(timer);reject(Error('Fixture exit '+code+stderr));});});
  browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(details.url);await page.waitForFunction(()=>typeof selectGlobal==='function');
  await page.evaluate(()=>selectGlobal('notes'));
  const host=page.locator('#tbody');
  await host.locator('#ntnew').waitFor();
  assert.equal(await page.locator('[data-global-tab=notes]').count(),0);
  assert.equal(await host.locator('.jrhcell[data-key]').count()>0,true);
  await host.locator('#ntnew').click();await host.locator('#nttitle').waitFor();
  assert.equal(await host.locator('#ntupload').count(),1);
  assert.equal(await host.locator('#ntupload').textContent().then(s=>s.trim()),'Add to Context');
  assert.equal(await host.locator('#nt-stories').count(),0);
  assert.equal(await host.locator('#nttitle').inputValue(),'Daily');
  assert.equal(await host.locator('#ntvisibility').getAttribute('aria-checked'),'true');
  assert.equal(await host.locator('#ntvisibility svg').count(),1);
  await host.locator('#ntvisibility').click();
  await page.waitForFunction(()=>document.querySelector('#ntvisibility')?.getAttribute('aria-checked')==='false');
  await page.reload();await host.locator('#nttitle').waitFor();
  assert.equal(await host.locator('#ntvisibility').getAttribute('aria-checked'),'false');
  await host.locator('#ntvisibility').click();
  await page.waitForFunction(()=>document.querySelector('#ntvisibility')?.getAttribute('aria-checked')==='true');
  await host.locator('#nttitle').fill('Global journal');
  await host.locator('#ntdoc .ntline').first().fill('Remember this across businesses');
  // One history survives structural redraws and saves.
  await host.locator('#ntdoc').press('Control+z');
  assert.equal(await host.locator('#ntdoc .ntline').first().textContent(),'');
  await host.locator('#ntdoc').press('Control+Shift+z');
  assert.equal(await host.locator('#ntdoc .ntline').first().textContent(),'Remember this across businesses');
  await page.evaluate(()=>ntSaveNow());
  await host.locator('#ntdoc').press('Control+z');
  assert.equal(await host.locator('#ntdoc .ntline').first().textContent(),'');
  await host.locator('#ntdoc').press('Control+Shift+z');
  await page.evaluate(()=>ntCommit(['- First task','- Second task'],{index:1,pos:3}));
  await host.locator('.ntball').first().click();
  assert.equal(await page.evaluate(()=>ntLineStatus(ntEditState,0)),'white');
  await host.locator('.ntball').first().press('Control+z');
  assert.equal(await page.evaluate(()=>ntLineStatus(ntEditState,0)),'todo');
  await host.locator('#ntdoc').press('Control+z');
  assert.equal(await host.locator('#ntdoc .ntline').first().textContent(),'Remember this across businesses');
  await host.locator('#ntdoc .ntline').first().fill('New edit');
  await host.locator('#ntdoc').press('Control+Shift+z');
  assert.equal(await host.locator('#ntdoc .ntline').first().textContent(),'New edit');
  await host.locator('#ntdoc .ntline').first().fill('Remember this across businesses');
  // Real mouse triple-clicks must select only the clicked journal block, including
  // task rows adjacent to other task rows (non-editable markers used to collapse it).
  const tripleClick = async i => {
    await host.locator('#ntdoc .ntline').nth(i).click({clickCount:3,position:{x:90,y:12}});
  };
  const selection = () => page.evaluate(()=>getSelection().toString());
  const lines = () => page.evaluate(()=>ntEditState.lines);
  const trialLines = ['- First task words','- Second task words','Plain journal line','Last journal line'];
  await page.evaluate(values=>ntCommit(values,{index:0,pos:0}),trialLines);
  for (let i=0;i<trialLines.length;i++) {
    await tripleClick(i);
    assert.equal(await selection(),trialLines[i].replace(/^- /,''));
  }
  await tripleClick(0);
  await page.context().grantPermissions(['clipboard-read','clipboard-write']);
  await page.keyboard.press('Control+c');
  assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'- First task words');
  await page.keyboard.insertText('Replaced task');
  assert.deepEqual(await lines(),['- Replaced task',...trialLines.slice(1)]);
  await host.locator('#ntdoc').press('Control+z');
  assert.deepEqual(await lines(),trialLines);
  await tripleClick(0);
  await page.keyboard.press('Backspace');
  assert.equal((await lines())[0].replace(/^- ?/,''),'');
  assert.deepEqual((await lines()).slice(1),trialLines.slice(1));
  await host.locator('#ntdoc').press('Control+z');
  assert.deepEqual(await lines(),trialLines);
  await tripleClick(1);
  await page.keyboard.press('Control+x');
  assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'- Second task words');
  assert.deepEqual((await lines()).slice(2),trialLines.slice(2));
  await host.locator('#ntdoc').press('Control+z');
  assert.deepEqual(await lines(),trialLines);
  // Double-click still selects a word; task controls still work outside the text.
  await host.locator('#ntdoc .ntline').nth(0).dblclick({position:{x:90,y:12}});
  assert.equal((await selection()).trim(),'task');
  await host.locator('.ntball').first().click();
  assert.equal(await page.evaluate(()=>ntLineStatus(ntEditState,0)),'white');
  await host.locator('.ntball').first().click({button:'right'});
  assert.equal(await page.evaluate(()=>ntLineStatus(ntEditState,0)),'blocked');
  // Wrapped text and soft breaks remain one journal block; resizing keeps its
  // status control aligned with the next block rather than overlapping text.
  const wrapped = '- '+ 'Wrapped journal text '.repeat(15) + '\u2028continued text';
  await page.evaluate(value=>ntCommit([value,'- Following task'],{index:0,pos:0}),wrapped);
  await page.setViewportSize({width:900,height:1000});
  await tripleClick(0);
  assert.equal(await selection(),wrapped.slice(2).replaceAll('\u2028','\n'));
  const offsets=await page.evaluate(()=>[...document.querySelectorAll('.ntball')].map(b=>{
    const row=document.querySelectorAll('#ntdoc .ntline')[Number(b.dataset.i)];
    return b.getBoundingClientRect().top-row.getBoundingClientRect().top;
  }));
  assert(Math.abs(offsets[0]-offsets[1])<1);
  await page.setViewportSize({width:1440,height:1000});
  await page.evaluate(()=>ntCommit(['Remember this across businesses'],{index:0,pos:0}));
  // Leaving through navigation must save before removing the editor.
  await page.locator('#taskspanel [data-v=tasks]').click();
  await page.waitForFunction(()=>tView==='tasks');
  await page.locator('#taskspanel [data-v=notes]').click();
  await host.locator('#nttitle').waitFor();
  assert.equal(await host.locator('#nttitle').inputValue(),'Global journal');
  assert.equal(await host.locator('#ntdoc .ntline').first().textContent(),'Remember this across businesses');
  const identity=await page.evaluate(()=>ntSel);
  await page.evaluate(()=>ntTogglePin(ntById(ntSel)));
  await page.waitForFunction(()=>ntById(ntSel)?.pinned);
  await page.reload();await host.locator('#nttitle').waitFor();
  assert.equal(await host.locator('#nttitle').inputValue(),'Global journal');
  assert(await page.evaluate(()=>ntById(ntSel).pinned));
  await host.locator('#ntdoc .ntline').first().fill('Saved before business switch');
  await page.evaluate(()=>selectBiz(state.bizs[0].slug));
  await page.evaluate(()=>{openTasks(true);tSetView('notes');});
  await page.waitForFunction(()=>tView==='tasks');
  assert.equal(await page.locator('#taskspanel [data-v=notes]').count(),0);
  assert.equal(await page.locator('#tbody #ntnew').count(),0);
  await page.evaluate(()=>selectGlobal('notes'));await host.locator('#nttitle').waitFor();
  assert.equal(await host.locator('#nttitle').inputValue(),'Global journal');
  assert.equal(await host.locator('#ntdoc .ntline').first().textContent(),'Saved before business switch');

  // A rejected save must retain both the page and its unsaved text.
  await page.route('**/api/root/notes/'+identity,route=>route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({error:'Test conflict'})}));
  await host.locator('#nttitle').fill('Unsaved conflict');
  await page.locator('#taskspanel [data-v=tasks]').click();
  await host.locator('#nterror').waitFor();
  assert.equal(await page.evaluate(()=>tView),'notes');
  assert.equal(await host.locator('#nttitle').inputValue(),'Unsaved conflict');
  await page.unroute('**/api/root/notes/'+identity);
  await host.locator('#nttitle').fill('Global journal');
  await page.evaluate(()=>ntSaveNow());
  await page.evaluate(()=>ntGoHome());await host.locator('#ntnew').waitFor();
  assert.equal(await page.evaluate(()=>ntNotes.length),1);
  await page.screenshot({path:'/tmp/global-notes-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  assert(await host.locator('.jrcard').evaluateAll(cards=>cards.every(card=>card.getBoundingClientRect().right<=innerWidth)));
  await page.screenshot({path:'/tmp/global-notes-mobile.png'});
  await page.reload();await host.locator('#ntnew').waitFor();
  await host.locator('.jrcard').first().click();await host.locator('#nttitle').waitFor();
  assert(await host.locator('#nttitle').isVisible());
  assert(await host.locator('#nttitle').evaluate(el=>el.getBoundingClientRect().right<=innerWidth));
  const selected=await page.evaluate(()=>ntSel), uploadRequests=[];
  await page.route(`**/api/root/notes/${selected}/upload`,async route=>{
    const payload=route.request().postDataJSON();uploadRequests.push(payload);
    const response=await route.fetch();
    assert.equal(response.status(),202,'real journal upload route should accept the button request');
    if(uploadRequests.length===1){
      // The real application has accepted and persisted the upload. Drop only
      // the browser-facing response to reproduce a lost acknowledgement.
      await route.abort();return;
    }
    await route.fulfill({response});
  });
  await host.locator('#nttitle').fill('Explicit filer upload');
  await host.locator('#ntdoc .ntline').first().fill('This exact saved revision is submitted.');
  await host.locator('#ntupload').click();
  await page.waitForFunction(()=>document.querySelector('#ntupload')?.textContent.includes('Retry'));
  assert.equal(await host.locator('#nttitle').evaluate(el=>el.readOnly),true);
  assert.equal(await host.locator('#ntdoc').getAttribute('contenteditable'),'false');
  const savedRevision=await page.evaluate(()=>ntById(ntSel).revision);
  await host.locator('#ntupload').click();
  await page.evaluate(id=>ntDoUpload(id),selected); // a repeated click during submission reuses the in-flight request
  await page.waitForFunction(()=>document.querySelector('#ntfiling')?.textContent.includes('Queued'));
  assert.equal(uploadRequests.length,2);
  assert.equal('document_id' in uploadRequests[0],false);
  assert.equal('document_id' in uploadRequests[1],false);
  assert.equal(uploadRequests[0].expected_revision,savedRevision);
  assert.equal(uploadRequests[1].expected_revision,uploadRequests[0].expected_revision);
  assert.equal(uploadRequests[1].expected_content_hash,uploadRequests[0].expected_content_hash);
  assert.equal(uploadRequests[1].idempotency_key,uploadRequests[0].idempotency_key);
  assert.match(await host.locator('#ntfiling').textContent(),/Queued/);
  const snapshotCheck=spawnSync(process.env.BAZARLYK_PYTHON||path.join(core,'.venv/bin/python'),['-c',
    "import json,sqlite3,sys; db=sqlite3.connect(sys.argv[1]+'/.bazarlyk/state.sqlite3'); db.create_function('inbox_actor',0,lambda:None); ids=[row[0] for row in db.execute(\"SELECT id FROM documents WHERE kind='context_upload' AND json_extract(metadata_json,'$.journal_source_id')=?\",(sys.argv[2],))]; jobs=sum(db.execute(\"SELECT count(*) FROM jobs WHERE kind='context.base_file' AND json_extract(arguments_json,'$.upload_id')=?\",(upload_id,)).fetchone()[0] for upload_id in ids); print(json.dumps({'uploads':len(ids),'jobs':jobs}))",
    details.root,selected],{encoding:'utf8'});
  assert.equal(snapshotCheck.status,0,snapshotCheck.stderr);
  assert.deepEqual(JSON.parse(snapshotCheck.stdout),{uploads:1,jobs:1});
  const submission=await page.evaluate(async id=>{
    const entries=await fetch('/api/root/notes').then(response=>response.json());
    return entries.find(entry=>entry.id===id)?.filerSubmissions?.[0];
  },selected);
  assert(submission?.uploadId);
  assert.equal(submission.state,'queued');
  assert.equal(await host.locator('#ntfiling a').getAttribute('href'),`/api/root/context-library/${submission.uploadId}`);
  const injected=spawnSync(process.env.BAZARLYK_PYTHON||path.join(core,'.venv/bin/python'),['-c',
    "import json,sqlite3,sys; db=sqlite3.connect(sys.argv[1]+'/.bazarlyk/state.sqlite3'); db.create_function('inbox_actor',0,lambda:None); uid,source=sys.argv[2:]; row=db.execute(\"SELECT metadata_json,revision FROM documents WHERE id=?\",(uid,)).fetchone(); meta=json.loads(row[0]); meta.update(status='failed',context_base={'status':'failed','summary':{'unresolved_checks':['prior verification']}}); db.execute(\"UPDATE documents SET metadata_json=?,revision=?,updated_at=datetime('now') WHERE id=?\",(json.dumps(meta),row[1]+1,uid)); job=db.execute(\"SELECT id FROM jobs WHERE kind='context.base_file' AND json_extract(arguments_json,'$.upload_id')=? ORDER BY rowid DESC LIMIT 1\",(uid,)).fetchone()[0]; db.execute(\"UPDATE jobs SET state='failed',error_json=? WHERE id=?\",(json.dumps({'message':'injected provider failure'}),job)); db.commit()",
    details.root,submission.uploadId,selected],{encoding:'utf8'});
  assert.equal(injected.status,0,injected.stderr);
  await page.evaluate(async()=>{await ntFetch();ntPaintUpload();ntWatchFiling();});
  await page.waitForFunction(()=>document.querySelector('#ntfiling')?.textContent.includes('Filing failed'));
  assert.equal(await host.locator('.ntretryfiler').count(),1);
  await host.locator('.ntretryfiler').click();
  await page.waitForFunction(()=>document.querySelector('#ntfiling')?.textContent.includes('Queued'));
  await host.locator('#ntfiling').getByText(/Previous attempt receipt \(failed\)/).waitFor();
  assert.equal(await page.evaluate(()=>ntFilingPoll!==null),true);
  const retrySnapshotCheck=spawnSync(process.env.BAZARLYK_PYTHON||path.join(core,'.venv/bin/python'),['-c',
    "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]+'/.bazarlyk/state.sqlite3'); db.create_function('inbox_actor',0,lambda:None); uid,source=sys.argv[2:]; uploads=db.execute(\"SELECT count(*) FROM documents WHERE kind='context_upload' AND json_extract(metadata_json,'$.journal_source_id')=?\",(source,)).fetchone()[0]; jobs=db.execute(\"SELECT count(*) FROM jobs WHERE kind='context.base_file' AND json_extract(arguments_json,'$.upload_id')=?\",(uid,)).fetchone()[0]; print(f'{uploads}:{jobs}')",
    details.root,submission.uploadId,selected],{encoding:'utf8'});
  assert.equal(retrySnapshotCheck.status,0,retrySnapshotCheck.stderr);
  assert.equal(retrySnapshotCheck.stdout.trim(),'1:2');
  const finished=spawnSync(process.env.BAZARLYK_PYTHON||path.join(core,'.venv/bin/python'),['-c',
    "import json,sqlite3,sys; db=sqlite3.connect(sys.argv[1]+'/.bazarlyk/state.sqlite3'); db.create_function('inbox_actor',0,lambda:None); uid=sys.argv[2]; row=db.execute(\"SELECT metadata_json,revision FROM documents WHERE id=?\",(uid,)).fetchone(); meta=json.loads(row[0]); meta.update(status='processed',context_base={'status':'filed','summary':{'developments':1}}); db.execute(\"UPDATE documents SET metadata_json=?,revision=?,updated_at=datetime('now') WHERE id=?\",(json.dumps(meta),row[1]+1,uid)); job=db.execute(\"SELECT id FROM jobs WHERE kind='context.base_file' AND json_extract(arguments_json,'$.upload_id')=? ORDER BY rowid DESC LIMIT 1\",(uid,)).fetchone()[0]; db.execute(\"UPDATE jobs SET state='succeeded' WHERE id=?\",(job,)); db.commit()",
    details.root,submission.uploadId],{encoding:'utf8'});
  assert.equal(finished.status,0,finished.stderr);
  await page.evaluate(async()=>{await ntFetch();ntPaintUpload();ntWatchFiling();});
  await page.waitForFunction(()=>document.querySelector('#ntfiling')?.textContent.includes('Filed'));
  assert.equal(await page.evaluate(()=>ntFilingPoll),null);
  assert.equal(await page.evaluate(id=>localStorage.getItem(`nt-filer-upload:${id}`),selected),null);
  await page.screenshot({path:'/tmp/global-notes-mobile-editor.png'});
  assert.deepEqual(errors,[]);
  console.log('PASS Global Notes: CRUD, autosave navigation, reload, visibility switch, real-service upload, persisted same-key retry after lost response, real retry from failure through queued to filed, polling and prior-receipt retention, triple-click/copy/cut/replace/delete/undo, status controls, conflict retention and mobile layout');
 }finally{if(browser)await browser.close();child.kill('SIGINT');}
})().catch(error=>{console.error(error);process.exitCode=1;});
