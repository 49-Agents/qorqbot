#!/usr/bin/env node
// Real agpadMount / agent-viewbox.js over a stub owner board API and stub frame targets (no live services or models).
// Real Viewbox -> real /files/view and /tables/view is covered by test_agent_viewbox_real_browser.js.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const shots = process.env.VIEWBOX_SHOTS || '/tmp/vbshots';
const core = fs.readFileSync(path.join(root, 'js/core.js'), 'utf8');
const identity = core.split('const TABS =')[0];
const icons = core.split('\n').filter(line => /^const (ICO_|SVG_)\w+ = `/.test(line)).join('\n');
const errors = [], boards = {}, reads = [];
let hold = null, origin;
const now = Date.now();
const iso = ms => new Date(now - ms).toISOString();
const item = (n, title, link, kind, ago) => ({id: 'item-' + n, link, title, kind, placed_at: iso(ago), attention_id: 'viewbox:item-' + n + ':1'});
const listOf = id => ({status: 'ok', data: {agent_id: id, revision: (boards[id] || []).length, items: boards[id] || []}});
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  if (url.pathname.startsWith('/api/')) {
    const match = url.pathname.match(/^\/api\/root\/agents\/([^/]+)\/viewbox$/);
    res.setHeader('Content-Type', 'application/json');
    if (match) {
      reads.push(match[1]);
      if (hold && hold.agent === match[1]) await new Promise(resolve => { hold.release = resolve; hold.reached(); });
      return res.end(JSON.stringify(listOf(match[1])));
    }
    res.statusCode = 404; return res.end('{}');
  }
  if (url.pathname === '/files/view' || url.pathname === '/tables/view') {
    res.setHeader('Content-Type', 'text/html'); res.setHeader('X-Frame-Options', 'SAMEORIGIN');
    return res.end(`<!doctype html><title>viewer</title><body data-search="${url.search.replace(/"/g, '')}">Viewer ${url.pathname}</body>`);
  }
  if (url.pathname === '/site/ok') { res.setHeader('Content-Type', 'text/html'); return res.end('<!doctype html><body>Ordinary site</body>'); }
  if (url.pathname === '/') {
    res.setHeader('Content-Type', 'text/html');
    return res.end(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/tasks.css"><link rel="stylesheet" href="/css/agentpane.css"><link rel="stylesheet" href="/css/agent-focus.css"><link rel="stylesheet" href="/css/agent-viewbox.css">
      <div class="split agsplit"><aside class="tasks agpad" id="agpad" style="width:900px;height:650px"></aside><button id="agpad-toggle"></button></div>
      <script>
      ${identity}
      const coreData=r=>{if(r?.status!=='ok'||!r.data)throw Error('invalid');return r.data;}, state={scope:'global',biz:null,bizs:[],tab:'agents'};
      const api=async (path,opt)=>{const r=await fetch('/api'+path,opt), d=await r.json();if(!r.ok)throw Error(d.error);return d;};
      const post=async()=>({status:'ok',data:{items:[]}});
      window.viewed=[];const attentionQueue={viewboxViewed:item=>{if(!viewed.includes(item.attention_id))viewed.push(item.attention_id);}};
      ${icons}
      const taskStateSubscribe=()=>()=>{};
      </script><script src="/js/tags.js"></script><script src="/js/work-groups.js"></script><script src="/js/tasks.js"></script><script src="/js/agentpane.js"></script><script src="/js/agent-focus.js"></script><script src="/js/agent-viewbox.js"></script>
      <script>
      agentCurrentWorkMount=()=>{}; window.agfocusMount=()=>{}; window.current=null;
      window.mount=(agentId,scope,role,extra={})=>{current=agentId;
        agpadMount(agentId,$('#agpad'),{biz:scope==='business'?'biz':null,agentId,workspaceScope:scope,workspaceRole:role,open:true,isCurrent:()=>current===agentId,initialView:'viewbox',...extra});};
      window.rows=()=>[...document.querySelectorAll('.agvb-row')].map(r=>({title:r.querySelector('.agvb-title').textContent,age:r.querySelector('.agvb-age').textContent,kind:r.dataset.kind}));
      </script>`);
  }
  const file = path.resolve(root, '.' + url.pathname);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css'); res.end(fs.readFileSync(file));
});
// A separate preview server with no CORS opt-in, like an agent's local app.
const preview = http.createServer((req, res) => {
  if (req.url === '/data') { res.setHeader('Content-Type', 'application/json'); res.end('{"message":"Preview data loaded"}'); return; }
  res.setHeader('Content-Type', 'text/html');
  res.end('<!doctype html><body><p id="data">Loading preview</p><script>fetch("/data").then(r=>r.json()).then(data=>{localStorage.setItem("preview-test","ok");document.querySelector("#data").textContent=data.message;});</script>');
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  await new Promise(resolve => preview.listen(0, '127.0.0.1', resolve));
  origin = 'http://127.0.0.1:' + server.address().port;
  const previewOrigin = 'http://127.0.0.1:' + preview.address().port;
  boards.ordu = [
    item(1, 'Spring brief', '/files/view?path=acme%2Fbrief.md', 'file', 120000),
    item(2, 'Leads <img src=x onerror=window.pwn=1>', '/tables/view?table_id=22222222-2222-4222-8222-222222222222', 'table', 3 * 3600000),
    item(3, 'Local preview', origin + '/site/ok', 'local', 26 * 3600000),
    item(4, 'Hostile', 'javascript:alert(1)', 'web', 30000),
    item(5, 'External', 'https://example.test/page', 'web', 5 * 60000),
  ];
  boards.mkt = [item(9, 'Only Marketing', origin + '/site/ok', 'local', 60000)];
  boards.empty = [];
  boards.preview = [item(10, 'App preview', previewOrigin, 'local', 0)];
  boards.many = Array.from({length: 32}, (_, i) => item(100 + i, 'Item ' + i, 'https://example.test/' + i, 'web', (i + 1) * 60000));
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1200, height: 800}});
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', route => {
      const u = new URL(route.request().url());
      return u.origin === origin || u.origin === previewOrigin || u.protocol === 'about:' || u.protocol === 'data:' ? route.continue() : route.abort();
    });
    await page.goto(origin);
    const tabs = () => page.evaluate(() => [...document.querySelectorAll('.ttab')].map(b => b.dataset.v));

    // --- tabs for Global, business and business-on-Global agents; never auto-opened over Focus/Inbox
    await page.evaluate(() => mount('ordu', 'global', 'ordubashy', {initialView: null}));
    let t = await tabs();
    assert.ok(t.includes('viewbox') && t.indexOf('viewbox') > t.indexOf('inbox'), 'Global Viewbox follows general views: ' + t);
    assert.notEqual(await page.evaluate(() => _agpad.view), 'viewbox', 'Viewbox is not auto-opened');
    assert.equal(await page.getAttribute('.ttab[data-v="viewbox"]', 'aria-label'), 'Viewbox');
    await page.evaluate(() => mount('mkt', 'business', 'business-agent', {initialView: null, workspaceBase: ['tasks', 'inbox']}));
    t = await tabs(); assert.ok(t.includes('viewbox'), 'business agent: ' + t);
    await page.evaluate(() => mount('mkt', 'global', 'business-agent', {initialView: null, biz: 'biz'}));
    t = await tabs(); assert.ok(t.includes('viewbox'), 'business agent on Global: ' + t);

    // --- list: newest first, ages, kind icons, empty state, escaping, per-agent isolation
    await page.evaluate(() => mount('ordu', 'global', 'ordubashy'));
    await page.waitForSelector('.agvb-row');
    const listed = await page.evaluate(() => rows());
    assert.deepEqual(listed.map(r => r.title), ['Spring brief', 'Leads <img src=x onerror=window.pwn=1>', 'Local preview', 'Hostile', 'External'].sort((a, b) => 0), 'server order is preserved');
    assert.deepEqual(listed.map(r => r.age), ['2m', '3h', '1d', 'now', '5m']);
    assert.deepEqual(listed.map(r => r.kind), ['file', 'table', 'local', 'web', 'web']);
    assert.equal(await page.evaluate(() => window.pwn), undefined, 'titles are escaped');
    assert.equal(await page.getAttribute('.agvb-row', 'title'), null, 'Viewbox rows have no hover tooltip');
    assert.match(await page.getAttribute('.agvb-row', 'aria-label'), /brief\.md/, 'link and exact time remain in the accessible name');
    await page.evaluate(() => mount('mkt', 'global', 'business-agent', {biz: 'biz'}));
    await page.waitForFunction(() => document.querySelector('.agvb-row .agvb-title')?.textContent === 'Only Marketing');
    await page.evaluate(() => mount('empty', 'global', 'ordubashy'));
    await page.waitForSelector('.agvb-empty:not([hidden])');
    assert.equal((await page.textContent('.agvb-empty')).trim(), 'Nothing shared yet.');
    await page.evaluate(() => mount('many', 'global', 'ordubashy'));
    await page.waitForSelector('.agvb-row');
    assert.equal(await page.locator('.agvb-row').count(), 30);
    assert.deepEqual(await page.evaluate(()=>viewed), [], 'listing never acknowledges placements');

    // --- list -> trusted viewer frame -> Back; contract URLs are reused verbatim
    await page.evaluate(() => mount('ordu', 'global', 'ordubashy'));
    await page.waitForSelector('.agvb-row');
    await page.screenshot({path: path.join(shots, 'viewbox-list-desktop.png')});
    await page.click('.agvb-row[data-id="item-1"]');
    await page.waitForSelector('iframe.agvb-frame');
    assert.equal(await page.getAttribute('iframe.agvb-frame', 'sandbox'), null, 'exact same-origin viewer path is trusted');
    assert.equal(await page.getAttribute('iframe.agvb-frame', 'referrerpolicy'), 'no-referrer');
    const fileFrame = page.frameLocator('iframe.agvb-frame').locator('body');
    await fileFrame.waitFor();
    await page.waitForFunction(()=>viewed.includes('viewbox:item-1:1'));
    assert.match(await fileFrame.getAttribute('data-search'), /path=acme%2Fbrief\.md/, 'file-viewer URL contract reused as given');
    const open = page.locator('.agvb-open');
    assert.equal(await open.getAttribute('rel'), 'noopener noreferrer');
    assert.equal(await open.getAttribute('target'), '_blank');
    assert.match(await open.getAttribute('href'), /\/files\/view\?path=acme%2Fbrief\.md$/);
    await page.screenshot({path: path.join(shots, 'viewbox-frame-desktop.png')});

    // --- a new placement arrives while the frame is open: the frame is not remounted
    await page.evaluate(() => { window.__frame = document.querySelector('iframe.agvb-frame'); agviewboxRefresh(_agpad); });
    boards.ordu.unshift(item(6, 'Fresh item', origin + '/site/ok', 'local', 0));
    await page.evaluate(() => { _agpad.vb.next = 0; agviewboxTick(_agpad); });
    await page.waitForFunction(() => _agpad.vb.items.length === 6);
    assert.equal(await page.evaluate(() => window.__frame === document.querySelector('iframe.agvb-frame') && window.__frame.isConnected), true, 'open iframe stays mounted');
    await page.click('.agvb-back');
    await page.waitForSelector('.agvb-row');
    await page.waitForFunction(() => rows()[0]?.title === 'Fresh item');
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.id), 'item-1', 'Back restores focus to the opened row');
    assert.equal(await page.locator('iframe').count(), 0, 'Back removes the frame');

    // --- unchanged lists are not repainted
    await page.evaluate(() => { window.__li = document.querySelector('.agvb-row'); _agpad.vb.next = 0; agviewboxTick(_agpad); });
    await page.waitForTimeout(150);
    assert.equal(await page.evaluate(() => window.__li === document.querySelector('.agvb-row')), true, 'same board keeps the same nodes');

    // --- non-viewer content on Qorqut's own origin remains isolated
    await page.click('.agvb-row[data-id="item-3"]');
    await page.waitForSelector('iframe.agvb-frame');
    assert.equal(await page.getAttribute('iframe.agvb-frame', 'sandbox'), 'allow-scripts allow-forms');
    await page.frameLocator('iframe.agvb-frame').locator('body').waitFor();
    await page.evaluate(() => document.querySelector('iframe.agvb-frame').dispatchEvent(new Event('error')));
    assert.equal(await page.locator('.agvb-fallback').isVisible(), true, 'load error shows the bounded fallback');
    assert.equal(await page.locator('.agvb-open').isVisible(), true, 'Open in new tab stays visible');
    await page.click('.agvb-back');

    // --- hostile link never becomes a frame or an anchor
    await page.click('.agvb-row[data-id="item-4"]');
    await page.waitForSelector('.agvb-fallback:not([hidden])');
    assert.equal(await page.locator('iframe').count(), 0, 'javascript: is not framed');
    assert.equal(await page.locator('.agvb-open').isVisible(), false, 'javascript: has no new-tab link');
    assert.equal(await page.evaluate(()=>viewed.includes('viewbox:item-4:1')),false,'invalid link is never acknowledged');
    await page.click('.agvb-back');

    // --- resizing: corner drag, persistence, clamp, Fit, keyboard
    await page.click('.agvb-row[data-id="item-3"]');
    await page.waitForSelector('iframe.agvb-frame');
    const box = async () => page.evaluate(() => { const b = document.querySelector('.agvb-box').getBoundingClientRect(), s = document.querySelector('.agvb-stage').getBoundingClientRect(); return {w: Math.round(b.width), h: Math.round(b.height), sw: Math.round(s.width), sh: Math.round(s.height)}; });
    const fill = await box();
    assert.ok(fill.w >= fill.sw - 1 && fill.h >= fill.sh - 1, 'iframe fills the pane by default');
    const h = await page.locator('.agvb-handle').boundingBox();
    await page.mouse.move(h.x + 9, h.y + 9); await page.mouse.down(); await page.mouse.move(h.x - 191, h.y - 141, {steps: 4}); await page.mouse.up();
    const small = await box();
    assert.ok(Math.abs(small.w - (fill.w - 200)) <= 2 && Math.abs(small.h - (fill.h - 150)) <= 2, `corner drag resizes both ways ${JSON.stringify([fill, small])}`);
    assert.equal(await page.locator('.agvb-shield').count(), 0, 'drag shield removed');
    assert.ok(JSON.parse(await page.evaluate(() => localStorage.getItem('agvb:size:ordu'))).w > 0, 'size remembered per agent');
    await page.focus('.agvb-handle');
    await page.keyboard.press('ArrowLeft'); await page.keyboard.press('Shift+ArrowUp');
    const keyed = await box();
    assert.ok(keyed.w < small.w && keyed.h < small.h, 'keyboard resizes');
    assert.match(await page.textContent('.agvb-size'), /Frame \d+ by \d+ pixels/);
    await page.evaluate(() => { const f = document.querySelector('iframe.agvb-frame'); window.__f2 = f; });
    await page.click('.agvb-fit');
    assert.ok(Math.abs((await box()).w - fill.w) <= 1, 'Fit fills the pane again');
    assert.equal(await page.evaluate(() => window.__f2 === document.querySelector('iframe.agvb-frame')), true, 'resize never reloads the frame');
    await page.evaluate(() => localStorage.setItem('agvb:size:ordu', JSON.stringify({w: 5000, h: 5000})));
    await page.click('.agvb-back'); await page.click('.agvb-row[data-id="item-3"]');
    await page.waitForSelector('iframe.agvb-frame');
    const clamped = await box();
    assert.ok(clamped.w <= clamped.sw && clamped.h <= clamped.sh, 'oversized stored size is clamped');
    await page.evaluate(() => { document.querySelector('#agpad').style.width = '420px'; });
    await page.waitForTimeout(150);
    const shrunk = await box(); assert.ok(shrunk.w <= shrunk.sw, 'shrinking the pane clamps the frame');
    // Keyboard steps start from the clamped rendered size, so one ArrowLeft/ArrowUp shrinks visibly right away.
    await page.focus('.agvb-handle');
    await page.keyboard.press('ArrowLeft'); await page.keyboard.press('ArrowUp');
    const stepped = await box();
    assert.ok(stepped.w === shrunk.w - 16 && stepped.h === shrunk.h - 16, `arrow keys shrink one step from the rendered size ${JSON.stringify([shrunk, stepped])}`);
    const saved = JSON.parse(await page.evaluate(() => localStorage.getItem('agvb:size:ordu')));
    assert.ok(saved.w === stepped.w && saved.h === stepped.h, 'the bounded result is what is persisted');
    await page.setViewportSize({width: 390, height: 700});
    await page.evaluate(() => { document.querySelector('#agpad').style.width = '100%'; document.querySelector('#agpad').style.height = '600px'; });
    await page.waitForTimeout(150);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'no horizontal overflow when narrow');
    await page.screenshot({path: path.join(shots, 'viewbox-frame-narrow.png')});
    await page.click('.agvb-back');
    await page.screenshot({path: path.join(shots, 'viewbox-list-narrow.png')});
    await page.setViewportSize({width: 1200, height: 800});
    await page.evaluate(() => { document.querySelector('#agpad').style.width = '900px'; document.querySelector('#agpad').style.height = '650px'; });

    // --- stale response after agent switch is ignored; everything is disposed
    await page.evaluate(() => mount('ordu', 'global', 'ordubashy'));
    await page.waitForSelector('.agvb-row');
    await page.click('.agvb-row[data-id="item-3"]');
    await page.waitForSelector('iframe.agvb-frame');
    await page.evaluate(() => { window.__old = document.querySelector('iframe.agvb-frame'); });
    let reached; const reachedP = new Promise(r => reached = r);
    hold = {agent: 'mkt', reached};
    await page.evaluate(() => mount('mkt', 'global', 'business-agent', {biz: 'biz'}));
    await reachedP;
    await page.evaluate(() => mount('ordu', 'global', 'ordubashy'));
    hold.release(); hold = null;
    await page.waitForSelector('.agvb-row');
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(() => document.querySelector('.agvb-title').textContent), 'Fresh item', 'late mkt response did not paint');
    assert.equal(await page.evaluate(() => window.__old.isConnected), false, 'previous iframe disposed');
    assert.equal(await page.locator('.agvb-shield').count(), 0);

    // --- attention links select the exact agent and item; a stale/missing item never clears another notice.
    await page.evaluate(()=>{
      history.replaceState(null,'','?focus_item=old#/global/agents');
      window.renderTab=async()=>{const p=new URLSearchParams(location.search);mount(p.get('agent'),'global','ordubashy',{initialView:p.get('workspace')});};
      return agviewboxNavigate({agent_id:'mkt',viewbox_item_id:'item-9'});
    });
    await page.waitForSelector('iframe.agvb-frame');
    await page.waitForFunction(()=>viewed.includes('viewbox:item-9:1'));
    assert.equal(await page.locator('.agvb-name').textContent(),'Only Marketing');
    assert.equal(new URL(page.url()).searchParams.has('viewbox_item'),false);
    assert.equal(new URL(page.url()).searchParams.has('focus_item'),false);
    const viewedBefore=await page.evaluate(()=>viewed.length);
    await page.evaluate(()=>agviewboxNavigate({agent_id:'mkt',viewbox_item_id:'missing'}));
    await page.waitForSelector('.agvb-row');
    assert.equal(await page.evaluate(()=>viewed.length),viewedBefore,'missing deep link leaves unreads intact');

    // Separate local apps retain their origin so their own API and storage work.
    await page.evaluate(() => mount('preview', 'global', 'ordubashy'));
    await page.locator('.agvb-row[data-id="item-10"]').click();
    await page.frameLocator('.agvb-frame').getByText('Preview data loaded', {exact:true}).waitFor();
    assert.equal(await page.locator('.agvb-frame').getAttribute('sandbox'), 'allow-scripts allow-forms allow-same-origin');
    assert.equal(await page.frameLocator('.agvb-frame').locator('body').evaluate(() => localStorage.getItem('preview-test')), 'ok');
    assert.equal(await page.frameLocator('.agvb-frame').locator('body').evaluate(() => { try { void parent.document; return true; } catch (_) { return false; } }), false, 'preview cannot access the dashboard document');

    // --- hidden panes do not poll
    const before = reads.length;
    await page.evaluate(() => { _agpad.open = false; _agpad.vb.next = 0; agpadTick(); });
    await page.waitForTimeout(100);
    assert.equal(reads.length, before, 'no reads while the pane is hidden');
    assert.deepEqual(errors, [], 'no page errors');
    console.log('agent viewbox browser checks passed; screenshots in ' + shots);
  } finally { await browser.close(); server.close(); preview.close(); }
})().catch(error => { console.error(error); process.exit(1); });
