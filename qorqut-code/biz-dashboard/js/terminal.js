// Terminal mirror: one live xterm bound to an agent's background PTY on the server.
// Output = base64 SSE frames -> xterm; keystrokes -> POST input (coalesced per flight). The PTY lives on the
// server and persists (survives tab switches / reconnects); reconnecting replays scrollback.
// The mirror fills the pane and re-fits on resize. Viewer controls live in the shared
// Chat/Terminal header; all agent execution control stays in the shell.
// The PTY is a plain shell in agents/<name>/: the owner types `claude` to start the agent. The shell
// itself lives in a tmux session on the server and outlives the dashboard; "ended" means the
// session is gone (Stop, or the shell exited), not that the mirror dropped.
//
// Chat follows the conversation in this terminal; the terminal always shows its actual shell/client.

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
  // Mark it inactive before dispose synchronously fires render/resize cleanup.
  // Queued fit callbacks must not inspect a renderer while it is being torn down.
  const term = _term; _term = null;
  if (term) term.dispose();
}

// Mount a live terminal for `agent` into `pane` (an element). `onStat(t)` gets
// "live" / "ended" / "disconnected" so a caller (e.g. the agent list) can reflect it.
// The main view uses the shared session; compact viewers pass an independent session.
// Closing a viewer leaves the server PTY running.
function mountTerminal(agent, pane, onStat, terminalBase = `/biz/${encodeURIComponent(state.biz)}/terminal/${encodeURIComponent(agent)}`, session = null) {
  session ||= {
    get term(){return _term;},set term(value){_term=value;},
    get es(){return _termES;},set es(value){_termES=value;},
    get ro(){return _termRO;},set ro(value){_termRO=value;}
  };
  const dispose=()=>{
    session.ro?.disconnect();session.ro=null;
    session.es?.close();session.es=null;
    const old=session.term;session.term=null;old?.dispose();
  };
  dispose();
  pane.innerHTML = '<div class="termpane"><div class="termhost" id="termhost"></div></div>';
  const host = $("#termhost", pane);

  const term = new Terminal({
    fontFamily: "var(--mono, ui-monospace, monospace)",
    fontSize: 14,
    cursorBlink: true,
    scrollback: 8000,
    theme: { background: "#0b0e14", foreground: "#c8d3e0", cursor: "#7dd3fc" },
  });
  term.open(host);
  session.term = term;

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
    if (session.term !== term || !host.isConnected || !host.clientWidth || !host.clientHeight) return; // hidden viewers must not resize the shared PTY
    const { w, h } = cell();
    // `.termhost` includes its own padding in clientWidth/clientHeight. Measure its
    // content box, including the native xterm scrollbar gutter, so the PTY never
    // gets more columns or rows than the visible terminal can render.
    const style = typeof getComputedStyle === "function"
      ? getComputedStyle(host)
      : { paddingLeft: 0, paddingRight: 0, paddingTop: 0, paddingBottom: 0 };
    const viewport = host.querySelector(".xterm-viewport");
    const px = value => Number.parseFloat(value) || 0;
    const gutter = viewport ? Math.max(0, viewport.offsetWidth - viewport.clientWidth) : 0;
    const width = Math.max(0, host.clientWidth - px(style.paddingLeft) - px(style.paddingRight) - gutter);
    const height = Math.max(0, host.clientHeight - px(style.paddingTop) - px(style.paddingBottom));
    const cols = Math.max(20, Math.floor(width / w));
    const rows = Math.max(6, Math.floor(height / h));
    if (cols !== term.cols || rows !== term.rows) {
      const atBottom = term.buffer.active.viewportY >= term.buffer.active.baseY;
      term.resize(cols, rows);
      post(`${terminalBase}/resize`, { cols, rows });
      if (atBottom) term.scrollToBottom();
    }
  };
  requestAnimationFrame(fit); // let the font metrics settle, then fit to real cell size
  let rt;
  session.ro = new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(fit, 100); });
  session.ro.observe(host);

  const setStat = t => {
    if (session.term !== term) return;
    if (onStat) { try { onStat(t); } catch (e) {} }
  };

  // output: base64 SSE frames -> xterm. On a real drop, close (no auto-retry, which would
  // replay the whole scrollback and duplicate it) and report the disconnected state.
  let nextReplay = false, replaying = false, outputQueue = Promise.resolve(), streamed = false;
  const clearScreen = () => { outputQueue = outputQueue.then(() => { if (session.term === term) term.reset(); }); };
  const openStream = () => {
    session.es?.close(); session.es = null;
    if (streamed) clearScreen();
    streamed = true;
    const es = new EventSource(evUrl(`${terminalBase}/stream`));
    session.es = es;
    // The first layout pass can precede server-side preparation; size again once attached.
    es.onopen = () => { if (session.term === term && host.clientWidth && host.clientHeight) post(`${terminalBase}/resize`, { cols: term.cols, rows: term.rows }); };
    es.addEventListener('replay', () => { nextReplay = true; });
    es.onmessage = ev => {
      const replay = nextReplay; nextReplay = false;
      outputQueue = outputQueue.then(() => new Promise(resolve => {
        if (session.term !== term) return resolve();
        replaying = replay;
        try { term.write(b64ToBytes(ev.data), () => { replaying = false; resolve(); }); }
        catch (_) { replaying = false; resolve(); }
      }));
      setStat("live");
    };
    es.addEventListener("done", () => setStat("ended"));
    es.onerror = () => {
      es.close(); if (session.es === es) session.es = null;
      setStat("disconnected");
    };
  };

  openStream();

  // Coalesce keystrokes while a request is in flight; never replay a failed write.
  const inputQueue = [];   // oldest first: {data, waiters}
  let inputFlushing = false, inputFailed = false;
  const dropChunks = match => {
    for (let i = inputQueue.length - 1; i >= 0; i--) {
      const chunk = inputQueue[i];
      if (!match(chunk)) continue;
      inputQueue.splice(i, 1);
      chunk.data = '';
      chunk.waiters.splice(0, chunk.waiters.length).forEach(resolve => resolve(false));
    }
  };
  const flushInput = async () => {
    if (inputFlushing) return;
    inputFlushing = true;
    try {
      while (!inputFailed && session.term === term && inputQueue.length) {
        const chunk = inputQueue[0];
        // Stay under the server's per-request input bound; a rare oversized paste drains in order across requests.
        const data = chunk.data.slice(0, 57344);
        chunk.data = chunk.data.slice(data.length);
        const waiters = chunk.data ? [] : chunk.waiters.splice(0, chunk.waiters.length);
        let delivered = !data;
        if (data) {
          try {
            const result = await post(`${terminalBase}/input`, { data });
            delivered = !result.error;
          } catch (_) { delivered = false; }
        }
        if (!delivered) { inputFailed = true; setStat('disconnected'); }
        waiters.forEach(resolve => resolve(delivered));
        if (!chunk.data && !chunk.waiters.length) { const at = inputQueue.indexOf(chunk); if (at >= 0) inputQueue.splice(at, 1); }
      }
    } finally {
      inputFlushing = false;
      if (inputFailed || session.term !== term) dropChunks(() => true);
    }
  };
  const sendInput = d => {
    if (inputFailed || session.term !== term) return Promise.resolve(false);
    let chunk = inputQueue[inputQueue.length - 1];
    if (!chunk) { chunk = { data: '', waiters: [] }; inputQueue.push(chunk); }
    chunk.data += d;
    const delivered = new Promise(resolve => chunk.waiters.push(resolve));
    flushInput();
    return delivered;
  };
  term.onData(d => {
    // Replaying old terminal queries must not type their generated replies into
    // the current shell. Keep ordinary keystrokes and live protocol replies.
    if (replaying && /^\x1b\[(?:\?|>)?[0-9;]*[cnR]$/.test(d)) return;
    sendInput(d);
  });

  return () => { dropChunks(() => true); dispose(); };
}
