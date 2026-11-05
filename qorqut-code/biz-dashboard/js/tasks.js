// Personal task drawer aggregates the current human's business tasks.
// state.tasks holds every Task of the selected business for its Tasks page. Every personal row
// and mutation carries its owning business; IDs alone are not globally unique.
// Completion holds the row briefly, and keeps its business even during navigation.

// The five fixed Task states; the engine owns them and they are not configurable.
const T_ORDER = ["open", "working", "waiting", "done"];        // the left-click cycle
const T_CLOSED = ["done", "cancelled"];
const T_STATES = ["open", "working", "blocked", "waiting", "done", "cancelled"];   // the detail's status picker
const T_LABEL = { open: "Open", working: "Working", blocked: "Blocked", waiting: "Waiting", done: "Done", cancelled: "Cancelled" };
const T_DONE_HOLD = 550;     // ms a completing row stays put before it sinks (Notaru)
const T_DAY = 86400000;

const SVG_TPLUS = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5v14"/></svg>`;
const SVG_TSEARCH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>`;
const SVG_TINBOX = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>`;

const tShort = o => String(o || "").replace(/^(agent|human|system):/, "") || "unassigned";
const tAssignees = t => t.owners || [];
// a due date is a calendar day: parse the parts as a LOCAL date (Notaru's parseDueDay)
const tDay = s => { const [y, m, d] = String(s).slice(0, 10).split("-").map(Number); return new Date(y, (m || 1) - 1, d || 1).getTime(); };
const tToday = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime(); };
const tDays = due => Math.round((tDay(due) - tToday()) / T_DAY);
const tClosed = t => T_CLOSED.indexOf(t.status) >= 0;
// a closed row sinks to the bottom of its section, except while its completion is still being held in place
const tSunk = t => tClosed(t) && !tCompleting.has(tKey(t)) && !taskStateSnapshot(tSlug(t), t.id).pending;
const tNext = s => { const i = T_ORDER.indexOf(s); return i < 0 ? "open" : T_ORDER[(i + 1) % T_ORDER.length]; };

let tSearch = "";
let tSnoozeFilter = 'active';
let tAdding = null;                 // key of the open inline add row
let tPainting = false;              // rows are being replaced: a blur fired by that detachment is not a commit
let tPaintSig = null;               // signature of what's currently on screen; a background refresh that still
                                     // matches it skips the rebuild entirely (no flicker, no lost hover/tooltip)
const tCompleting = new Set();      // ids shown done but still in place
const tDoneT = new Map();           // presentation-only completion hold; saves start immediately
let tGen = 0;                       // bumped when a write starts; a refresh that began before it is stale when it lands
let tLastBiz = null, tSoonT = null, tInside = false, tSearchT = null;
state.tasks = [];
state.tasksBiz = null; state.tasksError = null;   // which business state.tasks belongs to, or failed to load
state.myTasks = [];
let tPersonalLoaded = false, tPersonalError = null, tFetch = 0;

// ---------------------------------------------------------------- view (tabs)
// Retired drawer views are no longer offered by shared task headers.
const T_OVERVIEW_VIEWS = [];
const T_VIEWS = ["tasks", "notes", "globalNotes", "businessNotes", "signoffs", "uploads", "routines", ...T_OVERVIEW_VIEWS, "activity"];
const T_GLOBAL_VIEWS = ["notes", "tasks", "signoffs", "uploads", "routines", ...T_OVERVIEW_VIEWS, "activity"];
const T_VIEW_LABEL = { status: "Agent status", tasks: "Tasks", globalTasks: "Global tasks", activity: "Activity", notes: "Notes", globalNotes: "Global Notes", businessNotes: "Project notes", signoffs: "Signoffs", uploads: "Uploads", routines: "Routines", charts: "Charts", costs: "Costs", tables: "Tables", inbox: "Agent inbox", focus: "Pinned", attentionBoard: "Attention board", calendar: "Calendar", browser: "Browser", viewbox: "Viewbox", desktop: "Desktop", memory: "Working memory" };
const T_VIEW_ICON = {
  calendar: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18M8 15h2M14 15h2"/></svg>`,
  status: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M7 15v-3m5 3V8m5 7v-5"/></svg>`,
  attentionBoard: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9h2m3 0h5M7 15h2m3 0h5"/></svg>`,
  focus: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3h8l-1 6 4 4v2H5v-2l4-4-1-6ZM12 15v7"/></svg>`,
  browser: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M6.5 6.5h.01M9 6.5h.01"/></svg>`,
  desktop: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5h16v11H4zM8 20h8m-4-4v4"/></svg>`,
  memory: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" aria-hidden="true"><path d="M4 4h12l4 4v12H4zM8 9h5M8 13h8M8 17h8M16 4v4h4"/></svg>`,
  inbox: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" aria-hidden="true"><path d="M4 4h16l2 12v4H2v-4L4 4Z"/><path d="M2 16h6l2 3h4l2-3h6"/></svg>`,
  signoffs: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 5H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-4"/><rect x="9" y="2" width="6" height="6" rx="1"/><path d="m9 14 2 2 4-4"/></svg>`,
  tasks: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 10.656V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h12.344"></path><path d="m9 11 3 3L22 4"></path></svg>`,
  notes: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.67 19a2 2 0 0 0 1.416-.588l6.154-6.172a6 6 0 0 0-8.49-8.49L5.586 9.914A2 2 0 0 0 5 11.328V18a1 1 0 0 0 1 1z"></path><path d="M16 8 2 22"></path><path d="M17.5 15H9"></path></svg>`,
  routines: `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`,
  charts: NAV_PROGRESS_ICON,
  costs: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${AICONS.calculator}</svg>`,
  tables: typeof aicon === 'function' ? aicon('database', 'currentColor', 20) : '',
  uploads: ICO_UPLOAD,  // defined in core.js: the same glyph leads every upload button
  activity: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"></path></svg>`,
};
T_VIEW_ICON.viewbox = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><rect x="11" y="11" width="7" height="5" rx="1"/></svg>`;
T_VIEW_ICON.globalNotes = T_VIEW_ICON.businessNotes = T_VIEW_ICON.notes;
const embeddedTaskPane = typeof embeddedAgentPane !== 'undefined' && embeddedAgentPane;
let tView = embeddedTaskPane ? 'tasks' : globalThis.ovInitialView?.() || ls("tview") || "tasks";
T_VIEW_ICON.globalTasks = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h7M12 3c-4 5-4 13 0 18M12 3c2 3 3 5 3 7m-4 5 3 3 7-8"/></svg>`;
if (tView === "journal") tView = "notes";
if (T_VIEWS.indexOf(tView) < 0) tView = "tasks";

// activity (log) view state
let tLog = [];
let tLogSig = null;                 // signature of what's on screen for the log list; skips a no-op rebuild

// notes view state (ntSel/ntNotes/ntSearch/...) lives in js/notes.js the same way.
// uploads view state (upForm/upDone/...) lives in js/uploads.js; it has no editor, so nothing to flush.

const tPanelOpen = () => { const w = $("#taskswrap"); return !!w && w.classList.contains("open"); };
const tBusiness = t => t.business || (t.business_id && (state.bizs || []).find(b => b.id === t.business_id || b.business_id === t.business_id)) || tBiz();
const tSlug = t => tBusiness(t).slug;
const tKey = t => JSON.stringify([t.business_id || tBusiness(t).id || tSlug(t), t.id]);
const taskById = (id, biz = state.biz) => typeof id === 'object' ? id : [...(state.myTasks || []), ...(state.tasks || [])].find(t => tKey(t) === id || t.id === id && tSlug(t) === biz);
const tPersonalRows = () => (state.myTasks || []).filter(t => state.scope === 'global' || tSlug(t) === state.biz);
const tBiz = (biz = state.biz) => (state.bizs || []).find(x => x.slug === biz) || {};
// the business owner, as biztask spells it: `human:<owner from business.yaml>` (server's owner_actor fallback)
const tHuman = () => "human:" + (tBiz().owner || "dashboard");
const tAgents = () => (tBiz().agents || []).map(a => "agent:" + a);

// A repaint replaces every row, so it must not fire while a dropdown is open: it would lose its anchor.
function tEditingNow() { return !!document.querySelector(".menu"); }

// Kept for old callers while their destinations are retired; never recreate a drawer.
function openTasks() { return false; }

// ---------------------------------------------------------------- data
function refreshTasksSoon() { clearTimeout(tSoonT); tSoonT = setTimeout(refreshTasks, 500); }

