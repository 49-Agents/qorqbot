#!/usr/bin/env node
// Real service fixture: creates and edits a future Global broadcast; never contacts live services.
// Component coverage: renderBroadcast is mounted directly in a modal; this does not prove Broadcast is reachable from navigation (none is exposed).
const assert = require("node:assert/strict");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const core =
  process.env.BAZARLYK_CORE_ROOT || path.resolve(__dirname, "../../biz-core");
const dashboard = path.resolve(__dirname, "..");

(async () => {
  const child = spawn(
    process.env.TEST_PYTHON || path.join(core, ".venv/bin/python"),
    ["tests/global_agents_browser_fixture.py", "--dashboard-root", dashboard],
    {
      cwd: core,
      env: { ...process.env, PYTHONPATH: core },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let stderr = "",
    browser,
    page;
  child.stderr.on("data", (data) => {
    stderr += data;
  });
  try {
    const details = await new Promise((resolve, reject) => {
      let out = "";
      const timer = setTimeout(
        () => reject(Error("Fixture timed out: " + stderr)),
        180000,
      );
      child.stdout.on("data", (data) => {
        out += data;
        const line = out.split("\n").find((x) => x.startsWith('{"url":'));
        if (line) {
          clearTimeout(timer);
          resolve(JSON.parse(line));
        }
      });
      child.once("error", (error) => {
        clearTimeout(timer);
        reject(error);
      });
      child.once("exit", (code) => {
        clearTimeout(timer);
        reject(Error(`Fixture exited ${code}: ${stderr}`));
      });
    });
    browser = await chromium.launch({
      headless: true,
      args: ["--no-sandbox"],
      ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
        ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
        : {}),
    });
    page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    page.setDefaultTimeout(30000);
    await page.route("**/*", (route) =>
      route.request().url().startsWith(details.origin)
        ? route.continue()
        : route.abort(),
    );
    await page.goto(details.origin + "/#/global/agents");
    await page.locator(".global-agents").waitFor();
    assert.equal(await page.locator("[data-broadcast-open]").count(), 0, "Broadcast is absent from agent navigation");
    await page.evaluate(() => modal({
      title: "Broadcast", width: 820, body: '<div class="broadcast-modal"></div>',
      onOpen: el => renderBroadcast(el.querySelector(".broadcast-modal"), {scope: "global"}),
    }));
    await page
      .locator(".broadcast-recipient")
      .filter({ hasText: "Learning Applier" })
      .first()
      .locator("input")
      .check();
    await page.locator("#broadcast-message").fill("Fixture future reminder");
    await page.getByLabel("Once", { exact: true }).check();
    await page.locator("#broadcast-at").fill("2030-01-02T09:30");
    await page.getByRole("button", { name: "Schedule", exact: true }).click();
    await page.getByText("Broadcast scheduled", { exact: true }).waitFor();
    await page.getByRole("tab", { name: "Scheduled", exact: true }).click();
    await page.getByText("Fixture future reminder", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Edit", exact: true }).click();
    await page
      .locator("#broadcast-message")
      .fill("Fixture future reminder updated");
    await page
      .getByRole("button", { name: "Save changes", exact: true })
      .click();
    await page.getByText("Broadcast updated", { exact: true }).waitFor();
    await page.getByRole("tab", { name: "Scheduled", exact: true }).click();
    await page
      .getByText("Fixture future reminder updated", { exact: true })
      .waitFor();
    assert.equal(await page.locator(".broadcast-schedule").count(), 1);
    console.log("Broadcast real-service browser controls passed");
  } catch (error) {
    if (page) {
      console.error(await page.locator("body").innerText());
      await page.screenshot({ path: "/tmp/broadcast-real-failure.png" });
    }
    throw error;
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null)
      await new Promise((resolve) => {
        const timer = setTimeout(() => child.kill("SIGTERM"), 10000);
        child.once("exit", () => {
          clearTimeout(timer);
          resolve();
        });
        child.kill("SIGINT");
      });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
