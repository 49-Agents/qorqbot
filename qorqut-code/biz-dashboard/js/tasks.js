























const T_ORDER = ["open", "doing", "waiting", "done"];          // the left-click cycle
const T_CLOSED = ["done", "dropped"];
const T_STATES = ["open", "doing", "waiting", "blocked", "done", "dropped"];   // the detail's status picker
const T_LABEL = { open: "Open", doing: "Doing", waiting: "Waiting", done: "Done", blocked: "Blocked", dropped: "Dropped" };
const T_TAGS = ["operations", "coding", "research", "marketing", "needs-owner"];
const T_DEPTS = ["marketing", "accounting", "data-science", "code", "general", "onbashlyk"];
// department -> the tag that files a task there (the reverse of core's TAG_DEPT); general has none
const T_DEPT_TAG = {};
Object.keys(TAG_DEPT).forEach(tag => { if (!T_DEPT_TAG[TAG_DEPT[tag]]) T_DEPT_TAG[TAG_DEPT[tag]] = tag; });
const T_DONE_HOLD = 550;
const T_DAY = 86400000;

const SVG_TPLUS = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5v14"/></svg>`;
const SVG_TMORE = `<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>`;
const SVG_TTAG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r=".5" fill="currentColor"/></svg>`;
const SVG_TSEARCH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>`;
const SVG_TINBOX = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>`;

const tShort = o => String(o || "").replace(/^(agent|human|system):/, "") || "unassigned";

const tDay = s => { const [y, m, d] = String(s).slice(0, 10).split("-").map(Number); return new Date(y, (m || 1) - 1, d || 1).getTime(); };
const tToday = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime(); };
const tDays = due => Math.round((tDay(due) - tToday()) / T_DAY);
const tClosed = t => T_CLOSED.indexOf(t.status) >= 0;
// a closed row sinks to the bottom of its section, except while its completion is still being held in place
const tSunk = t => tClosed(t) && !tCompleting.has(t.id);
const tNext = s => { const i = T_ORDER.indexOf(s); return i < 0 ? "open" : T_ORDER[(i + 1) % T_ORDER.length]; };
function tDept(t) {
  for (const x of (t.tags || [])) if (TAG_DEPT[x]) return TAG_DEPT[x];
  return "general";
}

let tSearch = "";
let tAdding = null;                 // department key with an open inline add row
let tPainting = false;              // rows are being replaced: a blur fired by that detachment is not a commit
let tPaintSig = null;               // signature of what's currently on screen; a background refresh that still
                                     // matches it skips the rebuild entirely (no flicker, no lost hover/tooltip)
const tCompleting = new Set();      // ids shown done but still in place
const tDoneT = new Map();           // id -> the timer that sends the done write once the hold is over
let tGen = 0;                       // bumped when a write starts; a refresh that began before it is stale when it lands
let tLastBiz = null, tSoonT = null, tInside = false, tSearchT = null;
state.tasks = [];

// ---------------------------------------------------------------- view (tabs)
const T_VIEWS = ["tasks", "journal", "notes", "uploads", "activity"];
const T_VIEW_LABEL = { tasks: "Tasks", activity: "Activity", journal: "Journal", notes: "Notes", uploads: "Uploads" };
const T_VIEW_ICON = {
  tasks: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 10.656V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h12.344"></path><path d="m9 11 3 3L22 4"></path></svg>`,
  journal: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.67 19a2 2 0 0 0 1.416-.588l6.154-6.172a6 6 0 0 0-8.49-8.49L5.586 9.914A2 2 0 0 0 5 11.328V18a1 1 0 0 0 1 1z"></path><path d="M16 8 2 22"></path><path d="M17.5 15H9"></path></svg>`,
  notes: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13.4 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7.4"></path><path d="M2 6h4"></path><path d="M2 10h4"></path><path d="M2 14h4"></path><path d="M2 18h4"></path><path d="M21.378 5.626a1 1 0 1 0-3.004-3.004l-5.01 5.012a2 2 0 0 0-.506.854l-.837 2.87a.5.5 0 0 0 .62.62l2.87-.837a2 2 0 0 0 .854-.506z"></path></svg>`,
  uploads: ICO_UPLOAD,  // defined in core.js: the same glyph leads every upload button
  activity: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"></path></svg>`,
};
let tView = ls("tview") || "tasks";
if (T_VIEWS.indexOf(tView) < 0) tView = "tasks";

// activity (log) view state
let tLog = [];
let tLogSig = null;                 // signature of what's on screen for the log list; skips a no-op rebuild
let tLogLoaded = false;             // has this business's log been fetched at least once since the view opened

