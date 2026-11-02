// ---------------------------------------------------------------- learning
// The Learning page of the Context tab separates two collections: factual Context review (left: waiting on
// you, right: decided) and a paginated, read-only view of retained historical procedural records. Historical
// records are never accepted work: they have no decision, edit, work or reconciliation controls and no
// automatic conversion path. Cards share the review-card markup (css/review-cards.css) with Signoffs.
// Depends on core.js.
//
// The archive is loaded as bounded pages, never as one server-side loop. Each detail page is reassembled
// byte-first (content_cursor, repeating the same evidence/history selection) and only then are the independent
// evidence/history cursors followed. Inventory entries merge by original learning ID, sources by (run, session)
// and history events by event ID, so a later page never loses or duplicates an earlier one.

// ---------------------------------------------------------------- palette & icons
// Type is a colour (five from the shared COLORS palette, so a learning never reads as an agent glyph),
// scope is an icon. Lucide strokes at the same weight as the other icons.
const LN_TAGS = [
  ["fact", "Fact", COLORS.blue],
  ["decision", "Decision", COLORS.plum],
  ["rule", "Rule", COLORS.red],
  ["idea", "Idea", COLORS.amber],
  ["question", "Question", COLORS.teal],
];
const LN_TAG_BY_KEY = Object.fromEntries(LN_TAGS.map(([k, label, hex]) => [k, { key: k, label, hex }]));
const lnTag = k => LN_TAG_BY_KEY[k] || { key: String(k || ""), label: String(k || "other"), hex: "var(--fg-3)" };
const lnSvg = inner => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
const LN_SCOPES = [
  ["biz", "This project", lnSvg('<path d="m2 7 4.41-4.41A2 2 0 0 1 7.83 2h8.34a2 2 0 0 1 1.42.59L22 7"/><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><path d="M15 22v-4a2 2 0 0 0-2-2h-2a2 2 0 0 0-2 2v4"/><path d="M2 7h20"/>')],
  ["agent", "This agent", lnSvg(AICONS.bot)],
  ["agent_kind", "This agent kind", lnSvg('<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m6.08 12.9-3.49 1.59a1 1 0 0 0 0 1.81l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83l-3.51-1.6"/>')],
  ["global", "All projects", lnSvg(AICONS.globe)],
];
const LN_SCOPE_BY_KEY = Object.fromEntries(LN_SCOPES.map(([k, label, ico]) => [k, { key: k, label, ico }]));
const LN_BUCKETS = {
  context: { label: "Context", hex: COLORS.teal },
  delta: { label: "Delta", hex: COLORS.plum },
};
// the decided card's ball wears the same two colours as the notes gutter balls (done / blocked)
const LN_STATE_HEX = { accepted: "#30D158", "in-progress": "#30D158", rejected: "#FF453A", "rejected-in-work": "#FF453A" };
const lnCanRevert = it => ["accepted", "rejected"].includes(it && it.state) && !it.intake_pending && !(it.filing && it.filing.upload_id);

// notes: draft notes per item, kept in localStorage per business until the decision goes through, so a
// repaint, a reload or a revert never loses them; noteOpen: cards whose notes box is open; busy: ids with a
// write in flight; q: the search box; view: factual review or the read-only historical inventory;
// hist: the paginated archive state (inventory, selected detail, per-collection cursors and completeness)
let _ln = { biz: null, items: [], counts: null, notes: {}, noteOpen: {}, busy: {}, q: "", view: "review", hist: null };
const lnWorkspace = () => state.scope === 'global' ? 'global' : state.biz;
const lnBase = () => state.scope === 'global' ? '/root/learnings' : `/biz/${state.biz}/learnings`;
const lnActive = () => state.scope === 'global' ? state.globalTab === 'context' && location.hash.split('/')[3] === 'learning' : state.ctx === '_learning';
const lnDraftKey = () => `lnnotes:${lnWorkspace()}`;
function lnDraftsLoad() { try { return JSON.parse(ls(lnDraftKey()) || "{}") || {}; } catch (e) { return {}; } }
function lnDraftsSave() { ls(lnDraftKey(), JSON.stringify(_ln.notes)); }
function lnFresh() { return { biz: lnWorkspace(), items: [], counts: null, notes: lnDraftsLoad(), noteOpen: {}, busy: {}, q: "", view: "review", hist: null }; }
// The inventory page size matches the operation's bounded limit; every request stays one bounded page.
const LN_HISTORY_LIMIT = 50;
// One user action reads at most this many byte chunks; a larger valid page is resumed by its Continue control.
const LN_CONTENT_CHUNKS_PER_ACTION = 40;
// The global Learning route keeps the candidate ID as its last hash segment, so an existing link still opens
// that record's read-only history. The business page has no per-ID route in the existing router.
function lnLinkedHistoryId() {
  if (state.scope !== 'global') return '';
  try { return decodeURIComponent(location.hash.split('/')[4] || ''); } catch (error) { return ''; }
}

// ---------------------------------------------------------------- pure helpers
const lnOpenItem = it => !!it && (!it.state || it.state === "open");
const lnCreated = it => Date.parse((it && it.created) || "") || 0;
const lnDecided = it => Date.parse((it && it.decision && it.decision.created) || (it && it.created) || "") || 0;
const lnNote = it => (it && it.decision && it.decision.note) || "";
const lnErr = r => r?.error?.message || r?.error || "No reply from the server";
const lnWho = a => String(a || "").replace(/^(agent|human|system):/, "");
// "in 3h" / "2h ago" / "in 1d"; `t` is an ISO string or epoch seconds
function lnRel(t, now = Date.now()) {
  const ms = typeof t === "number" ? t * 1000 : Date.parse(t);
  if (isNaN(ms)) return "";
  const d = ms - now, a = Math.abs(d);
  if (a < 60e3) return "now";
  const u = a < 3600e3 ? `${Math.round(a / 60e3)}m` : a < 129600e3 ? `${Math.round(a / 3600e3)}h` : `${Math.round(a / 86400e3)}d`;
  return d < 0 ? `${u} ago` : `in ${u}`;
}
function lnSourceAge(t, now = Date.now()) {
  const at = Date.parse(t || "");
  if (!Number.isFinite(at)) return "";
  const minutes = Math.max(0, Math.floor((now - at) / 60000));
  if (minutes < 1) return "just now";
  const [n, unit] = minutes < 60 ? [minutes, "minute"] : minutes < 1440 ? [Math.floor(minutes / 60), "hour"] : [Math.floor(minutes / 1440), "day"];
  return `${n} ${unit}${n === 1 ? "" : "s"} ago`;
}
// context-bucket items are scope biz by rule (the CLI refuses anything else), so their other buttons are dead
const lnScopeLocked = it => !!it && it.bucket === "context";

