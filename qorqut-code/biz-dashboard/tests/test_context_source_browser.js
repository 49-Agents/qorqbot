#!/usr/bin/env node
// Context sources (H2-ui, simplified presentation): plain filing labels, UUID source routes, bounded body paging, exact
// retry/retract commands and the Context-only queue summary. Technical facts, diagnostics and input controls are not shown. A disposable loopback page serves the committed dashboard
// files and answers ONLY from recorded host exchanges (tests/fixtures/context-source/exchanges.json): a request that was not
// recorded in the selected stage fails visibly. Stages are named host states; a check moves between stages only where the host
// itself moved (a recorded command's next state, or a state the page was not yet aware of).
//
// FIXTURE PROVENANCE: exchanges.json was generated through the fixed H2 host handlers (host 4f96d823, Operations 2d697fa3,
// Context 32431e12) over the host's isolated synthetic test composition with an injected model runner, for the exact requests
// this page sends. It is not an installed product, a running server or live data. See docs/CONTEXT-SOURCE-UI.md.
// Injected cases (a dropped connection, a malformed copy of a recorded response, a malformed copy of a queue summary) are
// marked as injected in the test and are not host evidence.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {Replay, handle, fixtureFile} = require('./support/exchange_replay');
const root = path.resolve(__dirname, '..');
const replay = new Replay(fixtureFile('exchanges.json'));
assert.equal(replay.document.provenance.status, 'generated-through-fixed-host-handlers');
assert.equal(replay.document.provenance.host_commit, '4f96d823c8d0b1ea8b5f3935d114b79ad1e8356b');
const evidence = process.env.EVIDENCE_DIR;
const BIZ = 'first';

// document ids and recorded facts come from the recorded exchanges, never from hand-written ids
const docIn = (stage, title) => {
  for (const exchange of replay.all(stage, (request, response) => /context_get$/.test(request.path) && request.body?.max_chars === 100 && response.body?.data?.document?.title === title))
    return exchange.response.body.data.document;
  throw new Error(`No ${title} in stage ${stage}`);
};
const recorded = (stage, pathEnd, match = () => true) => replay.first(stage, (request) => request.path.endsWith(pathEnd) && match(request.body || {})).response.body;
const pagesOf = (stage, docId) => replay.all(stage, (request, response) => /context_get$/.test(request.path) && request.body?.document_id === docId && request.body?.max_chars === 6000 && response.status === 200)
  .map(exchange => exchange.response.body.data);
const ID = {
  pilot: docIn('pilot-blocked', 'Pilot call').id, privateDoc: docIn('private-blocked', 'Private').id, conflict: docIn('conflict', 'Conflict').id,
  withdrawn: docIn('withdrawn', 'Withdrawn').id, many: replay.first('many-revisions', request => /context_upload_map$/.test(request.path)).request.body.upload_id,
  long: docIn('long-filed', 'Long source').id, edited: docIn('long-edited', 'Long source, edited').id, blocked: docIn('blocked-input', 'Blocked at the end').id,
  queued: docIn('final', 'Queued at the end').id, history: docIn('final', 'Filed history 00').id,
  global0: replay.first('global-queued', request => /context_get$/.test(request.path) && request.body.scope === 'shared').request.body.document_id,
};
ID.global1 = replay.all('global-queued', request => /context_get$/.test(request.path) && request.body.scope === 'shared' && request.body.document_id !== ID.global0)[0].request.body.document_id;