// journal view state (jrSel/jrMemos/jrSearch/...) lives in js/journal.js, which owns the whole
// journal tab; this file only calls into it at tab-switch/business-change points (see tSetView, tInit).
// notes view state (ntSel/ntNotes/ntSearch/...) lives in js/notes.js the same way.
// uploads view state (upForm/upDone/...) lives in js/uploads.js; it has no editor, so nothing to flush.

const tPanelOpen = () => { const w = $("#taskswrap"); return !!w && w.classList.contains("open"); };
const taskById = id => (state.tasks || []).find(t => t.id === id);
const tBiz = () => (state.bizs || []).find(x => x.slug === state.biz) || {};
// the business owner, as biztask spells it: `human:<owner from business.yaml>` (server's owner_actor fallback)
const tHuman = () => "human:" + (tBiz().owner || "dashboard");
const tAgents = () => (tBiz().agents || []).map(a => "agent:" + a);
const tMeta = o => (tBiz().agent_meta || {})[tShort(o)];

// A repaint replaces every row, so it must not fire while a dropdown is open: it would lose its anchor.
function tEditingNow() { return !!document.querySelector(".menu"); }

function openTasks(on) {
  const w = $("#taskswrap"); if (!w) return;
  w.classList.toggle("open", !!on);
  ls("tasks", on ? "1" : "0");
  if (on) refreshTasks();
}

// ---------------------------------------------------------------- data
function refreshTasksSoon() { clearTimeout(tSoonT); tSoonT = setTimeout(refreshTasks, 500); }



async function refreshTasks() {
  if (!state.biz) return;
  const gen = tGen, biz = state.biz;
  const r = await api(`/biz/${biz}/tasks?all=1`);
  if (gen !== tGen || biz !== state.biz) return;
  if (Array.isArray(r)) {
    state.tasks = r;
    // a row mid-completion keeps its local done look until the write lands
    r.forEach(t => { if (tCompleting.has(t.id)) t.status = "done"; });
  } else if (r && r.error) toast(r.error);
  // the click that caused this refresh already patched its row in place: if the server agrees with
  // what's already on screen, tearing the whole list down and rebuilding it would only flicker for nothing
  if (tVisSig() === tPaintSig) return;
  paintTasks();
}

function tCount() { return (state.tasks || []).filter(t => !tClosed(t)).length; }

// a write always repaints, so let go of whatever row control is focused first
function tRelease() {
  const a = document.activeElement;
  if (a && a.closest && a.closest("#taskspanel") && a.id !== "taddin" && a.id !== "tsearch") try { a.blur(); } catch (e) {}
}

async function twrite(id, body) {
  tGen++;
  const r = await post(`/biz/${state.biz}/task/${encodeURIComponent(id)}`, body);
  if (r && r.error) { toast(r.error); await refreshTasks(); return null; }
  tRelease();
  await refreshTasks();
  if (typeof refreshSideSoon === "function") refreshSideSoon();
  return r;
}

async function tdone(id) {
  tGen++;
  const r = await post(`/biz/${state.biz}/task/${encodeURIComponent(id)}/done`, {});
  if (r && r.error) { toast(r.error); await refreshTasks(); return null; }
  tRelease();
  await refreshTasks();
  if (typeof refreshSideSoon === "function") refreshSideSoon();
  return r;
}

async function tadd(fields) {
  tGen++;
  const r = await post(`/biz/${state.biz}/task`, fields);
  if (r && r.error) { toast(r.error); return null; }
  await refreshTasks();
  if (typeof refreshSideSoon === "function") refreshSideSoon();
  return r;
}

// ---------------------------------------------------------------- search
function tMatch(t) {
  const q = tSearch.trim().toLowerCase();
  return !q || String(t.title).toLowerCase().includes(q) || (t.tags || []).some(x => x.includes(q))
    || (t.notes || []).some(n => String(n.text || "").toLowerCase().includes(q));
}

const tTagColor = x => x === "needs-owner" ? "var(--accent)" : dept(TAG_DEPT[x]).color;
const tDotHtml = s => `<span class="sdot ${esc(s)}"></span>`;

// a cheap fingerprint of every visible row's rendered fields (order matters: it doubles as a position
// check, since a sunk task lands in a different spot than an open one). `state.biz` is part of it too:
// two empty task lists (no business picked vs. a business with nothing in it) render different copy.
function tVisSig() {
  return JSON.stringify([state.biz, (state.tasks || []).filter(tMatch).map(t =>
    [t.id, t.status, t.owner || "", t.title, t.due || "", (t.tags || []).join(","), tSunk(t)])]);
}

