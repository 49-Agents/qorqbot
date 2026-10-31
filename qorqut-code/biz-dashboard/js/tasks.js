

















const T_ORDER = ["open", "doing", "done"];
const T_CLOSED = ["done", "dropped"];
const T_STATES = ["open", "doing", "blocked", "done", "dropped"];
const T_LABEL = { open: "Open", doing: "In progress", done: "Done", blocked: "Blocked", dropped: "Dropped" };
const T_DOT = { open: "rgba(245,240,232,.4)", doing: "#0A84FF", done: "#30D158", blocked: "#FF453A", dropped: "#8A8680" };
const T_TAGS = ["operations", "coding", "research", "marketing", "needs-owner"];
const T_BUCKETS = [["today", "Today"], ["tomorrow", "Tomorrow"], ["later", "Later"], ["someday", "Someday"], ["done", "Completed"]];
const T_FILTERS = [["all", "All"], ["today", "Today"], ["upcoming", "Upcoming"], ["overdue", "Overdue"]];
const T_DONE_HOLD = 550;
const T_POP = 260;           // ms the ball pop runs before a repaint may replace it
const T_DBL = 250;           // ms a click on the title text waits for a second click (inline edit) before opening the detail
const T_DAY = 86400000;

const SVG_TCHECK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>`;
const SVG_TCHEV = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>`;
const SVG_TPLUS = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5v14"/></svg>`;
const SVG_TCAL = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2v4M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/></svg>`;
const SVG_TTAG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r=".5" fill="currentColor"/></svg>`;
const SVG_TSEARCH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>`;
const SVG_TINBOX = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>`;

const tShort = o => String(o || "").replace(/^(agent|human|system):/, "") || "unassigned";
const tYmd = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

const tDay = s => { const [y, m, d] = String(s).slice(0, 10).split("-").map(Number); return new Date(y, (m || 1) - 1, d || 1).getTime(); };
const tToday = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime(); };
const tClosed = t => T_CLOSED.indexOf(t.status) >= 0;
const tNext = s => { const i = T_ORDER.indexOf(s); return i < 0 ? "open" : T_ORDER[(i + 1) % T_ORDER.length]; };

let tFilter = T_FILTERS.some(f => f[0] === ls("tasksflt")) ? ls("tasksflt") : "all";
let tSearch = "";
let tFold = { done: true };
try { tFold = Object.assign({ done: true }, JSON.parse(ls("tasksfold2") || "{}")); } catch (e) {}
let tAdding = null;                 // bucket key with an open inline add row
let tAnim = null;                   // task id whose ball animates on the next paint
let tHold = 0, tHoldT = null;       // no repaint before this time (an animation is running)
const tCompleting = new Set();      // ids shown done but still in their old section
let tGen = 0;                       // bumped when a write starts; a refresh that began before it is stale when it lands
let tLastBiz = null, tSoonT = null, tInside = false, tSearchT = null, tClickT = null;
state.tasks = [];

const tPanelOpen = () => { const w = $("#taskswrap"); return !!w && w.classList.contains("open"); };
const taskById = id => (state.tasks || []).find(t => t.id === id);
const tBiz = () => (state.bizs || []).find(x => x.slug === state.biz) || {};
// the business owner, as biztask spells it: `human:<owner from business.yaml>` (server's owner_actor fallback)
const tHuman = () => "human:" + (tBiz().owner || "dashboard");
const tAgents = () => (tBiz().agents || []).map(a => "agent:" + a);
const tMeta = o => (tBiz().agent_meta || {})[tShort(o)];

// A repaint replaces every row, so it must not fire while the user is inside one:
// an open dropdown loses its anchor, a half-picked date loses its value.
function tEditingNow() {
  if (document.querySelector(".menu")) return true;
  const a = document.activeElement;
  if (!a || !a.closest) return false;
  if (a.classList.contains("tedit")) return true;
  return !!(a.closest("#taskspanel") && (a.classList.contains("duein") || a.classList.contains("selbtn")));
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
  const gen = tGen, biz = state.biz;
  const r = await api(`/biz/${biz}/tasks?all=1`);
  if (gen !== tGen || biz !== state.biz) return;
  if (Array.isArray(r)) {
    state.tasks = r;
    // a row mid-completion keeps its local done look until the write lands
    r.forEach(t => { if (tCompleting.has(t.id)) t.status = "done"; });
  } else if (r && r.error) toast(r.error);
  paintTasks();
}