// open newest first, decided newest decision first. Retained deltas belong to the historical view, never to the
// factual columns, so they are excluded even if an older server still returns them here.
function lnSplit(items) {
  const all = (Array.isArray(items) ? items : []).filter(it => !lnIsHistorical(it) && it.state !== "implemented" && !(it.filing && it.filing.upload_id));
  return {
    open: all.filter(lnOpenItem).slice().sort((a, b) => lnCreated(b) - lnCreated(a)),
    decided: all.filter(it => !lnOpenItem(it)).slice().sort((a, b) => lnDecided(b) - lnDecided(a)),
  };
}
// how many open candidates of each type; the status pane draws one line per LN_TAGS key
function lnCountsByTag(items) {
  const c = Object.fromEntries(LN_TAGS.map(([k]) => [k, 0]));
  for (const it of (items || []).filter(lnOpenItem)) if (it.tag in c) c[it.tag]++;
  return c;
}
// Who a decision would touch: every
// person the text names. Pure so the test can check it without a DOM.
function lnTouches(item, people) {
  const it = item || {}, obs = it.observed || {}, out = [];
  const text = String(it.text || "");
  for (const p of people || []) {
    const names = [p.name].concat(p.aka || []).filter(Boolean);
    if (names.some(n => new RegExp(`\\b${String(n).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i").test(text))) {
      out.push({ label: p.name, kind: "person" });
    }
  }
  return out;
}

// The search box: every whitespace-separated word must appear somewhere on the card — text, type, scope,
// agent, the decision's or the draft's note, the touch chips, the id.
function lnMatch(item, q, people, draft) {
  const words = String(q || "").toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const it = item || {}, obs = it.observed || {};
  const hay = [it.text, it.tag, it.scope, it.bucket, it.state, it.agent_note, obs.agent, obs.agent_kind, lnNote(it), draft, it.id, it.learning_id, (it.evidence || {}).owner_quote]
    .concat(lnTouches(it, people).map(x => x.label)).filter(Boolean).join(" ").toLowerCase();
  return words.every(w => hay.includes(w));
}

// ---------------------------------------------------------------- historical records (read-only)
// A delta is retained history, never an actionable candidate. It is shown with its original ID and status and
// can only be read; the server-side guards stay authoritative.
const lnIsHistorical = it => !!it && it.bucket === "delta";
const LN_STATE_LABEL = { open: "Open", accepted: "Accepted", rejected: "Rejected", "in-progress": "In progress",
  implemented: "Implemented", "rejected-in-work": "Rejected in work" };
const lnStateLabel = v => LN_STATE_LABEL[v] || String(v || "open");
const lnHistoryId = it => String((it && (it.learning_id || it.id)) || "");
const lnSourceKey = s => `${String((s && s.run) || "")}\u0000${String((s && s.session) || "")}`;
const lnEventKey = e => String((e && (e.id || e.event_id)) || "");
const lnHistFresh = () => ({ items: [], count: null, next_cursor: null, complete: false, read_only: true,
  busy: false, error: "", failed: null, detail: null });

// Deterministic page merge: keeps earlier order, appends only identities not already displayed. A missing
// identity is never silently dropped; it is appended so nothing displayed is lost.
function lnMergeKeyed(existing, incoming, key) {
  const out = Array.isArray(existing) ? existing.slice() : [];
  const seen = new Set(out.map(key));
  for (const entry of Array.isArray(incoming) ? incoming : []) {
    const identity = key(entry);
    if (identity && seen.has(identity)) continue;
    if (identity) seen.add(identity);
    out.push(entry);
  }
  return out;
}
const lnMergeInventory = (a, b) => lnMergeKeyed(a, b, lnHistoryId);
const lnMergeSources = (a, b) => lnMergeKeyed(a, b, lnSourceKey);
const lnMergeHistory = (a, b) => lnMergeKeyed(a, b, lnEventKey);

// Which completeness/cursor values a page owns. The contract's page_collections is authoritative; an older
// or unwrapped page is read from the collections it actually carries.
function lnPageCollections(page) {
  if (Array.isArray(page && page.page_collections)) return page.page_collections.slice();
  const found = [];
  if (page && Object.prototype.hasOwnProperty.call(page, "sources")) found.push("evidence");
  if (page && Object.prototype.hasOwnProperty.call(page, "history")) found.push("history");
  return found;
}
function lnDetailFresh(id) {
  return { id, item: null, sources: [], history: [], evidence_status: "", evidence_complete: false,
    references_complete: false, history_complete: false, source_kind: "", original_transcripts_included: null,
    next_evidence_cursor: null, next_history_cursor: null, read_only: true, loading: true, busy: "", error: "",
    streams: {} };
}
// Merge one decoded page into the selected detail. Only the collections the page names are touched, so
// loading evidence necessarily leaves history (and vice versa) exactly as it was.
function lnApplyDetailPage(detail, page) {
  const collections = lnPageCollections(page);
  if (page && page.item) detail.item = page.item;
  for (const key of ["source_kind", "original_transcripts_included"]) if (page && page[key] !== undefined) detail[key] = page[key];
  if (page && page.read_only !== undefined) detail.read_only = !!page.read_only;
  if (collections.includes("evidence")) {
    detail.sources = lnMergeSources(detail.sources, page.sources);
    if (page.evidence_complete !== undefined) detail.evidence_complete = !!page.evidence_complete;
    if (page.references_complete !== undefined) detail.references_complete = !!page.references_complete;
    // Keep the page label for diagnostics only. Displayed availability is derived from the merged sources and
    // the current enumeration indicators, so an intermediate empty/partial page label cannot latch forever.
    if (page.evidence_status !== undefined) detail.evidence_status = String(page.evidence_status || "");
    detail.next_evidence_cursor = page.next_evidence_cursor || null;
  }
  if (collections.includes("history")) {
    detail.history = lnMergeHistory(detail.history, page.history);
    if (page.history_complete !== undefined) detail.history_complete = !!page.history_complete;
    detail.next_history_cursor = page.next_history_cursor || null;
  }
  detail.loading = false;
  detail.error = "";
  return detail;
}

// One archive page, byte-first: an oversized page returns JSON chunks addressed by content_cursor. Every
// chunk repeats the same collection selection, and the page is only decoded once the stream is complete.
// Automatic work is bounded per user action; the accumulated parts, original selection, next cursor and the
// already-fetched cursor set live in the selected detail so a Continue control resumes the same page. No
// partial JSON is parsed or merged and no collection cursor advances before the page is complete.
function lnReviewUrl(base, id, selection) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(selection || {})) if (value) query.set(key, String(value));
  const suffix = query.toString();
  return `${base}/${encodeURIComponent(id)}/review${suffix ? `?${suffix}` : ""}`;
}
function lnStreamFresh(kind, selection, candidate) {
  return { kind, candidate, workspace: lnWorkspace(), selection: { ...(selection || {}) },
    parts: [], next: null, seen: [], chunks: 0 };
}
const lnStreamError = message => ({ error: { message } });
function lnStreamText(stream) { return (stream.parts || []).join(""); }
function lnStreamPage(stream) {
  try { return { page: JSON.parse(lnStreamText(stream)) }; }
  catch (error) { return lnStreamError("The history page could not be decoded"); }
}
// Validate one chunk and advance the stream. A repeated or non-progressing cursor is an explicit protocol
// error; it is never followed in a loop and never silently starts the page again.
function lnAdvanceStream(stream, chunk, requestCursor) {
  if (!chunk || chunk.error) return chunk;
  if (typeof chunk.content !== "string" || chunk.encoding !== "json")
    return lnStreamError("The history page could not be reassembled");
  const next = chunk.complete ? null : chunk.next_content_cursor;
  if (!chunk.complete && (!next || next === requestCursor || stream.seen.includes(next)))
    return lnStreamError("The history page repeated a byte cursor instead of advancing");
  if (requestCursor !== null) stream.seen.push(requestCursor);
  stream.parts.push(chunk.content);
  stream.chunks += 1;
  stream.next = next;
  return chunk.complete ? null : "more";
}
// Load (or resume) one bounded page. Returns {page}, {stream} when the per-action budget ran out, or an error.
// Network/JSON failures throw; callers catch them and keep the saved stream so the same cursor can be retried.
async function lnReadReviewPage(detail, kind, selection) {
  const base = lnBase(), id = detail.id;
  detail.streams = detail.streams || {};
  let stream = detail.streams[kind];
  // A saved stream belongs to this workspace, candidate and exact selection; anything else is abandoned safely.
  if (stream && (stream.candidate !== id || stream.workspace !== lnWorkspace())) {
    delete detail.streams[kind];
    stream = null;
  }
  let budget = LN_CONTENT_CHUNKS_PER_ACTION;
  if (!stream) {
    const chosen = { ...(selection || {}) };
    const first = await api(lnReviewUrl(base, id, chosen));
    if (!first || first.error) return first;
    const stub = first.next_content_cursor && first.item === undefined && first.sources === undefined && first.history === undefined;
    if (typeof first.content !== "string" && !stub) return { page: first };
    stream = detail.streams[kind] = lnStreamFresh(kind, chosen, id);
    if (typeof first.content === "string") {
      const movement = lnAdvanceStream(stream, first, null);
      if (movement && movement.error) { delete detail.streams[kind]; return movement; }
      budget -= 1;
      if (!movement) { delete detail.streams[kind]; return lnStreamPage(stream); }
    } else {
      stream.next = first.next_content_cursor;
    }
  }
  while (budget > 0 && stream.next) {
    const cursor = stream.next;
    if (stream.seen.includes(cursor)) { delete detail.streams[kind]; return lnStreamError("The history page repeated a byte cursor instead of advancing"); }
    const chunk = await api(lnReviewUrl(base, id, { ...stream.selection, content_cursor: cursor }));
    if (!chunk || chunk.error) { stream.next = cursor; return chunk; }
    const movement = lnAdvanceStream(stream, chunk, cursor);
    budget -= 1;
    if (movement && movement.error) { delete detail.streams[kind]; return movement; }
    if (!movement) { delete detail.streams[kind]; return lnStreamPage(stream); }
  }
  if (!stream.next) { delete detail.streams[kind]; return lnStreamPage(stream); }
  return { stream };
}
// The failure a caller shows: a thrown transport/JSON error or a returned archive error body. Access loss is
// terminal for the already-loaded content; a transient failure keeps it for the next retry.
function lnFailure(input) {
  const body = input && input.error && typeof input.error === "object" ? input.error : input || {};
  const status = input && input.httpStatus;
  const code = (input && input.coreError && input.coreError.code) || body.code;
  const message = (typeof body.message === "string" && body.message) || (typeof input === "string" && input)
    || (typeof body.error === "string" && body.error) || "The request failed.";
  return { message, lost: [401, 403].includes(status)
    || ["forbidden", "policy_denied", "invalid_principal", "unauthenticated", "expired_credential"].includes(code) };
}
function lnDetailFailed(detail, input) {
  const failure = lnFailure(input);
  detail.loading = false;
  detail.busy = "";
  if (failure.lost) {
    detail.item = null;
    detail.sources = [];
    detail.history = [];
    detail.streams = {};
    detail.next_evidence_cursor = null;
    detail.next_history_cursor = null;
    detail.evidence_complete = false;
    detail.history_complete = false;
  }
  detail.error = failure.message;
}

const lnHistUrl = cursor => `${lnBase()}/history?limit=${LN_HISTORY_LIMIT}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;

// ---------------------------------------------------------------- fetch, status pane
async function lnFetch() {
  const workspace = lnWorkspace();
  if (!workspace) return null;
  const d = await api(lnBase());
  if (workspace !== lnWorkspace()) return null;
  if (!d || d.error || !Array.isArray(d.items)) return d;
  _ln.biz = lnWorkspace(); _ln.items = d.items; _ln.counts = d.counts || null;
  // a reverted decision hands its note back as the draft, unless the owner already typed a new one
  for (const it of d.items) if (lnOpenItem(it) && _ln.notes[it.id] === undefined) {
    const note = (it.reopened && it.reopened.note) || lnNote(it);
    if (note) _ln.notes[it.id] = note;
  }
  return d;
}

// ---------------------------------------------------------------- the page
async function openLearning() {
  if (_ln.biz !== lnWorkspace()) _ln = lnFresh();                   // another business's cards never flash
  const linked = lnLinkedHistoryId();
  if (linked) _ln.view = "history";
  if (_ln.view === "history") {
    // The archive never depends on the factual agenda route: its inventory is the only page it needs.
    lnPaint();
    if (!_ln.hist) _ln.hist = lnHistFresh();
    await lnHistoryLoad({ reset: !_ln.hist.items.length });
    if (!lnActive()) return;
    if (linked) await lnHistoryOpen(linked);
    else lnHistPaint();
    return;
  }
  lnPaint(!_ln.items.length);
  const d = await lnFetch();
  if (!lnActive()) return;                      // navigated away while the CLI ran
  if (d && d.error) { const v = $("#ctxview"); if (v) v.innerHTML = `<div class="empty bad">${esc(lnErr(d))}</div>`; return; }
  lnPaint();
}
function lnViewTabs() {
  return `<div class="lnviews" role="tablist" aria-label="Learning view">
    <button type="button" role="tab" class="lnview ${_ln.view !== "history" ? "on" : ""}" aria-selected="${_ln.view !== "history"}" data-view="review">Context learning</button>
    <button type="button" role="tab" class="lnview ${_ln.view === "history" ? "on" : ""}" aria-selected="${_ln.view === "history"}" data-view="history">Historical procedural records</button></div>`;
}
function lnWireViewTabs(v) {
  v.querySelectorAll("[data-view]").forEach(button => button.onclick = () => {
    const view = button.dataset.view === "history" ? "history" : "review";
    if (_ln.view === view) return;
    _ln.view = view;
    if (view === "history" && !_ln.hist) _ln.hist = lnHistFresh();
    lnPaint(!_ln.items.length && view === "review");
    if (view === "history") lnHistoryLoad({ reset: !(_ln.hist.items.length) }).then(() => { if (lnActive()) lnHistPaint(); });
    else if (!_ln.items.length) lnFetch().then(() => { if (lnActive() && _ln.view === "review") lnPaint(); });
  });
}
function lnPaint(loading) {
  const v = $("#ctxview"); if (!v) return;
  const history = _ln.view === "history";
  v.innerHTML = `${state.scope !== "global" && typeof ctxCrumbs === "function" ? ctxCrumbs("", "Learning") : ""}
    <div class="th"><h2>Learning</h2><span class="sp"></span>${lnViewTabs()}
      <input class="inp lnsearch" id="lnsearch" type="search" placeholder="${history ? "search historical records" : "search both sides"}" spellcheck="false" value="${esc(_ln.q)}"></div>
    ${history ? `<p class="lnfootnote">Retained procedural records. Read-only history: no decisions, edits, work transitions or automatic conversion; the original IDs and statuses are preserved.</p><div class="lnhist"></div>`
      : `<div class="lnsplit">
      <div class="lnq"><div class="sh">Waiting on you <span class="cnt"></span></div><div class="lnbody"></div></div>
      <div class="lndone"><div class="sh">Decided <span class="cnt"></span></div><div class="lnbody"></div></div>
    </div>`}`;
  if (typeof wireCrumbs === "function") wireCrumbs(v);
  lnWireViewTabs(v);
  const q = v.querySelector("#lnsearch");
  if (q) q.oninput = () => { _ln.q = q.value; if (history) lnHistPaint(); else lnPaintCols(loading); };
  if (history) lnHistPaint(); else lnPaintCols(loading);
}
// the two columns alone, so typing in the search box never repaints the box itself
function lnPaintCols(loading) {
  const v = $("#ctxview"); if (!v) return;
  const people = state.scope === "global" ? [] : (typeof _ctxData !== "undefined" && _ctxData && _ctxData.people) || [];
  const all = lnSplit(_ln.items), q = _ln.q.trim();
  const open = all.open.filter(it => lnMatch(it, q, people, _ln.notes[it.id]));
  const decided = all.decided.filter(it => lnMatch(it, q, people));
  const cards = list => `<div class="mkcards">${lnRelatedGroups(list).map(group => group.length > 1
    ? `<section class="lnrelatedgroup"><p class="lnpending">Related statements — review together</p>${group.map(it => lnCardHtml(it, people)).join('')}</section>`
    : lnCardHtml(group[0], people)).join('')}</div>`;
  const left = loading ? `<div class="empty">loading…</div>`
    : open.length ? cards(open)
    : q ? `<div class="empty">No open card matches.</div>` : `<div class="empty">Nothing waiting on you.</div>`;
  const right = decided.length ? cards(decided)
    : q ? `<div class="empty">No decided card matches.</div>` : `<div class="empty">Nothing decided yet.</div>`;
  const cnt = (col, n, total) => { const c = v.querySelector(`${col} .cnt`); if (c) c.textContent = q && n !== total ? `${n}/${total}` : String(total); };
  cnt(".lnq", open.length, all.open.length); cnt(".lndone", decided.length, all.decided.length);
  const lb = v.querySelector(".lnq .lnbody"), rb = v.querySelector(".lndone .lnbody");
  if (lb) lb.innerHTML = left;
  if (rb) rb.innerHTML = right;
  lnWire(v);
}

// ---------------------------------------------------------------- historical inventory & detail
async function lnHistoryLoad({ reset = false, cursor } = {}) {
  const workspace = lnWorkspace();
  if (!workspace) return;
  if (!_ln.hist) _ln.hist = lnHistFresh();
  const hist = _ln.hist;
  if (hist.busy) return;
  // Retries pass the exact failed selection; normal loads start or continue from the current inventory.
  const requested = cursor !== undefined ? cursor : (reset ? null : hist.next_cursor);
  hist.busy = true; hist.error = ""; hist.failed = null;
  let page;
  try { page = await api(lnHistUrl(requested)); }
  catch (error) { page = { error: error.message }; }
  hist.busy = false;
  if (workspace !== lnWorkspace() || _ln.hist !== hist) return;
  if (!page || page.error || !Array.isArray(page.items)) {
    const failure = lnFailure(page);
    if (failure.lost) {
      // Access loss withholds the inventory and any selected detail; the retry starts cleanly rather than
      // merging a later page into rows from an earlier authorization.
      hist.items = []; hist.count = null; hist.next_cursor = null; hist.complete = false;
      hist.detail = null; hist.failed = { reset: true, cursor: null };
    } else {
      hist.failed = { reset, cursor: requested };
    }
    hist.error = failure.message;
    lnHistPaint(); return;
  }
  hist.items = reset ? page.items : lnMergeInventory(hist.items, page.items);
  if (Number.isFinite(page.count)) hist.count = page.count;
  hist.next_cursor = page.next_cursor || null;
  hist.complete = !hist.next_cursor;
  hist.read_only = page.read_only !== false;
  hist.failed = null;
  lnHistPaint();
}
const lnDetailCurrent = (hist, detail, workspace) =>
  _ln.hist === hist && hist.detail === detail && workspace === lnWorkspace();
async function lnHistoryOpen(id) {
  const workspace = lnWorkspace();
  if (!_ln.hist) _ln.hist = lnHistFresh();
  const hist = _ln.hist;
  const detail = lnDetailFresh(id);
  hist.detail = detail;                                    // an inventory failure keeps its Retry underneath
  lnHistPaint();
  try {
    const result = await lnReadReviewPage(detail, "initial", {});
    if (!lnDetailCurrent(hist, detail, workspace)) return;             // abandoned: no late write
    if (!result || result.error) { lnDetailFailed(detail, result); lnHistPaint(); return; }
    if (result.stream) { detail.loading = false; lnHistPaint(); return; }
    lnApplyDetailPage(detail, result.page);
    lnHistPaint();
  } catch (error) {
    if (!lnDetailCurrent(hist, detail, workspace)) return;
    lnDetailFailed(detail, error);
    lnHistPaint();
  }
}
async function lnHistoryMore(kind) {
  const workspace = lnWorkspace();
  if (!_ln.hist || !_ln.hist.detail) return;
  const hist = _ln.hist, detail = _ln.hist.detail;
  if (detail.busy) return;
  let selection;
  if (detail.streams && detail.streams[kind]) {
    selection = detail.streams[kind].selection;                        // resume this exact page, not a new selection
  } else if (kind === "initial") {
    return lnHistoryOpen(detail.id);                                   // a fresh page after an unrecoverable failure
  } else {
    const cursor = kind === "evidence" ? detail.next_evidence_cursor : detail.next_history_cursor;
    if (!cursor) return;
    selection = kind === "evidence" ? { evidence_cursor: cursor } : { history_cursor: cursor };
  }
  detail.busy = kind;
  detail.error = "";
  lnHistPaint();
  try {
    const result = await lnReadReviewPage(detail, kind, selection);
    if (!lnDetailCurrent(hist, detail, workspace)) return;             // scope/detail changed: discard the late result
    detail.busy = "";
    if (!result || result.error) { lnDetailFailed(detail, result); lnHistPaint(); return; }
    if (result.stream) { lnHistPaint(); return; }                      // parts and next cursor retained
    lnApplyDetailPage(detail, result.page);
    lnHistPaint();
  } catch (error) {
    if (!lnDetailCurrent(hist, detail, workspace)) return;
    lnDetailFailed(detail, error);                                     // keep the saved cursor/parts for retry
    lnHistPaint();
  }
}
function lnHistoryBack() {
  if (!_ln.hist) return;
  _ln.hist.detail = null;                                  // retain an inventory failure and its retry
  lnHistPaint();
}
function lnHistoryRetry() {
  const hist = _ln.hist;
  if (!hist || !hist.failed) return;
  return lnHistoryLoad({ reset: !!hist.failed.reset, cursor: hist.failed.cursor });
}
const lnHistoryTotal = hist => Number.isFinite(hist.count) ? hist.count : hist.items.length;
function lnHistoryCompleteness(hist) {
  const shown = hist.items.length, total = lnHistoryTotal(hist);
  if (hist.next_cursor) return `showing ${shown} of ${total}`;
  return shown < total ? `${shown} of ${total} loaded; no further page was offered` : `all ${total} loaded`;
}
function lnHistCaption(hist) {
  const scope = state.scope === "global" ? "all projects" : "this project";
  return `${lnHistoryCompleteness(hist)} · ${scope}`;
}
function lnHistoricalStatus(it) {
  const bits = [];
  const decision = it.decision && typeof it.decision === "object" ? it.decision.state : it.decision;
  for (const value of [it.state, decision, it.work_state]) {
    if (!value) continue;
    const label = lnStateLabel(value);
    if (!bits.includes(label)) bits.push(label);
  }
  if (it.filed) bits.push("Filed");
  if (it.reconciled_into) bits.push("Reconciled");
  return bits.length ? bits.join(" · ") : "Open";
}
function lnHistRowHtml(it) {
  const id = lnHistoryId(it), state = lnHistoricalStatus(it), t = lnTag(it.tag);
  const truncated = it.text_truncated ? `<span class="lntrunc">truncated summary</span>` : "";
  return `<button type="button" class="lnhistrow" data-hist-id="${esc(id)}">
    <div class="lnhisthead"><span class="lntag" style="--tc:${esc(t.hex)}">${esc(t.label)}</span>
      <span class="lnreadonly">read-only</span>${it.filed ? `<span class="lnstatus">Filed</span>` : ""}${it.reconciled_into ? `<span class="lnstatus">Reconciled into ${esc(it.reconciled_into)}</span>` : ""}
      <span class="sp"></span><span class="ago">${esc(lnRel(it.created))}</span></div>
    <div class="lnhisttext">${esc(it.text || "")}</div>${truncated}
    <div class="lnhistmeta"><span>${esc(state)}</span><span class="lnhistid">${esc(id)}</span><span>${esc((LN_SCOPE_BY_KEY[it.scope] || {}).label || it.scope || "")}</span></div>
  </button>`;
}
function lnHistListHtml(hist) {
  const q = _ln.q.trim();
  const items = hist.items.filter(it => lnMatch(it, q, [], ""));
  const banner = hist.error ? `<div class="empty bad lnhistfailure">${esc(hist.error)}</div>` : "";
  const retry = hist.error && hist.failed
    ? `<button type="button" class="mka" data-hist-retry ${hist.busy ? "disabled" : ""}>Retry loading records</button>` : "";
  const foot = `<div class="lnhistfoot"><span>${hist.items.length ? esc(lnHistCaption(hist)) : ""}</span><span class="sp"></span>
    ${retry || (hist.next_cursor ? `<button type="button" class="mka" data-hist-more ${hist.busy ? "disabled" : ""}>Load more records</button>` : "")}</div>`;
  if (!hist.items.length) {
    if (hist.busy && !hist.error) return `<div class="empty">loading…</div>`;
    if (hist.error) return `${banner}${foot}`;
    return `<div class="empty">No retained procedural records in this scope.</div>`;
  }
  return `${banner}<div class="lnhistlist">${items.length ? items.map(lnHistRowHtml).join("") : `<div class="empty">No historical record matches.</div>`}</div>${foot}`;
}
function lnSnapshotSummary(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return "";
  const bits = [];
  for (const key of ["revision", "state", "work_state", "decision", "scope", "bucket", "tag"]) {
    if (snapshot[key] !== undefined && snapshot[key] !== null && snapshot[key] !== "") bits.push(`${key}: ${typeof snapshot[key] === "object" ? JSON.stringify(snapshot[key]) : snapshot[key]}`);
  }
  const note = snapshot.decision && typeof snapshot.decision === "object" ? snapshot.decision.note : snapshot.note;
  if (note) bits.push(`note: ${note}`);
  if (snapshot.text) bits.push(`text: ${String(snapshot.text).slice(0, 300)}${String(snapshot.text).length > 300 ? "…" : ""}`);
  if (snapshot.agent_note) bits.push(`agent note: ${String(snapshot.agent_note).slice(0, 300)}${String(snapshot.agent_note).length > 300 ? "…" : ""}`);
  return bits.join(" · ");
}
function lnHistEventHtml(event) {
  const id = lnEventKey(event) || "(no event id)";
  const before = lnSnapshotSummary(event.before_json), after = lnSnapshotSummary(event.after_json);
  return `<article class="lnhistevent" data-event="${esc(id)}">
    <div class="lnhisthead"><span class="lnhisteventname">${esc(event.event || "change")}</span><span class="sp"></span>
      <span class="ago">${esc(event.created_at || "")}</span></div>
    <div class="lnhistmeta"><span>${esc(event.actor_id || "")}</span><span class="lnhistid">${esc(id)}</span></div>
    ${before ? `<div class="lnhiststate"><span class="lnnotelabel">Before</span>${esc(before)}</div>` : ""}
    ${after ? `<div class="lnhiststate"><span class="lnnotelabel">After</span>${esc(after)}</div>` : ""}
    <details class="lnhistjson"><summary>Full recorded snapshots</summary>
      <pre>${esc(JSON.stringify({ before: event.before_json, after: event.after_json }, null, 2))}</pre></details>
  </article>`;
}
function lnStreamSaved(detail, kind) {
  const stream = detail.streams && detail.streams[kind];
  return stream ? lnStreamText(stream).length : 0;
}
function lnStreamPendingHtml(detail, kind, subject) {
  const stream = detail.streams && detail.streams[kind];
  if (!stream) return "";
  return `<p class="lncompleteness warn">${esc(subject)} is larger than one bounded read. ${lnStreamSaved(detail, kind).toLocaleString()} characters are saved; continue loading this same page before it is used. ${stream.chunks} chunk${stream.chunks === 1 ? "" : "s"} read so far.</p>`;
}
function lnStreamButtonHtml(detail, kind) {
  if (!(detail.streams && detail.streams[kind])) return "";
  return `<button type="button" class="mka" data-hist-more="${esc(kind)}" ${detail.busy ? "disabled" : ""}>${detail.error ? "Retry loading" : "Continue loading"}</button>`;
}
const lnMoreLabel = (detail, text) => detail.error ? "Retry loading" : text;
// The summary follows every accumulated source and the current traversal indicators, not a stored severity
// rank. An empty/partial intermediate page cannot assert a permanent record-level gap; real missing entries and
// unresolved reference metadata still warn.
function lnEvidenceView(detail) {
  const sources = detail.sources || [];
  const available = sources.filter(source => source.status === "available").length;
  const missing = sources.length - available;
  const complete = !!detail.evidence_complete;
  const referencesComplete = detail.references_complete !== false;
  let status = "", text = "";
  if (missing) {
    status = available ? "partial" : "missing";
    text = available
      ? `Saved evidence is partial: ${missing} of ${sources.length} listed source${sources.length === 1 ? "" : "s"} cannot be shown.`
      : `Saved evidence is missing for ${missing} listed source${missing === 1 ? "" : "s"}; no frozen source content is available.`;
  } else if (available) {
    status = "available";
    if (!complete) text = "All sources loaded so far have saved content; evidence enumeration is not complete.";
    else if (!referencesComplete) text = "All listed sources have saved content, but some reference metadata is unresolved.";
    else text = "All listed sources have saved content.";
  } else if (!complete) {
    text = "No saved sources have been loaded yet; evidence enumeration is not complete.";
  } else if (!referencesComplete) {
    status = "partial";
    text = "No frozen source references are recorded, but reference metadata is unresolved.";
  }
  return { available, missing, status, text };
}
function lnHistEvidenceHtml(detail) {
  const sources = detail.sources || [];
  const view = lnEvidenceView(detail);
  const pending = !!(detail.streams && detail.streams.evidence);
  return `<section class="lnhistsection"><div class="sh">Saved evidence <span class="cnt">${view.available}/${sources.length}</span></div>
    ${view.text ? `<p class="lncompleteness ${view.status === "available" ? "ok" : "warn"}">${esc(view.text)}</p>` : ""}
    ${detail.original_transcripts_included === false ? `<p class="lnfootnote">Original transcripts are not included; these are frozen extraction snapshots.</p>` : ""}
    ${lnStreamPendingHtml(detail, "evidence", "This evidence page")}
    ${sources.length ? `<div class="lnsources">${sources.map(source => `<article class="lnsource" data-run="${esc(source.run || "")}" data-session="${esc(source.session || "")}">
      <div class="lnhisthead"><span class="lnsourcestatus ${source.status === "available" ? "ok" : "missing"}">${esc(source.status === "available" ? "available" : "missing")}</span><span class="sp"></span>
        <span class="lnhistid">run ${esc(source.run || "unknown")} · session ${esc(source.session || "unknown")}</span></div>
      ${source.core && source.core.markdown ? `<pre>${esc(source.core.markdown)}</pre>` : `<p class="lnmissing">No saved core content for this run/session pair.</p>`}
    </article>`).join("")}</div>` : detail.evidence_complete ? `<div class="empty">No frozen source references are recorded.</div>` : ""}
    ${!pending && !detail.evidence_complete ? `<p class="lncompleteness warn">More source references may exist; evidence traversal has not reached its end.</p>` : ""}
    ${detail.references_complete === false ? `<p class="lncompleteness warn">Some reference metadata is unresolved, so this evidence is not a complete reference set.</p>` : ""}
    <div class="lnhistfoot"><span class="sp"></span>${lnStreamButtonHtml(detail, "evidence")
      || (detail.next_evidence_cursor ? `<button type="button" class="mka" data-hist-more="evidence" ${detail.busy ? "disabled" : ""}>${lnMoreLabel(detail, "Load more evidence")}</button>` : "")}</div>
  </section>`;
}
function lnHistHistoryHtml(detail) {
  const events = detail.history || [];
  const pending = !!(detail.streams && detail.streams.history);
  return `<section class="lnhistsection"><div class="sh">History <span class="cnt">${events.length}</span></div>
    ${events.length ? `<div class="lnhistevents">${events.map(lnHistEventHtml).join("")}</div>` : detail.history_complete && !pending ? `<div class="empty">No recorded history events.</div>` : ""}
    ${lnStreamPendingHtml(detail, "history", "This history page")}
    ${!pending && !detail.history_complete ? `<p class="lncompleteness warn">More history events may exist; this is not the complete history.</p>` : ""}
    <div class="lnhistfoot"><span class="sp"></span>${lnStreamButtonHtml(detail, "history")
      || (detail.next_history_cursor ? `<button type="button" class="mka" data-hist-more="history" ${detail.busy ? "disabled" : ""}>${lnMoreLabel(detail, "Load more history")}</button>` : "")}</div>
  </section>`;
}
function lnHistDetailHtml(detail) {
  const item = detail.item || {};
  const head = `<div class="lnhistdetailhead"><button type="button" class="mka" data-hist-back>← Historical records</button><span class="sp"></span>
    <span class="lnreadonly">read-only</span></div>`;
  const failure = detail.error ? `<div class="empty bad lnhistfailure">${esc(detail.error)}</div>` : "";
  if (detail.streams && detail.streams.initial) return `${head}${failure}
    <div class="lnhistdetail" data-id="${esc(detail.id)}">${lnStreamPendingHtml(detail, "initial", "This record page")}
      <div class="lnhistfoot"><span class="sp"></span>${lnStreamButtonHtml(detail, "initial")}</div></div>`;
  if (!detail.item) {
    if (detail.loading) return `${head}<div class="empty">loading…</div>`;
    if (detail.error) return `${head}${failure}<div class="lnhistfoot"><span class="sp"></span>
      <button type="button" class="mka" data-hist-more="initial" ${detail.busy ? "disabled" : ""}>Retry</button></div>`;
    return `${head}<div class="empty">No historical record is loaded.</div>`;
  }
  const t = lnTag(item.tag);
  const note = lnNote(item);
  return `${head}${failure}
    <article class="lnhistdetail" data-id="${esc(detail.id)}">
      <div class="lnhisthead"><span class="lntag" style="--tc:${esc(t.hex)}">${esc(t.label)}</span>
        <span class="lnreadonly">historical procedural record</span>
        ${item.filed ? `<span class="lnstatus">Filed</span>` : ""}${item.reconciled_into ? `<span class="lnstatus">Reconciled into ${esc(item.reconciled_into)}</span>` : ""}
        <span class="sp"></span><span class="ago">${esc(lnRel(item.created))}</span></div>
      <div class="mkt">${esc(item.text || "")}</div>
      ${item.text_truncated ? `<p class="lncompleteness warn">The summary text was truncated; the stored record is longer.</p>` : ""}
      <div class="lnhistmeta"><span>${esc(lnHistoricalStatus(item))}</span><span>${esc((LN_SCOPE_BY_KEY[item.scope] || {}).label || item.scope || "")}</span>
        <span class="lnhistid">${esc(detail.id)}</span>${item.revision !== undefined ? `<span>revision ${esc(item.revision)}</span>` : ""}</div>
      ${note ? `<div class="lnwhy"><span class="lnnotelabel">Owner note</span>${esc(note)}</div>` : ""}
      ${item.agent_note ? `<div class="lnagentnote"><span class="lnnotelabel">Agent note</span>${esc(item.agent_note)}</div>` : ""}
      ${Array.isArray(item.targets) && item.targets.length ? `<div class="lnhiststate"><span class="lnnotelabel">Commit targets</span>${esc(JSON.stringify(item.targets, null, 2))}</div>` : ""}
      ${item.outcome && Object.keys(item.outcome).length ? `<div class="lnhiststate"><span class="lnnotelabel">Outcome</span>${esc(JSON.stringify(item.outcome, null, 2))}</div>` : ""}
      ${lnHistEvidenceHtml(detail)}
      ${lnHistHistoryHtml(detail)}
    </article>`;
}
function lnHistPaint() {
  const host = document.querySelector(".lnhist"); if (!host) return;
  const hist = _ln.hist;
  if (!hist) { host.innerHTML = `<div class="empty">loading…</div>`; return; }
  host.innerHTML = hist.detail ? lnHistDetailHtml(hist.detail) : lnHistListHtml(hist);
  const back = host.querySelector("[data-hist-back]");
  if (back) back.onclick = lnHistoryBack;
  host.querySelectorAll("[data-hist-id]").forEach(row => row.onclick = () => lnHistoryOpen(row.dataset.histId));
  host.querySelectorAll("[data-hist-more]").forEach(button => button.onclick = () => {
    if (button.dataset.histMore) return lnHistoryMore(button.dataset.histMore);
    return lnHistoryLoad();
  });
  host.querySelectorAll("[data-hist-retry]").forEach(button => button.onclick = lnHistoryRetry);
}

// one card, open or decided: one head line (state ball on decided, type, scope icon(s), bucket icon, people
// chips, who, votes, age), the text, the
// evidence, the passes, then the notes box (closed until the card takes focus) and Reject / Approve —
// decided cards carry their note instead, and a Revert that puts them back on the left
function lnRelatedGroups(items) {
  const byId = new Map(items.map(it => [it.id, it])), links = new Map(items.map(it => [it.id, new Set()]));
  for (const it of items) {
    const related = [...(it.contradicts || []), ...(it.reconciliations || []).filter(r => !r.kept_both).map(r => r.earlier?.id)];
    for (const id of related) if (byId.has(id) && id !== it.id) { links.get(it.id).add(id); links.get(id).add(it.id); }
  }
  const seen = new Set(), groups = [];
  for (const it of items) {
    if (seen.has(it.id)) continue;
    const stack = [it.id], group = []; seen.add(it.id);
    while (stack.length) {
      const id = stack.pop(); group.push(byId.get(id));
      for (const other of links.get(id)) if (!seen.has(other)) { seen.add(other); stack.push(other); }
    }
    groups.push(group);
  }
  return groups;
}

function lnReconciliationHtml(it) {
  const labels = {answered: 'Question answered', updated: 'Later update', duplicate: 'Duplicate combined'};
  return (it.reconciliations || []).map(r => {
    const old = r.earlier || {};
    const label = r.kept_both ? 'Kept separately' : r.automatic ? (labels[r.kind] || 'Related evidence') : 'Proposed update — review both';
    return `<details class="lnreconciliation"><summary>${esc(label)}</summary><p><strong>Earlier:</strong> ${esc(old.text || '')}</p>
      <p>${esc(lnSourceAge(old.evidence?.source_at || old.evidence?.source?.at))}</p>
      <blockquote>${esc(old.evidence?.owner_quote || '')}</blockquote><p>${esc(r.reason || '')}</p>
      ${r.automatic && !r.kept_both ? '<button type="button" class="mka" data-act="keep-both">Keep both separately</button>' : ''}</details>`;
  }).join('');
}

async function lnKeepBoth(it) {
  const workspace = lnWorkspace();
  try {
    const r = await post(`${lnBase()}/${encodeURIComponent(it.id)}/keep-both`, {expected_revision: it.revision});
    if (workspace !== lnWorkspace()) return;
    if (r.error) { toast(r.error.message || 'Could not restore earlier learning'); return; }
    await lnRefetch();
  } catch (error) { if (workspace === lnWorkspace()) toast(error.message); }
}

// A delta that an older server still returns in the collection remains only history: an explicit label, its
// original status and a link to the read-only detail. No scope, note, decision, revert, keep-both or intake control.
function lnLegacyCardHtml(it, t) {
  const bk = LN_BUCKETS.delta;
  const quote = (it.evidence || {}).owner_quote ? `<div class="lnquote">“${esc(it.evidence.owner_quote)}”</div>` : "";
  return `<div class="mkcard lncard done lnlegacy" data-id="${esc(it.id)}" data-state="${esc(it.state || "open")}">
    <div class="mkhead"><span class="lntag lnbucket" data-bucket="delta" style="--tc:${esc(bk.hex)}">${esc(bk.label)}</span>
      <span class="lntag" style="--tc:${esc(t.hex)}">${esc(t.label)}</span><span class="lnreadonly">historical · read-only</span>
      <span class="sp"></span><span class="ago">${esc(lnRel(it.created))}</span></div>
    <div class="mkt">${esc(it.text || "")}</div>${quote}
    ${it.agent_note ? `<div class="lnagentnote"><span class="lnnotelabel">Agent note</span>${esc(it.agent_note)}</div>` : ""}
    <div class="mkfoot"><span class="lnpending">${esc(lnHistoricalStatus(it))}</span><div class="mkacts">
      <button type="button" class="mka" data-act="history">Open history</button></div></div></div>`;
}

function lnCardHtml(item, people) {
  const it = item || {}, t = lnTag(it.tag), obs = it.observed || {}, ev = it.evidence || {};
  if (lnIsHistorical(it)) return lnLegacyCardHtml(it, t);
  const locked = lnScopeLocked(it);
  const pick = LN_SCOPES.map(([k, label, ico]) => {
    const off = (locked && k !== (it.origin_scope === "global" ? "global" : "biz")) ||
      (it.origin_scope === "global" && (k === "biz" || (["agent", "agent_kind"].includes(k) && !it.observed?.agent))) || !!it.intake_pending;
    const reason = locked ? 'Context stays in its originating workspace' : k === 'biz' ? 'Global sources cannot select a project' : 'No observed transcript agent';
    return `<button class="lnscope ${it.scope === k ? "on" : ""}" type="button" data-scope="${esc(k)}" ${off ? `disabled title="${esc(reason)}"` : `title="${esc(label)}"`}>${ico}</button>`;
  }).join("");
  const votes = it.votes != null && it.passes ? `<span class="chip" title="passes that agreed">${esc(it.votes)}/${esc(it.passes)}</span>` : "";
  const touches = lnTouches(it, people).map(x => `<span class="chip ${esc(x.kind)}" ${x.title ? `title="${esc(x.title)}"` : ""}>${esc(x.label)}</span>`).join("");
  const scopeIco = `<span class="lnscopeico" title="${esc((LN_SCOPE_BY_KEY[it.scope] || {}).label || it.scope || "")}">${(LN_SCOPE_BY_KEY[it.scope] || {}).ico || ""}</span>`;
  const bk = LN_BUCKETS[it.bucket];
  const bucketIco = bk ? `<span class="lntag lnbucket" data-bucket="${esc(it.bucket)}" style="--tc:${esc(bk.hex)}">${esc(bk.label)}</span>` : "";
  const source = ev.source;
  const stamp = source ? [String(source.kind).replaceAll('_', ' '), source.actor_name, lnSourceAge(source.at)].filter(Boolean).join(' · ') : [(ev.turns || []).join(" "), lnWho(obs.agent), String(ev.session || "").slice(0, 8)].filter(Boolean).join(" · ");
  const conflict = it.contradicts?.length ? `<p class="lnpending">Possible conflict with ${it.contradicts.length} related learning${it.contradicts.length === 1 ? '' : 's'}.</p>` : '';
  const sourceInfo = `${lnReconciliationHtml(it)}${conflict}`;
  const sourceButton = `<button type="button" class="mka" data-act="source">Source${it.contradicts?.length ? ' & conflicts' : ''}</button>`;
  const quote = ev.owner_quote ? `<div class="lnquote">“${esc(ev.owner_quote)}”<span class="src">${esc(stamp)}</span></div>` : "";
  const more = Array.isArray(it.evidence_more) ? it.evidence_more.filter(e => e && e.owner_quote) : [];
  const moreEv = more.length ? `<details class="mkchain"><summary title="Seen ${more.length} more time${more.length === 1 ? '' : 's'}">+${more.length} repeat${more.length === 1 ? "" : "s"}</summary>${more.map(e =>
    `<div class="lnquote">“${esc(e.owner_quote)}”<span class="src">${esc([(e.turns || []).join(" "), String(e.session || "").slice(0, 8)].filter(Boolean).join(" · "))}</span></div>`).join("")}</details>` : "";
  const vs = Array.isArray(it.variants) ? it.variants : [];
  const variants = vs.length ? `<details class="mkchain"><summary>${esc(it.passes || vs.length)} passes · ${vs.length} wording${vs.length === 1 ? "" : "s"}</summary>${vs.map(vr =>
    `<div class="lnvar"><div class="vt">${esc(vr.text || "")}</div><div class="vm">${esc(vr.scope || "")} · ${esc(vr.confidence != null ? vr.confidence : "?")}</div></div>`).join("")}</details>` : "";
  if (!lnOpenItem(it)) {
    const st = String(it.state), note = lnNote(it);
    const pill = {"in-progress": "In progress", "rejected-in-work": "Rejected in work"}[st];
    const pending = it.origin_scope === "global" && it.bucket === "context" && st === "accepted" ? "Accepted Global context" : it.intake_pending ? "Context handoff interrupted" : ({accepted: "awaiting selection", rejected: "rejected", "in-progress": "work in progress", "rejected-in-work": "work stopped"}[st] || st);
    return `<div class="mkcard lncard done" data-id="${esc(it.id)}" data-state="${esc(st)}" style="--sc:${esc(LN_STATE_HEX[st] || "var(--fg-3)")}">
      <div class="mkhead">${bucketIco}<span class="lnball" title="${esc(st)}"></span>${pill ? `<span class="lnstate">${esc(pill)}</span>` : ""}<span class="lntag" style="--tc:${esc(t.hex)}">${esc(t.label)}</span>${scopeIco}${touches}<span class="sp"></span><span class="ago">${esc(lnRel((it.decision || {}).created || it.created))}</span></div>
      <div class="mkt">${esc(it.text || "")}</div>
      ${note ? `<div class="lnwhy"><span class="lnnotelabel">Owner note</span>${esc(note)}</div>` : ""}
      ${quote}${sourceInfo}<div class="ln-evidence-tools">${sourceButton}</div>${it.agent_note ? `<div class="lnagentnote"><span class="lnnotelabel">Agent note</span>${esc(it.agent_note)}</div>` : ""}
      ${!it.intake_pending ? `<details class="lnowneredit"><summary>Edit owner note</summary><textarea class="lnnote" spellcheck="false">${esc(_ln.notes[it.id] ?? note)}</textarea><button class="mka" type="button" data-act="save-note">Save owner note</button></details>` : ""}
      <div class="mkfoot"><span class="lnpending">${esc(pending)}</span><div class="mkacts">
        ${it.intake_pending ? `<button class="mka" type="button" data-act="resume-intake">Resume handoff</button>` : ""}
        ${lnCanRevert(it) ? `<button class="mka revert" type="button" data-act="reopen" title="Return to owner review; keep notes and work history">Revert</button>` : ""}
      </div></div></div>`;
  }
  const notes = _ln.notes[it.id] || "";
  const noteBox = `<textarea class="lnnote" placeholder="Note (required to reject)" spellcheck="false" ${_ln.noteOpen[it.id] || notes ? "" : "hidden"}>${esc(notes)}</textarea>`;
  return `<div class="mkcard lncard" tabindex="0" data-id="${esc(it.id)}" data-tag="${esc(it.tag || "")}">
    <div class="mkhead">${bucketIco}<span class="lntag" style="--tc:${esc(t.hex)}">${esc(t.label)}</span><span class="lnpick">${pick}</span>${touches}
      <span class="who">${esc(lnWho(obs.agent))}</span><span class="sp"></span>${votes}<span class="ago">${esc(lnRel(it.created))}</span></div>
    <div class="mkt">${esc(it.text || "")}</div>
    ${quote}${sourceInfo}<div class="ln-evidence-tools">${sourceButton}${moreEv}${variants}</div>${noteBox}
    ${it.agent_note ? `<div class="lnagentnote"><span class="lnnotelabel">Agent note</span>${esc(it.agent_note)}</div>` : ""}
    <div class="mkfoot"><div class="mklinks"></div><div class="mkacts">
      ${it.intake_pending ? `<span class="lnpending">Context handoff interrupted</span><button class="mka" type="button" data-act="resume-intake">Resume handoff</button>` : `<button class="mka reject" type="button" data-act="reject">Reject</button>
      <button class="mka approve" type="button" data-act="approve">Approve</button>`}
    </div></div></div>`;
}

// ---------------------------------------------------------------- wiring
const lnFind = id => _ln.items.find(x => x.id === id);
// focusing a card opens its notes box and puts the caret there; nothing is re-rendered, so focus holds
function lnOpenNote(card) {
  const ta = card.querySelector(".lnnote"); if (!ta) return;
  _ln.noteOpen[card.dataset.id] = 1;
  ta.hidden = false;
  if (document.activeElement !== ta) ta.focus();
}
function lnWire(root) {
  root.querySelectorAll(".lncard").forEach(card => { if (_ln.busy[card.dataset.id]) lnReviewBusy(card.dataset.id, true); });
  root.querySelectorAll(".lncard:not(.done)").forEach(card => {
    card.onfocus = () => lnOpenNote(card);
    card.onclick = e => { if (!e.target.closest || !e.target.closest("button, a, summary, details")) lnOpenNote(card); };
  });
  root.querySelectorAll(".lncard .lnnote").forEach(ta => {
    ta.oninput = () => { const c = ta.closest(".lncard"); if (c) { _ln.notes[c.dataset.id] = ta.value; lnDraftsSave(); } };
  });
  root.querySelectorAll(".lnscope").forEach(b => b.onclick = e => {
    e.stopPropagation();
    const card = b.closest(".lncard"), it = card && lnFind(card.dataset.id);
    if (it && !b.disabled) lnSetScope(it, b.dataset.scope);
  });
  root.querySelectorAll(".lncard .mka").forEach(b => b.onclick = e => {
    e.stopPropagation();
    const card = b.closest(".lncard"), it = card && lnFind(card.dataset.id);
    if (!it) return;
    ({ "keep-both": lnKeepBoth, source: lnShowSource, history: lnOpenHistory, approve: lnApprove, reject: lnReject, reopen: lnReopen, "save-note": lnSaveOwnerNote, "resume-intake": lnResumeIntake }[b.dataset.act] || (() => {}))(it);
  });
}
// Keep drafts and the current card until the service confirms the durable change.
function lnDecide(it, st, note) {
  it.state = st;
  it.decision = { state: st, note: note || "", actor: "human:owner", created: new Date().toISOString() };
  delete it.reopened;
  delete _ln.notes[it.id]; delete _ln.noteOpen[it.id]; lnDraftsSave();
  lnPaintCols();
}
function lnUndecide(it) {
  const note = lnNote(it);
  it.reopened = { state: "open", note, created: new Date().toISOString() };
  delete it.state; delete it.decision;
  if (note && !_ln.notes[it.id]) { _ln.notes[it.id] = note; lnDraftsSave(); }
  lnPaintCols();
}
async function lnRefetch() { const d = await lnFetch(); if (d && !d.error && lnActive()) lnPaintCols(); }
// Context decisions share an index: commit one review at a time, without blocking card interaction.
let lnReviewQueue = Promise.resolve();
function lnReviewBusy(id, busy, label = 'Saving…') {
  const card = document.querySelector(`.lncard[data-id="${id}"]`);
  if (!card) return;
  card.setAttribute('aria-busy', String(busy));
  card.querySelectorAll('button, textarea').forEach(el => { el.disabled = busy; });
  let status = card.querySelector('.lnsaving');
  if (busy && !status) { status = document.createElement('span'); status.className = 'lnsaving'; status.setAttribute('role', 'status'); card.querySelector('.mkfoot')?.prepend(status); }
  if (status) { if (busy) status.textContent = label; else status.remove(); }
}
async function lnSend(it, st, note, apply, label) {
  const id = it.id, biz = lnWorkspace(), base = lnBase(), model = _ln;
  if (model.busy[id]) return;
  model.busy[id] = 1;
  lnReviewBusy(id, true);
  const previous = lnReviewQueue;
  let release; lnReviewQueue = new Promise(resolve => { release = resolve; });
  await previous;
  try {
    // A queued click belongs to the workspace where it was made.
    const request = {state: st, ...(note !== undefined ? {note} : {}), ...(it.revision ? {expected_revision: it.revision} : {}), idempotency_key: newRequestId()};
    let r, observed;
    try { r = await post(`${base}/${id}/state`, request); } catch (e) { r = {error: e.message}; }
    if (!r || r.error) {
      // A transport failure can happen after commit. Read this item, never resubmit blindly.
      try {
        const saved = await api(`${base}/${id}`), item = saved?.item || saved?.data?.item;
        observed = item;
        if (item && (item.decision?.state || item.state) === st && (note === undefined || lnNote(item) === note)) r = {item};
      } catch (_) {}
    }
    if (lnWorkspace() !== biz || _ln !== model) return;
    if (!r || r.error) {
      if (observed) Object.assign(lnFind(id) || it, observed);
      notify({text: `${label} could not be confirmed`, sub: lnErr(r), color: 'var(--danger)'});
      return;
    }
    delete model.notes[id]; delete model.noteOpen[id]; lnDraftsSave();
    if (r.item) {
      const current = lnFind(id) || it;
      Object.assign(current, r.item);
      if (st === "open" && lnNote(current)) { model.notes[id] = lnNote(current); lnDraftsSave(); }
      if (r.item.filing?.upload_id) model.items = model.items.filter(item => item.id !== id);
    } else { apply(); }
    toast(label.toLowerCase());
  } finally {
    delete model.busy[id]; release();
    if (_ln === model && lnWorkspace() === biz) { lnPaintCols(); }
  }
}
function lnApprove(it) {
  if (!lnOpenItem(it)) return;
  const note = (_ln.notes[it.id] || "").trim();
  return lnSend(it, "accepted", note, () => lnDecide(it, "accepted", note), "Accepted");
}
// Reject takes its reason from the notes box; without one it opens the box instead of a dialog
function lnReject(it) {
  if (!lnOpenItem(it)) return;
  const note = (_ln.notes[it.id] || "").trim();
  if (!note) {
    const card = document.querySelector(`.lncard[data-id="${it.id}"]`);
    if (card) lnOpenNote(card);
    notify({ text: "Reject needs a note", sub: "write why in the notes box; that reason is what the record keeps", color: "var(--danger)" });
    return;
  }
  return lnSend(it, "rejected", note, () => lnDecide(it, "rejected", note), "Rejected");
}
// Revert: back to the left as if it never went right; the old note becomes the draft
function lnReopen(it) {
  if (!lnCanRevert(it)) return;
  return lnSend(it, "open", undefined, () => lnUndecide(it), "Reverted");
}
function lnSaveOwnerNote(it) {
  const note = _ln.notes[it.id] ?? lnNote(it);
  return lnSend(it, (it.decision || {}).state || it.state, note, () => {
    it.decision = {...it.decision, note}; delete _ln.notes[it.id]; lnDraftsSave();
  }, "Owner note saved");
}
async function lnResumeIntake(it) {
  const biz = lnWorkspace(), base = lnBase(), model = _ln;
  if (_ln.busy[it.id]) return;
  _ln.busy[it.id] = 1;
  let r; try { r = await post(`${base}/${it.id}/intake/retry`, {}); } catch (e) { r = null; }
  delete model.busy[it.id];
  if (lnWorkspace() !== biz || _ln !== model) return;
  if (!r || r.error) notify({text: "Context handoff failed", sub: lnErr(r), color: "var(--danger)"});
  else { delete _ln.notes[it.id]; lnDraftsSave(); toast("context sent to filer"); }
  return lnRefetch();
}
// the scope picker saves on the click; there is no Save button anywhere on this page
async function lnSetScope(it, scope) {
  if (!scope || it.scope === scope || _ln.busy[it.id]) return;
  const model = _ln, workspace = lnWorkspace(), base = lnBase();
  const was = it.scope;
  _ln.busy[it.id] = 1;
  it.scope = scope; lnPaintCols();
  let r; try { r = await post(`${base}/${it.id}/edit`, { scope }); } catch (e) { r = null; }
  delete model.busy[it.id];
  if (_ln !== model || workspace !== lnWorkspace()) return;
  if (!r || r.error) { it.scope = was; notify({ text: "Scope change failed", sub: lnErr(r), color: "var(--danger)" }); return lnRefetch(); }
  if (r.item) Object.assign(it, r.item);
  else await lnRefetch();
  lnPaintCols();
  toast(`scope → ${scope}`);
}

// Open a retained record's read-only history from the review collection (an older server may still list deltas here).
async function lnOpenHistory(it) {
  _ln.view = "history";
  if (!_ln.hist) _ln.hist = lnHistFresh();
  lnPaint();
  await Promise.all([lnHistoryLoad({ reset: !_ln.hist.items.length }), lnHistoryOpen(it.id)]);
}

// Ordinary factual source view. It reads the candidate's own saved evidence for Context learning; the
// historical view never uses this session-keyed `cores` projection as archive evidence.
async function lnShowSource(it) {
  const workspace = lnWorkspace();
  let result;
  try { result = await api(`${lnBase()}/${encodeURIComponent(it.id)}`); }
  catch (error) { if (workspace === lnWorkspace()) notify({text: 'Source unavailable', sub: error.message, color: 'var(--danger)'}); return; }
  if (workspace !== lnWorkspace()) return;
  const data = result?.data || result;
  if (!data?.cores) { notify({text: 'Source unavailable', color: 'var(--danger)'}); return; }
  const dialog = document.createElement('dialog');
  dialog.style.cssText = 'max-width:850px;width:85vw;max-height:80vh;overflow:auto';
  const title = document.createElement('h2'); title.textContent = 'Learning source';
  const close = document.createElement('button'); close.textContent = 'Close'; close.onclick = () => dialog.close();
  dialog.append(title, close);
  for (const core of Object.values(data.cores)) {
    const pre = document.createElement('pre'); pre.style.whiteSpace = 'pre-wrap'; pre.textContent = core.markdown || '';
    dialog.append(pre);
  }
  if (!Object.keys(data.cores).length) { const p=document.createElement('p'); p.textContent='Historical source snapshot is unavailable. The saved quotation remains on the learning.'; dialog.append(p); }
  for (const id of (data.item?.contradicts || []).slice(0, 10)) {
    const section = document.createElement('p'); section.textContent = 'Loading conflicting learning…'; dialog.append(section);
    api(`${lnBase()}/${encodeURIComponent(id)}`).then(result => {
      const item = result?.data?.item || result?.item;
      section.textContent = item ? `Possible conflict (${item.state}): ${item.text}` : 'Related learning unavailable.';
    }).catch(() => { section.textContent = 'Related learning unavailable.'; });
  }
  dialog.addEventListener('close', () => dialog.remove()); document.body.append(dialog); dialog.showModal();
}
