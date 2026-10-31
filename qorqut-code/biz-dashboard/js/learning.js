// ---------------------------------------------------------------- learning
// The Learning page of the Context tab: the owner triages the candidates `bizlearn` pulled out of the
// agents' transcripts. Three processes, not one: extraction (the CLI, done), review (this file), application
// (not built — the right column says so). Left column is what still waits on you, right column is what you
// already decided and nobody has applied yet. Cards borrow the marketing Queue's markup so the two review
// surfaces read the same; nothing here touches `_mk`. Depends on core.js and marketing.js's `mkRel`.

// ---------------------------------------------------------------- palette & icons
// Type is a colour (five from the shared COLORS palette, so a learning never reads as a department glyph),
// scope is an icon. Lucide strokes at the same weight as MK_ICO.
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
  ["biz", "This business", lnSvg('<path d="m2 7 4.41-4.41A2 2 0 0 1 7.83 2h8.34a2 2 0 0 1 1.42.59L22 7"/><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><path d="M15 22v-4a2 2 0 0 0-2-2h-2a2 2 0 0 0-2 2v4"/><path d="M2 7h20"/>')],
  ["agent", "This agent", lnSvg(AICONS.bot)],
  ["agent_kind", "This agent kind", lnSvg('<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m6.08 12.9-3.49 1.59a1 1 0 0 0 0 1.81l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83l-3.51-1.6"/>')],
  ["global", "All businesses", lnSvg(AICONS.globe)],
];
const LN_SCOPE_BY_KEY = Object.fromEntries(LN_SCOPES.map(([k, label, ico]) => [k, { key: k, label, ico }]));
// bucket is an icon too, the same two glyphs as the main rail: Context for a fact about the world, Code for a
// change to how the system works
const LN_BUCKETS = {
  context: { label: "context: a fact about the world (product, market, people); lands in the context/ tree, so scope is always this business", ico: lnSvg('<path d="m16 6 4 14"/><path d="M12 6v14"/><path d="M8 8v12"/><path d="M4 4v16"/>') },
  delta: { label: "delta: a change to how agents work; lands in CLAUDE.md, an agent's rules or a plugin's SKILL.md", ico: lnSvg(AICONS.code) },
};
// the decided card's ball wears the same two colours as the notes gutter balls (done / blocked)
const LN_STATE_HEX = { accepted: "#30D158", rejected: "#FF453A" };

// notes: draft notes per item, kept in localStorage per business until the decision goes through, so a
// repaint, a reload or a revert never loses them; noteOpen: cards whose notes box is open; busy: ids with a
// write in flight; q: the search box
let _ln = { biz: null, items: [], counts: null, notes: {}, noteOpen: {}, busy: {}, q: "" };
const lnDraftKey = () => `lnnotes:${state.biz}`;
function lnDraftsLoad() { try { return JSON.parse(ls(lnDraftKey()) || "{}") || {}; } catch (e) { return {}; } }
function lnDraftsSave() { ls(lnDraftKey(), JSON.stringify(_ln.notes)); }
function lnFresh() { return { biz: state.biz, items: [], counts: null, notes: lnDraftsLoad(), noteOpen: {}, busy: {}, q: "" }; }

// ---------------------------------------------------------------- pure helpers
const lnOpenItem = it => !!it && !it.state;
const lnCreated = it => Date.parse((it && it.created) || "") || 0;
const lnDecided = it => Date.parse((it && it.decision && it.decision.created) || (it && it.created) || "") || 0;
const lnNote = it => (it && it.decision && it.decision.note) || "";
const lnErr = r => (r && r.error) || "no reply from the server";
const lnWho = a => String(a || "").replace(/^(agent|human|system):/, "");
const lnRel = t => (typeof mkRel === "function" ? mkRel(t) : "");
// context-bucket items are scope biz by rule (the CLI refuses anything else), so their other buttons are dead
const lnScopeLocked = it => !!it && it.bucket === "context";

