// Persistent chat for any registered agent (global or business). It is a projection of the agent's native session
// conversation (qorqut-code docs/v1.2/NATIVE_CHAT_DTO.md): opening, switching or reading never starts, resumes, stops or types
// into an agent; Terminal owns the session lifecycle. One mount owns one SSE stream, its timers and listeners, and releases all
// of them in dispose().
// Composer state is bound to the exact agent and conversation, and outlives a mount so a switch never leaks or loses it.
const GLOBAL_CHAT_DRAFTS = new Map();           // scope -> unsent composer text
const GLOBAL_CHAT_FILES = new Map();            // scope -> selected browser Files; copied only when Send is pressed
const GLOBAL_CHAT_SENDS = new Map();            // scope -> {text, key, pin}: the frozen identity of an unconfirmed send, reused by a retry
const GLOBAL_CHAT_INFLIGHT = new Map();         // scope -> {text, promise} of a send still waiting for its response
const GLOBAL_CHAT_RESENDS = new Map();          // undelivered input id -> {key, pin}: identity of an explicit resend
const GLOBAL_CHAT_HIDDEN = new Set();           // settled inputs the owner dismissed or already resent
// scope -> {records, consumed}: owner messages accepted by the backend whose transcript entry has not been seen yet. Browser-only and
// presentation-only: a record keeps the sent text visible until its exact native user entry arrives, and never touches receipts.
const GLOBAL_CHAT_ACCEPTED = new Map();
const globalChatText = text => String(text ?? '').replace(/\r\n/g, '\n');
function globalChatAccept(scope, text, input, position) {
  if (!scope || !input?.id) return;
  const held = GLOBAL_CHAT_ACCEPTED.get(scope) || {records: [], consumed: new Set()};
  GLOBAL_CHAT_ACCEPTED.set(scope, held);
  const record = held.records.find(item => item.id === input.id) || {id: input.id, entry: null};
  if (!held.records.includes(record)) held.records.push(record);
  Object.assign(record, {text, position, state: input.state, sequence: input.sequence ?? Number.MAX_SAFE_INTEGER, createdAt: Date.parse(input.created_at)});
}
// Exact provider + conversation identity of a captured conversation; the one key for drafts, sends and history queries.
const globalChatKey = target => target?.conversation_id ? `${target.provider || ''}\n${target.conversation_id}` : null;
const globalChatScope = (agentId, target) => globalChatKey(target) ? `${agentId}\n${globalChatKey(target)}` : null;
const globalChatQuery = target => target ? `&conversation=${encodeURIComponent(target.conversation_id)}&provider=${encodeURIComponent(target.provider || '')}` : '';
function globalChatReadable(agentId) {
  const root = document.querySelector(`[data-global-chat="${CSS.escape(agentId)}"]`);
  if (document.hidden || !root?.isConnected || root.closest('[inert],[hidden],[aria-hidden="true"]')) return false;
  if ([...document.querySelectorAll('[aria-modal="true"]')].some(el => el.getClientRects().length && !el.contains(root))) return false;
  if (!root.getClientRects().length || getComputedStyle(root).visibility === 'hidden') return false;
  const scroll = root.querySelector('.gchat-scroll');
  return !!scroll && scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 48;
}
const GLOBAL_CHAT_PAGE = 50;
const GLOBAL_CHAT_MAX = 17000;
// Refusals store no input; any other failure leaves the outcome unknown and keeps the send identity.
const GLOBAL_CHAT_REFUSALS = new Set(['terminal_offline', 'capture_disabled', 'capture_not_ready', 'binding_not_ready', 'operation_unavailable',
  'unsupported_runtime', 'terminal_changed', 'validation_error', 'not_found', 'forbidden', 'idempotency_conflict']);
const GLOBAL_CHAT_REFUSAL_TEXT = {
  terminal_offline: 'Not running. Start or resume it in Terminal, then send again.',
  capture_disabled: 'Chat unavailable.',
  capture_not_ready: 'Connecting. Try again in a moment.',
  binding_not_ready: 'Connecting. Try again in a moment.',
  terminal_changed: 'Not delivered. Check the agent and try again.',
};
const GLOBAL_CHAT_INPUT_LABEL = {inbox_nudge: 'Inbox nudge', bootstrap_recovery: 'Session recovery', telegram: 'Telegram', broadcast: 'Broadcast', direct_message: 'Direct message'};
const GLOBAL_CHAT_INPUT_STATE = {
  waiting: 'Waiting',
  submitting: 'Sending…',
  uncertain: 'Delivery unconfirmed',
  submitted: 'Sent',
  failed: 'Not delivered',
  undelivered: 'Not delivered',
};
// The expanded text of an inbox nudge or session recovery is system-generated and technical: the timeline shows only its compact marker.
const globalChatMarkerOnly = type => type === 'inbox_nudge' || type === 'bootstrap_recovery';
const globalChatNoticeLabel = system => !system ? 'System notice'
  : system.verified ? GLOBAL_CHAT_INPUT_LABEL[system.input_type] || 'System notice'
  : 'Unverified notice';
