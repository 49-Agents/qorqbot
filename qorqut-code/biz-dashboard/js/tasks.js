























const T_ORDER = ["open", "doing", "waiting", "done"];          // the left-click cycle
const T_CLOSED = ["done", "dropped"];
const T_STATES = ["open", "doing", "waiting", "blocked", "done", "dropped"];   // the detail's status picker
const T_LABEL = { open: "Open", doing: "Doing", waiting: "Waiting", done: "Done", blocked: "Blocked", dropped: "Dropped" };
const T_TAGS = ["operations", "coding", "research", "marketing", "needs-owner"];
const T_DEPTS = ["marketing", "accounting", "data-science", "code", "general"];
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
const T_VIEWS = ["tasks", "journal", "activity"];
const T_VIEW_LABEL = { tasks: "Tasks", activity: "Activity", journal: "Journal" };
let tView = ls("tview") || "tasks";
if (T_VIEWS.indexOf(tView) < 0) tView = "tasks";

// activity (log) view state
let tLog = [];
let tLogSig = null;                 // signature of what's on screen for the log list; skips a no-op rebuild
let tLogLoaded = false;             // has this business's log been fetched at least once since the view opened

// journal view state: painted on tab-switch/selection-change/after-writes only — never by the 12s interval
let jrSel = null;                   // selected memo id, or null for the homepage
let jrMemos = [];                   // cache of the last /journal fetch
let jrSearch = "";                  // detail sidebar search (in-memory filter)
let jrListOff = ls("jrlistoff") === "1";   // sidebar collapsed, persisted
let jrEditState = null;             // { id, biz, title0, body0, dirty, saving, countdown }
let jrIdleT = null, jrTickT = null; // autosave idle/countdown timers
const jrUploadingIds = new Set();   // memo ids with an in-flight /upload

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
  return `<div class="ttabs">${T_VIEWS.map(v => `<button class="ttab${v === tView ? " on" : ""}" type="button" data-v="${v}">${T_VIEW_LABEL[v]}</button>`).join("")}</div>`;
}

