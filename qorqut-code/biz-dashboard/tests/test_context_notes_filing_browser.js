#!/usr/bin/env node
// Note and journal filing status with the REAL js/notes.js renderer and handlers: business note and Global journal snapshots,
// a queue-event refresh that leaves a dirty note alone, and the exact retry request (revision bound, same key when uncertain,
// nothing sent or painted after the owner leaves the note). Loopback only.
// FIXTURE PROVENANCE: answers come only from recorded host exchanges (tests/fixtures/context-source/exchanges.json) generated
// through the fixed H2 host handlers (host 4f96d823, Operations 2d697fa3, Context 32431e12) over the host's isolated synthetic
// test composition; an unrecorded request fails. It is not an installed product. Injected cases are marked in the test.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {Replay, handle, fixtureFile} = require('./support/exchange_replay');
const root = path.resolve(__dirname, '..');
const replay = new Replay(fixtureFile('exchanges.json'));
assert.equal(replay.document.provenance.status, 'generated-through-fixed-host-handlers');
assert.equal(replay.document.provenance.host_commit, '4f96d823c8d0b1ea8b5f3935d114b79ad1e8356b');
const BIZ = 'first', BIZ_ID = '56143392-10bd-4bf7-afae-a2491b6ca066';  // the recorded project's UUID: notes target the UUID, never the slug
const notesAt = stage => replay.first(stage, request => request.path === `/api/biz/${BIZ}/notes`).response.body;
const journalAt = stage => replay.first(stage, request => request.path === '/api/root/notes').response.body;
const noteTitled = (stage, title) => notesAt(stage).find(note => note.title === title);
const journalTitled = (stage, title) => journalAt(stage).find(note => note.title === title);

