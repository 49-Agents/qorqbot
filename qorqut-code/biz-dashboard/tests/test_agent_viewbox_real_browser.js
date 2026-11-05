#!/usr/bin/env node
// Real service fixture (isolated temp database, real placements and table reads): the Viewbox list and the standalone owner
// table page for a business and Global table. The full application is never mounted; no live services or models.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const core = process.env.BAZARLYK_CORE_ROOT || path.resolve(__dirname, '../../biz-core');
const dashboard = path.resolve(__dirname, '..');
const shots = process.env.VIEWBOX_SHOTS || '/tmp/vbshots';
const coreJs = fs.readFileSync(path.join(dashboard, 'js/core.js'), 'utf8');
const identity = coreJs.split('const TABS =')[0];
const icons = coreJs.split('\n').filter(line => /^const (ICO_|SVG_)\w+ = `/.test(line)).join('\n');

(async () => {
  const child = spawn(process.env.TEST_PYTHON || path.join(core, '.venv/bin/python'), ['tests/viewbox_browser_fixture.py', '--dashboard-root', dashboard],
    { cwd: core, env: { ...process.env, PYTHONPATH: [core, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter) }, stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '', browser;
  child.stderr.on('data', data => { stderr += data; });
  try {
    const fx = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('Fixture timed out: ' + stderr)), 180000);
      let out = '';
      child.stdout.on('data', data => { out += data; const line = out.split('\n').find(x => x.startsWith('{"url":')); if (line) { clearTimeout(timer); resolve(JSON.parse(line)); } });
      child.on('exit', code => reject(Error('Fixture exited ' + code + ': ' + stderr)));
    });
    const origin = fx.url, errors = [], requests = [];
    browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined, args: ['--no-sandbox'] });
    const page = await browser.newPage({ viewport: { width: 1100, height: 760 } });
    page.on('pageerror', e => errors.push(e.message));
    page.on('request', r => requests.push(r.url()));
    await page.route('**/*', route => {
      const u = new URL(route.request().url());
      if (u.origin === origin && u.pathname === '/__harness') return route.fulfill({ contentType: 'text/html', body: harness() });
      return u.origin === origin || u.protocol === 'about:' ? route.continue() : route.abort();
    });
    const harness = () => `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/tasks.css"><link rel="stylesheet" href="/css/agentpane.css"><link rel="stylesheet" href="/css/agent-focus.css"><link rel="stylesheet" href="/css/agent-viewbox.css">
      <div class="split agsplit"><aside class="tasks agpad" id="agpad" style="width:900px;height:620px"></aside><button id="agpad-toggle"></button></div>
      <script>${identity}
      const coreData=r=>{if(r?.status!=='ok'||!r.data)throw Error('invalid');return r.data;}, state={scope:'global',biz:null,bizs:[],tab:'agents'};
      const api=async (p,opt)=>{const r=await fetch('/api'+p,opt);return r.json();}; const post=async()=>({status:'ok',data:{items:[]}});
      ${icons} const taskStateSubscribe=()=>()=>{};</script>
      <script src="/js/tags.js"></script><script src="/js/work-groups.js"></script><script src="/js/tasks.js"></script><script src="/js/agentpane.js"></script><script src="/js/agent-focus.js"></script><script src="/js/agent-viewbox.js"></script>
      <script>agentCurrentWorkMount=()=>{};window.agfocusMount=()=>{};
      window.mount=(id,scope,role)=>agpadMount(id,$('#agpad'),{biz:scope==='business'?'biz':null,agentId:id,workspaceScope:scope,workspaceRole:role,open:true,isCurrent:()=>true,initialView:'viewbox'});</script>`;
    const get = async p => page.evaluate(async url => { const r = await fetch(url); return { status: r.status, etag: r.headers.get('etag'), body: await r.json() }; }, p);

    // Real owner board: per-agent, newest first, relative viewer links deduplicated with the absolute form
    await page.goto(origin + '/health/live');
    const mgr = await get(`/api/root/agents/${fx.manager_id}/viewbox`);
    assert.equal(mgr.status, 200);
    assert.deepEqual(mgr.body.data.items.map(i => [i.title, i.kind]), [['Motion options', 'file'], ['Hostile HTML', 'file'], ['Page HTML', 'file'], ['Small CSV', 'file'], ['Report markdown', 'file'], ['Report', 'web'], ['Global table', 'table']]);
    assert.ok(mgr.body.data.items[6].link.startsWith('/tables/view?table_id='), 'application-origin link stored root-relative');
    const biz = await get(`/api/root/agents/${fx.agent_id}/viewbox`);
    assert.deepEqual(biz.body.data.items.map(i => i.title), ['Sales again'], 'same table placed twice is one row, repinned');
    assert.ok(mgr.etag && mgr.etag.startsWith('W/"viewbox-'));

    // Real Viewbox renderer over the real board: table row opens the real table page in a trusted frame
    await page.goto(origin + '/__harness');
    await page.evaluate(id => mount(id, 'global', 'ordubashy'), fx.manager_id);
    await page.waitForSelector('.agvb-row');
    assert.deepEqual(await page.$$eval('.agvb-title', n => n.map(x => x.textContent)), ['Motion options', 'Hostile HTML', 'Page HTML', 'Small CSV', 'Report markdown', 'Report', 'Global table']);
    await page.click('.agvb-row[data-kind="table"]');
    await page.waitForSelector('iframe.agvb-frame');
    assert.equal(await page.getAttribute('iframe.agvb-frame', 'sandbox'), null);
    const inner = page.frameLocator('iframe.agvb-frame');
    await inner.locator('#tv-title').waitFor();
    await page.waitForFunction(() => document.querySelector('iframe.agvb-frame').contentDocument?.querySelectorAll('tbody tr').length === 3);
    assert.equal(await inner.locator('#tv-title').textContent(), 'Global <i>pipeline</i>', 'the real table page renders in the real frame, escaped');
    await page.screenshot({ path: path.join(shots, 'viewbox-real-table-frame.png') });
    await page.click('.agvb-back');
    await page.click('.agvb-row[data-kind="web"]');
    await page.waitForSelector('iframe.agvb-frame');
    assert.equal(await page.getAttribute('iframe.agvb-frame', 'sandbox'), 'allow-scripts allow-forms', 'external link is sandboxed');

    // Real Viewbox -> real /files/view reader (integrated core and dashboard, no fixture route substitutions)
    await page.click('.agvb-back');
    const openRow = async title => { await page.click(`.agvb-row:has-text("${title}")`); await page.waitForSelector('iframe.agvb-frame'); };
    const reader = () => page.frameLocator('iframe.agvb-frame');
    await openRow('Report markdown');
    assert.equal(await page.getAttribute('iframe.agvb-frame', 'sandbox'), null, 'the exact reader path is framed trusted');
    await reader().locator('.fv-md h1').waitFor();
    assert.equal(await reader().locator('.fv-md h1').textContent(), 'Synthetic report');
    assert.equal(await reader().locator('.fv-md script, .fv-md img').count(), 0, 'Markdown is inert inside the Viewbox');
    await page.screenshot({ path: path.join(shots, 'viewbox-real-file-markdown.png') });
    // New-tab fallback is the same exact reader URL, opened without an opener
    assert.match(await page.getAttribute('.agvb-open', 'href'), /\/files\/view\?path=widget%2Ffiles%2Freport\.md$/);
    assert.equal(await page.getAttribute('.agvb-open', 'rel'), 'noopener noreferrer');
    // Resizing (corner drag, parent shrink, Fit) never reloads the reader
    await page.evaluate(() => { document.querySelector('iframe.agvb-frame').contentWindow.__alive = 1; });
    const handle = await page.locator('.agvb-handle').boundingBox();
    await page.mouse.move(handle.x + 9, handle.y + 9); await page.mouse.down(); await page.mouse.move(handle.x - 171, handle.y - 121, { steps: 4 }); await page.mouse.up();
    await page.evaluate(() => { document.querySelector('#agpad').style.width = '500px'; });
    await page.waitForTimeout(150);
    await page.click('.agvb-fit');
    assert.equal(await page.evaluate(() => document.querySelector('iframe.agvb-frame').contentWindow.__alive), 1, 'resize does not reload the real reader');
    await page.evaluate(() => { document.querySelector('#agpad').style.width = '900px'; });
    await page.click('.agvb-back');
    assert.equal(await page.locator('.agvb-row').count(), 7, 'Back shows the list');
    await openRow('Small CSV');
    await reader().locator('.fv-csv table').waitFor();
    assert.equal(await reader().locator('.fv-csv tr').count(), 3);
    await page.screenshot({ path: path.join(shots, 'viewbox-real-file-csv.png') });
    await page.click('.agvb-back');
    await openRow('Motion options');
    // HTML renders (not source) and its preview fills the frame: no nested page scroll, and it follows the resize handle.
    const motion = reader().frameLocator('iframe.fv-frame');
    await motion.locator('#intro').waitFor();
    assert.equal(await reader().locator('.fv-frame').count(), 1);
    const fill = () => page.evaluate(() => { const o = document.querySelector('iframe.agvb-frame'), d = o.contentDocument, f = d.querySelector('iframe.fv-frame').getBoundingClientRect();
      return { outerH: o.getBoundingClientRect().height, outerW: o.getBoundingClientRect().width, scrollH: d.documentElement.scrollHeight, frameH: f.height, frameW: f.width, frameBottom: f.bottom, filled: d.querySelector('.fv').classList.contains('fv-fill') }; });
    // The page's sibling SVG files are decoded in the inert frame; the missing one stays blocked and is reported once.
    for (const id of ['hero', 'square']) assert.ok(await motion.locator('#' + id).evaluate(img => img.complete && img.naturalWidth > 0), id + ' image decoded');
    assert.equal(await motion.locator('#gone').evaluate(img => img.naturalWidth), 0, 'missing image stays blocked');
    assert.match(await reader().locator('.fv-banner').innerText(), /An image used by this page could not be shown/);
    assert.equal(await reader().locator('.fv-banner').count(), 1);
    let m = await fill();
    assert.ok(m.filled && m.scrollH <= m.outerH + 1, 'the reader page does not scroll inside the Viewbox ' + JSON.stringify(m));
    assert.ok(m.outerH - m.frameBottom <= 16 && m.frameH > 250, 'the HTML preview reaches the bottom of the pane ' + JSON.stringify(m));
    await page.screenshot({ path: path.join(shots, 'viewbox-html-fill-large.png') });
    const grip = await page.locator('.agvb-handle').boundingBox();
    await page.mouse.move(grip.x + 9, grip.y + 9); await page.mouse.down(); await page.mouse.move(grip.x - 191, grip.y - 141, { steps: 4 }); await page.mouse.up();
    const small = await fill();
    assert.ok(small.outerH < m.outerH - 100 && small.frameH >= 200 && small.frameH < m.frameH, 'the preview resizes with the drag handle ' + JSON.stringify(small));
    assert.ok(small.outerH - small.frameBottom <= 16 || small.scrollH > small.outerH, 'the preview still fills what is available ' + JSON.stringify(small));
    await page.screenshot({ path: path.join(shots, 'viewbox-html-fill-small.png') });
    await page.click('.agvb-fit');
    m = await fill();
    assert.ok(m.outerH - m.frameBottom <= 16, 'Fill pane restores the full preview ' + JSON.stringify(m));
    // Source shows the page exactly as saved, not the embedded copy.
    await reader().getByRole('button', { name: 'Source' }).click();
    const source = await reader().locator('pre.fv-text').innerText();
    assert.ok(source.includes('src="animated.svg"') && !source.includes('base64'), 'Source keeps the original text');
    await page.click('.agvb-back');
    await openRow('Page HTML');
    const htmlFrame = reader().frameLocator('iframe.fv-frame');
    await htmlFrame.locator('#heading').waitFor();
    assert.equal(await htmlFrame.locator('#heading').textContent(), 'Synthetic HTML', 'scripts in the HTML preview did not run');
    await page.click('.agvb-back');
    // Hostile HTML: every attempt gets a fresh Viewbox -> real reader -> inner HTML instance and ACTUALLY activates its target
    // (an explicit _self may replace the preview with the blocked page). Every unsafe request is tracked by identity and must
    // either never be announced or fail specifically on CSP ('csp' is Chromium's blocked reason; net::ERR_BLOCKED_BY_CSP the network name).
    const unsafe = url => url.includes('/ping') || url.includes('content?path=widget%2Ffiles%2Freport.md');
    const tracked = new Map();
    page.on('request', r => { if (unsafe(r.url())) tracked.set(r, { url: r.url(), responded: false, finished: false, failure: null }); });
    page.on('response', r => { const t = tracked.get(r.request()); if (t) t.responded = true; });
    page.on('requestfinished', r => { const t = tracked.get(r); if (t) t.finished = true; });
    page.on('requestfailed', r => { const t = tracked.get(r); if (t) t.failure = r.failure()?.errorText ?? 'unknown'; });
    const attempts = ['load', 'self-link', 'top-link', 'parent-link', 'blank-link', 'named-link', 'plain-link', 'self-submit'];
    for (const attempt of attempts) {
      const windows = page.context().pages().length;
      await page.goto(origin + '/__harness');
      await page.evaluate(id => { mount(id, 'global', 'ordubashy'); window.__messages = []; window.addEventListener('message', e => window.__messages.push(String(e.data))); }, fx.manager_id);
      await page.waitForSelector('.agvb-row');
      const before = await page.evaluate(() => ({ title: document.title, url: location.href, outside: document.body.innerHTML.replace(/<aside[\s\S]*<\/aside>/, '') }));
      tracked.clear();
      await openRow('Hostile HTML');
      const inner = reader().frameLocator('iframe.fv-frame');
      await inner.locator('#heading').waitFor();
      assert.equal(await inner.locator('#heading').textContent(), 'Hostile HTML', attempt + ': the page script must not run');
      await page.waitForTimeout(2500); // past the page's one-second meta refresh
      if (attempt !== 'load') await inner.locator('#' + attempt).click({ noWaitAfter: true, timeout: 3000 });
      await page.waitForTimeout(800);
      const label = 'hostile ' + attempt;
      assert.equal(await page.evaluate(() => window.__evil), undefined, label + ': nothing reached the parent');
      assert.deepEqual(await page.evaluate(() => window.__messages), [], label + ': no postMessage');
      assert.equal(page.url(), before.url, label + ': the top page was not navigated');
      assert.equal(await page.title(), before.title, label + ': title unchanged');
      assert.equal(await page.evaluate(() => document.body.innerHTML.replace(/<aside[\s\S]*<\/aside>/, '')), before.outside, label + ': DOM outside the pane unchanged');
      assert.equal(page.context().pages().length, windows, label + ': no window opened');
      assert.equal(await page.locator('iframe.agvb-frame').count(), 1, label + ': the reader stays in place');
      const visible = (await Promise.all(page.frames().map(f => f.evaluate(() => document.body?.innerText || '').catch(() => '')))).join('\n');
      assert.ok(!visible.includes('Synthetic report') && !/"text"\s*:/.test(visible), label + ': the owner API target content never became visible');
      for (const t of tracked.values()) {
        assert.ok(!t.responded && !t.finished && /^(csp|net::ERR_BLOCKED_BY_CSP)$/.test(t.failure), `${label}: ${t.url} was announced but not refused by CSP ${JSON.stringify(t)}`);
      }
    }
    console.log('hostile attempts all activated and contained');
    await page.goto(origin + '/__harness');
    await page.evaluate(id => mount(id, 'global', 'ordubashy'), fx.manager_id);
    await page.waitForSelector('.agvb-row');

    // Standalone table page: business table with Load more
    requests.length = 0;
    await page.goto(`${origin}/tables/view?table_id=${fx.business_table}&business_id=${fx.business_id}`);
    await page.waitForFunction(() => document.querySelectorAll('tbody tr').length === 50);
    assert.equal(await page.textContent('#tv-title'), 'Sales');
    assert.equal(await page.textContent('#tv-count'), '50 rows');
    assert.equal(await page.textContent('tbody tr td'), '<b>row 0</b>', 'cells are text, not markup');
    await page.screenshot({ path: path.join(shots, 'table-view-business-desktop.png') });
    await page.click('#tv-more');
    await page.waitForFunction(() => document.querySelectorAll('tbody tr').length === 60);
    assert.equal(await page.isHidden('#tv-more'), true);
    assert.equal(await page.evaluate(() => typeof window.state), 'undefined', 'the full application is not mounted');
    assert.ok(!requests.some(u => /\/js\/(core|tables|agentpane)\.js/.test(u)), 'no application scripts loaded');
    assert.ok(requests.every(u => !u.includes('/biz/null')), 'never /biz/null');

    // Global table: no business_id, root operations
    requests.length = 0;
    await page.goto(`${origin}/tables/view?table_id=${fx.global_table}`);
    await page.waitForFunction(() => document.querySelectorAll('tbody tr').length === 3);
    assert.equal(await page.textContent('#tv-title'), 'Global <i>pipeline</i>');
    assert.ok(requests.some(u => u.includes('/api/root/operations/tables_get')) && requests.every(u => !u.includes('/biz/')), 'Global uses root operations only');
    await page.setViewportSize({ width: 390, height: 700 });
    await page.screenshot({ path: path.join(shots, 'table-view-global-narrow.png') });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'narrow page does not overflow');

    // Failure states are bounded
    await page.goto(`${origin}/tables/view?table_id=${fx.global_table}&business_id=${fx.business_id}`);
    await page.waitForFunction(() => /not found/.test(document.getElementById('tv-status').textContent));
    await page.goto(`${origin}/tables/view?table_id=00000000-0000-4000-8000-000000000000`);
    await page.waitForFunction(() => /not found/.test(document.getElementById('tv-status').textContent));
    const bad = await get(`/tables/view?table_id=nope`).catch(() => null);
    assert.equal((await page.request.get(`${origin}/tables/view?table_id=nope`)).status(), 400);
    assert.equal((await page.request.get(`${origin}/tables/view?table_id=${fx.global_table}&x=1`)).status(), 400);
    assert.deepEqual(errors.filter(e => !/Failed to load resource/.test(e)), [], 'no page errors');
    console.log('agent viewbox real-service browser checks passed; screenshots in ' + shots);
  } finally { await browser?.close(); child.kill(); }
})().catch(error => { console.error(error); process.exit(1); });
