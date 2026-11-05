// Native workspace shell and screens against a fixture backend. No live records,
// terminal process or agent model is touched by this test.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const profiles = [['ordu', 'Ordubashy', 'ordubashy'], ['marketing', 'Marketing', 'global-worker'], ['founder', 'Founder', 'founder'], ['learning', 'Learning Applier', 'learning-applier']]
  .map(([agent_id, display_name, role]) => ({agent_id, name: agent_id, display_name, status: 'active', role: {id: role}, presentation: {department: 'operations'}, scope: {kind: 'ecosystem', allowed_business_ids: ['biz-a', 'biz-b'], global_capabilities: []}}));
const businesses = [{id: 'biz-a', slug: 'widget', name: 'Widget', agents: ['dev']}, {id: 'biz-b', slug: 'second', name: 'Second', agents: ['dev']}];
const states = ['open', 'working', 'waiting', 'done', 'cancelled'];
const board = {revision: 1, items: states.map((state, i) => ({id: 'focus-' + i, revision: 1, label: state, state,
  source: {type: 'task', id: 'task-' + i, business_id: 'biz-a'}, source_revision: 1,
  resolved_source: {type: 'task', id: 'task-' + i, business_id: 'biz-a', business_slug: 'widget', business_name: 'Widget', label: state, state}, references: []}))};
board.items.push({id:'focus-parent', revision:1, label:'Launch preparation', state:'open', source:{type:'task',id:'task-parent',business_id:'biz-a'}, source_revision:1,
  resolved_source:{type:'task',id:'task-parent',business_id:'biz-a',business_slug:'widget',business_name:'Widget',label:'Launch preparation',state:'open'}, references:[]});
const signoff={...JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/signoffs.json'),'utf8')).data.items[0],id:'signoff-choice',review_business_id:null,category:'input_request',title:'Choose the launch date',
  input:{title:'Choose the launch date',explanation:'Can the launch proceed?',kind:'question',selection_mode:'single',choices:[{id:'yes',label:'Yes'},{id:'hold',label:'Hold'}],selection_required:true,notes_required:false}};