const globalChatBase = id => `/root/agents/${encodeURIComponent(id)}/chat`;
// ---- the mount
function globalChatViewToggle(terminal = false) {
  return `<div class="gchat-view-toggle" role="group" aria-label="Agent view">
    <button type="button" class="quiet" data-chat-return aria-label="Chat" aria-pressed="${!terminal}" ${terminal ? '' : 'disabled'}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></button>
    <button type="button" class="quiet" data-terminal aria-label="Terminal" aria-pressed="${terminal}" ${terminal ? 'disabled' : ''}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4 17 6-6-6-6M12 19h8"/></svg></button>
  </div><button type="button" class="quiet gchat-refresh" data-agent-refresh aria-label="Refresh" title="Refresh"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 7v5h-5"/><path d="M20 12a8 8 0 1 0-2.3 5.7M20 12l-3-4"/></svg></button>`;
}

// One Chat/Terminal lifecycle for any registered agent, on the Global page and in a business workspace.
// `agent.agent_id` is the registered id the chat API needs; `terminal.key`/`terminal.base` name the exact existing terminal route.
// Only one view is live at a time: mounting another, toggling, or dispose() releases the old stream and terminal.
// opts: {terminal: {key, base?}, isCurrent(): bool, onTerminalStatus(status), view: 'chat'|'terminal'}
let globalAgentViewActive = null;
function agentViewDispose() { globalAgentViewActive?.dispose(); }
function mountAgentView(pane, agent, opts) {
  agentViewDispose();
  let chat = null, closeTerminal = null, watcher = null, disposed = false;
  const stop = () => {
    clearInterval(watcher); watcher = null;
    chat?.dispose(); chat = null;
    if (closeTerminal) { closeTerminal(); closeTerminal = null; }
  };
  const showChat = () => {
    if (disposed) return;
    stop();
    // The shared main terminal may belong to a shell mounted outside this view (workspace Terminal, a legacy agent without an id):
    // release its viewer (stream and resize observer only; the server process keeps running).
    if (typeof teardownTerm === 'function') teardownTerm();
    chat = mountGlobalChat(pane, agent, {openTerminal: showTerminal});
    // Navigation tears the pane down without an event: release the stream once it has left the page.
    const mine = chat;
    watcher = setInterval(() => { if (!pane.isConnected || chat !== mine) { clearInterval(watcher); if (chat === mine) stop(); } }, 2000);
  };
  const showTerminal = () => {
    if (disposed) return;
    stop();
    const label = agent.display_name || agent.name;
    pane.innerHTML = `<div class="gchat-diagnostic"><div class="gchat-top"><span class="gchat-name">${esc(label)}</span><span class="gchat-status">Terminal</span></div><div class="gchat-session-switch">${globalChatViewToggle(true)}</div><div class="gchat-diagnostic-host"></div></div>`;
    pane.querySelector('[data-chat-return]').onclick = () => { if (!opts.isCurrent || opts.isCurrent()) showChat(); };
    pane.querySelector('[data-agent-refresh]').onclick = () => { if (!opts.isCurrent || opts.isCurrent()) showTerminal(); };
    const shellStatus = pane.querySelector('.gchat-status');
    closeTerminal = mountTerminal(opts.terminal.key, pane.querySelector('.gchat-diagnostic-host'), status => {
      shellStatus.textContent = status === 'live' ? 'Terminal connected' : status === 'ended' ? 'Terminal exited' : 'Terminal disconnected';
      opts.onTerminalStatus?.(status);
    }, opts.terminal.base);
  };
  const view = {showChat, showTerminal, dispose() {
    if (disposed) return;
    disposed = true; stop();
    if (globalAgentViewActive === view) globalAgentViewActive = null;
  }};
  globalAgentViewActive = view;
  (opts.view === 'terminal' ? showTerminal : showChat)();
  return view;
}



