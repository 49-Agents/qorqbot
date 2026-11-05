#!/usr/bin/env node
// Headless smoke test for js/learning.js: loads core.js + san-juan-time.js + side.js + context.js + learning.js into
// one V8 context over a tiny DOM stub defined in this file, and exercises the pure helpers (palette,
// open/decided split, counts by tag, touches), the factual review card renderers and the page.
// The paginated historical view and its page merging are covered by test_learning_history.js.
//   node tests/test_learning.js
const fs = require("fs"), path = require("path"), vm = require("vm");
const HERE = path.join(__dirname, "..");
let fails = 0, n = 0;
const ok = (cond, msg) => { n++; if (!cond) { fails++; console.log(`FAIL ${msg}`); } else console.log(`ok   ${msg}`); };
const eq = (a, b, msg) => ok(JSON.stringify(a) === JSON.stringify(b), `${msg} (${JSON.stringify(a)} == ${JSON.stringify(b)})`);

// ------------------------------------------------------------- DOM stub
const mkEl = (tag = "div") => ({
  tagName: tag.toUpperCase(), _q: {}, children: [], style: { setProperty() {} }, dataset: {}, classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  hidden: false, textContent: "", innerHTML: "", disabled: false, value: "",
  querySelector(s) { return this._q[s] || (this._q[s] = mkEl()); }, querySelectorAll() { return []; }, appendChild(c) { this.children.push(c); return c; }, remove() {},
  addEventListener() {}, removeEventListener() {}, setAttribute() {}, getAttribute() { return null; }, focus() {}, closest() { return null; },
  getBoundingClientRect() { return { left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 }; },
});
const byId = {}, store = {}, notes = [];
const g = globalThis;
g.window = g;
g.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
g.location = { search: "", hash: "" };
g.document = {
  body: mkEl("body"), activeElement: null,
  querySelector: s => (s.startsWith("#") ? (byId[s.slice(1)] = byId[s.slice(1)] || mkEl()) : null),
  querySelectorAll: () => [], createElement: t => mkEl(t), addEventListener() {}, removeEventListener() {},
};
g.addEventListener = () => {};
g.requestAnimationFrame = f => f();
g.fetch = async () => ({ json: async () => ({}) });
g.setInterval = () => 0;
g.innerWidth = 1200; g.innerHeight = 800;
for (const f of ["core", "san-juan-time", "side", "context", "learning"]) vm.runInThisContext(fs.readFileSync(path.join(HERE, "js", f + ".js"), "utf8"), { filename: f + ".js" });
g.notify = t => notes.push(t);
g.toast = t => notes.push({ text: t });
state.biz = "project-a";

// ------------------------------------------------------------- fixtures
// A delta is retained history: it is never reviewed, decided, edited or worked again.
const DELTA = { id: "01M1X657VW5A3X129H77F2AYWQ", bucket: "delta", tag: "rule", scope: "agent", text: "Never pull or rebase from inside a session, Sam says.",
  evidence: { session: "9f3a1b2c-dead-beef", turns: ["T3"], owner_quote: "never rebase in a session" },
  evidence_more: [{ session: "aaaabbbb-1", turns: ["T9"], owner_quote: "again: no rebase" }],
  observed: { biz: "ProjectA/hq", agent: "onbashy", agent_kind: "biz-agents-manager" },
  votes: 3, passes: 3, variants: [{ pass: 1, text: "no rebase", scope: "agent", confidence: 0.9 }],
  created: "2026-09-07T05:41:18Z", state: "open" };
const DELTA_ACCEPTED = { ...DELTA, id: "01M1X657VW5A3X129H77F2AYWS", tag: "idea", scope: "global", state: "accepted",
  decision: { state: "accepted", note: "worth trying", actor: "human:user", created: "2026-09-07T09:00:00Z" } };
const FACT_OPEN = { id: "01M1X657VW5A3X129H77F2AYWR", bucket: "context", tag: "fact", scope: "biz", text: "The fleet box runs over tailscale.",
  evidence: { session: "1234abcd-x", turns: ["T1"], owner_quote: "it is on tailscale" },
  observed: { biz: "ProjectA/hq", agent: "onbashy" }, votes: 2, passes: 3, created: "2026-09-06T05:41:18Z" };
const FACT_REJECTED = { ...FACT_OPEN, id: "01M1X657VW5A3X129H77F2AYWT", tag: "question", text: "Should we post on X?",
  created: "2026-09-04T05:41:18Z", state: "rejected",
  decision: { state: "rejected", note: "already answered", actor: "human:user", created: "2026-09-07T11:00:00Z" } };
