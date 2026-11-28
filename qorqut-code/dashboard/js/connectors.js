// Connections: the agent browser, the websites agents use as the owner, and Qorqbot's extensions (OpenRouter, treg, WhatsApp).
// `connectorsModel` is pure (unit tested with `node --test dashboard/tests/*.test.js`); `connectorsPanel` renders the
// panel shown in Settings. Extension status comes from /root/connectors (qorqut.connectors); WhatsApp is linked through its
// own owner route, /root/extensions/whatsapp/setup, and shows its code from /root/extensions/whatsapp/qr. An extension that
// follows the contract (`kind` `contract`) is drawn and set up by js/extension-cards.js.
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
    linking: {label: 'Linking', tone: 'warn'}, finishing: {label: 'Finishing', tone: 'warn'},
    failed: {label: 'Needs attention', tone: 'warn'}, unsupported: {label: 'Not available', tone: 'idle'},
  };
  // What the WhatsApp card shows beside its code while linking.
  const LINK_STEPS = ['On your phone, open WhatsApp.', 'Go to Settings, then Linked devices, then Link a device.', 'Point your phone at this code.'];
  // The WhatsApp card reads its own route again every few seconds while the code is shown or the setup is finishing.
  const polls = item => item.id === 'whatsapp' && ['linking', 'finishing'].includes(item.status);
  const status = item => STATUS[item.status] || STATUS.unknown;
  // What the card's main button does: 'connect' (sign in with the service; `key` then names a second button that opens the
  // card's key field), 'key' (open the card's key field) or 'none'.
  function action(item) {
    if (item.id === 'whatsapp') {
      if (item.status === 'not_set_up') return {kind: 'wa-link', label: 'Link phone'};
      if (item.status === 'failed') return {kind: 'wa-link', label: 'Try again'};
      if (item.status === 'linking') return {kind: 'wa-cancel', label: 'Cancel', quiet: true};
      if (item.status === 'connected' && item.send_ready === false) return {kind: 'wa-link', label: 'Finish setup', quiet: true};
      return {kind: 'none'};
    }
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
    return {text, tone: ['not_installed', 'unsupported', 'finishing'].includes(item.status) ? 'info' : 'warn'};
  }
  function noteText(item) {
    if (item.id === 'openrouter' && item.status === 'not_installed') return 'Comes with the OpenRouter extension, which isn’t part of this Qorqbot yet.';
    if (item.id === 'openrouter' && item.status === 'not_set_up') return 'Connect your OpenRouter account to turn on voice input.';
    if (item.id === 'treg' && item.status === 'not_installed') return 'Comes with the treg extension, which isn’t part of this Qorqbot yet.';
    if (item.id === 'treg' && item.status === 'not_set_up') return 'Add your treg key to turn it on.';
    if (item.id === 'whatsapp') {
      if (item.status === 'not_installed') return 'Comes with the WhatsApp extension, which isn’t part of this Qorqbot.';
      if (item.status === 'unsupported') return 'WhatsApp can’t be linked on this computer yet.';
      if (item.status === 'not_set_up') return 'Link your phone to turn it on, or ask Ordubashy in Chat to set it up.';
      if (item.status === 'finishing') return item.note || 'Your phone is linked. Qorqbot is finishing the setup by itself.';
      if (item.status === 'failed') return item.note || 'Linking didn’t finish. Try again.';
      if (item.status === 'connected' && item.send_ready === false) return item.note || 'Reading works. Sending through your approval isn’t set up yet.';
    }
    return null;
  }
  return {COPY, ICONS, STATUS, LINK_STEPS, status, action, note, polls};
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
  // While WhatsApp is linking: the code (read again as it changes) and the three steps on the phone. `version` makes each
  // read of the code a new address, so a rotated code is fetched instead of a stale one.
  function linkBlock(item, version) {
    if (item.id !== 'whatsapp' || item.status !== 'linking') return '';
    const code = item.qr
      ? `<img class="cx-wa-code" data-cx-wa-code src="/api/root/extensions/whatsapp/qr?v=${version}" alt="WhatsApp link code" width="192" height="192">`
      : '<p class="cx-wa-wait" data-cx-wa-code>Preparing the code…</p>';
    return `<div class="cx-wa-link" data-cx-wa-link>${code}
      <ol>${connectorsModel.LINK_STEPS.map(step => `<li>${esc(step)}</li>`).join('')}</ol></div>`;
  }
  // The card's main button, and beside a 'connect' button the quiet one that opens the key field.
  function buttons(item, action) {
    if (action.kind === 'none') return '';
    const one = (kind, label, quiet) => `<button type="button" class="${quiet ? 'quiet' : ''}" data-cx-action="${esc(kind)}" data-cx-id="${esc(item.id)}">${esc(label)}</button>`;
    return one(action.kind, action.label, action.quiet) + (action.key ? one('key', action.key, true) : '');
  }
  function card(item, version = 0) {
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
      ${linkBlock(item, version)}
      ${keyForm(item, action)}
      <details class="cx-more"><summary>Why it helps and what it can see</summary>
        <p><b>Why:</b> ${esc(copy.why || '')}</p><p><b>Privacy:</b> ${esc(copy.privacy || '')}</p>
        ${copy.needs ? `<p><b>Needs:</b> ${esc(copy.needs)}</p>` : ''}${steps}</details>
    </article>`;
  }
  // Settings shows each extension as one compact row; the cards above share the same model.
  function row(item, version = 0) {
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
        ${linkBlock(item, version)}
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
      <div class="cx-browser-area" data-cx-browser-area>${options.rows ? '<div class="record-section-head"><h4>Agent browser</h4></div>' : ''}<div class="cx-browser" data-cx-browser aria-live="polite"><div class="cx-browser-copy">
        <h3>Agent browser <span class="cx-status" data-cx-browser-status>Checking…</span></h3>
        <p>Included with Qorqbot. Your agents share one private browser to work on websites for you: they share its website sign-ins and each works in its own tabs. It sets itself up.</p>
        <p class="cx-browser-detail" data-cx-browser-detail>Checking browser readiness…</p></div>
        <button type="button" class="quiet" data-cx-browser-retry hidden>Retry setup</button></div>
      <section class="cx-accounts" data-cx-accounts hidden>
        ${options.rows ? '<div class="record-section-head"><h4>Websites your agents can use as you</h4></div>' : '<h3>Websites your agents can use as you</h3>'}
        <p>To connect a website, ask Ordubashy in Chat, for example “connect my Slack”. It opens the website’s sign-in page in the agent browser, and you sign in yourself through the agent’s Browser tab: your password goes only to that website, and your agent keeps just the signed-in session.</p>
        <p class="cx-accounts-empty" data-cx-accounts-empty hidden>No websites connected yet.</p>
        <p class="cx-error" data-cx-accounts-error role="alert" hidden></p>
        <div class="cx-rows" data-cx-accounts-list hidden></div>
        <p class="cx-accounts-limit">A signed-in agent can act on that website as you, including sending messages. Only its instructions tell it to ask you first, and Qorqbot can’t block those actions in code.</p>
      </section></div>
      <p class="cx-error" data-cx-error role="alert" hidden></p>
      <section class="cx-extensions" data-cx-extensions>
        ${options.rows ? '<div class="record-section-head"><h4>Extensions</h4></div>' : '<h2>Extensions</h2>'}
        <p>Extra tools that run inside Qorqbot, so they work the same for agents in Claude Code and Codex.</p>
        <div class="${options.rows ? 'cx-rows' : 'cx-grid'}" data-cx-list><p class="muted small">Loading extensions…</p></div>
      </section>`;
    container.appendChild(root);
    let view = null, browserView = null, browserTimer = 0, browserChecks = 0;
    let accountsView = null, accountsTimer = 0, waTimer = 0, waVersion = 0, restartTimer = 0, loadTimer = 0;
    // Each read of a list is numbered: only the answer to the newest one is painted, so an older answer that comes later
    // (a read started by focus beside the timer's) never paints over a newer one.
    let listRead = 0, accountsRead = 0;
    // A request that got no answer from Qorqbot at all, as opposed to one it answered with a refusal.
    const unreached = failure => !failure?.code || failure.code === 'network_unavailable' || failure.code === 'invalid_response'
      || failure.httpStatus >= 500;
    const SIGN_IN_STATUS = {true: ['Session saved', 'ok'], false: ['No session', 'warn'], null: ['Status unavailable while the browser is off', '']};
    function paintAccounts() {
      const section = root.querySelector('[data-cx-accounts]'), rows = accountsView?.sites;
      section.hidden = !rows;
      if (section.hidden) return;
      const list = root.querySelector('[data-cx-accounts-list]');
      list.hidden = !rows.length;
      root.querySelector('[data-cx-accounts-empty]').hidden = !!rows.length;
      list.innerHTML = rows.map(item => {
        const [label, tone] = SIGN_IN_STATUS[item.session_saved ?? null];
        return `<div class="cx-row cx-account-row" data-cx-site="${esc(item.site)}"><div class="cx-row-main"><div class="cx-row-title"><h3>${esc(item.site)}</h3><span class="cx-status" data-tone="${tone}">${label}</span></div></div>
          <div class="cx-row-action"><button type="button" class="quiet" data-cx-site-action="sign_out" data-cx-site-name="${esc(item.site)}">${item.session_saved === false ? 'Remove' : 'Sign out'}</button></div></div>`;
      }).join('');
      clearTimeout(accountsTimer);
      // Ordubashy starts sign-ins from Chat, so this list has to notice them on its own.
      if (live()) accountsTimer = setTimeout(loadAccounts, 5000);
      root.dispatchEvent(new CustomEvent('connectors-painted', {bubbles: true}));
    }
    const accountsError = text => { const el = root.querySelector('[data-cx-accounts-error]'); el.textContent = text || ''; el.hidden = !text; };
    async function loadAccounts(change) {
      const mine = ++accountsRead;
      try {
        const next = coreData(change ? await post('/root/browser-accounts', change) : await api('/root/browser-accounts'));
        if (!live() || mine !== accountsRead) return true;   // a newer read was asked for meanwhile: its answer counts
        accountsView = next; accountsError(''); paintAccounts();
        return true;
      } catch (failure) {
        if (live() && mine === accountsRead) {
          accountsError(failure.message || 'Website sign-ins could not be loaded.');
          // While Qorqbot gives no answer the list keeps being read, so the sentence goes once it answers again.
          if (!change && unreached(failure)) { clearTimeout(accountsTimer); accountsTimer = setTimeout(loadAccounts, 5000); }
        }
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
      // The agents' browser runs on the installed Google Chrome; without it the card says "Chrome needed" (the server's text
      // says Qorqbot carries on by itself once it is installed). "Downloading" is only for a browser that already runs on
      // Qorqbot's own build while that build is fetched again.
      const ownChrome = browserView.managed_chrome || {};
      const downloading = chromeDownloading();
      status.textContent = ready ? 'Ready' : !browserView.tool_installed ? 'Tool missing' :
        downloading ? 'Downloading' : !browserView.chrome_installed ? (ownChrome.state === 'failed' ? 'Download failed' : 'Chrome needed') : 'Setup needed';
      status.dataset.tone = ready ? 'ok' : 'warn';
      const agents = browserView.agents || [];
      detail.textContent = ready ? `Ready for ${agents.map(agent => agent.name).join(', ')}.` :
        !browserView.tool_installed ? 'The tool agents use to control their browser isn’t installed. Quit and reopen Qorqbot to install it again.' :
        !browserView.chrome_installed ? ownChrome.explanation || 'Qorqbot needs Google Chrome so your agents can work on the web, and it isn’t installed on this computer. Install it from google.com/chrome. Qorqbot carries on by itself once it is installed.' :
        browserView.explanation || 'Preparing the agent browser…';
      retry.hidden = ready || !browserView.tool_installed;
      retry.disabled = downloading || (!browserView.chrome_installed && !ownChrome.state);
      root.dispatchEvent(new CustomEvent('browser-setup-painted', {bubbles: true}));
    }
    async function loadBrowser(retry = false) {
      const button = root.querySelector('[data-cx-browser-retry]');
      button.disabled = true;
      try {
        const next = coreData(retry ? await post('/root/browser-setup', {action: 'retry'}) : await api('/root/browser-setup'));
        if (live()) { browserView = next; paintBrowser(); }
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
      // The same for the fields form of a contract extension.
      const openFields = root.querySelector('[data-ext-form]:not([hidden])');
      const fields = openFields && {id: openFields.dataset.extForm, focused: document.activeElement?.name,
        values: [...openFields.querySelectorAll('input')].map(input => [input.name, input.value])};
      root.querySelector('[data-cx-list]').innerHTML = view.connectors.map(item => item.kind === 'contract'
        ? (typeof extensionCards === 'undefined' ? '' : extensionCards.render(item, {rows: options.rows, esc}))
        : (options.rows ? row : card)(item, waVersion)).join('');
      const again = typed && root.querySelector(`[data-cx-key="${typed.id}"]`);
      if (again) { again.hidden = false; again.elements.key.value = typed.value; if (typed.focused) again.elements.key.focus(); }
      const fieldsAgain = fields && root.querySelector(`[data-ext-form="${CSS.escape(fields.id)}"]`);
      if (fieldsAgain) {
        fieldsAgain.hidden = false;
        for (const [name, value] of fields.values) { const input = fieldsAgain.elements[name]; if (input) input.value = value; }
        if (fields.focused && fieldsAgain.elements[fields.focused]) fieldsAgain.elements[fields.focused].focus();
      }
      options.onChange?.(view);
      root.dispatchEvent(new CustomEvent('connectors-painted', {bubbles: true}));
      watchWhatsApp();
      watchRestart();
    }
    // While an extension's change waits for Qorqbot's restart, read the list again every few seconds; a read that
    // fails while Qorqbot restarts is tried again, and the card shows the result once Qorqbot is back.
    // The same after a card's request got no answer (Qorqbot may be restarting): until the list loads again.
    function watchRestart() {
      clearTimeout(restartTimer);
      if (!view?.connectors.some(item => item.kind === 'contract' && (item.restarting || item.unreached)) || !live()) return;
      restartTimer = setTimeout(async () => {
        if (!live()) return;
        const mine = ++listRead;
        try {
          const next = coreData(await api('/root/connectors'));
          if (live() && mine === listRead) { view = next; error(''); paint(); }
        } catch { if (mine === listRead) watchRestart(); }
      }, 3000);
    }
    // A request of a contract card that failed: its sentence on that card. With no answer at all (Qorqbot stopping or
    // restarting) the card says so and the list is read again until Qorqbot answers.
    function cardFailure(id, failure, fallback) {
      if (!live() || !view) return;
      const none = unreached(failure);
      const text = none ? extensionCards.UNREACHED : failure.message || fallback;
      view.connectors = view.connectors.map(item => item.kind === 'contract' && item.id === id
        ? {...item, result: text, failed: true, unreached: none} : item);
      paint();
    }
    // WhatsApp's own setup state replaces the card's entry: {state, note, qr, send_ready} from its route.
    function setWhatsApp(setup) {
      const item = view?.connectors.find(entry => entry.id === 'whatsapp');
      if (!item) return;
      const same = item.status === 'linking' && setup.state === 'linking' && !!item.qr === !!setup.qr;
      Object.assign(item, {status: setup.state, note: setup.note, qr: setup.qr, send_ready: setup.send_ready});
      waVersion += 1;
      // A new code while linking swaps the image only, so the card does not flicker every few seconds.
      const image = same && root.querySelector('img[data-cx-wa-code]');
      if (image) { image.src = `/api/root/extensions/whatsapp/qr?v=${waVersion}`; watchWhatsApp(); }
      else paint();
    }
    // Reads the setup route every few seconds, only while the card shows the code or is finishing and the page is visible.
    function watchWhatsApp() {
      clearTimeout(waTimer);
      const item = view?.connectors.find(entry => entry.id === 'whatsapp');
      if (!item || !connectorsModel.polls(item) || !live() || document.visibilityState !== 'visible') return;
      waTimer = setTimeout(async () => {
        if (!live() || document.visibilityState !== 'visible') return;
        try { setWhatsApp(coreData(await api('/root/extensions/whatsapp/setup'))); }
        catch (failure) { if (live()) { error(failure.message || 'WhatsApp could not be checked.'); watchWhatsApp(); } }
      }, item.qr === undefined ? 0 : 3000);   // the connectors list carries no code; read it at once
    }
    async function whatsapp(button, change) {
      button.disabled = true;
      try { setWhatsApp(coreData(await post('/root/extensions/whatsapp/setup', {action: change}))); error(''); }
      catch (failure) { if (live()) { error(failure.message || 'WhatsApp could not be linked.'); button.disabled = false; } }
    }
    async function load() {
      if (!live()) return;
      const mine = ++listRead;
      try {
        const next = coreData(await api('/root/connectors'));
        if (!live() || mine !== listRead) return;
        view = next; error(''); paint();
      } catch (failure) {
        if (!live() || mine !== listRead) return;
        error(failure.message || 'Extensions could not be loaded.');
        // No answer at all (Qorqbot stopping or restarting): read again until it answers, which clears the line.
        if (unreached(failure)) { clearTimeout(loadTimer); loadTimer = setTimeout(load, 3000); }
      }
    }
    // A contract extension's action: one POST to its connection route, answered with its entry as it is then.
    async function connection(id, body) {
      const next = coreData(await post(extensionCards.path(id), body));
      if (!live() || !view) return;
      view.connectors = view.connectors.map(item => item.kind === 'contract' && item.id === id ? next : item);
      error(''); paint();
    }
    root.addEventListener('click', async event => {
      const fieldsForm = event.target.closest('[data-ext-form]');
      if (fieldsForm && event.target.closest('[data-ext-form-cancel]')) { fieldsForm.reset(); fieldsForm.hidden = true; return; }
      const choices = event.target.closest('[data-ext-remove]');
      if (choices && event.target.closest('[data-ext-remove-cancel]')) { choices.hidden = true; return; }
      const extAction = event.target.closest('[data-ext-action]');
      if (extAction) {
        const id = extAction.dataset.cxId;
        if (extAction.dataset.extAction === 'key') {
          const form = root.querySelector(`[data-ext-form="${CSS.escape(id)}"]`);
          form.hidden = !form.hidden;
          if (!form.hidden) form.querySelector('input')?.focus();
          return;
        }
        if (extAction.dataset.extAction === 'remove-choose') {
          const panel = root.querySelector(`[data-ext-remove="${CSS.escape(id)}"]`);
          panel.hidden = !panel.hidden;
          return;
        }
        // The two removal choices are one question: pressing either disables both until the answer is back.
        const pressed = extAction.closest('[data-ext-remove]') ? [...extAction.closest('[data-ext-remove]').querySelectorAll('[data-ext-action]')] : [extAction];
        pressed.forEach(button => { button.disabled = true; });
        try { await connection(id, extensionCards.request[extAction.dataset.extAction](extAction.dataset.extVersion)); }
        catch (failure) { cardFailure(id, failure, 'The extension could not be changed.'); }
        return;
      }
      const siteAction = event.target.closest('[data-cx-site-action]');
      if (siteAction) {
        siteAction.disabled = true;
        if (!await loadAccounts({action: siteAction.dataset.cxSiteAction, site: siteAction.dataset.cxSiteName})) siteAction.disabled = false;
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
      if (button.dataset.cxAction === 'wa-link') { await whatsapp(button, 'link'); return; }
      if (button.dataset.cxAction === 'wa-cancel') { await whatsapp(button, 'cancel'); return; }
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
    root.addEventListener('change', async event => {
      const toggle = event.target.closest('[data-ext-switch]');
      if (!toggle) return;
      toggle.disabled = true;
      try { await connection(toggle.dataset.cxId, extensionCards.request.switch(toggle.dataset.agentId, toggle.checked)); }
      catch (failure) { cardFailure(toggle.dataset.cxId, failure, 'The extension could not be switched.'); }
    });
    root.addEventListener('submit', async event => {
      const fieldsForm = event.target.closest('[data-ext-form]');
      if (fieldsForm) {
        event.preventDefault();
        const problem = fieldsForm.querySelector('[data-ext-error]'), buttons = fieldsForm.querySelectorAll('button');
        const values = Object.fromEntries([...fieldsForm.querySelectorAll('input')].map(input => [input.name, input.value.trim()]));
        problem.hidden = true;
        buttons.forEach(button => { button.disabled = true; });
        try { fieldsForm.reset(); fieldsForm.hidden = true; await connection(fieldsForm.dataset.extForm, extensionCards.request.accept(values)); }
        catch (failure) {
          if (!live()) return;
          fieldsForm.hidden = false;
          problem.textContent = failure.message || 'The values could not be saved.'; problem.hidden = false;
          buttons.forEach(button => { button.disabled = false; });
        }
        return;
      }
      const form = event.target.closest('[data-cx-key]');
      if (!form) return;
      event.preventDefault();
      await saveKey(form, form.elements.key.value.trim());
    });
    // An extension can be set up outside this page (Ordubashy links WhatsApp from Chat), so returning here reads again.
    const focus = () => { if (live() && document.visibilityState === 'visible') load(); };
    window.addEventListener('focus', focus);
    document.addEventListener('visibilitychange', focus);
    const watch = new MutationObserver(() => {
      if (live()) return;
      clearTimeout(browserTimer); clearTimeout(accountsTimer); clearTimeout(waTimer); clearTimeout(restartTimer); clearTimeout(loadTimer); watch.disconnect();
      window.removeEventListener('focus', focus); document.removeEventListener('visibilitychange', focus);
    });
    watch.observe(container.ownerDocument.body, {childList: true, subtree: true});
    load(); loadBrowser(); loadAccounts();
    return {root, reload: () => load(), get view() { return view; }};
  }
  return {mount, PRIVACY};
})();