function tCount() { return (state.tasks || []).filter(t => !tClosed(t)).length; }

// a write always repaints, so let go of whatever row control is focused first
function tRelease() {
  const a = document.activeElement;
  if (a && a.closest && a.closest("#taskspanel") && a.id !== "taddin" && a.id !== "tsearch") try { a.blur(); } catch (e) {}
}

async function twrite(id, body) {
  tGen++;
  const r = await post(`/biz/${state.biz}/task/${encodeURIComponent(id)}`, body);
  if (r && r.error) { toast(r.error); await refreshTasks(); return null; }
  tRelease();
  await refreshTasks();
  if (typeof refreshSideSoon === "function") refreshSideSoon();
  return r;
}

async function tdone(id) {
  tGen++;
  const r = await post(`/biz/${state.biz}/task/${encodeURIComponent(id)}/done`, {});
  if (r && r.error) { toast(r.error); await refreshTasks(); return null; }
  tRelease();
  await refreshTasks();
  if (typeof refreshSideSoon === "function") refreshSideSoon();
  return r;
}

async function tadd(fields) {
  tGen++;
  const r = await post(`/biz/${state.biz}/task`, fields);
  if (r && r.error) { toast(r.error); return null; }
  await refreshTasks();
  if (typeof refreshSideSoon === "function") refreshSideSoon();
  return r;
}

// ---------------------------------------------------------------- buckets & filters
function tBucket(t) {
  if (tClosed(t) && !tCompleting.has(t.id)) return "done";
  if (!t.due) return "someday";
  const due = tDay(t.due), today = tToday();
  if (due <= today) return "today";
  if (due === today + T_DAY) return "tomorrow";
  return "later";
}

function tMatch(t) {
  const q = tSearch.trim().toLowerCase();
  if (q && !(String(t.title).toLowerCase().includes(q) || (t.tags || []).some(x => x.includes(q))
      || (t.notes || []).some(n => String(n.text || "").toLowerCase().includes(q)))) return false;
  const closed = tClosed(t), today = tToday();
  if (tFilter === "today") return !!t.due && !closed && tDay(t.due) <= today;
  // upcoming = today onward; no due date counts too (do-it-whenever)
  if (tFilter === "upcoming") return !closed && (!t.due || tDay(t.due) >= today);
  if (tFilter === "overdue") return !!t.due && !closed && tDay(t.due) < today;
  return true;
}