// ---------------------------------------------------------------- assignee
// none | agent (one of the business's agents) | human (the owner) | other (any other actor)
function tWhoKind(o) {
  if (!o) return "none";
  if (o === tHuman()) return "human";
  if (tAgents().indexOf(o) >= 0) return "agent";
  return "other";
}
function tWhoLeft(o) {
  const ag = tAgents(); if (!ag.length) return null;
  const i = ag.indexOf(o);
  if (i < 0) return ag[0];
  return i + 1 < ag.length ? ag[i + 1] : "";
}
const tWhoRight = o => (o === tHuman() ? "" : tHuman());

// the dot: dashed ring unassigned, the agent's department colour (plain), cream for the owner (may carry
// their initial), gray for anyone else
function tWhoDot(o) {
  const k = tWhoKind(o);
  const c = k === "agent" ? dept((tMeta(o) || {}).department).color : k === "human" ? "var(--accent)" : "var(--fg-3)";
  const initial = k === "human" ? esc(tShort(o).slice(0, 1).toUpperCase()) : "";
  return `<span class="wd ${k}" style="--wc:${c}">${initial}</span>`;
}
const tWhoInner = o => `<span class="who inl ${tWhoKind(o)}">${tWhoDot(o)}</span>`;
function tWho(t) {
  const o = t.owner || "";
  return `<button class="who ${tWhoKind(o)}" type="button" aria-label="Assignee: ${esc(o || "unassigned")}">${tWhoDot(o)}</button>`;
}
function tWhoTip(t) {
  const o = t.owner || "", k = tWhoKind(o);
  const who = k === "none" ? "Unassigned" : `${esc(tShort(o))} <span class="k">${k === "human" ? "owner" : k === "agent" ? "agent" : o.split(":")[0]}</span>`;
  const left = tWhoLeft(o);
  const hints = [left == null ? "no agents" : `click: ${left ? tShort(left) : "unassign"}`,
    `right-click: ${k === "human" ? "unassign" : tShort(tHuman())}`];
  return `${who}<br><span class="k">${hints.join(" · ")}</span>`;
}
function tWhoItems() {
  const items = [{ value: "", label: "Unassigned", html: `<span class="towner">${tWhoInner("")}<span class="tn">Unassigned</span></span>` }];
  tAgents().forEach(o => items.push({ value: o, label: o, html: `<span class="towner">${tWhoInner(o)}<span class="tn">${esc(tShort(o))}</span><span class="tsub">agent</span></span>` }));
  if (tBiz().owner) items.push({ value: tHuman(), label: tHuman(), html: `<span class="towner">${tWhoInner(tHuman())}<span class="tn">${esc(tShort(tHuman()))}</span><span class="tsub">owner</span></span>` });
  return items;
}



function tDue(t) {
  if (!t.due || tClosed(t)) return "";
  const n = tDays(t.due);
  const text = n === 0 ? "today" : n > 0 ? `${n}d` : `-${-n}d`;
  const tone = t.status === "blocked" || n < 0 ? "over" : n <= 1 ? "soon" : "far";
  return `<span class="tdl ${tone}" title="Due ${esc(t.due)}">${text}</span>`;
}

function tBall(t) {
  const s = t.status, label = T_LABEL[s] || s;
  return `<button class="ball ${esc(s)}" type="button" title="${esc(label)} — click to change, right-click to block"
    aria-label="Status: ${esc(label)}. Click to change."><span class="core"></span></button>`;
}

// the deadline slot is reserved on every row in every section, whether or not any task anywhere has a
// due date, so assignee/status balls line up identically across sections
function tRow(t) {
  return `<div class="trow ${esc(t.status)}${tClosed(t) ? " off" : ""}" data-id="${esc(t.id)}">${tDue(t) || `<span class="tdl"></span>`}${tWho(t)}${tBall(t)}<p class="ttl">${esc(t.title)}</p><button class="tmore" type="button" title="Details" aria-label="Task details">${SVG_TMORE}</button></div>`;
}

const tAddRow = key => `<div class="tadd" data-b="${esc(key)}"><span class="tdl"></span><span class="who ph"><span class="wd none"></span></span><span class="ball ph"><span class="core"></span></span><input id="taddin" placeholder="Task name…" autocomplete="off" spellcheck="false"></div>`;

