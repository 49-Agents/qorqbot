#!/usr/bin/env node
// Real Chromium DOM acceptance for the historical archive UI, with every service response intercepted and
// bounded by this fixture. No listener, no host, no core domain: the dashboard files are served from disk to
// a fake origin, so the real tabs, buttons, CSS and DOM are exercised against explicit operation pages.
//   PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/test_learning_history_browser.js
// Set CHROMIUM_PATH when Playwright's own browser revision is unavailable (any installed Chromium works).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const HERE = path.join(__dirname, "..");
const ORIGIN = "http://learning.test";

function loadPlaywright() {
  const candidates = [process.env.PLAYWRIGHT_MODULE, "playwright"];
  const npx = path.join(process.env.HOME || "", ".npm/_npx");
  try {
    for (const entry of fs.readdirSync(npx)) candidates.push(path.join(npx, entry, "node_modules/playwright"));
  } catch {}
  for (const candidate of candidates) {
    if (!candidate) continue;
    try { return require(candidate); } catch {}
  }
  console.error("Playwright is required for the real-browser check. Set PLAYWRIGHT_MODULE to its node_modules/playwright directory.");
  process.exit(2);
}
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

const { chromium } = loadPlaywright();

// ------------------------------------------------------------- fixture pages
const INVENTORY = Array.from({ length: 120 }, (_, index) => ({
  learning_id: index === 4 ? "legacy-id-77" : `L${index + 1}`, text: index === 4 ? "Retained rule five" : `Retained record ${index + 1}`,
  tag: index % 3 === 0 ? "rule" : "fact", bucket: "delta", scope: "biz", created: "2026-09-01T00:00:00Z",
  state: index % 3 === 0 ? "implemented" : "accepted", work_state: index % 3 === 0 ? "implemented" : null,
  decision: { state: "accepted", note: index === 4 ? "Owner kept this" : "" },
  filed: index === 7, reconciled_into: index === 9 ? "L10" : null,
}));
const DETAIL = {
  item: { id: "legacy-id-77", text: "Retained rule five", tag: "rule", bucket: "delta", scope: "biz", revision: 2,
    state: "accepted", created: "2026-09-01T00:00:00Z", decision: { state: "accepted", note: "Owner kept this" },
    targets: [{ repository: "fixture", paths: ["app/x.py"], commit: "a".repeat(40) }], outcome: { note: "covered" },
    filed: false, reconciled_into: null },
  sources: [{ run: "run-1", session: "sess-1", status: "available", core: { markdown: "Frozen core one <literal>" } }],
  history: [{ id: "e1", event: "reviewed", actor_id: "human:owner", created_at: "2026-09-02T00:00:00Z",
    before_json: { revision: 1, state: "open", work_state: null, decision: { state: "accepted", note: "kept" },
      scope: "biz", bucket: "delta", tag: "rule", text: "fixture snapshot" },
    after_json: { revision: 2, state: "accepted" } }],
  page_collections: ["evidence", "history"], source_kind: "frozen_extraction_cores",
  original_transcripts_included: false, evidence_complete: true, references_complete: true,
  history_complete: true, evidence_status: "available", read_only: true,
  next_evidence_cursor: "ev2", next_history_cursor: "hi2",
};
const EVIDENCE_PAGE = { item: DETAIL.item, page_collections: ["evidence"],
  sources: [{ run: "run-2", session: "sess-2", status: "missing", core: null }],
  evidence_complete: true, references_complete: true, next_evidence_cursor: null, evidence_status: "partial" };
const HISTORY_PAGE = { item: DETAIL.item, page_collections: ["history"],
  history: [{ id: "e2", event: "implemented", actor_id: "agent:x", created_at: "2026-09-03T00:00:00Z",
    before_json: { state: "accepted" }, after_json: { state: "implemented" } }],
  history_complete: true, next_history_cursor: null };

