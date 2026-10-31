// ---------------------------------------------------------------- agents / chat
async function renderAgents(c) {
  const [agents, live] = await Promise.all([api(`/biz/${state.biz}/agents`), api(`/biz/${state.biz}/live`)]);
  state.agents = Array.isArray(agents) ? agents : []; state.liveNow = Array.isArray(live) ? live : [];
  c.innerHTML = `<div class="split"><div class="listcol" id="agentlist"></div><div class="chat" id="chat"></div></div>`;
  paintList();
  const a = state.agents.find(x => x.name === state.agent); if (!a) return;
  const ch = $("#chat");
  ch.innerHTML = `<div class="top">
      <span id="aglyph">${agentGlyph(a, 14)}</span><span class="name">${esc(a.name)}</span>
      <span class="pick" id="shapes">${SHAPES.map(s => `<button class="gbtn" data-s="${s}" title="${s}">${glyph(s, hex(a.color), 9)}</button>`).join("")}</span>
      <span class="pick" id="colors">${Object.keys(COLORS).map(k => `<button class="cbtn" data-c="${k}" title="${k}">${glyph("circle", COLORS[k], 10)}</button>`).join("")}</span>
      <select class="sel" id="deptsel">${Object.keys(DEPTS).map(k => `<option value="${k}">${esc(DEPTS[k].label)}</option>`).join("")}</select>
      <span class="sp"></span>
      <label class="switch ${state.verbose ? "on" : ""}" id="verbose"><span class="k"></span>verbose</label></div>
    <div class="msgs ${state.verbose ? "" : "quiet"}" id="msgs"></div>
    <div class="compose"><textarea id="box" rows="1" placeholder="Tell ${esc(a.name)} what to do. Enter to send, Shift+Enter for a new line."></textarea><button class="primary" id="send">Send</button></div>`;
  paintHead(a);
  ch.querySelectorAll("#shapes .gbtn").forEach(b => b.onclick = () => applyMeta(a, { icon: b.dataset.s }));
  ch.querySelectorAll("#colors .cbtn").forEach(b => b.onclick = () => applyMeta(a, { color: b.dataset.c }));
  $("#deptsel").onchange = e => applyMeta(a, { department: e.target.value });
  $("#verbose").onclick = () => { state.verbose = !state.verbose; ls("verbose", state.verbose ? "1" : "0");
    $("#verbose").classList.toggle("on", state.verbose); $("#msgs").classList.toggle("quiet", !state.verbose); $("#msgs").scrollTop = $("#msgs").scrollHeight; };
  const msgs = $("#msgs");
  // history: last 5 sessions, oldest first
  const hist = a.sessions.slice(0, 5).reverse();
  for (const f of hist) {
    const ev = await api(`/biz/${state.biz}/session/${a.name}/${f}`);
    addDay(msgs, f.replace(".jsonl", "").replace(/-(\d{2})(\d{2})(\d{2})$/, " $1:$2 UTC"));
    ev.forEach(e => addEvent(msgs, e));
  }
  const running = state.liveNow.find(s => s.agent === a.name && !s.done);
  if (running) attach(running.id, msgs);
  msgs.scrollTop = msgs.scrollHeight;
  const box = $("#box"), send = $("#send");
  const go = async () => {
    const m = box.value.trim(); if (!m) return;
    send.disabled = true; box.value = "";
    const r = await post(`/biz/${state.biz}/chat`, { agent: a.name, message: m });
    if (r.error) { toast(r.error); send.disabled = false; return; }
    addDay(msgs, "now"); addEvent(msgs, { kind: "start", user: m }); attach(r.session, msgs);
  };
  send.onclick = go;
  box.onkeydown = e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); } };
  box.oninput = () => { box.style.height = "auto"; box.style.height = Math.min(160, box.scrollHeight) + "px"; };
}
function paintList() {
  const al = $("#agentlist"); if (!al) return;
  al.innerHTML = state.agents.map(a => {
    const running = state.liveNow.some(s => s.agent === a.name && !s.done), d = dept(a.department);
    return `<div class="item agent ${a.name === state.agent ? "on" : ""}" data-a="${esc(a.name)}">
      <div class="nm">${agentGlyph(a, 10)}<span>${esc(a.name)}</span></div>
      <div class="st">${glyph(d.icon, d.color, 8)}<span>${esc(d.label)}</span><span>·</span><span class="${running ? "live" : ""}">${running ? "working" : a.sessions.length + " session" + (a.sessions.length === 1 ? "" : "s")}</span></div></div>`;
  }).join("") || `<div class="empty" style="padding:0 24px">No agents. Copy agents/_template.</div>`;
  al.querySelectorAll(".agent").forEach(d => d.onclick = () => { state.agent = d.dataset.a; renderAgents($("#content")); });
}
function paintHead(a) {
  const ch = $("#chat"); if (!ch) return;
  $("#aglyph").innerHTML = agentGlyph(a, 14);
  ch.querySelectorAll("#shapes .gbtn").forEach(b => { b.classList.toggle("on", b.dataset.s === a.icon); b.innerHTML = glyph(b.dataset.s, hex(a.color), 9); });
  ch.querySelectorAll("#colors .cbtn").forEach(b => b.classList.toggle("on", b.dataset.c === a.color));
  $("#deptsel").value = DEPTS[a.department] ? a.department : "general";
}
async function applyMeta(a, patch) {
  let r; try { r = await post(`/biz/${state.biz}/agent/${a.name}/meta`, patch); } catch (e) { r = { error: "could not reach the server" }; }
  if (!r || r.error) return toast((r && r.error) || "could not save");
  a.icon = r.icon; a.color = r.color; a.department = r.department;
  const b = state.bizs.find(x => x.slug === state.biz);
  if (b) (b.agent_meta = b.agent_meta || {})[a.name] = { icon: a.icon, color: a.color, department: a.department };
  paintHead(a); paintList(); paintBiz();
}
function addDay(msgs, t) { const d = document.createElement("div"); d.className = "m day"; d.textContent = t; msgs.appendChild(d); }
function addEvent(msgs, e) {
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
  else if (e.kind === "end") { d.className = "m sys"; d.textContent = `ended: ${e.summary || ""} · ${e.turns || 0} turns`; }
  else if (e.kind === "learnings") { d.className = "m sys"; d.textContent = e.count ? `${e.count} learning${e.count === 1 ? "" : "s"} noted` : "nothing new learned"; }
  else if (e.kind === "error") { d.className = "m tool err fatal"; d.textContent = e.error || "error"; }
  else return;
  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
}
function attach(sid, msgs) {
  if (state.live) state.live.close();
  const es = new EventSource(`/api/session/${sid}/events`); state.live = es;
  const w = document.createElement("div"); w.className = "m sys working"; w.textContent = "working"; msgs.appendChild(w); msgs.scrollTop = msgs.scrollHeight;
  const stop = () => { es.close(); state.live = null; w.remove(); const s = $("#send"); if (s) s.disabled = false; };
  es.onmessage = ev => { const e = JSON.parse(ev.data); if (e.kind !== "start") addEvent(msgs, e); msgs.appendChild(w); if (e.kind === "tool_result") refreshSideSoon(); };
  es.addEventListener("done", () => { stop(); refreshSide(); });
  es.onerror = stop;
}
