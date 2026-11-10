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
// Comparison only: native clients may trim the pasted message's trailing whitespace.
const globalChatText = text => String(text ?? '').replace(/\r\n/g, '\n').trimEnd();
async function globalChatOpenFocusReference(ref, context = {}) {
  const pane = Object.hasOwn(context, 'pane') ? context.pane : (typeof _agpad !== 'undefined' ? _agpad : null);
  const agentId = String(context.agentId ?? pane?.agentId ?? '');
  const current = () => context.isCurrent?.() !== false
    && (typeof _agpad === 'undefined' || _agpad === pane)
    && (!pane || (pane.open && (!agentId || String(pane.agentId ?? '') === agentId)));
  const supported=new Set(['table','reference','signoff','project']);
  if (!current()) return false;
  if(ref?.type==='task' && typeof ref.id==='string' && ref.id) return agentOpenRecordReference(ref);
  if(supported.has(ref?.type) && typeof ref.id==='string' && ref.id && pane) {
    if(pane?.open && pane.workspaceRole==='ordubashy' && pane.agentId && state.scope==='global' && state.globalTab==='agents') {
      if(pane.view!=='focus') {
        await agpadSetView(pane,'focus');
        if (!current() || pane.view !== 'focus') return false;
      }
      if(pane.view==='focus' && pane.surface) { await agpadSurfaceClose(pane); if (!current() || pane.surface) return false; }
      if(pane.view==='focus') {
        const frame=pane.workspaceFrame;
        if(frame?.isConnected && pane.el?.contains(frame)) {
          await frame.workspaceReady;
          if (!current() || pane.workspaceFrame !== frame || !frame.isConnected || !pane.el?.contains(frame)) return false;
          const child=frame.contentWindow, open=child?.agentWorkspacePairedReference;
          if(typeof open==='function' && child.document.querySelector('.agfocus')) {
            const opened = await open(ref);
            return current() && opened === true;
          }
        }
        if(pane.focusMount?.isConnected) {
          const open=window.agentWorkspacePairedReference;
          if(typeof open==='function') {
            const opened = await open(ref);
            return current() && opened === true;
          }
        }
      }
      return false;
    }
  }
  if (!current()) return false;
  return taskProgressOpenReference(ref?.business_slug ?? null,ref,current);
}
function globalChatAccept(scope, text, input, position, command = false) {
  if (!scope || !input?.id) return;
  const held = GLOBAL_CHAT_ACCEPTED.get(scope) || {records: [], consumed: new Set()};
  GLOBAL_CHAT_ACCEPTED.set(scope, held);
  const record = held.records.find(item => item.id === input.id) || {id: input.id, entry: null};
  if (!held.records.includes(record)) held.records.push(record);
  Object.assign(record, {text, position, command, state: input.state, sequence: input.sequence ?? Number.MAX_SAFE_INTEGER, createdAt: Date.parse(input.created_at)});
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
  'unsupported_runtime', 'terminal_changed', 'terminal_required', 'validation_error', 'not_found', 'forbidden', 'idempotency_conflict']);
