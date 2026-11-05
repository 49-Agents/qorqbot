#!/usr/bin/env node
// Deterministic/synthetic browser timing check: cold-load requests and bytes, requests-per-60s-idle-window
// per endpoint, and click-to-DOM-settled pane-switch timing for Agents -> Notes -> Signoffs -> Routines ->
// Context, 5 repeats, against the real, isolated backend fixture
// (biz-core/tests/service_browser_fixture.py). Spawn/readiness/teardown follow
// tests/test_broadcast_real_browser.js and tests/test_shared_runtime_browser.js.
//
// This fixture is a disposable, isolated SQLite instance in its own tempfile.TemporaryDirectory
// (see service_browser_fixture.py's `main()`), which also refuses to ever bind the service's default
// port (1049). It never touches the protected businesses root or a real dashboard. Because of that
// isolation, every request -- reads and writes alike -- is let through and recorded unmodified, so later
// assertions (e.g. "idle issues zero list requests after the SSE feed lands") can rely on this script
// reflecting complete, real behavior rather than a hobbled recording.
//
// The report is written to PERF_OUT_DIR (default docs/perf/, created on first run and not kept in source).
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium, request} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {
  PANE_ORDER, PANE_SETTLE, COLD_SETTLE,
  waitForDomSettle, clickPane, timedPaneSwitch,
  paneStats, bucket, byEndpoint, fmtBytes, responseBytes,
} = require('./support/perf_instrumentation.js');

const CORE = process.env.BAZARLYK_CORE_ROOT || path.resolve(__dirname, '../../biz-core');
const DASHBOARD = path.resolve(__dirname, '..');
const REPS = Number(process.env.PERF_PANE_REPS || 5);
const IDLE_MS = Number(process.env.PERF_IDLE_MS || 60000);
const OUT_DIR = path.resolve(process.env.PERF_OUT_DIR || path.join(__dirname, '..', 'docs', 'perf'));
const OUT_BASENAME = process.env.PERF_OUT_BASENAME || 'BROWSER-SYNTHETIC';
// The protected businesses directory of the account running this check: a fixture must never use it.
const PROTECTED_BUSINESSES_ROOT = path.join(os.homedir(), 'businesses');
const REPORT_HEADING = '# Browser synthetic baseline — isolated fixture (`tests/perf_browser_synthetic.js`)';

const log = (...args) => console.error(`[perf-synthetic ${new Date().toISOString()}]`, ...args);

// ---------------------------------------------------------------- fixture lifecycle
// Mirrors tests/test_shared_runtime_browser.js's startFixture(): spawn the real isolated backend,
// read its one-line `{"url": ...}` readiness signal off stdout, and tear it down the same way
// (SIGINT, SIGTERM fallback after 10s).
async function startFixture(extraArgs = []) {
  const pythonBin = process.env.BAZARLYK_TEST_PYTHON || path.join(CORE, '.venv/bin/python');
  const child = spawn(pythonBin, ['tests/service_browser_fixture.py', '--dashboard-root', DASHBOARD, ...extraArgs],
    {cwd: CORE, env: {...process.env, PYTHONPATH: '.'}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  const details = await new Promise((resolve, reject) => {
    let stdout = '';
    const timeout = setTimeout(() => reject(new Error('Fixture startup timed out: ' + stderr)), 180000);
    child.stdout.on('data', chunk => {
      stdout += chunk;
      for (const line of stdout.split('\n')) {
        if (!line.startsWith('{"url":')) continue;
        try { const data = JSON.parse(line); clearTimeout(timeout); resolve(data); } catch (_) { /* partial line */ }
      }
    });
    child.once('exit', code => { clearTimeout(timeout); reject(new Error(`Fixture exited ${code}: ${stderr}`)); });
    child.once('error', error => { clearTimeout(timeout); reject(error); });
  }).catch(error => { child.kill('SIGINT'); throw error; });
  return {
    details,
    async stop() {
      if (child.exitCode !== null) return;
      await new Promise(resolve => {
        const timeout = setTimeout(() => child.kill('SIGTERM'), 10000);
        child.once('exit', () => { clearTimeout(timeout); resolve(); });
        child.kill('SIGINT');
      });
    },
  };
}

// ---------------------------------------------------------------- recording (no guard: see header)
// Deliberately no context.route()/abort here: this is a disposable, isolated fixture, not the live
// dashboard, so every request -- GET or POST, read or write -- is let through unmodified and simply
// observed via the 'response'/'requestfailed' events.
function installRecorder(context, sink) {
  const responded = new WeakSet();
  context.on('response', response => {
    const request = response.request();
    responded.add(request);
    const record = {url: request.url(), method: request.method(), status: response.status(),
      resourceType: request.resourceType(), t: Date.now(), bytes: 0};
    sink.responses.push(record);
    sink.pending.push(responseBytes(response).then(n => { record.bytes = n; }).catch(() => {}));
  });
  context.on('requestfailed', request => {
    // A conditional GET answered with 304 fires `response` (already recorded above, with its
    // real status) and *then* `requestfailed`/net::ERR_ABORTED -- Chromium's network layer
    // reports the revalidation as `loadingFailed canceled=true` even though the page already
    // has its answer (confirmed via a direct CDP probe, not assumed). Recording it again here
    // would double-count a 304 hit as a wasted/aborted read.
    if (responded.has(request)) return;
    sink.failed.push({url: request.url(), method: request.method(), error: request.failure()?.errorText || 'unknown', t: Date.now()});
  });
}

// ---------------------------------------------------------------- /live cost at signoff volume
// Separate from measureScope()'s fixture on purpose: that fixture's minimal signoff count (one
// pending Reddit platform Signoff) is what the already-accepted cold-load/idle/pane-switch numbers
// above were measured against, and inflating it would silently invalidate those. This spins up its
// own disposable --many-signoffs fixture (165 platform_action signoffs, 5 pending, plus the base
// fixture's own pending Reddit signoff = 166 total / 6 pending) and hits /live directly over HTTP,
// with no browser page: this dashboard's dispatch resolves the owner identity itself for any request
// whose Host header matches a configured origin (see bazarlyk/transport/http.py's `_dispatch` and
// OriginPolicy.validate), so no cookie/session/token dance is needed to read this response.
function parseServerTiming(header) {
  const out = {};
  for (const part of (header || '').split(',')) {
    const match = part.trim().match(/^(\w+);dur=([\d.]+)$/);
    if (match) out[match[1]] = Number(match[2]);
  }
  return out;
}

async function measureLiveAtVolume() {
  log('spawning second isolated backend fixture (--many-signoffs) for /live-at-volume measurement');
  const fixture = await startFixture(['--many-signoffs']);
  let api;
  try {
    api = await request.newContext({baseURL: fixture.details.origin});
    const livePath = `/api/biz/${fixture.details.business_slug}/live`;

    const cold = await api.get(livePath);
    assert.equal(cold.status(), 200, `cold /live returned ${cold.status()}: ${await cold.text()}`);
    const coldBody = await cold.json();
    const coldTiming = parseServerTiming(cold.headers()['server-timing']);
    log(`live/cold platform_signoffs=${coldBody.platform_signoffs.length} server-timing=${cold.headers()['server-timing']}`);

    const warm = await api.get(livePath, {params: {since: coldBody.since}});
    assert.equal(warm.status(), 200, `incremental /live returned ${warm.status()}: ${await warm.text()}`);
    const warmBody = await warm.json();
    const warmTiming = parseServerTiming(warm.headers()['server-timing']);
    log(`live/incremental platform_signoffs=${warmBody.platform_signoffs.length} server-timing=${warm.headers()['server-timing']}`);

    return {
      fixture: {
        business_slug: fixture.details.business_slug,
        bulk_platform_signoffs_total: fixture.details.bulk_platform_signoffs_total,
        bulk_platform_signoffs_pending: fixture.details.bulk_platform_signoffs_pending,
      },
      cold: {server_timing: coldTiming, platform_signoffs: coldBody.platform_signoffs.length, signoffs_pending: coldBody.signoffs_pending},
      incremental: {server_timing: warmTiming, platform_signoffs: warmBody.platform_signoffs.length, signoffs_pending: warmBody.signoffs_pending},
    };
  } finally {
    if (api) await api.dispose();
    await fixture.stop();
  }
}

// ---------------------------------------------------------------- the one fixture scope
async function measureScope(browser, details) {
  const context = await browser.newContext();
  const sink = {responses: [], failed: [], pending: []};
  installRecorder(context, sink);
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(String((error && error.message) || error)));

  // ---- cold load
  // Without a URL hash, loadBiz() (js/core.js) falls back to Global scope (selectGlobal('agents'))
  // rather than auto-selecting the fixture's sole business, so navigate to `${origin}/#${slug}`
  // rather than the bare origin. The fixture's own `details.url`
  // already carries the owner token as a query string (`?token=...`); the business hash appends
  // cleanly after it.
  const targetUrl = `${details.url}#${encodeURIComponent(details.business_slug)}`;
  const coldStart = Date.now();
  await page.goto(targetUrl, {waitUntil: 'load'});
  const loadAt = Date.now();
  const coldCapped = await waitForDomSettle(page, COLD_SETTLE);
  const settledAt = Date.now();
  const businessSelected = await page.waitForFunction(() => typeof state !== 'undefined' && !!state.biz, {timeout: 10000})
    .then(() => true).catch(() => false);
  const navTiming = await page.evaluate(() => {
    const entry = performance.getEntriesByType('navigation')[0];
    return entry ? {domContentLoaded_ms: Math.round(entry.domContentLoadedEventEnd), load_ms: Math.round(entry.loadEventEnd)} : null;
  });
  const coldWindow = bucket(sink, coldStart, settledAt);
  const coldLoad = {
    dom_content_loaded_ms: navTiming ? navTiming.domContentLoaded_ms : null,
    load_ms: navTiming ? navTiming.load_ms : (loadAt - coldStart),
    content_settled_ms: settledAt - coldStart,
    content_settle_capped: coldCapped,
    business_selected: businessSelected,
    requests: coldWindow.count,
    bytes: coldWindow.bytes,
    not_modified: coldWindow.notModified,
  };
  log(`cold load dcl=${coldLoad.dom_content_loaded_ms}ms load=${coldLoad.load_ms}ms settled=${coldLoad.content_settled_ms}ms requests=${coldLoad.requests}`);

  // ---- idle window
  log(`idle window ${IDLE_MS}ms`);
  const idleStart = Date.now();
  await page.waitForTimeout(IDLE_MS);
  const idleEnd = Date.now();
  const idleWindow = bucket(sink, idleStart, idleEnd);
  const idle = {duration_ms: idleEnd - idleStart, requests: idleWindow.count, bytes: idleWindow.bytes,
    not_modified: idleWindow.notModified, by_endpoint: byEndpoint(idleWindow.rows)};

  // ---- pane switches
  const paneResults = {};
  const paneStart = Date.now();
  await clickPane(page, 'context'); // unmeasured warm-up off the default Agents tab
  await waitForDomSettle(page, PANE_SETTLE);
  for (let rep = 0; rep < REPS; rep++) {
    for (const pane of PANE_ORDER) {
      const result = await timedPaneSwitch(page, pane);
      (paneResults[pane] = paneResults[pane] || []).push(result);
    }
  }
  const paneEnd = Date.now();
  const paneWindow = bucket(sink, paneStart, paneEnd);
  const paneStatsOut = Object.fromEntries(Object.entries(paneResults).map(([k, v]) => [k, paneStats(v)]));

  await Promise.allSettled(sink.pending);
  await context.close();

  return {
    coldLoad, idle,
    paneSwitches: {aggregate_requests: paneWindow.count, aggregate_bytes: paneWindow.bytes,
      not_modified: paneWindow.notModified, panes: paneStatsOut},
    totals: {requests: sink.responses.length, bytes: sink.responses.reduce((a, r) => a + (r.bytes || 0), 0),
      not_modified: sink.responses.filter(r => r.status === 304).length},
    failedRequests: sink.failed, pageErrors,
  };
}

// ---------------------------------------------------------------- report rendering
function renderSyntheticMarkdown(synthetic) {
  const lines = [];
  const p = (...args) => lines.push(args.join(''));
  const label = `${synthetic.fixture.business_slug} (isolated fixture)`;
  p(REPORT_HEADING);
  p();
  p(`Produced by \`tests/perf_browser_synthetic.js\`. Target: the real, isolated ` +
    `backend fixture spawned from \`biz-core/tests/service_browser_fixture.py\` (default flags: no ` +
    `\`--email\`/\`--learnings\`/\`--global-signoffs\`/\`--personal-tasks\`/\`--tables\`/\`--inbox\`/` +
    `\`--notes-only\`), business scope \`${synthetic.fixture.business_slug}\`. Run window: ` +
    `\`${synthetic.run.start}\` to \`${synthetic.run.end}\`.`);
  p();
  p('No read-only guard is enforced: the fixture is disposable, so every request -- reads and writes ' +
    'alike -- was let through and recorded unmodified. That matters when asserting on exact request ' +
    'counts (e.g. "idle issues zero list requests after the SSE feed lands"): a hobbled recording would ' +
    'make those assertions meaningless.');
  p();
  p('### Cold first load');
  p();
  p('| Scope | DOMContentLoaded | load | content settled* | requests | 304s | bytes |');
  p('|---|---:|---:|---:|---:|---:|---:|');
  const c = synthetic.scope.coldLoad;
  p(`| ${label} | ${c.dom_content_loaded_ms ?? 'n/a'} ms | ${c.load_ms ?? 'n/a'} ms | ${c.content_settled_ms} ms${c.content_settle_capped ? '†' : ''} | ${c.requests} | ${c.not_modified} | ${fmtBytes(c.bytes)} |`);
  p();
  p('\\* supplementary MutationObserver-quiet(500ms) signal; † = settle detector hit its timeout cap.');
  if (!c.business_selected) p(`\n**Warning:** ${label} cold load: \`state.biz\` was never observed set within 10s after load — business selection may not have completed.`);
  p();
  p('### Pane switches (median / mean of 5 clicks, click → DOM-settled)');
  p();
  p('| Scope | Pane | median | mean | min | max | timed out (of 5) |');
  p('|---|---|---:|---:|---:|---:|---:|');
  for (const pane of PANE_ORDER) {
    const st = synthetic.scope.paneSwitches.panes[pane];
    if (!st) continue;
    p(`| ${label} | ${pane} | ${st.median_ms} ms | ${st.mean_ms} ms | ${st.min_ms} ms | ${st.max_ms} ms | ${st.capped_count} |`);
  }
  p();
  p(`Aggregate over the whole pane-switch phase: **${synthetic.scope.paneSwitches.aggregate_requests} requests, ` +
    `${synthetic.scope.paneSwitches.not_modified} of them 304** (an unchanged repeat visit -- ETag revalidation, not a wasted read).`);
  p();
  p('### 60-second idle window (what the tab polls on its own)');
  p();
  p(`Total during ${Math.round(synthetic.scope.idle.duration_ms / 1000)}s idle: **${synthetic.scope.idle.requests} requests` +
    ` (${synthetic.scope.idle.not_modified} of them 304), ${fmtBytes(synthetic.scope.idle.bytes)}**.`);
  p();
  if (synthetic.scope.idle.by_endpoint.length) {
    p('| Endpoint | Requests | Bytes |');
    p('|---|---:|---:|');
    for (const e of synthetic.scope.idle.by_endpoint) p(`| \`${e.endpoint}\` | ${e.count} | ${fmtBytes(e.bytes)} |`);
  } else {
    p('No requests observed during the idle window.');
  }
  p();
  p('### `/live` cost at signoff volume (second, isolated `--many-signoffs` fixture)');
  p();
  const lv = synthetic.liveAtVolume;
  p(`Separate fixture from the tables above, seeded with ${lv.fixture.bulk_platform_signoffs_total} platform_action ` +
    `signoffs (${lv.fixture.bulk_platform_signoffs_pending} pending, the rest rejected) so PR2/PR6's fix -- bounding ` +
    '`/live` cost to the pending set plus rows changed since the last call, never a browse page of the full, ' +
    'mostly-resolved history -- is measured against a volume the default fixture above is deliberately too small to ' +
    'show. `total`/`db` are server-side milliseconds from the `Server-Timing` response header ' +
    '(`bazarlyk/service/web.py`\'s `ServiceBoundary`); `body` is response bytes.');
  p();
  p('| Call | `platform_signoffs` returned | `signoffs_pending` | total;dur | db;dur | units;dur | body;dur |');
  p('|---|---:|---:|---:|---:|---:|---:|');
  for (const [label, result] of [['cold (no `since`)', lv.cold], ['incremental (`since` from cold call)', lv.incremental]]) {
    const t = result.server_timing;
    p(`| ${label} | ${result.platform_signoffs} | ${result.signoffs_pending} | ${t.total ?? 'n/a'} ms | ${t.db ?? 'n/a'} ms | ${t.units ?? 'n/a'} | ${t.body ?? 'n/a'} |`);
  }
  p();
  p(`Both calls returned ${lv.cold.platform_signoffs} and ${lv.incremental.platform_signoffs} items respectively out ` +
    `of ${lv.fixture.bulk_platform_signoffs_total} total signoffs in the business -- bounded by the pending+changed ` +
    'set, not by total signoff count.');
  p();
  p('### What this fixture does and does not cover');
  p();
  for (const note of synthetic.notes) p(`- ${note}`);
  p();
  p(`- ETag hit ratio (PR3/PR4, acceptance item 3's "304 on an unchanged repeat"), whole run: **${synthetic.scope.totals.not_modified} of ${synthetic.scope.totals.requests} requests were 304s** ` +
    '-- a conditional GET Chromium\'s network layer reports as `requestfailed`/`net::ERR_ABORTED` (confirmed via a direct CDP probe: `responseReceived` with the real 304 fires first, ' +
    '`loadingFailed canceled=true` follows) even though the page already has its answer; `installRecorder` now excludes this case from the count below rather than double-counting it as a wasted read.');
  p(`- Requests that genuinely failed/aborted in-flight (expected: pane switches abort the shared \`pageReads\` AbortController mid-flight): ${synthetic.scope.failedRequests.length}.`);
  p(`- Page errors observed: ${synthetic.scope.pageErrors.length}${synthetic.scope.pageErrors.length ? ' — see JSON `synthetic.scope.pageErrors`.' : '.'}`);
  p();
  p('### Isolation from the live dashboard');
  p();
  p(`- Fixture root (\`tempfile.TemporaryDirectory(prefix="bazarlyk-browser-")\` in \`service_browser_fixture.py\`'s \`main()\`): \`${synthetic.fixture.root}\`, confirmed by this script to fall under the OS temp dir and outside both \`${PROTECTED_BUSINESSES_ROOT}\` and this repository.`);
  p('- `service_browser_fixture.py` refuses the live service\'s port outright (`if args.port == 1049: parser.error(...)`) and otherwise binds an OS-assigned ephemeral port on `127.0.0.1` (a fresh `socket.socket()` bound with `--port` defaulting to `0`), so it can never collide with, or be mistaken for, a real running dashboard.');
  p(`- This run used \`${synthetic.fixture.origin}\` (port \`${synthetic.fixture.port}\`).`);
  p('- The fixture process (and its temporary directory, SQLite database, and any file receipts) was torn down after this run; nothing it wrote persists inside this repository.');
  p();
  return lines.join('\n');
}

// ---------------------------------------------------------------- main
(async () => {
  const startedAt = new Date();
  log(`spawning isolated backend fixture (core=${CORE}, dashboard=${DASHBOARD})`);
  const fixture = await startFixture();
  let browser;
  try {
    log(`fixture ready: ${fixture.details.origin} (business ${fixture.details.business_slug})`);

    // Enforce, not just document, that this is a disposable scratch instance: never the real
    // businesses root, never inside this repository.
    const tmpRoot = os.tmpdir();
    assert.ok(fixture.details.root, 'fixture did not report a root path');
    assert.ok(fixture.details.root.startsWith(tmpRoot) || fixture.details.root.startsWith('/tmp'),
      `fixture root ${fixture.details.root} is not under the OS temp dir (${tmpRoot})`);
    assert.ok(!fixture.details.root.startsWith(PROTECTED_BUSINESSES_ROOT),
      'fixture root must never be the real businesses root');
    assert.ok(!fixture.details.root.startsWith(DASHBOARD) && !fixture.details.root.startsWith(CORE),
      'fixture root must never live inside a repository checkout');
    log(`fixture root confirmed disposable and isolated: ${fixture.details.root}`);

    browser = await chromium.launch({
      headless: true, args: ['--no-sandbox'],
      ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {}),
    });
    const scope = await measureScope(browser, fixture.details);
    const liveAtVolume = await measureLiveAtVolume();
    const finishedAt = new Date();

    const notes = [
      'Default fixture flags only (no `--email`, `--learnings`, `--global-signoffs`, `--personal-tasks`, `--tables`, `--inbox`, `--notes-only`): the single business scope is `widget`, seeded with one pending agent-creation Signoff, one pending Reddit platform Signoff, two Notes (including a literal-journal fixture), and a Context graph (two topics, one person, one note) — enough for most of the named panes to render real, non-empty content.',
      'Routines: this fixture creates no *business*-scope routine (only `--global-signoffs` creates Global-scope routine proposals, a different concept, reachable only outside this single-business scope). The Routines pane click-to-settle timing above is still real — it is timing the pane\'s genuine empty state, not a broken pane. This is stated plainly rather than worked around.',
      'Global/root scope was not measured here: a single-business isolated fixture cannot meaningfully reproduce a multi-business fleet view. This script targets *business*-scope metrics: cold load, idle window and all five named panes.',
    ];
    if (scope.pageErrors.length) notes.push(`Page errors observed (recorded, not treated as failures): ${scope.pageErrors.length}.`);

    const syntheticReport = {
      produced_by: 'biz-dashboard/tests/perf_browser_synthetic.js',
      run: {start: startedAt.toISOString(), end: finishedAt.toISOString()},
      config: {pane_reps: REPS, idle_ms: IDLE_MS, pane_settle: PANE_SETTLE, cold_settle: COLD_SETTLE},
      fixture: {
        command: 'biz-core/tests/service_browser_fixture.py (default flags)',
        business_slug: fixture.details.business_slug, business_id: fixture.details.business_id,
        root: fixture.details.root, origin: fixture.details.origin,
        port: new URL(fixture.details.origin).port,
      },
      scope, liveAtVolume, notes,
    };

    fs.mkdirSync(OUT_DIR, {recursive: true});
    const jsonPath = path.join(OUT_DIR, `${OUT_BASENAME}.json`);
    const mdPath = path.join(OUT_DIR, `${OUT_BASENAME}.md`);
    fs.writeFileSync(jsonPath, JSON.stringify({synthetic: syntheticReport}, null, 2));
    fs.writeFileSync(mdPath, renderSyntheticMarkdown(syntheticReport) + '\n');
    log(`wrote ${jsonPath}`);
    log(`wrote ${mdPath}`);

    console.log('\n==== SYNTHETIC SUMMARY ====');
    console.log(`fixture: ${fixture.details.origin} (business ${fixture.details.business_slug})`);
    console.log(`fixture root (disposable, under ${tmpRoot}): ${fixture.details.root}`);
    console.log(`cold load: dcl=${scope.coldLoad.dom_content_loaded_ms}ms load=${scope.coldLoad.load_ms}ms settled=${scope.coldLoad.content_settled_ms}ms requests=${scope.coldLoad.requests} bytes=${fmtBytes(scope.coldLoad.bytes)}`);
    console.log(`idle: ${scope.idle.requests} requests / ${Math.round(scope.idle.duration_ms / 1000)}s, ${fmtBytes(scope.idle.bytes)}`);
    for (const pane of PANE_ORDER) {
      const st = scope.paneSwitches.panes[pane];
      if (st) console.log(`pane ${pane}: median=${st.median_ms}ms mean=${st.mean_ms}ms timed_out=${st.capped_count}/${REPS}`);
    }
    console.log(`page errors: ${scope.pageErrors.length}, failed/aborted requests: ${scope.failedRequests.length}`);
    console.log(`/live at volume (${liveAtVolume.fixture.bulk_platform_signoffs_total} signoffs, ${liveAtVolume.fixture.bulk_platform_signoffs_pending} pending): ` +
      `cold total=${liveAtVolume.cold.server_timing.total}ms items=${liveAtVolume.cold.platform_signoffs}, ` +
      `incremental total=${liveAtVolume.incremental.server_timing.total}ms items=${liveAtVolume.incremental.platform_signoffs}`);
  } finally {
    if (browser) await browser.close();
    await fixture.stop();
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
