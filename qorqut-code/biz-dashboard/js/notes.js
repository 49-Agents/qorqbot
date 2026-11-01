// Notes: free-text blocks and dash items, autosave, activity board and Context snapshots.
// LF separates blocks. U+2028 stores an explicit soft break inside a block, displayed as LF.
// Enter starts a block; Shift+Enter continues it. Pasted physical lines remain separate blocks.
const NT_SOFT_BREAK = "\u2028";
const ntUploadingIds = new Set();
const ntUploadUncertainIds = new Set();
let ntFilingPoll = null;

// notes view state
let ntSel = null;                   // selected note id, or null for the homepage
let ntNotes = [];                   // cache of the last /notes fetch
let ntSearch = "";                  // detail sidebar search (in-memory filter)

// remember what was open (a note id, or "" for the homepage) per business
const ntScope = () => state.scope === 'global' ? '/root' : null;
const ntEndpoint = (scope = ntScope()) => scope === '/root' ? '/root/notes' : `/biz/${encodeURIComponent(scope)}/notes`;
const ntActive = () => tView === 'notes';
const ntKey = () => `nt:${ntScope()}`;
function ntSetSel(id) {
  ntSel = id;
  if (ntScope()) ls(ntKey(), id || "");
}
const ntEditingLocked = id => ntUploadingIds.has(id) || ntUploadUncertainIds.has(id);
let ntLoadedScope = null;
let ntRestoredBiz = null;           // biz slug whose saved selection we've already applied this session
let ntListOff = ls("ntlistoff") === "1";   // sidebar collapsed, persisted
let ntEditState = null;             // { id, biz, title0, lines0[], statuses0(json), lines[], statuses{}, dirty, saving, countdown }
let ntIdleT = null, ntTickT = null; // autosave idle/countdown timers

// icons (notes-local; the generic ICO_PIN/ICO_PENCIL/ICO_TRASH used in the context menu below come
// from js/agents.js, same as journal's).
const NT_ICO_PIN_TINY = `<svg class="ntpt" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>`;
const NT_ICO_PINOFF = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M15 9.34V6a2 2 0 0 0 0-4H9.55"/><path d="M9 9.35V6a2 2 0 0 1 2-2h1.53"/><path d="m2 2 20 20"/><path d="M9.51 9.51 5 15.24V16a1 1 0 0 0 1 1h9.53"/></svg>`;

// ---- helpers
const ntWords = c => { const s = String(c || "").trim(); return s ? s.split(/\s+/).length : 0; };
// first non-blank content line, task marker/bold-token stripped (jrPreview has no task lines to strip)
function ntPreview(content) {
  const lines = String(content || "").split("\n");
  for (const raw of lines) {
    if (!raw.trim()) continue;
    const d = ntParseRaw(raw).display.trim();
    if (d) return d;
  }
  return "";
}



// ---- data
async function ntFetch() {
  if (!ntScope()) { ntNotes = []; return; }
  const biz = ntScope(), r = await api(ntEndpoint(biz));
  if (ntScope() !== biz) return;
  if (Array.isArray(r)) { ntNotes = r; ntLoadedScope = biz; } else if (r && r.error) toast(r.error);
}
const ntById = id => ntNotes.find(n => n.id === id);
async function ntCreate(fields) {
  const r = await post(ntEndpoint(), fields || {});
  if (r && r.error) { toast(r.error); return null; }
  return r;
}
async function ntPatch(biz, id, fields) {
  const before = ntById(id) || {}, editing = ntEditState;
  let r;
  try { r = await post(`${ntEndpoint(biz)}/${encodeURIComponent(id)}`, {expected_revision:before.revision,expected_content_hash:before.content_hash,...fields}); }
  catch(error) { r={error:error.message || 'Could not save this page.'}; }
  if (r && r.error) {
    toast(r.error);
    if (editing && ntEditState===editing && editing.biz===biz && editing.id===id) {editing.error=r.error;ntPaintSaveState();}
    return null;
  }
  const baseRevision=fields.expected_revision ?? before.revision;
  if (editing && ntEditState===editing && editing.revision===baseRevision) {editing.revision=r.revision;editing.content_hash=r.content_hash;}
  if (ntScope()===biz && before.id===id) Object.assign(before,r);
  return r;
}
async function ntDeleteReq(id, version = ntById(id) || {}) {
  const r = await post(`${ntEndpoint()}/${encodeURIComponent(id)}/delete`, {expected_revision:version.revision,expected_content_hash:version.content_hash});
  if (r && r.error) { toast(r.error); return null; }
  return r;
}

// ---- entry / flush
// called by tasks.js's tSetView(v) on tab-entry ("notes") and by its 500ms business-change interval
function tPaintNotes() { ntOpenView(); }
function ntOpenView() {
  if (innerWidth <= 760) ntListOff = true;
  let justRestored = false;
  if(ntRestoredBiz!==ntScope()){ntNotes=[];ntSel=null;ntSearch="";ntLoadedScope=null;const body=$("#tbody");if(body)body.innerHTML='<p class="muted">Loading notes…</p>';}
  if (ntScope() && ntRestoredBiz !== ntScope()) {
    ntRestoredBiz = ntScope();
    const saved = ls(ntKey());
    // ntNotes isn't loaded yet here, so ntRender's not-found fallback would wipe an unverified id
    // right back to null; skip the synchronous render this once and let the fetch below paint it
    if (saved) { ntSel = saved; justRestored = true; }
  }
  if (!justRestored && ntLoadedScope === ntScope()) ntRender();
  if (!ntScope()) return;
  const biz = ntScope();
  ntFetch().then(() => { if (ntActive() && ntScope() === biz && !ntEditState?.dirty && !ntEditState?.saving) ntRender(); });
}
// force-save whatever's dirty in the open editor, reading the DOM/model before it's torn down by a
// repaint; used when the tab is left or the business/selection changes (tasks.js's tSetView/interval)
async function ntFlush() {
  const s = ntEditState; if (!s) return true;
  ntClearTimers();
  if ((s.dirty || s.saving) && !await ntSaveNow()) return false;
  if (s.dirty) return false;
  if (ntEditState === s) ntEditState = null;
  return true;
}

// ---- render dispatch
function ntRender() {
  const body = $("#tbody"); if (!body) return;
  if (!ntScope()) { body.className = "tbody"; body.innerHTML = `<div class="tempty"><div class="ic">${SVG_TINBOX}</div><p class="t">Pick a business first.</p></div>`; return; }
  if (ntSel == null) {
    body.className = "tbody nt nt-home";
    body.innerHTML = ntHomeHtml();
    ntWireHome(body);
  } else {
    const n = ntById(ntSel);
    if (!n) { ntSel = null; return ntRender(); }
    body.className = "tbody nt nt-detail";
    body.innerHTML = ntDetailHtml(n);
    ntWireDetail(body, n);
  }
}
// non-typing refreshes (pin, rename): patch sidebar/meta in place, never rebuild the homepage or the
// open lines editor from a background write
function ntRenderCurrent() {
  if (!ntActive()) return;
  if (ntSel == null) { ntRender(); return; }
  ntPatchSidebar(); ntPatchMeta();
}

// ---- homepage
// Shared card and activity-board presentation.
function ntCardHtml(n) {
  const words = ntWords(n.content);
  return writingCard({id:n.id, title:n.title, updated:n.updatedAt, detail:`${words} ${words === 1 ? 'word' : 'words'}`, icon:n.pinned ? NT_ICO_PIN_TINY : ''});
}
function ntHomeHtml() {
  const n = ntNotes.length;
  const byUpdated = ntNotes.slice().sort((a, b) => Date.parse(b.updatedAt || 0) - Date.parse(a.updatedAt || 0));
  const pinned = byUpdated.filter(x => x.pinned);
  const today = tToday(), weekAgo = today - 7 * T_DAY;
  const editedToday = byUpdated.filter(x => Date.parse(x.updatedAt || 0) >= today);
  const editedWeek = byUpdated.filter(x => { const t = Date.parse(x.updatedAt || 0); return t < today && t >= weekAgo; });
  const sec = (label, icon, rows) => rows.length ? `<div class="jrsec"><div class="jreyebrow">${icon || ""}${esc(label)}</div><div class="jrgrid">${rows.map(ntCardHtml).join("")}</div></div>` : "";
  // Assigned dates keep daily entries stable when edited later.
  const heatEntries = ntNotes.map(x => ({ date: String(x.date || x.createdAt || "").slice(0, 10) }));
  return `<div class="jrhome notes-home">
    <div class="jrhhead">
      <div><div class="jrsub">${n} ${n === 1 ? "page" : "pages"}</div></div>
      <button id="ntnew" class="btn" type="button">${ICO_PLUS}Create new</button>
    </div>
    ${writingHeatmapHtml(heatEntries)}
    ${sec("Pinned", NT_ICO_PIN_TINY, pinned)}
    ${sec("Edited today", "", editedToday)}
    ${sec("Edited last week", "", editedWeek)}
    ${sec("All notes", "", byUpdated)}
    ${!n ? `<div class="jrfaint jrfaint-lg">No pages yet.</div>` : ""}
  </div>`;
}
function ntWireHome(root) {
  root.querySelectorAll(".jrcard").forEach(c => {
    c.onclick = () => ntSelect(c.dataset.id);
    c.oncontextmenu = e => { e.preventDefault(); ntCtxMenu(e.clientX, e.clientY, c.dataset.id); };
  });
  const nb = root.querySelector("#ntnew"); if (nb) nb.onclick = () => ntCreateNew();
  root.querySelectorAll(".jrhcell[data-key]").forEach(cell => {
    cell.setAttribute("aria-label", cell.getAttribute("data-tip") + ". Open or create a note.");
    cell.onclick = () => ntPickDay(cell.dataset.key);
  });
}

// ---- detail: sidebar list
// Search every note, including entries retained from Journal.
function ntFilterNotes() {
  const capped = ntNotes;
  const q = ntSearch.trim().toLowerCase();
  if (!q) return capped;
  return capped.filter(n => (n.title || "").toLowerCase().includes(q) || (n.content || "").toLowerCase().includes(q));
}
function ntRowHtml(n) {
  return `<div class="ntrow ${n.id === ntSel ? "on" : ""}" data-id="${esc(n.id)}">
    ${n.id === ntSel ? `<span class="ntind"></span>` : ""}
    <div class="ntr1"><span class="ntrttl">${n.pinned ? NT_ICO_PIN_TINY : ""}${esc(n.title && n.title.trim() ? n.title : "Untitled")}</span><span class="ntrtime">${esc(relativeTime(n.updatedAt))}</span></div>
    <div class="ntr2">${esc(ntPreview(n.content) || "Empty note")}</div>
    <button class="ntrowdel" type="button" data-id="${esc(n.id)}" aria-label="Archive page" data-tip="Archive page">${aicon('archive','currentColor',16)}</button>
  </div>`;
}
function ntListHtml() {
  const filtered = ntFilterNotes();
  const pinned = filtered.filter(n => n.pinned), rest = filtered.filter(n => !n.pinned);
  let html = "";
  if (pinned.length) html += `<div class="nteyebrow ntsm">Pinned</div>` + pinned.map(ntRowHtml).join("");
  html += rest.map(ntRowHtml).join("");
  if (!filtered.length) html += `<div class="ntfaint ntfaint-pad">No pages found.</div>`;
  return html;
}
function ntWireListRows(list) {
  if (!list) return;
  list.querySelectorAll(".ntrow").forEach(row => {
    row.onclick = e => { if (e.target.closest(".ntrowdel")) return; ntSelect(row.dataset.id); };
    row.oncontextmenu = e => { e.preventDefault(); ntCtxMenu(e.clientX, e.clientY, row.dataset.id); };
  });
  list.querySelectorAll(".ntrowdel").forEach(b => b.onclick = e => { e.stopPropagation(); ntDeleteFlow(b.dataset.id); });
}
function ntPatchSidebar() {
  const list = $("#ntlist"); if (!list) return;
  const scroll = list.scrollTop;
  list.innerHTML = ntListHtml();
  list.scrollTop = scroll;
  ntWireListRows(list);
  const foot = $("#ntfoot"); if (foot) foot.textContent = `${ntNotes.length} ${ntNotes.length === 1 ? "page" : "pages"}`;
}

// ---- detail: editor chrome (meta line, save pill) — patchable without touching the lines editor
function ntMetaHtml(n) {
  const words = ntWords(ntEditState ? ntEditState.lines.join("\n") : n.content);
  // no date here (same line as the journal's): just the word count
  const filingTag = ntScope() === '/root' ? 'div' : 'span';
  return `<span class="ntmeta-words">${words} word${words === 1 ? "" : "s"}</span> <${filingTag} id="ntfiling">${ntFilingHtml(n)}</${filingTag}>`;
}
function ntRingSvg(nn) {
  const total = 8, frac = Math.max(0, Math.min(1, nn / total)), r = 6, c = 2 * Math.PI * r, off = c * (1 - frac);
  return `<svg width="16" height="16" viewBox="0 0 16 16" class="ntring"><circle cx="8" cy="8" r="${r}" fill="none" stroke="currentColor" stroke-opacity=".25" stroke-width="2"/><circle cx="8" cy="8" r="${r}" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="${c}" stroke-dashoffset="${off}" transform="rotate(-90 8 8)"/></svg>`;
}
function ntSaveHtml() {
  const s = ntEditState; if (!s) return "";
  if (s.saving) return `<button class="btn ntsavepill busy" type="button" disabled>Saving…</button>`;
  if (s.dirty) return `<button class="btn ntsavepill" type="button">${s.countdown != null ? ntRingSvg(s.countdown) : ""}Save</button>`;
  return "";  // nothing to save: no pill at all, same as Discard
}
function ntPaintSaveState() {
  const el = $("#ntsavewrap"); if (!el) return;
  el.innerHTML = ntSaveHtml();
  const btn = el.querySelector(".ntsavepill"); if (btn) btn.onclick = () => ntSaveNow();
  // Discard only exists while there are unsaved changes: hidden otherwise, not just disabled
  const disc = $("#ntdiscard"); if (disc) disc.hidden = !ntEditState || !ntEditState.dirty;
  const error = $('#nterror');
  if (error) {
    error.hidden = !ntEditState || !ntEditState.error;
    error.querySelector('span').textContent = ntEditState && ntEditState.error || '';
    error.querySelector('button').onclick = async()=>{await ntFetch();if(ntActive())ntRender();};
  }
}
function ntPatchMeta() {
  const n = ntById(ntSel); if (!n) return;
  const meta = $("#ntmeta"); if (meta) meta.innerHTML = ntMetaHtml(n);
  ntPaintUpload();
}