function tDueLabel(due) {
  const d = tDay(due), today = tToday();
  if (d === today) return "Today";
  if (d === today - T_DAY) return "Yesterday";
  if (d === today + T_DAY) return "Tomorrow";
  return new Date(d).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

const tTagColor = x => x === "needs-owner" ? "var(--accent)" : dept(TAG_DEPT[x]).color;

// ---------------------------------------------------------------- assignee
// none | agent (one of the business's agents) | human (the owner) | other (any other actor)
function tWhoKind(o) {
  if (!o) return "none";
  if (o === tHuman()) return "human";
  if (tAgents().indexOf(o) >= 0) return "agent";
  return "other";
}
function tWhoLeft(o) {
  const ag = tAgents(); if (!ag.length) return null;
  const i = ag.indexOf(o);
  if (i < 0) return ag[0];
  return i + 1 < ag.length ? ag[i + 1] : "";
}
const tWhoRight = o => (o === tHuman() ? "" : tHuman());

function tWhoInner(o) {
  const k = tWhoKind(o);
  if (k === "none") return `<span class="ring"></span>`;
  if (k === "agent") return agentGlyph(tMeta(o), 10);
  return `<span class="disc ${k}">${esc(tShort(o).slice(0, 1).toUpperCase())}</span>`;
}
function tWho(t) {
  const o = t.owner || "";
  return `<button class="who ${tWhoKind(o)}" type="button" aria-label="Assignee: ${esc(o || "unassigned")}">${tWhoInner(o)}</button>`;
}
function tWhoTip(t) {
  const o = t.owner || "", k = tWhoKind(o);
  const who = k === "none" ? "Unassigned" : `${esc(tShort(o))} <span class="k">${k === "human" ? "owner" : k === "agent" ? "agent" : o.split(":")[0]}</span>`;
  const left = tWhoLeft(o);
  const hints = [left == null ? "no agents" : `click: ${left ? tShort(left) : "unassign"}`,
    `right-click: ${k === "human" ? "unassign" : tShort(tHuman())}`];
  return `${who}<br><span class="k">${hints.join(" · ")}</span>`;
}
function tWhoItems() {
  const items = [{ value: "", label: "Unassigned", html: `<span class="towner"><span class="who none"><span class="ring"></span></span><span class="tn">Unassigned</span></span>` }];
  tAgents().forEach(o => items.push({ value: o, label: o, html: `<span class="towner"><span class="who agent">${agentGlyph(tMeta(o), 10)}</span><span class="tn">${esc(tShort(o))}</span><span class="tsub">agent</span></span>` }));
  if (tBiz().owner) items.push({ value: tHuman(), label: tHuman(), html: `<span class="towner"><span class="who human"><span class="disc human">${esc(tShort(tHuman()).slice(0, 1).toUpperCase())}</span></span><span class="tn">${esc(tShort(tHuman()))}</span><span class="tsub">owner</span></span>` });
  return items;
}

// ---------------------------------------------------------------- render
function tBall(t, anim) {
  const s = t.status, label = T_LABEL[s] || s;
  return `<button class="ball ${esc(s)}${anim ? " anim" : ""}" type="button" title="${esc(label)} — click to change, right-click to block"
    aria-label="Status: ${esc(label)}. Click to change."><span class="core"></span><span class="burst"></span><span class="chk">${SVG_TCHECK}</span></button>`;
}

function tRow(t) {
  const closed = tClosed(t);
  const over = !closed && t.due && tDay(t.due) < tToday();
  const desc = !closed && (t.notes || []).length ? String(t.notes[0].text || "").split("\n")[0] : "";
  const tags = (t.tags || []).map(x => `<button class="chip" type="button" data-tag="${esc(x)}" title="Tag: ${esc(x)}"><span class="tdot" style="--tc:${tTagColor(x)}"></span><span class="tn">${esc(x)}</span></button>`).join("");
  const due = t.due
    ? `<span class="duew"><button class="due${over ? " over" : ""}" type="button" title="Due ${esc(t.due)}">${SVG_TCAL}<span>${esc(tDueLabel(t.due))}</span></button><input type="date" class="duein" value="${esc(t.due)}" tabindex="-1" aria-label="Due date"></span>`
    : closed ? "" : `<span class="duew"><button class="due none" type="button" title="Set due date">${SVG_TCAL}</button><input type="date" class="duein" value="" tabindex="-1" aria-label="Due date"></span>`;
  return `<div class="trow ${esc(t.status)}${closed ? " off" : ""}" data-id="${esc(t.id)}">
    ${tWho(t)}${tBall(t, tAnim === t.id)}
    <div class="tmain"><p class="ttl">${esc(t.title)}</p>${desc ? `<span class="tdesc">${esc(desc)}</span>` : ""}</div>
    <div class="tags">${tags}${closed ? "" : `<button class="chip add" type="button" title="Add tag">${SVG_TTAG}</button>`}</div>
    ${due}
  </div>`;
}

const tAddRow = key => `<div class="tadd" data-b="${esc(key)}"><span class="who ph"></span><span class="ring18"></span><input id="taddin" placeholder="Task name…" autocomplete="off" spellcheck="false"></div>`;

function tHead() {
  return `<div class="thead">
    <div class="ttop">
      <div><h1>Tasks</h1><div class="tstats" id="tstats"></div></div>
      <button class="pill" id="tnew" type="button">${SVG_TPLUS}New task</button>
    </div>
    <div class="tbar">
      <label class="tsearch">${SVG_TSEARCH}<input id="tsearch" type="text" placeholder="Search by title, tag or content..." autocomplete="off" spellcheck="false"></label>
      <div class="qf" id="tqf">${T_FILTERS.map(([k, l]) => `<button type="button" data-f="${k}" class="${tFilter === k ? "on" : ""}">${l}</button>`).join("")}</div>
    </div>
  </div><div class="tbody" id="tbody"></div>`;
}

function tStats() {
  const all = state.tasks || [], today = tToday();
  const doing = all.filter(t => t.status === "doing").length;
  const over = all.filter(t => t.due && !tClosed(t) && tDay(t.due) < today).length;
  return `<span><b>${all.length}</b> ${all.length === 1 ? "task" : "tasks"}</span><i>·</i><span><b class="blue">${doing}</b> in progress</span>`
    + (over ? `<i>·</i><span><b class="over">${over}</b> overdue</span>` : "");
}

function paintTasks() {
  const p = $("#taskspanel"); if (!p) return;
  const c = $("#tcount"); if (c) { const n = tCount(); c.textContent = n ? String(n) : ""; }
  if (!p.querySelector(".thead")) { p.innerHTML = tHead(); tWireHead(p); }
  const st = $("#tstats"); if (st) st.innerHTML = tStats();
  if (tEditingNow()) return;
  if (Date.now() < tHold) { clearTimeout(tHoldT); tHoldT = setTimeout(paintTasks, tHold - Date.now() + 20); return; }

  const prev = $("#taddin");
  const keep = prev ? prev.value : "";
  const hadFocus = prev && document.activeElement === prev;

  const tasks = (state.tasks || []).filter(tMatch);
  const groups = {};
  tasks.forEach(t => { const b = tBucket(t); (groups[b] = groups[b] || []).push(t); });
  let html = "";
  T_BUCKETS.forEach(([key, label]) => {
    const rows = groups[key] || [];
    if (!rows.length && tAdding !== key) return;
    const fold = !!tFold[key] && tAdding !== key;
    html += `<section class="tsec${fold ? " closed" : ""}" data-b="${key}">
      <div class="tsh">
        <button class="tst" type="button">${SVG_TCHEV}<h2>${esc(label)}</h2><span class="c">${rows.length}</span></button>
        ${key !== "done" ? `<button class="tplus" type="button" title="Add task to ${esc(label)}">${SVG_TPLUS}</button>` : ""}
      </div>
      <div class="tsb"><div class="tsi">${rows.map(tRow).join("")}${tAdding === key ? tAddRow(key) : ""}</div></div>
    </section>`;
  });
  if (!html) {
    html = `<div class="tempty"><div class="ic">${SVG_TINBOX}</div>
      <p class="t">${!state.biz ? "Pick a business first." : (state.tasks || []).length ? "Nothing here." : "Nothing scheduled."}</p>
      <p class="s">${(state.tasks || []).length ? "No task matches this view." : "Create your first task to start organizing what matters."}</p>
      ${state.biz ? `<button class="pill create" type="button">Create task</button>` : ""}</div>`;
  } else if (!tAdding) {
    html += `<button class="taddbtn" id="taddbottom" type="button">${SVG_TPLUS}Add task</button>`;
  }
  const body = $("#tbody"); if (!body) return;
  body.innerHTML = html;
  tAnim = null;

  body.querySelectorAll(".trow").forEach(row => {
    const t = taskById(row.dataset.id); if (!t) return;
    const who = row.querySelector(".who"); if (who && typeof tip === "function") tip(who, () => tWhoTip(t));
  });
  const add = $("#taddin");
  if (add) {
    if (keep && prev && prev.closest(".tadd") && prev.closest(".tadd").dataset.b === tAdding) add.value = keep;
    if (hadFocus || tAdding) { add.focus(); add.setSelectionRange(add.value.length, add.value.length); }
    tWireAdd(add);
  }
}

// ---------------------------------------------------------------- add
function tOpenAdd(key) {
  tAdding = key;
  if (tFold[key]) { tFold[key] = false; ls("tasksfold2", JSON.stringify(tFold)); }
  paintTasks();
  const el = $("#taddin"); if (el) { el.focus(); el.scrollIntoView({ block: "nearest" }); }
}

function tWireAdd(inp) {
  let settled = false;
  const commit = async () => {
    if (settled) return; settled = true;
    const title = inp.value.trim(), key = tAdding;
    tAdding = null;
    if (!title) return paintTasks();
    inp.disabled = true;
    const due = key === "today" ? tYmd(new Date()) : key === "tomorrow" ? tYmd(new Date(Date.now() + T_DAY)) : "";
    const r = await tadd({ title, owner: "", due });
    if (r && r.id) tAnim = r.id;
    paintTasks();
  };
  inp.onkeydown = e => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); settled = true; tAdding = null; paintTasks(); }
  };

  inp.onblur = () => { if (inp.isConnected && !settled) commit(); };
}

