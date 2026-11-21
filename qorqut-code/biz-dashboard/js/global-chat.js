// Persistent chat for any registered agent (global or business). It is a projection of the agent's native session
// conversation (qorqut-code docs/v1.2/NATIVE_CHAT_DTO.md): opening, switching or reading never starts, resumes, stops or types
// into an agent; a message from the owner starts or wakes the agent's session, and it rests again by itself. One mount owns
// one SSE stream, its timers and listeners, and releases all of them in dispose().
// Composer state is bound to the exact agent and conversation, and outlives a mount so a switch never leaks or loses it.
// When agents run in terminals (the legacy option) a payload with `session.execution === 'terminal'` takes the small guarded calls into
// js/agent-terminal-view.js (Terminal view, live/offline wording); every other payload is a managed session and never reaches them.
const GLOBAL_CHAT_DRAFTS = new Map();           // scope -> unsent composer text
const GLOBAL_CHAT_FILES = new Map();            // scope -> selected browser Files; copied only when Send is pressed
const GLOBAL_CHAT_SENDS = new Map();            // scope -> {text, key, pin}: the frozen identity of an unconfirmed send, reused by a retry
const GLOBAL_CHAT_INFLIGHT = new Map();         // scope -> {text, promise} of a send still waiting for its response
const GLOBAL_CHAT_RESENDS = new Map();          // undelivered input id -> {key, pin}: identity of an explicit resend
const GLOBAL_CHAT_OPENING = new Map();          // agent id -> {text, at, position}: a message (or file) that is opening a closed session, shown at once as a pending bubble
const GLOBAL_CHAT_HIDDEN = new Set();           // settled inputs the owner dismissed or already resent
// A session that was just opened writes the owner's line to the transcript about 0.1 s before the input record is stamped, so an echo up to this
// much older than the receipt still belongs to it (the same slack the opening bubble uses).
const GLOBAL_CHAT_ECHO_SLACK = 2000;
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
const GLOBAL_CHAT_REFUSALS = new Set(['capture_disabled', 'capture_not_ready', 'binding_not_ready', 'operation_unavailable',
  'unsupported_runtime', 'terminal_changed', 'validation_error', 'not_found', 'forbidden', 'idempotency_conflict',
  'managed_command', 'session_closed', 'driver_unavailable', 'cli_unavailable', 'cli_not_signed_in', 'resume_failed', 'resume_unavailable', 'open_timeout', 'open_busy',
  'session_opening', 'host_unverified', 'terminal_offline', 'terminal_required', 'terminal_present', 'terminal_mode_off', 'runtime_switching']);