function tSection(key, rows) {
  const d = dept(key);
  const open = rows.filter(t => !tSunk(t)), sunk = rows.filter(tSunk);
  // two ways to add to this section: the heading text/count itself, or the + — both call tOpenAdd(key)
  return `<section class="tsec" data-b="${esc(key)}">
    <div class="tsh">
      <button class="tshbtn" type="button" title="Add task to ${esc(d.label)}" aria-label="Add task to ${esc(d.label)}"><h2>${esc(d.label)}</h2><span class="c">${open.length}</span></button>
      <button class="tplus" type="button" title="Add task to ${esc(d.label)}" aria-label="Add task to ${esc(d.label)}">${SVG_TPLUS}</button>
    </div>
    <div class="tsi">${open.concat(sunk).map(t => tRow(t)).join("")}${tAdding === key ? tAddRow(key) : ""}</div>
  </section>`;
}

function tTabs() {
  return `<div class="ttabs"><div class="tind" id="tind" hidden></div>${T_VIEWS.map(v => `<button class="ttab${v === tView ? " on" : ""}" type="button" data-v="${v}" title="${T_VIEW_LABEL[v]}" aria-label="${T_VIEW_LABEL[v]}">${T_VIEW_ICON[v]}</button>`).join("")}</div>`;
}

function tHead() {
  // the journal/notes tabs supply their own header (homepage title/subtitle/Create, or the detail's
  // own sidebar controls) in #tbody, so the generic ttop (title + tasks-only stats/search) sits out
  const ownHead = tView === "journal" || tView === "notes" || tView === "uploads";
  return `<div class="thead${ownHead ? " nott" : ""}">
    ${tTabs()}
    ${ownHead ? "" : `<div class="ttop">
      <div><h1>${T_VIEW_LABEL[tView]}</h1>${tView === "tasks" ? `<div class="tstats" id="tstats"></div>` : ""}</div>
      ${tView === "tasks" ? `<label class="tsearch">${SVG_TSEARCH}<input id="tsearch" type="text" placeholder="Search by title, tag or content..." autocomplete="off" spellcheck="false"></label>` : ""}
    </div>`}
  </div><div class="tbody" id="tbody"></div>`;
}

function tStats() {
  const all = state.tasks || [], today = tToday();
  const doing = all.filter(t => t.status === "doing").length;
  const over = all.filter(t => t.due && !tClosed(t) && tDay(t.due) < today).length;
  return `<span><b>${all.length}</b> ${all.length === 1 ? "task" : "tasks"}</span><i>·</i><span><b class="blue">${doing}</b> in progress</span>`
    + (over ? `<i>·</i><span><b class="over">${over}</b> overdue</span>` : "");
}

function paintTasks() {
  const p = $("#taskspanel"); if (!p) return;
  const c = $("#tcount"); if (c) { const n = tCount(); c.textContent = n ? String(n) : ""; }
  if (!p.querySelector(".thead")) { p.innerHTML = tHead(); tWireHead(p); }
  // the tasks rows own #tbody only while the Tasks tab is active: another view (Activity, Journal) is
  // painted by its own function, and this repaint must not stomp it — #tcount above still gets refreshed
  if (tView !== "tasks") return;
  const st = $("#tstats"); if (st) st.innerHTML = tStats();
  if (tEditingNow()) return;

  const prev = $("#taddin");
  const keep = prev ? prev.value : "";
  const hadFocus = prev && document.activeElement === prev;

  const groups = {};
  (state.tasks || []).filter(tMatch).forEach(t => { const k = tDept(t); (groups[k] = groups[k] || []).push(t); });
  let html = "";
  T_DEPTS.forEach(key => {
    const rows = groups[key] || [];
    if (rows.length || tAdding === key) html += tSection(key, rows);   // an empty section shows only while its add row is open
  });
  if (!html) {
    html = `<div class="tempty"><div class="ic">${SVG_TINBOX}</div>
      <p class="t">${!state.biz ? "Pick a business first." : (state.tasks || []).length ? "Nothing here." : "Nothing scheduled."}</p>
      <p class="s">${(state.tasks || []).length ? "No task matches this search." : "Create your first task to start organizing what matters."}</p>
      ${state.biz ? `<button class="pill create" type="button">Create task</button>` : ""}</div>`;
  } else if (!tAdding) {
    html += `<button class="taddbtn" id="taddbottom" type="button">${SVG_TPLUS}Add task</button>`;
  }
  const body = $("#tbody"); if (!body) return;
  // Chrome fires blur on the focused add input before detaching it (isConnected is still true then)
  tPainting = true;
  try { body.innerHTML = html; } finally { tPainting = false; }
  tPaintSig = tVisSig();   // record what's now on screen so a later background refresh can skip a no-op rebuild

  body.querySelectorAll(".trow").forEach(row => {
    const t = taskById(row.dataset.id); if (!t) return;
    const who = row.querySelector(".who"); if (who && typeof tip === "function") tip(who, () => tWhoTip(t));
  });
  const add = $("#taddin");
  if (add) {
    if (keep && prev && prev.closest(".tadd") && prev.closest(".tadd").dataset.b === tAdding) add.value = keep;
    if (hadFocus || tAdding) { add.focus(); add.setSelectionRange(add.value.length, add.value.length); }
    tWireAdd(add);
  }
}