function tNewModal() {
  if (typeof modal !== "function") return tOpenAdd("someday");
  let owner = "", tags = [];
  const chips = T_TAGS.map(x => `<button class="chip tog" type="button" data-tag="${esc(x)}"><span class="tdot" style="--tc:${tTagColor(x)}"></span><span class="tn">${esc(x)}</span></button>`).join("");
  const body = `<div class="tform">
    <input id="tf-title" type="text" placeholder="What needs to be done?" autocomplete="off">
    <div class="grid">
      <div><label class="field">Assignee</label><button class="selbtn" id="tf-who" type="button"><span class="v">Unassigned</span></button></div>
      <div><label class="field">Due date</label><input id="tf-due" type="date"></div>
    </div>
    <div><label class="field">Tags</label><div class="tags">${chips}</div></div>
    <textarea id="tf-note" placeholder="Add a note (optional)…"></textarea>
  </div>`;
  modal({
    title: "New task", width: 560, body,
    actions: [
      { label: "Cancel", onClick: close => close() },
      { label: "Create task", primary: true, onClick: async (close, el) => {
        const title = el.querySelector("#tf-title").value.trim(); if (!title) return toast("a title, please");
        const due = el.querySelector("#tf-due").value || "", note = el.querySelector("#tf-note").value.trim();
        close();
        const r = await tadd({ title, owner, due, tags, note });
        if (r && r.id) { tAnim = r.id; paintTasks(); }
      } },
    ],
    onOpen(el) {
      const who = el.querySelector("#tf-who");
      who.onclick = () => dropdown(who, tWhoItems(), v => { owner = v; who.querySelector(".v").innerHTML = v ? `${tWhoInner(v)} ${esc(tShort(v))}` : "Unassigned"; }, owner);
      el.querySelectorAll(".chip.tog").forEach(ch => ch.onclick = () => {
        const x = ch.dataset.tag; tags = tags.indexOf(x) >= 0 ? tags.filter(y => y !== x) : tags.concat([x]);
        ch.classList.toggle("on", tags.indexOf(x) >= 0);
      });
      el.querySelector("#tf-title").onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); el.querySelector(".mfoot .primary").click(); } };
    },
  });
}

