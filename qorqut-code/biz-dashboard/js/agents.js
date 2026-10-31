// ---------------------------------------------------------------- agents / chat
const AG_MODEL = "deepseek-v4-flash";
const agRates = () => (typeof RATES === "object" && RATES && RATES[AG_MODEL]) || { hit: 0.014, miss: 0.44, out: 1.32 };
const agCost = (hit, miss, out) => { const r = agRates(); return (hit * r.hit + miss * r.miss + out * r.out) / 1e6; };
const NAME_RE = /^[a-z][a-z0-9-]{1,31}$/;
const SLASH = [
  { cmd: "clear", hint: "Clear the view; sessions stay on disk" },
];

// ------------------------------------------------------------------- usage
function blankUsage() { return { hit: 0, miss: 0, out: 0, cost: 0, est: false }; }
function addUsage(t, u) {
  if (!u) return t;
  t.hit += Number(u.hit) || 0; t.miss += Number(u.miss) || 0; t.out += Number(u.out) || 0;
  t.cost += Number(u.cost) || 0; if (u.est) t.est = true;
  return t;
}
// normalise an `end.usage` blob: new shape (hit/miss/out/cost) or old (prompt_tokens/completion_tokens)
function endUsage(u) {
  if (!u) return null;
  if (u.hit != null || u.miss != null || u.out != null) {
    const hit = Number(u.hit) || 0, miss = Number(u.miss) || 0, out = Number(u.out) || 0;
    const est = u.cost == null;
    return { hit, miss, out, cost: est ? agCost(hit, miss, out) : Number(u.cost) || 0, est };
  }
  if (u.prompt_tokens != null || u.completion_tokens != null) {
    const miss = Number(u.prompt_tokens) || 0, out = Number(u.completion_tokens) || 0;
    return { hit: 0, miss, out, cost: agCost(0, miss, out), est: true };
  }
  return null;
}
// per-call usage for one finished session, falling back to the end event
function sessionUsage(ev) {
  const t = blankUsage(); let any = false;
  for (const e of ev) if ((e.kind === "assistant" || e.kind === "learnings") && e.usage) { addUsage(t, e.usage); any = true; }
  if (!any) { const end = ev.find(e => e.kind === "end"); const u = end && endUsage(end.usage); if (u) addUsage(t, u); }
  return t;
}
function paintUsage() {
  const el = $("#usage"); if (!el) return;
  const u = state.usage || blankUsage();
  const tok = n => (typeof fmtTok === "function" ? fmtTok(n) : String(n));
  const usd = x => (typeof fmtUsd === "function" ? fmtUsd(x) : "$" + Number(x).toFixed(4));
  el.innerHTML = `<span class="uf"><span class="ul">cached</span><span class="un">${tok(u.hit)}</span></span>`
    + `<span class="uf"><span class="ul">uncached</span><span class="un">${tok(u.miss)}</span></span>`
    + `<span class="uf"><span class="ul">out</span><span class="un">${tok(u.out)}</span></span>`
    + `<span class="uf money"><span class="un">${u.est ? "~" : ""}${usd(u.cost)}</span></span>`;
}
function bumpUsage(u) { addUsage(state.usage || (state.usage = blankUsage()), u); paintUsage(); }

// ------------------------------------------------------------------- pane
// The Agents tab: each agent IS a live terminal bound to its background PTY on the server.
// Left = the agent list, middle = the mirrored terminal, right = the agent tasks and activity pane.
// remember which agent was focused, per business — same shape as notes.js's ntKey/ntSetSel
const agKey = () => `ag:${state.biz}`;
function agSetSel(name) {
  state.agent = name;
  if (state.biz) ls(agKey(), name || "");
}

