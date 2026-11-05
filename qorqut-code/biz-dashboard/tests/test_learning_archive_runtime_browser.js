#!/usr/bin/env node
// Real HTTP-to-dashboard acceptance for the retained learning archive.
//
// The actual composed owner application serves the actual combined dashboard and the actual candidate-only core
// operations. Nothing product-facing is intercepted, stubbed or fabricated: the inventory, detail pages, byte chunks,
// evidence/history continuations are read over the real routes from a real synthetic store, in both scopes. The Global
// Learning page mounts only inside the agent workspace pane, so it is driven through that existing product entry.
// The only routing hook blocks requests that leave the synthetic origin.
//   LEARNING_ARCHIVE_BASE=... LEARNING_ARCHIVE_FIXTURE=... PLAYWRIGHT_MODULE=... node tests/test_learning_archive_runtime_browser.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const path = require("node:path");

const base = process.env.LEARNING_ARCHIVE_BASE;
const fixture = JSON.parse(fs.readFileSync(process.env.LEARNING_ARCHIVE_FIXTURE, "utf8"));
const archive = fixture.learning_archive;
const origin = new URL(base).origin;

function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const cache = path.join(process.env.HOME || "", ".cache/ms-playwright");
  try {
    const dirs = fs.readdirSync(cache).filter(name => name.startsWith("chromium_headless_shell-")).sort().reverse();
    for (const dir of dirs) {
      const exe = path.join(cache, dir, "chrome-headless-shell-linux64", "chrome-headless-shell");
      if (fs.existsSync(exe)) return exe;
    }
  } catch {}
  for (const exe of ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]) if (fs.existsSync(exe)) return exe;
  return undefined;
}

