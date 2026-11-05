#!/usr/bin/env node
// Isolated dashboard browser contracts. No live API, businesses, terminals or jobs.
// PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_signoffs_browser.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const ROOT = path.join(__dirname, "..");
const OUT = process.env.SIGNOFF_SCREENSHOTS || path.join(__dirname, "artifacts", "signoffs");
const clone = value => JSON.parse(JSON.stringify(value));
const listFixture = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/signoffs.json")));
const configFixture = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/effective-config.json")));
const A = listFixture.data.review_business_id, B = "10000000-0000-4000-8000-000000000002";
const OWNER = "40000000-0000-4000-8000-000000000001";
const AGENT = configFixture.data.effective_config.agent_id;
const ids = listFixture.data.items.map(item => item.id);
const session = { principal_id: OWNER, principal_kind: "human", display_name: "Owner", is_owner: true };
let records, config, posts, replayed, mode, gates, permissionsPosts, errors, attentionOverview, attentionDeliveries, attentionPosts;
const envelope = data => ({ request_id: crypto.randomUUID(), status: "ok", data, warnings: [] });
function reset() {
  records = { alpha: clone(listFixture.data.items), beta: [] };
  config = clone(configFixture.data.effective_config);
  posts = []; replayed = new Map(); permissionsPosts = []; errors = [];
  mode = {}; gates = {}; attentionOverview={businesses:[],urgent:[]}; attentionDeliveries=[]; attentionPosts=[];
}
function reply(res, body, status = 200) {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" }); res.end(JSON.stringify(body));
}
function failure(res, code, message, status = 503, details = {}) { reply(res, { request_id: crypto.randomUUID(), status: "error", error: { code, message, retryable: false, details } }, status); }
function list(slug, category) {
  const selected = records[slug].filter(s => !category || s.category === category);
  const counts = Object.fromEntries(["pending", "approved", "applied", "rejected", "conflicted", "failed"].map(state => [state, selected.filter(s => s.state === state).length]));
  // An older pending proposal beyond the loaded page: the waiting list offers "Load more" only while pending items remain unloaded.
  if (mode.more) counts.pending++;
  return envelope({ review_business_id: slug === "alpha" ? A : B, items: selected.map(s => {
    const { preview, expected_targets, applied_at, apply_result, error, ...row } = s; return row;
  }), counts, next_cursor: mode.more ? "older" : null, updated_cursor: `fixture-${posts.length}`, has_more: false });
}
async function handle(req, res) {
  const request = new URL(req.url, "http://localhost");
  const route = request.pathname;
  if (route.endsWith("/operations/email_mailboxes_list")) return reply(res,envelope({mailboxes:[],agents:[]}));
  if (route.endsWith('/operations/context_reviews_list')) return reply(res,envelope({items:[]}));
  if (route.endsWith('/operations/stories_resource_get')) return reply(res,envelope({resource:null,links:[]}));
  if (route.startsWith('/api/me/attention/') && req.method === 'POST') {
    let raw=''; for await (const chunk of req) raw+=chunk;
    if (mode.attentionError) return failure(res,'unavailable','Notification update failed.');
    attentionPosts.push({route,body:JSON.parse(raw)}); attentionDeliveries=[];
    return reply(res,envelope({ok:true}));
  }
  if (route === "/api/me/attention") return reply(res,envelope(attentionOverview));
  if (route === "/api/me/attention/deliveries") return reply(res,envelope({items:attentionDeliveries}));
  if (route === "/api/session") return mode.unauthorized ? failure(res, "unauthorized", "Your owner session has expired.", 401) : reply(res, envelope(session));
  if (route === "/api/me/tasks") return reply(res, {principal_id: OWNER, tasks: []});
  if (route === "/api/biz") return reply(res, [{ id: A, slug: "alpha", name: "Atlas Research", owner: "owner", agents: ["onbashy"], agent_meta: {} }, { id: B, slug: "beta", name: "Beacon Studio", owner: "owner", agents: ["onbashy"], agent_meta: {} }]);
  if (route.endsWith("/agents")) return reply(res, [{ agent_id: AGENT, name: "onbashy", display_name: "Onbashy" }]);
  if (route.endsWith("/config")) return reply(res, envelope({ effective_config: config }));
  if (route.endsWith("/permissions")) {
    let raw = ""; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw); permissionsPosts.push(body);
    if (mode.permissionConflict) return failure(res, "revision_conflict", "The agent profile changed.", 409);
    if (body.expected_revision !== config.profile_revision) return failure(res, "revision_conflict", "The agent profile changed.", 409);
    config.tool_policy.overrides = body.overrides; config.profile_revision++;
    config.tools.forEach(tool => { if (body.overrides[tool.canonical_operation]) { tool.effective_mode = body.overrides[tool.canonical_operation]; tool.source = { kind: "profile_override", profile_revision: config.profile_revision }; } });
    return reply(res, envelope({ agent_id: AGENT, profile_revision: config.profile_revision, overrides: body.overrides, effective_config_id: config.effective_config_id }));
  }
  if (route.endsWith("/operations/signoffs_platform_cancel")) {
    let raw = ""; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw); posts.push({ action: "cancel", body });
    if (replayed.has(body.idempotency_key)) return reply(res, replayed.get(body.idempotency_key));
    const item = records.alpha.find(s => s.id === body.signoff_id);
    assert.equal(body.status_revision, item.status_revision);
    item.status_revision++; item.platform.execution.cancellation = { state: "queued" };
    const result = envelope({signoff_id:item.id, execution:item.platform.execution});
    replayed.set(body.idempotency_key, result);
    if (mode.loseExecution) { mode.loseExecution = false; res.writeHead(200, {"Content-Type":"application/json"}); res.end("{"); return; }
    return reply(res, result);
  }
  // Retained shell reads: reply notifications and the (idle) event stream.
  if (route === "/api/root/attention-queue") return reply(res, envelope({ items: [] }));
  if (route === "/api/root/reply-notifications") return reply(res, envelope({ items: [] }));
  if (route === "/api/root/events") { res.writeHead(200, { "Content-Type": "text/event-stream" }); res.write(": idle\n\n"); return; }
  if (route.endsWith("/env")) return reply(res, { pairs: [] });
  if (route.endsWith("/tasks") || route.endsWith("/log")) return reply(res, []);
  if (route.endsWith("/live")) return reply(res, { sessions: [], requests: [], routine_failures: [] });
  const signoff = /^\/api\/biz\/(alpha|beta)\/signoffs(?:\/([^/]+)(?:\/(approve|reject|retry|respond))?)?$/.exec(route);
  if (signoff) {
    const [, slug, id, action] = signoff;
    if (!id) {
      if (mode.listError) return failure(res, "unavailable", "Signoff storage is unavailable.");
      const response = list(slug, request.searchParams.get("category"));
      if (request.searchParams.has("cursor")) { response.data.items = [clone(records[slug][0])]; response.data.items[0].id = "30000000-0000-4000-8000-000000000099"; response.data.items[0].state = "rejected"; response.data.next_cursor = null; }
      if (mode.holdList && slug === "alpha") { mode.holdList = false; gates.list = () => reply(res, response); return; }
      return reply(res, response);
    }
    const item = records[slug].find(s => s.id === id);
    if (!item) return failure(res, "not_found", "Proposal not found.", 404);
    if (!action) {
      if (mode.detailError) return failure(res, "unavailable", "The prepared detail is unavailable.");
      if (mode.holdDetail) { mode.holdDetail = false; const response = envelope({ signoff: clone(item) }); gates.detail = () => reply(res, response); return; }
      return reply(res, envelope({ signoff: item }));
    }
    let raw = ""; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw); posts.push({ slug, id, action, body });
    if (mode.conflict) {
      item.state = "rejected"; item.status_revision++; item.decision = { kind: "reject", by: OWNER, at: "2026-09-08T10:00:00Z", feedback: "Reviewed in another tab." };
      return failure(res, "stale_signoff", "This proposal has already been rejected.", 409, { signoff: { id, state: item.state, status_revision: item.status_revision } });
    }
    if (replayed.has(body.idempotency_key)) return reply(res, envelope({ signoff: item, replayed: true }));
    if (body.expected_status_revision !== item.status_revision) return failure(res, "stale_signoff", "The proposal changed.", 409, { signoff: item });
    replayed.set(body.idempotency_key, true);
    item.state = action === "respond" ? "answered" : action === "reject" ? "rejected" : "approved"; item.status_revision++;
    item.updated_sequence++;
    if (action !== "retry") item.decision = { kind: action, by: OWNER, at: "2026-09-08T10:00:00Z", feedback: body.feedback, notes:body.notes, selected_choices:body.selected_choices };
    const response = envelope({ signoff: clone(item), replayed: false }); response.status = action === "reject" ? "ok" : "accepted";
    const finish = () => reply(res, response, action === "reject" ? 200 : 202);
    if (mode.dropWrite) { mode.dropWrite = false; res.destroy(); return; }
    if (mode.holdWrite) { mode.holdWrite = false; gates.write = finish; return; }
    return finish();
  }
  if (route.startsWith("/api/")) { errors.push(`Unexpected API ${req.method} ${route}`); return failure(res, "not_found", "No fixture for this route.", 404); }
  const relative = route === "/" ? "index.html" : route.slice(1);
  const target = path.resolve(ROOT, relative);
  if (!target.startsWith(ROOT + path.sep)) { res.writeHead(403); res.end(); return; }
  try {
    const contents = fs.readFileSync(target);
    res.writeHead(200, { "Content-Type": target.endsWith(".css") ? "text/css" : target.endsWith(".js") ? "text/javascript" : "text/html" }); res.end(contents);
  } catch { res.writeHead(404); res.end(); }
}
function extra(state, n, attributes = {}) {
  return { ...clone(records.alpha[0]), id: `30000000-0000-4000-8000-${String(n).padStart(12, "0")}`, title: `${state[0].toUpperCase() + state.slice(1)} prepared change`, state, status_revision: 2,
    decision: state === "rejected" ? { kind: "reject", by: OWNER, at: "2026-09-08T08:00:00Z", feedback: "Retain the existing assignment." } : { kind: "approve", by: OWNER, at: "2026-09-08T08:00:00Z", feedback: null }, ...attributes };
}
const row = (page, id) => page.locator(`.sg-row[data-id="${id}"]`);
const wait = async condition => { for (let i = 0; i < 100; i++) { if (condition()) return; await new Promise(resolve => setTimeout(resolve, 20)); } throw new Error("Timed out waiting for isolated fixture"); };
(async () => {
  reset(); fs.mkdirSync(OUT, { recursive: true });
  const server = http.createServer((req, res) => handle(req, res).catch(error => { console.error(error); if (!res.headersSent) res.writeHead(500); res.end(); }));
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser, context, page, tests = 0, navigation = 0;
  try {
    browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
    context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route("**/*", route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
    await context.addInitScript(() => {
      localStorage.setItem("tab", "settings"); localStorage.setItem("tasks", "1"); localStorage.setItem("tview", "signoffs");
      Object.defineProperty(crypto, "randomUUID", { value: undefined });
    });
    page = await context.newPage();
    page.setDefaultTimeout(8000);
    page.on("pageerror", error => errors.push(error.message));
    const fresh = async () => { await page.goto(origin + `/?fixture=${++navigation}#alpha`); await page.locator(".sg-row").first().waitFor(); };
    const check = async (label, fn) => { if(process.env.SIGNOFF_TEST_FILTER && !label.includes(process.env.SIGNOFF_TEST_FILTER))return; await fn(); tests++; console.log(`ok ${label}`); };
    await fresh();
    // Platform execution controls are mounted inline (Focus, Chat, attention queue); the retired Marketing modal is not involved.
    const mountExecution = item => page.evaluate(item => {
      document.querySelector("#sg-execution")?.remove();
      const host = document.createElement("div"); host.id = "sg-execution"; host.style.cssText = "position:fixed;inset:0;z-index:99999;overflow:auto;background:#101418"; document.body.append(host);
      window.sgExecutionTarget = { id: item.id, businessId: item.review_business_id };
      window.sgExecutionView = sgCreateView({ category: "platform_action", prefix: "test-execution-", execution: true, waitingOnly: true,
        item: () => sgExecutionTarget, active: () => host.isConnected, body: () => host });
      sgExecutionView.paint();
    }, item);
    await check("HTTP-compatible request IDs work without crypto.randomUUID", async () => {
      const keys = await page.evaluate(() => [typeof crypto.randomUUID, newRequestId(), newRequestId()]);
      assert.equal(keys[0], "undefined");
      for (const key of keys.slice(1)) assert.match(key, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
      assert.notEqual(keys[1], keys[2]);
    });
    await check("selected-business panel after Journal retirement, no modal and unchanged task count", async () => {
      assert.deepEqual(await page.locator('.ttab').evaluateAll(tabs=>tabs.map(tab=>tab.getAttribute('aria-label'))),['Global Notes','Project notes','Tasks','Signoffs','Uploads','Routines','Calendar','Charts','Costs','Tables','Activity']);
      assert.equal(await page.locator(".sg-row .business-label").first().textContent(), "Atlas Research");
      assert.equal(await page.locator("#thandle, #tcount").count(), 0);
      assert.equal(await page.locator(".overlay").count(), 0);
    });
    await check("closed signoffs offer Review, never an instant Approve; Review posts nothing and Approve then sends the unchanged bindings", async () => {
      const card = row(page, ids[0]);
      assert.equal(await card.locator(".sg-approve").count(), 0);
      assert.equal(await card.locator(".sg-row-actions .sg-reject:visible").isEnabled(), true);
      await card.locator('[data-sg="review"]').click();
      await card.locator(".sg-content").waitFor();
      assert.equal(posts.length, 0);
      await card.locator(".sg-approve:visible").click();
      await card.waitFor({ state: "detached" });
      assert.equal(posts.length, 1);
      assert.deepEqual([posts[0].body.proposal_revision, posts[0].body.payload_hash, posts[0].body.expected_status_revision], [1, records.alpha[0].payload_hash, 1]);
      reset(); await fresh();
    });
    await check("click reveals prepared content and optional feedback without scope, raw patches or hashes", async () => {
      await row(page, ids[0]).locator(".sg-rowhead").click();
      await row(page, ids[0]).locator(".sg-feedback").waitFor();
      assert.equal(posts.length, 0);
      assert.equal(await row(page, ids[0]).locator("textarea.sg-feedback").count(), 1);
      assert.equal(await row(page, ids[0]).locator("input").count(), 0);
      assert.equal(await row(page, ids[0]).locator(".sg-impact").count(), 0);
      assert.equal(await row(page, ids[0]).locator("pre, .sg-fields, .sg-targets").count(), 0);
      assert(! (await row(page, ids[0]).textContent()).includes(records.alpha[0].payload_hash));
      assert.equal(await page.locator(".sg-decided").count(), 0);
      assert.equal(await row(page, ids[0]).evaluate(el => el.classList.contains("mkcard")), true);
      assert.equal(await row(page, ids[0]).locator(".sg-rationale p").evaluate(el => getComputedStyle(el).fontSize), "14px");
      await page.screenshot({ path: path.join(OUT, "desktop-pending.png"), animations: "disabled" });
      assert.match(await row(page, ids[0]).locator(".sg-rowmeta").innerHTML(), /Onbashy/);
      assert.deepEqual(await page.evaluate(() => {
        const panel = document.querySelector("#taskspanel").getBoundingClientRect();
        const count = document.querySelector(".sg-row .sg-rowhead").getBoundingClientRect();
        return [panel.left >= 0, panel.right <= innerWidth, Math.abs(panel.width - innerWidth * .85) < 1,
          count.left >= panel.left, count.right <= innerWidth, count.top >= 0, count.bottom <= innerHeight];
      }), [true, true, true, true, true, true, true]);
      await row(page, ids[0]).locator(".sg-actionarea .sg-actions").scrollIntoViewIfNeeded();
      assert.equal(await row(page, ids[0]).locator(".sg-actionarea .sg-actions").evaluate(el => [...el.querySelectorAll("button")].every(button => {
        const r = button.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight;
      })), true);
      await page.screenshot({ path: path.join(OUT, "desktop-actions.png"), animations: "disabled" });
    });
    await check("native keyboard order runs row head, scrollable proposed text, disclosure, then feedback", async () => {
      await row(page, ids[0]).locator("textarea.sg-feedback").focus();
      const stops = [];
      for (let i = 0; i < 4 && stops.at(-1) !== "sg-rowhead"; i++) {
        await page.keyboard.press("Shift+Tab");
        stops.push(await page.evaluate(() => document.activeElement.className || document.activeElement.tagName));
      }
      assert.deepEqual(stops, ["SUMMARY", "sg-text", "sg-rowhead"]);
    });
    await check("background refresh keeps the same textarea, draft, selection and scroll", async () => {
      await row(page, ids[0]).locator("textarea.sg-feedback").fill("  Keep this exact draft.\nSecond line.  ");
      await page.evaluate(() => { const tx = document.querySelector(".sg-feedback"); window.fixtureTextarea = tx; tx.setSelectionRange(3, 9); });
      const before = await page.locator("#tbody").evaluate(el => el.scrollTop);
      await page.evaluate(() => sgRefresh());
      assert.equal(await page.evaluate(() => window.fixtureTextarea === document.querySelector(".sg-feedback")), true);
      assert.equal(await page.locator(".sg-feedback").inputValue(), "  Keep this exact draft.\nSecond line.  ");
      assert.deepEqual(await page.locator(".sg-feedback").evaluate(el => [el.selectionStart, el.selectionEnd]), [3, 9]);
      assert.equal(await page.locator("#tbody").evaluate(el => el.scrollTop), before);
    });
    await check("approve retains submitted notes, locks duplicate clicks and removes the decided card", async () => {
      mode.holdWrite = true;
      await row(page, ids[0]).locator(".sg-approve:visible").click(); await wait(() => gates.write);
      assert.equal(await row(page, ids[0]).locator(".sg-approve:visible").isDisabled(), true);
      assert.equal(await row(page, ids[0]).locator("textarea.sg-feedback").inputValue(), "  Keep this exact draft.\nSecond line.  ");
      assert.equal(posts.length, 1);
      assert.deepEqual(Object.keys(posts[0].body).sort(), ["expected_status_revision", "feedback", "idempotency_key", "payload_hash", "proposal_revision"]);
      gates.write(); await row(page, ids[0]).waitFor({ state: "detached" });
      records.alpha[0].state = "applied"; records.alpha[0].status_revision++; records.alpha[0].applied_at = "2026-09-08T10:01:00Z";
      await page.evaluate(() => sgRefresh());
      assert.equal(await row(page, ids[0]).count(), 0);
      assert.equal(await page.locator(".toasts").textContent(), "");
    });
    await check("non-agent attribution remains visible and malicious preview text stays escaped", async () => {
      await row(page, ids[1]).locator(".sg-rowhead").click();
      await row(page, ids[1]).locator("textarea.sg-feedback").waitFor();
      assert.equal(await row(page, ids[1]).locator(".sg-impact").count(), 0);
      assert.match(await row(page, ids[1]).textContent(), /Proposer's explanation/);
      assert.match(await row(page, ids[1]).locator(".sg-rowmeta").innerHTML(), /Shared instructions worker \/ initiated by Onbashy/);
      assert.equal(await page.evaluate(() => window.bad), undefined);
      assert.equal(await row(page, ids[1]).locator("script").count(), 0);
      await page.screenshot({ path: path.join(OUT, "desktop-global.png"), animations: "disabled" });
    });
    await check("reject stores optional feedback (surrounding whitespace trimmed) for the agent and removes the decided card", async () => {
      const feedback = "  Keep it unchanged.\n  Exact whitespace.  ";
      await row(page, ids[1]).locator("textarea.sg-feedback").fill(feedback);
      await row(page, ids[1]).locator(".sg-reject:visible").click();
      await row(page, ids[1]).waitFor({ state: "detached" });
      assert.equal(posts.at(-1).body.feedback, feedback.trim());
      assert.equal(records.alpha[1].decision.feedback, feedback.trim());
      assert.equal(await page.locator(".sg-row").count(), 0);
    });
    await check("only pending signoffs appear, not historical outcomes", async () => {
      records.alpha.push(extra("failed", 3, { error: { code: "file_busy", message: "One prepared file remains locked.", retryable: true }, apply_result: { effects: "partial" } }));
      records.alpha.push(extra("conflicted", 4, { error: { code: "target_changed", message: "The agent profile changed.", retryable: false } }));
      await page.evaluate(() => sgRefresh());
      assert.equal(await page.locator(".sg-row").count(), 0);
      await page.locator(".sg-empty").filter({ hasText: "No signoffs waiting." }).waitFor();
    });
    await check("unchanged index metadata is a valid prepared proposal with approval enabled", async () => {
      reset();
      records.alpha[0].affected_scope = { kind: "business", business_id: A };
      records.alpha[0].preview.affected_scope = records.alpha[0].affected_scope;
      const documentId = "60000000-0000-4000-8000-000000000001";
      records.alpha[0].preview.resources.push({ resource_id: documentId, label: "context/INDEX.md" });
      records.alpha[0].preview.changes.push({ kind: "structured", resource: { resource_id: documentId, label: documentId }, fields: [] });
      records.alpha[0].preview.changes.push({ kind: "structured", resource: { label: "context/INDEX.md metadata" }, fields: [] });
      records.alpha[0].preview.changes.push({ kind: "text", resource: { label: "context/INDEX.md" }, before: "Unchanged", after: "Unchanged", diff: "" });
      records.alpha[0].expected_targets[0].content_hash = "d".repeat(64);
      await fresh(); await row(page, ids[0]).locator(".sg-rowhead").click();
      await row(page, ids[0]).locator(".sg-approve:visible").waitFor();
      assert.equal(await row(page, ids[0]).locator(".sg-approve:visible").isEnabled(), true);
      assert.equal(await row(page, ids[0]).locator(".sg-impact").count(), 0);
      assert(! (await row(page, ids[0]).textContent()).includes("d".repeat(64)));
      await row(page, ids[0]).locator(".sg-approve:visible").click();
      await row(page, ids[0]).waitFor({ state: "detached" });
      assert.equal(posts[0].body.payload_hash, records.alpha[0].payload_hash);
    });
    await check("missing and unsupported prepared changes still prevent approval", async () => {
      for (const changes of [[], [null], [{ kind: "unsupported" }], [{ kind: "text", before: "old", diff: "" }]]) {
        reset(); records.alpha[0].preview.changes = changes;
        await fresh(); await row(page, ids[0]).locator(".sg-rowhead").click();
        await row(page, ids[0]).locator(".sg-error").filter({ hasText: "Reload it before approving" }).waitFor();
        assert.equal(await row(page, ids[0]).locator(".sg-approve:visible:not(:disabled)").count(), 0);
        assert.equal(posts.length, 0);
      }
    });
    await check("list and detail failures are inline and disable cached decisions", async () => {
      reset(); await fresh(); await row(page, ids[0]).locator(".sg-rowhead").click(); await row(page, ids[0]).locator("textarea.sg-feedback").waitFor();
      mode.listError = true; await page.evaluate(() => sgRefresh());
      assert.equal(await row(page, ids[0]).locator(".sg-approve:visible:not(:disabled)").count(), 0);
      assert.match(await page.locator(".sg-listnotice").textContent(), /Signoff storage is unavailable/);
      mode.listError = false; await page.locator('[data-sg="list"]').click(); await page.locator(".sg-listnotice .sg-error").waitFor({ state: "detached" });
      await page.locator('.sg[aria-busy="false"]').waitFor();
      await row(page, ids[0]).locator(".sg-rowhead").click(); await row(page, ids[0]).locator("textarea.sg-feedback").waitFor();
      mode.detailError = true; await page.evaluate(() => sgRefresh());
      await row(page, ids[0]).locator(".sg-detailnotice .sg-error").waitFor();
      assert.equal(await row(page, ids[0]).locator(".sg-approve:visible:not(:disabled)").count(), 0);
      mode.detailError = false; await row(page, ids[0]).locator('[data-sg="detail"]').click(); await page.locator(".sg-detailnotice .sg-error").waitFor({ state: "detached" });
    });
    await check("business switch invalidates old rows before loading and ignores a late write", async () => {
      mode.holdWrite = true; await row(page, ids[0]).locator("textarea.sg-feedback").fill("A feedback");
      await row(page, ids[0]).locator(".sg-reject:visible").click(); await wait(() => gates.write);
      const synchronous = await page.evaluate(async () => { await selectBiz("beta"); return document.querySelectorAll(".sg-row").length; });
      assert.equal(synchronous, 0);
      await page.waitForFunction(() => state.biz === "beta"); await page.locator('.sg[aria-busy="false"]').waitFor();
      gates.write(); await page.waitForTimeout(60);
      assert.equal(await page.locator(".sg-row").count(), 0); assert.equal(posts.at(-1).slug, "alpha");
      await page.evaluate(() => selectBiz("alpha"));
      await page.waitForFunction(() => state.biz === "alpha");
      await row(page, ids[1]).waitFor();
      assert.equal(await row(page, ids[0]).count(), 0);
      assert.equal(records.alpha[0].decision.feedback, "A feedback");
    });
    await check("drafts are isolated by owner, business, proposal revision and hash", async () => {
      reset(); await fresh(); await row(page, ids[0]).locator(".sg-rowhead").click(); await row(page, ids[0]).locator("textarea.sg-feedback").fill("First draft");
      await row(page, ids[1]).locator(".sg-rowhead").click(); await row(page, ids[1]).locator("textarea.sg-feedback").fill("Second draft");
      await row(page, ids[0]).locator(".sg-rowhead").click(); assert.equal(await row(page, ids[0]).locator("textarea.sg-feedback").inputValue(), "First draft");
      const keys = await page.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith("sgdraft:")));
      assert(keys.every(key => key.includes(OWNER) && key.includes(A)));
      records.alpha[0].proposal_revision = 2; records.alpha[0].payload_hash = "c".repeat(64);
      await fresh(); await row(page, ids[0]).locator(".sg-rowhead").click(); await row(page, ids[0]).locator("textarea.sg-feedback").waitFor();
      assert.equal(await row(page, ids[0]).locator("textarea.sg-feedback").inputValue(), "");
    });
    await check("lost decision response reconciles with GET without a second effect", async () => {
      reset(); await fresh(); await row(page, ids[0]).locator(".sg-rowhead").click(); await row(page, ids[0]).locator("textarea.sg-feedback").waitFor();
      mode.dropWrite = true; await row(page, ids[0]).locator(".sg-approve:visible").click();
      await row(page, ids[0]).waitFor({ state: "detached" });
      assert(posts.length >= 1); assert.equal(new Set(posts.map(post => post.body.idempotency_key)).size, 1); assert.equal(replayed.size, 1);
    });
    await check("a stale opposing decision removes the card and preserves the winning feedback", async () => {
      reset(); await fresh(); await row(page, ids[0]).locator(".sg-rowhead").click(); await row(page, ids[0]).locator("textarea.sg-feedback").waitFor();
      mode.conflict = true; await row(page, ids[0]).locator(".sg-approve:visible").click();
      await row(page, ids[0]).waitFor({ state: "detached" });
      assert.equal(records.alpha[0].decision.feedback, "Reviewed in another tab.");
    });
    await check("late list and detail responses cannot repaint another business", async () => {
      reset(); await fresh(); mode.holdList = true; await page.evaluate(() => { sgRefresh(); }); await wait(() => gates.list);
      await page.evaluate(() => selectBiz("beta")); gates.list(); await page.waitForTimeout(60);
      assert.equal(await page.locator(".sg-row").count(), 0);
      await page.evaluate(() => selectBiz("alpha")); await row(page, ids[0]).locator(".sg-rowhead").waitFor();
      mode.holdDetail = true; await row(page, ids[0]).locator(".sg-rowhead").click(); await wait(() => gates.detail);
      await page.evaluate(() => selectBiz("beta")); gates.detail(); await page.waitForTimeout(60);
      assert.equal(await page.locator(".sg-row").count(), 0);
    });
    await check("pagination excludes older outcomes and empty queues stay business scoped", async () => {
      reset(); mode.more = true; await fresh(); await page.locator(".sg-more").click();
      await page.locator(".sg-more").waitFor({ state: "hidden" });
      assert.equal(await row(page, "30000000-0000-4000-8000-000000000099").count(), 0);
      await page.evaluate(() => selectBiz("beta")); await page.locator(".sg-empty").filter({ hasText: "No signoffs waiting." }).waitFor();
      assert.equal(await page.locator(".sg-row").count(), 0, "an empty business queue shows no rows");
    });
    await check("mobile previews, actions, tabs and Settings do not overlap", async () => {
      reset(); await page.setViewportSize({ width: 390, height: 844 }); await fresh();
      await row(page, ids[1]).locator(".sg-rowhead").click(); await row(page, ids[1]).locator("textarea.sg-feedback").waitFor();
      await page.screenshot({ path: path.join(OUT, "mobile-signoffs.png"), animations: "disabled" });
      await row(page, ids[1]).locator(".sg-actionarea .sg-actions").scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(OUT, "mobile-actions.png"), animations: "disabled" });
      const layout = await page.evaluate(() => {
        const panel = document.querySelector("#taskspanel").getBoundingClientRect();
        const tabs = document.querySelector(".ttabs").getBoundingClientRect();
        const sg = document.querySelector(".sg");
        const buttons = [...document.querySelectorAll(".sg-actionarea .sg-actions button")].map(el => el.getBoundingClientRect());
        return { panel: panel.width, width: innerWidth, tabsRight: tabs.right, fits: sg.scrollWidth <= sg.clientWidth, noOverlap: buttons.length === 2 && buttons[0].right <= buttons[1].left };
      });
      assert.equal(layout.panel, layout.width); assert(layout.tabsRight <= layout.width); assert(layout.fits); assert(layout.noOverlap);
      await page.locator(".ttab[data-v=activity]").focus();
      // The eleven drawer tabs scroll inside their strip: a focused tab is scrolled into the strip's visible bounds.
      assert(await page.locator(".ttab[data-v=activity]").evaluate(el => { const r = el.getBoundingClientRect(), strip = el.closest(".ttabs").getBoundingClientRect(); return r.left >= strip.left - 1 && r.right <= strip.right + 1; }));
      await page.evaluate(() => openTasks(false)); await page.screenshot({ path: path.join(OUT, "mobile-settings.png"), animations: "disabled" });
      assert.equal(await page.locator(".settingspane").evaluate(el => el.scrollWidth <= el.clientWidth), true);
    });
    await check("keyboard Escape preserves feedback, closes the panel and releases focus", async () => {
      reset(); await fresh();
      await row(page, ids[0]).locator(".sg-rowhead").click(); await row(page, ids[0]).locator("textarea.sg-feedback").fill("Keep this draft on close.");
      await page.keyboard.press("Escape");
      assert.equal(await page.locator("#taskswrap").evaluate(el => el.classList.contains("open")), true);
      await page.keyboard.press("Escape");
      assert.equal(await page.locator("#taskswrap").evaluate(el => el.classList.contains("open")), false);
      // The corner launcher is removed; closing releases focus from the drawer.
      assert.equal(await page.evaluate(() => !!document.activeElement.closest("#taskspanel")), false);
      await page.evaluate(() => openTasks(true)); await row(page, ids[0]).locator(".sg-rowhead").click(); await row(page, ids[0]).locator("textarea.sg-feedback").waitFor();
      assert.equal(await row(page, ids[0]).locator("textarea.sg-feedback").inputValue(), "Keep this draft on close.");
      await page.locator(".ttab[data-v=signoffs]").focus(); await page.evaluate(() => tSetView("signoffs"));
      assert.equal(await page.evaluate(() => document.activeElement.id), "ttab-signoffs");
    });
    await check("no business clears all proposal content and mutation controls", async () => {
      await page.evaluate(() => { state.biz = null; sgBusinessChanged(); });
      assert.equal(await page.locator(".sg-row").count(), 0);
      assert.match(await page.locator(".sg").textContent(), /Pick a project first/);
      assert.equal(await page.locator(".sg-row").count(), 0);
      await page.evaluate(() => sgRefresh());
      assert.equal(await page.locator(".sg-approve, .sg-reject").count(), 0);
      await page.screenshot({ path: path.join(OUT, "no-business.png"), animations: "disabled" });
    });
    await check("an inline platform execution keeps its controls and replays a lost cancellation response with the same key", async () => {
      reset();
      const item = records.alpha[0];
      item.category = "platform_action"; item.state = "applied"; item.decision_state = "approved"; item.execution_state = "dispatched";
      item.platform = { submission: {tool:"email.send",route:"fleet",payload:{to:["person@example.com"],subject:"Approved subject",body:"Approved bytes"}}, execution:{state:"dispatched"} };
      await fresh(); await mountExecution(item);
        const execution = page.locator(`#sg-execution .sg-row[data-id="${item.id}"]`);
      await execution.locator('[data-sg="cancel-execution"]').waitFor();
      assert.match(await execution.locator(".sg-state").textContent(), /Approved · Awaiting delivery result/);
      mode.loseExecution = true;
      page.once("dialog", dialog => dialog.accept());
      await execution.locator('[data-sg="cancel-execution"]').click();
      await execution.locator('[data-sg="check-execution"]').waitFor();
      await execution.locator('[data-sg="check-execution"]').click();
      await execution.locator(".sg-notice").filter({hasText:"Cancellation: queued"}).waitFor();
      assert.equal(posts.length, 2);
      assert.deepEqual(posts[0].body, posts[1].body);
      assert.equal(item.execution_state, "dispatched");
      assert.equal(await execution.locator('[data-sg="cancel-execution"]').count(), 0);
      await page.screenshot({path:path.join(OUT,"platform-execution.png"),animations:"disabled"});
    });
    await check("removed integrations preserve readable Signoff history without decision controls", async () => {
      reset();
      const item = records.alpha[0];
      item.category = "platform_action";
      item.actionable = false;
      item.platform = { available: false, execution: { historical: true } };
      item.preview.rationale = "Retained explanation from earlier work.";
      await fresh();
      await row(page, item.id).locator(".sg-rowhead").click();
      await row(page, item.id).getByText("Retained explanation from earlier work.", {exact:true}).waitFor();
      assert.match(await row(page, item.id).textContent(), /integration is no longer installed/);
      assert.equal(await row(page, item.id).locator(".sg-detailnotice .sg-error").count(), 0);
      assert.equal(await row(page, item.id).locator(".sg-approve, .sg-reject, [data-sg=retry-execution]").count(), 0);
      assert.equal(posts.length, 0);
    });
    await check("expired session displays an inline authentication error without mutation controls", async () => {
      reset(); mode.unauthorized = true; await page.goto(origin + `/?fixture=${++navigation}#alpha`);
      await page.locator(".sg-error").filter({ hasText: "Your owner session has expired." }).waitFor();
      assert.equal(await page.locator(".sg-approve:visible").count(), 0);
    });
    await check("input choices and notes save only on Submit, with fixed geometry and retained decisions", async () => {
      reset(); await page.setViewportSize({width:1440,height:1000});
      const choices=[{id:'fast',label:'Fast supplier',explanation:'Available today.'},
        {id:'careful',label:'Careful supplier',explanation:'A long explanation that must occupy no more than two lines even when the text is much longer than the available width. '.repeat(4)}];
      records.alpha.forEach((r,index)=>{
        r.category='input_request'; r.input={title:r.title,explanation:'Choose the supplier. [Open the homepage mockups](http://qorqut:8857/media/compare.html). [Review gallery](http://qorqut:8857/) <img src=x onerror=alert(1)> [bad](javascript:alert(1))',kind:'question',selection_mode:index ? 'multiple':'single',choices,selection_required:true,notes_required:false};
        r.preview={title:r.title,explanation:r.input.explanation,input:r.input};
      });
      await fresh();
      await row(page,ids[0]).locator('.sg-rowhead').click();
      const explanation = row(page,ids[0]).locator('.sg-rationale');
      const reference = explanation.getByRole('link',{name:'Open the homepage mockups'});
      assert.equal(await reference.getAttribute('href'),'http://qorqut:8857/media/compare.html');
      assert.equal(await reference.getAttribute('target'),'_blank');
      assert.equal(await reference.getAttribute('rel'),'noopener noreferrer');
      assert.equal(await explanation.locator('a').count(),2);
      assert.equal(await explanation.locator('img,script').count(),0);
      await row(page,ids[0]).locator('[data-choice="fast"]').click();
      assert.equal(await row(page,ids[0]).locator('[data-choice="fast"]').getAttribute('aria-checked'),'true');
      await page.keyboard.press('ArrowDown');
      assert.equal(await row(page,ids[0]).locator('[data-choice="careful"]').getAttribute('aria-checked'),'true');
      assert.equal(await row(page,ids[0]).locator('[data-choice="fast"]').getAttribute('aria-checked'),'false');
      await row(page,ids[0]).locator('textarea.sg-feedback').fill('My submitted note');
      assert.equal(posts.length,0);
      const geometry=await row(page,ids[0]).locator('.sg-choice').evaluateAll(rows=>rows.map(r=>({height:r.getBoundingClientRect().height,
        label:parseFloat(getComputedStyle(r.querySelector('.sg-choice-name')).fontSize),
        explanation:parseFloat(getComputedStyle(r.querySelector('.sg-choice-explanation')).fontSize),
        lines:r.querySelector('.sg-choice-explanation').getBoundingClientRect().height,
        title:r.querySelector('.sg-choice-explanation').title})));
      assert.equal(geometry[0].height,geometry[1].height);
      assert(geometry.every(g=>g.label-g.explanation===2 && g.lines===40));
      assert.equal(geometry[1].title,choices[1].explanation);
      await row(page,ids[1]).locator('.sg-rowhead').click();
      assert.equal(await row(page,ids[0]).locator('.sg-detail').isVisible(),false);
      await row(page,ids[1]).locator('[data-choice="fast"]').click();
      await row(page,ids[1]).locator('[data-choice="careful"]').click();
      assert.equal(await row(page,ids[1]).locator('[aria-checked="true"]').count(),2);
      assert.equal(await row(page,ids[1]).locator('.sg-choice-ball').first().evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(255, 255, 255)');
      await row(page,ids[1]).locator('[data-choice="fast"]').click();
      assert.equal(await row(page,ids[1]).locator('[aria-checked="true"]').count(),1);
      await page.locator('.ttab[data-v=signoffs]').click();
      assert.equal(await row(page,ids[1]).locator('.sg-detail').isVisible(),false);
      await row(page,ids[0]).locator('.sg-rowhead').click();
      assert.equal(await row(page,ids[0]).locator('textarea.sg-feedback').inputValue(),'My submitted note');
      await page.screenshot({path:path.join(OUT,'input-desktop.png')});
      await row(page,ids[0]).locator('[data-sg="respond"]').click();
      await row(page,ids[0]).waitFor({state:'detached'});
      assert.equal(posts.length,1); assert.equal(posts[0].body.notes,'My submitted note');
      assert.deepEqual(posts[0].body.selected_choices,['careful']);
      assert.equal(await page.locator('[data-sg="responded"]').count(),0);
      assert.equal(records.alpha[0].decision.notes,'My submitted note');
      assert.deepEqual(records.alpha[0].decision.selected_choices,['careful']);
      await row(page,ids[1]).locator('.sg-rowhead').click();
      await row(page,ids[1]).locator('.sg-choice:not(:disabled)').first().waitFor();
      assert.equal(await row(page,ids[1]).locator('.sg-error').count(),0);
      await page.setViewportSize({width:390,height:844});
      await page.screenshot({path:path.join(OUT,'input-mobile.png')});
      const sizes=await row(page,ids[1]).locator('.sg-choice').evaluateAll(rows=>rows.map(r=>r.getBoundingClientRect().height));
      assert.equal(sizes[0],sizes[1]);
      assert.equal(posts.length,1);
    });
    await check("cross-business urgent badges, exact navigation, snooze, and grouped summary destinations", async () => {
      reset(); await page.setViewportSize({width:1440,height:1000});
      const urgent={...clone(records.alpha[0]),id:'30000000-0000-4000-8000-000000000099',review_business_id:B,
        title:'Need your access decision',business_slug:'beta',business_name:'Beacon Studio',attention:{notify:true,urgent:true,blocking:true,reason:'Work is blocked'}};
      urgent.affected_scope={kind:'business',business_id:B}; urgent.preview.affected_scope=urgent.affected_scope;
      records.beta=[urgent];
      attentionOverview={businesses:[{id:A,slug:'alpha',name:'Atlas Research',total:5,signoffs:2,tasks:3,urgent:0},
        {id:B,slug:'beta',name:'Beacon Studio',total:3,signoffs:1,tasks:2,urgent:1}],urgent:[urgent]};
      attentionDeliveries=[{id:'delivery-one',kind:'individual',created_at:'2026-09-09T12:00:00Z',items:[urgent]}];
      await fresh(); await page.evaluate(()=>sgAttentionRefresh());
      // Pending and urgent Signoffs never recreate the removed corner launcher.
      assert.equal(await page.locator('#thandle').count(),0);
      assert.equal(await page.locator('#taskspanel .ttab[data-v=signoffs] .sg-signoff-count').textContent(),'2');
      // The workspace-menu business badges no longer exist in this shell (neither at the base commit); the toast carries the cross-business route.
      await page.screenshot({path:path.join(OUT,'cross-business-toast.png')});
      await page.locator('.sg-toast-open').click();
      // Attention links never switch the owner's workspace: Signoffs open for the current project and the other project's row stays out.
      await page.waitForFunction(()=>tView==='signoffs' && tPanelOpen());
      assert.equal(await page.evaluate(()=>state.biz),'alpha');
      assert.equal(await page.locator(`.sg-row[data-id="${urgent.id}"]`).count(),0);
      mode.attentionError=true;
      await page.locator('[data-action="snooze"]').click();
      await page.locator('.sg-toast-error').filter({hasText:'Notification update failed.'}).waitFor();
      assert.equal(await page.locator('.sg-toast').count(),1);
      mode.attentionError=false;
      await page.locator('[data-action="snooze"]').click();
      await page.locator('.sg-toast').waitFor({state:'detached'});
      assert(attentionPosts[0].route.endsWith('/snooze'));
      assert.equal(await page.locator('#taskspanel .ttab[data-v=signoffs] .sg-signoff-count.urgent').count(),0, 'the current project has no urgent work of its own');
      attentionDeliveries=[{id:'summary-one',kind:'summary',created_at:'2026-09-09T12:00:00Z',items:[
        {...clone(records.alpha[0]),business_slug:'alpha',business_name:'Atlas Research'},{...urgent,attention:{notify:true,urgent:false,blocking:false}}]}];
      await page.evaluate(()=>sgAttentionRefresh());
      await page.locator('.sg-toast-business[data-slug="alpha"]').click();
      await page.waitForFunction(()=>state.biz==='alpha' && tView==='signoffs' && tPanelOpen());
      await page.locator('[data-action="dismiss"]').click();
      await page.locator('.sg-toast').waitFor({state:'detached'});
      assert(attentionPosts.at(-1).route.endsWith('/dismiss'));
    });
    assert.deepEqual(errors, []);
    console.log(`${tests} browser scenarios passed. Screenshots: ${OUT}`);
  } catch (error) {
    if (page) {
      console.error("Browser errors:", errors);
      console.error("Execution posts:", posts);
      console.error(await page.locator("#sg-execution").textContent().catch(() => "No execution view"));
      console.error(await page.locator(".sg").textContent().catch(() => "No signoff view"));
      await page.screenshot({ path: path.join(OUT, "failure.png"), animations: "disabled" }).catch(() => {});
    }
    throw error;
  } finally {
    if (context) await context.close();
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
