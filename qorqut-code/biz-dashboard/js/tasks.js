// ---------------------------------------------------------------- tasks panel
// Right slide-in issues table over `biztask`. Owns #taskswrap / #thandle / #taskspanel.
// Exposes: refreshTasks(), refreshTasksSoon(), openTasks(on), state.tasks

const T_BUCKETS = [
  { key: "doing", label: "Doing", st: ["doing"] },
  { key: "open", label: "Open", st: ["open"] },
  { key: "blocked", label: "Blocked", st: ["blocked"] },
  { key: "closed", label: "Done", st: ["done", "dropped"] },
];
const T_TAGS = ["operations", "coding", "research", "marketing", "needs-owner"];
const T_NEXT = { open: "doing", doing: "done", blocked: "doing", done: "open", dropped: "open" };
const T_CLOSED = ["done", "dropped"];
const tShort = o => String(o || "").replace(/^(agent|human):/, "") || "unassigned";

let tFilter = ["open", "all", "done"].includes(ls("tasksflt")) ? ls("tasksflt") : "open";
let tFolded = ls("tasksfold") !== "0";
let tLastBiz = null, tSoonT = null, tInside = false;
state.tasks = [];

const tPanelOpen = () => { const w = $("#taskswrap"); return !!w && w.classList.contains("open"); };
const taskById = id => (state.tasks || []).find(t => t.id === id);

// A repaint replaces every row, so it must not fire while the user is inside one:
// an open dropdown loses its anchor, an inline edit or a half-picked date loses its value.
function tEditingNow() {
  if (document.querySelector(".menu")) return true;
  const a = document.activeElement;
  if (!a || !a.closest) return false;
  if (a.classList.contains("tedit")) return true;
  return !!(a.closest("#taskspanel") && (a.classList.contains("due") || a.classList.contains("selbtn")));
}

function openTasks(on) {
  const w = $("#taskswrap"); if (!w) return;
  w.classList.toggle("open", !!on);
  ls("tasks", on ? "1" : "0");
  if (on) refreshTasks();
}

// ---------------------------------------------------------------- data
function refreshTasksSoon() { clearTimeout(tSoonT); tSoonT = setTimeout(refreshTasks, 500); }

async function refreshTasks() {
  if (!state.biz) return;
  const r = await api(`/biz/${state.biz}/tasks?all=1`);
  if (Array.isArray(r)) state.tasks = r;
  else if (r && r.error) toast(r.error);
  paintTasks();
}

function tCount() { return (state.tasks || []).filter(t => !T_CLOSED.includes(t.status)).length; }

// a write always repaints, so let go of whatever row control is focused first
function tRelease() {
  const a = document.activeElement;
  if (a && a.closest && a.closest("#taskspanel") && a.id !== "taddin") try { a.blur(); } catch (e) {}
}

async function twrite(id, body) {
  const r = await post(`/biz/${state.biz}/task/${encodeURIComponent(id)}`, body);
  if (r && r.error) { toast(r.error); return null; }
  tRelease();
  await refreshTasks();
  if (typeof refreshSideSoon === "function") refreshSideSoon();
  return r;
}

async function tdone(id) {
  const r = await post(`/biz/${state.biz}/task/${encodeURIComponent(id)}/done`, {});
  if (r && r.error) { toast(r.error); return null; }
  tRelease();
  await refreshTasks();
  if (typeof refreshSideSoon === "function") refreshSideSoon();
  return r;
}

const tBiz = () => (state.bizs || []).find(x => x.slug === state.biz) || {};
// the business owner, as biztask spells it: `human:<owner from business.yaml>`
const tHuman = () => "human:" + (tBiz().owner || "owner");

function tDefaultOwner() {
  const b = tBiz();
  if (b.agents && b.agents.length) return "agent:" + b.agents[0];
  const seen = (state.tasks || []).find(t => String(t.owner || "").startsWith("human:"));
  return seen ? seen.owner : tHuman();
}

