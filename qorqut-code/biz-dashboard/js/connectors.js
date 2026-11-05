// Connectors: the tools the owner connects so agents can use them (browser, Playwright, Gmail and more).
// `connectorsModel` is pure (unit tested with `node --test biz-dashboard/tests/*.test.js`); `connectorsPanel` renders the
// panel shown in Settings and in the walkthrough. Status comes from /root/connectors (bazarlyk.connectors).
const connectorsModel = (() => {
  const CLAUDE_CONNECTORS_URL = 'https://claude.ai/settings/connectors';
  const CHROME_GUIDE_URL = 'https://code.claude.com/docs/en/chrome';
  const icon = path => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
  const ICONS = {
    browser: icon('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/>'),
    chrome: icon('<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 9h18M7 6.5h.01M10 6.5h.01"/><path d="M9.5 14.5l2 2 3.5-4"/>'),
    playwright: icon('<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 9h18"/><path d="M10 12.5l4 2.5-4 2.5z"/>'),
    gmail: icon('<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M4 7l8 6 8-6"/>'),
    google_calendar: icon('<rect x="3.5" y="5" width="17" height="15" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>'),
    google_drive: icon('<path d="M4 18.5h16M8.5 4.5h7L21 14l-2.5 4.5M8.5 4.5L3 14l2.5 4.5M8.5 4.5L14 14H3"/>'),
    whatsapp: icon('<path d="M20 11.6a8 8 0 0 1-11.8 7L4 20l1.4-4.1A8 8 0 1 1 20 11.6z"/>'),
    notion: icon('<rect x="4" y="3.5" width="16" height="17" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>'),
    slack: icon('<path d="M9 3.5v17M15 3.5v17M3.5 9h17M3.5 15h17"/>'),
  };
  // Owner-facing copy. `does` is what agents can do with it, `why` why it helps, `privacy` what it can see and where it runs.
  const COPY = {
    browser: {name: 'Helper browser', does: 'Helper agents Ordubashy hands work to get a separate, signed-out browser of their own for web tasks.',
      why: 'Lets background helpers research and fill in forms without touching your own browser.',
      privacy: 'Runs on this computer in a separate browser profile. Nothing you are signed in to is shared with it.',
      needs: 'Needs Google Chrome and the free agent-browser command.'},
    chrome: {name: 'Your Chrome', does: 'Ordubashy opens and reads sites in your own Chrome, where you are already signed in (LinkedIn, Gmail on the web, your dashboards), only where you allow it.',
      why: 'Most useful sites need your login. This way an agent never needs your password.',
      privacy: 'Runs in your Chrome on this computer. You choose in the extension which sites Claude may use.',
      needs: 'Needs Claude Code on a paid Claude plan and the Claude in Chrome extension.',
      steps: ['Install the Claude in Chrome extension and sign in to it with your Claude account.', 'Press Turn on here.', 'Your agents use it from their next start.'],
      install: 'https://chromewebstore.google.com/detail/claude/fcoeoabgfenejglbffodgkkbkcdhcgfn'},
    playwright: {name: 'Playwright', does: 'Agents drive a separate, clean browser step by step: test your website, repeat a web task, save pages.',
      why: 'The most reliable way to check a site or do the same web task many times.',
      privacy: 'Runs locally through npx with its own browser. Your Chrome profile and passwords are never used.',
      needs: 'Needs Node.js on this computer. Uses Google Chrome when it is installed; otherwise the agent installs Playwright’s own browser on first use.'},
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
    unsupported: {label: 'Not on Windows', tone: 'idle'}, needs_extension: {label: 'Needs the Chrome extension', tone: 'warn'}, coming_soon: {label: 'Coming soon on Mac', tone: 'idle'}, needs_install: {label: 'Needs a quick install', tone: 'warn'},
  };
  const status = item => STATUS[item.status] || STATUS.unknown;
  // What the card's main button does: 'toggle' (on/off here), 'claude' (connect on claude.ai), 'none'.
  function action(item) {
    if (item.status === 'unsupported' || item.status === 'coming_soon') return {kind: 'none'};
    if (item.kind === 'local') return {kind: 'toggle', label: ['connected', 'needs_extension'].includes(item.status) ? 'Turn off' : 'Turn on'};
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
    if (item.id === 'chrome' && item.status === 'needs_extension') return 'On, but the Claude in Chrome extension isn’t installed in your Chrome yet. Install it from the Chrome Web Store and sign in to it with your Claude account.';
    if (item.id === 'chrome' && item.status === 'off' && item.extension === false) return 'Also needs the Claude in Chrome extension in your Chrome.';
    if (item.id === 'chrome' && item.status === 'unsupported') return 'Claude in Chrome can’t reach Windows Chrome from Qorqbot’s Linux side (WSL). Use Playwright instead.';
    if (item.kind === 'claude' && item.status === 'unavailable')
      return item.reason === 'claude_missing' ? 'Install Claude Code and sign in with your Claude account to use Claude’s connectors.'
        : 'Claude Code could not list its connectors. Open a terminal and run “claude” once to sign in.';
    if (item.kind === 'claude' && item.status === 'failed') return 'Claude could not reach it. Open Claude’s connectors and connect it again.';
    if (item.kind === 'claude' && item.status !== 'connected') return 'Claude Code only. Codex can’t use Claude’s connectors.';
    if (item.id === 'browser' && item.status === 'needs_install') {
      const chrome = item.missing?.includes('chrome'), tool = item.missing?.includes('agent_browser');
      return chrome && tool ? 'Install Google Chrome, then run the command below in Terminal.'
        : chrome ? 'Install Google Chrome from google.com/chrome.' : 'Run the command below in Terminal.';
    }
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
  return {COPY, ICONS, STATUS, CLAUDE_CONNECTORS_URL, CHROME_GUIDE_URL, status, action, note, summary, account};
})();
if (typeof module !== 'undefined') module.exports = connectorsModel;

const connectorsPanel = (() => {
  const POLL_MS = 3000;
  const PRIVACY = `<section class="cx-privacy" aria-labelledby="cx-privacy-title">
      <div class="cx-privacy-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.3-7.5 9.5-4.3-1.2-7.5-4.9-7.5-9.5V6z"/><path d="M8.8 12.2l2.2 2.2 4.3-4.6"/></svg></div>
      <div><h2 id="cx-privacy-title">Private by design. Qorqbot collects nothing.</h2>
      <p>No Qorqbot account, no tracking, no Qorqbot servers. Qorqbot runs on this computer and everything it keeps stays here.</p>
      <ul><li>Your sign-ins stay with Google and Claude. Qorqbot never sees or stores your passwords.</li>
      <li>Every connector is optional, and you can turn it off at any time.</li>
      <li>What you ask your agents goes to Claude or Codex under your own account, the same as when you use them directly.</li></ul></div>
    </section>`;
  function card(item) {
    const copy = connectorsModel.COPY[item.id] || {name: item.id, does: ''};
    const status = connectorsModel.status(item), action = connectorsModel.action(item), note = connectorsModel.note(item);
    const button = action.kind === 'none' ? '' : `<button type="button" class="${action.quiet || action.label === 'Turn off' ? 'quiet' : ''}" data-cx-action="${esc(action.kind)}" data-cx-id="${esc(item.id)}">${esc(action.label)}</button>`;
    const steps = copy.steps ? `<ol>${copy.steps.map(step => `<li>${esc(step)}</li>`).join('')}</ol>` : '';
    const guide = item.id === 'chrome' ? `<a href="${connectorsModel.CHROME_GUIDE_URL}" target="_blank" rel="noopener noreferrer">How to install Claude in Chrome</a>` : '';
    return `<article class="cx-card" data-cx-card="${esc(item.id)}" data-tone="${status.tone}">
      <div class="cx-card-head"><span class="cx-icon">${connectorsModel.ICONS[item.id] || ''}</span>
        <div class="cx-card-title"><h3>${esc(copy.name)}</h3><span class="cx-status" data-tone="${status.tone}">${esc(status.label)}</span></div>
        ${button}</div>
      <p class="cx-does">${esc(copy.does)}</p>
      ${note ? `<p class="cx-note" data-tone="${note.tone}">${esc(note.text)}</p>` : ''}
      ${item.id === 'chrome' && item.extension === false && copy.install ? `<a class="cx-get" href="${esc(copy.install)}" target="_blank" rel="noopener noreferrer">Get Claude in Chrome</a>` : ''}
      ${item.install && item.missing?.includes('agent_browser') ? `<div class="cx-command"><code>${esc(item.install)}</code><button type="button" class="quiet" data-cx-copy="${esc(item.install)}">Copy</button></div>` : ''}
      <details class="cx-more"><summary>Why it helps and what it can see</summary>
        <p><b>Why:</b> ${esc(copy.why || '')}</p><p><b>Privacy:</b> ${esc(copy.privacy || '')}</p>
        ${copy.needs ? `<p><b>Needs:</b> ${esc(copy.needs)}</p>` : ''}${steps}${guide}</details>
    </article>`;
  }
  // Settings shows each connector as one compact row; the walkthrough and the cards above share the same model.
  function row(item) {
    const copy = connectorsModel.COPY[item.id] || {name: item.id, does: ''};
    const status = connectorsModel.status(item), action = connectorsModel.action(item), note = connectorsModel.note(item);
    const button = action.kind === 'none' ? '' : `<button type="button" class="${action.quiet || action.label === 'Turn off' ? 'quiet' : ''}" data-cx-action="${esc(action.kind)}" data-cx-id="${esc(item.id)}">${esc(action.label)}</button>`;
    const steps = copy.steps ? `<ol>${copy.steps.map(step => `<li>${esc(step)}</li>`).join('')}</ol>` : '';
    const guide = item.id === 'chrome' ? `<a href="${connectorsModel.CHROME_GUIDE_URL}" target="_blank" rel="noopener noreferrer">How to install Claude in Chrome</a>` : '';
    const search = [copy.name, copy.does, copy.why, status.label, item.id.replace('_', ' ')].join(' ').toLowerCase();
    return `<div class="cx-row" data-cx-card="${esc(item.id)}" data-tone="${status.tone}" data-search="${esc(search)}">
      <span class="cx-icon">${connectorsModel.ICONS[item.id] || ''}</span>
      <div class="cx-row-main">
        <div class="cx-row-title"><h3>${esc(copy.name)}</h3><span class="cx-status" data-tone="${status.tone}">${esc(status.label)}</span></div>
        <p class="cx-row-does">${esc(copy.does)}</p>
        ${note ? `<p class="cx-note" data-tone="${note.tone}">${esc(note.text)}</p>` : ''}
        ${item.id === 'chrome' && item.extension === false && copy.install ? `<a class="cx-get" href="${esc(copy.install)}" target="_blank" rel="noopener noreferrer">Get Claude in Chrome</a>` : ''}
        ${item.install && item.missing?.includes('agent_browser') ? `<div class="cx-command"><code>${esc(item.install)}</code><button type="button" class="quiet" data-cx-copy="${esc(item.install)}">Copy</button></div>` : ''}
        <details class="cx-more"><summary>What it can see</summary>
          <p><b>Why:</b> ${esc(copy.why || '')}</p><p><b>Privacy:</b> ${esc(copy.privacy || '')}</p>
          ${copy.needs ? `<p><b>Needs:</b> ${esc(copy.needs)}</p>` : ''}${steps}${guide}</details>
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
      <div class="cx-head"><div>${options.rows ? '' : '<h2>Connect your tools</h2>'}<p>Give your agents the tools they need. Your Chrome, Playwright and Gmail are strongly recommended; everything is optional.</p></div>
        <div class="cx-progress" data-cx-progress aria-live="polite"></div></div>
      <p class="cx-error" data-cx-error role="alert" hidden></p>
      <p class="cx-account" data-cx-account hidden></p>
      <h3 class="cx-group">Strongly recommended</h3><div class="${options.rows ? 'cx-rows' : 'cx-grid'}" data-cx-tier="recommended"><p class="muted small">Loading connectors…</p></div>
      <h3 class="cx-group">Also useful</h3><div class="${options.rows ? 'cx-rows' : 'cx-grid'}" data-cx-tier="suggested"></div>
      ${CLAUDE_STEPS}
      <p class="cx-foot muted small">Changes reach an agent the next time it starts. <button type="button" class="cx-link" data-cx-refresh>Check again</button></p>`;
    container.appendChild(root);
    let view = null, timer = 0, busy = false, awaitingClaude = false;
    const live = () => root.isConnected;
    const error = text => { const el = root.querySelector('[data-cx-error]'); el.textContent = text || ''; el.hidden = !text; };
    function paint() {
      if (!view) return;
      for (const tier of ['recommended', 'suggested'])
        root.querySelector(`[data-cx-tier="${tier}"]`).innerHTML = view.connectors.filter(item => item.tier === tier).map(options.rows ? row : card).join('');
      const progress = connectorsModel.summary(view.connectors);
      root.querySelector('[data-cx-progress]').innerHTML = `<span class="cx-progress-text">${progress.done} of ${progress.total} recommended connected</span>
        <span class="cx-progress-bar" aria-hidden="true"><i style="width:${Math.round(progress.done / progress.total * 100)}%"></i></span>`;
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
      if (event.target.closest('[data-cx-refresh]')) { await load({refresh: true}); return; }
      const copy = event.target.closest('[data-cx-copy]');
      if (copy) {
        try { await navigator.clipboard.writeText(copy.dataset.cxCopy); copy.textContent = 'Copied'; }
        catch (_) { copy.textContent = 'Select and copy'; }
        setTimeout(() => { if (copy.isConnected) copy.textContent = 'Copy'; }, 2000);
        return;
      }
      const button = event.target.closest('[data-cx-action]');
      if (!button || busy) return;
      const item = view?.connectors.find(entry => entry.id === button.dataset.cxId);
      if (!item) return;
      if (button.dataset.cxAction === 'claude') {
        window.open(connectorsModel.CLAUDE_CONNECTORS_URL, '_blank', 'noopener,noreferrer');
        awaitingClaude = true; paint(); return;
      }
      busy = true; button.disabled = true;
      // On covers "needs the extension" too: its button reads Turn off.
      const enabling = !['connected', 'needs_extension'].includes(item.status);
      try {
        await load({id: item.id, enabled: enabling});
        if (live() && !root.querySelector('[data-cx-error]').textContent)
          toast(`${connectorsModel.COPY[item.id].name} ${enabling ? 'is on' : 'is off'}. Your agents get the change the next time they start.`);
      } finally { busy = false; if (button.isConnected) button.disabled = false; }
    });
    // Returning from claude.ai: check again by itself, once per return.
    const focus = () => { if (awaitingClaude && live() && document.visibilityState === 'visible') load({refresh: true}); };
    window.addEventListener('focus', focus);
    document.addEventListener('visibilitychange', focus);
    const watch = new MutationObserver(() => {
      if (live()) return;
      clearTimeout(timer); watch.disconnect();
      window.removeEventListener('focus', focus); document.removeEventListener('visibilitychange', focus);
    });
    watch.observe(container.ownerDocument.body, {childList: true, subtree: true});
    load();
    return {root, reload: () => load({refresh: true}), get view() { return view; }};
  }
  return {mount, PRIVACY};
})();
