#!/usr/bin/env node
// Bounded archive loading for js/learning.js: one inventory page at a time, byte-first reassembly of an
// oversized review page, then independent evidence/history cursor pages merged without losing or duplicating
// an inventory ID, a (run, session) source pair or a history event ID. The service is a recording double:
// the core stream owns the real operation contract, and this test states the contract it consumes.
//   node tests/test_learning_history.js
const fs = require("fs"), path = require("path"), vm = require("vm");
const HERE = path.join(__dirname, "..");
let fails = 0, n = 0;
const ok = (cond, msg) => { n++; if (!cond) { fails++; console.log(`FAIL ${msg}`); } else console.log(`ok   ${msg}`); };
const eq = (a, b, msg) => ok(JSON.stringify(a) === JSON.stringify(b), `${msg} (${JSON.stringify(a)} == ${JSON.stringify(b)})`);

// ------------------------------------------------------------- DOM stub (the page keeps its host element)
const mkEl = (tag = "div") => ({
  tagName: tag.toUpperCase(), _q: {}, children: [], style: { setProperty() {} }, dataset: {}, classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  hidden: false, textContent: "", innerHTML: "", disabled: false, value: "",
  querySelector(s) { return this._q[s] || (this._q[s] = mkEl()); }, querySelectorAll() { return []; }, appendChild(c) { this.children.push(c); return c; }, remove() {},
  addEventListener() {}, removeEventListener() {}, setAttribute() {}, getAttribute() { return null; }, focus() {}, closest() { return null; },
  getBoundingClientRect() { return { left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 }; },
});
const byId = {}, hosts = {}, store = [], notes = [];
const g = globalThis;
g.window = g;
g.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
g.location = { search: "", hash: "" };
g.document = {
  body: mkEl("body"), activeElement: null,
  querySelector: s => s === ".lnhist" ? (hosts.hist = hosts.hist || mkEl())
    : s.startsWith("#") ? (byId[s.slice(1)] = byId[s.slice(1)] || mkEl()) : null,
  querySelectorAll: () => [], createElement: t => mkEl(t), addEventListener() {}, removeEventListener() {},
};
g.addEventListener = () => {};
g.requestAnimationFrame = f => f();
g.setInterval = () => 0;
g.innerWidth = 1200; g.innerHeight = 800;
for (const f of ["core", "san-juan-time", "side", "context", "learning"]) vm.runInThisContext(fs.readFileSync(path.join(HERE, "js", f + ".js"), "utf8"), { filename: f + ".js" });
g.notify = t => notes.push(t);
g.toast = t => notes.push({ text: t });
state.biz = "project-a"; state.scope = "business"; state.ctx = "_learning";

// ------------------------------------------------------------- recording service double
let requests = [], respond = async () => ({});
g.fetch = async url => {
  const parsed = new URL(url, "http://fixture");
  requests.push(parsed.pathname + parsed.search);
  const body = await respond(parsed);
  return { ok: !(body && body.error), status: body && body.error ? 403 : 200, headers: { get: () => null }, json: async () => body };
};
const originalFetch = g.fetch;
const reset = () => { requests = []; state.scope = "business"; state.biz = "project-a"; _ln = lnFresh(); _ln.biz = "project-a"; };
const lastRequest = () => requests[requests.length - 1];