const FACT_ACCEPTED = { ...FACT_OPEN, id: "01M1X657VW5A3X129H77F2AYX1", tag: "idea", text: "A weekly digest would help.",
  created: "2026-09-05T05:41:18Z", state: "accepted",
  decision: { state: "accepted", note: "worth trying", actor: "human:user", created: "2026-09-07T09:00:00Z" } };
const ITEMS = [DELTA, FACT_OPEN, FACT_ACCEPTED, FACT_REJECTED];
const PEOPLE = [{ slug: "sam", name: "Sam", aka: ["Samuel"] }, { slug: "pat", name: "Pat", aka: [] }];

// ------------------------------------------------------------- palette
eq(LN_TAGS.map(t => t[0]), ["fact", "decision", "rule", "idea", "question"], "LN_TAGS order");
eq(LN_TAGS[0][2], COLORS.blue, "fact is blue");
eq(LN_TAGS[2][2], COLORS.red, "rule is red");
ok(new Set(LN_TAGS.map(t => t[2])).size === 5, "five distinct type colours");
eq(LN_SCOPES.map(s => s[0]), ["biz", "agent", "agent_kind", "global"], "LN_SCOPES order");
ok(LN_SCOPES.every(s => /^<svg /.test(s[2]) && s[2].includes("stroke-width")), "every scope icon is an inline stroke svg");
ok(LN_SCOPES[1][2].includes(AICONS.bot), "the agent scope icon is the AICONS bot");
eq(lnTag("nope").label, "nope", "an unknown tag falls back to its own key");

// ------------------------------------------------------------- split, counts, touches
const sp = lnSplit(ITEMS);
eq(sp.open.map(i => i.id), [FACT_OPEN.id], "only factual open candidates are waiting on the owner");
eq(sp.decided.map(i => i.id), [FACT_REJECTED.id, FACT_ACCEPTED.id], "decided factual cards are newest decision first");
ok(!lnSplit([DELTA]).open.length && !lnSplit([DELTA_ACCEPTED]).decided.length, "retained deltas never enter the review columns");
eq(lnCountsByTag(ITEMS), { fact: 1, decision: 0, rule: 1, idea: 0, question: 0 }, "counts by tag are over open items only");
eq(lnCountsByTag([]), { fact: 0, decision: 0, rule: 0, idea: 0, question: 0 }, "counts of nothing are all zero");
ok(lnScopeLocked(FACT_OPEN) && !lnScopeLocked(DELTA), "a context-bucket item has its scope locked");

eq(lnTouches(DELTA, []), [], "scope is the icon, never a chip");
eq(lnTouches(FACT_OPEN, []), [], "bucket is an icon, never a chip");
const ctxHead = lnCardHtml(FACT_OPEN, []);
ok(ctxHead.includes('class="lntag lnbucket" data-bucket="context"') && ctxHead.includes('>Context</span>'), "Context uses a text chip");
ok(ctxHead.indexOf('lnbucket') < ctxHead.indexOf('>Fact</span>'), "bucket precedes fact tag");
const deltaHead = lnCardHtml(DELTA, []);
ok(deltaHead.includes('>Delta</span>'), "Delta uses a text chip");
ok(!lnCardHtml({id:"x", tag:"fact", text:"t"}, []).includes('lnbucket'), "no bucket, no chip");
eq(lnTouches(DELTA, PEOPLE), [{ label: "Sam", kind: "person" }], "a person named in the text becomes a chip");
eq(lnTouches({ scope: "biz", text: "samuel said so" }, PEOPLE), [{ label: "Sam", kind: "person" }], "an aka matches, case-insensitively");
eq(lnTouches({ scope: "biz", text: "Alpine routes" }, PEOPLE), [], "a name inside another word is not a match");
eq(lnTouches({}, []), [], "an empty item touches nothing");

