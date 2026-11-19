// Managed runtime in Chat. An agent runs either in a Terminal ("terminal", the default) or headless under Qorqbot ("managed"), where
// the owner only uses Chat. This file holds what is specific to a managed agent: the session wording, the Stop button, the activity
// line, the permission and question cards, and the per-agent runtime switch. js/global-chat.js calls it; every function here
// returns null or does nothing for a terminal agent, so a session without `execution` renders exactly as it did before.
// The server shapes are in docs/MANAGED-RUNTIME-STEP3-DESIGN.md section 5. `session.managed` is
// {open, phase, activity, turn_started_at, can_stop, closed_text, pending: [{request_id, kind, tool, input_summary, input_text,
// input_truncated, decision_reason, blocked_path, questions: [{question, header, options: [{label, description}], multiSelect}]}]}.
// Every string from the server reaches the page as text or through escapeHtml.
const agentRuntime = (() => {
  const WORKING = new Set(['working', 'blocked']);
  const ACTIVITY_MAX = 160;
  const SUMMARY_MAX = 600;
  const INPUT_MAX = 20000;            // what the server sends of a tool input at most; a card never shows less than all of it
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const clip = (value, max) => { const text = String(value ?? '').replace(/\s+/g, ' ').trim(); return text.length > max ? text.slice(0, max - 1) + '…' : text; };

  // A session with no `execution` (an older server, a Codex session) is a terminal one.
  const execution = session => session?.execution === 'managed' ? 'managed' : 'terminal';
  const isManaged = session => execution(session) === 'managed';
  // The managed part of a session in one normalized shape, or null for a terminal agent.
  function managedOf(session) {
    if (!isManaged(session)) return null;
    const raw = session.managed && typeof session.managed === 'object' ? session.managed : {};
    const open = raw.open === false || session.reason === 'session_closed' ? false : true;
    const pending = (Array.isArray(raw.pending) ? raw.pending : []).filter(item => item && typeof item.request_id === 'string' && item.request_id
      && (item.kind === 'permission' || item.kind === 'question'));
    return {open, phase: typeof raw.phase === 'string' ? raw.phase : '', activity: typeof raw.activity === 'string' ? raw.activity : '',
      turnStartedAt: raw.turn_started_at ?? null, canStop: raw.can_stop === true, pending,
      closedText: typeof raw.closed_text === 'string' ? clip(raw.closed_text, 240) : ''};
  }
  const working = (session, managed = managedOf(session)) => !!managed && managed.open && (managed.phase ? WORKING.has(managed.phase) : session.working === true);
  // A managed agent with no session open. Sending a message starts one, so the composer is usable without a binding.
  const closed = session => !!managedOf(session) && !managedOf(session).open;
  const canOpen = session => closed(session) && session.can_send === true && !session.binding;

  // The dot and the line under the conversation. Null means "not managed: use the terminal wording".
  function summary(session, label, viewing = true) {
    const managed = managedOf(session);
    if (!managed) return null;
    // A session that ended unexpectedly says why (the server's own plain sentence); an ordinary close just says how to start it again.
    if (!managed.open) return {key: 'closed', line: managed.closedText || `Sending a message starts ${label}.`};
    if (session.state !== 'live') return {key: 'offline', line: session.can_send ? `${label} isn’t running. Sending a message starts it.` : `${label} isn’t available right now.`};
    if (session.reason === 'capture_disabled') return {key: 'disabled', line: ''};
    if (session.reason === 'binding_not_ready' || session.reason === 'capture_not_ready' || managed.phase === 'starting' || (session.can_send && !viewing)) return {key: 'starting', line: ''};
    return {key: working(session, managed) ? 'working' : 'live', line: ''};
  }
  const startHint = (session, label) => isManaged(session) ? `Send a message to start ${label}` : null;
  const emptyText = (session, label) => isManaged(session) ? `${startHint(session, label)}. Its conversation will appear here.` : null;

  // What the agent is doing right now, and whether Stop applies. Empty text hides the line.
  function activity(session) {
    const managed = managedOf(session);
    if (!managed || !managed.open || !WORKING.has(managed.phase)) return {text: '', stop: false};
    const waiting = managed.pending.length > 0;
    return {text: waiting ? 'Waiting for your answer' : clip(managed.activity, ACTIVITY_MAX) || 'Working', stop: managed.canStop};
  }

  // Wording for a refusal when the agent is managed; null leaves the terminal wording alone.
  const MANAGED_REFUSALS = {
    terminal_offline: 'Not running. Send the message again to start it.',
    session_closed: 'Not running. Send the message again to start it.',
    terminal_changed: 'Not delivered. The session changed. Try again.',
    terminal_required: 'This command isn’t available when Qorqbot runs the agent.',
    managed_command: 'This command isn’t available when Qorqbot runs the agent.',
    driver_unavailable: 'Couldn’t start the session. Check that Claude Code is signed in, then try again.',
    // The server's other refusals when a message opens a closed session (native_chat/chat.py `_OPEN_REFUSALS`).
    cli_unavailable: 'Claude Code isn’t installed. Install it in Settings, then send again.',
    resume_failed: 'Couldn’t continue the earlier conversation, so nothing was started.',
    resume_unavailable: 'The earlier conversation can’t be continued because its file is missing.',
    open_timeout: 'Still starting. Try again in a moment.',
    open_busy: 'Several sessions are starting right now. Try again in a moment.',
    session_opening: 'A session is being opened. Try again in a moment.',
    terminal_present: 'This agent’s Terminal is still running. Close it, then send again.',
    host_unverified: 'Couldn’t check the session. Try again in a moment.',
    binding_not_ready: 'Connecting. Try again in a moment.',
  };
  const refusalText = (session, code) => isManaged(session) ? MANAGED_REFUSALS[code] || null : null;
  // Refusals that are retried with the very same request (same key): the host keeps starting and the retry finds it.
  const RETRYABLE = new Set(['open_timeout', 'open_busy', 'session_opening', 'host_unverified', 'binding_not_ready']);
  const isRetryable = code => RETRYABLE.has(code);
  // A conversation that can't be continued is never replaced silently: the owner is offered a new one, and chooses.
  const NEW_CONVERSATION = new Set(['resume_unavailable', 'resume_failed']);
  const newConversationOffer = (session, code) => isManaged(session) && NEW_CONVERSATION.has(code)
    ? {text: `${MANAGED_REFUSALS[code]} Nothing was sent.`, label: 'Start a new conversation'} : null;

  // ---- the runtime switch: which agents offer it, and what a refusal says
  // Only Ordubashy or a worker running Claude Code can be managed; an agent already managed always keeps the switch so it can go back.
  function eligible(agent, session) {
    if (isManaged(session)) return true;
    const role = agent?.role?.id || agent?.role_id, runtime = agent?.runtime ?? agent?.profile?.runtime;
    if ((role !== 'global-worker' && role !== 'ordubashy') || (runtime !== undefined && runtime !== 'claude-code')) return false;
    return !session?.provider || session.provider === 'claude';
  }
  const RUNTIME_REFUSALS = {
    session_open: 'A session is still open. End it, then switch.',
    session_live: 'A session is still open. End it, then switch.',
    live_session: 'A session is still open. End it, then switch.',
    waiting_inputs: 'Messages are still waiting for this agent. Let them finish, then switch.',
    inputs_waiting: 'Messages are still waiting for this agent. Let them finish, then switch.',
    terminal_present: 'The agent’s Terminal is still running. Close it, then switch.',
    terminal_running: 'The agent’s Terminal is still running. Close it, then switch.',
    tmux_session: 'The agent’s Terminal is still running. Close it, then switch.',
    unsupported_role: 'Only Ordubashy and worker agents can be run by Qorqbot.',
    unsupported_runtime: 'Only Claude Code agents can be run by Qorqbot.',
    forbidden: 'Only the owner can switch how an agent runs.',
  };
  function runtimeRefusal(error) {
    const code = String(error?.code || ''), reason = String(error?.details?.reason || '');
    const text = RUNTIME_REFUSALS[code] || RUNTIME_REFUSALS[reason];
    if (text) return text;
    // A server message the shared failure wording already judged fit to show.
    const message = typeof error?.message === 'string' ? error.message.trim() : '';
    return code && code !== 'invalid_response' && code !== 'network_unavailable' && message && message.length <= 200 ? message : 'Couldn’t switch. Try again.';
  }

  // ---- permission and question cards
  const questionList = questions => (Array.isArray(questions) ? questions : []).map(item => {
    if (typeof item === 'string') return {question: item, header: '', options: [], multi: false};
    const options = (Array.isArray(item?.options) ? item.options : []).map(option => typeof option === 'string' ? {label: option, description: ''}
      : {label: String(option?.label ?? ''), description: String(option?.description ?? '')}).filter(option => option.label);
    return {question: String(item?.question ?? ''), header: String(item?.header ?? ''), options, multi: item?.multiSelect === true || item?.multi_select === true};
  }).filter(item => item.question);
  function cardHtml(card, label, index = 0) {
    const id = escapeHtml(card.request_id), tool = clip(card.tool, 80);
    if (card.kind === 'permission') {
      // The whole request is shown (scrollable, escaped): what is allowed is what was read. When any of it was cut on the way
      // here (or an older server only sent a clipped line) it cannot be judged, so Allow is off and the owner can only deny.
      const given = typeof card.input_text === 'string';
      const full = given ? card.input_text : clip(card.input_summary, SUMMARY_MAX);
      const cut = card.input_truncated === true || full.length > INPUT_MAX || (!given && String(card.input_summary ?? '').endsWith('…'));
      const shown = full.slice(0, INPUT_MAX), reason = clip(card.decision_reason, 600), path = clip(card.blocked_path, 400);
      const note = `mchat-cut-${index}`;
      return `<section class="mchat-card" role="group" data-request-id="${id}" data-kind="permission" aria-label="Permission request">
        <p class="mchat-card-title">${escapeHtml(label)} wants to use <strong>${escapeHtml(tool || 'a tool')}</strong></p>
        ${reason ? `<p class="mchat-card-reason">${escapeHtml(reason)}</p>` : ''}
        ${path ? `<p class="mchat-card-path">Path: <code>${escapeHtml(path)}</code></p>` : ''}
        ${shown ? `<pre class="mchat-card-input" tabindex="0" role="region" aria-label="What ${escapeHtml(label)} wants to run">${escapeHtml(shown)}</pre>` : ''}
        ${cut ? `<p class="mchat-card-note" id="${note}">This request is too long to show in full, so it can’t be allowed from here. You can deny it.</p>` : ''}
        <p class="mchat-card-error" role="alert" hidden></p>
        <div class="mchat-card-actions"><button type="button" data-answer="allow"${cut ? ` disabled data-locked="1" aria-describedby="${note}"` : ''}>Allow</button><button type="button" class="quiet" data-answer="deny">Deny</button></div>
      </section>`;
    }
    const questions = questionList(card.questions);
    const body = questions.map((item, q) => `<fieldset class="mchat-question"><legend>${item.header ? `<span class="mchat-question-tag">${escapeHtml(clip(item.header, 40))}</span> ` : ''}${escapeHtml(item.question)}</legend>
        ${item.options.map(option => `<label class="mchat-option"><input type="${item.multi ? 'checkbox' : 'radio'}" name="mchat-${index}-${q}" data-q="${q}" value="${escapeHtml(option.label)}"><span>${escapeHtml(option.label)}${option.description ? `<small>${escapeHtml(clip(option.description, 200))}</small>` : ''}</span></label>`).join('')}
        <input type="text" class="mchat-other" data-q-other="${q}" maxlength="2000" autocomplete="off" aria-label="${item.options.length ? 'Another answer' : 'Your answer'}" placeholder="${item.options.length ? 'Something else…' : 'Your answer…'}">
      </fieldset>`).join('');
    return `<section class="mchat-card" role="group" data-request-id="${id}" data-kind="question" aria-label="Question from ${escapeHtml(label)}">
      <p class="mchat-card-title">${escapeHtml(label)} has a question</p>
      ${body}
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

  // The Stop button, the activity line and the cards, in one block above the session line. `send(path, body)` posts to the agent's
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
    // A button the page turned off for good (Allow on a request that cannot be shown in full) stays off whatever is unlocked.
    const lock = (node, on) => { for (const tag of ['button', 'input']) for (const item of node.querySelectorAll(tag)) item.disabled = on || item.getAttribute('data-locked') !== null; };
    // One action the owner is offered after a refusal (starting a new conversation); gone as soon as a session is open.
    const offer = (what, onChoose) => { noticeText.textContent = what.text; noticeAction.textContent = what.label; choose = onChoose; notice.hidden = false; el.hidden = false; };
    const clearOffer = () => { choose = null; notice.hidden = true; noticeText.textContent = ''; };
    const fail = (node, text) => { const box = node.querySelector('.mchat-card-error'); box.textContent = text; box.hidden = !text; };

    function update(session) {
      last = session;
      const managed = managedOf(session);
      el.hidden = !managed;
      if (!managed) { for (const [, card] of cards) card.node.remove(); cards.clear(); stopping = false; clearOffer(); return; }
      if (managed.open) clearOffer();
      const info = activity(session);
      line.hidden = !info.text; lineText.textContent = info.text;
      stop.hidden = !info.stop; stop.disabled = stopping;
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
    }
    async function interrupt() {
      if (stopping || !binding()) { if (!binding()) onError('Couldn’t stop. Try again in a moment.'); return; }
      stopping = true; stop.disabled = true;
      try { await send('/interrupt', {expected_binding: binding()}); }
      catch (_) { onError('Couldn’t stop. Try again.'); }
      finally { stopping = false; if (!disposed) { stop.disabled = false; onChange(); } }
    }
    async function answer(id, choice) {
      const card = cards.get(id);
      if (!card || card.inflight) return;
      if (card.data.kind === 'permission' && choice === 'allow' && card.node.querySelector('[data-answer="allow"][data-locked]')) return;
      const decision = card.data.kind === 'permission' ? choice : questionDecision(card.data, card.node);
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
      if (node) void answer(node.dataset.requestId, button.dataset.answer);
    });
    // Enter in a typed answer sends the card, like its button.
    el.addEventListener('keydown', event => {
      if (event.key !== 'Enter' || event.isComposing || !event.target.matches?.('[data-q-other]')) return;
      event.preventDefault();
      const node = event.target.closest('[data-request-id]');
      if (node) void answer(node.dataset.requestId, 'submit');
    });
    return {el, update, offer, clearOffer, dispose() { disposed = true; el.remove(); cards.clear(); }, get session() { return last; }};
  }

  // The switch: two buttons (the selected one is disabled, as in the Chat/Terminal switch) and, for an open managed session, End session.
  function createToggle({label, send, onDone, onError, doc = globalThis.document}) {
    const el = doc.createElement('div');
    el.className = 'mchat-runtime'; el.hidden = true; el.setAttribute('role', 'group'); el.setAttribute('aria-label', `How ${label} runs`);
    el.innerHTML = `<button type="button" class="quiet" data-runtime="terminal" title="You use it in a Terminal">Terminal</button><button type="button" class="quiet" data-runtime="managed" title="Qorqbot runs it. You use Chat">Managed</button><button type="button" class="quiet" data-runtime-close hidden title="End the running session">End session</button>`;
    const buttons = [...el.querySelectorAll('[data-runtime]')], end = el.querySelector('[data-runtime-close]');
    let busy = false, mode = 'terminal', open = false;
    const paint = () => {
      for (const button of buttons) {
        const on = button.dataset.runtime === mode;
        button.setAttribute('aria-pressed', String(on)); button.disabled = busy || on;
      }
      end.hidden = !(mode === 'managed' && open); end.disabled = busy;
    };
    async function run(path, body, after) {
      if (busy) return;
      busy = true; paint();
      try { await send(path, body); busy = false; paint(); after(); }
      catch (problem) { busy = false; paint(); onError(path === '/close' ? 'Couldn’t end the session. Try again.' : runtimeRefusal(problem)); }
    }
    el.addEventListener('click', event => {
      const button = event.target.closest('[data-runtime]');
      if (button && button.dataset.runtime !== mode) { void run('/runtime', {mode: button.dataset.runtime}, () => onDone()); return; }
      if (event.target.closest('[data-runtime-close]')) void run('/close', {}, () => onDone());
    });
    return {el, update(session, show) {
      mode = execution(session); open = !!managedOf(session)?.open;
      el.hidden = !show; paint();
    }};
  }

  return {execution, isManaged, managedOf, closed, canOpen, working, summary, startHint, emptyText, activity, refusalText, isRetryable, newConversationOffer,
    eligible, runtimeRefusal, questionList, cardHtml, questionDecision, createBar, createToggle, escapeHtml};
})();
if (typeof module !== 'undefined') module.exports = agentRuntime;
