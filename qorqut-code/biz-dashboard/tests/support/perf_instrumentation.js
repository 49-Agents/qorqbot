// Browser timing helpers shared by the synthetic performance check (tests/perf_browser_synthetic.js): DOM-settle
// detection, pane switching, response sizing and simple statistics. They observe a disposable fixture only.
'use strict';

// The 300ms-quiet MutationObserver detector used for pane-switch timing.
const PANE_SETTLE = {quiet: 300, timeout: 8000};
// Cold load gets a longer/looser settle: the business-selection fetch chain (loadBiz -> selectBiz ->
// renderTab -> renderAgents -> its own data fetches) runs async *after* window `load` fires, since
// none of index.html's <script> tags are deferred. A short quiet window can fire mid-fetch. This
// settle point is a supplementary "content looks idle" signal only -- the DCL/load numbers
// come from the Performance API, not from this.
const COLD_SETTLE = {quiet: 500, timeout: 15000};
const PANEL_PANES = new Set(['notes', 'signoffs', 'routines']);
const PANE_ORDER = ['agents', 'notes', 'signoffs', 'routines', 'context'];

// ---------------------------------------------------------------- DOM-settle helper
// Injected into the page. Resolves once no DOM mutation has landed for `quiet` ms, or after
// `timeout` ms regardless (returned as `capped`). Deliberately not `networkidle` (this app opens
// long-lived SSE streams that would keep `networkidle` from ever firing) and not a fixed sleep.
async function waitForDomSettle(page, {quiet = PANE_SETTLE.quiet, timeout = PANE_SETTLE.timeout} = {}) {
  return page.evaluate(({quiet, timeout}) => new Promise(resolve => {
    let idleTimer, capTimer, capped = false, done = false;
    const finish = () => {
      if (done) return; done = true;
      try { observer.disconnect(); } catch (e) {}
      clearTimeout(idleTimer); clearTimeout(capTimer);
      resolve(capped);
    };
    const observer = new MutationObserver(() => { clearTimeout(idleTimer); idleTimer = setTimeout(finish, quiet); });
    observer.observe(document.documentElement, {childList: true, subtree: true, attributes: true, characterData: true});
    idleTimer = setTimeout(finish, quiet);
    capTimer = setTimeout(() => { capped = true; finish(); }, timeout);
  }), {quiet, timeout});
}

async function responseBytes(response) {
  const headers = response.headers();
  const cl = headers['content-length'];
  if (cl != null) { const n = Number(cl); if (!Number.isNaN(n)) return n; }
  const ct = headers['content-type'] || '';
  if (ct.includes('text/event-stream')) return 0; // long-lived stream: no meaningful total size
  try {
    const body = await Promise.race([response.body(), new Promise((_, reject) => setTimeout(() => reject(new Error('body timeout')), 3000))]);
    return body.length;
  } catch (e) { return 0; }
}

// ---------------------------------------------------------------- pane interaction helpers
async function panelIsOpen(page) {
  return page.evaluate(() => !!document.querySelector('#taskswrap')?.classList.contains('open'));
}
// The app closes the sliding tasks panel on any click outside #taskswrap (tasks.js's document
// click-outside handler). Reopening it is real, necessary user interaction -- done here unmeasured,
// before timing the section switch, so the numbers reflect render latency, not panel-slide setup.
async function ensurePanelOpen(page) {
  if (await panelIsOpen(page)) return false;
  await page.locator('#thandle').click();
  await waitForDomSettle(page, PANE_SETTLE);
  return true;
}
// Panel panes are real tab clicks; page sections use the goTab controller (there is no rail), so those
// timings are programmatic section switches, not measured user clicks.
async function clickPane(page, pane) {
  if (PANEL_PANES.has(pane)) {
    await ensurePanelOpen(page);
    await page.locator(`#taskspanel .ttab[data-v="${pane}"]`).click();
  } else {
    await page.evaluate(name => goTab(name), pane);
  }
}
async function timedPaneSwitch(page, pane) {
  const t0 = Date.now();
  await clickPane(page, pane);
  const capped = await waitForDomSettle(page, PANE_SETTLE);
  return {ms: Date.now() - t0, capped};
}

// ---------------------------------------------------------------- stats
const median = arr => { const s = [...arr].sort((a, b) => a - b), n = s.length; return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2; };
const mean = arr => arr.reduce((a, b) => a + b, 0) / arr.length;
function paneStats(samples) {
  const ms = samples.map(s => s.ms);
  return {samples: ms, median_ms: Math.round(median(ms)), mean_ms: Math.round(mean(ms)),
    min_ms: Math.min(...ms), max_ms: Math.max(...ms), capped_count: samples.filter(s => s.capped).length};
}
function bucket(sink, start, end) {
  const rows = sink.responses.filter(r => r.t >= start && r.t < end);
  // A 304 is a successful revalidation, recorded here from `response`'s own real status, not
  // from the `requestfailed` misfire the recorder filters out.
  return {count: rows.length, bytes: rows.reduce((a, r) => a + (r.bytes || 0), 0),
    notModified: rows.filter(r => r.status === 304).length, rows};
}
function byEndpoint(rows) {
  const groups = new Map();
  for (const r of rows) {
    let key = r.url;
    try { const u = new URL(r.url); key = u.pathname; } catch (e) {}
    const g = groups.get(key) || {endpoint: key, count: 0, bytes: 0};
    g.count++; g.bytes += r.bytes || 0;
    groups.set(key, g);
  }
  return [...groups.values()].sort((a, b) => b.count - a.count);
}
function fmtBytes(n) {
  if (n == null) return 'n/a';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

module.exports = {
  PANE_ORDER, PANE_SETTLE, COLD_SETTLE, PANEL_PANES,
  waitForDomSettle, panelIsOpen, ensurePanelOpen, clickPane, timedPaneSwitch,
  median, mean, paneStats, bucket, byEndpoint, fmtBytes, responseBytes,
};
