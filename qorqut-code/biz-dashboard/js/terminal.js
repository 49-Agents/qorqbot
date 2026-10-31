// Terminal mirror: one live xterm bound to an agent's background PTY on the server.
// Output = base64 SSE frames -> xterm; keystrokes -> POST input. The PTY lives on the
// server and persists (survives tab switches / reconnects); reconnecting replays scrollback.
// No chrome: the terminal fills the pane. A single floating button reconnects a broken
// stream (which also respawns the shell if it exited). All other control is done in the shell.

const CELL_W = 7.8, CELL_H = 17; // rough xterm cell metrics at 13px mono; used to size the PTY

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

// Mount a live terminal for `agent` into `pane` (an element). `onStat(t)` gets
// "live" / "ended" / "disconnected" so a caller (e.g. the agent list) can reflect it.
// One terminal is active in the page at a time; the PTY on the server keeps running.
function mountTerminal(agent, pane, onStat) {
  teardownTerm();
  pane.innerHTML = `<div class="termpane">
    <div class="termhost" id="termhost"></div>
    <button class="tref" id="tref" title="Reconnect the stream">⟳</button>
  </div>`;
  const host = $("#termhost", pane), btn = $("#tref", pane);

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

  const setStat = t => {
    if (btn) btn.classList.toggle("warn", t === "disconnected" || t === "ended");
    if (onStat) { try { onStat(t); } catch (e) {} }
  };

  // output: base64 SSE frames -> xterm. On a real drop, close (no auto-retry: a retry would
  // replay the whole scrollback and duplicate it) and flag the button so the user can reconnect.
  const es = new EventSource(evUrl(`/biz/${state.biz}/terminal/${agent}/stream`));
  _termES = es;
  es.onmessage = ev => { try { term.write(b64ToBytes(ev.data)); } catch (e) {} setStat("live"); };
  es.addEventListener("done", () => setStat("ended"));
  es.onerror = () => { es.close(); if (_termES === es) _termES = null; setStat("disconnected"); };

  // input: keystrokes -> POST (the PTY echoes back over the stream)
  term.onData(d => { post(`/biz/${state.biz}/terminal/${agent}/input`, { data: d }); });

  // tell the PTY our window size
  post(`/biz/${state.biz}/terminal/${agent}/resize`, { cols, rows });
  term.focus();

  // reconnect: re-open the stream. The PTY persists, so this replays scrollback; if the shell
  // had exited, the server spawns a fresh one. It is NOT a kill/restart.
  if (btn) btn.onclick = () => mountTerminal(agent, pane, onStat);
}

// Terminal tab: pick an agent, open its terminal in the right pane.
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
  mountTerminal(agent, $("#termpane"));
}
