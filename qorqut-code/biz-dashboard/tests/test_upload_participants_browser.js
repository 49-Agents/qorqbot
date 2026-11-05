#!/usr/bin/env node
// The Uploads tab's call and meeting controls: the Context | Call | Meeting switch, the "who" typeahead with its
// chips, the "when" date and the payload the text route receives, then how a filed call or meeting reads in the
// context view. Real app HTML/CSS/JS, an isolated static server
// and synthetic API responses: no dashboard server.py, filer, production data or other write endpoints run.
// Requires Playwright (see README). Optional PLAYWRIGHT_CHROMIUM_EXECUTABLE selects an existing browser.
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), http = require("node:http");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const repo = path.join(__dirname, ".."), fixture = require("./fixtures/context-graph.json");
const MICHAEL = "11111111-1111-4111-8111-111111111111", MICHELLE = "22222222-2222-4222-8222-222222222222";
const people = [{ id: MICHAEL, kind: "context_person", metadata: { name: "Michael Linford", role: "Founder", org: "Acme", aka: ["Mike"] } },
  { id: MICHELLE, kind: "context_person", metadata: { name: "Michelle Ortiz" } }];
const GONE = "33333333-3333-4333-8333-333333333333";
// uploads the context view lists: a filed call, a pending meeting whose picked Person is no longer registered, an ordinary one
const filed = {
  "2026-09-17-abc123": { where: "_dumps", meta: { title: "Pilot call", origin: "call", occurred_on: "2026-09-17", status: "processed",
    participants: [{ person_id: MICHAEL, name: "Mike" }, { name: "Priya" }] } },
  "2026-09-18-def456": { where: "_inbox", meta: { title: "Weekly sync", origin: "meeting", status: "pending", participants: [{ person_id: GONE, name: "Sam" }] } },
  "2026-09-16-plain": { where: "_dumps", meta: { title: "Plain note", origin: "upload", status: "processed" } },
};
const listing = where => Object.entries(filed).filter(([, u]) => u.where === where).map(([id, u]) => ({ id, document_id: "doc-" + id,
  path: `context/${where}/${id}.md`, kind: where === "_dumps" ? "dump" : "inbox", failed: false, attempts: 0, map: false, ...u.meta }));
const contextPayload = () => ({ ...fixture, inbox: listing("_inbox"), dumps: listing("_dumps"),
  people: [...(fixture.people || []), { document_id: MICHAEL, slug: "michael-linford", name: "Michael Linford", kind: "person", path: "_people/michael-linford.md", mentions: [] }] });