// open newest first, decided newest decision first
function lnSplit(items) {
  const all = Array.isArray(items) ? items : [];
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
  const hay = [it.text, it.tag, it.scope, it.bucket, obs.agent, obs.agent_kind, lnNote(it), draft, it.id, (it.evidence || {}).owner_quote]
    .concat(lnTouches(it, people).map(x => x.label)).filter(Boolean).join(" ").toLowerCase();
  return words.every(w => hay.includes(w));
}

// ---------------------------------------------------------------- fetch, badge, status pane
async function lnFetch() {
  if (!state.biz) return null;
  const d = await api(`/biz/${state.biz}/learnings`);
  if (!d || d.error || !Array.isArray(d.items)) { lnStatusPaint([]); lnBadgePaint(0); return d; }
  _ln.biz = state.biz; _ln.items = d.items; _ln.counts = d.counts || null;
  // a reverted decision hands its note back as the draft, unless the owner already typed a new one
  for (const it of d.items) if (it.reopened && it.reopened.note && _ln.notes[it.id] === undefined) _ln.notes[it.id] = it.reopened.note;
  lnStatusPaint(d.items);
  return d;
}
// the Context rail badge, same span and class as the marketing one
function lnBadgePaint(n) {
  const el = $("#ctxbadge"); if (!el) return;
  const k = Number(n) || 0;
  el.hidden = !k;
  el.textContent = k > 9 ? "9+" : String(k);
}
// the /live poll owns the badge whenever the owner is not standing on the page (which paints it itself)
function lnLivePaint(live) {
  if (state.tab === "context" && state.ctx === "_learning") return;
  lnBadgePaint((live && live.learnings_open) || 0);
}
const lnStatusHtml = counts => `<div class="lnstat-t">Waiting</div>${LN_TAGS.map(([k, label, hex]) => {
  const n = (counts || {})[k] || 0;
  return `<div class="lnstat-l ${n ? "" : "zero"}"><span class="lndot" style="--lc:${esc(hex)}"></span><span class="lnstat-n">${n}</span><span class="lnstat-x">${esc(label.toLowerCase())}</span></div>`;
}).join("")}`;
function lnStatusPaint(items) {
  const el = $("#lnstatus"); if (!el) return;
  const counts = lnCountsByTag(items);
  el.innerHTML = lnStatusHtml(counts);
  lnBadgePaint(Object.values(counts).reduce((a, b) => a + b, 0));
  el.querySelectorAll(".lnstat-l").forEach(r => r.onclick = () => (typeof ctxGo === "function" ? ctxGo("_learning") : null));
}