let agRenderGen = 0;
const agPageCache = new Map();
async function renderAgents(c) {
  const gen = ++agRenderGen, biz = state.biz;
  c.innerHTML = `<div class="split agsplit"><div class="listcol" id="agentlist"><div class="ctop"><div class="agent-nav-heading"><h2>Agents</h2>${broadcastButton()}</div><button class="primary" id="addagent">${ICO_PLUS}New agent</button></div><div class="crows" id="agentrows"></div></div><div class="chat termmode" id="chat"></div><aside class="tasks agpad" id="agpad" aria-label="Agent overview"></aside></div>`;
  wireBroadcastButton(c, {scope:'business',biz});
  $("#addagent").onclick = newAgentModal;
  $("#agentrows", c).innerHTML = `<p class="empty pad" role="status">Loading agents…</p>`;
  const cached = agPageCache.get(biz);
  if (cached) {
    state.agents = cached.agents; state.terms = cached.terms;
    paintList();
    const selected = state.agents.find(a => a.name === (state.agent || ls(agKey()))) || state.agents[0];
    if (selected) {
      agSetSel(selected.name); paintList();
      mountTerminal(selected.name, $("#chat"), stat => markTermState(selected.name, stat));
      agpadMount(selected.name, $("#agpad"));
    }
  }
  let agents, terms;
  try { [agents, terms] = await Promise.all([
    api(`/biz/${biz}/agents`),
    api(`/biz/${biz}/terminals`),
  ]); } catch (error) {
    if (!cached && gen === agRenderGen && biz === state.biz && state.tab === "agents") $("#agentrows", c).innerHTML = `<p class="empty pad" role="alert">${esc(error.message || "Could not load agents.")}</p>`;
    return;
  }
  if (gen !== agRenderGen || biz !== state.biz || state.tab !== "agents" || !c.isConnected) return;
  state.agents = Array.isArray(agents) ? agents : [];
  state.terms = Array.isArray(terms) ? terms : [];
  agPageCache.set(biz, {agents:state.agents, terms:state.terms});
  paintList();
  if (cached?.agents.some(a => a.name === state.agent) && state.agents.some(a => a.name === state.agent)) return;
  const want = state.agent || ls(agKey());
  const a = state.agents.find(x => x.name === want) || state.agents[0];
  if (!a) { agSetSel(null); $("#chat").innerHTML = `<div class="empty" style="padding:24px">No agents yet — add one with New agent.</div>`; agpadMount(null, $("#agpad")); return; }
  agSetSel(a.name);
  paintList();
  mountTerminal(a.name, $("#chat"), stat => markTermState(a.name, stat));
  agpadMount(a.name, $("#agpad"));
}

function selectAgent(name) {
  if (state.tab !== 'agents' || name === state.agent) return;
  const agent = state.agents.find(item => item.name === name);
  const chat = $('#chat'), pane = $('#agpad');
  if (!agent || !chat || !pane) return;
  agRenderGen++;
  agSetSel(name);
  document.querySelectorAll('#agentrows .agent').forEach(row => row.classList.toggle('on', row.dataset.a === name));
  mountTerminal(name, chat, status => markTermState(name, status));
  agpadMount(name, pane);
}

// reflect a terminal's live/ended state on its row in the agent list, without a refetch
function markTermState(name, stat) {
  const running = stat === "live";
  const t = (state.terms || []).find(x => x.agent === name);
  if (t) t.done = !running; else (state.terms = state.terms || []).push({ agent: name, done: !running });
  const row = $(`#agentlist .agent[data-a="${(window.CSS && CSS.escape) ? CSS.escape(name) : name}"] [data-st]`);
  if (row) { row.classList.toggle("live", running); row.title = running ? "running" : (stat === "ended" ? "exited" : "idle"); }
}