const peopleRequests = [], uploads = [], rejected = [], writes = [], errors = [], held = {};
let failNext = false;   // the next upload is refused, as the server does for a Person that is gone
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://localhost");
  if (req.method === "POST") {
    let body = ""; req.on("data", chunk => body += chunk); req.on("end", () => {
      const args = body ? JSON.parse(body) : {}, json = (status, value, delay = 0) => setTimeout(() => res.writeHead(status, { "Content-Type": "application/json" }).end(JSON.stringify(value)), delay);
      if (url.pathname === "/api/biz/studio/operations/context_people_list") {
        peopleRequests.push(args);
        return json(200, { status: "ok", data: { items: /^mich/i.test(args.query) ? people : [], next_cursor: null } }, held[args.query] || 0);
      }
      // reads the panel and the result cards make over POST; anything else that is not the two routes below is a write
      const operation = url.pathname.startsWith("/api/biz/studio/operations/") ? url.pathname.split("/").pop() : "";
      if (operation === "context_reviews_list") return json(200, { status: "ok", data: { items: [], reviews: [], item_revision: 1, next_cursor: null } });
      if (url.pathname === "/api/biz/studio/context/upload") {
        if (failNext) { failNext = false; rejected.push(args); return json(422, { status: "error", error: { code: "not_found", message: "Participant is not a Person in this business" } }); }
        uploads.push(args);
        return json(202, { ok: true, status: "accepted", id: "2026-09-19-a1b2", document_id: "doc-" + uploads.length, path: "_inbox/2026-09-19-a1b2.md" });
      }
      writes.push(req.method + " " + req.url); res.writeHead(405).end();
    });
    return;
  }
  if (url.pathname.startsWith("/api/")) {
    let body = {};
    if (url.pathname === "/api/biz") body = ["studio", "another-studio"].map(slug => ({ slug, name: slug === "studio" ? "Example Studio" : "Another Studio", agents: [] }));
    else if (url.pathname.endsWith("/context")) body = contextPayload();
    else if (url.pathname.endsWith("/context/file")) {
      const id = /([^/]+)\.md$/.exec(url.searchParams.get("p") || "")?.[1], u = filed[id];
      body = { path: url.searchParams.get("p"), dir: "context", text: "Michael [10:00]: hello\n", document_id: "doc-" + id, revision: 2, content_hash: "h",
        material: { material_type: "mixed", origin: u?.meta.origin, tags: [] } };
    }
    else if (url.pathname.endsWith("/context/map")) {
      const id = url.searchParams.get("id");
      body = { id, document_id: "doc-" + id, map: null, mapping: false, error: "", trace: { id, document_id: "doc-" + id, where: filed[id]?.where, notes: [], topics: [], people: [], map: false } };
    }
    else if (url.pathname.endsWith("/context/person/michael-linford")) body = { name: "Michael Linford", slug: "michael-linford", body: "", notes_text: "" };
    else if (url.pathname.endsWith("/learnings")) body = { items: [] };
    res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(body)); return;
  }
  const file = path.resolve(repo, "." + (url.pathname === "/" ? "/index.html" : url.pathname));
  if (!file.startsWith(repo + path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
  const contentType = file.endsWith(".css") ? "text/css" : file.endsWith(".js") ? "text/javascript" : "text/html";
  res.writeHead(200, { "Content-Type": contentType }).end(fs.readFileSync(file));
});
// test_stories_browser.js pins these: none of them may exist in the upload form, whatever mode it is in
const PINNED = '#uptasks,#uptopic,#uptext[placeholder],#upnotes[placeholder]';
const menuRows = page => page.locator(".menu .mi .lb").allTextContents();
const chipNames = page => page.locator("#upwhochips .upchip").evaluateAll(chips => chips.map(chip => chip.firstElementChild?.classList.contains("upperson") ? "@" + chip.textContent.replace("×", "") : chip.textContent.replace("×", "")));

