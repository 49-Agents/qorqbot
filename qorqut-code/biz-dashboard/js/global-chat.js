// Persistent chat for actual global agents (biz-core docs/GLOBAL-AGENT-CHAT.md). It replaces the terminal pane for those
// agents only; Terminal owns the native session and this view displays its saved conversation.
// One mount owns one SSE stream, its timers and listeners, and releases all of them in dispose().
const GLOBAL_CHAT_DRAFTS = new Map();           // agent id -> unsent composer text, kept while switching agents
const GLOBAL_CHAT_SENDS = new Map();            // retain retry identity across mounts
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
const GLOBAL_CHAT_STATE_TEXT = {
  queued: 'Queued', running: 'Sent to terminal', failed: 'Failed', interrupted: 'Stopped',
  uncertain: 'Delivery not confirmed. It was not sent again; check the agent before resending.',
};
// Internal notices (e.g. <task-notification>) are saved as chat turns; a message that starts with an opening tag is never shown.
// Same rule as bazarlyk/global_chat/store.py internal_notification.
const globalChatHidden = message => /^[ \t\n\r\v\f]*<[A-Za-z][\s\S]*>/.test(message.body || '');
// Native time controls presentation; sequence remains the delivery/read/event identity.
function globalChatOrder(a, b) {
  const time = Date.parse(a.created_at) - Date.parse(b.created_at);
  return (Number.isFinite(time) && time) || a.sequence - b.sequence;
}
const globalChatBase = id => `/root/agents/${encodeURIComponent(id)}/chat`;

// ---- Markdown (headings, lists, quotes, fenced and inline code, bold, italic, and the shared safe links)
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
    paragraph.push(line);
  }
  flush();
  return out.join('');
}

// ---- the mount
function globalChatViewToggle(terminal = false) {
  return `<div class="gchat-view-toggle" role="group" aria-label="Agent view">
    <button type="button" class="quiet" data-chat-return aria-pressed="${!terminal}" ${terminal ? '' : 'disabled'}>Chat</button>
    <button type="button" class="quiet" data-terminal aria-pressed="${terminal}" ${terminal ? 'disabled' : ''}>Terminal</button>
  </div>`;
}

