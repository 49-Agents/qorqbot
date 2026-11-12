// Connectors: the services the owner connects so agents can use them (Gmail, Calendar and more).
// `connectorsModel` is pure (unit tested with `node --test biz-dashboard/tests/*.test.js`); `connectorsPanel` renders the
// panel shown in Settings and in the walkthrough. Status comes from /root/connectors (qorqut.connectors).
const connectorsModel = (() => {
  const CLAUDE_CONNECTORS_URL = 'https://claude.ai/settings/connectors';
  const icon = path => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
  const ICONS = {
    gmail: icon('<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M4 7l8 6 8-6"/>'),
    google_calendar: icon('<rect x="3.5" y="5" width="17" height="15" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>'),
    google_drive: icon('<path d="M4 18.5h16M8.5 4.5h7L21 14l-2.5 4.5M8.5 4.5L3 14l2.5 4.5M8.5 4.5L14 14H3"/>'),
    whatsapp: icon('<path d="M20 11.6a8 8 0 0 1-11.8 7L4 20l1.4-4.1A8 8 0 1 1 20 11.6z"/>'),
    notion: icon('<rect x="4" y="3.5" width="16" height="17" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>'),
    slack: icon('<path d="M9 3.5v17M15 3.5v17M3.5 9h17M3.5 15h17"/>'),
  };
  // Owner-facing copy. `does` is what agents can do with it, `why` why it helps, `privacy` what it can see and where it runs.
  const COPY = {
    gmail: {name: 'Gmail', does: 'Ordubashy finds and reads your email, sorts what needs you and drafts replies for you to send.',
      why: 'Most requests arrive by email. This is how your agents keep up with them.',
      privacy: 'You sign in with Google on Google’s own page. Qorqbot never sees your password and keeps no copy of your mail.'},
    google_calendar: {name: 'Google Calendar', does: 'Agents see your schedule, suggest times and add events when you ask.',
      why: 'Planning work around your real day.',
      privacy: 'You sign in with Google on Google’s own page. Qorqbot keeps no copy of your events.'},
    google_drive: {name: 'Google Drive', does: 'Agents find and read your documents and spreadsheets.',
      why: 'Your agents can work from your own files.',
      privacy: 'You sign in with Google on Google’s own page. Qorqbot keeps no copy of your files.'},
    whatsapp: {name: 'WhatsApp', does: 'Ordubashy reads your chats to catch you up. Sending a message always needs your approval.',
      why: 'Catch up on chats without scrolling through them.',
      privacy: 'Runs on this computer and links like WhatsApp on a second device. Your messages stay in a private folder of this installation.'},
    notion: {name: 'Notion', does: 'Agents read and update your pages and databases.', why: 'Keep your notes and wikis in the loop.',
      privacy: 'You sign in on Notion’s own page. Qorqbot keeps no copy of your pages.'},
    slack: {name: 'Slack', does: 'Agents read channels and draft messages.', why: 'Follow team conversations without living in Slack.',
      privacy: 'You sign in on Slack’s own page. Qorqbot keeps no copy of your messages.'},
  };
  const STATUS = {
    connected: {label: 'Connected', tone: 'ok'}, off: {label: 'Off', tone: 'idle'}, not_added: {label: 'Not connected', tone: 'idle'},
    needs_sign_in: {label: 'Needs sign-in', tone: 'warn'}, failed: {label: 'Not working', tone: 'warn'},
    checking: {label: 'Checking…', tone: 'idle'}, unavailable: {label: 'Unavailable', tone: 'warn'},
    unknown: {label: 'Unknown', tone: 'idle'}, not_installed: {label: 'Not installed', tone: 'idle'},
    not_set_up: {label: 'Not set up', tone: 'idle'}, not_paired: {label: 'Not linked', tone: 'warn'},
    coming_soon: {label: 'Coming soon on Mac', tone: 'idle'}, needs_install: {label: 'Needs a quick install', tone: 'warn'},
  };
  const status = item => STATUS[item.status] || STATUS.unknown;
  // What the card's main button does: 'claude' (connect on claude.ai) or 'none'.
  function action(item) {
    if (item.status === 'coming_soon') return {kind: 'none'};
    if (item.kind === 'claude') {
      if (item.status === 'connected') return {kind: 'claude', label: 'Manage', quiet: true};
      if (item.status === 'unavailable' || item.status === 'checking') return {kind: 'none'};
      return {kind: 'claude', label: item.status === 'needs_sign_in' ? 'Sign in' : 'Connect'};
    }
    return {kind: 'none'};
  }
  // Explains a status: {text, tone} where tone 'warn' means the owner has to act; null when the card's own copy is enough.
  function note(item) {
    const text = noteText(item);
    if (!text) return null;
    return {text, tone: ['not_added', 'not_installed', 'unsupported', 'coming_soon', 'checking'].includes(item.status) ? 'info' : 'warn'};
  }
  function noteText(item) {
    if (item.kind === 'claude' && item.status === 'unavailable')
      return item.reason === 'claude_missing' ? 'Install Claude Code and sign in with your Claude account to use Claude’s connectors.'
        : 'Claude Code could not list its connectors. Open a terminal and run “claude” once to sign in.';
    if (item.kind === 'claude' && item.status === 'failed') return 'Claude could not reach it. Open Claude’s connectors and connect it again.';
    if (item.kind === 'claude' && item.status !== 'connected') return 'Claude Code only. Codex can’t use Claude’s connectors.';
    if (item.id === 'whatsapp' && item.status === 'coming_soon') return 'The WhatsApp extension is available for Windows (WSL) and Linux. A Mac version is in progress.';
    if (item.id === 'whatsapp' && item.status === 'not_installed') return 'Comes with the WhatsApp extension, which isn’t installed in this Qorqbot yet.';
    if (item.id === 'whatsapp' && item.status === 'not_set_up') return 'The WhatsApp extension is installed. Link your phone to finish.';
    if (item.id === 'whatsapp' && item.status === 'not_paired') return 'Link your phone: in WhatsApp open Settings, Linked devices, Link a device.';
    return null;
  }
  function summary(items) {
    const recommended = items.filter(item => item.tier === 'recommended');
    const done = recommended.filter(item => item.status === 'connected').length;
    return {done, total: recommended.length, complete: done === recommended.length};
  }
  // Which Claude account agents use, so the owner connects Gmail and others on that same account.
  function account(view) {
    const value = view?.claude_account;
    if (!value) return null;
    if (!value.signed_in) return {tone: 'warn', text: 'Claude Code isn’t signed in. Open Terminal, run “claude” and sign in, then press Check again.'};
    if (value.method !== 'claude.ai') return {tone: 'warn', text: 'Claude Code is signed in with an API key. Claude’s connectors need a Claude account sign-in.'};
    return {tone: 'ok', text: `Your agents use the Claude account ${value.email || 'you signed in with'}. Connect services on claude.ai while signed in to that same account.`};
  }
  return {COPY, ICONS, STATUS, CLAUDE_CONNECTORS_URL, status, action, note, summary, account};
})();
if (typeof module !== 'undefined') module.exports = connectorsModel;

