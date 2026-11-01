// Persistent chat for any registered agent (global or business). It is a projection of the agent's native session
// conversation (qorqut-code docs/v1.2/NATIVE_CHAT_DTO.md): opening, switching or reading never starts, resumes, stops or types
// into an agent; Terminal owns the session lifecycle. One mount owns one SSE stream, its timers and listeners, and releases all
// of them in dispose().
// Composer state is bound to the exact agent and conversation, and outlives a mount so a switch never leaks or loses it.
const GLOBAL_CHAT_DRAFTS = new Map();           // scope -> unsent composer text
const GLOBAL_CHAT_SENDS = new Map();            // scope -> {text, key, pin}: the frozen identity of an unconfirmed send, reused by a retry
const GLOBAL_CHAT_INFLIGHT = new Map();         // scope -> {text, promise} of a send still waiting for its response
const GLOBAL_CHAT_RESENDS = new Map();          // undelivered input id -> {key, pin}: identity of an explicit resend
const GLOBAL_CHAT_HIDDEN = new Set();           // settled inputs the owner dismissed or already resent
// Exact provider + conversation identity of a captured conversation; the one key for drafts, sends, queries and the selector.
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
  failed: 'Not delivered',
  undelivered: 'Not delivered',
};
// The expanded text of an inbox nudge is system-generated and technical: the timeline shows only its compact marker.
const globalChatMarkerOnly = type => type === 'inbox_nudge';
const globalChatNoticeLabel = system => !system ? 'System notice'
  : system.verified ? GLOBAL_CHAT_INPUT_LABEL[system.input_type] || 'System notice'
  : 'Unverified notice';
const GLOBAL_CHAT_TIME = new Intl.DateTimeFormat(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
const GLOBAL_CHAT_FULL_TIME = new Intl.DateTimeFormat(undefined, {dateStyle:'full', timeStyle:'long'});
function globalChatTimestamp(value) {
  if (!value) return '';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  const full = esc(GLOBAL_CHAT_FULL_TIME.format(date));
  return `<time class="gchat-time" datetime="${date.toISOString()}" title="${full}" aria-label="${full}">${esc(GLOBAL_CHAT_TIME.format(date))}</time>`;
}
const globalChatBase = id => `/root/agents/${encodeURIComponent(id)}/chat`;

// ---- Markdown (headings, lists, quotes, tables, code, emphasis, and the shared safe links)
const globalChatInline = text => {
  let html = '';
  text.split(/(`[^`\n]+`)/g).forEach(part => {
    if (part.length > 2 && part[0] === '`' && part.endsWith('`')) { html += `<code>${esc(part.slice(1, -1))}</code>`; return; }
    // Links come from the one shared renderer; emphasis is applied only outside the anchors it produced.
    html += renderSafeLinks(part).split(/(<a class="safe-link"[^>]*>[^<]*<\/a>)/g).map((piece, index) => index % 2 ? piece : piece
      .replace(/\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])/g, '$1<em>$2</em>')).join('');
  });
  return html;
};
// Pipe tables use the same single-backtick code spans as the inline renderer. Outer pipes are optional.
function globalChatTableCells(line) {
  const cells = [];
  let cell = '', code = false, lastPipe = false;
  const text = line.trim();
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    lastPipe = false;
    if (char === '\\' && text[i + 1] === '|') { cell += '|'; i++; continue; }
    if (char === '\\' && text[i + 1] === '\\') { cell += '\\\\'; i++; continue; }
    if (char === '`') code = !code && text.indexOf('`', i + 1) > 0;
    if (char === '|' && !code) { cells.push(cell); cell = ''; lastPipe = true; continue; }
    cell += char;
  }
  cells.push(cell);
  if (text.startsWith('|')) cells.shift();
  if (lastPipe && cells.length) cells.pop();
  return cells.map(item => item.trim());
}
const globalChatTableAlign = cell => {
  const match = /^(:)?-{3,}(:)?$/.exec(cell);
  return match ? (match[1] && match[2] ? 'center' : match[2] ? 'right' : match[1] ? 'left' : '') : null;
};
function globalChatTable(head, align, rows) {
  const cell = (tag, text, i) => `<${tag}${tag === 'th' ? ' scope="col"' : ''}${align[i] ? ` class="gc-${align[i]}"` : ''}>${globalChatInline(text)}</${tag}>`;
  const row = (tag, cells) => `<tr>${align.map((_, i) => cell(tag, cells[i] ?? '', i)).join('')}</tr>`;
  return `<div class="gc-table-wrap" role="region" aria-label="Table" tabindex="0"><table><thead>${row('th', head)}</thead>${rows.length ? `<tbody>${rows.map(cells => row('td', cells)).join('')}</tbody>` : ''}</table></div>`;
}
function globalChatMarkdown(source) {
  const lines = String(source ?? '').replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let paragraph = [], list = null, quote = [];
  const flushParagraph = () => { if (paragraph.length) out.push(`<p>${paragraph.map(globalChatInline).join('<br>')}</p>`); paragraph = []; };
  const flushList = () => { if (list) out.push(`<${list.tag}>${list.items.map(item => `<li>${globalChatInline(item)}</li>`).join('')}</${list.tag}>`); list = null; };
  const flushQuote = () => { if (quote.length) out.push(`<blockquote>${quote.map(globalChatInline).join('<br>')}</blockquote>`); quote = []; };
  const flush = () => { flushParagraph(); flushList(); flushQuote(); };
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    const fence = /^\s*```\s*([\w+-]{0,20})\s*$/.exec(line);
    if (fence) {
      flush();
      const code = [];
      while (++index < lines.length && !/^\s*```\s*$/.test(lines[index])) code.push(lines[index]);
      out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
      continue;
    }
    if (!line.trim()) { flush(); continue; }
    const heading = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (heading) { flush(); out.push(`<p class="gc-heading">${globalChatInline(heading[2])}</p>`); continue; }
    const bullet = /^\s*[-*+]\s+(.+)$/.exec(line), numbered = /^\s*\d{1,3}[.)]\s+(.+)$/.exec(line);
    if (bullet || numbered) {
      const tag = bullet ? 'ul' : 'ol';
      flushParagraph(); flushQuote();
      if (list && list.tag !== tag) flushList();
      (list ||= {tag, items: []}).items.push((bullet || numbered)[1]);
      continue;
    }
    const quoted = /^\s*>\s?(.*)$/.exec(line);
    if (quoted) { flushParagraph(); flushList(); quote.push(quoted[1]); continue; }
    flushList(); flushQuote();
    if (line.includes('|') && index + 1 < lines.length && lines[index + 1].includes('|')) {
      const head = globalChatTableCells(line), marks = globalChatTableCells(lines[index + 1]).map(globalChatTableAlign);
      if (head.length === marks.length && marks.length && marks.every(mark => mark !== null)) {
        flushParagraph();
        const rows = [];
        index++;
        while (index + 1 < lines.length && lines[index + 1].trim() && lines[index + 1].includes('|') && !/^\s*```/.test(lines[index + 1])) rows.push(globalChatTableCells(lines[++index]));
        out.push(globalChatTable(head, marks, rows));
        continue;
      }
    }
    paragraph.push(line);
  }
  flush();
  return out.join('');
}