// ---- detail: full build (selection change / tab open / create-new only)
function ntDetailHtml(n) {
  return `<div class="nted">
    <div class="ntside ${ntListOff ? "off" : ""}" id="ntside">
      <div class="ntsidectrl">
        <button id="ntnew2" class="btn" type="button">${ICO_PLUS}New page</button>
      </div>
      <div class="ntsearchwrap"><input id="ntsearch" class="ntsearch-in" placeholder="Search pages…" value="${esc(ntSearch)}" autocomplete="off" spellcheck="false"></div>
      <div class="ntdivider"></div>
      <div class="ntlist" id="ntlist">${ntListHtml()}</div>
      <div class="ntfoot" id="ntfoot">${ntNotes.length} ${ntNotes.length === 1 ? "page" : "pages"}</div>
    </div>
    <div class="nteditor" id="nteditor">
      <div class="ntedinner">
        <div class="ntedhead">
          <textarea id="nttitle" class="nttitlein" rows="1" placeholder="Untitled"></textarea>
          <div class="ntedctrls" id="ntedctrls">
            <button id="ntdiscard" class="btn quiet" type="button" hidden>Discard</button>
            <span class="ntsavewrap" id="ntsavewrap"></span>
            <span id="ntuploadwrap">${ntUploadButton(n)}</span>
            <button id="ntvisibility" class="nticon ntvisibility${n.ordubashyVisible !== false ? ' on' : ''}" type="button" role="switch" aria-checked="${n.ordubashyVisible !== false}" aria-label="Allow Ordubaşı to review this entry" title="${n.ordubashyVisible !== false ? 'Visible to Ordubaşı' : 'Hidden from Ordubaşı'}">${agentGlyph({role:{id:'ordubashy'}},18)}</button>
            <button id="ntdel" class="nticon" type="button" aria-label="Archive page" data-tip="Archive page">${aicon('archive','currentColor',16)}</button>
          </div>
        </div>
        <div class="ntmeta" id="ntmeta">${ntMetaHtml(n)}</div>
        ${ntScope() === "/root" ? "" : `<div id="nt-stories" class="small" aria-label="Related context"></div>`}
        <div id="nterror" class="ferr" role="alert" hidden><span></span><button class="ghost" type="button">Reload page</button></div>
        <div class="nthr"></div>
        <div class="nteditorhint">Enter: new block · Shift+Enter: continue this block</div>
        <div class="ntlinewrap" id="ntlinewrap">
          <div class="ntlines" id="ntlines"></div>
          <div class="ntoverlay" id="ntoverlay" hidden>
            <div class="ntoh">What's on your mind today?</div>
            <div class="nto2">Capture a thought, idea, or reflection.</div>
          </div>
        </div>
      </div>
    </div>
  </div>`;
}
function ntWireDetail(root, n) {
  if(ntScope()!=='/root')storyRelatedChips(root.querySelector('#nt-stories'),ntScope(),'notes',n.id,()=>ntFlush());
  const lines = (n.content || "").split("\n");
  const statuses = Object.assign({}, n.statuses || {});
  ntEditState = {
    id: n.id, biz: ntScope(), revision:n.revision, content_hash:n.content_hash, title0: n.title || "", lines0: lines.slice(), statuses0: JSON.stringify(statuses),
    lines: lines.slice(), statuses, dirty: false, saving: false, countdown: null,
  };
  if (ntScope() === '/root' && ntGetUploadAttempt(n.id)) ntUploadUncertainIds.add(n.id);
  const titleEl = $("#nttitle");
  titleEl.value = n.title || ""; titleEl.readOnly = ntUploadUncertainIds.has(n.id); ntGrow(titleEl);
  ntRenderLines();
  ntEditState.history = [ntHistorySnapshot()];
  ntEditState.historyIndex = 0;
  root.onkeydown = e => {
    if (!e.target.closest('#nttitle, #ntlines')) return;
    const mod = e.ctrlKey || e.metaKey, key = e.key.toLowerCase();
    if (mod && !e.altKey && (key === 'z' || key === 'y')) {
      if (e.isComposing) return;
      e.preventDefault(); e.stopPropagation();
      ntUndoRedo(key === 'y' || e.shiftKey);
    }
  };
  root.onpointerdown = () => ntRememberSelection();
  root.onbeforeinput = e => {
    if (!e.target.closest('#nttitle, #ntlines')) return;
    if (e.inputType === 'historyUndo' || e.inputType === 'historyRedo') {
      e.preventDefault(); ntUndoRedo(e.inputType === 'historyRedo');
    } else if (!e.defaultPrevented) ntRememberSelection();
  };

  titleEl.oninput = () => { ntGrow(titleEl); ntEditState.dirty = true; ntSchedule(); };
  titleEl.onkeydown = e => {
    if (e.key === "Enter") { e.preventDefault(); titleEl.blur(); }
    else if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) { e.preventDefault(); ntSaveNow(); }
  };
  titleEl.onblur = event => { if (ntEditState && ntEditState.dirty && !event.relatedTarget?.closest('#nterror, #ntdiscard')) ntSaveNow(); };

  root.querySelector("#ntnew2").onclick = () => ntCreateNew();
  root.querySelector("#ntdel").onclick = () => ntDeleteFlow(n.id);
  root.querySelector("#ntdiscard").onclick = () => ntDiscard();
  root.querySelector("#ntvisibility").onclick = async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      if (!await ntFlush()) return;
      const current = ntById(n.id);
      if (!current) return;
      const saved = await ntPatch('/root', n.id, {ordubashy_visible: current.ordubashyVisible === false});
      if (saved) await ntFetch();
      if (ntActive() && ntSel === n.id) ntRender();
    } finally { button.disabled = false; }
  };

  const search = root.querySelector("#ntsearch");
  let searchT = null;
  search.oninput = () => { clearTimeout(searchT); searchT = setTimeout(() => { ntSearch = search.value; ntPatchSidebar(); }, 80); };
  ntWireListRows(root.querySelector("#ntlist"));

  // click anywhere in the editor's empty space (not on a line/ball) focuses the end of the last line
  const wrap = root.querySelector("#ntlinewrap");
  wrap.onclick = e => {
    if (e.target.closest("#ntdoc") || e.target.closest(".ntball")) return;
    const es = ntEditState; if (!es) return;
    ntFocusLine(es.lines.length - 1, Infinity);
  };

  ntWireUpload(); ntWatchFiling();
  ntPaintSaveState();   // the wrapper's innerHTML was built before ntEditState existed — patch it now
}


const NT_DIVIDER = "⸻";                                  // Ctrl/Cmd+= inserts this exact line
const NT_STATUS_ORDER = ["todo", "white", "in_progress", "done"];   // the left-click cycle
const NT_STATUS_LABEL = { todo: "Open", white: "White", in_progress: "In progress", done: "Done", blocked: "Blocked", cancelled: "Cancelled" };
const ntNextStatus = cur => { const i = NT_STATUS_ORDER.indexOf(cur); return i === -1 ? "todo" : NT_STATUS_ORDER[(i + 1) % NT_STATUS_ORDER.length]; };

const ntIsTaskLine = raw => /^[ \t]*-/.test(raw || "");
// Split a raw line into its task marker / invisible bold token / display text (lines-editor.tsx parseRaw).
function ntParseRaw(raw) {
  raw = raw || "";
  let rest = raw, marker = "", lead = "";
  const mk = raw.match(/^([ \t]*)-[ \t]?/);
  if (mk) { marker = mk[0]; lead = mk[1] || ""; rest = raw.slice(mk[0].length); }
  const meta = { bold: false };
  const mm = rest.match(/^⁣([^⁣]*)⁣ ?/);
  if (mm) { rest = rest.slice(mm[0].length); mm[1].split(",").forEach(f => { if (f === "b") meta.bold = true; }); }
  return { marker, meta, display: (lead + rest).replaceAll(NT_SOFT_BREAK, "\n") };
}
const ntBuildMetaToken = meta => meta.bold ? "⁣b⁣ " : "";
// Map a display-space edit back to the raw line; typing "-" at the start auto-converts to a task.
function ntToRaw(es, v, i) {
  v = v.replaceAll("\n", NT_SOFT_BREAK);
  const meta = ntBuildMetaToken(ntParseRaw(es.lines[i]).meta);
  if (ntIsTaskLine(es.lines[i])) return v === "" ? "" : `- ${meta}${v}`;
  if (v.startsWith("-")) return `- ${meta}${v.slice(1).replace(/^\s/, "")}`;
  return v === "" ? "" : `${meta}${v}`;
}
const ntDisp = (es, i) => ntParseRaw(es.lines[i]).display;
const ntIsRuleAt = (es, i) => (es.lines[i] || "").trim() === NT_DIVIDER;




const ntStatusKey = (es, i) => ntDisp(es, i).trim();
function ntLineStatus(es, i) {
  return es.statuses[ntStatusKey(es, i)] || "todo";
}
function ntSetStatus(es, i, status) {
  const key = ntStatusKey(es, i);
  if (status === "todo") delete es.statuses[key]; else es.statuses[key] = status;
}
// Drop status entries whose key no longer matches any current line's trimmed display text — called
// right before a save so a renamed/deleted task line's stale status doesn't accumulate in the map forever.
function ntPruneStatuses(es) {
  const live = new Set(es.lines.map((_, i) => ntStatusKey(es, i)));
  Object.keys(es.statuses).forEach(k => { if (!live.has(k)) delete es.statuses[k]; });
}

// Single-surface editor: the whole note is ONE contenteditable with a block <div> per line, so a
// drag, shift-click or Ctrl+A selects across tasks, dividers and prose alike. Typing inside a block is
// native and re-read into es.lines; anything that changes block structure (Enter, joins, edits that
// span blocks, paste, cut) is applied to the model and the surface is rebuilt.
const ntDoc = () => $("#ntdoc");
let ntLineResizeObserver = null;
const ntBlockText = b => b.textContent.replace(/​/g, "");
function ntSetEditable(doc, on) {
  if (!on) { doc.contentEditable = "false"; return; }
  doc.contentEditable = "plaintext-only";
  if (doc.contentEditable !== "plaintext-only") doc.contentEditable = "true";
}
function ntBlocks(es) {
  return es.lines.map((raw, i) => {
    if (ntIsRuleAt(es, i)) return { rule: true, task: false, bold: false, text: "" };
    const p = ntParseRaw(raw);
    return { rule: false, task: ntIsTaskLine(raw), bold: p.meta.bold, text: p.display };
  });
}
function ntComposeRaw(b) {
  if (b.rule) return NT_DIVIDER;
  const t = b.text.replaceAll("\n", NT_SOFT_BREAK), meta = ntBuildMetaToken({ bold: b.bold });
  if (b.task) return `- ${meta}${t}`;
  return t === "" ? "" : `${meta}${t}`;
}
function ntDocBlocks() {
  const out = [];
  for (const n of ntDoc().childNodes) {
    if (n.nodeType === 3) { if (n.nodeValue) out.push({ rule: false, task: false, bold: false, text: n.nodeValue }); continue; }
    if (n.nodeType !== 1) continue;
    if (n.classList.contains("ntrule")) { out.push({ rule: true, task: false, bold: false, text: "" }); continue; }
    out.push({ rule: false, task: n.classList.contains("nttask"), bold: n.classList.contains("bold"), text: ntBlockText(n) });
  }
  return out.length ? out : [{ rule: false, task: false, bold: false, text: "" }];
}
function ntMakeBlock(es, i) {
  const el = document.createElement("div");
  el.dataset.i = i;
  if (ntIsRuleAt(es, i)) { el.className = "ntline ntrule"; el.contentEditable = "false"; el.innerHTML = '<div class="ntrulehr"></div>'; return el; }
  const raw = es.lines[i], task = ntIsTaskLine(raw), { meta, display } = ntParseRaw(raw);
  el.className = ["ntline", task && "nttask", meta.bold && "bold"].filter(Boolean).join(" ");
  if (task) {
    const status = ntLineStatus(es, i);
    if (status === "done" || status === "cancelled") el.classList.add("done");
  }
  if (display) el.append(display);
  if (display === "" || display.endsWith("\n")) el.append(document.createElement("br"));
  return el;
}

// selection as {i: block index, pos: offset in that block's display text}
function ntPosOf(node, offset) {
  const doc = ntDoc(); if (!doc || !doc.contains(node)) return null;
  const kids = doc.children;
  if (node === doc) {
    if (!kids.length) return { i: 0, pos: 0 };
    const i = Math.min(offset, kids.length - 1);
    return { i, pos: offset >= kids.length ? ntBlockText(kids[i]).length : 0 };
  }
  let b = node; while (b.parentNode !== doc) b = b.parentNode;
  const i = Array.prototype.indexOf.call(kids, b);
  if (b.classList.contains("ntrule")) return { i, pos: 0 };
  const r = document.createRange(); r.setStart(b, 0); r.setEnd(node, offset);
  return { i, pos: r.toString().replace(/​/g, "").length };
}
function ntSelection() {
  const s = getSelection(); if (!s.rangeCount) return null;
  const a = ntPosOf(s.anchorNode, s.anchorOffset), f = ntPosOf(s.focusNode, s.focusOffset);
  return a && f ? { a, f } : null;
}
const ntOrdered = s => (s.a.i < s.f.i || (s.a.i === s.f.i && s.a.pos <= s.f.pos)) ? [s.a, s.f] : [s.f, s.a];
// Keep a triple-click inside this block: native paragraph selection can end at the
// next block's start, making replacement consume the following line's boundary.
function ntDocMousedown(e) {
  if (e.button !== 0 || e.detail !== 3) return;
  const block = e.target.closest(".ntline");
  if (!block || block.classList.contains("ntrule")) return;
  e.preventDefault();
  const i = Number(block.dataset.i);
  ntDoc().focus({ preventScroll: true });
  ntSetSelection({ i, pos: 0 }, { i, pos: ntBlockText(block).length });
}
function ntDomPoint(i, pos) {
  const doc = ntDoc(), b = doc.children[i];
  if (!b) return { node: doc, offset: doc.childNodes.length };
  if (b.classList.contains("ntrule")) return { node: doc, offset: i };
  const w = document.createTreeWalker(b, NodeFilter.SHOW_TEXT);
  let acc = 0, n;
  while ((n = w.nextNode())) { const len = n.nodeValue.length; if (pos <= acc + len) return { node: n, offset: pos - acc }; acc += len; }
  return { node: b, offset: 0 };
}
function ntSetSelection(a, f) {
  const p = ntDomPoint(a.i, a.pos), q = ntDomPoint(f.i, f.pos);
  getSelection().setBaseAndExtent(p.node, p.offset, q.node, q.offset);
}
function ntFocusLine(index, pos) {
  const doc = ntDoc(); if (!doc || !doc.children.length) return;
  const n = doc.children.length, isRule = k => doc.children[k].classList.contains("ntrule");
  let i = Math.max(0, Math.min(index, n - 1));
  while (i < n - 1 && isRule(i)) { i++; pos = 0; }
  while (i > 0 && isRule(i)) { i--; pos = Infinity; }
  if (isRule(i)) return;
  const p = Math.min(pos, ntBlockText(doc.children[i]).length);
  if (document.activeElement !== doc) doc.focus({ preventScroll: true });
  ntSetSelection({ i, pos: p }, { i, pos: p });
  doc.children[i].scrollIntoView({ block: "nearest" });
}

