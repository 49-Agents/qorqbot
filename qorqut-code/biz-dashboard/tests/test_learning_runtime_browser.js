#!/usr/bin/env node
// Real service DTOs and SQLite historical rows, with the real dashboard and browser. No Claude/model service.
// Integration check for the historical archive: it needs the target core contract (`shared.py`, the
// evidence/history review page fields and the complete legacy inventory), so it is not a slice-local check.
//   BAZARLYK_CORE_ROOT=/path/to/core PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_learning_runtime_browser.js
const assert = require("node:assert/strict");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const core = process.env.BAZARLYK_CORE_ROOT || path.resolve(__dirname, "../../app");
const child = spawn(process.env.PYTHON || path.join(core, ".venv/bin/python"),
  ["tests/service_browser_fixture.py", "--dashboard-root", path.resolve(__dirname, ".."), "--learnings"],
  { cwd: core, env: { ...process.env, PYTHONPATH: "." }, stdio: ["ignore", "pipe", "pipe"] });
let stderr = "", browser, page;
child.stderr.on("data", value => { stderr += value; });

async function main() {
  const fixture = await new Promise((resolve, reject) => {
    let stdout = "";
    const timer = setTimeout(() => reject(new Error("Fixture startup timeout: " + stderr)), 180000);
    child.stdout.on("data", value => {
      stdout += value;
      const line = stdout.split("\n").find(value => value.startsWith('{"url":'));
      if (line) { try { resolve(JSON.parse(line)); clearTimeout(timer); } catch {} }
    });
    child.on("exit", code => { clearTimeout(timer); reject(new Error("Fixture exited " + code + ": " + stderr)); });
  });
  browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [], requests = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => requests.push(new URL(request.url()).pathname));
  await page.route("**/*", route => route.request().url().startsWith(fixture.origin) ? route.continue() : route.abort());
  await page.goto(fixture.url);
  await page.locator("#agentrows").waitFor();
  await page.waitForFunction(() => state.biz && tLastBiz === state.biz);
  await page.evaluate(() => goTab("context"));
  await page.locator("#ctxview").waitFor();
  await page.evaluate(() => ctxGo("_learning"));
  const ids = fixture.learning_ids;
  const card = key => page.locator(`.lncard[data-id="${ids[key]}"]`);

  // Retained procedural deltas are not factual review cards; factual review still keeps its controls.
  await page.locator(".lnsplit").waitFor();
  for (const key of ["active", "dropped", "done", "accepted", "rejected", "global", "chat"])
    assert.equal(await card(key).count(), 0, `a retained ${key} delta is not a factual review card`);
  const contextCard = card("context");
  await contextCard.waitFor();
  await contextCard.locator(".mkt").click();
  await contextCard.locator(".lnnote").fill("Preserve this as a question.");
  await contextCard.locator('[data-act="approve"]').click();
  await contextCard.waitFor({ state: "detached" });
  const filed = await page.evaluate(async id => api("/biz/" + state.biz + "/learnings/" + id), ids.context);
  assert((filed.data?.item || filed.item).filing.upload_id, "factual acceptance still files its context");

  // The archive is the only place a retained record appears, and it is read-only.
  await page.locator('[data-view="history"]').click();
  const row = key => page.locator(`.lnhistrow[data-hist-id="${ids[key]}"]`);
  await row("active").waitFor();
  assert.match(await row("active").textContent(), /In progress/);
  assert.match(await row("dropped").textContent(), /Rejected in work/);
  assert.match(await row("done").textContent(), /Implemented/);
  assert.match(await row("accepted").textContent(), /Accepted/);
  assert.match(await row("rejected").textContent(), /Rejected/);
  assert.equal(await page.locator(".lnhistrow .lncard").count(), 0);
  await page.locator("#lnsearch").fill(ids.active);
  await page.waitForFunction(() => document.querySelectorAll(".lnhistrow").length === 1);
  assert.match(await page.locator(".lnhistrow").textContent(), /In progress/);
  await page.locator("#lnsearch").fill("");

  await row("active").click();
  await page.waitForSelector(".lnhistdetail .mkt");
  const detail = page.locator(".lnhistdetail");
  assert.match(await detail.textContent(), /historical procedural record/);
  assert.match(await detail.textContent(), /read-only/);
  assert.match(await detail.textContent(), /work_start/);
  assert.equal(await detail.locator("[data-act]").count(), 0);
  assert.equal(await detail.locator("textarea").count(), 0);
  assert.equal(await detail.locator(".lnscope").count(), 0);
  assert.equal(await detail.getByRole("button", { name: /Approve|Reject|Revert|Keep both|Start work|Drop|Done|Edit/ }).count(), 0);

  // Evidence comes from the archive detail, never from the retired session-keyed cores dialog.
  await page.locator("[data-hist-back]").click();
  await row("global").click();
  await page.waitForSelector(".lnhistdetail .mkt");
  await page.locator(".lnsource pre").first().waitFor();
  assert.match(await page.locator(".lnsource pre").first().textContent(), /Frozen Global question and answer: <literal>/);
  assert.equal(await page.locator("dialog[open]").count(), 0);
  assert.equal(await page.locator(".lnhistdetail literal").count(), 0);
  const historical = new Set(["open", "accepted", "active", "rejected", "dropped", "done", "global", "chat"]
    .map(key => ids[key]));
  const plainDetails = requests.filter(pathname => /^\/api\/(biz\/[^/]+\/)?learnings\/[^/]+$/.test(pathname))
    .map(pathname => pathname.split("/").pop());
  assert(plainDetails.every(id => !historical.has(id)),
    "a historical candidate never uses the old session-keyed cores detail: " + plainDetails.join(","));

  assert.equal(errors.length, 0, errors.join("\n"));
  console.log("Learning real-runtime archive checks passed: retained records are read-only history only, factual filing still works, and evidence comes from the paginated archive.");
}
main().catch(error => {
  console.error(error);
  process.exitCode = 1;
  if (page && !page.isClosed()) console.error(page.locator("#ctxview").innerText().catch(() => "No context view"));
}).finally(async () => {
  if (browser) await browser.close();
  if (child.exitCode === null) { child.kill("SIGINT"); const timer = setTimeout(() => child.kill("SIGTERM"), 10000); child.once("exit", () => clearTimeout(timer)); }
});