// ---------------------------------------------------------------- view switch (tabs)
// A tab click owns the whole panel repaint: it rebuilds .thead (h1 label, stats/search visibility, the
// tabs themselves) and then paints #tbody for the view landed on. Only a tab switch does this — the
// intervals below only ever touch the view that's currently active.
function tSetView(v) {
  if (T_VIEWS.indexOf(v) < 0) v = "tasks";
  if (tView === "journal" && v !== "journal") jrFlush();
  if (tView === "notes" && v !== "notes") ntFlush();
  const from = tView;
  tView = v; ls("tview", v);
  const p = $("#taskspanel"); if (!p) return;
  p.innerHTML = tHead(); tWireHead(p, from);
  if (v === "tasks") { tPaintSig = null; paintTasks(); }
  else if (v === "activity") { tLogSig = null; tPaintActivity(); tActivityRefresh(); }
  else if (v === "journal") { tPaintJournal(); }
  else if (v === "notes") { tPaintNotes(); }
  else if (v === "uploads") { tPaintUploads(); }
}

// ---------------------------------------------------------------- activity view
// Business activity log, reusing side.js's global logHtml()/logTips() so the row look (department
// stroke, day gaps) stays a single source of truth with the (currently parked) side rail log.
function tActivitySig() { return JSON.stringify([state.biz, tLog.map(e => e.id || `${e.ts}|${e.summary}`)]); }

function tPaintActivity() {
  if (tView !== "activity") return;
  const body = $("#tbody"); if (!body) return;
  if (!state.biz) { body.innerHTML = `<div class="tempty"><div class="ic">${SVG_TINBOX}</div><p class="t">Pick a business first.</p></div>`; tLogSig = null; return; }
  const sig = tActivitySig();
  if (sig === tLogSig && body.querySelector("#tlog")) return;   // nothing changed: skip the rebuild, keep tooltips alive
  tLogSig = sig;
  body.innerHTML = tLog.length
    ? `<div class="tlog" id="tlog">${logHtml(tLog)}</div>`
    : `<div class="tempty"><div class="ic">${SVG_TINBOX}</div><p class="t">Nothing logged yet.</p></div>`;
  const wrap = $("#tlog");
  if (wrap) logTips(wrap, tLog);
}

async function tActivityRefresh() {
  if (!state.biz) { tLog = []; return tPaintActivity(); }
  const biz = state.biz;
  const r = await api(`/biz/${biz}/log?n=60`);
  if (biz !== state.biz || tView !== "activity") return;   // stale by the time it landed
  tLog = Array.isArray(r) ? r : [];
  tPaintActivity();
}

// ---------------------------------------------------------------- add
function tOpenAdd(key) {
  tAdding = T_DEPTS.indexOf(key) >= 0 ? key : "general";
  paintTasks();
  const el = $("#taddin"); if (el) { el.focus(); el.scrollIntoView({ block: "nearest" }); }
}

// a task typed under a department heading carries that department's tag; general carries none
function tWireAdd(inp) {
  let settled = false;
  const key = tAdding;   // the section this row was opened in, whatever happens to the global later
  const commit = async () => {
    if (settled) return; settled = true;
    const title = inp.value.trim();
    if (tAdding === key) tAdding = null;
    if (!title) return paintTasks();
    inp.disabled = true;
    const tag = T_DEPT_TAG[key];
    await tadd({ title, owner: "", tags: tag ? [tag] : [] });
    paintTasks();
  };
  inp.onkeydown = e => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); settled = true; if (tAdding === key) tAdding = null; paintTasks(); }
  };

  inp.onblur = () => { if (!tPainting && inp.isConnected && !settled) commit(); };
}