function globalChatReferenceTarget(href) {
  const safe = safeLinkUrl(href);
  if (!safe) return null;
  const url = new URL(safe, location.origin);
  const viewer = url.origin === location.origin && ['/files/view', '/tables/view'].includes(url.pathname);
  const path = viewer ? url.searchParams.get('path') || '' : url.pathname;
  const image = /\.(?:png|jpe?g|gif|webp)$/i.test(path);
  const kind = image ? 'Image' : viewer ? url.pathname === '/tables/view' ? 'Table' : 'File' : 'Web page';
  return {url, viewer, image, kind};
}

// Chips represent rendered links, never code samples. No document/image request happens until a chip is opened.
function globalChatReferenceChips(body) {
  const chips = document.createElement('div'), seen = new Set();
  chips.className = 'gchat-references';
  for (const link of body.querySelectorAll('a.safe-link')) {
    const target = globalChatReferenceTarget(link.href);
    if (!target || seen.has(target.url.href)) continue;
    seen.add(target.url.href);
    const chip = document.createElement('button');
    chip.type = 'button'; chip.className = 'gchat-reference';
    chip.dataset.previewHref = target.url.href; chip.dataset.previewTitle = link.textContent;
    chip.setAttribute('aria-label', `Preview ${link.textContent}`);
    if (typeof AGVB_ICON !== 'undefined') chip.innerHTML = AGVB_ICON[target.kind === 'Table' ? 'table' : target.viewer || target.image ? 'file' : 'web'];
    const label = document.createElement('span'); label.textContent = link.textContent;
    const kind = document.createElement('small'); kind.textContent = target.kind;
    chip.append(label, kind); chips.append(chip);
  }
  return chips.childElementCount ? chips : null;
}

