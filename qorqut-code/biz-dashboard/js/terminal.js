// Terminal mirror: one live xterm bound to an agent's background PTY on the server.
// Output = base64 SSE frames -> xterm; keystrokes -> POST input (coalesced per flight). The PTY lives on the
// server and persists (survives tab switches / reconnects); reconnecting replays scrollback.
// The mirror fills the pane and re-fits on resize. Viewer controls live in the shared
// Chat/Terminal header; all agent execution control stays in the shell.
// The PTY is a plain shell in agents/<name>/: the owner types `claude` to start the agent. The shell
// itself lives in a tmux session on the server and outlives the dashboard; "ended" means the
// session is gone (Stop, or the shell exited), not that the mirror dropped.
//
// A registered agent's terminal can be mounted as a view of the agent's ONE selected native conversation
// (options.attachment, see mountTerminal). Then it shows the server's attachment state (attached / reconnecting / disconnected
// / mismatch / unsupported), streams and types only while the terminal is verified to be that conversation, and offers the
// owner's explicit actions: Reconnect (exact resume of the selected conversation), Use this conversation (select another
// running one) and a separate Diagnostic terminal for starting a new conversation with the client's own controls. Plain shells
// (workspace utility terminals) pass no attachment option and behave exactly as before.

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
// `options.attachment` = {agentId, mode: 'selected' | 'diagnostic', onSelected(), onDiagnostic()} makes this a selected-conversation
// view; `options.onAttachment(view)` hears every attachment state. Without it nothing below the PTY mirror changes.
const TERMINAL_ATTACH_REFUSALS = new Set(['terminal_not_attached', 'terminal_changed']);
const TERMINAL_CLIENT = {claude: 'Claude Code', codex: 'Codex'};
const TERMINAL_ATTACH_LABEL = {attached: 'Terminal attached', reconnecting: 'Reconnecting terminal', disconnected: 'Terminal not attached', mismatch: 'Terminal mismatch', unsupported: 'Terminal'};
const terminalAttachmentLabel = (view, diagnostic = false) => diagnostic ? 'Diagnostic terminal' : view?.reason === 'viewer_unverified' ? 'Viewer unverified' : (view && view.mode === 'selected' && TERMINAL_ATTACH_LABEL[view.state]) || 'Terminal';
function mountTerminal(agent, pane, onStat, terminalBase = `/biz/${encodeURIComponent(state.biz)}/terminal/${encodeURIComponent(agent)}`, session = null, options = {}) {
  const attach = options.attachment?.agentId ? options.attachment : null;
  const diagnostic = !!attach && attach.mode === 'diagnostic';
  let disposed = false, pollTimer = null, noticeTimer = null;
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
  pane.innerHTML = `<div class="termpane">${attach ? '<div class="termattach" role="status" aria-live="polite" data-state="loading"><span class="termattach-text">Checking the terminal…</span><span class="termattach-actions"></span></div>' : ''}
    <div class="termhost" id="termhost"></div>
  </div>`;
  const host = $("#termhost", pane);
  const banner = attach ? pane.querySelector('.termattach') : null;

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
    if (session.term !== term || !host.isConnected) return; // a queued fit can outlive a tab/agent switch
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
  session.ro = new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(fit, 100); });
  session.ro.observe(host);

  const setStat = t => {
    if (session.term !== term) return;
    if (onStat) { try { onStat(t); } catch (e) {} }
  };

  // output: base64 SSE frames -> xterm. On a real drop, close (no auto-retry, which would
  // replay the whole scrollback and duplicate it) and report the disconnected state.
  // What a registered agent's terminal streams is decided by the attachment state it has read (see `apply`): the selected
  // conversation only while it is positively attached (the stream names the attachment it claims), the plain shell only when
  // nothing is selected yet, and raw output otherwise only in the explicit diagnostic mode. The server checks the claim before
  // streaming and while streaming, and ends the stream with `detached` when it stops being true. A new stream means a cleared
  // screen before the replay. The GET itself never launches anything.
  let nextReplay = false, replaying = false, outputQueue = Promise.resolve(), streamClaim, streamed = false;
  const clearScreen = () => { outputQueue = outputQueue.then(() => { if (session.term === term) term.reset(); }); };
  const closeStream = () => {
    session.es?.close(); session.es = null;
    if (streamed) clearScreen();
    streamed = false; streamClaim = null;
  };
  const openStream = want => {
    session.es?.close(); session.es = null;
    if (streamed) clearScreen();
    streamed = true; streamClaim = want;
    const query = want === 'diagnostic' ? '?mode=diagnostic' : want === 'open' ? '' : `?attachment=${encodeURIComponent(want)}`;
    const es = new EventSource(evUrl(`${terminalBase}/stream${query}`));
    session.es = es;
    // The first layout pass can precede server-side preparation; size again once attached.
    es.onopen = () => { if (session.term === term) post(`${terminalBase}/resize`, { cols: term.cols, rows: term.rows }); };
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
    es.addEventListener('detached', () => {
      es.close(); if (session.es === es) session.es = null;
      streamClaim = undefined;
      flash('The terminal no longer shows the selected conversation.');
      refresh();
    });
    es.onerror = () => {
      es.close(); if (session.es === es) session.es = null;
      setStat("disconnected");
      if (attach) refresh();
    };
  };

  // ---- the selected-conversation attachment (registered agent terminals only)
  let view = null, notice = '', busy = false;
  const autoTried = new Set(), providerName = provider => TERMINAL_CLIENT[provider] || 'A native client';
  const flash = text => {
    if (!banner || notice === text) return;
    notice = text; renderBanner();
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { notice = ''; renderBanner(); }, 8000);
  };
  const bannerButton = (label, handler) => {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'quiet'; button.textContent = label; button.disabled = busy; button.onclick = handler;
    return button;
  };
  function renderBanner() {
    if (!banner || session.term !== term || disposed) return;
    const text = banner.querySelector('.termattach-text'), actions = banner.querySelector('.termattach-actions');
    actions.replaceChildren();
    let message, status = view?.state || 'loading';
    if (diagnostic) {
      status = 'diagnostic';
      message = view?.candidate
        ? `Diagnostic terminal. A ${providerName(view.candidate.provider)} conversation is running here and is not selected yet.`
        : "Diagnostic terminal. No new conversation is selected yet: start one with the client's own controls, then choose it here.";
      if (view?.candidate) actions.append(bannerButton('Use this conversation', selectCandidate));
      if (attach.onSelected) actions.append(bannerButton('Selected conversation', attach.onSelected));
    } else if (!view) {
      message = 'Checking the terminal…';
    } else {
      message = view.mode === 'first_launch' && !view.candidate ? `${view.detail}. Start it in this terminal; the conversation it opens can then be selected.` : view.detail;
      if (view.action === 'reconnect') actions.append(bannerButton('Reconnect', reconnectNow));
      if (view.action === 'select_candidate' && view.candidate) actions.append(bannerButton('Use this conversation', selectCandidate));
      // Whatever keeps the selected conversation from being attached, the explicit diagnostic terminal stays reachable.
      if (view.mode === 'selected' && view.state !== 'attached' && view.state !== 'reconnecting' && attach.onDiagnostic) actions.append(bannerButton('Diagnostic terminal', attach.onDiagnostic));
    }
    banner.dataset.state = status;
    text.textContent = notice ? `${message} ${notice}` : message;
  }
  // The state the server reports now. Everything below decides from it: what to stream, whether keystrokes may be sent, and
  // whether the one automatic reconnect of this open is due. A stream is replaced only when what it claims changes, and a
  // selected view that is not positively attached shows state and actions only: it never falls back to unclaimed output.
  function apply(next) {
    if (session.term !== term || disposed) return;
    view = next;
    options.onAttachment?.(next, diagnostic);
    const want = diagnostic ? 'diagnostic' : next.mode !== 'selected' ? 'open' : next.state === 'attached' ? next.token : null;
    if (want === null) closeStream();
    else if (streamClaim !== want) openStream(want);
    pruneInput();
    renderBanner();
    clearTimeout(pollTimer);
    const wait = next.state === 'reconnecting' ? 2000 : next.state === 'attached' ? 30000 : next.state === 'unsupported' ? 15000 : 5000;
    pollTimer = setTimeout(refresh, wait);
    // Opening the normal Terminal reconnects the selected conversation once per selection revision, only when the server says
    // its stopped client or missing viewer can be opened safely; every other reconnect is the owner's explicit button.
    if (!diagnostic && next.mode === 'selected' && next.action === 'reconnect' && next.auto_reconnect === true
        && next.selection && !autoTried.has(next.selection.revision)) {
      autoTried.add(next.selection.revision);
      reconnectNow();
    }
  }
  let refreshing = 0;
  async function refresh() {
    if (!attach || session.term !== term || disposed) return;
    const seq = ++refreshing;
    let result = null;
    try { result = await api(`${terminalBase}/attachment`); } catch (_) { /* reported below */ }
    if (session.term !== term || disposed || seq !== refreshing) return;
    if (result?.attachment) return apply(result.attachment);
    flash(result?.error || "Couldn't check the terminal.");
    if ([401, 403, 404].includes(result?.httpStatus)) return;
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refresh, 5000);
  }
  async function reconnectNow() {
    if (busy || !view?.selection) return;
    busy = true; renderBanner();
    try {
      const result = await post(`${terminalBase}/attach`, { revision: view.selection.revision, cols: term.cols, rows: term.rows });
      if (session.term !== term || disposed) return;
      if (result.attachment) apply(result.attachment);
      else { flash(result.error || "Couldn't reconnect."); refresh(); }
    } catch (_) {
      flash("Couldn't reach the server to reconnect.");
    } finally { busy = false; renderBanner(); }
  }
  async function selectCandidate() {
    const candidate = view?.candidate;
    if (busy || !candidate) return;
    busy = true; renderBanner();
    try {
      coreData(await post(`${globalChatBase(attach.agentId)}/select`, { provider: candidate.provider, conversation_id: candidate.conversation_id, expected_generation: view.selection?.generation ?? 0 }));
      if (diagnostic && attach.onSelected) attach.onSelected();
      else refresh();
    } catch (error) {
      flash(error.code === 'revision_conflict' ? 'The selected conversation changed. Check it and try again.' : (error.message || "Couldn't select this conversation."));
      refresh();
    } finally { busy = false; renderBanner(); }
  }
  if (attach) refresh(); else openStream('open');

  // input: keystrokes -> POST (the PTY echoes back over the stream). Bytes arriving while a POST is in flight coalesce into the
  // next request, so fast typing costs one round-trip per flight, not one per keystroke. A caller's promise resolves true only
  // once everything up to and including its bytes was delivered.
  // A selected-conversation view pins every queued chunk to the exact mode + selection revision + attachment token it was typed
  // under, and the request carries that pin, never the current one. When the identity changes (a switch, a re-attach, a
  // refusal) the unsent chunks of the old identity are discarded with a visible explanation: they are never relabelled with a
  // new token and bytes already in flight are never replayed. Within one pin, order and the per-request bound are kept.
  const inputPin = () => {
    if (!attach) return { key: 'plain', mode: null, token: null };
    if (diagnostic) return { key: 'diagnostic', mode: 'diagnostic', token: null };
    if (view?.mode !== 'selected') return { key: `open|${view?.mode ?? ''}`, mode: 'selected', token: null };
    const token = view.state === 'attached' && view.token ? view.token : null;
    return { key: `selected|${view.selection?.revision ?? ''}|${token ?? ''}`, mode: 'selected', token };
  };
  const inputBody = (data, pin) => !attach ? { data } : { data, mode: pin.mode, ...(pin.token ? { attachment: pin.token } : {}) };
  const inputBlocked = () => {
    if (!attach || diagnostic) return '';
    if (!view) return 'Checking the terminal. Input was not sent.';
    return view.mode === 'selected' && view.state !== 'attached' ? `Input not sent. ${view.detail}` : '';
  };
  const inputQueue = [];   // oldest first: {pin, data, waiters}
  let inputFlushing = false, inputFailed = false;
  const dropChunks = (match, why) => {
    let dropped = false;
    for (let i = inputQueue.length - 1; i >= 0; i--) {
      const chunk = inputQueue[i];
      if (!match(chunk)) continue;
      inputQueue.splice(i, 1);
      chunk.data = '';
      chunk.waiters.splice(0, chunk.waiters.length).forEach(resolve => resolve(false));
      dropped = true;
    }
    if (dropped && why) flash(why);
  };
  // Unsent input typed under an identity that is no longer the current one.
  function pruneInput() {
    if (!attach || !inputQueue.length) return;
    const now = inputPin().key;
    dropChunks(chunk => chunk.pin.key !== now, 'Input not sent. The terminal changed.');
  }
  const flushInput = async () => {
    if (inputFlushing) return;
    inputFlushing = true;
    try {
      while (!inputFailed && session.term === term && inputQueue.length) {
        const chunk = inputQueue[0];
        if (chunk.pin.key !== inputPin().key) { dropChunks(item => item === chunk, 'Input not sent. The terminal changed.'); continue; }
        // Stay under the server's per-request input bound; a rare oversized paste drains in order across requests.
        const data = chunk.data.slice(0, 57344);
        chunk.data = chunk.data.slice(data.length);
        const waiters = chunk.data ? [] : chunk.waiters.splice(0, chunk.waiters.length);
        let delivered = !data, refused = '';
        if (data) {
          try {
            const result = await post(`${terminalBase}/input`, inputBody(data, chunk.pin));
            delivered = !result.error;
            if (!delivered && attach && TERMINAL_ATTACH_REFUSALS.has(result.coreError?.code)) refused = result.error;
          } catch (_) { delivered = false; }
        }
        if (!delivered && refused) {
          dropChunks(item => item.pin.key === chunk.pin.key, `Input not sent. ${refused}`);
          refresh();
        } else if (!delivered) { inputFailed = true; setStat('disconnected'); }
        waiters.forEach(resolve => resolve(delivered));
        if (!chunk.data && !chunk.waiters.length) { const at = inputQueue.indexOf(chunk); if (at >= 0) inputQueue.splice(at, 1); }
      }
    } finally {
      inputFlushing = false;
      if (inputFailed || session.term !== term) dropChunks(() => true, '');
    }
  };
  const sendInput = d => {
    if (inputFailed || session.term !== term) return Promise.resolve(false);
    const blocked = inputBlocked();
    if (blocked) { flash(blocked); return Promise.resolve(false); }
    const pin = inputPin();
    let chunk = inputQueue[inputQueue.length - 1];
    if (!chunk || chunk.pin.key !== pin.key) { chunk = { pin, data: '', waiters: [] }; inputQueue.push(chunk); }
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

  return () => { disposed = true; clearTimeout(pollTimer); clearTimeout(noticeTimer); dropChunks(() => true, ''); dispose(); };
}
