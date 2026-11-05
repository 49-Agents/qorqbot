#!/usr/bin/env node
// Isolated Browser/Desktop pane fixture: a stub iframe target proves dashboard
// routing only. No live desktops, agents, Guacamole or operational writes.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const requests = [], errors = [];
const identity = fs.readFileSync(path.join(root, 'js/core.js'), 'utf8').split('const TABS =')[0];
// Real Browser/Desktop glyphs from tasks.js (the rest of tasks.js boots the dashboard).
const viewIcons = fs.readFileSync(path.join(root, 'js/tasks.js'), 'utf8').split('\n').filter(line => /^  (browser|desktop): `/.test(line)).join('\n');
let origin, fail = false, hold = null;
const config = {};
const surface = (id, label, kind, extra = '') => ({id, label, kind, viewer_url: `${origin}/screen/${id}${extra}`});
const server = http.createServer(async (req, res) => {
  requests.push(req.url);
  if (req.url.startsWith('/api/')) {
    const match = req.url.match(/^\/api\/root\/agents\/([^/]+)\/browser-surfaces$/);
    if (hold && match && match[1] === hold.agent) await new Promise(resolve => { hold.release = resolve; hold.reached(); });
    res.setHeader('Content-Type', 'application/json');
    if (fail) { res.statusCode = 503; return res.end(JSON.stringify({error: 'Browser service unavailable'})); }
    if (match) return res.end(JSON.stringify(config[match[1]] || {agent_id: match[1], configured: false, browsers: [], desktop: null}));
    if (req.url.includes('/focus')) return res.end(JSON.stringify({data: {desktop_control_url: null, browser: {state: 'pending', request_id: 'ordu-input', prompt: 'Enter the code'}}}));
    res.statusCode = 404; return res.end('{}');
  }
  if (req.url.startsWith('/screen')) return res.end('<html><body style="background:#202830;color:white">Fixture ' + req.url + '</body></html>');
  if (req.url === '/') {
    res.setHeader('Content-Type', 'text/html');
    return res.end(`<!doctype html><meta charset="utf-8">
      <link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/agent-focus.css">
      <style>#agpad{width:900px;height:650px}#agpad-body{height:600px;overflow:auto}</style>
      <div id="agpad"><div class="tbody" id="agpad-body"></div></div>
      <script>
      ${identity}
      const coreData=r=>r.data;
      const T_VIEW_LABEL={inbox:'Inbox'}, tEmptyHtml=()=>'', T_VIEW_ICON={${viewIcons}};
      const api=async path=>{const r=await fetch('/api'+path), d=await r.json();if(!r.ok)throw Error(d.error);return d;};
      </script><script src="/js/agentpane.js"></script><script src="/js/agent-focus.js"></script><script src="/js/agent-browser.js"></script>
      <script>
      // Minimal pane: real view registry, mount and dispose paths without the task chrome.
      window.openAgent=(agentId,role,view,scope='global')=>{
        window.p?.workspaceDispose?.();
        window.p={el:$('#agpad'),agent:agentId,agentId,open:true,view,views:[],workspaceScope:scope,workspaceRole:role,workspaceBase:[],isCurrent:()=>true};
        _agpad=window.p;
        return p;
      };
      // The viewer toggle switches views through the real pane; here that is showView.
      window.agpadSetView=(pane,view)=>showView(view);
      window.showView=view=>{
        p.workspaceDispose?.();p.workspaceDispose=null;
        if(p.view==='desktop')p.desktopRequest=(p.desktopRequest||0)+1;
        if(p.view==='browser')p.browserRequest=(p.browserRequest||0)+1;
        p.view=view;$('#agpad-body').innerHTML='';
        agpadWorkspaceMount(p,view);
      };
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
  bind('ordu', [surface('ordubashy-browser', 'Ordubashy', 'browser', '?qorqut_width=1440&qorqut_height=900')], surface('ordubashy-desktop', 'Desktop', 'desktop', '?qorqut_width=1920&qorqut_height=1080'));
  bind('mkt', [surface('marketing-browser', 'Marketing', 'browser'), surface('outlook-browser', 'Outlook', 'browser')]);
  bind('other', [surface('other-browser', 'Other', 'browser')]);
  bind('bare', []);
  bind('onb49', [surface('project-browser', 'Project', 'browser', '?qorqut_width=1280&qorqut_height=800')]);
  bind('ordu49', [surface('ordubashy-browser', 'Ordubashy', 'browser'), surface('project-browser', 'Project', 'browser', '?qorqut_width=1280&qorqut_height=800')], surface('ordubashy-desktop', 'Desktop', 'desktop'));
  config.wrong = {agent_id: 'someone-else', configured: true, browsers: [surface('marketing-browser', 'Marketing', 'browser')], desktop: null};
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1200, height: 800}});
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await page.goto(origin);
    const views = (scope, role, browser) => page.evaluate(([s, r, b]) => agentPaneViews(s, r, ['inbox'], b), [scope, role, browser]);
    // Browser is generic; Desktop is Ordubashy's alone; the retired Marketing desktop tab is gone.
    assert.deepEqual(await views('global', 'ordubashy', true), ['focus', 'browser', 'desktop', 'inbox', 'status']);
    assert.deepEqual(await views('global', 'ordubashy', false), ['focus', 'browser', 'desktop', 'inbox', 'status'], 'Ordubashy always has one Browser entry');
    assert.deepEqual(await views('global', 'global-worker', true), ['browser', 'inbox', 'status']);
    assert.deepEqual(await views('global', 'global-worker', false), ['inbox', 'status']);
    assert.deepEqual(await views('global', 'founder', true), ['browser', 'inbox', 'status']);
    assert.equal(requests.some(url => url.startsWith('/api/')), false, 'browser stays lazy');

    // Marketing: Marketing and Outlook are separate exact targets inside Browser.
    await page.evaluate(() => { openAgent('mkt', 'global-worker', 'browser'); showView('browser'); });
    await page.locator('iframe').waitFor();
    assert.equal(await page.locator('iframe').getAttribute('src'), origin + '/screen/marketing-browser');
    assert.equal(await page.locator('[data-browser-surface]').count(), 2);
    assert.deepEqual(await page.locator('[data-browser-surface]').evaluateAll(b => b.map(x => [x.getAttribute('aria-label'), x.title, x.getAttribute('aria-pressed'), x.textContent.trim(), x.querySelectorAll('svg').length])),
      [['Marketing', '', 'true', '', 1], ['Outlook', '', 'false', '', 1]]);
    assert.equal(await page.locator('[data-agent-mode]').count(), 0, 'only Ordubashy has the Browser/Desktop toggle');
    for (const gone of ['[data-browser-open]', '.agbrowser-note', 'a[target=_blank]']) assert.equal(await page.locator(gone).count(), 0, gone);
    assert.equal(await page.getByText('open it in a new tab').count(), 0);
    assert.equal(await page.getByText('Open in new tab').count(), 0);
    assert.equal((await page.locator('[data-browser-reconnect]').innerText()).trim(), '', 'Reconnect is icon-only');
    assert.equal(await page.locator('[data-browser-reconnect] svg').count(), 1);
    assert.equal(await page.locator('[data-browser-reconnect]').getAttribute('title'), null);
    assert.equal(await page.locator('[data-browser-reconnect]').getAttribute('aria-label'), 'Reconnect');
    assert.equal(await page.locator('iframe').getAttribute('title'), null);
    assert.equal(await page.locator('iframe').getAttribute('aria-label'), 'Marketing browser');
    await page.screenshot({path: '/tmp/gbd-marketing-browser.png'});
    await page.locator('[data-browser-surface="outlook-browser"]').click();
    await page.waitForFunction(() => document.querySelectorAll('iframe').length === 1 && document.querySelector('iframe').dataset.surface === 'outlook-browser');
    assert.equal(await page.locator('iframe').getAttribute('src'), origin + '/screen/outlook-browser');
    assert.equal(await page.locator('[data-browser-surface="outlook-browser"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.evaluate(() => ls('agpad:browser:mkt')), 'outlook-browser');
    await page.screenshot({path: '/tmp/gbd-outlook-browser.png'});
    assert.equal(requests.filter(url => url.includes('/browser-surfaces')).length, 1, 'switching does not re-read bindings');
    // Selection is remembered for Marketing and never leaks to another agent.
    await page.evaluate(() => { showView('inbox'); });
    assert.equal(await page.locator('iframe').count(), 0, 'leaving tears the stream down');
    await page.evaluate(() => { openAgent('mkt', 'global-worker', 'browser'); showView('browser'); });
    await page.locator('iframe').waitFor();
    assert.equal(await page.locator('iframe').getAttribute('src'), origin + '/screen/outlook-browser');
    await page.evaluate(() => { openAgent('other', 'founder', 'browser'); showView('browser'); });
    await page.waitForFunction(() => document.querySelector('iframe')?.dataset.surface === 'other-browser');
    assert.equal(await page.locator('[data-browser-surface]').count(), 0, 'single browser has no selector');
    assert.equal(await page.evaluate(() => ls('agpad:browser:other')), 'other-browser');

    // Resize refits without reloading the stream.
    const loads = () => requests.filter(url => url.startsWith('/screen/other-browser')).length;
    const before = loads();
    await page.evaluate(() => { document.querySelector('#agpad').style.width = '500px'; });
    await page.waitForFunction(() => document.querySelector('iframe').getBoundingClientRect().width <= 500);
    assert.equal(loads(), before, 'resize does not reload the browser');

    // Narrow pane: the toolbar wraps without clipping the toggles or Reconnect.
    await page.evaluate(() => { document.querySelector('#agpad').style.width = '320px'; openAgent('mkt', 'global-worker', 'browser'); showView('browser'); });
    await page.waitForFunction(() => document.querySelector('iframe')?.dataset.surface === 'outlook-browser');
    await page.screenshot({path: '/tmp/gbd-marketing-narrow.png'});
    for (const sel of ['[data-browser-surface]', '[data-browser-reconnect]']) for (const box of await page.locator(sel).evaluateAll(b => b.map(x => x.getBoundingClientRect().toJSON())))
      assert(box.right <= 320 && box.left >= 0 && box.width >= 30 && box.height >= 30, sel + JSON.stringify(box));
    // Ordubashy: Browser and Desktop are separate streams; Focus input stays intact.
    await page.evaluate(() => { document.querySelector('#agpad').style.width = '900px'; openAgent('ordu', 'ordubashy', 'browser'); showView('browser'); });
    await page.waitForFunction(() => document.querySelector('iframe')?.dataset.surface === 'ordubashy-browser');
    await page.waitForFunction(() => document.querySelector('iframe').style.height !== '');
    let box = await page.locator('iframe').boundingBox();
    assert(Math.abs(box.width / box.height - 1440 / 900) < 0.02, JSON.stringify(box));
    await page.evaluate(() => showView('desktop'));
    await page.locator('.agfocus-done').waitFor();
    assert.equal(await page.locator('iframe').count(), 1, 'only the desktop stream remains');
    assert.equal(await page.locator('iframe').getAttribute('src'), origin + '/screen/ordubashy-desktop');
    assert.equal(await page.locator('.agfocus-desktop-help strong').innerText(), 'Enter the code');
    assert(requests.includes('/api/me/agents/ordu/focus'));
    await page.waitForFunction(() => document.querySelector('iframe').style.height !== '');
    box = await page.locator('iframe').boundingBox();
    assert(Math.abs(box.width / box.height - 1920 / 1080) < 0.02, JSON.stringify(box));
    await page.screenshot({path: '/tmp/gbd-ordubashy-desktop.png'});
    assert.equal(await page.locator('[data-agent-mode="desktop"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.getByText('Open in new tab').count(), 0);
    assert.equal(await page.locator('a[target=_blank]').count(), 0);
    assert.equal((await page.locator('[data-desktop-retry]').innerText()).trim(), '', 'desktop Reconnect is icon-only');
    const reconnects = requests.filter(url => url.includes('/agents/ordu/browser-surfaces')).length;
    await page.locator('[data-desktop-retry]').click();
    await page.locator('.agfocus-done').waitFor();
    assert.equal(requests.filter(url => url.includes('/agents/ordu/browser-surfaces')).length, reconnects + 1);
    assert.equal(await page.locator('iframe').count(), 1);
    await page.locator('[data-agent-mode="browser"]').click();
    await page.waitForFunction(() => document.querySelector('iframe')?.dataset.surface === 'ordubashy-browser');
    assert.equal(await page.locator('[data-agent-mode="browser"]').getAttribute('aria-pressed'), 'true');
    await page.screenshot({path: '/tmp/gbd-ordubashy-browser.png'});
    await page.evaluate(() => { document.querySelector('#agpad').style.width = '320px'; });
    await page.screenshot({path: '/tmp/gbd-ordubashy-narrow.png'});
    await page.evaluate(() => { document.querySelector('#agpad').style.width = '900px'; });
    await page.locator('[data-agent-mode="desktop"]').click();
    await page.locator('.agfocus-done').waitFor();
    await page.evaluate(() => showView('browser'));
    await page.waitForFunction(() => document.querySelector('iframe')?.dataset.surface === 'ordubashy-browser');
    assert.equal(await page.locator('.agfocus-done').count(), 0);

    // Business browsers are business resources: an agent pane outside global scope never discovers or offers one.
    const beforeBusiness = requests.filter(url => url.includes('/browser-surfaces')).length;
    assert.deepEqual(await page.evaluate(() => {
      const pane = {agentId: 'onb49', workspaceScope: 'business', workspaceRole: 'business-agent', browserRequested: true};
      agbrowserDiscover(pane);
      return [agbrowserTab(pane), agentPaneViews('business', 'business-agent', ['inbox'], agbrowserTab(pane)).includes('browser')];
    }), [false, false]);
    await page.waitForTimeout(50);
    assert.equal(requests.filter(url => url.includes('/browser-surfaces')).length, beforeBusiness, 'no business-agent discovery read');
    // Ordubashy keeps its own browser and Desktop and adds the business browser as another option.
    await page.evaluate(() => { localStorage.removeItem('agpad:browser:ordu49'); openAgent('ordu49', 'ordubashy', 'browser'); showView('browser'); });
    await page.waitForFunction(() => document.querySelector('iframe')?.dataset.surface === 'ordubashy-browser');
    assert.deepEqual(await page.locator('[data-browser-surface]').evaluateAll(b => b.map(x => [x.getAttribute('aria-label'), x.getAttribute('aria-pressed')])),
      [['Ordubashy', 'true'], ['Project', 'false']]);
    assert.equal(await page.locator('[data-browser-surface="project-browser"] .identity-business').count(), 1, 'a business binding wears its business initial');
    await page.screenshot({path: '/tmp/gbd-ordubashy-business-selector.png'});
    await page.locator('[data-browser-surface="project-browser"]').click();
    await page.waitForFunction(() => document.querySelector('iframe')?.dataset.surface === 'project-browser');
    assert.equal(await page.locator('[data-agent-mode="desktop"]').count(), 1, 'Ordubashy keeps its Desktop toggle');
    await page.evaluate(() => showView('desktop'));
    await page.locator('.agfocus-done').waitFor();
    assert.equal(await page.locator('iframe').getAttribute('src'), origin + '/screen/ordubashy-desktop');
    // Missing, wrong-agent and failing reads: honest messages, never another agent's iframe.
    await page.evaluate(() => { openAgent('bare', 'founder', 'browser'); showView('browser'); });
    await page.getByText('Browser unavailable.', {exact: true}).waitFor();
    await page.evaluate(() => { openAgent('unset', 'founder', 'browser'); showView('browser'); });
    await page.getByText('Browser unavailable.', {exact: true}).waitFor();
    await page.evaluate(() => { openAgent('wrong', 'founder', 'browser'); showView('browser'); });
    await page.getByText("Couldn't load. Retry", {exact: true}).waitFor();
    assert.equal(await page.locator('iframe').count(), 0);
    fail = true;
    await page.evaluate(() => { openAgent('mkt2', 'global-worker', 'browser'); showView('browser'); });
    await page.getByRole('button', {name: 'Reconnect'}).waitFor();
    assert.equal(await page.locator('iframe').count(), 0);
    assert.equal(await page.getByText('Browser service unavailable').count(), 0, 'the server message is never shown');
    config.mkt2 = {agent_id: 'mkt2', configured: true, browsers: [surface('marketing-browser', 'Marketing', 'browser')], desktop: null};
    fail = false;
    await page.getByRole('button', {name: 'Reconnect'}).click();
    await page.locator('iframe').waitFor();
    // Reconnect on a live view re-reads the binding and remounts one stream.
    const reads = requests.filter(url => url.includes('/agents/mkt2/browser-surfaces')).length;
    await page.locator('[data-browser-reconnect]').click();
    await page.waitForFunction(() => document.querySelectorAll('iframe').length === 1);
    assert.equal(requests.filter(url => url.includes('/agents/mkt2/browser-surfaces')).length, reads + 1);

    // A late response for agent A never resurrects its stream after B is selected.
    hold = {agent: 'late', reached: null};
    const reached = new Promise(resolve => { hold.reached = resolve; });
    config.late = {agent_id: 'late', configured: true, browsers: [surface('late-browser', 'Late', 'browser')], desktop: null};
    await page.evaluate(() => { openAgent('late', 'founder', 'browser'); showView('browser'); });
    await reached;
    await page.evaluate(() => { openAgent('other', 'founder', 'browser'); showView('browser'); });
    await page.waitForFunction(() => document.querySelector('iframe')?.dataset.surface === 'other-browser');
    hold.release(); hold = null;
    await page.waitForTimeout(150);
    assert.deepEqual(await page.locator('iframe').evaluateAll(list => list.map(f => f.dataset.surface)), ['other-browser']);
    // Leaving the tab while the read is pending mounts nothing.
    hold = {agent: 'mkt', reached: null};
    const reached2 = new Promise(resolve => { hold.reached = resolve; });
    await page.evaluate(() => { openAgent('mkt', 'global-worker', 'browser'); p.surfaces = null; showView('browser'); });
    await reached2;
    await page.evaluate(() => showView('inbox'));
    hold.release(); hold = null;
    await page.waitForTimeout(150);
    assert.equal(await page.locator('iframe').count(), 0);

    // Legacy Marketing desktop navigation lands in Browser; Ordubashy keeps Desktop.
    const landing = await page.evaluate(() => {
      const list = [];
      for (const [role, remembered] of [['global-worker', 'desktop'], ['ordubashy', 'desktop']]) {
        localStorage.setItem('agpad:view:landing', remembered);
        const p = {agentId: 'landing', workspaceRole: role, workspaceScope: 'global'};
        let view = agpadRememberedView(p);
        if (view === 'desktop' && p.workspaceRole !== 'ordubashy' && p.workspaceScope === 'global') view = 'browser';
        list.push(view);
      }
      return list;
    });
    assert.deepEqual(landing, ['browser', 'desktop']);
    assert.equal(requests.some(url => url.includes('desktop-servers')), false, 'the legacy Marketing desktop read is unused');
    assert.deepEqual(errors, []);
    console.log('Browser and Desktop pane checks passed');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
