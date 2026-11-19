// Chat for an agent Qorqbot runs itself. Every agent is run this way: the owner only uses Chat. This file holds the session wording
// for each state (asleep, opening, working, ready, off, cannot start), the Stop button, the activity line, the question cards, the
// End session button and the refusal texts. js/global-chat.js calls it.
// The server shapes are in docs/MANAGED-RUNTIME.md. `session.managed` is
// {open, off, phase, activity, turn_started_at, can_stop, closed_text, pending: [{request_id, kind: 'question', tool, questions:
// [{question, header, options: [{label, description}], multiSelect}]}]}. `off` is true for an agent the owner ended: it stays off
// until the owner sends a message. A session without `managed` reads as open. `session.execution` is not read any more.
// Every string from the server reaches the page as text or through escapeHtml.
const agentRuntime = (() => {
  const WORKING = new Set(['working', 'blocked']);
  const ACTIVITY_MAX = 160;
  const SUMMARY_MAX = 600;
  const INPUT_MAX = 20000;            // what the server sends of a tool input at most; a card never shows less than all of it
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const clip = (value, max) => { const text = String(value ?? '').replace(/\s+/g, ' ').trim(); return text.length > max ? text.slice(0, max - 1) + '…' : text; };

  // The managed part of a session in one normalized shape, or null while there is no session yet.
  function managedOf(session) {
    if (!session) return null;
    const raw = session.managed && typeof session.managed === 'object' ? session.managed : {};
    const off = raw.off === true;
    const open = off || raw.open === false || session.reason === 'session_closed' ? false : true;
    const pending = (Array.isArray(raw.pending) ? raw.pending : []).filter(item => item && typeof item.request_id === 'string' && item.request_id && item.kind === 'question');
    return {open, off, phase: typeof raw.phase === 'string' ? raw.phase : '', activity: typeof raw.activity === 'string' ? raw.activity : '',
      turnStartedAt: raw.turn_started_at ?? null, canStop: raw.can_stop === true, pending,
      closedText: typeof raw.closed_text === 'string' ? clip(raw.closed_text, 240) : ''};
  }
  const working = (session, managed = managedOf(session)) => !!managed && managed.open && (managed.phase ? WORKING.has(managed.phase) : session.working === true);
  // An agent with no session open (asleep or off). Sending a message starts one, so the composer is usable without a binding.
  const closed = session => !!managedOf(session) && !managedOf(session).open;
  const canOpen = session => closed(session) && session.can_send === true && !session.binding;

  // The dot and the line by the composer. `context.history`: the conversation already has messages; `context.opening`: a message
  // that starts the session is on its way.
  function summary(session, label, viewing = true, context = {}) {
    const managed = managedOf(session);
    if (!managed) return null;
    if (!managed.open) {
      if (context.opening) return {key: 'starting', line: `Starting ${label}…`};
      if (managed.off) return {key: 'off', line: `${label} is off. It stays off until you send a message.`};
      // A session that ended unexpectedly says why (the server's own plain sentence).
      if (managed.closedText) return {key: 'closed', line: managed.closedText};
      return {key: 'closed', line: context.history ? `${label} is asleep. It wakes when there is something to do.` : ''};
    }
    if (session.state !== 'live') return {key: 'offline', line: session.can_send ? `${label} isn’t running. Sending a message starts it.` : `${label} isn’t available right now.`};
    if (session.reason === 'capture_disabled') return {key: 'disabled', line: ''};
    if (session.reason === 'binding_not_ready' || session.reason === 'capture_not_ready' || managed.phase === 'starting' || (session.can_send && !viewing)) return {key: 'starting', line: `Starting ${label}…`};
    return working(session, managed) ? {key: 'working', line: ''} : {key: 'live', line: `${label} is ready.`};
  }
  const startHint = (session, label) => managedOf(session)?.off ? `Send a message to turn ${label} on` : `Send a message to start ${label}`;
  const emptyText = (session, label) => managedOf(session)?.off ? `${label} is off. Send a message to turn it on.` : `${startHint(session, label)}. Its conversation will appear here.`;
  // The composer's placeholder: what a message does when the session is not open.
  function placeholder(session, label, history) {
    const managed = managedOf(session);
    if (managed && !managed.open) return managed.off ? `Message ${label} to turn it on…` : history ? `Message ${label}…` : `Message ${label} to start it…`;
    return `Message ${label}…`;
  }

  // Why the composer can't be used right now, in plain words; empty when it can. Attach, Send, the microphone and the text field all
  // give this as their reason while they are off. `ctx.finished`: the chat is over; `ctx.paired`: a side conversation has the pane.
  function unavailableText(session, label, ctx = {}) {
    if (ctx.finished) return 'This chat is no longer available.';
    if (ctx.paired) return `Close the side conversation to message ${label}.`;
    if (!session) return 'Loading the conversation…';
    if (session.reason === 'capture_disabled') return 'Chat is unavailable.';
    const managed = managedOf(session);
    if (!session.can_send) return managed && !managed.open ? managed.closedText || `${label} can’t be started right now.` : `${label} isn’t available right now.`;
    return `Connecting to ${label}…`;
  }

  // What the agent is doing right now, and whether Stop applies. Empty text hides the line.
  function activity(session) {
    const managed = managedOf(session);
    if (!managed || !managed.open || !WORKING.has(managed.phase)) return {text: '', stop: false};
    const waiting = managed.pending.length > 0;
    return {text: waiting ? 'Waiting for your answer' : clip(managed.activity, ACTIVITY_MAX) || 'Working', stop: managed.canStop};
  }

  // Wording for a refusal, by the server's code. Where something must be set up, the text points to Settings, where Claude Code
  // and Codex are connected.
  const MANAGED_REFUSALS = {
    session_closed: 'Not running. Send the message again to start it.',
    terminal_changed: 'Not delivered. The session changed. Try again.',
    managed_command: 'This command isn’t available when Qorqbot runs the agent.',
    driver_unavailable: 'Couldn’t start. Check in Settings that Claude Code or Codex is connected, then send again.',
    // The server's other refusals when a message opens a closed session (native_chat/chat.py `_OPEN_REFUSALS`).
    cli_unavailable: 'Claude Code or Codex isn’t installed yet. Connect one in Settings, then send again.',
    resume_failed: 'Couldn’t continue the earlier conversation, so nothing was started.',
    resume_unavailable: 'The earlier conversation can’t be continued because its file is missing.',
    open_timeout: 'Still starting. Try again in a moment.',
    open_busy: 'Several sessions are starting right now. Try again in a moment.',
    session_opening: 'A session is being opened. Try again in a moment.',
    host_unverified: 'Couldn’t check the session. Try again in a moment.',
    binding_not_ready: 'Connecting. Try again in a moment.',
  };
  const refusalText = code => MANAGED_REFUSALS[code] || null;
  // Refusals that are retried with the very same request (same key): the host keeps starting and the retry finds it.
  const RETRYABLE = new Set(['open_timeout', 'open_busy', 'session_opening', 'host_unverified', 'binding_not_ready']);
  const isRetryable = code => RETRYABLE.has(code);
  // A conversation that can't be continued is never replaced silently: the owner is offered a new one, and chooses.
  const NEW_CONVERSATION = new Set(['resume_unavailable', 'resume_failed']);
  const newConversationOffer = code => NEW_CONVERSATION.has(code)
    ? {text: `${MANAGED_REFUSALS[code]} Nothing was sent.`, label: 'Start a new conversation'} : null;

  // ---- question cards
  const questionList = questions => (Array.isArray(questions) ? questions : []).map(item => {
    if (typeof item === 'string') return {question: item, header: '', options: [], multi: false};
    const options = (Array.isArray(item?.options) ? item.options : []).map(option => typeof option === 'string' ? {label: option, description: ''}
      : {label: String(option?.label ?? ''), description: String(option?.description ?? '')}).filter(option => option.label);
    return {question: String(item?.question ?? ''), header: String(item?.header ?? ''), options, multi: item?.multiSelect === true || item?.multi_select === true};
  }).filter(item => item.question);
  function cardHtml(card, label, index = 0) {
    const id = escapeHtml(card.request_id);
    const questions = questionList(card.questions);
    const body = questions.map((item, q) => `<fieldset class="mchat-question"><legend>${item.header ? `<span class="mchat-question-tag">${escapeHtml(clip(item.header, 40))}</span> ` : ''}${escapeHtml(item.question)}</legend>
        ${item.options.map(option => `<label class="mchat-option"><input type="${item.multi ? 'checkbox' : 'radio'}" name="mchat-${index}-${q}" data-q="${q}" value="${escapeHtml(option.label)}"><span>${escapeHtml(option.label)}${option.description ? `<small>${escapeHtml(clip(option.description, 200))}</small>` : ''}</span></label>`).join('')}
        <input type="text" class="mchat-other" data-q-other="${q}" maxlength="2000" autocomplete="off" aria-label="${item.options.length ? 'Another answer' : 'Your answer'}" placeholder="${item.options.length ? 'Something else…' : 'Your answer…'}">
      </fieldset>`).join('');
    return `<section class="mchat-card" role="group" data-request-id="${id}" data-kind="question" aria-label="Question from ${escapeHtml(label)}">
      <p class="mchat-card-title">${escapeHtml(label)} has a question</p>
      <div class="mchat-card-body">${body}</div>
      <p class="mchat-card-error" role="alert" hidden></p>
      <div class="mchat-card-actions"><button type="button" data-answer="submit">Send answer</button></div>
    </section>`;
  }
  // The `decision` for a question card: {answers: {<question text>: <answer>}} with an answer for every question, as the server
  // requires; null while one is unanswered. A question that allows several answers joins the chosen labels (and anything typed)
  // with ", "; one that allows a single answer sends the typed text when there is some, otherwise the chosen label.
  function questionDecision(card, element) {
    const questions = questionList(card.questions), answers = {};
    for (let q = 0; q < questions.length; q++) {
      const chosen = [...element.querySelectorAll('[data-q]')].filter(input => input.dataset.q === String(q) && input.checked).map(input => input.value);
      const other = element.querySelector(`[data-q-other="${q}"]`)?.value?.trim();
      const parts = questions[q].multi ? [...chosen, ...(other ? [other] : [])] : other ? [other] : chosen.slice(0, 1);
      if (!parts.length) return null;
      answers[questions[q].question] = parts.join(', ');
    }
    return Object.keys(answers).length ? {answers} : null;
  }

  // The Stop button, the activity line and the question cards, in one block above the session line. `send(path, body)` posts to the agent's
  // chat route and returns its data or throws; `binding()` is the live session's binding id.
  function createBar({label, send, binding, onChange, onError, now = Date.now, doc = globalThis.document}) {
    const el = doc.createElement('div');
    el.className = 'mchat'; el.hidden = true;
    el.innerHTML = `<div class="mchat-notice" role="alert" hidden><span class="mchat-notice-text"></span><button type="button" class="mchat-notice-action"></button></div>
      <div class="mchat-cards" role="group" aria-label="Waiting for you"></div>
      <div class="mchat-activity" role="status" aria-live="polite" hidden><span class="mchat-dots" aria-hidden="true"><i></i><i></i><i></i></span><span class="mchat-activity-text"></span><button type="button" class="quiet mchat-stop" hidden>Stop</button></div>`;
    const notice = el.querySelector('.mchat-notice'), noticeText = el.querySelector('.mchat-notice-text'), noticeAction = el.querySelector('.mchat-notice-action');
    let choose = null;
    const cardsHost = el.querySelector('.mchat-cards'), line = el.querySelector('.mchat-activity'), lineText = el.querySelector('.mchat-activity-text'), stop = el.querySelector('.mchat-stop');
    const cards = new Map();                      // request id -> {node, data, sentAt, inflight}
    let stopping = false, last = null, serial = 0, disposed = false;
    const lock = (node, on) => {
      for (const tag of ['button', 'input']) for (const item of node.querySelectorAll(tag)) item.disabled = on;
      const submit = node.querySelector('[data-answer]');
      if (submit) submit.setAttribute('title', on ? 'Sending your answer…' : 'Send your answer');
    };
    // One action the owner is offered after a refusal (starting a new conversation); gone as soon as a session is open.
    const offer = (what, onChoose) => { noticeText.textContent = what.text; noticeAction.textContent = what.label; choose = onChoose; notice.hidden = false; el.hidden = false; };
    const clearOffer = () => { choose = null; notice.hidden = true; noticeText.textContent = ''; el.hidden = !(cards.size || !line.hidden); };
    const fail = (node, text) => { const box = node.querySelector('.mchat-card-error'); box.textContent = text; box.hidden = !text; };

    function update(session) {
      last = session;
      const managed = managedOf(session);
      el.hidden = !managed;
      if (!managed) { for (const [, card] of cards) card.node.remove(); cards.clear(); stopping = false; clearOffer(); el.hidden = true; return; }
      if (managed.open) clearOffer();
      const info = activity(session);
      line.hidden = !info.text; lineText.textContent = info.text;
      stop.hidden = !info.stop; stop.disabled = stopping; stop.title = stopping ? 'Stopping…' : `Stop what ${label} is doing`;
      const wanted = managed.open ? managed.pending : [];
      for (const [id, card] of cards) if (!wanted.some(item => item.request_id === id)) { card.node.remove(); cards.delete(id); }
      for (const item of wanted) {
        const known = cards.get(item.request_id);
        if (known) {
          // An answer the server still lists after a while was not taken: let the owner try again.
          if (!known.inflight && known.sentAt && now() - known.sentAt > 8000) { known.sentAt = 0; lock(known.node, false); }
          continue;
        }
        const node = doc.createElement('div');
        node.innerHTML = cardHtml(item, label, serial++);
        const section = node.firstElementChild;
        cards.set(item.request_id, {node: section, data: item, sentAt: 0, inflight: false});
        cardsHost.append(section);
      }
      cardsHost.hidden = cards.size === 0;
      // The block takes room only while it has something to show.
      el.hidden = !(cards.size || info.text || !notice.hidden);
    }
    async function interrupt() {
      if (stopping || !binding()) { if (!binding()) onError('Couldn’t stop. Try again in a moment.'); return; }
      stopping = true; stop.disabled = true; stop.title = 'Stopping…';
      try { await send('/interrupt', {expected_binding: binding()}); }
      catch (_) { onError('Couldn’t stop. Try again.'); }
      finally { stopping = false; if (!disposed) { stop.disabled = false; stop.title = `Stop what ${label} is doing`; onChange(); } }
    }
    async function answer(id) {
      const card = cards.get(id);
      if (!card || card.inflight) return;
      const decision = questionDecision(card.data, card.node);
      if (!decision) { fail(card.node, 'Choose or type an answer for each question.'); return; }
      if (!binding()) { fail(card.node, 'Not connected. Try again in a moment.'); return; }
      fail(card.node, ''); card.inflight = true; lock(card.node, true);
      try {
        await send('/answer', {expected_binding: binding(), request_id: id, decision});
        card.sentAt = now();
      } catch (problem) {
        card.sentAt = 0; lock(card.node, false);
        fail(card.node, ['not_found', 'terminal_changed', 'stale_request'].includes(problem?.code) ? 'This is no longer waiting for an answer.' : 'Couldn’t send your answer. Try again.');
      } finally { card.inflight = false; if (!disposed) onChange(); }
    }
    el.addEventListener('click', event => {
      if (event.target.closest('.mchat-stop')) { void interrupt(); return; }
      if (event.target.closest('.mchat-notice-action')) { const run = choose; clearOffer(); if (run) run(); return; }
      const button = event.target.closest('[data-answer]');
      const node = button?.closest('[data-request-id]');
      if (node) void answer(node.dataset.requestId);
    });
    // Enter in a typed answer sends the card, like its button.
    el.addEventListener('keydown', event => {
      if (event.key !== 'Enter' || event.isComposing || !event.target.matches?.('[data-q-other]')) return;
      event.preventDefault();
      const node = event.target.closest('[data-request-id]');
      if (node) void answer(node.dataset.requestId);
    });
    return {el, update, offer, clearOffer, dispose() { disposed = true; el.remove(); cards.clear(); }, get session() { return last; }};
  }

  // End session: a quiet button in the session line, shown only while a session is open. It posts the close, which turns the agent
  // off. `send(path, body)` posts to the agent's chat route and returns its data or throws; `onDone()` runs when it was taken.
  const END_FAILED = 'Couldn’t end the session. Try again.';
  const canEnd = session => !!managedOf(session)?.open && session.state === 'live';
  function createEnd({send, onDone, onError, doc = globalThis.document}) {
    const el = doc.createElement('button');
    el.setAttribute('type', 'button'); el.className = 'quiet gchat-end'; el.hidden = true; el.textContent = 'End session';
    const TITLE = 'Turn the agent off until you send it a message';
    el.setAttribute('title', TITLE);
    let busy = false;
    el.addEventListener('click', async () => {
      if (busy) return;
      busy = true; el.disabled = true; el.setAttribute('title', 'Ending the session…');
      try { await send('/close', {}); busy = false; el.disabled = false; el.setAttribute('title', TITLE); onDone(); }
      catch (_) { busy = false; el.disabled = false; el.setAttribute('title', TITLE); onError(END_FAILED); }
    });
    return {el, update(session) { el.hidden = !canEnd(session); }};
  }
  // The session as the owner sees it the moment End session was taken, before the next read confirms it.
  const afterEnd = session => session ? {...session, state: 'offline', reason: 'session_closed', binding: null, working: false,
    managed: {...(session.managed || {}), open: false, off: true, phase: 'idle', activity: '', can_stop: false, pending: []}} : session;

  return {managedOf, closed, canOpen, canEnd, working, summary, startHint, emptyText, placeholder, unavailableText, activity, refusalText, isRetryable, newConversationOffer,
    questionList, cardHtml, questionDecision, createBar, createEnd, afterEnd, END_FAILED, escapeHtml};
})();
if (typeof module !== 'undefined') module.exports = agentRuntime;