function tHead() {
  // the journal tab supplies its own header (homepage title/subtitle/Create, or the detail's own
  // sidebar controls) in #tbody, so the generic ttop (title + tasks-only stats/search) sits out for it
  return `<div class="thead${tView === "journal" ? " nott" : ""}">
    ${tTabs()}
    ${tView === "journal" ? "" : `<div class="ttop">
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
  tView = v; ls("tview", v);
  const p = $("#taskspanel"); if (!p) return;
  p.innerHTML = tHead(); tWireHead(p);
  if (v === "tasks") { tPaintSig = null; paintTasks(); }
  else if (v === "activity") { tLogSig = null; tPaintActivity(); tActivityRefresh(); }
  else if (v === "journal") { tPaintJournal(); }
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







const JR_ICO_PANELLEFT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/></svg>`;
const JR_ICO_BOOK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/></svg>`;
const JR_ICO_ARROWUP = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3h14"/><path d="m18 13-6-6-6 6"/><path d="M12 7v14"/></svg>`;
const JR_ICO_ARROWUP_SM = `<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3h14"/><path d="m18 13-6-6-6 6"/><path d="M12 7v14"/></svg>`;
const JR_ICO_CHECK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
const JR_ICO_PIN_SM = `<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>`;
const JR_ICO_PIN_TINY = `<svg class="jrpt" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>`;
const JR_ICO_PINOFF = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M15 9.34V6a2 2 0 0 0 0-4H9.55"/><path d="M9 9.35V6a2 2 0 0 1 2-2h1.53"/><path d="m2 2 20 20"/><path d="M9.51 9.51 5 15.24V16a1 1 0 0 0 1 1h9.53"/></svg>`;


function relativeTime(iso) {
  const t = Date.parse(iso || ""); if (isNaN(t)) return "";
  const diff = Date.now() - t;
  const m = Math.floor(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
function jrFullLabel(dateStr) {
  const day = tDay(dateStr), today = tToday();
  const n = Math.round((day - today) / T_DAY);
  const full = new Date(day).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" });
  if (n === 0) return `Today · ${full}`;
  if (n === -1) return `Yesterday · ${full}`;
  if (n === 1) return `Tomorrow · ${full}`;
  if (n > 1 && n <= 3) return `In ${n} days · ${full}`;
  if (n < -1 && n >= -3) return `${-n} days ago · ${full}`;
  return full;
}
const jrWords = c => { const s = String(c || "").trim(); return s ? s.split(/\s+/).length : 0; };
const jrPreview = c => { const l = String(c || "").split("\n").find(x => x.trim()); return l ? l.trim() : ""; };
const jrTodayStr = () => { const d = new Date(), p = n => String(n).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`; };

// ---- data
async function jrFetch() {
  if (!state.biz) { jrMemos = []; return; }
  const r = await api(`/biz/${state.biz}/journal`);
  if (Array.isArray(r)) jrMemos = r; else if (r && r.error) toast(r.error);
}
const jrById = id => jrMemos.find(m => m.id === id);
async function jrCreate(fields) {
  const r = await post(`/biz/${state.biz}/journal`, fields || {});
  if (r && r.error) { toast(r.error); return null; }
  return r;
}
async function jrPatch(biz, id, fields) {
  const r = await post(`/biz/${biz}/journal/${encodeURIComponent(id)}`, fields);
  if (r && r.error) { toast(r.error); return null; }
  return r;
}
async function jrDeleteReq(id) {
  const r = await post(`/biz/${state.biz}/journal/${encodeURIComponent(id)}/delete`, {});
  if (r && r.error) { toast(r.error); return null; }
  return r;
}
async function jrUploadReq(id) {
  const r = await post(`/biz/${state.biz}/journal/${encodeURIComponent(id)}/upload`, {});
  if (r && r.error) { toast(r.error); return null; }
  return r;
}

// ---- entry / flush
function tPaintJournal() { jrOpenView(); }
function jrOpenView() {
  jrRender();
  if (!state.biz) return;
  const biz = state.biz;
  jrFetch().then(() => { if (tView === "journal" && state.biz === biz) jrRender(); });
}
// force-save whatever's dirty in the open editor, reading the DOM before it's torn down by a repaint;
// used when the tab is left or the business/selection changes
async function jrFlush() {
  const s = jrEditState; if (!s) return;
  jrClearTimers();
  if (s.dirty) {
    const titleEl = $("#jrtitle"), bodyEl = $("#jrbody");
    const title = titleEl ? titleEl.value : s.title0, content = bodyEl ? bodyEl.value : s.body0;
    if (title !== s.title0 || content !== s.body0) {
      const r = await jrPatch(s.biz, s.id, { title, content });
      if (r) { const m = jrMemos.find(x => x.id === s.id); if (m) { m.title = title; m.content = content; m.updatedAt = r.updatedAt || m.updatedAt; } }
    }
  }
  jrEditState = null;
}

// ---- render dispatch
function jrRender() {
  const body = $("#tbody"); if (!body) return;
  if (!state.biz) { body.className = "tbody"; body.innerHTML = `<div class="tempty"><div class="ic">${SVG_TINBOX}</div><p class="t">Pick a business first.</p></div>`; return; }
  if (jrSel == null) {
    body.className = "tbody jr jr-home";
    body.innerHTML = jrHomeHtml();
    jrWireHome(body);
  } else {
    const m = jrById(jrSel);
    if (!m) { jrSel = null; return jrRender(); }
    body.className = "tbody jr jr-detail";
    body.innerHTML = jrDetailHtml(m);
    jrWireDetail(body, m);
  }
}
// non-typing refreshes (pin, rename, upload-from-menu): patch sidebar/meta in place, never rebuild the
// homepage or the open textareas from a background write
function jrRenderCurrent() {
  if (tView !== "journal") return;
  if (jrSel == null) { jrRender(); return; }
  jrPatchSidebar(); jrPatchMeta();
}

// ---- homepage
function jrCardHtml(m) {
  const title = esc(m.title && m.title.trim() ? m.title : "Untitled");
  const prev = jrPreview(m.content);
  const words = jrWords(m.content);
  const foot = `${esc(relativeTime(m.updatedAt))}${m.uploaded ? ` <span class="jrup">${JR_ICO_ARROWUP_SM}in context</span>` : ""}`;
  return `<button class="jrcard" type="button" data-id="${esc(m.id)}">
    <div class="jrcrow1"><span class="jrctitle">${title}</span>${m.pinned ? JR_ICO_PIN_TINY : ""}</div>
    <div class="jrcprev">${prev ? esc(prev) : `<span class="jrfaint">Empty memo</span>`}</div>
    <div class="jrcfoot"><span>${foot}</span><span>${words} word${words === 1 ? "" : "s"}</span></div>
  </button>`;
}
function jrHomeHtml() {
  const n = jrMemos.length;
  const pinned = jrMemos.filter(m => m.pinned);
  const today = tToday(), weekAgo = today - 7 * T_DAY;
  const byUpdated = jrMemos.slice().sort((a, b) => Date.parse(b.updatedAt || 0) - Date.parse(a.updatedAt || 0));
  const editedToday = byUpdated.filter(m => Date.parse(m.updatedAt || 0) >= today);
  const editedWeek = byUpdated.filter(m => { const t = Date.parse(m.updatedAt || 0); return t < today && t >= weekAgo; });
  const sec = (label, icon, rows) => rows.length ? `<div class="jrsec"><div class="jreyebrow">${icon || ""}${esc(label)}</div><div class="jrgrid">${rows.map(jrCardHtml).join("")}</div></div>` : "";
  return `<div class="jrhome">
    <div class="jrhhead">
      <div><h1>Journal</h1><div class="jrsub">${n} ${n === 1 ? "memo" : "memos"}</div></div>
      <button id="jrnew" class="jrcreate" type="button">Create new</button>
    </div>
    ${sec("Pinned", JR_ICO_PIN_SM, pinned)}
    ${sec("Edited today", "", editedToday)}
    ${sec("Edited last week", "", editedWeek)}
    ${sec("All memos", "", byUpdated)}
    ${!n ? `<div class="jrfaint jrfaint-lg">No memos yet — create your first one.</div>` : ""}
  </div>`;
}
function jrWireHome(root) {
  root.querySelectorAll(".jrcard").forEach(c => {
    c.onclick = () => jrSelect(c.dataset.id);
    c.oncontextmenu = e => { e.preventDefault(); jrCtxMenu(e.clientX, e.clientY, c.dataset.id); };
  });
  const nb = root.querySelector("#jrnew"); if (nb) nb.onclick = () => jrCreateNew();
}

// ---- detail: sidebar list
function jrFilterMemos() {
  const q = jrSearch.trim().toLowerCase();
  if (!q) return jrMemos.slice();
  return jrMemos.filter(m => (m.title || "").toLowerCase().includes(q) || jrFullLabel(m.date).toLowerCase().includes(q) || (m.content || "").toLowerCase().includes(q));
}
function jrRowHtml(m) {
  return `<div class="jrrow ${m.id === jrSel ? "on" : ""}" data-id="${esc(m.id)}">
    ${m.id === jrSel ? `<span class="jrind"></span>` : ""}
    <div class="jrr1"><span class="jrrttl">${m.pinned ? JR_ICO_PIN_TINY : ""}${esc(m.title && m.title.trim() ? m.title : "Untitled")}</span>${m.uploaded ? `<span class="jrdot"></span>` : ""}<span class="jrrtime">${esc(relativeTime(m.updatedAt))}</span></div>
    <div class="jrr2">${esc(jrPreview(m.content) || "Empty memo")}</div>
    <button class="jrrowdel" type="button" data-id="${esc(m.id)}" title="Delete memo">${ICO_TRASH}</button>
  </div>`;
}
function jrListHtml() {
  const filtered = jrFilterMemos();
  const pinned = filtered.filter(m => m.pinned), rest = filtered.filter(m => !m.pinned);
  let html = "";
  if (pinned.length) html += `<div class="jreyebrow jrsm">Pinned</div>` + pinned.map(jrRowHtml).join("");
  html += rest.map(jrRowHtml).join("");
  if (!filtered.length) html += `<div class="jrfaint jrfaint-pad">No memos found.</div>`;
  return html;
}
function jrWireListRows(list) {
  if (!list) return;
  list.querySelectorAll(".jrrow").forEach(row => {
    row.onclick = e => { if (e.target.closest(".jrrowdel")) return; jrSelect(row.dataset.id); };
    row.oncontextmenu = e => { e.preventDefault(); jrCtxMenu(e.clientX, e.clientY, row.dataset.id); };
  });
  list.querySelectorAll(".jrrowdel").forEach(b => b.onclick = e => { e.stopPropagation(); jrDeleteFlow(b.dataset.id); });
}
function jrPatchSidebar() {
  const list = $("#jrlist"); if (!list) return;
  const scroll = list.scrollTop;
  list.innerHTML = jrListHtml();
  list.scrollTop = scroll;
  jrWireListRows(list);
  const foot = $("#jrfoot"); if (foot) foot.textContent = `${jrMemos.length} ${jrMemos.length === 1 ? "memo" : "memos"}`;
}

// ---- detail: editor chrome (meta line, upload button, save pill) — patchable without touching textareas
function jrMetaHtml(m) {
  const words = jrWords(m.content);
  const upl = m.uploaded ? `<span class="jrup">${JR_ICO_ARROWUP_SM}in context ${esc(relativeTime(m.uploaded))}</span>` : `<span class="jrup off">not in context</span>`;
  return `${esc(jrFullLabel(m.date))}<span class="sep">·</span>edited ${esc(relativeTime(m.updatedAt))}<span class="sep">·</span>${words} word${words === 1 ? "" : "s"}<span class="sep">·</span>${upl}`;
}
function jrUploadBtnHtml(m) {
  const busy = jrUploadingIds.has(m.id);
  const label = busy ? "Uploading…" : (m.uploaded ? "Re-upload" : "Upload to context");
  const dis = busy || !String(m.content || "").trim();
  return `<button id="jrupload" class="jbtn" type="button"${dis ? " disabled" : ""}>${esc(label)}</button>`;
}
function jrRingSvg(n) {
  const total = 8, frac = Math.max(0, Math.min(1, n / total)), r = 6, c = 2 * Math.PI * r, off = c * (1 - frac);
  return `<svg width="16" height="16" viewBox="0 0 16 16" class="jrring"><circle cx="8" cy="8" r="${r}" fill="none" stroke="currentColor" stroke-opacity=".25" stroke-width="2"/><circle cx="8" cy="8" r="${r}" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="${c}" stroke-dashoffset="${off}" transform="rotate(-90 8 8)"/></svg>`;
}
function jrSaveHtml() {
  const s = jrEditState; if (!s) return "";
  if (s.saving) return `<span class="jrsavepill busy">Saving…</span>`;
  if (s.dirty) return `<span class="jrsavepill" type="button">${s.countdown != null ? jrRingSvg(s.countdown) : ""}Save</span>`;
  return `<span class="jrsaved">${JR_ICO_CHECK}Saved</span>`;
}
function jrPaintSaveState() {
  const el = $("#jrsavewrap"); if (!el) return;
  el.innerHTML = jrSaveHtml();
  const btn = el.querySelector(".jrsavepill"); if (btn) btn.onclick = () => jrSaveNow();
  // Discard only does something while there are unsaved changes; disabled (not hidden) otherwise
  const disc = $("#jrdiscard"); if (disc) disc.disabled = !jrEditState || !jrEditState.dirty || jrEditState.saving;
}
function jrPatchMeta() {
  const m = jrById(jrSel); if (!m) return;
  const meta = $("#jrmeta"); if (meta) meta.innerHTML = jrMetaHtml(m);
  const upBtn = $("#jrupload");
  if (upBtn) { upBtn.outerHTML = jrUploadBtnHtml(m); const nb = $("#jrupload"); if (nb) nb.onclick = () => jrDoUpload(m.id); }
}

// ---- detail: full build (selection change / tab open / create-new only)
function jrDetailHtml(m) {
  return `<div class="jred">
    <div class="jrside ${jrListOff ? "off" : ""}" id="jrside">
      <div class="jrsidectrl">
        <button id="jrnew2" class="jrcreate jrflex" type="button">Create new</button>
      </div>
      <div class="jrsearchwrap"><input id="jrsearch" class="jrsearch-in" placeholder="Search memos…" value="${esc(jrSearch)}" autocomplete="off" spellcheck="false"></div>
      <div class="jrdivider"></div>
      <div class="jrlist" id="jrlist">${jrListHtml()}</div>
      <div class="jrfoot" id="jrfoot">${jrMemos.length} ${jrMemos.length === 1 ? "memo" : "memos"}</div>
    </div>
    <div class="jreditor" id="jreditor">
      <div class="jredinner">
        <div class="jrtopbar">
          <button id="jrtoggle" class="jricon" type="button" title="${jrListOff ? "Show memo list" : "Hide memo list"}">${JR_ICO_PANELLEFT}</button>
          <button id="jrhomebtn" class="jrback" type="button" title="Journal home">${JR_ICO_BOOK}<span>Journal</span></button>
        </div>
        <div class="jredhead">
          <textarea id="jrtitle" class="jrtitlein" rows="1" placeholder="Untitled"></textarea>
          <div class="jredctrls" id="jredctrls">
            ${jrUploadBtnHtml(m)}
            <button id="jrdiscard" class="jbtn" type="button" disabled>Discard</button>
            <span class="jrsavewrap" id="jrsavewrap">${jrSaveHtml()}</span>
            <button id="jrdel" class="jricon danger" type="button" title="Delete memo">${ICO_TRASH}</button>
          </div>
        </div>
        <div class="jrmeta" id="jrmeta">${jrMetaHtml(m)}</div>
        <div class="jrhr"></div>
        <textarea id="jrbody" class="jrbodyin" placeholder="Start writing…" spellcheck="false"></textarea>
      </div>
    </div>
  </div>`;
}
function jrWireDetail(root, m) {
  jrEditState = { id: m.id, biz: state.biz, title0: m.title || "", body0: m.content || "", dirty: false, saving: false, countdown: null };
  const titleEl = $("#jrtitle"), bodyEl = $("#jrbody");
  titleEl.value = m.title || ""; jrGrow(titleEl);
  bodyEl.value = m.content || "";

  titleEl.oninput = () => { jrGrow(titleEl); jrEditState.dirty = true; jrSchedule(); };
  titleEl.onkeydown = e => {
    if (e.key === "Enter") { e.preventDefault(); titleEl.blur(); }
    else if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) { e.preventDefault(); jrSaveNow(); }
  };
  titleEl.onblur = () => { if (jrEditState && jrEditState.dirty) jrSaveNow(); };

  bodyEl.oninput = () => { jrEditState.dirty = true; jrSchedule(); };
  bodyEl.onkeydown = e => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) { e.preventDefault(); jrSaveNow(); }
    else if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); jrSaveNow(); }
    else if ((e.ctrlKey || e.metaKey) && e.key === "=") {
      e.preventDefault();
      const start = bodyEl.selectionStart, end = bodyEl.selectionEnd;
      bodyEl.setRangeText("─".repeat(32), start, end, "end");
      jrEditState.dirty = true; jrSchedule();
    }
  };

  const toggleBtn = root.querySelector("#jrtoggle");
  toggleBtn.onclick = () => {
    jrListOff = !jrListOff; ls("jrlistoff", jrListOff ? "1" : "0");
    const side = root.querySelector("#jrside"); if (side) side.classList.toggle("off", jrListOff);
    toggleBtn.title = jrListOff ? "Show memo list" : "Hide memo list";
  };
  // Both controls live in the editor header (outside .jrside), so they still work — and stay
  // reachable — whether or not the sidebar is collapsed.
  root.querySelector("#jrhomebtn").onclick = () => jrGoHome();
  root.querySelector("#jrnew2").onclick = () => jrCreateNew();
  root.querySelector("#jrdel").onclick = () => jrDeleteFlow(m.id);
  root.querySelector("#jrdiscard").onclick = () => jrDiscard();
  const up = root.querySelector("#jrupload"); if (up) up.onclick = () => jrDoUpload(m.id);
  const savepill = root.querySelector(".jrsavepill"); if (savepill) savepill.onclick = () => jrSaveNow();

  const search = root.querySelector("#jrsearch");
  let searchT = null;
  search.oninput = () => { clearTimeout(searchT); searchT = setTimeout(() => { jrSearch = search.value; jrPatchSidebar(); }, 80); };

  jrWireListRows(root.querySelector("#jrlist"));
}

// ---- autosave (idle 4s → 8s visible countdown → save; Ctrl/Cmd+S, Ctrl/Cmd+Enter, title blur save now)
function jrClearTimers() { clearTimeout(jrIdleT); clearTimeout(jrTickT); jrIdleT = jrTickT = null; }
function jrGrow(ta) { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; }
function jrSchedule() {
  jrClearTimers();
  if (jrEditState) jrEditState.countdown = null;
  jrPaintSaveState();
  jrIdleT = setTimeout(() => jrCountdown(8), 4000);
}
function jrCountdown(n) {
  const s = jrEditState; if (!s) return;
  if (n <= 0) { s.countdown = null; return void jrSaveNow(); }
  s.countdown = n; jrPaintSaveState();
  jrTickT = setTimeout(() => jrCountdown(n - 1), 1000);
}
async function jrSaveNow() {
  const s = jrEditState; if (!s || s.saving) return;
  jrClearTimers();
  const titleEl = $("#jrtitle"), bodyEl = $("#jrbody"); if (!titleEl || !bodyEl) return;
  const title = titleEl.value, content = bodyEl.value;
  if (title === s.title0 && content === s.body0) { s.dirty = false; s.countdown = null; jrPaintSaveState(); return; }
  s.saving = true; jrPaintSaveState();
  const r = await jrPatch(s.biz, s.id, { title, content });
  if (jrEditState !== s) return;   // navigated away while saving
  s.saving = false;
  if (r) {
    s.title0 = title; s.body0 = content; s.dirty = false; s.countdown = null;
    const m = jrMemos.find(x => x.id === s.id); if (m) { m.title = title; m.content = content; m.updatedAt = r.updatedAt || new Date().toISOString(); }
    jrPatchMeta(); jrPatchSidebar();
  }
  jrPaintSaveState();
}

// revert the open memo's title/body to the last-saved snapshot, cancel any pending autosave, and repaint
// the save pill back to idle; a no-op (buttons stays disabled) while there's nothing unsaved to drop
function jrDiscard() {
  const s = jrEditState; if (!s || !s.dirty) return;
  jrClearTimers();
  s.dirty = false; s.saving = false; s.countdown = null;
  const titleEl = $("#jrtitle"), bodyEl = $("#jrbody");
  if (titleEl) { titleEl.value = s.title0; jrGrow(titleEl); }
  if (bodyEl) bodyEl.value = s.body0;
  jrPaintSaveState();
}

// ---- selection / navigation / create
async function jrSelect(id) {
  if (id === jrSel) return;
  await jrFlush();
  jrSel = id;
  jrRender();
}
const jrGoHome = () => jrSelect(null);
async function jrCreateNew() {
  await jrFlush();
  const r = await jrCreate({ date: jrTodayStr() });
  if (!r) return;
  await jrFetch();
  jrSel = r.id;
  jrRender();
}

// ---- pin / rename / upload / delete
function jrTogglePin(m) {
  const next = !m.pinned;
  m.pinned = next;
  jrRenderCurrent();
  jrPatch(state.biz, m.id, { pinned: next }).then(async r => {
    if (!r) { m.pinned = !next; jrRenderCurrent(); return; }
    await jrFetch();
    jrRenderCurrent();
  });
}
function jrRenameFlow(m) {
  const body = `<input id="jrrenamein" class="inp" type="text" value="${esc(m.title || "")}" autocomplete="off" spellcheck="false">`;
  const commit = async (close, el) => {
    const inp = el.querySelector("#jrrenamein");
    const v = inp ? inp.value.trim() : "";
    close();
    const r = await jrPatch(state.biz, m.id, { title: v });
    if (!r) return;
    await jrFetch();
    if (jrSel === m.id) {
      jrPatchMeta();
      const t = $("#jrtitle"); if (t) { t.value = v; jrGrow(t); }
      if (jrEditState && jrEditState.id === m.id) jrEditState.title0 = v;
    }
    jrPatchSidebar();
  };
  modal({
    title: "Rename memo", body, width: 420,
    onOpen: el => { const inp = el.querySelector("#jrrenamein"); inp.select(); },
    actions: [
      { label: "Cancel", onClick: close => close() },
      { label: "Save", primary: true, onClick: commit },
    ],
  });
  const inp = document.getElementById("jrrenamein");
  if (inp) inp.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); const b = document.querySelector(".modal .mfoot .primary"); if (b) b.click(); } };
}
async function jrDoUpload(id) {
  if (jrUploadingIds.has(id)) return;
  const m = jrById(id); if (!m || !String(m.content || "").trim()) return;
  jrUploadingIds.add(id);
  jrRenderCurrent();
  const r = await jrUploadReq(id);
  jrUploadingIds.delete(id);
  if (r) { toast("Uploaded — filer will file it."); await jrFetch(); }
  jrRenderCurrent();
}
function jrDeleteFlow(id) {
  const m = jrById(id); if (!m) return;
  const title = m.title && m.title.trim() ? m.title : "Untitled";
  modal({
    title: "Delete memo?",
    body: `<p>"${esc(title)}" will be deleted. This can’t be undone.</p>`,
    actions: [
      { label: "Cancel", onClick: close => close() },
      { label: "Delete", primary: true, onClick: async close => {
        close();
        const r = await jrDeleteReq(id); if (!r) return;
        let nextId = null;
        if (jrSel === id) {
          const filtered = jrFilterMemos();
          const ordered = filtered.filter(x => x.pinned).concat(filtered.filter(x => !x.pinned));
          const idx = ordered.findIndex(x => x.id === id);
          if (idx >= 0) nextId = (ordered[idx + 1] || ordered[idx - 1] || {}).id || null;
        }
        await jrFetch();
        if (jrSel === id) jrSel = nextId;
        jrRender();
      } },
    ],
  });
}
function jrCtxMenu(x, y, id) {
  const m = jrById(id); if (!m) return;
  ctxMenu(x, y, [
    { label: m.pinned ? "Unpin" : "Pin", icon: m.pinned ? JR_ICO_PINOFF : ICO_PIN, onClick: () => jrTogglePin(m) },
    { label: "Rename", icon: ICO_PENCIL, onClick: () => jrRenameFlow(m) },
    { label: "Upload to context", icon: JR_ICO_ARROWUP, onClick: () => jrDoUpload(m.id) },
    { label: "Delete", icon: ICO_TRASH, danger: true, onClick: () => jrDeleteFlow(m.id) },
  ]);
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
function tWireHead(p) {
  const s = p.querySelector("#tsearch");
  if (s) {
    s.oninput = () => { clearTimeout(tSearchT); tSearchT = setTimeout(() => { tSearch = s.value; paintTasks(); }, 80); };
    s.onkeydown = e => { if (e.key === "Escape") { e.stopPropagation(); s.value = ""; tSearch = ""; paintTasks(); s.blur(); } };
  }
  p.querySelectorAll(".ttab").forEach(b => b.onclick = () => { if (b.dataset.v !== tView) tSetView(b.dataset.v); });
}

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
    }
  }, 500);
  setInterval(() => {
    if (!state.biz) return;
    refreshTasks();                                    // keeps #tcount live in every view
    if (tView === "activity") tActivityRefresh();
  }, 12000);
})();