(async () => {
// ------------------------------------------------------------- page merging primitives
eq(lnSourceKey({ run: "r1", session: "s1" }), "r1\u0000s1", "a source identity is the run/session pair");
eq(lnMergeSources([{ run: "r1", session: "s1" }], [{ run: "r1", session: "s1" }]).length, 1, "an identical pair is deduplicated");
const twoRuns = lnMergeSources([{ run: "r1", session: "s1" }], [{ run: "r2", session: "s1" }]);
eq(twoRuns.map(s => s.run), ["r1", "r2"], "two runs of one session stay distinct entries");
eq(lnMergeHistory([{ id: "e1" }], [{ id: "e1" }, { id: "e2" }]).map(e => e.id), ["e1", "e2"], "history merges by event ID without duplicates");
eq(lnMergeInventory([{ learning_id: "L1" }], [{ learning_id: "L1" }, { learning_id: "L2" }]).map(i => i.learning_id), ["L1", "L2"], "the inventory merges by original learning ID");
eq(lnMergeSources([], [{ run: "r", session: "s" }, {}]).length, 2, "an entry without a full identity is never dropped");
eq(lnPageCollections({ page_collections: ["history"] }), ["history"], "page_collections identifies the collections a page owns");
eq(lnPageCollections({ sources: [], history: [] }), ["evidence", "history"], "an unwrapped page falls back to the collections it carries");

const fresh = lnDetailFresh("L1");
lnApplyDetailPage(fresh, { page_collections: ["evidence", "history"], item: { id: "L1", text: "full" },
  sources: [{ run: "r1", session: "s1", status: "available", core: { markdown: "core one" } }],
  history: [{ id: "e1", event: "created" }], next_evidence_cursor: "ev2", next_history_cursor: "hi2",
  evidence_complete: false, references_complete: true, history_complete: false, evidence_status: "available", read_only: true });
eq(fresh.sources.length, 1, "the initial page selects the first evidence page");
eq(fresh.history.length, 1, "the initial page selects the first history page");
eq([fresh.next_evidence_cursor, fresh.next_history_cursor], ["ev2", "hi2"], "each collection keeps its own cursor");
lnApplyDetailPage(fresh, { page_collections: ["evidence"], item: { id: "L1", text: "full" },
  sources: [{ run: "r2", session: "s1", status: "available", core: { markdown: "core two" } }],
  history: [], next_evidence_cursor: null, evidence_complete: true, references_complete: true });
eq(fresh.sources.map(s => s.run), ["r1", "r2"], "loading evidence keeps an earlier distinct run of the same session");
eq(fresh.history.map(e => e.id), ["e1"], "an evidence-only page never resets history");
eq(fresh.next_history_cursor, "hi2", "an evidence-only page keeps the history cursor");
lnApplyDetailPage(fresh, { page_collections: ["history"], sources: [], history: [{ id: "e2", event: "reviewed" }],
  next_history_cursor: null, history_complete: true });
eq(fresh.sources.map(s => s.run), ["r1", "r2"], "a history-only page never resets evidence");
eq(fresh.history.map(e => e.id), ["e1", "e2"], "history pages append in order");
eq([fresh.next_evidence_cursor, fresh.evidence_complete, fresh.history_complete], [null, true, true], "each collection keeps its own completeness");

// ------------------------------------------------------------- one bounded inventory page at a time
const inventory = [];
for (let i = 0; i < 230; i++) inventory.push({
  learning_id: `L${String(i).padStart(4, "0")}`, scope: "biz", tag: "rule", text: `historical record ${i}`,
  created: `2026-01-${String(1 + (i % 28)).padStart(2, "0")}T00:00:00Z`,
  work_state: i % 7 === 0 ? "implemented" : null, decision: i % 3 === 0 ? "accepted" : null, state: i % 7 === 0 ? "implemented" : "accepted",
  filed: i % 5 === 0, text_truncated: i === 0, reconciled_into: i === 1 ? "L0002" : null });
let inventoryPages = 0;
respond = async parsed => {
  if (!parsed.pathname.endsWith("/learnings/history")) return { error: { message: "unexpected " + parsed.pathname } };
  const after = parsed.searchParams.get("cursor");
  const start = after ? Number(after) : 0;
  const page = inventory.slice(start, start + 50);
  inventoryPages++;
  return { schema_version: 1, items: page, count: inventory.length, next_cursor: start + 50 < inventory.length ? String(start + 50) : null, read_only: true };
};
reset();
await lnHistoryLoad({ reset: true });
eq(inventoryPages, 1, "the first paint asks for exactly one inventory page");
eq(_ln.hist.items.length, 50, "the browser receives one bounded page, not the whole archive");
eq(requests[0], "/api/biz/project-a/learnings/history?limit=50", "the first request forwards the bounded limit");
while (_ln.hist.next_cursor) await lnHistoryLoad();
eq(_ln.hist.items.length, 230, "following next_cursor loads every inventory ID exactly once");
eq(new Set(_ln.hist.items.map(i => i.learning_id)).size, 230, "no inventory ID is duplicated across pages");
eq(requests[requests.length - 1].includes("cursor="), true, "later inventory pages forward the cursor");
eq(inventoryPages, 5, "230 records arrive in five bounded pages");
lnPaint();
let list = document.querySelector(".lnhist").innerHTML;
ok(list.includes("all 230 loaded"), "the list states truthful completeness once the inventory ends");
ok(list.includes("L0000") && list.includes("L0229"), "the list preserves original learning IDs across pages");
ok(list.includes("truncated summary"), "a truncated summary is marked explicitly");
ok(list.includes("Reconciled into L0002") && list.includes("Filed"), "filed and reconciled records keep their status");

// ------------------------------------------------------------- byte reassembly before collection following
reset();
const bigItem = { id: "BIG", learning_id: "BIG", bucket: "delta", tag: "rule", text: "oversized history", revision: 3
  , decision: { state: "accepted", note: "kept" } };
const bigPage = { item: bigItem, sources: [{ run: "r1", session: "s1", status: "missing", core: null }],
  history: [{ id: "h1", event: "created", actor_id: "human:owner", created_at: "2026-01-01T00:00:00Z",
    before_json: { state: "open" }, after_json: { state: "accepted" } }],
  page_collections: ["evidence", "history"], evidence_complete: true, references_complete: true, history_complete: true,
  evidence_status: "missing", read_only: true, source_kind: "frozen_extraction_cores", original_transcripts_included: false };
const encoded = JSON.stringify(bigPage);
const cut = [encoded.slice(0, 40), encoded.slice(40, 90), encoded.slice(90)];
respond = async parsed => {
  if (parsed.pathname === "/api/biz/project-a/learnings/BIG/review") {
    if (!parsed.searchParams.has("content_cursor")) return { learning_id: "BIG", revision: 3, complete: false, next_content_cursor: "c0" };
    const index = Number(parsed.searchParams.get("content_cursor").slice(1));
    return { learning_id: "BIG", revision: 3, content: cut[index], encoding: "json", complete: index === cut.length - 1,
      next_content_cursor: index === cut.length - 1 ? null : `c${index + 1}` };
  }
  return { error: { message: "unexpected " + parsed.pathname } };
};
await lnHistoryOpen("BIG");
eq(_ln.hist.detail.item.text, "oversized history", "an oversized page is reassembled and decoded before use");
eq(_ln.hist.detail.history.map(e => e.id), ["h1"], "history is read from the decoded JSON page, not the chunks");
eq(requests.filter(r => r.includes("content_cursor=")).length, 3, "the byte stream is followed chunk by chunk");
ok(requests.every(r => !r.includes("evidence_cursor") && !r.includes("history_cursor")), "the initial byte stream repeats the unchanged (empty) selection");

reset();
let selected = 0;
respond = async parsed => {
  if (parsed.pathname !== "/api/biz/project-a/learnings/BIG/review") return { error: { message: "unexpected" } };
  const evidence = parsed.searchParams.get("evidence_cursor");
  if (evidence !== "e2") return { learning_id: "BIG", revision: 3, complete: false, next_content_cursor: "s0" };
  if (!parsed.searchParams.has("content_cursor")) return { learning_id: "BIG", revision: 3, complete: false, next_content_cursor: "s0" };
  selected++;
  if (selected === 1) return { learning_id: "BIG", revision: 3, content: JSON.stringify({ ...bigPage, next_evidence_cursor: "e3", sources: [{ run: "r3", session: "s1", status: "available", core: { markdown: "third" } }], page_collections: ["evidence"], history: [] }).slice(0, 50), encoding: "json", complete: false, next_content_cursor: "s1" };
  const rest = JSON.stringify({ ...bigPage, next_evidence_cursor: "e3", sources: [{ run: "r3", session: "s1", status: "available", core: { markdown: "third" } }], page_collections: ["evidence"], history: [] }).slice(50);
  return { learning_id: "BIG", revision: 3, content: rest, encoding: "json", complete: true, next_content_cursor: null };
};
_ln.hist = lnHistFresh();
_ln.hist.detail = lnDetailFresh("BIG");
lnApplyDetailPage(_ln.hist.detail, bigPage);
_ln.hist.detail.next_evidence_cursor = "e2";
await lnHistoryMore("evidence");
eq(_ln.hist.detail.sources.map(s => s.run), ["r1", "r3"], "an oversized collection page merges only after reassembly");
eq(_ln.hist.detail.next_evidence_cursor, "e3", "the reassembled page advances its own cursor");
ok(requests.every(r => r.includes("evidence_cursor=e2")), "every chunk of a continuation repeats the same collection selection");
ok(!_ln.hist.detail.history.length === false && _ln.hist.detail.history.length === 1, "reassembling evidence never resets history");

// ------------------------------------------------------------- >200 evidence pairs and >100 nested results
reset();
const sourcePage = (from, total) => Array.from({ length: Math.min(20, total - from) }, (_, offset) => {
  const index = from + offset;
  return { run: `r${String(index % 2 === 0 ? 1 : 2)}`, session: `s${String(Math.floor(index / 2)).padStart(3, "0")}`,
    status: index < 3 ? "missing" : "available", core: index < 3 ? null : { markdown: `core ${index}` } };
});
const allSources = sourcePage(0, 210);
respond = async parsed => {
  if (parsed.pathname !== "/api/biz/project-a/learnings/L1/review") return { error: { message: "unexpected " + parsed.pathname } };
  const cursor = parsed.searchParams.get("evidence_cursor");
  if (!cursor) return { item: { id: "L1", text: "long evidence" }, sources: sourcePage(0, 210), history: [],
    next_evidence_cursor: "ev20", next_history_cursor: null, evidence_complete: false, references_complete: false,
    history_complete: true, evidence_status: "partial", page_collections: ["evidence", "history"], read_only: true,
    source_kind: "frozen_extraction_cores", original_transcripts_included: false };
  const from = Number(cursor.replace("ev", ""));
  return { item: { id: "L1", text: "long evidence" }, sources: sourcePage(from, 210), history: [],
    next_evidence_cursor: from + 20 < 210 ? `ev${from + 20}` : null, evidence_complete: from + 20 >= 210,
    references_complete: false, evidence_status: "partial", page_collections: ["evidence"] };
};
await lnHistoryOpen("L1");
while (_ln.hist.detail.next_evidence_cursor) await lnHistoryMore("evidence");
eq(_ln.hist.detail.sources.length, 210, "more than 200 source pairs are represented across bounded pages");
eq(new Set(_ln.hist.detail.sources.map(s => s.run + "\u0000" + s.session)).size, 210, "no source pair is lost or duplicated");
eq(_ln.hist.detail.sources.filter(s => s.run === "r1").length, 105, "two runs of the same sessions stay distinct (run 1)");
eq(_ln.hist.detail.sources.filter(s => s.run === "r2").length, 105, "two runs of the same sessions stay distinct (run 2)");
lnPaint();
const evidenceHtml = document.querySelector(".lnhist").innerHTML;
ok(evidenceHtml.includes("Saved evidence is partial"), "partial saved evidence is stated explicitly");
ok(evidenceHtml.includes("No saved core content for this run/session pair."), "a missing core is explicit, never silently complete");
ok(evidenceHtml.includes("Some reference metadata is unresolved"), "unresolved reference metadata remains an explicit gap");
ok(evidenceHtml.includes("run r1 · session s000") && evidenceHtml.includes("run r2 · session s000"), "the display keys sources by run and session");

reset();
const eventPage = (from, total) => Array.from({ length: Math.min(20, total - from) }, (_, offset) => ({
  id: `e${String(from + offset).padStart(3, "0")}`, event: "reviewed", actor_id: "human:owner",
  created_at: `2026-01-${String(1 + ((from + offset) % 28)).padStart(2, "0")}T00:00:00Z`,
  before_json: { state: "open" }, after_json: { state: "accepted" } }));
respond = async parsed => {
  if (parsed.pathname !== "/api/biz/project-a/learnings/L2/review") return { error: { message: "unexpected " + parsed.pathname } };
  const cursor = parsed.searchParams.get("history_cursor");
  if (!cursor) return { item: { id: "L2", text: "long history" }, sources: [], history: eventPage(0, 125),
    next_evidence_cursor: null, next_history_cursor: "hi20", evidence_complete: true, references_complete: true,
    history_complete: false, evidence_status: "missing", page_collections: ["evidence", "history"], read_only: true };
  const from = Number(cursor.replace("hi", ""));
  return { item: { id: "L2", text: "long history" }, sources: [], history: eventPage(from, 125),
    next_history_cursor: from + 20 < 125 ? `hi${from + 20}` : null, history_complete: from + 20 >= 125,
    page_collections: ["history"] };
};
await lnHistoryOpen("L2");
const sourcesBefore = _ln.hist.detail.sources.length;
while (_ln.hist.detail.next_history_cursor) await lnHistoryMore("history");
eq(_ln.hist.detail.history.length, 125, "history longer than the old recent-history bound is fully reachable");
eq(new Set(_ln.hist.detail.history.map(e => e.id)).size, 125, "no history event ID is lost or duplicated");
eq(_ln.hist.detail.sources.length, sourcesBefore, "loading history never resets evidence");
ok(_ln.hist.detail.history[0].id === "e000" && _ln.hist.detail.history[124].id === "e124", "page order preserves chronology");
lnPaint();
ok(document.querySelector(".lnhist").innerHTML.includes("Load more history") === false, "no continuation is offered once the history ends");

// ------------------------------------------------------------- read-only detail, no factual or engine controls
reset();
respond = async parsed => parsed.pathname.endsWith("/learnings/L3/review")
  ? { item: { id: "L3", bucket: "delta", tag: "rule", text: "historical <literal>", created: "2026-01-01T00:00:00Z",
      revision: 4, state: "accepted", decision: { state: "accepted", note: "owner note" }, agent_note: "agent note",
      work_state: "implemented", filed: true, reconciled_into: "L9", text_truncated: true,
      targets: [{ repository: "example/repo", paths: ["x"], commit: "a".repeat(40), remote: "origin", ref: "main", pushed: true }] },
    sources: [{ run: "r1", session: "s1", status: "missing", core: null }], history: [{ id: "e1", event: "created", actor_id: "human:owner", created_at: "2026-01-01T00:00:00Z", before_json: {}, after_json: { state: "open" } }],
    page_collections: ["evidence", "history"], evidence_complete: true, references_complete: true, history_complete: true,
    evidence_status: "missing", read_only: true, original_transcripts_included: false }
  : { error: { message: "old session-keyed detail must not be read" } };
await lnHistoryOpen("L3");
const detail = document.querySelector(".lnhist").innerHTML;
ok(detail.includes("read-only") && detail.includes("historical procedural record"), "the detail states its read-only history status");
ok(detail.includes("L3") && detail.includes("Implemented") && detail.includes("Filed") && detail.includes("Reconciled into L9"), "the detail keeps the original ID and status");
ok(detail.includes("historical &lt;literal&gt;") && !detail.includes("historical <literal>"), "historical text is escaped");
ok(detail.includes("Saved evidence is missing") && detail.includes("No saved core content"), "missing evidence is explicit");
for (const control of ["approve", "reject", "keep-both", "reopen", "save-note", "resume-intake", "start", "defer", "done", "drop"]) {
  ok(!detail.includes(`data-act="${control}"`) && !detail.includes(`data-${control}`), `the historical detail has no ${control} control`);
}
ok(!requests.some(r => r.endsWith("/learnings/L3")), "the old session-keyed cores detail is never used as archive evidence");

// ------------------------------------------------------------- errors stay explicit
reset();
respond = async () => ({ error: { message: "This learning is outside the authorized scope.", code: "forbidden" } });
await lnHistoryOpen("L4");
ok(document.querySelector(".lnhist").innerHTML.includes("outside the authorized scope"), "an unauthorized detail read is reported, not shown as empty evidence");

// ------------------------------------------------------------- existing global deep link still resolves to history
reset();
g.location.hash = "#/global/context/learning/L5";
state.scope = "global"; state.globalTab = "context";
eq(lnLinkedHistoryId(), "L5", "the global Learning route resolves an old candidate-ID deep link");
respond = async parsed => {
  if (parsed.pathname === "/api/root/learnings") return { items: [], counts: {} };
  if (parsed.pathname === "/api/root/learnings/history") return { schema_version: 1, items: [{ learning_id: "L5", text: "deep link", tag: "rule", scope: "global" }], count: 1, next_cursor: null, read_only: true };
  if (parsed.pathname === "/api/root/learnings/L5/review") return { item: { id: "L5", text: "deep link" }, sources: [], history: [],
    page_collections: ["evidence", "history"], evidence_complete: true, references_complete: true, history_complete: true,
    evidence_status: "missing", read_only: true, next_evidence_cursor: null, next_history_cursor: null };
  return { error: { message: "unexpected " + parsed.pathname } };
};
await openLearning();
eq(_ln.view, "history", "the deep link opens the historical view");
eq(_ln.hist.detail && _ln.hist.detail.id, "L5", "the deep link opens that record's read-only detail");
ok(requests.some(r => r === "/api/root/learnings/history?limit=50"), "the global inventory uses the root archive route");


// ------------------------------------------------------------- metadata survives independent pages
const metaDetail = lnDetailFresh("META");
lnApplyDetailPage(metaDetail, { page_collections: ["evidence", "history"], item: { id: "META", text: "meta" },
  sources: [], history: [], source_kind: "frozen_extraction_cores", original_transcripts_included: false,
  evidence_complete: true, references_complete: true, history_complete: true });
eq(metaDetail.source_kind, "frozen_extraction_cores", "the source kind is stored from the page");
eq(metaDetail.original_transcripts_included, false, "the transcripts flag is stored from the page");
lnApplyDetailPage(metaDetail, { page_collections: ["evidence"], sources: [{ run: "r", session: "s", status: "missing", core: null }],
  evidence_complete: true, next_evidence_cursor: null });
eq([metaDetail.source_kind, metaDetail.original_transcripts_included], ["frozen_extraction_cores", false],
  "independent pages preserve the page metadata");
_ln.hist = lnHistFresh(); _ln.hist.detail = metaDetail;
lnHistPaint();
ok(document.querySelector(".lnhist").innerHTML.includes("Original transcripts are not included"),
  "the rendered source section states the transcripts disclaimer");
const snapshotHtml = lnHistEventHtml({ id: "e1", event: "reviewed", actor_id: "human:owner", created_at: "2026-01-01T00:00:00Z",
  before_json: { revision: 1, state: "open", work_state: null, decision: { state: "accepted", note: "kept" }, scope: "biz", bucket: "delta", tag: "rule", text: "snapshot text" },
  after_json: { revision: 2, state: "accepted" } });
ok(snapshotHtml.includes("revision: 1") && snapshotHtml.includes("state: open") && snapshotHtml.includes("note: kept")
  && snapshotHtml.includes("bucket: delta") && snapshotHtml.includes("tag: rule"), "a snapshot summary lists the recorded fields");
ok(snapshotHtml.includes("Full recorded snapshots") && snapshotHtml.includes("&quot;before&quot;"), "full raw snapshots stay available");

// ------------------------------------------------------------- archive search by original ID
ok(lnMatch({ learning_id: "legacy-id-77", text: "unrelated", tag: "rule" }, "legacy-id-77", [], ""),
  "the archive search includes the original inventory ID");
_ln.q = "legacy-id-77"; _ln.hist.items = [{ learning_id: "legacy-id-77", text: "unrelated", tag: "rule" }];
ok(lnHistListHtml(_ln.hist).includes("legacy-id-77"), "an ID search lists the matching historical record");
_ln.q = "";

// ------------------------------------------------------------- a page larger than any single action budget
const HUGE_CHUNKS = 6000;
function hugePage(markerText) {
  return { item: { id: "HUGE", text: markerText }, sources: [], history: [],
    page_collections: ["evidence", "history"], evidence_complete: true, references_complete: true,
    history_complete: true, evidence_status: "missing", read_only: true };
}
const hugeText = "x".repeat(1_200_000);
const hugeJson = JSON.stringify({ ...hugePage(hugeText), sources: [{ run: "r1", session: "s1", status: "missing", core: null }] });
const hugeParts = Array.from({ length: Math.ceil(hugeJson.length / HUGE_CHUNKS) }, (_, i) => hugeJson.slice(i * HUGE_CHUNKS, (i + 1) * HUGE_CHUNKS));
ok(hugeParts.length > 200, "the reproduced page really needs more than 200 byte chunks");
let hugeRequests = [];
respond = async parsed => {
  if (parsed.pathname !== "/api/biz/project-a/learnings/HUGE/review") return { error: { message: "unexpected " + parsed.pathname } };
  const cursor = parsed.searchParams.get("content_cursor");
  if (!cursor) return { learning_id: "HUGE", revision: 1, complete: false, next_content_cursor: "c0" };
  const index = Number(cursor.slice(1));
  hugeRequests.push(cursor);
  return { learning_id: "HUGE", revision: 1, content: hugeParts[index], encoding: "json",
    complete: index === hugeParts.length - 1, next_content_cursor: index === hugeParts.length - 1 ? null : `c${index + 1}` };
};
reset();
await lnHistoryOpen("HUGE");
const hugeDetail = _ln.hist.detail;
ok(hugeDetail.streams.initial && hugeDetail.streams.initial.chunks === 40, "one action reads at most 40 byte chunks");
ok(hugeDetail.item === null && hugeDetail.sources.length === 0, "a partial page is never parsed or merged");
ok(hugeRequests.length === 40 && new Set(hugeRequests).size === 40, "the action reads 40 distinct chunks, no refetch");
ok(document.querySelector(".lnhist").innerHTML.includes("Continue loading"), "the pending page offers a Continue control");
let hugeActions = 1;
while (_ln.hist.detail.streams.initial) { await lnHistoryMore("initial"); hugeActions++; }
eq(hugeActions, Math.ceil(hugeParts.length / 40), "repeated Continue actions read the whole page");
eq(hugeDetail.item.text.length, hugeText.length, "a valid page beyond 200 chunks is reachable and decoded");
eq(hugeDetail.sources.length, 1, "the completed page merges its collection once");
eq(hugeRequests.length, hugeParts.length, "every chunk is fetched exactly once");
eq(new Set(hugeRequests).size, hugeParts.length, "no byte chunk is refetched across actions");

// ------------------------------------------------------------- independent continuations keep their selection and progress
const compactPage = { item: { id: "MIX", text: "mixed" }, sources: [{ run: "r0", session: "s0", status: "available", core: { markdown: "zero" } }],
  history: [], page_collections: ["evidence", "history"], next_evidence_cursor: "e2", next_history_cursor: "h2",
  evidence_complete: false, references_complete: true, history_complete: false, evidence_status: "available", read_only: true };
const hugeHistoryJson = JSON.stringify({ item: { id: "MIX", text: "mixed" },
  sources: [], history: [{ id: "h-e1", event: "reviewed", before_json: { state: "open" }, after_json: { state: "accepted", padding: "y".repeat(1_200_000) } }],
  page_collections: ["history"], next_history_cursor: "h3", history_complete: false, read_only: true });
const historyParts = Array.from({ length: Math.ceil(hugeHistoryJson.length / HUGE_CHUNKS) }, (_, i) => hugeHistoryJson.slice(i * HUGE_CHUNKS, (i + 1) * HUGE_CHUNKS));
let mixHistoryCursors = [], mixRequests = [];
respond = async parsed => {
  if (parsed.pathname !== "/api/biz/project-a/learnings/MIX/review") return { error: { message: "unexpected " + parsed.pathname } };
  mixRequests.push(parsed.search);
  const content = parsed.searchParams.get("content_cursor");
  const evidence = parsed.searchParams.get("evidence_cursor");
  const history = parsed.searchParams.get("history_cursor");
  if (content) {
    if (history !== "h2") return { error: { message: "content chunk lost its selection" } };
    const index = Number(content.slice(1));
    mixHistoryCursors.push(content);
    return { learning_id: "MIX", revision: 1, content: historyParts[index], encoding: "json",
      complete: index === historyParts.length - 1, next_content_cursor: index === historyParts.length - 1 ? null : `c${index + 1}` };
  }
  if (history === "h2") return { learning_id: "MIX", revision: 1, complete: false, next_content_cursor: "c0" };
  if (evidence === "e2") return { item: { id: "MIX", text: "mixed" }, sources: [{ run: "r1", session: "s1", status: "available", core: { markdown: "one" } }],
    history: [], page_collections: ["evidence"], next_evidence_cursor: null, evidence_complete: true, references_complete: true };
  if (history) return { error: { message: "unexpected history page" } };
  return compactPage;
};
reset();
await lnHistoryOpen("MIX");
eq(_ln.hist.detail.sources.length, 1, "the compact initial page loads");
await lnHistoryMore("history");
ok(_ln.hist.detail.streams.history && _ln.hist.detail.streams.history.parts.length > 0, "an oversized history page keeps its parts");
eq([_ln.hist.detail.history.length, _ln.hist.detail.next_history_cursor], [0, "h2"],
  "a pending page neither merges nor advances the history cursor");
const historyChunksFirst = _ln.hist.detail.streams.history.chunks;
await lnHistoryMore("evidence");
eq(_ln.hist.detail.sources.map(s => s.run), ["r0", "r1"], "evidence can complete while a history page is pending");
eq(_ln.hist.detail.streams.history.chunks, historyChunksFirst, "loading evidence does not discard history progress");
while (_ln.hist.detail.streams.history) await lnHistoryMore("history");
eq(_ln.hist.detail.history.map(e => e.id), ["h-e1"], "the long history page merges after completion");
eq(_ln.hist.detail.next_history_cursor, "h3", "the history cursor advances only after the page completes");
eq(mixHistoryCursors.length, historyParts.length, "the history byte stream is read once");
ok(mixRequests.filter(search => search.includes("content_cursor")).every(search => search.includes("history_cursor=h2")),
  "every byte chunk repeats the original history selection");


reset();
const jsonPage = JSON.stringify({ item: { id: "JSON", text: "json page" }, sources: [], history: [],
  page_collections: ["evidence", "history"], evidence_complete: true, references_complete: true,
  history_complete: true, evidence_status: "missing", read_only: true });
const jsonParts = Array.from({ length: Math.ceil(jsonPage.length / 8) }, (_, i) => jsonPage.slice(i * 8, (i + 1) * 8));
respond = async parsed => {
  if (!parsed.searchParams.get("content_cursor")) return { learning_id: "JSON", revision: 1, complete: false, next_content_cursor: "c0" };
  const index = Number(parsed.searchParams.get("content_cursor").slice(1));
  return { learning_id: "JSON", revision: 1, content: jsonParts[index], encoding: "json",
    complete: index === jsonParts.length - 1, next_content_cursor: index === jsonParts.length - 1 ? null : `c${index + 1}` };
};
const jsonFetch = g.fetch;
let jsonFailed = false;
g.fetch = async (url, options) => {
  if (!jsonFailed && String(url).includes("content_cursor=c2")) { jsonFailed = true; throw new SyntaxError("invalid JSON"); }
  return jsonFetch(url, options);
};
await lnHistoryOpen("JSON");
ok(_ln.hist.detail.error === "invalid JSON" && _ln.hist.detail.streams.initial.parts.length === 2,
  "an invalid JSON chunk keeps the two completed parts and reports it");
await lnHistoryMore("initial");
eq(_ln.hist.detail.item.text, "json page", "the saved byte cursor resumes after a JSON failure");
g.fetch = originalFetch;

// ------------------------------------------------------------- repeated cursor is an explicit protocol error
respond = async parsed => {
  if (!parsed.searchParams.get("content_cursor")) return { learning_id: "LOOP", revision: 1, complete: false, next_content_cursor: "c0" };
  return { learning_id: "LOOP", revision: 1, content: "x", encoding: "json", complete: false, next_content_cursor: "c0" };
};
reset();
await lnHistoryOpen("LOOP");
ok(_ln.hist.detail.error.includes("repeated a byte cursor"), "a non-progressing cursor is refused explicitly");
ok(!_ln.hist.detail.streams.initial && _ln.hist.detail.item === null, "the invalid stream is discarded, never looped or merged");
eq(requests.filter(r => r.includes("LOOP")).length, 2, "a repeated cursor stops after one observation, not a loop");

// ------------------------------------------------------------- transport/JSON failures recover with the saved state
respond = async parsed => {
  if (parsed.pathname !== "/api/biz/project-a/learnings/RETRY/review") return { error: { message: "unexpected" } };
  return { item: { id: "RETRY", text: "retry body" }, sources: [{ run: "r1", session: "s1", status: "available", core: { markdown: "one" } }],
    history: [], page_collections: ["evidence", "history"], evidence_complete: true, references_complete: true,
    history_complete: true, next_evidence_cursor: null, next_history_cursor: null, evidence_status: "available", read_only: true };
};
reset();
g.fetch = async () => { throw new Error("network offline"); };
await lnHistoryOpen("RETRY");
ok(_ln.hist.detail.loading === false && _ln.hist.detail.error === "network offline", "an initial transport failure releases loading and reports it");
ok(document.querySelector(".lnhist").innerHTML.includes("Retry"), "an initial failure offers an actionable retry");
g.fetch = originalFetch;
await lnHistoryMore("initial");
eq(_ln.hist.detail.item.text, "retry body", "retrying the same detail succeeds");

reset();
const midPage = JSON.stringify({ item: { id: "MID", text: "mid page" }, sources: [], history: [],
  page_collections: ["evidence", "history"], evidence_complete: true, references_complete: true,
  history_complete: true, evidence_status: "missing", read_only: true });
const midParts = Array.from({ length: Math.ceil(midPage.length / 8) }, (_, i) => midPage.slice(i * 8, (i + 1) * 8));
let midCount = 0, midFailed = false;
respond = async parsed => {
  if (!parsed.searchParams.get("content_cursor")) return { learning_id: "MID", revision: 1, complete: false, next_content_cursor: "c0" };
  if (midCount === 5 && !midFailed) { midFailed = true; throw new Error("network offline"); }
  const index = Number(parsed.searchParams.get("content_cursor").slice(1));
  midCount++;
  return { learning_id: "MID", revision: 1, content: midParts[index], encoding: "json",
    complete: index === midParts.length - 1, next_content_cursor: index === midParts.length - 1 ? null : `c${index + 1}` };
};
await lnHistoryOpen("MID");
ok(_ln.hist.detail.error === "network offline" && _ln.hist.detail.streams.initial.chunks === 5,
  "a mid-stream failure keeps the five completed parts and reports it");
const midBefore = midCount;
await lnHistoryMore("initial");
ok(_ln.hist.detail.item && _ln.hist.detail.item.id === "MID", "the saved next cursor resumes the same page");
eq(midCount - midBefore, midParts.length - 5, "the failed chunk was retried; no completed chunk was refetched");

reset();
respond = async parsed => {
  if (parsed.searchParams.get("evidence_cursor")) throw new Error("network offline");
  return compactPage;
};
await lnHistoryOpen("MIX2");
await lnHistoryMore("evidence");
eq([_ln.hist.detail.busy, _ln.hist.detail.error], ["", "network offline"], "a continuation failure releases busy and reports it");
eq(_ln.hist.detail.sources.length, 1, "a transient failure keeps already loaded authorized data");
ok(document.querySelector(".lnhist").innerHTML.includes("Retry loading"), "the continuation offers a retry with the saved cursor");

reset();
respond = async parsed => parsed.searchParams.get("evidence_cursor")
  ? { error: "Access was revoked", httpStatus: 403, coreError: { code: "forbidden" } }
  : { ...compactPage, item: { id: "LOST", text: "lost" } };
await lnHistoryOpen("LOST");
_ln.hist.detail.item = { id: "LOST", text: "lost" };
_ln.hist.detail.sources = [{ run: "r0", session: "s0", status: "available", core: { markdown: "zero" } }];
_ln.hist.detail.next_evidence_cursor = "e2";
await lnHistoryMore("evidence");
ok(_ln.hist.detail.error.includes("Access was revoked"), "an access-loss response is reported");
eq([_ln.hist.detail.item, _ln.hist.detail.sources.length], [null, 0], "access loss withholds already loaded source content");

reset();
const deferred = {};
g.fetch = async (url, opts) => {
  if (String(url).includes("LATE")) {
    return new Promise(resolve => { deferred.resolve = () => resolve({ ok: true, status: 200, headers: { get: () => null },
      json: async () => ({ item: { id: "LATE", text: "late write" }, sources: [], history: [], page_collections: ["evidence", "history"],
        evidence_complete: true, references_complete: true, history_complete: true, evidence_status: "missing", read_only: true }) }); });
  }
  return originalFetch(url, opts);
};
const lateLoad = lnHistoryOpen("LATE");
const lateDetail = _ln.hist.detail;
state.biz = "another-project";
deferred.resolve();
await lateLoad;
ok(!lateDetail.item, "a late page never writes into an abandoned workspace selection");
state.biz = "project-a";
g.fetch = originalFetch;


// ------------------------------------------------------------- inventory errors keep or withhold the right rows
const invA = { learning_id: "INV-A", text: "inventory A", tag: "rule", bucket: "delta", scope: "biz", state: "accepted" };
const invB = { learning_id: "INV-B", text: "inventory B", tag: "rule", bucket: "delta", scope: "biz", state: "accepted" };
reset();
let invInitial = true;
respond = async parsed => {
  if (!parsed.pathname.endsWith("/history")) return { items: [] };
  if (invInitial) { invInitial = false; throw new Error("synthetic network interruption"); }
  return { items: [invA], count: 2, next_cursor: "inv-next", read_only: true };
};
await lnHistoryLoad({ reset: true });
ok(_ln.hist.error === "synthetic network interruption" && _ln.hist.failed && _ln.hist.failed.reset,
  "an initial inventory failure is recorded for the exact selection");
ok(lnHistListHtml(_ln.hist).includes("data-hist-retry") && lnHistListHtml(_ln.hist).includes("Retry loading records"),
  "an initial inventory failure renders a retry control");
eq([_ln.hist.items.length, _ln.hist.next_cursor], [0, null], "a failed initial page does not invent rows or a cursor");
await lnHistoryRetry();
eq(_ln.hist.items.map(it => it.learning_id), ["INV-A"], "retrying the failed initial selection loads that page");
ok(lastRequest().endsWith("/history?limit=50"), "the initial retry does not add a cursor or start silently later");

reset();
let invContinuation = 0;
respond = async parsed => {
  if (!parsed.pathname.endsWith("/history")) return { items: [] };
  if (!parsed.searchParams.get("cursor")) return { items: [invA], count: 2, next_cursor: "inv-next", read_only: true };
  invContinuation++;
  if (invContinuation === 1) throw new SyntaxError("synthetic next-page JSON failure");
  return { items: [invB], count: 2, next_cursor: null, read_only: true };
};
await lnHistoryLoad({ reset: true });
await lnHistoryLoad();
eq(_ln.hist.items.map(it => it.learning_id), ["INV-A"], "a failed continuation keeps the authorized rows");
eq(_ln.hist.failed, { reset: false, cursor: "inv-next" }, "the failed continuation keeps its exact selection");
ok(lnHistListHtml(_ln.hist).includes("inventory A") && lnHistListHtml(_ln.hist).includes("data-hist-retry"),
  "rows and a retry control render together after a continuation failure");
await lnHistoryRetry();
eq(_ln.hist.items.map(it => it.learning_id), ["INV-A", "INV-B"], "the continuation retry appends the exact next page");
ok(lastRequest().includes("cursor=inv-next"), "the continuation retry does not reset to page one");

reset();
respond = async parsed => {
  if (!parsed.pathname.endsWith("/history")) return { items: [] };
  if (!parsed.searchParams.get("cursor")) return { items: [invA], count: 2, next_cursor: "inv-next", read_only: true };
  return { error: "Access was revoked", httpStatus: 403, coreError: { code: "forbidden" } };
};
await lnHistoryLoad({ reset: true });
_ln.hist.detail = lnDetailFresh("INV-A");
await lnHistoryLoad();
eq([_ln.hist.items.length, _ln.hist.next_cursor, _ln.hist.detail], [0, null, null],
  "access loss withholds the inventory rows, cursor and selected detail");
ok(_ln.hist.failed && _ln.hist.failed.reset && lnHistListHtml(_ln.hist).includes("Retry loading records"),
  "an access-loss inventory failure offers a fresh retry");
respond = async parsed => ({ items: [invA], count: 1, next_cursor: null, read_only: true });
await lnHistoryRetry();
ok(lastRequest().endsWith("/history?limit=50") && _ln.hist.items.length === 1,
  "the post-access-loss retry starts from a clean inventory selection");

// ------------------------------------------------------------- availability follows accumulated sources, not labels
const evidenceDetail = basis => {
  const detail = lnDetailFresh("EV");
  if (basis) lnApplyDetailPage(detail, basis);
  return detail;
};
const missingSource = { run: "r1", session: "s1", status: "missing", core: null };
const availableSource = { run: "r2", session: "s2", status: "available", core: { markdown: "saved two" } };
let ev = evidenceDetail({ page_collections: ["evidence"], sources: [missingSource], evidence_complete: false,
  references_complete: true, history_complete: true, evidence_status: "missing", read_only: true });
lnApplyDetailPage(ev, { page_collections: ["evidence"], sources: [availableSource], evidence_complete: true,
  references_complete: true, evidence_status: "available" });
eq([lnEvidenceView(ev).available, lnEvidenceView(ev).missing], [1, 1],
  "a later available page does not hide an earlier missing source");
ok(lnEvidenceView(ev).status === "partial" && !lnEvidenceView(ev).text.includes("All listed sources"),
  "the merged summary is partial, not all-available");
ok(lnHistEvidenceHtml(ev).includes("1/2"), "the rendered count matches the accumulated sources");
eq(ev.history_complete, false, "an evidence page leaves history completeness independent");

ev = evidenceDetail({ page_collections: ["evidence"], sources: [availableSource], evidence_complete: true,
  references_complete: true, history_complete: false, evidence_status: "available", read_only: true });
lnApplyDetailPage(ev, { page_collections: ["evidence"], sources: [missingSource], evidence_complete: true,
  references_complete: true, evidence_status: "missing" });
eq([lnEvidenceView(ev).available, lnEvidenceView(ev).missing], [1, 1], "a later missing page is visible in the summary");
ok(!lnEvidenceView(ev).text.includes("All listed sources"), "available-then-missing does not claim all available");
eq(ev.history_complete, false, "evidence merging does not change history state");

ev = evidenceDetail({ page_collections: ["evidence"], sources: [missingSource], evidence_complete: true,
  references_complete: true, evidence_status: "missing", read_only: true });
lnApplyDetailPage(ev, { page_collections: ["evidence"], sources: [], evidence_complete: true,
  references_complete: true, evidence_status: "available" });
eq(lnEvidenceView(ev).missing, 1, "an empty continuation cannot erase the earlier missing source");
ok(!lnEvidenceView(ev).text.includes("All listed sources"), "an empty available page does not claim all available");

// Manager reproduction: an empty, unfinished traversal page labels itself missing; it must not latch a
// record-level gap once the sole real source arrives and both completion flags are true.
ev = evidenceDetail({ page_collections: ["evidence"], sources: [], evidence_complete: false,
  references_complete: false, evidence_status: "missing", next_evidence_cursor: "more", read_only: true });
ok(lnEvidenceView(ev).text.includes("No saved sources have been loaded yet"),
  "an unfinished empty page says nothing has been loaded, regardless of its missing label");
ok(!lnEvidenceView(ev).text.includes("reports that saved source content is missing"),
  "an empty intermediate missing label is not a record-level assertion");
ok(!lnHistEvidenceHtml(ev).includes("No frozen source references are recorded"),
  "an unfinished empty page does not claim there are no references");
lnApplyDetailPage(ev, { page_collections: ["evidence"], sources: [availableSource], evidence_complete: true,
  references_complete: true, evidence_status: "available" });
eq([lnEvidenceView(ev).available, lnEvidenceView(ev).missing], [1, 0],
  "the sole available source is all the accumulated evidence");
eq(lnEvidenceView(ev).status, "available", "a complete all-available enumeration is available");
eq(lnEvidenceView(ev).text, "All listed sources have saved content.",
  "the empty intermediate missing label does not survive the completed enumeration");
eq(ev.evidence_status, "available", "the page label is the latest page, not a latched severity");

// A partial/incomplete intermediate label also cannot survive completed all-available traversal.
ev = evidenceDetail({ page_collections: ["evidence"], sources: [availableSource], evidence_complete: false,
  references_complete: false, evidence_status: "partial", next_evidence_cursor: "more", read_only: true });
ok(lnEvidenceView(ev).text.includes("enumeration is not complete"), "an unfinished all-available page is qualified");
lnApplyDetailPage(ev, { page_collections: ["evidence"], sources: [availableSource], evidence_complete: true,
  references_complete: true, evidence_status: "available" });
eq(lnEvidenceView(ev).status, "available", "an earlier partial traversal label does not latch");
eq(lnEvidenceView(ev).text, "All listed sources have saved content.",
  "completed all-available traversal is not called partial");

// A later empty page that labels itself missing cannot erase an available accumulated source.
ev = evidenceDetail({ page_collections: ["evidence"], sources: [availableSource], evidence_complete: true,
  references_complete: true, evidence_status: "available", read_only: true });
lnApplyDetailPage(ev, { page_collections: ["evidence"], sources: [], evidence_complete: true,
  references_complete: true, evidence_status: "missing" });
eq(lnEvidenceView(ev).status, "available", "an empty terminal missing label cannot erase an available source");
eq(lnEvidenceView(ev).text, "All listed sources have saved content.", "the accumulated source remains authoritative");

// Unresolved reference metadata is a real warning even when all loaded sources are available.
ev = evidenceDetail({ page_collections: ["evidence"], sources: [availableSource], evidence_complete: true,
  references_complete: false, evidence_status: "available", read_only: true });
eq(lnEvidenceView(ev).status, "available", "all available sources remain available");
ok(lnEvidenceView(ev).text.includes("reference metadata is unresolved"),
  "unresolved references warn when the sequence reports them");
ok(lnHistEvidenceHtml(ev).includes("Some reference metadata is unresolved"),
  "the rendered section keeps the unresolved-reference warning");

// A history-only page must not change the evidence view or completeness.
const beforeHistory = { ...lnEvidenceView(ev) };
lnApplyDetailPage(ev, { page_collections: ["history"], history: [{ id: "h1", event: "changed" }],
  history_complete: true });
eq(lnEvidenceView(ev), beforeHistory, "a history-only page does not change evidence availability");

ev = evidenceDetail({ page_collections: ["evidence"], sources: [], evidence_complete: true,
  references_complete: true, evidence_status: "missing", read_only: true });
ok(lnHistEvidenceHtml(ev).includes("No frozen source references are recorded"),
  "a complete empty evidence enumeration states there are no references");
ok(!lnEvidenceView(ev).text, "a complete empty evidence enumeration has no availability claim");

console.log(`\n${n - fails}/${n} passed${fails ? `, ${fails} FAILED` : ""}`);
process.exit(fails ? 1 : 0);
})();