// ------------------------------------------------------------- retained historical records are read-only
ok(lnIsHistorical(DELTA) && !lnIsHistorical(FACT_OPEN), "a delta is historical; a factual candidate is not");
const legacy = lnCardHtml(DELTA, PEOPLE);
ok(legacy.includes("historical · read-only") && legacy.includes('data-act="history"'), "a delta is labelled read-only history and offers its detail");
for (const control of ['data-act="approve"', 'data-act="reject"', 'data-act="reopen"', 'data-act="keep-both"',
  'data-act="save-note"', 'data-act="resume-intake"', 'data-act="source"', 'class="lnscope ', 'class="lnnote']) {
  ok(!legacy.includes(control), `a delta card never renders ${control}`);
}
const legacyAccepted = lnCardHtml(DELTA_ACCEPTED, []);
ok(!legacyAccepted.includes("awaiting selection"), "an accepted historical delta is never called awaiting selection");
ok(legacyAccepted.includes("Accepted") && legacyAccepted.includes("01M1X657VW5A3X129H77F2AYWS"), "historical status and original ID are preserved");

// ------------------------------------------------------------- one factual card
const card = lnCardHtml(FACT_OPEN, PEOPLE);
ok(card.includes('data-act="approve"') && card.includes('data-act="reject"') && !card.includes('data-act="reopen"'), "explicit open factual state shows review controls, not Revert");
ok(card.includes('class="mkcard lncard"') && card.includes('tabindex="0"'), "an open factual card is a focusable .mkcard");
ok(card.includes(`data-id="${FACT_OPEN.id}"`), "the card carries its id");
ok(card.includes(">Fact<") && card.includes(`--tc:${COLORS.blue}`), "the type is a coloured pill with its label");
LN_SCOPES.forEach(([k]) => ok(card.includes(`data-scope="${k}"`), `the scope picker offers ${k}`));
ok(card.includes('data-scope="biz"') && !/data-scope="biz"[^>]*disabled/.test(card), "a context item can still be biz");
["agent", "agent_kind", "global"].forEach(k => ok(new RegExp(`data-scope="${k}" disabled`).test(card), `a context item's ${k} button is disabled`));
ok(card.includes("Context stays in its originating workspace"), "…and says why");
ok(card.includes(">2/3<"), "votes read k/n");
ok(card.includes("it is on tailscale") && card.includes("T1 · onbashy · 1234abcd"), "the evidence quote carries turn, agent and session prefix");
ok(card.includes('class="lnnote"') && card.includes("hidden"), "the notes box is on the card but closed");
ok(card.includes('data-act="reject"') && card.includes('data-act="approve"') && !card.includes('data-act="notes"'), "Reject · Approve, no notes button");
_ln.notes[FACT_OPEN.id] = "keep this";
ok(!/<textarea class="lnnote"[^>]*hidden/.test(lnCardHtml(FACT_OPEN, PEOPLE)) && lnCardHtml(FACT_OPEN, PEOPLE).includes(">keep this</textarea>"), "a drafted note keeps the box open with the draft in it");
delete _ln.notes[FACT_OPEN.id];
ok(lnCardHtml({...FACT_OPEN, text: "Sam says the fleet box runs over tailscale."}, PEOPLE).includes("Sam</span>"), "the person chip is rendered");

const done = lnCardHtml(FACT_ACCEPTED, []);
ok(done.includes("lncard done") && !done.includes('tabindex="0"'), "a decided factual card is not focusable");
ok(done.includes('class="lnball" title="accepted"') && done.includes("--sc:#30D158"), "accepted is a green ball, the notes gutter's done colour");
ok(!done.includes("lnstate"), "no state word, the ball says it");
ok(done.includes('class="lnscopeico" title="This project"'), "a decided card shows its scope as one icon");
ok(done.includes("worth trying"), "accepted card shows the owner note");
ok(lnCardHtml(FACT_REJECTED, []).includes("--sc:#FF453A"), "rejected is a red ball, the notes gutter's blocked colour");
ok(!lnCardHtml(FACT_REJECTED, []).includes('data-act="approve"'), "a decided card has no approve");
const pendingAccepted = lnCardHtml({...FACT_ACCEPTED, intake_pending: {state: "failed"}}, []);
ok(pendingAccepted.includes('data-act="resume-intake"'), "accepted catch-up intake remains resumable");
ok(!pendingAccepted.includes('data-act="reopen"'), "pending accepted intake cannot revert its frozen snapshot");
ok(!pendingAccepted.includes('data-act="save-note"'), "pending accepted intake cannot replace its frozen owner note");
ok(done.includes('data-act="reopen"') && done.includes(">Revert<"), "a decided card has Revert");
ok(lnCardHtml({ id: "x", tag: "fact", text: "<script>bad" }, []).includes("&lt;script&gt;"), "text is escaped");

