#!/usr/bin/env node
// Account-email send outcome in Global Signoff drawer, inline Signoff view and shared formatter. Synthetic fixtures only: no live API, mailbox or provider.
// PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_signoff_email_outcome_browser.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const ROOT = path.join(__dirname, "..");
const clone = value => JSON.parse(JSON.stringify(value));
const listFixture = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/signoffs.json")));
const A = listFixture.data.review_business_id;
const OWNER = "40000000-0000-4000-8000-000000000001";
const session = { principal_id: OWNER, principal_kind: "human", display_name: "Owner", is_owner: true };
const envelope = data => ({ request_id: "fixture", status: "ok", data, warnings: [] });
const reply = (res, body, status = 200) => { res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" }); res.end(JSON.stringify(body)); };
const id = n => `30000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
let records, errors;
const GID = id(201);
const out = (stage, label, extra = {}) => ({ kind: "account_email", operation: "email_send", stage, label, ...extra });
const failed = () => out("failed", "Send failed", { job: { id: "j1", state: "failed", attempts: 3, updated_at: "2026-09-08T09:00:00Z" },
  error: { code: "external_failed", retryable: false }, diagnostics: { engine_error: "jmap_invalidEmail", provider_status: 400, provider_error_type: "invalidEmail" },
  provider_submission_accepted: false, recipient_delivery_confirmed: false });
function reset() {
  const base = clone(listFixture.data.items[0]);
  records = [{ ...base, id: GID, title: "Global account mail", review_business_id: null, category: "internal_change", operation_name: "email_send", state: "pending",
    status_revision: 3, decision_state: "pending", outcome: out("awaiting_approval", "Awaiting approval") }];
  // An account-email Signoff carries its exact frozen message; without it the preview is unavailable and cannot be approved.
  records[0].preview.changes = [{ kind: "structured", resource: { resource_type: "fastmail_account", resource_id: "mail-1", label: "Mailbox" }, fields: [
    { path: "from_address", before: null, after: "me@example.test" }, { path: "to", before: null, after: ["you@example.test"] },
    { path: "subject", before: null, after: "Hello" }, { path: "body", before: null, after: "Body text" }] }];
  errors = [];
}
async function handle(req, res) {
  const url = new URL(req.url, "http://localhost"), route = url.pathname;
  if (route.endsWith("/operations/email_mailboxes_list")) return reply(res, envelope({ mailboxes: [], agents: [] }));
  if (route.endsWith("/operations/context_reviews_list")) return reply(res, envelope({ items: [] }));
  if (route === "/api/session") return reply(res, envelope(session));
  if (route === "/api/me/tasks") return reply(res, { principal_id: OWNER, tasks: [] });
  if (route === "/api/me/attention") return reply(res, envelope({ businesses: [], urgent: [] }));
  if (route === "/api/me/attention/deliveries") return reply(res, envelope({ items: [] }));
  if (route === "/api/biz") return reply(res, [{ id: A, slug: "alpha", name: "Atlas Research", owner: "owner", agents: [], agent_meta: {} }]);
  if (route.endsWith("/agents")) return reply(res, []);
  if (route.endsWith("/env")) return reply(res, { pairs: [] });
  if (route.endsWith("/tasks") || route.endsWith("/log")) return reply(res, []);
  if (route.endsWith("/live")) return reply(res, { sessions: [], requests: [], routine_failures: [] });
  const m = /^\/api\/root\/signoffs(?:\/([^/]+))?$/.exec(route);
  if (m) {
    if (!m[1]) {
      // Honors the requested states filter like the real list does.
      const states = (url.searchParams.get("states") || "").split(",").filter(Boolean);
      const items = records.filter(s => !states.length || states.includes(s.state)).map(s => { const { preview, ...row } = s; return row; });
      return reply(res, envelope({ items, counts: { pending: items.length }, next_cursor: null, updated_cursor: "c", has_more: false }));
    }
    const item = records.find(s => s.id === m[1]);
    if (!item) return reply(res, { status: "error", error: { code: "not_found", message: "missing" } }, 404);
    return reply(res, envelope({ signoff: clone(item) }));
  }
  if (route === "/api/biz/alpha/signoffs") return reply(res, envelope({ review_business_id: A, items: [], counts: { pending: 0 }, next_cursor: null, updated_cursor: "c", has_more: false }));
  if (route.startsWith("/api/root/")) return reply(res, envelope({ items: [] }));
  if (route.startsWith("/api/")) { errors.push(`Unexpected API ${route}`); return reply(res, { status: "error", error: { code: "not_found", message: "no fixture" } }, 404); }
  const target = path.resolve(ROOT, route === "/" ? "index.html" : route.slice(1));
  if (!target.startsWith(ROOT + path.sep)) { res.writeHead(403); return res.end(); }
  try {
    res.writeHead(200, { "Content-Type": target.endsWith(".css") ? "text/css" : target.endsWith(".js") ? "text/javascript" : "text/html" }); res.end(fs.readFileSync(target));
  } catch { res.writeHead(404); res.end(); }
}
(async () => {
  reset();
  const server = http.createServer((req, res) => handle(req, res).catch(e => { console.error(e); if (!res.headersSent) res.writeHead(500); res.end(); }));
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser, tests = 0;
  const check = async (label, fn) => { await fn(); tests++; console.log(`ok ${label}`); };
  try {
    browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route("**/*", route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
    await context.addInitScript(() => { localStorage.setItem("tab", "settings"); localStorage.setItem("tasks", "1"); localStorage.setItem("tview", "signoffs"); });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    page.on("pageerror", e => errors.push(e.message));
    await page.goto(origin + "/#alpha");
    await page.waitForFunction(() => typeof sgCreateView === "function" && state.bizs?.length);

    await check("formatter: every stage separates approval from result; unknown and unrelated stay safe", async () => {
      const r = await page.evaluate(() => {
        const mk = (state, outcome) => ({ state, category: "internal_change", ...(outcome ? { outcome } : {}) });
        const st = stage => sgEmailStatus(mk("applied", { kind: "account_email", stage, label: "L" }));
        return {
          stages: ["awaiting_approval", "approved", "queued", "running", "reconciling", "submitted", "failed", "unconfirmed", "cancelled"].map(st),
          unknown: st("mystery"), bare: sgEmailStatus(mk("applied", { kind: "account_email", stage: "x" })),
          other: sgEmailStatus(mk("applied", { kind: "business_email", stage: "failed", label: "L" })), none: sgEmailStatus(mk("applied")),
          pendingState: sgEmailStatus(mk("pending", { kind: "account_email", stage: "queued", label: "L" })),
          xss: sgEmailOutcomeHtml(mk("applied", { kind: "account_email", stage: "x", label: "<img src=x onerror=1>" })),
        };
      });
      assert.deepEqual(r.stages, ["Awaiting approval", "Approved", "Approved · Queued", "Approved · Sending", "Approved · Checking send", "Approved · Submitted", "Approved · Send failed", "Approved · Send unconfirmed", "Approved · Cancelled"]);
      assert.equal(r.unknown, "L"); assert.equal(r.bare, "Status unavailable");
      assert.equal(r.other, null); assert.equal(r.none, null); assert.equal(r.pendingState, null);
      assert.doesNotMatch(r.xss, /<img/);
    });

    // The shared single-item view used by the queue modal and inline Chat.
    await page.evaluate(id => {
      const host=document.createElement('div');host.id='outcome-host';host.style.cssText='position:fixed;inset:20px;z-index:99999;background:#111;overflow:auto';document.body.append(host);
      window.outcomeView=sgCreateView({active:()=>host.isConnected,body:()=>host,workspace:()=>'scope:global',global:true,inline:true,execution:true,waitingOnly:false,item:()=>({id,businessId:null})});outcomeView.paint();
    }, GID);
    const row = page.locator(`.sg-row[data-id="${GID}"]`);
    await row.waitFor();
    await check("Shared review view opens the pending account-mail Signoff", async () => {
      await row.locator(".sg-detail:not([hidden])").waitFor();
      assert.equal(await row.locator(".sg-outcome").count(), 0);
    });
    await check("after approval the selected Signoff stays and follows queued -> failed on a fixed revision", async () => {
      Object.assign(records[0], { state: "applied", status_revision: 4, decision_state: "approved", outcome: out("queued", "Queued for sending") });
      await page.evaluate(() => outcomeView.refresh());
      await page.waitForFunction(i => /Queued/.test(document.querySelector(`.sg-row[data-id="${i}"] .sg-outcome`)?.textContent || ""), GID);
      assert.equal((await row.locator(".sg-state-tags").textContent()).trim(), "Approved · Queued");
      records[0].outcome = failed();
      await page.evaluate(() => outcomeView.refresh());
      await page.waitForFunction(i => /Send failed/.test(document.querySelector(`.sg-row[data-id="${i}"] .sg-outcome`)?.textContent || ""), GID);
      const text = await row.locator(".sg-outcome").textContent();
      for (const needle of ["external_failed", "jmap_invalidEmail", "400", "invalidEmail"]) assert(text.includes(needle), needle);
      assert.equal((await row.locator(".sg-state-tags").textContent()).trim(), "Approved · Send failed");
      assert.equal(records[0].status_revision, 4);
      assert.equal(await page.locator(".sg-empty").filter({ hasText: "No signoffs waiting" }).count(), 0);
      assert.equal(await page.locator("#tbody .sg-count").filter({ hasText: /[1-9]\d* pending/ }).count(), 0);
      assert.equal(await row.locator(".sg-state-tags").textContent().then(s => /Pending|waiting/i.test(s)), false);
      assert.equal(await row.locator("button:has-text('Resend'), button:has-text('Retry')").count(), 0);
    });
    await check("diagnostics clear when the outcome changes to reconciling", async () => {
      records[0].outcome = out("reconciling", "Checking whether the message was sent");
      await page.evaluate(() => outcomeView.refresh());
      await page.waitForFunction(i => /Checking send/.test(document.querySelector(`.sg-row[data-id="${i}"] .sg-outcome`)?.textContent || ""), GID);
      const text = await row.locator(".sg-outcome").textContent();
      assert.doesNotMatch(text, /external_failed|jmap_invalidEmail|400/);
      assert.match(text, /not confirmed yet/);
    });

    await page.evaluate(()=>{outcomeView.dispose();document.querySelector('#outcome-host').remove();});

    // Global single-item (inline Chat) route.
    await check("Global single-item view reads the root endpoint and refreshes the outcome", async () => {
      records[0].outcome = out("submitted", "Submitted to provider", { provider_submission_accepted: true, recipient_delivery_confirmed: false });
      await page.evaluate(i => {
        const host = document.createElement("div"); host.id = "inline-host"; host.style.cssText = "position:fixed;inset:0;z-index:99999;background:#fff;overflow:auto";
        document.body.append(host);
        window.inlineView = sgCreateView({ active: () => host.isConnected, body: () => host, workspace: () => "scope:global", global: true, inline: true, execution: true, waitingOnly: false, item: () => ({ id: i, businessId: null }) });
        window.inlineView.paint();
      }, GID);
      const inline = page.locator("#inline-host");
      await inline.locator(".sg-outcome").waitFor();
      assert.equal((await inline.locator(".sg-state-tags").textContent()).trim(), "Approved · Submitted");
      assert.match(await inline.locator(".sg-outcome").textContent(), /Accepted by the mail provider; delivery is not confirmed\./);
      records[0].outcome = out("unconfirmed", "Send could not be confirmed");
      await page.evaluate(() => window.inlineView.refresh());
      await page.waitForFunction(() => /Send unconfirmed/.test(document.querySelector("#inline-host .sg-state-tags")?.textContent || ""));
      assert.match(await inline.locator(".sg-outcome").textContent(), /may or may not have been sent/);
    });
    assert.deepEqual(errors, []);
    console.log(`${tests} signoff email outcome checks passed`);
  } finally {
    if (browser) await browser.close();
    server.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