const GLOBAL_CHAT_REFUSAL_TEXT = {
  capture_disabled: 'Chat unavailable.',
  capture_not_ready: 'Connecting. Try again in a moment.',
  binding_not_ready: 'Connecting. Try again in a moment.',
  terminal_changed: 'Not delivered. Check the agent and try again.',
  managed_command: 'This command isn’t available when Qorqbot runs the agent.',
  session_closed: 'Not running. Send the message again to start it.',
  driver_unavailable: 'Couldn’t start. Check in Settings that Claude Code or Codex is connected, then send again.',
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
const globalChatHasContext = agent => ['ordubashy','global-worker'].includes(agent.role?.id || agent.role_id);
// ---- the mount
// One Chat for any registered agent, on the Global page and in a business workspace.
// `agent.agent_id` is the registered id the chat API needs. Navigation/dispose releases its stream.
// opts: {isCurrent(): bool}
let globalAgentViewActive = null;
// Shown when a thread in the Chat area cannot close yet (Chat, another thread, or leaving Focus).
const PAIRED_THREAD_BUSY = 'Wait for the message to send, or save the signoff, before leaving this thread.';
function agentViewDispose() { globalAgentViewActive?.dispose(); }
function mountAgentView(pane, agent, opts = {}) {
  agentViewDispose();
  // Terminals mode: a second host for the Terminal view and the Chat/Terminal switch live in js/agent-terminal-view.js.
  if (typeof agentTerminalView !== 'undefined' && agentTerminalView.on()) {
    return agentTerminalView.mountView(pane, agent, opts, {mountChat: mountGlobalChat, openReference: globalChatOpenFocusReference,
      adopt: view => { globalAgentViewActive = view; }, release: view => { if (globalAgentViewActive === view) globalAgentViewActive = null; }});
  }
  let chat = null, disposed = false;
  const chatHost = document.createElement('div');
  chatHost.className = 'gchat-view-host';
  pane.replaceChildren(chatHost);
  const current = () => !disposed && (!opts.isCurrent || opts.isCurrent());
  const showChat = () => {
    if (!current()) return;
    chatHost.hidden = false;
    if (chat) return;
    const originAgentId = String(agent.agent_id ?? '');
    chat = mountGlobalChat(chatHost, agent, {openReference: ref => globalChatOpenFocusReference(ref, {
      agentId: originAgentId,
      isCurrent: () => current() && !chatHost.hidden,
    })});
  };
  // Also clean up if navigation removes/replaces the host without explicitly disposing the view.
  const watcher = setInterval(() => {
    if (!chatHost.isConnected || !current()) view.dispose();
  }, 2000);
  const view = {showChat,
    selectPairedSource(source, generation, onUnavailable, onReference, onBack) {
      if (disposed || !opts.isCurrent?.()) return false;
      if (chatHost.hidden) showChat();
      if (!chat || chatHost.hidden) return false;
      return chat.selectPairedSource?.(source, generation, onUnavailable, onReference, onBack) ?? false;
    },
    openSide(spec) {
      if (disposed || !opts.isCurrent?.()) return null;
      if (chatHost.hidden) showChat();
      return chat && !chatHost.hidden ? chat.openSide?.(spec) ?? null : null;
    },
    clearPairedSource(generation) {
      if (!chat) return false;
      return chat.clearPairedSource?.(generation) ?? false;
    },
    pairedCanLeave() { return chat?.pairedCanLeave?.() ?? true; },
    dispose() {
    if (disposed) return;
    disposed = true; clearInterval(watcher);
    chat?.dispose(); chat = null;
    chatHost.remove();
    if (globalAgentViewActive === view) globalAgentViewActive = null;
  }};
  globalAgentViewActive = view;
  showChat();
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
  let contextStatus = null, timelineBlock = null;
  let pairedHost = null, pairedConversation = null, pairedGeneration = 0;
  // A thread or side chat can take over this Chat area under a small header whose "Chat" button returns here.
  let takeover = null;   // {kind: 'paired'|'side', node, dead, beforeLeave, cleanups}
  const cleanups = [];
  const reads = new AbortController();

  // Native shell: the session line stays here; every message, composer and preview is the shared view.
  const hasContext=globalChatHasContext(agent);
  // Managed runtime (js/agent-runtime.js): the Stop, activity and question block is built with the first session read.
  let managedBar = null;
  // Terminals mode only: the Chat/Terminal switch (options.openTerminal comes from the Terminal view and from nowhere else).
  const terminalSync = {};
  const controls = options.openTerminal && typeof agentTerminalView !== 'undefined' ? `<div class="gchat-session-switch">${agentTerminalView.toggleHtml()}</div>` : '';
  pane.innerHTML = `<div class="gchat" data-global-chat="${esc(agentId)}">${controls}</div>`;
  const root = pane.firstElementChild;
  const $q = selector => root.querySelector(selector);
  const sessionLine = document.createElement('div');
  sessionLine.className = 'gchat-session'; sessionLine.setAttribute('role', 'status'); sessionLine.setAttribute('aria-live', 'polite'); sessionLine.dataset.state = 'loading';
  sessionLine.innerHTML = `<span class="gchat-working-tag" role="img" aria-label="${esc(label)} is working" hidden><span class="gchat-working-dots" aria-hidden="true"><i></i><i></i><i></i></span></span><span class="gchat-session-text">Loading…</span>`;
  const sessionText = sessionLine.querySelector('.gchat-session-text');
  const workingTag = sessionLine.querySelector('.gchat-working-tag');

  const live = () => !disposed && (root.isConnected || !!pairedHost?.isConnected || !!takeover?.node?.isConnected);
  const contextVisible = () => live() && root.isConnected && !root.closest('[hidden]') && !finished && !pairedHost && !takeover;
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
  // The client's own record of a Stop ("[Request interrupted by user]", with or without "for tool use") is not something the owner said:
  // older transcripts hold it as a user entry. It reads as a quiet centred notice, like the other system lines.
  const presentEntry = entry => entry.speaker === 'user' && /^\[Request interrupted by user(?: for tool use)?\]$/.test(String(entry.text ?? '').trim()) ? {key: entry.id, speaker: 'notice', text: 'Stopped.', timestamp: entry.timestamp, partial: false, position: entry.position,
    typedReferences: [], collapsedSource: null, notice: {label: '', inputType: null, markerOnly: false}} : ({
    key: entry.id, speaker: entry.speaker, text: entry.text, timestamp: entry.timestamp, partial: entry.partial === true, position: entry.position,
    typedReferences: Array.isArray(entry.references) ? entry.references : [],
    collapsedSource: ((entry.speaker === 'user' && /^(?:\[telegram\](?:\s|$)|Qorqut owner (?:message|reply) via Telegram\b)/i.test((entry.text || '').trimStart()))
      || (entry.system?.verified && entry.system.input_type === 'telegram')) ? 'telegram' : null,
    ...(entry.speaker === 'notice' ? {notice: {label: globalChatNoticeLabel(entry.system), verified: !!entry.system?.verified, inputType: entry.system?.input_type || null,
      markerOnly: !!entry.system?.verified && globalChatMarkerOnly(entry.system.input_type)}} : {}),
  });
  const isAgentMessage = entry => entry.speaker === 'notice' && Array.isArray(entry.agent_message?.recipients);
  const isNudge = entry => entry.speaker === 'notice' && entry.system?.verified === true && entry.system.input_type === 'inbox_nudge';
  // Rebuild contiguous runs from the ID-keyed, ordered transcript on every render.
  function groupActivity(sorted) {
    const rows = [];
    for (const entry of sorted) {
      const last = rows[rows.length - 1];
      if (!isNudge(entry) && !isAgentMessage(entry)) { rows.push(entry); continue; }
      if (last?.group) last.group.push(entry);
      else rows.push({group: [entry]});
    }
    return rows.map(row => {
      if (!row.group) return presentEntry(row);
      const first = row.group[0], seen = new Set(), recipients = [];
      let nudgeCount = 0;
      for (const entry of row.group) {
        if (isNudge(entry)) nudgeCount++;
        if (!isAgentMessage(entry)) continue;
        for (const raw of entry.agent_message.recipients) {
          const name = String(raw?.display_name || 'Agent'), id = String(raw?.agent_id || '');
          const key = id || `name:${name}`;
          if (!seen.has(key)) {
            seen.add(key);
            recipients.push({id, name, glyphHtml: agentGlyph({...(raw?.presentation || {}), agent_id: raw?.agent_id, display_name: raw?.display_name, role: raw?.role}, 16)});
          }
        }
      }
      return {...presentEntry(first), text: '', notice: {label: 'Activity', verified: true, markerOnly: true, activity: {nudgeCount, recipients}}};
    });
  }
  const orderedEntries = () => groupActivity([...entries.values()].sort((a, b) => a.position - b.position));
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
    const local = [...acceptedBubbles(new Set(shownInputs.map(item => item.id))), ...openingBubble()];
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
          && !(Number.isFinite(at) && Number.isFinite(record.createdAt) && at < record.createdAt - GLOBAL_CHAT_ECHO_SLACK);
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
  // The owner's message while it opens a sleeping or off agent (about 15 seconds): it is in the conversation at once, in the style of a
  // message being sent, and is replaced by the accepted bubble and then by the transcript's own entry. It is hidden as soon as the
  // transcript already holds that entry, so nothing shows twice.
  function openingBubble() {
    const held = GLOBAL_CHAT_OPENING.get(agentId);
    if (!held) return [];
    const want = globalChatText(held.probe);
    const echoed = want && [...entries.values()].some(entry => entry.speaker === 'user' && entry.position > held.position
      && globalChatText(entry.text).startsWith(want) && !(Number.isFinite(Date.parse(entry.timestamp)) && Date.parse(entry.timestamp) < held.at - 2000));
    if (echoed) return [];
    return [{sequence: Number.MAX_SAFE_INTEGER, model: {key: `opening-${agentId}`, speaker: 'user', state: 'submitting', stateLabel: GLOBAL_CHAT_INPUT_STATE.submitting,
      inputType: 'chat', resendable: false, collapsedSource: null, text: held.text, actions: []}}];
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
          ...(resendable ? [{key: 'resend', label: 'Resend', disabled: !session?.can_send || GLOBAL_CHAT_RESENDS.get(item.id)?.pending === true,
            title: GLOBAL_CHAT_RESENDS.get(item.id)?.pending === true ? 'Sending again…' : `${label} isn’t available right now.`}] : []),
          ...(dismissable ? [{key: 'dismiss', label: 'Dismiss'}] : []),
        ],
      };
    });
  }

  // ---- session status, composer enablement and the empty state
  // A closed managed agent has no binding to pin: sending a message opens its session.
  // A terminal agent that is not running receives nothing: a message goes only to the live conversation.
  const canSend = () => !pairedHost && !!session?.can_send
    && (viewingLive() || (!(typeof agentTerminalView !== 'undefined' && agentTerminalView.isTerminal(session)) && agentRuntime.canOpen(session))) && !finished;
  const selectedFiles = () => GLOBAL_CHAT_FILES.get(composerScope) || [];
  // What the conversation holds, and whether a message that starts the session is on its way: the wording of the session line.
  const hasHistory = () => entries.size > 0 || shownInputs.length > 0 || hasLocal;
  const sessionSummary = () => session && typeof agentTerminalView !== 'undefined' && agentTerminalView.isTerminal(session)
    ? agentTerminalView.summary(session, label, viewingLive()) : session ? agentRuntime.summary(session, label, viewingLive(), {history: hasHistory(),
    opening: agentRuntime.closed(session) && (GLOBAL_CHAT_INFLIGHT.has(composerScope) || GLOBAL_CHAT_OPENING.has(agentId))}) : {key: 'loading', line: ''};
  // ---- slash commands: the catalog for the CLI this session runs
  const commandCatalogs = new Map();
  let slashMenu = null;
  function commandCatalog() {
    const provider = session ? session.provider || 'claude' : null;
    if (!provider) return null;
    if (!commandCatalogs.has(provider)) {
      commandCatalogs.set(provider, null);
      api(`${base}/commands?client=${encodeURIComponent(provider)}`)
        .then(data => {
          if (disposed) return;
          // A terminal agent's menu commands are finished in its Terminal: they are offered, not refused.
          const list = coreData(data).commands || [];
          commandCatalogs.set(provider, typeof agentTerminalView !== 'undefined' && agentTerminalView.isTerminal(session) ? agentTerminalView.catalog(list) : list);
          slashMenu?.refresh();
        })
        .catch(() => commandCatalogs.delete(provider));
    }
    return commandCatalogs.get(provider);
  }
  function emptyState() {
    if (entries.size || shownInputs.length || hasLocal || !session) return {text: '', action: null};
    const key = sessionSummary().key;
    if (typeof agentTerminalView !== 'undefined' && agentTerminalView.isTerminal(session)) return agentTerminalView.emptyState(session, label, key, options.openTerminal);
    if (key === 'live' || key === 'working') return {text: 'Your agent conversation appears here.', action: null};
    if (key === 'closed' || key === 'off' || key === 'offline') return {text: agentRuntime.emptyText(session, label), action: null};
    if (key === 'starting') return {text: `Starting ${label}…`, action: null};
    if (key === 'disabled') return {text: 'Chat unavailable.', action: null};
    return {text: '', action: null};
  }
  // ---- managed runtime: Stop, the activity line and the cards follow the session
  const runtimeSend = async (path, body) => coreData(await post(`${base}${path}`, body));
  function syncManaged() {
    // A terminal agent has no managed session: no Stop block, no activity line, no question cards.
    const terminal = typeof agentTerminalView !== 'undefined' && agentTerminalView.isTerminal(session);
    if (session && !terminal && !managedBar && sessionLine.parentNode) {
      // Stop lives in the composer (Send turns into Stop while the agent works and nothing is typed), not on the activity line.
      managedBar = agentRuntime.createBar({label, send: runtimeSend, binding: () => liveBinding()?.binding_id || null,
        onChange: () => { paintSession(); void load(); }, onError: showBanner, stopHere: false});
      sessionLine.parentNode.insertBefore(managedBar.el, sessionLine);
      cleanups.push(() => managedBar?.dispose());
      // The model and effort pill sits at the bottom left of the composer (js/chat-model-picker.js).
      const form = view?.elements?.composer;
      if (form && typeof chatModelPicker !== 'undefined') {
        const picker = chatModelPicker.mount({base, label, read: async path => coreData(await api(path)), write: async (path, body) => coreData(await post(path, body)),
          onError: showBanner});
        form.insertBefore(picker.el, form.querySelector('.gchat-attach, .gchat-mic, .gchat-send'));
        cleanups.push(() => picker.dispose());
      }
    }
    // In Ordubashy's own chat the activity line (what it is doing, Stop) sits in the focus strip, where the focus intent is.
    if (managedBar) contextStatus?.hostActivity(managedBar.line);
    managedBar?.update(session);
    contextStatus?.paintActivity();
  }
  // The session line says what the empty conversation does not already say in the middle of the pane.
  const sessionLineText = (summary, hint) => hint.text && ['closed', 'off', 'starting'].includes(summary.key) ? '' : summary.line;
  // One repaint: the native status line, then every presentation field the view displays. `patch` adds fields such as the entries.
  function paint(patch = {}, scroll = 'preserve') {
    if (disposed) return;
    const summary = sessionSummary(), working = summary.key === 'working' && !streamDown && !finished;
    const terminal = typeof agentTerminalView !== 'undefined' && agentTerminalView.isTerminal(session);
    sessionLine.dataset.state = streamDown && summary.key === 'working' ? 'live' : summary.key;
    sessionLine.classList.toggle('gchat-session-working', working);
    // An empty conversation already shows the same direction in the middle of the pane.
    const hint = emptyState();
    sessionText.textContent = finished ? 'This chat is no longer available.' : terminal ? agentTerminalView.sessionLineText(summary, hint) : sessionLineText(summary, hint);
    // The activity line (Stop, what the agent is doing) already shows the dots while it is there.
    workingTag.hidden = !working || !!agentRuntime.activity(session).text;
    contextStatus?.show();
    // The question card and the activity line take room from the message list. Whether the owner was at the end of it is read before
    // they appear, so the list stays at its end (the last message is never under the card) and a list scrolled up stays where it is.
    const pinned = !!view?.isAtBottom?.();
    syncManaged();
    if (options.openTerminal && typeof agentTerminalView !== 'undefined') agentTerminalView.syncChat(root, session, options, terminalSync);
    sessionLine.hidden = !sessionText.textContent && (workingTag.hidden || workingTag.parentElement !== sessionLine);
    const enabled = canSend(), sending = GLOBAL_CHAT_INFLIGHT.has(composerScope) || GLOBAL_CHAT_OPENING.has(agentId);
    // A message that opens the session is on its way: every control that is off says the one thing, "Starting <agent>…", until the session is open.
    const opening = !terminal && (GLOBAL_CHAT_OPENING.has(agentId) || (agentRuntime.closed(session) && GLOBAL_CHAT_INFLIGHT.has(composerScope)));
    const unavailable = () => terminal ? agentTerminalView.unavailableText(session, label, {finished, paired: !!pairedHost})
      : agentRuntime.unavailableText(session, label, {finished, paired: !!pairedHost, opening});
    const pending = presentInputs();
    view.render({
      ...patch, pending,
      composer: {inputDisabled: !enabled, sendDisabled: !enabled || sending, filesDisabled: !enabled || sending,
        stop: !terminal && !finished && !pairedHost && viewingLive() && agentRuntime.activity(session).stop,
        stopDisabled: !!managedBar?.stopping, stopTitle: managedBar?.stopping ? 'Stopping…' : `Stop what ${label} is doing`,
        disabledReason: enabled ? '' : unavailable(),
        sendingReason: GLOBAL_CHAT_OPENING.has(agentId) ? `Starting ${label}…` : 'Sending your message…', filesReason: opening ? `Starting ${label}…` : 'Wait until your message is sent.',
        attachments: selectedFiles().map(file => ({key: file.key, name: file.file.name})),
        placeholder: enabled || !session ? agentRuntime.placeholder(session, label, hasHistory()) : unavailable()},
      older: {visible: older != null, disabled: olderBusy},
      empty: emptyState(),
    }, {scroll});
    if (pinned && !view.isAtBottom()) view.scrollToBottom();
    // Load the slash command catalog as soon as the running CLI is known, so "/" lists commands at once.
    if (session?.provider) commandCatalog();
  }
  const paintEntries = scroll => paint({entries: orderedEntries()}, scroll);
  const paintInputs = () => paint();
  const paintSession = () => paint();
  const showBanner = text => { bannerText = text || ''; reconnecting = false; view.render({banner: {text: bannerText}}); };
  if (options.openTerminal) listen(root, 'click', event => { if (event.target.closest('[data-terminal]')) options.openTerminal(); });

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
    const next = globalChatScope(agentId, conversation) || (agentRuntime.canOpen(session) ? `${agentId}\nmanaged` : null);
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
  const refusalText = problem => problem.offered ? '' : (typeof agentTerminalView !== 'undefined' && agentTerminalView.refusalText(session, problem.code))
    || agentRuntime.refusalText(problem.code, problem.serverMessage) || GLOBAL_CHAT_REFUSAL_TEXT[problem.code] || "Couldn't send. Try again";
  // One admission request. The result is recorded in module state so a mount that was left meanwhile loses nothing.
  // A closed managed agent has no session to pin: the message goes without a binding and opens one.
  // The owner's explicit choice to start a fresh conversation instead of the one that can't be continued: the next request (and its
  // retries, which keep their key) is sent with ``new_conversation``; ``key`` is bound when that request is first made.
  // A message to a closed session: the owner sees it at once, the composer is empty meanwhile, and a failed open puts the text back.
  const opening = {
    active: () => GLOBAL_CHAT_OPENING.has(agentId),
    start(text, attempt) {
      if (attempt.pin !== null || !agentRuntime.closed(session)) return false;
      const names = attempt.files.map(item => item.file.name).join(', ');
      GLOBAL_CHAT_OPENING.set(agentId, {text: text.trim() ? text : names, probe: text.trim() ? text : '', at: Date.now(), position: attempt.position});
      if (composerScope) GLOBAL_CHAT_DRAFTS.delete(composerScope);
      if (view.getDraft() === text) view.setDraft('');
      return true;
    },
    // Nothing the owner typed is lost: the text returns to the composer (ahead of anything typed meanwhile).
    restore(scope, text) {
      const target = live() && composerScope ? composerScope : scope;
      const now = live() && composerScope === target ? view.getDraft() : GLOBAL_CHAT_DRAFTS.get(target) || '';
      const draft = !now.trim() ? text : now.includes(text) ? now : `${text}\n${now}`;
      GLOBAL_CHAT_DRAFTS.set(target, draft);
      if (live() && composerScope === target) view.setDraft(draft);
    },
    end() { GLOBAL_CHAT_OPENING.delete(agentId); },
    // A file opened the session: the message that follows names the binding that came up, as a message to an open session does.
    async pin() {
      try { return coreData(await api(stateUrl(), {signal: reads.signal})).session?.binding?.binding_id || null; } catch (_) { return null; }
    },
  };
  let freshChoice = null;
  async function startFresh(text) {
    freshChoice = {key: null};
    try { await submit(text); } finally { if (freshChoice && freshChoice.key === null) freshChoice = null; }
  }
  const admit = async (text, key, pin) => {
    if (freshChoice && freshChoice.key && freshChoice.key !== key) freshChoice = null;
    const fresh = !!freshChoice && !pin && (freshChoice.key ??= key) === key;
    try {
      const result = coreData(await post(`${base}/messages`, {text, idempotency_key: key, ...(pin ? {expected_binding: pin} : {}), ...(fresh ? {new_conversation: true} : {})}));
      if (fresh) freshChoice = null;
      managedBar?.clearOffer();
      return result;
    } catch (problem) {
      // "Not yet" refusals (still starting, host busy) keep the request as it is, key and all; a refusal about the earlier
      // conversation offers the owner a new one, and says so beside the composer instead of in the banner.
      if (agentRuntime.isRetryable(problem?.code)) problem.keepAttempt = true;
      else if (fresh) freshChoice = null;
      const offer = managedBar ? agentRuntime.newConversationOffer(problem?.code) : null;
      if (offer) { problem.offered = true; managedBar.offer(offer, () => { const draft = live() ? view.getDraft() : ''; if (draft.trim()) void startFresh(draft); }); }
      throw problem;
    }
  };
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
    if (!mine || !text.trim() && !files.length || !canSend() || GLOBAL_CHAT_INFLIGHT.has(mine) || opening.active()) return;
    // A known slash command is judged for the session it is sent to, before anything is uploaded or sent. A shared Codex server
    // cannot run it: the draft and attachments stay as they are and the owner is told where it runs (the backend refuses it too).
    const slash = slashCommands.command(commandCatalog() || [], text);
    const refusal = slashCommands.refusal(session, slash, label);
    if (refusal) { showBanner(refusal); return; }
    // A retry of the same text reuses its key and its original binding pin, so a lost response can never become a second
    // message or move to another session. Only a deliberate new send takes the binding observed now.
    let attempt = GLOBAL_CHAT_SENDS.get(mine);
    if (!attempt || attempt.text !== text || attempt.fileKey !== fileKey) attempt = {text, fileKey, files, key: newRequestId(), pin: liveBinding()?.binding_id ?? null, position: newestPosition()};
    GLOBAL_CHAT_SENDS.set(mine, attempt);
    const opened = opening.start(text, attempt);
    const run = (async () => {
      try {
        const references = [];
        for (const item of attempt.files) {
          if (!item.saved || item.savedPin !== attempt.pin) {
            try {
              item.saved = coreData(await api(`${base}/attachments`, {method: 'POST',
                headers: {'Content-Type': 'application/octet-stream', 'X-File-Name': encodeURIComponent(item.file.name),
                  'Idempotency-Key': `${item.key}:${attempt.pin}`, ...(attempt.pin ? {'X-Chat-Binding': attempt.pin} : {})}, body: item.file}));
              item.savedPin = attempt.pin;
            } catch (error) { error.attachment = true; throw error; }
            // The first file for a sleeping agent opened its session: the files after it, and the message, name the binding that came up.
            if (attempt.pin === null) {
              const pin = await opening.pin();
              if (pin) { attempt.pin = pin; for (const other of attempt.files) if (other.saved) other.savedPin = pin; }
            }
          }
          references.push(item.saved.reference);
        }
        if (attempt.pin === null && references.length) {
          const pin = await opening.pin();
          if (pin) { attempt.pin = pin; for (const item of attempt.files) item.savedPin = pin; }
        }
        const payload = references.length ? [text, references.join('\n')].filter(part => part.trim()).join('\n\n') : text;
        if (payload.length > GLOBAL_CHAT_MAX) {
          const error = new Error('Message too long'); error.attachment = true; error.code = 'message_too_long'; throw error;
        }
        const result = await admit(payload, attempt.key, attempt.pin);
        globalChatAccept(mine, payload, result.input, attempt.position, !!slash);
        if (typeof agentTerminalView !== 'undefined') agentTerminalView.finishInTerminal(slash, text, session, label, options.openTerminal, toast);
        // A first message opens the conversation, so the composer may have moved to its scope meanwhile: the bubble follows it.
        if (composerScope && composerScope !== mine) globalChatAccept(composerScope, payload, result.input, attempt.position, !!slash);
        if (GLOBAL_CHAT_SENDS.get(mine) === attempt) GLOBAL_CHAT_SENDS.delete(mine);
        if (GLOBAL_CHAT_DRAFTS.get(mine) === text) GLOBAL_CHAT_DRAFTS.delete(mine);
        const sent = new Set(attempt.files.map(file => file.key));
        const remaining = (GLOBAL_CHAT_FILES.get(mine) || []).filter(file => !sent.has(file.key));
        if (remaining.length) GLOBAL_CHAT_FILES.set(mine, remaining); else GLOBAL_CHAT_FILES.delete(mine);
        if (live() && composerScope === mine) { if (view.getDraft() === text) view.setDraft(''); showBanner(''); }
        takeInput(result.input);
        if (live()) { view.scrollToBottom(); void load(); }
      } catch (problem) {
        if (opened) opening.restore(mine, text);
        // A refusal stores nothing, so its request is dropped; except one that is only "not yet" (``keepAttempt``, set by ``admit``: the
        // session is still starting, or the host is busy): that request keeps its key, so the retry is the same message, never a
        // second one and never a second open.
        if (GLOBAL_CHAT_REFUSALS.has(problem.code) && !problem.keepAttempt) { if (GLOBAL_CHAT_SENDS.get(mine) === attempt) GLOBAL_CHAT_SENDS.delete(mine); }
        if (live() && composerScope === mine) showBanner(problem.code === 'message_too_long' ? 'Shorten your message and try again.'
          : problem.attachment ? "Couldn't attach the file. Try again."
          : GLOBAL_CHAT_REFUSALS.has(problem.code) ? refusalText(problem) : "Couldn't confirm. Try again");
        if (problem.code === 'capture_not_ready' || problem.code === 'terminal_changed' || problem.code === 'terminal_offline') void load();
      } finally {
        GLOBAL_CHAT_INFLIGHT.delete(mine);
        if (opened) opening.end();
        if (live()) paintSession();
      }
    })();
    GLOBAL_CHAT_INFLIGHT.set(mine, {text, promise: run});
    paintSession();
    if (opened && live()) view.scrollToBottom();
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
      if (GLOBAL_CHAT_REFUSALS.has(problem.code) && !problem.keepAttempt) GLOBAL_CHAT_RESENDS.delete(id);
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
    onStop: () => { if (!managedBar) return; void managedBar.stop(); paintSession(); },
    onTask: openReference,
    onReference: openReference,
    onFilePreview: ref => agentFilePreviewOpen(ref),
    onFiles: files => {
      if (!composerScope || !canSend() || GLOBAL_CHAT_INFLIGHT.has(composerScope) || opening.active() || !files.length) return;
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
    onEmptyAction: action => { if (action === 'terminal') options.openTerminal?.(); },
    onPendingAction: (action, id) => {
      if (action === 'resend') void resendInput(id);
      else if (action === 'dismiss') { GLOBAL_CHAT_HIDDEN.add(id); paintInputs(); }
    },
  });
  paint();
  // Slash commands: the running CLI's catalog, loaded once per provider (native_chat/commands.py).
  if (view.elements?.input && view.elements?.composer) {
    slashMenu = slashCommands.attach({input: view.elements.input, form: view.elements.composer, commands: commandCatalog});
    cleanups.push(() => slashMenu.dispose());
  }
  // The focus board keeps Ordubashy first without changing this chat's input or session identity.
  if (hasContext && view.elements?.composer) {
    contextStatus=chatFocusPanelMount(view.elements.composer,agentId,{agent,visible:contextVisible,onChange:paintSession,workingTag});
    cleanups.push(()=>contextStatus.dispose());
  }
  // The Global timeline is not shown above the main chat (the owner's choice, 18 November 2026): agents still receive what
  // holds at the Global level, and its standing rules stay readable through the timeline views.

  // ---- loading: a state fetch is idempotent, so every "changed" event just reads it again (coalesced)
  // Chat always follows the backend's current conversation.
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
      void contextStatus?.refresh();
      void timelineBlock?.refresh();
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
  // A managed agent's activity changes often. The event's `managed` summary is shown at once, before the page read that follows
  // it lands, so Stop, the activity line and the cards do not wait for a round trip. It never replaces the read: the event
  // cannot say whether the conversation changed too.
  function showManagedEvent(data) {
    const summary = data?.managed;
    if (!summary || typeof summary !== 'object' || !session?.managed) return;
    if ((summary.open !== false) !== (session.managed.open !== false)) return;
    if ('off' in summary && (summary.off === true) !== (session.managed.off === true)) return;
    session = {...session, managed: {...session.managed, ...summary}};
    paintSession();
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
      if (item.kind === 'changed') { showManagedEvent(item.data); void load(); } // an event renders nothing; the fetched page does
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

  function takeoverShell(kind, title, onBack) {
    const node = document.createElement('div');
    node.className = 'gchat-takeover'; node.dataset.takeover = kind;
    node.innerHTML = `<div class="gchat-takeover-head"><button type="button" class="quiet gchat-takeover-back" title="Back to Chat"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg><span>Chat</span></button><span class="gchat-takeover-title"></span></div><div class="gchat-takeover-body"></div>`;
    const titleNode = node.querySelector('.gchat-takeover-title');
    titleNode.textContent = title || '';
    node.querySelector('.gchat-takeover-back').onclick = () => onBack?.();
    return {node, head: node.firstElementChild, title: titleNode, body: node.lastElementChild};
  }
  function pairedLeavable() {
    if (pairedConversation?.canLeave?.() !== false) return true;
    toast(PAIRED_THREAD_BUSY);
    return false;
  }
  function endTakeover(t) {
    if (!t || t.dead) return;
    t.dead = true;
    for (const fn of t.cleanups.splice(0)) { try { fn(); } catch (_) { /* one cleanup cannot stop the others */ } }
    if (takeover === t) takeover = null;
  }
  function restoreChat() {
    if (disposed) return;
    pane.replaceChildren(root); void load(true);
  }
  // A side chat must agree to leave (a send or a rename in flight) before anything replaces it.
  async function leaveSide() {
    const t = takeover;
    if (!t || t.kind !== 'side') return true;
    if (t.beforeLeave && !await t.beforeLeave()) return false;
    if (takeover !== t) return false;
    endTakeover(t);
    return true;
  }

  void load();
  return {
    agentId,
    async selectPairedSource(source, generation, onUnavailable, onReference, onBack) {
      if (!live() || !source || !source.type || !source.id) return false;
      if (generation < pairedGeneration) return false;
      if (pairedConversation?.canLeave?.() === false) return false;
      pairedGeneration = generation;
      if (takeover?.kind === 'side' && (!await leaveSide() || generation !== pairedGeneration || disposed)) return false;
      const biz=source.business_id == null ? null : (state.bizs || []).find(project =>
        project.id===source.business_id || project.business_id===source.business_id || project.slug===source.business_id)?.slug || null;
      if(source.business_id != null && !biz)return false;
      pairedConversation?.dispose?.(); pairedConversation = null;
      endTakeover(takeover);
      let thread = null;
      // Chat always returns to the main conversation from the thread it shows, even once the Focus
      // view that opened it has gone; a send or unsaved signoff still keeps the thread open.
      const shell = takeoverShell('paired', source.label || '', async () => {
        if (!pairedLeavable()) return;
        if (await onBack?.() !== false || takeover !== thread || thread.dead || disposed) return;
        this.clearPairedSource();
      });
      takeover = thread = {kind: 'paired', node: shell.node, dead: false, beforeLeave: null, cleanups: []};
      if (typeof threadPins !== 'undefined') {
        const pin = document.createElement('button'); pin.type='button'; pin.className='quiet gchat-thread-pin';
        pin.innerHTML=typeof RW_PIN_ICON==='string'?RW_PIN_ICON:'Pin';
        const paintPin=()=>{const pinned=threadPins.isPinned(source);pin.setAttribute('aria-pressed',String(pinned));pin.title=pinned?'Unpin thread':'Pin thread';pin.setAttribute('aria-label',pin.title);};
        pin.onclick=async()=>{pin.disabled=true;try{await threadPins.set(source,source.label,!threadPins.isPinned(source));}finally{pin.disabled=false;paintPin();}};
        paintPin();shell.head.append(pin);takeover.cleanups.push(threadPins.subscribe(paintPin));
        if(!threadPins.isLoaded())void threadPins.load();
      }
      if (onReference) {
        const details=document.createElement('button');details.type='button';details.className='quiet gchat-thread-details';
        details.textContent=source.type==='project'?'Open project':'Open details';
        details.onclick=()=>onReference({...source});shell.head.append(details);
      }
      pairedHost = document.createElement('div'); pairedHost.className = 'gchat-paired-record';
      shell.body.append(pairedHost);
      pane.replaceChildren(shell.node);
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
    pairedCanLeave() { return pairedConversation?.canLeave?.() !== false; },
    clearPairedSource(generation) {
      if (generation != null && generation < pairedGeneration) return false;
      if (pairedConversation?.canLeave?.() === false) return false;
      pairedGeneration = generation ?? pairedGeneration + 1;
      const hadPair = !!(pairedConversation || pairedHost);
      pairedConversation?.dispose?.(); pairedConversation = null; pairedHost = null;
      if (disposed) return true;
      // An open side chat is not a paired record; closing a record on the right leaves it in place.
      if (takeover?.kind === 'side') return true;
      endTakeover(takeover);
      // Leaving Focus with no paired record open must not disturb the live conversation: nothing needs restoring.
      if (!hadPair && pane.firstElementChild === root) return true;
      pane.replaceChildren(root); void load(true);
      return true;
    },
    // Open a side chat in this Chat area. The session matches window.focusExpansion's, plus `titleHost`.
    async openSide(spec) {
      if (!live() || typeof spec?.mount !== 'function') return null;
      if (!await leaveSide() || pairedHost || disposed) return null;
      const t = {kind: 'side', dead: false, beforeLeave: null, cleanups: []};
      let session = null;
      const shell = takeoverShell('side', spec.title || '', () => void session?.close());
      t.node = shell.node; takeover = t;
      const content = document.createElement('div'); content.className = 'gchat-side-content';
      shell.body.append(content);
      pane.replaceChildren(shell.node);
      const current = () => !t.dead && takeover === t && !disposed && shell.node.isConnected;
      session = {
        host: content, titleHost: shell.title, isCurrent: current,
        setTitle(value) { if (current()) shell.title.textContent = String(value ?? ''); },
        addHeaderAction(node) {
          if (!current()) return;
          node.classList.add('gchat-takeover-action'); shell.head.append(node);
          session.onDispose(() => node.remove());
        },
        setBeforeLeave(fn) { if (current()) t.beforeLeave = typeof fn === 'function' ? fn : null; },
        onDispose(fn) { if (typeof fn !== 'function') return; if (t.dead) { try { fn(); } catch (_) {} } else t.cleanups.push(fn); },
        async close() {
          if (!current() || !await leaveSide()) return false;
          restoreChat();
          return true;
        },
      };
      try {
        const dispose = await spec.mount(content, session);
        if (typeof dispose === 'function') session.onDispose(dispose);
      } catch (_) {
        if (current()) { endTakeover(t); restoreChat(); toast('Could not open this content.'); }
        return null;
      }
      return current() ? session : null;
    },
    dispose() {
      disposed = true; generation++;
      reads.abort();
      const pairedNode=pairedHost, takenNode=takeover?.node;
      endTakeover(takeover);
      pairedConversation?.dispose?.(); pairedConversation = null; pairedHost = null;
      closeStream(); clearTimeout(retryTimer); clearTimeout(readTimer);
      saveDraft();
      cleanups.splice(0).forEach(cleanup => cleanup());
      view.dispose();
      if (pane.firstElementChild === takenNode || pane.firstElementChild === pairedNode || pane.firstElementChild === root) pane.replaceChildren();
    },
    focus() { view.focusComposer(); },
  };
}