// ---------------------------------------------------------------- row actions
// A ball or assignee click must change only its own row: no innerHTML rebuild of the list, so no lost
// hover/tooltip state on the row under the mouse and no flicker on the rows around it. `tPaintSig` is kept
// in sync with the patch so the write's own refresh (which lands ~0.3-0.6s later) sees nothing changed
// and skips its own repaint (see refreshTasks()).
function tPatchRow(t) {
  const row = [...document.querySelectorAll(".trow")].find(r => r.dataset.id === t.id);
  if (!row) { paintTasks(); return; }
  const closed = tClosed(t);
  row.className = `trow ${t.status}${closed ? " off" : ""}`;
  const ball = row.querySelector(".ball");
  if (ball) {
    const label = T_LABEL[t.status] || t.status;
    ball.className = `ball ${t.status}`;
    ball.title = `${label} — click to change, right-click to block`;
    ball.setAttribute("aria-label", `Status: ${label}. Click to change.`);
  }
  const who = row.querySelector(".who");
  if (who) {
    const o = t.owner || "";
    who.className = `who ${tWhoKind(o)}`;
    who.setAttribute("aria-label", `Assignee: ${esc(o || "unassigned")}`);
    who.innerHTML = tWhoDot(o);
    if (typeof tip === "function") tip(who, () => tWhoTip(t));   // same node: rebinding is cheap, no flicker
  }
  const dl = row.querySelector(".tdl");
  if (dl) dl.outerHTML = tDue(t) || `<span class="tdl"></span>`;
  tPaintSig = tVisSig();
}

// optimistic: the ball changes at once, in place, the write follows; a failed write refreshes
function tSetStatus(t, next) {
  if (!next || next === t.status) return;
  const id = t.id, biz = state.biz;
  // another change during the completion hold cancels the pending done write
  if (tDoneT.has(id)) { clearTimeout(tDoneT.get(id)); tDoneT.delete(id); tCompleting.delete(id); }
  t.status = next;
  if (next === "done") {
    tCompleting.add(id);
    tPatchRow(t);
    // the hold is the row staying in tCompleting (tSunk() keeps it in place), not a blocked repaint: once
    // it elapses we repaint to sink it, then fire the real write (whose own refresh will find nothing changed)
    tDoneT.set(id, setTimeout(() => {
      tDoneT.delete(id); tCompleting.delete(id);
      paintTasks();
      if (state.biz === biz) tdone(id);
    }, T_DONE_HOLD));
  } else {
    tPatchRow(t);
    twrite(id, { set: { status: next } });
  }
}

function tSetOwner(t, o) {
  if (o == null || o === (t.owner || "")) return;
  t.owner = o;
  tPatchRow(t);
  twrite(t.id, { set: { owner: o } });
}

function tTagItems(t) {
  const have = t.tags || [];
  return T_TAGS.map(x => ({ value: x, label: x, html: `<span class="towner"><span class="tdot" style="--tc:${tTagColor(x)}"></span><span class="tn">${esc(x)}</span>${have.indexOf(x) >= 0 ? `<span class="tsub">on</span>` : ""}</span>` }));
}


