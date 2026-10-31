// Personal task drawer aggregates the current human's business tasks.
// Agent panes retain state.tasks, the selected business cache. Every personal row
// and mutation carries its owning business; IDs alone are not globally unique.
// Completion holds the row briefly, and keeps its business even during navigation.

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
const tSunk = t => tClosed(t) && !tCompleting.has(tKey(t));
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
state.myTasks = [];
let tPersonalLoaded = false, tPersonalError = null, tFetch = 0;

// ---------------------------------------------------------------- view (tabs)
const T_VIEWS = ["tasks", "notes", "signoffs", "uploads", "routines", "activity"];
const T_VIEW_LABEL = { tasks: "Tasks", activity: "Activity", notes: "Notes", signoffs: "Signoffs", uploads: "Uploads", routines: "Routines" };
const T_VIEW_ICON = {
  signoffs: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 5H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-4"/><rect x="9" y="2" width="6" height="6" rx="1"/><path d="m9 14 2 2 4-4"/></svg>`,
  tasks: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 10.656V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h12.344"></path><path d="m9 11 3 3L22 4"></path></svg>`,
  notes: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.67 19a2 2 0 0 0 1.416-.588l6.154-6.172a6 6 0 0 0-8.49-8.49L5.586 9.914A2 2 0 0 0 5 11.328V18a1 1 0 0 0 1 1z"></path><path d="M16 8 2 22"></path><path d="M17.5 15H9"></path></svg>`,
  routines: `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`,
  uploads: ICO_UPLOAD,  // defined in core.js: the same glyph leads every upload button
  activity: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"></path></svg>`,
};
let tView = ls("tview") || "tasks";
if (tView === "journal") tView = "notes";
if (T_VIEWS.indexOf(tView) < 0) tView = "tasks";

// activity (log) view state
let tLog = [];
let tLogSig = null;                 // signature of what's on screen for the log list; skips a no-op rebuild
let tLogLoaded = false;             // has this business's log been fetched at least once since the view opened

// notes view state (ntSel/ntNotes/ntSearch/...) lives in js/notes.js the same way.
// uploads view state (upForm/upDone/...) lives in js/uploads.js; it has no editor, so nothing to flush.

const tPanelOpen = () => { const w = $("#taskswrap"); return !!w && w.classList.contains("open"); };
const tBusiness = t => t.business || (t.business_id && (state.bizs || []).find(b => b.id === t.business_id || b.business_id === t.business_id)) || tBiz();
const tSlug = t => tBusiness(t).slug;
const tKey = t => JSON.stringify([t.business_id || tBusiness(t).id || tSlug(t), t.id]);
const taskById = (id, biz = state.biz) => typeof id === 'object' ? id : [...(state.myTasks || []), ...(state.tasks || [])].find(t => tKey(t) === id || t.id === id && tSlug(t) === biz);
const tPersonalRows = () => state.myTasks || [];
const tBiz = (biz = state.biz) => (state.bizs || []).find(x => x.slug === biz) || {};
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
  if (tView === "signoffs" && typeof tPaintSignoffs === "function") tPaintSignoffs();
  if (!on && document.activeElement && document.activeElement.closest("#taskspanel")) $("#thandle").focus();
}

// ---------------------------------------------------------------- data
function refreshTasksSoon() { clearTimeout(tSoonT); tSoonT = setTimeout(refreshTasks, 500); }



async function refreshTasks() {
  const gen = tGen, biz = state.biz, request = ++tFetch;
  const [personal, local] = await Promise.allSettled([
    api('/me/tasks'), biz ? api(`/biz/${encodeURIComponent(biz)}/tasks?all=1`) : Promise.resolve([]),
  ]);
  if (gen !== tGen || request !== tFetch) return;
  if (personal.status === 'fulfilled' && Array.isArray(personal.value?.tasks)) {
    state.myTasks = personal.value.tasks;
    tPersonalLoaded = true; tPersonalError = null;
    state.myTasks.forEach(t => { if (tCompleting.has(tKey(t))) t.status = 'done'; });
  } else {
    tPersonalError = 'Could not refresh your tasks. Retry before making changes.';
  }
  if (biz === state.biz && local.status === 'fulfilled' && Array.isArray(local.value)) {
    state.tasks = local.value;
    state.tasks.forEach(t => { if (tCompleting.has(tKey(t))) t.status = 'done'; });
  }
  if (tVisSig() !== tPaintSig) paintTasks();
}
async function tFetchTask(biz, id) {
  const result = await api(`/biz/${encodeURIComponent(biz)}/task/${encodeURIComponent(id)}`);
  if (!result?.task) throw new Error('Task is no longer available.');
  return {...result.task, business: tBiz(biz)};
}

