// ---------------------------------------------------------------- side rail
// Side refresh: the last 30 log actions, fetched once per refresh, never more. They feed the toasts (and the
// `#log` list if a page ever has one; the right rail is gone for now) plus the running-session count in the foot.
const LOG_N = 30;
const _logSeen = {};      // biz -> Set of log ids already on screen, so only actions logged since the last fetch toast
let sideT;
function refreshSideSoon() { clearTimeout(sideT); sideT = setTimeout(refreshSide, 800); }
let sidePending = false;
async function refreshSide() {
  if (!state.biz || document.hidden || sidePending) return;
  sidePending = true;
  try {
  const biz = state.biz;
  const results = await Promise.allSettled([pageRead(`/biz/${biz}/log?n=${LOG_N}`), pageRead(`/biz/${biz}/live`)]);
  if (results.some(result => result.status === "rejected")) return;
  const [log, live] = results.map(result => result.value);
  if (state.biz !== biz) return;
  const rows = Array.isArray(log) ? log.slice(0, LOG_N) : [];
  const logEl = $("#log");                                   // no log on screen right now; the rows are parked in logHtml()
  if (logEl) { logEl.innerHTML = rows.length ? logHtml(rows) : `<div class="empty">Nothing logged yet.</div>`; logTips(logEl, rows); }
  logToasts(biz, rows);
  // The shared platform Signoff feed supplies Marketing badges and outcome notifications.
  const sessions = Array.isArray(live) ? live : (live && Array.isArray(live.sessions) ? live.sessions : []);
  const reqs = (live && Array.isArray(live.platform_signoffs)) ? live.platform_signoffs.map(mkPlatformRow) : [];
  if (typeof mkLiveToasts === "function") mkLiveToasts(biz, reqs);
  if (typeof mkBadgePaint === "function" && state.tab !== "marketing") mkBadgePaint(reqs);
  if (typeof rtFailureToasts === "function") rtFailureToasts(live && live.routine_failures);
  if (typeof lnLivePaint === "function") lnLivePaint(live);   // learnings_open → the Context rail badge
  if (typeof sgLivePaint === "function") sgLivePaint(biz, live);
  const running = sessions.filter(s => !s.done);
  const f = $("#foot");
  f.innerHTML = running.length ? `<span class="dot on"></span>` : `<span class="dot"></span>`;
  f.title = running.length ? `${running.length} session${running.length === 1 ? "" : "s"} running` : "quiet";
  } catch (_) { /* Keep the last good sidebar snapshot. */ } finally { sidePending = false; }
}

// Shared activity rows: department stroke, summary, time/actor, and an optional related task.
// Only the log's explicit task reference establishes a relationship; prose is never matched.
function logTaskId(entry) {
  const ref = entry.refs && entry.refs.task;
  return typeof ref === "string" ? ref.trim() : "";
}

const logGraphs=new Map();
function logGraph(biz){
  const cached=logGraphs.get(biz);if(cached && Date.now()-cached.at<20000)return cached.promise;
  const promise=Promise.resolve().then(()=>storyCall(biz,'context_graph_get'));logGraphs.set(biz,{at:Date.now(),promise});return promise;
}
function logHeadline(entry){
  let text=String(entry.summary || '').replace(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi,'').replace(/\b[0-9A-HJKMNP-TV-Z]{26}\b/g,'');
  for(const id of [entry.id,...Object.values(entry.refs || {})])if(typeof id==='string' && id.length>8)text=text.split(id).join('');
  text=text.replace(/^\s*[:—-]+\s*/,'').replace(/\s+/g,' ').trim();
  return Array.from(text).length>100?Array.from(text).slice(0,97).join('')+'...':text;
}
function logHtml(rows) {
  const today = new Date().toISOString().slice(0, 10), yday = new Date(Date.now() - 864e5).toISOString().slice(0, 10);
  const dayLabel = k => k === today ? "today" : k === yday ? "yesterday"
    : new Date(k + "T00:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  let day = null, html = "";
  for (const e of rows) {
    const d = dept(TAG_DEPT[e.tag]), who = String(e.actor || "").replace(/^(agent|human|system):/, "");
    const raw = String(e.ts || ""), k = raw.slice(0, 10), time = raw.slice(11, 16);
    const task = logTaskId(e);
    if (k !== day) { day = k; html += `<div class="lday" data-day="${esc(dayLabel(k))}"></div>`; }
    const title=logHeadline(e);
    html += `<div class="le${task ? " has-task" : ""}" style="--c:${d.color}"><time class="ltime" datetime="${esc(raw)}">${esc(time)}</time><div class="ls" title="${esc(title)}">${esc(title)}</div><div class="log-tags">${e.tag?(TAG_DEPT[e.tag] ? departmentTag(e.tag) : `<span class="log-tag">${esc(e.tag)}</span>`):''}</div>` +
      (task ? `<button class="ltask" type="button" title="Open related task" aria-label="Open related task">${T_VIEW_ICON.tasks}</button>` : "") + `</div>`;
  }
  return html;
}
function logTips(el, rows) {
  const biz = state.biz;
  el.querySelectorAll(".le").forEach((row, i) => {
    const e = rows[i];
    logGraph(biz).then(graph=>{
      if(!row.isConnected || state.biz!==biz)return;
      const stories=graph.nodes.filter(n=>n.type==='story' && graph.edges.some(edge=>
        (edge.source.type==='action' && edge.source.id===e.id && edge.target.type==='story' && edge.target.id===n.id) ||
        (edge.target.type==='action' && edge.target.id===e.id && edge.source.type==='story' && edge.source.id===n.id)));
      const tags=row.querySelector('.log-tags');
      for(const story of stories){const button=document.createElement('button');button.className='log-tag';button.textContent=tChipLabel(story.label);button.title=story.label;button.onclick=()=>contextOpenItem(biz,story);tags.prepend(button);}
    }).catch(()=>{});
    const button = row.querySelector(".ltask"), task = logTaskId(e);
    if (button && task) button.onclick = async () => {
      if (state.biz !== biz || !button.isConnected || button.disabled) return;
      button.disabled = true;
      try { await tOpenTask(task); }
      finally { button.disabled = false; }
    };
    if (typeof tip !== "function") return;
    tip(row, () => {
      const who = String(e.actor || "").replace(/^(agent|human|system):/, "");
      const det = e.detail ? String(e.detail).slice(0, 600) : "";
      return `<div class="tip-le"><div class="tlab">${esc(e.tag || "")} · ${esc(who)} · ${esc(e.ts || "")}</div>` +
        `<div class="tsum">${esc(e.summary || "")}</div>` + (det ? `<div class="tdet">${esc(det)}</div>` : "") + `</div>`;
    });
  });
}

// New log entries, oldest first so the toasts read in order; the first fetch of a business only seeds the set.
function logToasts(biz, rows) {
  const key = e => e.id || `${e.ts}|${e.summary}`;
  const seen = _logSeen[biz];
  if (!seen) { _logSeen[biz] = new Set(rows.map(key)); return; }
  rows.filter(e => !seen.has(key(e))).reverse().forEach(e => {
    seen.add(key(e));
    const d = dept(TAG_DEPT[e.tag]), who = String(e.actor || "").replace(/^(agent|human|system):/, "");
    if (typeof notify === "function") notify({ text: e.summary || e.tag || "action", sub: `${String(e.ts || "").slice(11, 16)} · ${who}`, color: d.color });
  });
}