// A refresh that was in flight when a write started must not land on top of the optimistic
// update (Notaru cancels its queries in onMutate for the same reason): the write's own refresh follows.
let tRefreshPending = null;
// An embedded pane reads tasks only for its open drawer or its business Tasks section.
const tReadsNeeded = () => !embeddedTaskPane || tPanelOpen() || !!document.querySelector('[data-business-task-page]');
function refreshTasks() {
  if (!tReadsNeeded()) return Promise.resolve();
  const localNeeded = state.scope !== 'global' && !!state.biz;
  const key = JSON.stringify([tGen, state.biz, localNeeded]);
  if (tRefreshPending?.key === key) return tRefreshPending.promise;
  const pending = {key};
  pending.promise = tLoadTasks(localNeeded).finally(() => { if (tRefreshPending === pending) tRefreshPending = null; });
  tRefreshPending = pending;
  return pending.promise;
}
async function tLoadTasks(localNeeded) {
  const gen = tGen, biz = state.biz, request = ++tFetch;
  const current = () => gen === tGen && request === tFetch;
  const profilesBefore = globalAgentProfiles;
  loadGlobalAgentProfiles().then(() => {
    if (!current() || profilesBefore === globalAgentProfiles) return;
    tPaintSig = null;
    if (tView === 'tasks') paintTasks();
    if (typeof _agpad !== 'undefined' && _agpad?.view === 'tasks') {
      _agpad.bodyHtml = null; agpadPaint(_agpad);
    }
  }).catch(() => {}); // A directory failure must not hide otherwise usable tasks.
  const personal = api('/me/tasks').then(data => {
    if (!current()) return;
    if (!Array.isArray(data?.tasks)) throw new Error('Invalid task feed');
    state.myTasks = data.tasks;
    tPersonalLoaded = true; tPersonalError = null;
    state.myTasks.forEach(t => Object.assign(t, taskStateObserve(tSlug(t), t)));
  }).catch(() => {
    if (current()) tPersonalError = 'Could not refresh your tasks. Retry before making changes.';
  }).then(() => {
    if (!current()) return;
    if (tVisSig() !== tPaintSig) paintTasks();
  });
  const local = localNeeded && biz ? api(`/biz/${encodeURIComponent(biz)}/tasks?all=1`).then(data => {
    if (!current() || biz !== state.biz) return;
    if (!Array.isArray(data)) throw new Error('Invalid task list');
    state.tasks = data; state.tasksBiz = biz; state.tasksError = null;
    state.tasks.forEach(t => Object.assign(t, taskStateObserve(tSlug(t), t)));
  }).catch(() => {
    if (current() && biz === state.biz && state.tasksBiz !== biz) state.tasksError = biz;
  }).then(() => {
    if (current() && typeof businessTasksPaint === 'function') businessTasksPaint();
  }) : Promise.resolve();
  await Promise.allSettled([personal, local]);
}
async function tFetchTask(biz, id) {
  const result = await api(`/biz/${encodeURIComponent(biz)}/task/${encodeURIComponent(id)}`);
  if (!result?.task) throw new Error('Task is no longer available.');
  return {...result.task, business: tBiz(biz)};
}


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
    if (r?.error) throw Object.assign(new Error(typeof r.error === 'string' ? r.error : r.error.message), {code: r.error?.code, httpStatus: r.httpStatus});
    if (r?.status === 'signoff_required') { toast('Submitted for project approval.'); await refreshTasks(); return null; }
    tRelease(); await refreshTasks();
    if (typeof refreshSideSoon === 'function' && state.biz === tSlug(task)) refreshSideSoon();
    return r;
  } catch (error) { toast(uiFailure(error)); onError?.(uiFailure(error)); await refreshTasks(); return null; }
}