function tOwnerList() {
  const b = tBiz();
  const out = (b.agents || []).map(a => "agent:" + a);
  if (b.owner) out.push(tHuman());
  (state.tasks || []).forEach(t => { if (t.owner && out.indexOf(t.owner) < 0) out.push(t.owner); });
  // only invent a generic human row when nothing else names one
  if (!out.some(o => o.startsWith("human:"))) out.push(tHuman());
  return out;
}

// menu rows carry the agent glyph so the popover reads like the rest of the app
function tOwnerItems() {
  const meta = tBiz().agent_meta || {};
  return tOwnerList().map(o => {
    const name = tShort(o), agent = o.startsWith("agent:");
    const g = agent ? agentGlyph(meta[name], 8) : `<span class="tdot"></span>`;
    return { value: o, label: o, html: `<span class="towner">${g}<span class="tn">${esc(name)}</span><span class="tsub">${agent ? "agent" : "human"}</span></span>` };
  });
}

// only tags biztask accepts — offering anything else just earns a 400 from the server
function tTagList() { return T_TAGS.slice(); }

// ---------------------------------------------------------------- render
function tOwnerCell(t) {
  const b = (state.bizs || []).find(x => x.slug === state.biz) || {};
  const meta = (b.agent_meta || {})[String(t.owner || "").replace(/^agent:/, "")];
  const g = String(t.owner || "").startsWith("agent:") && meta ? agentGlyph(meta, 7) : "";
  return `<button class="selbtn own" type="button" title="${esc(t.owner || "unassigned")}">${g}<span class="v">${esc(tShort(t.owner))}</span></button>`;
}

function tRow(t) {
  const tags = (t.tags || []).map(x => `<button class="chip" data-tag="${esc(x)}" title="Remove ${esc(x)}">${esc(x)}</button>`).join("");
  return `<div class="trow ${esc(t.status)}" data-id="${esc(t.id)}">
    <button class="ball ${esc(t.status)}" title="${esc(t.status)} — click to advance"></button>
    <div class="ttl" title="${esc(t.title)}">${esc(t.title)}</div>
    ${tOwnerCell(t)}
    <input type="date" class="due" value="${esc(t.due || "")}" data-empty="${t.due ? 0 : 1}" title="Due date">
    <div class="tags">${tags}<button class="chip add" title="Add tag">+</button></div>
    <div class="tid" title="${esc(t.id)}">${esc(t.id)}</div>
    <button class="more" title="Details">&#8230;</button>
  </div>`;
}

function paintTasks() {
  const p = $("#taskspanel"); if (!p) return;
  const c = $("#tcount"); if (c) { const n = tCount(); c.textContent = n ? String(n) : ""; }
  if (tEditingNow()) return;

  const prev = $("#taddin");
  const keep = prev ? prev.value : "";
  const hadFocus = prev && document.activeElement === prev;

  const tasks = state.tasks || [];
  const show = tFilter === "done" ? ["closed"] : tFilter === "open" ? ["doing", "open", "blocked"] : ["doing", "open", "blocked", "closed"];
  let groups = "";
  T_BUCKETS.forEach(g => {
    if (show.indexOf(g.key) < 0) return;
    const rows = tasks.filter(t => g.st.indexOf(t.status) >= 0);
    if (!rows.length) return;
    const fold = g.key === "closed" && tFilter !== "done";
    groups += `<div class="tgroup ${fold ? "foldable" : ""} ${fold && tFolded ? "closed" : ""}" data-g="${g.key}">
      <button class="tgh" type="button"><span>${esc(g.label)}</span><span class="c">${rows.length}</span>${fold ? '<span class="chev"></span>' : ""}</button>
      ${rows.map(tRow).join("")}</div>`;
  });
  if (!groups) groups = `<div class="tempty">${state.biz ? (tasks.length ? "Nothing in this view." : "No tasks yet.") : "Pick a business first."}</div>`;

  p.innerHTML = `<div class="thead">
      <h2 class="lbl">Tasks</h2><div class="sp"></div>
      <div class="seg" id="tseg">
        <button type="button" data-f="open" class="${tFilter === "open" ? "on" : ""}">Open</button>
        <button type="button" data-f="all" class="${tFilter === "all" ? "on" : ""}">All</button>
        <button type="button" data-f="done" class="${tFilter === "done" ? "on" : ""}">Done</button>
      </div>
      <button class="pill" id="tnew" type="button">New</button>
    </div>
    <div class="tbody">
      <div class="tadd"><span class="ball open"></span><input id="taddin" placeholder="New task, Enter to add" autocomplete="off"><span class="hint">${esc(tShort(tDefaultOwner()))}</span></div>
      ${groups}
    </div>`;

  const add = $("#taddin");
  if (add) {
    add.value = keep;
    if (hadFocus) { add.focus(); add.setSelectionRange(keep.length, keep.length); }
    add.onkeydown = async e => {
      if (e.key === "Escape") { e.stopPropagation(); add.value = ""; add.blur(); return; }
      if (e.key !== "Enter") return;
      const title = add.value.trim(); if (!title) return;
      add.value = ""; add.disabled = true;
      const r = await post(`/biz/${state.biz}/task`, { title, owner: tDefaultOwner() });
      if (r && r.error) toast(r.error); else toast("added " + ((r && r.id) || ""));
      await refreshTasks();
      if (typeof refreshSideSoon === "function") refreshSideSoon();
      const el = $("#taddin"); if (el) { el.disabled = false; el.focus(); }
    };
  }
  const seg = $("#tseg");
  if (seg) seg.querySelectorAll("button").forEach(b => b.onclick = () => { tFilter = b.dataset.f; ls("tasksflt", tFilter); paintTasks(); });
  const nb = $("#tnew");
  if (nb) nb.onclick = () => { const el = $("#taddin"); if (el) el.focus(); };
}