const calls = [], control = {}, errors = [];
const reset = () => { for (const key of Object.keys(control)) delete control[key]; };
const styles = fs.readFileSync(path.join(root, 'index.html'), 'utf8').match(/<link rel="stylesheet"[^>]+>/g).join('\n');
const scripts = ['core', 'san-juan-time', 'store', 'events', 'context-source', 'notes'];
const html = `<!doctype html><html><head><meta charset="utf-8">${styles}</head><body><div id="toasts"></div><button id="wsbtn" hidden></button><div id="tbody" class="tbody"></div>
  <script>function relativeTime() { return ''; } function workRelatedChips() {} var tView = 'businessNotes', SVG_TINBOX = '', ICO_PLUS = '', ICO_PIN = '', ICO_PENCIL = '', ICO_TRASH = '';</script>
  ${scripts.map(name => `<script src="/js/${name}.js"></script>`).join('')}</body></html>`;
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const json = (data, status = 200) => { res.writeHead(status, {'Content-Type': 'application/json'}); res.end(JSON.stringify(data)); };
  if (url.pathname === '/') { res.setHeader('Content-Type', 'text/html'); return res.end(html); }
  // INJECTED (pending backend regeneration): the recorded host exchanges predate the ordinary Project notes contract. The dashboard now
  // sends target_business_id and no category, so the list read is answered by the recorded strategy-notes list for the same project.
  if (url.pathname === `/api/biz/${BIZ}/notes` && req.method === 'GET' && url.searchParams.get('target_business_id') === BIZ_ID && !url.searchParams.has('category')) url.search = '?category=strategy-notes';
  if (await handle(req, res, url, {replay, control, calls})) return;
  if (url.pathname.startsWith('/api/')) { calls.push({path: url.pathname + url.search, op: 'other', body: {}, unrecorded: true}); return json({status: 'error', error: {code: 'unrecorded_request', message: 'Not a recorded request'}}, 400); }
  const file = path.join(root, url.pathname);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'application/octet-stream');
  res.end(fs.readFileSync(file));
});
const gate = when => { let release, arrived; const promise = new Promise(r => { release = r; }); const seen = new Promise(r => { arrived = r; }); return {promise, release, arrived, seen, when}; };
const ops = (op, start = 0) => calls.slice(start).filter(call => call.op === op);
const filingText = page => page.evaluate(() => document.querySelector('#ntfiling')?.textContent || '');
const closeClean = async context => { assert.deepEqual(calls.filter(call => call.unrecorded).map(call => call.path + ' ' + JSON.stringify(call.body)), [], 'unrecorded requests'); await context.close(); };

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({headless: true, ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {})});
  const fresh = async stage => {
    reset(); calls.length = 0; replay.use(stage);
    const context = await browser.newContext({viewport: {width: 1300, height: 900}});
    await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    const page = await context.newPage(); page.setDefaultTimeout(8000);
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(origin);
    return {page, context};
  };
  const openBusiness = async (page, id) => {
    await page.evaluate(async ([noteId, slug, bizId]) => {
      state.scope = 'business'; state.biz = slug; state.bizs = [{id: bizId, slug}]; state.tab = 'tasks'; tView = 'businessNotes';
      await ntFetch(); ntSelRestored = true; ntSetSel(noteId); ntRender();
    }, [id, BIZ, BIZ_ID]);
    await page.waitForSelector('#ntfiling');
  };
  try {
    // ---- business note: recorded snapshot status, exact retry, uncertain replay ---------------------------------------------------
    {
      const {page, context} = await fresh('note-blocked');
      const note = noteTitled('note-blocked', 'Strategy');
      assert.equal(note.filing.state, 'blocked'); assert.ok(note.uploadId);
      await openBusiness(page, note.id);
      const shown = await filingText(page);
      assert.match(shown, /Couldn't process/);
      assert.ok(!shown.includes(note.filing.error.message), 'the raw recorded reason is not shown: ' + shown);
      assert.equal(await page.locator('#ntretry').count(), 1); assert.equal(await page.locator('#ntviewcontext').count(), 1);
      // INJECTED uncertain outcome: the connection drops; the next press repeats the SAME request and key and the host answers
      let n = 0;
      control.mutation = {context_retry: async () => { if (n++ > 0) return null; await new Promise(r => setTimeout(r, 400)); return 'drop'; }};
      control.served = call => { if (call.op === 'context_retry') replay.use('note-retry-pending'); };
      const start = calls.length;
      await page.locator('#ntretry').dblclick();
      await page.waitForFunction(() => srcBusy.size === 0);
      assert.equal(ops('context_retry', start).length, 1, 'one request for a double click');
      const inspect = ops('context_get', start)[0];
      assert.equal(inspect.path, `/api/biz/${BIZ}/operations/context_get`); assert.deepEqual(inspect.body, {scope: 'business', document_id: note.uploadId, max_chars: 100});
      const first = ops('context_retry', start)[0];
      assert.equal(first.path, `/api/biz/${BIZ}/operations/context_retry`);
      assert.deepEqual(Object.keys(first.body).sort(), ['expected_revision', 'idempotency_key', 'upload_id']);
      assert.equal(first.body.expected_revision, note.filing.document_revision); assert.equal(first.body.upload_id, note.uploadId);
      await page.locator('#ntretry').click();
      await page.waitForFunction(() => srcBusy.size === 0);
      const second = ops('context_retry', start);
      assert.equal(second.length, 2); assert.deepEqual(second[1].body, first.body, 'the uncertain request repeats unchanged');
      assert.ok(!second[1].injected, 'the repeat reached the recorded host answer');
      await closeClean(context);
      console.log('ok the real note renderer shows the recorded snapshot state and sends one exact retry');
    }

    // ---- a note filed and then edited: the submitted snapshot and the changed note are separate facts ---------------------------------
    {
      const {page, context} = await fresh('note-edited-after-submission');
      const note = noteTitled('note-edited-after-submission', 'Strategy');
      assert.equal(note.changedSinceUpload, true); assert.equal(note.filing.state, 'published');
      await openBusiness(page, note.id);
      const shown = await filingText(page);
      assert.match(shown, /Added to Context · Changes not submitted/); assert.ok(!/revision|snapshot|commit/i.test(shown), 'no technical detail');
      assert.equal(await page.locator('#ntretry').count(), 0);
      assert.equal(await page.locator('#ntviewcontext').count(), 1);
      await closeClean(context);
      console.log('ok a recorded filed-then-edited note reports the change separately from its submitted snapshot');
    }

    // ---- leaving the note before the preflight resolves: nothing is sent, nothing is repainted --------------------------------
    {
      const {page, context} = await fresh('note-blocked');
      const blocked = noteTitled('note-blocked', 'Strategy'), other = noteTitled('note-blocked', 'Other note');
      assert.equal(other.filing.state, 'published');
      await openBusiness(page, blocked.id);
      const held = gate(body => body.document_id === blocked.uploadId);
      control.hold = {context_get: held};
      const start = calls.length;
      await page.locator('#ntretry').click();
      await held.seen;
      await page.evaluate(id => { ntSetSel(id); ntRender(); }, other.id);
      await page.waitForSelector('#ntfiling');
      assert.match(await filingText(page), /Added to Context/);
      held.release();
      await page.waitForTimeout(400);
      assert.equal(ops('context_retry', start).length, 0, 'no mutation for a note that is no longer selected');
      assert.match(await filingText(page), /Added to Context/); assert.equal(await page.locator('#ntretry').count(), 0);
      await closeClean(context);
      console.log('ok a delayed preflight cannot submit or repaint after another note is selected');
    }

    // ---- a Context queue event refreshes the status and leaves a dirty note alone --------------------------------------------
    {
      const {page, context} = await fresh('note-blocked');
      const note = noteTitled('note-blocked', 'Strategy');
      await openBusiness(page, note.id);
      await page.evaluate(([id, title]) => { ntEditState = {id, biz: state.biz, title0: title, lines0: [''], statuses0: '{}', lines: ['', 'unsaved edit'], statuses: {}, dirty: true, saving: false}; ntPaintUpload(); }, [note.id, note.title]);
      replay.use('note-edited-after-submission');                             // the host filed the submission and the note was edited elsewhere
      const start = calls.length;
      await page.evaluate(() => receiveChanges({kinds: ['context_queue'], business_id: 'other'}));
      await page.waitForFunction(() => /Added to Context/.test(document.querySelector('#ntfiling')?.textContent || ''));
      assert.ok(ops('notes', start).length >= 1);
      assert.deepEqual(await page.evaluate(() => ({dirty: ntEditState.dirty, lines: ntEditState.lines})), {dirty: true, lines: ['', 'unsaved edit']});
      assert.match(await filingText(page), /Changes not submitted/, 'the unsaved edit is still reported against the snapshot');
      await closeClean(context);
      console.log('ok a queue-driven status refresh updates the snapshot state and preserves the dirty note');
    }

    // ---- Global journal submissions --------------------------------------------------------------------------------------------
    {
      const {page, context} = await fresh('journal-blocked');
      const entry = journalTitled('journal-blocked', 'Daily blocked');
      const submission = entry.filerSubmissions[0];
      assert.equal(submission.filing.state, 'blocked');
      await page.evaluate(async id => { state.scope = 'global'; state.biz = null; tView = 'globalNotes'; await ntFetch(); ntSetSel(id); ntRender(); }, entry.id);
      await page.waitForSelector('#ntfiling .ntfilingrow');
      const rows = await page.$$eval('#ntfiling .ntfilingrow', els => els.map(e => ({text: e.textContent, href: e.querySelector('a')?.getAttribute('href'), retry: !!e.querySelector('.ntretryfiler')})));
      assert.equal(rows.length, 1);
      assert.match(rows[0].text, /Couldn't process/); assert.equal(rows[0].retry, true);
      assert.equal(rows[0].href, `#/global/context/library/${submission.uploadId}`);
      control.served = call => { if (call.op === 'context_retry') replay.use('journal-filed-and-edited'); };
      const start = calls.length;
      await page.locator('.ntretryfiler').click();
      await page.waitForFunction(() => srcBusy.size === 0);
      const inspect = ops('context_get', start)[0], retry = ops('context_retry', start)[0];
      assert.equal(inspect.path, '/api/root/operations/context_get'); assert.deepEqual(inspect.body, {scope: 'shared', document_id: submission.uploadId, max_chars: 100});
      assert.equal(retry.path, '/api/root/operations/context_retry');
      assert.deepEqual(Object.keys(retry.body).sort(), ['expected_revision', 'idempotency_key', 'upload_id']);
      assert.equal(retry.body.expected_revision, submission.filing.document_revision);
      await closeClean(context);

      // a filed submission of an edited entry: no retry, the change is reported separately
      const filed = await fresh('journal-filed-and-edited');
      const edited = journalAt('journal-filed-and-edited').find(row => row.title === 'Daily filed');
      assert.equal(edited.filerSubmissions[0].changedSinceSubmission, true);
      await filed.page.evaluate(async id => { state.scope = 'global'; state.biz = null; tView = 'globalNotes'; await ntFetch(); ntSetSel(id); ntRender(); }, edited.id);
      await filed.page.waitForSelector('#ntfiling .ntfilingrow');
      const row = await filed.page.$eval('#ntfiling .ntfilingrow', e => ({text: e.textContent, retry: !!e.querySelector('.ntretryfiler')}));
      assert.match(row.text, /Added to Context/); assert.match(row.text, /Changes not submitted/); assert.equal(row.retry, false);
      await closeClean(filed.context);
      console.log('ok Global journal submissions render with document links and retry through the root namespace');
    }
    assert.deepEqual(errors, []);
    console.log('all note filing checks passed (recorded exchanges generated through the fixed H2 host handlers, host 4f96d823)');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); console.error('page errors:', errors); process.exit(1); });
