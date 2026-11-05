#!/usr/bin/env node
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const ok = (data) => ({ status: "ok", data });
const recipients = [
  {
    agent_id: "founder-id",
    name: "Founder",
    role_id: "founder",
    business_id: null,
    group: "Global",
  },
  {
    agent_id: "learning-id",
    name: "Learning Applier",
    role_id: "learning-applier",
    business_id: null,
    group: "Global",
  },
  {
    agent_id: "ord-id",
    name: "Ordubashy",
    role_id: "ordubashy",
    business_id: null,
    group: "Global",
  },
  {
    agent_id: "marketing-id",
    name: "Marketing",
    role_id: "global-worker",
    business_id: "widget-id",
    business_slug: "widget",
    group: "Businesses",
  },
  {
    agent_id: "agent-id",
    name: "Tamga",
    role_id: "business-agent",
    business_id: "widget-id",
    business_slug: "widget",
    group: "Businesses",
  },
  {
    agent_id: "other-agent",
    name: "Bilge",
    role_id: "business-agent",
    business_id: "other-id",
    business_slug: "other",
    group: "Businesses",
  },
  {
    agent_id: "dev-id",
    name: "Dev",
    role_id: "dev",
    business_id: "widget-id",
    business_slug: "widget",
    group: "Businesses",
  },
];

