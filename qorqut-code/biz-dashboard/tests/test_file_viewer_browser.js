#!/usr/bin/env node
// Real shared service, temporary root, synthetic files and records only (biz-core/tests/file_viewer_browser_fixture.py).
// Drives the standalone /files/view page over HTTP and the ordinary links in the dashboard's task and story surfaces.
// Also embeds the reader in a same-origin frame (the dashboard Viewbox) and from a cross-origin parent.
// Screenshots go to $FILE_VIEWER_SHOTS (default /tmp/file-viewer-screens).
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const http=require('node:http');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const CORE=process.env.BAZARLYK_CORE_ROOT || path.resolve(__dirname,'../../biz-core');
const SHOTS=process.env.FILE_VIEWER_SHOTS || '/tmp/file-viewer-screens';
fs.mkdirSync(SHOTS,{recursive:true});

async function start(){
  const child=spawn(process.env.BAZARLYK_TEST_PYTHON || path.join(CORE,'.venv/bin/python'),['tests/file_viewer_browser_fixture.py','--dashboard-root',path.resolve(__dirname,'..')],
    {cwd:CORE,env:{...process.env,PYTHONPATH:CORE+path.delimiter+path.join(CORE,'tests')},stdio:['ignore','pipe','pipe']});
  let error='',output='';child.stderr.on('data',chunk=>error+=chunk);
  const details=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Fixture timeout: '+error)),180000);
    child.stdout.on('data',chunk=>{output+=chunk;for(const line of output.split('\n'))if(line.startsWith('{"url":')){clearTimeout(timer);resolve(JSON.parse(line));}});
    child.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exit '+code+': '+error));});
  }).catch(e=>{child.kill('SIGINT');throw e;});
  return {details,async stop(){if(child.exitCode!==null)return;await new Promise(resolve=>{const timer=setTimeout(()=>child.kill('SIGTERM'),5000);child.once('exit',()=>{clearTimeout(timer);resolve();});child.kill('SIGINT');});}};
}

