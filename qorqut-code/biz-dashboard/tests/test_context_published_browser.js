#!/usr/bin/env node
// Published-graph readers (Global Graph/object and business Home, Graph, search, object): one shared scoped transport,
// closed schemas, revision/cursor integrity, inert rendering and late-response guards. A disposable loopback page serves the
// committed dashboard files; the API answers only with the exact accepted reader/operation answers recorded in
// tests/fixtures/context-published (see generate.py and provenance.json). Synthetic only: nothing leaves 127.0.0.1.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..'), fixtures = path.join(root, 'tests/fixtures/context-published');
const evidence = process.env.EVIDENCE_DIR;
const fixture = name => JSON.parse(fs.readFileSync(path.join(fixtures, name + '.json'), 'utf8'));
const lists = {main: fixture('main'), later: fixture('later'), window: fixture('window'), empty: fixture('empty'),
  not_initialized: fixture('not-initialized'), unavailable: fixture('unavailable')};
const provenance = fixture('provenance');
const refs = provenance.refs, N = provenance.totals.nodes, E = provenance.totals.edges, QUARTZ = provenance.quartz_hits;
const sameRequest = (a, b) => JSON.stringify(Object.entries(a).sort()) === JSON.stringify(Object.entries(b).sort());

const calls = [], mismatches = [];
const control = {};
const reset = () => { for (const key of Object.keys(control)) delete control[key]; control.scenario = 'main'; control.opScenario = {}; };
reset();
const exchangeFor = (op, body) => {
  const scenario = control.opScenario[op] || control.scenario;
  const order = scenario === 'main' ? ['main'] : ['later', 'window'].includes(scenario) ? [scenario, 'main'] : [scenario];
  for (const name of order) {
    const hit = lists[name].find(item => item.operation === op && sameRequest(item.request, body));
    if (hit) return structuredClone(hit.response);
  }
  return null;
};
const styles = fs.readFileSync(path.join(root, 'index.html'), 'utf8').match(/<link rel="stylesheet"[^>]+>/g).join('\n');
const scripts = ['core', 'san-juan-time', 'ctxmap', 'ctxgraph', 'global-context-graph', 'global-context-graph-renderer', 'context-published',
  'context', 'global-context-base'];
const html = `<!doctype html><html><head><meta charset="utf-8">${styles}</head><body>
  <div id="toasts"></div><button id="wsbtn" hidden></button><div class="global-context" style="height:100vh"><div id="host" style="height:100%"></div></div>
  ${scripts.map(name => `<script src="/js/${name}.js"></script>`).join('')}</body></html>`;

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const json = (data, status = 200) => { res.writeHead(status, {'Content-Type': 'application/json'}); res.end(JSON.stringify(data)); };
  if (url.pathname === '/') { res.setHeader('Content-Type', 'text/html'); return res.end(html); }
  const match = /^\/api(\/root|\/biz\/[^/]+)\/operations\/([a-z_]+)$/.exec(url.pathname);
  if (match) {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw || '{}'), op = match[2];
    calls.push({path: url.pathname, op, body});
    // The sidebar navigation read is another contract (docs/v1.2/SHARED_CONTEXT_READS.md, "Navigation UI contract"); this is a
    // stand-in of that documented shape, not a recorded answer.
    if (op === 'context_navigation_get') return json({status: 'ok', data: {navigation_only: true, counts: {story: 0, table: 2, product: 1, person: null},
      context: {status: 'ready', reason: null, revision: provenance.main_revision}}});
    if (!op.startsWith('context_')) return json({status: 'ok', data: {}});
    if (control.fail?.[op]) { const hit = control.fail[op]; if (hit.when(body)) { if (!hit.keep) delete control.fail[op]; return json({status: 'error', error: {code: 'temporarily_unavailable', message: hit.message || 'The reader is busy'}}, 503); } }
    if (control.hold?.[op] && (!control.hold[op].when || control.hold[op].when(body))) { const gate = control.hold[op]; delete control.hold[op]; gate.seen = body; gate.arrived(); await gate.promise; }
    let response = exchangeFor(op, body) || control.synth?.(op, body) || null;
    if (response === null) { mismatches.push({op, body}); return json({status: 'error', error: {code: 'validation_error', message: 'Unrecorded request'}}, 400); }
    if (control.tamper) response = control.tamper(op, body, response) || response;
    return json(response, response.status === 'ok' ? 200 : response.error.code === 'forbidden' ? 403 : 400);
  }
  if (url.pathname.startsWith('/api/')) { calls.push({path: url.pathname, op: 'other', body: {}}); return json({status: 'ok', data: {}}); }
  const file = path.join(root, url.pathname);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'application/octet-stream');
  res.end(fs.readFileSync(file));
});

const gate = () => { let release, arrived; const promise = new Promise(r => { release = r; }); const seen = new Promise(r => { arrived = r; });
  return {promise, release, arrived, seen: null, wait: seen}; };