const connectorsPanel = (() => {
  const POLL_MS = 3000;
  const PRIVACY = `<section class="cx-privacy" aria-labelledby="cx-privacy-title">
      <div class="cx-privacy-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.3-7.5 9.5-4.3-1.2-7.5-4.9-7.5-9.5V6z"/><path d="M8.8 12.2l2.2 2.2 4.3-4.6"/></svg></div>
      <div><h2 id="cx-privacy-title">Private by design. Your work stays on this computer.</h2>
      <p>Qorqbot’s servers know only your sign-in email, whether you use it for personal or work, and a daily check that it is running. Never your conversations, files, records or what your agents do.</p>
      <ul><li>Browser sessions stay in each agent’s private local profile. External connection sign-ins happen on the provider’s page.</li>
      <li>External connections are optional and managed in their own services.</li>
      <li>What you ask your agents goes to Claude or Codex under your own account, the same as when you use them directly.</li></ul></div>
    </section>`;
  function card(item) {
    const copy = connectorsModel.COPY[item.id] || {name: item.id, does: ''};
    const status = connectorsModel.status(item), action = connectorsModel.action(item), note = connectorsModel.note(item);
    const button = action.kind === 'none' ? '' : `<button type="button" class="${action.quiet ? 'quiet' : ''}" data-cx-action="${esc(action.kind)}" data-cx-id="${esc(item.id)}">${esc(action.label)}</button>`;
    const steps = copy.steps ? `<ol>${copy.steps.map(step => `<li>${esc(step)}</li>`).join('')}</ol>` : '';
    return `<article class="cx-card" data-cx-card="${esc(item.id)}" data-tone="${status.tone}">
      <div class="cx-card-head"><span class="cx-icon">${connectorsModel.ICONS[item.id] || ''}</span>
        <div class="cx-card-title"><h3>${esc(copy.name)}</h3><span class="cx-status" data-tone="${status.tone}">${esc(status.label)}</span></div>
        ${button}</div>
      <p class="cx-does">${esc(copy.does)}</p>
      ${note ? `<p class="cx-note" data-tone="${note.tone}">${esc(note.text)}</p>` : ''}
      <details class="cx-more"><summary>Why it helps and what it can see</summary>
        <p><b>Why:</b> ${esc(copy.why || '')}</p><p><b>Privacy:</b> ${esc(copy.privacy || '')}</p>
        ${copy.needs ? `<p><b>Needs:</b> ${esc(copy.needs)}</p>` : ''}${steps}</details>
    </article>`;
  }
  // Settings shows each connector as one compact row; the walkthrough and the cards above share the same model.
  function row(item) {
    const copy = connectorsModel.COPY[item.id] || {name: item.id, does: ''};
    const status = connectorsModel.status(item), action = connectorsModel.action(item), note = connectorsModel.note(item);
    const button = action.kind === 'none' ? '' : `<button type="button" class="${action.quiet ? 'quiet' : ''}" data-cx-action="${esc(action.kind)}" data-cx-id="${esc(item.id)}">${esc(action.label)}</button>`;
    const steps = copy.steps ? `<ol>${copy.steps.map(step => `<li>${esc(step)}</li>`).join('')}</ol>` : '';
    const search = [copy.name, copy.does, copy.why, status.label, item.id.replace('_', ' ')].join(' ').toLowerCase();
    return `<div class="cx-row" data-cx-card="${esc(item.id)}" data-tone="${status.tone}" data-search="${esc(search)}">
      <span class="cx-icon">${connectorsModel.ICONS[item.id] || ''}</span>
      <div class="cx-row-main">
        <div class="cx-row-title"><h3>${esc(copy.name)}</h3><span class="cx-status" data-tone="${status.tone}">${esc(status.label)}</span></div>
        <p class="cx-row-does">${esc(copy.does)}</p>
        ${note ? `<p class="cx-note" data-tone="${note.tone}">${esc(note.text)}</p>` : ''}
        <details class="cx-more"><summary>What it can see</summary>
          <p><b>Why:</b> ${esc(copy.why || '')}</p><p><b>Privacy:</b> ${esc(copy.privacy || '')}</p>
          ${copy.needs ? `<p><b>Needs:</b> ${esc(copy.needs)}</p>` : ''}${steps}</details>
      </div>
      <div class="cx-row-action">${button}</div>
    </div>`;
  }
  // Shown under the cards once the owner has gone to claude.ai, so returning to the dashboard shows what to do next.
  const CLAUDE_STEPS = `<div class="cx-claude-steps" data-cx-claude-steps hidden role="status">
      <h3>Finish in Claude</h3>
      <ol><li>On claude.ai, open <b>Settings</b>, then <b>Connectors</b>.</li>
      <li>Find the service and press <b>Connect</b>.</li>
      <li>Sign in with your Google (or other) account on its own page and allow access.</li>
      <li>Come back here. Qorqbot checks again by itself.</li></ol>
      <p class="muted small">Signing in gives your Claude account access to that one service. You can remove it at any time in Claude’s connectors or in your Google account’s security settings.</p>
    </div>`;
  // `options.onChange(view)` is called after every read; `options.compact` drops the privacy banner (the walkthrough shows its own).
  function mount(container, options = {}) {
    const root = document.createElement('div');
    root.className = 'cx-panel' + (options.rows ? ' cx-panel-rows' : '');
    root.innerHTML = `${options.compact ? '' : PRIVACY}
      <div class="cx-browser" data-cx-browser aria-live="polite"><div class="cx-browser-copy"><span class="cx-eyebrow">Included with Qorqbot</span>
        <h3>Agent browser <span class="cx-status" data-cx-browser-status>Checking…</span></h3>
        <p>Your agents use their own private browser to work on websites for you. It sets itself up.</p>
        <p class="cx-browser-detail" data-cx-browser-detail>Checking browser readiness…</p></div>
        <button type="button" class="quiet" data-cx-browser-retry hidden>Retry setup</button></div>
      <section class="cx-accounts" data-cx-accounts hidden>
        <h3>Websites your agents can use as you</h3>
        <p>To connect a website, ask Ordubashy in Chat, for example “connect my Slack”. It opens Chrome on this computer and you sign in yourself. Your password goes only to that website. Qorqbot never sees or stores it, and your agent keeps just the signed-in session.</p>
        <p class="cx-signin-note" data-cx-signin-note role="status" hidden></p>
        <p class="cx-accounts-empty" data-cx-accounts-empty hidden>No websites connected yet.</p>
        <p class="cx-error" data-cx-accounts-error role="alert" hidden></p>
        <div class="cx-rows" data-cx-accounts-list hidden></div>
        <p class="cx-accounts-limit">A signed-in agent can act on that website as you, including sending messages. Only its instructions tell it to ask you first, and Qorqbot can’t block those actions in code. Claude connectors below do require your approval.</p>
      </section>
      <p class="cx-error" data-cx-error role="alert" hidden></p>
      <details class="cx-direct" data-cx-direct><summary>Claude connectors <span class="cx-direct-count" data-cx-direct-count></span></summary>
      <div class="cx-head"><div>${options.rows ? '' : '<h2>Optional connections</h2>'}<p>Direct connections for agents running in Claude Code. Connect opens claude.ai, where you allow access on the service’s own page.</p></div>
        <div class="cx-progress" data-cx-progress aria-live="polite"></div></div>
      <p class="cx-account" data-cx-account hidden></p>
      <h3 class="cx-group">Recommended for email</h3><div class="${options.rows ? 'cx-rows' : 'cx-grid'}" data-cx-tier="recommended"><p class="muted small">Loading connections…</p></div>
      <h3 class="cx-group">Also useful</h3><div class="${options.rows ? 'cx-rows' : 'cx-grid'}" data-cx-tier="suggested"></div>
      ${CLAUDE_STEPS}
      <p class="cx-foot muted small"><button type="button" class="cx-link" data-cx-refresh>Check again</button></p></details>`;
    container.appendChild(root);
    let view = null, browserView = null, timer = 0, browserTimer = 0, browserChecks = 0, awaitingClaude = false;
    let accountsView = null, accountsTimer = 0;
    const SIGN_IN_STATUS = {true: ['Session saved', 'ok'], false: ['No session', 'warn'], null: ['Status unavailable while the browser is off', '']};
    function paintAccounts() {
      const section = root.querySelector('[data-cx-accounts]'), agents = accountsView?.agents;
      section.hidden = !agents?.length;
      if (section.hidden) return;
      const busy = agents.find(agent => agent.signing_in), note = root.querySelector('[data-cx-signin-note]');
      note.hidden = !busy;
      if (busy) note.textContent = `Finish signing in to ${busy.signing_in} in the Chrome window, then close that window. ${busy.name}’s browser is paused until you do.`;
      const rows = agents.flatMap(agent => agent.sites.map(item => ({agent, item})));
      const list = root.querySelector('[data-cx-accounts-list]');
      list.hidden = !rows.length;
      root.querySelector('[data-cx-accounts-empty]').hidden = !!rows.length || !!busy;
      list.innerHTML = rows.map(({agent, item}) => {
        const [label, tone] = SIGN_IN_STATUS[item.session_saved ?? null];
        return `<div class="cx-row cx-account-row" data-cx-site="${esc(item.site)}"><div class="cx-row-main"><div class="cx-row-title"><h3>${esc(item.site)}</h3><span class="cx-status" data-tone="${tone}">${label}</span></div>${agents.length > 1 ? `<p class="cx-row-does">${esc(agent.name)}</p>` : ''}</div>
          <div class="cx-row-action"><button type="button" class="quiet" data-cx-site-action="sign_out" data-cx-agent="${esc(agent.agent_id)}" data-cx-site-name="${esc(item.site)}"${busy ? ' disabled' : ''}>${item.session_saved === false ? 'Remove' : 'Sign out'}</button></div></div>`;
      }).join('');
      clearTimeout(accountsTimer);
      // Ordubashy starts sign-ins from Chat, so this list has to notice them on its own.
      if (live()) accountsTimer = setTimeout(() => { loadAccounts(); if (busy) loadBrowser(); }, busy ? 2500 : 5000);
      root.dispatchEvent(new CustomEvent('connectors-painted', {bubbles: true}));
    }
    const accountsError = text => { const el = root.querySelector('[data-cx-accounts-error]'); el.textContent = text || ''; el.hidden = !text; };
    async function loadAccounts(change) {
      try {
        const next = coreData(change ? await post('/root/browser-accounts', change) : await api('/root/browser-accounts'));
        if (!live()) return true;
        const wasBusy = accountsView?.agents?.some(agent => agent.signing_in);
        accountsView = next; accountsError(''); paintAccounts();
        // The session list only settles once the agent's browser is back after the window closes.
        if (wasBusy && !next.agents?.some(agent => agent.signing_in)) { clearTimeout(accountsTimer); accountsTimer = setTimeout(() => { loadAccounts(); loadBrowser(); }, 1500); }
        return true;
      } catch (failure) {
        if (live()) accountsError(failure.message || 'Website sign-ins could not be loaded.');
        return false;
      }
    }
    const live = () => root.isConnected;
    const error = text => { const el = root.querySelector('[data-cx-error]'); el.textContent = text || ''; el.hidden = !text; };
    function paintBrowser() {
      const status = root.querySelector('[data-cx-browser-status]');
      const detail = root.querySelector('[data-cx-browser-detail]');
      const retry = root.querySelector('[data-cx-browser-retry]');
      if (!browserView) return;
      const ready = browserView.ready;
      status.textContent = ready ? 'Ready' : !browserView.tool_installed ? 'Tool missing' :
        !browserView.chrome_installed ? 'Chrome needed' : 'Setup needed';
      status.dataset.tone = ready ? 'ok' : 'warn';
      const agents = browserView.agents || [];
      const problems = agents.filter(agent => agent.state !== 'healthy')
        .map(agent => agents.length > 1 ? `${agent.name}: ${agent.explanation}` : agent.explanation);
      detail.textContent = ready ? `Ready for ${agents.map(agent => agent.name).join(', ')}.` :
        !browserView.tool_installed ? 'The tool agents use to control their browser isn’t installed. Quit and reopen Qorqbot to install it again.' :
        !browserView.chrome_installed ? 'Agents need Google Chrome to browse, and it isn’t installed on this computer. Install it from google.com/chrome, then press Retry setup.' :
        problems.join(' ') || 'Preparing agent browsers…';
      retry.hidden = ready || !browserView.tool_installed;
      retry.disabled = !browserView.chrome_installed;
      root.dispatchEvent(new CustomEvent('browser-setup-painted', {bubbles: true}));
    }
    async function loadBrowser(retry = false) {
      const button = root.querySelector('[data-cx-browser-retry]');
      button.disabled = true;
      try {
        browserView = coreData(retry ? await post('/root/browser-setup', {action: 'retry'}) : await api('/root/browser-setup'));
        if (live()) paintBrowser();
      } catch (failure) {
        if (live()) root.querySelector('[data-cx-browser-detail]').textContent = failure.message || 'Could not check browser setup.';
      } finally {
        if (live()) button.disabled = false;
        clearTimeout(browserTimer);
        if (live() && !browserView?.ready && browserChecks++ < 12)
          browserTimer = setTimeout(() => loadBrowser(), 5000);
      }
    }
    function paint() {
      if (!view) return;
      for (const tier of ['recommended', 'suggested'])
        root.querySelector(`[data-cx-tier="${tier}"]`).innerHTML = view.connectors.filter(item => item.tier === tier).map(options.rows ? row : card).join('');
      const progress = connectorsModel.summary(view.connectors);
      root.querySelector('[data-cx-progress]').innerHTML = `<span class="cx-progress-text">${progress.done} of ${progress.total} recommended connected</span>
        <span class="cx-progress-bar" aria-hidden="true"><i style="width:${Math.round(progress.done / progress.total * 100)}%"></i></span>`;
      const connected = view.connectors.filter(item => item.status === 'connected').length;
      root.querySelector('[data-cx-direct-count]').textContent = connected ? `${connected} connected` : '';
      root.querySelector('[data-cx-claude-steps]').hidden = !awaitingClaude;
      const signIn = connectorsModel.account(view), line = root.querySelector('[data-cx-account]');
      line.hidden = !signIn; if (signIn) { line.textContent = signIn.text; line.dataset.tone = signIn.tone; }
      const refresh = root.querySelector('[data-cx-refresh]');
      refresh.disabled = view.checking; refresh.textContent = view.checking ? 'Checking…' : 'Check again';
      options.onChange?.(view);
      root.dispatchEvent(new CustomEvent('connectors-painted', {bubbles: true}));
    }
    function schedule() {
      clearTimeout(timer);
      if (live() && view?.checking) timer = setTimeout(() => load(), POLL_MS);
    }
    async function load(change) {
      if (!live()) return;
      try {
        view = coreData(change ? await post('/root/connectors', change) : await api('/root/connectors'));
        if (!live()) return;
        error(''); paint();
      } catch (failure) { if (live()) error(failure.message || 'Connectors could not be loaded.'); }
      schedule();
    }
    root.addEventListener('click', async event => {
      const siteAction = event.target.closest('[data-cx-site-action]');
      if (siteAction) {
        siteAction.disabled = true;
        if (!await loadAccounts({action: siteAction.dataset.cxSiteAction, agent_id: siteAction.dataset.cxAgent, site: siteAction.dataset.cxSiteName})) siteAction.disabled = false;
        return;
      }
      if (event.target.closest('[data-cx-browser-retry]')) { await loadBrowser(true); return; }
      if (event.target.closest('[data-cx-refresh]')) { await load({refresh: true}); return; }
      const button = event.target.closest('[data-cx-action]');
      if (!button) return;
      const item = view?.connectors.find(entry => entry.id === button.dataset.cxId);
      if (!item) return;
      if (button.dataset.cxAction === 'claude') {
        window.open(connectorsModel.CLAUDE_CONNECTORS_URL, '_blank', 'noopener,noreferrer');
        awaitingClaude = true; paint();
      }
    });
    // Returning from claude.ai: check again by itself, once per return.
    const focus = () => { if (awaitingClaude && live() && document.visibilityState === 'visible') load({refresh: true}); };
    window.addEventListener('focus', focus);
    document.addEventListener('visibilitychange', focus);
    const watch = new MutationObserver(() => {
      if (live()) return;
      clearTimeout(timer); clearTimeout(browserTimer); clearTimeout(accountsTimer); watch.disconnect();
      window.removeEventListener('focus', focus); document.removeEventListener('visibilitychange', focus);
    });
    watch.observe(container.ownerDocument.body, {childList: true, subtree: true});
    load(); loadBrowser(); loadAccounts();
    return {root, reload: () => load({refresh: true}), get view() { return view; }};
  }
  return {mount, PRIVACY};
})();
