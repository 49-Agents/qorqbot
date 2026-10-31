// Terminal mirror: one live xterm bound to an agent's background PTY on the server.
// Output = base64 SSE frames -> xterm; keystrokes -> POST input. The PTY lives on the
// server and persists (survives tab switches / reconnects); reconnecting replays scrollback.
// No chrome: the mirror fills the pane and re-fits on resize. One floating button reconnects
// a broken stream (which also respawns the shell if it exited); all other control is in the shell.
// The PTY is a plain shell in agents/<name>/: the owner types `claude` to start the agent. The shell
// itself lives in a tmux session on the server and outlives the dashboard; "ended" means the
// session is gone (Stop, or the shell exited), not that the mirror dropped.

const CELL_W = 8.4, CELL_H = 18.5; // rough xterm cell metrics at 14px mono; used to size the PTY

function b64ToBytes(b64) {
  const bin = atob(b64);
  const a = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
  return a;
}

let _termES = null, _term = null, _termRO = null;

function teardownTerm() {
  if (_termRO) { _termRO.disconnect(); _termRO = null; }
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
    fontSize: 14,
    cursorBlink: true,
    scrollback: 8000,
    theme: { background: "#0b0e14", foreground: "#c8d3e0", cursor: "#7dd3fc" },
  });
  term.open(host);
  _term = term;

  // Size the PTY to fill the pane exactly. Use xterm's MEASURED cell size (falling back to a
  // rough estimate before the first render) — an estimate that runs large would ask for more
  // rows than fit, pushing the shell's bottom line (its input) below the clip. Floor so the
  // grid never exceeds the visible area, then pin the view to the bottom.
  const cell = () => {
    const d = term._core && term._core._renderService && term._core._renderService.dimensions;
    const c = d && d.css && d.css.cell;
    return { w: (c && c.width) || CELL_W, h: (c && c.height) || CELL_H };
  };
  const fit = () => {
    if (_term !== term || !host.isConnected) return; // a queued fit can outlive a tab/agent switch
    const { w, h } = cell();
    const cols = Math.max(20, Math.floor(host.clientWidth / w));
    const rows = Math.max(6, Math.floor(host.clientHeight / h));
    if (cols !== term.cols || rows !== term.rows) {
      term.resize(cols, rows);
      post(`/biz/${state.biz}/terminal/${agent}/resize`, { cols, rows });
    }
    term.scrollToBottom();
  };
  requestAnimationFrame(fit); // let the font metrics settle, then fit to real cell size
  let rt;
  _termRO = new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(fit, 100); });
  _termRO.observe(host);

  const setStat = t => {
    if (btn) btn.classList.toggle("warn", t === "disconnected" || t === "ended");
    if (onStat) { try { onStat(t); } catch (e) {} }
  };

  // output: base64 SSE frames -> xterm. On a real drop, close (no auto-retry, which would
  // replay the whole scrollback and duplicate it) and flag the button so the user can reconnect.
  const es = new EventSource(evUrl(`/biz/${state.biz}/terminal/${agent}/stream`));
  _termES = es;
  es.onmessage = ev => { try { term.write(b64ToBytes(ev.data)); } catch (e) {} setStat("live"); };
  es.addEventListener("done", () => setStat("ended"));
  es.onerror = () => { es.close(); if (_termES === es) _termES = null; setStat("disconnected"); };

  // input: keystrokes -> POST (the PTY echoes back over the stream)
  term.onData(d => { post(`/biz/${state.biz}/terminal/${agent}/input`, { data: d }); });
  term.focus();

  // reconnect: re-open the stream. The PTY persists, so this replays scrollback; if the shell
  // had exited, the server spawns a fresh one. It is NOT a kill/restart.
  if (btn) btn.onclick = () => mountTerminal(agent, pane, onStat);
}

// Terminal tab (legacy — the Agents tab now mounts terminals directly). Kept so the tab keeps
// working wherever it is still wired. Pick an agent, open its terminal in the right pane.
function renderTerminals(content) {
  teardownTerm();
  content.innerHTML = `<div class="split">
    <div class="listcol" id="termlist"><div class="empty">loading…</div></div>
    <div class="chat termmode" id="termpane"><div class="empty">Pick an agent to open its terminal.</div></div>
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