const calls = [], control = {};
const reset = () => { for (const key of Object.keys(control)) delete control[key]; };
const unrecorded = () => calls.filter(call => call.unrecorded);
const styles = fs.readFileSync(path.join(root, 'index.html'), 'utf8').match(/<link rel="stylesheet"[^>]+>/g).join('\n');
const scripts = ['core', 'san-juan-time', 'ctxmap', 'ctxgraph', 'global-context-graph', 'global-context-graph-renderer', 'context-published', 'store', 'events', 'context-source', 'context'];
// js/global.js needs most of the application at load; only the real Global library function is lifted from it.
const globalSource = fs.readFileSync(path.join(root, 'js/global.js'), 'utf8');
const libraryScript = globalSource.slice(globalSource.indexOf('const GLOBAL_LIBRARY_KINDS'), globalSource.indexOf('let globalRender = 0;'));
assert.ok(libraryScript.includes('function renderGlobalLibrary'));
const html = `<!doctype html><html><head><meta charset="utf-8">${styles}</head><body>
  <script>function workRelatedChips() {}</script><div id="toasts"></div><button id="wsbtn" hidden></button><div id="content"><div id="host" style="height:100vh"></div></div>
  ${scripts.map(name => `<script src="/js/${name}.js"></script>`).join('')}<script src="/__global_library.js"></script></body></html>`;

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const json = (data, status = 200) => { res.writeHead(status, {'Content-Type': 'application/json'}); res.end(JSON.stringify(data)); };
  if (url.pathname === '/__global_library.js') { res.setHeader('Content-Type', 'text/javascript'); return res.end(libraryScript); }
  if (url.pathname === '/') { res.setHeader('Content-Type', 'text/html'); return res.end(html); }
  // NOT part of the recorded Context surface: navigation counts and the authored-file read are other features; they are answered by
  // fixed stubs so the unchanged Context shell can render.
  if (url.pathname.endsWith('/operations/context_navigation_get')) { calls.push({path: url.pathname, op: 'navigation', body: {}}); return json({status: 'ok', data: {navigation_only: true, counts: {story: 0, table: 2, product: 1, person: null}, context: {status: 'ready', reason: null, revision: 1}}}); }
  if (url.pathname === `/api/biz/${BIZ}/context/file`) { calls.push({path: url.pathname + url.search, op: 'file', body: {}});
    return json({text: '---\ntitle: Authored\n---\n\noriginal text', revision: 1, content_hash: 'h1', document_id: '0000aaaa-0000-4000-8000-00000000aaaa', path: url.searchParams.get('p')}); }
  // the Context shell's own tree read (authored files) is another feature; a fixed stub, never counted as a Context-source answer
  if (url.pathname === `/api/biz/${BIZ}/context` && !url.searchParams.has('view')) { calls.push({path: url.pathname, op: 'shell_tree', body: {}}); return json({status: 'ok', data: {}}); }
  if (/\/routines$/.test(url.pathname)) { calls.push({path: url.pathname + url.search, op: 'routines_stub', body: {}}); return json({status: 'ok', data: {}}); }   // routines are another feature
  if (await handle(req, res, url, {replay, control, calls})) return;
  if (url.pathname.startsWith('/api/')) { calls.push({path: url.pathname + url.search, op: 'other', body: {}, unrecorded: true}); return json({status: 'error', error: {code: 'unrecorded_request', message: 'Not a recorded Context request'}}, 400); }
  const file = path.join(root, url.pathname);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'application/octet-stream');
  res.end(fs.readFileSync(file));
});
const gate = when => { let release, arrived; const promise = new Promise(r => { release = r; }); const seen = new Promise(r => { arrived = r; }); return {promise, release, arrived, seen, when}; };
const until = async (page, fn, what, arg) => { for (let i = 0; i < 160; i++) { if (await page.evaluate(fn, arg).catch(() => false)) return; await page.waitForTimeout(50); } throw new Error('Timed out waiting for ' + what); };
const text = (page, selector = '#host') => page.evaluate(sel => document.querySelector(sel)?.textContent || '', selector);
const shot = async (page, name) => { if (evidence) await page.screenshot({path: path.join(evidence, name + '.png')}); };
const ops = (op, start = 0) => calls.slice(start).filter(call => call.op === op);
const message = (page, pattern) => page.waitForFunction(p => new RegExp(p).test(document.querySelector('[data-src-message]')?.textContent || ''), pattern.source).catch(async error => { throw new Error(`${error.message}\nmessage was: ${await text(page, '[data-src-message]')}\ncalls: ${JSON.stringify(calls.slice(-6).map(c => [c.op, c.stage, c.unrecorded, c.injected, JSON.stringify(c.body).slice(0, 120)]))}`); });
const idle = async page => { await page.waitForFunction(() => srcBusy.size === 0); await page.waitForTimeout(300); };

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({headless: true, ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {})});
  const pageErrors = [];
  const fresh = async (stage, viewport = {width: 1440, height: 960}) => {
    reset(); calls.length = 0; replay.use(stage);
    const context = await browser.newContext({viewport});
    await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    page.on('pageerror', error => pageErrors.push(error.message));
    await page.goto(origin);
    return {page, context};
  };
  const mountBusiness = (page, ctx) => page.evaluate(([p, slug]) => {
    state.scope = 'business'; state.biz = slug; state.tab = 'context'; state.ctx = p; state.bizs = [{id: slug, slug, name: slug}];
    renderContext(document.querySelector('#host'));
  }, [ctx, BIZ]);
  const go = (page, p) => page.evaluate(path => ctxGo(path), p);
  const mountGlobal = (page, hash) => page.evaluate(h => {
    state.scope = 'global'; state.globalTab = 'context'; state.biz = null;
    const paint = () => renderGlobalLibrary(document.querySelector('#host'));
    if (!window.__wired) { window.__wired = true; addEventListener('hashchange', paint); }
    if (location.hash === h) paint(); else location.hash = h;
  }, hash);
  const sourceText = (page, id) => page.waitForFunction(i => document.querySelector(`[data-src-view="${i}"] [data-src-body]`)?.textContent.length > 0, id);
  // a check that ends with an unrecorded request has asked the host something it never answered
  const closeClean = async (context, allow = []) => { const stray = unrecorded().filter(call => !allow.includes(call.op)); assert.deepEqual(stray.map(call => call.path + ' ' + JSON.stringify(call.body)), [], 'unrecorded requests'); await context.close(); };

  try {
    // ---- 1. filing labels (no host) ---------------------------------------------------------------------------------
    {
      const {page, context} = await fresh('final');
      const labels = await page.evaluate(() => {
        const f = (state, extra = {}) => srcFilingLabel({state, ...extra}).label;
        return [f('unfiled'), f('queued'), f('processing'), f('blocked'), f('discard_requested'), f('discarded'),
          f('published', {completion_state: 'pending'}), f('published', {completion_state: 'conflict'}), f('published', {completion_state: 'applied', operation: 'edit'}),
          f('published', {completion_state: 'applied', operation: 'remove'}), f('published', {completion_state: null}), f('published', {completion_state: 'weird'}), srcFilingLabel(null).label, srcFilingLabel({state: 'bogus'}).label];
      });
      assert.deepEqual(labels, ['Not added', 'Waiting', 'Processing', "Couldn't process", 'Cancelling', 'Cancelled', 'Finishing', 'Needs attention',
        'Added to Context', 'Removed', 'Finishing', 'Unavailable', 'Unavailable', 'Unavailable']);
      assert.deepEqual(await page.evaluate(() => [srcFilingLabel({}).label, srcFilingLabel({state: null}).label, srcFilingLabel({state: ''}).label]), ['Unavailable', 'Unavailable', 'Unavailable'], 'a missing state is never Not added');
      const line = await page.evaluate(() => srcFilingLine({state: 'published', completion_state: 'applied', source_revision: 2, published_commit: 'abc123', error: 'boom'}, {changed: true}));
      assert.match(line, /Added to Context · Changes not submitted/); assert.ok(!/revision|abc123|boom|commit/i.test(line), 'no technical detail in the line');
      // every recorded filing of every stage maps to a non-fallback label
      const filings = replay.document.exchanges.flatMap(e => { const d = e.response.body?.data; return [d?.document?.filing, ...(d?.items || []).map(i => i.filing)].filter(Boolean); });
      const seen = await page.evaluate(all => all.map(f => srcFilingLabel(f).label), filings);
      assert.ok(filings.length > 50 && seen.every(label => label !== 'Unavailable'), 'every recorded filing has a label');
      for (const label of ['Waiting', 'Processing', "Couldn't process", 'Cancelling', 'Cancelled', 'Finishing', 'Needs attention', 'Added to Context', 'Removed', 'Not added']) assert.ok(seen.includes(label), label + ' recorded: ' + [...new Set(seen)].join(', '));
      await closeClean(context);
      console.log('ok every filing state, completion state and operation maps to its exact label (including every recorded host filing); a changed note says only "Changes not submitted"');
    }

    // ---- 2. business Uploads: two kinds, independent cursors ---------------------------------------------------------
    {
      const {page, context} = await fresh('final');
      await mountBusiness(page, '_uploads');
      await page.waitForSelector('[data-src-list="context_upload"] [data-src-open]');
      await page.waitForFunction(() => document.querySelector('[data-src-list="context_dump"] [data-src-status]').textContent === 'None.');
      const firstPage = recorded('final', '/context_list', body => body.kind === 'context_upload' && !body.cursor).data;
      const lists = ops('context_list');
      assert.deepEqual(lists.map(c => c.body.kind).sort(), ['context_dump', 'context_upload']);
      assert.ok(lists.every(c => c.path === `/api/biz/${BIZ}/operations/context_list` && c.body.limit === 30 && c.body.scope === 'business' && !('cursor' in c.body)));
      assert.ok(firstPage.total > 30 && firstPage.next_cursor, 'the recorded Uploads list pages');
      assert.equal(await page.locator('[data-src-list="context_upload"] [data-src-open]').count(), 30);
      assert.equal(await page.locator('[data-src-total]').count(), 0, 'no counters');
      await page.locator('[data-src-list="context_upload"] [data-src-more]').click();
      await page.waitForFunction(total => document.querySelectorAll('[data-src-list="context_upload"] [data-src-open]').length === total, firstPage.total);
      const second = ops('context_list').filter(c => c.body.cursor);
      assert.equal(second.length, 1); assert.equal(second[0].body.kind, 'context_upload'); assert.equal(second[0].body.cursor, firstPage.next_cursor);
      assert.equal(await page.locator('[data-src-list="context_upload"] [data-src-more]').isHidden(), true);
      const states = await page.$$eval('[data-src-list="context_upload"] [data-src-state]', els => els.map(e => e.dataset.srcState));
      for (const key of ['queued', 'blocked', 'discarded', 'completion_conflict', 'filed', 'withdrawn', 'unfiled']) assert.ok(states.includes(key), key + ' in ' + [...new Set(states)].join(','));
      assert.equal(await page.locator('[data-src-recent]').count(), 0);
      assert.equal(ops('context_filings').length, 0, 'no recent-filings feed');
      const cards = await text(page, '[data-src-list="context_upload"]');
      assert.ok(!/[0-9a-f]{8}-[0-9a-f]{4}-|revision/i.test(cards), 'cards show no identity or revision');
      for (const old of ['/context/map', '/context/summarize', 'context_base', '_dumps', '_inbox', '/context/retry']) assert.ok(!calls.some(c => c.path.includes(old)), old);
      await page.locator('[data-src-list="context_upload"] [data-src-open]').first().click();
      await until(page, () => state.ctx.startsWith('_source/'), 'source route');
      assert.match(await page.evaluate(() => state.ctx), /^_source\/[0-9a-f-]{36}$/);
      await closeClean(context, ['context_get']);
      console.log('ok Uploads pages context_upload and context_dump independently with no counters, ids or recent-filing feed; no map/summarize/base/legacy-path requests');
    }

    // ---- 3. source reader: filing, body paging, inert text, history ---------------------------------------------------
    {
      const {page, context} = await fresh('long-filed');
      await mountBusiness(page, `_source/${ID.long}`);
      await sourceText(page, ID.long);
      assert.equal(await page.evaluate(() => window.__xss), undefined);
      assert.equal(await page.locator('[data-src-body] img').count(), 0);
      assert.match(await text(page, '[data-src-body]'), /<img src=x onerror=/);
      assert.equal(await page.locator('[data-src-more]').count(), 1, 'more is available');
      assert.equal(await page.locator('[data-src-count]').count(), 0, 'no character counter');
      const first = ops('context_get')[0];
      assert.equal(first.path, `/api/biz/${BIZ}/operations/context_get`); assert.deepEqual(first.body, {scope: 'business', document_id: ID.long, max_chars: 6000});
      for (let i = 0; i < 6 && await page.locator('[data-src-more]').count(); i++) {
        const shown = (await text(page, '[data-src-body]')).length;
        await page.locator('[data-src-more]').click();
        await page.waitForFunction(n => document.querySelector('[data-src-body]').textContent.length > n, shown);
      }
      await page.waitForFunction(() => !document.querySelector('[data-src-more]'));
      const expected = pagesOf('long-filed', ID.long).map(data => data.document.body).join('');
      assert.ok([...expected].length > 6000 && pagesOf('long-filed', ID.long).length >= 2, 'the recorded body needs several pages');
      assert.equal(await text(page, '[data-src-body]'), expected, 'the shown body is exactly the recorded pages in order');
      const pageRequests = ops('context_get').filter(c => c.body.cursor);
      assert.ok(pageRequests.length >= 1 && pageRequests.every(c => c.body.document_id === ID.long && c.body.max_chars === 6000));
      assert.equal(await page.locator('[data-src-filing] [data-src-state="filed"]').count(), 1);
      assert.equal(await page.locator('[data-src-version], [data-src-section="diagnostics"], dl').count(), 0, 'no diagnostics or fact grid');
      assert.ok(!ops('context_upload_map').length, 'no display-only diagnostic fetch');
      assert.ok(!/[0-9a-f]{8}-[0-9a-f]{4}-|revision|commit/i.test(await text(page, '[data-src-view]')), 'no identity, revision or commit');
      assert.equal((await text(page, '.crumbs')).includes(ID.long), false);
      await closeClean(context);
      // a source that is being processed shows its state and offers no input control
      const busy = await fresh('pilot-processing');
      await mountBusiness(busy.page, `_source/${ID.pilot}`);
      await sourceText(busy.page, ID.pilot);
      assert.equal(await busy.page.locator('[data-src-filing] [data-src-state="processing"]').count(), 1);
      assert.equal(await busy.page.locator('[data-src-retry], [data-src-retract]').count(), 0, 'no control for a processing source');
      await closeClean(busy.context);
      console.log('ok source body pages inert, bounded and in order; one plain status renders beside it with no history');
    }

    // ---- 4. refused pages, completion facts, earlier revisions --------------------------------------------------------
    {
      const {page, context} = await fresh('long-filed');
      // INJECTED: a copy of a recorded page-2 answer that replays the first page
      control.tamper = (op, body, response) => { if (op !== 'context_get' || !body.cursor) return null; response.data.document.character_offset = 0; return response; };
      await mountBusiness(page, `_source/${ID.long}`);
      await sourceText(page, ID.long);
      await page.locator('[data-src-more]').click();
      await page.waitForSelector('[data-src-body-error]');
      assert.equal(await text(page, '[data-src-body-error]'), "Couldn't load. Retry");
      assert.equal([...await text(page, '[data-src-body]')].length, 6000);
      control.tamper = null;
      await closeClean(context);

      // completion happens between pages: the cursor recorded while queued is the cursor recorded after filing (it binds the body, never the file hash)
      const queued = await fresh('long-queued');
      await mountBusiness(queued.page, `_source/${ID.long}`);
      await sourceText(queued.page, ID.long);
      assert.match(await text(queued.page, '[data-src-filing]'), /Waiting/);
      const queuedHash = recorded('long-queued', '/context_get', body => body.max_chars === 6000 && !body.cursor).data.document.content_hash;
      replay.use('long-filed');                                              // the host filed the source while the reader was open
      const filedHash = recorded('long-filed', '/context_get', body => body.max_chars === 6000 && !body.cursor).data.document.content_hash;
      await queued.page.locator('[data-src-more]').click();
      await queued.page.waitForFunction(() => !document.querySelector('[data-src-more]'));
      assert.equal(await queued.page.locator('[data-src-body-error]').count(), 0);
      assert.ok(queuedHash && filedHash, `file hashes recorded (${queuedHash === filedHash ? 'unchanged' : 'changed'} by completion); the page never compares them`);
      await closeClean(queued.context);

      // a real body change: the host refuses the cursor; nothing is appended and reading restarts from the beginning
      const edited = await fresh('long-edited');
      await mountBusiness(edited.page, `_source/${ID.edited}`);
      await sourceText(edited.page, ID.edited);
      assert.equal([...await text(edited.page, '[data-src-body]')].length, 6000);
      await edited.page.locator('[data-src-more]').click();
      await edited.page.waitForSelector('[data-src-restart]');
      const refusal = recorded('long-edited', '/context_get', body => !!body.cursor && body.document_id === ID.edited);
      assert.equal(refusal.status, 'error');
      assert.equal(await text(edited.page, '[data-src-body-error]'), 'Changed since you opened it. Refresh');
      assert.ok(!(await text(edited.page)).includes(refusal.error.message), 'the raw host refusal is not shown');
      assert.equal([...await text(edited.page, '[data-src-body]')].length, 6000, 'the refused page appended nothing');
      // restart staging: the old body stays mounted (same node, selection, scroll) while the first page is held, and through a failure
      const bodyBefore = await text(edited.page, '[data-src-body]');
      await edited.page.evaluate(() => {
        window.__body = document.querySelector('[data-src-body]'); window.__body.scrollTop = 40;
        const range = document.createRange(); range.setStart(window.__body.firstChild, 10); range.setEnd(window.__body.firstChild, 30);
        getSelection().removeAllRanges(); getSelection().addRange(range);
      });
      const sameBody = () => edited.page.evaluate(() => document.querySelector('[data-src-body]') === window.__body && getSelection().toString().length === 20);
      const restartHold = gate(body => body.document_id === ID.edited && !body.cursor && body.max_chars === 6000);
      control.hold = {context_get: restartHold};
      await edited.page.locator('[data-src-restart]').click();
      await restartHold.seen;
      assert.equal(await sameBody(), true, 'the old body stays mounted while the restart is held');
      assert.equal(await edited.page.locator('[data-src-restart]').isDisabled(), true);
      control.tamper = (op, body) => (op === 'context_get' && body.max_chars === 6000 ? {status: 'error', error: {message: 'Temporarily unavailable'}} : null);
      restartHold.release();
      await edited.page.waitForSelector('[data-src-body-error]');
      assert.equal(await text(edited.page, '[data-src-body]'), bodyBefore, 'a failed restart keeps the prior body');
      assert.equal(await sameBody(), true, 'a failed restart keeps the body node and selection');
      assert.equal(await text(edited.page, '[data-src-body-error]'), "Couldn't load. Retry");
      control.tamper = null;
      await edited.page.locator('[data-src-restart]').click();
      await edited.page.waitForFunction(() => !document.querySelector('[data-src-body-error]') && [...(document.querySelector('[data-src-body]')?.textContent || '')].length === 6000);
      assert.equal(await edited.page.locator('[data-src-body-error]').count(), 0);
      // the restart read finding the document gone or denied clears the body
      control.tamper = (op, body) => (op === 'context_get' && body.cursor ? {status: 'error', error: {code: 'cursor_invalid', message: 'refused'}} : null);   // INJECTED refusal
      await edited.page.locator('[data-src-more]').click();
      await edited.page.waitForSelector('[data-src-restart]');
      control.tamper = (op, body) => (op === 'context_get' && body.max_chars === 6000 ? {status: 'error', error: {code: 'not_found', message: 'gone'}} : null);
      await edited.page.locator('[data-src-restart]').click();
      await edited.page.waitForFunction(() => !document.querySelector('[data-src-body]'));
      assert.equal(await edited.page.locator('[data-src-view] .src-title').textContent(), 'Unavailable');
      control.tamper = null;
      await closeClean(edited.context);

      // malformed pagination never becomes a complete body (INJECTED malformed copies of the recorded first page)
      for (const [name, tweak] of [
        ['first page at a nonzero offset', (body, r) => { if (!body.cursor) r.data.document.character_offset = 5; }],
        ['complete with a cursor', (body, r) => { r.data.complete = true; }],
        ['incomplete without a cursor', (body, r) => { r.data.next_cursor = null; r.data.complete = false; }],
        ['missing complete', (body, r) => { delete r.data.complete; }],
        ['non-string cursor', (body, r) => { r.data.next_cursor = 7; }],
        ['complete without next_cursor', (body, r) => { delete r.data.next_cursor; r.data.complete = true; }]]) {
        const bad = await fresh('long-filed');
        control.tamper = (op, body, response) => { if (op !== 'context_get' || body.max_chars !== 6000) return null; tweak(body, response); return response; };
        await mountBusiness(bad.page, `_source/${ID.long}`);
        await bad.page.waitForSelector('[data-src-view]');
        await bad.page.waitForTimeout(400);
        assert.ok(/Unavailable/.test(await text(bad.page)) && !/refused|malformed/.test(await text(bad.page)), name + ': ' + (await text(bad.page)).slice(0, 300));
        assert.equal(await bad.page.locator('[data-src-body]').count(), 0, name + ': no body is shown or labelled complete');
        await closeClean(bad.context);
      }

      // earlier revision keeps the submitted snapshot visible; a completion conflict keeps the published commit
      const second = await fresh('pilot-changed-after-publication');
      await mountBusiness(second.page, `_source/${ID.pilot}`);
      await sourceText(second.page, ID.pilot);
      const unfiled = recorded('pilot-changed-after-publication', '/context_get', body => body.max_chars === 100).data.document.filing;
      assert.equal(unfiled.state, 'unfiled');
      assert.match(await text(second.page, '[data-src-filing]'), /Not added/);
      assert.equal(unfiled.source_current ?? null, null, 'the host marks an unfiled source by state, not by source_current');
      assert.equal(await second.page.locator('[data-src-earlier]').count(), 0);
      replay.use('conflict');
      await go(second.page, `_source/${ID.conflict}`);
      await second.page.waitForFunction(() => document.querySelector('[data-src-state="completion_conflict"]'));
      const conflictFiling = recorded('conflict', '/context_get', body => body.max_chars === 100 && body.document_id === ID.conflict).data.document.filing;
      assert.ok(conflictFiling.published_commit, 'the recorded conflict has a published commit');
      assert.match(await text(second.page, '[data-src-filing]'), /Needs attention/);
      assert.ok(!(await text(second.page, '[data-src-view]')).includes(conflictFiling.published_commit), 'the commit is not shown');
      await closeClean(second.context);
      console.log('ok duplicate pages are refused, completion between pages does not stop paging, a host cursor refusal restarts instead of appending; an unfiled change and a conflict show one plain label');
    }

    // ---- 5. no diagnostics: a source with many recorded revisions shows only its plain status --------------------------------------
    {
      const {page, context} = await fresh('many-revisions');
      await mountBusiness(page, `_source/${ID.many}`);
      await sourceText(page, ID.many);
      await page.waitForTimeout(300);
      assert.equal(ops('context_upload_map').length, 0, 'no display-only filing-history fetch');
      assert.equal(await page.locator('[data-src-bound], [data-src-version], [data-src-diagnostic], table').count(), 0);
      assert.ok(!/revision|attempt|history|recorded/i.test(await text(page, '[data-src-section="filing"]')));
      assert.ok(!calls.some(c => /context\/(file|map)/.test(c.path)));
      await closeClean(context, ['processes']);   // the footer's own process read was not recorded in this host state
      console.log('ok a source with many recorded filings renders no history, attempts or bound note');
    }

    // ---- 6. document retry: inspection, key, duplicates, uncertainty, conflict --------------------------------------------------
    {
      const {page, context} = await fresh('pilot-blocked');
      await mountBusiness(page, `_source/${ID.pilot}`);
      await sourceText(page, ID.pilot);
      assert.equal(await page.locator('[data-src-retry]').count(), 1);
      const shownRevision = recorded('pilot-blocked', '/context_get', body => body.max_chars === 100).data.document.filing.document_revision;
      // INJECTED uncertain outcome: the connection drops; the next press repeats the SAME request and key and the host answers
      let n = 0;
      control.mutation = {context_retry: async () => { if (n++ > 0) return null; await new Promise(r => setTimeout(r, 500)); return 'drop'; }};
      control.served = call => { if (call.op === 'context_retry') replay.use('pilot-retry-pending'); };      // the host moved on once it accepted the retry
      const start = calls.length;
      await page.locator('[data-src-retry]').dblclick();
      await message(page, /Couldn't confirm\. Try again/);
      assert.equal(ops('context_retry', start).length, 1, 'double click sends once');
      const inspected = ops('context_get', start);
      assert.equal(inspected.length, 1); assert.deepEqual(inspected[0].body, {scope: 'business', document_id: ID.pilot, max_chars: 100});
      const sent = ops('context_retry', start)[0];
      assert.deepEqual(Object.keys(sent.body).sort(), ['expected_revision', 'idempotency_key', 'upload_id']);
      assert.equal(sent.body.upload_id, ID.pilot); assert.equal(sent.body.expected_revision, shownRevision); assert.ok(!JSON.stringify(sent.body).includes('force'));
      await page.locator('[data-src-retry]').click();
      await message(page, /Requested/);
      await idle(page);
      const again = ops('context_retry', start);
      assert.equal(again.length, 2); assert.deepEqual(again[1].body, sent.body);
      assert.ok(!again[1].injected, 'the repeat reached the recorded host answer');
      assert.equal(ops('context_get', start).length, 2, 'only the status refresh rereads; the repeat did not re-inspect');
      assert.ok(!calls.slice(start).some(c => c.op === 'context_queue_retry'), 'no input retry for a document retry');
      assert.match(await text(page, '[data-src-filing]'), /Couldn't process/, 'the recorded retry-pending state is still blocked until the driver starts the attempt');
      await closeClean(context);

      // the host moved BEFORE the click (the page had not noticed): the preflight refreshes the display and sends nothing
      const moved = await fresh('pilot-blocked');
      await mountBusiness(moved.page, `_source/${ID.pilot}`);
      await sourceText(moved.page, ID.pilot);
      replay.use('pilot-applied');
      const appliedRevision = recorded('pilot-applied', '/context_get', body => body.max_chars === 100).data.document.filing.document_revision;
      assert.notEqual(appliedRevision, shownRevision);
      await moved.page.locator('[data-src-retry]').click();
      await message(moved.page, /Changed since you opened it\. Refresh/);
      await moved.page.waitForSelector('[data-src-filing] [data-src-state="filed"]');
      assert.equal(ops('context_retry').length, 0, 'no mutation after a changed display');
      await closeClean(moved.context);

      // the host moved AFTER the preflight: it refuses the displayed revision; nothing else is sent and the page rereads
      const late = await fresh('pilot-blocked');
      await mountBusiness(late.page, `_source/${ID.pilot}`);
      await sourceText(late.page, ID.pilot);
      control.served = call => { if (call.op === 'context_get' && call.body.max_chars === 100 && call.stage === 'pilot-blocked') replay.use('pilot-applied'); };
      const before = calls.length;
      await late.page.locator('[data-src-retry]').click();
      await message(late.page, /Changed since you opened it\. Refresh/);
      const refused = ops('context_retry', before);
      assert.equal(refused.length, 1); assert.equal(refused[0].body.expected_revision, shownRevision);
      const conflict = recorded('pilot-applied', '/context_retry', body => body.expected_revision === shownRevision);
      assert.equal(conflict.error.code, 'revision_conflict');
      await late.page.waitForSelector('[data-src-filing] [data-src-state="filed"]');
      await idle(late.page);
      assert.equal(ops('context_retry', before).length, 1, 'a refused revision is never resent on its own');
      assert.equal(await late.page.locator('[data-src-retry]').count(), 0, 'no retry is offered for the filed revision');
      await closeClean(late.context);
      console.log('ok document retry inspects the revision, repeats the same key when uncertain, rereads on conflict and never swaps the token');
    }

    // ---- 7. retract and exact-input controls ------------------------------------------------------------------------------------
    {
      const {page, context} = await fresh('pilot-applied');
      await mountBusiness(page, `_source/${ID.pilot}`);
      await sourceText(page, ID.pilot);
      assert.equal(await page.locator('[data-src-retract-confirm]').count(), 0);
      let start = calls.length;
      await page.locator('[data-src-retract]').click();
      assert.equal(ops('context_retract', start).length, 0, 'asks first');
      await page.locator('[data-src-retract-cancel]').click();
      await page.locator('[data-src-retract]').click();
      control.served = call => { if (call.op === 'context_retract') replay.use('pilot-retract-pending'); };
      await page.locator('[data-src-retract-confirm]').click();
      await message(page, /Removal requested/);
      assert.match(await text(page, '[data-src-actions]'), /Refresh/);
      const retract = ops('context_retract', start);
      assert.equal(retract.length, 1); assert.deepEqual(Object.keys(retract[0].body).sort(), ['expected_revision', 'idempotency_key', 'upload_id']);
      assert.equal(retract[0].body.expected_revision, recorded('pilot-applied', '/context_retract', body => body.idempotency_key === 'ui-pilot-retract').data.filing.document_revision);
      assert.ok(!JSON.stringify(calls).includes('"force"'));
      await closeClean(context);

      // the host refuses a retract bound to a revision that moved after the preflight
      const stale = await fresh('pilot-applied');
      await mountBusiness(stale.page, `_source/${ID.pilot}`);
      await sourceText(stale.page, ID.pilot);
      control.served = call => { if (call.op === 'context_get' && call.body.max_chars === 100 && call.stage === 'pilot-applied') replay.use('pilot-retracted'); };
      await stale.page.locator('[data-src-retract]').click();
      await stale.page.locator('[data-src-retract-confirm]').click();
      await message(stale.page, /Changed since you opened it\. Refresh/);
      assert.equal(recorded('pilot-retracted', '/context_retract', body => body.idempotency_key === 'ui-pilot-retract-stale').error.code, 'revision_conflict');
      await closeClean(stale.context);

      // a blocked input exposes no input-level controls and its payload is never read or rendered
      const blocked = await fresh('blocked-input');
      await mountBusiness(blocked.page, `_source/${ID.blocked}`);
      await sourceText(blocked.page, ID.blocked);
      await blocked.page.waitForTimeout(300);
      assert.equal(await blocked.page.locator('[data-src-input-retry], [data-src-input-discard]').count(), 0);
      assert.ok(!calls.some(c => /context_queue_(get|retry|discard)$/.test(c.path)), 'no input-level requests');
      const payload = recorded('blocked-input', '/context_queue_get').data.input.payload;
      assert.ok(payload && typeof payload.content === 'string');
      assert.ok(!(await text(blocked.page)).includes(payload.content.slice(0, 20)));
      assert.match(await text(blocked.page, '[data-src-filing]'), /Couldn't process/);
      await closeClean(blocked.context);
      console.log('ok removal is confirmed and bound to the displayed revision; no input-level controls exist');
    }

    // ---- 8. late navigation + dirty editor + status refresh ----------------------------------------------------------------------
    {
      const {page, context} = await fresh('final');
      await mountBusiness(page, `_source/${ID.queued}`);
      await sourceText(page, ID.queued);
      const held = gate(body => body.document_id === ID.blocked);
      control.hold = {context_get: held};
      await page.evaluate(id => ctxGo(`_source/${id}`), ID.blocked);
      await held.seen;
      await go(page, `_source/${ID.history}`);
      await sourceText(page, ID.history);
      held.release();
      await page.waitForTimeout(300);
      assert.match(await text(page, '[data-src-view]'), /Filed history 00|Filed/);
      assert.equal(await page.locator(`[data-src-view="${ID.blocked}"]`).count(), 0);
      assert.equal(await page.locator('[data-src-state="blocked"]').count(), 0);
      // authored file: dirty text survives a Context queue refresh
      await go(page, 'notes/a.md');
      await page.waitForSelector('#jta');
      await page.fill('#jta', 'unsaved edit');
      await page.evaluate(() => ctxQueueRefresh());
      await page.waitForFunction(() => /Context needs attention/.test(document.querySelector('#ctxq')?.textContent || ''));
      assert.equal(await page.inputValue('#jta'), 'unsaved edit');
      await page.evaluate(() => { _jState.dirty = false; });
      await closeClean(context);
      console.log('ok a late response for a previous document never paints; a status refresh leaves a dirty editor untouched');
    }

    // ---- 9. footer queue -----------------------------------------------------------------------------------------------
    {
      const {page, context} = await fresh('final');
      await mountBusiness(page, '_uploads');
      await page.waitForFunction(() => document.querySelector('#ctxq [data-src-queue]'));
      const queue = recorded('final', '/context').context_queue;
      assert.ok(queue.queued && queue.blocked && queue.completion_conflict && queue.blocked_input_id, 'the recorded final summary has queued, blocked, conflict and a held head');
      const footer = (await text(page, '#ctxq')).trim();
      assert.equal(footer, 'Context needs attention', 'no counters, identifiers or mechanics');
      // INJECTED: a process view whose Context queue is missing is a visible error, not "all clear"
      control.tamper = op => (op === 'processes' ? {filing: {current: null}} : null);
      await page.evaluate(() => ctxQueueRefresh());
      await page.waitForFunction(() => document.querySelector('[data-src-queue-error]'));
      assert.equal((await text(page, '#ctxq')).trim(), 'Context needs attention');
      control.tamper = null;
      await closeClean(context);
      console.log('ok the footer speaks only about the Context queue, show missing composition as an error and invent no completion');
    }

    // ---- 10. Global library -----------------------------------------------------------------------------------------------------
    {
      const {page, context} = await fresh('global-queued');
      await mountGlobal(page, '#/global/context/library');
      await page.waitForSelector('[data-src-list="context_note"] [data-src-status]');
      await page.waitForFunction(() => document.querySelector('[data-src-list] [data-src-status]').textContent === 'None.');
      const options = await page.$$eval('.global-library select option', els => els.map(e => e.textContent));
      assert.deepEqual(options, ['Materials', 'Topics', 'People', 'Uploads', 'Dumps']);
      await page.selectOption('.global-library select', 'context_upload');
      await page.waitForSelector('[data-src-state="queued"]');
      assert.ok(ops('context_list').every(c => c.path === '/api/root/operations/context_list' && c.body.scope === 'shared'));
      assert.ok(!calls.some(c => c.path.includes('context-library')), 'the old library REST route is gone');
      await page.locator(`[data-src-open="${ID.global0}"]`).click();
      await page.waitForFunction(id => location.hash === `#/global/context/library/${id}`, ID.global0);
      await page.waitForSelector(`[data-src-view="${ID.global0}"]`);
      await sourceText(page, ID.global0);
      assert.ok(ops('context_get').every(c => c.path === '/api/root/operations/context_get' && c.body.scope === 'shared'));
      assert.match(await text(page, '[data-src-filing]'), /Waiting/);
      assert.equal(await page.locator('[data-src-retract]').count(), 0);
      assert.equal(await text(page, '.crumbs .cur'), await text(page, '.src-title'), 'the breadcrumb names the source, not its id');
      await shot(page, 'global-source-desktop');
      await closeClean(context);
      console.log('ok the Global library lists by type with neutral labels and opens a source by its document UUID in the root namespace');
    }

    // ---- 10b. context_queue is scope-wide; other kinds keep their scope -----------------------------------------------------------
    {
      const {page, context} = await fresh('pilot-blocked');
      await mountBusiness(page, `_source/${ID.pilot}`);
      await sourceText(page, ID.pilot);
      await page.waitForFunction(() => document.querySelector('#ctxq [data-src-queue]'));
      await page.waitForTimeout(400);
      let start = calls.length;
      await page.evaluate(() => receiveChanges({kinds: ['documents'], business_id: 'other'}));
      await page.waitForTimeout(900);
      assert.equal(calls.slice(start).length, 0, 'another business\'s documents change is not ours');
      await page.evaluate(() => receiveChanges({kinds: ['context_queue'], business_id: 'other'}));
      for (let i = 0; i < 40 && !(ops('processes', start).length && ops('context_get', start).length); i++) await page.waitForTimeout(50);
      assert.ok(ops('processes', start).length >= 1, 'footer reread');
      const refreshed = ops('context_get', start);
      assert.ok(refreshed.length >= 1 && refreshed.every(c => c.body.max_chars === 100 && !c.body.cursor), 'status-only source refresh');
      assert.equal(ops('context_upload_map', start).length, 0);
      assert.ok((await text(page, '[data-src-body]')).includes('Met Alice'), 'body untouched');
      // teardown: leaving the source stops its subscription
      await go(page, '_uploads');
      await page.waitForTimeout(500);
      start = calls.length;
      await page.evaluate(() => receiveChanges({kinds: ['context_queue'], business_id: 'other'}));
      await page.waitForTimeout(900);
      assert.equal(ops('context_get', start).length, 0, 'no source reread after teardown');
      assert.equal(await page.evaluate(() => storeVersion('context_queue', null)) >= 2, true);
      assert.equal(await page.evaluate(() => storeVersion('context_queue', 'other')), 0);
      await closeClean(context, ['context_list']);
      console.log('ok context_queue events are scope-wide, refresh footer and source status only, and unsubscribe on teardown');
    }

    // ---- 10c. the host's verbatim captures drive the page's own validators (captured.json, HTTP-dispatcher captures only) ----------
    {
      const captured = JSON.parse(fs.readFileSync(fixtureFile('captured.json'), 'utf8'));
      assert.equal(captured.provenance.status, 'captured');
      assert.deepEqual([captured.provenance.host_commit, captured.provenance.operations_commit, captured.provenance.context_commit],
        ['4f96d823c8d0b1ea8b5f3935d114b79ad1e8356b', '2d697fa3973e929f7f2be5afa5e9a731db634aaa', '32431e125b1982ea10fce4db1993ab22c6a241be']);
      assert.ok(captured.captures.length > 40 && captured.captures.every(entry => entry.transport === 'http-dispatcher'), 'HTTP evidence only');
      const named = name => captured.captures.find(entry => entry.name === name);
      const {page, context} = await fresh('final');
      const processes = captured.captures.filter(entry => entry.request.method === 'GET' && entry.request.query?.view === 'processes' && entry.response.status === 200 && entry.response.body.context_queue);
      assert.ok(processes.length >= 4);
      const queueChecks = await page.evaluate(all => all.map(queue => ({valid: srcQueueValid(queue), bits: srcQueueBits(queue).map(b => b.text)})), processes.map(entry => entry.response.body.context_queue));
      assert.ok(queueChecks.every(check => check.valid && check.bits.every(text => ['Updating Context', 'Context needs attention'].includes(text))), JSON.stringify(queueChecks));
      const wrongView = named('global/processes-wrong-view');
      assert.ok(wrongView.response.status >= 400 && !wrongView.response.body.context_queue, 'the host refuses another view; the page would show an error, not an empty queue');
      // the captured document commands carry only upload_id, expected_revision and the key
      for (const name of ['pilot/retry-stale-revision', 'pilot/retract', 'pilot/retract-stale-revision', 'global-ops/third/retry', 'global-ops/applied/retract']) {
        assert.deepEqual(Object.keys(named(name).request.body).sort(), ['expected_revision', 'idempotency_key', 'upload_id'], name);
      }
      // foreign-scope operations are refused and never a success the page could act on
      for (const name of ['global-ops/foreign/retry-business-source', 'global-ops/foreign/retract-business-source', 'global-ops/foreign/queue-retry-business-input', 'global-ops/foreign/queue-discard-business-input',
        'global-ops/foreign/retry-global-source-from-business', 'global-ops/foreign/retract-global-source-from-business']) assert.ok(named(name).response.status >= 400 && named(name).response.body.status === 'error', name);
      // body paging: the captured cursor relationship (request cursor = response cursor) and the captured refusal after a body change
      const pageOne = named('long-business/page-1'), pageTwo = named('long-business/page-2-after-body-change');
      assert.equal(pageTwo.request.body.cursor, pageOne.response.body.data.next_cursor);
      assert.equal(pageTwo.response.body.status, 'error');
      // snapshots on notes and journal entries use the same filing labels
      const filings = [...named('note/submitted-queued').response.body, ...named('journal/submitted-queued').response.body.flatMap(row => row.filerSubmissions)].map(row => row.filing).filter(Boolean);
      const labels = await page.evaluate(all => all.map(f => srcFilingLabel(f).label), filings);
      assert.ok(filings.length >= 2 && labels.every(label => label !== 'Unavailable'), labels.join(','));
      await closeClean(context);
      console.log('ok the host\'s verbatim HTTP captures satisfy the page\'s queue and filing validators; foreign-scope operations are refused');
    }

    // ---- 11. screenshots -------------------------------------------------------------------------------------------------------------
    {
      for (const [name, viewport] of [['desktop', {width: 1440, height: 960}], ['narrow', {width: 390, height: 844}]]) {
        const {page, context} = await fresh('pilot-blocked', viewport);
        await mountBusiness(page, `_source/${ID.pilot}`);
        await sourceText(page, ID.pilot);
        await page.waitForSelector('[data-src-filing] [data-src-state]');
        await shot(page, `source-${name}`);
        const overflow = await page.evaluate(() => { const v = document.querySelector('.src-view'); return v.scrollWidth - v.clientWidth; });
        assert.ok(overflow <= 1, `${name} source panel overflows by ${overflow}px`);
        replay.use('final');
        await go(page, '_uploads');
        await page.waitForSelector('[data-src-list="context_upload"] [data-src-open]');
        await shot(page, `uploads-${name}`);
        await closeClean(context);
      }
      console.log('ok desktop and narrow source panels do not overflow');
    }

    assert.deepEqual(pageErrors, []);
    console.log('all context source checks passed (recorded exchanges generated through the fixed H2 host handlers, host 4f96d823)');
  } finally {
    await browser.close(); server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
