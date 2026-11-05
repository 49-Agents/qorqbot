#!/usr/bin/env node
// Real agpadMount / Focus surfaces / agpadSetView / tab controls with the Browser and
// Desktop viewers, over stub APIs and stub iframe targets (routing only; no live hosts).
// The shared record workspace, the Focus surfaces and the Today calendar are the app's
// own scripts in the dashboard's load order; only the network and the DOM shell are synthetic.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const requests = [], errors = [];
const core = fs.readFileSync(path.join(root, 'js/core.js'), 'utf8');
// Real identity/helpers and icon constants from core.js (the rest of core.js boots the whole app).
const identity = core.split('const TABS =')[0];
// The late core helpers the Focus board and the Today calendar share at runtime.
const lateCore = core.split('\n').filter(line => /^const (accessLost|sourceGone) = /.test(line)).join('\n');
const icons = core.split('\n').filter(line => /^const (ICO_|SVG_)\w+ = `/.test(line)).join('\n');
let origin, hold = null, focusPending = false, focusFail = false;
const config = {}, refused = new Set();
const surface = (id, label, kind) => ({id, label, kind, viewer_url: `${origin}/screen/${id}`});
// The owner's pending Browser input request, read by the Focus board and by the desktop viewer.
const focusBrowser = () => focusPending ? {state: 'pending', request_id: 'req-1', prompt: 'Enter the code'} : {state: 'idle'};
const focusBoard = () => ({status: 'ok', data: {revision: 1, status: 'Working', updated_at: new Date().toISOString(),
  stale: false, items: [], next_offset: null, activity: null, desktop_control_url: null, browser: focusBrowser()}});
const server = http.createServer(async (req, res) => {
  requests.push(req.url);
  const body = async () => { let raw = ''; for await (const chunk of req) raw += chunk; return raw ? JSON.parse(raw) : {}; };
  if (req.url.startsWith('/api/')) {
    const url = req.url.split('?')[0];
    const match = url.match(/^\/api\/root\/agents\/([^/]+)\/browser-surfaces$/);
    if (hold && match && match[1] === hold.agent) await new Promise(resolve => { hold.release = resolve; hold.reached(); });
    res.setHeader('Content-Type', 'application/json');
    if (match && refused.has(match[1])) { res.statusCode = 403; return res.end(JSON.stringify({error: 'Browser surfaces require the installation owner'})); }
    if (match) return res.end(JSON.stringify(config[match[1]] || {agent_id: match[1], configured: false, browsers: [], desktop: null}));
    // The Focus board read, the pending-input read and one completion are the only writes here.
    if (url === '/api/root/operations/focus_get') { await body(); return res.end(JSON.stringify(focusBoard())); }
    if (/^\/api\/me\/agents\/[^/]+\/focus\/done$/.test(url)) {
      const submission = await body();
      if (submission.request_id !== 'req-1') { res.statusCode = 409; return res.end(JSON.stringify({error: 'This input request changed.'})); }
      focusPending = false;
      return res.end(JSON.stringify({status: 'ok', data: {completed: true, request_id: submission.request_id}}));
    }
    if (/^\/api\/me\/agents\/[^/]+\/focus$/.test(url)) {
      if (focusFail) { res.statusCode = 503; return res.end(JSON.stringify({error: 'Focus unavailable'})); }
      return res.end(JSON.stringify({status: 'ok', data: {desktop_control_url: null, browser: focusBrowser()}}));
    }
    if (url === '/api/root/calendar-statuses') return res.end(JSON.stringify({status: 'ok', data: {items: []}}));
    res.statusCode = 404; return res.end('{}');
  }
  if (req.url.startsWith('/screen')) return res.end('<html><body>Fixture ' + req.url + '</body></html>');
  if (req.url === '/') {
    res.setHeader('Content-Type', 'text/html');
    return res.end(`<!doctype html><meta charset="utf-8">
      <link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/tasks.css"><link rel="stylesheet" href="/css/agentpane.css"><link rel="stylesheet" href="/css/agent-focus.css"><link rel="stylesheet" href="/css/agent-viewbox.css"><link rel="stylesheet" href="/css/actions.css"><link rel="stylesheet" href="/css/global-chat.css">
      <div class="split agsplit"><aside class="tasks agpad" id="agpad" style="width:900px;height:650px"></aside></div>
      
      <script>
      ${identity}
      ${lateCore}
      const coreData=r=>{if(r?.status!=='ok'||!r.data)throw Error('invalid');return r.data;}, state={scope:'global',biz:null,bizs:[],tab:'agents'};
      const api=async path=>{const r=await fetch('/api'+path), d=await r.json();if(!r.ok)throw Error(d.error);return d;};
      window.posts=[];
      // The Focus board and the desktop completion reach the fixture server; the other shared
      // operations this pane touches answer with their empty DTOs.
      const post=async(path,body)=>{
        posts.push([path,body]);
        if(path==='/root/operations/focus_get'||path.endsWith('/focus/done')) {
          const r=await fetch('/api'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
          const d=await r.json();if(!r.ok)throw Error(d.error||'The request failed');return d;
        }
        if(path==='/root/operations/calendar_calendars_list')return {status:'ok',data:{calendars:[],errors:[],configured:false}};
        if(path==='/root/operations/calendar_events_list')return {status:'ok',data:{events:[],next_cursor:null}};
        return {status:'ok',data:{items:[]}};
      };
      ${icons}
      const taskStateSubscribe=()=>()=>{};
      </script><script src="/js/tags.js"></script><script src="/js/work-groups.js"></script><script src="/js/tasks.js"></script><script src="/js/record-panel.js"></script><script src="/js/record-workspace.js"></script><script src="/js/global-chat.js"></script><script src="/js/agentpane.js"></script><script src="/js/agent-focus.js"></script><script src="/js/agent-viewbox.js"></script><script src="/js/agent-browser.js"></script><script src="/js/global-calendar.js"></script>
      <script>
      // Unrelated inbox/task painting is out of scope for this fixture.
      agpadPaint=()=>{}; agpadTick=()=>{}; agentCurrentWorkMount=()=>{};
      window.current=null;
      window.mount=(agentId,role,extra={})=>{
        current=agentId;
        agpadMount(agentId,$('#agpad'),{biz:null,agentId,workspaceScope:'global',workspaceRole:role,open:true,isCurrent:()=>current===agentId,...extra});
      };
      window.tab=v=>$('.ttab[data-v="'+v+'"]');
      window.tabs=()=>[...document.querySelectorAll('.ttab')].map(b=>b.dataset.v);
      window.chips=()=>[...document.querySelectorAll('[data-focus-surface]')].map(b=>[b.dataset.focusSurface,b.getAttribute('aria-disabled'),b.title]);
      window.frames_=()=>[...document.querySelectorAll('iframe')].map(f=>f.dataset.surface||f.src);
      </script>`);
  }
  const file = path.resolve(root, '.' + req.url.split('?')[0]);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css'); res.end(fs.readFileSync(file));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = 'http://127.0.0.1:' + server.address().port;
  const bind = (id, browsers, desktop = null) => config[id] = {agent_id: id, configured: true, browsers, desktop};
  bind('ordu', [surface('ordubashy-browser', 'Ordubashy', 'browser')], surface('ordubashy-desktop', 'Desktop', 'desktop'));
  bind('mkt', [surface('marketing-browser', 'Marketing', 'browser'), surface('outlook-browser', 'Outlook', 'browser')]);
  bind('founder', [surface('founder-browser', 'Founder', 'browser')]);
  // A business's Onbashy and Dev share one dedicated browser; Ordubashy keeps its own and adds it.
  bind('onb49', [surface('project-browser', 'Project', 'browser')]);
  bind('dev49', [surface('project-browser', 'Project', 'browser')]);
  bind('ordu2', [surface('ordubashy-browser', 'Ordubashy', 'browser'), surface('project-browser', 'Project', 'browser')], surface('ordubashy-desktop', 'Desktop', 'desktop'));
  bind('holdbiz', [surface('other-biz-browser', 'Other business', 'browser')]);
  config.mismatch = {agent_id: 'someone-else', configured: true, browsers: [surface('project-browser', 'Project', 'browser')], desktop: null};
  refused.add('member');
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1200, height: 800}});
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await page.goto(origin); if (errors.length) console.log('pageerrors', errors);
    const singleFrame = async surface => {
      await page.waitForFunction(id => { const f = frames_(); return f.length === 1 && f[0] === id; }, surface);
    };
    // A Focus launcher is enabled only by this agent's own resolved binding.
    const launched = async (kind, enabled = true) => page.waitForFunction(([k, want]) => {
      const chip = document.querySelector('[data-focus-surface="' + k + '"]');
      return !!chip && (chip.getAttribute('aria-disabled') === 'true') !== want;
    }, [kind, enabled]);
    const chip = kind => page.locator(`[data-focus-surface="${kind}"]`);
    const framesNow = () => page.evaluate(() => frames_());

    if (process.env.SESSION_TOGGLE_ONLY) {
      await page.evaluate(() => { localStorage.setItem('agpad:null','off'); mount('mkt','global-worker'); });
      assert.equal(await page.locator('.agpad').isVisible(),true);
      assert.equal(await page.locator('.agpad-toggle, .agpad-session-view, #agpad .gchat-view-toggle').count(),0);
      console.log('Agent pane stays available and does not duplicate the chat/terminal switch');
      return;
    }

    // An agent with Focus opens no stream by discovery alone: Browser is a launcher on its Focus board.
    await page.evaluate(() => mount('mkt', 'global-worker'));
    await launched('browser');
    assert.deepEqual(await page.evaluate(() => tabs()), ['focus', 'inbox', 'status'], 'Browser and Viewbox are Focus surfaces, not header tabs');
    assert.deepEqual(await page.evaluate(() => chips()), [['browser', null, 'Browser'], ['desktop', 'true', 'Desktop unavailable: only Ordubashy has a desktop']]);
    assert.deepEqual(await framesNow(), [], 'discovery alone opens no stream');
    assert.equal(await page.evaluate(() => document.querySelector('.agfocus-dock-content').classList.contains('rw')), true, 'the shared record workspace owns the Focus dock');
    assert.equal(requests.filter(url => url.includes('/agents/mkt/browser-surfaces')).length, 1, 'one discovery read');
    // Real launcher opens one Browser stream; the Marketing / Outlook selector stays inside it.
    await chip('browser').click();
    await singleFrame('marketing-browser');
    await page.locator('[data-browser-surface="outlook-browser"]').click();
    await singleFrame('outlook-browser');
    assert.equal(await page.evaluate(() => ls('agpad:view:mkt')), 'browser');
    assert.equal(await page.evaluate(() => ls('agpad:browser:mkt')), 'outlook-browser', 'the chosen surface is remembered for this agent');
    // Leaving through the tab strip tears the stream down and forgets Browser.
    await page.locator('.ttab[data-v="inbox"]').click();
    assert.deepEqual(await framesNow(), []);
    assert.equal(await page.locator('.agfocus').count(), 0, 'the Focus workspace is disposed with the view');
    assert.equal(await page.evaluate(() => ls('agpad:view:mkt')), '');
    // Returning through the Focus tab and launching Browser again opens straight into the remembered choice.
    await page.locator('.ttab[data-v="focus"]').click();
    await launched('browser');
    await chip('browser').click();
    await singleFrame('outlook-browser');
    assert.equal(await page.evaluate(() => document.querySelector('.ttab.on').dataset.v), 'focus');
    // Switching agent and coming back keeps the per-agent browser surface, never another agent's.
    await page.evaluate(() => mount('founder', 'founder'));
    await page.waitForFunction(() => tabs().includes('browser'));
    assert.deepEqual(await page.evaluate(() => tabs()), ['browser', 'inbox', 'viewbox', 'status'], 'an agent without Focus keeps the Browser header entry');
    assert.equal(await page.locator('[data-focus-surface]').count(), 0);
    await page.evaluate(() => mount('mkt', 'global-worker'));
    await singleFrame('outlook-browser');
    // A remembered or linked "desktop" for a non-Ordubashy agent lands in Browser, never a desktop.
    await page.evaluate(() => { localStorage.setItem('agpad:view:mkt', 'desktop'); mount('mkt', 'global-worker'); });
    await singleFrame('outlook-browser');
    await page.evaluate(() => mount('mkt', 'global-worker', {initialView: 'desktop'}));
    await singleFrame('outlook-browser');
    assert.equal(await page.locator('[data-agent-mode]').count(), 0, 'only Ordubashy can choose a desktop');
    assert.deepEqual(await page.locator('[data-browser-surface]').evaluateAll(b => b.map(x => [x.getAttribute('aria-label'), x.title, x.querySelector('svg') ? 1 : 0, x.textContent.trim()])),
      [['Marketing', '', 1, ''], ['Outlook', '', 1, '']]);
    assert.equal(await page.locator('[data-browser-surface="outlook-browser"]').getAttribute('aria-pressed'), 'true');
    // A linked view for an agent with no browser binding shows the honest state, no frame.
    await page.evaluate(() => mount('nobrowser', 'founder', {initialView: 'browser'}));
    await page.getByText('Browser unavailable.', {exact: true}).waitFor();
    assert.deepEqual(await framesNow(), []);
    // A refused binding stays unavailable: the Focus launcher never enables and opens nothing.
    await page.evaluate(() => mount('member', 'global-worker'));
    await launched('browser', false);
    await page.waitForFunction(() => document.querySelector('[data-focus-surface="browser"]').title.includes('could not be checked'));
    assert.deepEqual(await framesNow(), []);
    // The same refusal outside Focus is an honest retry state, not a stream.
    await page.evaluate(() => mount('member', 'founder', {initialView: 'browser'}));
    await page.getByText("Couldn't load. Retry", {exact: true}).waitFor();
    assert.deepEqual(await framesNow(), []);
    // A binding answering for another agent is refused the same way.
    await page.evaluate(() => mount('mismatch', 'founder', {initialView: 'browser'}));
    await page.getByText("Couldn't load. Retry", {exact: true}).waitFor();
    assert.deepEqual(await framesNow(), []);
    assert.equal(await page.locator('[data-browser-surface]').count(), 0, 'a mismatched answer never reaches the selector');

    // Ordubashy: Browser and Desktop are two launchable surfaces of one Focus board.
    await page.evaluate(() => { localStorage.clear(); mount('ordu', 'ordubashy'); });
    await launched('desktop');
    assert.deepEqual(await page.evaluate(() => chips()), [['browser', null, 'Browser'], ['desktop', null, 'Desktop']]);
    assert.deepEqual(await framesNow(), []);
    await chip('desktop').click();
    await singleFrame('ordubashy-desktop');
    assert.equal(await page.evaluate(() => ls('agpad:view:ordu')), 'desktop');
    assert.equal(await page.evaluate(() => document.querySelector('.ttab.on').dataset.v), 'focus', 'the one Focus entry stays active in Desktop mode');
    assert.equal(await page.locator('[data-agent-mode="desktop"]').getAttribute('aria-pressed'), 'true');
    await page.locator('[data-agent-mode="browser"]').click();
    await singleFrame('ordubashy-browser');
    assert.equal(await page.locator('[data-agent-mode="browser"]').getAttribute('aria-pressed'), 'true');
    await page.locator('[data-agent-mode="desktop"]').click();
    await singleFrame('ordubashy-desktop');
    assert.equal(await page.evaluate(() => ls('agpad:view:ordu')), 'desktop');
    // Re-selecting the mode already on screen never reloads it.
    await page.evaluate(() => { window.frameNode = document.querySelector('iframe'); });
    await page.locator('[data-agent-mode="desktop"]').click();
    assert.equal(await page.evaluate(() => document.querySelector('iframe') === window.frameNode), true, 'selected mode does not reload');
    // Back to Focus tears the stream down; the launcher reopens the same one.
    await page.locator('[data-surface-back]').click();
    assert.deepEqual(await framesNow(), []);
    assert.equal(await page.evaluate(() => document.querySelector('.agfocus-surface').hidden), true);
    await chip('browser').click();
    await singleFrame('ordubashy-browser');
    // Switching agent and coming back reopens the remembered mode instead of a reload.
    await page.evaluate(() => mount('founder', 'founder'));
    await page.evaluate(() => mount('ordu', 'ordubashy'));
    await singleFrame('ordubashy-browser');
    assert.notEqual(await page.evaluate(() => ls('agpad:view:founder')), 'desktop', 'selection never leaks between agents');
    // Leaving through the tab strip still tears the stream down and forgets the remembered mode.
    await page.locator('.ttab[data-v="inbox"]').click();
    assert.deepEqual(await framesNow(), []);
    assert.equal(await page.evaluate(() => ls('agpad:view:ordu')), '');
    // Removed controls and text, judged on the open Browser surface.
    await page.evaluate(() => mount('ordu', 'ordubashy'));
    await launched('browser');
    await chip('browser').click();
    await singleFrame('ordubashy-browser');
    assert.equal(await page.getByText('Open in new tab').count(), 0);
    assert.equal(await page.getByText('stays blank').count(), 0);
    assert.equal(await page.locator('[data-browser-open]').count(), 0);
    assert.equal(await page.locator('[data-browser-reconnect]').getAttribute('aria-label'), 'Reconnect');
    assert.equal((await page.locator('[data-browser-reconnect]').innerText()).trim(), '');
    // A linked or remembered Desktop view opens the desktop surface directly, and only Ordubashy has one.
    await page.evaluate(() => { localStorage.setItem('agpad:view:ordu', 'desktop'); mount('ordu', 'ordubashy'); });
    await singleFrame('ordubashy-desktop');
    assert.equal(await page.evaluate(() => ls('agpad:view:ordu')), 'desktop');
    await page.evaluate(() => { localStorage.removeItem('agpad:view:ordu'); mount('ordu', 'ordubashy', {initialView: 'desktop'}); });
    await singleFrame('ordubashy-desktop');
    assert.equal(await page.evaluate(() => ls('agpad:view:ordu')), 'desktop');

    // Pending desktop input through the Focus surface: draft, reconnect, one completion.
    focusPending = true;
    await page.evaluate(() => mount('ordu', 'ordubashy', {initialView: 'desktop'}));
    await singleFrame('ordubashy-desktop');
    await page.waitForFunction(() => document.querySelector('[data-focus-surface="desktop"]').dataset.attention === 'true');
    await page.locator('.agfocus-note').fill('code is 1234');
    assert.equal(await page.locator('[data-focus-surface="desktop"]').getAttribute('aria-label'), 'Desktop, input requested', 'pending input marks the Desktop launcher');
    await page.locator('[data-agent-mode="browser"]').click();
    await singleFrame('ordubashy-browser');
    await page.locator('[data-agent-mode="desktop"]').click();
    await singleFrame('ordubashy-desktop');
    assert.equal(await page.locator('.agfocus-note').inputValue(), 'code is 1234', 'note survives Browser/Desktop switching');
    const doneWrites = () => page.evaluate(() => posts.filter(([path]) => path.endsWith('/done')));
    assert.deepEqual(await doneWrites(), [], 'switching writes no completion');
    // A failing read shows the error with mode switching and Reconnect still available; retry recovers.
    focusFail = true;
    await page.locator('[data-desktop-retry]').click();
    await page.getByText('Focus unavailable').waitFor();
    assert.deepEqual(await framesNow(), []);
    assert.equal(await page.locator('[data-agent-mode]').count(), 2);
    assert.equal((await page.locator('[data-desktop-retry]').innerText()).trim(), '');
    focusFail = false;
    await page.locator('[data-desktop-retry]').click();
    await singleFrame('ordubashy-desktop');
    assert.equal(await page.locator('.agfocus-note').inputValue(), 'code is 1234', 'note survives Reconnect');
    // Mode switching from the error state works too.
    focusFail = true;
    await page.locator('[data-desktop-retry]').click();
    await page.getByText('Focus unavailable').waitFor();
    await page.locator('[data-agent-mode="browser"]').click();
    await singleFrame('ordubashy-browser');
    focusFail = false;
    await page.locator('[data-agent-mode="desktop"]').click();
    await singleFrame('ordubashy-desktop');
    await page.locator('.agfocus-done').click();
    await page.getByText('Input sent to Ordubashy.').waitFor();
    assert.deepEqual(await doneWrites(), [['/me/agents/ordu/focus/done', {request_id: 'req-1', note: 'code is 1234'}]], 'exactly one exact completion');
    focusPending = false;

    // Selecting another agent while discovery is pending: the late answer changes nothing.
    hold = {agent: 'mkt', reached: null};
    const reached = new Promise(resolve => { hold.reached = resolve; });
    await page.evaluate(() => { localStorage.clear(); mount('mkt', 'global-worker'); });
    // Fresh page state so this mount performs a new read.
    await reached;
    await page.evaluate(() => mount('founder', 'founder', {initialView: 'browser'}));
    await singleFrame('founder-browser');
    hold.release(); hold = null;
    await page.waitForTimeout(200);
    assert.deepEqual(await framesNow(), ['founder-browser']);
    assert.equal(await page.evaluate(() => tabs().includes('browser')), true);
    assert.equal(await page.evaluate(() => document.querySelector('.ttab.on').dataset.v), 'browser');
    assert.equal(await page.locator('[data-focus-surface]').count(), 0, 'no Marketing launcher on another agent');

    // Business browsers are business resources: a business-scope pane neither discovers nor offers Browser.
    const business = (agent, role, extra = {}) => page.evaluate(([a, r, x]) => mount(a, r, {workspaceScope: 'business', biz: 'ProjectA', ...x}), [agent, role, extra]);
    const surfaceReads = agent => requests.filter(url => url.includes(`/agents/${agent}/browser-surfaces`)).length;
    for (const [agent, role] of [['dev49', 'dev'], ['onb49', 'business-agent']]) {
      await business(agent, role);
      await page.waitForFunction(a => window.current === a, agent);
      await page.waitForTimeout(150);
      assert.equal(surfaceReads(agent), 0, agent + ': no discovery read');
      assert.equal(await page.evaluate(() => tabs().includes('browser')), false, agent + ': no Browser entry');
      assert.equal(await page.locator('[data-focus-surface]').count(), 0, agent + ': no Focus launchers');
      assert.deepEqual(await framesNow(), []);
    }
    // Ordubashy keeps its own browser and Desktop and gains the business browser as another option.
    await page.evaluate(() => { localStorage.removeItem('agpad:view:ordu2'); localStorage.removeItem('agpad:browser:ordu2'); mount('ordu2', 'ordubashy'); });
    await launched('browser');
    await chip('browser').click();
    await singleFrame('ordubashy-browser');
    assert.deepEqual(await page.locator('[data-browser-surface]').evaluateAll(b => b.map(x => [x.getAttribute('aria-label'), x.getAttribute('aria-pressed')])),
      [['Ordubashy', 'true'], ['Project', 'false']]);
    await page.locator('[data-browser-surface="project-browser"]').click();
    await singleFrame('project-browser');
    await page.locator('[data-agent-mode="desktop"]').click();
    await singleFrame('ordubashy-desktop');
    await page.locator('[data-agent-mode="browser"]').click();
    await singleFrame('project-browser');
    assert.equal(await page.evaluate(() => ls('agpad:browser:ordu2')), 'project-browser');
    assert.deepEqual(errors, []);
    console.log('Agent pane Browser integration checks passed');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });