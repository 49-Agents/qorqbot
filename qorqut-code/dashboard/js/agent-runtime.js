// Chat for an agent Qorqbot runs itself. Every agent is run this way: the owner only uses Chat. This file holds the session wording
// for each state (asleep, opening, working, ready, off, cannot start), the Stop button, the activity line, the question cards, the
// refusal texts. js/global-chat.js calls it.
// The server shapes are in docs/MANAGED-RUNTIME.md. `session.managed` is
// {open, off, phase, activity, turn_started_at, can_stop, closed_text, account_problem, pending: [{request_id, kind: 'question', tool, questions:
// [{question, header, options: [{label, description}], multiSelect}]}]}. `off` is true for an agent the owner ended: it stays off
// until the owner sends a message. `account_problem` is null or {kind: 'usage_limit' | 'sign_in', provider, resets_at, resets_text, account:
// {email, plan}, other_provider: {id, ready}}: the last answer failed because of the account, and Chat shows a notice for it. A session without `managed` reads as open. `session.execution` is not read any more.
// Every string from the server reaches the page as text or through escapeHtml.
const agentRuntime = (() => {
  const WORKING = new Set(['working', 'blocked']);
  // What the server says while an open session is not yet ready to take a message (the whole opening phase).
  const STARTING_REASONS = new Set(['binding_not_ready', 'capture_not_ready', 'registering']);
  const ACTIVITY_MAX = 160;
  const SUMMARY_MAX = 600;
  const INPUT_MAX = 20000;            // what the server sends of a tool input at most; a card never shows less than all of it
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const clip = (value, max) => { const text = String(value ?? '').replace(/\s+/g, ' ').trim(); return text.length > max ? text.slice(0, max - 1) + '…' : text; };

  // `account_problem` as the page uses it, or null when the last answer did not fail for the account.
  function accountProblemOf(raw) {
    if (!raw || typeof raw !== 'object' || !['usage_limit', 'sign_in'].includes(raw.kind) || !['claude', 'codex'].includes(raw.provider)) return null;
    const text = value => typeof value === 'string' && value.trim() ? clip(value, 120) : null;
    const who = raw.account && typeof raw.account === 'object' ? {email: text(raw.account.email) || '', plan: text(raw.account.plan) || ''} : null;
    const other = raw.other_provider && ['claude', 'codex'].includes(raw.other_provider.id) && raw.other_provider.id !== raw.provider
      ? {id: raw.other_provider.id, ready: raw.other_provider.ready === true} : null;
    return {kind: raw.kind, provider: raw.provider, resetsAt: text(raw.resets_at), resetsText: text(raw.resets_text), account: who && who.email ? who : null, other};
  }

  // The managed part of a session in one normalized shape, or null while there is no session yet.
  function managedOf(session) {
    if (!session) return null;
    const raw = session.managed && typeof session.managed === 'object' ? session.managed : {};
    const off = raw.off === true;
    const open = off || raw.open === false || session.reason === 'session_closed' ? false : true;
    const pending = (Array.isArray(raw.pending) ? raw.pending : []).filter(item => item && typeof item.request_id === 'string' && item.request_id && item.kind === 'question');
    return {open, off, phase: typeof raw.phase === 'string' ? raw.phase : '', activity: typeof raw.activity === 'string' ? raw.activity : '',
      turnStartedAt: raw.turn_started_at ?? null, canStop: raw.can_stop === true, pending,
      closedText: typeof raw.closed_text === 'string' ? clip(raw.closed_text, 240) : '',
      accountProblem: accountProblemOf(raw.account_problem),
      // The client's own words about the last turn when it failed (an account problem, a usage limit and when to try again).
      turnError: raw.last_turn_status === 'error' && typeof raw.turn_error === 'string' ? clip(raw.turn_error, 300) : ''};
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
    // A session that is open and whose conversation is still being registered is part of the opening phase, not an outage.
    if (session.state !== 'live' && session.reason === 'registering') return {key: 'starting', line: `Starting ${label}…`};
    if (session.state !== 'live') return {key: 'offline', line: session.can_send ? `${label} isn’t running. Sending a message starts it.` : `${label} isn’t available right now.`};
    if (session.reason === 'capture_disabled') return {key: 'disabled', line: ''};
    if (session.reason === 'binding_not_ready' || session.reason === 'capture_not_ready' || managed.phase === 'starting' || (session.can_send && !viewing)) return {key: 'starting', line: `Starting ${label}…`};
    if (working(session, managed)) return {key: 'working', line: ''};
    // An account problem has its own notice above the line (createBar), with the buttons that fix it.
    if (managed.turnError && !managed.accountProblem) return {key: 'error', line: `${label} couldn’t answer: ${managed.turnError}`};
    // Ready says nothing: the composer being usable already says it.
    return {key: 'live', line: ''};
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
    // The whole opening phase says one thing: from the message that starts the session until it can take the next one.
    if (ctx.opening || (managed && managed.open && (managed.phase === 'starting' || STARTING_REASONS.has(session.reason)))) return `Starting ${label}…`;
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
    cli_unavailable: 'This agent’s client (Claude Code or Codex) isn’t installed yet. Download it under Settings > Model/Provider, then send again.',
    cli_not_signed_in: 'This agent’s client (Claude Code or Codex) isn’t signed in yet. Sign in under Settings > Model/Provider, then send again.',
    resume_failed: 'Couldn’t continue the earlier conversation, so nothing was started.',
    resume_unavailable: 'The earlier conversation can’t be continued because its file is missing.',
    open_timeout: 'Still starting. Try again in a moment.',
    open_busy: 'Several sessions are starting right now. Try again in a moment.',
    session_opening: 'A session is being opened. Try again in a moment.',
    host_unverified: 'Couldn’t check the session. Try again in a moment.',
    binding_not_ready: 'Connecting. Try again in a moment.',
  };
  // The refusals the server words for the owner, naming the agent, its client, what is wrong and what to do (native_chat/client_state.py):
  // that sentence is shown as it is, and the table above is only the fallback (an older server, or a sentence that is missing).
  const SERVER_WORDED = new Set(['cli_unavailable', 'cli_not_signed_in']);
  function refusalText(code, serverMessage = '') {
    const own = typeof serverMessage === 'string' ? serverMessage.trim() : '';
    if (SERVER_WORDED.has(code) && own && own.length <= 400) return own;
    return MANAGED_REFUSALS[code] || null;
  }
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
  // `stopHere: false`: the agent is stopped from elsewhere (the composer's Send turns into Stop, via `stop()`), so the line shows no Stop.
  // `clients` is js/agent-clients.js (the notice for an account problem reads and changes the products' sign-in through it; without it the
  // notice is not shown) and `clientsOptions` its controller's options (tests). `useOther(client)` switches this agent to the other product.
  function createBar({label, send, binding, onChange, onError, now = Date.now, doc = globalThis.document, stopHere = true, clients = null, clientsOptions = {}, useOther = null}) {
    const el = doc.createElement('div');
    el.className = 'mchat'; el.hidden = true;
    el.innerHTML = `<div class="mchat-notice" role="alert" hidden><span class="mchat-notice-text"></span><button type="button" class="mchat-notice-action"></button></div>
      <div class="mchat-notice mchat-account" role="alert" hidden></div>
      <div class="mchat-cards" role="group" aria-label="Waiting for you"></div>
      <div class="mchat-activity" role="status" aria-live="polite" hidden><span class="mchat-dots" aria-hidden="true"><i></i><i></i><i></i></span><span class="mchat-activity-text"></span><button type="button" class="quiet mchat-stop" hidden>Stop</button></div>`;
    const notice = el.querySelector('.mchat-notice'), noticeText = el.querySelector('.mchat-notice-text'), noticeAction = el.querySelector('.mchat-notice-action');
    let choose = null;
    const accountBox = el.querySelector('.mchat-account');
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
    // The activity line can be shown elsewhere (the chat's focus strip); the block then takes no room for it.
    const lineHere = () => !line.hidden && line.parentNode === el;
    const clearOffer = () => { choose = null; notice.hidden = true; noticeText.textContent = ''; el.hidden = !(cards.size || lineHere() || !accountBox.hidden); };
    const fail = (node, text) => { const box = node.querySelector('.mchat-card-error'); box.textContent = text; box.hidden = !text; };

    // ---- the account notice: a usage limit or a sign-in that is no longer accepted. The products' sign-in state is read only while it is up.
    const acct = {problem: null, ctl: null, attempted: false, busy: '', error: '', shape: ''};
    const acctStop = () => { acct.ctl?.stop(); Object.assign(acct, {ctl: null, problem: null, attempted: false, busy: '', error: '', shape: ''}); accountBox.hidden = true; accountBox.innerHTML = ''; };
    function acctPaint() {
      const problem = acct.problem;
      if (!problem || disposed) return;
      const provider = problem.provider, view = acct.ctl?.state.view, pending = acct.ctl?.state.pending[provider] || acct.busy;
      const info = clients.accountNotice(problem, view && view[provider], {agent: label, attempted: acct.attempted, busy: pending, now: now(),
        error: acct.error || acct.ctl?.state.errors[provider] || ''});
      const shape = JSON.stringify([info.key, info.text, info.small, info.error, info.actions, info.signin && clients.signInShape(info.signin)]);
      if (shape !== acct.shape) {
        const kept = clients.captureCode(accountBox), had = accountBox.contains(doc.activeElement) && doc.activeElement.closest?.('button');
        acct.shape = shape;
        accountBox.dataset.state = info.key;
        // While a sign-in waits, the notice's buttons (Cancel) belong to the sign-in step, at its bottom right.
        const buttons = info.actions.map(a => `<button type="button" class="${a.kind === 'quiet' ? 'quiet' : ''}" data-account-action="${escapeHtml(a.id)}" data-provider="${escapeHtml(provider)}"${a.disabled ? ' disabled aria-busy="true"' : ''} aria-label="${escapeHtml(a.busy ? a.label : a.aria)}">${escapeHtml(a.label)}</button>`).join('');
        acct.buttons = buttons;
        accountBox.innerHTML = `<span class="mchat-notice-text">${escapeHtml(info.text)}${info.small ? `<small>${escapeHtml(info.small)}</small>` : ''}${info.error ? `<small class="mchat-notice-error" role="alert">${escapeHtml(info.error)}</small>` : ''}</span>
          <div class="mchat-notice-actions">${info.signin ? '' : buttons}</div>
          ${info.signin ? `<div class="ac-code" data-ac-signin data-provider="${escapeHtml(provider)}"></div>` : ''}`;
        const slot = accountBox.querySelector('[data-ac-signin]');
        if (slot) { clients.paintSignIn(slot, info.signin, buttons); clients.restoreCode(accountBox, kept); }
        // A pressed button that was replaced hands focus on to the next one that can still be pressed.
        if (had && !accountBox.contains(doc.activeElement)) accountBox.querySelector('button:not([disabled])')?.focus?.({preventScroll: true});
      } else if (info.signin) clients.paintSignIn(accountBox.querySelector('[data-ac-signin]'), info.signin, acct.buttons || '');
      accountBox.hidden = false; el.hidden = false;
    }
    function acctSync(problem) {
      if (!problem || !clients) { if (acct.problem || acct.ctl) acctStop(); el.hidden = !(cards.size || lineHere() || !notice.hidden); return; }
      // A different problem (the other product, or the limit turning into a sign-in) starts afresh.
      if (acct.problem && (acct.problem.provider !== problem.provider || acct.problem.kind !== problem.kind)) acctStop();
      acct.problem = problem;
      if (!acct.ctl) {
        acct.ctl = clients.controller({...clientsOptions, onChange: acctPaint, live: () => !!acct.problem && !disposed});
        void acct.ctl.refresh({quiet: true});
      }
      acctPaint();
    }
    async function acctAction(id, provider) {
      if (!acct.ctl || acct.busy) return;
      acct.error = '';
      if (id === 'use_other') {
        const other = acct.problem?.other;
        if (!other || typeof useOther !== 'function') return;
        acct.busy = 'use_other'; acctPaint();
        try { await useOther(other.id); }
        catch (problem) { acct.error = (problem && problem.message) || `Couldn’t switch ${label}. Try again.`; }
        finally { acct.busy = ''; if (!disposed) { acctPaint(); onChange(); } }
        return;
      }
      if (id !== 'cancel_connect') acct.attempted = true;
      await acct.ctl.run(provider, id);
      if (!disposed) onChange();
    }

    // `note`: a line of the agent's own text ({text, title}) shown after the step while it works; the step alone otherwise, and
    // while a question waits for the owner (the line written before the question says nothing about the wait).
    function update(session, {note = null} = {}) {
      last = session;
      const managed = managedOf(session);
      el.hidden = !managed;
      if (!managed) { for (const [, card] of cards) card.node.remove(); cards.clear(); stopping = false; acctSync(null); clearOffer(); el.hidden = true; return; }
      acctSync(managed.accountProblem);
      if (managed.open) clearOffer();
      const info = activity(session);
      const said = info.text && note?.text && !managed.pending.length ? note : null;
      line.hidden = !info.text; lineText.textContent = said ? `${info.text} · ${said.text}` : info.text;
      lineText.title = said ? said.title || said.text : '';
      stop.hidden = !info.stop || !stopHere; stop.disabled = stopping; stop.title = stopping ? 'Stopping…' : `Stop what ${label} is doing`;
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
      el.hidden = !(cards.size || lineHere() || !notice.hidden || !accountBox.hidden);
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
      if (clients && clients.signInEvent(event, (provider, code) => { acct.attempted = true; void acct.ctl?.run(provider, 'submit_code', {code}); })) return;
      const account = event.target.closest('[data-account-action]');
      if (account) { if (!account.disabled) void acctAction(account.dataset.accountAction, account.dataset.provider); return; }
      if (event.target.closest('.mchat-notice-action')) { const run = choose; clearOffer(); if (run) run(); return; }
      const button = event.target.closest('[data-answer]');
      const node = button?.closest('[data-request-id]');
      if (node) void answer(node.dataset.requestId);
    });
    // Shown outside the block, the activity line takes its own Stop press, and the press goes no further.
    line.addEventListener('click', event => {
      if (line.parentNode === el || !event.target.closest('.mchat-stop')) return;
      event.stopPropagation?.(); void interrupt();
    });
    // Enter in the sign-in code field sends it, like its button.
    el.addEventListener('keydown', event => {
      if (clients) clients.signInEvent(event, (provider, code) => { acct.attempted = true; void acct.ctl?.run(provider, 'submit_code', {code}); });
    });
    // Enter in a typed answer sends the card, like its button.
    el.addEventListener('keydown', event => {
      if (event.key !== 'Enter' || event.isComposing || !event.target.matches?.('[data-q-other]')) return;
      event.preventDefault();
      const node = event.target.closest('[data-request-id]');
      if (node) void answer(node.dataset.requestId);
    });
    return {el, line, update, offer, clearOffer, stop: () => interrupt(), get stopping() { return stopping; }, dispose() { disposed = true; acctStop(); el.remove(); line.remove(); cards.clear(); }, get session() { return last; }};
  }

  return {managedOf, closed, canOpen, working, summary, startHint, emptyText, placeholder, unavailableText, activity, refusalText, isRetryable, newConversationOffer,
    questionList, cardHtml, questionDecision, createBar, escapeHtml};
})();
if (typeof module !== 'undefined') module.exports = agentRuntime;
