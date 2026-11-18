// Claude Code and Codex, set up from the dashboard: one row per product, one button that moves Download, Connect,
// Ready. Qorqbot downloads each product and opens its sign-in page in the owner's browser; nobody opens a terminal.
// Shown in Settings (Agents) and, when neither product is both installed and signed in, as a screen after the Qorqbot
// sign-in and before the walkthrough (`preflight`, called from main.js).
// Server: GET /root/agent-clients -> {claude, codex}; POST /root/agent-clients {provider, action} -> the same view,
// where action is download, cancel_download, connect or cancel_connect (qorqut/owner_settings.py).
// `rowState`, `hasReady`, `needsPoll` and `controller` are pure or DOM-free (tested with node --test, which also runs
// `mount` and `preflight` against a small fake DOM).
const agentClients = (() => {
  const PROVIDERS = ['claude', 'codex'];
  const NAMES = {claude: 'Claude Code', codex: 'Codex'};
  // Where the owner signs in, said the way the sign-in page says it.
  const ACCOUNTS = {claude: 'your Claude account', codex: 'your ChatGPT account'};
  // Where to get it by hand, offered only when Qorqbot cannot download it for this computer.
  const SITES = {claude: 'claude.com/code', codex: 'github.com/openai/codex'};
  // Read every 2 s for the first 15 minutes of a download or sign-in, then every 15 s for as long as it goes on (a big
  // download on a slow link): the row never freezes. Coming back to the tab reads at the fast rate again.
  const POLL_MS = 2000, SLOW_MS = 15000, POLL_FAILURES = 5, POLL_LIMIT = 450;
  // The first read before the walkthrough waits this long; the dashboard never waits longer for this screen.
  const FIRST_READ_MS = 5000;
  const MANAGED_NOTE = 'Agents you start from now on use this copy.';
  const LATER_KEY = 'qorqbot:agent-setup-later';
  // What a button says while its request is on the way, so it never sits disabled without a reason.
  const BUSY = {download: 'Starting…', connect: 'Opening your browser…', cancel_download: 'Cancelling…', cancel_connect: 'Cancelling…'};

  const item = (view, provider) => (view && view[provider]) || {};
  const phase = it => (it.install && it.install.phase) || 'not_downloaded';
  const signingIn = it => !!(it.installed && it.sign_in && it.sign_in.state === 'connecting');
  const ready = it => !!(it.installed && it.signed_in);
  // One sentence from server text: no stray trailing dots, one full stop.
  // Server text is shown only when it is text: a missing or odd value never prints as "undefined" or "null".
  const str = value => (typeof value === 'string' || typeof value === 'number') ? String(value).trim() : '';
  const sentence = text => { const clean = str(text).replace(/[.\s]+$/, ''); return clean ? clean + '.' : ''; };
  const percent = it => {
    const value = it.install && it.install.progress;
    return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(100, Math.round(value))) : null;
  };

  // What one row shows. `pending` is the action already sent for this row and not yet answered; `error` the last
  // failed request. Returns {provider, name, key, tone, pill, detail, note, progress, indeterminate, actions, error}.
  function rowState(provider, it, {pending = null, error = ''} = {}) {
    it = it || {};
    const name = NAMES[provider], account = ACCOUNTS[provider];
    const row = {provider, name, key: '', tone: 'warn', pill: '', detail: '', note: '', progress: null, indeterminate: false, actions: [], error: error || ''};
    // `label` is what the button says; `aria` names the product too, for a screen reader.
    const ARIA = {download: `Download ${name}`, connect: `Connect ${name}`, cancel_download: `Cancel the ${name} download`,
      cancel_connect: `Cancel signing in to ${name}`, refresh: `Check again for ${name}`};
    const act = (id, label, kind = 'primary') => row.actions.push({id, label, kind, aria: id === 'download' && row.key === 'error' ? `Try again to download ${name}` : ARIA[id]});
    if (signingIn(it)) {
      Object.assign(row, {key: 'connecting', pill: 'Signing in', tone: 'info',
        detail: 'Finish signing in in the browser window that opened. This page updates when you are done.'});
      act('cancel_connect', 'Cancel', 'quiet');
    } else if (phase(it) === 'downloading' && !it.installed) {
      const progress = percent(it);
      Object.assign(row, {key: 'downloading', pill: 'Downloading', tone: 'info', progress, indeterminate: progress === null,
        detail: `Downloading ${name}. Keep Qorqbot open until it finishes.`});
      act('cancel_download', 'Cancel', 'quiet');
    } else if (ready(it)) {
      const info = it.account || {}, email = str(info.email), plan = str(info.plan), version = str(it.version);
      // Signed in with an API key there is no email to show.
      const who = email ? ` as ${email}${plan ? ` (${plan})` : ''}` : info.method === 'api_key' ? ' with an API key' : '';
      Object.assign(row, {key: 'ready', pill: 'Ready', tone: 'ok', detail: `Signed in${who}.${version ? ` Version ${version}.` : ''}`,
        // An agent terminal opened before the download finished keeps the copy it started with.
        note: it.source === 'managed' ? MANAGED_NOTE : ''});
    } else if (it.installed) {
      const failed = it.sign_in && it.sign_in.state === 'failed';
      const why = failed ? str(it.sign_in.reason).replace(/[.\s]+$/, '') : '';
      Object.assign(row, {key: 'connect', pill: 'Sign in needed', tone: 'warn',
        detail: failed ? `Signing in did not finish${why ? ` (${why})` : ''}. Press Connect to try again.`
          : `${name} is installed. Sign in with ${account} to finish.`});
      act('connect', 'Connect');
    } else if (phase(it) === 'unsupported') {
      Object.assign(row, {key: 'unsupported', pill: 'Not available', tone: 'warn',
        detail: `${sentence(it.install.message) || `Qorqbot cannot download ${name} for this computer.`} Install it yourself from ${SITES[provider]}, then press Check again.`});
      act('refresh', 'Check again', 'quiet');
    } else if (phase(it) === 'error') {
      Object.assign(row, {key: 'error', pill: 'Download failed', tone: 'warn',
        detail: `${sentence(it.install.message) || 'The download did not finish.'} Check your internet connection, then try again.`});
      act('download', 'Try again');
    } else if (phase(it) === 'ready') {
      // Downloaded, and the server has not said so yet: the next read does.
      Object.assign(row, {key: 'finishing', pill: 'Finishing', tone: 'info', indeterminate: true, detail: `Almost done. Getting ${name} ready…`});
    } else {
      // The server says why when the copy on this computer cannot be used (too old): that comes before the Download action.
      Object.assign(row, {key: 'download', pill: 'Not installed', tone: 'warn',
        detail: sentence(it.install && it.install.message) || `Qorqbot downloads ${name} for you. You do not need a terminal.`});
      act('download', 'Download');
    }
    if (pending) {
      row.actions = row.actions.map(action => ({...action, busy: action.id === pending, disabled: true,
        label: action.id === pending ? (BUSY[pending] || '…') : action.label}));
      if (!row.actions.some(action => action.busy)) row.actions.push({id: pending, label: BUSY[pending] || '…', kind: 'quiet', busy: true, disabled: true});
    }
    return row;
  }
  const rows = (view, state = {}) => PROVIDERS.map(provider =>
    rowState(provider, item(view, provider), {pending: state.pending && state.pending[provider], error: state.errors && state.errors[provider]}));
  const hasReady = view => PROVIDERS.some(provider => ready(item(view, provider)));
  // A download, a sign-in or a finishing download is under way: the view is read again until it settles.
  const needsPoll = view => PROVIDERS.some(provider => {
    const it = item(view, provider);
    return signingIn(it) || (!it.installed && ['downloading', 'ready'].includes(phase(it)));
  });

  const defaultRead = async () => coreData(await api('/root/agent-clients'));
  const defaultAct = async (provider, action) => coreData(await post('/root/agent-clients', {provider, action}));

  // Holds the view, sends actions and keeps reading while something is under way. No DOM: `onChange(state)` repaints.
  // Answers that arrive after a newer request are dropped, so an old read never undoes a newer answer.
  function controller({read = defaultRead, act = defaultAct, onChange = () => {}, live = () => true, view = null,
    delay = POLL_MS, slow = SLOW_MS, schedule = setTimeout, cancel = clearTimeout} = {}) {
    const state = {view, loading: !view, loadError: '', errors: {}, pending: {}};
    let timer = null, seq = 0, failures = 0, polls = 0, stopped = false;
    const emit = () => { if (!stopped && live()) onChange(state); };
    const settle = () => {
      cancel(timer); timer = null;
      if (stopped || !live() || !state.view || !needsPoll(state.view)) return;
      timer = schedule(() => refresh({quiet: true}), polls++ >= POLL_LIMIT ? slow : delay);
    };
    async function refresh({quiet = false} = {}) {
      // Rows that have left the page (or were stopped) read nothing more.
      if (stopped || !live()) return state.view;
      const mine = ++seq;
      if (!quiet) { state.loading = !state.view; emit(); }
      try {
        const next = await read();
        if (mine !== seq || stopped) return state.view;
        failures = 0; state.view = next; state.loading = false; state.loadError = '';
      } catch (error) {
        if (mine !== seq || stopped) return state.view;
        state.loading = false;
        // A read that fails while something is under way is tried again a few times before the owner is told.
        if (!quiet || ++failures >= POLL_FAILURES) state.loadError = (error && error.message) || 'Could not check Claude Code and Codex.';
        else { emit(); cancel(timer); timer = schedule(() => refresh({quiet: true}), (polls >= POLL_LIMIT ? slow : delay) * 2); return state.view; }
      }
      emit(); settle();
      return state.view;
    }
    async function run(provider, action) {
      if (action === 'refresh') return refresh();
      polls = 0; failures = 0;
      state.pending[provider] = action; state.errors[provider] = ''; emit();
      try {
        const next = await act(provider, action);
        // Reads started before this answer are older than it.
        seq++;
        if (!stopped) { state.view = next; state.loadError = ''; }
      } catch (error) {
        state.errors[provider] = (error && error.message) || `That did not work. Try again.`;
      } finally { delete state.pending[provider]; }
      emit(); settle();
      return state.view;
    }
    // The tab is visible again: read now and go back to the fast rate while something is still under way.
    const resume = () => {
      if (stopped || !live() || !state.view || !needsPoll(state.view)) return Promise.resolve(state.view);
      polls = 0; failures = 0;
      return refresh({quiet: true});
    };
    const stop = () => { stopped = true; cancel(timer); timer = null; };
    return {state, refresh, run, resume, stop};
  }

  const buttonHtml = (row, action) => `<button type="button" class="${action.kind === 'quiet' ? 'quiet' : ''}" data-ac-action="${esc(action.id)}" data-provider="${esc(row.provider)}"${action.disabled ? ' disabled aria-busy="true"' : ''}
    aria-label="${esc(action.busy ? action.label : action.aria || action.label)}">${esc(action.label)}</button>`;
  // Everything that changes the row's shape; the progress bar alone is updated in place so a button keeps focus.
  const signature = row => JSON.stringify([row.key, row.tone, row.pill, row.detail, row.note, row.indeterminate, row.error, row.actions]);
  function rowHtml(row) {
    const bar = row.key === 'downloading' || row.key === 'finishing'
      ? `<div class="ac-progress${row.indeterminate ? ' is-indeterminate' : ''}" role="progressbar" aria-label="${esc(row.name)} download" aria-valuemin="0" aria-valuemax="100"${row.indeterminate ? '' : ` aria-valuenow="${row.progress}"`}><i${row.indeterminate ? '' : ` style="width:${row.progress}%"`}></i></div>
        <p class="ac-percent" data-ac-percent>${row.indeterminate ? '' : row.progress + '%'}</p>` : '';
    return `<div class="ac-main"><div class="ac-head"><span class="ac-name">${esc(row.name)}</span><span class="ac-pill" data-tone="${esc(row.tone)}">${esc(row.pill)}</span></div>
      <p class="ac-detail">${esc(row.detail)}</p>${row.note ? `<p class="ac-note">${esc(row.note)}</p>` : ''}${bar}${row.error ? `<p class="ac-error" role="alert">${esc(row.error)}</p>` : ''}</div>
      <div class="ac-actions">${row.actions.map(action => buttonHtml(row, action)).join('')}</div>`;
  }

  // Draws the two rows into `container` and drives them. `options.view` is a read the caller already has,
  // `options.onChange(view)` is called after every paint, `options.search` makes the rows findable by Settings search.
  function mount(container, options = {}) {
    const root = document.createElement('div');
    root.className = 'ac-list';
    root.innerHTML = '<p class="ac-load" data-ac-load role="status">Checking Claude Code and Codex…</p><div class="ac-rows" data-ac-rows></div><p class="ac-sr" data-ac-sr role="status"></p>';
    container.replaceChildren(root);
    const live = () => root.isConnected;
    const load = root.querySelector('[data-ac-load]'), list = root.querySelector('[data-ac-rows]'), announce = root.querySelector('[data-ac-sr]');
    const seen = new Map(), refocus = new Set();
    function paint(state) {
      const loadFailed = !state.view && state.loadError;
      load.hidden = !!state.view;
      load.innerHTML = loadFailed ? `${esc(state.loadError)} <button type="button" class="quiet" data-ac-reload>Try again</button>` : 'Checking Claude Code and Codex…';
      if (!state.view) return;
      const stale = state.loadError ? `<p class="ac-error" role="alert" data-ac-stale>${esc(state.loadError)} <button type="button" class="quiet" data-ac-reload>Try again</button></p>` : '';
      root.querySelector('[data-ac-stale]')?.remove();
      for (const row of rows(state.view, state)) {
        let el = list.querySelector(`[data-ac-row="${row.provider}"]`);
        if (!el) {
          el = document.createElement('div'); el.className = 'ac-row'; el.dataset.acRow = row.provider;
          if (options.search !== false) el.dataset.search = `${row.name} claude codex agents install download sign in connect account ready`.toLowerCase();
          list.append(el);
        }
        const sig = signature(row);
        if (seen.get(row.provider) !== sig) {
          // Focus stays in the row: a button that is replaced by a busy one hands it to the next enabled button.
          if (el.contains(document.activeElement) && document.activeElement.closest('button')) refocus.add(row.provider);
          const first = !seen.has(row.provider);
          el.dataset.state = row.key; el.dataset.tone = row.tone;
          el.innerHTML = rowHtml(row);
          const target = refocus.has(row.provider) && el.querySelector('[data-ac-action]:not([disabled])');
          if (target) { target.focus({preventScroll: true}); refocus.delete(row.provider); }
          // The change is spoken once; the first paint is not an announcement.
          if (!first) announce.textContent = `${row.name}: ${row.pill}. ${row.detail}`;
          seen.set(row.provider, sig);
        } else if (row.progress !== null) {
          const bar = el.querySelector('.ac-progress');
          if (bar) { bar.setAttribute('aria-valuenow', row.progress); bar.firstElementChild.style.width = row.progress + '%'; }
          const text = el.querySelector('[data-ac-percent]'); if (text) text.textContent = row.progress + '%';
        }
      }
      if (stale) list.insertAdjacentHTML('afterend', stale);
      options.onChange?.(state.view);
      root.dispatchEvent(new CustomEvent('agent-clients-painted', {bubbles: true}));
    }
    const ctl = controller({view: options.view || null, live, onChange: paint, read: options.read, act: options.act,
      delay: options.delay, slow: options.slow, schedule: options.schedule, cancel: options.cancel});
    // Back on this tab: read at once and at the fast rate. The listener goes when the rows leave the page or stop.
    const onVisible = () => {
      if (!live()) { document.removeEventListener('visibilitychange', onVisible); return; }
      if (document.visibilityState === 'visible') void ctl.resume();
    };
    document.addEventListener('visibilitychange', onVisible);
    const stop = () => { ctl.stop(); document.removeEventListener('visibilitychange', onVisible); };
    root.addEventListener('click', event => {
      const button = event.target.closest('[data-ac-action]');
      if (button && !button.disabled) { void ctl.run(button.dataset.provider, button.dataset.acAction); return; }
      if (event.target.closest('[data-ac-reload]')) void ctl.refresh();
    });
    if (options.view) paint(ctl.state);
    // The first read decides whether the rows appear; a poll starts by itself when a download is already under way.
    if (!options.view) void ctl.refresh(); else if (needsPoll(options.view)) void ctl.refresh({quiet: true});
    return {root, refresh: ctl.refresh, stop, state: ctl.state};
  }

  const skippedBefore = () => { try { return sessionStorage.getItem(LATER_KEY) === '1'; } catch (_) { return false; } };
  const rememberSkip = () => { try { sessionStorage.setItem(LATER_KEY, '1'); } catch (_) { /* Asked again next time. */ } };

  // The first read, given at most `ms`: the server can take a while to answer (it may check each product's version).
  async function firstRead(read, ms) {
    let timer;
    const late = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('The check took too long.')), ms); });
    try { return await Promise.race([read(), late]); } finally { clearTimeout(timer); }
  }

  // The screen between the Qorqbot sign-in and the walkthrough. Resolves at once when a product is already Ready, and
  // also, without a screen, when the server cannot be read in time or at all or anything here goes wrong: this never
  // keeps the dashboard from loading, and never rejects. Otherwise resolves when the owner continues or chooses
  // "Set up later". Settings always has the same rows. `options` is for tests: read, act, timeout, schedule, cancel.
  async function preflight({timeout = FIRST_READ_MS, ...options} = {}) {
    try {
      if (skippedBefore()) return null;
      let first;
      try { first = await firstRead(options.read || defaultRead, timeout); } catch (_) { return null; }
      if (!first || typeof first !== 'object') return null;
      if (hasReady(first)) return first;
      return await gateScreen(first, options);
    } catch (_) { return null; }
  }

  function gateScreen(first, options) {
    return new Promise(resolve => {
      let gate = null, panel = null, blocked = [], done = false;
      // Whatever ends the screen (a button, or an error while building it) puts the page back the way it was.
      const finish = view => {
        if (done) return; done = true;
        try { if (panel) panel.stop(); } catch (_) { /* Gone anyway. */ }
        try { if (gate) gate.remove(); } catch (_) { /* Gone anyway. */ }
        blocked.forEach(el => { el.inert = false; });
        resolve(view);
      };
      try {
        gate = document.createElement('div');
        gate.className = 'ac-gate';
        gate.innerHTML = `<section class="onboarding-card ac-gate-card" role="dialog" aria-modal="true" aria-labelledby="ac-gate-title" aria-describedby="ac-gate-lead">
          <h2 id="ac-gate-title">get your agents ready</h2>
          <p id="ac-gate-lead">Your agents work through Claude Code or Codex. Set up one of them below. You only do this once, and you do not need a terminal.</p>
          <div class="ac-gate-rows" data-ac-gate-rows></div>
          <p class="ac-gate-hint" id="ac-gate-hint" data-ac-gate-hint role="status"></p>
          <div class="ac-gate-actions"><button type="button" class="ghost ac-later" data-ac-later>Set up later</button>
            <button type="button" data-ac-continue aria-describedby="ac-gate-hint" disabled>Continue</button></div>
        </section>`;
        document.body.append(gate);
        // Nothing behind the screen can be reached, by mouse or keyboard, until it is gone.
        blocked = [...document.body.children].filter(el => el !== gate && !el.inert);
        blocked.forEach(el => { el.inert = true; });
        const next = gate.querySelector('[data-ac-continue]'), later = gate.querySelector('[data-ac-later]'), hint = gate.querySelector('[data-ac-gate-hint]');
        let last = first;
        const paintGate = view => {
          last = view;
          const ok = hasReady(view), wasOk = !next.disabled;
          next.disabled = !ok; later.hidden = ok;
          hint.textContent = ok ? 'You are set. Continue to your workspace.' : 'Continue unlocks when Claude Code or Codex says Ready.';
          if (ok && !wasOk) next.focus();
        };
        panel = mount(gate.querySelector('[data-ac-gate-rows]'), {...options, view: first, search: false, onChange: paintGate});
        next.onclick = () => finish(last);
        later.onclick = () => { rememberSkip(); finish(last); };
        (gate.querySelector('[data-ac-action]:not([disabled])') || later).focus();
      } catch (_) { finish(null); }
    });
  }

  return {rowState, rows, hasReady, needsPoll, controller, mount, preflight, PROVIDERS, NAMES, POLL_MS, SLOW_MS, POLL_LIMIT, FIRST_READ_MS};
})();
if (typeof module !== 'undefined') module.exports = agentClients;