const enc=value=>encodeURIComponent(value);
(async()=>{
  const fixture=await start();let browser;
  try{
    const d=fixture.details, origin=d.origin;
    browser=await chromium.launch({headless:true,args:['--no-sandbox'],...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
    const context=await browser.newContext({viewport:{width:1280,height:900},permissions:['clipboard-read','clipboard-write'],acceptDownloads:true});
    await context.route('https://example.com/**',route=>route.fulfill({contentType:'text/plain',body:'external page'}));
    const page=await context.newPage(), errors=[], childRequests=[], apiRequests=[], posts=[];
    // The fixture has no terminal broker, so the dashboard's own xterm mount logs an unrelated error; ignore only that.
    page.on('pageerror',e=>{if(String(e.stack).includes('/js/xterm.js'))return;errors.push(e.message);console.error(e.stack);});
    page.on('request',request=>{
      if(request.frame()!==page.mainFrame())childRequests.push(request.url());
      if(request.url().includes('/api/root/files/'))apiRequests.push(request.url());
      if(request.method()==='POST')posts.push(request.url());
    });
    await page.addInitScript(()=>{window.__messages=[];window.addEventListener('message',event=>window.__messages.push(String(event.data)));});
    const view=(query)=>`${origin}/files/view?${query}`;
    const byPath=name=>view('path='+enc('widget/files/'+name));
    const shot=async(name)=>page.screenshot({path:path.join(SHOTS,name+'.png')});
    const fileHead=async()=>({title:await page.locator('.fv-title h1').textContent(),path:await page.locator('.fv-path').textContent()});

    // --- Markdown: rendered, inert, links kept safe, frontmatter collapsed, source and wrap, same-origin link, download.
    await page.goto(byPath('report.md'));await page.locator('.fv-md').waitFor();
    assert.deepEqual(await fileHead(),{title:'report.md',path:'widget/files/report.md'});
    assert.equal(await page.title(),'report.md · Qorqut');
    assert.equal(await page.locator('.fv-chips').count(),0);
    assert.equal(await page.getByRole('button',{name:'History',exact:true}).count(),0,'an unmanaged file offers no History');
    assert.equal(await page.locator('.fv-md h1').textContent(),'Synthetic report');
    assert.equal(await page.locator('.fv-md script, .fv-md img, .fv-md #raw-html, .fv-md iframe').count(),0);
    assert.match(await page.locator('.fv-md').textContent(),/<b id="raw-html">raw html stays text<\/b>/);
    assert.equal(await page.evaluate(()=>window.__md),undefined);
    assert.equal(await page.locator('.fv-md a[href^="javascript"]').count(),0);
    assert.equal(await page.locator('.fv-md tbody tr').count(),2);
    const frontmatter=page.locator('.fv-md details.frontmatter');
    assert.equal(await frontmatter.evaluate(node=>node.open),false);
    assert.match(await frontmatter.textContent(),/owner: fixture/);
    const sibling=page.locator('.fv-md a[href="/files/view?path=widget/files/notes.txt"]');
    assert.equal(await sibling.getAttribute('target'),'_blank');assert.equal(await sibling.getAttribute('rel'),'noopener noreferrer');
    await shot('desktop-markdown');
    await page.getByRole('button',{name:'Source',exact:true}).click();
    assert.match(await page.locator('pre.fv-text').textContent(),/^---\ntitle: Synthetic report/);
    await page.getByRole('button',{name:'Copy text',exact:true}).click();
    assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),await page.locator('pre.fv-text').textContent());
    assert.equal(await page.getByRole('button',{name:'Source',exact:true}).textContent(),'');
    const wrap=page.getByRole('button',{name:'Wrap lines'});
    assert.equal(await wrap.count(),0);assert.equal(await page.locator('pre.fv-text.wrap').count(),1);
    await page.getByRole('button',{name:'Rendered',exact:true}).click();await page.locator('.fv-md').waitFor();
    const [popup]=await Promise.all([context.waitForEvent('page'),page.locator('.fv-md a',{hasText:'sibling file'}).click()]);
    await popup.waitForLoadState();await popup.locator('pre.fv-text').waitFor();
    assert.equal(popup.url(),`${origin}/files/view?path=widget/files/notes.txt`);
    assert.match(await popup.locator('pre.fv-text').textContent(),/<script>alert\(1\)<\/script>/);
    assert.equal(await popup.locator('#fv script').count(),0);
    await popup.close();
    const [download]=await Promise.all([page.waitForEvent('download'),page.getByRole('link',{name:'Download',exact:true}).click()]);
    assert.equal(download.suggestedFilename(),'report.md');
    assert.equal(fs.readFileSync(await download.path(),'utf8'),fs.readFileSync(path.join(d.root,'widget/files/report.md'),'utf8'));

    // --- No polling: a quiet page makes no further API requests.
    const settled=apiRequests.length;await page.waitForTimeout(2500);assert.equal(apiRequests.length,settled);

    // --- A file below the businesses root but outside any business; and a name with spaces and punctuation.
    await page.goto(view('path=shared%2Fbrief.md'));await page.locator('.fv-md').waitFor();
    assert.equal(await page.locator('.fv-md h1').textContent(),'Shared brief');
    await page.goto(byPath('weird & name (1).md'));await page.locator('.fv-md').waitFor();
    assert.equal(await page.locator('.fv-title h1').textContent(),'weird & name (1).md');

    // --- Plain text: escaped, never markup.
    await page.goto(byPath('notes.txt'));await page.locator('pre.fv-text').waitFor();
    assert.match(await page.locator('pre.fv-text').textContent(),/<script>alert\(1\)<\/script>/);
    assert.equal(await page.locator('#fv script').count(),0);
    assert.equal(await page.locator('pre.fv-text.wrap').count(),1);

    // --- CSV: bounded table, formulas are plain text.
    await page.goto(byPath('table.csv'));await page.locator('.fv-csv table').waitFor();
    assert.equal(await page.locator('.fv-csv tr').count(),200);
    assert.equal(await page.locator('.fv-csv thead th').count(),50);
    assert.equal(await page.locator('.fv-csv a, .fv-csv script').count(),0);
    assert.equal(await page.locator('.fv-csv td',{hasText:'=HYPERLINK("http://evil.example","x")'}).count(),1);
    assert.match(await page.locator('.fv-banner.warn').textContent(),/limited/);
    await shot('desktop-csv');

    // --- CSV Source: the bounded decoded original (BOM gone; quotes, doubled quotes, newlines kept), then back to Table.
    const fileText=name=>fs.readFileSync(path.join(d.root,'widget/files/'+name),'utf8').replace(/^﻿/,'');
    const source=page.getByRole('button',{name:'Source',exact:true}), tableMode=page.getByRole('button',{name:'Table',exact:true});
    await page.goto(byPath('small.csv'));await page.locator('.fv-csv table').waitFor();
    assert.equal(await page.locator('.fv-csv tr').count(),3);
    await source.click();await page.locator('pre.fv-text').waitFor();
    assert.equal(await page.locator('.fv-csv').count(),0);
    assert.equal(await page.locator('pre.fv-text').textContent(),fileText('small.csv'));
    assert.ok((await page.locator('pre.fv-text').textContent()).includes('"Smith, J","line1\nline2"\r\n=SUM(A1:A2),"say ""hi"""'));
    await tableMode.click();await page.locator('.fv-csv table').waitFor();
    assert.equal(await page.locator('pre.fv-text').count(),0);
    assert.equal(await page.locator('.fv-csv td',{hasText:'say "hi"'}).count(),1);
    assert.equal(await page.locator('.fv-csv td',{hasText:'line1'}).textContent(),'line1\nline2');

    await page.goto(byPath('table.csv'));await page.locator('.fv-csv table').waitFor();
    assert.equal(await page.locator('.fv-csv tr').count(),200);
    await source.click();await page.locator('pre.fv-text').waitFor();
    const tableSource=await page.locator('pre.fv-text').textContent();
    assert.equal(tableSource,fileText('table.csv'),'Source must be the original text, not the truncated table');
    assert.ok(tableSource.includes('"=HYPERLINK(""http://evil.example"",""x"")"'));
    assert.ok(tableSource.includes('r249c54'),'Source keeps the rows and columns the table preview cut');
    assert.equal(await page.locator('.fv-csv a, .fv-csv script').count(),0);
    assert.equal(await page.getByRole('button',{name:'Wrap lines'}).count(),0);assert.equal(await page.locator('pre.fv-text.wrap').count(),1);
    await shot('desktop-csv-source');
    await tableMode.click();await page.locator('.fv-csv table').waitFor();
    assert.equal(await page.locator('.fv-csv tr').count(),200);assert.equal(await page.locator('.fv-csv thead th').count(),50);

    // A file over the 2 MiB read limit: the table and Source are both cut at that limit, with the full Download offered.
    await page.goto(byPath('long.csv'));await page.locator('.fv-csv table').waitFor();
    assert.match(await page.locator('.fv-banner.warn').first().textContent(),/Showing the first 2\.0 MiB of \d/);
    await source.click();await page.locator('pre.fv-text').waitFor();
    const longSource=await page.locator('pre.fv-text').textContent(), longFile=fileText('long.csv');
    assert.equal(longSource.length,2*1024*1024);assert.ok(longFile.length>longSource.length);assert.ok(longFile.startsWith(longSource));
    assert.ok(longSource.startsWith('name,note,n\n"row 0, quoted","line1\nline2",0\n'));
    await tableMode.click();await page.locator('.fv-csv table').waitFor();
    assert.equal(await page.locator('.fv-csv tr').count(),200);

    if (process.env.FILE_VIEWER_CONTROLS_ONLY === '1') {
      await page.getByRole('button',{name:'Copy link',exact:true}).click();
      assert.equal(decodeURIComponent(await page.evaluate(()=>navigator.clipboard.readText())),decodeURIComponent(page.url()));
      assert.equal(await page.getByRole('button',{name:'Copy preview'}).count(),1);
      console.log('File viewer controls: icons, text/link copy, download, rendered/source, default wrapping and CSV passed.');
      return;
    }

    // --- HTML: a sandboxed, script-less frame that cannot reach the parent, the owner API or the network.
    await page.goto(byPath('page.html'));await page.locator('iframe.fv-frame').waitFor();
    const frameHandle=await page.locator('iframe.fv-frame').elementHandle(), frame=await frameHandle.contentFrame();
    assert.equal(await page.locator('iframe.fv-frame').getAttribute('sandbox'),'');
    assert.equal(await page.locator('iframe.fv-frame').evaluate(node=>node.sandbox.length),0);
    await frame.locator('#heading').waitFor();
    assert.equal(await frame.locator('#heading').textContent(),'Synthetic HTML','the script must not run');
    await page.waitForTimeout(400);
    assert.equal(await page.title(),'page.html · Qorqut');assert.deepEqual(await page.evaluate(()=>window.__messages),[]);
    const before=apiRequests.length, tabs=context.pages().length;
    await frame.locator('#api-link').click({noWaitAfter:true}).catch(()=>{});
    await frame.locator('#sibling-link').click({noWaitAfter:true}).catch(()=>{});
    await frame.locator('#submit').click({noWaitAfter:true}).catch(()=>{});
    await page.waitForTimeout(600);
    assert.equal(apiRequests.length,before,'the frame must not navigate into the owner API');
    assert.equal(context.pages().length,tabs,'a link in the preview must not open a window');
    assert.equal(await frame.locator('#heading').textContent(),'Synthetic HTML','links in the preview do nothing');
    assert.deepEqual(childRequests.filter(url=>!url.startsWith('about:')&&!url.startsWith('data:')),[],'the frame made a request');
    assert.equal(await page.locator('iframe.fv-frame').count(),1);
    await shot('desktop-html');
    await page.getByRole('button',{name:'Source',exact:true}).click();
    assert.match(await page.locator('pre.fv-text').textContent(),/<script>/);

    // --- Binary, missing, symlink, folder, traversal, too large.
    await page.goto(byPath('image.bin'));await page.getByRole('heading',{name:'No preview for this file'}).waitFor();
    assert.equal(await page.getByRole('link',{name:'Download',exact:true}).count(),2);
    await page.goto(byPath('missing.md'));await page.getByRole('heading',{name:'File not found'}).waitFor();
    await shot('desktop-missing');
    await page.goto(byPath('escape.txt'));await page.getByRole('heading',{name:'This file cannot be opened'}).waitFor();
    assert.doesNotMatch(await page.locator('body').textContent(),/outside the root|fv-outside/);
    await page.goto(byPath('folder'));await page.getByRole('heading',{name:'This is not a file'}).waitFor();
    await page.goto(view('path='+enc('../../../../etc/passwd')));await page.locator('.fv-error').waitFor();
    assert.doesNotMatch(await page.locator('body').textContent(),/root:/);
    await page.goto(view('path=widget%2Ffiles%2F%252e%252e%2Freport.md'));await page.locator('.fv-error').waitFor();
    await page.goto(`${origin}/files/view`);await page.locator('.fv-error').waitFor();
    await page.goto(byPath('big.log'));await page.locator('pre.fv-text').waitFor();
    assert.match(await page.locator('.fv-banner.warn').textContent(),/Showing the first 2\.0 MiB of \d/);
    const bigSize=fs.statSync(path.join(d.root,'widget/files/big.log')).size;
    const [big]=await Promise.all([page.waitForEvent('download'),page.locator('.fv-banner').getByRole('link',{name:'Download',exact:true}).click()]);
    assert.equal(fs.statSync(await big.path()).size,bigSize);
    await shot('desktop-too-large');

    // --- Managed note: stable link, real saved versions, paging, compare, historical view, back to current, downloads.
    const stable=`/files/view?document_id=${d.history_id}&business_id=${d.business_id}`;
    await page.goto(view('path='+enc(d.history_path)));await page.locator('.fv-md').waitFor();
    assert.equal(await page.locator('.fv-chips').count(),0);
    assert.equal(await page.locator('.fv-chips').count(),0);
    assert.match(await page.locator('.fv-md').textContent(),/Revision 25/);
    await page.getByRole('button',{name:'Copy link',exact:true}).click();
    assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),origin+stable);
    await page.getByRole('button',{name:'History',exact:true}).click();
    await page.locator('.fv-history li').first().waitFor();
    assert.equal(await page.locator('.fv-history li').count(),20);
    assert.match(await page.locator('.fv-history li').first().textContent(),/v25.*Owner.*Latest saved version/);
    await page.getByRole('button',{name:'Older versions'}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.fv-history li').length===25);
    assert.equal(await page.getByRole('button',{name:'Older versions'}).count(),0);
    // Only two versions stay selected for a comparison, and the diff is escaped text.
    for(const n of [3,10,25])await page.getByLabel(`Select version ${n} to compare`).check();
    assert.equal(await page.locator('.fv-history input:checked').count(),2);
    assert.equal(await page.getByLabel('Select version 3 to compare').isChecked(),false);
    await page.getByLabel('Select version 3 to compare').check();
    await page.getByRole('button',{name:'Compare selected'}).click();
    await page.getByRole('heading',{name:'Changes from version 3 to version 25'}).waitFor();
    assert.match(await page.locator('.fv-diff .add').first().textContent(),/^\+/);
    assert.match(await page.locator('.fv-diff .del').first().textContent(),/^-/);
    assert.match(await page.locator('.fv-diff').textContent(),/Revision 25/);
    assert.doesNotMatch(await page.locator('.fv-history').textContent(),/null|business:/);
    await shot('desktop-history-diff');
    await page.locator('.fv-history li',{hasText:'v3 '}).getByRole('link',{name:'View'}).click();
    await page.waitForURL(`${origin}${stable}&version=3`);
    await page.locator('.fv-md').waitFor();
    assert.equal(await page.locator('.fv-chips').count(),0);
    assert.match(await page.locator('.fv-md').textContent(),/Revision 3\b/);
    assert.doesNotMatch(await page.locator('.fv-md').textContent(),/Revision 25/);
    assert.equal(await page.locator('.fv-banner.info a',{hasText:'Back to current'}).count(),1);
    await shot('desktop-historical-version');
    await page.getByRole('button',{name:'Copy version link'}).click();
    assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),`${origin}${stable}&version=3`);
    const [saved]=await Promise.all([page.waitForEvent('download'),page.getByRole('link',{name:'Download',exact:true}).click()]);
    assert.match(saved.suggestedFilename(),/\.v3\.md$/);
    assert.match(fs.readFileSync(await saved.path(),'utf8'),/Revision 3\n/);
    await page.getByRole('link',{name:'Back to current'}).click();
    await page.waitForURL(`${origin}${stable}`);await page.locator('.fv-md').waitFor();
    assert.equal(await page.locator('.fv-chips').count(),0);
    assert.match(await page.locator('.fv-md').textContent(),/Revision 25/);
    // A deep link to a saved version opens directly; a missing version is an honest error.
    await page.goto(`${origin}${stable}&version=2`);await page.locator('.fv-md').waitFor();
    assert.equal(await page.locator('.fv-chips').count(),0);
    await page.goto(`${origin}${stable}&version=99`);await page.getByRole('heading',{name:'File not found'}).waitFor();
    // Edits made outside Qorqut are labelled, never passed off as saved history.
    await page.goto(view('path='+enc(d.drifted_path)));await page.locator('.fv-md').waitFor();
    assert.equal(await page.locator('.fv-chips').count(),0);
    assert.match(await page.locator('.fv-md').textContent(),/Edited outside Qorqut/);

    // --- Copy link falls back when the clipboard API is unavailable (the dashboard is plain HTTP on a private network).
    const plain=await context.newPage();
    await plain.addInitScript(()=>Object.defineProperty(navigator,'clipboard',{value:undefined,configurable:true}));
    await plain.goto(byPath('notes.txt'));await plain.locator('pre.fv-text').waitFor();
    await plain.getByRole('button',{name:'Copy link',exact:true}).click();
    const outcome=await plain.locator('#fv-toast').textContent();
    assert.ok(['Link copied','Press Ctrl+C to copy'].includes(outcome),outcome);
    if(outcome!=='Link copied')assert.equal(await plain.locator('.fv-manual').inputValue(),`${origin}/files/view?path=widget%2Ffiles%2Fnotes.txt`);
    await plain.close();

    // --- Keyboard: the header controls are reachable in order and show a focus ring.
    await page.goto(byPath('notes.txt'));await page.locator('pre.fv-text').waitFor();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('aria-label')),'Copy link');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('aria-label')),'Copy text');

    // --- Narrow layout: no page-level horizontal scrolling; wide tables scroll inside their own region.
    await page.setViewportSize({width:390,height:844});
    for(const [name,url] of [['markdown',byPath('report.md')],['csv',byPath('table.csv')],['text',byPath('notes.txt')],['history',view('path='+enc(d.history_path))]]){
      await page.goto(url);await page.locator('.fv-md, .fv-csv, pre.fv-text').first().waitFor();
      if(name==='history'){await page.getByRole('button',{name:'History',exact:true}).click();await page.locator('.fv-history li').first().waitFor();}
      const overflow=await page.evaluate(()=>document.documentElement.scrollWidth-window.innerWidth);
      assert.ok(overflow<=1,`${name} overflows the narrow viewport by ${overflow}px`);
      await shot('narrow-'+name);
    }
    await page.setViewportSize({width:1280,height:900});
    // The page follows the system colour scheme; the dark theme is checked by eye in this screenshot.
    await page.emulateMedia({colorScheme:'dark'});
    await page.goto(view('path='+enc(d.history_path)));await page.locator('.fv-md').waitFor();
    await page.getByRole('button',{name:'History',exact:true}).click();await page.locator('.fv-history li').first().waitFor();
    await shot('desktop-dark-history');
    await page.emulateMedia({colorScheme:'light'});

    // --- Ordinary links in the dashboard's own surfaces: new tab, no side effects, no markup from text.
    await page.goto(origin+'/');await page.waitForFunction(()=>typeof selectBiz==='function'&&typeof tFetchTask==='function'&&state.bizs.length>0);
    await page.evaluate(slug=>selectBiz(slug),d.business_slug);await page.waitForFunction(slug=>state.biz===slug,d.business_slug);
    await page.evaluate(async({slug,id})=>{const task=await tFetchTask(slug,id);tDetail(task);},{slug:d.business_slug,id:d.task_id});
    const bubble=page.locator('#td-progress .conversation-bubble',{hasText:'Second line.'});await bubble.waitFor();
    assert.match(await bubble.locator('summary').textContent(),/^Read the brief or https:\/\/example\.com\/page, but not/,'the preview shows link labels, not Markdown');
    await bubble.locator('summary').click();
    assert.equal(await bubble.locator('details').evaluate(node=>node.open),true);
    assert.equal(await bubble.locator('a.safe-link').count(),2);
    assert.equal(await bubble.locator('b, img, a[href^="javascript"]').count(),0);
    assert.match(await bubble.textContent(),/<b>markup<\/b>/);
    assert.equal(await page.evaluate(()=>window.__prose),undefined);
    const link=bubble.locator('a.safe-link',{hasText:'the brief'});
    assert.equal(await link.getAttribute('target'),'_blank');assert.equal(await link.getAttribute('rel'),'noopener noreferrer');
    assert.equal(await link.getAttribute('href'),'/files/view?path=widget%2Ffiles%2Freport.md');
    await link.scrollIntoViewIfNeeded();
    await shot('dashboard-task-links');
    await page.waitForTimeout(800);const postsBefore=posts.length;
    const [taskTab]=await Promise.all([context.waitForEvent('page'),link.click()]);
    await taskTab.waitForLoadState();await taskTab.locator('.fv-md').waitFor();
    assert.equal(taskTab.url(),`${origin}/files/view?path=widget%2Ffiles%2Freport.md`);
    await taskTab.close();
    assert.equal(await bubble.locator('details').evaluate(node=>node.open),true,'clicking a link must not collapse the message');
    const [external]=await Promise.all([context.waitForEvent('page'),bubble.locator('a.safe-link',{hasText:'example.com'}).click()]);
    await external.waitForLoadState();
    assert.equal(external.url(),'https://example.com/page');
    assert.equal(await external.evaluate(()=>window.opener),null,'noopener');
    await external.close();
    await page.waitForTimeout(500);
    assert.equal(posts.length,postsBefore,'clicking a link sent a request that changes something');
    assert.equal(await page.locator('#td-progress').count(),1,'the task detail stayed open');
    await page.locator('button.ghost',{hasText:'Close'}).click();await page.locator('.overlay').waitFor({state:'detached'});

    // Story: the description link does not open its editor; the timeline note link opens a tab.
    await page.evaluate(async()=>{if(state.tab!=='context')await goTab('context');await ctxGo('_stories');});
    await page.locator('#story-home').waitFor({state:'visible'});
    if(await page.locator('#story-show-closed').count())await page.locator('#story-show-closed').check().catch(()=>{});
    await page.locator(`#story-list .jrcard[data-id="${d.story_id}"]`).click();
    await page.locator('#story-description a.safe-link').first().waitFor();
    assert.equal(await page.locator('#story-description a.safe-link').count(),2);
    assert.equal(await page.locator('#story-description b, #story-description img').count(),0);
    await page.locator('#story-timeline .story-event-body a.safe-link').first().waitFor({state:'attached'});
    await page.waitForTimeout(1000);const storyBefore=posts.length;
    const story=page.locator('#story-description a.safe-link',{hasText:'the brief'});
    const [storyTab]=await Promise.all([context.waitForEvent('page'),story.click()]);
    await storyTab.waitForLoadState();
    assert.equal(storyTab.url(),`${origin}/files/view?path=widget%2Ffiles%2Freport.md`);await storyTab.close();
    assert.equal(await page.locator('#story-description .story-description-editor').isHidden(),true,'the description editor must stay closed');
    await story.focus();
    const [keyTab]=await Promise.all([context.waitForEvent('page'),page.keyboard.press('Enter')]);
    await keyTab.waitForLoadState();await keyTab.close();
    assert.equal(await page.locator('#story-description .story-description-editor').isHidden(),true,'Enter on a link must not open the editor');
    const note=page.locator('#story-timeline .story-event-body a.safe-link',{hasText:'the brief'}).filter({visible:true}).first();
    await note.scrollIntoViewIfNeeded();await note.waitFor();
    const [noteTab]=await Promise.all([context.waitForEvent('page'),note.click()]);
    await noteTab.waitForLoadState();
    assert.equal(noteTab.url(),`${origin}/files/view?path=widget%2Ffiles%2Freport.md`);await noteTab.close();
    await shot('dashboard-story-links');
    assert.equal(posts.length,storyBefore,'story link clicks sent a request');

    // --- Framing: only the exact reader shell may sit in a same-origin frame (the dashboard's Viewbox).
    const fetched=async(route)=>(await context.request.get(origin+route,{maxRedirects:0})).headers();
    const shellHeaders=await fetched('/files/view?path='+enc('widget/files/report.md'));
    assert.equal(shellHeaders['x-frame-options'],'SAMEORIGIN');
    assert.match(shellHeaders['content-security-policy'],/frame-ancestors 'self'/);
    for(const route of['/','/api/root/files/metadata?path='+enc('widget/files/report.md'),'/api/root/files/download?path='+enc('widget/files/report.md'),'/files/view/']){
      const headers=await fetched(route);
      assert.equal(headers['x-frame-options'],'DENY',route);assert.match(headers['content-security-policy'],/frame-ancestors 'none'/,route);
    }
    await context.route(origin+'/__host',route=>route.fulfill({contentType:'text/html',body:'<!doctype html><title>host</title><body>same-origin host</body>'}));
    const embed=(host,src,{name='viewbox',id='viewbox',style='width:900px;height:600px'}={})=>host.evaluate(([url,frameName,frameId,css])=>{
      const frame=document.createElement('iframe');frame.id=frameId;frame.name=frameName;frame.style.cssText=css;
      frame.src=url;document.body.append(frame);},[src,name,id,style]);
    const hostPage=async(url)=>{
      const host=await context.newPage(), seen=[];
      host.on('request',request=>seen.push(request.url()));
      await host.addInitScript(()=>{window.__hostMessages=[];window.addEventListener('message',event=>window.__hostMessages.push(String(event.data)));});
      await host.goto(url);
      return {host,seen};
    };

    // The real dashboard page embeds the reader: Markdown, and an HTML preview nested inside it.
    {
      const {host}=await hostPage(origin+'/');
      const pane=(left,width)=>`position:fixed;top:20px;left:${left}px;width:${width}px;height:860px;z-index:99999;background:#fff;border:1px solid #888`;
      await embed(host,byPath('report.md'),{style:pane(20,620)});
      const shell=host.frameLocator('#viewbox');
      await shell.locator('.fv-md h1').waitFor();
      assert.equal(await shell.locator('.fv-md h1').textContent(),'Synthetic report');
      assert.equal(await shell.locator('.fv-title h1').textContent(),'report.md');
      assert.equal(await shell.locator('.fv-md script, .fv-md iframe').count(),0);
      await embed(host,byPath('page.html'),{name:'nested',id:'nested',style:pane(660,600)});
      const nested=host.frameLocator('#nested');
      await nested.locator('iframe.fv-frame').waitFor();
      assert.equal(await nested.locator('iframe.fv-frame').getAttribute('sandbox'),'');
      const preview=nested.frameLocator('iframe.fv-frame');
      await preview.locator('#heading').waitFor();
      assert.equal(await preview.locator('#heading').textContent(),'Synthetic HTML','the script must not run in the nested preview');
      // The other dashboard paths still refuse to be framed, even by the same origin.
      await embed(host,'/',{name:'dash',id:'dash',style:'display:none'});
      await embed(host,'/api/root/files/metadata?path='+enc('widget/files/report.md'),{name:'api',id:'api',style:'display:none'});
      await host.waitForTimeout(800);
      const framed=host.frames().map(frame=>frame.url());
      assert.equal(framed.filter(url=>url.startsWith('chrome-error:')).length,2,'a denied path must not render in a frame: '+framed.join(' '));
      await host.screenshot({path:path.join(SHOTS,'framed-dashboard-reader.png')});
      await host.close();
    }

    // Hostile HTML inside the framed reader: no script, explicit self/top/parent targets, meta refresh or form reaches anything.
    // Each attempt gets a fresh same-origin parent, because the one allowed outcome (below) replaces the preview frame.
    const attempts=['load','self-link','top-link','parent-link','blank-link','named-link','plain-link','self-submit'];
    for(const attempt of attempts){
      const {host,seen}=await hostPage(origin+'/__host');
      const tabs=context.pages().length, label=attempt==='load'?'after load and the meta refresh':'after clicking #'+attempt;
      await embed(host,byPath('evil.html'));
      const shell=host.frameLocator('#viewbox'), inner=shell.frameLocator('iframe.fv-frame');
      await inner.locator('#heading').waitFor();
      assert.equal(await inner.locator('#heading').textContent(),'Hostile HTML',attempt+': the script must not run');
      await host.waitForTimeout(2500); // Past the one-second meta refresh.
      if(attempt!=='load'){
        await inner.locator('#'+attempt).click({noWaitAfter:true,timeout:3000});
        await host.waitForTimeout(800);
      }
      const urls=host.frames().map(frame=>frame.url()), preview=host.frames()[2];
      // Never a request to the owner API or the network, from any frame, and the parent is untouched.
      assert.deepEqual(seen.filter(url=>url.includes('report.md')||url.includes('/ping')),[],label+': the page reached the owner API or the network');
      assert.equal(host.url(),origin+'/__host',label);assert.equal(await host.title(),'host',label);
      assert.equal(await host.evaluate(()=>window.__evil),undefined,label);
      assert.deepEqual(await host.evaluate(()=>window.__hostMessages),[],label);
      assert.equal(await host.locator('body').evaluate(node=>node.children.length),1,label+': the parent was changed');
      assert.equal(context.pages().length,tabs,label+': a window opened');
      assert.equal(await shell.locator('.fv-title h1').textContent(),'evil.html',label+': the reader itself was navigated');
      assert.equal(host.frames().length,3,label+': unexpected frames: '+urls.join(' '));
      assert.ok(!urls.some(url=>url.includes('/api/')||url.includes('report.md')),label+': a frame reached the owner API: '+urls.join(' '));
      if(attempt==='self-link'){
        // The explicit _self link is the one attempt that acts: the browser blocks the navigation (frame-src 'none') and
        // shows its own blocked-content page in the preview. Nothing was requested, and Source/Download remain.
        assert.ok(urls[2].startsWith('chrome-error:')||['','about:srcdoc'].includes(urls[2]),label+': '+urls.join(' '));
      } else {
        assert.ok(['','about:srcdoc'].includes(urls[2]),label+': the preview frame navigated away: '+urls.join(' '));
        assert.equal(await inner.locator('#heading').textContent(),'Hostile HTML',label);
      }
      assert.ok(!(await preview.locator('body').textContent({timeout:1000}).catch(()=>'')).includes('Synthetic report'),label+': the owner file was shown');
      if(attempt==='load')await host.screenshot({path:path.join(SHOTS,'framed-hostile-html.png')});
      await host.close();
    }

    // A parent from another origin cannot embed the reader (or any owner route).
    {
      const foreign=http.createServer((request,response)=>{
        response.setHeader('content-type','text/html');
        response.end(`<!doctype html><title>cross</title><iframe id="f" src="${origin}/files/view?path=${enc('widget/files/report.md')}"></iframe>`
          +`<iframe id="g" src="${origin}/api/root/files/metadata?path=${enc('widget/files/report.md')}"></iframe>`);
      });
      await new Promise(resolve=>foreign.listen(0,'127.0.0.1',resolve));
      const foreignOrigin='http://127.0.0.1:'+foreign.address().port;
      assert.notEqual(foreignOrigin,origin);
      const cross=await context.newPage(), consoleLines=[], shellResponses=[];
      cross.on('console',message=>consoleLines.push(message.text()));
      cross.on('response',response=>{if(response.url().startsWith(origin+'/files/view'))shellResponses.push(response.headers());});
      await cross.goto(foreignOrigin+'/');
      await cross.waitForTimeout(1500);
      const frames=cross.frames().map(frame=>frame.url());
      assert.ok(!frames.some(url=>url.startsWith(origin+'/files/view')),'the cross-origin parent embedded the reader: '+frames.join(' '));
      assert.equal(frames.filter(url=>url.startsWith('chrome-error:')).length,2,'the embeds were not refused: '+frames.join(' '));
      for(const directive of["frame-ancestors 'self'","frame-ancestors 'none'"])
        assert.ok(consoleLines.some(line=>line.includes('directive: "'+directive+'"')),'no refusal by '+directive+': '+consoleLines.join(' | '));
      assert.ok(shellResponses.every(headers=>headers['x-frame-options']==='SAMEORIGIN'));
      await cross.close();
      await new Promise(resolve=>foreign.close(resolve));
    }

    assert.deepEqual(errors,[]);
    console.log('file viewer browser checks passed; screenshots in '+SHOTS);
  } finally {
    if(browser)await browser.close();
    await fixture.stop();
  }
})().catch(error=>{console.error(error);process.exit(1);});