function mountGlobalChat(pane, agent, options = {}) {
  const agentId = agent.agent_id, base = globalChatBase(agentId), label = agent.display_name || agent.name || 'Agent';
  const messages = new Map();                    // id -> message view
  const nodes = new Map();                       // id -> element
  let runtime = null, actions = [], older = false, historyBefore = null, cursor = 0, lastRead = 0;
  let stream = null, retryTimer = null, retryDelay = 1000, flushTimer = null, readTimer = null;
  let disposed = false, loading = false, generation = 0, atBottom = true, unseen = 0, sending = false, pendingSend = GLOBAL_CHAT_SENDS.get(agentId) || null;
  const cleanups = [];

  pane.innerHTML = `<div class="gchat" data-global-chat="${esc(agentId)}">
    <header class="gchat-top">
      <span class="gchat-name">${esc(label)}</span>
      <span class="gchat-status" role="status" aria-live="polite"></span>
    </header>
    ${options.openTerminal ? `<div class="gchat-session-switch">${globalChatViewToggle()}</div>` : ''}
    <div class="gchat-banner" role="status" hidden></div>
    <div class="gchat-scroll" tabindex="-1"><button type="button" class="quiet gchat-older" hidden>Load older messages</button>
      <div class="gchat-log" role="log" aria-live="polite" aria-label="Conversation with ${esc(label)}"></div>
      <div class="gchat-actions"></div>
      <div class="gchat-empty" hidden></div></div>
    <button type="button" class="gchat-jump" hidden>New messages ↓</button>
    <form class="gchat-composer">
      <textarea rows="2" maxlength="${GLOBAL_CHAT_MAX}" aria-label="Message ${esc(label)}" placeholder="Message ${esc(label)}…" autocomplete="off"></textarea>
      <button type="submit" class="gchat-send">Send</button>
    </form>
  </div>`;
  const root = pane.firstElementChild;
  const $q = selector => root.querySelector(selector);
  const status = $q('.gchat-status'), banner = $q('.gchat-banner');
  const scroller = $q('.gchat-scroll'), log = $q('.gchat-log'), actionHost = $q('.gchat-actions'), emptyNote = $q('.gchat-empty');
  const olderButton = $q('.gchat-older'), jump = $q('.gchat-jump'), form = $q('.gchat-composer');
  const input = form.querySelector('textarea'), send = form.querySelector('.gchat-send');
  input.value = GLOBAL_CHAT_DRAFTS.get(agentId) || '';

  const live = () => !disposed && root.isConnected;
  const listen = (target, type, handler, opts) => { target.addEventListener(type, handler, opts); cleanups.push(() => target.removeEventListener(type, handler, opts)); };
  const failure = error => (error && (error.message || error)) || 'The request failed.';

  // ---- scrolling
  const nearBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 48;
  const toBottom = () => { scroller.scrollTop = scroller.scrollHeight; atBottom = true; unseen = 0; jump.hidden = true; scheduleRead(); };
  listen(scroller, 'scroll', () => {
    atBottom = nearBottom();
    if (atBottom) { unseen = 0; jump.hidden = true; scheduleRead(); }
  });
  jump.onclick = () => { toBottom(); input.focus(); };
  const afterChange = (grew) => {
    if (atBottom) toBottom();
    else if (grew) { unseen++; jump.hidden = false; jump.textContent = unseen > 1 ? `${unseen} new messages ↓` : 'New messages ↓'; }
  };

  // ---- the read position: the newest agent message actually shown, only while the tab is visible and scrolled to the end
  function newestAgentSequence() {
    let newest = 0;
    for (const message of messages.values()) if (message.role === 'agent' && message.state === 'completed' && message.body && !globalChatHidden(message)) newest = Math.max(newest, message.sequence);
    return newest;
  }
  function scheduleRead() {
    clearTimeout(readTimer);
    readTimer = setTimeout(async () => {
      const sequence = newestAgentSequence();
      if (!live() || !globalChatReadable(agentId) || !atBottom || sequence <= lastRead) return;
      lastRead = sequence;
      try { coreData(await post(`${base}/read`, {sequence})); if (typeof replyNotifications !== 'undefined') void replyNotifications.refresh(); }
      catch (_) { lastRead = Math.min(lastRead, sequence - 1); }
    }, 400);
  }
  listen(document, 'visibilitychange', () => { if (!document.hidden) scheduleRead(); });
  const visibilityObserver = new MutationObserver(scheduleRead);
  visibilityObserver.observe(document.body, {attributes:true, childList:true, subtree:true, attributeFilter:['inert','hidden','aria-hidden']});
  cleanups.push(() => visibilityObserver.disconnect());

  // ---- messages
  const stateLine = message => {
    if (message.role !== 'owner' && message.role !== 'notice') return '';
    if (message.state === 'completed') return '';
    const detail = message.error?.message && message.state !== 'uncertain' ? ` ${message.error.message}` : '';
    return `<span class="gchat-state" data-state="${esc(message.state)}">${esc((GLOBAL_CHAT_STATE_TEXT[message.state] || message.state) + detail)}</span>`;
  };
  function paintMessage(message) {
    if (globalChatHidden(message)) {      // also covers a streamed reply whose text turns out to start with the tag
      nodes.get(message.id)?.remove(); nodes.delete(message.id);
      return {node: null, isNew: false};
    }
    let node = nodes.get(message.id);
    const isNew = !node;
    if (isNew) { node = document.createElement('div'); nodes.set(message.id, node); }
    node.className = `gchat-msg gchat-${message.role}`;
    node.dataset.id = message.id; node.dataset.state = message.state; node.dataset.sequence = String(message.sequence);
    const origin = message.origin && !['chat', 'system'].includes(message.origin) ? `<span class="gchat-origin">${esc(message.origin)}</span>` : '';
    if (message.role === 'agent') {
      const working = message.state === 'running';
      const tail = message.state === 'interrupted' ? '<span class="gchat-state" data-state="interrupted">Stopped</span>'
        : message.state === 'failed' ? `<span class="gchat-state" data-state="failed">${esc(message.error?.message || 'The agent reported an error.')}</span>` : '';
      node.innerHTML = `<div class="gchat-body md">${message.body ? globalChatMarkdown(message.body) : ''}${working && !message.body ? '<span class="gchat-typing" aria-label="Working">…</span>' : ''}</div>${tail}`;
      node.classList.toggle('gchat-streaming', working);
    } else {
      node.innerHTML = `<div class="gchat-body">${origin}<span class="gchat-text">${esc(message.body)}</span></div>${stateLine(message)}`;
    }
    return {node, isNew};
  }
  function place(message) {
    const {node, isNew} = paintMessage(message);
    if (!isNew) return false;
    // Backfilled native messages belong at their original time, including live SSE imports.
    const after = [...log.children].find(child => globalChatOrder(messages.get(child.dataset.id), message) > 0);
    log.insertBefore(node, after || null);
    return !after;
  }
  function upsert(message) {
    const previous = messages.get(message.id);
    if (previous && previous.state === 'completed' && message.state !== 'completed') return;   // a replayed older state never regresses
    messages.set(message.id, {...previous, ...message});
    const appended = place(messages.get(message.id));
    if (previous) paintMessage(messages.get(message.id));
    emptyNote.hidden = nodes.size > 0 || !runtime;
    afterChange(appended && message.role === 'agent');
    if (message.role === 'agent' && message.state === 'completed') scheduleRead();
  }
  function delta(data) {
    const message = messages.get(data.message_id);
    if (!message) { void loadState(); return; }
    if (message.state === 'completed') return;
    message.body += data.text;
    // Coalesce rendering of a fast stream into one paint per frame.
    if (!flushTimer) flushTimer = setTimeout(() => { flushTimer = null; if (live()) { for (const m of messages.values()) if (m.state === 'running' && m.role === 'agent') paintMessage(m); afterChange(false); } }, 60);
  }

  // ---- pending permission and question cards
  function paintActions() {
    actionHost.innerHTML = actions.map(action => {
      const request = action.request || {};
      if (action.kind === 'permission') {
        return `<section class="gchat-card" data-action="${esc(action.id)}" aria-label="Permission request">
          <h4>${esc(request.title || `${label} asks permission`)}</h4>${request.detail ? `<p>${esc(request.detail)}</p>` : ''}
          ${request.blocked_path ? `<p class="muted">${esc(request.blocked_path)}</p>` : ''}
          <div class="gchat-card-buttons"><button type="button" data-answer="allow">Allow</button><button type="button" class="quiet" data-answer="deny">Deny</button></div>
          <p class="gchat-card-error" role="alert" hidden></p></section>`;
      }
      return `<section class="gchat-card" data-action="${esc(action.id)}" aria-label="Question from ${esc(label)}"><form>${(request.questions || []).map(question => {
        const name = esc(question.id), multi = !!question.multi_select, type = multi ? 'checkbox' : 'radio';
        return `<fieldset data-question="${name}"><legend>${esc(question.header ? `${question.header}: ` : '')}${esc(question.question)}</legend>
          ${(question.options || []).map((option, index) => `<label><input type="${type}" name="q-${esc(action.id)}-${name}" value="${esc(option.label)}"> <span>${esc(option.label)}</span>${option.description ? `<small> ${esc(option.description)}</small>` : ''}</label>`).join('')}
          ${question.allow_other !== false ? `<label>Other <input type="${question.secret ? 'password' : 'text'}" autocomplete="off" data-other maxlength="2000" aria-label="Other answer for ${esc(question.question)}"></label>` : ''}</fieldset>`;
      }).join('')}<div class="gchat-card-buttons"><button type="submit">Answer</button></div><p class="gchat-card-error" role="alert" hidden></p></form></section>`;
    }).join('');
    if (actions.length) afterChange(true);
  }
  async function answer(card, response) {
    const error = card.querySelector('.gchat-card-error');
    card.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      coreData(await post(`${base}/actions/${encodeURIComponent(card.dataset.action)}`, {response}));
      actions = actions.filter(action => action.id !== card.dataset.action);
      paintActions();
    } catch (problem) {
      if (!live()) return;
      error.hidden = false; error.textContent = failure(problem);
      card.querySelectorAll('button').forEach(button => { button.disabled = false; });
      if (problem.code === 'stale_action') { actions = actions.filter(action => action.id !== card.dataset.action); paintActions(); }
    }
  }
  listen(actionHost, 'click', event => {
    const button = event.target.closest('[data-answer]');
    const card = button && button.closest('[data-action]');
    if (card) answer(card, {decision: button.dataset.answer});
  });
  listen(actionHost, 'submit', event => {
    event.preventDefault();
    const card = event.target.closest('[data-action]');
    if (!card) return;
    const answers = {};
    card.querySelectorAll('fieldset[data-question]').forEach(set => {
      const picked = [...set.querySelectorAll('input:checked')].map(box => box.value);
      const other = set.querySelector('[data-other]')?.value.trim();
      if (other) picked.push(other);
      if (picked.length) answers[set.dataset.question] = picked;
    });
    answer(card, {answers});
  });

  // Terminal owns lifecycle, configuration and native approval prompts.
  function paintRuntime() {
    const connected = !!runtime?.can_send;
    status.textContent = !runtime ? 'Loading…' : connected ? 'Terminal connected' : 'Terminal offline';
    status.dataset.state = !runtime ? 'loading' : connected ? 'ready' : 'stopped';
    emptyNote.hidden = nodes.size > 0 || !runtime;
    if (!nodes.size && runtime) {
      emptyNote.innerHTML = connected ? '<p>Your terminal conversation appears here.</p>'
        : `<p>Start or resume ${esc(label)} in Terminal. Its conversation will appear here.</p>${options.openTerminal ? '<button type="button" class="quiet" data-terminal>Open terminal</button>' : ''}`;
    }
    input.disabled = !connected;
    send.disabled = !connected || sending;
    input.placeholder = connected ? `Message ${label}…` : 'Start the agent in Terminal';
  }
  const showBanner = text => { banner.hidden = !text; banner.textContent = text || ''; delete banner.dataset.reconnect; };
  listen(root, 'click', event => {
    if (event.target.closest('[data-terminal]')) options.openTerminal?.();
  });

  // ---- composer
  const saveDraft = () => { if (input.value) GLOBAL_CHAT_DRAFTS.set(agentId, input.value); else GLOBAL_CHAT_DRAFTS.delete(agentId); };
  input.oninput = () => { saveDraft(); if (pendingSend && pendingSend.text !== input.value) { pendingSend = null; GLOBAL_CHAT_SENDS.delete(agentId); } };
  input.onkeydown = event => {
    // Enter sends, Shift+Enter inserts a newline, and a composition in progress (IME) is never interrupted.
    if (event.key === 'Enter' && !event.isComposing && (!event.shiftKey || event.ctrlKey || event.metaKey)) {
      event.preventDefault(); form.requestSubmit();
    }
  };
  form.onsubmit = async event => {
    event.preventDefault();
    const text = input.value;
    if (sending || !text.trim() || !runtime?.can_send) return;
    sending = true; send.disabled = true;
    // A retry of the same text reuses its key, so a lost response can never become a second message.
    if (!pendingSend || pendingSend.text !== text) pendingSend = {text, key: newRequestId()};
    GLOBAL_CHAT_SENDS.set(agentId, pendingSend);
    const attempt = pendingSend;
    try {
      const result = coreData(await post(`${base}/messages`, {text, idempotency_key: attempt.key}));
      if (GLOBAL_CHAT_SENDS.get(agentId) === attempt) {
        GLOBAL_CHAT_SENDS.delete(agentId);
        if (GLOBAL_CHAT_DRAFTS.get(agentId) === text) GLOBAL_CHAT_DRAFTS.delete(agentId);
      }
      if (!live()) return;
      if (pendingSend === attempt) { pendingSend = null; if (input.value === text) { input.value = ''; saveDraft(); } }
      showBanner('');
      if (result.message) { upsert(result.message); atBottom = true; toBottom(); }
      void refreshRuntime();
    } catch (problem) {
      if (live()) showBanner(['transition_required', 'terminal_offline', 'terminal_not_ready', 'terminal_unavailable'].includes(problem.code) ? failure(problem) : `Delivery unconfirmed: ${failure(problem)} Press Send to check or retry this same message.`);
    } finally { sending = false; if (live()) paintRuntime(); }
  };

  // ---- loading
  async function refreshRuntime() {
    try { const data = coreData(await api(base)); if (live()) { runtime = data.runtime; actions = data.actions || []; paintRuntime(); paintActions(); } } catch (_) { /* the stream keeps it current */ }
  }
  async function loadState() {
    if (loading) return;
    loading = true;
    const mine = ++generation;
    try {
      const data = coreData(await api(base));
      if (!live() || mine !== generation) return;
      messages.clear(); nodes.clear(); log.innerHTML = '';
      runtime = data.runtime; older = !!data.older; cursor = data.cursor || 0; lastRead = data.last_read_sequence || 0; actions = data.actions || [];
      historyBefore = data.messages[0]?.sequence ?? null;
      data.messages.forEach(message => { messages.set(message.id, message); place(message); });
      olderButton.hidden = !older;
      paintRuntime(); paintActions(); showBanner('');
      toBottom();
      connect();
    } catch (problem) {
      if (live()) { showBanner(`Chat is unavailable: ${failure(problem)}`); status.textContent = 'Unavailable'; retry(loadState); }
    } finally { loading = false; }
  }
  olderButton.onclick = async () => {
    // Only history responses advance this cursor. An old SSE backfill must not skip intervening pages.
    const first = historyBefore;
    if (first == null) return;
    olderButton.disabled = true;
    try {
      const data = coreData(await api(`${base}/history?before=${first}&limit=${GLOBAL_CHAT_PAGE}`));
      if (!live()) return;
      const height = scroller.scrollHeight, top = scroller.scrollTop;
      data.messages.forEach(message => { if (!messages.has(message.id)) { messages.set(message.id, message); place(message); } });
      historyBefore = data.messages[0]?.sequence ?? historyBefore;
      older = !!data.older; olderButton.hidden = !older;
      scroller.scrollTop = top + (scroller.scrollHeight - height);          // keep the reader's place
      atBottom = nearBottom();
    } catch (problem) { if (live()) showBanner(`Could not load older messages: ${failure(problem)}`); }
    finally { if (live()) olderButton.disabled = false; }
  };

  // ---- the event stream: replay from the saved cursor, reconnect with backoff, resynchronise on reset
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
    source.addEventListener('open', () => { retryDelay = 1000; if (!banner.hidden && banner.dataset.reconnect) showBanner(''); });
    source.addEventListener('chat', event => {
      let item;
      try { item = JSON.parse(event.data); } catch (_) { return; }
      if (!item || item.cursor <= cursor) return;                              // replay overlap is ignored by cursor
      cursor = item.cursor;
      if (item.kind === 'message') upsert(item.data);
      else if (item.kind === 'delta') delta(item.data);
      else if (item.kind === 'runtime') { runtime = item.data; paintRuntime(); }
      else if (item.kind === 'action') {
        actions = actions.filter(action => action.id !== item.data.id);
        if (item.data.state === 'pending') actions.push(item.data);
        paintActions();
      }
    });
    source.addEventListener('reset', () => { closeStream(); void loadState(); });
    source.addEventListener('done', () => { closeStream(); showBanner('This chat is no longer available.'); });
    source.onerror = () => {
      if (!live()) return;
      showBanner('Reconnecting…'); banner.dataset.reconnect = '1';
      // The browser retries a dropped stream itself with Last-Event-ID; a closed one needs a fresh connection.
      if (source.readyState === EventSource.CLOSED) { stream = null; retry(connect); }
    };
  }

  void loadState();
  return {
    agentId,
    dispose() {
      disposed = true; generation++;
      closeStream(); clearTimeout(retryTimer); clearTimeout(flushTimer); clearTimeout(readTimer);
      saveDraft();
      cleanups.splice(0).forEach(cleanup => cleanup());
      input.oninput = input.onkeydown = form.onsubmit = olderButton.onclick = jump.onclick = null;
      if (pane.firstElementChild === root) pane.innerHTML = '';
    },
    focus() { input.focus(); },
  };
}