async function tadd(fields) {
  tGen++;
  const r = await post(`/biz/${state.biz}/task`, {owner: tHuman(), ...fields});
  if (r && r.error) { toast("Couldn't save. Try again"); return null; }
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

const tDotHtml = s => `<span class="sdot ${esc(s)}"></span>`;

// a cheap fingerprint of every visible row's rendered fields (order matters: it doubles as a position
// check, since a sunk task lands in a different spot than an open one). `state.biz` is part of it too:
// two empty task lists (no business picked vs. a business with nothing in it) render different copy.
function tVisSig() {
  return JSON.stringify([state.scope, state.biz, tSnoozeFilter, tPersonalLoaded, tPersonalError, tPersonalRows().filter(tMatch).map(t =>
    [tKey(t), t.status, tAssignees(t).join(','), t.title, t.due || "", (t.tags || []).join(","), t.created_by, t.created_by_name, tSunk(t), t.snoozed_until, taskSnoozed(t)])]);
}

// ---------------------------------------------------------------- assignee
// none | agent (one of the business's agents) | human (the owner) | other (any other actor)
function tWhoKind(o) {
  if (!o) return "none";
  if (o === tHuman()) return "human";
  if (tAgents().indexOf(o) >= 0) return "agent";
  return "other";
}

// the dot: dashed ring unassigned, a plain dot for an agent, cream for the owner (may carry
// their initial), gray for anyone else
function tWhoDot(o, biz = state.biz) {
  const kind = o?.startsWith('human:') ? 'founder' : o?.startsWith('agent:') ? 'agent' : 'unknown';
  const name = tShort(o), business = tBiz(biz);
  const key = Object.keys(business.agent_meta || {}).find(key => key.toLowerCase() === name.toLowerCase());
  const agent = biz === state.biz && (state.agents || []).find(a => a.name?.toLowerCase() === name.toLowerCase());
  return identityAvatar(kind, name, agent || business.agent_meta?.[key] || {}, name);
}
function tAssigneeLabel(o, biz = state.biz) {
  if (!o) return 'Unassigned';
  if (o.startsWith('human:')) return tBiz(biz).owner || 'Owner';
  if (o.startsWith('agent:')) return o.slice(6) || 'Agent';
  return 'Unavailable assignee';
}
function tAssigneeAvatars(task) {
  const owners = tAssignees(task);
  return `<span class="task-assignee-column">${owners.slice(0, 2).map(actor => `<button type="button" class="task-assignee-avatar" data-task-assignees title="Assigned to ${esc(tAssigneeLabel(actor,tSlug(task)))}">${tWhoDot(actor, tSlug(task))}</button>`).join('')}${owners.length > 2 ? `<button type="button" class="task-assignee-avatar task-assignee-more" data-task-assignees title="${tWhoTip(task)}">+${owners.length - 2}</button>` : ''}</span>`;
}
function tAssignerAvatars(task) {
  const business = {...tBiz(tSlug(task)), ...tBusiness(task)};
  const actors = task.created_by ? [{id: task.created_by, name: task.created_by_name, kind: task.created_by_kind}] : [];
  return `<span class="task-assigner-column" aria-label="Created by">${(actors.length ? actors : [{name:'Unknown creator'}]).map(actor => {
    const name = actor.name || globalAgentProfile(actor.id)?.display_name || 'Unknown creator';
    const agentName = Object.keys(business.agent_meta || {}).find(agent => agent.toLowerCase() === name.toLowerCase());
    const agent = globalAgentProfile(actor.id) || (state.agents || []).find(agent => ((actor.id && (agent.principal_id === actor.id || agent.agent_id === actor.id)) || (name && (agent.display_name === name || agent.name?.toLowerCase() === name.toLowerCase()))) && tSlug(task) === state.biz);
    const kind = actor.kind === 'agent' || agent || agentName ? 'agent' : actor.kind === 'human' || name === business.owner ? 'founder' : 'unknown';
    return identityAvatar(kind, name, agent || business.agent_meta?.[agentName] || {}, name === 'Unknown creator' ? name : `Created by ${name || 'unknown'}`, true);
  }).join('')}</span>`;
}

const tWhoInner = o => `<span class="who inl ${tWhoKind(o)}">${tWhoDot(o)}</span>`;
function tWho(t) {
  const owners=tAssignees(t),o=owners[0] || '';
  return `<button class="who ${tWhoKind(o)}" type="button" aria-label="Assignees: ${esc(owners.map(owner=>tAssigneeLabel(owner,tSlug(t))).join(', ') || 'Unassigned')}">${owners.length>1?`<span class="task-assignee-count">${owners.length}</span>`:tWhoDot(o)}</button>`;
}
function tWhoTip(t, detailsOnly = false) {
  return esc(tAssignees(t).map(o=>tAssigneeLabel(o,tSlug(t))).join(', ') || 'Unassigned');
}
// Assignment choices are the business's assignees_list: its IDs and names are authoritative.
// A choice also matches a task's existing kind:name owner, so current assignees stay checked.
async function tWhoItems(biz = state.biz) {
  const {assignees} = await workCall(biz, 'assignees_list');
  const items = [{ value: "", label: "Unassigned", html: `<span class="towner">${tWhoInner("")}<span class="tn">Unassigned</span></span>` }];
  for (const person of assignees || []) {
    const name = person.display_name || person.name || 'Unnamed', owner = `${person.kind}:${person.name || person.display_name}`;
    const avatar = identityAvatar(person.kind === 'human' ? 'founder' : person.kind === 'agent' ? 'agent' : 'unknown', name, globalAgentProfile(person.id) || person, name);
    items.push({ value: person.id, owner, label: name, html: `<span class="towner"><span class="who inl">${avatar}</span><span class="tn">${esc(name)}</span><span class="tsub">${person.is_owner ? 'owner' : esc(person.kind)}</span></span>` });
  }
  return items;
}

// ---------------------------------------------------------------- render
// Notaru's countdown chip: days from the date-only due
function tDue(t) {
  if (!t.due || tClosed(t)) return "";
  const n = tDays(t.due);
  const text = n === 0 ? "today" : n > 0 ? `${n}d` : `-${-n}d`;
  const tone = n < 0 ? "over" : n <= 1 ? "soon" : "far";
  return `<span class="tdl ${tone}" title="Due ${esc(t.due)}">${text}</span>`;
}

function taskShowsState(task) { return !TASK_SPECIAL_TYPES.includes(task?.task_type); }
function tBall(t, detailsOnly = false) {
  if (!taskShowsState(t)) return `<span class="task-type-marker">${taskTypeIcon(t.task_type)}</span>`;
  const s = t.status, label = T_LABEL[s] || s;
  return `<button class="ball ${esc(s)}" type="button"
    aria-label="Status: ${esc(label)}. Click to change."><span class="core"></span></button>`;
}

// the deadline slot is reserved on every row in every section, whether or not any task anywhere has a
// due date, so assignee/status balls line up identically across sections
// A child Task shows its parent work group as a chip; everything else related is reached from the detail.
function tRelatedItems(task) {
  return task.parent ? [{type: 'task', id: task.parent.id, label: task.parent.title}] : [];
}
const tChipLabel = label => {const chars=Array.from(String(label));return chars.length>20?chars.slice(0,19).join('')+'…':chars.join('');};
function tChips(task) {
  const chip=(label,title,extra='')=>`<button type="button" class="task-chip" title="${esc(title)}" ${extra}>${esc(tChipLabel(label))}</button>`;
  const related=tRelatedItems(task).map((item,i)=>chip(item.label,'Parent: '+item.label,`data-task-related="${i}"`)).join('');
  return `<span class="task-chips" ${related ? 'aria-label="Parent task"' : 'aria-hidden="true"'}>${related}</span>`;
}

function taskRowLayout(t, {attributes = '', leading = '', assigner = '', trailing = '', closed = false, detailsOnly = false} = {}) {
  return `<div class="trow ${esc(t.status)}${closed ? ' off' : ''}${detailsOnly ? '' : ' personal-task'}" ${attributes}>${detailsOnly ? leading : `<span class="task-business-column">${leading}</span>`}<div class="task-headline">${tBall(t, detailsOnly)}<p class="ttl">${esc(t.title)}${customerStateChip(t)}${taskSnoozed(t) ? `<small class="task-snooze-label">${esc(taskSnoozeLabel(t))}</small>` : ''}</p></div>${assigner}${trailing}</div>`;
}

function tRow(t, detailsOnly = false) {
  Object.assign(t, taskStateObserve(tSlug(t), t));
  const business = tBusiness(t);
  return taskRowLayout(t, {
    attributes: `data-id="${esc(t.id)}" data-key="${esc(tKey(t))}" data-business="${esc(business.slug || '')}"`,
    leading: detailsOnly ? (tDue(t) || '<span class="tdl"></span>') + tWho(t) : taskBusinessLabel(business.name || business.slug),
    assigner: tAssignerAvatars(t),
    trailing: detailsOnly ? '' : (tDue(t) || '<span class="tdl"></span>') + tAssigneeAvatars(t) + tChips(t),
    closed: tClosed(t), detailsOnly,
  });
}

function tWireRows(body, detailsOnly = false) {
  body.querySelectorAll(".trow").forEach(row => {
    const t = taskById(row.dataset.key || row.dataset.id); if (!t) return;
    const ball = row.querySelector('.ball');
    taskStateBind(ball, tSlug(t), t);
    if (ball) {
      ball.onclick = event => { event.stopPropagation(); tSetStatus(t, tNext(t.status)); };
    }
    row.querySelectorAll('[data-task-related]').forEach(button=>button.onclick=event=>{
      event.stopPropagation();const item=tRelatedItems(t)[Number(button.dataset.taskRelated)];
      if(item)contextOpenItem(tSlug(t),item).catch(e=>toast(uiFailure(e,{load:true})));
    });
    row.querySelectorAll('[data-task-assignees]').forEach(button=>button.onclick=event=>{event.stopPropagation();tDetail(t);});
    const who = row.querySelector(".who");
    if (who && typeof tip === "function") tip(who, () => tWhoTip(t, detailsOnly));
  });
}

const tAddRow = key => `<div class="tadd" data-b="${esc(key)}"><span class="tdl"></span><span class="who ph"><span class="wd none"></span></span><span class="ball ph"><span class="core"></span></span><input id="taddin" placeholder="Task name…" autocomplete="off" spellcheck="false"></div>`;

function tSection(key, rows, { label = key, canAdd = true, detailsOnly = false } = {}) {
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
  if (bodyId === 'tbody' && state.scope !== 'global') views = views.filter(v => v !== 'notes');
  if (bodyId !== 'tbody' || embeddedTaskPane) views = views.filter(v => !T_OVERVIEW_VIEWS.includes(v));
  return `<div class="ttabs" role="tablist" aria-label="${bodyId === "tbody" ? "Project panel" : "Agent pane"}"><div class="tind" hidden></div>${views.map(v => {
    const agentTasks = bodyId !== 'tbody' && v === 'globalTasks';
    // An undeclared workspace type still gets a labeled, icon-free tab.
    return `<button class="ttab${v === active ? " on" : ""}" id="${esc(tTabId(v, bodyId))}" role="tab" aria-selected="${v === active}" aria-controls="${esc(bodyId)}" type="button" data-v="${v}" aria-label="${esc(agentTasks ? 'Tasks' : T_VIEW_LABEL[v] || v)}">${T_VIEW_ICON[agentTasks ? 'tasks' : v] || ''}</button>`;
  }).join("")}</div>`;
}

function tHead({ view = tView, views = T_VIEWS, bodyId = "tbody", title = "", taskTools = true } = {}) {
  const tools = view === "tasks" && taskTools;
  return `<div class="thead nott">
    ${tTabs(bodyId === "tbody" ? (state.scope === "global" ? T_GLOBAL_VIEWS : ["globalNotes", "businessNotes", ...views.filter(v => !['globalTasks', 'globalNotes', 'notes', 'businessNotes'].includes(v))]) : views.filter(v => v !== 'globalTasks' || state.scope === 'global'), view, bodyId)}
    ${title ? `<div class="ttop"><h1>${esc(title)}</h1></div>` : ""}
    ${tools ? `<div class="task-toolbar"><label class="tsearch">${SVG_TSEARCH}<input id="tsearch" type="search" aria-label="Search tasks" autocomplete="off" spellcheck="false"></label><select id="task-snooze-filter" aria-label="Task visibility">${['active','snoozed','all'].map(v=>`<option value="${v}" ${tSnoozeFilter===v?'selected':''}>${v[0].toUpperCase()+v.slice(1)}</option>`).join('')}</select><button class="btn" id="task-new" type="button"${state.biz ? "" : " hidden"}>${SVG_TPLUS}New task</button></div>` : ""}
  </div><div class="tbody" id="${esc(bodyId)}" role="tabpanel" aria-labelledby="${esc(tTabId(view, bodyId))}"></div>`;
}

function tEmptyHtml(title, { sub = "", create = false } = {}) {
  return `<div class="tempty"><div class="ic">${SVG_TINBOX}</div><p class="t">${esc(title)}</p>` +
    (sub ? `<p class="s">${esc(sub)}</p>` : "") +
    (create ? `<button class="pill create" type="button">Create task</button>` : "") + `</div>`;
}

function paintTasks() {
  const p = $("#taskspanel"); if (!p) return;
  if (!p.querySelector(".thead")) { p.innerHTML = tHead(); tWireHead(p); }
  // the tasks rows own #tbody only while the Tasks tab is active: another view (Activity, Journal) is
  // painted by its own function, and this repaint must not stomp it
  if (tView !== "tasks") return;
  const newTask=p.querySelector("#task-new");if(newTask)newTask.hidden=!state.biz;
  if (tEditingNow()) return;

  const prev = $("#taddin");
  const keep = prev ? prev.value : "";
  const hadFocus = prev && document.activeElement === prev;

  const rows = tPersonalRows().filter(tMatch).sort((a,b) => Number(tSunk(a))-Number(tSunk(b)) || String(a.due || '9999').localeCompare(b.due || '9999') || a.title.localeCompare(b.title) || tKey(a).localeCompare(tKey(b)));
  const open = rows.filter(t => !tSunk(t) && (tSnoozeFilter === 'all' || taskSnoozed(t) === (tSnoozeFilter === 'snoozed')));
  let html = tPersonalError ? `<p class="personal-task-error" role="alert">${esc(tPersonalError)} <button class="ghost" id="tasks-retry">Retry</button></p>` : '';
  if (!tPersonalLoaded && !tPersonalError) html += tEmptyHtml('Loading your tasks…');
  else {
    html += open.length ? `<div class="personal-task-list">${open.map(t => tRow(t)).join('')}</div>` : tEmptyHtml(tSearch ? 'No matching tasks.' : tSnoozeFilter === 'snoozed' ? 'No snoozed tasks.' : 'Nothing waiting on you.');
    if (state.biz && tAdding) html += tAddRow(tAdding);
  }
  const taskBody = $("#tbody"); if (!taskBody) return;
  let body = taskBody;
  if (state.scope === 'global') {
    // Keep Global's editor mounted during business-task polling and state updates.
    if (!taskBody.querySelector('[data-business-tasks]')) {
      taskBody.innerHTML = '<div data-global-tasks></div><section class="business-task-section" aria-label="Project tasks"><h3 class="global-task-heading">Project</h3><div data-business-tasks></div></section>';
      globalTasksMount(taskBody.querySelector('[data-global-tasks]'), {heading: 'Global', compact: true});
    }
    taskBody.querySelector('[data-global-tasks]').setTaskFilters(tSearch, tSnoozeFilter);
    body = taskBody.querySelector('[data-business-tasks]');
  }
  // Chrome fires blur on the focused add input before detaching it (isConnected is still true then)
  tPainting = true;
  try { body.innerHTML = html; } finally { tPainting = false; }
  tPaintSig = tVisSig();   // record what's now on screen so a later background refresh can skip a no-op rebuild

  tWireRows(body);
  body.querySelector("#tasks-retry")?.addEventListener("click", refreshTasks);
  if (tPersonalError) body.querySelectorAll(".ball").forEach(b => b.disabled = true);
  const add = $("#taddin");
  if (add) {
    if (keep && prev && prev.closest(".tadd") && prev.closest(".tadd").dataset.b === tAdding) add.value = keep;
    if (hadFocus || tAdding) { add.focus(); add.setSelectionRange(add.value.length, add.value.length); }
    tWireAdd(add);
  }
}

// ---------------------------------------------------------------- view switch (tabs)
// A tab click owns the whole panel repaint: it rebuilds .thead (tabs and search, the
// tabs themselves) and then paints #tbody for the view landed on. Only a tab switch does this — the
// intervals below only ever touch the view that's currently active.
function tSetView(v) {
  if (v === 'notes' && state.scope !== 'global' && state.biz) v = 'tasks';
  if (embeddedTaskPane && T_OVERVIEW_VIEWS.includes(v)) v = 'tasks';
  if (T_VIEWS.indexOf(v) < 0 || state.scope === "global" && !T_GLOBAL_VIEWS.includes(v) || state.scope !== "global" && ["globalNotes", "businessNotes"].includes(v) && !state.biz) v = "tasks";
  if (["notes", "globalNotes", "businessNotes"].includes(tView) && v !== tView && ntEditState) { ntFlush().then(saved=>{if(saved)tSetView(v);}); return; }
  const from = tView;
  const tabFocused = document.activeElement && document.activeElement.closest(".ttabs");
  tView = v; ls("tview", v);
  const p = $("#taskspanel"); if (!p) return;
  globalThis.ovDispose?.();
  p.innerHTML = tHead(); tWireHead(p, from);
  if (from === "signoffs" && v !== "signoffs" && typeof tPaintSignoffs === "function") tPaintSignoffs();
  if (v === "tasks") { tPaintSig = null; paintTasks(); }
  else if (v === "activity") { tLogSig = null; tPaintActivity(); tActivityRefresh(); }
  else if (["notes", "globalNotes", "businessNotes"].includes(v)) { tPaintNotes(); }
  else if (v === "signoffs") { tPaintSignoffs(); }
  else if (v === "uploads") { tPaintUploads(); }
  else if (v === "routines") { tPaintRoutines(); }
  else if (T_OVERVIEW_VIEWS.includes(v)) { globalThis.ovPaint?.(v); }
  globalThis.ovViewChanged?.(v);
  if (tabFocused) p.querySelector(`.ttab[data-v="${v}"]`).focus({ preventScroll: true });
  if (typeof sgAttentionPaint === "function") sgAttentionPaint();
}

// ---------------------------------------------------------------- activity view
// Business activity log, reusing side.js's global logHtml()/logTips() so the row look (day
// gaps) is shared formatting with the other logHtml()/logTips() callers.
function tActivitySig() { return JSON.stringify([state.biz, tLog.map(e => e.id || `${e.ts}|${e.summary}`)]); }

function tActivityHtml(rows, emptyMessage = "Nothing logged yet.") {
  return rows.length ? `<div class="tlog">${logHtml(rows)}</div>` : tEmptyHtml(emptyMessage);
}

function tPaintActivity() {
  if (tView !== "activity") return;
  if (typeof acActivityActive === "function" && acActivityActive()) return acListPaint();
  const body = $("#tbody"); if (!body) return;
  if (!state.biz) { body.innerHTML = tEmptyHtml("Pick a project first."); tLogSig = null; return; }
  const sig = tActivitySig();
  if (sig === tLogSig && body.querySelector(".tlog")) return;   // nothing changed: skip the rebuild, keep tooltips alive
  tLogSig = sig;
  body.innerHTML = tActivityHtml(tLog);
  const wrap = body.querySelector(".tlog");
  if (wrap) logTips(wrap, tLog);
}

async function tActivityRefresh() {
  if (typeof acActivityActive === "function" && acActivityActive()) return acListRefresh();
  if (!state.biz) { tLog = []; return tPaintActivity(); }
  const biz = state.biz;
  const r = await api(`/biz/${biz}/log?n=60`);
  if (biz !== state.biz || tView !== "activity") return;   // stale by the time it landed
  tLog = Array.isArray(r) ? r : [];
  tPaintActivity();
}

// ---------------------------------------------------------------- add
function tOpenAdd() {
  tAdding = "tasks";
  paintTasks();
  const el = $("#taddin"); if (el) { el.focus(); el.scrollIntoView({ block: "nearest" }); }
}

function tWireAdd(inp) {
  let settled = false;
  const key = tAdding;   // the row this input was opened in, whatever happens to the global later
  const commit = async () => {
    if (settled) return; settled = true;
    const title = inp.value.trim();
    if (tAdding === key) tAdding = null;
    if (!title) return paintTasks();
    inp.disabled = true;
    await tadd({ title });
    paintTasks();
  };
  inp.onkeydown = e => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); settled = true; if (tAdding === key) tAdding = null; paintTasks(); }
  };
  // Notaru commits on blur too; the blur a repaint fires while detaching the input is not one
  inp.onblur = () => { if (!tPainting && inp.isConnected && !settled) commit(); };
}

// ---------------------------------------------------------------- row actions
// The shared controller owns writes; this timer only keeps a completed row in place.
function tSetStatus(t, next) {
  if (!taskShowsState(t) || !next || next === t.status || tPersonalError) return;
  const id = tKey(t);
  clearTimeout(tDoneT.get(id)); tDoneT.delete(id); tCompleting.delete(id);
  if (next === 'done') {
    tCompleting.add(id);
    tDoneT.set(id, setTimeout(() => {
      tDoneT.delete(id); tCompleting.delete(id); paintTasks();
    }, T_DONE_HOLD));
  }
  return taskStateSet(tSlug(t), t, next);
}

// Bind state controls without replacing their DOM nodes or interrupting input drafts.
function taskStateBind(button, scope, task, onChange) {
  if (!button) return;
  Object.assign(task, taskStateObserve(scope, task));
  button.dataset.taskStateKey = JSON.stringify([scope || null, task.id]);
  button._taskStateRecord = task;
  button._taskStateChange = onChange;
  taskStatePaintControl(button, taskStateSnapshot(scope, task.id));
}
function taskStatePaintControl(button, snapshot) {
  if (!snapshot?.task) return;
  const task = button._taskStateRecord;
  Object.assign(task, snapshot.task);
  for (const status of T_STATES) button.classList.toggle(status, status === task.status);
  button.setAttribute('aria-label', (button.classList.contains('agfocus-task-state') ? '' : 'Status: ') + (T_LABEL[task.status] || task.status));
  button.setAttribute('aria-busy', String(!!snapshot.pending));
  button.setAttribute('aria-invalid', String(!!snapshot.error));
  button.dataset.saving = String(!!snapshot.pending);
  button.dataset.saveError = String(!!snapshot.error);
  button.removeAttribute('title');
  const row = button.closest('.trow');
  if (row) {
    for (const status of T_STATES) row.classList.toggle(status, status === task.status);
    row.classList.toggle('off', tClosed(task));
  }
  button._taskStateChange?.(snapshot);
}
let tStatePaintPending = false;
taskStateSubscribe((scope, id, snapshot) => {
  let regroup = false;
  for (const task of [...(state.myTasks || []), ...(state.tasks || [])]) {
    if (task.id === id && (tSlug(task) || null) === scope) {
      regroup ||= tClosed(task) || tClosed(snapshot.task) || !!snapshot.error;
      Object.assign(task, snapshot.task);
    }
  }
  const key = JSON.stringify([scope, id]);
  for (const button of document.querySelectorAll('[data-task-state-key]')) {
    if (button.dataset.taskStateKey === key) taskStatePaintControl(button, snapshot);
  }
  if (snapshot.error) toast(uiFailure(snapshot.error));
  if (regroup && !snapshot.pending && !tStatePaintPending) {
    tStatePaintPending = true;
    Promise.resolve().then(() => { tStatePaintPending = false; paintTasks(); });
  }
});

// ---------------------------------------------------------------- detail (Notaru's side sheet, as a modal)
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
  tDetail(t);
}

function tDetail(t,{host=null,onClose=null,recordPanelOpen=false,embedded=false,navigate=null,relatedMount=null,onMount=null,onCanonical=null}={}) {
  if (!host && typeof modal !== "function") return toast("details need core modal()");
  const biz = tSlug(t);
  Object.assign(t, taskStateObserve(biz, t));
  const fld = (k, v, mono, help) => `<div class="k"${help ? ` title="${esc(help)}"` : ""}>${esc(k)}</div><div class="v${mono ? " mono" : ""}">${v}${help ? `<small class="muted task-field-help">${esc(help)}</small>` : ""}</div>`;
  const none = s => `<span class="none">${esc(s)}</span>`;
  const stamp = s => esc(String(s || "").slice(0, 16).replace("T", " "));
  // Story, Customer and Email records open their Ordubashy conversation first, with an optional panel beside it.
  // An embedded workspace dashboard owns the one private Chat tab instead.
  const special = !embedded && TASK_SPECIAL_TYPES.includes(t.task_type) && typeof recordSurfaceMount === 'function';
  const renderBody = () => {
  const extra = Object.entries(t.fields || {}).map(([k, v]) => fld(k, esc(String(v)))).join("");
  const stateChip = task => !taskShowsState(task) ? taskTypeIcon(task.task_type) + customerStateChip(task) : `<span class="task-state-chip"><span class="sdot ${esc(task.status)}" aria-hidden="true"></span>${esc(T_LABEL[task.status] || task.status)}</span>`;
  const relatedTask = task => `<button type="button" class="chip task-link-chip" data-open-task="${esc(task.id)}" title="${esc(task.title)}">${stateChip(task)} ${esc(tChipLabel(task.title))}</button>`;
  const children = (t.children || []).map(relatedTask).join(' ');
  return `<div class="tdetail">
    <div class="task-detail-heading">${taskShowsState(t) ? `<button class="ball task-status-control ${esc(t.status)}" id="td-status" type="button" aria-label="Status: ${esc(T_LABEL[t.status] || t.status)}"><span class="core"></span></button>` : tBall(t)}<button class="dtitle" type="button" title="Edit title">${esc(t.title)}</button><button class="task-snooze-control quiet" id="td-snooze" type="button" aria-label="Snooze task" title="${esc(taskSnoozeLabel(t) || 'Snooze task')}" aria-pressed="${taskSnoozed(t)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/></svg></button></div>
    ${special ? '<section class="record-thread-host" data-record-surface aria-label="Record conversation"><div id="td-progress"></div></section>' : ''}
    <div class="task-type-detail">${taskTypeFields(t)}<button type="button" class="btn" data-save-type>${['story','customer'].includes(t.task_type) ? 'Save type' : 'Save overview'}</button>${t.email_conversation_id ? ' <button type="button" class="btn" data-task-read-email>Historical email reference</button>' : ''}</div>
    <div class="flds">
      ${fld("Assignees", `<button class="selbtn" id="td-who" type="button"><span class="v">${esc(tAssignees(t).map(o=>tAssigneeLabel(o,biz)).join(', ') || 'Unassigned')}</span></button>`)}
      ${fld("Due", `<input id="td-due" type="date" value="${esc(t.due || "")}">`)}
      ${fld("Following", `<button type="button" class="quiet" data-task-follow aria-pressed="${t.following ? 'true' : 'false'}">${t.following ? 'Following' + (t.follow_reason ? ' · ' + esc(String(t.follow_reason).replace(/_/g, ' ')) : '') + ' — unfollow' : 'Not following — follow'}</button>`)}
      ${fld("Snoozed", t.snoozed_until ? esc(taskSnoozeLabel(t) || 'Snooze elapsed ' + workDate(t.snoozed_until)) : none("not snoozed"))}
      ${fld("Tags", `<div data-tag-picker></div><button type="button" class="btn" data-tags-save hidden>Save tags</button>`)}
      ${fld("Parent", `${t.parent ? relatedTask(t.parent) + ' <button type="button" class="quiet" data-parent-clear>Remove parent</button>' : none('none')} <button type="button" class="quiet" data-parent-set>${t.parent ? 'Change parent' : 'Set parent'}</button>`)}
      ${fld("Children", `${children || none('none')}${t.children_has_more ? ' <span class="muted">and more</span>' : ''}<div class="task-child-add"><input class="inp" aria-label="New child task title" placeholder="Add a child task" maxlength="1000"><button type="button" class="quiet" data-child-add>Add child</button></div>`)}
      ${fld("Depends on", (t.blocked_by || []).length ? `<span class="task-dependency-chips" data-blocked-by>Loading…</span>` : none("nothing"), true, "Informational: a dependency never blocks or changes this task's state.")}
      ${(t.blocks || []).length ? fld("Informs", esc(String((t.blocks || []).length) + ' task' + ((t.blocks || []).length === 1 ? '' : 's')), true) : ""}
      ${fld("Created", stamp(t.created) + " · " + esc(tAssigneeLabel(t.by,biz)), true)}
      ${fld("Updated", stamp(t.updated), true)}
      ${extra}
    </div>
    <h4>Linked tasks</h4><div class="task-cross-links"></div>
    <h4>Related context</h4><div id="td-work" class="small muted">Loading…</div>
    ${special || embedded ? '' : `<h4>Progress notes${t.note_count ? " · " + t.note_count : ""}</h4>
    <section id="td-progress" aria-label="Task progress notes"></section>`}
    <div id="td-error" class="ferr" role="alert" hidden></div><button id="td-reload" class="ghost" type="button" hidden>Reload task</button>
  </div>`;
  };
  const body = renderBody();
  // a write repaints the detail with the fresh task; not if the user closed it while the write was in flight
  let m, surface = null, progress = null, typeEditor = null, related = null;
  const goTask = async (id, label) => {
    if (embedded && typeof navigate === 'function') return navigate({type:'task', id, business_id:t.business_id ?? null, business_slug:biz,
      label:label || (t.children || []).find(child => child.id === id)?.title || t.parent?.title || id});
    const other = await tFetchTask(biz, id);
    leave(); tDetail(other, host ? {host, onClose} : {});
  };
  // One teardown per true close, record switch or conversion: every controller this detail mounted.
  let moveDialog = null;
  const teardown = () => {
    // A dialog never outlives the editor that opened it; a late success still invalidates caches but opens nothing.
    try { moveDialog?.close(); } catch (_) { /* already gone */ } moveDialog = null;
    surface?.dispose(); surface = null; progress?.dispose?.(); progress = null;
    related?.dispose?.(); related = null;
    typeEditor?.dispose(); typeEditor = null;
  };
  const leave = () => { teardown(); if (host) host.replaceChildren(); else m.close(); };
  // Move to project: the record's own canonical scope, never the sidebar's. An embedded dashboard uses the workspace's control.
  const startMove = () => {
    const box = m.el.querySelector('#td-error');
    if (m.dirty?.()) { box.textContent = 'Save or discard your edits before moving this task.'; box.hidden = false; return; }
    const business = tBusiness(t);
    const sourceBusinessId = Object.hasOwn(t, 'business_id') ? t.business_id : (business.id ?? business.business_id);
    if (moveDialog) return;
    const open = reference => { if (!m.el.isConnected) return; m.close(); void taskMoveOpenReference(reference); };
    const handle = taskMoveOpen({task: t, sourceBusinessId, sourceSlug: tSlug(t), sourceName: business.name || '',
      onClosed: () => { if (moveDialog === handle || !handle) moveDialog = null; },
      onSourceLost: () => { if (m.el.isConnected) { m.close(); toast('Task is no longer available.'); } },
      onSourceMoved: () => { if (m.el.isConnected) { box.textContent = 'This task has moved. Reopen it from its new project.'; box.hidden = false; } },
      onMoved: open, onMovedElsewhere: open});
    moveDialog = handle;
  };
  // A same-record update keeps the record surface and its conversation mounted: only the controls around it are replaced,
  // and a type form with unsaved edits is kept as typed.
  const patchDetail = fresh => {
    for (const key of Object.keys(t)) if (!(key in fresh) && key !== 'business') delete t[key];
    Object.assign(t, fresh);
    const live = m.el.querySelector('.tdetail'), keep = live.querySelector('[data-record-surface]');
    const keepType = typeEditor?.dirty() ? live.querySelector('.task-type-detail') : null;
    const template = document.createElement('template'); template.innerHTML = renderBody();
    const next = template.content.querySelector('.tdetail'), marker = next.querySelector('[data-record-surface]');
    const before = [], after = []; let seen = false;
    for (const node of [...next.childNodes]) { if (node === marker) seen = true; else (seen ? after : before).push(node); }
    for (const node of [...live.childNodes]) if (node !== keep && node !== keepType) node.remove();
    for (const node of before) keep.before(node);
    let anchor = keep;
    for (const node of after) {
      if (keepType && node.classList?.contains('task-type-detail')) { anchor = keepType; continue; }
      anchor.after(node); anchor = node;
    }
    wire(m.el);
  };
  const again = async (id, body) => {
    if (!m.el.isConnected) return;
    const r = await twrite(t, {...body,expected_revision:t.revision}, message=>{
      if (!m.el.isConnected) return;
      const error=m.el.querySelector('#td-error');error.textContent=message;error.hidden=false;
      m.el.querySelector('#td-reload').hidden=false;
    });
    if (!m || !m.el || !m.el.isConnected) return;
    if (!r) return;
    const fresh = r.task ? {...r.task, business: tBusiness(t)} : await tFetchTask(biz, id);
    if (fresh) {
      // The workspace owns subtype adoption. A handled result means it replaced (or deliberately
      // kept) this dashboard; an unhandled result continues with the ordinary same-type repaint.
      if (onCanonical && await onCanonical(fresh, {reload: false, body})) return;
      await repaint(fresh);
    }
  };
  const repaint = async fresh => {
    if (!m.el.isConnected) return;
    if (special && surface && String(fresh.id) === String(t.id) && fresh.task_type === t.task_type) { patchDetail(fresh); return; }
    const panelOpen = !!surface?.isOpen();
    leave();
    tDetail(fresh, {...(host ? {host, onClose} : {}), recordPanelOpen: panelOpen,
      ...(embedded ? {embedded: true, navigate, relatedMount} : {}), onMount, onCanonical});
  };
  const wire = el => {
      typeEditor?.dispose();
      typeEditor=taskTypeMount(el,biz,t);
      el.querySelector('[data-save-type]').onclick=async()=>{
        try{
          const changes=taskTypeChanges(t,typeEditor.values());
          if(!Object.keys(changes).length){toast('There are no type changes to save.');return;}
          // Leaving Customer clears its profile and state, so that is confirmed first and sent explicitly.
          if(!await typeEditor.confirmConversion())return;
          again(t.id,{set:changes});
        }catch(error){toast(error.message);}
      };
      el.querySelector('[data-task-read-email]')?.addEventListener('click',()=>{
        if (embedded && typeof navigate === 'function') return navigate({type:'email', id:t.email_conversation_id, business_id:t.business_id ?? null, business_slug:biz, label:'Historical email reference'});
        return taskEmailOpen(t,biz);
      });
      if(host && !embedded){
        const actions=document.createElement('div');actions.className='task-inline-actions';
        actions.innerHTML='<button type="button" class="quiet" data-task-back>Back</button>'+(t.task_type!=='email'?'<button type="button" class="quiet" data-task-move>Move to project…</button>':'');
        actions.querySelector('[data-task-back]').onclick=()=>m.close();
        const moveButton=actions.querySelector('[data-task-move]');if(moveButton)moveButton.onclick=startMove;
        el.querySelector('.tdetail').prepend(actions);
      }
      // The record surface keeps its own conversation across same-record updates; otherwise it is mounted once.
      const section = el.querySelector('[data-record-surface]'), kept = !!section?._recordSurface;
      if (!kept && !embedded) progress = taskProgressMount(el.querySelector('#td-progress'), t, biz, () => m.close());
      if (special) {
        surface = recordSurfaceMount(section, {task: {id: t.id, task_type: t.task_type, business_id: t.business_id ?? null, email_conversation_id: t.email_conversation_id}, biz,
          beforeNavigate: () => { m.close(); },
          // A panel save updates this detail in place: the revision stays current and the conversation is never remounted.
          onTask: fresh => {
            Object.assign(t, fresh);
            const title = el.querySelector('.dtitle'); if (title && fresh.title) title.textContent = fresh.title;
            refreshTasks();
          },
          openTask: link => { m.close(); return taskLinkOpen(link); }});
        surface?.sync(t);
        if (recordPanelOpen && !kept) surface?.open();
      }
      taskLinksMount(el.querySelector('.task-cross-links'), biz, t, {open: async link=>{
        if (embedded && typeof navigate === 'function') return navigate({type:'task', id:link.id || link.task_id, business_id:link.business_id ?? null,
          business_slug:link.business_slug ?? null, label:link.label || link.title || link.id || link.task_id});
        m.close();await taskLinkOpen(link);
      }});
      if (embedded && typeof relatedMount === 'function') {
        const hostRelated = el.querySelector('#td-work');
        related?.dispose?.();
        related = relatedMount(hostRelated, t) || null;
      } else workRelatedChips(el.querySelector('#td-work'), biz, 'tasks', t.id, () => m.close());
      const blocked=el.querySelector('[data-blocked-by]');
      if(blocked)Promise.all((t.blocked_by || []).map(async id=>{try{return await tFetchTask(biz,id);}catch{return null;}})).then(tasks=>{
        if(!m.el.isConnected)return;
        blocked.replaceChildren();
        tasks.forEach((dep,index)=>{
          const button=document.createElement('button');button.type='button';button.className='chip task-blocked-chip';
          if(dep){button.textContent=dep.title;button.onclick=()=>{void goTask(dep.id, dep.title).catch(error=>toast(uiFailure(error)));};}
          else{button.textContent='Unavailable task';button.disabled=true;}
          blocked.appendChild(button);if(index<tasks.length-1)blocked.append(' ');
        });
      });
      el.querySelector('#td-snooze').onclick=()=>{
        const popup=tControlPopover(el.querySelector('#td-snooze'),`${taskSnoozePicker(t)}<div class="task-popover-actions"><button type="button" class="btn" data-snooze-save>Apply</button></div>`,'Snooze task');
        if(!popup)return;
        const {el:pop,close}=popup;taskSnoozeWire(pop);
        pop.querySelector('[data-snooze-save]').onclick=()=>{try{const snooze=taskSnoozeChange(pop,t);if(snooze!==undefined)again(t.id,{set:{snoozed_until:snooze}});close();}catch(error){const box=el.querySelector('#td-error');box.textContent=error.message;box.hidden=false;}};
      };
      el.querySelector('#td-reload').onclick = async()=>{try {const fresh=await tFetchTask(biz,t.id);if(!m.el.isConnected)return;
        if (onCanonical && await onCanonical(fresh, {reload: true})) return;
        leave();tDetail(fresh,{...(host ? {host,onClose} : {}), ...(embedded ? {embedded:true,navigate,relatedMount} : {}), onMount, onCanonical});}catch(error){toast(uiFailure(error,{load:true}));}};
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
      taskStateBind(sb, biz, t);
      if (sb) sb.onclick = () => dropdown(sb, T_STATES.map(s => ({ value: s, label: T_LABEL[s], html: `<span class="towner">${tDotHtml(s)}<span class="tn">${T_LABEL[s]}</span></span>` })),
        v => { if (v !== t.status) taskStateSet(biz, t, v); }, t.status);
      const wb = el.querySelector("#td-who");
      wb.onclick = () => tChooseAssignees(t,owners=>again(t.id,{set:{owners}}),wb);
      el.querySelector("#td-due").onchange = e => again(t.id, { set: { due: e.target.value || "" } });
      const saveTags = el.querySelector('[data-tags-save]');
      const picker = tagPickerMount(el.querySelector('[data-tag-picker]'), {biz, labels: t.tags || [], label: 'Tags', onChange: labels => { saveTags.hidden = JSON.stringify(labels) === JSON.stringify(tagNormalize(t.tags || [])); }});
      saveTags.onclick = () => again(t.id, { set: { tags: picker.labels() } });
      el.querySelectorAll('[data-open-task]').forEach(button => button.onclick = async () => {
        try { await goTask(button.dataset.openTask, button.title); }
        catch (error) { toast(uiFailure(error)); }
      });
      el.querySelector('[data-task-follow]').onclick = async () => {
        try { await workCall(biz, 'tasks_follow', { task_id: t.id, following: !t.following }); await repaint(await tFetchTask(biz, t.id)); }
        catch (error) { const box = el.querySelector('#td-error'); box.textContent = uiFailure(error); box.hidden = false; }
      };
      el.querySelector('[data-parent-set]').onclick = () => workChooseTasks(biz, [], chosen => {
        const parent = chosen.find(task => task.id !== t.id);
        if (parent) again(t.id, { set: { parent_id: parent.id } });
      });
      el.querySelector('[data-parent-clear]')?.addEventListener('click', () => again(t.id, { set: { parent_id: null } }));
      el.querySelector('[data-child-add]').onclick = async () => {
        const input = el.querySelector('.task-child-add input'), title = input.value.trim(); if (!title) return;
        try { await workCall(biz, 'tasks_create', { title, parent_id: t.id }); await repaint(await tFetchTask(biz, t.id)); }
        catch (error) { const box = el.querySelector('#td-error'); box.textContent = uiFailure(error); box.hidden = false; }
      };

  };
  if(host){
    m={el:host,close:()=>{teardown();if(host.isConnected){host.replaceChildren();onClose?.();}}};
    host.innerHTML=body;wire(host);
    m.dirty=()=> {
      if(typeEditor?.dirty?.()) return true;
      const tags=host.querySelector('[data-tags-save]');
      return !!tags && !tags.hidden || !!host.querySelector('.dtitle-in');
    };
  }else{
    m=modal({
      title: `Task · ${tBusiness(t).name || biz}`, width: special ? 980 : 620, body,
      actions: [
        ...(embedded || t.task_type === 'email' ? [] : [{ label: "Move to project…", onClick: startMove }]),
        { label: "Close", onClick: close => close() },
      ],
      onOpen:wire, onClose:teardown,
    });
  }
  if(m && !m.dirty) m.dirty=()=> !!(typeEditor?.dirty?.() || (m.el?.querySelector('[data-tags-save]:not([hidden])')) || m.el?.querySelector('.dtitle-in'));
  if(m) m.changed = message => {
    const box=m.el?.querySelector('#td-error');
    if(box){box.textContent=message;box.hidden=false;}
    // Make the existing explicit discard/reload action reachable from the changed-type
    // notice; a user must not have to attempt a doomed Save just to reveal it.
    const reload=m.el?.querySelector('#td-reload');
    if(reload) reload.hidden=false;
  };
  onMount?.(m);
  return m;
}