// ---------------------------------------------------------------- row actions
// optimistic: the ball changes at once (and pops), the write follows; a failed write refreshes
function tSetStatus(t, next) {
  if (!next || next === t.status) return;
  const id = t.id;
  t.status = next; tAnim = id;
  if (next === "done") {
    tCompleting.add(id);
    paintTasks(); tHold = Date.now() + T_DONE_HOLD + 120;
    setTimeout(async () => { tCompleting.delete(id); await tdone(id); }, T_DONE_HOLD);
  } else {
    paintTasks(); tHold = Date.now() + T_POP;
    twrite(id, { set: { status: next } });
  }
}

function tSetOwner(t, o) {
  if (o == null || o === (t.owner || "")) return;
  t.owner = o; paintTasks();
  twrite(t.id, { set: { owner: o } });
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

function tTagItems(t) {
  const have = t.tags || [];
  return T_TAGS.map(x => ({ value: x, label: x, html: `<span class="towner"><span class="tdot" style="--tc:${tTagColor(x)}"></span><span class="tn">${esc(x)}</span>${have.indexOf(x) >= 0 ? `<span class="tsub">on</span>` : ""}</span>` }));
}

function tTagMenu(anchor, t, current) {
  dropdown(anchor, tTagItems(t), v => {
    const have = t.tags || [];
    twrite(t.id, { set: { tags: have.indexOf(v) >= 0 ? have.filter(x => x !== v) : have.concat([v]) } });
  }, current || null);
}

function tPickDue(row) {
  const inp = row.querySelector(".duein"); if (!inp) return;
  try { if (inp.showPicker) return inp.showPicker(); } catch (e) {}
  inp.focus(); inp.click();
}


function tDetail(t) {
  if (typeof modal !== "function") return toast("details need core modal()");
  const closed = tClosed(t);
  const fld = (k, v, mono) => `<div class="k">${esc(k)}</div><div class="v${mono ? " mono" : ""}">${v}</div>`;
  const none = s => `<span class="none">${esc(s)}</span>`;
  const stamp = s => esc(String(s || "").slice(0, 16).replace("T", " "));
  const extra = Object.entries(t.fields || {}).map(([k, v]) => fld(k, esc(String(v)))).join("");
  const notes = (t.notes || []).map(n => `<div class="tnote">
      <div class="nh"><span class="by">${esc(tShort(n.by))}</span><span class="ts">${stamp(n.ts)}</span></div>
      <div class="tx">${esc(n.text)}</div></div>`).join("");
  const tags = (t.tags || []).map(x => `<button class="chip" type="button" data-tag="${esc(x)}" title="Tag: ${esc(x)}"><span class="tdot" style="--tc:${tTagColor(x)}"></span><span class="tn">${esc(x)}</span></button>`).join("")
    + `<button class="chip add on" type="button" title="Add tag">${SVG_TTAG}</button>`;
  const o = t.owner || "";
  const body = `<div class="tdetail">
    <div class="dstat"><span class="dot" style="background:${T_DOT[t.status] || T_DOT.open}"></span>${esc(T_LABEL[t.status] || t.status)}</div>
    <button class="dtitle" type="button" title="Edit title">${esc(t.title)}</button>
    <div class="did">${esc(t.id)}</div>
    <div class="flds">
      ${fld("Status", `<button class="selbtn" id="td-status" type="button"><span class="v"><span class="dot" style="background:${T_DOT[t.status] || T_DOT.open}"></span>${esc(T_LABEL[t.status] || t.status)}</span></button>`)}
      ${fld("Assignee", `<button class="selbtn" id="td-who" type="button"><span class="v">${o ? `${tWhoInner(o)} ${esc(tShort(o))}` : "Unassigned"}</span></button>`)}
      ${fld("Due", `<input id="td-due" type="date" value="${esc(t.due || "")}">`)}
      ${fld("Tags", `<div class="tags">${tags}</div>`)}
      ${fld("Blocked by", (t.blocked_by || []).length ? esc((t.blocked_by || []).join(", ")) : none("nothing"), true)}
      ${fld("Created", stamp(t.created) + " · " + esc(tShort(t.by)), true)}
      ${fld("Updated", stamp(t.updated), true)}
      ${extra}
    </div>
    <h4>Notes${(t.notes || []).length ? " · " + (t.notes || []).length : ""}</h4>
    ${notes || `<div class="tnote"><div class="tx"><span class="none">No notes yet.</span></div></div>`}
    <textarea id="tnotetx" placeholder="Add a note…"></textarea>
    <div class="drow"><button class="ghost" id="tnotebtn" type="button">Add note</button></div>
  </div>`;
  // a write repaints the detail with the fresh task; not if the user closed it while the write was in flight
  const again = async (id, body) => {
    const r = await twrite(id, body);
    if (!m || !m.el || !m.el.isConnected) return;
    m.close();
    const fresh = taskById(id); if (r && fresh) tDetail(fresh);
  };
  const m = modal({
    title: "Task", width: 620, body,
    actions: [
      { label: "Close", onClick: close => close() },
      { label: closed ? "Reopen" : "Mark done", primary: true, onClick: async close => { close(); closed ? await twrite(t.id, { set: { status: "open" } }) : await tdone(t.id); } },
    ],
    onOpen(el) {
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
      sb.onclick = () => dropdown(sb, T_STATES.map(s => ({ value: s, label: T_LABEL[s], html: `<span class="towner"><span class="dot" style="background:${T_DOT[s]}"></span><span class="tn">${T_LABEL[s]}</span></span>` })),
        v => { if (v !== t.status) again(t.id, { set: { status: v } }); }, t.status);
      const wb = el.querySelector("#td-who");
      wb.onclick = () => dropdown(wb, tWhoItems(), v => { if (v !== o) again(t.id, { set: { owner: v } }); }, o);
      el.querySelector("#td-due").onchange = e => again(t.id, { set: { due: e.target.value || "" } });
      el.querySelectorAll(".chip").forEach(ch => ch.onclick = () => dropdown(ch, tTagItems(t), v => {
        const have = t.tags || [];
        again(t.id, { set: { tags: have.indexOf(v) >= 0 ? have.filter(x => x !== v) : have.concat([v]) } });
      }, ch.dataset.tag || null));
      const btn = el.querySelector("#tnotebtn"), tx = el.querySelector("#tnotetx");
      btn.onclick = () => { const v = tx.value.trim(); if (!v) return; btn.disabled = true; again(t.id, { note: v }); };
      tx.onkeydown = e => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); btn.click(); } };
    },
  });
  return m;
}

