#!/usr/bin/env node
// Costs page: compact Context usage rows (date, observed model, cost), known subtotal, asynchronous reads and the summary / profit-and-loss wording.
// A disposable loopback page serves the committed dashboard files; the API answers with the exact accepted backend DTOs
// recorded in tests/fixtures/context-usage (see its generate.py and provenance.json). Synthetic only: no real cookies,
// credentials, businesses or operational data, and nothing leaves 127.0.0.1.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..'), fixtures = path.join(root, 'tests/fixtures/context-usage');
const evidence = process.env.EVIDENCE_DIR;
const fixture = name => JSON.parse(fs.readFileSync(path.join(fixtures, name + '.json'), 'utf8'));
const provenance = fixture('provenance');

// Every scenario lists the recorded pages (request + response) of the selections it can serve; the router answers only a request
// whose full filter set, limit and cursor equal a recorded one, as the real backend binds a cursor to its exact filters.
const usage = {main: [...fixture('usage-month'), ...fixture('usage-all')], empty: fixture('usage-empty-month'), paged: fixture('usage-month-pages'),
  unfinished: [...fixture('usage-unfinished-month'), ...fixture('usage-unfinished')], silent: [...fixture('usage-no-evidence-month'), ...fixture('usage-no-evidence')]};
// A deliberate new selection after the month turns, and All time, are served for any scenario from these recordings.
const shared = [...fixture('usage-empty-month'), ...fixture('usage-all')];
const overviews = {main: fixture('overview-main'), complete: fixture('overview-complete'), silent: fixture('overview-no-evidence'),
  history: fixture('overview-unknown-month'), unavailable: fixture('overview-unavailable')};
const unavailable = fixture('usage-unavailable');

const calls = {usage: [], overview: 0, mismatches: []};
const control = {scenario: 'main', overview: 'main', failNext: false, failFirst: false, hold: null, repeatOnSecondPage: false, unavailable: false};
const clear = () => Object.assign(control, {scenario: 'main', overview: 'main', failNext: false, failFirst: false, hold: null, repeatOnSecondPage: false, unavailable: false});

const sameRequest = (left, right) => JSON.stringify(Object.entries(left).sort()) === JSON.stringify(Object.entries(right).sort());
function pageFor(body) {
  const page = [...usage[control.scenario], ...shared].find(item => sameRequest(item.request, body));
  if (!page) { calls.mismatches.push(body); return null; }
  return structuredClone(page.response);
}

const styles = fs.readFileSync(path.join(root, 'index.html'), 'utf8').match(/<link rel="stylesheet"[^>]+>/g).join('\n');
const scripts = ['core', 'san-juan-time', 'global-filing-costs', 'global-costs'];
const html = `<!doctype html><html><head><meta charset="utf-8">${styles}</head><body>
  <div id="toasts"></div><button id="wsbtn" hidden></button><div id="host"></div>
  ${scripts.map(name => `<script src="/js/${name}.js"></script>`).join('')}
  <script>state.scope = 'global'; state.globalTab = 'costs'; state.bizs = [{id:'biz-1', slug:'one', name:'One'}];
  window.mountCosts = () => renderGlobalCosts(document.querySelector('#host'));</script></body></html>`;

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const json = (data, status = 200) => { res.writeHead(status, {'Content-Type': 'application/json'}); res.end(JSON.stringify(data)); };
  if (url.pathname === '/') { res.setHeader('Content-Type', 'text/html'); return res.end(html); }
  if (url.pathname.startsWith('/api/root/operations/')) {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw || '{}'), op = url.pathname.split('/').at(-1);
    if (op === 'subscriptions_list') return json({status: 'ok', data: {subscriptions: [], items: []}});
    if (op === 'costs_overview') { calls.overview++; return json({status: 'ok', data: overviews[control.overview]}); }
    if (op === 'context_filing_costs') {
      calls.usage.push(body);
      if (control.hold) { const hold = control.hold; control.hold = null; const data = control.unavailable ? null : pageFor(body); await hold.promise; hold.served = body;
        return control.unavailable ? json({status: 'error', error: unavailable.error}, 503) : data ? json({status: 'ok', data}) : json({status: 'error', error: {code: 'validation_error', message: 'Invalid or differently scoped usage cursor'}}, 400); }
      if (control.unavailable) return json({status: 'error', error: unavailable.error}, 503);
      if (control.failNext && body.cursor) { control.failNext = false; return json({status: 'error', error: {code: 'temporarily_unavailable', message: 'The ledger is busy'}}, 503); }
      if (control.failFirst && !body.cursor) { control.failFirst = false; return json({status: 'error', error: {code: 'temporarily_unavailable', message: 'The first page failed'}}, 503); }
      const data = pageFor(body);
      if (!data) return json({status: 'error', error: {code: 'validation_error', message: 'Invalid or differently scoped usage cursor'}}, 400);
      if (control.repeatOnSecondPage && body.cursor) {
        // A later page can repeat an earlier call with a newer cumulative snapshot (here: a larger recorded charge).
        const first = structuredClone(fixture('usage-all')[0].response.calls[0]); first.usage.cost_usd = '0.5'; first.updated_at = '2026-10-12T09:00:00.000000Z';
        data.calls.unshift(first);
      }
      return json({status: 'ok', data});
    }
    return json({status: 'ok', data: {}});
  }
  const file = path.join(root, url.pathname);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css'); res.end(fs.readFileSync(file));
});