function ntRenderLines() {
  const wrap = $("#ntlines"); if (!wrap) return;
  const es = ntEditState; if (!es) return;
  let doc = ntDoc();
  if (!doc) {
    doc = document.createElement("div");
    doc.id = "ntdoc"; doc.className = "ntdoc";
    doc.setAttribute("role", "textbox"); doc.setAttribute("aria-multiline", "true"); doc.setAttribute("aria-label", "Note");
    doc.spellcheck = false;
    doc.onbeforeinput = ntDocBeforeInput; doc.oninput = ntDocInput; doc.onkeydown = ntDocKeydown;
    doc.oncopy = e => ntDocClipboard(e, false); doc.oncut = e => ntDocClipboard(e, true); doc.onpaste = ntDocPaste;
    doc.ondrop = e => e.preventDefault();
    doc.onmousedown = ntDocMousedown;
    ntLineResizeObserver?.disconnect();
    ntLineResizeObserver = new ResizeObserver(() => ntPlaceBalls());
    ntLineResizeObserver.observe(doc);
    doc.onfocus = () => ntPaintOverlay();
    doc.onblur = () => setTimeout(ntPaintOverlay, 0);
    const gutter = document.createElement("div");
    gutter.id = "ntgutter"; gutter.className = "ntgutter";
    gutter.onmousedown = e => e.preventDefault();
    wrap.replaceChildren(doc, gutter);
  }
  ntSetEditable(doc, !ntEditingLocked(ntSel));
  doc.replaceChildren(...es.lines.map((_, i) => ntMakeBlock(es, i)));
  ntRenderBalls();
  ntPaintOverlay();
}
// Status balls live in a layer beside the editable text, not inside it, so they never sit in the
// selectable flow (a non-editable node at a block start makes triple-click and range ends collapse).
function ntRenderBalls() {
  const es = ntEditState, g = $("#ntgutter"); if (!es || !g) return;
  g.replaceChildren(...es.lines.flatMap((raw, i) => {
    if (!ntIsTaskLine(raw) || ntIsRuleAt(es, i)) return [];
    const status = ntLineStatus(es, i), w = document.createElement("span");
    w.className = "ntballwrap";
    w.innerHTML = `<button type="button" class="ntball ntb-${esc(status)}" data-i="${i}" aria-label="Status: ${esc(NT_STATUS_LABEL[status] || status)}. Click to change."><span class="ntdot"></span></button>`;
    const b = w.firstChild;
    b.onclick = e => { e.stopPropagation(); e.preventDefault(); ntCycleBall(i); };
    b.oncontextmenu = e => { e.preventDefault(); e.stopPropagation(); ntToggleBlocked(i); };
    return [w];
  }));
  ntPlaceBalls();
}
function ntPlaceBalls() {
  const doc = ntDoc(), g = $("#ntgutter"); if (!doc || !g) return;
  g.querySelectorAll(".ntball").forEach(b => {
    const blk = doc.children[Number(b.dataset.i)];
    if (blk) b.parentNode.style.top = `calc(${blk.offsetTop}px + 15.5px * 1.75 / 2)`;
  });
}
function ntPaintOverlay() {
  const es = ntEditState; if (!es) return;
  const ov = $("#ntoverlay"); if (!ov) return;
  const empty = es.lines.length <= 1 && !es.lines[0];
  ov.hidden = !(empty && document.activeElement !== ntDoc());
}
// Commit a structural change: rebuild the surface from new raw lines and put the caret back.
function ntCommit(newLines, focus) {
  const es = ntEditState; if (!es) return;
  es.lines = newLines; es.dirty = true;
  ntRenderLines();
  if (focus) ntFocusLine(focus.index, focus.pos);
  ntSchedule();
}
// Replace the range [a,f] with `text` (each "\n" starts a new block; soft keeps it inside the block).
function ntEditRange(a, f, text, soft = false) {
  const es = ntEditState; if (!es) return;
  const bl = ntBlocks(es), first = bl[a.i], last = bl[f.i];
  const head = first.rule ? "" : first.text.slice(0, a.pos), tail = last.rule ? "" : last.text.slice(f.pos);
  const src = first.rule ? {} : first;
  const mk = t => ({ rule: false, task: !!src.task, bold: !!src.bold, text: t });
  const parts = soft ? [text] : text.split("\n");
  const mid = [];
  let pos;
  if (parts.length === 1) { mid.push(ntComposeRaw(mk(head + parts[0] + tail))); pos = head.length + parts[0].length; }
  else {
    mid.push(ntComposeRaw(mk(head + parts[0])));
    for (let k = 1; k < parts.length - 1; k++) mid.push(parts[k]);
    mid.push(parts[parts.length - 1] + tail.replaceAll("\n", NT_SOFT_BREAK));
    pos = parts[parts.length - 1].length;
  }
  ntCommit([...bl.slice(0, a.i).map(ntComposeRaw), ...mid, ...bl.slice(f.i + 1).map(ntComposeRaw)],
    { index: a.i + (soft ? 0 : parts.length - 1), pos });
}
function ntRangeText(a, f) {
  const bl = ntBlocks(ntEditState), out = [];
  for (let i = a.i; i <= f.i; i++) {
    const b = bl[i], s = b.rule ? NT_DIVIDER : b.text;
    const from = i === a.i ? a.pos : 0, piece = s.slice(from, i === f.i ? f.pos : s.length);
    out.push(b.task && from === 0 ? "- " + piece : piece);
  }
  return out.join("\n");
}

function ntDocBeforeInput(e) {
  const es = ntEditState; if (!es) return;
  const t = e.inputType;
  if (t === "historyUndo" || t === "historyRedo") return;
  if (ntEditingLocked(ntSel)) { e.preventDefault(); return; }
  if (e.isComposing) return;
  const s = ntSelection(); if (!s) return;
  const [a, f] = ntOrdered(s), multi = a.i !== f.i, collapsed = !multi && a.pos === f.pos;
  const take = fn => { e.preventDefault(); ntRememberSelection(); fn(); };
  if (t === "insertParagraph") return take(() => ntEditRange(a, f, "\n"));
  if (t === "insertLineBreak") return take(() => ntEditRange(a, f, "\n", true));
  if (t === "insertFromDrop" || t === "deleteByDrag") return e.preventDefault();
  if (t.startsWith("delete")) {
    if (multi) return take(() => ntEditRange(a, f, ""));
    if (!collapsed) return;
    const bl = ntBlocks(es), b = bl[a.i], n = bl.length;
    if (t.includes("Backward") && a.pos === 0) {
      if (b.task) return take(() => { b.task = false; ntCommit(bl.map(ntComposeRaw), { index: a.i, pos: 0 }); });
      if (a.i === 0) return;
      return take(() => {
        const prev = bl[a.i - 1];
        if (prev.rule) { bl.splice(a.i - 1, 1); ntCommit(bl.map(ntComposeRaw), { index: a.i - 1, pos: 0 }); return; }
        const at = prev.text.length; prev.text += b.text; bl.splice(a.i, 1);
        ntCommit(bl.map(ntComposeRaw), { index: a.i - 1, pos: at });
      });
    }
    if (t.includes("Forward") && a.pos === b.text.length && a.i < n - 1) {
      return take(() => {
        const next = bl[a.i + 1];
        if (next.rule) { bl.splice(a.i + 1, 1); ntCommit(bl.map(ntComposeRaw), { index: a.i, pos: b.text.length }); return; }
        const at = b.text.length; b.text += next.text; bl.splice(a.i + 1, 1);
        ntCommit(bl.map(ntComposeRaw), { index: a.i, pos: at });
      });
    }
    return;
  }
  if (multi && t.startsWith("insert")) return take(() => ntEditRange(a, f, e.data || ""));
}
// Native typing inside one block: re-read the surface; typing "-" at the start of a plain line makes it a task.
function ntDocInput(e) {
  const es = ntEditState; if (!es) return;
  const doc = ntDoc(), bl = ntDocBlocks(), s = ntSelection();
  if (s && s.a.i === s.f.i && !e.isComposing) {
    const b = bl[s.a.i];
    if (b && !b.rule && !b.task && b.text.startsWith("-")) {
      b.task = true; b.text = b.text.slice(1).replace(/^\s/, "");
      ntCommit(bl.map(ntComposeRaw), { index: s.a.i, pos: Math.max(0, s.a.pos - 1) });
      return;
    }
  }
  const stray = [...doc.childNodes].some(n => n.nodeType !== 1 || !n.classList.contains("ntline"));
  if (stray) { ntCommit(bl.map(ntComposeRaw), s ? { index: s.a.i, pos: s.a.pos } : null); return; }
  es.lines = bl.map(ntComposeRaw); es.dirty = true;
  ntPlaceBalls();
  ntPaintOverlay();
  ntSchedule();
}
function ntDocKeydown(e) {
  const mod = e.ctrlKey || e.metaKey, k = e.key;
  if (k === "Enter" && !mod && !e.isComposing && !ntEditingLocked(ntSel)) {
    // plaintext-only reports both Enter and Shift+Enter as insertLineBreak, so decide here
    const s = ntSelection(); if (!s) return;
    e.preventDefault(); ntRememberSelection();
    const [a, f] = ntOrdered(s); ntEditRange(a, f, "\n", e.shiftKey);
  }
  else if (mod && (k === "s" || k === "S" || k === "Enter")) { e.preventDefault(); ntSaveNow(); }
  else if (mod && (k === "b" || k === "B")) { e.preventDefault(); ntToggleBold(); }
  else if (mod && (k === "=" || k === "+")) { e.preventDefault(); ntInsertDivider(); }
}
function ntDocClipboard(e, cut) {
  const s = ntSelection(); if (!s) return;
  const [a, f] = ntOrdered(s);
  if (a.i === f.i && a.pos === f.pos) return;
  e.preventDefault();
  e.clipboardData.setData("text/plain", ntRangeText(a, f));
  if (cut && !ntEditingLocked(ntSel)) { ntRememberSelection(); ntEditRange(a, f, ""); }
}
function ntDocPaste(e) {
  e.preventDefault();
  if (ntEditingLocked(ntSel)) return;
  const text = (e.clipboardData?.getData("text/plain") || "").replace(/\r\n?/g, "\n");
  const s = ntSelection(); if (!text || !s) return;
  const [a, f] = ntOrdered(s);
  ntRememberSelection();
  ntEditRange(a, f, text);
}
// Ctrl/Cmd+=: a blank block becomes the divider, otherwise the divider goes below the caret's block.
function ntInsertDivider() {
  const es = ntEditState, s = ntSelection(); if (!es || !s || ntEditingLocked(ntSel)) return;
  const i = s.f.i, last = i === es.lines.length - 1;
  if (ntDisp(es, i).trim() === "") ntCommit([...es.lines.slice(0, i), NT_DIVIDER, ...(last ? [""] : []), ...es.lines.slice(i + 1)], { index: i + 1, pos: 0 });
  else ntCommit([...es.lines.slice(0, i + 1), NT_DIVIDER, ...(last ? [""] : []), ...es.lines.slice(i + 1)], { index: i + 2, pos: 0 });
}

