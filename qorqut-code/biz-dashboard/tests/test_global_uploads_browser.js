#!/usr/bin/env node
// The Global drawer's Uploads tab: text-only (no attach button), the call/meeting switch and participant
// typeahead reused from the business tab, and both routed to the dedicated root endpoints
// (POST /api/root/context/upload, POST /api/root/context/people) rather than a business path or the
// generic operations proxy. Real app HTML/CSS/JS, an isolated static server and synthetic API responses:
// no dashboard server.py, filer, production data or other write endpoints run.
// Requires Playwright (see README). Optional PLAYWRIGHT_CHROMIUM_EXECUTABLE selects an existing browser.
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), http = require("node:http");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const repo = path.resolve(__dirname, "..");
const PRIYA = "11111111-1111-4111-8111-111111111111";
const people = [{ id: PRIYA, kind: "context_person", metadata: { name: "Priya Rao" } }];
const peopleRequests = [], uploads = [], notesReads = [], writes = [], errors = [];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://localhost");
  if (req.method === "POST") {
    let body = ""; req.on("data", chunk => body += chunk); req.on("end", () => {
      const args = body ? JSON.parse(body) : {}, json = (status, value) => res.writeHead(status, { "Content-Type": "application/json" }).end(JSON.stringify(value));
      if (url.pathname === "/api/root/context/people") {
        peopleRequests.push(args);
        return json(200, { status: "ok", data: { items: /^pri/i.test(args.query || "") ? people : [], next_cursor: null } });
      }
      if (url.pathname === "/api/root/context/upload") {
        uploads.push(args);
        return json(202, { ok: true, status: "accepted", id: "2026-09-20-a1b2", document_id: "doc-1", path: "_inbox/2026-09-20-a1b2.md",
          job: { id: "job-1", state: "queued" } });
      }
      // The Global Overview page behind the drawer polls its own summary operations; harmless empty
      // answers so opening the drawer over it produces no console errors, distinct from the two routes above.
      if (url.pathname.startsWith("/api/root/operations/")) return json(200, { status: "ok", data: {} });
      writes.push(req.method + " " + req.url); res.writeHead(405).end();
    });
    return;
  }
  if (url.pathname.startsWith("/api/")) {
    let data = {};
    if (url.pathname === "/api/biz") data = [{ id: "studio-id", slug: "studio", name: "Example Studio", agents: [] }];
    if (url.pathname === "/api/root/notes") { notesReads.push(req.url); data = []; }
    if (url.pathname === "/api/session") data = { status: "ok", data: { principal_id: "owner" } };
    if (url.pathname === "/api/root/agents") data = { status: "ok", data: { agents: [] } };
    if (url.pathname === "/api/me/tasks") data = { principal_id: "owner", tasks: [] };
    if (url.pathname === "/api/root/routines") data = { routines: [], programs: [], enabled: true, root: true, current: null, queued: [], invalid: [] };
    res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(data)); return;
  }
  const file = path.resolve(repo, "." + (url.pathname === "/" ? "/index.html" : url.pathname));
  if (!file.startsWith(repo + path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
  const contentType = file.endsWith(".css") ? "text/css" : file.endsWith(".js") ? "text/javascript" : "text/html";
  res.writeHead(200, { "Content-Type": contentType }).end(fs.readFileSync(file));
});

(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route("**/*", route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    const page = await context.newPage(); page.on("pageerror", e => errors.push(e.message));
    await page.goto(origin + "/#/global/overview");
    await page.waitForFunction(() => typeof state !== "undefined" && state.scope === "global");

    // The Upload call-to-action next to Filed context opens the drawer on Uploads.
    await page.evaluate(() => { location.hash = "/global/context/library"; });
    await page.locator("#global-context-upload").waitFor();
    await page.locator("#global-context-upload").click();
    await page.locator("#uptext").waitFor();
    assert.equal(await page.evaluate(() => tView), "uploads");
    assert.deepEqual(await page.locator(".ttabs [data-v]").evaluateAll(tabs => tabs.map(t => t.dataset.v)), ["notes", "tasks", "signoffs", "uploads", "routines", "activity"]);
    console.log("ok the Global nav's Upload button opens the drawer on the Uploads tab");

    // Text-only: no attach button, no file input reachable, no Story chips control.
    assert.equal(await page.locator("#upattach").isVisible(), false, "no attach button at Global");
    assert.equal(await page.locator("#ctxuploadtasks").count(), 0);
    await page.evaluate(() => { const dt = new DataTransfer(); dt.items.add(new File(["x"], "image.png", { type: "image/png" }));
      document.querySelector("#upbox").dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true })); });
    await page.locator("#toasts").getByText("Media and data files belong to a business").waitFor();
    assert.equal(await page.locator(".upth").count(), 0, "the dropped file was refused, not attached");
    console.log("ok text-only: attach button hidden, Story chips absent, drops refused");

    // Call reveals who/when; the typeahead calls the root people route, not the business route or the generic proxy.
    await page.locator('#upkind [data-origin="call"]').click();
    const transcript = "Michael [10:00]: Can we pilot in October?\nPriya [10:01]: Yes, the budget is approved.";
    await page.locator("#uptext").fill(transcript);
    await page.locator("#upwho").fill("Pri");
    await page.locator(".menu .mi").first().waitFor();
    assert.deepEqual(await page.locator(".menu .mi .lb").allTextContents(), ["Priya Rao", 'Add "Pri"']);
    assert.deepEqual(peopleRequests.map(({ query, limit }) => ({ query, limit })), [{ query: "Pri", limit: 8 }]);
    await page.locator(".menu .mi").first().click();
    assert.deepEqual(await page.locator("#upwhochips .upchip").evaluateAll(chips => chips.map(chip => chip.textContent.replace("×", ""))), ["Priya Rao"]);
    console.log("ok the participant typeahead calls POST /api/root/context/people");

    // Sending posts to the root upload route with origin, participants and an empty task_ids -- never a business path.
    await page.locator("#upsend").click();
    await page.waitForFunction(() => document.querySelector(".updrow .updk")?.textContent === "call");
    assert.equal(uploads.length, 1);
    const { idempotency_key, ...sent } = uploads[0];
    assert.ok(idempotency_key);
    assert.deepEqual(sent, { text: transcript, notes: "", topic: "", task_ids: [], origin: "call", participants: [{ person_id: PRIYA, name: "Priya Rao" }] });
    console.log("ok sending posts to POST /api/root/context/upload with origin, participants and empty task_ids");

    await page.evaluate(() => selectBiz("studio"));
    await page.waitForFunction(() => state.scope === "business" && state.biz === "studio");
    await page.evaluate(() => { openTasks(true); tSetView("tasks"); });
    assert.deepEqual(await page.locator(".ttabs [data-v]").evaluateAll(tabs => tabs.map(t => t.dataset.v)), ["globalNotes", "tasks", "signoffs", "uploads", "routines", "activity"]);
    writes.length = 0; // Ignore the business-context lookup triggered while switching workspaces.
    await page.locator(".ttabs [data-v=globalNotes]").click();
    await page.waitForFunction(() => tView === "globalNotes" && ntLoadedScope === "/root");
    assert.deepEqual(notesReads, ["/api/root/notes"]);
    console.log("ok Global Notes stays first in a business pane and reads the root notes endpoint");

    assert.deepEqual(writes, []); assert.deepEqual(errors, []);
    console.log("PASS Global uploads browser checks; no unexpected writes or uncaught browser errors");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
