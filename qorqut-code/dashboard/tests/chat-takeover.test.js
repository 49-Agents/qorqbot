'use strict';
// A side chat (and a paired thread) takes over the Chat area under a small header; "Chat" hands the area back.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname, '..');

async function fixture(browser) {
  const page = await browser.newPage({viewport: {width: 1000, height: 800}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setContent('<body><main id="pane" style="display:flex;flex-direction:column;height:700px;width:800px"></main></body>');
  for (const file of ['app.css', 'css/chat-view.css', 'css/global-chat.css']) await page.addStyleTag({path: path.join(root, file)});
  await page.evaluate(() => {
    window.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    window.$ = (selector, host = document) => host.querySelector(selector);
    window.identityAvatar = () => '';
    window.state = {project: 'demo'};
    window.coreData = value => value;
    window.evUrl = value => value;
    window.toast = () => {};
    window.EventSource = class extends EventTarget { static CLOSED = 2; close() {} };
    const binding = {provider: 'codex', conversation_id: 'conversation-1', binding_id: 'binding-1'};
    const entries = [{id: 'entry-1', position: 1, speaker: 'agent', text: 'Main chat message', completion_sequence: 1}];
    window.api = async () => ({session: {state: 'live', can_send: true, binding}, conversation: binding, entries: [...entries], cursor: 1, inputs: []});
    window.post = async (url, body) => ({last_read_sequence: body.sequence});
  });
  for (const file of ['safe-links', 'chat-view', 'slash-commands', 'agent-runtime', 'global-chat']) await page.addScriptTag({path: path.join(root, 'js', file + '.js')});
  await page.evaluate(() => {
    window.controller = mountAgentView(document.querySelector('#pane'), {agent_id: 'agent-1', name: 'Demo'}, {isCurrent: () => true});
    // A side chat stand-in: it may refuse to leave, like one with a message still sending.
    window.side = {allowLeave: true, disposed: 0};
    window.openSide = title => controller.openSide({title, mount(host, session) {
      host.innerHTML = `<p class="side-body">${title} body</p>`;
      session.setBeforeLeave(() => side.allowLeave);
      return () => { side.disposed++; };
    }});
  });
  await page.locator('.gchat-msg').first().waitFor();
  return {page, errors};
}
const takeover = page => page.evaluate(() => ({
  kind: document.querySelector('.gchat-takeover')?.dataset.takeover || null,
  title: document.querySelector('.gchat-takeover-title')?.textContent || '',
  body: document.querySelector('.side-body')?.textContent || '',
  chat: !!document.querySelector('.gchat')?.isConnected,
}));

test('a side chat takes over the Chat area and "Chat" brings the main chat back with its draft', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser);
    await page.locator('.gchat-composer textarea').fill('Unsent main draft');
    await page.evaluate(() => openSide('Pricing page'));
    assert.deepEqual(await takeover(page), {kind: 'side', title: 'Pricing page', body: 'Pricing page body', chat: false});
    await page.click('.gchat-takeover-back');
    await page.locator('.gchat-msg').first().waitFor();
    assert.deepEqual(await takeover(page), {kind: null, title: '', body: '', chat: true});
    assert.equal(await page.locator('.gchat-composer textarea').inputValue(), 'Unsent main draft');
    assert.equal(await page.evaluate(() => side.disposed), 1);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('a side chat that cannot leave yet stays put, and another one replaces it once it can', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser);
    await page.evaluate(() => openSide('First'));
    await page.evaluate(() => { side.allowLeave = false; });
    assert.equal(await page.evaluate(() => openSide('Second')), null);
    await page.click('.gchat-takeover-back');
    assert.deepEqual(await takeover(page), {kind: 'side', title: 'First', body: 'First body', chat: false});
    await page.evaluate(() => { side.allowLeave = true; });
    assert.notEqual(await page.evaluate(() => openSide('Second').then(Boolean)), false);
    assert.deepEqual(await takeover(page), {kind: 'side', title: 'Second', body: 'Second body', chat: false});
    assert.equal(await page.evaluate(() => side.disposed), 1);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('project details and pinned Chat threads open and close independently', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser);
    await page.addScriptTag({path: path.join(root, 'js/record-workspace.js')});
    await page.addScriptTag({path: path.join(root, 'js/thread-pins.js')});
    await page.evaluate(() => {
      state.projects=[{id:'demo',slug:'demo'}];window.taskTypeIcon=()=>'';window.T_VIEW_ICON={tasks:'',signoffs:''};
      window.threadState={allow:true,disposed:0};
      window.conversationMount=(host,source)=>{host.textContent='Thread '+source.id;return {canLeave:()=>threadState.allow,expand(){},dispose(){threadState.disposed++;host.replaceChildren();}};};
      const originalPost=post;let pins=[];
      window.post=async(url,body)=>{
        if(url==='/root/thread-pins'){pins=body.pinned?[{source:body.source,title:body.title}]:[];return {threads:pins};}
        if(url.endsWith('/project_record_get'))return {project:{id:body.project_id,name:'Demo',summary:'Synthetic project'}};
        if(url.endsWith('/project_items_list'))return {items:[]};
        return originalPost(url,body);
      };
      const originalApi=api;window.api=async url=>url==='/root/thread-pins'?{threads:pins}:originalApi(url);
      const host=document.createElement('aside');host.id='project';document.body.append(host);
      window._agpad={focusMount:host,workspaceRole:'ordubashy'};window.agfocusLeftChatReady=()=>true;let generation=0;
      window.agfocusPairedSource=record=>record?controller.selectPairedSource({...record.source,label:record.label},++generation,()=>{},()=>{},()=>controller.clearPairedSource(++generation)):controller.clearPairedSource(++generation);
      window.workspace=recordWorkspaceMount(host,{entry:'focus',paired:true,independentChat:true,onPairedSource:agfocusPairedSource});
      window.record=rwRecordFromItem({source:{type:'project',id:'demo',project_id:'demo',label:'Demo'},can_thread:true});
      window.openProject=()=>workspace.open(record,{root:true});
    });
    await page.evaluate(()=>openProject());
    assert.equal(await page.locator('.gchat-paired-record').count(),0);
    await page.click('.rw-open-thread');
    await page.locator('.gchat-paired-record').waitFor();
    await page.click('.gchat-thread-pin');
    assert.equal(await page.evaluate(()=>threadPins.list().length),1);
    await page.click('.rw-close');
    assert.equal(await page.locator('.rw-record').count(),0);
    assert.equal(await page.locator('.gchat-paired-record').textContent(),'Thread demo');
    assert.equal(await page.evaluate(()=>threadState.disposed),0);
    await page.evaluate(()=>{threadState.allow=false});
    await page.click('.gchat-takeover-back');
    assert.equal(await page.locator('.gchat-paired-record').count(),1);
    await page.evaluate(()=>{threadState.allow=true});
    await page.click('.gchat-takeover-back');
    await page.locator('.gchat-msg').first().waitFor();
    await page.evaluate(()=>threadPins.open(threadPins.list()[0]));
    assert.equal(await page.locator('.rw-record').count(),0);
    assert.equal(await page.locator('.gchat-paired-record').textContent(),'Thread demo');
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

test('a Project opens on its timeline, and the thread button shows and hides its thread in the Chat area', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser);
    for (const file of ['css/record-workspace.css', 'css/timeline-panel.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.evaluate(() => { window.taskTypeIcon=()=>'';window.T_VIEW_ICON={tasks:'',signoffs:'',timeline:'<svg data-icon="timeline"></svg>',files:'<svg data-icon="folder"></svg>'}; });
    await page.addScriptTag({path: path.join(root, 'js/timeline-panel.js')});
    await page.addScriptTag({path: path.join(root, 'js/record-workspace.js')});
    await page.evaluate(() => {
      state.projects=[{id:'demo',slug:'demo'}];window.reads=[];
      window.conversationMount=(host,source)=>{host.textContent='Thread '+source.id;return {canLeave:()=>true,expand(){},dispose(){host.replaceChildren();}};};
      const originalPost=post;
      window.post=async(url,body)=>{
        if(url.endsWith('/timeline_view')){reads.push(body.item);return {item:body.item,title:'Demo',revision:2,further:{},
          state:{goal:{id:'g',text:'Ship the demo.'},decisions:[],facts:[],progress:null,questions:[]}};}
        if(url.endsWith('/project_record_get'))return {project:{id:body.project_id,name:'Demo',summary:'Synthetic project'}};
        if(url.endsWith('/project_items_list'))return {items:[]};
        return originalPost(url,body);
      };
      const host=document.createElement('aside');host.id='project';host.style.cssText='display:flex;flex-direction:column;height:400px;width:420px';document.body.append(host);
      window._agpad={focusMount:host,workspaceRole:'ordubashy'};let generation=0;
      window.agfocusPairedSource=record=>record?controller.selectPairedSource({...record.source,label:record.label},++generation,()=>{},()=>{},()=>controller.clearPairedSource(++generation)):controller.clearPairedSource(++generation);
      window.workspace=recordWorkspaceMount(host,{entry:'focus',paired:true,independentChat:true,onPairedSource:agfocusPairedSource});
      window.record=rwRecordFromItem({source:{type:'project',id:'demo',project_id:'demo',label:'Demo'},can_thread:true});
    });
    await page.evaluate(()=>workspace.open(record,{root:true}));
    // The reader opens on the timeline; its tabs are the timeline and the Project page (a folder), and the thread is not a tab.
    await page.locator('.rw-timeline .tlp-block').first().waitFor();
    assert.equal(await page.locator('.rw-record').getAttribute('data-tab'),'timeline');
    assert.deepEqual(await page.evaluate(()=>reads),[{type:'project',id:'demo',project_id:'demo'}]);
    assert.match(await page.locator('.rw-timeline').textContent(),/Ship the demo\./);
    assert.equal(/holds now/i.test(await page.locator('.rw-record').textContent()),false);
    assert.deepEqual(await page.evaluate(()=>[...document.querySelectorAll('.rw-tabs [data-rw-tab]')].map(tab=>[tab.dataset.rwTab,tab.querySelector('svg')?.dataset.icon,tab.getAttribute('aria-selected')])),
      [['timeline','timeline','true'],['dashboard','folder','false']]);
    assert.equal(await page.locator('.rw-dashboard').isVisible(),false);
    // The thread button is a toggle: it shows the thread on the left and takes it away again.
    const thread=page.locator('.rw-open-thread');
    assert.equal(await thread.getAttribute('aria-pressed'),'false');assert.equal(await thread.getAttribute('aria-label'),'Show thread');
    await thread.click();
    await page.locator('.gchat-paired-record').waitFor();
    assert.equal(await page.locator('.gchat-paired-record').textContent(),'Thread demo');
    assert.equal(await thread.getAttribute('aria-pressed'),'true');assert.equal(await thread.getAttribute('aria-label'),'Hide thread');
    assert.equal(await page.locator('.rw-record').getAttribute('data-tab'),'timeline');
    await thread.click();
    await page.locator('.gchat-msg').first().waitFor();
    assert.equal(await page.locator('.gchat-paired-record').count(),0);
    assert.equal(await thread.getAttribute('aria-pressed'),'false');
    // Leaving the thread from the Chat area releases the button too.
    await thread.click();
    await page.locator('.gchat-paired-record').waitFor();
    await page.click('.gchat-takeover-back');
    await page.locator('.gchat-msg').first().waitFor();
    assert.equal(await thread.getAttribute('aria-pressed'),'false');
    // The Project page is one click away and loads then.
    await page.click('.rw-tabs [data-rw-tab="dashboard"]');
    assert.equal(await page.locator('.rw-dashboard').isVisible(),true);
    assert.equal(await page.locator('.rw-timeline').isVisible(),false);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

test('Chat leaves a thread whose Focus view has gone, but not while it is busy', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser);
    await page.evaluate(() => {
      window.threadState={allow:true,toasts:[]};window.toast=message=>threadState.toasts.push(message);
      window.conversationMount=(host,source)=>{host.textContent='Thread '+source.id;return {canLeave:()=>threadState.allow,expand(){},dispose(){host.replaceChildren();}};};
      // The Focus callback refuses once Focus has gone, as it does after the pane left Focus.
      void controller.selectPairedSource({type:'project',id:'demo',label:'Demo'},1,()=>{},null,()=>false);
    });
    await page.locator('.gchat-paired-record').waitFor();
    await page.evaluate(()=>{threadState.allow=false});
    await page.click('.gchat-takeover-back');
    assert.equal(await page.locator('.gchat-paired-record').count(),1);
    assert.match((await page.evaluate(()=>threadState.toasts)).join('\n'),/before leaving this thread/);
    assert.equal(await page.evaluate(()=>controller.pairedCanLeave()),false);
    await page.evaluate(()=>{threadState.allow=true});
    await page.click('.gchat-takeover-back');
    await page.locator('.gchat-msg').first().waitFor();
    assert.equal(await page.locator('.gchat-paired-record').count(),0);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

test('a report opens and pins the thread of the Task it belongs to', {skip: !available}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await fixture(browser);
    await page.addScriptTag({path: path.join(root, 'js/record-workspace.js')});
    await page.addScriptTag({path: path.join(root, 'js/thread-pins.js')});
    await page.evaluate(() => {
      state.projects=[{id:'demo',slug:'demo'}];window.taskTypeIcon=()=>'';window.T_VIEW_ICON={tasks:'',signoffs:''};
      window.conversationMount=(host,source)=>{host.textContent='Thread '+source.type+' '+source.id;return {canLeave:()=>true,expand(){},dispose(){host.replaceChildren();}};};
      const originalPost=post;let pins=[];
      window.post=async(url,body)=>{
        if(url==='/root/thread-pins'){pins=body.pinned?[...pins.filter(p=>p.source.id!==body.source.id),{source:body.source,title:body.title}]:pins.filter(p=>p.source.id!==body.source.id);return {threads:pins};}
        return originalPost(url,body);
      };
      const originalApi=api;window.api=async url=>url==='/root/thread-pins'?{threads:pins}:originalApi(url);
      // The report view announces its Task once the Signoff has loaded; 'old' is a report from before Tasks were required.
      window.rwDashboardMount=async(host,record,ctx)=>{host.textContent='Report '+record.source.id;
        ctx.report(record.source.id==='old'?null:{id:'t1',project_id:'demo',title:'Launch'});return {dirty:()=>false};};
      const host=document.createElement('aside');host.id='project';document.body.append(host);
      window._agpad={focusMount:host,workspaceRole:'ordubashy'};window.agfocusLeftChatReady=()=>true;let generation=0;
      window.agfocusPairedSource=record=>record?controller.selectPairedSource({...record.source,label:record.label},++generation,()=>{},()=>{},()=>controller.clearPairedSource(++generation)):controller.clearPairedSource(++generation);
      window.workspace=recordWorkspaceMount(host,{entry:'focus',paired:true,independentChat:true,onPairedSource:agfocusPairedSource});
      window.report=id=>rwRecordFromItem({source:{type:'signoff',id,project_id:'demo',label:'Launch report'},can_thread:true});
    });
    await page.evaluate(()=>workspace.open(report('r1'),{root:true}));
    await page.locator('.gchat-paired-record').waitFor();
    assert.equal(await page.locator('.gchat-paired-record').textContent(),'Thread task t1');
    assert.equal(await page.locator('.rw-record [data-rw-tab="chat"]').count(),0);
    assert.equal(await page.locator('.rw-open-thread').isVisible(),true);
    await page.click('.rw-pin');
    assert.deepEqual(await page.evaluate(()=>threadPins.list().map(p=>p.source)),[{type:'task',id:'t1',project_id:'demo'}]);
    assert.equal(await page.locator('.rw-pin').getAttribute('aria-pressed'),'true');
    await page.click('.gchat-takeover-back');
    await page.locator('.gchat-msg').first().waitFor();
    await page.click('.rw-open-thread');
    await page.locator('.gchat-paired-record').waitFor();
    assert.equal(await page.locator('.gchat-paired-record').textContent(),'Thread task t1');
    // A report with no Task has no thread to open or pin.
    await page.evaluate(()=>workspace.open(report('old'),{root:true}));
    await page.waitForFunction(()=>document.querySelector('.rw-record .rw-dashboard')?.textContent==='Report old');
    assert.equal(await page.locator('.rw-open-thread').isVisible(),false);
    assert.equal(await page.locator('.rw-pin').isVisible(),false);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});
