// ---------------------------------------------------------------- side rail
// Log: the last 30 actions, fetched once per refresh, never more; about 7 rows visible, the rest scroll.
const LOG_N = 30;
let sideT;
function refreshSideSoon() { clearTimeout(sideT); sideT = setTimeout(refreshSide, 800); }
async function refreshSide() {
  if (!state.biz) return;
  const [log, live] = await Promise.all([api(`/biz/${state.biz}/log?n=${LOG_N}`), api(`/biz/${state.biz}/live`)]);
  const rows = Array.isArray(log) ? log.slice(0, LOG_N) : [];
  $("#log").innerHTML = rows.length ? rows.map(e => {
    const d = dept(TAG_DEPT[e.tag]), who = String(e.actor || "").replace(/^(agent|human|system):/, "");
    const raw = String(e.ts || ""), date = raw.slice(5, 10), time = raw.slice(11, 16);
    return `<div class="le">${glyph(d.icon, d.color, 12)}<span class="lh" title="${esc(date)}">${esc(time)}</span><span class="la">${esc(who)}</span></div>`;
  }).join("") : `<div class="empty">Nothing logged yet.</div>`;
  $("#log").querySelectorAll(".le").forEach((row, i) => {
    const e = rows[i];
    if (typeof tip === "function") tip(row, () => {
      const who = String(e.actor || "").replace(/^(agent|human|system):/, "");
      const det = e.detail ? String(e.detail).slice(0, 600) : "";
      return `<div class="tip-le"><div class="tlab">${esc(e.tag || "")} · ${esc(who)} · ${esc(e.ts || "")}</div>` +
        `<div class="tsum">${esc(e.summary || "")}</div>` +
        (det ? `<div class="tdet">${esc(det)}</div>` : "") + `</div>`;
    });
  });
  const running = (Array.isArray(live) ? live : []).filter(s => !s.done);
  $("#foot").innerHTML = running.length ? `<span class="dot on"></span>${running.length} session${running.length === 1 ? "" : "s"} running` : "quiet";
}