(async () => {
  const server = spawn(
    "python3",
    ["-u", "-m", "http.server", "0", "--bind", "127.0.0.1"],
    { cwd: root, stdio: ["ignore", "pipe", "pipe"] },
  );
  let port;
  const read = (data) => {
    const match = String(data).match(/port (\d+)/);
    if (match) port = Number(match[1]);
  };
  server.stdout.on("data", read);
  server.stderr.on("data", read);
  while (!port) await new Promise((resolve) => setTimeout(resolve, 20));
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : {}),
  });
  const page = await browser.newPage({
    viewport: { width: 1280, height: 900 },
  });
  page.setDefaultTimeout(5000);
  const posts = [];
  let pauseFails = true;
  const schedules = [
    {
      id: "scheduled-1",
      revision: 7,
      name: "Every morning",
      message: "Review your queue",
      delivery: "inbox",
      status: "active",
      next_due_at: "2026-09-15T09:00:00Z",
      max_occurrences: null,
      recipients_json: JSON.stringify([
        { agent_id: "marketing-id" },
        { agent_id: "agent-id" },
      ]),
      timing_json: JSON.stringify({
        kind: "interval",
        every_seconds: 7200,
        at: "2026-09-15T09:00:00Z",
      }),
    },
  ];
  await page.route("**/api/**", async (route) => {
    const request = route.request(),
      pathname = new URL(request.url()).pathname,
      payload = JSON.parse(request.postData() || "{}");
    let response = [];
    if (pathname === "/api/biz")
      response = [
        { slug: "widget", name: "Widget", agents: ["Marketing", "Tamga"] },
      ];
    else if (pathname === "/api/session")
      response = { principal_id: "owner", is_owner: true };
    else if (pathname === "/api/root/agents") response = ok({ agents: [] });
    else if (pathname === "/api/me/attention") response = { total: 0 };
    else if (pathname === "/api/me/attention/deliveries") response = [];
    else if (pathname === "/api/biz/widget/agents")
      response = [
        { agent_id: "marketing-id", name: "Marketing", role_id: "global-worker" },
        { agent_id: "agent-id", name: "Tamga", role_id: "business-agent" },
        { agent_id: "dev-id", name: "Dev", role_id: "dev" },
      ];
    else if (pathname.endsWith("/terminals")) response = [];
    else if (pathname.endsWith("/broadcast_recipients"))
      response = ok({
        items: recipients.filter(
          (row) => payload.scope === "global" || row.business_slug === "widget",
        ),
      });
    else if (pathname.endsWith("/broadcasts_list"))
      response = ok({ items: schedules });
    else if (pathname.endsWith("/broadcasts_get"))
      response = ok({
        broadcast: schedules[0],
        deliveries: [
          { recipient_id: "marketing-id", state: "queued" },
          {
            recipient_id: "agent-id",
            state: "denied",
            detail: "Agent is unavailable",
          },
        ],
      });
    else if (
      /broadcasts_(create|update|pause|resume|cancel|send_now)$/.test(pathname)
    ) {
      posts.push({ pathname, payload });
      response =
        pathname.endsWith("/broadcasts_pause") && pauseFails
          ? ((pauseFails = false),
            { status: "error", error: { message: "Revision changed" } })
          : ok({ broadcast: { id: "new-broadcast" } });
    } else response = ok({});
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(response),
    });
  });
  try {
    await page.goto(`http://127.0.0.1:${port}/#/global/agents`);
    await page.locator(".global-agents").waitFor();
    assert.equal(await page.locator("[data-broadcast-open]").count(), 0, "Broadcast is absent from agent navigation");
    await page.evaluate(() => modal({
      title: "Broadcast", width: 820, body: '<div class="broadcast-modal"></div>',
      onOpen: el => renderBroadcast(el.querySelector(".broadcast-modal"), {scope: "global"}),
    }));
    await page.getByText("Founder", { exact: true }).waitFor();
    if (process.env.BROADCAST_SCREENSHOT)
      await page.screenshot({
        path: process.env.BROADCAST_SCREENSHOT,
        fullPage: true,
        animations: "disabled",
      });
    await page.setViewportSize({ width: 600, height: 900 });
    assert(
      await page
        .locator("#broadcast-body")
        .evaluate((element) => element.scrollWidth <= element.clientWidth + 1),
      "Broadcast controls fit a narrow viewport",
    );
    if (process.env.BROADCAST_NARROW_SCREENSHOT)
      await page.screenshot({
        path: process.env.BROADCAST_NARROW_SCREENSHOT,
        animations: "disabled",
      });
    await page.setViewportSize({ width: 1280, height: 900 });
    assert.equal(
      await page.locator("[data-recipient]:checked").count(),
      0,
      "Global agents start off",
    );
    assert.equal(
      await page.locator(".broadcast-business-tile.selected").count(),
      2,
      "Business groups start selected",
    );
    assert.equal(await page.locator('.broadcast-business-tile').first().evaluate(el => getComputedStyle(el).justifyContent), 'flex-start');
    await page.getByRole("button", { name: "Terminal input", exact: true }).click();
    await page.locator("#broadcast-message").fill("Run checklist");
    await page.locator('[data-recipient="founder-id"]').check();
    await page
      .locator("#broadcast-form")
      .evaluate((form) => form.requestSubmit());
    await page.waitForTimeout(40);
    const create = posts.at(-1).payload;
    assert.equal(create.send_now, true);
    assert.equal(create.definition.delivery, "input");
    assert.deepEqual(
      create.definition.recipients.sort(),
      ["founder-id", "agent-id", "other-agent", "marketing-id", "dev-id"].sort(),
    );
    assert.equal(
      Object.hasOwn(create.definition, "max_occurrences"),
      false,
      "empty maximum is omitted",
    );
    await page.getByRole("tab", { name: "Scheduled", exact: true }).click();
    await page.getByRole("button", { name: "Edit", exact: true }).click();
    await page.locator("#broadcast-every").waitFor();
    assert.equal(await page.locator("#broadcast-every").inputValue(), "2");
    assert.equal(await page.locator("#broadcast-unit").inputValue(), "hours");
    await page.locator("#broadcast-message").fill("Updated queue");
    await page
      .locator("#broadcast-form")
      .evaluate((form) => form.requestSubmit());
    await page.waitForTimeout(40);
    const update = posts.at(-1).payload;
    assert.equal(posts.at(-1).pathname.endsWith("broadcasts_update"), true);
    assert.equal(update.expected_revision, 7);
    assert.equal(update.definition.timing.every, 2);
    assert.equal(update.definition.timing.unit, "hours");
    await page.getByRole("tab", { name: "Scheduled", exact: true }).click();
    await page.getByText("Delivery outcomes", { exact: true }).click();
    await page.getByRole("dialog").getByText("Marketing", { exact: true }).waitFor();
    assert.match(
      await page.locator(".broadcast-results").innerText(),
      /Agent is unavailable/,
    );
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    await page.getByText("Revision changed", { exact: true }).waitFor();
    assert.equal(
      await page
        .getByRole("button", { name: "Pause", exact: true })
        .isEnabled(),
      true,
      "failed management action remains retryable",
    );
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    await page.waitForTimeout(40);
    assert.equal(posts.at(-1).payload.expected_revision, 7);
    await page.getByRole("tab", { name: "Compose", exact: true }).click();
    await page.locator("#broadcast-message").fill("Once payload");
    await page.getByLabel("Once", { exact: true }).check();
    assert.equal(await page.locator("#broadcast-max").count(), 0);
    await page.locator("#broadcast-at").fill("2030-01-02T09:30");
    await page
      .locator("#broadcast-form")
      .evaluate((form) => form.requestSubmit());
    await page.waitForTimeout(40);
    assert.deepEqual(posts.at(-1).payload.definition.timing.kind, "once");
    assert.equal(posts.at(-1).payload.send_now, undefined);
    await page.locator("#broadcast-message").fill("Cron payload");
    await page.getByLabel("Cron", { exact: true }).check();
    await page.locator("#broadcast-cron").fill("0 9 * * 1-5");
    await page.locator("#broadcast-timezone").fill("Asia/Almaty");
    await page
      .locator("#broadcast-form")
      .evaluate((form) => form.requestSubmit());
    await page.waitForTimeout(40);
    assert.deepEqual(posts.at(-1).payload.definition.timing, {
      kind: "cron",
      schedule: "0 9 * * 1-5",
      timezone: "Asia/Almaty",
    });
    await page.locator("#broadcast-message").fill("Minutes payload");
    await page.getByLabel("Repeat", { exact: true }).check();
    await page.locator("#broadcast-every").fill("15");
    await page.locator("#broadcast-unit").selectOption("minutes");
    await page.locator("#broadcast-max").fill("3");
    await page
      .locator("#broadcast-form")
      .evaluate((form) => form.requestSubmit());
    await page.waitForTimeout(40);
    assert.equal(posts.at(-1).payload.definition.timing.every, 15);
    assert.equal(posts.at(-1).payload.definition.timing.unit, "minutes");
    assert.equal(posts.at(-1).payload.definition.max_occurrences, 3);
    await page.locator("#broadcast-message").fill("Draft survives selection");
    await page.locator(".broadcast-business-tile").first().click();
    assert.equal(
      await page.locator("#broadcast-message").inputValue(),
      "Draft survives selection",
    );
    await page.getByRole("button", { name: "Clear all", exact: true }).click();
    assert.equal(
      await page.locator("#broadcast-message").inputValue(),
      "Draft survives selection",
    );
    assert.equal(await page.locator("#broadcast-filter").count(), 0, "no resident-role filter remains");
    await page.getByRole("button", { name: "Select all", exact: true }).click();
    await page
      .locator("#broadcast-form")
      .evaluate((form) => form.requestSubmit());
    await page.waitForTimeout(40);
    const allCreate = posts.at(-1).payload.definition.recipients;
    assert(
      ["marketing-id", "agent-id", "dev-id"].every((id) => allCreate.includes(id)),
      "Select all sends to every selected recipient",
    );
    await page.getByRole("button",{name:"Close",exact:true}).click();
    await page.evaluate(() => selectBiz("widget"));
    await page.locator("[data-business-task-page]").waitFor();
    assert.equal(await page.locator("[data-broadcast-open]").count(), 0, "Business navigation also omits Broadcast");
    await page.evaluate(() => modal({
      title: "Broadcast", width: 820, body: '<div class="broadcast-modal"></div>',
      onOpen: el => renderBroadcast(el.querySelector(".broadcast-modal"), {scope: "business", biz: "widget"}),
    }));
    await page.getByRole("dialog").getByText("Marketing", { exact: true }).waitFor();
    assert.equal(
      await page.locator("[data-recipient]:checked").count(),
      3,
      "Business agents start selected",
    );
    await page.screenshot({path:'/tmp/broadcast-polished.png',animations:'disabled'});
    await page.setViewportSize({width:430,height:850});
    assert(await page.locator('.modal').evaluate(el=>el.scrollWidth<=el.clientWidth));
    await page.screenshot({path:'/tmp/broadcast-polished-mobile.png',animations:'disabled'});
    await page.getByRole('button',{name:'Close',exact:true}).click();
    await page.evaluate(()=>goTab('progress'));
    assert.equal(await page.locator('[data-tab="milestones"],[data-tab="datascience"]').count(),0);
    assert.equal(await page.locator('[data-progress-view]').count(),0,'Progress has no Milestones or Research navigation');
    await page.locator('.business-progress-page').waitFor();
    assert(await page.locator('.business-progress-page').evaluate(el=>el.clientWidth>250));
    await page.screenshot({path:'/tmp/progress-nav-mobile.png',animations:'disabled'});
    console.log("Broadcast modal controls and the single Progress view passed");
  } finally {
    await browser.close();
    server.kill("SIGTERM");
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