// ------------------------------------------------------------- page
_ln.items = ITEMS;
state.ctx = "_learning";
lnPaint();
const page = byId.ctxview.innerHTML, colL = () => byId.ctxview.querySelector(".lnq .lnbody").innerHTML, colR = () => byId.ctxview.querySelector(".lndone .lnbody").innerHTML;
ok(page.includes("<h2>Learning</h2>") && page.includes('data-p=""') && page.includes("Learning</span>"), "the page has the crumbs and the heading");
ok(page.includes('data-view="review"') && page.includes('data-view="history"'), "the page separates factual review from the historical inventory");
ok(page.includes("Historical procedural records") && page.includes("Read-only history") === false, "the history switch is labelled");
ok(page.includes('class="lnsplit"') && page.includes('class="lnq"') && page.includes('class="lndone"'), "two review columns");
ok(!page.includes("not built yet"), "obsolete application footnote is removed");
ok(page.includes('id="lnsearch"'), "the page has the search box");
eq((colL().match(/mkcard lncard/g) || []).length, 1, "every factual open item is a card on the left");
eq((colR().match(/mkcard lncard/g) || []).length, 2, "every decided factual item is a card on the right");
eq(byId.ctxview.querySelector(".lnq .cnt").textContent, "1", "the left count");
_ln.items = [];
lnPaint();
ok(colL().includes("Nothing waiting on you.") && colR().includes("Nothing decided yet."), "empty states in the house voice");
lnPaint(true);
ok(colL().includes("loading…"), "the first paint says loading");

// ------------------------------------------------------------- search
ok(lnMatch(FACT_OPEN, "", PEOPLE), "an empty query matches everything");
ok(lnMatch(DELTA, "REBASE", PEOPLE), "the text matches, case-insensitively");
ok(lnMatch(DELTA, "rule onbashy", PEOPLE), "type and agent match, all words must");
ok(!lnMatch(DELTA, "rule marketing", PEOPLE), "…so one missing word is a miss");
ok(lnMatch(FACT_OPEN, "context", []) && !lnMatch(DELTA, "context", []), "the bucket is searchable");
ok(lnMatch(FACT_ACCEPTED, "worth trying", []), "a decided card's note is searchable");
ok(lnMatch(FACT_OPEN, "fact", [], "my draft") && lnMatch(FACT_OPEN, "draft", [], "my draft"), "an open card's draft is searchable");
_ln.items = ITEMS; _ln.q = "";
lnPaint();
_ln.q = "fleet"; lnPaintCols();
eq((colL().match(/mkcard lncard/g) || []).length, 1, "the search filters the left column");
ok(colR().includes("No decided card matches."), "…and the right column separately");
eq(byId.ctxview.querySelector(".lnq .cnt").textContent, "1", "an all-matching filtered column shows its total");
ok(byId.ctxview.querySelector(".lndone .cnt").textContent === "0/2", "a filtered column counts matches over total");
_ln.q = ""; lnPaintCols();