(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route("**/*", route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await context.addInitScript(() => { localStorage.setItem("tab", "context"); localStorage.setItem("ctxpage:studio", ""); });
    const page = await context.newPage(); page.on("pageerror", e => errors.push(e.message));
    await page.goto(origin + "/#studio");
    await page.waitForFunction(() => typeof state !== "undefined" && state.biz === "studio");
    await page.evaluate(() => { openTasks(true); tSetView("uploads"); });
    await page.locator("#uptext").waitFor();

    // Context is the default and adds nothing to the form.
    assert.deepEqual(await page.locator("#upkind [role=radio]").allTextContents(), ["Context", "Call", "Meeting"]);
    assert.equal(await page.locator('#upkind [aria-checked="true"]').textContent(), "Context");
    assert.equal(await page.locator("#upwho,#upwhen,#upwhochips").count(), 0);
    assert.equal(await page.getByRole("button", { name: "Upload", exact: true }).count(), 1);
    assert.equal(await page.locator(PINNED).count(), 0);
    console.log("ok Context is the default kind and shows no call fields");

    // Call reveals who and when, waits for a participant, and never sends before one is chosen.
    await page.locator('#upkind [data-origin="call"]').click();
    assert.equal(await page.locator('#upkind [aria-checked="true"]').textContent(), "Call");
    assert.equal((await page.locator("label[for=upwho]").textContent()).trim(), "Who was the meeting with");
    assert.equal((await page.locator("label[for=upwhen]").innerText()).replace(/\s+/g, " ").trim().toLowerCase(), "when optional");
    assert.equal(await page.locator("#upwhen").getAttribute("type"), "date");
    assert.equal(await page.locator("#upwho,#upwhen").evaluateAll(fields => fields.filter(field => field.hasAttribute("placeholder")).length), 0);
    assert.equal(await page.locator("#uptasks,#uptopic").count(), 0);
    assert.equal(await page.locator("#upwhochips").isVisible(), false, "no chips, no chip row");
    assert.equal(await page.locator("#upattach").isDisabled(), true, "calls are pasted as text");
    const transcript = "Michael [10:00]: Can we pilot in October?\nPriya [10:01]: Yes, the budget is approved.";
    await page.locator("#uptext").fill(transcript);
    assert.equal(await page.locator(PINNED).count(), 0);
    assert.equal(await page.locator("#upsend").isDisabled(), true);
    assert.equal(await page.locator("#upsendlbl").textContent(), "Add who the call was with");
    await page.locator("#uptext").press("Control+Enter");
    await page.waitForTimeout(150);
    assert.equal(uploads.length, 0, "Ctrl+Enter cannot send a call without participants");
    console.log("ok Call fields, no placeholders, send waits for a participant");

    // Typing finds Persons (role and org shown), lists Add "<typed>" last, and the pick becomes a chip with a person icon.
    await page.locator("#upwho").fill("Mich");
    await page.locator(".menu .mi").first().waitFor();
    assert.deepEqual(await menuRows(page), ["Michael Linford · Founder · Acme", "Michelle Ortiz", 'Add "Mich"']);
    assert.deepEqual(peopleRequests.map(({ query, limit }) => ({ query, limit })), [{ query: "Mich", limit: 8 }]);
    await page.screenshot({ path: "/tmp/upload-participants-menu.png" });
    await page.locator(".menu .mi").first().click();
    assert.deepEqual(await chipNames(page), ["@Michael Linford"]);
    assert.equal(await page.locator("#upwho").inputValue(), "");
    assert.equal(await page.evaluate(() => document.activeElement?.id), "upwho", "a mouse pick leaves the field ready for the next name");
    assert.equal(await page.locator(".menu").count(), 0);
    assert.equal(await page.locator("#upsend").isDisabled(), false);
    assert.equal(await page.locator("#upsendlbl").textContent(), "Upload");
    // The picked Person is not offered again, while another match still is.
    await page.locator("#upwho").fill("Mich");
    await page.locator(".menu .mi").first().waitFor();
    assert.deepEqual(await menuRows(page), ["Michelle Ortiz", 'Add "Mich"']);
    await page.locator("#upwho").fill("");
    assert.equal(await page.locator(".menu").count(), 0, "under two characters no menu");
    console.log("ok typeahead rows, Add row, chip with person icon, no repeat offers");

    // Enter adds the typed name and never submits, whether or not the menu is open.
    await page.locator("#upwho").fill("Priya");
    await page.locator("#upwho").press("Enter");
    assert.deepEqual(await chipNames(page), ["@Michael Linford", "Priya"]);
    await page.waitForTimeout(400);
    assert.equal(await page.locator(".menu").count(), 0, "the pending lookup was cancelled");
    assert.equal(uploads.length, 0, "Enter inside the field must not submit");
    await page.locator("#upwho").fill("Zed");
    await page.locator(".menu .mi").first().waitFor();
    assert.deepEqual(await menuRows(page), ['Add "Zed"']);
    await page.locator("#upwho").press("Enter");
    assert.deepEqual(await chipNames(page), ["@Michael Linford", "Priya", "Zed"]);
    await page.locator("#upwho").fill("priya");
    await page.locator("#upwho").press("Enter");
    assert.deepEqual(await chipNames(page), ["@Michael Linford", "Priya", "Zed"], "the same name twice adds nothing");
    await page.locator("#toasts").getByText("Already on the list").waitFor();
    await page.locator('#upwhochips .upchip:has-text("Zed") .x').click();
    assert.deepEqual(await chipNames(page), ["@Michael Linford", "Priya"]);
    assert.equal(uploads.length, 0);
    console.log("ok Enter adds names without submitting; chips can be removed");

    // With real matches showing, Enter picks the highlighted one (the first row) rather than the typed text.
    await page.locator("#upwho").fill("Mich");
    await page.locator(".menu .mi").first().waitFor();
    assert.deepEqual(await menuRows(page), ["Michelle Ortiz", 'Add "Mich"']);
    await page.locator("#upwho").press("Enter");
    assert.deepEqual(await chipNames(page), ["@Michael Linford", "Priya", "@Michelle Ortiz"]);
    assert.equal(uploads.length, 0);
    await page.locator('#upwhochips .upchip:has-text("Michelle") .x').click();
    assert.deepEqual(await chipNames(page), ["@Michael Linford", "Priya"]);
    // Rows built for older text do not stay open: Enter right after the next keystroke adds what was typed, not the old first row.
    await page.locator("#upwho").fill("Mich");
    await page.locator(".menu .mi").first().waitFor();
    await page.locator("#upwho").fill("Michx");
    assert.equal(await page.locator(".menu").count(), 0, "the menu built for Mich closed with the next keystroke");
    await page.locator("#upwho").press("Enter");
    assert.deepEqual(await chipNames(page), ["@Michael Linford", "Priya", "Michx"]);
    await page.locator('#upwhochips .upchip:has-text("Michx") .x').click();
    assert.deepEqual(await chipNames(page), ["@Michael Linford", "Priya"]);
    console.log("ok Enter picks the highlighted match; an outdated menu closes at the next keystroke");

    // The chips are drawn from the state whenever the form is rebuilt, and their × must keep working then: after switching kind ...
    await page.locator('#upkind [data-origin="meeting"]').click();
    await page.locator('#upkind [data-origin="call"]').click();
    await page.locator('#upwhochips .upchip:has-text("Priya") .x').click();
    assert.deepEqual(await chipNames(page), ["@Michael Linford"]);
    assert.equal(await page.evaluate(() => document.activeElement?.id), "upwho", "removing a chip leaves the field ready");
    await page.locator("#upwho").fill("Priya");
    await page.locator("#upwho").press("Enter");
    assert.deepEqual(await chipNames(page), ["@Michael Linford", "Priya"]);
    // ... and after a send the server refused, which rebuilds the form as well and keeps who was chosen.
    failNext = true;
    await page.locator("#upsend").click();
    await page.locator("#uperr:not([hidden])").waitFor();
    assert.equal(rejected.length, 1);
    assert.equal(rejected[0].origin, "call");
    assert.equal(uploads.length, 0);
    assert.equal((await page.locator("#uperr").textContent()).trim(), "Couldn't save. Try again");
    assert.deepEqual(await chipNames(page), ["@Michael Linford", "Priya"]);
    await page.locator('#upwhochips .upchip:has-text("Priya") .x').click();
    assert.deepEqual(await chipNames(page), ["@Michael Linford"]);
    await page.locator("#upwho").fill("Priya");
    await page.locator("#upwho").press("Enter");
    assert.deepEqual(await chipNames(page), ["@Michael Linford", "Priya"]);
    console.log("ok chips can still be removed after the form is rebuilt and after a refused send");

    // A lookup answered after the text changed is ignored.
    held.Mich = 500;
    const before = peopleRequests.length;
    await page.locator("#upwho").fill("Mich");
    while (peopleRequests.length === before) await page.waitForTimeout(25);   // the Mich lookup is in flight, held by the server
    await page.locator("#upwho").fill("Xavier");
    await page.waitForTimeout(1000);
    assert.deepEqual(await menuRows(page), ['Add "Xavier"'], "the late Mich answer did not replace the menu");
    await page.locator("#upwho").fill("");
    // ... and so is one that arrives after focus has left the field.
    const asked = peopleRequests.length;
    await page.locator("#upwho").fill("Mich");
    while (peopleRequests.length === asked) await page.waitForTimeout(25);
    await page.locator("#upnotes").focus();
    await page.waitForTimeout(900);
    assert.equal(await page.locator(".menu").count(), 0, "no menu under a field the user has left");
    delete held.Mich;
    await page.locator("#upwho").fill("");
    console.log("ok stale typeahead answers are ignored");

    // The date and the send: the payload carries origin, participants as chosen and typed, and the date.
    await page.locator("#upwhen").fill("2026-09-17");
    await page.locator("#upnotes").fill("Pilot call");
    await page.locator("#upsend").click();
    await page.waitForFunction(() => document.querySelector(".updrow:not(.bad) .updk")?.textContent === "call");   // the refused send above is a .bad row
    assert.equal(uploads.length, 1);
    const { idempotency_key, ...sent } = uploads[0];
    assert.ok(idempotency_key);
    assert.deepEqual(sent, { text: transcript, notes: "Pilot call", topic: "", task_ids: [], origin: "call",
      participants: [{ person_id: MICHAEL, name: "Michael Linford" }, { name: "Priya" }], occurred_on: "2026-09-17" });
    assert.match(await page.locator(".updrow .updl").first().innerText(), /Pilot call\s+with Michael Linford, Priya/);
    assert.equal(await page.locator('#upkind [aria-checked="true"]').textContent(), "Context", "the form goes back to Context");
    assert.equal(await page.locator("#uptext").inputValue(), "");
    assert.equal(await page.locator("#upwho").count(), 0);
    await page.screenshot({ path: "/tmp/upload-participants-done.png" });
    console.log("ok call payload and result card");

    // A meeting keeps who and when when switching between the two, and needs no date.
    await page.locator('#upkind [data-origin="meeting"]').click();
    await page.locator("#uptext").fill("Notes from the weekly sync.");
    await page.locator("#upwho").fill("Sam");
    await page.locator("#upwho").press("Enter");
    await page.locator('#upkind [data-origin="call"]').click();
    assert.deepEqual(await chipNames(page), ["Sam"]);
    await page.locator('#upkind [data-origin="meeting"]').click();
    await page.screenshot({ path: "/tmp/upload-participants-meeting.png" });
    await page.locator("#upsend").click();
    await page.waitForFunction(() => document.querySelector(".updrow .updk")?.textContent === "meeting");
    assert.deepEqual(uploads[1], { ...uploads[1], text: "Notes from the weekly sync.", origin: "meeting", participants: [{ name: "Sam" }] });
    assert.equal("occurred_on" in uploads[1], false);
    console.log("ok meeting without a date");

    // Attaching files is refused while a call or meeting is chosen; an ordinary upload sends nothing new.
    await page.locator('#upkind [data-origin="call"]').click();
    await page.locator("#uppick").setInputFiles({ name: "image.png", mimeType: "image/png", buffer: Buffer.from("synthetic-image") });
    assert.equal(await page.locator(".upth").count(), 0);
    await page.locator('#upkind [data-origin=""]').click();
    await page.locator("#uppick").setInputFiles({ name: "image.png", mimeType: "image/png", buffer: Buffer.from("synthetic-image") });
    assert.equal(await page.locator(".upth").count(), 1, "files attach again in Context");
    assert.equal(await page.locator("#upkind").count(), 0, "the kind switch belongs to text, not media");
    assert.equal(await page.locator(PINNED).count(), 0);
    await page.locator("#upclear").click();
    await page.locator("#uptext").fill("An ordinary note.");
    assert.equal(await page.locator(PINNED).count(), 0);
    assert.equal(await page.locator("#upsend").isDisabled(), false);
    await page.locator("#upsend").click();
    await page.waitForFunction(() => document.querySelectorAll(".updrow").length === 4);   // three sends and the refused one
    assert.deepEqual(Object.keys(uploads[2]).sort(), ["idempotency_key", "notes", "task_ids", "text", "topic"]);
    assert.equal(await page.locator(".updrow .updk").first().textContent(), "context");
    console.log("ok files refused for calls; ordinary upload payload unchanged");

    // Leaving Call for Context keeps the text but sends no call fields.
    await page.locator('#upkind [data-origin="call"]').click();
    await page.locator("#upwho").fill("Dana");
    await page.locator("#upwho").press("Enter");
    await page.locator("#uptext").fill("Switching my mind.");
    await page.locator('#upkind [data-origin=""]').click();
    await page.locator("#upsend").click();
    await page.waitForFunction(() => document.querySelectorAll(".updrow").length === 5);
    assert.deepEqual(Object.keys(uploads[3]).sort(), ["idempotency_key", "notes", "task_ids", "text", "topic"]);
    console.log("ok Context after Call sends no call fields");

    // The filed view: "call with A, B on date" for a call or meeting; a picked Person links to their page, a typed name does not.
    await page.evaluate(() => { openTasks(false); return ctxGo("_dumps/2026-09-17-abc123.md"); });
    await page.locator("#taskspanel").waitFor({ state: "hidden" });
    await page.locator(".trace .row .lbl").first().waitFor();
    const sentence = async () => (await page.locator(".jcol .trace .row").first().innerText()).replace(/\s+/g, " ").trim();
    assert.equal(await sentence(), "CALL with Mike Priya on 2026-09-17");
    assert.deepEqual(await page.locator(".jcol .trace .row .chip").evaluateAll(chips => chips.map(chip => [chip.textContent, chip.classList.contains("go"), chip.dataset.p || ""])),
      [["Mike", true, "_people/michael-linford"], ["Priya", false, ""]]);
    await page.screenshot({ path: "/tmp/upload-participants-filed.png" });
    await page.locator(".jcol .trace .row .chip.go").click();
    await page.waitForFunction(() => state.ctx === "_people/michael-linford");
    await page.getByRole("heading", { name: "Michael Linford", exact: true }).waitFor();
    await page.evaluate(() => ctxGo("_inbox/2026-09-18-def456.md"));
    await page.locator(".jcol .trace .row .lbl").first().waitFor();
    assert.equal(await sentence(), "MEETING with Sam");
    assert.equal(await page.locator(".jcol .trace .row .chip.go").count(), 0, "a Person that is no longer registered is not linked");
    await page.evaluate(() => ctxGo("_dumps/2026-09-16-plain.md"));
    await page.locator(".jcol .sec .sh").first().waitFor();
    assert.equal(await page.locator(".jcol .trace .row").count(), 0, "an ordinary upload shows no call or meeting line");
    console.log("ok filed view: call with names, links only for registered Persons");

    // A narrow pane: the switch, the chips and the fields stay inside the drawer.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => { openTasks(true); tSetView("uploads"); });
    await page.locator('#upkind [data-origin="call"]').click();
    for (const name of ["Michael Linford, head of partnerships at a rather long company name", "Priya", "Sam"]) {
      await page.locator("#upwho").fill(name);
      await page.locator("#upwho").press("Enter");
    }
    assert.equal(await page.locator("#upwhochips .upchip").count(), 3);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "no horizontal overflow");
    assert.ok(await page.locator("#upwhochips").evaluate(row => row.scrollWidth <= row.clientWidth + 1), "chips wrap inside the drawer");
    assert.ok(await page.locator("#upkind").evaluate(kind => kind.getBoundingClientRect().right <= innerWidth), "the switch fits");
    await page.screenshot({ path: "/tmp/upload-participants-mobile.png" });
    console.log("ok narrow pane");

    assert.deepEqual(writes, []); assert.deepEqual(errors, []);
    console.log("PASS upload participants browser checks; no unexpected writes or uncaught browser errors");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
