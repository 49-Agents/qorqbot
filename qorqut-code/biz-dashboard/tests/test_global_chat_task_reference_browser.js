#!/usr/bin/env node
// Real native Chat (global-chat.js) with the real agent-pane Focus shell and the shared record workspace over a
// synthetic HTTP fixture: a resolved Task reference in native Chat opens the paired private record thread and details,
// preserves canonical scope without inventing a CRM parent, works from the keyboard, and has stable hover styling.
// Unavailable references stay inert, flat record-thread rows keep their click and get the same hover outline.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const core = fs.readFileSync(path.join(root, 'js/core.js'), 'utf8');
const identity = core.split('const TABS =')[0];
const lateCore = core.split('\n').filter(line => /^const (accessLost|sourceGone) = /.test(line)).join('\n');
const icons = core.split('\n').filter(line => /^const (ICO_|SVG_)\w+ = `/.test(line)).join('\n');

const calls = [], errors = [];
let origin;

const CHAT = {
  agent_id: 'ordu', name: 'Ordubashy', revision: 10, cursor: 10,
  session: {state: 'live', reason: 'live', can_send: true, working: false, provider: 'claude', conversation_id: 'conv-1', transport: 'terminal', detail: 'Live in Terminal',
    binding: {binding_id: 'b1', agent_id: 'ordu', provider: 'claude', conversation_id: 'conv-1', generation: 1, transport: 'terminal'}},
  conversation: {conversation_id: 'conv-1', provider: 'claude', complete: true, finalized: false, error: null, captured_length: 2},
  conversations: [{conversation_id: 'conv-1', provider: 'claude', updated_at: '2026-10-28T20:00:00Z', current: true}],
  entries: [
    {id: 'id1', kind: 'message', speaker: 'agent', text: 'The Events reference was updated.', timestamp: null, position: 100,
     partial: false, identity: 'native_id', completion_sequence: 1, completion_id: 'c1', system: null,
     references: [
       {type: 'task', id: 'task-events', business_id: 'biz-49', business_slug: 'proj49', label: 'Events', task_type: 'story', state: 'working', available: true},
       {type: 'task', id: 'task-hidden', business_id: 'biz-49', business_slug: 'proj49', label: 'Sealed plan', available: false},
     ]},
  ],
  inputs: [], actions: [], older: null, rendered_boundary: 1, last_read_sequence: 0, newest_sequence: 1,
};
const RELATED = {type: 'task', id: 'task-related', business_id: 'biz-49', business_slug: 'proj49', label: 'Related follow-up', state: 'open', created_at: '2026-10-28T20:01:00Z'};
const focusBoard = () => ({status: 'ok', data: {revision: 1, status: 'Working', updated_at: '2026-10-28T20:00:00Z', stale: false,
  items: [], next_offset: null, activity: null, desktop_control_url: null, browser: {state: 'idle'}}});
const json = (res, value, status = 200) => { res.statusCode = status; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(value)); };

const server = http.createServer(async (req, res) => {
  const body = async () => { let raw = ''; for await (const chunk of req) raw += chunk; return raw ? JSON.parse(raw) : {}; };
  if (req.url.startsWith('/api/')) {
    const url = req.url.split('?')[0], method = req.method;
    if (method === 'POST') calls.push({url, body: await body()});
    else calls.push({url});
    if (url === '/api/root/agents/ordu/chat' && method === 'GET') return json(res, {status: 'ok', data: CHAT});
    if (url === '/api/root/agents/ordu/chat/read') return json(res, {status: 'ok', data: {last_read_sequence: 0, newest_sequence: 1}});
    if (url === '/api/root/operations/focus_get') return json(res, focusBoard());
    if (url === '/api/me/agents/ordu/focus') return json(res, {status: 'ok', data: {desktop_control_url: null, browser: {state: 'idle'}}});
    if (url === '/api/root/operations/owner_conversation_get')
      return json(res, {status: 'ok', data: {conversation: {message_count: 0}, read_state: {unread: false}, messages: [], next_before: null, participants: []}});
    if (url === '/api/biz/proj49/operations/tasks_get')
      return json(res, {status: 'ok', data: {task: {id: 'task-events', title: 'Events', task_type: 'task', status: 'working', revision: 1, tags: [], assignee_ids: []}}});
    if (url === '/api/root/operations/owner_conversation_related')
      return json(res, {status: 'ok', data: {items: [RELATED], next_offset: null}});
    if (url === '/api/root/operations/owner_conversation_mark_read')
      return json(res, {status: 'ok', data: {last_read_sequence: 0, newest_sequence: 1}});
    return json(res, {status: 'error', error: {code: 'not_found', message: 'unexpected ' + method + ' ' + url}}, 404);
  }
  if (req.url === '/') {
    res.setHeader('Content-Type', 'text/html');
    return res.end(`<!doctype html><meta charset="utf-8">
      <link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/tasks.css"><link rel="stylesheet" href="/css/agentpane.css">
      <link rel="stylesheet" href="/css/agent-focus.css"><link rel="stylesheet" href="/css/agent-viewbox.css"><link rel="stylesheet" href="/css/global-chat.css">
      <link rel="stylesheet" href="/css/chat-view.css"><link rel="stylesheet" href="/css/conversation.css"><link rel="stylesheet" href="/css/record-workspace.css"><link rel="stylesheet" href="/css/record-panel.css">
      <div class="split agsplit"><aside class="tasks agpad" id="agpad" style="width:900px;height:650px"></aside></div>
      <div id="chat" style="height:420px;width:700px;display:flex;flex-direction:column"></div>
      <div id="toasts"></div>
      <script>
      ${identity}
      ${lateCore}
      const uiFailure=error=>error?.message||'Failed';
      const coreData=r=>{if(r?.status!=='ok'||!r.data)throw Error('invalid');return r.data;}, state={scope:'global',globalTab:'agents',biz:null,bizs:[{id:'biz-49',slug:'proj49',name:'Project 49'}],tab:'agents'};
      window.EventSource=class{static CONNECTING=0;static OPEN=1;static CLOSED=2;constructor(url){this.url=url;this.readyState=1;this.l={};}
        addEventListener(t,f){(this.l[t]||=[]).push(f);}close(){this.readyState=2;}};
      const api=async p=>{const r=await fetch('/api'+p), d=await r.json();if(!r.ok)throw Error(d.error||'failed');return d;};
      const post=async(p,b)=>{const r=await fetch('/api'+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});return await r.json();};
      ${icons}
      const taskStateSubscribe=()=>()=>{};
      </script>
      <script src="/js/tags.js"></script><script src="/js/work-groups.js"></script><script src="/js/tasks.js"></script><script src="/js/record-panel.js"></script>
      <script src="/js/safe-links.js"></script><script src="/js/chat-view.js"></script><script src="/js/conversation.js"></script><script src="/js/record-workspace.js"></script><script src="/js/global-chat.js"></script><script src="/js/agentpane.js"></script>
      <script src="/js/agent-focus.js"></script><script src="/js/agent-viewbox.js"></script><script src="/js/agent-browser.js"></script><script src="/js/global-calendar.js"></script>
      <script>
      agpadPaint=()=>{}; agpadTick=()=>{}; agentCurrentWorkMount=()=>{};
      window.agentId='ordu';
      window.mountAll=()=>{
        agpadMount(agentId,$('#agpad'),{biz:null,agentId,workspaceScope:'global',workspaceRole:'ordubashy',open:true,isCurrent:()=>window.agentId===agentId});
        window.chat=mountAgentView($('#chat'),{agent_id:agentId,display_name:'Ordubashy'},{isCurrent:()=>window.agentId===agentId});
        let pairedGeneration=0;
        window.agentWorkspacePairedRecord=(child,source,parentTable)=>source
          ? window.chat.selectPairedSource(source,++pairedGeneration,()=>{},ref=>window.agentWorkspacePairedReference?.(ref) ?? false)
          : window.chat.clearPairedSource(++pairedGeneration);
      };
      </script>`);
  }
  const file = path.resolve(root, '.' + req.url.split('?')[0]);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css'); res.end(fs.readFileSync(file));
});

let page;
const until = async (fn, timeout = 15000) => {
  const started = Date.now();
  while (Date.now() - started < timeout) { if (fn()) return; await page.waitForTimeout(50); }
  throw new Error('Timed out waiting for the fixture');
};
const posts = suffix => calls.filter(call => call.url.endsWith(suffix));
const conversationReads = () => posts('/owner_conversation_get');

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined, args: ['--no-sandbox']});
  try {
    page = await browser.newPage({viewport: {width: 1400, height: 900}});
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await page.goto(origin);
    await page.evaluate(() => mountAll());
    await page.waitForSelector('.gchat-msg[data-id="id1"] button.gchat-task-reference', {timeout: 15000});

    const reference = page.locator('button.gchat-task-reference');
    const inactive = page.locator('.gchat-msg[data-id="id1"] .gchat-reference-unavailable');
    assert.equal(await reference.count(), 1, 'only the resolved Task reference is an opener');
    assert.equal(await reference.getAttribute('type'), 'button');
    assert.equal(await inactive.count(), 1, 'the unavailable Task reference stays a plain chip');
    assert.equal(await inactive.evaluate(el => el.tagName), 'SPAN');

    // -- interaction is visible and stable: 1px inset outline plus a wash, no size change
    const box = () => reference.boundingBox();
    const idle = await reference.evaluate(el => { const s = getComputedStyle(el); return {shadow: s.boxShadow, background: s.backgroundColor}; });
    const before = await box();
    await reference.hover();
    const hovered = await reference.evaluate(el => { const s = getComputedStyle(el); return {shadow: s.boxShadow, background: s.backgroundColor, cursor: s.cursor}; });
    const after = await box();
    assert.deepEqual(after, before, 'hovering a Task reference must not move or resize it');
    assert.match(hovered.shadow, /inset/, 'hover draws an inset 1px outline');
    assert.notEqual(hovered.background, idle.background, 'hover adds a modest background');
    assert.equal(hovered.cursor, 'pointer');

    // -- the main chat draft and session survive opening the record
    const composer = page.locator('#chat .gchat-composer textarea');
    await composer.fill('keep this unsent draft');
    const chatEventsBefore = posts('/chat/messages').length;

    // -- click opens that exact Task as the paired private thread and authorized details, with no synthetic CRM parent
    await reference.click();
    await page.waitForFunction(() => document.querySelector('.rw-record:not([hidden]) .rw-title')?.textContent === 'Events', null, {timeout: 15000});
    assert.equal(await page.locator('.agfocus-layout.agfocus-paired-workspace').count(), 1, 'a main-chat Task reference opens paired Focus');
    assert.equal(await page.locator('.rw-record:not([hidden]) [data-rw-tab="chat"]').count(), 0, 'paired details do not add a second Chat tab');
    assert.equal(await page.locator('.rw-record:not([hidden]) .rw-dashboard').isVisible(), true, 'the Task details occupy the right pane');
    assert.equal(await page.locator('.rw-record:not([hidden]) .rw-context').isVisible(), false, 'a standalone reference does not invent a CRM parent');
    await page.waitForSelector('.gchat-paired-record .gchat-scroll', {timeout: 15000});
    assert.equal(await page.locator('.gchat-composer textarea,.conversation-inline-compose textarea').count(), 1, 'paired mode keeps one main composer');
    await until(() => conversationReads().length > 0);
    const firstRead = conversationReads().at(-1);
    assert.equal(firstRead.url, '/api/root/operations/owner_conversation_get');
    assert.deepEqual(firstRead.body.source, {type: 'task', id: 'task-events', business_id: 'biz-49'}, 'the exact Task and its Project identity are read');
    assert.equal(firstRead.body.scope, 'global');
    assert.equal(posts('/tasks_get').filter(call => call.body?.task_id === 'task-events').length, 2, 'one canonical metadata read and one paired-details read target only the referenced Task');
    assert.equal(conversationReads().filter(call => call.body?.source?.id === 'task-hidden').length, 0);
    assert.equal(posts('/chat/messages').length, chatEventsBefore, 'no message is sent');

    // -- keyboard invocation reaches the same paired Task
    await page.locator('.rw-head .rw-close, [data-rw-close]').first().click();
    await page.waitForFunction(() => !document.querySelector('.rw-record'), null, {timeout: 5000});
    await page.waitForFunction(() => document.querySelector('#chat .gchat-composer textarea')?.value === 'keep this unsent draft', null, {timeout: 5000});
    assert.equal(posts('/chat/messages').length, chatEventsBefore, 'closing restores the main chat draft without sending it');
    const readsBefore = conversationReads().length;
    await page.locator('#chat .gchat-scroll').evaluate(el => el.focus());
    for (let step = 0; step < 8 && !await reference.evaluate(el => el === document.activeElement); step++) await page.keyboard.press('Tab');
    assert.equal(await reference.evaluate(el => el === document.activeElement), true, 'the reference is reachable from the keyboard');
    const focusRing = await reference.evaluate(el => { const s = getComputedStyle(el); return {width: s.outlineWidth, style: s.outlineStyle}; });
    assert.equal(focusRing.style, 'solid');
    assert.equal(focusRing.width, '2px', 'keyboard focus stays equally apparent');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.querySelector('.rw-record:not([hidden]) .rw-title')?.textContent === 'Events' && document.querySelector('.gchat-paired-record'), null, {timeout: 15000});
    assert.equal(conversationReads().length, readsBefore + 1, 'Enter opens the same Task thread');
    assert.deepEqual(conversationReads().at(-1).body.source, {type: 'task', id: 'task-events', business_id: 'biz-49'});

    // -- flat record-thread rows keep their click and gain the same hover outline
    await page.waitForSelector('.gchat-paired-record button.gchat-ref-row', {timeout: 15000});
    const row = page.locator('.gchat-paired-record button.gchat-ref-row').filter({hasText: 'Related follow-up'}).first();
    const rowBefore = await row.boundingBox();
    const rowIdle = await row.evaluate(el => getComputedStyle(el).boxShadow);
    await row.hover();
    const rowHover = await row.evaluate(el => getComputedStyle(el).boxShadow);
    assert.deepEqual(await row.boundingBox(), rowBefore, 'the grouped row does not move on hover');
    assert.match(rowHover, /inset/);
    assert.notEqual(rowHover, rowIdle);
    await row.click();
    await page.waitForFunction(() => document.querySelector('.rw-record:not([hidden]) .rw-title')?.textContent === 'Related follow-up', null, {timeout: 15000});
    assert.deepEqual(conversationReads().at(-1).body.source, {type: 'task', id: 'task-related', business_id: 'biz-49'}, 'a grouped row still opens its own record');

    // -- an unavailable main-transcript reference remains inert after closing paired Focus
    await page.locator('.rw-close:visible').first().click();
    await page.waitForSelector('#chat .gchat-msg[data-id="id1"] .gchat-reference-unavailable');
    const readsNow = conversationReads().length;
    const inactiveBox = await inactive.boundingBox();
    await inactive.hover();
    assert.deepEqual(await inactive.boundingBox(), inactiveBox);
    assert.equal((await inactive.evaluate(el => getComputedStyle(el).boxShadow)), 'none');
    assert.equal(await inactive.evaluate(el => getComputedStyle(el).cursor), 'not-allowed');
    await inactive.click({force: true});
    await page.waitForTimeout(200);
    assert.equal(conversationReads().length, readsNow, 'an unavailable reference opens nothing');

    // A delayed workspace frame must not open a reference after the selected agent pane changes.
    const readsBeforeStale = conversationReads().length;
    await page.evaluate(async () => {
      const pane=_agpad, frame=document.createElement('iframe');
      pane.el.append(frame);
      const loaded=new Promise(resolve=>frame.addEventListener('load',resolve,{once:true}));
      frame.srcdoc='<div class="agfocus"></div>';
      await loaded;
      frame.contentWindow.agentWorkspacePairedReference=()=>{window.staleReferenceOpened=true;return true;};
      window.stalePane=pane; window.staleFrame=frame; pane.workspaceFrame=frame;
      window.staleReferenceOpened=false; window.staleReadyRequested=false;
      window.staleReady=new Promise(resolve=>{window.releaseStaleReady=resolve;});
      Object.defineProperty(frame,'workspaceReady',{configurable:true,get(){window.staleReadyRequested=true;return window.staleReady;}});
    });
    await reference.click();
    await page.waitForFunction(() => window.staleReadyRequested === true);
    await page.evaluate(() => {
      _agpad={...window.stalePane,agentId:'marketing'};
      window.releaseStaleReady();
    });
    await page.waitForTimeout(250);
    assert.equal(await page.evaluate(() => window.staleReferenceOpened), false, 'the delayed callback never opens in a replacement agent pane');
    assert.equal(conversationReads().length, readsBeforeStale, 'stale navigation opens no private source thread');
    assert.equal(await page.locator('.rw-record:not([hidden]),.gchat-paired-record').count(), 0, 'stale navigation opens no record or paired thread');
    await page.evaluate(() => { _agpad=window.stalePane; window.stalePane.workspaceFrame=null; window.staleFrame.remove(); });

    assert.deepEqual(errors, []);
    console.log('Task reference click: paired thread/details, canonical scope, no inferred parent, keyboard, stable hover, inert unavailable refs, flat rows and stale-agent guard passed');
  } catch (error) {
    console.error(error.stack || error.message);
    process.exitCode = 1;
  } finally {
    await browser.close();
    server.close();
  }
})();