// Ctrl/Cmd+B: bold for every text block in the selection (all bold -> unbold), stored as the invisible
// "⁣b⁣ " token; patches classes in place so the caret and selection stay put.
function ntToggleBold() {
  const es = ntEditState, sel = ntSelection(); if (!es || !sel || ntEditingLocked(ntSel)) return;
  const [a, f] = ntOrdered(sel), bl = ntBlocks(es);
  const idx = []; for (let i = a.i; i <= f.i; i++) if (!bl[i].rule) idx.push(i);
  if (!idx.length) return;
  const on = !idx.every(i => bl[i].bold);
  idx.forEach(i => { bl[i].bold = on; ntBlockClass(i, "bold", on); });
  es.lines = bl.map(ntComposeRaw); es.dirty = true;
  ntSchedule();
}
const ntBlockClass = (i, cls, on) => ntDoc()?.children[i]?.classList.toggle(cls, on);
// Left-click cycle / right-click blocked-toggle on a task line's gutter ball — patches just that ball
// (and its row's struck-through state) rather than a full rebuild.
function ntCycleBall(i) {
  if (ntEditingLocked(ntSel)) return;
  const es = ntEditState; if (!es) return;
  ntSetStatus(es, i, ntNextStatus(ntLineStatus(es, i)));
  es.dirty = true;
  ntPatchBall(i);
  ntSchedule();
}
function ntToggleBlocked(i) {
  if (ntEditingLocked(ntSel)) return;
  const es = ntEditState; if (!es) return;
  const cur = ntLineStatus(es, i);
  ntSetStatus(es, i, cur === "blocked" ? "todo" : "blocked");
  es.dirty = true;
  ntPatchBall(i);
  ntSchedule();
}
function ntPatchBall(i) {
  const es = ntEditState; if (!es) return;
  const status = ntLineStatus(es, i);
  const closed = status === "done" || status === "cancelled";
  const btn = document.querySelector(`.ntball[data-i="${i}"]`); if (!btn) return;
  btn.className = `ntball ntb-${status}`;
  btn.setAttribute("aria-label", `Status: ${NT_STATUS_LABEL[status] || status}. Click to change.`);
  ntBlockClass(i, "done", closed);
}

// History belongs to the open note, not the surface rebuilt by structural edits.
function ntHistorySnapshot() {
  const es = ntEditState, el = document.activeElement;
  let focus = null;
  if (el?.id === 'nttitle') focus = {title: true, start: el.selectionStart, end: el.selectionEnd};
  else if (el && el === ntDoc()) { const sel = ntSelection(); if (sel) focus = {a: sel.a, f: sel.f}; }
  return {title: $('#nttitle').value, lines: es.lines.slice(), statuses: {...es.statuses}, focus};
}
function ntRememberSelection() {
  const es = ntEditState;
  if (es?.history) es.history[es.historyIndex].focus = ntHistorySnapshot().focus;
}
function ntRecordHistory() {
  const es = ntEditState;
  if (!es?.history || es.restoringHistory) return;
  const next = ntHistorySnapshot(), last = es.history[es.historyIndex];
  if (JSON.stringify([next.title,next.lines,next.statuses]) === JSON.stringify([last.title,last.lines,last.statuses])) return;
  es.history.splice(es.historyIndex + 1);
  es.history.push(next);
  if (es.history.length > 200) es.history.shift();
  es.historyIndex = es.history.length - 1;
}
function ntUndoRedo(redo) {
  const es = ntEditState;
  if (!es?.history || ntEditingLocked(ntSel)) return;
  const index = es.historyIndex + (redo ? 1 : -1);
  if (index < 0 || index >= es.history.length) return;
  const snapshot = es.history[index];
  es.historyIndex = index; es.restoringHistory = true;
  es.lines = snapshot.lines.slice(); es.statuses = {...snapshot.statuses}; es.dirty = true;
  const title = $('#nttitle'); title.value = snapshot.title; ntGrow(title);
  ntRenderLines();
  const f = snapshot.focus;
  if (f?.title) { title.focus(); title.setSelectionRange(Math.min(f.start || 0, title.value.length), Math.min(f.end || 0, title.value.length)); }
  else if (f?.a) {
    const clamp = p => { const i = Math.min(p.i, es.lines.length - 1); return {i, pos: Math.min(p.pos, ntBlockText(ntDoc().children[i]).length)}; };
    ntDoc().focus({preventScroll: true}); ntSetSelection(clamp(f.a), clamp(f.f));
  } else ntFocusLine(0, 0);
  ntSchedule(); es.restoringHistory = false;
}

// ---- autosave (idle 4s → 8s visible countdown → save; Ctrl/Cmd+S, Ctrl/Cmd+Enter, title blur save now)
function ntClearTimers() { clearTimeout(ntIdleT); clearTimeout(ntTickT); ntIdleT = ntTickT = null; }
function ntGrow(ta) { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; }
function ntSchedule() {
  ntRecordHistory();
  ntClearTimers();
  if (ntEditState) ntEditState.countdown = null;
  ntPaintSaveState(); ntPaintUpload();
  ntIdleT = setTimeout(() => ntCountdown(8), 4000);
}
function ntCountdown(n) {
  const s = ntEditState; if (!s) return;
  if (n <= 0) { s.countdown = null; return void ntSaveNow(); }
  s.countdown = n; ntPaintSaveState();
  ntTickT = setTimeout(() => ntCountdown(n - 1), 1000);
}
async function ntSaveNow() {
  const s = ntEditState; if (!s) return true;
  if (s.saving) {
    await s.savePromise;
    if (ntEditState !== s || s.error) return false;
    return ntSaveNow();
  }
  ntClearTimers();
  const titleEl = $("#nttitle"); if (!titleEl) return;
  ntPruneStatuses(s);
  const title = titleEl.value, content = s.lines.join("\n");
  const statusesJson = JSON.stringify(s.statuses);
  if (title === s.title0 && content === s.lines0.join("\n") && statusesJson === s.statuses0) { s.dirty = false; s.countdown = null; ntPaintSaveState(); return true; }
  s.saving = true; s.error=null; ntPaintSaveState();
  s.savePromise=ntPatch(s.biz, s.id, { title, content, statuses: JSON.parse(statusesJson),expected_revision:s.revision,expected_content_hash:s.content_hash });
  const r = await s.savePromise;
  if (ntEditState !== s) return;   // navigated away while saving
  s.saving = false;
  if (r) {
    s.title0 = title; s.lines0 = content.split('\n'); s.statuses0 = statusesJson; s.revision=r.revision; s.content_hash=r.content_hash;
    s.dirty = titleEl.value!==title || s.lines.join('\n')!==content || JSON.stringify(s.statuses)!==statusesJson; s.countdown = null;
    if(s.dirty)ntSchedule();
    ntPatchMeta(); ntPatchSidebar();
  }
  ntPaintSaveState();
  return !!r;
}
// revert to the last-saved snapshot, cancel any pending autosave, rebuild the lines column and repaint
// the save pill back to idle; a no-op (button stays disabled) while there's nothing unsaved to drop
function ntDiscard() {
  const s = ntEditState; if (!s || !s.dirty) return;
  ntClearTimers();
  s.dirty = false; s.saving = false; s.countdown = null;
  s.lines = s.lines0.slice(); s.statuses = JSON.parse(s.statuses0);
  const titleEl = $("#nttitle");
  if (titleEl) { titleEl.value = s.title0; ntGrow(titleEl); }
  ntRenderLines();
  ntRecordHistory();
  ntPaintSaveState(); ntPaintUpload();
}