// ---- the mount
function globalChatViewToggle(terminal = false) {
  return `<div class="gchat-view-toggle" role="group" aria-label="Agent view">
    <button type="button" class="quiet" data-chat-return aria-label="Chat" aria-pressed="${!terminal}" ${terminal ? '' : 'disabled'}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></button>
    <button type="button" class="quiet" data-terminal aria-label="Terminal" aria-pressed="${terminal}" ${terminal ? 'disabled' : ''}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4 17 6-6-6-6M12 19h8"/></svg></button>
  </div>`;
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
  const nodes = new Map();                       // entry id -> element
  const inputNodes = new Map();                  // input id -> element
  let session = null, conversation = null, conversations = [], inputs = [], older = null, shown, selected = null, switching = false, clears = 0;
  let cursor = 0, lastRead = 0, composerScope = null;
  let stream = null, retryTimer = null, retryDelay = 1000, readTimer = null, streamDown = false;
  let disposed = false, loading = false, again = false, generation = 0, atBottom = true, unseen = 0, finished = false, reading = 0;
  const cleanups = [];

  pane.innerHTML = `<div class="gchat" data-global-chat="${esc(agentId)}">
    <header class="gchat-top">
      <span class="gchat-name">${esc(label)}</span>
      <span class="gchat-status" role="status" aria-live="polite"></span>
    </header>
    ${options.openTerminal ? `<div class="gchat-session-switch">${globalChatViewToggle()}</div>` : ''}
    <div class="gchat-banner" role="status" hidden></div>
    <div class="gchat-conversations" hidden><select aria-label="Conversation"></select><button type="button" class="quiet" data-current-conversation>Back to current</button></div>
    <div class="gchat-scroll" tabindex="-1"><button type="button" class="quiet gchat-older" hidden>Load older messages</button>
      <div class="gchat-log" role="log" aria-live="polite" aria-label="Conversation with ${esc(label)}"></div>
      <div class="gchat-pending-list"></div>
      <div class="gchat-empty" hidden></div></div>
    <button type="button" class="gchat-jump" hidden>New messages ↓</button>
    <div class="gchat-session" role="status" aria-live="polite" data-state="loading"><span class="gchat-session-dot" aria-hidden="true"></span><span class="gchat-session-text">Loading…</span></div>
    <form class="gchat-composer">
      <textarea rows="2" disabled maxlength="${GLOBAL_CHAT_MAX}" aria-label="Message ${esc(label)}" placeholder="Message ${esc(label)}…" autocomplete="off"></textarea>
      <button type="submit" class="gchat-send" disabled>Send</button>
    </form>
    <section class="gchat-preview-window" role="dialog" aria-label="Document preview" hidden>
      <header><span class="gchat-preview-title"></span><a class="gchat-preview-open" target="_blank" rel="noopener noreferrer" aria-label="Open preview in new tab">↗</a><button type="button" class="quiet" data-preview-close aria-label="Close preview">×</button></header>
      <div class="gchat-preview-content"></div>
    </section>
  </div>`;
  const root = pane.firstElementChild;
  const $q = selector => root.querySelector(selector);
  const status = $q('.gchat-status'), banner = $q('.gchat-banner');
  const scroller = $q('.gchat-scroll'), log = $q('.gchat-log'), pendingHost = $q('.gchat-pending-list'), emptyNote = $q('.gchat-empty');
  const olderButton = $q('.gchat-older'), jump = $q('.gchat-jump'), form = $q('.gchat-composer');
  const sessionLine = $q('.gchat-session'), sessionText = $q('.gchat-session-text');
  const convBar = $q('.gchat-conversations'), convSelect = convBar.querySelector('select');
  const input = form.querySelector('textarea'), send = form.querySelector('.gchat-send');

  const live = () => !disposed && root.isConnected;
  const listen = (target, type, handler, opts) => { target.addEventListener(type, handler, opts); cleanups.push(() => target.removeEventListener(type, handler, opts)); };
  const preview = $q('.gchat-preview-window'), previewContent = $q('.gchat-preview-content');
  let previewTrigger = null;
  const clearPreview = () => {
    // Release the old embedded page before replacing, closing or leaving this agent.
    previewContent.querySelectorAll('iframe').forEach(frame => { frame.src = 'about:blank'; });
    previewContent.replaceChildren();
  };
  const closePreview = (restoreFocus = true) => {
    clearPreview(); preview.hidden = true;
    if (restoreFocus && previewTrigger?.isConnected) previewTrigger.focus({preventScroll:true});
    previewTrigger = null;
  };
  cleanups.push(() => closePreview(false));
  const openPreview = button => {
    const target = globalChatReferenceTarget(button.dataset.previewHref);
    if (!target) return;
    clearPreview(); previewTrigger = button;
    const title = button.dataset.previewTitle;
    $q('.gchat-preview-title').textContent = title;
    $q('.gchat-preview-open').href = target.url.href;
    const media = document.createElement(target.image ? 'img' : 'iframe');
    media.referrerPolicy = 'no-referrer';
    if (target.image) {
      media.alt = title; media.decoding = 'async';
      media.src = target.viewer ? `/api/root/files/image${target.url.search}` : target.url.href;
      media.addEventListener('error', () => { if (media.isConnected) previewContent.textContent = 'This image could not be displayed.'; });
    } else {
      media.setAttribute('aria-label', title + ' preview');
      // Viewbox uses these same reader shells; arbitrary websites retain its sandbox.
      if (!target.viewer) media.setAttribute('sandbox', 'allow-scripts allow-forms');
      media.src = target.url.href;
    }
    previewContent.append(media); preview.hidden = false;
    $q('[data-preview-close]').focus({preventScroll:true});
  };
  listen(preview, 'keydown', event => { if (event.key === 'Escape') { event.stopPropagation(); closePreview(); } });

  // ---- scrolling
  const nearBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 48;
  const toBottom = () => { scroller.scrollTop = scroller.scrollHeight; atBottom = true; unseen = 0; jump.hidden = true; scheduleRead(); };
  listen(scroller, 'scroll', () => {
    atBottom = nearBottom();
    if (atBottom) { unseen = 0; jump.hidden = true; scheduleRead(); }
  });
  jump.onclick = () => { toBottom(); input.focus(); };
  const afterChange = grew => {
    if (atBottom) toBottom();
    else if (grew) { unseen++; jump.hidden = false; jump.textContent = unseen > 1 ? `${unseen} new messages ↓` : 'New messages ↓'; }
  };

  // ---- the read position: the newest completed reply whose entry is rendered in this visible Chat. Only a rendered page or
  // entry counts; received events, prefetching and agent selection never do, and newest_sequence is never acknowledged.
  function renderedBoundary() {
    const edge = scroller.getBoundingClientRect().bottom;
    let newest = 0;
    for (const [id, node] of nodes) {
      const entry = entries.get(id);
      if (entry?.speaker === 'agent' && entry.completion_sequence > newest && node.getBoundingClientRect().top < edge) newest = entry.completion_sequence;
    }
    return newest;
  }
  function scheduleRead() {
    clearTimeout(readTimer);
    readTimer = setTimeout(async () => {
      if (!live() || !session || !globalChatReadable(agentId) || !atBottom) return;
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

  // ---- transcript entries (the list is the transcript; entries are keyed by their stable id and ordered by archive position)
  function paintEntry(entry) {
    let node = nodes.get(entry.id);
    const isNew = !node;
    if (isNew) { node = document.createElement('div'); nodes.set(entry.id, node); }
    node.className = `gchat-msg gchat-${entry.speaker}`;
    node.dataset.id = entry.id; node.dataset.position = String(entry.position);
    // Replayed entries keep their chips and keyboard focus.
    const signature = JSON.stringify([entry.speaker, entry.text, entry.partial, entry.timestamp, entry.system]);
    if (node.chatSignature === signature) return {node, isNew};
    node.chatSignature = signature;
    node.classList.toggle('gchat-partial', entry.partial === true);
    if (entry.speaker === 'agent') {
      node.innerHTML = `<div class="gchat-body md">${globalChatMarkdown(entry.text)}</div>`;
      if (entry.partial !== true) {
        const chips = globalChatReferenceChips(node.querySelector('.gchat-body'));
        if (chips) node.append(chips);
      }
    } else if (entry.speaker === 'notice') {
      const system = entry.system, verified = !!system?.verified;
      node.dataset.verified = String(verified);
      if (system?.input_type) node.dataset.inputType = system.input_type; else delete node.dataset.inputType;
      const markerOnly = verified && globalChatMarkerOnly(system.input_type);
      node.innerHTML = `<div class="gchat-body"><span class="gchat-marker">${esc(globalChatNoticeLabel(system))}</span>${markerOnly ? '' : `<span class="gchat-text">${esc(entry.text)}</span>`}</div>`;
    } else {
      node.innerHTML = `<div class="gchat-body"><span class="gchat-text">${esc(entry.text)}</span></div>`;
    }
    node.insertAdjacentHTML('beforeend', globalChatTimestamp(entry.timestamp));
    return {node, isNew};
  }
  // Returns true when the entry was added after every existing one.
  function placeEntry(entry) {
    const previous = entries.get(entry.id);
    entries.set(entry.id, entry);
    const {node, isNew} = paintEntry(entry);
    if (!isNew) return false;
    const after = [...log.children].find(child => Number(child.dataset.position) > entry.position);
    log.insertBefore(node, after || null);
    return !after && !previous;
  }
  const newestPosition = () => { let top = -1; for (const entry of entries.values()) top = Math.max(top, entry.position); return top; };
  function clearEntries() {
    clears++; entries.clear(); nodes.clear(); log.replaceChildren();
    pendingHost.replaceChildren(); inputNodes.clear();
  }

  // ---- application inputs not yet confirmed by the transcript
  const viewedKey = () => globalChatKey(conversation);
  // The live session's exact binding; a conversation is sendable only when it is the one displayed.
  const liveBinding = () => session?.binding || null;
  const viewingLive = () => !!liveBinding() && globalChatKey(liveBinding()) === viewedKey();
  const defaultConversation = () => conversations.find(item => item.current) || null;
  // A receipt belongs to the exact provider/conversation of its own binding; one without a binding is shown nowhere.
  function inputVisible(item, noticeIds) {
    if (item.state === 'submitted' || GLOBAL_CHAT_HIDDEN.has(item.id) || noticeIds.has(item.id)) return false;
    return !!viewedKey() && globalChatKey(item.binding) === viewedKey();
  }
  // Only a definitely undelivered owner Chat message can be offered again; every other producer's receipt is status only.
  const ownerChat = item => item.origin === 'owner' && item.input_type === 'chat';
  function paintInputs() {
    // A verified notice entry already is its input's receipt: never show the same input a second time.
    const noticeIds = new Set([...entries.values()].map(entry => entry.system?.input_id).filter(Boolean));
    const shownInputs = inputs.filter(item => inputVisible(item, noticeIds)).sort((a, b) => a.sequence - b.sequence);
    const keep = new Set(shownInputs.map(item => item.id));
    for (const [id, node] of inputNodes) if (!keep.has(id)) { node.remove(); inputNodes.delete(id); }
    for (const item of shownInputs) {
      let node = inputNodes.get(item.id);
      if (!node) { node = document.createElement('div'); inputNodes.set(item.id, node); }
      const system = item.origin === 'system', kind = GLOBAL_CHAT_INPUT_LABEL[item.input_type] || (system ? 'System notice' : '');
      node.className = `gchat-msg gchat-pending ${system ? 'gchat-notice' : 'gchat-user'}`;
      node.dataset.inputId = item.id; node.dataset.state = item.state; node.dataset.inputType = item.input_type;
      const definite = ownerChat(item) && ['failed', 'undelivered'].includes(item.state) && !!item.body;
      const resendable = definite && viewingLive(), reusable = definite && !viewingLive() && !!defaultConversation();
      const dismissable = ['uncertain', 'failed', 'undelivered'].includes(item.state);
      const signature = JSON.stringify([item.state, item.body, kind, resendable, reusable, dismissable, canSend()]);
      node.dataset.resendable = String(resendable);
      if (node.chatSignature !== signature) {
        node.chatSignature = signature;
        const text = globalChatMarkerOnly(item.input_type) ? '' : item.body ?? (item.input_type === 'telegram' ? 'Private message' : '');
        node.innerHTML = `<div class="gchat-body">${kind ? `<span class="gchat-marker">${esc(kind)}</span>` : ''}${text ? `<span class="gchat-text">${esc(text)}</span>` : ''}</div>
          <span class="gchat-state" data-state="${esc(item.state)}">${esc(GLOBAL_CHAT_INPUT_STATE[item.state])}</span>
          ${resendable || reusable || dismissable ? `<span class="gchat-pending-actions">${resendable ? `<button type="button" class="quiet" data-resend="${esc(item.id)}" ${canSend() ? '' : 'disabled'}>Resend</button>` : ''}${reusable ? `<button type="button" class="quiet" data-reuse="${esc(item.id)}">Use in current chat</button>` : ''}${dismissable ? `<button type="button" class="quiet" data-dismiss="${esc(item.id)}">Dismiss</button>` : ''}</span>` : ''}`;
      }
    }
    shownInputs.forEach((item, index) => {                                                   // chronological, without re-inserting settled nodes
      const node = inputNodes.get(item.id);
      if (pendingHost.children[index] !== node) pendingHost.insertBefore(node, pendingHost.children[index] || null);
    });
    paintEmpty();
  }

  // ---- session status, composer enablement and the empty state
  const canSend = () => !!session?.can_send && viewingLive() && !switching && !finished;
  function sessionSummary() {
    if (!session) return {key: 'loading', short: 'Loading…', line: ''};
    if (switching) return {key: 'loading', short: 'Loading…', line: ''};
    if (selected || (session.state === 'live' && session.can_send && !viewingLive())) return {key: 'archive', short: 'Earlier conversation', line: 'Messages go only to the live session.'};
    if (session.state === 'live') {
      if (session.reason === 'capture_disabled') return {key: 'disabled', short: 'Chat unavailable', line: ''};
      if (session.reason === 'binding_not_ready') return {key: 'starting', short: 'Connecting', line: ''};
      if (session.reason === 'capture_not_ready') return {key: 'starting', short: 'Connecting', line: ''};
      return session.working ? {key: 'working', short: 'Working', line: ''} : {key: 'live', short: 'Live', line: ''};
    }
    const direction = `Start or resume ${label} in Terminal to send.`;
    if (session.reason === 'capture_disabled') return {key: 'disabled', short: 'Chat unavailable', line: ''};
    if (session.reason === 'unsupported_runtime') return {key: 'offline', short: 'Chat unavailable', line: ''};
    return {key: 'offline', short: 'Offline', line: direction};
  }
  function paintEmpty() {
    const empty = !nodes.size && !inputNodes.size && !!session;
    emptyNote.hidden = !empty;
    if (!empty) return;
    const key = sessionSummary().key;
    emptyNote.innerHTML = key === 'live' || key === 'working' ? '<p>Your agent conversation appears here.</p>'
      : key === 'starting' ? '<p>Connecting…</p>'
      : key === 'disabled' ? '<p>Chat unavailable.</p>'
      : `<p>Start or resume ${esc(label)} in Terminal. Its conversation will appear here.</p>${options.openTerminal ? '<button type="button" class="quiet" data-terminal>Open terminal</button>' : ''}`;
  }
  function paintSession() {
    const summary = sessionSummary(), working = summary.key === 'working' && !streamDown;
    sessionLine.dataset.state = streamDown && summary.key === 'working' ? 'live' : summary.key;
    sessionLine.classList.toggle('gchat-session-working', working);
    sessionText.textContent = finished ? 'This chat is no longer available.' : summary.line;
    sessionLine.hidden = !sessionText.textContent;
    status.textContent = finished ? 'Unavailable' : summary.short;
    status.dataset.state = !session ? 'loading' : ['live', 'working'].includes(summary.key) ? 'ready' : 'stopped';
    const enabled = canSend(), sending = GLOBAL_CHAT_INFLIGHT.has(composerScope);
    input.disabled = !enabled;
    send.disabled = !enabled || sending;
    input.placeholder = enabled ? `Message ${label}…` : !session ? `Message ${label}…` : (selected || !viewingLive()) ? 'Not the live conversation: nothing can be sent here' : 'Not available to send';
    paintEmpty();
    for (const button of pendingHost.querySelectorAll('[data-resend]')) button.disabled = !session?.can_send || GLOBAL_CHAT_RESENDS.get(button.dataset.resend)?.pending === true;
  }
  const showBanner = text => { banner.hidden = !text; banner.textContent = text || ''; delete banner.dataset.reconnect; };
  function paintConversations() {
    convBar.hidden = conversations.length < 2;
    const chosen = selected ? globalChatKey(selected) : globalChatKey(defaultConversation());
    convSelect.innerHTML = conversations.map(item => {
      const when = item.updated_at ? ` · ${GLOBAL_CHAT_TIME.format(new Date(item.updated_at))}` : '';
      return `<option value="${esc(globalChatKey(item))}"${globalChatKey(item) === chosen ? ' selected' : ''}>${esc(`${item.provider || 'Conversation'}${when}${item.current ? ' (current)' : ''}`)}</option>`;
    }).join('');
    convBar.querySelector('[data-current-conversation]').hidden = !selected;
  }
  listen(root, 'click', event => {
    const chip = event.target.closest('[data-preview-href]');
    if (chip) { openPreview(chip); return; }
    if (event.target.closest('[data-preview-close]')) { closePreview(); return; }
    if (event.target.closest('[data-terminal]')) { options.openTerminal?.(); return; }
    if (event.target.closest('[data-current-conversation]')) { chooseConversation(null); return; }
    const resend = event.target.closest('[data-resend]'), dismiss = event.target.closest('[data-dismiss]'), reuse = event.target.closest('[data-reuse]');
    if (resend) void resendInput(resend.dataset.resend);
    else if (reuse) useInCurrentChat(reuse.dataset.reuse);
    else if (dismiss) { GLOBAL_CHAT_HIDDEN.add(dismiss.dataset.dismiss); paintInputs(); }
  });
  listen(convSelect, 'change', () => chooseConversation(convSelect.value));
  function chooseConversation(key) {
    const item = key ? conversations.find(row => globalChatKey(row) === key) : null;
    const next = item && !item.current ? {provider: item.provider, conversation_id: item.conversation_id} : null;
    if (globalChatKey(next) === globalChatKey(selected)) { paintConversations(); return; }
    selected = next; generation++; loading = false; again = false;
    switching = true;                                   // until a successful state for this selection arrives
    invalidatePage();
    void load();
  }
  // Drops the displayed page and its session so nothing stale can be read, sent or acknowledged until a fresh successful state
  // arrives. The unsent draft stays in its conversation-specific store.
  function invalidatePage() {
    saveDraft(); composerScope = null; input.value = '';
    reading = 0; clearTimeout(readTimer);
    session = null; conversation = null; inputs = []; older = null; shown = undefined;
    clearEntries(); olderButton.hidden = true;
    paintInputs(); paintSession();
  }

  // ---- composer: the draft and the unconfirmed send are bound to this exact agent, provider and conversation
  function bindComposer() {
    const next = globalChatScope(agentId, conversation);
    if (next === composerScope) return;
    composerScope = next;
    input.value = next ? GLOBAL_CHAT_DRAFTS.get(next) || '' : '';
    if (next) watchInflight(next);
  }
  // A send started from an earlier mount of this composer finishes on its own; this mount only shows its outcome.
  function watchInflight(key) {
    const earlier = GLOBAL_CHAT_INFLIGHT.get(key);
    earlier?.promise.finally(() => {
      if (!live()) return;
      if (composerScope === key && input.value === earlier.text && GLOBAL_CHAT_DRAFTS.get(key) !== earlier.text) input.value = '';
      paintSession(); void load();
    });
  }
  const saveDraft = () => { if (!composerScope) return; if (input.value) GLOBAL_CHAT_DRAFTS.set(composerScope, input.value); else GLOBAL_CHAT_DRAFTS.delete(composerScope); };
  input.oninput = () => {
    saveDraft();
    const pending = GLOBAL_CHAT_SENDS.get(composerScope);
    if (pending && pending.text !== input.value && !GLOBAL_CHAT_INFLIGHT.has(composerScope)) GLOBAL_CHAT_SENDS.delete(composerScope);
  };
  input.onkeydown = event => {
    // Enter sends, Shift+Enter inserts a newline, and a composition in progress (IME) is never interrupted.
    if (event.key === 'Enter' && !event.isComposing && (!event.shiftKey || event.ctrlKey || event.metaKey)) {
      event.preventDefault(); form.requestSubmit();
    }
  };
  const refusalText = problem => GLOBAL_CHAT_REFUSAL_TEXT[problem.code] || "Couldn't send. Try again";
  // One admission request. The result is recorded in module state so a mount that was left meanwhile loses nothing.
  const admit = async (text, key, pin) => coreData(await post(`${base}/messages`, {text, idempotency_key: key, expected_binding: pin}));
  function takeInput(item) {
    if (!item || !live()) return;
    inputs = [item, ...inputs.filter(old => old.id !== item.id)];
    paintInputs(); afterChange(true);
  }
  form.onsubmit = async event => {
    event.preventDefault();
    const text = input.value, mine = composerScope;
    if (!mine || !text.trim() || !canSend() || GLOBAL_CHAT_INFLIGHT.has(mine)) return;
    // A retry of the same text reuses its key and its original binding pin, so a lost response can never become a second
    // message or move to another session. Only a deliberate new send takes the binding observed now.
    let attempt = GLOBAL_CHAT_SENDS.get(mine);
    if (!attempt || attempt.text !== text) attempt = {text, key: newRequestId(), pin: liveBinding().binding_id};
    GLOBAL_CHAT_SENDS.set(mine, attempt);
    const run = (async () => {
      try {
        const result = await admit(text, attempt.key, attempt.pin);
        if (GLOBAL_CHAT_SENDS.get(mine) === attempt) GLOBAL_CHAT_SENDS.delete(mine);
        if (GLOBAL_CHAT_DRAFTS.get(mine) === text) GLOBAL_CHAT_DRAFTS.delete(mine);
        if (live() && composerScope === mine) { if (input.value === text) input.value = ''; showBanner(''); }
        takeInput(result.input);
        if (live()) { atBottom = true; toBottom(); void load(); }
      } catch (problem) {
        if (GLOBAL_CHAT_REFUSALS.has(problem.code)) { if (GLOBAL_CHAT_SENDS.get(mine) === attempt) GLOBAL_CHAT_SENDS.delete(mine); }
        if (live() && composerScope === mine) showBanner(GLOBAL_CHAT_REFUSALS.has(problem.code) ? refusalText(problem)
          : "Couldn't confirm. Try again");
        if (problem.code === 'capture_not_ready' || problem.code === 'terminal_offline' || problem.code === 'terminal_changed') void load();
      } finally {
        GLOBAL_CHAT_INFLIGHT.delete(mine);
        if (live()) paintSession();
      }
    })();
    GLOBAL_CHAT_INFLIGHT.set(mine, {text, promise: run});
    paintSession();
    await run;
  };
  // A definitely undelivered input is resent only when the owner asks; the new send has its own key, stable across a retry.
  async function resendInput(id) {
    const item = inputs.find(row => row.id === id);
    if (!item?.body || !ownerChat(item) || !['failed', 'undelivered'].includes(item.state) || !canSend() || !viewingLive()) return;
    const resend = GLOBAL_CHAT_RESENDS.get(id) || {key: newRequestId(), pin: liveBinding().binding_id};
    resend.pending = true; GLOBAL_CHAT_RESENDS.set(id, resend);
    paintSession();
    try {
      const result = await admit(item.body, resend.key, resend.pin);
      GLOBAL_CHAT_RESENDS.delete(id); GLOBAL_CHAT_HIDDEN.add(id);
      showBanner(''); takeInput(result.input); void load();
    } catch (problem) {
      resend.pending = false;
      if (GLOBAL_CHAT_REFUSALS.has(problem.code)) GLOBAL_CHAT_RESENDS.delete(id);
      if (live()) showBanner(GLOBAL_CHAT_REFUSALS.has(problem.code) ? refusalText(problem) : "Couldn't confirm. Try again");
    } finally { if (live()) paintSession(); }
  }

  // From an archive the undelivered text is only copied into the current conversation's draft; sending stays a separate action.
  function useInCurrentChat(id) {
    const item = inputs.find(row => row.id === id), target = defaultConversation();
    if (!item?.body || !ownerChat(item) || !['failed', 'undelivered'].includes(item.state) || !target) return;
    const key = globalChatScope(agentId, target), existing = GLOBAL_CHAT_DRAFTS.get(key);
    GLOBAL_CHAT_DRAFTS.set(key, existing && !existing.includes(item.body) ? `${existing}\n\n${item.body}` : existing || item.body);
    showBanner('Copied to the current conversation’s draft. Switch back to review and send it.');
  }

  // ---- loading: a state fetch is idempotent, so every "changed" event just reads it again (coalesced)
  const stateUrl = () => `${base}?limit=${GLOBAL_CHAT_PAGE}${globalChatQuery(selected)}`;
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
      if (fresh) { clearEntries(); older = data.older; }
      shown = id; session = data.session; conversation = data.conversation; conversations = data.conversations || [];
      inputs = data.inputs || []; lastRead = Math.max(fresh && reset ? 0 : lastRead, data.last_read_sequence || 0);
      cursor = fresh && reset ? data.cursor || 0 : Math.max(cursor, data.cursor || 0);
      let grew = false;
      page.forEach(entry => { if (placeEntry(entry) && entry.speaker === 'agent') grew = true; });
      switching = false; bindComposer();
      olderButton.hidden = older == null;
      paintConversations(); paintInputs(); paintSession();
      if (fresh) { showBanner(''); toBottom(); } else afterChange(grew);
      if (!stream) connect();
    } catch (problem) {
      if (live() && mine === generation) { paintSession(); showBanner("Couldn't load. Retry"); status.textContent = 'Unavailable'; retry(() => load()); }
    } finally {
      if (mine === generation) { loading = false; if (again && live()) { again = false; void load(); } }
    }
  }
  olderButton.onclick = async () => {
    // Only history responses advance this cursor, so no page is skipped.
    const before = older, mine = generation, requested = viewedKey(), epoch = clears, target = conversation;
    if (before == null || !target) return;
    olderButton.disabled = true;
    try {
      const data = coreData(await api(`${base}/history?before=${before}&limit=${GLOBAL_CHAT_PAGE}${globalChatQuery(target)}`));
      // Applied only to the exact provider/conversation it asked for, even if the live conversation changed on its own.
      if (!live() || mine !== generation || epoch !== clears || viewedKey() !== requested) return;
      const height = scroller.scrollHeight, top = scroller.scrollTop;
      (data.entries || []).forEach(entry => { if (!entries.has(entry.id)) placeEntry(entry); });
      older = data.older ?? null; olderButton.hidden = older == null;
      scroller.scrollTop = top + (scroller.scrollHeight - height);          // keep the reader's place
      atBottom = nearBottom();
      paintInputs();
    } catch (problem) { if (live()) showBanner("Couldn't load. Retry"); }
    finally { if (live()) olderButton.disabled = false; }
  };

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
      if (!banner.hidden && banner.dataset.reconnect) { showBanner(''); void load(); }
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
      showBanner('Reconnecting…'); banner.dataset.reconnect = '1';
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
      input.oninput = input.onkeydown = form.onsubmit = olderButton.onclick = jump.onclick = null;
      if (pane.firstElementChild === root) pane.innerHTML = '';
    },
    focus() { input.focus(); },
  };
}
