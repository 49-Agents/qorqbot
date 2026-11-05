// Isolated preview navigation checks. All application reads use fixture records.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..'), calls = [], pairedPrivateReads = [];
const profiles = [{agent_id:'ordu', name:'ordu', display_name:'Ordubashy', status:'active', role:{id:'ordubashy'}, scope:{kind:'ecosystem',allowed_business_ids:[],global_capabilities:[]}},
  {agent_id:'marketing', name:'marketing', display_name:'Marketing', status:'active', role:{id:'global-worker'}, scope:{kind:'ecosystem',allowed_business_ids:[],global_capabilities:[]}}];
let attentionItems = [], marketingBrowser = 'none';
let pendingSignoffs = Array.from({length:20}, (_,i)=>({id:'approval-'+i,title:'Approve the launch announcement '+(i+1),state:'pending',category:'proposal'}));
const activity = agent_id => ({agent_id, state:'running', sessions:[], inbox:{state:'pending', reason:'offline', last_attempt_at:null}});
const heldReads = new Map(), pendingReads = new Set();
function holdRead(url) {
  let arrive, release;
  const arrived = new Promise(resolve => { arrive = resolve; }), waiting = new Promise(resolve => { release = resolve; });
  const hold = {arrive, arrived, waiting, release:() => { pendingReads.delete(hold); release(); }};
  heldReads.set(url, hold); pendingReads.add(hold); return hold;
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/api/')) {
    calls.push({method:req.method, path:url.pathname, search:url.search});
    const hold = heldReads.get(req.url);
    if (hold) { heldReads.delete(req.url); hold.arrive(); await hold.waiting; }
    let data = {items:[], messages:[], agents:[], subscriptions:[], purposes:[], tags:[], totals:{}};
    let raw = false;
    if (url.pathname === '/api/biz') { data = [{id:'biz-a', slug:'widget', name:'Widget', agents:['dev']}]; raw = true; }
    else if (url.pathname === '/api/session') data = {principal_id:'owner', principal_kind:'human', is_owner:true, is_installation_owner:true};
    else if (url.pathname === '/api/root/agents') data = {agents:profiles};
    else if (url.pathname === '/api/root/agent-pins') data = {initialized:true, pinned_agents:[]};
    else if (url.pathname === '/api/root/attention-queue') data = {items:attentionItems};
    else if (url.pathname === '/api/root/signoffs') data = {items:pendingSignoffs,next_cursor:null};
    else if (/\/agents\/[^/]+\/viewbox$/.test(url.pathname)) data = {agent_id:url.pathname.split('/')[4], items:[1,2].map(n => ({id:'preview-'+n, title:'Preview '+n, kind:'local', link:'/screen/preview-'+n, placed_at:'2026-10-03T12:00:00Z', attention_id:'viewbox:preview-'+n+':1'})), dismissed_attention_ids:[]};
    else if (url.pathname.endsWith('/calendar_calendars_list')) data = {configured:false,calendars:[],errors:[]};
    else if (url.pathname.endsWith('/focus_get')) data = {revision:1, items:[]};
    else if (url.pathname.endsWith('/owner_conversation_get')) {
      let rawBody='';for await(const chunk of req)rawBody+=chunk;
      const body=JSON.parse(rawBody);pairedPrivateReads.push(body);
      data={conversation:{id:body.source.id,source:body.source,audience:'owner_ordubashy'},notes:[],next_before:null,participants:[],read_state:{unread:false}};
    }
    else if (url.pathname.endsWith('/workspace_types')) data = {types:[{key:'focus', display_name:'Focus', supported_roles:['ordubashy','global-worker'], renderer:'focus_board'}]};
    else if (url.pathname.endsWith('/browser-surfaces')) {
      const agentId = url.pathname.split('/')[4], viewer = kind => ({id:agentId + '-' + kind, label:kind, kind, viewer_url:`http://${req.headers.host}/screen/${kind}`});
      if (agentId === 'marketing' && marketingBrowser === 'fail') { res.statusCode = 500; res.setHeader('Content-Type', 'application/json'); return res.end('{"error":"fixture"}'); }
      const configured = agentId === 'ordu' || (agentId === 'marketing' && marketingBrowser === 'ok');
      data = {agent_id:agentId, configured, browsers:configured ? [viewer('browser')] : [], desktop:agentId === 'ordu' ? viewer('desktop') : null};
      raw = true;
    }
    else if (url.pathname === '/api/me/agents/ordu/focus') data = {revision:1, items:[], desktop_control_url:null, browser:{state:'idle'}};
    else if (/\/agents\/[^/]+\/chat$/.test(url.pathname)) data = {agent_id:url.pathname.split('/')[4], session: {state: 'live', reason: 'live', can_send: true, working: false, provider: 'claude', conversation_id: 'conv-1', transport: 'terminal', detail: 'Live', binding: {binding_id: 'b1', agent_id: 'a', provider: 'claude', conversation_id: 'conv-1', generation: 1, transport: 'terminal'}}, revision: 0, cursor: 0, conversation: {conversation_id: 'conv-1', provider: 'claude', complete: true, finalized: false, error: null, captured_length: 0}, conversations: [{conversation_id: 'conv-1', provider: 'claude', updated_at: null, current: true}], entries: [], older: null, rendered_boundary: 0, inputs: [], last_read_sequence: 0, newest_sequence: 0};
    else if (url.pathname === '/api/me/agents/activity') data = {agents:[activity(url.searchParams.get('agent_id') || 'ordu')], next_cursor:null};
    else if (url.pathname.endsWith('/inbox')) data = {items:[{id:'update-1', source_id:'task-1', source_type:'tasks', business_slug:'widget', summary:'Fixture inbox update', event_type:'note_added', actor_name:'Dev', created_at:'2026-10-03T12:00:00Z'}], subscriptions:[], delivery:{state:'pending', reason:'offline', last_attempt_at:null}};
    else if (url.pathname === '/api/biz/widget/tasks') { data = []; raw = true; }
    else if (url.pathname.endsWith('/context_graph_get')) data = {nodes:[], edges:[]};
    res.setHeader('Content-Type', 'application/json');
    return res.end(JSON.stringify(raw ? data : {status:'ok', data}));
  }
  if (url.pathname.startsWith('/screen/')) { res.setHeader('Content-Type', 'text/html'); return res.end('<p>Fixture viewer</p>'); }
  const file = path.resolve(root, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined, args:['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport:{width:1400, height:900}}), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
    await page.goto(origin + '/#/global/agents');
    await page.locator('.agfocus').waitFor();
    const firstAgent = page.locator('#global-agent-list .agent').first(), terminalEntry = page.locator('.global-workspace-terminal');
    const mainMenu = page.getByRole('button', {name:'Main menu', exact:true});
    assert.equal(await mainMenu.count(), 0, 'there is no Main menu control');
    assert.equal(await page.locator('.workspace-menu-anchor, .global-rail, #rail:not([inert])').count(), 0, 'no menu anchor, global rail entries or live rail');
    assert.equal(await page.locator('#rail').count(), 0, 'the hidden icon-source rail is removed');
    const firstBox = await firstAgent.boundingBox();
    assert.equal(await page.locator('#global-biz-toggle, [data-business-agent], [data-pin-agent]').count(), 0, 'the rail has no business lookup or pins');
    assert.equal(await page.locator('.workspace-chrome').count(), 0, 'there is no separate top header');
    assert.equal((await page.locator('.global-agent-session').boundingBox()).y, 0, 'chat uses the former header space');
    assert.equal((await page.locator('#agpad').boundingBox()).y, 0, 'agent pane uses the former header space');
    assert.equal(await page.locator('.global-agents > .listcol button[title]:not([data-global-settings]), #agpad .ttab[title]').count(), 0, 'agent rail and pane tabs have no hover titles');
    assert.equal(await page.locator('.agent-nav-actions .agent-broadcast').count(), 0, 'agent navigation has no Broadcast button');
    assert.deepEqual(await page.locator('#agpad .ttab').evaluateAll(tabs => tabs.slice(0, 2).map(tab => tab.dataset.v)), ['focus', 'status'], 'Ordubashy has no Project management entry; Calendar is a drawer tab');
    assert.equal(await page.locator('#agpad .ttab[data-v=browser], #agpad .ttab[data-v=desktop], #agpad .ttab[data-v=viewbox], #agpad .ttab[data-v=calendar], #agpad .ttab[data-v=tables]').count(), 0, 'Ordubashy has no separate Browser, Desktop, Viewbox, Calendar or Tables tabs');
    await page.waitForFunction(() => !!_agpad.focusMount?.isConnected && !!globalAgentViewActive);
    const paired = await page.evaluate(async () => {
      const child=window, view=globalAgentViewActive, calls=[];
      const select=view.selectPairedSource.bind(view), clear=view.clearPairedSource.bind(view);
      view.selectPairedSource=(source,generation)=>{calls.push({source,generation});return select(source,generation);};
      const valid={type:'task',id:'paired-task',business_id:null};
      const foreign=document.createElement('iframe');document.body.append(foreign);
      const invalidFrame=agentWorkspacePairedRecord(foreign.contentWindow,valid);foreign.remove();
      const invalidScope=agentWorkspacePairedRecord(child,{type:'task',id:'x',business_id:42});
      const invalidParent=agentWorkspacePairedRecord(child,valid,{type:'table',id:'crm-table',business_id:'biz-a'});
      const previousFrame=_agpad.workspaceFrame, trustedFrame=document.createElement('iframe');_agpad.el.append(trustedFrame);_agpad.workspaceFrame=trustedFrame;
      const trustedFrameAccepted=agentWorkspacePairedRecord(trustedFrame.contentWindow,{type:'task',id:'frame-task',business_id:null});
      const waitThread=async()=>{for(let i=0;i<100;i++){if(document.querySelector('.gchat-paired-record')?._conversation?.state.loaded)return;await new Promise(resolve=>setTimeout(resolve,20));}throw new Error('paired conversation did not load');};
      await waitThread();
      _agpad.workspaceFrame=previousFrame;trustedFrame.remove();
      const host=child.document.createElement('div');child.document.body.append(host);
      const pairParents=[];
      const workspace=child.recordWorkspaceMount(host,{paired:true,onPairedSource:(record,parent)=>{
        pairParents.push(parent);
        return agentWorkspacePairedRecord(child,record?{type:record.source.type,id:record.source.id,business_id:record.source.business_id ?? null}:null,parent);
      }});
      await workspace.open({source:{type:'table',id:'crm-table',business_id:'biz-a'},label:'CRM',can_thread:true});
      await waitThread();
      const firstGeneration=calls.at(-1)?.generation;
      const navigated=await workspace.navigate({source:valid,label:'Person',can_thread:true},{parentTable:{type:'table',id:'crm-table',business_id:'biz-a',label:'Launch CRM'}});
      await waitThread();
      const pairedHasNoSecondChat=host.querySelectorAll('[data-rw-tab="chat"]').length===0
        && !host.querySelector('.conversation-inline-compose');
      const leftComposerCount=document.querySelectorAll('.gchat-paired-record .conversation-inline-compose textarea').length;
      const rowContext=host.querySelector('[data-navigation-context="crm-record"]')?.querySelector('.rw-context')?.textContent || '';
      const back=await workspace.back();
      await waitThread();
      const backKey=child.parent.document.querySelector('.gchat-paired-record')?.dataset.conversation || '';
      const stale=select(valid,firstGeneration);
      await workspace.close();
      const restored=!!document.querySelector('[data-global-chat="ordu"]') && !document.querySelector('.gchat-paired-record');
      workspace.dispose();host.remove();view.selectPairedSource=select;view.clearPairedSource=clear;
      return {invalidFrame,invalidScope,invalidParent,trustedFrameAccepted,selected:calls.map(call=>call.source?.id),pairParents,navigated,current:workspace.current(),pairedHasNoSecondChat,leftComposerCount,rowContext,back,backKey,stale,restored};
    });
    assert.equal(paired.invalidFrame,false,'a caller other than the current Focus frame is ignored');
    assert.equal(paired.invalidScope,false,'malformed source scope is ignored');
    assert.equal(paired.invalidParent,true,'a CRM parent scope does not override the row scope');
    assert.equal(paired.trustedFrameAccepted,true,'the current embedded Focus frame can bind the paired source');
    assert.deepEqual(paired.selected,['paired-task','frame-task','crm-table','paired-task','crm-table']);
    assert.equal(paired.pairedHasNoSecondChat,true,'paired mode has no second Chat tab or composer');
    assert.equal(paired.leftComposerCount,1,'the visible left pane owns the sole paired composer');
    assert(pairedPrivateReads.some(call=>call.scope==='global' && JSON.stringify(call.source)==JSON.stringify({type:'table',id:'crm-table',business_id:'biz-a'})),'CRM private binding keeps its project identity');
    assert(pairedPrivateReads.some(call=>call.scope==='global' && JSON.stringify(call.source)==JSON.stringify({type:'task',id:'paired-task'})),'the row private binding preserves explicit Global scope');
    assert(pairedPrivateReads.every(call=>!('agent_id' in call) && !('peer_role' in call)),'paired navigation stays in the private Ordubashy channel');
    assert.equal(paired.rowContext,'Launch CRM /','a CRM row carries its parent breadcrumb');
    assert.equal(paired.back,true,'Back restores the prior table and its private conversation');
    assert.equal(paired.backKey,'table:widget:crm-table');
    assert.equal(paired.stale,false,'a stale selection generation cannot replace a newer paired conversation');
    assert.equal(paired.restored,true,'closing the paired workspace restores the original agent chat');
    // Browser/Desktop stay fixed; pending Signoffs scroll beside them without loading the Viewbox board.
    const strip = page.locator('#agpad .attention-queue .agfocus-surface-chip');
    const stripNames = () => strip.evaluateAll(chips => chips.map(chip => chip.getAttribute('aria-label')));
    await page.waitForFunction(() => document.querySelectorAll('.agfocus-surface-board [data-signoff-key]').length === 20);
    assert.deepEqual(await stripNames(), ['Browser', 'Desktop']);
    assert.equal(await page.locator('.agfocus > .focus-signoff-queue, .agfocus-surface-board [data-viewbox-item]').count(),0);
    assert.equal(calls.some(call => /\/viewbox$/.test(call.path)),false,'the toolbar does not fetch documents');
    const queueLayout=await page.locator('.agfocus-surface-board').evaluate(queue=>{
      const fixed=queue.previousElementSibling.previousElementSibling.getBoundingClientRect(),box=queue.getBoundingClientRect();
      queue.scrollLeft=100;
      return {beside:box.left>=fixed.right,scrolls:queue.scrollLeft>0,wrapped:getComputedStyle(queue).flexWrap,overflow:document.documentElement.scrollWidth>innerWidth};
    });
    assert.deepEqual(queueLayout,{beside:true,scrolls:true,wrapped:'nowrap',overflow:false});
    const savedSignoffs=pendingSignoffs;pendingSignoffs=[];
    await page.evaluate(()=>attentionQueue.refreshSignoffs());
    assert.equal(await page.locator('.agfocus-surface-board').isVisible(),false);
    assert.equal(await page.locator('.agfocus-divider').isVisible(),false);
    assert.deepEqual(await stripNames(),['Browser','Desktop']);
    pendingSignoffs=savedSignoffs;await page.evaluate(()=>attentionQueue.refreshSignoffs());
    assert.equal(await page.locator('.agfocus-divider').isVisible(),true);
    await page.evaluate(()=>agviewboxNavigate({agent_id:'ordu',viewbox_item_id:'preview-2'}));
    await page.frameLocator('.agvb-frame').getByText('Fixture viewer', {exact:true}).waitFor();
    assert.equal(await page.locator('.agvb-name').textContent(), 'Preview 2');
    assert.equal(await page.locator('#agpad .ttab[aria-selected=true]').getAttribute('data-v'), 'focus', 'a placement is a detail of Focus');
    await page.getByRole('button', {name:'Back to Focus', exact:true}).click();
    await page.locator('.agfocus').waitFor();
    assert(calls.some(call => call.method === 'POST' && call.path === '/api/root/attention-queue'), 'opening the placement still acknowledges it');
    assert.equal(await page.locator('.agvb-frame').count(), 0);
    assert.equal(await page.locator('#agpad .ttab[data-v=ideas]').count(), 0, 'Ordubashy has no Ideas tab');
    assert((await terminalEntry.boundingBox()).y > firstBox.y, 'Terminal sits below the agents');
    await page.evaluate(() => { window.focusBeforeReselect = document.querySelector('.agfocus'); });
    await firstAgent.click();
    await page.getByRole('tab', {name:'Focus', exact:true}).focus(); await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => focusBeforeReselect === document.querySelector('.agfocus')), true, 'same-agent and current Focus tab selections preserve its renderer');

    const overview = page.getByRole('tab', {name:'Activity and inbox', exact:true});
    assert.equal(await overview.count(), 1);
    assert.equal(await page.locator('#agpad .ttab[data-v=inbox], #agpad .ttab[data-v=activity]').count(), 0);
    assert.equal(await page.locator('#agpad .ttab').evaluateAll(tabs => tabs.every(tab => !tab.textContent.trim())), true, 'main tabs remain icon-only');
    await overview.click();
    await page.locator('.agpad-current .fleet-state[data-state=running]').waitFor();
    assert(calls.some(call => call.path === '/api/me/agents/activity' && call.search.includes('agent_id=ordu')), 'Activity retains its native current-work read');
    await page.waitForFunction(() => document.querySelector('.ttab[data-v=status]')?.getAttribute('aria-description') === 'Running. Notice pending');
    assert.equal(await overview.getAttribute('aria-description'), 'Running. Notice pending');
    const inboxToggle = page.locator('[data-agent-overview=inbox]');
    await inboxToggle.click();
    await page.getByText('Fixture inbox update', {exact:true}).waitFor();
    assert.equal(await inboxToggle.getAttribute('aria-pressed'), 'true');
    assert.equal(await overview.getAttribute('aria-selected'), 'true');
    assert.equal(await inboxToggle.locator('.agpad-tab-dot').getAttribute('data-state'), 'idle', 'a pending notice is an attention dot, not a connected one');
    assert.match(await page.locator('#agpad-body .agpad-inbox-status').innerText(), /Notice pending · No live session; it is delivered after someone starts one manually/);
    assert.equal(await page.locator('#agpad-body').evaluate(el => /watcher|Monitor/i.test(el.textContent)), false);
    assert(calls.some(call => call.path === '/api/me/agents/ordu/inbox'), 'Inbox retains its agent-specific read');
    await page.locator('.gchat-composer textarea').fill('Keep selected agent draft');
    await page.evaluate(() => {
      window.selectionBefore = {pane:_agpad, head:document.querySelector('#agpad .thead'), body:document.querySelector('#agpad-body'), composer:document.querySelector('.gchat-composer textarea')};
    });
    const sameSelection = async (view, draft) => assert.deepEqual(await page.evaluate(() => ({
      same:selectionBefore.pane === _agpad && selectionBefore.head === document.querySelector('#agpad .thead')
        && selectionBefore.body === document.querySelector('#agpad-body') && selectionBefore.composer === document.querySelector('.gchat-composer textarea'),
      view:_agpad.view, draft:document.querySelector('.gchat-composer textarea').value,
    })), {same:true, view, draft}, 'same-agent/tab selection keeps the selected pane, composer and draft');
    await firstAgent.click(); await sameSelection('inbox', 'Keep selected agent draft');
    await overview.click(); await sameSelection('inbox', 'Keep selected agent draft');
    await overview.focus(); await page.keyboard.press('Enter'); await sameSelection('inbox', 'Keep selected agent draft');
    // Registry updates still rebuild navigation when the current view is unchanged.
    await page.evaluate(() => {
      window.workspaceDeclarationBefore = workspaceDeclaration;
      workspaceDeclaration = [...workspaceTypes(), {key:'fixture_workspace', display_name:'Fixture workspace', supported_roles:['ordubashy'], renderer:'fixture_workspace'}];
      agpadWorkspaceViewsSync(_agpad);
    });
    await page.locator('#agpad .ttab[data-v=fixture_workspace]').waitFor();
    assert.equal(await page.evaluate(() => _agpad.view), 'inbox');
    assert.equal(await page.locator('.gchat-composer textarea').inputValue(), 'Keep selected agent draft');
    await page.evaluate(() => { workspaceDeclaration = workspaceDeclarationBefore; agpadWorkspaceViewsSync(_agpad); });
    assert.equal(await page.locator('#agpad .ttab[data-v=fixture_workspace]').count(), 0);
    await page.getByRole('tab', {name:'Focus', exact:true}).click();
    await overview.click();
    assert.equal(await inboxToggle.getAttribute('aria-pressed'), 'true', 'returning restores the selected subview');
    await page.locator('[data-agent-overview=activity]').click();
    await page.locator('.agpad-current').waitFor();
    assert.equal(await page.locator('#agpad .agpad-overview-switch').count(), 1);

    await page.getByRole('tab', {name:'Focus', exact:true}).click();
    await page.locator('[data-focus-surface=browser]').click();
    await page.locator('iframe[data-surface=ordu-browser]').waitFor();
    assert.equal(await page.locator('#agpad .agbrowser-bar button[title], #agpad iframe[title]').count(), 0, 'Browser controls and frame have no hover titles');
    await page.locator('[data-agent-mode=desktop]').click();
    await page.locator('iframe[data-surface=ordu-desktop]').waitFor();
    assert.equal(await page.locator('#agpad .ttab[aria-selected=true]').getAttribute('data-v'), 'focus', 'Browser and Desktop are Focus details');
    await page.getByRole('tab', {name:'Focus', exact:true}).click();
    await page.locator('.agfocus').waitFor();
    await overview.click();
    assert.equal(await page.locator('#agpad iframe').count(), 0, 'switching to Activity tears down only the viewer');
    await page.getByRole('tab', {name:'Focus', exact:true}).click();
    await page.locator('[data-focus-surface=desktop]').click();
    await page.locator('iframe[data-surface=ordu-desktop]').waitFor();
    await page.getByRole('tab', {name:'Focus', exact:true}).click();
    await page.locator('[data-focus-surface=browser]').click();
    await page.locator('iframe[data-surface=ordu-browser]').waitFor();
    assert.equal(await page.locator('[data-agent-mode=browser]').getAttribute('aria-pressed'), 'true', 'the Browser chip opens Browser even after Desktop was last used');
    await overview.click();

    // Nested surfaces: Browser, Desktop and Viewbox items open under the queue in place of the items list; Focus stays mounted.
    await page.getByRole('tab', {name:'Focus', exact:true}).click();
    await page.locator('.agfocus').waitFor();
    await page.evaluate(() => {
      const items = document.querySelector('.agfocus-items'), layout = document.querySelector('.agfocus-layout');
      items.dataset.keep = 'draft'; window.nestedNodes = {board:document.querySelector('.agfocus'), items, queue:document.querySelector('#agpad .attention-queue'), workspaceDispose:_agpad.workspaceDispose};
      _agpad.focusDrafts.set('draft-1', {note:'kept note'});
    });
    const nestedState = () => page.evaluate(() => {
      const queue = document.querySelector('#agpad .attention-queue').getBoundingClientRect(), frames = [...document.querySelectorAll('#agpad iframe')];
      const frame = frames[0]?.getBoundingClientRect(), layout = document.querySelector('.agfocus-layout'), host = document.querySelector('.agfocus-surface');
      return {frames:frames.length, inHost:frames.every(f => host.contains(f)), below:frame ? frame.top >= queue.bottom - 1 : null,
        listHidden:getComputedStyle(layout).display === 'none', sameBoard:nestedNodes.board === document.querySelector('.agfocus') && nestedNodes.items === document.querySelector('.agfocus-items') && nestedNodes.queue === document.querySelector('#agpad .attention-queue'),
        keep:document.querySelector('.agfocus-items').dataset.keep, note:_agpad.focusDrafts.get('draft-1')?.note, view:_agpad.view, surface:_agpad.surface?.kind || null,
        sameDispose:_agpad.workspaceDispose === nestedNodes.workspaceDispose, overflow:document.documentElement.scrollWidth > innerWidth,
        pressed:[...document.querySelectorAll('#agpad .attention-queue [aria-pressed=true]')].map(chip => chip.dataset.focusSurface || chip.dataset.viewboxItem),
        mainTabs:[...document.querySelectorAll('#agpad .ttab')].some(tab => ['browser','desktop','viewbox'].includes(tab.dataset.v))};
    });
    await page.locator('[data-focus-surface=browser]').click();
    await page.locator('.agfocus-surface-body iframe[data-surface=ordu-browser]').waitFor();
    assert.deepEqual(await nestedState(), {frames:1, inHost:true, below:true, listHidden:true, sameBoard:true, keep:'draft', note:'kept note', view:'focus', surface:'browser', sameDispose:true, overflow:false, pressed:['browser'], mainTabs:false}, 'Browser replaces only the list area under the queue');
    await page.locator('[data-focus-surface=desktop]').click();
    await page.locator('.agfocus-surface-body iframe[data-surface=ordu-desktop]').waitFor();
    assert.deepEqual((await nestedState()).pressed, ['desktop']);
    assert.equal((await nestedState()).frames, 1, 'one active frame after switching');
    await page.evaluate(()=>agviewboxNavigate({agent_id:'ordu',viewbox_item_id:'preview-1'}));
    await page.locator('.agfocus-surface-body .agvb-frame').waitFor();
    assert.deepEqual([(await nestedState()).frames, (await nestedState()).pressed, (await nestedState()).surface], [1, [], 'viewbox']);
    // The compact Back control (keyboard operable) restores the same list, draft and queue without remounting Focus.
    await page.locator('[data-surface-back]').focus(); await page.keyboard.press('Enter');
    await page.waitForFunction(() => !document.querySelector('#agpad iframe'));
    assert.deepEqual(await nestedState(), {frames:0, inHost:true, below:null, listHidden:false, sameBoard:true, keep:'draft', note:'kept note', view:'focus', surface:null, sameDispose:true, overflow:false, pressed:[], mainTabs:false}, 'Back to Focus restores the list');
    // The already-selected Focus entry also closes a surface.
    await page.locator('[data-focus-surface=browser]').click();
    await page.locator('.agfocus-surface-body iframe[data-surface=ordu-browser]').waitFor();
    await page.getByRole('tab', {name:'Focus', exact:true}).click();
    assert.deepEqual([(await nestedState()).frames, (await nestedState()).sameBoard, (await nestedState()).listHidden], [0, true, false]);
    // A late Desktop read cannot repaint after return or after another agent is selected.
    const lateDesktop = holdRead('/api/me/agents/ordu/focus');
    await page.locator('[data-focus-surface=desktop]').click(); await lateDesktop.arrived;
    await page.locator('[data-surface-back]').click();
    lateDesktop.release(); await page.waitForTimeout(200);
    assert.deepEqual([(await nestedState()).frames, (await nestedState()).surface, await page.locator('.agfocus-surface-body > *').count()], [0, null, 0], 'late response after Back paints nothing');
    const lateAgent = holdRead('/api/me/agents/ordu/focus');
    await page.locator('[data-focus-surface=desktop]').click(); await lateAgent.arrived;
    await page.locator('#global-agent-list').getByRole('button', {name:'Marketing', exact:true}).click(); await page.getByRole('tab', {name:'Focus', exact:true}).click();
    await page.waitForFunction(() => _agpad.agentId === 'marketing');
    lateAgent.release(); await page.waitForTimeout(200);
    assert.equal(await page.locator('#agpad iframe, .agfocus-surface-body > *').count(), 0, 'late response cannot show another agent content');
    await page.locator('#global-agent-list').getByRole('button', {name:'Ordubashy', exact:true}).click();
    await page.locator('.agfocus').waitFor();

    // Slow first reads finish for the same agent after its subview is hidden.
    // A general worker opens on inbox/status, so these are its first reads on selection; they finish after the subview is hidden.
    const activityURL = '/api/me/agents/activity?agent_id=marketing', inboxURL = '/api/me/agents/marketing/inbox';
    // Strip launchers follow the selected agent's own resolved binding: checking, then enabled; failed, then retried; late reads never leak.
    const agentButton = name => page.locator('#global-agent-list').getByRole('button', {name, exact:true});
    const launcherState = name => page.locator(`[data-focus-surface=${name}]`).evaluate(chip => [chip.getAttribute('aria-disabled'), chip.title]);
    marketingBrowser = 'ok';
    const lateBinding = holdRead('/api/root/agents/marketing/browser-surfaces');
    await agentButton('Marketing').click(); await lateBinding.arrived; await page.getByRole('tab', {name:'Focus', exact:true}).click();
    assert.deepEqual(await launcherState('browser'), ['true', 'Browser: checking this agent’s configuration']);
    lateBinding.release();
    await page.waitForFunction(() => !document.querySelector('[data-focus-surface=browser]').hasAttribute('aria-disabled'));
    assert.deepEqual(await launcherState('desktop'), ['true', 'Desktop unavailable: only Ordubashy has a desktop']);
    await page.locator('[data-focus-surface=browser]').click();
    await page.locator('.agfocus-surface-body iframe[data-surface=marketing-browser]').waitFor();
    assert.equal(await page.locator('.agfocus-layout').evaluate(el => getComputedStyle(el).display), 'none', 'Marketing Browser replaces the list under its queue');
    assert.equal(await page.locator('[data-focus-surface=browser]').getAttribute('aria-pressed'), 'true');
    await page.getByRole('button', {name:'Back to Focus', exact:true}).click();
    assert.equal(await page.locator('#agpad iframe').count(), 0);
    assert.equal(await page.locator('[data-focus-surface=browser]').getAttribute('aria-pressed'), 'false');
    await page.locator('[data-focus-surface=browser]').click();
    await page.locator('.agfocus-surface-body iframe[data-surface=marketing-browser]').waitFor();
    await page.getByRole('tab', {name:'Focus', exact:true}).click();
    marketingBrowser = 'fail';
    await agentButton('Ordubashy').click(); await agentButton('Marketing').click(); await page.getByRole('tab', {name:'Focus', exact:true}).click();
    await page.waitForFunction(() => /could not be checked/.test(document.querySelector('[data-focus-surface=browser]')?.title || ''));
    assert.equal((await launcherState('browser'))[0], 'true', 'a failed read is not reported as no configuration');
    marketingBrowser = 'ok';
    await page.evaluate(() => document.querySelector('[data-focus-refresh]').click());
    await page.waitForFunction(() => !document.querySelector('[data-focus-surface=browser]').hasAttribute('aria-disabled'));
    const wrongAgent = holdRead('/api/root/agents/marketing/browser-surfaces');
    await agentButton('Ordubashy').click(); await agentButton('Marketing').click(); await wrongAgent.arrived; await page.getByRole('tab', {name:'Focus', exact:true}).click();
    await agentButton('Ordubashy').click(); wrongAgent.release();
    await page.waitForFunction(() => !document.querySelector('[data-focus-surface=browser]').hasAttribute('aria-disabled'));
    assert.deepEqual(await launcherState('browser'), [null, 'Browser'], 'a late read for another agent changes nothing');
    assert.deepEqual(await launcherState('desktop'), [null, 'Desktop']);
    marketingBrowser = 'none';
    await page.locator('#global-agent-list').getByRole('button', {name:'Marketing', exact:true}).click(); await page.getByRole('tab', {name:'Focus', exact:true}).click();
    // Marketing also has Focus: same pending Signoff strip; its Browser (none configured) and Desktop (Ordubashy's alone) are unavailable.
    assert.equal(await page.locator('#agpad .ttab[data-v=viewbox], #agpad .ttab[data-v=browser], #agpad .ttab[data-v=desktop]').count(), 0, 'Marketing has no separate Browser, Desktop or Viewbox tabs');
    await page.waitForFunction(() => document.querySelectorAll('.agfocus-surface-board [data-signoff-key]').length === 20);
    assert.deepEqual(await page.locator('#agpad .attention-queue [data-focus-surface]').evaluateAll(chips => chips.map(chip => [chip.dataset.focusSurface, chip.getAttribute('aria-disabled')])), [['browser', 'true'], ['desktop', 'true']]);
    assert.match(await page.locator('[data-focus-surface=desktop]').getAttribute('title'), /only Ordubashy/);
    await page.locator('[data-focus-surface=browser]').click({force:true});
    assert.equal(await page.evaluate(() => _agpad.view), 'focus', 'an unavailable launcher opens nothing');
    assert(calls.some(call => call.path === '/api/root/agents/marketing/browser-surfaces'), 'Marketing reads its own browser bindings');
    assert.deepEqual(await page.evaluate(() => [agpadHeadViews({views:['focus','viewbox','browser'], workspaceRole:'ordubashy', agentId:'a'}), agpadHeadViews({views:['inbox','viewbox','browser'], workspaceRole:'founder', agentId:'f'})]), [['focus'], ['status', 'viewbox', 'browser']], 'agents without Focus keep their tabs');
    // Reselecting Marketing after Ordubashy disposes the old pane; holds and read accounting start from this fresh selection.
    await agentButton('Ordubashy').click();
    const cacheReadStart = calls.length;
    const delayedActivity = holdRead(activityURL), delayedInbox = holdRead(inboxURL);
    await agentButton('Marketing').click();
    await overview.click(); await page.locator('[data-agent-overview=activity]').click(); await delayedActivity.arrived;
    const scroll = await page.evaluate(() => {
      const tabs = document.querySelector('#agpad .ttabs');
      tabs.style.maxWidth = '30px'; tabs.style.overflowX = 'auto'; tabs.scrollLeft = 15;
      window.overviewNodes = ['.thead', '.ttabs', '.agpad-overview-switch', '[data-agent-overview=activity]', '[data-agent-overview=inbox]', '#agpad-body']
        .map(selector => [selector, document.querySelector('#agpad ' + selector)]);
      return tabs.scrollLeft;
    });
    assert(scroll > 0, 'fixture creates a scrolled top navigation');
    const stableOverview = async mode => {
      assert.deepEqual(await page.evaluate(() => ({
        same:overviewNodes.every(([selector, node]) => node === document.querySelector('#agpad ' + selector)),
        scroll:document.querySelector('#agpad .ttabs').scrollLeft,
        focus:document.activeElement.dataset.agentOverview,
      })), {same:true, scroll, focus:mode}, 'subview switching preserves header/toggle nodes, header scroll and local focus');
    };
    const activityToggle = page.locator('[data-agent-overview=activity]');
    await inboxToggle.click(); await delayedInbox.arrived; await stableOverview('inbox');
    delayedActivity.release();
    await page.waitForFunction(() => _agpad.activity?.agent_id === 'marketing');
    assert.equal(await page.locator('.agpad-current').count(), 0, 'offscreen Activity response does not replace Inbox');
    await activityToggle.click(); await stableOverview('activity');
    await page.locator('.agpad-current .fleet-state[data-state=running]').waitFor();
    delayedInbox.release();
    await page.waitForFunction(() => !!_agpad.inbox && !_agpad.inboxPending);
    assert.equal(await page.locator('.agpad-current').count(), 1, 'offscreen Inbox response does not replace Activity');
    const readCount = url => calls.slice(cacheReadStart).filter(call => call.path + call.search === url).length;
    assert.equal(await page.evaluate(() => {
      document.querySelector('[data-agent-overview=inbox]').click();
      return document.querySelector('#agpad-body').textContent.includes('Fixture inbox update');
    }), true, 'cached Inbox is present synchronously on switchback');
    await stableOverview('inbox');
    assert.equal(readCount(activityURL), 1, 'a fresh Activity cache avoids a duplicate read');
    assert.equal(readCount(inboxURL), 1, 'a fresh Inbox cache avoids a duplicate read');

    const inboxRefresh = holdRead(inboxURL);
    await page.evaluate(() => { _agpad.nextInbox = 0; agpadTick(); }); await inboxRefresh.arrived;
    await page.getByText('Fixture inbox update', {exact:true}).waitFor();
    await activityToggle.click(); await inboxToggle.click(); await stableOverview('inbox');
    assert.equal(readCount(inboxURL), 2, 'switching during refresh reuses the pending read');
    inboxRefresh.release(); await page.waitForFunction(() => !_agpad.inboxPending);
    await activityToggle.click(); await stableOverview('activity');
    const manualActivity = holdRead(activityURL);
    await page.getByRole('button', {name:'Refresh agent activity', exact:true}).click(); await manualActivity.arrived;
    await page.locator('.agpad-current .fleet-state[data-state=running]').waitFor();
    await inboxToggle.click(); await activityToggle.click(); await stableOverview('activity');
    assert.equal(readCount(activityURL), 2, 'manual refresh still reads while cached Activity stays visible');
    manualActivity.release(); await page.waitForFunction(() => !_agpad.activityPending);

    // A late result belongs to its original pane, never to a newly selected agent.
    await inboxToggle.click();
    const oldInbox = holdRead(inboxURL);
    await page.evaluate(() => { window.previousPane = _agpad; _agpad.nextInbox = 0; agpadTick(); }); await oldInbox.arrived;
    await page.locator('#global-agent-list').getByRole('button', {name:'Ordubashy', exact:true}).click();
    oldInbox.release(); await page.locator('.agfocus').waitFor();
    await page.waitForFunction(() => !previousPane.inboxPending);
    assert.equal(await page.evaluate(() => _agpad.agentId === 'ordu' && !_agpad.inbox), true, 'late inbox data cannot leak to another agent');

    // Attention uses the already-mounted roster, including agents only in lookup.
    await page.locator('#global-agent-list').getByRole('button', {name:'Marketing', exact:true}).click(); await page.getByRole('tab', {name:'Focus', exact:true}).click();
    await page.locator('.agfocus').waitFor();
    await page.locator('.gchat-composer textarea').fill('Marketing attention draft');
    await page.evaluate(() => {
      window.attentionShell = ['.global-agents', '.global-agents > .listcol', '#global-primary-roster', '.global-workspace-terminals', '.global-agent-session', '#agpad', '.agent-pane-divider', '#rail']
        .map(selector => [selector, document.querySelector(selector)]);
    });
    const stableShell = async () => assert.equal(await page.evaluate(() => attentionShell.every(([selector,node]) => node === document.querySelector(selector))), true, 'attention retains the page shell, menu, roster, lookup, pane containers and divider');
    const rosterReads = () => calls.filter(call => call.path === '/api/root/agents').length;
    const beforeAttentionReads = rosterReads();
    // Focus agents show the surfaces strip instead of the queue, so exercise the shared queue through its own mount.
    await page.evaluate(() => { const host = document.createElement('section'); host.id = 'queue-fixture'; document.body.append(host); attentionQueue.mount(host); });
    // A former business agent's reply is history: it explains itself and opens nothing.
    attentionItems = [{id:'dev-reply',kind:'global',chat:true,agent_id:'dev',agent_name:'Dev',text:'Email preview ready',viewed_ids:['dev-reply']}];
    await page.evaluate(() => attentionQueue.refresh());
    await page.locator('#queue-fixture [data-attention-key="dev-reply"]').click();
    await page.locator('#toasts').getByText('This agent is no longer active. Its history is kept.', {exact:true}).waitFor();
    assert.equal(await page.locator('[data-global-chat="dev"]').count(), 0);
    assert.equal(await page.evaluate(() => _agpad.agentId), 'marketing', 'the current agent stays selected');
    await stableShell();
    assert.equal(rosterReads(), beforeAttentionReads, 'opening another agent does not reload profiles');
    assert.equal(calls.some(call => call.path === '/api/root/agent-pins' || /^\/api\/biz\/[^/]+\/agents/.test(call.path)), false, 'no pins or business rosters are read');
    await page.evaluate(() => replyNotifications.open({kind:'global',chat:true,agent_id:'marketing'}));
    await page.locator('[data-global-chat="marketing"]').waitFor();
    assert.equal(await page.locator('.gchat-composer textarea').inputValue(), 'Marketing attention draft', 'returning preserves the previous agent draft');
    await page.evaluate(() => { window.attentionChat = document.querySelector('[data-global-chat]'); window.attentionPane = _agpad; });
    await page.evaluate(() => replyNotifications.open({kind:'global',chat:true,agent_id:'marketing'}));
    assert.equal(await page.evaluate(() => attentionChat === document.querySelector('[data-global-chat]') && attentionPane === _agpad), true, 'same-agent notification preserves chat and pane');
    attentionItems = [{id:'marketing-preview',kind:'viewbox',agent_id:'marketing',agent_name:'Marketing',text:'Preview 1',viewbox_item_id:'preview-1'}];
    await page.evaluate(() => attentionQueue.refresh());
    await page.locator('#queue-fixture [data-attention-key="marketing-preview"]').click();
    await page.frameLocator('.agvb-frame').getByText('Fixture viewer', {exact:true}).waitFor();
    assert.equal(await page.locator('.agvb-name').textContent(), 'Preview 1');
    assert.equal(await page.evaluate(() => attentionChat === document.querySelector('[data-global-chat]')), true, 'Viewbox attention changes only the current right pane');
    await stableShell();
    await page.evaluate(() => agviewboxNavigate({agent_id:'marketing',viewbox_item_id:'preview-2'}));
    await page.waitForFunction(() => document.querySelector('.agvb-name')?.textContent === 'Preview 2');
    assert.equal(await page.evaluate(() => attentionChat === document.querySelector('[data-global-chat]')), true, 'opening another placement does not remount chat');
    await assert.rejects(page.evaluate(() => agviewboxNavigate({agent_id:'dev',viewbox_item_id:'preview-1'})), /no longer active/);
    assert.equal(await page.evaluate(() => _agpad.agentId), 'marketing', 'an unavailable agent\'s placement opens nothing');
    await stableShell();
    assert.equal(rosterReads(), beforeAttentionReads);
    attentionItems = [];

    for (const width of [760, 390]) {
      await page.setViewportSize({width, height:900});
      assert.equal(await terminalEntry.isVisible(), true, 'Terminal stays reachable');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'the rail fits the viewport');
    }
    await page.setViewportSize({width:1400, height:900});
    await page.evaluate(() => document.querySelector('[data-agent-nav-toggle]').click());
    await page.getByRole('button', {name:'Show agent navigation', exact:true}).click();
    await page.locator('.global-agents > .listcol').waitFor();
    await page.goto(origin + '/?agent=ordu&workspace=browser#/global/agents');
    await page.locator('iframe[data-surface=ordu-browser]').waitFor();
    assert.equal(await page.locator('#agpad .ttab[aria-selected=true]').getAttribute('data-v'), 'focus', 'explicit workspace links still open their requested view with Focus selected');
    await page.goto(origin + '/?agent-pane-embed=business&business=widget&section=email');
    await page.waitForFunction(() => state.tab === 'tasks');   // the retired Mail section lands on the first offered section
    assert.equal(await page.locator('.email-view').count(), 0);
    assert.equal(await mainMenu.count(), 0, 'embedded pages have no main menu');
    assert.equal(await page.locator('#mknav, #mkbar, .mkcal').count(), 0, 'the retired Marketing navigation and calendar are not rendered');
    await page.goto(origin + '/?agent-pane-embed=business&business=widget&section=marketing');
    await page.waitForFunction(() => state.tab === 'tasks');
    assert.equal(await page.locator('#mknav, .email-view').count(), 0, 'the retired Marketing section lands on the first offered section');
    const readOperations = new Set(['calendar_calendars_list', 'calendar_events_list', 'focus_get', 'workspace_types', 'tasks_config_get', 'assignees_list', 'broadcasts_list', 'context_graph_get', 'tasks_list', 'subscriptions_list', 'context_filing_costs', 'costs_overview', 'owner_conversation_get', 'owner_conversation_related', 'tables_get', 'tasks_get']);
    assert.deepEqual(calls.filter(call => call.method !== 'GET' && call.path !== '/api/root/attention-queue' && !/viewbox\/dismiss$/.test(call.path) && !readOperations.has(call.path.split('/').at(-1))), [], 'navigation sends no operational mutations');
    assert.deepEqual(errors, []);
    console.log('Workspace navigation: paired private thread selection and Back, exact frame/scope checks, Signoff queue, Viewbox links, stable drafts, registry sync and narrow layouts passed.');
  } finally { for (const hold of pendingReads) hold.release(); await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