// ---- selection / navigation / create
async function ntSelect(id) {
  if (id === ntSel) return;
  if(!await ntFlush())return;
  ntSetSel(id);
  ntRender();
}
const ntGoHome = () => ntSelect(null);
async function ntCreateNew() {
  if(!await ntFlush())return;
  const r = await ntCreate({title:'Daily'});
  if (!r) return;
  await ntFetch();
  ntSetSel(r.id);
  ntRender();
}

// ---- pin / rename / delete
function ntTogglePin(n) {
  const next = !n.pinned;
  n.pinned = next;
  ntRenderCurrent();
  return ntPatch(ntScope(), n.id, { pinned: next }).then(async r => {
    if (!r) { n.pinned = !next; ntRenderCurrent(); return; }
    await ntFetch();
    ntRenderCurrent();
  });
}
function ntRenameFlow(n) {
  const version = {expected_revision:n.revision,expected_content_hash:n.content_hash}, biz=ntScope();
  const body = `<input id="ntrenamein" class="inp" type="text" value="${esc(n.title || "")}" autocomplete="off" spellcheck="false">`;
  const commit = async (close, el) => {
    const inp = el.querySelector("#ntrenamein");
    const v = inp ? inp.value.trim() : "";
    close();
    const r = await ntPatch(biz, n.id, { title: v,...version });
    if (!r) return;
    await ntFetch();
    if (ntSel === n.id) {
      ntPatchMeta();
      const t = $("#nttitle"); if (t) { t.value = v; ntGrow(t); }
      if (ntEditState && ntEditState.id === n.id) {ntEditState.title0=v;ntEditState.revision=r.revision;ntEditState.content_hash=r.content_hash;}
    }
    ntPatchSidebar();
  };
  modal({
    title: "Rename page", body, width: 420,
    onOpen: el => { const inp = el.querySelector("#ntrenamein"); inp.select(); },
    actions: [
      { label: "Cancel", onClick: close => close() },
      { label: "Save", primary: true, onClick: commit },
    ],
  });
  const inp = document.getElementById("ntrenamein");
  if (inp) inp.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); const b = document.querySelector(".modal .mfoot .primary"); if (b) b.click(); } };
}
function ntDeleteFlow(id) {
  const n = ntById(id); if (!n) return;
  const version = {revision:n.revision,content_hash:n.content_hash};
  const title = n.title && n.title.trim() ? n.title : "Untitled";
  modal({
    title: "Archive page?",
    body: `<p>Archive "${esc(title)}"?</p>`,
    actions: [
      { label: "Cancel", onClick: close => close() },
      { label: "Archive", primary: true, onClick: async close => {
        close();
        const r = await ntDeleteReq(id,version); if (!r) return;
        let nextId = null;
        if (ntSel === id) {
          const capped = ntFilterNotes();
          const ordered = capped.filter(x => x.pinned).concat(capped.filter(x => !x.pinned));
          const idx = ordered.findIndex(x => x.id === id);
          if (idx >= 0) nextId = (ordered[idx + 1] || ordered[idx - 1] || {}).id || null;
        }
        await ntFetch();
        if (ntSel === id) ntSetSel(nextId);
        ntRender();
      } },
    ],
  });
}
function ntCtxMenu(x, y, id) {
  const n = ntById(id); if (!n) return;
  ctxMenu(x, y, [
    { label: "Rename", icon: ICO_PENCIL, onClick: () => ntRenameFlow(n) },
    { label: n.pinned ? "Unpin" : "Pin", icon: n.pinned ? NT_ICO_PINOFF : ICO_PIN, onClick: () => ntTogglePin(n) },
    { label: "Add to Context", icon: ICO_UPLOAD, onClick: () => ntDoUpload(n.id) },
    { label: "Archive", icon: aicon('archive','currentColor',16), onClick: () => ntDeleteFlow(n.id) },
  ]);
}