function wireCompose(a, msgs) {
  const box = $("#box"), send = $("#send"), sl = $("#slash");
  const autosize = () => {
    box.style.height = "auto";
    const chrome = box.offsetHeight - box.clientHeight;          // borders only
    const need = box.scrollHeight + chrome;
    box.style.height = Math.min(160, need) + "px";
    box.style.overflowY = need > 160 ? "auto" : "hidden";
  };
  autosize();
  // ---- slash popover
  let items = [];
  const hideSlash = () => { sl.hidden = true; items = []; };
  const showSlash = q => {
    items = SLASH.filter(s => s.cmd.startsWith(q));
    if (!items.length) return hideSlash();
    sl.innerHTML = items.map((s, i) => `<div class="si ${i ? "" : "on"}" data-cmd="${s.cmd}"><span class="c">/${s.cmd}</span><span class="d">${esc(s.hint)}</span></div>`).join("");
    sl.hidden = false;
    sl.querySelectorAll(".si").forEach(r => { r.onmousedown = e => { e.preventDefault(); pick(r.dataset.cmd); }; });
  };
  const move = d => {
    const rows = [...sl.querySelectorAll(".si")]; if (!rows.length) return;
    let i = rows.findIndex(r => r.classList.contains("on")); i = (i + d + rows.length) % rows.length;
    rows.forEach((r, j) => r.classList.toggle("on", j === i)); rows[i].scrollIntoView({ block: "nearest" });
  };
  const pick = cmd => { hideSlash(); box.value = ""; autosize(); runSlash(cmd, a, msgs, send); };
  const syncSlash = () => {
    const v = box.value;
    if (v[0] === "/" && !/\s/.test(v)) showSlash(v.slice(1).toLowerCase()); else hideSlash();
  };
  // ---- send
  const go = async () => {
    const m = box.value.trim(); if (!m) return;
    if (m[0] === "/") { const c = m.slice(1).toLowerCase(); if (SLASH.some(s => s.cmd === c)) return pick(c); }
    send.disabled = true; box.value = ""; autosize();
    const r = await post(`/biz/${state.biz}/chat`, { agent: a.name, message: m });
    if (r.error) { toast(r.error); send.disabled = false; return; }
    addDay(msgs, "now"); addEvent(msgs, { kind: "start", user: m }); attach(r.session, msgs);
  };
  send.onclick = go;
  box.oninput = () => { autosize(); syncSlash(); };
  box.onblur = hideSlash;
  box.onkeydown = e => {
    if (!sl.hidden) {
      if (e.key === "ArrowDown") { e.preventDefault(); return move(1); }
      if (e.key === "ArrowUp") { e.preventDefault(); return move(-1); }
      if (e.key === "Escape") { e.preventDefault(); return hideSlash(); }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); const on = sl.querySelector(".si.on"); return pick(on ? on.dataset.cmd : items[0].cmd); }
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); }
  };
}