function mountGlobalChat(pane, agent, options = {}) {
  const agentId = agent.agent_id, base = globalChatBase(agentId), label = agent.display_name || agent.name || 'Agent';
  const entries = new Map();                     // entry id -> entry
  let session = null, conversation = null, inputs = [], older = null, shown, clears = 0, shownInputs = [];
  let cursor = 0, lastRead = 0, composerScope = null, hasLocal = false;
  let stream = null, retryTimer = null, retryDelay = 1000, readTimer = null, streamDown = false;
  let disposed = false, loading = false, again = false, generation = 0, finished = false, reading = 0;
  let bannerText = '', reconnecting = false, olderBusy = false, view = null;
  const cleanups = [];

  // Native shell: header, view switch and the session line stay here; every message, composer and preview is the shared view.
  pane.innerHTML = `<div class="gchat" data-global-chat="${esc(agentId)}">
    <header class="gchat-top gchat-identity">${identityAvatar('agent', label, {...agent, ...agent.presentation})}</header>
    ${options.openTerminal ? `<div class="gchat-session-switch">${globalChatViewToggle()}</div>` : ''}
  </div>`;
  const root = pane.firstElementChild;
  const $q = selector => root.querySelector(selector);
  const sessionLine = document.createElement('div');
  sessionLine.className = 'gchat-session'; sessionLine.setAttribute('role', 'status'); sessionLine.setAttribute('aria-live', 'polite'); sessionLine.dataset.state = 'loading';
  sessionLine.innerHTML = `<span class="gchat-working-tag" role="img" aria-label="${esc(label)} is working" hidden><span aria-hidden="true">${identityAvatar('agent', label, {...agent, ...agent.presentation})}</span><span class="gchat-working-dots" aria-hidden="true"><i></i><i></i><i></i></span></span><span class="gchat-session-dot" aria-hidden="true"></span><span class="gchat-session-text">Loading…</span>`;
  const sessionText = sessionLine.querySelector('.gchat-session-text');
  const workingTag = sessionLine.querySelector('.gchat-working-tag');

  const live = () => !disposed && root.isConnected;
  const listen = (target, type, handler, opts) => { target.addEventListener(type, handler, opts); cleanups.push(() => target.removeEventListener(type, handler, opts)); };

  // ---- the read position: the newest completed reply whose entry is rendered in this visible Chat. Only a rendered page or
  // entry counts; received events, prefetching and agent selection never do, and newest_sequence is never acknowledged.
  function renderedBoundary() {
    let newest = 0;
    for (const key of view.getVisibleKeys()) {
      const entry = entries.get(key);
      if (entry?.speaker === 'agent' && entry.completion_sequence > newest) newest = entry.completion_sequence;
    }
    return newest;
  }
  function scheduleRead() {
    clearTimeout(readTimer);
    readTimer = setTimeout(async () => {
      if (!live() || !session || !globalChatReadable(agentId) || !view.isAtBottom()) return;
      const sequence = renderedBoundary();
      if (sequence <= lastRead || sequence <= reading) return;
      reading = sequence;
      const epoch = generation;
      try {
        const result = coreData(await post(`${base}/read`, {sequence}));
        if (epoch !== generation) return;
        lastRead = Math.max(lastRead, result.last_read_sequence || 0);
        if (typeof replyNotifications !== 'undefined') void replyNotifications.refresh();
      } catch (_) { /* the next render or scroll tries again */ }
      finally { if (epoch === generation) reading = 0; }
    }, 400);
  }
  listen(document, 'visibilitychange', () => { if (!document.hidden) scheduleRead(); });
  const visibilityObserver = new MutationObserver(scheduleRead);
  visibilityObserver.observe(document.body, {attributes:true, childList:true, subtree:true, attributeFilter:['inert','hidden','aria-hidden']});
  cleanups.push(() => visibilityObserver.disconnect());

  // ---- normalization: the transcript entries and the application inputs not yet confirmed by the transcript
  // The list is the transcript; entries are keyed by their stable id and ordered by archive position.
  const presentEntry = entry => ({
    key: entry.id, speaker: entry.speaker, text: entry.text, timestamp: entry.timestamp, partial: entry.partial === true, position: entry.position,
    collapsedSource: ((entry.speaker === 'user' && /^(?:\[telegram\](?:\s|$)|Qorqut owner (?:message|reply) via Telegram\b)/i.test((entry.text || '').trimStart()))
      || (entry.system?.verified && entry.system.input_type === 'telegram')) ? 'telegram' : null,
    ...(entry.speaker === 'notice' ? {notice: {label: globalChatNoticeLabel(entry.system), verified: !!entry.system?.verified, inputType: entry.system?.input_type || null,
      markerOnly: !!entry.system?.verified && globalChatMarkerOnly(entry.system.input_type)}} : {}),
  });
  // A successful outgoing agent message: recipients come from the backend DTO, glyphs from the existing trusted renderer.
  const agentMessageRecipient = recipient => {
    const name = String(recipient?.display_name || 'Agent');
    return {id: String(recipient?.agent_id || ''), name, glyphHtml: agentGlyph({...(recipient?.presentation || {}), role: recipient?.role}, 25)};
  };
  const isAgentMessage = entry => entry.speaker === 'notice' && Array.isArray(entry.agent_message?.recipients);
  // Adjacent outgoing-message entries share one row: first entry's key and position, distinct recipients in first-seen order.
  // Recomputed from the per-conversation entry map on every render, so replay and older pages never double-count.
  function groupAgentMessages(sorted) {
    const rows = [];
    for (const entry of sorted) {
      const last = rows[rows.length - 1];
      if (!isAgentMessage(entry)) { rows.push(entry); continue; }
      if (last?.group) { last.group.push(entry); continue; }
      rows.push({group: [entry]});
    }
    return rows.map(row => {
      if (!row.group) return presentEntry(row);
      const first = row.group[0], seen = new Set(), recipients = [];
      for (const entry of row.group) for (const raw of entry.agent_message.recipients) {
        const recipient = agentMessageRecipient(raw), key = recipient.id || `name:${recipient.name}`;
        if (!seen.has(key)) { seen.add(key); recipients.push(recipient); }
      }
      return {...presentEntry(first), text: '', notice: {label: 'Messaged', verified: true, inputType: null, markerOnly: true, agentMessage: {recipients}}};
    });
  }
  const orderedEntries = () => groupAgentMessages([...entries.values()].sort((a, b) => a.position - b.position));
  const newestPosition = () => { let top = -1; for (const entry of entries.values()) top = Math.max(top, entry.position); return top; };
  const viewedKey = () => globalChatKey(conversation);
  // The live session's exact binding; a conversation is sendable only when it is the one displayed.
  const liveBinding = () => session?.binding || null;
  const viewingLive = () => !!liveBinding() && globalChatKey(liveBinding()) === viewedKey();
  // A receipt belongs to the exact provider/conversation of its own binding; one without a binding is shown nowhere.
  function inputVisible(item, noticeIds) {
    // Inbox delivery is backend bookkeeping, not an owner message needing a
    // resend/dismiss decision. Keep its audit record there; only a verified
    // transcript entry gets the small Inbox nudge marker in the conversation.
    if (item.origin === 'system' && item.input_type === 'inbox_nudge') return false;
    if (item.state === 'submitted' || GLOBAL_CHAT_HIDDEN.has(item.id) || noticeIds.has(item.id)) return false;
    return !!viewedKey() && globalChatKey(item.binding) === viewedKey();
  }
  // Only a definitely undelivered owner Chat message can be offered again; every other producer's receipt is status only.
  const ownerChat = item => item.origin === 'owner' && item.input_type === 'chat';
  function presentInputs() {
    // A verified notice entry already is its input's receipt: never show the same input a second time.
    const noticeIds = new Set([...entries.values()].map(entry => entry.system?.input_id).filter(Boolean));
    shownInputs = inputs.filter(item => inputVisible(item, noticeIds)).sort((a, b) => a.sequence - b.sequence);
    const local = acceptedBubbles(new Set(shownInputs.map(item => item.id)));
    hasLocal = local.length > 0;
    const server = presentReceipts();
    return local.length ? [...server.map((model, i) => ({sequence: shownInputs[i].sequence, model})), ...local].sort((a, b) => a.sequence - b.sequence).map(row => row.model) : server;
  }
  // Accepted messages awaiting their transcript echo. Each record claims, in send order, the first unclaimed native user entry that
  // is newer than the transcript seen when it was sent, has the exact text and is not older than the receipt. Display only.
  function acceptedBubbles(visibleIds) {
    const held = GLOBAL_CHAT_ACCEPTED.get(globalChatScope(agentId, conversation));
    if (!held) return [];
    const users = [...entries.values()].filter(entry => entry.speaker === 'user').sort((a, b) => a.position - b.position);
    for (const record of held.records) {
      const item = inputs.find(row => row.id === record.id);
      if (item) record.state = item.state;
      const echo = users.find(entry => {
        const at = Date.parse(entry.timestamp);
        return !held.consumed.has(entry.id) && entry.position > record.position && globalChatText(entry.text) === globalChatText(record.text)
          && !(Number.isFinite(at) && Number.isFinite(record.createdAt) && at < record.createdAt);
      });
      if (echo) { held.consumed.add(echo.id); record.entry = echo.id; }
    }
    held.records = held.records.filter(record => !record.entry);
    return held.records.filter(record => !GLOBAL_CHAT_HIDDEN.has(record.id) && !visibleIds.has(record.id)).map(record => ({sequence: record.sequence, model: {
      key: record.id, speaker: 'user', state: record.state, stateLabel: GLOBAL_CHAT_INPUT_STATE[record.state], inputType: 'chat', resendable: false,
      collapsedSource: null, text: record.text, actions: [],
    }}));
  }
  function presentReceipts() {
    return shownInputs.map(item => {
      const system = item.origin === 'system', kind = GLOBAL_CHAT_INPUT_LABEL[item.input_type] || (system ? 'System notice' : '');
      const definite = ownerChat(item) && ['failed', 'undelivered'].includes(item.state) && !!item.body;
      const resendable = definite && viewingLive();
      const dismissable = ['uncertain', 'failed', 'undelivered'].includes(item.state);
      return {
        key: item.id, speaker: system ? 'notice' : 'user', state: item.state, stateLabel: GLOBAL_CHAT_INPUT_STATE[item.state], inputType: item.input_type, resendable, collapsedSource: item.input_type === 'telegram' ? 'telegram' : null,
        text: globalChatMarkerOnly(item.input_type) ? '' : item.body ?? (item.input_type === 'telegram' ? 'Private message' : ''),
        ...(kind ? {notice: {label: kind}} : {}),
        actions: [
          ...(resendable ? [{key: 'resend', label: 'Resend', disabled: !session?.can_send || GLOBAL_CHAT_RESENDS.get(item.id)?.pending === true}] : []),
          ...(dismissable ? [{key: 'dismiss', label: 'Dismiss'}] : []),
        ],
      };
    });
  }

  // ---- session status, composer enablement and the empty state
  const canSend = () => !!session?.can_send && viewingLive() && !finished;
  const selectedFiles = () => GLOBAL_CHAT_FILES.get(composerScope) || [];
  function sessionSummary() {
    if (!session) return {key: 'loading', line: ''};
    if (session.state === 'live' && session.can_send && !viewingLive()) return {key: 'starting', line: ''};
    if (session.state === 'live') {
      if (session.reason === 'capture_disabled') return {key: 'disabled', line: ''};
      if (session.reason === 'binding_not_ready') return {key: 'starting', line: ''};
      if (session.reason === 'capture_not_ready') return {key: 'starting', line: ''};
      return {key: session.working ? 'working' : 'live', line: ''};
    }
    const direction = `Start or resume ${label} in Terminal to send.`;
    if (session.reason === 'capture_disabled') return {key: 'disabled', line: ''};
    if (session.reason === 'unsupported_runtime') return {key: 'offline', line: ''};
    return {key: 'offline', line: direction};
  }
  function emptyState() {
    if (entries.size || shownInputs.length || hasLocal || !session) return {text: '', action: null};
    const key = sessionSummary().key;
    if (key === 'live' || key === 'working') return {text: 'Your agent conversation appears here.', action: null};
    if (key === 'starting') return {text: 'Connecting…', action: null};
    if (key === 'disabled') return {text: 'Chat unavailable.', action: null};
    return {text: `Start or resume ${label} in Terminal. Its conversation will appear here.`, action: options.openTerminal ? {key: 'terminal', label: 'Open terminal'} : null};
  }
  // One repaint: the native status line, then every presentation field the view displays. `patch` adds fields such as the entries.
  function paint(patch = {}, scroll = 'preserve') {
    if (disposed) return;
    const summary = sessionSummary(), working = summary.key === 'working' && !streamDown && !finished;
    sessionLine.dataset.state = streamDown && summary.key === 'working' ? 'live' : summary.key;
    sessionLine.classList.toggle('gchat-session-working', working);
    sessionText.textContent = finished ? 'This chat is no longer available.' : summary.line;
    workingTag.hidden = !working;
    sessionLine.hidden = !working && !sessionText.textContent;
    const enabled = canSend(), sending = GLOBAL_CHAT_INFLIGHT.has(composerScope);
    const pending = presentInputs();
    view.render({
      ...patch, pending,
      composer: {inputDisabled: !enabled, sendDisabled: !enabled || sending, filesDisabled: !enabled || sending,
        attachments: selectedFiles().map(file => ({key: file.key, name: file.file.name})),
        placeholder: enabled || !session ? `Message ${label}…` : 'Not available to send'},
      older: {visible: older != null, disabled: olderBusy},
      empty: emptyState(),
    }, {scroll});
  }
  const paintEntries = scroll => paint({entries: orderedEntries()}, scroll);
  const paintInputs = () => paint();
  const paintSession = () => paint();
  const showBanner = text => { bannerText = text || ''; reconnecting = false; view.render({banner: {text: bannerText}}); };
  listen(root, 'click', event => {
    if (event.target.closest('[data-agent-refresh]')) {
      clearTimeout(retryTimer); retryDelay = 1000; closeStream(); void load(); return;
    }
    if (event.target.closest('[data-terminal]')) options.openTerminal?.();
  });

  // Drops the displayed page and its session so nothing stale can be read, sent or acknowledged until a fresh successful state
  // arrives. The unsent draft stays in its conversation-specific store.
  function invalidatePage() {
    saveDraft(); composerScope = null; view.setDraft('');
    reading = 0; clearTimeout(readTimer);
    session = null; conversation = null; inputs = []; older = null; shown = undefined;
    clears++; entries.clear();
    paint({entries: []});
  }

  // ---- composer: the draft and the unconfirmed send are bound to this exact agent, provider and conversation
  function bindComposer() {
    const next = globalChatScope(agentId, conversation);
    if (next === composerScope) return;
    composerScope = next;
    view.setDraft(next ? GLOBAL_CHAT_DRAFTS.get(next) || '' : '');
    if (next) watchInflight(next);
  }
  // A send started from an earlier mount of this composer finishes on its own; this mount only shows its outcome.
  function watchInflight(key) {
    const earlier = GLOBAL_CHAT_INFLIGHT.get(key);
    earlier?.promise.finally(() => {
      if (!live()) return;
      if (composerScope === key && view.getDraft() === earlier.text && GLOBAL_CHAT_DRAFTS.get(key) !== earlier.text) view.setDraft('');
      paintSession(); void load();
    });
  }
  const saveDraft = () => { if (!composerScope) return; const text = view.getDraft(); if (text) GLOBAL_CHAT_DRAFTS.set(composerScope, text); else GLOBAL_CHAT_DRAFTS.delete(composerScope); };
  const refusalText = problem => GLOBAL_CHAT_REFUSAL_TEXT[problem.code] || "Couldn't send. Try again";
  // One admission request. The result is recorded in module state so a mount that was left meanwhile loses nothing.
  const admit = async (text, key, pin) => coreData(await post(`${base}/messages`, {text, idempotency_key: key, expected_binding: pin}));
  function takeInput(item) {
    if (!item || !live()) return;
    inputs = [item, ...inputs.filter(old => old.id !== item.id)];
    const followed = view.isAtBottom();
    paintInputs();
    if (followed) view.scrollToBottom();
  }
  async function submit(text) {
    const mine = composerScope;
    const files = [...selectedFiles()], fileKey = files.map(file => file.key).join(',');
    if (!mine || !text.trim() && !files.length || !canSend() || GLOBAL_CHAT_INFLIGHT.has(mine)) return;
    // A retry of the same text reuses its key and its original binding pin, so a lost response can never become a second
    // message or move to another session. Only a deliberate new send takes the binding observed now.
    let attempt = GLOBAL_CHAT_SENDS.get(mine);
    if (!attempt || attempt.text !== text || attempt.fileKey !== fileKey) attempt = {text, fileKey, files, key: newRequestId(), pin: liveBinding().binding_id, position: newestPosition()};
    GLOBAL_CHAT_SENDS.set(mine, attempt);
    const run = (async () => {
      try {
        const references = [];
        for (const item of attempt.files) {
          if (!item.saved || item.savedPin !== attempt.pin) {
            try {
              item.saved = coreData(await api(`${base}/attachments`, {method: 'POST',
                headers: {'Content-Type': 'application/octet-stream', 'X-File-Name': encodeURIComponent(item.file.name),
                  'Idempotency-Key': `${item.key}:${attempt.pin}`, 'X-Chat-Binding': attempt.pin}, body: item.file}));
              item.savedPin = attempt.pin;
            } catch (error) { error.attachment = true; throw error; }
          }
          references.push(item.saved.reference);
        }
        const payload = references.length ? [text, references.join('\n')].filter(part => part.trim()).join('\n\n') : text;
        if (payload.length > GLOBAL_CHAT_MAX) {
          const error = new Error('Message too long'); error.attachment = true; error.code = 'message_too_long'; throw error;
        }
        const result = await admit(payload, attempt.key, attempt.pin);
        globalChatAccept(mine, payload, result.input, attempt.position);
        if (GLOBAL_CHAT_SENDS.get(mine) === attempt) GLOBAL_CHAT_SENDS.delete(mine);
        if (GLOBAL_CHAT_DRAFTS.get(mine) === text) GLOBAL_CHAT_DRAFTS.delete(mine);
        const sent = new Set(attempt.files.map(file => file.key));
        const remaining = (GLOBAL_CHAT_FILES.get(mine) || []).filter(file => !sent.has(file.key));
        if (remaining.length) GLOBAL_CHAT_FILES.set(mine, remaining); else GLOBAL_CHAT_FILES.delete(mine);
        if (live() && composerScope === mine) { if (view.getDraft() === text) view.setDraft(''); showBanner(''); }
        takeInput(result.input);
        if (live()) { view.scrollToBottom(); void load(); }
      } catch (problem) {
        if (GLOBAL_CHAT_REFUSALS.has(problem.code)) { if (GLOBAL_CHAT_SENDS.get(mine) === attempt) GLOBAL_CHAT_SENDS.delete(mine); }
        if (live() && composerScope === mine) showBanner(problem.code === 'message_too_long' ? 'Shorten your message and try again.'
          : problem.attachment ? "Couldn't attach the file. Try again."
          : GLOBAL_CHAT_REFUSALS.has(problem.code) ? refusalText(problem) : "Couldn't confirm. Try again");
        if (problem.code === 'capture_not_ready' || problem.code === 'terminal_offline' || problem.code === 'terminal_changed') void load();
      } finally {
        GLOBAL_CHAT_INFLIGHT.delete(mine);
        if (live()) paintSession();
      }
    })();
    GLOBAL_CHAT_INFLIGHT.set(mine, {text, promise: run});
    paintSession();
    await run;
  }
  // A definitely undelivered input is resent only when the owner asks; the new send has its own key, stable across a retry.
  async function resendInput(id) {
    const item = inputs.find(row => row.id === id);
    if (!item?.body || !ownerChat(item) || !['failed', 'undelivered'].includes(item.state) || !canSend() || !viewingLive()) return;
    const resend = GLOBAL_CHAT_RESENDS.get(id) || {key: newRequestId(), pin: liveBinding().binding_id, position: newestPosition()};
    resend.pending = true; GLOBAL_CHAT_RESENDS.set(id, resend);
    const scope = composerScope;
    paintSession();
    try {
      const result = await admit(item.body, resend.key, resend.pin);
      GLOBAL_CHAT_RESENDS.delete(id); GLOBAL_CHAT_HIDDEN.add(id);
      globalChatAccept(scope, item.body, result.input, resend.position);
      showBanner(''); takeInput(result.input); void load();
    } catch (problem) {
      resend.pending = false;
      if (GLOBAL_CHAT_REFUSALS.has(problem.code)) GLOBAL_CHAT_RESENDS.delete(id);
      if (live()) showBanner(GLOBAL_CHAT_REFUSALS.has(problem.code) ? refusalText(problem) : "Couldn't confirm. Try again");
    } finally { if (live()) paintSession(); }
  }

  view = mountChatView(root, {
    logLabel: `Conversation with ${label}`, composerLabel: `Message ${label}`, placeholder: `Message ${label}…`, maxLength: GLOBAL_CHAT_MAX,
    olderLabel: 'Load older messages', footer: sessionLine,
    onDraft: () => {
      saveDraft();
      const pending = GLOBAL_CHAT_SENDS.get(composerScope);
      if (pending && pending.text !== view.getDraft() && !GLOBAL_CHAT_INFLIGHT.has(composerScope)) GLOBAL_CHAT_SENDS.delete(composerScope);
    },
    onSubmit: text => submit(text),
    onFiles: files => {
      if (!composerScope || !canSend() || GLOBAL_CHAT_INFLIGHT.has(composerScope) || !files.length) return;
      if (files.length + selectedFiles().length > 10 || files.some(file => file.size > 25 * 1024 * 1024)) {
        showBanner('Choose up to 10 files, each 25 MB or smaller.'); return;
      }
      GLOBAL_CHAT_FILES.set(composerScope, [...selectedFiles(), ...files.map(file => ({key: newRequestId(), file}))]);
      GLOBAL_CHAT_SENDS.delete(composerScope); showBanner(''); paintSession();
    },
    onAttachmentRemove: key => {
      if (!composerScope || GLOBAL_CHAT_INFLIGHT.has(composerScope)) return;
      GLOBAL_CHAT_FILES.set(composerScope, selectedFiles().filter(file => file.key !== key));
      GLOBAL_CHAT_SENDS.delete(composerScope); paintSession();
    },
    onOlder: () => loadOlder(),
    onViewportChange: scheduleRead,
    onPendingAction: (action, id) => {
      if (action === 'resend') void resendInput(id);
      else if (action === 'dismiss') { GLOBAL_CHAT_HIDDEN.add(id); paintInputs(); }
    },
    onEmptyAction: action => { if (action === 'terminal') options.openTerminal?.(); },
  });
  paint();

  // ---- loading: a state fetch is idempotent, so every "changed" event just reads it again (coalesced)
  // Terminal owns conversation selection; Chat always follows the backend's current conversation.
  const stateUrl = () => `${base}?limit=${GLOBAL_CHAT_PAGE}`;
  async function load(reset = false) {
    if (loading) { again = true; return; }
    loading = true;
    const mine = generation;
    try {
      const data = coreData(await api(stateUrl()));
      if (!live() || mine !== generation) return;
      const id = globalChatKey(data.conversation);
      const page = data.entries || [];
      // A page that starts after everything held (a long gap while away) cannot be joined to it: start from that page.
      const gap = data.older != null && page.length && page[0].position > newestPosition() && entries.size > 0;
      const fresh = reset || shown === undefined || id !== shown || gap;
      if (fresh) { clears++; entries.clear(); older = data.older; }
      shown = id; session = data.session; conversation = data.conversation;
      inputs = data.inputs || []; lastRead = Math.max(fresh && reset ? 0 : lastRead, data.last_read_sequence || 0);
      cursor = fresh && reset ? data.cursor || 0 : Math.max(cursor, data.cursor || 0);
      page.forEach(entry => entries.set(entry.id, entry));
      bindComposer();
      if (fresh) showBanner('');
      paintEntries(fresh ? 'bottom' : 'auto');
      if (!stream) connect();
    } catch (problem) {
      if (live() && mine === generation) { paintSession(); showBanner("Couldn't load. Retry"); retry(() => load()); }
    } finally {
      if (mine === generation) { loading = false; if (again && live()) { again = false; void load(); } }
    }
  }
  async function loadOlder() {
    // Only history responses advance this cursor, so no page is skipped.
    const before = older, mine = generation, requested = viewedKey(), epoch = clears, target = conversation;
    if (before == null || !target || olderBusy) return;
    olderBusy = true; paint();
    try {
      const data = coreData(await api(`${base}/history?before=${before}&limit=${GLOBAL_CHAT_PAGE}${globalChatQuery(target)}`));
      // Applied only to the exact provider/conversation it asked for, even if the live conversation changed on its own.
      if (!live() || mine !== generation || epoch !== clears || viewedKey() !== requested) return;
      (data.entries || []).forEach(entry => { if (!entries.has(entry.id)) entries.set(entry.id, entry); });
      older = data.older ?? null;
      paintEntries('prepend');
    } catch (problem) { if (live()) showBanner("Couldn't load. Retry"); }
    finally { olderBusy = false; if (live()) paint(); }
  }

  // ---- the event stream: replay from the durable cursor, reconnect with backoff, resynchronise on reset
  function closeStream() { if (stream) { stream.close(); stream = null; } }
  function retry(action) {
    clearTimeout(retryTimer);
    if (!live()) return;
    retryTimer = setTimeout(() => { if (live()) void action(); }, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 15000);
  }
  function connect() {
    closeStream();
    if (!live()) return;
    const source = new EventSource(evUrl(`${base}/events?cursor=${cursor}`));
    stream = source;
    source.addEventListener('open', () => {
      retryDelay = 1000;
      if (streamDown) { streamDown = false; paintSession(); }
      if (bannerText && reconnecting) { showBanner(''); void load(); }
    });
    source.addEventListener('chat', event => {
      let item;
      try { item = JSON.parse(event.data); } catch (_) { return; }
      if (!item || item.cursor <= cursor) return;                              // replay overlap is ignored by cursor
      cursor = item.cursor;
      if (item.kind === 'changed') void load();                                // an event renders nothing; the fetched page does
    });
    source.addEventListener('reset', () => {
      // A restored installation: outstanding state/history/read callbacks are obsolete and must not reinstate the old cursor or boundary.
      closeStream(); generation++; loading = false; again = false;
      cursor = 0; lastRead = 0; invalidatePage(); void load(true);
    });
    source.addEventListener('done', () => { closeStream(); finished = true; streamDown = false; showBanner('This chat is no longer available.'); paintSession(); });
    source.onerror = () => {
      if (!live()) return;
      streamDown = true; paintSession();
      showBanner('Reconnecting…'); reconnecting = true;
      // The browser retries a dropped stream itself with Last-Event-ID; a closed one needs a fresh connection.
      if (source.readyState === EventSource.CLOSED) { stream = null; retry(connect); }
    };
  }

  void load();
  return {
    agentId,
    dispose() {
      disposed = true; generation++;
      closeStream(); clearTimeout(retryTimer); clearTimeout(readTimer);
      saveDraft();
      cleanups.splice(0).forEach(cleanup => cleanup());
      view.dispose();
      if (pane.firstElementChild === root) pane.innerHTML = '';
    },
    focus() { view.focusComposer(); },
  };
}
