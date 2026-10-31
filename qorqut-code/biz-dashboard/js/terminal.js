// Terminal tab: pick an agent, open its live terminal (xterm) mirrored from the server.
// Output arrives as base64 SSE frames -> xterm; keystrokes -> POST input. One active
// terminal at a time (the PTY lives on the server and survives tab switches/reconnects).

const CELL_W = 7.8, CELL_H = 17; // rough xterm cell metrics at 13px mono; only used to size the PTY

function b64ToBytes(b64) {
  const bin = atob(b64);
  const a = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
  return a;
}

let _termES = null, _term = null;

function teardownTerm() {
  if (_termES) { _termES.close(); _termES = null; }
  if (_term) { _term.dispose(); _term = null; }
}

function renderTerminals(content) {
  teardownTerm();
  content.innerHTML = `<div class="split">
    <div class="listcol" id="termlist"><div class="empty">loading…</div></div>
    <div class="chat" id="termpane"><div class="empty">Pick an agent to open its terminal.</div></div>
  </div>`;
  const listEl = $("#termlist", content);
  api(`/biz/${state.biz}/agents`).then(agents => {
    state.agents = Array.isArray(agents) ? agents : [];
    if (!state.agents.length) { listEl.innerHTML = `<div class="empty">No agents in this business.</div>`; return; }
    listEl.innerHTML = state.agents.map(a =>
      `<div class="item" data-agent="${esc(a.name)}">${agentGlyph(a, 16)}<span>${esc(a.name)}</span></div>`).join("");
    listEl.querySelectorAll(".item").forEach(el => el.onclick = () => openTerm(el.dataset.agent, listEl));
  });
}

function openTerm(agent, listEl) {
  if (listEl) listEl.querySelectorAll(".item").forEach(x => x.classList.toggle("on", x.dataset.agent === agent));
  teardownTerm();

  const pane = $("#termpane");
  pane.innerHTML = `<div class="top"><b>${esc(agent)}</b>
    <span class="tstat" id="tstat">connecting…</span>
    <span style="flex:1"></span>
    <button class="tstop" id="tstop" title="Kill and restart this terminal">restart</button></div>
    <div class="termhost" id="termhost"></div>`;
  const host = $("#termhost");

  const term = new Terminal({
    fontFamily: "var(--mono, ui-monospace, monospace)",
    fontSize: 13,
    cursorBlink: true,
    scrollback: 5000,
    theme: { background: "#0b0e14", foreground: "#c8d3e0", cursor: "#7dd3fc" },
  });
  term.open(host);
  _term = term;

  const cols = Math.max(20, Math.floor(host.clientWidth / CELL_W));
  const rows = Math.max(6, Math.floor(host.clientHeight / CELL_H));
  term.resize(cols, rows);

  const setStat = t => { const s = $("#tstat"); if (s) s.textContent = t; };

  // output: base64 SSE frames -> xterm
  const es = new EventSource(evUrl(`/biz/${state.biz}/terminal/${agent}/stream`));
  _termES = es;
  es.onmessage = ev => { try { term.write(b64ToBytes(ev.data)); } catch (e) {} setStat("live"); };
  es.addEventListener("done", () => setStat("ended"));
  es.onerror = () => setStat("disconnected");

  // input: keystrokes -> POST (the PTY echoes back over the stream)
  term.onData(d => { post(`/biz/${state.biz}/terminal/${agent}/input`, { data: d }); });

  // tell the PTY our window size
  post(`/biz/${state.biz}/terminal/${agent}/resize`, { cols, rows });
  term.focus();

  const stop = $("#tstop");
  if (stop) stop.onclick = async () => {
    await post(`/biz/${state.biz}/terminal/${agent}/stop`, {});
    openTerm(agent, listEl); // reopening auto-starts a fresh shell
  };
}