function tControlPopover(anchor,html,label) {
  if(anchor._taskPopover?.isConnected){anchor._taskPopover.hidePopover();return null;}
  const el=document.createElement('div');el.className='menu task-controls-popover';el.popover='auto';
  el.setAttribute('aria-label',label);el.innerHTML=html;
  (anchor.closest('.tdetail') || document.body).appendChild(el);
  anchor._taskPopover=el;anchor.setAttribute('aria-expanded','true');
  const close=()=>{if(el.isConnected)el.hidePopover();el.remove();anchor.setAttribute('aria-expanded','false');};
  el.addEventListener('toggle',event=>{if(event.newState==='closed'){el.remove();anchor.setAttribute('aria-expanded','false');}});
  el.showPopover();
  const rect=anchor.getBoundingClientRect();
  el.style.left=Math.max(8,Math.min(rect.left,innerWidth-el.offsetWidth-8))+'px';
  el.style.top=Math.max(8,Math.min(rect.bottom+6,innerHeight-el.offsetHeight-8))+'px';
  el.querySelector('input,select,button')?.focus();
  return {el,close};
}

async function tChooseAssignees(task,onSave,anchorEl=null) {
  if(!anchorEl)return;
  let choices;
  try { choices=await tWhoItems(tSlug(task)); } catch(error){ toast(uiFailure(error,{load:true})); return; }
  if(!anchorEl.isConnected)return;
  const current=tAssignees(task),selected=new Set();
  for(const owner of current){
    const choice=choices.find(item=>item.value && (item.value===owner || item.owner===owner));
    if(choice)selected.add(choice.value);
    else{selected.add(owner);choices.push({value:owner,label:'Unavailable assignee',html:`<span class="towner"><span class="tn">Unavailable assignee</span></span>`});}
  }
  const popup=tControlPopover(anchorEl,`<fieldset class="task-assignee-choices"><legend>Assignees</legend>${choices.map(choice=>`<label><input type="checkbox" value="${esc(choice.value)}" ${selected.has(choice.value)?'checked':''}><span>${choice.html || esc(choice.label)}</span></label>`).join('')}</fieldset><div class="task-popover-actions"><button type="button" class="quiet" data-assignee-cancel>Cancel</button><button type="button" class="btn" data-assignee-save>Apply</button></div>`,'Assignees');
  if(!popup)return;
  const {el:menu,close}=popup;
  menu.querySelectorAll('input').forEach(input=>input.onchange=()=>{
    if(!input.value && input.checked){selected.clear();menu.querySelectorAll('input').forEach(other=>{other.checked=other===input;});}
    else {input.checked?selected.add(input.value):selected.delete(input.value);const none=menu.querySelector('input[value=""]');if(none)none.checked=!selected.size;}
  });
  menu.querySelector('[data-assignee-cancel]').onclick=close;
  menu.querySelector('[data-assignee-save]').onclick=()=>{const values=[...selected].filter(Boolean);close();onSave(values);};
}