async function ntPickDay(date) {
  const existing = ntNotes.find(n => String(n.date || n.createdAt || "").slice(0, 10) === date);
  if (existing) return ntSelect(existing.id);
  const biz = ntScope(), n = await ntCreate({date});
  if (!n || ntScope() !== biz) return;
  await ntFetch();
  if (ntScope() === biz && ntActive()) ntSelect(n.id);
}
function ntUploadButton(n) {
  const dirty = ntEditState?.id === n.id && ntEditState.dirty;
  const busy = ntUploadingIds.has(n.id);
  if (ntScope() === '/root') {
    const attempt = ntGetUploadAttempt(n.id);
    const content = ntEditState?.id === n.id ? ntEditState.lines.join("\n") : n.content;
    const disabled = busy || !String(content || "").trim();
    const label = busy ? "Submitting…" : attempt ? "Retry same submission" : "Upload to filer";
    return `<button class="btn" id="ntupload" type="button"${disabled ? " disabled" : ""}>${ICO_UPLOAD}${label}</button>`;
  }
  const disabled = busy || (!dirty && (!n.content.trim() || (n.uploadId && !n.changedSinceUpload)));
  const label = busy ? "Submitting…" : n.uploadId ? "Send updated version" : "Add to Context";
  return `<button class="btn" id="ntupload" type="button"${disabled ? " disabled" : ""}>${ICO_UPLOAD}${label}</button>`;
}
function ntFilingHtml(n) {
  if (ntScope() === '/root') {
    return (n.filerSubmissions || []).map(submission => {
      const state = submission.state;
      const label = ({queued:"Queued", running:"Filing…", filed:"Filed", partial:"Partially filed", not_filed:"Not filed", failed:"Filing failed", cancelled:"Cancelled", cancelling:"Cancelling…"}[state] || "Status unavailable");
      const changed = submission.changedSinceSubmission || (ntEditState?.id === n.id && ntEditState.dirty);
      const summary = Object.keys(submission.summary || {}).length ? `<details class="ntfilingresult"><summary>View filing result</summary><pre>${esc(JSON.stringify(submission.summary, null, 2))}</pre><a class="ntmetalink" target="_blank" rel="noopener" href="/api/root/context-library/${encodeURIComponent(submission.uploadId)}">Open submitted snapshot and receipt</a></details>` : `<a class="ntmetalink" target="_blank" rel="noopener" href="/api/root/context-library/${encodeURIComponent(submission.uploadId)}">Open submitted snapshot</a>`;
      const prior = submission.previousReceipt ? `<details class="ntfilingresult"><summary>Previous attempt receipt (${esc(submission.previousReceipt.status || "saved")})</summary><pre>${esc(JSON.stringify(submission.previousReceipt.summary || {}, null, 2))}</pre></details>` : "";
      return `<div class="ntfilingrow"><span class="ntfilingstatus">${esc(label)} · ${esc(submission.uploadId.slice(0, 8))}${changed ? " · Changed since submission" : ""}${submission.filing?.error ? ` · ${esc(submission.filing.error)}` : ""}</span>${summary}${prior}${submission.retryable ? `<button class="ntmetalink ntretryfiler" type="button" data-upload-id="${esc(submission.uploadId)}">Retry</button>` : ""}</div>`;
    }).join("");
  }
  if (!n.uploadId) return "";
  const f = n.filing;
  const label = !f ? "Previous submission unavailable" : ({processed: "Filed", failed: "Filing failed", queued: "Queued for filing", running: "Filing…", waiting_signoff: "Awaiting approval", cancelled: "Filing cancelled", cancelling: "Cancelling filing…"}[f.state] || "Filing status unavailable");
  const changed = n.changedSinceUpload || (ntEditState?.id === n.id && ntEditState.dirty);
  return `<span class="ntfilingstatus">${esc(label)}${f?.error ? ` · ${esc(f.error)}` : ""}${changed ? " · Changed since submission" : ""}</span>
    ${f ? '<button class="ntmetalink" id="ntviewcontext" type="button">View in Context</button>' : ""}
    ${f?.retryable ? '<button class="ntmetalink" id="ntretry" type="button">Retry</button>' : ""}`;
}
function ntWireUpload() {
  const n = ntById(ntSel); if (!n) return;
  const button = $("#ntupload"); if (button) button.onclick = () => ntDoUpload(n.id);
  if (ntScope() === '/root') {
    document.querySelectorAll('.ntretryfiler').forEach(retry => retry.onclick = async () => {
      const submission = (n.filerSubmissions || []).find(item => item.uploadId === retry.dataset.uploadId);
      if (!submission || retry.disabled) return;
      retry.disabled = true;
      try {
        const result = await post(`/root/context-library/${encodeURIComponent(submission.uploadId)}/retry`, {
          expected_revision: submission.filing.revision, idempotency_key: newRequestId()});
        if (result?.error || result?.status === 'error') toast(result.error?.message || result.error || 'Could not retry this filing.');
        else { await ntFetch(); ntPaintUpload(); ntWatchFiling(); }
      } catch (error) { toast(error.message || 'Could not retry this filing.'); }
      finally { retry.disabled = false; }
    });
    return;
  }
  const view = $("#ntviewcontext"); if (view) view.onclick = async () => {
    const biz = ntScope();
    if (!await ntFlush() || ntScope() !== biz) return;
    state.ctx = "_dumps/" + n.uploadId + ".md";
    openTasks(false); goTab("context");
  };
  const retry = $("#ntretry"); if (retry) retry.onclick = async () => {
    retry.disabled = true;
    const biz = ntScope();
    try {
      const r = await post(`/biz/${biz}/context/retry`, {id:n.uploadId, expected_revision:n.filing.revision});
      if (r?.error) toast(r.error);
      else if (ntScope() === biz) {await ntFetch(); ntPaintUpload(); ntWatchFiling();}
    } catch (error) {toast(error.message);} finally {retry.disabled = false;}
  };
}
function ntPaintUpload() {
  if (!ntActive()) return;
  const n = ntById(ntSel); if (!n) return;
  const wrap = $("#ntuploadwrap"); if (wrap) wrap.innerHTML = ntUploadButton(n);
  const status = $("#ntfiling"); if (status) status.innerHTML = ntFilingHtml(n);
  ntWireUpload();
}
async function ntDoUpload(id) {
  if (ntUploadingIds.has(id)) return;
  const scope = ntScope();
  let locked = [], uncertain = false, attempt = null;
  ntUploadingIds.add(id); ntPaintUpload();
  try {
    attempt = ntScope() === '/root' ? ntGetUploadAttempt(id) : null;
    if (!attempt) {
      if (ntEditState && !await ntSaveNow()) return;
      if (ntScope() !== scope || ntEditState?.dirty) return;
      const n = ntById(id); if (!n) return;
      attempt = {idempotency_key: newRequestId(), document_id: id,
        expected_revision: n.revision, expected_content_hash: n.content_hash};
      if (scope === '/root') ntPutUploadAttempt(id, attempt);
    } else {
      // The exact original key and revision/hash survive a lost response, even if the editor was reloaded.
      ntUploadUncertainIds.add(id);
    }
    locked = [...document.querySelectorAll('#nttitle')];
    locked.forEach(el => el.readOnly = true);
    if (ntDoc()) ntSetEditable(ntDoc(), false);
    // The entry ID is already bound by the route. Keep it in local storage so
    // the retry can validate its identity, but send only the supported body.
    const request = {...attempt};
    delete request.document_id;
    const r = await post(`${ntEndpoint(scope)}/${encodeURIComponent(id)}/upload`, request);
    if (r?.error || r?.status === 'error') {
      if (scope === '/root') ntClearUploadAttempt(id);
      ntUploadUncertainIds.delete(id);
      toast(r.error?.message || r.error || 'Could not submit this entry.');
      return;
    }
    if (r?.status === "signoff_required") {
      if (scope === '/root') ntClearUploadAttempt(id);
      ntUploadUncertainIds.delete(id);
      toast("Context submission is awaiting approval."); return;
    }
    if (!['accepted', 'ok'].includes(r?.status)) throw new Error('The filing response was uncertain. Retry to resume the same submission.');
    if (scope === '/root') ntClearUploadAttempt(id);
    ntUploadUncertainIds.delete(id);
    if (ntScope() !== scope) return;
    await ntFetch();
    if (scope !== '/root') {
      const fresh = ntById(id), es = ntEditState;
      if (fresh && es?.id === id && es.biz === scope) { es.revision = fresh.revision; es.content_hash = fresh.content_hash; }
    }
    toast(scope === '/root' ? "Journal snapshot submitted to the filer." : "Submitted to Context — filing queued.");
    ntWatchFiling();
  } catch (error) {
    uncertain = scope === '/root' && Boolean(attempt);
    if (uncertain) ntUploadUncertainIds.add(id);
    toast(error.message || (scope === '/root' ? 'Upload response uncertain; retry to resume the same submission.' : 'Could not add note to Context.'));
  }
  finally {
    ntUploadingIds.delete(id);
    if (!uncertain) { locked.forEach(el => el.readOnly = false); if (ntDoc()) ntSetEditable(ntDoc(), !ntEditingLocked(id)); }
    ntPaintUpload();
  }
}
function ntUploadAttemptStorageKey(id) { return `nt-filer-upload:${id}`; }
function ntGetUploadAttempt(id) {
  try { const value = JSON.parse(localStorage.getItem(ntUploadAttemptStorageKey(id)) || 'null'); return value?.idempotency_key && value.document_id === id ? value : null; }
  catch (_) { return null; }
}
function ntPutUploadAttempt(id, value) { try { localStorage.setItem(ntUploadAttemptStorageKey(id), JSON.stringify(value)); } catch (_) {} }
function ntClearUploadAttempt(id) { try { localStorage.removeItem(ntUploadAttemptStorageKey(id)); } catch (_) {} }
function ntWatchFiling() {
  clearTimeout(ntFilingPoll);
  ntFilingPoll = null;
  const scope = ntScope();
  const active = ntNotes.some(n => scope === '/root'
    ? (n.filerSubmissions || []).some(sub => ['queued', 'running', 'cancelling'].includes(sub.filing?.state))
    : ["queued", "running", "waiting_signoff", "cancelling"].includes(n.filing?.state));
  if (!ntActive() || !active) return;
  ntFilingPoll = setTimeout(async () => {
    if (ntScope() !== scope || !ntActive()) return;
    try {await ntFetch(); ntPaintUpload();} catch (_) { /* retry on the next poll */ }
    ntWatchFiling();
  }, 3000);
}