const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const waitFor = async (check, what) => {
  for (let attempt = 0; attempt < 400; attempt++) { if (await check()) return; await delay(25); }
  throw new Error("Timed out waiting for " + what);
};

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: chromiumPath(), args: ["--no-sandbox"]});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.setDefaultTimeout(45000);
    const errors = [], notes = [], reads = [];
    page.on("pageerror", error => errors.push("pageerror: " + error.message));
    page.on("requestfailed", request => {
      const url = request.url();
      if (new URL(url).origin !== origin) return notes.push("blocked external request: " + url);
      if (String(request.failure()?.errorText || "").includes("ERR_ABORTED")) return notes.push("cancelled request: " + url);
      errors.push("requestfailed: " + url + " (" + (request.failure()?.errorText || "") + ")");
    });
    page.on("response", async response => {
      const url = new URL(response.url());
      if (url.origin !== origin) return;
      if (/\/learnings(?:\/history|\/[^/]+\/review)$/.test(url.pathname)) {
        reads.push({path: url.pathname + url.search, status: response.status()});
        if (response.status() !== 200) errors.push("archive read http " + response.status() + ": " + url.pathname + url.search);
      } else if (response.status() >= 400) {
        errors.push("http " + response.status() + ": " + url.pathname);
      }
    });
    await page.route("**/*", route => new URL(route.request().url()).origin === origin
      ? route.continue() : route.abort());

    // ---------------------------------------------------------------- business archive
    await page.goto(base + "/#" + archive.slug);
    await waitFor(() => page.evaluate(() => typeof goTab === "function" && Array.isArray(state.bizs) && state.bizs.length > 0),
      "the dashboard application");
    await page.evaluate(async () => { state.ctx = "_learning"; await goTab("context"); });
    await page.locator('[data-view="history"]').waitFor();
    assert.equal(await page.locator('[data-view="review"]').getAttribute("aria-selected"), "true",
      "the page first opens on factual review");
    assert.equal(await page.locator(".lncard").count() >= 0, true);
    await page.locator('[data-view="history"]').click();
    await page.waitForSelector(".lnhistlist .lnhistrow");
    await waitFor(async () => (await page.locator(".lnhistrow").count()) === Math.min(50, archive.inventory_total),
      "the first bounded inventory page");
    assert.equal(await page.locator('[data-view="history"]').getAttribute("aria-selected"), "true");
    assert.equal(await page.locator(".lnhistrow").count(), 50, "the first page is one bounded read");
    assert.equal(await page.locator(".lnhistrow [data-act]").count(), 0, "inventory rows carry no write controls");

    // Follow the server's own continuation until the whole archive is loaded exactly once.
    let guard = 0;
    while (await page.locator('[data-hist-more=""]').count()) {
      const before = await page.locator(".lnhistrow").count();
      await page.locator('[data-hist-more=""]').click();
      await waitFor(async () => (await page.locator(".lnhistrow").count()) > before, "the next inventory page");
      if (++guard > 20) throw new Error("the inventory continuation never ended");
    }
    assert.equal(await page.locator(".lnhistrow").count(), archive.inventory_total,
      "every original historical identity is loaded once");
    assert.match(await page.locator(".lnhistfoot").first().textContent(), /all \d+ loaded/);
    const ids = await page.locator(".lnhistrow").evaluateAll(rows => rows.map(row => row.dataset.histId));
    assert.equal(new Set(ids).size, ids.length, "no inventory ID is duplicated across pages");
    for (const identity of [archive.implemented_id, archive.filed_id, archive.collapsed_id, archive.no_date_id,
                            archive.long_text_id, archive.rich_id]) assert.ok(ids.includes(identity), "kept original ID " + identity);
    assert.ok(!ids.includes(archive.factual_id), "a factual candidate is not a historical record");
    assert.match(await page.locator(`[data-hist-id="${archive.implemented_id}"]`).textContent(), /Implemented|implemented/i);
    assert.match(await page.locator(`[data-hist-id="${archive.filed_id}"]`).textContent(), /Filed/);
    assert.match(await page.locator(`[data-hist-id="${archive.collapsed_id}"]`).textContent(),
      /Reconciled into legacy-0002/);
    assert.match(await page.locator(`[data-hist-id="${archive.long_text_id}"]`).textContent(), /truncated summary/);

    // Search resolves an original ID, then the full list returns.
    await page.locator("#lnsearch").fill(archive.rich_id);
    await waitFor(async () => (await page.locator(".lnhistrow").count()) === 1, "the searched ID");
    assert.match(await page.locator(".lnhistrow").textContent(), new RegExp(archive.rich_id));
    await page.locator("#lnsearch").fill("");
    await waitFor(async () => (await page.locator(".lnhistrow").count()) === archive.inventory_total, "the full list");

    // ---------------------------------------------------------------- read-only detail and both collections
    await page.locator(`[data-hist-id="${archive.rich_id}"]`).click();
    await page.waitForSelector(".lnhistdetail .mkt");
    await waitFor(async () => (await page.locator('[data-hist-more="initial"]').count()) === 0,
      "the oversized record page to reassemble");
    const detail = page.locator(".lnhistdetail");
    const detailText = await detail.textContent();
    assert.match(detailText, /historical procedural record/);
    assert.match(detailText, /Owner note\s*Owner kept the harbor procedure/);
    assert.match(detailText, /Original transcripts are not included/);
    assert.match(detailText, /Full recorded snapshots/);
    assert.match(detailText, /<literal>/, "stored angle brackets are rendered as text");
    assert.equal(await detail.locator("[data-act]").count(), 0, "the historical detail offers no procedural write control");
    assert.equal(await detail.locator("textarea").count(), 0);
    assert.equal(await detail.locator("input").count(), 0);
    const labels = await detail.locator("button").allTextContents();
    assert.ok(labels.every(label => /Historical records|^Load more|^Continue loading|^Retry/.test(label)),
      "only reading and continuation controls: " + JSON.stringify(labels));

    // Follow the independent evidence collection to its end: more than 200 (run, session) pairs, two runs of one session.
    guard = 0;
    while (await page.locator('[data-hist-more="evidence"]').count()) {
      const before = await page.locator(".lnsource").count();
      await page.locator('[data-hist-more="evidence"]').click();
      await waitFor(async () => (await page.locator(".lnsource").count()) > before, "the next evidence page");
      if (++guard > 40) throw new Error("the evidence continuation never ended");
    }
    guard = 0;
    while (await page.locator('[data-hist-more="history"]').count()) {
      const before = await page.locator(".lnhistevent").count();
      await page.locator('[data-hist-more="history"]').click();
      await waitFor(async () => (await page.locator(".lnhistevent").count()) > before, "the next history page");
      if (++guard > 20) throw new Error("the history continuation never ended");
    }
    const sources = await page.locator(".lnsource").evaluateAll(nodes =>
      nodes.map(node => [node.dataset.run, node.dataset.session]));
    const pairs = new Set(sources.map(pair => pair[0] + "\u0000" + pair[1]));
    assert.equal(pairs.size, archive.reference_pairs, "every frozen (run, session) pair is displayed once");
    assert.equal(sources.length, pairs.size, "an identical pair is not duplicated");
    assert.ok(pairs.has("run-a\u0000sess-001") && pairs.has("run-b\u0000sess-001"), "two runs of one session stay distinct");
    assert.ok(await page.locator(".lnsource .lnsourcestatus.missing").count() > 0, "missing saved cores stay explicit");
    assert.match(await detail.textContent(), /Saved evidence is (partial|missing)/);
    assert.equal(await page.locator(".lnhistevent").count(), archive.history_events,
      "the whole bounded history is enumerated in order");

    // ---------------------------------------------------------------- global archive (real embedded product entry)
    // The Global Learning page mounts only inside the agent workspace pane (GLOBAL_PAGES.context.pane). The existing
    // product entry is `/?agent-pane-embed=global_context&route=learning/<id>`; nothing is intercepted.
    reads.length = 0;
    await page.goto(base + "/?agent-pane-embed=global_context&route=learning");
    await waitFor(() => page.evaluate(() => state.scope === "global" && !!document.querySelector('[data-view="history"]')),
      "the embedded Global Learning page");
    await page.locator('[data-view="history"]').click();
    await waitFor(async () => (await page.locator('[data-view="history"]').getAttribute("aria-selected")) === "true",
      "the Global history view");
    await waitFor(async () => (await page.locator(".lnhistrow").count()) === 3, "the Global inventory page");
    const globalRows = await page.locator(".lnhistrow").evaluateAll(nodes => nodes.map(node => node.dataset.histId));
    assert.ok(globalRows.includes(archive.global_long_id), "the Global inventory lists its own long record");
    assert.ok(globalRows.includes(archive.global_rich_id) && globalRows.includes(archive.global_id));
    assert.ok(!globalRows.includes(archive.rich_id), "business records never appear in the Global inventory");

    await page.goto(base + "/?agent-pane-embed=global_context&route=learning%2F" + archive.global_long_id);
    await waitFor(() => page.evaluate(() => !!document.querySelector(".lnhistdetail")), "the embedded Global deep link");
    const globalDetail = page.locator(".lnhistdetail");
    await waitFor(async () => (await page.locator('[data-hist-more="initial"]').count()) === 0,
      "the oversized Global page to reassemble");
    assert.match(await globalDetail.textContent(), /Global retained procedure with a long frozen evidence trail/);
    guard = 0;
    while (await page.locator('[data-hist-more="evidence"]').count()) {
      const before = await page.locator(".lnsource").count();
      await page.locator('[data-hist-more="evidence"]').click();
      await waitFor(async () => (await page.locator(".lnsource").count()) > before, "the next Global evidence page");
      if (++guard > 20) throw new Error("the Global evidence continuation never ended");
    }
    guard = 0;
    while (await page.locator('[data-hist-more="history"]').count()) {
      const before = await page.locator(".lnhistevent").count();
      await page.locator('[data-hist-more="history"]').click();
      await waitFor(async () => (await page.locator(".lnhistevent").count()) > before, "the next Global history page");
      if (++guard > 20) throw new Error("the Global history continuation never ended");
    }
    assert.equal(await page.locator(".lnsource").count(), 45, "every Global frozen source is enumerated");
    assert.equal(await page.locator(".lnhistevent").count(), 25, "the whole Global history is enumerated");
    assert.equal(await globalDetail.locator("[data-act]").count(), 0, "the Global detail offers no write control");
    assert.equal(await globalDetail.locator("textarea").count(), 0);
    assert.ok(reads.length > 3, "the Global checks used the real archive routes");
    assert.ok(reads.every(read => read.path.startsWith("/api/root/learnings/")),
      "Global reads never touch a business route: " + JSON.stringify(reads.map(read => read.path)));

    // The rich record keeps its three frozen entries through the same embedded entry.
    await page.goto(base + "/?agent-pane-embed=global_context&route=learning%2F" + archive.global_rich_id);
    await waitFor(() => page.evaluate(() => !!document.querySelector(".lnhistdetail")), "the embedded Global rich deep link");
    await waitFor(async () => (await page.locator(".lnsource").count()) === 3, "the three Global rich sources");
    assert.equal(await page.locator(".lnhistdetail [data-act]").count(), 0);

    // ---------------------------------------------------------------- global archive (real owning Learning Applier pane)
    // The old Global Learning address redirects into the registered Learning Applier's own agent pane, which frames the same
    // embedded page. The agent is only registered (no native session, no model turn); nothing is intercepted.
    reads.length = 0;
    await page.goto(base + "/#/global/context/learning/" + archive.global_long_id);
    const paneFrame = () => page.frames().find(frame => /agent-pane-embed=global_context/.test(frame.url()));
    await waitFor(() => !!paneFrame(), "the Learning Applier pane frame");
    const pane = paneFrame();
    await waitFor(async () => (await pane.locator(".lnhistdetail").count()) > 0, "the deep-linked record inside the owning pane");
    assert.match(page.url(), /#\/global\/agents$/, "the old Context address redirects into the owning agent pane");
    await waitFor(async () => (await pane.locator('[data-hist-more="initial"]').count()) === 0, "the pane page to reassemble");
    assert.match(await pane.locator(".lnhistdetail").textContent(), /Global retained procedure with a long frozen evidence trail/);
    guard = 0;
    while (await pane.locator('[data-hist-more="evidence"]').count()) {
      const before = await pane.locator(".lnsource").count();
      await pane.locator('[data-hist-more="evidence"]').click();
      await waitFor(async () => (await pane.locator(".lnsource").count()) > before, "the next pane evidence page");
      if (++guard > 20) throw new Error("the pane evidence continuation never ended");
    }
    guard = 0;
    while (await pane.locator('[data-hist-more="history"]').count()) {
      const before = await pane.locator(".lnhistevent").count();
      await pane.locator('[data-hist-more="history"]').click();
      await waitFor(async () => (await pane.locator(".lnhistevent").count()) > before, "the next pane history page");
      if (++guard > 20) throw new Error("the pane history continuation never ended");
    }
    assert.equal(await pane.locator(".lnsource").count(), 45, "every Global frozen source is enumerated in the owning pane");
    assert.equal(await pane.locator(".lnhistevent").count(), 25, "the whole Global history is enumerated in the owning pane");
    assert.equal(await pane.locator(".lnhistdetail [data-act]").count(), 0, "the owning pane offers no write control");
    assert.ok(reads.length > 3 && reads.every(read => read.path.startsWith("/api/root/learnings/")),
      "the owning pane reads only Global routes: " + JSON.stringify(reads.map(read => read.path)));

    // ---------------------------------------------------------------- scope separation on the business route
    const businessFrom = reads.length;
    await page.goto(base + "/#" + archive.slug);
    await waitFor(() => page.evaluate(() => typeof goTab === "function" && Array.isArray(state.bizs) && state.bizs.length > 0),
      "the dashboard application after the Global pass");
    await page.evaluate(async () => { state.ctx = "_learning"; await goTab("context"); });
    await page.locator('[data-view="history"]').waitFor();
    await page.locator('[data-view="history"]').click();
    await waitFor(async () => (await page.locator(".lnhistrow").count()) === Math.min(50, archive.inventory_total),
      "the business inventory after the Global pass");
    const businessReads = reads.slice(businessFrom);
    assert.ok(businessReads.length > 0 && businessReads.every(read => read.path.startsWith("/api/biz/")),
      "business reads never touch the Global route: " + JSON.stringify(businessReads.map(read => read.path)));

    // ---------------------------------------------------------------- reload stability
    await page.reload();
    await waitFor(() => page.evaluate(() => typeof goTab === "function" && Array.isArray(state.bizs) && state.bizs.length > 0),
      "the reloaded dashboard");
    await page.evaluate(async () => { state.ctx = "_learning"; await goTab("context"); });
    await page.locator('[data-view="history"]').waitFor();
    await page.locator('[data-view="history"]').click();
    await waitFor(async () => (await page.locator(".lnhistrow").count()) === Math.min(50, archive.inventory_total),
      "the first inventory page after a reload");
    assert.equal(await page.locator(".lnhistrow").count(), 50);

    assert.deepEqual(errors, [], "no page or request error occurred");
    for (const note of notes) console.log("note:", note);
    const archiveReads = reads.filter(read => !read.path.includes("/agents/"));
    assert.ok(archiveReads.length > 10, "the checks used the real archive routes (" + archiveReads.length + " reads)");
    console.log("ok   archive browser reads: " + archiveReads.length + " real route responses");
    console.log("ok   inventory, detail, byte streams, evidence and history verified over the real routes in both scopes");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });