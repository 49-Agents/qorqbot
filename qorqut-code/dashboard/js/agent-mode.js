// How agents run on this installation: as managed sessions (the default, recommended) or, as a legacy option, each in its own
// terminal. One setting for the whole installation; only the owner changes it, here in Settings > Model/Provider.
// Server: GET /root/agents/runtime -> {mode, recommended, terminals: {available, reason, detail, agents}, busy, switching, last};
// POST /root/agents/runtime {mode, force?, trust?} -> the same view, or a refusal (turns_running names the agents mid-turn).
// `terminals.trusted` says whether the owner already let Qorqbot mark its agent folders as trusted in Claude Code; when not, the
// confirmation of a switch to Terminals asks for it (`trust: true`), because a start never answers Claude Code's own question.
// `agentMode` is also the page's one answer to "do agents run in terminals?": `mode()` is 'managed' until a read said
// otherwise, so a slow, failed or missing read never shows terminal pieces. `terminals()` guards every terminal-only piece.
// `normalize`, `rowState`, `confirmText` and `controller` are pure or DOM-free (tested with node --test).
const agentMode = (() => {
  const MANAGED = 'managed', TERMINAL = 'terminal';
  const ROUTE = '/root/agents/runtime';
  const LABEL = 'How agents run';
  const CHOICE = {[MANAGED]: 'Managed sessions (recommended)', [TERMINAL]: 'Terminals (legacy)'};
  const SHORT = {[MANAGED]: 'Managed sessions', [TERMINAL]: 'Terminals'};
  const HELP = {
    [MANAGED]: 'Qorqbot starts and stops each agent’s session for you. You only use Chat.',
    [TERMINAL]: 'Each agent runs in its own terminal on this computer, as in earlier versions. Only Claude Code agents can run this way.',
  };
  const CONFIRM = {
    [TERMINAL]: {title: 'Switch to Terminals?', body: 'Every agent’s session ends now. Each agent that is on starts again in its own terminal, in the same conversation. From then on an agent that is not running in its terminal receives nothing until it is started. You can switch back here at any time.'},
    [MANAGED]: {title: 'Switch to Managed sessions?', body: 'Every agent’s terminal ends now. Agents then start by themselves when you or another agent writes to them, in the same conversation. You can switch back here at any time.'},
  };
  const TRUST = {
    label: 'Let Qorqbot mark each agent’s own folder as trusted in Claude Code',
    help: 'Claude Code asks before it works in a new folder. With this, agents start in their terminals without that question. Nothing else is trusted, and you can turn it off in Settings. Without it, no agent is started for you: you open each agent’s Terminal, run claude and answer Claude Code there.',
  };
  const DONE = {[TERMINAL]: 'Agents now run in terminals.', [MANAGED]: 'Agents now run as managed sessions.'};
  const NO_TMUX = 'Terminals need tmux, which is not installed on this computer.';
  const NO_TMUX_NOW = 'Agents are set to run in terminals, but terminals need tmux, which is not installed on this computer. Choose Managed sessions to carry on.';
  const BROKEN = 'Terminals can’t be used on this computer right now.';
  const UNREADABLE = 'Couldn’t read this setting.';
  const SWITCH_FAILED = 'Couldn’t switch. Try again.';
  // Read every 2 s while a switch runs; the failure counter is the same as the Claude Code rows use.
  const POLL_MS = 2000, POLL_FAILURES = 5, FIRST_READ_MS = 1500, RETRY_MS = [2000, 6000];
  // The page reloads after a finished switch so every view is drawn for the new mode; a note stays long enough to be read.
  const RELOAD_MS = 1200, NOTE_RELOAD_MS = 8000;

  const str = value => (typeof value === 'string' || typeof value === 'number') ? String(value).trim() : '';
  const count = value => (typeof value === 'number' && Number.isFinite(value) && value >= 0) ? Math.floor(value) : 0;
  const otherMode = mode => mode === TERMINAL ? MANAGED : TERMINAL;
  // "Ada", "Ada and Atlas", "Ada, Atlas and Bo".
  const listNames = names => names.length < 2 ? (names[0] || '') : names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
  const namesOf = list => (Array.isArray(list) ? list : []).map(entry => str(entry && typeof entry === 'object' ? entry.name || entry.agent_id : entry)).filter(Boolean);
  const sentence = text => { const clean = str(text).replace(/[.\s]+$/, ''); return clean ? clean.slice(0, 400) + '.' : ''; };

  // The server view, made safe: anything that is not exactly "terminal" is managed.
  function normalize(raw) {
    const view = raw && typeof raw === 'object' ? raw : {};
    const terminals = view.terminals && typeof view.terminals === 'object' ? view.terminals : {};
    const switching = view.switching && typeof view.switching === 'object' && (view.switching.to === TERMINAL || view.switching.to === MANAGED)
      ? {to: view.switching.to, step: str(view.switching.step), done: count(view.switching.done), total: count(view.switching.total), current: str(view.switching.current) || null} : null;
    const last = view.last && typeof view.last === 'object' ? {to: view.last.to === TERMINAL ? TERMINAL : view.last.to === MANAGED ? MANAGED : null, ok: view.last.ok !== false, message: str(view.last.message), at: view.last.at || null} : null;
    return {
      mode: view.mode === TERMINAL ? TERMINAL : MANAGED,
      // Only an explicit false asks for the consent: a server that says nothing about it is not asked about.
      terminals: {available: terminals.available === true, reason: str(terminals.reason) || null, detail: str(terminals.detail) || null, agents: namesOf(terminals.agents), trusted: terminals.trusted !== false},
      busy: namesOf(view.busy),
      switching,
      last,
    };
  }

  function whyNoTerminals(terminals) {
    if (terminals.reason === 'codex_agents' && terminals.agents.length)
      return `${listNames(terminals.agents)} ${terminals.agents.length === 1 ? 'uses' : 'use'} Codex. Only Claude Code agents can run in terminals. Set ${terminals.agents.length === 1 ? 'it' : 'them'} to Claude Code under Client, model and effort first.`;
    if (terminals.reason === 'tmux_missing') return NO_TMUX;
    return BROKEN;
  }

  const progressText = switching => {
    const target = SHORT[switching.to];
    return switching.total ? `Switching to ${target}: ${Math.min(switching.done, switching.total)} of ${switching.total}…` : `Switching to ${target}…`;
  };

  // What the row shows. `ui` is {loadError, missing, pending (the mode a request is sending), error, done}.
  // key: hidden | unreadable | ready | switching | done.
  function rowState(rawView, ui = {}) {
    const row = {key: 'ready', label: LABEL, help: '', notice: '', choices: [], progress: null, error: str(ui.error), done: str(ui.done), actions: []};
    if (ui.missing) return {...row, key: 'hidden'};
    if (!rawView) {
      if (ui.loadError) return {...row, key: 'unreadable', notice: UNREADABLE, actions: [{id: 'retry', label: 'Try again'}]};
      return {...row, key: 'hidden'};
    }
    const view = normalize(rawView);
    const available = view.terminals.available;
    const switching = view.switching || (ui.pending ? {to: ui.pending, step: '', done: 0, total: 0, current: null} : null);
    row.help = HELP[view.mode];
    if (view.mode === TERMINAL && !available) row.notice = NO_TMUX_NOW;
    else if (view.mode === MANAGED && !available) row.notice = whyNoTerminals(view.terminals);
    for (const value of [MANAGED, TERMINAL]) {
      row.choices.push({value, label: CHOICE[value], selected: value === view.mode,
        // The reason sits under the row; only the choice that cannot be used is disabled, and everything is while a switch runs.
        disabled: !!switching || (value === TERMINAL && view.mode === MANAGED && !available)});
    }
    if (switching) {
      row.key = 'switching';
      row.progress = {text: progressText(switching), done: switching.done, total: switching.total, indeterminate: !switching.total};
    } else if (row.done) row.key = 'done';
    return row;
  }

  // The two confirmation dialogs' texts. `trust` (a switch to Terminals whose folders are not trusted yet) adds the one checkbox.
  const confirmText = (to, {trust = false} = {}) => ({...CONFIRM[to === TERMINAL ? TERMINAL : MANAGED], confirm: 'Switch', cancel: 'Not now',
    trust: trust && to === TERMINAL ? {...TRUST} : null});
  const busyText = names => {
    const one = names.length === 1;
    return `${names.length ? (one ? `${names[0]} is` : `${listNames(names)} are`) : 'Some agents are'} in the middle of a turn. Wait for ${one ? 'it' : 'them'} to finish, or switch now and cut the turn off.`;
  };
  const busyDialog = names => ({title: 'Agents are working', body: busyText(names), confirm: 'Switch now', cancel: 'Not now'});

  const defaultRead = async () => coreData(await api(ROUTE));
  const defaultAct = async (mode, force, trust) => coreData(await post(ROUTE, {mode, ...(force ? {force: true} : {}), ...(trust ? {trust: true} : {})}));
  const routeMissing = error => !!error && [404, 405].includes(error.httpStatus);
  const hiddenNow = () => typeof document !== 'undefined' && document.visibilityState === 'hidden';

  // Holds the view, sends the switch and keeps reading while one runs. No DOM: `onChange(state)` repaints, `onDone(text)` is
  // called once when a switch this page watched has finished. A switch is watched from the moment it is accepted or seen
  // running (also after a page reload), so its end is always reported as done or as the server's own failure sentence.
  function controller({read = defaultRead, act = defaultAct, onChange = () => {}, onDone = () => {}, live = () => true, view = null,
    delay = POLL_MS, schedule = setTimeout, cancel = clearTimeout, hidden = hiddenNow} = {}) {
    const state = {view: view ? normalize(view) : null, loading: !view, loadError: '', missing: false, pending: null, error: '', done: '', watching: null};
    let timer = null, seq = 0, failures = 0, stopped = false;
    const emit = () => { if (!stopped && live()) onChange(state); };
    const running = () => !!(state.view && state.view.switching) || !!state.watching;
    const settle = () => {
      cancel(timer); timer = null;
      if (stopped || !live() || !running() || hidden()) return;
      timer = schedule(() => { timer = null; if (!hidden()) void refresh({quiet: true}); }, delay);
    };
    // A finished switch: done and reload, or the server's sentence. `view` is the page's new truth either way.
    function finish(next) {
      const target = state.watching; state.watching = null;
      if (next.last && next.last.ok === false) state.error = sentence(next.last.message) || SWITCH_FAILED;
      // A finished switch can carry a note (an agent that could not be started in its terminal): the owner reads that instead.
      else if (next.mode === target) {
        const said = next.last && next.last.ok && next.last.to === target ? sentence(next.last.message) : '';
        const note = said && said !== DONE[target] ? said : '';
        state.done = note || DONE[target]; onDone(state.done, {note: !!note});
      }
      else state.error = SWITCH_FAILED;
    }
    function apply(raw, {fromAct = false} = {}) {
      const next = normalize(raw);
      state.view = next; state.loading = false; state.loadError = ''; state.missing = false;
      if (next.switching) { state.watching = next.switching.to; state.error = ''; state.done = ''; }
      // An accepted request is answered with the running switch; a view without it is judged by the next read.
      else if (state.watching && !fromAct) finish(next);
    }
    async function refresh({quiet = false} = {}) {
      if (stopped || !live()) return state.view;
      const mine = ++seq;
      if (!quiet) { state.loading = !state.view; emit(); }
      try {
        const next = await read();
        if (mine !== seq || stopped) return state.view;
        failures = 0; apply(next);
      } catch (error) {
        if (mine !== seq || stopped) return state.view;
        state.loading = false;
        if (routeMissing(error)) { state.missing = true; state.view = null; state.watching = null; }
        // A read that fails while a switch runs is tried again a few times before the owner is told.
        else if (!quiet || ++failures >= POLL_FAILURES) state.loadError = (error && error.message) || UNREADABLE;
        else { emit(); cancel(timer); timer = schedule(() => refresh({quiet: true}), delay * 2); return state.view; }
      }
      emit(); settle();
      return state.view;
    }
    // Sends the switch. Resolves to {ok} or {refusal: {names, text}} when agents are mid-turn (the caller may ask again with force).
    async function request(to, {force = false, trust = false} = {}) {
      if (state.pending || (state.view && state.view.switching)) return {ok: false, busy: true};
      state.pending = to; state.error = ''; state.done = ''; failures = 0; emit();
      let outcome = {ok: true};
      try {
        const next = await act(to, force, trust);
        seq++;
        if (!stopped) {
          if (next && next.changed === false) { state.pending = null; apply(next); }
          else { state.watching = to; apply(next, {fromAct: true}); }
        }
      } catch (error) {
        if (error && error.code === 'turns_running') {
          const names = namesOf(error.details && error.details.agents);
          const known = names.length ? names : (state.view ? state.view.busy : []);
          outcome = {ok: false, refusal: {names: known, text: busyText(known)}};
        } else {
          state.error = sentence(error && (error.serverMessage || error.message)) || SWITCH_FAILED;
          outcome = {ok: false};
        }
      } finally { state.pending = null; }
      emit(); settle();
      return outcome;
    }
    const resume = () => {
      if (stopped || !live() || !running()) return Promise.resolve(state.view);
      failures = 0;
      return refresh({quiet: true});
    };
    const stop = () => { stopped = true; cancel(timer); timer = null; };
    return {state, refresh, request, resume, stop};
  }

  // ---- the page's answer
  let current = MANAGED, latest = null;
  const mode = () => current;
  const terminals = () => current === TERMINAL;
  const view = () => latest;
  function set(raw) {
    const next = raw ? normalize(raw) : null;
    latest = next;
    const was = current;
    current = next ? next.mode : MANAGED;
    if (was !== current && typeof document !== 'undefined' && typeof CustomEvent === 'function')
      document.dispatchEvent(new CustomEvent('agent-mode-changed', {detail: {mode: current}}));
    return latest;
  }
  const wait = (ms, schedule) => new Promise(resolve => { schedule(resolve, ms); });
  // The one read at boot. Resolves within `timeout` whatever happens and never rejects: a slow or failed read leaves the page
  // managed, and a later answer that says terminal repaints through `agent-mode-changed`. A read that failed (not a missing
  // route) is tried again twice in the background, so a hiccup at boot does not leave a terminal installation looking managed.
  async function load({read = defaultRead, timeout = FIRST_READ_MS, retry = RETRY_MS, schedule = setTimeout} = {}) {
    const reading = (async () => {
      for (let attempt = 0; ; attempt++) {
        try { set(await read()); return; }
        catch (error) {
          if (routeMissing(error) || attempt >= retry.length) { if (routeMissing(error)) set(null); return; }
          await wait(retry[attempt], schedule);
        }
      }
    })().catch(() => {});
    await Promise.race([reading, wait(timeout, schedule)]);
  }

  // ---- the row in Settings
  const signature = row => JSON.stringify([row.key, row.help, row.notice, row.error, row.done, row.actions, row.choices,
    row.progress && [row.progress.text, row.progress.indeterminate]]);
  function rowHtml(row) {
    if (row.key === 'unreadable')
      return `<p class="ac-load" data-am-unreadable role="status">${esc(row.notice)} <button type="button" class="quiet" data-am-retry>Try again</button></p>`;
    const buttons = row.choices.map(choice => `<button type="button" role="radio" aria-checked="${choice.selected}" data-am-choice="${esc(choice.value)}"${choice.disabled ? ' disabled' : ''}${row.notice && choice.value === TERMINAL ? ' aria-describedby="am-notice"' : ''}>${esc(choice.label)}</button>`).join('');
    const bar = row.progress ? `<div class="ac-progress am-progress${row.progress.indeterminate ? ' is-indeterminate' : ''}" role="progressbar" aria-label="Switching" aria-valuemin="0" aria-valuemax="${row.progress.total || 100}"${row.progress.indeterminate ? '' : ` aria-valuenow="${row.progress.done}"`}><i${row.progress.indeterminate ? '' : ` style="width:${Math.round(100 * Math.min(row.progress.done, row.progress.total) / row.progress.total)}%"`}></i></div>` : '';
    return `<div class="st-row-text"><div class="st-label">${esc(row.label)}</div><div class="st-help" data-am-help>${esc(row.help)}</div>
      ${row.progress ? `<p class="am-progress-text" data-am-progress-text>${esc(row.progress.text)}</p>${bar}` : ''}
      ${row.notice ? `<p class="am-notice" id="am-notice" data-am-notice>${esc(row.notice)}</p>` : ''}
      ${row.done ? `<p class="am-done" data-am-done role="status">${esc(row.done)}</p>` : ''}
      ${row.error ? `<p class="ac-error am-error" role="alert" data-am-error>${esc(row.error)}</p>` : ''}</div>
      <div class="st-control"><div class="st-choice am-choice" role="radiogroup" aria-label="${esc(row.label)}">${buttons}</div></div>`;
  }

  // Draws the row into `container` and drives it. Options (all optional): view (a read the caller has), read, act, onChange,
  // openModal, toast, reload, delay, schedule, cancel. A server without the route leaves the container empty and hidden.
  function mount(container, options = {}) {
    const root = document.createElement('div');
    root.className = 'am-list';
    root.innerHTML = '<div class="am-body" data-am-body></div><p class="ac-sr" data-am-sr role="status"></p>';
    container.replaceChildren(root);
    container.style.display = 'none';
    const live = () => root.isConnected;
    const body = root.querySelector('[data-am-body]'), announce = root.querySelector('[data-am-sr]');
    const openModal = options.openModal || (spec => modal(spec));
    const reload = options.reload || (wait => { setTimeout(() => location.reload(), wait); });
    let seen = '', lastKey = '';
    function paint(state) {
      if (state.view) set(state.view);
      const row = rowState(state.view, {loadError: state.loadError, missing: state.missing, pending: state.pending, error: state.error, done: state.done});
      container.style.display = row.key === 'hidden' ? 'none' : '';
      if (row.key === 'hidden') { body.replaceChildren(); seen = ''; lastKey = row.key; return; }
      const sig = signature(row);
      if (sig !== seen) {
        const hadFocus = body.contains(document.activeElement) && document.activeElement.closest('button');
        body.innerHTML = row.key === 'unreadable' ? rowHtml(row) : `<div class="st-row am-row" data-am-row data-state="${esc(row.key)}" data-search="${esc((LABEL + ' ' + HELP[MANAGED] + ' ' + HELP[TERMINAL] + ' managed sessions terminals legacy tmux mode switch recommended').toLowerCase())}">${rowHtml(row)}</div>`;
        if (hadFocus) body.querySelector('button:not([disabled])')?.focus({preventScroll: true});
        // The change is spoken once; the first paint is not an announcement.
        if (seen && row.key !== lastKey) announce.textContent = row.progress ? row.progress.text : row.done || row.error || row.notice;
        seen = sig; lastKey = row.key;
      } else if (row.progress) {
        const text = body.querySelector('[data-am-progress-text]'); if (text) text.textContent = row.progress.text;
      }
      options.onChange?.(state.view);
      root.dispatchEvent(new CustomEvent('agent-mode-painted', {bubbles: true}));
    }
    const ctl = controller({view: options.view || null, live, onChange: paint, read: options.read, act: options.act, delay: options.delay,
      schedule: options.schedule, cancel: options.cancel, hidden: options.hidden,
      onDone: (text, info) => { try { options.toast?.(text); } catch (_) { /* The row says it too. */ } reload(info && info.note ? NOTE_RELOAD_MS : RELOAD_MS); }});
    async function submit(to, force, trust) {
      const outcome = await ctl.request(to, {force, trust});
      if (outcome.refusal) ask(busyDialog(outcome.refusal.names), () => { void submit(to, true, trust); });
    }
    // `onConfirm(trust)`: whether the owner left the trust checkbox ticked (false when the dialog has none).
    function ask(text, onConfirm) {
      const trust = text.trust ? `<label class="am-trust"><input type="checkbox" data-am-trust checked> <span>${esc(text.trust.label)}</span></label>
        <p class="muted am-trust-help">${esc(text.trust.help)}</p>` : '';
      openModal({title: esc(text.title), width: 520, body: `<p>${esc(text.body)}</p>${trust}`, actions: [
        {label: text.cancel, quiet: true, onClick: close => close()},
        {label: text.confirm, primary: true, onClick: (close, el) => {
          const box = el && el.querySelector ? el.querySelector('[data-am-trust]') : null;
          close(); onConfirm(!!(box && box.checked));
        }},
      ]});
    }
    const onVisible = () => {
      if (!live()) { document.removeEventListener('visibilitychange', onVisible); return; }
      if (document.visibilityState === 'visible') void ctl.resume();
    };
    document.addEventListener('visibilitychange', onVisible);
    const stop = () => { ctl.stop(); document.removeEventListener('visibilitychange', onVisible); };
    root.addEventListener('click', event => {
      const button = event.target.closest('[data-am-choice]');
      if (button && !button.disabled) {
        const to = button.dataset.amChoice;
        if (ctl.state.view && to !== ctl.state.view.mode && !ctl.state.pending)
          ask(confirmText(to, {trust: !ctl.state.view.terminals.trusted}), trust => { void submit(to, false, trust); });
        return;
      }
      if (event.target.closest('[data-am-retry]')) void ctl.refresh();
    });
    if (options.view) paint(ctl.state);
    void ctl.refresh({quiet: !!options.view});
    return {root, refresh: ctl.refresh, stop, state: ctl.state};
  }

  return {mode, terminals, view, set, load, normalize, rowState, confirmText, busyText, controller, mount,
    MANAGED, TERMINAL, HELP, CHOICE, DONE, POLL_MS, FIRST_READ_MS};
})();
if (typeof module !== 'undefined') module.exports = agentMode;