// ---------------------------------------------------------------- the page
async function openLearning() {
  if (_ln.biz !== state.biz) _ln = lnFresh();                   // another business's cards never flash
  lnPaint(!_ln.items.length);
  const d = await lnFetch();
  if (state.ctx !== "_learning") return;                      // navigated away while the CLI ran
  if (d && d.error) { const v = $("#ctxview"); if (v) v.innerHTML = `<div class="empty bad">${esc(lnErr(d))}</div>`; return; }
  lnPaint();
}
function lnPaint(loading) {
  const v = $("#ctxview"); if (!v) return;
  v.innerHTML = `${typeof ctxCrumbs === "function" ? ctxCrumbs("", "Learning") : ""}
    <div class="th"><h2>Learning</h2><span class="sp"></span>
      <input class="inp lnsearch" id="lnsearch" type="search" placeholder="search both sides" spellcheck="false" value="${esc(_ln.q)}"></div>
    <div class="lnsplit">
      <div class="lnq"><div class="sh">Waiting on you <span class="cnt"></span></div><div class="lnbody"></div></div>
      <div class="lndone"><div class="sh">Decided <span class="cnt"></span></div><div class="lnbody"></div>
        <div class="lnfootnote">Applying a decision is a separate job and is not built yet.</div></div>
    </div>`;
  if (typeof wireCrumbs === "function") wireCrumbs(v);
  const q = v.querySelector("#lnsearch");
  if (q) q.oninput = () => { _ln.q = q.value; lnPaintCols(loading); };
  lnPaintCols(loading);
}
// the two columns alone, so typing in the search box never repaints the box itself
function lnPaintCols(loading) {
  const v = $("#ctxview"); if (!v) return;
  const people = (typeof _ctxData !== "undefined" && _ctxData && _ctxData.people) || [];
  const all = lnSplit(_ln.items), q = _ln.q.trim();
  const open = all.open.filter(it => lnMatch(it, q, people, _ln.notes[it.id]));
  const decided = all.decided.filter(it => lnMatch(it, q, people));
  const cards = list => `<div class="mkcards">${list.map(it => lnCardHtml(it, people)).join("")}</div>`;
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

// one card, open or decided: one head line (state ball on decided, type, scope icon(s), bucket icon, people
// chips, who, votes, age), the text, the
// evidence, the passes, then the notes box (closed until the card takes focus) and Reject / Approve —
// decided cards carry their note instead, and a Revert that puts them back on the left
function lnCardHtml(item, people) {
  const it = item || {}, t = lnTag(it.tag), obs = it.observed || {}, ev = it.evidence || {};
  const locked = lnScopeLocked(it);
  const pick = LN_SCOPES.map(([k, label, ico]) => {
    const off = locked && k !== "biz";
    return `<button class="lnscope ${it.scope === k ? "on" : ""}" type="button" data-scope="${esc(k)}" ${off ? `disabled title="context items are always scope biz"` : `title="${esc(label)}"`}>${ico}</button>`;
  }).join("");
  const votes = it.votes != null && it.passes ? `<span class="chip" title="passes that agreed">${esc(it.votes)}/${esc(it.passes)}</span>` : "";
  const touches = lnTouches(it, people).map(x => `<span class="chip ${esc(x.kind)}" ${x.title ? `title="${esc(x.title)}"` : ""}>${esc(x.label)}</span>`).join("");
  const scopeIco = `<span class="lnscopeico" title="${esc((LN_SCOPE_BY_KEY[it.scope] || {}).label || it.scope || "")}">${(LN_SCOPE_BY_KEY[it.scope] || {}).ico || ""}</span>`;
  const bk = LN_BUCKETS[it.bucket];
  const bucketIco = bk ? `<span class="lnbucket" data-bucket="${esc(it.bucket)}" title="${esc(bk.label)}">${bk.ico}</span>` : "";
  const stamp = [(ev.turns || []).join(" "), lnWho(obs.agent), String(ev.session || "").slice(0, 8)].filter(Boolean).join(" · ");
  const quote = ev.owner_quote ? `<div class="lnquote">“${esc(ev.owner_quote)}”<span class="src">${esc(stamp)}</span></div>` : "";
  const more = Array.isArray(it.evidence_more) ? it.evidence_more.filter(e => e && e.owner_quote) : [];
  const moreEv = more.length ? `<details class="mkchain"><summary>seen ${more.length} more time${more.length === 1 ? "" : "s"}</summary>${more.map(e =>
    `<div class="lnquote">“${esc(e.owner_quote)}”<span class="src">${esc([(e.turns || []).join(" "), String(e.session || "").slice(0, 8)].filter(Boolean).join(" · "))}</span></div>`).join("")}</details>` : "";
  const vs = Array.isArray(it.variants) ? it.variants : [];
  const variants = vs.length ? `<details class="mkchain"><summary>${esc(it.passes || vs.length)} passes, ${vs.length} wording${vs.length === 1 ? "" : "s"}</summary>${vs.map(vr =>
    `<div class="lnvar"><div class="vt">${esc(vr.text || "")}</div><div class="vm">${esc(vr.scope || "")} · ${esc(vr.confidence != null ? vr.confidence : "?")}</div></div>`).join("")}</details>` : "";
  if (!lnOpenItem(it)) {
    const st = String(it.state), note = lnNote(it);
    return `<div class="mkcard lncard done" data-id="${esc(it.id)}" data-state="${esc(st)}" style="--sc:${esc(LN_STATE_HEX[st] || "var(--fg-3)")}">
      <div class="mkhead"><span class="lnball" title="${esc(st)}"></span><span class="lntag" style="--tc:${esc(t.hex)}">${esc(t.label)}</span>${scopeIco}${bucketIco}${touches}<span class="sp"></span><span class="ago">${esc(lnRel((it.decision || {}).created || it.created))}</span></div>
      <div class="mkt">${esc(it.text || "")}</div>
      ${note ? `<div class="lnwhy">${esc(note)}</div>` : ""}
      <div class="mkfoot"><span class="lnpending">not yet applied</span><div class="mkacts">
        <button class="mka revert" type="button" data-act="reopen" title="back to the left, as if never decided">Revert</button>
      </div></div></div>`;
  }
  const notes = _ln.notes[it.id] || "";
  const noteBox = `<textarea class="lnnote" placeholder="a note for the record — it rides with your decision, and Reject needs one" spellcheck="false" ${_ln.noteOpen[it.id] || notes ? "" : "hidden"}>${esc(notes)}</textarea>`;
  return `<div class="mkcard lncard" tabindex="0" data-id="${esc(it.id)}" data-tag="${esc(it.tag || "")}">
    <div class="mkhead"><span class="lntag" style="--tc:${esc(t.hex)}">${esc(t.label)}</span><span class="lnpick">${pick}</span>${bucketIco}${touches}
      <span class="who">${esc(lnWho(obs.agent))}</span><span class="sp"></span>${votes}<span class="ago">${esc(lnRel(it.created))}</span></div>
    <div class="mkt">${esc(it.text || "")}</div>
    ${quote}${moreEv}${variants}${noteBox}
    <div class="mkfoot"><div class="mklinks"></div><div class="mkacts">
      <button class="mka reject" type="button" data-act="reject">Reject</button>
      <button class="mka approve" type="button" data-act="approve">Approve</button>
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
    ({ approve: lnApprove, reject: lnReject, reopen: lnReopen }[b.dataset.act] || (() => {}))(it);
  });
}
// every write is optimistic: the card moves at once, a failed write toasts and refetches the truth.
// The draft note is dropped once the decision is in; a revert hands the decision's note back as the draft.
function lnDecide(it, st, note) {
  it.state = st;
  it.decision = { state: st, note: note || "", actor: "human:owner", created: new Date().toISOString() };
  delete it.reopened;
  delete _ln.notes[it.id]; delete _ln.noteOpen[it.id]; lnDraftsSave();
  lnPaintCols(); lnStatusPaint(_ln.items);
}
function lnUndecide(it) {
  const note = lnNote(it);
  it.reopened = { state: "open", note, created: new Date().toISOString() };
  delete it.state; delete it.decision;
  if (note && !_ln.notes[it.id]) { _ln.notes[it.id] = note; lnDraftsSave(); }
  lnPaintCols(); lnStatusPaint(_ln.items);
}
async function lnRefetch() { const d = await lnFetch(); if (d && !d.error && state.ctx === "_learning") lnPaintCols(); }
async function lnSend(it, st, note, apply, label) {
  const id = it.id;
  if (_ln.busy[id]) return;
  _ln.busy[id] = 1;
  apply();
  let r; try { r = await post(`/biz/${state.biz}/learnings/${id}/state`, { state: st, note }); } catch (e) { r = null; }
  delete _ln.busy[id];
  if (!r || r.error) { notify({ text: `${label} failed`, sub: lnErr(r), color: "var(--danger)" }); return lnRefetch(); }
  toast(label.toLowerCase());
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
  if (lnOpenItem(it)) return;
  return lnSend(it, "open", "", () => lnUndecide(it), "Reverted");
}
// the scope picker saves on the click; there is no Save button anywhere on this page
async function lnSetScope(it, scope) {
  if (!scope || it.scope === scope || _ln.busy[it.id]) return;
  const was = it.scope;
  _ln.busy[it.id] = 1;
  it.scope = scope; lnPaintCols();
  let r; try { r = await post(`/biz/${state.biz}/learnings/${it.id}/edit`, { scope }); } catch (e) { r = null; }
  delete _ln.busy[it.id];
  if (!r || r.error) { it.scope = was; notify({ text: "Scope change failed", sub: lnErr(r), color: "var(--danger)" }); return lnRefetch(); }
  toast(`scope → ${scope}`);
}