// ---------------------------------------------------------------- wiring
function tWireHead(p, from, onTab = tSetView) {
  const add=p.querySelector("#task-new");if(add)add.onclick=()=>tOpenAdd();
  const s = p.querySelector("#tsearch");
  const snoozeFilter = p.querySelector('#task-snooze-filter');
  if (snoozeFilter) snoozeFilter.onchange = () => { tSnoozeFilter = snoozeFilter.value; paintTasks(); };
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

// Presentation subtypes share Task identity, permissions, state and discussion.
const TASK_TYPE_LABELS = {task: 'Task', story: 'Story', customer: 'Customer', email: 'Email'};
const TASK_SPECIAL_TYPES = ['story', 'customer', 'email'];
function taskTypeIcon(type) {
  if(!TASK_SPECIAL_TYPES.includes(type))return '';
  return `<span class="task-type-icon task-type-icon-${type}" role="img" aria-label="${TASK_TYPE_LABELS[type]}" title="${TASK_TYPE_LABELS[type]}"></span>`;
}
// A Customer's free-text state label. It is a label only and never moves the Task's status.
const customerStateText = task => task?.task_type === 'customer' ? String(task.customer_state || '') : '';
const customerStateChip = task => customerStateText(task) ? `<span class="customer-state-chip" title="Customer state">${esc(customerStateText(task))}</span>` : '';
const CUSTOMER_PROFILE_FIELDS = [['company', 'Company'], ['contact_name', 'Contact name'], ['email', 'Email'], ['phone', 'Phone'], ['website', 'Website']];
const customerNormalizeState = value => String(value || '').replace(/\s+/gu, ' ').trim();
// The comparison key the engine uses for a label: whitespace-collapsed, Unicode-normalized and case-folded.
const customerStateKey = value => customerNormalizeState(String(value || '').normalize('NFKC').toUpperCase().toLowerCase().normalize('NFKC'));
let customerFieldSequence = 0;
// Typed Customer fields shared by the create form, the generic type editor and the Customer context panel.
function customerFieldsHtml(task = {}) {
  const profile = task.customer_profile || {}, list = `customer-states-${++customerFieldSequence}`;
  const text = ([key, label]) => `<label>${label}<input class="inp" data-customer-profile="${key}" maxlength="500" autocomplete="off" value="${esc(profile[key] || '')}"></label>`;
  return `<fieldset class="task-customer-fields global-task-wide" data-task-customer hidden><legend>Customer details</legend>
    <label>Kind<select data-customer-kind><option value="person" ${task.customer_kind !== 'company' ? 'selected' : ''}>Person</option><option value="company" ${task.customer_kind === 'company' ? 'selected' : ''}>Company</option></select></label>
    ${CUSTOMER_PROFILE_FIELDS.map(text).join('')}
    <label>State<input class="inp" data-customer-state maxlength="120" list="${list}" autocomplete="off" placeholder="Prospect, active, …" value="${esc(task.customer_state || '')}"></label><datalist id="${list}" data-customer-state-options></datalist>
    <p class="muted" data-customer-state-status role="status" aria-live="polite"></p>
    <label class="global-task-wide">Notes<textarea class="inp" data-customer-notes rows="4" maxlength="20000">${esc(profile.notes || '')}</textarea></label></fieldset>`;
}
function customerFieldsRead(root) {
  const profile = {};
  for (const [key] of CUSTOMER_PROFILE_FIELDS) profile[key] = root.querySelector(`[data-customer-profile="${key}"]`).value.trim();
  profile.notes = root.querySelector('[data-customer-notes]').value;
  return {customer_kind: root.querySelector('[data-customer-kind]').value, customer_state: customerNormalizeState(root.querySelector('[data-customer-state]').value), customer_profile: profile};
}
// Suggestions are the states already used in this exact scope (business or Global); a failed read leaves free text usable.
function customerStatesMount(root, biz) {
  const list = root.querySelector('[data-customer-state-options]'), status = root.querySelector('[data-customer-state-status]');
  let generation = 0;
  async function load() {
    const own = ++generation;
    status.textContent = 'Loading suggestions…';
    try {
      const data = await workCall(biz, 'tasks_config_get');
      if (own !== generation || !root.isConnected) return;
      list.replaceChildren(...(data.customer_states || []).map(label => { const option = document.createElement('option'); option.value = label; return option; }));
      status.textContent = '';
    } catch (error) {
      if (own !== generation || !root.isConnected) return;
      status.textContent = 'State suggestions unavailable. ';
      const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'quiet'; retry.textContent = 'Retry'; retry.onclick = load;
      status.append(retry);
    }
  }
  // A saved new label joins the choices at once; the next read confirms the scope's stored vocabulary.
  function extend(label) {
    const key = customerStateKey(label);
    if (!key || [...list.options].some(option => customerStateKey(option.value) === key)) return;
    const option = document.createElement('option'); option.value = customerNormalizeState(label); list.append(option);
  }
  return {load, extend, dispose() { generation++; }};
}
// `overviewOwned`: an existing Story or Customer edits its overview in the record panel, so the generic editor hides it.
function taskTypeFields(task = {}) {
  const ownedElsewhere = !!task.id && ['story', 'customer'].includes(task.task_type);
  return `<div class="task-type-editor global-task-wide"><label>Type<select data-task-type aria-label="Task type">${Object.entries(TASK_TYPE_LABELS).filter(([value])=>value!=='email' || task.task_type==='email').map(([value,label])=>`<option value="${value}" ${(task.task_type || 'task')===value?'selected':''}>${label}</option>`).join('')}</select></label>
    <label data-task-overview-field ${ownedElsewhere ? 'hidden' : ''}>Overview<textarea class="inp" data-task-overview aria-label="Overview" rows="3" maxlength="20000" placeholder="Context, current situation and next steps…">${esc(task.overview || '')}</textarea></label>
    ${customerFieldsHtml(task)}
    </div>`;
}
// What the type editor changed relative to the Task as last known. A type change sends every deliberate value;
// an unchanged Story or Customer is edited in its record panel, so the generic editor sends none of its fields.
const taskSameValue = (a, b) => JSON.stringify(a, (_, v) => v && typeof v === 'object' && !Array.isArray(v) ? Object.fromEntries(Object.entries(v).sort(([x], [y]) => x < y ? -1 : x > y ? 1 : 0)) : v) === JSON.stringify(b, (_, v) => v && typeof v === 'object' && !Array.isArray(v) ? Object.fromEntries(Object.entries(v).sort(([x], [y]) => x < y ? -1 : x > y ? 1 : 0)) : v);
function taskTypeChanges(task, values) {
  const type = task.task_type || 'task';
  if (values.task_type !== type) return values;
  if (task.id && ['story', 'customer'].includes(type)) return {};
  const known = {task_type: type, overview: task.overview || '', customer_kind: task.customer_kind ?? null, customer_state: task.customer_state || '', customer_profile: task.customer_profile || {}};
  return Object.fromEntries(Object.entries(values).filter(([key, value]) => !taskSameValue(value, known[key])));
}
// Shared confirmation for a deliberate, destructive conversion. Resolves false on cancel, Escape or backdrop.
function taskConfirm({title, message, confirmLabel}) {
  return new Promise(resolve => {
    let settled = false;
    const finish = value => { if (!settled) { settled = true; resolve(value); } };
    modal({title: esc(title), stack: true, width: 440, body: `<p>${esc(message)}</p>`,
      actions: [{label: 'Cancel', onClick: close => { finish(false); close(); }}, {label: confirmLabel, primary: true, onClick: close => { finish(true); close(); }}],
      onClose: () => finish(false)});
  });
}
function taskTypeMount(host, biz, task = {}) {
  const type=host.querySelector('[data-task-type]');
  const customerBox=host.querySelector('[data-task-customer]'), overviewField=host.querySelector('[data-task-overview-field]');
  const originalType=task.task_type || 'task', ownedElsewhere=!!task.id && ['story','customer'].includes(originalType);
  let statesLoaded=false;
  const states=customerStatesMount(customerBox,biz);
  function update(){
    const showState=type.value==='task';
    host.querySelectorAll('[data-task-status-field], #td-status').forEach(el=>{el.hidden=!showState;});
    const status=host.querySelector('[name=status]');if(status)status.disabled=!showState;
    // Existing Customers edit their details in the record panel; a conversion to Customer shows them here.
    customerBox.hidden=type.value!=='customer' || originalType==='customer' && ownedElsewhere;
    if(type.value==='customer' && !customerBox.hidden && !statesLoaded){statesLoaded=true;states.load();}
    if(overviewField)overviewField.hidden=ownedElsewhere && type.value===originalType;
    // A Customer's title is its name.
    const title=host.querySelector('input[name=title]'), label=title?.closest('label');
    if(label && label.firstChild?.nodeType===Node.TEXT_NODE && /^(Title|Name)$/.test(label.firstChild.textContent.trim()))label.firstChild.textContent=type.value==='customer'?'Name':'Title';
  }
  type.onchange=update;
  update();
  return {
    // Converting a Customer away clears its kind, state and profile; the owner confirms that first.
    async confirmConversion(){
      if(originalType!=='customer' || type.value==='customer')return true;
      return taskConfirm({title:'Remove customer details?',message:`Changing this Customer to ${TASK_TYPE_LABELS[type.value]} clears its kind, state, profile and notes. Its conversation and links stay.`,confirmLabel:'Change type'});
    },
    values(){
      // The legacy email link is display-only: it is never sent in a create, update or type change.
      const task_type=type.value, values={task_type};
      if(!overviewField?.hidden)values.overview=host.querySelector('[data-task-overview]').value;
      if(task_type==='customer'&&!customerBox.hidden)Object.assign(values,customerFieldsRead(customerBox));
      else if(task_type==='customer'&&originalType!=='customer')values.customer_kind='person';
      else if(originalType==='customer'&&task_type!=='customer')Object.assign(values,{customer_kind:null,customer_state:'',customer_profile:{}});
      return values;
    },
    // True while the editor holds a change that has not been saved.
    dirty(){try{return Object.keys(taskTypeChanges(task,this.values())).length>0;}catch(_){return true;}},
    dispose(){states.dispose();},
  };
}
function taskEmailOpen(task) {
  if(task.email_conversation_id)historicalEmailOpen();
}
function taskCreateDialog(biz, {parentId=null, onCreated=null, initial={}} = {}) {
  let editor, submission=null, busy=false;
  modal({title:'New task',width:620,stack:true,body:`<form data-task-create><label>Title<input class="inp" name="title" required maxlength="1000" value="${esc(initial.title || '')}" autocomplete="off"></label>${taskTypeFields(initial)}<button type="submit" hidden>Create</button><p class="ferr" role="alert" hidden></p></form>`,
    actions:[{label:'Cancel',onClick:close=>close()},{label:'Create',primary:true,onClick:async(close,el)=>{
      if(busy)return;
      const form=el.querySelector('form'),error=form.querySelector('[role="alert"]');
      try{
        if(!submission){
          if(!form.reportValidity())return;
          const fields=editor.values(),title=form.elements.title.value.trim();
          busy=true;
          const {assignees}=await workCall(biz,'assignees_list');
          const owner=assignees.find(person=>person.is_owner);
          submission={title,...fields,assignee_ids:owner?[owner.id]:[],...(parentId?{parent_id:parentId}:{}),idempotency_key:newRequestId()};
        }
        busy=true;error.hidden=true;form.querySelectorAll('input,select,textarea,button').forEach(e=>e.disabled=true);
        const {task}=await workCall(biz,'tasks_create',submission);
        close();refreshTasks();onCreated?.(task);
      }catch(err){error.textContent=submission?uiFailure(err):err.message;error.hidden=false;if(['validation_error','forbidden','not_found','revision_conflict'].includes(err.code))submission=null;}
      finally{busy=false;if(!submission)form.querySelectorAll('input,select,textarea,button').forEach(e=>e.disabled=false);}
    }}],onOpen:el=>{editor=taskTypeMount(el,biz,initial);el.querySelector('input').focus();el.querySelector('form').onsubmit=e=>{e.preventDefault();el.querySelector('.mfoot button:last-child')?.click();};}});
}

// The owner drawer and its background polling have been retired.