function tCount() { return tPersonalRows().filter(t => !tClosed(t)).length; }

// a write always repaints, so let go of whatever row control is focused first
function tRelease() {
  const a = document.activeElement;
  if (a && a.closest && a.closest("#taskspanel") && a.id !== "taddin" && a.id !== "tsearch") try { a.blur(); } catch (e) {}
}

async function twrite(identity, body, onError) {
  const task = taskById(identity); if (!task) return null;
  tGen++;
  try {
    const r = await post(`/biz/${encodeURIComponent(tSlug(task))}/task/${encodeURIComponent(task.id)}`, {expected_revision: task.revision, ...body});
    if (r?.error) throw new Error(typeof r.error === 'string' ? r.error : r.error.message);
    if (r?.status === 'signoff_required') { toast('Submitted for business approval.'); await refreshTasks(); return null; }
    tRelease(); await refreshTasks();
    if (typeof refreshSideSoon === 'function' && state.biz === tSlug(task)) refreshSideSoon();
    return r;
  } catch (error) { toast(error.message); onError?.(error.message); await refreshTasks(); return null; }
}
async function tdone(identity, revision) {
  return twrite(identity, {set: {status: 'done'}, ...(revision ? {expected_revision: revision} : {})});
}

async function tadd(fields) {
  tGen++;
  const r = await post(`/biz/${state.biz}/task`, {owner: tHuman(), ...fields});
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
  return JSON.stringify([state.scope, state.biz, tPersonalLoaded, tPersonalError, tPersonalRows().filter(tMatch).map(t =>
    [tKey(t), t.status, t.owner || "", t.title, t.due || "", (t.tags || []).join(","), tSunk(t)])]);
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
function tWhoTip(t, detailsOnly = false) {
  const o = t.owner || "", k = tWhoKind(o);
  const who = k === "none" ? "Unassigned" : `${esc(tShort(o))} <span class="k">${k === "human" ? "owner" : k === "agent" ? "agent" : o.split(":")[0]}</span>`;
  if (detailsOnly) return `${who}<br><span class="k">Open in Tasks</span>`;
  const left = tWhoLeft(o);
  const hints = [left == null ? "no agents" : `click: ${left ? tShort(left) : "unassign"}`,
    `right-click: ${k === "human" ? "unassign" : tShort(tHuman())}`];
  return `${who}<br><span class="k">${hints.join(" · ")}</span>`;
}
function tWhoItems(biz = state.biz) {
  const items = [{ value: "", label: "Unassigned", html: `<span class="towner">${tWhoInner("")}<span class="tn">Unassigned</span></span>` }];
  (tBiz(biz).agents || []).map(a => "agent:" + a).forEach(o => items.push({ value: o, label: o, html: `<span class="towner">${tWhoInner(o)}<span class="tn">${esc(tShort(o))}</span><span class="tsub">agent</span></span>` }));
  const human = "human:" + (tBiz(biz).owner || "dashboard");
  if (tBiz(biz).owner) items.push({ value: human, label: human, html: `<span class="towner">${tWhoInner(human)}<span class="tn">${esc(tShort(human))}</span><span class="tsub">owner</span></span>` });
  return items;
}



function tDue(t) {
  if (!t.due || tClosed(t)) return "";
  const n = tDays(t.due);
  const text = n === 0 ? "today" : n > 0 ? `${n}d` : `-${-n}d`;
  const tone = t.status === "blocked" || n < 0 ? "over" : n <= 1 ? "soon" : "far";
  return `<span class="tdl ${tone}" title="Due ${esc(t.due)}">${text}</span>`;
}

function tBall(t, detailsOnly = false) {
  const s = t.status, label = T_LABEL[s] || s;
  return `<button class="ball ${esc(s)}" type="button" title="${esc(label)} — ${detailsOnly ? "open in Tasks" : "click to change, right-click to block"}"
    aria-label="Status: ${esc(label)}. ${detailsOnly ? "Open in Tasks." : "Click to change."}"><span class="core"></span></button>`;
}

// the deadline slot is reserved on every row in every section, whether or not any task anywhere has a
// due date, so assignee/status balls line up identically across sections
function tRow(t, detailsOnly = false) {
  const business = tBusiness(t);
  const icon = `<span class="task-business" title="${esc(business.name || business.slug || '')}" aria-label="Business: ${esc(business.name || business.slug || '')}">${esc((business.name || business.slug || '·').charAt(0).toUpperCase())}</span>`;
  return `<div class="trow ${esc(t.status)}${tClosed(t) ? ' off' : ''}${detailsOnly ? '' : ' personal-task'}" data-id="${esc(t.id)}" data-key="${esc(tKey(t))}" data-business="${esc(business.slug || '')}">${detailsOnly ? (tDue(t) || '<span class="tdl"></span>') + tWho(t) : icon}${tBall(t, detailsOnly)}<p class="ttl">${esc(t.title)}</p>${detailsOnly ? '' : tDue(t)}<button class="tmore" type="button" title="Details" aria-label="Task details">${SVG_TMORE}</button></div>`;
}

function tWireRows(body, detailsOnly = false) {
  body.querySelectorAll(".trow").forEach(row => {
    const t = taskById(row.dataset.key || row.dataset.id); if (!t) return;
    const who = row.querySelector(".who");
    if (who && typeof tip === "function") tip(who, () => tWhoTip(t, detailsOnly));
  });
}

const tAddRow = key => `<div class="tadd" data-b="${esc(key)}"><span class="tdl"></span><span class="who ph"><span class="wd none"></span></span><span class="ball ph"><span class="core"></span></span><input id="taddin" placeholder="Task name…" autocomplete="off" spellcheck="false"></div>`;

function tSection(key, rows, { label = dept(key).label, canAdd = true, detailsOnly = false } = {}) {
  const open = rows.filter(t => !tSunk(t)), sunk = rows.filter(tSunk);
  const heading = `<h2>${esc(label)}</h2><span class="c">${open.length}</span>`;
  // two ways to add to this section: the heading text/count itself, or the + — both call tOpenAdd(key)
  return `<section class="tsec" data-b="${esc(key)}">
    <div class="tsh">
      ${canAdd ? `<button class="tshbtn" type="button" title="Add task to ${esc(label)}" aria-label="Add task to ${esc(label)}">${heading}</button>
      <button class="tplus" type="button" title="Add task to ${esc(label)}" aria-label="Add task to ${esc(label)}">${SVG_TPLUS}</button>` : heading}
    </div>
    <div class="tsi">${open.concat(sunk).map(t => tRow(t, detailsOnly)).join("")}${canAdd && tAdding === key ? tAddRow(key) : ""}</div>
  </section>`;
}

const tTabId = (view, bodyId = "tbody") => `${bodyId === "tbody" ? "ttab" : `${bodyId}-tab`}-${view}`;
function tTabs(views = T_VIEWS, active = tView, bodyId = "tbody") {
  return `<div class="ttabs" role="tablist" aria-label="${bodyId === "tbody" ? "Business panel" : "Agent pane"}"><div class="tind" hidden></div>${views.map(v => `<button class="ttab${v === active ? " on" : ""}" id="${esc(tTabId(v, bodyId))}" role="tab" aria-selected="${v === active}" aria-controls="${esc(bodyId)}" type="button" data-v="${v}" title="${T_VIEW_LABEL[v]}" aria-label="${T_VIEW_LABEL[v]}">${T_VIEW_ICON[v]}</button>`).join("")}</div>`;
}

function tHead({ view = tView, views = T_VIEWS, bodyId = "tbody", title = view === "tasks" && bodyId === "tbody" ? "My tasks" : T_VIEW_LABEL[view], taskTools = true } = {}) {
  // the journal/notes tabs supply their own header (homepage title/subtitle/Create, or the detail's
  // own sidebar controls) in #tbody, so the generic ttop (title + tasks-only stats/search) sits out
  const ownHead = view === "notes" || view === "signoffs" || view === "uploads" || view === "routines";
  const tools = view === "tasks" && taskTools;
  return `<div class="thead${ownHead ? " nott" : ""}">
    ${tTabs(state.scope === "global" && bodyId === "tbody" ? ["tasks", "signoffs", "routines"] : views, view, bodyId)}
    ${ownHead ? "" : `<div class="ttop">
      <div><h1>${esc(title)}</h1>${tools ? `<div class="tstats" id="tstats"></div>` : ""}</div>
      ${tools ? `<label class="tsearch">${SVG_TSEARCH}<input id="tsearch" type="text" placeholder="Search by title, tag or content..." autocomplete="off" spellcheck="false"></label>` : ""}
    </div>`}
  </div><div class="tbody" id="${esc(bodyId)}" role="tabpanel" aria-labelledby="${esc(tTabId(view, bodyId))}"></div>`;
}

function tEmptyHtml(title, { sub = "", create = false } = {}) {
  return `<div class="tempty"><div class="ic">${SVG_TINBOX}</div><p class="t">${esc(title)}</p>` +
    (sub ? `<p class="s">${esc(sub)}</p>` : "") +
    (create ? `<button class="pill create" type="button">Create task</button>` : "") + `</div>`;
}

function tStats() {
  const all = tPersonalRows(), today = tToday();
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

  const rows = tPersonalRows().filter(tMatch).sort((a,b) => Number(tSunk(a))-Number(tSunk(b)) || String(a.due || '9999').localeCompare(b.due || '9999') || a.title.localeCompare(b.title) || tKey(a).localeCompare(tKey(b)));
  const open = rows.filter(t => !tSunk(t)), closed = rows.filter(tSunk);
  let html = tPersonalError ? `<p class="personal-task-error" role="alert">${esc(tPersonalError)} <button class="ghost" id="tasks-retry">Retry</button></p>` : '';
  if (!tPersonalLoaded && !tPersonalError) html += tEmptyHtml('Loading your tasks…');
  else {
    html += open.length ? `<div class="personal-task-list">${open.map(t => tRow(t)).join('')}</div>` : tEmptyHtml(tSearch ? 'No matching tasks.' : 'Nothing waiting on you.');
    if (closed.length) html += `<details class="personal-completed"><summary>Completed · ${closed.length}</summary>${closed.map(t => tRow(t)).join('')}</details>`;
    if (state.biz) html += tAdding ? tAddRow(tAdding) : `<button class="taddbtn" id="taddbottom" type="button">${SVG_TPLUS}Add task in ${esc(tBiz().name || state.biz)}</button>`;
  }
  const body = $("#tbody"); if (!body) return;
  // Chrome fires blur on the focused add input before detaching it (isConnected is still true then)
  tPainting = true;
  try { body.innerHTML = html; } finally { tPainting = false; }
  tPaintSig = tVisSig();   // record what's now on screen so a later background refresh can skip a no-op rebuild

  tWireRows(body);
  body.querySelector("#tasks-retry")?.addEventListener("click", refreshTasks);
  if (tPersonalError) body.querySelectorAll(".ball, .tmore").forEach(b => b.disabled = true);
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
  if (T_VIEWS.indexOf(v) < 0 || state.scope === "global" && !["tasks", "signoffs", "routines"].includes(v)) v = "tasks";
  if (tView === "notes" && v !== "notes" && ntEditState) { ntFlush().then(saved=>{if(saved)tSetView(v);}); return; }
  const from = tView;
  const tabFocused = document.activeElement && document.activeElement.closest(".ttabs");
  tView = v; ls("tview", v);
  const p = $("#taskspanel"); if (!p) return;
  p.innerHTML = tHead(); tWireHead(p, from);
  if (from === "signoffs" && v !== "signoffs" && typeof tPaintSignoffs === "function") tPaintSignoffs();
  if (v === "tasks") { tPaintSig = null; paintTasks(); }
  else if (v === "activity") { tLogSig = null; tPaintActivity(); tActivityRefresh(); }
  else if (v === "notes") { tPaintNotes(); }
  else if (v === "signoffs") { tPaintSignoffs(); }
  else if (v === "uploads") { tPaintUploads(); }
  else if (v === "routines") { tPaintRoutines(); }
  if (tabFocused) p.querySelector(`.ttab[data-v="${v}"]`).focus({ preventScroll: true });
}

// ---------------------------------------------------------------- activity view
// Business activity log, reusing side.js's global logHtml()/logTips() so the row look (department
// stroke, day gaps) stays a single source of truth with the (currently parked) side rail log.
function tActivitySig() { return JSON.stringify([state.biz, tLog.map(e => e.id || `${e.ts}|${e.summary}`)]); }

function tActivityHtml(rows, emptyMessage = "Nothing logged yet.") {
  return rows.length ? `<div class="tlog">${logHtml(rows)}</div>` : tEmptyHtml(emptyMessage);
}

function tPaintActivity() {
  if (tView !== "activity") return;
  const body = $("#tbody"); if (!body) return;
  if (!state.biz) { body.innerHTML = tEmptyHtml("Pick a business first."); tLogSig = null; return; }
  const sig = tActivitySig();
  if (sig === tLogSig && body.querySelector(".tlog")) return;   // nothing changed: skip the rebuild, keep tooltips alive
  tLogSig = sig;
  body.innerHTML = tActivityHtml(tLog);
  const wrap = body.querySelector(".tlog");
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
    await tadd({ title, tags: tag ? [tag] : [] });
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
  // The agent pane shares these rows; edits patch the drawer that owns the controls.
  const panel = $("#taskspanel");
  const row = panel && [...panel.querySelectorAll(".trow")].find(r => r.dataset.key === tKey(t));
  if (!row) { paintTasks(); return; }
  const closed = tClosed(t);
  row.className = `trow personal-task ${t.status}${closed ? " off" : ""}`;
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
  if (tPersonalError) return;
  const id = tKey(t);
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
      tdone(t, t.revision);
    }, T_DONE_HOLD));
  } else {
    tPatchRow(t);
    twrite(t, { set: { status: next }, expected_revision: t.revision });
  }
}