// ---------------------------------------------------------------- row actions
function tCycle(t) {
  const nx = T_NEXT[t.status] || "doing";
  return nx === "done" ? tdone(t.id) : twrite(t.id, { set: { status: nx } });
}

function tEditTitle(row, t) {
  const el = row.querySelector(".ttl"); if (!el) return;
  const inp = document.createElement("input");
  inp.className = "tedit"; inp.value = t.title; inp.type = "text";
  el.replaceWith(inp);
  inp.focus(); inp.select();
  let settled = false;
  const stop = () => { if (settled) return; settled = true; inp.classList.remove("tedit"); paintTasks(); };
  inp.onkeydown = e => {
    if (e.key === "Enter") {
      e.preventDefault();
      const v = inp.value.trim(); settled = true;
      inp.classList.remove("tedit");
      if (!v || v === t.title) return paintTasks();
      inp.disabled = true; twrite(t.id, { set: { title: v } });
    } else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); stop(); }
  };
  inp.onblur = stop;
}

function tPick(anchor, items, cur, onPick) {
  if (typeof dropdown === "function") return dropdown(anchor, items, onPick, cur);
  const v = prompt(items.map(i => i.label).join(" / "), cur || "");
  if (v) onPick(v);
}

function tOwnerMenu(anchor, t) {
  tPick(anchor, tOwnerItems(), t.owner, v => { if (v !== t.owner) twrite(t.id, { set: { owner: v } }); });
}

function tTagMenu(anchor, t) {
  const have = t.tags || [];
  const items = tTagList().filter(x => have.indexOf(x) < 0).map(x => ({ value: x, label: x }));
  if (!items.length) return toast("no more tags");
  tPick(anchor, items, null, v => twrite(t.id, { set: { tags: have.concat([v]) } }));
}