const deferred = () => { let release; const promise = new Promise(resolve => { release = resolve; }); return {promise, release}; };
const section = page => page.locator('#costs-filing');

async function open(browser, {width = 1280, scenario = 'main', overview = 'main', period = 'month', unavailable = false, now = '2026-10-20T15:00:00Z', failFirst = false} = {}) {
  clear(); Object.assign(control, {scenario, overview, unavailable, failFirst}); calls.usage.length = 0; calls.overview = 0; calls.mismatches.length = 0;
  const context = await browser.newContext({viewport: {width, height: 1000}, timezoneId: 'America/Puerto_Rico'});
  const page = await context.newPage(), errors = [];
  page.on('pageerror', error => errors.push(error.message + ' ' + error.stack.split('\n').slice(1, 3).join(' | ')));
  page.setDefaultTimeout(10000);
  await page.clock.install({time: new Date(now)});
  await page.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
  await page.goto(origin);
  await page.evaluate(() => { mountCosts(); });
  await page.locator('#costs-summary .costs-total').waitFor();
  if (period === 'all') { await page.getByLabel('Period').selectOption('all'); }
  await page.waitForFunction(() => document.querySelector('#filing-cost-total .filing-cost-subtotal') || document.querySelector('#filing-cost-status [role=alert]'));
  return {page, context, errors};
}