board.items.push({id:'focus-signoff',revision:1,label:signoff.title,display_name:'Launch decision',source:{type:'signoff',id:signoff.id,business_id:null},state:'pending'});
const signoffAnswers=[];
const calls = [];
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  // Production forbids navigating an iframe to the dashboard document.
  res.setHeader('X-Frame-Options', 'DENY');
  res.setHeader('Content-Security-Policy', "frame-ancestors 'none'");
  if (url.pathname.startsWith('/api/')) {
    calls.push(url.pathname);
    let data = {items: [], messages: [], purposes: [], tags: [], agents: [], totals: {}};
    if (url.pathname === '/api/biz') data = businesses;
    else if (/\/biz\/[^/]+\/agents/.test(url.pathname)) data = [];
    else if (url.pathname === '/api/session') data = {principal_id: 'owner', principal_kind: 'human', is_owner: true, is_installation_owner: true};
    else if (url.pathname === '/api/root/signoffs') data={items:signoff.state==='pending'?[signoff]:[],next_cursor:null};
    else if (url.pathname === '/api/root/signoffs/signoff-choice') data={signoff};
    else if (url.pathname === '/api/root/signoffs/signoff-choice/respond') {
      let raw='';for await(const chunk of req)raw+=chunk;signoffAnswers.push(JSON.parse(raw));
      signoff.state='answered';signoff.status_revision++;data={signoff};
    }
    else if (url.pathname === '/api/root/agents') data = {agents: profiles};
    else if (url.pathname === '/api/root/agent-pins') data = {initialized: true, pinned_agents: []};
    else if (url.pathname.endsWith('/focus_get')) data = board;
    else if (url.pathname.endsWith('/workspace_types')) data = {types: [{key: 'focus', display_name: 'Focus', supported_roles: ['ordubashy', 'global-worker'], renderer: 'focus_board'}]};
    else if (url.pathname.endsWith('/tasks_list')) data = {tasks:[{id:'child-task',title:'Review the homepage',status:'waiting',revision:1,archived:false,closed:false}],next_cursor:null};
    else if (url.pathname.endsWith('/relationships_list')) data = {items:[],next_cursor:null};
    else if (url.pathname.endsWith('/global_progress_get')) data = {series:Array.from({length:365}, (_,i) => ({date:new Date(Date.UTC(2026,0,1+i)).toISOString().slice(0,10),tasks_done:i%9,inbox_messages:i%16,learnings_accepted:i%3,new_tickets:i%5,cold_outreach_emails_sent:i%8}))};
    else if (url.pathname.endsWith('/browser-surfaces')) data = {surfaces: []};
    else if (/\/agents\/[^/]+\/chat$/.test(url.pathname)) data = {agent_id:url.pathname.split('/')[4], session: {state: 'live', reason: 'live', can_send: true, working: false, provider: 'claude', conversation_id: 'conv-1', transport: 'terminal', detail: 'Live', binding: {binding_id: 'b1', agent_id: 'a', provider: 'claude', conversation_id: 'conv-1', generation: 1, transport: 'terminal'}}, revision: 0, cursor: 0, conversation: {conversation_id: 'conv-1', provider: 'claude', complete: true, finalized: false, error: null, captured_length: 0}, conversations: [{conversation_id: 'conv-1', provider: 'claude', updated_at: null, current: true}], entries: [], older: null, rendered_boundary: 0, inputs: [], last_read_sequence: 0, newest_sequence: 0};
    res.setHeader('Content-Type', 'application/json');
    return res.end(JSON.stringify(url.pathname === '/api/biz' || /\/biz\/[^/]+\/agents/.test(url.pathname) ? data : {status: 'ok', data}));
  }
  const file = path.resolve(root, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1400, height: 900}}), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const childRequests = [];
    page.on('request', req => { if (req.frame() !== page.mainFrame()) childRequests.push(new URL(req.url()).pathname); });
    await page.addInitScript(() => { window.__streams = []; window.EventSource = class { constructor(url) { window.__streams.push(url); } addEventListener() {} close() {} }; });
    await page.goto(`http://127.0.0.1:${server.address().port}/#/global/agents`);
    await page.locator('.agfocus .ball.done').waitFor();
    const colors = await page.locator('.agfocus .ball').evaluateAll(elements => elements.map(el => getComputedStyle(el.querySelector('.core')).backgroundColor));
    assert.deepEqual(colors, ['rgba(0, 0, 0, 0)', 'rgb(245, 245, 240)', 'rgb(10, 132, 255)', 'rgb(48, 209, 88)', 'rgba(0, 0, 0, 0)', 'rgba(0, 0, 0, 0)']);
    assert.equal(await page.locator('#agpad .thead [data-focus-add]').count(),1,'Add sits beside pane tabs');
    assert.equal(await page.locator('[data-focus-refresh]').isVisible(),false,'secondary controls start tucked away');
    await page.locator('.agfocus-options summary').click();
    await page.getByRole('button',{name:'Archived',exact:true}).click();
    assert.equal(await page.getByRole('button',{name:'Archived',exact:true}).getAttribute('aria-pressed'),'true');
    await page.getByRole('button',{name:'Archived',exact:true}).click();
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('[data-focus-refresh]').isVisible(),false);
    assert.equal(await page.locator('.agfocus-options summary').evaluate(el=>el===document.activeElement),true);
    const story=page.locator('[data-card-id=focus-parent]');
    await story.getByRole('button',{name:'Show related work'}).click();
    const child=page.locator('[data-card-id="focus-parent/task/child-task"]');
    await child.waitFor();
    assert.equal(await child.locator('.agfocus-scope').isVisible(),false,'child Tasks omit the inherited business');
    assert.equal(await child.locator('.ball.waiting .core').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(10, 132, 255)');
    assert((await child.boundingBox()).x>(await story.boundingBox()).x,'child Tasks stay indented');
    await page.mouse.move(0,0);
    assert.equal(await story.locator('.agfocus-actions').evaluate(el=>getComputedStyle(el).opacity),'1','focused row actions remain keyboard accessible');
    await page.locator('.gchat-composer textarea').focus();
    assert.equal(await story.locator('.agfocus-actions').evaluate(el=>getComputedStyle(el).opacity),'0','row controls remain quiet until hover/focus');
    const row=page.locator('.agfocus-row').first();
    assert.equal(await row.evaluate(el=>getComputedStyle(el).borderBottomWidth),'0px','rows have no borders');
    assert.equal((await row.boundingBox()).height,32,'compact rows');
    assert.equal(await row.locator('.ttl').evaluate(el=>getComputedStyle(el).fontSize),'14.5px');
    assert.equal(await row.locator('.ttl').evaluate(el=>getComputedStyle(el).textTransform),'lowercase');
    assert.equal(await page.locator('[data-focus-grip],.agfocus-row [data-manage]').count(),0,'no drag handle or ellipsis');
    const spacing=await row.evaluate(el=>{const dot=el.querySelector('.agfocus-notification').getBoundingClientRect(),state=el.querySelector('.agfocus-marker').getBoundingClientRect();return state.left-dot.right-parseFloat(getComputedStyle(el).columnGap);});
    assert.equal(spacing,10,'10px extra after the reserved notification column');
    const scope=row.locator('.agfocus-scope'),title=row.locator('.task-headline');
    const beforeHover=await title.boundingBox();
    assert.equal(await scope.evaluate(el=>getComputedStyle(el).opacity),'0','business stays quiet until hover');
    await row.hover();
    assert.equal(await scope.evaluate(el=>getComputedStyle(el).opacity),'1');
    assert.deepEqual(await title.boundingBox(),beforeHover,'hover badge overlays without shifting or narrowing the title');
    const badge=await scope.boundingBox();
    assert(badge.x>beforeHover.x && badge.x+badge.width<=beforeHover.x+beforeHover.width+1,'business badge sits over the right end of the title');
    assert.equal(await scope.evaluate(el=>getComputedStyle(el).zIndex),'1');
    assert.equal(await row.evaluate(el=>el.getBoundingClientRect().left-el.closest('.tbody').getBoundingClientRect().left),10,'Focus rows have a single 10px left inset');
    const queue=await page.locator('.attention-queue').boundingBox(),firstRow=await row.boundingBox();
    assert(firstRow.y>=queue.y+queue.height+10,'attention keeps its compact gap above the board');
    await page.getByRole('button',{name:'Review Launch decision',exact:true}).waitFor();
    assert.equal(await page.locator('.focus-signoff-chip').evaluate(el=>getComputedStyle(el).borderWidth),'0px','Signoff chips are borderless');
    if(process.env.FOCUS_SCREENSHOT)await page.locator('.agpad').screenshot({path:process.env.FOCUS_SCREENSHOT});
    assert.equal(await page.locator('.agfocus-items [data-card-id="focus-signoff"]').count(),0,'Signoffs leave the task list for their own queue');
    assert.equal(await page.locator('.attention-queue [data-attention-key^="signoff:"]').count(),0);
    await page.getByRole('button',{name:'Review Launch decision',exact:true}).click();
    await page.locator('.attention-signoff-view [data-choice="yes"]').click();
    await page.locator('.attention-signoff-view [data-sg="respond"]').click();
    await page.waitForFunction(()=>!document.querySelector('.attention-signoff-view [data-sg="respond"]'));
    assert.equal(signoffAnswers.length,1,'the existing Signoff form submits once');
    assert.equal(signoffAnswers[0].proposal_revision,signoff.proposal_revision);
    assert.equal(signoffAnswers[0].payload_hash,signoff.payload_hash);
    await page.getByRole('button',{name:'Close',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('[data-signoff-key="signoff-choice"]'));
    // Project management and Paper Outreach left the right agent pane. Neither a stale descriptor nor a
    // direct request adds a tab back; project data stays reachable from the existing Charts page.
    const paneViews = () => page.evaluate(() => _agpad.views);
    assert.equal((await paneViews()).includes('business'), false, 'Ordubashy has no Project management entry');
    assert.equal((await paneViews()).includes('campaign_review'), false, 'no Paper Outreach entry');
    assert.deepEqual(await page.evaluate(() => ['business', 'campaign_review'].map(view => [agpadRequestView(_agpad, view), _agpad.views.includes(view)])),
      [[false, false], [false, false]], 'a direct or stale view request adds no pane tab');
    assert.equal(await page.getByRole('tab', {name: 'Project management', exact: true}).count(), 0);
    await page.evaluate(() => ovOpen('charts'));
    await page.locator('[data-open-business="widget"]').click();
    await page.locator('[data-business-task-page]').waitFor();
    assert.deepEqual(await page.evaluate(() => [state.scope, state.biz]), ['business', 'widget'], 'the Charts project opens its existing workspace');
    await page.evaluate(() => selectGlobal('agents'));
    await page.locator('#global-agent-list').getByRole('button', {name: 'Ordubashy', exact: true}).click();
    await page.locator('.agfocus').waitFor();
    assert.equal(await page.locator('.agent-workspace-frame').count(), 0, 'no embedded project screen remains');
    assert.equal(await page.locator('.agent-business-picker').count(), 0, 'no project selector remains in the pane header');
    const divider = page.locator('[data-agent-pane-divider]');
    await divider.focus(); await page.keyboard.press('ArrowRight');
    assert(await page.evaluate(() => localStorage.getItem('agent-pane-ratio')), 'divider persists its ratio');
    for (const width of [760, 390]) {
      await page.setViewportSize({width, height: 900});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert((await page.locator('.agpad').boundingBox()).width > 240);
      const clipped=await page.locator('.agfocus-row').evaluateAll(rows=>rows.some(row=>row.scrollWidth>row.clientWidth+1));
      assert.equal(clipped,false,'Focus rows fit narrow panes');
      assert.equal(await page.locator('.agfocus-row').first().evaluate(el=>el.getBoundingClientRect().left-el.closest('.tbody').getBoundingClientRect().left),10);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
      await page.getByRole('tab', {name:'Focus',exact:true}).click();
      await page.locator('.agfocus').waitFor();
    }
    assert.deepEqual(errors, []);
    console.log('Workspace: native Focus, retired pane entries, the Charts project entry point, stale view refusal and resizing passed.');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
