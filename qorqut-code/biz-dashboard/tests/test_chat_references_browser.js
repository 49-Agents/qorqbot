#!/usr/bin/env node
// Real Chromium with the real dashboard scripts over a synthetic local server: typed Chat references (request_user_input) —
// inline Signoff (existing single-item view and decisions), actual iframe browser, canonical Task, Agent and Project
// chips and unavailable targets. A Project reference is informational: it creates no channel and sends no message.
// No live API, login, Signoff decision or browser: every viewer is a local synthetic page.
//   PLAYWRIGHT_MODULE=/path/to/playwright CHROMIUM_EXECUTABLE=/path/to/chrome node tests/test_chat_references_browser.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const ROOT = path.resolve(__dirname, '..');
const SHOT = process.env.CHAT_REFERENCES_SCREENSHOT_DIR || '';
const clone = value => JSON.parse(JSON.stringify(value));
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/signoffs.json')));
const BIZ = fixture.data.review_business_id, OWNER = '40000000-0000-4000-8000-000000000001';
const SIGNOFF = fixture.data.items[0].id, SIGNOFF2 = fixture.data.items[1]?.id || SIGNOFF;
const TASK = '60000000-0000-4000-8000-000000000001', MISSING = '60000000-0000-4000-8000-0000000000ff';
const AGENT = 'ordubashy-agent';
const envelope = data => ({request_id: crypto.randomUUID(), status: 'ok', data, warnings: []});
let records, posts, chat, mode, requests, apiLog;
let privateMessages = [], privateSends = [];
const INPUT = '30000000-0000-4000-8000-0000000000a1', STRICT = '30000000-0000-4000-8000-0000000000a2';
const reset = () => {
  records = {[SIGNOFF]: clone(fixture.data.items[0])};
  records[INPUT] = {...clone(fixture.data.items[0]), id: INPUT, title: 'Choose the support address', input: {explanation: 'Which address should customers see?', selection_mode: 'single', selection_required: true, notes_required: false,
    choices: [{id: 'a', label: 'support@', explanation: 'Use the support mailbox'}, {id: 'b', label: 'help@', explanation: 'Keep the help mailbox'}]}};
  records[STRICT] = {...clone(fixture.data.items[0]), id: STRICT, title: 'Change the billing address', rejection_requires_message: true};
  posts = []; mode = {}; requests = []; apiLog = [];
  chat = null;
};
const reply = (res, body, status = 200) => { res.writeHead(status, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}); res.end(JSON.stringify(body)); };
const fail = (res, code, message, status, details = {}) => reply(res, {request_id: crypto.randomUUID(), status: 'error', error: {code, message, retryable: false, details}}, status);
const entry = (n, text, references = [], extra = {}) => ({id: 'id' + n, kind: 'message', speaker: 'agent', text, timestamp: '2026-10-18T10:0' + n + ':00Z', position: n * 100, partial: false,
  identity: 'native_id', completion_sequence: null, completion_id: null, system: null, references, ...extra});
const chatState = entries => ({agent_id: AGENT, name: 'Ordubashy', session: {state: 'live', reason: 'live', can_send: true, working: false, provider: 'claude', conversation_id: 'c1', transport: 'terminal', detail: 'Live in Terminal',
  binding: {binding_id: 'b1', agent_id: AGENT, provider: 'claude', conversation_id: 'c1', generation: 1, transport: 'terminal'}}, revision: 1, cursor: 1,
  conversation: {conversation_id: 'c1', provider: 'claude', complete: true, finalized: false, error: null, captured_length: 1}, conversations: [{conversation_id: 'c1', provider: 'claude', updated_at: '2026-10-18T10:00:00Z', current: true}],
  entries, older: null, rendered_boundary: 0, inputs: [], actions: [], last_read_sequence: 0, newest_sequence: 0});