function tSetOwner(t, o) {
  if (o == null || o === (t.owner || "")) return;
  t.owner = o;
  tPatchRow(t);
  twrite(t, { set: { owner: o }, expected_revision: t.revision });
}

function tTagItems(t) {
  const have = t.tags || [];
  return T_TAGS.map(x => ({ value: x, label: x, html: `<span class="towner"><span class="tdot" style="--tc:${tTagColor(x)}"></span><span class="tn">${esc(x)}</span>${have.indexOf(x) >= 0 ? `<span class="tsub">on</span>` : ""}</span>` }));
}


async function tOpenTask(id) {
  const biz = state.biz;
  if (!biz) return;
  let t = taskById(id, biz);
  if (!t) {
    try { t = await tFetchTask(biz, id); }
    catch (error) { toast(error.httpStatus === 404 || error.message === 'Task is no longer available.' ? 'Task is no longer available.' : 'Could not load task.'); return; }
    if (state.biz !== biz) return;
  }
  clearTimeout(tSearchT); tSearch = "";
  // Cross-pane navigation belongs to this click, even though it began outside the drawer.
  tInside = true;
  openTasks(true);
  tSetView("tasks");
  const row = [...$("#taskspanel").querySelectorAll(".trow")].find(r => r.dataset.key === tKey(t));
  if (row) row.scrollIntoView({ block: "nearest" });
  tDetail(t);
}

