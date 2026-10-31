// ---------------------------------------------------------------- agents / chat
const AG_MODEL = "deepseek-v4-flash";
const agRates = () => (typeof RATES === "object" && RATES && RATES[AG_MODEL]) || { hit: 0.014, miss: 0.44, out: 1.32 };
const agCost = (hit, miss, out) => { const r = agRates(); return (hit * r.hit + miss * r.miss + out * r.out) / 1e6; };
const NEW_AGENT_PLUGIN = "biz-agents-marketing@1";
const NAME_RE = /^[a-z][a-z0-9-]{1,31}$/;
const SLASH = [
  { cmd: "compact", hint: "Summarize this conversation into a note the agent reads next time, then clear the view" },
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
async function renderAgents(c) {
  const [agents, live] = await Promise.all([api(`/biz/${state.biz}/agents`), api(`/biz/${state.biz}/live`)]);
  state.agents = Array.isArray(agents) ? agents : []; state.liveNow = Array.isArray(live) ? live : [];
  c.innerHTML = `<div class="split"><div class="listcol" id="agentlist"></div><div class="chat" id="chat"></div></div>`;
  paintList();
  const a = state.agents.find(x => x.name === state.agent); if (!a) return;
  state.usage = blankUsage();
  const ch = $("#chat");
  ch.innerHTML = `<div class="top">
      <button class="aglyph" id="aglyph" title="Change icon"></button><span class="name">${esc(a.name)}</span>
      <button class="badge" id="deptbadge" title="Change department"></button>
      <span class="sp"></span>
      <span class="usage" id="usage"></span>
      <label class="switch ${state.verbose ? "on" : ""}" id="verbose"><span class="k"></span>verbose</label></div>
    <div class="msgs ${state.verbose ? "" : "quiet"}" id="msgs"></div>
    <div class="compose">
      <div class="slash" id="slash" hidden></div>
      <textarea id="box" rows="1" placeholder="Tell ${esc(a.name)} what to do. Enter to send, Shift+Enter for a new line, / for commands."></textarea>
      <button class="primary" id="send">Send</button></div>`;
  paintHead(a); paintUsage();
  $("#aglyph").onclick = () => lookModal(a);
  $("#deptbadge").onclick = () => lookModal(a);
  $("#verbose").onclick = () => { state.verbose = !state.verbose; ls("verbose", state.verbose ? "1" : "0");
    $("#verbose").classList.toggle("on", state.verbose); $("#msgs").classList.toggle("quiet", !state.verbose); $("#msgs").scrollTop = $("#msgs").scrollHeight; };
  const msgs = $("#msgs");
  // history: last 5 sessions newer than the last /clear, oldest first
  const cut = clearedAt(a.name);
  const hist = a.sessions.filter(f => !cut || !(sessTime(f) < cut)).slice(0, 5).reverse();
  for (const f of hist) {
    const ev = await api(`/biz/${state.biz}/session/${a.name}/${f}`);
    if (!Array.isArray(ev)) continue;
    addDay(msgs, f.replace(".jsonl", "").replace(/-(\d{2})(\d{2})(\d{2})$/, " $1:$2 UTC"));
    const acc = sessionUsage(ev);
    ev.forEach(e => addEvent(msgs, e, acc));
    bumpUsage(acc);
  }
  const running = state.liveNow.find(s => s.agent === a.name && !s.done);
  if (running) attach(running.id, msgs);
  msgs.scrollTop = msgs.scrollHeight;
  wireCompose(a, msgs);
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
async function runSlash(cmd, a, msgs, send) {
  if (cmd === "clear") return clearView(a, msgs);
  if (cmd !== "compact") return;
  send.disabled = true;
  const w = pulse(msgs);
  let r; try { r = await post(`/biz/${state.biz}/compact`, { agent: a.name }); } catch (e) { r = { error: "could not reach the server" }; }
  w.remove(); send.disabled = false;
  if (!r || r.error || !r.summary) return toast((r && r.error) || "could not compact");
  clearView(a, msgs, false);
  const d = document.createElement("div"); d.className = "m agent compact";
  d.innerHTML = `<span class="tag">compact</span>${esc(r.summary)}`;
  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
}

// ------------------------------------------------------------------- list
function paintList() {
  const al = $("#agentlist"); if (!al) return;
  const rows = state.agents.map(a => {
    const running = state.liveNow.some(s => s.agent === a.name && !s.done), d = dept(a.department);
    return `<div class="item agent ${a.name === state.agent ? "on" : ""}" data-a="${esc(a.name)}">
      <div class="nm">${agentGlyph(a, 10)}<span>${esc(a.name)}</span></div>
      <div class="st">${glyph(d.icon, d.color, 8)}<span>${esc(d.label)}</span><span>·</span><span class="${running ? "live" : ""}">${running ? "working" : a.sessions.length + " session" + (a.sessions.length === 1 ? "" : "s")}</span></div></div>`;
  }).join("") || `<div class="empty" style="padding:0 24px">No agents yet.</div>`;
  al.innerHTML = `<div class="lhead"><span class="h">Agents</span><button class="plus" id="addagent" title="New agent" aria-label="New agent">+</button></div>` + rows;
  $("#addagent").onclick = newAgentModal;
  al.querySelectorAll(".agent").forEach(d => d.onclick = () => { state.agent = d.dataset.a; renderAgents($("#content")); });
}
function paintHead(a) {
  const ch = $("#chat"); if (!ch) return;
  const g = $("#aglyph"); if (g) g.innerHTML = agentGlyph(a, 18);
  const b = $("#deptbadge"); const d = dept(a.department);
  if (b) b.innerHTML = `${glyph(d.icon, d.color, 8)}<span>${esc(d.label)}</span>`;
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
const shapeRow = (cur, colorHex) => SHAPES.map(s =>
  `<button class="sbtn ${s === cur ? "on" : ""}" data-s="${s}" title="${s}">${glyph(s, colorHex, 28)}</button>`).join("");
const colorRow = cur => Object.keys(COLORS).map(k =>
  `<button class="cdot ${k === cur ? "on" : ""}" data-c="${k}" title="${k}" style="--dc:${COLORS[k]}"></button>`).join("");
const deptItems = () => Object.keys(DEPTS).map(k => ({ value: k, label: DEPTS[k].label, html: `${glyph(DEPTS[k].icon, DEPTS[k].color, 8)}<span>${esc(DEPTS[k].label)}</span>` }));
const deptBtn = k => `<span class="v">${glyph(dept(k).icon, dept(k).color, 8)}<span>${esc(dept(k).label)}</span></span>`;

// shared wiring for the shape / colour / department trio inside a modal body
function wirePickers(el, pick, prefix, onChange) {
  const prev = el.querySelector(`#${prefix}prev`);
  const repaint = () => {
    if (prev) prev.innerHTML = glyph(pick.icon, hex(pick.color), 34);
    el.querySelectorAll(`#${prefix}shapes .sbtn`).forEach(b => {
      b.classList.toggle("on", b.dataset.s === pick.icon); b.innerHTML = glyph(b.dataset.s, hex(pick.color), 28);
    });
    el.querySelectorAll(`#${prefix}colors .cdot`).forEach(b => b.classList.toggle("on", b.dataset.c === pick.color));
    if (onChange) onChange();
  };
  el.querySelectorAll(`#${prefix}shapes .sbtn`).forEach(b => b.onclick = () => { pick.icon = b.dataset.s; repaint(); });
  el.querySelectorAll(`#${prefix}colors .cdot`).forEach(b => b.onclick = () => { pick.color = b.dataset.c; repaint(); });
  const db = el.querySelector(`#${prefix}dept`);
  if (db) db.onclick = () => dropdown(db, deptItems(), v => { pick.department = v; db.innerHTML = deptBtn(v); }, pick.department);
  repaint();
}

function lookModal(a) {
  const pick = { icon: a.icon, color: a.color, department: DEPTS[a.department] ? a.department : "general" };
  const body = `<div class="prev" id="lkprev"></div>
    <div class="fl">Shape</div><div class="shapes" id="lkshapes">${shapeRow(pick.icon, hex(pick.color))}</div>
    <div class="fl">Colour</div><div class="swatches" id="lkcolors">${colorRow(pick.color)}</div>
    <div class="fl">Department</div><button class="selbtn" id="lkdept">${deptBtn(pick.department)}</button>`;
  modal({
    title: "Agent look", body, width: 440,
    onOpen: el => wirePickers(el, pick, "lk"),
    actions: [
      { label: "Cancel", onClick: close => close() },
      { label: "Save", primary: true, onClick: async close => { if (await applyMeta(a, pick)) close(); } },
    ],
  });
}

function newAgentModal() {
  const pick = { icon: "circle", color: "red", department: "marketing" };
  const body = `<div class="prev" id="naprev"></div>
    <div class="fl">Name</div><input class="inp" id="naname" placeholder="growth" autocomplete="off" spellcheck="false">
    <div class="fmsg" id="namsg">lowercase letters, digits and dashes, 2–32 characters</div>
    <div class="fl">Plugin</div><input class="inp" id="naplugin" value="${esc(NEW_AGENT_PLUGIN)}" autocomplete="off" spellcheck="false">
    <div class="fl">Department</div><button class="selbtn" id="nadept">${deptBtn(pick.department)}</button>
    <div class="fl">Shape</div><div class="shapes" id="nashapes">${shapeRow(pick.icon, hex(pick.color))}</div>
    <div class="fl">Colour</div><div class="swatches" id="nacolors">${colorRow(pick.color)}</div>
    <div class="ferr" id="naerr" hidden></div>`;
  const m = modal({
    title: "New agent", body, width: 460,
    onOpen: el => {
      wirePickers(el, pick, "na");
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
        err.hidden = true;
        const plugin = el.querySelector("#naplugin").value.trim() || NEW_AGENT_PLUGIN;
        let r; try { r = await post(`/biz/${state.biz}/agent`, { name, plugin, department: pick.department, icon: pick.icon, color: pick.color }); }
        catch (e) { r = { error: "could not reach the server" }; }
        if (!r || r.error) return fail((r && r.error) || "could not create the agent");
        if (!Array.isArray(r.sessions)) r.sessions = [];
        state.agents.push(r); state.agent = r.name;
        close(); toast(`${r.name} created`);
        renderAgents($("#content")); loadBiz();
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
  const es = new EventSource(`/api/session/${sid}/events`); state.live = es;
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