function tDetail(t) {
  if (typeof modal !== "function") return toast("details need core modal()");
  const closed = tClosed(t);
  const fld = (k, v, mono) => `<div class="k">${esc(k)}</div><div class="v${mono ? " mono" : ""}">${v}</div>`;
  const none = s => `<span class="none">${esc(s)}</span>`;
  const stamp = s => esc(String(s || "").slice(0, 16).replace("T", " "));
  const extra = Object.entries(t.fields || {}).map(([k, v]) => fld(k, esc(String(v)))).join("");
  const notes = (t.notes || []).map(n => `<div class="tnote">
      <div class="nh"><span class="by">${esc(tShort(n.by))}</span><span class="ts">${stamp(n.ts)}</span></div>
      <div class="tx">${esc(n.text)}</div></div>`).join("");
  const tags = (t.tags || []).map(x => `<button class="chip" type="button" data-tag="${esc(x)}" title="Tag: ${esc(x)}"><span class="tdot" style="--tc:${tTagColor(x)}"></span><span class="tn">${esc(x)}</span></button>`).join("")
    + `<button class="chip add" type="button" title="Add tag">${SVG_TTAG}</button>`;
  const o = t.owner || "";
  const body = `<div class="tdetail">
    <div class="dstat">${tDotHtml(t.status)}${esc(T_LABEL[t.status] || t.status)}</div>
    <button class="dtitle" type="button" title="Edit title">${esc(t.title)}</button>
    <div class="did">${esc(t.id)}</div>
    <div class="flds">
      ${fld("Status", `<button class="selbtn" id="td-status" type="button"><span class="v">${tDotHtml(t.status)}${esc(T_LABEL[t.status] || t.status)}</span></button>`)}
      ${fld("Assignee", `<button class="selbtn" id="td-who" type="button"><span class="v">${o ? `${tWhoInner(o)} ${esc(tShort(o))}` : "Unassigned"}</span></button>`)}
      ${fld("Due", `<input id="td-due" type="date" value="${esc(t.due || "")}">`)}
      ${fld("Tags", `<div class="tags">${tags}</div>`)}
      ${fld("Blocked by", (t.blocked_by || []).length ? esc((t.blocked_by || []).join(", ")) : none("nothing"), true)}
      ${fld("Created", stamp(t.created) + " · " + esc(tShort(t.by)), true)}
      ${fld("Updated", stamp(t.updated), true)}
      ${extra}
    </div>
    <h4>Notes${(t.notes || []).length ? " · " + (t.notes || []).length : ""}</h4>
    ${notes || `<div class="tnote"><div class="tx"><span class="none">No notes yet.</span></div></div>`}
    <textarea id="tnotetx" placeholder="Add a note…"></textarea>
    <div class="drow"><button class="ghost" id="tnotebtn" type="button">Add note</button></div>
  </div>`;
  // a write repaints the detail with the fresh task; not if the user closed it while the write was in flight
  const again = async (id, body) => {
    const r = await twrite(id, body);
    if (!m || !m.el || !m.el.isConnected) return;
    m.close();
    const fresh = taskById(id); if (r && fresh) tDetail(fresh);
  };
  const m = modal({
    title: "Task", width: 620, body,
    actions: [
      { label: "Close", onClick: close => close() },
      { label: closed ? "Reopen" : "Mark done", primary: true, onClick: async close => { close(); closed ? await twrite(t.id, { set: { status: "open" } }) : await tdone(t.id); } },
    ],
    onOpen(el) {
      const title = el.querySelector(".dtitle");
      title.onclick = () => {
        const inp = document.createElement("input"); inp.type = "text"; inp.className = "dtitle-in"; inp.value = t.title;
        title.replaceWith(inp); inp.focus(); inp.select();
        let done = false;
        const commit = () => { if (done) return; done = true; const v = inp.value.trim(); if (v && v !== t.title) again(t.id, { set: { title: v } }); else inp.replaceWith(title); };
        inp.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); commit(); } else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done = true; inp.replaceWith(title); } };
        inp.onblur = commit;
      };
      const sb = el.querySelector("#td-status");
      sb.onclick = () => dropdown(sb, T_STATES.map(s => ({ value: s, label: T_LABEL[s], html: `<span class="towner">${tDotHtml(s)}<span class="tn">${T_LABEL[s]}</span></span>` })),
        v => { if (v !== t.status) again(t.id, { set: { status: v } }); }, t.status);
      const wb = el.querySelector("#td-who");
      wb.onclick = () => dropdown(wb, tWhoItems(), v => { if (v !== o) again(t.id, { set: { owner: v } }); }, o);
      el.querySelector("#td-due").onchange = e => again(t.id, { set: { due: e.target.value || "" } });
      el.querySelectorAll(".chip").forEach(ch => ch.onclick = () => dropdown(ch, tTagItems(t), v => {
        const have = t.tags || [];
        again(t.id, { set: { tags: have.indexOf(v) >= 0 ? have.filter(x => x !== v) : have.concat([v]) } });
      }, ch.dataset.tag || null));
      const btn = el.querySelector("#tnotebtn"), tx = el.querySelector("#tnotetx");
      btn.onclick = () => { const v = tx.value.trim(); if (!v) return; btn.disabled = true; again(t.id, { note: v }); };
      tx.onkeydown = e => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); btn.click(); } };
    },
  });
  return m;
}

// ---------------------------------------------------------------- wiring
function tWireHead(p, from) {
  const s = p.querySelector("#tsearch");
  if (s) {
    s.oninput = () => { clearTimeout(tSearchT); tSearchT = setTimeout(() => { tSearch = s.value; paintTasks(); }, 80); };
    s.onkeydown = e => { if (e.key === "Escape") { e.stopPropagation(); s.value = ""; tSearch = ""; paintTasks(); s.blur(); } };
  }
  p.querySelectorAll(".ttab").forEach(b => b.onclick = () => { if (b.dataset.v !== tView) tSetView(b.dataset.v); });
  moveTInd(p, from);
}
// .thead (tabs included) is rebuilt from scratch on every switch, so #tind is a brand-new node each
// time: left to its own devices it'd render at its CSS default (translateX(0), i.e. sat under the
// first tab) for a frame before jumping to the real target. Instead, plant it at the tab we came FROM
// with transitions off, force a reflow, then re-enable transitions and slide to the real target.
function moveTInd(p, from) {
  const on = p.querySelector(".ttab.on"), ind = p.querySelector("#tind");
  if (!on || !ind) return;
  const fromBtn = from && p.querySelector(`.ttab[data-v="${from}"]`);
  if (fromBtn && fromBtn !== on) {
    ind.style.transition = "none";
    ind.style.width = `${fromBtn.offsetWidth}px`;
    ind.style.transform = `translateX(${fromBtn.offsetLeft - ind.offsetLeft}px)`;
    ind.hidden = false;
    ind.offsetHeight;          // force reflow so the line above commits before transitions come back
    ind.style.transition = "";
  } else {
    ind.hidden = false;
  }
  ind.style.width = `${on.offsetWidth}px`;
  ind.style.transform = `translateX(${on.offsetLeft - ind.offsetLeft}px)`;
}
window.addEventListener("resize", () => { const p = $("#taskspanel"); if (p && p.querySelector(".ttabs")) moveTInd(p, tView); });

