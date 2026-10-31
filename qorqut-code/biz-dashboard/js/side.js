// ---------------------------------------------------------------- side rail
let sideT;
function refreshSideSoon() { clearTimeout(sideT); sideT = setTimeout(refreshSide, 800); }
async function refreshSide() {
  if (!state.biz) return;
  const [log, tasks, reqs, live] = await Promise.all([api(`/biz/${state.biz}/log?n=30`), api(`/biz/${state.biz}/tasks`), api(`/biz/${state.biz}/requests`), api(`/biz/${state.biz}/live`)]);
  $("#log").innerHTML = Array.isArray(log) && log.length ? log.map(e => {
    const d = dept(TAG_DEPT[e.tag]), who = String(e.actor || "").replace(/^(agent|human|system):/, "");
    return `<div class="le" title="${esc(e.tag || "")} · ${esc(who)}"><span class="ts">${esc(String(e.ts || "").slice(5, 16).replace("T", " "))}</span>${glyph(d.icon, d.color, 10)}<span class="s">${esc(e.summary)}</span></div>`;
  }).join("") : `<div class="empty">Nothing logged yet.</div>`;
  const t = $("#tasks");
  if (Array.isArray(tasks) && tasks.length) {
    const by = {}; tasks.forEach(x => (by[x.owner] = by[x.owner] || []).push(x));
    t.innerHTML = Object.entries(by).map(([o, xs]) => `<div class="owner">${esc(o)}</div>` + xs.map(x => `<div class="task"><div class="t"><span>${esc(x.title)}</span><span class="st ${esc(x.status)}">${esc(x.status)}${x.due ? " · " + esc(x.due) : ""}</span></div><div class="id">${esc(x.id)}${x.tags.length ? " · " + esc(x.tags.join(", ")) : ""}</div></div>`).join("")).join("");
  } else t.innerHTML = `<div class="empty" style="padding:0 22px">No open tasks.</div>`;
  const r = $("#requests");
  r.innerHTML = Array.isArray(reqs) && reqs.length ? reqs.slice(0, 12).map(x => `<div class="req"><span><span class="dot ${x.executed ? "ok" : ""}"></span><span class="id">${esc(x.id)}</span></span><span class="muted">${esc(x.tool || "")}</span></div>`).join("") : `<div class="empty" style="padding:0 22px">No requests yet.</div>`;
  const running = (Array.isArray(live) ? live : []).filter(s => !s.done);
  $("#foot").innerHTML = running.length ? `<span class="dot on"></span>${running.length} session${running.length === 1 ? "" : "s"} running` : "quiet";
}
$("#gwbtn").onclick = async () => { $("#gwbtn").disabled = true; const r = await post(`/biz/${state.biz}/gateway`, {}); toast(r.error || (r.out || "").split("\n").slice(-1)[0] || "done"); $("#gwbtn").disabled = false; refreshSide(); };
$("#taskbtn").onclick = async () => {
  const title = prompt("Task title"); if (!title) return;
  const b = state.bizs.find(x => x.slug === state.biz);
  const owner = prompt("Owner (agent:name or human:name)", b && b.agents[0] ? "agent:" + b.agents[0] : "human:owner") || "";
  const r = await post(`/biz/${state.biz}/task`, { title, owner }); toast(r.error || `added ${r.id}`); refreshSide();
};