async function handle(req, res, origin) {
  const url = new URL(req.url, origin), route = url.pathname;
  if (route.startsWith('/api/')) apiLog.push(route);
  if (route.startsWith('/viewer/')) { requests.push(route); res.writeHead(200, {'Content-Type': 'text/html'}); return res.end(`<html><body style="margin:0;background:#fff;font:14px sans-serif;color:#202020;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;gap:8px"><strong style="font-size:20px;font-weight:500">Outlook</strong><span style="color:#666">Sign in to your account</span><span style="border:1px solid #ccc;padding:8px 40px">demo@example.com</span><i hidden>${route}</i><script>window.name='loaded-at-'+Date.now()</script></body></html>`); }
  if (route === '/api/session') return reply(res, envelope({principal_id: OWNER, principal_kind: 'human', display_name: 'Owner', is_owner: true}));
  if (route === '/api/biz') return reply(res, [{id: BIZ, slug: 'alpha', name: 'Atlas Research', owner: 'owner', agents: [], agent_meta: {}}]);
  if (route.endsWith('/operations/tasks_get')) {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw);
    if (body.task_id === TASK) return reply(res, envelope({task: {id: TASK, title: 'Connect support inbox', task_type: 'task', status: 'waiting', customer_profile:{email:'support@example.test',company:'Example Support'}}}));
    if (String(body.task_id || '').startsWith('60000000-0000-4000-8000-00000000002')) return reply(res, envelope({task: {id: body.task_id, title: 'Task ' + (Number(body.task_id.slice(-1)) + 1), task_type: 'task', status: 'waiting', ...(body.task_id === '60000000-0000-4000-8000-000000000020' ? {customer_profile:{email:'first@example.test',company:'First Company'}} : {})}}));
    return fail(res, 'not_found', 'Task not found.', 404);
  }
  if (/\/operations\/owner_conversation_/.test(route)) {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw), operation = route.split('/').at(-1);
    if (operation === 'owner_conversation_send') {
      privateSends.push({route, body});
      privateMessages.push({id: 'comment-' + privateMessages.length, sequence: privateMessages.length + 1,
        body: body.text, created_by: OWNER, author_kind: 'human', is_owner: true, created_at: '2026-10-27T14:00:00Z'});
    }
    return reply(res, envelope({conversation: {id: 'private-' + body.source.id, source: body.source,
      revision: 1, latest_sequence: privateMessages.length, message_count: privateMessages.length},
      messages: privateMessages, participants: [], next_before: null,
      read_state: {last_read_sequence: privateMessages.length, unread: false}}));
  }
  let match = /^\/api\/(?:root|biz\/alpha)\/signoffs\/([^/]+)(?:\/(approve|reject|respond))?$/.exec(route);
  if (match) {
    const item = records[match[1]];
    if (!item) return fail(res, 'not_found', 'Proposal not found.', 404);
    if (!match[2]) return reply(res, envelope({signoff: clone(item)}));
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw); posts.push({id: match[1], action: match[2], body});
    if (mode.conflict) { item.state = 'rejected'; item.status_revision++; return fail(res, 'stale_signoff', 'This proposal has already been rejected.', 409, {signoff: {id: item.id, state: item.state, status_revision: item.status_revision}}); }
    item.state = match[2] === 'reject' ? 'rejected' : match[2] === 'respond' ? 'answered' : 'approved'; item.status_revision++;
    item.decision = {kind: match[2], by: OWNER, at: '2026-10-18T10:00:00Z', feedback: body.feedback};
    return reply(res, envelope({signoff: clone(item), replayed: false}), match[2] === 'reject' ? 200 : 202);
  }
  match = /^\/api\/root\/agents\/([^/]+)\/chat(\/[a-z]+)?$/.exec(route);
  if (match) {
    if (!match[2]) return reply(res, {status: 'ok', data: chat});
    if (match[2] === '/read') return reply(res, {status: 'ok', data: {last_read_sequence: 0, newest_sequence: 0}});
  }
  if (route.startsWith('/api/')) return fail(res, 'not_found', 'No fixture.', 404);
  const target = path.resolve(ROOT, route === '/' ? 'index.html' : route.slice(1));
  if (!target.startsWith(ROOT + path.sep)) { res.writeHead(403); return res.end(); }
  try { const body = fs.readFileSync(target); res.writeHead(200, {'Content-Type': target.endsWith('.css') ? 'text/css' : target.endsWith('.js') ? 'text/javascript' : 'text/html'}); res.end(body); }
  catch { res.writeHead(404); res.end(); }
}
let checks = 0;
const check = async (label, action) => { await action(); checks++; console.log('ok ' + label); };
(async () => {
  reset();
  const server = http.createServer((req, res) => handle(req, res, origin).catch(error => { console.error(error); if (!res.headersSent) res.writeHead(500); res.end(); }));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const launch = {headless: true, args: ['--no-sandbox']};
  if (process.env.CHROMIUM_EXECUTABLE) launch.executablePath = process.env.CHROMIUM_EXECUTABLE;
  const browser = await chromium.launch(launch);
  try {
    const context = await browser.newContext({viewport: {width: 900, height: 1300}, locale: 'en-US', timezoneId: 'America/La_Paz'});
    // Isolated fixture: only the synthetic loopback origin is served, so external fonts and assets never load.
    await context.route('**/*', route => { const url = new URL(route.request().url()); return url.hostname === '127.0.0.1' ? route.continue() : route.abort(); });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin + '/?fixture=1#alpha');
    await page.waitForFunction(() => typeof mountGlobalChat === 'function' && typeof sgCreateView === 'function');
    await page.evaluate(() => {
      document.head.insertAdjacentHTML('beforeend', '<style>body>*:not(#pane):not(script){display:none!important}</style>');
      document.body.insertAdjacentHTML('beforeend', '<div id="pane" style="position:fixed;inset:0;z-index:5;background:var(--bg);display:flex;flex-direction:column"></div>');
      window.sources = [];
      window.EventSource = class { constructor() { this.readyState = 1; this.l = {}; window.sources.push(this); } addEventListener(type, fn) { (this.l[type] ||= []).push(fn); } close() { this.readyState = 2; } emit(type, data) { (this.l[type] || []).forEach(fn => fn({data: JSON.stringify(data)})); } };
    });
    const refs = [
      {type: 'signoff', id: SIGNOFF, business_id: BIZ, business_slug: 'alpha', label: 'Update agent', available: true},
      {type: 'browser', id: 'outlook-browser', business_id: null, label: 'Outlook browser', viewer_url: origin + '/viewer/outlook', available: true},
      {type: 'task', id: TASK, business_id: null, business_slug: null, label: 'Connect support inbox', state: 'waiting', available: true},
      {type: 'task', id: MISSING, business_id: null, business_slug: null, label: 'Unavailable task', state: '', available: false},
      {type: 'browser', id: 'gone', business_id: null, label: 'Unavailable browser', available: false},
    ];
    const agentRef = (extra = {}) => ({type: 'agent', id: 'agent-uuid-1', business_id: null, label: 'Marketing Lead', role: {id: 'marketing'}, presentation: {}, available: true, ...extra});
    const signoffRef = (id = SIGNOFF, extra = {}) => ({...refs[0], id, ...extra});
    const mount = async (entries, withOpenReference = false) => {
      chat = chatState(entries);
      await page.evaluate(withOpenReference => {
        window.chat?.dispose();
        window.openedReferences = [];
        const options = withOpenReference ? {openReference: ref => window.openedReferences.push(ref)} : {};
        window.chat = mountGlobalChat(document.querySelector('#pane'), {agent_id: 'ordubashy-agent', display_name: 'Ordubashy'}, options);
      }, withOpenReference);
      await page.waitForFunction(() => !document.querySelector('.gchat-session[data-state="loading"]'));
    };
    const reread = async entries => {
      chat = chatState(entries); chat.cursor = chat.revision = (reread.n = (reread.n || 1) + 1);
      await page.evaluate(n => window.sources.at(-1).emit('chat', {cursor: n, kind: 'changed', data: {revision: n, newest_sequence: 0, waiting: 0}}), chat.cursor);
      await page.waitForTimeout(250);
    };
    const msg = id => page.locator(`.gchat-msg[data-id="${id}"]`);
    const card = id => msg(id).locator('.gchat-signoff-reference');
    const views = () => page.evaluate(() => sgViews.size);
    const baseViews = await views();

    await check('mixed references render under the message; resolved Task opens through the supplied callback, missing targets are unavailable', async () => {
      await mount([entry(1, 'Please check these.', refs), entry(2, 'Notes with a file [report.txt](</absolute/path/to/qorqut/operations/agents/ordubashy/downloads/report.txt>)')], true);
      await msg('id1').locator('.sg-row').first().waitFor();
      assert.equal(await msg('id1').locator('.gchat-typed .gchat-browser-frame').count(), 1);
      const task = msg('id1').locator('.gchat-task-reference');
      assert.equal(await task.evaluate(el => el.tagName + el.querySelectorAll('a,button').length), 'BUTTON0');
      assert.equal(await task.locator('.gchat-reference-title').textContent(), 'Connect support inbox');
      assert.equal(await task.locator('.gchat-task-state').count(), 0, 'Task state is not presented as CRM stage');
      assert.equal(await msg('id1').locator('.gchat-reference-unavailable').count(), 2);
      assert.equal(await msg('id1').locator('.gchat-reference-unavailable iframe').count(), 0);
      await task.click();
      await page.waitForFunction(() => window.openedReferences.length === 1);
      assert.deepEqual(await page.evaluate(() => window.openedReferences[0]), refs[2], 'the provided callback takes precedence over the fallback');
      assert.equal(await msg('id2').locator('.gchat-typed').count(), 0);
      assert.equal(await msg('id2').locator('.gchat-reference').count() > 0, true, 'file reference chip still renders');
      if (SHOT) await page.screenshot({path: path.join(SHOT, 'chat-references-desktop.png')});
    });
    await check('the Signoff card is compact by default: label, status, title and brief explanation with decision controls, no discussion or related-work requests', async () => {
      const box = await card('id1').boundingBox();
      assert(box.height < 640, 'compact card with its visible proposed content: ' + box.height);
      const text = (await card('id1').innerText()).replace(/\s+/g, ' ');
      assert.match(text, /Signoff Atlas Research/);
      assert.match(text, /Pending/);
      assert.match(text, /Update campaign research agent permissions/);
      assert.match(text, /Research needs permission/);
      assert.equal(await card('id1').locator('.sg-state time').count(), 0, 'no age');
      assert.equal(await card('id1').locator('.identity-avatar').count(), 0, 'no proposer portrait');
      assert.equal(await card('id1').locator('.sg-actionarea .sg-approve').isVisible(), true);
      assert.equal(await card('id1').locator('.sg-actionarea .sg-reject').isVisible(), true);
      assert.equal(await card('id1').locator('.sg-content').isVisible(), true, 'proposed content is visible before Details');
      assert.equal(await card('id1').locator('.sg-impact').count(), 0);
      assert.equal(await card('id1').locator('.sg-feedback').isVisible(), false);
      assert.equal(await card('id1').locator('.sg-conversation textarea').isVisible(), true, 'comment composer is available without Details');
      assert.equal(await card('id1').locator('.conversation-toggle').count(), 1);
      assert.deepEqual(apiLog.filter(route => /conversation|related|work/.test(route)), [], 'no discussion or related-work requests');
    });
    await check('Details reveals effects and feedback without a scope block, then hides them again', async () => {
      const toggle = card('id1').getByRole('button', {name: 'Details'});
      await toggle.click();
      assert.equal(await card('id1').locator('.sg-impact').count(), 0);
      assert.equal(await card('id1').locator('.sg-effects').isVisible(), true);
      assert.equal(await card('id1').locator('.sg-feedback').isVisible(), true);
      assert.equal(await card('id1').getByRole('button', {name: 'Hide details'}).getAttribute('aria-expanded'), 'true');
      await card('id1').getByRole('button', {name: 'Hide details'}).click();
      assert.equal(await card('id1').locator('.sg-impact').count(), 0);
    });
    await check('the browser is a real iframe with the exact viewer URL, no-referrer and clipboard allowances', async () => {
      const frame = msg('id1').locator('iframe.gchat-browser-frame');
      assert.equal(await frame.getAttribute('src'), origin + '/viewer/outlook');
      assert.equal(await frame.getAttribute('referrerpolicy'), 'no-referrer');
      assert.match(await frame.getAttribute('allow'), /clipboard-read/);
      const box = await msg('id1').locator('.gchat-browser').boundingBox();
      assert(box.width <= 360.5 && box.width > 300, 'about 360px: ' + box.width);
      assert.equal(Math.round((await frame.boundingBox()).height), 210);
      assert.equal(await page.evaluate(() => document.querySelectorAll('#pane iframe').length), 1);
      if (SHOT) await page.screenshot({path: path.join(SHOT, 'chat-references-desktop.png')});
    });
    await check('approve posts one exact decision: proposal_revision, payload_hash, status_revision and an idempotency key', async () => {
      await card('id1').locator('.sg-actionarea .sg-approve').click();
      await page.waitForFunction(() => !document.querySelector('.gchat-signoff-reference .sg-approve'));
      assert.equal(posts.length, 1);
      const item = fixture.data.items[0];
      assert.equal(posts[0].action, 'approve');
      assert.equal(posts[0].body.proposal_revision, item.proposal_revision);
      assert.equal(posts[0].body.payload_hash, item.payload_hash);
      assert.equal(posts[0].body.expected_status_revision, item.status_revision);
      assert.match(posts[0].body.idempotency_key, /^[0-9a-f-]{36}$/);
      assert.match(await card('id1').locator('.sg-state').innerText(), /Approved/);
    });
    await check('a stale decision reports the authoritative conflict instead of approving from transcript data', async () => {
      reset(); mode.conflict = true;
      await mount([entry(1, 'Please check.', [signoffRef()])]);
      await card('id1').locator('.sg-actionarea .sg-approve').waitFor();
      await card('id1').locator('.sg-actionarea .sg-approve').click();
      await page.waitForFunction(() => /rejected/i.test(document.querySelector('.gchat-signoff-reference').innerText));
      assert.equal(posts.length, 1);
      assert.equal(await card('id1').locator('.sg-approve').count(), 0);
    });
    await check('reject sends the written feedback; a required rejection message reveals and focuses the existing feedback field and posts nothing', async () => {
      reset();
      await mount([entry(1, 'Please check.', [signoffRef(), signoffRef(STRICT)]), entry(2, 'x')]);
      const first = card('id1').first();
      await first.locator('.sg-actionarea .sg-reject').waitFor();
      await first.getByRole('button', {name: 'Details'}).click();
      await first.locator('.sg-feedback').fill('Not this one.');
      await first.locator('.sg-actionarea .sg-reject').click();
      await page.waitForFunction(() => document.querySelector('.gchat-signoff-reference .sg-state').innerText.includes('Rejected'));
      assert.deepEqual(posts.map(post => [post.action, post.body.feedback, post.body.expected_status_revision]), [['reject', 'Not this one.', 1]]);
      posts.length = 0;
      const strict = msg('id1').locator('.gchat-signoff-reference').nth(1);
      await strict.locator('.sg-actionarea .sg-reject').waitFor();
      assert.equal(await strict.locator('.sg-feedback').isVisible(), false);
      await strict.locator('.sg-actionarea .sg-reject').click();
      await page.waitForTimeout(200);
      assert.equal(await strict.locator('.sg-feedback').isVisible(), true);
      assert.equal(await strict.locator('.sg-feedback').evaluate(el => el === document.activeElement), true);
      assert.equal(posts.length, 0);
    });
    await check('an input Signoff exposes its choices and answer field by default and submits through the existing response', async () => {
      reset();
      await mount([entry(1, 'Which one?', [signoffRef(INPUT)])]);
      await card('id1').locator('.sg-choice').first().waitFor();
      assert.equal(await card('id1').locator('.sg-choice').count(), 2);
      assert.equal(await card('id1').locator('.sg-feedback').isVisible(), true);
      await card('id1').locator('.sg-choice').first().click();
      await card('id1').locator('.sg-actionarea [data-sg="respond"]').click();
      await page.waitForFunction(() => !document.querySelector('.gchat-signoff-reference [data-sg="respond"]'));
      assert.deepEqual(posts.map(post => [post.action, post.body.selected_choices]), [['respond', ['a']]]);
      assert.match(await card('id1').locator('.sg-state').innerText(), /Answered/);
    });
    await check('refresh, appended messages and Signoff updates keep the same iframe node and viewer session', async () => {
      reset();
      await mount([entry(1, 'Please check.', refs.slice(0, 3))]);
      await msg('id1').locator('.sg-row').first().waitFor();
      await page.waitForTimeout(150);
      await page.evaluate(() => { window.keptFrame = document.querySelector('.gchat-browser-frame'); window.keptRow = document.querySelector('.gchat-signoff-reference .sg-row'); window.keptTask = document.querySelector('.gchat-task-reference'); });
      const loads = requests.length;
      await reread([entry(1, 'Please check.', refs.slice(0, 3).map(ref => ref.type === 'task' ? {...ref, state: 'done'} : ref)), entry(2, 'A later message')]);
      assert.equal(await msg('id1').locator('.gchat-reference-title').textContent(), 'Connect support inbox', 'the canonical reference label remains stable');
      assert.equal(await msg('id1').locator('.gchat-task-state').count(), 0, 'Task status is not styled as a CRM stage');
      assert.equal(await page.evaluate(() => document.querySelector('.gchat-browser-frame') === window.keptFrame && keptFrame.isConnected), true);
      assert.equal(await page.evaluate(() => document.querySelector('.gchat-signoff-reference .sg-row') === window.keptRow), true);
      assert.equal(await page.evaluate(() => document.querySelector('.gchat-task-reference') === window.keptTask), true, 'task chip updates in place');
      assert.equal(requests.length, loads, 'no duplicate reference reads or viewer load');
    });
    await check('availability transitions: an available Signoff becomes an unavailable chip with its view disposed, then mounts again; Task and browser follow', async () => {
      reset();
      const full = refs.slice(0, 3);
      await mount([entry(1, 'Please check.', full)]);
      await msg('id1').locator('.sg-row').first().waitFor();
      const mounted = await views();
      await page.evaluate(() => { window.keptFrame = document.querySelector('.gchat-browser-frame'); });
      const gone = full.map(ref => ref.type === 'browser' ? ref : {...ref, available: false, label: 'Unavailable ' + ref.type});
      await reread([entry(1, 'Please check.', gone)]);
      assert.equal(await msg('id1').locator('.gchat-signoff-reference').count(), 0);
      assert.equal(await msg('id1').locator('.sg-approve').count(), 0);
      assert.equal(await views(), mounted - 1, 'the Signoff view is disposed');
      assert.equal(await msg('id1').locator('.gchat-reference-unavailable').count(), 2);
      assert.equal(await msg('id1').locator('.gchat-task-reference').count(), 0);
      assert.equal(await page.evaluate(() => document.querySelector('.gchat-browser-frame') === window.keptFrame), true, 'unrelated iframe is untouched');
      await reread([entry(1, 'Please check.', full)]);
      await msg('id1').locator('.sg-row').first().waitFor();
      assert.equal(await views(), mounted);
      assert.equal(await msg('id1').locator('.gchat-reference-title').textContent(), 'Connect support inbox');
      assert.equal(await msg('id1').locator('.gchat-task-state').count(), 0);
      assert.equal(await page.evaluate(() => document.querySelector('.gchat-browser-frame') === window.keptFrame), true);
    });
    await check('a changed Signoff route (business_slug) remounts the Signoff without touching the iframe', async () => {
      await page.evaluate(() => { window.keptFrame = document.querySelector('.gchat-browser-frame'); window.keptRow = document.querySelector('.gchat-signoff-reference .sg-row'); });
      await reread([entry(1, 'Please check.', [signoffRef(SIGNOFF, {business_slug: 'beta'}), ...refs.slice(1, 3)])]);
      assert.equal(await page.evaluate(() => window.keptFrame === document.querySelector('.gchat-browser-frame')), true);
      assert.equal(await page.evaluate(() => window.keptRow === document.querySelector('.gchat-signoff-reference .sg-row')), false);
    });
    await check('standalone Task references enrich from canonical scoped reads and retain verified profile details on reconciliation', async () => {
      reset();
      const states = ['waiting', 'open', 'working', 'done', 'cancelled', 'snoozed'];
      const taskRefs = states.map((state, index) => ({type: 'task', id: '60000000-0000-4000-8000-00000000002' + index, business_id: null, business_slug: null, label: `Task ${index + 1}`, state, available: true}));
      await mount([entry(1, 'Task references.', taskRefs)]);
      const chips = msg('id1').locator('.gchat-task-reference');
      assert.equal(await chips.count(), states.length);
      assert.equal(await chips.locator('.gchat-task-state, .gchat-reference-state-hidden').count(), 0, 'state does not appear as a CRM stage');
      assert.equal(await chips.first().locator('.gchat-reference-title').innerText(), 'Task 1');
      await chips.first().locator('.gchat-reference-detail').filter({hasText:'first@example.test'}).waitFor();
      assert.equal(await chips.first().locator('.gchat-reference-detail').innerText(), 'first@example.test · First Company', 'the authorized Task read supplies email and company');
      assert.equal(apiLog.filter(route => route.endsWith('/operations/tasks_get')).length, states.length, 'each canonical Task is read once for display metadata');
      await page.evaluate(() => { window.keptTaskChip = document.querySelector('.gchat-task-reference'); });
      const reads = apiLog.filter(route => route.endsWith('/operations/tasks_get')).length;
      await reread([entry(1, 'Task references.', taskRefs.map((ref, index) => index ? ref : {...ref, state: 'done'}))]);
      const first = msg('id1').locator('.gchat-task-reference').first();
      assert.equal(await page.evaluate(() => document.querySelector('.gchat-task-reference') === window.keptTaskChip), true, 'the chip updates in place');
      assert.equal(await first.locator('.gchat-reference-title').innerText(), 'Task 1', 'the canonical title stays stable as the hidden status changes');
      assert.equal(await first.locator('.gchat-reference-detail').innerText(), 'first@example.test · First Company', 'verified metadata survives refresh reconciliation');
      assert.equal(apiLog.filter(route => route.endsWith('/operations/tasks_get')).length, reads, 'refresh reconciliation reuses the bounded metadata read');
      await reread([entry(1, 'Task references.', [{...taskRefs[0], available: false, label: 'Unavailable task'}])]);
      const unavailable = msg('id1').locator('.gchat-reference-unavailable');
      assert.equal(await unavailable.count(), 1);
      assert.equal(await unavailable.locator('.gchat-task-state, .gchat-reference-state-hidden').count(), 0, 'an unavailable Task keeps no state presentation');
      assert.equal((await unavailable.innerText()).trim(), 'Unavailable reference');
      assert.equal(await unavailable.evaluate(el => el.tagName), 'SPAN', 'unavailable references are noninteractive');
    });
    await check('missing, blank, relative, credentialed and non-http viewer URLs never create an iframe', async () => {
      const bad = [undefined, '', '/relative/page', 'http://user:pass@host.test/', 'not a url', 'javascript:alert(1)', 'data:text/html,hi', 'ftp://host.test/'];
      await mount([entry(1, 'Please check.', bad.map((url, i) => ({...refs[1], id: 'b' + i, viewer_url: url})))]);
      assert.equal(await page.locator('#pane iframe').count(), 0);
      assert.equal(await page.locator('#pane .gchat-reference-unavailable').count(), bad.length);
    });
    await check('valid DTO width/height fit inside the card without cropping, centered, and expanding keeps the same frame', async () => {
      await mount([entry(1, 'Please check.', [{...refs[1], width: 1280, height: 720}])]);
      await msg('id1').locator('iframe').waitFor();
      await page.waitForTimeout(100);
      await page.evaluate(() => { window.keptFrame = document.querySelector('.gchat-browser-frame'); });
      const page1 = await msg('id1').locator('.gchat-browser-page').boundingBox(), frame1 = await msg('id1').locator('iframe').boundingBox();
      assert(Math.abs(frame1.width / frame1.height - 16 / 9) < 0.03, 'aspect preserved');
      assert(frame1.width <= page1.width + 1 && frame1.height <= page1.height + 1, 'fits within the card');
      assert(Math.abs((frame1.x - page1.x) - (page1.x + page1.width - frame1.x - frame1.width)) < 2, 'centered');
      const loads = requests.length;
      await msg('id1').getByRole('button', {name: 'Expand browser'}).click();
      await page.waitForTimeout(150);
      const frame2 = await msg('id1').locator('iframe').boundingBox();
      assert(frame2.width > frame1.width * 1.5 && Math.abs(frame2.width / frame2.height - 16 / 9) < 0.03, 'refit in place');
      assert.equal(await page.evaluate(() => document.querySelector('.gchat-browser-frame') === window.keptFrame), true);
      assert.equal(requests.length, loads);
      await page.keyboard.press('Escape');
    });
    await check('expanding resizes the same iframe in place; closing returns it inline; Escape closes', async () => {
      reset();
      await mount([entry(1, 'Please check.', [refs[1]]), entry(2, 'after')]);
      await msg('id1').locator('iframe').waitFor();
      await page.waitForTimeout(150);
      await page.evaluate(() => { window.keptFrame = document.querySelector('.gchat-browser-frame'); });
      const loads = requests.length, inline = await msg('id1').locator('.gchat-browser-slot').boundingBox();
      await msg('id1').getByRole('button', {name: 'Expand browser'}).click();
      assert.equal(await msg('id1').locator('.gchat-browser').getAttribute('role'), 'dialog');
      const big = await msg('id1').locator('iframe').boundingBox();
      assert(big.height > 400 && big.width > 700, JSON.stringify(big));
      assert.equal(Math.round((await msg('id1').locator('.gchat-browser-slot').boundingBox()).height), Math.round(inline.height), 'inline slot keeps its place');
      if (SHOT) await page.screenshot({path: path.join(SHOT, 'chat-references-expanded.png')});
      await page.keyboard.press('Escape');
      assert.equal(await msg('id1').locator('.gchat-browser').getAttribute('role'), null);
      assert.equal(Math.round((await msg('id1').locator('iframe').boundingBox()).height), 210);
      assert.equal(await page.evaluate(() => document.querySelector('.gchat-browser-frame') === window.keptFrame), true);
      await page.waitForTimeout(150);
      assert.equal(requests.length, loads, 'expanding never reloads the viewer');
      assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), 'Expand browser');
    });
    await check('an agent reference is a noninteractive chip with the shared identity icon and current name; metadata updates in place, unavailable transitions, sibling iframe untouched', async () => {
      reset();
      await mount([entry(1, 'Talk to them.', [refs[1], agentRef()])]);
      await msg('id1').locator('iframe').waitFor();
      const chip = msg('id1').locator('.gchat-agent-reference');
      assert.equal(await chip.locator('.gchat-agent-icon svg').count() > 0, true, 'identity glyph rendered');
      assert.equal((await chip.innerText()).trim(), 'Marketing Lead');
      assert.equal(await chip.evaluate(el => el.tagName + el.querySelectorAll('a,button,[tabindex]').length + getComputedStyle(el).cursor), 'SPAN0default');
      assert.equal(await chip.locator('small').count(), 0, 'no status or role badge');
      await page.evaluate(() => { window.keptFrame = document.querySelector('.gchat-browser-frame'); window.keptChip = document.querySelector('.gchat-agent-reference'); });
      const loads = requests.length, before = await chip.locator('.gchat-agent-icon').innerHTML();
      await reread([entry(1, 'Talk to them.', [refs[1], agentRef({label: 'Growth Lead', role: {id: 'ordubashy'}})])]);
      assert.equal((await chip.innerText()).trim(), 'Growth Lead');
      assert.notEqual(await chip.locator('.gchat-agent-icon').innerHTML(), before, 'icon follows role/presentation');
      assert.equal(await page.evaluate(() => document.querySelector('.gchat-agent-reference') === window.keptChip), true, 'updated in place');
      await reread([entry(1, 'Talk to them.', [refs[1], agentRef({available: false, label: 'Unavailable agent', role: undefined, presentation: undefined})])]);
      assert.equal(await msg('id1').locator('.gchat-agent-reference').count(), 0);
      assert.equal(await msg('id1').locator('.gchat-reference-unavailable .gchat-agent-icon').count(), 0);
      assert.equal((await msg('id1').locator('.gchat-reference-unavailable').innerText()).trim(), 'Unavailable reference');
      await reread([entry(1, 'Talk to them.', [refs[1], agentRef()])]);
      assert.equal((await msg('id1').locator('.gchat-agent-reference').innerText()).trim(), 'Marketing Lead');
      assert.equal(await page.evaluate(() => document.querySelector('.gchat-browser-frame') === window.keptFrame), true, 'sibling iframe untouched');
      assert.equal(requests.length, loads);
      if (SHOT) await page.screenshot({path: path.join(SHOT, 'agent-references.png')});
    });
    await check('a Project is a noninteractive typed reference with the existing Project icon; it never creates a channel and handles unavailability', async () => {
      reset();
      const projectRef = {type: 'project', id: '70000000-0000-4000-8000-000000000001', business_id: '70000000-0000-4000-8000-000000000001', label: 'Atlas Launch', available: true};
      await mount([entry(1, 'The pinned Project.', [projectRef])]);
      const chip = msg('id1').locator('.gchat-project-reference');
      await chip.waitFor();
      assert.equal((await chip.innerText()).trim(), 'Atlas Launch');
      assert.equal(await chip.locator('.gchat-project-icon svg').count(), 1, 'the existing Project icon marks the reference');
      assert.equal(await chip.evaluate(el => el.tagName + el.querySelectorAll('a,button,[tabindex]').length + getComputedStyle(el).cursor), 'SPAN0default', 'informational, never a private-thread opener');
      assert.deepEqual(privateSends, [], 'a typed Project reference sends nothing');
      await reread([entry(1, 'The pinned Project.', [{...projectRef, available: false, label: 'Unavailable project'}])]);
      assert.equal(await msg('id1').locator('.gchat-project-reference').count(), 0);
      assert.equal((await msg('id1').locator('.gchat-reference-unavailable').innerText()).trim(), 'Unavailable reference');
      await reread([entry(1, 'The pinned Project.', [projectRef])]);
      assert.equal(await msg('id1').locator('.gchat-project-reference').count(), 1, 'availability restores the same typed chip');
    });
    await check('narrow layout stays within the pane', async () => {
      await page.setViewportSize({width: 380, height: 800});
      await mount([entry(1, 'Please check.', refs.slice(0, 3))]);
      await msg('id1').locator('iframe').waitFor();
      await msg('id1').locator('.sg-row').first().waitFor();
      const overflow = await page.evaluate(() => [...document.querySelectorAll('#pane .gchat-typed *')].filter(el => el.getBoundingClientRect().right > innerWidth + 1).length);
      assert.equal(overflow, 0);
      if (SHOT) await page.screenshot({path: path.join(SHOT, 'chat-references-narrow.png')});
      await page.setViewportSize({width: 900, height: 1300});
    });
    await check('WhatsApp and email inline comments use the record channel without deciding or sending externally', async () => {
      for (const [index, operation] of ['whatsapp_send', 'account_email_send'].entries()) {
        const identity = '30000000-0000-4000-8000-00000000006' + index;
        const original = clone(fixture.data.items[0]);
        records[identity] = {...original, id: identity, operation_name: operation, review_business_id: index ? null : BIZ};
        privateMessages = []; privateSends = [];
        await mount([entry(1, 'Review this message.', [signoffRef(identity, {business_id: index ? null : BIZ, business_slug: index ? null : 'alpha'})])]);
        const composer = card('id1').locator('.sg-conversation textarea');
        await composer.waitFor();
        const text = 'Please shorten this draft before sending.';
        await composer.fill(text);
        await reread([entry(1, 'Review this message.', [signoffRef(identity, {business_id: index ? null : BIZ, business_slug: index ? null : 'alpha'})])]);
        assert.equal(await composer.inputValue(), text, 'draft survives chat refresh');
        const decisionsBefore = posts.length;
        await card('id1').locator('.sg-conversation .gchat-send').click();
        await page.waitForFunction(() => document.querySelector('.sg-conversation textarea')?.value === '');
        assert.equal(privateSends.length, 1);
        assert.equal(privateSends[0].route, '/api/' + (index ? 'root' : 'biz/alpha') + '/operations/owner_conversation_send');
        assert.deepEqual(privateSends[0].body.source, {type: 'signoff', id: identity});
        assert.equal(privateSends[0].body.text, text);
        assert.match(privateSends[0].body.idempotency_key, /^[0-9a-f-]{36}$/);
        assert.equal(posts.length, decisionsBefore, 'comment is independent of approval/rejection');
        assert.equal(records[identity].state, original.state);
        await card('id1').locator('.conversation-toggle').click();
        await card('id1').locator('.sg-conversation .gchat-body').filter({hasText:text}).waitFor();
      }
    });
    await check('teardown disposes iframes and Signoff views', async () => {
      await mount([entry(1, 'Please check.', refs.slice(0, 3))]);
      await msg('id1').locator('iframe').waitFor();
      await page.evaluate(() => { window.frameRef = document.querySelector('.gchat-browser-frame'); window.chat.dispose(); });
      assert.equal(await page.locator('iframe').count(), 0);
      assert.equal(await page.evaluate(() => window.frameRef.isConnected), false);
      assert.equal(await views(), baseViews, 'every Signoff view is disposed');
    });
    assert.deepEqual(errors, []);
    console.log(`${checks} checks passed`);
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
