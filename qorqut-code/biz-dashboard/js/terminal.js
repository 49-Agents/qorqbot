// Terminal mirror: one live xterm bound to an agent's background PTY on the server.
// Output = base64 SSE frames -> xterm; keystrokes -> POST input. The PTY lives on the
// server and persists (survives tab switches / reconnects); reconnecting replays scrollback.
// The mirror fills the pane and re-fits on resize. Floating controls reconnect a broken
// stream and toggle the agent overview; all agent execution control stays in the shell.
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
// Local drafts are scoped to the terminal endpoint, never persisted to storage.
const terminalDrafts = new Map();

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
// One terminal is active in the page at a time; the PTY on the server keeps running.
function mountTerminal(agent, pane, onStat, terminalBase = `/biz/${encodeURIComponent(state.biz)}/terminal/${encodeURIComponent(agent)}`) {
  const paneToggle = pane.querySelector("#agpad-toggle");
  teardownTerm();
  pane.innerHTML = `<div class="termpane">
    <div class="termhost" id="termhost"></div>
    <form class="termcomposer" hidden>
      <input type="text" aria-label="Local terminal input" maxlength="16000" autocomplete="off" spellcheck="false">
      <button type="submit">Send</button>
      <span class="termcomposer-status" role="status"></span>
    </form>
    <div class="termtools"><button type="button" class="ttab" id="termcompose-toggle" aria-label="Toggle local composer" aria-expanded="false" aria-controls="termcomposer"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7 9h.01M11 9h.01M15 9h.01M7 12h.01M11 12h.01M15 12h.01M8 15h8"/></svg></button><button class="ttab tref" id="tref" title="Reconnect the stream" aria-label="Reconnect the stream"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 7v5h-5"/><path d="M20 12a8 8 0 1 0-2.3 5.7M20 12l-3-4"/></svg></button></div>
  </div>`;
  const host = $("#termhost", pane), btn = $("#tref", pane);
  const composer = $(".termcomposer", pane), composeToggle = $("#termcompose-toggle", pane);
  composer.id = "termcomposer";
  const input = $("input", composer), send = $("button", composer), status = $(".termcomposer-status", composer);
  const draft = terminalDrafts.get(terminalBase) || { open: false, text: '' };
  terminalDrafts.set(terminalBase, draft);
  input.value = draft.text;
  input.oninput = () => { draft.text = input.value; };
  if (pane.closest(".agsplit") && pane.dataset.agentPane !== "off") agpadTerminalButton($(".termtools", pane), paneToggle);

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
      term.resize(cols, rows);
      post(`${terminalBase}/resize`, { cols, rows });
    }
    term.scrollToBottom();
  };
  requestAnimationFrame(fit); // let the font metrics settle, then fit to real cell size
  let rt;
  _termRO = new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(fit, 100); });
  _termRO.observe(host);

  const setStat = t => {
    if (_term !== term) return;
    if (btn) btn.classList.toggle("warn", t === "disconnected" || t === "ended");
    if (onStat) { try { onStat(t); } catch (e) {} }
  };

  // output: base64 SSE frames -> xterm. On a real drop, close (no auto-retry, which would
  // replay the whole scrollback and duplicate it) and flag the button so the user can reconnect.
  const es = new EventSource(evUrl(`${terminalBase}/stream`));
  _termES = es;
  // The first layout pass can precede server-side preparation; size again once attached.
  es.onopen = () => { if (_term === term) post(`${terminalBase}/resize`, { cols: term.cols, rows: term.rows }); };
  let nextReplay = false, replaying = false, outputQueue = Promise.resolve();
  es.addEventListener('replay', () => { nextReplay = true; });
  es.onmessage = ev => {
    const replay = nextReplay; nextReplay = false;
    outputQueue = outputQueue.then(() => new Promise(resolve => {
      if (_term !== term) return resolve();
      replaying = replay;
      try { term.write(b64ToBytes(ev.data), () => { replaying = false; resolve(); }); }
      catch (_) { replaying = false; resolve(); }
    }));
    setStat("live");
  };
  es.addEventListener("done", () => setStat("ended"));
  es.onerror = () => { es.close(); if (_termES === es) _termES = null; setStat("disconnected"); };

  // input: keystrokes -> POST (the PTY echoes back over the stream)
  let inputQueue = Promise.resolve(), inputFailed = false;
  const sendInput = d => {
    const pending = inputQueue.then(async () => {
      if (_term !== term || inputFailed) return false;
      const result = await post(`${terminalBase}/input`, { data: d });
      if (result.error) throw new Error('Terminal input failed');
      return true;
    }).catch(() => { inputFailed = true; setStat('disconnected'); return false; });
    inputQueue = pending;
    return pending;
  };
  term.onData(d => {
    // Replaying old terminal queries must not type their generated replies into
    // the current shell. Keep ordinary keystrokes and live protocol replies.
    if (replaying && /^\x1b\[(?:\?|>)?[0-9;]*[cnR]$/.test(d)) return;
    sendInput(d);
  });

  const showComposer = open => {
    draft.open = open;
    composer.hidden = !open;
    composeToggle.setAttribute('aria-expanded', String(open));
    composeToggle.classList.toggle('on', open);
    requestAnimationFrame(fit);
    if (open) input.focus(); else term.focus();
  };
  composeToggle.onclick = () => showComposer(!draft.open);
  input.addEventListener('keydown', event => {
    // IME Enter finishes composition, not the terminal command.
    if (event.key === 'Enter' && (event.isComposing || event.keyCode === 229)) event.preventDefault();
    if (event.key === 'Escape') { event.preventDefault(); showComposer(false); }
  });
  composer.onsubmit = async event => {
    event.preventDefault();
    if (send.disabled || !input.value || _term !== term) return;
    // Single-line local editing. Control characters from pasted text must not
    // become hidden terminal commands. This appends to the current remote prompt.
    const text = input.value;
    if (/[\x00-\x1f\x7f]/.test(text)) {
      status.textContent = 'Remove control characters before sending.';
      return;
    }
    send.disabled = true;
    input.readOnly = true;
    status.textContent = 'Sending…';
    const sent = await sendInput(text + '\r');
    if (sent && draft.text === text) draft.text = '';
    if (_term !== term) return;
    input.readOnly = false;
    input.value = draft.text;
    send.disabled = !sent;
    status.textContent = sent ? '' : 'Delivery unconfirmed. Check the terminal before reconnecting and retrying.';
    if (!composer.hidden) input.focus();
  };
  showComposer(draft.open);

  // reconnect: re-open the stream. The PTY persists, so this replays scrollback; if the shell
  // had exited, the server spawns a fresh one. It is NOT a kill/restart.
  if (btn) btn.onclick = () => mountTerminal(agent, pane, onStat, terminalBase);
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