const until = async (page, fn, what, arg) => { for (let i = 0; i < 160; i++) { if (await page.evaluate(fn, arg).catch(() => false)) return; await page.waitForTimeout(50); } throw new Error('Timed out waiting for ' + what); };
const text = (page, selector = '#host') => page.evaluate(sel => document.querySelector(sel)?.textContent || '', selector);
const shot = async (page, name) => { if (evidence) await page.screenshot({path: path.join(evidence, name + '.png')}); };
const opsSince = (start, op) => calls.slice(start).filter(call => call.op === op);

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({headless: true, ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {})});
  const pageErrors = [];
  const fresh = async (viewport = {width: 1440, height: 960}) => {
    reset(); calls.length = 0; mismatches.length = 0;
    const context = await browser.newContext({viewport});
    await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    page.on('pageerror', error => pageErrors.push(error.message));
    await page.goto(origin);
    return {page, context};
  };
  const mountGlobal = (page, hash = '#/global/context/graph') => page.evaluate(h => {
    state.scope = 'global'; state.globalTab = 'context';
    const paint = () => { const host = document.querySelector('#host'); host.className = 'pane ctxview global-context-base-view' + (location.hash.endsWith('/graph') ? ' global-context-graph-view' : '');
      renderGlobalContextBase(host); };
    if (!window.__hashWired) { window.__hashWired = true; addEventListener('hashchange', paint); }
    if (location.hash === h) paint(); else location.hash = h;
  }, hash);
  const routeGlobal = (page, hash) => page.evaluate(h => { if (location.hash === h) dispatchEvent(new HashChangeEvent('hashchange')); else location.hash = h; }, hash);
  const mountBusiness = (page, slug, ctx) => page.evaluate(([biz, p]) => {
    state.scope = 'business'; state.biz = biz; state.tab = 'context'; state.ctx = p; state.bizs = [{id: biz, slug: biz, name: biz}];
    renderContext(document.querySelector('#host'));
  }, [slug, ctx]);

  try {
    // ---- 1. schema guard and ref syntax --------------------------------------------------------------------------
    {
      const {page, context} = await fresh();
      const verdicts = await page.evaluate(([good, state]) => {
        const attempt = (op, data) => { try { pubCheck(op, data); return 'ok'; } catch (error) { return error.code; } };
        const out = {good: attempt('context_home', good), unavailable: attempt('context_home', state)};
        out.brief = attempt('context_home', {...good, brief: 'retired'});
        out.timeline = attempt('context_home', {...good, nodes: good.nodes.map(n => ({...n, timeline_next_cursor: 'x'}))});
        out.person = attempt('context_find', {status: 'ready', revision: good.revision, query: '', page: good.page, items: [{...good.nodes[0], kind: 'person'}]});
        out.refs = ['node:abcde', 'edge:abcde', 'source:abcde', '<object>topic_A</object>', 'node:abcd', 'Node:abcde', 'abcde'].map(pubRefKind);
        return out;
      }, [lists.main.find(e => e.operation === 'context_home').response.data, lists.unavailable[0].response.data]);
      assert.deepEqual(verdicts, {good: 'ok', unavailable: 'ok', brief: 'invalid_response', timeline: 'invalid_response', person: 'invalid_response',
        refs: ['node', 'edge', 'source', null, null, null, null]});
      await context.close();
      console.log('ok closed schemas reject retired fields; only node:/edge:/source: refs are accepted');
    }

    // ---- 2. one graph, Global and two business routes -------------------------------------------------------------
    const seenGraphs = [];
    {
      for (const target of [{kind: 'global'}, {kind: 'business', slug: 'alpha'}, {kind: 'business', slug: 'beta/two'}]) {
        const {page, context} = await fresh();
        if (target.kind === 'global') await mountGlobal(page); else await mountBusiness(page, target.slug, '');
        await until(page, () => document.querySelector('.pub-graph-status')?.textContent.includes('items shown'), 'graph status');
        await until(page, () => document.querySelectorAll('.ctxgraph-node').length > 3, 'graph nodes');
        const status = await text(page, '.pub-graph-status');
        const kinds = await page.evaluate(() => [...new Set([...document.querySelectorAll('.ctxgraph-node')].map(n => n.className.split(' ')[1]))].sort());
        seenGraphs.push({status, kinds, calls: calls.map(c => c.path + ' ' + JSON.stringify(c.body)).filter(c => c.includes('context_'))});
        const expectedPath = target.kind === 'global' ? '/api/root/operations/context_home' : `/api/biz/${encodeURIComponent(target.slug)}/operations/context_home`;
        assert.deepEqual(calls.filter(c => c.op === 'context_home').map(c => [c.path, c.body]), [[expectedPath, {limit: 30}]], 'one Home read with the fixed bound');
        assert.deepEqual(calls.filter(c => c.op !== 'context_home' && c.op !== 'context_navigation_get'), [], 'only graph reads and the sidebar navigation counts');
        assert.equal(status, `30 of ${N} items shown · More available`);
        assert.ok(!status.includes(provenance.main_revision.slice(0, 8)) && !/revision|endpoint|complete graph/i.test(status), 'no revision or mechanics in the status');
        // an endpoint whose node is not loaded is its own visual kind, labelled only with what the endpoint states
        await page.fill('.ctxgraph-search input', 'Rim leaf 1');
        await until(page, () => document.querySelector('.ctxgraph-results button[data-select]'), 'search result');
        await page.click('.ctxgraph-results button[data-select]');
        await until(page, () => !document.querySelector('.ctxgraph-reader').hidden, 'endpoint reader');
        assert.match(await text(page, '.ctxgraph-reader'), /More available\. Open the record for details/);
        assert.match(await text(page, '.ctxgraph-reader'), /Endpoint \(not loaded\)/);
        assert.equal(await page.evaluate(() => document.querySelector('.ctxgraph-reader').textContent.includes('node:zz001')), false, 'no invented ref label in the title');
        assert.deepEqual(mismatches, []);
        if (target.kind === 'business') assert.equal(await page.evaluate(() => state.biz), target.slug, 'the selected business is never replaced');
        await context.close();
      }
      assert.equal(seenGraphs[1].status, seenGraphs[0].status); assert.equal(seenGraphs[2].status, seenGraphs[0].status);
      assert.deepEqual(seenGraphs[1].kinds, seenGraphs[0].kinds);
      console.log('ok the same published graph is served identically from Global and two business routes');
    }

    // ---- 3. graph: load more, markers, selection and opening ------------------------------------------------------
    {
      const {page, context} = await fresh();
      await mountGlobal(page);
      await until(page, () => document.querySelectorAll('.ctxgraph-node').length > 3, 'graph nodes');
      assert.match(await text(page, '.pub-graph-notes'), /more connections than are shown/);
      assert.equal(await page.evaluate(() => document.querySelector('.pub-more').hidden), false);
      await page.click('.pub-more');
      await until(page, n => new RegExp(`60 of ${n} items shown`).test(document.querySelector('.pub-graph-status').textContent), 'second page', N);
      await page.click('.pub-more');
      await until(page, n => new RegExp(`^${n} items$`).test(document.querySelector('.pub-graph-status').textContent), 'third page', N);
      assert.equal(await page.evaluate(() => document.querySelector('.pub-more').hidden), true);
      assert.ok(!/complete graph|More available/.test(await text(page, '.pub-graph-status')), 'a fully loaded list says nothing more is available');
      const home = calls.filter(c => c.op === 'context_home');
      assert.equal(home.length, 3); assert(home.every(c => c.body.limit === 30)); assert(!home[0].body.cursor && home[1].body.cursor && home[2].body.cursor);
      // focus Alice: her self relationship is its own node beside her, and both parallel edges to Atlas are drawn
      await page.fill('.ctxgraph-search input', 'Alice Rivera');
      await until(page, () => document.querySelector('.ctxgraph-results button[data-select="node:alice"]'), 'alice result');
      await page.click('.ctxgraph-results button[data-select="node:alice"]');
      await until(page, () => !document.querySelector('.ctxgraph-reader').hidden, 'alice reader');
      await page.click('.ctxgraph-reader [data-action="focus"]');
      await until(page, () => document.querySelector('.ctxgraph-node[data-node="edge:self1"]'), 'self edge node');
      const lines = await page.evaluate(() => [...document.querySelectorAll('.ctxgraph-edges > *')].length);
      assert(lines >= 4, 'edge lines drawn for the focused neighbourhood: ' + lines);
      await page.evaluate(() => document.querySelector('.ctxgraph-node[data-node="edge:self1"]').click());
      await until(page, () => /Both ends are the same item/.test(document.querySelector('.ctxgraph-reader').textContent), 'self reader');
      assert.match(await text(page, '.ctxgraph-reader'), /Alice mentors herself/);
      const drawn = await page.evaluate(() => [...document.querySelectorAll('[data-relationship]')].map(e => e.dataset.relationship));
      assert(drawn.includes('edge:lead1') && drawn.includes('edge:lead2'), 'both parallel edges to Atlas are drawn: ' + drawn);
      // open the Alice node through the reader pane
      const open = await page.evaluate(() => {
        const button = [...document.querySelectorAll('.ctxgraph-node.node')].find(b => b.dataset.node === 'node:alice');
        if (!button) return 'missing'; button.click(); return 'clicked';
      });
      assert.equal(open, 'clicked');
      await until(page, () => !document.querySelector('.ctxgraph-reader').hidden && /Alice/.test(document.querySelector('.ctxgraph-reader').textContent), 'reader');
      await page.click('.ctxgraph-open');
      await until(page, () => location.hash === '#/global/context/object/node%3Aalice', 'object route');
      await until(page, () => document.querySelector('.pub-object'), 'object view');
      assert.deepEqual(mismatches, []);
      await context.close();
      console.log('ok graph load-more appends same-revision pages, keeps bounded-view markers and opens exact objects');
    }

    // ---- 4. readiness states --------------------------------------------------------------------------------------
    for (const [scenario, selector, pattern] of [['empty', '.pub-graph-status[data-pub-state="empty"]', /Nothing has been added to Context yet/],
      ['not_initialized', '[data-pub-state="not_initialized"]', /Nothing has been added to Context yet/], ['unavailable', '[data-pub-state="unavailable"]', /Couldn't load/]]) {
      const {page, context} = await fresh();
      control.scenario = scenario;
      await mountGlobal(page);
      await until(page, sel => !!document.querySelector(sel), scenario, selector);
      const shown = await text(page);
      assert.match(shown, pattern);
      assert.equal(await page.evaluate(() => document.querySelectorAll('.ctxgraph-node').length), 0, 'no fake nodes in ' + scenario);
      if (scenario !== 'empty') {
        control.scenario = 'main';
        await page.click('.pub-retry');
        await until(page, () => document.querySelectorAll('.ctxgraph-node').length > 3, 'recovery');
      }
      assert.equal(calls.filter(c => c.op !== 'context_home').length, 0);
      await context.close();
    }
    {
      const {page, context} = await fresh();
      control.scenario = 'empty';
      await mountBusiness(page, 'alpha', '_home');
      await until(page, () => document.querySelector('[data-pub-state="empty"]'), 'business empty home');
      assert.match(await text(page), /Shared Context/);
      control.scenario = 'not_initialized';
      await page.evaluate(() => ctxGo('_home'));
      await until(page, () => document.querySelector('[data-pub-state="not_initialized"]'), 'business not initialized');
      await context.close();
      console.log('ok ready-empty, not-initialized and unavailable are distinct, with retry and no invented nodes');
    }

    // ---- 5. page integrity: nonadvancing, other revision, differently bound, failed continuation --------------------
    {
      const {page, context} = await fresh();
      await mountBusiness(page, 'alpha', '_home');
      await until(page, () => document.querySelectorAll('.pub-node').length === 30, 'first Home page');
      assert.ok(!/ of \d+ loaded/.test(await text(page)), 'no loaded counters');
      // a continuation that fails is retried with its exact cursor
      control.fail = {context_home: {when: body => !!body.cursor, message: 'The reader is busy'}};
      await page.click('.pub-more');
      await until(page, () => document.querySelector('[data-pub-error="transient"]'), 'transient error');
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-node').length), 30, 'earlier pages stay visible');
      const failedCursor = calls.filter(c => c.op === 'context_home').at(-1).body.cursor;
      await page.click('.pub-again');
      await until(page, () => document.querySelectorAll('.pub-node').length === 60, 'retry succeeded');
      assert.equal(calls.filter(c => c.op === 'context_home').at(-1).body.cursor, failedCursor, 'the retry repeats the exact cursor');
      // a nonadvancing cursor is a visible, recoverable integrity error
      control.tamper = (op, body, response) => { if (op === 'context_home' && body.cursor && response.status === 'ok') Object.assign(response.data.page, {next_cursor: body.cursor, has_more: true}); return response; };
      await page.click('.pub-more');
      await until(page, () => document.querySelector('[data-pub-error="integrity"]'), 'integrity error');
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-node').length), 60, 'earlier pages are kept');
      assert.match(await text(page), /Context changed\. Reload/);
      control.tamper = null;
      const before = calls.filter(c => c.op === 'context_home').length;
      await page.click('.pub-restart');
      for (let i = 0; i < 100 && (calls.filter(c => c.op === 'context_home').length < before + 2 || await page.evaluate(() => document.querySelectorAll('.pub-node').length !== 60 || !!document.querySelector('.pub-error'))); i++) await new Promise(r => setTimeout(r, 25));
      assert.equal(calls.filter(c => c.op === 'context_home')[before].body.cursor, undefined, 'the reload starts from the first page');
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-node').length), 60, 'the loaded depth is read again');
      // a page at another revision is refused
      control.tamper = (op, body, response) => { if (op === 'context_home' && body.cursor && response.status === 'ok') response.data.revision = '0'.repeat(40); return response; };
      await page.click('.pub-more');
      await until(page, () => document.querySelector('[data-pub-error="integrity"]'), 'revision error');
      assert.match(await text(page), /Context changed\. Reload/);
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-node').length), 60, 'the loaded pages stay');
      control.tamper = null;
      await page.click('.pub-restart');
      await until(page, () => document.querySelectorAll('.pub-node').length === 60 && !document.querySelector('.pub-error'), 'restart again');
      // repeated records on a later page
      control.tamper = (op, body, response) => { if (op === 'context_home' && body.cursor && response.status === 'ok') response.data.nodes[0] = structuredClone(JSON.parse(JSON.stringify(lists.main.find(e => e.operation === 'context_home' && !e.request.cursor).response.data.nodes[0]))); return response; };
      await page.click('.pub-more');
      await until(page, () => document.querySelector('[data-pub-error="integrity"]'), 'repeat error');
      assert.match(await text(page), /Context changed\. Reload/);
      control.tamper = null;
      // a cursor bound to a different request is refused by the reader and shown as an error, earlier pages kept
      control.tamper = (op, body, response) => {
        if (op === 'context_home' && !body.cursor && response.status === 'ok') {
          const foreign = lists.main.find(e => e.operation === 'context_home' && e.response.status === 'error');
          response.data.page.next_cursor = foreign.request.cursor;
        }
        return response;
      };
      await mountBusiness(page, 'alpha', '_home');
      await until(page, () => document.querySelectorAll('.pub-node').length === 30 && !document.querySelector('.pub-error'), 'tampered first page');
      await page.click('.pub-more');
      await until(page, () => document.querySelector('.pub-error'), 'foreign cursor error');
      assert.match(await text(page), /Couldn't load\. Retry/); assert.ok(!/cursor|validation|Invalid/i.test(await text(page)), 'no raw backend error');
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-node').length), 30);
      assert.deepEqual(mismatches, []);
      await context.close();
      console.log('ok failed, repeated, nonadvancing, other-revision and differently bound pages keep earlier pages and stay recoverable');
    }

    // ---- 6. business search ---------------------------------------------------------------------------------------
    {
      const {page, context} = await fresh();
      await mountBusiness(page, 'alpha', '_home');
      await until(page, () => document.querySelectorAll('.pub-node').length === 30, 'home');
      const find = async query => { await page.fill('#ctxsearch', query); await until(page, q => document.querySelector('.ctx-search-results .sh')?.textContent.includes('Search: ' + q) || document.querySelector('.pub-error, .pub-state'), 'search for ' + query, query); };
      await find('Quartz');
      await until(page, () => document.querySelectorAll('[data-pub-result]').length === 50, 'first find page');
      assert.deepEqual(calls.filter(c => c.op === 'context_find').map(c => c.body), [{query: 'Quartz', limit: 50}]);
      await page.click('.pub-more');
      await until(page, () => document.querySelectorAll('[data-pub-result], [data-pub-source]').length === 100, 'second find page');
      await page.click('.pub-more');
      await until(page, q => document.querySelectorAll('[data-pub-result], [data-pub-source]').length === q, 'third find page', QUARTZ);
      assert.equal(await page.evaluate(() => !!document.querySelector('.pub-more')), false);
      assert.equal(calls.filter(c => c.op === 'context_find').at(-1).body.cursor.length > 10, true);
      // an empty result is not an error and not proof of absence
      await find('zzznomatchzzz');
      await until(page, () => document.querySelector('[data-pub-state="no-results"]'), 'empty search');
      assert.match(await text(page), /No results/);
      // a failed search is an error with its own retry
      control.fail = {context_find: {when: body => body.query === 'Atlas', message: 'Search is down'}};
      await find('Atlas');
      await until(page, () => document.querySelector('[data-pub-error="transient"]'), 'failed search');
      assert.ok(!/Search is down/.test(await text(page)), 'the raw error is not shown');
      // the search window ends the paging with a request to narrow
      control.scenario = 'window';
      await find('Quartz');
      await until(page, () => document.querySelector('.pub-more'), 'window first page');
      await page.click('.pub-more');
      await until(page, () => document.querySelector('.pub-window'), 'window exhausted');
      assert.match(await text(page), /Narrow the search/);
      assert.equal(await page.evaluate(() => !!document.querySelector('.pub-more')), false);
      // Kinds: edge and source results render with their refs
      control.scenario = 'main';
      await find('Atlas');
      await until(page, () => document.querySelectorAll('[data-pub-result], [data-pub-source]').length > 2, 'atlas results');
      const shown = await text(page);
      assert.match(shown, /Relationship/); assert.match(shown, /Source/);
      assert.equal(await page.evaluate(() => window.__pwn), undefined);
      assert.deepEqual(mismatches, []);
      await context.close();
      console.log('ok business search pages, empty, failed and exhausted windows are distinct; hits keep kind and ref');
    }

    // ---- 7. object detail: node ------------------------------------------------------------------------------------
    {
      const {page, context} = await fresh();
      await mountGlobal(page, '#/global/context/object/' + encodeURIComponent('node:alice'));
      await until(page, () => document.querySelector('.pub-object') && document.querySelector('.pub-neighbourhood .cards'), 'alice');
      const shown = await text(page);
      assert.match(shown, /score\s*0/); assert.match(shown, /active\s*false/); assert.match(shown, /role\s*lead, founder/);
      assert.match(shown, /Alice mentors herself/); assert.match(shown, /Alice also sponsors the Atlas program/); assert.match(shown, /Alice leads the Atlas program/);
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-relationships [data-pub-edge]').length), 6, 'parallel and self edges all listed');
      assert.match(shown, /Sources/);
      assert.ok(!/node:|edge:|source:[a-z0-9]{5}|published revision|[0-9a-f]{40}/i.test(shown), 'no reference, revision or commit is shown');
      assert.deepEqual(calls.filter(c => c.op === 'context_object').map(c => c.body), [{ref: 'node:alice', history_limit: 10, text_limit: 16000}]);
      assert.deepEqual(calls.filter(c => c.op === 'context_expand').map(c => c.body), [{ref: 'node:alice', depth: 1, direction: 'both', limit: 50}]);
      assert.equal(calls.filter(c => c.op === 'context_changes').length, 0, 'no change-diagnostic read');
      assert(calls.every(c => c.path.startsWith('/api/root/operations/context_')), 'Global reads stay on /root/operations');
      // history pages are independent of text and carry only the returned cursor
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent('node:atlas'));
      await until(page, () => document.querySelectorAll('.pub-history').length === 10, 'atlas history');
      const before = calls.length;
      await page.click('.pub-history-more');
      await until(page, () => document.querySelectorAll('.pub-history').length === 13, 'atlas history complete');
      const continuation = opsSince(before, 'context_object');
      assert.equal(continuation.length, 1); assert.deepEqual(Object.keys(continuation[0].body).sort(), ['history_cursor', 'history_limit', 'ref']);
      assert.equal(await page.evaluate(() => !!document.querySelector('.pub-history-more')), false);
      // truncated summary markers
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent('node:bigno'));
      await until(page, () => document.querySelector('.pub-object h2'), 'bigno');
      // endpoints of an edge route to the graph reader
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent('edge:self1'));
      await until(page, () => document.querySelector('.pub-endpoints'), 'edge');
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-endpoints [data-pub-ref="node:alice"]').length), 2);
      await page.click('.pub-endpoints [data-pub-ref="node:alice"]');
      await until(page, () => location.hash === '#/global/context/object/node%3Aalice', 'endpoint route');
      assert.deepEqual(mismatches, []);
      await context.close();
      console.log('ok node/edge detail: attributes keep 0/false, parallel and self edges, history and independent cursors');
    }

    // ---- 8. neighbourhood: pages, truncation, publication change --------------------------------------------------
    {
      const {page, context} = await fresh();
      await mountGlobal(page, '#/global/context/object/' + encodeURIComponent('node:hubxx'));
      await until(page, () => document.querySelectorAll('.pub-neighbourhood .pub-edge').length === 50, 'hub neighbourhood');
      assert.match(await text(page), /More available/);
      await page.click('.pub-expand-more');
      await until(page, () => document.querySelectorAll('.pub-neighbourhood .pub-edge').length === 55, 'hub neighbourhood complete');
      assert.equal(calls.filter(c => c.op === 'context_expand').at(-1).body.cursor.length > 10, true);
      // the Context is published again between the object and its neighbourhood
      reset(); calls.length = 0;
      control.opScenario = {context_expand: 'later'};
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent('node:atlas'));
      await until(page, () => document.querySelector('[data-pub-error="publication-changed"]'), 'changed publication');
      assert.match(await text(page), /Context changed\. Reload/);
      control.opScenario = {context_expand: 'later', context_object: 'later'};
      await page.click('.pub-reload');
      await until(page, () => document.querySelector('.pub-neighbourhood .cards') && !document.querySelector('[data-pub-error="publication-changed"]'), 'reloaded');
      assert.match(await text(page), /Atlas partners with the late partner/);
      assert.deepEqual(mismatches, []);
      await context.close();
      console.log('ok expansion pages, truncation markers and a publication change between object and neighbourhood');
    }

    // ---- 9. source: text pages, adversarial text, withdrawn, evidence, origin_ref -----------------------------------
    {
      const {page, context} = await fresh();
      await mountGlobal(page, '#/global/context/object/' + encodeURIComponent('node:alice'));
      await until(page, () => document.querySelector('.pub-support .pub-evidence'), 'evidence');
      const start = calls.length;
      // follow evidence to the source; nothing but graph reads happens
      await page.click('.pub-support .pub-evidence [data-pub-ref]');
      await until(page, () => document.querySelector('.pub-object[data-pub-kind="source"]'), 'source');
      assert(calls.slice(start).every(c => c.op.startsWith('context_')), 'following evidence only calls graph reads');
      assert.ok(!/origin reference|example\.invalid/i.test(await text(page)), 'the origin reference is not shown');
      assert.equal(await page.evaluate(() => !!document.querySelector('.pub-origin-ref a, [href*="example.invalid"]')), false);
      assert(!calls.some(c => JSON.stringify(c.body).includes('example.invalid')), 'origin_ref is never sent anywhere');
      // the survey: three text pages
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent(refs.source_survey));
      await until(page, () => document.querySelector('.pub-source-text'), 'survey');
      assert.equal(await page.evaluate(() => Array.from(document.querySelector('.pub-source-text')?.textContent || '').length), 16000);
      assert.ok(!/ of 43327|characters/.test(await text(page)), 'no character counter');
      let before = calls.length;
      await page.click('.pub-text-more');
      await until(page, () => Array.from(document.querySelector('.pub-source-text')?.textContent || '').length===32000, 'second text page');
      const textCall = opsSince(before, 'context_object');
      assert.deepEqual(Object.keys(textCall[0].body).sort(), ['ref', 'text_cursor', 'text_limit'], 'text continuation uses only text_cursor');
      await page.click('.pub-text-more');
      await until(page, () => Array.from(document.querySelector('.pub-source-text')?.textContent || '').length===43327 && !document.querySelector('.pub-text-more'), 'complete text');
      assert.equal(await page.evaluate(() => !!document.querySelector('.pub-text-more')), false);
      const inert = await page.evaluate(() => ({pwn: window.__pwn, injected: !!document.querySelector('#injected'), scripts: document.querySelectorAll('.pub-source-text script, .pub-source-text img, .pub-source-text b').length,
        literal: document.querySelector('.pub-source-text').textContent.includes('<script>window.__pwn=9</script>'), tail: document.querySelector('.pub-source-text').textContent.includes('</pre><b id="injected">markup</b>'),
        title: document.querySelector('.pub-object h2').textContent, titleMarkup: !!document.querySelector('.pub-object h2 b')}));
      assert.deepEqual(inert, {pwn: undefined, injected: false, scripts: 0, literal: true, tail: true, title: 'Survey <b>markup</b> title', titleMarkup: false});
      // a text page that leaves a gap, overlaps, or comes from another revision is refused and the loaded text stays
      for (const [label, tamper, pattern] of [
        ['gap', content => { content.offset += 5; }, /Context changed\. Reload/],
        ['overlap', content => { content.offset -= 5; }, /Context changed\. Reload/],
        ['revision', (content, data) => { data.revision = '1'.repeat(40); }, /Context changed\. Reload/]]) {
        await routeGlobal(page, '#/global/context/object/' + encodeURIComponent(refs.source_survey));
        await until(page, () => Array.from(document.querySelector('.pub-source-text')?.textContent || '').length===16000, 'survey ' + label);
        control.tamper = (op, body, response) => { if (op === 'context_object' && body.text_cursor && response.status === 'ok') tamper(response.data.content, response.data); return response; };
        await page.click('.pub-text-more');
        await until(page, () => document.querySelector('[data-pub-error="integrity"]'), 'text integrity ' + label);
        assert.match(await text(page), pattern);
        assert.equal(await page.evaluate(() => Array.from(document.querySelector('.pub-source-text')?.textContent || '').length), 16000, 'the loaded text is kept');
        control.tamper = null;
      }
      // a failed text continuation is retried with the exact cursor
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent(refs.source_survey));
      await until(page, () => Array.from(document.querySelector('.pub-source-text')?.textContent || '').length===16000, 'survey retry');
      control.fail = {context_object: {when: body => !!body.text_cursor, message: 'Text is busy'}};
      await page.click('.pub-text-more');
      await until(page, () => document.querySelector('[data-pub-error="transient"]'), 'text transient');
      const failed = calls.filter(c => c.op === 'context_object' && c.body.text_cursor).at(-1).body.text_cursor;
      await page.click('.pub-text-again');
      await until(page, () => Array.from(document.querySelector('.pub-source-text')?.textContent || '').length===32000, 'text retried');
      assert.equal(calls.filter(c => c.op === 'context_object' && c.body.text_cursor).at(-1).body.text_cursor, failed);
      // a withdrawn source: no committed text, explicit withdrawn marker, tombstoned history
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent(refs.source_temp));
      await until(page, () => document.querySelector('.pub-withdrawn'), 'withdrawn');
      assert.match(await text(page), /Removed/); assert.match(await text(page), /No text is available/);
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-tombstone').length > 0), true);
      assert.deepEqual(mismatches, []);
      assert(calls.every(c => c.op.startsWith('context_')), 'no non-graph request anywhere');
      assert(calls.every(c => !JSON.stringify(c.body).includes('"limit":500')));
      await context.close();
      console.log('ok source text pages stay inert and bound; gaps, overlaps and revision changes are refused; withdrawn sources are explicit');
    }

    // ---- 10. no change diagnostics; malformed refs ---------------------------------------------------------------------
    {
      const {page, context} = await fresh();
      await mountGlobal(page, '#/global/context/object/' + encodeURIComponent('node:atlas'));
      await until(page, () => document.querySelector('.pub-object') && document.querySelector('.pub-history'), 'atlas');
      await page.waitForTimeout(300);
      assert.equal(calls.filter(c => c.op === 'context_changes').length, 0, 'the display-only change feed is not read');
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-change, [data-pub-state^="changes-"]').length), 0);
      assert.ok(!/Change diagnostics/.test(await text(page)));
      // malformed or old refs are rejected visibly, with no read
      const start = calls.length;
      for (const hash of ['#/global/context/object/' + encodeURIComponent('<object>topic_A</object>'), '#/global/context/object/node%3Aabcd', '#/global/context/object/%E0%A4%A']) {
        await routeGlobal(page, hash);
        await until(page, () => document.querySelector('[role="alert"]'), 'rejected ' + hash);
        assert.match(await text(page), /Unavailable/);
      }
      assert.equal(calls.length, start, 'malformed refs make no read');
      await mountBusiness(page, 'alpha', '_obj/<object>topic_A</object>');
      await until(page, () => document.querySelector('.pub-route-error'), 'business rejected');
      assert.equal(calls.filter(c => c.op !== 'context_navigation_get').length, start, 'a rejected business ref makes no graph read');
      await context.close();
      console.log('ok no change-diagnostic panel or read; malformed and old refs are rejected without a read');
    }

    // ---- 11. late responses --------------------------------------------------------------------------------------------
    {
      const {page, context} = await fresh();
      const slow = gate(); control.hold = {context_home: slow};
      await mountGlobal(page);
      await slow.wait;
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent('node:acme1'));
      await until(page, () => document.querySelector('.pub-object'), 'object after navigation');
      slow.release();
      await page.waitForTimeout(400);
      assert.equal(await page.evaluate(() => document.querySelectorAll('.ctxgraph, .global-context-graph').length), 0, 'a late Home never paints into the object view');
      assert.match(await text(page), /Acme Corporation/);
      // object A answers after the user opened object B
      const late = gate(); control.hold = {context_object: late};
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent('node:bobby'));
      await late.wait;
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent('node:alice'));
      await until(page, () => /Alice Rivera/.test(document.body.textContent), 'alice');
      late.release();
      await page.waitForTimeout(400);
      assert.equal(await page.evaluate(() => document.querySelector('.pub-object h2').textContent), 'Alice Rivera', 'the earlier object never replaces the current one');
      // business scope change while a read is pending
      await mountBusiness(page, 'alpha', '_home');
      await until(page, () => document.querySelectorAll('.pub-node').length === 30, 'home');
      const pending = gate(); control.hold = {context_home: pending};
      await page.click('.pub-refresh');
      await pending.wait;
      await page.evaluate(() => { state.biz = 'beta'; state.bizs = [{id: 'beta', slug: 'beta', name: 'beta'}]; renderContext(document.querySelector('#host')); });
      await until(page, () => document.querySelectorAll('.pub-node').length === 30, 'beta home');
      pending.release();
      await page.waitForTimeout(400);
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-node').length), 30);
      await context.close();
      console.log('ok late Home, object and scope-change responses never replace the current view');
    }

    // ---- 11b. request ownership: restart, reload, double activation ------------------------------------------------------
    {
      // Refresh while a continuation is outstanding (Home): the replacement is issued and paging keeps working
      const {page, context} = await fresh();
      await mountBusiness(page, 'alpha', '_home');
      await until(page, () => document.querySelectorAll('.pub-node').length === 30, 'home');
      const held = gate(); control.hold = {context_home: held};
      await page.click('.pub-more'); await held.wait;
      await page.click('.pub-refresh');
      await until(page, () => document.querySelectorAll('.pub-node').length === 30 && document.querySelector('.pub-more'), 'replacement first page');
      held.release(); await page.waitForTimeout(400);
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-node').length), 30, 'the superseded page never appends');
      await page.click('.pub-more');
      await until(page, () => document.querySelectorAll('.pub-node').length === 60, 'paging works after the restart');
      assert.equal(await page.evaluate(() => new Set([...document.querySelectorAll('[data-pub-node]')].map(n => n.dataset.pubNode)).size), 60);
      await context.close();
    }
    {
      // The same for the graph's Refresh
      const {page, context} = await fresh();
      await mountGlobal(page);
      await until(page, () => document.querySelectorAll('.ctxgraph-node').length > 3, 'graph');
      const held = gate(); control.hold = {context_home: held};
      await page.click('.pub-more'); await held.wait;
      await page.click('.pub-refresh');
      await until(page, () => /30 of \d+ items shown/.test(document.querySelector('.pub-graph-status').textContent) && !document.querySelector('.pub-more').hidden, 'graph replacement');
      held.release(); await page.waitForTimeout(400);
      assert.match(await text(page, '.pub-graph-status'), new RegExp(`30 of ${N} items shown`));
      await page.click('.pub-more');
      await until(page, n => new RegExp(`60 of ${n} items shown`).test(document.querySelector('.pub-graph-status').textContent), 'graph pages again', N);
      await context.close();
    }
    {
      // Same-object reload with an older section request pending: its late page is ignored and history pages again without duplicates
      const {page, context} = await fresh();
      control.opScenario = {context_expand: 'later'};
      await mountGlobal(page, '#/global/context/object/' + encodeURIComponent('node:atlas'));
      await until(page, () => document.querySelector('[data-pub-error="publication-changed"]') && document.querySelectorAll('.pub-history').length === 10, 'changed publication');
      const held = gate(); control.hold = {context_object: held};
      await page.click('.pub-history-more'); await held.wait;
      control.opScenario = {context_expand: 'later', context_object: 'later'};
      await page.click('.pub-reload');
      await until(page, () => document.querySelector('.pub-history') && !document.querySelector('[data-pub-error="publication-changed"]'), 'reloaded');
      const afterReload = await page.evaluate(() => document.querySelectorAll('.pub-history').length);
      held.release(); await page.waitForTimeout(400);
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-history').length), afterReload, 'the old history page is ignored');
      await page.click('.pub-history-more');
      await until(page, () => !document.querySelector('.pub-history-more') && document.querySelector('.pub-history'), 'history complete again');
      assert.equal(await page.evaluate(() => new Set([...document.querySelectorAll('[data-pub-history]')].map(n => n.dataset.pubHistory)).size
        === document.querySelectorAll('[data-pub-history]').length), true, 'no duplicated history rows');
      await context.close();
    }
    {
      // Double activation of the text continuation sends one request and never doubles the text
      const {page, context} = await fresh();
      await mountGlobal(page, '#/global/context/object/' + encodeURIComponent(refs.source_survey));
      await until(page, () => Array.from(document.querySelector('.pub-source-text')?.textContent || '').length===16000, 'survey');
      const held = gate(); control.hold = {context_object: held};
      const start = calls.length;
      await page.click('.pub-text-more'); await held.wait;
      assert.equal(await page.evaluate(() => document.querySelector('.pub-text-more').disabled), true, 'the control is disabled while the page loads');
      await page.evaluate(() => document.querySelector('.pub-text-more').click());
      held.release();
      await until(page, () => Array.from(document.querySelector('.pub-source-text')?.textContent || '').length===32000, 'second page');
      assert.equal(opsSince(start, 'context_object').filter(c => c.body.text_cursor).length, 1, 'one request for one cursor');
      assert.equal(await page.evaluate(() => Array.from(document.querySelector('.pub-source-text').textContent).length), 32000);
      // an old continuation that finishes after the source was reloaded changes nothing: the record's history is made to repeat a
      // cursor (an integrity error whose Restart reloads the whole source) while a text page is still outstanding
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent(refs.source_survey));
      control.tamper = (op, body, response) => { if (op === 'context_object' && !body.text_cursor && !body.history_cursor && response.status === 'ok') Object.assign(response.data.history.page, {has_more: true, next_cursor: 'cursor-one-aaaa'}); return response; };
      control.synth = (op, body) => {
        if (op !== 'context_object' || body.history_cursor !== 'cursor-one-aaaa') return null;
        const first = structuredClone(exchangeFor('context_object', {ref: refs.source_survey, history_limit: 10, text_limit: 16000}));
        Object.assign(first.data.history.page, {has_more: true, next_cursor: 'cursor-one-aaaa'}); return first;
      };
      await until(page, () => document.querySelector('.pub-history-more') && Array.from(document.querySelector('.pub-source-text')?.textContent || '').length===16000, 'survey with a continuation');
      await page.click('.pub-history-more');
      await until(page, () => document.querySelector('.pub-history-restart'), 'history integrity');
      control.tamper = null; control.synth = null;
      const slow = gate(); control.hold = {context_object: slow};
      await page.click('.pub-text-more'); await slow.wait;
      await page.click('.pub-history-restart');
      await until(page, () => Array.from(document.querySelector('.pub-source-text')?.textContent || '').length===16000 && document.querySelector('.pub-text-more') && !document.querySelector('.pub-text-more').disabled, 'reloaded source');
      slow.release(); await page.waitForTimeout(400);
      assert.equal(await page.evaluate(() => Array.from(document.querySelector('.pub-source-text').textContent).length), 16000, 'the old text page does not touch the reloaded source');
      assert.equal(await page.evaluate(() => !!document.querySelector('[data-pub-error]')), false);
      await page.click('.pub-text-more');
      await until(page, () => Array.from(document.querySelector('.pub-source-text')?.textContent || '').length===32000, 'paging after the reload');
      assert.equal(await page.evaluate(() => Array.from(document.querySelector('.pub-source-text').textContent).length), 32000);
      await context.close();
      console.log('ok restart, reload and double activation: replacements are issued, old results are ignored, text never doubles');
    }

    // ---- 11c. supported records and history evidence -----------------------------------------------------------------
    {
      const {page, context} = await fresh();
      await mountGlobal(page, '#/global/context/object/' + encodeURIComponent(refs.source_atlas));
      await until(page, () => document.querySelector('.pub-supports [data-pub-source], .pub-supports .pub-node'), 'supported records');
      const supportsText = await text(page, '.pub-supports');
      assert.doesNotMatch(supportsText, /\b0 relationships?\b/, 'a supported node never claims zero relationships');
      assert.ok(!/node:alice/.test(supportsText), 'the reference is not shown');
      await page.click('.pub-supports [data-pub-ref="node:alice"]');
      await until(page, () => location.hash === '#/global/context/object/node%3Aalice', 'supported node opens its object');
      await until(page, () => document.querySelector('.pub-history-details'), 'history');
      const newest = lists.main.find(e => e.operation === 'context_object' && e.request.ref === 'node:alice' && !e.request.history_cursor).response.data.history.items[0];
      assert.equal(newest.before.summary, newest.after.summary);
      await page.evaluate(() => document.querySelector('.pub-history-details').open = true);
      const details = await text(page, '.pub-history-details');
      assert.match(details, /The text is unchanged/);
      assert.match(details, /Before:[\s\S]*score\s*0(?!,)/); assert.match(details, /After:[\s\S]*score\s*0, 1/);
      assert(!details.includes(newest.commit) && !details.includes(newest.citations[0]) && !/Support after|Citations|Event /.test(details), 'no commit, citation or event identity is shown');
      // the oldest event has no earlier record
      await page.click('.pub-history-more').catch(() => {});
      // an endpoint change with an unchanged summary
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent('edge:work1'));
      await until(page, () => document.querySelector('.pub-history-details'), 'edge history');
      await page.evaluate(() => document.querySelectorAll('.pub-history-details').forEach(d => { d.open = true; }));
      const edge = await text(page, '.pub-history-section');
      assert.match(edge, /Before:/); assert.match(edge, /After:/);
      assert.ok(!/acme1|bobby|from_id|to_id/.test(edge), 'endpoint identities are not shown');
      assert.match(edge, /Source changed|Destination changed/);
      const ends = await page.$$eval('.pub-history-details [data-pub-stored] .pub-end [data-pub-ref]', nodes => nodes.map(n => [n.dataset.pubRef, n.textContent]));
      assert.ok(ends.length >= 2 && ends.every(([ref, label]) => /^node:[a-z0-9]{5}$/.test(ref) && label === 'Item'), 'endpoints are neutral Item links to node: references');
      assert.deepEqual(mismatches, []);
      await context.close();
      console.log('ok source-supported nodes never claim zero relationships; history shows before/after content without commits or citations');
    }

    // ---- 12. business object route, keyboard, screenshots ------------------------------------------------------------------
    {
      const {page, context} = await fresh();
      await mountBusiness(page, 'alpha', '_home');
      await until(page, () => document.querySelectorAll('.pub-node').length === 30, 'home');
      await shot(page, 'business-home-desktop');
      // keyboard: tab to the first node title, Enter opens its exact object
      await page.focus('.pub-node .pub-open');
      await page.keyboard.press('Enter');
      await until(page, () => /^_obj\/node:/.test(state.ctx) && document.querySelector('.pub-object'), 'business object');
      assert.deepEqual(calls.filter(c => c.path.startsWith('/api/biz/alpha/operations/context_object')).map(c => c.body.ref).length, 1);
      assert.equal(await page.evaluate(() => state.biz), 'alpha');
      await context.close();
      console.log('ok business Home opens canonical objects from the keyboard');
    }
    for (const [label, viewport] of [['desktop', {width: 1440, height: 960}], ['narrow', {width: 390, height: 844}]]) {
      const {page, context} = await fresh(viewport);
      await mountGlobal(page);
      await until(page, () => document.querySelectorAll('.ctxgraph-node').length > 3, 'graph');
      await page.waitForTimeout(600);
      await shot(page, `global-graph-${label}`);
      await mountGlobal(page, '#/global/context/object/' + encodeURIComponent('node:alice'));
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent('node:alice'));
      await until(page, () => document.querySelector('.pub-support .pub-evidence'), 'alice evidence');
      await shot(page, `global-object-evidence-${label}`);
      await routeGlobal(page, '#/global/context/object/' + encodeURIComponent(refs.source_survey));
      await until(page, () => document.querySelector('.pub-source-text'), 'survey');
      await shot(page, `global-source-${label}`);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      assert(overflow <= 1, `${label}: no horizontal page overflow (${overflow})`);
      const clipped = await page.evaluate(() => { const box = document.querySelector('.pub-object').getBoundingClientRect();
        return [...document.querySelectorAll('.pub-object h2, .pub-object .th .pa, .pub-object .pa *, .pub-object .meta, .pub-object .crumbs, .pub-object .crumbs *')]
          .filter(el => { const r = el.getBoundingClientRect(); return r.width && (r.right > box.right + 1 || r.left < box.left - 1); }).map(el => el.className || el.tagName); });
      assert.deepEqual(clipped, [], `${label}: heading and metadata stay inside the reader`);
      await context.close();
    }
    {
      // keyboard navigation of the graph canvas and reader
      const {page, context} = await fresh();
      await mountGlobal(page);
      await until(page, () => document.querySelectorAll('.ctxgraph-node').length > 3, 'graph');
      await page.focus('.ctxgraph-node.node');
      await page.keyboard.press('Enter');
      await until(page, () => !document.querySelector('.ctxgraph-reader').hidden, 'reader via keyboard');
      await page.keyboard.press('Tab');
      assert.equal(await page.evaluate(() => document.activeElement?.tagName), 'BUTTON');
      await context.close();
    }

    // ---- 12. refresh keeps the published content that is shown ---------------------------------------------------------------
    {
      // Home: a held refresh keeps the loaded pages and the very same nodes; an unchanged answer rebuilds nothing.
      const {page, context} = await fresh();
      await mountBusiness(page, 'alpha', '_home');
      await until(page, () => document.querySelectorAll('.pub-node').length === 30, 'home');
      await page.click('.pub-more');
      await until(page, () => document.querySelectorAll('.pub-node').length === 60, 'second page');
      await page.evaluate(() => { window.__node = document.querySelector('[data-pub-node]'); window.__last = [...document.querySelectorAll('[data-pub-node]')].at(-1); });
      const same = () => page.evaluate(() => ({first: document.querySelector('[data-pub-node]') === window.__node, last: [...document.querySelectorAll('[data-pub-node]')].at(-1) === window.__last,
        count: document.querySelectorAll('[data-pub-node]').length, loading: /Loading…/.test(document.querySelector('#host').textContent)}));
      const start = calls.length, held = gate(); control.hold = {context_home: held};
      await page.click('.pub-refresh'); await held.wait;
      assert.deepEqual(await same(), {first: true, last: true, count: 60, loading: false}, 'the loaded pages stay while the refresh is held');
      assert.equal(await page.evaluate(() => document.querySelector('.pub-refresh').disabled), true);
      held.release();
      for (let i = 0; i < 100 && (opsSince(start, 'context_home').length < 2 || await page.evaluate(() => document.querySelector('.pub-refresh').disabled)); i++) await page.waitForTimeout(25);
      const reads = opsSince(start, 'context_home').map(c => c.body.cursor || null);
      assert.equal(reads[0], null, 'the refresh starts from the first page'); assert.equal(reads.length, 2, 'and rereads exactly the loaded depth');
      assert.deepEqual(await same(), {first: true, last: true, count: 60, loading: false}, 'an unchanged answer keeps the nodes');
      // a transient failure keeps the pages with a reload control; the retry replaces them
      control.fail = {context_home: {when: () => true, message: 'The reader is busy'}};
      await page.click('.pub-refresh');
      await until(page, () => document.querySelector('[data-pub-error="transient"]'), 'refresh error');
      assert.deepEqual(await same(), {first: true, last: true, count: 60, loading: false});
      assert.equal(await page.evaluate(() => !!document.querySelector('.pub-error .pub-restart')), true, 'the error offers a staged reload, not a continuation');
      await page.click('.pub-error .pub-restart');
      await until(page, () => !document.querySelector('.pub-error') && document.querySelectorAll('[data-pub-node]').length === 60, 'retry succeeded');
      // a page of another publication in the middle of the refresh is refused and the shown pages stay
      control.tamper = (op, body, response) => { if (op === 'context_home' && body.cursor && response.status === 'ok') response.data.revision = '0'.repeat(40); return response; };
      await page.click('.pub-refresh');
      await until(page, () => document.querySelector('[data-pub-error="integrity"]'), 'mixed publication refused');
      assert.deepEqual(await same(), {first: true, last: true, count: 60, loading: false}, 'no mixed publication is shown');
      control.tamper = null;
      // changed content replaces the pages; lost access clears them
      control.tamper = (op, body, response) => { if (op === 'context_home' && !body.cursor && response.status === 'ok') response.data.nodes[0].summary += ' (edited)'; return response; };
      await page.click('.pub-error .pub-restart');
      await until(page, () => /\(edited\)/.test(document.querySelector('#host').textContent) && !document.querySelector('.pub-error'), 'changed content applied');
      control.tamper = (op, body, response) => op === 'context_home' ? {status: 'error', error: {code: 'forbidden', message: 'No longer permitted'}} : response;
      await page.click('.pub-refresh');
      await until(page, () => !document.querySelector('[data-pub-node]') && document.querySelector('.pub-error'), 'lost access clears the pages');
      assert.deepEqual(mismatches, []);
      await context.close();
      console.log('ok Home refresh keeps pages and nodes, rereads the loaded depth, refuses mixed publications, applies changes and clears on lost access');
    }
    {
      // Graph: the same graph instance (camera, layout, selection) survives a pending, failed or unchanged refresh.
      const {page, context} = await fresh();
      await mountGlobal(page);
      await until(page, () => document.querySelectorAll('.ctxgraph-node').length > 3, 'graph');
      await page.evaluate(() => { window.__graph = _ctxGraph; window.__graphEl = document.querySelector('.ctxgraph'); });
      const alive = () => page.evaluate(() => ({graph: _ctxGraph === window.__graph, el: document.querySelector('.ctxgraph') === window.__graphEl, nodes: document.querySelectorAll('.ctxgraph-node').length > 3}));
      const held = gate(); control.hold = {context_home: held};
      await page.click('.pub-refresh'); await held.wait;
      assert.deepEqual(await alive(), {graph: true, el: true, nodes: true}, 'the graph stays while the refresh is held');
      held.release(); await page.waitForTimeout(400);
      assert.deepEqual(await alive(), {graph: true, el: true, nodes: true}, 'an unchanged answer keeps the graph instance');
      control.fail = {context_home: {when: () => true}};
      await page.click('.pub-refresh');
      await until(page, () => document.querySelector('[data-pub-error="transient"]'), 'graph refresh error');
      assert.deepEqual(await alive(), {graph: true, el: true, nodes: true}, 'a failed refresh keeps the graph');
      control.tamper = (op, body, response) => { if (op === 'context_home' && !body.cursor && response.status === 'ok') response.data.nodes[0].summary += ' (edited)'; return response; };
      await page.click('.pub-more');
      await until(page, () => window.__graph !== _ctxGraph && !document.querySelector('.pub-error'), 'changed graph rebuilt');
      control.tamper = null;
      await context.close();
      console.log('ok Graph refresh keeps the instance through pending, failed and unchanged reads and rebuilds only for changed data');
    }
    {
      // Object: a same-record reload keeps the displayed record and history until the whole replacement is validated.
      const {page, context} = await fresh();
      control.opScenario = {context_expand: 'later'};
      await mountGlobal(page, '#/global/context/object/' + encodeURIComponent('node:atlas'));
      await until(page, () => document.querySelector('[data-pub-error="publication-changed"]') && document.querySelectorAll('.pub-history').length === 10, 'changed publication');
      await page.evaluate(() => { window.__title = document.querySelector('.pub-object h2'); window.__entry = document.querySelector('.pub-history'); });
      const view = () => page.evaluate(() => ({title: document.querySelector('.pub-object h2') === window.__title, entry: document.querySelector('.pub-history') === window.__entry,
        entries: document.querySelectorAll('.pub-history').length, loading: !!document.querySelector('.pub-loading'), changed: !!document.querySelector('[data-pub-error="publication-changed"]')}));
      control.opScenario = {context_expand: 'later', context_object: 'later'};
      const held = gate(); control.hold = {context_object: held};
      await page.click('.pub-reload'); await held.wait;
      assert.deepEqual(await view(), {title: true, entry: true, entries: 10, loading: false, changed: true}, 'the record and its history stay while the reload is held');
      held.release();
      await until(page, () => !document.querySelector('[data-pub-error="publication-changed"]') && document.querySelector('.pub-neighbourhood .pub-edge'), 'reload applied');
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-history').length), 10);
      await context.close();
    }
    {
      // Lost access during a reload removes the record that was shown.
      const {page, context} = await fresh();
      control.opScenario = {context_expand: 'later'};
      await mountGlobal(page, '#/global/context/object/' + encodeURIComponent('node:atlas'));
      await until(page, () => document.querySelector('[data-pub-error="publication-changed"]') && document.querySelectorAll('.pub-history').length === 10, 'changed publication');
      control.tamper = (op, body, response) => op === 'context_object' ? {status: 'error', error: {code: 'forbidden', message: 'No longer permitted'}} : response;
      await page.click('.pub-reload');
      await until(page, () => document.querySelector('.pub-route-error') && !document.querySelector('.pub-object'), 'record cleared');
      control.tamper = null;
      await context.close();
      console.log('ok Object reload keeps the record and history until the replacement is complete');
    }
    {
      // A refresh whose second page is held stops asking for pages once the view is gone or the refresh is superseded.
      const {page, context} = await fresh();
      await mountBusiness(page, 'alpha', '_home');
      await until(page, () => document.querySelectorAll('.pub-node').length === 30, 'home');
      await page.click('.pub-more');
      await until(page, () => document.querySelectorAll('.pub-node').length === 60, 'second page');
      const start = calls.length, held = gate(); held.when = body => !!body.cursor; control.hold = {context_home: held};
      await page.click('.pub-refresh'); await held.wait;
      await page.evaluate(() => { pubMountGeneration++; });   // navigation: the mount is no longer current
      held.release();
      await page.waitForTimeout(500);
      assert.equal(opsSince(start, 'context_home').length, 2, 'the walk issued the first and the held second read and nothing more');
      assert.equal(await page.evaluate(() => document.querySelectorAll('[data-pub-node]').length), 60, 'nothing was committed or painted');
      await context.close();
    }
    {
      // A continuation that started before a reload cannot change the displayed collection while the reload is pending or after it fails.
      const {page, context} = await fresh();
      control.opScenario = {context_expand: 'later'};
      await mountGlobal(page, '#/global/context/object/' + encodeURIComponent('node:atlas'));
      await until(page, () => document.querySelector('[data-pub-error="publication-changed"]') && document.querySelectorAll('.pub-history').length === 10 && document.querySelector('.pub-history-more'), 'history with a continuation');
      const start = calls.length;
      const oldMore = gate(); oldMore.when = body => !!body.history_cursor; control.hold = {context_object: oldMore};
      await page.click('.pub-history-more'); await oldMore.wait;
      const reloadHeld = gate(); reloadHeld.when = body => !body.history_cursor; control.hold = {context_object: reloadHeld};
      await page.click('.pub-reload'); await reloadHeld.wait;
      assert.equal(await page.evaluate(() => document.querySelector('.pub-history-more').disabled), true, 'continuations are disabled while the reload is pending');
      oldMore.release();
      await page.waitForTimeout(400);
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-history').length), 10, 'the old continuation did not change the displayed history');
      control.tamper = (op, body, response) => op === 'context_object' ? {status: 'error', error: {code: 'temporarily_unavailable', message: 'The reader is busy'}} : response;
      reloadHeld.release();
      await until(page, () => document.querySelector('[data-pub-error="transient"]'), 'reload failed');
      control.tamper = null;
      assert.equal(await page.evaluate(() => document.querySelectorAll('.pub-history').length), 10, 'a failed reload keeps the original snapshot');
      assert.equal(opsSince(start, 'context_object').filter(c => c.body.history_cursor).length, 1, 'no new continuation started while the reload was pending');
      assert.equal(await page.evaluate(() => document.querySelector('.pub-history-more')?.disabled), false, 'the old request did not leave the control locked or unlocked wrongly');
      await page.click('.pub-history-more');
      await until(page, () => document.querySelectorAll('.pub-history').length > 10, 'a new continuation after the failed reload');
      await context.close();
      console.log('ok a refresh walk stops when its view is gone; a continuation older than a reload cannot change the snapshot or run during the reload');
    }
    assert.deepEqual(pageErrors, []);
    console.log('PASS published graph readers');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