// ---------------------------------------------------------------- detail modal
function tDetail(t) {
  if (typeof modal !== "function") return toast("details need core modal()");
  const closed = T_CLOSED.indexOf(t.status) >= 0;
  const fld = (k, v, mono) => `<div class="k">${esc(k)}</div><div class="v${mono ? " mono" : ""}">${v}</div>`;
  const none = s => `<span class="none">${esc(s)}</span>`;
  const extra = Object.entries(t.fields || {}).map(([k, v]) => fld(k, esc(String(v)))).join("");
  const notes = (t.notes || []).map(n => `<div class="tnote">
      <div class="nh"><span class="by">${esc(tShort(n.by))}</span><span class="ts">${esc(String(n.ts || "").slice(0, 16).replace("T", " "))}</span></div>
      <div class="tx">${esc(n.text)}</div></div>`).join("");
  const body = `<div class="tdetail">
    <div class="dtitle">${esc(t.title)}</div>
    <div class="did">${esc(t.id)}</div>
    <div class="flds">
      ${fld("Status", esc(t.status))}
      ${fld("Owner", esc(t.owner || "") || none("unassigned"))}
      ${fld("Due", t.due ? esc(t.due) : none("none"), true)}
      ${fld("Tags", (t.tags || []).length ? esc((t.tags || []).join(", ")) : none("none"))}
      ${fld("Blocked by", (t.blocked_by || []).length ? esc((t.blocked_by || []).join(", ")) : none("nothing"), true)}
      ${fld("Created", esc(String(t.created || "").slice(0, 16).replace("T", " ")) + " · " + esc(tShort(t.by)), true)}
      ${fld("Updated", esc(String(t.updated || "").slice(0, 16).replace("T", " ")), true)}
      ${extra}
    </div>
    <h4>Notes${(t.notes || []).length ? " · " + (t.notes || []).length : ""}</h4>
    ${notes || `<div class="tnote"><div class="tx"><span class="none">No notes yet.</span></div></div>`}
    <textarea id="tnotetx" placeholder="Add a note…"></textarea>
    <div class="drow"><button class="ghost" id="tnotebtn" type="button">Add note</button></div>
  </div>`;
  const m = modal({
    title: "Task", width: 620, body,
    actions: [
      { label: "Close", onClick: close => close() },
      { label: closed ? "Reopen" : "Mark done", primary: true, onClick: async close => { close(); closed ? await twrite(t.id, { set: { status: "open" } }) : await tdone(t.id); } },
    ],
    onOpen(el) {
      const btn = el.querySelector("#tnotebtn"), tx = el.querySelector("#tnotetx");
      if (!btn || !tx) return;
      btn.onclick = async () => {
        const v = tx.value.trim(); if (!v) return;
        btn.disabled = true;
        const r = await twrite(t.id, { note: v });
        if (m && m.close) m.close();
        if (r) { const fresh = taskById(t.id); if (fresh) tDetail(fresh); }
      };
    },
  });
  return m;
}

// ---------------------------------------------------------------- wiring
(function tInit() {
  const wrap = $("#taskswrap"), handle = $("#thandle"), panel = $("#taskspanel");
  if (!wrap || !handle || !panel) return;

  handle.onclick = () => openTasks(!tPanelOpen());
  if (ls("tasks") === "1") wrap.classList.add("open");

  panel.addEventListener("click", e => {
    const gh = e.target.closest(".tgh");
    if (gh) {
      const g = gh.closest(".tgroup");
      if (g && g.classList.contains("foldable")) { tFolded = !g.classList.contains("closed"); ls("tasksfold", tFolded ? "1" : "0"); g.classList.toggle("closed", tFolded); }
      return;
    }
    const row = e.target.closest(".trow"); if (!row) return;
    const t = taskById(row.dataset.id); if (!t) return;
    if (e.target.closest(".ball")) return void tCycle(t);
    if (e.target.closest(".more")) return void tDetail(t);
    const chip = e.target.closest(".chip");
    if (chip) {
      if (chip.classList.contains("add")) return void tTagMenu(chip, t);
      return void twrite(t.id, { set: { tags: (t.tags || []).filter(x => x !== chip.dataset.tag) } });
    }
    const own = e.target.closest(".own");
    if (own) return void tOwnerMenu(own, t);
    if (e.target.closest(".ttl")) return void tEditTitle(row, t);
  });

  panel.addEventListener("change", e => {
    const due = e.target.closest(".due"); if (!due) return;
    const row = due.closest(".trow"); const t = row && taskById(row.dataset.id); if (!t) return;
    twrite(t.id, { set: { due: due.value || "" } });
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
  });

  paintTasks();
  setInterval(() => { if (state.biz && state.biz !== tLastBiz) { tLastBiz = state.biz; refreshTasks(); } }, 500);
  setInterval(() => { if (state.biz) refreshTasks(); }, 12000);
})();