// ------------------------------------------------------------ slash commands
const clearKey = name => `clear:${state.biz}:${name}`;
function clearedAt(name) { const v = Date.parse(ls(clearKey(name)) || ""); return isNaN(v) ? 0 : v; }
function sessTime(f) {
  const m = /^(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(f || "");
  return m ? Date.parse(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z`) : NaN;
}
function clearView(a, msgs, line) {
  ls(clearKey(a.name), new Date().toISOString());
  msgs.innerHTML = ""; state.usage = blankUsage(); paintUsage();
  if (line !== false) { const d = document.createElement("div"); d.className = "m sys note"; d.textContent = "cleared"; msgs.appendChild(d); }
}
function pulse(msgs) {
  const w = document.createElement("div"); w.className = "m sys working"; w.textContent = "working";
  msgs.appendChild(w); msgs.scrollTop = msgs.scrollHeight; return w;
}
function runSlash(cmd, a, msgs) {
  if (cmd === "clear") clearView(a, msgs);
}

// Stable flat navigation: business lead first, then the rest alphabetically.
function agentNavOrder() {
  return [...state.agents].sort((a,b) => (a.role?.id==='onbashy'||a.name.toLowerCase()==='onbashy'?-1:0)-(b.role?.id==='onbashy'||b.name.toLowerCase()==='onbashy'?-1:0) || a.name.localeCompare(b.name)).map(a=>a.name);
}

// ------------------------------------------------------------------ context menu
// A hand-rolled right-click menu, one at a time: fixed div in document.body, clamped to the
// viewport after mount, dismissed on outside mousedown / another contextmenu / scroll / resize / Escape.
let _cmenuEl = null;
function closeCmenu() {
  if (!_cmenuEl) return;
  const el = _cmenuEl; _cmenuEl = null;
  document.removeEventListener("mousedown", el._onDoc, true);
  document.removeEventListener("contextmenu", el._onDoc, true);
  document.removeEventListener("keydown", el._onKey, true);
  window.removeEventListener("scroll", el._onAway, true);
  window.removeEventListener("resize", el._onAway, true);
  el.remove();
}
function ctxMenu(x, y, items) {
  closeCmenu();
  const el = document.createElement("div");
  el.className = "cmenu";
  el.innerHTML = items.map((it, i) => `<button type="button" class="ci ${it.danger ? "danger" : ""}" data-i="${i}">${it.icon}<span>${esc(it.label)}</span></button>`).join("");
  document.body.appendChild(el);
  _cmenuEl = el;
  el.querySelectorAll(".ci").forEach((b, i) => b.onclick = e => { e.stopPropagation(); closeCmenu(); items[i].onClick(); });
  const r = el.getBoundingClientRect();
  const cx = Math.min(x, window.innerWidth - r.width - 6);
  const cy = Math.min(y, window.innerHeight - r.height - 6);
  el.style.left = Math.max(6, cx) + "px";
  el.style.top = Math.max(6, cy) + "px";
  el._onDoc = e => { if (!el.contains(e.target)) closeCmenu(); };
  el._onKey = e => { if (e.key === "Escape") closeCmenu(); };
  el._onAway = () => closeCmenu();
  document.addEventListener("mousedown", el._onDoc, true);
  document.addEventListener("contextmenu", el._onDoc, true);
  document.addEventListener("keydown", el._onKey, true);
  window.addEventListener("scroll", el._onAway, true);
  window.addEventListener("resize", el._onAway, true);
  return el;
}
const ICO_PIN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>`;
const ICO_PENCIL = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg>`;
const ICO_TRASH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>`;
const ICO_PLUS = `<svg class="ico-plus" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg>`;

function paintList() {
  const rows=$('#agentrows'); if(!rows)return;
  const live=new Set((state.terms||[]).filter(t=>!t.done).map(t=>t.agent));
  rows.innerHTML=agentNavOrder().map(name=>{
    const a=state.agents.find(a=>a.name===name);
    return `<button type="button" class="item agent ${name===state.agent?'on':''}" data-a="${esc(name)}"><span class="nm">${identityAvatar('agent',name,a)}<span class="tt">${esc(name)}</span></span><span class="ldot ${live.has(name)?'live':''}" data-st></span></button>`;
  }).join('') || '<p class="empty pad">No agents yet.</p>';
  rows.querySelectorAll('[data-a]').forEach(b=>{b.onclick=()=>selectAgent(b.dataset.a);b.oncontextmenu=e=>{e.preventDefault();lookModal(state.agents.find(a=>a.name===b.dataset.a));};});
}
function paintHead(a) {
  const ch = $("#chat"); if (!ch) return;
  const g = $("#aglyph"); if (g) { g.innerHTML = identityAvatar("agent", a.name, a); g.title = "Change look"; g.onclick = () => lookModal(a); }
  const b = $("#deptbadge"); const d = dept(a.department);
  if (b) b.innerHTML = `${departmentIcon(a.department, 14)}<span>${esc(d.label)}</span>`;
}
async function applyMeta(a, patch) {
  let r; try { r = await post(`/biz/${state.biz}/agent/${a.name}/meta`, patch); } catch (e) { r = { error: "could not reach the server" }; }
  if (!r || r.error) { toast((r && r.error) || "could not save"); return false; }
  a.icon = r.icon; a.color = r.color; a.department = r.department;
  const b = state.bizs.find(x => x.slug === state.biz);
  if (b) (b.agent_meta = b.agent_meta || {})[a.name] = { icon: a.icon, color: a.color, department: a.department };
  paintHead(a); paintList(); paintBiz();
  return true;
}

// ------------------------------------------------------------------ modals
const deptItems = () => Object.keys(DEPTS).map(k => ({ value: k, label: DEPTS[k].label, html: `${departmentIcon(k, 14)}<span>${esc(DEPTS[k].label)}</span>` }));
const deptBtn = k => `<span class="v">${departmentIcon(k, 14)}<span>${esc(dept(k).label)}</span></span>`;

let _market = null;
async function marketplace() {
  if (_market) return _market;
  try { _market = await api("/marketplace"); } catch (e) { _market = { kinds: [], error: "could not reach the server" }; }
  if (!Array.isArray(_market.kinds)) _market.kinds = [];
  return _market;
}
const kindItems = kinds => kinds.map(k => ({
  value: k.id, label: k.label,
  html: `${departmentIcon(k.department || "general", 14)}<span class="kd"><span class="kl">${esc(k.label)}${k.available ? "" : ` <em>soon</em>`}</span><span class="kb">${esc(k.blurb || "")}</span></span>`,
}));
const kindBtn = k => k ? `<span class="v">${departmentIcon(k.department || "general", 14)}<span>${esc(k.label)}</span>${k.available ? "" : `<em class="soon">soon</em>`}</span>` : `<span class="v muted">pick a kind</span>`;

function lookModal(a) {
  const startDept = departmentId(a.department || "general");
  const pick = { department: startDept, icon: a.icon && AICONS[a.icon] ? a.icon : null };
  const igrid = `<div class="igrid" style="display:flex;flex-wrap:wrap;gap:8px">
    <button type="button" class="ibtn" data-icon="" style="width:32px;height:32px;padding:0;display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--line);background:none;border-radius:var(--r-sm);cursor:pointer"></button>
    ${Object.keys(AICONS).map(k => `<button type="button" class="ibtn" data-icon="${k}" style="width:32px;height:32px;padding:0;display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--line);background:none;border-radius:var(--r-sm);cursor:pointer"></button>`).join("")}
  </div>`;
  const body = `<div class="prev" id="lkprev"></div>
    <div class="fl">Department</div><button class="selbtn" id="lkdept">${deptBtn(pick.department)}</button>
    <div class="fl">Icon</div>${igrid}
    <div class="fmsg">the department fixes the colour; pick an icon or leave it as the department icon</div>`;
  modal({
    title: "Agent look", body, width: 440,
    onOpen: el => {
      const prev = el.querySelector("#lkprev"), db = el.querySelector("#lkdept");
      const ibtns = [...el.querySelectorAll(".ibtn")];
      const repaint = () => {
        const d = dept(pick.department);
        prev.innerHTML = agentGlyph({ ...a, department: pick.department, icon: pick.icon }, 34);
        db.innerHTML = deptBtn(pick.department);
        ibtns.forEach(b => {
          const k = b.dataset.icon;
          b.innerHTML = k ? aicon(k, d.color, 18) : departmentIcon(pick.department, 18);
          b.style.borderColor = (k || null) === (pick.icon || null) ? "rgba(255,255,255,.34)" : "var(--line)";
          b.style.background = (k || null) === (pick.icon || null) ? "var(--wash)" : "none";
        });
      };
      db.onclick = () => dropdown(db, deptItems(), v => { pick.department = v; repaint(); }, pick.department);
      ibtns.forEach(b => b.onclick = () => { pick.icon = b.dataset.icon || null; repaint(); });
      repaint();
    },
    actions: [
      { label: "Cancel", onClick: close => close() },
      { label: "Save", primary: true, onClick: async close => {
        const patch = {};
        if (pick.department !== startDept) patch.department = pick.department;
        const startIcon = a.icon && AICONS[a.icon] ? a.icon : null;
        if (pick.icon !== startIcon) patch.icon = pick.icon || dept(pick.department).icon;
        if (!Object.keys(patch).length) return close();
        if (await applyMeta(a, patch)) close();
      } },
    ],
  });
}

function newAgentModal() {
  const biz = state.biz, pick = { kind: null, department: "general" };
  const body = `<div class="prev" id="naprev"></div>
    <div class="fl">Name</div><input class="inp" id="naname" placeholder="growth" autocomplete="off" spellcheck="false">
    <div class="fmsg" id="namsg">lowercase letters, digits and dashes, 2–32 characters</div>
    <div class="fl">Role</div><select class="inp" id="narole" aria-label="Role" required><option value="">Loading roles...</option></select>
    <div class="fmsg" id="nakmsg"></div>
    <label class="fl" for="nabrief">Assignment</label><textarea class="inp ta" id="nabrief" rows="4" required></textarea>
    <div class="fl">Department</div><button class="selbtn" id="nadept">${deptBtn(pick.department)}</button>
    <div class="ferr" id="naerr" hidden></div>`;
  const m = modal({
    title: "New agent", body, width: 460,
    onOpen: async el => {
      const prev = el.querySelector("#naprev"), kb = el.querySelector("#narole"), db = el.querySelector("#nadept"), kmsg = el.querySelector("#nakmsg");
      const repaint = () => { const d = dept(pick.department); prev.innerHTML = agentGlyph({ department: pick.department }, 34); db.innerHTML = deptBtn(pick.department); };
      db.onclick = () => dropdown(db, deptItems(), v => { pick.department = v; repaint(); }, pick.department);
      const mk = await marketplace();
      if (!el.isConnected || state.biz !== biz) return;
      if (mk.error) kmsg.textContent = mk.error;
      const roles = mk.kinds.filter(k => k.available && k.role && k.role.digest && k.role.version);
      kb.innerHTML = '<option value="">Select a role</option>' + roles.map((k, i) => `<option value="${i}">${esc(k.label)} · ${esc(k.role.version)}</option>`).join("");
      kb.onchange = () => {
        pick.kind = kb.value === "" ? null : roles[Number(kb.value)];
        kmsg.textContent = pick.kind ? `${pick.kind.role.id} · ${pick.kind.role.version}` : "";
        repaint();
      };
      repaint();
      const inp = el.querySelector("#naname"), msg = el.querySelector("#namsg");
      const check = () => {
        const v = inp.value.trim(), ok = NAME_RE.test(v);
        inp.classList.toggle("bad", !!v && !ok);
        msg.textContent = !v ? "lowercase letters, digits and dashes, 2–32 characters"
          : ok ? "looks good" : "must start with a letter: lowercase, digits and dashes, 2–32 characters";
        msg.classList.toggle("ok", !!v && ok); msg.classList.toggle("bad", !!v && !ok);
        return ok;
      };
      inp.oninput = check; inp.focus();
    },
    actions: [
      { label: "Cancel", onClick: close => close() },
      { label: "Create", primary: true, onClick: async (close, el) => {
        const err = el.querySelector("#naerr"), inp = el.querySelector("#naname");
        const name = inp.value.trim();
        const fail = t => { err.hidden = false; err.textContent = t; };
        if (!NAME_RE.test(name)) return fail("that name will not do: ^[a-z][a-z0-9-]{1,31}$");
        if (state.agents.some(x => x.name === name)) return fail(`${name} already exists`);
        if (state.biz !== biz) return fail("The selected business changed. Reopen this form.");
        if (!pick.kind || !pick.kind.role) return fail("Select a published role.");
        const brief = el.querySelector("#nabrief").value;
        if (!brief.trim()) return fail("Enter this agent's assignment.");
        err.hidden = true;
        const button = el.querySelector(".mfoot .primary"); button.disabled = true;
        try {
          coreData(await post(`/biz/${encodeURIComponent(biz)}/agent`, { name, role: pick.kind.role, assignment_brief: brief, department: pick.department }));
          close(); toast(`${name} created`);
          if (state.biz === biz) { agSetSel(name); await loadBiz(); if (state.tab === "agents") renderAgents($("#content")); }
        } catch (e) { fail(e.message || "Could not create the agent."); }
        finally { button.disabled = false; }
      } },
    ],
  });
  return m;
}

// ---------------------------------------------------------------- messages
function addDay(msgs, t) { const d = document.createElement("div"); d.className = "m day"; d.textContent = t; msgs.appendChild(d); }
function addEvent(msgs, e, acc) {
  let d = document.createElement("div");
  if (e.kind === "start") { d.className = "m user"; d.textContent = e.user || ""; }
  else if (e.kind === "assistant") { if (!e.text) return; d.className = "m agent"; d.textContent = e.text; }
  else if (e.kind === "tool_call") {
    d.className = "m tool"; const a = e.args || {};
    d.innerHTML = `<span>${esc(e.name)}</span> <span class="muted">${esc(a.cmd || a.path || a.summary || "")}</span>` + (e.name === "write_file" ? `<div class="out">${esc((a.content || "").slice(0, 600))}</div>` : "");
  }
  else if (e.kind === "tool_result") {
    if (e.rc === 0 && e.name === "sh" && /^\(no output\)$/.test(e.output || "")) return;
    d.className = "m tool" + (e.rc ? " err" : ""); d.innerHTML = `<div class="out">${esc((e.output || "").slice(0, 700))}</div>`;
  }
  else if (e.kind === "end") {
    d.className = "m sys";
    const u = endUsage(e.usage) || acc || blankUsage();
    const tok = n => (typeof fmtTok === "function" ? fmtTok(n) : String(n));
    const usd = x => (typeof fmtUsd === "function" ? fmtUsd(x) : "$" + Number(x).toFixed(4));
    d.textContent = `ended · ${e.turns || 0} turns · ${tok(u.hit)} cached · ${tok(u.miss)} uncached · ${tok(u.out)} out · ${u.est ? "~" : ""}${usd(u.cost)}`;
    if (e.summary && typeof tip === "function") tip(d, esc(e.summary));
  }
  else if (e.kind === "learnings") { d.className = "m sys"; d.textContent = e.count ? `${e.count} learning${e.count === 1 ? "" : "s"} noted` : "nothing new learned"; }
  else if (e.kind === "error") { d.className = "m tool err fatal"; d.textContent = e.error || "error"; }
  else return;
  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
}
function attach(sid, msgs) {
  if (state.live) state.live.close();
  const es = new EventSource(evUrl(`/session/${sid}/events`)); state.live = es;
  const w = pulse(msgs);
  const acc = blankUsage();
  const stop = () => { es.close(); state.live = null; w.remove(); const s = $("#send"); if (s) s.disabled = false; };
  es.onmessage = ev => {
    const e = JSON.parse(ev.data);
    if ((e.kind === "assistant" || e.kind === "learnings") && e.usage) { addUsage(acc, e.usage); bumpUsage(e.usage); }
    if (e.kind === "end" && !(acc.hit || acc.miss || acc.out)) { const u = endUsage(e.usage); if (u) { addUsage(acc, u); bumpUsage(u); } }
    if (e.kind !== "start") addEvent(msgs, e, acc);
    msgs.appendChild(w);
    if (e.kind === "tool_result") refreshSideSoon();
  };
  es.addEventListener("done", () => { stop(); refreshSide(); });
  es.onerror = stop;
}
