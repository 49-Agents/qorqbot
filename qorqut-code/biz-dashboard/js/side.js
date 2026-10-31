// ---------------------------------------------------------------- side rail
let sideT;
function refreshSideSoon() { clearTimeout(sideT); sideT = setTimeout(refreshSide, 800); }
async function refreshSide() {
  if (!state.biz) return;
  const [log, reqs, live] = await Promise.all([api(`/biz/${state.biz}/log?n=40`), api(`/biz/${state.biz}/requests`), api(`/biz/${state.biz}/live`)]);
  $("#log").innerHTML = Array.isArray(log) && log.length ? log.map(e => {
    const d = dept(TAG_DEPT[e.tag]), who = String(e.actor || "").replace(/^(agent|human|system):/, "");
    const raw = String(e.ts || ""), date = raw.slice(5, 10), time = raw.slice(11, 16);
    return `<div class="le">${glyph(d.icon, d.color, 16)}<span class="lt"><span class="ld">${esc(date)}</span><span class="lh">${esc(time)}</span></span><span class="la">${esc(who)}</span></div>`;
  }).join("") : `<div class="empty">Nothing logged yet.</div>`;
  if (Array.isArray(log) && log.length) {
    const rows = $("#log").querySelectorAll(".le");
    rows.forEach((row, i) => {
      const e = log[i];
      if (typeof tip === "function") tip(row, () => {
        const who = String(e.actor || "").replace(/^(agent|human|system):/, "");
        const det = e.detail ? String(e.detail).slice(0, 600) : "";
        return `<div class="tip-le"><div class="tlab">${esc(e.tag || "")} · ${esc(who)} · ${esc(e.ts || "")}</div>` +
          `<div class="tsum">${esc(e.summary || "")}</div>` +
          (det ? `<div class="tdet">${esc(det)}</div>` : "") + `</div>`;
      });
    });
  }
  const r = $("#requests");
  r.innerHTML = Array.isArray(reqs) && reqs.length ? reqs.slice(0, 12).map(x => `<div class="req"><span><span class="dot ${x.executed ? "ok" : ""}"></span><span class="id">${esc(x.id)}</span></span><span class="muted">${esc(x.tool || "")}</span></div>`).join("") : `<div class="empty" style="padding:0 22px">No requests yet.</div>`;
  const running = (Array.isArray(live) ? live : []).filter(s => !s.done);
  $("#foot").innerHTML = running.length ? `<span class="dot on"></span>${running.length} session${running.length === 1 ? "" : "s"} running` : "quiet";
}
$("#gwbtn").onclick = async () => { $("#gwbtn").disabled = true; const r = await post(`/biz/${state.biz}/gateway`, {}); toast(r.error || (r.out || "").split("\n").slice(-1)[0] || "done"); $("#gwbtn").disabled = false; refreshSide(); };