// Viewport captures of the changed section (the page scrolls inside its own pane, so an element capture would be clipped).
async function shot(page, name, target = '#costs-filing') {
  fs.mkdirSync(evidence, {recursive: true});
  await page.locator(target).scrollIntoViewIfNeeded();
  await page.screenshot({path: path.join(evidence, name)});
}
const rowOf = (page, id) => page.locator(`[data-filing-call="${id}"]`);
const withoutCursor = request => Object.fromEntries(Object.entries(request).filter(([key]) => key !== 'cursor'));
const callsOf = (name, request) => usage[name].filter(item => sameRequest(withoutCursor(item.request), request)).flatMap(item => item.response.calls);
let origin;

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {})});
  try {
    // -- exact decimal presentation ------------------------------------------------------------------------------------------
    {
      const {page, context, errors} = await open(browser);
      const money = await page.evaluate(() => ({
        zero: filingCostMoney('0'), half: filingCostMoney('0.5'), small: filingCostMoney('0.00004'), long: filingCostMoney('0.1234567'),
        big: filingCostMoney('1234567.5'), unknown: [null, undefined, '', 'abc', '-1', '1e3', 0.5, {}].map(value => filingCostMoney(value)),
        overview: [costsMoney(null), costsMoney(undefined), costsMoney('0.0000'), costsMoney('0.2470')]}));
      assert.deepEqual([money.zero, money.half, money.small, money.long, money.big], ['$0.00', '$0.50', '$0.00004', '$0.1234567', '$1,234,567.50']);
      assert.ok(money.unknown.every(value => value === 'Unavailable'), 'null, missing and invalid amounts are Unavailable, never $0');
      assert.deepEqual(money.overview.slice(0, 2), ['Unavailable', 'Unavailable']); assert.equal(money.overview[2], '$0.00');
      assert.deepEqual(errors, []); await context.close();
    }

    // -- this month: compact rows, unknown versus recorded zero, subtotal --------------------------------------
    {
      const {page, context, errors} = await open(browser);
      assert.equal(await section(page).getByRole('heading', {name: 'Context usage'}).count(), 1);
      assert.doesNotMatch(await section(page).innerText(), /Model calls recorded/);
      assert.deepEqual(calls.usage[0], {start_date: '2026-10-01', end_date: '2026-10-31', limit: 10}, 'inclusive UTC month, fixed page limit, no cursor');
      assert.equal(await page.locator('[data-filing-call]').count(), 10);
      const month = callsOf('main', {start_date: '2026-10-01', end_date: '2026-10-31', limit: 10});
      // A recorded zero is a zero, a sub-cent charge keeps its digits, an unknown charge is Unknown.
      const zero = month.find(call => call.usage.cost_usd === '0'), small = month.find(call => call.usage.cost_usd === '0.00004');
      const exact = month.find(call => call.usage.cost_usd === '0.1234567'), unknown = month.find(call => call.usage.cost_usd === null);
      assert.match(await rowOf(page, zero.call_id).locator('.filing-cost-amount').innerText(), /^\$0\.00(\s+Partial)?$/);
      assert.match(await rowOf(page, small.call_id).locator('.filing-cost-amount').innerText(), /^\$0\.00004/);
      assert.match(await rowOf(page, exact.call_id).locator('.filing-cost-amount').innerText(), /^\$0\.1234567/);
      assert.match(await rowOf(page, unknown.call_id).locator('.filing-cost-amount').innerText(), /^Unavailable$/);
      // Configured identity never fills in the observed model; a verified observed model is shown as observed.
      const noObserved = rowOf(page, zero.call_id), verified = rowOf(page, exact.call_id);
      assert.equal((await noObserved.locator('[data-label=Model]').innerText()).trim(), 'Unavailable');
      assert.equal((await verified.locator('[data-label=Model]').innerText()).trim(), 'gpt-6-observed');
      assert.equal(await noObserved.innerText().then(text => text.includes(zero.configured.model)), false);
      // Only date / observed model / cost: no identifiers, tokens, attempts, scope, state or expanders.
      const rowText = await verified.innerText();
      for (const hidden of [exact.call_id, exact.input_id, exact.attempt_id, exact.context_event_id, 'Attempt', 'Event ', 'Input ', 'In 7', 'Configured', 'Native'])
        assert.ok(!rowText.includes(hidden), 'row hides ' + hidden);
      assert.equal(await page.locator('.filing-cost-toggle, .filing-cost-detail, #filing-cost-coverage').count(), 0, 'no expanders, details or scan inventory');
      assert.equal(await page.locator('.filing-cost-row.filing-cost-head [role=columnheader]').count(), 3);
      // The subtotal is the backend aggregate and no row claims final or complete usage.
      const aggregates = usage.main[0].response.aggregates;
      assert.equal(aggregates.known_cost_usd, '0.2469934');
      assert.match(await page.locator('#filing-cost-total .filing-cost-subtotal').innerText(), /Recorded cost\s+\$0\.2469934\s+This month/);
      assert.match(await page.locator('#filing-cost-total').innerText(), /Some costs unavailable/);
      assert.doesNotMatch(await page.locator('#filing-cost-total').innerText(), /recorded calls?|with no recorded charge|without token usage|not finished/);
      const all = await section(page).innerText();
      assert.equal(/\b(Final|Complete usage|Luna|Jev|upload)\b/i.test(all.replace(/not proven final/g, '')), false, 'no final/complete badge and no retired Luna/Jev/upload wording');
      assert.equal(/Final totals are not proven/.test(all), false, 'the evidence note stays in the detail');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true, 'no page-level horizontal spill (desktop)');
      if (evidence) { fs.mkdirSync(evidence, {recursive: true}); await shot(page, 'context-usage-1280.png'); }
      assert.deepEqual(errors, []); await context.close();
    }

    // -- all time: pages, stable call IDs, aggregate independent of the loaded pages, failed continuation ------------------------
    {
      const {page, context, errors} = await open(browser, {period: 'all'});
      const pages = fixture('usage-all');
      assert.equal(pages.length, 2);
      assert.deepEqual(calls.usage.at(-1), {limit: 10}, 'all time sends no dates');
      assert.equal(await page.locator('[data-filing-call]').count(), 10);
      const subtotal = pages[0].response.aggregates.known_cost_usd;
      assert.equal(subtotal, '1.7469934');
      assert.match(await page.locator('#filing-cost-total .filing-cost-subtotal').innerText(), /Recorded cost\s+\$1\.7469934\s+All time/, 'the subtotal covers the whole period, not the 10 loaded rows');
      // A failed continuation keeps the shown rows and retries the same cursor.
      control.failNext = true;
      await page.getByRole('button', {name: 'Load more'}).click();
      await page.locator('#filing-cost-status [role=alert]').getByText("Couldn't load. Retry").waitFor();
      assert.ok(!(await page.locator('#filing-cost-status').innerText()).includes('The ledger is busy'), 'no raw backend error');
      assert.equal(await page.locator('[data-filing-call]').count(), 10, 'rows stay after a failed continuation');
      assert.match(await page.locator('#filing-cost-total').innerText(), /\$1\.7469934/);
      const failedCursor = calls.usage.at(-1).cursor; assert.equal(failedCursor, pages[0].response.next_cursor);
      control.repeatOnSecondPage = true;
      await page.getByRole('button', {name: 'Retry'}).click();
      await page.waitForFunction(() => document.querySelectorAll('[data-filing-call]').length === 12);
      assert.deepEqual(calls.usage.at(-1), {limit: 10, cursor: failedCursor}, 'retry uses the same cursor and unchanged filters');
      // The repeated call replaced its earlier row (not added) and the aggregate is the last accepted response's.
      assert.equal(await page.locator('[data-filing-call]').count(), 12);
      const repeated = pages[0].response.calls[0].call_id;
      assert.equal(await rowOf(page, repeated).count(), 1);
      assert.match(await rowOf(page, repeated).locator('.filing-cost-amount').innerText(), /^\$0\.5/);
      assert.match(await page.locator('#filing-cost-total .filing-cost-subtotal').innerText(), /\$1\.7469934/);
      assert.equal(await page.getByRole('button', {name: 'Load more'}).isVisible(), false);
      assert.deepEqual(errors, []); await context.close();
    }

    // -- period change and late responses ---------------------------------------------------------------------------------------
    {
      const {page, context, errors} = await open(browser);
      const monthRows = await page.locator('[data-filing-call]').count();
      // A slow "all time" response that lands after the period was switched back never paints.
      const hold = deferred(); control.hold = hold;
      await page.getByLabel('Period').selectOption('all');
      await page.waitForFunction(() => document.querySelector('#filing-cost-table')?.textContent.includes('Loading'));
      assert.equal(await page.locator('[data-filing-call]').count(), 0, 'a period change resets the rows while loading');
      await page.getByLabel('Period').selectOption('month');
      await page.waitForFunction(count => document.querySelectorAll('[data-filing-call]').length === count, monthRows);
      hold.release();
      await page.waitForTimeout(300);
      assert.equal(await page.locator('[data-filing-call]').count(), monthRows, 'the earlier selection did not overwrite the current view');
      assert.match(await page.locator('#filing-cost-total .filing-cost-subtotal').innerText(), /\$0\.2469934/);
      assert.equal(await page.locator('#filing-cost-more').isVisible(), false);
      // Leaving Costs while a response is pending: nothing paints into the retired view.
      const leave = deferred(); control.hold = leave;
      await page.getByLabel('Period').selectOption('all');
      await page.evaluate(() => { state.globalTab = 'agents'; });
      const before = await page.locator('#costs-filing').innerHTML();
      leave.release(); await page.waitForTimeout(300);
      assert.equal(await page.locator('#costs-filing').innerHTML(), before, 'a response after leaving Costs never paints');
      // A remount shows only its own selection.
      await page.evaluate(() => { state.globalTab = 'costs'; });
      const stale = deferred(); control.hold = stale;
      await page.evaluate(() => { mountCosts(); });
      await page.locator('#costs-summary .costs-total').waitFor();
      await page.evaluate(() => { mountCosts(); });
      await page.waitForFunction(() => document.querySelectorAll('[data-filing-call]').length === 10);
      stale.release(); await page.waitForTimeout(300);
      assert.equal(await page.locator('[data-filing-call]').count(), 10);
      assert.deepEqual(errors, []); await context.close();
    }

    // -- the selected dates are frozen across pages, retries and a month boundary -----------------------------------------------------
    {
      const october = {start_date: '2026-10-01', end_date: '2026-10-31', limit: 10}, november = {start_date: '2026-11-01', end_date: '2026-11-30', limit: 10};
      const {page, context, errors} = await open(browser, {scenario: 'paged', now: '2026-10-31T23:58:00Z'});
      assert.deepEqual(calls.usage[0], october);
      assert.equal(await page.locator('[data-filing-call]').count(), 10);
      const cursor = usage.paged[0].response.next_cursor;
      assert.ok(cursor, 'the recorded October month has a second page');
      await page.clock.setSystemTime(new Date('2026-11-01T00:30:00Z'));       // the UTC month turns before the owner continues
      assert.equal(await page.evaluate(() => filingCostRange('month').start_date), '2026-11-01', 'a fresh selection would now be November');
      control.failNext = true;
      await page.getByRole('button', {name: 'Load more'}).click();
      await page.locator('#filing-cost-status [role=alert]').waitFor();
      assert.deepEqual(calls.usage.at(-1), {...october, cursor}, 'the continuation keeps the October dates with its cursor');
      await page.getByRole('button', {name: 'Retry'}).click();
      await page.waitForFunction(() => document.querySelectorAll('[data-filing-call]').length === 12);
      assert.deepEqual(calls.usage.at(-1), {...october, cursor}, 'the retry keeps the same dates and cursor');
      assert.equal(calls.mismatches.length, 0, 'every request equals a recorded request');
      // A deliberate new selection recomputes the dates: All time, then This month again is November.
      await page.getByLabel('Period').selectOption('all');
      await page.waitForFunction(() => document.querySelector('#filing-cost-status [role=alert]') || document.querySelector('#filing-cost-total .filing-cost-subtotal'));
      await page.getByLabel('Period').selectOption('month');
      await page.waitForFunction(() => document.querySelector('#filing-cost-table .empty'));
      assert.deepEqual(calls.usage.at(-1), november, 'a new selection uses the current UTC month');
      assert.match(await page.locator('#filing-cost-table').innerText(), /No recorded costs for this period/);
      assert.equal(calls.mismatches.length, 0);
      assert.deepEqual(errors, []); await context.close();
    }
    {
      // A failed first page retries its original selected dates, not a recomputed month.
      const {page, context, errors} = await open(browser, {failFirst: true, now: '2026-10-31T23:59:00Z'});
      await page.locator('#filing-cost-status [role=alert]').getByText("Couldn't load. Retry").waitFor();
      const original = calls.usage.at(-1);
      assert.deepEqual(original, {start_date: '2026-10-01', end_date: '2026-10-31', limit: 10});
      await page.clock.setSystemTime(new Date('2026-11-01T00:05:00Z'));
      await page.getByRole('button', {name: 'Retry'}).click();
      await page.locator('[data-filing-call]').first().waitFor();
      assert.deepEqual(calls.usage.at(-1), original, 'the retry of a failed first page keeps its original dates');
      assert.equal(calls.mismatches.length, 0);
      assert.deepEqual(errors, []); await context.close();
    }

    // -- empty period, unavailable endpoint and the retained sections --------------------------------------------------------------
    {
      const {page, context, errors} = await open(browser, {scenario: 'empty', now: '2026-11-15T15:00:00Z'});
      assert.match(await section(page).innerText(), /No recorded costs for this period/);
      assert.match(await page.locator('#filing-cost-total .filing-cost-subtotal').innerText(), /Recorded cost\s+Unavailable\s+This month/);
      assert.equal(await page.locator('#filing-cost-coverage').count(), 0);
      assert.deepEqual(errors, []); await context.close();
    }
    {
      const {page, context, errors} = await open(browser, {unavailable: true});
      assert.match(await page.locator('#filing-cost-status').innerText(), /Couldn't load\. Retry/);
      assert.doesNotMatch(await page.locator('#filing-cost-status').innerText(), /ledger is not installed/);
      assert.equal(await page.locator('#costs-summary .costs-row').count() > 1, true, 'the cost summary is untouched by a usage error');
      assert.equal(await page.locator('#costs-subscriptions').count(), 0, 'Costs carries no subscriptions section');
      control.unavailable = false; Object.assign(control, {scenario: 'main'});
      await page.getByRole('button', {name: 'Retry'}).click();
      await page.locator('[data-filing-call]').first().waitFor();
      assert.deepEqual(errors, []); await context.close();
    }
    {
      const {page, context} = await open(browser, {scenario: 'unfinished'});
      const unfinishedCall = usage.unfinished[0].response.calls.find(call => call.state === 'started');
      const row = rowOf(page, unfinishedCall.call_id);
      assert.match(await row.innerText(), /\$0\.25/); assert.match(await row.innerText(), /Partial/);
      assert.doesNotMatch(await row.innerText(), /Started, not finished/);
      assert.match(await page.locator('#filing-cost-total').innerText(), /Some costs unavailable/);
      await context.close();
    }
    {
      const {page, context} = await open(browser, {scenario: 'silent', overview: 'silent'});
      assert.equal(await page.locator('#filing-cost-coverage').count(), 0);
      assert.match(await page.locator('#filing-cost-total .filing-cost-subtotal').innerText(), /Recorded cost\s+\$0\.2469134/);
      assert.equal(usage.silent[1].response.aggregates.unknown_cost_calls + usage.silent[1].response.aggregates.unfinished_calls, 0);
      assert.ok(usage.silent[1].response.coverage.ready_events_without_calls > 0);
      assert.equal((await page.locator('#filing-cost-total .filing-cost-caveat').innerText()).trim(), 'Some costs unavailable', 'event evidence without calls still shows the note');
      assert.equal(await page.locator('#costs-summary .costs-incomplete').count(), 1, 'one coalesced note');
      const note = await page.locator('#costs-summary .costs-incomplete').innerText();
      assert.equal(note.trim(), 'Some costs unavailable');
      assert.match(await page.locator('#costs-summary .costs-total').innerText(), /Total spend \(known subtotal\)\s+\$0\.2469\s+\$0\.2469/);
      await context.close();
    }

    // -- the Costs summary and profit and loss -----------------------------------------------------------------------------------
    {
      const {page, context} = await open(browser, {overview: 'history'});
      const filing = page.locator('#costs-summary .costs-row', {hasText: 'Context filing'});
      const cells = filing.locator('[role=cell]');
      assert.match(await cells.nth(1).innerText(), /^Unavailable$/);
      assert.match(await cells.nth(2).innerText(), /^\$1\.5000$/);
      assert.match(await cells.nth(3).innerText(), /Observed charges \(known subtotal\)/);
      assert.equal((await page.locator('#costs-summary .costs-incomplete').innerText()).trim(), 'Some costs unavailable');
      assert.equal(await page.locator('#costs-pnl').count(), 0, 'no profit and loss section');
      await context.close();
    }
    {
      const {page, context} = await open(browser, {overview: 'unavailable'});
      const filing = page.locator('#costs-summary .costs-row', {hasText: 'Context filing'});
      const cells = filing.locator('[role=cell]');
      assert.match(await cells.nth(1).innerText(), /^Unavailable$/);
      assert.match(await cells.nth(3).innerText(), /Unavailable/);
      assert.equal((await page.locator('#costs-summary [role=alert]').innerText()).trim(), 'Some costs unavailable');
      assert.equal((await page.locator('#costs-summary .costs-incomplete').innerText()).trim(), 'Some costs unavailable');
      assert.equal(await page.locator('#costs-summary .costs-row', {hasText: 'Background AI'}).count(), 1, 'Background AI stays');
      // PENDING BACKEND CONTRACT: the recorded costs_overview still lists subscription and expense sources. They are shown as plain
      // rows only if the host sends them; the dashboard no longer links to a removed section for them.
      assert.deepEqual(await page.locator('#costs-summary [data-costs-jump]').evaluateAll(buttons => buttons.map(b => b.dataset.costsJump).sort()), ['costs-background', 'costs-filing']);
      assert.match(await page.locator('#costs-summary .costs-total').innerText(), /Total spend \(known subtotal\)/);
      await context.close();
    }
    {
      const {page, context} = await open(browser, {overview: 'complete'});
      assert.equal(await page.locator('#costs-summary .costs-incomplete').count(), 0, 'no completeness note when nothing is missing');
      assert.equal(/Unavailable/.test(await page.locator('#costs-summary .costs-row', {hasText: 'Context filing'}).innerText()), false);
      await context.close();
    }
    {
      const {page, context} = await open(browser, {overview: 'main'});
      const none = await page.evaluate(() => costsMoney(null));
      assert.equal(none, 'Unavailable');
      const row = page.locator('#costs-summary .costs-row', {hasText: 'Context filing'}).locator('[role=cell]');
      assert.match(await row.nth(1).innerText(), /^\$0\.2470$/); assert.match(await row.nth(2).innerText(), /^\$1\.7470$/);
      assert.equal((await page.locator('#costs-summary .costs-incomplete').innerText()).trim(), 'Some costs unavailable');
      assert.doesNotMatch(await page.locator('#costs-summary').innerText(), /calls? (has|have) no recorded charge|unknown cost|unfinished/);
      await context.close();
    }

    // -- narrow content width and keyboard --------------------------------------------------------------------------------------
    {
      const {page, context, errors} = await open(browser, {width: 390});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true, 'no page-level horizontal spill (390px)');
      const box = await page.locator('.filing-cost-table').boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= 390 + 1, 'the call list fits the narrow content width');
      const first = page.locator('[data-filing-call]').first();
      assert.equal(await first.locator('.filing-cost-amount').evaluate(el => getComputedStyle(el, '::before').content), '"Recorded cost"', 'narrow cards label each value');
      // Keyboard: the period control and Load more are reachable and operable without a pointer.
      await page.getByLabel('Period').focus();
      await page.keyboard.press('ArrowDown');
      await page.waitForFunction(() => document.querySelector('#filing-cost-period').value === 'all');
      await page.waitForFunction(() => document.querySelectorAll('[data-filing-call]').length === 10);
      await page.locator('#filing-cost-more').focus(); await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.querySelectorAll('[data-filing-call]').length === 12);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true);
      if (evidence) {
        await shot(page, 'context-usage-390.png');
        await shot(page, 'costs-summary-390.png', '#costs-summary');
      }
      assert.deepEqual(errors, []); await context.close();
    }
    {
      const {page, context} = await open(browser, {overview: 'history', scenario: 'silent'});
      if (evidence) {
        await shot(page, 'costs-summary-1280.png', '#costs-summary');
      }
      await context.close();
    }
    console.log('Context usage costs: exact decimals, compact rows, aggregates, pages and late responses, summary labels without profit and loss, narrow and keyboard passed.');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