const GLOBAL_CHAT_REFUSAL_TEXT = {
  terminal_offline: 'Not running. Start or resume it in Terminal, then send again.',
  capture_disabled: 'Chat unavailable.',
  capture_not_ready: 'Connecting. Try again in a moment.',
  binding_not_ready: 'Connecting. Try again in a moment.',
  terminal_changed: 'Not delivered. Check the agent and try again.',
  terminal_required: 'This command runs only in the agent’s Terminal. Open Terminal and run it there.',
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
// Recorded focus belongs to this chat's registered agent, never the independently selected Todo board.
// Read only the existing focus projection; the native session remains authoritative for the working animation.
function globalChatFocusMount(host, agent, {visible, onChange}) {
  if (!['ordubashy','global-worker'].includes(agent.role?.id || agent.role_id)) return null;
  const agentId=agent.agent_id, reads=new AbortController();
  let disposed=false, pending=null, last=0, attention=null, error=false;
  host.innerHTML='<span class="gchat-focus-label"></span><span class="gchat-focus-intent"></span>';
  host.setAttribute('aria-label','Recorded focus and intent');
  host.tabIndex=0;
  const paint=()=>{
    const focus=attention?.focus, focused=attention?.state === 'focused' && focus && !focus.unavailable;
    host.querySelector('.gchat-focus-label').textContent=error || focus?.unavailable ? 'Focus unavailable'
      : focused ? focus.label || attention.task?.label || 'Focused' : 'No focus set';
    const intent=host.querySelector('.gchat-focus-intent');
    intent.textContent=focused ? attention.intent || 'No intent recorded' : '';
    intent.hidden=!focused;
    onChange();
  };
  const refresh=(force=false)=>{
    if (disposed || document.hidden || !visible() || !force && Date.now()-last < 12000) return Promise.resolve();
    if (pending) return pending;
    last=Date.now();
    pending=(async()=>{
      try {
        const data=coreData(await post('/root/operations/todo_get',{scope:'global',agent_id:agentId,projection:'focus'},{signal:reads.signal}));
        if (disposed) return;
        if (data?.agent_id !== agentId || !data.attention) throw Error('Focus unavailable');
        attention=data.attention; error=false;
      } catch (_) {
        if (disposed) return;
        attention=null; error=true;
      } finally {pending=null;}
      if (!disposed) paint();
    })();
    return pending;
  };
  const timer=setInterval(()=>{void refresh();},12000);
  const onVisible=()=>{if (!document.hidden) void refresh();};
  document.addEventListener('visibilitychange',onVisible);
  return {
    refresh,
    show(show) {host.hidden=!show || !attention && !error;return !host.hidden;},
    dispose() {disposed=true;reads.abort();clearInterval(timer);document.removeEventListener('visibilitychange',onVisible);host.replaceChildren();host.hidden=true;},
  };
}
// ---- the mount
function globalChatViewToggle(terminal = false) {
  return `<div class="gchat-view-toggle" role="group" aria-label="Agent view">
    <button type="button" class="ttab" data-chat-return aria-label="Chat" aria-pressed="${!terminal}" ${terminal ? '' : 'disabled'}><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></button>
    <button type="button" class="ttab" data-terminal aria-label="Terminal" aria-pressed="${terminal}" ${terminal ? 'disabled' : ''}><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4 17 6-6-6-6M12 19h8"/></svg></button>
  </div><button type="button" class="ttab gchat-refresh" data-agent-refresh aria-label="Refresh" title="Refresh"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 7v5h-5"/><path d="M20 12a8 8 0 1 0-2.3 5.7M20 12l-3-4"/></svg></button>`;
}

// One Chat/Terminal lifecycle for any registered agent, on the Global page and in a business workspace.
// `agent.agent_id` is the registered id the chat API needs; `terminal.key`/`terminal.base` name the exact existing terminal route.
// Keep the current agent’s two views mounted across toggles; navigation/dispose releases both streams.
// opts: {terminal: {key, base?}, isCurrent(): bool, onTerminalStatus(status), view: 'chat'|'terminal'}
let globalAgentViewActive = null;
function agentViewDispose() { globalAgentViewActive?.dispose(); }
function mountAgentView(pane, agent, opts) {
  agentViewDispose();
  let chat = null, closeTerminal = null, disposed = false;
  const chatHost = document.createElement('div'), terminalHost = document.createElement('div');
  for (const host of [chatHost, terminalHost]) { host.className = 'gchat-view-host'; host.hidden = true; }
  pane.replaceChildren(chatHost, terminalHost);
  // Release a legacy viewer once on entry, not when returning to this agent's warm Chat.
  if (typeof teardownTerm === 'function') teardownTerm();
  const current = () => !disposed && (!opts.isCurrent || opts.isCurrent());
  const showChat = () => {
    if (!current()) return;
    terminalHost.hidden = true; chatHost.hidden = false;
    if (chat) return;
    const originAgentId = String(agent.agent_id ?? '');
    chat = mountGlobalChat(chatHost, agent, {openTerminal: showTerminal, openReference: ref => globalChatOpenFocusReference(ref, {
      agentId: originAgentId,
      isCurrent: () => current() && !chatHost.hidden,
    })});
  };
  // Refresh explicitly replaces the terminal viewer; ordinary toggles retain its renderer and stream.
  const showTerminal = (refresh = false) => {
    if (!current()) return;
    chatHost.hidden = true; terminalHost.hidden = false;
    if (closeTerminal && !refresh) return;
    closeTerminal?.();
    const label = agent.display_name || agent.name;
    terminalHost.innerHTML = `<div class="gchat-terminal"><div class="gchat-top"><span class="gchat-name">${esc(label)}</span><span class="gchat-status">Terminal</span></div><div class="gchat-session-switch">${globalChatViewToggle(true)}</div><div class="gchat-terminal-host"></div></div>`;
    terminalHost.querySelector('[data-chat-return]').onclick = () => showChat();
    terminalHost.querySelector('[data-agent-refresh]').onclick = () => showTerminal(true);
    const shellStatus = terminalHost.querySelector('.gchat-status');
    closeTerminal = mountTerminal(opts.terminal.key, terminalHost.querySelector('.gchat-terminal-host'), status => {
      shellStatus.textContent = status === 'ended' ? 'Terminal exited' : status === 'disconnected' ? 'Terminal disconnected' : 'Terminal';
      opts.onTerminalStatus?.(status);
    }, opts.terminal.base);
  };
  // Also clean up if navigation removes/replaces the hosts without explicitly disposing the view.
  const watcher = setInterval(() => {
    if (!chatHost.isConnected || !terminalHost.isConnected || !current()) view.dispose();
  }, 2000);
  const view = {showChat, showTerminal,
    selectPairedSource(source, generation, onUnavailable, onReference) {
      if (disposed || !chat || chatHost.hidden || !opts.isCurrent?.()) return false;
      return chat.selectPairedSource?.(source, generation, onUnavailable, onReference) ?? false;
    },
    clearPairedSource(generation) {
      if (!chat) return false;
      return chat.clearPairedSource?.(generation) ?? false;
    },
    dispose() {
    if (disposed) return;
    disposed = true; clearInterval(watcher);
    chat?.dispose(); chat = null;
    closeTerminal?.(); closeTerminal = null;
    chatHost.remove(); terminalHost.remove();
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
  let focusStatus = null;
  let pairedHost = null, pairedConversation = null, pairedGeneration = 0;
  const cleanups = [];
  const reads = new AbortController();

  // Native shell: view switch and the session line stay here; every message, composer and preview is the shared view.
  pane.innerHTML = `<div class="gchat" data-global-chat="${esc(agentId)}">
    ${options.openTerminal ? `<div class="gchat-session-switch">${globalChatViewToggle()}</div>` : ''}
  </div>`;
  const root = pane.firstElementChild;
  const $q = selector => root.querySelector(selector);
  const sessionLine = document.createElement('div');
  sessionLine.className = 'gchat-session'; sessionLine.setAttribute('role', 'status'); sessionLine.setAttribute('aria-live', 'polite'); sessionLine.dataset.state = 'loading';
  sessionLine.innerHTML = `<span class="gchat-working-tag" role="img" aria-label="${esc(label)} is working" hidden><span aria-hidden="true">${identityAvatar('agent', label, {...agent, ...agent.presentation})}</span><span class="gchat-working-dots" aria-hidden="true"><i></i><i></i><i></i></span></span><span class="gchat-session-dot" aria-hidden="true"></span><span class="gchat-session-text">Loading…</span><span class="gchat-focus" hidden></span>`;
  const sessionText = sessionLine.querySelector('.gchat-session-text');
  const workingTag = sessionLine.querySelector('.gchat-working-tag');

  const live = () => !disposed && (root.isConnected || !!pairedHost?.isConnected);
  const focusVisible = () => live() && root.isConnected && !root.closest('[hidden]') && !finished
    && viewingLive() && ['live','working'].includes(sessionSummary().key);
  const originPane = typeof _agpad !== 'undefined' ? _agpad : null;
  const openReference = typeof options.openReference === 'function' ? options.openReference
    : ref => globalChatOpenFocusReference(ref, {pane: originPane, agentId, isCurrent: live});
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
      if (!live() || pairedHost || !session || !globalChatReadable(agentId) || !view.isAtBottom()) return;
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
    typedReferences: Array.isArray(entry.references) ? entry.references : [],
    collapsedSource: ((entry.speaker === 'user' && /^(?:\[telegram\](?:\s|$)|Qorqut owner (?:message|reply) via Telegram\b)/i.test((entry.text || '').trimStart()))
      || (entry.system?.verified && entry.system.input_type === 'telegram')) ? 'telegram' : null,
    ...(entry.speaker === 'notice' ? {notice: {label: globalChatNoticeLabel(entry.system), verified: !!entry.system?.verified, inputType: entry.system?.input_type || null,
      markerOnly: !!entry.system?.verified && globalChatMarkerOnly(entry.system.input_type)}} : {}),
  });
  // A successful outgoing agent message: recipients come from the backend DTO, glyphs from the existing trusted renderer.
  const agentMessageRecipient = recipient => {
    const name = String(recipient?.display_name || 'Agent');
    return {id: String(recipient?.agent_id || ''), name, glyphHtml: agentGlyph({...(recipient?.presentation || {}), agent_id: recipient?.agent_id, display_name: recipient?.display_name, role: recipient?.role}, 25)};
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
    if (supersededOwnerCopy(item)) return false;
    return !!viewedKey() && globalChatKey(item.binding) === viewedKey();
  }
  // A failed owner copy stops pinning the bottom once its text reached the transcript later or a newer owner message was delivered.
  const globalChatSame = text => globalChatText(text).replace(/\s+/g, ' ').trim();
  function supersededOwnerCopy(item) {
    if (!ownerChat(item) || !['uncertain', 'failed', 'undelivered'].includes(item.state)) return false;
    const key = globalChatKey(item.binding), since = Date.parse(item.created_at);
    if (inputs.some(other => ownerChat(other) && other.state === 'submitted' && other.sequence > item.sequence
      && globalChatKey(other.binding) === key)) return true;
    if (!item.body) return false;
    const want = globalChatSame(item.body);
    return [...entries.values()].some(entry => entry.speaker === 'user' && globalChatSame(entry.text) === want
      && !(Number.isFinite(since) && Date.parse(entry.timestamp) < since));
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
      // A slash command is done once it is typed: Codex records no entry for it, and Claude may record an alias.
      else if (record.command && record.state === 'submitted') record.entry = 'command';
    }
    // Delivery confirmation can precede transcript capture. Keep the accepted message until its exact echo is loaded.
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
  const canSend = () => !pairedHost && !!session?.can_send && viewingLive() && !finished;
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
    const direction = ['other_conversation', 'selection_required', 'registering'].includes(session.reason) ? 'Connecting to the conversation in Terminal…'
      : `${startHint()}.`;
    if (session.reason === 'capture_disabled') return {key: 'disabled', line: ''};
    if (session.reason === 'unsupported_runtime') return {key: 'offline', line: ''};
    return {key: 'offline', line: direction};
  }
  // ---- slash commands: the catalog for the CLI this session runs
  const commandCatalogs = new Map();
  let slashMenu = null;
  function commandCatalog() {
    const provider = session?.provider;
    if (!provider) return null;
    if (!commandCatalogs.has(provider)) {
      commandCatalogs.set(provider, null);
      api(`${base}/commands?client=${encodeURIComponent(provider)}`)
        .then(data => { if (!disposed) { commandCatalogs.set(provider, coreData(data).commands || []); slashMenu?.refresh(); } })
        .catch(() => commandCatalogs.delete(provider));
    }
    return commandCatalogs.get(provider);
  }
  // Names the programs that connect: any supported CLI started in this agent's own terminal, the agent's default first.
  function startHint() {
    const commands = session?.launch_commands || [];
    return commands.length ? `Open the terminal (>_) and run ${commands.join(' or ')} to start ${label}` : `Start or resume ${label} in Terminal`;
  }
  function emptyState() {
    if (entries.size || shownInputs.length || hasLocal || !session) return {text: '', action: null};
    const key = sessionSummary().key;
    if (key === 'live' || key === 'working') return {text: 'Your agent conversation appears here.', action: null};
    if (key === 'starting') return {text: 'Connecting…', action: null};
    if (key === 'disabled') return {text: 'Chat unavailable.', action: null};
    return {text: `${startHint()}. Its conversation will appear here.`, action: options.openTerminal ? {key: 'terminal', label: 'Open terminal'} : null};
  }
  // One repaint: the native status line, then every presentation field the view displays. `patch` adds fields such as the entries.
  function paint(patch = {}, scroll = 'preserve') {
    if (disposed) return;
    const summary = sessionSummary(), working = summary.key === 'working' && !streamDown && !finished;
    sessionLine.dataset.state = streamDown && summary.key === 'working' ? 'live' : summary.key;
    sessionLine.classList.toggle('gchat-session-working', working);
    // An empty conversation already shows the same direction, with its button, in the middle of the pane.
    sessionText.textContent = finished ? 'This chat is no longer available.' : emptyState().action ? '' : summary.line;
    workingTag.hidden = !working;
    const hasFocus=focusStatus?.show(focusVisible()) || false;
    sessionLine.hidden = !working && !sessionText.textContent && !hasFocus;
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
    // Load the slash command catalog as soon as the running CLI is known, so "/" lists commands at once.
    if (session?.provider) commandCatalog();
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
    // A known slash command is judged for the session it is sent to, before anything is uploaded or sent. A shared Codex server
    // cannot run it: the draft and attachments stay as they are and the owner is told where it runs (the backend refuses it too).
    const slash = slashCommands.command(commandCatalog() || [], text), provider = session?.provider;
    const refusal = slashCommands.sharedServerRefusal(session, slash, label);
    if (refusal) { showBanner(refusal); return; }
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
        globalChatAccept(mine, payload, result.input, attempt.position, !!slash);
        if (slashCommands.opensTerminal(slash, text, provider) && options.openTerminal) {
          toast(`Finish ${slash.name} in ${label}’s Terminal.`);
          options.openTerminal();
        }
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
    onTask: openReference,
    onReference: openReference,
    onFilePreview: ref => agentFilePreviewOpen(ref),
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
    // A Signoff reference is the existing single-item Signoff view: the owner session, exact proposal and decisions stay its own.
    mountSignoff: (ref, host) => sgChatMount(ref, host, {active: () => !disposed}),
    onViewportChange: scheduleRead,
    onPendingAction: (action, id) => {
      if (action === 'resend') void resendInput(id);
      else if (action === 'dismiss') { GLOBAL_CHAT_HIDDEN.add(id); paintInputs(); }
    },
    onEmptyAction: action => {
      if (action === 'terminal') options.openTerminal?.();
    },
  });
  paint();
  // Slash commands: the running CLI's catalog, loaded once per provider (native_chat/commands.py).
  if (view.elements?.input && view.elements?.composer) {
    slashMenu = slashCommands.attach({input: view.elements.input, form: view.elements.composer, commands: commandCatalog});
    cleanups.push(() => slashMenu.dispose());
  }
  // Ordubashy's current workflow Focus appears above the composer; it refreshes with this chat's own loads (no extra timer).
  const workflowPanel = (agent.role?.id || agent.role_id) === 'ordubashy' && view.elements?.composer ? workflowPanelMount(view.elements.composer, agentId) : null;
  if (workflowPanel) cleanups.push(() => workflowPanel.dispose());
  focusStatus=globalChatFocusMount(sessionLine.querySelector('.gchat-focus'),agent,{visible:focusVisible,onChange:paintSession});
  if (focusStatus) cleanups.push(()=>focusStatus.dispose());

  // ---- loading: a state fetch is idempotent, so every "changed" event just reads it again (coalesced)
  // Terminal owns conversation selection; Chat always follows the backend's current conversation.
  const stateUrl = () => `${base}?limit=${GLOBAL_CHAT_PAGE}`;
  async function load(reset = false) {
    if (loading) { again = true; return; }
    loading = true;
    const mine = generation;
    try {
      const data = coreData(await api(stateUrl(), {signal: reads.signal}));
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
      void workflowPanel?.refresh(fresh);
      void focusStatus?.refresh(fresh);
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
      const data = coreData(await api(`${base}/history?before=${before}&limit=${GLOBAL_CHAT_PAGE}${globalChatQuery(target)}`, {signal: reads.signal}));
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
    selectPairedSource(source, generation, onUnavailable, onReference) {
      if (!live() || !source || !source.type || !source.id) return false;
      if (generation < pairedGeneration) return false;
      pairedGeneration = generation;
      const biz=source.business_id == null ? null : (state.bizs || []).find(project =>
        project.id===source.business_id || project.business_id===source.business_id || project.slug===source.business_id)?.slug || null;
      if(source.business_id != null && !biz)return false;
      pairedConversation?.dispose?.(); pairedConversation = null;
      pairedHost = document.createElement('div'); pairedHost.className = 'gchat-paired-record';
      pane.replaceChildren(pairedHost);
      const business = source.business_id ?? null;
      const privateSource = {type: source.type, id: source.id, ...(business == null ? {} : {business_id: business})};
      pairedConversation = conversationMount(pairedHost, {type: source.type, id: source.id, biz, revision: null}, {
        inlineComposer: true, related: true, dismissOnOutside: false, privateGlobal: true, privateSource,
        isVisible: () => live() && pairedHost?.isConnected && !pane.closest('[hidden]') && !document.hidden,
        onUnavailable: () => onUnavailable?.(privateSource),
        openReference: ref => onReference?.(ref),
      });
      pairedConversation?.expand?.();
      return true;
    },
    clearPairedSource(generation) {
      if (generation != null && generation < pairedGeneration) return false;
      pairedGeneration = generation ?? pairedGeneration + 1;
      const hadPair = !!(pairedConversation || pairedHost);
      pairedConversation?.dispose?.(); pairedConversation = null; pairedHost = null;
      if (disposed) return true;
      // Leaving Focus with no paired record open must not disturb the live conversation: nothing needs restoring.
      if (!hadPair && pane.firstElementChild === root) return true;
      pane.replaceChildren(root); void load(true);
      return true;
    },
    dispose() {
      disposed = true; generation++;
      reads.abort();
      const pairedNode=pairedHost;
      pairedConversation?.dispose?.(); pairedConversation = null; pairedHost = null;
      closeStream(); clearTimeout(retryTimer); clearTimeout(readTimer);
      saveDraft();
      cleanups.splice(0).forEach(cleanup => cleanup());
      view.dispose();
      if (pane.firstElementChild === pairedNode || pane.firstElementChild === root) pane.replaceChildren();
    },
    focus() { view.focusComposer(); },
  };
}
