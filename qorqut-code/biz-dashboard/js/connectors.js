// Connections: the agent browser, the websites agents use as the owner, and Qorqbot's extensions (OpenRouter, treg, WhatsApp).
// `connectorsModel` is pure (unit tested with `node --test biz-dashboard/tests/*.test.js`); `connectorsPanel` renders the
// panel shown in Settings. Extension status comes from /root/connectors (qorqut.connectors).
const connectorsModel = (() => {
  const icon = path => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
  const ICONS = {
    openrouter: icon('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/>'),
    whatsapp: icon('<path d="M20 11.6a8 8 0 0 1-11.8 7L4 20l1.4-4.1A8 8 0 1 1 20 11.6z"/>'),
    treg: icon('<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><path d="M17 14v6M14 17h6"/>'),
  };
  // Owner-facing copy. `does` is what agents can do with it, `why` why it helps, `privacy` what it can see and where it runs.
  const COPY = {
    whatsapp: {name: 'WhatsApp', does: 'Ordubashy reads your chats to catch you up. Sending a message always needs your approval.',
      why: 'Catch up on chats without scrolling through them.',
      privacy: 'Runs on this computer and links like WhatsApp on a second device. Your messages stay in a private folder of this installation.'},
    // A Qorqbot extension: the owner signs in with their own OpenRouter account (or pastes a key) and Chat gets voice input.
    openrouter: {name: 'OpenRouter', does: 'Speak your Chat messages instead of typing them. Press the microphone in Chat, or Ctrl+Shift+Space, talk, and your words appear in the message box for you to check and send.',
      why: 'Talking is faster than typing for long instructions. One OpenRouter account covers it, with no separate speech service to set up.',
      privacy: 'Only what you record goes to OpenRouter and the speech model it runs, and only when you press the microphone. Recordings are not kept on this computer. Your key is kept in a private file on this computer and is never shown to your agents.',
      needs: 'Your own OpenRouter account with a small balance. OpenRouter charges your account for each recording, usually a fraction of a cent.',
      steps: ['Press Connect. OpenRouter opens in this tab.', 'Sign in or create an account there, set a spending limit if you like, and approve.', 'You come back here connected. If you already have an OpenRouter key, press Paste a key instead.']},
    // A Qorqbot extension: the owner pastes a treg key here and the tools reach every agent through Qorqbot itself.
    treg: {name: 'treg', does: 'Agents look up live data when a task needs it: search results, keyword and backlink numbers, company and contact details, social profiles and web pages. Lookups run on their own, each capped at $0.10. treg tools that post, send or buy stay switched off.',
      why: 'One key gives your agents thousands of data tools, so they don’t need a separate account for each service. It works with Claude Code and Codex alike.',
      privacy: 'What your agents look up goes to treg and the data provider it picks. Your key is kept in a private file on this computer and is never shown to your agents.',
      needs: 'A treg account and a key from it. treg charges per lookup from a balance you top up there.',
      steps: ['Create an account at treg.to and add a small balance.', 'Create an API key in your treg account and copy it.', 'Press Add key here, paste the key and save.']},
  };
  const STATUS = {
    connected: {label: 'Connected', tone: 'ok'}, unknown: {label: 'Unknown', tone: 'idle'},
    not_installed: {label: 'Not installed', tone: 'idle'}, not_set_up: {label: 'Not set up', tone: 'idle'},
    not_paired: {label: 'Not linked', tone: 'warn'}, coming_soon: {label: 'Coming soon on Mac', tone: 'idle'},
  };
  const status = item => STATUS[item.status] || STATUS.unknown;
  // What the card's main button does: 'connect' (sign in with the service; `key` then names a second button that opens the
  // card's key field), 'key' (open the card's key field) or 'none'.
  function action(item) {
    if (item.status === 'coming_soon') return {kind: 'none'};
    if (item.id === 'openrouter') {
      if (item.status === 'connected') return {kind: 'key', label: 'Change key', quiet: true};
      return item.status === 'not_set_up' ? {kind: 'connect', label: 'Connect', key: 'Paste a key'} : {kind: 'none'};
    }
    if (item.id === 'treg') {
      if (item.status === 'connected') return {kind: 'key', label: 'Change key', quiet: true};
      return item.status === 'not_set_up' ? {kind: 'key', label: 'Add key'} : {kind: 'none'};
    }
    return {kind: 'none'};
  }
  // Explains a status: {text, tone} where tone 'warn' means the owner has to act; null when the card's own copy is enough.
  function note(item) {
    const text = noteText(item);
    if (!text) return null;
    return {text, tone: ['not_installed', 'coming_soon'].includes(item.status) ? 'info' : 'warn'};
  }
  function noteText(item) {
    if (item.id === 'openrouter' && item.status === 'not_installed') return 'Comes with the OpenRouter extension, which isn’t part of this Qorqbot yet.';
    if (item.id === 'openrouter' && item.status === 'not_set_up') return 'Connect your OpenRouter account to turn on voice input.';
    if (item.id === 'treg' && item.status === 'not_installed') return 'Comes with the treg extension, which isn’t part of this Qorqbot yet.';
    if (item.id === 'treg' && item.status === 'not_set_up') return 'Add your treg key to turn it on.';
    if (item.id === 'whatsapp' && item.status === 'coming_soon') return 'The WhatsApp extension is available for Windows (WSL) and Linux. A Mac version is in progress.';
    if (item.id === 'whatsapp' && item.status === 'not_installed') return 'Comes with the WhatsApp extension, which isn’t installed in this Qorqbot yet.';
    if (item.id === 'whatsapp' && item.status === 'not_set_up') return 'The WhatsApp extension is installed. Link your phone to finish.';
    if (item.id === 'whatsapp' && item.status === 'not_paired') return 'Link your phone: in WhatsApp open Settings, Linked devices, Link a device.';
    return null;
  }
  return {COPY, ICONS, STATUS, status, action, note};
})();
if (typeof module !== 'undefined') module.exports = connectorsModel;

