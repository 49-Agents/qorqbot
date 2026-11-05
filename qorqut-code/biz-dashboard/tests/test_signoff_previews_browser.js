#!/usr/bin/env node
// Exact Signoff proposal content (WhatsApp, account email, generic changes) and Review-before-Approve. Synthetic fixtures only: no live API, recipient or provider.
// PLAYWRIGHT_MODULE=/path/to/playwright SIGNOFF_PREVIEW_SCREENSHOTS=/dir node tests/test_signoff_previews_browser.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const ROOT = path.join(__dirname, "..");
const SHOTS = process.env.SIGNOFF_PREVIEW_SCREENSHOTS || "";
const clone = value => JSON.parse(JSON.stringify(value));
const OWNER = "40000000-0000-4000-8000-000000000001";
const BIZ = "10000000-0000-4000-8000-000000000001";
const session = { principal_id: OWNER, principal_kind: "human", display_name: "Owner", is_owner: true };
const envelope = data => ({ request_id: "fixture", status: "ok", data, warnings: [] });
const reply = (res, body, status = 200) => { res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" }); res.end(JSON.stringify(body)); };
const id = n => `30000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const field = (p, after, before = null) => ({ path: p, before, after });
const resource = (type, label) => ({ resource_type: type, resource_id: type + "-1", label });
function record(n, title, operation, changes, effects = []) {
  return { id: id(n), review_business_id: null, proposal_revision: 1, payload_hash: String(n).repeat(64).slice(0, 64), status_revision: 1, state: "pending", decision_state: "pending",
    operation_name: operation, operation_version: 1, category: "internal_change", proposer_principal_id: OWNER, proposer_display_name: "Onbashy", proposer_agent_id: null,
    title, affected_scope: { kind: "global" }, created_at: "2026-09-07T12:00:00Z", updated_at: "2026-09-07T12:00:00Z", updated_sequence: n, decision: null, supersedes_id: null, superseded_by_id: null,
    preview: { title, rationale: "Owner asked for this.", affected_scope: { kind: "global" }, resources: [], changes, effects }, outcome: null };
}
const wa = (chatId, name, text, extra = []) => record(1, "Send a WhatsApp message", "whatsapp_send", [{ kind: "structured", resource: resource("whatsapp_account", "WhatsApp account"),
  fields: [field("chat_id", chatId), ...(name === undefined ? [] : [field("chat_name", name)]), field("text", text), ...extra] }]);
let records, posts, errors, nextId;
function reset() { records = []; posts = []; errors = []; }
const waRecord = (chatId, name, text) => { const r = wa(chatId, name, text); r.id = id(++nextId); r.payload_hash = String(nextId).padStart(64, "a"); return r; };
async function handle(req, res) {
  const url = new URL(req.url, "http://localhost"), route = url.pathname;
  if (route.endsWith("/operations/email_mailboxes_list")) return reply(res, envelope({ mailboxes: [], agents: [] }));
  if (route.endsWith("/operations/context_reviews_list")) return reply(res, envelope({ items: [] }));
  if (route === "/api/session") return reply(res, envelope(session));
  if (route === "/api/me/tasks") return reply(res, { principal_id: OWNER, tasks: [] });
  if (route === "/api/me/attention") return reply(res, envelope({ businesses: [], urgent: [] }));
  if (route === "/api/me/attention/deliveries") return reply(res, envelope({ items: [] }));
  if (route === "/api/biz") return reply(res, [{ id: BIZ, slug: "alpha", name: "Atlas Research", owner: "owner", agents: [], agent_meta: {} }]);
  if (route.endsWith("/agents")) return reply(res, []);
  if (route.endsWith("/env")) return reply(res, { pairs: [] });
  if (route.endsWith("/tasks") || route.endsWith("/log")) return reply(res, []);
  if (route.endsWith("/live")) return reply(res, { sessions: [], requests: [], routine_failures: [] });
  const m = /^\/api\/root\/signoffs(?:\/([^/]+)(?:\/(approve|reject))?)?$/.exec(route);
  if (m) {
    if (!m[1]) {
      const items = records.map(s => { const { preview, ...row } = s; return row; });
      return reply(res, envelope({ items, counts: { pending: items.length }, next_cursor: null, updated_cursor: "c", has_more: false }));
    }
    const item = records.find(s => s.id === m[1]);
    if (!item) return reply(res, { status: "error", error: { code: "not_found", message: "missing" } }, 404);
    if (m[2]) {
      let raw = ""; for await (const chunk of req) raw += chunk;
      posts.push({ id: item.id, action: m[2], body: JSON.parse(raw) });
      item.state = m[2] === "approve" ? "applied" : "rejected"; item.status_revision++;
      return reply(res, envelope({ signoff: clone(item), replayed: false }), 200);
    }
    return reply(res, envelope({ signoff: clone(item) }));
  }
  if (route === "/api/biz/alpha/signoffs") return reply(res, envelope({ review_business_id: BIZ, items: [], counts: { pending: 0 }, next_cursor: null, updated_cursor: "c", has_more: false }));
  if (route.startsWith("/api/root/")) return reply(res, envelope({ items: [] }));
  if (route.startsWith("/api/")) { errors.push(`Unexpected API ${route}`); return reply(res, { status: "error", error: { code: "not_found", message: "no fixture" } }, 404); }
  const target = path.resolve(ROOT, route === "/" ? "index.html" : route.slice(1));
  if (!target.startsWith(ROOT + path.sep)) { res.writeHead(403); return res.end(); }
  try { res.writeHead(200, { "Content-Type": target.endsWith(".css") ? "text/css" : target.endsWith(".js") ? "text/javascript" : "text/html" }); res.end(fs.readFileSync(target)); }
  catch { res.writeHead(404); res.end(); }
}
(async () => {
  reset(); nextId = 100;
  const server = http.createServer((req, res) => handle(req, res).catch(e => { console.error(e); if (!res.headersSent) res.writeHead(500); res.end(); }));
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser, tests = 0;
  const check = async (label, fn) => { await fn(); tests++; console.log(`ok ${label}`); };
  try {
    const launch = { headless: true, args: ["--no-sandbox"] };
    if (process.env.CHROMIUM_EXECUTABLE) launch.executablePath = process.env.CHROMIUM_EXECUTABLE;
    browser = await chromium.launch(launch);
    const context = await browser.newContext({ viewport: { width: 1200, height: 900 } });
    await context.route("**/*", route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
    await context.addInitScript(() => { localStorage.setItem("tab", "settings"); localStorage.setItem("tasks", "1"); localStorage.setItem("tview", "signoffs"); });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    page.on("pageerror", e => errors.push(e.message));
    await page.goto(origin + "/#alpha");
    await page.waitForFunction(() => typeof sgCreateView === "function" && state.bizs?.length);
    // Mounts one inline (single-item, compact) card per record in a stack, like several Chat messages.
    const mountInline = async ids => {
      await page.evaluate(ids => {
        document.querySelector("#inline-stack")?.remove();
        const stack = document.createElement("div"); stack.id = "inline-stack"; stack.style.cssText = "position:fixed;inset:0;z-index:99999;background:#111;overflow:auto;padding:12px;max-width:460px;margin:0 auto";
        document.body.append(stack);
        window.inlineViews = ids.map(i => {
          const host = document.createElement("div"); host.className = "inline-host"; host.dataset.id = i; stack.append(host);
          const view = sgCreateView({ active: () => host.isConnected, body: () => host, workspace: () => "scope:global", global: true, inline: true, execution: true, waitingOnly: false, item: () => ({ id: i, businessId: null }), prefix: "i" + Math.random().toString(36).slice(2) + "-" });
          view.paint(); return view;
        });
      }, ids);
      for (const i of ids) await page.locator(`.inline-host[data-id="${i}"] .sg-preview`).waitFor();
      await page.waitForFunction(ids => ids.every(i => document.querySelector(`.inline-host[data-id="${i}"] .sg-detail:not([hidden])`)), ids);
    };
    const until = async fn => { for (let i = 0; i < 80 && !fn(); i++) await page.waitForTimeout(50); assert(fn(), 'condition not reached'); };
    const host = i => page.locator(`.inline-host[data-id="${i}"]`);
    const approveEnabled = async i => (await host(i).locator(".sg-approve").count()) > 0 && await host(i).locator(".sg-approve").isEnabled();

    const msgs = [
      waRecord("120363025@g.us", "Launch team 🚀", "Hello team,\nthe launch moves to <b>Friday</b> & <script>window.bad=1</script>\n\nمرحبا — 你好 — Привет"),
      waRecord("905551112233@s.whatsapp.net", "", "Ayrı mesaj: ikinci kişi\nsatır iki"),
      waRecord("905559998877@s.whatsapp.net", "Unknown", "x".repeat(600) + " " + "Z".repeat(200) + "\nEND-OF-MESSAGE"),
    ];
    records.push(...msgs);
    await check("WhatsApp cards show recipient and exact text, expanded with supporting context and no technical IDs", async () => {
      await mountInline(msgs.map(m => m.id));
      const first = host(msgs[0].id);
      assert.equal((await first.locator(".sg-open").count()), 1);
      assert.equal(await first.locator(".sg-recipient-name").textContent(), "Launch team 🚀");
      assert.doesNotMatch(await first.innerText(), /120363025@g.us/);
      assert.doesNotMatch(await first.innerText(), /120363025@g.us/);
      assert.equal(await first.locator(".sg-message-rationale").isVisible(), true);
      assert.equal(await first.locator(".sg-inline-toggle").count(),0);
      assert.equal(await first.locator(".sg-whatsapp .sg-text").evaluate(el => el.textContent), msgs[0].preview.changes[0].fields[2].after);
      assert.equal(await first.locator(".sg-whatsapp .sg-text").isVisible(), true, "visible without Details");
      assert.equal(await first.locator(".sg-rowtitle").textContent(), "Send a WhatsApp message");
      assert.equal(await first.locator("script, b").count(), 0, "HTML-looking text stays inert");
      assert.equal(await page.evaluate(() => window.bad), undefined);
      assert.equal(await first.locator(".sg-whatsapp .sg-text").getAttribute("tabindex"), "0");
    });
    await check("missing names fall back to phone numbers; distinct messages remain distinguishable", async () => {
      assert.equal(await host(msgs[1].id).locator(".sg-recipient-name").textContent(), "+905551112233");
      assert.equal(await host(msgs[1].id).locator(".sg-secondary").count(), 0);
      assert.equal(await host(msgs[2].id).locator(".sg-recipient-name").textContent(), "+905559998877");
      const texts = await page.locator(".inline-host .sg-whatsapp .sg-text").evaluateAll(els => els.map(e => e.textContent));
      assert.equal(new Set(texts).size, 3);
    });
    await check("long messages remain fully expanded and wrap without horizontal overflow", async () => {
      const region = host(msgs[2].id).locator(".sg-whatsapp .sg-text");
      const m = await region.evaluate(el => ({ full: el.textContent.endsWith("END-OF-MESSAGE"), client: el.clientHeight, scroll: el.scrollHeight, over: el.scrollWidth > el.clientWidth + 1 }));
      assert.equal(m.full, true); assert.equal(m.over, false);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert(m.client >= m.scroll - 1, "full message is expanded without internal scrolling");
    });
    await check("incomplete or conflicting required fields show an unavailable preview and cannot approve", async () => {
      const bad = [
        waRecord("", "Name", "text"), waRecord("120@g.us", "Name", "   "),
        (() => { const r = waRecord("120@g.us", "Name", "a"); r.preview.changes[0].fields.push(field("text", "different")); return r; })(),
        (() => { const r = waRecord("120@g.us", "Name", "a"); r.preview.changes[0].fields = r.preview.changes[0].fields.filter(f => f.path !== "chat_id"); return r; })(),
        (() => { const r = waRecord("120@g.us", "Name", "a"); r.preview.changes[0].fields[2].after = { not: "a string" }; return r; })(),
      ];
      records.push(...bad);
      await mountInline(bad.map(r => r.id));
      for (const r of bad) {
        assert.match(await host(r.id).locator(".sg-unavailable").textContent(), /Preview unavailable/);
        assert.equal(await host(r.id).locator(".sg-approve").count(), 0);
        assert.equal(await host(r.id).locator(".sg-whatsapp").count(), 0);
      }
      const rows = await page.evaluate(() => [...sgViews].length);
      assert(rows > 0);
    });
    await mountInline(msgs.map(m => m.id));
    if (SHOTS) {
      fs.mkdirSync(SHOTS, { recursive: true });
      await page.screenshot({ path: path.join(SHOTS, "whatsapp-desktop.png") });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.screenshot({ path: path.join(SHOTS, "whatsapp-mobile.png") });
      const samples = [waRecord("120363025@g.us", "Design team", "The updated mockups are ready.\nCan you review them before tomorrow’s meeting?"),
        waRecord("905551112233@s.whatsapp.net", "Aylin", "Hi Aylin, does 10:30 tomorrow still work for you?")];
      records.push(...samples);
      await mountInline(samples.map(r => r.id));
      await page.locator("#inline-stack").evaluate(el => { el.style.bottom = "auto"; el.style.display = "grid"; el.style.gap = "12px"; });
      await page.locator("#inline-stack").screenshot({ path: path.join(SHOTS, "whatsapp-clean.png") });
      await page.setViewportSize({ width: 1200, height: 900 });
    }
    await check("narrow screens wrap long unbroken names and text without horizontal overflow", async () => {
      const long = waRecord("1".repeat(80) + "@s.whatsapp.net", "N".repeat(120), "U".repeat(400));
      records.push(long);
      await page.setViewportSize({ width: 360, height: 800 });
      await mountInline([long.id]);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const over = await host(long.id).evaluate(el => el.scrollWidth > el.clientWidth + 1);
      assert.equal(over, false);
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "whatsapp-mobile-long.png") });
      await page.setViewportSize({ width: 1200, height: 900 });
    });
    await check("inline Approve sends the unchanged exact bindings after the preview is displayed", async () => {
      await mountInline([msgs[0].id]);
      assert.equal(await approveEnabled(msgs[0].id), true);
      await host(msgs[0].id).locator(".sg-approve").click();
      await page.waitForFunction(() => /Applied/.test(document.querySelector(".inline-host .sg-state-tags")?.textContent || ""));
      assert.equal(posts.length, 1);
      assert.deepEqual({ action: posts[0].action, revision: posts[0].body.proposal_revision, hash: posts[0].body.payload_hash, status: posts[0].body.expected_status_revision, feedback: posts[0].body.feedback },
        { action: "approve", revision: 1, hash: msgs[0].payload_hash, status: 1, feedback: null });
      assert(posts[0].body.idempotency_key);
    });

    // Account email.
    const email = record(10, "Send an email", "account_email_forward", [
      { kind: "structured", resource: resource("fastmail_account", "Support mailbox"), fields: [field("from_address", "support@example.test"), field("to", ["a@example.test", "b@example.test"]),
        field("cc", ["c@example.test"]), field("bcc", ["d@example.test"]), field("subject", "Fwd: Contract <b>draft</b>"), field("body", "Please see below.\nThanks"),
        field("attachments", [{ filename: "plan.pdf", content_type: "application/pdf", sha256: "f".repeat(64), content_base64: "SECRETBYTES" }]), field("forward_message_id", "msg-77")] },
      { kind: "structured", resource: resource("jobs", "Queued job"), fields: [field("program_kind", "email.send"), field("arguments", { to: ["a@example.test"], body: "DUPLICATE-QUEUE-PAYLOAD" }), field("principal_id", "p-1")] },
    ], ["From: support@example.test", "To: a@example.test", "Body: Please see below.\nThanks", "Forward source message msg-77", "Source body:\nOriginal text\nline two\nSource attachments: this line is part of the message", "Source attachments: orig.png"]);
    records.push(email);
    const emptyEmail = record(11, "Send an email", "email_send", [{ kind: "structured", resource: resource("fastmail_account", "Mailbox"), fields: [field("from_address", "me@example.test"), field("to", ["x@example.test"]), field("subject", ""), field("body", "")] }]);
    const badEmail = record(12, "Send an email", "account_email_send", [{ kind: "structured", resource: resource("fastmail_account", "Mailbox"), fields: [field("from_address", "me@example.test"), field("to", []), field("subject", "s"), field("body", "b")] }]);
    records.push(emptyEmail, badEmail);
    await check("account email shows To/Cc/Bcc/subject/body, attachment filename only and the frozen forwarded content; no queue duplicate or bytes", async () => {
      await mountInline([email.id, emptyEmail.id, badEmail.id]);
      const text = await host(email.id).locator(".sg-preview").innerText();
      for (const needle of ["support@example.test", "a@example.test, b@example.test", "c@example.test", "d@example.test", "Fwd: Contract <b>draft</b>", "Please see below.", "plan.pdf", "Original text\nline two", "orig.png"]) assert(text.includes(needle), needle);
      assert.doesNotMatch(text, /DUPLICATE-QUEUE-PAYLOAD|SECRETBYTES|f{16}|email\.send|msg-77/);
      assert.equal(await host(email.id).locator(".sg-email b").count(), 0);
      assert.equal(await host(email.id).getByRole("region", { name: "Source message body", exact: true }).textContent(), "Original text\nline two\nSource attachments: this line is part of the message");
      assert.equal(await host(email.id).locator(".sg-approve").isEnabled(), true);
      assert.equal(await host(email.id).locator(".sg-effects li").count(), 0, "documented effect strings are not duplicated");
    });
    await check("empty subject/body are valid; missing recipients make the preview unavailable", async () => {
      assert.equal(await host(emptyEmail.id).locator(".sg-approve").isEnabled(), true);
      assert.match(await host(emptyEmail.id).locator(".sg-email").innerText(), /No subject[\s\S]*Empty body/);
      assert.match(await host(badEmail.id).locator(".sg-unavailable").textContent(), /Preview unavailable/);
      assert.equal(await host(badEmail.id).locator(".sg-approve").count(), 0);
    });
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "email-desktop.png") });
    await check("a forward without its frozen source content cannot approve", async () => {
      const missingSource = clone(email); missingSource.id = id(13);
      missingSource.preview.effects = missingSource.preview.effects.filter(e => !e.startsWith("Source body:"));
      records.push(missingSource);
      await mountInline([missingSource.id]);
      assert.match(await host(missingSource.id).locator(".sg-unavailable").textContent(), /forwarded email content/);
      assert.equal(await host(missingSource.id).locator(".sg-approve").count(), 0);
    });

    // Generic changes.
    const generic = record(20, "Update task and agent", "tasks_update", [
      { kind: "structured", resource: resource("task", "Task: Ship v1.2"), fields: [field("status", "working", "open"), field("assignee", "Ordubashy", null), field("schedule", { cron: "0 9 * * 1", enabled: true }, { cron: "0 8 * * 1", enabled: false }),
        field("tool_policy.overrides.requests_create", "allow", "deny"), field("payload_hash", "x", "y"), field("api_key", "sk-LIVE-SECRET", "sk-OLD-SECRET"), field("settings", { auth: { refresh_token: "RT-SECRET", mode: "oauth" } }, { auth: { refresh_token: "RT-OLD", mode: "basic" } }),
        field("tags", ["a", "b"], ["a"]), field("updated_at", "2026", "2025"), field("priority", 3, 1), field("due_at", "2026-11-01T09:00:00Z"),
        field("settings.deep", [{ a: { b: { c: { d: { e: { f: { api_key: "DEEP-SECRET", sha256: "INTERNAL-DIGEST", mode: "readable" } } } } } } }])] },
      { kind: "text", resource: resource("document", "agents/x/PLAN.md"), before: "old line\n", after: "new line\nsecond <i>line</i>\n", diff: "--- a\n+++ b\n@@\n-old line\n+new line\n" },
      { kind: "structured", resource: resource("jobs", "Queue"), fields: [field("program_kind", "task.update"), field("arguments", { note: "JOB-ARGS" })] },
    ]);
    const jobOnly = record(21, "Run routine", "routines_run", [{ kind: "structured", resource: resource("jobs", "Routine job"), fields: [field("kind", "routine.run"), field("arguments_json", { routine: "weekly", api_key: "KEY-SECRET" }), field("principal_id", "P"), field("digest", "D")] }]);
    records.push(generic, jobOnly);
    await check("generic changes use readable labels, before → after, a four-row default with Show all, and text before/after disclosure", async () => {
      await mountInline([generic.id, jobOnly.id]);
      const box = host(generic.id).locator(".sg-changes-wrap");
      const text = await box.innerText();
      for (const needle of ["Status", "working", "Assignee", "Ordubashy", "Schedule"]) assert(text.toLowerCase().includes(needle.toLowerCase()), needle);
      assert.equal(await box.locator(":scope > .sg-change-group .sg-change").count() + await box.locator(".sg-more-changes .sg-change").count() >= 8, true);
      assert.equal(await box.locator(":scope > .sg-change-group .sg-change").count(), 4);
      assert.equal(await box.locator("details.sg-more-changes > summary").textContent().then(t => /Show all changes/.test(t)), true);
      assert.doesNotMatch(await page.locator(`.inline-host[data-id="${generic.id}"] .sg-changes-wrap`).evaluate(el => el.textContent), /payload_hash|updated_at|2025/);
      assert.equal(await box.locator("i").count(), 0);
      await box.locator("details.sg-more-changes > summary").click();
      const all = await box.innerText();
      assert.match(all, /new line\nsecond <i>line<\/i>/);
      assert.match(all, /Permission › Overrides › Requests create/); assert.match(all, /Priority/); assert.match(all, /Tags/);
      assert.match(all, /2026-11-01T09:00:00Z/); assert.match(all, /readable/);
      await box.locator(".sg-fulltext > summary").click();
      assert.match(await box.locator(".sg-fulltext").innerText(), /Before[\s\S]*old line[\s\S]*After[\s\S]*new line/);
    });
    await check("secret values are hidden recursively and queue job records are omitted or sanitized", async () => {
      const text = await host(generic.id).locator(".sg-preview").evaluate(el => el.textContent);
      assert.doesNotMatch(text, /sk-LIVE|sk-OLD|RT-SECRET|RT-OLD|JOB-ARGS|DEEP-SECRET|INTERNAL-DIGEST/);
      assert.match(text, /value hidden/i);
      const jobText = await host(jobOnly.id).locator(".sg-preview").evaluate(el => el.textContent);
      assert.match(jobText, /routine\.run/); assert.match(jobText, /weekly/);
      assert.doesNotMatch(jobText, /KEY-SECRET|digest|principal|"D"/);
      assert.match(jobText, /Value hidden/);
    });
    await check("Show all changes keeps its disclosure state across refresh of unchanged content", async () => {
      await page.evaluate(() => Promise.all(inlineViews.map(v => v.refresh())));
      assert.equal(await host(generic.id).locator("details.sg-more-changes").evaluate(el => el.open), true);
    });
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "generic-desktop.png") });

    // Attention review dialog: Review before Approve, with the drawer retired.
    await page.evaluate(() => document.querySelector("#inline-stack").remove());
    records.length = 0; posts.length = 0;
    const r1 = waRecord("120363@g.us", "Team", "Drawer exact text"), r2 = waRecord("120364@g.us", "Other", "Second drawer text");
    records.push(r1, r2);
    await page.evaluate(async () => { await selectGlobal("agents"); await sgAttentionNavigate(null); });
    const row = id => page.locator(`[data-attention-signoffs] .sg-row[data-id="${id}"]`);
    await row(r1.id).waitFor();
    await check("closed cards offer Reject and Review, never an immediate Approve; Review posts nothing", async () => {
      assert.equal(await row(r1.id).locator(".sg-row-actions .sg-approve").count(), 0);
      assert.equal(await row(r1.id).locator(".sg-row-actions [data-sg='review']").textContent(), "Review");
      assert.equal(await row(r1.id).locator(".sg-row-actions .sg-reject").count(), 1);
      await row(r1.id).locator(".sg-row-actions [data-sg='review']").click();
      await row(r1.id).locator(".sg-whatsapp").waitFor();
      assert.equal(posts.length, 0);
      assert.match(await row(r1.id).locator(".sg-whatsapp").innerText(), /Drawer exact text/);
    });
    await check("Approve appears only in the prepared detail and sends exact bindings", async () => {
      assert.equal(await row(r2.id).locator(".sg-approve").count(), 0);
      await row(r1.id).locator(".sg-detail .sg-approve").click();
      await until(() => posts.length === 1);
      assert.deepEqual({ action: posts[0].action, id: posts[0].id, hash: posts[0].body.payload_hash, rev: posts[0].body.proposal_revision, status: posts[0].body.expected_status_revision }, { action: "approve", id: r1.id, hash: r1.payload_hash, rev: 1, status: 1 });
    });
    await check("quick Reject from a closed card still decides without opening", async () => {
      await row(r2.id).waitFor();
      await row(r2.id).locator(".sg-row-actions .sg-reject").click();
      await until(() => posts.length >= 2);
      assert.equal(posts[1].action, "reject");
    });
    await check("incomplete attention preview cannot approve from detail", async () => {
      const bad = waRecord("", "Name", "t"); records.push(bad);
      await page.evaluate(() => sgRefresh());
      await row(bad.id).locator(".sg-row-actions [data-sg='review']").click();
      await row(bad.id).locator(".sg-unavailable").waitFor();
      assert.equal(await row(bad.id).locator(".sg-approve").count(), 0);
    });
    // Input requests keep their own path.
    await check("input requests are unchanged: explanation, choices and submit", async () => {
      const input = record(30, "Which option?", "requests_input", []);
      input.category = "input_request"; input.input = { explanation: "Pick one.", choices: [{ id: "a", label: "A", explanation: "first" }], selection_mode: "single", selection_required: true, notes_required: false };
      input.preview = { title: "q", explanation: "Pick one.", input: input.input };
      records.push(input);
      await page.evaluate(() => sgRefresh());
      await row(input.id).locator(".sg-rowhead").click();
      await row(input.id).locator(".sg-choice").waitFor();
      assert.match(await row(input.id).locator(".sg-preview").innerText(), /Pick one/);
      assert.equal(await row(input.id).locator(".sg-content").count(), 0);
    });
    assert.deepEqual(errors, [], JSON.stringify(errors));
    console.log(`${tests} signoff preview checks passed`);
  } finally {
    if (browser) await browser.close();
    server.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
