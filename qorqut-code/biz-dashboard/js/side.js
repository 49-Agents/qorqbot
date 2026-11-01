// ---------------------------------------------------------------- side rail
// Side refresh: the last 30 log actions, fetched once per refresh, never more. They feed the toasts (and the
// `#log` list if a page ever has one; the right rail is gone for now).
const LOG_N = 30;
const _logSeen = {};      // biz -> Set of log ids already on screen, so only actions logged since the last fetch toast
const _liveSince = {};    // biz -> the last /live response's cursor, so the next poll asks for changes only
let sideT;
function refreshSideSoon() { clearTimeout(sideT); sideT = setTimeout(refreshSide, 800); }
let sidePending = false;
async function refreshSide() {
  if (!state.biz || document.hidden || sidePending) return;
  sidePending = true;
  try {
  const biz = state.biz;
  const since = _liveSince[biz];
  const liveUrl = `/biz/${biz}/live` + (since ? `?since=${encodeURIComponent(since)}` : "");
  const results = await Promise.allSettled([pageRead(`/biz/${biz}/log?n=${LOG_N}`), pageRead(liveUrl)]);
  if (results.some(result => result.status === "rejected")) return;
  const [log, live] = results.map(result => result.value);
  if (state.biz !== biz) return;
  const rows = Array.isArray(log) ? log.slice(0, LOG_N) : [];
  const logEl = $("#log");                                   // no log on screen right now; the rows are parked in logHtml()
  if (logEl) { logEl.innerHTML = rows.length ? logHtml(rows) : `<div class="empty">Nothing logged yet.</div>`; logTips(logEl, rows); }
  logToasts(biz, rows);
  if (live && live.since) _liveSince[biz] = live.since;
  // The shared platform Signoff feed supplies Marketing badges and outcome notifications.
  const reqs = (live && Array.isArray(live.platform_signoffs)) ? live.platform_signoffs.map(mkPlatformRow) : [];
  if (typeof mkLiveToasts === "function") mkLiveToasts(biz, reqs);
  if (typeof mkBadgePaint === "function" && state.tab !== "marketing") mkBadgePaint(reqs);
  if (typeof rtFailureToasts === "function") rtFailureToasts(live && live.routine_failures);
  if (typeof lnLivePaint === "function") lnLivePaint(live);   // learnings_open → the Context rail badge
  if (typeof sgLivePaint === "function") sgLivePaint(biz, live);
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
// When an entry happened, precision-aware: a backfilled day has no time, an hour shows HH:00, and the
// stored anchor of either is never shown as an exact time. Dates are San Juan dates.
function logWhen(e) {
  const raw = String(e.ts || ""), precision = e.time_precision || "instant";
  const day = e.occurred_date || sjDate(raw);
  if (precision === "day") return { day, time: "", full: day ? `${day}, time unknown` : "" };
  if (precision === "hour") { const time = `${String(e.occurred_hour).padStart(2, "0")}:00`; return { day, time, full: `${day} ${time}` }; }
  return { day, time: sjTime(raw), full: sjDateTime(raw) || raw };
}
function logHtml(rows) {
  // Day groups and times are San Juan time (sjDate/sjTime), never the browser's zone or raw UTC.
  const today = sjToday(), yday = sjAddDays(today, -1);
  const dayLabel = k => !k ? "undated" : k === today ? "today" : k === yday ? "yesterday"
    : new Date(k + "T12:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  let day = null, html = "";
  for (const e of rows) {
    const d = dept(TAG_DEPT[e.tag]), who = String(e.actor || "").replace(/^(agent|human|system):/, "");
    const raw = String(e.ts || ""), { day: k, time } = logWhen(e);
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
    const e = rows[i], ids = [e.id, e.action_id].filter(Boolean);
    logGraph(biz).then(graph=>{
      if(!row.isConnected || state.biz!==biz)return;
      const stories=graph.nodes.filter(n=>n.type==='story' && graph.edges.some(edge=>
        (edge.source.type==='action' && ids.includes(edge.source.id) && edge.target.type==='story' && edge.target.id===n.id) ||
        (edge.target.type==='action' && ids.includes(edge.target.id) && edge.source.type==='story' && edge.source.id===n.id)));
      const tags=row.querySelector('.log-tags');
      for(const story of stories){const button=document.createElement('button');button.className='log-tag';button.textContent=tChipLabel(story.label);button.title=story.label;button.onclick=()=>contextOpenItem(biz,story);tags.prepend(button);}
    }).catch(()=>{});
    const button = row.querySelector(".ltask"), task = logTaskId(e);
    if (button && task) button.onclick = async () => {
      if (state.biz !== biz || !button.isConnected || button.disabled) return;
      button.disabled = true;
      // A shared action stored in Global links a Global task; it never opens in this business.
      try { await (e.business_id === null ? taskProgressOpenReference(null, { type: "task", id: task }) : tOpenTask(task)); }
      finally { button.disabled = false; }
    };
    if (typeof tip !== "function") return;
    tip(row, () => {
      const who = String(e.actor || "").replace(/^(agent|human|system):/, "");
      const det = e.detail ? String(e.detail).slice(0, 600) : "";
      return `<div class="tip-le"><div class="tlab">${esc(e.tag || "")} · ${esc(who)} · ${esc(logWhen(e).full)} ${esc(SJ_LABEL)}</div>` +
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
    if (typeof notify === "function") notify({ text: e.summary || e.tag || "action", sub: `${logWhen(e).time || logWhen(e).day} · ${who}`, color: d.color });
  });
}