(function tInit() {
  const wrap = $("#taskswrap"), handle = $("#thandle"), panel = $("#taskspanel");
  if (!wrap || !handle || !panel) return;

  handle.onclick = () => openTasks(!tPanelOpen());
  if (ls("tasks") === "1") wrap.classList.add("open");

  panel.addEventListener("click", e => {
    const plus = e.target.closest(".tplus");
    if (plus) { const sec = plus.closest(".tsec"); return void tOpenAdd(sec ? sec.dataset.b : "general"); }
    const head = e.target.closest(".tshbtn");
    if (head) { const sec = head.closest(".tsec"); return void tOpenAdd(sec ? sec.dataset.b : "general"); }
    if (e.target.closest("#taddbottom") || e.target.closest(".tempty .create")) return void tOpenAdd("general");
    if (e.target.closest(".tadd")) return;
    const row = e.target.closest(".trow"); if (!row) return;
    const t = taskById(row.dataset.id); if (!t) return;
    if (e.target.closest(".ball")) return void tSetStatus(t, tNext(t.status));
    const who = e.target.closest(".who");
    if (who) { const nx = tWhoLeft(t.owner || ""); if (nx == null) return toast("no agents in this business"); return void tSetOwner(t, nx); }
    tDetail(t);
  });

  // right click: blocked on the ball, the human owner on the assignee
  panel.addEventListener("contextmenu", e => {
    const row = e.target.closest(".trow"); if (!row) return;
    const t = taskById(row.dataset.id); if (!t) return;
    if (e.target.closest(".ball")) { e.preventDefault(); return void tSetStatus(t, t.status === "blocked" ? "open" : "blocked"); }
    if (e.target.closest(".who")) { e.preventDefault(); return void tSetOwner(t, tWhoRight(t.owner || "")); }
  });

  // click outside closes; capture phase records origin before rows are re-rendered
  document.addEventListener("click", e => {
    const el = e.target && e.target.closest ? e.target : null;
    tInside = !!(el && (el.closest("#taskswrap") || el.closest(".overlay") || el.closest(".modal") || el.closest(".menu") || el.closest(".tip")));
  }, true);
  document.addEventListener("click", () => { if (tPanelOpen() && !tInside) openTasks(false); });

  document.addEventListener("keydown", e => {
    if (e.key !== "Escape" || !tPanelOpen()) return;
    if (document.querySelector(".overlay") || document.querySelector(".menu")) return;
    const a = document.activeElement;
    if (a && a.closest && a.closest("#taskspanel") && /^(INPUT|TEXTAREA)$/.test(a.tagName)) { a.blur(); return; }
    openTasks(false);
  });

  tSetView(tView);
  setInterval(() => {
    if (state.biz && state.biz !== tLastBiz) {
      tLastBiz = state.biz;
      tDoneT.forEach(clearTimeout); tDoneT.clear(); tCompleting.clear();   // holds belong to the business they started in
      refreshTasks();
      if (tView === "activity") { tLog = []; tLogSig = null; tActivityRefresh(); }
      // the business changed under the journal tab: flush whatever was dirty to the OLD business
      // (jrFlush snapshots its biz at edit-open time, so this is safe even though state.biz just moved
      // on), then start clean on the new one
      if (tView === "journal") { jrFlush(); jrSel = null; jrSearch = ""; jrMemos = []; tPaintJournal(); }
      // same idea for the notes tab (see js/notes.js's ntFlush)
      if (tView === "notes") { ntFlush(); ntSel = null; ntSearch = ""; ntNotes = []; tPaintNotes(); }
      if (tView === "uploads") tPaintUploads();   // resets its form for the new business itself
    }
  }, 500);
  setInterval(() => {
    if (!state.biz) return;
    refreshTasks();                                    // keeps #tcount live in every view
    if (tView === "activity") tActivityRefresh();
  }, 12000);
})();