function harness(state) {
  const global = !!state.global;
  return `<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/learning.css"></head>
<body><div id="ctxview"></div>
<script>
window.state = {scope: ${global ? '"global"' : '"business"'}, biz: ${global ? "null" : '"project-a"'},
  ctx: "_learning", globalTab: "context"};
if (${global}) location.hash = "#/global/context/learning/G5";
window.COLORS = {blue: "#0A84FF", plum: "#BF5AF2", red: "#FF453A", amber: "#FFD60A", teal: "#40C8E0"};
window.AICONS = {bot: "<circle cx='12' cy='12' r='9'/>", globe: "<circle cx='12' cy='12' r='9'/>"};
window._ctxData = {people: []};
window.ls = (key, value) => value === undefined ? localStorage.getItem(key) : localStorage.setItem(key, String(value));
window.esc = value => String(value == null ? "" : value).replace(/[&<>"']/g,
  c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
window.$ = selector => document.querySelector(selector);
window.notify = () => {};
window.toast = () => {};
window.api = async (requestPath, options) => {
  const response = await fetch("/api" + requestPath, options);
  const body = await response.json();
  if (body && body.error && typeof body.error === "object") {
    body.coreError = body.error;
    body.error = body.error.message || "Request failed";
  }
  body.httpStatus = response.status;
  return body;
};
</script>
<script src="/js/learning.js"></script>
<script>window.ready = openLearning();</script>
</body></html>`;
}

// ------------------------------------------------------------- intercepted transport
function fixtureRouter(page, state) {
  page.route(ORIGIN + "/**", async route => {
    const url = new URL(route.request().url());
    const file = name => route.fulfill({ contentType: name.endsWith(".css") ? "text/css" : "application/javascript",
      body: fs.readFileSync(path.join(HERE, name), "utf8") });
    if (url.pathname === "/harness.html") return route.fulfill({ contentType: "text/html", body: harness(state) });
    if (url.pathname.startsWith("/js/")) return file(url.pathname.slice(1));
    if (url.pathname.startsWith("/css/")) return file(url.pathname.slice(1));
    if (url.pathname.startsWith("/api/")) {
      state.requests.push(url.pathname.slice(4) + url.search);
      const result = await state.responder(url, state);
      if (result === "abort") return route.abort("failed");
      return route.fulfill({ status: result.status || 200, contentType: "application/json", body: JSON.stringify(result.body) });
    }
    return route.fulfill({ status: 404, body: "" });
  });
}

async function newPage(browser, state) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await fixtureRouter(page, state);
  await page.goto(ORIGIN + "/harness.html");
  await page.waitForFunction(() => typeof openLearning === "function" && !!document.querySelector("#ctxview .th"));
  return { page, errors };
}

function defaultResponder(url) {
  if (url.pathname.endsWith("/learnings")) return { body: { items: [], counts: {} } };
  return { status: 404, body: { error: { message: "unexpected " + url.pathname } } };
}

