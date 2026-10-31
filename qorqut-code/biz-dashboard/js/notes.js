// Notes: free-text blocks and dash items, autosave, activity board and Context snapshots.
// LF separates blocks. U+2028 stores an explicit soft break inside a block, displayed as LF.
// Enter starts a block; Shift+Enter continues it. Pasted physical lines remain separate blocks.
const NT_SOFT_BREAK = "\u2028";
const ntUploadingIds = new Set();
let ntFilingPoll = null;

// notes view state
let ntSel = null;                   // selected note id, or null for the homepage
let ntNotes = [];                   // cache of the last /notes fetch
let ntSearch = "";                  // detail sidebar search (in-memory filter)

// remember what was open (a note id, or "" for the homepage) per business
const ntScope = () => state.scope === 'global' ? '/root' : state.biz;
const ntEndpoint = (scope = ntScope()) => scope === '/root' ? '/root/notes' : `/biz/${encodeURIComponent(scope)}/notes`;
const ntGlobalPage = () => state.scope === 'global' && state.globalTab === 'notes';
const ntActive = () => ntGlobalPage() || state.scope !== 'global' && tView === 'notes';
const ntKey = () => `nt:${ntScope()}`;
function ntSetSel(id) {
  ntSel = id;
  if (ntScope()) ls(ntKey(), id || "");
}
let ntRestoredBiz = null;           // biz slug whose saved selection we've already applied this session
let ntListOff = ls("ntlistoff") === "1";   // sidebar collapsed, persisted
let ntEditState = null;             // { id, biz, title0, lines0[], statuses0(json), lines[], statuses{}, dirty, saving, countdown }
let ntIdleT = null, ntTickT = null; // autosave idle/countdown timers




const NT_ICO_PANELLEFT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/></svg>`;
const NT_ICO_PANELLEFTCLOSE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/><path d="m16 15-3-3 3-3"/></svg>`;
const NT_ICO_HOME = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>`;
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
  if (Array.isArray(r)) ntNotes = r; else if (r && r.error) toast(r.error);
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
  if(ntGlobalPage() && innerWidth<=760)ntListOff=true;
  let justRestored = false;
  if(ntRestoredBiz!==ntScope()){ntNotes=[];ntSel=null;ntSearch="";}
  if (ntScope() && ntRestoredBiz !== ntScope()) {
    ntRestoredBiz = ntScope();
    const saved = ls(ntKey());
    // ntNotes isn't loaded yet here, so ntRender's not-found fallback would wipe an unverified id
    // right back to null; skip the synchronous render this once and let the fetch below paint it
    if (saved) { ntSel = saved; justRestored = true; }
  }
  if (!justRestored) ntRender();
  if (!ntScope()) return;
  const biz = ntScope();
  ntFetch().then(() => { if (ntActive() && ntScope() === biz) ntRender(); });
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
  const body = $(ntGlobalPage() ? "#global-notes-body" : "#tbody"); if (!body) return;
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
  return `<span class="ntmeta-words">${words} word${words === 1 ? "" : "s"}</span> <span id="ntfiling">${ntFilingHtml(n)}</span>`;
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
    <div class="ntrail">
      <button id="nttoggle" class="nticon" type="button" aria-label="${ntListOff ? "Show page list" : "Hide page list"}" data-tip="${ntListOff ? "Show page list" : "Hide page list"}">${ntListOff ? NT_ICO_PANELLEFT : NT_ICO_PANELLEFTCLOSE}</button>
      <button id="nthomebtn" class="nticon" type="button" aria-label="Notes home" data-tip="Notes home">${NT_ICO_HOME}</button>
    </div>
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
  const titleEl = $("#nttitle");
  titleEl.value = n.title || ""; ntGrow(titleEl);
  ntRenderLines();

  titleEl.oninput = () => { ntGrow(titleEl); ntEditState.dirty = true; ntSchedule(); };
  titleEl.onkeydown = e => {
    if (e.key === "Enter") { e.preventDefault(); titleEl.blur(); }
    else if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) { e.preventDefault(); ntSaveNow(); }
  };
  titleEl.onblur = event => { if (ntEditState && ntEditState.dirty && !event.relatedTarget?.closest('#nterror, #ntdiscard')) ntSaveNow(); };

  const toggleBtn = root.querySelector("#nttoggle");
  toggleBtn.onclick = () => {
    ntListOff = !ntListOff; ls("ntlistoff", ntListOff ? "1" : "0");
    const side = root.querySelector("#ntside"); if (side) side.classList.toggle("off", ntListOff);
    const label = ntListOff ? "Show page list" : "Hide page list";
    toggleBtn.setAttribute("data-tip", label); toggleBtn.setAttribute("aria-label", label);
    toggleBtn.innerHTML = ntListOff ? NT_ICO_PANELLEFT : NT_ICO_PANELLEFTCLOSE;
  };
  root.querySelector("#nthomebtn").onclick = () => ntGoHome();
  root.querySelector("#ntnew2").onclick = () => ntCreateNew();
  root.querySelector("#ntdel").onclick = () => ntDeleteFlow(n.id);
  root.querySelector("#ntdiscard").onclick = () => ntDiscard();

  const search = root.querySelector("#ntsearch");
  let searchT = null;
  search.oninput = () => { clearTimeout(searchT); searchT = setTimeout(() => { ntSearch = search.value; ntPatchSidebar(); }, 80); };
  ntWireListRows(root.querySelector("#ntlist"));

  // click anywhere in the editor's empty space (not on a line/ball) focuses the end of the last line
  const wrap = root.querySelector("#ntlinewrap");
  wrap.onclick = e => {
    if (e.target.closest(".ntlinein") || e.target.closest(".ntball")) return;
    const es = ntEditState; if (!es) return;
    let j = es.lines.length - 1; while (j > 0 && ntIsRuleAt(es, j)) j--;
    ntFocusLine(j, ntDisp(es, j).length);
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

function ntLineRowHtml(es, i) {
  if (ntIsRuleAt(es, i)) return `<div class="ntline ntrule" data-i="${i}"><div class="ntrulehr"></div></div>`;
  const raw = es.lines[i];
  const isTask = ntIsTaskLine(raw);
  const { meta } = ntParseRaw(raw);
  const status = isTask ? ntLineStatus(es, i) : null;
  const closed = status === "done" || status === "cancelled";
  const cls = ["ntline"];
  if (isTask) cls.push("nttask");
  if (meta.bold) cls.push("bold");
  if (closed) cls.push("done");
  const ball = isTask ? `<span class="ntballwrap"><button type="button" class="ntball ntb-${esc(status)}" data-i="${i}" aria-label="Status: ${esc(NT_STATUS_LABEL[status] || status)}. Click to change."><span class="ntdot"></span></button></span>` : "";
  return `<div class="${cls.join(" ")}" data-i="${i}">${ball}<textarea class="ntlinein" data-i="${i}" rows="1" spellcheck="false" autocomplete="off" autocorrect="off" autocapitalize="off"></textarea></div>`;
}
// Plain journal prose can use one native editing surface, which lets the browser select
// across block boundaries. Structured lines keep the existing per-line editor so task
// markers, status balls, bold metadata, and dividers retain their current behavior.
const ntCanUseMultilineSurface = es => es.lines.every((raw, i) => !ntIsTaskLine(raw) && !ntIsRuleAt(es, i) && !ntParseRaw(raw).meta.bold);
function ntHandleMultilineInput(ta) {
  const es = ntEditState; if (!es) return;
  es.lines = ta.value.replace(/\r\n?/g, "\n").split("\n");
  es.dirty = true;
  ntGrowLine(ta);
  ntPaintOverlay();
  ntSchedule();
}
function ntRenderLines() {
  const wrap = $("#ntlines"); if (!wrap) return;
  const es = ntEditState; if (!es) return;
  if (ntCanUseMultilineSurface(es)) {
    wrap.innerHTML = `<div class="ntline ntmultiline"><textarea class="ntlinein" data-i="0" rows="8" spellcheck="false" autocomplete="off" autocorrect="off" autocapitalize="off" aria-label="Journal entry"></textarea></div>`;
    const ta = wrap.querySelector(".ntlinein");
    ta.value = es.lines.map((_, i) => ntDisp(es, i)).join("\n");
    ntGrowLine(ta);
    ta.oninput = () => ntHandleMultilineInput(ta);
    ta.onkeydown = e => {
      if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S" || e.key === "Enter")) {
        e.preventDefault(); ntSaveNow();
      }
    };
    ta.onfocus = () => ntPaintOverlay();
    ta.onblur = () => setTimeout(ntPaintOverlay, 0);
    ntPaintOverlay();
    return;
  }
  wrap.innerHTML = es.lines.map((_, i) => ntLineRowHtml(es, i)).join("");
  wrap.querySelectorAll(".ntlinein").forEach(ta => {
    const i = Number(ta.dataset.i);
    ta.value = ntDisp(es, i);          // values are set via .value, never interpolated into the HTML
    ntGrowLine(ta);
    ta.oninput = () => ntHandleInput(i, ta.value, ta.selectionStart);
    ta.onkeydown = e => ntHandleKeydown(i, e);
    ta.onpaste = e => {
      if (ta.readOnly) {e.preventDefault(); return;}
      const pasted = e.clipboardData?.getData("text/plain").replace(/\r\n?/g, "\n");
      if (!pasted?.includes("\n")) return;
      e.preventDefault();
      const parts = pasted.split("\n"), display = ntDisp(es, i);
      const first = ntToRaw(es, display.slice(0, ta.selectionStart) + parts[0], i);
      const tail = display.slice(ta.selectionEnd).replaceAll("\n", NT_SOFT_BREAK);
      const last = parts[parts.length - 1] + tail;
      ntCommit([...es.lines.slice(0, i), first, ...parts.slice(1, -1), last, ...es.lines.slice(i + 1)],
        {index:i + parts.length - 1, pos:parts[parts.length - 1].length});
    };
    ta.setAttribute("aria-label", ntIsTaskLine(es.lines[i]) ? "Dash item" : "Free text");
    ta.onfocus = () => ntPaintOverlay();
    ta.onblur = () => setTimeout(ntPaintOverlay, 0);   // let the next element take focus first
  });
  wrap.querySelectorAll(".ntball").forEach(b => {
    const i = Number(b.dataset.i);
    b.onclick = e => { e.stopPropagation(); e.preventDefault(); ntCycleBall(i); };
    b.oncontextmenu = e => { e.preventDefault(); e.stopPropagation(); ntToggleBlocked(i); };
  });
  ntPaintOverlay();
}
function ntPaintOverlay() {
  const es = ntEditState; if (!es) return;
  const ov = $("#ntoverlay"); if (!ov) return;
  const empty = es.lines.length <= 1 && !es.lines[0];
  const focused = document.activeElement && document.activeElement.classList && document.activeElement.classList.contains("ntlinein");
  ov.hidden = !(empty && !focused);
}
function ntFocusLine(index, pos) {
  const wrap = $("#ntlines"); if (!wrap) return;
  const ta = wrap.querySelector(`.ntlinein[data-i="${index}"]`); if (!ta) return;
  ta.focus();
  const p = Math.min(pos, ta.value.length);
  ta.setSelectionRange(p, p);
}
// Commit a structural change (split/merge/divider/auto-convert) — full rebuild of the lines column,
// caret restored precisely afterward (the "rebuild only what changed, but a full rebuild on structural
// changes is fine if focus is restored" allowance in the spec).
function ntCommit(newLines, focus) {
  const es = ntEditState; if (!es) return;
  es.lines = newLines; es.dirty = true;
  ntRenderLines();
  if (focus) ntFocusLine(focus.index, focus.pos);
  ntSchedule();
}

function ntHandleInput(i, value, selStart) {
  const es = ntEditState; if (!es) return;
  const wasTask = ntIsTaskLine(es.lines[i]);
  const converted = !wasTask && value.startsWith("-");
  const newLines = es.lines.slice();
  newLines[i] = ntToRaw(es, value, i);
  es.lines = newLines; es.dirty = true;
  if (converted) {
    // structural: the row gains a gutter ball, and the auto-convert swallows the "-" just typed.
    // selStart was captured by the oninput handler (ta.selectionStart) BEFORE this rebuild tears the
    // textarea down, so the caret restores to exactly where it was, minus the swallowed "-".
    const caret = Math.max(0, (selStart != null ? selStart : value.length) - 1);
    ntRenderLines();
    ntFocusLine(i, caret);
  } else {
    const ta = document.querySelector(`.ntlinein[data-i="${i}"]`); if (ta) ntGrowLine(ta);
    ntPaintOverlay();
  }
  ntSchedule();
}

function ntHandleKeydown(i, e) {
  if (e.currentTarget.readOnly) return;
  const es = ntEditState; if (!es) return;
  const el = e.currentTarget;
  const start = el.selectionStart, end = el.selectionEnd;
  const dtext = ntDisp(es, i);
  const atStart = start === 0 && end === 0;
  const atEnd = start === dtext.length && end === dtext.length;
  const mod = e.ctrlKey || e.metaKey;

  if (mod && (e.key === "s" || e.key === "S")) { e.preventDefault(); ntSaveNow(); return; }
  if (mod && e.key === "Enter") { e.preventDefault(); ntSaveNow(); return; }
  if (mod && (e.key === "b" || e.key === "B")) { e.preventDefault(); ntToggleBold(i); return; }
  if (mod && (e.key === "=" || e.key === "+")) {
    e.preventDefault();
    if (dtext.trim() === "") {
      const hasNext = i < es.lines.length - 1;
      ntCommit([...es.lines.slice(0, i), NT_DIVIDER, ...(hasNext ? [] : [""]), ...es.lines.slice(i + 1)], { index: i + 1, pos: 0 });
    } else {
      const isLast = i === es.lines.length - 1;
      const insert = isLast ? [NT_DIVIDER, ""] : [NT_DIVIDER];
      ntCommit([...es.lines.slice(0, i + 1), ...insert, ...es.lines.slice(i + 1)], { index: i + 2, pos: 0 });
    }
    return;
  }
  if (e.key === "Enter" && e.shiftKey && !mod) {
    e.preventDefault();
    const next = es.lines.slice(), status = ntLineStatus(es, i);
    next[i] = ntToRaw(es, dtext.slice(0, start) + "\n" + dtext.slice(end), i);
    es.lines = next;
    if (ntIsTaskLine(next[i])) ntSetStatus(es, i, status);
    ntCommit(next, {index:i, pos:start + 1});
    return;
  }
  if (e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
    e.preventDefault();
    const off = es.lines[i].length - dtext.length;
    const head = es.lines[i].slice(0, start + off);
    const tail = dtext.slice(end).replaceAll("\n", NT_SOFT_BREAK);
    ntCommit([...es.lines.slice(0, i), head, tail, ...es.lines.slice(i + 1)], { index: i + 1, pos: 0 });
    return;
  }








  if (e.key === "Backspace" && atStart) {
    if (ntIsTaskLine(es.lines[i])) {
      e.preventDefault();
      const newLines = es.lines.slice(); newLines[i] = dtext.replaceAll("\n", NT_SOFT_BREAK);
      ntCommit(newLines, { index: i, pos: 0 });
      return;
    }
    if (i > 0) {
      e.preventDefault();
      if (ntIsRuleAt(es, i - 1)) { ntCommit([...es.lines.slice(0, i - 1), ...es.lines.slice(i)], { index: i - 1, pos: 0 }); return; }
      const prevD = ntDisp(es, i - 1);
      ntCommit([...es.lines.slice(0, i - 1), es.lines[i - 1] + dtext.replaceAll("\n", NT_SOFT_BREAK), ...es.lines.slice(i + 1)], { index: i - 1, pos: prevD.length });
      return;
    }
  }
  if (e.key === "Delete" && atEnd && i < es.lines.length - 1) {
    e.preventDefault();
    if (ntIsRuleAt(es, i + 1)) { ntCommit([...es.lines.slice(0, i + 1), ...es.lines.slice(i + 2)], { index: i, pos: dtext.length }); return; }
    ntCommit([...es.lines.slice(0, i), es.lines[i] + ntDisp(es, i + 1).replaceAll("\n", NT_SOFT_BREAK), ...es.lines.slice(i + 2)], { index: i, pos: dtext.length });
    return;
  }
  if (e.key === "ArrowUp" && atStart && i > 0) {
    e.preventDefault();
    let j = i - 1; while (j >= 0 && ntIsRuleAt(es, j)) j--;
    if (j >= 0) ntFocusLine(j, ntDisp(es, j).length);
    return;
  }
  if (e.key === "ArrowDown" && atEnd && i < es.lines.length - 1) {
    e.preventDefault();
    let j = i + 1; while (j < es.lines.length && ntIsRuleAt(es, j)) j++;
    if (j < es.lines.length) ntFocusLine(j, 0);
  }
}

// Ctrl/Cmd+B: line-level bold, stored as the invisible "⁣b⁣ " token — the displayed text is unchanged,
// so this patches the wrapper's class in place instead of a full rebuild (no caret disruption).
function ntToggleBold(i) {
  const es = ntEditState; if (!es) return;
  const cur = ntParseRaw(es.lines[i]);
  const meta = { bold: !cur.meta.bold };
  const token = ntBuildMetaToken(meta);
  const marker = ntIsTaskLine(es.lines[i]) ? "- " : "";
  const newLines = es.lines.slice();
  newLines[i] = cur.display === "" && !token ? "" : `${marker}${token}${cur.display.replaceAll("\n", NT_SOFT_BREAK)}`;
  es.lines = newLines; es.dirty = true;
  const ta = document.querySelector(`.ntlinein[data-i="${i}"]`);
  const row = ta ? ta.closest(".ntline") : null;
  if (row) row.classList.toggle("bold", meta.bold);
  ntSchedule();
}
// Left-click cycle / right-click blocked-toggle on a task line's gutter ball — patches just that ball
// (and its row's struck-through state) rather than a full rebuild.
function ntCycleBall(i) {
  if (ntUploadingIds.has(ntSel)) return;
  const es = ntEditState; if (!es) return;
  ntSetStatus(es, i, ntNextStatus(ntLineStatus(es, i)));
  es.dirty = true;
  ntPatchBall(i);
  ntSchedule();
}
function ntToggleBlocked(i) {
  if (ntUploadingIds.has(ntSel)) return;
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
  const row = btn.closest(".ntline"); if (row) row.classList.toggle("done", closed);
}

// ---- autosave (idle 4s → 8s visible countdown → save; Ctrl/Cmd+S, Ctrl/Cmd+Enter, title blur save now)
function ntClearTimers() { clearTimeout(ntIdleT); clearTimeout(ntTickT); ntIdleT = ntTickT = null; }
function ntGrow(ta) { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; }
function ntGrowLine(ta) { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; }
function ntSchedule() {
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
  const r = await ntCreate({});
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
  if(ntScope()==='/root')return "";
  const dirty = ntEditState?.id === n.id && ntEditState.dirty;
  const busy = ntUploadingIds.has(n.id);
  const disabled = busy || (!dirty && (!n.content.trim() || (n.uploadId && !n.changedSinceUpload)));
  const label = busy ? "Submitting…" : n.uploadId ? "Send updated version" : "Add to Context";
  return `<button class="btn" id="ntupload" type="button"${disabled ? " disabled" : ""}>${ICO_UPLOAD}${label}</button>`;
}
function ntFilingHtml(n) {
  if(ntScope()==='/root')return "";
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
  if(ntScope()==='/root')return;
  if (ntUploadingIds.has(id)) return;
  const biz = ntScope();
  let locked = [];
  ntUploadingIds.add(id); ntPaintUpload();
  try {
    if (ntEditState && !await ntSaveNow()) return;
    if (ntScope() !== biz || ntEditState?.dirty) return;
    const n = ntById(id); if (!n) return;
    locked = [...document.querySelectorAll('#nttitle, .ntlinein')];
    locked.forEach(el => el.readOnly = true);
    const r = await post(`/biz/${biz}/notes/${encodeURIComponent(id)}/upload`, {
      expected_revision:n.revision, expected_content_hash:n.content_hash});
    if (r?.error) {toast(r.error); return;}
    if (r?.status === "signoff_required") {toast("Context submission is awaiting approval."); return;}
    if (ntScope() !== biz) return;
    await ntFetch();
    const fresh = ntById(id), es = ntEditState;
    if (fresh && es?.id === id && es.biz === biz && es.revision === n.revision) {
      es.revision = fresh.revision; es.content_hash = fresh.content_hash;
    }
    toast("Submitted to Context — filing queued.");
    ntWatchFiling();
  } catch (error) {toast(error.message || "Could not add note to Context.");}
  finally {locked.forEach(el => el.readOnly = false); ntUploadingIds.delete(id); ntPaintUpload();}
}
function ntWatchFiling() {
  clearTimeout(ntFilingPoll);
  if(ntScope()==='/root')return;
  const biz = ntScope();
  if (!ntActive() || !ntNotes.some(n => ["queued", "running", "waiting_signoff", "cancelling"].includes(n.filing?.state))) return;
  ntFilingPoll = setTimeout(async () => {
    if (ntScope() !== biz || !ntActive()) return;
    try {await ntFetch(); ntPaintUpload();} catch (_) { /* retry on the next poll */ }
    ntWatchFiling();
  }, 3000);
}
