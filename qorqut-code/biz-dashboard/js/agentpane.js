// Read-only agent overview. Activity uses the existing log renderer; tasks come from
// tasks.js's shared cache and refresh loop. No task controls or business writes live here.
const AGPAD_LOG_N = 200, AGPAD_POLL_MS = 12000;
let _agpad = null;

function agpadGroups(tasks, name) {
  const actor = `agent:${name}`;
  const open = tasks.filter(t => !tClosed(t));
  const owned = open.filter(t => t.owner === actor);
  return {
    open: owned,
    blocked: owned.filter(t => ["waiting", "blocked"].includes(t.status) || (t.blocked_by || []).length),
    // `by` is last editor, not creator. The first note is only a proxy: it can
    // have been added after creation, by someone other than the task's creator.
    assigned: open.filter(t => t.owner !== actor && t.notes && t.notes[0] && t.notes[0].by === actor),
  };
}

function agpadTaskHtml(rows, tasks, assigned = false, blocked = false) {
  return rows.map(t => {
    const label = T_LABEL[t.status] || t.status || "Open";
    const meta = [esc(label)];
    if (assigned) meta.push(esc(tShort(t.owner)));
    if (t.due) meta.push(`Due ${esc(t.due)}`);
    const blockers = blocked ? (t.blocked_by || []).map(id => {
      const task = tasks.find(x => x.id === id);
      return task ? task.title : id;
    }) : [];
    return `<div class="agpad-task"><div class="agpad-task-main">` +
      `<span class="agpad-status" data-state="${esc(t.status)}" aria-hidden="true"></span>` +
      `<span class="agpad-task-title">${esc(t.title)}</span></div>` +
      `<div class="agpad-meta">${meta.join(" · ")}</div>` +
      (blockers.length ? `<div class="agpad-blockers">Blocked by ${esc(blockers.join(", "))}</div>` : "") + `</div>`;
  }).join("");
}

function agpadActive(p) {
  return p && _agpad === p && p.agent && p.open && !document.hidden &&
    state.tab === "agents" && state.biz === p.biz && state.agent === p.agent &&
    p.el.isConnected && $("#agpad") === p.el;
}

function agpadMount(name, el) {
  const p = _agpad = {
    biz: state.biz, agent: name, el, open: !!name && ls(`agpad:${state.biz}`) !== "off",
    logs: null, logError: false, nextLog: 0, pending: false,
    tasks: null, source: state.tasks, tasksStarted: false, taskError: false, sigs: {},
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
  el.innerHTML = `<header class="agpad-head"><span class="agpad-eyebrow">Agent overview</span><h1>${esc(name)}</h1></header>
    <div class="agpad-sections">
      <section aria-labelledby="agpad-activity-title"><h2 id="agpad-activity-title">Activity <span id="agpad-activity-count"></span></h2><div class="agpad-scroll" id="agpad-activity"></div></section>
      <section aria-labelledby="agpad-open-title"><h2 id="agpad-open-title">Open tasks <span id="agpad-open-count"></span></h2><div class="agpad-scroll" id="agpad-open"></div></section>
      <section aria-labelledby="agpad-waiting-title"><h2 id="agpad-waiting-title">Waiting on <span id="agpad-waiting-count"></span></h2><div class="agpad-scroll">
        <h3>Blocked <span id="agpad-blocked-count"></span></h3><div id="agpad-blocked"></div>
        <h3 title="Open tasks owned by someone else whose first note was written by this agent. The first note's author can differ from the task's creator.">Assigned out <span id="agpad-assigned-count"></span></h3><div id="agpad-assigned"></div>
      </div></section>
    </div>`;
  agpadPaint(p);
  agpadTick();
}

function agpadVisibility(p) {
  p.el.closest(".agsplit").classList.toggle("pad-off", !p.open);
  p.el.hidden = !p.open;
  const button = $("#agpad-toggle");
  button.setAttribute("aria-expanded", String(p.open));
  button.title = p.open ? "Hide agent pane" : "Show agent pane";
}

function agpadReplace(p, key, html) {
  if (p.sigs[key] === html) return false;
  p.sigs[key] = html;
  const el = $(`#agpad-${key}`, p.el), scroll = el.scrollTop;
  el.innerHTML = html;
  el.scrollTop = scroll;
  return true;
}

function agpadPaint(p) {
  const empty = text => `<p class="agpad-empty">${text}</p>`;
  const logHtmlBody = p.logs && p.logs.length ? logHtml(p.logs) : empty(p.logs ?
    `No actions in the latest ${AGPAD_LOG_N} business log entries.` : "Loading activity…");
  const activity = p.logError ? empty(p.logs ? "Could not refresh activity. Showing the last update." : "Activity unavailable. Retrying…") +
    (p.logs ? logHtmlBody : "") : logHtmlBody;
  if (agpadReplace(p, "activity", activity) && p.logs) logTips($("#agpad-activity", p.el), p.logs);
  $("#agpad-activity-count", p.el).textContent = p.logs ? String(p.logs.length) : "";

  const groups = agpadGroups(p.tasks || [], p.agent);
  for (const key of ["open", "blocked", "assigned"]) {
    const message = { open: "No open tasks.", blocked: "Nothing blocked or waiting.", assigned: "No assigned-out tasks found." }[key];
    const html = p.tasks ? (groups[key].length ? agpadTaskHtml(groups[key], p.tasks, key === "assigned", key === "blocked") : empty(message)) :
      empty(p.taskError ? "Tasks unavailable. Waiting for the next refresh…" : "Loading tasks…");
    agpadReplace(p, key, html);
    $(`#agpad-${key}-count`, p.el).textContent = p.tasks ? String(groups[key].length) : "";
  }
  $("#agpad-waiting-count", p.el).textContent = p.tasks ? String(groups.blocked.length + groups.assigned.length) : "";
}

async function agpadRefreshLog(p) {
  if (!agpadActive(p) || p.pending) return;
  p.pending = true;
  try {
    const rows = await api(`/biz/${encodeURIComponent(p.biz)}/log?n=${AGPAD_LOG_N}`);
    if (!agpadActive(p)) return;
    if (!Array.isArray(rows)) throw new Error("Log unavailable");
    p.logs = rows.filter(e => e.actor === `agent:${p.agent}`);
    p.logError = false;
  } catch (e) {
    if (agpadActive(p)) p.logError = true;
  } finally {
    p.pending = false;
    p.nextLog = Date.now() + AGPAD_POLL_MS;
    if (agpadActive(p)) agpadPaint(p);
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
  if (Date.now() >= p.nextLog) agpadRefreshLog(p);
}

// A cheap local reconciliation keeps edits in the shared task cache visible.
// The only pane-specific network poll is the 12-second log request, while visible.
setInterval(agpadTick, 1000);