function tDetail(t) {
  if (typeof modal !== "function") return toast("details need core modal()");
  const biz = tSlug(t);
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
    <h4>Stories</h4><div id="td-stories" class="small muted">Loading related stories…</div>
    <h4>Notes${(t.notes || []).length ? " · " + (t.notes || []).length : ""}</h4>
    ${notes || `<div class="tnote"><div class="tx"><span class="none">No notes yet.</span></div></div>`}
    <textarea id="tnotetx" placeholder="Add a note…"></textarea>
    <div class="drow"><button class="ghost" id="tnotebtn" type="button">Add note</button></div>
    <div id="td-error" class="ferr" role="alert" hidden></div><button id="td-reload" class="ghost" type="button" hidden>Reload task</button>
  </div>`;
  // a write repaints the detail with the fresh task; not if the user closed it while the write was in flight
  const again = async (id, body) => {
    if (!m.el.isConnected) return;
    const r = await twrite(t, {...body,expected_revision:t.revision}, message=>{
      if (!m.el.isConnected) return;
      const error=m.el.querySelector('#td-error');error.textContent=message;error.hidden=false;
      m.el.querySelector('#td-reload').hidden=false;
      m.el.querySelector('#tnotebtn').disabled=false;
    });
    if (!m || !m.el || !m.el.isConnected) return;
    if (!r) return;
    m.close();
    const fresh = r.task ? {...r.task, business: tBusiness(t)} : await tFetchTask(biz, id);
    if (fresh) tDetail(fresh);
  };
  const m = modal({
    title: `Task · ${tBusiness(t).name || biz}`, width: 620, body,
    actions: [
      { label: "Close", onClick: close => close() },
      { label: closed ? "Reopen" : "Mark done", primary: true, onClick: () => again(t.id, {set:{status:closed ? 'open' : 'done'}}) },
    ],
    onOpen(el) {
      if (typeof storyRelatedChips === 'function') storyRelatedChips(el.querySelector('#td-stories'), biz, 'tasks', t.id, () => m.close());
      else el.querySelector('#td-stories').textContent = 'No linked stories.';
      el.querySelector('#td-reload').onclick = async()=>{try {const fresh=await tFetchTask(biz,t.id);if(!m.el.isConnected)return;m.close();tDetail(fresh);}catch(error){toast(error.message);}};
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
      wb.onclick = () => dropdown(wb, tWhoItems(biz), v => { if (v !== o) again(t.id, { set: { owner: v } }); }, o);
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
function tWireHead(p, from, onTab = tSetView) {
  const s = p.querySelector("#tsearch");
  if (s) {
    s.oninput = () => { clearTimeout(tSearchT); tSearchT = setTimeout(() => { tSearch = s.value; paintTasks(); }, 80); };
    s.onkeydown = e => { if (e.key === "Escape") { e.stopPropagation(); s.value = ""; tSearch = ""; paintTasks(); s.blur(); } };
  }
  const tabs = [...p.querySelectorAll(".ttab")];
  const select = v => {
    onTab(v);
    const selected = p.querySelector(`.ttab[data-v="${v}"]`);
    if (selected) selected.focus({ preventScroll: true });
  };
  tabs.forEach((b, i) => {
    b.onclick = () => { if (!b.classList.contains("on")) select(b.dataset.v); };
    b.onkeydown = e => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      e.preventDefault();
      const next = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      select(tabs[next].dataset.v);
    };
  });
  moveTInd(p, from);
}
// .thead (tabs included) is rebuilt from scratch on every switch, so .tind is a brand-new node each
// time: left to its own devices it'd render at its CSS default (translateX(0), i.e. sat under the
// first tab) for a frame before jumping to the real target. Instead, plant it at the tab we came FROM
// with transitions off, force a reflow, then re-enable transitions and slide to the real target.
function moveTInd(p, from) {
  const on = p.querySelector(".ttab.on"), ind = p.querySelector(".tind");
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
    const t = taskById(row.dataset.key || row.dataset.id); if (!t || tPersonalError) return;
    if (e.target.closest(".ball")) return void tSetStatus(t, tNext(t.status));
    const who = e.target.closest(".who");
    if (who) { const nx = tWhoLeft(t.owner || ""); if (nx == null) return toast("no agents in this business"); return void tSetOwner(t, nx); }
    tDetail(t);
  });

  // right click: blocked on the ball, the human owner on the assignee
  panel.addEventListener("contextmenu", e => {
    const row = e.target.closest(".trow"); if (!row) return;
    const t = taskById(row.dataset.key || row.dataset.id); if (!t || tPersonalError) return;
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
    handle.focus({ preventScroll: true });
  });

  tSetView(tView);
  refreshTasks();
  setInterval(() => {
    if (state.biz && state.biz !== tLastBiz) {
      tLastBiz = state.biz;
      // Completion timers retain the originating task and business across navigation.
      //
      refreshTasks();
      if (tView === "activity") { tLog = []; tLogSig = null; tActivityRefresh(); }
      // Flush the old business draft before opening the next Notes view.
      if (tView === "notes") { ntFlush(); ntSel = null; ntSearch = ""; ntNotes = []; tPaintNotes(); }
      if (tView === "signoffs") sgBusinessChanged();
      if (tView === "routines") tPaintRoutines();
      if (tView === "uploads") tPaintUploads();   // resets its form for the new business itself
    }
  }, 500);
  setInterval(() => {
    refreshTasks();
    if (!state.biz && state.scope !== "global") return;
    if (tView === "activity") tActivityRefresh();
    if (tView === "routines") rtRefresh();
    if (tView === "signoffs" && tPanelOpen()) sgRefresh();
  }, 12000);
})();