// ---------------------------------------------------------------- wiring
function tWireHead(p) {
  const nb = p.querySelector("#tnew"); if (nb) nb.onclick = tNewModal;
  const qf = p.querySelector("#tqf");
  if (qf) qf.querySelectorAll("button").forEach(b => b.onclick = () => {
    tFilter = b.dataset.f; ls("tasksflt", tFilter);
    qf.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
    paintTasks();
  });
  const s = p.querySelector("#tsearch");
  if (s) {
    s.oninput = () => { clearTimeout(tSearchT); tSearchT = setTimeout(() => { tSearch = s.value; paintTasks(); }, 80); };
    s.onkeydown = e => { if (e.key === "Escape") { e.stopPropagation(); s.value = ""; tSearch = ""; paintTasks(); s.blur(); } };
  }
}

(function tInit() {
  const wrap = $("#taskswrap"), handle = $("#thandle"), panel = $("#taskspanel");
  if (!wrap || !handle || !panel) return;

  handle.onclick = () => openTasks(!tPanelOpen());
  if (ls("tasks") === "1") wrap.classList.add("open");

  panel.addEventListener("click", e => {
    const st = e.target.closest(".tst");
    if (st) {
      const sec = st.closest(".tsec"), key = sec && sec.dataset.b; if (!key) return;
      tFold[key] = !sec.classList.contains("closed"); ls("tasksfold2", JSON.stringify(tFold));
      sec.classList.toggle("closed", tFold[key]);
      return;
    }
    const plus = e.target.closest(".tplus");
    if (plus) { const sec = plus.closest(".tsec"); return void tOpenAdd(sec ? sec.dataset.b : "someday"); }
    if (e.target.closest("#taddbottom")) return void tOpenAdd("someday");
    if (e.target.closest(".tempty .create")) return void tNewModal();
    if (e.target.closest(".tadd")) return;
    const row = e.target.closest(".trow"); if (!row) return;
    const t = taskById(row.dataset.id); if (!t) return;
    if (e.target.closest(".ball")) return void tSetStatus(t, tNext(t.status));
    const who = e.target.closest(".who");
    if (who) { const nx = tWhoLeft(t.owner || ""); if (nx == null) return toast("no agents in this business"); return void tSetOwner(t, nx); }
    const chip = e.target.closest(".chip");
    if (chip) return void tTagMenu(chip, t, chip.dataset.tag);
    if (e.target.closest(".due")) return void tPickDue(row);
    if (e.target.closest(".duein") || e.target.closest(".tedit")) return;

    if (e.target.closest(".ttl")) { clearTimeout(tClickT); tClickT = setTimeout(() => tDetail(t), T_DBL); return; }
    tDetail(t);
  });

  // right click: blocked on the ball, the human owner on the assignee
  panel.addEventListener("contextmenu", e => {
    const row = e.target.closest(".trow"); if (!row) return;
    const t = taskById(row.dataset.id); if (!t) return;
    if (e.target.closest(".ball")) { e.preventDefault(); return void tSetStatus(t, t.status === "blocked" ? "open" : "blocked"); }
    if (e.target.closest(".who")) { e.preventDefault(); return void tSetOwner(t, tWhoRight(t.owner || "")); }
  });

  panel.addEventListener("dblclick", e => {
    const ttl = e.target.closest(".ttl"); if (!ttl) return;
    const row = ttl.closest(".trow"), t = row && taskById(row.dataset.id); if (!t) return;
    clearTimeout(tClickT);
    if (_modal) _modal.close();
    tEditTitle(row, t);
  });

  panel.addEventListener("change", e => {
    const due = e.target.closest(".duein"); if (!due) return;
    const row = due.closest(".trow"); const t = row && taskById(row.dataset.id); if (!t) return;
    if ((due.value || "") === (t.due || "")) return;
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
  setInterval(() => { if (state.biz && state.biz !== tLastBiz) { tLastBiz = state.biz; tCompleting.clear(); refreshTasks(); } }, 500);
  setInterval(() => { if (state.biz) refreshTasks(); }, 12000);
})();