// ------------------------------------------------------------- checks
async function main() {
  const browser = await chromium.launch({ headless: true, executablePath: chromiumPath(), args: ["--no-sandbox"] });
  try {
    // ---------------------------------------------------------------- list, search, tabs
    const state = { requests: [], responder: defaultResponder };
    state.responder = async url => {
      if (url.pathname.endsWith("/learnings")) return { body: { items: [], counts: {} } };
      if (url.pathname.endsWith("/history")) {
        const offset = Number(url.searchParams.get("cursor") || 0);
        return { body: { items: INVENTORY.slice(offset, offset + 50), count: INVENTORY.length,
          next_cursor: offset + 50 < INVENTORY.length ? String(offset + 50) : null, read_only: true } };
      }
      if (url.pathname.endsWith("/legacy-id-77/review")) {
        if (url.searchParams.get("evidence_cursor")) return { body: EVIDENCE_PAGE };
        if (url.searchParams.get("history_cursor")) return { body: HISTORY_PAGE };
        return { body: DETAIL };
      }
      return { status: 404, body: { error: { message: "unexpected " + url.pathname } } };
    };
    let { page, errors } = await newPage(browser, state);
    assert.equal(await page.locator('[data-view="review"]').getAttribute("aria-selected"), "true");
    await page.locator('[data-view="history"]').click();
    await page.waitForSelector(".lnhistlist .lnhistrow");
    assert.equal(await page.locator('[data-view="history"]').getAttribute("aria-selected"), "true");
    assert.equal(await page.locator(".lnhistlist .lnhistrow").count(), 50);
    await page.locator('[data-hist-more]').click();
    await page.waitForFunction(() => document.querySelectorAll(".lnhistrow").length === 100);
    await page.locator('[data-hist-more]').click();
    await page.waitForFunction(() => document.querySelectorAll(".lnhistrow").length === 120);
    assert.equal(await page.locator("[data-hist-more]").count(), 0);
    assert.match(await page.locator(".lnhistfoot").textContent(), /all 120 loaded/);
    assert.equal(await page.locator(".lncard").count(), 0, "historical records are not factual review cards");
    assert.equal(await page.locator(".lnhistrow [data-act]").count(), 0, "inventory has no mutation controls");

    await page.locator("#lnsearch").fill("legacy-id-77");
    await page.waitForFunction(() => document.querySelectorAll(".lnhistrow").length === 1);
    assert.match(await page.locator(".lnhistrow").textContent(), /Retained rule five/);
    await page.locator("#lnsearch").fill("");

    // ---------------------------------------------------------------- read-only detail, metadata, snapshots
    await page.locator(".lnhistrow", { hasText: "Retained rule five" }).click();
    await page.waitForSelector(".lnhistdetail .mkt");
    const detailText = await page.locator(".lnhistdetail").textContent();
    assert.match(detailText, /historical procedural record/);
    assert.match(detailText, /Owner note\s*Owner kept this/);
    assert.match(detailText, /Original transcripts are not included/);
    assert.match(detailText, /revision: 1/);
    assert.match(detailText, /bucket: delta/);
    assert.match(detailText, /Full recorded snapshots/);
    assert.match(detailText, /fixture snapshot/);
    assert.match(await page.locator(".lnhistdetail pre").first().textContent(), /Frozen core one <literal>/);
    const buttonLabels = await page.locator(".lnhistdetail button").allTextContents();
    assert(buttonLabels.every(label => /Historical records|Load more|Continue|Retry/.test(label)),
      "detail offers only reading/continuation controls: " + JSON.stringify(buttonLabels));
    assert.equal(await page.locator(".lnhistdetail [data-act]").count(), 0);
    assert.equal(await page.locator(".lnhistdetail textarea").count(), 0);
    assert.equal(await page.locator(".lnhistdetail .lnscope").count(), 0);

    // independent collection pages merge without reset and keep the page metadata
    await page.getByRole("button", { name: "Load more evidence" }).click();
    await page.waitForFunction(() => document.querySelectorAll(".lnsource").length === 2);
    await page.getByRole("button", { name: "Load more history" }).click();
    await page.waitForFunction(() => document.querySelectorAll(".lnhistevent").length === 2);
    assert.equal(await page.locator(".lnsource").count(), 2, "loading history kept the evidence sources");
    assert.match(await page.locator(".lnhistsection").first().textContent(), /Original transcripts are not included/,
      "metadata survives an independent evidence page that omits it");
    assert.equal(await page.locator(".lnhistdetail [data-act]").count(), 0);
    const historicalIds = new Set(INVENTORY.map(item => item.learning_id));
    assert(state.requests.every(request => {
      const detail = request.match(/^\/biz\/project-a\/learnings\/([^/?]+)$/);
      return !detail || !historicalIds.has(detail[1]);
    }), "the project historical detail never uses the old session-keyed cores endpoint");
    assert.equal(errors.length, 0, errors.join("\n"));

    // ---------------------------------------------------------------- inventory failure offers the exact retry
    const invState = { requests: [], responder: defaultResponder };
    let invFail = true;
    invState.responder = async url => {
      if (url.pathname.endsWith("/learnings")) return { body: { items: [], counts: {} } };
      if (url.pathname.endsWith("/history")) {
        if (invFail) { invFail = false; return "abort"; }
        return { body: { items: [{ learning_id: "INV1", text: "inventory retry", tag: "rule", bucket: "delta",
          scope: "biz", state: "accepted" }], count: 1, next_cursor: null, read_only: true } };
      }
      return { status: 404, body: { error: { message: "unexpected " + url.pathname } } };
    };
    const invLoaded = await newPage(browser, invState);
    await invLoaded.page.locator('[data-view="history"]').click();
    await invLoaded.page.waitForFunction(() => document.querySelector(".lnhistfailure")?.textContent.length);
    assert.equal(await invLoaded.page.getByRole("button", { name: "Retry loading records" }).count(), 1);
    await invLoaded.page.getByRole("button", { name: "Retry loading records" }).click();
    await invLoaded.page.waitForSelector(".lnhistrow");
    assert.match(await invLoaded.page.locator(".lnhistrow").textContent(), /inventory retry/);
    assert.equal(invLoaded.errors.length, 0, invLoaded.errors.join("\n"));
    await invLoaded.page.close();

    // ---------------------------------------------------------------- >40 byte chunks: Continue preserves progress
    const bigState = { requests: [], responder: defaultResponder, chunks: 0, cursors: [], aborts: 0 };
    let bigPage;
    const bigJson = JSON.stringify({ item: { id: "BIG", text: "big record body " + "x".repeat(120_000), tag: "rule",
      bucket: "delta", scope: "biz", revision: 1, created: "2026-09-01T00:00:00Z", state: "accepted" },
      sources: [], history: [], page_collections: ["evidence", "history"], evidence_complete: true,
      references_complete: true, history_complete: true, evidence_status: "missing", read_only: true });
    const bigParts = Array.from({ length: Math.ceil(bigJson.length / 2000) }, (_, i) => bigJson.slice(i * 2000, (i + 1) * 2000));
    bigState.responder = async url => {
      if (url.pathname.endsWith("/learnings")) return { body: { items: [], counts: {} } };
      if (url.pathname.endsWith("/history")) return { body: { items: [{ learning_id: "BIG", text: "big record", tag: "rule", bucket: "delta", scope: "biz", state: "accepted" }],
        count: 1, next_cursor: null, read_only: true } };
      if (url.pathname.endsWith("/BIG/review")) {
        const cursor = url.searchParams.get("content_cursor");
        if (!cursor) return { body: { learning_id: "BIG", revision: 1, complete: false, next_content_cursor: "c0" } };
        const index = Number(cursor.slice(1));
        bigState.chunks += 1;
        if (bigState.chunks === 43 && bigState.aborts === 0) { bigState.aborts += 1; return "abort"; }
        bigState.cursors.push(index);
        return { body: { learning_id: "BIG", revision: 1, content: bigParts[index], encoding: "json",
          complete: index === bigParts.length - 1, next_content_cursor: index === bigParts.length - 1 ? null : `c${index + 1}` } };
      }
      return { status: 404, body: { error: { message: "unexpected " + url.pathname } } };
    };
    ({ page: bigPage, errors } = await newPage(browser, bigState));
    await bigPage.locator('[data-view="history"]').click();
    await bigPage.locator(".lnhistrow").click();
    await bigPage.waitForFunction(() => document.querySelector(".lnhistdetail")?.textContent.includes("Continue loading"));
    assert.equal(await bigPage.locator(".lnhistdetail .mkt").count(), 0, "a partial page is never parsed or shown");
    assert.match(await bigPage.locator(".lnhistdetail").textContent(), /characters are saved/);
    assert.equal(bigState.chunks, 40, "one action reads at most 40 byte chunks");
    await bigPage.getByRole("button", { name: "Continue loading" }).click();
    await bigPage.waitForFunction(() => document.querySelector(".lnhistfailure")?.textContent.length);
    assert.equal(bigState.chunks, 43, "the second action stopped at the interrupted chunk");
    assert.equal(await bigPage.locator(".lnhistdetail .mkt").count(), 0);
    assert.match(await bigPage.locator(".lnhistdetail").textContent(), /characters are saved/);
    await bigPage.getByRole("button", { name: "Retry loading" }).click();
    await bigPage.waitForFunction(() => document.querySelector(".lnhistdetail .mkt")?.textContent.includes("big record body"));
    assert(bigState.chunks >= bigParts.length, "the page is reachable through bounded continuations");
    assert.equal(bigState.cursors.length, bigParts.length);
    assert.equal(new Set(bigState.cursors).size, bigParts.length, "no byte chunk is refetched after the retry");
    assert.equal(errors.length, 0, errors.join("\n"));

    // ---------------------------------------------------------------- transient and access failures
    const failState = { requests: [], responder: defaultResponder };
    let failPage;
    failState.responder = async url => {
      if (url.pathname.endsWith("/learnings")) return { body: { items: [], counts: {} } };
      if (url.pathname.endsWith("/history")) return { body: { items: [{ learning_id: "F", text: "failure record", tag: "rule", bucket: "delta", scope: "biz", state: "accepted" }],
        count: 1, next_cursor: null, read_only: true } };
      if (url.pathname.endsWith("/F/review")) {
        if (url.searchParams.get("evidence_cursor"))
          return { status: 403, body: { error: { code: "forbidden", message: "Access was revoked" } } };
        return failState.first ? { status: 500, body: { error: { code: "internal", message: "temporary read failure" } } }
          : { body: { item: { id: "F", text: "failure record", tag: "rule", bucket: "delta", scope: "biz", revision: 1, state: "accepted" },
            sources: [{ run: "r", session: "s", status: "available", core: { markdown: "saved" } }], history: [],
            page_collections: ["evidence", "history"], evidence_complete: false, references_complete: true,
            history_complete: true, evidence_status: "available", read_only: true, next_evidence_cursor: "ev" } };
      }
      return { status: 404, body: { error: { message: "unexpected " + url.pathname } } };
    };
    ({ page: failPage, errors } = await newPage(browser, failState));
    await failPage.locator('[data-view="history"]').click();
    failState.first = true;
    await failPage.locator(".lnhistrow").click();
    await failPage.waitForFunction(() => document.querySelector(".lnhistfailure")?.textContent.includes("temporary read failure"));
    assert.equal(await failPage.getByRole("button", { name: "Retry" }).count(), 1);
    failState.first = false;
    await failPage.getByRole("button", { name: "Retry" }).click();
    await failPage.waitForSelector(".lnhistdetail .mkt");
    failState.responder = async url => {
      if (url.pathname.endsWith("/history")) return { body: { items: [{ learning_id: "F", text: "failure record", tag: "rule", bucket: "delta", scope: "biz", state: "accepted" }],
        count: 1, next_cursor: null, read_only: true } };
      if (url.searchParams.get("evidence_cursor"))
        return { status: 403, body: { error: { code: "forbidden", message: "Access was revoked" } } };
      return { status: 404, body: { error: { message: "unexpected " + url.pathname } } };
    };
    await failPage.getByRole("button", { name: "Load more evidence" }).click();
    await failPage.waitForFunction(() => document.querySelector(".lnhistfailure")?.textContent.includes("Access was revoked"));
    assert.equal(await failPage.locator(".lnhistdetail .mkt").count(), 0, "access loss withholds the previously loaded record");
    assert.equal(await failPage.locator(".lnsource").count(), 0, "access loss withholds the previously loaded sources");
    assert.equal(errors.length, 0, errors.join("\n"));
    await page.close(); await bigPage.close(); await failPage.close();

    // ---------------------------------------------------------------- Global candidate deep link
    const globalState = { requests: [], responder: defaultResponder, global: true };
    globalState.responder = async url => {
      if (url.pathname.endsWith("/history")) return { body: { items: [{ learning_id: "G5", text: "Global retained", tag: "rule", bucket: "delta",
        scope: "global", state: "accepted" }], count: 1, next_cursor: null, read_only: true } };
      if (url.pathname.endsWith("/G5/review")) return { body: { item: { id: "G5", text: "Global retained", tag: "rule", bucket: "delta",
        scope: "global", revision: 1, state: "accepted", created: "2026-09-01T00:00:00Z" }, sources: [], history: [],
        page_collections: ["evidence", "history"], evidence_complete: true, references_complete: true, history_complete: true,
        evidence_status: "missing", read_only: true } };
      return { status: 404, body: { error: { message: "unexpected " + url.pathname } } };
    };
    const globalLoaded = await newPage(browser, globalState);
    await globalLoaded.page.waitForSelector(".lnhistdetail .mkt");
    assert.match(await globalLoaded.page.locator(".lnhistdetail").textContent(), /Global retained/);
    assert.equal(await globalLoaded.page.locator('[data-view="history"]').getAttribute("aria-selected"), "true");
    assert(globalState.requests.some(request => request.startsWith("/root/learnings/history")), "the Global inventory uses the root route");
    assert(globalState.requests.every(request => !/^\/root\/learnings\/G5$/.test(request)),
      "the old session-keyed cores detail is never requested");
    assert.equal(globalLoaded.errors.length, 0, globalLoaded.errors.join("\n"));
    await globalLoaded.page.close();

    console.log("Learning historical archive browser checks passed: real tabs, paginated inventory, ID search, read-only detail, metadata/snapshots, resumable oversized pages, transient retry, access-loss withholding, independent collections and the Global deep link.");
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exit(1); });