// ---------------------------------------------------------------- side rail
// Side refresh: the last 30 log actions, fetched once per refresh, never more. They feed the toasts (and the
// `#log` list if a page ever has one; the right rail is gone for now) plus the running-session count in the foot.
const LOG_N = 30;
const _logSeen = {};      // biz -> Set of log ids already on screen, so only actions logged since the last fetch toast
let sideT;
function refreshSideSoon() { clearTimeout(sideT); sideT = setTimeout(refreshSide, 800); }
async function refreshSide() {
  if (!state.biz) return;
  const [log, live] = await Promise.all([api(`/biz/${state.biz}/log?n=${LOG_N}`), api(`/biz/${state.biz}/live`)]);
  const rows = Array.isArray(log) ? log.slice(0, LOG_N) : [];
  const logEl = $("#log");                                   // no log on screen right now; the rows are parked in logHtml()
  if (logEl) { logEl.innerHTML = rows.length ? logHtml(rows) : `<div class="empty">Nothing logged yet.</div>`; logTips(logEl, rows); }
  logToasts(state.biz, rows);
  const running = (Array.isArray(live) ? live : []).filter(s => !s.done);
  const f = $("#foot");
  f.innerHTML = running.length ? `<span class="dot on"></span>` : `<span class="dot"></span>`;
  f.title = running.length ? `${running.length} session${running.length === 1 ? "" : "s"} running` : "quiet";
}

// The log rows (stroke look): a department-coloured stroke, the summary in one line, time + actor under it,
// days as labelled gaps. Not on screen at the moment (the right rail is gone); kept for the log's next home.
function logHtml(rows) {
  const today = new Date().toISOString().slice(0, 10), yday = new Date(Date.now() - 864e5).toISOString().slice(0, 10);
  const dayLabel = k => k === today ? "today" : k === yday ? "yesterday"
    : new Date(k + "T00:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  let day = null, html = "";
  for (const e of rows) {
    const d = dept(TAG_DEPT[e.tag]), who = String(e.actor || "").replace(/^(agent|human|system):/, "");
    const raw = String(e.ts || ""), k = raw.slice(0, 10), time = raw.slice(11, 16);
    if (k !== day) { day = k; html += `<div class="lday" data-day="${esc(dayLabel(k))}"></div>`; }
    html += `<div class="le" style="--c:${d.color}"><div class="ls">${esc(e.summary || "")}</div>` +
      `<div class="lm"><span>${esc(time)}</span><span class="la">${esc(who)}</span></div></div>`;
  }
  return html;
}
function logTips(el, rows) {
  if (typeof tip !== "function") return;
  el.querySelectorAll(".le").forEach((row, i) => {
    const e = rows[i];
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