// ------------------------------------------------------------- decisions without a dialog
(async () => {
const reopened = { id: "01M1X657VW5A3X129H77F2AYWU", bucket: "context", tag: "fact", scope: "biz", text: "reverted one", created: "2026-09-03T00:00:00Z",
  state: "open", decision: { state: "open", note: "was accepted for this reason", created: "2026-09-07T12:00:00Z" },
  reopened: { state: "open", note: "was accepted for this reason", created: "2026-09-07T12:00:00Z" } };
ok(lnOpenItem(reopened), "a reopened item is open");
eq(lnSplit([reopened, FACT_ACCEPTED]).open.map(i => i.id), [reopened.id], "…and sits on the left");
_ln.items = [reopened, ...ITEMS]; _ln.notes = {};
const posts = [];
g.fetch = async (url, opts) => { posts.push([url, JSON.parse(opts.body)]); return { json: async () => ({ ok: true }) }; };
notes.length = 0;
await lnReject(FACT_OPEN);
ok(posts.length === 0 && notes.some(t => /needs a note/.test(t.text)), "Reject without a note posts nothing and says so");
_ln.notes[FACT_OPEN.id] = "already in CLAUDE.md";
await lnReject(FACT_OPEN);
ok(!!posts[0][1].idempotency_key, "Review has a request identity");
delete posts[0][1].idempotency_key;
eq(posts[0], [`/api/biz/project-a/learnings/${FACT_OPEN.id}/state`, { state: "rejected", note: "already in CLAUDE.md" }], "Reject with a note posts it as the reason, no dialog");
ok(FACT_OPEN.state === "rejected" && FACT_OPEN.decision.note === "already in CLAUDE.md", "…and the card moves right");
ok(_ln.notes[FACT_OPEN.id] === undefined, "the draft is dropped once decided");
await lnReopen(FACT_OPEN);
delete posts[1][1].idempotency_key;
eq(posts[1][1], { state: "open" }, "Revert preserves the owner note by omitting it");
ok(lnOpenItem(FACT_OPEN) && !FACT_OPEN.decision, "…and the card is open again");
eq(_ln.notes[FACT_OPEN.id], "already in CLAUDE.md", "…with the old note as the draft");
eq(JSON.parse(store["lnnotes:project-a"])[FACT_OPEN.id], "already in CLAUDE.md", "drafts persist per business in localStorage");
_ln.notes[FACT_OPEN.id] = "ok then";
await lnApprove(FACT_OPEN);
delete posts[2][1].idempotency_key;
eq(posts[2][1], { state: "accepted", note: "ok then" }, "Approve rides with the draft note");
g.fetch = async () => ({ json: async () => ({ error: "boom" }) });
notes.length = 0;
await lnApprove(reopened);
ok(notes.some(t => /could not be confirmed/.test(t.text)), "a failed write toasts");

// Slow writes serialize; a lost response reconciles without a second POST or a full list read.
const first = {id:'queue-a',bucket:'context',state:'open'}, second = {id:'queue-b',bucket:'delta',state:'open'};
_ln.items = [first, second];
let releaseFirst, writes = 0, reads = 0;
g.fetch = async (url, opts = {}) => {
  if (opts.body) {
    writes++;
    if (writes === 1) await new Promise(resolve => { releaseFirst = resolve; });
    if (writes === 2) throw new Error('Lost response');
    return {json:async()=>({item:{...first,state:'accepted',decision:{state:'accepted',note:''},filing:{upload_id:'upload'}}})};
  }
  reads++; ok(url.endsWith('/queue-b'), 'reconcile only the affected learning');
  return {json:async()=>({item:{...second,state:'accepted',decision:{state:'accepted',note:''},revision:2}})};
};
const a = lnApprove(first), b = lnApprove(second);
await Promise.resolve(); await Promise.resolve();
eq(writes,1,'only one review write is in flight');
releaseFirst(); await Promise.all([a,b]);
eq(writes,2,'each decision sent once'); eq(reads,1,'lost reply read back once');
eq(_ln.items.map(it=>it.id),['queue-b'],'filed context removed without full list fetch');
eq(second.state,'accepted','lost response reconciles accepted decision');

// house rules: no emoji, no marketing state reads, no dialogs
ok(!/modal\(/.test(fs.readFileSync(path.join(HERE, "js", "learning.js"), "utf8")), "learning.js opens no modal");
const src = fs.readFileSync(path.join(HERE, "js", "learning.js"), "utf8");
ok(!/_mk\s*[.[]/.test(src), "learning.js never reads the marketing state");
ok(!/[\u{1F300}-\u{1FAFF}\u{2700}-\u{27BF}]/u.test(src), "no emoji in learning.js");

const relatedGroups = lnRelatedGroups([{id:'a',contradicts:['b']},{id:'b'},{id:'c'}]);
eq(relatedGroups.map(g => g.map(i => i.id)), [['a','b'],['c']], 'conflicting pending candidates are grouped');
const combinedHtml = lnReconciliationHtml({reconciliations:[{kind:'answered',automatic:true,reason:'Later answer',earlier:{text:'Question <literal>',evidence:{owner_quote:'How much?'}}}]});
ok(combinedHtml.includes('Question answered') && combinedHtml.includes('Keep both separately'), 'combined answer explains resolution and offers undo');
ok(combinedHtml.includes('&lt;literal&gt;') && !combinedHtml.includes('<literal>'), 'earlier statement is escaped');
const proposedHtml = lnReconciliationHtml({reconciliations:[{kind:'updated',automatic:false,reason:'Later evidence',earlier:{text:'Accepted earlier'}}]});
ok(proposedHtml.includes('Proposed update') && !proposedHtml.includes('data-act="keep-both"'), 'protected earlier learning remains a proposed update');

console.log(`\n${n - fails}/${n} passed${fails ? `, ${fails} FAILED` : ""}`);
process.exit(fails ? 1 : 0);
})();