const connectorsPanel = (() => {
  const PRIVACY = `<section class="cx-privacy" aria-labelledby="cx-privacy-title">
      <div class="cx-privacy-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.3-7.5 9.5-4.3-1.2-7.5-4.9-7.5-9.5V6z"/><path d="M8.8 12.2l2.2 2.2 4.3-4.6"/></svg></div>
      <div><h2 id="cx-privacy-title">Private by design. Your work stays on this computer.</h2>
      <p>Qorqbot’s servers know only your sign-in email, whether you use it for personal or work, and a daily check that it is running. Never your conversations, files, records or what your agents do.</p>
      <ul><li>Browser sessions stay in each agent’s private local profile.</li>
      <li>Extensions are optional. Each one says what it can see before you turn it on.</li>
      <li>What you ask your agents goes to Claude or Codex under your own account, the same as when you use them directly.</li></ul></div>
    </section>`;
  // The key field of a connector whose button is 'key'. It stays closed until that button is pressed; the saved key is
  // never sent back, so the field always starts empty.
  function keyForm(item, action) {
    if (action.kind !== 'key' && !action.key) return '';
    const name = esc((connectorsModel.COPY[item.id] || {name: item.id}).name);
    return `<form class="cx-key" data-cx-key="${esc(item.id)}" hidden>
      <label><span>Your ${name} key</span><input type="password" name="key" autocomplete="off" spellcheck="false" placeholder="Paste the key from ${name}" required></label>
      <div class="cx-key-actions"><button type="submit">Save key</button>
        ${item.status === 'connected' ? '<button type="button" class="quiet" data-cx-key-remove>Remove key</button>' : ''}
        <button type="button" class="quiet" data-cx-key-cancel>Cancel</button></div>
      <p class="cx-note" data-cx-key-error hidden></p></form>`;
  }
  // The card's main button, and beside a 'connect' button the quiet one that opens the key field.
  function buttons(item, action) {
    if (action.kind === 'none') return '';
    const one = (kind, label, quiet) => `<button type="button" class="${quiet ? 'quiet' : ''}" data-cx-action="${esc(kind)}" data-cx-id="${esc(item.id)}">${esc(label)}</button>`;
    return one(action.kind, action.label, action.quiet) + (action.key ? one('key', action.key, true) : '');
  }
  function card(item) {
    const copy = connectorsModel.COPY[item.id] || {name: item.id, does: ''};
    const status = connectorsModel.status(item), action = connectorsModel.action(item), note = connectorsModel.note(item);
    const button = buttons(item, action);
    const steps = copy.steps ? `<ol>${copy.steps.map(step => `<li>${esc(step)}</li>`).join('')}</ol>` : '';
    return `<article class="cx-card" data-cx-card="${esc(item.id)}" data-tone="${status.tone}">
      <div class="cx-card-head"><span class="cx-icon">${connectorsModel.ICONS[item.id] || ''}</span>
        <div class="cx-card-title"><h3>${esc(copy.name)}</h3><span class="cx-status" data-tone="${status.tone}">${esc(status.label)}</span></div>
        ${button}</div>
      <p class="cx-does">${esc(copy.does)}</p>
      ${note ? `<p class="cx-note" data-tone="${note.tone}">${esc(note.text)}</p>` : ''}
      ${keyForm(item, action)}
      <details class="cx-more"><summary>Why it helps and what it can see</summary>
        <p><b>Why:</b> ${esc(copy.why || '')}</p><p><b>Privacy:</b> ${esc(copy.privacy || '')}</p>
        ${copy.needs ? `<p><b>Needs:</b> ${esc(copy.needs)}</p>` : ''}${steps}</details>
    </article>`;
  }
  // Settings shows each extension as one compact row; the cards above share the same model.
  function row(item) {
    const copy = connectorsModel.COPY[item.id] || {name: item.id, does: ''};
    const status = connectorsModel.status(item), action = connectorsModel.action(item), note = connectorsModel.note(item);
    const button = buttons(item, action);
    const steps = copy.steps ? `<ol>${copy.steps.map(step => `<li>${esc(step)}</li>`).join('')}</ol>` : '';
    const search = [copy.name, copy.does, copy.why, status.label, item.id.replace('_', ' ')].join(' ').toLowerCase();
    return `<div class="cx-row" data-cx-card="${esc(item.id)}" data-tone="${status.tone}" data-search="${esc(search)}">
      <span class="cx-icon">${connectorsModel.ICONS[item.id] || ''}</span>
      <div class="cx-row-main">
        <div class="cx-row-title"><h3>${esc(copy.name)}</h3><span class="cx-status" data-tone="${status.tone}">${esc(status.label)}</span></div>
        <p class="cx-row-does">${esc(copy.does)}</p>
        ${note ? `<p class="cx-note" data-tone="${note.tone}">${esc(note.text)}</p>` : ''}
        ${keyForm(item, action)}
        <details class="cx-more"><summary>What it can see</summary>
          <p><b>Why:</b> ${esc(copy.why || '')}</p><p><b>Privacy:</b> ${esc(copy.privacy || '')}</p>
          ${copy.needs ? `<p><b>Needs:</b> ${esc(copy.needs)}</p>` : ''}${steps}</details>
      </div>
      <div class="cx-row-action">${button}</div>
    </div>`;
  }
  // `options.onChange(view)` is called after every read; `options.compact` drops the privacy banner (the walkthrough shows its own).
  function mount(container, options = {}) {
    const root = document.createElement('div');
    root.className = 'cx-panel' + (options.rows ? ' cx-panel-rows' : '');
    root.innerHTML = `${options.compact ? '' : PRIVACY}
      <div class="cx-browser-area" data-cx-browser-area><div class="cx-browser" data-cx-browser aria-live="polite"><div class="cx-browser-copy"><span class="cx-eyebrow">Included with Qorqbot</span>
        <h3>Agent browser <span class="cx-status" data-cx-browser-status>Checking…</span></h3>
        <p>Your agents use their own private browser to work on websites for you. It sets itself up.</p>
        <p class="cx-browser-detail" data-cx-browser-detail>Checking browser readiness…</p></div>
        <button type="button" class="quiet" data-cx-browser-retry hidden>Retry setup</button></div>
      <section class="cx-accounts" data-cx-choice hidden>
        <h3>Which browser each agent uses</h3>
        <p>An agent’s own browser has its own website sign-ins. The Shared browser is one browser for every agent set to it: they share its sign-ins and each works in its own tabs. Switching restarts the browser; nothing is signed out or deleted.</p>
        <p class="cx-error" data-cx-choice-error role="alert" hidden></p>
        <div class="cx-rows" data-cx-choice-list></div>
      </section>
      <section class="cx-accounts" data-cx-accounts hidden>
        <h3>Websites your agents can use as you</h3>
        <p>To connect a website, ask Ordubashy in Chat, for example “connect my Slack”. It opens Chrome on this computer and you sign in yourself: your password goes only to that website, and your agent keeps just the signed-in session. On a computer without a screen, Ordubashy asks you for the password and signs in for you.</p>
        <p class="cx-signin-note" data-cx-signin-note role="status" hidden></p>
        <p class="cx-accounts-empty" data-cx-accounts-empty hidden>No websites connected yet.</p>
        <p class="cx-error" data-cx-accounts-error role="alert" hidden></p>
        <div class="cx-rows" data-cx-accounts-list hidden></div>
        <p class="cx-accounts-limit">A signed-in agent can act on that website as you, including sending messages. Only its instructions tell it to ask you first, and Qorqbot can’t block those actions in code.</p>
      </section></div>
      <p class="cx-error" data-cx-error role="alert" hidden></p>
      <section class="cx-extensions" data-cx-extensions>
        <${options.rows ? 'h3' : 'h2'}>Extensions</${options.rows ? 'h3' : 'h2'}>
        <p>Extra tools that run inside Qorqbot, so they work the same for agents in Claude Code and Codex.</p>
        <div class="${options.rows ? 'cx-rows' : 'cx-grid'}" data-cx-list><p class="muted small">Loading extensions…</p></div>
      </section>`;
    container.appendChild(root);
    let view = null, browserView = null, browserTimer = 0, browserChecks = 0, choiceHtml = '';
    // A status read that was already on its way, or returns while a choice is being saved, still shows the browser from before the
    // choice; it must not replace the answer to the choice or redraw the old value in the menu.
    let choosing = false, browserEpoch = 0;
    let accountsView = null, accountsTimer = 0;
    const BROWSER_CHOICES = {own: 'Own browser', shared: 'Shared browser'};
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
    const chromeDownloading = () => !!browserView && !browserView.chrome_installed &&
      ['waiting', 'downloading'].includes(browserView.managed_chrome?.state);
    function paintBrowser() {
      const status = root.querySelector('[data-cx-browser-status]');
      const detail = root.querySelector('[data-cx-browser-detail]');
      const retry = root.querySelector('[data-cx-browser-retry]');
      if (!browserView) return;
      const ready = browserView.ready;
      // Qorqbot downloads its own Chrome. Until it is there (and no Chrome is installed) the card says what the download
      // is doing; "Chrome needed" is left for a computer where Qorqbot's own Chrome cannot be used.
      const ownChrome = browserView.managed_chrome || {};
      const downloading = chromeDownloading();
      status.textContent = ready ? 'Ready' : !browserView.tool_installed ? 'Tool missing' :
        downloading ? 'Downloading' : !browserView.chrome_installed ? (ownChrome.state === 'failed' ? 'Download failed' : 'Chrome needed') : 'Setup needed';
      status.dataset.tone = ready ? 'ok' : 'warn';
      const agents = browserView.agents || [];
      const problems = agents.filter(agent => agent.state !== 'healthy')
        .map(agent => agents.length > 1 ? `${agent.name}: ${agent.explanation}` : agent.explanation);
      detail.textContent = ready ? `Ready for ${agents.map(agent => agent.name).join(', ')}.` :
        !browserView.tool_installed ? 'The tool agents use to control their browser isn’t installed. Quit and reopen Qorqbot to install it again.' :
        !browserView.chrome_installed ? ownChrome.explanation || 'Agents need Google Chrome to browse, and it isn’t installed on this computer. Install it from google.com/chrome, then press Retry setup.' :
        problems.join(' ') || 'Preparing agent browsers…';
      retry.hidden = ready || !browserView.tool_installed;
      paintChoice(agents.filter(agent => agent.agent_id && (BROWSER_CHOICES[agent.browser] || agent.browser === 'damaged')));
      retry.disabled = downloading || (!browserView.chrome_installed && !ownChrome.state);
      root.dispatchEvent(new CustomEvent('browser-setup-painted', {bubbles: true}));
    }
    // The list is rebuilt only when it changes, so a status refresh does not close a menu the owner has open.
    function paintChoice(agents) {
      const section = root.querySelector('[data-cx-choice]'), list = root.querySelector('[data-cx-choice-list]');
      section.hidden = !agents.length;
      const html = agents.map(agent => `<div class="cx-row cx-account-row" data-cx-choice-row="${esc(agent.agent_id)}"><div class="cx-row-main"><div class="cx-row-title"><h3>${esc(agent.name)}</h3></div></div>
        <div class="cx-row-action"><select data-cx-choice-agent="${esc(agent.agent_id)}" aria-label="Browser for ${esc(agent.name)}">${
          agent.browser === 'damaged' ? '<option value="" selected disabled>Choose…</option>' : ''}${
          Object.entries(BROWSER_CHOICES).map(([value, label]) => `<option value="${value}"${value === agent.browser ? ' selected' : ''}>${label}</option>`).join('')}</select></div></div>`).join('');
      if (html !== choiceHtml) list.innerHTML = choiceHtml = html;
    }
    async function choose(select) {
      const problem = root.querySelector('[data-cx-choice-error]');
      const menus = root.querySelectorAll('[data-cx-choice-agent]');
      problem.hidden = true;
      menus.forEach(menu => { menu.disabled = true; });
      choosing = true; browserEpoch++;
      try {
        browserView = coreData(await post('/root/browser-setup', {action: 'choose', agent_id: select.dataset.cxChoiceAgent, browser: select.value}));
      } catch (failure) {
        if (live()) { problem.textContent = failure.message || 'The browser could not be switched.'; problem.hidden = false; }
      } finally {
        choosing = false; browserEpoch++;
        choiceHtml = '';   // rebuild: the menus are enabled again and show the browser each agent really uses
        if (live()) paintBrowser();
      }
    }
    async function loadBrowser(retry = false) {
      const button = root.querySelector('[data-cx-browser-retry]');
      button.disabled = true;
      const epoch = browserEpoch;
      try {
        const next = coreData(retry ? await post('/root/browser-setup', {action: 'retry'}) : await api('/root/browser-setup'));
        if (live() && !choosing && epoch === browserEpoch) { browserView = next; paintBrowser(); }
      } catch (failure) {
        if (live()) root.querySelector('[data-cx-browser-detail]').textContent = failure.message || 'Could not check browser setup.';
      } finally {
        if (live()) button.disabled = false;
        clearTimeout(browserTimer);
        // A download takes minutes on a slow connection, so the card keeps checking for as long as it runs.
        if (live() && !browserView?.ready && (chromeDownloading() || browserChecks++ < 12))
          browserTimer = setTimeout(() => loadBrowser(), 5000);
      }
    }
    function paint() {
      if (!view) return;
      // A repaint must not close a key field the owner is typing in.
      const open = root.querySelector('[data-cx-key]:not([hidden])');
      const typed = open && {id: open.dataset.cxKey, value: open.elements.key.value, focused: document.activeElement === open.elements.key};
      root.querySelector('[data-cx-list]').innerHTML = view.connectors.map(options.rows ? row : card).join('');
      const again = typed && root.querySelector(`[data-cx-key="${typed.id}"]`);
      if (again) { again.hidden = false; again.elements.key.value = typed.value; if (typed.focused) again.elements.key.focus(); }
      options.onChange?.(view);
      root.dispatchEvent(new CustomEvent('connectors-painted', {bubbles: true}));
    }
    async function load() {
      if (!live()) return;
      try {
        view = coreData(await api('/root/connectors'));
        if (!live()) return;
        error(''); paint();
      } catch (failure) { if (live()) error(failure.message || 'Extensions could not be loaded.'); }
    }
    root.addEventListener('click', async event => {
      const siteAction = event.target.closest('[data-cx-site-action]');
      if (siteAction) {
        siteAction.disabled = true;
        if (!await loadAccounts({action: siteAction.dataset.cxSiteAction, agent_id: siteAction.dataset.cxAgent, site: siteAction.dataset.cxSiteName})) siteAction.disabled = false;
        return;
      }
      if (event.target.closest('[data-cx-browser-retry]')) { await loadBrowser(true); return; }
      const keyForm = event.target.closest('[data-cx-key]');
      if (keyForm && event.target.closest('[data-cx-key-cancel]')) { keyForm.reset(); keyForm.hidden = true; return; }
      if (keyForm && event.target.closest('[data-cx-key-remove]')) { await saveKey(keyForm, null); return; }
      const button = event.target.closest('[data-cx-action]');
      if (!button) return;
      const item = view?.connectors.find(entry => entry.id === button.dataset.cxId);
      if (!item) return;
      if (button.dataset.cxAction === 'key') {
        const form = root.querySelector(`[data-cx-key="${item.id}"]`);
        form.hidden = !form.hidden;
        if (!form.hidden) form.elements.key.focus();
      }
      // Signing in with the service leaves this page; it comes back connected (js/voice-input.js finishes the sign-in).
      if (button.dataset.cxAction === 'connect' && typeof openrouterConnect !== 'undefined') {
        button.disabled = true;
        try { await openrouterConnect.start(); }
        catch (failure) { if (live()) { error(failure.message || 'OpenRouter could not be opened.'); button.disabled = false; } }
      }
    });
    // Saves the pasted key (or removes the saved one when `key` is null) through the connector's own extension route.
    async function saveKey(form, key) {
      const problem = form.querySelector('[data-cx-key-error]'), buttons = form.querySelectorAll('button');
      problem.hidden = true;
      buttons.forEach(button => { button.disabled = true; });
      try {
        await post(`/root/extensions/${encodeURIComponent(form.dataset.cxKey)}/key`, {key});
        form.reset(); form.hidden = true;
        await load();
      } catch (failure) {
        if (!live()) return;
        problem.textContent = failure.message || 'The key could not be saved.'; problem.hidden = false;
        buttons.forEach(button => { button.disabled = false; });
      }
    }
    root.addEventListener('change', event => {
      const select = event.target.closest('[data-cx-choice-agent]');
      if (select) choose(select);
    });
    root.addEventListener('submit', async event => {
      const form = event.target.closest('[data-cx-key]');
      if (!form) return;
      event.preventDefault();
      await saveKey(form, form.elements.key.value.trim());
    });
    // An extension can be set up outside this page (WhatsApp is linked from a terminal), so returning here reads again.
    const focus = () => { if (live() && document.visibilityState === 'visible') load(); };
    window.addEventListener('focus', focus);
    document.addEventListener('visibilitychange', focus);
    const watch = new MutationObserver(() => {
      if (live()) return;
      clearTimeout(browserTimer); clearTimeout(accountsTimer); watch.disconnect();
      window.removeEventListener('focus', focus); document.removeEventListener('visibilitychange', focus);
    });
    watch.observe(container.ownerDocument.body, {childList: true, subtree: true});
    load(); loadBrowser(); loadAccounts();
    return {root, reload: () => load(), get view() { return view; }};
  }
  return {mount, PRIVACY};
})();
