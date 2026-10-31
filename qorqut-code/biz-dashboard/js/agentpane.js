// Agent tasks and activity, using the drawer's shared renderers and task cache.
// Task clicks open the existing drawer detail; business writes stay in tasks.js.
const AGPAD_LOG_N = 200, AGPAD_POLL_MS = 12000;
let _agpad = null;

function agpadGroups(tasks, name) {
  const actor = `agent:${name}`;
  const open = tasks.filter(t => !tClosed(t));
  const owned = open.filter(t => t.owner === actor);
  return {
    open: owned,
    // `by` is last editor, not creator. The first note is only a proxy: it can
    // have been added after creation, by someone other than the task's creator.
    waiting: open.filter(t => t.owner !== actor && t.notes && t.notes[0] && t.notes[0].by === actor),
  };
}

function agpadActive(p) {
  return p && _agpad === p && p.agent && p.open && !document.hidden &&
    state.tab === "agents" && state.biz === p.biz && state.agent === p.agent &&
    p.el.isConnected && $("#agpad") === p.el;
}

function agpadMount(name, el) {
  const p = _agpad = {
    biz: state.biz, agent: name, el, open: !!name && ls(`agpad:${state.biz}`) !== "off",
    view: "tasks",
    logs: null, logError: false, nextLog: 0, pending: false,
    tasks: null, source: state.tasks, tasksStarted: false, taskError: false, bodyHtml: null,
  };
  const button = $("#agpad-toggle");
  button.disabled = !name;
  button.onclick = () => {
    if (_agpad !== p || !p.agent) return;
    p.open = !p.open;
    ls(`agpad:${p.biz}`, p.open ? "on" : "off");
    p.nextLog = 0;
    agpadVisibility(p);
    agpadTick();
  };
  agpadVisibility(p);
  if (!name) { el.innerHTML = ""; return; }
  el.onclick = e => {
    const row = e.target.closest(".trow");
    if (row && agpadActive(p)) tOpenTask(row.dataset.id);
  };
  agpadSetView(p, "tasks");
}

function agpadSetView(p, view) {
  if (_agpad !== p || !["tasks", "activity"].includes(view)) return;
  const from = p.view;
  p.view = view;
  p.bodyHtml = null;
  p.el.innerHTML = tHead({ view, views: ["tasks", "activity"], title: p.agent, bodyId: "agpad-body", taskTools: false });
  tWireHead(p.el, from, next => agpadSetView(p, next));
  if (view === "activity") p.nextLog = 0;
  agpadPaint(p);
  agpadTick();
}

function agpadVisibility(p) {
  p.el.closest(".agsplit").classList.toggle("pad-off", !p.open);
  p.el.hidden = !p.open;
  const button = $("#agpad-toggle");
  button.setAttribute("aria-expanded", String(p.open));
  button.title = p.open ? "Hide agent pane" : "Show agent pane";
  if (p.open) moveTInd(p.el, p.view);
}

function agpadPaint(p) {
  let html;
  if (p.view === "activity") {
    html = p.logs ? tActivityHtml(p.logs, `No actions in the latest ${AGPAD_LOG_N} business log entries.`) : tEmptyHtml("Loading activity…");
    if (p.logError) html = tEmptyHtml(p.logs ? "Could not refresh activity. Showing the last update." : "Activity unavailable. Retrying…") + (p.logs ? html : "");
  } else if (!p.tasks) {
    html = tEmptyHtml(p.taskError ? "Tasks unavailable. Waiting for the next refresh…" : "Loading tasks…");
  } else {
    const groups = agpadGroups(p.tasks, p.agent);
    html = tSection("open", groups.open, { label: "Open tasks", canAdd: false, detailsOnly: true }) +
      tSection("waiting", groups.waiting, { label: "Waiting on", canAdd: false, detailsOnly: true });
  }
  if (p.bodyHtml === html) return;
  p.bodyHtml = html;
  const body = $("#agpad-body", p.el), scroll = body.scrollTop;
  body.innerHTML = html;
  body.scrollTop = scroll;
  if (p.view === "tasks") tWireRows(body, true);
  else if (p.logs) logTips(body, p.logs);
}

async function agpadRefreshLog(p) {
  if (!agpadActive(p) || p.view !== "activity" || p.pending) return;
  p.pending = true;
  try {
    const rows = await api(`/biz/${encodeURIComponent(p.biz)}/log?n=${AGPAD_LOG_N}`);
    if (!agpadActive(p) || p.view !== "activity") return;
    if (!Array.isArray(rows)) throw new Error("Log unavailable");
    p.logs = rows.filter(e => e.actor === `agent:${p.agent}`);
    p.logError = false;
  } catch (e) {
    if (agpadActive(p) && p.view === "activity") p.logError = true;
  } finally {
    p.pending = false;
    p.nextLog = Date.now() + AGPAD_POLL_MS;
    if (agpadActive(p) && p.view === "activity") agpadPaint(p);
  }
}

function agpadTick() {
  const p = _agpad;
  if (!agpadActive(p)) return;
  // A newly mounted pane must not read the previous business's task array.
  // Ask the existing shared refresh once, then follow its cache (including
  // optimistic task edits). All subsequent task requests belong to tasks.js.
  if (!p.tasksStarted) {
    p.tasksStarted = true;
    refreshTasks().then(() => {
      if (!agpadActive(p)) return;
      p.taskError = state.tasks === p.source && p.tasks === null;
      agpadTick();
    }).catch(() => {
      if (agpadActive(p)) { p.taskError = true; agpadPaint(p); }
    });
  }
  if (state.tasks !== p.source) { p.source = p.tasks = state.tasks; p.taskError = false; }
  agpadPaint(p);
  if (p.view === "activity" && Date.now() >= p.nextLog) agpadRefreshLog(p);
}

// A cheap local reconciliation keeps edits in the shared task cache visible.
// The only pane-specific network poll is the 12-second log request, while Activity is visible.
setInterval(agpadTick, 1000);
